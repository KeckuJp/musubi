"""Authored compact DataValue matrices; no live node or physical axis inference."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_opcua_recorded import convert
from tests.test_opcua_recorded import record

ROOT = Path(__file__).resolve().parents[1]


def matrix(values=None, dimensions=None, code=0):
    item = record([0, 1, 2, 3, 4, 5] if values is None else values, code)
    item["data_value"]["Dimensions"] = [2, 3] if dimensions is None else dimensions
    item["data_value"]["future"] = {"not_an_axis": "原値"}
    return item


class MatrixTests(unittest.TestCase):
    def test_selected_numeric_arrays_and_separate_matrix_reach_common(self):
        self.assertIn("MUSUBI_TELEMETRY_READER", os.environ)
        cases = [(6, [-2147483648, 2147483647], "A", "current_a", 1, False),
                 (7, [0, 4294967295], "Pa", "pressure_pa", 1, False),
                 (10, [1250.5, -250.25], "mV", "voltage_v", .001, False),
                 (6, [100, 200, 300, 400], "mm", "distance_m", .001, True)]
        for kind, values, unit, field, scale, two_d in cases:
            item = matrix(values, [2, 2]) if two_d else record(values)
            item["data_value"]["UaType"] = kind
            output, report = convert(json.dumps(item), item["node_id"], unit,
                                     arrays=not two_d, matrix2d=two_d, array_type=kind)
            self.assertEqual(report["converted_values"], len(values))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), item)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], len(values))
            for observation, value in zip(common["observations"], values):
                self.assertEqual(observation["fields"][field], value * scale)
                self.assertEqual(observation["clock_basis"], "Unknown")

    def test_numeric_array_boundaries_withholding_and_selection(self):
        for kind in (6, 7, 10):
            item = record([1, 2], 0x80000000); item["data_value"]["UaType"] = kind
            item["data_value"].pop("SourceTimestamp"); item["data_value"].pop("SourcePicoseconds")
            output, report = convert(json.dumps(item), item["node_id"], "V", arrays=True, array_type=kind)
            self.assertEqual(report["withheld_values"], 2)
            self.assertTrue(all(row["voltage_v"] == row["source_time_us"] == "" for row in csv.DictReader(io.StringIO(output))))
            for bad in (None, True, "NaN", [], 1e39 if kind == 10 else 2**32):
                item["data_value"]["Value"] = [bad]
                with self.assertRaises(ValueError):
                    convert(json.dumps(item), item["node_id"], "V", arrays=True, array_type=kind)
            item["data_value"]["Value"] = []
            self.assertEqual(convert(json.dumps(item), item["node_id"], "V", arrays=True, array_type=kind)[1]["empty_arrays"], 1)
        for kind, bad in ((6, -2**31-1), (7, -1), (6, 1.25), (7, 1.25)):
            item = record([bad]); item["data_value"]["UaType"] = kind
            with self.assertRaises(ValueError): convert(json.dumps(item), item["node_id"], "V", arrays=True, array_type=kind)
        item = record([1]); item["data_value"]["UaType"] = 6
        for options in ({"arrays": True}, {"arrays":True, "array_type":7}, {"array_type":6},
                        {"arrays":True, "array_type":8}, {"arrays":True, "array_type":True}):
            with self.assertRaises(ValueError): convert(json.dumps(item), item["node_id"], "V", **options)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "in.jsonl"; source.write_text(json.dumps(item))
            command = [sys.executable, str(ROOT / "scripts/convert_opcua_recorded.py"), str(source),
                       str(base / "out.csv"), str(base / "report.json"), "--node", item["node_id"], "--unit", "V"]
            subprocess.run(command + ["--arrays", "--array-ua-type", "6"], check=True, capture_output=True)
            self.assertEqual((base / "out.csv").read_text(), convert(source.read_text(), item["node_id"], "V", arrays=True, array_type=6)[0])
            self.assertNotEqual(subprocess.run(command + ["--arrays", "--array-ua-type", "6", "--source-format", "sensorthings-1.1-celsius"], capture_output=True).returncode, 0)

    def test_two_shapes_units_and_repeated_clock_reach_common(self):
        self.assertIn("MUSUBI_TELEMETRY_READER", os.environ)
        for unit, field, scale, offset in (("degC", "temperature_k", 1, 273.15), ("mV", "voltage_v", .001, 0)):
            items = [matrix([-10, 0, 1.5, 4, 6, 11]), matrix([25, 30, 40, 50, 60, 70], [3, 2])]
            text = "\n".join(json.dumps(item, ensure_ascii=False) for item in items)
            output, report = convert(text, items[0]["node_id"], unit, matrix2d=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["records"], report["arrays"], report["array_elements"], report["converted_values"]), (2, 2, 12, 12))
            self.assertEqual(report["matrix_shape"], "EXPLICIT_2D_LAST_DIMENSION_FASTEST_NOT_PHYSICAL_AXES")
            self.assertEqual([int(row["opcua_array_ordinal"]) for row in rows], [1] * 6 + [2] * 6)
            for row, item, index in zip(rows, [items[0]] * 6 + [items[1]] * 6, list(range(6)) * 2):
                d0, d1 = item["data_value"]["Dimensions"]
                self.assertEqual([int(row[k]) for k in ("opcua_dimension_0_length", "opcua_dimension_1_length",
                                  "opcua_dimension_0_index", "opcua_dimension_1_index")], [d0, d1, index // d1, index % d1])
                self.assertEqual(json.loads(bytes.fromhex(row["source_record_hex"][4:])), item)
                self.assertAlmostEqual(float(row[field]), item["data_value"]["Value"][index] * scale + offset)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 12)
            self.assertIn("not_physical", common["profile_units"]["opcua_dimension_0_index"])
            for observation, row in zip(common["observations"], rows):
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["t_boot_us"])
                self.assertIsNone(observation["anchor_unix_us"])
                self.assertEqual(observation["fields"]["record_time_us"], 15)
                self.assertEqual(observation["fields"]["source_10ps"], 4)
                self.assertAlmostEqual(observation["fields"][field], float(row[field]))

    def test_quality_withholds_every_element_and_shape_always_validated(self):
        for status in (1, 0x40000000, 0x80000000):
            item = matrix(code=status)
            item["data_value"].pop("SourceTimestamp")
            item["data_value"].pop("SourcePicoseconds")
            output, report = convert(json.dumps(item), item["node_id"], "V", matrix2d=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["converted_values"], report["withheld_values"]), (0, 6))
            self.assertTrue(all(r["voltage_v"] == r["source_time_us"] == "" for r in rows))
            self.assertTrue(all(r["value_disposition"] == "WITHHELD_STATUS_NOT_EXACT_GOOD" for r in rows))
            for bad in (None, True, "NaN", [], {}):
                item["data_value"]["Value"][0] = bad
                with self.assertRaises(ValueError): convert(json.dumps(item), item["node_id"], "V", matrix2d=True)

    def test_shape_bounds_selection_and_cli(self):
        for dims in ([], [6], [1, 2, 3], [0, 6], [-1, 6], [True, 6], [1.0, 6], ["1", 6], [2**32, 1], [2, 2], None):
            item = matrix()
            item["data_value"]["Dimensions"] = dims
            with self.subTest(dims=dims), self.assertRaises(ValueError):
                convert(json.dumps(item), item["node_id"], "V", matrix2d=True)
        item = matrix([0] * 512, [2, 256])
        self.assertEqual(convert(json.dumps(item), item["node_id"], "V", matrix2d=True)[1]["array_elements"], 512)
        for item in (matrix([0] * 513, [3, 171]), matrix([], [1, 1]), record([1, 2])):
            with self.assertRaises(ValueError): convert(json.dumps(item), item["node_id"], "V", matrix2d=True)
        item = matrix()
        for kwargs in ({"arrays":True}, {"boolean":True}, {"matrix2d":1}):
            with self.assertRaises(ValueError):
                convert(json.dumps(item), item["node_id"], "V", **({"matrix2d":True} | kwargs))
        with self.assertRaises(ValueError): convert(json.dumps(item), "different-node", "V", matrix2d=True)
        with self.assertRaises(ValueError): convert(json.dumps(item), item["node_id"], "V", arrays=True)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "in.jsonl"; source.write_text(json.dumps(item))
            command = [sys.executable, str(ROOT / "scripts/convert_opcua_recorded.py"), str(source),
                       str(base / "out.csv"), str(base / "report.json"), "--node", item["node_id"], "--unit", "V"]
            subprocess.run(command + ["--matrix2d"], check=True, capture_output=True)
            self.assertEqual((base / "out.csv").read_text(), convert(source.read_text(), item["node_id"], "V", matrix2d=True)[0])
            for flags in ([], ["--matrix2d", "--arrays"], ["--matrix2d", "--source-format", "sensorthings-1.1-celsius"]):
                self.assertNotEqual(subprocess.run(command + flags, capture_output=True).returncode, 0)

    def test_old_scalar_boolean_and_array_bytes_are_unchanged(self):
        cases = [(record(12.5), "V", {}), (record(True), None, {"boolean":True}),
                 (record([1.5, -2.25]), "V", {"arrays":True})]
        cases[1][0]["data_value"]["UaType"] = 1
        hashes = [("bbc76966dfe4d4367d3bd24d5582a6b926523409fb38bdf68b88afcc74623bdb", "a4bb31aabbddab5af4c206d5e35d941420b15c6f8b311a2763306727d91c895d"),
                  ("d4e83882077f884063eaabc80fd44771c0f58687a8b30c6b1a95971a454b4b1b", "a4bb31aabbddab5af4c206d5e35d941420b15c6f8b311a2763306727d91c895d"),
                  ("69f42f2a9982af25f349af2ee8077e592660ae268b67e184fd1a25cd848ea711", "a6cc2eb33853dff6edd94c875d92ab4ae110f0b975be9501ba14bcfefdfb8994")]
        for (item, unit, options), expected in zip(cases, hashes):
            output, report = convert(json.dumps(item), item["node_id"], unit, **options)
            self.assertEqual((hashlib.sha256(output.encode()).hexdigest(), hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()), expected)
