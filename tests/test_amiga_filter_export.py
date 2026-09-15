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
