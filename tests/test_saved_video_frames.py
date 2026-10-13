import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_saved_video_frames import convert, digest

ROOT = Path(__file__).resolve().parents[1]


def fixture(ticks=(0, 2, 2, 4), stream=0, base="1/2000000"):
    obj = {"program_version": {"version": "8.1"}, "streams": [{"index": stream,
        "codec_type": "video", "time_base": base, "nb_read_frames": str(len(ticks))}],
        "frames": [{"media_type": "video", "stream_index": stream, "pts": p,
                    "width": 320, "height": 200, "key_frame": 0, "pict_type": "P", "unknown": "keep"}
                   for p in ticks]}
    raw = json.dumps(obj).encode()
    dec = dict(schema="saved-video-pts-expectation/v1", export_sha256=digest(raw), stream_index=stream,
        time_base=base, start_pts=0, stop_pts=5, expected_pts=[0, 1, 2, 4])
    return raw, dec


class SavedVideoFrames(unittest.TestCase):
    def test_distinct_exports_common_and_exact_ticks(self):
        for raw, dec in (fixture(), fixture((0, 1, 4), 3, "1001/30000")):
            text, report = convert(raw, json.dumps(dec).encode())
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(rows[0]["presence"], "OBSERVED")
            self.assertEqual(len(rows), 4)
            self.assertEqual(report["expectation_check"]["source_export_sha256"], digest(raw))
            self.assertEqual(report["expectation_check"]["output_sha256"], digest(text.encode()))
            with tempfile.TemporaryDirectory() as d:
                p = Path(d)
                (p/"o.csv").write_text(text)
                binary = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(binary)
                result = subprocess.run([binary, str(ROOT/"profiles/declared/video-frame-export/expectation-profile.toml"),
                    str(p/"o.csv"), "--allow-equal-time"], capture_output=True, text=True, check=True)
                observations = json.loads(result.stdout)["observations"]
                self.assertEqual(len(observations), 4)
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
        raw, dec = fixture()
        text, report = convert(raw, json.dumps(dec).encode())
        rows = list(csv.DictReader(io.StringIO(text)))
        # PTS0 and expectedPTS1 floor to the same microsecond, but are not equal ticks.
        self.assertEqual(rows[1]["presence"], "NOT_OBSERVED_IN_SAVED_WINDOW")
        self.assertEqual(rows[2]["matched_frame_count"], "2")
        self.assertEqual(json.loads(bytes.fromhex(rows[2]["matched_frame_indices_hex"][4:])), [1, 2])
        self.assertEqual(report["expectation_check"]["matched_frames"], 4)

    def test_unknown_window_and_refusals(self):
        raw, dec = fixture((0, 2, "N/A", 4))
        text, report = convert(raw, json.dumps(dec).encode())
        self.assertEqual(list(csv.DictReader(io.StringIO(text)))[1]["presence"], "INDETERMINATE")
        self.assertEqual(report["untimed_video_frames"], 1)
        raw, dec = fixture()
        dec["stop_pts"] = 6
        text, _ = convert(raw, json.dumps(dec).encode())
        self.assertEqual(list(csv.DictReader(io.StringIO(text)))[1]["presence"], "INDETERMINATE")
        for key, value in (("export_sha256", "bad"), ("stream_index", True), ("time_base", "1/1"),
                           ("expected_pts", [0, 0]), ("expected_pts", [6]), ("expected_pts", []),
                           ("expected_pts", list(range(4097))), ("start_pts", 6), ("schema", "other")):
            with self.subTest(key=key, value=str(value)[:30]), self.assertRaises(ValueError):
                convert(raw, json.dumps(dict(dec, **{key:value})).encode())
        with self.assertRaises(ValueError):
            convert(raw, json.dumps(dec).replace('"stream_index": 0', '"stream_index": 0,"stream_index": 1').encode())
        for modify in (lambda o:o["program_version"].update(version="8.0"),
                       lambda o:o["streams"][0].update(codec_type="audio"),
                       lambda o:o["streams"][0].update(nb_read_frames="2")):
            obj = json.loads(raw); modify(obj); changed = json.dumps(obj).encode()
            with self.assertRaises(ValueError):
                convert(changed, json.dumps(dict(dec, export_sha256=digest(changed))).encode())

    def test_cli_preserves_sources_and_refuses_overwrite(self):
        raw, dec = fixture()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); expectation = json.dumps(dec).encode()
            (p/"x.json").write_bytes(raw); (p/"e.json").write_bytes(expectation)
            command = [os.sys.executable, str(ROOT/"scripts/check_saved_video_frames.py"),
                       str(p/"x.json"), str(p/"e.json"), str(p/"out")]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((p/"out/source-export.json").read_bytes(),raw)
            self.assertEqual((p/"out/expectation.json").read_bytes(),expectation)
            old = (p/"out/observations.csv").read_bytes()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((p/"out/observations.csv").read_bytes(),old)
