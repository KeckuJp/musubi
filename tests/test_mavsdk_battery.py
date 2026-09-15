"""Synthetic fixed-version SDK exports, not a MAVSDK connection or vehicle capture."""
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


def sample():
    return dict(sdk_version="3.17.4", capture_time_us=10, battery=dict(id=2,
        voltageV=24, currentBatteryA=-1, capacityConsumedAh=3, remainingPercent=75,
        temperatureDegc=20, timeRemainingS=50, batteryFunction="BATTERY_FUNCTION_PROPULSION"),
        future={"unmapped": 7})


class SdkBattery(unittest.TestCase):

    def test_missing_sentinel_is_not_valid_zero_or_negative_fraction(self):
        record = sample()
        record["battery"].update(remainingPercent=-1, currentBatteryA="NaN", temperatureDegc="NaN",
                                 batteryFunction=99)
        row = next(csv.DictReader(io.StringIO(convert(json.dumps(record), "mavsdk-battery-json"))))
        self.assertEqual(row["battery_remaining_fraction"], "")
        self.assertEqual(row["battery_power_w"], "")
        self.assertEqual(row["battery_function_reported"], "UNKNOWN_99")
        for key, value in (("remainingPercent", 101), ("remainingPercent", -2), ("voltageV", True),
                           ("temperatureDegc", -274), ("capacityConsumedAh", -1),
                           ("voltageV", 1e100), ("remaining_percent", 1)):
            record = sample()
            record["battery"][key] = value
            with self.assertRaises(ValueError):
                convert(json.dumps(record), "mavsdk-battery-json")
        record = sample()
        record["sdk_version"] = "2.0.0"
        with self.assertRaises(ValueError):
            convert(json.dumps(record), "mavsdk-battery-json")
