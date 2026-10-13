"""Plane 4.5.5 saved GPS/BAT/BCL/VER/MSG rows reach the existing routes unchanged.

Authored input only, in the declarations read at ArduPilot Plane 4.5.5
`d6ebfc21844e2bc0ab28c8c688b8dae94abccf5a`. General use and limitations are in
`docs/saved-input-windows.md`; this test fixes the writer revision above.
No vehicle, no device, no real record: **real records 0**. Values are already pymavlink-decoded
(GPS Lat/Lng degrees, Alt metres, BAT Temp degrees C, CurrTot mAh), so no reader may rescale them.
ATT at this pin is covered by the existing `tests/test_plane_copter_att.py`.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
JSONL_CASE = ROOT / "profiles/declared/arduplane-jsonl"
BAT_CASE = ROOT / "profiles/declared/ardupilot-battery"
SCRIPT = ROOT / "scripts/convert_ardupilot_battery_csv.py"
MODE_SCRIPT = ROOT / "scripts/convert_ardupilot_mode_csv.py"
VEHICLE_PROFILE = ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"
JSON_READER_ENV = "MUSUBI_MAVLOG_READER"
CSV_READER_ENV = "MUSUBI_TELEMETRY_READER"

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
battery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(battery)
mode_spec = importlib.util.spec_from_file_location(MODE_SCRIPT.stem, MODE_SCRIPT)
identity = importlib.util.module_from_spec(mode_spec)
mode_spec.loader.exec_module(identity)


def run_reader(case, variable, *args):
    reader = os.environ.get(variable)
    case.assertTrue(reader, f"{variable} must name the built reader example; not set")
    case.assertTrue(Path(reader).is_file(), f"{variable}={reader!r} does not exist")
    finished = subprocess.run([reader, *map(str, args)], capture_output=True)
    case.assertEqual(finished.returncode, 0, finished.stderr[:400])
    return json.loads(finished.stdout)


def record(kind, data):
    return json.dumps({"meta": {"type": kind}, "data": data})


class PlaneGpsBindingTests(unittest.TestCase):
    def test_two_gps_instances_stay_separate_decoded_and_domain_unknown(self):
        rows = [
            record("GPS", {"TimeUS": 7000, "I": 0, "Status": 3, "GMS": 1000, "GWk": 2300,
                           "NSats": 14, "HDop": 0.71, "Lat": -35.3632621, "Lng": 149.1652374,
                           "Alt": 584.07, "Spd": 12.5, "GCrs": 271.3, "VZ": -0.4, "Yaw": 0,
                           "U": 1, "Future": "kept"}),
            record("GPS", {"TimeUS": 7000, "I": 1, "Status": 1, "GMS": 1000, "GWk": 2300,
                           "NSats": 4, "HDop": 99.99, "Lat": 0, "Lng": 0, "Alt": 0,
                           "Spd": 0, "GCrs": 0, "VZ": 0, "Yaw": 0, "U": 0}),
            record("PARM", {"Name": "GPS_TYPE", "Value": 1}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selected.jsonl"
            path.write_text("\n".join(rows) + "\n")
            report = run_reader(self, JSON_READER_ENV, JSONL_CASE / "profile.toml", path)
        self.assertEqual(report["platform_domain"], "Unknown")
        self.assertEqual(report["untimed_records"], 1)
        self.assertEqual(len(report["source_records"]), 3)
        fields = [o["fields"] for o in report["observations"]]
        self.assertEqual([f["GPS.I"] for f in fields], [0, 1])
        self.assertTrue(all(o["t_boot_us"] == 7000 and o["clock_basis"] == "BootRelative"
                            and o["wall_ms"] is None for o in report["observations"]))
        self.assertEqual(fields[0]["GPS.Lat"], -35.3632621)
        self.assertEqual(fields[0]["GPS.Alt"], 584.07)
        self.assertEqual(fields[0]["GPS.HDop"], 0.71)
        self.assertEqual(fields[0]["GPS.Future"], "kept")
        self.assertEqual((fields[1]["GPS.Status"], fields[1]["GPS.Lat"]), (1, 0))
        self.assertEqual(report["profile_units"]["GPS.Alt"], "m")


class PlaneBatteryBindingTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bat.csv"
            path.write_text(text)
            return run_reader(self, CSV_READER_ENV, BAT_CASE / "profile.toml", path,
                              "--allow-equal-time", "--preserve-nonfinite-as-text")

    def test_plane_455_bat_uses_the_inst_layout_with_missing_current(self):
        text = battery.convert(
            "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH\n"
            "9000,0,16.2,16.4,-2.5,350,5.6,0,0.02,255,1,0\n"
            "9000,1,12.1,12.2,NaN,NaN,NaN,31.25,NaN,80,0,95\n",
            "ardupilot-bat-inst", bat_details=True)
        common = self.read(text)
        self.assertEqual(common["platform_domain"], "Unknown")
        fields = [o["fields"] for o in common["observations"]]
        self.assertEqual([f["Inst"] for f in fields], [0, 1])
        self.assertEqual(fields[0]["battery_current_a"], -2.5)
        self.assertEqual(fields[0]["battery_consumed_ah"], 0.35)
        self.assertIsNone(fields[0]["battery_remaining_fraction"])
        self.assertEqual(fields[0]["battery_temperature_state"], "ZERO_OR_UNAVAILABLE")
        self.assertIsNone(fields[0]["battery_temperature_k"])
        self.assertIsNone(fields[0]["battery_state_of_health_fraction"])
        for key in ("battery_current_a", "battery_consumed_ah", "battery_consumed_j",
                    "battery_power_w", "battery_resistance_estimate_ohm"):
            self.assertIsNone(fields[1][key], key)
        self.assertEqual(fields[1]["battery_temperature_k"], 304.4)
        self.assertEqual(fields[1]["battery_state_of_health_fraction"], 0.95)

    def test_plane_455_bcl_keeps_stored_millivolts_plus_one(self):
        cells = ",".join(f"V{i}" for i in range(1, 13))
        text = battery.convert(
            f"TimeUS,Instance,Volt,{cells}\n"
            "9000,0,16.2,4051,4049,1,0" + ",0" * 8 + "\n",
            "ardupilot-bcl-4.3")
        fields = self.read(text)["observations"][0]["fields"]
        self.assertEqual(fields["battery_cell_1_v"], 4.05)
        self.assertEqual(fields["battery_cell_3_v"], 0)
        self.assertIsNone(fields["battery_cell_4_v"])
        self.assertEqual(fields["battery_voltage_v"], 16.2)


class PlaneReportedIdentityTests(unittest.TestCase):
    HEADER = "TimeUS,BT,BST,Maj,Min,Pat,FWT,GH,FWS,APJ,BU\n"

    def ver(self, fws, version="4,5,5"):
        return identity.convert(self.HEADER + f"1000,3,0,{version},255,3605789729,{fws},9,3\n",
                                "plane-4.5.5", "ver")

    def test_ver_reports_release_through_declared_vehicle_profile(self):
        expected = (("ArduPlane V4.5.5", "ArduPlane", "", "AGREES_WITH_DECLARED_RELEASE"),
                    ("ArduPlane V4.5.5 (d6ebfc21)", "ArduPlane", "d6ebfc21",
                     "AGREES_WITH_DECLARED_RELEASE_BUILD_COMMIT_NOT_CHECKED"),
                    ("Vendor 7", "", "", "CUSTOM_FIRMWARE_STRING_NO_FAMILY_ESTABLISHED_RETAINED"))
        with tempfile.TemporaryDirectory() as directory:
            for index, (fws, family, build, agreement) in enumerate(expected):
                path = Path(directory) / f"ver{index}.csv"
                path.write_text(self.ver(fws))
                common = run_reader(self, CSV_READER_ENV, VEHICLE_PROFILE, path)
                self.assertEqual(common["platform_domain"], "Unknown")
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["reported_firmware_family"], family or None)
                self.assertEqual(fields["reported_firmware_version"], "4.5.5")
                self.assertEqual(fields["reported_firmware_build_annotation"], build or None)
                self.assertEqual(fields["declared_vehicle_agreement"], agreement)
                self.assertEqual(fields["FWS"], fws)
        units = common["profile_units"]
        for key in ("FWS", "GH", "reported_firmware_family", "reported_firmware_version",
                    "declared_vehicle_agreement", "reported_board_target_id"):
            self.assertIn(key, units)
        self.assertIn("not_installed_device_proof", units["reported_firmware_version"])
        self.assertNotIn("ArduRover_or_ArduSub", units["reported_firmware_family"])
        for fws, version in (("ArduPlane V4.5.6", "4,5,6"), ("ArduCopter V4.5.5", "4,5,5")):
            with self.assertRaises(ValueError, msg=fws):
                self.ver(fws, version)

    def test_msg_identity_is_a_log_claim_and_custom_string_stays_unknown(self):
        gps = record("GPS", {"TimeUS": 2000, "I": 0, "Lat": 1.5, "Lng": 2.5, "Alt": 3.5})
        with tempfile.TemporaryDirectory() as directory:
            for message, domain in (("ArduPlane V4.5.5 (d6ebfc21)", "Air"),
                                    ("Vendor 7 [ArduPlane V4.5.5]", "Unknown")):
                path = Path(directory) / "msg.jsonl"
                path.write_text(record("MSG", {"TimeUS": 1000, "Message": message}) + "\n" + gps + "\n")
                report = run_reader(self, JSON_READER_ENV, JSONL_CASE / "profile.toml", path)
                self.assertEqual(report["platform_domain"], domain, message)


if __name__ == "__main__":
    unittest.main()
