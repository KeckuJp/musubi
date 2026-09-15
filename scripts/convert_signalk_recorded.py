#!/usr/bin/env python3
"""Finite saved Signal K delta quantities; no server or transport."""
import argparse
import csv
import io
import json
import math
import re
from pathlib import Path

try:
    from scripts.convert_mqtt_recorded import strict_json, LIMIT
    from scripts.convert_ardupilot_battery_csv import json_time
except ModuleNotFoundError:
    from convert_mqtt_recorded import strict_json, LIMIT
    from convert_ardupilot_battery_csv import json_time

QUANTITIES = {
    "navigation.speedThroughWater": "speed_through_water_m_s",
    "environment.depth.belowTransducer": "depth_below_transducer_m",
    "environment.water.temperature": "water_temperature_k",
    "environment.outside.temperature": "outside_temperature_k",
    "environment.outside.dewPointTemperature": "outside_dew_point_temperature_k",
    "environment.outside.apparentWindChillTemperature": "outside_apparent_wind_chill_temperature_k",
    "environment.outside.theoreticalWindChillTemperature": "outside_theoretical_wind_chill_temperature_k",
    "environment.outside.heatIndexTemperature": "outside_heat_index_temperature_k",
    "environment.outside.pressure": "outside_pressure_pa",
    "environment.outside.relativeHumidity": "outside_relative_humidity_ratio",
    "environment.outside.airDensity": "outside_air_density_kg_m3",
    "environment.outside.illuminance": "outside_illuminance_lux",
    "environment.wind.angleApparent": "wind_apparent_angle_port_negative_rad",
    "environment.wind.angleTrueGround": "wind_true_ground_angle_port_negative_rad",
    "environment.wind.angleTrueWater": "wind_true_water_angle_port_negative_rad",
    "environment.wind.directionTrue": "wind_direction_true_north_rad",
    "environment.wind.directionMagnetic": "wind_direction_magnetic_north_rad",
    "environment.wind.speedTrue": "wind_true_over_water_speed_m_s",
    "environment.wind.speedOverGround": "wind_true_over_ground_speed_m_s",
    "environment.wind.speedApparent": "wind_apparent_speed_m_s",
}
BATTERY_QUANTITIES = {"voltage": "battery_voltage_v", "current": "battery_current_a",
    "temperature": "battery_temperature_k", "capacity.nominal": "battery_nominal_j",
    "capacity.actual": "battery_actual_j", "capacity.remaining": "battery_remaining_j",
    "capacity.stateOfCharge": "battery_soc_reported_ratio", "capacity.stateOfHealth": "battery_soh_reported_ratio",
    "capacity.dischargeSinceFull": "battery_discharge_since_full_c", "capacity.timeRemaining": "battery_time_remaining_s",
    "lifetimeDischarge": "battery_lifetime_discharge_c", "lifetimeRecharge": "battery_lifetime_recharge_c"}
FIELDS = ["record_time_us", "source_record_hex", "signalk_context_hex", "signalk_path_hex",
          "signalk_update_index", "signalk_value_index", "signalk_value_status", *QUANTITIES.values(),
          *BATTERY_QUANTITIES.values(), "battery_id_hex", "battery_current_sign_basis"]


def convert(text, context):
    if not isinstance(context, str) or not context.startswith("vessels.") or len(context) <= 8:
        raise ValueError("explicit vessel context required")
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized delta capture")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    report = {"source_records": 0, "source_values": 0, "output_records": 0,
              "unsupported_values": 0, "retained_records": [], "clock": "REPORTED_UTC_NOT_AUTHENTICATED"}
    previous = None
    for line in text.splitlines():
        if not line.strip() or len(line.encode()) > 30000:
            raise ValueError("blank or oversized delta")
        record = strict_json(line)
        if not isinstance(record, dict) or record.get("context") != context or not isinstance(record.get("updates"), list):
            raise ValueError("delta context or updates mismatch")
        report["source_records"] += 1
        # Retain every source record, including metadata-only updates and unknown paths.
        raw = "hex:" + line.encode().hex()
        report["retained_records"].append(raw)
        for update_index, update in enumerate(record["updates"]):
            if not isinstance(update, dict) or ("source" in update and "$source" in update):
                raise ValueError("invalid delta update source")
            values = update.get("values", [])
            if not isinstance(values, list):
                raise ValueError("delta values must be an array")
            for value_index, entry in enumerate(values):
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or "value" not in entry:
                    raise ValueError("invalid delta value")
                report["source_values"] += 1
                path = entry["path"]
                battery = re.fullmatch(r"electrical\.batteries\.([A-Za-z0-9]{1,128})\.(.+)", path)
                field = QUANTITIES.get(path)
                if battery:
                    field = BATTERY_QUANTITIES.get(battery[2])
                if field is None:
                    report["unsupported_values"] += 1
                    continue
                timestamp = json_time(update, "utc-z")
                if previous is not None and timestamp < previous:
                    raise ValueError("selected timestamps decrease; preserve source order")
                previous = timestamp
                value = entry["value"]
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                    raise ValueError("selected quantity must be finite numeric or null")
                if type(value) is int and not -(2**63) <= value < 2**63:
                    raise ValueError("selected integer exceeds common signed range")
                if (path == "environment.water.temperature" or path.startswith("environment.outside.")) and value is not None and value < 0:
                    raise ValueError("negative absolute environmental quantity")
                if battery and field != "battery_current_a" and value is not None and value < 0:
                    raise ValueError("negative absolute battery quantity")
                writer.writerow({"record_time_us": timestamp, "source_record_hex": raw,
                    "signalk_context_hex": "hex:" + context.encode().hex(),
                    "signalk_path_hex": "hex:" + path.encode().hex(),
                    "signalk_update_index": update_index, "signalk_value_index": value_index,
                    "signalk_value_status": "NOT_PROVIDED" if value is None else "REPORTED",
                    field: value,
                    "battery_id_hex": "hex:" + battery[1].encode().hex() if battery else "",
                    "battery_current_sign_basis": "POSITIVE_OUT_OF_DEVICE" if field == "battery_current_a" else ""})
                report["output_records"] += 1
                if output.tell() > LIMIT:
                    raise ValueError("converted delta exceeds bound")
    if not report["output_records"]:
        raise ValueError("no selected values")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--context", required=True)
    args = parser.parse_args()
    try:
        output, report = convert(args.input.read_text(encoding="utf-8"), args.context)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        parser.exit(2, "Saved delta conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
