"""Authored robotnik_msgs/State records; no ROS subscription, device or health verdict."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_robotnik_dashboard_state import SCHEMA_COMMIT, convert

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/convert_robotnik_dashboard_state.py'
PROFILE = ROOT / 'profiles/declared/robotnik-dashboard-reuse/profile.toml'
CAPTURE = 1758326400000000


def record(code=300, description='READY', *, desired=40.0, real=39.5, topic='/rb_base/state',
           commit=SCHEMA_COMMIT, capture=CAPTURE, extra=None):
    body = {'capture_time_us': capture, 'schema_commit': commit, 'topic': topic,
            'state': {'state': code, 'desired_freq': desired, 'real_freq': real,
                      'state_description': description}}
    if extra:
        body.update(extra)
    return json.dumps(body)


def log(*records):
    return "\n".join(records) + "\n"


class RobotnikDashboardStateTests(unittest.TestCase):
    def rows(self, text):
        output, report = convert(text)
        return list(csv.DictReader(io.StringIO(output))), report

    def refuses(self, text):
        with self.assertRaises((ValueError, json.JSONDecodeError, KeyError)):
            convert(text)

    def test_both_producer_conventions_reach_common_observations(self):
        text = log(record(300, 'READY', desired=40.0, real=39.5),
                   record(400, 'EMERGENCY_STATE', desired=10.0, real=9.75, topic='/arm/state'))
        rows, report = self.rows(text)
        self.assertEqual([r['reported_state_code'] for r in rows], ['300', '400'])
        self.assertEqual([r['reported_state_name'] for r in rows], ['READY', 'EMERGENCY'])
        self.assertEqual([r['state_description_convention'] for r in rows],
                         ['SHORT_CPP', 'LONG_PYTHON'])
        self.assertEqual({r['state_description_matches_code'] for r in rows}, {'MATCHES_CODE'})
        self.assertEqual([r['dashboard_highlight'] for r in rows], ['GREEN_READY', 'RED_EMERGENCY'])
        self.assertEqual([r['desired_loop_frequency_hz'] for r in rows], ['40.0', '10.0'])
        self.assertEqual([r['real_loop_frequency_hz'] for r in rows], ['39.5', '9.75'])
        self.assertEqual({r['real_loop_frequency_status'] for r in rows}, {'REPORTED'})
        self.assertEqual(rows[1]['rcomponent_topic_hex'], 'hex:' + b'/arm/state'.hex())
        self.assertEqual(rows[0]['loop_frequency_basis'],
                         'COMPONENT_SELF_MEASURED_LOOP_RATE_NOT_DASHBOARD_ARRIVAL_RATE')
        self.assertEqual(report['records'], 2)
        self.assertEqual(report['live_subscription_records'], 'NOT_OBSERVED')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'state.jsonl').write_text(text)
            result = subprocess.run([os.sys.executable, str(SCRIPT), str(base / 'state.jsonl'),
                                     str(base / 'out')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((base / 'out/observations.csv').read_text(),
                             convert(text)[0])
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(PROFILE), str(base / 'out/observations.csv'), '--allow-equal-time'],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 2)
                self.assertEqual(common['platform_domain'], 'Unknown')
                fields = common['observations'][0]['fields']
                self.assertEqual(fields['real_loop_frequency_hz'], 39.5)
                self.assertEqual(fields['reported_state_name'], 'READY')
                self.assertEqual(fields['state_time_basis'],
                                 'CALLER_CAPTURE_CLOCK_MESSAGE_CARRIES_NO_STAMP_OR_FRAME')

    def test_the_dashboard_highlights_only_four_names(self):
        text = log(record(100, 'INIT'), record(200, 'STANDBY_STATE'), record(500, 'FAILURE'),
                   record(600, 'SHUTDOWN_STATE'))
        rows, report = self.rows(text)
        self.assertEqual([r['dashboard_highlight'] for r in rows],
                         ['GRAY_INIT', 'DARK_YELLOW_STANDBY', 'DEFAULT_NO_HIGHLIGHT',
                          'DEFAULT_NO_HIGHLIGHT'])
        self.assertEqual([r['reported_state_name'] for r in rows],
                         ['INIT', 'STANDBY', 'FAILURE', 'SHUTDOWN'])
        self.assertEqual({r['state_description_matches_code'] for r in rows}, {'MATCHES_CODE'})
        self.assertEqual(report['highlighted_rows'], 2)

    def test_unknown_codes_and_disagreeing_text_stay_accounted(self):
        text = log(record(700, 'UNKNOWN'),          # C++ has no case for 700; its default is UNKNOWN
                   record(700, 'UNKNOWN_STATE'),    # Python's default for the same code
                   record(999, 'UNKNOWN_STATE'),    # a code neither table names
                   record(300, 'STANDBY'),          # text disagreeing with its own code
                   record(300, 'ALL SYSTEMS NOMINAL'))
        rows, report = self.rows(text)
        self.assertEqual([r['reported_state_name'] for r in rows],
                         ['UNKOWN_STATE_700', 'UNKOWN_STATE_700', 'UNKNOWN_RETAINED',
                          'READY', 'READY'])
        self.assertEqual([r['state_description_convention'] for r in rows],
                         ['SHORT_CPP', 'LONG_PYTHON', 'LONG_PYTHON', 'SHORT_CPP',
                          'UNRECOGNISED_TEXT'])
        self.assertEqual([r['state_description_matches_code'] for r in rows],
                         ['MATCHES_CODE', 'MATCHES_CODE', 'MATCHES_CODE',
                          'DISAGREES_WITH_CODE', 'NOT_A_PRODUCER_NAME'])
        self.assertEqual(rows[4]['reported_state_description_hex'],
                         'hex:' + b'ALL SYSTEMS NOMINAL'.hex())
        self.assertEqual([r['dashboard_highlight'] for r in rows],
                         ['DEFAULT_NO_HIGHLIGHT', 'DEFAULT_NO_HIGHLIGHT', 'DEFAULT_NO_HIGHLIGHT',
                          'DARK_YELLOW_STANDBY', 'DEFAULT_NO_HIGHLIGHT'])
        self.assertEqual((report['unknown_state_codes'], report['descriptions_disagreeing_with_code'],
                          report['unrecognised_descriptions']), (1, 1, 1))

    def test_a_nonpositive_reported_rate_is_retained_not_sanitised(self):
        rows, report = self.rows(log(record(real=0.0), record(real=-1.5), record(real=0.25)))
        self.assertEqual([r['real_loop_frequency_status'] for r in rows],
                         ['NONPOSITIVE_RETAINED', 'NONPOSITIVE_RETAINED', 'REPORTED'])
        self.assertEqual([r['real_loop_frequency_hz'] for r in rows], ['0.0', '-1.5', '0.25'])
        self.assertEqual(report['nonpositive_real_frequencies'], 2)

    def test_an_unread_declaration_is_reported_never_dropped(self):
        text = log(record(extra={'battery': {'voltage': 48.0}, 'operator_note': 'shift change'}),
                   record())
        rows, report = self.rows(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(report['unread_declarations'], ['battery', 'operator_note'])
        self.assertNotIn('voltage', "".join(rows[0].keys()))

    def test_malformed_missing_and_unqualified_input_refuses(self):
        self.refuses(log(record(commit='0' * 40)))
        self.refuses(log(record(commit='5c44131ba354962322e969a4b0fe0b3026659686')))
        self.refuses(log(json.dumps({'capture_time_us': CAPTURE, 'topic': '/s',
                                     'state': {'state': 300, 'desired_freq': 1.0,
                                               'real_freq': 1.0, 'state_description': 'READY'}})))
        for bad in ({'state': 300.5}, {'state': True}, {'desired_freq': 'fast'},
                    {'real_freq': float('inf')}, {'state_description': 3},
                    {'desired_freq': 4e38}):
            body = json.loads(record())
            body['state'].update(bad)
            self.refuses(json.dumps(body) + "\n")
        for drop in ('state', 'desired_freq', 'real_freq', 'state_description'):
            body = json.loads(record())
            body['state'].pop(drop)
            self.refuses(json.dumps(body) + "\n")
        body = json.loads(record())
        body['state']['extra_field'] = 1  # State has exactly four fields; a fifth is not this type
        self.refuses(json.dumps(body) + "\n")
        self.refuses(log(record(capture=-1)))
        self.refuses(log(record(capture=1.5)))
        self.refuses(log(record(topic='')))
        self.refuses(log('{"capture_time_us": 1'))
        self.refuses(log(json.dumps([record()])))
        self.refuses('')
        self.refuses('\n\n')
        self.refuses(log(record()) + "\x00")
        self.refuses(log(json.dumps(json.loads(record())) + ' ' * 30000))

    def test_a_duplicated_key_never_silently_picks_one_value(self):
        conflicting = ('{"capture_time_us": %d, "schema_commit": "%s", "topic": "/s",'
                       ' "state": {"state": 300, "state": 400, "desired_freq": 40.0,'
                       ' "real_freq": 39.5, "state_description": "READY"}}' % (CAPTURE, SCHEMA_COMMIT))
        self.assertEqual(json.loads(conflicting)['state']['state'], 400)  # what it would have been
        self.refuses(log(conflicting))
        for duplicated in ('"capture_time_us": 1, "capture_time_us": 2',
                           '"topic": "/a", "topic": "/b"',
                           '"schema_commit": "x", "schema_commit": "%s"' % SCHEMA_COMMIT):
            body = record()
            self.refuses(log(body[0] + duplicated + ',' + body[1:]))

    def test_the_digest_binds_the_exact_bytes_including_crlf(self):
        records = [record(300, 'READY'), record(400, 'EMERGENCY')]
        unix, dos = "\n".join(records) + "\n", "\r\n".join(records) + "\r\n"
        unix_rows, unix_report = self.rows(unix)
        dos_rows, dos_report = self.rows(dos)
        self.assertEqual(len(unix_rows), 2)
        self.assertEqual(len(dos_rows), 2)  # the same two records, not one and not three
        self.assertEqual([r['source_record_hex'] for r in dos_rows],
                         [r['source_record_hex'] for r in unix_rows])
        self.assertFalse(dos_rows[0]['source_record_hex'].endswith('0d'))  # no trailing CR kept
        self.assertNotEqual(dos_report['source_sha256'], unix_report['source_sha256'])
        for text in (unix, dos):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / 'state.jsonl').write_bytes(text.encode())
                result = subprocess.run([os.sys.executable, str(SCRIPT), str(base / 'state.jsonl'),
                                         str(base / 'out')], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads((base / 'out/report.json').read_text())
                self.assertEqual(report['source_sha256'], self.rows(text)[1]['source_sha256'])
                self.assertEqual(report['records'], 2)

    def test_malformed_input_refuses_through_the_cli_without_a_traceback(self):
        missing = [json.dumps({key: value for key, value in json.loads(record()).items()
                               if key != dropped}) for dropped in
                   ('capture_time_us', 'topic', 'state')]
        huge = json.loads(record())
        huge['state']['real_freq'] = int(10 ** 400)  # a JSON integer float() cannot represent
        for text in (*[log(one) for one in missing], json.dumps(huge) + "\n"):
            self.refuses(text)  # ValueError, never KeyError or OverflowError
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / 'state.jsonl').write_text(text)
                failed = subprocess.run([os.sys.executable, str(SCRIPT), str(base / 'state.jsonl'),
                                         str(base / 'out')], capture_output=True, text=True)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(failed.stderr.splitlines()[-1],
                    'Robotnik dashboard state conversion failed; no successful conversion claim')
                self.assertNotIn('Traceback', failed.stderr)
                self.assertFalse((base / 'out').exists())

    def test_a_thousand_records_reach_common_output_under_the_same_bounds(self):
        states = ((300, 'READY'), (200, 'STANDBY_STATE'), (500, 'FAILURE'), (999, 'UNKNOWN'))
        text = log(*[record(*states[index % 4], capture=CAPTURE + index, real=10.0 + index / 8)
                     for index in range(1000)])
        self.assertLess(len(text.encode()), 16 * 1024 * 1024)  # the existing input bound stands
        rows, report = self.rows(text)
        self.assertEqual((len(rows), report['records']), (1000, 1000))
        self.assertEqual(report['unknown_state_codes'], 250)
        self.assertEqual(report['highlighted_rows'], 500)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'state.jsonl').write_text(text)
            self.assertEqual(subprocess.run([os.sys.executable, str(SCRIPT),
                str(base / 'state.jsonl'), str(base / 'out')], capture_output=True).returncode, 0)
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(PROFILE), str(base / 'out/observations.csv'), '--allow-equal-time'],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 1000)
                self.assertEqual(common['observations'][-1]['fields']['reported_state_name'],
                                 'UNKNOWN_RETAINED')

    def test_bounds_and_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'state.jsonl').write_text(log(record()))
            (base / 'out').mkdir()
            failed = subprocess.run([os.sys.executable, str(SCRIPT), str(base / 'state.jsonl'),
                                     str(base / 'out')], capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)  # an existing output directory is refused
            self.assertEqual(failed.stderr.splitlines()[-1],
                             'Robotnik dashboard state conversion failed; no successful conversion claim')
            self.assertEqual(list((base / 'out').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
