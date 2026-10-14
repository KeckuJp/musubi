import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_rmf_issue_reports as subject

ROOT = Path(__file__).resolve().parents[1]
PIN = 'a736ddebeee9eaf3970756d7dd452065273da918'


class RmfParticipationTests(unittest.TestCase):
    def test_rest_socket_reuse_common_and_old_default(self):
        state = dict(name='fleet', robots={'a': dict(commission={'dispatch_tasks': True, 'direct_tasks': False,
            'future_flag': {'retained': 7}}, mutex_groups={'locked': ['area', 'area'], 'requesting': []}, issues=[])})
        rest = dict(schema_commit=PIN, capture_time_us=10, state=state)
        socket = dict(schema_commit=PIN, capture_time_us=20, protocol_version=5,
            transport='websocket-complete-text', direction='received', packet='42'+json.dumps(['/fleets/fleet/state',
            dict(name='fleet', robots={'b': dict(commission={'idle_behavior': True}, mutex_groups={'requesting': ['other']})})]))
        for record, fmt, expected in ((rest, subject.existing.RMF_WEB_FORMAT, 6), (socket, subject.existing.RMF_SOCKET_FORMAT, 5)):
            text = json.dumps(record)
            old = subject.convert(text, fmt)
            base, issues, participation, report = subject.convert_participation(text, fmt)
            self.assertEqual((base, issues), old[:2])
            self.assertEqual(report['participation_output_rows'], expected)
            rows = list(csv.DictReader(io.StringIO(participation)))
            self.assertEqual(len(rows), expected)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'out.csv'; path.write_text(participation)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT/'profiles/declared/rmf-participation/profile.toml'), str(path),
                    '--allow-equal-time'], check=True, capture_output=True).stdout)
            self.assertEqual(common['main_rows'], expected)
            self.assertTrue(all(row['clock_basis'] == 'Unknown' for row in common['observations']))
            self.assertEqual(report['source_record_bindings'][0]['source_record_index'], 0)
        base, _, sidecar, _ = subject.convert_participation(json.dumps(rest), subject.existing.RMF_WEB_FORMAT)
        original = bytes.fromhex(next(csv.DictReader(io.StringIO(base)))['source_record_hex'][4:]).decode()
        self.assertIn('future_flag', original)
        rows = list(csv.DictReader(io.StringIO(sidecar)))
        self.assertEqual([row['participation_flag_reported'] for row in rows[:3]], ['1', '0', ''])
        self.assertEqual([row['mutex_index'] for row in rows[3:5]], ['0', '1'])

    def test_missing_null_empty_wrong_type_and_empty_fleet(self):
        for robot in ({}, {'commission': None, 'mutex_groups': None}, {'commission': {}, 'mutex_groups': {}}):
            text = json.dumps(dict(schema_commit=PIN, capture_time_us=1, state={'robots': {'x': robot}}))
            rows = list(csv.DictReader(io.StringIO(subject.convert_participation(text, subject.existing.RMF_WEB_FORMAT)[2])))
            self.assertTrue(all(row['participation_flag_reported'] == '' for row in rows))
        for robot in ({'commission': {'dispatch_tasks': 1}}, {'commission': []},
                      {'mutex_groups': {'locked': 'bad'}}, {'mutex_groups': {'requesting': [1]}}):
            text = json.dumps(dict(schema_commit=PIN, capture_time_us=1, state={'robots': {'x': robot}}))
            with self.assertRaises(ValueError):
                subject.convert_participation(text, subject.existing.RMF_WEB_FORMAT)
        empty = json.dumps(dict(schema_commit=PIN, capture_time_us=1, state={}))
        rows = list(csv.DictReader(io.StringIO(subject.convert_participation(empty, subject.existing.RMF_WEB_FORMAT)[2])))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['participation_collection'] == 'INAPPLICABLE_NO_ROBOTS' for row in rows))
