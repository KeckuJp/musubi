"""Authored PCAP interval to existing ICMP meanings; never a live network claim."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import dpkt
from scripts import convert_imc_sensors as legacy
from scripts import convert_icmp_echo_window as target
from scripts import convert_udp_payload_window as budget
from tests.test_agopen_steering_window import write_capture

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/imc-sensors/profile.toml"


def echo(kind, seq=1, payload=b"odd"):
    message = struct.pack(">BBHHH", kind, 0, 0, 7, seq) + payload
    message = message[:2] + struct.pack(">H", dpkt.in_cksum(message)) + message[4:]
    src, dst = (b"\x0a\0\0\1", b"\x0a\0\0\2")
    if kind == 0:
        src, dst = dst, src
    header = struct.pack(">BBHHHBBH", 0x45, 0, 20+len(message), 1, 0, 64, 1, 0) + src + dst
    header = header[:10] + struct.pack(">H", dpkt.in_cksum(header)) + header[12:]
    return b"\xff"*6 + b"\x11"*6 + b"\x08\0" + header + message


class IcmpEchoWindowTests(unittest.TestCase):
    def common(self, text, path):
        path.write_text(text)
        run = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                              str(path), "--allow-equal-time"], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr.decode())
        result = json.loads(run.stdout)
        for obs in result["observations"]:
            self.assertEqual(obs["clock_basis"], "Unknown")
            self.assertIsNone(obs["anchor_unix_us"])
            self.assertIsNone(obs["t_boot_us"])
            self.assertIsNone(obs["wall_ms"])
            self.assertEqual(obs["time_confidence"],0)
        return result["observations"]

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_large_adjacent_windows_global_pairs_missing_and_equal_times(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "large.pcap"
            records = [(100, b"x" * 1024**2)] * 17 + [
                (200, echo(8,1)), (200,echo(0,1)), (210,echo(8,2)),
                (250,b"\xff"*12+b"\x08\x06"), (300,echo(0,2)),
                (310,echo(8,3)), (320,echo(0,3)), (400,b"x")]
            write_capture(path, records)
            self.assertGreater(path.stat().st_size, legacy.LIMIT)
            digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            seen = []
            for start, stop, expected in ((200,300,[17,19]), (300,400,[21,22])):
                with patch.object(Path, "read_bytes", side_effect=AssertionError("no whole read")):
                    text, report = target.convert_path(path, start_us=start, stop_us=stop)
                observations = self.common(text, root/f"{start}.csv")
                self.assertEqual(observations[0]["t_ms"],0)
                fields = [o["fields"] for o in observations]
                self.assertEqual([r["pcap_packet_index"] for r in fields], expected)
                self.assertEqual([r["record_time_us"] for r in fields], [start,start+10])
                self.assertEqual([r["pcap_source_sha256"] for r in fields], [digest]*2)
                self.assertEqual(report["source_sha256"], digest)
                self.assertNotEqual(report["derived_capture_sha256"], digest)
                self.assertEqual(report["pair_scope"], "SELECTED_CAPTURE_INTERVAL_ONLY")
                window = report["capture_window"]
                self.assertEqual(window["source_packets"], 25)
                self.assertEqual(window["selected_packets"]+window["outside_window_packets"],25)
                self.assertEqual(window["outside_window"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual(report["accounted_selected_packets"],3)
                self.assertEqual(report["selected_packets"],3)
                self.assertEqual(report["unselected_packets"],1 if start==200 else 0)
                if start == 200:
                    self.assertEqual([r["icmp_pair_status"] for r in fields], ["PAIRED","NO_REPLY_IN_CAPTURE"])
                    self.assertEqual(fields[0]["icmp_rtt_us"],0)
                    self.assertEqual(fields[0]["icmp_reply_packet_index"],18)
                    self.assertIsNone(fields[1]["icmp_rtt_us"])
                else:
                    self.assertEqual([r["icmp_pair_status"] for r in fields], ["REPLY_WITHOUT_REQUEST_IN_CAPTURE","PAIRED"])
                    self.assertIsNone(fields[0]["icmp_request_packet_index"])
                    self.assertEqual(fields[1]["icmp_reply_packet_index"],23)
                    self.assertEqual(fields[1]["icmp_rtt_us"],10)
                    self.assertEqual(fields[1]["icmp_rtt_s"],0.00001)
                for row in fields:
                    seen.extend(row[k] for k in ("icmp_request_packet_index","icmp_reply_packet_index") if row[k] is not None)
            self.assertEqual(sorted(seen),[17,18,19,21,22,23])

    def test_invalid_intervals_native_checks_resources_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"input.pcap"
            write_capture(path, [(20,echo(8)),(21,echo(0))])
            for start,stop in ((True,30),(0,None),(-1,30),(20,20),(30,20)):
                with self.subTest(start=start,stop=stop), self.assertRaises(ValueError):
                    target.convert_path(path,start_us=start,stop_us=stop)
            for module,field,value in ((legacy,"PCAP_WINDOW_INPUT_LIMIT",24),
                    (legacy,"PCAP_WINDOW_SELECTED_LIMIT",24),(legacy,"PCAP_WINDOW_RECORD_LIMIT",1),
                    (target,"CSV_LIMIT",1),(budget,"REPORT_LIMIT",1)):
                with self.subTest(field=field), patch.object(module,field,value), self.assertRaises(ValueError):
                    target.convert_path(path,start_us=20,stop_us=30)
            for records in ([(20,echo(8)[:-1]+b"x")],[(20,b"\xff"*12+b"\x08\x06")],
                            [(10,b"x"),(9,b"x"),(20,echo(8))]):
                write_capture(path,records)
                with self.subTest(records=records),self.assertRaises(ValueError):
                    target.convert_path(path,start_us=20,stop_us=30)
            write_capture(path,[(20,echo(8))])
            with path.open("ab") as stream:
                stream.write(b"x")
            with self.assertRaises(ValueError):
                target.convert_path(path,start_us=20,stop_us=30)
            write_capture(path,[(20,echo(8))])
            original = target.read_pcap_window
            def changed(*args):
                result = original(*args)
                with path.open("ab") as stream:
                    stream.write(b"x")
                return result
            with patch.object(target,"read_pcap_window",side_effect=changed), self.assertRaisesRegex(ValueError,"source changed"):
                target.convert_path(path,start_us=20,stop_us=30)

    def test_small_parity_nano_and_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/"small.pcap"
            write_capture(path,[(20,echo(8)),(25,echo(0))])
            old,previous=legacy.convert_icmp_echo(path.read_bytes())
            text,report=target.convert_path(path,start_us=0,stop_us=30)
            self.assertEqual(text,old)
            for key,value in previous.items(): self.assertEqual(report[key],value)
            command=[sys.executable,str(ROOT/"scripts/convert_icmp_echo_window.py"),str(path),str(root/"out"),
                     "--start-us","0","--stop-us","30"]
            run=subprocess.run(command,capture_output=True)
            self.assertEqual(run.returncode,0,run.stderr.decode())
            self.assertEqual((root/"out/observations.csv").read_text(),old)
            self.assertEqual(json.loads((root/"out/report.json").read_text()),report)
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,2)
            self.assertEqual((root/"out/observations.csv").read_text(),old)
            write_capture(path,[(10001,b"x"),(20000,echo(8)),(25000,echo(0))],nano=True)
            text,report=target.convert_path(path,start_us=20,stop_us=30)
            self.assertEqual(next(csv.DictReader(io.StringIO(text)))["icmp_rtt_us"],"5")
            self.assertEqual(report["capture_window"]["original_packet_indexes"],[1,2])
            write_capture(path,[(20001,echo(8))],nano=True)
            with self.assertRaisesRegex(ValueError,"exact microseconds"):
                target.convert_path(path,start_us=20,stop_us=30)


if __name__ == "__main__":
    unittest.main()
