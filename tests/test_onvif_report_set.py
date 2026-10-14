import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_onvif_report_set import convert, digest, BASIS, ROUTES
from tests.test_onvif_imaging_settings import response as imaging
from tests.test_onvif_recording_job import response as recording, source, track

ROOT=Path(__file__).resolve().parents[1]


def inputs():
    ptz=('<p:GetStatusResponse xmlns:p="http://www.onvif.org/ver20/ptz/wsdl" '
         'xmlns:t="http://www.onvif.org/ver10/schema"><p:PTZStatus>'
         '<t:Position><t:PanTilt x="0.5" y="-0.5" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/>'
         '</t:Position><t:UtcTime>2026-01-01T00:00:00Z</t:UtcTime></p:PTZStatus></p:GetStatusResponse>')
    return [ptz.encode(), imaging('<s:Exposure><s:Mode>AUTO</s:Mode><s:ExposureTime>100</s:ExposureTime></s:Exposure>').encode(),
            recording(source(track('Error','<t:Error>reported</t:Error>'))).encode()]


def declaration(raw):
    return dict(schema='onvif-report-set-v1',basis=BASIS,camera='caller-camera',run='run-one',citation='supplied relation',
        sources=[dict(role=role,sha256=digest(data),context_token=role+'-token',capture_time_us=i+1,
                      clock='clock-'+role,schema_basis=ROUTES[role][1]) for i,(role,data) in enumerate(zip(ROUTES,raw))])


class ReportSet(unittest.TestCase):
    def common(self, output):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER'); self.assertTrue(reader)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'input.csv'; path.write_text(output)
            run=subprocess.run([reader,str(ROOT/'profiles/declared/onvif-report-set/profile.toml'),str(path),'--allow-equal-time'],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            return json.loads(run.stdout)['observations']

    def test_report_meanings_and_independent_set(self):
        raw=inputs()
        for alternative in (False,True):
            if alternative:
                raw[1]=imaging('').replace('><','>\r\n<').encode()
                raw[2]=recording(state='Idle').encode()
            spec=declaration(raw)
            if alternative:
                spec['camera']='second-camera'; spec['run']='second-run'
                for item, stamp in zip(spec['sources'], (30,10,20)):
                    item['capture_time_us']=stamp
            encoded=json.dumps(spec).encode()
            output,report=convert(raw,encoded); rows=self.common(output)
            self.assertEqual({r['fields']['binding_role'] for r in rows},set(ROUTES))
            self.assertEqual({r['clock_basis'] for r in rows},{'Unknown'})
            self.assertEqual({r['fields']['binding_manifest_sha256'] for r in rows},{digest(encoded)})
            self.assertEqual(report['observations'],len(rows))
            for row in rows:
                role=row['fields']['binding_role']; idx=list(ROUTES).index(role)
                self.assertEqual(row['fields']['binding_input_sha256'],digest(raw[idx]))
                self.assertEqual(row['fields']['record_time_us'],0)
                self.assertEqual(row['fields']['source_capture_time_us'],spec['sources'][idx]['capture_time_us'])
                self.assertEqual(row['fields']['association_clock_basis'],'UNLOCATED_ASSOCIATION_NOT_SYNCHRONIZED')
            im=next(r['fields'] for r in rows if r['fields']['binding_role']=='imaging')
            if not alternative: self.assertAlmostEqual(im['imaging_exposure_time_s'],.0001)
            else: self.assertEqual(im['imaging_exposure_disposition'],'EXPOSURE_NOT_REPORTED')

    def test_binding_refusals(self):
        raw=inputs(); original=declaration(raw)
        for key,value in [('sha256','sha256:'+'0'*64),('role','ptz'),('clock',''),('capture_time_us',True),('schema_basis','same-release')]:
            spec=copy.deepcopy(original); spec['sources'][1][key]=value
            with self.assertRaises(ValueError): convert(raw,json.dumps(spec).encode())
        with self.assertRaises(ValueError): convert(raw[:2],json.dumps(original).encode())
        wrong=list(raw); wrong[0]=b'<bad>'
        spec=declaration(wrong)
        with self.assertRaises(ValueError): convert(wrong,json.dumps(spec).encode())


if __name__=='__main__': unittest.main()
