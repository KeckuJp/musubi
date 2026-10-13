#!/usr/bin/env python3
"""Supplied systemd v257 kernel-coredump journal metadata -> common-reader CSV."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_journal_storage_errors import (
    CLOCKS, LIMIT, MAX_ENTRIES, MAX_FIELDS, MAX_LINE, MESSAGE_ID, OUTPUT_LIMIT,
    SOURCE_PIN, _identity, _microseconds, _text, invalid_constant, unique_object,
)

PRODUCER = "systemd-v257-kernel-coredump"
COREDUMP_ID = "fc2e22bc6ee647b6b90729ab34a250b1"
IDENTITIES = {
    "COREDUMP_SIGNAL_NAME": "reported_signal_name_hex", "COREDUMP_COMM": "reported_comm_hex",
    "COREDUMP_EXE": "reported_exe_hex", "COREDUMP_UNIT": "reported_unit_hex",
    "COREDUMP_FORWARDED": "reported_forwarded_hex", "_PID": "reporter_pid_hex",
    "_BOOT_ID": "reported_boot_id_hex", "_MACHINE_ID": "reported_machine_id_hex",
    "__CURSOR": "journal_cursor_hex",
}
NUMERIC = {"COREDUMP_PID": "reported_pid", "COREDUMP_SIGNAL": "reported_signal",
           "COREDUMP_TIMESTAMP": "reported_crash_time_us"}
INTERPRETED = set(IDENTITIES) | set(NUMERIC) | {"MESSAGE_ID", "__REALTIME_TIMESTAMP", "COREDUMP"}
COLUMNS = ["record_time_us", "record_time_basis", "crash_event", "event_basis",
           "reported_pid", "reported_pid_status", "reported_signal", "reported_signal_status",
           "reported_crash_time_us", "reported_crash_time_us_status", "reported_crash_time_precision_us",
           *IDENTITIES.values(), "identity_encoding_notes", "identity_basis", "dump_body_status",
           "source_record_index", "source_sha256", "record_sha256", "uninterpreted_field_count",
           "uninterpreted_field_names_hex", "source_pin"]


def _number(entry, name, index):
    if name not in entry:
        return "", "ABSENT"
    if entry[name] is None:
        return "", "WITHHELD_NULL"
    token = _text(entry, name, index)
    maximum = 2 ** 53 - 1 if name == "COREDUMP_TIMESTAMP" else 2 ** 31 - 1
    if not re.fullmatch(r"[0-9]{1,19}", token) or int(token) > maximum:
        raise ValueError(f"entry {index}: {name} outside finite decimal bound")
    value = int(token)
    if name == "COREDUMP_TIMESTAMP":
        if value % 1000000:
            raise ValueError(f"entry {index}: kernel timestamp must retain whole-second precision")
    elif value == 0:
        raise ValueError(f"entry {index}: {name} must be positive")
    return value, "REPORTED"


def metadata_records(data):
    """Shared bounded metadata framing; caller owns selection and record meaning."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized input bound")
    cursors, count = set(), 0
    for index, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        if len(raw) > MAX_LINE:
            raise ValueError(f"entry {index}: line bound exceeded")
        entry = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
        if not isinstance(entry, dict) or len(entry) > MAX_FIELDS:
            raise ValueError(f"entry {index}: expected object within field bound")
        count += 1
        if count > MAX_ENTRIES:
            raise ValueError("entry bound exceeded")
        if "COREDUMP" in entry and entry["COREDUMP"] is not None:
            raise ValueError(f"entry {index}: memory body is outside metadata-only input")
        cursor, cursor_note = _identity(entry, "__CURSOR", index)
        if "__CURSOR" in entry and cursor_note is None:
            if cursor in cursors:
                raise ValueError(f"entry {index}: repeated cursor")
            cursors.add(cursor)
        yield index, raw, entry


def convert(data, *, producer):
    if producer != PRODUCER:
        raise ValueError("explicit fixed kernel-coredump producer required")
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized input bound")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = {"source_sha256": digest, "source_pin": SOURCE_PIN, "declared_producer": producer,
              "entries": 0, "selected_entries": 0, "excluded_record_indices": [],
              "uninterpreted_fields_total": 0, "clock": "Unknown",
              "original_input_required": True,
              "excluded_scope": "FRAMING_SELECTOR_AND_NO_MEMORY_BODY_CHECK_ONLY",
              "claim": "PRODUCER_REPORT_NOT_CAUSE_OR_STORED_DUMP_OR_AUTHENTICATED_IDENTITY"}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    previous_time = None
    for index, raw, entry in metadata_records(data):
        report["entries"] += 1
        message_id = _text(entry, "MESSAGE_ID", index)
        if message_id is not None and not MESSAGE_ID.fullmatch(message_id):
            raise ValueError(f"entry {index}: malformed MESSAGE_ID")
        if message_id != COREDUMP_ID:
            report["excluded_record_indices"].append(index)
            continue
        time = _microseconds(entry, "__REALTIME_TIMESTAMP", index, required=True)
        if previous_time is not None and time < previous_time:
            raise ValueError("selected reception time decreased; no sorting or clock repair")
        previous_time = time
        row = {"record_time_us": time, "record_time_basis": CLOCKS["journal_reception_realtime"][2],
               "crash_event": "PRODUCER_REPORTED_COREDUMP",
               "event_basis": "EXACT_MESSAGE_ID_PLUS_CALLER_PRODUCER_DECLARATION_NOT_AUTHENTICATION",
               "identity_basis": "REPORTED_SUBJECT_NAMESPACE_NOT_GLOBAL_HOST_IDENTITY_OR_REPORTER_PID",
               "source_record_index": index, "source_sha256": digest,
               "record_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "source_pin": SOURCE_PIN,
               "dump_body_status": "WITHHELD_NULL" if "COREDUMP" in entry else "NOT_SUPPLIED"}
        for name, column in NUMERIC.items():
            row[column], row[column + "_status"] = _number(entry, name, index)
        row["reported_crash_time_precision_us"] = (1000000 if row["reported_crash_time_us"] != "" else "")
        notes = []
        for name, column in IDENTITIES.items():
            value, note = _identity(entry, name, index)
            row[column] = "hex:" + value.encode().hex() if name in entry and note is None else ""
            if name not in entry:
                note = "ABSENT"
            if note:
                notes.append(name + ":" + note.replace("SIDECAR", "SOURCE_INPUT"))
        row["identity_encoding_notes"] = " ".join(notes)
        unknown = sorted(set(entry) - INTERPRETED)
        row["uninterpreted_field_count"] = len(unknown)
        row["uninterpreted_field_names_hex"] = "hex:" + json.dumps(unknown, ensure_ascii=True).encode().hex()
        report["uninterpreted_fields_total"] += len(unknown)
        writer.writerow(row)
        report["selected_entries"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("CSV output bound exceeded")
    if not report["selected_entries"]:
        raise ValueError("no selected kernel-coredump report")
    result = output.getvalue()
    report["output_sha256"] = hashlib.sha256(result.encode()).hexdigest()
    if len(result.encode()) > OUTPUT_LIMIT or len(json.dumps(report, indent=2).encode()) > LIMIT:
        raise ValueError("output bound exceeded")
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--producer", required=True, choices=(PRODUCER,))
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        output, report = convert(data, producer=args.producer)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, UnicodeError) as error:
        # Do not print exception detail: malformed supplied input may contain confidential log content.
        print("coredump metadata conversion failed; no success claim (" + type(error).__name__ + ")",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
