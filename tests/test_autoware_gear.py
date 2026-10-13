"""Authored schema examples, not measured transmission/vehicle evidence."""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from rosbags.rosbag2.writer import StoragePlugin
from scripts import convert_rosbag_observations as ros
from tests.test_rosbag_observations import bag, samples

ROOT = Path(__file__).resolve().parents[1]


class GearTests(unittest.TestCase):
    def test_fixed_schema_codes_to_common_and_other_container_reuse(self):
        self.assertEqual(ros.STORE.hash_rihs01(ros.GEAR_REPORT), ros.GEAR_HASH)
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        for plugin, codes in ((StoragePlugin.SQLITE3, list(range(25)) + [255]),
                              (StoragePlugin.MCAP, [22, 20, 2, 1, 0, 254])):
            stamp = ros.STORE.types['builtin_interfaces/msg/Time'](3, 987654321)
            messages = [ros.STORE.types[ros.GEAR_REPORT](stamp, code) for code in codes]
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); source = root / 'bag'
                bag(source, plugin=plugin, messages=messages)
                output, report = ros.convert(source, autoware_gear=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report['decoded_messages'], len(codes))
                self.assertEqual(len(report['unsupported_records']), 1)
                self.assertEqual([int(r['vehicle_gear_code']) for r in rows], codes)
                self.assertEqual(rows[-1]['vehicle_gear_disposition'], 'UNKNOWN_ENUM_RETAINED')
                self.assertEqual(rows[-1]['vehicle_gear_name'], '')
                self.assertEqual(rows[codes.index(0)]['vehicle_gear_disposition'], 'NONE_REPORTED_NOT_GEAR_SELECTION')
                self.assertEqual(rows[codes.index(20)]['vehicle_gear_name'], 'REVERSE')
                self.assertEqual(rows[0]['vehicle_report_stamp_us'], '3987654')
                retained = json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:]))
                self.assertEqual(retained['decoded']['stamp']['nanosec'], 987654321)
                out = root / 'output.csv'; out.write_text(output)
                result = subprocess.run([reader, str(ROOT / 'profiles/declared/autoware-gear/profile.toml'),
                                         str(out), '--allow-equal-time'], capture_output=True, check=True)
                common = json.loads(result.stdout)
                self.assertEqual(common['main_rows'], len(codes))
                self.assertEqual(common['platform_domain'], 'Unknown')
                self.assertEqual(common['observations'][-1]['fields']['vehicle_gear_code'], codes[-1])
                self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))

    def test_refusals_and_default_nonselection(self):
        message = ros.STORE.types[ros.GEAR_REPORT](samples()[0].header.stamp, 2)
        for bad in (True, -1, 256, 2.0, '2', None):
            with self.assertRaises(ValueError):
                ros.gear_quantities({'stamp': {'sec': 2, 'nanosec': 3}, 'report': bad})
        with self.assertRaises(ValueError):
            ros.gear_quantities({'stamp': {'sec': 2, 'nanosec': 1000000000}, 'report': 2})
        for extra in ({'ros1': True}, {'pointcloud_compact': True}, {'legacy_schema': 'battery-humble-equivalent'}):
            with self.assertRaises(ValueError): ros.convert(Path('not-read'), autoware_gear=True, **extra)
        for options in ({'hash_override': 'RIHS01_' + '0' * 64}, {'corrupt': True}):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'bag'; bag(path, messages=[message], **options)
                with self.assertRaises((ValueError, AssertionError, struct.error)):
                    ros.convert(path, autoware_gear=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'bag'
            bag(source, messages=[samples()[0], message])
            default, report = ros.convert(source)
            self.assertNotIn('vehicle_gear_code', default.splitlines()[0])
            self.assertEqual(report['decoded_messages'], 1)
            self.assertEqual(len(report['unsupported_records']), 2)
            args = [os.sys.executable, str(ROOT / 'scripts/convert_rosbag_observations.py'),
                    str(source), str(root / 'out'), '--autoware-gear']
            subprocess.run(args, capture_output=True, check=True)
            self.assertTrue((root / 'out/observations.csv').exists())
            rejected = subprocess.run(args + ['--metadata-only'], capture_output=True)
            self.assertEqual(rejected.returncode, 2)
            self.assertNotIn(b'Traceback', rejected.stderr)
