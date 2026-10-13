"""Authored fixed ROS2 covariance reports; no physical accuracy claim."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import numpy as np
from tests.test_rosbag_observations import bag, samples, ROOT, StoragePlugin
from scripts.convert_rosbag_observations import convert, imu_quantities, plain


class ImuCovarianceTests(unittest.TestCase):
    def test_two_bags_to_common_and_default_parity(self):
        for plugin, scale in ((StoragePlugin.SQLITE3, 1.), (StoragePlugin.MCAP, 2.)):
            _, imu = samples()
            imu.orientation_covariance = np.array([1., .25, 0., .25, 2., 0., 0., 0., 3.]) * scale
            imu.linear_acceleration_covariance[0] = -1
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "bag"
                bag(source, plugin, messages=[imu])
                before, _ = convert(source)
                output, report = convert(source, imu_reported_covariance=True)
                row = next(csv.DictReader(io.StringIO(output)))
                old = next(csv.DictReader(io.StringIO(before)))
                self.assertTrue(all(row[k] == v for k, v in old.items()))
                self.assertEqual(float(row["orientation_covariance_1_rad2"]), .25 * scale)
                self.assertEqual(row["linear_acceleration_covariance_0_m2_s4"], "")
                self.assertEqual(report["decoded_messages"], 1)
                path = Path(directory) / "out.csv"; path.write_text(output)
                reader = os.environ.get("MUSUBI_TELEMETRY_READER")
                if not reader: self.skipTest("common reader not configured")
                common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"), str(path)], check=True, capture_output=True).stdout)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["orientation_covariance_1_rad2"], .25 * scale)
                self.assertIsNone(fields["linear_acceleration_covariance_0_m2_s4"])
                for name, unit in (("orientation", "rad2"), ("angular_velocity", "rad2_s2"), ("linear_acceleration", "m2_s4")):
                    for index in range(9):
                        key = f"{name}_covariance_{index}_{unit}"
                        expected = None if name == "linear_acceleration" else float(getattr(imu, name + "_covariance")[index])
                        self.assertEqual(fields[key], expected)
                        self.assertIn(f"row{index // 3}_column{index % 3}", common["profile_units"][key])

    def test_missing_unknown_and_malformed(self):
        _, imu = samples(); record = plain(imu)
        result = imu_quantities(record, reported_covariance=True)
        self.assertIsNone(result["orientation_covariance_0_rad2"])
        self.assertEqual(result["orientation_covariance_status"], "UNKNOWN")
        record["angular_velocity_covariance"][0] = float("nan")
        with self.assertRaises(ValueError): imu_quantities(record, reported_covariance=True)
        record["angular_velocity_covariance"] = [1.]
        with self.assertRaises(ValueError): imu_quantities(record, reported_covariance=True)
        with self.assertRaises(ValueError): convert(Path("absent"), imu_reported_covariance=True, ros1=True)
