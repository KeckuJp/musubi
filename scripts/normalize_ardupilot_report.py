"""Reuse state/BAT converters on a saved decoded JSONL observation report, offline."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path

try:
    from scripts import convert_ardupilot_battery_csv as battery
    from scripts import convert_ardupilot_mode_csv as mode
except ModuleNotFoundError:
    import convert_ardupilot_battery_csv as battery
    import convert_ardupilot_mode_csv as mode

LIMIT = 128 * 1024 * 1024
BASIS = "pymavlink_decoded_no_second_wire_scaling"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("nonfinite JSON constant")


def convert(raw, *, mode_vehicle=None, battery_format=None, bat_details=False, record="mode", preserve_nonfinite_default=False):
    if not isinstance(raw, bytes) or not raw or len(raw) > LIMIT:
        raise ValueError("bounded saved reader report required")
    if ((mode_vehicle is None) == (battery_format is None) or type(bat_details) is not bool
            or (bat_details and battery_format is None)):
        raise ValueError("select exactly one existing MODE or BAT configuration")
    if type(preserve_nonfinite_default) is not bool or (preserve_nonfinite_default and mode_vehicle is None):
        raise ValueError("nonfinite default is only for selected parameter records")
    if mode_vehicle is not None and mode_vehicle not in mode.MODE_SCHEMAS:
        raise ValueError("unsupported MODE vehicle")
    if record not in ("mode", "arm", *mode.PARAM_SCHEMAS, *mode.PM_SCHEMAS) or (
            battery_format is not None and record != "mode"):
        raise ValueError("unsupported or conflicting state record selection")
    if battery_format is not None and battery_format not in ("ardupilot-bat-4.3", "ardupilot-bat-inst"):
        raise ValueError("unsupported BAT export layout")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    if not isinstance(document, dict) or document.get("value_basis") != BASIS:
        raise ValueError("expected decoded reader report, not raw JSONL or wire data")
    records = document.get("source_records")
    if not isinstance(records, list) or not records:
        raise ValueError("source_records required")
    selected_type = ("PARM" if record in mode.PARAM_SCHEMAS else "PM" if record in mode.PM_SCHEMAS
                     else record.upper()) if mode_vehicle is not None else "BAT"
    selected, fields, counts = [], [], {}
    reserved = ("source_report_record_index", "source_report_sha256")
    for index, source_record in enumerate(records):
        if (not isinstance(source_record, dict) or not isinstance(source_record.get("meta"), dict)
                or not isinstance(source_record.get("data"), dict)):
            raise ValueError("invalid source record shape")
        kind = source_record["meta"].get("type")
        if not isinstance(kind, str) or not kind or len(kind) > 256:
            raise ValueError("invalid source record type")
        counts[kind] = counts.get(kind, 0) + 1
        if kind != selected_type:
            continue
        data = source_record["data"]
        for key in data:
            if (not key or not key.isascii() or not key.replace("_", "a").isalnum()
                    or key in reserved):
                raise ValueError("unsupported or reserved selected field name")
            if key not in fields:
                fields.append(key)
        selected.append((index, data))
    if not selected:
        raise ValueError("no selected records")
    source_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(fields + list(reserved))
    for index, data in selected:
        row = []
        for key in fields:
            value = data.get(key)
            if type(value) is float and not math.isfinite(value):
                raise ValueError("nonfinite numeric JSON field")
            if value is None:
                cell = ""
            elif type(value) in (int, float):
                cell = str(value)
            elif isinstance(value, str) and not any(c in value for c in ',"\r\n\t') and all(ord(c) >= 32 for c in value):
                cell = value
            else:
                # Not a numerical interpretation: exact parsed value remains in source-report.json.
                cell = "jsonhex:" + json.dumps(value, ensure_ascii=False, allow_nan=False).encode().hex()
            row.append(cell)
        writer.writerow(row + [index, source_sha])
    selected_csv = stream.getvalue()
    if len(selected_csv.encode()) > LIMIT:
        raise ValueError("selected CSV exceeds bound")
    result = (mode.convert(selected_csv, mode_vehicle, record, preserve_nonfinite_default=preserve_nonfinite_default) if mode_vehicle is not None else
              battery.convert(selected_csv, battery_format, bat_details=bat_details))
    if len(result.encode()) > LIMIT:
        raise ValueError("normalized CSV exceeds bound")
    return result, dict(source_report_sha256=source_sha, source_records=len(records),
        selected_type=selected_type, selected_records=len(selected),
        not_selected_records=len(records) - len(selected), source_type_counts=counts,
        source_retention="source-report.json is the unchanged input; retain alongside observations.csv",
        value_basis=BASIS, selection_basis="caller-selected existing layout; not authentication",
        mode_vehicle=mode_vehicle, state_record=record if mode_vehicle is not None else None,
        battery_format=battery_format, bat_details=bat_details, preserve_nonfinite_default=preserve_nonfinite_default)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, help="new directory only")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode-vehicle", "--vehicle", dest="mode_vehicle", choices=tuple(mode.MODE_SCHEMAS))
    group.add_argument("--battery-format", choices=("ardupilot-bat-4.3", "ardupilot-bat-inst"))
    parser.add_argument("--bat-details", action="store_true")
    parser.add_argument("--preserve-nonfinite-default", action="store_true")
    parser.add_argument("--record", choices=("mode", "arm", *mode.PARAM_SCHEMAS, *mode.PM_SCHEMAS), default="mode")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            raw = source.read(LIMIT + 1)
        result, report = convert(raw, mode_vehicle=args.mode_vehicle,
                                 battery_format=args.battery_format, bat_details=args.bat_details, record=args.record,
                                 preserve_nonfinite_default=args.preserve_nonfinite_default)
        args.output.mkdir()
        (args.output / "source-report.json").write_bytes(raw)
        (args.output / "observations.csv").write_text(result, encoding="utf-8")
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"record normalization failed: {exc}\n")


if __name__ == "__main__":
    main()
