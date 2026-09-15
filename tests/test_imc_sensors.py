"""Authored IMC packets; finite spec checks, not real marine recordings."""
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from scripts.convert_imc_sensors import LIMIT, SENSORS, convert, convert_input, convert_alog, convert_slog, crc16

ROOT = Path(__file__).resolve().parents[1]


def frame(message, value, order="<", timestamp=1700000000.25):
    payload = struct.pack(order + SENSORS[message][0], value) if message in SENSORS else bytes(value)
    raw = struct.pack(order + "HHHdHBHB", 0xFE54, message, len(payload), timestamp, 42, 3, 65535, 255) + payload
    return raw + struct.pack(order + "H", crc16(raw))


def stream(order="<"):
    values = {263: 20, 264: 1013.25, 265: 7.5, 267: 1500, 268: 1025, 270: 35}
    return b"".join(frame(key, value, order) for key, value in values.items()) + frame(65000, b"unmapped", order)


class ImcSensorTests(unittest.TestCase):







    def test_gzip_crc_size_truncation_trailing_and_expansion_fail_closed(self):
        packed = gzip.compress(stream(), mtime=0)
        crc_bad = bytearray(packed); crc_bad[-8] ^= 1
        size_bad = bytearray(packed); size_bad[-4] ^= 1
        for data in (packed[:-1], packed[:12], packed + b"garbage", bytes(crc_bad), bytes(size_bad),
                     gzip.compress(b"\0" * (LIMIT + 1), mtime=0),
                     gzip.compress(b"\0" * (LIMIT // 2 + 1), mtime=0) * 2,
                     gzip.compress(b"", mtime=0) * 1025):
            with self.subTest(prefix=data[:10]), self.assertRaises(ValueError):
                convert_input(data)


    def test_both_endians_six_meanings_and_full_frame_accounting(self):
        self.assertEqual(crc16(b"123456789"), 0xBB3D)
        for order in ("<", ">"):
            with self.subTest(order=order):
                data = stream(order)
                output, report = convert(data)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((len(rows), report["source_frames"], report["decoded_frames"]), (6, 7, 6))
                for row, (field, value) in zip(rows, [("temperature_k", 293.15), ("external_pressure_pa", 101325),
                    ("depth_m", 7.5), ("sound_speed_m_s", 1500), ("water_density_kg_m3", 1025), ("salinity_psu", 35)]):
                    self.assertAlmostEqual(float(row[field]), value)
                    self.assertEqual(row["record_time_us"], "1700000000250000")
                    self.assertEqual(row["source_id"], "42")
                restored = b"".join(bytes.fromhex(row["source_frame_hex"][4:]) for row in rows)
                restored += bytes.fromhex(report["unsupported_frames"][0]["frame_hex"])
                self.assertEqual(restored, data)


    def test_unavailable_sound_and_important_failures(self):
        row = next(csv.DictReader(io.StringIO(convert(frame(267, -1))[0])))
        self.assertEqual(row["sound_speed_m_s"], "")
        self.assertEqual(row["measurement_status"], "UNAVAILABLE_SOURCE_SENTINEL")
        raw = frame(263, 20)
        bad_crc = bytearray(raw); bad_crc[21] ^= 1
        # A valid CRC cannot make a selected message with the wrong size valid.
        header = struct.pack("<HHHdHBHB", 0xFE54, 263, 8, 1., 1, 1, 1, 1) + b"\0" * 8
        wrong_size = header + struct.pack("<H", crc16(header))
        for data in [b"", raw[:-1], bytes(bad_crc), b"\x55\xfe" + raw[2:], wrong_size,
                     frame(263, float("nan")), frame(263, -274), frame(263, 20, timestamp=-1),
                     raw + frame(263, 20, timestamp=1), frame(65000, b"only unsupported")]:
            with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                convert(data)



if __name__ == "__main__":
    unittest.main()
