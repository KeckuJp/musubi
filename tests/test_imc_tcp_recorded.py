"""Authored offline sessions, not live TCP or actual UUV recordings."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

import dpkt
from scripts.convert_imc_tcp_recorded import convert_path
from tests.test_imc_sensors import frame

FLOW = dict(source_address="192.0.2.1", source_port=6001,
            destination_address="192.0.2.2", destination_port=6002)
ROOT = Path(__file__).resolve().parents[1]


def packet(seq, flags, payload=b"", port=6002):
    tcp = dpkt.tcp.TCP(sport=6001, dport=port, seq=seq & 0xffffffff, flags=flags, data=payload)
    tcp.off = 5
    ip = dpkt.ip.IP(src=b"\xc0\x00\x02\x01", dst=b"\xc0\x00\x02\x02", p=6, data=tcp)
    ip.len = 20 + len(tcp)
    return bytes(dpkt.ethernet.Ethernet(src=bytes(6), dst=b"\x01"*6, type=0x0800, data=ip))


def capture(packets):
    out = io.BytesIO()
    writer = dpkt.pcap.Writer(out)
    for index, value in enumerate(packets):
        writer.writepkt(value, ts=1700000000 + index)
    return out.getvalue()


class SavedImcTCP(unittest.TestCase):
    def test_estimated_state_current_and_explicit_legacy(self):
        from scripts.convert_imc_sensors import NEPTUS_SCHEMA
        from tests.test_imc_estimated_state import packet as estimate, PROFILE
        from tests.saved_can_window_helpers import common
        stream = estimate([-.2, .3, 8, 2, -3, 4, .1, .2, .3, 1, 2, 3, 4, 5, 6, .1, .2, .3, 0, -1])
        raw = capture([packet(50, 2), packet(51, 24, stream[:40]),
                       packet(91, 24, stream[40:]), packet(51+len(stream), 17)])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); path = folder / "tcp.pcap"; path.write_bytes(raw)
            for schema in (None, NEPTUS_SCHEMA):
                output, report = convert_path(path, FLOW, start_us=1700000000000000,
                    stop_us=1700000010000000, schema=schema, estimated_state=True)
                fields = common(self, folder, output, PROFILE)["observations"][0]["fields"]
                self.assertEqual(fields["estimated_offset_east_m"], -3)
                self.assertEqual(fields["record_time_us"], 1700000000250000)
                self.assertEqual(fields["pcap_packet_index"], 2)
                self.assertEqual(report["frames"][0]["message_id"], 350)
            with self.assertRaises(ValueError):
                convert_path(path, FLOW, start_us=0, stop_us=1, estimated_state=1)
            with self.assertRaises(ValueError):
                convert_path(path, FLOW, start_us=0, stop_us=1, estimated_state=True, schema="unknown")

    def test_two_sessions_split_retransmit_wrap_and_common(self):
        for initial, order, temperature in ((50, "<", 20.), (0xfffffffa, ">", 7.5)):
            stream = frame(263, temperature, order) + frame(65000, b"unknown", order) + frame(265, 3., order)
            start = (initial + 1) & 0xffffffff
            chunks = [stream[:8], stream[8:43], stream[43:]]
            packets = [packet(initial, 2), packet(start, 24, chunks[0]),
                       packet(start, 24, chunks[0]), packet(1, 2, port=9000),
                       packet(start+8, 24, chunks[1]), packet(start+43, 24, chunks[2]),
                       packet(start+len(stream), 17)]
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp); path = folder / "capture.pcap"
                original = capture(packets); path.write_bytes(original)
                output, report = convert_path(path, FLOW, start_us=1700000000000000, stop_us=1700000010000000)
                self.assertEqual((report["selected_tcp_packets"], report["unselected_packets"], report["output_rows"]), (6, 1, 2))
                self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(original).hexdigest())
                self.assertEqual(report["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(report["stream_sha256"], hashlib.sha256(stream).hexdigest())
                self.assertEqual([f["output_index"] for f in report["frames"]], [0, None, 1])
                self.assertEqual(report["packets"][2]["disposition"], "IDENTICAL_PAYLOAD_RETRANSMISSION")
                self.assertEqual(report["frames"][0]["packet_spans"][0]["packet_index"], 1)
                self.assertEqual(report["frames"][0]["packet_spans"][-1]["packet_index"], 4)
                for item in report["frames"]:
                    self.assertEqual(sum(s["stream_stop"]-s["stream_start"] for s in item["packet_spans"]), item["stream_stop"]-item["stream_start"])
                output_path = folder / "selected.csv"; output_path.write_text(output)
                run = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                    str(output_path), "--allow-equal-time"], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                observations = json.loads(run.stdout)["observations"]
                self.assertAlmostEqual(observations[0]["fields"]["temperature_k"], temperature+273.15)
                self.assertEqual(observations[0]["fields"]["pcap_packet_index"], 4)
                self.assertEqual(observations[0]["fields"]["record_time_us"], 1700000000250000)
                self.assertEqual(observations[0]["clock_basis"], "Unknown")

    def test_gap_conflict_partial_midstart_end_and_bad_crc_refuse(self):
        body = frame(263, 20.)
        syn, data, fin = packet(50, 2), packet(51, 24, body), packet(51+len(body), 17)
        cases = [
            [data, fin], [syn, data], [syn, packet(52, 24, body), fin],
            [syn, data, packet(51, 24, bytes(len(body))), fin],
            [syn, data, packet(52, 24, body[1:]), fin],
            [syn, data, packet(51+len(body), 4)], [syn, data, syn, fin],
            [syn, packet(51, 24, body[:-1]), packet(50+len(body), 17)],
            [syn, packet(51, 24, body[:-1]+bytes([body[-1]^1])), fin],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.pcap"
            for packets in cases:
                path.write_bytes(capture(packets))
                with self.assertRaises(ValueError):
                    convert_path(path, FLOW, start_us=1700000000000000, stop_us=1700000010000000)
            with self.assertRaises(ValueError):
                convert_path(path, {**FLOW, "source_port": True}, start_us=0, stop_us=1)
