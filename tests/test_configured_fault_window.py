"""Authored saved classic candump to existing configured EMCY/common; no actual bus."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_candump_recorded as capture
from scripts import convert_dronecan_capture as fault

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/dronecan-recorded/canopen-configured-profile.toml"

def line(identifier, data):
    return f"(1700000000.000001) can0 {identifier:03X}#{data}\n"

class ConfiguredFaultWindow(unittest.TestCase):
    def test_two_saved_inputs_large_window_and_common_meaning(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "post-build stage requires actual common reader")
        for node, identifier, large in ((7, 0x181, True), (42, 0x301, False)):
            with self.subTest(node=node), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                padding = line(0x123, "01").encode()
                count = capture.LIMIT // len(padding) + 1 if large else 0
                prefix = padding * count
                selected = (line(identifier, "3081501b02000000") + line(identifier, "0000000000000000")
                            + line(identifier, "R8") + line(0x123, "01")).encode()
                original = prefix + selected
                source = root / "capture.log"
                source.write_bytes(original)
                if large:
                    self.assertGreater(source.stat().st_size, capture.LIMIT)
                converted, account = capture.convert_path(source, "can0", start_line=count,
                    stop_line=count + 4, data_frame_type=True)
                self.assertEqual(converted.splitlines()[0], "time_us,standard_id,frame_type,data_hex")
                self.assertEqual(account["source_window"]["source_sha256"], "sha256:" + hashlib.sha256(original).hexdigest())
                self.assertEqual(account["records"][0]["original_line"], count + 1)
                self.assertEqual(account["records"][0]["original_byte_offset"], len(prefix))
                self.assertEqual(account["records"][2]["reason"], "REMOTE_REQUEST")
                self.assertEqual((account["selected_frames"], account["unconverted_lines"]), (3, 1))
                output, report = fault.convert_canopen(converted, include_emergency=True,
                                                       emergency_layout=[f"{node}:{hex(identifier)}"])
                self.assertEqual((report["frames"], report["decoded_frames"], len(report["unsupported_frames"])), (3, 2, 1))
                path = root / "observations.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                observations = common["observations"]
                fields = [o["fields"] for o in observations]
                self.assertEqual(fields[0]["source_node_id"], node)
                self.assertEqual(fields[0]["canopen_error_code"], 0x8130)
                self.assertEqual(fields[0]["canopen_error_reserved_reported"], 1)
                self.assertEqual(fields[1]["canopen_error_report_kind"], "ERROR_RESET_REPORTED")
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
                retained = json.loads(bytes.fromhex(fields[0]["source_frames_hex"][4:]))
                self.assertEqual(retained[0][2], "DATA")

    def test_option_layout_and_truncation_refuse(self):
        text = line(0x181, "3081501b02000000")
        for options in (dict(fd=True), dict(extended=True), dict(data_frame_type="yes")):
            options.setdefault("data_frame_type", True)
            with self.assertRaises(ValueError):
                capture.convert(text, "can0", **options)
        converted, _ = capture.convert(text, "can0", data_frame_type=True)
        with self.assertRaises(ValueError):
            fault.convert_canopen(converted, include_emergency=True, emergency_layout=["7:0x80000181"])
        with self.assertRaises(ValueError):
            capture.convert(line(0x181, "3081501b0200000"), "can0", data_frame_type=True)

    def test_default_three_column_bytes_and_accounting_unchanged(self):
        text = line(0x181, "3081501b02000000") + line(0x181, "R8")
        previous = capture.convert(text, "can0")
        self.assertEqual(previous, capture.convert(text, "can0", data_frame_type=False))
        self.assertEqual(previous[0], "time_us,standard_id,data_hex\n1700000000000001,181,3081501b02000000\n")
        current = capture.convert(text, "can0", data_frame_type=True)
        self.assertEqual(current[1], previous[1])
        self.assertIn(",DATA,3081501b02000000\n", current[0])
