"""Authored ffprobe 8.1 documents for FPV recordings; no media, device, goggles or real capture.

The adopted saved-video path already reads a frame timeline, and it is unchanged by this module: no
runtime functionality is added here. This is a **conditional candidate route** kept under regression —
for a producer that declares its container, an authored document shaped for a mode that producer's
specification declares goes through the UNMODIFIED converter and the actual common reader.

The earlier one-off demonstration in
`workers/evidence/application-fpv-video-20260921/o4-applicability.log` **remains valid within its own
pins and scope**; this module is additional protection against drift, not a replacement for it and not
a rule that evidence must be a test to count.

What an authored document plus a producer's container declaration establishes is a **candidate route**.
It is **not** provenance for an arbitrary file, **not** a producer binding, and **not** proof that any
device's recording decoded -- attribution stays at the caller and file level. Reading a frame timeline
is not reading pixels, not a camera health report, and not a goggles or receiver health report. A DVR
file is a file: its timing evidences the recording, not the camera, link or goggles that produced it.
Authored documents only -- real records 0.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_video_frames import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/video-frame-export/profile.toml"


def recording(width, height, time_base, deltas, *, stream_index=0):
    """An ffprobe 8.1 document for one declared recording mode, in the pinned reader's own shape."""
    pts, frames = 0, []
    for index, delta in enumerate(deltas):
        frames.append({"media_type": "video", "stream_index": stream_index, "pts": pts,
                       "width": width, "height": height, "key_frame": 1 if index == 0 else 0,
                       "pict_type": "I" if index == 0 else "P", "vendor_extra": {"kept": True}})
        pts += delta
    return {"program_version": {"version": "8.1"},
            "streams": [{"index": stream_index, "codec_type": "video", "time_base": time_base,
                         "nb_read_frames": str(len(frames))}],
            "frames": frames, "other": {"keep": True}}


class FpvRecordingReuseTests(unittest.TestCase):
    def test_a_declared_container_mode_converts_unchanged(self):
        document = recording(3840, 2160, "1/60000", [1000, 1000, 1000])
        output, report = convert(json.dumps(document), 0)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["record_time_us"] for r in rows], ["0", "16666", "33333"])
        self.assertEqual({(r["width_px"], r["height_px"]) for r in rows}, {("3840", "2160")})
        self.assertEqual([r["picture_type_reported"] for r in rows], ["I", "P", "P"])
        self.assertEqual((report["source_frames"], report["selected_video_frames"]), (3, 3))
        self.assertEqual((report["timed_video_frames"], report["untimed_video_frames"]), (3, 0))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_the_same_mode_reaches_the_actual_common_reader(self):
        """Explicitly skipped without the configured reader, never quietly passed."""
        document = recording(3840, 2160, "1/60000", [1000, 1000, 1000])
        output, _ = convert(json.dumps(document), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frames.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(path), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        observations = common["observations"]
        self.assertEqual({o["channel"] for o in observations}, {"video"})
        self.assertTrue(all(o["anchor_unix_us"] is None for o in observations))
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
        self.assertEqual(observations[0]["fields"]["width_px"], 3840)

    def test_a_second_declared_mode_needs_no_implementation_change(self):
        """Reuse evidence: authored after the first, and the converter is untouched between them."""
        document = recording(1920, 1080, "1/120000", [1000, 1000, 1000], stream_index=2)
        output, report = convert(json.dumps(document), 2)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["record_time_us"] for r in rows], ["0", "8333", "16666"])
        self.assertEqual({r["width_px"] for r in rows}, {"1920"})
        self.assertEqual((report["source_frames"], report["selected_video_frames"]), (3, 3))

    def test_wrong_version_wrong_stream_and_an_untimed_frame_are_not_repaired(self):
        document = recording(3840, 2160, "1/60000", [1000])
        other = json.loads(json.dumps(document))
        other["program_version"]["version"] = "7.1"
        with self.assertRaises(ValueError):
            convert(json.dumps(other), 0)
        audio = json.loads(json.dumps(document))
        audio["streams"][0]["codec_type"] = "audio"
        with self.assertRaises(ValueError):
            convert(json.dumps(audio), 0)
        untimed = json.loads(json.dumps(document))
        untimed["frames"].append({k: v for k, v in untimed["frames"][0].items() if k != "pts"})
        untimed["streams"][0]["nb_read_frames"] = "2"
        output, report = convert(json.dumps(untimed), 0)
        self.assertEqual(report["untimed_video_frames"], 1)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 1)


if __name__ == "__main__":
    unittest.main()
