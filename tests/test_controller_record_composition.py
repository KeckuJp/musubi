import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_controller_record_composition as m


def csv_bytes(fields,rows):
    out=io.StringIO(); w=csv.DictWriter(out,fields,lineterminator='\n'); w.writeheader(); w.writerows(rows)
    return out.getvalue().encode()


def fixture(node=7):
    raw=csv_bytes(m.can.META+m.can.FIELDS,[
        dict(record_time_us=100,source_node_id=node,data_type='NodeStatus',transfer_kind='MESSAGE',node_reported_mode='OPERATIONAL',node_mode_code=0,node_health_code=0),
        dict(record_time_us=101,source_node_id=node,data_type='GetNodeInfo.Response',transfer_kind='SERVICE_RESPONSE',node_software_major_reported=1,node_software_minor_reported=2,node_software_version_disposition='REPORTED_BY_NODE_NOT_VERIFIED'),
        dict(record_time_us=102,source_node_id=node,data_type='ESCStatus',transfer_kind='MESSAGE',esc_voltage_v=12),
        dict(record_time_us=103,source_node_id=8 if node==7 else 7,data_type='ESCStatus',transfer_kind='MESSAGE',esc_voltage_v=99)])
    # Existing decoder, not a new synthetic diagnostic parser.
    diag,_=m.can.convert_canopen('time_us,standard_id,data_hex\n1,087,0010010000000000\n',include_emergency=True)
    inputs=[raw,diag.encode()]
    sources=[]
    for kind,data,key,clock,start,stop in [('dronecan',raw,node,'clock-A',100,103),('canopen',diag.encode(),7,'clock-B',0,10)]:
        sources.append(dict(format=kind,sha256=m.digest(data),profile_sha256=m.digest((m.ROOT/'profiles/declared'/m.FORMATS[kind]).read_bytes()),key=key,clock=clock,start_us=start,stop_us=stop,dictionary_sha256=None))
    spec=dict(schema='controller-record-composition-v1',basis=m.BASIS,session='session-'+str(node),entity='declared-controller',citation='authored setup relation',sources=sources)
    return inputs,spec


class ControllerCompositionTests(unittest.TestCase):
    def test_two_sessions_common_original_and_no_carry(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER'); self.assertTrue(reader,'actual reader required')
        for node in (7,42):
            inputs,spec=fixture(node); before=list(inputs)
            output,report=m.convert(inputs,json.dumps(spec).encode())
            self.assertTrue(report['complete']); self.assertEqual(inputs,before)
            self.assertEqual(report['sources'][0]['excluded'],1)
            self.assertEqual(report['output_rows'],4)
            rows=list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['source_capture_time_us'] for r in rows],['100','101','102','1'])
            self.assertTrue(all(r['record_time_us']=='0' for r in rows))
            with tempfile.TemporaryDirectory() as tmp:
                for i,data in enumerate(inputs+[output.encode()]):
                    path=Path(tmp)/str(i); path.write_bytes(data)
                    profile=m.ROOT/'profiles/declared'/(m.FORMATS[spec['sources'][i]['format']] if i<2 else 'controller-record-composition/profile.toml')
                    common=json.loads(subprocess.run([reader,str(profile),str(path),'--allow-equal-time'],check=True,capture_output=True).stdout)
                    if i==0:
                        self.assertEqual(common['observations'][2]['fields']['esc_voltage_v'],12)
                        self.assertIsNone(common['observations'][2]['fields']['node_software_major_reported'])
                    if i==2: self.assertEqual(common['main_rows'],4)

    def test_bad_bindings_and_incomplete(self):
        inputs,spec=fixture()
        for key,value in [('sha256','sha256:'+'0'*64),('profile_sha256','wrong'),('key',6),('start_us',103),('stop_us',100),('format','arbitrary')]:
            bad=json.loads(json.dumps(spec)); bad['sources'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): m.convert(inputs,json.dumps(bad).encode())
        spec['sources'][0]['stop_us']=101
        _,report=m.convert(inputs,json.dumps(spec).encode())
        self.assertFalse(report['complete']); self.assertEqual(report['missing_roles'],['firmware','signal'])
        bad=json.loads(json.dumps(spec)); bad['sources'][0]['role']='firmware'
        with self.assertRaises(ValueError): m.convert(inputs,json.dumps(bad).encode())

    def test_alternate_diagnostic_and_dictionary_signal(self):
        inputs,spec=fixture()
        fields=['record_time_us','source_node_id','source_frames_hex','j1939_pgn','diagnostic_report_kind','spn_reported','fmi_reported','occurrence_count_code']
        dtc=dict(record_time_us=2,source_node_id=9,source_frames_hex='hex:00',j1939_pgn=65226,diagnostic_report_kind='ACTIVE_DTC_REPORTED',spn_reported=123,fmi_reported=3,occurrence_count_code=1)
        data=csv_bytes(fields,[dtc]); inputs[1]=data
        spec['sources'][1].update(format='j1939',sha256=m.digest(data),key=9)
        dictionary='sha256:'+'1'*64
        signal=dict(record_time_us=3,frame_id=123,frame_kind='standard',dbc_sha256=dictionary,signal_name_hex='hex:76',source_data_hex='hex:00',voltage_v=24)
        self.assertIn('voltage_v',m.dbc.FIELDS)
        data=csv_bytes(m.dbc.FIELDS,[signal]); inputs.append(data)
        spec['sources'].append(dict(format='dbc',sha256=m.digest(data),profile_sha256=m.digest((m.ROOT/'profiles/declared'/m.FORMATS['dbc']).read_bytes()),key=123,clock='clock-C',start_us=0,stop_us=10,dictionary_sha256=dictionary))
        output,report=m.convert(inputs,json.dumps(spec).encode())
        self.assertTrue(report['complete']); self.assertEqual(report['output_rows'],5)
        reader=os.environ['MUSUBI_TELEMETRY_READER']
        with tempfile.TemporaryDirectory() as tmp:
            for i in (1,2):
                path=Path(tmp)/str(i); path.write_bytes(inputs[i])
                profile=m.ROOT/'profiles/declared'/m.FORMATS[spec['sources'][i]['format']]
                common=json.loads(subprocess.run([reader,str(profile),str(path)],check=True,capture_output=True).stdout)
                self.assertEqual(common['main_rows'],1)
        dtc['fmi_reported']=32; inputs[1]=csv_bytes(fields,[dtc]); spec['sources'][1]['sha256']=m.digest(inputs[1])
        with self.assertRaises(ValueError): m.convert(inputs,json.dumps(spec).encode())

    def test_unknown_and_nonfinite_do_not_fulfil_role(self):
        inputs,spec=fixture()
        rows=list(csv.DictReader(io.StringIO(inputs[0].decode())))
        rows[0]['node_mode_code']='99'; rows[0]['node_reported_mode']='UNKNOWN_99'
        inputs[0]=csv_bytes(m.can.META+m.can.FIELDS,rows); spec['sources'][0]['sha256']=m.digest(inputs[0])
        _,report=m.convert(inputs,json.dumps(spec).encode())
        self.assertEqual(report['missing_roles'],['state'])
        rows[2]['esc_current_a']='nan'
        inputs[0]=csv_bytes(m.can.META+m.can.FIELDS,rows); spec['sources'][0]['sha256']=m.digest(inputs[0])
        with self.assertRaises(ValueError): m.convert(inputs,json.dumps(spec).encode())

    def test_j1939_source_zero_diagnostic_common(self):
        inputs,spec=fixture()
        diagnostic,_=m.can.convert_j1939('time_us,extended_id,data_hex\n2,18feca00,00007b000301ffff\n')
        inputs[1]=diagnostic.encode()
        spec['sources'][1].update(format='j1939',sha256=m.digest(inputs[1]),key=0)
        output,report=m.convert(inputs,json.dumps(spec).encode())
        self.assertTrue(report['complete'])
        rows=list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[-1]['source_key'],'0')
        self.assertEqual(rows[-1]['qualified_roles'],'diagnostic')
        reader=os.environ['MUSUBI_TELEMETRY_READER']
        with tempfile.TemporaryDirectory() as tmp:
            for name,data,profile in [('diagnostic',inputs[1],m.FORMATS['j1939']),('association',output.encode(),'controller-record-composition/profile.toml')]:
                path=Path(tmp)/name; path.write_bytes(data)
                common=json.loads(subprocess.run([reader,str(m.ROOT/'profiles/declared'/profile),str(path),'--allow-equal-time'],check=True,capture_output=True).stdout)
                if name=='diagnostic':
                    self.assertEqual(common['observations'][0]['fields']['source_node_id'],0)
                    self.assertEqual(common['observations'][0]['fields']['spn_reported'],123)
                else:
                    self.assertEqual(common['observations'][-1]['fields']['source_key'],0)
