"""Fixed FarmBot saved pin reports, never GPIO acquisition or actuation."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys

try:
    from . import convert_ardupilot_battery_csv as existing
except ImportError:
    import convert_ardupilot_battery_csv as existing

PIN = "e711c03e628e29bc069d0b17434834ba5e33a7a9"
MAX_INPUT_BYTES = MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
MAX_PINS = 256
FIELDS = ["record_time_us", "source_record_hex", "farm_source_record_index",
          "farm_snapshot_sha256", "farm_pin_record_kind", "farm_pin_count",
          "farm_pin_number", "farm_pin_mode_reported", "farm_pin_status",
          "digital_level_reported", "digital_value_reported"]
PROVENANCE_FIELDS = ["farm_pin_value_provenance", "analog_written_value_reported"]
PROVENANCE_BASIS = ("FarmBotOS e711c03e lib/ exact-pin search: the only pin writer is BotState.set_pin_value "
                    "(bot_state.ex sha256 223e9f97..., bot_state_ng.ex 1a9a0ecd...), called by "
                    "InboundSideEffects pin_value_report with mode nil -> -1 (inbound_side_effects.ex 229f0d91...) "
                    "and PinControl.do_write_pin with literal mode 0/1 after the firmware write response "
                    "(pin_control.ex 68e9ac9e...); no generic state change carries pins. A recorded write "
                    "response is not physical success; no range, unit or calibration is given")


def pin_values(entry):
    if not isinstance(entry, dict):
        raise ValueError("pin entry must be an object")
    mode, value = entry.get("mode"), entry.get("value")
    if mode is not None and (type(mode) is not int or not -(2**31) <= mode < 2**31):
        raise ValueError("invalid reported pin mode")
    if value is not None and (type(value) not in (int, float) or
                              not math.isfinite(value) or abs(value) > 2**53 - 1):
        raise ValueError("invalid reported pin value")
    if mode is None:
        status = "MODE_NOT_REPORTED"
    elif mode == 1:
        status = "ANALOG_NOT_CONVERTED"
    elif mode != 0:
        status = "MODE_UNKNOWN"
    elif value is None:
        status = "VALUE_NOT_REPORTED"
    elif value not in (0, 1):
        status = "NON_BINARY_NOT_CONVERTED"
    else:
        return mode, "REPORTED_DIGITAL", ("LOW", "HIGH")[int(value)], int(value)
    return mode, status, None, None


def provenance(mode, value):
    if mode is None:
        return ["MODE_NOT_REPORTED", None]
    if mode in (0, 1):
        if value is None:
            return ["VALUE_NOT_REPORTED", None]
        return ["WRITE_RESPONSE_RECORDED_NOT_A_READ", value if mode == 1 else None]
    if mode == -1:
        return ["FIRMWARE_REPORT_MODE_NOT_GIVEN", None]
    return ["MODE_UNKNOWN", None]


def convert(text, write_provenance=False):
    if type(write_provenance) is not bool:
        raise ValueError("explicit write provenance selection required")
    if not text or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("empty or oversized FarmBot pin input")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(FIELDS + (PROVENANCE_FIELDS if write_provenance else []))
    report = dict(schema="farmbot-saved-pins/v1", schema_commit=PIN, source_records=0,
                  pin_rows=0, digital_rows=0, output_rows=0, statuses={},
                  source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                  clock_basis="UNKNOWN_CALLER_CAPTURE_NOT_PER_PIN_TIME",
                  meaning="SAVED_STATE_CAN_INCLUDE_WRITE_RESPONSE_NOT_MEASURED_GPIO",
                  unknown_fields="RETAINED_IN_EXACT_SOURCE_LINE_ON_SNAPSHOT_NOT_INTERPRETED")
    if write_provenance:
        report["write_provenance_basis"] = PROVENANCE_BASIS
    previous = None

    def emit(values, extra=(None, None)):
        if write_provenance:
            values = [*values, *extra]
        writer.writerow(["" if v is None else v for v in values])
        if output.tell() > MAX_OUTPUT_BYTES:
            raise ValueError("FarmBot pin output bound exceeded")
        report["output_rows"] += 1

    try:
        for index, line in enumerate(text.splitlines()):
            if not line.strip() or len(line.encode("utf-8")) > MAX_RECORD_BYTES:
                raise ValueError("empty or oversized FarmBot pin record")
            record = json.loads(line, object_pairs_hook=existing.unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
            if record["schema_commit"] != PIN:
                raise ValueError("unqualified FarmBot pin schema")
            time = existing.json_time(record, "farmbot-state")
            if previous is not None and time < previous:
                raise ValueError("decreasing capture timestamp")
            previous = time
            payload = record["payload"]
            if not isinstance(payload, dict):
                raise ValueError("saved BotState must be an object")
            pins = payload.get("pins")
            if pins is not None and not isinstance(pins, dict):
                raise ValueError("pins must be a map or missing")
            status = ("NOT_REPORTED" if "pins" not in payload else "NULL" if pins is None
                      else "EMPTY" if not pins else "REPORTED")
            if pins is not None and len(pins) > MAX_PINS:
                raise ValueError("FarmBot pin count bound exceeded")
            digest = "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
            count = None if pins is None else len(pins)
            emit([time, "hex:" + line.encode("utf-8").hex(), index, digest,
                  "SNAPSHOT", count, None, None, status, None, None])
            for key, entry in (pins or {}).items():
                if not re.fullmatch(r"0|[1-9][0-9]{0,2}", key) or int(key) > 255:
                    raise ValueError("pin key must be canonical decimal 0..255")
                mode, disposition, level, value = pin_values(entry)
                emit([time, "", index, digest, "PIN", count, int(key), mode, disposition, level, value],
                     provenance(mode, entry.get("value")))
                report["pin_rows"] += 1
                report["digital_rows"] += int(disposition == "REPORTED_DIGITAL")
                report["statuses"][disposition] = report["statuses"].get(disposition, 0) + 1
            report["source_records"] += 1
    except (KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("incompatible FarmBot pin export") from exc
    report["output_sha256"] = hashlib.sha256(output.getvalue().encode("utf-8")).hexdigest()
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--write-provenance", action="store_true",
                        help="add the fixed-source write/report provenance and the raw written analog value")
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError("output paths must be distinct and new")
        with args.input.open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("FarmBot pin input bound exceeded")
        output, report = convert(data.decode("utf-8"), args.write_provenance)
        with args.output.open("x", encoding="utf-8", newline="") as target:
            target.write(output)
        with args.report.open("x", encoding="utf-8") as target:
            json.dump(report, target, indent=2)
        return 0
    except (ValueError, OSError) as exc:
        print(f"FarmBot pin conversion rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
