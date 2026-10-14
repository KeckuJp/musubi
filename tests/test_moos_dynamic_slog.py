import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_imc_sensors import convert_slog

ROOT = Path(__file__).resolve().parents[1]


def sample(first='PRESSURE', second='OTHER'):
    return (f'%% (1) TIME\n%% (2) {first}\n%% (3) {second}\n'
        '%% TIME DYNAMIC_0 DYNAMIC_1\n0 NaN NaN\n'
        f'%% TIME {first} DYNAMIC_1\n1 10 NaN\n'
        f'%% TIME {first} {second}\n2 20 30\n').encode()


class DynamicSlogTests(unittest.TestCase):
    def test_two_update_orders_common_without_retroactive_mapping(self):
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        for data, expected, absent in ((sample(), [None, 1000., 2000.], 1),
                (sample('OTHER', 'PRESSURE'), [None, None, 3000.], 2)):
            output, report = convert_slog(data, 'PRESSURE', 'hPa', 123, dynamic_columns=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([float(r['pressure_pa']) if r['pressure_pa'] else None for r in rows], expected)
            self.assertEqual(report['not_yet_declared_records'], absent)
            self.assertEqual(report['selected_records'], 3 - absent)
            self.assertEqual(rows[0]['value_disposition'], 'WITHHELD_NOT_YET_DECLARED')
            self.assertEqual(bytes.fromhex(rows[0]['source_label_hex'][4:]).decode(), 'TIME DYNAMIC_0 DYNAMIC_1')
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / 'in.slog', Path(directory) / 'output'
                source.write_bytes(data)
                subprocess.run([os.sys.executable, '-m', 'scripts.convert_imc_sensors', str(source), str(target),
                    '--source-format', 'moos-dynamic-slog-f56c', '--variable', 'PRESSURE', '--unit', 'hPa',
                    '--capture-time-us', '123'], check=True, capture_output=True, cwd=ROOT)
                self.assertEqual((target / 'source.slog').read_bytes(), data)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/imc-sensors/alog-profile.toml'),
                    str(target / 'observations.csv'), '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 3)
                self.assertEqual([r['fields']['pressure_pa'] for r in common['observations']], expected)

    def test_refusals_and_default_parity(self):
        data = sample()
        with self.assertRaises(ValueError): convert_slog(data, 'PRESSURE', 'hPa', 123)
        for bad in (data.replace(b'0 NaN NaN', b'0 5 NaN'),
                data.replace(b'%% TIME PRESSURE DYNAMIC_1', b'%% TIME WRONG DYNAMIC_1'),
                data.replace(b'%% TIME PRESSURE DYNAMIC_1', b'%% TIME DYNAMIC_0 OTHER'),
                data.replace(b'DYNAMIC_0', b'DYNAMIC_3'),
                data.replace(b'%% TIME PRESSURE OTHER', b'%% TIME OTHER PRESSURE'),
                data.replace(b'%% TIME PRESSURE OTHER', b'%% TIME PRESSURE PRESSURE'),
                data.replace(b'2 20 30', b'2 20'), data.replace(b'2 20 30', b'0 20 30'),
                data.replace(b'%% TIME DYNAMIC_0 DYNAMIC_1\n', b''),
                data.replace(b'%% TIME PRESSURE OTHER\n2 20 30\n', b'')):
            with self.subTest(bad=bad[-70:]), self.assertRaises(ValueError):
                convert_slog(bad, 'PRESSURE', 'hPa', 123, dynamic_columns=True)
        fixed = b'%% (1) TIME\n%% (2) PRESSURE\n%% TIME PRESSURE\n0 10\n'
        self.assertEqual(convert_slog(fixed, 'PRESSURE', 'hPa', 123),
                         convert_slog(fixed, 'PRESSURE', 'hPa', 123, dynamic_columns=False))
