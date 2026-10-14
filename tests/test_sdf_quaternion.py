"""Declared local quaternion geometry, not a simulator or physical-airframe test."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_sdf_airframe import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/sdf-airframe/profile.toml"


def document(q, *, attributes='rotation_format="quat_xyzw" relative_to="local"'):
    return (f'<sdf version="1.11"><model name="m"><link name="body">'
        f'<visual name="a"><pose {attributes}>100 0 20 {q}</pose>'
        '<geometry><box><size>100 200 300</size></box></geometry></visual>'
        '<collision name="b"><geometry><sphere><radius>20</radius></sphere></geometry>'
        '</collision></link></model></sdf>').encode()


def selected(raw, **options):
    return convert(raw, 123, primitive_geometry=True, position_unit="cm",
        unit_basis="operator-declared", geometry_quaternion=True, **options)


class QuaternionGeometry(unittest.TestCase):
    def test_two_inputs_common_rotation_source_and_missing(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "mandatory actual common reader required")
        for text, expected in (("0 0 0.707107 0.707107", (0, 0, 2**-.5, 2**-.5)),
                               ("-1e308 1e308 1e308 -1e308", (-.5, .5, .5, -.5))):
            raw = document(text)
            csv_text, report = selected(raw)
            self.assertEqual(bytes.fromhex(report["source_xml_hex"][4:]), raw)
            self.assertEqual(report["selected_primitive_geometries"], 2)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "observations.csv"
                path.write_text(csv_text)
                result = json.loads(subprocess.run([reader, str(PROFILE), str(path),
                    "--allow-equal-time"], check=True, capture_output=True).stdout)
            geometries = [x for x in result["observations"]
                          if x["fields"].get("airframe_record_kind") == "GEOMETRY"]
            self.assertEqual(len(geometries), 2)
            obs = geometries[0]
            fields = obs["fields"]
            self.assertEqual(obs["clock_basis"], "Unknown")
            self.assertEqual(fields["airframe_geometry_pose_x_m"], 1)
            self.assertEqual(fields["airframe_geometry_frame_hex"], "hex:6c6f63616c")
            self.assertEqual(fields["airframe_box_z_m"], 3)
            for axis, value in zip("xyzw", expected):
                self.assertAlmostEqual(fields["airframe_geometry_q" + axis], value)
            self.assertAlmostEqual(math.hypot(*(fields["airframe_geometry_q" + a] for a in "xyzw")), 1)
            self.assertIn("normalized", result["profile_units"]["airframe_geometry_qw"])
            self.assertIsNone(fields["airframe_geometry_roll_rad"])
            self.assertEqual(geometries[1]["fields"]["airframe_geometry_frame_disposition"], "POSE_NOT_REPORTED")
            self.assertIsNone(geometries[1]["fields"]["airframe_geometry_qw"])
            with self.assertRaises(ValueError):
                convert(raw, 123, primitive_geometry=True, position_unit="cm", unit_basis="operator-declared")

    def test_refusals_euler_parity_and_cli(self):
        for q in ("0 0 0 0", "0 0 1", "0 0 NaN 1", "0 0 1e999 1", "0 0 1e-999 1", "1e-308 1e308 0 1"):
            with self.subTest(q=q), self.assertRaises(ValueError):
                selected(document(q))
        for attrs in ('rotation_format="quat_xyzw" degrees="true"',
                      'rotation_format="quat_wxyz"', 'rotation_format="quat_xyzw" extra="x"'):
            with self.subTest(attrs=attrs), self.assertRaises(ValueError):
                selected(document("0 0 0 1", attributes=attrs))
        with self.assertRaises(ValueError):
            selected(document("0 0 0 1").replace(b'1.11', b'1.5'))
        for flag in (1, "true"):
            with self.assertRaises(ValueError):
                convert(document("0 0 0 1"), 1, geometry_quaternion=flag)
        with self.assertRaises(ValueError):
            convert(document("0 0 0 1"), 1, geometry_quaternion=True)
        euler = document("0 0 90", attributes='degrees="true"')
        options = dict(primitive_geometry=True, position_unit="cm", unit_basis="operator-declared")
        self.assertEqual(convert(euler, 123, **options), convert(euler, 123, geometry_quaternion=False, **options))
        old, _ = convert(euler, 123, **options)
        new, _ = selected(euler)
        for before, after in zip(csv.DictReader(io.StringIO(old)), csv.DictReader(io.StringIO(new))):
            self.assertEqual(before, {k: after[k] for k in before})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sdf"
            source.write_bytes(document("0 0 0 2"))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_sdf_airframe.py"),
                str(source), str(Path(directory) / "out"), "--capture-time-us", "123",
                "--primitive-geometry", "--geometry-quaternion", "--position-unit", "cm",
                "--unit-basis", "operator-declared"], check=True, capture_output=True)
