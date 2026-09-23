"""Authored saved samples through converters, shipped meanings and viewer validation.

These are offline specification checks; no field recording or device is involved.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PublicSavedSamples(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ.get('MUSUBI_PUBLIC_STEPS_ROOT', Path(__file__).resolve().parents[1]))
        self.ingest = os.environ.get('MUSUBI_RECORDED_OBSERVATIONS_BIN')
        self.viewer = os.environ.get('MUSUBI_REFERENCE_VIEWER_BIN')
        self.assertTrue(self.ingest and self.viewer, 'actual ingest and viewer binaries required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def run_ok(self, command):
        result = subprocess.run(command, cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout

    def report(self, csv, case, stem='profile'):
        profile_path = self.root / 'profiles/recorded' / case / (stem + '.json')
        meaning_path = self.root / 'profiles/meanings' / case / (stem + '.json')
        profile = json.loads(profile_path.read_text())
        meanings = json.loads(meaning_path.read_text())
        raw = self.run_ok([self.ingest, str(csv), str(profile_path), str(meaning_path),
                           'authored-source', '1700000000123', '--allow-equal-time'])
        report = json.loads(raw)
        self.assertEqual(report['schema'], 'recorded-observation-report/v1')
        self.assertEqual(bytes.fromhex(report['source_hex']), csv.read_bytes())
        self.assertEqual(len(report['observations']), 1)
        saved = self.work / 'observations.json'
        saved.write_bytes(raw)
        self.run_ok([self.viewer, '--report', '--in', str(saved), '--check'])
        obs = report['observations'][0]
        fields = {field['name']: field for field in obs['fields']}
        # Every profile annotation survives, including those too long for a meaning alias.
        for name, field in fields.items():
            if name in profile.get('units', {}):
                self.assertEqual(field['source_unit'], profile['units'][name], name)
            if name in meanings['fields']:
                declaration = meanings['fields'][name]
                self.assertEqual(field['meaning'], declaration['meaning'], name)
                self.assertEqual(field['unit'], declaration['unit'], name)
                self.assertEqual(field['basis'], 'declared', name)
        self.assertIsNone(obs['wall_ms'])
        return obs, fields

    def test_gcs_attitude_sample_uses_shipped_meanings_without_rescaling(self):
        source = self.root / 'examples/saved-inputs/authored-gcs.csv'
        csv = self.work / 'attitude.csv'
        self.run_ok([sys.executable, str(self.root / 'scripts/convert_gcs_tlog_csv.py'),
            str(source), str(csv), '--source-format', 'missionplanner-1.3.82-tlog-csv',
            '--selected-message', 'mavlink_attitude_t', '--decimal-separator', 'dot'])
        obs, fields = self.report(csv, 'gcs-csv-reuse', 'attitude-profile')
        self.assertEqual(obs['clock_basis'], 'boot_relative')
        self.assertEqual(obs['t_boot_us'], 1234000)
        self.assertEqual(fields['reported_pitch_rad']['value'], -0.5)
        self.assertEqual(fields['reported_roll_rad']['value'], 0.25)
        self.assertEqual(fields['source_sysid']['value'], 10)
        self.assertEqual(fields['reported_pitch_rad']['meaning'], 'reported_pitch_rad')
        self.assertEqual(fields['source_local_time_text']['value'], '2026-09-20T11:22:33.456')

    def test_rtklib_sample_keeps_unknown_clock_signed_cross_term_and_long_annotation(self):
        source = self.root / 'examples/saved-inputs/authored-rtklib.pos'
        converted = self.work / 'converted'
        self.run_ok([sys.executable, str(self.root / 'scripts/convert_rtklib_solution.py'),
                     str(source), str(converted)])
        obs, fields = self.report(converted / 'observations.csv', 'rtklib-solution')
        self.assertEqual(obs['clock_basis'], 'unknown')
        self.assertEqual(fields['solution_time_system']['value'], 'GPST')
        self.assertEqual(fields['height_reference']['value'], 'ellipsoidal')
        self.assertEqual(fields['solution_quality_name']['value'], 'FIX')
        self.assertAlmostEqual(fields['sdne_signed_m']['value'], -0.4)
        self.assertEqual(fields['source_sha256']['value'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertGreater(len(fields['height_m']['source_unit']), 256)
        self.assertIsNone(fields['height_m']['meaning'])
        self.assertIsNone(fields['height_m']['unit'])
        self.assertEqual(fields['height_m']['value'], 40.125)


if __name__ == '__main__':
    unittest.main()
