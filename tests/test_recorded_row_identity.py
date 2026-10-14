"""Authored saved rows; new per-row binding, not a real experiment or authenticated entity."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.convert_trajectory_csv import convert as trajectory

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared"


class RecordedRowIdentity(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.convert_recorded_row_identity")

    def common(self, data, profile):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required for this connection evidence")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.csv"
            source.write_bytes(data)
            return json.loads(subprocess.run([reader, str(CASE / profile / "profile.toml"),
                str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)

    def test_paired_rows_and_quantities_reach_common_without_deduplication(self):
        data = trajectory("time,x_true,x_pred,feat_time_ms,run,entity,extra\n"
            "1,2,3,4,A,robot,kept\n1,5,6,7,B,robot,later\n"
            "2,8,9,10,A,,missing\n3,11,12,13,,unknown,last\n"
            "3,14,15,16,0,0,zero\n", "time", allow_equal_time=True).encode()
        output, report = self.module.convert(data, run_column="run", entity_column="entity",
                                            time_column="record_time (us)")
        rows = list(csv.DictReader(io.StringIO(output)))
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.assertEqual(report["rows"], 5)
        self.assertEqual((report["missing_run_rows"], report["missing_entity_rows"]), (1, 1))
        self.assertIn("extra", report["unselected_columns"])
        self.assertEqual([r["source_row_index"] for r in rows], list(map(str, range(5))))
        self.assertEqual([r["run_status"] for r in rows], ["PRESENT"] * 3 + ["EMPTY", "PRESENT"])
        self.assertTrue(all(r["bound_observations_sha256"] == digest for r in rows))
        quantities = self.common(data, "turtlebot3-trajectory")
        common = self.common(output.encode(), "recorded-row-identity")
        self.assertEqual((quantities["main_rows"], common["main_rows"]), (5, 5))
        self.assertEqual([r["fields"]["x_pred"] for r in quantities["observations"]], [3, 6, 9, 12, 15])
        self.assertEqual(quantities["profile_units"]["feat_time_ms"], "ms")
        fields = [r["fields"] for r in common["observations"]]
        self.assertEqual([r["reported_run_hex"] for r in fields],
                         ["hex:41", "hex:42", "hex:41", None, "hex:30"])
        self.assertEqual(fields[3]["reported_entity_hex"], "hex:756e6b6e6f776e")
        self.assertIsNone(fields[2]["reported_entity_hex"])
        self.assertEqual([r["source_row_index"] for r in fields], list(range(5)))
        self.assertTrue(all("NOT_AUTHENTICATED" in r["row_identity_basis"] for r in fields))
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_second_shape_cli_keeps_source_once_and_last_row(self):
        # Explicitly authored pose-compatible CSV, not a claim that native TUM has ID columns.
        data = ("record_time (us),tx (m),trial,subject,opaque\n" + "".join(
            f"{i},1.5,trial-{i % 2}, robot ,value-{i}\n" for i in range(1000))).encode()
        output, report = self.module.convert(data, run_column="trial", entity_column="subject",
                                            time_column="record_time (us)")
        self.assertEqual(report["rows"], 1000)
        self.assertEqual(self.common(data, "pose-text")["main_rows"], 1000)
        common = self.common(output.encode(), "recorded-row-identity")
        self.assertEqual(common["main_rows"], 1000)
        self.assertEqual(common["observations"][-1]["fields"]["source_row_index"], 999)
        self.assertEqual(common["observations"][-1]["fields"]["reported_entity_hex"], "hex:20726f626f7420")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "source.csv").write_bytes(data)
            command = [os.sys.executable, str(ROOT / "scripts/convert_recorded_row_identity.py"),
                str(base / "source.csv"), str(base / "out"), "--run-column", "trial",
                "--entity-column", "subject", "--time-column", "record_time (us)"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((base / "out/source.csv").read_bytes(), data)
            self.assertEqual((base / "out/identities.csv").read_text(), output)
            self.assertEqual(json.loads((base / "out/report.json").read_text()), report)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((base / "out/source.csv").read_bytes(), data)

    def test_bad_structure_roles_clock_and_resource_bounds_refuse(self):
        convert = self.module.convert
        good = b"record_time_us,run,entity\n0,A,B\n"
        for data in [b"", b"record_time_us,run,run\n0,A,B\n",
                     b"record_time_us,run,entity\n", b"record_time_us,run,entity\n0,A\n",
                     good + b"0,A,B,C\n", good + b"-1,A,B\n", good + b"0.1,A,B\n",
                     good + b"9223372036854775808,A,B\n",
                     good + b"1,A,B\n0,A,B\n", good.replace(b",A,", b',"A",'),
                     good.replace(b",A,", b",A\x00,"), good + b"\xff"]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                convert(data, run_column="run", entity_column="entity")
        for run, entity in [("absent", "entity"), ("run", "run"), ("record_time_us", "entity")]:
            with self.subTest(columns=(run, entity)), self.assertRaises(ValueError):
                convert(good, run_column=run, entity_column=entity)
        with self.assertRaises(ValueError):
            convert(good.replace(b",A,", ("," + "あ" * 86 + ",").encode()),
                    run_column="run", entity_column="entity")
        for name, value in [("OUTPUT_LIMIT", 1)]:
            with patch.object(self.module, name, value), self.assertRaises(ValueError):
                convert(good, run_column="run", entity_column="entity")
        for name, value in [("LIMIT", 1), ("ROW_LIMIT", 0), ("COLUMN_LIMIT", 2)]:
            with patch.object(self.module.binding, name, value), self.assertRaises(ValueError):
                convert(good, run_column="run", entity_column="entity")


if __name__ == "__main__":
    unittest.main()
