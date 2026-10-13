"""Authored saved API events, not device or severity validation."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import unquote
from scripts import convert_farmbot_saved_logs as m

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'profiles/declared/farmbot-saved-logs/profile.toml'


def event(kind='warn', created=1700000000):
    return dict(schema_commit=m.PIN, capture_time_us=8, device_identity='caller:bot-A',
                payload=dict(id=7, message='motor report\nnot diagnosis', type=kind,
                             created_at=created, verbosity=2, future={'x': True}))


class SavedLogsTests(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not reader: self.skipTest('actual common reader not configured')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'events.csv'; path.write_text(output)
            run = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                                 text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_two_inputs_common_unknown_and_missing(self):
        for records in ([event(), event('future_type', None)], [event('error', 0), event('info', 9)]):
            text = '\n'.join(json.dumps(x) for x in records)
            output, report = m.convert(text)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report['source_records'], 2)
            self.assertEqual(bytes.fromhex(rows[0]['source_record_hex'][4:]).decode(), text.splitlines()[0])
            common = self.common(output)
            self.assertEqual(len(common['observations']), 2)
            self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
            self.assertEqual(common['observations'][0]['fields']['log_type_reported'], records[0]['payload']['type'])
            self.assertEqual(unquote(rows[0]['log_message'][5:]), records[0]['payload']['message'])
        output, report = m.convert(json.dumps(event('unknown', None)))
        self.assertEqual(report['unknown_type_records'], 1)
        self.assertEqual(list(csv.DictReader(io.StringIO(output)))[0]['log_created_at_seconds'], '')

    def test_window_source_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'capture.jsonl'
            path.write_bytes(b'outside-invalid-json\r\n' + json.dumps(event()).encode() + b'\r\n' + json.dumps(event('info')).encode())
            output, report = m.convert_path(path, start_record=1, stop_record=3)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['source_record_index'] for r in rows], ['1', '2'])
            self.assertEqual(rows[0]['source_record_byte_offset'], '22')
            self.assertEqual(report['excluded_records_not_decoded'], 1)
            self.assertEqual(len(self.common(output)['observations']), 2)

    def test_invalid_inputs(self):
        cases = []
        for key, value in [('created_at', True), ('created_at', -1), ('created_at', 2**53),
                           ('type', 1), ('message', None), ('verbosity', True), ('id', 2**53)]:
            row = event(); row['payload'][key] = value; cases.append(row)
        for key, value in [('schema_commit', 'wrong'), ('device_identity', ''), ('capture_time_us', -1)]:
            row = event(); row[key] = value; cases.append(row)
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError): m.convert(json.dumps(row))
        for text in ('', '{"a":1,"a":2}', json.dumps(event())+'\n\n'):
            with self.assertRaises(ValueError): m.convert(text)
