"""Authored wire codes; not receiver captures or an independent decoder oracle."""
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.convert_nmea_wind import convert
from tests import test_ais_recorded as fixtures
from tests.test_nmea_wind_conversion import sentence

ROOT = Path(__file__).resolve().parents[1]


def authored(code, kind=1, mmsi=123456789):
    # Set the literal signed wire code, not pyais.encode's degrees/min API.
    raw = fixtures.AisRecordedTests().report(msg_type=kind, mmsi=mmsi)
    fields = raw[1:raw.index('*')].split(',')
    bits = ''.join(f'{(ord(c)-48 if ord(c)<88 else ord(c)-56):06b}' for c in fields[5])
    bits = bits[:42] + f'{code & 255:08b}' + bits[50:]
    values = [int(bits[i:i+6], 2) for i in range(0, len(bits), 6)]
    fields[5] = ''.join(chr(v + (48 if v < 40 else 56)) for v in values)
    return '!' + sentence(','.join(fields))[1:]


class AisRotTests(unittest.TestCase):
    def test_signed_codes_and_sentinels_reach_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not reader:
            self.skipTest('actual common reader not configured')
        codes = [-128, -127, -126, -125, -10, 0, 10, 125, 126, 127]
        for mmsi in (123456789, 234567890):
            source = [authored(code, 1 + i % 3, mmsi) for i, code in enumerate(codes)]
            output, report = convert('\n'.join(source), 'ais-class-a', ais_rot=True)
            self.assertEqual((report['input_records'], report['ais_records']), (10, 10))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'out.csv'; path.write_text(output)
                common = json.loads(subprocess.run([reader,
                    str(ROOT / 'profiles/declared/nmea-wind/profile.toml'),
                    str(path), '--allow-equal-time'], capture_output=True, check=True).stdout)
            self.assertEqual(common['main_rows'], 10)
            self.assertIn('inverse', common['profile_units']['ais_rot_rad_s'])
            for code, row, raw in zip(codes, common['observations'], source):
                fields = row['fields']
                self.assertEqual(row['clock_basis'], 'Unknown')
                self.assertEqual(fields['ais_rot_code'], code)
                self.assertEqual(fields['ais_mmsi_reported'], mmsi)
                self.assertEqual(bytes.fromhex(fields['source_sentence_hex'][4:]).decode(), raw)
                if abs(code) <= 125:
                    expected = math.copysign((abs(code) / 4.733)**2, code) * math.pi / 10800
                    self.assertAlmostEqual(fields['ais_rot_rad_s'], expected)
                    self.assertEqual(fields['ais_rot_disposition'], 'INVERSE_QUANTIZED_ESTIMATE')
                else:
                    self.assertIsNone(fields['ais_rot_rad_s'])
                    self.assertEqual(fields['ais_rot_disposition'],
                        {-128: 'NO_INFORMATION', -127: 'NO_TURN_INDICATOR', 127: 'NO_TURN_INDICATOR',
                         -126: 'RANGE_END_MAGNITUDE_UNRESOLVED', 126: 'RANGE_END_MAGNITUDE_UNRESOLVED'}[code])
                self.assertEqual(fields['ais_rot_direction'], 'UNKNOWN' if code == -128 else
                    'ZERO_QUANTIZED' if code == 0 else 'RIGHT' if code > 0 else 'LEFT')

    def test_mixed_static_and_unsupported_accounting(self):
        static = fixtures.AisRecordedTests().static()
        source = '\n'.join([authored(10), *static, sentence('GPXYZ,future'), sentence('WIMWV,0,R,2,M,A')])
        output, report = convert(source, 'marine-mixed', static_pairs=True, ais_rot=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report['input_records'], report['selected_records'],
                          report['consumed_records'], report['unsupported_records']), (5, 3, 4, 1))
        self.assertEqual(report['unsupported'][0]['line'], 4)
        self.assertEqual(rows[1]['ais_rot_code'], '')
        self.assertEqual(rows[1]['ais_rot_disposition'], 'NOT_IN_THIS_MESSAGE')
        self.assertEqual(rows[2]['ais_rot_disposition'], '')
        self.assertEqual([row['source_line'] for row in rows], ['1', '2', '5'])

    def test_rejections_and_cli(self):
        raw = authored(10)
        for args in [dict(ais_rot='true'), dict(kind='wind', ais_rot=True),
                     dict(kind='water-dpt-mtw', ais_rot=True)]:
            with self.assertRaises(ValueError): convert(raw, **args)
        for source in [raw[:-1], raw + '\n\n', raw.replace('!', '$', 1), '# comment\n' + raw]:
            with self.assertRaises(ValueError): convert(source, 'ais-class-a', ais_rot=True)
        with patch('scripts.convert_nmea_wind.math.radians', return_value=float('nan')):
            with self.assertRaisesRegex(ValueError, 'finite.*ROT'):
                convert(raw, 'ais-class-a', ais_rot=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'; path.write_text(raw)
            out = Path(tmp) / 'out'
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_nmea_wind.py'),
                str(path), str(out), '--kind', 'ais-class-a', '--ais-rot'], check=True, capture_output=True)
            self.assertIn('ais_rot_rad_s', (out / 'converted.csv').read_text())

    def test_default_exact_bytes_unchanged(self):
        fx = fixtures.AisRecordedTests()
        cases = [('ais-class-a', {}, [fx.report(turn=-20)],
                  '5cf27255cee61f8e838bafebe8452a094a7b547902175d77403b4ed069e891b3',
                  '28851b55eef52c03197d5f1b96cfd8b80fc8dfb5ab9bff887bf597e4a2a7502a'),
                 ('marine-mixed', {}, [fx.report(turn=-20)],
                  '0a9bb380c9e64e8c47ceefdd1d2a929185ba1efbecca1bff4a4396858a084fc6',
                  '6d69982682a87a6b3592f84c14209eb4a92d6242a3c1b852226f88c8ea131fa4'),
                 ('ais-class-a', {'static_pairs': True}, fx.static(),
                  '6268f87fa769099a02628e6b2fc17615ce59ea0b8f35c35ed72efd6c8e98a013',
                  '7921c842efc87c4476f5cd7c93a901b259e0ada7d2e6cc44e81ffbcb659d81cc')]
        for kind, kwargs, lines, csv_sha, report_sha in cases:
            output, report = convert('\n'.join(lines), kind, **kwargs)
            self.assertEqual(hashlib.sha256(output.encode()).hexdigest(), csv_sha)
            self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(), report_sha)
