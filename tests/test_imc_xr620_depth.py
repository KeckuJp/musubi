"""Caller-bound fixed writer; authored IMC, no physical CTD qualification."""
import csv
import hashlib
import io
import tempfile
from pathlib import Path
import unittest

from scripts.convert_imc_sensors import XR620_PIN, convert_input, convert_path
from tests.test_imc_sensors import frame
from tests.saved_can_window_helpers import common

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/imc-sensors/profile.toml"


def declaration(data):
    return dict(driver="DUNE/Sensors/XR620CTD", source_pin=XR620_PIN,
                source_sha256=hashlib.sha256(data).hexdigest(), source_id=42,
                source_entity=3, session="authored-caller-session", citation="authored deployment assertion")


class Xr620DepthTests(unittest.TestCase):
    def test_two_sources_common_and_native_window_without_double_conversion(self):
        for order, value in (("<", 7.5), (">", -1.25)):
            data = frame(265, value, order) + frame(265, 0, order) + frame(65000, b"unknown", order)
            default = convert_input(data)
            output, report = convert_input(data, writer_declaration=declaration(data))
            self.assertEqual(convert_input(data), default)
            self.assertEqual(report["unsupported_frames"], default[1]["unsupported_frames"])
            self.assertEqual(report["xr620_writer_qualification"]["qualified_depth_rows"], 2)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(rows[0]["depth_m"]), value)
            self.assertEqual(rows[0]["source_frame_hex"], "hex:" + frame(265, value, order).hex())
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                report_common = common(self, folder, output, PROFILE)
                obs = report_common["observations"][0]
                self.assertEqual(obs["clock_basis"], "Unknown")
                self.assertEqual(obs["fields"]["depth_m"], value)
                self.assertEqual(obs["fields"]["depth_reference_pressure_pa"], 101325)
                self.assertIn("NOT_ACTUAL_SURFACE", obs["fields"]["depth_writer_basis"])
                source = folder / "source.lsf"
                source.write_bytes(data)
                selected, selection = convert_path(source, start_record=1, stop_record=2,
                                                   writer_declaration=declaration(data))
                selected_row = next(csv.DictReader(io.StringIO(selected)))
                self.assertEqual(float(selected_row["depth_m"]), 0)
                self.assertEqual(selected_row["source_record_index"], "1")
                self.assertEqual(selected_row["depth_source_sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
                self.assertEqual(selection["xr620_writer_qualification"]["qualified_depth_rows"], 1)

    def test_declaration_refusals(self):
        data = frame(265, 3)
        for key, value in (("driver", "Keller"), ("source_pin", "wrong"), ("source_id", 7),
                           ("source_entity", 9), ("source_id", True), ("session", ""),
                           ("source_sha256", "0" * 64)):
            invalid = declaration(data); invalid[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                convert_input(data, writer_declaration=invalid)
        invalid = declaration(data); del invalid["citation"]
        with self.assertRaises(ValueError):
            convert_input(data, writer_declaration=invalid)
        with self.assertRaises(ValueError):
            convert_input(data, schema="neptus-2019.10.1-sensors", writer_declaration=declaration(data))


if __name__ == "__main__":
    unittest.main()
