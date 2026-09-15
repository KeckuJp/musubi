"""Authored ffprobe frame metadata, not physical DVR confirmation."""
import copy
import csv
import io
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_video_frames import convert, convert_detections

ROOT = Path(__file__).resolve().parents[1]


def document():
    frame = {"media_type": "video", "stream_index": 0, "pts": 3003, "width": 320,
             "height": 240, "key_frame": 1, "pict_type": "I", "future_field": [1, 2]}
    missing = dict(frame); missing.pop("pts")
    return {"program_version": {"version": "8.1"}, "streams": [
        {"index": 0, "codec_type": "video", "time_base": "1/90000", "nb_read_frames": "3"}],
        "frames": [frame, dict(frame), missing, {"media_type": "audio", "stream_index": 1}],
        "other": {"keep": True}}


class VideoFrameTests(unittest.TestCase):



    def test_compact_untimed_accounting_reuse_and_cli(self):
        data = document()
        baseline, old_report = convert(json.dumps(data), 0)
        output, report = convert(json.dumps(data), 0, compact=True)
        for key in old_report:
            self.assertEqual(report[key], old_report[key])
        old_rows = list(csv.DictReader(io.StringIO(baseline)))
        rows = list(csv.DictReader(io.StringIO(output)))
        for old, row in zip(old_rows, rows):
            for key in old:
                if key != "source_stream_hex": self.assertEqual(row[key], old[key])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frames.json"; source.write_text(json.dumps(data))
            destination = Path(directory) / "new"
            command = [sys.executable, str(ROOT / "scripts/convert_video_frames.py"),
                       str(source), "0", str(destination), "--compact"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((destination / "observations.csv").read_text(), output)
            self.assertEqual(json.loads((destination / "report.json").read_text()), report)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
        for value in (1, None, "yes"):
            with self.assertRaises(ValueError): convert(json.dumps(data), 0, compact=value)

    def test_rational_time_equal_records_and_untimed_accounting(self):
        output, report = convert(json.dumps(document()), 0)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["record_time_us"] for r in rows], ["33366", "33366"])
        self.assertEqual([r["source_frame_index"] for r in rows], ["0", "1"])
        self.assertEqual(report["selected_video_frames"], 3)
        self.assertEqual(report["untimed_video_frames"], 1)
        self.assertEqual(report["unsupported_frames"], 1)
        self.assertEqual(len(report["retained_unconverted_frames"]), 2)
        self.assertEqual(report["source_metadata"]["other"], {"keep": True})
        raw = json.loads(bytes.fromhex(rows[0]["source_frame_hex"][4:]))
        self.assertEqual(raw["future_field"], [1, 2])
        self.assertNotIn("dropped_frames", rows[0])

    def test_invalid_timebase_count_version_and_values_reject(self):
        for mode in ["version", "timebase", "count", "negative", "decreasing", "dimension", "no_time"]:
            data = document()
            if mode == "version": data["program_version"]["version"] = "unconfirmed"
            if mode == "timebase": data["streams"][0]["time_base"] = "1/0"
            if mode == "count": data["streams"][0]["nb_read_frames"] = "4"
            if mode == "negative": data["frames"][0]["pts"] = -1
            if mode == "decreasing": data["frames"][1]["pts"] = 1
            if mode == "dimension": data["frames"][0]["width"] = 0
            if mode == "no_time":
                for f in data["frames"]: f.pop("pts", None)
            for compact in (False, True):
                with self.subTest(mode=mode, compact=compact), self.assertRaises(ValueError):
                    convert(json.dumps(data), 0, compact=compact)
        for raw in ['{"frames":[],"frames":[]}', '{}', '{"x":NaN}']:
            with self.assertRaises(ValueError): convert(raw, 0)



if __name__ == "__main__":
    unittest.main()
