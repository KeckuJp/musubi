"""Existing saved-CAN window and engine resolver composition; authored captures."""
import hashlib
import csv
import json
from pathlib import Path
import tempfile
import unittest

from scripts.convert_candump_recorded import convert_path
from scripts import convert_dronecan_capture as can
from tests.test_nmea2000_weather import dynamic_frames
from tests.saved_can_window_helpers import native, common


class EngineWindowComposition(unittest.TestCase):
    def common(self, folder, output):
        return common(self, folder, output)

    def test_large_original_complete_exchange_and_second_instance(self):
        sample = bytes.fromhex("002f06ffffe3736505ff7f72100000ffffffffff060000007f7f")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "capture.log"
            prefix = b"(1700000000.000000) can1 09f20110#0000000000000000\n" * 350000
            raw = prefix + native(dynamic_frames(sample)).encode()
            self.assertGreater(len(raw), 16 * 1024 * 1024)
            path.write_bytes(raw)
            capture, report = convert_path(path, "can0", start_line=350000, stop_line=350004,
                                          extended=True, expected_size=len(raw),
                                          expected_sha256=hashlib.sha256(raw).hexdigest())
            self.assertEqual(report["source_window"]["source_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertEqual(report["source_window"]["excluded_physical_lines"], 350000)
            self.assertEqual([r["original_line"] for r in report["records"]], list(range(350001, 350005)))
            self.assertEqual(report["records"][0]["original_byte_offset"], len(prefix))
            output, engine = can.convert_nmea2000_engine_dynamic(capture)
            self.assertEqual((engine["frames"], engine["completed_packets"], engine["observations"]), (4, 1, 1))
            common = self.common(folder, output)
            row = common["observations"][0]
            self.assertEqual(row["clock_basis"], "Unknown")
            self.assertEqual(row["fields"]["engine_total_runtime_s"], 4210)
            self.assertEqual(row["fields"]["engine_temperature_k"], 296.67)
            source_rows = json.loads(bytes.fromhex(row["fields"]["source_frames_hex"][4:]))
            self.assertEqual(len(source_rows), 4)
            self.assertEqual(source_rows, list(csv.reader(capture.splitlines()))[1:])
            # Different original and instance, no implementation change.
            other = bytearray(sample)
            other[0] = 1
            other[11:15] = (42).to_bytes(4, "little")
            path.write_bytes(native(dynamic_frames(bytes(other), source=37)).encode())
            capture, report = convert_path(path, "can0", start_line=0, stop_line=4, extended=True)
            output, _ = can.convert_nmea2000_engine_dynamic(capture)
            row = self.common(folder, output)["observations"][0]
            self.assertEqual(row["fields"]["engine_total_runtime_s"], 42)
            self.assertEqual(row["fields"]["source_node_id"], 37)

    def test_rapid_composition_and_cut_exchange_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "capture.log"
            path.write_text("(1700000000.000000) can0 09f20010#00920cffff7fffff\n")
            capture, report = convert_path(path, "can0", start_line=0, stop_line=1, extended=True)
            output, _ = can.convert_nmea2000_weather(capture, engine=True)
            row = self.common(folder, output)["observations"][0]
            self.assertEqual(row["fields"]["engine_speed_rpm"], 804.5)
            path.write_text(native(dynamic_frames(bytes(26))))
            for start, stop in ((0, 3), (1, 4)):
                capture, _ = convert_path(path, "can0", start_line=start, stop_line=stop, extended=True)
                with self.assertRaises(ValueError):
                    can.convert_nmea2000_engine_dynamic(capture)


if __name__ == "__main__":
    unittest.main()
