import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_image_flight_binding import convert, RELATION
from tests.test_image_metadata import second_export
from tests.test_m3m_capture import OBSERVED


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inputs(root, shift=0, kind="exif"):
    records = []
    for second in (10, 11, 12):
        entry = dict(OBSERVED) if kind == "m3m" else second_export()[0]
        entry.update({"EXIF:GPSDateStamp": "2026:01:01", "EXIF:GPSTimeStamp": f"00:00:{second:02}"})
        records.append(entry)
    raw = json.dumps(records).encode()
    mapping = json.dumps({"schema_version": 1, "export_sha256": sha(raw), "entries": [
        {"source_image_index": i, "path": f"{i}.jpg"} for i in range(1, 4)]}).encode()
    for i in range(1, 4):
        (root / f"{i}.jpg").write_bytes(f"authored image {shift}-{i}".encode())
    flight = f"TimeUS,Roll,Pitch,Yaw\n{100+shift},1,2,3\n{1000100+shift},2,3,4\n{3000100+shift},3,4,5\n".encode()
    anchor = 1767225610000000
    declaration = {"schema": "still-flight-clock-v1", "export_sha256": sha(raw), "mapping_sha256": sha(mapping),
        "flight_sha256": sha(flight), "session_id": f"session-{shift}", "camera_id": "caller-camera",
        "flight_entity_id": "caller-plane", "image_clock_id": "reported-gps", "flight_clock_id": "boot-us",
        "citation": "authored declared same event, not measured synchronization", "relation": RELATION,
        "anchor_image_index": 1, "anchor_flight_index": 1, "valid_image_start_us": anchor,
        "valid_image_end_us": anchor+3000000, "max_abs_delta_us": 3000000, "image_time_source": "gps-utc"}
    return raw, mapping, flight, declaration


class ImageFlightBindingTests(unittest.TestCase):
    def test_two_sessions_common_and_exact_sample_distinction(self):
        for shift, kind in ((0, "exif"), (987654, "m3m")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw, mapping, flight, declaration = inputs(root, shift, kind)
                text, report, profile, _, _ = convert(raw, mapping, root, flight, json.dumps(declaration).encode(), 0, kind=kind)
                rows = list(csv.DictReader(io.StringIO(text)))
                self.assertEqual([int(row["derived_flight_time_us"]) for row in rows], [100+shift,1000100+shift,2000100+shift])
                self.assertEqual(rows[0]["flight_connection_status"], "CLOCK_CONNECTED_EXACT_SAMPLE")
                self.assertEqual(rows[2]["flight_connection_status"], "CLOCK_CONNECTED_NO_EXACT_SAMPLE")
                self.assertEqual(rows[2]["flight_roll_rad"], "")
                self.assertEqual(report["flight_binding"]["connected_entries"], 3)
                self.assertTrue(all(row["record_time_us"] == "0" for row in rows))
                (root / "out.csv").write_text(text)
                (root / "out.toml").write_text(profile)
                reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(reader)
                run = subprocess.run([reader,str(root/"out.toml"),str(root/"out.csv"),"--allow-equal-time"],check=True,capture_output=True,text=True)
                obs = json.loads(run.stdout)["observations"]
                self.assertEqual(len(obs),3)
                self.assertTrue(all(item["clock_basis"] == "Unknown" for item in obs))

    def test_withheld_and_invalid_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, mapping, flight, declaration = inputs(root)
            declaration["valid_image_end_us"] = declaration["valid_image_start_us"] + 1000000
            text, report, *_ = convert(raw,mapping,root,flight,json.dumps(declaration).encode(),0)
            self.assertEqual(report["flight_binding"]["withheld_entries"],1)
            self.assertEqual(list(csv.DictReader(io.StringIO(text)))[2]["flight_connection_status"],"WITHHELD_DECLARED_INTERVAL")
            for key, value in (("flight_sha256","0"*64),("anchor_image_index",99),("relation","guess")):
                wrong = dict(declaration,**{key:value})
                with self.assertRaises(ValueError):
                    convert(raw,mapping,root,flight,json.dumps(wrong).encode(),0)

    def test_missing_clock_and_duplicate_sample_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, mapping, flight, declaration = inputs(root)
            photos = json.loads(raw)
            photos[2].pop("EXIF:GPSTimeStamp")
            raw = json.dumps(photos).encode()
            mapped = json.loads(mapping)
            mapped["export_sha256"] = sha(raw)
            mapping = json.dumps(mapped).encode()
            flight = flight.replace(b"3000100,", b"1000100,9,9,9\n3000100,")
            declaration.update(export_sha256=sha(raw), mapping_sha256=sha(mapping), flight_sha256=sha(flight))
            text, report, *_ = convert(raw,mapping,root,flight,json.dumps(declaration).encode(),0)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(rows[1]["flight_connection_status"],"CLOCK_CONNECTED_AMBIGUOUS_SAMPLE")
            self.assertEqual(rows[1]["flight_roll_rad"],"")
            self.assertEqual(rows[2]["flight_connection_status"],"WITHHELD_IMAGE_CLOCK_UNRESOLVED")
            self.assertFalse(report["flight_binding"]["all_entries_clock_connected"])
            declaration["anchor_image_index"] = 3
            with self.assertRaises(ValueError):
                convert(raw,mapping,root,flight,json.dumps(declaration).encode(),0)
