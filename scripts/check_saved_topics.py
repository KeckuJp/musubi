#!/usr/bin/env python3
"""Explicit expectations against saved bag records, never physical packet-loss claims."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile

from rosbags.rosbag2 import Reader
try:
    from .convert_rosbag_observations import (convert_metadata, YAML, sqlite_storage_schemas,
        TF, HASHES, LIMIT, qualify_connection, decode, transform_quantities)
    from .check_declared_schema_units import _unique
except ImportError:
    from convert_rosbag_observations import (convert_metadata, YAML, sqlite_storage_schemas,
        TF, HASHES, LIMIT, qualify_connection, decode, transform_quantities)
    from check_declared_schema_units import _unique


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check(path, expectation):
    path = Path(path)
    with Path(expectation).open("rb") as stream:
        raw = stream.read(256001)
    if len(raw) > 256000:
        raise ValueError("expectation exceeds bound")
    exp = json.loads(raw, object_pairs_hook=_unique)
    frame_mode = isinstance(exp, dict) and exp.get("schema") == "saved-frame-expectation/v1"
    keys = {"schema", "database_sha256", "metadata_sha256", "clock", "start_ns", "stop_ns", "edges" if frame_mode else "topics"}
    if type(exp) is not dict or set(exp) != keys or exp["schema"] not in ("saved-topic-expectation/v1", "saved-frame-expectation/v1"):
        raise ValueError("invalid expectation schema")
    if exp["clock"] != "BAG_RECORD_TIMESTAMP_NS":
        raise ValueError("expectation must use the saved record axis")
    start, stop = exp["start_ns"], exp["stop_ns"]
    if any(type(t) is not int or not 0 <= t < 2**63 for t in (start, stop)) or start >= stop:
        raise ValueError("invalid half-open record window")
    edges = exp.get("edges", [])
    if frame_mode and (not isinstance(edges, list) or not 1 <= len(edges) <= 512
            or any(type(e) is not dict or set(e) != {"topic", "parent", "child"}
                or any(not isinstance(v, str) or not v or len(v.encode()) > 4096 for v in e.values())
                or e["parent"] == e["child"] for e in edges)
            or len({(e["topic"], e["parent"], e["child"]) for e in edges}) != len(edges)):
        raise ValueError("invalid finite expected direct edge set")
    topics = list(dict.fromkeys(e["topic"] for e in edges)) if frame_mode else exp["topics"]
    if (not isinstance(topics, list) or not 1 <= len(topics) <= 512
            or any(not isinstance(t, str) or not t or len(t.encode()) > 4096 for t in topics)
            or len(set(topics)) != len(topics)):
        raise ValueError("invalid finite expected topic set")
    if path.is_symlink() or not path.is_dir():
        raise ValueError("saved directory required")
    metadata = path / "metadata.yaml"
    if metadata.is_symlink() or metadata.stat().st_size > 256000:
        raise ValueError("invalid metadata input")
    metadata_bytes = metadata.read_bytes()
    if digest(metadata_bytes) != exp["metadata_sha256"]:
        raise ValueError("metadata digest mismatch")
    with tempfile.TemporaryDirectory(prefix="musubi-topic-") as temp:
        snapshot = Path(temp)
        (snapshot / "metadata.yaml").write_bytes(metadata_bytes)
        convert_metadata(snapshot / "metadata.yaml")  # Existing version/count/alias checks.
        info = YAML(typ="safe").load(metadata_bytes)["rosbag2_bagfile_information"]
        names = info["relative_file_paths"]
        if (info["storage_identifier"] != "sqlite3" or info.get("compression_mode", "")
                or len(names) != 1 or Path(names[0]).name != names[0] or names[0] in (".", "..")):
            raise ValueError("single uncompressed sqlite3 snapshot required")
        database = path / names[0]
        if database.is_symlink() or not database.is_file() or database.stat().st_size > 2 * 1024**3:
            raise ValueError("database exceeds saved scope")
        # Refuse a live WAL view; copy and hash the same byte stream before opening.
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise ValueError("database must be an offline checkpointed snapshot")
        before = database.stat()
        sha = hashlib.sha256()
        copied = 0
        with database.open("rb") as source, (snapshot / names[0]).open("wb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                copied += len(block)
                if copied > 2 * 1024**3:
                    raise ValueError("database copy exceeds bound; incomplete")
                sha.update(block); target.write(block)
        after = database.stat()
        if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                (after.st_size, after.st_mtime_ns, after.st_ino)
                or sha.hexdigest() != exp["database_sha256"]):
            raise ValueError("database changed or digest mismatch")
        counts = dict.fromkeys(topics, 0)
        edge_counts = {(e["topic"], "hex:" + e["parent"].encode().hex(),
                        "hex:" + e["child"].encode().hex()): 0 for e in edges}
        decoded_tf_records = 0
        all_counts, first, last, total = {}, None, None, 0
        # The fixed Reader JOIN omits orphan topic IDs. Audit the immutable copy's
        # actual table count first, not just the rows exposed by that JOIN.
        with sqlite3.connect((snapshot / names[0]).as_uri() + "?mode=ro", uri=True) as db:
            database_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            orphan = db.execute("SELECT 1 FROM messages LEFT JOIN topics ON messages.topic_id=topics.id "
                                "WHERE topics.id IS NULL LIMIT 1").fetchone()
            if database_count > 5_000_000 or orphan is not None:
                raise ValueError("database record bound or orphan topic; incomplete census")
        with Reader(snapshot) as reader:
            if sqlite_storage_schemas(reader) != (4,):
                raise ValueError("sqlite schema4 required")
            if frame_mode:
                with sqlite3.connect((snapshot / names[0]).as_uri() + "?mode=ro", uri=True) as db:
                    for connection in reader.connections:
                        if connection.topic in counts:
                            stored = db.execute("SELECT name,type,serialization_format,type_description_hash "
                                                "FROM topics WHERE id=?", (connection.id,)).fetchone()
                            declared = (connection.topic, connection.msgtype,
                                        connection.ext.serialization_format, connection.digest)
                            if stored != declared:
                                raise ValueError("selected TF storage and metadata schema declarations disagree")
                            if connection.msgtype != TF:
                                raise ValueError("expected frame topic requires fixed TFMessage type")
                            qualify_connection(connection, {TF: HASHES[TF]})
            for connection, timestamp, payload in reader.messages():
                total += 1
                if total > 5_000_000:
                    raise ValueError("record census exceeds bound; incomplete")
                if not 0 <= timestamp < 2**63:
                    raise ValueError("invalid saved timestamp")
                first = timestamp if first is None else min(first, timestamp)
                last = timestamp if last is None else max(last, timestamp)
                all_counts[connection.topic] = all_counts.get(connection.topic, 0) + 1
                if connection.topic in counts and start <= timestamp < stop:
                    counts[connection.topic] += 1
                    if frame_mode:
                        if len(payload) > LIMIT:
                            raise ValueError("TF payload exceeds existing decode bound; incomplete")
                        quantities = transform_quantities(decode(payload, TF))
                        decoded_tf_records += 1
                        for row in quantities:
                            key = (connection.topic, row.get("tf_parent_frame_hex"), row.get("tf_child_frame_hex"))
                            if key in edge_counts:
                                edge_counts[key] += 1
            declared = {entry["topic_metadata"]["name"]: entry["message_count"]
                        for entry in info["topics_with_message_count"]}
            if total != database_count or total != info["message_count"] or any(all_counts.get(t, 0) != n for t, n in declared.items()) or set(all_counts) - set(declared):
                raise ValueError("actual record count differs from metadata")
        covered = first is not None and start >= first and stop <= last + 1
        output = io.StringIO(newline="")
        fields = ["record_time_us", "topic_hex", "disposition", "window_records", "start_ns", "stop_ns",
                  "record_axis", "database_sha256", "metadata_sha256", "expectation_sha256"]
        if frame_mode:
            fields += ["parent_frame_hex", "child_frame_hex", "window_transforms"]
        writer = csv.DictWriter(output, fields, lineterminator="\n"); writer.writeheader()
        comparisons = [(key[0], count, key) for key, count in edge_counts.items()] if frame_mode else [(t, c, None) for t, c in counts.items()]
        for topic, count, edge in comparisons:
            row = dict(record_time_us=0, topic_hex="hex:" + topic.encode().hex(),
                disposition="OBSERVED" if count else "NOT_OBSERVED_IN_SAVED_WINDOW" if covered else "INDETERMINATE_WINDOW_COVERAGE",
                window_records=counts[topic], start_ns=start, stop_ns=stop, record_axis=exp["clock"],
                database_sha256=sha.hexdigest(), metadata_sha256=digest(metadata_bytes), expectation_sha256=digest(raw))
            if edge:
                row.update(parent_frame_hex=edge[1], child_frame_hex=edge[2], window_transforms=count)
            writer.writerow(row)
        return output.getvalue(), dict(source_records=total, actual_topic_counts=all_counts,
            first_record_ns=first, last_record_ns=last, window_within_record_extent=covered,
            output_records=len(comparisons), decoded_tf_records=decoded_tf_records, clock="Unknown", expectation_source_hex="hex:" + raw.hex(),
            metadata_source_hex="hex:" + metadata_bytes.hex(),
            scope=("saved qualified direct TF edge presence only; no interpolation, inverse, transitive path, runtime or physical completeness"
                   if frame_mode else "saved record presence only; no payload decode, cadence, physical completeness, frame or loss cause"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag", type=Path); parser.add_argument("expectation", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output, report = check(args.bag, args.expectation)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "observations.csv").write_text(output)
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
