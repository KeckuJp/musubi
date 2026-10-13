"""Authored ffprobe8.1 frame tags shaped by the pinned blackdetect/freezedetect sources.

No filter, model or media runs here. Tag texts are written by hand from the pinned
av_ts_make_time_string2 rules, not produced by the converter's own formatter.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_video_detector_intervals as detector
from scripts.check_saved_video_frames import digest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/video-frame-export/detector-interval-profile.toml"
UNFILTERED = "sha256:" + "ab" * 32


def export(time_base, pts_list, tags, *, stream=0, extra_audio=False):
    frames = []
    if extra_audio:
        frames.append({"media_type": "audio", "stream_index": 1 - stream, "pts": 0,
                       "tags": {"lavfi.black_start": "0"}})
    for index, pts in enumerate(pts_list):
        frame = {"media_type": "video", "stream_index": stream, "pts": pts, "width": 64, "height": 48,
                 "key_frame": int(index == 0), "pict_type": "I" if index == 0 else "P"}
        if index in tags:
            frame["tags"] = tags[index]
        frames.append(frame)
    streams = [{"index": stream, "codec_type": "video", "time_base": time_base,
                "nb_read_frames": str(len(pts_list))}]
    if extra_audio:
        streams.append({"index": 1 - stream, "codec_type": "audio", "time_base": "1/48000"})
    return json.dumps({"program_version": {"version": "8.1"}, "streams": streams,
                       "frames": frames}).encode()


def declare(raw, *, stream=0, detectors=("blackdetect", "freezedetect"), session="session-a", link=True):
    return json.dumps(dict(schema=detector.SCHEMA, export_sha256=digest(raw), stream_index=stream,
        detectors=list(detectors), filter_graph="movie=in.mp4,blackdetect=d=0,freezedetect=d=0.2",
        session_id=session, camera_id="fpv-camera", citation="authored tags, not a detector run",
        unfiltered_export_sha256=UNFILTERED if link else None,
        unfiltered_relation=detector.LINK if link else None)).encode()


def session_a():
    # 1/1000: black on frames 2-3 (end tag on 4), freeze referenced at frame 5 but tagged on 7,
    # ending at 9, and a black start on 8 that never ends in the export.
    tags = {2: {"lavfi.black_start": "0.2"}, 4: {"lavfi.black_end": "0.4"},
            7: {"lavfi.freezedetect.freeze_start": "0.5"},
            8: {"lavfi.black_start": "0.8"},
            9: {"lavfi.freezedetect.freeze_duration": "0.4", "lavfi.freezedetect.freeze_end": "0.9"}}
    return export("1/1000", [i * 100 for i in range(10)], tags)


def session_b():
    # 1/90000 at 60 fps, stream 1 beside an audio stream: an end without a start, a freeze whose
    # start and end tags share a frame, and keys outside the two filters. The audio frame comes first,
    # so export frame indices are the video ordinals plus one.
    tags = {1: {"lavfi.black_end": "0.0166667", "lavfi.scene_score": "0.1"},
            3: {"lavfi.freezedetect.freeze_start": "0", "lavfi.freezedetect.freeze_duration": "0.05",
                "lavfi.freezedetect.freeze_end": "0.05", "lavfi.freezedetect.future": "x"}}
    return export("1/90000", [i * 1500 for i in range(5)], tags, stream=1, extra_audio=True)


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class DetectorIntervalTests(unittest.TestCase):
    def test_pinned_time_text_rules(self):
        for (ts, num, den), text in {(0, 1, 90000): "0", (90000, 1, 90000): "1", (1500, 1, 90000): "0.0166667",
                                     (1001, 1, 60000): "0.0166833", (4500, 1, 90000): "0.05",
                                     (135000, 1, 90000): "1.5", (3600000, 1, 1000): "3600",
                                     (500, 1, 10**6): "0.0005"}.items():
            self.assertEqual(detector.time_text(ts, num, den), text)
        self.assertEqual(detector.duration_us("0.4"), 400000)
        self.assertIsNone(detector.duration_us("0.4000001"))

    def test_two_sessions_intervals_and_common_output(self):
        raw = session_a()
        text, report = detector.convert(raw, declare(raw))
        black0, freeze0, black1 = rows(text)
        self.assertEqual((black0["detector"], black0["interval_status"], black0["start_frame_index"],
                          black0["start_pts"], black0["end_frame_index"], black0["end_pts_exclusive"],
                          black0["interval_ticks"]), ("blackdetect", "CLOSED", "2", "200", "4", "400", "200"))
        # The freeze start tag rides on frame 7 but names frame 5: the interval starts at 5.
        self.assertEqual((freeze0["start_tag_frame_index"], freeze0["start_frame_index"], freeze0["start_pts"],
                          freeze0["start_frame_resolution"], freeze0["end_pts_exclusive"],
                          freeze0["freeze_duration_us_from_reported_text"], freeze0["interval_ticks"]),
                         ("7", "5", "500", "UNIQUE_EARLIER_FRAME_EXACT_REPRINT", "900", "400000", "400"))
        self.assertEqual((black1["interval_status"], black1["detector_interval_index"], black1["end_pts_exclusive"]),
                         ("OPEN_NO_END_TAG_IN_EXPORT", "1", ""))
        self.assertEqual(black0["min_duration_basis"], "TAGS_EMITTED_REGARDLESS_OF_BLACK_MIN_DURATION")
        self.assertEqual(report["counts"], {"blackdetect:CLOSED": 1, "freezedetect:CLOSED": 1,
                                            "blackdetect:OPEN_NO_END_TAG_IN_EXPORT": 1})
        self.assertEqual(len(report["tagged_frames"]), 5)
        self.assertEqual(black0["unfiltered_relation"], detector.LINK)

        raw = session_b()
        text, report = detector.convert(raw, declare(raw, stream=1, session="session-b", link=False))
        black, freeze = rows(text)
        self.assertEqual((black["interval_status"], black["start_frame_resolution"], black["end_frame_index"],
                          black["end_pts_exclusive"]), ("END_WITHOUT_START_TAG_IN_EXPORT", "NO_START_TAG_IN_EXPORT", "2", "1500"))
        self.assertEqual((freeze["interval_status"], freeze["start_frame_index"], freeze["start_tag_frame_index"],
                          freeze["end_frame_index"], freeze["interval_ticks"], freeze["freeze_duration_us_from_reported_text"]),
                         ("CLOSED", "1", "4", "4", "4500", "50000"))
        self.assertEqual(report["unknown_tag_keys"], {"lavfi.scene_score": 1, "lavfi.freezedetect.future": 1})
        self.assertEqual(report["tagged_unselected_frames"], 1)
        self.assertEqual(black["unfiltered_export_sha256"], "")

        reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
        self.assertTrue(reader)
        for raw, stream, session in ((session_a(), 0, "session-a"), (session_b(), 1, "session-b")):
            text, _ = detector.convert(raw, declare(raw, stream=stream, session=session))
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "o.csv"
                path.write_text(text)
                result = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                        capture_output=True, text=True, check=True)
            observations = json.loads(result.stdout)["observations"]
            self.assertEqual(len(observations), len(rows(text)))
            self.assertTrue(all(o["clock_basis"] == "Unknown" and o["channel"] == "video" for o in observations))
            self.assertEqual({o["fields"]["session_id_hex"] for o in observations}, {"hex:" + session.encode().hex()})
            self.assertEqual({o["fields"]["detector_basis"] for o in observations}, {detector.BASIS})

    def test_none_reported_unresolved_start_and_refusals(self):
        raw = export("1/1000", [0, 100, 200], {})
        text, _ = detector.convert(raw, declare(raw))
        self.assertEqual([(r["detector"], r["interval_status"]) for r in rows(text)],
                         [("blackdetect", "NO_INTERVAL_REPORTED"), ("freezedetect", "NO_INTERVAL_REPORTED")])
        # Two earlier frames share the named pts: the start frame is not chosen.
        raw = export("1/1000", [0, 0, 100], {2: {"lavfi.freezedetect.freeze_start": "0"}})
        row = next(r for r in rows(detector.convert(raw, declare(raw))[0]) if r["detector"] == "freezedetect")
        self.assertEqual((row["start_frame_resolution"], row["start_pts"]), ("UNRESOLVED_NO_UNIQUE_EARLIER_FRAME", ""))
        # Printable references with no frame in the export (outside it, or NOPTS) stay unresolved.
        for value in ("5", "NOPTS", "-0.5"):
            raw = export("1/1000", [0, 100, 200], {2: {"lavfi.freezedetect.freeze_start": value}})
            row = next(r for r in rows(detector.convert(raw, declare(raw))[0]) if r["detector"] == "freezedetect")
            self.assertEqual((row["start_frame_resolution"], row["start_time_text_reported"]),
                             ("UNRESOLVED_NO_UNIQUE_EARLIER_FRAME", value))
        # Duration microseconds are read back from rounded text, never presented as the filter's counter.
        for text, micro, basis in (("0.2", "200000", detector.DURATION_TYPED),
                                   ("0.0000005", "", detector.DURATION_UNTYPED)):
            raw = export("1/1000", [0, 100, 200], {2: {"lavfi.freezedetect.freeze_end": "0.2",
                                                       "lavfi.freezedetect.freeze_duration": text}})
            row = next(r for r in rows(detector.convert(raw, declare(raw))[0]) if r["detector"] == "freezedetect")
            self.assertEqual((row["freeze_duration_text_reported"], row["freeze_duration_us_from_reported_text"],
                              row["freeze_duration_precision_basis"], row["interval_ticks"]), (text, micro, basis, ""))
        refusals = {
            "time differs from carrier": {2: {"lavfi.black_start": "0.3"}},
            "start and end share a frame": {2: {"lavfi.black_start": "0.2", "lavfi.black_end": "0.2"}},
            "repeated start": {1: {"lavfi.black_start": "0.1"}, 2: {"lavfi.black_start": "0.2"}},
            "end without duration": {2: {"lavfi.freezedetect.freeze_end": "0.2"}},
            "non-string value": {2: {"lavfi.black_start": 0.2}},
            "malformed freeze start": {2: {"lavfi.freezedetect.freeze_start": "garbage"}},
            "malformed duration": {2: {"lavfi.freezedetect.freeze_end": "0.2",
                                       "lavfi.freezedetect.freeze_duration": "-1"}},
        }
        for name, tags in refusals.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                raw = export("1/1000", [0, 100, 200], tags)
                detector.convert(raw, declare(raw))
        raw = export("1/1000", [0, 100, 200], {2: {"lavfi.freezedetect.freeze_start": "0"}})
        for change in (dict(detectors=["blackdetect"]), dict(export_sha256="sha256:" + "0" * 64),
                       dict(unfiltered_export_sha256=digest(raw)), dict(unfiltered_relation=None),
                       dict(detectors=[]), dict(stream_index=True), dict(session_id=" ")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                detector.convert(raw, json.dumps(dict(json.loads(declare(raw)), **change)).encode())

    def test_cli_retains_inputs_and_refuses_existing_output(self):
        raw = session_a()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "e.json").write_bytes(raw)
            (path / "d.json").write_bytes(declare(raw))
            command = ["python3", "-m", "scripts.convert_video_detector_intervals",
                       str(path / "e.json"), str(path / "d.json"), str(path / "out")]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            self.assertEqual((path / "out/source-export.json").read_bytes(), raw)
            self.assertEqual((path / "out/observations.csv").read_text(),
                             detector.convert(raw, declare(raw))[0])
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
