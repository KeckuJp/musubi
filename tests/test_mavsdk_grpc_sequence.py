"""Authored saved response bodies; no SDK, RPC or vehicle execution."""
import csv
import gzip
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

from scripts.convert_ardupilot_battery_csv import convert as standalone
from tests.test_mavsdk_battery import euler_wire, varint

ROOT = Path(__file__).resolve().parents[1]
PROTO = "d3cb35f49d1379d2d95a6d22cdbe40ff4074e9bf"
METHOD = "/mavsdk.rpc.telemetry.TelemetryService/Subscribe"
PROFILE = ROOT / "profiles/declared/mavsdk-grpc-sequence/profile.toml"


def frame(inner, outer_unknown=b""):
    response = b"\x0a" + varint(len(inner)) + inner + outer_unknown
    return b"\x00" + len(response).to_bytes(4, "big") + response


def battery(voltage=24.0, current=-1.0):
    return b"\x08\x02" + b"".join(bytes([number * 8 + 5]) + struct.pack("<f", value)
        for number, value in ((2, 20), (3, voltage), (4, current), (5, 3), (6, 75), (7, 50))) + b"\x40\x02"


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class GrpcSequence(unittest.TestCase):
    def setUp(self):
        from scripts import convert_mavsdk_grpc_sequence
        self.module = convert_mavsdk_grpc_sequence

    def convert(self, data, method="Battery", **changes):
        args = dict(rpc_method=METHOD + method, sdk_version="3.17.4", proto_commit=PROTO,
                    capture_time_us=7000)
        args.update(changes)
        return self.module.convert(data, **args)

    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required; CI supplies it")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            result = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)

    def assert_accounting(self, text, report, frames, method, source_format):
        rows = list(csv.DictReader(io.StringIO(text)))
        data = b"".join(frames)
        self.assertEqual((report["message_count"], report["output_rows"]), (len(frames), len(frames)))
        self.assertEqual((report["input_bytes"], report["accounted_bytes"]), (len(data), len(data)))
        self.assertEqual(report["source_stream_hex"], "hex:" + data.hex())
        self.assertEqual(report["source_stream_sha256"], sha(data))
        self.assertEqual(report["output_csv_sha256"], sha(text.encode()))
        offset = 0
        for index, (wire, row, entry) in enumerate(zip(frames, rows, report["frames"])):
            self.assertEqual(entry, dict(index=index, offset=offset, frame_bytes=len(wire), sha256=sha(wire)))
            self.assertEqual(row["source_message_index"], str(index))
            self.assertEqual(row["source_byte_offset"], str(offset))
            self.assertEqual(row["source_frame_bytes"], str(len(wire)))
            self.assertEqual(row["source_stream_sha256"], sha(data))
            self.assertEqual(row["source_frame_sha256"], sha(wire))
            self.assertEqual(bytes.fromhex(row["rpc_method_hex"][4:]).decode(), METHOD + method)
            envelope = bytes.fromhex(row["source_record_hex"][4:]).decode()
            self.assertEqual(json.loads(envelope)["grpc_frame_hex"], wire.hex())
            old = next(csv.DictReader(io.StringIO(standalone(envelope, source_format))))
            self.assertEqual({key: row[key] for key in old}, old)
            offset += len(wire)
        return rows

    def test_battery_sequence_existing_cells_and_common(self):
        frames = [frame(battery(), b"\x98\x06\x07"), frame(battery(12, float("nan")))]
        text, report = self.convert(b"".join(frames))
        rows = self.assert_accounting(text, report, frames, "Battery", "mavsdk-battery-grpc")
        self.assertEqual(rows[0]["protobuf_unknown_fields"], "1")
        self.assertEqual(rows[1]["battery_current_a"], "")
        common = self.common(text)
        self.assertEqual(common["main_rows"], 2)
        fields = [item["fields"] for item in common["observations"]]
        self.assertEqual([f["battery_voltage_v"] for f in fields], [24, 12])
        self.assertEqual(fields[0]["battery_power_w"], -24)
        self.assertEqual(fields[0]["battery_remaining_fraction"], .75)
        self.assertEqual(fields[0]["battery_temperature_k"], 293.15)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        self.assertIn("DERIVED", common["profile_units"]["source_record_hex"])

    def test_attitude_equal_reset_and_absent_are_preserved(self):
        frames = [frame(euler_wire(90, 1, 2, 42000)), frame(euler_wire(None, -2, 3, 42000)),
                  frame(euler_wire(-90, 0, float("nan"), 1000))]
        text, report = self.convert(b"".join(frames), "AttitudeEuler")
        rows = self.assert_accounting(text, report, frames, "AttitudeEuler", "mavsdk-attitude-grpc")
        self.assertEqual([int(r["attitude_device_time_us"]) for r in rows], [42000, 42000, 1000])
        self.assertIn("rollDeg", bytes.fromhex(rows[1]["protobuf_zero_or_absent_fields_hex"][4:]).decode())
        self.assertEqual(rows[2]["attitude_yaw_rad"], "")
        common = self.common(text)
        self.assertEqual(common["main_rows"], 3)
        for observation, expected in zip(common["observations"], [math.pi / 2, 0, -math.pi / 2]):
            self.assertAlmostEqual(observation["fields"]["attitude_roll_rad"], expected)
            self.assertEqual(observation["fields"]["record_time_us"], 7000)
            self.assertEqual(observation["clock_basis"], "Unknown")
        self.assertIn("boot_relative", common["profile_units"]["attitude_device_time_us"])

    def test_framing_selection_and_capacity_refusals(self):
        good = frame(battery())
        for data in (b"", b"\x00", good[:-1], good + b"\x00", b"\x01" + good[1:],
                     b"\x02" + good[1:], b"\x00\xff\xff\xff\xff", b"\x00\0\0\0\0",
                     b"\x00\0\0\0\1\xff"):
            with self.subTest(data=data[:5]), self.assertRaises(ValueError):
                self.convert(data)
        for changes in (dict(rpc_method="SubscribeBattery"), dict(sdk_version="3.17.5"),
                        dict(proto_commit="wrong"), dict(capture_time_us=True),
                        dict(capture_time_us=-1), dict(capture_time_us=2**63)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.convert(good, **changes)
        for name, value in (("INPUT_LIMIT", len(good) - 1), ("FRAME_LIMIT", len(good) - 1),
                            ("MESSAGE_LIMIT", 1), ("CSV_LIMIT", 1), ("REPORT_LIMIT", 1)):
            with self.subTest(limit=name), patch.object(self.module, name, value), self.assertRaises(ValueError):
                self.convert(good * 2)

    def test_cli_saved_pair_and_invalid_no_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "saved.bin"
            output = Path(directory) / "converted"
            source.write_bytes(frame(battery()) * 2)
            command = [sys.executable, str(ROOT / "scripts/convert_mavsdk_grpc_sequence.py"),
                       str(source), str(output), "--rpc-method", METHOD + "Battery",
                       "--sdk-version", "3.17.4", "--proto-commit", PROTO, "--capture-time-us", "7000"]
            subprocess.run(command, check=True, capture_output=True)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["output_rows"], 2)
            self.assertEqual(report["output_csv_sha256"], sha((output / "observations.csv").read_bytes()))
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            command[3] = str(Path(directory) / "invalid")
            source.write_bytes(frame(battery()) + b"\0")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(Path(command[3]).exists())

    def test_gzip_battery_mixed_body_and_euler_reuse(self):
        for method, plain in (("Battery", [frame(battery(), b"\x98\x06\x07"), frame(battery(12))]),
                              ("AttitudeEuler", [frame(euler_wire(90, 1, 2, 42000)),
                                                 frame(euler_wire(-90, 0, 3, 1000))])):
            packed = gzip.compress(plain[0][5:], mtime=0)
            compressed = b"\x01" + len(packed).to_bytes(4, "big") + packed
            original = compressed + plain[1]
            expected, _ = self.convert(b"".join(plain), method)
            text, report = self.convert(original, method, message_encoding="gzip")
            expected_rows = list(csv.DictReader(io.StringIO(expected)))
            rows = list(csv.DictReader(io.StringIO(text)))
            for index, row in enumerate(rows):
                self.assertEqual({k: v for k, v in row.items() if k not in self.module.EXTRA_FIELDS},
                                 {k: v for k, v in expected_rows[index].items() if k not in self.module.EXTRA_FIELDS})
                self.assertEqual(report["frames"][index]["decoded_frame_sha256"], sha(plain[index]))
            self.assertEqual(rows[0]["source_frame_sha256"], sha(compressed))
            self.assertEqual(rows[1]["source_byte_offset"], str(len(compressed)))
            self.assertEqual(report["source_stream_hex"], "hex:" + original.hex())
            self.assertEqual(report["accounted_bytes"], len(original))
            self.assertEqual([f["compressed_flag"] for f in report["frames"]], [1, 0])
            self.assertIn("CALLER_DECLARED", report["message_encoding_basis"])
            common = self.common(text)
            self.assertEqual(common["main_rows"], 2)
            fields = [o["fields"] for o in common["observations"]]
            if method == "Battery":
                self.assertEqual([f["battery_voltage_v"] for f in fields], [24, 12])
                self.assertEqual(fields[0]["battery_power_w"], -24)
                self.assertEqual(fields[0]["protobuf_unknown_fields"], 1)
            else:
                self.assertAlmostEqual(fields[0]["attitude_roll_rad"], math.pi / 2)
                self.assertAlmostEqual(fields[1]["attitude_roll_rad"], -math.pi / 2)
                self.assertEqual([f["attitude_device_time_us"] for f in fields], [42000, 1000])

    def test_gzip_refuses_corrupt_expansion_and_ambiguous_members(self):
        good = gzip.compress(frame(battery())[5:], mtime=0)
        for payload in (good[:-1], good + b"trailing", good + good,
                        good[:-8] + bytes([good[-8] ^ 1]) + good[-7:],
                        gzip.compress(b"x" * self.module.FRAME_LIMIT, mtime=0)):
            wire = b"\x01" + len(payload).to_bytes(4, "big") + payload
            with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                self.convert(wire, message_encoding="gzip")
        wire = b"\x01" + len(good).to_bytes(4, "big") + good
        for encoding in (None, "deflate", "identity", True):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                self.convert(wire, message_encoding=encoding)
        with self.assertRaises(ValueError):
            self.convert(b"\x02" + wire[1:], message_encoding="gzip")

    def test_gzip_cli_declared_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "saved.bin", Path(directory) / "output"
            payload = gzip.compress(frame(battery())[5:], mtime=0)
            source.write_bytes(b"\x01" + len(payload).to_bytes(4, "big") + payload)
            command = [sys.executable, str(ROOT / "scripts/convert_mavsdk_grpc_sequence.py"),
                       str(source), str(output), "--rpc-method", METHOD + "Battery",
                       "--sdk-version", "3.17.4", "--proto-commit", PROTO, "--capture-time-us", "7000"]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(output.exists())
            subprocess.run(command + ["--message-encoding", "gzip"], check=True, capture_output=True)
            self.assertEqual(json.loads((output / "report.json").read_text())["message_encoding"], "gzip")


if __name__ == "__main__":
    unittest.main()
