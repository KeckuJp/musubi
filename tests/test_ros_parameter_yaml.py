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


if __name__ == "__main__":
    unittest.main()
