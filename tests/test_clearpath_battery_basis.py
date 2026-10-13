"""Authored saved reports, not installed Clearpath hardware evidence."""
import csv
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.test_electrical_schema_reuse import module, ros, ROOT

BASIS = "clearpath-estimator-ac11c88a"


class ClearpathBasisTest(unittest.TestCase):
    def test_estimator_state_temperature_and_power_basis(self):
        for change in ({"temperature": 0}, {"power_supply_status": 0}, {"present": False}):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(ros() | change), "ros-battery-json", ros_battery_basis=BASIS)
        record = ros() | {"temperature": "NaN", "power_supply_status": 4}
        result = module.convert(json.dumps(record), "ros-battery-json", ros_battery_basis=BASIS)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "common reader must be supplied")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "saved.csv"
            target.write_text(result)
            common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"), str(target)], check=True, capture_output=True).stdout)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["battery_power_basis"], "ARITHMETIC_REPORTED_V_TIMES_A_POLARITY_UNQUALIFIED")
            self.assertEqual(fields["battery_charging_state_basis"], "REPORTED_CHARGER_FLAG_OR_LUT_FULL_NOT_PHYSICAL_STATE_PROOF")
            self.assertEqual(fields["battery_temperature_basis"], "UNAVAILABLE_SOURCE_NAN")

    def test_two_layouts_common_and_default(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "common reader must be supplied")
        for current, charge, fraction in ((-2, 3, .75), (4, 0, 0)):
            record = ros()
            record.update(current=current, charge=charge, percentage=fraction,
                          future={"field": [current]}, cell_voltage=[12, 12])
            text = json.dumps(record)
            default = list(csv.DictReader(io.StringIO(module.convert(text, "ros-battery-json"))))[0]
            self.assertEqual(default["battery_current_sign_basis"], "NEGATIVE_DISCHARGING")
            result = module.convert(text, "ros-battery-json", ros_battery_basis=BASIS)
            row = list(csv.DictReader(io.StringIO(result)))[0]
            self.assertEqual(float(row["battery_current_a"]), current)
            self.assertEqual(row["battery_remaining_fraction"], "")
            self.assertEqual(row["battery_stored_charge_ah"], "")
            self.assertEqual(json.loads(bytes.fromhex(row["source_record_hex"][4:])), record)
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "saved.csv"
                target.write_text(result)
                common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"), str(target)], check=True, capture_output=True).stdout)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["battery_current_a"], current)
                self.assertEqual(fields["battery_current_sign_basis"], "UNQUALIFIED_COPIED_POWER")
                self.assertIn("source_record_hex", fields["battery_estimate_basis"])
                self.assertIn("REPORTED_ZERO" if fraction == 0 else "REPORTED_NONZERO", fields["battery_estimate_basis"])
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")

    def test_rejection(self):
        for basis, fmt in (("wrong", "ros-battery-json"), (BASIS, "vda-state-3.0")):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(ros()), fmt, ros_battery_basis=basis)
        for field, value in (("current", "bad"), ("percentage", 1.1), ("voltage", -1)):
            record = ros()
            record[field] = value
            with self.assertRaises(ValueError):
                module.convert(json.dumps(record), "ros-battery-json", ros_battery_basis=BASIS)
        record = ros()
        record["header"]["stamp"]["nanosec"] = 10**9
        with self.assertRaises(ValueError):
            module.convert(json.dumps(record), "ros-battery-json", ros_battery_basis=BASIS)
        record = ros()
        record.update(percentage="NaN", charge="NaN")
        row = list(csv.DictReader(io.StringIO(module.convert(json.dumps(record), "ros-battery-json", ros_battery_basis=BASIS))))[0]
        self.assertIn("percentage=MISSING_NAN;charge=MISSING_NAN", row["battery_estimate_basis"])
