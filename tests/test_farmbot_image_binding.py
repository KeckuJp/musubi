"""Authored resource/export pairs, not acquired FarmBot photographs."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_farmbot_image_binding import convert, SOURCE
from scripts import convert_image_metadata, convert_m3m_capture
from tests.test_image_metadata import first_export
from tests.test_m3m_capture import OBSERVED


def raw(value):
    return json.dumps(value).encode()


def pair(export, resource_id=7):
    resources = raw([dict(id=resource_id, device_id=4, created_at="2025-01-01T01:02:03Z",
                         updated_at="2025-01-01T01:03:04Z", attachment_processed_at=None,
                         attachment_url="https://example.invalid/image", meta={"x": 17, "future": "retained"},
                         future_field={"opaque": 3})])
    declaration = dict(schema_version=1, source_commit=SOURCE,
                       resources_sha256=hashlib.sha256(resources).hexdigest(),
                       export_sha256=hashlib.sha256(export).hexdigest(),
                       entries=[dict(source_image_index=1, resource_id=resource_id, device_id=4,
                                     path="image", original_sha256=hashlib.sha256(b"authored").hexdigest())])
    return resources, declaration


class FarmBotImageBindingTests(unittest.TestCase):
    def test_two_separate_export_pairs_reach_common(self):
        for kind, data in (("exif", first_export()), ("m3m", [OBSERVED])):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "image").write_bytes(b"authored")
                export = raw(data)
                resources, declaration = pair(export, 7 if kind == "exif" else 11)
                text, report, profile = convert(resources, export, raw(declaration), root, 12, kind=kind)
                row = next(csv.DictReader(io.StringIO(text)))
                base_converter = convert_image_metadata if kind == "exif" else convert_m3m_capture
                base_text, _ = base_converter.convert(export, 12)
                base_row = next(csv.DictReader(io.StringIO(base_text)))
                self.assertEqual({key: row[key] for key in base_row}, base_row)
                self.assertEqual(row["farmbot_resource_id"], str(declaration["entries"][0]["resource_id"]))
                self.assertEqual(row["farmbot_created_at"], "2025-01-01T01:02:03Z")
                self.assertEqual(report["farmbot"]["bound_resources"], 1)
                self.assertEqual(report["farmbot"]["source_resources"], 1)
                self.assertNotIn("meta_x_m", row)
                (root / "profile.toml").write_text(profile)
                (root / "observations.csv").write_text(text)
                reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(reader, "common reader required")
                run = subprocess.run([reader, str(root / "profile.toml"), str(root / "observations.csv"),
                                      "--allow-equal-time"], capture_output=True, text=True, check=True)
                fields = json.loads(run.stdout)["observations"][0]["fields"]
                self.assertEqual(fields["farmbot_resource_id"], declaration["entries"][0]["resource_id"])
                self.assertEqual(fields["farmbot_resources_sha256"], hashlib.sha256(resources).hexdigest())
                if kind == "m3m":
                    self.assertEqual(fields["band_name_reported"], "NIR")
                else:
                    self.assertIn("gps_latitude_deg", fields)

    def test_refuses_wrong_binding_and_missing_original(self):
        export = raw(first_export())
        resources, good = pair(export)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image").write_bytes(b"authored")
            variants = []
            for key in ("resources_sha256", "export_sha256", "source_commit"):
                value = json.loads(raw(good)); value[key] = "0" * 64; variants.append(value)
            for key, value in (("resource_id", 8), ("device_id", 9), ("source_image_index", 2),
                               ("original_sha256", "0" * 64), ("path", "missing")):
                item = json.loads(raw(good)); item["entries"][0][key] = value; variants.append(item)
            variants.extend([dict(good, entries=[]), dict(good, unknown=True)])
            for declaration in variants:
                with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                    convert(resources, export, raw(declaration), root, 12)
            broken = json.loads(resources); del broken[0]["created_at"]
            with self.assertRaises(ValueError):
                convert(raw(broken), export, raw(dict(good, resources_sha256=hashlib.sha256(raw(broken)).hexdigest())), root, 12)

    def test_cli_preserves_exact_inputs_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = base / "originals"; root.mkdir()
            (root / "image").write_bytes(b"authored")
            export = raw(first_export()); resources, declaration = pair(export)
            inputs = {"resources.json": resources, "export.json": export, "mapping.json": raw(declaration)}
            for name, data in inputs.items(): (base / name).write_bytes(data)
            script = Path(__file__).resolve().parents[1] / "scripts/convert_farmbot_image_binding.py"
            command = ["python3", str(script), *(str(base / name) for name in inputs), str(root),
                       str(base / "out"), "--capture-time-us", "12"]
            subprocess.run(command, check=True, capture_output=True)
            for name, data in inputs.items(): self.assertEqual((base / "out" / ("source-" + name)).read_bytes(), data)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
