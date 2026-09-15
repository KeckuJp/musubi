"""Passive classic CAN export with caller-qualified DBC; no CAN device access."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from scripts.convert_dronecan_capture import capture_rows
from scripts.recorded_units import UNITS, normalize
OUTPUT_LIMIT = 128 * 1024 * 1024
FIELDS = ["record_time_us", "frame_id", "frame_kind", "dbc_sha256",
          "signal_name_hex", "source_data_hex"] + sorted({v[0] for v in UNITS.values()})


def convert(text, dbc, *, extended=False, include_enums=False, fd=False):
    import cantools
    if cantools.__version__ != "40.7.1":
        raise ValueError("unqualified decoder version")
    if not isinstance(dbc, bytes) or len(dbc) > 2 * 1024 * 1024:
        raise ValueError("expected bounded DBC bytes")
    database = cantools.database.load_string(dbc.decode("utf-8"), database_format="dbc", strict=True)
    digest = hashlib.sha256(dbc).hexdigest()
    messages = {}
    for message in database.messages:
        key = (message.frame_id, message.is_extended_frame)
        if key in messages:
            raise ValueError("ambiguous DBC identifier")
        messages[key] = message
    output = io.StringIO(newline="")
    fields = FIELDS + (["enum_raw_code", "enum_label_hex", "enum_status", "multiplex_selector_reported"] if include_enums else [])
    if fd:
        fields += ["fd_flags_reported"]
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    report = dict(frames=0, decoded_frames=0, observations=0, unsupported=[],
                  dbc_sha256=digest, clock="Unknown", decoder="cantools-40.7.1")
    retained_size = 0

    def unsupported(record):
        nonlocal retained_size
        retained_size += len(json.dumps(record))
        if retained_size > 16 * 1024 * 1024:
            raise ValueError("unsupported report exceeds bound")
        report["unsupported"].append(record)

    for line, row, timestamp, identifier, data in capture_rows(text, extended=extended, fd=fd):
        report["frames"] += 1
        message = messages.get((identifier, extended))
        if message is not None and fd and not message.is_fd:
            raise ValueError("FD capture requires explicit FD dictionary declaration")
        if message is None or (message.is_fd and not fd) or message.is_container:
            unsupported(dict(line=line, source=row, reason="UNSELECTED_FRAME"))
            continue
        if len(data) != message.length:
            raise ValueError("DBC frame length mismatch")
        values = message.decode(data, decode_choices=False, scaling=not include_enums,
                                allow_truncated=False, allow_excess=False)
        emitted = 0
        for signal in message.signals:
            if signal.name not in values:  # Inactive multiplex branch is not a missing observation.
                continue
            base = dict(record_time_us=timestamp, frame_id=identifier,
                frame_kind=("extended" if extended else "standard") + ("_fd" if fd else ""), dbc_sha256="sha256:" + digest,
                signal_name_hex="hex:" + signal.name.encode().hex(), source_data_hex="hex:" + data.hex())
            if fd:
                base["fd_flags_reported"] = int(row[2], 16)
            if include_enums and (signal.choices or signal.is_multiplexer):
                raw = values[signal.name]
                if type(raw) is not int or not -(2**63) <= raw < 2**63:
                    raise ValueError("enum requires common-range raw integer")
                label = signal.choices.get(raw) if signal.choices else None
                writer.writerow(dict(base, enum_raw_code=raw,
                    enum_label_hex="hex:" + str(label).encode().hex() if label is not None else "",
                    enum_status="DECLARED_LABEL" if label is not None else "UNKNOWN_CODE" if signal.choices else "SELECTOR_NO_LABEL",
                    multiplex_selector_reported=int(signal.is_multiplexer)))
                emitted += 1
                continue
            unit = UNITS.get(signal.unit)
            if unit is None or signal.choices or signal.is_multiplexer:
                unsupported(dict(line=line, source=row,
                    signal=signal.name, reason="UNIT_OR_ENUM_NOT_QUALIFIED"))
                continue
            value = (signal.conversion.raw_to_scaled(values[signal.name], decode_choices=False)
                     if include_enums else values[signal.name])
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 2**53:
                raise ValueError("unrepresentable decoded numeric signal")
            field, normalized = normalize(value, signal.unit)
            writer.writerow(dict(base, **{field: normalized}))
            emitted += 1
        report["observations"] += emitted
        report["decoded_frames"] += int(emitted > 0)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("expanded output exceeds bound")
    report["conversion_status"] = "OBSERVATIONS_PRODUCED" if report["observations"] else "NO_SUPPORTED_OBSERVATIONS"
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("dbc", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--include-enums", action="store_true", help="retain raw choice codes and declared labels, not physical-state certification")
    parser.add_argument("--fd", action="store_true", help="require explicit saved FD capture and matching DBC frame declaration")
    args = parser.parse_args()
    with args.capture.open("rb") as source:
        capture = source.read(16 * 1024 * 1024 + 1).decode("utf-8")
    with args.dbc.open("rb") as source:
        dbc = source.read(2 * 1024 * 1024 + 1)
    output, report = convert(capture, dbc, extended=args.extended, include_enums=args.include_enums, fd=args.fd)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    if not report["observations"]:
        parser.exit(2, "No supported observations; diagnostic report saved, not a successful adaptation\n")


if __name__ == "__main__":
    main()
