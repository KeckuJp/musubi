import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_rmf_issue_reports import convert
from scripts import convert_ardupilot_battery_csv as existing

class RmfIssueTests(unittest.TestCase):
    def test_rest_and_socket_reports_reach_common_and_bind_to_existing(self):
        record = dict(schema_commit='a736ddebeee9eaf3970756d7dd452065273da918',
            capture_time_us=1000, state=dict(name='fleet', robots={
                'a': dict(issues=[dict(category='obstruction', detail={'future': [0, False]}), {}]),
                'b': dict(issues=[]), 'c': dict(issues=None), 'd': {}}))
        other = dict(record, capture_time_us=2000, protocol_version=5,
            transport='websocket-complete-text', direction='received',
            packet='42'+json.dumps(['/fleets/fleet/state', dict(name='fleet', robots={'x': dict(issues=[dict(category='')])})]))
        for source, fmt, count in ((record, existing.RMF_WEB_FORMAT, 5), (other, existing.RMF_SOCKET_FORMAT, 1)):
            text = json.dumps(source)
            base, sidecar, report = convert(text, fmt)
            self.assertEqual(base, existing.convert_json(text, fmt))
            rows = list(csv.DictReader(io.StringIO(sidecar)))
            self.assertEqual(len(rows), count)
            base_rows = list(csv.DictReader(io.StringIO(base)))
            keys = {(r['rmf_snapshot_sha256'], r['rmf_robot_key_hex']) for r in base_rows}
            self.assertTrue(all((r['rmf_source_sha256'], r['rmf_robot_key_hex']) in keys for r in rows))
            self.assertEqual(report['output_rows'], count)
            self.assertEqual(report['source_record_bindings'], [dict(source_record_index=0, base_row_start=0, base_row_stop=len(base_rows))])
            reader = os.environ.get('MUSUBI_TELEMETRY_READER')
            if not reader:
                self.skipTest('common reader not configured')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'issues.csv'
                path.write_text(sidecar)
                result = json.loads(subprocess.run([reader, 'profiles/declared/electrical-schema-reuse/json-profile.toml', str(path), '--allow-equal-time'], check=True, text=True, capture_output=True).stdout)
                self.assertEqual(len(result['observations']), count)
        self.assertEqual([r['rmf_issue_collection'] for r in list(csv.DictReader(io.StringIO(convert(json.dumps(record), existing.RMF_WEB_FORMAT)[1])))],
                         ['PRESENT', 'PRESENT', 'EMPTY', 'NULL', 'MISSING'])

    def test_bad_type_length_and_format_reject(self):
        for issues in ([dict(category=4)], [dict(detail=3)], 'bad', [dict(category='x'*4097)]):
            record = dict(schema_commit='a736ddebeee9eaf3970756d7dd452065273da918', capture_time_us=1,
                          state=dict(robots={'a':dict(issues=issues)}))
            with self.assertRaises(ValueError):
                convert(json.dumps(record), existing.RMF_WEB_FORMAT)
        with self.assertRaises(ValueError):
            convert('{}', 'ros-battery-json')
        text = json.dumps(dict(schema_commit='a736ddebeee9eaf3970756d7dd452065273da918', capture_time_us=1, state={}))
        base, sidecar, report = convert(text+'\n'+text, existing.RMF_WEB_FORMAT)
        self.assertEqual(report['source_record_bindings'], [dict(source_record_index=0, base_row_start=0, base_row_stop=1), dict(source_record_index=1, base_row_start=1, base_row_stop=2)])
        rows = list(csv.DictReader(io.StringIO(sidecar)))
        self.assertEqual([r['rmf_source_record_index'] for r in rows], ['0', '1'])
        self.assertEqual({r['rmf_issue_collection'] for r in rows}, {'INAPPLICABLE_NO_ROBOTS'})
