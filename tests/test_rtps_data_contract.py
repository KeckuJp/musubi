"""Independent authored RTPS packets; no device, discovery or type attestation."""
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
from unittest.mock import patch

from tests.test_rtp_records import datagram
from tests.test_rosbag_observations import samples, STORE, BATTERY

ROOT = Path(__file__).resolve().parents[1]
PREFIX = bytes.fromhex("0102030405060708090a0b0c")
WRITER = bytes.fromhex("00000103")
PORT = 7401


def sub(kind, body=b"", *, little=True, flags=0, rest=False):
    return bytes((kind, flags | int(little))) + struct.pack(
        "<H" if little else ">H", 0 if rest else len(body)) + body


def data_message(*, little=True, cdr_little=True, writer=WRITER, sequence=1,
                 flags=4, offset=16, cdr_suffix=b"", rest=False):
    message = samples()[0]
    message.temperature = 20.
    raw = bytes(STORE.serialize_cdr(message, BATTERY, little_endian=cdr_little)) + cdr_suffix
    raw += bytes((-len(raw)) % 4)
    order = "<" if little else ">"
    body = struct.pack(order + "HH", 0, offset) + bytes(4) + writer
    body += struct.pack(order + "iI", 0, sequence) + raw
    return sub(0x15, body, little=little, flags=flags, rest=rest)


def rtps(*messages):
    return b"RTPS" + bytes((2, 3, 1, 15)) + PREFIX + b"".join(messages)


def capture(*payloads):
    frames = [datagram(payload, port=PORT) for payload in payloads]
    return struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1) + b"".join(
        struct.pack("<IIII", 100, 123, len(frame), len(frame)) + frame for frame in frames)


def binding(blob):
    return dict(source_input_sha256="sha256:" + hashlib.sha256(blob).hexdigest(),
                writer_guid_hex="hex:" + (PREFIX + WRITER).hex(),
                schema="ros2-humble-battery-cdr1")


class RtpsContractTests(unittest.TestCase):
    def module(self):
        return importlib.import_module("scripts.convert_rtps_data")

    def test_cross_endian_repeats_and_other_writer_reach_common(self):
        module = self.module()
        blob = capture(rtps(data_message(little=False, cdr_little=True),
                            data_message(writer=bytes.fromhex("00000203"))),
                       rtps(data_message(little=True, cdr_little=False, rest=True)), b"not RTPS")
        output, report = module.convert_pcap_rtps(blob, PORT, binding=binding(blob))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(report["source_packets"], 3)
        self.assertEqual(report["selected_data"], 2)
        self.assertEqual(report["unselected_data"], 1)
        self.assertEqual(len(report["packets"]), 3)
        self.assertEqual([r["rtps_writer_sequence_low"] for r in rows], ["1", "1"])
        for row in rows:
            self.assertEqual(row["record_time_us"], "100000123")
            self.assertEqual(row["rtps_capture_sha256"], binding(blob)["source_input_sha256"])
            self.assertEqual(row["rtps_writer_guid_hex"], binding(blob)["writer_guid_hex"])
            self.assertEqual(row["rtps_timestamp_disposition"], "NOT_REPORTED")
            self.assertEqual(row["rtps_timestamp_seconds"], "")
            self.assertEqual(float(row["battery_voltage_v"]), 24.)
            self.assertEqual(float(row["battery_current_a"]), -2.)
            self.assertEqual(float(row["battery_remaining_fraction"]), .75)
            self.assertAlmostEqual(float(row["battery_temperature_k"]), 293.15)
            self.assertIn("TYPE_HASH_ABSENT_NOT_VERIFIED", row["rtps_schema_basis"])
            self.assertIn("CALLER_SELECTED", row["rtps_schema_basis"])
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader is required, not an optional skip")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.csv"
            path.write_text(output)
            run = subprocess.run([reader, str(ROOT / "profiles/declared/rtps-data/profile.toml"),
                                  str(path), "--allow-equal-time"], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            common = json.loads(run.stdout)
            self.assertEqual(common["main_rows"], 2)
            for item in common["observations"]:
                self.assertEqual(item["clock_basis"], "Unknown")
                self.assertEqual(item["fields"]["battery_voltage_v"], 24.)
                self.assertAlmostEqual(item["fields"]["battery_temperature_k"], 293.15)

    def test_timestamp_context_invalidation_and_unsigned_sentinels(self):
        module = self.module()
        info = sub(0x09, struct.pack(">II", 0xf0000000, 0x80000000), little=False)
        invalid = sub(0x09, flags=2)
        sentinel = sub(0x09, struct.pack("<II", 0xffffffff, 0xffffffff))
        blob = capture(rtps(info, data_message(), invalid, data_message(sequence=2)),
                       rtps(data_message(sequence=3)), rtps(sentinel, data_message(sequence=4)))
        output, _ = module.convert_pcap_rtps(blob, PORT, binding=binding(blob))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["rtps_timestamp_seconds"], str(0xf0000000))
        self.assertEqual(rows[0]["rtps_timestamp_fraction"], str(0x80000000))
        self.assertEqual(rows[1]["rtps_timestamp_seconds"], "")
        self.assertIn("INVALID", rows[1]["rtps_timestamp_disposition"])
        self.assertEqual(rows[2]["rtps_timestamp_disposition"], "NOT_REPORTED")
        self.assertEqual(rows[3]["rtps_timestamp_seconds"], str(0xffffffff))
        self.assertIn("INVALID", rows[3]["rtps_timestamp_disposition"])
        self.assertTrue(all(r["ros_source_stamp_us"] == "2123456" for r in rows))
        self.assertTrue(all(r["record_time_us"] == "100000123" for r in rows))

    def test_binding_wrong_wire_and_resource_refusals(self):
        module = self.module()
        blob = capture(rtps(data_message()))
        good = binding(blob)
        for change in ({"source_input_sha256": "sha256:" + "0" * 64},
                       {"writer_guid_hex": "hex:" + (PREFIX + bytes.fromhex("00000203")).hex()},
                       {"type_hash": "pretended verified"}, {"schema": "other-type"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.convert_pcap_rtps(blob, PORT, binding={**good, **change})
        for message in (data_message(flags=6), data_message(offset=20),
                        data_message(sequence=0), data_message(cdr_suffix=b"garbage!"),
                        sub(0x16), sub(0x0c, bytes(20)), sub(0x99), data_message()[:-1]):
            bad = capture(rtps(message))
            with self.subTest(message=message[:8].hex()), self.assertRaises(ValueError):
                module.convert_pcap_rtps(bad, PORT, binding=binding(bad))
        for constant in ("MAX_PACKETS", "MAX_SUBMESSAGES", "MAX_CDR_BYTES", "MAX_REPORT_BYTES"):
            with self.subTest(limit=constant), patch.object(module, constant, 0), self.assertRaises(ValueError):
                module.convert_pcap_rtps(blob, PORT, binding=good)

    def test_cli_preserves_original_and_refuses_without_output(self):
        self.module()
        blob = capture(rtps(data_message()))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, manifest, dest = folder / "source.pcap", folder / "binding.json", folder / "out"
            source.write_bytes(blob)
            manifest.write_text(json.dumps(binding(blob)))
            command = [sys.executable, str(ROOT / "scripts/convert_rtps_data.py"), str(source),
                       str(dest), "--port", str(PORT), "--binding", str(manifest)]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((dest / "source.pcap").read_bytes(), blob)
            self.assertEqual(json.loads((dest / "binding.json").read_text()), binding(blob))
            manifest.write_text(json.dumps({**binding(blob), "type_hash": "not allowed"}))
            command[3] = str(folder / "refused")
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse((folder / "refused").exists())


if __name__ == "__main__":
    unittest.main()
