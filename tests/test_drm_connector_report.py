import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_drm_connector_report as converter
ROOT=Path(__file__).resolve().parents[1]
PROFILE=ROOT/'profiles/declared/drm-connector-report/profile.toml'


def connector(cid,value=0):
    return dict(id=cid,type=10,status=1,properties={'link-status':dict(
        id=7,flags=8,type=8,raw_value=value,value=value,
        spec=[dict(name='Good',value=0),dict(name='Bad',value=1)])})


class DrmConnectorTests(unittest.TestCase):
    def test_two_saved_layouts_reuse_common_and_keep_missing_unknown_and_ids(self):
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        for source,expected in [({'/dev/dri/card0':{'connectors':[connector(20),connector(21,1)]}},
                ['GOOD_REPORTED','BAD_REPORTED']),
                ({'/dev/dri/card2':{'connectors':[connector(70,2**64-1),dict(id=71,type=11,status=2,properties={})]},
                  '/dev/dri/card3':{'connectors':[dict(id=71,type=10,status=3,properties=None)]}},
                ['UNKNOWN_REPORTED_VALUE','UNKNOWN','UNKNOWN'])]:
            raw=json.dumps(source).encode()
            converted,report=converter.convert(raw,1000000)
            self.assertEqual(report['connectors'],len(expected))
            with tempfile.TemporaryDirectory() as temp:
                folder=Path(temp); path=folder/'observations.csv'; path.write_text(converted)
                common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],str(PROFILE),str(path),
                    '--allow-equal-time'],check=True,capture_output=True).stdout)
                fields=[r['fields'] for r in common['observations']]
                self.assertEqual([r['link_status'] for r in fields],expected)
                if len(expected)==3:
                    self.assertEqual(fields[0]['link_value_hex'],'hex:ffffffffffffffff')
                    self.assertEqual(fields[1]['property_disposition'],'LINK_STATUS_NOT_REPORTED')
                    self.assertEqual(fields[2]['property_disposition'],'PROPERTIES_UNAVAILABLE')
                saved=folder/'input.json'; saved.write_bytes(raw)
                cmd=[sys.executable,'-m','scripts.convert_drm_connector_report',str(saved),str(folder/'out'),
                     '--capture-time-us','1000000']
                subprocess.run(cmd,cwd=ROOT,check=True,capture_output=True)
                self.assertEqual((folder/'out/source.json').read_bytes(),raw)
                self.assertEqual((folder/'out/observations.csv').read_text(),converted)

    def test_malformed_enum_shape_and_duplicate_are_rejected(self):
        base={'node':{'connectors':[connector(1)]}}
        for key,value in [('type',2),('flags',2),('value',1),('raw_value',-1),('spec',[dict(name='Bad',value=0)])]:
            source=copy.deepcopy(base); source['node']['connectors'][0]['properties']['link-status'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                converter.convert(json.dumps(source).encode(),0)
        for raw in [b'{"node":{},"node":{}}',b'[]',b'{"node":{"connectors":[]}}',
                    json.dumps({'node':{'connectors':[connector(1),connector(1)]}}).encode()]:
            with self.assertRaises(ValueError): converter.convert(raw,0)
        with self.assertRaises(ValueError): converter.convert(json.dumps(base).encode(),True)


if __name__=='__main__': unittest.main()
