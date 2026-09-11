#!/usr/bin/env python3
"""Offline TUM-style pose text to existing telemetry CSV; no frame/clock inference."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_trajectory_csv import convert as seconds_to_microseconds

BASE = ["timestamp_unix_s", "tx (m)", "ty (m)", "tz (m)", "qx", "qy", "qz", "qw"]


def convert(text, extra_columns=(), *, allow_equal_time=False):
    """Require explicit names for extensions; retain comments in the accounting report."""
    names = list(extra_columns)
    reserved = {name.split(" (")[0] for name in BASE} | {"record_time"}
    if len(set(names)) != len(names) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        or name in reserved for name in names
    ):
        raise ValueError("extension names must be unique nonreserved identifiers")
    report = {"data_rows": 0, "blank_lines": 0, "comment_lines": 0, "comments": []}
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(BASE + names)
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            report["blank_lines"] += 1
            continue
        if line.lstrip().startswith("#"):
            report["comment_lines"] += 1
            report["comments"].append(line)
            continue
        cells = line.split()
        if len(cells) != len(BASE) + len(names):
            raise ValueError(f"line {number}: wrong field count; declare extensions explicitly")
        try:
            finite = all(math.isfinite(float(cell)) for cell in cells)
        except ValueError as exc:
            raise ValueError(f"line {number}: nonnumeric field") from exc
        if not finite:
            raise ValueError(f"line {number}: nonfinite field")
        writer.writerow(cells)
        report["data_rows"] += 1
    if not report["data_rows"]:
        raise ValueError("no pose records")
    converted = seconds_to_microseconds(output.getvalue(), "timestamp_unix_s", allow_equal_time=allow_equal_time)
    times = [row[0] for row in list(csv.reader(io.StringIO(converted)))[1:]]
    report["equal_time_pairs"] = sum(a == b for a, b in zip(times, times[1:]))
    return converted, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--extra-column", action="append", default=[])
    parser.add_argument("--allow-equal-time", action="store_true")
    args = parser.parse_args()
    if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
        parser.error("output and report must be separate new files")
    converted, report = convert(args.input.read_text(), args.extra_column, allow_equal_time=args.allow_equal_time)
    with args.output.open("x") as stream:
        stream.write(converted)
    with args.report.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
