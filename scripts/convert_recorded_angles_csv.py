#!/usr/bin/env python3
"""Two fixed decoded angle schemas, passive and without frame/rotation inference."""
import argparse
import csv
import io
import math
from pathlib import Path
import re

LIMIT = 16 * 1024 * 1024
SCHEMAS = {
    "inav-7.1.2-raw-attitude": ("time (us)", ("attitude[0]", "attitude[1]", "attitude[2]"), math.pi / 1800),
    "plane-4.3.1-decoded-att": ("TimeUS", ("Roll", "Pitch", "Yaw"), math.pi / 180),
    "inav-decoder-1918-si-motion": ("time (us)",
        tuple(f"gyroADC[{i}] (rad/s)" for i in range(3)) +
        tuple(f"accSmooth[{i}] (m/s/s)" for i in range(3)), 1.0),
    # Betaflight 4.2.0 through the pinned blackbox-tools f832acf9 asked for SI rotation and
    # acceleration. The header shape matches the INAV row above, but that row is qualified for
    # INAV's own decoder, so this is its own row rather than a borrowed qualification.
    "betaflight-4.2.0-si-motion": ("time (us)",
        tuple(f"gyroADC[{i}] (rad/s)" for i in range(3)) +
        tuple(f"accSmooth[{i}] (m/s/s)" for i in range(3)), 1.0),
}
# Both rows are the same decoder family emitting %.2f SI, so they share the value rule and the
# renamed output; only their firmware/decoder qualification differs.
SI_MOTION_SCHEMAS = ("inav-decoder-1918-si-motion", "betaflight-4.2.0-si-motion")
OUTPUT = ["record_time_us", "reported_roll_rad", "reported_pitch_rad", "reported_yaw_rad",
          "angle_schema", "source_header_hex", "source_record_hex"]


def convert(text, schema):
    if schema not in SCHEMAS or not text or len(text.encode()) > LIMIT or '"' in text:
        raise ValueError("unsupported angle schema or CSV representation")
    time_field, angle_fields, scale = SCHEMAS[schema]
    output_fields = OUTPUT
    if schema in SI_MOTION_SCHEMAS:
        output_fields = ["record_time_us"] + [f"reported_angular_velocity_{axis}_rad_s" for axis in "xyz"] + [
            f"reported_acceleration_{axis}_m_s2" for axis in "xyz"] + [
            "motion_schema", "source_header_hex", "source_record_hex"]
    lines = text.splitlines()
    rows = csv.reader(lines, strict=True)
    header = [field.strip() for field in next(rows)]
    if (not header or len(header) > 256 or len(header) != len(set(header))
            or any(not field or len(field) > 128 for field in header)
            or not {time_field, *angle_fields}.issubset(header)):
        raise ValueError("angle header mismatch")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, output_fields, lineterminator="\n")
    writer.writeheader()
    previous, count = None, 0
    for index, row in enumerate(rows, 1):
        if len(row) != len(header):
            raise ValueError("ragged angle row")
        values = dict(zip(header, row))
        time = values[time_field].strip()
        if not re.fullmatch(r"[0-9]{1,19}", time) or int(time) > 2**63 - 1:
            raise ValueError("invalid boot microseconds")
        time = int(time)
        if previous is not None and time < previous:
            raise ValueError("decreasing boot time")
        previous = time
        angles = []
        for field in angle_fields:
            token = values[field].strip()
            if schema in SI_MOTION_SCHEMAS and not re.fullmatch(r"-?[0-9]{1,20}\.[0-9]{2}", token):
                raise ValueError("fixed decoder SI output requires two decimal places")
            if schema == "inav-7.1.2-raw-attitude" and (not re.fullmatch(r"[+-]?[0-9]{1,6}", token)
                    or not -32768 <= int(token) <= 32767):
                raise ValueError("INAV raw angle requires signed16 decidegrees")
            angle = float(token) * scale
            if not math.isfinite(angle):
                raise ValueError("nonfinite reported angle")
            angles.append(angle)
        writer.writerow(dict(zip(output_fields, [time, *angles, schema, "hex:" + lines[0].encode().hex(),
                                         "hex:" + lines[index].encode().hex()])))
        count += 1
        if output.tell() > LIMIT:
            raise ValueError("angle output exceeds bound")
    if not count:
        raise ValueError("no angle records")
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--schema", choices=SCHEMAS, required=True)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output = convert(args.input.read_text(encoding="utf-8"), args.schema)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(output)
    except (OSError, ValueError, csv.Error, StopIteration):
        parser.exit(2, "Angle conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
