"""Saved authored native replies reuse the adopted canvas, not a physical display."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_crsf_link as native
from tests.test_displayport_canvas import reply
from tests.test_msp_stream import attitude

ROOT = Path(__file__).resolve().parents[1]


class NativeDisplayPortTests(unittest.TestCase):
    def test_selected_canvas_original_binding_and_second_input_common(self):
        for glyph in (65, 128):
            frames = [attitude(), reply([2]), reply([3, 0, 1, 0x41, glyph]), reply([4]), reply([4])]
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "capture.bin"; path.write_bytes(b"idle" + b"".join(frames) + b"$M")
                operations, canvas, report = native.convert_native_file(path, native.MSP_DISPLAY_SERIAL_KIND,
                    capture_time_us=123, start_packet=0, stop_packet=4, canvas_geometry=(2, 3))
                self.assertEqual(report["decoded_records"], 3)
                self.assertEqual(len(report["unsupported_records"]), 1)
                self.assertEqual(report["native_stream"]["unselected_framing_records"], 1)
                self.assertEqual(report["native_stream"]["discarded_bytes"], 6)
                rows = list(csv.DictReader(io.StringIO(canvas)))
                cell = next(row for row in rows if row["row_kind"] == "GLYPH_CELL")
                self.assertEqual(cell["original_write_packet_index"], "2")
                self.assertEqual(cell["original_draw_packet_index"], "3")
                self.assertEqual(int(cell["original_write_start_offset"]), 4 + len(frames[0]) + len(frames[1]))
                target = Path(directory) / "canvas.csv"; target.write_text(canvas)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/crsf-link/canvas-profile.toml"), str(target),
                    "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][1]["fields"]["glyph_byte"], glyph)
                _, later, _ = native.convert_native_file(path, native.MSP_DISPLAY_SERIAL_KIND,
                    capture_time_us=123, start_packet=4, stop_packet=5, canvas_geometry=(2, 3))
                later_row = next(csv.DictReader(io.StringIO(later)))
                self.assertEqual(later_row["unknown_count"], "6")
                self.assertEqual(later_row["original_draw_packet_index"], "4")

    def test_refusals_and_old_attitude_default_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.bin"; path.write_bytes(attitude())
            old = native.convert_msp_stream(attitude(), capture_time_us=0)
            same = native.convert_msp_stream(attitude(), capture_time_us=0, canvas_geometry=None)
            self.assertEqual(same, old)
            for kind, geometry in ((native.MSP_SERIAL_KIND, (1, 1)), (native.MSP_DISPLAY_SERIAL_KIND, None),
                                   (native.MSP_DISPLAY_SERIAL_KIND, (0, 1))):
                with self.assertRaises(ValueError):
                    native.convert_native_file(path, kind, capture_time_us=0, start_packet=0, stop_packet=1,
                                               canvas_geometry=geometry)
            good = reply([4]); path.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))
            with self.assertRaises(ValueError):
                native.convert_native_file(path, native.MSP_DISPLAY_SERIAL_KIND, capture_time_us=0,
                    start_packet=0, stop_packet=1, canvas_geometry=(1, 1))
