"""Saved Ping-protocol binary frames -> the adopted decoded-report conversion -> common output.

Only the input step is new here. mm->m, the confidence rules and the common Observation are the
adopted `ping1d-distance-1746` path and are exercised, not reimplemented.

Authored bytes only. No real sonar record, no device, no serial/UDP, no Registry, no credentials.
A decoded distance is a reported acoustic return, never vehicle depth, altitude, calibrated
accuracy or target identity.
"""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_ping_frames import (
    HEADER,
    PING_PYTHON_COMMIT,
    PROTOCOL_COMMIT,
    convert,
    new_report,
    decode_frames,
)

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "scripts/convert_ardupilot_battery_csv.py"
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"

SIMPLE = struct.Struct("<IB")       # 1211 distance_simple: u32 mm, u8 %
FULL = struct.Struct("<IHHIIII")    # 1212 distance


def frame(message_id, payload, src=5, dst=0, checksum=None, start=b"BR"):
    """One Ping frame; the checksum is a plain byte sum, not a CRC."""
    head = struct.pack("<BBHHBB", start[0], start[1], len(payload), message_id, src, dst)
    body = head + payload
    return body + struct.pack("<H", sum(body) & 0xFFFF if checksum is None else checksum)


def capture(path, raw):
    path.write_bytes(raw)
    return path


def common_reader(case):
    """The built common reader, or an explicit outcome — never a quiet pass.

    If `MUSUBI_TELEMETRY_READER` is set it is a caller instruction: a missing executable there is a
    **failure**, not something to work around. With nothing configured, an absent default build is
    an explicit **skip**, so a run without the reader can never look like a passing test that
    silently omitted its common assertions. Mandatory CI supplies the executable.
    """
    declared = os.environ.get("MUSUBI_TELEMETRY_READER")
    if declared:
        if not Path(declared).exists():
            case.fail(f"MUSUBI_TELEMETRY_READER is set to {declared!r}, which does not exist")
        return declared
    default = ROOT / "target/debug/examples/read_telemetry_csv"
    if not default.exists():
        case.skipTest("common reader not built; run cargo build -p musubi-reference-readers "
                      "--example read_telemetry_csv, or set MUSUBI_TELEMETRY_READER")
    return str(default)


def read_common(case, profile, csv_path):
    """Run the actual common reader over a converted CSV and return its JSON."""
    return json.loads(subprocess.run(
        [common_reader(case), str(profile), str(csv_path), "--allow-equal-time"],
        check=True, capture_output=True).stdout)


class PingSavedFrameTests(unittest.TestCase):
    def test_frame_geometry_matches_the_pinned_definitions(self):
        """8-byte header + payload + 2-byte sum, so 15 and 34 bytes for 1211 and 1212."""
        self.assertEqual(HEADER.size, 8)
        self.assertEqual(SIMPLE.size, 5)
        self.assertEqual(FULL.size, 24)
        self.assertEqual(len(frame(1211, SIMPLE.pack(1, 2))), 15)
        self.assertEqual(len(frame(1212, FULL.pack(1, 2, 3, 4, 5, 6, 7))), 34)
        self.assertEqual(PROTOCOL_COMMIT, "1746cd03f942d58bcf08253055854caea2e33fda")
        self.assertEqual(PING_PYTHON_COMMIT, "7f27d04588916fa0157374ca10625b1415117bb4")

    def test_two_authored_captures_reach_the_common_output_through_the_adopted_conversion(self):
        """Saved bytes -> frames -> adopted mm/confidence semantics -> actual common Observation."""
        first = frame(1211, SIMPLE.pack(1250, 0)) + frame(1211, SIMPLE.pack(3000, 100))
        second = (frame(1212, FULL.pack(2500, 90, 250, 4294967295, 500, 10000, 6), src=9)
                  + frame(1211, SIMPLE.pack(0, 55), src=9))
        expected = ([(1.25, 0.0), (3.0, 1.0)], [(2.5, 0.9), (0.0, 0.55)])

        for raw, want in zip((first, second), expected):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                text, report = convert(capture(base / "capture.bin", raw), 1_000_000)
                self.assertEqual(report["frames_read"], 2)
                self.assertEqual(report["records_emitted"], 2)
                self.assertEqual(report["unselected_messages"], {})
                self.assertEqual(report["protocol_commit"], PROTOCOL_COMMIT)
                self.assertIn("frames carry none", report["clock"])

                jsonl = base / "decoded.jsonl"
                jsonl.write_text(text)
                records = [json.loads(line) for line in text.splitlines()]
                self.assertEqual([r["frame_index"] for r in records], [0, 1])
                self.assertEqual({r["capture_time_us"] for r in records}, {1_000_000})

                target = base / "out.csv"
                subprocess.run([sys.executable, str(CONVERTER), str(jsonl), str(target),
                                "--source-format", "ping1d-distance-1746"],
                               check=True, capture_output=True)
                rows = list(csv.DictReader(io.StringIO(target.read_text())))
                self.assertEqual(len(rows), 2)
                self.assertEqual([float(r["sonar_reported_distance_m"]) for r in rows],
                                 [d for d, _ in want])
                self.assertEqual([float(r["sonar_reported_confidence_fraction"]) for r in rows],
                                 [c for _, c in want])
                self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])),
                                 records[0])
                self.assertEqual(len(records[0]["frame_sha256"]), 64)

                common = read_common(self, PROFILE, target)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(
                    [o["fields"]["sonar_reported_distance_m"] for o in common["observations"]],
                    [d for d, _ in want])
                self.assertTrue(
                    all(o["clock_basis"] == "Unknown" for o in common["observations"]))
                self.assertEqual(common["platform_domain"], "Unknown")

    def test_a_practical_multi_record_capture_reaches_common_without_omission(self):
        """400 frames: every one is read, emitted and accounted for."""
        frames = b"".join(
            frame(1211, SIMPLE.pack(1000 + i, i % 101)) if i % 3 else
            frame(1212, FULL.pack(2000 + i, i % 101, 250, i, 500, 10000, 2))
            for i in range(400))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            text, report = convert(capture(base / "capture.bin", frames), 7)
            self.assertEqual(report["frames_read"], 400)
            self.assertEqual(report["records_emitted"], 400)
            self.assertEqual(len(text.splitlines()), 400)
            jsonl = base / "decoded.jsonl"
            jsonl.write_text(text)
            target = base / "out.csv"
            subprocess.run([sys.executable, str(CONVERTER), str(jsonl), str(target),
                            "--source-format", "ping1d-distance-1746"],
                           check=True, capture_output=True)
            rows = list(csv.DictReader(io.StringIO(target.read_text())))
            self.assertEqual(len(rows), 400, "no record is dropped between the two layers")
            self.assertEqual(float(rows[0]["sonar_reported_distance_m"]), 2.0)

            common = read_common(self, PROFILE, target)
            self.assertEqual(common["main_rows"], 400)
            observations = common["observations"]
            self.assertEqual(len(observations), 400, "every record reaches common output")
            for index, metres in ((0, 2.0), (1, 1.001), (398, 1.398), (399, 2.399)):
                self.assertAlmostEqual(
                    observations[index]["fields"]["sonar_reported_distance_m"], metres, places=9)
            self.assertEqual(
                [o["fields"]["sonar_reported_distance_m"] for o in observations],
                [float(r["sonar_reported_distance_m"]) for r in rows],
                "the common values are the CSV values, record for record")
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
            self.assertEqual(common["equal_time_order"], "source-order-only")
            self.assertEqual(common["platform_domain"], "Unknown")

    def test_corruption_is_refused_rather_than_resynchronised(self):
        good = frame(1211, SIMPLE.pack(1250, 50))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            def refuse(raw, why, **kwargs):
                with self.assertRaises(ValueError, msg=why):
                    convert(capture(base / "c.bin", raw), 1, **kwargs)

            broken = bytearray(good)
            broken[9] ^= 0xFF
            refuse(bytes(broken), "checksum mismatch")
            refuse(frame(1211, SIMPLE.pack(1250, 50), checksum=0), "stored checksum wrong")
            refuse(good[:6], "header truncated")
            refuse(good[:-1], "checksum truncated")
            refuse(good + good[:9], "trailing partial frame")
            refuse(good + b"\x00\x00" + good, "no resynchronisation")
            refuse(b"\x00\x00" + good, "capture does not start with a frame")
            refuse(frame(1211, SIMPLE.pack(1, 2), start=b"XY"), "wrong start bytes")
            refuse(frame(1212, FULL.pack(1, 2, 3, 4, 5, 6, 7)[:20]), "short 1212 payload")
            refuse(b"", "empty capture")
            refuse(frame(1300, b"\x01\x02"), "unselected message id")
            refuse(frame(1300, b"\x01\x02"), "nothing selected at all",
                   allow_other_ids=True)

            text, report = convert(
                capture(base / "c.bin", frame(1300, b"\x01\x02") + good), 1, allow_other_ids=True)
            self.assertEqual(report["frames_read"], 2)
            self.assertEqual(report["records_emitted"], 1)
            self.assertEqual(report["unselected_messages"], {"1300": 1})
            self.assertEqual(len(text.splitlines()), 1)

            for bad in (None, "1", 1.5, -1, 2**63):
                with self.assertRaises(ValueError):
                    convert(capture(base / "c.bin", good), bad)

    def test_payload_longer_than_the_definition_is_retained_not_dropped(self):
        """The pinned decoder silently drops trailing bytes; they are kept and accounted here."""
        raw = frame(1211, SIMPLE.pack(1250, 50) + b"\xde\xad")
        with tempfile.TemporaryDirectory() as directory:
            text, report = convert(capture(Path(directory) / "c.bin", raw), 1)
            record = json.loads(text)
            self.assertEqual(record["payload"], {"distance": 1250, "confidence": 50})
            self.assertEqual(record["payload_extra_hex"], "dead")
            self.assertEqual(report["records_emitted"], 1)

    def test_the_report_totals_reconcile_over_a_mixed_capture(self):
        raw = (frame(1211, SIMPLE.pack(10, 1)) + frame(1300, b"\x00")
               + frame(1212, FULL.pack(20, 2, 3, 4, 5, 6, 7)) + frame(1301, b"\x00\x00"))
        report = new_report()
        emitted = list(decode_frames(raw, 5, report, allow_other_ids=True))
        self.assertEqual(report["frames_read"], 4)
        self.assertEqual(report["records_emitted"], 2)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(report["unselected_messages"], {"1300": 1, "1301": 1})
        self.assertEqual(
            report["records_emitted"] + sum(report["unselected_messages"].values()),
            report["frames_read"], "every frame is either emitted or counted")


if __name__ == "__main__":
    unittest.main()
