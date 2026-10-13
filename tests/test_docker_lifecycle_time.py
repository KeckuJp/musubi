"""Fixed saved Moby State lifecycle instants; authored, never a daemon operation."""
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

from scripts import convert_docker_lifecycle_time as bridge
from scripts import convert_ardupilot_battery_csv as base

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'profiles/declared/docker-lifecycle-time/profile.toml'


def sample():
    return dict(daemon_version='27.3.1', capture_time_us=1000, inspect=dict(
        Id='container-a', Name='/reported-name', Future={'retained': 'yes'}, State=dict(
            Status='restarting', Running=True, Paused=False, Restarting=True,
            OOMKilled=False, Dead=False, ExitCode=7, Error='',
            Health=dict(Status='unhealthy', FailingStreak=2),
            StartedAt='2026-01-02T00:00:00.123456789+01:00',
            FinishedAt='2026-01-01T22:59:59.000000001Z')))


class DockerLifecycleTests(unittest.TestCase):
    def common(self, content):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not reader:
            self.skipTest('actual common reader not configured')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.csv'
            path.write_text(content)
            return json.loads(subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                                             check=True, capture_output=True).stdout)

    def test_exact_nanoseconds_state_association_and_old_columns_unchanged(self):
        first, second = sample(), sample()
        second['inspect']['Id'] = 'container-b'
        second['inspect']['State'].update(StartedAt='2026-01-03T12:00:00.4Z',
                                          FinishedAt='2026-01-03T12:00:03.99Z')
        text = '\n'.join(map(json.dumps, [first, second]))
        before = base.convert(text, base.DOCKER_STATE_FORMAT)
        output, report = bridge.convert(text)
        self.assertEqual(base.convert(text, base.DOCKER_STATE_FORMAT), before)
        old = list(csv.DictReader(io.StringIO(before)))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([{k: row[k] for k in old[0]} for row in rows], old)
        common = self.common(output)
        self.assertEqual(common['main_rows'], 2)
        fields = [o['fields'] for o in common['observations']]
        self.assertEqual(fields[0]['container_started_epoch_s'], 1767308400)
        self.assertEqual(fields[0]['container_started_subsecond_ns'], 123456789)
        self.assertEqual(fields[0]['container_finished_epoch_s'], 1767308399)
        self.assertEqual(fields[0]['container_finished_subsecond_ns'], 1)
        self.assertEqual(fields[1]['container_started_subsecond_ns'], 400000000)
        self.assertEqual(fields[1]['container_finished_subsecond_ns'], 990000000)
        self.assertEqual(fields[0]['container_exit_code_reported'], 7)
        self.assertEqual(fields[0]['container_health_reported'], 'unhealthy')
        self.assertEqual(fields[0]['container_running_reported'], 'True')
        self.assertNotEqual(fields[0]['container_id_hex'], fields[1]['container_id_hex'])
        self.assertEqual({o['clock_basis'] for o in common['observations']}, {'Unknown'})
        self.assertEqual(report['source_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(report['rows'], 2)
        self.assertEqual(fields[1]['source_record_index'], 1)
        self.assertEqual(json.loads(bytes.fromhex(fields[0]['source_record_hex'][4:])), first)
        self.assertNotIn('container_duration_s', fields[0])

    def test_zero_time_missing_and_cli_pair(self):
        first = sample()
        first['inspect']['State']['StartedAt'] = '0001-01-01T00:00:00Z'
        del first['inspect']['State']['FinishedAt']
        output, report = bridge.convert(json.dumps(first))
        fields = self.common(output)['observations'][0]['fields']
        self.assertEqual(fields['container_started_disposition'], 'ZERO_TIME')
        self.assertEqual(fields['container_finished_disposition'], 'NOT_REPORTED')
        self.assertIsNone(fields['container_started_epoch_s'])
        self.assertIsNone(fields['container_finished_epoch_s'])
        self.assertEqual(report['time_dispositions'], {'REPORTED': 0, 'ZERO_TIME': 1, 'NOT_REPORTED': 1})
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p / 'input.jsonl').write_text(json.dumps(first))
            args = [os.sys.executable, str(ROOT / 'scripts/convert_docker_lifecycle_time.py'),
                    str(p / 'input.jsonl'), str(p / 'out.csv'), '--report', str(p / 'report.json')]
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual((p / 'out.csv').read_bytes().decode(), output)
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
            self.assertEqual((p / 'out.csv').read_bytes().decode(), output)

    def test_bad_time_version_shape_and_output_bound_refuse(self):
        for value in (None, '', True, '2026-01-01T00:00:00', '2026-02-30T00:00:00Z',
                      '2026-01-01T00:00:60Z', '2026-01-01T00:00:00.1234567890Z',
                      '2026-01-01T00:00:00+14:01', '2026-01-01T00:00:00-00:00',
                      '1960-01-01T00:00:00Z'):
            item = sample(); item['inspect']['State']['StartedAt'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.convert(json.dumps(item))
        item = sample(); item['daemon_version'] = 'other'
        with self.assertRaises(ValueError): bridge.convert(json.dumps(item))
        with self.assertRaises(ValueError): bridge.convert('{"inspect":{},"inspect":{}}')
        with patch.object(bridge, 'OUTPUT_LIMIT', 10), self.assertRaisesRegex(ValueError, 'output'):
            bridge.convert(json.dumps(sample()))


if __name__ == '__main__':
    unittest.main()
