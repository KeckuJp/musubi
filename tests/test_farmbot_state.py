"""Conditional producer-code checks for saved FarmBot state; no device access."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_ardupilot_battery_csv import convert

ROOT = Path(__file__).resolve().parents[1]


def record(settings, time=1234567):
    return {"capture_time_us": time, "payload": {"informational_settings": settings,
        "location_data": {"position": {"x": 100, "y": None}}, "unknown": ["retain"]}}


class FarmbotStateTests(unittest.TestCase):

    def test_no_semantic_guess_from_malformed_empty_or_untimed_state(self):
        for settings in [{}, {"busy": "false"}, {"uptime": -1}, {"soc_temp": -274},
                         {"uptime": 1.2}, {"soc_temp": "47"}, {"controller_uuid": 12}]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                value = record(settings); value["payload"].pop("location_data")
                convert(json.dumps(value), "farmbot-state")
        raw = record({"busy": False}); raw.pop("capture_time_us")
        with self.assertRaises(ValueError): convert(json.dumps(raw), "farmbot-state")
        with self.assertRaises(ValueError): convert('{"payload":{},"payload":{}}', "farmbot-state")
        with self.assertRaises(ValueError):
            convert(json.dumps(record({"busy": True}, -1)), "farmbot-state")




if __name__ == "__main__": unittest.main()
