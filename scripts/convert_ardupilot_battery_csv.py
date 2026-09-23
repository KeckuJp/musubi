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
# VDA5050 connection topic, one explicit selector per pinned version. The two versions do NOT share an
# enum - 2.1.0 has ONLINE/OFFLINE/CONNECTIONBROKEN, 3.0.0 renames the last one and adds HIBERNATING - so a
# version is always declared and never inferred from a payload that merely looks similar.
CONNECTION_FORMATS = {"vda-connection-2.1": "2.1.0", "vda-connection-3.0": "3.0.0"}
CONNECTION_STATES = {"2.1.0": ("ONLINE", "OFFLINE", "CONNECTIONBROKEN"),
                     "3.0.0": ("ONLINE", "OFFLINE", "HIBERNATING", "CONNECTION_BROKEN")}
DOCKER_FORMAT = "docker-stats-27.3.1-linux"
DOCKER_STATE_FORMAT = "docker-inspect-state-27.3.1"
DOCKER_STATE_FIELDS = ("container_id_hex", "container_name_hex", "container_status_reported", "container_status_disposition",
    "container_running_reported", "container_paused_reported", "container_restarting_reported",
    "container_oom_killed_reported", "container_dead_reported", "container_exit_code_reported",
    "container_last_lifecycle_error_hex", "container_health_reported", "container_health_disposition",
    "container_health_failing_streak_reported", "container_state_basis")
TEGRA_FORMAT = "tegrastats-l4t-36.4.3"
TEGRA_FIELDS = ("tegrastats_record_kind", "tegrastats_cpu_index", "tegrastats_block_hex",
    "tegrastats_block_index", "tegrastats_reported_utilization_fraction",
    "tegrastats_reported_frequency_hz", "temperature_k",
    "tegrastats_reported_memory_used", "tegrastats_reported_memory_total",
    "tegrastats_reported_memory_unit_label", "tegrastats_reported_free_block_count",
    "tegrastats_reported_free_block_size", "tegrastats_reported_power_current_w",
    "tegrastats_reported_power_average_w", "tegrastats_unselected_token_count",
    "tegrastats_value_disposition", "tegrastats_source_sha256")
ORT_FORMAT = "onnxruntime-profile-1.20.1"
TRT_FORMAT = "tensorrt-times-10.7"
HAILO_FORMAT = "hailort-infer-csv-5.4.0"
HAILO_FORMAT_424 = "hailort-infer-csv-4.24.0"
# What the CSV's frame count counts, established per version from that exact pin: each
# version's CLI assigns the minimum of its per-output-stream received counters after the
# streaming threads join. It is a host-side received minimum, so it can be lower than what
# the device processed (uneven streams, aborted or time-limited runs) and never higher; the
# requested `--frames-count` never substitutes for it.
HAILO_FRAME_BASIS = {
    HAILO_FORMAT: "REPORTED_MINIMUM_RECEIVED_ACROSS_STREAMS_IN_5_4_0_SOURCE",
    HAILO_FORMAT_424: "REPORTED_MINIMUM_RECEIVED_ACROSS_STREAMS_IN_4_24_0_SOURCE",
}
HAILO_HEADER = tuple("net_name,status,status_description,fps,num_of_frames,send_rate,recv_rate,hw_latency,overall_latency,min_power,average_power,max_power,min_current,average_current,max_current,min_temp,average_temp,max_temp".split(","))
TRT_TIMES = (("startEnqMs", "host_enqueue_start_offset_s"), ("endEnqMs", "host_enqueue_end_offset_s"),
    ("startH2dMs", "h2d_start_offset_s"), ("endH2dMs", "h2d_end_offset_s"),
    ("startComputeMs", "compute_start_offset_s"), ("endComputeMs", "compute_end_offset_s"),
    ("startD2hMs", "d2h_start_offset_s"), ("endD2hMs", "d2h_end_offset_s"),
    ("h2dMs", "h2d_duration_s"), ("computeMs", "compute_duration_s"),
    ("d2hMs", "d2h_duration_s"), ("latencyMs", "ideal_transfer_compute_sum_s"))
# The same 10.7.0 pin writes a second, differently shaped file. Profiler::exportJSONProfile emits
# `[ {"count": N}, {"name": .., "timeMs": .., "averageMs": .., "medianMs": .., "percentage": ..}, ... ]`
# in collector insertion order, unsorted and unfiltered -- unlike Profiler::print, it does not skip a
# layer whose time is zero. `timeMs` is the sum over that layer's recorded invocations and `averageMs`
# divides by that layer's own sample count, which the file never carries, so no total/count relation is
# asserted or repaired here. `percentage` divides by the sum of every layer's samples, so it is a share
# of summed layer time and never of a measured wall clock; when that sum is zero the writer prints
# nan/inf, which the existing non-JSON-number guard refuses rather than reading as zero.
TRT_PROFILE_FORMAT = "tensorrt-profile-10.7"
TRT_LAYER_FIELDS = ("trt_layer_name_hex", "trt_layer_total_time_s", "trt_layer_average_time_s",
    "trt_layer_median_time_s", "trt_layer_reported_percentage", "trt_layer_time_share",
    "trt_layer_share_disposition", "trt_profile_iteration_count_text", "trt_layer_time_basis")
TRT_LAYER_TIMES = (("timeMs", "trt_layer_total_time_s"), ("averageMs", "trt_layer_average_time_s"),
                   ("medianMs", "trt_layer_median_time_s"))
# sampleInference.cpp attaches the profiler to contexts.front() once, before the loop, and reports after
# every enqueue with no reset; the warmup branch only declines to count an iteration. So warmup samples
# are included and the file says nothing about how many. Collection can also be incomplete without
# saying so (skipped while a stream is capturing, a failed report only warns, and only the first context
# is profiled). printPerformanceProfile itself warns that with auxiliary streams layers may run in
# parallel, so these times are not summable into one run's wall clock.
TRT_LAYER_BASIS = ("REPORTED_PER_LAYER_TIME_SUMMED_OVER_RECORDED_INVOCATIONS_INCLUDING_WARMUP_"
                   "NOT_GPU_EXCLUSIVE_NOT_WALL_CLOCK_AND_NOT_SUMMABLE_INTO_ONE")
TRT_SHARE_BASIS = "REPORTED_SHARE_OF_SUMMED_LAYER_TIME_NOT_WALL_CLOCK"
TRT_SHARE_OUTSIDE = "OUTSIDE_REPORTED_SHARE_RANGE"
RMF_FORMAT = "rmf-robot-state-6990fc74"
RMF_WEB_FORMAT = "rmf-web-fleet-state-a736"
RMF_SOCKET_FORMAT = "rmf-socketio5-fleet-a736"
MIR_FORMAT = "mir-saved-status-0c5476d"
MIR_FIELDS = ("battery_remaining_fraction", "mir_map_id_hex", "mir_map_x_m", "mir_map_y_m",
    "mir_map_orientation_rad", "mir_state_code_reported", "mir_state_reported", "mir_position_basis")
RMF_WEB_FIELDS = ("rmf_fleet_name_hex", "rmf_robot_key_hex", "rmf_name_hex", "rmf_task_id_hex",
    "battery_remaining_fraction", "rmf_battery_disposition", "rmf_status_reported_hex",
    "rmf_status_disposition", "rmf_unix_millis_reported_text", "rmf_snapshot_sha256",
    # The same declared map-local pose columns as the RobotState path. The pinned web model has no
    # path field at all, so no plan column is added here.
    "rmf_map_name_hex", "rmf_map_x_m", "rmf_map_y_m", "rmf_map_yaw_rad", "rmf_position_basis",
    # Which kind of row this is, and what the response said about its robot dictionary. Without
    # these a row with no robot values could not be told apart from a robot that reported nothing.
    "rmf_record_kind", "rmf_robot_count", "rmf_robots_availability")
# `FleetState.robots` is Optional in the pinned model, so three different things have to stay
# apart: the publisher reported robots, the publisher reported an empty dictionary (an idle fleet
# is a real answer), and the publisher did not report the dictionary at all.
RMF_ROBOTS_WITH = "REPORTED_ROBOT_DICTIONARY_WITH_AT_LEAST_ONE_ROBOT"
RMF_ROBOTS_EMPTY = "REPORTED_EMPTY_ROBOT_DICTIONARY_THE_PUBLISHER_REPORTED_ZERO_ROBOTS"
RMF_ROBOTS_UNAVAILABLE = "UNAVAILABLE_THE_OPTIONAL_ROBOT_DICTIONARY_WAS_NOT_REPORTED"
# The same token the RobotState path already writes for "this row describes one robot", reused
# rather than spelled a second way; FLEET_SNAPSHOT is the only new kind, and PATH_POINT stays
# exclusive to the RobotState path, which is the only one with a reported plan.
RMF_ROBOT_KIND = "STATE"
RMF_SNAPSHOT_KIND = "FLEET_SNAPSHOT"
# On a snapshot row the per-robot columns are not "missing from a robot": there is no robot. That
# is a third answer again, and it never reads as an unavailable robot value.
RMF_SNAPSHOT_BASIS = "NOT_APPLICABLE_FLEET_SNAPSHOT_ROW_NO_ROBOT_IS_DESCRIBED_HERE"
FARMDUINO_FORMAT = "farmduino-r82-52053ec8"
FARM_POINT_FORMAT = "farmbot-point-15.30.5"
FARM_DIGITAL_FORMAT = "farmbot-digital-reading-15.30.5"
FARM_DIGITAL_FIELDS = ("farm_reading_id_text", "farm_pin_reported", "farm_digital_level_reported",
    "farm_digital_value_reported", "farm_read_at_reported_us", "farm_read_at_basis")
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
ATTITUDE_JSON = "mavsdk-attitude-json"
ATTITUDE_PROTO = "mavsdk-attitude-protobuf"
ATTITUDE_GRPC = "mavsdk-attitude-grpc"
ATTITUDE_FORMATS = (ATTITUDE_JSON, ATTITUDE_PROTO, ATTITUDE_GRPC)
ATTITUDE_WIRE = (ATTITUDE_PROTO, ATTITUDE_GRPC)
ATTITUDE_FIELDS = ("attitude_roll_rad", "attitude_pitch_rad", "attitude_yaw_rad",
                   "attitude_angle_basis", "attitude_device_time_us", "attitude_device_time_basis")
# EulerAngle at the pinned proto states its unit, a per-axis sign and its rotation sequence, and
# the pinned producer fills it with to_deg_from_rad(...), so the saved numbers are degrees. What
# no orientation message at this pin states is the reference frame: none of Quaternion,
# EulerAngle or AngularVelocityBody names a body, NED, FRD or earth-fixed frame, so none is
# supplied here.
ATTITUDE_ANGLE_BASIS = ("REPORTED_DEGREES_CONVERTED_ONCE_TO_RADIANS_RIGHT_HANDED_3_2_1_INTRINSIC_"
                        "TAIT_BRYAN_SEQUENCE_ROLL_POSITIVE_BANKING_RIGHT_PITCH_POSITIVE_NOSE_UP_"
                        "YAW_POSITIVE_CLOCKWISE_SEEN_FROM_ABOVE_REFERENCE_FRAME_NOT_STATED_BY_SOURCE")
# The producer writes time_boot_ms * 1000, so the resolution is the millisecond it came from.
ATTITUDE_TIME_BASIS = ("SOURCE_BOOT_RELATIVE_MILLISECOND_TICK_TIMES_1000_NOT_UTC_NOT_THE_CAPTURE_"
                       "CLOCK_AND_NOT_MICROSECOND_RESOLUTION")
GUTMA_FORMAT = "openflight-gutma-dx-1.0.3"
GUTMA_PINS = (("version", "1.0.0"), ("parrot_version", "1.0.3"), ("logging_type", "GUTMA_DX_JSON"))
GUTMA_FIELDS = ("source_keys_hex", "source_document_sha256", "gutma_item_index",
                "battery_remaining_fraction", "gutma_battery_percent_disposition",
                "gutma_logging_start_reported_hex", "gutma_time_basis", "gutma_value_basis")
INAV_FORMAT = "inav-logging-csv-ddf18258"
INAV_ANALOG = ("voltage", "amperage", "mAhdrawn", "rssi")
INAV_ATTITUDE = ("kinematicsX", "kinematicsY", "kinematicsZ")
# tabs/logging.js print_head writes these seven together, in this order, for MSP_RAW_GPS.
INAV_GPS = ("gpsFix", "gpsNumSat", "gpsLat", "gpsLon", "gpsAlt", "gpsSpeed", "gpsGroundCourse")
# The angular unit is not implied by the division: it comes from the same pinned commit's own
# consumer. tabs/logging.js:194-195 writes `(GPS_DATA.lat / 10000000)`; tabs/gps.js:152-153 forms
# the identical expression and lines 164-165 display it as "deg". No file names a datum, and the
# gps.js `fix >= 2` map gate is a display threshold with no declared enum, so neither a geodetic
# frame nor fix validity follows from it.
INAV_ANGLE_BASIS = ("DEG_FROM_THE_SAME_PINNED_CONFIGURATOR_GPS_TAB_DISPLAY_OF_THIS_DIVIDED_VALUE"
                    "_NO_DATUM_DECLARED_ANYWHERE_AND_NOT_A_FIX_VALIDITY_STATEMENT")
# gps.js labels `GPS_DATA.alt` "m" (line 163) and `GPS_DATA.speed` "cm/s" (line 166) and displays
# ground course nowhere. Those labels exist but are deliberately not applied here, so the three
# cells stay raw and the altitude datum stays unverified; this states that rather than claiming
# the pin is silent about all three.
INAV_UNSCALED_BASIS = ("ALTITUDE_SPEED_COURSE_RAW_NO_UNIT_APPLIED_HERE_SAME_PIN_DISPLAYS_ALT_AS_M"
                       "_AND_SPEED_AS_CM_PER_S_UNADOPTED_COURSE_NEVER_DISPLAYED_NO_DATUM")
INAV_FIELDS = ("battery_voltage_v", "battery_current_a", "battery_consumed_ah", "battery_power_w",
               "battery_current_sign_basis", "inav_rssi_code_reported", "inav_rssi_basis",
               "reported_roll_rad", "reported_pitch_rad", "reported_yaw_rad",
               "inav_gps_latitude_deg_reported", "inav_gps_longitude_deg_reported",
               "inav_gps_angle_basis",
               "inav_gps_coordinate_disposition", "inav_gps_fix_code_reported",
               "inav_gps_satellites_reported", "inav_gps_altitude_code_reported",
               "inav_gps_speed_code_reported", "inav_gps_ground_course_code_reported",
               "inav_gps_unscaled_basis",
               "inav_value_basis", "source_header_hex")
PING_FIELDS = ("sonar_reported_distance_m", "sonar_confidence_code", "sonar_reported_confidence_fraction",
    "sonar_confidence_status", "sonar_message_id", "sonar_source_device_id", "sonar_transmit_duration_s",
    "sonar_ping_number_reported", "sonar_scan_start_m", "sonar_scan_length_m", "sonar_gain_code")
RMF_MODES = ("IDLE", "CHARGING", "MOVING", "PAUSED", "WAITING", "EMERGENCY", "GOING_HOME",
             "DOCKING", "ADAPTER_ERROR", "CLEANING", "PERFORMING_ACTION", "ACTION_COMPLETED")
RMF_FIELDS = ("battery_remaining_fraction", "rmf_name_hex", "rmf_model_hex", "rmf_task_id_hex",
              "rmf_sequence_reported_text", "rmf_mode_number_reported", "rmf_mode_reported",
              "rmf_mode_request_id_reported_text", "rmf_performing_action_hex")
# Location.msg at the pinned commit states a unit for exactly one field - "Speed limit of the lane
# leading to this waypoint in m/s". x, y and yaw carry no unit there, and the pinned rmf_ros2
# consumer copies them without scaling or stating one, so the caller must declare the units and the
# rows say the declaration is theirs. The map/level name is an opaque nav-graph name, never a datum.
RMF_LOCATION_FIELDS = ("rmf_source_record_index", "rmf_snapshot_sha256",
    "rmf_record_kind", "rmf_path_point_index", "rmf_path_point_count",
    "rmf_map_name_hex", "rmf_map_x_m", "rmf_map_y_m", "rmf_map_yaw_rad", "rmf_position_basis",
    "rmf_waypoint_index_text", "rmf_waypoint_time_us_reported", "rmf_waypoint_time_basis",
    "rmf_approach_speed_limit_m_s", "rmf_approach_speed_limit_disposition")
RMF_POSITION_BASIS = "CALLER_DECLARED_UNITS_IN_REPORTED_MAP_FRAME_NOT_GLOBAL_OR_GEODETIC"
RMF_TIME_BASIS = "REPORTED_WAYPOINT_TIME_CLOCK_BASIS_NOT_ESTABLISHED_BY_THE_PIN_NOT_THE_ROW_CLOCK"
DOCKER_FIELDS = ("docker_value_basis", "docker_id_hex", "docker_name_reported_hex", "docker_cpu_equivalents_reported",
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
CONNECTION_BASIS_VEHICLE = ("REPORTED_BY_THE_VEHICLE_ON_THE_CONNECTION_TOPIC_NOT_PHYSICAL_RF_CONNECTIVITY_"
                            "NOT_LIVENESS_NOW_NOT_AUTHENTICATION_AND_NOT_A_DELIVERY_GUARANTEE")
CONNECTION_BASIS_BROKER = ("BROKER_LAST_WILL_REGISTERED_BY_THE_VEHICLE_AT_CONNECT_SO_THE_SPEC_SAYS_ITS_"
                           "TIMESTAMP_AND_HEADERID_ARE_ALWAYS_OUTDATED_NOT_THE_MOMENT_A_CONNECTION_FAILED")
CONNECTION_BASIS_UNSUPPORTED = ("VALUE_OUTSIDE_THIS_VERSIONS_PINNED_ENUM_RETAINED_AS_REPORTED_"
                                "NO_PUBLISHER_OR_MEANING_IS_INFERRED_FOR_IT")
CONNECTION_FIELDS = ("vda_connection_basis", "vda_connection_state_reported",
                     "vda_connection_state_status", "vda_connection_version_reported",
                     "vda_connection_header_id", "vda_connection_manufacturer_hex",
                     "vda_connection_serial_hex")
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
# Selected-schema battery temperature. Kept out of JSON_ELECTRICAL_FIELDS because other JSON
# formats already carry their own battery_temperature_k and position their values by index.
BATTERY_TEMPERATURE_FIELDS = ("battery_temperature_k", "battery_temperature_basis")
TEMPERATURE_FORMATS = ("ros-battery-json", "vda-state-1.1", "vda-state-2.0", "vda-state-2.1", "vda-state-3.0")
PX4_REQUIRED = ("timestamp", "id", "voltage_v", "current_a", "discharged_mah", "remaining")
CELL_FORMATS = ("ardupilot-bcl-8178", "ardupilot-bcl2-8178")
CELL43_FORMATS = ("ardupilot-bcl-4.3", "ardupilot-bcl2-4.3")
CELL_REQUIRED = ("TimeUS", "Volt", *[f"V{i}" for i in range(1, 11)])
CELL_FIELDS = ("battery_bank_selected", *[f"battery_cell_{i}_v" for i in range(1, 11)],
               *[f"battery_cell_{i}_status" for i in range(1, 11)])
FORMATS = ("ardupilot-bat", "ardupilot-bat-inst", "ardupilot-bat-4.3", "px4-battery",
           "vda-state-1.1", "vda-state-2.0", "vda-state-2.1", "vda-state-3.0", *CONNECTION_FORMATS, "ros-battery-json", "farmbot-state", "mavsdk-battery-json", INAV_FORMAT, GUTMA_FORMAT, DJI_FORMAT, ROBOTNIK_FORMAT, FACTSHEET_FORMAT, DOCKER_FORMAT, DOCKER_STATE_FORMAT, TEGRA_FORMAT, ORT_FORMAT, TRT_FORMAT, TRT_PROFILE_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, MIR_FORMAT, FARM_POINT_FORMAT, FARM_DIGITAL_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT, *ATTITUDE_FORMATS, *CELL_FORMATS, *CELL43_FORMATS)
MAVSDK_FIELDS = ("battery_id_reported", "battery_temperature_k", "battery_time_remaining_s",
                 "battery_function_reported")
FORMATS += (HAILO_FORMAT, HAILO_FORMAT_424)
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
VDA_FIELDS = ("vda_selected_version_hex",
              "vda_operating_mode_reported", "vda_operating_mode_raw_hex",
              "vda_driving_reported", "vda_paused_reported",
              "vda_emergency_stop_reported", "vda_emergency_stop_raw_hex",
              "vda_protective_field_violation_reported",
              "vda_instance_manufacturer_hex", "vda_instance_serial_number_hex",
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
    if vda_actions and source_format == "vda-state-1.1":
        # The 1.1 action-status and error-level enums are outside the compared field set, so no
        # detail claim is made for them.
        raise ValueError("selected VDA 1.1 action details are not qualified")
    if vda_actions and not source_format.startswith("vda-state-"):
        raise ValueError("action expansion requires selected VDA state version")
    if vda_errors and source_format not in ("vda-state-2.1", "vda-state-3.0"):
        raise ValueError("error detail expansion requires VDA2.1 or3.0")
    if source_format in HAILO_FRAME_BASIS:
        return convert_hailo_profile(text, capture_time_us, source_format)
    if source_format in (ORT_FORMAT, TRT_FORMAT, TRT_PROFILE_FORMAT):
        return convert_runtime_profile(text, capture_time_us, source_format)
    if capture_time_us is not None:
        raise ValueError("capture time option requires runtime profile")
    if source_format == INAV_FORMAT:
        return convert_inav_logging(text)
    if source_format == GUTMA_FORMAT:
        return convert_gutma_flight_logging(text)
    if source_format.startswith("vda-state-") or source_format in (*CONNECTION_FORMATS, "ros-battery-json", "farmbot-state", "mavsdk-battery-json", DJI_FORMAT, ROBOTNIK_FORMAT, FACTSHEET_FORMAT, DOCKER_FORMAT, DOCKER_STATE_FORMAT, TEGRA_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, MIR_FORMAT, FARM_POINT_FORMAT, FARM_DIGITAL_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT, *ATTITUDE_FORMATS):
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
    if source_format in ("farmbot-state", "mavsdk-battery-json", DJI_FORMAT, ROBOTNIK_FORMAT, DOCKER_FORMAT, DOCKER_STATE_FORMAT, TEGRA_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, MIR_FORMAT, FARM_POINT_FORMAT, FARM_DIGITAL_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT, *ATTITUDE_FORMATS):
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
        # BatteryState declares "Temperature in Degrees Celsius (If unmeasured NaN)". An export
        # that omits the field is a third, separately named case: still no value, different reason.
        if "temperature" not in record:
            temperature = (None, "ABSENT_FROM_SOURCE_RECORD")
        else:
            celsius = quantity(record["temperature"], "temperature", missing=True)
            if celsius is None:
                temperature = (None, "UNAVAILABLE_SOURCE_NAN")
            else:
                # 0 degC is a reported measurement here; NaN is how this schema says unmeasured.
                # Same shared degC->K normalizer the other selected temperature paths use.
                kelvin = normalize_unit(celsius, "degC")[1]
                if kelvin < 0:
                    raise ValueError("battery temperature below absolute zero")
                temperature = (kelvin, "REPORTED")
        status = record["power_supply_status"]
        if type(status) is not int or not 0 <= status <= 255 or type(record["present"]) is not bool:
            raise ValueError("invalid ROS status/presence")
        # Absence must not masquerade as a measured battery with zero quantities.
        if not record["present"]:
            voltage = current = stored_charge = remaining = None
            temperature = (None, "BATTERY_NOT_PRESENT")
        state = {0: "UNKNOWN", 1: "CHARGING", 2: "DISCHARGING", 3: "NOT_CHARGING", 4: "FULL"}.get(status, f"UNKNOWN_{status}")
    else:
        # The 1.1 field meanings here come from one immutable commit of the VDA5050 repository,
        # 54578243f5413b0706ce74cbeb1aff5ea2730afc (reached through the mutable release/1.1.0 ref),
        # which carries no state JSON schema and says of itself: "This version may have differences
        # to the published version 1.1 by the VDA, as this Git version was only a working document."
        # So this is a repository working draft, not verified published-standard 1.1. Its header
        # prose gives the version syntax "[Major].[Minor].[Patch]" only; it does not establish that
        # patch 0 is the published 1.1 baseline. "1.1.0" is therefore the caller's explicit profile
        # condition, matched exactly, and not a conformance claim about any published document.
        expected = {"vda-state-1.1": "1.1.0", "vda-state-2.0": "2.0.0",
                    "vda-state-2.1": "2.1.0", "vda-state-3.0": "3.0.0"}[source_format]
        if record["version"] != expected:
            raise ValueError("wrong VDA version")
        for field in ("manufacturer", "serialNumber"):
            if not isinstance(record[field], str) or not record[field]:
                raise ValueError("invalid VDA identity")
        old = source_format in ("vda-state-1.1", "vda-state-2.0", "vda-state-2.1")
        battery = record["batteryState" if old else "powerSupply"]
        remaining = quantity(battery["batteryCharge" if old else "stateOfCharge"], "state of charge") / 100
        voltage = quantity(battery["batteryVoltage"], "voltage") if "batteryVoltage" in battery else None
        current = quantity(battery["batteryCurrent"], "current") if not old and "batteryCurrent" in battery else None
        if type(battery["charging"]) is not bool:
            raise ValueError("invalid charging state")
        state = "CHARGING" if battery["charging"] else "NOT_CHARGING"
        # VDA5050 defines no battery temperature at these pinned versions. Say so; invent nothing.
        temperature = (None, "NOT_PROVIDED_BY_SELECTED_SCHEMA")
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
    result = values + [stored_charge, state, basis, *temperature]
    if allow_missing_voltage:
        if source_format != "ros-battery-json":
            raise ValueError("missing voltage policy requires ROS BatteryState")
        result.append("BATTERY_NOT_PRESENT" if not record["present"] else
                      "UNAVAILABLE_SOURCE_NAN" if record["voltage"] == "NaN" else "REPORTED")
    return result


def vda_reported_state(record, source_format):
    """Finite state reports, not actuation, physical certification or full schema validation."""
    # For the finite compared field set, this working draft uses the same field names, units and
    # enum members as the pinned 2.0.0 schema, so it takes the same older-layout branch. Only the
    # 1.1.0 and 2.0.0 pins were compared; nothing here covers every intermediate revision. Its two
    # known spelling differences - the 6.10.6 table's TEACHING
    # and the lowercase eStop tokens - are deliberately left to the UNKNOWN-plus-raw path below
    # rather than mapped onto the 2.x tokens, because the 1.1 document contradicts itself on the
    # first and 2.0 changed the second.
    old = source_format in ("vda-state-1.1", "vda-state-2.0", "vda-state-2.1")
    result = dict.fromkeys(VDA_FIELDS)
    # Which version's semantics produced this row. The string is the publisher's own, already matched
    # exactly against the selector, so 1.1 and 2.0 records - which share field names but not every
    # enum spelling - are distinguishable in the output instead of only inside the raw record.
    result["vda_selected_version_hex"] = "hex:" + record["version"].encode("utf-8").hex()

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
        # Keep the token the publisher actually sent, as the action-status and error-level paths
        # already do, so an unmapped value is not reduced to the bare label "UNKNOWN".
        result["vda_operating_mode_raw_hex"] = "hex:" + record["operatingMode"].encode("utf-8").hex()
    for name in ("driving", "paused"):
        if name in record:
            result[f"vda_{name}_reported"] = boolean(record[name])
    if "safetyState" in record:
        state = record["safetyState"]
        reported_stop = state["eStop" if old else "activeEmergencyStop"]
        stop = enum(reported_stop, {"AUTOACK", "MANUAL", "REMOTE", "NONE"} if old
                    else {"MANUAL", "REMOTE", "NONE"})
        result["vda_emergency_stop_reported"] = stop
        result["vda_emergency_stop_raw_hex"] = "hex:" + reported_stop.encode("utf-8").hex()
        result["vda_protective_field_violation_reported"] = boolean(state["fieldViolation"])
    if "velocity" in record:
        velocity = record["velocity"]
        if type(velocity) is not dict:
            raise ValueError("invalid velocity object")
        # Named rather than sliced out of VDA_FIELDS, so adding a field cannot silently move
        # these three onto the wrong columns.
        for source, destination in zip(("vx", "vy", "omega"),
                                       ("vehicle_vx_m_s", "vehicle_vy_m_s", "vehicle_yaw_rate_rad_s")):
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
    # The instance the report belongs to. Already validated above for every state record; carry it
    # instead of leaving it readable only inside the retained raw JSON. Not an authenticated
    # identity and not a global one: it is the manufacturer/serial pair this publisher declared.
    for source, destination in (("manufacturer", "vda_instance_manufacturer_hex"),
                                ("serialNumber", "vda_instance_serial_number_hex")):
        result[destination] = "hex:" + record[source].encode("utf-8").hex()
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


def mir_saved_status(record):
    if (record["adapter_commit"] != "0c5476d17979a8d65f603f235f7f736e731d2737" or
            record["api_version"] != "2.0.0" or record["position_unit"] != "m"):
        raise ValueError("qualified saved MiR status and metre declaration required")
    status = record["status"]
    remaining = quantity(status["battery_percentage"], "MiR battery percentage")
    map_id, pose = status["map_id"], status["position"]
    if not 0 <= remaining <= 100 or not isinstance(map_id, str) or not map_id:
        raise ValueError("invalid MiR percentage or map identity")
    x, y, angle = [quantity(pose[key], "MiR map pose") for key in ("x", "y", "orientation")]
    if not -360 <= angle <= 360:
        raise ValueError("orientation outside qualified reported range")
    code = status.get("state_id")
    if code is not None and (type(code) is not int or not 0 <= code < 2**63):
        raise ValueError("invalid MiR reported state code")
    state = "NOT_REPORTED" if code is None else {3: "READY", 4: "PAUSE", 5: "EXECUTING",
        11: "MANUAL_CONTROL", 12: "ERROR"}.get(code, "UNKNOWN_RETAINED")
    return [normalize_unit(remaining, "%")[1], "hex:" + map_id.encode().hex(), x, y,
        normalize_unit(angle, "deg")[1], code, state, "DECLARED_METRE_MAP_FRAME_NOT_GLOBAL"]


def farmbot_digital_reading(record):
    try:
        from scripts.convert_gpx_recorded import utc_microseconds
    except ModuleNotFoundError:
        from convert_gpx_recorded import utc_microseconds
    if record["schema_commit"] != "6db0ea3eb0059424573da8399d082b445624b24c":
        raise ValueError("unqualified saved SensorReading schema")
    reading = record["reading"]
    identifier, pin, mode, value = (reading[key] for key in ("id", "pin", "mode", "value"))
    if (type(identifier) is not int or not 0 <= identifier < 2**63 or
            type(pin) is not int or not 0 <= pin <= 255 or type(mode) is not int or mode != 0 or
            type(value) is not int or value not in (0, 1) or not isinstance(reading["read_at"], str)):
        raise ValueError("qualified digital reading required")
    reported_time = utc_microseconds(reading["read_at"])
    return ["id:" + str(identifier), pin, ("LOW", "HIGH")[value], value,
            reported_time, "REPORTED_OR_LEGACY_CREATED_TIME_NOT_CAPTURE"]


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


def mavsdk_attitude_protobuf(record, *, grpc=False):
    """Fixed standalone EulerAngle descriptor, built in code; no runtime schema loading."""
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
    wire = record["grpc_frame_hex" if grpc else "attitude_protobuf_hex"]
    if not isinstance(wire, str) or len(wire) > 28000 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", wire):
        raise ValueError("bounded complete protobuf hex required")
    payload = bytes.fromhex(wire)
    if grpc:
        # The adopted non-compressed Length-Prefixed-Message check, reused unchanged.
        if len(payload) < 5 or payload[0] != 0 or int.from_bytes(payload[1:5], "big") != len(payload) - 5:
            raise ValueError("one complete noncompressed gRPC message required")
        payload = payload[5:]
    schema = descriptor_pb2.FileDescriptorProto(name="selected_attitude.proto", package="selected", syntax="proto3")
    descriptor = schema.message_type.add(name="EulerAngle")
    names = ("rollDeg", "pitchDeg", "yawDeg", "timestampUs")
    for number, name in enumerate(names, 1):
        descriptor.field.add(name=name, number=number, label=1, type=4 if number == 4 else 2)
    if grpc:
        response = schema.message_type.add(name="AttitudeEulerResponse")
        response.field.add(name="attitudeEuler", number=1, label=1, type=11, type_name=".selected.EulerAngle")
    pool = descriptor_pool.DescriptorPool(); pool.Add(schema)
    message = message_factory.GetMessageClass(pool.FindMessageTypeByName(
        "selected.AttitudeEulerResponse" if grpc else "selected.EulerAngle"))()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError("malformed protobuf message") from exc
    outer_unknown = 0
    if grpc:
        if not message.HasField("attitudeEuler"):
            raise ValueError("AttitudeEulerResponse has no selected EulerAngle")
        outer_unknown = len(unknown_fields.UnknownFieldSet(message))
        message = message.attitudeEuler
    present = {field.name for field, _ in message.ListFields()}
    values = {name: getattr(message, name) for name in names}
    values = {name: "NaN" if isinstance(value, float) and math.isnan(value) else value
              for name, value in values.items()}
    return mavsdk_attitude(dict(sdk_version=record["sdk_version"], attitude_euler=values)) + [
        # proto3 gives an absent float and a reported 0.0 the same bytes, and a level attitude is a
        # real 0.0, so the value is kept and the ambiguity is named in this adopted column instead.
        # The pin's default_value="NaN" option is an SDK-side default, not a wire sentinel.
        "hex:" + ",".join(name for name in names if name not in present).encode().hex(),
        outer_unknown + len(unknown_fields.UnknownFieldSet(message)),
        "GRPC_MESSAGE_NOT_RPC_SUCCESS" if grpc else "PROTO3_DECODED_NOT_SENSOR_VALIDATED"]


def mavsdk_attitude(record):
    """Saved reported EulerAngle: declared orientation values, never a derived or fused attitude."""
    if record["sdk_version"] != "3.17.4":
        raise ValueError("unqualified MAVSDK version")
    euler = record["attitude_euler"]
    if any(name in euler for name in ("roll_deg", "pitch_deg", "yaw_deg", "timestamp_us")):
        raise ValueError("mixed or unselected protobuf field spelling")
    if not all(name in euler for name in ("rollDeg", "pitchDeg", "yawDeg", "timestampUs")):
        raise ValueError("every selected EulerAngle field required; absence is not assumed zero")
    angles = [quantity(euler[name], name, missing=True) for name in ("rollDeg", "pitchDeg", "yawDeg")]
    if any(value is not None and abs(value) > 3.4028234663852886e38 for value in angles):
        raise ValueError("reported angle exceeds protobuf float32 range")
    device_time = euler["timestampUs"]
    if type(device_time) is not int or type(device_time) is bool or not 0 <= device_time < 2**63:
        raise ValueError("invalid reported device timestamp")
    # The source guarantees degrees, so the one conversion the common output needs is applied once.
    # Nothing else is scaled, wrapped, normalised or turned into a heading.
    return [None if value is None else normalize_unit(value, "deg")[1] for value in angles] + [
        ATTITUDE_ANGLE_BASIS, device_time, ATTITUDE_TIME_BASIS]


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


def vda_connection_state(record, source_format):
    """The connection state a saved VDA5050 connection payload reports - nothing more.

    Established at the pinned schemas and specifications (2.1.0 `511d01d7`, 3.0.0 `e9ba560b`): the six
    fields are required; `headerId` is a `uint32` "defined per topic and incremented by 1 with each sent
    (but not necessarily received) message"; `timestamp` is ISO 8601 UTC; and the enums differ between the
    versions, so each version has its own selector and its own accepted set.

    Who reports what matters here. ONLINE, OFFLINE and (3.0.0) HIBERNATING are published by the vehicle;
    `CONNECTIONBROKEN`/`CONNECTION_BROKEN` is the broker's **last will**, which the vehicle registered when
    it connected, so the spec says its "timestamp and headerId fields will always be outdated". This
    importer therefore never treats a broken-state stamp as the moment a connection failed, and never turns
    any of this into physical connectivity, liveness now, authentication, freshness or delivery.
    """
    expected = CONNECTION_FORMATS[source_format]
    if record["version"] != expected:
        raise ValueError("wrong VDA connection version")
    identity = [record[key] for key in ("manufacturer", "serialNumber")]
    if any(not isinstance(value, str) or not value for value in identity):
        raise ValueError("invalid VDA connection identity")
    header = record["headerId"]
    # uint32 per the protocol header table; a per-topic counter, so gaps are legitimate and duplicates are
    # kept as their own rows rather than merged away.
    if type(header) is not int or not 0 <= header <= 2**32 - 1:
        raise ValueError("invalid VDA connection headerId")
    state = record["connectionState"]
    if not isinstance(state, str) or not state:
        raise ValueError("invalid VDA connection state")
    known = state in CONNECTION_STATES[expected]
    # An unlisted value keeps its text and is marked unsupported: this adapter reads selected fields and is
    # not a schema validator, so it reports what the payload said instead of inventing a known state. The
    # schema prose's `DISCONNECTED` is in neither enum and lands here too.
    basis = (CONNECTION_BASIS_BROKER if known and state in ("CONNECTIONBROKEN", "CONNECTION_BROKEN")
             else CONNECTION_BASIS_VEHICLE if known else CONNECTION_BASIS_UNSUPPORTED)
    return [basis, state, "SUPPORTED_PINNED_STATE" if known else "UNSUPPORTED_STATE_VALUE_RETAINED",
            expected, header, *["hex:" + value.encode().hex() for value in identity]]


def reported_container_name(value):
    """Daemon-reported name, kept byte-for-byte. Mutable and not an authenticated identity.

    The CLI writes `--` when it has no name, the same unavailable sentinel it uses for the
    quantities; an absent key is an export that did not carry one. Neither is a container
    without a name, so both stay empty rather than becoming a value.
    """
    if value is None or value == "--":
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("invalid reported container name")
    return "hex:" + value.encode("utf-8").hex()


def docker_state(record):
    if record["daemon_version"] != "27.3.1":
        raise ValueError("unqualified container state version")
    source = record["inspect"]
    identifier, state = source["Id"], source["State"]
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("missing container identifier")
    status, error = state["Status"], state["Error"]
    if not isinstance(status, str) or not status or not isinstance(error, str):
        raise ValueError("invalid container state strings")
    flags = [state[key] for key in ("Running", "Paused", "Restarting", "OOMKilled", "Dead")]
    if any(type(value) is not bool for value in flags):
        raise ValueError("container state boolean required")
    exit_code = state["ExitCode"]
    if type(exit_code) is not int or not -(2**63) <= exit_code < 2**63:
        raise ValueError("container exit code out of range")
    health = state.get("Health")
    health_status, health_kind, streak = None, "NOT_PROVIDED", None
    if health is not None:
        health_status, streak = health["Status"], health["FailingStreak"]
        if not isinstance(health_status, str) or not health_status or type(streak) is not int or not 0 <= streak < 2**63:
            raise ValueError("invalid reported container health")
        health_kind = "KNOWN_REPORTED" if health_status in ("starting", "healthy", "unhealthy", "none") else "UNKNOWN_RETAINED"
    return ["hex:" + identifier.encode().hex(), reported_container_name(source.get("Name")), status,
        "KNOWN_REPORTED" if status in ("paused", "restarting", "removing", "running", "dead", "created", "exited") else "UNKNOWN_RETAINED",
        *flags, exit_code, "hex:" + error.encode().hex(), health_status, health_kind, streak,
        "DAEMON_REPORT_NOT_CAUSE_OR_COMPLETION_PROOF"]


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
    # The CLI emits Container/Name/ID; the name is what makes the row a *named* container,
    # and the displayed ID can be truncated. `Container` stays raw: it echoes what was asked
    # for, not an observation of the container.
    return ["ROUNDED_DISPLAY_NOT_RAW_COUNTER_MISSING_IS_UNAVAILABLE", "hex:" + identifier.encode().hex(),
        reported_container_name(stats.get("Name")),
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


def _tegra_row(kind, disposition, source_sha, **cells):
    """One widened tegrastats row; every unset cell stays blank rather than becoming a zero."""
    values = dict.fromkeys(TEGRA_FIELDS)
    values["tegrastats_record_kind"] = kind
    values["tegrastats_value_disposition"] = disposition
    values["tegrastats_source_sha256"] = source_sha
    for name, value in cells.items():
        assert name in values, name
        values[name] = value
    return [values[name] for name in TEGRA_FIELDS]


def tegrastats_quantities(record, source_sha):
    """Only the CPU/thermal/GPU/memory/rail tokens the r36.4.3 page documents; the rest stays raw."""
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
                rows.append(_tegra_row("CPU", "OFF_REPORTED", source_sha,
                                       tegrastats_cpu_index=index))
                continue
            parsed = re.fullmatch(r"([0-9]+)%" if shared else r"([0-9]+)%@([0-9]+)", token)
            if not parsed or int(parsed[1]) > 100:
                raise ValueError("invalid CPU utilization/frequency token")
            mhz = int(shared if shared else parsed[2])
            if not 0 < mhz <= (2**53 - 1) // 1000000:
                raise ValueError("CPU frequency outside exact Hz range")
            rows.append(_tegra_row(
                "CPU", "REPORTED_APPROXIMATE_UTILIZATION", source_sha, tegrastats_cpu_index=index,
                tegrastats_reported_utilization_fraction=int(parsed[1]) / 100,
                tegrastats_reported_frequency_hz=mhz * 1000000))
    blocks = set()
    for token in report.split():
        if "@" not in token or not token.endswith("C"):
            continue
        parsed = re.fullmatch(r"([A-Za-z0-9_-]+)@(-?[0-9]+(?:\.[0-9]+)?)C", token)
        if not parsed or parsed[1] in blocks:
            raise ValueError("malformed or duplicate temperature block")
        blocks.add(parsed[1])
        temperature = normalize_unit(float(parsed[2]), "degC")[1]
        rows.append(_tegra_row("TEMPERATURE", "REPORTED_UNCALIBRATED", source_sha,
                               tegrastats_block_hex="hex:" + parsed[1].encode().hex(),
                               temperature_k=temperature))
    def megahertz(text):
        mhz = int(text)
        if not 0 < mhz <= (2**53 - 1) // 1000000:
            raise ValueError("reported frequency outside exact Hz range")
        return mhz * 1000000

    # GR3D_FREQ X%@Y or X%@[Y1,Y2]: the page documents the percentage as a "proportion of GPU
    # activation time in a period" without defining the period, and documents the GPC frequencies in
    # megahertz. The AGX two-GPC form is the documented one; a single frequency is accepted without
    # asserting a count, because the Orin Nano/NX form is not separately described.
    gpu = list(re.finditer(r"(?<!\S)GR3D_FREQ ([0-9]+)%@(?:\[([0-9,]+)\]|([0-9]+))(?=\s|$)", report))
    if len(gpu) > 1 or len(re.findall(r"(?<!\S)GR3D_FREQ(?=\s|$)", report)) != len(gpu):
        raise ValueError("malformed or duplicate GPU group")
    for match in gpu:
        if int(match[1]) > 100:
            raise ValueError("invalid GPU utilization token")
        entries = match[3].split(",") if match[3] else match[2].split(",")
        for index, entry in enumerate(entries):
            if not re.fullmatch(r"[0-9]+", entry.strip()):
                raise ValueError("invalid GPU frequency token")
            rows.append(_tegra_row(
                "GPU", "REPORTED_GPU_PROPORTION_OVER_UNDOCUMENTED_PERIOD", source_sha,
                tegrastats_block_index=index,
                tegrastats_reported_utilization_fraction=int(match[1]) / 100,
                tegrastats_reported_frequency_hz=megahertz(entry.strip())))

    # RAM X/Y (lfb NxZ): the page labels these "megabytes" but never states the byte base, so the
    # reported numbers and the label are kept literally and only a same-unit fraction is derived.
    memory = list(re.finditer(
        r"(?<!\S)RAM ([0-9]+)/([0-9]+)([A-Za-z]+)(?: \(lfb ([0-9]+)x([0-9]+)([A-Za-z]+)\))?(?=\s|$)",
        report))
    if len(memory) > 1 or len(re.findall(r"(?<!\S)RAM(?=\s|$)", report)) != len(memory):
        raise ValueError("malformed or duplicate RAM group")
    for match in memory:
        # An (lfb …) group that follows the selected RAM report but does not parse is a malformed part
        # of a selected report, not an unrelated token: refuse rather than emit blank free-block cells.
        if match[4] is None and re.match(r"\s+\(lfb\b", report[match.end():]):
            raise ValueError("malformed free block group")
        used, total = int(match[1]), int(match[2])
        if total <= 0 or used > total:
            raise ValueError("invalid reported memory extent")
        if match[4] is not None and match[6] != match[3]:
            raise ValueError("free block label disagrees with the reported memory label")
        rows.append(_tegra_row(
            "MEMORY", "REPORTED_MEMORY_IN_SOURCE_UNIT_LABEL_NO_BYTE_BASE_DOCUMENTED", source_sha,
            tegrastats_block_hex="hex:" + b"RAM".hex(),
            tegrastats_reported_memory_used=used, tegrastats_reported_memory_total=total,
            tegrastats_reported_memory_unit_label=match[3],
            tegrastats_reported_utilization_fraction=used / total,
            tegrastats_reported_free_block_count=None if match[4] is None else int(match[4]),
            tegrastats_reported_free_block_size=None if match[5] is None else int(match[5])))

    # VDDX Y/Z: the page documents the first number as the block's current power consumption in
    # milliwatts and the second as its average, without stating the averaging window. Rails are never
    # summed and never turned into an energy or battery figure.
    rails = set()
    for token in re.finditer(r"(?<!\S)(VDD[A-Za-z0-9_]*) (-?[0-9]+)/(-?[0-9]+)(?=\s|$)", report):
        if token[1] in rails:
            raise ValueError("duplicate reported power rail")
        rails.add(token[1])
        if int(token[2]) < 0 or int(token[3]) < 0:
            raise ValueError("negative reported power value")
        rows.append(_tegra_row(
            "POWER_RAIL", "REPORTED_RAIL_CURRENT_AND_AVERAGE_WINDOW_UNDOCUMENTED", source_sha,
            tegrastats_block_hex="hex:" + token[1].encode().hex(),
            tegrastats_reported_power_current_w=int(token[2]) / 1000,
            tegrastats_reported_power_average_w=int(token[3]) / 1000))
    for token in re.finditer(r"(?<!\S)(VDD[A-Za-z0-9_]*)(?=\s|$)", report):
        if token[1] not in rails:
            # Only the documented "VDDX Y/Z" shape (example "VDDRQ 312/234", milliwatts stated in the
            # prose) is accepted. Any other VDD token shape - including the mW-suffixed variant some
            # builds print, which this pinned page does not show - is refused rather than guessed.
            raise ValueError("undocumented power rail token shape")

    if not rows:
        raise ValueError("no selected tegrastats report in this line")
    # Whatever the selection did not consume is counted on every row, so no block disappears quietly.
    selected = sum(len(match[0].split()) for match in cpu) + len(blocks)
    selected += sum(len(match[0].split()) for match in gpu)
    selected += sum(len(match[0].split()) for match in memory) + 2 * len(rails)
    unselected = len(report.split()) - selected
    if unselected < 0:
        raise ValueError("token accounting disagrees with the reported line")
    for row in rows:
        row[TEGRA_FIELDS.index("tegrastats_unselected_token_count")] = unselected
    return rows


def rmf_socket_state(record):
    if (type(record["protocol_version"]) is not int or record["protocol_version"] != 5
            or record["transport"] != "websocket-complete-text" or record["direction"] != "received"):
        raise ValueError("unqualified saved socket.io transport")
    packet = record["packet"]
    if not isinstance(packet, str) or not packet.startswith("42["):
        raise ValueError("complete root namespace event required")
    event = json.loads(packet[2:], object_pairs_hook=unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
    if not isinstance(event, list) or len(event) != 2 or not isinstance(event[1], dict):
        raise ValueError("one fleet event payload required")
    state = event[1]
    # Deliberately stricter than the REST body, and for a transport reason rather than a schema
    # one: the event carries the room `/fleets/NAME/state`, so a payload whose name is absent or
    # different is not the event it claims to be. The REST response is fetched from a URL the
    # caller already chose, so there the pinned model's Optional name is honoured as optional.
    if not isinstance(state.get("name"), str) or event[0] != "/fleets/" + state["name"] + "/state":
        raise ValueError("fleet event and payload identity mismatch")
    return dict(record, state=state)


def rmf_web_quantities(record, source_sha):
    """Saved web fleet status; no ROS-mode or live-health inference.

    The pose is converted only under the same caller declaration the RobotState path requires:
    Location2D at the pinned commit names `map`, `x`, `y` and `yaw` with no units and no
    descriptions, so metres and radians are the caller's statement, in the reported map frame.
    """
    if record["schema_commit"] != "a736ddebeee9eaf3970756d7dd452065273da918":
        raise ValueError("unqualified RMF Web schema")
    state = record["state"]
    def encoded(value, required=False):
        if value is None and not required:
            return None
        if not isinstance(value, str) or (required and not value):
            raise ValueError("invalid RMF Web reported identifier")
        return "hex:" + value.encode("utf-8").hex()
    declared = "location_unit" in record or "yaw_unit" in record
    if declared and (record.get("location_unit") != "m" or record.get("yaw_unit") != "rad"):
        raise ValueError("RMF location conversion requires metre and radian declarations")
    # Both are Optional in the pinned model. An absent name is carried as absent rather than
    # refused; a reported empty name stays a reported empty string, which the hex encoding already
    # distinguishes from absent. A present-but-not-a-string name is still malformed.
    fleet = encoded(state.get("name"))
    reported = state.get("robots")
    if reported is None:
        robots, count, availability = {}, None, RMF_ROBOTS_UNAVAILABLE
    elif not isinstance(reported, dict):
        raise ValueError("invalid saved fleet robot dictionary")
    else:
        robots, count = reported, len(reported)
        availability = RMF_ROBOTS_WITH if reported else RMF_ROBOTS_EMPTY
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
        pose = robot.get("location")
        if not declared:
            # Unchanged for callers that did not declare units: the pose stays in the retained
            # envelope and the columns say so rather than looking like an absent location.
            placed = [None, None, None, None, "NOT_SELECTED_WITHOUT_CALLER_UNIT_DECLARATION"]
        elif pose is None:
            # Optional and absent is not a pose at the origin.
            placed = [None, None, None, None, "NOT_REPORTED"]
        else:
            if not isinstance(pose, dict):
                raise ValueError("invalid reported RMF Web location object")
            name = pose["map"]
            if not isinstance(name, str) or not name:
                raise ValueError("invalid reported RMF Web map name")
            x, y, yaw = [quantity(pose[axis], "RMF Web reported location") for axis in ("x", "y", "yaw")]
            placed = ["hex:" + name.encode("utf-8").hex(), x, y, yaw, RMF_POSITION_BASIS]
        rows.append([fleet, encoded(key, True), encoded(robot.get("name")),
            encoded(robot.get("task_id")), fraction,
            "UNAVAILABLE" if fraction is None else "REPORTED",
            status_hex, "UNAVAILABLE" if status is None else
                "SCHEMA_KNOWN_REPORTED" if status in known else "UNKNOWN_RETAINED",
            None if stamp is None else "unix_ms:" + str(stamp), source_sha] + placed
            + [RMF_ROBOT_KIND, count, availability])
    if not rows:
        # Zero robot rows is still a read record, not a skipped one. Emitting nothing here would
        # drop the snapshot silently: the envelope, its digest and the caller's capture time ride
        # on the row, so an idle fleet would otherwise be indistinguishable from a file that was
        # never converted. Every per-robot column stays empty - no identity, pose or battery is
        # invented for a robot that was not described.
        rows.append([fleet, None, None, None, None, RMF_SNAPSHOT_BASIS, None, RMF_SNAPSHOT_BASIS,
                     None, source_sha, None, None, None, None, RMF_SNAPSHOT_BASIS,
                     RMF_SNAPSHOT_KIND, count, availability])
    return rows


def rmf_quantities(record, record_index=0, source_sha=""):
    """Saved RobotState subset, not a fleet adapter or command acknowledgement."""
    if record["schema_commit"] != "6990fc7418c412234b4ff64f01560d8bf3047f46":
        raise ValueError("unqualified RMF RobotState schema")
    # Location.msg gives no unit for x/y/yaw, so converting them needs the caller's declaration,
    # exactly as the adopted MiR status path requires an explicit metre declaration. The declaration
    # is also the opt-in: a saved record without it keeps the adopted behaviour, where location and
    # path stay in the retained JSON and no coordinate is read on the strength of a field name. A
    # declaration that is present but says something else is an error, not a silent opt-out.
    declared = "location_unit" in record or "yaw_unit" in record
    if declared and (record.get("location_unit") != "m" or record.get("yaw_unit") != "rad"):
        raise ValueError("RMF location conversion requires metre and radian declarations")
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
    selected = [percent / 100, encoded(state["name"], True), encoded(state["model"]), encoded(state["task_id"]),
            "u64:" + str(unsigned(state["seq"], 64)), number,
            "MODE_" + RMF_MODES[number] if number < len(RMF_MODES) else "UNKNOWN",
            "u64:" + str(unsigned(mode["mode_request_id"], 64)), encoded(mode["performing_action"])]
    # One STATE row, then one row per reported plan point. The battery and mode columns stay blank on
    # a plan row so a waypoint is never read as a battery or mode report.
    binding = [record_index, source_sha]
    if not declared:
        # Adopted behaviour, unchanged: the reported location and path are not converted, and the
        # columns say why rather than looking like an absent location.
        return [selected + binding + ["STATE", None, None, None, None, None, None,
                            "NOT_SELECTED_WITHOUT_CALLER_UNIT_DECLARATION", None, None, None, None,
                            "NOT_SELECTED_WITHOUT_CALLER_UNIT_DECLARATION"]]
    location_rows = rmf_location_rows(state)
    # A plan row repeats the robot name and, with it, the record's position in the file and that
    # record's digest - so two state records for the same robot at the same capture time stay
    # separable, which the name alone does not establish. Both values are computed once per record,
    # never a per-row copy of the source. Named by column rather than padded by arithmetic, so
    # adding a field cannot shift the values.
    association = [None if name != "rmf_name_hex" else selected[RMF_FIELDS.index("rmf_name_hex")]
                   for name in RMF_FIELDS]
    return [selected + binding + location_rows[0]] + [association + binding + row
                                                      for row in location_rows[1:]]


def rmf_location_rows(state):
    """The reported location and the reported *plan*, from Location.msg at the pinned commit.

    Only `approach_speed_limit` has a unit in that message ("in m/s"); x, y and yaw do not, and the
    pinned consumer neither scales nor documents them, so the envelope must declare those units and
    every row records that the declaration is the caller's. `path` is what the publisher reported it
    intends to traverse: it is never an executed or observed trajectory, nothing is reordered, and
    the message itself defines no ordering - the array order is kept as saved.
    """
    def location(source, kind, index, count):
        if not isinstance(source, dict):
            raise ValueError("invalid reported RMF location object")
        name = source["level_name"]
        if not isinstance(name, str):
            raise ValueError("invalid reported RMF level name")
        # Location.msg states no interval for yaw, so a finite reported radian value is kept exactly
        # as reported: not clipped, not wrapped into some interval, and not rejected against a range
        # no source defines. Only the caller's radian declaration is required.
        x, y, yaw = [quantity(source[key], "RMF reported location") for key in ("x", "y", "yaw")]
        stamp = source["t"]
        if not isinstance(stamp, dict):
            raise ValueError("invalid reported RMF waypoint time")
        sec, nanosec = stamp["sec"], stamp["nanosec"]
        if type(sec) is not int or type(nanosec) is not int or not 0 <= nanosec < 10**9:
            raise ValueError("invalid reported RMF waypoint time")
        if not -2**31 <= sec < 2**31:
            raise ValueError("reported RMF waypoint time outside the declared integer type")
        waypoint = source["index"]
        if type(waypoint) is not int or not 0 <= waypoint < 2**64:
            raise ValueError("invalid reported RMF waypoint index")
        obey = source["obey_approach_speed_limit"]
        if type(obey) is not bool:
            raise ValueError("invalid reported RMF speed limit flag")
        limit = quantity(source["approach_speed_limit"], "RMF approach speed limit")
        if limit < 0:
            raise ValueError("reported RMF approach speed limit below zero")
        return [kind, index, count, "hex:" + name.encode("utf-8").hex(), x, y, yaw,
                RMF_POSITION_BASIS, "u64:" + str(waypoint), sec * 10**6 + nanosec // 1000,
                RMF_TIME_BASIS,
                # The flag is the publisher's own statement about whether the limit applies; the
                # value keeps the unit the message states either way.
                limit if obey else None,
                "REPORTED_LANE_LIMIT_OBEYED" if obey else "REPORTED_NOT_OBEYED_VALUE_RETAINED_IN_SOURCE"]

    path = state["path"]
    if not isinstance(path, list):
        raise ValueError("invalid reported RMF path array")
    count = len(path)
    reported = state["location"]
    if reported is None:
        # Absent location is not a pose at the origin; the plan below is still reported.
        head = ["STATE", None, count, None, None, None, None, "NOT_REPORTED",
                None, None, None, None, "NOT_REPORTED"]
    else:
        head = location(reported, "STATE", None, count)
    # Every reported point becomes its own row, so the plan stays reconstructable from the common
    # output instead of only from the retained JSON. Nothing is sampled or truncated.
    return [head] + [location(point, "PATH_POINT", index, count)
                     for index, point in enumerate(path)]


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


def trt_layer_values(layer, iterations):
    """One reported layer of a saved exportProfile file; nothing is recomputed or repaired."""
    if not isinstance(layer, dict) or not {key for key, _ in TRT_LAYER_TIMES} | {"name", "percentage"} <= set(layer):
        raise ValueError("selected per-layer profile object required")
    name = layer["name"]
    if not isinstance(name, str):
        # The writer always quotes the runtime's layer name, so a JSON number here is a different file.
        raise ValueError("reported layer name string required")
    values = []
    for key, _ in TRT_LAYER_TIMES:
        value = layer[key]
        if type(value) not in (int, float):
            raise ValueError("numeric reported layer milliseconds required")
        try:
            value = float(value)
        except OverflowError as exc:
            raise ValueError("reported layer milliseconds overflow") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError("finite nonnegative reported layer milliseconds required")
        if value and value / 1000 == 0:
            raise ValueError("reported layer seconds underflow")
        values.append(value / 1000)
    percentage = layer["percentage"]
    if type(percentage) not in (int, float) or not math.isfinite(float(percentage)):
        raise ValueError("finite reported layer percentage required")
    percentage = float(percentage)
    # A share outside the reported range is withheld rather than clamped or zeroed; the reported
    # number itself stays in the row and in the retained record.
    share = percentage / 100 if 0 <= percentage <= 100 else None
    return ["hex:" + name.encode().hex(), *values, percentage, share,
            TRT_SHARE_BASIS if share is not None else TRT_SHARE_OUTSIDE,
            "i32:" + str(iterations), TRT_LAYER_BASIS]


def trt_profile_iterations(header):
    """The leading `{ "count": N }` object, which carries the writer's own iteration count."""
    if not isinstance(header, dict) or set(header) != {"count"}:
        raise ValueError("selected profile iteration-count header required")
    count = header["count"]
    if type(count) is not int or not 0 <= count < 2**31:
        raise ValueError("reported nonnegative int32 iteration count required")
    return count


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


def gutma_flight_logging(document):
    """Check the declared saved shape only; the GUTMA writer itself is closed source."""
    exchange = document["exchange"]
    if exchange["exchange_type"] != "flight_logging":
        raise ValueError("selected GUTMA flight logging exchange required")
    message = exchange["message"]
    if message["message_type"] != "flight_logging_submission":
        raise ValueError("selected GUTMA submission message required")
    for key, expected in GUTMA_PINS:
        if message["file"][key] != expected:
            raise ValueError("unqualified GUTMA writer declaration: " + key)
    logging = message["flight_logging"]
    if logging["uom_system"] != "Metric":
        raise ValueError("declared Metric unit system required")
    start = logging["logging_start_dtg"]
    if not isinstance(start, str) or not start:
        raise ValueError("declared logging start required")
    keys = logging["flight_logging_keys"]
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 256 or len(set(keys)) != len(keys)
            or any(not isinstance(key, str) or not key or key != key.strip() for key in keys)):
        raise ValueError("uniquely named GUTMA logging columns required")
    # Only these two columns carry a source-declared meaning; see the integration case.
    if not {"timestamp", "battery_percent"} <= set(keys):
        raise ValueError("qualified GUTMA timestamp and battery percent columns required")
    items = logging["flight_logging_items"]
    if not isinstance(items, list) or not items:
        raise ValueError("nonempty GUTMA logging items required")
    return keys, items, start


def convert_gutma_flight_logging(text):
    """Saved OpenFlight/GroundSdk GUTMA_DX flight log, not a live drone connection."""
    raw = text.encode("utf-8")
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("GUTMA document exceeds bound; split at flight boundaries")
    document = json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-JSON number")))
    if not isinstance(document, dict):
        raise ValueError("one saved GUTMA document required")
    try:
        keys, items, start = gutma_flight_logging(document)
    except (KeyError, TypeError) as exc:
        raise ValueError("incompatible saved GUTMA document") from exc
    time_index, percent_index = keys.index("timestamp"), keys.index("battery_percent")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_record_hex", *GUTMA_FIELDS])
    encoded = ("hex:" + json.dumps(keys, ensure_ascii=True, separators=(",", ":")).encode().hex(),
               "sha256:" + hashlib.sha256(raw).hexdigest(), "hex:" + start.encode("utf-8").hex())
    previous = None
    for index, item in enumerate(items):
        if not isinstance(item, list) or len(item) != len(keys):
            raise ValueError("ragged GUTMA logging item")
        if any(type(value) not in (int, float) for value in item):
            raise ValueError("numeric GUTMA logging item required")
        # Seconds relative to the declared logging start; no UTC anchor is fabricated.
        seconds = number(item[time_index], "timestamp")
        if not 0 <= seconds * 10**6 <= 2**63 - 1:
            raise ValueError("reported timestamp outside common range")
        microseconds = round(seconds * 10**6)
        if previous is not None and microseconds < previous:
            raise ValueError("decreasing reported timestamp")
        previous = microseconds
        percent = number(item[percent_index], "battery_percent")
        if not 0 <= percent <= 100:
            raise ValueError("battery percent outside reported range")
        writer.writerow([microseconds,
            "hex:" + json.dumps(item, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode().hex(),
            encoded[0], encoded[1], index, normalize_unit(percent, "%")[1],
            # The source reader treats a leading zero as not yet known; keep the value, flag it.
            "ZERO_REPORTED_MAY_BE_UNSET" if percent == 0 else "REPORTED", encoded[2],
            "REPORTED_SECONDS_FROM_DECLARED_LOGGING_START_NOT_UTC_OR_ATTESTED_ANCHOR",
            "DECLARED_METRIC_PERCENT_ONLY_VOLTAGE_CURRENT_CAPACITY_UNITS_NOT_DECLARED_IN_SOURCE"])
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("converted CSV too large; split at flight boundaries")
    return output.getvalue()


def inav_number(token, name, decimals):
    """Accept only text the pinned Configurator can write for this column."""
    pattern = r"-?(?:0|[1-9][0-9]{0,6})" + (r"(?:\.[0-9]{1,%d})?" % decimals if decimals else "")
    if not re.fullmatch(pattern, token):
        raise ValueError("invalid saved INAV value: " + name)
    return number(token, name)


def inav_signed_code(token, name, maximum):
    """A signed wire code carried exactly as reported: no scale, and no unit is invented."""
    value = inav_number(token, name, 0)
    if value != int(value) or not -maximum - 1 <= value <= maximum:
        raise ValueError("invalid saved INAV value: " + name)
    return int(value)


def inav_reported_values(values):
    """Selected saved logging columns of one host-side snapshot; no request is sent."""
    result = dict.fromkeys(INAV_FIELDS)
    if "voltage" in values:
        # MSP_ANALOG uint8/10 and MSPV2_INAV_ANALOG uint16/100 share the volt unit;
        # which handler last wrote the global is not recoverable from the CSV.
        voltage = inav_number(values["voltage"], "voltage", 2)
        current = inav_number(values["amperage"], "amperage", 2)
        drawn = integer(values["mAhdrawn"], 2**31 - 1, "mAhdrawn")
        code = integer(values["rssi"], 65535, "rssi")
        if not 0 <= voltage <= 655.35:
            raise ValueError("voltage outside both qualified MSP encodings")
        if not -327.68 <= current <= 327.67:
            raise ValueError("current outside qualified signed16 centiampere range")
        result.update(battery_voltage_v=voltage, battery_current_a=current,
                      battery_consumed_ah=drawn / 1000, battery_power_w=voltage * current,
                      # Neither handler documents a discharge sign; do not infer one.
                      battery_current_sign_basis="UNSPECIFIED", inav_rssi_code_reported=code,
                      inav_rssi_basis="REPORTED_CODE_DECLARED_0_1023_NOT_PERCENT_OR_DBM"
                      if code <= 1023 else "OUTSIDE_DECLARED_0_1023_RANGE")
    if "kinematicsX" in values:
        # X/Y are signed16 decidegrees scaled by the helper; Z is written unscaled.
        roll, pitch = (inav_number(values[key], key, 1) for key in INAV_ATTITUDE[:2])
        yaw = inav_number(values["kinematicsZ"], "kinematicsZ", 0)
        if any(not -3276.8 <= angle <= 3276.7 for angle in (roll, pitch)) or not -32768 <= yaw <= 32767:
            raise ValueError("attitude angle outside qualified signed16 encoding")
        result.update(zip(INAV_FIELDS[7:10], (normalize_unit(angle, "deg")[1] for angle in (roll, pitch, yaw))))
    if "gpsLat" in values:
        # crunch_data writes (GPS_DATA.lat / 10000000); MSPHelper stores the int32 unscaled, so
        # that division in the logger is the only scale applied. The division alone fixes the
        # scale and not the unit: the degree comes from gps.js displaying the identical expression
        # as "deg" at this same commit (see INAV_ANGLE_BASIS). No datum is named anywhere, so no
        # WGS84 or world frame is asserted.
        latitude, longitude = (inav_number(values[key], key, 7) for key in ("gpsLat", "gpsLon"))
        inside = -90 <= latitude <= 90 and -180 <= longitude <= 180
        result.update(
            inav_gps_angle_basis=INAV_ANGLE_BASIS,
            inav_gps_coordinate_disposition="REPORTED_WITHIN_ANGLE_DOMAIN" if inside
            else "OUTSIDE_ANGLE_DOMAIN_WITHHELD",
            inav_gps_fix_code_reported=integer(values["gpsFix"], 255, "gpsFix"),
            inav_gps_satellites_reported=integer(values["gpsNumSat"], 255, "gpsNumSat"),
            # The pinned handler and the logger apply no scale to these three, and no unit is
            # applied here either, so they travel as the reported integers, never converted.
            inav_gps_altitude_code_reported=inav_signed_code(values["gpsAlt"], "gpsAlt", 32767),
            inav_gps_speed_code_reported=integer(values["gpsSpeed"], 65535, "gpsSpeed"),
            inav_gps_ground_course_code_reported=integer(
                values["gpsGroundCourse"], 65535, "gpsGroundCourse"),
            inav_gps_unscaled_basis=INAV_UNSCALED_BASIS)
        if inside:
            result.update(inav_gps_latitude_deg_reported=latitude,
                          inav_gps_longitude_deg_reported=longitude)
    result["inav_value_basis"] = "HOST_SNAPSHOT_OF_ASYNCHRONOUS_MSP_RESPONSES_ZERO_MAY_BE_UNSET_NOT_VALIDATED"
    return [result[field] for field in INAV_FIELDS]


def convert_inav_logging(text):
    """Saved Configurator logging CSV (tabs/logging.js), not onboard Blackbox .TXT."""
    if (len(text.encode("utf-8")) > 16 * 1024 * 1024 or '"' in text
            or any(ord(c) < 32 and c not in "\r\n" for c in text)):
        raise ValueError("unsupported INAV logging CSV representation or size")
    lines = text.splitlines()
    rows = csv.reader(lines, strict=True)
    header = next(rows, [])
    if (len(header) < 2 or len(header) > 256 or header[0] != "timestamp"
            or len(set(header)) != len(header) or any(not c or c != c.strip() for c in header)):
        raise ValueError("selected INAV logging header required")
    present = set(header)
    # A partial group is not a print_head output; never convert half a message.
    if any(0 < len(present & set(group)) < len(group)
           for group in (INAV_ANALOG, INAV_ATTITUDE, INAV_GPS)):
        raise ValueError("incomplete selected MSP column group")
    if not any(present >= set(group) for group in (INAV_ANALOG, INAV_ATTITUDE, INAV_GPS)):
        raise ValueError("no qualified MSP_ANALOG, MSP_ATTITUDE or MSP_RAW_GPS column group")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_record_hex", *INAV_FIELDS])
    header_hex = "hex:" + lines[0].encode("utf-8").hex()
    previous, count = None, 0
    for index, row in enumerate(rows, 1):
        if len(row) != len(header):
            raise ValueError("ragged or truncated INAV logging row")
        values = dict(zip(header, row))
        # millitime() is the host wall clock: reject a decreasing file, never reorder.
        milliseconds = integer(values["timestamp"], (2**63 - 1) // 1000, "timestamp")
        if previous is not None and milliseconds < previous:
            raise ValueError("decreasing host clock")
        previous = milliseconds
        selected = inav_reported_values(values)
        selected[-1] = header_hex
        writer.writerow([milliseconds * 1000, "hex:" + lines[index].encode("utf-8").hex()]
                        + ["" if value is None else value for value in selected])
        count += 1
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("converted CSV too large; split at record boundaries")
    if not count:
        raise ValueError("no INAV logging records")
    return output.getvalue()


def convert_hailo_profile(text, capture_time_us, source_format=HAILO_FORMAT):
    """One saved successful-infer CSV. The two pinned versions share the writer; only the
    source-established frame-count basis differs, so it is stated per row, never assumed."""
    if source_format not in HAILO_FRAME_BASIS:
        raise ValueError("explicit pinned Hailo runtime version required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit nonnegative signed64 capture microseconds required")
    raw = text.encode("utf-8")
    if len(raw) > 16 * 1024 * 1024 or '\x00' in text or '"' in text:
        raise ValueError("unsupported Hailo CSV representation or size")
    rows = csv.reader(io.StringIO(text))
    header = next(rows, [])
    if (tuple(header[:len(HAILO_HEADER)]) != HAILO_HEADER or len(set(header)) != len(header)
            or any(not key or key.strip() != key for key in header)):
        raise ValueError("selected Hailo inference CSV header required")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_document_sha256", "source_event_index", "canonical_event_hex",
        "runtime_name_hex", "runtime_status_reported", "runtime_fps_reported", "runtime_frame_count_text",
        "runtime_hw_latency_s", "runtime_overall_latency_s", "runtime_time_basis",
        "runtime_frame_count_basis"])
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    count = 0
    for index, row in enumerate(rows):
        if len(row) != len(header):
            raise ValueError("ragged Hailo inference row")
        event = dict(zip(header, row))
        if not event["net_name"] or integer(event["status"], 2**32 - 1, "Hailo status") != 0:
            raise ValueError("successful Hailo inference row required; failure report not repaired")
        values = []
        for key in ("fps", "hw_latency", "overall_latency"):
            value = number(event[key], key) if event[key] else None
            if value is not None and value < 0:
                raise ValueError("negative Hailo rate or duration")
            if value is not None and key != "fps":
                if value and value / 1000 == 0:
                    raise ValueError("runtime seconds underflow")
                value /= 1000
            values.append(value)
        frames = integer(event["num_of_frames"], 2**64 - 1, "frame count") if event["num_of_frames"] else None
        encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode()
        writer.writerow([capture_time_us, digest, index, "hex:" + encoded.hex(),
            "hex:" + event["net_name"].encode().hex(), "SUCCESS_REPORTED_NOT_DEVICE_VALIDATION",
            values[0], "u64:" + str(frames) if frames is not None else None, *values[1:],
            "REPORTED_DURATION_NOT_CAPTURE_OR_SHARED_CLOCK", HAILO_FRAME_BASIS[source_format]])
        count += 1
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("runtime profile output exceeds bound")
    if not count:
        raise ValueError("empty Hailo inference report")
    return output.getvalue()


def convert_runtime_profile(text, capture_time_us, source_format=ORT_FORMAT):
    if source_format not in (ORT_FORMAT, TRT_FORMAT, TRT_PROFILE_FORMAT):
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
    columns = list(TRT_LAYER_FIELDS) if source_format == TRT_PROFILE_FORMAT else (
        ["trt_" + field for _, field in TRT_TIMES] + ["runtime_time_basis"] if source_format == TRT_FORMAT else [
        "runtime_category_hex", "runtime_category_status", "runtime_name_hex",
        "runtime_pid_text", "runtime_tid_text", "runtime_start_us_text", "runtime_duration_us_text",
        "runtime_start_offset_s", "runtime_duration_s", "runtime_time_basis"])
    writer.writerow(["record_time_us", "source_document_sha256", "source_event_index", "canonical_event_hex"] + columns)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    # The per-layer file leads with the iteration-count object, so the layers start at index 1 and keep
    # their original position: an empty layer list is a reported empty profile and is refused, never
    # emitted as a zero row.
    iterations = trt_profile_iterations(records[0]) if source_format == TRT_PROFILE_FORMAT else None
    if source_format == TRT_PROFILE_FORMAT and len(records) < 2:
        raise ValueError("reported profile carries no layer")
    for index, event in enumerate(records):
        if source_format == TRT_PROFILE_FORMAT:
            if not index:
                continue
            values = trt_layer_values(event, iterations)
        else:
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
        + (list(BATTERY_TEMPERATURE_FIELDS) if source_format in TEMPERATURE_FORMATS else [])
        + (list(VDA_FIELDS) if source_format.startswith("vda-") else [])
        + (list(MAVSDK_FIELDS) if source_format in ("mavsdk-battery-json", PROTO_FORMAT, GRPC_FORMAT) else [])
        + (list(DJI_FIELDS) if source_format == DJI_FORMAT else [])
        + (list(ROBOTNIK_FIELDS) if source_format == ROBOTNIK_FORMAT else []))
    detailed = vda_actions or vda_errors
    if source_format == FACTSHEET_FORMAT:
        selected_fields = list(FACTSHEET_FIELDS)
    if source_format in CONNECTION_FORMATS:
        selected_fields = list(CONNECTION_FIELDS)
    if source_format == DOCKER_FORMAT:
        selected_fields = list(DOCKER_FIELDS)
    if source_format == DOCKER_STATE_FORMAT:
        selected_fields = list(DOCKER_STATE_FIELDS)
    if source_format == TEGRA_FORMAT:
        selected_fields = list(TEGRA_FIELDS)
    if source_format == RMF_FORMAT:
        selected_fields = list(RMF_FIELDS) + list(RMF_LOCATION_FIELDS)
    if source_format in (RMF_WEB_FORMAT, RMF_SOCKET_FORMAT):
        selected_fields = list(RMF_WEB_FIELDS)
    if source_format == FARMDUINO_FORMAT:
        selected_fields = list(FARM_FIELDS) + ["farmduino_q_reported_text", "farmduino_position_basis"]
    if source_format == FARM_POINT_FORMAT:
        selected_fields = list(FARM_POINT_FIELDS)
    if source_format == FARM_DIGITAL_FORMAT:
        selected_fields = list(FARM_DIGITAL_FIELDS)
    if source_format == MIR_FORMAT:
        selected_fields = list(MIR_FIELDS)
    if source_format == PING_FORMAT:
        selected_fields = list(PING_FIELDS)
    if source_format == PARROT_FORMAT:
        selected_fields = list(PARROT_FIELDS)
    if source_format == MSDK_FORMAT:
        selected_fields = list(MSDK_FIELDS)
    if source_format in ATTITUDE_FORMATS:
        selected_fields = list(ATTITUDE_FIELDS)
    if source_format in (PROTO_FORMAT, GRPC_FORMAT, *ATTITUDE_WIRE):
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
        if not line.strip() or len(line.encode("utf-8")) > (512 * 1024 if detailed or source_format in (FACTSHEET_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, DOCKER_STATE_FORMAT) else 30000):
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
            elif source_format == FARM_DIGITAL_FORMAT:
                values = farmbot_digital_reading(record)
            elif source_format == MIR_FORMAT:
                values = mir_saved_status(record)
            elif source_format == RMF_FORMAT:
                values = rmf_quantities(record, record_index,
                                        "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest())
            elif source_format in (RMF_WEB_FORMAT, RMF_SOCKET_FORMAT):
                state_record = rmf_socket_state(record) if source_format == RMF_SOCKET_FORMAT else record
                values = rmf_web_quantities(state_record, "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest())
            elif source_format == DOCKER_FORMAT:
                values = docker_quantities(record)
            elif source_format == DOCKER_STATE_FORMAT:
                values = docker_state(record)
            elif source_format == TEGRA_FORMAT:
                values = tegrastats_quantities(record, "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest())
            elif source_format == FACTSHEET_FORMAT:
                values = factsheet_quantities(record)
            elif source_format in CONNECTION_FORMATS:
                values = vda_connection_state(record, source_format)
            elif source_format == DJI_FORMAT:
                values = dji_batteries(record)
            elif source_format == ROBOTNIK_FORMAT:
                values = robotnik_battery(record)
            elif source_format in ATTITUDE_WIRE:
                values = mavsdk_attitude_protobuf(record, grpc=source_format == ATTITUDE_GRPC)
            elif source_format == ATTITUDE_JSON:
                values = mavsdk_attitude(record)
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
        for row_index, row_values in enumerate(values if source_format in (DJI_FORMAT, ROBOTNIK_FORMAT, RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, TEGRA_FORMAT) else [values]):
            source_hex = "" if source_format in (RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, TEGRA_FORMAT) and row_index else "hex:" + line.encode("utf-8").hex()
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
        if output.tell() > (128 if source_format in (ROBOTNIK_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, TEGRA_FORMAT) else 16) * 1024 * 1024:
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
        if args.vda_actions or args.vda_errors or args.source_format in (INAV_FORMAT, GUTMA_FORMAT, FACTSHEET_FORMAT, DOCKER_FORMAT, DOCKER_STATE_FORMAT, TEGRA_FORMAT, ORT_FORMAT, TRT_FORMAT, TRT_PROFILE_FORMAT, HAILO_FORMAT, HAILO_FORMAT_424, RMF_FORMAT, RMF_WEB_FORMAT, RMF_SOCKET_FORMAT, MIR_FORMAT, FARM_POINT_FORMAT, FARM_DIGITAL_FORMAT, FARMDUINO_FORMAT, PING_FORMAT, PARROT_FORMAT, MSDK_FORMAT, PROTO_FORMAT, GRPC_FORMAT, *ATTITUDE_FORMATS):
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
