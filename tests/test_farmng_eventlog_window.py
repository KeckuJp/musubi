"""Authored native records; actual bridge/common, never a farm-ng recording claim."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import export_farmng_eventlog_window as window
from scripts import export_farmng_eventlog_filter_state as legacy
from tests import test_farmng_eventlog as fixtures
from tests.test_farmng_eventlog import (
    SELECTION, bridge_rows, container, event, future_messages,
    payload, record, state, write)


class FarmngWindowTests(unittest.TestCase):
    def test_long_native_adjacent_windows_reach_common_with_exact_source(self):
        later_event, later_state = future_messages()
        msg = state(x=4., cls=later_state)
        msg.future_scalar, msg.pose.future_note = 8.5, 'uninterpreted'
        pair = event(payload=msg.SerializeToString(), seconds=2., cls=later_event)
        pair[0].future_tag = 'new'
        outside = record(event('/camera/raw', payload=b'\xff' * (1024 * 1024), seconds=99.))
        selected = [record(event(payload=payload(x=x), seconds=t))
                    for x, t in [(1., 1.), (2., 2.), (3., 2.)]] + [record(pair)]
        blob = outside * 17 + b''.join(selected)
        self.assertGreater(len(blob), legacy.LIMIT)
        digest = hashlib.sha256(blob).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, blob)
            with self.assertRaisesRegex(ValueError, '16MiB'):
                legacy.export_filter_state_events(path, **SELECTION)
            all_envelopes, common_values = [], []
            for start, stop in [(17, 19), (19, 21)]:
                parts, report = window.export_filter_state_window(
                    path, start_record=start, stop_record=stop,
                    max_part_bytes=3000, **SELECTION)
                self.assertEqual((report['source_records'], report['selected_records'],
                                  report['unselected_records']), (21, 2, 19))
                self.assertEqual(report['input_sha256'], digest)
                self.assertEqual(report['input_bytes'], len(blob))
                self.assertEqual(report['record_window']['outside_records'], 19)
                self.assertEqual(report['record_window']['outside_payload'], 'NOT_DECODED_OR_VALIDATED')
                self.assertEqual(sum(report['unselected_paths'].values()), 19)
                self.assertEqual(report['unselected_paths']['/camera/raw'], 17)
                for n, part in enumerate(parts):
                    self.assertLessEqual(len(part.encode()), 3000)
                    envs = [json.loads(line) for line in part.splitlines()]
                    all_envelopes += envs
                    common = bridge_rows(part, tmp, f'w{start}-{n}')
                    self.assertEqual(common['main_rows'], len(envs))
                    for row in common['observations']:
                        self.assertEqual(row['clock_basis'], 'Unknown')
                        self.assertIsNone(row['anchor_unix_us'])
                        common_values.append(row['fields']['pose_position_m_x'])
            self.assertEqual(common_values, [1., 2., 3., 4.])
            self.assertEqual([e['source']['record_index'] for e in all_envelopes], [17,18,19,20])
            self.assertEqual([e['capture_time_us'] for e in all_envelopes], [1000000,2000000,2000000,2000000])
            offset = len(outside) * 17
            for envelope, raw in zip(all_envelopes, selected):
                src = envelope['source']
                self.assertEqual(src['record_offset'], offset)
                self.assertEqual(src['file_sha256'], digest)
                self.assertEqual(bytes.fromhex(src['record_hex'][4:]), raw)
                self.assertEqual(src['record_sha256'], hashlib.sha256(raw).hexdigest())
                offset += len(raw)
            self.assertEqual(offset, len(blob))
            raw = bytes.fromhex(all_envelopes[-1]['source']['record_hex'][4:])
            length = struct.unpack('<I', raw[:4])[0]
            recovered = later_state()
            recovered.ParseFromString(raw[4+length:])
            self.assertEqual(recovered.pose.future_note, 'uninterpreted')
            self.assertEqual(recovered.future_scalar, 8.5)

    def test_refusals_global_framing_selected_meaning_and_legacy_identity(self):
        good = record(event(payload=payload(x=1.)))
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, fixtures.FarmngEventLogTests().authored())
            parts, report = legacy.export_filter_state_events(path, **SELECTION)
            self.assertEqual(hashlib.sha256(''.join(parts).encode()).hexdigest(), LEGACY_PART_SHA)
            self.assertEqual(hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest(), LEGACY_REPORT_SHA)
            for bounds in [(False,1),(-1,1),(1,1),(2,1),(0,7)]:
                with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                    window.export_filter_state_window(path,start_record=bounds[0],stop_record=bounds[1],**SELECTION)
            cases = [good+b'\x01', good+struct.pack('<I',0),
                     good+struct.pack('<I',legacy.EVENT_LIMIT+1),
                     good+record(event('/outside',payload=b'x'))[:-1],
                     record(event(payload=b'\x0a\x05')),
                     record(event(query='type=wrong&pb=wrong',payload=payload())),
                     record(event('/other',payload=b'bad')),
                     record(event(payload=payload(),seconds=2.))+record(event(payload=payload(),seconds=1.))]
            for index, blob in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(ValueError):
                    window.export_filter_state_window(write(tmp,blob,'bad.bin'),start_record=0,
                        stop_record=2 if index==7 else 1,**SELECTION)

    def test_resource_limits_cli_and_existing_output(self):
        blob = container([event(payload=payload(x=1.)),event(payload=payload(x=2.))])
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp,blob)
            for name, limit, stop in [('MAX_INPUT_BYTES',len(blob)-1,1),('MAX_RECORDS',1,1),
                    ('MAX_SELECTED_BYTES',1,1),('MAX_SELECTED_RECORDS',1,2),('REPORT_LIMIT',1,1)]:
                with self.subTest(bound=name), patch.object(window,name,limit), self.assertRaisesRegex(ValueError,'bound'):
                    window.export_filter_state_window(path,start_record=0,stop_record=stop,**SELECTION)
            with patch.object(legacy,'OUTPUT_LIMIT',1), self.assertRaises(ValueError):
                window.export_filter_state_window(path,start_record=0,stop_record=1,**SELECTION)
            original = path.stat()
            changed = SimpleNamespace(**{name:getattr(original,name) for name in
                ['st_dev','st_ino','st_size','st_mode','st_mtime_ns','st_ctime_ns']})
            changed.st_mtime_ns += 1
            with patch.object(window.os,'fstat',side_effect=[original,changed]), self.assertRaisesRegex(ValueError,'source changed'):
                window.export_filter_state_window(path,start_record=0,stop_record=1,**SELECTION)
            output = Path(tmp)/'parts'
            args = [os.sys.executable,str(Path(window.__file__)),str(path),str(output),
                    '--start-record','0','--stop-record','2','--event-path',SELECTION['event_path'],
                    '--timestamp-clock',SELECTION['clock_name'],'--timestamp-semantics',SELECTION['semantics']]
            result = subprocess.run(args,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report = json.loads((output/'report.json').read_text())
            manifest = json.loads((output/'manifest.json').read_text())
            self.assertEqual(manifest['source_sha256'],hashlib.sha256(blob).hexdigest())
            self.assertEqual(manifest['records'],2)
            self.assertEqual(report['selected_records'],2)
            before = {p.name:p.read_bytes() for p in output.iterdir()}
            self.assertNotEqual(subprocess.run(args,capture_output=True).returncode,0)
            self.assertEqual(before,{p.name:p.read_bytes() for p in output.iterdir()})


LEGACY_PART_SHA = 'ce7d4a4737f26dff1780ec8520447afa5ef4cf12e7a8f06b8570ee0e53c3cadf'
LEGACY_REPORT_SHA = '96a439972d5c3e6d6f2c2d360829ba44957e9653d20119ced47a34b35d1d53ff'
