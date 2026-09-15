"""Independent synthetic NMEA MWV tests; no capture files are accessed."""
import copy
import csv
import importlib.util
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_nmea_wind.py"
COLUMNS = ["unlocated_time_us", "source_line", "talker", "wind_reference", "reported_valid",
           "wind_angle_rad", "wind_speed_m_s", "source_sentence_hex"]


def sentence(payload, prefix="$" ):
    checksum = 0
    for byte in payload.encode("ascii"):
        checksum ^= byte
    return f"{prefix}{payload}*{checksum:02X}"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.converter = load(SCRIPT)

    def decode(self, text):
        converted, report = self.converter.convert(text)
        rows = list(csv.reader(io.StringIO(converted)))
        self.assertEqual(rows[0], COLUMNS)
        return [dict(zip(COLUMNS, row)) for row in rows[1:]], report


    def test_units_references_and_signed_angle_boundaries(self):
        cases = [(0, "R", 10, "M", 0, 10), (180, "T", 36, "K", math.pi, 10),
                 (270, "R", 3600, "N", -math.pi / 2, 1852),
                 (360, "T", 0, "M", 0, 0)]
        lines = [sentence(f"WIMWV,{angle},{ref},{speed},{unit},A") for angle, ref, speed, unit, _, _ in cases]
        rows, report = self.decode("\r\n".join(lines) + "\r\n")
        self.assertEqual(report["valid_with_measurement"], 4)
        for index, (row, case) in enumerate(zip(rows, cases)):
            with self.subTest(case=case):
                self.assertEqual(row["unlocated_time_us"], "0")
                self.assertEqual(row["source_line"], str(index + 1))
                self.assertEqual(row["talker"], "WI")
                self.assertEqual(row["reported_valid"], "true")
                self.assertEqual(row["wind_reference"], "apparent_relative_to_vessel" if case[1] == "R"
                                 else "theoretical_relative_to_vessel")
                self.assertAlmostEqual(float(row["wind_angle_rad"]), case[4])
                self.assertAlmostEqual(float(row["wind_speed_m_s"]), case[5])
                self.assertEqual(bytes.fromhex(row["source_sentence_hex"][4:]).decode("ascii"), lines[index])

    def test_missing_invalid_unknown_extras_and_accounting(self):
        lines = [sentence("WIMWV,,R,2,M,A,extra,\"quoted\""), sentence("GPGGA,unsupported,\"raw\""),
                 sentence("IIMWV,90,T,,N,A"), sentence("WIMWV,,R,,M,A"),
                 sentence("WIMWV,90,R,4,M,V"), sentence("AIVDM,unsupported", "!")]
        rows, report = self.decode("\n".join(lines))
        self.assertEqual((report["input_records"], report["mwv_records"], report["unsupported_records"]), (6, 4, 2))
        self.assertEqual(report["invalid_status_records"], 1)
        self.assertEqual(report["valid_with_measurement"], 2)
        self.assertEqual(report["blank_lines"], 0)
        self.assertEqual(report["unsupported"], [{"line": 2, "raw": lines[1]}, {"line": 6, "raw": lines[5]}])
        self.assertEqual([row["source_line"] for row in rows], ["1", "3", "4", "5"])
        self.assertEqual([row["wind_speed_m_s"] for row in rows[1:]], ["", "", ""])
        self.assertEqual([row["wind_angle_rad"] for row in (rows[0], rows[2], rows[3])], ["", "", ""])
        self.assertEqual(rows[3]["reported_valid"], "false")
        self.assertEqual(rows[0]["source_sentence_hex"], "hex:" + lines[0].encode("ascii").hex())

    def test_reject_framing_checksum_blank_foreign_and_zero_mwv(self):
        good = sentence("WIMWV,90,R,2,M,A")
        cases = ["", "\n", sentence("GPGGA,unsupported"), "foreign", "\n" + good, good + "\n\n",
                 good + "\nforeign", good[:-1], good[:-2] + "ZZ", good[:-2] + "00",
                 good.replace("90", "91"), good + "trailing", good + "\x00",
                 sentence("WIMWV,90,R,2,M,A,extra") + "\t", sentence("MWV,90,R,2,M,A"),
                 sentence("WIMWV,90,R,2,M,A").replace("$", "#"), good + "é"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.converter.convert(text)

    def test_reject_numeric_range_unit_reference_status_and_short_payload(self):
        fields = ["90", "R", "2", "M", "A"]
        invalid = {0: ["-1", "361", "NaN", "inf", "junk"], 1: ["", "X"],
                   2: ["-1", "NaN", "inf", "junk", "1e999"], 3: ["", "X"], 4: ["", "X"]}
        for index, values in invalid.items():
            for value in values:
                source = fields.copy()
                source[index] = value
                with self.subTest(field=index, value=value), self.assertRaises(ValueError):
                    self.converter.convert(sentence("WIMWV," + ",".join(source)))
        for payload in ("WIMWV,90,R,2,M", "WIMWV,junk,R,2,M,V", "WIMWV,90,R,-1,M,V"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.converter.convert(sentence(payload))

    def test_cli_exclusive_directory_and_validate_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.log", Path(directory) / "result"
            source.write_text(sentence("WIMWV,90,R,2,M,A"))
            command = [sys.executable, str(SCRIPT), str(source), str(output)]
            run = subprocess.run(command, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual({p.name for p in output.iterdir()}, {"converted.csv", "report.json"})
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)
            source.write_text(sentence("WIMWV,90,R,2,M,A") + "\nforeign")
            command[-1] = str(Path(directory) / "invalid-result")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(Path(command[-1]).exists())




if __name__ == "__main__":
    unittest.main()
