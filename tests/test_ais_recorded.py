"""Authored AIS reports, not receiver or vessel evidence."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from pyais.encode import encode_dict
from scripts.convert_nmea_wind import convert
from tests.test_nmea_wind_conversion import sentence

ROOT = Path(__file__).resolve().parents[1]


class AisRecordedTests(unittest.TestCase):
    def report(self, **values):
        fields = dict(msg_type=1, mmsi=123456789, lon=-70.25, lat=40.5,
                      speed=12.3, course=90., heading=80, second=59)
        fields.update(values)
        return encode_dict(fields, radio_channel="A")[0]



    def test_missing_and_censored_values_not_zero_or_exact(self):
        for speed, status in ((102.3, "UNAVAILABLE"), (102.2, "AT_LEAST")):
            output, _ = convert(self.report(lon=181, lat=91, speed=speed, course=360,
                                           heading=511, second=63), "ais-class-a")
            row = next(csv.DictReader(io.StringIO(output)))
            for name in ("ais_longitude_deg", "ais_latitude_deg", "ais_speed_m_s", "ais_course_rad", "ais_true_heading_rad"):
                self.assertEqual(row[name], "")
            self.assertEqual(row["ais_speed_disposition"], status)
            self.assertEqual(row["ais_second_code"], "63")
            if speed == 102.2:
                self.assertAlmostEqual(float(row["ais_speed_lower_bound_m_s"]), 102.2 * 1852 / 3600)

    def test_corruption_truncation_and_reserved_values_rejected(self):
        valid = self.report()
        payload = valid[1:].split("*")[0].split(",")
        short = payload.copy(); short[5] = short[5][:-1]
        fragmented = payload.copy(); fragmented[1] = "2"
        for value in (valid[:-1], valid[:-2] + "00", sentence(",".join(short), "!"),
                      sentence(",".join(fragmented), "!"), self.report(course=360.1),
                      self.report(heading=360), self.report(lat=92), self.report(lon=182)):
            with self.assertRaises(ValueError): convert(value, "ais-class-a")
