#!/usr/bin/env python3
"""Finite saved RMF FleetState arrays; reuse the adopted RobotState resolver."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

try:
    from . import convert_ardupilot_battery_csv as existing
except ImportError:
    import convert_ardupilot_battery_csv as existing

PIN = "6990fc7418c412234b4ff64f01560d8bf3047f46"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 512 * 1024
MAX_ROBOTS = 4096
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
FIELDS = list(existing.RMF_FIELDS) + list(existing.RMF_LOCATION_FIELDS)
ARRAY_FIELDS = ["fleet_name_hex", "fleet_robot_index", "fleet_robot_count"]


def convert(text):
    if not text or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("empty or oversized RMF fleet export")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_record_hex"] + ARRAY_FIELDS + FIELDS)
    report = dict(schema="rmf-fleet-array/v1", schema_commit=PIN, source_records=0,
                  robot_states=0, path_points=0, output_rows=0,
                  source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                  unknown_fields="RETAINED_IN_EXACT_SOURCE_LINE_ON_FLEET_SUMMARY_NOT_INTERPRETED",
                  clock_basis="UNKNOWN_CALLER_CAPTURE_NOT_WAYPOINT_TIME")
    previous = None

    def emit(time, raw, association, values):
        writer.writerow([time, raw] + association + ["" if v is None else v for v in values])
        # Every non-ASCII source string is hex encoded; CSV byte count equals character count.
        if output.tell() > MAX_OUTPUT_BYTES:
            raise ValueError("RMF fleet output bound exceeded")
        report["output_rows"] += 1

    try:
        for index, line in enumerate(text.splitlines()):
            if not line.strip() or len(line.encode("utf-8")) > MAX_RECORD_BYTES:
                raise ValueError("empty or oversized RMF fleet record")
            record = json.loads(line, object_pairs_hook=existing.unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
            if record["schema_commit"] != PIN:
                raise ValueError("unqualified RMF FleetState schema")
            # Validate paired declaration even when there are no robots to resolve.
            if "location_unit" in record or "yaw_unit" in record:
                if record.get("location_unit") != "m" or record.get("yaw_unit") != "rad":
                    raise ValueError("RMF location requires metre and radian declarations")
            time = existing.json_time(record, existing.RMF_FORMAT)
            if previous is not None and time < previous:
                raise ValueError("decreasing RMF capture timestamp")
            previous = time
            fleet = record["fleet_state"]
            name, robots = fleet["name"], fleet["robots"]
            if not isinstance(name, str) or not isinstance(robots, list):
                raise ValueError("invalid RMF fleet name or robots array")
            if len(robots) > MAX_ROBOTS:
                raise ValueError("RMF fleet robot count bound exceeded")
            name_hex = "hex:" + name.encode("utf-8").hex()
            digest = "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
            summary = dict(rmf_source_record_index=index, rmf_snapshot_sha256=digest,
                           rmf_record_kind="FLEET_SUMMARY")
            emit(time, "hex:" + line.encode("utf-8").hex(), [name_hex, "", len(robots)],
                 [summary.get(field) for field in FIELDS])
            for robot_index, state in enumerate(robots):
                # Only the envelope changes. All mode/units/pose/path/missing semantics remain
                # owned by the adopted resolver, including undeclared coordinates staying raw.
                envelope = {key: record[key] for key in ("schema_commit", "location_unit", "yaw_unit")
                            if key in record}
                envelope["state"] = state
                for values in existing.rmf_quantities(envelope, index, digest):
                    emit(time, "", [name_hex, robot_index, len(robots)], values)
                    kind = values[FIELDS.index("rmf_record_kind")]
                    report["path_points" if kind == "PATH_POINT" else "robot_states"] += 1
            report["source_records"] += 1
    except (KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("incompatible RMF fleet export") from exc
    report["output_sha256"] = hashlib.sha256(output.getvalue().encode("utf-8")).hexdigest()
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError("output paths must be distinct and new")
        with args.input.open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("RMF fleet input bound exceeded")
        output, report = convert(data.decode("utf-8"))
        with args.output.open("x", encoding="utf-8", newline="") as target:
            target.write(output)
        with args.report.open("x", encoding="utf-8") as target:
            json.dump(report, target, indent=2)
        return 0
    except (ValueError, OSError) as exc:
        print(f"RMF fleet conversion rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
