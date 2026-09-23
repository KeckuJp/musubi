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
from rosbags.typesys.msg import normalize_msgtype
from rosbags.interfaces import MessageDefinitionFormat
from ruamel.yaml import YAML
import lz4.frame
import zstandard

try:
    from scripts.convert_ardupilot_battery_csv import (BATTERY_TEMPERATURE_FIELDS, JSON_ELECTRICAL_FIELDS, NORMALIZED, PX4_REQUIRED,
        convert as battery_convert, json_quantities, json_time, unique_object)
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import (BATTERY_TEMPERATURE_FIELDS, JSON_ELECTRICAL_FIELDS, NORMALIZED, PX4_REQUIRED,
        convert as battery_convert, json_quantities, json_time, unique_object)

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
# A legacy rosbag2 SQLite3 bag at storage schema 1 or 2 carries no type hash and no embedded
# definition: pinned rosbags 0.11.5 builds those connections with an empty digest and
# MessageDefinitionFormat.NONE, so the bag offers only a type *name*. A name is never compatibility
# evidence, so the guard is not relaxed - it is replaced, only on explicit request, by this fixed
# selection of the definition this file already qualifies. The producer and its binary stay
# unverified and every row says so.
LEGACY_SCHEMA_CHOICES = {"battery-humble-equivalent": BATTERY}
LEGACY_SCHEMA_BASIS = ("TYPE_HASH_ABSENT_NOT_VERIFIED_DECODED_WITH_A_CALLER_SELECTED_LOCAL_DEFINITION_"
                       "PRODUCER_AND_ITS_BINARY_UNVERIFIED")
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
                 "action_accepted_stamp_us", "action_accepted_stamp_ns", "action_status_code",
                 "action_status_reported"]
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
# husarion_ugv_ros 641a08a0afe45c495abe48ecd037c9dfd57b236e husarion_ugv_msgs/msg/
# ChargingStatus.msg, and ros-controls/control_msgs de74566b0b4edf334655f4f69cdffd05df06f9b2
# (humble) control_msgs/msg/{DynamicJointState,InterfaceValue}.msg, verbatim. Neither package
# is in the pinned Humble typestore, so both are registered like the other vendor messages and
# their hashes are derived from these exact definitions, not copied from a document.
CHARGING = "husarion_ugv_msgs/msg/ChargingStatus"
DYNAMIC_JOINT = "control_msgs/msg/DynamicJointState"
GROUND_DEFINITIONS = {
    CHARGING: ("uint8 UNKNOWN = 0\nuint8 WIRED = 1\nuint8 WIRELESS = 2\n"
               "std_msgs/Header header\nbool charging\nfloat32 current\n"
               "float32 current_battery_1\nfloat32 current_battery_2\nuint8 charger_type\n"),
    "control_msgs/msg/InterfaceValue": "string[] interface_names\nfloat64[] values\n",
    DYNAMIC_JOINT: ("std_msgs/Header header\nstring[] joint_names\n"
                    "control_msgs/InterfaceValue[] interface_values\n")}
for _typename, _definition in GROUND_DEFINITIONS.items():
    STORE.register(get_types_from_msg(_definition, _typename))
HASHES[CHARGING] = "RIHS01_18c1e70c025be0c4f9860fd98c4d9b93dc593f4fd5e56a3381dc1c0af38aabc7"
HASHES[DYNAMIC_JOINT] = "RIHS01_18024c98a62697680f581108eeed40b28fb4542fa0ac5a8e36f56c62b6813d26"
# Clearpath OutdoorNav's own reported autonomy state, from cpr-application/clearpath_outdoornav
# eee7d9aaa7311a60d2137f08af334c7a26f65c15 (BSD-3-Clause),
# clearpath_outdoornav_msgs/clearpath_navigation_msgs/msg/AutonomyStatus.msg verbatim -- 334 bytes,
# sha256 6fbda0d27410b0f6cc823e630e7a2c6bb12402b5a46d4d7ec53f85c08bcb164e. The manual documents the
# API as a ROS 2 API with every node/topic/service namespace-prefixed by the robot serial number, and
# documents no export of its own, so the saved artefact is an operator-run rosbag2 like Husarion's.
# The file's own first line says "status of individual Waypoint in Mission", which contradicts its
# documented use as the autonomy status topic; meanings below come from the fields, types and
# constants, never from that comment. Whether these definitions are byte-identical to what a given
# robot build publishes is not established -- the RIHS01 gate is what actually protects the read.
AUTONOMY = "clearpath_navigation_msgs/msg/AutonomyStatus"
OUTDOORNAV_DEFINITIONS = {
    AUTONOMY: ("std_msgs/Header header\nbuiltin_interfaces/Time last_start\nbool paused\n"
               "string current_goal\nuint8 IDLE = 0\nuint8 MISSION = 1\nuint8 MISSION_FROM_GOAL = 2\n"
               "uint8 GOTO = 3\nuint8 GOTO_POI = 4\nuint8 DOCKING_LOCAL = 5\n"
               "uint8 UNDOCKING_LOCAL = 6\nuint8 DOCKING_MAP = 7\nuint8 state\n")}
for _typename, _definition in OUTDOORNAV_DEFINITIONS.items():
    STORE.register(get_types_from_msg(_definition, _typename))
HASHES[AUTONOMY] = "RIHS01_dba59db94904e7a6ac1e1c0c99c8ad244360fcdb19d4e5dfa8c5b4c72de52d1d"
AUTONOMY_STATES = {0: "IDLE", 1: "MISSION", 2: "MISSION_FROM_GOAL", 3: "GOTO", 4: "GOTO_POI",
                   5: "DOCKING_LOCAL", 6: "UNDOCKING_LOCAL", 7: "DOCKING_MAP"}
AUTONOMY_FIELDS = ["autonomy_source_stamp_us", "autonomy_last_start_us", "autonomy_time_basis",
                   "autonomy_frame_hex", "autonomy_state_code", "autonomy_state_reported",
                   "autonomy_paused_reported", "autonomy_goal_hex", "autonomy_status_basis"]
# Neither stamp has a declared epoch at that pin, so neither is called UTC, boot-relative or attested.
AUTONOMY_TIME_BASIS = "REPORTED_STAMPS_WITH_NO_EPOCH_DECLARED_BY_THE_PRODUCER_NOT_UTC_OR_BOOT_RELATIVE"
AUTONOMY_BASIS = ("REPORTED_AUTONOMY_STATE_OF_THE_SERIAL_NAMESPACED_TOPIC_NOT_VERIFIED_ACTIVITY_"
                  "NOT_A_SAFETY_STATE_AND_NOT_A_FAULT_REPORT")
# Every setter read at the pin writes UNKNOWN; WIRED/WIRELESS are accepted, not claimed to occur.
CHARGER_TYPES = {0: "UNKNOWN", 1: "WIRED", 2: "WIRELESS"}
CHARGING_CURRENTS = (("total", "current"), ("battery_1", "current_battery_1"),
                     ("battery_2", "current_battery_2"))
CHARGING_FIELDS = ["charging_source_stamp_us", "charging_frame_hex", "charging_reported_flag",
                   "charging_flag_basis", "charging_charger_type_code",
                   "charging_charger_type_reported", "charging_current_unit_basis"]
CHARGING_FIELDS += ["charging_current_" + name + suffix
                    for name, _ in CHARGING_CURRENTS for suffix in ("_a", "_status")]
# control_msgs declares no interface units at all, so the caller names both the axis kind and
# the quantity per (joint, interface); the unit vocabulary is the adopted JointState one.
INTERFACE_KINDS = {"angular_position": "rad", "angular_velocity": "rad_s", "angular_effort": "nm",
                   "linear_position": "m", "linear_velocity": "m_s", "linear_effort": "n"}
DYNAMIC_FIELDS = ["dynamic_joint_count", "dynamic_joint_index", "dynamic_joint_name_hex",
                  "dynamic_interface_count", "dynamic_interface_index",
                  "dynamic_interface_name_hex", "dynamic_interface_kind_configured",
                  "dynamic_interface_status", "dynamic_joint_source_stamp_us"]
DYNAMIC_FIELDS += ["dynamic_interface_value_" + unit for unit in dict.fromkeys(INTERFACE_KINDS.values())]
HASHES[TF] = "RIHS01_e369d0f05a23ae52508854b66f6aa0437f3449d652e8cbf22d5abe85d020f087"
TF_FIELDS = ["tf_entry_count", "tf_entry_index", "tf_parent_frame_hex", "tf_child_frame_hex", "tf_source_stamp_us"]
TF_FIELDS += ["tf_translation_m_" + a for a in "xyz"] + ["tf_rotation_xyzw_" + a for a in "xyzw"]
# ros2/rcl_interfaces 7aa3caf43377ea6ad615bc1040832e2c7566bfbe (jazzy, packages 2.0.4):
# GetParameters.srv request "string[] names" / response "ParameterValue[] values", and the
# build-time generated <srv>_Event shape fixed by rosidl_parser/definition.py. ParameterValue
# and ServiceEventInfo come from the pinned Jazzy typestore itself, not restated here.
GET_PARAMETERS_EVENT = "rcl_interfaces/srv/GetParameters_Event"
SERVICE_STORE = get_typestore(Stores.ROS2_JAZZY)
SERVICE_DEFINITIONS = {
    "rcl_interfaces/srv/GetParameters_Request": "string[] names\n",
    "rcl_interfaces/srv/GetParameters_Response": "rcl_interfaces/ParameterValue[] values\n",
    GET_PARAMETERS_EVENT: ("service_msgs/ServiceEventInfo info\n"
                           "rcl_interfaces/srv/GetParameters_Request[<=1] request\n"
                           "rcl_interfaces/srv/GetParameters_Response[<=1] response\n")}
for _typename, _definition in SERVICE_DEFINITIONS.items():
    SERVICE_STORE.register(get_types_from_msg(_definition, _typename))
GET_PARAMETERS_EVENT_HASH = "RIHS01_05d51816fd0bee4d74555d40921f20dc6d020458334c6d3880e70adc213a2874"
if SERVICE_STORE.hash_rihs01(normalize_msgtype(GET_PARAMETERS_EVENT)) != GET_PARAMETERS_EVENT_HASH:
    raise ValueError("pinned GetParameters service event layout changed")
HASHES[GET_PARAMETERS_EVENT] = GET_PARAMETERS_EVENT_HASH
# ServiceEventInfo.event_type; a request side and a response side are paired per key.
SERVICE_EVENT_KINDS = ("REQUEST_SENT", "REQUEST_RECEIVED", "RESPONSE_SENT", "RESPONSE_RECEIVED")
SERVICE_STAMP_BASIS = "RCL_NOW_ON_SENDING_OR_RECEIVING_HOST_NOT_WIRE_TIME_MAY_BE_SIM_TIME"
# The saved event only shows the topic it was published on. Deriving a service name
# from it would need the producer's topic suffix rule, which is not verified here.
SERVICE_ENDPOINT_BASIS = "OBSERVED_EVENT_TOPIC_ONLY_SERVICE_NAME_SUFFIX_RULE_NOT_VERIFIED_HERE"
SERVICE_FIELDS = ["service_event_topic_hex", "service_endpoint_basis", "service_node_identity",
                  "service_event_kind", "service_transaction_status", "service_client_gid_hex",
                  "service_sequence_number", "service_event_stamp_us", "service_stamp_basis",
                  "service_request_name_count", "service_response_value_count",
                  "service_transaction_events", "service_source_indexes"]
PX4_BATTERY = "px4_msgs/msg/BatteryStatus"
# PX4-Autopilot 99c40407ffd7ac184e2d7b4b293f36f10fe561ef (v1.15.4) msg/BatteryStatus.msg,
# sha256 3064fa302ba3de7485dce15f4f36cb5e903a0edf49ebd6816f7c1466267067e9: the 40 field
# declarations in source order, published by dds_topics.yaml as /fmu/out/battery_status.
# Comments and the 28 constants are left out because they change neither the wire layout
# nor RIHS01; the hash below was computed from the verbatim upstream file, so a
# transcription error here fails at import instead of decoding a different layout.
PX4_BATTERY_DEFINITION = """uint64 timestamp
bool connected
float32 voltage_v
float32 voltage_filtered_v
float32 current_a
float32 current_filtered_a
float32 current_average_a
float32 discharged_mah
float32 remaining
float32 scale
float32 time_remaining_s
float32 temperature
uint8 cell_count
uint8 source
uint8 priority
uint16 capacity
uint16 cycle_count
uint16 average_time_to_empty
uint16 serial_number
uint16 manufacture_date
uint16 state_of_health
uint16 max_error
uint8 id
uint16 interface_error
float32[14] voltage_cell_v
float32 max_cell_voltage_delta
bool is_powering_off
bool is_required
uint16 faults
uint32 custom_faults
uint8 warning
uint8 mode
float32 average_power
float32 available_energy
float32 full_charge_capacity_wh
float32 remaining_capacity_wh
float32 design_capacity
uint16 average_time_to_full
uint16 over_discharge_count
float32 nominal_voltage
"""
PX4_BATTERY_HASH = "RIHS01_ce63b675bae07df04e7f268a98d4a033cefe4732baf8d0918a152eb5870776bd"
STORE.register(get_types_from_msg(PX4_BATTERY_DEFINITION, PX4_BATTERY))
if STORE.hash_rihs01(PX4_BATTERY) != PX4_BATTERY_HASH:
    raise ValueError("pinned PX4 BatteryStatus layout changed")
HASHES[PX4_BATTERY] = PX4_BATTERY_HASH
PX4_BATTERY_FIELDS = ["px4_battery_source_stamp_us", "px4_battery_source_stamp_basis",
                      "px4_battery_id_reported", "px4_battery_connected_reported",
                      "px4_battery_voltage_status", "px4_battery_current_status",
                      "px4_battery_consumed_status", "px4_battery_remaining_status"]
# In dds_topics.h.em SendTopicsSubs::update the client reads session->time_offset and
# passes time_offset_us into the generated per-topic serializer, so the serialized
# timestamp is the hrt value plus the agent offset. module.yaml declares UXRCE_DDS_SYNCT
# default 1 (enabled); only when it is disabled is the offset forced to zero and the
# value raw boot-relative hrt. A saved bag shows neither that setting, nor whether sync
# had converged at a sample, nor the offset, so the number is preserved exactly and its
# basis is reported as not established. Never subtract a guessed offset.
PX4_STAMP_BASIS = "SERIALIZED_UORB_US_UXRCE_DDS_SYNCT_OFFSET_NOT_ESTABLISHED"
SCALARS = {
    "Temperature": ("temperature", "temperature_k", 273.15, 0., None),
    "FluidPressure": ("fluid_pressure", "external_pressure_pa", 0., 0., None),
    "RelativeHumidity": ("relative_humidity", "relative_humidity_fraction", 0., 0., 1.),
    "Illuminance": ("illuminance", "illuminance_lux", 0., 0., None)}
SENSOR_FIELDS = [entry[1] for entry in SCALARS.values()] + [
    "sensor_variance_status", "sensor_frame_hex", "sensor_report_type",
    "magnetic_covariance_status"]
SENSOR_FIELDS += ["magnetic_field_tesla_" + a for a in "xyz"]
SENSOR_FIELDS += ["magnetic_field_" + a + "_status" for a in "xyz"]
SENSOR_FIELDS += ["range_m", "range_status", "range_min_m", "range_max_m", "range_fov_rad",
                  "range_radiation_code", "range_radiation_reported", "range_variance_m2", "range_variance_status"]
SCAN_FIELDS = ["scan_ray_count", "scan_ray_index", "scan_angle_rad", "scan_first_stamp_us",
               "scan_ray_offset_s", "scan_period_s", "scan_intensity_device_units", "scan_intensity_status"]
LIMIT = 16 * 1024 * 1024
BASE = ["record_time_us", "source_record_hex", *JSON_ELECTRICAL_FIELDS, *BATTERY_TEMPERATURE_FIELDS]
GROUPS = (("orientation", "xyzw", "orientation_quaternion"),
          ("angular_velocity", "xyz", "angular_velocity_rad_s"),
          ("linear_acceleration", "xyz", "linear_acceleration_m_s2"))
IMU_FIELDS = [f"{prefix}_{axis}" for _, axes, prefix in GROUPS for axis in axes]
IMU_FIELDS += [f"{name}_covariance_status" for name, _, _ in GROUPS]
DIAG_FIELDS = ["diagnostic_entry_count", "diagnostic_entry_index", "diagnostic_level_reported",
               "diagnostic_level_code", "diagnostic_name_hex", "diagnostic_message_hex", "diagnostic_hardware_id_hex",
               "diagnostic_value_count", "diagnostic_value_pairs_hex"]
MOTION_GROUPS = (("pose", "pose", "position", "xyz", "pose_position_m"),
                 ("pose", "pose", "orientation", "xyzw", "pose_orientation_quaternion"),
                 ("twist", "twist", "linear", "xyz", "twist_linear_m_s"),
                 ("twist", "twist", "angular", "xyz", "twist_angular_rad_s"))
MOTION_FIELDS = [f"{prefix}_{axis}" for _, _, _, axes, prefix in MOTION_GROUPS for axis in axes]
MOTION_FIELDS += ["pose_frame_hex", "twist_frame_hex", "pose_covariance_status", "twist_covariance_status"]
MOTION_FIELDS += ["pose_covariance_row_major_hex", "twist_covariance_row_major_hex"]
# The six covariance axes, in the row/column order the pinned message documents. The pose block's
# last three are the REP rotation axes, NOT the four quaternion components.
COVARIANCE_AXES = {"pose": ("x_m", "y_m", "z_m", "rot_x_rad", "rot_y_rad", "rot_z_rad"),
                   "twist": ("vx_m_s", "vy_m_s", "vz_m_s", "wx_rad_s", "wy_rad_s", "wz_rad_s")}
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


def px4_battery_quantities(record):
    """One saved CDR record through the existing read-only px4-battery semantics.

    The six meanings and every sentinel rule stay in convert_ardupilot_battery_csv;
    nothing about PX4 electrical arithmetic is restated here. The serialized uORB
    microsecond value is carried through unchanged and reported with an explicit
    basis; it is never adjusted, merged with the bag record time or read as a clock.
    """
    stamp, identifier, connected = record["timestamp"], record["id"], record["connected"]
    if type(stamp) is not int or not 0 <= stamp <= 2**63 - 1:
        raise ValueError("invalid PX4 uORB microsecond stamp")
    if type(identifier) is not int or not 0 <= identifier <= 255 or type(connected) is not bool:
        raise ValueError("invalid PX4 battery identity or connection flag")
    selected = [record[name] for name in PX4_REQUIRED[2:]]
    # plain() leaves a non-finite float as text; a selected quantity must not be one.
    if any(type(value) is not float for value in selected):
        raise ValueError("non-finite PX4 battery quantity is not a measurement")
    text = ",".join(PX4_REQUIRED) + "\n" + ",".join(
        [str(stamp), str(identifier)] + [repr(value) for value in selected]) + "\n"
    converted = list(csv.reader(io.StringIO(battery_convert(text, "px4-battery"))))
    if len(converted) != 2:
        raise ValueError("selected px4-battery conversion did not return one record")
    values = dict(zip(NORMALIZED, converted[1][len(PX4_REQUIRED):]))
    # Availability is read back from that converter's own decision, not re-derived.
    status = {name: ("REPORTED" if values[field] else "NOT_PROVIDED") for name, field in
              (("voltage", "battery_voltage_v"), ("current", "battery_current_a"),
               ("consumed", "battery_consumed_ah"), ("remaining", "battery_remaining_fraction"))}
    return dict({field: value for field, value in values.items() if value},
                px4_battery_source_stamp_us=stamp, px4_battery_source_stamp_basis=PX4_STAMP_BASIS,
                px4_battery_id_reported=identifier, px4_battery_connected_reported=int(connected),
                **{f"px4_battery_{name}_status": value for name, value in status.items()})


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
        stamp = entry["goal_info"]["stamp"]
        time = json_time({"header": {"stamp": stamp, "frame_id": ""}}, "ros-battery-json")
        # `GoalInfo.stamp` is nanosecond resolution ("Time when the goal was accepted"); the microsecond
        # column truncates it, and compact list selection strips `status_list` from the retained source,
        # so the exact value would otherwise be lost with no trace. Carry it instead of dropping it.
        output.append(dict(action_entry_count=len(entries), action_entry_index=index,
            action_uuid_hex="hex:" + bytes(identifier).hex(), action_accepted_stamp_us=time,
            action_accepted_stamp_ns=stamp["sec"] * 10**9 + stamp["nanosec"],
            action_status_code=code,
            action_status_reported=(names[code] + "_REPORTED") if 0 <= code < len(names) else f"UNKNOWN_{code}"))
    return output


def service_transaction_rows(events):
    """Pair saved GetParameters events by (topic, client_gid, sequence_number).

    Nothing is called here: these are recorded introspection events. Time proximity is
    never used as identity, and only a fully matched transaction associates a reported
    value with the name that was requested for it. Every participating event is
    validated and kept joinable: the caller retains each one in the report under its
    source message index, and every row carries the indexes it was built from. Returns
    the rows and the number of distinct transactions, which is not the row count
    because one match expands.
    """
    order, groups = [], {}
    for row, topic, record, index, capture_ns in events:
        info = record["info"]
        kind, sequence, gid = info["event_type"], info["sequence_number"], info["client_gid"]
        if type(kind) is not int or not 0 <= kind <= 255:
            raise ValueError("invalid service event kind")
        if type(sequence) is not int or not -(2**63) <= sequence < 2**63:
            raise ValueError("invalid service sequence number")
        if not isinstance(gid, list) or len(gid) != 16 or any(
                type(byte) is not int or not -128 <= byte <= 255 for byte in gid):
            raise ValueError("invalid service client gid")
        for side in ("request", "response"):
            if not isinstance(record[side], list) or len(record[side]) > 1:
                raise ValueError("service event carries a bounded single request and response")
        # Every saved event's own stamp is validated here, not only the last one.
        stamp = json_time({"header": {"stamp": info["stamp"], "frame_id": ""}}, "ros-battery-json")
        # A known kind carrying the other side's contents is a contradiction, not a match.
        contradictory = bool(record["response"] if kind < 2 else record["request"]) if kind < 4 else False
        identity = (topic, bytes(byte & 255 for byte in gid).hex(), sequence)
        if identity not in groups:
            groups[identity] = []
            order.append(identity)
        groups[identity].append((kind, record, row, index, stamp, contradictory, capture_ns))
        if len(groups[identity]) > 4096:
            raise ValueError("service transaction event bound")
    output = []
    for identity in order:
        topic, gid, sequence = identity
        entries = groups[identity]
        requests = [item for item in entries if item[0] < 2 and item[1]["request"]]
        responses = [item for item in entries if item[0] >= 2 and item[1]["response"]]
        names = [item[1]["request"][0]["names"] for item in requests]
        values = [item[1]["response"][0]["values"] for item in responses]
        last = entries[-1]
        row = dict(last[2])
        indexes = [item[3] for item in entries]
        # The whole transaction is joinable to each retained source by the
        # source_message_index key, without copying any event body into the row: the
        # bodies stay in the report once each. capture_ns is the record's exact
        # nanoseconds, not record_time_us scaled back up.
        row["source_record_hex"] = text_hex(json.dumps(
            {"transaction": [{"source_message_index": item[3], "event_type": item[0],
                              "capture_ns": item[6]}
                             for item in entries]}, separators=(",", ":")))
        base = dict(service_event_topic_hex=text_hex(topic),
                    service_endpoint_basis=SERVICE_ENDPOINT_BASIS,
                    service_node_identity="NOT_ESTABLISHED_FROM_SAVED_EVENT",
                    service_event_kind=SERVICE_EVENT_KINDS[last[0]] if last[0] < 4 else f"UNKNOWN_{last[0]}",
                    service_client_gid_hex="hex:" + gid, service_sequence_number=sequence,
                    service_event_stamp_us=last[4], service_stamp_basis=SERVICE_STAMP_BASIS,
                    service_transaction_events=len(entries),
                    # The downstream CSV contract is unquoted, so this list is pipe separated.
                    service_source_indexes="|".join(str(item) for item in indexes))
        if any(item[0] > 3 for item in entries):
            output.append((row, dict(base, service_transaction_status="UNKNOWN_EVENT_KIND_RETAINED")))
            continue
        if any(item[5] for item in entries):
            output.append((row, dict(base, service_transaction_status="CONTRADICTORY_EVENT_SIDE",
                                     service_request_name_count=len(names),
                                     service_response_value_count=len(values))))
            continue
        if not requests and not responses:
            output.append((row, dict(base, service_transaction_status="METADATA_ONLY",
                                     service_request_name_count=0, service_response_value_count=0)))
            continue
        if any(item != names[0] for item in names) or any(item != values[0] for item in values):
            output.append((row, dict(base, service_transaction_status="DUPLICATE_CONFLICT",
                                     service_request_name_count=len(names),
                                     service_response_value_count=len(values))))
            continue
        counted = dict(base, service_request_name_count=len(names[0]) if names else 0,
                       service_response_value_count=len(values[0]) if values else 0)
        if not responses:
            output.append((row, dict(counted, service_transaction_status="REQUEST_ONLY")))
            continue
        if not requests:
            output.append((row, dict(counted, service_transaction_status="RESPONSE_ONLY")))
            continue
        if len(names[0]) != len(values[0]):
            output.append((row, dict(counted, service_transaction_status="COUNT_MISMATCH")))
            continue
        if not names[0]:
            output.append((row, dict(counted, service_transaction_status="MATCHED_EMPTY")))
            continue
        # Adopted typed expansion, called read-only, exactly as the parameter YAML path
        # does. The observed topic is supplied only because that validator requires a
        # node-shaped string; the resulting node field is cleared below, because a node
        # identity is not established by a saved event.
        expanded = parameter_event_quantities(dict(node=topic, stamp=last[1]["info"]["stamp"],
            new_parameters=[{"name": name, "value": value}
                            for name, value in zip(names[0], values[0])],
            changed_parameters=[], deleted_parameters=[]))
        for values_row in expanded:
            values_row["parameter_event_kind"] = "SERVICE_GET_PARAMETERS_REPORTED"
            values_row["parameter_node_hex"] = ""
            output.append((row, dict(counted, service_transaction_status="MATCHED", **values_row)))
    return output, len(order)


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


def sqlite_storage_schemas(reader):
    """Storage schema version of every SQLite3 file the pinned reader opened.

    rosbags0.11.5 exposes one storage for a bare .db3 and a DirectoryReader holding
    one storage per file for a bag directory; only its sqlite3 storage carries
    ``schema``. An empty result means the version was not established.
    """
    storage = getattr(reader, "storage", None)
    stores = getattr(storage, "storages", None)
    stores = list(stores) if stores is not None else ([storage] if storage is not None else [])
    versions = [getattr(x, "schema", None) for x in stores]
    return () if not versions or any(not isinstance(v, int) or v < 1 for v in versions) else tuple(versions)


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


def diagnostic_key_values(entry):
    """The entry's reported KeyValue pairs, in source order, as an ordered list - never a dict.

    `diagnostic_msgs/DiagnosticStatus` carries `KeyValue[] values`, and a publisher may repeat a key. A
    mapping would silently drop the earlier pair, so the pairs are carried as a JSON array of two-element
    arrays: order kept, duplicates kept, nothing merged. Only the count and that hex-transported list leave
    here; no key is interpreted as a unit or a quantity.
    """
    pairs = entry["values"]
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("invalid diagnostic key/value array")
    ordered = []
    for pair in pairs:
        key, value = pair["key"], pair["value"]
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("invalid diagnostic key/value text")
        ordered.append([key, value])
    return len(ordered), text_hex(json.dumps(ordered, ensure_ascii=False, sort_keys=False,
                                             separators=(",", ":")))


def diagnostic_quantities(record):
    entries = record["status"]
    if not entries:
        # An explicit empty report: no level, no code, no pairs. This is not a healthy device.
        return [{"diagnostic_entry_count": 0, "diagnostic_level_reported": "EMPTY_ARRAY"}]
    result = []
    for index, entry in enumerate(entries):
        code = entry["level"]
        if type(code) is not int or not 0 <= code <= 255:
            raise ValueError("invalid diagnostic level")
        count, pairs_hex = diagnostic_key_values(entry)
        result.append({"diagnostic_entry_count": len(entries), "diagnostic_entry_index": index,
            "diagnostic_level_code": code,
            "diagnostic_level_reported": {0: "OK", 1: "WARN", 2: "ERROR", 3: "STALE"}.get(code, f"UNKNOWN_{code}"),
            "diagnostic_name_hex": text_hex(entry["name"]),
            "diagnostic_message_hex": text_hex(entry["message"]),
            "diagnostic_hardware_id_hex": text_hex(entry["hardware_id"]),
            "diagnostic_value_count": count, "diagnostic_value_pairs_hex": pairs_hex})
    return result


def motion_quantities(record):
    parent, child = record["header"]["frame_id"], record["child_frame_id"]
    if not parent or not child:
        raise ValueError("Odometry frame labels required; no implicit world frame")
    result = motion_part(record["pose"], "pose", parent)
    result.update(motion_part(record["twist"], "twist", child))
    # Odometry carries its own header stamp; expose it in the existing column rather than leaving
    # a usable source clock readable only inside the retained raw record. Callers that synthesise a
    # header without one (the Amiga export) keep the column empty, exactly as before.
    if "stamp" in record["header"]:
        result["motion_source_stamp_us"] = json_time(record, "ros-battery-json")
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
        # The reported values themselves, in the source's own row-major order, through the same
        # JSON-array-in-hex transport the diagnostic key/value pairs already use. Nothing is
        # reordered, rotated, normalized or checked for positive-semidefiniteness.
        result[f"{name}_covariance_row_major_hex"] = text_hex(json.dumps(
            [float(value) for value in values], separators=(",", ":")))
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
    # The pinned interface that produced this row, stated in the row itself. Several of these
    # columns -- `temperature_k` above all -- are shared engineering-unit names that other
    # producers also write through the same profile, and until now a ROS row's own type existed
    # only inside the retained `source_record_hex`. Naming it here is what lets a reader tell a
    # reported sensor reading from another producer's quantity without parsing the raw record.
    # It is the interface's name, not a device identity and not a claim about what was measured.
    result = {"sensor_frame_hex": text_hex(record["header"]["frame_id"]),
              "sensor_report_type": typename}
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


def charging_quantities(record):
    """Reported charging status. The flag is the publisher's inference, not a measurement.

    `adc_battery.cpp` IsCharging() is false unless `charger_connected` arrives from
    hardware/io_state, then true when an averaged charge current passes 0.1 or an averaged
    voltage passes 41.2. The dual publisher also collapses a disagreement between the two
    batteries to false/UNKNOWN, so a false does not separate "not charging" from "conflict".
    """
    code = record["charger_type"]
    if type(code) is not int or not 0 <= code <= 255:
        raise ValueError("charger type must be the reported uint8")
    flag = record["charging"]
    if type(flag) is not bool:
        raise ValueError("charging flag must be the reported boolean")
    row = {"charging_source_stamp_us": json_time(record, "ros-battery-json"),
           "charging_frame_hex": text_hex(record["header"]["frame_id"]),
           "charging_reported_flag": "true" if flag else "false",
           "charging_flag_basis": "PUBLISHER_DERIVED_FROM_CONNECTION_AND_THRESHOLDS_NOT_MEASURED_OR_SAFE",
           "charging_charger_type_code": code,
           # An undefined code keeps its number rather than being guessed into a known one.
           "charging_charger_type_reported": CHARGER_TYPES.get(code, "UNKNOWN_CODE_RETAINED"),
           "charging_current_unit_basis": "AMPERES_DECLARED_BY_MESSAGE_COMMENT_ONLY_PUBLISHER_SCALING_UNCOMMENTED"}
    for name, field in CHARGING_CURRENTS:
        value = record[field]
        # The message itself declares NaN as this type's unavailable value ("NaN for single
        # battery configuration"), so it needs no opt-in; it is never a zero. Infinity is not
        # declared anywhere and stays a refusal.
        if value == "NaN":
            row["charging_current_" + name + "_status"] = "UNAVAILABLE_SOURCE_NAN"
            continue
        row["charging_current_" + name + "_status"] = "REPORTED"
        row["charging_current_" + name + "_a"] = finite(value)
    return row


def autonomy_status_quantities(record):
    """One reported OutdoorNav autonomy state, with nothing about it resolved or rated.

    `current_goal` is an opaque reference: the pinned message gives it no meaning, so it is
    retained verbatim and never read as a place, mission or waypoint. An undocumented state code
    keeps its number rather than being guessed into a neighbouring constant.
    """
    code, paused = record["state"], record["paused"]
    if type(code) is not int or not 0 <= code <= 255:
        raise ValueError("autonomy state must be the reported uint8")
    if type(paused) is not bool:
        raise ValueError("autonomy paused flag must be the reported boolean")
    return {"autonomy_source_stamp_us": json_time(record, "ros-battery-json"),
            "autonomy_last_start_us": json_time(
                {"header": {"stamp": record["last_start"], "frame_id": ""}}, "ros-battery-json"),
            "autonomy_time_basis": AUTONOMY_TIME_BASIS,
            "autonomy_frame_hex": text_hex(record["header"]["frame_id"]),
            "autonomy_state_code": code,
            "autonomy_state_reported": AUTONOMY_STATES.get(code, "UNKNOWN_CODE_RETAINED"),
            "autonomy_paused_reported": "true" if paused else "false",
            "autonomy_goal_hex": text_hex(record["current_goal"]),
            "autonomy_status_basis": AUTONOMY_BASIS}


def dynamic_joint_quantities(record, kinds, allow_missing_joint_values=False, used=None):
    """One row per reported (joint, interface), and one row for a joint that reports none.

    control_msgs declares no interface units, so an unmapped interface keeps its identity
    and accounting and carries no value at all. `used` collects the configuration keys this
    record consumed; the caller checks consumption across the whole conversion.
    """
    names, entries = record["joint_names"], record["interface_values"]
    if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("dynamic joint names must be nonempty and unique")
    # The .msg only implies these lengths in comments, so they are enforced here.
    if len(entries) != len(names):
        raise ValueError("dynamic joint interface array length mismatch")
    stamp = json_time(record, "ros-battery-json")
    if not names:
        return [{"dynamic_joint_count": 0, "dynamic_joint_source_stamp_us": stamp}]
    rows = []
    used = set() if used is None else used
    for index, name in enumerate(names):
        interfaces, values = entries[index]["interface_names"], entries[index]["values"]
        if len(values) != len(interfaces):
            raise ValueError("dynamic joint interface value length mismatch")
        if (any(not isinstance(key, str) or not key for key in interfaces)
                or len(set(interfaces)) != len(interfaces)):
            raise ValueError("dynamic joint interface names must be nonempty and unique")
        if not interfaces:
            # A joint that reports no interface still exists in the declaration. Keep its
            # identity, index and count with an explicit empty disposition and no value.
            rows.append({"dynamic_joint_count": len(names), "dynamic_joint_index": index,
                         "dynamic_joint_name_hex": text_hex(name),
                         "dynamic_interface_count": 0,
                         "dynamic_interface_status": "NOT_PROVIDED",
                         "dynamic_joint_source_stamp_us": stamp})
            continue
        for position, key in enumerate(interfaces):
            kind = kinds.get(name + "." + key)
            row = {"dynamic_joint_count": len(names), "dynamic_joint_index": index,
                   "dynamic_joint_name_hex": text_hex(name),
                   "dynamic_interface_count": len(interfaces), "dynamic_interface_index": position,
                   "dynamic_interface_name_hex": text_hex(key),
                   "dynamic_joint_source_stamp_us": stamp}
            if kind is None:
                # Referenced and counted, with the missing declaration named. No value.
                row["dynamic_interface_status"] = "UNMAPPED_NO_CALLER_UNIT"
                rows.append(row)
                continue
            used.add(name + "." + key)
            row["dynamic_interface_kind_configured"] = kind
            value = values[position]
            # joint_state_broadcaster initialises every interface with quiet_NaN, so a NaN is
            # "not provided or not yet updated" and reuses the adopted opt-in.
            if allow_missing_joint_values and value == "NaN":
                row["dynamic_interface_status"] = "UNAVAILABLE_SOURCE_NAN"
            else:
                row["dynamic_interface_status"] = "REPORTED"
                row["dynamic_interface_value_" + INTERFACE_KINDS[kind]] = finite(value)
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


MCAP_MAGIC = b"\x89MCAP0\r\n"
MCAP_SUMMARY_RECORDS = 4096
MCAP_HEADER_BYTES = 65536


def mcap_string(body, position):
    (length,) = struct.unpack_from("<I", body, position)
    end = position + 4 + length
    if end > len(body):
        raise ValueError("malformed MCAP string")
    return body[position + 4:end].decode("utf-8"), end


def mcap_interval_preflight(paths, start_ns, stop_ns, topics):
    """Bound and check the MCAP index the storage will trust, before reading.

    Declared sizes are never taken on faith: the summary walk, the chunk record
    at each declared offset and the message index of the selected chunks are
    re-read from the file. Chunk CRCs and payload bytes are not verified here.
    """
    totals = dict(declared_chunks=0, selected_chunks=0, selected_chunk_bytes=0,
                  selected_index_bytes=0, selected_compressed_bytes=0,
                  chunks_ending_at_start_ns=0, index_declared_selected_messages=0)
    if len(set(paths)) != len(paths):
        raise ValueError("bag metadata repeats an MCAP file")
    for path in paths:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size < 45 or stream.read(8) != MCAP_MAGIC:
                raise ValueError("MCAP file magic required")
            stream.seek(size - 37)
            footer = stream.read(37)
            if len(footer) != 37 or footer[-8:] != MCAP_MAGIC or footer[:9] != b"\x02\x14" + bytes(7):
                raise ValueError("MCAP footer required")
            data_end = size - 37
            (summary_start,) = struct.unpack_from("<Q", footer, 9)
            try:
                data_start = mcap_header_preflight(stream, data_end)
                if not data_start <= summary_start < data_end:
                    raise ValueError("interval selection requires an MCAP summary section")
                mcap_file_preflight(stream, data_start, summary_start, data_end,
                                    start_ns, stop_ns, topics, totals)
            except (struct.error, IndexError) as error:
                raise ValueError("malformed MCAP header, summary or index") from error
    return totals


def mcap_header_preflight(stream, data_end):
    """Bound the header record before the external reader reads its sized strings."""
    stream.seek(8)
    head = stream.read(9)
    length = struct.unpack_from("<Q", head, 1)[0] if len(head) == 9 else data_end
    if head[0] != 0x01 or length > min(MCAP_HEADER_BYTES, data_end - 17):
        raise ValueError("MCAP header record outside its bound")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("truncated MCAP header record")
    mcap_string(body, mcap_string(body, 0)[1])  # profile then library, inside the record
    return 17 + length


def mcap_file_preflight(stream, data_start, summary_start, data_end, start_ns, stop_ns, topics, totals):
    schemas, channels, chunks, offset, records = {}, {}, [], summary_start, 0
    stream.seek(summary_start)
    while offset < data_end:
        records += 1
        if records > MCAP_SUMMARY_RECORDS:
            raise ValueError("MCAP summary record count exceeds bound")
        head = stream.read(9)
        length = struct.unpack_from("<Q", head, 1)[0] if len(head) == 9 else data_end
        # Bind the remaining summary budget before this body is allocated.
        if length > min(LIMIT - (offset - summary_start) - 9, data_end - offset - 9):
            raise ValueError("truncated or oversized MCAP summary record")
        body = stream.read(length)
        if len(body) != length:
            raise ValueError("truncated MCAP summary record")
        offset += 9 + length
        if head[0] == 0x03:
            key = struct.unpack_from("<H", body, 0)[0]
            schemas[key] = mcap_string(body, 2)[0]
        elif head[0] == 0x04:
            key, schema_id = struct.unpack_from("<HH", body, 0)
            channels[key] = (mcap_string(body, 4)[0], schema_id)
        elif head[0] == 0x08:
            chunks.append(body)
    if not schemas or not chunks:
        raise ValueError("interval selection requires MCAP summary schemas and a chunk index")
    named = {key: (topic, schemas.get(schema_id, "")) for key, (topic, schema_id) in channels.items()}
    if len(set(named.values())) != len(named):
        raise ValueError("ambiguous MCAP channel topic and schema pair")
    seen = set()
    for body in chunks:
        start, end, chunk_offset, chunk_length = struct.unpack_from("<QQQQ", body, 0)
        (index_bytes,) = struct.unpack_from("<I", body, 32)
        if index_bytes % 10 or 36 + index_bytes > len(body):
            raise ValueError("malformed MCAP message index offsets")
        index_offsets = dict(struct.unpack_from("<HQ", body, 36 + step * 10)
                             for step in range(index_bytes // 10))
        position = 36 + index_bytes
        (index_length,) = struct.unpack_from("<Q", body, position)
        compression, position = mcap_string(body, position + 8)
        compressed_size, uncompressed_size = struct.unpack_from("<QQ", body, position)
        if position + 16 != len(body) or len(index_offsets) != index_bytes // 10:
            raise ValueError("malformed MCAP chunk index record")
        if compression not in ("", "zstd", "lz4"):
            raise ValueError("only uncompressed, zstd or lz4 MCAP chunks are within this bounded scope")
        if not index_offsets:
            raise ValueError("MCAP chunk without a message index is unsupported")
        name_bytes = len(compression.encode("utf-8"))
        # The equality below bounded the decompressed size only while the two sizes were equal.
        # A zstd chunk breaks that, so uncompressed_size is bounded in its own right.
        if (start > end or chunk_length > LIMIT or uncompressed_size > LIMIT
                or chunk_length != 49 + name_bytes + compressed_size
                or (not compression and compressed_size != uncompressed_size)):
            raise ValueError("MCAP chunk size or time declaration rejected")
        if (chunk_offset < data_start or chunk_offset in seen
                or chunk_offset + chunk_length + index_length > summary_start):
            raise ValueError("MCAP chunk offsets outside the data section")
        seen.add(chunk_offset)
        stream.seek(chunk_offset)
        head = stream.read(49 + name_bytes)
        if len(head) != 49 + name_bytes or head[0] != 0x06:
            raise ValueError("declared MCAP chunk offset is not a chunk record")
        record_length, chunk_start, chunk_end, expanded = struct.unpack_from("<QQQQ", head, 1)
        # The compression name sits between the header fields and records_size, so every field
        # after it moves by its length; read the name and compare it with the index entry.
        (name_length,) = struct.unpack_from("<I", head, 37)
        (records_size,) = struct.unpack_from("<Q", head, 41 + name_bytes)
        if (record_length != chunk_length - 9 or (chunk_start, chunk_end, expanded) != (start, end, uncompressed_size)
                or name_length != name_bytes or head[41:41 + name_bytes] != compression.encode("utf-8")
                or records_size != compressed_size):
            raise ValueError("MCAP chunk record does not match its index entry")
        totals["declared_chunks"] += 1
        if end == start_ns:
            totals["chunks_ending_at_start_ns"] += 1
        if end < start_ns or start >= stop_ns:
            continue
        totals["selected_chunks"] += 1
        totals["selected_chunk_bytes"] += uncompressed_size
        totals["selected_index_bytes"] += index_length
        # The reader holds the compressed bytes and the decompressed bytes of every selected
        # chunk at once, so the compressed side needs its own bound rather than only the file one.
        totals["selected_compressed_bytes"] += compressed_size
        if (totals["selected_chunk_bytes"] > LIMIT or totals["selected_index_bytes"] > LIMIT
                or totals["selected_compressed_bytes"] > LIMIT):
            raise ValueError("selected MCAP chunk or message index volume exceeds bound")
        if compression == "zstd":
            # Decided before the reader touches this chunk. A known frame content size overrides
            # the reader's max_output_size, so the declared uncompressed_size alone bounds nothing;
            # the frame header is what must agree, and trailing data must not hide behind it.
            stream.seek(chunk_offset + 49 + name_bytes)
            compressed = stream.read(compressed_size)
            if len(compressed) != compressed_size:
                raise ValueError("truncated compressed MCAP chunk")
            try:
                parameters = zstandard.get_frame_parameters(compressed)
                check_single_zstd_frame(compressed, parameters.has_checksum)
            except zstandard.ZstdError as error:
                raise ValueError("invalid compressed MCAP chunk frame") from error
            if (parameters.content_size == zstandard.CONTENTSIZE_UNKNOWN
                    or parameters.content_size != uncompressed_size
                    or parameters.window_size > LIMIT):
                raise ValueError("compressed MCAP chunk frame disagrees with its declared size")
        elif compression == "lz4":
            # Same decision point as zstd, and the same already-bounded sizes. The difference is
            # the reader: rosbags passes uncompressed_size to zstd as max_output_size but drops it
            # for lz4 (`'lz4': lambda x, _: lz4_decompress(x)`), so nothing downstream bounds the
            # expansion and this check is the only bound. MCAP's lz4 payload is an LZ4 *frame*,
            # which is what the official writer emits, so the frame header is read first and the
            # body is expanded exactly once under a hard cap.
            stream.seek(chunk_offset + 49 + name_bytes)
            compressed = stream.read(compressed_size)
            if len(compressed) != compressed_size:
                raise ValueError("truncated compressed MCAP chunk")
            try:
                info = lz4.frame.get_frame_info(compressed)
            except RuntimeError as error:
                raise ValueError("invalid compressed MCAP chunk frame") from error
            # A stored content size is optional in the frame header: 0 means the writer stored
            # none, which is not a declaration of zero and is not read as one.
            if (info["content_size"] and info["content_size"] != uncompressed_size) or info["block_size"] > LIMIT:
                raise ValueError("compressed MCAP chunk frame disagrees with its declared size")
            decompressor = lz4.frame.LZ4FrameDecompressor()
            expanded_bytes = decompressor.decompress(compressed, max_length=uncompressed_size)
            if (len(expanded_bytes) != uncompressed_size or not decompressor.eof
                    or decompressor.unused_data):
                # Short, expanding past the cap, or hiding a further frame behind this one.
                raise ValueError("compressed MCAP chunk frame disagrees with its declared size")
        base = chunk_offset + chunk_length
        stream.seek(base)
        region = stream.read(index_length)
        if len(region) != index_length:
            raise ValueError("truncated MCAP message index")
        ordered = sorted((value, key) for key, value in index_offsets.items())
        ends = [entry[0] for entry in ordered][1:] + [base + index_length]
        for (entry_offset, channel), entry_end in zip(ordered, ends):
            if (entry_offset < base or entry_end > base + index_length
                    or entry_end - entry_offset < 15 or (entry_end - entry_offset - 15) % 16):
                raise ValueError("MCAP message index layout unsupported")
            record = region[entry_offset - base:entry_end - base]
            declared, entry_channel, payload = struct.unpack_from("<QHI", record, 1)
            if (record[0] != 0x07 or declared != entry_end - entry_offset - 9
                    or entry_channel != channel or payload != entry_end - entry_offset - 15):
                raise ValueError("MCAP message index record does not match its offsets")
            topic = named.get(channel, (None, None))[0]
            counted = topic is not None and (topics is None or topic in topics)
            for step in range(payload // 16):
                stamp, message_offset = struct.unpack_from("<QQ", record, 15 + step * 16)
                if not start <= stamp <= end or message_offset >= uncompressed_size:
                    raise ValueError("MCAP message index entry outside its chunk")
                if counted and start_ns <= stamp < stop_ns:
                    totals["index_declared_selected_messages"] += 1


def convert_file_compressed(path, metadata, names, joint_kinds, stamped_motion_role, pointcloud_unit, pointcloud_compact=False, scan_compact=False, list_compact=None, allow_missing_voltage=False, allow_missing_joint_values=False, scan_unavailable_infinity=False, interface_kinds=None):
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
                                 scan_unavailable_infinity=scan_unavailable_infinity,
                                 interface_kinds=interface_kinds)
        report.update(source_file_compression="zstd", expanded_storage_bytes=total,
                      source_metadata_yaml_hex="hex:" + (path / "metadata.yaml").read_bytes().hex())
        if len(json.dumps(report).encode()) > LIMIT:
            raise ValueError("compressed source report exceeds bound")
        return output, report


def convert(path, joint_kinds=None, stamped_motion_role=None, pointcloud_unit=None, pointcloud_compact=False, scan_compact=False, list_compact=None, *, start_ns=None, stop_ns=None, topics=None, allow_missing_voltage=False, allow_missing_joint_values=False, ros1=False, scan_unavailable_infinity=False, interface_kinds=None, legacy_schema=None):
    if type(ros1) is not bool or (ros1 and (joint_kinds or stamped_motion_role or pointcloud_unit or pointcloud_compact or scan_compact or list_compact or start_ns is not None or stop_ns is not None or topics is not None or allow_missing_joint_values or interface_kinds)):
        raise ValueError("ROS1 selection accepts only the fixed battery and IMU record scope")
    qualified_hashes = ROS1_HASHES if ros1 else HASHES
    if legacy_schema is not None and (ros1 or legacy_schema not in LEGACY_SCHEMA_CHOICES):
        # A fixed enumeration, not free text, and not available for the ROS1 path.
        raise ValueError("legacy schema selection must name one supported ROS2 schema contract")
    legacy_type = None if legacy_schema is None else LEGACY_SCHEMA_CHOICES[legacy_schema]
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
    interface_kinds = {} if interface_kinds is None else interface_kinds
    # Shape first: a list or a string must refuse here, not raise AttributeError below.
    if not isinstance(interface_kinds, dict) or any(
            not isinstance(key, str) or key.count(".") != 1 or not all(key.split("."))
            or kind not in INTERFACE_KINDS for key, kind in interface_kinds.items()):
        raise ValueError("invalid joint interface configuration")
    if type(scan_compact) is not bool or (scan_compact and (pointcloud_compact or pointcloud_unit)):
        raise ValueError("scan selection cannot use point cloud configuration")
    if type(scan_unavailable_infinity) is not bool or (scan_unavailable_infinity and not scan_compact):
        raise ValueError("scan return policy requires explicit compact LaserScan selection")
    if list_compact is not None and (list_compact not in ("actions", "parameters")
            or pointcloud_compact or scan_compact or pointcloud_unit):
        raise ValueError("invalid or conflicting compact list selection")
    compact = pointcloud_compact or scan_compact or list_compact is not None
    if type(allow_missing_joint_values) is not bool or allow_missing_joint_values and (
            compact or not (joint_kinds or interface_kinds)):
        raise ValueError("joint missing-value policy requires explicit joint kinds and message selection")
    if interface_kinds and compact:
        raise ValueError("joint interface configuration cannot use compact selection")
    if type(allow_missing_voltage) is not bool or allow_missing_voltage and compact:
        raise ValueError("battery missing-voltage policy cannot use other compact selection")
    if legacy_type is not None and compact:
        # Another compact selection would silently exclude the named legacy type; refuse instead.
        raise ValueError("legacy schema selection cannot use another compact selection")
    selected_type = ({"actions": ACTION_STATUS, "parameters": PARAMETER_EVENT}[list_compact]
        if list_compact else POINT_CLOUD if pointcloud_compact else "sensor_msgs/msg/LaserScan")
    source_key = "list_sources" if list_compact else "pointcloud_sources" if pointcloud_compact else "scan_sources"
    if compact and (joint_kinds or stamped_motion_role):
        raise ValueError("compact selection cannot silently ignore other message configuration")
    if not isinstance(joint_kinds, dict) or any(not isinstance(k, str) or not k or v not in ("angular", "linear") for k, v in joint_kinds.items()):
        raise ValueError("invalid joint configuration")
    path = Path(path)
    mcap_window = None
    storage = None
    if ros1 and not path.is_file():
        raise ValueError("ROS1 requires a single saved bag file")
    paths = list(path.rglob("*")) if path.is_dir() else [path]
    if path.is_symlink() or any(p.is_symlink() for p in paths):
        raise ValueError("symlink bag not supported")
    if windowed and not path.is_dir():
        raise ValueError("window selection requires an uncompressed sqlite3 or indexed MCAP bag directory")
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
        storage = metadata["rosbag2_bagfile_information"].get("storage_identifier")
        if windowed and (storage not in ("sqlite3", "mcap")
                         or metadata["rosbag2_bagfile_information"].get("compression_mode", "") != ""):
            raise ValueError("window selection requires an uncompressed sqlite3 or indexed MCAP bag directory")
        names = metadata["rosbag2_bagfile_information"]["relative_file_paths"]
        if not names or any(not isinstance(name, str) or Path(name).is_absolute()
                            or ".." in Path(name).parts or not (path / name).is_file()
                            for name in names):
            raise ValueError("bag metadata references an unsupported path")
        if legacy_type is not None and metadata["rosbag2_bagfile_information"].get("compression_mode", "") != "":
            # The compressed route has no legacy selection; refuse rather than ignore the request.
            raise ValueError("legacy schema selection requires an uncompressed sqlite3 bag")
        if metadata["rosbag2_bagfile_information"].get("compression_mode", "").lower() == "file":
            return convert_file_compressed(path, metadata, names, joint_kinds, stamped_motion_role, pointcloud_unit, pointcloud_compact, scan_compact, list_compact, allow_missing_voltage, allow_missing_joint_values, scan_unavailable_infinity, interface_kinds)
        if windowed and storage == "mcap":
            # The MCAP reader trusts declared chunk bounds and skips a chunk whose
            # last message is exactly the requested start; check and bound it first.
            mcap_window = mcap_interval_preflight([path / name for name in names], start_ns, stop_ns, topics)
    output = io.StringIO(newline="")
    output_fields = BASE + IMU_FIELDS + DIAG_FIELDS + MOTION_FIELDS + SENSOR_FIELDS + TF_FIELDS + SCAN_FIELDS + JOINT_FIELDS + ACTION_FIELDS + CLOUD_FIELDS + NAVSAT_FIELDS + PARAMETER_FIELDS
    output_fields += NAV2_FIELDS + VEHICLE_FIELDS + PX4_BATTERY_FIELDS + SERVICE_FIELDS + CHARGING_FIELDS + DYNAMIC_FIELDS + AUTONOMY_FIELDS
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
    service_events = []
    consumed_interfaces = set()
    report["service_sources"] = []
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
        storage_start = start_ns
        if mcap_window is not None:
            # rosbags0.11.5 keeps a chunk only when start < message_end_time, so ask
            # storage for one nanosecond earlier and apply the exact interval here.
            storage_start = None if start_ns == 0 else start_ns - 1
            report["capture_window"]["mcap"] = dict(mcap_window,
                storage_request_start_ns=storage_start, boundary_filtered_records=0,
                accepted_contract="uncompressed, zstd or lz4 chunks, other names refused; a zstd chunk must be one complete frame whose declared content size equals the chunk uncompressed_size, window within bound; an lz4 chunk must be one complete frame that expands to exactly the chunk uncompressed_size under that cap, with no trailing frame and block size within bound, and any content size it stores must agree; summary schemas, chunk index and message index; no bag compression",
                verified="magic, bounded header, footer, bounded summary walk, chunk record against its index entry including its compression name, declared uncompressed and compressed volume bounds, zstd frame framing and declared content size, lz4 frame header and a capped expansion to exactly the declared size with no trailing data, selected message index entries, read count against index declaration",
                not_verified="chunk CRC, message payload bytes, chunks outside the selected interval")
        storage_schemas = sqlite_storage_schemas(reader) if not ros1 else ()
        legacy_connections = set()
        legacy_unselected = set()
        if legacy_type is not None:
            sqlite_bag = storage == "sqlite3" if path.is_dir() else path.suffix == ".db3"
            if not sqlite_bag or set(storage_schemas) - {1, 2} or len(set(storage_schemas)) != 1:
                # Only a SQLite3 bag whose every file is at one schema that cannot carry a
                # hash may use the selection; a newer schema must present its own hash.
                raise ValueError("legacy schema selection requires a legacy sqlite3 storage schema")
            storage_schema = storage_schemas[0]
            report["legacy_schema_selection"] = dict(selected=legacy_schema, type=legacy_type,
                storage_schema=storage_schema, basis=LEGACY_SCHEMA_BASIS,
                local_definition_hash=qualified_hashes[legacy_type],
                unselected_legacy_connections=[],
                verified="CDR decode against the locally selected definition, record timestamps, bounds",
                not_verified="the producer, its binary, the recorded schema and any type hash, which the bag does not carry")
        for connection in connections:
            if compact and connection.msgtype != selected_type:
                continue
            if (legacy_type is not None and not connection.digest
                    and connection.msgdef.format == MessageDefinitionFormat.NONE
                    and connection.ext.serialization_format == "cdr"):
                # The bag carries neither a hash nor a definition for this connection. When the
                # caller named this exact type, decode it with the fixed local definition and
                # record that nothing was verified; any other type keeps only its name, which is
                # not evidence, so it is retained and accounted without being decoded.
                (legacy_connections if connection.msgtype == legacy_type else legacy_unselected).add(connection.id)
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
                declared_hash = (declared.generate_msgdef(connection.msgtype)[1] if ros1
                                 else declared.hash_rihs01(normalize_msgtype(connection.msgtype)))
                if declared_hash != connection.digest:
                    raise ValueError("embedded ROS definition does not match selected schema")
        if legacy_unselected:
            report["legacy_schema_selection"]["unselected_legacy_connections"] = [
                dict(topic=c.topic, type=c.msgtype, declared_messages=c.msgcount,
                     disposition="TYPE_NOT_SELECTED_NO_HASH_PRESENT_NOT_DECODED")
                for c in connections if c.id in legacy_unselected]
            if len(json.dumps(report).encode()) > LIMIT:
                raise ValueError("legacy declaration report exceeds bound")
        for connection, timestamp, raw in reader.messages(connections=connections, start=storage_start, stop=stop_ns):
            if topics is not None and connection.topic not in topics:
                raise ValueError("storage yielded an unselected topic")
            if mcap_window is not None and timestamp < start_ns:
                if storage_start is None or timestamp < storage_start:
                    raise ValueError("storage yielded a record outside the requested interval")
                report["capture_window"]["mcap"]["boundary_filtered_records"] += 1
                continue
            report["source_messages"] += 1
            if windowed and not start_ns <= timestamp < stop_ns:
                raise ValueError("storage yielded a record outside selected interval")
            raw_limit = 8 * 1024 * 1024 if compact else 12000
            if not 0 <= timestamp <= 2**63 - 1 or len(raw) > raw_limit:
                raise ValueError("record timestamp or size outside finite scope")
            source = {"topic": connection.topic, "type": connection.msgtype,
                      "type_hash": ("ABSENT_NOT_VERIFIED_LEGACY_SQLITE_SCHEMA"
                                    if connection.id in legacy_connections else connection.digest),
                      "capture_ns": str(timestamp),
                      "raw_ros1_hex" if ros1 else "raw_cdr_hex": bytes(raw).hex()}
            if connection.id in legacy_connections:
                source.update(type_hash_basis=LEGACY_SCHEMA_BASIS, selected_schema=legacy_schema,
                              local_definition_hash=qualified_hashes[connection.msgtype],
                              storage_schema=storage_schema)
            if (connection.msgtype not in qualified_hashes or connection.id in legacy_unselected
                    or (compact and connection.msgtype != selected_type)):
                if connection.id in legacy_unselected:
                    source["legacy_disposition"] = "TYPE_NOT_SELECTED_NO_HASH_PRESENT_NOT_DECODED"
                if compact:
                    source["reason"] = "NOT_SELECTED_" + (list_compact.upper() if list_compact else "POINTCLOUD" if pointcloud_compact else "LASERSCAN")
                report_bytes += len(json.dumps(source).encode())
                if report_bytes > LIMIT:
                    raise ValueError("unsupported report exceeds bound")
                report["unsupported_records"].append(source)
                continue
            store = (SERVICE_STORE if connection.msgtype == GET_PARAMETERS_EVENT else
                     RANGE_STORE if connection.msgtype == RANGE and connection.digest == RANGE_VARIANCE_HASH else STORE)
            record = decode(raw, normalize_msgtype(connection.msgtype), store, ros1=ros1)
            # Validate header stamp/frame, without promoting it to capture/UTC time.
            if connection.msgtype not in (TF, ACTION_STATUS, PARAMETER_EVENT, NAV2_FEEDBACK,
                                         STEERING_REPORT, PX4_BATTERY, GET_PARAMETERS_EVENT):
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
            if connection.msgtype == GET_PARAMETERS_EVENT:
                # A transaction spans records, so pair them after the whole bag is read.
                # Every participating event is retained here exactly once, under the
                # index each row joins back to, and bounded while collecting.
                index = report["source_messages"] - 1
                source.update(source_message_index=index,
                              source_cdr_sha256=hashlib.sha256(raw).hexdigest())
                report_bytes += len(json.dumps(source).encode())
                if report_bytes > LIMIT or len(service_events) >= 65536:
                    raise ValueError("retained service source report exceeds bound")
                report["service_sources"].append(source)
                service_events.append((row, connection.topic, record, index, timestamp))
                report["decoded_messages"] += 1
                continue
            if connection.msgtype == BATTERY:
                fields = BASE[2:] + (["battery_voltage_disposition"] if allow_missing_voltage else [])
                quantities = [dict(zip(fields, json_quantities(record, "ros-battery-json",
                                                             allow_missing_voltage=allow_missing_voltage)))]
            elif connection.msgtype == PX4_BATTERY:
                quantities = [px4_battery_quantities(record)]
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
            elif connection.msgtype == CHARGING:
                quantities = [charging_quantities(record)]
            elif connection.msgtype == AUTONOMY:
                quantities = [autonomy_status_quantities(record)]
            elif connection.msgtype == DYNAMIC_JOINT:
                quantities = dynamic_joint_quantities(record, interface_kinds,
                                                      allow_missing_joint_values, consumed_interfaces)
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
        if service_events:
            paired, transactions = service_transaction_rows(service_events)
            for row, values in paired:
                writer.writerow(dict(row, **values))
                report["output_records"] += 1
                if output.tell() > (128 * 1024 * 1024 if compact else LIMIT):
                    raise ValueError("converted CSV exceeds bound")
            report["service_transactions"] = transactions
            report["service_correlation"] = "topic, client_gid and sequence_number; not time proximity"
            report["service_identity_limit"] = "a client_gid cannot be mapped to a node or service name from the saved event alone"
            report["service_source_retention"] = "every participating event is retained once in service_sources; join a row to a source by matching service_source_indexes against the source_message_index key, which is the global selected-message index and not a position in this service-only list"
            report["service_retained_sources"] = len(report["service_sources"])
        if not service_events:
            report.pop("service_sources")
        if not windowed and report["source_messages"] != declared_selected:
            raise ValueError("bag message count mismatch")
        if windowed:
            if report["source_messages"] > declared_selected:
                raise ValueError("selected messages exceed declared bag count")
            report["capture_window"]["selected_messages"] = report["source_messages"]
            if mcap_window is not None and report["source_messages"] != mcap_window["index_declared_selected_messages"]:
                raise ValueError("read messages differ from the MCAP index declaration for this interval")
        if topics is not None:
            report["topic_selection"]["read_messages"] = report["source_messages"]
    if not report["decoded_messages"]:
        raise ValueError("no supported messages")
    if consumed_interfaces != set(interface_kinds):
        # A configuration no selected record consumed is a caller error, whether the pair was
        # absent from a decoded message or no DynamicJointState was selected at all. It is
        # never silently carried over to unrelated selected data.
        raise ValueError("configured joint interface absent from the reported records")
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
    # Reading failures of the pinned reader (tabs, an unclosed flow, any scan/parse error) are
    # rejections of this input, so they must arrive as this function's own ValueError and not as
    # the reader's exception type. Documents are counted explicitly rather than left to the
    # reader: a single-document read would otherwise drop every declaration after the first if
    # the reader ever stopped objecting.
    try:
        tokens = list(yaml.scan(text))
    except Exception as error:
        raise ValueError("unreadable parameter YAML") from error
    if any(type(token).__name__ in ("AliasToken", "AnchorToken", "TagToken", "DirectiveToken") for token in tokens):
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
    try:
        documents = list(yaml.compose_all(text))
    except Exception as error:
        raise ValueError("unreadable parameter YAML") from error
    if len(documents) != 1:
        raise ValueError("one parameter YAML document required")
    namespaces(documents[0])
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


# Nav2 1.1.20 occupancy sentinels (nav2_util/occ_grid_values.hpp at that tag).
OCC_GRID_UNKNOWN, OCC_GRID_FREE, OCC_GRID_OCCUPIED = -1, 0, 100
# Same bound the adopted ISOXML grid writer uses, so a practical map is not capped smaller here.
MAP_CELL_LIMIT = 16 * 1024 * 1024
# Same practical ceiling the adopted ISOXML grid writer enforces while writing.
MAP_OUTPUT_LIMIT = 128 * 1024 * 1024


def read_pgm_p5(data):
    """Binary PGM (`P5`, maxval 255) -> (width, height, samples). The only qualified format.

    Nav2 loads the image through ImageMagick, which reads far more than this. Only the one output
    `nav2_map_server`'s own saver writes is qualified here, and everything else is refused by name
    rather than approximated.
    """
    if not data.startswith(b"P5") or not data[2:3].isspace():
        # `P52 3 255` is not a 2-wide P5: the magic ends at a whitespace byte or the file is not one.
        raise ValueError("qualified map image is binary PGM 'P5'; other formats are not decoded here")
    fields, offset = [], 2
    while len(fields) < 3:
        while offset < len(data) and data[offset:offset + 1].isspace():
            offset += 1
        if data[offset:offset + 1] == b"#":
            while offset < len(data) and data[offset:offset + 1] not in (b"\n", b"\r"):
                offset += 1
            continue
        start = offset
        while offset < len(data) and not data[offset:offset + 1].isspace():
            offset += 1
        token = data[start:offset]
        if not token or not token.isdigit():
            raise ValueError("PGM header needs decimal width, height and maxval")
        fields.append(int(token))
    if data[offset:offset + 1] and not data[offset:offset + 1].isspace():
        raise ValueError("PGM header must end with one whitespace byte before the samples")
    width, height, maxval = fields
    if maxval != 255:
        raise ValueError("only 8-bit PGM (maxval 255) is qualified; other depths are not scaled here")
    if width <= 0 or height <= 0:
        raise ValueError("PGM needs positive width and height")
    if width * height > MAP_CELL_LIMIT:
        raise ValueError("map exceeds the supported cell count")
    samples = data[offset + 1:]
    if len(samples) < width * height:
        raise ValueError("PGM pixel data is truncated")
    if len(samples) > width * height:
        raise ValueError("PGM carries bytes beyond its declared dimensions")
    return width, height, samples


def occupancy_of(sample, mode, negate, free, occupied):
    """One pixel -> one occupancy value, exactly as Nav2 1.1.20 `map_io.cpp` computes it.

    A PGM's three colour channels hold the same sample, so the channel mean is the sample itself.
    **Alpha is absent from this format**, so Nav2's Trinary alpha averaging and its Scale
    non-opaque rule cannot be exercised here; they are not implemented and not claimed.
    """
    shade = sample / 255.0
    if mode == "raw":
        # Raw reads brightness directly: neither negate nor the thresholds take part.
        percent = round(shade * 255)
        return int(percent) if OCC_GRID_FREE <= percent <= OCC_GRID_OCCUPIED else OCC_GRID_UNKNOWN
    occ = shade if negate else 1.0 - shade
    if occ > occupied:
        return OCC_GRID_OCCUPIED
    if occ < free:
        return OCC_GRID_FREE
    if mode == "trinary":
        return OCC_GRID_UNKNOWN
    return int(round_half_even((occ - free) / (occupied - free) * 100.0))


def round_half_even(value):
    """C++ `std::rint` under the default rounding mode: ties to even, which is Python's `round`."""
    return round(value)


def iter_occupancy_runs(width, height, samples, mode, negate, free, occupied):
    """Yield grid-order runs of equal occupancy, holding no more than one run at a time.

    Grid row 0 is the image's **last** row: Nav2 stores `data[width * (height - y - 1) + x]`, so
    the raster is flipped bottom-up and cell 0 sits at the origin. Yaw never enters -- `map_io`
    puts it only in the metadata quaternion and does not rotate or resample the raster.

    A generator, not a list: an irregular map can have nearly as many runs as cells, so neither the
    runs nor the rows built from them may be collected before writing.
    """
    table = [occupancy_of(v, mode, negate, free, occupied) for v in range(256)]
    start = length = 0
    current = None
    index = 0
    for row in range(height):
        base = (height - 1 - row) * width
        for column in range(width):
            value = table[samples[base + column]]
            if current is not None and value == current:
                length += 1
            else:
                if current is not None:
                    yield start, length, current, start // width, start % width
                start, length, current = index, 1, value
            index += 1
    if current is not None:
        yield start, length, current, start // width, start % width


def convert_map_metadata(path, capture_time_us, image_path=None, image_sha256=None):
    """Nav2 1.1.20 saved map declaration.

    The referenced image is **never** resolved or opened from the YAML. Occupancy cells are
    produced only when the caller supplies both the image path and the digest it must have.
    """
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
    if (image_path is None) != (image_sha256 is None):
        raise ValueError("map image needs both the caller's path and the digest it must have")
    cell_fields = ["map_cell_run_start_index", "map_cell_run_length", "map_cell_run_end_index",
                   "map_cell_run_start_row", "map_cell_run_start_column", "map_cell_occupancy"]
    scope = ("map YAML declaration only; image pixels, frame identity and physical map not "
             "verified")
    runs = None
    if image_path is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", str(image_sha256)):
            raise ValueError("map image digest must be lowercase hex sha256")
        with Path(image_path).open("rb") as stream:
            pixels = stream.read(MAP_CELL_LIMIT + 4096)
        if len(pixels) > MAP_CELL_LIMIT + 4095:
            raise ValueError("map image exceeds the supported size")
        digest = hashlib.sha256(pixels).hexdigest()
        if digest != image_sha256:
            raise ValueError("map image digest does not match the one the caller declared")
        width, height, samples = read_pgm_p5(pixels)
        total = width * height

        def walk():
            return iter_occupancy_runs(width, height, samples, mode, bool(negate), free, occupied)

        # First bounded pass: the declaration row needs the run count and the accounted total, and
        # neither the runs nor their rows may be kept. Counting costs one integer pair.
        runs = accounted = 0
        for _, length, _, _, _ in walk():
            runs += 1
            accounted += length
        if accounted != total:
            raise ValueError("occupancy runs do not account for every cell")
        declared_name = image.rsplit("/", 1)[-1]
        row.update(map_declaration_disposition="DECLARED_AND_CALLER_SUPPLIED_IMAGE_READ",
            map_image_sha256=digest, map_image_format="PGM_P5_MAXVAL_255",
            map_image_reference_match=("CALLER_FILE_NAME_MATCHES_DECLARED_REFERENCE"
                if Path(image_path).name == declared_name
                else "CALLER_FILE_NAME_DIFFERS_FROM_DECLARED_REFERENCE"),
            map_cell_columns=width, map_cell_rows=height, map_cell_total=total,
            map_cell_accounted=accounted, map_cell_run_count=runs,
            map_cell_order_basis="ROW_MAJOR_FROM_BOTTOM_LEFT_CELL_0_AT_DECLARED_ORIGIN",
            map_cell_occupancy_basis="NAV2_1_1_20_PERCENT_0_TO_100_OR_MINUS_1_UNKNOWN_NOT_OBSTACLE_TRUTH")
        row.update({key: "" for key in cell_fields})
        scope = ("map YAML declaration plus the caller-supplied image decoded by the pinned "
                 "Nav2 1.1.20 rule; frame identity, physical map and obstacle truth not verified")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(row), lineterminator="\n", restval="")

    def write(values):
        # Every emitted row is bounded while writing, before the expansion can grow unbounded.
        writer.writerow(values)
        if output.tell() > MAP_OUTPUT_LIMIT:
            raise ValueError("converted map CSV exceeds bound; no cell was sampled or dropped")

    writer.writeheader()
    write(row)
    emitted = 1
    if runs:
        # Second bounded pass over the same pixels: one row at a time, nothing accumulated. Cell
        # rows carry the cell columns only; the declaration above holds the geometry once.
        for start, length, value, start_row, start_column in walk():
            write({"record_time_us": capture_time_us, "map_cell_run_start_index": start,
                   "map_cell_run_length": length, "map_cell_run_end_index": start + length - 1,
                   "map_cell_run_start_row": start_row, "map_cell_run_start_column": start_column,
                   "map_cell_occupancy": value})
            emitted += 1
        if emitted != runs + 1:
            raise ValueError("occupancy runs do not account for every cell")
    return output.getvalue(), dict(output_records=emitted, clock="Unknown",
        source_sha256=hashlib.sha256(raw).hexdigest(), scope=scope)


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


def convert_rqt_console(path, capture_time_us):
    """Saved rqt_console3.0.2 writer contract, not generic escaped CSV."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT or b"\0" in raw:
        raise ValueError("rqt input exceeds bound or contains NUL")
    text = raw.decode("utf-8")
    header, separator, body = text.partition("\n")
    if header.rstrip("\r") != "message;severity;node;stamp;location" or not separator:
        raise ValueError("selected rqt five-column header required")
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_record_hex", "source_record_index",
        "log_message_hex", "log_level_code_reported", "log_level_reported",
        "log_node_hex", "log_location_hex", "ros_stamp_sec_reported",
        "ros_stamp_nanosec_reported", "log_time_basis"])
    position, count = 0, 0
    levels = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR", 50: "FATAL"}
    while position < len(body):
        start = position
        if body[position] != '"':
            raise ValueError("rqt row opening quote required")
        position += 1
        values = []
        for column in range(4):
            end = body.find('";"', position)
            if end < 0:
                raise ValueError("truncated rqt field")
            value = body[position:end]
            if column == 0:
                if any(c == '"' and (i == 0 or value[i - 1] != "\\") for i, c in enumerate(value)):
                    raise ValueError("unescaped rqt message quote")
                value = value.replace('\\"', '"')
            elif any(c in value for c in '"\r\n'):
                raise ValueError("ambiguous rqt non-message field")
            values.append(value)
            position = end + 3
        end = body.find('"', position)
        if end < 0 or any(c in body[position:end] for c in "\r\n"):
            raise ValueError("invalid rqt location")
        values.append(body[position:end])
        position = end + 1
        if position != len(body):
            if body.startswith("\r\n", position): position += 2
            elif body.startswith("\n", position): position += 1
            else: raise ValueError("invalid rqt record boundary")
        message, level, node, stamp, location = values
        if not re.fullmatch(r"[0-9]+", level) or not 0 <= int(level) <= 255:
            raise ValueError("invalid reported ROS log level")
        match = re.fullmatch(r"(-?[0-9]+)\.([0-9]{9})", stamp)
        if not match or not -(2**31) <= int(match[1]) < 2**31:
            raise ValueError("invalid reported ROS stamp")
        writer.writerow([capture_time_us, text_hex(body[start:position]), count, text_hex(message),
            int(level), levels.get(int(level), "UNKNOWN_RETAINED"), text_hex(node), text_hex(location),
            int(match[1]), int(match[2]), "REPORTED_ROS_CLOCK_NOT_CAPTURE_OR_UTC"])
        count += 1
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("rqt output exceeds bound")
    if not count: raise ValueError("empty rqt log")
    return output.getvalue(), {"source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_messages": count, "observations": count, "clock": "configured_capture_unknown",
        "source_scope": "saved_proxy_rows_not_complete_rosout", "unsupported": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--ros1-noetic", action="store_true", help="saved uncompressed ROSbag1: fixed BatteryState/Imu, retain original Header.seq")
    parser.add_argument("--joint-kind", action="append", default=[], metavar="NAME=angular|linear")
    parser.add_argument("--interface-kind", action="append", default=[],
                        metavar="JOINT.INTERFACE=angular_position|angular_velocity|angular_effort|linear_position|linear_velocity|linear_effort",
                        help="explicit unit for one reported DynamicJointState interface; control_msgs declares none")
    parser.add_argument("--stamped-motion-role", choices=("reported_estimate", "reported_reference"))
    parser.add_argument("--metadata-only", action="store_true", help="read version9 metadata.yaml declarations, not bag messages")
    parser.add_argument("--amiga-filter-json", action="store_true", help="saved SI-qualified FilterState protobuf JSON; no ROS or device connection")
    parser.add_argument("--rqt-console-csv", action="store_true", help="saved rqt_console3.0.2 message CSV; no ROS subscription")
    parser.add_argument("--parameters-only", action="store_true", help="read saved ROS parameter YAML declarations, not live settings")
    parser.add_argument("--map-metadata-only", action="store_true", help="read Nav2 1.1.20 map YAML declaration without opening its image")
    parser.add_argument("--map-image", type=Path, help="caller-associated Nav2 map image to decode; the YAML's own image reference is never followed")
    parser.add_argument("--map-image-sha256", help="digest the caller states the map image must have; a mismatch is refused")
    parser.add_argument("--capture-time-us", type=int, help="explicit capture clock for parameter/map YAML or saved rqt logs")
    parser.add_argument("--pointcloud-unit", choices=("m",), help="explicit xyz coordinate units for selected PointCloud2")
    parser.add_argument("--pointcloud-compact", action="store_true", help="select point clouds; retain original CDR once in report.json")
    parser.add_argument("--scan-compact", action="store_true", help="select LaserScan; retain original CDR once in report.json")
    parser.add_argument("--list-compact", choices=("actions", "parameters"),
                        help="select existing action/parameter lists; retain CDR once in report.json")
    parser.add_argument("--start-ns", type=int, help="inclusive capture timestamp; requires --stop-ns, uncompressed sqlite3 or indexed MCAP")
    parser.add_argument("--stop-ns", type=int, help="exclusive capture timestamp; requires --start-ns")
    parser.add_argument("--topic", action="append", help="exact topic selection; repeat up to32, sqlite3/MCAP without external compression")
    parser.add_argument("--allow-missing-voltage", action="store_true",
                        help="qualified BatteryState publisher NaN voltage is unavailable, not zero")
    parser.add_argument("--allow-missing-joint-values", action="store_true",
                        help="qualified JointState publisher NaN quantities are unavailable, not zero")
    parser.add_argument("--scan-unavailable-infinity", action="store_true",
                        help="qualified LaserScan positive infinity means unavailable return, not proven beyond-range")
    parser.add_argument("--legacy-schema", choices=tuple(sorted(LEGACY_SCHEMA_CHOICES)),
                        help="legacy sqlite3 bag without a type hash: decode the named type with this repo's fixed local definition; the producer schema stays UNVERIFIED")
    args = parser.parse_args()
    try:
        # Rejected before any route is chosen, so no earlier branch can ignore a map image the
        # caller supplied and still look like it succeeded.
        if (args.map_image is not None or args.map_image_sha256 is not None) and not args.map_metadata_only:
            raise ValueError("a map image belongs to the map YAML declaration route")
        if args.scan_unavailable_infinity and (not args.scan_compact or args.metadata_only or args.parameters_only
                or args.map_metadata_only or args.amiga_filter_json or args.ros1_noetic):
            raise ValueError("scan return policy requires compact ROS2 message conversion")
        if args.ros1_noetic and (args.metadata_only or args.parameters_only or args.map_metadata_only or args.amiga_filter_json):
            raise ValueError("ROS1 message selection conflicts with other input formats")
        if (args.allow_missing_voltage or args.allow_missing_joint_values) and (args.metadata_only or args.parameters_only or args.map_metadata_only):
            raise ValueError("missing-value policy requires message conversion")
        if args.legacy_schema and (args.metadata_only or args.parameters_only or args.map_metadata_only
                or args.amiga_filter_json or args.rqt_console_csv or args.ros1_noetic):
            raise ValueError("legacy schema selection requires ROS2 message conversion")
        kinds = {}
        for item in args.joint_kind:
            name, kind = item.rsplit("=", 1)
            if name in kinds:
                raise ValueError("duplicate joint configuration")
            kinds[name] = kind
        interfaces = {}
        for item in args.interface_kind:
            name, kind = item.rsplit("=", 1)
            if name in interfaces:
                raise ValueError("duplicate joint interface configuration")
            interfaces[name] = kind
        if interfaces and (args.metadata_only or args.parameters_only or args.map_metadata_only
                           or args.amiga_filter_json or args.rqt_console_csv or args.ros1_noetic):
            raise ValueError("joint interface configuration requires ROS2 message conversion")
        if args.rqt_console_csv:
            if args.amiga_filter_json or args.parameters_only or args.map_metadata_only or args.metadata_only or args.ros1_noetic or kinds or args.stamped_motion_role or args.pointcloud_unit or args.pointcloud_compact or args.scan_compact or args.list_compact or args.start_ns is not None or args.stop_ns is not None or args.topic is not None or args.allow_missing_voltage or args.allow_missing_joint_values or args.scan_unavailable_infinity:
                raise ValueError("rqt saved log conflicts with other input selections")
            output, report = convert_rqt_console(args.input, args.capture_time_us)
        elif args.amiga_filter_json:
            if args.parameters_only or args.map_metadata_only or args.metadata_only or kinds or args.stamped_motion_role or args.pointcloud_unit or args.pointcloud_compact or args.scan_compact or args.list_compact or args.start_ns is not None or args.stop_ns is not None or args.topic is not None or args.capture_time_us is not None or args.allow_missing_voltage or args.allow_missing_joint_values:
                raise ValueError("FilterState configuration conflicts with ROS selection")
            output, report = convert_amiga_filter_json(args.input)
        elif args.parameters_only or args.map_metadata_only:
            if (args.parameters_only and args.map_metadata_only) or args.metadata_only or kinds or args.stamped_motion_role or args.pointcloud_unit or args.pointcloud_compact or args.scan_compact or args.list_compact or args.start_ns is not None or args.stop_ns is not None or args.topic is not None:
                raise ValueError("parameter-file configuration conflicts with message/metadata selection")
            if args.parameters_only:
                output, report = convert_parameter_yaml(args.input, args.capture_time_us)
            else:
                output, report = convert_map_metadata(args.input, args.capture_time_us,
                                                      args.map_image, args.map_image_sha256)
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
                                     scan_unavailable_infinity=args.scan_unavailable_infinity,
                                     interface_kinds=interfaces, legacy_schema=args.legacy_schema)
        args.output_directory.mkdir()  # Refuse existing output/evidence, including input equality.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        report_text = json.dumps(report, separators=(",", ":")) if args.pointcloud_compact or args.scan_compact or args.list_compact else json.dumps(report, indent=2)
        (args.output_directory / "report.json").write_text(report_text, encoding="utf-8")
    except Exception:
        parser.exit(2, "ROS record conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
