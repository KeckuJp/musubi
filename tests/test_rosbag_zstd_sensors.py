"""Authored file-zstd storage -> unchanged compact sensor semantics/common."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from rosbags.rosbag2 import Writer
from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
from scripts import convert_rosbag_zstd_window as window
from scripts import convert_rosbag_observations as ros
from tests.test_rosbag_observations import bag, samples

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"
SCAN = "sensor_msgs/msg/LaserScan"


def scan(distance):
    return ros.STORE.types[SCAN](samples()[0].header, 0., 1., .5, .01, .1, .2, 10.,
                                np.array([distance, 2., float("inf")], dtype=np.float32),
                                np.array([], dtype=np.float32))


def saved(path, records, large=False):
    writer = Writer(path, version=9)
    writer.set_compression(CompressionMode.FILE, CompressionFormat.ZSTD)
    with writer:
        connections = {}
        for topic, stamp, message in records:
            if topic not in connections:
                connections[topic] = writer.add_connection(topic, message.__msgtype__, typestore=ros.STORE)
            writer.write(connections[topic], stamp, ros.STORE.serialize_cdr(message, message.__msgtype__))
        other = writer.add_connection("/outside", "std_msgs/msg/String", typestore=ros.STORE)
        writer.write(other, 0, b"not-cdr" + (b"x" * (17 * 1024**2) if large else b""))
        writer.write(other, 2000000, b"opaque-selected")


class ZstdSensors(unittest.TestCase):
    def common(self, output, directory):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not supplied")
        path = Path(directory) / "common.csv"
        path.write_text(output)
        return json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                         check=True, capture_output=True).stdout)

    def test_long_scan_adjacent_equal_capture_and_source_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bag"
            saved(path, [("/scan", 1000000, scan(1.)), ("/scan", 2000000, scan(3.)),
                         ("/scan", 2000000, scan(4.))], large=True)
            member = next(path.glob("*.zstd")).read_bytes()
            outputs = [window.convert(path, start_ns=a, stop_ns=b, scan_compact=True)
                       for a, b in ((1000000, 2000000), (2000000, 3000000))]
            all_ranges = []
            for output, report in outputs:
                fields = [o["fields"] for o in self.common(output, tmp)["observations"]]
                self.assertEqual(len(fields), report["output_records"])
                all_ranges.extend(f["range_m"] for f in fields)
                self.assertEqual([f["scan_ray_index"] for f in fields], list(range(3)) * (len(fields) // 3))
                self.assertTrue(all(f["scan_intensity_status"] == "NOT_PROVIDED" for f in fields))
                for source in report["scan_sources"]:
                    raw = bytes.fromhex(source["raw_cdr_hex"])
                    self.assertEqual(source["source_cdr_sha256"], hashlib.sha256(raw).hexdigest())
                    related = [f for f in fields if f["source_message_index"] == source["source_message_index"]]
                    self.assertEqual(len(related), 3)
                    self.assertEqual(related[-1]["range_status"], "OUT_OF_RANGE_REPORTED")
                    self.assertEqual(related[-1]["source_cdr_sha256"], "sha256:" + source["source_cdr_sha256"])
                    self.assertEqual(source["decoded"]["header"]["stamp"]["nanosec"], 123456789)
                binding = report["file_zstd_window"]
                self.assertGreater(binding["expanded_bytes"], 16 * 1024**2)
                self.assertEqual(binding["members"][0]["compressed_sha256"], hashlib.sha256(member).hexdigest())
                self.assertEqual(binding["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(report["capture_window"]["outside_window"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual({o["clock_basis"] for o in self.common(output, tmp)["observations"]}, {"Unknown"})
            self.assertEqual(all_ranges, [1., 2., None, 3., 2., None, 4., 2., None])
            self.assertEqual(len(outputs[1][1]["unsupported_records"]), 1)
            self.assertIn(b"opaque-selected".hex(), json.dumps(outputs[1][1]["unsupported_records"]))

    def test_cloud_big_endian_missing_coordinate_and_cli(self):
        types = ros.STORE.types
        fields = [types["sensor_msgs/msg/PointField"](name, i * 8, 8, 1)
                  for i, name in enumerate(("x", "y", "z", "future"))]
        data = struct.pack(">dddddddd", 1., -2., 3., 9., float("nan"), 5., 6., 8.)
        cloud = types[ros.POINT_CLOUD](samples()[0].header, 1, 2, fields, True, 32, 64,
                                      np.frombuffer(data, dtype=np.uint8), False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bag"; saved(path, [("/cloud", 1000000, cloud)])
            output, report = window.convert(path, start_ns=1000000, stop_ns=2000000,
                topics=["/cloud"], pointcloud_compact=True, pointcloud_unit="m")
            common = self.common(output, tmp)
            self.assertEqual(common["main_rows"], 2)
            rows = [o["fields"] for o in common["observations"]]
            self.assertEqual(rows[0]["point_x_m"], 1.)
            self.assertEqual(rows[0]["point_y_m"], -2.)
            self.assertIsNone(rows[1]["point_x_m"])
            self.assertEqual(rows[1]["point_z_m"], 6.)
            self.assertEqual(report["topic_selection"]["declared_excluded_messages"], 2)
            raw = bytes(ros.STORE.serialize_cdr(cloud, ros.POINT_CLOUD))
            self.assertEqual(bytes.fromhex(report["pointcloud_sources"][0]["raw_cdr_hex"]), raw)
            args = [sys.executable, str(ROOT / "scripts/convert_rosbag_zstd_window.py"), str(path),
                    str(Path(tmp) / "out"), "--start-ns", "1000000", "--stop-ns", "2000000",
                    "--topic", "/cloud", "--pointcloud-unit", "m", "--pointcloud-compact"]
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual((Path(tmp) / "out/observations.csv").read_bytes().decode(), output)
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)

    def test_invalid_selectors_and_selected_layout_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bag"; saved(path, [("/scan", 1000000, scan(1.))])
            for options in ({"pointcloud_compact": True}, {"pointcloud_unit": "m"},
                            {"pointcloud_compact": True, "pointcloud_unit": "cm"},
                            {"scan_compact": 1}, {"pointcloud_compact": "true"},
                            {"scan_compact": True, "pointcloud_compact": True, "pointcloud_unit": "m"},
                            {"scan_compact": True, "topics": ["/outside"]}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    window.convert(path, start_ns=1000000, stop_ns=3000000, **options)
            malformed = scan(1.); malformed.intensities = np.array([1.], dtype=np.float32)
            other = Path(tmp) / "invalid"; saved(other, [("/scan", 1000000, malformed)])
            with self.assertRaises(ValueError):
                window.convert(other, start_ns=1000000, stop_ns=2000000, scan_compact=True)


if __name__ == "__main__":
    unittest.main()
