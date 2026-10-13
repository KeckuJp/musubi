"""Authored same-feature reported-time/quantity tuples, not real inspection proof."""
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

from scripts import convert_saved_inspection_features as target
from tests import test_saved_inspection_features as fixture


def convert(features):
    return target.convert(json.dumps(fixture.document(features)).encode(), fixture.CAPTURE_US,
                          reported_time_property="observed", **fixture.DECLARED)


def feature(stamp, value=3.5):
    return fixture.feature(identity="same-reported-id", properties={
        "observed": stamp, "crack_width_mm": value, "unknown": {"kept": True}})


class SavedInspectionTimeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_exact_reported_time_quantity_tuples_are_usable_from_common(self):
        for value in (3.5, 12.5):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                text, report = convert([
                    feature("2023-11-14T22:13:20.000001Z", value),
                    feature("2023-11-15T07:13:20.000001+09:00", value + 1),
                    feature("2023-11-14T22:13:19.999999Z", value + 2)])
                path = Path(directory) / "observations.csv"
                path.write_text(text)
                read = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(fixture.PROFILE), str(path), "--allow-equal-time"], capture_output=True)
                self.assertEqual(read.returncode, 0, read.stderr.decode())
                common = json.loads(read.stdout)
                self.assertEqual((common["main_rows"], common["platform_domain"]), (3, "Unknown"))
                observations = common["observations"]
                rows = [o["fields"] for o in observations]
                self.assertEqual([r["record_time_us"] for r in rows], [fixture.CAPTURE_US] * 3)
                self.assertEqual([r["reported_time_unix_us"] for r in rows],
                                 [1700000000000001, 1700000000000001, 1699999999999999])
                # This is the documented consumer operation: retain the source key, select the
                # explicit reported clock, then order by it. No capture-time/ID proximity join.
                series = sorted((int(r["reported_time_unix_us"]), int(r["feature_index"]),
                                 r["source_document_sha256"], r["reported_quantity_field"],
                                 r["reported_quantity_value"]) for r in rows)
                self.assertEqual([r[1] for r in series], [2, 0, 1])
                self.assertEqual(len({(r[2], r[1]) for r in series}), 3)
                self.assertEqual([r[3] for r in series], ["distance_m"] * 3)
                for r, expected in zip(rows, (value, value + 1, value + 2)):
                    self.assertAlmostEqual(r["reported_quantity_value"], expected / 1000)
                    self.assertEqual(r["property_count"], 3)
                    self.assertIn("NOT_TRUSTED", r["reported_time_basis"])
                self.assertEqual(fixture.unhex(rows[1]["reported_time_offset_hex"]), "+09:00")
                self.assertEqual(fixture.unhex(rows[0]["reported_time_text_hex"]),
                                 "2023-11-14T22:13:20.000001Z")
                for observation in observations:
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertIsNone(observation["anchor_unix_us"])
                self.assertIn("NOT_TRUSTED", common["profile_units"]["reported_time_unix_us"])
                self.assertEqual(report["reported_time_status_counts"], {"PRESENT": 3})
                self.assertEqual(report["features_read"], 3)

    def test_missing_null_unknown_and_empty_collection_are_accounted(self):
        absent = feature(None)
        del absent["properties"]["observed"]
        bare = dict(feature(None), properties=None)
        text, report = convert([absent, feature(None), bare, feature("1970-01-01T00:00:00Z")])
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([r["reported_time_status"] for r in rows],
                         ["ABSENT_PROPERTY", "NULL_PROPERTY", "NULL_PROPERTIES", "PRESENT"])
        self.assertEqual([r["reported_time_unix_us"] for r in rows], ["", "", "", "0"])
        self.assertEqual(sum(report["reported_time_status_counts"].values()), 4)
        self.assertEqual([fixture.unhex(x) for x in report["properties_not_interpreted"][0]
                          ["property_keys_not_interpreted"]], ["crack_width_mm", "unknown"])
        empty, empty_report = convert([])
        self.assertEqual(list(csv.DictReader(io.StringIO(empty))), [])
        self.assertEqual(empty_report["reported_time_status_counts"], {})

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_exact_numeric_boundary_and_missing_are_distinct_in_common(self):
        text, _ = convert([feature("2255-06-05T23:47:34.740991+00:00"),
                           feature(None), feature("1970-01-01T00:00:00Z")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            run = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(fixture.PROFILE),
                                  str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            values = [o["fields"]["reported_time_unix_us"]
                      for o in json.loads(run.stdout)["observations"]]
        self.assertEqual(values, [2**53 - 1, None, 0])
        with self.assertRaisesRegex(ValueError, "exact common"):
            convert([feature("2255-06-05T23:47:34.740992+00:00")])

    def test_important_invalid_declarations_refuse_without_fallback(self):
        for bad in ("", True, 1, [], {}, "2023-11-14T22:13:20", "2023-11-14T22:13:20-00:00",
                    "2023-11-14T22:13:20.0000001Z", "2023-02-30T00:00:00Z",
                    "2023-11-14T22:13:60Z", "2023-11-14T22:13:20+14:01",
                    "1969-12-31T23:59:59Z", "9999-01-01T00:00:00Z"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                convert([feature(bad)])
        for key in ("", True, 4, "x" * 513):
            with self.subTest(key=key), self.assertRaises(ValueError):
                target.convert(b'{"type":"FeatureCollection","features":[]}', 0,
                               reported_time_property=key)

    def test_cli_and_prior_default_bytes_are_preserved(self):
        raw = json.dumps(fixture.document()).encode()
        text, report = target.convert(raw, fixture.CAPTURE_US, **fixture.DECLARED)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                         "5118da25b90b6168ab5c3c50a286e0d6e72e06e438187d4d7eda3c14407ff8a3")
        self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
                         "0946c2fd4f9ff0b2ad4f756c7c6f0cf5dabfbcff35dd5a32c45056d7cb0fa913")
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.json", Path(directory) / "out"
            source.write_text(json.dumps(fixture.document([feature("2023-11-14T22:13:20Z")])))
            run = subprocess.run([sys.executable, str(fixture.ROOT / "scripts/convert_saved_inspection_features.py"),
                                  str(source), str(output), "--capture-time-us", "9",
                                  "--reported-time-property", "observed", "--quantity-property",
                                  "crack_width_mm", "--quantity-unit", "mm"], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            row = next(csv.DictReader(io.StringIO((output / "observations.csv").read_text())))
            self.assertEqual(row["reported_time_unix_us"], "1700000000000000")
            self.assertEqual(row["record_time_us"], "9")


if __name__ == "__main__":
    unittest.main()
