"""Saved compressed storage -> adopted IMU covariance, authored inputs only."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from tests.test_rosbag_zstd_sensors import saved, scan, PROFILE, ROOT
from tests.test_rosbag_observations import samples
from scripts import convert_rosbag_zstd_window as window


class ZstdCovarianceTests(unittest.TestCase):
    def test_scan_policy_cli_reaches_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bag'; saved(path, [('/scan', 1000000, scan(1.))])
            subprocess.run([os.sys.executable, str(ROOT/'scripts/convert_rosbag_zstd_window.py'),
                str(path), str(Path(directory)/'cli'), '--start-ns', '1000000', '--stop-ns', '2000000',
                '--scan-compact', '--scan-unavailable-infinity'], capture_output=True, check=True)
            common = json.loads(subprocess.run([reader, str(PROFILE),
                str(Path(directory)/'cli/observations.csv'), '--allow-equal-time'],
                capture_output=True, check=True).stdout)
            self.assertEqual(common['main_rows'], 3)
            fields = common['observations'][-1]['fields']
            self.assertIsNone(fields['range_m'])
            self.assertEqual(fields['range_status'], 'UNAVAILABLE_RETURN_REPORTED')

    def test_finite_ros2_signature_forwarding(self):
        excluded = {'path', 'ros1', 'point_window'}
        inner = inspect.signature(window.ros.convert).parameters
        outer = inspect.signature(window.convert).parameters
        self.assertEqual(set(inner) - excluded, set(outer) - {'path'})
        values = dict(joint_kinds={'wheel':'angular'}, stamped_motion_role='reported_estimate',
            list_compact='actions', allow_missing_joint_values=True,
            scan_unavailable_infinity=True, interface_kinds={'wheel.velocity':'angular_velocity'},
            legacy_schema=next(iter(window.ros.LEGACY_SCHEMA_CHOICES)), nav2_through_poses=True,
            outdoornav_control_state=True, autoware_gear=True, tf_relation=True,
            husarion_estop=True, husarion_model=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bag'; saved(path, [('/imu', 1000000, samples()[1])])
            with self.assertRaises(TypeError):
                window.convert(path, start_ns=1000000, stop_ns=2000000,
                    point_window=(0, 1))
            for name, value in values.items():
                with self.subTest(option=name), patch.object(window.ros, 'convert', return_value=('header\n', {})) as call:
                    window.convert(path, start_ns=1000000, stop_ns=2000000, **{name:value})
                    self.assertEqual(call.call_args.kwargs[name], value)
            with self.assertRaises(ValueError):
                window.convert(path, start_ns=1000000, stop_ns=2000000,
                    list_compact='actions', imu_reported_covariance=True)

    def test_missing_voltage_window_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        for voltage, large in ((24., True), (48., False)):
            with self.subTest(voltage=voltage), tempfile.TemporaryDirectory() as directory:
                reset, _ = samples(); reset.voltage = float('nan')
                normal, _ = samples(); normal.voltage = voltage
                path = Path(directory)/'bag'
                saved(path, [('/battery', 1000000, reset), ('/battery', 2000000, normal)], large=large)
                options = dict(start_ns=1000000, stop_ns=3000000, topics=['/battery'])
                with self.assertRaises(ValueError): window.convert(path, **options)
                output, report = window.convert(path, allow_missing_voltage=True, **options)
                csv = Path(directory)/'out.csv'; csv.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(csv)],
                    capture_output=True, check=True).stdout)
                self.assertEqual(common['main_rows'], 2)
                first, second = [o['fields'] for o in common['observations']]
                self.assertIsNone(first['battery_voltage_v'])
                self.assertEqual(first['battery_voltage_disposition'], 'UNAVAILABLE_SOURCE_NAN')
                self.assertEqual(second['battery_voltage_v'], voltage)
                self.assertEqual(report['file_zstd_window']['output_sha256'], hashlib.sha256(output.encode()).hexdigest())
                if large: self.assertGreater(report['file_zstd_window']['expanded_bytes'], 16*1024**2)
                subprocess.run([os.sys.executable, str(ROOT/'scripts/convert_rosbag_zstd_window.py'),
                    str(path), str(Path(directory)/'cli'), '--start-ns', '1000000', '--stop-ns', '3000000',
                    '--topic', '/battery', '--allow-missing-voltage'], capture_output=True, check=True)
                self.assertEqual((Path(directory)/'cli/observations.csv').read_text(), output)

    def test_two_saved_bags_common_and_default_parity(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        for scale, large in ((1., True), (2., False)):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as directory:
                _, imu = samples()
                imu.orientation_covariance = np.eye(3).flatten() * scale
                imu.linear_acceleration_covariance[0] = -1
                path = Path(directory)/'bag'
                saved(path, [('/imu', 1000000, imu)], large=large)
                options = dict(start_ns=1000000, stop_ns=2000000, topics=['/imu'])
                old = window.convert(path, **options)
                self.assertEqual(old, window.convert(path, imu_reported_covariance=False, **options))
                output, report = window.convert(path, imu_reported_covariance=True, **options)
                member = next(path.glob('*.zstd')).read_bytes()
                binding = report['file_zstd_window']
                self.assertEqual(binding['members'][0]['compressed_sha256'], hashlib.sha256(member).hexdigest())
                self.assertEqual(binding['output_sha256'], hashlib.sha256(output.encode()).hexdigest())
                if large: self.assertGreater(binding['expanded_bytes'], 16*1024**2)
                self.assertEqual(report['capture_window']['outside_window'], 'NOT_DECODED_OR_VALIDATED')
                csv = Path(directory)/'out.csv'; csv.write_text(output)
                run = subprocess.run([reader, str(PROFILE), str(csv)], capture_output=True, check=True)
                common = json.loads(run.stdout)
                self.assertEqual(common['main_rows'], 1)
                fields = common['observations'][0]['fields']
                self.assertEqual(fields['orientation_covariance_0_rad2'], scale)
                self.assertEqual(fields['orientation_covariance_1_rad2'], 0)
                self.assertEqual(fields['angular_velocity_covariance_0_rad2_s2'], 1)
                self.assertIsNone(fields['linear_acceleration_covariance_0_m2_s4'])
                self.assertEqual(common['observations'][0]['clock_basis'], 'Unknown')
                args = [os.sys.executable, str(ROOT/'scripts/convert_rosbag_zstd_window.py'),
                        str(path), str(Path(directory)/'cli'), '--start-ns', '1000000',
                        '--stop-ns', '2000000', '--topic', '/imu', '--imu-reported-covariance']
                subprocess.run(args, check=True, capture_output=True)
                self.assertEqual((Path(directory)/'cli/observations.csv').read_text(), output)

    def test_invalid_flags_before_read(self):
        for kwargs in ({'imu_reported_covariance':1},
                       {'allow_missing_voltage':1},
                       {'allow_missing_voltage':True, 'scan_compact':True},
                       {'imu_reported_covariance':True, 'scan_compact':True},
                       {'imu_reported_covariance':True, 'pointcloud_compact':True, 'pointcloud_unit':'m'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                window.convert(Path('not-opened'), start_ns=1, stop_ns=2, **kwargs)
