"""Authored exiftool-shaped metadata exports; no image was ever opened or captured."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_image_metadata import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/image-metadata/profile.toml"
CAPTURE_US = 1_700_000_000_000_000


def first_export():
    """Authored layout A: a southern/western fix below the reference surface, local-only
    time, a rotated orientation, and tags this converter does not interpret."""
    return [{
        "SourceFile": "/authored/survey/a0001.jpg",
        "ExifTool:ExifToolVersion": 13.59,
        "File:FileName": "a0001.jpg",
        "File:FileTypeExtension": "jpg",
        "EXIF:ExifImageWidth": 4000,
        "EXIF:ExifImageHeight": 3000,
        "EXIF:Orientation": 6,
        "EXIF:GPSLatitude": 33.865,
        "EXIF:GPSLatitudeRef": "S",
        "EXIF:GPSLongitude": 151.2094,
        "EXIF:GPSLongitudeRef": "W",
        "EXIF:GPSAltitude": 12.25,
        "EXIF:GPSAltitudeRef": 1,
        "EXIF:GPSMapDatum": "WGS-84",
        "EXIF:DateTimeOriginal": "2026:03:04 05:06:07",
        "EXIF:Make": "AuthoredOptics",
        "EXIF:Model": "Synthetic One",
        "EXIF:UserComment": "authored private note that must never be emitted",
        "MakerNotes:InternalSerialNumber": "authored-serial-not-emitted",
    }]


def second_export():
    """Authored layout B, reserved for reuse: different columns exercised — an explicit GPS
    UTC pair, an explicit zone offset, no altitude, conflicting size tags, orientation 1,
    and the version declared in its quoted form."""
    return [{
        "SourceFile": "/authored/second/b0001.jpg",
        "ExifTool:ExifToolVersion": "13.59",
        "File:FileName": "b0001.jpg",
        "File:ImageDataHash": "0123456789abcdef0123456789abcdef",
        "EXIF:ExifImageWidth": 1920,
        "EXIF:ExifImageHeight": 1080,
        "EXIF:ImageWidth": 1928,
        "EXIF:ImageHeight": 1080,
        "EXIF:Orientation": 1,
        "EXIF:GPSLatitude": 60.5,
        "EXIF:GPSLatitudeRef": "N",
        "EXIF:GPSLongitude": 5.25,
        "EXIF:GPSLongitudeRef": "E",
        "EXIF:GPSDateStamp": "2026:03:04",
        "EXIF:GPSTimeStamp": "05:06:07.500",
        "EXIF:DateTimeOriginal": "2026:03:04 14:06:07",
        "EXIF:OffsetTimeOriginal": "+09:00",
        "EXIF:Software": "authored-export-tool",
    }]


def rows_of(document, capture_time_us=CAPTURE_US):
    output, report = convert(json.dumps(document).encode(), capture_time_us)
    return list(csv.DictReader(io.StringIO(output))), report, output


def common(output, directory, name):
    path = Path(directory) / name
    path.write_text(output, encoding="utf-8")
    result = subprocess.run(
        [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(path), "--allow-equal-time"],
        check=True, capture_output=True)
    return json.loads(result.stdout)


class ImageMetadataTests(unittest.TestCase):
    def test_first_authored_export_keeps_reference_datum_and_local_time(self):
        rows, report, output = rows_of(first_export())
        row = rows[0]
        self.assertEqual(float(row["gps_latitude_deg"]), -33.865)
        self.assertEqual(float(row["gps_longitude_deg"]), -151.2094)
        self.assertEqual((row["gps_latitude_ref"], row["gps_longitude_ref"]), ("S", "W"))
        self.assertEqual(row["gps_coordinate_status"], "DECLARED_WITH_EXPLICIT_REFERENCE")
        self.assertEqual(float(row["gps_altitude_m"]), 12.25)
        self.assertEqual(row["gps_altitude_reference"], "SEA_LEVEL_REFERENCE_BELOW")
        self.assertEqual(row["gps_coordinate_datum_status"], "DECLARED_BY_RECEIVER_NOT_VERIFIED")
        self.assertEqual(bytes.fromhex(row["gps_map_datum_hex"][4:]).decode(), "WGS-84")
        self.assertEqual(row["datetime_original_text"], "2026:03:04 05:06:07")
        self.assertEqual(row["datetime_original_basis"], "LOCAL_WALL_TEXT_ZONE_UNKNOWN_NOT_UTC")
        self.assertEqual(row["gps_time_basis"], "NOT_DECLARED")
        self.assertEqual((row["gps_date_stamp_utc"], row["gps_time_of_day_utc"]), ("", ""))
        self.assertEqual((row["stored_width_px"], row["stored_height_px"]), ("4000", "3000"))
        self.assertEqual(row["stored_size_tag"], "PIXEL_X_Y_DIMENSION")
        self.assertEqual(row["stored_size_conflict"], "NONE")
        self.assertEqual(row["orientation_code"], "6")
        self.assertEqual(row["orientation_placement"], "ROW0_RIGHT_COL0_TOP")
        self.assertEqual(row["orientation_swaps_display_axes"], "1")
        self.assertEqual(bytes.fromhex(row["camera_model_hex"][4:]).decode(), "Synthetic One")
        self.assertEqual(row["camera_identity_basis"],
                         "DECLARED_CAMERA_STRING_NOT_A_PLATFORM_IDENTITY")
        self.assertEqual(int(row["unknown_tag_count"]), 2)
        retained = {entry["field_path"] for entry in report["retained_tags"][0]["not_interpreted"]}
        self.assertEqual(retained, {"EXIF:UserComment", "MakerNotes:InternalSerialNumber"})
        self.assertNotIn("authored private note", output)
        self.assertNotIn("authored-serial", output)
        self.assertNotIn("/authored/survey", output)
        self.assertEqual(bytes.fromhex(row["source_file_hex"][4:]).decode(),
                         "/authored/survey/a0001.jpg")
        self.assertIn("no image was opened or verified", report["digest_basis"])

    def test_second_authored_export_keeps_gps_utc_zone_and_size_conflict(self):
        rows, report, output = rows_of(second_export())
        row = rows[0]
        self.assertEqual(row["gps_date_stamp_utc"], "2026:03:04")
        self.assertEqual(row["gps_time_of_day_utc"], "05:06:07.500")
        self.assertEqual(row["gps_time_basis"], "EXIF_GPS_UTC_DECLARED")
        self.assertEqual(report["images_with_gps_utc"], 1)
        self.assertEqual(row["datetime_original_text"], "2026:03:04 14:06:07")
        self.assertEqual(row["datetime_original_basis"],
                         "LOCAL_WALL_TEXT_WITH_DECLARED_OFFSET_NOT_APPLIED")
        self.assertEqual(bytes.fromhex(row["datetime_original_zone_offset_text"][4:]).decode(),
                         "+09:00")
        self.assertEqual(row["stored_size_conflict"], "CONFLICTING_DECLARED_SIZES")
        self.assertEqual((row["stored_width_px"], row["tiff_image_width_px"]), ("1920", "1928"))
        self.assertEqual(row["orientation_swaps_display_axes"], "0")
        self.assertEqual((row["gps_altitude_m"], row["gps_altitude_ref_code"]), ("", ""))
        self.assertEqual(row["gps_altitude_reference"], "NOT_DECLARED")
        retained = {entry["field_path"] for entry in report["retained_tags"][0]["not_interpreted"]}
        self.assertIn("File:ImageDataHash", retained)
        self.assertNotIn("0123456789abcdef", output + json.dumps(report))

    def test_both_exports_reach_the_common_reader(self):
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            for name, document, expected in (("a.csv", first_export(), -33.865),
                                             ("b.csv", second_export(), 60.5)):
                output = rows_of(document)[2]
                result = common(output, directory, name)
                self.assertEqual(result["main_rows"], 1)
                self.assertEqual(result["platform_domain"], "Unknown")
                observation = result["observations"][0]
                self.assertEqual(observation["channel"], "video")
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["anchor_unix_us"])
                self.assertEqual(observation["fields"]["gps_latitude_deg"], expected)
                self.assertEqual(observation["fields"]["record_time_us"], CAPTURE_US)
                if name == "b.csv":
                    self.assertIsNone(observation["fields"]["gps_altitude_m"])

    def test_missing_malformed_and_unsupported_stay_explicit(self):
        bare = [{"SourceFile": "/authored/bare.jpg", "ExifTool:ExifToolVersion": 13.59}]
        row = rows_of(bare)[0][0]
        for column, value in (("gps_coordinate_status", "NOT_DECLARED"),
                              ("gps_altitude_reference", "NOT_DECLARED"),
                              ("gps_time_basis", "NOT_DECLARED"),
                              ("datetime_original_basis", "NOT_DECLARED"),
                              ("orientation_placement", "NOT_DECLARED"),
                              ("stored_size_tag", "NOT_DECLARED")):
            self.assertEqual(row[column], value)
        self.assertEqual((row["gps_latitude_deg"], row["stored_width_px"]), ("", ""))

        unsupported = first_export()
        unsupported[0].update({"EXIF:Orientation": 9, "EXIF:GPSAltitudeRef": 3})
        row = rows_of(unsupported)[0][0]
        self.assertEqual(row["orientation_code"], "9")
        self.assertEqual(row["orientation_placement"], "UNSUPPORTED_VALUE_AT_PIN")
        self.assertEqual(row["orientation_swaps_display_axes"], "")
        self.assertEqual(row["gps_altitude_ref_code"], "3")
        self.assertEqual(row["gps_altitude_reference"], "ALTITUDE_REFERENCE_UNSUPPORTED_AT_PIN")

        def refused(mutate):
            document = first_export()
            mutate(document[0])
            with self.assertRaises(ValueError):
                convert(json.dumps(document).encode(), CAPTURE_US)

        refused(lambda record: record.pop("EXIF:GPSLatitudeRef"))
        refused(lambda record: record.update({"EXIF:GPSLatitude": -33.865}))
        refused(lambda record: record.update({"EXIF:GPSLatitudeRef": "X"}))
        refused(lambda record: record.update({"EXIF:GPSLatitudeRef": "E"}))
        refused(lambda record: record.update({"EXIF:GPSLatitude": 91.0}))
        refused(lambda record: record.update({"Composite:GPSLatitude": 33.865}))
        refused(lambda record: record.pop("ExifTool:ExifToolVersion"))
        refused(lambda record: record.update({"EXIF:ExifImageWidth": 0}))
        refused(lambda record: record.pop("EXIF:ExifImageHeight"))
        refused(lambda record: record.update({"EXIF:DateTimeOriginal": "2026-03-04T05:06:07Z"}))
        refused(lambda record: record.update({"EXIF:GPSDateStamp": "2026:03:04",
                                              "EXIF:GPSTimeStamp": "25:00:00"}))
        refused(lambda record: record.update({"EXIF:GPSDateStamp": "2026:03:04",
                                              "EXIF:GPSTimeStamp": {"h": 5}}))
        refused(lambda record: record.update({"EXIF:GPSAltitudeRef": 0,
                                              "EXIF:GPSAltitude": None}))
        for malformed in (b"", b"{}", b"[]", b"[1]", b'[{"a":1}]', b"not json",
                          b'[{"SourceFile":"a\x00b"}]'):
            with self.assertRaises(ValueError):
                convert(malformed, CAPTURE_US)
        with self.assertRaises(ValueError):
            convert(json.dumps(first_export()).encode(), -1)

    def test_ordinary_exporter_shapes_are_read_not_refused(self):
        """ExifTool 13.59 GPS.pm settles what each coordinate key means: the recorded tag is
        an unsigned magnitude (`ToDegrees($val)` with no sign argument, Ref not consulted)
        and the Composite tag is already signed (`$val[1] =~ /^S/i ? -$val[0] : $val[0]`).
        So a flat export resolves from the sign and the reference together, and none of
        these ordinary shapes is refused."""
        southern = -33.865
        self.assertEqual(float(rows_of(first_export())[0][0]["gps_latitude_deg"]), southern)

        flat = [{key.split(":")[-1]: value for key, value in first_export()[0].items()}]
        row = rows_of(flat)[0][0]
        self.assertEqual(float(row["gps_latitude_deg"]), southern)
        self.assertEqual(row["gps_coordinate_status"], "DECLARED_WITH_EXPLICIT_REFERENCE")
        self.assertEqual(row["orientation_placement"], "ROW0_RIGHT_COL0_TOP")
        self.assertEqual(row["gps_altitude_reference"], "SEA_LEVEL_REFERENCE_BELOW")
        self.assertEqual(row["exiftool_version_declared"], "13.59")

        signed_flat = [dict(flat[0], GPSLatitude=southern, GPSLongitude=-151.2094)]
        self.assertEqual(float(rows_of(signed_flat)[0][0]["gps_latitude_deg"]), southern)

        with_composite = first_export()
        with_composite[0].update({"Composite:GPSLatitude": southern,
                                  "Composite:GPSLongitude": -151.2094})
        checked = rows_of(with_composite)
        self.assertEqual(float(checked[0][0]["gps_latitude_deg"]), southern)
        self.assertEqual(checked[0][0]["gps_coordinate_status"],
                         "DECLARED_WITH_EXPLICIT_REFERENCE")
        self.assertEqual(int(checked[0][0]["unknown_tag_count"]), 2)

        derived_only = first_export()
        for axis, value in (("Latitude", southern), ("Longitude", -151.2094)):
            derived_only[0].pop("EXIF:GPS" + axis)
            derived_only[0]["Composite:GPS" + axis] = value
        row = rows_of(derived_only)[0][0]
        self.assertEqual(float(row["gps_latitude_deg"]), southern)
        self.assertEqual(row["gps_coordinate_status"], "EXPORTER_DERIVED_SIGNED_FROM_REFERENCE")

        quoted = [dict(first_export()[0], **{"EXIF:GPSAltitude": "12.25"})]
        self.assertEqual(float(rows_of(quoted)[0][0]["gps_altitude_m"]), 12.25)

        for stamp in ("05:06:07", "05:06:07.5", "05:06:07.123456789"):
            timed = [dict(second_export()[0], **{"EXIF:GPSTimeStamp": stamp})]
            self.assertEqual(rows_of(timed)[0][0]["gps_time_of_day_utc"], stamp)

        signed_altitude = dict(flat[0], GPSAltitude=-12.25)
        row = rows_of([signed_altitude])[0][0]
        self.assertEqual(float(row["gps_altitude_m"]), 12.25)
        self.assertEqual(row["gps_altitude_reference"], "SEA_LEVEL_REFERENCE_BELOW")
        for contradiction in ([dict(first_export()[0], **{"EXIF:GPSAltitude": -12.25})],
                              [dict(signed_altitude, GPSAltitudeRef=0)]):
            with self.assertRaises(ValueError):
                convert(json.dumps(contradiction).encode(), CAPTURE_US)

    def test_seconds_dates_and_enums_are_exact_or_refused(self):
        """Counterexamples that used to pass silently: a rounded second that invented a
        minute, a fractional hour that was truncated away, a calendar that has no such day,
        and `1.9` read as the enumerated value `1`."""
        from scripts.convert_image_metadata import gps_time_of_day

        self.assertEqual(gps_time_of_day("23:59:59.999999"), ("23:59:59.999999", False))
        self.assertEqual(gps_time_of_day("00:00:00"), ("00:00:00", False))
        self.assertEqual(gps_time_of_day("5:6:7"), ("05:06:07", False))
        self.assertEqual(gps_time_of_day([1, 2, 3.123456]), ("01:02:03.123456", False))
        self.assertEqual(gps_time_of_day(["23", "59", "59.9999999999"]),
                         ("23:59:59.9999999999", False))

        stamp, leap = gps_time_of_day("23:59:60")
        self.assertEqual((stamp, leap), ("23:59:60", True))
        leap_row = [dict(second_export()[0], **{"EXIF:GPSTimeStamp": "23:59:60.25"})]
        row = rows_of(leap_row)[0][0]
        self.assertEqual(row["gps_time_of_day_utc"], "23:59:60.25")
        self.assertEqual(row["gps_time_basis"],
                         "EXIF_GPS_UTC_DECLARED_LEAP_SECOND_LEXICAL_NOT_NORMALIZED")

        for bad in ([1.5, 2.5, 3.123456], [1, 2.5, 3], ["1.5", "2", "3"]):
            with self.assertRaises(ValueError):
                gps_time_of_day(bad)
        for bad in ("24:00:00", "23:60:00", "23:59:61", "23:59", "1:2:3:4", "", [1, 2]):
            with self.assertRaises(ValueError):
                gps_time_of_day(bad)

        def refused(**tags):
            document = second_export()
            document[0].update(tags)
            with self.assertRaises(ValueError):
                convert(json.dumps(document).encode(), CAPTURE_US)

        for bad in ("2026:99:99", "2026:02:30", "2026:00:10", "2026:13:01", "2026:3:4",
                    "2026-03-04"):
            refused(**{"EXIF:GPSDateStamp": bad})
        refused(**{"EXIF:DateTimeOriginal": "2026:02:30 05:06:07"})
        refused(**{"EXIF:DateTimeOriginal": "2026:03:04 24:06:07"})
        refused(**{"EXIF:DateTimeOriginal": "2026:03:04 05:60:07"})
        local_leap = [dict(second_export()[0],
                           **{"EXIF:DateTimeOriginal": "2026:03:04 23:59:60"})]
        self.assertEqual(rows_of(local_leap)[0][0]["datetime_original_basis"],
                         "LOCAL_WALL_TEXT_WITH_DECLARED_OFFSET_NOT_APPLIED"
                         "_LEAP_SECOND_LEXICAL_NOT_NORMALIZED")
        good = [dict(second_export()[0], **{"EXIF:GPSDateStamp": "2028:02:29"})]
        self.assertEqual(rows_of(good)[0][0]["gps_date_stamp_utc"], "2028:02:29")

        for bad in ("1.9", "1.0", " 1", "+1", "", "one", 1.9, True, [1]):
            refused(**{"EXIF:GPSAltitude": 10, "EXIF:GPSAltitudeRef": bad})
            refused(**{"EXIF:Orientation": bad})
        quoted = [dict(second_export()[0], **{"EXIF:GPSAltitude": 10,
                                              "EXIF:GPSAltitudeRef": "1",
                                              "EXIF:Orientation": "6"})]
        row = rows_of(quoted)[0][0]
        self.assertEqual((row["gps_altitude_ref_code"], row["orientation_code"]), ("1", "6"))
        self.assertEqual(row["gps_altitude_reference"], "SEA_LEVEL_REFERENCE_BELOW")

    def test_only_the_qualified_exporter_release_is_read(self):
        """A version number is not a compatibility claim: only the release whose value
        conversions were read at the source is given those meanings."""
        self.assertEqual(rows_of(first_export())[0][0]["exiftool_version_declared"], "13.59")
        for accepted in (13.59, "13.59", "13.590"):
            document = [dict(first_export()[0], **{"ExifTool:ExifToolVersion": accepted})]
            self.assertEqual(float(rows_of(document)[0][0]["gps_latitude_deg"]), -33.865)
        for rejected in ("13.55", 13.6, "13", 14, "13.59.1", "v13.59", "", None, True, [13.59]):
            document = [dict(first_export()[0], **{"ExifTool:ExifToolVersion": rejected})]
            with self.assertRaises(ValueError):
                convert(json.dumps(document).encode(), CAPTURE_US)
        missing = first_export()
        missing[0].pop("ExifTool:ExifToolVersion")
        with self.assertRaises(ValueError):
            convert(json.dumps(missing).encode(), CAPTURE_US)

    def test_present_but_unused_aliases_are_cross_checked_or_listed(self):
        """A tag whose name happens to be known is not accounted for by that alone: it is
        either used, cross-checked, or listed as retained in the original export."""
        agreeing = [dict(first_export()[0], GPSLatitude=33.865)]
        rows, report, _ = rows_of(agreeing)
        self.assertEqual(float(rows[0]["gps_latitude_deg"]), -33.865)
        retained = {entry["field_path"] for entry in report["retained_tags"][0]["not_interpreted"]}
        self.assertNotIn("GPSLatitude", retained)
        with self.assertRaises(ValueError):
            convert(json.dumps([dict(first_export()[0], GPSLatitude=1.0)]).encode(), CAPTURE_US)

        with_derived = [dict(first_export()[0], **{"Composite:GPSAltitude": -12.25})]
        rows, report, _ = rows_of(with_derived)
        retained = {entry["field_path"]: entry["status"]
                    for entry in report["retained_tags"][0]["not_interpreted"]}
        self.assertEqual(retained.get("Composite:GPSAltitude"),
                         "RETAINED_IN_SOURCE_EXPORT_NOT_INTERPRETED")
        self.assertEqual(float(rows[0]["gps_altitude_m"]), 12.25)
        self.assertEqual(int(rows[0]["unknown_tag_count"]), 3)

        orphan = first_export()
        orphan[0].pop("EXIF:DateTimeOriginal")
        orphan[0]["EXIF:OffsetTimeOriginal"] = "+09:00"
        rows, report, output = rows_of(orphan)
        retained = {entry["field_path"] for entry in report["retained_tags"][0]["not_interpreted"]}
        self.assertIn("EXIF:OffsetTimeOriginal", retained)
        self.assertEqual(rows[0]["datetime_original_basis"], "NOT_DECLARED")
        self.assertEqual(rows[0]["datetime_original_zone_offset_text"], "")
        self.assertEqual(int(rows[0]["unknown_tag_count"]), len(retained))
        self.assertNotIn("value_sha256", output + json.dumps(report))
        self.assertNotIn("source_record_sha256", output + json.dumps(report))

    def test_sensitive_unknown_values_are_referenced_not_fingerprinted(self):
        """A digest of a comment is still a reusable fingerprint of it, and it preserves
        nothing. The supplied export plus the record index is the recovery reference."""
        document = first_export() + second_export()
        secrets = {
            "EXIF:UserComment": "token=AUTHORED-SECRET-VALUE",
            "MakerNotes:InternalSerialNumber": "AUTHORED-SERIAL-0001",
            "XMP:Description": "https://authored.invalid/private/path?k=AUTHORED-SECRET-VALUE",
        }
        document[0].update(secrets)
        raw = json.dumps(document).encode()
        output, report = convert(raw, CAPTURE_US)
        serialised = output + json.dumps(report)

        for name, value in secrets.items():
            self.assertNotIn(value, serialised, name + " value leaked")
            self.assertNotIn(value.encode().hex(), serialised)
            self.assertNotIn(hashlib.sha256(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True).encode()).hexdigest(), serialised)
            self.assertNotIn(hashlib.sha256(value.encode()).hexdigest(), serialised)
        self.assertNotIn(hashlib.sha256(
            json.dumps(document[0], ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True).encode()).hexdigest(), serialised)
        self.assertNotIn("source_record_sha256", serialised)

        entry = report["retained_tags"][0]
        self.assertEqual(entry["source_image_index"], 1)
        self.assertEqual(entry["recovery_reference"], "export_sha256 + source_image_index")
        retained = {item["field_path"]: item["status"] for item in entry["not_interpreted"]}
        for name in secrets:
            self.assertEqual(retained[name], "RETAINED_IN_SOURCE_EXPORT_NOT_INTERPRETED")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(int(rows[0]["unknown_tag_count"]), len(retained))
        self.assertEqual(report["unknown_tag_total"],
                         sum(int(row["unknown_tag_count"]) for row in rows))

        self.assertEqual(rows[0]["export_sha256"], hashlib.sha256(raw).hexdigest())
        original = json.loads(raw)[int(rows[0]["source_image_index"]) - 1]
        for name, value in secrets.items():
            self.assertEqual(original[name], value)

        self.assertEqual(bytes.fromhex(rows[0]["camera_make_hex"][4:]).decode(), "AuthoredOptics")
        self.assertEqual(bytes.fromhex(rows[0]["source_file_hex"][4:]).decode(),
                         "/authored/survey/a0001.jpg")
        self.assertEqual(float(rows[0]["gps_latitude_deg"]), -33.865)
        self.assertEqual(rows[1]["gps_time_basis"], "EXIF_GPS_UTC_DECLARED")

    def test_bounded_batch_and_cli(self):
        batch = []
        for index in range(250):
            record = dict(second_export()[0])
            record["SourceFile"] = "/authored/batch/%04d.jpg" % index
            record["EXIF:GPSLatitude"] = 60.5 + index / 1000
            batch.append(record)
        rows, report, output = rows_of(batch)
        self.assertEqual((len(rows), report["images"]), (250, 250))
        self.assertEqual(report["images_with_coordinates"], 250)
        self.assertEqual([int(row["source_image_index"]) for row in rows[:3]], [1, 2, 3])
        self.assertEqual(len({row["export_sha256"] for row in rows}), 1)
        self.assertEqual(len(report["retained_tags"]), 250)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "export.json"
            source.write_text(json.dumps(batch), encoding="utf-8")
            out = Path(directory) / "converted"
            subprocess.run(["python3", str(ROOT / "scripts/convert_image_metadata.py"),
                            str(source), str(out), "--capture-time-us", str(CAPTURE_US)],
                           check=True, capture_output=True, cwd=ROOT)
            self.assertEqual((out / "observations.csv").read_text(), output)
            self.assertEqual(json.loads((out / "report.json").read_text())["images"], 250)
            failed = subprocess.run(["python3", str(ROOT / "scripts/convert_image_metadata.py"),
                                     str(source), str(out), "--capture-time-us", "0"],
                                    capture_output=True, cwd=ROOT)
            self.assertEqual(failed.returncode, 2)
            self.assertIn(b"no successful conversion claim", failed.stderr)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                result = common(output, directory, "batch.csv")
                self.assertEqual(result["main_rows"], 250)
                self.assertEqual(len(result["observations"]), 250)


if __name__ == "__main__":
    unittest.main()
