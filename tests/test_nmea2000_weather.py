"""Synthetic classic frames against finite canboat-defined physical fields."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dronecan_capture import convert_nmea2000_weather

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,extended_id,data_hex\n"


class NmeaWeather(unittest.TestCase):

    def test_heading_missing_error_and_wrong_length(self):
        output, report = convert_nmea2000_weather(HEADER +
            "1,09f1122a,00ffffff7ffe7fff\n2,09f1132a,00ffffff7fffffff\n3,09fd022a,00\n", heading=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["heading_rad_status"], "NOT_AVAILABLE")
        self.assertEqual(rows[0]["heading_deviation_rad_status"], "NOT_AVAILABLE")
        self.assertEqual(rows[0]["heading_variation_rad_status"], "OUT_OF_RANGE_REPORTED")
        self.assertEqual(rows[0]["heading_reference_reported"], "UNKNOWN_3")
        self.assertEqual(rows[1]["turn_rate_rad_s_status"], "NOT_AVAILABLE")
        self.assertEqual(len(report["unsupported_frames"]), 1)
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(HEADER + "1,09f1122a,00\n", heading=True)


    def test_missing_error_and_unselected_frames_not_normal_values(self):
        output, report = convert_nmea2000_weather(HEADER +
            "1,09fd022a,fffffffffeffffff\n2,09fd032a,0000\n")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["wind_speed_m_s"], "")
        self.assertEqual(row["wind_speed_m_s_status"], "NOT_AVAILABLE")
        # Literal ff fe is 0xfeff (valid), while fe ff is the error indicator.
        output, _ = convert_nmea2000_weather(HEADER + "1,09fd022a,00fffffeffffffff\n")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["wind_angle_rad_status"], "OUT_OF_RANGE_REPORTED")
        self.assertEqual(row["wind_reference_reported"], "UNKNOWN_7")
        self.assertEqual(len(report["unsupported_frames"]), 1)
        for body in ("1,09fd022a,0000\n", "2,15fd080e,ff0001c572ffffff\n1,15fd080e,ff0001c572ffffff\n"):
            with self.assertRaises(ValueError):
                convert_nmea2000_weather(HEADER + body)
