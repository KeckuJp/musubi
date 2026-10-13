"""Authored fixed control-selection reports; no ROS graph, command or device execution."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from rosbags.typesys import get_types_from_msg
from rosbags.rosbag2.writer import StoragePlugin
from scripts import convert_rosbag_observations as c
from tests import test_ground_message_closure as old
from tests.test_rosbag_observations import bag

PREFIX = "clearpath_control_selection_msgs/msg/"
TYPE = PREFIX + "ControlSelectionState"
HASH = "RIHS01_ff1ac5f75b32bdf967356148a79b5aa522a534ed33e8bb20826bab6295ecb904"
ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/ground-message-closure/profile.toml"


def sample(code, enabled=False, paused=False):
    return c.STORE.types[TYPE](c.STORE.types[PREFIX + "ControlState"](enabled, paused),
                               c.STORE.types[PREFIX + "ControlMode"](code))


class OutdoorNavControlStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fixed public schema only, also constructible on the pre-feature baseline for RED.
        for name, definition in (
            ("ControlState", "bool enabled\nbool paused\n"),
            ("ControlMode", "int8 NEUTRAL=0\nint8 MANUAL=1\nint8 AUTONOMY=2\nint8 mode\n"),
            ("ControlSelectionState", "ControlState autonomy\nControlMode mode\n")):
            c.STORE.register(get_types_from_msg(definition, PREFIX + name))

    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.csv"
            path.write_text(text)
            process = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                     check=True, capture_output=True)
            return json.loads(process.stdout)

    def test_nested_report_in_two_containers_reaches_common(self):
        self.assertEqual(c.STORE.hash_rihs01(TYPE), HASH)
        for storage, values in ((StoragePlugin.SQLITE3, [(0, False, False), (1, True, False), (2, True, True)]),
                                (StoragePlugin.MCAP, [(-128, False, True), (127, True, False)])):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(storage=storage):
                target = Path(tmp) / "bag"
                bag(target, plugin=storage, messages=[sample(*value) for value in values])
                text, report = c.convert(target, outdoornav_control_state=True)
                self.assertEqual((report["source_messages"], report["decoded_messages"],
                                  report["output_records"], len(report["unsupported_records"])),
                                 (len(values) + 1, len(values), len(values), 1))
                common = self.common(text)
                self.assertEqual(common["main_rows"], len(values))
                self.assertEqual(common["platform_domain"], "Unknown")
                for index, (observation, (code, enabled, paused)) in enumerate(zip(common["observations"], values)):
                    fields = observation["fields"]
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertEqual(fields["control_selection_mode_code"], code)
                    self.assertEqual(fields["control_selection_mode_reported"],
                                     {0: "NEUTRAL", 1: "MANUAL", 2: "AUTONOMY"}.get(code, "UNKNOWN_CODE_RETAINED"))
                    self.assertEqual(fields["control_selection_autonomy_enabled_reported"], str(enabled).lower())
                    self.assertEqual(fields["control_selection_autonomy_paused_reported"], str(paused).lower())
                    self.assertIn("NO_PRODUCER_TIMESTAMP", fields["control_selection_time_basis"])
                    self.assertIn("NOT_CONTROL_AUTHORITY", fields["control_selection_basis"])
                    source = json.loads(bytes.fromhex(fields["source_record_hex"][4:]))
                    self.assertEqual((source["type"], source["type_hash"], source["topic"], source["capture_ns"]),
                                     (TYPE, HASH, f"/example/{index}", "7000123456"))
                    self.assertEqual(source["raw_cdr_hex"], bytes(c.STORE.serialize_cdr(sample(code, enabled, paused), TYPE)).hex())
                    self.assertIsNone(fields["autonomy_source_stamp_us"])
                    self.assertIsNone(fields["battery_remaining_fraction"])
                    self.assertIsNone(fields["pose_frame_hex"])

    def test_schema_corruption_and_invalid_selection_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "valid"
            bag(target, messages=[sample(1)])
            c.convert(target, outdoornav_control_state=True)
            for option in ({"ros1": True}, {"scan_compact": True}, {"legacy_schema": "unknown"},
                           {"outdoornav_control_state": "yes"}):
                with self.subTest(option=option), self.assertRaises(ValueError):
                    c.convert(target, **dict({"outdoornav_control_state": True}, **option))
            bad_hash = Path(tmp) / "hash"
            bag(bad_hash, messages=[sample(1)], hash_override="RIHS01_" + "1" * 64)
            with self.assertRaises((ValueError, AssertionError)):
                c.convert(bad_hash, outdoornav_control_state=True)
            bad_definition = Path(tmp) / "definition"
            old.bag(bad_definition, [sample(1)], definition_replace=("bool enabled", "uint8 enabled"))
            with self.assertRaises((ValueError, AssertionError)):
                c.convert(bad_definition, outdoornav_control_state=True)
            for name, raw in (("short", b"\x00\x01\x00\x00\x01"),
                              ("bad-bool", b"\x00\x01\x00\x00\x02\x00\x01"),
                              ("tail", b"\x00\x01\x00\x00\x01\x00\x01\x07")):
                path = Path(tmp) / name
                bag(path, messages=[sample(1)])
                with sqlite3.connect(next(path.glob("*.db3"))) as db:
                    db.execute("UPDATE messages SET data=? WHERE id=1", (raw,))
                with self.subTest(name=name), self.assertRaises((ValueError, AssertionError, IndexError)):
                    c.convert(path, outdoornav_control_state=True)
            for record in ({}, {"autonomy": {"enabled": 1, "paused": False}, "mode": {"mode": 1}},
                           {"autonomy": {"enabled": True, "paused": False}, "mode": {"mode": True}},
                           {"autonomy": {"enabled": True, "paused": False}, "mode": {"mode": 128}}):
                with self.assertRaises(ValueError):
                    c.control_selection_quantities(record)

    def test_default_unchanged_and_cli_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path(tmp) / "previous"
            old.bag(previous, [old.autonomy_status()])
            output, _ = c.convert(previous)
            self.assertEqual(hashlib.sha256(output.encode()).hexdigest(),
                             "070034bf22df03fc90ec4602f7d474d352396835880bb6d70e2e4990982c6df0")
            target = Path(tmp) / "bag"
            bag(target, messages=[sample(2), old.autonomy_status()])
            _, report = c.convert(target)
            self.assertEqual((report["decoded_messages"], len(report["unsupported_records"])), (1, 2))
            destination = Path(tmp) / "out"
            command = [sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                       str(target), str(destination), "--outdoornav-control-state"]
            process = subprocess.run(command, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(self.common((destination / "observations.csv").read_text())["main_rows"], 2)
            invalid_output = Path(tmp) / "invalid-mode-output"
            refused_command = [*command[:3], str(invalid_output), *command[4:], "--metadata-only"]
            refused = subprocess.run(refused_command, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(invalid_output.exists())


if __name__ == "__main__":
    unittest.main()
