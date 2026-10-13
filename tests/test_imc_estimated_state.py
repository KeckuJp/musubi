"""Authored current IMC350 estimates, not a real UUV or a surface reference."""
import csv
import gzip
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import unittest

from scripts import convert_imc_sensors as c
from tests.test_imc_sensors import frame
from tests.saved_can_window_helpers import common

PROFILE = Path(__file__).resolve().parents[1] / "profiles/declared/imc-sensors/profile.toml"


def packet(values, order="<"):
    return frame(350, struct.pack(order + "dd18f", *values), order)


class EstimatedStateTests(unittest.TestCase):
    def test_two_inputs_common_gzip_and_native_window(self):
        for order, lat, north, depth, altitude in (("<", .5, -12, -1, 0), (">", -.7, 24, 5, -2)):
            values = [lat, 1.1, 34, north, 2, -3, .1, -.2, .3,
                      1, -2, 3, 4, -5, 6, .4, -.5, .6, depth, altitude]
            raw = frame(263, 12, order) + packet(values, order) + frame(65000, b"unknown", order)
            old = c.convert_input(raw)
            self.assertEqual(old, c.convert_input(raw, estimated_state=False))
            self.assertEqual([r["message_id"] for r in old[1]["unsupported_frames"]], [350, 65000])
            output, report = c.convert_input(raw, estimated_state=True)
            self.assertEqual((report["source_frames"], report["decoded_frames"]), (3, 2))
            self.assertEqual([r["message_id"] for r in report["unsupported_frames"]], [65000])
            self.assertEqual(output, c.convert_input(gzip.compress(raw, mtime=0), estimated_state=True)[0])
            row = list(csv.DictReader(io.StringIO(output)))[1]
            for name, value in zip(c.ESTIMATED_FIELDS, values):
                if name in ("estimated_depth_m", "estimated_altitude_m") and value < 0:
                    self.assertEqual(row[name], "")
                else:
                    self.assertAlmostEqual(float(row[name]), value, places=6)
            self.assertEqual(row["source_frame_hex"], "hex:" + packet(values, order).hex())
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                common_report = common(self, folder, output, PROFILE)
                obs = common_report["observations"][1]
                self.assertEqual(obs["clock_basis"], "Unknown")
                self.assertEqual(obs["fields"]["estimated_offset_north_m"], north)
                self.assertEqual(obs["fields"]["estimated_origin_latitude_rad"], lat)
                self.assertEqual(obs["fields"]["source_id"], 42)
                self.assertEqual(obs["fields"]["source_entity"], 3)
                self.assertEqual(obs["fields"]["estimated_depth_raw_m"], depth)
                self.assertIn("NOT_SURFACE", obs["fields"]["estimated_state_basis"])
                self.assertEqual(common_report["profile_units"]["estimated_offset_north_m"], "m_NED_north_offset_from_reported_WGS84_origin")
                source = folder / "source.lsf"; source.write_bytes(raw)
                window, selection = c.convert_path(source, start_record=1, stop_record=2, estimated_state=True)
                selected = next(csv.DictReader(io.StringIO(window)))
                self.assertEqual(selected["source_record_index"], "1")
                self.assertEqual(selected["source_file_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                self.assertEqual(common(self, folder, window, PROFILE)["observations"][0]["fields"]["estimated_offset_north_m"], north)

    def test_refusals(self):
        values = [0.] * 20
        for index, invalid in ((0, 2.), (1, 4.), (7, 2.), (15, 4.), (3, float("nan")), (19, float("inf"))):
            bad = values.copy(); bad[index] = invalid
            with self.subTest(index=index), self.assertRaises(ValueError):
                c.convert_input(packet(bad), estimated_state=True)
        for raw in (frame(350, b"short"), packet(values)[:-1], packet(values)[:-2] + b"\0\0"):
            with self.assertRaises(ValueError):
                c.convert_input(raw, estimated_state=True)
        for options in ({"estimated_state": 1}, {"estimated_state": True, "schema": "other-imc"}):
            with self.assertRaises(ValueError):
                c.convert_input(packet(values), **options)

    def test_legacy_selection_reuses_same_meaning_and_preserves_default(self):
        values = [.3, -.4, 10, -1, 2, 3, .1, .2, .3, 1, 2, 3, -4, 5, -6, .1, .2, .3, -1, 0]
        raw = frame(263, 20) + packet(values)
        legacy, report = c.convert_input(raw, schema=c.NEPTUS_SCHEMA)
        self.assertEqual(report["decoded_frames"], 1)
        self.assertEqual(report["unsupported_frames"][0]["message_id"], 350)
        self.assertNotIn("estimated_state_basis", legacy.splitlines()[0])
        current, _ = c.convert_input(raw, estimated_state=True)
        output, qualified = c.convert_input(raw, schema=c.NEPTUS_SCHEMA, estimated_state=True)
        self.assertEqual(output, current)
        self.assertEqual(qualified["selected_schema"], c.NEPTUS_SCHEMA)
        self.assertEqual(qualified["decoded_frames"], 2)
        self.assertEqual(output, c.convert_input(gzip.compress(raw, mtime=0), schema=c.NEPTUS_SCHEMA,
                                               estimated_state=True)[0])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.lsf"; source.write_bytes(raw)
            selected, selected_report = c.convert_path(source, start_record=1, stop_record=2,
                                                       schema=c.NEPTUS_SCHEMA, estimated_state=True)
            obs = common(self, folder, selected, PROFILE)["observations"][0]
            self.assertEqual(obs["fields"]["estimated_offset_north_m"], -1)
            self.assertEqual(obs["fields"]["estimated_depth_disposition"], "WITHHELD_NEGATIVE_INVALID_ESTIMATE")
            self.assertEqual(obs["fields"]["estimated_depth_raw_m"], -1)
            self.assertEqual(obs["clock_basis"], "Unknown")
            self.assertEqual(selected_report["selected_schema"], c.NEPTUS_SCHEMA)
        with self.assertRaises(ValueError):
            c.convert_input(raw, schema="other-imc", estimated_state=True)


if __name__ == "__main__":
    unittest.main()
