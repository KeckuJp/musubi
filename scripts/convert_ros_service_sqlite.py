#!/usr/bin/env python3
"""Bounded saved GetParameters SQLite → standard MCAP → existing reported meaning."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from rosbags.rosbag2 import Writer, StoragePlugin
from rosbags.typesys import get_types_from_msg
from rosbags.typesys.msg import normalize_msgtype
from rosbags.typesys.store import Typestore

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_rosbag_observations as ros
from scripts.convert_rosbag_zstd_window import read_regular, safe_name, MAX_METADATA_BYTES

MAX_DB_BYTES = 16 * 1024**2
MAX_MESSAGES = 65536
MAX_TOPICS = 64
MAX_DERIVED_BYTES = 16 * 1024**2
MAX_REPORT_BYTES = 16 * 1024**2
MAX_OUTPUT_BYTES = 64 * 1024**2


def digest(data):
    return hashlib.sha256(data).hexdigest()


def count(value, label, limit=MAX_MESSAGES):
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError(f"invalid {label} count")
    return value


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()


def inspect_db(db, info, topic):
    required = {
        "schema": ["schema_version", "ros_distro"],
        "topics": ["id", "name", "type", "serialization_format", "offered_qos_profiles", "type_description_hash"],
        "message_definitions": ["id", "topic_type", "encoding", "encoded_message_definition", "type_description_hash"],
        "messages": ["id", "topic_id", "timestamp", "data"],
    }
    for table, columns in required.items():
        if db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchall() != [("table",)]:
            raise ValueError("required physical SQLite table missing")
        if [r[1] for r in db.execute(f"PRAGMA table_info({table})")] != columns:
            raise ValueError("unsupported SQLite table schema")
    if db.execute("SELECT schema_version FROM schema LIMIT 2").fetchall() != [(4,)]:
        raise ValueError("SQLite storage schema4 required")
    topics = db.execute("SELECT id,name,type,serialization_format,offered_qos_profiles,type_description_hash FROM topics LIMIT ?",
                        (MAX_TOPICS + 1,)).fetchall()
    if not 1 <= len(topics) <= MAX_TOPICS:
        raise ValueError("topic count bound")
    by_id = {r[0]: r for r in topics}
    if len(by_id) != len(topics) or len({r[1] for r in topics}) != len(topics):
        raise ValueError("ambiguous topic identity")
    if any(type(r[0]) is not int or r[0] < 1 or any(not isinstance(v, str) for v in r[1:]) for r in topics):
        raise ValueError("invalid topic declaration")
    selected = [r for r in topics if r[1] == topic]
    if (len(selected) != 1 or selected[0][2:4] != (ros.GET_PARAMETERS_EVENT, "cdr")
            or selected[0][5] != ros.GET_PARAMETERS_EVENT_HASH):
        raise ValueError("exact fixed service topic/type/CDR/hash required")
    selected = selected[0]
    definitions = db.execute("SELECT encoding,encoded_message_definition,type_description_hash FROM message_definitions WHERE topic_type=? LIMIT 2",
                             (ros.GET_PARAMETERS_EVENT,)).fetchall()
    if (len(definitions) != 1 or definitions[0][0] != "ros2msg"
            or not isinstance(definitions[0][1], str) or definitions[0][2] != ros.GET_PARAMETERS_EVENT_HASH):
        raise ValueError("unique fixed service definition/hash required")
    definition = definitions[0][1]
    store = Typestore()
    try:
        store.register(get_types_from_msg(definition, ros.GET_PARAMETERS_EVENT))
        actual_hash = store.hash_rihs01(normalize_msgtype(ros.GET_PARAMETERS_EVENT))
    except Exception as error:
        raise ValueError("invalid embedded service schema") from error
    if actual_hash != ros.GET_PARAMETERS_EVENT_HASH:
        raise ValueError("embedded schema is not the fixed service schema")
    records = db.execute("SELECT id,topic_id,timestamp,data FROM messages ORDER BY id LIMIT ?",
                         (MAX_MESSAGES + 1,)).fetchall()
    if not records or len(records) > MAX_MESSAGES:
        raise ValueError("message count bound")
    counts = dict.fromkeys(by_id, 0)
    previous_id, previous_time, selected_bytes = 0, -1, 0
    accounting, chosen = [], []
    for ident, topic_id, stamp, raw in records:
        if (type(ident) is not int or ident <= previous_id or type(topic_id) is not int or topic_id not in by_id
                or type(stamp) is not int or not 0 <= stamp <= 2**63 - 1 or stamp < previous_time
                or not isinstance(raw, bytes)):
            raise ValueError("invalid message ID/topic/order/capture/data")
        previous_id, previous_time = ident, stamp
        counts[topic_id] += 1
        is_selected = topic_id == selected[0]
        accounting.append(dict(source_message_id=ident, source_topic_id=topic_id,
            topic=by_id[topic_id][1], capture_ns=str(stamp), raw_bytes=len(raw), raw_sha256=digest(raw),
            derived_index=len(chosen) if is_selected else None,
            disposition="SELECTED_FIXED_SERVICE" if is_selected else "UNSELECTED_NOT_DECODED"))
        if is_selected:
            selected_bytes += len(raw)
            if selected_bytes > MAX_DERIVED_BYTES:
                raise ValueError("selected CDR byte bound")
            chosen.append((stamp, raw))
    declared = info.get("topics_with_message_count")
    if not isinstance(declared, list) or len(declared) != len(topics):
        raise ValueError("metadata topic accounting mismatch")
    seen = set()
    for entry in declared:
        item = entry.get("topic_metadata") if isinstance(entry, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item["name"] in seen:
            raise ValueError("ambiguous metadata topic")
        seen.add(item["name"])
        matches = [r for r in topics if r[1] == item["name"]]
        if (len(matches) != 1 or (item.get("type"), item.get("serialization_format"), item.get("type_description_hash"))
                != (matches[0][2], matches[0][3], matches[0][5])
                or count(entry.get("message_count"), "topic") != counts[matches[0][0]]):
            raise ValueError("metadata topic/type/hash/count mismatch")
    if count(info.get("message_count"), "total") != len(records) or not chosen:
        raise ValueError("metadata total mismatch or no selected messages")
    return definition, chosen, accounting


def convert(path, *, topic):
    if not isinstance(topic, str) or not topic or len(topic) > 1024 or any(ord(c) < 32 for c in topic):
        raise ValueError("explicit exact topic required")
    root_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata_raw = read_regular(root_fd, "metadata.yaml", MAX_METADATA_BYTES)
        metadata = ros.YAML(typ="safe").load(metadata_raw.decode("utf-8"))
        info = metadata.get("rosbag2_bagfile_information") if isinstance(metadata, dict) else None
        if (not isinstance(info, dict) or type(info.get("version")) is not int or info["version"] != 9
                or info.get("storage_identifier") != "sqlite3"
                or info.get("compression_mode") != "" or info.get("compression_format") != ""):
            raise ValueError("uncompressed metadata9 SQLite required")
        names = info.get("relative_file_paths")
        if not isinstance(names, list) or len(names) != 1:
            raise ValueError("exactly one declared member required")
        name = names[0]; safe_name(name)
        if not name.endswith(".db3"):
            raise ValueError("declared db3 member required")
        files = info.get("files")
        if (not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
                or files[0].get("path") != name
                or count(files[0].get("message_count"), "member") != count(info.get("message_count"), "total")):
            raise ValueError("metadata member accounting mismatch")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(name + suffix, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("saved closed SQLite required; sidecar present")
        original = read_regular(root_fd, name, MAX_DB_BYTES)
    finally:
        os.close(root_fd)
    with tempfile.TemporaryDirectory(prefix="musubi-service-sqlite-") as directory:
        temporary = Path(directory)
        snapshot = temporary / "snapshot.db3"; snapshot.write_bytes(original)
        try:
            with sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                definition, chosen, accounting = inspect_db(db, info, topic)
        except sqlite3.Error as error:
            raise ValueError("invalid saved SQLite database") from error
        derived = temporary / "derived"
        with Writer(derived, version=9, storage_plugin=StoragePlugin.MCAP) as writer:
            connection = writer.add_connection(topic, ros.GET_PARAMETERS_EVENT, msgdef=definition,
                rihs01=ros.GET_PARAMETERS_EVENT_HASH, serialization_format="cdr")
            for stamp, raw in chosen:
                writer.write(connection, stamp, raw)
        artifacts = {f"derived/{entry.name}": entry.read_bytes() for entry in derived.iterdir()}
        if sum(map(len, artifacts.values())) > MAX_DERIVED_BYTES:
            raise ValueError("derived MCAP artifact byte bound")
        text, semantic_report = ros.convert(derived)
    outputs = dict(artifacts, **{"observations.csv": text.encode(), "source.db3": original,
                                 "source-metadata.yaml": metadata_raw})
    if len(outputs["observations.csv"]) > MAX_REPORT_BYTES:
        raise ValueError("observation CSV byte bound")
    report = dict(schema="musubi.saved-service-sqlite.v1", source_member=name,
        source_sha256=digest(original), metadata_sha256=digest(metadata_raw),
        source_messages=len(accounting), selected_messages=len(chosen),
        unselected_messages=len(accounting) - len(chosen), messages=accounting,
        selected_topic=topic, fixed_type=ros.GET_PARAMETERS_EVENT, fixed_type_hash=ros.GET_PARAMETERS_EVENT_HASH,
        basis="SAVED_SCHEMA_QUALIFIED_EXPORT_NOT_LIVE_SERVICE_OR_CAUSALITY",
        clock_basis="Unknown; capture_ns retained exactly, not trusted UTC",
        preserved_not_interpreted="source metadata extras, embedded metadata table, unselected definitions/CDR",
        derived_artifacts={key: dict(bytes=len(value), sha256=digest(value)) for key, value in artifacts.items()},
        semantic_report=semantic_report)
    outputs["report.json"] = json_bytes(report)
    if len(outputs["report.json"]) > MAX_REPORT_BYTES or sum(map(len, outputs.values())) > MAX_OUTPUT_BYTES:
        raise ValueError("report or total output byte bound")
    return outputs, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path); parser.add_argument("output", type=Path)
    parser.add_argument("--topic", required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("fresh output directory required")
        outputs, report = convert(args.input, topic=args.topic)
        args.output.mkdir(parents=True, exist_ok=False)
        for name, raw in outputs.items():
            target = args.output / name; target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)
        print(json.dumps(dict(selected_messages=report["selected_messages"],
                              unselected_messages=report["unselected_messages"])))
    except (ValueError, OSError, AssertionError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
