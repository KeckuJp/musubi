"""Authored saved exports; no robots, transport, or task execution."""
import copy
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PIN = "6990fc7418c412234b4ff64f01560d8bf3047f46"
PROFILE = ROOT / "profiles/declared/rmf-fleet-state/profile.toml"


def robot(percent=25, mode=2):
    return dict(name="same", model="model", task_id="reported", seq=2**64-1,
                mode=dict(mode=mode, mode_request_id=4, performing_action=""),
                battery_percent=percent, location=None, path=[], future={"not": "discarded"})


def record(robots, **extra):
    return dict(schema_commit=PIN, capture_time_us=10,
                fleet_state=dict(name="fleet", robots=robots, future=42), **extra)


class FleetArrayTests(unittest.TestCase):
    def setUp(self):
        old_limit = csv.field_size_limit(16 * 1024 * 1024)
        self.addCleanup(csv.field_size_limit, old_limit)
        self.m = importlib.import_module("scripts.convert_rmf_fleet_state")

    def common(self, output):
        executable = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not executable:
            self.skipTest("actual common reader not configured")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "saved.csv"
            source.write_text(output)
            result = subprocess.run([executable, str(PROFILE), str(source), "--allow-equal-time"],
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fleet_membership_repeated_names_empty_and_common(self):
        first = robot()
        point = dict(t=dict(sec=5, nanosec=1), x=1.5, y=-2, yaw=.5, level_name="L1",
                     index=9, obey_approach_speed_limit=True, approach_speed_limit=1.2)
        first.update(location=point, path=[point, dict(point, x=3)])
        documents = [record([first, robot(0, 999)], location_unit="m", yaw_unit="rad"), record([])]
        lines = list(map(json.dumps, documents))
        output, report = self.m.convert("\n".join(lines))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["rmf_record_kind"] for r in rows],
                         ["FLEET_SUMMARY", "STATE", "PATH_POINT", "PATH_POINT", "STATE", "FLEET_SUMMARY"])
        self.assertEqual([r["fleet_robot_index"] for r in rows], ["", "0", "0", "0", "1", ""])
        self.assertEqual([r["battery_remaining_fraction"] for r in rows], ["", "0.25", "", "", "0.0", ""])
        self.assertEqual(rows[4]["rmf_mode_reported"], "UNKNOWN")
        self.assertEqual(rows[1]["rmf_sequence_reported_text"], "u64:18446744073709551615")
        self.assertEqual(rows[3]["rmf_map_x_m"], "3.0")
        self.assertEqual(rows[4]["rmf_map_x_m"], "")
        self.assertEqual(rows[-1]["fleet_robot_count"], "0")
        self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), lines[0])
        self.assertTrue(all(not r["source_record_hex"] for r in rows[1:5]))
        sha = "sha256:" + hashlib.sha256(lines[0].encode()).hexdigest()
        self.assertTrue(all(r["rmf_snapshot_sha256"] == sha for r in rows[:5]))
        self.assertEqual([r["rmf_source_record_index"] for r in rows], ["0"]*5+["1"])
        self.assertEqual((report["source_records"], report["robot_states"], report["path_points"],
                          report["output_rows"]), (2, 2, 2, 6))
        common = self.common(output)
        self.assertEqual(len(common["observations"]), 6)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        fields = [o["fields"] for o in common["observations"]]
        self.assertEqual((fields[1]["fleet_robot_index"], fields[4]["fleet_robot_index"]), (0, 1))
        self.assertEqual((fields[1]["battery_remaining_fraction"], fields[4]["battery_remaining_fraction"]), (.25, 0))
        self.assertEqual(fields[3]["rmf_map_x_m"], 3)
        self.assertEqual(fields[5]["fleet_robot_count"], 0)
        self.assertEqual(fields[5]["rmf_source_record_index"], 1)
        self.assertIn("CALLER_DECLARED_UNITS", common["profile_units"]["rmf_position_basis"])

    def test_important_failure_and_undeclared_location(self):
        valid = record([robot()])
        bad = []
        for value in (None, {}, "robots"):
            item = copy.deepcopy(valid); item["fleet_state"]["robots"] = value; bad.append(item)
        for key, value in (("schema_commit", "wrong"), ("capture_time_us", True),
                           ("location_unit", "cm")):
            item = copy.deepcopy(valid); item[key] = value; bad.append(item)
        for key, value in (("battery_percent", None), ("battery_percent", 101), ("seq", -1)):
            item = copy.deepcopy(valid); item["fleet_state"]["robots"][0][key] = value; bad.append(item)
        item = copy.deepcopy(valid); del item["fleet_state"]["robots"]; bad.append(item)
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.m.convert(json.dumps(item))
        for text in ("", '{"x":1,"x":2}', json.dumps(valid)+"\n"+json.dumps(dict(valid, capture_time_us=9))):
            with self.subTest(text=text), self.assertRaises(ValueError): self.m.convert(text)
        valid["fleet_state"]["robots"][0]["location"] = {"future": "not interpreted"}
        rows = list(csv.DictReader(io.StringIO(self.m.convert(json.dumps(valid))[0])))
        self.assertIn("NOT_SELECTED", rows[1]["rmf_position_basis"])

    def test_many_robots_bounds_and_cli_no_overwrite(self):
        text = json.dumps(record([robot() for _ in range(1024)]))
        output, report = self.m.convert(text)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), rows[-1]["fleet_robot_index"]), (1025, "1023"))
        self.assertEqual(sum(bool(r["source_record_hex"]) for r in rows), 1)
        common = self.common(output)["observations"]
        self.assertEqual(len(common), 1025)
        self.assertEqual(common[-1]["fields"]["fleet_robot_index"], 1023)
        for name in ("MAX_INPUT_BYTES", "MAX_RECORD_BYTES", "MAX_ROBOTS", "MAX_OUTPUT_BYTES"):
            with self.subTest(bound=name), patch.object(self.m, name, 1), self.assertRaises(ValueError):
                self.m.convert(json.dumps(record([robot(), robot()])))
        with tempfile.TemporaryDirectory() as directory:
            source, target, report_path = [Path(directory)/name for name in ("input.jsonl", "out.csv", "report.json")]
            source.write_text(json.dumps(record([])))
            command = [os.sys.executable, str(ROOT/"scripts/convert_rmf_fleet_state.py"), str(source),
                       str(target), "--report", str(report_path)]
            done = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            original = target.read_bytes(), report_path.read_bytes()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((target.read_bytes(), report_path.read_bytes()), original)


if __name__ == "__main__":
    unittest.main()
