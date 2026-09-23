#!/usr/bin/env python3
"""Offline saved Cockpit `.clog` -> reported-display CSV for the existing common reader.

Blue Robotics Cockpit (bluerobotics/cockpit) records telemetry **on the display device** and exports it
as `Cockpit (<start> - <end>).clog`. Writer pinned at commit
9e48d8d671236223bc640c695f787eafe4e26e53, `src/libs/sensors-logging.ts` (33921 bytes, sha256
583b26e3029daf6bb7e797872f196f53d6596b2be5235be7394fc6e1c3b8b53e), with its unit vocabulary in
`src/libs/units.ts` and the preference default in `src/assets/defaults.ts` at the same commit.

What that writer actually stores is a **display string per variable**, already rounded, with the unit
token appended by the write site. Those tokens are the producer's own, so parsing them is reading what
the writer declared -- but the numbers are display values, never raw sensor accuracy, and the original
precision is gone. Every parsed value therefore keeps its source text beside it, and anything this pin
does not declare -- a datum, a frame, a vehicle identity, an operator's own variable -- is retained
rather than interpreted.

No vehicle is contacted, no command is sent and no GCS is operated: this reads one already-saved file.
"""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys

try:
    from scripts.convert_ardupilot_battery_csv import unique_object
    from scripts.recorded_units import normalize
except ModuleNotFoundError:  # running the script directly from scripts/
    from convert_ardupilot_battery_csv import unique_object
    from recorded_units import normalize

import csv

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
# The exact DatalogVariable members at the pin, in declaration order. These strings are the keys of a
# log point's `data` object, because the write site indexes with `[DatalogVariable.x]`.
STANDARD_VARIABLES = ("Roll", "Pitch", "Heading", "Depth", "Mode", "Battery voltage", "Battery current",
                      "GPS satellites", "GPS status", "Latitude", "Longitude", "Mission name", "Time",
                      "Date", "Instantaneous power")
# Quantities whose write expression appends a unit token that this pin declares. `°`, `V`, `A` and `W`
# are literals in sensors-logging.ts; `m`/`ft` are the whole of unitAbbreviation in units.ts, whose
# DistanceDisplayUnit has exactly the members Meters and Feet.
DEGREE = "°"
ATTITUDE = {"Roll": "cockpit_roll_rad", "Pitch": "cockpit_pitch_rad", "Heading": "cockpit_heading_rad"}
COORDINATES = {"Latitude": "cockpit_latitude_deg", "Longitude": "cockpit_longitude_deg"}
ELECTRICAL = {"Battery voltage": ("V", "cockpit_battery_voltage_v"),
              "Battery current": ("A", "cockpit_battery_current_a")}
# The writer declares W at its write site, but the adopted engineering-unit table has no watt entry, so
# this unit emits its own column instead of editing that shared table.
POWER = ("W", "cockpit_instantaneous_power_w")
DISTANCE_TOKENS = ("m", "ft")
VALUE_FIELDS = (list(ATTITUDE.values()) + ["cockpit_depth_m", "cockpit_depth_unit_token"]
                + [field for _, field in ELECTRICAL.values()] + [POWER[1]]
                + list(COORDINATES.values()) + ["cockpit_gps_satellites_reported"])
STATUS_FIELDS = ["cockpit_" + name + "_status" for name in
                 ("roll", "pitch", "heading", "depth", "battery_voltage", "battery_current",
                  "instantaneous_power", "latitude", "longitude", "gps_satellites")]
TEXT_FIELDS = ["cockpit_mode_hex", "cockpit_gps_status_hex", "cockpit_mission_name_hex",
               "cockpit_reported_time_hex", "cockpit_reported_date_hex"]
ACCOUNTING_FIELDS = ["cockpit_variable_count", "cockpit_selected_count", "cockpit_retained_only_count",
                     "cockpit_selected_max_stale_ms", "cockpit_reported_seconds_text"]
BASIS_FIELDS = ["cockpit_value_basis", "cockpit_depth_basis", "cockpit_clock_basis",
                "cockpit_seconds_basis", "cockpit_identity_basis"]
FIELDS = (["record_time_us", "source_document_sha256", "source_point_index", "source_record_hex"]
          + VALUE_FIELDS + STATUS_FIELDS + TEXT_FIELDS + ACCOUNTING_FIELDS + BASIS_FIELDS)
VALUE_BASIS = ("REPORTED_DISPLAY_STRING_ALREADY_ROUNDED_BY_THE_WRITER_UNIT_TOKEN_IS_THE_PRODUCERS_OWN_"
               "NOT_RAW_SENSOR_ACCURACY_ORIGINAL_PRECISION_UNRECOVERABLE")
# The write site builds depth as (-altitude.msl.to(preference)).toPrecision(4): the sign flip is the
# writer's, and the unit is whichever the operator was displaying at the time.
DEPTH_BASIS = ("REPORTED_AS_NEGATED_MSL_ALTITUDE_IN_THE_OPERATORS_DISPLAY_UNIT_NOT_A_MEASURED_DEPTH_"
               "AND_NOT_A_SOUNDING")
CLOCK_BASIS = "DISPLAY_DEVICE_UNIX_MILLISECONDS_NOT_VEHICLE_TIME_AND_NOT_ATTESTED"
SECONDS_BASIS = "REPORTED_RECORDING_OFFSET_CLAMPED_AT_ZERO_BY_THE_WRITER_SO_EPOCH_IS_AUTHORITATIVE"
IDENTITY_BASIS = "NO_VEHICLE_IDENTITY_FRAME_OR_DATUM_IS_DECLARED_BY_THIS_WRITER_NONE_IS_INFERRED"
UNAVAILABLE = "REPORTED_UNAVAILABLE_TEXT_RETAINED"
REPORTED = "REPORTED"
NOT_PRESENT = "NOT_REPORTED"
UNQUALIFIED_TOKEN = "REPORTED_UNIT_TOKEN_NOT_DECLARED_AT_THIS_PIN_TEXT_RETAINED"
FOOT_RETAINED = "REPORTED_IN_FEET_WHOSE_DEFINITION_THIS_PIN_DOES_NOT_DECLARE_TEXT_RETAINED"
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def text_hex(value):
    if not isinstance(value, str):
        raise ValueError("reported display value must be a string")
    return "hex:" + value.encode("utf-8").hex()


def split_display(text):
    """Split a written display value into its number and its trailing unit token.

    Returns (number, token) or (None, None) when the text is not one of this writer's numeric forms --
    which includes its own unavailable literals such as `undefined °`, `NaN m` and `Unknown`. Those are
    never coerced into a number.
    """
    parts = text.strip().split(" ")
    if not parts or not NUMBER.fullmatch(parts[0]):
        return None, None
    value = float(parts[0])
    if not math.isfinite(value):
        return None, None
    token = " ".join(parts[1:]).strip()
    return value, token


def quantity(row, entry, key, field, status_key, expected_token, transform=None):
    """One selected quantity: parse only when the writer's own token is there, else retain the text."""
    if entry is None:
        row[status_key] = NOT_PRESENT
        return
    number, token = split_display(entry["value"])
    if number is None:
        row[status_key] = UNAVAILABLE
        return
    if token != expected_token:
        row[status_key] = UNQUALIFIED_TOKEN
        return
    row[field] = transform(number) if transform else number
    row[status_key] = REPORTED


def depth_quantity(row, entry):
    if entry is None:
        row["cockpit_depth_status"] = NOT_PRESENT
        return
    number, token = split_display(entry["value"])
    if number is None:
        row["cockpit_depth_status"] = UNAVAILABLE
        return
    if token not in DISTANCE_TOKENS:
        row["cockpit_depth_status"] = UNQUALIFIED_TOKEN
        return
    row["cockpit_depth_unit_token"] = token
    if token == "ft":
        # Feet are one of exactly two display units this pin offers, but it declares no foot length, so
        # the number stays reported text rather than becoming an invented metre value.
        row["cockpit_depth_status"] = FOOT_RETAINED
        return
    row["cockpit_depth_m"] = normalize(number, "m")[1]
    row["cockpit_depth_status"] = REPORTED


def log_point_row(point, index, digest):
    if not isinstance(point, dict):
        raise ValueError("saved log point object required")
    epoch = point.get("epoch")
    if type(epoch) is not int or type(epoch) is bool or not 0 <= epoch * 1000 < 2**63:
        raise ValueError("reported Unix millisecond epoch required")
    data = point.get("data")
    if not isinstance(data, dict):
        raise ValueError("saved log point data object required")
    if not data:
        # The writer's own export skips a point whose data is empty; an empty row is not a report.
        raise ValueError("reported log point carries no variable")
    for name, entry in data.items():
        if (not isinstance(entry, dict) or not isinstance(entry.get("value"), str)
                or type(entry.get("lastChanged")) is not int or type(entry.get("lastChanged")) is bool):
            raise ValueError("reported variable requires string value and integer lastChanged: " + name)
    row = dict.fromkeys(FIELDS, "")
    row.update(record_time_us=epoch * 1000, source_document_sha256=digest, source_point_index=index,
               source_record_hex="hex:" + json.dumps(point, ensure_ascii=True, separators=(",", ":"),
                                                     allow_nan=False).encode().hex(),
               cockpit_value_basis=VALUE_BASIS, cockpit_depth_basis=DEPTH_BASIS,
               cockpit_clock_basis=CLOCK_BASIS, cockpit_seconds_basis=SECONDS_BASIS,
               cockpit_identity_basis=IDENTITY_BASIS)
    for name, field in ATTITUDE.items():
        quantity(row, data.get(name), name, field, "cockpit_" + field.split("_")[1] + "_status",
                 DEGREE, lambda number: normalize(number, "deg")[1])
    depth_quantity(row, data.get("Depth"))
    for name, (token, field) in ELECTRICAL.items():
        status = "cockpit_" + ("battery_voltage" if token == "V" else "battery_current") + "_status"
        quantity(row, data.get(name), name, field, status, token)
    quantity(row, data.get("Instantaneous power"), "Instantaneous power", POWER[1],
             "cockpit_instantaneous_power_status", POWER[0])
    for name, field in COORDINATES.items():
        # Degrees are kept as degrees: a coordinate is not an angle to convert, and this writer declares
        # no datum, so none is attached.
        quantity(row, data.get(name), name, field, "cockpit_" + name.lower() + "_status", DEGREE)
    satellites = data.get("GPS satellites")
    if satellites is None:
        row["cockpit_gps_satellites_status"] = NOT_PRESENT
    elif re.fullmatch(r"[0-9]{1,9}", satellites["value"].strip()):
        row["cockpit_gps_satellites_reported"] = int(satellites["value"].strip())
        row["cockpit_gps_satellites_status"] = REPORTED
    else:
        row["cockpit_gps_satellites_status"] = UNAVAILABLE
    for name, field in (("Mode", "cockpit_mode_hex"), ("GPS status", "cockpit_gps_status_hex"),
                        ("Mission name", "cockpit_mission_name_hex"),
                        ("Time", "cockpit_reported_time_hex"), ("Date", "cockpit_reported_date_hex")):
        entry = data.get(name)
        if entry is not None:
            row[field] = text_hex(entry["value"])
    selected = [name for name in STANDARD_VARIABLES if name in data]
    stale = [epoch - data[name]["lastChanged"] for name in selected]
    row.update(cockpit_variable_count=len(data), cockpit_selected_count=len(selected),
               cockpit_retained_only_count=len(data) - len(selected),
               cockpit_selected_max_stale_ms=max(stale) if stale else "")
    seconds = point.get("seconds")
    if seconds is not None:
        if type(seconds) is not int or type(seconds) is bool or seconds < 0:
            raise ValueError("reported recording offset must be a nonnegative integer")
        row["cockpit_reported_seconds_text"] = "i64:" + str(seconds)
    return row


def convert(text):
    raw = text.encode("utf-8")
    if len(raw) > MAX_INPUT:
        raise ValueError("saved log exceeds input bound")
    points = json.loads(text, object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
    if not isinstance(points, list) or not points:
        raise ValueError("nonempty saved log point array required")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    for index, point in enumerate(points):
        writer.writerow(log_point_row(point, index, digest))
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted output exceeds bound")
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8", newline="") as stream:
            text = stream.read(MAX_INPUT + 1)
        converted = convert(text)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Cockpit log conversion failed: {exc}\n")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
