"""Authored Linux psutil reports, not host/device recordings."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_filesystem_capacity import convert, FORMAT

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT/'profiles/declared/saved-filesystem-capacity/profile.toml'


def entry(total=1000, used=600, free=350, percent=63.2, **changes):
    value = dict(source_format=FORMAT, capture_time_us=1, subject='saved-host-a',
                 path='/authored/data', disk_usage=dict(total=total, used=used, free=free, percent=percent))
    value.update(changes)
    return value


def source(*entries):
    return ('\n'.join(json.dumps(e) for e in entries)+'\n').encode()


class FilesystemCapacityTests(unittest.TestCase):
    def common(self, output):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data.csv'; path.write_text(output)
            result=subprocess.run([reader,str(PROFILE),str(path),'--allow-equal-time'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
        return json.loads(result.stdout)

    def test_reserved_and_equal_time_unknown_preservation(self):
        data=source(entry(extra={'future':3}),entry(path='/second',free=400,percent=60))
        output,report=convert(data)
        rows=list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]['root_free_bytes_exact'],'integer:400')
        self.assertEqual(rows[0]['reserved_difference_bytes_exact'],'integer:50')
        self.assertEqual(rows[0]['reported_percent'],'63.2')
        self.assertEqual(report['source_records'],2)
        common=self.common(output)
        self.assertEqual(common['main_rows'],2)
        self.assertEqual(common['platform_domain'],'Unknown')
        self.assertTrue(all(o['clock_basis']=='Unknown' for o in common['observations']))
        self.assertEqual(common['observations'][0]['fields']['user_available_bytes'],350)
        self.assertIn('future',bytes.fromhex(rows[0]['source_record_hex'][4:]).decode())

    def test_separate_reuse_negative_available_and_large_integer(self):
        large=2**53+1
        output,report=convert(source(entry(total=100,used=100,free=-10,percent=111.1),
            entry(total=large,used=0,free=large,percent=0,capture_time_us=2,subject='saved-host-b')))
        rows=list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]['user_available_bytes'],'-10')
        self.assertEqual(rows[1]['total_bytes'],'')
        common=self.common(output)
        self.assertEqual(common['observations'][1]['fields']['total_bytes_exact'],f'integer:{large}')
        self.assertEqual(report['source_records'],2)

    def test_failures_and_cli_original(self):
        for changes in ({'source_format':'psutil-any'},{'capture_time_us':True},{'subject':''},
                        {'disk_usage':{'total':1,'used':0,'free':1,'percent':True}},
                        {'disk_usage':{'total':1,'used':2,'free':0,'percent':100}},
                        {'disk_usage':{'total':1000,'used':600,'free':350,'percent':60}}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): convert(source(entry(**changes)))
        with self.assertRaises(ValueError): convert(b'{"a":1,"a":2}\n')
        with self.assertRaises(ValueError): convert(source(entry(capture_time_us=2),entry(capture_time_us=1)))
        data=source(entry())
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); inp=base/'input.jsonl'; inp.write_bytes(data)
            args=[os.sys.executable,str(ROOT/'scripts/convert_saved_filesystem_capacity.py'),str(inp),str(base/'out')]
            subprocess.run(args,check=True,capture_output=True)
            self.assertEqual((base/'out/source.jsonl').read_bytes(),data)
            self.assertEqual(subprocess.run(args,capture_output=True).returncode,2)
