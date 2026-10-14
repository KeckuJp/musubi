"""Saved native navigation window composition, authored capture not real vessel."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.convert_candump_recorded import convert_path
from scripts.convert_dronecan_capture import convert_nmea2000_weather
from tests.saved_can_window_helpers import native, common


class NavigationWindow(unittest.TestCase):
    def test_large_original_and_other_reference_reach_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "capture.log"
            for reference, rate, excluded in ((0, 32000000, 350000), (1, -16000000, 1)):
                heading = (bytes([7]) + (10000).to_bytes(2, "little") +
                           (-100).to_bytes(2, "little", signed=True) +
                           (200).to_bytes(2, "little", signed=True) + bytes([reference]))
                turn = bytes([7]) + rate.to_bytes(4, "little", signed=True) + bytes([255] * 3)
                prefix = b"(1700000000.000000) can1 09f20110#0000000000000000\n" * excluded
                raw = prefix + native(f"10,09f1122a,{heading.hex()}\n10,09f1132a,{turn.hex()}\n").encode()
                if excluded == 350000:
                    self.assertGreater(len(raw), 16 * 1024 * 1024)
                path.write_bytes(raw)
                capture, report = convert_path(path, "can0", start_line=excluded,
                    stop_line=excluded + 2, extended=True, expected_size=len(raw),
                    expected_sha256=hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["source_window"]["source_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["source_window"]["excluded_physical_lines"], excluded)
                self.assertEqual([r["original_line"] for r in report["records"]], [excluded + 1, excluded + 2])
                self.assertEqual(report["records"][0]["original_byte_offset"], len(prefix))
                output, semantic = convert_nmea2000_weather(capture, heading=True)
                self.assertEqual(semantic["decoded_frames"], 2)
                rows = common(self, folder, output)["observations"]
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["fields"]["heading_rad"], 1)
                self.assertEqual(rows[0]["fields"]["heading_reference_reported"],
                                 ("TRUE_REPORTED", "MAGNETIC_REPORTED")[reference])
                self.assertEqual(rows[0]["fields"]["heading_deviation_rad"], -.01)
                self.assertEqual(rows[0]["fields"]["heading_variation_rad"], .02)
                self.assertEqual(rows[1]["fields"]["turn_rate_rad_s"], rate * 3.125e-8)
                self.assertTrue(all(r["clock_basis"] == "Unknown" for r in rows))

    def test_missing_unknown_reference_and_selected_wrong_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "capture.log"
            path.write_text(native("1,09f1122a,00ffffff7ffe7fff\n2,09f1132a,00ffffff7fffffff\n"))
            capture, _ = convert_path(path, "can0", start_line=0, stop_line=2, extended=True)
            output, _ = convert_nmea2000_weather(capture, heading=True)
            rows = common(self, folder, output)["observations"]
            self.assertEqual(rows[0]["fields"]["heading_reference_reported"], "UNKNOWN_3")
            self.assertEqual(rows[0]["fields"]["heading_rad_status"], "NOT_AVAILABLE")
            self.assertEqual(rows[1]["fields"]["turn_rate_rad_s_status"], "NOT_AVAILABLE")
            path.write_text(native("1,09f1122a,00\n"))
            capture, _ = convert_path(path, "can0", start_line=0, stop_line=1, extended=True)
            with self.assertRaises(ValueError):
                convert_nmea2000_weather(capture, heading=True)
