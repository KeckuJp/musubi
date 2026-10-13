import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

import numpy as np
from rosbags.typesys import Stores, get_typestore
from scripts.convert_rosbag_observations import convert, STORE, HASHES, POINT_CLOUD
from tests.test_rosbag_observations import legacy_bag

ROOT = Path(__file__).resolve().parents[1]
SCAN = 'sensor_msgs/msg/LaserScan'


def message(store, kind, value=1.25, count=3):
    types = store.types
    header = types['std_msgs/msg/Header'](types['builtin_interfaces/msg/Time'](7, 500000), 'reported_frame')
    if kind == SCAN:
        return types[kind](header, -.5, .5, .25, .01, .1, .2, 10.,
            np.full(count, value, dtype=np.float32), np.array([], dtype=np.float32))
    fields = [types['sensor_msgs/msg/PointField'](name, index * 4, 7, 1)
              for index, name in enumerate(('x', 'y', 'z'))]
    raw = struct.pack('<fff', value, -2.5, .75) * count
    return types[kind](header, 1, count, fields, False, 12, 12 * count,
        np.frombuffer(raw, dtype=np.uint8), True)


def selected(kind):
    return (dict(scan_compact=True, legacy_schema='laserscan-humble-equivalent') if kind == SCAN
        else dict(pointcloud_compact=True, pointcloud_unit='m', legacy_schema='pointcloud2-humble-equivalent'))


class LegacySensorTests(unittest.TestCase):
    def test_two_legacy_stores_reuse_compact_meaning_and_common(self):
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        for distro, schema, kind, count, value in (
                (Stores.ROS2_FOXY, 1, SCAN, 4000, 1.25),
                (Stores.ROS2_GALACTIC, 2, POINT_CLOUD, 2, 3.5)):
            store = get_typestore(distro)
            for name in (kind, 'std_msgs/msg/Header', 'builtin_interfaces/msg/Time'):
                self.assertEqual(store.fielddefs[name], STORE.fielddefs[name])
            raw = store.serialize_cdr(message(store, kind, value, count), kind)
            with tempfile.TemporaryDirectory() as directory:
                path = legacy_bag(Path(directory) / 'bag', [raw], schema=schema, msgtype=kind)
                output, report = convert(path, **selected(kind))
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), count)
                descriptor = json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:]))
                self.assertIn('PRODUCER_AND_ITS_BINARY_UNVERIFIED', descriptor['type_hash_basis'])
                self.assertEqual(descriptor['selected_schema'], selected(kind)['legacy_schema'])
                key = 'range_m' if kind == SCAN else 'point_x_m'
                self.assertEqual(float(rows[0][key]), value)
                self.assertEqual(report['decoded_messages'], 1)
                retained = report['scan_sources' if kind == SCAN else 'pointcloud_sources'][0]
                self.assertEqual(bytes.fromhex(retained['raw_cdr_hex']), raw)
                out = Path(directory) / 'out.csv'
                out.write_text(output)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                    str(out), '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], count)
                self.assertEqual(common['observations'][0]['fields'][key], value)
                self.assertEqual(common['platform_domain'], 'Unknown')

    def test_default_hash_and_selection_refusals(self):
        store = get_typestore(Stores.ROS2_FOXY)
        raw = store.serialize_cdr(message(store, SCAN), SCAN)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = legacy_bag(root / 'valid', [raw], msgtype=SCAN)
            for settings in ({'scan_compact': True}, {'legacy_schema': 'laserscan-humble-equivalent'},
                    dict(scan_compact=True, legacy_schema='battery-humble-equivalent'),
                    dict(pointcloud_compact=True, pointcloud_unit='m', legacy_schema='laserscan-humble-equivalent')):
                with self.subTest(settings=settings), self.assertRaises(ValueError): convert(path, **settings)
            wrong = legacy_bag(root / 'wrong', [raw], msgtype=SCAN, declared_hash='RIHS01_'+'0'*64)
            with self.assertRaises(ValueError): convert(wrong, **selected(SCAN))
            broken = legacy_bag(root / 'truncated', [raw[:-1]], msgtype=SCAN)
            with self.assertRaises((ValueError, struct.error)): convert(broken, **selected(SCAN))
