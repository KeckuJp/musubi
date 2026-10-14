"""Authored saved packets and plans, never a guidance execution or machine record."""
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
from scripts import convert_agopen_guidance as bridge
from tests.test_agopen_steering import pcap, datagram, checksum

ROOT=Path(__file__).resolve().parents[1]
TRACK=b'$TrackLines\nAB\n0\n0,0\n0,10\n0\n2\nTrue\n0\n'


def frame(code):
    raw=bytearray(b'\x80\x81\x7f\xfe\x08'+bytes(9)); raw[10]=code
    raw[-1]=checksum(raw); return bytes(raw)


def inputs(codes,track=TRACK):
    cap=pcap([datagram(frame(code)) for code in codes])
    declaration=dict(producer_pin=bridge.PIN,capture_sha256=bridge.digest(cap),
        track_sha256=bridge.digest(track),track_index=0,track_capture_us=777,
        normal_guidance_not_free_drive=True,source_address='192.0.2.1',source_port=6001,
        destination_port=8888)
    return cap,track,declaration


class GuidanceTests(unittest.TestCase):
    def test_two_inputs_connect_saved_geometry_and_quantized_deviation_to_common(self):
        for codes,track in [([117,137,255],TRACK),([0,127,254],TRACK.replace(b'AB',b'Other').replace(b'0,10',b'20,30'))]:
            cap,track,d=inputs(codes,track); files=bridge.convert(cap,track,d)
            self.assertEqual(files['source.pcap'],cap); self.assertEqual(files['TrackLines.txt'],track)
            with tempfile.TemporaryDirectory() as directory:
                base=Path(directory)
                for name,raw in files.items(): (base/name).write_bytes(raw)
                for name,profile,n in [('deviation.csv','agopen-field-folder/guidance-profile.toml',3),
                    ('track.csv','gpx-points/track-profile.toml',2)]:
                    completed=subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(ROOT/'profiles/declared'/profile),str(base/name),
                        '--allow-equal-time'],capture_output=True,check=True)
                    common=json.loads(completed.stdout); self.assertEqual(common['main_rows'],n)
                    self.assertEqual(common['platform_domain'],'Unknown')
                    self.assertTrue(all(x['clock_basis']=='Unknown' for x in common['observations']))
                    if name=='deviation.csv':
                        for code,row in zip(codes,common['observations']):
                            self.assertEqual(row['fields']['lightbar_code'],code)
                            self.assertEqual(row['fields']['quantized_deviation_m'],None if code==255 else (code-127)*.02)
                            self.assertEqual(row['fields']['track_sha256'],d['track_sha256'])
                report=json.loads(files['report.json']); self.assertEqual(report['accounting']['selected_frames'],3)
            rows=list(csv.DictReader(io.StringIO(files['deviation.csv'].decode())))
            for code,row in zip(codes,rows):
                if code in (0,254): self.assertIn('SATURATED',row['deviation_status'])
                if code==255: self.assertEqual(row['deviation_status'],'UNAVAILABLE')

    def test_important_binding_and_corruption_refusals(self):
        cap,track,d=inputs([127])
        for key,value in [('capture_sha256','bad'),('track_sha256','bad'),('producer_pin','wrong'),
                          ('track_index',1),('normal_guidance_not_free_drive',False),('source_port',9999)]:
            bad=copy.deepcopy(d); bad[key]=value
            with self.assertRaises(ValueError): bridge.convert(cap,track,bad)
        for key in ('normal_guidance_not_free_drive','track_index'):
            bad=copy.deepcopy(d); del bad[key]
            with self.assertRaises(ValueError): bridge.convert(cap,track,bad)
        for payload in (frame(1)[:-1],frame(1)[:-1]+b'\x00'):
            malformed=pcap([datagram(payload)]); bad=dict(d,capture_sha256=bridge.digest(malformed))
            with self.assertRaises(ValueError): bridge.convert(malformed,track,bad)

    def test_cli_preserves_originals_and_refuses_existing_output(self):
        cap,track,d=inputs([127,255])
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); (base/'in.pcap').write_bytes(cap); (base/'TrackLines.txt').write_bytes(track)
            (base/'binding.json').write_text(json.dumps(d)); out=base/'out'
            cmd=[sys.executable,str(ROOT/'scripts/convert_agopen_guidance.py'),str(base/'in.pcap'),
                str(base/'TrackLines.txt'),str(base/'binding.json'),str(out)]
            result=subprocess.run(cmd,capture_output=True); self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((out/'source.pcap').read_bytes(),cap)
            self.assertNotEqual(subprocess.run(cmd,capture_output=True).returncode,0)


if __name__=='__main__': unittest.main()
