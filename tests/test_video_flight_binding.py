import csv
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_video_flight_binding import convert, RELATION, digest


def inputs(shift=0, denominator=1000):
    export = json.dumps({"program_version": {"version": "8.1"}, "streams": [{
        "index": 0, "codec_type": "video", "time_base": f"1/{denominator}", "nb_read_frames": "4"}],
        "frames": [{"media_type": "video", "stream_index": 0, "pts": pts, "width": 640,
                    "height": 480, "key_frame": 1, "pict_type": "I", "extra": shift}
                   for pts in (0, 1000, 2000, "N/A")]}).encode()
    flight = f"TimeUS,Roll,Pitch,Yaw\n{100+shift},1,2,3\n{1000100+shift},2,3,4\n{3000100+shift},3,4,5\n".encode()
    declaration = dict(schema="video-flight-clock-v1", export_sha256=digest(export), flight_sha256=digest(flight),
        relation=RELATION, stream_index=0, anchor_frame_index=0, anchor_flight_index=1,
        valid_start_pts=0, valid_end_pts=3000, max_abs_delta_ticks=3000,
        session_id=f"session-{shift}", camera_id="camera", flight_entity_id="plane",
        media_clock_id="PTS", flight_clock_id="boot", citation="authored assertion, not measured sync")
    return export, flight, declaration


class VideoFlightBindingTests(unittest.TestCase):
    def test_two_authored_sessions_common(self):
        for shift in (0, 87654):
            export, flight, declaration = inputs(shift)
            text, report, profile, _, _ = convert(export, flight, json.dumps(declaration).encode())
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual([r["derived_flight_time_us"] for r in rows], [str(100+shift),str(1000100+shift),str(2000100+shift)])
            self.assertEqual(rows[2]["flight_connection_status"], "CLOCK_CONNECTED_NO_EXACT_SAMPLE")
            self.assertEqual(rows[2]["flight_roll_rad"], "")
            self.assertEqual(report["flight_binding"]["withheld_frames"],1)
            self.assertEqual(report["untimed_video_frames"],1)
            self.assertEqual(report["source_export_sha256"],digest(export))
            self.assertEqual(report["csv_sha256"],digest(text.encode()))
            self.assertEqual([r["source_frame_index"] for r in rows],["0","1","2"])
            self.assertTrue(all(r["record_time_us"] == "0" for r in rows))
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder)
                (path/"o.csv").write_text(text)
                (path/"p.toml").write_text(profile)
                reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
                self.assertTrue(reader)
                result = subprocess.run([reader,str(path/"p.toml"),str(path/"o.csv"),"--allow-equal-time"],capture_output=True,text=True,check=True)
                observations=json.loads(result.stdout)["observations"]
                self.assertEqual(len(observations),3)
                self.assertTrue(all(o["clock_basis"]=="Unknown" for o in observations))

    def test_withheld_ambiguity_and_refusals(self):
        export, flight, declaration = inputs()
        declaration["valid_end_pts"] = 1000
        text, report, *_ = convert(export, flight, json.dumps(declaration).encode())
        self.assertEqual(list(csv.DictReader(io.StringIO(text)))[2]["flight_connection_status"],"WITHHELD_DECLARED_INTERVAL")
        self.assertEqual(report["flight_binding"]["withheld_frames"],2)
        for key,value in (("export_sha256","0"*64),("anchor_frame_index",3),("relation","guess"),("stream_index",True)):
            with self.assertRaises(ValueError):
                convert(export,flight,json.dumps(dict(declaration,**{key:value})).encode())
        flight = flight.replace(b"3000100,",b"1000100,9,9,9\n3000100,")
        declaration["flight_sha256"] = digest(flight)
        text, *_ = convert(export,flight,json.dumps(declaration).encode())
        self.assertEqual(list(csv.DictReader(io.StringIO(text)))[1]["flight_connection_status"],"CLOCK_CONNECTED_AMBIGUOUS_SAMPLE")
        export,flight,declaration = inputs(denominator=30000)
        text, *_ = convert(export,flight,json.dumps(declaration).encode())
        self.assertEqual(list(csv.DictReader(io.StringIO(text)))[1]["flight_connection_status"],"WITHHELD_SUBMICROSECOND_MAPPING")


def sixty(time_base, step, frames=60):
    """Authored 60 fps export; flight rows sit at every frame's floor time, so a floor match is tempting."""
    export = json.dumps({"program_version": {"version": "8.1"}, "streams": [{
        "index": 0, "codec_type": "video", "time_base": time_base, "nb_read_frames": str(frames)}],
        "frames": [{"media_type": "video", "stream_index": 0, "pts": i*step, "width": 3840, "height": 2160,
                    "key_frame": int(i == 0), "pict_type": "I" if i == 0 else "P"} for i in range(frames)]}).encode()
    rate = Fraction(time_base)
    times = [1000 + int(i*step*rate*1000000) for i in range(frames)] + [10**7]
    flight = ("TimeUS,Roll,Pitch,Yaw\n" + "".join(f"{t},1,2,3\n" for t in times)).encode()
    declaration = dict(schema="video-flight-clock-v1", export_sha256=digest(export), flight_sha256=digest(flight),
        relation=RELATION, stream_index=0, anchor_frame_index=0, anchor_flight_index=1,
        valid_start_pts=0, valid_end_pts=frames*step, max_abs_delta_ticks=frames*step,
        session_id="fpv-session", camera_id="camera", flight_entity_id="plane",
        media_clock_id="PTS", flight_clock_id="boot", citation="authored assertion, not measured sync")
    return export, flight, json.dumps(declaration).encode()


class RationalClockTests(unittest.TestCase):
    def test_sixty_fps_rational_connection_and_common_output(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_CSV_READER")
        self.assertTrue(reader)
        for time_base, step in (("1/60000", 1000), ("1/60000", 1001), ("1/90000", 1500)):
            with self.subTest(time_base=time_base, step=step):
                export, flight, declaration = sixty(time_base, step)
                strict, strict_report, *_ = convert(export, flight, declaration)
                self.assertEqual(strict_report["flight_binding"]["statuses"]["WITHHELD_SUBMICROSECOND_MAPPING"], 40)
                self.assertNotIn("derived_flight_time_us_exact", strict.splitlines()[0])
                text, report, profile, _, _ = convert(export, flight, declaration, rational_clock=True)
                rows = list(csv.DictReader(io.StringIO(text)))
                binding = report["flight_binding"]
                self.assertEqual((len(rows), binding["connected_frames"], binding["withheld_frames"]), (60, 60, 0))
                self.assertEqual(binding["statuses"]["CLOCK_CONNECTED_SUBMICROSECOND_NO_EXACT_SAMPLE"], 40)
                self.assertTrue(binding["rational_clock"] and binding["all_selected_frames_connected"])
                exact_ticks = Fraction(time_base) * 1000000
                for index, row in enumerate(rows):
                    mapped = 1000 + index*step*exact_ticks
                    self.assertEqual(row["derived_flight_time_us_exact"], f"ratio:{mapped.numerator}/{mapped.denominator}")
                    self.assertEqual(int(row["derived_flight_time_us_floor"]), mapped.numerator // mapped.denominator)
                    if mapped.denominator == 1:
                        self.assertEqual(row["flight_connection_status"], "CLOCK_CONNECTED_EXACT_SAMPLE")
                        self.assertEqual(row["derived_flight_time_us"], str(mapped))
                    else:
                        # A flight row exists at the floor time; it must not become an exact sample.
                        self.assertEqual(row["flight_connection_status"], "CLOCK_CONNECTED_SUBMICROSECOND_NO_EXACT_SAMPLE")
                        self.assertEqual((row["derived_flight_time_us"], row["flight_exact_sample_index"],
                                          row["flight_roll_rad"]), ("", "", ""))
                    self.assertEqual(row["record_time_us"], "0")
                    self.assertEqual(row["session_id_hex"], "hex:" + b"fpv-session".hex())
                self.assertIn("video_flight_declared_rational_v1", profile)
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder)
                    (path/"o.csv").write_text(text)
                    (path/"p.toml").write_text(profile)
                    result = subprocess.run([reader, str(path/"p.toml"), str(path/"o.csv"), "--allow-equal-time"],
                                            capture_output=True, text=True, check=True)
                observations = json.loads(result.stdout)["observations"]
                self.assertEqual(len(observations), 60)
                self.assertTrue(all(o["clock_basis"] == "Unknown" and o["channel"] == "video" for o in observations))
                self.assertEqual([o["fields"]["derived_flight_time_us_exact"] for o in observations],
                                 [r["derived_flight_time_us_exact"] for r in rows])
                self.assertEqual({o["fields"]["derived_flight_time_precision"] for o in observations},
                                 {"EXACT_RATIONAL_AUTHORITATIVE_FLOOR_US_QUANTIZED_NOT_A_TIMESTAMP"})

    def test_rational_keeps_withheld_range_missing_and_ambiguity(self):
        export, flight, declaration = inputs(denominator=30000)
        flight = flight.replace(b"\n1000100,", b"\n33433,9,9,9\n1000100,")  # floor of frame 1's 100+100000/3 us, but not its exact time
        declaration["flight_sha256"] = digest(flight)
        with self.assertRaises(ValueError):
            convert(export, flight, json.dumps(declaration).encode(), rational_clock="yes")
        text, report, *_ = convert(export, flight, json.dumps(declaration).encode(), rational_clock=True)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[1]["derived_flight_time_us_exact"], "ratio:100300/3")
        self.assertEqual(rows[1]["flight_connection_status"], "CLOCK_CONNECTED_SUBMICROSECOND_NO_EXACT_SAMPLE")
        self.assertEqual(rows[1]["flight_exact_sample_index"], "")
        self.assertEqual(report["flight_binding"]["statuses"]["WITHHELD_MISSING_PTS"], 1)
        self.assertEqual(report["flight_binding"]["connected_frames"] + report["flight_binding"]["withheld_frames"],
                         report["selected_video_frames"])
        for change, frame, status in ((dict(valid_end_pts=1000), 2, "WITHHELD_DECLARED_INTERVAL"),
                                      (dict(anchor_frame_index=1, anchor_flight_index=4), 2, "WITHHELD_FLIGHT_RANGE"),
                                      (dict(anchor_frame_index=2, valid_start_pts=0), 0, "WITHHELD_FLIGHT_RANGE")):
            with self.subTest(change=change):
                text, *_ = convert(export, flight, json.dumps(dict(declaration, **change)).encode(), rational_clock=True)
                row = list(csv.DictReader(io.StringIO(text)))[frame]
                self.assertEqual(row["flight_connection_status"], status)
                self.assertEqual((row["derived_flight_time_us_exact"], row["derived_flight_time_us_floor"]), ("", ""))
        export, flight, declaration = inputs()
        flight = flight.replace(b"3000100,", b"1000100,9,9,9\n3000100,")
        declaration["flight_sha256"] = digest(flight)
        text, *_ = convert(export, flight, json.dumps(declaration).encode(), rational_clock=True)
        row = list(csv.DictReader(io.StringIO(text)))[1]
        self.assertEqual(row["flight_connection_status"], "CLOCK_CONNECTED_AMBIGUOUS_SAMPLE")
        self.assertEqual((row["derived_flight_time_us_exact"], row["flight_roll_rad"]), ("ratio:1000100/1", ""))
