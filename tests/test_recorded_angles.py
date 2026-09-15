"""Finite source-qualified synthetic exports, not decoded flight qualifications."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_recorded_angles_csv import convert

ROOT = Path(__file__).resolve().parents[1]


class RecordedAnglesTests(unittest.TestCase):


    def test_wrong_scale_header_nonfinite_and_time_fail(self):
        header = "time (us),attitude[0],attitude[1],attitude[2]\n"
        for text in [header, header + "1,90.5,0,0", header + "1,32768,0,0", header + "1,NaN,0,0",
                     header + "2,0,0,0\n1,0,0,0", header.replace("(us)", "(ms)") + "1,0,0,0",
                     header + "1,0,0", header + "1,0,0,0,extra"]:
            with self.assertRaises(ValueError):
                convert(text, "inav-7.1.2-raw-attitude")
        with self.assertRaises(ValueError):
            convert("TimeUS,Roll,Pitch,Yaw\n1,inf,0,0", "plane-4.3.1-decoded-att")
