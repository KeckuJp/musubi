"""Two authored saved response layouts; no UART/RF/device execution."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import convert_smartaudio_recorded as module
from convert_crsf_link import crc8


def frame(payload, response=0x11):
    body = bytes([response, len(payload)]) + bytes(payload)
    return (b"\xaa\x55" + body + bytes([crc8(body)])).hex()


class SmartAudioTest(unittest.TestCase):
    def test_two_layouts_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required")
        for payload, hz, dbm in (([3, 1, 1, 0x16, 0xa8, 20, 2, 0, 20, 27], 5800000000, 20),
                                  ([8, 9, 0x82, 0x16, 0x44, 0, 0, 0], 5700000000, 0)):
            text = "time_us,frame_hex,note\n10," + frame(payload) + ",authored\n"
            converted, report = module.convert(text, module.BASIS)
            self.assertEqual(report["decoded_records"], 1)
            row = next(csv.DictReader(io.StringIO(converted)))
            self.assertEqual(float(row["vtx_frequency_field_hz"]), hz)
            self.assertEqual(float(row["vtx_current_power_reported_dbm"]), dbm)
            self.assertEqual(json.loads(bytes.fromhex(row["source_record_hex"][4:]))["note"], "authored")
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "status.csv"
                target.write_text(converted)
                common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/vtx-saved-settings/smartaudio-profile.toml"), str(target)], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 1)
                observation = common["observations"][0]
                self.assertEqual(observation["fields"]["vtx_frequency_field_hz"], hz)
                self.assertEqual(observation["fields"]["vtx_current_power_reported_dbm"], dbm)
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIn("NOT_ACTUAL_RF", observation["fields"]["vtx_value_basis"])

    def test_refusal_and_unsupported_accounting(self):
        valid = frame([3, 1, 1, 0x16, 0xa8, 20, 2, 0, 20, 27])
        for broken in (valid[:-2] + "ff", valid[:-2], frame([0] * 7), frame([0] * 8 + [1])):
            with self.assertRaises(ValueError):
                module.convert("time_us,frame_hex\n0," + broken + "\n", module.BASIS)
        with self.assertRaises(ValueError):
            module.convert("time_us,frame_hex\n0," + valid + "\n", "wrong")
        text = "time_us,frame_hex\n0," + valid + "\n0," + frame([0] * 5, 9) + "\n"
        converted, report = module.convert(text, module.BASIS)
        rows = list(csv.DictReader(io.StringIO(converted)))
        self.assertEqual((report["source_records"], report["decoded_records"], report["unsupported_records"]), (2, 1, 1))
        self.assertEqual(rows[1]["vtx_disposition"], "UNSUPPORTED_RESPONSE")
        self.assertEqual(rows[1]["vtx_frequency_field_hz"], "")
        with self.assertRaises(ValueError):
            module.convert(text.replace("0,", "-1,", 1), module.BASIS)
