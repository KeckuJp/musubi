"""Saved declared geometry, never safety/topology or real vehicle qualification."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_electrical_schema_reuse import module, ROOT


def document():
    return dict(timestamp="2026-01-01T00:00:00.123456789Z", version="3.0.0",
        manufacturer="example", serialNumber="declared", physicalParameters=dict(
            minimumSpeed=.1, maximumSpeed=2., maximumAcceleration=.5,
            maximumDeceleration=-.7, minimumHeight=.2, maximumHeight=1., width=.8, length=1.2),
        typeSpecification=dict(maximumLoadMass=100), mobileRobotGeometry=dict(
            wheelDefinitions=[dict(type="FIXED", isActiveDriven=False, isActiveSteered=False,
                position=dict(x=-.5, y=.25, theta=-.2), diameter=.3, width=.1),
                dict(type="CUSTOM", isActiveDriven=True, isActiveSteered=True,
                    position=dict(x=1., y=-.25), diameter=.4, width=.2, centerDisplacement=.05)],
            envelopes2d=[dict(envelope2dId="same", vertices=[dict(x=-1., y=-2.),
                dict(x=2., y=-2.), dict(x=2., y=2.), dict(x=-1., y=-2.)]),
                dict(envelope2dId="same", vertices=[], description="")],
            envelopes3d=[dict(url="https://invalid.example/do-not-fetch", data={"opaque": True})],
            future={"retained": True}))


def convert(record):
    return module.convert(json.dumps(record), module.FACTSHEET_FORMAT, vda_geometry=True)


class VdaGeometryTests(unittest.TestCase):
    def test_geometry_common_order_missing_and_source(self):
        first = document()
        second = document()
        second["serialNumber"] = "different"
        second["mobileRobotGeometry"]["wheelDefinitions"][0]["position"]["x"] = -2.
        lines = [json.dumps(x) for x in (first, second)]
        result = module.convert("\n".join(lines), module.FACTSHEET_FORMAT, vda_geometry=True)
        rows = list(csv.DictReader(io.StringIO(result)))
        self.assertEqual(len(rows), 18)
        self.assertEqual([x["geometry_row_kind"] for x in rows[:9]],
                         ["SUMMARY", "WHEEL", "WHEEL", "ENVELOPE2D", "VERTEX", "VERTEX", "VERTEX", "VERTEX", "ENVELOPE2D"])
        self.assertEqual(rows[1]["geometry_center_displacement_reported_m"], "")
        self.assertEqual(float(rows[1]["geometry_center_displacement_effective_m"]), 0)
        self.assertEqual(rows[1]["geometry_center_displacement_basis"], "SPEC_DEFAULT_IF_ABSENT")
        self.assertEqual(rows[2]["geometry_theta_rad"], "")
        self.assertEqual(rows[2]["geometry_wheel_type_status"], "EXTENSIBLE_UNRECOGNIZED")
        self.assertEqual(rows[1]["geometry_active_driven"], "0")
        self.assertEqual(rows[3]["geometry_id_hex"], rows[8]["geometry_id_hex"])
        self.assertEqual(rows[3]["geometry_item_index"], "0")
        self.assertEqual(rows[8]["geometry_item_index"], "1")
        self.assertEqual(rows[3]["geometry_description_hex"], "")
        self.assertEqual(rows[8]["geometry_description_hex"], "hex:")
        self.assertEqual(rows[0]["geometry_unconverted_3d_count"], "1")
        self.assertEqual(rows[0]["geometry_unknown_key_count"], "1")
        self.assertEqual([float(x["geometry_x_m"]) for x in rows[4:8]], [-1, 2, 2, -1])
        for index, row in enumerate(rows):
            record_index = index // 9
            self.assertEqual(row["geometry_source_record_index"], str(record_index))
            self.assertEqual(row["geometry_source_sha256"], "sha256:" + hashlib.sha256(lines[record_index].encode()).hexdigest())
            self.assertEqual(row["source_record_hex"], "hex:" + lines[record_index].encode().hex() if index % 9 == 0 else "")
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not configured")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "geometry.csv"
            path.write_text(result)
            report = json.loads(subprocess.run([reader,
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(report["main_rows"], 18)
        observations = report["observations"]
        self.assertTrue(all(x["clock_basis"] == "Unknown" for x in observations))
        self.assertEqual(observations[10]["fields"]["geometry_x_m"], -2.)
        self.assertEqual(observations[4]["fields"]["geometry_frame_basis"], "ENVELOPE_FRAME_UNSPECIFIED")
        self.assertEqual(observations[1]["fields"]["geometry_frame_basis"], "DECLARED_MOBILE_ROBOT_FRAME")

    def test_absent_empty_default_and_cli(self):
        first = document()
        legacy = module.convert(json.dumps(first), module.FACTSHEET_FORMAT)
        self.assertEqual(hashlib.sha256(legacy.encode()).hexdigest(),
                         "4aac83e8727888cfc6da68198d8b1a985b1ca21fd246b08ab25e6914e89de3d1")
        self.assertNotIn("geometry_row_kind", legacy.splitlines()[0])
        self.assertEqual(legacy, module.convert(json.dumps(first), module.FACTSHEET_FORMAT, vda_geometry=False))
        for geometry, status, count in ((None, "ABSENT", ""), ({}, "PRESENT", ""),
                                       ({"wheelDefinitions": [], "envelopes2d": []}, "PRESENT", "0")):
            value = document()
            if geometry is None:
                del value["mobileRobotGeometry"]
            else:
                value["mobileRobotGeometry"] = geometry
            rows = list(csv.DictReader(io.StringIO(convert(value))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["geometry_status"], status)
            self.assertEqual(rows[0]["geometry_wheel_count"], count)
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "source.jsonl", Path(tmp) / "new.csv"
            source.write_text(json.dumps(first))
            cmd = [os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                   str(source), str(out), "--source-format", module.FACTSHEET_FORMAT, "--vda-geometry"]
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertEqual(out.read_text(), convert(first))
            before = out.read_bytes()
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
            self.assertEqual(out.read_bytes(), before)

    def test_shape_finite_and_bounded_refusals(self):
        mutations = [lambda g: g.update(wheelDefinitions=None),
            lambda g: g["wheelDefinitions"][0].update(isActiveDriven=0),
            lambda g: g["wheelDefinitions"][0].update(diameter=-1),
            lambda g: g["wheelDefinitions"][0]["position"].pop("theta"),
            lambda g: g["wheelDefinitions"][0]["position"].update(x=True),
            lambda g: g["wheelDefinitions"][0]["position"].update(x=float("inf")),
            lambda g: g["envelopes2d"][0]["vertices"][0].update(y=None),
            lambda g: g["envelopes2d"][0].update(description=3),
            lambda g: g.update(wheelDefinitions=[g["wheelDefinitions"][0]] * 1025),
            lambda g: g["envelopes2d"][0].update(vertices=[dict(x=1., y=2.)] * 4097),
            lambda g: g.update(envelopes2d=[g["envelopes2d"][1]] * 1025),
            lambda g: g.update(envelopes3d=[{}] * 1025)]
        for change in mutations:
            with self.subTest(change=mutations.index(change)):
                value = document()
                change(value["mobileRobotGeometry"])
                with self.assertRaises(ValueError):
                    convert(value)
        for flag in (1, "true", None):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(document()), module.FACTSHEET_FORMAT, vda_geometry=flag)
        with self.assertRaises(ValueError):
            module.convert("{}", "vda-state-3.0", vda_geometry=True)
        with self.assertRaises(ValueError):
            module.convert('{"version":"3.0.0","version":"3.0.0"}', module.FACTSHEET_FORMAT, vda_geometry=True)
        # Exactly the selected extreme vertex count is preserved, not truncated.
        value = document()
        value["mobileRobotGeometry"] = {"envelopes2d": [{"envelope2dId": "large",
            "vertices": [dict(x=1., y=2.)] * 4096}]}
        converted = convert(value)
        previous_limit = csv.field_size_limit(2 * 512 * 1024 + 4)
        try:
            self.assertEqual(len(list(csv.DictReader(io.StringIO(converted)))), 4098)
        finally:
            csv.field_size_limit(previous_limit)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("large actual common reader not configured")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.csv"
            path.write_text(converted)
            report = json.loads(subprocess.run([reader,
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(report["main_rows"], 4098)
        observations = report["observations"]
        expected_sha = "sha256:" + hashlib.sha256(json.dumps(value).encode()).hexdigest()
        self.assertTrue(all(x["fields"]["geometry_source_sha256"] == expected_sha for x in observations))
        self.assertEqual(observations[0]["fields"]["source_record_hex"], "hex:" + json.dumps(value).encode().hex())
        self.assertEqual(observations[-1]["fields"]["geometry_vertex_index"], 4095)
        self.assertEqual(observations[-1]["fields"]["geometry_source_record_index"], 0)
        self.assertEqual(observations[-1]["fields"]["geometry_y_m"], 2.)


if __name__ == "__main__":
    unittest.main()
