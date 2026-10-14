"""Authored pinned event payloads; no device tracing or physical diagnosis."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_block_error import convert_path, SOURCE_FORMAT

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT/'profiles/declared/saved-block-error/profile.toml'


def record(payload='8,0 WS () 4096 + 8 BE,0,4 [-5]', **changes):
    return dict(dict(source_format=SOURCE_FORMAT, errno_abi='linux-6.12-asm-generic',
        event='block_rq_error', source_id='host-a', capture_time_us=100,
        clock_basis='supplied-trace-clock-unqualified', payload=payload), **changes)


class SavedBlockErrorTests(unittest.TestCase):
    def common(self, value, directory):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'postbuild common reader required')
        path = Path(directory)/'output.csv'; path.write_text(value)
        run = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                             capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr.decode())
        return json.loads(run.stdout)['observations']

    def test_two_saved_inputs_common_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.jsonl'
            for source in ([record(future={'retained':1}), record('8,0 R () 50 + 1 BE,0,4 [-30]')],
                           [record('0,0 FWSM () 1 + 0 NONE,100,0 [-28]', source_id='host-b'),
                            record('9,1 Z () 20 + 2 RT,1,0 [-123]')]):
                data = ('\r\n'.join(json.dumps(v) for v in source)+'\r\n').encode()
                path.write_bytes(data)
                out, report = convert_path(path, start_record=0, stop_record=2)
                rows = list(csv.DictReader(io.StringIO(out)))
                observations = self.common(out, directory)
                self.assertEqual(len(observations), 2)
                self.assertEqual(observations[0]['clock_basis'], 'Unknown')
                self.assertEqual(rows[0]['write_assertion'], 'REPORTED_PARTIAL_REQUEST_WRITE_FAILURE')
                self.assertEqual(report['source_sha256'], 'sha256:'+hashlib.sha256(data).hexdigest())
                self.assertEqual(rows[0]['source_record_index'], '1')
                self.assertIn('payload', bytes.fromhex(rows[0]['source_record_hex'][4:]).decode())
                if source[0]['source_id'] == 'host-b':
                    self.assertEqual(rows[0]['target_status'], 'UNKNOWN_NO_DISK_DEVICE')
                    self.assertEqual(rows[1]['operation'], 'UNKNOWN_OPERATION')
                    self.assertEqual(rows[1]['errno_name'], 'UNKNOWN_ERRNO')
                else:
                    self.assertEqual(rows[0]['request_sector_bytes'], '4096')
                    self.assertEqual(rows[1]['write_assertion'], 'NOT_A_REPORTED_WRITE')

    def test_large_source_window_and_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.jsonl'
            padding = ('{"outside":"'+'x'*1024+'"}\n').encode()
            data = padding*17000 + (json.dumps(record())+'\n').encode()
            self.assertGreater(len(data), 16*1024**2)
            path.write_bytes(data)
            out, report = convert_path(path, start_record=17000, stop_record=17001)
            row = self.common(out, directory)[0]['fields']
            self.assertEqual(row['source_record_index'], 17001)
            self.assertEqual(report['outside_physical_lines'], 17000)
            self.assertEqual(row['source_record_byte_offset'], len(padding)*17000)
            for bad in (record(event='block_rq_complete'), record(clock_basis=''),
                        record(capture_time_us=True), record('8,0 W () 1 + 1 BE,0,4 [0]'),
                        record('8,0 W () 1 + 1 BE,0,4 [5]'), record('broken'),
                        record(errno_abi='inferred')):
                path.write_text(json.dumps(bad)+'\n')
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    convert_path(path, start_record=0, stop_record=1)


if __name__ == '__main__':
    unittest.main()
