"""Authored snapshots, not device reads or physical GPIO evidence."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PIN = "e711c03e628e29bc069d0b17434834ba5e33a7a9"
PROFILE = ROOT / "profiles/declared/farmbot-saved-pins/profile.toml"


def record(pins, time=7):
    return dict(schema_commit=PIN, capture_time_us=time,
                payload=dict(pins=pins, future={"unknown": "保持"}))


class SavedPinsTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module("scripts.convert_farmbot_saved_pins")

    def common(self, output):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.csv"
            path.write_text(output)
            run = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                 capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_values_missing_unknown_associations_and_common(self):
        first = record({"9": {"mode": 0, "value": 1.0, "future": 42},
                        "2": {"mode": -1, "value": 1},
                        "3": {"mode": 1, "value": 99},
                        "4": {"mode": 0, "value": None},
                        "5": {"mode": 0, "value": 2}, "6": {"value": 0}})
        lines = [json.dumps(first, ensure_ascii=False), json.dumps(record({"9": {"mode": 0, "value": 0}})),
                 json.dumps(record({})), json.dumps(record(None))]
        missing = record({}); del missing["payload"]["pins"]; lines.append(json.dumps(missing))
        text = "\n".join(lines)
        output, report = self.m.convert(text)
        rows = list(csv.DictReader(io.StringIO(output)))
        pins = [r for r in rows if r["farm_pin_record_kind"] == "PIN"]
        self.assertEqual([r["farm_pin_number"] for r in pins], ["9", "2", "3", "4", "5", "6", "9"])
        self.assertEqual([r["digital_level_reported"] for r in pins], ["HIGH", "", "", "", "", "", "LOW"])
        self.assertEqual([r["digital_value_reported"] for r in pins], ["1", "", "", "", "", "", "0"])
        self.assertEqual([r["farm_pin_status"] for r in pins],
                         ["REPORTED_DIGITAL", "MODE_UNKNOWN", "ANALOG_NOT_CONVERTED", "VALUE_NOT_REPORTED",
                          "NON_BINARY_NOT_CONVERTED", "MODE_NOT_REPORTED", "REPORTED_DIGITAL"])
        self.assertEqual([rows[i]["farm_pin_status"] for i in (-3, -2, -1)], ["EMPTY", "NULL", "NOT_REPORTED"])
        self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), lines[0])
        digest = "sha256:" + hashlib.sha256(lines[0].encode()).hexdigest()
        self.assertTrue(all(r["farm_snapshot_sha256"] == digest for r in rows[:7]))
        self.assertTrue(all(not r["source_record_hex"] for r in pins))
        self.assertEqual(sum(bool(r["source_record_hex"]) for r in rows), 5)
        self.assertEqual((report["source_records"], report["pin_rows"], report["digital_rows"], report["output_rows"]), (5, 7, 2, 12))
        self.assertEqual(report["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
        common = self.common(output)
        self.assertEqual(len(common["observations"]), 12)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        fields = [o["fields"] for o in common["observations"]]
        self.assertEqual(fields[1]["digital_value_reported"], 1)
        self.assertIsNone(fields[2]["digital_value_reported"])
        self.assertEqual(fields[8]["digital_value_reported"], 0)
        self.assertEqual(fields[-1]["farm_source_record_index"], 4)
        self.assertIn("NOT_MEASURED", common["profile_units"]["digital_value_reported"])

    def test_bad_shape_types_clock_and_duplicate_rejected(self):
        cases = [record({key: {"mode": 0, "value": 1}}) for key in ("01", "-1", "256", "1.0")]
        cases += [record(value) for value in ([], "pins", 0)]
        cases += [record({"1": value}) for value in (None, [], 1)]
        cases += [record({"1": {"mode": value, "value": 1}}) for value in (True, 0.0, "0", 2**31)]
        cases += [record({"1": {"mode": 0, "value": value}}) for value in (True, "1", 2**53, float("inf"))]
        cases += [record({}, value) for value in (True, -1, 2**63)]
        cases += [dict(record({}), schema_commit="wrong"), dict(record({}), payload=None)]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError): self.m.convert(json.dumps(value))
        for text in ("", '{"x":1,"x":2}', json.dumps(record({}))+"\n\n",
                     json.dumps(record({}))+"\n"+json.dumps(record({}, 6)),
                     json.dumps(record({"1": {"mode": 0, "value": 1}})).replace('"mode": 0', '"mode": 0, "mode": 0')):
            with self.subTest(text=text), self.assertRaises(ValueError): self.m.convert(text)

    def test_max_pin_count_source_once_bounds_and_cli(self):
        text = json.dumps(record({str(i): dict(mode=0, value=i % 2) for i in range(256)}))
        output, report = self.m.convert(text)
        common = self.common(output)["observations"]
        self.assertEqual((len(common), report["digital_rows"]), (257, 256))
        self.assertEqual(common[-1]["fields"]["farm_pin_number"], 255)
        for key in ("MAX_INPUT_BYTES", "MAX_RECORD_BYTES", "MAX_PINS", "MAX_OUTPUT_BYTES"):
            with self.subTest(bound=key), patch.object(self.m, key, 1), self.assertRaises(ValueError): self.m.convert(text)
        with tempfile.TemporaryDirectory() as directory:
            source, target, report_path = [Path(directory) / name for name in ("input.jsonl", "output.csv", "report.json")]
            source.write_text(text)
            args = [str(ROOT / "scripts/convert_farmbot_saved_pins.py"), str(source), str(target), "--report", str(report_path)]
            run = subprocess.run([os.sys.executable] + args, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(target.read_text(), output)
            before = (target.read_bytes(), report_path.read_bytes())
            refused = subprocess.run([os.sys.executable] + args, text=True, capture_output=True)
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(before, (target.read_bytes(), report_path.read_bytes()))

    def test_opt_in_write_provenance_and_written_analog_value(self):
        # FarmBotOS e711c03e: mode 0/1 reach BotState only from a successful PinControl write;
        # firmware pin reports arrive with mode nil (-1). The source gives analog no range/unit.
        self.assertTrue(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
        pins = {"9": {"mode": 0, "value": 1.0}, "5": {"mode": 1, "value": 128.0},
                "6": {"mode": 1, "value": None}, "2": {"mode": -1, "value": 512},
                "3": {"value": 1}, "4": {"mode": 7, "value": 1}, "7": {"mode": 0, "value": None}}
        text = json.dumps(record(pins)) + "\n" + json.dumps(record({"8": {"mode": 1, "value": 0}}, 7)) + "\n"
        default, _ = self.m.convert(text)
        self.assertEqual(default, self.m.convert(text, False)[0])
        self.assertNotIn("farm_pin_value_provenance", default.splitlines()[0])
        output, report = self.m.convert(text, True)
        self.assertIn("exact-pin search", report["write_provenance_basis"])
        self.assertIn("not physical success", report["write_provenance_basis"])
        rows = [r for r in csv.DictReader(io.StringIO(output)) if r["farm_pin_record_kind"] == "PIN"]
        got = {r["farm_pin_number"]: (r["farm_pin_value_provenance"], r["analog_written_value_reported"])
               for r in rows}
        self.assertEqual(got, {"9": ("WRITE_RESPONSE_RECORDED_NOT_A_READ", ""),
                               "5": ("WRITE_RESPONSE_RECORDED_NOT_A_READ", "128.0"),
                               "6": ("VALUE_NOT_REPORTED", ""),
                               "2": ("FIRMWARE_REPORT_MODE_NOT_GIVEN", ""),
                               "3": ("MODE_NOT_REPORTED", ""), "4": ("MODE_UNKNOWN", ""),
                               "7": ("VALUE_NOT_REPORTED", ""),
                               "8": ("WRITE_RESPONSE_RECORDED_NOT_A_READ", "0")})
        # Default dispositions are unchanged: analog stays unconverted, -1 stays unknown.
        self.assertEqual({r["farm_pin_number"]: r["farm_pin_status"] for r in rows}["5"], "ANALOG_NOT_CONVERTED")
        common = self.common(output)
        fields = [o["fields"] for o in common["observations"] if o["fields"].get("farm_pin_number") == 5]
        self.assertEqual(fields[0]["analog_written_value_reported"], 128.0)
        self.assertIn("no_range_unit_voltage", common["profile_units"]["analog_written_value_reported"])
        with self.assertRaises(ValueError):
            self.m.convert(text, "yes")


if __name__ == "__main__":
    unittest.main()
