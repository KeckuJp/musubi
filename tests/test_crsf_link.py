"""Authored CRSF records: conditional code/spec checks, not captured radio proof."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from scripts.convert_crsf_link import convert, crc8

ROOT = Path(__file__).resolve().parents[1]


def frame(payload=bytes([70, 80, 99, 251, 1, 250, 250, 90, 50, 3]), kind=0x14):
    body = bytes([kind]) + payload
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8(body)])


def capture(frames):
    return "time_us,frame_hex,unknown_hint\n" + "".join(
        f"{time},{data.hex()},keep-me\n" for time, data in frames)


class CrsfLinkTests(unittest.TestCase):






    def test_meanings_unknown_fields_and_same_time_accounting(self):
        self.assertEqual(crc8(b"123456789"), 0xBC)
        zero = frame(bytes([0, 255, 0, 128, 8, 251, 252, 0, 0, 127]))
        output, report = convert(capture([(1, frame()), (1, zero), (2, frame(b"raw", 0x16))]))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), report["source_records"], report["decoded_records"]), (2, 3, 2))
        row = rows[0]
        for field, expected in {"uplink_rssi_1_dbm": -70, "uplink_rssi_2_dbm": -80,
                "uplink_lq_fraction": .99, "uplink_snr_db": -5,
                "downlink_rssi_dbm": -90, "downlink_lq_fraction": .5, "downlink_snr_db": 3}.items():
            self.assertEqual(float(row[field]), expected)
        self.assertEqual(row["rf_mode_code"], "250")
        self.assertNotIn("rf_rate_hz", row)
        self.assertEqual(row["source_frame_hex"], "hex:" + frame().hex())
        raw = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
        self.assertEqual(raw["unknown_hint"], "keep-me")
        self.assertEqual(rows[1]["uplink_lq_fraction"], "0.0")
        self.assertEqual(rows[1]["active_antenna"], "UNKNOWN_8")
        self.assertEqual(report["unsupported_records"][0]["frame_hex"], frame(b"raw", 0x16).hex())

    def test_critical_failures_are_not_silent_skips(self):
        wrong_crc = bytearray(frame()); wrong_crc[-1] ^= 1
        invalid_percent = frame(bytes([70, 80, 101, 0, 0, 0, 0, 90, 100, 0]))
        for value in ["", "frame_hex,time_us\n00,1\n", "time_us,time_us,frame_hex\n1,1,00\n",
                capture([(1, frame()[:-1])]), capture([(1, bytes(wrong_crc))]),
                capture([(1, invalid_percent)]), capture([(1, frame(b"\0" * 11))]),
                capture([(2, frame()), (1, frame())]), capture([(-1, frame())]),
                capture([(2**63, frame())]), capture([(1, frame(b"raw", 0x16))]),
                capture([(1, frame())]).replace("c80c", "c8 0c")]:
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                convert(value)



if __name__ == "__main__":
    unittest.main()
