"""Passive per-message CSV export from a saved ArduPilot binary DataFlash log.

This is a thin wrapper around the already pinned pymavlink 2.4.49 DataFlash reader:
it decodes a user-provided `.bin` that is already on disk, and writes one CSV per
selected message type using that message's own FMT labels. No device is contacted,
no download is performed, and nothing is re-scaled.

Scaling is applied exactly once, by the decoder. In pymavlink 2.4.49 the multiplier
comes from the FMT format character (`L` gives 1e-7, `c`/`C`/`e`/`E` give 1e-2, every
other character is raw) and `FMTU`/`MULT` records only relabel units, so the values
written here are the decoder's values passed straight through. `TimeUS` is a `Q` and
therefore stays the native unscaled microsecond count.

A selected type with no records produces no file and is reported absent; it never
becomes a zero row.

**Counting returned messages is not accounting for the input.** `DFReader._parse_next`
resyncs past bytes it cannot frame and returns None on a truncated tail, so a balance
between exported and unselected messages is a tautology over what the decoder handed
back. Every message is therefore also attributed to a byte span using its own FMT
length, and any span the decoder stepped over -- or any tail it left -- is reported
explicitly as an undecoded span. A file with undecoded spans never returns a clean
complete export.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import stat
import tempfile
from importlib.metadata import version
from pathlib import Path

PYMAVLINK = "2.4.49"
MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 512 * 1024 * 1024
MAX_MESSAGES = 4_000_000
MAX_RETAINED_OFFSETS = 65536
SUPPORTED = ("MODE", "ARM", "VER", "BAT", "GPS")
CSV_SUPPORTED = (*SUPPORTED, "ATT", "PARM", "BCL", "BCL2", "MSG")
SUB457_LAYOUTS = {
    "MSG": ("QZ", "TimeUS,Message"),
    "CTUN": ("Qfffffffccfhh", "TimeUS,ThI,ABst,ThO,ThH,DAlt,Alt,BAlt,DSAlt,SAlt,TAlt,DCRt,CRt"),
    "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
    "BAT": ("QBfffffcfBBB", "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH"),
    "BCL": ("QBfHHHHHHHHHHHH", "TimeUS,Instance,Volt,V1,V2,V3,V4,V5,V6,V7,V8,V9,V10,V11,V12"),
    "BCL2": ("QBHH", "TimeUS,Instance,V13,V14"),
    "IMU": ("QBffffffIIfBBHH", "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ,EG,EA,T,GH,AH,GHz,AHz"),
}
SCALING_BASIS = "PYMAVLINK_FMT_FORMAT_CHARACTER_APPLIED_ONCE_BY_THE_DECODER"
DIGEST_BASIS = "SHA256_OF_THE_BYTES_THE_DECODER_MAPPED_VERIFIED_EQUAL_BEFORE_DECODING"
COMPLETE = "EVERY_INPUT_BYTE_DECODED_AS_A_DECLARED_RECORD"
INCOMPLETE = "UNDECODED_SPANS_PRESENT_NOT_A_COMPLETE_EXPORT"


class UndecodedSpans(ValueError):
    """Raised instead of returning a partial export; carries the explicit span report."""

    def __init__(self, report):
        super().__init__("saved DataFlash has spans the decoder did not frame")
        self.report = report


def safe(value):
    """The downstream CSV contract is unquoted, so refuse a value that would quote."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "strict")
    if isinstance(value, str):
        if any(c in value for c in ',"\r\n') or any(ord(c) < 32 for c in value):
            raise ValueError("record field needs quoting; refusing to write ambiguous CSV")
        return value
    if isinstance(value, bool):
        raise ValueError("unexpected boolean in a DataFlash field")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    raise ValueError("unsupported DataFlash field type")


def write(path, text):
    """Write exactly the newlines we composed, on any platform."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def bounded_read(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("saved DataFlash input exceeds 64MiB")
    return raw


def schema_of(fmt):
    """The declaration a record was parsed under: labels alone do not fix meaning."""
    return {
        "type_id": fmt.type, "length": fmt.len, "format": fmt.format,
        "columns": list(fmt.columns),
        "multipliers": {name: mult for name, mult in zip(fmt.columns, fmt.msg_mults)},
        "units": dict(zip(fmt.columns, fmt.units)) if fmt.units else None,
    }


def export_records(path, types, *, output_format="csv", include_source_indices=False):
    """Decode a saved .bin and return {type: csv_text} plus a byte-level report."""
    if version("pymavlink") != PYMAVLINK:
        raise ValueError(f"pymavlink {PYMAVLINK} required")
    from pymavlink.DFReader import DFReader_binary
    selected = tuple(dict.fromkeys(types))
    if output_format not in ("csv", "jsonl"):
        raise ValueError("output format must be csv or jsonl")
    allowed = CSV_SUPPORTED if output_format == "csv" else SUPPORTED
    if type(include_source_indices) is not bool or (include_source_indices and output_format != "csv"):
        raise ValueError("source index report requires CSV")
    if not selected or any(name not in allowed for name in selected):
        raise ValueError(f"selected types must come from {', '.join(allowed)}")
    raw = bounded_read(path, MAX_INPUT)
    reader = DFReader_binary(str(path))
    try:
        if reader.data_len != len(raw) or bytes(reader.data_map[:reader.data_len]) != raw:
            raise ValueError("input changed between the bounded read and the decoder")
        exports, report = _decode(reader, raw, selected, output_format=output_format,
                                 include_source_indices=include_source_indices)
    finally:
        reader.data_map.close()
        reader.filehandle.close()
    if report["framing"] != COMPLETE:
        raise UndecodedSpans(report)
    return exports, report


def export_window(path, types, start_record, stop_record, *, allow_truncated_tail=False,
                  sub457_jsonl=False, sub_source_pin=None):
    """Explicit original-record interval; scan/map original without copying it into RAM.

    Original FMT context and full byte/count accounting are reused. The selected
    interval is not a complete original export; no earlier identity is carried forward.
    """
    if (type(allow_truncated_tail) is not bool or type(start_record) is not int or type(stop_record) is not int or
            not 0 <= start_record < stop_record <= 50_000_000 or stop_record-start_record > 100_000):
        raise ValueError("invalid bounded original record interval")
    if version("pymavlink") != PYMAVLINK:
        raise ValueError("pinned pymavlink required")
    if type(sub457_jsonl) is not bool:
        raise ValueError("explicit boolean Sub JSONL selector required")
    if sub_source_pin is not None and sub_source_pin not in (
            "30257f01185471ab4c1ac544e47d1b4437e44c98",
            "80e8c23a7a07d0653fc1ce6b6a55ee17889c77af"):
        raise ValueError("unsupported fixed Sub source pin")
    if sub457_jsonl and sub_source_pin not in (None, "30257f01185471ab4c1ac544e47d1b4437e44c98"):
        raise ValueError("conflicting Sub selectors")
    sub_jsonl = sub457_jsonl or sub_source_pin is not None
    selected = tuple(dict.fromkeys(types))
    allowed = SUB457_LAYOUTS if sub_jsonl else CSV_SUPPORTED
    if not selected or any(name not in allowed for name in selected):
        raise ValueError("unsupported selected CSV types")
    from saved_binary_snapshot import copy_source, MAX_BYTES
    from pymavlink.DFReader import DFReader_binary
    path = Path(path)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError("regular saved original required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or identity(before) != identity(initial)
                or not 0 < before.st_size <= MAX_BYTES):
            raise ValueError("window original identity or 2GiB limit")
        with tempfile.NamedTemporaryFile(mode="w+b") as frozen:
            digest, total = copy_source(source, frozen)
            if (total != before.st_size or identity(before) != identity(os.fstat(source.fileno()))
                    or identity(before) != identity(path.lstat())):
                raise ValueError("original changed during snapshot copy")
            reader = DFReader_binary(frozen.name)
            try:
                if digest != hashlib.sha256(reader.data_map).hexdigest():
                    raise ValueError("snapshot digest mismatch")
                exports, report = _decode(reader, reader.data_map, selected, include_source_indices=True,
                    record_window=(start_record, stop_record), output_format="jsonl" if sub_jsonl else "csv",
                    required_layouts=SUB457_LAYOUTS if sub_jsonl else None)
                if allow_truncated_tail and report["framing"] != COMPLETE:
                    spans = report["byte_coverage"]["undecoded_spans"]
                    tail = reader.data_map[spans[0]["offset"]:] if len(spans) == 1 else b""
                    declaration = reader.formats.get(tail[2]) if len(tail) >= 3 else None
                    declared = schema_of(declaration) if declaration is not None else None
                    if (len(spans) != 1 or spans[0]["disposition"] != "TRAILING_BYTES_THE_DECODER_DID_NOT_FRAME"
                            or not tail.startswith(b"\xa3\x95") or declared is None
                            or not 3 <= len(tail) < declared["length"]):
                        raise UndecodedSpans(report)
                    report["selected_prefix_disposition"] = "COMPLETE_SELECTED_RECORDS_WITH_KNOWN_TRUNCATED_ORIGINAL_TAIL"
                    report["truncated_tail_schema"] = declared
            finally:
                reader.data_map.close()
                reader.filehandle.close()
        if (identity(before) != identity(os.fstat(source.fileno()))
                or identity(before) != identity(path.lstat())):
            raise ValueError("original changed during window scan")
    report["snapshot_basis"] = "READ_SNAPSHOT_ONLY_NOT_LATER_PATH_IMMUTABILITY"
    if stop_record > report["total_messages"]:
        raise ValueError("window stop exceeds original decoded record count")
    if report["framing"] != COMPLETE and "selected_prefix_disposition" not in report:
        raise UndecodedSpans(report)
    return exports, report


def _decode(reader, raw, selected, *, output_format="csv", include_source_indices=False,
            record_window=None, required_layouts=None):
    rows, schemas, counts, offsets = {}, {}, {}, {}
    json_lines, json_bytes, untimed = [], 0, 0
    spans, cursor, decoded, total, dropped = [], 0, 0, 0, 0
    selected_refs = {}
    outside = {}
    while True:
        message = reader.recv_msg()
        if message is None:
            break
        total += 1
        if total > (50_000_000 if record_window else MAX_MESSAGES):
            raise ValueError("saved DataFlash message count exceeds bound")
        fmt = message.fmt
        end, name = reader.offset, fmt.name
        start = end - fmt.len
        if start < cursor or end > reader.data_len:
            raise ValueError("decoder offsets did not advance over a distinct span")
        if start > cursor:
            spans.append({"offset": cursor, "bytes": start - cursor,
                          "disposition": "UNDECODED_SPAN_SKIPPED_BY_THE_DECODER"})
        cursor, decoded = end, decoded + fmt.len
        schema = schema_of(fmt)
        if name not in schemas:
            schemas[name] = schema
        elif schemas[name] != schema:
            raise ValueError("a message type changed its FMT declaration within one file")
        counts[name] = counts.get(name, 0) + 1
        in_window = record_window is None or record_window[0] <= total-1 < record_window[1]
        if name in selected and in_window:
            if required_layouts is not None and (fmt.format, ",".join(fmt.columns)) != required_layouts[name]:
                raise ValueError("selected Sub record FMT differs from fixed source")
            if include_source_indices:
                selected_refs.setdefault(name, []).append(dict(index=total-1, offset=start, length=fmt.len))
            if output_format == "jsonl":
                if len(set(fmt.columns)) != len(fmt.columns) or "mavpackettype" in fmt.columns:
                    raise ValueError("selected JSONL columns must be unique and not reserved")
                fields = message.to_dict()
                fields.pop("mavpackettype")
                formats = dict(zip(fmt.columns, fmt.format))
                if "TimeMS" in fields or ("TimeUS" in fields and
                        (formats.get("TimeUS") != "Q" or type(fields["TimeUS"]) is not int)):
                    raise ValueError("selected JSONL clock requires integer Q TimeUS or no counter")
                if name == "GPS" and any(formats.get(key) != code for key, code in
                        (("TimeUS", "Q"), ("Lat", "L"), ("Lng", "L"), ("Alt", "e"))):
                    raise ValueError("selected GPS JSONL requires qualified Q/L/L/e fields")
                meta = dict(type=name)
                for key, value in fields.items():
                    if type(value) is float and not math.isfinite(value):
                        fields[key] = "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
                        meta.setdefault("nonfinite_fields", []).append(key)
                    elif type(value) not in (str, int, float):
                        raise ValueError("selected JSONL requires decoded scalar fields")
                line = json.dumps(dict(meta=meta, data=fields, source_record_index=total - 1,
                    source_offset=start, source_length=fmt.len, source_record_hex=raw[start:end].hex()),
                    ensure_ascii=False, allow_nan=False)
                json_bytes += len(line.encode("utf-8")) + 1
                if json_bytes > MAX_OUTPUT:
                    raise ValueError("exported JSONL exceeds 512MiB")
                json_lines.append(line)
                untimed += "TimeUS" not in fields
                rows.setdefault(name, []).append(None)
                continue
            if name not in rows:
                rows[name] = []
            values = message.to_dict()
            if name == "MSG":
                rows[name].append([str(values[label]) for label in fmt.columns])
            elif name == "PARM":
                rows[name].append([("NaN" if math.isnan(values[label]) else
                    "Infinity" if values[label] > 0 else "-Infinity")
                    if type(values[label]) is float and not math.isfinite(values[label])
                    else safe(values[label]) for label in fmt.columns])
            else:
                rows[name].append([safe(values[label]) for label in fmt.columns])
        elif name in selected:
            outside[name] = outside.get(name, 0) + 1
        elif len(offsets.get(name, ())) < MAX_RETAINED_OFFSETS:
            offsets.setdefault(name, []).append(start)
        else:
            dropped += 1
    if cursor < reader.data_len:
        spans.append({"offset": cursor, "bytes": reader.data_len - cursor,
                      "disposition": "TRAILING_BYTES_THE_DECODER_DID_NOT_FRAME"})
    exports, absent = {}, []
    for name in selected:
        if not rows.get(name):
            absent.append(name)
            continue
        if output_format == "jsonl":
            continue
        if name == "MSG":
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerows([schemas[name]["columns"], *rows[name]])
            exports[name] = stream.getvalue()
        else:
            exports[name] = "\n".join(
                ",".join(row) for row in [schemas[name]["columns"], *rows[name]]) + "\n"
        if sum(len(item) for item in exports.values()) > MAX_OUTPUT:
            raise ValueError("exported CSV exceeds 512MiB")
    undecoded = sum(span["bytes"] for span in spans)
    report = {
        "source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "source_bytes": len(raw), "digest_basis": DIGEST_BASIS,
        "decoder": f"pymavlink {PYMAVLINK} DFReader_binary",
        "scaling_basis": SCALING_BASIS,
        "clock": "TimeUS is the native unscaled microsecond count; no wall clock is derived",
        "selected_types": list(selected), "absent_selected_types": absent,
        "total_messages": total, "message_counts": dict(sorted(counts.items())),
        "exported_counts": {name: len(rows[name]) for name in sorted(rows)},
        "unselected_counts": {name: count for name, count in sorted(counts.items())
                              if name not in selected},
        "schemas": {name: schemas[name] for name in sorted(schemas)},
        "retained_unselected": {
            name: {"count": counts[name], "schema_ref": name, "offsets": offsets[name],
                   "offsets_retained": "ALL" if len(offsets[name]) == counts[name]
                   else "TRUNCATED_AT_LIMIT"}
            for name in sorted(offsets)},
        "unretained_unselected_records": dropped,
        "byte_coverage": {"decoded_bytes": decoded, "undecoded_bytes": undecoded,
                          "undecoded_spans": spans},
        "framing": COMPLETE if not spans else INCOMPLETE,
        "association_basis": "these CSVs share one source_sha256; that is file provenance, not hardware authentication or a same-session proof for any separately supplied file"}
    if record_window is not None:
        report.update(original_record_window=list(record_window), outside_window_selected_type_counts=outside,
            interval_basis="zero based decoded original records, stop exclusive; all original bytes scanned; outside records not meaning-converted",
            original_absent_selected_types=[name for name in selected if not counts.get(name)])
    if sum(report["exported_counts"].values()) + sum(report["unselected_counts"].values()) + sum(outside.values()) != total:
        raise ValueError("message accounting does not balance")
    if decoded + undecoded != len(raw):
        raise ValueError("byte accounting does not cover the input")
    if output_format == "jsonl":
        report.update(output_format="jsonl", selected_untimed_records=untimed,
            selected_timed_records=len(json_lines) - untimed,
            selection_basis="only selected messages; unselected records remain in source and report",
            association_basis="one saved source digest; no physical identity or complete-flight claim")
        exports = {"selected": "\n".join(json_lines) + "\n"} if json_lines else {}
    if include_source_indices:
        report['selected_record_refs'] = selected_refs
    return exports, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved .bin already on disk; no download is performed")
    parser.add_argument("output", type=Path, help="new directory for the per-type CSVs and report.json")
    parser.add_argument("--types", required=True,
                        help=f"CSV subset of {', '.join(CSV_SUPPORTED)}; JSONL retains the original five types")
    parser.add_argument("--output-format", choices=("csv", "jsonl"), default="csv")
    args = parser.parse_args(argv)
    try:
        exports, report = export_records(args.input, args.types.split(","), output_format=args.output_format)
        args.output.mkdir()
        for name, text in exports.items():
            write(args.output / f"{name}.{args.output_format}", text)
        write(args.output / "report.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    except UndecodedSpans as exc:
        parser.exit(3, json.dumps(exc.report, indent=2, sort_keys=True) + "\n"
                    + "DataFlash export refused: " + str(exc) + "\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"DataFlash export failed: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
