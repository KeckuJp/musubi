"""Authored saved delta reuse only; no engine capture or control."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_signalk_recorded import convert, convert_path, PROPULSION_QUANTITIES
from tests.test_signalk_recorded import delta, ROOT


class PropulsionReports(unittest.TestCase):
    def test_two_instances_and_saved_window_reuse_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "dedicated postbuild test requires common reader")
        for identity, scale in (("port1", 1), ("starboard2", 2)):
            values = [25, 300, 100000, 301, 120000, 900, .25, .2, .00001]
            values = [value * scale for value in values]
            selected = [("propulsion." + identity + "." + key, value)
                        for key, value in zip(PROPULSION_QUANTITIES, values)]
            selected += [("propulsion." + identity + ".temperature", None),
                         ("propulsion." + identity + ".unknownField", 17),
                         ("navigation.speedThroughWater", 2)]
            raw = delta("vessels.example", selected)
            old, old_report = convert(raw, "vessels.example")
            output, report = convert(raw, "vessels.example", propulsion=True)
            self.assertEqual(old_report["output_records"], 1)
            self.assertEqual(report["output_records"], 11)
            self.assertEqual(report["unsupported_values"], 1)
            self.assertIn("SPEC_1_7_0_b8579f70", report["propulsion_basis"])
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(rows[9]["signalk_value_status"], "NOT_PROVIDED")
            self.assertEqual(rows[0]["propulsion_id_hex"], "hex:" + identity.encode().hex())
            for field, value in next(csv.DictReader(io.StringIO(old))).items():
                self.assertEqual(rows[-1][field], value)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "capture.txt"
                source.write_text("1700000000000;provider;" + raw + "\n")
                window, win_report = convert_path(source, "vessels.example",
                    start_record=0, stop_record=1, propulsion=True)
                self.assertEqual(win_report["record_selection"]["source_records"], 1)
                dest = Path(directory) / "rows.csv"
                dest.write_text(window)
                common = json.loads(subprocess.run([reader, str(ROOT /
                    "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(dest), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 11)
                for obs, field, value in zip(common["observations"], PROPULSION_QUANTITIES.values(), values):
                    self.assertEqual(obs["fields"][field], value)
                    self.assertEqual(obs["clock_basis"], "Unknown")
                subprocess.run([sys.executable, "scripts/convert_signalk_recorded.py", str(source),
                    str(Path(directory) / "out"), "--context", "vessels.example", "--server-log",
                    "--start-record", "0", "--stop-record", "1", "--propulsion"],
                    cwd=ROOT, check=True, capture_output=True)

    def test_wrong_value_identity_missing_and_ratio(self):
        for key, value in (("temperature", -1), ("engineLoad", 1.01), ("engineTorque", -.1),
                           ("oilPressure", True), ("revolutions", "25")):
            with self.assertRaises(ValueError):
                convert(delta("vessels.x", [("propulsion.port." + key, value)]),
                        "vessels.x", propulsion=True)
        with self.assertRaises(ValueError):
            convert(delta("vessels.x", [("propulsion.bad-id.revolutions", 1)]),
                    "vessels.x", propulsion=True)
        with self.assertRaises(ValueError):
            convert("", "vessels.x", propulsion=1)
