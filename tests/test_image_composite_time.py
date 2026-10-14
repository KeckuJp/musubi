"""Authored fixed-exporter values, not photographed images or an Exif decoder run."""
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

from scripts.convert_image_metadata import convert
from tests.test_image_metadata import first_export, second_export, CAPTURE_US

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/image-metadata/profile.toml"
SOURCE = "exiftool-13.59"


def export(value, *, grouped=True):
    prefix = "Composite:" if grouped else ""
    return {"ExifToolVersion": 13.59, "SourceFile": "caller-supplied.jpg",
            "ExifImageWidth": 120, "ExifImageHeight": 80,
            prefix + "SubSecDateTimeOriginal": value}


class CompositeTime(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            return json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                             capture_output=True, check=True).stdout)

    def test_offsets_fraction_equal_moments_and_common_association(self):
        records = [export("2026:01:01 00:15:00.123456+00:30"),
                   export("2025:12:31 18:45:00.123456-05:00", grouped=False),
                   export("1970:01:01 00:00:00-00:00")]
        records[0].update({"EXIF:DateTimeOriginal": "2026:01:01 00:15:00",
                           "EXIF:OffsetTimeOriginal": "+0:30", "EXIF:SubSecTimeOriginal": "123456",
                           "MakerNotes:Private": "uninterpreted"})
        data = json.dumps(records).encode()
        output, report = convert(data, CAPTURE_US, composite_time=SOURCE)
        rows = list(csv.DictReader(io.StringIO(output)))
        # Exact fixed expected Unix microseconds; no float-derived expected value.
        self.assertEqual([row["image_capture_epoch_us"] for row in rows],
                         ["1767224700123456", "1767224700123456", "0"])
        self.assertEqual([row["source_image_index"] for row in rows], ["1", "2", "3"])
        self.assertEqual(rows[0]["datetime_original_basis"], "LOCAL_WALL_TEXT_WITH_DECLARED_OFFSET_NOT_APPLIED")
        self.assertEqual(report["composite_capture_time"]["derived_images"], 3)
        self.assertEqual(report["composite_capture_time"]["statuses"], {"EXPLICIT_OFFSET_DERIVED": 3})
        retained = [entry["field_path"] for entry in report["retained_tags"][0]["not_interpreted"]]
        self.assertIn("MakerNotes:Private", retained)
        self.assertIn("EXIF:SubSecTimeOriginal", retained)
        self.assertNotIn("Composite:SubSecDateTimeOriginal", retained)
        common = self.common(output)
        self.assertEqual(common["main_rows"], 3)
        for index, observation in enumerate(common["observations"]):
            self.assertEqual(observation["clock_basis"], "Unknown")
            fields = observation["fields"]
            self.assertEqual(fields["image_capture_epoch_us"], int(rows[index]["image_capture_epoch_us"]))
            self.assertEqual(fields["export_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(fields["stored_width_px"], 120)
            self.assertEqual(fields["image_capture_time_text_hex"], rows[index]["image_capture_time_text_hex"])

    def test_missing_zoneless_leap_and_grouped_alias_accounting(self):
        records = [export("2026:01:01 00:15:00.5"), export("2016:12:31 23:59:60+00:00"),
                   {"ExifToolVersion": 13.59, "EXIF:SubSecDateTimeOriginal": "lookalike-not-composite"}]
        records[0]["SubSecDateTimeOriginal"] = records[0]["Composite:SubSecDateTimeOriginal"]
        output, report = convert(json.dumps(records).encode(), CAPTURE_US, composite_time=SOURCE)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([row["image_capture_epoch_us"] for row in rows], ["", "", ""])
        self.assertEqual([row["image_capture_time_status"] for row in rows],
                         ["ZONE_NOT_REPORTED", "LEAP_SECOND_LEXICAL_NOT_NORMALIZED", "NOT_REPORTED"])
        self.assertEqual(report["composite_capture_time"]["derived_images"], 0)
        self.assertEqual(sum(report["composite_capture_time"]["statuses"].values()), 3)
        self.assertEqual(report["retained_tags"][0]["not_interpreted"], [])
        self.assertEqual(report["retained_tags"][2]["not_interpreted"][0]["field_path"], "EXIF:SubSecDateTimeOriginal")
        common = self.common(output)
        self.assertTrue(all(item["fields"]["image_capture_epoch_us"] is None for item in common["observations"]))
        self.assertTrue(all(item["clock_basis"] == "Unknown" for item in common["observations"]))

    def test_invalid_values_and_unchanged_default_with_cli(self):
        for value in (None, "", 12, "2026:01:01 00:00:00+1:00", "2026:01:01 00:00:00+14:01",
                      "2026:01:01 00:00:00+00:60", "2026:01:01 00:00:00+99:00",
                      "2026:02:30 00:00:00+00:00", "2026:01:01 24:00:00+00:00",
                      "2026:01:01 00:00:61+00:00", "2026:01:01 00:00:00.1234567+00:00",
                      "1969:12:31 23:59:59+00:00", "9999:01:01 00:00:00+00:00",
                      "2026:01:01 00:00:00Z", "2026:01:01 00:00:00+00:00tail"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert(json.dumps([export(value)]).encode(), CAPTURE_US, composite_time=SOURCE)
        for change in ({"ExifToolVersion": 13.55}, {"SubSecDateTimeOriginal": "2026:01:02 00:00:00+00:00"},
                       {"DateTimeOriginal": "2026:01:02 00:00:00"}):
            record = export("2026:01:01 00:00:00+00:00")
            record.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                convert(json.dumps([record]).encode(), CAPTURE_US, composite_time=SOURCE)
        with self.assertRaises(ValueError):
            convert(b"[]", CAPTURE_US, composite_time="Exif2.31")
        expected = [
            ("0a2672eb277813217b218ed0a12421f85bc1365881c69b1d49928c94f8b8f306", "2b8c1c0c7be663849ba9947941fd8d95e82ba80911c60d1e2a8d0799dcc7e6c1"),
            ("16b30aad1cabec7a6afa4d17a299c06dd01e6ad84428936af49edf3e100c0132", "3c2066576f14ea1d9247a60dfc484f720deebe80be4741e89cc6316df5391686")]
        for make, hashes in zip((first_export, second_export), expected):
            output, report = convert(json.dumps(make()).encode(), CAPTURE_US)
            self.assertEqual((hashlib.sha256(output.encode()).hexdigest(),
                              hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()), hashes)
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.json", Path(directory) / "out"
            source.write_text(json.dumps([export("2026:01:01 00:00:00+00:00")]))
            command = [sys.executable, str(ROOT / "scripts/convert_image_metadata.py"), str(source), str(target),
                       "--capture-time-us", str(CAPTURE_US), "--composite-time", SOURCE]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = (target / "observations.csv").read_bytes()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((target / "observations.csv").read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
