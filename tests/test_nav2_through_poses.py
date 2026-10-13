"""Fixed server-shaped saved feedback only; no navigator, goal or device execution."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from rosbags.rosbag2.writer import StoragePlugin
from scripts import convert_rosbag_observations as c
from tests import test_nav2_progress as old
from tests.test_rosbag_observations import bag

ROOT = Path(__file__).resolve().parents[1]


def definition():
    result = c.NAV2_DEFINITIONS[c.NAV2_THROUGH]
    result += "\n" + "=" * 80 + "\nMSG: " + c.NAV2_THROUGH_CHILD + "\n" + c.NAV2_DEFINITIONS[c.NAV2_THROUGH_CHILD]
    for name in ("geometry_msgs/msg/PoseStamped", "builtin_interfaces/msg/Duration",
                 "unique_identifier_msgs/msg/UUID"):
        result += "\n" + "=" * 80 + "\nMSG: " + name + "\n" + c.STORE.generate_msgdef(name, ros_version=2)[0]
    return result


def feedback(count, **options):
    original = old.feedback(old.GOAL_A, **options)
    f = original.feedback
    return c.STORE.types[c.NAV2_THROUGH](original.goal_id, c.STORE.types[c.NAV2_THROUGH_CHILD](
        f.current_pose, f.navigation_time, f.estimated_time_remaining, f.number_of_recoveries,
        f.distance_remaining, count))


class ThroughPoses(unittest.TestCase):
    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.csv"
            path.write_text(text)
            result = subprocess.run([reader, str(old.PROFILE), str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def convert(self, messages, **options):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bag"
            bag(path, messages=messages, msgdefs={c.NAV2_THROUGH: definition()}, **options)
            return c.convert(path, nav2_through_poses=True)

    def test_two_containers_positive_progress_and_status_reach_common(self):
        self.assertEqual(c.STORE.hash_rihs01(c.NAV2_THROUGH), c.NAV2_THROUGH_HASH)
        for storage, count, distance in ((StoragePlugin.SQLITE3, 3, 12.5), (StoragePlugin.MCAP, 1, 2.25)):
            with self.subTest(storage=storage):
                messages = [feedback(count, distance=distance, elapsed=(-1, 500000000), frame="reported-map"),
                            old.status((old.GOAL_A, 2, (7, 123)))]
                text, report = self.convert(messages, plugin=storage)
                rows = list(csv.DictReader(io.StringIO(text)))
                self.assertEqual(report["source_messages"], 3)
                self.assertEqual(report["decoded_messages"], 2)
                self.assertEqual(len(report["unsupported_records"]), 1)
                progress = rows[0]
                self.assertEqual(progress["action_uuid_hex"], rows[1]["action_uuid_hex"])
                self.assertEqual(rows[1]["action_status_reported"], "EXECUTING_REPORTED")
                self.assertEqual(progress["nav2_number_of_poses_remaining"], str(count))
                self.assertEqual(progress["action_status_reported"], "")
                retained = json.loads(bytes.fromhex(progress["source_record_hex"][4:]))
                self.assertEqual(retained["decoded"]["feedback"]["number_of_poses_remaining"], count)
                common = self.common(text)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["platform_domain"], "Unknown")
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["nav2_number_of_poses_remaining"], count)
                self.assertEqual(fields["nav2_distance_remaining_m"], distance)
                self.assertEqual(fields["nav2_navigation_time_ns"], -500000000)
                self.assertEqual(fields["pose_frame_hex"], "hex:" + b"reported-map".hex())
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                self.assertIn("not_verified_physical_count", common["profile_units"]["nav2_number_of_poses_remaining"])

    def test_zero_default_and_negative_representation_withhold_derived_progress(self):
        messages = [feedback(0, frame="", elapsed=(0, 0), remaining=(0, 0), distance=0, recoveries=0),
                    feedback(-32768, distance=9)]
        text, _ = self.convert(messages)
        rows = list(csv.DictReader(io.StringIO(text)))
        common = self.common(text)
        for index, (row, count) in enumerate(zip(rows, (0, -32768))):
            self.assertEqual(row["nav2_number_of_poses_remaining"], str(count))
            self.assertEqual(row["nav2_pose_disposition"], "WITHHELD_NONPOSITIVE_REMAINING_COUNT")
            for key in ("nav2_navigation_time_ns", "nav2_estimated_time_remaining_ns", "nav2_distance_remaining_m",
                        "nav2_number_of_recoveries", "pose_position_m_x", "action_status_reported"):
                self.assertEqual(row[key], "")
                self.assertIn(common["observations"][index]["fields"].get(key), (None, ""))
            retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
            self.assertEqual(retained["decoded"]["feedback"]["number_of_poses_remaining"], count)
        self.assertEqual(rows[0]["nav2_poses_remaining_disposition"], "ZERO_OR_EMPTY_GOALS_DEFAULT_FEEDBACK")
        self.assertIn("NEGATIVE_REPORTED", rows[1]["nav2_poses_remaining_disposition"])

    def test_default_bytes_and_important_refusals_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            bag(legacy, messages=[old.feedback(old.GOAL_A)], msgdefs={c.NAV2_FEEDBACK: old.feedback_definition()})
            output, _ = c.convert(legacy)
            self.assertEqual(hashlib.sha256(output.encode()).hexdigest(),
                             "ea0b59ce3c0bbb09a3b86b774c5f7d6a594b9afa288cbaa1d5ce53c4472a079b")
            source = root / "new"
            bag(source, messages=[feedback(2), old.feedback(old.GOAL_A)],
                msgdefs={c.NAV2_THROUGH: definition(), c.NAV2_FEEDBACK: old.feedback_definition()})
            output, report = c.convert(source)
            self.assertNotIn("nav2_number_of_poses_remaining", output)
            self.assertEqual(report["decoded_messages"], 1)
            self.assertEqual(len(report["unsupported_records"]), 2)
            for config in ({"nav2_through_poses": 1}, {"nav2_through_poses": True, "ros1": True},
                           {"nav2_through_poses": True, "list_compact": "actions"}):
                with self.assertRaises(ValueError):
                    c.convert(source, **config)
            cli = [sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"), str(source),
                   str(root / "output"), "--nav2-through-poses"]
            result = subprocess.run(cli, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nav2_number_of_poses_remaining", (root / "output/observations.csv").read_text())
            result = subprocess.run(cli + ["--metadata-only"], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        with self.assertRaisesRegex(AssertionError, "Failed to parse"):
            self.convert([feedback(2)], hash_override="RIHS01_" + "0" * 64)
        with self.assertRaises(Exception):
            self.convert([feedback(2)], corrupt=True)
        original = c.plain(feedback(2))
        for key, value in (("number_of_poses_remaining", True), ("number_of_poses_remaining", 32768),
                           ("distance_remaining", -1), ("estimated_time_remaining", {"sec": 0, "nanosec": 10**9})):
            record = copy.deepcopy(original)
            record["feedback"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                c.nav2_through_quantities(record)
        del original["feedback"]["number_of_poses_remaining"]
        with self.assertRaises(KeyError):
            c.nav2_through_quantities(original)
