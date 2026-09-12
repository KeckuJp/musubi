"""Authored synthetic tests for explicitly selecting an offline GPS decoder stream.

External decoder calls are stubbed; these are not real-log acceptance tests.
Existing default-main compatibility tests must also remain in the ordinary suite.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(os.environ.get("MUSUBI_POSITION_CSV_ROOT", Path(__file__).resolve().parents[1]))
SCRIPT = ROOT / "scripts" / "decode_blackbox_recording.py"
GPS = (b"time (us), GPS_numSat, GPS_coord[0], GPS_coord[1], GPS_altitude, vendor\n"
       b"1001, 7, 1.25, -2.5, 1234, retained-a\n"
       b"1001, 8, 1.5, -2.0, 1235, retained-b\n")
MAIN = b"time (us), vbatLatest\n1, 2100\n2, 2101\n3, 2102\n4, 2103\n"
REPORT = "I frames 1 20 bytes avg\nP frames 3 10 bytes avg\nG frames 2 15 bytes avg\nFrames 4 20 bytes\n7 iterations are missing in total\n"


class BlackboxGpsStreamContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("blackbox_stream_under_test", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def recording(self, version="4.2.0"):
        return (self.module.MAGIC + f"H Firmware revision:Betaflight {version} (authored) GENERIC\n".encode()
                + self.module.END)

    def test_default_main_version_scope_is_unchanged(self):
        self.assertIn("4.2.0", self.module.inspect_recording(self.recording()))
        self.assertIn("4.2.11", self.module.inspect_recording(self.recording("4.2.11")))
        with self.assertRaises(ValueError):
            self.module.inspect_recording(self.recording("4.3.0"))

    def test_only_explicit_gps_mode_adds_43_family(self):
        for version in ("4.2.0", "4.2.11", "4.3.0", "4.3.2"):
            with self.subTest(version=version):
                self.assertIn(version, self.module.inspect_recording(self.recording(version), stream="gps"))
        for version in ("3.5.0", "4.1.0", "4.4.0", "5.0.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.module.inspect_recording(self.recording(version), stream="gps")

    def test_gps_mode_rejects_truncation_multilog_and_wrong_family(self):
        data = self.recording("4.3.0")
        for wrong in (data[:-1], data + data, data.replace(b"Betaflight", b"INAV"),
                      b"time,latitude\n1,2\n"):
            with self.subTest(kind=wrong[:24]), self.assertRaises(ValueError):
                self.module.inspect_recording(wrong, stream="gps")
        with self.assertRaises(ValueError):
            self.module.inspect_recording(data, stream="guess")

    def test_main_report_return_shape_stays_compatible(self):
        self.assertEqual(self.module.check_decoder_report(REPORT),
                         {"main_frames": 4, "missing_loop_iterations": 7})

    def test_gps_report_counts_g_frames_not_main_or_sampling_gaps(self):
        report = self.module.check_decoder_report(REPORT, stream="gps")
        self.assertEqual(report["main_frames"], 4)
        self.assertEqual(report["gps_frames"], 2)
        self.assertEqual(report["missing_loop_iterations"], 7)

    def test_gps_requires_one_positive_selected_frame_count(self):
        for report in ("Frames 4 20 bytes\n", REPORT.replace("G frames 2", "G frames 0"),
                       REPORT + "G frames 2 15 bytes\n", REPORT.replace("G frames 2", "G frames unknown")):
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.module.check_decoder_report(report, stream="gps")

    def test_any_corrupt_or_unreadable_report_prevents_gps_success(self):
        for suffix in ("1 frames failed to decode, rendering 0 loop iterations unreadable",
                       "0 frames failed to decode, rendering 1 loop iterations unreadable",
                       "0 frames failed to decode, rendering 0 loop iterations unreadable\n"
                       "1 frames failed to decode, rendering 0 loop iterations unreadable"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.module.check_decoder_report(REPORT + suffix, stream="gps")

    def run_stub(self, root, *, gps=GPS, report=REPORT, returncode=0, missing=False, mode="gps"):
        raw, decoder, output = root / "source.bfl", root / "decoder", root / "output"
        raw.write_bytes(self.recording("4.3.0" if mode == "gps" else "4.2.0"))
        decoder.write_bytes(b"authored decoder placeholder; never executed")
        calls = []

        def decode(argv, **kwargs):
            calls.append(argv)
            if mode == "gps":
                self.assertNotIn("--stdout", argv)
                self.assertNotIn("--merge-gps", argv)
                self.assertNotIn("--raw", argv)
                self.assertIn("--output-dir", argv)
                directory = Path(argv[argv.index("--output-dir") + 1])
                directory.resolve().relative_to(output.resolve())
                self.assertEqual(argv[argv.index("--prefix") + 1], "decoded")
                self.assertEqual(argv[argv.index("--unit-frame-time") + 1], "us")
                self.assertEqual(argv[argv.index("--unit-gps-speed") + 1], "mps")
                (directory / "decoded.01.csv").write_bytes(MAIN)
                if not missing:
                    (directory / "decoded.01.gps.csv").write_bytes(gps)
                (directory / "decoded.01.gps.gpx").write_bytes(b"<gpx/>")
                (directory / "decoded.01.event").write_bytes(b"authored event sidecar\n")
                stdout = b""
            else:
                self.assertIn("--stdout", argv)
                self.assertNotIn("--merge-gps", argv)
                stdout = MAIN
            return subprocess.CompletedProcess(argv, returncode, stdout, report.encode())

        args = [str(SCRIPT), str(decoder), str(raw), str(output)]
        if mode == "gps":
            args += ["--stream", "gps"]
        with patch.object(sys, "argv", args), patch.object(self.module.subprocess, "run", side_effect=decode):
            self.module.main()
        return output, calls

    def test_gps_mode_selects_separate_file_and_retains_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, calls = self.run_stub(Path(tmp))
            self.assertEqual(len(calls), 1)
            self.assertEqual((output / "decoded.csv").read_bytes(), GPS)
            self.assertEqual(len((output / "decoded.csv").read_bytes().splitlines()) - 1, 2)
            self.assertTrue(list(output.rglob("decoded.01.csv")))
            self.assertTrue(list(output.rglob("decoded.01.event")))
            report = json.loads((output / "decode.json").read_bytes())
            self.assertEqual(report["stream"], "gps")
            self.assertEqual(report["gps_frames"], 2)
            self.assertEqual(report["main_frames"], 4)
            self.assertEqual(report["domain"], "Unknown")
            self.assertEqual(report["common_output"], "NOT_RUN")
            self.assertEqual(report["source_units"]["time"], "us")
            self.assertEqual(report["source_units"]["GPS_coord[0]"], "deg")
            self.assertEqual(report["source_units"]["GPS_coord[1]"], "deg")
            self.assertEqual(report["source_units"]["GPS_altitude"], "dm")
            self.assertNotIn("vbatLatest", report["source_units"])

    def test_main_default_outputs_and_native_units_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, _ = self.run_stub(Path(tmp), mode="main")
            self.assertEqual((output / "decoded.csv").read_bytes(), MAIN)
            report = json.loads((output / "decode.json").read_bytes())
            self.assertEqual(report["main_frames"], 4)
            self.assertEqual(report["source_units"],
                             {"time": "us", "vbatLatest": "cV", "amperageLatest": "cA"})

    def test_selected_file_missing_mismatch_or_failed_decoder_does_not_create_success_metadata(self):
        for options in ({"missing": True}, {"gps": GPS.splitlines()[0] + b"\n"},
                        {"gps": GPS + GPS.splitlines()[-1] + b"\n"}, {"returncode": 1},
                        {"report": REPORT + "1 frames failed to decode, rendering 0 loop iterations unreadable"}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((ValueError, OSError)):
                    self.run_stub(Path(tmp), **options)
                self.assertFalse((Path(tmp) / "output" / "decode.json").exists())


if __name__ == "__main__":
    unittest.main()
