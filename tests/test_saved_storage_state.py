"""Authored fixed writer-shaped reports, not device acquisitions."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_storage_state import convert, MOUNT, SMART

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT/'profiles/declared/saved-storage-state/profile.toml'


def mount(parts=None, **extra):
    value = dict(source_format=MOUNT, capture_time_us=1, subject='caller-a',
                 partitions=parts if parts is not None else [dict(device='/dev/example',
                     mountpoint='/data', fstype='ext4', opts='rw,relatime,future=1')])
    return dict(value, **extra)


def smart(byte=0, **extra):
    value = dict(source_format=SMART, capture_time_us=1, subject='caller-a', device='/dev/example',
                 smart_status=dict(passed=not byte, nvme=dict(value=byte)))
    return dict(value, **extra)


def source(*rows):
    return ('\n'.join(json.dumps(row) for row in rows)+'\n').encode()


class SavedStorageTests(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'data.csv'; path.write_text(output)
            run = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
        return json.loads(run.stdout)

    def test_mount_and_health_meaning_common(self):
        output, report = convert(source(mount(future={'x':1}), smart(8)))
        rows = list(csv.DictReader(io.StringIO(output)))
        common = self.common(output)
        self.assertEqual(report['source_records'], 2)
        self.assertEqual(common['main_rows'], 2)
        self.assertEqual(common['observations'][0]['fields']['mount_access_report'], 'REPORTED_RW')
        self.assertEqual(common['observations'][1]['fields']['media_read_only'], 'reported_true')
        self.assertEqual(common['observations'][1]['fields']['warning_byte'], 8)
        self.assertEqual(common['observations'][0]['clock_basis'], 'Unknown')
        self.assertIn('future', bytes.fromhex(rows[0]['source_record_hex'][4:]).decode())
        self.assertNotEqual(rows[0]['source_record_sha256'], rows[1]['source_record_sha256'])

    def test_other_input_zero_unknown_missing_and_empty(self):
        output, report = convert(source(mount([]), smart(0), smart(128), smart(smart_status=None)))
        rows = self.common(output)['observations']
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['fields']['partition_count'], 0)
        self.assertEqual(rows[1]['fields']['health_report'], 'REPORTED_PASSED')
        self.assertEqual(rows[2]['fields']['unknown_warning_bits'], 128)
        self.assertEqual(rows[3]['fields']['health_report'], 'UNKNOWN_NULL_STATUS')
        self.assertEqual((report['empty_inventories'], report['missing_health']), (1,1))
        missing = smart(); del missing['smart_status']
        out, _ = convert(source(missing))
        self.assertIn('UNKNOWN_MISSING_STATUS', out)

    def test_refusals_and_option_ambiguity(self):
        for value in (smart(smart_status={'passed':True,'nvme':{'value':8}}),
                      smart(smart_status={'passed':False,'nvme':{'value':8,'media_read_only':False}}),
                      smart(smart_status={'passed':False,'nvme':{'value':64,'other':False}}),
                      smart(source_format='all-smart'), mount(capture_time_us=True),
                      mount([{'device':'x','mountpoint':'/','fstype':'ext4','opts':None}])):
            with self.subTest(value=value), self.assertRaises(ValueError): convert(source(value))
        with self.assertRaises(ValueError): convert(b'{"x":1,"x":2}\n')
        with self.assertRaises(ValueError): convert(source(mount(capture_time_us=2), smart()))
        for options, expected in [('ro,rw','AMBIGUOUS_RO_RW'), ('future','UNKNOWN'), ('ro','REPORTED_RO')]:
            out, _ = convert(source(mount([dict(device='',mountpoint='/',fstype='tmpfs',opts=options)])))
            self.assertEqual(list(csv.DictReader(io.StringIO(out)))[0]['mount_access_report'], expected)

    def test_cli_preserves_original_and_no_overwrite(self):
        data = source(mount(), smart())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); inp = base/'in.jsonl'; inp.write_bytes(data)
            args = [os.sys.executable, str(ROOT/'scripts/convert_saved_storage_state.py'), str(inp), str(base/'out')]
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual((base/'out/source.jsonl').read_bytes(), data)
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
