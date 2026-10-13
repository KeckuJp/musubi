import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts import convert_blueos_resources as bridge


def source():
    return dict(cpu=[dict(name='cpu0',frequency=1500,usage=25,brand='authored',vendor_id='A')],
        memory=dict(ram=dict(total_kB=1000,used_kB=250),swap=dict(total_kB=0,used_kB=0)),
        temperature=[dict(name='soc',temperature=30,maximum_temperature=80,critical_temperature=None)],
        network=[dict(name='eth0',is_up=True,is_loopback=False,total_received_B=2**53+1,
            total_transmitted_B=200,received_B=30,transmitted_B=20,packets_received=2,
            packets_transmitted=3,total_packets_received=200,total_packets_transmitted=300,
            errors_on_received=0,errors_on_transmitted=0,total_errors_on_received=0,total_errors_on_transmitted=0)],
        info=dict(host_name='authored-host',system_name='Linux',os_version='X',kernel_version='Y'),
        unix_time_seconds=123.125,unknown=dict(retain=True))


def bind(raw):
    return json.dumps(dict(schema='blueos-saved-resources/v1',source_pin=bridge.PIN,
        source_sha256=bridge.sha(raw),capture_time_us=99,session='authored-A',citation='saved consumer schema')).encode()


def common(text):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'out.csv';path.write_text(text)
        return json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],str(bridge.PROFILE),
            str(path),'--allow-equal-time'],capture_output=True,check=True).stdout)['observations']


class BlueOS(unittest.TestCase):
    def test_cli_original_precision_and_no_overwrite(self):
        raw=json.dumps(source()).encode().replace(b'"retain": true',b'"retain": 1.12345678901234567890123456789')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'in.json';path.write_bytes(raw)
            binding=root/'binding.json';binding.write_bytes(bind(raw));out=root/'out'
            command=[sys.executable,str(bridge.ROOT/'scripts/convert_blueos_resources.py'),str(path),str(binding),str(out)]
            subprocess.run(command,check=True,capture_output=True)
            self.assertEqual((out/'source-response.json').read_bytes(),raw)
            before=(out/'observations.csv').read_bytes()
            again=subprocess.run(command,capture_output=True)
            self.assertNotEqual(again.returncode,0)
            self.assertEqual((out/'source-response.json').read_bytes(),raw)
            self.assertEqual((out/'observations.csv').read_bytes(),before)

    def test_independent_response_reuse_and_missing(self):
        # Added after development implementation passed; not used to tune it.
        obj=source()
        obj['cpu']=[dict(name=f'cpu{i}',frequency=800.5,usage=i%100) for i in range(128)]
        obj['info']['host_name']='independent-B'
        obj['memory']['ram']['used_kB']=None
        obj['temperature']=[];obj['network'][0]['is_up']=False
        raw=json.dumps(obj).encode().replace(b'123.125',b'123.1234567890123456789')
        result,report=bridge.convert(raw,bind(raw));rows=common(result)
        self.assertEqual(len(rows),131)
        self.assertEqual(rows[127]['fields']['cpu_frequency_hz'],800500000)
        self.assertEqual(rows[128]['fields']['memory_ratio_state'],'MISSING_QUANTITY')
        self.assertEqual(rows[-1]['fields']['is_up_reported'],0)
        self.assertEqual(rows[0]['fields']['reported_unix_seconds_hex'],'hex:'+b'123.1234567890123456789'.hex())
        self.assertIn('temperature',report['absent_resources'])
        self.assertEqual(rows[0]['fields']['capture_time_us'],99)

    def test_missing_wrong_types_and_closed_binding(self):
        raw=json.dumps(source()).encode()
        for key,value in [('capture_time_us',True),('source_pin','wrong'),('extra',1)]:
            binding=json.loads(bind(raw));binding[key]=value
            with self.assertRaises(ValueError):bridge.convert(raw,json.dumps(binding).encode())
        for obj in [dict(info={}),dict(source(),cpu={}),dict(source(),unix_time_seconds=True)]:
            bad=json.dumps(obj).encode()
            with self.assertRaises(ValueError):bridge.convert(bad,bind(bad))
        bad=raw.replace(b'"usage": 25',b'"usage": 25, "usage": 30')
        with self.assertRaises(ValueError):bridge.convert(bad,bind(bad))

    def test_development_meanings_and_refusals(self):
        raw=json.dumps(source()).encode();text,report=bridge.convert(raw,bind(raw));rows=common(text)
        fields=[row['fields'] for row in rows]
        self.assertEqual(fields[0]['cpu_frequency_hz'],1500000000)
        self.assertEqual(fields[0]['cpu_usage_fraction'],.25)
        self.assertEqual(fields[1]['memory_used_fraction'],.25)
        self.assertEqual(fields[2]['memory_ratio_state'],'ZERO_TOTAL_NO_RATIO')
        self.assertEqual(fields[3]['temperature_k'],303.15)
        self.assertEqual(fields[4]['total_received_B'],2**53+1)
        self.assertEqual(report['source']['unknown'],{'retain':True})
        self.assertEqual(fields[0]['reported_unix_seconds_hex'],'hex:'+b'123.125'.hex())
        for edit in ('overusage','overmemory','negativefrequency','badcounter','badtemperature'):
            obj=source()
            if edit=='overusage':obj['cpu'][0]['usage']=101
            elif edit=='overmemory':obj['memory']['ram']['used_kB']=1001
            elif edit=='negativefrequency':obj['cpu'][0]['frequency']=-1
            elif edit=='badcounter':obj['network'][0]['total_received_B']=1.5
            else:obj['temperature'][0]['temperature']=-274
            bad=json.dumps(obj).encode()
            with self.subTest(edit=edit),self.assertRaises(ValueError):bridge.convert(bad,bind(bad))
        bad=json.loads(bind(raw));bad['source_sha256']='bad'
        with self.assertRaises(ValueError):bridge.convert(raw,json.dumps(bad).encode())
