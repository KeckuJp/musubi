"""Authored saved log text; no CAN device or real capture claim."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_candump_recorded import convert, convert_slcan
from scripts.convert_dronecan_capture import convert_canopen, convert_j1939

ROOT = Path(__file__).resolve().parents[1]


class SavedCandump(unittest.TestCase):
    def test_slcan_responses_reuse_two_protocols_and_common_meaning(self):
        cases = [(False, "t701105", convert_canopen, "node_reported_mode", "OPERATIONAL"),
                 (True, "T18FECA8081BE445232307FFFF", convert_j1939, "spn_reported", 74565)]
        for extended, response, downstream, field, expected in cases:
            text = response + "EA5F\r" + response + "0000\r" + "r1231000001\r"
            capture, report = convert_slcan(text, "saved0", 1700000000123456,
                                            extended=extended, timestamps=True)
            self.assertEqual((report["source_lines"], report["selected_frames"], report["unconverted_lines"]), (3, 2, 1))
            self.assertEqual(report["clock"], "DECLARED_BATCH_CAPTURE_NOT_BUS_EVENT")
            self.assertEqual([r["reported_millisecond_within_minute"] for r in report["records"]], [59999, 0, 1])
            self.assertEqual(report["records"][-1]["reason"], "REMOTE_REQUEST")
            self.assertEqual(bytes.fromhex(report["records"][0]["original_hex"][4:]).decode(), response + "EA5F\r")
            output, decoded = downstream(capture)
            self.assertEqual(decoded["decoded_frames"], 2)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "observations.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"][field], expected)
                    self.assertEqual(common["platform_domain"], "Unknown")
        output, report = convert_slcan("t702104\r", "saved0", 1700000000000000)
        self.assertEqual(report["timestamp_mode"], "NOT_REPORTED")
        self.assertEqual(convert_canopen(output)[1]["decoded_frames"], 1)
        for bad in ("t701105", "t800105\r", "T20000000105\r", "t701205\r",
                    "t7011050000\r", "O\r", "t701105\n", "t701105\r\r"):
            with self.assertRaises(ValueError):
                convert_slcan(bad, "saved0", 1700000000000000)
        with self.assertRaises(ValueError):
            convert_slcan("t701105EA60\r", "saved0", 1700000000000000, timestamps=True)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "saved.txt"; source.write_bytes(b"t701105\r")
            target = Path(directory) / "out"
            subprocess.run([os.sys.executable, "-m", "scripts.convert_candump_recorded", str(source), str(target),
                            "--interface", "saved0", "--source-format", "slcanpty-2023.03",
                            "--capture-time-us", "1700000000000000"], cwd=ROOT, check=True, capture_output=True)
            self.assertEqual(json.loads((target / "report.json").read_text())["selected_frames"], 1)

    def test_two_protocols_reuse_existing_meanings_and_common_output(self):
        cases = [(False, "701#05", convert_canopen, "node_reported_mode", "OPERATIONAL"),
                 (True, "18FECA80#1BE445232307FFFF", convert_j1939, "spn_reported", 74565)]
        for extended, message, downstream, field, expected in cases:
            text = f"(1700000000.123456)  can0 {message} R\n(1700000000.123456) can0 {message}\n"
            capture, report = convert(text, "can0", extended=extended)
            self.assertEqual((report["source_lines"], report["selected_frames"]), (2, 2))
            self.assertEqual(report["records"][0]["capture_time_us"], 1700000000123456)
            self.assertEqual(report["records"][1]["selected_row"], 2)
            self.assertEqual(bytes.fromhex(report["records"][0]["original_hex"][4:]).decode(), text.splitlines()[0])
            output, decoded = downstream(capture)
            self.assertEqual(decoded["decoded_frames"], 2)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "observations.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"][field], expected)
                    self.assertEqual(common["platform_domain"], "Unknown")

    def test_unconverted_records_and_drop_notice_are_never_data_frames(self):
        bodies = ["can0 701#05", "can1 702#04", "can0 00000701#05", "can0 123#R8",
                  "can0 123##10011", "can0 20000004#0000000000000000"]
        text = "\n".join("(1700000000.000000) " + body for body in bodies)
        text += "\nDROPCOUNT: dropped 2 CAN frames on 'can0' socket (total drops 2)\n"
        output, report = convert(text, "can0")
        self.assertEqual((report["source_lines"], report["selected_frames"], report["unconverted_lines"]), (7, 1, 6))
        self.assertEqual([row["reason"] for row in report["records"]][1:],
                         ["UNSELECTED_INTERFACE", "UNSELECTED_FRAME_KIND", "REMOTE_REQUEST", "CAN_FD", "ERROR_FRAME", "SOURCE_DROP_REPORT"])
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 1)

    def test_important_format_time_and_selection_failures(self):
        good = "(1700000000.000000) can0 701#05"
        for text in ("", "(0.000000) can0 701#05", good.replace("701#05", "800#05"),
                     good.replace("701#05", "701#0"), good.replace("701#05", "701#" + "00"*9),
                     good + "\n" + good.replace("1700000000", "1699999999"),
                     good + "\nDROPCOUNT: corrupted", good.replace("can0", "can1")):
            with self.assertRaises(ValueError):
                convert(text, "can0")


if __name__ == "__main__":
    unittest.main()
