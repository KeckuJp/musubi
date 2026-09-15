#!/usr/bin/env python3
"""Passive, explicitly typed ROS record conversion; no ROS runtime or transport."""
import argparse
import csv
import hashlib
from dataclasses import fields, is_dataclass
import io
import json
import math
import re
import struct
import tempfile
from pathlib import Path
import numpy as np
from rosbags.rosbag2 import Reader
from rosbags.rosbag1 import Reader as Reader1
from rosbags.rosbag1.reader import decompressors as ROS1_DECOMPRESSORS
from rosbags.typesys import Stores, get_typestore, get_types_from_msg, get_types_from_idl
from rosbags.interfaces import MessageDefinitionFormat
from ruamel.yaml import YAML
import zstandard

try:
    from scripts.convert_ardupilot_battery_csv import JSON_ELECTRICAL_FIELDS, json_quantities, json_time, unique_object
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import JSON_ELECTRICAL_FIELDS, json_quantities, json_time, unique_object

STORE = get_typestore(Stores.ROS2_HUMBLE)
ROS1_STORE = get_typestore(Stores.ROS1_NOETIC)
ROS1_HASHES = {"sensor_msgs/msg/BatteryState": "4ddae7f048e32fda22cac764685e3974",
               "sensor_msgs/msg/Imu": "6a62c6daae103f4ff57a132d6f95cec2"}
VELOCITY_REPORT = "autoware_vehicle_msgs/msg/VelocityReport"
STEERING_REPORT = "autoware_vehicle_msgs/msg/SteeringReport"
# Functional field layouts at autoware_msgs bb8e7bf, with existing Humble dependencies.
VEHICLE_DEFINITIONS = {
    VELOCITY_REPORT: "std_msgs/Header header\nfloat32 longitudinal_velocity\nfloat32 lateral_velocity\nfloat32 heading_rate\n",
    STEERING_REPORT: "builtin_interfaces/Time stamp\nfloat32 steering_tire_angle\n"}
for _typename, _definition in VEHICLE_DEFINITIONS.items():
    STORE.register(get_types_from_msg(_definition, _typename))
VEHICLE_FIELDS = ["vehicle_report_stamp_us", "vehicle_report_frame_hex", "vehicle_velocity_longitudinal_m_s",
                  "vehicle_velocity_lateral_m_s", "vehicle_heading_rate_rad_s", "vehicle_steering_tire_angle_rad"]
NAV2_FEEDBACK = "nav2_msgs/action/NavigateToPose_FeedbackMessage"
# Fixed NavigateToPose 1.1.20 feedback plus the standard ROSIDL action envelope.
NAV2_DEFINITIONS = {
    "nav2_msgs/action/NavigateToPose_Feedback":
        "geometry_msgs/PoseStamped current_pose\nbuiltin_interfaces/Duration navigation_time\n"
        "builtin_interfaces/Duration estimated_time_remaining\nint16 number_of_recoveries\nfloat32 distance_remaining\n",
    NAV2_FEEDBACK: "unique_identifier_msgs/UUID goal_id\nnav2_msgs/action/NavigateToPose_Feedback feedback\n"}
for _typename, _definition in NAV2_DEFINITIONS.items():
    STORE.register(get_types_from_msg(_definition, _typename))
RANGE_STORE = get_typestore(Stores.ROS2_IRON)
RANGE = "sensor_msgs/msg/Range"
RANGE_VARIANCE_HASH = "RIHS01_b42b62562e93cbfe9d42b82fe5994dfa3d63d7d5c90a317981703f7388adff3a"
BATTERY = "sensor_msgs/msg/BatteryState"
IMU = "sensor_msgs/msg/Imu"
DIAGNOSTIC = "diagnostic_msgs/msg/DiagnosticArray"
ODOMETRY = "nav_msgs/msg/Odometry"
HASHES = {BATTERY: "RIHS01_4bee5dfce981c98faa6828b868307a0a73f992ed0789f374ee96c8f840e69741",
          IMU: "RIHS01_7d9a00ff131080897a5ec7e26e315954b8eae3353c3f995c55faf71574000b5b",
          DIAGNOSTIC: "RIHS01_5a8a36efb05fb25070fa0fb3810290c0e6cd4862b54a8fb975a1ee8dc55a333e",
          ODOMETRY: "RIHS01_3cc97dc7fb7502f8714462c526d369e35b603cfc34d946e3f2eda2766dfec6e0"}
SENSOR_HASHES = {
    "LaserScan": "64c191398013af96509d518dac71d5164f9382553fce5c1f8cca5be7924bd828",
    "Range": "9430b1915b94d4268ff903679e8ecd09b6a67d331bd028738ec1eeb592891ebd",
    "Temperature": "72514a14126ab9f8a9abec974c78e5610a367b59db5da355ff1fb982d5bad4b8",
    "FluidPressure": "22dfb2b145a0bd5a31a1ac3882a1b32148b51d9b2f3bab250290d66f3595bc32",
    "RelativeHumidity": "8687c99b4fb393cb2e545e407b5ea7fd0b5d8960bcd849a0f86c544740138839",
    "MagneticField": "e80f32f56a20486c9923008fc1a1db07bbb273cbbf6a5b3bfa00835ee00e4dff",
    "Illuminance": "b954b25f452fcf81a91c9c2a7e3b3fd85c4c873d452aecb3cfd8fd1da732a22d"}
HASHES.update({"sensor_msgs/msg/" + name: "RIHS01_" + digest for name, digest in SENSOR_HASHES.items()})
HASHES[NAV2_FEEDBACK] = "RIHS01_9d1f6fa2eb975ce733b419ac2e97b134d8ff92b6478b6559feed9a50bdfd82b3"
HASHES[VELOCITY_REPORT] = "RIHS01_9052adda949c32f4a98500abc1fb5bd23f2560e321eebdfbb25318d6108d4ce4"
HASHES[STEERING_REPORT] = "RIHS01_aa3acc9ca95ebc4daf9dec0ecf87911ad9c196392857c3026bfead589db65a94"
NAV2_FIELDS = ["nav2_pose_disposition", "nav2_navigation_time_ns", "nav2_estimated_time_remaining_ns",
               "nav2_estimated_time_disposition", "nav2_distance_remaining_m", "nav2_distance_disposition",
               "nav2_number_of_recoveries"]
TF = "tf2_msgs/msg/TFMessage"
JOINT = "sensor_msgs/msg/JointState"
ACTION_STATUS = "action_msgs/msg/GoalStatusArray"
PARAMETER_EVENT = "rcl_interfaces/msg/ParameterEvent"
HASHES[PARAMETER_EVENT] = "RIHS01_043e627780fcad87a22d225bc2a037361dba713fca6a6b9f4b869a5aa0393204"
PARAMETER_FIELDS = ["parameter_event_kind", "parameter_node_hex", "parameter_name_hex",
                    "parameter_source_stamp_us", "parameter_type_code", "parameter_type_reported",
                    "parameter_value_status", "parameter_element_count", "parameter_element_index",
                    "parameter_bool_reported", "parameter_integer_reported", "parameter_double_reported",
                    "parameter_text_hex", "parameter_event_entry_count"]
HASHES[ACTION_STATUS] = "RIHS01_6c1684b00f177d37438febe6e709fc4e2b0d4248dca4854946f9ed8b30cda83e"
POINT_CLOUD = "sensor_msgs/msg/PointCloud2"
NAVSAT = "sensor_msgs/msg/NavSatFix"
HASHES[NAVSAT] = "RIHS01_62223ab3fe210a15976021da7afddc9e200dc9ec75231c1b6a557fc598a65404"
NAVSAT_FIELDS = ["navsat_status_code", "navsat_status_reported", "navsat_service_bits",
                 "navsat_position_status", "navsat_latitude_deg", "navsat_longitude_deg",
                 "navsat_altitude_wgs84_ellipsoid_m", "navsat_altitude_status",
                 "navsat_covariance_type_code", "navsat_covariance_status", "navsat_covariance_basis",
                 "navsat_frame_hex", "navsat_source_stamp_us"]
NAVSAT_FIELDS += ["navsat_covariance_" + str(index) + "_m2" for index in range(9)]
NAVSAT_FIELDS += ["navsat_service_" + name for name in ("gps", "glonass", "compass", "galileo", "unknown_bits")]
HASHES[POINT_CLOUD] = "RIHS01_9198cabf7da3796ae6fe19c4cb3bdd3525492988c70522628af5daa124bae2b5"
CLOUD_FIELDS = ["point_count", "point_index", "point_row", "point_column", "point_frame_hex",
                "point_source_stamp_us", "point_unit_basis", "point_status", "point_unmapped_fields_hex",
                "point_x_m", "point_y_m", "point_z_m"]
ACTION_FIELDS = ["action_entry_count", "action_entry_index", "action_uuid_hex",
                 "action_accepted_stamp_us", "action_status_code", "action_status_reported"]
STAMPED_MOTION = {
    "PoseStamped": "10f3786d7d40fd2b54367835614bff85d4ad3b5dab62bf8bca0cc232d73b4cd8",
    "PoseWithCovarianceStamped": "26432f9803e43727d3c8f668d1fdb3c630f548af631e2f4e31382371bfea3b6e",
    "TwistStamped": "5f0fcd4f81d5d06ad9b4c4c63e3ea51b82d6ae4d0558f1d475229b1121db6f64",
    "TwistWithCovarianceStamped": "77b67434531e6529b7a0091357b186b6ebdb17fd9ffd3e0c7ce9d3fb11a44563"}
STAMPED_MOTION = {"geometry_msgs/msg/" + name: "RIHS01_" + digest for name, digest in STAMPED_MOTION.items()}
HASHES.update(STAMPED_MOTION)
HASHES[JOINT] = "RIHS01_a13ee3a330e346c9d87b5aa18d24e11690752bd33a0350f11c5882bc9179260e"
JOINT_FIELDS = ["joint_count", "joint_index", "joint_name_hex", "joint_kind_configured", "joint_source_stamp_us"]
JOINT_FIELDS += ["joint_" + quantity + "_" + unit for quantity, units in
                 (("position", ("rad", "m")), ("velocity", ("rad_s", "m_s")), ("effort", ("nm", "n"))) for unit in units]
JOINT_FIELDS += ["joint_" + quantity + "_status" for quantity in ("position", "velocity", "effort")]
HASHES[TF] = "RIHS01_e369d0f05a23ae52508854b66f6aa0437f3449d652e8cbf22d5abe85d020f087"
TF_FIELDS = ["tf_entry_count", "tf_entry_index", "tf_parent_frame_hex", "tf_child_frame_hex", "tf_source_stamp_us"]
TF_FIELDS += ["tf_translation_m_" + a for a in "xyz"] + ["tf_rotation_xyzw_" + a for a in "xyzw"]
SCALARS = {
    "Temperature": ("temperature", "temperature_k", 273.15, 0., None),
    "FluidPressure": ("fluid_pressure", "external_pressure_pa", 0., 0., None),
    "RelativeHumidity": ("relative_humidity", "relative_humidity_fraction", 0., 0., 1.),
    "Illuminance": ("illuminance", "illuminance_lux", 0., 0., None)}
SENSOR_FIELDS = [entry[1] for entry in SCALARS.values()] + [
    "sensor_variance_status", "sensor_frame_hex", "magnetic_covariance_status"]
SENSOR_FIELDS += ["magnetic_field_tesla_" + a for a in "xyz"]
SENSOR_FIELDS += ["magnetic_field_" + a + "_status" for a in "xyz"]
SENSOR_FIELDS += ["range_m", "range_status", "range_min_m", "range_max_m", "range_fov_rad",
                  "range_radiation_code", "range_radiation_reported", "range_variance_m2", "range_variance_status"]
SCAN_FIELDS = ["scan_ray_count", "scan_ray_index", "scan_angle_rad", "scan_first_stamp_us",
               "scan_ray_offset_s", "scan_period_s", "scan_intensity_device_units", "scan_intensity_status"]
LIMIT = 16 * 1024 * 1024
BASE = ["record_time_us", "source_record_hex", *JSON_ELECTRICAL_FIELDS]
GROUPS = (("orientation", "xyzw", "orientation_quaternion"),
          ("angular_velocity", "xyz", "angular_velocity_rad_s"),
          ("linear_acceleration", "xyz", "linear_acceleration_m_s2"))
IMU_FIELDS = [f"{prefix}_{axis}" for _, axes, prefix in GROUPS for axis in axes]
IMU_FIELDS += [f"{name}_covariance_status" for name, _, _ in GROUPS]
DIAG_FIELDS = ["diagnostic_entry_count", "diagnostic_entry_index", "diagnostic_level_reported",
               "diagnostic_level_code", "diagnostic_name_hex", "diagnostic_message_hex", "diagnostic_hardware_id_hex"]
MOTION_GROUPS = (("pose", "pose", "position", "xyz", "pose_position_m"),
                 ("pose", "pose", "orientation", "xyzw", "pose_orientation_quaternion"),
                 ("twist", "twist", "linear", "xyz", "twist_linear_m_s"),
                 ("twist", "twist", "angular", "xyz", "twist_angular_rad_s"))
MOTION_FIELDS = [f"{prefix}_{axis}" for _, _, _, axes, prefix in MOTION_GROUPS for axis in axes]
MOTION_FIELDS += ["pose_frame_hex", "twist_frame_hex", "pose_covariance_status", "twist_covariance_status"]
MOTION_FIELDS += ["motion_source_role_configured", "motion_source_stamp_us"]


def plain(value):
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


def action_status_quantities(record):
    entries = record["status_list"]
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ValueError("invalid bounded action status list")
    if not entries:
        return [dict(action_entry_count=0, action_status_reported="EMPTY_ARRAY")]
    names = ("UNKNOWN", "ACCEPTED", "EXECUTING", "CANCELING", "SUCCEEDED", "CANCELED", "ABORTED")
    output = []
    for index, entry in enumerate(entries):
        code = entry["status"]
        identifier = entry["goal_info"]["goal_id"]["uuid"]
        if type(code) is not int or not -128 <= code <= 127:
            raise ValueError("invalid action status int8")
        if not isinstance(identifier, list) or len(identifier) != 16 or any(
                type(byte) is not int or not 0 <= byte <= 255 for byte in identifier):
            raise ValueError("invalid action UUID bytes")
        time = json_time({"header": {"stamp": entry["goal_info"]["stamp"], "frame_id": ""}}, "ros-battery-json")
        output.append(dict(action_entry_count=len(entries), action_entry_index=index,
            action_uuid_hex="hex:" + bytes(identifier).hex(), action_accepted_stamp_us=time,
            action_status_code=code,
            action_status_reported=(names[code] + "_REPORTED") if 0 <= code < len(names) else f"UNKNOWN_{code}"))
    return output


def parameter_event_quantities(record, *, declaration=False):
    stamp = None if declaration else json_time({"header": {"stamp": record["stamp"], "frame_id": ""}}, "ros-battery-json")
    node = record["node"]
    if not isinstance(node, str) or not node or (not declaration and not node.startswith("/")):
        raise ValueError("parameter event requires reported fully qualified node path")
    names, output = set(), []
    kinds = ("NOT_SET", "BOOL", "INTEGER", "DOUBLE", "STRING", "BYTE_ARRAY", "BOOL_ARRAY",
             "INTEGER_ARRAY", "DOUBLE_ARRAY", "STRING_ARRAY")
    value_fields = (None, "bool_value", "integer_value", "double_value", "string_value",
                    "byte_array_value", "bool_array_value", "integer_array_value", "double_array_value", "string_array_value")
    for group, event in (("new_parameters", "NEW_REPORTED"), ("changed_parameters", "CHANGED_REPORTED"),
                         ("deleted_parameters", "DELETED_REPORTED")):
        entries = record[group]
        if not isinstance(entries, list) or len(entries) > 4096:
            raise ValueError("invalid bounded parameter event list")
        for entry in entries:
            name, value = entry["name"], entry["value"]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("ambiguous parameter name in atomic event")
            names.add(name)
            code = value["type"]
            if type(code) is not int or not 0 <= code <= 255:
                raise ValueError("invalid parameter type code")
            base = dict(parameter_event_kind="DECLARED_FILE" if declaration else event, parameter_node_hex=text_hex(node),
                        parameter_name_hex=text_hex(name), parameter_source_stamp_us=stamp,
                        parameter_type_code=code, parameter_type_reported=kinds[code] if code < 10 else "UNKNOWN",
                        parameter_value_status="DECLARED_NOT_APPLIED" if declaration else "REPORTED_NOT_CURRENT_SETTING")
            if code == 0 or code >= 10:
                output.append(dict(base, parameter_value_status="NOT_SET" if code == 0 else "UNKNOWN_TYPE_RETAINED"))
                continue
            selected = value[value_fields[code]]
            elements = selected if code >= 5 else [selected]
            if not isinstance(elements, list) or len(elements) > 4096:
                raise ValueError("invalid bounded parameter array")
            if not elements:
                output.append(dict(base, parameter_element_count=0, parameter_value_status="EMPTY_ARRAY"))
            for index, item in enumerate(elements):
                row = dict(base, parameter_element_count=len(elements), parameter_element_index=index)
                if code in (1, 6):
                    if type(item) is not bool:
                        raise ValueError("parameter bool required")
                    row["parameter_bool_reported"] = int(item)
                elif code in (2, 5, 7):
                    if type(item) is not int or not (-128 <= item <= 255 if code == 5 else -(2**63) <= item < 2**63):
                        raise ValueError("parameter integer range")
                    # rosbags maps ROS byte to int8; expose the octet, not a negative count.
                    row["parameter_integer_reported"] = item & 255 if code == 5 else item
                elif code in (3, 8):
                    if item in ("NaN", "Infinity", "-Infinity"):
                        row["parameter_value_status"] = "NONFINITE_RETAINED"
                    elif type(item) not in (float, int) or not math.isfinite(item):
                        raise ValueError("parameter double required")
                    else:
                        row["parameter_double_reported"] = item
                else:
                    row["parameter_text_hex"] = text_hex(item)
                output.append(row)
            if len(output) > 4096:
                raise ValueError("parameter event expansion bound")
    if not output:
        output = [dict(parameter_event_kind="EMPTY_DECLARATION" if declaration else "EMPTY_EVENT", parameter_node_hex=text_hex(node), parameter_source_stamp_us=stamp)]
    if len(output) > 4096:
        raise ValueError("parameter event expansion bound")
    for row in output:
        row["parameter_event_entry_count"] = len(names)
    return output


def decode(raw, typename, store=STORE, *, ros1=False):
    if ros1:
        definition = ROS1_STORE.get_msgdef(typename)
        message, consumed = definition.deserialize_ros1(raw, 0, definition.cls, ROS1_STORE)
        if consumed != len(raw):
            raise ValueError("ROS1 payload length mismatch")
        return plain(message)
    # Do not inherit the external decoder's permissive header/end-padding handling.
    if len(raw) < 4 or raw[:4] not in (b"\x00\x00\x00\x00", b"\x00\x01\x00\x00"):
        raise ValueError("unsupported CDR representation")
    definition = store.get_msgdef(typename)
    method = definition.deserialize_cdr_le if raw[1] else definition.deserialize_cdr_be
    message, consumed = method(raw[4:], 0, definition.cls, store)
    tail = raw[4 + consumed:]
    if consumed + 4 > len(raw) or len(tail) > 3 or any(tail):
        raise ValueError("CDR payload length mismatch")
    return plain(message)


def imu_quantities(record):
    result = {}
    for name, axes, prefix in GROUPS:
        covariance = record[f"{name}_covariance"]
        if len(covariance) != 9:
            raise ValueError("invalid covariance size")
        absent = covariance[0] == -1
        if not absent and any(type(v) not in (int, float) or not math.isfinite(v) for v in covariance):
            raise ValueError("invalid covariance")
        result[f"{name}_covariance_status"] = "ABSENT" if absent else (
            "UNKNOWN" if not any(covariance) else "REPORTED")
        for axis in axes:
            value = record[name][axis]
            if not absent and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("nonfinite IMU estimate")
            result[f"{prefix}_{axis}"] = None if absent else value
    return result


def text_hex(value):
    if not isinstance(value, str):
        raise ValueError("invalid text field")
    return "hex:" + value.encode("utf-8").hex()


def diagnostic_quantities(record):
    entries = record["status"]
    if not entries:
        return [{"diagnostic_entry_count": 0, "diagnostic_level_reported": "EMPTY_ARRAY"}]
    result = []
    for index, entry in enumerate(entries):
        code = entry["level"]
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError("invalid diagnostic level")
        result.append({"diagnostic_entry_count": len(entries), "diagnostic_entry_index": index,
            "diagnostic_level_code": code,
            "diagnostic_level_reported": {0: "OK", 1: "WARN", 2: "ERROR", 3: "STALE"}.get(code, f"UNKNOWN_{code}"),
            "diagnostic_name_hex": text_hex(entry["name"]),
            "diagnostic_message_hex": text_hex(entry["message"]),
            "diagnostic_hardware_id_hex": text_hex(entry["hardware_id"])})
    return result


def motion_quantities(record):
    parent, child = record["header"]["frame_id"], record["child_frame_id"]
    if not parent or not child:
        raise ValueError("Odometry frame labels required; no implicit world frame")
    result = motion_part(record["pose"], "pose", parent)
    result.update(motion_part(record["twist"], "twist", child))
    return result


def motion_part(record, name, frame):
    if not frame:
        raise ValueError("explicit motion frame required")
    result = {name + "_frame_hex": text_hex(frame)}
    for outer, inner, group, axes, prefix in MOTION_GROUPS:
        if outer != name:
            continue
        for axis in axes:
            value = record[inner][group][axis]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("nonfinite motion estimate")
            result[f"{prefix}_{axis}"] = value
    if name == "pose":
        norm2 = sum(result[f"pose_orientation_quaternion_{a}"] ** 2 for a in "xyzw")
        if not math.isfinite(norm2) or abs(norm2 - 1) > 1e-6:
            raise ValueError("motion requires a unit quaternion; not silently normalized")
    result[f"{name}_covariance_status"] = "NOT_PROVIDED"
    if "covariance" in record:
        values = record["covariance"]
        if len(values) != 36 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError("invalid Odometry covariance")
        result[f"{name}_covariance_status"] = "REPORTED" if any(values) else "REPORTED_ALL_ZERO"
    return result


def stamped_motion_quantities(record, typename, role):
    if role not in ("reported_estimate", "reported_reference"):
        raise ValueError("explicit stamped-motion source role required")
    name = "pose" if typename.rsplit("/", 1)[-1].startswith("Pose") else "twist"
    data = record[name] if "WithCovariance" in typename else {name: record[name]}
    row = motion_part(data, name, record["header"]["frame_id"])
    row.update(motion_source_role_configured=role,
               motion_source_stamp_us=json_time(record, "ros-battery-json"))
    return row


def nav2_feedback_quantities(record):
    uid = record["goal_id"]["uuid"]
    if not isinstance(uid, list) or len(uid) != 16 or any(type(v) is not int or not 0 <= v <= 255 for v in uid):
        raise ValueError("fixed action UUID required")
    data = record["feedback"]
    row = {"action_uuid_hex": "hex:" + bytes(uid).hex()}
    for key, target in (("navigation_time", "nav2_navigation_time_ns"),
                        ("estimated_time_remaining", "nav2_estimated_time_remaining_ns")):
        value = data[key]
        sec, ns = value["sec"], value["nanosec"]
        if type(sec) is not int or not -(2**31) <= sec < 2**31 or type(ns) is not int or not 0 <= ns < 10**9:
            raise ValueError("invalid reported duration")
        row[target] = sec * 10**9 + ns
    eta = row["nav2_estimated_time_remaining_ns"]
    distance = finite(data["distance_remaining"])
    count = data["number_of_recoveries"]
    if eta < 0 or distance < 0 or type(count) is not int or not 0 <= count <= 32767:
        raise ValueError("invalid navigation estimate or recovery count")
    row.update(nav2_estimated_time_disposition="ZERO_OR_NOT_ESTIMATED" if eta == 0 else "REPORTED_ESTIMATE",
               nav2_distance_remaining_m=distance,
               nav2_distance_disposition="ZERO_OR_UNAVAILABLE" if distance == 0 else "REPORTED_PATH_LENGTH",
               nav2_number_of_recoveries=count)
    pose = data["current_pose"]
    json_time(pose, "ros-battery-json")  # The wrapper has no header; its nested pose does.
    if pose["header"]["frame_id"] == "":
        row["nav2_pose_disposition"] = "MISSING_FRAME_NOT_NORMALIZED"
    else:
        row.update(stamped_motion_quantities(pose, "geometry_msgs/msg/PoseStamped", "reported_estimate"))
        row["nav2_pose_disposition"] = "REPORTED_ESTIMATE"
    return row


def navsat_quantities(record):
    status = record["status"]["status"]
    covariance_type = record["position_covariance_type"]
    result = {"navsat_status_code": status, "navsat_status_reported": {
        -1: "NO_FIX", 0: "FIX", 1: "SBAS_FIX", 2: "GBAS_FIX"}.get(status, "UNKNOWN"),
        "navsat_service_bits": record["status"]["service"],
        "navsat_position_status": "FIX_REPORTED" if status >= 0 else "NO_VALID_FIX_REPORTED",
        "navsat_covariance_type_code": covariance_type, "navsat_covariance_status": "UNAVAILABLE",
        "navsat_covariance_basis": "REPORTED_LOCAL_ENU_NOT_WORLD_FRAME",
        "navsat_frame_hex": text_hex(record["header"]["frame_id"]),
        "navsat_source_stamp_us": json_time(record, "ros-battery-json"),
        "navsat_altitude_status": "NO_VALID_FIX_REPORTED"}
    service = record["status"]["service"]
    result.update({"navsat_service_" + name: "true" if service & bit else "false"
                   for name, bit in (("gps", 1), ("glonass", 2), ("compass", 4), ("galileo", 8))})
    result["navsat_service_unknown_bits"] = service & ~15
    if status < 0:
        return result  # Raw coordinates/covariance remain in source_record_hex.
    latitude, longitude = finite(record["latitude"]), finite(record["longitude"])
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("invalid NavSatFix coordinates")
    result.update(navsat_latitude_deg=latitude, navsat_longitude_deg=longitude)
    altitude = record["altitude"]
    result["navsat_altitude_status"] = "UNAVAILABLE" if altitude == "NaN" else "ELLIPSOID_REPORTED"
    if altitude != "NaN":
        result["navsat_altitude_wgs84_ellipsoid_m"] = finite(altitude)
    covariance = record["position_covariance"]
    if len(covariance) != 9:
        raise ValueError("invalid NavSatFix covariance length")
    result["navsat_covariance_status"] = {0: "UNKNOWN", 1: "APPROXIMATED", 2: "DIAGONAL_KNOWN",
                                         3: "KNOWN"}.get(covariance_type, "UNKNOWN_TYPE")
    if covariance_type in (1, 2, 3):
        for index in ((0, 4, 8) if covariance_type == 2 else range(9)):
            value = finite(covariance[index])
            if index in (0, 4, 8) and value < 0:
                raise ValueError("negative NavSatFix variance")
            result["navsat_covariance_" + str(index) + "_m2"] = value
    return result


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("nonfinite sensor quantity")
    return value


def vehicle_report_quantities(record, typename):
    if typename == STEERING_REPORT:
        return {"vehicle_report_stamp_us": json_time({"header": {"stamp": record["stamp"], "frame_id": ""}}, "ros-battery-json"),
                "vehicle_steering_tire_angle_rad": finite(record["steering_tire_angle"])}
    return {"vehicle_report_stamp_us": json_time(record, "ros-battery-json"),
            "vehicle_report_frame_hex": text_hex(record["header"]["frame_id"]),
            "vehicle_velocity_longitudinal_m_s": finite(record["longitudinal_velocity"]),
            "vehicle_velocity_lateral_m_s": finite(record["lateral_velocity"]),
            "vehicle_heading_rate_rad_s": finite(record["heading_rate"])}


def transform_quantities(record):
    entries = record["transforms"]
    if not entries:
        return [{"tf_entry_count": 0}]
    result = []
    for index, entry in enumerate(entries):
        stamp = json_time(entry, "ros-battery-json")
        parent, child = entry["header"]["frame_id"], entry["child_frame_id"]
        if not parent or not child or parent == child:
            raise ValueError("distinct nonempty TF frame labels required")
        row = {"tf_entry_count": len(entries), "tf_entry_index": index,
               "tf_parent_frame_hex": text_hex(parent), "tf_child_frame_hex": text_hex(child),
               "tf_source_stamp_us": stamp}
        for group, axes, prefix in [("translation", "xyz", "tf_translation_m_"),
                                    ("rotation", "xyzw", "tf_rotation_xyzw_")]:
            for axis in axes:
                row[prefix + axis] = finite(entry["transform"][group][axis])
        norm2 = sum(row["tf_rotation_xyzw_" + a] ** 2 for a in "xyzw")
        if not math.isfinite(norm2) or abs(norm2 - 1) > 1e-6:
            raise ValueError("TF requires a unit quaternion; no silent normalization")
        result.append(row)
    return result


def range_value(value, low, high):
    special = {"-Infinity": "TOO_CLOSE_REPORTED", "Infinity": "OUT_OF_RANGE_REPORTED",
               "NaN": "INVALID_OR_MISSING"}
    status = special.get(value)
    if status is None:
        value = finite(value)
        status = "REPORTED" if low <= value <= high else "DISCARDED_OUTSIDE_LIMITS"
    return {"range_m": value if status == "REPORTED" else None, "range_status": status}


def scan_quantities(record, unavailable_infinity=False):
    frame = record["header"]["frame_id"]
    low, high = finite(record["range_min"]), finite(record["range_max"])
    start, end, step, interval, period = (finite(record[k]) for k in
        ("angle_min", "angle_max", "angle_increment", "time_increment", "scan_time"))
    if not frame or low > high or interval < 0 or period < 0:
        raise ValueError("invalid scan frame, range bounds or time interval")
    ranges, intensities = record["ranges"], record["intensities"]
    if len(intensities) not in (0, len(ranges)):
        raise ValueError("scan intensity count differs from rays")
    base = {"scan_ray_count": len(ranges), "sensor_frame_hex": text_hex(frame),
            "range_min_m": low, "range_max_m": high, "scan_period_s": period,
            "scan_first_stamp_us": json_time(record, "ros-battery-json")}
    if not ranges:
        return [dict(base, range_status="EMPTY_SCAN", scan_intensity_status="NOT_PROVIDED")]
    result = []
    for index, value in enumerate(ranges):
        row = dict(base, **range_value(value, low, high), scan_ray_index=index,
                   scan_angle_rad=finite(start + index * step),
                   scan_ray_offset_s=finite(index * interval))
        if unavailable_infinity and value == "Infinity":
            row["range_status"] = "UNAVAILABLE_RETURN_REPORTED"
        # Device-specific intensity has no SI unit or standardized sentinel here.
        intensity = intensities[index] if intensities else None
        reported = type(intensity) in (int, float) and math.isfinite(intensity)
        row.update(scan_intensity_device_units=intensity if reported else None,
                   scan_intensity_status="REPORTED" if reported else
                   ("NONFINITE_REPORTED" if intensities else "NOT_PROVIDED"))
        result.append(row)
    return result


def sensor_quantities(record, typename):
    name = typename.removeprefix("sensor_msgs/msg/")
    result = {"sensor_frame_hex": text_hex(record["header"]["frame_id"])}
    if name == "Range":
        result["range_variance_status"] = "NOT_PROVIDED"
        if "variance" in record:
            variance = finite(record["variance"])
            if variance < 0:
                raise ValueError("negative range variance")
            result["range_variance_status"] = "UNKNOWN" if variance == 0 else "REPORTED"
            result["range_variance_m2"] = variance if variance else None
        if not record["header"]["frame_id"]:
            raise ValueError("range requires a sensor frame")
        low, high, fov = (finite(record[k]) for k in ("min_range", "max_range", "field_of_view"))
        if low > high or not 0 <= fov <= 2 * math.pi:
            raise ValueError("invalid range bounds or field of view")
        code = record["radiation_type"]
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError("invalid range radiation code")
        value = record["range"]
        if low == high and value not in ("-Infinity", "Infinity"):
            raise ValueError("fixed-distance range requires an infinity report")
        result.update(**range_value(value, low, high),
                      range_min_m=low, range_max_m=high, range_fov_rad=fov,
                      range_radiation_code=code,
                      range_radiation_reported={0: "ULTRASOUND", 1: "INFRARED"}.get(code, f"UNKNOWN_{code}"))
    elif name in SCALARS:
        source, target, bias, minimum, maximum = SCALARS[name]
        value = finite(finite(record[source]) + bias)
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError("sensor quantity outside declared range")
        variance = finite(record["variance"])
        if variance < 0:
            raise ValueError("negative sensor variance")
        result[target] = value
        # Source value remains in retained record. A bias does not change variance.
        result["sensor_variance_status"] = "UNKNOWN" if variance == 0 else "REPORTED"
    elif name == "MagneticField":
        if not record["header"]["frame_id"]:
            raise ValueError("magnetic vector requires a frame label")
        for axis in "xyz":
            value = record["magnetic_field"][axis]
            absent = value == "NaN"  # Defined per-axis sentinel; not all nonfinite values.
            result["magnetic_field_tesla_" + axis] = None if absent else finite(value)
            result["magnetic_field_" + axis + "_status"] = "UNREPORTED" if absent else "REPORTED"
        covariance = record["magnetic_field_covariance"]
        if len(covariance) != 9 or any(finite(v) < 0 for v in covariance[::4]):
            raise ValueError("invalid magnetic covariance diagonal")
        for value in covariance:
            finite(value)
        result["magnetic_covariance_status"] = "REPORTED" if any(covariance) else "UNKNOWN"
    else:
        raise ValueError("unsupported selected sensor")
    return result


def joint_quantities(record, kinds, allow_missing_joint_values=False):
    names = record["name"]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("joint names must be nonempty and unique")
    for quantity in ("position", "velocity", "effort"):
        if len(record[quantity]) not in (0, len(names)):
            raise ValueError("joint array length mismatch")
    stamp = json_time(record, "ros-battery-json")
    if not names:
        return [{"joint_count": 0, "joint_source_stamp_us": stamp}]
    rows = []
    for index, name in enumerate(names):
        kind = kinds.get(name)
        if kind not in ("angular", "linear"):
            raise ValueError("explicit angular/linear joint configuration required")
        row = {"joint_count": len(names), "joint_index": index, "joint_name_hex": text_hex(name),
               "joint_kind_configured": kind, "joint_source_stamp_us": stamp}
        units = ("rad", "rad_s", "nm") if kind == "angular" else ("m", "m_s", "n")
        for quantity, unit in zip(("position", "velocity", "effort"), units):
            values = record[quantity]
            row["joint_" + quantity + "_status"] = "REPORTED" if values else "NOT_PROVIDED"
            if values:
                if allow_missing_joint_values and values[index] == "NaN":
                    row["joint_" + quantity + "_status"] = "UNAVAILABLE_SOURCE_NAN"
                else:
                    row["joint_" + quantity + "_" + unit] = finite(values[index])
        rows.append(row)
    return rows


def cloud_quantities(record, unit, compact=False):
    if unit != "m" or not record["header"]["frame_id"]:
        raise ValueError("point cloud requires explicit metre units and frame")
    width, height, step, stride = [record[key] for key in ("width", "height", "point_step", "row_step")]
    if any(type(n) is not int or n < 0 for n in (width, height, step, stride)):
        raise ValueError("invalid point dimensions")
    if width * height > (100000 if compact else 256) or step > 64 or stride < width * step:
        raise ValueError("point layout outside finite scope")
    data = record["data"]
    if (not isinstance(data, list) or len(data) != stride * height
            or any(type(n) is not int or not 0 <= n <= 255 for n in data)
            or type(record["is_bigendian"]) is not bool or type(record["is_dense"]) is not bool):
        raise ValueError("invalid point data")
    raw, layout = bytes(data), {}
    sizes = {1: 1, 2: 1, 3: 2, 4: 2, 5: 4, 6: 4, 7: 4, 8: 8}
    if not isinstance(record["fields"], list) or len(record["fields"]) > 64:
        raise ValueError("invalid point field list")
    for field in record["fields"]:
        name, offset, datatype, count = [field[key] for key in ("name", "offset", "datatype", "count")]
        if (not isinstance(name, str) or not name or name in layout
                or type(offset) is not int or offset < 0 or type(count) is not int or count < 1
                or type(datatype) is not int or datatype not in sizes or offset + count * sizes[datatype] > step):
            raise ValueError("invalid point field layout")
        layout[name] = (offset, datatype, count)
    base = dict(point_count=width * height, point_frame_hex=text_hex(record["header"]["frame_id"]),
                point_source_stamp_us=json_time(record, "ros-battery-json"),
                point_unit_basis="METRES_CONFIGURED_NOT_INFERRED_FROM_TYPE",
                point_unmapped_fields_hex=text_hex(json.dumps([name for name in layout if name not in ("x", "y", "z")])))
    if not width * height:
        return [dict(base, point_status="EMPTY_CLOUD")]
    selected = []
    for axis in ("x", "y", "z"):
        if axis not in layout or layout[axis][1] not in (7, 8) or layout[axis][2] != 1:
            raise ValueError("point xyz must be scalar float32 or float64")
        offset, datatype, _ = layout[axis]
        span = set(range(offset, offset + sizes[datatype]))
        if any(span & previous for previous in selected):
            raise ValueError("overlapping point xyz")
        selected.append(span)
    rows = []
    for index in range(width * height):
        row, column = divmod(index, width)
        values = {}
        for axis in ("x", "y", "z"):
            offset, datatype, _ = layout[axis]
            value = struct.unpack_from((">" if record["is_bigendian"] else "<") + ("f" if datatype == 7 else "d"),
                                       raw, row * stride + column * step + offset)[0]
            values["point_" + axis + "_m"] = value if math.isfinite(value) else None
        valid = all(value is not None for value in values.values())
        if record["is_dense"] and not valid:
            raise ValueError("dense point cloud contains invalid coordinates")
        rows.append(dict(base, **values, point_index=index, point_row=row, point_column=column,
                         point_status="REPORTED_COORDINATES" if valid else "NONFINITE_COORDINATE_REPORTED"))
        if compact:
            start = row * stride + column * step
            rows[-1]["point_bytes_hex"] = "hex:" + raw[start:start + step].hex()
    return rows


def check_single_zstd_frame(data, checksum):
    """Check framing lengths only; never decompress or allocate output here."""
    if data[:4] != b"\x28\xb5\x2f\xfd":
        raise ValueError("ordinary zstd frame required")
    offset = zstandard.frame_header_size(data)
    while True:
        if offset + 3 > len(data):
            raise ValueError("truncated compressed block header")
        header = int.from_bytes(data[offset:offset + 3], "little")
        kind, size = (header >> 1) & 3, header >> 3
        if kind == 3:
            raise ValueError("reserved zstd block type")
        offset += 3 + (1 if kind == 1 else size)
        if offset > len(data):
            raise ValueError("truncated compressed block")
        if header & 1:
            if offset + (4 if checksum else 0) != len(data):
                raise ValueError("compressed bag must contain exactly one complete frame")
            return


def convert_file_compressed(path, metadata, names, joint_kinds, stamped_motion_role, pointcloud_unit, pointcloud_compact=False, scan_compact=False, list_compact=None, allow_missing_voltage=False, allow_missing_joint_values=False, scan_unavailable_infinity=False):
    info = dict(metadata["rosbag2_bagfile_information"])
    if info.get("compression_format") != "zstd":
        raise ValueError("unsupported file compression")
    with tempfile.TemporaryDirectory(prefix="musubi-ros-record-") as directory:
        target = Path(directory)
        mapped, total = {}, 0
        try:
            for name in names:
                if not name.endswith(".zstd"):
                    raise ValueError("file compression requires explicit zstd suffix")
                mapped[name] = name[:-5]
                destination = target / mapped[name]
                destination.parent.mkdir(parents=True, exist_ok=True)
                compressed = (path / name).read_bytes()
                parameters = zstandard.get_frame_parameters(compressed)
                check_single_zstd_frame(compressed, parameters.has_checksum)
                remaining = LIMIT - total
                if remaining <= 0 or parameters.window_size > LIMIT or (
                        parameters.content_size != zstandard.CONTENTSIZE_UNKNOWN and parameters.content_size > remaining):
                    raise ValueError("expanded bag exceeds bound")
                # A stream reader can return complete-looking bytes for a truncated
                # frame. One-shot decoding checks frame completion; preflight is
                # essential because known content size overrides max_output_size.
                expanded = zstandard.ZstdDecompressor().decompress(
                    compressed, max_output_size=remaining, allow_extra_data=False)
                total += len(expanded)
                if total > LIMIT:
                    raise ValueError("expanded bag exceeds bound")
                with destination.open("xb") as output:
                    output.write(expanded)
        except zstandard.ZstdError as error:
            raise ValueError("invalid or expanded bag exceeds bounded compression scope") from error
        info.update(relative_file_paths=[mapped[name] for name in names], compression_mode="", compression_format="")
        if "files" in info:
            info["files"] = [dict(item, path=mapped[item["path"]]) for item in info["files"]]
        with (target / "metadata.yaml").open("x") as output:
            YAML(typ="safe").dump({"rosbag2_bagfile_information": info}, output)
        output, report = convert(target, joint_kinds, stamped_motion_role, pointcloud_unit, pointcloud_compact, scan_compact, list_compact,
                                 allow_missing_voltage=allow_missing_voltage,
                                 allow_missing_joint_values=allow_missing_joint_values,
                                 scan_unavailable_infinity=scan_unavailable_infinity)
        report.update(source_file_compression="zstd", expanded_storage_bytes=total,
                      source_metadata_yaml_hex="hex:" + (path / "metadata.yaml").read_bytes().hex())
        if len(json.dumps(report).encode()) > LIMIT:
            raise ValueError("compressed source report exceeds bound")
        return output, report


def convert(path, joint_kinds=None, stamped_motion_role=None, pointcloud_unit=None, pointcloud_compact=False, scan_compact=False, list_compact=None, *, start_ns=None, stop_ns=None, topics=None, allow_missing_voltage=False, allow_missing_joint_values=False, ros1=False, scan_unavailable_infinity=False):
    if type(ros1) is not bool or (ros1 and (joint_kinds or stamped_motion_role or pointcloud_unit or pointcloud_compact or scan_compact or list_compact or start_ns is not None or stop_ns is not None or topics is not None or allow_missing_joint_values)):
        raise ValueError("ROS1 selection accepts only the fixed battery and IMU record scope")
    qualified_hashes = ROS1_HASHES if ros1 else HASHES
    if topics is not None and (not isinstance(topics, (list, tuple)) or not 1 <= len(topics) <= 32
            or any(not isinstance(topic, str) or not topic or len(topic) > 256
                   or any(ord(c) < 32 for c in topic) for topic in topics)
            or len(set(topics)) != len(topics)):
        raise ValueError("one to 32 unique exact topics required")
    windowed = start_ns is not None or stop_ns is not None
    if windowed and (type(start_ns) is not int or type(stop_ns) is not int
                     or not 0 <= start_ns < stop_ns <= 2**63 - 1):
        raise ValueError("explicit nonempty nanosecond interval required")
    if type(pointcloud_compact) is not bool or (pointcloud_compact and pointcloud_unit != "m"):
        raise ValueError("compact point cloud requires explicit metre units")
    joint_kinds = {} if joint_kinds is None else joint_kinds
    if type(scan_compact) is not bool or (scan_compact and (pointcloud_compact or pointcloud_unit)):
        raise ValueError("scan selection cannot use point cloud configuration")
    if type(scan_unavailable_infinity) is not bool or (scan_unavailable_infinity and not scan_compact):
        raise ValueError("scan return policy requires explicit compact LaserScan selection")
    if list_compact is not None and (list_compact not in ("actions", "parameters")
            or pointcloud_compact or scan_compact or pointcloud_unit):
        raise ValueError("invalid or conflicting compact list selection")
    compact = pointcloud_compact or scan_compact or list_compact is not None
    if type(allow_missing_joint_values) is not bool or allow_missing_joint_values and (compact or not joint_kinds):
        raise ValueError("joint missing-value policy requires explicit joint kinds and message selection")
    if type(allow_missing_voltage) is not bool or allow_missing_voltage and compact:
        raise ValueError("battery missing-voltage policy cannot use other compact selection")
    selected_type = ({"actions": ACTION_STATUS, "parameters": PARAMETER_EVENT}[list_compact]
        if list_compact else POINT_CLOUD if pointcloud_compact else "sensor_msgs/msg/LaserScan")
    source_key = "list_sources" if list_compact else "pointcloud_sources" if pointcloud_compact else "scan_sources"
    if compact and (joint_kinds or stamped_motion_role):
        raise ValueError("compact selection cannot silently ignore other message configuration")
    if not isinstance(joint_kinds, dict) or any(not isinstance(k, str) or not k or v not in ("angular", "linear") for k, v in joint_kinds.items()):
        raise ValueError("invalid joint configuration")
    path = Path(path)
    if ros1 and not path.is_file():
        raise ValueError("ROS1 requires a single saved bag file")
    paths = list(path.rglob("*")) if path.is_dir() else [path]
    if path.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError("symlink bag not supported")
    if windowed and not path.is_dir():
        raise ValueError("window selection requires an uncompressed sqlite3 bag directory")
    if topics is not None and not path.is_dir():
        raise ValueError("topic selection requires a sqlite3 or MCAP bag directory without external compression")
    if sum(p.stat().st_size for p in paths if p.is_file()) > (2 * 1024**3 if windowed else LIMIT):
        raise ValueError("bag too large; select a finite recording segment")
    if path.is_dir():
        with (path / "metadata.yaml").open("rb") as stream:
            metadata_raw = stream.read(256001 if windowed else LIMIT + 1)
        if len(metadata_raw) > (256000 if windowed else LIMIT):
            raise ValueError("bag metadata exceeds bound")
        metadata = YAML(typ="safe").load(metadata_raw.decode("utf-8"))
        if topics is not None and (metadata["rosbag2_bagfile_information"].get("storage_identifier") not in ("sqlite3", "mcap")
                         or metadata["rosbag2_bagfile_information"].get("compression_mode", "") != ""):
            raise ValueError("topic selection requires a sqlite3 or MCAP bag directory without external compression")
        if windowed and (metadata["rosbag2_bagfile_information"].get("storage_identifier") != "sqlite3"
                         or metadata["rosbag2_bagfile_information"].get("compression_mode", "") != ""):
            raise ValueError("window selection requires an uncompressed sqlite3 bag directory")
        names = metadata["rosbag2_bagfile_information"]["relative_file_paths"]
        if not names or any(not isinstance(name, str) or Path(name).is_absolute()
                            or ".." in Path(name).parts or not (path / name).is_file()
                            for name in names):
            raise ValueError("bag metadata references an unsupported path")
        if metadata["rosbag2_bagfile_information"].get("compression_mode", "").lower() == "file":
            return convert_file_compressed(path, metadata, names, joint_kinds, stamped_motion_role, pointcloud_unit, pointcloud_compact, scan_compact, list_compact, allow_missing_voltage, allow_missing_joint_values, scan_unavailable_infinity)
    output = io.StringIO(newline="")
    output_fields = BASE + IMU_FIELDS + DIAG_FIELDS + MOTION_FIELDS + SENSOR_FIELDS + TF_FIELDS + SCAN_FIELDS + JOINT_FIELDS + ACTION_FIELDS + CLOUD_FIELDS + NAVSAT_FIELDS + PARAMETER_FIELDS
    output_fields += NAV2_FIELDS + VEHICLE_FIELDS
    if allow_missing_voltage:
        output_fields += ["battery_voltage_disposition"]
    if pointcloud_compact:
        output_fields = ["record_time_us", "source_record_hex", "source_message_index", "source_cdr_sha256", "point_bytes_hex"] + CLOUD_FIELDS
    elif scan_compact:
        output_fields = ["record_time_us", "source_record_hex", "source_message_index", "source_cdr_sha256"] + SENSOR_FIELDS + SCAN_FIELDS
    elif list_compact:
        output_fields = ["record_time_us", "source_record_hex", "source_message_index", "source_cdr_sha256"] + (
            ACTION_FIELDS if list_compact == "actions" else PARAMETER_FIELDS)
    writer = csv.DictWriter(output, fieldnames=output_fields, lineterminator="\n")
    writer.writeheader()
    report = {"source_messages": 0, "decoded_messages": 0, "output_records": 0, "unsupported_records": [],
              "clock": "Unknown", "evidence_kind": "record-conversion-not-physical-certification"}
    report_bytes = 0
    if scan_unavailable_infinity:
        report["scan_infinity_policy"] = "UNAVAILABLE_RETURN_REPORTED"
    if allow_missing_voltage:
        report["battery_voltage_policy"] = "QUALIFIED_SOURCE_NAN_UNAVAILABLE_NOT_ZERO"
    if allow_missing_joint_values:
        report["joint_value_policy"] = "QUALIFIED_SOURCE_NAN_UNAVAILABLE_NOT_ZERO"
    if compact:
        report.update({source_key: [], "selection": list_compact.upper() + "_ONLY_COMPACT" if list_compact else "POINTCLOUD_ONLY_COMPACT" if pointcloud_compact else "LASERSCAN_ONLY_COMPACT",
                       "source_binding": "sha256-of-exact-CDR; not authentication"})
    with (Reader1(path) if ros1 else Reader(path)) as reader:
        if ros1:
            if any(chunk.decompressor is not ROS1_DECOMPRESSORS["none"] for chunk in reader.chunks.values()):
                raise ValueError("ROS1 compressed chunks are outside this bounded input scope")
            report["source_format"] = "rosbag1-noetic-selected; original sequence and ROS1 bytes retained"
        declared_total = sum(c.msgcount for c in reader.connections)
        connections = reader.connections
        if topics is not None:
            if set(topics) - {c.topic for c in connections}:
                raise ValueError("requested topic is absent")
            connections = [c for c in connections if c.topic in topics]
            report["topic_selection"] = dict(topics=list(topics),
                declared_selected_messages=sum(c.msgcount for c in connections),
                declared_excluded_messages=sum(c.msgcount for c in reader.connections if c.topic not in topics),
                excluded_topics=[dict(topic=c.topic, type=c.msgtype, declared_messages=c.msgcount)
                                 for c in reader.connections if c.topic not in topics],
                outside_selection=("NOT_DECODED_OR_VALIDATED"
                    if metadata["rosbag2_bagfile_information"]["storage_identifier"] == "mcap"
                    else "NOT_READ_OR_VALIDATED"),
                source_message_index_basis="selected topics and interval; not global bag index")
            if len(json.dumps(report).encode()) > LIMIT:
                raise ValueError("topic declaration report exceeds bound")
        declared_selected = sum(c.msgcount for c in connections)
        if windowed:
            report["capture_window"] = dict(start_ns=start_ns, stop_ns_exclusive=stop_ns,
                declared_bag_messages=declared_total, outside_window="NOT_DECODED_OR_VALIDATED",
                source_message_index_basis="selected interval, not global bag index")
        for connection in connections:
            if compact and connection.msgtype != selected_type:
                continue
            allowed_hashes = (qualified_hashes.get(connection.msgtype),)
            if not ros1 and connection.msgtype == RANGE:
                allowed_hashes += (RANGE_VARIANCE_HASH,)
            if connection.msgtype in qualified_hashes and (
                    connection.digest not in allowed_hashes
                    or (not ros1 and connection.ext.serialization_format != "cdr")):
                raise ValueError("selected ROS schema hash or serialization mismatch")
            if connection.msgtype in qualified_hashes:
                definition = connection.msgdef
                if len(definition.data.encode()) > 256000:
                    raise ValueError("embedded ROS definition exceeds bound")
                declared = get_typestore(Stores.EMPTY)
                if definition.format == MessageDefinitionFormat.MSG:
                    declared.register(get_types_from_msg(definition.data, connection.msgtype))
                elif definition.format == MessageDefinitionFormat.IDL:
                    declared.register(get_types_from_idl(definition.data))
                else:
                    raise ValueError("selected ROS definition is missing")
                declared_hash = declared.generate_msgdef(connection.msgtype)[1] if ros1 else declared.hash_rihs01(connection.msgtype)
                if declared_hash != connection.digest:
                    raise ValueError("embedded ROS definition does not match selected schema")
        for connection, timestamp, raw in reader.messages(connections=connections, start=start_ns, stop=stop_ns):
            if topics is not None and connection.topic not in topics:
                raise ValueError("storage yielded an unselected topic")
            report["source_messages"] += 1
            if windowed and not start_ns <= timestamp < stop_ns:
                raise ValueError("storage yielded a record outside selected interval")
            raw_limit = 8 * 1024 * 1024 if compact else 12000
            if not 0 <= timestamp <= 2**63 - 1 or len(raw) > raw_limit:
                raise ValueError("record timestamp or size outside finite scope")
            source = {"topic": connection.topic, "type": connection.msgtype,
                      "type_hash": connection.digest, "capture_ns": str(timestamp),
                      "raw_ros1_hex" if ros1 else "raw_cdr_hex": bytes(raw).hex()}
            if connection.msgtype not in qualified_hashes or (compact and connection.msgtype != selected_type):
                if compact:
                    source["reason"] = "NOT_SELECTED_" + (list_compact.upper() if list_compact else "POINTCLOUD" if pointcloud_compact else "LASERSCAN")
                report_bytes += len(json.dumps(source).encode())
                if report_bytes > LIMIT:
                    raise ValueError("unsupported report exceeds bound")
                report["unsupported_records"].append(source)
                continue
            store = RANGE_STORE if connection.msgtype == RANGE and connection.digest == RANGE_VARIANCE_HASH else STORE
            record = decode(raw, connection.msgtype, store, ros1=ros1)
            # Validate header stamp/frame, without promoting it to capture/UTC time.
            if connection.msgtype not in (TF, ACTION_STATUS, PARAMETER_EVENT, NAV2_FEEDBACK, STEERING_REPORT):
                json_time(record, "ros-battery-json")
            source["decoded"] = record if not compact else {k: v for k, v in record.items()
                if k not in (("status_list", "new_parameters", "changed_parameters", "deleted_parameters")
                    if list_compact else ("data",) if pointcloud_compact else ("ranges", "intensities"))}
            retained = json.dumps(source, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(retained.encode()) > (LIMIT if compact else 30000):
                raise ValueError("retained source record exceeds bound")
            row = {"record_time_us": timestamp // 1000,
                   "source_record_hex": "hex:" + retained.encode().hex()}
            if compact:
                # Retain the exact CDR once, including unknown fields and padding.
                # Each expanded entry carries an explicit report binding.
                message_index = report["source_messages"] - 1
                digest = hashlib.sha256(raw).hexdigest()
                source.update(source_message_index=message_index, source_cdr_sha256=digest)
                report_bytes += len(json.dumps(source).encode())
                if report_bytes > LIMIT:
                    raise ValueError("compact source report exceeds bound")
                report[source_key].append(source)
                descriptor = {k: source[k] for k in ("topic", "type", "type_hash", "capture_ns")}
                row.update(source_record_hex="hex:" + json.dumps(descriptor, separators=(",", ":")).encode().hex(),
                           source_message_index=message_index, source_cdr_sha256="sha256:" + digest)
            if connection.msgtype == BATTERY:
                fields = BASE[2:] + (["battery_voltage_disposition"] if allow_missing_voltage else [])
                quantities = [dict(zip(fields, json_quantities(record, "ros-battery-json",
                                                             allow_missing_voltage=allow_missing_voltage)))]
            elif connection.msgtype == NAV2_FEEDBACK:
                quantities = [nav2_feedback_quantities(record)]
            elif connection.msgtype in VEHICLE_DEFINITIONS:
                quantities = [vehicle_report_quantities(record, connection.msgtype)]
            elif connection.msgtype == POINT_CLOUD:
                quantities = cloud_quantities(record, pointcloud_unit, pointcloud_compact)
            elif connection.msgtype == NAVSAT:
                quantities = [navsat_quantities(record)]
            elif connection.msgtype == IMU:
                quantities = [imu_quantities(record)]
            elif connection.msgtype == DIAGNOSTIC:
                quantities = diagnostic_quantities(record)
            elif connection.msgtype == ODOMETRY:
                quantities = [motion_quantities(record)]
            elif connection.msgtype == TF:
                quantities = transform_quantities(record)
            elif connection.msgtype == JOINT:
                quantities = joint_quantities(record, joint_kinds, allow_missing_joint_values)
            elif connection.msgtype == ACTION_STATUS:
                quantities = action_status_quantities(record)
            elif connection.msgtype == PARAMETER_EVENT:
                quantities = parameter_event_quantities(record)
            elif connection.msgtype in STAMPED_MOTION:
                quantities = [stamped_motion_quantities(record, connection.msgtype, stamped_motion_role)]
            elif connection.msgtype == "sensor_msgs/msg/LaserScan":
                if scan_compact and len(record["ranges"]) > 100000:
                    raise ValueError("scan ray count exceeds finite scope")
                quantities = scan_quantities(record, scan_unavailable_infinity)
            else:
                quantities = [sensor_quantities(record, connection.msgtype)]
            for values in quantities:
                writer.writerow(dict(row, **values))
                report["output_records"] += 1
                if output.tell() > (128 * 1024 * 1024 if compact else LIMIT):
                    raise ValueError("converted CSV exceeds bound")
            report["decoded_messages"] += 1
        if not windowed and report["source_messages"] != declared_selected:
            raise ValueError("bag message count mismatch")
        if windowed:
            if report["source_messages"] > declared_selected:
                raise ValueError("selected messages exceed declared bag count")
            report["capture_window"]["selected_messages"] = report["source_messages"]
        if topics is not None:
            report["topic_selection"]["read_messages"] = report["source_messages"]
    if not report["decoded_messages"]:
        raise ValueError("no supported messages")
    if compact and len(json.dumps(report, separators=(",", ":")).encode()) > LIMIT:
        raise ValueError("complete compact report exceeds bound")
    if topics is not None and len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("complete topic selection report exceeds bound")
    return output.getvalue(), report


def convert_parameter_yaml(path, capture_time_us):
    """Saved declarations, not ROS parameter application or wildcard resolution."""
    from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture time required")
    with Path(path).open("rb") as stream:
        raw = stream.read(256001)
    if not raw or len(raw) > 256000:
        raise ValueError("bounded parameter YAML required")
    text = raw.decode("utf-8")
    yaml = YAML(typ="safe")
    if any(type(token).__name__ in ("AliasToken", "AnchorToken", "TagToken", "DirectiveToken") for token in yaml.scan(text)):
        raise ValueError("parameter YAML aliases, tags and directives unsupported")

    def pairs(node):
        if not isinstance(node, MappingNode):
            raise ValueError("parameter namespace mapping required")
        seen = set()
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or not key.value or key.value in seen or key.value == "<<":
                raise ValueError("duplicate or invalid parameter key")
            seen.add(key.value)
            yield key.value, value

    def scalar(node):
        if not isinstance(node, ScalarNode):
            raise ValueError("scalar parameter value required")
        value = node.value
        if node.style in ("'", '"'):
            return 4, value
        if not value:
            raise ValueError("empty unquoted parameter value")
        if value != value.lstrip():
            raise ValueError("leading-space scalar lexical form unsupported")
        # Match the pinned RCL lexical rules, not YAML1.2 implicit Python types.
        if value in ("Y", "y", "yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"):
            return 1, True
        if value in ("N", "n", "no", "No", "NO", "false", "False", "FALSE", "off", "Off", "OFF"):
            return 1, False
        if re.fullmatch(r"[+-]?(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", value):
            unsigned = value.lstrip("+-")
            number = int(value, 16 if unsigned.lower().startswith("0x") else 8 if unsigned.startswith("0") else 10)
            if not -(2**63) <= number < 2**63:
                raise ValueError("parameter integer exceeds int64")
            return 2, number
        lower = value.lower()
        if (re.fullmatch(r"[+-]?(?:nan(?:\([a-z0-9_]*\))?|inf|infinity)", lower) or value in
                (".nan", ".NaN", ".NAN", ".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF", "-.inf", "-.Inf", "-.INF")):
            return 3, "NaN" if "nan" in lower else "-Infinity" if lower.startswith("-") else "Infinity"
        if re.fullmatch(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|0[xX][0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?[0-9]+)?)", value):
            number = float.fromhex(value) if "0x" in lower else float(value)
            unsigned = lower.lstrip("+-")
            mantissa = unsigned.split("p")[0][2:] if unsigned.startswith("0x") else unsigned.split("e")[0]
            if (not math.isfinite(number) or 0 < abs(number) < float.fromhex("0x1p-1022")
                    or (number == 0 and any(c not in "0." for c in mantissa))):
                raise ValueError("parameter floating range unsupported")
            return 3, number
        return 4, value

    nodes = {}
    def parameters(node, prefix="", depth=0):
        if depth > 16:
            raise ValueError("parameter nesting bound")
        result = []
        for key, value in pairs(node):
            name = prefix + key
            if isinstance(value, MappingNode):
                result.extend(parameters(value, name + ".", depth + 1))
                continue
            if isinstance(value, SequenceNode):
                items = [scalar(item) for item in value.value]
                if not items or len(items) > 4096 or len({code for code, _ in items}) != 1:
                    raise ValueError("nonempty homogeneous parameter array required")
                code, selected = items[0][0] + 5, [item for _, item in items]
            else:
                code, selected = scalar(value)
            fields = {1: "bool_value", 2: "integer_value", 3: "double_value", 4: "string_value",
                      6: "bool_array_value", 7: "integer_array_value", 8: "double_array_value", 9: "string_array_value"}
            result.append(dict(name=name, value={"type": code, fields[code]: selected}))
            if len(result) > 4096:
                raise ValueError("parameter count bound")
        return result

    def namespaces(node, prefix="", depth=0):
        if depth > 16:
            raise ValueError("node nesting bound")
        for key, value in pairs(node):
            if key == "ros__parameters":
                name = prefix.rstrip("/")
                if not name or name in nodes:
                    raise ValueError("missing or duplicate node declaration")
                nodes[name] = parameters(value)
            else:
                if prefix and key.startswith("/"):
                    raise ValueError("nested absolute node path is ambiguous")
                if not re.fullmatch(r"/?(?:[A-Za-z_][A-Za-z_0-9]*|\*\*?)(?:/(?:[A-Za-z_][A-Za-z_0-9]*|\*\*?))*", key):
                    raise ValueError("invalid declared node path")
                namespaces(value, prefix + key + "/", depth + 1)
    namespaces(yaml.compose(text))
    if not nodes or not any(nodes.values()):
        raise ValueError("no declared parameters")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_record_hex", "source_cdr_sha256", *PARAMETER_FIELDS], lineterminator="\n")
    writer.writeheader()
    count = 0
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    for node, entries in nodes.items():
        rows = parameter_event_quantities(dict(node=node, new_parameters=entries,
            changed_parameters=[], deleted_parameters=[]), declaration=True)
        for row in rows:
            writer.writerow(dict(record_time_us=capture_time_us,
                source_record_hex=text_hex(json.dumps({"kind": "PARAMETER_YAML_DECLARATION", "sha256": digest})),
                source_cdr_sha256="", **row))
            count += 1
            if count > 4096 or output.tell() > LIMIT:
                raise ValueError("parameter YAML output bound")
    return output.getvalue(), dict(output_records=count, declared_nodes=len(nodes),
        source_yaml_hex=text_hex(text), source_sha256=digest, clock="Unknown",
        scope="file declarations only; no parameter application or wildcard resolution")


def convert_map_metadata(path, capture_time_us):
    """Nav2 1.1.20 saved map declaration; never loads the referenced image."""
    from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture time required")
    with Path(path).open("rb") as stream:
        raw = stream.read(256001)
    if not raw or len(raw) > 256000:
        raise ValueError("bounded map YAML required")
    text = raw.decode("utf-8")
    yaml = YAML(typ="safe")
    if any(type(token).__name__ in ("AliasToken", "AnchorToken", "TagToken", "DirectiveToken")
           for token in yaml.scan(text)):
        raise ValueError("map YAML aliases, tags and directives unsupported")
    root = yaml.compose(text)
    if not isinstance(root, MappingNode) or any(not isinstance(key, ScalarNode) for key, _ in root.value):
        raise ValueError("map declaration mapping required")
    nodes = {key.value: value for key, value in root.value}
    if len(nodes) != len(root.value) or "<<" in nodes:
        raise ValueError("duplicate map key")
    doc = yaml.load(text)

    def scalar_number(node):
        if not isinstance(node, ScalarNode) or node.style is not None or not re.fullmatch(
                r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?", node.value):
            raise ValueError("canonical decimal map quantity required")
        return finite(float(node.value))

    resolution = scalar_number(nodes["resolution"])
    origin_node = nodes["origin"]
    if not isinstance(origin_node, SequenceNode) or len(origin_node.value) != 3:
        raise ValueError("map origin needs x/y/yaw")
    x, y, yaw = (scalar_number(node) for node in origin_node.value)
    free, occupied = (scalar_number(nodes[key]) for key in ("free_thresh", "occupied_thresh"))
    if resolution <= 0 or not 0 <= free < occupied <= 1:
        raise ValueError("invalid map resolution or thresholds")
    image = doc["image"]
    if not isinstance(image, str) or not image or len(image.encode()) > 4096 or "\x00" in image:
        raise ValueError("invalid map image reference")
    mode = doc.get("mode", "trinary")
    if not isinstance(mode, str) or mode.lower() not in ("trinary", "scale", "raw"):
        raise ValueError("unsupported map mode")
    mode = mode.lower()
    negate = doc["negate"]
    if not (type(negate) is bool or type(negate) is int and -(2**31) <= negate < 2**31):
        raise ValueError("invalid map negate")
    row = dict(record_time_us=capture_time_us, source_record_hex=text_hex(text),
        map_declaration_schema="NAV2_1_1_20", map_image_reference_hex=text_hex(image),
        map_image_reference_basis="ABSOLUTE_POSIX" if image.startswith("/") else "YAML_DIRECTORY",
        map_resolution_m_per_cell=resolution, map_origin_x_m=x, map_origin_y_m=y,
        map_origin_yaw_rad=yaw, map_origin_basis="CELL_0_0_BOTTOM_LEFT_NOT_GEODETIC",
        map_mode=mode.upper(), map_mode_basis="DECLARED" if "mode" in doc else "SCHEMA_DEFAULT",
        map_negate_reported=int(bool(negate)), map_free_threshold=free, map_occupied_threshold=occupied,
        map_threshold_rule="NOT_USED_RAW_SHADE" if mode == "raw" else "STRICT_ABOVE_OCCUPIED_BELOW_FREE",
        map_negate_used_by_mode=int(mode != "raw"), map_load_time_state="NOT_IN_YAML",
        map_declaration_disposition="DECLARED_ONLY_IMAGE_NOT_READ")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(row), lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue(), dict(output_records=1, clock="Unknown",
        source_sha256=hashlib.sha256(raw).hexdigest(),
        scope="map YAML declaration only; image pixels, frame identity and physical map not verified")


def convert_metadata(path):
    """Declared metadata only; never follows paths or authenticates bag contents."""
    with Path(path).open("rb") as source:
        raw = source.read(256001)
    if not raw or len(raw) > 256000:
        raise ValueError("metadata size outside finite scope")
    text = raw.decode("utf-8")
    yaml = YAML(typ="safe")
    if any(type(token).__name__ == "AliasToken" for token in yaml.scan(text)):
        raise ValueError("metadata aliases unsupported")
    info = yaml.load(text)["rosbag2_bagfile_information"]
    if type(info) is not dict or type(info.get("version")) is not int or info["version"] != 9:
        raise ValueError("metadata version unsupported")
    if info.get("storage_identifier") not in ("sqlite3", "mcap"):
        raise ValueError("metadata storage unsupported")

    def count(value):
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("invalid metadata integer")
        return value

    def encoded(value):
        if not isinstance(value, str) or not value or len(value.encode()) > 4096:
            raise ValueError("invalid metadata string")
        return "hex:" + value.encode().hex()

    start = count(info["starting_time"]["nanoseconds_since_epoch"])
    duration = count(info["duration"]["nanoseconds"])
    total = count(info["message_count"])
    topics = info["topics_with_message_count"]
    files = info["files"]
    paths = info["relative_file_paths"]
    if (not isinstance(topics, list) or len(topics) > 512 or not isinstance(files, list)
            or not isinstance(paths, list) or len(files) > 512 or len(paths) != len(files)):
        raise ValueError("invalid metadata entries")
    if (any(not isinstance(name, str) or not name for name in paths)
            or len(set(paths)) != len(paths)
            or [entry["path"] for entry in files] != paths
            or sum(count(entry["message_count"]) for entry in files) != total):
        raise ValueError("metadata file count mismatch")
    rows, names = [], set()
    for entry in topics:
        meta = entry["topic_metadata"]
        name = encoded(meta["name"])
        if name in names:
            raise ValueError("duplicate metadata topic")
        names.add(name)
        type_hash = meta.get("type_description_hash")
        if type_hash is not None and (not isinstance(type_hash, str) or len(type_hash) > 256):
            raise ValueError("invalid declared type hash")
        rows.append(dict(metadata_entry_kind="TOPIC", topic_name_hex=name, topic_type_hex=encoded(meta["type"]),
                         serialization_format_hex=encoded(meta["serialization_format"]),
                         declared_type_hash_hex="hex:" + type_hash.encode().hex() if type_hash is not None else None,
                         declared_topic_messages=count(entry["message_count"])))
    if sum(row["declared_topic_messages"] for row in rows) != total:
        raise ValueError("metadata topic count mismatch")
    for index, entry in enumerate(files):
        rows.append(dict(metadata_entry_kind="FILE", declared_file_index=index,
                         declared_file_path_hex=encoded(entry["path"]),
                         declared_file_start_ns=count(entry["starting_time"]["nanoseconds_since_epoch"]),
                         declared_file_duration_ns=count(entry["duration"]["nanoseconds"]),
                         declared_file_messages=count(entry["message_count"])))
    mode = info.get("compression_mode")
    compression = info.get("compression_format")
    if any(value is not None and (not isinstance(value, str) or len(value.encode()) > 256)
           for value in (mode, compression)):
        raise ValueError("invalid compression declaration")
    base = dict(record_time_us=start // 1000, source_record_hex="hex:" + raw.hex(),
                metadata_version=9, declared_start_ns=start, declared_duration_ns=duration,
                declared_duration_s=duration / 1e9, declared_total_messages=total,
                declared_topic_count=len(topics), declared_file_count=len(files),
                declared_compression_mode={None: "NOT_PROVIDED", "": "NONE", "file": "FILE", "message": "MESSAGE"}.get(mode, "UNKNOWN"),
                declared_compression_mode_hex="hex:" + mode.encode().hex() if mode is not None else None,
                declared_compression_format_hex="hex:" + compression.encode().hex() if compression is not None else None,
                storage_identifier=info["storage_identifier"],
                metadata_disposition="DECLARED_ONLY_CONTENTS_NOT_VERIFIED")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, [*base, "topic_name_hex", "topic_type_hex",
                                   "serialization_format_hex", "declared_topic_messages", "metadata_entry_kind",
                                   "declared_type_hash_hex", "declared_file_index", "declared_file_path_hex",
                                   "declared_file_start_ns", "declared_file_duration_ns", "declared_file_messages"], lineterminator="\n")
    writer.writeheader()
    for row in rows or [{}]:
        writer.writerow(dict(base, **(row or {"metadata_entry_kind": "SUMMARY"})))
        if output.tell() > LIMIT:
            raise ValueError("metadata output exceeds bound")
    return output.getvalue(), dict(output_records=max(1, len(rows)), clock="Unknown",
                                   evidence_kind="metadata-declaration-not-bag-content-validation")


def convert_amiga_filter_json(path):
    """Saved, explicitly SI-qualified FilterState protobuf JSON; no bridge runtime."""
    if path.stat().st_size > LIMIT:
        raise ValueError("saved FilterState input exceeds 16MiB")
    raw = path.read_bytes()
    if len(raw) > LIMIT:
        raise ValueError("saved FilterState input exceeds 16MiB")
    text = raw.decode("utf-8")
    output = io.StringIO()
    extra = ["heading_rad", "filter_converged_reported", "filter_calibrated_reported"]
    writer = csv.DictWriter(output, ["record_time_us", "source_record_hex", *MOTION_FIELDS, *extra], lineterminator="\n")
    writer.writeheader()
    count, previous = 0, None
    for line in text.splitlines():
        if not line.strip():
            continue
        envelope = json.loads(line, object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        if envelope["schema"] != "farm-ng-amiga/37a8bf217d37a7bc15bb995ca9f13f3c9eebff13/FilterState":
            raise ValueError("unqualified FilterState schema")
        if envelope["units"] != {"position": "m", "linear_velocity": "m/s", "angular_velocity": "rad/s", "heading": "rad"}:
            raise ValueError("explicit SI configuration required")
        stamp = envelope["capture_time_us"]
        if type(stamp) is not int or not 0 <= stamp < 2**63 or previous is not None and stamp < previous:
            raise ValueError("invalid capture clock")
        previous = stamp
        state = envelope["message"]
        pose = state["pose"]
        transform, tangent = pose["a_from_b"], pose["tangent_of_b_in_a"]
        quaternion = transform["rotation"]["unit_quaternion"]
        # The parent comes from the protobuf contract, not the old bridge's
        # duplicated frame_b header. Missing covariance must not become zero.
        values = motion_quantities({
            "header": {"frame_id": pose["frame_a"]}, "child_frame_id": pose["frame_b"],
            "pose": {"pose": {"position": transform["translation"],
                      "orientation": {**quaternion["imag"], "w": quaternion["real"]}}},
            "twist": {"twist": {"linear": tangent["linear_velocity"], "angular": tangent["angular_velocity"]}}})
        heading = state["heading"]
        if type(heading) not in (int, float) or not math.isfinite(heading):
            raise ValueError("invalid reported heading")
        if any(type(state[key]) is not bool for key in ("has_converged", "is_calibrated")):
            raise ValueError("explicit filter flags required")
        writer.writerow(dict(values, record_time_us=stamp, source_record_hex=text_hex(line),
            heading_rad=heading, filter_converged_reported=int(state["has_converged"]),
            filter_calibrated_reported=int(state["is_calibrated"])))
        count += 1
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("saved FilterState output exceeds 128MiB")
    if not count:
        raise ValueError("no FilterState observations")
    return output.getvalue(), {"source_messages": count, "observations": count,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "clock": "configured_capture_not_device_UTC", "evidence": "qualified_saved_export_not_physical_acceptance",
        "not_normalized": ["uncertainty_diagonal", "innovation"],
        "source_preservation": "exact_nonblank_JSON_lines_in_source_record_hex"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--ros1-noetic", action="store_true", help="saved uncompressed ROSbag1: fixed BatteryState/Imu, retain original Header.seq")
    parser.add_argument("--joint-kind", action="append", default=[], metavar="NAME=angular|linear")
    parser.add_argument("--stamped-motion-role", choices=("reported_estimate", "reported_reference"))
    parser.add_argument("--metadata-only", action="store_true", help="read version9 metadata.yaml declarations, not bag messages")
    parser.add_argument("--amiga-filter-json", action="store_true", help="saved SI-qualified FilterState protobuf JSON; no ROS or device connection")
    parser.add_argument("--parameters-only", action="store_true", help="read saved ROS parameter YAML declarations, not live settings")
    parser.add_argument("--map-metadata-only", action="store_true", help="read Nav2 1.1.20 map YAML declaration without opening its image")
    parser.add_argument("--capture-time-us", type=int, help="explicit capture clock for parameter or map YAML declarations")
    parser.add_argument("--pointcloud-unit", choices=("m",), help="explicit xyz coordinate units for selected PointCloud2")
    parser.add_argument("--pointcloud-compact", action="store_true", help="select point clouds; retain original CDR once in report.json")
    parser.add_argument("--scan-compact", action="store_true", help="select LaserScan; retain original CDR once in report.json")
    parser.add_argument("--list-compact", choices=("actions", "parameters"),
                        help="select existing action/parameter lists; retain CDR once in report.json")
    parser.add_argument("--start-ns", type=int, help="inclusive capture timestamp; requires --stop-ns, uncompressed sqlite3")
    parser.add_argument("--stop-ns", type=int, help="exclusive capture timestamp; requires --start-ns")
    parser.add_argument("--topic", action="append", help="exact topic selection; repeat up to32, sqlite3/MCAP without external compression")
    parser.add_argument("--allow-missing-voltage", action="store_true",
                        help="qualified BatteryState publisher NaN voltage is unavailable, not zero")
    parser.add_argument("--allow-missing-joint-values", action="store_true",
                        help="qualified JointState publisher NaN quantities are unavailable, not zero")
    parser.add_argument("--scan-unavailable-infinity", action="store_true",
                        help="qualified LaserScan positive infinity means unavailable return, not proven beyond-range")
    args = parser.parse_args()
    try:
        if args.scan_unavailable_infinity and (not args.scan_compact or args.metadata_only or args.parameters_only
                or args.map_metadata_only or args.amiga_filter_json or args.ros1_noetic):
            raise ValueError("scan return policy requires compact ROS2 message conversion")
        if args.ros1_noetic and (args.metadata_only or args.parameters_only or args.map_metadata_only or args.amiga_filter_json):
            raise ValueError("ROS1 message selection conflicts with other input formats")
        if (args.allow_missing_voltage or args.allow_missing_joint_values) and (args.metadata_only or args.parameters_only or args.map_metadata_only):
            raise ValueError("missing-value policy requires message conversion")
        kinds = {}
        for item in args.joint_kind:
            name, kind = item.rsplit("=", 1)
            if name in kinds:
                raise ValueError("duplicate joint configuration")
            kinds[name] = kind
        if args.amiga_filter_json:
            if args.parameters_only or args.map_metadata_only or args.metadata_only or kinds or args.stamped_motion_role or args.pointcloud_unit or args.pointcloud_compact or args.scan_compact or args.list_compact or args.start_ns is not None or args.stop_ns is not None or args.topic is not None or args.capture_time_us is not None or args.allow_missing_voltage or args.allow_missing_joint_values:
                raise ValueError("FilterState configuration conflicts with ROS selection")
            output, report = convert_amiga_filter_json(args.input)
        elif args.parameters_only or args.map_metadata_only:
            if (args.parameters_only and args.map_metadata_only) or args.metadata_only or kinds or args.stamped_motion_role or args.pointcloud_unit or args.pointcloud_compact or args.scan_compact or args.list_compact or args.start_ns is not None or args.stop_ns is not None or args.topic is not None:
                raise ValueError("parameter-file configuration conflicts with message/metadata selection")
            convert_declaration = convert_parameter_yaml if args.parameters_only else convert_map_metadata
            output, report = convert_declaration(args.input, args.capture_time_us)
        elif args.capture_time_us is not None:
            raise ValueError("capture clock requires a parameter or map YAML declaration")
        elif args.metadata_only:
            if kinds or args.stamped_motion_role or args.pointcloud_unit or args.pointcloud_compact or args.scan_compact or args.list_compact or args.start_ns is not None or args.stop_ns is not None or args.topic is not None:
                raise ValueError("message configuration is not metadata configuration")
            output, report = convert_metadata(args.input)
        else:
            output, report = convert(args.input, kinds, args.stamped_motion_role, args.pointcloud_unit, args.pointcloud_compact, args.scan_compact, args.list_compact,
                                     start_ns=args.start_ns, stop_ns=args.stop_ns, topics=args.topic,
                                     allow_missing_voltage=args.allow_missing_voltage,
                                     allow_missing_joint_values=args.allow_missing_joint_values, ros1=args.ros1_noetic,
                                     scan_unavailable_infinity=args.scan_unavailable_infinity)
        args.output_directory.mkdir()  # Refuse existing output/evidence, including input equality.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        report_text = json.dumps(report, separators=(",", ":")) if args.pointcloud_compact or args.scan_compact or args.list_compact else json.dumps(report, indent=2)
        (args.output_directory / "report.json").write_text(report_text, encoding="utf-8")
    except Exception:
        parser.exit(2, "ROS record conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
