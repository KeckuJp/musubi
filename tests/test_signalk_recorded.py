"""Finite code/spec fixtures, not captured vessel data."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_signalk_recorded import convert

ROOT = Path(__file__).resolve().parents[1]


def delta(context, values, timestamp="2025-01-01T01:02:03.123456789Z"):
    return json.dumps({"context": context, "updates": [{"timestamp": timestamp,
        "$source": "sensor.reported", "values": [{"path": path, "value": value} for path, value in values]}]})


class SignalKTests(unittest.TestCase):




    def test_invalid_selected_meaning_time_and_structure_are_rejected(self):
        context = "vessels.example-a"
        valid = delta(context, [("navigation.speedThroughWater", 1.)])
        for source in ["", "[]", "{}", valid.replace(context, "vessels.other"),
                       valid.replace("123456789Z", "123+09:00"),
                       delta(context, [("navigation.speedThroughWater", True)]),
                       delta(context, [("navigation.speedThroughWater", "1.0")]),
                       delta(context, [("environment.water.temperature", -1.)]),
                       delta(context, [("navigation.speedThroughWater", float("nan"))]),
                       valid + "\n" + delta(context, [("navigation.speedThroughWater", 1.)], "2020-01-01T00:00:00Z"),
                       valid.replace('"context":', '"context":"vessels.other","context":')]:
            with self.subTest(source=source), self.assertRaises((ValueError, KeyError, TypeError)):
                convert(source, context)


if __name__ == "__main__":
    unittest.main()
