"""Authored bags and CDR, not physical recordings; shared semantics reused."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest

import numpy as np
from rosbags.rosbag2 import Writer
from rosbags.rosbag2.writer import StoragePlugin
from scripts.convert_rosbag_observations import BATTERY, IMU, STORE, convert, decode, NAV2_FEEDBACK, NAV2_DEFINITIONS

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


class RosbagObservationTests(unittest.TestCase):









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
            # Unknown fields survive without being promoted into measured values.
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



if __name__ == "__main__":
    unittest.main()
