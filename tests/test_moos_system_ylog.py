import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_moos_system_ylog as ylog

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT/'profiles/declared/moos-system-ylog/profile.toml'


def row(time, key, source, payload):
    return f'{time:<10} {key:<20} {source:<20} {payload:<20}\n'


class SystemYlogTests(unittest.TestCase):
    def test_two_inputs_and_empty_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        for time, content in (('1.25', '17'), ('-2e-3', '  engine state')):
            with self.subTest(time=time), tempfile.TemporaryDirectory() as d:
                path = Path(d)/'saved.ylog'
                path.write_text('%% LOGSTART 1000\n'+row(time,'MOOS_DEBUG','pX',content)+
                    row('3','MOOS_SYSTEM','pY','ready')+row('4','OTHER','pY','unqualified'))
                output, report = ylog.convert(path, 1000)
                target = Path(d)/'out.csv'; target.write_text(output)
                common = json.loads(subprocess.run([reader,str(PROFILE),str(target),'--allow-equal-time'],
                    capture_output=True,check=True).stdout)
                self.assertEqual(common['main_rows'],3)
                first = common['observations'][0]
                self.assertEqual(first['clock_basis'],'Unknown')
                self.assertEqual(first['fields']['reported_relative_seconds'],float(time))
                self.assertEqual(first['fields']['event_kind'],'REPORTED_DEBUG_EVENT')
                self.assertEqual(first['fields']['payload_type'],'UNKNOWN_WRITER_ERASED_DOUBLE_VS_TEXT')
                self.assertEqual(first['fields']['payload_text_hex'],ylog.hextext(f'{content:<20}'))
                self.assertEqual(report['qualified_system_events'],2)
                self.assertEqual(report['unknown_key_records'],1)
                self.assertEqual(report['physical_lines'],4)
                path.write_text('%% LOGSTART 1000\n')
                empty, report = ylog.convert(path,1000); target.write_text(empty)
                common = json.loads(subprocess.run([reader,str(PROFILE),str(target),'--allow-empty-rows'],
                    capture_output=True,check=True).stdout)
                self.assertEqual(common['main_rows'],0)

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'x'
            for text in (row('nan','MOOS_DEBUG','pX','a'),row('1e-999','MOOS_SYSTEM','pX','a'),
                         '1 MOOS_DEBUG pX a\n','bad\n'):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError): ylog.convert(path,1)
            path.write_text(row('1','MOOS_DEBUG','pX','a'))
            with self.assertRaises(ValueError): ylog.convert(path, True)
