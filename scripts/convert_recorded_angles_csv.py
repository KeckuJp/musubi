#!/usr/bin/env python3
"""Two fixed decoded angle schemas, passive and without frame/rotation inference."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat

LIMIT = 16 * 1024 * 1024
MAX_INPUT_BYTES = 2 * 1024**3
MAX_RECORDS = 5_000_000
MAX_RECORD_BYTES = 64 * 1024
SCHEMAS = {
    "inav-7.1.2-raw-attitude": ("time (us)", ("attitude[0]", "attitude[1]", "attitude[2]"), math.pi / 1800),
    "plane-4.3.1-decoded-att": ("TimeUS", ("Roll", "Pitch", "Yaw"), math.pi / 180),
    "rover-4.5.5-decoded-att": ("TimeUS", ("Roll", "Pitch", "Yaw"), math.pi / 180),
    "plane-4.5.5-decoded-att": ("TimeUS", ("Roll", "Pitch", "Yaw"), math.pi / 180),
    "copter-4.5.5-decoded-att": ("TimeUS", ("Roll", "Pitch", "Yaw"), math.pi / 180),
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
WINDOW_SCHEMAS = (*SI_MOTION_SCHEMAS, "plane-4.3.1-decoded-att", "rover-4.5.5-decoded-att",
                  "plane-4.5.5-decoded-att", "copter-4.5.5-decoded-att")
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


def convert_path(path, schema, *, start_record, stop_record):
    """Select a bounded native row interval, reusing the adopted conversion.

    Scan framing and boot time throughout the original file, but interpret motion
    values only inside the selection. The digest identifies bytes actually read;
    it is neither firmware detection nor authenticated source identity.
    """
    if schema not in WINDOW_SCHEMAS:
        raise ValueError("record selection requires a fixed window motion schema")
    legacy_inav = schema == "inav-decoder-1918-si-motion"
    label = "INAV" if legacy_inav else "recorded motion"
    time_field, fields, _ = SCHEMAS[schema]
    result, report = convert_selected_csv_path(
        path, time_field=time_field, required_fields=fields,
        transform=lambda text: convert(text, schema), label=label,
        start_record=start_record, stop_record=stop_record)
    if not legacy_inav:
        report.update(schema="recorded-motion-csv-record-selection/v1", source_schema=schema)
    return result, report


def convert_selected_csv_path(path, *, time_field, required_fields, transform, label,
                              start_record, stop_record, validate_time=True):
    """Shared saved-decoded CSV selection for existing motion and motor meanings.

    The caller supplies the already-qualified semantic converter, not another parser.
    Entire-source framing/time and selected semantic interpretation remain separate.
    """
    if type(validate_time) is not bool:
        raise ValueError("time-validation selector must be bool")
    if (type(start_record) is not int or type(stop_record) is not int
            or not 0 <= start_record < stop_record <= MAX_RECORDS):
        raise ValueError("invalid record interval")
    path = Path(path).resolve()
    metadata = path.stat()
    if (not stat.S_ISREG(metadata.st_mode)
            or path.parts[1:2] in (("sys",), ("proc",), ("dev",))):
        raise ValueError("saved regular input required")
    if metadata.st_size > MAX_INPUT_BYTES:
        raise ValueError(f"{label} input byte bound exceeded")
    digest = hashlib.sha256()
    selected = []
    total_bytes = selected_bytes = count = 0
    previous = None

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if signature(before) != signature(metadata):
            raise ValueError("source changed before read")
        header = None
        while True:
            raw = stream.readline(MAX_RECORD_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError(f"{label} individual record byte bound exceeded")
            total_bytes += len(raw)
            if total_bytes > MAX_INPUT_BYTES:
                raise ValueError(f"{label} input byte bound exceeded")
            digest.update(raw)
            native = raw[:-1].removesuffix(b"\r") if raw.endswith(b"\n") else raw
            text = native.decode("utf-8")
            if (not text or '"' in text or "\0" in text
                    or any(c in text for c in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")):
                raise ValueError("unsupported native CSV record representation")
            cells = next(csv.reader([text], strict=True))
            if header is None:
                header = [field.strip() for field in cells]
                if (not header or len(header) > 256 or len(header) != len(set(header))
                        or any(not field or len(field) > 128 for field in header)
                        or not {time_field, *required_fields}.issubset(header)):
                    raise ValueError("angle header mismatch")
                time_index = header.index(time_field)
                selected.append(text + "\n")
                selected_bytes = len(selected[0].encode("utf-8"))
                continue
            if len(cells) != len(header):
                raise ValueError("ragged angle row")
            if validate_time:
                time_token = cells[time_index].strip()
                if not re.fullmatch(r"[0-9]{1,19}", time_token) or int(time_token) > 2**63 - 1:
                    raise ValueError("invalid boot microseconds")
                time = int(time_token)
                if previous is not None and time < previous:
                    raise ValueError("decreasing boot time; split input sessions")
                previous = time
            if start_record <= count < stop_record:
                retained = text + "\n"
                selected_bytes += len(retained.encode("utf-8"))
                if selected_bytes > LIMIT:
                    raise ValueError(f"{label} selected source byte bound exceeded")
                selected.append(retained)
            count += 1
            if count > MAX_RECORDS:
                raise ValueError(f"{label} source record count bound exceeded")
        after = os.fstat(stream.fileno())
        if signature(after) != signature(before) or total_bytes != before.st_size:
            raise ValueError("source changed during read")
    if stop_record > count:
        raise ValueError("record interval exceeds source records")
    converted = transform("".join(selected))
    source_sha = digest.hexdigest()
    reader = csv.DictReader(io.StringIO(converted))
    output = io.StringIO(newline="")
    names = [*reader.fieldnames, "source_file_sha256", "source_record_index"]
    writer = csv.DictWriter(output, names,
                            lineterminator="\n")
    writer.writeheader()
    output_bytes = len(output.getvalue().encode("utf-8"))
    for index, row in enumerate(reader, start_record):
        row.update(source_file_sha256="sha256:" + source_sha, source_record_index=index)
        record_output = io.StringIO(newline="")
        csv.DictWriter(record_output, names, lineterminator="\n").writerow(row)
        encoded = record_output.getvalue()
        output_bytes += len(encoded.encode("utf-8"))
        if output_bytes > LIMIT:
            raise ValueError(f"{label} selected output byte bound exceeded")
        output.write(encoded)
    result = output.getvalue()
    report = {
        "schema": "inav-csv-record-selection/v1",
        "source_sha256": source_sha, "source_bytes": total_bytes, "source_records": count,
        "selected_records": stop_record - start_record,
        "excluded_records": count - (stop_record - start_record),
        "selection": {"start_record": start_record, "stop_record_exclusive": stop_record},
        "excluded_values": "NOT_DECODED_OR_VALIDATED", "clock_basis": "BootRelative",
        "output_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        "output_bytes": output_bytes,
        "bounds": {"max_input_bytes": MAX_INPUT_BYTES, "max_records": MAX_RECORDS,
                   "max_record_bytes": MAX_RECORD_BYTES, "max_selected_source_bytes": LIMIT,
                   "max_output_bytes": LIMIT},
    }
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--schema", choices=SCHEMAS, required=True)
    parser.add_argument("--start-record", type=int)
    parser.add_argument("--stop-record", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        window = any(value is not None for value in (args.start_record, args.stop_record, args.report))
        if window:
            if any(value is None for value in (args.start_record, args.stop_record, args.report)):
                raise ValueError("record interval and report must be declared together")
            if (args.output.resolve() == args.report.resolve()
                    or any(p.exists() or p.is_symlink() for p in (args.output, args.report))):
                raise ValueError("distinct new output and report paths required")
            output, report = convert_path(args.input, args.schema,
                                          start_record=args.start_record, stop_record=args.stop_record)
        else:
            if args.input.stat().st_size > LIMIT:
                raise ValueError("input exceeds bound")
            output = convert(args.input.read_text(encoding="utf-8"), args.schema)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(output)
        if window:
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")
    except (OSError, ValueError, csv.Error, StopIteration):
        parser.exit(2, "Angle conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
