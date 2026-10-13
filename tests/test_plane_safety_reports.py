"""Fixed Plane reports; authored records, not flight evidence."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_ardupilot_controller_reports import module, record, err, report, PROFILE, READER


class PlaneSafetyTests(unittest.TestCase):
    def convert(self, records):
        text, summary = module.convert(report(records), producer="plane-4.5.5")
        return text, summary, list(csv.DictReader(io.StringIO(text)))

    def test_two_authored_inputs_common(self):
        inputs = [
            [err(100, 9, 3), record("MSG", {"TimeUS": 101, "Message": "RC Short Failsafe On", "future": 7})],
            [err(200, 9, 0), record("MSG", {"TimeUS": 201, "Message": "GCS Failsafe On: RTL"}),
             record("MSG", {"TimeUS": 202, "Message": "GCS Failsafe Off"})],
        ]
        for records in inputs:
            text, summary, rows = self.convert(records)
            self.assertEqual(summary["selected_rows"], len(records))
            self.assertEqual(rows[0]["safety_producer_pin"], module.PLANE_PIN)
            self.assertEqual(rows[0]["err_reported_code"], str(records[0]["data"]["ECode"]))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "observations.csv"
                path.write_text(text)
                common = json.loads(subprocess.run([READER, str(PROFILE), str(path)],
                                    check=True, capture_output=True).stdout)
            self.assertEqual(len(common["observations"]), len(records))
            self.assertTrue(all(o["clock_basis"] == "BootRelative" for o in common["observations"]))
            self.assertIn("safety_reported_state", str(common["observations"]))
        self.assertEqual(rows[0]["safety_reported_state"], "BREACH_CLEARED_REPORTED")
        self.assertEqual(rows[-1]["safety_reported_state"], "CLEARED_REPORTED")

    def test_unqualified_accounting_and_failures(self):
        records = [err(1, 9, 1), err(2, 6, 1), record("PM", {"TimeUS": 3}),
                   record("MSG", {"TimeUS": 4, "Message": "GCS:RC Short Failsafe On"}),
                   record("MSG", {"TimeUS": 5, "Message": "RC Short Failsafe On", "Seq": 1}),
                   record("MSG", {"TimeUS": 6, "Message": "GCS Failsafe On: " + "X" * 50})]
        _, summary, rows = self.convert(records)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(summary["unselected_record_types"].values()), 5)
        for bad in ([err(1, 9, 256)], [err(2, 9, 1), err(1, 9, 0)], [err(1, True, 0)]):
            with self.assertRaises(ValueError):
                self.convert(bad)
        with self.assertRaises(ValueError):
            module.convert(report(records), producer="plane-4.5.6")
        before = module.convert(report([err(1, 6, 1)]))
        after = module.convert(report([err(1, 6, 1)]), producer=None)
        self.assertEqual(before, after)
        self.assertNotIn("safety_reported_state", before[0])

    def test_fence_named_bits_and_unknown_reach_common(self):
        text, _, rows = self.convert([err(1, 9, 143), err(2, 9, 0)])
        self.assertEqual(rows[0]["fence_breach_types_reported"], "ALT_MAX|CIRCLE|POLYGON|ALT_MIN")
        self.assertEqual(rows[0]["fence_unknown_bits_reported"], "128")
        self.assertEqual(rows[1]["safety_reported_reason"], "ERROR_RESOLVED")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            common = json.loads(subprocess.run([READER, str(PROFILE), str(path)],
                                check=True, capture_output=True).stdout)
        self.assertIn("ALT_MAX|CIRCLE|POLYGON|ALT_MIN", str(common["observations"]))
        self.assertIn("UNKNOWN_BITS_128", str(common["observations"]))

    def test_cli_raw_original_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            target = Path(directory) / "output"
            raw = report([err(1, 9, 1)])
            source.write_bytes(raw)
            command = [os.sys.executable, module.__file__, str(source), str(target), "--producer", "plane-4.5.5"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((target / "source-report.json").read_bytes(), raw)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_battery_low_critical_without_mode_common(self):
        for text, instance, reason in (("Battery 1 is low 10.50V used 250 mAh", "1", "BATTERY_LOW"),
                                      ("Battery 2 is critical 9.00V used 999 mAh", "2", "BATTERY_CRITICAL")):
            converted, summary, rows = self.convert([record("MSG", {"TimeUS": 100, "Message": text})])
            self.assertEqual(rows[0]["safety_reported_reason"], reason)
            self.assertEqual(rows[0]["battery_instance_reported"], instance)
            self.assertEqual(summary["selected_rows"], 1)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "observations.csv"
                path.write_text(converted)
                common = json.loads(subprocess.run([READER, str(PROFILE), str(path)],
                                    check=True, capture_output=True).stdout)
            self.assertIn(reason, str(common["observations"]))
            self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")

    def test_battery_false_positive_not_promoted(self):
        bad = ["Battery 0 is low 10.50V used 250 mAh", "Battery 17 is low 10.50V used 250 mAh",
               "Battery 1 is clear 10.50V used 250 mAh", "Battery 1 is low 10.5V used 250 mAh",
               "GCS:Battery 1 is low 10.50V used 250 mAh", "Battery 1 is low 10.50V used 250"]
        records = [err(1, 9, 0)] + [record("MSG", {"TimeUS": i+2, "Message": text}) for i, text in enumerate(bad)]
        _, summary, rows = self.convert(records)
        self.assertEqual(len(rows), 1)
        self.assertEqual(summary["unselected_record_types"]["MSG"], len(bad))
