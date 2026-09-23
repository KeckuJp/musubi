"""Saved motion reports through the adopted converters and the adopted bridge.

Nothing here decodes a new format, adds a producer or judges a machine. It composes what is
already adopted -- the AgOpenGPS steering converter and the saved-state-transitions bridge -- and
it binds the distinctions that keep a **demand**, a **measured feedback** and a **reported fault
word** from being read as one another.

Authored datagrams only, built with the adopted steering test's own builders. **No device, no real
record**, and a modelled packet is not evidence about any machine.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_agopen_steering import convert as convert_steering
from scripts.convert_saved_state_transitions import convert as convert_transitions
from tests.test_agopen_steering import IMU_BNO, PORT, datagram, pcap, steering_frame

ROOT = Path(__file__).resolve().parents[1]
STEERING_CASE = ROOT / "profiles/declared/agopen-steering-reuse"
DRONECAN_CASE = ROOT / "profiles/declared/dronecan-recorded"
TRANSITIONS_PROFILE = ROOT / "profiles/declared/saved-state-transitions/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER")
NO_IMU_HEADING = 9999
DECLARATION = {
    "clock_basis": "unknown",
    "states": (("agopen_heading_status",
                "AgOpenGPS PGN 253 reported IMU heading validity, as the adopted "
                "agopen-steering-reuse case declares it"),),
    "window_us": 4_000_000,
    "expected_interval_us": 1_000_000,
}


def unhex(cell):
    """The bridge carries declared text as `hex:` so a saved word cannot break a CSV row."""
    return bytes.fromhex(cell[4:]).decode() if cell.startswith("hex:") else cell


def declared_units(path):
    """The `[units]` table of an adopted case profile, read as text -- no TOML dependency."""
    units, inside = {}, False
    for line in path.read_text().splitlines():
        if line.strip() == "[units]":
            inside = True
        elif line.startswith("[") and inside:
            break
        elif inside and " = " in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition(" = ")
            units[name.strip()] = value.strip().strip('"')
    return units


class MotionReportCompositionTests(unittest.TestCase):
    def steering_observations(self, headings):
        """Authored PGN 253 reports through the adopted steering converter."""
        packets = [datagram(steering_frame(angle_hundredths=angle, heading_tenths=heading))
                   for angle, heading in headings]
        output, report = convert_steering(pcap(packets), PORT, IMU_BNO)
        return output, report

    def test_saved_steering_reports_reach_a_reported_validity_change_and_a_window(self):
        """U005: what the adopted pair actually produces from saved steering reports.

        A reported-validity word changing is a change of a reported word. It is not a steering
        fault, not a cause, and not evidence that anything moved.
        """
        steering, steering_report = self.steering_observations(
            [(-1234, 1234), (-1200, NO_IMU_HEADING), (-1100, 1300)])
        self.assertEqual(steering_report["decoded_frames"], 3)
        rows = list(csv.DictReader(io.StringIO(steering)))
        self.assertEqual([row["agopen_heading_status"] for row in rows],
                         ["REPORTED", "UNAVAILABLE_NO_IMU_SENTINEL_9999", "REPORTED"])
        self.assertEqual([float(row["agopen_steer_angle_deg"]) for row in rows],
                         [-12.34, -12.0, -11.0])

        output, report = convert_transitions(steering.encode(), **DECLARATION)
        events = list(csv.DictReader(io.StringIO(output)))
        transitions = [row for row in events if row["event_kind"] == "STATE_TRANSITION"]
        windows = [row for row in events if row["event_kind"] == "OBSERVATION_WINDOW"]
        self.assertEqual([row["transition_disposition"] for row in transitions],
                         ["FIRST_REPORTED_VALUE", "CHANGE_BETWEEN_TWO_REPORTED_VALUES",
                          "CHANGE_BETWEEN_TWO_REPORTED_VALUES"])
        self.assertEqual([(unhex(row["previous_state_hex"]), unhex(row["reported_state_hex"]))
                          for row in transitions[1:]],
                         [("REPORTED", "UNAVAILABLE_NO_IMU_SENTINEL_9999"),
                          ("UNAVAILABLE_NO_IMU_SENTINEL_9999", "REPORTED")])
        self.assertEqual(unhex(transitions[0]["previous_state_hex"]), "")
        for row in transitions:
            self.assertEqual(unhex(row["state_column_hex"]), "agopen_heading_status")
            self.assertIn("agopen-steering-reuse", unhex(row["state_meaning_hex"]))
            self.assertIn("NOT_A_TIMEOUT", row["state_transition_basis"].upper())
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["window_observations"], "3")
        self.assertEqual((report["saved_rows"], report["reported_value_changes"]), (3, 2))
        self.assertEqual(report["physical_cause_records"], "NOT_DERIVED")

    @unittest.skipUnless(READER, "CI supplies shared reader")
    def test_those_reported_changes_reach_common_observations(self):
        steering, _ = self.steering_observations(
            [(-1234, 1234), (-1200, NO_IMU_HEADING), (-1100, 1300)])
        output, _ = convert_transitions(steering.encode(), **DECLARATION)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            result = subprocess.run([READER, str(TRANSITIONS_PROFILE), str(path),
                                     "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual(common["platform_domain"], "Unknown")
        kinds = [observation["fields"]["event_kind"] for observation in common["observations"]]
        self.assertEqual(kinds.count("STATE_TRANSITION"), 3)  # the first value, then two changes
        self.assertEqual(kinds.count("OBSERVATION_WINDOW"), 1)
        units = common["profile_units"]
        self.assertIn("not_a_physical_cause", units["state_transition_basis"])
        self.assertIn("says_nothing_about_why", units["window_rate_ratio"])

    def test_the_bound_producers_keep_demand_feedback_and_fault_apart(self):
        """The four distinctions this unit exists to protect, read from the adopted cases."""
        steering = declared_units(STEERING_CASE / "profile.toml")
        dronecan = declared_units(DRONECAN_CASE / "profile.toml")

        self.assertIn("not_a_commanded_or_target_angle", steering["agopen_steer_angle_deg"])
        self.assertIn("not_true_north_or_a_verified_course", steering["agopen_heading_deg"])

        for absent in ("no_mechanical_rotation_direction", "motion_axis", "gear_ratio"):
            self.assertIn(absent, dronecan["reported_rpm"])
        for stated in ("resets_when_the_motor_restarts", "never_a_physical_fault"):
            self.assertIn(stated, dronecan["esc_reported_error_count"])
        self.assertIn("operator_declared", dronecan["actuator_unit_basis"])
        for column in ("actuator_position_m", "actuator_position_rad"):
            self.assertIn("no_zero_reference_axis_or_calibration", dronecan[column])
        self.assertIn("reported_load_as_a_fraction", dronecan["actuator_demand_fraction"])
        self.assertIn("0_is_unloaded", dronecan["actuator_demand_fraction"])
        self.assertIn("reported_instant_demand", dronecan["esc_demand_fraction"])
        self.assertNotEqual(dronecan["esc_demand_fraction"], dronecan["reported_rpm"])

    def test_no_commanded_or_target_column_exists_in_either_bound_schema(self):
        """The shared gap, proved rather than asserted: the demand side is not decoded.

        `convert_dronecan_capture.py` selects Status messages only (ESC 1034, RPM 1045, Actuator
        1011); the command messages of the same DSDL set are not read. The AgOpenGPS converter
        says in its own docstring that the packet it reads carries no commanded or target angle.
        """
        steering, _ = self.steering_observations([(-1234, 1234)])
        columns = next(csv.reader(io.StringIO(steering)))
        for column in columns:
            self.assertNotIn("command", column)
            self.assertNotIn("target", column)
            self.assertNotIn("setpoint", column)

        from scripts.convert_dronecan_capture import FIELDS, TYPES
        for message_id, name in ((1034, "ESCStatus"), (1045, "RPM"), (1011, "ActuatorStatus")):
            self.assertEqual(TYPES[message_id], name)
        for name in TYPES.values():
            self.assertNotIn("Command", name)
        for column in FIELDS:
            self.assertNotIn("command", column)
            self.assertNotIn("setpoint", column)
        self.assertIn("actuator_demand_fraction", FIELDS)


if __name__ == "__main__":
    unittest.main()
