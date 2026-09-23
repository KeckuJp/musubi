"""Independent synthetic contract cases for the whitespace pose bridge."""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "pose_converter", Path(__file__).resolve().parents[1] / "scripts/convert_pose_text.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PoseTextConversionContract(unittest.TestCase):
    def test_orbslam_saved_profile_reuses_pose_without_ground_or_world_inference(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("common reader path not supplied")
        root = Path(__file__).resolve().parents[1]
        cases = ["1305031102.175304 -1.250000000 2.000000000 3.000000000 0.000000000 0.000000000 0.000000000 1.000000000\n",
                 "1305031103.000001 4.000000000 -5.000000000 6.000000000 0.000000000 0.000000000 1.000000000 0.000000000\n"]
        for text in cases:
            converted, report = MODULE.convert(text + text, allow_equal_time=True)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pose.csv"; path.write_text(converted)
                common = json.loads(subprocess.run([reader,
                    str(root / "profiles/declared/pose-text/orbslam2-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(report["equal_time_pairs"], 1)
            for key, cell in zip(("tx", "ty", "tz", "qx", "qy", "qz", "qw"), text.split()[1:]):
                self.assertEqual(common["observations"][0]["fields"][key], float(cell))

    def test_exact_source_values_comment_accounting_and_no_normalization(self):
        text = "# source comment\n\n  # units are source assertions\n1509241629.863412 -1.25 +2 3e-2 -0.5 0 0 2\n \t\n1509241629.863413 4 5 6 0 0 0 1\n"
        result, report = MODULE.convert(text)
        rows = list(csv.reader(io.StringIO(result)))
        self.assertEqual(rows[0], ["record_time (us)", "timestamp_unix_s", "tx (m)", "ty (m)", "tz (m)", "qx", "qy", "qz", "qw"])
        self.assertEqual(rows[1], ["1509241629863412", "1509241629.863412", "-1.25", "+2", "3e-2", "-0.5", "0", "0", "2"])
        self.assertEqual(rows[2][0], "1509241629863413")
        self.assertEqual(report["data_rows"], 2)
        self.assertEqual(report["blank_lines"], 2)
        self.assertEqual(report["comment_lines"], 2)
        self.assertEqual(report["equal_time_pairs"], 0)
        self.assertEqual(report["comments"], ["# source comment", "  # units are source assertions"])

    def test_reuse_with_explicit_unknown_columns_retains_names_and_values(self):
        result, report = MODULE.convert("0 1 2 3 0 0 0 1 -7.500 2e-3\n0.000001 2 3 4 0 0 0 1 8 9\n", extra_columns=("source_score", "unknown_numeric"))
        rows = list(csv.reader(io.StringIO(result)))
        self.assertEqual(rows[0][-2:], ["source_score", "unknown_numeric"])
        self.assertEqual(rows[1][-2:], ["-7.500", "2e-3"])
        self.assertEqual([row[0] for row in rows[1:]], ["0", "1"])
        self.assertEqual(report["data_rows"], 2)

    def test_exact_i64_upper_limit(self):
        result, _ = MODULE.convert("9223372036854.775807 0 0 0 0 0 0 1\n")
        self.assertEqual(list(csv.reader(io.StringIO(result)))[1][0], "9223372036854775807")

    def test_malformed_or_wrong_format_inputs_fail(self):
        for text in ["", "\n # only comment\n", "1 2 3\n", "1 2 3 4 5 6 7 8 9\n", "1,2,3,4,5,6,7,8\n", '{"time":1,"pose":[2,3,4]}', "timestamp tx ty tz qx qy qz qw\n", "1 2 3 4 5 6 7 8 # inline\n", "1 2 3 4 5 6 7 8\n2 3\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                MODULE.convert(text)

    def test_all_numeric_fields_must_be_finite(self):
        for index in range(9):
            for bad in ["NaN", "Infinity", "-inf", "oops"]:
                row = ["1", "2", "3", "4", "0", "0", "0", "1", "5"]
                row[index] = bad
                with self.subTest(index=index, bad=bad), self.assertRaises(ValueError):
                    MODULE.convert(" ".join(row), extra_columns=("extra",))

    def test_unrepresentable_and_nonincreasing_times_fail(self):
        for time in ["-1", "0.0000001", "9223372036854.775808", "1.00000000000000000000000000001"]:
            with self.subTest(time=time), self.assertRaises(ValueError):
                MODULE.convert(f"{time} 0 0 0 0 0 0 1\n")
        for times in [("1", "1.000000"), ("2", "1")]:
            with self.subTest(times=times), self.assertRaises(ValueError):
                MODULE.convert("".join(f"{time} 0 0 0 0 0 0 1\n" for time in times))

    def test_extension_names_cannot_collide_or_require_csv_escaping(self):
        for names in [("duplicate", "duplicate"), ("timestamp_unix_s",), ("tx (m)",), ("qx",), ("record_time",), ("record_time (us)",), ("",), ("  ",), ("x,y",), ('x"y',), ("x\ny",), ("x\ry",)]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                MODULE.convert("1 0 0 0 0 0 0 1" + " 2" * len(names), extra_columns=names)

    def test_extension_count_is_explicit_and_exact(self):
        for text, names in [("1 0 0 0 0 0 0 1 9", ()), ("1 0 0 0 0 0 0 1", ("extra",))]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                MODULE.convert(text, extra_columns=names)

    def test_equal_time_requires_opt_in_and_retains_both_rows(self):
        text = "1 0 0 0 0 0 0 1\n1.000000 2 3 4 0 0 0 2\n"
        with self.assertRaises(ValueError):
            MODULE.convert(text)
        result, report = MODULE.convert(text, allow_equal_time=True)
        rows = list(csv.reader(io.StringIO(result)))
        self.assertEqual([row[0] for row in rows[1:]], ["1000000", "1000000"])
        self.assertEqual([row[2] for row in rows[1:]], ["0", "2"])
        self.assertEqual(report["equal_time_pairs"], 1)
        self.assertEqual(report["data_rows"], 2)
        with self.assertRaises(ValueError):
            MODULE.convert("2 0 0 0 0 0 0 1\n1 0 0 0 0 0 0 1\n", allow_equal_time=True)


if __name__ == "__main__":
    unittest.main()
