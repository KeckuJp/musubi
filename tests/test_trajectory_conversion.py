"""Independent synthetic inputs for the trajectory CSV bridge."""
import csv
import importlib.util
import io
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "trajectory_converter", Path(__file__).resolve().parents[1] / "scripts/convert_trajectory_csv.py"
)
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


if __name__ == "__main__":
    unittest.main()
