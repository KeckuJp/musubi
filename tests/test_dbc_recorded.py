"""Authored synthetic DBC and independent literal CAN inputs, not real captures."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_dbc_recorded import convert

ROOT = Path(__file__).resolve().parents[1]
DBC = b'''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 256 Measurements: 4 ECU
 SG_ Voltage : 0|16@1+ (0.01,0) [0|655.35] "V" ECU
 SG_ Temperature : 16|8@1- (1,0) [-128|127] "degC" ECU
 SG_ Vendor : 24|8@1+ (1,0) [0|255] "mystery" ECU
'''
HEADER = "time_us,standard_id,data_hex\n"


class DbcSignals(unittest.TestCase):
    def test_zero_observations_remain_diagnostic_not_successful_adaptation(self):
        self.assertEqual(convert(HEADER + "1,100,e803f602\n", DBC)[1]["conversion_status"], "OBSERVATIONS_PRODUCED")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, dictionary = root / "capture.csv", root / "signals.dbc"
            source.write_text(HEADER + "1,200,00000000\n")
            dictionary.write_bytes(DBC)
            result = subprocess.run([sys.executable, "-m", "scripts.convert_dbc_recorded",
                str(source), str(dictionary), str(root / "out.csv"), str(root / "report.json")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not a successful adaptation", result.stderr)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["conversion_status"], "NO_SUPPORTED_OBSERVATIONS")
            self.assertEqual(report["observations"], 0)
            self.assertEqual(len(report["unsupported"]), 1)
            self.assertEqual(len(list(csv.DictReader(io.StringIO((root / "out.csv").read_text())))), 0)




    def test_choice_multiplex_branch_and_signed_code(self):
        dbc = b'''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 256 Multiplexed: 2 ECU
 SG_ Select M : 0|8@1+ (1,0) [0|255] "" ECU
 SG_ First m0 : 8|8@1- (2,10) [-128|127] "" ECU
 SG_ Second m1 : 8|8@1+ (1,0) [0|255] "V" ECU
VAL_ 256 Select 0 "First branch" 1 "Second branch";
VAL_ 256 First -1 "Missing" 2 "Ready";
'''
        output, report = convert(HEADER + "1,100,00ff\n2,100,010c\n", dbc, include_enums=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["observations"], 4)
        self.assertFalse(report["unsupported"])
        self.assertEqual(rows[1]["enum_raw_code"], "-1")
        self.assertEqual(bytes.fromhex(rows[1]["enum_label_hex"][4:]).decode(), "Missing")
        self.assertEqual(float(rows[3]["voltage_v"]), 12.)
        from cantools.database.errors import DecodeError
        with self.assertRaises(DecodeError):
            convert(HEADER + "1,100,02ff\n", dbc, include_enums=True)


    def test_cli_does_not_overwrite_prior_input_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dbc, report = [Path(directory) / name for name in ("in.csv", "in.dbc", "report.json")]
            text = HEADER + "1,100,e803f602\n"
            source.write_text(text)
            dbc.write_bytes(DBC)
            result = subprocess.run([sys.executable, "-m", "scripts.convert_dbc_recorded",
                str(source), str(dbc), str(source), str(report)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_text(), text)
            self.assertFalse(report.exists())

    def test_finite_unit_set_extended_and_enum_boundary(self):
        for unit, field, expected in [(b"A", "current_a", 10), (b"m", "distance_m", 10),
                (b"rpm", "angular_speed_rad_s", math.pi / 3),
                (b"K", "temperature_k", 10), (b"degK", "temperature_k", 10)]:
            dbc = DBC.replace(b'"V"', b'"' + unit + b'"').replace(b"BO_ 256", b"BO_ 2147483904")
            output, report = convert(HEADER.replace("standard_id", "extended_id") +
                "1,00000100,e8030000\n", dbc, extended=True)
            self.assertAlmostEqual(float(next(csv.DictReader(io.StringIO(output)))[field]), expected)
            self.assertEqual(report["observations"], 2)
        output, report = convert(HEADER + "1,100,e8030000\n",
            DBC + b'VAL_ 256 Voltage 1000 "Missing";\n')
        self.assertEqual(report["observations"], 1)
        self.assertEqual(len(report["unsupported"]), 2)


    def test_big_endian_and_important_rejections(self):
        dbc = DBC.replace(b"0|16@1+", b"7|16@0+")
        output, _ = convert(HEADER + "1,100,03e81400\n", dbc)
        self.assertEqual(float(next(csv.DictReader(io.StringIO(output)))["voltage_v"]), 10)
        for body in ("1,100,00\n", "1,100,0000000000\n", "2,100,e803f602\n1,100,e803f602\n"):
            with self.assertRaises(ValueError):
                convert(HEADER + body, DBC)
        with self.assertRaises(ValueError):
            convert(HEADER + "1,100,e803f602\n", DBC + DBC[DBC.index(b"BO_"):])
