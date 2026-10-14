"""Authored captures only; caller binding is not authenticated synchronization."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_rtp_sender_clock as bridge
from tests.test_rtp_records import pcap, datagram, rtp
from tests.test_rtcp_sender_reports import sender_report

ROOT = Path(__file__).resolve().parents[1]


def inputs(seconds=100, fraction=2**31, media=2**32-40, ticks=(2**32-20, 20), rate=80):
    raw = pcap([datagram(rtp(i+10, tick)) for i, tick in enumerate(ticks)])
    control = pcap([datagram(sender_report(seconds=seconds, fraction=fraction, media=media), port=5005)])
    declaration = dict(rtp_sha256=bridge.digest(raw), rtcp_sha256=bridge.digest(control),
        rtp_tuple=['192.0.2.1', 5002, '192.0.2.2', 5004], ssrc=0x11223344,
        payload_type=96, clock_rate_hz=rate, rtcp_port=5005, anchor_capture_index=0,
        anchor_packet_index=0, max_abs_delta_ticks=100000, relation=bridge.BASIS)
    return raw, control, declaration


class SenderClockTests(unittest.TestCase):
    def test_two_anchors_reuse_existing_common_profile_and_exact_precision(self):
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        for args, expected in [({}, [100750000,101250000]),
                ({'seconds':200,'fraction':0,'media':100,'ticks':(99,101),'rate':3},
                 [199666666,200333333])]:
            raw, control, declaration = inputs(**args)
            converted, report = bridge.convert(raw, control, declaration)
            rows = list(csv.DictReader(io.StringIO(converted)))
            self.assertEqual([int(row['sender_microseconds_floor']) for row in rows], expected)
            self.assertEqual(report['selected_packets'], 2)
            self.assertTrue(all(row['sender_seconds_exact'].startswith('ratio:') for row in rows))
            self.assertEqual(report['time_basis'], bridge.BASIS)
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'observations.csv'; path.write_text(converted)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT/'profiles/declared/rtp-recorded/profile.toml'), str(path),
                    '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual([r['fields']['sender_microseconds_floor'] for r in common['observations']], expected)
                self.assertEqual([r['fields']['sender_seconds_exact'] for r in common['observations']],
                                 [r['sender_seconds_exact'] for r in rows])
                if not args:
                    folder = Path(temp)
                    (folder/'rtp.pcap').write_bytes(raw)
                    (folder/'rtcp.pcap').write_bytes(control)
                    (folder/'binding.json').write_text(json.dumps(declaration))
                    command = [sys.executable, '-m', 'scripts.convert_rtp_sender_clock',
                        str(folder/'rtp.pcap'), str(folder/'rtcp.pcap'), str(folder/'binding.json'),
                        str(folder/'out')]
                    subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
                    self.assertEqual((folder/'out/rtp.pcap').read_bytes(),raw)
                    self.assertEqual((folder/'out/rtcp.pcap').read_bytes(),control)
                    self.assertEqual((folder/'out/observations.csv').read_text(),converted)
                    self.assertNotEqual(subprocess.run(command,cwd=ROOT,capture_output=True).returncode,0)

    def test_important_binding_clock_and_interval_refusals(self):
        raw, control, declaration = inputs()
        for key, value in [('rtp_sha256','sha256:wrong'), ('anchor_capture_index',1),
                           ('anchor_packet_index',1), ('ssrc',99), ('max_abs_delta_ticks',19),
                           ('clock_rate_hz',True), ('relation','inferred'),
                           ('rtp_tuple',['192.0.2.99',5002,'192.0.2.2',5004])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.convert(raw, control, dict(declaration, **{key:value}))
        for args in [dict(seconds=0,fraction=0), dict(media=0,ticks=(2**31,)),
                     dict(seconds=0,fraction=1,media=100,ticks=(0,),rate=1),
                     dict(seconds=2**32-1,fraction=0,media=0,ticks=(100,),rate=1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                bridge.convert(*inputs(**args))
        with self.assertRaises(ValueError):
            bridge.convert(raw, control, dict(declaration, extra='unknown'))
        discontinuous = pcap([datagram(rtp(10,100)),datagram(rtp(30000,101))])
        with self.assertRaises(ValueError):
            bridge.convert(discontinuous,control,dict(declaration,rtp_sha256=bridge.digest(discontinuous)))


if __name__ == '__main__':
    unittest.main()
