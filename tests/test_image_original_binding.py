"""Authored supplied bytes and existing authored metadata; not verified image extraction."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.convert_image_original_binding import convert
from tests.test_image_metadata import first_export, second_export
from tests.test_m3m_capture import OBSERVED


def mapping(raw, entries):
    return json.dumps({"schema_version": 1, "export_sha256": hashlib.sha256(raw).hexdigest(),
                       "entries": entries}).encode()


class ImageOriginalBindingTests(unittest.TestCase):
    def test_existing_semantics_and_two_layouts_reach_common(self):
        for kind, records in (("exif", first_export()), ("exif", second_export()), ("m3m", [OBSERVED])):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                image = b"authored supplied original bytes, not a captured image"
                (root / "a.tif").write_bytes(image)
                raw = json.dumps(records).encode()
                mapped = mapping(raw, [{"source_image_index": 1, "path": "a.tif"}])
                text, report, profile = convert(raw, mapped, root, 12, kind=kind)
                row = next(csv.DictReader(io.StringIO(text)))
                self.assertEqual(row["original_image_sha256"], hashlib.sha256(image).hexdigest())
                self.assertEqual(row["original_image_bytes"], str(len(image)))
                self.assertEqual(report["binding"]["bound_entries"], 1)
                self.assertEqual(row["original_binding_status"], "CALLER_ASSOCIATION_FILE_HASHED")
                if kind == "m3m":
                    self.assertEqual(row["band_name_reported"], "NIR")
                else:
                    self.assertIn("gps_latitude_deg", row)
                (root / "observations.csv").write_text(text)
                (root / "profile.toml").write_text(profile)
                reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(reader, "common reader required")
                run = subprocess.run([reader, str(root / "profile.toml"), str(root / "observations.csv"),
                                      "--allow-equal-time"], capture_output=True, text=True, check=True)
                result = json.loads(run.stdout)["observations"][0]["fields"]
                self.assertEqual(result["original_image_bytes"], len(image))
                self.assertEqual(result["original_image_sha256"], hashlib.sha256(image).hexdigest())

    def test_missing_unmapped_and_invalid_mappings(self):
        raw = json.dumps(first_export() + second_export()).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text, report, _ = convert(raw, mapping(raw, [{"source_image_index": 1, "path": "missing.jpg"}]), root, 0)
            self.assertEqual([row["original_binding_status"] for row in csv.DictReader(io.StringIO(text))],
                             ["MISSING_SUPPLIED_FILE", "UNMAPPED_EXPORT_ENTRY"])
            self.assertEqual(report["binding"]["missing_entries"], 1)
            self.assertEqual(report["binding"]["unmapped_entries"], 1)
            entry = {"source_image_index": 1, "path": "a"}
            for entries in ([entry, entry], [{"source_image_index": 3, "path": "a"}],
                            [{"source_image_index": 1, "path": "../escape"}]):
                with self.assertRaises(ValueError):
                    convert(raw, mapping(raw, entries), root, 0)
            with self.assertRaises(ValueError):
                convert(raw, mapping(b"[]", []), root, 0)

    def test_regular_only_limits_and_no_symlink(self):
        raw = json.dumps(first_export()).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image").write_bytes(b"0123456789")
            mapped = mapping(raw, [{"source_image_index": 1, "path": "image"}])
            with self.assertRaises(ValueError):
                convert(raw, mapped, root, 0, max_file_bytes=9)
            with self.assertRaises(ValueError):
                convert(raw, mapped, root, 0, max_total_bytes=9)
            (root / "link").symlink_to(root / "image")
            (root / "subdir").mkdir()
            os.mkfifo(root / "fifo")
            for path in ("link", "subdir", "fifo"):
                with self.assertRaises((ValueError, OSError)):
                    convert(raw, mapping(raw, [{"source_image_index": 1, "path": path}]), root, 0)
            original_read = os.read
            def change_during_read(fd, count):
                data = original_read(fd, count)
                if data:
                    (root / "image").write_bytes(b"changed bytes")
                return data
            with patch("scripts.convert_image_original_binding.os.read", side_effect=change_during_read):
                with self.assertRaises(ValueError):
                    convert(raw, mapped, root, 0)

    def test_cli_saves_binding_inputs_not_original_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "originals"
            root.mkdir()
            (root / "one.jpg").write_bytes(b"authored")
            raw = json.dumps(first_export()).encode()
            mapped = mapping(raw, [{"source_image_index": 1, "path": "one.jpg"}])
            (base / "export.json").write_bytes(raw)
            (base / "mapping.json").write_bytes(mapped)
            script = Path(__file__).resolve().parents[1] / "scripts/convert_image_original_binding.py"
            subprocess.run(["python3", str(script), str(base / "export.json"), str(base / "mapping.json"),
                            str(root), str(base / "output"), "--capture-time-us", "0"], check=True, capture_output=True)
            self.assertEqual((base / "output/source-export.json").read_bytes(), raw)
            self.assertEqual((base / "output/source-mapping.json").read_bytes(), mapped)
            self.assertFalse((base / "output/one.jpg").exists())
