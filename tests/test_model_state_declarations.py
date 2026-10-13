"""Finite authored model/state declarations: code evidence, never simulator/physical evidence."""
import csv
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_model_state_declarations as module

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/model-state-declarations/profile.toml"


def model(version="1.11", mass="1.5"):
    inertial = "" if mass is None else f"<inertial><mass>{mass}</mass></inertial>"
    return (f"<sdf version='{version}'><model name='model'><link name='body'>{inertial}</link>"
            "<link name='other'/><include><uri>unresolved://saved</uri></include>"
            "<plugin name='not-run'/></model></sdf>").encode()


def declaration(sdf, state, **changes):
    result = dict(schema=module.SCHEMA, model_sha256=module.digest(sdf),
                  state_sha256=module.digest(state), model_name="model", link_name="body",
                  state_entity="reported-unit-A", state_child_frame="pose-body",
                  state_reference_frame="local-A", state_clock_id="experiment-clock-A",
                  run_id="attempt-2", citation="saved mapping note", capture_time_us=987654,
                  mass_unit="kg", unit_basis="operator-declared",
                  frame_relation=module.FRAME_RELATION, model_time_relation=module.MODEL_TIME)
    result.update(changes)
    return json.dumps(result).encode()


class ModelStateTests(unittest.TestCase):
    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not supplied")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            return json.loads(subprocess.run([reader, str(PROFILE), str(path),
                                               "--allow-equal-time"], check=True,
                                              capture_output=True).stdout)

    def test_two_versions_actual_common_and_exact_declared_correspondence(self):
        for version, mass, xyz in (("1.11", "1.5", "1 2 3"), ("1.5", None, "-4 0 6")):
            with self.subTest(version=version):
                source = model(version, mass)
                state = f"# 原記録\n\n9007199254.740993 {xyz} 0 0 0 2\n9007199254.740993 7 8 9 0 0 1 0".encode()
                manifest = declaration(source, state)
                result, report = module.convert(source, state, manifest)
                rows = list(csv.DictReader(io.StringIO(result)))
                common = self.common(result)
                self.assertEqual((common["main_rows"], common["platform_domain"]), (2, "Unknown"))
                self.assertEqual([r["source_state_row_index"] for r in rows], ["0", "1"])
                self.assertEqual(report["state_rows"], 2)
                self.assertEqual(report["state_report"]["equal_time_pairs"], 1)
                self.assertEqual(report["state_report"]["comment_lines"], 1)
                self.assertEqual(report["state_report"]["blank_lines"], 1)
                self.assertEqual(report["model_nonselected_rows"], 2)
                self.assertEqual(report["model_report"]["unselected_model_children"], 2)
                self.assertEqual(bytes.fromhex(report["source_state_hex"][4:]), state)
                self.assertEqual(bytes.fromhex(report["model_report"]["source_xml_hex"][4:]), source)
                self.assertEqual(bytes.fromhex(report["source_declaration_hex"][4:]), manifest)
                self.assertEqual(report["output_bytes"], len(result.encode()))
                self.assertEqual(report["output_sha256"], module.digest(result.encode()))
                for observation in common["observations"]:
                    self.assertEqual(observation["t_ms"], 9007199254740)
                    self.assertIsNone(observation["t_boot_us"])
                    self.assertEqual(observation["clock_basis"], "Unknown")
                fields = common["observations"][0]["fields"]
                for key, value in zip(("tx", "ty", "tz"), xyz.split()):
                    self.assertEqual(fields[key], float(value))
                self.assertEqual(fields["qw"], 2)  # never normalize the report
                self.assertEqual(fields["source_declaration_sha256"], module.digest(manifest))
                self.assertEqual(fields["state_time_exact_hex"], module.text_hex("9007199254.740993"))
                exact_seconds = bytes.fromhex(fields["state_time_exact_hex"][4:]).decode()
                self.assertEqual(Decimal(exact_seconds) * 1000000, 9007199254740993)
                self.assertEqual(rows[0]["record_time (us)"], "9007199254740993")
                self.assertEqual(fields["state_clock_id_hex"], module.text_hex("experiment-clock-A"))
                self.assertEqual(fields["model_capture_time_us_hex"], module.text_hex("987654"))
                self.assertIn("FILE_SCOPE_CALLER", fields["declaration_basis"])
                self.assertEqual(fields["frame_relation"], module.FRAME_RELATION)
                self.assertEqual(rows[0]["model_mass_disposition"], "REPORTED" if mass else "NOT_REPORTED")
                if mass:
                    self.assertEqual(fields["model_mass_kg"], 1.5)
                else:
                    self.assertEqual(rows[0]["model_mass_kg"], "")
                self.assertNotIn("airframe_com_x_m", fields)
                self.assertIn("not_world_inferred", common["profile_units"]["tx"])

    def test_declaration_failures_do_not_infer_identity_frame_or_clock(self):
        source, state = model(), b"0 0 0 0 0 0 0 1\n"
        for change in ({"model_sha256": "sha256:" + "0" * 64}, {"state_sha256": "sha256:" + "0" * 64},
                       {"model_name": "other"}, {"link_name": "included"}, {"state_entity": ""},
                       {"state_child_frame": ""}, {"state_reference_frame": " "}, {"state_clock_id": ""},
                       {"citation": ""}, {"frame_relation": "INFER_FROM_NAME"},
                       {"model_time_relation": "SAME_CLOCK"}, {"capture_time_us": True},
                       {"mass_unit": "g"}, {"unit_basis": "inferred"}, {"unknown": "not dropped"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.convert(source, state, declaration(source, state, **change))
        original = declaration(source, state)
        for invalid in (original[:-1] + b',"run_id":"duplicate"}', b"[]", b"{}"):
            with self.assertRaises(ValueError):
                module.convert(source, state, invalid)
        duplicate = source.replace(b"<link name='other'/>", b"<link name='body'/>")
        with self.assertRaises(ValueError):
            module.convert(duplicate, state, declaration(duplicate, state))
        # Same clock label alone never makes different runs the same binding.
        first, _ = module.convert(source, state, original)
        second, _ = module.convert(source, state, declaration(source, state, run_id="attempt-3"))
        a, b = [next(csv.DictReader(io.StringIO(value))) for value in (first, second)]
        self.assertNotEqual(a["source_declaration_sha256"], b["source_declaration_sha256"])
        self.assertEqual(a["state_clock_id_hex"], b["state_clock_id_hex"])

    def test_shapes_numeric_and_finite_bounds_refuse_whole(self):
        source = model()
        for state in (b"", b"# only", b"0 1 2", b"0 1 2 3 0 0 0 0", b"0 1e-999 0 0 0 0 0 1",
                      b"0 nan 0 0 0 0 0 1", b"0.0000001 0 0 0 0 0 0 1",
                      b"2 0 0 0 0 0 0 1\n1 0 0 0 0 0 0 1", b"0 0 0 0 0 0 0 1 99"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                module.convert(source, state, declaration(source, state))
        state = b"0 1 2 3 0 0 0 1\n0 4 5 6 0 0 0 1\n"
        manifest = declaration(source, state)
        # Cheap test-only cap overrides, production caps stay unchanged.
        for constant in ("STATE_LIMIT", "DECLARATION_LIMIT", "LINE_LIMIT", "ROW_LIMIT", "OUTPUT_LIMIT", "REPORT_LIMIT"):
            with self.subTest(constant=constant), patch.object(module, constant, 1), self.assertRaises(ValueError):
                module.convert(source, state, manifest)
        negative = model(mass="-1")
        result, _ = module.convert(negative, state, declaration(negative, state))
        row = next(csv.DictReader(io.StringIO(result)))
        self.assertEqual((row["model_reported_mass"], row["model_mass_kg"]), ("-1", ""))
        self.assertIn("NOT_A_POSITIVE", row["model_mass_disposition"])

    def test_cli_paired_outputs_and_no_overwrite(self):
        source, state = model(), b"1 1 2 3 0 0 0 1\n"
        manifest = declaration(source, state)
        expected, report = module.convert(source, state, manifest)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("model.sdf", "state.txt", "declaration.json")]
            for path, data in zip(paths, (source, state, manifest)):
                path.write_bytes(data)
            command = [os.sys.executable, str(ROOT / "scripts/convert_model_state_declarations.py"),
                       *map(str, paths), str(root / "output")]
            done = subprocess.run(command, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((root / "output/observations.csv").read_text(), expected)
            self.assertEqual(json.loads((root / "output/report.json").read_text()), report)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
