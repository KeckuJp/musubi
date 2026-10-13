"""Saved Moby 27.3.1 State plus exact reported lifecycle instants; no daemon IO."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re

try:
    from scripts import convert_ardupilot_battery_csv as base
    from scripts.convert_gpx_recorded import utc_microseconds
except ModuleNotFoundError:
    import convert_ardupilot_battery_csv as base
    from convert_gpx_recorded import utc_microseconds

INPUT_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 1024 * 1024
TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})")
EXTRA = [f"container_{event}_{field}" for event in ("started", "finished")
         for field in ("raw_hex", "epoch_s", "subsecond_ns", "disposition")]
EXTRA += ["source_document_sha256", "source_record_index"]


def lifecycle(state, key):
    if key not in state:
        return ["", "", "", "NOT_REPORTED"]
    value = state[key]
    if not isinstance(value, str):
        raise ValueError("reported lifecycle time must be a string")
    raw = "hex:" + value.encode("utf-8").hex()
    if value == "0001-01-01T00:00:00Z":
        return [raw, "", "", "ZERO_TIME"]
    match = TIME.fullmatch(value)
    if not match or match[3] == "-00:00":
        raise ValueError("exact lifecycle RFC3339Nano with explicit selected offset required")
    # Reuse calendar/offset validation, but never send nanoseconds through a
    # microsecond conversion. The remainder stays an exact, separate integer.
    whole_us = utc_microseconds(match[1] + match[3])
    return [raw, whole_us // 1000000, int((match[2] or "0").ljust(9, "0")), "REPORTED"]


def convert(text):
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > INPUT_LIMIT:
        raise ValueError("bounded saved Docker JSONL required")
    existing = base.convert(text, base.DOCKER_STATE_FORMAT)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    counts = {"REPORTED": 0, "ZERO_TIME": 0, "NOT_REPORTED": 0}
    # The existing snapshot bound permits a retained hex field above Python's
    # CSV default. This is only internal CSV transport, not a larger input limit.
    previous_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(max(previous_limit, 2 * INPUT_LIMIT))
        rows = csv.reader(io.StringIO(existing, newline=""))
        writer.writerow(next(rows) + EXTRA)
        count = 0
        for index, row in enumerate(rows):
            record = json.loads(bytes.fromhex(row[1][4:]), object_pairs_hook=base.unique_object)
            state = record["inspect"]["State"]
            started, finished = lifecycle(state, "StartedAt"), lifecycle(state, "FinishedAt")
            for value in (started, finished):
                counts[value[3]] += 1
            writer.writerow(row + started + finished + ["sha256:" + digest, index])
            count += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("lifecycle CSV output bound exceeded")
    finally:
        csv.field_size_limit(previous_limit)
    content = output.getvalue()
    if len(content.encode("utf-8")) > OUTPUT_LIMIT:
        raise ValueError("lifecycle CSV output bound exceeded")
    report = {"source_sha256": digest, "output_sha256": hashlib.sha256(content.encode()).hexdigest(),
              "rows": count, "time_dispositions": counts,
              "source_format": base.DOCKER_STATE_FORMAT,
              "time_basis": "REPORTED_WALL_INSTANT_NOT_TRUSTED_CLOCK_OR_SAME_RUN_DURATION",
              "main_clock_basis": "Unknown"}
    if len(json.dumps(report, indent=2).encode()) > REPORT_LIMIT:
        raise ValueError("lifecycle report output bound exceeded")
    return content, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError("new distinct outputs required")
        with args.input.open("rb") as source:
            raw = source.read(INPUT_LIMIT + 1)
        if len(raw) > INPUT_LIMIT:
            raise ValueError("saved Docker input bound exceeded")
        content, report = convert(raw.decode("utf-8"))
        with args.output.open("x", encoding="utf-8", newline="") as destination:
            destination.write(content)
        with args.report.open("x", encoding="utf-8") as destination:
            destination.write(json.dumps(report, indent=2))
    except (OSError, ValueError, OverflowError, csv.Error) as error:
        parser.exit(2, f"Docker lifecycle conversion refused: {error}\n")


if __name__ == "__main__":
    main()
