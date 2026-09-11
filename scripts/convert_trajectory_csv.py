#!/usr/bin/env python3
"""Convert an explicit seconds field to exact integer microseconds; retain every source column.

No inference, pose generation, SI re-scaling, network, firmware identity or clock-quality claims.
This stage makes existing telemetry CSV observation logic reusable for a new exporter.
"""
import argparse
import csv
from decimal import Decimal, InvalidOperation, localcontext
import io
from pathlib import Path


def convert(text, time_column, *, allow_equal_time=False):
    rows = list(csv.reader(io.StringIO(text), strict=True))
    if len(rows) < 2 or time_column not in rows[0] or len(set(rows[0])) != len(rows[0]):
        raise ValueError("missing time/data or duplicate column")
    if any(any(c in name for c in '\r\n,"') for name in rows[0]):
        raise ValueError("quoted header requires a different reader")
    if any(name.strip().split("(", 1)[0].strip() == "record_time" for name in rows[0]):
        raise ValueError("generated time column would overwrite source")
    time_index = rows[0].index(time_column)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time (us)"] + rows[0])
    previous = None
    for row in rows[1:]:
        if len(row) != len(rows[0]): raise ValueError("ragged row")
        try:
            seconds = Decimal(row[time_index])
            with localcontext() as context:
                context.prec = max(28, len(seconds.as_tuple().digits) + 7)
                us = seconds * 1000000
        except InvalidOperation as e: raise ValueError("invalid seconds") from e
        if not us.is_finite() or us != us.to_integral_value() or us < 0 or us > 2**63 - 1:
            raise ValueError("time must be finite/nonnegative and exactly representable as i64 microseconds")
        value = int(us)
        if previous is not None and (value < previous or (value == previous and not allow_equal_time)):
            raise ValueError("time must increase (equal time requires explicit opt-in)")
        # The current target CSV reader is intentionally unquoted; unsupported strings fail here.
        if any(any(c in value for c in '\r\n,"') for value in row): raise ValueError("quoted cell requires a different reader")
        writer.writerow([str(value)] + row)
        previous = value
    return output.getvalue()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--time-column", required=True)
    a = p.parse_args()
    result = convert(a.input.read_text(), a.time_column)
    with a.output.open("x") as f: f.write(result)
