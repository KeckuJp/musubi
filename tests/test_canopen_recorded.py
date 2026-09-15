"""Synthetic CANopen CC status semantics, not device qualification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dronecan_capture import convert_canopen, convert_canopen_dictionary, convert_canopen_pdo

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,standard_id,data_hex\n"


class CanopenRecordedTests(unittest.TestCase):





    def test_dictionary_invalid_duplicate_and_compact_boundary(self):
        valid = "[2000]\nParameterName=A\nDataType=1\nAccessType=ro\n"
        row = next(csv.DictReader(io.StringIO(convert_canopen_dictionary(valid, 1)[0])))
        self.assertEqual(row["declared_bit_width"], "8")
        for text in (valid + valid, valid.replace("DataType=1", "DataType=bad"),
                     valid + "PDOMapping=2\n", "[DEFAULT]\nDataType=1\n" + valid,
                     valid + "[2000sub0]\nParameterName=Orphan\nDataType=1\nAccessType=ro\n",
                     valid + "[2000sub100]\nParameterName=Invalid\n",
                     "[2000]\nParameterName=Container\nObjectType=9\nSubNumber=1\n"):
            with self.assertRaises(ValueError):
                convert_canopen_dictionary(text, 1)
        compact = "[2000]\nParameterName=Array\nObjectType=8\nCompactSubObj=5\n"
        self.assertIn("COMPACT_NOT_EXPANDED", convert_canopen_dictionary(compact, 1)[0])
        with self.assertRaises(ValueError):
            convert_canopen_dictionary(valid, None)


    def test_reject_wrong_kind_length_reserved_bit_and_time(self):
        for text in [HEADER + "1,701,\n", HEADER + "1,701,0000\n", HEADER + "1,701,85\n",
                     HEADER + "2,701,05\n1,701,05\n", HEADER + "1,800,05\n",
                     HEADER + "1,700,05\n", HEADER.replace("standard_id", "extended_id") + "1,701,05\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                convert_canopen(text)
