import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_rmf_path_association as c

def inputs(name='robot', empty=False):
    location = dict(t=dict(sec=5,nanosec=0),x=1,y=2,yaw=.5,level_name='ROS-L2',index=3,
                    obey_approach_speed_limit=True,approach_speed_limit=1.25)
    web = dict(schema_commit='a736ddebeee9eaf3970756d7dd452065273da918',capture_time_us=1000,
        location_unit='m',yaw_unit='rad',state=dict(robots={name:dict(status='idle',battery=.5,
        location=dict(map='WEB-L1',x=9,y=8,yaw=1),issues=[],commission=dict(dispatch_tasks=False))}))
    ros = dict(schema_commit='6990fc7418c412234b4ff64f01560d8bf3047f46',capture_time_us=2000,
        location_unit='m',yaw_unit='rad',state=dict(name=name+'-ros',model='model',task_id='task',seq=7,
        mode=dict(mode=2,mode_request_id=3,performing_action=''),battery_percent=80,
        location=location,path=[] if empty else [location,dict(location,index=4,x=3)]))
    w=json.dumps(web).encode(); r=json.dumps(ros).encode()
    d=dict(schema='rmf-path-association/v1',web_sha256=c.digest(w),ros_sha256=c.digest(r),
        web_record_index=0,ros_record_index=0,web_robot_key=name,ros_robot_name=name+'-ros',
        association_id='declared-pair',citation='authored declaration, not real identity')
    return w,r,d

class AssociationTests(unittest.TestCase):
    def test_two_pairs_common_preserve_roles_frames_clocks_and_empty_path(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader,'required reader stage')
        for name,empty in (('one',False),('two',True)):
            w,r,d=inputs(name,empty)
            outputs,originals,report=c.convert(w,r,json.dumps(d).encode())
            self.assertEqual(originals['ros-state-path'],c.existing.convert_json(r.decode(),c.existing.RMF_FORMAT))
            with tempfile.TemporaryDirectory() as directory:
                for role,text in outputs.items():
                    path=Path(directory)/(role+'.csv');path.write_text(text)
                    actual=json.loads(subprocess.run([reader,'profiles/declared/electrical-schema-reuse/json-profile.toml',str(path),'--allow-equal-time'],check=True,capture_output=True).stdout)
                    self.assertEqual(len(actual['observations']),report['counts'][role]['bound_rows'])
                    base=list(csv.DictReader(io.StringIO(originals[role])))
                    annotated=list(csv.DictReader(io.StringIO(text)))
                    self.assertEqual([{k:row[k] for k in base[0]} for row in annotated],base)
                    for obs in actual['observations']:
                        self.assertEqual(obs['fields']['association_id_hex'],'hex:'+b'declared-pair'.hex())
                        self.assertEqual(obs['clock_basis'],'Unknown')
                    if role=='ros-state-path':
                        self.assertEqual(len(actual['observations']),1 if empty else 3)
                        self.assertEqual(actual['observations'][0]['fields']['rmf_map_name_hex'],'hex:'+b'ROS-L2'.hex())
                    if role=='web-state':
                        self.assertEqual(actual['observations'][0]['fields']['rmf_map_name_hex'],'hex:'+b'WEB-L1'.hex())

    def test_mismatch_missing_units_and_indices_refuse(self):
        w,r,d=inputs()
        for key,value in (('web_sha256','wrong'),('ros_robot_name','wrong'),('web_robot_key','wrong'),
                          ('web_record_index',1),('ros_record_index',True),('citation','')):
            with self.subTest(key=key),self.assertRaises(ValueError):
                c.convert(w,r,json.dumps(dict(d,**{key:value})).encode())
        for field in ('path','location'):
            modified=json.loads(r);del modified['state'][field]
            raw=json.dumps(modified).encode();declaration=dict(d,ros_sha256=c.digest(raw))
            with self.assertRaises((ValueError,KeyError)):
                c.convert(w,raw,json.dumps(declaration).encode())
        modified=json.loads(w);modified['location_unit']='ft';raw=json.dumps(modified).encode()
        with self.assertRaises(ValueError):
            c.convert(raw,r,json.dumps(dict(d,web_sha256=c.digest(raw))).encode())
        # Identical snapshots are not implicitly deduplicated or joined by matching clocks.
        repeated=w+b'\n'+w
        selected=dict(d,web_sha256=c.digest(repeated),web_record_index=1)
        outputs,_,counts=c.convert(repeated,r,json.dumps(selected).encode())
        rows=list(csv.DictReader(io.StringIO(outputs['web-state'])))
        self.assertEqual(rows[0]['association_basis'],'UNBOUND_SOURCE_ROW')
        self.assertEqual(rows[1]['association_input_record_index'],'1')
        self.assertEqual(counts['counts']['web-state'],dict(bound_rows=1,unbound_rows=1))

if __name__=='__main__':unittest.main()
