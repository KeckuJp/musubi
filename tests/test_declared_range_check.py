"""Explicit saved expectations, not a sensor/physical validity verdict."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_declared_expectation_check as m
from scripts import convert_ardupilot_battery_csv as battery

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROFILE = ROOT / 'profiles/declared/ardupilot-battery/profile.toml'
PROFILE = ROOT / 'profiles/declared/declared-range-check/profile.toml'


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def spec(data, profile=None, **changes):
    value = dict(schema='musubi.declared-range.v1', source_sha256=digest(data),
        profile_sha256=digest(profile or SOURCE_PROFILE.read_bytes()), column='battery_voltage_v',
        minimum='-1.25', maximum='1.25', unit='V', clock_basis='boot_relative',
        subject_columns=['Instance'], subject_values=['0'])
    value.update(changes)
    return json.dumps(value).encode()


def convert(data, declaration=None, profile=None, **changes):
    return m.convert(data, clock_basis='boot_relative', value_meaning='saved BAT declared volts',
        subjects=('Instance',), time_column='TimeUS', range_spec=declaration or spec(data, **changes),
        range_profile=profile or SOURCE_PROFILE.read_bytes())


def common(test, text):
    reader = os.environ.get('MUSUBI_TELEMETRY_READER')
    if not reader:
        test.skipTest('actual common reader not configured')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'data.csv'; path.write_text(text)
        return json.loads(subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                         check=True, capture_output=True).stdout)


class DeclaredRangeTests(unittest.TestCase):
    def test_endpoints_missing_unqualified_and_exact_large_values(self):
        data = b'TimeUS,Instance,battery_voltage_v\n10,0,-2\n10,0,-1.25\n10,0,0\n10,0,1.25\n10,0,2\n10,0,\n10,1,NaN\n10,,0\n'
        output, report = convert(data)
        rows = list(csv.DictReader(io.StringIO(output)))
        expected = ['BELOW_DECLARED_RANGE', 'WITHIN_DECLARED_RANGE', 'WITHIN_DECLARED_RANGE',
            'WITHIN_DECLARED_RANGE', 'ABOVE_DECLARED_RANGE', 'NOT_REPORTED',
            'UNQUALIFIED_SUBJECT', 'UNQUALIFIED_SUBJECT']
        self.assertEqual([r['expectation_disposition'] for r in rows], expected)
        self.assertEqual([int(r['source_row_index']) for r in rows], list(range(8)))
        self.assertEqual(report['range_counts'], dict(within=3, below=1, above=1, not_reported=1, unqualified_subject=2))
        result = common(self, output)
        self.assertEqual(result['main_rows'], 8)
        self.assertTrue(all(o['clock_basis'] == 'Unknown' and o['anchor_unix_us'] is None for o in result['observations']))
        self.assertEqual(result['observations'][1]['fields']['range_minimum_text'], 'decimal:-1.25')
        self.assertIn('exact_decimal', result['profile_units']['range_minimum_text'])
        self.assertTrue(all(r['range_spec_sha256'] == digest(spec(data)) for r in rows))
        big = b'TimeUS,Instance,battery_voltage_v\n1,0,9007199254740992\n1,0,9007199254740993\n'
        output, _ = convert(big, minimum='9007199254740993', maximum='9007199254740993')
        fields = [o['fields'] for o in common(self, output)['observations']]
        self.assertEqual([f['expectation_disposition'] for f in fields], ['BELOW_DECLARED_RANGE', 'WITHIN_DECLARED_RANGE'])
        self.assertEqual(fields[1]['range_maximum_text'], 'decimal:9007199254740993')
        self.assertEqual(bytes.fromhex(fields[1]['reported_value_hex'][4:]).decode(), '9007199254740993')

    def test_adopted_battery_export_two_inputs_reach_common(self):
        for value, expected in (('12.0', 'WITHIN_DECLARED_RANGE'), ('9.5', 'BELOW_DECLARED_RANGE')):
            with self.subTest(value=value):
                raw = 'TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct\n100,0,' + value + ',2,1000,1,75\n'
                data = battery.convert(raw, 'ardupilot-bat').encode()
                output, report = convert(data, minimum='10', maximum='12')
                result = common(self, output)
                fields = result['observations'][0]['fields']
                self.assertEqual(fields['expectation_disposition'], expected)
                self.assertEqual(fields['source_sha256'], digest(data))
                self.assertEqual(fields['source_row_index'], 0)
                self.assertEqual(report['source_sha256'], digest(data))
                self.assertEqual(report['range_profile_id'], 'ardupilot_bat_finite_current_v1')

    def test_binding_numeric_shape_and_resource_refusals(self):
        data = b'TimeUS,Instance,battery_voltage_v\n1,0,1\n'
        changes = [dict(minimum='2', maximum='1'), dict(minimum='NaN'), dict(maximum='Inf'),
            dict(minimum=True), dict(maximum=1), dict(minimum='1e0'), dict(unit='mV'),
            dict(clock_basis='unknown'), dict(source_sha256='sha256:'+'0'*64),
            dict(profile_sha256='sha256:'+'0'*64), dict(subject_columns=[]),
            dict(subject_columns=['Other']), dict(subject_values=['']), dict(extra='not allowed'),
            dict(schema='wrong'), dict(column='missing'), dict(subject_values=[0])]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                convert(data, spec(data, **change))
        duplicate = spec(data)[:-1] + b',"unit":"V"}'
        with self.assertRaises(ValueError): convert(data, duplicate)
        for invalid in ('NaN', 'Inf', '1e0', 'true', '9'*31):
            bad = data.replace(b',1\n', (','+invalid+'\n').encode())
            with self.subTest(value=invalid), self.assertRaises(ValueError): convert(bad)
        for old, new in ((b'"V"', b'"mV"'), (b'telemetry_csv_us', b'wrong'), (b'TimeUS', b'wrong')):
            profile = SOURCE_PROFILE.read_bytes().replace(old, new)
            with self.assertRaises(ValueError): convert(data, spec(data, profile), profile)
        for option, value in [('RANGE_SPEC_LIMIT', 1), ('RANGE_PROFILE_LIMIT', 1), ('OUTPUT_LIMIT', 1), ('RANGE_REPORT_LIMIT', 1)]:
            with self.subTest(bound=option), patch.object(m, option, value), self.assertRaises(ValueError): convert(data)
        with self.assertRaises(ValueError):
            m.convert(data, clock_basis='boot_relative', value_meaning='saved volts', subjects=('Instance',),
                time_column='TimeUS', range_spec=spec(data), range_profile=SOURCE_PROFILE.read_bytes(),
                membership_column='battery_voltage_v', expected_values=('1',))

    def test_cli_original_artifacts_and_old_modes_unchanged(self):
        data = b'TimeUS,Instance,battery_voltage_v\n1,0,0\n'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'saved.csv'; source.write_bytes(data)
            declaration = root/'expected.json'; declaration.write_bytes(spec(data))
            output = root/'out'
            command = [os.sys.executable, str(ROOT/'scripts/convert_declared_expectation_check.py'),
                str(source), str(output), '--clock-basis', 'boot_relative', '--value-meaning', 'saved volts',
                '--subject-column', 'Instance', '--time-column', 'TimeUS', '--range-spec', str(declaration),
                '--range-profile', str(SOURCE_PROFILE)]
            success = subprocess.run(command, capture_output=True)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual((output/'source.csv').read_bytes(), data)
            self.assertEqual((output/'range-spec.json').read_bytes(), spec(data))
            self.assertEqual((output/'source-profile.toml').read_bytes(), SOURCE_PROFILE.read_bytes())
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        old_data = b'record_time_us,subject,a,b\n1,s,1,2\n2,s,2,2\n'
        baselines = [
            (dict(membership_column='a', expected_values=('1',)),
             'ac78d191a3cbfdf14516f0e95e585fa75486bf1fa3590b967c579e78438e11eb',
             '2c9e6ee9c8ff6a477308f49100163661017e610c972f367baa49f700e0a94877'),
            (dict(advance_column='a'),
             '871710d53aa58937b263a9632c26c51bd140e732614d3f7c329a94f54771ec93',
             '7a983c6741ea02f9e7b68e1dd36f07af65ac13da260945839df52e7e2af002ea'),
            (dict(difference_columns=('a', 'b'), tolerance='0.5'),
             'bf1fd1c5b469183e188049c8ae26a69e2f3d98033e68a47c896dea220d37c8a6',
             'a93cb748d0b73db7b49f4f28ddc4cd7f7fe9a54bec19171b79c670c36cb92716')]
        for options, csv_sha, report_sha in baselines:
            output, report = m.convert(old_data, clock_basis='unknown', value_meaning='authored declared numbers',
                subjects=('subject',), **options)
            self.assertEqual(hashlib.sha256(output.encode()).hexdigest(), csv_sha)
            self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(), report_sha)


if __name__ == '__main__':
    unittest.main()
