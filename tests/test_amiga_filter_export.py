"""Two authored saved exports; no farm-ng runtime, physical data or control."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_rosbag_observations import convert_amiga_filter_json

ROOT = Path(__file__).resolve().parents[1]


def sample(x=2., parent="world", child="robot"):
    vector = dict(x=x, y=0., z=-1.)
    return dict(schema="farm-ng-amiga/37a8bf217d37a7bc15bb995ca9f13f3c9eebff13/FilterState",
        units=dict(position="m", linear_velocity="m/s", angular_velocity="rad/s", heading="rad"),
        capture_time_us=1234567, message=dict(heading=0., has_converged=False, is_calibrated=True,
        pose=dict(frame_a=parent, frame_b=child, a_from_b=dict(translation=vector,
            rotation=dict(unit_quaternion=dict(imag=dict(x=0., y=0., z=0.), real=1.))),
            tangent_of_b_in_a=dict(linear_velocity=vector, angular_velocity=dict(x=0., y=.5, z=0.))),
        uncertainty_diagonal=dict(data=[9., 8.]), innovation=dict(data=[7.]), future={"unchanged": 42}))


class AmigaExportTests(unittest.TestCase):
    def test_two_exports_reuse_motion_preserve_frames_unknowns_and_equal_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.jsonl"
            examples = [sample(), sample(-7., "map", "tool")]
            path.write_text("\n".join(map(json.dumps, examples)))
            output, report = convert_amiga_filter_json(path)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["source_messages"], 2)
            self.assertEqual([float(r["pose_position_m_x"]) for r in rows], [2., -7.])
            for row, original in zip(rows, examples):
                self.assertEqual(row["record_time_us"], "1234567")
                self.assertEqual(row["pose_covariance_status"], "NOT_PROVIDED")
                self.assertEqual(row["pose_frame_hex"], "hex:" + original["message"]["pose"]["frame_a"].encode().hex())
                self.assertEqual(row["twist_frame_hex"], "hex:" + original["message"]["pose"]["frame_b"].encode().hex())
                self.assertEqual(json.loads(bytes.fromhex(row["source_record_hex"][4:])), original)
            destination = Path(tmp) / "output"
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(path), str(destination), "--amiga-filter-json"], check=True, capture_output=True)
            self.assertEqual((destination / "observations.csv").read_text(), output)
            reader = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
            result = subprocess.run([reader,
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"), str(destination / "observations.csv"),
                "--allow-equal-time"], check=True, capture_output=True, text=True)
            common = json.loads(result.stdout)
            self.assertEqual(common["main_rows"], 2)
            for row, x in zip(common["observations"], [2., -7.]):
                self.assertEqual(row["fields"]["pose_position_m_x"], x)
                self.assertEqual(row["fields"]["twist_linear_m_s_x"], x)
                self.assertEqual(row["fields"]["twist_angular_rad_s_y"], .5)
                self.assertEqual(row["clock_basis"], "Unknown")
                self.assertIsNone(row["anchor_unix_us"])

    def test_wrong_schema_unit_clock_quaternion_and_missing_fields_reject(self):
        cases = []
        for key, value in [("schema", "unknown"), ("capture_time_us", -1), ("capture_time_us", True),
                           ("units", {"position": "cm"})]:
            item = sample(); item[key] = value; cases.append(item)
        item = sample(); item["message"]["pose"]["a_from_b"]["rotation"]["unit_quaternion"]["real"] = 2.; cases.append(item)
        item = sample(); del item["message"]["has_converged"]; cases.append(item)
        item = sample(); item["message"]["pose"]["frame_a"] = ""; cases.append(item)
        item = sample(); item["message"]["heading"] = float("nan"); cases.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.jsonl"
            for item in cases:
                path.write_text(json.dumps(item))
                with self.assertRaises((ValueError, KeyError)):
                    convert_amiga_filter_json(path)
            first = sample(); second = copy.deepcopy(first); second["capture_time_us"] -= 1
            path.write_text(json.dumps(first) + "\n" + json.dumps(second))
            with self.assertRaises(ValueError): convert_amiga_filter_json(path)


if __name__ == "__main__":
    unittest.main()
