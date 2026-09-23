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
from scripts.convert_video_frames import convert, convert_detections, convert_progress

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
    def test_saved_deepstream_metadata_reuses_boxes_and_keeps_unavailable_score(self):
        def record(scale):
            return dict(exporter_version="9.0.0", coordinate_basis="pipeline_output_pixels",
                pipeline_width=320 * scale, pipeline_height=240 * scale, capture_time_us=1000,
                image_id="saved-frame", model_id="caller-model", future={"retained": True},
                objects=[dict(class_id=1, obj_label="container", object_id=2**64 - 2,
                    confidence=.8, rect_params=dict(left=10 * scale, top=20, width=30, height=40))])
        first, second = record(1), record(2)
        second["objects"][0].update(confidence=-0.10000000149011612, object_id=2**64 - 1)
        text = "\n".join(map(json.dumps, (first, second)))
        output, report = convert_detections(text, 0, deepstream=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[1]["box_x2_px"]), 50)
        self.assertEqual(rows[0]["track_id_text"], "u64:18446744073709551614")
        self.assertEqual(rows[1]["track_id_text"], "")
        self.assertEqual(rows[1]["model_score_reported"], "")
        self.assertEqual(rows[1]["model_score_status"], "UNAVAILABLE_SOURCE_SENTINEL")
        self.assertEqual(rows[1]["tracking_status"], "UNTRACKED_REPORTED")
        self.assertEqual(rows[0]["box_coordinate_basis"], "PIPELINE_LAST_COMPONENT_PIXELS")
        self.assertEqual(json.loads(report["source_records"][1]["original_line"]), second)
        with tempfile.TemporaryDirectory() as tmp:
            path, out = Path(tmp) / "in.jsonl", Path(tmp) / "out"
            path.write_text(text)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_video_frames.py"), str(path), "0", str(out),
                            "--deepstream-900"], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/video-frame-export/detection-profile.toml"),
                    str(out / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][0]["fields"]["track_id_text"], "u64:18446744073709551614")
                self.assertEqual(common["observations"][1]["fields"]["model_score_status"], "UNAVAILABLE_SOURCE_SENTINEL")
        for key, value in (("confidence", -.2), ("confidence", None), ("confidence", True),
                           ("object_id", 2**64), ("object_id", -1), ("class_id", -1),
                           ("rect_params", dict(left=0, top=0, width=-1, height=4))):
            bad = record(1); bad["objects"][0][key] = value
            with self.assertRaises(ValueError): convert_detections(json.dumps(bad), 0, deepstream=True)
        for key, value in (("coordinate_basis", "original_image"), ("exporter_version", "7.1"),
                           ("pipeline_width", 1), ("objects", None)):
            bad = record(1); bad[key] = value
            with self.assertRaises(ValueError): convert_detections(json.dumps(bad), 0, deepstream=True)
        with self.assertRaises(ValueError): convert_detections(json.dumps(first), 0)
        with self.assertRaises(ValueError): convert_detections('{"mdsversion":"1.0","object":{}}', 0, deepstream=True)

    def test_saved_detections_reuse_pixels_normalized_empty_and_common_output(self):
        def source(normalized):
            return dict(exporter_version="8.3.0", normalized=normalized, image_width=640 if normalized else 320,
                image_height=480 if normalized else 240, image_id="example-image", model_id="example-model",
                capture_time_us=1000, detections=[dict(name="box", **{"class": 2}, confidence=.75,
                    box=dict(x1=.1, y1=.2, x2=.5, y2=.6) if normalized else dict(x1=10, y1=20, x2=50, y2=60),
                    future={"uninterpreted": [1, 2]})])
        first, second = source(False), source(True)
        second["detections"][0]["track_id"] = 17
        empty = dict(first, detections=[])
        text = "\n".join(map(json.dumps, (first, second, empty)))
        output, report = convert_detections(text, 3)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), report["detections"], report["empty_records"]), (3, 2, 1))
        self.assertEqual(float(rows[0]["box_x1_px"]), 10)
        self.assertEqual(float(rows[1]["box_x1_px"]), 64)
        self.assertEqual(float(rows[1]["box_y2_px"]), 288)
        self.assertEqual(rows[1]["track_id_reported"], "17")
        self.assertEqual(rows[2]["detection_status"], "NO_DETECTIONS_REPORTED")
        self.assertEqual(rows[2]["box_x1_px"], "")
        self.assertEqual(json.loads(report["source_records"][0]["original_line"]), first)
        self.assertEqual(rows[0]["source_record_sha256"], "sha256:" + hashlib.sha256(json.dumps(first).encode()).hexdigest())
        many = dict(first, detections=[dict(first["detections"][0], track_id=i) for i in range(1000)])
        many_output, many_report = convert_detections(json.dumps(many), 3)
        self.assertEqual(many_report["detections"], 1000)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(many_output)))), 1000)
        with tempfile.TemporaryDirectory() as tmp:
            path, out = Path(tmp) / "in.jsonl", Path(tmp) / "out"
            path.write_text(text)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_video_frames.py"), str(path), "3", str(out),
                            "--detections-830"], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/video-frame-export/detection-profile.toml"),
                    str(out / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                self.assertEqual(common["observations"][1]["fields"]["box_x1_px"], 64)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for key, value in (("confidence", True), ("confidence", 1.1), ("class", -1), ("track_id", None),
                           ("box", {"x1": 0, "y1": 0, "x2": 1, "y2": 1, "x3": 0, "y3": 1}),
                           ("box", {"x1": 50, "y1": 0, "x2": 10, "y2": 10})):
            bad = source(False); bad["detections"][0][key] = value
            with self.assertRaises(ValueError): convert_detections(json.dumps(bad), 0)
        for key, value in (("exporter_version", "8.2.0"), ("normalized", "false"), ("image_width", 0),
                           ("capture_time_us", -1), ("image_id", ""), ("detections", {})):
            bad = source(False); bad[key] = value
            with self.assertRaises(ValueError): convert_detections(json.dumps(bad), 0)
        bad = source(True); bad["detections"][0]["box"]["x2"] = 1.1
        with self.assertRaises(ValueError): convert_detections(json.dumps(bad), 0)
        bad = source(False); del bad["detections"][0]["box"]
        with self.assertRaises(ValueError): convert_detections(json.dumps(bad), 0)

    def test_compact_stream_binding_practical_count_and_common_output(self):
        data = document()
        count = 30000
        data["streams"][0].update(nb_read_frames=str(count), future_stream_metadata="retained" * 150)
        first = data["frames"][0]
        data["frames"] = [dict(first, pts=i * 3003, future_text="unknown" * 12) for i in range(count)]
        text = json.dumps(data)
        with self.assertRaisesRegex(ValueError, "converted output exceeds bound"):
            convert(text, 0)
        output, report = convert(text, 0, compact=True)
        self.assertGreater(len(output.encode()), 16 * 1024 * 1024)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), count)
        stream_bytes = bytes.fromhex(report["selected_stream_hex"][4:])
        digest = "sha256:" + hashlib.sha256(stream_bytes).hexdigest()
        self.assertEqual(report["selected_stream_sha256"], digest)
        self.assertEqual(json.loads(stream_bytes), data["streams"][0])
        self.assertTrue(all(row["source_stream_hex"] == "" and row["source_stream_sha256"] == digest for row in rows))
        self.assertEqual(report["timed_video_frames"], count)
        self.assertEqual(json.loads(bytes.fromhex(rows[-1]["source_frame_hex"][4:])), data["frames"][-1])
        self.assertEqual(rows[-1]["record_time_us"], str((count - 1) * 3003 * 1000000 // 90000))
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "frames.csv"; source.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/video-frame-export/profile.toml"),
                    str(source)], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], count)
                self.assertEqual(len(common["observations"]), count)
                self.assertEqual(common["observations"][-1]["fields"]["source_stream_sha256"], digest)
                self.assertEqual(common["observations"][-1]["clock_basis"], "Unknown")

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

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_common_video_channel_preserves_metadata_and_does_not_create_utc(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frames.csv"
            source.write_text(convert(json.dumps(document()), 0)[0])
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/video-frame-export/profile.toml"),
                str(source), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(len(common["observations"]), 2)
            for record in common["observations"]:
                self.assertEqual(record["channel"], "video")
                self.assertEqual(record["fields"]["width_px"], 320)
                self.assertIsNone(record["anchor_unix_us"])
                self.assertEqual(record["clock_basis"], "Unknown")


DROP_BLOCK = """frame=22
fps=0.00
stream_0_0_q=25.0
bitrate=  15.7kbits/s
total_size=3933
out_time_us=2000000
out_time_ms=2000000
out_time=00:00:02.000000
dup_frames=0
drop_frames=38
speed= 430x
progress=end
"""

DUP_BLOCK = DROP_BLOCK.replace("frame=22", "frame=50").replace("dup_frames=0", "dup_frames=30") \
    .replace("drop_frames=38", "drop_frames=0").replace("total_size=3933", "total_size=4993")


def progress_text(*blocks):
    return "".join(blocks)


class FfmpegProgressCounterTests(unittest.TestCase):
    """Reported processing counters; never a camera drop or a packet loss claim."""

    def rows(self, text):
        output, report = convert_progress(text)
        return list(csv.DictReader(io.StringIO(output))), report, output

    def test_reported_drops_reach_common_output_with_their_processing_basis(self):
        rows, report, output = self.rows(DROP_BLOCK)
        row = rows[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(row["drop_frames_reported"], "38")
        self.assertEqual(row["dup_frames_reported"], "0")
        self.assertEqual(row["output_frames_reported"], "22")
        self.assertEqual(row["record_time_us"], "2000000")
        self.assertEqual(row["out_time_us_reported"], "2000000")
        self.assertEqual(row["out_time_text_reported"], "00:00:02.000000")
        self.assertEqual(row["progress_state"], "end")
        self.assertEqual(row["frame_counter_basis"],
                         "PROCESSING_FRAME_RATE_CONVERSION_NOT_CAPTURE_OR_TRANSPORT_LOSS")
        self.assertEqual(row["counter_scope"], "FIRST_VIDEO_OUTPUT_STREAM_NOT_AGGREGATED")
        self.assertEqual(report["unknown_keys"], ["stream_0_0_q"])
        self.assertEqual(report["terminal_state"], "END")
        self.assertEqual(report["source_sha256"],
                         "sha256:" + hashlib.sha256(DROP_BLOCK.encode()).hexdigest())
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/video-frame-export/progress-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["drop_frames_reported"], 38)
            self.assertEqual(fields["output_frames_reported"], 22)
            self.assertEqual(common["observations"][0]["channel"], "video")
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")

    def test_a_different_authored_run_needs_no_implementation_change(self):
        rows, report, _ = self.rows(DUP_BLOCK)
        self.assertEqual(rows[0]["dup_frames_reported"], "30")
        self.assertEqual(rows[0]["drop_frames_reported"], "0")
        self.assertEqual(rows[0]["output_frames_reported"], "50")
        self.assertEqual(report["timed_blocks"], 1)
        growing = progress_text(DROP_BLOCK.replace("progress=end", "progress=continue"),
                                DROP_BLOCK.replace("frame=22", "frame=40")
                                          .replace("drop_frames=38", "drop_frames=70")
                                          .replace("=2000000", "=4000000"))
        rows, report, _ = self.rows(growing)
        self.assertEqual([r["drop_frames_reported"] for r in rows], ["38", "70"])
        self.assertEqual([r["progress_state"] for r in rows], ["continue", "end"])
        self.assertEqual(report["blocks"], 2)

    def test_untimed_and_interrupted_blocks_stay_accounted(self):
        untimed = DROP_BLOCK.replace("out_time_us=2000000", "out_time_us=N/A") \
            .replace("out_time_ms=2000000", "out_time_ms=N/A") \
            .replace("out_time=00:00:02.000000", "out_time=N/A") \
            .replace("progress=end", "progress=continue")
        later = DROP_BLOCK.replace("frame=22", "frame=40").replace("drop_frames=38", "drop_frames=70")
        rows, report, _ = self.rows(progress_text(untimed, later))
        self.assertEqual(len(rows), 1)  # the untimed block is not given a false time
        self.assertEqual((report["blocks"], report["timed_blocks"], report["untimed_blocks"]),
                         (2, 1, 1))
        self.assertEqual(report["retained_untimed_blocks"][0]["reason"], "NO_OUT_TIME")
        partial = later.split("dup_frames")[0]
        interrupted = progress_text(DROP_BLOCK.replace("progress=end", "progress=continue"), partial)
        rows, report, _ = self.rows(interrupted)
        self.assertEqual(len(rows), 1)  # the incomplete block invents no counter and no time
        self.assertEqual(report["terminal_state"], "INCOMPLETE_NO_TERMINAL_BLOCK")
        self.assertEqual((report["blocks"], report["complete_blocks"]), (2, 1))
        incomplete = report["incomplete_final_block"]
        self.assertEqual(incomplete["reason"], "NO_TERMINAL_LINE_INCOMPLETE_FINAL_BLOCK")
        self.assertNotIn("dup_frames", incomplete["keys_present"])
        self.assertEqual(bytes.fromhex(incomplete["source_block_hex"][4:]).decode(), partial)
        with self.assertRaises(ValueError):
            convert_progress(progress_text(partial, DROP_BLOCK))
        with self.assertRaises(ValueError):
            convert_progress(partial)

    def test_recording_stop_states_reach_common_without_a_physical_claim(self):
        """A reported end, a truncated final record and no terminal record are three answers."""
        continued = DROP_BLOCK.replace("progress=end", "progress=continue")
        second = DROP_BLOCK.replace("frame=22", "frame=40").replace("drop_frames=38", "drop_frames=70") \
            .replace("=2000000", "=4000000").replace("00:00:02", "00:00:04")
        terminated = progress_text(continued, second)
        truncated = progress_text(continued, second.split("dup_frames")[0])
        for text, expected, rows_expected, trailing in (
                (terminated, "REPORTED_END_BLOCK_AS_THE_LAST_SAVED_BLOCK", 2, 0),
                (truncated, "NO_END_BLOCK_FINAL_SAVED_BLOCK_TRUNCATED", 1,
                 len(second.split("dup_frames")[0].encode()))):
            rows, report, output = self.rows(text)
            self.assertEqual(len(rows), rows_expected)
            self.assertEqual({row["recording_stop_disposition"] for row in rows}, {expected})
            self.assertEqual(report["recording_stop_disposition"], expected)
            self.assertEqual({int(row["unterminated_final_block_bytes"]) for row in rows}, {trailing})
            self.assertEqual(report["blocks"], 2)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "stop.csv"
                    path.write_text(output, encoding="utf-8")
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/video-frame-export/progress-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], rows_expected)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["recording_stop_disposition"], expected)
                self.assertEqual(fields["unterminated_final_block_bytes"], trailing)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                declared = common["profile_units"]
                for column in ("recording_stop_disposition", "unterminated_final_block_bytes"):
                    self.assertTrue(declared.get(column), f"{column} must declare its meaning")
                self.assertIn("never_evidence_that_a_camera_stopped_capturing",
                              declared["recording_stop_disposition"])
                self.assertIn("missing_end_block_is_not_a_failure",
                              declared["recording_stop_disposition"])
                self.assertIn("gets_no_row", declared["unterminated_final_block_bytes"])

        rows, report, _ = self.rows(progress_text(continued, second.replace("progress=end", "progress=continue")))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["recording_stop_disposition"] for row in rows},
                         {"NO_END_BLOCK_LAST_SAVED_BLOCK_WAS_CONTINUE"})
        self.assertEqual({int(row["unterminated_final_block_bytes"]) for row in rows}, {0})
        self.assertEqual(report["terminal_state"], "NO_TERMINAL_BLOCK_LAST_BLOCK_WAS_CONTINUE")
        self.assertIsNone(report["incomplete_final_block"])

        rows, report, _ = self.rows(progress_text(DROP_BLOCK, second.split("dup_frames")[0]))
        self.assertEqual({row["recording_stop_disposition"] for row in rows},
                         {"REPORTED_END_BLOCK_WITH_LATER_SAVED_BLOCKS"})
        self.assertEqual(report["terminal_state"], "END_BEFORE_LAST_BLOCK")

        untimed_end = DROP_BLOCK.replace("out_time_us=2000000", "out_time_us=N/A") \
            .replace("out_time_ms=2000000", "out_time_ms=N/A") \
            .replace("out_time=00:00:02.000000", "out_time=N/A")
        rows, report, _ = self.rows(progress_text(continued, untimed_end))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["progress_state"], "continue")
        self.assertEqual(rows[0]["recording_stop_disposition"],
                         "REPORTED_END_BLOCK_AS_THE_LAST_SAVED_BLOCK")
        self.assertEqual((report["terminal_state"], report["untimed_blocks"]), ("END", 1))

        ended, _, _ = self.rows(terminated)
        cut, _, _ = self.rows(truncated)
        self.assertEqual(ended[0]["output_frames_reported"], cut[0]["output_frames_reported"])
        self.assertNotEqual(ended[0]["recording_stop_disposition"], cut[0]["recording_stop_disposition"])

    def test_the_digest_and_block_bytes_bind_the_exact_input(self):
        crlf = DROP_BLOCK.replace("\n", "\r\n")
        rows, report, _ = self.rows(crlf)
        block_bytes = bytes.fromhex(rows[0]["source_block_hex"][4:])
        self.assertEqual(block_bytes, crlf.encode())
        self.assertIn(b"\r\n", block_bytes)
        self.assertEqual(int(rows[0]["source_block_offset"]), 0)
        self.assertEqual(int(rows[0]["source_block_length"]), len(crlf.encode()))
        self.assertEqual(report["unknown_keys"], ["stream_0_0_q"])
        self.assertIn(b"stream_0_0_q=25.0", block_bytes)
        self.assertEqual(report["source_sha256"],
                         "sha256:" + hashlib.sha256(crlf.encode()).hexdigest())
        self.assertNotEqual(report["source_sha256"], self.rows(DROP_BLOCK)[1]["source_sha256"])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "progress.txt").write_bytes(crlf.encode())
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_video_frames.py"),
                            str(base / "progress.txt"), "0", str(base / "out"), "--progress-81"],
                           check=True, capture_output=True)
            written = json.loads((base / "out/report.json").read_text())
            self.assertEqual(written["source_sha256"], report["source_sha256"])

    def test_an_unknown_terminator_is_not_a_known_block(self):
        for terminator in ("progress=paused", "progress=", "progress=END"):
            with self.assertRaises(ValueError):
                convert_progress(DROP_BLOCK.replace("progress=end", terminator))
        with self.assertRaises(ValueError):
            convert_progress(DROP_BLOCK.replace("out_time_ms=2000000", "out_time_ms=2000"))
        self.assertEqual(len(self.rows(DROP_BLOCK)[0]), 1)

    def test_malformed_duplicate_and_decreasing_counters_refuse(self):
        for broken in (DROP_BLOCK.replace("drop_frames=38", "drop_frames=-1"),
                       DROP_BLOCK.replace("drop_frames=38", "drop_frames=9" * 20),
                       DROP_BLOCK.replace("drop_frames=38", "drop_frames=3.5"),
                       DROP_BLOCK.replace("frame=22", "frame=22\nframe=23"),
                       DROP_BLOCK.replace("frame=22", "frame"),
                       DROP_BLOCK.replace("out_time_us=2000000", "out_time_us=-5"),
                       "", "\n\n"):
            with self.assertRaises(ValueError):
                convert_progress(broken)
        decreasing = progress_text(DROP_BLOCK.replace("progress=end", "progress=continue"),
                                   DROP_BLOCK.replace("drop_frames=38", "drop_frames=7")
                                             .replace("=2000000", "=4000000"))
        with self.assertRaises(ValueError):
            convert_progress(decreasing)
        with self.assertRaises(ValueError):
            convert_progress(DROP_BLOCK.replace("out_time_us=2000000", "out_time_us=N/A")
                                       .replace("out_time_ms=2000000", "out_time_ms=N/A"))

    def test_the_progress_mode_does_not_mix_with_the_frame_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "progress.txt").write_text(DROP_BLOCK)
            command = [sys.executable, str(ROOT / "scripts/convert_video_frames.py"),
                       str(base / "progress.txt"), "0", str(base / "out"), "--progress-81"]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(json.loads((base / "out/report.json").read_text())["blocks"], 1)
            mixed = subprocess.run(command[:-1] + ["--progress-81", "--compact", str()],
                                   capture_output=True)
            self.assertEqual(mixed.returncode, 2)



if __name__ == "__main__":
    unittest.main()
