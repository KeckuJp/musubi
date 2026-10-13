"""Authored SensorThings compact responses; no server request or sensor acquisition."""
import copy
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

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/sensorthings-dataarray/profile.toml"
LINK = "https://example.invalid/v1.1/Datastreams(7)"
PROPERTY = "urn:example:temperature"
TIME = "2026-01-01T00:00:00.123456Z"


def metadata():
    return {"@iot.id": 7, "@iot.selfLink": LINK,
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C", "definition": "http://unitsofmeasure.org/ucum.html#para-30"},
            "ObservedProperty": {"definition": PROPERTY}, "future": "元metadata"}


def group(rows, components=None, link=LINK):
    return {"Datastream@iot.navigationLink": link,
            "components": components or ["id", "phenomenonTime", "resultTime", "result"], "dataArray": rows}


class DataArrayTests(unittest.TestCase):
    def test_native_window_large_group_preserves_original_indices_and_common(self):
        source = {"value": [group([[i, TIME, None, i % 30] for i in range(10001)])]}
        with self.assertRaisesRegex(ValueError, "group bound"):
            self.convert(source)
        first, report = self.convert(source, start_row=0, stop_row=10000)
        second, last = self.convert(source, start_row=10000, stop_row=10001)
        self.assertEqual((report["native_rows"], report["selected_rows"], report["window_excluded_rows"]),
                         (10001, 10000, 1))
        self.assertEqual((last["selected_rows"], last["excluded_rows"]), (1, 10000))
        self.assertEqual(report["source_sha256"], last["source_sha256"])
        rows = list(csv.DictReader(io.StringIO(second)))
        self.assertEqual(rows[0]["dataarray_row_index"], "10000")
        self.assertEqual(rows[0]["source_index"], "10000")
        self.assertEqual(rows[0]["dataarray_global_row_index"], "10000")
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), source)
        self.assertEqual(len(self.common(first)["observations"]), 10000)
        self.assertEqual(self.common(second)["observations"][0]["fields"]["temperature_k"], 283.15)

    def test_window_typed_reuse_outside_values_and_default_parity(self):
        meta = metadata()
        meta["observationType"] = "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_TruthObservation"
        meta["unitOfMeasurement"] = {"name": None, "symbol": None, "definition": None}
        source = {"value": [group([["outside", "bad", {}, 9]], link=LINK+"other"),
                            group([[1, "uninterpreted", None, 9], [2, TIME, None, False]])]}
        output, report = self.convert(source, meta, result_kind="truth", start_row=2, stop_row=3)
        self.assertEqual((report["native_rows"], report["selected_rows"], report["excluded_rows"],
                          report["window_excluded_rows"]), (3, 1, 2, 1))
        row = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual((row["dataarray_group_index"], row["dataarray_row_index"], row["source_index"]),
                         ("1", "1", "2"))
        self.assertEqual(self.common(output)["observations"][0]["fields"]["truth_reported"], "false")
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "input.json").write_text(json.dumps(source))
            (folder / "metadata.json").write_text(json.dumps(meta))
            run = subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_sensorthings_dataarray.py"),
                                  str(folder / "input.json"), str(folder / "metadata.json"), str(folder / "out.csv"),
                                  "--report", str(folder / "report.json"), "--datastream-link", LINK,
                                  "--observed-property", PROPERTY, "--capture-time-us", "50",
                                  "--result-kind", "truth", "--start-row", "2", "--stop-row", "3"],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((folder / "out.csv").read_text(), output)
        small = {"value": [group([[1, TIME, None, 0]])]}
        default, _ = self.convert(small)
        explicit, _ = self.convert(small, start_row=0, stop_row=1)
        self.assertEqual(default, explicit)
        for start, stop in [(None, 1), (0, None), (True, 1), (-1, 1), (0, 0), (0, 10001), (0, 2)]:
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                self.convert(small, start_row=start, stop_row=stop)
        source["value"][0]["dataArray"][0].pop()
        with self.assertRaisesRegex(ValueError, "row width"):
            self.convert(source, meta, result_kind="truth", start_row=2, stop_row=3)

    def setUp(self):
        self.m = importlib.import_module("scripts.convert_sensorthings_dataarray")
        old = csv.field_size_limit(128 * 1024 * 1024)
        self.addCleanup(csv.field_size_limit, old)

    def convert(self, source, meta=None, **kwargs):
        return self.m.convert(json.dumps(source, ensure_ascii=False), json.dumps(metadata() if meta is None else meta),
                              datastream_link=LINK, property_definition=PROPERTY, capture_time_us=50, **kwargs)

    def common(self, output):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader: self.skipTest("actual common reader not configured")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "saved.csv"; path.write_text(output)
            run = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_order_null_period_unselected_empty_and_actual_common(self):
        source = {"@iot.count": 42, "@iot.nextLink": "https://example.invalid/never-fetch",
                  "value": [group([[1, TIME, None, -10], [2, TIME, TIME, None]]),
                            group([["raw", "not-a-time", {}, "uninterpreted"]], link=LINK+"other"),
                            group([[0, TIME+"/2026-01-02T00:00:00Z", 1, TIME, "未知"]],
                                  ["result", "phenomenonTime", "id", "resultTime", "future"]), group([])]}
        output, report = self.convert(source)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["temperature_k"] for r in rows], ["263.15", "", "273.15"])
        self.assertEqual([r["dataarray_group_index"] for r in rows], ["0", "0", "2"])
        self.assertEqual([r["dataarray_global_row_index"] for r in rows], ["0", "1", "3"])
        self.assertEqual(rows[2]["phenomenon_time_kind"], "PERIOD")
        self.assertEqual(rows[2]["dataarray_repeat_disposition"], "REPEATED_IN_DISTINCT_GROUP")
        self.assertEqual((report["native_rows"], report["selected_rows"], report["excluded_rows"], report["converted_values"], report["withheld_values"]), (4, 3, 1, 2, 1))
        self.assertEqual(report["empty_selected_groups"], [3])
        self.assertEqual(report["repeated_observation_ids"], 1)
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), source)
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["dataarray_metadata_hex"][4:])), metadata())
        self.assertTrue(all(not r["source_record_hex"] and not r["dataarray_metadata_hex"] for r in rows[1:]))
        self.assertEqual(rows[0]["source_sha256"], "sha256:"+hashlib.sha256(json.dumps(source, ensure_ascii=False).encode()).hexdigest())
        common = self.common(output)["observations"]
        self.assertEqual(len(common), 3)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common))
        self.assertIsNone(common[1]["fields"]["temperature_k"])
        self.assertEqual(common[-1]["fields"]["temperature_k"], 273.15)
        empty, empty_report = self.convert({"value": [group([])]})
        self.assertEqual((empty, empty_report["selected_rows"], empty_report["empty_selected_groups"]), ("", 0, [0]))

    def test_important_refusals_including_empty_and_outside_structure(self):
        valid = {"value": [group([[1, TIME, None, 0]])]}
        cases = [{"value": []}, {"value": [group([], link=LINK+"/")]},
                 {"value": [group([[1, TIME, None]])]},
                 {"value": [group([[1, TIME, None, 0]], ["id", "phenomenonTime", "result", "result"])]},
                 {"value": [group([[1, TIME, None, 0], [1, TIME, None, 1]])]},
                 {"value": [group([[1, "no-zone", None, 0]])]},
                 dict(valid, **{"@iot.nextLink": None}), dict(valid, **{"@iot.count": -1})]
        outside = copy.deepcopy(valid); outside["value"].append(group([[1]], link=LINK+"other")); cases.append(outside)
        for source in cases:
            with self.subTest(source=source), self.assertRaises(ValueError): self.convert(source)
        for key, value in (("@iot.selfLink", LINK+"/"), ("unitOfMeasurement", {}), ("ObservedProperty", {}), ("@iot.id", None)):
            meta = metadata(); meta[key] = value
            with self.subTest(metadata=key), self.assertRaises(ValueError): self.convert({"value": [group([])]}, meta)
        for raw in ('{"value":[],"value":[]}', "NaN", "[]"):
            with self.assertRaises(ValueError):
                self.m.convert(raw, json.dumps(metadata()), datastream_link=LINK, property_definition=PROPERTY, capture_time_us=50)
        for clock in (True, -1):
            with self.assertRaises(ValueError):
                self.m.convert(json.dumps(valid), json.dumps(metadata()), datastream_link=LINK, property_definition=PROPERTY, capture_time_us=clock)

    def test_capacity_shared_scalar_equality_bounds_and_cli(self):
        source = {"value": [group([[i, TIME, None, i % 10] for i in range(1000)])]}
        output, report = self.convert(source)
        common = self.common(output)["observations"]
        self.assertEqual((len(common), report["selected_rows"]), (1000, 1000))
        self.assertEqual(common[-1]["fields"]["dataarray_global_row_index"], 999)
        self.assertEqual(common[-1]["fields"]["temperature_k"], 282.15)
        from scripts import convert_opcua_recorded as old
        expanded = {"value": [{"@iot.id": 0, "phenomenonTime": TIME, "resultTime": None, "result": 0, "Datastream": metadata()}]}
        previous, _ = old.convert_sensorthings(json.dumps(expanded), PROPERTY, 50)
        old_row = next(csv.DictReader(io.StringIO(previous)))
        new_row = next(csv.DictReader(io.StringIO(output)))
        for key in ("temperature_k", "value_disposition", "phenomenon_time_us", "result_time_us", "observation_id_hex", "datastream_id_hex"):
            self.assertEqual(new_row[key], old_row[key], key)
        for key in ("MAX_INPUT_BYTES", "MAX_GROUPS", "MAX_COMPONENTS", "MAX_NATIVE_ROWS", "MAX_SELECTED_ROWS", "MAX_OUTPUT_BYTES"):
            with self.subTest(bound=key), patch.object(self.m, key, 0), self.assertRaises(ValueError): self.convert(source)
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder); a=p/"array.json"; b=p/"stream.json"; out=p/"out.csv"; rep=p/"report.json"
            a.write_text(json.dumps(source)); b.write_text(json.dumps(metadata()))
            args=[os.sys.executable, str(ROOT/"scripts/convert_sensorthings_dataarray.py"), str(a), str(b), str(out), "--report", str(rep), "--datastream-link", LINK, "--observed-property", PROPERTY, "--capture-time-us", "50"]
            run=subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            before=(out.read_bytes(), rep.read_bytes())
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
            self.assertEqual(before, (out.read_bytes(), rep.read_bytes()))


if __name__ == "__main__": unittest.main()
