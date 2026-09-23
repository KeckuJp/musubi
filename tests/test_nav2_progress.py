"""Saved Nav2 navigation progress and goal status, authored inputs only.

Every message here is authored in this file against the pinned schemas (navigation2 1.1.20
a097086719c88f781aa59788eca29ac6ca5e56db; action_msgs read on the humble branch head). Nothing is
executed: no goal, no cancellation, no action client, no live navigator, no device.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np

from scripts.convert_rosbag_observations import (ACTION_STATUS, NAV2_DEFINITIONS, NAV2_FEEDBACK, STORE,
                                                action_status_quantities, convert, plain)
from tests.test_rosbag_observations import bag, samples

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/nav2-passive-progress/profile.toml"
GOAL_A = bytes(range(16))
GOAL_B = bytes(range(16, 32))


def feedback_definition():
    """The fixed dependency expansion the adopted feedback path already uses."""
    definition = NAV2_DEFINITIONS[NAV2_FEEDBACK]
    child = "nav2_msgs/action/NavigateToPose_Feedback"
    definition += "\n" + "=" * 80 + "\nMSG: " + child + "\n" + NAV2_DEFINITIONS[child]
    for name in ("geometry_msgs/msg/PoseStamped", "builtin_interfaces/msg/Duration",
                 "unique_identifier_msgs/msg/UUID"):
        definition += "\n" + "=" * 80 + "\nMSG: " + name + "\n" + STORE.generate_msgdef(name, ros_version=2)[0]
    return definition


def feedback(goal, *, x=1.0, distance=4.0, elapsed=(3, 123), remaining=(5, 0), recoveries=2, frame=None):
    types = STORE.types
    duration = types["builtin_interfaces/msg/Duration"]
    header = samples()[0].header
    if frame is not None:
        header = types["std_msgs/msg/Header"](header.stamp, frame)
    pose = types["geometry_msgs/msg/PoseStamped"](header,
        types["geometry_msgs/msg/Pose"](types["geometry_msgs/msg/Point"](x, 2.0, 0.0),
                                        types["geometry_msgs/msg/Quaternion"](0.0, 0.0, 0.0, 1.0)))
    return types[NAV2_FEEDBACK](
        types["unique_identifier_msgs/msg/UUID"](np.frombuffer(goal, dtype=np.uint8).copy()),
        types["nav2_msgs/action/NavigateToPose_Feedback"](pose, duration(*elapsed), duration(*remaining),
                                                          recoveries, distance))


def status(*entries):
    """`entries` are (goal uuid bytes, status code, (sec, nanosec))."""
    types = STORE.types
    return types[ACTION_STATUS]([
        types["action_msgs/msg/GoalStatus"](
            types["action_msgs/msg/GoalInfo"](
                types["unique_identifier_msgs/msg/UUID"](np.frombuffer(goal, dtype=np.uint8).copy()),
                types["builtin_interfaces/msg/Time"](*stamp)), code)
        for goal, code, stamp in entries])


class Nav2ProgressTests(unittest.TestCase):
    def rows_of(self, messages):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "bag"
            bag(source, messages=messages, msgdefs={NAV2_FEEDBACK: feedback_definition()})
            output, report = convert(source)
        return list(csv.DictReader(io.StringIO(output))), report, output

    def observations_of(self, output):
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(output, encoding="utf-8")
            return json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows), "--allow-equal-time"],
                check=True, capture_output=True).stdout)


    def test_one_recording_carries_progress_and_status_joined_by_goal(self):
        rows, report, _ = self.rows_of([feedback(GOAL_A), status((GOAL_A, 2, (7, 500)))])
        self.assertEqual((report["decoded_messages"], len(rows)), (2, 2))
        self.assertEqual(report["clock"], "Unknown", "the capture clock stays Unknown")
        progress, identity = rows
        self.assertEqual(progress["action_uuid_hex"], "hex:" + GOAL_A.hex())
        self.assertEqual(identity["action_uuid_hex"], progress["action_uuid_hex"])
        self.assertEqual(progress["nav2_navigation_time_ns"], "3000000123")
        self.assertEqual(progress["nav2_estimated_time_remaining_ns"], "5000000000")
        self.assertEqual(progress["nav2_estimated_time_disposition"], "REPORTED_ESTIMATE")
        self.assertEqual(progress["nav2_distance_remaining_m"], "4.0")
        self.assertEqual(progress["nav2_distance_disposition"], "REPORTED_PATH_LENGTH")
        self.assertEqual(progress["nav2_number_of_recoveries"], "2")
        self.assertEqual(progress["nav2_pose_disposition"], "REPORTED_ESTIMATE")
        self.assertEqual(progress["pose_position_m_x"], "1.0")
        self.assertTrue(progress["pose_frame_hex"].startswith("hex:"))
        self.assertEqual((identity["action_status_code"], identity["action_status_reported"]),
                         ("2", "EXECUTING_REPORTED"))
        self.assertEqual(identity["action_entry_count"], "1")
        self.assertEqual(identity["action_entry_index"], "0")
        self.assertEqual(identity["action_accepted_stamp_us"], "7000000")
        self.assertEqual(identity["action_accepted_stamp_ns"], "7000000500",
                         "the nanosecond remainder the microsecond column truncates")
        self.assertEqual(progress["record_time_us"], identity["record_time_us"])
        self.assertNotEqual(identity["record_time_us"], identity["action_accepted_stamp_us"])

    def test_a_second_authored_layout_reuses_the_same_path(self):
        rows, report, _ = self.rows_of([
            status((GOAL_A, 4, (11, 0)), (GOAL_B, 9, (0, 0))),
            status(),
            feedback(GOAL_B, x=-2.0, distance=0.0, elapsed=(-1, 500000000), remaining=(0, 0),
                     recoveries=0, frame=""),
        ])
        self.assertEqual(report["decoded_messages"], 3)
        entries = [r for r in rows if r["action_status_reported"]]
        self.assertEqual([(r["action_entry_index"], r["action_status_code"], r["action_status_reported"])
                          for r in entries],
                         [("0", "4", "SUCCEEDED_REPORTED"),
                          ("1", "9", "UNKNOWN_9"),          # outside the pinned enumeration, never remapped
                          ("", "", "EMPTY_ARRAY")])         # an empty array stays visible
        self.assertEqual([r["action_entry_count"] for r in entries], ["2", "2", "0"])
        self.assertEqual(entries[0]["action_uuid_hex"], "hex:" + GOAL_A.hex())
        self.assertEqual(entries[1]["action_uuid_hex"], "hex:" + GOAL_B.hex())
        self.assertEqual(entries[1]["action_accepted_stamp_ns"], "0", "a zero stamp is reported as zero")
        progress = rows[-1]
        self.assertEqual(progress["nav2_navigation_time_ns"], "-500000000",
                         "a negative elapsed duration is a reported clock difference, retained")
        self.assertEqual(progress["nav2_estimated_time_disposition"], "ZERO_OR_NOT_ESTIMATED")
        self.assertEqual(progress["nav2_distance_disposition"], "ZERO_OR_UNAVAILABLE")
        self.assertEqual(progress["nav2_pose_disposition"], "MISSING_FRAME_NOT_NORMALIZED")
        self.assertEqual(progress["pose_position_m_x"], "", "no pose is invented without a frame")


    def test_malformed_status_entries_never_silently_succeed(self):
        good = plain(status((GOAL_A, 2, (7, 500))))
        for key, bad in (("status", "2"), ("status", 200)):
            record = plain(status((GOAL_A, 2, (7, 500))))
            record["status_list"][0][key] = bad
            with self.assertRaises(ValueError):
                action_status_quantities(record)
        for uuid in ([1], list(range(17)), "abc", [256] * 16):
            record = plain(status((GOAL_A, 2, (7, 500))))
            record["status_list"][0]["goal_info"]["goal_id"]["uuid"] = uuid
            with self.assertRaises(ValueError):
                action_status_quantities(record)
        for stamp in ({"sec": 7, "nanosec": 10 ** 9}, {"sec": -1, "nanosec": 0}, {"sec": 7, "nanosec": -1}):
            record = plain(status((GOAL_A, 2, (7, 500))))
            record["status_list"][0]["goal_info"]["stamp"] = stamp
            with self.assertRaises(ValueError):
                action_status_quantities(record)
        oversized = plain(status((GOAL_A, 2, (7, 500))))
        oversized["status_list"] = oversized["status_list"] * 4097
        with self.assertRaises(ValueError):
            action_status_quantities(oversized)
        self.assertEqual(action_status_quantities(good)[0]["action_accepted_stamp_ns"], 7000000500)

    def test_a_mismatched_status_type_hash_is_not_decoded_as_this_type(self):
        message = status((GOAL_A, 2, (7, 500)))
        with tempfile.TemporaryDirectory() as work:
            wrong = Path(work) / "wrong"
            bag(wrong, messages=[message], hash_override="RIHS01_" + "0" * 64)
            with self.assertRaises((ValueError, AssertionError)):
                convert(wrong)
            right = Path(work) / "right"
            bag(right, messages=[message])
            output, _ = convert(right)
            rows = [r for r in csv.DictReader(io.StringIO(output)) if r["action_status_reported"]]
            self.assertEqual([r["action_status_reported"] for r in rows], ["EXECUTING_REPORTED"])

    def test_the_existing_microsecond_column_and_names_are_unchanged(self):
        row = action_status_quantities(plain(status((GOAL_A, 5, (11, 999999)))))[0]
        self.assertEqual(row["action_accepted_stamp_us"], 11000999)
        self.assertEqual(row["action_accepted_stamp_ns"], 11000999999)
        self.assertEqual(row["action_status_reported"], "CANCELED_REPORTED")
        self.assertEqual(row["action_uuid_hex"], "hex:" + GOAL_A.hex())
        self.assertEqual({"UNKNOWN", "ACCEPTED", "EXECUTING", "CANCELING", "SUCCEEDED", "CANCELED",
                          "ABORTED"},
                         {action_status_quantities(plain(status((GOAL_A, code, (1, 0)))))[0]
                          ["action_status_reported"].removesuffix("_REPORTED") for code in range(7)},
                         "the pinned GoalStatus constants, in their source order")


    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the rows as common Observations")
    def test_an_epoch_sized_acceptance_stamp_stays_exact_in_compact_actions_mode(self):
        epoch = (1700000000, 123456789)
        exact = epoch[0] * 10 ** 9 + epoch[1]
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "bag"
            bag(source, messages=[status((GOAL_A, 2, epoch))])
            output, _ = convert(source, list_compact="actions")
            row = [r for r in csv.DictReader(io.StringIO(output)) if r["action_status_reported"]][0]
            self.assertEqual(row["action_accepted_stamp_ns"], str(exact))
            self.assertEqual(row["action_accepted_stamp_us"], str(epoch[0] * 10 ** 6 + epoch[1] // 1000))
            self.assertNotIn("status_list", row["source_record_hex"],
                             "compact selection strips the list, so the column is the only carrier")
            fields = self.observations_of(output)["observations"][0]["fields"]
        self.assertEqual(fields["action_accepted_stamp_ns"], exact)
        self.assertIsInstance(fields["action_accepted_stamp_ns"], int)
        self.assertEqual(fields["action_accepted_stamp_us"], epoch[0] * 10 ** 6 + epoch[1] // 1000)
        largest = (2 ** 31 - 1) * 10 ** 9 + 999999999
        self.assertLess(largest, 2 ** 63)
        self.assertEqual(action_status_quantities(
            plain(status((GOAL_A, 2, (2 ** 31 - 1, 999999999)))))[0]["action_accepted_stamp_ns"], largest)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the rows as common Observations")
    def test_progress_and_status_reach_common_observations(self):
        _, report, output = self.rows_of([feedback(GOAL_A), status((GOAL_A, 2, (7, 500)))])
        common = self.observations_of(output)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["platform_domain"], "Unknown")
        progress, identity = (o["fields"] for o in common["observations"])
        self.assertEqual([o["clock_basis"] for o in common["observations"]], ["Unknown"] * 2)
        self.assertEqual(progress["nav2_distance_remaining_m"], 4.0)
        self.assertEqual(progress["nav2_navigation_time_ns"], 3000000123.0)
        self.assertEqual(progress["pose_position_m_x"], 1.0)
        self.assertEqual(identity["action_status_reported"], "EXECUTING_REPORTED")
        self.assertEqual(identity["action_accepted_stamp_ns"], 7000000500.0)
        self.assertEqual(identity["action_uuid_hex"], progress["action_uuid_hex"],
                         "the goal join survives the reader")
        distance_unit = common["profile_units"]["nav2_distance_remaining_m"]
        self.assertIn("REP103_SI", distance_unit)
        self.assertIn("two_dimensional", distance_unit)
        self.assertIn("from_the_nearest_path_vertex", distance_unit)
        self.assertIn("not_a_straight_line", distance_unit)
        self.assertIn("ros_clock_since_goal_initialization",
                      common["profile_units"]["nav2_navigation_time_ns"])
        self.assertIn("divided_by_smoothed_planar_speed",
                      common["profile_units"]["nav2_estimated_time_remaining_ns"])
        self.assertIn("global_frame", common["profile_units"]["pose_frame_hex"])
        self.assertIn("structural_identity", common["profile_units"]["action_uuid_hex"])


if __name__ == "__main__":
    unittest.main()
