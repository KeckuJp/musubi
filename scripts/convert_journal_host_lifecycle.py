#!/usr/bin/env python3
"""Fixed systemd v257 startup/final-action reports, not inferred physical reboots."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_journal_coredump import metadata_records
from scripts.convert_journal_storage_errors import (
    CLOCKS, LIMIT, MESSAGE_ID, OUTPUT_LIMIT, SOURCE_PIN, _identity, _microseconds, _text,
)

PRODUCER = "systemd-v257-system-manager-lifecycle"
EVENTS = {
    "b07a249cd024414a82dd00cd181378ff": "SYSTEM_MANAGER_STARTUP_FINISHED",
    "3ed0163e868a4417ab8b9e210407a96c": "POST_CRASH_FINAL_ACTION_FAILED",
}
IDENTITIES = {"_BOOT_ID": "reported_boot_id_hex", "_MACHINE_ID": "reported_machine_id_hex",
              "_PID": "reporter_pid_hex", "__CURSOR": "journal_cursor_hex"}
INTERPRETED = set(IDENTITIES) | {"MESSAGE_ID", "__REALTIME_TIMESTAMP", "COREDUMP"}
COLUMNS = ["record_time_us", "record_time_basis", "lifecycle_event", "event_basis",
           "reported_message_id", "operation_limit", *IDENTITIES.values(),
           "identity_encoding_notes", "source_record_index", "source_sha256", "record_sha256",
           "uninterpreted_field_count", "uninterpreted_field_names_hex", "source_pin"]


def convert(data, *, producer):
    if producer != PRODUCER:
        raise ValueError("explicit fixed system-manager producer required")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = {"source_sha256": digest, "source_pin": SOURCE_PIN, "declared_producer": producer,
              "entries": 0, "selected_entries": 0, "excluded_record_indices": [],
              "uninterpreted_fields_total": 0, "clock": "Unknown", "original_input_required": True,
              "claim": "PRODUCER_REPORT_NOT_PHYSICAL_REBOOT_POWERLOSS_OR_CAUSE"}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    previous = None
    for index, raw, entry in metadata_records(data):
        report["entries"] += 1
        message_id = _text(entry, "MESSAGE_ID", index)
        if message_id is not None and not MESSAGE_ID.fullmatch(message_id):
            raise ValueError("malformed message ID")
        if message_id not in EVENTS:
            report["excluded_record_indices"].append(index)
            continue
        time = _microseconds(entry, "__REALTIME_TIMESTAMP", index, required=True)
        if previous is not None and time < previous:
            raise ValueError("selected reception time decreased")
        previous = time
        row = {"record_time_us": time, "record_time_basis": CLOCKS["journal_reception_realtime"][2],
               "lifecycle_event": EVENTS[message_id], "reported_message_id": message_id,
               "event_basis": "FIXED_ID_AND_SOURCE_CALLSITE_PLUS_CALLER_PRODUCER_NOT_AUTHENTICATION",
               "operation_limit": ("ORDINARY_OR_SOFT_STARTUP_UNDISTINGUISHED_NOT_IDLE_OR_PHYSICAL_BOOT"
                                   if message_id.startswith("b07a") else
                                   "POWER_OFF_OR_REBOOT_FAILURE_UNDISTINGUISHED_NO_COMPLETION"),
               "source_record_index": index, "source_sha256": digest,
               "record_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "source_pin": SOURCE_PIN}
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
        row["uninterpreted_field_names_hex"] = "hex:" + json.dumps(unknown).encode().hex()
        report["uninterpreted_fields_total"] += len(unknown)
        writer.writerow(row)
        report["selected_entries"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("CSV output bound exceeded")
    if not report["selected_entries"]:
        raise ValueError("no selected lifecycle report")
    result = output.getvalue()
    if len(result.encode()) > OUTPUT_LIMIT or len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("output bound exceeded")
    report["output_sha256"] = hashlib.sha256(result.encode()).hexdigest()
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
        print("lifecycle conversion failed; no success claim (" + type(error).__name__ + ")", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
