#!/usr/bin/env python3
"""Finite saved Signal K delta quantities, bare or inside a saved server log line container;
no server, no transport, no subscription."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import stat
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
PROPULSION_QUANTITIES = {
    "revolutions": "propulsion_revolutions_hz", "temperature": "propulsion_temperature_k",
    "oilPressure": "propulsion_oil_pressure_pa", "coolantTemperature": "propulsion_coolant_temperature_k",
    "boostPressure": "propulsion_boost_pressure_pa", "runTime": "propulsion_run_time_s",
    "engineLoad": "propulsion_load_ratio", "engineTorque": "propulsion_torque_ratio",
    "fuel.rate": "propulsion_fuel_rate_m3_s",
}
# The server writes `Date.now() + ';' + discriminator + ';' + payload` per line, so the container
# columns are appended and delta-mode output stays byte-identical.
CONTAINER_FIELDS = [*FIELDS, "signalk_log_time_us", "signalk_log_time_basis",
                    "signalk_log_discriminator_hex"]
CONTAINER_LINE = re.compile(r"(\d{1,15});([^;]*);(.*)", re.DOTALL)
LOG_TIME_BASIS = ("HOST_WALL_CLOCK_WHEN_THE_SERVER_WROTE_THE_LINE_NOT_THE_OBSERVATION_TIME_NOT_"
                  "AUTHENTICATED_AND_NOT_ASSERTED_MONOTONIC")
MAX_INPUT_BYTES = 2 * 1024**3
MAX_RECORDS = 5_000_000
MAX_RECORD_BYTES = 30000
MAX_REPORT_BYTES = 64 * 1024**2


def convert(text, context, *, server_log=False, propulsion=False):
    return _convert(text, context, server_log=server_log, propulsion=propulsion)


def _convert(text, context, *, server_log, source_sha256=None, start_record=0, propulsion=False):
    if not isinstance(context, str) or not context.startswith("vessels.") or len(context) <= 8:
        raise ValueError("explicit vessel context required")
    if type(server_log) is not bool:
        raise ValueError("explicit container selection required")
    if type(propulsion) is not bool:
        raise ValueError("explicit propulsion selection required")
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized delta capture")
    output = io.StringIO(newline="")
    fields = CONTAINER_FIELDS if server_log else FIELDS
    if propulsion:
        fields = [*fields, *PROPULSION_QUANTITIES.values(), "propulsion_id_hex"]
    if source_sha256 is not None:
        fields = [*fields, "source_file_sha256", "source_record_index"]
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    report = {"source_records": 0, "source_values": 0, "output_records": 0,
              "unsupported_values": 0, "retained_records": [], "clock": "REPORTED_UTC_NOT_AUTHENTICATED"}
    if propulsion:
        report["propulsion_basis"] = (
            "SPEC_1_7_0_b8579f70_CALLER_SELECTED_REPORTED_SCALARS_NOT_ENGINE_IDENTITY_OR_THRUST")
    if server_log:
        report.update(unselected_context_records=0, provider_text_records=0, opaque_object_records=0,
            container="saved skserver-raw line container: host write time, provider discriminator, "
                      "payload; the payload is a delta only when the server saw updates",
            log_clock=LOG_TIME_BASIS)
    previous = None
    for record_index, line in enumerate(text.splitlines(), start_record):
        if not line.strip() or len(line.encode()) > 30000:
            raise ValueError("blank or oversized delta")
        # Retain every source line verbatim, including metadata-only updates and unknown paths.
        raw = "hex:" + line.encode().hex()
        container = {}
        if server_log:
            shape = CONTAINER_LINE.fullmatch(line)
            if not shape:
                # Raw provider text holding a newline lands here; it is refused, not reassembled.
                raise ValueError("saved server log line is not a complete host time, discriminator, payload line")
            container = {"signalk_log_time_us": int(shape[1]) * 1000,
                         "signalk_log_time_basis": LOG_TIME_BASIS,
                         "signalk_log_discriminator_hex": "hex:" + shape[2].encode().hex()}
            line = shape[3]
            report["source_records"] += 1
            report["retained_records"].append(raw)
            if not line.startswith("{"):
                # The server stringifies whatever it saw no updates on; never decoded here, and an
                # object stringified this way has already lost its content at the source.
                report["provider_text_records"] += 1
                if line == "[object Object]":
                    report["opaque_object_records"] += 1
                continue
            # A payload that claims to be a JSON object must parse and must be a delta: the pinned
            # writer only serializes an object when it saw updates on it.
            record = strict_json(line)
            if not isinstance(record.get("updates"), list):
                raise ValueError("saved server log JSON payload is not a delta carrying updates")
            if record.get("context") != context:
                report["unselected_context_records"] += 1
                continue
        else:
            record = strict_json(line)
            if not isinstance(record, dict) or record.get("context") != context or not isinstance(record.get("updates"), list):
                raise ValueError("delta context or updates mismatch")
            report["source_records"] += 1
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
                engine = re.fullmatch(r"propulsion\.([A-Za-z0-9]+)\.(.+)", path) if propulsion else None
                field = QUANTITIES.get(path)
                if battery:
                    field = BATTERY_QUANTITIES.get(battery[2])
                if engine:
                    field = PROPULSION_QUANTITIES.get(engine[2])
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
                if engine and value is not None:
                    if ((engine[2] in ("temperature", "coolantTemperature") and value < 0)
                            or (engine[2] in ("engineLoad", "engineTorque") and not 0 <= value <= 1)):
                        raise ValueError("propulsion absolute quantity or ratio outside bounds")
                writer.writerow({"record_time_us": timestamp, "source_record_hex": raw,
                    "signalk_context_hex": "hex:" + context.encode().hex(),
                    "signalk_path_hex": "hex:" + path.encode().hex(),
                    "signalk_update_index": update_index, "signalk_value_index": value_index,
                    "signalk_value_status": "NOT_PROVIDED" if value is None else "REPORTED",
                    field: value,
                    "battery_id_hex": "hex:" + battery[1].encode().hex() if battery else "",
                    "battery_current_sign_basis": "POSITIVE_OUT_OF_DEVICE" if field == "battery_current_a" else "",
                    **({"propulsion_id_hex": "hex:" + engine[1].encode().hex() if engine else ""}
                       if propulsion else {}),
                    **container,
                    **({"source_file_sha256": "sha256:" + source_sha256,
                        "source_record_index": record_index} if source_sha256 is not None else {})})
                report["output_records"] += 1
                if output.tell() > LIMIT:
                    raise ValueError("converted delta exceeds bound")
    if not report["output_records"]:
        raise ValueError("no selected values")
    return output.getvalue(), report


def convert_path(path, context, *, start_record, stop_record, propulsion=False):
    """Scan a saved native log and pass one bounded record interval to the existing decoder.

    Record selection is independent of both clocks: host time may decrease and
    excluded delta timestamps are not decoded. The digest covers the original,
    not a generated selection file; indices refer to its native physical lines.
    """
    if type(propulsion) is not bool:
        raise ValueError("explicit propulsion selection required")
    if (type(start_record) is not int or type(stop_record) is not int
            or not 0 <= start_record < stop_record <= MAX_RECORDS):
        raise ValueError("invalid record interval")
    path = Path(path).resolve()
    metadata = path.stat()
    if (not stat.S_ISREG(metadata.st_mode)
            or path.parts[1:2] in (("sys",), ("proc",), ("dev",))):
        raise ValueError("saved regular input required")
    if metadata.st_size > MAX_INPUT_BYTES:
        raise ValueError("Signal K input byte bound exceeded")
    digest = hashlib.sha256()
    selected = []
    total_bytes = selected_bytes = count = 0

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if signature(before) != signature(metadata):
            raise ValueError("source changed before read")
        while True:
            raw = stream.readline(MAX_RECORD_BYTES + 3)
            if not raw:
                break
            native = raw[:-1].removesuffix(b"\r") if raw.endswith(b"\n") else raw
            if len(native) > MAX_RECORD_BYTES:
                raise ValueError("Signal K individual record byte bound exceeded")
            total_bytes += len(raw)
            if total_bytes > MAX_INPUT_BYTES:
                raise ValueError("Signal K input byte bound exceeded")
            digest.update(raw)
            text = native.decode("utf-8")
            if (not text.strip() or "\0" in text
                    or any(c in text for c in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
                    or not CONTAINER_LINE.fullmatch(text)):
                raise ValueError("unsupported native server log record representation")
            if start_record <= count < stop_record:
                retained = text + "\n"
                selected_bytes += len(retained.encode("utf-8"))
                if selected_bytes > LIMIT:
                    raise ValueError("Signal K selected source byte bound exceeded")
                selected.append(retained)
            count += 1
            if count > MAX_RECORDS:
                raise ValueError("Signal K source record count bound exceeded")
        after = os.fstat(stream.fileno())
        if signature(after) != signature(before) or total_bytes != before.st_size:
            raise ValueError("source changed during read")
    if stop_record > count:
        raise ValueError("record interval exceeds source records")
    source_sha = digest.hexdigest()
    output, report = _convert("".join(selected), context, server_log=True,
                              source_sha256=source_sha, start_record=start_record, propulsion=propulsion)
    encoded = output.encode("utf-8")
    if len(encoded) > LIMIT:
        raise ValueError("Signal K selected output byte bound exceeded")
    report.update(output_sha256=hashlib.sha256(encoded).hexdigest(), output_bytes=len(encoded),
        record_selection={"source_sha256": source_sha, "source_bytes": total_bytes,
            "source_records": count, "selected_records": stop_record - start_record,
            "excluded_records": count - (stop_record - start_record),
            "start_record": start_record, "stop_record_exclusive": stop_record,
            "outside_selection": "NOT_DECODED_OR_VALIDATED",
            "delta_clock_scope": "SELECTED_RECORDS_ONLY_NO_CROSS_WINDOW_ORDER_CLAIM",
            "bounds": {"max_input_bytes": MAX_INPUT_BYTES, "max_records": MAX_RECORDS,
                "max_record_bytes": MAX_RECORD_BYTES, "max_selected_source_bytes": LIMIT,
                "max_output_bytes": LIMIT, "max_report_bytes": MAX_REPORT_BYTES}})
    if len(json.dumps(report).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("Signal K selected report byte bound exceeded")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--server-log", action="store_true",
                        help="input is a saved skserver-raw line container, not bare deltas")
    parser.add_argument("--propulsion", action="store_true", help="select fixed propulsion scalar reports")
    parser.add_argument("--start-record", type=int, help="first native server-log record, zero-based")
    parser.add_argument("--stop-record", type=int, help="exclusive native server-log record stop")
    args = parser.parse_args()
    try:
        if args.start_record is not None or args.stop_record is not None:
            if args.start_record is None or args.stop_record is None or not args.server_log:
                raise ValueError("both record interval bounds and --server-log required")
            output, report = convert_path(args.input, args.context,
                start_record=args.start_record, stop_record=args.stop_record, propulsion=args.propulsion)
        else:
            output, report = convert(args.input.read_text(encoding="utf-8"), args.context,
                                     server_log=args.server_log, propulsion=args.propulsion)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        parser.exit(2, "Saved delta conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
