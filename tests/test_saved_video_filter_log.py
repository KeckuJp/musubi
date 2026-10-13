"""Fixed-writer authored logs, not filmed video or a physical failure demonstration."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_saved_video_conditions import THRESHOLDS, OBSERVED
from scripts import convert_saved_video_conditions as legacy

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-video-filter-log/profile.toml"
BLACK, FREEZE = "[blackdetect @ 0x100]", "[freezedetect @ 0x200]"
OPTIONS = dict(black_context=BLACK, freeze_context=FREEZE, thresholds=THRESHOLDS,
               threshold_basis="operator-declared", decoder_status="errors-reported",
               analysed_frame_count=70)


def line(context, body):
    return f"{context} {body}\n".encode()


class FilterLogTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module("scripts.convert_saved_video_filter_log")

    def convert(self, data, **options):
        return self.m.convert(data, 555, **(OPTIONS | options))

    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader: self.skipTest("actual common reader required in CI")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            output = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                    check=True, capture_output=True)
        return json.loads(output.stdout)

    def test_reported_intervals_and_missing_attach_to_original_source_and_common(self):
        data = (b"ordinary unrelated diagnostic\n"
                + line(BLACK, "black_start:1 black_end:4 black_duration:3")
                + line(FREEZE, "lavfi.freezedetect.freeze_start: 1")
                + line("[freezedetect @ 0x999]", "lavfi.freezedetect.freeze_end: 99")
                + line(FREEZE, "lavfi.freezedetect.freeze_duration: 3")
                + line(FREEZE, "lavfi.freezedetect.freeze_end: 4")
                + line(FREEZE, "lavfi.freezedetect.freeze_start: NOPTS"))
        output, report = self.convert(data)
        common = self.common(output)
        rows = [o["fields"] for o in common["observations"]]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["video_record_kind"] for r in rows],
                         ["BLACK_INTERVAL", "FREEZE_INTERVAL", "FREEZE_INTERVAL"])
        self.assertEqual([r["video_duration_source"] for r in rows[:2]],
                         ["REPORTED_BY_THE_DECODER"] * 2)
        self.assertEqual((rows[0]["video_start_s"], rows[0]["video_end_s"],
                          rows[0]["video_duration_s"]), (1, 4, 3))
        self.assertIsNone(rows[2]["video_start_s"])
        self.assertIsNone(rows[2]["video_end_s"])
        self.assertIn("NO_END_REPORTED", rows[2]["video_end_disposition"])
        self.assertEqual([r["video_source_line_start"] for r in rows], [2, 3, 7])
        self.assertEqual([r["video_source_line_end"] for r in rows], [2, 6, None])
        self.assertTrue(all(r["video_start_attachment_frame"] is None for r in rows))
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.assertTrue(all(r["source_document_sha256"] == digest for r in rows))
        self.assertEqual(report["selected_cue_lines"], [2, 3, 5, 6, 7])
        self.assertEqual([v["line"] for v in report["unselected_lines"]], [1, 4])
        self.assertEqual(bytes.fromhex(report["source_text_hex"][4:]), data)
        self.assertEqual(report["open_intervals"], 1)

    def test_second_log_parent_context_and_unknown_reported_duration_reuse(self):
        parent = "[graph @ 0xabc] [blackdetect @ 0x123]"
        data = (line(parent, "black_start:2 black_end:2.25 black_duration:0.25")
                + line(FREEZE, "lavfi.freezedetect.freeze_start: NOPTS")
                + line(FREEZE, "lavfi.freezedetect.freeze_duration: 2")
                + line(FREEZE, "lavfi.freezedetect.freeze_end: NOPTS"))
        output, report = self.convert(data, black_context=parent)
        rows = [o["fields"] for o in self.common(output)["observations"]]
        self.assertEqual(rows[0]["video_duration_s"], 0.25)
        # Rounded minimum/caller mismatch is qualified, not claimed physically impossible.
        self.assertEqual(rows[0]["video_minimum_duration_qualification"],
                         "BELOW_THE_CALLER_DECLARED_MINIMUM_LOG_VALUES_AND_DECLARATION_NOT_RECONCILED")
        self.assertEqual(rows[1]["video_duration_s"], 2)
        self.assertIsNone(rows[1]["video_start_s"])
        self.assertIn("FRAME_RATE_FALLBACK", rows[1]["video_time_basis"])
        self.assertEqual(report["intervals_with_an_unknown_reported_end"], 1)
        output, report = self.convert(b"unknown new diagnostic\n")
        self.assertEqual(list(csv.DictReader(io.StringIO(output))), [])
        self.assertIn("not proof", report["no_interval_basis"])

    def test_rejections_cli_and_legacy_bytes(self):
        good = line(BLACK, "black_start:1 black_end:4 black_duration:3")
        for bad in (good[:-1], line(BLACK, "black_start:1 black_end:4"),
                    line(BLACK, "black_start:4 black_end:1 black_duration:3"),
                    line(BLACK, "black_start:1 black_end:4 black_duration:-3"),
                    line(BLACK, "black_start:1e999 black_end:4 black_duration:3"),
                    line(FREEZE, "lavfi.freezedetect.freeze_end: 3"),
                    line(FREEZE, "lavfi.freezedetect.freeze_start: 1") * 2,
                    b"    Last message repeated 3 times\n", b"\xff\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError): self.convert(bad)
        for name in ("MAX_LINES", "MAX_LINE_BYTES", "MAX_REPORT_BYTES"):
            with self.subTest(bound=name), patch.object(self.m, name, 0), self.assertRaises(ValueError):
                self.convert(good)
        with self.assertRaises(ValueError): self.convert(good, freeze_context=BLACK)
        with self.assertRaises(ValueError): self.convert(good, black_context="blackdetect")
        c, r = legacy.convert(OBSERVED, 555, thresholds=THRESHOLDS,
            threshold_basis="operator-declared", decoder_status="completed-without-reported-errors",
            analysed_frame_count=70)
        self.assertEqual(hashlib.sha256(c.encode()).hexdigest(),
                         "d0869ee6ca89893b41beadddd8d8ffcf4344d11b91a59bc91aa09bb32bd810bf")
        self.assertEqual(hashlib.sha256(json.dumps(r, indent=2).encode()).hexdigest(),
                         "11160716263af9c81b8c3316043189b396140177f4f1005ebe39ae24ae86c2f6")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "log.txt", base / "out"
            source.write_bytes(good)
            command = [os.sys.executable, str(ROOT / "scripts/convert_saved_video_filter_log.py"),
                       str(source), str(out), "--black-context", BLACK, "--freeze-context", FREEZE,
                       "--capture-time-us", "555", "--decoder-status", "errors-reported",
                       "--analysed-frame-count", "70", "--threshold-basis", "operator-declared"]
            for key, value in THRESHOLDS.items(): command += ["--" + key.replace("_", "-"), value]
            done = subprocess.run(command, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((out / "source.log.txt").read_bytes(), good)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_reversed_freeze_pts_preserves_reported_fallback_duration_not_timeline(self):
        data = (line(FREEZE, "lavfi.freezedetect.freeze_start: 4")
                + line(FREEZE, "lavfi.freezedetect.freeze_duration: 3")
                + line(FREEZE, "lavfi.freezedetect.freeze_end: 1"))
        output, _ = self.convert(data)
        row = self.common(output)["observations"][0]["fields"]
        self.assertEqual((row["video_start_s"], row["video_end_s"], row["video_duration_s"]), (4, 1, 3))
        self.assertEqual(row["video_pts_order"], "REVERSED_PTS_WITH_REPORTED_DURATION")
        self.assertEqual(row["video_duration_source"], "REPORTED_BY_THE_DECODER")
        with self.assertRaises(ValueError):
            self.convert(data.replace(b"freeze_duration: 3", b"freeze_duration: NOPTS"))
        metadata = (b"frame:0 pts:0 pts_time:0\nlavfi.freezedetect.freeze_start=4\n"
                    b"lavfi.freezedetect.freeze_duration=3\nlavfi.freezedetect.freeze_end=1\n")
        with self.assertRaises(ValueError):
            legacy.convert(metadata, 555, thresholds=THRESHOLDS, threshold_basis="operator-declared",
                           decoder_status="errors-reported", analysed_frame_count=70)


if __name__ == "__main__": unittest.main()
