"""Authored saved declarations only: no mission generation for a device."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.recorded_mission_parameters import interpret, DEFINITIONS
from scripts.convert_gpx_recorded import convert_wpl
from scripts.convert_saved_mission_plan import convert
from tests.test_saved_mission_plan import plan, item

ROOT = Path(__file__).resolve().parents[1]

class MissionParametersTests(unittest.TestCase):
    def test_fixed_nine_command_roles_and_sentinels(self):
        cases = {16:([2,3,-4,'NaN',40,20,30],1,'hold','2'),
                 20:([0]*7,1,'unused',''),
                 21:([0,2,0,'NaN',40,20,0],2,'land_mode','2'),
                 22:([10,0,1,'NaN',40,20,20],3,'takeoff_flags','1'),
                 178:([1,-1,-2,0,0,0,0],2,'speed',''),
                 179:([0,0.01,0,'NaN',40,20,20],2,'surface_roll','0'),
                 181:([2,1,0,0,0,0,0],2,'relay_setting','1'),
                 183:([2,1500,0,0,0,0,0],2,'pulse_width','1500'),
                 189:([0]*7,5,'latitude','')}
        self.assertEqual(set(cases),set(DEFINITIONS))
        for command,(values,index,role,expected) in cases.items():
            with self.subTest(command=command):
                row=interpret(command,3,values)
                self.assertEqual(row[f'declared_p{index}_role'],role)
                self.assertEqual(row[f'declared_p{index}_value'],expected)
        self.assertEqual(interpret(178,2,cases[178][0])['declared_p2_disposition'],'NO_CHANGE')
        self.assertEqual(interpret(16,3,[0,0,0,None,40,20,30])['declared_p4_disposition'],'MISSING_NULL')
        self.assertEqual(interpret(16,3,cases[16][0])['declared_p4_disposition'],'USE_SYSTEM_YAW_MODE')
        self.assertEqual(interpret(16,1,[0,0,0,0,400,200,30])['declared_p5_value'],'')
        self.assertEqual(interpret(60000,0,[1]*7)['mission_parameter_coverage'],'UNINTERPRETED_COMMAND')
        self.assertEqual(interpret(179,0,[1,0,0,0,400,200,30])['declared_p5_disposition'],'IGNORED_USE_CURRENT_LOCATION')
        for command,values in ((16,[-1,0,0,0,40,20,30]),(179,[2,0,0,0,0,0,0]),
                               (183,[1,1.5,0,0,0,0,0]),(178,[1,-3,0,0,0,0,0])):
            with self.assertRaises(ValueError): interpret(command,3,values)
        with self.assertRaises(ValueError): interpret(183,2,[1,2**53,0,0,0,0,0])

    def test_two_formats_common_and_original_default(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader,'post-build stage must supply actual reader')
        wpl=b'QGC WPL 110\n0 0 3 16 2 3 0 nan 40 20 30 1\n1 0 2 178 1 -1 -2 0 0 0 0 1\n2 0 2 60000 1 2 3 4 5 6 7 1\n'
        baseline,report=convert_wpl(wpl,99)
        self.assertEqual(report['decoded_points'],1)
        self.assertNotIn('declared_p1',baseline)
        expanded,report=convert_wpl(wpl,99,parameter_meanings=True)
        self.assertEqual((report['output_rows'],report['parameter_qualified_rows'],report['parameter_uninterpreted_rows'],report['decoded_points']),(3,2,1,1))
        data=json.dumps(plan(items=[item(params=[2,3,0,None,40,20,30]),
                                    item(command=183,frame=2,params=[1,1500,None,None,None,None,None]),
                                    item(command=60000)])).encode()
        old,_=convert(data,100)
        self.assertNotIn('declared_p1',old)
        newer,report=convert(data,100,parameter_meanings=True)
        self.assertEqual(report['parameter_uninterpreted_items'],1)
        with tempfile.TemporaryDirectory() as directory:
            for name,output,profile,count in [('wpl',expanded,'gpx-points/wpl-profile.toml',3),
                                              ('plan',newer,'saved-mission-plan/profile.toml',4)]:
                target=Path(directory)/f'{name}.csv';target.write_text(output)
                result=subprocess.run([reader,str(ROOT/'profiles/declared'/profile),str(target),'--allow-equal-time'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                common=json.loads(result.stdout)
                self.assertEqual(common['main_rows'],count)
                fields=[x['fields'] for x in common['observations']]
                if name=='wpl':
                    self.assertEqual(fields[0]['declared_p1_value'],2)
                    self.assertEqual(fields[1]['declared_p2_disposition'],'NO_CHANGE')
                else:
                    self.assertEqual(fields[1]['declared_p4_disposition'],'MISSING_NULL')
                    self.assertEqual(fields[2]['declared_p2_value'],1500)
                    self.assertEqual(fields[2]['declared_p2_unit'],'us')
                self.assertEqual(common['platform_domain'],'Unknown')
            saved=Path(directory)/'input.plan';saved.write_bytes(data)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/convert_saved_mission_plan.py'),str(saved),str(Path(directory)/'planout'),'--capture-time-us','100','--parameter-meanings'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((Path(directory)/'planout/observations.csv').read_text(),newer)
            saved=Path(directory)/'input.waypoints';saved.write_bytes(wpl)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/convert_gpx_recorded.py'),str(saved),str(Path(directory)/'wplout'),'--capture-time-us','99','--source-format','qgc-wpl110','--mission-parameter-meanings'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((Path(directory)/'wplout/observations.csv').read_text(),expanded)
        bad=plan(items=[item(command=179,params=[2,0,0,0,40,20,30])])
        with self.assertRaises(ValueError):convert(json.dumps(bad).encode(),100,parameter_meanings=True)
