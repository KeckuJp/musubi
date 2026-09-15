"""Selected schemas: synthetic contract checks, not real-device evidence."""
import csv
import importlib.util
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("battery", ROOT / "scripts/convert_ardupilot_battery_csv.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def vda(version):
    old = version in ("2.0.0", "2.1.0")
    return {"timestamp": "2026-01-01T00:00:00.123456789Z", "version": version,
            "manufacturer": "synthetic", "serialNumber": "example-1",
            "batteryState" if old else "powerSupply": {
                "batteryCharge" if old else "stateOfCharge": 75,
                "batteryVoltage": 24, "charging": False}, "future": {"field": [1, 2]}}


def ros():
    return {"header": {"stamp": {"sec": 2, "nanosec": 123456789}, "frame_id": "battery"},
            "voltage": 24, "current": -2, "charge": 3, "percentage": .75,
            "power_supply_status": 2, "present": True, "future": ["new"]}


class ElectricalReuseTests(unittest.TestCase):














    def test_qualified_missing_voltage_is_opt_in_and_not_zero_power(self):
        missing = ros()
        missing.update(voltage="NaN", current="NaN", charge="NaN", percentage="NaN", power_supply_status=0)
        text = json.dumps(missing) + "\n" + json.dumps(ros())
        with self.assertRaises(ValueError): module.convert(text, "ros-battery-json")
        output = module.convert(text, "ros-battery-json", allow_missing_voltage=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["battery_voltage_disposition"], "UNAVAILABLE_SOURCE_NAN")
        self.assertEqual(rows[0]["battery_voltage_v"], "")
        self.assertEqual(rows[0]["battery_power_w"], "")
        self.assertEqual(rows[1]["battery_voltage_disposition"], "REPORTED")
        self.assertEqual(float(rows[1]["battery_power_w"]), -48.)
        for bad in ("Infinity", "-Infinity", None, True, -1):
            item = dict(missing, voltage=bad)
            with self.assertRaises(ValueError): module.convert(json.dumps(item), "ros-battery-json", allow_missing_voltage=True)
        with self.assertRaises(ValueError): module.convert(json.dumps(vda("3.0.0")), "vda-state-3.0", allow_missing_voltage=True)


    def test_vda_error_details_absent_empty_and_invalid(self):
        fmt = "vda-state-3.0"
        def rows(record):
            return list(csv.DictReader(io.StringIO(module.convert(json.dumps(record), fmt, vda_errors=True))))
        record = vda("3.0.0")
        self.assertEqual(rows(record)[0]["vda_error_warning_count"], "")
        self.assertEqual(rows({**record, "errors": []})[0]["vda_error_warning_count"], "0")
        error = {"errorType": "x", "errorLevel": "FUTURE", "errorReferences": []}
        self.assertEqual(rows({**record, "errors": [error]})[1]["vda_error_reference_count"], "0")
        for invalid in ({**error, "errorHint": 1}, {**error, "errorReferences": None},
                        {**error, "errorReferences": [{}]},
                        {**error, "errorReferences": [{"referenceKey": "x", "referenceValue": 1}]}):
            with self.assertRaises(ValueError): rows({**record, "errors": [invalid]})
        with self.assertRaises(ValueError): module.convert(json.dumps(vda("2.0.0")), "vda-state-2.0", vda_errors=True)


    def test_individual_vda_actions_missing_wrong_types_and_legacy(self):
        record = vda("3.0.0")
        fmt = "vda-state-3.0"
        def rows(value):
            return list(csv.DictReader(io.StringIO(module.convert(json.dumps(value), fmt, vda_actions=True))))
        self.assertEqual(rows(record)[0]["vda_action_finished_count"], "")
        record["actionStates"] = []
        self.assertEqual(rows(record)[0]["vda_action_finished_count"], "0")
        action = {"actionId": "a", "actionStatus": "FINISHED", "resultDescription": "old-only"}
        record["actionStates"] = [action]
        self.assertEqual(rows(record)[1]["vda_action_result_hex"], "")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(module.convert(json.dumps(record), fmt))))), 1)
        for actions in ([action, action], [{**action, "actionResult": 1}],
                        [{**action, "actionType": None}], [{**action, "actionStatus": 1}]):
            with self.assertRaises(ValueError):
                rows({**record, "actionStates": actions})
        with self.assertRaises(ValueError):
            module.convert("", "ardupilot-bat", vda_actions=True)
        with self.assertRaises(ValueError):
            rows({**record, "large": "x" * (512 * 1024)})


    def test_current_sign_basis_does_not_infer_direction_from_status(self):
        record = ros()
        record["power_supply_status"] = 1
        row = self.rows(record, "ros-battery-json")
        self.assertEqual(row["battery_current_sign_basis"], "NEGATIVE_DISCHARGING")
        self.assertEqual(float(row["battery_current_a"]), -2)
        self.assertEqual(float(row["battery_power_w"]), -48)
        self.assertEqual(row["battery_reported_charging_state"], "CHARGING")
        for charging in (False, True):
            record = vda("3.0.0")
            record["powerSupply"].update(batteryCurrent=-2, charging=charging)
            row = self.rows(record, "vda-state-3.0")
            self.assertEqual(row["battery_current_sign_basis"], "UNSPECIFIED")
            self.assertEqual(float(row["battery_current_a"]), -2)
        self.assertEqual(self.rows(vda("2.1.0"), "vda-state-2.1")["battery_current_sign_basis"], "UNSPECIFIED")

    def test_vda_reported_states_version_boundaries_and_vehicle_units(self):
        for version, fmt in [("2.0.0", "vda-state-2.0"), ("2.1.0", "vda-state-2.1"), ("3.0.0", "vda-state-3.0")]:
            old = version in ("2.0.0", "2.1.0")
            record = vda(version)
            record.update(operatingMode="TEACHIN" if old else "TEACH_IN", driving=True, paused=False,
                safetyState={"eStop" if old else "activeEmergencyStop": "MANUAL", "fieldViolation": True},
                velocity={"vx": -2, "vy": .5, "omega": -.2}, orderId='order,quoted"', orderUpdateId=4,
                lastNodeId="node-2", lastNodeSequenceId=2,
                actionStates=[{"actionId": "a1", "actionStatus": "RUNNING"},
                              {"actionId": "a2", "actionStatus": "RETRIABLE"},
                              {"actionId": "a3", "actionStatus": "PAUSED"}],
                errors=[{"errorType": "x", "errorLevel": "URGENT"}])
            row = self.rows(record, fmt)
            self.assertEqual(row["vda_operating_mode_reported"], "TEACH_IN")
            self.assertEqual(row["vda_driving_reported"], "1")
            self.assertEqual(row["vda_paused_reported"], "0")
            self.assertEqual(row["vda_emergency_stop_reported"], "MANUAL")
            self.assertEqual(float(row["vehicle_vx_m_s"]), -2.)
            self.assertEqual(float(row["vehicle_yaw_rate_rad_s"]), -.2)
            self.assertEqual(bytes.fromhex(row["vda_order_id_hex"][4:]).decode(), record["orderId"])
            self.assertEqual(row["vda_action_running_count"], "1")
            self.assertEqual(row["vda_action_paused_count"], "1")
            self.assertEqual(row["vda_action_unknown_count" if old else "vda_action_retriable_count"], "1")
            self.assertEqual(row["vda_error_unknown_count" if old else "vda_error_urgent_count"], "1")

    def test_vda_absent_unknown_and_bad_state_types(self):
        record = vda("3.0.0")
        self.assertEqual(self.rows(record, "vda-state-3.0")["vda_error_fatal_count"], "")
        record.update(errors=[], actionStates=[], operatingMode="FUTURE", driving=False)
        row = self.rows(record, "vda-state-3.0")
        self.assertEqual(row["vda_error_fatal_count"], "0")
        self.assertEqual(row["vda_action_failed_count"], "0")
        self.assertEqual(row["vda_operating_mode_reported"], "UNKNOWN")
        for extra in [{"driving": 0}, {"velocity": {"vx": "2"}}, {"orderUpdateId": True},
                      {"errors": {}}, {"errors": [{"errorType": "x", "errorLevel": 2}]},
                      {"safetyState": {"eStop": "NONE", "fieldViolation": False}}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                module.convert(json.dumps({**record, **extra}), "vda-state-3.0")

    def rows(self, record, fmt):
        text = json.dumps(record, ensure_ascii=False)
        rows = list(csv.DictReader(io.StringIO(module.convert(text, fmt))))
        self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
        return rows[0]

    def test_rover_alias_preserves_columns_and_old_result(self):
        old = "TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct\n1,0,24,2,1000,1,75\n"
        new = old.replace("Instance", "Inst")
        self.assertEqual(module.convert(new, "ardupilot-bat-inst"), module.convert(old).replace("Instance", "Inst"))
        with self.assertRaises(ValueError):
            module.convert(new)
        missing = new.replace(",2,1000,1,75", ",nan,nan,nan,255")
        row = next(csv.DictReader(io.StringIO(module.convert(missing, "ardupilot-bat-inst"))))
        self.assertEqual(float(row["battery_voltage_v"]), 24)
        self.assertEqual([row[k] for k in module.NORMALIZED[1:]], [""] * 5)

    def test_vda_version_boundary_and_optional_values(self):
        for version, fmt in [("2.1.0", "vda-state-2.1"), ("3.0.0", "vda-state-3.0")]:
            row = self.rows(vda(version), fmt)
            self.assertEqual(row["record_time_us"], "1767225600123456")
            self.assertEqual(float(row["battery_remaining_fraction"]), .75)
            self.assertEqual(float(row["battery_voltage_v"]), 24)
            self.assertEqual(row["battery_power_w"], "")
            self.assertEqual(row["battery_reported_charging_state"], "NOT_CHARGING")
        record = vda("3.0.0")
        record["powerSupply"]["batteryCurrent"] = 2
        self.assertEqual(float(self.rows(record, "vda-state-3.0")["battery_power_w"]), 48)

    def test_ros_sign_stored_not_consumed_enum_missing_and_presence(self):
        record = ros()
        row = self.rows(record, "ros-battery-json")
        self.assertEqual(row["record_time_us"], "2123456")
        self.assertEqual(float(row["battery_power_w"]), -48)
        self.assertEqual(float(row["battery_stored_charge_ah"]), 3)
        self.assertEqual(row["battery_consumed_ah"], "")
        record.update(current="NaN", charge="NaN", percentage="NaN", power_supply_status=255)
        row = self.rows(record, "ros-battery-json")
        self.assertEqual(row["battery_power_w"], "")
        self.assertEqual(row["battery_reported_charging_state"], "UNKNOWN_255")
        record["present"] = False
        self.assertTrue(all(self.rows(record, "ros-battery-json")[field] == "" for field in module.NORMALIZED))

    def test_important_failures_and_equal_time_accounting(self):
        for record, fmt in [(vda("3.0.0"), "vda-state-2.1"), (vda("2.1.0"), "vda-state-3.0")]:
            with self.assertRaises(ValueError):
                module.convert(json.dumps(record), fmt)
        for key, bad in [("current", True), ("percentage", 1.1), ("voltage", "NaN"),
                         ("present", 1), ("power_supply_status", 256)]:
            record = ros(); record[key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.convert(json.dumps(record), "ros-battery-json")
        for bad in ["", "{}", '{"voltage":1,"voltage":2}', '{"x":NaN}', "null", "\n"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.convert(bad, "ros-battery-json")
        text = json.dumps(ros())
        self.assertEqual(len(list(csv.DictReader(io.StringIO(module.convert(text + "\n" + text, "ros-battery-json"))))), 2)
        later = ros(); later["header"]["stamp"]["sec"] = 3
        with self.assertRaises(ValueError):
            module.convert(json.dumps(later) + "\n" + text, "ros-battery-json")


if __name__ == "__main__":
    unittest.main()
