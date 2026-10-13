"""Authored saved bodies connect adopted fixed response meaning; no RPC execution."""
import csv
import gzip
import io
import struct
import subprocess
import sys
import unittest

from scripts import convert_mavsdk_grpc_sequence as target
from tests import test_mavsdk_grpc_sequence as existing
from tests.test_mavsdk_grpc_sequence import METHOD, PROTO, frame


class BasicMethods(unittest.TestCase):
    def test_standalone_cli_import(self):
        result = subprocess.run([sys.executable, target.__file__, "--help"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"--source-id", result.stdout)

    def convert(self, wire, method, **changes):
        options = dict(rpc_method=METHOD + method, sdk_version="3.17.4", proto_commit=PROTO,
                       capture_time_us=7000, source_id="caller-declared-source")
        options.update(changes)
        return target.convert(wire, **options)

    def test_seven_methods_two_bodies_reach_common(self):
        common = existing.GrpcSequence(methodName="test_battery_sequence_existing_cells_and_common")
        for variant in (0, 1):
            cases = {
                "Position": (frame(b"\x09" + struct.pack("<d", 35.1 + variant)), "latitude_deg", 35.1 + variant),
                "VelocityNed": (frame(b"\x0d" + struct.pack("<f", 2 + variant)), "reported_velocity_north_m_s", 2 + variant),
                "Health": (frame(b"\x08\x01\x20\x01"), "unknown_wire_fields", 1),
                "FlightMode": (b"\0\0\0\0\x02\x08" + bytes([99 + variant]), "reported_flight_mode", "UNKNOWN_ENUM_" + str(99 + variant)),
                "LandedState": (b"\0\0\0\0\x02\x08\x02", "reported_landed_state", "IN_AIR"),
                "Armed": (b"\0\0\0\0\x02\x08\x01", "reported_armed", "TRUE_REPORTED"),
                "InAir": (b"\0\0\0\0\0", "reported_in_air", "FALSE_OR_ABSENT_REPORTED"),
            }
            for method, (wire, field, expected) in cases.items():
                with self.subTest(method=method, variant=variant):
                    body = wire + wire
                    text, report = self.convert(body, method, source_id="separate-source-" + str(variant))
                    rows = list(csv.DictReader(io.StringIO(text)))
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(float(rows[0][field]) if isinstance(expected, (int, float)) else rows[0][field], expected)
                    self.assertEqual(rows[1]["source_byte_offset"], str(len(wire)))
                    self.assertEqual(report["accounted_bytes"], len(body))
                    decoded = common.common(text)
                    self.assertEqual(len(decoded["observations"]), 2)
                    values = decoded["observations"][0]["fields"]
                    self.assertEqual(values[field], expected)
                    self.assertEqual(values["source_id_hex"], "hex:" + ("separate-source-" + str(variant)).encode().hex())

    def test_gzip_and_important_refusals(self):
        wire = frame(b"\x09" + struct.pack("<d", 35.5))
        compressed = gzip.compress(wire[5:], mtime=0)
        body = b"\1" + len(compressed).to_bytes(4, "big") + compressed
        text, report = self.convert(body, "Position", message_encoding="gzip")
        self.assertEqual(next(csv.DictReader(io.StringIO(text)))["latitude_deg"], "35.5")
        self.assertEqual(report["frames"][0]["decoded_frame_bytes"], len(wire))
        for changes in ({"source_id": None}, {"source_id": ""}, {"source_id": "bad\n"},
                        {"proto_commit": "wrong"}, {"rpc_method": METHOD + "Unsupported"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.convert(wire, "Position", **changes)
        for invalid in (wire[:-1], b"\0\0\0\0\0", frame(b"\x09" + struct.pack("<d", float("inf")))):
            with self.assertRaises(ValueError):
                self.convert(invalid, "Position")


if __name__ == "__main__":
    unittest.main()
