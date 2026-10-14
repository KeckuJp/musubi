"""Authored saved packets; no DDS process, device, or real capture."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_rtp_records import datagram, pcap
from tests.test_rosbag_observations import samples, STORE, BATTERY

ROOT = Path(__file__).resolve().parents[1]
PREFIX = bytes.fromhex("0102030405060708090a0b0c")
WRITER = bytes.fromhex("00000103")
PORT = 7411


def submessage(kind, body=b"", flags=1, length=None):
    return bytes([kind, flags]) + struct.pack("<H" if flags & 1 else ">H",
                                             len(body) if length is None else length) + body


def data_message(raw=None, *, seq=1, order="<", writer=WRITER, offset=16, flags=None, length=None):
    raw = bytes(STORE.serialize_cdr(samples()[0], BATTERY)) if raw is None else raw
    body = struct.pack(order + "HH", 0, offset) + bytes(4) + writer + struct.pack(order + "iI", 0, seq)
    return submessage(0x15, body + raw, (5 if order == "<" else 4) if flags is None else flags, length)


def wire(*messages, prefix=PREFIX):
    return b"RTPS" + bytes([2, 3, 1, 15]) + prefix + b"".join(messages)


def capture(*payloads):
    return pcap([datagram(payload, port=PORT) for payload in payloads])


def binding(raw):
    return {"source_input_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "writer_guid_hex": "hex:" + (PREFIX + WRITER).hex(), "schema": "ros2-humble-battery-cdr1"}


class RtpsDataTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.convert_rtps_data")

    def test_last_length_zero_context_reset_and_all_packet_accounting(self):
        raw = capture(wire(submessage(1), submessage(0x09, struct.pack("<II", 9, 0x80000000)),
                           data_message(length=0)),
                      b"not RTPS", wire(data_message(seq=2)))
        text, report = self.module.convert_pcap_rtps(raw, PORT, binding=binding(raw))
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rtps_timestamp_fraction"], str(0x80000000))
        self.assertEqual(rows[1]["rtps_timestamp_disposition"], "NOT_REPORTED")
        self.assertEqual(rows[1]["rtps_timestamp_seconds"], "")
        self.assertEqual([p["frame_hex"] for p in report["packets"]],
                         ["hex:" + datagram(payload, port=PORT).hex() for payload in
                          (wire(submessage(1), submessage(0x09, struct.pack("<II", 9, 0x80000000)),
                                data_message(length=0)), b"not RTPS", wire(data_message(seq=2)))])
        self.assertEqual(report["source_packets"], 3)
        self.assertEqual(report["rtps_datagrams"], 2)
        self.assertEqual(report["submessages"], 4)
        self.assertEqual(report["selected_data"], report["output_records"])
        self.assertEqual(report["packets"][1]["disposition"], "UNSUPPORTED_NON_RTPS_DATAGRAM")

    def test_framing_failures_and_all_explicit_resource_bounds(self):
        raw = capture(wire(data_message()))
        failures = [wire(data_message(flags=7)), wire(data_message(offset=20)),
                    wire(submessage(0x0c, bytes(20))), wire(submessage(0x16, bytes(20))),
                    wire(submessage(0x31, bytes(4))), wire(submessage(0x80, bytes(4))),
                    wire(data_message(seq=0)), wire(data_message(length=65535)),
                    wire(submessage(0x09, bytes(8), flags=3)),
                    wire(data_message(raw=bytes(STORE.serialize_cdr(samples()[0], BATTERY)) + b"\1")),
                    wire(data_message()) + b"\0"]
        for payload in failures:
            with self.subTest(payload=payload[-16:]):
                saved = capture(payload)
                with self.assertRaises(ValueError):
                    self.module.convert_pcap_rtps(saved, PORT, binding=binding(saved))
        for constant in ("MAX_PACKETS", "MAX_SUBMESSAGES", "MAX_SUBMESSAGES_PER_DATAGRAM",
                         "MAX_CDR_BYTES", "MAX_SOURCE_BYTES", "MAX_BINDING_BYTES", "MAX_REPORT_BYTES", "LIMIT"):
            with self.subTest(bound=constant), mock.patch.object(self.module, constant, 1):
                saved = capture(wire(data_message()), wire(data_message(seq=2))) if constant in (
                    "MAX_PACKETS", "MAX_SUBMESSAGES") else (
                    capture(wire(submessage(1), data_message())) if constant == "MAX_SUBMESSAGES_PER_DATAGRAM" else raw)
                with self.assertRaises(ValueError):
                    self.module.convert_pcap_rtps(saved, PORT, binding=binding(saved))

    def test_cli_and_actual_common_unknown_clock_with_original_cdr(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not configured")
        message = samples()[0]
        message.temperature = 20.
        raw_cdr = bytes(STORE.serialize_cdr(message, BATTERY, little_endian=False))
        raw = capture(wire(data_message(raw_cdr, order="<")))
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "source.pcap"; source.write_bytes(raw)
            config = folder / "binding.json"; config.write_text(json.dumps(binding(raw)))
            output = folder / "out"
            command = [sys.executable, str(ROOT / "scripts/convert_rtps_data.py"), str(source),
                       str(output), "--port", str(PORT), "--binding", str(config)]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / "source.pcap").read_bytes(), raw)
            result = subprocess.run([reader, str(ROOT / "profiles/declared/rtps-data/profile.toml"),
                                     str(output / "observations.csv"), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            common = json.loads(result.stdout)
            self.assertEqual(common["main_rows"], 1)
            observation = common["observations"][0]
            self.assertEqual(observation["clock_basis"], "Unknown")
            fields = observation["fields"]
            self.assertAlmostEqual(fields["battery_temperature_k"], 293.15)
            self.assertEqual(fields["battery_current_a"], -2.)
            retained = json.loads(bytes.fromhex(fields["source_record_hex"][4:]))
            self.assertEqual(retained["raw_cdr_hex"], "hex:" + raw_cdr.hex())
            self.assertEqual(retained["decoded"]["serial_number"], "reported-id")
            config.write_text(json.dumps(dict(binding(raw), type_hash="caller-asserted")))
            rejected = folder / "rejected"
            command[3] = str(rejected)
            result = subprocess.run(command, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(rejected.exists())


if __name__ == "__main__":
    unittest.main()
