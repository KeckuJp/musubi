"""Independent authored receive bytes; not physical UART/RF qualification."""
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

from scripts import convert_crsf_link as subject
from tests.test_crsf_link import capture

ROOT = Path(__file__).resolve().parents[1]
KIND = "sport-serial-fb8622dd"


def packet(application, value, physical=0x1b):
    # Same authored checksum construction as the adopted packet-level test.
    data = bytes([physical, 0x10]) + struct.pack("<Hi", application, value)
    checksum = sum(data[1:])
    while checksum > 255:
        checksum = (checksum & 255) + (checksum >> 8)
    return data + bytes([255 - checksum])


def wire(data):
    # First physical-ID is literal, all remaining native bytes are stuffed.
    return b"\x7e" + data[:1] + b"".join(
        bytes([0x7d, b ^ 0x20]) if b in (0x7d, 0x7e) else bytes([b])
        for b in data[1:])


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class SportStreamContractTests(unittest.TestCase):
    def common(self, text):
        self.assertIn("MUSUBI_TELEMETRY_READER", os.environ,
                      "actual common reader is required for this integration test")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            command = [os.environ["MUSUBI_TELEMETRY_READER"], str(ROOT /
                "profiles/declared/crsf-link/profile.toml"), str(path)]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            return json.loads(subprocess.run(command + ["--allow-equal-time"],
                              check=True, capture_output=True).stdout)

    def test_escaped_adjacent_unknown_packets_have_exact_spans_and_common(self):
        packets = [packet(0x0210, 0x7e), packet(0x0400, 55), packet(0x020f, -123)]
        spans = [wire(p) for p in packets]
        source = b"".join(spans)
        text, report = subject.convert_sport_stream(source, capture_time_us=1234567)
        output = rows(text)
        self.assertEqual((report["source_records"], report["decoded_records"]), (3, 2))
        self.assertEqual(len(report["unsupported_records"]), 1)
        self.assertEqual(report["clock"], "Unknown")
        digest = hashlib.sha256(source).hexdigest()
        native = report["native_stream"]
        self.assertEqual((native["source_sha256"], native["source_bytes"]), (digest, len(source)))
        self.assertEqual((native["complete_packet_bytes"], native["discarded_bytes"]), (len(source), 0))
        self.assertEqual(native["discarded_ranges"], [])
        self.assertEqual(native["capture_time_basis"], "CALLER_CAPTURE_LABEL_NOT_PACKET_TIME")
        self.assertEqual(native["framing_basis"], "OPENTX_FB8622DD_SPORT_RECEIVE_BYTES")
        self.assertEqual(native["capture_time_us"], 1234567)
        for row, index in zip(output, (0, 2)):
            start = sum(map(len, spans[:index])); stop = start + len(spans[index])
            for key, expected in {"source_input_sha256": "sha256:" + digest,
                    "source_packet_index": str(index), "source_start_offset": str(start),
                    "source_stop_offset_exclusive": str(stop),
                    "source_wire_hex": "hex:" + source[start:stop].hex()}.items():
                self.assertEqual(row[key], expected)
                self.assertEqual(str(json.loads(bytes.fromhex(row["source_record_hex"][4:]))[key]), expected)
            self.assertEqual(row["source_frame_hex"], "hex:" + packets[index].hex())
        unsupported = json.loads(bytes.fromhex(report["unsupported_records"][0]["source_record_hex"]))
        self.assertEqual(str(unsupported["source_packet_index"]), "1")
        common = self.common(text)
        onboard = [o for o in common["observations"] if o["channel"] == "onboard"]
        self.assertEqual(len(onboard), 2)
        self.assertEqual(onboard[0]["fields"]["battery_voltage_v"], 1.26)
        self.assertEqual(onboard[1]["fields"]["battery_current_a"], -12.3)
        self.assertIsNone(onboard[0]["fields"]["battery_current_a"])
        for observation, index in zip(onboard, (0, 2)):
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["t_boot_us"])
            self.assertIsNone(observation["anchor_unix_us"])
            self.assertEqual(observation["fields"]["record_time_us"], 1234567)
            self.assertEqual(observation["fields"]["source_packet_index"], index)
            self.assertEqual(observation["fields"]["source_input_sha256"], "sha256:" + digest)

    def test_literal_first_id_repeated_delimiter_and_xor_delimiter(self):
        literal = packet(0x0210, 94, physical=0x7d)
        ordinary = packet(0x0200, 0)
        # 7d 7e represents 5e inside a packet; it must NOT restart framing.
        escaped = wire(literal).replace(b"\x5e", b"\x7d\x7e", 1)
        source = escaped + b"\x7e" + wire(ordinary)
        text, report = subject.convert_sport_stream(source, capture_time_us=0)
        output = rows(text)
        self.assertEqual([r["source_frame_hex"] for r in output],
                         ["hex:" + literal.hex(), "hex:" + ordinary.hex()])
        self.assertEqual(float(output[0]["battery_voltage_v"]), .94)
        self.assertEqual(float(output[1]["battery_current_a"]), 0)
        native = report["native_stream"]
        self.assertEqual(native["complete_packet_bytes"] + native["discarded_bytes"], len(source))
        self.assertEqual(report["source_records"], 2)

    def test_discard_accounting_and_complete_checksum_refusal(self):
        selected = wire(packet(0x0210, 1200))
        source = b"idle" + b"\x7e\x1b\x10" + selected + b"\x01\x02" + b"\x7e\x1b\x7d"
        text, report = subject.convert_sport_stream(source, capture_time_us=4)
        self.assertEqual(len(rows(text)), 1)
        start = 7; stop = start + len(selected)
        self.assertEqual(report["native_stream"]["discarded_ranges"], [
            {"start_offset": 0, "stop_offset_exclusive": 4, "reason": "UNFRAMED_BYTES"},
            {"start_offset": 4, "stop_offset_exclusive": 7, "reason": "INCOMPLETE_BEFORE_DELIMITER"},
            {"start_offset": stop, "stop_offset_exclusive": stop + 2, "reason": "UNFRAMED_BYTES"},
            {"start_offset": stop + 2, "stop_offset_exclusive": len(source), "reason": "INCOMPLETE_AT_EOF"}])
        self.assertEqual(report["native_stream"]["complete_packet_bytes"], len(selected))
        self.assertEqual(report["native_stream"]["discarded_bytes"], len(source) - len(selected))
        for bad_packet in (packet(0x0210, 1), packet(0x0400, 1)):
            corrupt = bad_packet[:-1] + bytes([bad_packet[-1] ^ 1])
            with self.assertRaises(ValueError):
                subject.convert_sport_stream(selected + wire(corrupt), capture_time_us=4)
        for source in (b"", b"idle", b"\x7e\x1b", wire(packet(0x0400, 1))):
            with self.assertRaisesRegex(ValueError, "no selected frame reports"):
                subject.convert_sport_stream(source, capture_time_us=4)
        with self.assertRaises(ValueError):
            subject.convert_sport_stream(wire(packet(0x0210, -1)), capture_time_us=4)

    def test_time_bounds_cli_and_legacy_parity(self):
        native = wire(packet(0x0210, 1200))
        for time in (-1, 2**63, True, 1.5, "1", None):
            with self.subTest(time=time), self.assertRaises((ValueError, TypeError)):
                subject.convert_sport_stream(native, capture_time_us=time)
        with self.assertRaises((ValueError, TypeError)):
            subject.convert_sport_stream(native.hex(), capture_time_us=1)
        for constant, source in (("LIMIT", native), ("SPORT_MAX_PACKETS", native),
                                 ("SPORT_MAX_DISCARDED_RANGES", b"x" + native)):
            with self.subTest(bound=constant), patch.object(subject, constant, 0), self.assertRaises(ValueError):
                subject.convert_sport_stream(source, capture_time_us=1)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "native.bin"; source.write_bytes(native)
            command = [sys.executable, str(ROOT / "scripts/convert_crsf_link.py"), str(source), str(base / "out")]
            flags = ["--kind", KIND, "--capture-time-us", "1"]
            result = subprocess.run(command + flags, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(rows((base / "out/observations.csv").read_text())[0]["battery_voltage_v"], "12.0")
            self.assertNotEqual(subprocess.run(command + flags, capture_output=True).returncode, 0)
            for flags in (["--kind", KIND], ["--kind", "sport-electrical-fb8622dd", "--capture-time-us", "1"]):
                rejected = command[:]; rejected[-1] = str(base / "bad")
                self.assertNotEqual(subprocess.run(rejected + flags, capture_output=True).returncode, 0)
                self.assertFalse((base / "bad").exists())
            legacy = capture([(1, packet(0x0210, 1200))])
            expected, expected_report = subject.convert(legacy, "sport-electrical-fb8622dd")
            source.write_text(legacy)
            old_command = command[:]; old_command[-1] = str(base / "legacy")
            subprocess.run(old_command + ["--kind", "sport-electrical-fb8622dd"], check=True, capture_output=True)
            self.assertEqual((base / "legacy/observations.csv").read_text(), expected)
            self.assertEqual(json.loads((base / "legacy/report.json").read_text()), expected_report)


if __name__ == "__main__":
    unittest.main()
