"""Authored FFmpeg 8.1 filter-metadata fixtures; cues from a saved analysis, not a real recording.

No media is opened and no decoder is run by these tests. The fixtures are the output shape observed
directly from `blackdetect`/`freezedetect` at 8.1 on an authored lavfi clip.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_video_conditions import CUE_BASIS, TIME_BASIS, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-video-conditions/profile.toml"
THRESHOLDS = dict(black_min_duration="1", black_ratio_threshold="0.98",
                  black_pixel_threshold="0.10", freeze_noise="0.001", freeze_duration="1")
OBSERVED = b"""frame:10   pts:10240   pts_time:1
lavfi.black_start=1
frame:20   pts:20480   pts_time:2
lavfi.freezedetect.freeze_start=1
frame:40   pts:40960   pts_time:4
lavfi.black_end=4
lavfi.freezedetect.freeze_duration=3
lavfi.freezedetect.freeze_end=4
frame:50   pts:51200   pts_time:5
lavfi.freezedetect.freeze_start=4
"""


def run(data=OBSERVED, capture=555, **overrides):
    options = dict(thresholds=THRESHOLDS, threshold_basis="operator-declared",
                   decoder_status="completed-without-reported-errors", analysed_frame_count=70)
    options.update(overrides)
    output, report = convert(data, capture, **options)
    return list(csv.DictReader(io.StringIO(output))), report


class SavedVideoConditionTests(unittest.TestCase):
    def test_black_and_freeze_are_separate_cues_over_the_same_interval(self):
        rows, report = run()
        self.assertEqual([row["video_record_kind"] for row in rows],
                         ["BLACK_INTERVAL", "FREEZE_INTERVAL", "FREEZE_INTERVAL"])
        black, freeze, still = rows
        self.assertEqual((black["video_start_s"], black["video_end_s"]), ("1.0", "4.0"))
        self.assertEqual((freeze["video_start_s"], freeze["video_end_s"]), ("1.0", "4.0"))
        self.assertEqual(black["video_duration_source"], "DERIVED_FROM_THE_REPORTED_START_AND_END")
        self.assertEqual(freeze["video_duration_source"], "REPORTED_BY_THE_DECODER")
        self.assertEqual((black["video_duration_s"], freeze["video_duration_s"]), ("3.0", "3.0"))
        self.assertEqual(freeze["video_start_attachment_frame"], "20")
        self.assertEqual(freeze["video_start_attachment_pts_time_s"], "2.0")
        self.assertNotEqual(freeze["video_start_attachment_pts_time_s"], freeze["video_start_s"])
        self.assertEqual(still["video_start_s"], "4.0")
        self.assertEqual(still["video_end_s"], "")
        self.assertEqual(still["video_duration_s"], "")
        self.assertEqual(still["video_end_disposition"], "NO_END_REPORTED_BEFORE_THE_ANALYSIS_ENDED")
        self.assertEqual(still["video_minimum_duration_qualification"], "UNKNOWN_WITHOUT_A_DURATION")
        self.assertEqual((report["black_intervals"], report["freeze_intervals"],
                          report["open_intervals"]), (1, 2, 1))
        for row in rows:
            self.assertEqual(row["video_cue_basis"], CUE_BASIS)
            self.assertEqual(row["video_time_basis"], TIME_BASIS)
        for absent in ("NEVER_A_FAULT", "NEITHER_FILTERS_DOCUMENTATION_MAKES_A_CAUSAL_CLAIM",
                       "A_DARK_SCENE_AND_A_DELIBERATELY_STATIC_SCENE_PRODUCE_THESE_SAME_CUES"):
            self.assertIn(absent, CUE_BASIS)
        self.assertIn("NOT_UTC", TIME_BASIS)

    def test_a_static_scene_produces_a_freeze_cue_and_no_black_cue(self):
        """A deliberately static, well-lit scene is frozen by definition. That is a cue, and the
        row must not imply anything failed."""
        rows, report = run(b"frame:0   pts:0   pts_time:0\n"
                           b"lavfi.freezedetect.freeze_start=0\n"
                           b"frame:30   pts:30720   pts_time:3\n"
                           b"lavfi.freezedetect.freeze_duration=3\n"
                           b"lavfi.freezedetect.freeze_end=3\n")
        self.assertEqual([row["video_record_kind"] for row in rows], ["FREEZE_INTERVAL"])
        self.assertEqual(report["black_intervals"], 0)
        self.assertEqual(rows[0]["video_duration_s"], "3.0")
        self.assertIn("NEVER_A_FAULT", rows[0]["video_cue_basis"])

    def test_an_interval_shorter_than_the_declared_minimum_is_qualified_not_hidden(self):
        """The metadata form is not filtered by the minimum duration the analysis was run with --
        observed at 8.1: with d=2 a 0.5 s black run emits no log line but is still in metadata."""
        short = (b"frame:10   pts:10240   pts_time:1\nlavfi.black_start=1\n"
                 b"frame:15   pts:15360   pts_time:1.5\nlavfi.black_end=1.5\n")
        rows, _ = run(short, thresholds=dict(THRESHOLDS, black_min_duration="2"))
        self.assertEqual(rows[0]["video_duration_s"], "0.5")
        self.assertEqual(rows[0]["video_minimum_duration_qualification"],
                         "BELOW_THE_DECLARED_MINIMUM_THE_METADATA_FORM_REPORTS_EVERY_RUN_"
                         "REGARDLESS_OF_IT")
        rows, _ = run(short, thresholds=dict(THRESHOLDS, black_min_duration="0.25"))
        self.assertEqual(rows[0]["video_minimum_duration_qualification"],
                         "AT_OR_ABOVE_THE_DECLARED_MINIMUM")
        self.assertIn("basis=operator-declared", rows[0]["video_threshold_declaration"])
        self.assertIn("not_recoverable_from_the_analysis_output",
                      rows[0]["video_threshold_declaration"])

    def test_no_cue_under_a_failed_decode_is_not_evidence_of_absence(self):
        """Missing frames and decode failures are never reported as black or freeze, and an empty
        result under a failed run is explicitly not a clean-video finding."""
        rows, report = run(b"frame:0   pts:0   pts_time:0\nlavfi.something.else=1\n",
                           decoder_status="errors-reported", analysed_frame_count=1)
        self.assertEqual(rows, [])
        self.assertEqual(report["unselected_keys"], 1)
        self.assertIn("NOT evidence that none occurred", report["no_interval_basis"])
        self.assertIn("not evidence that a camera worked", report["no_interval_basis"])
        self.assertEqual(report["decoder_status"], "errors-reported")
        with self.assertRaises(ValueError):
            run(OBSERVED, analysed_frame_count=0)
        with self.assertRaises(ValueError):
            run(OBSERVED, decoder_status="assumed-fine")

    def test_unknown_and_malformed_analysis_output_is_refused_or_kept_unknown(self):
        rows, _ = run(b"frame:0   pts:0   pts_time:NOPTS\nlavfi.black_start=NOPTS\n"
                      b"frame:5   pts:5120   pts_time:0.5\nlavfi.black_end=0.5\n")
        self.assertEqual(rows[0]["video_start_s"], "")
        self.assertEqual(rows[0]["video_start_attachment_pts_time_s"], "")
        self.assertEqual(rows[0]["video_duration_s"], "")
        self.assertEqual(rows[0]["video_duration_source"],
                         "NOT_AVAILABLE_A_REPORTED_TIMESTAMP_WAS_ITSELF_UNKNOWN")
        self.assertEqual(rows[0]["video_end_disposition"], "REPORTED")
        for broken in (
            b"lavfi.black_start=1\n",                                    # entry before any frame
            b"frame:0   pts:0   pts_time:0\nnot a metadata line\n",
            b"frame:5   pts:0   pts_time:0\nlavfi.black_start=0\n"
            b"frame:5   pts:0   pts_time:0\n",                           # frame does not increase
            b"frame:0   pts:0   pts_time:0\nlavfi.black_end=0\n",        # end with no start
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=0\n"
            b"frame:1   pts:1   pts_time:0.1\nlavfi.black_start=0.1\n",  # second start, no end
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=5\n"
            b"frame:1   pts:1   pts_time:0.1\nlavfi.black_end=1\n",      # ends before it starts
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=1e999\n",
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=1e-999\n",
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=later\n",
            b"\xff\xfe",
        ):
            with self.subTest(document=broken[:48]), self.assertRaises(
                    (ValueError, UnicodeDecodeError)):
                run(broken)
        for missing in ({}, dict(THRESHOLDS, black_min_duration="-1"),
                        dict(THRESHOLDS, freeze_noise="none"),
                        {k: v for k, v in THRESHOLDS.items() if k != "freeze_noise"}):
            with self.subTest(thresholds=missing), self.assertRaises(ValueError):
                run(OBSERVED, thresholds=missing)
        with self.assertRaises(ValueError):
            run(OBSERVED, threshold_basis="because-i-say-so")

    def test_an_empty_export_is_the_ordinary_no_cue_shape_not_a_malformed_one(self):
        """Verified at 8.1: a clip with no black and no freeze produces a ZERO-BYTE metadata file.
        Refusing empty input would reject the commonest honest case, or make a caller fabricate a
        header to report a clean analysis."""
        rows, report = run(b"", analysed_frame_count=30)
        self.assertEqual(rows, [])
        self.assertEqual(report["analysed_frame_blocks"], 0)
        self.assertEqual(report["declared_count_consistency"],
                         "NOT_CONSTRAINED_BY_THIS_OUTPUT_WHICH_SHOWS_NO_FRAME_BLOCK_AT_ALL")
        self.assertIn("an empty or cue-free export is the ordinary shape",
                      report["no_interval_basis"])
        self.assertIn("not evidence that a camera worked", report["no_interval_basis"])
        self.assertEqual(report["analysed_frame_count"], 30)
        with self.assertRaises(ValueError):
            run(b"", analysed_frame_count=0)

    def test_a_reported_unknown_end_is_not_the_same_as_no_end(self):
        """The decoder reporting an end it could not express is a reported unknown. Calling that
        'no end was reported' would deny that anything was reported at all."""
        rows, report = run(b"frame:0   pts:0   pts_time:0\nlavfi.black_start=0\n"
                           b"frame:5   pts:5120   pts_time:0.5\nlavfi.black_end=NOPTS\n")
        row = rows[0]
        self.assertEqual(row["video_end_disposition"],
                         "END_REPORTED_BUT_THE_DECODER_COULD_NOT_EXPRESS_ITS_TIMESTAMP")
        self.assertEqual(row["video_end_s"], "")
        self.assertEqual(row["video_duration_s"], "")
        self.assertEqual(row["video_duration_source"],
                         "NOT_AVAILABLE_A_REPORTED_TIMESTAMP_WAS_ITSELF_UNKNOWN")
        self.assertEqual(report["intervals_with_an_unknown_reported_end"], 1)
        self.assertEqual(report["open_intervals"], 0)          # it is not an open interval
        rows, report = run(b"frame:0   pts:0   pts_time:0\nlavfi.black_start=0\n")
        self.assertEqual(rows[0]["video_end_disposition"],
                         "NO_END_REPORTED_BEFORE_THE_ANALYSIS_ENDED")
        self.assertEqual(report["open_intervals"], 1)
        self.assertEqual(report["intervals_with_an_unknown_reported_end"], 0)

    def test_repeated_or_impossible_selected_values_are_refused_not_overwritten(self):
        start = b"frame:0   pts:0   pts_time:0\nlavfi.freezedetect.freeze_start=0\n"
        for broken in (
            start + b"frame:5   pts:5120   pts_time:0.5\n"
                    b"lavfi.freezedetect.freeze_duration=1\n"
                    b"lavfi.freezedetect.freeze_duration=9\n"
                    b"lavfi.freezedetect.freeze_end=1\n",
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=0\n"
            b"frame:5   pts:5120   pts_time:0.5\nlavfi.black_end=1\nlavfi.black_end=2\n",
            start + b"frame:5   pts:5120   pts_time:0.5\n"
                    b"lavfi.freezedetect.freeze_duration=-3\n"
                    b"lavfi.freezedetect.freeze_end=1\n",
            b"frame:0   pts:0   pts_time:0\nlavfi.black_start=-1e308\n"
            b"frame:5   pts:5120   pts_time:0.5\nlavfi.black_end=1e308\n",
        ):
            with self.subTest(document=broken[-60:]), self.assertRaises(ValueError):
                run(broken)
        rows, _ = run(start + b"frame:5   pts:5120   pts_time:0.5\n"
                              b"lavfi.freezedetect.freeze_duration=1\n"
                              b"lavfi.freezedetect.freeze_end=1\n")
        self.assertEqual(rows[0]["video_duration_s"], "1.0")

    def test_a_duration_reported_as_unknown_is_still_reported(self):
        """Presence must be its own flag, not the value. A duration the decoder reported as its own
        NOPTS is `None` as a value but WAS reported, so testing the value for presence let a later
        one silently replace it -- and the reported unknown must survive into the row."""
        opened = (b"frame:0   pts:0   pts_time:0\nlavfi.freezedetect.freeze_start=0\n"
                  b"frame:5   pts:5120   pts_time:0.5\n")
        with self.assertRaises(ValueError):
            run(opened + b"lavfi.freezedetect.freeze_duration=NOPTS\n"
                         b"lavfi.freezedetect.freeze_duration=1\n"
                         b"lavfi.freezedetect.freeze_end=1\n")
        with self.assertRaises(ValueError):
            run(opened + b"lavfi.freezedetect.freeze_duration=1\n"
                         b"lavfi.freezedetect.freeze_duration=NOPTS\n"
                         b"lavfi.freezedetect.freeze_end=1\n")

        rows, report = run(opened + b"lavfi.freezedetect.freeze_duration=NOPTS\n"
                                    b"lavfi.freezedetect.freeze_end=1\n")
        self.assertEqual(rows[0]["video_duration_s"], "1.0")
        self.assertEqual(rows[0]["video_duration_source"],
                         "DERIVED_FROM_THE_REPORTED_START_AND_END_BECAUSE_THE_REPORTED_DURATION_"
                         "WAS_ITSELF_UNKNOWN")
        self.assertEqual(report["intervals_with_an_unknown_reported_duration"], 1)

        rows, report = run(opened + b"lavfi.freezedetect.freeze_duration=NOPTS\n"
                                    b"lavfi.freezedetect.freeze_end=NOPTS\n")
        self.assertEqual(rows[0]["video_duration_s"], "")
        self.assertEqual(rows[0]["video_duration_source"],
                         "NOT_AVAILABLE_THE_REPORTED_DURATION_WAS_ITSELF_UNKNOWN")
        self.assertEqual(report["intervals_with_an_unknown_reported_duration"], 1)

        rows, report = run(opened + b"lavfi.freezedetect.freeze_end=1\n")
        self.assertEqual(rows[0]["video_duration_source"],
                         "DERIVED_FROM_THE_REPORTED_START_AND_END")
        self.assertEqual(report["intervals_with_an_unknown_reported_duration"], 0)

    def test_the_declared_frame_count_must_agree_with_the_output_it_describes(self):
        """Only what the file itself shows is enforced: the analysis cannot have examined fewer
        frames than it emitted blocks for, and the frame index is zero-based. Nothing is derived
        and no count is invented."""
        document = (b"frame:0   pts:0   pts_time:0\nlavfi.black_start=0\n"
                    b"frame:99   pts:99   pts_time:9.9\nlavfi.black_end=9.9\n")
        _, report = run(document, analysed_frame_count=100)
        self.assertEqual(report["declared_count_consistency"],
                         "AT_OR_ABOVE_THE_FRAMES_THIS_OUTPUT_ITSELF_SHOWS")
        self.assertEqual(report["highest_frame_index"], 99)
        for short in (99, 2, 1):
            with self.subTest(count=short), self.assertRaises(ValueError):
                run(document, analysed_frame_count=short)

    def test_the_cues_reach_common_output_with_their_meaning(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "analysis.txt"
            source.write_bytes(OBSERVED)
            out = base / "converted"
            subprocess.run([os.sys.executable,
                            str(ROOT / "scripts/convert_saved_video_conditions.py"),
                            str(source), str(out), "--capture-time-us", "555",
                            "--decoder-status", "completed-without-reported-errors",
                            "--analysed-frame-count", "70",
                            "--threshold-basis", "operator-declared",
                            "--black-min-duration", "1", "--black-ratio-threshold", "0.98",
                            "--black-pixel-threshold", "0.10", "--freeze-noise", "0.001",
                            "--freeze-duration", "1"], check=True, capture_output=True)
            self.assertEqual((out / "source.metadata.txt").read_bytes(), OBSERVED)
            self.assertEqual(json.loads((out / "report.json").read_text())["black_intervals"], 1)
            if not reader:
                print("saved video condition common-reader integration NOT_RUN: reader not "
                      "configured")
                return
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(out / "observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        black, freeze, still = [o["fields"] for o in common["observations"]]
        self.assertEqual(black["video_record_kind"], "BLACK_INTERVAL")
        self.assertEqual((black["video_start_s"], black["video_end_s"]), (1.0, 4.0))
        self.assertEqual(freeze["video_duration_s"], 3.0)
        self.assertEqual(freeze["video_duration_source"], "REPORTED_BY_THE_DECODER")
        self.assertIsNone(still["video_end_s"])
        self.assertIsNone(still["video_duration_s"])
        self.assertEqual(still["video_end_disposition"],
                         "NO_END_REPORTED_BEFORE_THE_ANALYSIS_ENDED")
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        units = common["profile_units"]
        self.assertIn("never_a_fault", units["video_cue_basis"])
        self.assertIn("frozen_by_definition", units["video_cue_basis"])
        self.assertIn("is_NOT_evidence_that_no_such_interval_occurred",
                      units["video_decoder_status"])
        self.assertIn("never_reported_here_as_black_or_as_freeze", units["video_decoder_status"])
        self.assertIn("never_as_zero", units["video_start_s"])

    def test_an_operator_supplied_analysis_output_is_read_when_given(self):
        """Applicability against a real decoder run, when an operator supplies its saved output.

        Nothing is generated or decoded here: without the file the check is NOT_RUN by name.
        """
        supplied = os.environ.get("MUSUBI_VIDEO_CONDITION_ANALYSIS")
        if not supplied:
            self.skipTest("MUSUBI_VIDEO_CONDITION_ANALYSIS not supplied; the real-analysis check "
                          "is NOT_RUN, not passed")
        rows, report = run(Path(supplied).read_bytes())
        self.assertEqual(len(rows), report["black_intervals"] + report["freeze_intervals"])
        for row in rows:
            self.assertIn(row["video_end_disposition"],
                          ("REPORTED", "NO_END_REPORTED_BEFORE_THE_ANALYSIS_ENDED"))
            if row["video_end_disposition"] != "REPORTED":
                self.assertEqual(row["video_duration_s"], "")


if __name__ == "__main__":
    unittest.main()
