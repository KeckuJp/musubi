"""Authored `.plan` files in the shape the pinned format states.

No mission was flown, no vehicle was contacted and no plan here was ever sent anywhere. These are a
code and format proof: a saved configuration file reaches common Observations as a configuration
record, with the things it cannot say left unsaid.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_mission_plan import (MAV_CMD_DECLARED_ENTRIES, MAV_CMD_NAMED, convert)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-mission-plan/profile.toml"
CAPTURE_US = 1_700_000_000_000_000


def item(command=16, frame=3, do_jump_id=1, auto=True, params=None, **overrides):
    """One SimpleItem in the format's own field names."""
    record = {"type": "SimpleItem", "command": command, "frame": frame, "doJumpId": do_jump_id,
              "autoContinue": auto,
              "params": [0, 0, 0, None, 47.3977, 8.5456, 20] if params is None else params}
    for key, value in overrides.items():
        if value is None:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def without(record, key):
    """The same item with one declared key missing entirely."""
    record = dict(record)
    record.pop(key)
    return record


def plan(items=None, mission=None, **overrides):
    document = {
        "fileType": "Plan", "version": 1, "groundStation": "QGroundControl",
        "mission": {"version": 2, "firmwareType": 3, "vehicleType": 2, "cruiseSpeed": 15,
                    "hoverSpeed": 5, "plannedHomePosition": [47.397742, 8.545594, 488.0],
                    "items": [item(command=22, do_jump_id=1), item(command=16, do_jump_id=2)]
                    if items is None else items},
    }
    if mission:
        for key, value in mission.items():
            if value is None:
                document["mission"].pop(key, None)
            else:
                document["mission"][key] = value
    for key, value in overrides.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    return document


def rows_of(document, capture_time_us=CAPTURE_US):
    output, report = convert(json.dumps(document).encode(), capture_time_us)
    return list(csv.DictReader(io.StringIO(output))), report, output


class SavedMissionPlanTests(unittest.TestCase):
    def test_a_saved_plan_arrives_as_a_declaration_and_its_items(self):
        rows, report, _ = rows_of(plan())
        self.assertEqual([row["plan_record_kind"] for row in rows],
                         ["PLAN_DECLARATION", "MISSION_ITEM", "MISSION_ITEM"])
        header = rows[0]
        self.assertEqual((header["file_type_declared"], header["file_version_declared"],
                          header["mission_version_declared"]), ("Plan", "1", "2"))
        self.assertEqual((header["firmware_type_name"], header["vehicle_type_name"]),
                         ("MAV_AUTOPILOT_ARDUPILOTMEGA", "MAV_TYPE_QUADROTOR"))
        self.assertEqual((header["planned_home_latitude_deg"], header["planned_home_longitude_deg"],
                          header["planned_home_amsl_altitude_m"]),
                         ("47.397742", "8.545594", "488.0"))
        self.assertEqual((header["plan_cruise_speed_m_s"], header["plan_hover_speed_m_s"]),
                         ("15", "5"))
        self.assertEqual((header["simple_item_count"], header["complex_item_count"]), ("2", "0"))
        self.assertEqual((header["geofence_section_present"], header["rally_points_section_present"]),
                         ("ABSENT", "ABSENT"))
        self.assertEqual([row["item_command_name"] for row in rows[1:]],
                         ["MAV_CMD_NAV_TAKEOFF", "MAV_CMD_NAV_WAYPOINT"])
        self.assertEqual([row["item_frame_name"] for row in rows[1:]],
                         ["MAV_FRAME_GLOBAL_RELATIVE_ALT"] * 2)
        self.assertEqual([row["item_index"] for row in rows[1:]], ["0", "1"])
        self.assertEqual([row["item_do_jump_id"] for row in rows[1:]], ["1", "2"])
        for row in rows:
            self.assertIn("OBSERVED_RECORD", row["record_basis"])
            self.assertIn("never_a_transmission", row["record_basis"])
            self.assertIn("task_configured_is_not_task_accepted", row["record_basis"])
        self.assertIn("no transmission", report["not_produced"])
        self.assertIn("no execution claim", report["not_produced"])

    def test_the_seven_parameters_are_positional_and_a_null_is_not_a_zero(self):
        rows, _, _ = rows_of(plan(items=[item(params=[1, 2, 3, None, 5, 6, 7])]))
        row = rows[1]
        self.assertEqual([row["item_param_%d" % position] for position in range(1, 8)],
                         ["1", "2", "3", "", "5", "6", "7"])
        self.assertIn("depend_on_the_particular_command", row["item_param_basis"])
        for broken in ([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6, 7, 8], "1,2,3,4,5,6,7"):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(plan(items=[item(params=broken)]))

    def test_an_unnamed_code_is_retained_beside_its_enum_space(self):
        rows, report, _ = rows_of(plan(items=[item(command=31_000, frame=99)]))
        row = rows[1]
        self.assertEqual((row["item_command_code"], row["item_command_name"]),
                         ("31000", "UNKNOWN_CODE_RETAINED"))
        self.assertEqual((row["item_frame_code"], row["item_frame_name"]),
                         ("99", "UNKNOWN_CODE_RETAINED"))
        self.assertIn("MAV_CMD", row["enum_space_basis"])
        self.assertEqual(report["unnamed_command_codes"], [31_000])
        self.assertEqual(report["named_command_codes"], len(MAV_CMD_NAMED))
        self.assertEqual(report["declared_command_entries_at_the_pin"], MAV_CMD_DECLARED_ENTRIES)
        self.assertLess(report["named_command_codes"], report["declared_command_entries_at_the_pin"])
        header = rows_of(plan(mission={"firmwareType": 200, "vehicleType": 201}))[0][0]
        self.assertEqual((header["firmware_type_name"], header["vehicle_type_name"]),
                         ("UNKNOWN_CODE_RETAINED", "UNKNOWN_CODE_RETAINED"))
        self.assertEqual((header["firmware_type_code"], header["vehicle_type_code"]), ("200", "201"))

    def test_an_item_index_is_the_saved_position_and_a_gap_stays_a_gap(self):
        """[SimpleItem, ComplexItem, SimpleItem] is 0 and 2 -- never renumbered to 0 and 1."""
        rows, report, _ = rows_of(plan(items=[
            item(command=22, do_jump_id=7),
            {"type": "ComplexItem", "complexItemType": "survey"},
            item(command=16, do_jump_id=9)]))
        self.assertEqual([row["plan_record_kind"] for row in rows],
                         ["PLAN_DECLARATION", "MISSION_ITEM", "MISSION_ITEM"])
        self.assertEqual([row["item_index"] for row in rows[1:]], ["0", "2"])
        self.assertEqual([row["item_do_jump_id"] for row in rows[1:]], ["7", "9"])
        self.assertEqual((rows[0]["simple_item_count"], rows[0]["complex_item_count"]), ("2", "1"))
        self.assertIn("leaves a gap", report["item_index_basis"])
        trailing = rows_of(plan(items=[item(), {"type": "ComplexItem"}]))[0]
        self.assertEqual([row["item_index"] for row in trailing[1:]], ["0"])
        leading = rows_of(plan(items=[{"type": "ComplexItem"}, item()]))[0]
        self.assertEqual([row["item_index"] for row in leading[1:]], ["1"])

    def test_an_unrecognised_type_is_not_counted_as_a_complex_item(self):
        rows, report, _ = rows_of(plan(items=[
            item(command=22), {"type": "ComplexItem", "complexItemType": "survey"},
            {"type": "FutureItemKind"}, {"type": "FutureItemKind"}]))
        self.assertEqual((rows[0]["simple_item_count"], rows[0]["complex_item_count"],
                          rows[0]["unrecognised_item_count"]), ("1", "1", "2"))
        complex_entries = report["complex_items_counted_not_decoded"]
        unrecognised = report["unrecognised_item_types_retained"]
        self.assertEqual([entry["count"] for entry in complex_entries], [1])
        self.assertEqual([entry["declared_type_hex"] for entry in complex_entries],
                         ["hex:" + "ComplexItem".encode().hex()])
        self.assertEqual([entry["declared_type_hex"] for entry in unrecognised],
                         ["hex:" + "FutureItemKind".encode().hex()])
        self.assertEqual([entry["count"] for entry in unrecognised], [2])
        self.assertIn("NOT_a_ComplexItem", rows[0]["item_kind_basis"])

    def test_a_boolean_is_not_the_pinned_version_number(self):
        for broken in (True, False, 1.0, "1", None):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(plan(version=broken) if broken is not None
                        else {"fileType": "Plan", "mission": {"version": 2, "items": []}})
        for broken in (True, 2.0, "2"):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(plan(mission={"version": broken}))
        self.assertEqual(rows_of(plan(items=[]))[0][0]["file_version_declared"], "1")

    def test_a_complex_item_is_counted_and_never_flattened(self):
        rows, report, _ = rows_of(plan(items=[
            item(command=22), {"type": "ComplexItem", "complexItemType": "survey"},
            {"type": "ComplexItem", "complexItemType": "survey"}, {"type": "ComplexItem"}]))
        self.assertEqual([row["plan_record_kind"] for row in rows],
                         ["PLAN_DECLARATION", "MISSION_ITEM"])
        self.assertEqual((rows[0]["simple_item_count"], rows[0]["complex_item_count"],
                          rows[0]["unrecognised_item_count"]), ("1", "3", "0"))
        self.assertIn("flattening_or_relabelling_either_would_state_something_the_file_does_not",
                      rows[0]["item_kind_basis"])
        counted = report["complex_items_counted_not_decoded"]
        self.assertEqual(sum(entry["count"] for entry in counted), 3)
        self.assertIn("hex:" + "survey".encode().hex(),
                      [entry["declared_complex_item_type_hex"] for entry in counted])
        header = rows_of(plan(geoFence={"version": 2, "polygons": []},
                              rallyPoints={"version": 2, "points": []}))[0][0]
        self.assertEqual((header["geofence_section_present"], header["rally_points_section_present"]),
                         ("PRESENT", "PRESENT"))

    def test_a_plan_with_no_items_still_declares_itself(self):
        rows, report, _ = rows_of(plan(items=[]))
        self.assertEqual([row["plan_record_kind"] for row in rows], ["PLAN_DECLARATION"])
        self.assertEqual((rows[0]["simple_item_count"], rows[0]["complex_item_count"]), ("0", "0"))
        self.assertEqual(report["simple_items_read"], 0)
        bare, _, _ = rows_of(plan(mission={"cruiseSpeed": None, "hoverSpeed": None,
                                           "plannedHomePosition": None}, groundStation=None))
        self.assertEqual((bare[0]["plan_cruise_speed_m_s"], bare[0]["plan_hover_speed_m_s"]),
                         ("", ""))
        self.assertEqual((bare[0]["planned_home_latitude_deg"], bare[0]["ground_station_hex"]),
                         ("", ""))

    def test_an_unqualified_or_unusable_plan_is_refused(self):
        for broken in (
                {"fileType": "NotAPlan"},
                {"fileType": None},
                {"version": 2},
                {"version": "1"},
        ):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(plan(**broken))
        for missing in ({"fileType": "Plan", "version": 1},
                        {"fileType": "Plan", "version": 1, "mission": []}):
            with self.assertRaises(ValueError, msg=missing):
                rows_of(missing)
        for broken in ({"version": 1}, {"version": 3}, {"items": None}, {"items": {}}):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(plan(mission=broken))
        for broken in (without(item(), "command"), without(item(), "frame"),
                       without(item(), "params"), without(item(), "doJumpId"),
                       without(item(), "autoContinue"), without(item(), "type"),
                       item(**{"autoContinue": 1}), item(command=-1), item(frame=70_000),
                       item(params=[1, 2, 3, 4, 5, 6, "seven"]),
                       item(params=[1, 2, 3, 4, 5, 6, 1e999]),
                       item(params=[1, 2, 3, 4, 5, 6, True])):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(plan(items=[broken]))
        for home in ([91.0, 8.5, 488.0], [47.4, 181.0, 488.0], [47.4, 8.5], [47.4, 8.5, 488.0, 0]):
            with self.assertRaises(ValueError, msg=home):
                rows_of(plan(mission={"plannedHomePosition": home}))
        with self.assertRaises(ValueError):
            convert(b'{"fileType":"Plan","version":1,"version":1,'
                    b'"mission":{"version":2,"items":[]}}', CAPTURE_US)
        with self.assertRaises(ValueError):
            convert(json.dumps(plan(mission={"cruiseSpeed": 1})).encode().replace(
                b'"cruiseSpeed": 1', b'"cruiseSpeed": 1e-400'), CAPTURE_US)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_the_declared_meanings_reach_common_observations(self):
        _, _, output = rows_of(plan(items=[item(command=22, do_jump_id=1),
                                           item(command=183, do_jump_id=2,
                                                params=[1, 1500, 0, 0, 0, 0, 0])]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                     str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (3, "Unknown"))
        fields = [observation["fields"] for observation in common["observations"]]
        self.assertEqual([row["plan_record_kind"] for row in fields],
                         ["PLAN_DECLARATION", "MISSION_ITEM", "MISSION_ITEM"])
        self.assertEqual(fields[2]["item_command_name"], "MAV_CMD_DO_SET_SERVO")
        self.assertEqual(fields[2]["item_param_2"], 1500)
        for row in fields:
            self.assertIn("OBSERVED_RECORD", row["record_basis"])
        for observation in common["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"])
        units = common["profile_units"]
        for name in ("plan_record_kind", "item_command_code", "item_command_name", "item_param_5",
                     "item_param_basis", "planned_home_latitude_deg", "plan_cruise_speed_m_s",
                     "record_basis", "complex_item_count", "geofence_section_present"):
            self.assertTrue(units.get(name), name)
        self.assertIn("reading_it_is_never_sending_it", units["item_command_code"])
        self.assertIn("NOT_declared_here_to_be_a_latitude", units["item_param_5"])
        self.assertIn("never_an_observed_fix", units["planned_home_latitude_deg"])
        self.assertIn("CREATED_FOR", units["vehicle_type_code"])


if __name__ == "__main__":
    unittest.main()
