"""Synthetic checks for exact timestamp conversion."""
import csv
import importlib.util
import io
from pathlib import Path
import unittest
import subprocess
import sys
import tempfile

ROOT = next(root for root in Path(__file__).resolve().parents
            if (root / "scripts/convert_trajectory_csv.py").is_file())
SCRIPT = ROOT / "scripts/convert_trajectory_csv.py"
SPEC = importlib.util.spec_from_file_location("trajectory_converter", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TrajectoryConversionContract(unittest.TestCase):
    def test_exact_decimal_microseconds_and_source_cells(self):
        result = MODULE.convert("time,x,unknown,blank\n0.000001,1.25,new,\n1509241629.863412,2.5,next,0\n", "time")
        rows = list(csv.reader(io.StringIO(result)))
        self.assertEqual(rows[0], ["record_time (us)", "time", "x", "unknown", "blank"])
        self.assertEqual(rows[1], ["1", "0.000001", "1.25", "new", ""])
        self.assertEqual(rows[2], ["1509241629863412", "1509241629.863412", "2.5", "next", "0"])

    def test_malformed_and_unrepresentable_inputs(self):
        for text in ["", "time,x\n", "other,x\n1,2\n", "time,time\n1,2\n",
                     "time,x\n1\n", "time,x\n1,2,3\n", "time,x\n1,2\n1,3\n",
                     "time,x\n2,2\n1,3\n", "time,x\n1,2\n\n",
                     'time,x\n1,"a,b"\n', 'time,x\n1,"unterminated\n']:
            with self.subTest(text=text), self.assertRaises((ValueError, csv.Error)):
                MODULE.convert(text, "time")
        for value in ["NaN", "Infinity", "-1", "0.0000001", "oops", "9223372036854.775808",
                      "1.00000000000000000000000000001"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.convert(f"time,x\n{value},2\n", "time")

    def test_generated_column_collision(self):
        for name in ["record_time", "record_time (us)", " record_time (us)"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                MODULE.convert(f"time,{name}\n1,2\n", "time")

    def test_reuse_with_explicit_alternate_column_and_exact_i64_limit(self):
        result = MODULE.convert("label,elapsed_s,extra\nfirst,0,not-a-number\nlast,9223372036854.775807,\n", "elapsed_s")
        rows = list(csv.reader(io.StringIO(result)))
        self.assertEqual(rows[1], ["0", "first", "0", "not-a-number"])
        self.assertEqual(rows[2], ["9223372036854775807", "last", "9223372036854.775807", ""])

    def test_unsupported_header_cells_are_rejected_before_target_reader(self):
        for header in ['time,"x,y"', 'time,"x\ny"', 'time,"x""y"']:
            with self.subTest(header=header), self.assertRaises(ValueError):
                MODULE.convert(header + "\n1,2\n", "time")

    def test_cli_alternate_column_and_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.csv", Path(directory) / "output.csv"
            source.write_text("elapsed,label\n0.000001,first\n0.000002,next\n")
            command = [sys.executable, str(SCRIPT), str(source), str(output), "--time-column", "elapsed"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            expected = "record_time (us),elapsed,label\n1,0.000001,first\n2,0.000002,next\n"
            self.assertEqual(output.read_text(), expected)
            output.write_text("existing output must survive\n")
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "existing output must survive\n")

    def test_cli_invalid_input_never_creates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.csv", Path(directory) / "output.csv"
            for text in ["time,x\n0.0000001,2\n", "time,x\n1,2\n1,3\n", "other,x\n1,2\n"]:
                source.write_text(text)
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), str(source), str(output), "--time-column", "time"],
                    capture_output=True, text=True, timeout=10,
                )
                with self.subTest(text=text):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
