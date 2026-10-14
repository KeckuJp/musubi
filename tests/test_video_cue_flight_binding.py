"""Authored filtered/unfiltered ffprobe8.1 exports and Plane ATT; no media, filter or real log."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_video_cue_flight_binding as cue
from scripts import convert_video_detector_intervals as detector
from scripts import convert_video_flight_binding as flight_binding
from scripts.check_saved_video_frames import digest

ROOT = Path(__file__).resolve().parents[1]
# Hand-computed av_ts2timestr texts at 1/60000.
TAGS = {3: {"lavfi.black_start": "0.05"}, 6: {"lavfi.black_end": "0.1"},
        40: {"lavfi.freezedetect.freeze_start": "0.5"},
        50: {"lavfi.freezedetect.freeze_duration": "0.333333", "lavfi.freezedetect.freeze_end": "0.833333"},
        55: {"lavfi.black_start": "0.916667"}}


def frames(stream, pts, tags=None):
    return [{"media_type": "video", "stream_index": stream, "pts": p, "width": 3840, "height": 2160,
             "key_frame": int(i == 0), "pict_type": "I" if i == 0 else "P",
             **({"tags": tags[i]} if tags and i in tags else {})} for i, p in enumerate(pts)]


def document(stream, time_base, pts, tags=None, audio_first=False):
    streams = [{"index": stream, "codec_type": "video", "time_base": time_base, "nb_read_frames": str(len(pts))}]
    head = []
    if audio_first:
        streams.append({"index": 1 - stream, "codec_type": "audio", "time_base": "1/48000"})
        head = [{"media_type": "audio", "stream_index": 1 - stream, "pts": 0}]
    return json.dumps({"program_version": {"version": "8.1"}, "streams": streams,
                       "frames": head + frames(stream, pts, tags)}).encode()


def inputs(pts=None, filtered_pts=None, filtered_tb="1/60000", tags=TAGS, session="fpv-s1"):
    pts = pts or [i * 1000 for i in range(60)]
    unfiltered = document(0, "1/60000", pts)
    # A different artifact: an audio frame comes first, so every filtered index is one higher.
    filtered = document(1, filtered_tb, filtered_pts or pts, tags, audio_first=True)
    flight = ("TimeUS,Roll,Pitch,Yaw\n" + "".join(f"{5000 + i * 1000},1,2,3\n" for i in range(1200))).encode()
    flight_decl = json.dumps(dict(schema="video-flight-clock-v1", export_sha256=flight_binding.digest(unfiltered),
        flight_sha256=flight_binding.digest(flight), relation=flight_binding.RELATION, stream_index=0,
        anchor_frame_index=0, anchor_flight_index=1, valid_start_pts=0, valid_end_pts=10**6,
        max_abs_delta_ticks=10**6, session_id="fpv-s1", camera_id="cam", flight_entity_id="plane",
        media_clock_id="pts", flight_clock_id="boot", citation="authored")).encode()
    detector_decl = json.dumps(dict(schema=detector.SCHEMA, export_sha256=digest(filtered), stream_index=1,
        detectors=["blackdetect", "freezedetect"], filter_graph="movie=in.mp4,blackdetect=d=0,freezedetect=d=0.2",
        session_id=session, camera_id="cam", citation="authored", unfiltered_export_sha256=digest(unfiltered),
        unfiltered_relation=detector.LINK)).encode()
    relation = dict(schema=cue.SCHEMA, relation=cue.RELATION, filtered_export_sha256=digest(filtered),
        unfiltered_export_sha256=digest(unfiltered), flight_sha256=digest(flight),
        detector_declaration_sha256=digest(detector_decl), flight_declaration_sha256=digest(flight_decl),
        citation="authored same-media relation")
    return [filtered, unfiltered, flight, detector_decl, flight_decl, json.dumps(relation).encode()]


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class CueFlightBindingTests(unittest.TestCase):
    def test_shifted_indices_sixty_fps_and_common_output(self):
        text, report, profile = cue.convert(*inputs(), rational_clock=True)
        black, freeze, open_black = rows(text)
        # Filtered index 4/7 joins unfiltered 3/6 by exact PTS, never by index.
        self.assertEqual((black["start_frame_index"], black["start_unfiltered_frame_index"],
                          black["start_derived_flight_time_us_exact"], black["end_frame_index"],
                          black["end_unfiltered_frame_index"], black["end_derived_flight_time_us"]),
                         ("4", "3", "ratio:55000/1", "7", "6", "105000"))
        self.assertEqual((freeze["start_tag_frame_index"], freeze["start_frame_index"],
                          freeze["start_unfiltered_frame_index"], freeze["start_derived_flight_time_us"]),
                         ("41", "31", "30", "505000"))
        # 50000 ticks at 1/60000 is a fractional microsecond: ratio only, floor is not an exact sample.
        self.assertEqual((freeze["end_flight_connection_status"], freeze["end_derived_flight_time_us"],
                          freeze["end_derived_flight_time_us_exact"], freeze["end_derived_flight_time_us_floor"]),
                         ("CLOCK_CONNECTED_SUBMICROSECOND_NO_EXACT_SAMPLE", "", "ratio:2515000/3", "838333"))
        self.assertEqual((open_black["interval_status"], open_black["end_endpoint_match"],
                          open_black["end_derived_flight_time_us_exact"]),
                         ("OPEN_NO_END_TAG_IN_EXPORT", "NO_ENDPOINT_PTS", ""))
        self.assertEqual(report["precondition"], cue.PRECONDITION)
        self.assertIn("video_cue_flight_declared_rational_v1", profile)
        strict = rows(cue.convert(*inputs())[0])[1]
        self.assertEqual((strict["end_flight_connection_status"], strict["end_derived_flight_time_us_exact"]),
                         ("WITHHELD_SUBMICROSECOND_MAPPING", ""))
        reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
        self.assertTrue(reader)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "o.csv").write_text(text)
            (path / "p.toml").write_text(profile)
            result = subprocess.run([reader, str(path / "p.toml"), str(path / "o.csv"), "--allow-equal-time"],
                                    capture_output=True, text=True, check=True)
        observations = json.loads(result.stdout)["observations"]
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
        self.assertEqual(observations[1]["fields"]["end_derived_flight_time_us_exact"], "ratio:2515000/3")
        self.assertEqual({o["fields"]["session_id_hex"] for o in observations}, {"hex:" + b"fpv-s1".hex()})

    def test_ambiguous_and_unresolved_endpoints_stay_accounted(self):
        pts = [0, 1000, 1000, 2000, 3000]
        tags = {1: {"lavfi.black_start": "0.0166667"}, 3: {"lavfi.black_end": "0.0333333"},
                4: {"lavfi.freezedetect.freeze_start": "0.0166667"}}
        black, freeze = rows(cue.convert(*inputs(pts=pts, tags=tags), rational_clock=True)[0])
        self.assertEqual((black["start_endpoint_match"], black["start_derived_flight_time_us_exact"],
                          black["end_endpoint_match"]), ("AMBIGUOUS_DUPLICATE_PTS", "", "UNIQUE_EXACT_PTS"))
        self.assertEqual((freeze["start_frame_resolution"], freeze["start_endpoint_match"]),
                         ("UNRESOLVED_NO_UNIQUE_EARLIER_FRAME", "NO_ENDPOINT_PTS"))

    def test_retimed_or_unbound_inputs_refuse(self):
        base = [i * 1000 for i in range(60)]
        changes = {
            "changed timebase": dict(filtered_tb="1/90000", filtered_pts=[i * 1500 for i in range(60)]),
            "shifted pts": dict(filtered_pts=[p + 1000 for p in base]),
            "dropped frame": dict(filtered_pts=base[:59]),
            "different session": dict(session="other"),
        }
        for name, change in changes.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                cue.convert(*inputs(tags={}, **change))
        good = inputs(tags={})
        for key, value in (("relation", "inferred"), ("unfiltered_export_sha256", digest(b"x")),
                           ("detector_declaration_sha256", digest(b"y")), ("extra", 1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                cue.convert(*good[:5], json.dumps(dict(json.loads(good[5]), **{key: value})).encode())
        unlinked = json.loads(good[3])
        unlinked.update(unfiltered_export_sha256=None, unfiltered_relation=None)
        raw = json.dumps(unlinked).encode()
        relation = dict(json.loads(good[5]), detector_declaration_sha256=digest(raw))
        with self.assertRaises(ValueError):
            cue.convert(*good[:3], raw, good[4], json.dumps(relation).encode())

    def test_cli_retains_originals_and_refuses_existing_output(self):
        raw = inputs()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            files = []
            for index, data in enumerate(raw):
                (path / f"{index}.in").write_bytes(data)
                files.append(str(path / f"{index}.in"))
            command = ["python3", "-m", "scripts.convert_video_cue_flight_binding", *files,
                       str(path / "out"), "--rational-clock"]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            self.assertEqual((path / "out/source-filtered-export").read_bytes(), raw[0])
            self.assertEqual((path / "out/observations.csv").read_text(),
                             cue.convert(*raw, rational_clock=True)[0])
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
