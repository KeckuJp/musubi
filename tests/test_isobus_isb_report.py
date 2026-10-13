import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_candump_recorded as capture
from scripts import convert_isobus_ut_tc_capture as converter
ROOT=Path(__file__).resolve().parents[1]
PROFILE=ROOT/'profiles/declared/isobus-ut-tc-passive/profile.toml'


def saved(states, source=0x42):
    lines=[]
    for index,(state,counter) in enumerate(states):
        payload=b'\xff'*6+bytes([counter,0xfc|state])
        lines.append(f'(1700000000.{index:06d}) can0 {0x0cfd0200|source:08X}#{payload.hex()}')
    return capture.convert('\n'.join(lines)+'\n','can0',extended=True)[0]


class IsbReportTests(unittest.TestCase):
    def test_two_sources_all_states_and_common_retention(self):
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        for source,states in [(0x42,[(0,254),(1,255)]),(0x55,[(2,0),(3,1)])]:
            text=saved(states,source)
            output,report=converter.convert(text,isb_switch_state=True)
            self.assertEqual(report['isb_switch_state_messages'],2)
            with tempfile.TemporaryDirectory() as temp:
                folder=Path(temp); path=folder/'observations.csv'; path.write_text(output)
                common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],str(PROFILE),str(path),
                    '--allow-equal-time'],check=True,capture_output=True).stdout)
                fields=[r['fields'] for r in common['observations']]
                self.assertEqual([r['isb_switch_state_code'] for r in fields],[s[0] for s in states])
                self.assertEqual([r['isb_transition_count_reported'] for r in fields],[s[1] for s in states])
                self.assertTrue(all(r['isobus_source_address']==source for r in fields))
                self.assertTrue(all('NOT_ACTUAL_STOP' in r['isb_report_basis'] for r in fields))
                self.assertTrue(all(r['source_record_hex'].startswith('hex:0cfd02') for r in fields))
                sourcefile=folder/'capture.csv'; sourcefile.write_text(text)
                subprocess.run([sys.executable,str(ROOT/'scripts/convert_isobus_ut_tc_capture.py'),
                    str(sourcefile),str(folder/'out'),'--isb-switch-state'],check=True,capture_output=True)
                self.assertEqual((folder/'out/observations.csv').read_text(),output)

    def test_default_parity_length_page_and_option_refusals(self):
        legacy=converter.HEADER+'\n1700000000000000,18E62680,0effffff018203ff\n'
        self.assertEqual(converter.convert(legacy),converter.convert(legacy,isb_switch_state=False))
        text=saved([(0,255),(1,0)])
        for flag in [0,1,None,'true']:
            with self.assertRaises(ValueError): converter.convert(text,isb_switch_state=flag)
        with self.assertRaises(ValueError): converter.convert(text)
        extra='1700000000000002,0CFD0242,ff\n1700000000000003,0DFD0242,ffffffffffff00fc\n'
        output,report=converter.convert(text+extra,isb_switch_state=True)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))),2)
        self.assertEqual(len(report['short_selected_frames']),1)
        self.assertEqual(len(report['other_data_page_frames']),1)
        self.assertEqual(report['selected_rows'],2)


if __name__=='__main__': unittest.main()
