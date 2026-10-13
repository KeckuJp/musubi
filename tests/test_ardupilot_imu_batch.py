import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from tests.test_ardupilot_record_export import fmt_record, HEAD
from scripts import convert_ardupilot_imu_batch as bridge


def fixture(sensor=0, count=64, mul=100, rate=1000., seq=None, n=7, stamp=100000):
    raw=fmt_record(128,89,'FMT','BBnNZ','Type,Length,Name,Format,Columns')
    raw+=fmt_record(140,31,'ISBH','QHBBHHQf','TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate')
    raw+=fmt_record(141,207,'ISBD','QHHaaa','TimeUS,N,seqno,x,y,z')
    raw+=HEAD+bytes([140])+struct.pack('<QHBBHHQf',stamp,n,sensor,2,mul,count,90000,rate)
    for k in (range(count//32) if seq is None else seq):
        raw+=HEAD+bytes([141])+struct.pack('<QHH',stamp+1000+k,n,k)
        raw+=struct.pack('<96h',*([100]*32+[-200]*32+[300]*32))
    return raw


def declaration(raw):
    return dict(schema='ardupilot-imu-batch/v1',source_pin=bridge.PIN,
        source_sha256='sha256:'+hashlib.sha256(raw).hexdigest(),ins_bat_lgct=32,
        sensor_rate_logging=False,single_boot=True,citation='authored config')


class Batch(unittest.TestCase):
    def test_independent_multiple_batches_reuse(self):
        first=fixture(sensor=1,count=320,mul=200,rate=400,n=12)
        second=fixture(sensor=0,count=64,mul=50,rate=200,n=13,stamp=200000)
        raw=first+second[267:]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.bin';path.write_bytes(raw)
            text,report=bridge.convert(path,json.dumps(declaration(raw)).encode())
            out=Path(directory)/'out.csv';out.write_text(text)
            common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                str(bridge.PROFILE),str(out),'--allow-equal-time'],capture_output=True,check=True).stdout)
            self.assertEqual(len(common['observations']),384)
            self.assertEqual(report['batches'],2)
            a=common['observations'][319]['fields'];b=common['observations'][-1]['fields']
            self.assertEqual(a['angular_rate_x_rad_s'],.5)
            self.assertEqual(a['nominal_offset_s'],.7975)
            self.assertEqual(b['acceleration_z_m_s2'],6)
            self.assertEqual(b['sample_index'],63)

    def test_header_n_clock_layout_and_unknown_records(self):
        good=fixture()
        raw=good+fmt_record(150,4,'FUTR','B','Novel')+(HEAD+bytes([150,7]))*65537
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.bin';path.write_bytes(raw)
            _,report=bridge.convert(path,json.dumps(declaration(raw)).encode())
            self.assertEqual(report['unselected_counts']['FUTR'],65537)
            tail=report['unselected_spans'][-1]
            self.assertEqual(tail['stop_record']-tail['start_record'],65538)
            self.assertEqual(tail['stop_offset'],len(raw))
            self.assertEqual(len(report['unselected_spans']),2)
            variants=[good[:267]+good[298:], # missing header
                good[:309]+struct.pack('<H',8)+good[311:], # wrong N in first ISBD
                good[:301]+struct.pack('<Q',1)+good[309:], # recording clock reset
                good.replace(b'QHHaaa',b'QHHhhh',1)]
            for bad in variants:
                path.write_bytes(bad)
                with self.assertRaises(ValueError):bridge.convert(path,json.dumps(declaration(bad)).encode())

    def test_development_and_important_refusals(self):
        raw=fixture()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.bin';path.write_bytes(raw)
            text,report=bridge.convert(path,json.dumps(declaration(raw)).encode())
            out=Path(directory)/'out.csv';out.write_text(text)
            result=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                str(bridge.PROFILE),str(out),'--allow-equal-time'],capture_output=True,check=True).stdout)
            self.assertEqual(len(result['observations']),64)
            fields=result['observations'][-1]['fields']
            self.assertEqual(fields['acceleration_y_m_s2'],-2)
            self.assertEqual(fields['sample_index'],63)
            self.assertEqual(fields['nominal_offset_s'],.063)
            self.assertEqual(report['samples'],64)
            for key,value in [('source_sha256','bad'),('ins_bat_lgct',16),('sensor_rate_logging',True),('single_boot',False),('source_pin','bad')]:
                bad=declaration(raw);bad[key]=value
                with self.subTest(key=key),self.assertRaises(ValueError):bridge.convert(path,json.dumps(bad).encode())
            for bad in [fixture(sensor=2),fixture(mul=0),fixture(rate=0),fixture(rate=float('nan')),
                        fixture(seq=[1,0]),fixture(seq=[0]),fixture(seq=[0,0]),fixture(count=33),raw[:-1]]:
                path.write_bytes(bad)
                with self.subTest(size=len(bad)),self.assertRaises(ValueError):bridge.convert(path,json.dumps(declaration(bad)).encode())
