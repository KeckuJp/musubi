"""Authored fixed MSPv1 streams; saved framing checks, not real UART evidence."""
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import convert_crsf_link as subject
from tests.test_crsf_link import capture, frame
from tests.test_sport_stream_contract import packet, wire

ROOT = Path(__file__).resolve().parents[1]
KIND = "msp-serial-attitude-bf452"


def reply(payload, command=108, direction=b">"):
    body = bytes([len(payload), command]) + payload
    checksum = 0
    for byte in body:
        checksum ^= byte
    return b"$M" + direction + body + bytes([checksum])


def attitude(roll=-123, pitch=456, yaw=270):
    return reply(struct.pack("<hhH", roll, pitch, yaw))


class MspStreamTests(unittest.TestCase):
    def test_signed_boundary_repeat_unknown_and_native_accounting_reach_common(self):
        packets = [attitude(), reply(b"$M>\x00", 1), attitude(1800, -900, 360), attitude()]
        source = b"idle" + b"".join(packets) + b"junk$M>\x06\x6c\x01"
        text, report = subject.convert_msp_stream(source, capture_time_us=1234567)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual((report["source_records"], report["decoded_records"]), (4, 3))
        self.assertEqual([int(r["source_packet_index"]) for r in rows], [0, 2, 3])
        self.assertEqual(len(report["unsupported_records"]), 1)
        self.assertEqual(report["clock"], "Unknown")
        native = report["native_stream"]
        self.assertEqual(native["complete_packet_bytes"], sum(map(len, packets)))
        self.assertEqual(native["complete_packet_bytes"] + native["discarded_bytes"], len(source))
        self.assertEqual([r["reason"] for r in native["discarded_ranges"]],
                         ["UNFRAMED_BYTES", "UNFRAMED_BYTES", "INCOMPLETE_AT_EOF"])
        for item in native["discarded_ranges"]:
            self.assertEqual(item["source_wire_hex"], "hex:" + source[item["start_offset"]:item["stop_offset_exclusive"]].hex())
        for row, index in zip(rows, (0, 2, 3)):
            start = 4 + sum(map(len, packets[:index]))
            self.assertEqual(int(row["source_start_offset"]), start)
            self.assertEqual(int(row["source_stop_offset_exclusive"]), start + len(packets[index]))
            self.assertEqual(row["source_wire_hex"], "hex:" + packets[index].hex())
            self.assertEqual(row["source_input_sha256"], "sha256:" + hashlib.sha256(source).hexdigest())
            self.assertEqual(row["record_time_us"], "1234567")
        self.assertIn("MUSUBI_TELEMETRY_READER", os.environ)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"; path.write_text(text)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/crsf-link/msp-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        self.assertIn("rad", common["profile_units"]["reported_roll_rad"])
        for observation, values in zip(common["observations"], ((-123, 456, 270), (1800, -900, 360), (-123, 456, 270))):
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["t_boot_us"])
            self.assertIsNone(observation["anchor_unix_us"])
            for key, value, scale in zip(("roll", "pitch", "yaw"), values, (10, 10, 1)):
                self.assertAlmostEqual(observation["fields"][f"reported_{key}_rad"], math.radians(value / scale))
            self.assertEqual(observation["fields"]["attitude_wire_basis"], "BF452_MSP1_REPORTED_FRAME_UNQUALIFIED")

    def test_prefixes_unsupported_and_refusals_are_explicit(self):
        valid = attitude()
        for tail in (b"$", b"$M", b"$M>", b"$M>\x06", valid[:-1]):
            _, report = subject.convert_msp_stream(valid + tail, capture_time_us=0)
            item = report["native_stream"]["discarded_ranges"][-1]
            self.assertEqual((item["reason"], item["source_wire_hex"]), ("INCOMPLETE_AT_EOF", "hex:" + tail.hex()))
        corrupt = valid[:-1] + bytes([valid[-1] ^ 1])
        for bad in (corrupt, reply(b"x", 1)[:-1] + b"\xff", reply(bytes(6), direction=b"<"),
                    reply(bytes(6), direction=b"!"), b"$X>\x00", b"$M>\xff", reply(bytes(6), 255),
                    reply(bytes(5)), attitude(1801, 0, 0), attitude(0, 901, 0), attitude(0, 0, 361)):
            with self.subTest(bad=bad.hex()), self.assertRaises(ValueError):
                subject.convert_msp_stream(valid + bad, capture_time_us=0)
        for data in (b"", b"noise", b"$M", reply(b"x", 1)):
            with self.assertRaisesRegex(ValueError, "no selected frame reports"):
                subject.convert_msp_stream(data, capture_time_us=0)

    def test_explicit_time_and_resource_limits(self):
        valid = attitude()
        for time in (None, True, -1, 2**63, "1", 1.0):
            with self.subTest(time=time), self.assertRaises(ValueError):
                subject.convert_msp_stream(valid, capture_time_us=time)
        with self.assertRaises(ValueError): subject.convert_msp_stream("not bytes", capture_time_us=0)
        for constant, data in (("LIMIT", valid), ("MSP_MAX_PACKETS", valid), ("MSP_MAX_DISCARDED_RANGES", b"x" + valid)):
            with patch.object(subject, constant, 0), self.assertRaises(ValueError):
                subject.convert_msp_stream(data, capture_time_us=0)

    def test_cli_and_existing_default_bytes_unchanged(self):
        for (text, report), expected in zip((subject.convert(capture([(1, frame())])),
                subject.convert_sport_stream(wire(packet(0x0210, 1200)), capture_time_us=1)), (
                ("a34af3d557de9209474f96487f6da9b54338ef0b1835e6b0150ec17249c32f50", "f741447e01a75e83056932fc0ed67694e7539ce2963a3ba8f843a1ed21e89565"),
                ("4f171089ce1f56cd02a5ef040b660b198f52cf834f6685e3df982b06fe1d9ec7", "26247e701f880a69e511c2e6323848e7ddc74083b28d94d78dae1fad85ffdc08"))):
            self.assertEqual((hashlib.sha256(text.encode()).hexdigest(),
                              hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()), expected)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "native.bin"; source.write_bytes(attitude())
            command = [sys.executable, str(ROOT / "scripts/convert_crsf_link.py"), str(source), str(base / "out")]
            flags = ["--kind", KIND, "--capture-time-us", "9"]
            completed = subprocess.run(command + flags, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual((base / "out/observations.csv").read_text(), subject.convert_msp_stream(source.read_bytes(), capture_time_us=9)[0])
            self.assertEqual(subprocess.run(command + flags, capture_output=True).returncode, 2)
            command[-1] = str(base / "bad")
            for flags in (["--kind", KIND], ["--kind", "link", "--capture-time-us", "9"]):
                self.assertEqual(subprocess.run(command + flags, capture_output=True).returncode, 2)
                self.assertFalse((base / "bad").exists())
