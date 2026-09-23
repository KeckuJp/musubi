"""Authored bags and CDR, not physical recordings; shared semantics reused."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import struct
import tempfile
import unittest

import numpy as np
from rosbags.rosbag2 import Writer
from rosbags.rosbag2.writer import StoragePlugin
from scripts.convert_rosbag_observations import BATTERY, IMU, STORE, convert, decode, NAV2_FEEDBACK, NAV2_DEFINITIONS
from scripts.convert_rosbag_observations import PX4_BATTERY, PX4_BATTERY_HASH, PX4_STAMP_BASIS
from scripts.convert_rosbag_observations import (GET_PARAMETERS_EVENT,
    GET_PARAMETERS_EVENT_HASH, SERVICE_STAMP_BASIS, SERVICE_STORE)
from rosbags.typesys.msg import normalize_msgtype

ROOT = Path(__file__).resolve().parents[1]


def samples():
    types = STORE.types
    header = types["std_msgs/msg/Header"](types["builtin_interfaces/msg/Time"](2, 123456789), "sensor")
    battery = types[BATTERY](header, 24., float("nan"), -2., 3., 4., 4., .75,
                            2, 0, 0, True, np.array([12, 12], dtype=np.float32),
                            np.array([], dtype=np.float32), "slot", "reported-id")
    vector = types["geometry_msgs/msg/Vector3"]
    imu = types[IMU](header, types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.),
                    np.zeros(9), vector(1., 2., 3.), np.eye(3).flatten(),
                    vector(0., 0., 9.81), np.zeros(9))
    return battery, imu


def parameter_event_sample(integer=-7):
    types = STORE.types
    def parameter(code, name):
        value = types["rcl_interfaces/msg/ParameterValue"](
            code, False, integer, 1.25, "reported", np.array([0, 255], dtype=np.uint8),
            np.array([True, False]), np.array([-1, 2], dtype=np.int64),
            np.array([1.5, float("nan")]), ["first", "second"])
        return types["rcl_interfaces/msg/Parameter"](name, value)
    return types["rcl_interfaces/msg/ParameterEvent"](
        types["builtin_interfaces/msg/Time"](3, 12000), "/reported_node",
        [parameter(code, f"new_{code}") for code in range(1, 5)],
        [parameter(code, f"changed_{code}") for code in range(5, 10)], [parameter(0, "removed")])


def bag(path, plugin=StoragePlugin.SQLITE3, messages=None, hash_override=None, corrupt=False, wrong_definition=False, typestore=STORE, compression=None, msgdefs=None):
    writer = Writer(path, version=9, storage_plugin=plugin)
    if compression is not None:
        from rosbags.rosbag2.enums import CompressionFormat
        writer.set_compression(compression, CompressionFormat.ZSTD)
    with writer:
        for index, message in enumerate(messages or samples()):
            definition = (msgdefs[message.__msgtype__] if msgdefs and message.__msgtype__ in msgdefs
                          else typestore.generate_msgdef(message.__msgtype__, ros_version=2)[0])
            if wrong_definition:
                definition = definition.replace("float32 voltage", "float64 voltage")
            connection = writer.add_connection(f"/example/{index}", message.__msgtype__,
                typestore=typestore, rihs01=hash_override or typestore.hash_rihs01(message.__msgtype__),
                msgdef=definition)
            raw = typestore.serialize_cdr(message, message.__msgtype__)
            writer.write(connection, 7000123456, raw[:-8] if corrupt else raw)
        typename = "std_msgs/msg/String"
        connection = writer.add_connection("/unsupported", typename, typestore=STORE)
        writer.write(connection, 7000123456, STORE.serialize_cdr(STORE.types[typename]("retained"), typename))


LEGACY_TOPIC = "/battery"
LEGACY_CAPTURE_NS = 1631000000000000000


def legacy_battery(store, voltage=24., temperature=float("nan"), current=-2., charge=3.,
                   percentage=.75, present=True, status=2, health=1, technology=3):
    """BatteryState built from an older distro's own typestore, not the local one."""
    types = store.types
    header = types["std_msgs/msg/Header"](types["builtin_interfaces/msg/Time"](5, 250000000), "battery")
    return types[BATTERY](header, voltage, temperature, current, charge, 4., 4., percentage,
                          status, health, technology, present,
                          np.array([12., 12.], dtype=np.float32), np.array([], dtype=np.float32),
                          "slot", "reported-id")


def legacy_bag(path, payloads, *, schema=2, msgtype=BATTERY, serialization="cdr",
               topic=LEGACY_TOPIC, declared_hash=None, extra=(), compression=""):
    """Authored pre-schema-4 rosbag2 SQLite3 bag: no message_definitions, no type hash.

    rosbags0.11.5 detects schema 2 from a topics table carrying offered_qos_profiles and
    schema 1 from one without it, and reads type_description_hash only at schema >= 4,
    so such a bag can supply nothing but the type NAME. Authored bytes, no recording.
    """
    path.mkdir()
    connection = sqlite3.connect(path / "legacy_0.db3")
    qos = ", offered_qos_profiles TEXT NOT NULL" if schema >= 2 else ""
    connection.execute("CREATE TABLE topics(id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                       " type TEXT NOT NULL, serialization_format TEXT NOT NULL" + qos + ")")
    connection.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL,"
                       " timestamp INTEGER NOT NULL, data BLOB NOT NULL)")
    topics = [(topic, msgtype, list(payloads))] + [(t, m, list(p)) for t, m, p in extra]
    identifier = 0
    for index, (name, typename, records) in enumerate(topics, start=1):
        values = (index, name, typename, serialization) + (("",) if schema >= 2 else ())
        connection.execute("INSERT INTO topics VALUES(" + ",".join("?" * len(values)) + ")", values)
        for offset, raw in enumerate(records):
            identifier += 1
            connection.execute("INSERT INTO messages VALUES(?,?,?,?)",
                               (identifier, index, LEGACY_CAPTURE_NS + offset * 1000000, bytes(raw)))
    connection.commit()
    connection.close()
    declared = [
        "    - topic_metadata:\n"
        f"        name: {name}\n        type: {typename}\n"
        f"        serialization_format: {serialization}\n"
        '        offered_qos_profiles: ""\n'
        + (f"        type_description_hash: {declared_hash}\n" if declared_hash else "")
        + f"      message_count: {len(records)}\n"
        for name, typename, records in topics]
    total = sum(len(records) for _, _, records in topics)
    (path / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n  version: 4\n  storage_identifier: sqlite3\n"
        "  relative_file_paths:\n    - legacy_0.db3\n"
        f"  duration:\n    nanoseconds: {max(total - 1, 0) * 1000000}\n"
        f"  starting_time:\n    nanoseconds_since_epoch: {LEGACY_CAPTURE_NS}\n"
        f"  message_count: {total}\n  topics_with_message_count:\n" + "".join(declared)
        + f'  compression_format: "{"zstd" if compression else ""}"\n'
        + f'  compression_mode: "{compression}"\n')
    return path


PX4_TOPIC = "/fmu/out/battery_status"
PX4_CAPTURE_NS = 1789000000123456789


def px4_sample(stamp=123456789, voltage=16.2, current=3.5, discharged=250., remaining=.75,
               identifier=1, connected=True, temperature=float("nan")):
    """Authored px4_msgs/msg/BatteryStatus; no PX4 build, agent or recording."""
    return STORE.types[PX4_BATTERY](
        timestamp=stamp, connected=connected, voltage_v=voltage, voltage_filtered_v=16.1,
        current_a=current, current_filtered_a=3.4, current_average_a=3.3, discharged_mah=discharged,
        remaining=remaining, scale=1., time_remaining_s=1200., temperature=temperature,
        cell_count=4, source=0, priority=0, capacity=5000, cycle_count=7, average_time_to_empty=20,
        serial_number=11, manufacture_date=0, state_of_health=98, max_error=2, id=identifier,
        interface_error=0, voltage_cell_v=np.full(14, 4.05, dtype=np.float32),
        max_cell_voltage_delta=.01, is_powering_off=False, is_required=True, faults=0,
        custom_faults=0, warning=0, mode=0, average_power=56., available_energy=0.,
        full_charge_capacity_wh=0., remaining_capacity_wh=0., design_capacity=0.,
        average_time_to_full=0, over_discharge_count=0, nominal_voltage=14.8)


def px4_bag(path, messages, *, hash_override=None, definition=None, corrupt=False, step_ns=1000000):
    with Writer(path, version=9, storage_plugin=StoragePlugin.SQLITE3) as writer:
        connection = writer.add_connection(PX4_TOPIC, PX4_BATTERY, typestore=STORE,
            rihs01=hash_override or STORE.hash_rihs01(PX4_BATTERY),
            msgdef=definition or STORE.generate_msgdef(PX4_BATTERY, ros_version=2)[0])
        for index, message in enumerate(messages):
            raw = STORE.serialize_cdr(message, PX4_BATTERY)
            writer.write(connection, PX4_CAPTURE_NS + index * step_ns, raw[:-8] if corrupt else raw)


SERVICE_TOPIC = "/demo/get_parameters/_service_event"
EVENT_KEY = normalize_msgtype(GET_PARAMETERS_EVENT)
REQUEST_KEY = normalize_msgtype("rcl_interfaces/srv/GetParameters_Request")
RESPONSE_KEY = normalize_msgtype("rcl_interfaces/srv/GetParameters_Response")
GID_A, GID_B = bytes(range(16)), bytes(range(16, 32))


def parameter_value(**kwargs):
    """rcl_interfaces/msg/ParameterValue from the pinned Jazzy typestore, not hand JSON."""
    base = dict(type=0, bool_value=False, integer_value=0, double_value=0., string_value="",
                byte_array_value=np.array([], dtype=np.uint8),
                bool_array_value=np.array([], dtype=bool),
                integer_array_value=np.array([], dtype=np.int64),
                double_array_value=np.array([], dtype=np.float64), string_array_value=[])
    base.update(kwargs)
    return SERVICE_STORE.types["rcl_interfaces/msg/ParameterValue"](**base)


def service_event(kind, gid, sequence, names=None, values=None, sec=12, nanosec=500000):
    types = SERVICE_STORE.types
    info = types["service_msgs/msg/ServiceEventInfo"](event_type=kind,
        stamp=types["builtin_interfaces/msg/Time"](sec=sec, nanosec=nanosec),
        client_gid=np.frombuffer(gid, dtype=np.uint8).copy(), sequence_number=sequence)
    return types[EVENT_KEY](info=info,
        request=[types[REQUEST_KEY](names=names)] if names is not None else [],
        response=[types[RESPONSE_KEY](values=values)] if values is not None else [])


def service_bag(path, messages, *, plugin=StoragePlugin.MCAP, hash_override=None, definition=None):
    with Writer(path, version=9, storage_plugin=plugin) as writer:
        connection = writer.add_connection(SERVICE_TOPIC, GET_PARAMETERS_EVENT, typestore=SERVICE_STORE,
            rihs01=hash_override or GET_PARAMETERS_EVENT_HASH,
            msgdef=definition or SERVICE_STORE.generate_msgdef(EVENT_KEY, ros_version=2)[0])
        for index, message in enumerate(messages):
            writer.write(connection, 1789000000000000000 + index * 1000,
                         SERVICE_STORE.serialize_cdr(message, EVENT_KEY))


class RosbagObservationTests(unittest.TestCase):
    def test_saved_rqt_logs_reuse_common_output_without_losing_text_or_clock(self):
        from scripts.convert_rosbag_observations import convert_rqt_console
        header = "message;severity;node;stamp;location\n"
        message = 'literal \\path\nquote "; here" and trailing\\'
        def row(level=30, stamp="2.123456789", node="/node"):
            values = [message.replace('"', '\\"'), str(level), node, stamp, r"C:\src:fn:7"]
            return ";".join('"' + value + '"' for value in values) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rqt.csv"
            for index, count in enumerate((2, 10000)):
                text = header + row() + row(99, "-1.000000001") * (count - 1)
                source.write_bytes(text.encode())
                output, report = convert_rqt_console(source, 777)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["observations"], count)
                self.assertEqual(len(rows), count)
                self.assertEqual(bytes.fromhex(rows[0]["log_message_hex"][4:]).decode(), message)
                self.assertEqual(bytes.fromhex(rows[0]["log_location_hex"][4:]).decode(), r"C:\src:fn:7")
                self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), row())
                self.assertEqual(rows[-1]["log_level_reported"], "UNKNOWN_RETAINED")
                self.assertEqual(rows[-1]["ros_stamp_sec_reported"], "-1")
                self.assertEqual(rows[-1]["ros_stamp_nanosec_reported"], "1")
                target = Path(directory) / f"out{index}"
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                    str(source), str(target), "--rqt-console-csv", "--capture-time-us", "777"],
                    check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target / "observations.csv"), "--allow-equal-time"],
                        check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(common["observations"][0]["fields"]["log_level_reported"], "WARN")
                    self.assertEqual(common["observations"][-1]["fields"]["ros_stamp_sec_reported"], -1)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for text in (header, "wrong\n" + row(), header + row(256), header + row(stamp="1.123"),
                         header + row(node='bad"node'), header + row()[:-2], header + row() + "\n"):
                source.write_text(text)
                with self.assertRaises(ValueError): convert_rqt_console(source, 777)
            source.write_text(header + row())
            with self.assertRaises(ValueError): convert_rqt_console(source, None)
            failed = subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(source), str(Path(directory) / "bad"), "--rqt-console-csv", "--capture-time-us", "777",
                "--amiga-filter-json"], capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((Path(directory) / "bad").exists())

    def test_ros1_noetic_reuses_quantities_without_dropping_sequence(self):
        import sys
        from rosbags.rosbag1 import Writer as Writer1
        from scripts.convert_rosbag_observations import ROS1_STORE, ROS1_HASHES
        def write(path, sequence, failure=None):
            writer = Writer1(path)
            if failure == "compression":
                writer.set_compression(Writer1.CompressionFormat.BZ2)
            with writer:
                for message in samples():
                    message.header = ROS1_STORE.types["std_msgs/msg/Header"](
                        sequence, message.header.stamp, "source_frame")
                    if message.__msgtype__ == BATTERY:
                        message.voltage = 12. if sequence == 1 else 24.
                    definition, digest = ROS1_STORE.generate_msgdef(message.__msgtype__)
                    self.assertEqual(digest, ROS1_HASHES[message.__msgtype__])
                    connection = writer.add_connection("/" + message.__msgtype__, message.__msgtype__,
                        msgdef=definition.replace("voltage", "other_voltage") if failure == "definition" else definition,
                        md5sum="0" * 32 if failure == "hash" else digest)
                    raw = ROS1_STORE.serialize_ros1(message, message.__msgtype__)
                    writer.write(connection, 7000000001, raw[:-1] if failure == "truncated" else bytes(raw) + b"\0" if failure == "overlong" else raw)
                typename = "std_msgs/msg/String"
                connection = writer.add_connection("/other", typename, typestore=ROS1_STORE)
                writer.write(connection, 8000000001, ROS1_STORE.serialize_ros1(ROS1_STORE.types[typename]("retained"), typename))
        for sequence in (1, 4294967295):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); source = root / "saved.bag"; write(source, sequence)
                output, report = convert(source, ros1=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((report["source_messages"], report["decoded_messages"], report["output_records"]), (3, 2, 2))
                self.assertEqual(len(report["unsupported_records"]), 1)
                self.assertIn("raw_ros1_hex", report["unsupported_records"][0])
                retained = json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:]))
                self.assertEqual(retained["decoded"]["header"]["seq"], sequence)
                self.assertIn("raw_ros1_hex", retained)
                self.assertEqual(retained["capture_ns"], "7000000001")
                self.assertEqual(float(rows[0]["battery_voltage_v"]), 12. if sequence == 1 else 24.)
                self.assertEqual(float(rows[0]["battery_current_a"]), -2.)
                self.assertEqual(float(rows[1]["angular_velocity_rad_s_x"]), 1.)
                self.assertAlmostEqual(float(rows[1]["linear_acceleration_m_s2_z"]), 9.81)
                command = [sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                    str(source), str(root / "out"), "--ros1-noetic"]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(root / "out/observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                for options in ({"pointcloud_compact": True}, {"topics": ["/other"]}):
                    with self.assertRaises(ValueError): convert(source, ros1=True, **options)
        for failure in ("hash", "definition", "truncated", "overlong", "compression"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); source = root / "saved.bag"; write(source, 1, failure)
                result = subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                    str(source), str(root / "out"), "--ros1-noetic"], capture_output=True)
                self.assertEqual(result.returncode, 2, failure)
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertFalse((root / "out").exists())

    def test_autoware_reports_reuse_cdr_units_without_filling_unreported_axes(self):
        from scripts.convert_rosbag_observations import VELOCITY_REPORT, STEERING_REPORT, HASHES, plain, vehicle_report_quantities
        for name in (VELOCITY_REPORT, STEERING_REPORT):
            self.assertEqual(STORE.hash_rihs01(name), HASHES[name])
        for longitudinal, lateral, heading, steering in ((3.5, -.25, -.5, -.125), (-2., 0., .25, .5)):
            velocity = STORE.types[VELOCITY_REPORT](samples()[0].header, longitudinal, lateral, heading)
            angle = STORE.types[STEERING_REPORT](samples()[0].header.stamp, steering)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "bag"
                bag(source, messages=[velocity, angle])
                output, report = convert(source)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((report["decoded_messages"], report["output_records"]), (2, 2))
                self.assertEqual(float(rows[0]["vehicle_velocity_longitudinal_m_s"]), longitudinal)
                self.assertEqual(float(rows[0]["vehicle_velocity_lateral_m_s"]), lateral)
                self.assertEqual(float(rows[0]["vehicle_heading_rate_rad_s"]), heading)
                self.assertEqual(float(rows[1]["vehicle_steering_tire_angle_rad"]), steering)
                self.assertEqual(rows[1]["vehicle_report_stamp_us"], "2123456")
                self.assertEqual(rows[1]["vehicle_report_frame_hex"], "")
                self.assertEqual(rows[0]["twist_linear_m_s_z"], "")
                self.assertIn('"nanosec":123456789', bytes.fromhex(rows[0]["source_record_hex"][4:]).decode())
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    out = Path(directory) / "out.csv"; out.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(out), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(common["observations"][1]["fields"]["vehicle_steering_tire_angle_rad"], steering)
                    moving = common["observations"][0]["fields"]
                    self.assertEqual(moving["vehicle_velocity_longitudinal_m_s"], longitudinal)
                    self.assertEqual(moving["vehicle_velocity_lateral_m_s"], lateral)
                    self.assertEqual(moving["vehicle_heading_rate_rad_s"], heading)
                    self.assertEqual(moving["vehicle_report_frame_hex"], "hex:" + "sensor".encode().hex())
                    self.assertIsNone(moving["twist_linear_m_s_z"])
                    self.assertIsNone(moving["pose_frame_hex"])
                    self.assertEqual(common["observations"][1]["fields"]["vehicle_report_stamp_us"], 2123456)
                    self.assertNotEqual(common["observations"][1]["t_ms"], 2123)
                    units = common["profile_units"]
                    self.assertIn("left_positive", units["vehicle_steering_tire_angle_rad"])
                    self.assertIn("not_a_steering_wheel_angle", units["vehicle_steering_tire_angle_rad"])
                    self.assertIn("not_a_commanded_target", units["vehicle_steering_tire_angle_rad"])
                    self.assertIn("forward_positive", units["vehicle_velocity_longitudinal_m_s"])
                    self.assertIn("left_positive", units["vehicle_velocity_lateral_m_s"])
                    self.assertIn("counterclockwise_positive", units["vehicle_heading_rate_rad_s"])
                    self.assertIn("never_read_as_a_world_or_map_frame", units["vehicle_report_frame_hex"])
                    self.assertIn("not_the_capture_clock", units["vehicle_report_stamp_us"])
            for kwargs in ({"hash_override": "RIHS01_" + "0" * 64}, {"corrupt": True}):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "bag"; bag(source, messages=[velocity, angle], **kwargs)
                    with self.assertRaises((ValueError, AssertionError, struct.error)): convert(source)
                    rejected = subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                        str(source), str(Path(directory) / "rejected")], capture_output=True)
                    self.assertEqual(rejected.returncode, 2)
                    self.assertNotIn(b"Traceback", rejected.stderr)
        for name, record, key in ((VELOCITY_REPORT, plain(velocity), "heading_rate"),
                                   (STEERING_REPORT, plain(angle), "steering_tire_angle")):
            for bad in (float("nan"), float("inf"), True, "1"):
                with self.assertRaises(ValueError): vehicle_report_quantities(dict(record, **{key: bad}), name)
        record = plain(angle); record["stamp"]["nanosec"] = 1000000000
        with self.assertRaises(ValueError): vehicle_report_quantities(record, STEERING_REPORT)

    def test_qualified_joint_missing_values_reuse_normal_quantities(self):
        from scripts.convert_rosbag_observations import JOINT, joint_quantities, plain
        from rosbags.rosbag2.enums import CompressionMode
        for kind, value, compression in (("angular", .5, None), ("linear", .2, CompressionMode.FILE)):
            message = STORE.types[JOINT](samples()[0].header, ["joint"], np.array([value]),
                np.array([float("nan")]), np.array([float("nan")]))
            normal = STORE.types[JOINT](samples()[0].header, ["joint"], np.array([value * 2]),
                np.array([-.1]), np.array([2.]))
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "bag"
                bag(source, messages=[message, normal], compression=compression)
                with self.assertRaises(ValueError): convert(source, {"joint": kind})
                output, report = convert(source, {"joint": kind}, allow_missing_joint_values=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["decoded_messages"], 2)
                self.assertEqual(rows[0]["joint_velocity_status"], "UNAVAILABLE_SOURCE_NAN")
                self.assertEqual(rows[0]["joint_effort_status"], "UNAVAILABLE_SOURCE_NAN")
                self.assertEqual(rows[1]["joint_velocity_status"], "REPORTED")
                self.assertEqual(report["joint_value_policy"], "QUALIFIED_SOURCE_NAN_UNAVAILABLE_NOT_ZERO")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    suffix = "rad" if kind == "angular" else "m"
                    self.assertEqual(common["observations"][0]["fields"]["joint_position_" + suffix], value)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                    str(source), str(Path(directory) / "cli"), "--joint-kind", "joint=" + kind,
                    "--allow-missing-joint-values"], check=True, capture_output=True)
                self.assertEqual((Path(directory) / "cli/observations.csv").read_text(), output)
            for invalid in ("Infinity", "-Infinity", True, "missing"):
                record = plain(message); record["velocity"] = [invalid]
                with self.assertRaises(ValueError): joint_quantities(record, {"joint": kind}, True)
        with self.assertRaises(ValueError): convert("missing", allow_missing_joint_values=True)

    def test_nav2_feedback_reuses_pose_and_does_not_promote_zero_estimates(self):
        import copy
        from scripts.convert_rosbag_observations import HASHES, nav2_feedback_quantities, plain
        self.assertEqual(STORE.hash_rihs01(NAV2_FEEDBACK), HASHES[NAV2_FEEDBACK])
        definition = NAV2_DEFINITIONS[NAV2_FEEDBACK]
        child = "nav2_msgs/action/NavigateToPose_Feedback"
        definition += "\n" + "=" * 80 + "\nMSG: " + child + "\n" + NAV2_DEFINITIONS[child]
        for name in ("geometry_msgs/msg/PoseStamped", "builtin_interfaces/msg/Duration", "unique_identifier_msgs/msg/UUID"):
            definition += "\n" + "=" * 80 + "\nMSG: " + name + "\n" + STORE.generate_msgdef(name, ros_version=2)[0]
        types = STORE.types
        duration = types["builtin_interfaces/msg/Duration"]
        pose = types["geometry_msgs/msg/PoseStamped"](samples()[0].header,
            types["geometry_msgs/msg/Pose"](types["geometry_msgs/msg/Point"](1., 2., 0.),
                                            types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.)))
        first = types[NAV2_FEEDBACK](types["unique_identifier_msgs/msg/UUID"](np.arange(16, dtype=np.uint8)),
            types[child](pose, duration(3, 123), duration(5, 0), 2, 4.))
        second = copy.deepcopy(first)
        second.goal_id.uuid = np.arange(16, 32, dtype=np.uint8)
        second.feedback.current_pose.pose.position.x = -2.
        second.feedback.navigation_time = duration(-1, 500000000)
        second.feedback.estimated_time_remaining = duration(0, 0)
        second.feedback.distance_remaining = 0.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bag"
            bag(source, messages=[first, second], msgdefs={NAV2_FEEDBACK: definition})
            output, report = convert(source)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["decoded_messages"], len(rows)), (2, 2))
            self.assertEqual(rows[0]["nav2_navigation_time_ns"], "3000000123")
            self.assertEqual(rows[1]["nav2_navigation_time_ns"], "-500000000")
            self.assertEqual(rows[1]["nav2_estimated_time_disposition"], "ZERO_OR_NOT_ESTIMATED")
            self.assertEqual(rows[1]["nav2_distance_disposition"], "ZERO_OR_UNAVAILABLE")
            self.assertEqual(rows[1]["action_uuid_hex"], "hex:" + bytes(range(16, 32)).hex())
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][1]["fields"]["pose_position_m_x"], -2.)
                self.assertEqual(common["observations"][1]["clock_basis"], "Unknown")
            bad_source = Path(directory) / "wrong"
            bag(bad_source, messages=[first], msgdefs={NAV2_FEEDBACK: definition}, hash_override="RIHS01_" + "0" * 64)
            with self.assertRaises((ValueError, AssertionError)): convert(bad_source)
        for key, bad in (("number_of_recoveries", -1), ("distance_remaining", "NaN"),
                         ("estimated_time_remaining", {"sec": -1, "nanosec": 0})):
            record = plain(first); record["feedback"][key] = bad
            with self.assertRaises(ValueError): nav2_feedback_quantities(record)
        record = plain(first); record["goal_id"]["uuid"] = [1]
        with self.assertRaises(ValueError): nav2_feedback_quantities(record)
        record = plain(first); record["feedback"]["current_pose"]["header"]["frame_id"] = ""
        self.assertEqual(nav2_feedback_quantities(record)["nav2_pose_disposition"], "MISSING_FRAME_NOT_NORMALIZED")

    def test_bag_battery_temperature_reaches_common_output_with_its_reason(self):
        import copy
        reported = samples()[0]
        reported.temperature = 21.5
        unmeasured = copy.deepcopy(reported)
        unmeasured.temperature = float("nan")  # the schema's own "unmeasured" marker
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bag"
            bag(source, messages=[reported, unmeasured])
            output, report = convert(source)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["decoded_messages"], 2)
            self.assertAlmostEqual(float(rows[0]["battery_temperature_k"]), 294.65)
            self.assertEqual(rows[0]["battery_temperature_basis"], "REPORTED")
            self.assertEqual(rows[1]["battery_temperature_k"], "")
            self.assertEqual(rows[1]["battery_temperature_basis"], "UNAVAILABLE_SOURCE_NAN")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                first, second = (o["fields"] for o in common["observations"])
                self.assertAlmostEqual(first["battery_temperature_k"], 294.65)
                self.assertEqual(first["battery_temperature_basis"], "REPORTED")
                self.assertIsNone(second["battery_temperature_k"])
                self.assertEqual(second["battery_temperature_basis"], "UNAVAILABLE_SOURCE_NAN")
                units = common["profile_units"]
                self.assertTrue(units["battery_temperature_k"].startswith("K_converted_from"))
                self.assertIn("UNAVAILABLE_SOURCE_NAN", units["battery_temperature_basis"])

    def test_qualified_battery_reset_does_not_discard_following_values(self):
        import copy
        from rosbags.rosbag2.enums import CompressionMode
        for voltage, compression in ((24., None), (48., CompressionMode.FILE)):
            normal = samples()[0]
            normal.voltage = voltage
            reset = copy.deepcopy(normal)
            for field in ("voltage", "current", "charge", "percentage"):
                setattr(reset, field, float("nan"))
            reset.power_supply_status = 0
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "bag"
                bag(source, messages=[reset, normal], compression=compression)
                with self.assertRaises(ValueError): convert(source)
                output, report = convert(source, allow_missing_voltage=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["decoded_messages"], 2)
                self.assertEqual(report["battery_voltage_policy"], "QUALIFIED_SOURCE_NAN_UNAVAILABLE_NOT_ZERO")
                self.assertEqual(rows[0]["battery_voltage_disposition"], "UNAVAILABLE_SOURCE_NAN")
                self.assertEqual(rows[0]["battery_power_w"], "")
                self.assertEqual(float(rows[1]["battery_voltage_v"]), voltage)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_disposition"], "UNAVAILABLE_SOURCE_NAN")
                    self.assertEqual(common["observations"][1]["fields"]["battery_voltage_v"], voltage)
                command = [os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                           str(source), str(Path(directory) / "cli"), "--allow-missing-voltage"]
                subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
                self.assertEqual((Path(directory) / "cli/observations.csv").read_text(), output)

    def test_exact_topics_reuse_common_output_despite_large_unselected_records(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            for index, (plugin, voltage) in enumerate(((StoragePlugin.SQLITE3, 12.),
                    (StoragePlugin.SQLITE3, 27.), (StoragePlugin.MCAP, 12.), (StoragePlugin.MCAP, 27.))):
                path = Path(directory) / f"topics{index}"
                with Writer(path, version=9, storage_plugin=plugin) as writer:
                    connection = writer.add_connection("/battery", BATTERY, typestore=STORE)
                    battery = samples()[0]; battery.voltage = voltage
                    writer.write(connection, 7000123456, STORE.serialize_cdr(battery, BATTERY))
                    kind = "std_msgs/msg/String"
                    excluded = writer.add_connection("/large", kind, typestore=STORE)
                    writer.write(excluded, 7000123456, STORE.serialize_cdr(STORE.types[kind]("x" * 20000), kind))
                with self.assertRaisesRegex(ValueError, "record timestamp or size"):
                    convert(path)
                output, report = convert(path, topics=["/battery"])
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), 1)
                self.assertEqual(float(rows[0]["battery_voltage_v"]), voltage)
                self.assertEqual(report["topic_selection"]["declared_selected_messages"], 1)
                self.assertEqual(report["topic_selection"]["declared_excluded_messages"], 1)
                self.assertEqual(report["topic_selection"]["outside_selection"],
                    "NOT_DECODED_OR_VALIDATED" if plugin == StoragePlugin.MCAP else "NOT_READ_OR_VALIDATED")
                self.assertEqual(report["unsupported_records"], [])
                window, window_report = convert(path, topics=["/battery"], start_ns=7000123456, stop_ns=7000123457)
                self.assertEqual(output, window)
                self.assertEqual(window_report["capture_window"]["selected_messages"], 1)
                if plugin == StoragePlugin.MCAP:
                    self.assertEqual(window_report["capture_window"]["mcap"]["index_declared_selected_messages"], 1)
                    self.assertEqual(window_report["topic_selection"]["declared_excluded_messages"], 1)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    target = Path(directory) / "selected.csv"; target.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], voltage)
                for topics in ([], ["/absent"], ["/battery", "/battery"], [7], "/battery"):
                    with self.assertRaises(ValueError): convert(path, topics=topics)
                with self.assertRaisesRegex(ValueError, "record timestamp or size"):
                    convert(path, topics=["/large"])
            destination = Path(directory) / "cli-topics"
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(path), str(destination), "--topic", "/battery"], check=True, capture_output=True)
            self.assertEqual(json.loads((destination / "report.json").read_text())["topic_selection"]["read_messages"], 1)
            corrupt_path = Path(directory) / "corrupt-selected"
            bag(corrupt_path, messages=[samples()[0]], corrupt=True)
            with self.assertRaises(Exception): convert(corrupt_path, topics=["/example/0"])
            wrong_schema = Path(directory) / "wrong-schema"
            bag(wrong_schema, hash_override="RIHS01_" + "0" * 64)
            with self.assertRaises((ValueError, AssertionError)):
                convert(wrong_schema, topics=["/example/0"])

    def test_large_sqlite_bag_explicit_adjacent_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"
            with Writer(path, version=9, storage_plugin=StoragePlugin.SQLITE3) as writer:
                connection = writer.add_connection("/battery", BATTERY, typestore=STORE)
                battery = samples()[0]
                for timestamp, voltage in ((1000000, 12.), (2000000, 24.)):
                    battery.voltage = voltage
                    writer.write(connection, timestamp, STORE.serialize_cdr(battery, BATTERY))
                kind = "std_msgs/msg/String"
                other = writer.add_connection("/outside", kind, typestore=STORE)
                writer.write(other, 0, STORE.serialize_cdr(STORE.types[kind]("x" * (17 * 1024 * 1024)), kind))
            self.assertGreater(sum(p.stat().st_size for p in path.iterdir()), 16 * 1024 * 1024)
            with self.assertRaisesRegex(ValueError, "bag too large"):
                convert(path)
            for start, expected in ((1000000, 12.), (2000000, 24.)):
                output, report = convert(path, start_ns=start, stop_ns=start + 1000000)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), 1)
                self.assertEqual(float(rows[0]["battery_voltage_v"]), expected)
                source = json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:]))
                self.assertEqual(source["capture_ns"], str(start))
                self.assertEqual(source["decoded"]["voltage"], expected)
                self.assertTrue(source["raw_cdr_hex"])
                self.assertEqual(report["capture_window"]["declared_bag_messages"], 3)
                self.assertEqual(report["capture_window"]["selected_messages"], 1)
                self.assertEqual(report["capture_window"]["outside_window"], "NOT_DECODED_OR_VALIDATED")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    target = Path(directory) / "out.csv"; target.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], expected)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for start, stop in ((None, 1), (1, None), (1, 1), (-1, 2), (2, 1), (True, 2), (0, 2**63)):
                with self.assertRaises(ValueError): convert(path, start_ns=start, stop_ns=stop)
            with self.assertRaisesRegex(ValueError, "no supported messages"):
                convert(path, start_ns=3000000, stop_ns=4000000)
            with self.assertRaisesRegex(ValueError, "record timestamp or size"):
                convert(path, start_ns=0, stop_ns=1)
            import sys
            destination = Path(directory) / "cli"
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(path), str(destination), "--start-ns", "1000000", "--stop-ns", "2000000"],
                check=True, capture_output=True)
            self.assertEqual(json.loads((destination / "report.json").read_text())["capture_window"]["selected_messages"], 1)
            rejected = subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(path), str(Path(directory) / "bad"), "--metadata-only", "--start-ns", "0", "--stop-ns", "1"],
                capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((Path(directory) / "bad").exists())
            mcap = Path(directory) / "mcap"
            bag(mcap, plugin=StoragePlugin.MCAP)
            output, report = convert(mcap, start_ns=0, stop_ns=8000000000)
            self.assertEqual(output, convert(mcap)[0])
            self.assertEqual(report["capture_window"]["selected_messages"], 3)
            self.assertEqual(report["capture_window"]["mcap"]["index_declared_selected_messages"], 3)
            self.assertIsNone(report["capture_window"]["mcap"]["storage_request_start_ns"])
            with self.assertRaisesRegex(ValueError, "no supported messages"):
                convert(mcap, start_ns=0, stop_ns=7000123456)
            with self.assertRaisesRegex(ValueError, "sqlite3 or indexed MCAP"):
                convert(next(mcap.glob("*.mcap")), start_ns=0, stop_ns=8000000000)

    def test_compact_lists_reuse_binding_for_actions_and_parameters(self):
        import hashlib
        from scripts.convert_rosbag_observations import ACTION_STATUS
        types = STORE.types
        for selection, count in (("actions", 1024), ("parameters", 4096)):
            if selection == "actions":
                entries = [types["action_msgs/msg/GoalStatus"](
                    types["action_msgs/msg/GoalInfo"](
                        types["unique_identifier_msgs/msg/UUID"](np.frombuffer(index.to_bytes(16, "little"), dtype=np.uint8)),
                        types["builtin_interfaces/msg/Time"](2, 123456789)), 2 if index < count - 1 else 42)
                    for index in range(count)]
                message = types[ACTION_STATUS](entries)
            else:
                message = parameter_event_sample(-19)
                value = message.new_parameters[1].value  # INTEGER, preserved signed value.
                message.new_parameters = [types["rcl_interfaces/msg/Parameter"](f"p{index}", value)
                                          for index in range(count)]
                message.changed_parameters = []
                message.deleted_parameters = []
            raw = bytes(STORE.serialize_cdr(message, message.__msgtype__))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"
                bag(path, messages=[message])
                with self.assertRaisesRegex(ValueError, "record timestamp or size"):
                    convert(path)
                output, report = convert(path, list_compact=selection)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((report["source_messages"], report["decoded_messages"], len(rows)), (2, 1, count))
                source = report["list_sources"][0]
                self.assertEqual(bytes.fromhex(source["raw_cdr_hex"]), raw)
                self.assertEqual(len(report["unsupported_records"]), 1)
                for row in rows:
                    self.assertEqual(row["source_cdr_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.assertEqual(row["source_message_index"], "0")
                if selection == "actions":
                    self.assertEqual(rows[-1]["action_status_reported"], "UNKNOWN_42")
                    self.assertEqual(rows[-1]["action_uuid_hex"], "hex:" + (count - 1).to_bytes(16, "little").hex())
                else:
                    self.assertEqual(rows[-1]["parameter_integer_reported"], "-19")
                    self.assertEqual(rows[-1]["parameter_name_hex"], "hex:" + f"p{count - 1}".encode().hex())
                    self.assertEqual(rows[-1]["parameter_event_entry_count"], str(count))
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(len(common["observations"]), count)
                    self.assertEqual(common["observations"][-1]["clock_basis"], "Unknown")
                    field = "action_status_code" if selection == "actions" else "parameter_integer_reported"
                    self.assertEqual(common["observations"][-1]["fields"][field], 42 if selection == "actions" else -19)
                for extra in ({"scan_compact": True}, {"joint_kinds": {"a": "linear"}},
                              {"pointcloud_unit": "m"}):
                    with self.assertRaises(ValueError):
                        convert(path, list_compact=selection, **extra)

    def test_parameter_events_two_storages_reuse_common_typed_output(self):
        for plugin, integer in ((StoragePlugin.SQLITE3, -7), (StoragePlugin.MCAP, 8)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); source = root / "bag"
                bag(source, plugin=plugin, messages=[parameter_event_sample(integer)])
                output, report = convert(source)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), 15)
                self.assertEqual(rows[0]["parameter_bool_reported"], "0")
                self.assertEqual(rows[1]["parameter_integer_reported"], str(integer))
                self.assertEqual([r["parameter_integer_reported"] for r in rows if r["parameter_type_code"] == "5"], ["0", "255"])
                self.assertEqual(rows[-1]["parameter_event_kind"], "DELETED_REPORTED")
                self.assertEqual(rows[-1]["parameter_value_status"], "NOT_SET")
                self.assertEqual(rows[0]["parameter_source_stamp_us"], "3000012")
                self.assertNotEqual(rows[0]["record_time_us"], "3000012")
                self.assertTrue(any(r["parameter_value_status"] == "NONFINITE_RETAINED" for r in rows))
                self.assertEqual(rows[0]["parameter_event_entry_count"], "10")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    path = root / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 15)
                    self.assertEqual(common["observations"][1]["fields"]["parameter_integer_reported"], integer)

    def test_parameter_event_unknown_empty_and_duplicate_name_boundary(self):
        from scripts.convert_rosbag_observations import parameter_event_quantities, plain
        record = plain(parameter_event_sample())
        record["new_parameters"][0]["value"]["type"] = 255
        rows = parameter_event_quantities(record)
        self.assertEqual(rows[0]["parameter_value_status"], "UNKNOWN_TYPE_RETAINED")
        record["changed_parameters"][0]["value"]["byte_array_value"] = []
        self.assertTrue(any(r["parameter_value_status"] == "EMPTY_ARRAY" for r in parameter_event_quantities(record)))
        record["deleted_parameters"][0]["name"] = record["new_parameters"][0]["name"]
        with self.assertRaises(ValueError):
            parameter_event_quantities(record)
        record = dict(node="/node", stamp=dict(sec=0, nanosec=0), new_parameters=[], changed_parameters=[], deleted_parameters=[])
        self.assertEqual(parameter_event_quantities(record)[0]["parameter_event_kind"], "EMPTY_EVENT")
        record["stamp"]["nanosec"] = 1000000000
        with self.assertRaises(ValueError):
            parameter_event_quantities(record)

    def test_navsat_fixed_meanings_missing_and_unknown_fields(self):
        from scripts.convert_rosbag_observations import NAVSAT, navsat_quantities
        status_type = STORE.types["sensor_msgs/msg/NavSatStatus"]
        for plugin, coordinates in ((StoragePlugin.SQLITE3, (35., 139., 12.5)),
                                    (StoragePlugin.MCAP, (-33., -70., -10.))):
            message = STORE.types[NAVSAT](samples()[0].header, status_type(2, 0x89),
                *coordinates, np.array([1., .1, .2, .1, 2., .3, .2, .3, 3.]), 3)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"; bag(path, plugin=plugin, messages=[message])
                output, report = convert(path)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual((report["decoded_messages"], len(report["unsupported_records"])), (1, 1))
                self.assertEqual([float(row[field]) for field in ("navsat_latitude_deg",
                    "navsat_longitude_deg", "navsat_altitude_wgs84_ellipsoid_m")], list(coordinates))
                self.assertEqual(row["navsat_status_reported"], "GBAS_FIX")
                self.assertEqual(row["navsat_service_unknown_bits"], "128")
                self.assertEqual(row["navsat_service_gps"], "true")
                self.assertEqual(row["navsat_service_glonass"], "false")
                self.assertEqual(float(row["navsat_covariance_4_m2"]), 2.)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(csv_path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    observation = common["observations"][0]
                    self.assertEqual(observation["fields"]["navsat_altitude_wgs84_ellipsoid_m"], coordinates[2])
                    self.assertIsNone(observation["anchor_unix_us"])
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertNotIn("altitude_msl_m", observation["fields"])
            record = decode(STORE.serialize_cdr(message, NAVSAT), NAVSAT)
            record["altitude"] = "NaN"; record["position_covariance_type"] = 2
            record["position_covariance"][1] = "NaN"
            selected = navsat_quantities(record)
            self.assertEqual(selected["navsat_altitude_status"], "UNAVAILABLE")
            self.assertNotIn("navsat_covariance_1_m2", selected)
            record["position_covariance_type"] = 255
            self.assertEqual(navsat_quantities(record)["navsat_covariance_status"], "UNKNOWN_TYPE")
            record["status"]["status"] = 3
            self.assertEqual(navsat_quantities(record)["navsat_status_reported"], "UNKNOWN")
            self.assertEqual(navsat_quantities(record)["navsat_position_status"], "FIX_REPORTED")
            bad_covariance = dict(record, position_covariance_type=3, position_covariance=[-1.] + [0.] * 8)
            with self.assertRaises(ValueError):
                navsat_quantities(bad_covariance)
            for field, value in (("latitude", 91.), ("longitude", -181.), ("altitude", "Infinity")):
                with self.assertRaises(ValueError):
                    navsat_quantities(dict(record, **{field: value}))
            record["status"]["status"] = -1; record["latitude"] = "NaN"
            selected = navsat_quantities(record)
            self.assertNotIn("navsat_latitude_deg", selected)
            self.assertEqual(selected["navsat_position_status"], "NO_VALID_FIX_REPORTED")

    def test_compressed_framing_and_unknown_size_expansion_fail_closed(self):
        import zstandard
        from scripts.convert_rosbag_observations import check_single_zstd_frame, convert_file_compressed, LIMIT
        for checksum in (False, True):
            data = zstandard.ZstdCompressor(write_checksum=checksum).compress(b"example" * 20)
            check_single_zstd_frame(data, checksum)
            for bad in (data[:-1], data + data):
                with self.assertRaises(ValueError):
                    check_single_zstd_frame(bad, checksum)
            bad = bytearray(data); bad[zstandard.frame_header_size(data)] |= 6
            with self.assertRaises(ValueError):
                check_single_zstd_frame(bytes(bad), checksum)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            data = zstandard.ZstdCompressor(write_content_size=False).compress(bytes(LIMIT + 1))
            self.assertEqual(zstandard.get_frame_parameters(data).content_size, zstandard.CONTENTSIZE_UNKNOWN)
            self.assertLessEqual(zstandard.get_frame_parameters(data).window_size, LIMIT)
            (path / "oversized.db3.zstd").write_bytes(data)
            with self.assertRaisesRegex(ValueError, "expanded bag"):
                convert_file_compressed(path, {"rosbag2_bagfile_information": {"compression_format": "zstd"}},
                                        ["oversized.db3.zstd"], {}, None, None)

    def test_file_compression_reuses_observations_with_expansion_bound(self):
        from rosbags.rosbag2.enums import CompressionMode
        from unittest.mock import patch
        for voltage in (12.0, 24.0):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "compressed"
                battery, imu = samples(); battery.voltage = voltage
                bag(path, messages=[battery, imu], compression=CompressionMode.FILE)
                output, report = convert(path)
                self.assertEqual((report["source_messages"], report["decoded_messages"]), (3, 2))
                self.assertEqual(report["source_file_compression"], "zstd")
                self.assertEqual(bytes.fromhex(report["source_metadata_yaml_hex"][4:]), (path / "metadata.yaml").read_bytes())
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], voltage)
                budget = sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) + 1
                self.assertGreater(report["expanded_storage_bytes"], budget)
                with patch("scripts.convert_rosbag_observations.LIMIT", budget), self.assertRaisesRegex(ValueError, "expanded bag"):
                    convert(path)
                compressed = next(path.glob("*.zstd"))
                original = compressed.read_bytes()
                for label, corrupt in (("header", original[:8]), ("tail", original[:-1]), ("extra", original + original)):
                    compressed.write_bytes(corrupt)
                    with self.subTest(corruption=label), self.assertRaises(ValueError):
                        convert(path)

    def test_compact_cloud_practical_count_and_original_binding(self):
        import hashlib
        import struct
        from scripts.convert_rosbag_observations import POINT_CLOUD, cloud_quantities, plain
        types = STORE.types
        for count, big, datatype, size in ((100000, False, 7, 4), (1024, True, 8, 8)):
            fields = [types["sensor_msgs/msg/PointField"](name, index * size, datatype, 1)
                      for index, name in enumerate(("x", "y", "z", "future_intensity"))]
            point = struct.pack((">" if big else "<") + ("ffff" if size == 4 else "dddd"), 1., -2., 3., 9.)
            width = count // 2
            data = (point * width + b"PADDING!") * 2
            message = types[POINT_CLOUD](samples()[0].header, 2, width, fields, big,
                len(point), len(point) * width + 8, np.frombuffer(data, dtype=np.uint8), True)
            original = bytes(STORE.serialize_cdr(message, POINT_CLOUD))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"; bag(path, messages=[message])
                with self.assertRaises(ValueError):
                    convert(path, pointcloud_unit="m")  # Legacy bound remains explicit.
                output, report = convert(path, pointcloud_unit="m", pointcloud_compact=True)
                self.assertEqual((report["source_messages"], report["decoded_messages"], report["output_records"]), (2, 1, count))
                stored = report["pointcloud_sources"][0]
                self.assertEqual(bytes.fromhex(stored["raw_cdr_hex"]), original)
                self.assertEqual(stored["source_cdr_sha256"], hashlib.sha256(original).hexdigest())
                self.assertNotIn("data", stored["decoded"])
                self.assertEqual(report["unsupported_records"][0]["reason"], "NOT_SELECTED_POINTCLOUD")
                rows = csv.DictReader(io.StringIO(output))
                for index, row in enumerate(rows):
                    self.assertEqual(int(row["point_index"]), index)
                    self.assertEqual(int(row["point_row"]), index // width)
                    self.assertEqual(bytes.fromhex(row["point_bytes_hex"][4:]), point)
                    self.assertEqual(row["source_cdr_sha256"], "sha256:" + stored["source_cdr_sha256"])
                    self.assertEqual(float(row["point_y_m"]), -2.)
                self.assertEqual(index + 1, count)
                from unittest.mock import patch
                budget = sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) + 1
                if count == 100000:
                    with patch("scripts.convert_rosbag_observations.LIMIT", budget), self.assertRaisesRegex(ValueError, "source.*bound"):
                        convert(path, pointcloud_unit="m", pointcloud_compact=True)
                with self.assertRaises(ValueError):
                    convert(path, pointcloud_compact=True)
                with self.assertRaises(ValueError):
                    convert(path, joint_kinds={"joint": "angular"}, pointcloud_unit="m", pointcloud_compact=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    source = Path(directory) / "points.csv"; source.write_text(output)
                    with (Path(directory) / "common.json").open("wb") as destination:
                        subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(source), "--allow-equal-time"], stdout=destination, check=True)
                    with (Path(directory) / "common.json").open() as common_file:
                        common = json.load(common_file)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(len(common["observations"]), count)
                    self.assertEqual(common["observations"][-1]["fields"]["point_index"], count - 1)
                    self.assertEqual(common["observations"][-1]["clock_basis"], "Unknown")
                    del common
            invalid = plain(message); invalid["width"] = 100001; invalid["height"] = 1
            with self.assertRaises(ValueError): cloud_quantities(invalid, "m", True)
            invalid = plain(message); invalid["row_step"] -= 9
            with self.assertRaises(ValueError): cloud_quantities(invalid, "m", True)

    def test_pointcloud_layouts_reuse_common_observations(self):
        import copy
        import struct
        from scripts.convert_rosbag_observations import POINT_CLOUD, cloud_quantities, plain
        types = STORE.types
        for big, datatype, size in ((False, 7, 4), (True, 8, 8)):
            point_fields = [types["sensor_msgs/msg/PointField"](name, index * size, datatype, 1)
                            for index, name in enumerate(("x", "y", "z", "intensity"))]
            fmt = (">" if big else "<") + ("ffff" if size == 4 else "dddd")
            raw = b"".join(struct.pack(fmt, x, 2., 3., 9.) + bytes(8) for x in (1., 5.))
            message = types[POINT_CLOUD](samples()[0].header, 2, 1, point_fields, big,
                                        size * 4, size * 4 + 8, np.frombuffer(raw, dtype=np.uint8), True)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"; bag(path, messages=[message])
                with self.assertRaises(ValueError): convert(path)
                output, report = convert(path, pointcloud_unit="m")
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["output_records"], 2)
                self.assertEqual([float(row["point_x_m"]) for row in rows], [1., 5.])
                self.assertEqual([int(row["point_row"]) for row in rows], [0, 1])
                self.assertEqual(json.loads(bytes.fromhex(rows[0]["point_unmapped_fields_hex"][4:])), ["intensity"])
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    source = Path(directory) / "out.csv"; source.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][1]["fields"]["point_x_m"], 5.)
            value = plain(message)
            for key, bad in (("row_step", 1), ("point_step", 1), ("is_bigendian", 1)):
                modified = copy.deepcopy(value); modified[key] = bad
                with self.assertRaises(ValueError): cloud_quantities(modified, "m")
            modified = copy.deepcopy(value)
            modified["data"][:size] = list(struct.pack((">" if big else "<") + ("f" if size == 4 else "d"), float("nan")))
            with self.assertRaises(ValueError): cloud_quantities(modified, "m")
            modified["is_dense"] = False
            result = cloud_quantities(modified, "m")
            self.assertIsNone(result[0]["point_x_m"])
            self.assertEqual(result[0]["point_status"], "NONFINITE_COORDINATE_REPORTED")

    def test_metadata_declarations_reuse_two_storage_outputs(self):
        from scripts.convert_rosbag_observations import convert_metadata
        for plugin in (StoragePlugin.SQLITE3, StoragePlugin.MCAP):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"
                bag(path, plugin=plugin)
                metadata = path / "metadata.yaml"
                output, report = convert_metadata(metadata)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["output_records"], 4)
                self.assertEqual(sum(int(row["declared_topic_messages"]) for row in rows if row["metadata_entry_kind"] == "TOPIC"), 3)
                self.assertEqual(rows[-1]["metadata_entry_kind"], "FILE")
                self.assertEqual(rows[-1]["declared_file_messages"], "3")
                self.assertEqual(rows[-1]["declared_file_start_ns"], "7000123456")
                self.assertEqual(rows[-1]["declared_file_duration_ns"], "0")
                self.assertTrue(bytes.fromhex(rows[-1]["declared_file_path_hex"][4:]).decode().endswith((".db3", ".mcap")))
                self.assertTrue(bytes.fromhex(rows[0]["declared_type_hash_hex"][4:]).decode().startswith("RIHS01_"))
                self.assertEqual(rows[0]["declared_compression_mode"], "NONE")
                self.assertEqual(rows[0]["declared_total_messages"], "3")
                self.assertEqual(rows[0]["declared_start_ns"], "7000123456")
                self.assertEqual(rows[0]["record_time_us"], "7000123")
                self.assertEqual(float(rows[0]["declared_duration_s"]), int(rows[0]["declared_duration_ns"]) / 1e9)
                self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]), metadata.read_bytes())
                self.assertEqual(rows[0]["metadata_disposition"], "DECLARED_ONLY_CONTENTS_NOT_VERIFIED")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    source = Path(directory) / "out.csv"; source.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 4)
                    self.assertEqual(common["observations"][0]["fields"]["declared_start_ns"], 7000123456)
                    self.assertEqual(common["observations"][-1]["fields"]["declared_file_messages"], 3)

    def test_metadata_invalid_counts_types_and_aliases_reject(self):
        from scripts.convert_rosbag_observations import convert_metadata
        from ruamel.yaml import YAML
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"; bag(path)
            original = (path / "metadata.yaml").read_text()
            target = Path(directory) / "standalone.yaml"
            for field, value in (("message_count", 4), ("version", 10), ("version", True),
                                 ("duration", {"nanoseconds": -1}),
                                 ("relative_file_paths", ["different.db3"])):
                data = YAML(typ="safe").load(original)
                data["rosbag2_bagfile_information"][field] = value
                with target.open("w") as stream: YAML().dump(data, stream)
                with self.assertRaises(ValueError): convert_metadata(target)
            target.write_text("x: &v [1]\ny: *v\n" + original)
            with self.assertRaises(ValueError): convert_metadata(target)
            target.write_text("unknown_metadata: retained\n" + original)
            output, _ = convert_metadata(target)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertIn(b"unknown_metadata: retained", bytes.fromhex(row["source_record_hex"][4:]))

    def test_metadata_file_descriptors_are_declarations_not_followed_paths(self):
        from scripts.convert_rosbag_observations import convert_metadata
        from ruamel.yaml import YAML
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"; bag(path)
            original = (path / "metadata.yaml").read_text()
            target = Path(directory) / "standalone.yaml"
            data = YAML(typ="safe").load(original)
            info = data["rosbag2_bagfile_information"]
            info["relative_file_paths"] = ["../not-opened.db3"]
            info["files"][0]["path"] = "../not-opened.db3"
            info["compression_mode"] = "future-mode"
            info["compression_format"] = "future-format"
            with target.open("w") as stream: YAML().dump(data, stream)
            output, _ = convert_metadata(target)
            row = list(csv.DictReader(io.StringIO(output)))[-1]
            self.assertEqual(row["declared_compression_mode"], "UNKNOWN")
            self.assertEqual(bytes.fromhex(row["declared_file_path_hex"][4:]).decode(), "../not-opened.db3")
            self.assertFalse((Path(directory) / "not-opened.db3").exists())
            for key, value in (("duration", {"nanoseconds": -1}),
                               ("starting_time", {"nanoseconds_since_epoch": True})):
                bad = YAML(typ="safe").load(original)
                bad["rosbag2_bagfile_information"]["files"][0][key] = value
                with target.open("w") as stream: YAML().dump(bad, stream)
                with self.assertRaises(ValueError): convert_metadata(target)
            data["rosbag2_bagfile_information"]["compression_mode"] = True
            with target.open("w") as stream: YAML().dump(data, stream)
            with self.assertRaises(ValueError): convert_metadata(target)

    def test_stamped_motion_shares_quantities_without_inventing_other_half(self):
        from scripts.convert_rosbag_observations import STAMPED_MOTION, stamped_motion_quantities, plain
        types, header = STORE.types, samples()[0].header
        for x, role in ((1., "reported_estimate"), (5., "reported_reference")):
            pose = types["geometry_msgs/msg/Pose"](types["geometry_msgs/msg/Point"](x, 2., 3.),
                types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.))
            vector = types["geometry_msgs/msg/Vector3"]
            twist = types["geometry_msgs/msg/Twist"](vector(x, 0., 0.), vector(0., .5, 0.))
            messages = []
            for typename in STAMPED_MOTION:
                value = pose if "Pose" in typename else twist
                if "WithCovariance" in typename:
                    value = types[typename.replace("Stamped", "")](value, np.eye(6).flatten())
                messages.append(types[typename](header, value))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"; bag(path, messages=messages)
                with self.assertRaises(ValueError):
                    convert(path)
                output, report = convert(path, stamped_motion_role=role)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["output_records"], 4)
                for index, row in enumerate(rows):
                    self.assertEqual(row["motion_source_role_configured"], role)
                    self.assertEqual(row["motion_source_stamp_us"], "2123456")
                    group = "pose" if index < 2 else "twist"
                    other = "twist_linear_m_s_x" if index < 2 else "pose_position_m_x"
                    key = "pose_position_m_x" if index < 2 else "twist_linear_m_s_x"
                    self.assertEqual(float(row[key]), x)
                    self.assertEqual(row[other], "")
                    self.assertEqual(row[group + "_covariance_status"], "NOT_PROVIDED" if index % 2 == 0 else "REPORTED")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    source = Path(directory) / "out.csv"; source.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 4)
                    self.assertIsNone(common["observations"][0]["fields"]["twist_linear_m_s_x"])
                    self.assertEqual(common["observations"][2]["fields"]["twist_linear_m_s_x"], x)
            for bad in ("frame", "quaternion"):
                value = plain(messages[0])
                if bad == "frame": value["header"]["frame_id"] = ""
                else: value["pose"]["orientation"]["w"] = 2
                with self.assertRaises(ValueError):
                    stamped_motion_quantities(value, messages[0].__msgtype__, role)

    def test_action_status_reuses_bag_decoder_and_common_output(self):
        from scripts.convert_rosbag_observations import ACTION_STATUS, action_status_quantities, plain
        types = STORE.types
        for codes in ((1, 2, 4), (3, 5, 6, 0, 42)):
            entries = [types["action_msgs/msg/GoalStatus"](
                types["action_msgs/msg/GoalInfo"](
                    types["unique_identifier_msgs/msg/UUID"](np.array([index] * 16, dtype=np.uint8)),
                    types["builtin_interfaces/msg/Time"](2, 123456789)), code)
                for index, code in enumerate(codes)]
            message = types[ACTION_STATUS](entries)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"
                bag(path, messages=[message, types[ACTION_STATUS]([])])
                output, report = convert(path)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["decoded_messages"], 2)
                self.assertEqual(report["output_records"], len(codes) + 1)
                self.assertEqual(rows[-1]["action_entry_count"], "0")
                self.assertEqual(rows[0]["action_accepted_stamp_us"], "2123456")
                self.assertEqual([int(row["action_status_code"]) for row in rows[:-1]], list(codes))
                self.assertEqual([row["action_status_reported"] for row in rows[:-1]],
                    ["ACCEPTED_REPORTED", "EXECUTING_REPORTED", "SUCCEEDED_REPORTED"] if codes[0] == 1 else
                    ["CANCELING_REPORTED", "CANCELED_REPORTED", "ABORTED_REPORTED", "UNKNOWN_REPORTED", "UNKNOWN_42"])
                self.assertEqual(rows[1]["action_uuid_hex"], "hex:" + "01" * 16)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    source = Path(directory) / "out.csv"
                    source.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], len(codes) + 1)
                    self.assertEqual(result["observations"][0]["fields"]["action_status_code"], codes[0])
            for bad in ("uuid", "stamp", "code"):
                value = plain(message)
                if bad == "uuid": value["status_list"][0]["goal_info"]["goal_id"]["uuid"] = [1]
                if bad == "stamp": value["status_list"][0]["goal_info"]["stamp"]["nanosec"] = 10**9
                if bad == "code": value["status_list"][0]["status"] = True
                with self.assertRaises(ValueError):
                    action_status_quantities(value)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader; the common-output proof is the point "
                         "of this test, so a missing reader is a skip and not a pass")
    def test_packaged_jazzy_scan_and_cloud_reach_common_through_the_adopted_path(self):
        """The one previously unexecuted configuration: bags written against the packaged
        ROS2_JAZZY definitions of the two selected types, read by the adopted path unchanged.

        Hash equality across the four packaged stores is already asserted by
        test_fixed_schema_reuse_and_range_variance_boundary; that test only ever writes a
        Range bag with a non-Humble store, so no packaged-Jazzy scan or cloud had reached the
        common output. No converter change accompanies this test: the packaged Jazzy and Humble
        entries for LaserScan, PointCloud2, PointField, Header and Time are the same objects, so
        a Jazzy-qualified connection already carries the digest the converter qualifies.
        """
        import struct
        from rosbags.typesys import Stores, get_typestore
        from scripts.convert_rosbag_observations import HASHES, POINT_CLOUD
        jazzy = get_typestore(Stores.ROS2_JAZZY)
        scan_type = "sensor_msgs/msg/LaserScan"
        for typename in (scan_type, POINT_CLOUD, "sensor_msgs/msg/PointField",
                         "std_msgs/msg/Header", "builtin_interfaces/msg/Time"):
            self.assertEqual(jazzy.fielddefs[typename], STORE.fielddefs[typename], typename)
            self.assertEqual(jazzy.hash_rihs01(typename), STORE.hash_rihs01(typename), typename)
        self.assertEqual(jazzy.hash_rihs01(scan_type), HASHES[scan_type])
        self.assertEqual(jazzy.hash_rihs01(POINT_CLOUD), HASHES[POINT_CLOUD])
        types = jazzy.types
        header = types["std_msgs/msg/Header"](
            types["builtin_interfaces/msg/Time"](7, 500000), "jazzy_laser")
        scan = types[scan_type](header, -.5, .5, .25, .01, .1, .2, 10.,
                                np.array([1.5, float("inf"), 2.5], dtype=np.float32),
                                np.array([], dtype=np.float32))
        point_fields = [types["sensor_msgs/msg/PointField"](name, index * 4, 7, 1)
                        for index, name in enumerate(("x", "y", "z"))]
        raw = b"".join(struct.pack("<fff", *point) for point in ((1.25, -2.5, .75), (4., 5.5, -6.25)))
        cloud = types[POINT_CLOUD](header, 1, 2, point_fields, False, 12, 24,
                                  np.frombuffer(raw, dtype=np.uint8), True)
        profile = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"
        for plugin in StoragePlugin:
            with self.subTest(plugin=plugin), tempfile.TemporaryDirectory() as directory:
                scan_bag, cloud_bag = Path(directory) / "scan", Path(directory) / "cloud"
                bag(scan_bag, plugin, messages=[scan], typestore=jazzy)
                bag(cloud_bag, plugin, messages=[cloud], typestore=jazzy)
                scan_out, scan_report = convert(scan_bag, scan_compact=True)
                cloud_out, cloud_report = convert(cloud_bag, pointcloud_compact=True,
                                                 pointcloud_unit="m")
                scan_rows = list(csv.DictReader(io.StringIO(scan_out)))
                cloud_rows = list(csv.DictReader(io.StringIO(cloud_out)))
                self.assertEqual((scan_report["decoded_messages"], len(scan_rows)), (1, 3))
                self.assertEqual([r["range_status"] for r in scan_rows],
                                 ["REPORTED", "OUT_OF_RANGE_REPORTED", "REPORTED"])
                self.assertEqual([r["range_m"] for r in scan_rows], ["1.5", "", "2.5"])
                self.assertEqual([r["scan_ray_index"] for r in scan_rows], ["0", "1", "2"])
                self.assertEqual({r["scan_ray_count"] for r in scan_rows}, {"3"})
                self.assertEqual((cloud_report["decoded_messages"], len(cloud_rows)), (1, 2))
                self.assertEqual([float(r["point_x_m"]) for r in cloud_rows], [1.25, 4.])
                self.assertEqual([float(r["point_z_m"]) for r in cloud_rows], [.75, -6.25])
                self.assertEqual({r["point_unit_basis"] for r in cloud_rows},
                                 {"METRES_CONFIGURED_NOT_INFERRED_FROM_TYPE"})
                self.assertEqual(len(scan_report["unsupported_records"]), 1)
                with self.assertRaises(ValueError):
                    convert(cloud_bag, pointcloud_compact=True)
                wrong = Path(directory) / "wrong"
                bag(wrong, plugin, messages=[scan], typestore=jazzy,
                    hash_override="RIHS01_" + "0" * 64)
                expected = AssertionError if plugin is StoragePlugin.SQLITE3 else ValueError
                with self.assertRaises(expected):
                    convert(wrong, scan_compact=True)
                for name, output in (("scan", scan_out), ("cloud", cloud_out)):
                    path = Path(directory) / f"{name}.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(profile), str(path), "--allow-equal-time"],
                        check=True, capture_output=True).stdout)
                    fields = [o["fields"] for o in common["observations"]]
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    if name == "scan":
                        self.assertEqual(common["main_rows"], 3)
                        self.assertEqual([f["range_m"] for f in fields], [1.5, None, 2.5])
                        self.assertEqual([round(f["scan_angle_rad"], 4) for f in fields],
                                         [-.5, -.25, 0.])
                    else:
                        self.assertEqual(common["main_rows"], 2)
                        self.assertEqual([f["point_y_m"] for f in fields], [-2.5, 5.5])
                        self.assertEqual([f["point_unit_basis"] for f in fields],
                                         ["METRES_CONFIGURED_NOT_INFERRED_FROM_TYPE"] * 2)

    def test_fixed_schema_reuse_and_range_variance_boundary(self):
        from rosbags.typesys import Stores, get_typestore
        from scripts.convert_rosbag_observations import HASHES, OUTDOORNAV_DEFINITIONS, RANGE, RANGE_VARIANCE_HASH, sensor_quantities, VEHICLE_DEFINITIONS, GROUND_DEFINITIONS
        for typename in (*VEHICLE_DEFINITIONS, PX4_BATTERY):
            self.assertEqual(STORE.hash_rihs01(typename), HASHES[typename])
        self.assertEqual(HASHES[PX4_BATTERY], PX4_BATTERY_HASH)
        self.assertEqual(SERVICE_STORE.hash_rihs01(normalize_msgtype(GET_PARAMETERS_EVENT)),
                         HASHES[GET_PARAMETERS_EVENT])
        self.assertEqual(HASHES[GET_PARAMETERS_EVENT], GET_PARAMETERS_EVENT_HASH)
        for version in (Stores.ROS2_HUMBLE, Stores.ROS2_IRON, Stores.ROS2_JAZZY, Stores.ROS2_KILTED):
            store = get_typestore(version)
            for typename, expected in HASHES.items():
                if (typename in (NAV2_FEEDBACK, PX4_BATTERY, GET_PARAMETERS_EVENT)
                        or typename in VEHICLE_DEFINITIONS or typename in GROUND_DEFINITIONS
                        or typename in OUTDOORNAV_DEFINITIONS):
                    continue
                if typename == RANGE and version != Stores.ROS2_HUMBLE:
                    expected = RANGE_VARIANCE_HASH
                self.assertEqual(store.hash_rihs01(typename), expected)
            header = samples()[0].header
            for variance in (0., .25):
                args = (header, 0, .5, .2, 3., 1.)
                if version != Stores.ROS2_HUMBLE:
                    args += (variance,)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "bag"
                    bag(path, messages=[store.types[RANGE](*args)], typestore=store)
                    output, report = convert(path)
                    row = next(csv.DictReader(io.StringIO(output)))
                    status = "NOT_PROVIDED" if version == Stores.ROS2_HUMBLE else "UNKNOWN" if variance == 0 else "REPORTED"
                    self.assertEqual(row["range_variance_status"], status)
                    self.assertEqual(float(row["range_m"]), 1.)
                    self.assertEqual(report["decoded_messages"], 1)
                    self.assertEqual(row["range_variance_m2"], "0.25" if status == "REPORTED" else "")
                    if os.environ.get("MUSUBI_TELEMETRY_READER") and version == Stores.ROS2_KILTED and variance:
                        csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                        self.assertEqual(common["observations"][0]["fields"]["range_variance_m2"], .25)
                        self.assertEqual(common["observations"][0]["fields"]["range_variance_status"], "REPORTED")
        base = dict(header={"frame_id": "sensor"}, radiation_type=0, field_of_view=.5, min_range=.2, max_range=3., range=1.)
        for invalid in (-1., "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                sensor_quantities(dict(base, variance=invalid), RANGE)

    def test_joint_state_explicit_units_and_missing_arrays_reuse_common_reader(self):
        from scripts.convert_rosbag_observations import JOINT, joint_quantities
        header = samples()[0].header
        first = STORE.types[JOINT](header, ["pivot", "slide"], np.array([.5, .2]),
                                  np.array([-.1, .3]), np.array([2., -3.]))
        second = STORE.types[JOINT](header, ["other"], np.array([1.25]), np.array([]), np.array([]))
        empty = STORE.types[JOINT](header, [], np.array([]), np.array([]), np.array([]))
        with tempfile.TemporaryDirectory() as directory:
            for index, (messages, kinds, expected) in enumerate([
                ([first], {"pivot": "angular", "slide": "linear"}, 2),
                ([second, empty], {"other": "linear"}, 2),
            ]):
                path = Path(directory) / str(index); bag(path, messages=messages)
                output, report = convert(path, kinds)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), expected)
                self.assertEqual(report["decoded_messages"], len(messages))
                self.assertEqual(len(report["unsupported_records"]), 1)
                self.assertEqual(rows[0]["joint_source_stamp_us"], "2123456")
                if index == 0:
                    self.assertEqual(rows[0]["joint_position_rad"], "0.5")
                    self.assertEqual(rows[1]["joint_effort_n"], "-3.0")
                    self.assertEqual(rows[1]["joint_position_rad"], "")
                    with self.assertRaises(ValueError): convert(path)
                else:
                    self.assertEqual(rows[0]["joint_effort_status"], "NOT_PROVIDED")
                    self.assertEqual(rows[1]["joint_count"], "0")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    csv_path = Path(directory) / f"out{index}.csv"; csv_path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], expected)
                    self.assertEqual(common["observations"][0]["fields"]["joint_source_stamp_us"], 2123456)
                    first_joint = common["observations"][0]["fields"]
                    if index == 0:
                        self.assertEqual(first_joint["joint_name_hex"], "hex:" + "pivot".encode().hex())
                        self.assertEqual(first_joint["joint_kind_configured"], "angular")
                        self.assertEqual(first_joint["joint_position_rad"], .5)
                        self.assertEqual(first_joint["joint_velocity_rad_s"], -.1)
                        self.assertEqual(first_joint["joint_effort_nm"], 2.)
                        second_joint = common["observations"][1]["fields"]
                        self.assertEqual(second_joint["joint_kind_configured"], "linear")
                        self.assertEqual(second_joint["joint_position_m"], .2)
                        self.assertIsNone(second_joint["joint_position_rad"])
                    else:
                        self.assertEqual(first_joint["joint_effort_status"], "NOT_PROVIDED")
                        self.assertIsNone(first_joint["joint_effort_n"])
                    units = common["profile_units"]
                    self.assertIn("no_wheel_radius_or_gear_conversion", units["joint_position_rad"])
                    self.assertIn("not_a_calibrated_or_verified_torque", units["joint_effort_nm"])
                    self.assertIn("never_guessed_from_the_name", units["joint_kind_configured"])
                    self.assertIn("never_a_zero_standing_in_for_a_missing_reading",
                                  units["joint_position_status"])
                    self.assertIn("not_the_capture_clock", units["joint_source_stamp_us"])
        base = dict(header={"frame_id": "", "stamp": {"sec": 1, "nanosec": 0}},
                    name=["pivot"], position=[1.], velocity=[], effort=[])
        for patch in [dict(name=["pivot", "pivot"]), dict(name=[""]), dict(position=[1., 2.]),
                      dict(position=[float("nan")]), dict(effort=[float("inf")])]:
            with self.assertRaises(ValueError):
                joint_quantities(dict(base, **patch), {"pivot": "angular"})

    def test_laserscan_reuses_range_states_and_preserves_every_ray_and_empty_scan(self):
        from scripts.convert_rosbag_observations import scan_quantities
        typename = "sensor_msgs/msg/LaserScan"
        header = samples()[0].header
        values = np.array([1., float("inf"), float("nan"), .1], dtype=np.float32)
        scan = STORE.types[typename](header, -1., .5, .5, .01, .1, .2, 3., values,
                                     np.array([10., 11., float("nan"), 13.], dtype=np.float32))
        empty = STORE.types[typename](header, 0., 0., 0., 0., 0., .2, 3.,
                                     np.array([], dtype=np.float32), np.array([], dtype=np.float32))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"; bag(path, messages=[scan, empty])
            output, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["decoded_messages"], len(rows)), (2, 5))
            self.assertEqual([r["range_status"] for r in rows], ["REPORTED", "OUT_OF_RANGE_REPORTED",
                "INVALID_OR_MISSING", "DISCARDED_OUTSIDE_LIMITS", "EMPTY_SCAN"])
            self.assertEqual([float(r["scan_angle_rad"]) for r in rows[:4]], [-1., -.5, 0., .5])
            self.assertAlmostEqual(float(rows[3]["scan_ray_offset_s"]), .03, places=7)
            self.assertEqual(rows[0]["scan_first_stamp_us"], "2123456")
            self.assertEqual(rows[2]["scan_intensity_status"], "NONFINITE_REPORTED")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 5)
                self.assertEqual(common["observations"][3]["fields"]["scan_angle_rad"], .5)
        base = dict(header={"frame_id": "laser", "stamp": {"sec": 1, "nanosec": 0}},
                    range_min=.2, range_max=3., angle_min=0., angle_max=1., angle_increment=1.,
                    time_increment=0., scan_time=0., ranges=[1., 2.], intensities=[])
        self.assertEqual(scan_quantities(base)[0]["scan_intensity_status"], "NOT_PROVIDED")
        for patch in [dict(intensities=[1.]), dict(time_increment=-1.), dict(range_min=4.),
                      dict(angle_max="NaN"), dict(header={"frame_id": ""})]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                scan_quantities(dict(base, **patch))

    def test_scan_unavailable_return_policy_reuses_compact_common_output(self):
        typename = 'sensor_msgs/msg/LaserScan'
        for distance in (1.25, 2.5):
            scan = STORE.types[typename](samples()[0].header, -1., 1., .5, .01, .1, .05, 10.,
                np.array([distance, float('inf'), float('nan'), float('-inf'), .01], dtype=np.float32),
                np.array([], dtype=np.float32))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'bag'; bag(path, messages=[scan])
                output, report = convert(path, scan_compact=True, scan_unavailable_infinity=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([r['range_status'] for r in rows], ['REPORTED', 'UNAVAILABLE_RETURN_REPORTED',
                    'INVALID_OR_MISSING', 'TOO_CLOSE_REPORTED', 'DISCARDED_OUTSIDE_LIMITS'])
                self.assertEqual(float(rows[0]['range_m']), distance)
                self.assertTrue(all(r['range_m'] == '' for r in rows[1:]))
                self.assertEqual(report['scan_infinity_policy'], 'UNAVAILABLE_RETURN_REPORTED')
                self.assertEqual(bytes.fromhex(report['scan_sources'][0]['raw_cdr_hex']), bytes(STORE.serialize_cdr(scan, typename)))
                out = Path(directory) / 'out'
                subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_rosbag_observations.py'),
                    str(path), str(out), '--scan-compact', '--scan-unavailable-infinity'], check=True, capture_output=True)
                self.assertEqual((out / 'observations.csv').read_text(), output)
                if os.environ.get('MUSUBI_TELEMETRY_READER'):
                    common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                        str(out / 'observations.csv'), '--allow-equal-time'], check=True, capture_output=True).stdout)
                    self.assertEqual(common['main_rows'], 5)
                    self.assertEqual(common['observations'][1]['fields']['range_status'], 'UNAVAILABLE_RETURN_REPORTED')
                    self.assertEqual(common['observations'][0]['clock_basis'], 'Unknown')
                with self.assertRaises(ValueError): convert(path, scan_unavailable_infinity=True)
                with self.assertRaises(ValueError): convert(path, scan_compact=True, scan_unavailable_infinity='true')

    def test_scan_compact_reuses_source_binding_at_practical_density(self):
        import hashlib
        typename = "sensor_msgs/msg/LaserScan"
        for count, distance in ((1080, 1.25), (4096, 2.5)):
            scan = STORE.types[typename](samples()[0].header, -1., 1., 2. / (count - 1),
                .00001, .1, .2, 10., np.full(count, distance, dtype=np.float32),
                np.arange(count, dtype=np.float32))
            scan.ranges[-1] = float("inf")
            raw = bytes(STORE.serialize_cdr(scan, typename))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"
                bag(path, messages=[scan, samples()[0]])
                with self.assertRaises(ValueError):
                    convert(path)  # Existing full-source-per-ray path cannot handle this scope.
                output, report = convert(path, scan_compact=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((report["source_messages"], report["decoded_messages"], len(rows)), (3, 1, count))
                self.assertEqual(len(report["unsupported_records"]), 2)
                self.assertEqual(report["unsupported_records"][0]["reason"], "NOT_SELECTED_LASERSCAN")
                source = report["scan_sources"][0]
                self.assertEqual(bytes.fromhex(source["raw_cdr_hex"]), raw)
                self.assertNotIn("ranges", source["decoded"])
                for index, row in enumerate(rows):
                    self.assertEqual(int(row["scan_ray_index"]), index)
                    self.assertEqual(row["source_cdr_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.assertEqual(float(row["scan_intensity_device_units"]), index)
                self.assertEqual(float(rows[0]["range_m"]), distance)
                self.assertEqual(rows[-1]["range_status"], "OUT_OF_RANGE_REPORTED")
                self.assertLess(len(output), count * 2000)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(len(common["observations"]), count)
                    self.assertEqual(common["observations"][0]["fields"]["range_m"], distance)
                with self.assertRaises(ValueError):
                    convert(path, pointcloud_unit="m", scan_compact=True)
                with self.assertRaises(ValueError):
                    convert(path, {"joint": "linear"}, scan_compact=True)


    def test_range_special_values_keep_records_without_fabricating_distance(self):
        from scripts.convert_rosbag_observations import sensor_quantities
        typename = "sensor_msgs/msg/Range"
        header = samples()[0].header
        values = [.2, 3., 4., float("nan"), float("-inf"), float("inf")]
        messages = [STORE.types[typename](header, 0, .5, .2, 3., v) for v in values]
        messages += [STORE.types[typename](header, 7, .5, 2., 2., v)
                     for v in [float("-inf"), float("inf")]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"
            bag(path, messages=messages)
            output, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["decoded_messages"], len(rows)), (8, 8))
            self.assertEqual([r["range_status"] for r in rows[:6]],
                ["REPORTED", "REPORTED", "DISCARDED_OUTSIDE_LIMITS", "INVALID_OR_MISSING",
                 "TOO_CLOSE_REPORTED", "OUT_OF_RANGE_REPORTED"])
            self.assertTrue(all(r["range_m"] == "" for r in rows[2:]))
            self.assertEqual(rows[6]["range_radiation_reported"], "UNKNOWN_7")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 8)
                self.assertEqual(common["observations"][1]["fields"]["range_m"], 3.)
                units = common["profile_units"]
                self.assertTrue(units["range_m"].startswith("m_"))
                self.assertIn("never_becomes_a_usable_distance", units["range_m"])
                self.assertTrue(units["range_fov_rad"].startswith("rad_"))
                self.assertIn("not_a_measured_angle", units["range_fov_rad"])
                for state in ("REPORTED", "DISCARDED_OUTSIDE_LIMITS", "TOO_CLOSE_REPORTED",
                              "OUT_OF_RANGE_REPORTED", "INVALID_OR_MISSING"):
                    self.assertIn(state, units["range_status"])
                self.assertIn("NOT_PROVIDED", units["range_variance_status"])
        base = dict(header={"frame_id": "sensor"}, min_range=.2, max_range=3.,
                    field_of_view=.5, radiation_type=0, range=1.)
        for patch in [dict(min_range=4.), dict(field_of_view=-1.), dict(range="unexpected"),
                      dict(header={"frame_id": ""}), dict(min_range=3.), dict(radiation_type=256)]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                sensor_quantities(dict(base, **patch), typename)

    def test_transform_arrays_keep_frames_order_and_individual_source_stamps(self):
        from scripts.convert_rosbag_observations import TF, transform_quantities, plain
        types, header = STORE.types, samples()[0].header
        transform = types["geometry_msgs/msg/Transform"](types["geometry_msgs/msg/Vector3"](1., 2., 3.),
            types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.))
        entry = types["geometry_msgs/msg/TransformStamped"](header, "child", transform)
        message = types[TF]([entry, entry])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"
            bag(path, messages=[message, types[TF]([])])
            output, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["decoded_messages"], len(rows)), (2, 3))
            self.assertEqual([r["tf_entry_count"] for r in rows], ["2", "2", "0"])
            self.assertEqual(rows[0]["tf_source_stamp_us"], "2123456")
            self.assertEqual(rows[0]["record_time_us"], "7000123")
            self.assertEqual(float(rows[0]["tf_translation_m_z"]), 3.)
            self.assertEqual(bytes.fromhex(rows[0]["tf_child_frame_hex"][4:]).decode(), "child")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                csv_path = Path(directory) / "out.csv"
                csv_path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                self.assertEqual(common["observations"][0]["fields"]["tf_translation_m_z"], 3)
        for change in ("same_frame", "nonunit", "nonfinite"):
            record = plain(message)
            if change == "same_frame":
                record["transforms"][0]["child_frame_id"] = "sensor"
            elif change == "nonunit":
                record["transforms"][0]["transform"]["rotation"]["w"] = 2.
            else:
                record["transforms"][0]["transform"]["translation"]["x"] = "NaN"
            with self.subTest(change=change), self.assertRaises(ValueError):
                transform_quantities(record)

    def test_the_shared_temperature_column_says_which_producer_reported_it(self):
        """Two producers write `temperature_k` through one profile; the row must say which.

        The Jetson thermal-block path and a ROS `sensor_msgs/Temperature` reading are read with the
        same `json-profile.toml`. They must agree on the kelvin conversion and stay distinguishable,
        and the shared declaration must not assert either producer's subject for the other.
        """
        import importlib.util
        import re
        profile = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"
        spec = importlib.util.spec_from_file_location(
            "battery_csv", ROOT / "scripts/convert_ardupilot_battery_csv.py")
        battery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(battery)
        tegra = list(csv.DictReader(io.StringIO(battery.convert(json.dumps(
            {"exporter_version": "l4t-36.4.3", "capture_time_us": 55,
             "report": "CPU [1%@102] MCPU@20C"}), battery.TEGRA_FORMAT))))
        thermal = [r for r in tegra if r["tegrastats_record_kind"] == "TEMPERATURE"]
        self.assertEqual(len(thermal), 1)
        self.assertEqual(float(thermal[0]["temperature_k"]), 293.15)
        self.assertEqual(bytes.fromhex(thermal[0]["tegrastats_block_hex"][4:]).decode(), "MCPU")
        self.assertNotIn("sensor_report_type", thermal[0])

        header = samples()[0].header
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bag"
            bag(source, messages=[STORE.types["sensor_msgs/msg/Temperature"](header, 20., 0.)])
            output, _ = convert(source)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(rows[0]["temperature_k"]), float(thermal[0]["temperature_k"]))
            self.assertEqual(rows[0]["sensor_report_type"], "sensor_msgs/msg/Temperature")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                csv_path = Path(directory) / "out.csv"
                csv_path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(profile), str(csv_path), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common["observations"][0]["fields"]["temperature_k"], 293.15)
                self.assertEqual(common["observations"][0]["fields"]["sensor_report_type"],
                                 "sensor_msgs/msg/Temperature")
                declared = common["profile_units"]["temperature_k"]
                self.assertNotEqual(
                    declared, "K_reported_named_processor_block_not_calibrated_environment_temperature")
                self.assertIn("does_NOT_establish_what_body_was_measured", declared)
                self.assertIn("tegrastats_record_kind", declared)
                self.assertIn("sensor_report_type", declared)
                self.assertTrue(common["profile_units"]["sensor_report_type"])

        from scripts.convert_rosbag_observations import SENSOR_FIELDS
        keys = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*) = ", profile.read_text(), re.M))
        self.assertEqual([f for f in SENSOR_FIELDS if f not in keys], [])

    def test_scalar_and_magnetic_units_reuse_typed_container_and_common_output(self):
        types, header = STORE.types, samples()[0].header
        names = ["Temperature", "FluidPressure", "RelativeHumidity", "Illuminance", "MagneticField"]
        messages = [types["sensor_msgs/msg/" + n](header, value, variance)
                    for n, value, variance in zip(names[:4], [20., 101325., .5, 12.], [0., 4., .01, 2.])]
        messages.append(types["sensor_msgs/msg/MagneticField"](header,
            types["geometry_msgs/msg/Vector3"](1e-5, float("nan"), -2e-5), np.zeros(9)))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bag"
            bag(source, messages=messages)
            output, report = convert(source)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_messages"], report["decoded_messages"], len(rows)), (6, 5, 5))
            self.assertEqual(float(rows[0]["temperature_k"]), 293.15)
            self.assertEqual(rows[0]["sensor_variance_status"], "UNKNOWN")
            self.assertEqual(float(rows[1]["external_pressure_pa"]), 101325.)
            self.assertEqual(float(rows[2]["relative_humidity_fraction"]), .5)
            self.assertEqual(float(rows[3]["illuminance_lux"]), 12.)
            self.assertEqual(float(rows[4]["magnetic_field_tesla_z"]), -2e-5)
            self.assertEqual(rows[4]["magnetic_field_tesla_y"], "")
            self.assertEqual(rows[4]["magnetic_field_y_status"], "UNREPORTED")
            self.assertEqual(rows[4]["magnetic_covariance_status"], "UNKNOWN")
            retained = json.loads(bytes.fromhex(rows[4]["source_record_hex"][4:]))
            self.assertEqual(retained["decoded"]["magnetic_field"]["y"], "NaN")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                csv_path = Path(directory) / "out.csv"; csv_path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(csv_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 5)
                self.assertEqual(common["observations"][0]["fields"]["temperature_k"], 293.15)
                self.assertEqual(common["platform_domain"], "Unknown")
                units = common["profile_units"]
                self.assertTrue(units["external_pressure_pa"].startswith("Pa_"))
                self.assertTrue(units["illuminance_lux"].startswith("lux_"))
                self.assertIn("closed_interval_zero_to_one", units["relative_humidity_fraction"])
                self.assertIn("refused_rather_than_clipped", units["relative_humidity_fraction"])
                self.assertTrue(units["magnetic_field_tesla_x"].startswith("T_"))
                self.assertIn("zero_to_mean_unknown_variance", units["sensor_variance_status"])
                self.assertIn("or_an_authenticated_identity", units["sensor_frame_hex"])

    def test_sensor_bad_units_variance_or_frame_rejected_not_guessed(self):
        from scripts.convert_rosbag_observations import sensor_quantities
        base = {"header": {"frame_id": "sensor"}, "variance": 0.}
        for name, field, value in [("Temperature", "temperature", -274.),
                                  ("Temperature", "temperature", "NaN"),
                                  ("FluidPressure", "fluid_pressure", -1.),
                                  ("RelativeHumidity", "relative_humidity", 1.1),
                                  ("Illuminance", "illuminance", -1.)]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                sensor_quantities(dict(base, **{field: value}), "sensor_msgs/msg/" + name)
        with self.assertRaises(ValueError):
            sensor_quantities(dict(base, temperature=20., variance=-1.), "sensor_msgs/msg/Temperature")
        for frame, x in [("", 1.), ("sensor", "Infinity")]:
            with self.assertRaises(ValueError):
                sensor_quantities({"header": {"frame_id": frame},
                    "magnetic_field": {"x": x, "y": 0., "z": 0.},
                    "magnetic_field_covariance": [0.] * 9}, "sensor_msgs/msg/MagneticField")

    def test_odometry_covariance_values_reach_common_output_in_row_major_order(self):
        from scripts.convert_rosbag_observations import ODOMETRY, COVARIANCE_AXES
        types = STORE.types
        header = samples()[0].header
        vec = types["geometry_msgs/msg/Vector3"]
        pose = types["geometry_msgs/msg/Pose"](types["geometry_msgs/msg/Point"](1., 2., 3.),
            types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.))
        pose_cov = np.zeros(36)
        for axis in range(6):
            pose_cov[axis * 6 + axis] = float(f"{axis + 1}{axis + 1}")
        pose_cov[0 * 6 + 5] = .5      # x / rot_z term, m*rad
        pose_cov[5 * 6 + 0] = -.5     # deliberately not the mirror of it
        twist_cov = np.array([float(index) / 10 for index in range(36)])
        reported = types[ODOMETRY](header, "child",
            types["geometry_msgs/msg/PoseWithCovariance"](pose, pose_cov),
            types["geometry_msgs/msg/TwistWithCovariance"](
                types["geometry_msgs/msg/Twist"](vec(4., 5., 6.), vec(.1, .2, .3)), twist_cov))
        zeroed = types[ODOMETRY](header, "child",
            types["geometry_msgs/msg/PoseWithCovariance"](pose, np.zeros(36)),
            types["geometry_msgs/msg/TwistWithCovariance"](
                types["geometry_msgs/msg/Twist"](vec(4., 5., 6.), vec(.1, .2, .3)), np.zeros(36)))
        self.assertEqual(len(COVARIANCE_AXES["pose"]), 6)
        self.assertEqual(len(COVARIANCE_AXES["twist"]), 6)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"
            bag(path, messages=[reported, zeroed])
            output, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["decoded_messages"], 2)
            carried = json.loads(bytes.fromhex(rows[0]["pose_covariance_row_major_hex"][4:]))
            self.assertEqual(len(carried), 36)
            self.assertEqual([carried[axis * 6 + axis] for axis in range(6)],
                             [11, 22, 33, 44, 55, 66])
            self.assertEqual((carried[5], carried[30]), (.5, -.5))  # order kept, not transposed
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["twist_covariance_row_major_hex"][4:])),
                             [index / 10 for index in range(36)])
            self.assertEqual(rows[0]["pose_covariance_status"], "REPORTED")
            self.assertEqual(rows[1]["pose_covariance_status"], "REPORTED_ALL_ZERO")
            self.assertEqual(json.loads(bytes.fromhex(rows[1]["pose_covariance_row_major_hex"][4:])),
                             [0.0] * 36)
            self.assertEqual(rows[0]["motion_source_stamp_us"], "2123456")
            self.assertNotEqual(rows[0]["record_time_us"], rows[0]["motion_source_stamp_us"])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                out = Path(directory) / "out.csv"; out.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(out), "--allow-equal-time"], check=True, capture_output=True).stdout)
                fields = common["observations"][0]["fields"]
                values = json.loads(bytes.fromhex(fields["pose_covariance_row_major_hex"][4:]))
                self.assertEqual([values[axis * 6 + axis] for axis in range(6)],
                                 [11, 22, 33, 44, 55, 66])
                self.assertEqual(fields["motion_source_stamp_us"], 2123456)
                self.assertEqual(fields["pose_frame_hex"], "hex:" + b"sensor".hex())
                self.assertEqual(fields["twist_frame_hex"], "hex:" + b"child".hex())
                units = common["profile_units"]
                for name, axes in (("pose", "x_m_y_m_z_m_rot_x_rad_rot_y_rad_rot_z_rad"),
                                   ("twist", "vx_m_s_vy_m_s_vz_m_s_wx_rad_s_wy_rad_s_wz_rad_s")):
                    declared = units[f"{name}_covariance_row_major_hex"]
                    self.assertIn("row_major", declared)
                    self.assertIn(axes, declared)
                    self.assertIn("product_of_those_two_axis_units", declared)
                    self.assertIn("no_positive_semidefinite", declared)
                    self.assertIn("all_zero_never_promoted_to_exact_certainty", declared)
                self.assertIn("never_the_four_quaternion_components",
                              units["pose_covariance_row_major_hex"])
                self.assertIn("m2_s2", units["twist_covariance_row_major_hex"])
        from scripts.convert_rosbag_observations import motion_part
        base = {"pose": {"position": {"x": 0., "y": 0., "z": 0.},
                         "orientation": {"x": 0., "y": 0., "z": 0., "w": 1.}}}
        self.assertNotIn("pose_covariance_row_major_hex", motion_part(base, "pose", "f"))
        for bad in ([0.] * 35, [0.] * 37, [float("nan")] + [0.] * 35, [float("inf")] + [0.] * 35):
            with self.subTest(len(bad)), self.assertRaises(ValueError):
                motion_part({**base, "covariance": bad}, "pose", "f")

    def test_diagnostics_and_odometry_share_record_path_without_collapsing_entries(self):
        from scripts.convert_rosbag_observations import DIAGNOSTIC, ODOMETRY
        types = STORE.types
        header = samples()[0].header
        status = types["diagnostic_msgs/msg/DiagnosticStatus"]
        kv = types["diagnostic_msgs/msg/KeyValue"]
        diagnostic = types[DIAGNOSTIC](header, [status(level, "same-name", "text", "same-id",
            [kv("value", "not a declared unit"), kv("value", "retained duplicate")]) for level in [0, 1, 2, 3, 42]])
        empty = types[DIAGNOSTIC](header, [])
        vec = types["geometry_msgs/msg/Vector3"]
        pose = types["geometry_msgs/msg/Pose"](types["geometry_msgs/msg/Point"](1., 2., 3.),
            types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.))
        motion = types[ODOMETRY](header, "child", types["geometry_msgs/msg/PoseWithCovariance"](pose, np.zeros(36)),
            types["geometry_msgs/msg/TwistWithCovariance"](
                types["geometry_msgs/msg/Twist"](vec(4., 5., 6.), vec(.1, .2, .3)), np.zeros(36)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"
            bag(path, messages=[diagnostic, empty, motion])
            output, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_messages"], report["decoded_messages"], len(rows)), (4, 3, 7))
            self.assertEqual(report["output_records"], 7)
            self.assertEqual([r["diagnostic_level_reported"] for r in rows[:5]],
                             ["OK", "WARN", "ERROR", "STALE", "UNKNOWN_42"])
            self.assertEqual(rows[5]["diagnostic_level_reported"], "EMPTY_ARRAY")
            self.assertEqual(rows[5]["diagnostic_entry_count"], "0")
            retained = json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:]))
            self.assertEqual(len(retained["decoded"]["status"][0]["values"]), 2)
            for index, row in enumerate(rows[:5]):
                self.assertEqual((row["diagnostic_entry_index"], row["diagnostic_entry_count"]),
                                 (str(index), "5"))
                self.assertEqual(row["diagnostic_name_hex"], "hex:" + b"same-name".hex())
                self.assertEqual(row["diagnostic_message_hex"], "hex:" + b"text".hex())
                self.assertEqual(row["diagnostic_hardware_id_hex"], "hex:" + b"same-id".hex())
                self.assertEqual(row["diagnostic_value_count"], "2")
                self.assertEqual(json.loads(bytes.fromhex(row["diagnostic_value_pairs_hex"][4:])),
                                 [["value", "not a declared unit"], ["value", "retained duplicate"]])
            self.assertEqual([r["diagnostic_level_code"] for r in rows[:5]], ["0", "1", "2", "3", "42"])
            self.assertEqual((rows[5]["diagnostic_level_code"], rows[5]["diagnostic_value_count"],
                              rows[5]["diagnostic_value_pairs_hex"], rows[5]["diagnostic_entry_index"]),
                             ("", "", "", ""))
            self.assertEqual(float(rows[6]["pose_position_m_z"]), 3)
            self.assertEqual(float(rows[6]["twist_linear_m_s_x"]), 4)
            self.assertEqual(float(rows[6]["twist_angular_rad_s_z"]), .3)
            self.assertEqual(rows[6]["pose_covariance_status"], "REPORTED_ALL_ZERO")
            self.assertEqual(rows[6]["pose_frame_hex"], "hex:" + b"sensor".hex())
            self.assertEqual(rows[6]["twist_frame_hex"], "hex:" + b"child".hex())
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                source = Path(directory) / "out.csv"; source.write_text(output)
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(source), "--allow-equal-time"], capture_output=True, check=True)
                common = json.loads(result.stdout)
                self.assertEqual(common["main_rows"], 7)
                self.assertEqual(common["observations"][2]["fields"]["diagnostic_level_reported"], "ERROR")
                self.assertEqual(common["observations"][2]["fields"]["diagnostic_level_code"], 2)
                self.assertEqual(common["observations"][2]["fields"]["diagnostic_value_count"], 2)
                units = common["profile_units"]
                for column in ("diagnostic_entry_count", "diagnostic_entry_index",
                               "diagnostic_level_reported", "diagnostic_level_code",
                               "diagnostic_name_hex", "diagnostic_message_hex",
                               "diagnostic_hardware_id_hex", "diagnostic_value_count",
                               "diagnostic_value_pairs_hex"):
                    self.assertTrue(units.get(column), column)
                self.assertIn("never_a_musubi_diagnosis_or_an_overall_health_verdict",
                              units["diagnostic_level_reported"])
                self.assertIn("not_a_healthy_device", units["diagnostic_entry_count"])
                self.assertIn("never_defaulted_to_zero", units["diagnostic_level_code"])
                self.assertIn("duplicate_keys_preserved_never_a_dictionary",
                              units["diagnostic_value_pairs_hex"])
                self.assertEqual(common["observations"][6]["fields"]["twist_linear_m_s_y"], 5)
                odom = common["observations"][6]["fields"]
                self.assertEqual(odom["pose_position_m_z"], 3)
                self.assertEqual(odom["twist_angular_rad_s_z"], .3)
                self.assertEqual(odom["pose_frame_hex"], "hex:" + b"sensor".hex())
                self.assertEqual(odom["twist_frame_hex"], "hex:" + b"child".hex())
                self.assertNotEqual(odom["pose_frame_hex"], odom["twist_frame_hex"])
                self.assertEqual(odom["pose_covariance_status"], "REPORTED_ALL_ZERO")
                for column in ("pose_position_m_x", "pose_position_m_y", "pose_position_m_z",
                               "pose_orientation_quaternion_w", "twist_linear_m_s_x",
                               "twist_angular_rad_s_z", "pose_frame_hex", "twist_frame_hex",
                               "pose_covariance_status", "twist_covariance_status"):
                    self.assertTrue(units.get(column), column)
                self.assertIn("parent_frame_named_by_pose_frame_hex", units["pose_position_m_x"])
                self.assertIn("child_frame_named_by_twist_frame_hex", units["twist_linear_m_s_x"])
                self.assertIn("never_an_implicit_world_frame", units["pose_frame_hex"])
                self.assertIn("the_two_are_never_merged", units["twist_frame_hex"])
                self.assertIn("all_zero_is_never_promoted_to_exact_certainty",
                              units["pose_covariance_status"])
                self.assertIn("stay_in_the_retained_source_record", units["pose_covariance_status"])
                self.assertIn("refused_unless_normalized", units["pose_orientation_quaternion_w"])

        from scripts.convert_rosbag_observations import diagnostic_quantities
        entry = {"level": 0, "name": "n", "message": "m", "hardware_id": "h",
                 "values": [{"key": "k", "value": "v"}]}
        self.assertEqual(diagnostic_quantities({"status": [entry]})[0]["diagnostic_value_count"], 1)
        for bad in ({"level": 256}, {"level": True}, {"name": 5}, {"values": {"k": "v"}},
                    {"values": [{"key": 1, "value": "v"}]}, {"values": [{"key": "k", "value": 2}]}):
            with self.subTest(str(bad)), self.assertRaises(ValueError):
                diagnostic_quantities({"status": [dict(entry, **bad)]})

        from scripts.convert_rosbag_observations import motion_quantities, plain
        for bad in ["frame", "quaternion", "nonfinite"]:
            raw = plain(motion)
            if bad == "frame": raw["child_frame_id"] = ""
            if bad == "quaternion": raw["pose"]["pose"]["orientation"]["w"] = 0
            if bad == "nonfinite": raw["twist"]["twist"]["linear"]["x"] = "NaN"
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                motion_quantities(raw)

    def test_both_containers_reuse_battery_meaning_and_preserve_imu_and_all_records(self):
        for plugin in StoragePlugin:
            with self.subTest(plugin=plugin), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "bag"
                bag(source, plugin)
                output, report = convert(source)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((len(rows), report["source_messages"], report["decoded_messages"]), (2, 3, 2))
                self.assertEqual(len(report["unsupported_records"]), 1)
                self.assertEqual(report["unsupported_records"][0]["topic"], "/unsupported")
                self.assertEqual(float(rows[0]["battery_power_w"]), -48.)
                self.assertEqual(float(rows[0]["battery_stored_charge_ah"]), 3.)
                self.assertEqual(rows[0]["battery_consumed_ah"], "")
                self.assertEqual(rows[0]["record_time_us"], "7000123")
                retained = json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:]))
                self.assertEqual(retained["capture_ns"], "7000123456")
                self.assertEqual(retained["decoded"]["header"]["stamp"]["nanosec"], 123456789)
                self.assertEqual(retained["decoded"]["cell_voltage"], [12., 12.])
                self.assertEqual(float(rows[1]["angular_velocity_rad_s_x"]), 1.)
                self.assertEqual(float(rows[1]["linear_acceleration_m_s2_z"]), 9.81)
                self.assertEqual(rows[1]["orientation_covariance_status"], "UNKNOWN")
                self.assertEqual(rows[1]["angular_velocity_covariance_status"], "REPORTED")

    def test_missing_estimate_and_invalid_hash_or_cdr(self):
        battery, imu = samples()
        imu.orientation_covariance[0] = -1
        imu.orientation.w = float("nan")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "absent"
            bag(source, messages=[imu])
            row = next(csv.DictReader(io.StringIO(convert(source)[0])))
            self.assertEqual(row["orientation_quaternion_w"], "")
            self.assertEqual(row["orientation_covariance_status"], "ABSENT")
            for name, options in [("hash", {"hash_override": "RIHS01_" + "0" * 64}),
                                  ("definition", {"wrong_definition": True}),
                                  ("truncated", {"corrupt": True})]:
                source = Path(directory) / name
                bag(source, **options)
                with self.subTest(name=name), self.assertRaises(Exception):
                    convert(source)
        raw = bytes(STORE.serialize_cdr(battery, BATTERY))
        for bad in [b"\x00\x03\x00\x00" + raw[4:], raw + b"invalid", raw[:10]]:
            with self.assertRaises(Exception):
                decode(bad, BATTERY)

    def test_legacy_sqlite_without_type_hash_reuses_battery_meaning_on_explicit_selection(self):
        """A pre-hash SQLite3 bag reaches the adopted V/A/Ah/K/fraction meaning, unverified.

        The payloads are serialized by the older distros' OWN typestores (Foxy, Galactic), so the
        equal byte layout is exercised, not assumed. Nothing here verifies the producer.
        """
        from rosbags.typesys import Stores, get_typestore
        from scripts.convert_rosbag_observations import HASHES, LEGACY_SCHEMA_BASIS
        foxy, galactic = get_typestore(Stores.ROS2_FOXY), get_typestore(Stores.ROS2_GALACTIC)
        cases = ((2, foxy, dict(voltage=24., current=-2., temperature=float("nan")),
                  dict(voltage=48., current=-1.5, temperature=21.5, charge=6., percentage=.5)),
                 (1, galactic, dict(voltage=12.5, current=3.25, temperature=-10., charge=1.5,
                                    percentage=.25, status=1, technology=2),
                  dict(voltage=12.5, current=0., temperature=5., present=False, status=4)))
        for schema, store, first, second in cases:
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as directory:
                payloads = [store.serialize_cdr(legacy_battery(store, **values), BATTERY)
                            for values in (first, second)]
                source = legacy_bag(Path(directory) / "bag", payloads, schema=schema)
                with self.assertRaises(ValueError) as refusal:
                    convert(source)
                self.assertEqual(str(refusal.exception),
                                 "selected ROS schema hash or serialization mismatch")
                output, report = convert(source, legacy_schema="battery-humble-equivalent")
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((len(rows), report["source_messages"], report["decoded_messages"]), (2, 2, 2))
                self.assertEqual(report["unsupported_records"], [])
                selection = report["legacy_schema_selection"]
                self.assertEqual(selection["storage_schema"], schema)
                self.assertEqual(selection["type"], BATTERY)
                self.assertEqual(selection["basis"], LEGACY_SCHEMA_BASIS)
                self.assertEqual(selection["local_definition_hash"], HASHES[BATTERY])
                self.assertEqual(selection["unselected_legacy_connections"], [])
                self.assertIn("the producer, its binary", selection["not_verified"])
                self.assertEqual(rows[0]["record_time_us"], str(LEGACY_CAPTURE_NS // 1000))
                self.assertEqual(rows[1]["record_time_us"], str(LEGACY_CAPTURE_NS // 1000 + 1000))
                self.assertEqual(float(rows[0]["battery_voltage_v"]), first["voltage"])
                self.assertEqual(float(rows[0]["battery_current_a"]), first["current"])
                self.assertEqual(float(rows[0]["battery_power_w"]), first["voltage"] * first["current"])
                self.assertEqual(float(rows[0]["battery_stored_charge_ah"]), first.get("charge", 3.))
                self.assertEqual(float(rows[0]["battery_remaining_fraction"]), first.get("percentage", .75))
                self.assertEqual(rows[0]["battery_current_sign_basis"], "NEGATIVE_DISCHARGING")
                for row in rows:
                    retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                    self.assertEqual(retained["type_hash"], "ABSENT_NOT_VERIFIED_LEGACY_SQLITE_SCHEMA")
                    self.assertEqual(retained["type_hash_basis"], LEGACY_SCHEMA_BASIS)
                    self.assertIn("NOT_VERIFIED", retained["type_hash_basis"])
                    self.assertEqual(retained["selected_schema"], "battery-humble-equivalent")
                    self.assertEqual(retained["local_definition_hash"], HASHES[BATTERY])
                    self.assertEqual(retained["storage_schema"], schema)
                    self.assertEqual(retained["type"], BATTERY)
                    self.assertEqual(retained["decoded"]["header"]["stamp"]["nanosec"], 250000000)
                    self.assertEqual(retained["decoded"]["cell_voltage"], [12., 12.])
                    self.assertEqual(retained["decoded"]["serial_number"], "reported-id")
                for row, raw in zip(rows, payloads):
                    retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                    self.assertEqual(retained["raw_cdr_hex"], bytes(raw).hex())
                if first["temperature"] != first["temperature"]:  # NaN marker of the schema itself
                    self.assertEqual(rows[0]["battery_temperature_k"], "")
                    self.assertEqual(rows[0]["battery_temperature_basis"], "UNAVAILABLE_SOURCE_NAN")
                else:
                    self.assertAlmostEqual(float(rows[0]["battery_temperature_k"]),
                                           first["temperature"] + 273.15)
                    self.assertEqual(rows[0]["battery_temperature_basis"], "REPORTED")
                if second.get("present", True) is False:
                    self.assertEqual(rows[1]["battery_voltage_v"], "")
                    self.assertEqual(rows[1]["battery_power_w"], "")
                    self.assertEqual(rows[1]["battery_temperature_basis"], "BATTERY_NOT_PRESENT")
                    self.assertEqual(rows[1]["battery_reported_charging_state"], "FULL")
                else:
                    self.assertEqual(float(rows[1]["battery_voltage_v"]), second["voltage"])
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    fields = common["observations"][0]["fields"]
                    self.assertEqual(fields["battery_voltage_v"], first["voltage"])
                    self.assertEqual(fields["battery_power_w"], first["voltage"] * first["current"])
                    self.assertEqual(fields["battery_remaining_fraction"], first.get("percentage", .75))
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    units = common["profile_units"]
                    self.assertTrue(units["battery_voltage_v"].startswith("V"))
                    self.assertTrue(units["battery_current_a"].startswith("A"))
                    self.assertTrue(units["battery_stored_charge_ah"].startswith("Ah"))
                    self.assertTrue(units["battery_temperature_k"].startswith("K_converted_from"))
                command = [os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                           str(source), str(Path(directory) / "cli"),
                           "--legacy-schema", "battery-humble-equivalent"]
                subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
                self.assertEqual((Path(directory) / "cli/observations.csv").read_text(), output)

    def test_legacy_selection_is_bounded_and_the_hash_guard_is_unchanged(self):
        from rosbags.typesys import Stores, get_typestore
        from scripts.convert_rosbag_observations import HASHES, IMU, LEGACY_SCHEMA_CHOICES
        foxy = get_typestore(Stores.ROS2_FOXY)
        self.assertEqual(set(LEGACY_SCHEMA_CHOICES), {"battery-humble-equivalent"})
        self.assertEqual(LEGACY_SCHEMA_CHOICES["battery-humble-equivalent"], BATTERY)
        raw = bytes(foxy.serialize_cdr(legacy_battery(foxy), BATTERY))
        with tempfile.TemporaryDirectory() as directory:
            source = legacy_bag(Path(directory) / "bag", [raw])
            for asserted in (BATTERY, "battery", "", "BATTERY-HUMBLE-EQUIVALENT",
                             "sensor_msgs/msg/Imu", "trust-me"):
                with self.subTest(asserted=asserted), self.assertRaises(ValueError) as refusal:
                    convert(source, legacy_schema=asserted)
                self.assertEqual(str(refusal.exception),
                                 "legacy schema selection must name one supported ROS2 schema contract")
            with self.assertRaises(ValueError):  # the ROS1 path has no legacy selection
                convert(Path(directory) / "bag", legacy_schema="battery-humble-equivalent", ros1=True)
            with self.assertRaises(ValueError) as refusal:  # never silently excluded by compaction
                convert(source, scan_compact=True, legacy_schema="battery-humble-equivalent")
            self.assertEqual(str(refusal.exception),
                             "legacy schema selection cannot use another compact selection")
            with self.assertRaises(ValueError):
                convert(source, allow_missing_voltage=True, legacy_schema="battery-humble-equivalent",
                        list_compact="actions")
            current = Path(directory) / "current"
            bag(current, messages=[legacy_battery(STORE)])
            with self.assertRaises(ValueError) as refusal:
                convert(current, legacy_schema="battery-humble-equivalent")
            self.assertEqual(str(refusal.exception),
                             "legacy schema selection requires a legacy sqlite3 storage schema")
            self.assertEqual(len(list(csv.DictReader(io.StringIO(convert(current)[0])))), 1)
            mcap = Path(directory) / "mcap"
            bag(mcap, StoragePlugin.MCAP, messages=[legacy_battery(STORE)])
            with self.assertRaises(ValueError):
                convert(mcap, legacy_schema="battery-humble-equivalent")
            for name, declared, message in (
                    ("wrong", "RIHS01_" + "0" * 64, "selected ROS schema hash or serialization mismatch"),
                    ("right", HASHES[BATTERY], "selected ROS definition is missing")):
                declaring = legacy_bag(Path(directory) / ("declared_" + name), [raw],
                                       declared_hash=declared)
                for selection in (None, "battery-humble-equivalent"):
                    with self.subTest(declared=name, selection=selection), \
                            self.assertRaises(ValueError) as refusal:
                        convert(declaring, legacy_schema=selection)
                    self.assertEqual(str(refusal.exception), message)
            for name, payload in (("truncated", raw[:-8]), ("header", b"\x00\x03\x00\x00" + raw[4:]),
                                  ("trailing", raw + b"invalid"), ("empty", b""),
                                  ("short", raw[:6])):
                broken = legacy_bag(Path(directory) / ("broken_" + name), [payload])
                with self.subTest(name=name), self.assertRaises(Exception):
                    convert(broken, legacy_schema="battery-humble-equivalent")
            compressed = legacy_bag(Path(directory) / "compressed", [raw], compression="file")
            with self.assertRaises(ValueError) as refusal:
                convert(compressed, legacy_schema="battery-humble-equivalent")
            self.assertEqual(str(refusal.exception),
                             "legacy schema selection requires an uncompressed sqlite3 bag")
            noncdr = legacy_bag(Path(directory) / "noncdr", [raw], serialization="cdr_fastrtps")
            with self.assertRaises(Exception):
                convert(noncdr, legacy_schema="battery-humble-equivalent")
            imu = bytes(foxy.serialize_cdr(samples()[1], IMU))
            mixed = legacy_bag(Path(directory) / "mixed", [raw],
                               extra=(("/imu", IMU, [imu]),))
            with self.assertRaises(ValueError):
                convert(mixed)
            output, report = convert(mixed, legacy_schema="battery-humble-equivalent")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((len(rows), report["source_messages"], report["decoded_messages"]), (1, 2, 1))
            self.assertEqual(len(report["unsupported_records"]), 1)
            unsupported = report["unsupported_records"][0]
            self.assertEqual((unsupported["topic"], unsupported["type"]), ("/imu", IMU))
            self.assertEqual(unsupported["legacy_disposition"],
                             "TYPE_NOT_SELECTED_NO_HASH_PRESENT_NOT_DECODED")
            self.assertEqual(unsupported["raw_cdr_hex"], imu.hex())  # no silent drop
            self.assertNotIn("decoded", unsupported)
            self.assertEqual(report["legacy_schema_selection"]["unselected_legacy_connections"],
                             [dict(topic="/imu", type=IMU, declared_messages=1,
                                   disposition="TYPE_NOT_SELECTED_NO_HASH_PRESENT_NOT_DECODED")])

    def test_legacy_lookalike_producer_is_undetectable_and_is_reported_unverified(self):
        """The boundary this selection cannot close, demonstrated rather than asserted away.

        A producer whose own BatteryState carried the same name and byte width but a different
        field order is decoded as the local definition. Nothing in a pre-hash bag can detect that,
        so the output must never call the schema verified - it must say the producer is unverified.
        """
        from rosbags.typesys import Stores, get_typestore, get_types_from_msg
        from scripts.convert_rosbag_observations import LEGACY_SCHEMA_BASIS
        local = get_typestore(Stores.ROS2_HUMBLE).generate_msgdef(BATTERY, ros_version=2)[0]
        swapped = local.replace("float32 current\nfloat32 charge", "float32 charge\nfloat32 current")
        self.assertNotEqual(swapped, local)
        store = get_typestore(Stores.ROS2_FOXY)
        alien = get_typestore(Stores.EMPTY)
        alien.register(get_types_from_msg(swapped.split("=" * 80)[0], BATTERY))
        alien.register(get_types_from_msg("time stamp\nstring frame_id", "std_msgs/msg/Header"))
        types = alien.types
        message = types[BATTERY](
            types["std_msgs/msg/Header"](types["builtin_interfaces/msg/Time"](5, 250000000), "battery"),
            24., float("nan"), 6., 2., 4., 4., .75, 2, 1, 3, True,
            np.array([12., 12.], dtype=np.float32), np.array([], dtype=np.float32),
            "slot", "reported-id")
        with tempfile.TemporaryDirectory() as directory:
            source = legacy_bag(Path(directory) / "lookalike",
                                [alien.serialize_cdr(message, BATTERY)])
            output, report = convert(source, legacy_schema="battery-humble-equivalent")
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row["battery_current_a"]), 6.)
            self.assertEqual(float(row["battery_stored_charge_ah"]), 2.)
            self.assertEqual(float(row["battery_power_w"]), 144.)
            retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
            self.assertEqual(retained["type_hash"], "ABSENT_NOT_VERIFIED_LEGACY_SQLITE_SCHEMA")
            self.assertEqual(retained["type_hash_basis"], LEGACY_SCHEMA_BASIS)
            self.assertIn("PRODUCER_AND_ITS_BINARY_UNVERIFIED", retained["type_hash_basis"])
            self.assertIn("the producer, its binary",
                          report["legacy_schema_selection"]["not_verified"])
            declared = legacy_bag(Path(directory) / "declared",
                                  [alien.serialize_cdr(message, BATTERY)],
                                  declared_hash=alien.hash_rihs01(BATTERY))
            with self.assertRaises(ValueError):
                convert(declared, legacy_schema="battery-humble-equivalent")
            with self.assertRaises(ValueError):
                convert(declared)

    def test_indexed_zstd_chunk_window_reaches_common_output_and_bounds_the_expansion(self):
        """Independent confirmation input: authored after the implementation was fixed.

        The development bag lives in the interval test; these values, topics and window are its
        own, so passing here is not the same input that drove the code.
        """
        import base64
        import random
        from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
        kind = "std_msgs/msg/String"
        battery, imu = samples()
        noise = base64.b64encode(random.Random(7).randbytes(768 * 1024)).decode("ascii")
        filler = STORE.serialize_cdr(STORE.types[kind](noise), kind)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zstd-window"
            writer = Writer(path, version=9, storage_plugin=StoragePlugin.MCAP)
            writer.set_compression(CompressionMode.STORAGE, CompressionFormat.ZSTD)
            with writer:
                power = writer.add_connection("/battery", BATTERY, typestore=STORE)
                motion = writer.add_connection("/imu", IMU, typestore=STORE)
                bulk = writer.add_connection("/bulk", kind, typestore=STORE)
                for index in range(24):  # unselected bulk, so the whole bag is over 16 MiB
                    writer.write(bulk, 500000 + index, filler)
                battery.voltage = 48.5
                writer.write(power, 4000, STORE.serialize_cdr(battery, BATTERY))
                writer.write(motion, 4500, STORE.serialize_cdr(imu, IMU))
                battery.voltage = 47.25
                writer.write(power, 5000, STORE.serialize_cdr(battery, BATTERY))
                battery.voltage = 12.0  # outside the window below
                writer.write(power, 9000, STORE.serialize_cdr(battery, BATTERY))
            total = sum(item.stat().st_size for item in path.iterdir())
            self.assertGreater(total, 16 * 1024 * 1024, "a compressed window over the old bound")
            with self.assertRaisesRegex(ValueError, "bag too large"):
                convert(path)
            output, report = convert(path, start_ns=4000, stop_ns=5001,
                                     topics=["/battery", "/imu"])
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([row["battery_voltage_v"] for row in rows if row["battery_voltage_v"]],
                             ["48.5", "47.25"])
            self.assertEqual(report["capture_window"]["selected_messages"], len(rows))
            window = report["capture_window"]["mcap"]
            self.assertGreaterEqual(window["selected_chunks"], 1)
            self.assertLessEqual(window["selected_chunk_bytes"], 16 * 1024 * 1024)
            self.assertLessEqual(window["selected_compressed_bytes"], 16 * 1024 * 1024)
            self.assertEqual(window["index_declared_selected_messages"], report["source_messages"])
            self.assertIn("zstd", window["accepted_contract"])
            self.assertIn("chunk CRC", window["not_verified"])
            name = next(path.glob("*.mcap")).name
            raw = (path / name).read_bytes()
            offset, records = int.from_bytes(raw[-28:-20], "little"), []
            while offset < len(raw) - 37:
                length = int.from_bytes(raw[offset + 1:offset + 9], "little")
                records.append((raw[offset], offset + 9, length))
                offset += 9 + length
            body, size = next((start, length) for kind_, start, length in records
                              if kind_ == 0x08 and int.from_bytes(raw[start:start + 8], "little") == 4000)
            chunk_start = int.from_bytes(raw[body + 16:body + 24], "little")
            self.assertEqual(raw[body + size - 20:body + size - 16], b"zstd")

            def patched(data, position, value, width=8):
                return data[:position] + value.to_bytes(width, "little") + data[position + width:]

            def refused(mutated, message, index=[0]):
                index[0] += 1
                target = Path(directory) / f"zstd-patched{index[0]}"
                target.mkdir()
                (target / "metadata.yaml").write_bytes((path / "metadata.yaml").read_bytes())
                (target / name).write_bytes(mutated)
                with self.assertRaisesRegex(ValueError, message):
                    convert(target, start_ns=4000, stop_ns=5001)

            refused(raw[:body + size - 20] + b"zstX" + raw[body + size - 16:],
                    "uncompressed, zstd or lz4")
            refused(patched(patched(raw, body + size - 8, 2 ** 40), chunk_start + 25, 2 ** 40),
                    "size or time declaration rejected")
            consistent = patched(patched(raw, body + size - 8, 999), chunk_start + 25, 999)
            refused(consistent, "frame disagrees with its declared size")
            refused(raw[:chunk_start + 60] + bytes(8) + raw[chunk_start + 68:],
                    "truncated compressed block")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                target = Path(directory) / "out.csv"; target.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                voltages = [o["fields"]["battery_voltage_v"] for o in common["observations"]
                            if o["fields"].get("battery_voltage_v") is not None]
                self.assertEqual(voltages, [48.5, 47.25])
                self.assertIn("K_converted_from", common["profile_units"]["battery_temperature_k"])
                self.assertIn("row_major", common["profile_units"]["pose_covariance_row_major_hex"])

    def test_indexed_lz4_chunk_window_reaches_common_output_and_bounds_the_expansion(self):
        """The lz4 chunk codec, inside the same interval semantics, accounting and limits.

        The pinned rosbags writer emits zstd or nothing (`CompressionFormat` has only `ZSTD`), so
        an lz4 bag is authored by substituting the writer's own two codec attributes. Every offset,
        chunk index and message index is still computed by that writer, which is why this is a
        valid MCAP rather than a hand-cut fixture.

        Authored input. No real bag, and the whole-file integrity of the bag is not claimed: only
        the chunks the index places inside the selected window are inspected at all.
        """
        import base64
        import random
        import lz4.frame
        from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
        kind = "std_msgs/msg/String"
        battery, imu = samples()
        noise = base64.b64encode(random.Random(11).randbytes(768 * 1024)).decode("ascii")
        filler = STORE.serialize_cdr(STORE.types[kind](noise), kind)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lz4-window"
            writer = Writer(path, version=9, storage_plugin=StoragePlugin.MCAP)
            writer.set_compression(CompressionMode.STORAGE, CompressionFormat.ZSTD)
            with writer:
                power = writer.add_connection("/battery", BATTERY, typestore=STORE)
                motion = writer.add_connection("/imu", IMU, typestore=STORE)
                bulk = writer.add_connection("/bulk", kind, typestore=STORE)
                writer.storage.compression = "lz4"
                writer.storage.compressor = lz4.frame.compress
                for index in range(24):  # unselected bulk, so the whole bag is over 16 MiB
                    writer.write(bulk, 700000 + index, filler)
                battery.voltage = 24.75
                writer.write(power, 6000, STORE.serialize_cdr(battery, BATTERY))
                writer.write(motion, 6500, STORE.serialize_cdr(imu, IMU))
                battery.voltage = 23.5
                writer.write(power, 7000, STORE.serialize_cdr(battery, BATTERY))
                battery.voltage = 11.0  # outside the window below
                writer.write(power, 12000, STORE.serialize_cdr(battery, BATTERY))
            name = next(path.glob("*.mcap")).name
            raw = (path / name).read_bytes()
            self.assertGreater(sum(item.stat().st_size for item in path.iterdir()),
                               16 * 1024 * 1024, "a compressed window over the old bound")
            self.assertIn(b"\x04\x22\x4d\x18", raw)
            with self.assertRaisesRegex(ValueError, "bag too large"):
                convert(path)

            output, report = convert(path, start_ns=6000, stop_ns=7001,
                                     topics=["/battery", "/imu"])
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([row["battery_voltage_v"] for row in rows if row["battery_voltage_v"]],
                             ["24.75", "23.5"])
            self.assertEqual(report["capture_window"]["selected_messages"], len(rows))
            window = report["capture_window"]["mcap"]
            self.assertGreaterEqual(window["selected_chunks"], 1)
            self.assertLessEqual(window["selected_chunk_bytes"], 16 * 1024 * 1024)
            self.assertLessEqual(window["selected_compressed_bytes"], 16 * 1024 * 1024)
            self.assertEqual(window["index_declared_selected_messages"], report["source_messages"])
            self.assertIn("lz4", window["accepted_contract"])
            self.assertIn("chunk CRC", window["not_verified"])

            offset, records = int.from_bytes(raw[-28:-20], "little"), []
            while offset < len(raw) - 37:
                length = int.from_bytes(raw[offset + 1:offset + 9], "little")
                records.append((raw[offset], offset + 9, length))
                offset += 9 + length
            body, size = next((start, length) for kind_, start, length in records
                              if kind_ == 0x08 and int.from_bytes(raw[start:start + 8], "little") == 6000)
            chunk_start = int.from_bytes(raw[body + 16:body + 24], "little")
            self.assertEqual(raw[body + size - 19:body + size - 16], b"lz4")

            def refused(mutated, message, index=[0]):
                index[0] += 1
                target = Path(directory) / f"lz4-patched{index[0]}"
                target.mkdir()
                (target / "metadata.yaml").write_bytes((path / "metadata.yaml").read_bytes())
                (target / name).write_bytes(mutated)
                with self.assertRaisesRegex(ValueError, message):
                    convert(target, start_ns=6000, stop_ns=7001)

            def patched(data, position, value, width=8):
                return data[:position] + value.to_bytes(width, "little") + data[position + width:]

            refused(raw[:body + size - 19] + b"lzX" + raw[body + size - 16:],
                    "uncompressed, zstd or lz4")
            consistent = patched(patched(raw, body + size - 8, 999), chunk_start + 25, 999)
            refused(consistent, "frame disagrees with its declared size")
            refused(patched(patched(raw, body + size - 8, 2 ** 40), chunk_start + 25, 2 ** 40),
                    "size or time declaration rejected")
            payload = chunk_start + 49 + 3
            refused(raw[:payload] + bytes(4) + raw[payload + 4:],
                    "invalid compressed MCAP chunk frame")

            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                target = Path(directory) / "out.csv"; target.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                voltages = [o["fields"]["battery_voltage_v"] for o in common["observations"]
                            if o["fields"].get("battery_voltage_v") is not None]
                self.assertEqual(voltages, [24.75, 23.5])
                self.assertIn("K_converted_from", common["profile_units"]["battery_temperature_k"])
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_a_second_distinct_lz4_bag_reaches_the_same_common_meaning(self):
        """A different lz4 bag: different values, topics, window and size, same inherited meaning.

        Distinct from the >16 MiB case so the codec is not evidenced by a single input. Authored;
        no real bag.
        """
        import lz4.frame
        from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
        battery, imu = samples()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lz4-small"
            writer = Writer(path, version=9, storage_plugin=StoragePlugin.MCAP)
            writer.set_compression(CompressionMode.STORAGE, CompressionFormat.ZSTD)
            with writer:
                power = writer.add_connection("/battery", BATTERY, typestore=STORE)
                motion = writer.add_connection("/imu", IMU, typestore=STORE)
                writer.storage.compression = "lz4"
                writer.storage.compressor = lz4.frame.compress
                battery.voltage = 51.0  # before the window
                writer.write(power, 1000, STORE.serialize_cdr(battery, BATTERY))
                battery.voltage = 36.5
                writer.write(power, 20000, STORE.serialize_cdr(battery, BATTERY))
                writer.write(motion, 20500, STORE.serialize_cdr(imu, IMU))
                battery.voltage = 35.25
                writer.write(power, 21000, STORE.serialize_cdr(battery, BATTERY))
                battery.voltage = 9.5  # after the window
                writer.write(power, 40000, STORE.serialize_cdr(battery, BATTERY))

            output, report = convert(path, start_ns=20000, stop_ns=21001,
                                     topics=["/battery", "/imu"])
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([row["battery_voltage_v"] for row in rows if row["battery_voltage_v"]],
                             ["36.5", "35.25"], "only the selected interval, both ends excluded")
            window = report["capture_window"]["mcap"]
            self.assertIn("lz4", window["accepted_contract"])
            self.assertEqual(window["index_declared_selected_messages"], report["source_messages"])
            self.assertEqual(report["capture_window"]["selected_messages"], len(rows))

            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                target = Path(directory) / "out.csv"; target.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                voltages = [o["fields"]["battery_voltage_v"] for o in common["observations"]
                            if o["fields"].get("battery_voltage_v") is not None]
                self.assertEqual(voltages, [36.5, 35.25])
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
                self.assertEqual(common["platform_domain"], "Unknown")

    def test_large_mcap_interval_reuses_common_output_without_boundary_loss(self):
        import sys
        from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
        kind = "std_msgs/msg/String"
        battery, imu = samples()
        filler = STORE.serialize_cdr(STORE.types[kind]("x" * (1024 * 1024)), kind)
        note = STORE.serialize_cdr(STORE.types[kind]("retained"), kind)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interval"
            with Writer(path, version=9, storage_plugin=StoragePlugin.MCAP) as writer:
                power = writer.add_connection("/battery", BATTERY, typestore=STORE)
                motion = writer.add_connection("/imu", IMU, typestore=STORE)
                other = writer.add_connection("/other", kind, typestore=STORE)
                def battery_at(timestamp, voltage):
                    battery.voltage = voltage
                    writer.write(power, timestamp, STORE.serialize_cdr(battery, BATTERY))
                battery_at(1000, 11.)
                writer.write(other, 1000, filler)  # closes a chunk ending exactly at1000
                for index in range(16):
                    writer.write(other, 20000 + index, filler)  # unselected bulk beyond16MiB
                battery_at(2000, 12.)
                writer.write(motion, 2100, STORE.serialize_cdr(imu, IMU))
                writer.write(other, 2200, note)
                writer.write(other, 9000, filler)
                battery_at(3000, 13.)  # final single-record chunk
            self.assertGreater(sum(p.stat().st_size for p in path.iterdir()), 16 * 1024 * 1024)
            with self.assertRaisesRegex(ValueError, "bag too large"):
                convert(path)
            stamps = []
            for start, stop, voltages, ending, selected in ((1000, 2000, ["11.0"], 1, 1),
                    (2000, 3000, ["12.0", ""], 0, 1), (3000, 3001, ["13.0"], 1, 2)):
                output, report = convert(path, start_ns=start, stop_ns=stop, topics=["/battery", "/imu"])
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([row["battery_voltage_v"] for row in rows], voltages)
                window = report["capture_window"]["mcap"]
                self.assertEqual(window["declared_chunks"], 19)
                self.assertEqual(window["selected_chunks"], selected)
                self.assertEqual(window["chunks_ending_at_start_ns"], ending)
                self.assertEqual(window["index_declared_selected_messages"], len(rows))
                self.assertEqual(window["boundary_filtered_records"], 0)
                self.assertLess(window["selected_chunk_bytes"], 16 * 1024 * 1024)
                self.assertEqual(report["capture_window"]["selected_messages"], len(rows))
                self.assertEqual(report["topic_selection"]["read_messages"], len(rows))
                self.assertEqual(report["topic_selection"]["declared_excluded_messages"], 19)
                stamps += [json.loads(bytes.fromhex(row["source_record_hex"][4:]))["capture_ns"] for row in rows]
                if os.environ.get("MUSUBI_TELEMETRY_READER") and start == 1000:
                    target = Path(directory) / "interval.csv"; target.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], 11.)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            self.assertEqual(stamps, ["1000", "2000", "2100", "3000"])  # adjacent, no loss or repeat
            whole, report = convert(path, start_ns=1000, stop_ns=3001, topics=["/battery", "/imu"])
            self.assertEqual([row["record_time_us"] for row in csv.DictReader(io.StringIO(whole))],
                             [stamp[:-3] or "0" for stamp in stamps])
            self.assertEqual(report["capture_window"]["mcap"]["selected_chunks"], 3)
            with self.assertRaisesRegex(ValueError, "volume exceeds bound"):
                convert(path, start_ns=1000, stop_ns=30000, topics=["/battery"])
            output, report = convert(path, start_ns=2101, stop_ns=3001, topics=["/battery", "/imu"])
            self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 1)
            self.assertEqual(report["capture_window"]["mcap"]["boundary_filtered_records"], 1)
            self.assertEqual(report["capture_window"]["mcap"]["storage_request_start_ns"], 2100)
            output, report = convert(path, start_ns=2000, stop_ns=2300)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 2)
            self.assertEqual(report["source_messages"], 3)
            self.assertEqual([record["topic"] for record in report["unsupported_records"]], ["/other"])
            self.assertEqual(report["capture_window"]["mcap"]["index_declared_selected_messages"], 3)
            with self.assertRaisesRegex(ValueError, "no supported messages"):
                convert(path, start_ns=4000, stop_ns=5000)  # selected chunk, empty interval
            destination = Path(directory) / "cli-interval"
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(path), str(destination), "--start-ns", "3000", "--stop-ns", "3001", "--topic", "/battery"],
                check=True, capture_output=True)
            report = json.loads((destination / "report.json").read_text())
            self.assertEqual(report["capture_window"]["selected_messages"], 1)
            self.assertEqual(report["capture_window"]["mcap"]["not_verified"],
                "chunk CRC, message payload bytes, chunks outside the selected interval")
            compressed = Path(directory) / "compressed"
            writer = Writer(compressed, version=9, storage_plugin=StoragePlugin.MCAP)
            writer.set_compression(CompressionMode.STORAGE, CompressionFormat.ZSTD)
            with writer:
                connection = writer.add_connection("/battery", BATTERY, typestore=STORE)
                writer.write(connection, 1000, STORE.serialize_cdr(battery, BATTERY))
            self.assertEqual(convert(compressed)[1]["decoded_messages"], 1)
            windowed = convert(compressed, start_ns=1000, stop_ns=2000)[1]
            self.assertEqual(windowed["decoded_messages"], 1)
            self.assertEqual(windowed["capture_window"]["mcap"]["selected_chunks"], 1)
            self.assertGreater(windowed["capture_window"]["mcap"]["selected_compressed_bytes"], 0)
            self.assertIn("zstd", windowed["capture_window"]["mcap"]["accepted_contract"])
            source = Path(directory) / "small"
            bag(source, plugin=StoragePlugin.MCAP)
            name = next(source.glob("*.mcap")).name
            raw = (source / name).read_bytes()
            offset, records = int.from_bytes(raw[-28:-20], "little"), []
            while offset < len(raw) - 37:  # the summary the storage and the check both trust
                length = int.from_bytes(raw[offset + 1:offset + 9], "little")
                records.append((raw[offset], offset + 9, length))
                offset += 9 + length
            body, size = next((start, length) for kind_, start, length in records if kind_ == 0x08)
            entries = int.from_bytes(raw[body + 32:body + 36], "little")
            chunk_start = int.from_bytes(raw[body + 16:body + 24], "little")
            chunk_length = int.from_bytes(raw[body + 24:body + 32], "little")

            def replaced(data, position, value, width=8):
                return data[:position] + value.to_bytes(width, "little") + data[position + width:]

            def refused(mutated, message, index=[0]):
                index[0] += 1
                target = Path(directory) / f"patched{index[0]}"
                target.mkdir()
                (target / "metadata.yaml").write_bytes((source / "metadata.yaml").read_bytes())
                (target / name).write_bytes(mutated)
                with self.assertRaisesRegex(ValueError, message):
                    convert(target, start_ns=0, stop_ns=8000000000)

            refused(raw[:-40], "MCAP footer required")
            refused(raw[:30], "MCAP file magic required")
            refused(replaced(raw, len(raw) - 28, 0), "requires an MCAP summary section")
            refused(replaced(raw, body + 24, 17 * 1024 * 1024), "size or time declaration rejected")
            refused(replaced(raw, body + size - 8, 2 ** 40), "size or time declaration rejected")
            refused(raw[:chunk_start] + b"\x07" + raw[chunk_start + 1:], "not a chunk record")
            refused(replaced(raw, body + 16, chunk_start - 1), "offsets outside the data section")
            refused(replaced(raw, body + 16, len(raw)), "offsets outside the data section")
            refused(replaced(raw, chunk_start + 1, chunk_length), "does not match its index entry")
            refused(replaced(raw, chunk_start + chunk_length + 23, 2 ** 40),
                    "message index entry outside its chunk")
            refused(replaced(raw, chunk_start + chunk_length + 11, 48, 4),
                    "message index record does not match its offsets")
            refused(replaced(raw, body + 32, 7, 4), "malformed MCAP message index offsets")
            refused(replaced(raw, body - 8, 17 * 1024 * 1024), "oversized MCAP summary record")
            refused(replaced(raw, 9, 65537), "MCAP header record outside its bound")
            refused(replaced(raw, 9, len(raw)), "MCAP header record outside its bound")
            refused(raw[:8] + b"\x03" + raw[9:], "MCAP header record outside its bound")
            refused(replaced(raw, 17, 2 ** 31, 4), "malformed MCAP string")
            shortened = raw[body:body + 32] + bytes(4) + raw[body + 36 + entries:body + size]
            refused(raw[:body - 9] + b"\x08" + len(shortened).to_bytes(8, "little") + shortened
                    + raw[body + size:], "chunk without a message index")

    def test_mcap_summary_budget_refuses_the_tail_before_allocating_it(self):
        from scripts.convert_rosbag_observations import mcap_file_preflight
        part = 9 * 1024 * 1024  # two records, each inside the per-record bound
        buffer = (b"\x0a" + part.to_bytes(8, "little") + bytes(part)) * 2

        class Counted(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.total = 0

            def read(self, size=-1):
                data = super().read(size)
                self.total += len(data)
                return data

        stream = Counted(buffer)
        with self.assertRaisesRegex(ValueError, "oversized MCAP summary record"):
            mcap_file_preflight(stream, 0, 0, len(buffer), 0, 1, None, {})
        self.assertGreaterEqual(stream.total, part)  # the first body was read
        self.assertLess(stream.total, 2 * part)  # the tail is refused on its declaration

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_common_observation_connection_uses_capture_clock_without_domain_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bag"
            bag(source, StoragePlugin.MCAP)
            output = Path(directory) / "converted.csv"
            output.write_text(convert(source)[0])
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(output), "--allow-equal-time"], capture_output=True, check=True)
            records = json.loads(result.stdout)
            self.assertEqual((records["main_rows"], records["platform_domain"]), (2, "Unknown"))
            self.assertEqual(records["observations"][0]["fields"]["battery_power_w"], -48)
            self.assertEqual(records["observations"][1]["fields"]["linear_acceleration_m_s2_z"], 9.81)
            for record in records["observations"]:
                self.assertEqual(record["clock_basis"], "Unknown")
                self.assertIsNone(record["wall_ms"])
                self.assertIsNone(record["t_boot_us"])

    def test_saved_get_parameters_transactions_reach_common_observation(self):
        """Values associate to the requested names only for a fully matched transaction."""
        first = [service_event(0, GID_A, 1, names=["use_sim_time", "rate_hz", "frames"]),
                 service_event(2, GID_A, 1, values=[
                     parameter_value(type=1, bool_value=True),
                     parameter_value(type=3, double_value=25.5),
                     parameter_value(type=9, string_array_value=["odom", "base_link"])])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "first"
            service_bag(path, first)
            text, report = convert(path)
            self.assertEqual((report["source_messages"], report["decoded_messages"]), (2, 2))
            self.assertEqual(report["service_transactions"], 1)
            self.assertIn("not time proximity", report["service_correlation"])
            self.assertIn("cannot be mapped to a node", report["service_identity_limit"])
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["service_transaction_status"] == "MATCHED" for row in rows))
            self.assertEqual([bytes.fromhex(row["parameter_name_hex"][4:]).decode() for row in rows],
                             ["use_sim_time", "rate_hz", "frames", "frames"])
            self.assertEqual([row["parameter_type_reported"] for row in rows],
                             ["BOOL", "DOUBLE", "STRING_ARRAY", "STRING_ARRAY"])
            self.assertEqual(rows[0]["parameter_bool_reported"], "1")
            self.assertEqual(rows[1]["parameter_double_reported"], "25.5")
            self.assertEqual([bytes.fromhex(row["parameter_text_hex"][4:]).decode() for row in rows[2:]],
                             ["odom", "base_link"])
            self.assertEqual([row["parameter_element_index"] for row in rows[2:]], ["0", "1"])
            self.assertTrue(all(row["parameter_event_kind"] == "SERVICE_GET_PARAMETERS_REPORTED" for row in rows))
            self.assertTrue(all(row["parameter_value_status"] == "REPORTED_NOT_CURRENT_SETTING" for row in rows))
            self.assertEqual(rows[0]["service_client_gid_hex"], "hex:" + GID_A.hex())
            self.assertEqual(rows[0]["service_sequence_number"], "1")
            self.assertEqual(bytes.fromhex(rows[0]["service_event_topic_hex"][4:]).decode(), SERVICE_TOPIC)
            self.assertTrue(all(row["parameter_node_hex"] == "" for row in rows))
            self.assertTrue(all(row["service_node_identity"] == "NOT_ESTABLISHED_FROM_SAVED_EVENT"
                                for row in rows))
            self.assertIn("SUFFIX_RULE_NOT_VERIFIED", rows[0]["service_endpoint_basis"])
            self.assertEqual(rows[0]["service_stamp_basis"], SERVICE_STAMP_BASIS)
            self.assertEqual(rows[0]["service_event_stamp_us"], str(12 * 10**6 + 500))
            self.assertNotEqual(rows[0]["service_event_stamp_us"], rows[0]["record_time_us"])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                output = Path(directory) / "first.csv"
                output.write_text(text)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(output), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 4)
                self.assertEqual(common["observations"][1]["fields"]["parameter_double_reported"], 25.5)
                self.assertEqual(common["observations"][0]["fields"]["service_transaction_status"], "MATCHED")
                for record in common["observations"]:
                    self.assertEqual(record["clock_basis"], "Unknown")
                    self.assertIsNone(record["anchor_unix_us"])

    def test_saved_get_parameters_second_transaction_and_incomplete_cases(self):
        """Authored after the code was fixed; integers, bytes and every non-match status."""
        second = [service_event(1, GID_B, 77, names=["count", "mask", "label"]),
                  service_event(3, GID_B, 77, values=[
                      parameter_value(type=2, integer_value=-9),
                      parameter_value(type=5, byte_array_value=np.array([0, 255], dtype=np.uint8)),
                      parameter_value(type=4, string_value="ready")], sec=30, nanosec=0)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "second"
            service_bag(path, second)
            text, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(report["service_transactions"], 1)
            self.assertEqual([row["parameter_type_reported"] for row in rows],
                             ["INTEGER", "BYTE_ARRAY", "BYTE_ARRAY", "STRING"])
            self.assertEqual(rows[0]["parameter_integer_reported"], "-9")
            self.assertEqual([row["parameter_integer_reported"] for row in rows[1:3]], ["0", "255"])
            self.assertEqual(bytes.fromhex(rows[3]["parameter_text_hex"][4:]).decode(), "ready")
            self.assertEqual(rows[0]["service_event_stamp_us"], str(30 * 10**6))

            incomplete = [service_event(0, GID_A, 1, names=["only_request"]),
                          service_event(2, GID_A, 2, values=[parameter_value(type=4, string_value="orphan")]),
                          service_event(0, GID_A, 3, names=["a", "b"]),
                          service_event(2, GID_A, 3, values=[parameter_value(type=1, bool_value=False)]),
                          service_event(1, GID_A, 4), service_event(3, GID_A, 4),
                          service_event(0, GID_B, 5, names=["x"]),
                          service_event(0, GID_B, 5, names=["y"]),
                          service_event(2, GID_B, 5, values=[parameter_value(type=1, bool_value=True)]),
                          service_event(0, GID_A, 6, names=["unknown_code"]),
                          service_event(2, GID_A, 6, values=[parameter_value(type=42)])]
            path = Path(directory) / "incomplete"
            service_bag(path, incomplete)
            text, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(report["service_transactions"], 6)
            self.assertEqual([row["service_transaction_status"] for row in rows],
                             ["REQUEST_ONLY", "RESPONSE_ONLY", "COUNT_MISMATCH", "METADATA_ONLY",
                              "DUPLICATE_CONFLICT", "MATCHED"])
            for row in rows[:5]:
                self.assertEqual(row["parameter_name_hex"], "")
                self.assertEqual(row["parameter_type_reported"], "")
            self.assertEqual((rows[2]["service_request_name_count"], rows[2]["service_response_value_count"]),
                             ("2", "1"))
            self.assertEqual(rows[3]["service_transaction_events"], "2")
            self.assertEqual(rows[5]["parameter_type_reported"], "UNKNOWN")
            self.assertEqual(rows[5]["parameter_value_status"], "UNKNOWN_TYPE_RETAINED")

    def test_saved_get_parameters_keeps_every_participating_source_joinable(self):
        """Each event is retained once and every row joins back to all of its sources."""
        messages = [service_event(0, GID_A, 1, names=["only_in_request"], sec=5),
                    service_event(2, GID_A, 1, values=[
                        parameter_value(type=4, string_value="only_in_response")], sec=9),
                    service_event(0, GID_B, 2, names=["first_version"]),
                    service_event(0, GID_B, 2, names=["second_version"]),
                    service_event(2, GID_B, 2, values=[parameter_value(type=1, bool_value=True)])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "join"
            service_bag(path, messages)
            text, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(report["service_retained_sources"], 5)
            self.assertEqual(len(report["service_sources"]), 5)
            self.assertIn("retained once in service_sources", report["service_source_retention"])
            sources = {item["source_message_index"]: item for item in report["service_sources"]}
            self.assertEqual(sources[0]["decoded"]["request"][0]["names"], ["only_in_request"])
            self.assertEqual(sources[1]["decoded"]["response"][0]["values"][0]["string_value"],
                             "only_in_response")
            self.assertEqual(sources[0]["decoded"]["info"]["stamp"]["sec"], 5)
            self.assertEqual(sources[1]["decoded"]["info"]["stamp"]["sec"], 9)
            for index, item in sources.items():
                self.assertEqual(len(bytes.fromhex(item["raw_cdr_hex"])) % 4, 0)
                self.assertEqual(item["source_cdr_sha256"],
                                 hashlib.sha256(bytes.fromhex(item["raw_cdr_hex"])).hexdigest())
            self.assertEqual(rows[0]["service_source_indexes"], "0|1")
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:]))["transaction"],
                [{"source_message_index": 0, "event_type": 0, "capture_ns": 1789000000000000000},
                 {"source_message_index": 1, "event_type": 2, "capture_ns": 1789000000000001000}])
            conflict = rows[1]
            self.assertEqual(conflict["service_transaction_status"], "DUPLICATE_CONFLICT")
            self.assertEqual(conflict["service_source_indexes"], "2|3|4")
            self.assertEqual(conflict["service_transaction_events"], "3")
            self.assertEqual([sources[index]["decoded"]["request"][0]["names"] for index in (2, 3)],
                             [["first_version"], ["second_version"]])
            self.assertEqual(sources[4]["decoded"]["response"][0]["values"][0]["bool_value"], True)

    def test_saved_get_parameters_joins_by_key_in_a_mixed_topic_bag(self):
        """source_message_index is the global index, not a position in service_sources."""
        battery = samples()[0]
        request = service_event(0, GID_A, 3, names=["mixed"])
        response = service_event(2, GID_A, 3, values=[parameter_value(type=4, string_value="ok")])
        stamps = [7000000000000000001, 7000000000000000123, 7000000000000002001, 7000000000000002345]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed"
            with Writer(path, version=9, storage_plugin=StoragePlugin.MCAP) as writer:
                ordinary = writer.add_connection("/battery", BATTERY, typestore=STORE,
                    rihs01=STORE.hash_rihs01(BATTERY),
                    msgdef=STORE.generate_msgdef(BATTERY, ros_version=2)[0])
                service = writer.add_connection(SERVICE_TOPIC, GET_PARAMETERS_EVENT,
                    typestore=SERVICE_STORE, rihs01=GET_PARAMETERS_EVENT_HASH,
                    msgdef=SERVICE_STORE.generate_msgdef(EVENT_KEY, ros_version=2)[0])
                for stamp, connection, message, store, key in (
                        (stamps[0], ordinary, battery, STORE, BATTERY),
                        (stamps[1], service, request, SERVICE_STORE, EVENT_KEY),
                        (stamps[2], ordinary, battery, STORE, BATTERY),
                        (stamps[3], service, response, SERVICE_STORE, EVENT_KEY)):
                    writer.write(connection, stamp, store.serialize_cdr(message, key))
            text, report = convert(path)
            self.assertEqual(report["source_messages"], 4)
            self.assertEqual(report["service_retained_sources"], 2)
            self.assertEqual([item["source_message_index"] for item in report["service_sources"]],
                             [1, 3])
            self.assertIn("not a position in this service-only list", report["service_source_retention"])
            rows = list(csv.DictReader(io.StringIO(text)))
            service_rows = [row for row in rows if row["service_transaction_status"]]
            self.assertEqual(len(service_rows), 1)
            self.assertEqual(service_rows[0]["service_source_indexes"], "1|3")
            by_key = {item["source_message_index"]: item for item in report["service_sources"]}
            self.assertEqual(sorted(by_key), [1, 3])
            self.assertEqual(by_key[1]["decoded"]["request"][0]["names"], ["mixed"])
            self.assertEqual(by_key[3]["decoded"]["response"][0]["values"][0]["string_value"], "ok")
            self.assertEqual(by_key[1]["topic"], SERVICE_TOPIC)
            descriptor = json.loads(bytes.fromhex(service_rows[0]["source_record_hex"][4:]))["transaction"]
            self.assertEqual([item["capture_ns"] for item in descriptor], [stamps[1], stamps[3]])
            self.assertEqual([item["source_message_index"] for item in descriptor], [1, 3])
            self.assertNotEqual(descriptor[0]["capture_ns"],
                                int(service_rows[0]["record_time_us"]) * 1000)
            self.assertEqual([int(by_key[index]["capture_ns"]) for index in (1, 3)],
                             [stamps[1], stamps[3]])

    def test_saved_get_parameters_validates_every_event_and_refuses_contradictions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stamp"
            service_bag(path, [service_event(0, GID_A, 1, names=["a"], nanosec=10**9),
                               service_event(2, GID_A, 1, values=[parameter_value(type=1)])])
            with self.assertRaises(ValueError):
                convert(path)
            path = Path(directory) / "side"
            service_bag(path, [service_event(0, GID_A, 1, names=["a"]),
                               service_event(0, GID_A, 1, names=["a"],
                                             values=[parameter_value(type=1, bool_value=True)]),
                               service_event(2, GID_A, 1, values=[parameter_value(type=1, bool_value=True)])])
            text, report = convert(path)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["service_transaction_status"], "CONTRADICTORY_EVENT_SIDE")
            self.assertEqual(rows[0]["parameter_name_hex"], "")
            self.assertEqual(rows[0]["service_source_indexes"], "0|1|2")
            self.assertEqual(report["service_retained_sources"], 3)
            path = Path(directory) / "side2"
            service_bag(path, [service_event(2, GID_A, 1, names=["a"],
                                             values=[parameter_value(type=1, bool_value=True)])])
            rows = list(csv.DictReader(io.StringIO(convert(path)[0])))
            self.assertEqual(rows[0]["service_transaction_status"], "CONTRADICTORY_EVENT_SIDE")

    def test_saved_get_parameters_retention_stays_bounded_and_complete(self):
        """Retention is complete and accounted; the report bound is the existing 16MiB."""
        count = 2000
        messages = []
        for index in range(count // 2):
            messages.append(service_event(0, GID_A, index, names=[f"name_{index}"]))
            messages.append(service_event(2, GID_A, index,
                                          values=[parameter_value(type=2, integer_value=index)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many"
            service_bag(path, messages)
            text, report = convert(path)
            self.assertEqual(report["service_retained_sources"], count)
            self.assertEqual(report["service_transactions"], count // 2)
            self.assertEqual(len(report["service_sources"]), count)
            self.assertEqual(sorted(item["source_message_index"] for item in report["service_sources"]),
                             list(range(count)))
            self.assertLess(len(json.dumps(report).encode()), 16 * 1024 * 1024)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(len(rows), count // 2)
            self.assertEqual(rows[-1]["service_source_indexes"], f"{count - 2}|{count - 1}")
            self.assertEqual(bytes.fromhex(rows[-1]["parameter_name_hex"][4:]).decode(),
                             f"name_{count // 2 - 1}")

    def test_saved_get_parameters_rejects_bad_schema_and_sqlite_srv_limit(self):
        wrong = SERVICE_STORE.generate_msgdef(EVENT_KEY, ros_version=2)[0].replace(
            "int64 sequence_number", "int32 sequence_number")
        messages = [service_event(0, GID_A, 1, names=["a"]),
                    service_event(2, GID_A, 1, values=[parameter_value(type=1, bool_value=True)])]
        with tempfile.TemporaryDirectory() as directory:
            for name, options in (("hash", dict(hash_override="RIHS01_" + "0" * 64)),
                                  ("layout", dict(definition=wrong))):
                path = Path(directory) / name
                service_bag(path, messages, **options)
                with self.assertRaises((ValueError, AssertionError, struct.error), msg=name):
                    convert(path)
            path = Path(directory) / "sqlite"
            service_bag(path, messages, plugin=StoragePlugin.SQLITE3)
            with self.assertRaises((ValueError, AssertionError, KeyError)):
                convert(path)

    def test_px4_saved_cdr_reuses_battery_meanings_and_keeps_clocks_separate(self):
        """Two authored bags; the second was written only after the code was fixed."""
        first = [px4_sample(), px4_sample(stamp=200000, voltage=0., current=-1., discharged=-1.,
                                          remaining=-1., identifier=2, connected=False)]
        second = [px4_sample(stamp=900000, voltage=48.35, current=-1., discharged=1250.5,
                             remaining=.125, identifier=3, temperature=25.5),
                  px4_sample(stamp=900500, voltage=48.3, current=12.5, discharged=-1., remaining=-1.)]
        with tempfile.TemporaryDirectory() as directory:
            for name, messages, expected in (("first", first, [16.200000762939453, None]),
                                             ("second", second, [48.349998474121094, 48.29999923706055])):
                path = Path(directory) / name
                px4_bag(path, messages)
                text, report = convert(path, topics=[PX4_TOPIC])
                self.assertEqual((report["source_messages"], report["decoded_messages"],
                                  report["output_records"], report["clock"]), (2, 2, 2, "Unknown"))
                rows = list(csv.DictReader(io.StringIO(text)))
                for row, message, voltage in zip(rows, messages, expected):
                    self.assertEqual(row["battery_voltage_v"], "" if voltage is None else repr(voltage))
                    self.assertEqual(row["battery_consumed_j"], "")
                    self.assertEqual(row["px4_battery_source_stamp_us"], str(message.timestamp))
                    self.assertEqual(row["px4_battery_id_reported"], str(message.id))
                    self.assertEqual(row["px4_battery_connected_reported"], str(int(message.connected)))
                    self.assertNotEqual(row["px4_battery_source_stamp_us"], row["record_time_us"])
                    self.assertEqual(int(row["record_time_us"]) * 1000 // 1000,
                                     (PX4_CAPTURE_NS + rows.index(row) * 1000000) // 1000)
                    retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                    self.assertEqual(retained["topic"], PX4_TOPIC)
                    self.assertEqual(retained["type_hash"], PX4_BATTERY_HASH)
                    self.assertEqual(bytes.fromhex(retained["raw_cdr_hex"]),
                                     STORE.serialize_cdr(message, PX4_BATTERY))
                    self.assertEqual(len(bytes.fromhex(retained["raw_cdr_hex"])), 180)
                    self.assertEqual(retained["decoded"]["voltage_cell_v"], [4.050000190734863] * 14)
                    self.assertEqual(retained["decoded"]["current_filtered_a"], 3.4000000953674316)
                if name == "first":
                    missing = rows[1]
                    self.assertEqual([missing[f"battery_{q}"] for q in
                                      ("voltage_v", "current_a", "consumed_ah", "power_w", "remaining_fraction")],
                                     ["", "", "", "", ""])
                    self.assertEqual([missing[f"px4_battery_{q}_status"] for q in
                                      ("voltage", "current", "consumed", "remaining")],
                                     ["NOT_PROVIDED"] * 4)
                    self.assertEqual(rows[0]["px4_battery_voltage_status"], "REPORTED")
                    self.assertEqual(rows[0]["battery_consumed_ah"], "0.25")
                    self.assertEqual(rows[0]["battery_power_w"], repr(16.200000762939453 * 3.5))
                    self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:]))
                                     ["decoded"]["temperature"], "NaN")
                else:
                    self.assertEqual(rows[0]["battery_current_a"], "")
                    self.assertEqual(rows[0]["px4_battery_current_status"], "NOT_PROVIDED")
                    self.assertEqual(rows[0]["battery_power_w"], "")
                    self.assertEqual(rows[0]["battery_consumed_ah"], "1.2505")
                    self.assertEqual(rows[0]["battery_remaining_fraction"], "0.125")
                    self.assertEqual(rows[1]["battery_current_a"], "12.5")
                    self.assertEqual(rows[1]["px4_battery_consumed_status"], "NOT_PROVIDED")
                output = Path(directory) / f"{name}.csv"
                output.write_text(text)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(output), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], expected[0])
                    self.assertEqual(common["observations"][0]["fields"]["px4_battery_source_stamp_us"],
                                     messages[0].timestamp)
                    for record in common["observations"]:
                        self.assertEqual(record["clock_basis"], "Unknown")
                        self.assertIsNone(record["t_boot_us"])

    def test_px4_saved_cdr_preserves_either_stamp_magnitude_without_a_clock_basis(self):
        """UXRCE_DDS_SYNCT (default on) can offset the serialized stamp, so neither
        magnitude may be read as an epoch and neither may be adjusted."""
        relative, epoch_sized = 1500, 1789000000123456
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stamps"
            px4_bag(path, [px4_sample(stamp=relative), px4_sample(stamp=epoch_sized)])
            text, report = convert(path, topics=[PX4_TOPIC])
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual([row["px4_battery_source_stamp_us"] for row in rows],
                             [str(relative), str(epoch_sized)])
            self.assertEqual({row["px4_battery_source_stamp_basis"] for row in rows}, {PX4_STAMP_BASIS})
            self.assertIn("NOT_ESTABLISHED", PX4_STAMP_BASIS)
            for row in rows:
                self.assertNotEqual(row["px4_battery_source_stamp_us"], row["record_time_us"])
                retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                self.assertNotEqual(retained["capture_ns"], row["px4_battery_source_stamp_us"])
            self.assertEqual([row["record_time_us"] for row in rows],
                             [str((PX4_CAPTURE_NS + index * 1000000) // 1000) for index in range(2)])
            self.assertEqual(report["clock"], "Unknown")
            output = Path(directory) / "stamps.csv"
            output.write_text(text)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(output), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual([record["fields"]["px4_battery_source_stamp_us"]
                                  for record in common["observations"]], [relative, epoch_sized])
                for record in common["observations"]:
                    self.assertEqual(record["fields"]["px4_battery_source_stamp_basis"], PX4_STAMP_BASIS)
                    self.assertEqual(record["clock_basis"], "Unknown")
                    self.assertIsNone(record["t_boot_us"])
                    self.assertIsNone(record["wall_ms"])

    def test_px4_saved_cdr_rejects_layout_hash_corruption_and_unmeasurable_values(self):
        wrong = STORE.generate_msgdef(PX4_BATTERY, ros_version=2)[0].replace(
            "float32 discharged_mah", "float64 discharged_mah")
        with tempfile.TemporaryDirectory() as directory:
            for name, options in (("layout", dict(definition=wrong)),
                                  ("shorter_array", dict(definition=STORE.generate_msgdef(
                                      PX4_BATTERY, ros_version=2)[0].replace("float32[14]", "float32[13]"))),
                                  ("hash", dict(hash_override="RIHS01_" + "0" * 64)),
                                  ("corrupt", dict(corrupt=True))):
                path = Path(directory) / name
                px4_bag(path, [px4_sample()], **options)
                with self.assertRaises((ValueError, AssertionError, struct.error), msg=name):
                    convert(path, topics=[PX4_TOPIC])
            for name, message in (("nonfinite_voltage", px4_sample(voltage=float("nan"))),
                                  ("nonfinite_current", px4_sample(current=float("inf"))),
                                  ("remaining_above_one", px4_sample(remaining=2.)),
                                  ("negative_discharged", px4_sample(discharged=-5.)),
                                  ("negative_voltage", px4_sample(voltage=-1.))):
                path = Path(directory) / name
                px4_bag(path, [message])
                with self.assertRaises(ValueError, msg=name):
                    convert(path, topics=[PX4_TOPIC])

    def test_px4_saved_cdr_ten_thousand_records_through_existing_windows(self):
        """The existing 16MiB output bound is not relaxed; windows carry every record."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many"
            px4_bag(path, [px4_sample(stamp=1000 + index, voltage=16. + index / 10000.,
                                      discharged=float(index)) for index in range(10000)])
            with self.assertRaises(ValueError):
                convert(path, topics=[PX4_TOPIC])
            total, observed, stamps = 0, 0, []
            edges = [0, 2500, 5000, 7500, 10000]
            for start, stop in zip(edges, edges[1:]):
                text, report = convert(path, topics=[PX4_TOPIC], start_ns=PX4_CAPTURE_NS + start * 1000000,
                                       stop_ns=PX4_CAPTURE_NS + stop * 1000000)
                self.assertEqual(report["capture_window"]["selected_messages"], stop - start)
                rows = list(csv.DictReader(io.StringIO(text)))
                self.assertEqual(len(rows), stop - start)
                self.assertEqual(rows[0]["battery_consumed_ah"], repr(start / 1000))
                stamps += [int(row["px4_battery_source_stamp_us"]) for row in rows]
                total += report["output_records"]
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    window = Path(directory) / f"window{start}.csv"
                    window.write_text(text)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(window), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], stop - start)
                    self.assertEqual([record["fields"]["battery_consumed_ah"] for record in common["observations"]],
                                     [index / 1000 for index in range(start, stop)])
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    observed += common["main_rows"]
            self.assertEqual(total, 10000)
            self.assertEqual(stamps, [1000 + index for index in range(10000)])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.assertEqual(observed, 10000)


if __name__ == "__main__":
    unittest.main()
