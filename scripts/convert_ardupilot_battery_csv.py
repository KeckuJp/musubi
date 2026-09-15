#!/usr/bin/env python3
"""Offline finite-current BAT CSV -> unit-normalized CSV for TelemetryCsvReader.

No external decoder, transport or sensor-health claim.
"""
import argparse
import csv
import io
import json
import hashlib
from datetime import datetime, timezone
import math
from pathlib import Path
import re

try:
    from scripts.recorded_units import normalize as normalize_unit
except ModuleNotFoundError:
    from recorded_units import normalize as normalize_unit

DJI_FORMAT = "dji-m3-osd-4ec6"
ROBOTNIK_FORMAT = "robotnik-battery-status-json"
FACTSHEET_FORMAT = "vda-factsheet-3.0"
DOCKER_FORMAT = "docker-stats-27.3.1-linux"
TEGRA_FORMAT = "tegrastats-l4t-36.4.3"
TEGRA_FIELDS = ("tegrastats_record_kind", "tegrastats_cpu_index", "tegrastats_block_hex",
    "tegrastats_reported_utilization_fraction", "tegrastats_reported_frequency_hz",
    "temperature_k", "tegrastats_value_disposition", "tegrastats_source_sha256")
ORT_FORMAT = "onnxruntime-profile-1.20.1"
TRT_FORMAT = "tensorrt-times-10.7"
TRT_TIMES = (("startEnqMs", "host_enqueue_start_offset_s"), ("endEnqMs", "host_enqueue_end_offset_s"),
    ("startH2dMs", "h2d_start_offset_s"), ("endH2dMs", "h2d_end_offset_s"),
    ("startComputeMs", "compute_start_offset_s"), ("endComputeMs", "compute_end_offset_s"),
    ("startD2hMs", "d2h_start_offset_s"), ("endD2hMs", "d2h_end_offset_s"),
    ("h2dMs", "h2d_duration_s"), ("computeMs", "compute_duration_s"),
    ("d2hMs", "d2h_duration_s"), ("latencyMs", "ideal_transfer_compute_sum_s"))
RMF_FORMAT = "rmf-robot-state-6990fc74"
RMF_WEB_FORMAT = "rmf-web-fleet-state-a736"
RMF_WEB_FIELDS = ("rmf_fleet_name_hex", "rmf_robot_key_hex", "rmf_name_hex", "rmf_task_id_hex",
    "battery_remaining_fraction", "rmf_battery_disposition", "rmf_status_reported_hex",
    "rmf_status_disposition", "rmf_unix_millis_reported_text", "rmf_snapshot_sha256")
FARMDUINO_FORMAT = "farmduino-r82-52053ec8"
FARM_POINT_FORMAT = "farmbot-point-15.30.5"
FARM_POINT_FIELDS = ("farm_point_id_text", "farm_point_device_id_text", "farm_point_type",
    "farm_point_x_m", "farm_point_y_m", "farm_point_z_m", "farm_point_coordinate_basis")
PING_FORMAT = "ping1d-distance-1746"
PARROT_FORMAT = "parrot-battery-2299"
MSDK_FORMAT = "dji-msdk-battery-5.18"
MSDK_FIELDS = ("sdk_key_reported", "sdk_component_index", "sdk_value_disposition",
    "battery_voltage_v", "battery_current_a", "battery_current_sign_basis", "battery_temperature_k",
    "battery_full_charge_ah", "battery_remaining_charge_ah", "battery_remaining_percent_code",
    "battery_remaining_fraction", "battery_connection_reported")
PARROT_FIELDS = ("parrot_event_reported", "battery_voltage_v", "battery_temperature_k",
    "battery_full_charge_ah", "battery_remaining_charge_ah", "battery_cell_index",
    "battery_cell_voltage_v", "battery_cycle_count", "battery_health_percent_code",
    "battery_state_of_health_fraction", "battery_health_basis")
PROTO_FORMAT = "mavsdk-battery-protobuf"
GRPC_FORMAT = "mavsdk-battery-grpc"
PING_FIELDS = ("sonar_reported_distance_m", "sonar_confidence_code", "sonar_reported_confidence_fraction",
    "sonar_confidence_status", "sonar_message_id", "sonar_source_device_id", "sonar_transmit_duration_s",
    "sonar_ping_number_reported", "sonar_scan_start_m", "sonar_scan_length_m", "sonar_gain_code")
RMF_MODES = ("IDLE", "CHARGING", "MOVING", "PAUSED", "WAITING", "EMERGENCY", "GOING_HOME",
             "DOCKING", "ADAPTER_ERROR", "CLEANING", "PERFORMING_ACTION", "ACTION_COMPLETED")
RMF_FIELDS = ("battery_remaining_fraction", "rmf_name_hex", "rmf_model_hex", "rmf_task_id_hex",
              "rmf_sequence_reported_text", "rmf_mode_number_reported", "rmf_mode_reported",
              "rmf_mode_request_id_reported_text", "rmf_performing_action_hex")
DOCKER_FIELDS = ("docker_value_basis", "docker_id_hex", "docker_cpu_equivalents_reported",
    "docker_memory_fraction_reported", "docker_memory_used_approx_bytes", "docker_memory_limit_approx_bytes",
    "docker_network_rx_approx_bytes", "docker_network_tx_approx_bytes",
    "docker_block_read_approx_bytes", "docker_block_write_approx_bytes", "docker_pids_reported_text")
FACTSHEET_QUANTITIES = (("minimumSpeed", "minimum_speed_m_s"), ("maximumSpeed", "maximum_speed_m_s"),
    ("maximumAcceleration", "maximum_acceleration_m_s2"), ("maximumDeceleration", "maximum_deceleration_m_s2"),
    ("minimumHeight", "minimum_height_m"), ("maximumHeight", "maximum_height_m"),
    ("width", "width_m"), ("length", "length_m"),
    ("minimumAngularSpeed", "minimum_angular_speed_rad_s"), ("maximumAngularSpeed", "maximum_angular_speed_rad_s"))
FACTSHEET_FIELDS = ("factsheet_basis", "factsheet_manufacturer_hex", "factsheet_serial_hex",
    *["declared_" + name for _, name in FACTSHEET_QUANTITIES], "declared_maximum_load_mass_kg")
ROBOTNIK_FIELDS = ("robotnik_row_kind", "battery_time_remaining_reported_s", "battery_time_charging_reported_s",
                   "battery_connection_reported", "battery_cell_count", "battery_cell_index", "battery_cell_voltage_v")
DJI_FIELDS = ("battery_id_reported", "battery_temperature_k", "battery_total_remaining_fraction",
              "battery_time_remaining_s", "battery_cycle_count", "battery_sn_hex",
              "battery_firmware_hex", "battery_array_count")

REQUIRED = ("TimeUS", "Instance", "Volt", "Curr", "CurrTot", "EnrgTot", "RemPct")
NORMALIZED = ("battery_voltage_v", "battery_current_a", "battery_consumed_ah",
              "battery_consumed_j", "battery_power_w", "battery_remaining_fraction")
JSON_ELECTRICAL_FIELDS = (*NORMALIZED, "battery_stored_charge_ah",
                          "battery_reported_charging_state", "battery_current_sign_basis")
PX4_REQUIRED = ("timestamp", "id", "voltage_v", "current_a", "discharged_mah", "remaining")
CELL_FORMATS = ("ardupilot-bcl-8178", "ardupilot-bcl2-8178")
CELL43_FORMATS = ("ardupilot-bcl-4.3", "ardupilot-bcl2-4.3")
CELL_REQUIRED = ("TimeUS", "Volt", *[f"V{i}" for i in range(1, 11)])
CELL_FIELDS = ("battery_bank_selected", *[f"battery_cell_{i}_v" for i in range(1, 11)],
               *[f"battery_cell_{i}_status" for i in range(1, 11)])
FORMATS = ("ardupilot-bat", "ardupilot-bat-inst", "ardupilot-bat-4.3", "px4-battery",
           "vda-state-2.0", "vda-state-2.1", "vda-state-3.0", "ros-battery-json", "farmbot-state", "mavsdk-battery-json", DJI_FORMAT, ROBOTNIK_FORMAT, FACTSHEET_FORMAT, DOCKER_FORMAT, TEGRA_FORMAT, ORT_FORMAT, TRT_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, FARM_POINT_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT, *CELL_FORMATS, *CELL43_FORMATS)
MAVSDK_FIELDS = ("battery_id_reported", "battery_temperature_k", "battery_time_remaining_s",
                 "battery_function_reported")
BAT_DETAIL_FIELDS = ("battery_resting_voltage_estimate_v", "battery_resistance_estimate_ohm",
                     "battery_temperature_k", "battery_temperature_state",
                     "battery_health_reported", "battery_state_of_health_fraction")
FARM_IDS = ("controller_uuid", "controller_version", "controller_commit", "firmware_version", "firmware_commit")
FARM_FIELDS = ("runtime_uptime_s", "soc_temperature_k", "busy_reported", "idle_reported", "locked_reported",
               *[f"{key}_hex" for key in FARM_IDS],
               *[f"farm_{kind}_{axis}_{unit}" for kind, unit in
                 (("position", "m"), ("scaled_encoder", "m"), ("raw_encoder", "edges"))
                 for axis in "xyz"])
ACTION_STATES = ("WAITING", "INITIALIZING", "RUNNING", "PAUSED", "RETRIABLE", "FINISHED", "FAILED", "UNKNOWN")
ERROR_LEVELS = ("WARNING", "URGENT", "CRITICAL", "FATAL", "UNKNOWN")
VDA_FIELDS = ("vda_operating_mode_reported", "vda_driving_reported", "vda_paused_reported",
              "vda_emergency_stop_reported", "vda_protective_field_violation_reported",
              "vehicle_vx_m_s", "vehicle_vy_m_s", "vehicle_yaw_rate_rad_s",
              "vda_order_id_hex", "vda_order_update_id", "vda_last_node_id_hex", "vda_last_node_sequence",
              *[f"vda_action_{state.lower()}_count" for state in ACTION_STATES],
              *[f"vda_error_{level.lower()}_count" for level in ERROR_LEVELS])
VDA_FIELDS += ("vda_map_id_hex", "vda_map_x_m", "vda_map_y_m", "vda_map_theta_rad",
               "vda_position_initialized_reported", "vda_localized_reported", "vda_position_state",
               "vda_localization_score_reported", "vda_deviation_range_m")


def integer(value, maximum, name):
    if not re.fullmatch(r"[0-9]+", value) or int(value) > maximum:
        raise ValueError(f"invalid {name}")
    return int(value)


def number(value, name):
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not math.isfinite(result):
        raise ValueError(f"nonfinite {name}")
    return result


def convert(text: str, source_format: str = "ardupilot-bat", *, vda_actions=False, vda_errors=False,
            bat_details=False, allow_missing_voltage=False, capture_time_us=None) -> str:
    if source_format not in FORMATS:
        raise ValueError("unsupported source format")
    if allow_missing_voltage and source_format != "ros-battery-json":
        raise ValueError("missing voltage policy requires qualified ROS BatteryState")
    if bat_details and source_format not in ("ardupilot-bat-4.3", "ardupilot-bat-inst"):
        raise ValueError("BAT details require a qualified BAT layout")
    if vda_actions and not source_format.startswith("vda-state-"):
        raise ValueError("action expansion requires selected VDA state version")
    if vda_errors and source_format not in ("vda-state-2.1", "vda-state-3.0"):
        raise ValueError("error detail expansion requires VDA2.1 or3.0")
    if source_format in (ORT_FORMAT, TRT_FORMAT):
        return convert_runtime_profile(text, capture_time_us, source_format)
    if capture_time_us is not None:
        raise ValueError("capture time option requires runtime profile")
    if source_format.startswith("vda-state-") or source_format in ("ros-battery-json", "farmbot-state", "mavsdk-battery-json", DJI_FORMAT, ROBOTNIK_FORMAT, FACTSHEET_FORMAT, DOCKER_FORMAT, TEGRA_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, FARM_POINT_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT):
        return convert_json(text, source_format, vda_actions=vda_actions, vda_errors=vda_errors,
                            allow_missing_voltage=allow_missing_voltage)
    cells_layout = source_format in (*CELL_FORMATS, *CELL43_FORMATS)
    cells43 = source_format in CELL43_FORMATS
    extension_cells = source_format == "ardupilot-bcl2-4.3"
    cell_indices = range(13, 15) if extension_cells else range(1, 13) if cells43 else range(1, 11)
    cell_fields = ("battery_bank_selected", *[f"battery_cell_{i}_v" for i in cell_indices],
                   *[f"battery_cell_{i}_status" for i in cell_indices])
    required = PX4_REQUIRED if source_format == "px4-battery" else REQUIRED
    if source_format == "ardupilot-bat-inst":
        required = ("TimeUS", "Inst", *REQUIRED[2:])
    if bat_details:
        required += ("VoltR", "Temp", "Res") + (("H", "SH") if source_format == "ardupilot-bat-inst" else ())
    if cells_layout:
        required = (("TimeUS", "Instance", *(() if extension_cells else ("Volt",)),
                     *[f"V{i}" for i in cell_indices]) if cells43 else CELL_REQUIRED)
    # The downstream CSV contract is deliberately unquoted. Reject, do not strip quoting.
    if '"' in text or '\x00' in text or any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise ValueError("unsupported CSV representation")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("missing header or data")
    header = rows[0]
    if (len(set(header)) != len(header) or any(not c or c != c.strip() for c in header)
            or any(c.startswith("battery_") or '(' in c or ')' in c for c in header)
            or not set(required).issubset(header)):
        raise ValueError("incompatible BAT header")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header + list(NORMALIZED) + (list(cell_fields) if cells_layout else [])
                    + (list(BAT_DETAIL_FIELDS) if bat_details else []))
    previous = None
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError("ragged or blank BAT row")
        values = dict(zip(header, row))
        time = integer(values[required[0]], 2**63 - 1, required[0])
        if not cells_layout or cells43:
            integer(values[required[1]], 255, required[1])
        if previous is not None and time < previous:
            raise ValueError("decreasing TimeUS")
        previous = time
        cell_values = []
        if cells_layout:
            voltage = None if extension_cells else number(values["Volt"], "Volt")
            if voltage is not None and voltage < 0:
                raise ValueError("invalid pack voltage")
            encoded = [integer(values[f"V{i}"], 65535, f"V{i}") for i in cell_indices]
            normalized = [voltage, None, None, None, None, None]
            cell_values = ["" if cells43 else str(CELL_FORMATS.index(source_format))]
            cell_values += ["" if value == 0 else repr((value - 1) / 1000) for value in encoded]
            cell_values += ["NOT_PROVIDED" if value == 0 else "REPORTED" for value in encoded]
        elif source_format != "px4-battery":
            voltage = number(values["Volt"], "Volt")
            if voltage < 0:
                raise ValueError("invalid pack voltage")
            current, charge, energy = (
                None if source_format in ("ardupilot-bat-inst", "ardupilot-bat-4.3") and values[k].lower() == "nan"
                else number(values[k], k) for k in REQUIRED[3:6])
            remaining = integer(values["RemPct"], 255, "RemPct")
            if remaining > 100 and remaining != 255:
                raise ValueError("invalid RemPct")
            normalized = [voltage, current, None if charge is None else charge / 1000,
                          None if energy is None else energy * 3600,
                          None if current is None else voltage * current,
                          None if remaining == 255 else remaining / 100]
        else:
            voltage, current, charge, remaining = (number(values[k], k) for k in required[2:])
            if voltage < 0 or (charge < 0 and charge != -1) or (remaining != -1 and not 0 <= remaining <= 1):
                raise ValueError("invalid PX4 quantity range")
            voltage = None if voltage == 0 else voltage
            current = None if current == -1 else current
            normalized = [voltage, current, None if charge == -1 else charge / 1000, None,
                          None if voltage is None or current is None else voltage * current,
                          None if remaining == -1 else remaining]
        if not all(v is None or math.isfinite(v) for v in normalized):
            raise ValueError("normalized value overflow")
        details = bat_reported_details(values, source_format) if bat_details else []
        writer.writerow(row + ["" if v is None else repr(v) for v in normalized] + cell_values
                        + ["" if v is None else v for v in details])
    return output.getvalue()


def bat_reported_details(values, source_format):
    # DFReader c is already degrees C (wire int16 * .01), not raw centidegrees.
    resting, resistance = (None if values[key].lower() == "nan" else number(values[key], key)
                           for key in ("VoltR", "Res"))
    if any(value is not None and value < 0 for value in (resting, resistance)):
        raise ValueError("invalid BAT estimate")
    temperature = number(values["Temp"], "Temp")
    if not -273.15 <= temperature <= 327.67 or not math.isclose(
            temperature * 100, round(temperature * 100), abs_tol=1e-8, rel_tol=0):
        raise ValueError("invalid exported BAT temperature")
    health = soh = None
    if source_format == "ardupilot-bat-inst":
        health = ("UNHEALTHY", "HEALTHY")[integer(values["H"], 1, "H")]
        raw_soh = integer(values["SH"], 100, "SH")
        soh = raw_soh / 100 if raw_soh else None
    return [resting, resistance, temperature + 273.15 if temperature else None,
            "REPORTED" if temperature else "ZERO_OR_UNAVAILABLE", health, soh]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def quantity(value, name, *, missing=False):
    # The documented ROS JSON export uses string NaN for an unavailable float.
    if missing and value == "NaN":
        return None
    if type(value) not in (int, float):
        raise ValueError(f"invalid numeric type: {name}")
    return number(value, name)


def json_time(record, source_format):
    if source_format in ("farmbot-state", "mavsdk-battery-json", DJI_FORMAT, ROBOTNIK_FORMAT, DOCKER_FORMAT, TEGRA_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, FARM_POINT_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT):
        time = record["capture_time_us"]
        if type(time) is not int or not 0 <= time <= 2**63 - 1:
            raise ValueError("explicit recorder microsecond timestamp required")
        return time
    if source_format == "ros-battery-json":
        stamp = record["header"]["stamp"]
        sec, ns = stamp["sec"], stamp["nanosec"]
        if type(sec) is not int or type(ns) is not int or not 0 <= sec < 2**31 or not 0 <= ns < 10**9:
            raise ValueError("unsupported ROS stamp")
        if not isinstance(record["header"]["frame_id"], str):
            raise ValueError("invalid frame_id")
        return sec * 10**6 + ns // 1000
    value = record["timestamp"]
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", value):
        raise ValueError("unsupported VDA timestamp")
    # Explicitly truncate sub-microsecond digits on Python versions with stricter
    # fromisoformat; the exact nanosecond text remains in source_record_hex.
    parsed = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    if value[19:20] == ".":
        parsed = parsed.replace(microsecond=int((value[20:-1] + "000000")[:6]))
    elapsed = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
    us = (elapsed.days * 86400 + elapsed.seconds) * 10**6 + elapsed.microseconds
    if not 0 <= us <= 2**63 - 1:
        raise ValueError("unsupported VDA timestamp range")
    return us


def json_quantities(record, source_format, *, allow_missing_voltage=False):
    """Selected source semantics only; this is not whole-message schema validation."""
    stored_charge = None
    if source_format == "ros-battery-json":
        voltage = quantity(record["voltage"], "voltage", missing=allow_missing_voltage)
        current = quantity(record["current"], "current", missing=True)
        stored_charge = quantity(record["charge"], "charge", missing=True)
        remaining = quantity(record["percentage"], "percentage", missing=True)
        status = record["power_supply_status"]
        if type(status) is not int or not 0 <= status <= 255 or type(record["present"]) is not bool:
            raise ValueError("invalid ROS status/presence")
        # Absence must not masquerade as a measured battery with zero quantities.
        if not record["present"]:
            voltage = current = stored_charge = remaining = None
        state = {0: "UNKNOWN", 1: "CHARGING", 2: "DISCHARGING", 3: "NOT_CHARGING", 4: "FULL"}.get(status, f"UNKNOWN_{status}")
    else:
        expected = {"vda-state-2.0": "2.0.0", "vda-state-2.1": "2.1.0", "vda-state-3.0": "3.0.0"}[source_format]
        if record["version"] != expected:
            raise ValueError("wrong VDA version")
        for field in ("manufacturer", "serialNumber"):
            if not isinstance(record[field], str) or not record[field]:
                raise ValueError("invalid VDA identity")
        old = source_format in ("vda-state-2.0", "vda-state-2.1")
        battery = record["batteryState" if old else "powerSupply"]
        remaining = quantity(battery["batteryCharge" if old else "stateOfCharge"], "state of charge") / 100
        voltage = quantity(battery["batteryVoltage"], "voltage") if "batteryVoltage" in battery else None
        current = quantity(battery["batteryCurrent"], "current") if not old and "batteryCurrent" in battery else None
        if type(battery["charging"]) is not bool:
            raise ValueError("invalid charging state")
        state = "CHARGING" if battery["charging"] else "NOT_CHARGING"
    if ((voltage is not None and voltage < 0) or (stored_charge is not None and stored_charge < 0)
            or (remaining is not None and not 0 <= remaining <= 1)):
        raise ValueError("invalid electrical range")
    values = [voltage, current, None, None,
              None if voltage is None or current is None else voltage * current, remaining]
    if not all(v is None or math.isfinite(v) for v in values):
        raise ValueError("normalized value overflow")
    # Sign convention belongs to the selected schema, not the reported status.
    # Never infer VDA polarity from its charging boolean or change native values.
    basis = "NEGATIVE_DISCHARGING" if source_format == "ros-battery-json" else "UNSPECIFIED"
    result = values + [stored_charge, state, basis]
    if allow_missing_voltage:
        if source_format != "ros-battery-json":
            raise ValueError("missing voltage policy requires ROS BatteryState")
        result.append("BATTERY_NOT_PRESENT" if not record["present"] else
                      "UNAVAILABLE_SOURCE_NAN" if record["voltage"] == "NaN" else "REPORTED")
    return result


def vda_reported_state(record, source_format):
    """Finite state reports, not actuation, physical certification or full schema validation."""
    old = source_format in ("vda-state-2.0", "vda-state-2.1")
    result = dict.fromkeys(VDA_FIELDS)

    def boolean(value):
        if type(value) is not bool:
            raise ValueError("invalid reported boolean")
        return int(value)

    def enum(value, known):
        if not isinstance(value, str) or not value:
            raise ValueError("invalid reported enum")
        return value if value in known else "UNKNOWN"

    if "operatingMode" in record:
        known = {"AUTOMATIC", "SEMIAUTOMATIC", "MANUAL", "SERVICE", "TEACHIN"} if old else {
            "STARTUP", "AUTOMATIC", "SEMIAUTOMATIC", "INTERVENED", "MANUAL", "SERVICE", "TEACH_IN"}
        mode = enum(record["operatingMode"], known)
        result["vda_operating_mode_reported"] = "TEACH_IN" if mode == "TEACHIN" else mode
    for name in ("driving", "paused"):
        if name in record:
            result[f"vda_{name}_reported"] = boolean(record[name])
    if "safetyState" in record:
        state = record["safetyState"]
        stop = enum(state["eStop" if old else "activeEmergencyStop"],
                    {"AUTOACK", "MANUAL", "REMOTE", "NONE"} if old else {"MANUAL", "REMOTE", "NONE"})
        result["vda_emergency_stop_reported"] = stop
        result["vda_protective_field_violation_reported"] = boolean(state["fieldViolation"])
    if "velocity" in record:
        velocity = record["velocity"]
        if type(velocity) is not dict:
            raise ValueError("invalid velocity object")
        for source, destination in zip(("vx", "vy", "omega"), VDA_FIELDS[5:8]):
            if source in velocity:
                result[destination] = quantity(velocity[source], source)
    position_key = "agvPosition" if old else "mobileRobotPosition"
    if ("mobileRobotPosition" if old else "agvPosition") in record:
        raise ValueError("position property does not match selected VDA version")
    result["vda_position_state"] = "NOT_REPORTED"
    if position_key in record:
        position = record[position_key]
        if type(position) is not dict or not isinstance(position.get("mapId"), str) or not position["mapId"]:
            raise ValueError("invalid reported map position")
        state_key = "positionInitialized" if old else "localized"
        state = boolean(position[state_key])
        if ("localized" if old else "positionInitialized") in position:
            raise ValueError("position state does not match VDA version")
        result["vda_position_initialized_reported" if old else "vda_localized_reported"] = state
        result["vda_map_id_hex"] = "hex:" + position["mapId"].encode().hex()
        x, y, theta = [quantity(position[key], key) for key in ("x", "y", "theta")]
        if not -3.14159265359 <= theta <= 3.14159265359:
            raise ValueError("invalid reported orientation")
        score = None
        if "localizationScore" in position:
            score = quantity(position["localizationScore"], "localizationScore")
            if not 0 <= score <= 1:
                raise ValueError("invalid reported localization score")
            result["vda_localization_score_reported"] = score
        if "deviationRange" in position:
            deviation = quantity(position["deviationRange"], "deviationRange")
            if deviation < 0:
                raise ValueError("invalid reported position deviation")
            result["vda_deviation_range_m"] = deviation
        usable = bool(state) and score != 0
        result["vda_position_state"] = (("INITIALIZED_REPORTED" if old else "LOCALIZED_REPORTED")
                                        if usable else "UNAVAILABLE_POSITION_REPORTED")
        if usable:
            result.update(vda_map_x_m=x, vda_map_y_m=y, vda_map_theta_rad=theta)
    for source, destination in (("orderId", "vda_order_id_hex"), ("lastNodeId", "vda_last_node_id_hex")):
        if source in record:
            if not isinstance(record[source], str):
                raise ValueError("invalid reported identifier")
            result[destination] = "hex:" + record[source].encode("utf-8").hex()
    for source, destination in (("orderUpdateId", "vda_order_update_id"), ("lastNodeSequenceId", "vda_last_node_sequence")):
        if source in record:
            if type(record[source]) is not int or not 0 <= record[source] <= 2**32 - 1:
                raise ValueError("invalid reported sequence")
            result[destination] = record[source]
    for source, field, prefix, choices in (("actionStates", "actionStatus", "action", ACTION_STATES),
                                          ("errors", "errorLevel", "error", ERROR_LEVELS)):
        if source not in record:
            continue  # Missing is not an empty, successful report.
        if type(record[source]) is not list:
            raise ValueError("invalid reported state list")
        known = set(choices) - {"UNKNOWN"}
        if old:
            # The 2.0/2.1 prose actionStatus table defines PAUSED despite its schema omission.
            known -= {"RETRIABLE", "URGENT", "CRITICAL"}
        for choice in choices:
            result[f"vda_{prefix}_{choice.lower()}_count"] = 0
        for item in record[source]:
            identifier = item["actionId" if prefix == "action" else "errorType"]
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("invalid action/error identifier")
            state = enum(item[field], known)
            result[f"vda_{prefix}_{state.lower()}_count"] += 1
    return [result[name] for name in VDA_FIELDS]


def farmbot_point(record):
    if record["exporter_version"] != "v15.30.5" or record["coordinate_unit"] != "mm":
        raise ValueError("unqualified FarmBot point schema or unit")
    point = record["point"]
    identifiers = [point[key] for key in ("id", "device_id")]
    if any(type(value) is not int or not 0 < value < 2**63 for value in identifiers):
        raise ValueError("invalid point resource identity")
    kind = point["pointer_type"]
    if kind not in ("GenericPointer", "Plant", "ToolSlot", "Weed"):
        raise ValueError("unqualified point type")
    coordinates = []
    for axis in "xyz":
        value = point[axis]
        if type(value) not in (int, float) or not math.isfinite(value) or value >= 50000:
            raise ValueError("invalid saved point coordinate")
        coordinates.append(normalize_unit(value, "mm")[1])
    return ["id:" + str(value) for value in identifiers] + [kind] + coordinates + ["SAVED_MACHINE_AXIS_POINT_NOT_ACTUAL_POSITION"]


def farmbot_reported_state(record):
    settings = record["payload"].get("informational_settings", {})
    if not isinstance(settings, dict):
        raise ValueError("invalid FarmBot informational settings")
    result = dict.fromkeys(FARM_FIELDS)
    for source, target in (("uptime", "runtime_uptime_s"), ("soc_temp", "soc_temperature_k")):
        value = settings.get(source)
        if value is not None:
            if type(value) is not int or not -273 <= value <= 2**53 - 1 or (source == "uptime" and value < 0):
                raise ValueError("invalid reported uptime/temperature")
            result[target] = value if source == "uptime" else value + 273.15
    for key in ("busy", "idle", "locked"):
        value = settings.get(key)
        if value is not None:
            if type(value) is not bool:
                raise ValueError("invalid FarmBot reported boolean")
            result[f"{key}_reported"] = int(value)
    for key in FARM_IDS:
        value = settings.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("invalid declared identity")
            result[f"{key}_hex"] = "hex:" + value.encode("utf-8").hex()
    location = record["payload"].get("location_data", {})
    if not isinstance(location, dict):
        raise ValueError("invalid FarmBot location data")
    for source, target, unit in (("position", "position", "m"),
                                  ("scaled_encoders", "scaled_encoder", "m"),
                                  ("raw_encoders", "raw_encoder", "edges")):
        vector = location.get(source)
        if vector is None:
            continue
        if not isinstance(vector, dict):
            raise ValueError("invalid FarmBot location vector")
        for axis in "xyz":
            value = vector.get(axis)
            if value is None:
                continue
            if type(value) not in (int, float):
                raise ValueError("invalid FarmBot location scalar")
            numeric = number(value, "FarmBot location scalar")
            if unit == "edges" and type(value) is int and not -(2**63) <= value < 2**63:
                raise ValueError("raw encoder integer exceeds common signed range")
            result[f"farm_{target}_{axis}_{unit}"] = numeric / 1000 if unit == "m" else value
    if all(value is None for value in result.values()):
        raise ValueError("no selected reported state")
    return [result[key] for key in FARM_FIELDS]


def dji_batteries(record):
    battery = record["payload"]["data"]["battery"]
    entries = battery["batteries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        raise ValueError("bounded battery array required")

    def integer(value, name, high=2**63 - 1):
        if value is None: return None
        if type(value) is not int or not 0 <= value <= high:
            raise ValueError("invalid declared battery integer: " + name)
        return value

    def text(value):
        if value is None: return None
        if not isinstance(value, str): raise ValueError("invalid battery identity string")
        return "hex:" + value.encode().hex()

    total = integer(battery.get("capacity_percent"), "total percent", 100)
    duration = integer(battery.get("remain_flight_time"), "remaining seconds")
    rows, identifiers = [], set()
    for entry in entries:
        if not isinstance(entry, dict): raise ValueError("battery entry must be an object")
        identifier = integer(entry["index"], "index")
        if identifier is None or identifier in identifiers:
            raise ValueError("missing or duplicate battery index")
        identifiers.add(identifier)
        voltage = integer(entry.get("voltage"), "mV")
        remaining = integer(entry.get("capacity_percent"), "percent", 100)
        temperature = entry.get("temperature")
        if temperature is not None:
            temperature = normalize_unit(quantity(temperature, "temperature"), "degC")[1]
        if voltage is None and remaining is None and temperature is None:
            raise ValueError("no selected battery quantity")
        rows.append([None if voltage is None else normalize_unit(voltage, "mV")[1],
            None, None, None, None, None if remaining is None else normalize_unit(remaining, "%")[1],
            None, "UNKNOWN", "UNSPECIFIED", identifier, temperature,
            None if total is None else normalize_unit(total, "%")[1], duration,
            integer(entry.get("loop_times"), "cycles"), text(entry.get("sn")),
            text(entry.get("firmware_version")), len(entries)])
    return rows


def mavsdk_protobuf(record, *, grpc=False):
    """Fixed standalone Battery descriptor; no gRPC or runtime schema loading."""
    if record["proto_commit"] != "d3cb35f49d1379d2d95a6d22cdbe40ff4074e9bf":
        raise ValueError("unqualified protobuf schema")
    try:
        import google.protobuf
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, unknown_fields
        from google.protobuf.message import DecodeError
    except ImportError as exc:
        raise ValueError("install pinned optional protobuf decoder") from exc
    if google.protobuf.__version__ != "6.33.5":
        raise ValueError("pinned protobuf decoder required")
    wire = record["grpc_frame_hex" if grpc else "battery_protobuf_hex"]
    if not isinstance(wire, str) or len(wire) > 28000 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", wire):
        raise ValueError("bounded complete protobuf hex required")
    payload = bytes.fromhex(wire)
    if grpc:
        if len(payload) < 5 or payload[0] != 0 or int.from_bytes(payload[1:5], "big") != len(payload) - 5:
            raise ValueError("one complete noncompressed gRPC message required")
        payload = payload[5:]
    schema = descriptor_pb2.FileDescriptorProto(name="selected_battery.proto", package="selected", syntax="proto3")
    enum = schema.enum_type.add(name="BatteryFunction")
    for number, name in enumerate(("UNKNOWN", "ALL", "PROPULSION", "AVIONICS", "PAYLOAD")):
        enum.value.add(name="BATTERY_FUNCTION_" + name, number=number)
    descriptor = schema.message_type.add(name="Battery")
    names = ("id", "temperatureDegc", "voltageV", "currentBatteryA", "capacityConsumedAh",
             "remainingPercent", "timeRemainingS", "batteryFunction")
    for number, name in enumerate(names, 1):
        field = descriptor.field.add(name=name, number=number, label=1,
            type=13 if number == 1 else 14 if number == 8 else 2)
        if number == 8:
            field.type_name = ".selected.BatteryFunction"
    if grpc:
        response = schema.message_type.add(name="BatteryResponse")
        response.field.add(name="battery", number=1, label=1, type=11, type_name=".selected.Battery")
    pool = descriptor_pool.DescriptorPool(); pool.Add(schema)
    message = message_factory.GetMessageClass(pool.FindMessageTypeByName(
        "selected.BatteryResponse" if grpc else "selected.Battery"))()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError("malformed protobuf message") from exc
    outer_unknown = 0
    if grpc:
        if not message.HasField("battery"):
            raise ValueError("BatteryResponse has no selected Battery")
        outer_unknown = len(unknown_fields.UnknownFieldSet(message))
        message = message.battery
    present = {field.name for field, _ in message.ListFields()}
    values = {name: getattr(message, name) for name in names}
    values = {name: "NaN" if isinstance(value, float) and math.isnan(value) else value
              for name, value in values.items()}
    return mavsdk_battery(dict(sdk_version=record["sdk_version"], battery=values)) + [
        "hex:" + ",".join(name for name in names if name not in present).encode().hex(),
        outer_unknown + len(unknown_fields.UnknownFieldSet(message)),
        "GRPC_MESSAGE_NOT_RPC_SUCCESS" if grpc else "PROTO3_DECODED_NOT_SENSOR_VALIDATED"]


def mavsdk_battery(record):
    if record["sdk_version"] != "3.17.4":
        raise ValueError("unqualified MAVSDK version")
    battery = record["battery"]
    if any(name in battery for name in ("voltage_v", "current_battery_a", "capacity_consumed_ah",
            "remaining_percent", "temperature_degc", "time_remaining_s", "battery_function")):
        raise ValueError("mixed or unselected protobuf field spelling")
    identifier = battery["id"]
    if type(identifier) is not int or not 0 <= identifier <= 2**32 - 1:
        raise ValueError("invalid reported battery identifier")
    voltage, current, consumed, remaining, temperature, duration = [
        quantity(battery[name], name, missing=True) for name in
        ("voltageV", "currentBatteryA", "capacityConsumedAh", "remainingPercent", "temperatureDegc", "timeRemainingS")]
    if any(value is not None and abs(value) > 3.4028234663852886e38
           for value in (voltage, current, consumed, remaining, temperature, duration)):
        raise ValueError("reported value exceeds protobuf float32 range")
    # The pinned implementation leaves MAVLink's -1 remaining sentinel unchanged.
    if remaining == -1:
        remaining = None
    if ((voltage is not None and voltage < 0) or (consumed is not None and consumed < 0)
            or (remaining is not None and not 0 <= remaining <= 100)
            or (temperature is not None and temperature < -273.15)
            or (duration is not None and duration < 0)):
        raise ValueError("invalid MAVSDK reported electrical range")
    function = battery["batteryFunction"]
    names = ("BATTERY_FUNCTION_UNKNOWN", "BATTERY_FUNCTION_ALL", "BATTERY_FUNCTION_PROPULSION",
             "BATTERY_FUNCTION_AVIONICS", "BATTERY_FUNCTION_PAYLOAD")
    if type(function) is int and -(2**31) <= function < 2**31:
        function = names[function] if 0 <= function < len(names) else f"UNKNOWN_{function}"
    elif function not in names:
        raise ValueError("invalid protobuf battery function")
    power = None if voltage is None or current is None else voltage * current
    if power is not None and not math.isfinite(power):
        raise ValueError("reported power overflows")
    return [voltage, current, consumed, None, power,
            None if remaining is None else remaining / 100,
            None, "UNKNOWN", "UNSPECIFIED", identifier,
            None if temperature is None else temperature + 273.15, duration, function]


VDA_ACTION_FIELDS = ("vda_record_kind", "vda_source_record_index", "vda_source_sha256",
                     "vda_action_index", "vda_action_id_hex", "vda_action_status_reported",
                     "vda_action_status_raw_hex", "vda_action_type_hex",
                     "vda_action_description_hex", "vda_action_result_hex")
VDA_ERROR_FIELDS = ("vda_error_index", "vda_error_type_hex", "vda_error_level_reported",
                    "vda_error_level_raw_hex", "vda_error_description_hex", "vda_error_hint_hex",
                    "vda_error_reference_count", "vda_error_reference_index",
                    "vda_error_reference_key_hex", "vda_error_reference_value_hex")


def vda_error_rows(record, source_format):
    known = {"WARNING", "FATAL"} if source_format == "vda-state-2.1" else set(ERROR_LEVELS) - {"UNKNOWN"}
    def text(value):
        if not isinstance(value, str):
            raise ValueError("invalid error detail text")
        return "hex:" + value.encode().hex()
    for index, error in enumerate(record.get("errors", [])):
        level = error["errorLevel"]  # Summary validated required type/level strings.
        references = error.get("errorReferences", [])
        if not isinstance(references, list):
            raise ValueError("invalid error reference list")
        base = [index, text(error["errorType"]), level if level in known else "UNKNOWN", text(level),
                text(error["errorDescription"]) if "errorDescription" in error else None,
                text(error["errorHint"]) if "errorHint" in error else None,
                len(references) if "errorReferences" in error else None]
        yield "ERROR", base + [None] * 3
        for reference_index, reference in enumerate(references):
            if not isinstance(reference, dict) or not {"referenceKey", "referenceValue"} <= reference.keys():
                raise ValueError("invalid error reference")
            yield "ERROR_REFERENCE", base + [reference_index, text(reference["referenceKey"]), text(reference["referenceValue"])]


def vda_action_rows(record, source_format):
    old = source_format != "vda-state-3.0"
    known = set(ACTION_STATES) - {"UNKNOWN"} - ({"RETRIABLE"} if old else set())
    seen = set()
    for index, action in enumerate(record.get("actionStates", [])):
        identifier = action["actionId"]  # Same required fields already checked by summary.
        if identifier in seen:
            raise ValueError("duplicate action identifier in one state")
        seen.add(identifier)
        status = action["actionStatus"]  # Summary has already required a string.
        values = [index, "hex:" + identifier.encode().hex(), status if status in known else "UNKNOWN",
                  "hex:" + action["actionStatus"].encode().hex()]
        for key in ("actionType", "actionDescription" if old else "actionDescriptor",
                    "resultDescription" if old else "actionResult"):
            value = action.get(key)
            if key in action and not isinstance(value, str):
                raise ValueError("invalid reported action text")
            values.append(None if value is None else "hex:" + value.encode().hex())
        # Version-incompatible aliases stay in the exact source, never masquerade as results.
        yield values


def robotnik_battery(record):
    if record["schema_commit"] not in ("5c44131ba354962322e969a4b0fe0b3026659686", "012c20a589c0c180dd792f41d46bd84b7391b8ed"):
        raise ValueError("unqualified BatteryStatus schema")
    battery = record["battery"]
    voltage, current, level = [quantity(battery[key], key) for key in ("voltage", "current", "level")]
    if voltage < 0 or not 0 <= level <= 100 or max(abs(voltage), abs(current)) > 3.4028234663852886e38:
        raise ValueError("invalid reported BatteryStatus float32 range")
    times = [battery[key] for key in ("time_remaining", "time_charging")]
    if any(type(value) is not int or not 0 <= value < 2**32 for value in times) or type(battery["is_charging"]) is not bool:
        raise ValueError("invalid duration or connected flag")
    cells = battery["cell_voltages"]
    if not isinstance(cells, list):
        raise ValueError("cell voltage array required")
    cells = [quantity(cell, "cell voltage") for cell in cells]
    if any(not 0 <= cell <= 3.4028234663852886e38 for cell in cells):
        raise ValueError("invalid cell voltage")
    # is_charging is documented as connected, not a charge-flow measurement.
    rows = [[voltage, current, None, None, voltage * current, level / 100,
             None, "UNKNOWN", "UNSPECIFIED", "SUMMARY", times[0] * 60, times[1] * 60,
             int(battery["is_charging"]), len(cells), None, None]]
    for index, cell in enumerate(cells):
        rows.append([None] * len(JSON_ELECTRICAL_FIELDS) + ["CELL", None, None, None, len(cells), index, cell])
    return rows


def factsheet_quantities(record):
    """Declared operating quantities, never measured motion or certified capacity."""
    if record["version"] != "3.0.0":
        raise ValueError("wrong factsheet version")
    identity = [record[key] for key in ("manufacturer", "serialNumber")]
    if any(not isinstance(value, str) or not value for value in identity):
        raise ValueError("invalid factsheet identity")
    physical = record["physicalParameters"]
    values = {}
    for index, (key, _) in enumerate(FACTSHEET_QUANTITIES):
        if index >= 8 and key not in physical:
            values[key] = None
            continue
        value = quantity(physical[key], key)
        if key != "maximumDeceleration" and value < 0:
            raise ValueError("negative declared physical quantity")
        values[key] = value
    for lower, upper in (("minimumSpeed", "maximumSpeed"), ("minimumHeight", "maximumHeight"),
                         ("minimumAngularSpeed", "maximumAngularSpeed")):
        if values[lower] is not None and values[upper] is not None and values[lower] > values[upper]:
            raise ValueError("inverted declared physical bounds")
    mass = quantity(record["typeSpecification"]["maximumLoadMass"], "maximum load mass")
    if mass < 0:
        raise ValueError("negative declared load mass")
    return ["DECLARED_NOT_MEASURED_OR_CERTIFIED", *["hex:" + value.encode().hex() for value in identity],
            *[values[key] for key, _ in FACTSHEET_QUANTITIES], mass]


def docker_quantities(record):
    """Parse rounded CLI display quantities, not raw daemon counters or health."""
    if record["exporter_version"] != "27.3.1" or record["os_type"] != "linux":
        raise ValueError("unqualified Docker display layout")
    stats = record["stats"]
    identifier = stats["ID"]
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("missing reported container identifier")

    def percent(value):
        if value == "--":
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]{2}%", value):
            raise ValueError("invalid displayed percentage")
        return number(value[:-1], "displayed percentage") / 100

    def pair(value, binary):
        if value in ("-- / --", "--"):
            return [None, None]
        if not isinstance(value, str) or len(value.split(" / ")) != 2:
            raise ValueError("invalid displayed quantity pair")
        units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB") if binary else (
            "B", "kB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        result = []
        for part in value.split(" / "):
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)([A-Za-z]+)", part)
            if match is None or match[2] not in units:
                raise ValueError("invalid displayed byte unit")
            result.append(number(number(match[1], "display size") * (1024 if binary else 1000) ** units.index(match[2]), "display bytes"))
        return result

    pids = stats["PIDs"]
    if pids != "--":
        if not isinstance(pids, str):
            raise ValueError("invalid displayed PID count")
        integer(pids, 2**64 - 1, "PID count")
    return ["ROUNDED_DISPLAY_NOT_RAW_COUNTER_MISSING_IS_UNAVAILABLE", "hex:" + identifier.encode().hex(),
        percent(stats["CPUPerc"]), percent(stats["MemPerc"]), *pair(stats["MemUsage"], True),
        *pair(stats["NetIO"], False), *pair(stats["BlockIO"], False),
        "UNAVAILABLE" if pids == "--" else "u64:" + pids]


def farmduino_position(record):
    """Receive a saved report only; declared scale is not a calibration proof."""
    if record["firmware_commit"] != "52053ec86e6cef1a39340439dfdfaf9cb2337a60":
        raise ValueError("unqualified Farmduino response writer")
    scale = record["steps_per_mm"]
    if not isinstance(scale, dict) or any(type(scale.get(axis)) is not int or not 0 < scale[axis] < 2**31 for axis in "xyz"):
        raise ValueError("positive declared steps per mm required for each axis")
    response = record["response"]
    if not isinstance(response, str):
        raise ValueError("saved R82 text required")
    decimal = r"(-?[0-9]+\.[0-9]{2})"
    match = re.fullmatch(r"R82 X" + decimal + r" Y" + decimal + r" Z" + decimal + r" Q(-?[0-9]+)(?:\r\n)?", response)
    if not match:
        raise ValueError("not a complete qualified R82 response")
    q = int(match[4])
    if not -(2**31) <= q < 2**31:
        raise ValueError("R82 Q exceeds signed long scope")
    position = {axis: number(match[index], "reported mm") for index, axis in enumerate("xyz", 1)}
    # Same position field/scale as the already adopted FarmBot state export.
    values = farmbot_reported_state({"payload": {"location_data": {"position": position}}})
    return values + ["i32:" + str(q), "REPORTED_MM_WITH_DECLARED_POSITIVE_SCALE_NOT_CALIBRATION"]


def tegrastats_quantities(record, source_sha):
    """Only qualified CPU/thermal tokens; the remaining report stays in raw source."""
    if record["exporter_version"] != "l4t-36.4.3":
        raise ValueError("unqualified tegrastats exporter")
    report = record["report"]
    if not isinstance(report, str) or not report or any(c in report for c in "\r\n\x00"):
        raise ValueError("one saved tegrastats line required")
    rows = []
    cpu = list(re.finditer(r"(?<!\S)CPU \[([^\]]+)\](?:@([0-9]+))?(?=\s|$)", report))
    if len(cpu) > 1 or len(re.findall(r"(?<!\S)CPU(?=\s|$)", report)) != len(cpu):
        raise ValueError("malformed or duplicate CPU group")
    if cpu:
        match = cpu[0]
        shared = match[2]
        for index, token in enumerate(match[1].split(",")):
            token = token.strip()
            if token == "off":
                rows.append(["CPU", index, None, None, None, None, "OFF_REPORTED", source_sha])
                continue
            parsed = re.fullmatch(r"([0-9]+)%" if shared else r"([0-9]+)%@([0-9]+)", token)
            if not parsed or int(parsed[1]) > 100:
                raise ValueError("invalid CPU utilization/frequency token")
            mhz = int(shared if shared else parsed[2])
            if not 0 < mhz <= (2**53 - 1) // 1000000:
                raise ValueError("CPU frequency outside exact Hz range")
            rows.append(["CPU", index, None, int(parsed[1]) / 100, mhz * 1000000,
                         None, "REPORTED_APPROXIMATE_UTILIZATION", source_sha])
    blocks = set()
    for token in report.split():
        if "@" not in token or not token.endswith("C"):
            continue
        parsed = re.fullmatch(r"([A-Za-z0-9_-]+)@(-?[0-9]+(?:\.[0-9]+)?)C", token)
        if not parsed or parsed[1] in blocks:
            raise ValueError("malformed or duplicate temperature block")
        blocks.add(parsed[1])
        temperature = normalize_unit(float(parsed[2]), "degC")[1]
        rows.append(["TEMPERATURE", None, "hex:" + parsed[1].encode().hex(),
                     None, None, temperature, "REPORTED_UNCALIBRATED", source_sha])
    if not rows:
        raise ValueError("no selected tegrastats CPU or temperature reports")
    return rows


def rmf_web_quantities(record, source_sha):
    """Saved web fleet status; no ROS-mode, map-coordinate or live-health inference."""
    if record["schema_commit"] != "a736ddebeee9eaf3970756d7dd452065273da918":
        raise ValueError("unqualified RMF Web schema")
    state = record["state"]
    def encoded(value, required=False):
        if value is None and not required:
            return None
        if not isinstance(value, str) or (required and not value):
            raise ValueError("invalid RMF Web reported identifier")
        return "hex:" + value.encode("utf-8").hex()
    fleet = encoded(state["name"], True)
    robots = state["robots"]
    if not isinstance(robots, dict) or not robots:
        raise ValueError("nonempty saved fleet robot dictionary required")
    rows = []
    known = {"uninitialized", "offline", "shutdown", "idle", "charging", "working", "error"}
    for key, robot in robots.items():
        if not isinstance(robot, dict):
            raise ValueError("invalid saved robot object")
        fraction = robot.get("battery")
        if fraction is not None:
            fraction = quantity(fraction, "RMF Web battery fraction")
            if not 0 <= fraction <= 1:
                raise ValueError("RMF Web battery must be a fraction, not percent")
        status = robot.get("status")
        status_hex = encoded(status)
        stamp = robot.get("unix_millis_time")
        if stamp is not None and (type(stamp) is not int or not 0 <= stamp < 2**63):
            raise ValueError("invalid reported Unix millisecond integer")
        rows.append([fleet, encoded(key, True), encoded(robot.get("name")),
            encoded(robot.get("task_id")), fraction,
            "UNAVAILABLE" if fraction is None else "REPORTED",
            status_hex, "UNAVAILABLE" if status is None else
                "SCHEMA_KNOWN_REPORTED" if status in known else "UNKNOWN_RETAINED",
            None if stamp is None else "unix_ms:" + str(stamp), source_sha])
    return rows


def rmf_quantities(record):
    """Saved RobotState subset, not a fleet adapter or command acknowledgement."""
    if record["schema_commit"] != "6990fc7418c412234b4ff64f01560d8bf3047f46":
        raise ValueError("unqualified RMF RobotState schema")
    state = record["state"]
    def unsigned(value, bits):
        if type(value) is not int or not 0 <= value < 2**bits:
            raise ValueError("invalid RMF unsigned integer")
        return value
    def encoded(value, nonempty=False):
        if not isinstance(value, str) or (nonempty and not value):
            raise ValueError("invalid RMF reported identifier")
        return "hex:" + value.encode("utf-8").hex()
    percent = quantity(state["battery_percent"], "battery_percent")
    if not 0 <= percent <= 100:
        raise ValueError("RMF battery percent outside reported range")
    mode = state["mode"]
    number = unsigned(mode["mode"], 32)
    return [percent / 100, encoded(state["name"], True), encoded(state["model"]), encoded(state["task_id"]),
            "u64:" + str(unsigned(state["seq"], 64)), number,
            "MODE_" + RMF_MODES[number] if number < len(RMF_MODES) else "UNKNOWN",
            "u64:" + str(unsigned(mode["mode_request_id"], 64)), encoded(mode["performing_action"])]


def ping_quantities(record):
    if record["protocol_commit"] != "1746cd03f942d58bcf08253055854caea2e33fda":
        raise ValueError("unqualified Ping definitions")
    def uint(value, bits):
        if type(value) is not int or not 0 <= value < 2**bits:
            raise ValueError("Ping field outside declared integer type")
        return value
    message = uint(record["message_id"], 16)
    if message not in (1211, 1212):
        raise ValueError("only decoded Ping distance responses are selected")
    source = uint(record["src_device_id"], 8)
    payload = record["payload"]
    distance = normalize_unit(uint(payload["distance"], 32), "mm")[1]
    confidence = uint(payload["confidence"], 8 if message == 1211 else 16)
    result = [distance, confidence, normalize_unit(confidence, "%")[1] if confidence <= 100 else None,
              "REPORTED_UNCALIBRATED" if confidence <= 100 else "OUTSIDE_PERCENT_RANGE", message, source]
    if message == 1211:
        return result + [None] * 5
    return result + [uint(payload["transmit_duration"], 16) / 1000000,
        uint(payload["ping_number"], 32), normalize_unit(uint(payload["scan_start"], 32), "mm")[1],
        normalize_unit(uint(payload["scan_length"], 32), "mm")[1], uint(payload["gain_setting"], 32)]


def msdk_battery(record):
    if (record.get("sdk_version") != "5.18.0" or
            record.get("sdk_source_commit") != "07d37cfdff865cdda9d523b00b723c9984575f8f"):
        raise ValueError("exact selected Mobile SDK key schema required")
    key, index, value = record.get("key"), record.get("component_index"), record["value"]
    mappings = {"Voltage": ("mV", "battery_voltage_v"), "Current": ("mA", "battery_current_a"),
        "BatteryTemperature": ("degC", "battery_temperature_k"),
        "FullChargeCapacity": ("mAh", "battery_full_charge_ah"),
        "ChargeRemaining": ("mAh", "battery_remaining_charge_ah"),
        "ChargeRemainingInPercent": (None, "battery_remaining_percent_code"),
        "Connection": (None, "battery_connection_reported")}
    if not isinstance(key, str) or key not in mappings or type(index) is not int or not 0 <= index < 2**31:
        raise ValueError("selected key and declared component index required")
    result = dict.fromkeys(MSDK_FIELDS)
    result.update(sdk_key_reported=key, sdk_component_index=index,
                  sdk_value_disposition="UNAVAILABLE_NOT_ZERO" if value is None else "REPORTED_NOT_PHYSICALLY_VERIFIED")
    if value is not None:
        unit, field = mappings[key]
        if key == "Connection":
            if type(value) is not bool: raise ValueError("connection result must be Boolean")
            value = "TRUE" if value else "FALSE"
        elif key == "BatteryTemperature":
            value = quantity(value, key)
            if not -128 <= value <= 127: raise ValueError("temperature outside SDK range")
        elif type(value) is not int or not (-2**31 if key == "Current" else 0) <= value < 2**31:
            raise ValueError("key result outside selected Integer range")
        result[field] = value / 1000 if unit == "mAh" else normalize_unit(value, unit)[1] if unit else value
        if key == "Current": result["battery_current_sign_basis"] = "NEGATIVE_DISCHARGING"
        if key == "ChargeRemainingInPercent":
            if value <= 100: result["battery_remaining_fraction"] = normalize_unit(value, "%")[1]
            else: result["sdk_value_disposition"] = "OUTSIDE_PERCENT_RANGE"
    return [result[field] for field in MSDK_FIELDS]


def parrot_battery(record):
    if record.get("arsdk_xml_commit") != "2299c689be5951fa2104793e0154b0b5b1896c03":
        raise ValueError("exact selected Parrot event schema required")
    event, payload = record.get("event"), record.get("payload")
    mappings = {
        "voltage": (("voltage", 16, "mV", "battery_voltage_v"),),
        "temperature": (("temperature", 16, "K", "battery_temperature_k"),),
        "capacity": (("full_charge", 32, "mAh", "battery_full_charge_ah"),
                     ("remaining", 32, "mAh", "battery_remaining_charge_ah")),
        "cell_voltage": (("index", 16, None, "battery_cell_index"),
                         ("cell_voltage", 16, "mV", "battery_cell_voltage_v")),
        "cycle_count": (("count", 32, None, "battery_cycle_count"),),
        "health": (("state_of_health", 8, None, "battery_health_percent_code"),)}
    if not isinstance(event, str) or event not in mappings or not isinstance(payload, dict):
        raise ValueError("selected decoded battery event required")
    result = dict.fromkeys(PARROT_FIELDS)
    result["parrot_event_reported"] = event
    for name, bits, unit, field in mappings[event]:
        value = payload[name]
        if type(value) is not int or not 0 <= value < 2**bits:
            raise ValueError("event unsigned quantity outside schema")
        result[field] = value / 1000 if unit == "mAh" else normalize_unit(value, unit)[1] if unit else value
    if event == "health":
        code = result["battery_health_percent_code"]
        result["battery_state_of_health_fraction"] = normalize_unit(code, "%")[1] if code <= 100 else None
        result["battery_health_basis"] = "REPORTED_NOT_PHYSICALLY_VERIFIED" if code <= 100 else "OUTSIDE_PERCENT_RANGE"
    return [result[field] for field in PARROT_FIELDS]


def runtime_timing_values(event, source_format):
    if not isinstance(event, dict):
        raise ValueError("runtime event object required")
    if source_format == TRT_FORMAT:
        values = []
        for index, (key, _) in enumerate(TRT_TIMES):
            value = event.get(key)
            if type(value) not in (int, float):
                raise ValueError("numeric runtime milliseconds required")
            try:
                value = float(value)
            except OverflowError as exc:
                raise ValueError("runtime milliseconds overflow") from exc
            if not math.isfinite(value) or (index >= 8 and value < 0):
                raise ValueError("finite runtime milliseconds and nonnegative durations required")
            if value and value / 1000 == 0:
                raise ValueError("runtime seconds underflow")
            values.append(value / 1000)
        return values + ["REPORTED_MILLISECOND_OFFSETS_NO_CROSS_CLOCK_OR_STREAM_INFERENCE"]
    if event.get("ph") != "X" or not isinstance(event.get("args"), dict):
        raise ValueError("runtime complete event with args required")
    for key in ("cat", "name"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError("runtime category/name required")
    for key in ("ts", "dur", "pid", "tid"):
        value = event.get(key)
        bits = 32 if key in ("pid", "tid") else 64
        if type(value) is not int or not -(2**(bits - 1)) <= value < 2**(bits - 1):
            raise ValueError("runtime integer quantity outside range")
    if event["dur"] < 0:
        raise ValueError("negative reported runtime duration")
    return ["hex:" + event["cat"].encode().hex(),
        "KNOWN_REPORTED_CATEGORY" if event["cat"] in ("Session", "Node", "Kernel", "Api") else "UNKNOWN_RETAINED",
        "hex:" + event["name"].encode().hex(), *["i64:" + str(event[key]) for key in ("pid", "tid", "ts", "dur")],
        event["ts"] / 1000000, event["dur"] / 1000000,
        "PROFILE_RELATIVE_REPORTED_NOT_CAPTURE_OR_GPU_COMPLETION"]


def convert_runtime_profile(text, capture_time_us, source_format=ORT_FORMAT):
    if source_format not in (ORT_FORMAT, TRT_FORMAT):
        raise ValueError("explicit runtime timing writer required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit nonnegative signed64 capture microseconds required")
    raw = text.encode("utf-8")
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("runtime profile input exceeds bound")
    records = json.loads(text, object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
    if not isinstance(records, list) or not records:
        raise ValueError("nonempty runtime complete-event array required")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    columns = ["trt_" + field for _, field in TRT_TIMES] + ["runtime_time_basis"] if source_format == TRT_FORMAT else [
        "runtime_category_hex", "runtime_category_status", "runtime_name_hex",
        "runtime_pid_text", "runtime_tid_text", "runtime_start_us_text", "runtime_duration_us_text",
        "runtime_start_offset_s", "runtime_duration_s", "runtime_time_basis"]
    writer.writerow(["record_time_us", "source_document_sha256", "source_event_index", "canonical_event_hex"] + columns)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    for index, event in enumerate(records):
        values = runtime_timing_values(event, source_format)
        encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        writer.writerow([capture_time_us, digest, index, "hex:" + encoded.hex()] + values)
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("runtime profile output exceeds bound")
    return output.getvalue()


def convert_json(text, source_format, *, vda_actions=False, vda_errors=False, allow_missing_voltage=False):
    if len(text.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("JSON export too large; split at record boundaries")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    selected_fields = list(FARM_FIELDS) if source_format == "farmbot-state" else (
        list(JSON_ELECTRICAL_FIELDS)
        + (list(VDA_FIELDS) if source_format.startswith("vda-") else [])
        + (list(MAVSDK_FIELDS) if source_format in ("mavsdk-battery-json", PROTO_FORMAT, GRPC_FORMAT) else [])
        + (list(DJI_FIELDS) if source_format == DJI_FORMAT else [])
        + (list(ROBOTNIK_FIELDS) if source_format == ROBOTNIK_FORMAT else []))
    detailed = vda_actions or vda_errors
    if source_format == FACTSHEET_FORMAT:
        selected_fields = list(FACTSHEET_FIELDS)
    if source_format == DOCKER_FORMAT:
        selected_fields = list(DOCKER_FIELDS)
    if source_format == TEGRA_FORMAT:
        selected_fields = list(TEGRA_FIELDS)
    if source_format == RMF_FORMAT:
        selected_fields = list(RMF_FIELDS)
    if source_format == RMF_WEB_FORMAT:
        selected_fields = list(RMF_WEB_FIELDS)
    if source_format == FARMDUINO_FORMAT:
        selected_fields = list(FARM_FIELDS) + ["farmduino_q_reported_text", "farmduino_position_basis"]
    if source_format == FARM_POINT_FORMAT:
        selected_fields = list(FARM_POINT_FIELDS)
    if source_format == PING_FORMAT:
        selected_fields = list(PING_FIELDS)
    if source_format == PARROT_FORMAT:
        selected_fields = list(PARROT_FIELDS)
    if source_format == MSDK_FORMAT:
        selected_fields = list(MSDK_FIELDS)
    if source_format in (PROTO_FORMAT, GRPC_FORMAT):
        selected_fields += ["protobuf_zero_or_absent_fields_hex", "protobuf_unknown_fields", "protobuf_value_basis"]
    if allow_missing_voltage:
        if source_format != "ros-battery-json":
            raise ValueError("missing voltage policy requires ROS BatteryState")
        selected_fields += ["battery_voltage_disposition"]
    detail_fields = (list(VDA_ACTION_FIELDS[:3]) + (list(VDA_ACTION_FIELDS[3:]) if vda_actions else [])
                     + (list(VDA_ERROR_FIELDS) if vda_errors else [])) if detailed else []
    writer.writerow(["record_time_us", "source_record_hex"] + selected_fields + detail_fields)
    previous = None
    lines = text.splitlines()
    if not lines:
        raise ValueError("empty JSON export")
    for record_index, line in enumerate(lines):
        # Hex keeps exact unknown/nested source data, without CSV quoting ambiguity.
        if not line.strip() or len(line.encode("utf-8")) > (512 * 1024 if detailed or source_format in (FACTSHEET_FORMAT, RMF_WEB_FORMAT) else 30000):
            raise ValueError("empty or oversized JSON record")
        try:
            record = json.loads(line, object_pairs_hook=unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
            time = json_time(record, source_format)
            if source_format == MSDK_FORMAT:
                values = msdk_battery(record)
            elif source_format == PARROT_FORMAT:
                values = parrot_battery(record)
            elif source_format == PING_FORMAT:
                values = ping_quantities(record)
            elif source_format == FARMDUINO_FORMAT:
                values = farmduino_position(record)
            elif source_format == FARM_POINT_FORMAT:
                values = farmbot_point(record)
            elif source_format == RMF_FORMAT:
                values = rmf_quantities(record)
            elif source_format == RMF_WEB_FORMAT:
                values = rmf_web_quantities(record, "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest())
            elif source_format == DOCKER_FORMAT:
                values = docker_quantities(record)
            elif source_format == TEGRA_FORMAT:
                values = tegrastats_quantities(record, "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest())
            elif source_format == FACTSHEET_FORMAT:
                values = factsheet_quantities(record)
            elif source_format == DJI_FORMAT:
                values = dji_batteries(record)
            elif source_format == ROBOTNIK_FORMAT:
                values = robotnik_battery(record)
            elif source_format in (PROTO_FORMAT, GRPC_FORMAT):
                values = mavsdk_protobuf(record, grpc=source_format == GRPC_FORMAT)
            elif source_format == "mavsdk-battery-json":
                values = mavsdk_battery(record)
            else:
                values = farmbot_reported_state(record) if source_format == "farmbot-state" else json_quantities(record, source_format, allow_missing_voltage=allow_missing_voltage)
            if source_format.startswith("vda-state-"):
                values += vda_reported_state(record, source_format)
        except (KeyError, TypeError, OverflowError, RecursionError) as exc:
            raise ValueError("incompatible electrical JSON record") from exc
        if previous is not None and time < previous:
            raise ValueError("decreasing source timestamp")
        previous = time
        digest = hashlib.sha256(line.encode()).hexdigest() if detailed else None
        extra = (["SUMMARY", record_index, "sha256:" + digest] + [""] * (len(detail_fields) - 3)) if detailed else []
        for row_index, row_values in enumerate(values if source_format in (DJI_FORMAT, ROBOTNIK_FORMAT, RMF_WEB_FORMAT, TEGRA_FORMAT) else [values]):
            source_hex = "" if source_format in (RMF_WEB_FORMAT, TEGRA_FORMAT) and row_index else "hex:" + line.encode("utf-8").hex()
            writer.writerow([time, source_hex]
                            + ["" if v is None else v for v in row_values] + extra)
            if output.tell() > 128 * 1024 * 1024:
                raise ValueError("converted CSV too large; split at record boundaries")
        if vda_actions:
            for action in vda_action_rows(record, source_format):
                writer.writerow([time, ""] + [""] * len(selected_fields)
                                + ["ACTION", record_index, "sha256:" + digest]
                                + ["" if v is None else v for v in action]
                                + ([""] * len(VDA_ERROR_FIELDS) if vda_errors else []))
                if output.tell() > 16 * 1024 * 1024:
                    raise ValueError("converted CSV too large; split at record boundaries")
        if vda_errors:
            for kind, error in vda_error_rows(record, source_format):
                writer.writerow([time, ""] + [""] * len(selected_fields)
                                + [kind, record_index, "sha256:" + digest]
                                + ([""] * 7 if vda_actions else [])
                                + ["" if v is None else v for v in error])
                if output.tell() > 16 * 1024 * 1024:
                    raise ValueError("converted CSV too large; split at record boundaries")
        if output.tell() > (128 if source_format in (ROBOTNIK_FORMAT, RMF_WEB_FORMAT, TEGRA_FORMAT) else 16) * 1024 * 1024:
            raise ValueError("converted CSV too large; split at record boundaries")
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-format", choices=FORMATS, default="ardupilot-bat")
    parser.add_argument("--capture-time-us", type=int, help="explicit capture clock for selected runtime profile only")
    parser.add_argument("--allow-missing-voltage", action="store_true",
                        help="qualified ROS publisher NaN voltage is explicitly unavailable; default rejects")
    parser.add_argument("--bat-details", action="store_true",
                        help="include qualified BAT reported estimates/temperature/health")
    parser.add_argument("--vda-actions", action="store_true",
                        help="emit individually reported VDA actions; retain equal-time rows")
    parser.add_argument("--vda-errors", action="store_true", help="emit reported error details/references for VDA2.1/3.0")
    args = parser.parse_args()
    try:
        if args.vda_actions or args.vda_errors or args.source_format in (FACTSHEET_FORMAT, DOCKER_FORMAT, TEGRA_FORMAT, ORT_FORMAT, TRT_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, FARM_POINT_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT):
            with args.input.open("rb") as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("input too large; split at record boundaries")
            text = raw.decode("utf-8")
        else:
            text = args.input.read_text(encoding="utf-8")
        converted = convert(text, args.source_format, vda_actions=args.vda_actions,
                            vda_errors=args.vda_errors, bat_details=args.bat_details,
                            allow_missing_voltage=args.allow_missing_voltage, capture_time_us=args.capture_time_us)
        # Never overwrite an input or a prior evidence file, even on accidental path equality.
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"BAT conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
