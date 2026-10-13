"""Caller-declared saved age, not physical synchronization or producer freshness."""
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

from scripts import check_saved_observation_age as age
from scripts import check_saved_age_window as window_age
from scripts.convert_recorded_angles_csv import convert_selected_csv_path
from scripts import convert_ardupilot_battery_csv as battery

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROFILE = ROOT/'profiles/declared/ardupilot-battery/profile.toml'
PROFILE = ROOT/'profiles/declared/saved-observation-age/profile.toml'


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def spec(data, profile=None, **changes):
    value = dict(schema='musubi.saved-age.v1', source_sha256=sha(data),
                 profile_sha256=sha(profile or SOURCE_PROFILE.read_bytes()),
                 time_column='TimeUS', time_unit='us_since_boot', clock_basis='boot_relative',
                 subject_columns=['Instance'], subject_values=['0'],
                 clock_column='clock_domain', clock_domain='boot-session-a',
                 evaluation_time_us='1000', maximum_age_us='100')
    value.update(changes)
    return json.dumps(value).encode()


def convert(data, profile=None, **changes):
    return age.convert(data, spec(data, profile, **changes), profile or SOURCE_PROFILE.read_bytes())


class SavedAgeTests(unittest.TestCase):
    def test_large_window_reuses_age_and_keeps_original_accounting(self):
        header = b'TimeUS,Instance,clock_domain,padding\n'
        data = header + (b'bad,0,boot-session-a,' + b'x'*1024 + b'\n')*17000
        data += b'1001,0,boot-session-a,reuse\n,0,boot-session-a,missing\n899,0,boot-session-a,older\n'
        self.assertGreater(len(data), 16*1024**2)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.csv'; source.write_bytes(data)
            output, report, selected = window_age.convert_path(source, spec(data), SOURCE_PROFILE.read_bytes(),
                start_record=17000, stop_record=17003)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['age_disposition'] for r in rows],
                ['SOURCE_TIME_AFTER_EVALUATION', 'SOURCE_TIME_NOT_REPORTED', 'STALE_BY_DECLARATION'])
            self.assertEqual([r['source_row_index'] for r in rows], ['17000','17001','17002'])
            self.assertTrue(all(r['source_sha256'] == sha(data) for r in rows))
            self.assertTrue(all(r['spec_sha256'] == sha(spec(data)) for r in rows))
            self.assertEqual((report['source_records'], report['selected_records'], report['excluded_records']),
                (17003, 3, 17000))
            self.assertEqual(sum(report['age_counts'].values()), 3)
            self.assertIn('NOT_SEMANTICALLY', report['excluded_values'])
            self.assertEqual(self.common(output)['main_rows'], 3)
            self.assertNotIn(b'bad', selected)
            # Same implementation, independent layout/window and nonmatching clock.
            other = header + b'900,0,boot-session-a,first\n900,0,other,second\n'
            source.write_bytes(other)
            output, report, _ = window_age.convert_path(source, spec(other), SOURCE_PROFILE.read_bytes(),
                start_record=1, stop_record=2)
            self.assertEqual(report['age_counts'], {'UNQUALIFIED_CLOCK': 1})
            self.assertEqual(self.common(output)['main_rows'], 1)

    def test_window_refusals_and_shared_default_time_guard(self):
        data = b'TimeUS,Instance,clock_domain\n900,0,boot-session-a\n,0,boot-session-a\n'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.csv'; source.write_bytes(data)
            with self.assertRaises(ValueError):
                window_age.convert_path(source, spec(data, source_sha256='sha256:'+'0'*64),
                    SOURCE_PROFILE.read_bytes(), start_record=0, stop_record=1)
            for start, stop in ((True,2), (0,3), (0,200001)):
                with self.assertRaises(ValueError):
                    window_age.convert_path(source, spec(data), SOURCE_PROFILE.read_bytes(),
                        start_record=start, stop_record=stop)
            for extra in ({}, {'validate_time': 'false'}):
                with self.assertRaises(ValueError):
                    convert_selected_csv_path(source, time_field='TimeUS', required_fields=(),
                        transform=lambda text:text, label='guard', start_record=0, stop_record=1, **extra)
            malformed = data.replace(b'900,0,', b'invalid,0,')
            source.write_bytes(malformed)
            with self.assertRaises(ValueError):
                window_age.convert_path(source, spec(malformed), SOURCE_PROFILE.read_bytes(),
                    start_record=0, stop_record=1)

    def common(self, data):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rows.csv'; path.write_text(data)
            result = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_boundary_missing_future_and_unqualified(self):
        data = (b'TimeUS,Instance,clock_domain,value,unknown\n'
                b'899,0,boot-session-a,7,retained\n900,0,boot-session-a,7,retained\n'
                b'1001,0,boot-session-a,7,retained\n,0,boot-session-a,7,retained\n'
                b'900,,boot-session-a,7,retained\n900,1,boot-session-a,7,retained\n'
                b'900,0,,7,retained\n900,0,other-boot,7,retained\n')
        output, report = convert(data)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r['age_disposition'] for r in rows], [
            'STALE_BY_DECLARATION', 'WITHIN_DECLARED_AGE', 'SOURCE_TIME_AFTER_EVALUATION',
            'SOURCE_TIME_NOT_REPORTED', 'SUBJECT_NOT_REPORTED', 'UNQUALIFIED_SUBJECT',
            'CLOCK_NOT_REPORTED', 'UNQUALIFIED_CLOCK'])
        self.assertEqual([r['age_us_exact'] for r in rows[:3]], ['integer:101', 'integer:100', 'integer:-1'])
        self.assertEqual(report['source_records'], 8)
        self.assertEqual(sum(report['counts'].values()), 8)
        common = self.common(output)
        self.assertEqual(common['main_rows'], 8)
        self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
        self.assertTrue(all(r['evaluation_time_us'] == '1000' for r in rows))
        self.assertEqual(common['observations'][0]['fields']['source_time_us_exact'], 'integer:899')

    def test_independent_clock_and_large_exact_integer(self):
        profile = SOURCE_PROFILE.read_bytes().replace(b'us_since_boot', b'us').replace(b'boot_relative', b'unknown')
        data = b'TimeUS,Instance,clock_domain\n9007199254740993,0,capture-clock-b\n9007199254740992,0,capture-clock-b\n'
        output, report = convert(data, profile, time_unit='us', clock_basis='unknown',
                                 clock_domain='capture-clock-b', evaluation_time_us='9007199254740993', maximum_age_us='0')
        common = self.common(output)
        fields = [o['fields'] for o in common['observations']]
        self.assertEqual([f['age_disposition'] for f in fields], ['WITHIN_DECLARED_AGE', 'STALE_BY_DECLARATION'])
        self.assertEqual(fields[0]['source_time_us_exact'], 'integer:9007199254740993')
        self.assertEqual(fields[1]['age_us_exact'], 'integer:1')
        self.assertEqual(report['clock_comparison'], 'CALLER_DECLARED_NOT_PHYSICAL_SYNCHRONIZATION')

    def test_strict_contract_and_cli_preserves_originals(self):
        data = b'TimeUS,Instance,clock_domain\n900,0,boot-session-a\n'
        for changes in ({'evaluation_time_us': True}, {'maximum_age_us':'-1'}, {'evaluation_time_us':'1e3'},
                        {'maximum_age_us':100}, {'clock_domain':'unknown'}, {'time_unit':'ms'},
                        {'source_sha256':'sha256:'+'0'*64}, {'clock_basis':'utc'}, {'extra':True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): convert(data, **changes)
        for bad in (b'TimeUS,Instance,clock_domain\nNaN,0,boot-session-a\n',
                    b'TimeUS,TimeUS,Instance,clock_domain\n1,2,0,boot-session-a\n',
                    b'TimeUS,Instance\n900,0\n'):
            with self.assertRaises(ValueError): convert(bad)
        declaration = spec(data)
        with self.assertRaises(ValueError):
            age.convert(data, declaration[:-1]+b',"maximum_age_us":"100"}', SOURCE_PROFILE.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source=base/'source.csv'; source.write_bytes(data)
            policy=base/'spec.json'; policy.write_bytes(declaration)
            args=[os.sys.executable, str(ROOT/'scripts/check_saved_observation_age.py'), str(source),
                  str(policy), str(SOURCE_PROFILE), str(base/'out')]
            subprocess.run(args, capture_output=True, check=True)
            self.assertEqual((base/'out/source.csv').read_bytes(), data)
            self.assertEqual((base/'out/age-spec.json').read_bytes(), declaration)
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)

    def test_adopted_export_reuse_and_bounds(self):
        raw = ('TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct,clock_domain\n'
               '900,0,12,2,1000,1,75,boot-session-a\n'
               '1000,0,12,2,1000,1,75,boot-session-a\n')
        data = battery.convert(raw, 'ardupilot-bat').encode()
        output, _ = convert(data, maximum_age_us='50')
        common = self.common(output)
        self.assertEqual([o['fields']['age_disposition'] for o in common['observations']],
                         ['STALE_BY_DECLARATION', 'WITHIN_DECLARED_AGE'])
        # Equal battery values are intentionally irrelevant: age, not frozen-value inference.
        for bound in ('INPUT_LIMIT', 'OUTPUT_LIMIT', 'ROW_LIMIT'):
            with patch.object(age, bound, 0), self.assertRaises(ValueError): convert(data)
        profile = b'profile_id="x"\nversion="1"\nformat="telemetry_csv_us"\nfields=true\n'
        with self.assertRaises(ValueError): convert(data, profile)


if __name__ == '__main__':
    unittest.main()
