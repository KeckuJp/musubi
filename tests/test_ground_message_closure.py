"""Authored bags for the two ground residual schemas; no device, ROS graph or real record.

Every input is constructed here from the pinned definitions. The adopted Odometry,
BatteryState and DiagnosticArray paths are not re-exercised; only the two new types and
the settings they need are covered.
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
from rosbags.rosbag2 import Writer
from rosbags.rosbag2.writer import StoragePlugin
from rosbags.typesys.msg import normalize_msgtype

from scripts.convert_rosbag_observations import (
    AUTONOMY, CHARGING, DYNAMIC_JOINT, GROUND_DEFINITIONS, HASHES, INTERFACE_KINDS,
    OUTDOORNAV_DEFINITIONS, STORE, convert)

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/ground-message-closure"
SCRIPT = ROOT / "scripts/convert_rosbag_observations.py"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
NAN = float("nan")


def header(seconds=2, nanoseconds=123456789, frame="battery_link"):
    types = STORE.types
    return types["std_msgs/msg/Header"](types["builtin_interfaces/msg/Time"](seconds, nanoseconds), frame)


def charging_status(charging=True, current=1.5, first=1.5, second=NAN, charger_type=0, frame="battery_link"):
    """Single-battery layout as the pinned publisher writes it: battery 2 is NaN."""
    return STORE.types[CHARGING](header(frame=frame), charging, current, first, second, charger_type)


def dynamic_joint_state(names=("fl_wheel_joint", "fr_wheel_joint"),
                        interfaces=(("position", "velocity"), ("position",)),
                        values=((1.25, -0.5), (2.5,))):
    types = STORE.types
    entries = [types["control_msgs/msg/InterfaceValue"](list(keys), np.array(row, dtype=np.float64))
               for keys, row in zip(interfaces, values)]
    return types[DYNAMIC_JOINT](header(), list(names), entries)


def autonomy_status(state=1, paused=False, goal="poi_3", frame="map", seconds=2,
                    nanoseconds=123456789, start_seconds=1, start_nanoseconds=500000000):
    """One OutdoorNav autonomy status, built from the pinned definition only."""
    types = STORE.types
    return types[AUTONOMY](header(seconds, nanoseconds, frame),
                           types["builtin_interfaces/msg/Time"](start_seconds, start_nanoseconds),
                           paused, goal, state)


def bag(path, messages, topic="/selected", hash_override=None, definition_replace=None):
    with Writer(path, version=9, storage_plugin=StoragePlugin.SQLITE3) as writer:
        for index, message in enumerate(messages):
            typename = message.__msgtype__
            definition = STORE.generate_msgdef(typename, ros_version=2)[0]
            if definition_replace is not None:
                definition = definition.replace(*definition_replace)
            connection = writer.add_connection(
                f"{topic}/{index}", typename, typestore=STORE,
                rihs01=hash_override or STORE.hash_rihs01(typename), msgdef=definition)
            writer.write(connection, 7000123456, STORE.serialize_cdr(message, typename))


_SEQUENCE = iter(range(1, 1 << 20))


def rows(directory, messages, **options):
    target = Path(directory) / f"bag-{next(_SEQUENCE)}"
    bag(target, messages)
    output, report = convert(target, **options)
    return list(csv.DictReader(io.StringIO(output))), report


class GroundMessageClosureTests(unittest.TestCase):
    def test_pinned_definitions_hash_to_the_qualified_values(self):
        """A type name alone is never enough: the definition must produce the pinned hash."""
        for typename in (CHARGING, DYNAMIC_JOINT, "control_msgs/msg/InterfaceValue"):
            self.assertIn(typename, GROUND_DEFINITIONS)
        for typename in (CHARGING, DYNAMIC_JOINT):
            self.assertEqual(STORE.hash_rihs01(normalize_msgtype(typename)), HASHES[typename])

    def test_charging_status_reaches_common_output_with_its_reported_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            observed, report = rows(directory, [charging_status(),
                                                charging_status(charging=False, current=-0.25,
                                                                first=-0.25, charger_type=2)])
            self.assertEqual(report["decoded_messages"], 2)
            self.assertEqual(report["output_records"], 2)
            first, second = observed
            self.assertEqual(first["charging_reported_flag"], "true")
            self.assertIn("NOT_MEASURED_OR_SAFE", first["charging_flag_basis"])
            self.assertEqual(float(first["charging_current_total_a"]), 1.5)
            self.assertEqual(first["charging_current_total_status"], "REPORTED")
            self.assertEqual(first["charging_current_battery_2_status"], "UNAVAILABLE_SOURCE_NAN")
            self.assertEqual(first["charging_current_battery_2_a"], "")
            self.assertEqual(first["charging_charger_type_code"], "0")
            self.assertEqual(first["charging_charger_type_reported"], "UNKNOWN")
            self.assertIn("MESSAGE_COMMENT_ONLY", first["charging_current_unit_basis"])
            self.assertEqual(first["charging_source_stamp_us"], "2123456")
            self.assertEqual(bytes.fromhex(first["charging_frame_hex"][4:]).decode(), "battery_link")
            self.assertEqual(float(second["charging_current_total_a"]), -0.25)
            self.assertEqual(second["charging_reported_flag"], "false")
            self.assertEqual(second["charging_charger_type_reported"], "WIRELESS")

    def test_charging_status_unknown_code_and_nonfinite_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            observed, _ = rows(directory, [charging_status(charger_type=7)])
            self.assertEqual(observed[0]["charging_charger_type_code"], "7")
            self.assertEqual(observed[0]["charging_charger_type_reported"], "UNKNOWN_CODE_RETAINED")
            observed, _ = rows(directory, [charging_status(current=NAN, first=NAN)],
                               )
            for name in ("total", "battery_1", "battery_2"):
                self.assertEqual(observed[0]["charging_current_" + name + "_status"],
                                 "UNAVAILABLE_SOURCE_NAN")
                self.assertEqual(observed[0]["charging_current_" + name + "_a"], "")
            for value in (float("inf"), float("-inf")):
                with self.assertRaises(ValueError):
                    rows(directory, [charging_status(current=value)])

    def test_dynamic_joint_state_requires_a_named_interface_unit(self):
        kinds = {"fl_wheel_joint.position": "angular_position",
                 "fl_wheel_joint.velocity": "angular_velocity",
                 "fr_wheel_joint.position": "linear_position"}
        with tempfile.TemporaryDirectory() as directory:
            observed, report = rows(directory, [dynamic_joint_state()], interface_kinds=kinds)
            self.assertEqual(report["output_records"], 3)
            self.assertEqual([row["dynamic_joint_count"] for row in observed], ["2", "2", "2"])
            self.assertEqual([row["dynamic_interface_count"] for row in observed], ["2", "2", "1"])
            self.assertEqual([bytes.fromhex(row["dynamic_interface_name_hex"][4:]).decode()
                              for row in observed], ["position", "velocity", "position"])
            self.assertEqual(float(observed[0]["dynamic_interface_value_rad"]), 1.25)
            self.assertEqual(float(observed[1]["dynamic_interface_value_rad_s"]), -0.5)
            self.assertEqual(float(observed[2]["dynamic_interface_value_m"]), 2.5)
            self.assertEqual(observed[2]["dynamic_interface_value_rad"], "")
            self.assertTrue(all(row["dynamic_interface_status"] == "REPORTED" for row in observed))
            self.assertEqual(observed[0]["dynamic_joint_source_stamp_us"], "2123456")

    def test_unmapped_interface_keeps_identity_and_accounting_without_a_value(self):
        with tempfile.TemporaryDirectory() as directory:
            observed, report = rows(directory, [dynamic_joint_state()],
                                    interface_kinds={"fl_wheel_joint.position": "angular_position"})
            self.assertEqual(report["output_records"], 3)
            self.assertEqual(observed[0]["dynamic_interface_status"], "REPORTED")
            for row in observed[1:]:
                self.assertEqual(row["dynamic_interface_status"], "UNMAPPED_NO_CALLER_UNIT")
                self.assertEqual(row["dynamic_interface_kind_configured"], "")
                self.assertTrue(all(row["dynamic_interface_value_" + unit] == ""
                                    for unit in set(INTERFACE_KINDS.values())))
                self.assertNotEqual(row["dynamic_interface_name_hex"], "")
                self.assertEqual(row["dynamic_joint_count"], "2")

    def test_dynamic_joint_state_structural_and_value_refusals(self):
        types = STORE.types
        kinds = {"fl_wheel_joint.position": "angular_position"}
        with tempfile.TemporaryDirectory() as directory:
            observed, _ = rows(directory, [dynamic_joint_state(names=(), interfaces=(), values=())])
            self.assertEqual(observed[0]["dynamic_joint_count"], "0")
            self.assertEqual(observed[0]["dynamic_interface_status"], "")
            mismatched = dynamic_joint_state(names=("a", "b"), interfaces=(("position",),),
                                             values=((1.,),))
            with self.assertRaises(ValueError):
                rows(directory, [mismatched], interface_kinds={"a.position": "angular_position"})
            ragged = types[DYNAMIC_JOINT](header(), ["a"], [
                types["control_msgs/msg/InterfaceValue"](["position", "velocity"],
                                                         np.array([1.], dtype=np.float64))])
            with self.assertRaises(ValueError):
                rows(directory, [ragged], interface_kinds={"a.position": "angular_position"})
            for names, interfaces, values in ((("a", "a"), (("position",), ("position",)), ((1.,), (2.,))),
                                              (("",), (("position",),), ((1.,),)),
                                              (("a",), (("position", "position"),), ((1., 2.),)),
                                              (("a",), (("",),), ((1.,),))):
                with self.assertRaises(ValueError):
                    rows(directory, [dynamic_joint_state(names, interfaces, values)],
                         interface_kinds={"a.position": "angular_position"})
            with self.assertRaises(ValueError):
                rows(directory, [dynamic_joint_state()],
                     interface_kinds=dict(kinds, **{"absent_joint.position": "angular_position"}))
            for bad in ({"fl_wheel_joint.position": "angular"}, {"fl_wheel_joint": "angular_position"}):
                with self.assertRaises(ValueError):
                    rows(directory, [dynamic_joint_state()], interface_kinds=bad)
            infinite = dynamic_joint_state(names=("a",), interfaces=(("position",),),
                                           values=((float("inf"),),))
            with self.assertRaises(ValueError):
                rows(directory, [infinite], interface_kinds={"a.position": "angular_position"})
            missing = dynamic_joint_state(names=("a",), interfaces=(("position",),), values=((NAN,),))
            with self.assertRaises(ValueError):
                rows(directory, [missing], interface_kinds={"a.position": "angular_position"})
            observed, _ = rows(directory, [missing], interface_kinds={"a.position": "angular_position"},
                               allow_missing_joint_values=True)
            self.assertEqual(observed[0]["dynamic_interface_status"], "UNAVAILABLE_SOURCE_NAN")
            self.assertEqual(observed[0]["dynamic_interface_value_rad"], "")

    def test_a_joint_reporting_no_interface_keeps_its_identity(self):
        """An empty interface list must not delete the joint from the accounting."""
        mixed = dynamic_joint_state(names=("fl_wheel_joint", "quiet_joint"),
                                    interfaces=(("position",), ()), values=((1.25,), ()))
        empty = dynamic_joint_state(names=("a_joint", "b_joint"), interfaces=((), ()),
                                    values=((), ()))
        with tempfile.TemporaryDirectory() as directory:
            observed, report = rows(directory, [mixed],
                                    interface_kinds={"fl_wheel_joint.position": "angular_position"})
            self.assertEqual(report["output_records"], 2)
            self.assertEqual(float(observed[0]["dynamic_interface_value_rad"]), 1.25)
            quiet = observed[1]
            self.assertEqual(bytes.fromhex(quiet["dynamic_joint_name_hex"][4:]).decode(), "quiet_joint")
            self.assertEqual(quiet["dynamic_joint_index"], "1")
            self.assertEqual(quiet["dynamic_joint_count"], "2")
            self.assertEqual(quiet["dynamic_interface_count"], "0")
            self.assertEqual(quiet["dynamic_interface_status"], "NOT_PROVIDED")
            self.assertEqual(quiet["dynamic_interface_index"], "")
            self.assertEqual(quiet["dynamic_interface_name_hex"], "")
            self.assertEqual(quiet["dynamic_interface_kind_configured"], "")
            self.assertTrue(all(quiet["dynamic_interface_value_" + unit] == ""
                                for unit in set(INTERFACE_KINDS.values())))

            observed, report = rows(directory, [empty])
            self.assertEqual(report["decoded_messages"], 1)
            self.assertEqual(report["output_records"], 2)
            self.assertEqual([row["dynamic_joint_index"] for row in observed], ["0", "1"])
            self.assertEqual([row["dynamic_interface_count"] for row in observed], ["0", "0"])
            self.assertEqual([bytes.fromhex(row["dynamic_joint_name_hex"][4:]).decode()
                              for row in observed], ["a_joint", "b_joint"])
            observed, _ = rows(directory, [dynamic_joint_state(names=(), interfaces=(), values=())])
            self.assertEqual(observed[0]["dynamic_joint_count"], "0")
            self.assertEqual(observed[0]["dynamic_joint_name_hex"], "")

            if not Path(READER).exists():
                print("empty-interface common-reader integration NOT_RUN: reader not built")
                return
            out = Path(directory) / "empty-out"
            target = Path(directory) / "empty-bag"
            bag(target, [mixed])
            subprocess.run([os.sys.executable, str(SCRIPT), str(target), str(out),
                            "--interface-kind", "fl_wheel_joint.position=angular_position"],
                           check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = common["observations"][1]["fields"]
            self.assertEqual(fields["dynamic_interface_count"], 0)
            self.assertEqual(fields["dynamic_interface_status"], "NOT_PROVIDED")
            self.assertIsNone(fields["dynamic_interface_value_rad"])

    def test_malformed_or_unconsumed_configuration_refuses_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            for malformed in ([], "fl_wheel_joint.position=angular_position", 7,
                              {"fl_wheel_joint.position": "angular"}, {7: "angular_position"},
                              {"fl_wheel_joint": "angular_position"},
                              {"a.b.c": "angular_position"}, {".position": "angular_position"}):
                with self.assertRaises(ValueError, msg=repr(malformed)):
                    rows(directory, [dynamic_joint_state()], interface_kinds=malformed)
            with self.assertRaises(ValueError):
                rows(directory, [charging_status()],
                     interface_kinds={"fl_wheel_joint.position": "angular_position"})
            with self.assertRaises(ValueError):
                rows(directory, [dynamic_joint_state()],
                     interface_kinds={"fl_wheel_joint.position": "angular_position",
                                      "absent_joint.position": "angular_position",
                                      "fl_wheel_joint.velocity": "angular_velocity",
                                      "fr_wheel_joint.position": "linear_position"})
            observed, report = rows(directory, [
                dynamic_joint_state(names=("left",), interfaces=(("position",),), values=((1.,),)),
                dynamic_joint_state(names=("right",), interfaces=(("velocity",),), values=((2.,),))],
                interface_kinds={"left.position": "angular_position",
                                 "right.velocity": "linear_velocity"})
            self.assertEqual(report["output_records"], 2)
            self.assertEqual(float(observed[0]["dynamic_interface_value_rad"]), 1.)
            self.assertEqual(float(observed[1]["dynamic_interface_value_m_s"]), 2.)
            target = Path(directory) / "cli-bag"
            bag(target, [charging_status()])
            refused = subprocess.run(
                [os.sys.executable, str(SCRIPT), str(target), str(Path(directory) / "cli-out"),
                 "--interface-kind", "fl_wheel_joint.position=angular_position"],
                capture_output=True)
            self.assertEqual(refused.returncode, 2)
            self.assertFalse((Path(directory) / "cli-out").exists())

    def test_a_matching_type_name_is_not_enough(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "wrong-hash"
            bag(target, [charging_status()], hash_override="RIHS01_" + "0" * 64)
            with self.assertRaises((ValueError, AssertionError)):
                convert(target)
            self.assertEqual(subprocess.run(
                [os.sys.executable, str(SCRIPT), str(target), str(Path(directory) / "refused")],
                capture_output=True).returncode, 2)
            other = Path(directory) / "wrong-definition"
            bag(other, [charging_status()],
                definition_replace=("float32 current\n", "float64 current\n"))
            with self.assertRaises((ValueError, AssertionError)):
                convert(other)

    def test_command_line_reaches_the_common_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bag"
            bag(target, [charging_status(), dynamic_joint_state()])
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(target), str(out),
                            "--interface-kind", "fl_wheel_joint.position=angular_position",
                            "--interface-kind", "fl_wheel_joint.velocity=angular_velocity",
                            "--interface-kind", "fr_wheel_joint.position=linear_position"],
                           check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["output_records"], 4)
            self.assertEqual(report["clock"], "Unknown")
            if not Path(READER).exists():
                print("ground message common-reader integration NOT_RUN: reader not built")
                return
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 4)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["charging_current_total_a"], 1.5)
            self.assertEqual(fields["charging_charger_type_reported"], "UNKNOWN")
            self.assertIsNone(fields["charging_current_battery_2_a"])
            self.assertEqual(common["observations"][1]["fields"]["dynamic_interface_value_rad"], 1.25)
            self.assertEqual(common["observations"][3]["fields"]["dynamic_interface_value_m"], 2.5)


class OutdoorNavAutonomyStatusTests(unittest.TestCase):
    """Clearpath OutdoorNav's own reported autonomy state, from an authored rosbag2.

    Source: cpr-application/clearpath_outdoornav eee7d9aaa7311a60d2137f08af334c7a26f65c15,
    clearpath_navigation_msgs/msg/AutonomyStatus.msg (334 B, sha256 6fbda0d2...8bcb164e). The
    product documents no export of its own, so the saved artefact is an operator-run rosbag2;
    it documents no battery or health topic at all, so none is claimed here.
    """

    def test_the_pinned_definition_hashes_to_the_qualified_value(self):
        self.assertIn(AUTONOMY, OUTDOORNAV_DEFINITIONS)
        self.assertEqual(STORE.hash_rihs01(normalize_msgtype(AUTONOMY)), HASHES[AUTONOMY])
        self.assertNotEqual(HASHES[AUTONOMY], HASHES[CHARGING])

    def test_reported_autonomy_state_keeps_its_own_meaning(self):
        with tempfile.TemporaryDirectory() as directory:
            observed, report = rows(directory, [
                autonomy_status(),
                autonomy_status(state=7, paused=True, goal="", frame="odom",
                                seconds=9, nanoseconds=0, start_seconds=8, start_nanoseconds=1000)])
            self.assertEqual((report["decoded_messages"], report["output_records"]), (2, 2))
            first, second = observed
            self.assertEqual(first["autonomy_state_code"], "1")
            self.assertEqual(first["autonomy_state_reported"], "MISSION")
            self.assertEqual(first["autonomy_paused_reported"], "false")
            self.assertEqual(second["autonomy_state_reported"], "DOCKING_MAP")
            self.assertEqual(second["autonomy_paused_reported"], "true")
            self.assertEqual(first["autonomy_source_stamp_us"], str(2 * 10**6 + 123456))
            self.assertEqual(first["autonomy_last_start_us"], str(1 * 10**6 + 500000))
            self.assertEqual(second["autonomy_last_start_us"], str(8 * 10**6 + 1))
            self.assertIn("NOT_UTC_OR_BOOT_RELATIVE", first["autonomy_time_basis"])
            self.assertIn("NOT_A_FAULT_REPORT", first["autonomy_status_basis"])
            self.assertEqual(bytes.fromhex(first["autonomy_goal_hex"][4:]).decode(), "poi_3")
            self.assertEqual(second["autonomy_goal_hex"], "hex:")
            self.assertEqual(bytes.fromhex(first["autonomy_frame_hex"][4:]).decode(), "map")
            descriptor = json.loads(bytes.fromhex(first["source_record_hex"][4:]))
            self.assertEqual(descriptor["type"], AUTONOMY)
            self.assertEqual(descriptor["type_hash"], HASHES[AUTONOMY])
            self.assertIn("/selected", descriptor["topic"])
            self.assertNotIn("robot_identity", first)
            self.assertNotIn("fleet_name_hex", first)
            self.assertEqual(first["battery_remaining_fraction"], "")
            self.assertEqual(first["pose_frame_hex"], "")

    def test_an_undocumented_state_code_is_retained_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            observed, _ = rows(directory, [autonomy_status(state=9), autonomy_status(state=255)])
            self.assertEqual([row["autonomy_state_reported"] for row in observed],
                             ["UNKNOWN_CODE_RETAINED"] * 2)
            self.assertEqual([row["autonomy_state_code"] for row in observed], ["9", "255"])

    def test_bad_types_and_corruption_are_refused_rather_than_repaired(self):
        types = STORE.types
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "wrong-definition"
            bag(target, [autonomy_status()],
                definition_replace=("uint8 state\n", "int32 state\n"))
            with self.assertRaises((ValueError, AssertionError)):
                convert(target)
            other = Path(directory) / "wrong-hash"
            bag(other, [autonomy_status()], hash_override="RIHS01_" + "1" * 64)
            with self.assertRaises((ValueError, AssertionError)):
                convert(other)
            for stamp in ({"sec": -1, "nanosec": 0}, {"sec": 2, "nanosec": 10**9}):
                message = autonomy_status()
                message.last_start = types["builtin_interfaces/msg/Time"](stamp["sec"], stamp["nanosec"])
                broken = Path(directory) / f"stamp-{stamp['sec']}-{stamp['nanosec']}"
                with self.subTest(stamp=stamp):
                    try:
                        bag(broken, [message])
                    except (OverflowError, ValueError):
                        continue  # the serializer itself refuses; that is also a refusal
                    with self.assertRaises(ValueError):
                        convert(broken)

    def test_command_line_and_common_reader_carry_the_reported_state(self):
        configured = os.environ.get("MUSUBI_TELEMETRY_READER")
        if configured:
            self.assertTrue(Path(configured).exists(),
                            f"MUSUBI_TELEMETRY_READER is set to {configured!r}, which does not exist; "
                            "an explicitly configured reader that is missing is a failure, not a skip")
        elif not Path(READER).exists():
            self.skipTest("no reader configured and the default build is absent, so the common-output "
                          "proof cannot run; this is an explicit skip and never a pass")
        with tempfile.TemporaryDirectory() as directory:
            target, out = Path(directory) / "bag", Path(directory) / "out"
            bag(target, [autonomy_status(state=3, goal="dock_a"), autonomy_status(state=5, paused=True)])
            subprocess.run([os.sys.executable, str(SCRIPT), str(target), str(out)],
                           check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual((report["output_records"], report["clock"]), (2, "Unknown"))
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            first, second = (o["fields"] for o in common["observations"])
            self.assertEqual(first["autonomy_state_reported"], "GOTO")
            self.assertEqual(second["autonomy_state_reported"], "DOCKING_LOCAL")
            self.assertEqual(second["autonomy_paused_reported"], "true")
            self.assertEqual(first["autonomy_last_start_us"], 1500000)
            units = common["profile_units"]
            self.assertIn("declares_no_epoch", units["autonomy_source_stamp_us"])
            self.assertIn("never_resolved_to_a_place", units["autonomy_goal_hex"])
            self.assertIn("never_a_verified_activity", units["autonomy_state_reported"])


if __name__ == "__main__":
    unittest.main()
