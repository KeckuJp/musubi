"""Constructed text captures; code qualification, not new flight evidence."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dataflash_text import convert

ROOT = Path(__file__).resolve().parents[1]


def sample(value=7, default=False):
    return ("FMT, 128, 89, FMT, BBnNZ, Type,Length,Name,Format,Columns\n"
        "FMT, 129, 75, MSG, QZ, TimeUS,Message\n"
        + ("FMT, 32, 35, PARM, QNff, TimeUS,Name,Value,Default\n" if default else
           "FMT, 32, 31, PARM, QNf, TimeUS,Name,Value\n")
        + "MSG, 100, ArduPlane synthetic\n"
        + f"PARM, 100, LOG_BITMASK, {value}" + (", 1" if default else "") + "\n").encode()


class TextCapture(unittest.TestCase):



    def test_no_ignored_prefix_unknown_short_duplicate_or_missing_newline(self):
        for data in [b"garbage\n" + sample(), sample()[:-1], sample() + b"UNKNOWN, 1\n",
                     sample().replace(b"PARM, 100, LOG_BITMASK, 7", b"PARM, 100"),
                     sample() + sample(), sample() + b"\n",
                     sample().replace(b"LOG_BITMASK, 7", b"LOG_BITMASK, NaN"),
                     sample().replace(b"TimeUS,Name,Value", b"TimeUS,Name,mavpackettype"),
                     sample().replace(b"32, 31, PARM", b"32, 30, PARM")]:
            with self.assertRaises((ValueError, TypeError)):
                convert(data)
