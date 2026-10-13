"""Authored saved RCOU; no firmware, device, bus or physical servo observation."""
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
SCRIPT = ROOT / "scripts/convert_ardupilot_rcout.py"
PROFILE = ROOT / "profiles/declared/ardupilot-rcout/profile.toml"
SOURCE = "ardupilot-a64bad1a-rcou"
BASIS = "pymavlink_decoded_no_second_wire_scaling"
INPUT_PROFILE = '''profile_id = "authored_rcou_input"
version = "1"
family = "unknown"
source_role = "fc"
format = "pymavlink_dataflash_jsonl"
extensions = ["jsonl"]
default_clock_basis = "boot_relative"
[fields]
time = "TimeUS"
'''


def row(time_us=1000, base=1000):
    return {"meta": {"type": "RCOU"}, "data": {
        "TimeUS": time_us, **{f"C{i}": base + i for i in range(1, 15)}}}


def report(*records):
    return json.dumps({"value_basis": BASIS, "source_records": records}).encode()


class RcoutContract(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.convert_ardupilot_rcout")

    def test_two_actual_reader_inputs_reach_all_fourteen_common_quantities(self):
        binaries = [os.environ.get(key) for key in
                    ("MUSUBI_MAVLOG_READER", "MUSUBI_TELEMETRY_READER")]
        if not all(binaries):
            self.skipTest("both actual reader examples are required")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            profile = folder / "input.toml"
            profile.write_text(INPUT_PROFILE)
            for base in (1000, 2000):
                first = row(base=base)
                first["data"].update(C1=0, C14=65535, Future={"label": "未知", "x": [None, False]})
                other = {"meta": {"type": "RCO2"}, "data": {"C15": 12}}
                records = [first, other, row(base=base + 10)]
                source = folder / "source.jsonl"
                source.write_text("".join(json.dumps(r) + "\n" for r in records))
                native = subprocess.run([binaries[0], str(profile), str(source)],
                                        check=True, capture_output=True).stdout
                text, counts = self.module.convert(native, source_version=SOURCE)
                self.assertEqual((counts["source_records"], counts["selected_records"],
                                  counts["not_selected_records"]), (3, 2, 1))
                self.assertEqual(counts["unselected_record_types"], {"RCO2": 1})
                csv_path = folder / "observations.csv"
                csv_path.write_text(text)
                result = subprocess.run([binaries[1], str(PROFILE), str(csv_path),
                                         "--allow-equal-time"], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                common = json.loads(result.stdout)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["source_role"], "fc")
                self.assertEqual(common["equal_time_order"], "source-order-only")
                observations = common["observations"]
                self.assertEqual(len(observations), 2)
                for index, observation in enumerate(observations):
                    fields = observation["fields"]
                    self.assertEqual(observation["clock_basis"], "BootRelative")
                    self.assertEqual(observation["t_boot_us"], 1000)
                    self.assertEqual(fields["source_report_record_index"], index * 2)
                    self.assertEqual(fields["source_document_sha256"],
                                     "sha256:" + hashlib.sha256(native).hexdigest())
                    for channel in range(1, 15):
                        name = f"rcout_c{channel}_reported_s"
                        expected = records[index * 2]["data"][f"C{channel}"] / 1e6
                        self.assertAlmostEqual(fields[name], expected, places=12)
                        self.assertIn("s_reported", common["profile_units"][name])
                    retained = json.loads(bytes.fromhex(fields["source_record_hex"][4:]))
                    self.assertEqual(retained, records[index * 2])
                    self.assertIn("NOT_PHYSICAL_SERVO_POSITION", fields["rcout_value_basis"])
                    self.assertIn("NOT_ALL_CHANNELS_VALID", fields["rcout_channel_basis"])
                self.assertEqual(observations[0]["fields"]["rcout_c1_reported_s"], 0)

    def test_bad_declaration_shape_values_and_reset_are_not_zero_filled(self):
        module = self.module
        for selector in ("plane-4.5.5", "", None):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                module.convert(report(row()), source_version=selector)
        for value in (None, True, 1.5, -1, 65536, "1500"):
            record = row()
            record["data"]["C7"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.convert(report(record), source_version=SOURCE)
        missing = row()
        del missing["data"]["C7"]
        wrong_time = row(True)
        for raw in (report(missing), report(wrong_time), report(row(2000), row(1000)),
                    report({"meta": {"type": "OTHER"}, "data": {}}),
                    report({"meta": {"type": "RCOU"}, "data": []}),
                    report(row()).replace(BASIS.encode(), b"raw_wire"),
                    report(row()).replace(b'"C1": 1001', b'"C1": 1001, "C1": 2'),
                    report(row()).replace(b'"C1": 1001', b'"C1": NaN')):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                module.convert(raw, source_version=SOURCE)

    def test_resource_limits_and_equal_records(self):
        raw = report(row(), row())
        text, counts = self.module.convert(raw, source_version=SOURCE)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(text)))), 2)
        self.assertEqual(counts["selected_records"], 2)
        for name, limit in (("MAX_INPUT", len(raw) - 1), ("MAX_OUTPUT", 100),
                            ("MAX_ROWS", 1), ("MAX_RECORD_BYTES", 30), ("MAX_REPORT", 30)):
            with self.subTest(bound=name), patch.object(self.module, name, limit):
                with self.assertRaises(ValueError):
                    self.module.convert(raw, source_version=SOURCE)

    def test_cli_preserves_input_once_and_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output = folder / "input.json", folder / "result"
            raw = report(row(), {"meta": {"type": "FUTURE"}, "data": {"opaque": [1, 2]}})
            source.write_bytes(raw)
            command = [os.sys.executable, str(SCRIPT), str(source), str(output),
                       "--source-version", SOURCE]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((output / "source-report.json").read_bytes(), raw)
            self.assertEqual(set(p.name for p in output.iterdir()),
                             {"source-report.json", "observations.csv", "report.json"})
            self.assertEqual(json.loads((output / "report.json").read_text())["not_selected_records"], 1)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            rejected = folder / "rejected"
            command[3] = str(rejected)
            command[-1] = "different-version"
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(rejected.exists())


if __name__ == "__main__":
    unittest.main()
