"""Authored saved declarations, not runtime ROS settings."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_rosbag_observations import convert_parameter_yaml, convert_map_metadata

ROOT = Path(__file__).resolve().parents[1]


class ParameterYamlTests(unittest.TestCase):
    def test_map_declaration_geometry_modes_and_common_output(self):
        for index, (mode, resolution, origin, negate) in enumerate((
                (None, .05, [-2., 3., .25], 0), ("ScAlE", .1, [8., -9., -1.5], True),
                ("raw", .2, [1., 2., 0.], -1))):
            text = f"image: missing-image.pgm\nresolution: {resolution}\norigin: {origin}\n"
            text += f"negate: {str(negate).lower()}\nfree_thresh: 0.2\noccupied_thresh: 0.65\n"
            text += "future: {nested: [1, 'unknown']}\n"
            if mode is not None:
                text += f"mode: {mode}\n"
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "map.yaml"; source.write_text(text)
                output, report = convert_map_metadata(source, 123 + index)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), text)
                self.assertEqual(row["map_mode"], (mode or "trinary").upper())
                self.assertEqual(row["map_mode_basis"], "DECLARED" if mode else "SCHEMA_DEFAULT")
                self.assertEqual(float(row["map_resolution_m_per_cell"]), resolution)
                self.assertEqual(float(row["map_origin_x_m"]), origin[0])
                self.assertEqual(float(row["map_origin_y_m"]), origin[1])
                self.assertEqual(float(row["map_origin_yaw_rad"]), origin[2])
                self.assertEqual(row["map_negate_reported"], str(int(bool(negate))))
                self.assertEqual(row["map_negate_used_by_mode"], "0" if mode == "raw" else "1")
                self.assertEqual(row["map_load_time_state"], "NOT_IN_YAML")
                self.assertEqual(row["map_image_reference_basis"], "YAML_DIRECTORY")
                self.assertEqual(report["output_records"], 1)
                self.assertEqual(report["clock"], "Unknown")
                self.assertFalse((Path(directory) / "missing-image.pgm").exists())
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    target = Path(directory) / "out.csv"; target.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target)], check=True, capture_output=True).stdout)
                    fields = common["observations"][0]["fields"]
                    self.assertEqual(fields["map_resolution_m_per_cell"], resolution)
                    self.assertEqual(fields["map_origin_yaw_rad"], origin[2])
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    self.assertEqual(common["profile_units"]["map_resolution_m_per_cell"], "m_per_cell_declared")
                destination = Path(directory) / "cli"
                command = [sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                    str(source), str(destination), "--map-metadata-only", "--capture-time-us", str(123 + index)]
                subprocess.run(command, check=True, capture_output=True)
                self.assertEqual((destination / "observations.csv").read_text(), output)
                self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_map_declaration_rejects_ambiguous_or_invalid_configuration(self):
        text = "image: /absent/map.pgm\nresolution: 0.1\norigin: [1, 2, 0]\nnegate: false\nfree_thresh: 0.2\noccupied_thresh: 0.65\n"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "map.yaml"
            for bad in (text.replace("0.1", "0"), text.replace("0.1", ".inf"),
                        text.replace("0.1", "0x10"), text.replace("[1, 2, 0]", "[1, 2]"),
                        text.replace("0.65", "0.1"), text.replace("false", "wrong"),
                        text.replace("false", str(2**31)), text.replace("0.2", "-1"),
                        text.replace("image: /absent/map.pgm\n", ""), text + "mode: future\n",
                        text + "resolution: 1\n", text + "future: &x [*x]\n"):
                source.write_text(bad)
                with self.subTest(bad=bad), self.assertRaises((ValueError, KeyError)):
                    convert_map_metadata(source, 1)
            source.write_text(text)
            output, _ = convert_map_metadata(source, 0)
            self.assertEqual(next(csv.DictReader(io.StringIO(output)))["map_image_reference_basis"], "ABSOLUTE_POSIX")
            for time in (None, -1, True, 2**63):
                with self.assertRaises(ValueError): convert_map_metadata(source, time)
            for extra in (["--parameters-only"], ["--metadata-only"], ["--topic", "/battery"]):
                destination = Path(directory) / "rejected"
                result = subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                    str(source), str(destination), "--map-metadata-only", "--capture-time-us", "1", *extra], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(destination.exists())

    def test_typed_declarations_reuse_and_scale(self):
        for count, integer in ((2000, -7), (2, 19)):
            text = "/robot:\n  node:\n    ros__parameters:\n"
            text += "".join(f"      p{i}: {integer}\n" for i in range(count))
            text += """      group:
        enabled: yes
        quoted: 'yes'
        octal: 010
        hex: 0x10
        decimal: 09
        floats: [1.25, 2e1]
        bools: [true, off]
        ints: [1, -2]
        strings: [one, 'no']
        absent_word: null
        nonfinite: -.Inf
        empty: ''
"""
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "params.yaml"; path.write_text(text)
                output, report = convert_parameter_yaml(path, 123)
                rows = list(csv.DictReader(io.StringIO(output)))
                byname = {}
                for row in rows:
                    name = bytes.fromhex(row["parameter_name_hex"][4:]).decode()
                    byname.setdefault(name, []).append(row)
                    self.assertEqual(row["parameter_event_kind"], "DECLARED_FILE")
                    self.assertEqual(row["parameter_source_stamp_us"], "")
                    self.assertEqual(bytes.fromhex(row["parameter_node_hex"][4:]).decode(), "/robot/node")
                self.assertEqual(len(rows), count + 16)
                self.assertEqual(byname["p0"][0]["parameter_integer_reported"], str(integer))
                self.assertEqual(byname["group.enabled"][0]["parameter_bool_reported"], "1")
                self.assertEqual(byname["group.quoted"][0]["parameter_type_reported"], "STRING")
                self.assertEqual(byname["group.octal"][0]["parameter_integer_reported"], "8")
                self.assertEqual(byname["group.hex"][0]["parameter_integer_reported"], "16")
                self.assertEqual(float(byname["group.decimal"][0]["parameter_double_reported"]), 9)
                self.assertEqual(byname["group.absent_word"][0]["parameter_text_hex"], "hex:6e756c6c")
                self.assertEqual(byname["group.nonfinite"][0]["parameter_value_status"], "NONFINITE_RETAINED")
                self.assertEqual(bytes.fromhex(report["source_yaml_hex"][4:]).decode(), text)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    out = Path(directory) / "out.csv"; out.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(out), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count + 16)
                    self.assertEqual(common["observations"][0]["fields"]["parameter_integer_reported"], integer)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                if count == 2:
                    destination = Path(directory) / "cli"
                    subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                        str(path), str(destination), "--parameters-only", "--capture-time-us", "123"],
                        check=True, capture_output=True)
                    self.assertEqual(json.loads((destination / "report.json").read_text())["output_records"], count + 16)

    def test_malformed_and_ambiguous_declarations_reject(self):
        prefix = "/node:\n  ros__parameters:\n"
        cases = [prefix + f"    p: {value}\n" for value in
                 ("[]", "[1, true]", "[[1]]", "9223372036854775808", "1e999", "1e-999", "1e-310", "", "!!str 1")]
        cases += [prefix + "    p: 1\n    p: 2\n", prefix + "    a.b: 1\n    a:\n      b: 2\n",
                  prefix + "    p: &x [1]\n    q: *x\n", "{}\n", "/node:\n  unrelated: 1\n"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p.yaml"
            for text in cases:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    convert_parameter_yaml(path, 1)
            path.write_text(prefix + "    p: 1\n")
            with self.assertRaises(ValueError): convert_parameter_yaml(path, None)

    def test_ordinary_file_shapes_convert_and_unreadable_ones_reject_as_value_errors(self):
        declarations = "/node:\n  ros__parameters:\n    p: 1\n    q: two\n"
        accepted = {"plain": declarations.encode(),
                    "crlf": declarations.replace("\n", "\r\n").encode(),
                    "utf8 bom": "\ufeff".encode() + declarations.encode(),
                    "comments": b"# saved by hand\n" + declarations.encode(),
                    "document markers": b"---\n" + declarations.encode() + b"...\n"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "p.yaml"
            for label, data in accepted.items():
                path.write_bytes(data)
                with self.subTest(accepted=label):
                    output, report = convert_parameter_yaml(path, 7)
                    rows = list(csv.DictReader(io.StringIO(output)))
                    self.assertEqual([bytes.fromhex(row["parameter_name_hex"][4:]).decode() for row in rows],
                                     ["p", "q"])
                    self.assertEqual(rows[0]["parameter_integer_reported"], "1")
                    self.assertEqual(bytes.fromhex(rows[1]["parameter_text_hex"][4:]).decode(), "two")
                    self.assertEqual(bytes.fromhex(rows[0]["parameter_node_hex"][4:]).decode(), "/node")
                    self.assertEqual(report["output_records"], 2)
                    self.assertEqual(report["declared_nodes"], 1)
            for label, data in (("tab indent", b"/node:\n\tros__parameters:\n\t  p: 1\n"),
                                ("unclosed flow", b"/node:\n  ros__parameters:\n    p: [1, 2\n"),
                                ("two documents", declarations.encode()
                                 + b"---\n/other:\n  ros__parameters:\n    z: 3\n"),
                                ("empty documents", b"---\n---\n" + declarations.encode()),
                                ("comment only", b"# nothing declared\n"),
                                ("latin-1 bytes", declarations.encode() + "    c: caf\xe9\n".encode("latin-1"))):
                path.write_bytes(data)
                with self.subTest(rejected=label), self.assertRaises(ValueError):
                    convert_parameter_yaml(path, 7)
            path.write_bytes(declarations.encode() + b"---\n/other:\n  ros__parameters:\n    z: 3\n")
            with self.assertRaisesRegex(ValueError, "one parameter YAML document required"):
                convert_parameter_yaml(path, 7)
            destination = Path(directory) / "cli"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/convert_rosbag_observations.py"),
                str(path), str(destination), "--parameters-only", "--capture-time-us", "7"],
                capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
