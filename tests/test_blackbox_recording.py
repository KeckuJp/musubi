"""Synthetic, offline checks for the raw recording wrapper; no decoder is installed."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "decode_blackbox_recording",
    Path(__file__).resolve().parents[1] / "scripts" / "decode_blackbox_recording.py",
)
decoder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(decoder)


def recording(version="4.2.11"):
    return (
        decoder.MAGIC
        + f"H Firmware revision:Betaflight {version} synthetic\n".encode("ascii")
        + b"synthetic payload\n"
        + decoder.END
    )


class RecordingInspectionTests(unittest.TestCase):
    def test_supported_version(self):
        self.assertEqual(decoder.inspect_recording(recording()), "Betaflight 4.2.11 synthetic")

    def test_other_versions_are_rejected(self):
        for version in ("4.1.7", "4.3.0", "5.0.0"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "4.2"):
                decoder.inspect_recording(recording(version))

    def test_wrong_format_is_rejected(self):
        for data in (b"", b"time,vbatLatest\n0,1200\n", b"other" + recording()):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "expected one raw"):
                decoder.inspect_recording(data)
        with self.assertRaisesRegex(ValueError, "4.2"):
            decoder.inspect_recording(recording().replace(b"Betaflight", b"INAV"))

    def test_truncated_and_multiple_recordings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "End of log"):
            decoder.inspect_recording(recording()[:-1])
        with self.assertRaisesRegex(ValueError, "expected one raw"):
            decoder.inspect_recording(recording() + recording())


class DecoderReportTests(unittest.TestCase):
    def test_positive_count_and_missing_iterations_are_preserved(self):
        self.assertEqual(
            decoder.check_decoder_report(
                "Frames 2 100%\n0 frames failed to decode, rendering 0 loop iterations unreadable\n"
                "3 iterations are missing in total\n"
            ),
            {"main_frames": 2, "missing_loop_iterations": 3},
        )
        self.assertEqual(decoder.check_decoder_report("Frames 1 100%\n")["missing_loop_iterations"], 0)

    def test_corruption_is_rejected_even_with_positive_frames(self):
        for failed, unreadable in ((1, 0), (0, 1), (2, 3)):
            report = f"Frames 2 100%\n{failed} frames failed to decode, rendering {unreadable} loop iterations unreadable\n"
            with self.subTest(failed=failed, unreadable=unreadable):
                with self.assertRaisesRegex(ValueError, "corrupt/unreadable"):
                    decoder.check_decoder_report(report)

    def test_zero_or_missing_frame_count_is_rejected(self):
        for report in ("Frames 0 0%\n", "", "no frame summary\n"):
            with self.subTest(report=report), self.assertRaisesRegex(ValueError, "positive main-frame"):
                decoder.check_decoder_report(report)


class MainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = recording()
        self.input = self.root / "synthetic recording.bbl"
        self.input.write_bytes(self.raw)
        self.executable = self.root / "selected decoder"
        self.executable.write_bytes(b"synthetic decoder identity only")
        self.output = self.root / "output"
        self.csv = b"time,vbatLatest,amperageLatest\n1000000,1200,50\n2000000,1190,60\n"

    def invoke(self, result):
        argv = ["decode_blackbox_recording.py", str(self.executable), str(self.input), str(self.output)]
        with patch("sys.argv", argv), patch.object(decoder.subprocess, "run", return_value=result) as run:
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                decoder.main()
        return run, printed.getvalue()

    def test_success_preserves_native_csv_and_writes_metadata(self):
        report = b"Frames 2 100%\n3 iterations are missing in total\n"
        run, printed = self.invoke(subprocess.CompletedProcess([], 0, self.csv, report))
        run.assert_called_once_with(
            [str(self.executable.resolve()), "--stdout", "--index", "1", "--unit-vbat", "raw",
             "--unit-amperage", "raw", "--unit-flags", "raw", str(self.input.resolve())],
            capture_output=True, timeout=60, env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        self.assertEqual((self.output / "decoded.csv").read_bytes(), self.csv)
        self.assertEqual((self.output / "decoder.stderr.log").read_bytes(), report)
        metadata = json.loads((self.output / "decode.json").read_text())
        self.assertEqual(metadata, json.loads(printed))
        self.assertEqual(metadata["firmware"], "Betaflight 4.2.11 synthetic")
        self.assertEqual(metadata["main_frames"], 2)
        self.assertEqual(metadata["missing_loop_iterations"], 3)
        self.assertEqual(metadata["input_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(metadata["decoder_sha256"], hashlib.sha256(self.executable.read_bytes()).hexdigest())
        self.assertEqual(metadata["source_units"], {"time": "us", "vbatLatest": "cV", "amperageLatest": "cA"})
        self.assertEqual(metadata["domain"], "Unknown")
        self.assertEqual(metadata["common_output"], "NOT_RUN")
        self.assertIn("GPS/events not normalized", metadata["scope"])
        self.assertIn("not sufficient", metadata["domain_basis"])

    def test_decoder_and_validation_failures_do_not_write_csv(self):
        cases = (
            (1, self.csv, b"Frames 2 100%\n", "external decoder failed"),
            (0, self.csv, b"Frames 2 100%\n1 frames failed to decode, rendering 0 loop iterations unreadable\n", "corrupt/unreadable"),
            (0, self.csv, b"Frames 0 0%\n", "positive main-frame"),
            (0, self.csv, b"Frames 1 100%\n", "row count"),
        )
        for index, (code, stdout, stderr, message) in enumerate(cases):
            with self.subTest(case=index):
                self.output = self.root / f"failure-{index}"
                with self.assertRaisesRegex(ValueError, message):
                    self.invoke(subprocess.CompletedProcess([], code, stdout, stderr))
                self.assertEqual((self.output / "decoder.stderr.log").read_bytes(), stderr)
                self.assertFalse((self.output / "decoded.csv").exists())
                self.assertFalse((self.output / "decode.json").exists())


if __name__ == "__main__":
    unittest.main()
