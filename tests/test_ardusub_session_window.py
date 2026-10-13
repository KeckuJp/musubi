"""Authored long saved input, not a native BIN or real underwater recording."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import convert_ardusub_session_window as window
from tests.test_ardusub_session import records, ROOT


def encoded(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class SubWindow(unittest.TestCase):
    def convert(self, path, start, stop, context=0):
        reader = os.environ.get("MUSUBI_MAVLOG_READER")
        self.assertTrue(reader, "reader-equipped mandatory stage required")
        return window.convert(path, start_record=start, stop_record=stop, build_record=context,
            source_pin=window.session.PIN, session_id="authored-window", single_boot_session=True,
            mavlog_reader=reader)

    def test_large_original_two_distinct_windows_reach_common(self):
        common = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(common, "common reader required")
        first, second = records(12), records(24)
        second[0]["data"]["TimeUS"] = 20
        for row in second[1:-1]:
            row["data"]["TimeUS"] += 100
        filler = dict(meta=dict(type="FUTURE"), data=dict(blob="a" * 29000))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.jsonl"
            path.write_bytes(encoded(first + [filler] * 600 + second))
            self.assertGreater(path.stat().st_size, 16 * 1024**2)
            for start, stop, context, value in ((1, 6, 0, 12), (608, 613, 607, 24)):
                files, report = self.convert(path, start, stop, context)
                self.assertEqual(report["window_records"], 5)
                self.assertEqual(report["selected_records"], 5)
                self.assertEqual(report["context_only_records"], 1)
                self.assertEqual(report["window_unselected_records"], 0)
                self.assertEqual(report["missing_required_families"], [])
                self.assertEqual(report["original_records"], 614)
                self.assertEqual(report["outside_window_records"], 609)
                self.assertNotEqual(report["original_jsonl_sha256"], report["selected_with_context_sha256"])
                for item in report["outputs"]:
                    out = Path(directory) / item["file"]
                    out.write_bytes(files[item["file"]])
                    observed = json.loads(subprocess.run([common, str(ROOT / item["profile"]), str(out),
                        *item["reader_options"]], capture_output=True, check=True).stdout)
                    field = observed["observations"][0]["fields"]
                    self.assertGreaterEqual(field["original_jsonl_record_index"], start)
                    self.assertLess(field["original_jsonl_record_index"], stop)
                    self.assertEqual(field["build_context_original_index"], context)
                    self.assertEqual(field["original_jsonl_sha256"], report["original_jsonl_sha256"])
                    if item["file"] == "IMU.csv":
                        self.assertEqual(field["GyrX"], value)
            _, report = self.convert(path, 0, 6)
            self.assertEqual(report["context_only_records"], 0)
            self.assertEqual(report["window_unselected_records"], 1)

    def test_changed_source_mixed_build_cut_record_and_clock_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.jsonl"
            for failure in ("mixed", "cut", "clock", "wrongcontext", "changed"):
                rows = records(12)
                if failure == "mixed":
                    rows[-1] = dict(meta=dict(type="MSG"), data=dict(Message="ArduCopter V4.5.7 (abcd1234)"))
                elif failure == "clock":
                    rows[5]["data"]["TimeUS"] = 0
                path.write_bytes(encoded(rows))
                if failure == "cut":
                    path.write_bytes(encoded(rows)[:-8])
                original = window.read_jsonl_window
                calls = []
                def changing(*args, **kwargs):
                    result = original(*args, **kwargs)
                    calls.append(1)
                    if len(calls) == 1:
                        path.write_bytes(encoded(rows) + b"{}\n")
                    return result
                with self.subTest(failure=failure), self.assertRaises(ValueError):
                    if failure == "changed":
                        with patch.object(window, "read_jsonl_window", changing):
                            self.convert(path, 1, 7)
                    else:
                        self.convert(path, 1, 7, 2 if failure == "wrongcontext" else 0)
