"""Authored fixed-writer folders, not real farm or device recordings."""
import csv
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import convert_agopen_field_folder as module


def field(lat=51, lon=4):
    return (f"2026-October-09 01:02:03 PM\n$FieldDir\nFieldNew\n$Offsets\n0,0\nConvergence\n0\nStartFix\n{lat},{lon}\n").encode()


class FolderTests(unittest.TestCase):
    def test_declared_drift_direction_inverse_and_invalid_boundaries(self):
        # At the equator the pinned polynomial has these exact decimal coefficient sums.
        self.assertEqual(module.declared_world(-2, -3, 0, 0, (2, 3)), (0, 0))
        for origin_lat, origin_lon, target_lat, target_lon, drift in (
                (0, 0, 1, 2, (2, 3)), (-34, 138, -33.99, 138.02, (-5, 7))):
            rad = math.radians(origin_lat)
            north = (target_lat - origin_lat) * (111132.92 - 559.82 * math.cos(2*rad)
                      + 1.175 * math.cos(4*rad) - .0023 * math.cos(6*rad)) - drift[0]
            rad = math.radians(target_lat)
            east = (target_lon - origin_lon) * (111412.84 * math.cos(rad)
                    - 93.5 * math.cos(3*rad) + .118 * math.cos(5*rad)) - drift[1]
            lat, lon = module.declared_world(north, east, origin_lat, origin_lon, drift)
            self.assertAlmostEqual(lat, target_lat, places=12)
            self.assertAlmostEqual(lon, target_lon, places=12)
        for values in ((0, 0, 90, 0, (0, 0)), (float("inf"), 0, 0, 0, (0, 0)),
                       (0, 1e9, 0, 180, (0, 0)), (0, 0, 0, 0, (True, 0))):
            with self.assertRaises(ValueError):
                module.declared_world(*values)

    def test_conditional_world_rows_common_and_default_local_unchanged(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader and Path(reader).is_file())
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "Field.txt").write_bytes(field(0, 0))
            (folder / "Boundary.txt").write_bytes(b"$Boundary\nFalse\n1\n-3,-2,0\n")
            local, _, report = module.convert_folder(folder, 1)
            self.assertEqual(report["geometry_transform"], "NOT_EVALUATED_DRIFT_NOT_SUPPLIED")
            converted, _, report = module.convert_folder(folder, 1, drift=(2, 3))
            self.assertEqual(converted["Boundary.txt.csv"], local["Boundary.txt.csv"])
            output = folder / "derived.csv"
            output.write_text(converted["Boundary.txt.world.csv"])
            common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/gpx-points/boundary-profile.toml"), str(output)], check=True, capture_output=True).stdout)
            values = common["observations"][0]["fields"]
            self.assertEqual((values["derived_latitude_deg"], values["derived_longitude_deg"]), (0, 0))
            self.assertEqual(values["easting_m"], -3)
            self.assertEqual(values["applied_drift_north_m"], 2)
            self.assertIn("CALLER_DECLARED", values["world_transform_basis"])
            for drift in ((0,), (0, float("nan"))):
                with self.assertRaises(ValueError):
                    module.convert_folder(folder, 1, drift=drift)

    def test_existing_empty_defaults_and_optin_keep_invalid_rejected(self):
        for func, raw in ((module.geometry.convert_boundary, b"$Boundary\n"),
                          (module.geometry.convert_track, b"$TrackLines\n")):
            with self.assertRaises(ValueError):
                func(raw, 1)
            text, report = func(raw, 1, allow_empty=True)
            self.assertEqual(list(csv.DictReader(io.StringIO(text))), [])
            for broken in (b"", raw + b"broken\n", b"wrong\n"):
                with self.assertRaises(ValueError):
                    func(broken, 1, allow_empty=True)
        nonempty = b"$Boundary\nFalse\n1\n1,2,0\n"
        self.assertEqual(module.geometry.convert_boundary(nonempty, 1),
                         module.geometry.convert_boundary(nonempty, 1, allow_empty=True))

    def test_two_folders_origin_association_and_empty_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader and Path(reader).is_file(), "postbuild reader is required")
        with tempfile.TemporaryDirectory() as directory:
            for index, (lat, lon) in enumerate(((51.5, -0.25), (-34.25, 138.75))):
                folder = Path(directory) / str(index)
                folder.mkdir()
                (folder / "Field.txt").write_bytes(field(lat, lon))
                for name, raw in (("Boundary.txt", b"$Boundary\n"), ("TrackLines.txt", b"$TrackLines\n"),
                                  ("Sections.txt", b""), ("RecPath.txt", b"$RecPath\n0\n")):
                    (folder / name).write_bytes(raw)
                if index == 1:
                    (folder / "Boundary.txt").write_bytes(b"$Boundary\nFalse\n1\n1,2,0\n")
                    (folder / "TrackLines.txt").write_bytes(b"$TrackLines\nAB\n0\n1,2\n3,4\n0\n2\nTrue\n0\n")
                    (folder / "Sections.txt").write_bytes(b"5\n1,2,3\n1,2,0\n3,4,0\n5,6,0\n7,8,0\n")
                    (folder / "RecPath.txt").write_bytes(b"$RecPath\n1\n1,2,0,3.6,True\n")
                outputs, originals, report = module.convert_folder(folder, 10)
                self.assertEqual(len(originals), 5)
                self.assertEqual(report["origin_source_sha256"], module.digest(field(lat, lon)))
                for name, text in outputs.items():
                    output = Path(directory) / (str(index) + name)
                    output.write_text(text)
                    if name == "Field.txt.csv":
                        profile = ROOT / "profiles/declared/agopen-field-folder/profile.toml"
                    else:
                        profile_name = report["artifacts"][name[:-4]]["profile"]
                        profile = ROOT / "profiles/declared" / (
                            profile_name if "/" in profile_name else "gpx-points/" + profile_name)
                    common = json.loads(subprocess.run([reader, str(profile), str(output), "--allow-empty-rows", "--allow-equal-time"],
                                                       capture_output=True, check=True).stdout)
                    expected = {"Field.txt.csv": 1, "Boundary.txt.csv": 1, "TrackLines.txt.csv": 2,
                                "Sections.txt.csv": 4, "RecPath.txt.csv": 1}
                    self.assertEqual(common["main_rows"], expected[name] if index else (1 if name == "Field.txt.csv" else 0))
                    if name == "Field.txt.csv":
                        fields = common["observations"][0]["fields"]
                        self.assertEqual(fields["declared_origin_latitude_deg"], lat)
                        self.assertEqual(fields["declared_origin_longitude_deg"], lon)
                        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")

    def test_missing_optional_and_invalid_origin_link_or_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "Field.txt").write_bytes(field())
            self.assertTrue(all(a["state"] == "NOT_SUPPLIED" for a in module.convert_folder(folder, 1)[2]["artifacts"].values()))
            for raw in (field(91, 2), field().replace(b"StartFix", b"Other"), b"StartFix\n1,2\n"):
                (folder / "Field.txt").write_bytes(raw)
                with self.assertRaises(ValueError):
                    module.convert_folder(folder, 1)
            (folder / "Field.txt").write_bytes(field())
            (folder / "Sections.txt").symlink_to(folder / "Field.txt")
            with self.assertRaises((OSError, ValueError)):
                module.convert_folder(folder, 1)
            (folder / "Sections.txt").unlink()
            os.mkfifo(folder / "Sections.txt")
            with self.assertRaises(ValueError):
                module.convert_folder(folder, 1)
            (folder / "Sections.txt").unlink()
            (folder / "Sections.txt").write_bytes(b"5\n")
            with self.assertRaises(ValueError):
                module.convert_folder(folder, 1)
