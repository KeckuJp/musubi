"""Authored saved Cockpit `.clog` documents; no device, no vehicle, no real recording.

Every input here is constructed from the pinned writer's own expressions
(bluerobotics/cockpit 9e48d8d671236223bc640c695f787eafe4e26e53, src/libs/sensors-logging.ts).
The adopted engineering-unit transforms and the common CSV reader are reused, not re-exercised.
"""
import csv
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/cockpit-clog"
SCRIPT = ROOT / "scripts/convert_cockpit_clog.py"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
DEGREE = "°"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = load(SCRIPT)
EPOCH = 1789000000123


def variable(value, last_changed=EPOCH, display_name=""):
    return {"displayName": display_name, "value": value, "lastChanged": last_changed}


def point(**changes):
    """A log point shaped exactly as the pinned write site builds one."""
    data = {
        "Roll": variable(f"-12.5 {DEGREE}", display_name="Roll"),
        "Pitch": variable(f"3.0 {DEGREE}", EPOCH - 123, "Pitch"),
        "Heading": variable(f"271.4 {DEGREE}", display_name="Heading"),
        "Depth": variable("12.34 m", display_name="Depth"),
        "Mode": variable("MANUAL", display_name="Mode"),
        "Battery voltage": variable("15.80 V", display_name="Battery voltage"),
        "Battery current": variable("2.25 A", display_name="Battery current"),
        "GPS satellites": variable("11", display_name="GPS satellites"),
        "GPS status": variable("3D fix", display_name="GPS status"),
        "Latitude": variable(f"-27.123456 {DEGREE}", display_name="Latitude"),
        "Longitude": variable(f"153.000001 {DEGREE}", display_name="Longitude"),
        "Mission name": variable("Cockpit"),
        "Time": variable("07:55:01 GMT+3"),
        "Date": variable("Sep 21, 2026"),
        "Instantaneous power": variable("35.6 W", display_name="Instantaneous power"),
        "Tether turns": variable("2 turns CW", EPOCH - 23, "Tether turns"),
    }
    data.update(changes.pop("data", {}))
    return {"epoch": changes.pop("epoch", EPOCH), "seconds": changes.pop("seconds", 0), "data": data}


def rows(document):
    return list(csv.DictReader(io.StringIO(module.convert(json.dumps(document)))))


class CockpitLogTests(unittest.TestCase):
    def test_reported_display_values_reach_their_declared_units(self):
        """The writer's own tokens decide what is parsed; nothing else is given a unit."""
        first = rows([point()])[0]
        self.assertEqual(first["record_time_us"], str(EPOCH * 1000))
        self.assertAlmostEqual(float(first["cockpit_roll_rad"]), math.radians(-12.5))
        self.assertAlmostEqual(float(first["cockpit_heading_rad"]), math.radians(271.4))
        self.assertEqual(float(first["cockpit_depth_m"]), 12.34)
        self.assertEqual(first["cockpit_depth_unit_token"], "m")
        self.assertEqual(float(first["cockpit_battery_voltage_v"]), 15.8)
        self.assertEqual(float(first["cockpit_battery_current_a"]), 2.25)
        self.assertEqual(float(first["cockpit_instantaneous_power_w"]), 35.6)
        self.assertEqual(float(first["cockpit_latitude_deg"]), -27.123456)
        self.assertEqual(float(first["cockpit_longitude_deg"]), 153.000001)
        self.assertEqual(first["cockpit_gps_satellites_reported"], "11")
        self.assertEqual({first["cockpit_roll_status"], first["cockpit_depth_status"],
                          first["cockpit_battery_voltage_status"]}, {"REPORTED"})
        self.assertEqual(bytes.fromhex(first["cockpit_mode_hex"][4:]).decode(), "MANUAL")
        self.assertEqual(bytes.fromhex(first["cockpit_reported_time_hex"][4:]).decode(), "07:55:01 GMT+3")
        self.assertEqual(bytes.fromhex(first["cockpit_reported_date_hex"][4:]).decode(), "Sep 21, 2026")
        self.assertEqual((first["cockpit_variable_count"], first["cockpit_selected_count"],
                          first["cockpit_retained_only_count"]), ("16", "15", "1"))
        retained = json.loads(bytes.fromhex(first["source_record_hex"][4:]))
        self.assertEqual(retained["data"]["Tether turns"]["value"], "2 turns CW")
        self.assertNotIn("cockpit_tether_turns", first)
        self.assertEqual(first["cockpit_selected_max_stale_ms"], "123")
        self.assertEqual(first["cockpit_reported_seconds_text"], "i64:0")
        self.assertIn("NOT_RAW_SENSOR_ACCURACY", first["cockpit_value_basis"])
        self.assertIn("NEGATED_MSL_ALTITUDE", first["cockpit_depth_basis"])
        self.assertIn("NO_VEHICLE_IDENTITY", first["cockpit_identity_basis"])

    def test_the_operators_unit_preference_travels_and_feet_are_not_converted(self):
        """unitAbbreviation declares exactly m and ft; this pin declares no foot length."""
        metres, feet = rows([point(), point(epoch=EPOCH + 1000,
                                            data={"Depth": variable("40.49 ft", display_name="Depth")})])
        self.assertEqual((metres["cockpit_depth_unit_token"], metres["cockpit_depth_status"]),
                         ("m", "REPORTED"))
        self.assertEqual(feet["cockpit_depth_unit_token"], "ft")
        self.assertEqual(feet["cockpit_depth_m"], "")
        self.assertIn("FEET_WHOSE_DEFINITION_THIS_PIN_DOES_NOT_DECLARE", feet["cockpit_depth_status"])

    def test_the_writers_own_unavailable_literals_never_become_zero(self):
        unavailable = rows([point(data={
            "Roll": variable(f"undefined {DEGREE}", display_name="Roll"),
            "Depth": variable("NaN m", display_name="Depth"),
            "Battery voltage": variable("undefined V", display_name="Battery voltage"),
            "GPS satellites": variable("Unknown", display_name="GPS satellites"),
            "Mode": variable("Unknown", display_name="Mode"),
        })])[0]
        for field in ("cockpit_roll_rad", "cockpit_depth_m", "cockpit_battery_voltage_v",
                      "cockpit_gps_satellites_reported"):
            self.assertEqual(unavailable[field], "", field)
        for status in ("cockpit_roll_status", "cockpit_depth_status", "cockpit_battery_voltage_status",
                       "cockpit_gps_satellites_status"):
            self.assertEqual(unavailable[status], "REPORTED_UNAVAILABLE_TEXT_RETAINED", status)
        self.assertEqual(bytes.fromhex(unavailable["cockpit_mode_hex"][4:]).decode(), "Unknown")
        self.assertEqual(json.loads(bytes.fromhex(unavailable["source_record_hex"][4:]))
                         ["data"]["Depth"]["value"], "NaN m")

    def test_absent_variables_and_undeclared_tokens_are_distinguished(self):
        absent = rows([{"epoch": EPOCH, "data": {"Mode": variable("AUTO", display_name="Mode")}}])[0]
        self.assertEqual(absent["cockpit_roll_status"], "NOT_REPORTED")
        self.assertEqual(absent["cockpit_depth_status"], "NOT_REPORTED")
        self.assertEqual(absent["cockpit_selected_max_stale_ms"], "0")
        self.assertEqual(absent["cockpit_reported_seconds_text"], "")  # not every saved point carries it
        odd = rows([point(data={"Depth": variable("12.34 fathoms", display_name="Depth"),
                                "Battery voltage": variable("15.80 mV", display_name="Battery voltage")})])[0]
        self.assertEqual(odd["cockpit_depth_m"], "")
        self.assertIn("UNIT_TOKEN_NOT_DECLARED", odd["cockpit_depth_status"])
        self.assertEqual(odd["cockpit_battery_voltage_v"], "")
        self.assertIn("UNIT_TOKEN_NOT_DECLARED", odd["cockpit_battery_voltage_status"])

    def test_wrong_shapes_and_accounting_violations_are_refused(self):
        good = point()
        for label, document in (
                ("not an array", {"epoch": EPOCH}),
                ("empty array", []),
                ("point not an object", ["x"]),
                ("missing epoch", [{"data": good["data"]}]),
                ("non-integer epoch", [{"epoch": 1.5, "data": good["data"]}]),
                ("boolean epoch", [{"epoch": True, "data": good["data"]}]),
                ("negative epoch", [{"epoch": -1, "data": good["data"]}]),
                ("missing data", [{"epoch": EPOCH}]),
                ("data not an object", [{"epoch": EPOCH, "data": []}]),
                ("empty data", [{"epoch": EPOCH, "data": {}}]),
                ("entry not an object", [{"epoch": EPOCH, "data": {"Roll": "-12.5"}}]),
                ("value not a string", [{"epoch": EPOCH, "data": {"Roll": {"value": -12.5, "lastChanged": EPOCH}}}]),
                ("missing lastChanged", [{"epoch": EPOCH, "data": {"Roll": {"value": f"1 {DEGREE}"}}}]),
                ("negative seconds", [{"epoch": EPOCH, "seconds": -1, "data": good["data"]}]),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                module.convert(json.dumps(document))
        with self.assertRaises(ValueError):
            module.convert('[{"epoch":1,"epoch":1,"data":{"Mode":{"value":"A","lastChanged":1}}}]')
        with self.assertRaises(ValueError):
            module.convert('[{"epoch":1,"data":{"Mode":{"value":"A","lastChanged":NaN}}}]')

    def test_command_line_and_common_reader_carry_the_reported_meaning(self):
        configured = os.environ.get("MUSUBI_TELEMETRY_READER")
        if configured:
            self.assertTrue(Path(configured).exists(),
                            f"MUSUBI_TELEMETRY_READER is set to {configured!r}, which does not exist; "
                            "an explicitly configured reader that is missing is a failure, not a skip")
        elif not Path(READER).exists():
            self.skipTest("no reader configured and the default build is absent, so the common-output "
                          "proof cannot run; this is an explicit skip and never a pass")
        document = [point(epoch=EPOCH, seconds=0,
                          data={"Depth": variable("3.500 m", display_name="Depth"),
                                "Roll": variable(f"45.0 {DEGREE}", display_name="Roll")}),
                    point(epoch=EPOCH + 2000, seconds=2,
                          data={"Battery voltage": variable("14.05 V", display_name="Battery voltage"),
                                "Depth": variable(f"undefined m", display_name="Depth")})]
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "saved.clog", Path(directory) / "out.csv"
            source.write_text(json.dumps(document))
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)],
                           check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(target), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["platform_domain"], "Unknown")
        first, second = (observation["fields"] for observation in common["observations"])
        self.assertAlmostEqual(first["cockpit_roll_rad"], math.radians(45.0))
        self.assertEqual(first["cockpit_depth_m"], 3.5)
        self.assertEqual(second["cockpit_battery_voltage_v"], 14.05)
        self.assertIsNone(second["cockpit_depth_m"])
        self.assertEqual(second["cockpit_depth_status"], "REPORTED_UNAVAILABLE_TEXT_RETAINED")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]),
                        [o["clock_basis"] for o in common["observations"]])
        self.assertIn("NOT_VEHICLE_TIME", first["cockpit_clock_basis"])
        units = common["profile_units"]
        self.assertIn("not_the_vehicles_sampling_time", units["record_time_us"])
        self.assertIn("never_a_sounding", units["cockpit_depth_m"])
        self.assertIn("no_datum_or_frame", units["cockpit_latitude_deg"])


if __name__ == "__main__":
    unittest.main()
