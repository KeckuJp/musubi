"""Authored saved ASCII windows, not vessel or sensor recordings."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_nmea_ascii_window import convert_path
from tests.test_nmea_wind_conversion import sentence

ROOT = Path(__file__).resolve().parents[1]


class NmeaWindowTests(unittest.TestCase):
    def test_cli_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.txt'
            source.write_text(sentence('YXMTW,1,C')+'\n')
            cmd = [sys.executable, str(ROOT / 'scripts/convert_nmea_ascii_window.py'),
                   str(source), str(root/'out'), '--kind', 'water-dpt-mtw',
                   '--start-record', '0', '--stop-record', '1']
            subprocess.run(cmd, check=True, capture_output=True)
            before = (root/'out/converted.csv').read_bytes()
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
            self.assertEqual((root/'out/converted.csv').read_bytes(), before)

    def common(self, output, directory):
        path = directory / 'output.csv'
        path.write_text(output)
        return json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
            str(ROOT / 'profiles/declared/nmea-wind/profile.toml'),
            str(path), '--allow-equal-time'], check=True, capture_output=True).stdout)['observations']

    def test_large_mixed_source_and_small_water_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = directory / 'source.txt'
            prefix = (b'outside-not-decoded-' + b'x' * 32750 + b'\n') * 520
            records = [sentence('YXMTW,20,C'), sentence('WIMWV,90,R,10,M,A'),
                       sentence('IIDPT,12.5,-1,50'), sentence('GPXYZ,unknown')]
            raw = prefix + ('\r\n'.join(records)).encode()
            self.assertGreater(len(raw), 16 * 1024**2)
            path.write_bytes(raw)
            output, report = convert_path(path, start_record=520, stop_record=524, kind='marine-mixed')
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([int(r['source_record_index']) for r in rows], [520, 521, 522])
            self.assertEqual(int(rows[0]['source_record_byte_offset']), len(prefix))
            self.assertEqual(report['source_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual((report['source_records'], report['excluded_records']), (524, 520))
            self.assertEqual(report['semantics']['unsupported'][0]['raw'], records[3])
            common = self.common(output, directory)
            self.assertEqual(len(common), 3)
            self.assertAlmostEqual(common[0]['fields']['water_temperature_k'], 293.15)
            self.assertEqual(common[1]['fields']['wind_speed_m_s'], 10)
            self.assertEqual(common[2]['fields']['depth_below_transducer_m'], 12.5)
            self.assertTrue(all(o['clock_basis'] == 'Unknown' and o['anchor_unix_us'] is None for o in common))
            path.write_text(sentence('IIDPT,0,1,100')+'\n'+sentence('YXMTW,-2,C')+'\n')
            output, report = convert_path(path, start_record=0, stop_record=2, kind='water-dpt-mtw')
            common = self.common(output, directory)
            self.assertEqual(len(common), 2)
            self.assertEqual(common[0]['fields']['depth_below_transducer_m'], 0)
            self.assertAlmostEqual(common[1]['fields']['water_temperature_k'], 271.15)

    def test_bad_selection_checksum_cut_and_expected_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'in.txt'
            valid = sentence('YXMTW,1,C')
            path.write_text(valid+'\n')
            for start, stop in ((0,0),(-1,1),(0,2),(False,1)):
                with self.assertRaises(ValueError):
                    convert_path(path, start_record=start, stop_record=stop, kind='water-dpt-mtw')
            with self.assertRaises(ValueError):
                convert_path(path, start_record=0, stop_record=1, kind='water-dpt-mtw',
                             expected_size=path.stat().st_size, expected_sha256='0'*64)
            for broken in (valid[:-1], valid[:-2]+'ZZ', valid+'\rbroken'):
                path.write_text(broken)
                with self.assertRaises(ValueError):
                    convert_path(path, start_record=0, stop_record=1, kind='water-dpt-mtw')


if __name__ == '__main__':
    unittest.main()
