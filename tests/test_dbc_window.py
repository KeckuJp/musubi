"""Existing saved-window/DBC composition; authored inputs, no CAN device."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_candump_recorded import convert_path
from scripts.convert_dbc_recorded import convert
from tests.test_dbc_recorded import DBC

ROOT = Path(__file__).resolve().parents[1]


class DbcWindow(unittest.TestCase):
    def test_classic_and_explicit_fd_window_common_composition(self):
        reader = os.environ["MUSUBI_TELEMETRY_READER"]
        fd_dbc = DBC.replace(b"Measurements: 4", b"Measurements: 12") + b'''
BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN_FD","ExtendedCAN_FD";
BA_ "VFrameFormat" BO_ 256 0;
'''
        for fd, dbc, payload, voltage, temperature in (
                (False, DBC, "e803f602", 10., 263.15),
                (True, fd_dbc, "d0071402" + "00" * 8, 20., 293.15)):
            with self.subTest(fd=fd), tempfile.TemporaryDirectory() as directory:
                prefix = (b"unselected semantics NOT_DECODED " + b"x" * 240 + b"\n") * (65000 if not fd else 2)
                start = 65000 if not fd else 2
                marker = "##1" if fd else "#"
                selected = (f"(1700000000.000001) can0 100{marker}{payload}\r\n"
                            f"(1700000000.000002) can0 101{marker}{payload}\r\n").encode()
                raw = prefix + selected + b"outside semantics unknown\n"
                if not fd:
                    self.assertGreater(len(raw), 16 * 1024 * 1024)
                source = Path(directory) / "saved.log"
                source.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                capture, framing = convert_path(source, "can0", start_line=start,
                    stop_line=start + 2, fd=fd, expected_size=len(raw), expected_sha256=digest)
                self.assertEqual(framing["selected_frames"], 2)
                self.assertEqual(framing["source_window"]["total_physical_lines"], start + 3)
                self.assertEqual(framing["source_window"]["selected_physical_lines"], 2)
                self.assertEqual(framing["source_window"]["excluded_physical_lines"], start + 1)
                self.assertEqual(framing["source_window"]["outside_semantics"],
                    "NOT_DECODED_FRAMING_AND_UTF8_CHECKED_ONLY")
                self.assertEqual(framing["source_window"]["source_sha256"], "sha256:" + digest)
                self.assertEqual(framing["records"][0]["original_line"], start + 1)
                self.assertEqual(framing["records"][0]["original_byte_offset"], len(prefix))
                output, meaning = convert(capture, dbc, fd=fd)
                self.assertEqual((meaning["frames"], meaning["decoded_frames"], meaning["observations"]), (2, 1, 2))
                self.assertEqual([item["reason"] for item in meaning["unsupported"]],
                    ["UNIT_OR_ENUM_NOT_QUALIFIED", "UNSELECTED_FRAME"])
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(rows[0]["source_data_hex"], "hex:" + payload)
                self.assertEqual(rows[0]["dbc_sha256"], "sha256:" + hashlib.sha256(dbc).hexdigest())
                self.assertEqual(int(rows[0]["record_time_us"]), 1700000000000001)
                path = Path(directory) / "out.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([reader,
                    str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertAlmostEqual(common["observations"][0]["fields"]["voltage_v"], voltage)
                self.assertAlmostEqual(common["observations"][1]["fields"]["temperature_k"], temperature)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                with self.assertRaises(ValueError):
                    convert_path(source, "can0", start_line=start, stop_line=start + 2,
                        fd=fd, expected_size=len(raw), expected_sha256="0" * 64)
                short_capture = capture.replace(payload, payload[:-2])
                with self.assertRaises(ValueError):
                    convert(short_capture, dbc, fd=fd)

    def test_multi_interface_candump_selects_one_ordered_interface_to_common(self):
        """candump 2023.03 logs each named interface's frames in epoll order with own kernel time."""
        from scripts.convert_candump_recorded import convert as candump
        reader = os.environ["MUSUBI_TELEMETRY_READER"]
        text = ("(1700000000.000001) can0 100#e803f602\n(1700000000.000005) can1 100#d0071402\n"
                "(1700000000.000003) can0 100#d0071402\n(1700000000.000004) can0 123#R\n")
        capture, framing = candump(text, "can0")
        self.assertEqual([r["reason"] for r in framing["records"]],
            ["SELECTED_CLASSIC_DATA", "UNSELECTED_INTERFACE", "SELECTED_CLASSIC_DATA", "REMOTE_REQUEST"])
        output, meaning = convert(capture, DBC)
        self.assertEqual((meaning["frames"], meaning["observations"]), (2, 4))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([reader,
                str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        voltages = [o["fields"]["voltage_v"] for o in common["observations"] if o["fields"].get("voltage_v") is not None]
        self.assertEqual([round(v, 6) for v in voltages], [10.0, 20.0])
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
