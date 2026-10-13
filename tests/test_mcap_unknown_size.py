"""Authored MCAP with fixed-library zstd no-FCS; no real bag or ROS runtime."""
import base64
import csv
import io
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rosbags.rosbag2 import Writer
from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
from rosbags.rosbag2.writer import StoragePlugin
import zstandard

from scripts import convert_rosbag_observations as module
from tests import test_rosbag_observations as fixtures

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"


def make_bag(path, *, voltage=24.5, large=False, compressor=None):
    battery, imu = fixtures.samples()
    writer = Writer(path, version=9, storage_plugin=StoragePlugin.MCAP)
    writer.set_compression(CompressionMode.STORAGE, CompressionFormat.ZSTD)
    with writer:
        writer.storage.compressor = compressor or zstandard.ZstdCompressor(write_content_size=False).compress
        power = writer.add_connection("/battery", module.BATTERY, typestore=module.STORE)
        motion = writer.add_connection("/imu", module.IMU, typestore=module.STORE)
        if large:
            kind = "std_msgs/msg/String"
            bulk = writer.add_connection("/bulk", kind, typestore=module.STORE)
            noise = base64.b64encode(random.Random(37).randbytes(768 * 1024)).decode()
            filler = module.STORE.serialize_cdr(module.STORE.types[kind](noise), kind)
            for index in range(24):
                writer.write(bulk, 500000 + index, filler)
        for stamp, value in ((4000, voltage), (4000, voltage - 1), (5000, voltage + 1), (9000, 12.)):
            battery.voltage = value
            writer.write(power, stamp, module.STORE.serialize_cdr(battery, module.BATTERY))
        writer.write(motion, 4500, module.STORE.serialize_cdr(imu, module.IMU))
    return path


def chunk_reference(raw):
    """Small authored single-chunk fixture; index/body offsets, not a runtime parser."""
    offset = int.from_bytes(raw[-28:-20], "little")
    while offset < len(raw) - 37:
        length = int.from_bytes(raw[offset + 1:offset + 9], "little")
        if raw[offset] == 8:
            body = offset + 9
            start = int.from_bytes(raw[body + 16:body + 24], "little")
            return body + length - 8, start + 25, start + 53
        offset += 9 + length
    raise AssertionError("fixture requires chunk index")


class McapUnknownSize(unittest.TestCase):
    def test_two_inputs_including_large_bag_reach_common_without_fcs(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for index, voltage in enumerate((24.5, 47.25)):
                path = make_bag(folder / f"bag{index}", voltage=voltage, large=index == 0)
                if index == 0:
                    self.assertGreater(sum(p.stat().st_size for p in path.iterdir()), module.LIMIT)
                    with self.assertRaisesRegex(ValueError, "bag too large"):
                        module.convert(path)
                output, report = module.convert(path, start_ns=4000, stop_ns=5001, topics=["/battery"])
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), 3)
                window = report["capture_window"]["mcap"]
                self.assertEqual(window["index_declared_selected_messages"], report["source_messages"])
                self.assertEqual(report["source_messages"], 3)
                self.assertLessEqual(window["selected_chunk_bytes"], module.LIMIT)
                self.assertLessEqual(window["selected_compressed_bytes"], module.LIMIT)
                self.assertIn("chunk CRC", window["not_verified"])
                target = folder / "output.csv"
                target.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(target), "--allow-equal-time"],
                                                   check=True, capture_output=True).stdout)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["main_rows"], 3)
                observations = common["observations"]
                self.assertEqual([o["fields"]["battery_voltage_v"] for o in observations],
                                 [voltage, voltage - 1, voltage + 1])
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
                self.assertTrue(all(o["fields"]["battery_current_a"] == -2 for o in observations))
                self.assertTrue(all(o["fields"]["battery_temperature_k"] is None for o in observations))
                self.assertIn("K_converted_from", common["profile_units"]["battery_temperature_k"])
                before, count = module.convert(path, start_ns=4000, stop_ns=5000, topics=["/battery"])
                self.assertEqual(count["source_messages"], 2)
                self.assertEqual(len(list(csv.DictReader(io.StringIO(before)))), 2)

    def test_unknown_size_declared_caps_are_enforced_before_storage_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = make_bag(Path(directory) / "bag")
            source = next(path.glob("*.mcap"))
            raw = source.read_bytes()
            index_size, record_size, payload = chunk_reference(raw)
            actual = int.from_bytes(raw[index_size:index_size + 8], "little")
            self.assertGreater(actual, 0)
            self.assertEqual(zstandard.get_frame_parameters(raw[payload:]).content_size,
                             zstandard.CONTENTSIZE_UNKNOWN)
            for declared in (actual - 1, actual + 1, 0, module.LIMIT + 1):
                changed = bytearray(raw)
                for offset in (index_size, record_size):
                    changed[offset:offset + 8] = declared.to_bytes(8, "little")
                source.write_bytes(changed)
                with self.subTest(declared=declared), patch.object(module, "Reader") as reader:
                    with self.assertRaises(ValueError) as error:
                        module.convert(path, start_ns=4000, stop_ns=5001)
                    reader.assert_not_called()
                    if declared == actual - 1:
                        self.assertIsInstance(error.exception.__cause__, zstandard.ZstdError)
                        self.assertIn("expanded size", str(error.exception))

    def test_single_frame_completion_window_and_no_trailing_data(self):
        codec = zstandard.ZstdCompressor(write_content_size=False)
        def huge_window(data):
            frame = bytearray(codec.compress(data))
            self.assertFalse(frame[4] & 32)  # no single-segment frame; byte5 is window descriptor
            frame[5] = 0x88  # 128MiB window declaration, above adopted16MiB
            return bytes(frame)
        variants = {
            "trailing": lambda data: codec.compress(data) + b"extra",
            "concatenated": lambda data: codec.compress(data) + codec.compress(b"second"),
            "truncated": lambda data: codec.compress(data)[:-1],
            "window": huge_window,
        }
        with tempfile.TemporaryDirectory() as directory:
            for name, compressor in variants.items():
                path = make_bag(Path(directory) / name, compressor=compressor)
                with self.subTest(name=name), patch.object(module, "Reader") as reader:
                    with self.assertRaises(ValueError):
                        module.convert(path, start_ns=4000, stop_ns=5001)
                    reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
