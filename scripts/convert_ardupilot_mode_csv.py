#!/usr/bin/env python3
"""Offline recorded MODE CSV -> explicitly selected vehicle names, no commands."""
import argparse
import csv
import io
import math
from pathlib import Path
import re

# Protocol numbers, not modes enabled on the recorded hardware/build.
# Exact producer source references are in the integration case.
PLANE_MODES = {
    0: "MANUAL", 1: "CIRCLE", 2: "STABILIZE", 3: "TRAINING", 4: "ACRO",
    5: "FLY_BY_WIRE_A", 6: "FLY_BY_WIRE_B", 7: "CRUISE", 8: "AUTOTUNE",
    10: "AUTO", 11: "RTL", 12: "LOITER", 13: "TAKEOFF", 14: "AVOID_ADSB",
    15: "GUIDED", 16: "INITIALISING", 17: "QSTABILIZE", 18: "QHOVER",
    19: "QLOITER", 20: "QLAND", 21: "QRTL", 22: "QAUTOTUNE", 23: "QACRO",
    24: "THERMAL", 25: "LOITER_ALT_QLAND",
}
MODE_SCHEMAS = {
    "plane": PLANE_MODES,
    "plane-4.5.5": PLANE_MODES,
    "rover-4.5.5": {0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER",
        6: "FOLLOW", 7: "SIMPLE", 8: "DOCK", 9: "CIRCLE", 10: "AUTO", 11: "RTL",
        12: "SMART_RTL", 15: "GUIDED", 16: "INITIALISING"},
    "copter-4.5.5": {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
        5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP",
        15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
        20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW", 24: "ZIGZAG",
        25: "SYSTEMID", 26: "AUTOROTATE", 27: "AUTO_RTL", 28: "TURTLE"},
    "sub-4.5.7": {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
        7: "CIRCLE", 9: "SURFACE", 16: "POSHOLD", 19: "MANUAL", 20: "MOTOR_DETECT", 21: "SURFTRAK"},
}
REQUIRED = ("TimeUS", "Mode", "ModeNum", "Rsn")
ARM_REQUIRED = ("TimeUS", "ArmState", "ArmChecks", "Forced", "Method")
ARM_FIELDS = ["reported_arm_state", "reported_arm_forced", "reported_arm_checks_mask", "reported_arm_method_code"]
ARM_SCHEMAS = {"plane", "plane-4.5.5", "rover-4.5.5", "copter-4.5.5", "sub-4.5.7"}
PARAM_SCHEMAS = {"parm-4.2.3": ("TimeUS", "Name", "Value"),
                 "parm-4.3.1": ("TimeUS", "Name", "Value", "Default")}
PARAM_VEHICLES = {schema: "plane" for schema in PARAM_SCHEMAS}
for _vehicle in ("plane-4.5.5", "rover-4.5.5", "copter-4.5.5", "sub-4.5.7"):
    PARAM_SCHEMAS["parm-" + _vehicle] = PARAM_SCHEMAS["parm-4.3.1"]
    PARAM_VEHICLES["parm-" + _vehicle] = _vehicle
PM_COMMON = ("MaxT", "Mem", "Load", "ErrL", "IntE", "ErrC", "SPIC", "I2CC", "I2CI", "Ex")
PM_SCHEMAS = {"pm-4.3.1": ("TimeUS", "NLon", "NLoop", *PM_COMMON),
              "pm-4.3.2": ("TimeUS", "LR", "NLon", "NL", *PM_COMMON)}
PM_FIELDS = ["reported_pm_schema", "reported_pm_long_loops", "reported_pm_loops",
             "reported_pm_loop_rate_hz", "reported_pm_max_loop_s", "reported_pm_memory_bytes",
             "reported_pm_scheduler_load", "reported_pm_extra_loop_s", "reported_pm_load_basis"]
# Exact source-qualified exports; no claim about every intervening build.
for _schema in ("rover-4.4.0", "rover-4.5.0"):
    MODE_SCHEMAS[_schema] = MODE_SCHEMAS["rover-4.5.5"]
for _schema in ("copter-4.4.0", "copter-4.5.0"):
    MODE_SCHEMAS[_schema] = MODE_SCHEMAS["copter-4.5.5"]
    ARM_SCHEMAS.add(_schema)


def unsigned(value, maximum):
    if not re.fullmatch(r"[0-9]+", value) or int(value) > maximum:
        raise ValueError("invalid MODE integer")
    return int(value)


def convert_parameter_snapshot(text, capture_time_us):
    """Read a fixed saved parameter set, never load it onto a vehicle."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    if len(text.encode("utf-8")) > 256000:
        raise ValueError("parameter snapshot too large")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "parameter_name", "reported_parameter_value",
                     "parameter_unit_basis", "parameter_time_basis", "source_line",
                     "source_value_hex", "source_document_hex"])
    names = set()
    for line_number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", parts[0]):
            raise ValueError("invalid selected saved-parameter syntax")
        name, value = parts
        if name in names:
            raise ValueError("duplicate parameter name is not a snapshot")
        if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]{6})?", value):
            raise ValueError("finite numeric saved parameter required")
        number = float(value)
        if not math.isfinite(number) or abs(number) > 3.4028234663852886e38:
            raise ValueError("saved parameter outside finite float32 range")
        writer.writerow([capture_time_us, name, value, "NATIVE_PARAMETER_UNIT_UNDECLARED",
                         "CALLER_CAPTURE_NOT_DEVICE_OR_FILE_TIME", line_number,
                         "hex:" + value.encode().hex(), "hex:" + text.encode().hex() if not names else ""])
        names.add(name)
    if not names:
        raise ValueError("empty parameter set")
    return output.getvalue()


def convert(text: str, vehicle: str, record: str = "mode", *, preserve_nonfinite_default=False) -> str:
    parameter = record in PARAM_SCHEMAS
    performance = record in PM_SCHEMAS
    if type(preserve_nonfinite_default) is not bool or (preserve_nonfinite_default and
            (not parameter or "Default" not in PARAM_SCHEMAS[record])):
        raise ValueError("nonfinite default requires an explicit QNff parameter record")
    if ((record not in ("mode", "arm") and not parameter and not performance)
            or (parameter and vehicle != PARAM_VEHICLES[record])
            or (performance and vehicle != "plane")
            or (record == "arm" and vehicle not in ARM_SCHEMAS)):
        raise ValueError("unsupported recorded state schema")
    required = (PARAM_SCHEMAS[record] if parameter else PM_SCHEMAS[record] if performance
                else REQUIRED if record == "mode" else ARM_REQUIRED)
    if vehicle not in MODE_SCHEMAS:
        raise ValueError("explicit supported vehicle required")
    modes = MODE_SCHEMAS[vehicle]
    if '"' in text or any(ord(c) < 32 and c not in '\r\n' for c in text):
        raise ValueError("unsupported CSV representation")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("missing MODE header or records")
    header = rows[0]
    if (len(set(header)) != len(header) or not set(required).issubset(header)
            or any(not c or c != c.strip() or '(' in c or ')' in c
                   or c.startswith(("reported_mode_", "reported_arm_", "reported_parameter_", "reported_pm_")) for c in header)
            or (record == "parm-4.2.3" and "Default" in header)
            or (record == "pm-4.3.1" and ("LR" in header or "NL" in header))
            or (record == "pm-4.3.2" and "NLoop" in header)):
        raise ValueError("incompatible MODE header")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    appended = ARM_FIELDS if record == "arm" else ["reported_mode_name", "reported_mode_known"] + ([] if vehicle == "plane" else ["reported_mode_schema"])
    if record == "arm" and vehicle != "plane":
        appended = appended + ["reported_arm_schema"]
    if parameter:
        appended = ["reported_parameter_schema", "reported_parameter_default_present"]
        if preserve_nonfinite_default:
            appended += ["reported_parameter_default_state"]
    if performance:
        appended = PM_FIELDS
    writer.writerow(header + appended)
    previous = None
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError("ragged or blank MODE row")
        values = dict(zip(header, row))
        time = unsigned(values["TimeUS"], 2**63 - 1)
        if previous is not None and time < previous:
            raise ValueError("decreasing MODE boot time")
        previous = time
        if performance:
            parsed = {key: unsigned(values[key], 65535 if key in ("LR", "NLon", "NLoop", "NL", "Load", "ErrL")
                                    else 2**32 - 1) for key in required[1:]}
            writer.writerow(row + [record, parsed["NLon"], parsed["NLoop" if record == "pm-4.3.1" else "NL"],
                parsed.get("LR", ""), parsed["MaxT"] / 1e6, parsed["Mem"], parsed["Load"] / 1000,
                parsed["Ex"] / 1e6, "SPARE_TIME_REPORTED" if record == "pm-4.3.1" else "LOOP_RATE_OR_SPARE_TIME_REPORTED"])
        elif parameter:
            name = values["Name"]
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", name):
                raise ValueError("invalid recorded parameter name")
            for key in required[2:]:
                value = values[key]
                if preserve_nonfinite_default and key == "Default" and value in ("NaN", "Infinity", "-Infinity"):
                    continue
                if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
                    raise ValueError("parameter value requires explicit finite numeric export")
                number = float(value)
                if not math.isfinite(number) or abs(number) > 3.4028234663852886e38:
                    raise ValueError("parameter value exceeds finite float32 range")
            writer.writerow(row + [record, "true" if "Default" in required else "false"] + (
                ["NONFINITE_UNUSABLE" if values["Default"] in ("NaN", "Infinity", "-Infinity") else "FINITE_REPORTED"]
                if preserve_nonfinite_default else []))
        elif record == "arm":
            state = unsigned(values["ArmState"], 1)
            forced = unsigned(values["Forced"], 1)
            checks = unsigned(values["ArmChecks"], 2**32 - 1)
            method = unsigned(values["Method"], 255)
            writer.writerow(row + ["ARMED_REPORTED" if state else "DISARMED_REPORTED",
                                   "true" if forced else "false", checks, method]
                            + ([] if vehicle == "plane" else [vehicle]))
        else:
            mode, alias, _reason = (unsigned(values[k], 255) for k in REQUIRED[1:])
            if mode != alias:
                raise ValueError("MODE aliases disagree")
            writer.writerow(row + [modes.get(mode, f"UNKNOWN_{mode}"),
                                   "true" if mode in modes else "false"]
                            + ([] if vehicle == "plane" else [vehicle]))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vehicle", choices=list(MODE_SCHEMAS), required=True)
    parser.add_argument("--record", choices=("mode", "arm", *PARAM_SCHEMAS, *PM_SCHEMAS, "parameter-file-2.4.49"), default="mode")
    parser.add_argument("--capture-time-us", type=int, help="explicit local capture clock for untimed parameter file only")
    parser.add_argument("--preserve-nonfinite-default", action="store_true")
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8", newline="") as source:
            text = source.read(256001) if args.record == "parameter-file-2.4.49" else source.read()
        if args.record == "parameter-file-2.4.49":
            if args.preserve_nonfinite_default:
                raise ValueError("snapshot has no reported default field")
            converted = convert_parameter_snapshot(text, args.capture_time_us)
        else:
            if args.capture_time_us is not None:
                raise ValueError("capture override is not valid for timed records")
            converted = convert(text, args.vehicle, args.record, preserve_nonfinite_default=args.preserve_nonfinite_default)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"MODE conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
