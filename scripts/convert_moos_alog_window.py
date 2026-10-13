"""Select physical lines of a saved typed MOOS-IvP alog; no broker or simulator."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys

try:
    from scripts.convert_imc_sensors import convert_alog, convert_node_report
except ModuleNotFoundError:
    from convert_imc_sensors import convert_alog, convert_node_report

INPUT_LIMIT = 2 * 1024**3
LINE_COUNT_LIMIT = 5_000_000
LINE_LIMIT = 1024**2
HEADER_LIMIT = 64 * 1024
SELECTED_LIMIT = 16 * 1024**2
DERIVED_LINE_LIMIT = 100_000
CSV_LIMIT = 128 * 1024**2
REPORT_LIMIT = 64 * 1024**2
SCALAR = "moos-ivp-typed-alog-f56c"
NODE = "moos-ivp-node-report-f56c"
SPLIT = "moos-ivp-split-klog-f56c"
EXTRA = ("window_source_line", "source_byte_offset", "source_line_bytes")


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def report_text(report):
    text = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if len(text.encode("utf-8")) > REPORT_LIMIT:
        raise ValueError("window report exceeds bound")
    return text


def convert_path(path, *, start_line, stop_line, source_format, variable, publisher,
                 capture_time_us, unit=None, declared_units=None, unit_basis=None,
                 record_clock="caller-capture", declared_start=None):
    if (type(start_line) is not int or type(stop_line) is not int
            or not 0 <= start_line < stop_line <= LINE_COUNT_LIMIT):
        raise ValueError("explicit nonempty physical-line interval required")
    if source_format not in (SCALAR, NODE, SPLIT):
        raise ValueError("selected fixed typed MOOS-IvP source required")
    if (declared_start is not None) != (source_format == SPLIT):
        raise ValueError("split klog alone requires explicit summary log start")
    if source_format == SCALAR:
        if unit is None or declared_units is not None or unit_basis is not None or record_clock != "caller-capture":
            raise ValueError("scalar needs a qualified unit, not node-only options")
    elif unit is not None or (not declared_units and unit_basis != "producer-documentation-stated"):
        raise ValueError("node needs an explicit unit declaration/basis, not scalar unit")
    path = Path(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= INPUT_LIMIT:
        raise ValueError("bounded regular original alog required; symlinks are not followed")
    digest = hashlib.sha256()
    selected = bytearray()
    lines = []
    count = total = header_bytes = header_lines = 0
    begun = False
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if signature(before) != signature(metadata):
            raise ValueError("source changed before read")
        while True:
            raw = source.readline(LINE_LIMIT + 3)
            if not raw:
                break
            content = raw[:-1] if raw.endswith(b"\n") else raw
            if raw.endswith(b"\n") and content.endswith(b"\r"):
                content = content[:-1]
            if len(content) > LINE_LIMIT:
                raise ValueError("original alog line exceeds bound")
            text = content.decode("utf-8-sig" if count == 0 else "utf-8")
            if any(char in text for char in "\0\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029\ufeff"):
                raise ValueError("unsupported physical line representation")
            if count >= LINE_COUNT_LIMIT or total + len(raw) > INPUT_LIMIT:
                raise ValueError("original input exceeds line/byte bound")
            if source_format == SPLIT and text.startswith("%"):
                raise ValueError("split klog cannot contain logger headers")
            prefix = source_format != SPLIT and not begun and (not text.strip() or text.startswith("%"))
            if prefix:
                header_bytes += len(raw)
                header_lines += 1
                if header_bytes > HEADER_LIMIT:
                    raise ValueError("original header exceeds bound")
            else:
                if text == "%% DATATYPE MARKING ON" or text.startswith("%% LOGSTART"):
                    raise ValueError("late logger header declaration")
                begun = True
            if prefix or start_line <= count < stop_line:
                if len(selected) + len(raw) > SELECTED_LIMIT or len(lines) >= DERIVED_LINE_LIMIT:
                    raise ValueError("selected input or line-map exceeds bound")
                selected.extend(raw)
                lines.append(dict(source_line=count + 1, source_byte_offset=total, source_line_bytes=len(raw)))
            digest.update(raw)
            total += len(raw)
            count += 1
        if (signature(os.fstat(source.fileno())) != signature(before)
                or signature(path.lstat()) != signature(before) or total != before.st_size):
            raise ValueError("source changed during read")
    if stop_line > count:
        raise ValueError("line interval exceeds EOF")
    data = bytes(selected)
    if source_format == SCALAR:
        text, report = convert_alog(data, variable, publisher, unit, capture_time_us, timestamp_decimals=5)
    else:
        text, report = convert_node_report(data, variable, publisher, capture_time_us,
                                          declared_units=declared_units, unit_basis=unit_basis,
                                          record_clock=record_clock, timestamp_decimals=5,
                                          declared_start=declared_start)
    original_sha = digest.hexdigest()
    output = io.StringIO(newline="")
    previous_limit = csv.field_size_limit(max(csv.field_size_limit(), 4 * LINE_LIMIT))
    try:
        reader = csv.DictReader(io.StringIO(text))
        writer = csv.DictWriter(output, [*reader.fieldnames, *EXTRA], lineterminator="\n")
        writer.writeheader()
        output_rows = 0
        for row in reader:
            local = int(row["source_line"])
            if not 1 <= local <= len(lines):
                raise ValueError("decoder line outside original-line map")
            entry = lines[local - 1]
            row.update(entry)
            row["window_source_line"] = local
            row["source_sha256"] = original_sha
            writer.writerow(row)
            output_rows += 1
            if output.tell() > CSV_LIMIT:
                raise ValueError("window CSV exceeds bound")
    finally:
        csv.field_size_limit(previous_limit)
    text = output.getvalue()
    encoded = text.encode("utf-8")
    if len(encoded) > CSV_LIMIT or output_rows != report["selected_records"]:
        raise ValueError("window CSV byte/row accounting mismatch")
    report.update(derived_source_sha256=report["source_sha256"], source_sha256=original_sha,
                  derived_source_hex="hex:" + data.hex(), output_sha256=hashlib.sha256(encoded).hexdigest(),
                  output_bytes=len(encoded), output_rows=output_rows,
                  source_retention="original alog required externally; exact derived subset/header in this paired report",
                  semantic_count_scope="DERIVED_WINDOW_ONLY_NOT_WHOLE_ORIGINAL_RECORDS",
                  line_selection=dict(source_bytes=total, source_lines=count, start_line=start_line,
                      stop_line_exclusive=stop_line, selected_lines=stop_line-start_line,
                      excluded_lines=count-(stop_line-start_line), header_lines=header_lines,
                      header_bytes=header_bytes, derived_lines=lines,
                      outside_selection="NOT_DECODED_OR_VALIDATED",
                      framing_scope="whole source UTF8/physical-line bounds and header placement only",
                      bounds=dict(input_bytes=INPUT_LIMIT, physical_lines=LINE_COUNT_LIMIT,
                          line_bytes=LINE_LIMIT, header_bytes=HEADER_LIMIT, selected_bytes=SELECTED_LIMIT,
                          derived_lines=DERIVED_LINE_LIMIT, csv_bytes=CSV_LIMIT, report_bytes=REPORT_LIMIT)))
    report_text(report)
    return text, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--start-line", required=True, type=int)
    parser.add_argument("--stop-line", required=True, type=int)
    parser.add_argument("--source-format", required=True, choices=(SCALAR, NODE, SPLIT))
    parser.add_argument("--declared-logstart")
    parser.add_argument("--variable", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--capture-time-us", required=True, type=int)
    parser.add_argument("--unit")
    parser.add_argument("--node-unit", action="append", default=[])
    parser.add_argument("--unit-basis")
    parser.add_argument("--record-clock", default="caller-capture")
    args = parser.parse_args()
    try:
        declared = {}
        for item in args.node_unit:
            key, unit = item.split("=", 1)
            if key in declared:
                raise ValueError("duplicate node unit declaration")
            declared[key] = unit
        if args.output_dir.exists():
            raise ValueError("output directory exists; no overwrite")
        text, report = convert_path(args.input, start_line=args.start_line, stop_line=args.stop_line,
            source_format=args.source_format, variable=args.variable, publisher=args.publisher,
            capture_time_us=args.capture_time_us, unit=args.unit, declared_units=declared or None,
            unit_basis=args.unit_basis, record_clock=args.record_clock,
            declared_start=args.declared_logstart)
        serialized = report_text(report)
        args.output_dir.mkdir()
        (args.output_dir / "observations.csv").write_text(text, encoding="utf-8")
        (args.output_dir / "report.json").write_text(serialized, encoding="utf-8")
    except (ValueError, OSError) as exc:
        print(f"refused: {exc}; if output directory exists it may be partial", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
