"""Authored binary inputs, not real Rover operation or hardware certification."""
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import convert_rover_saved_bundle as bundle
import export_ardupilot_records as exporter
from tests.test_ardupilot_record_export import fmt_record, pack, SIZES, HEAD


def original(second=False, omit=()):
    stamp = 2000 if second else 1000
    records = {
        "VER": (stamp, 9, 21, 4, 5, 5, 3, 123, b"ArduRover V4.5.5", 1054, 1),
        "MODE": (stamp, 10, 10, 1), "ARM": (stamp, 1, 0, 0, 3),
        "PARM": (stamp, b"FRAME_CLASS", 1.0, float("nan")),
        "BAT": (stamp, 0, 12.0, 12.2, -2.0 if second else 2.0, 100., 1., 2300, .01, 90, 1, 100),
        "BCL": (stamp, 0, 12.0, *([4001] * 12)), "BCL2": (stamp, 0, 4001, 0),
        "MSG": (stamp, b"ArduRover, saved report"),
        "GPS": (stamp, 0, 3 if not second else 0, 1000, 2300, 10, 150,
                350000000, 1390000000, 1234, 3.0, 90.0, -.2, 0.0, 1),
        "ATT": (stamp, 0, 100, 0, -200, 0, 9000, 0, 0, 2),
    }
    blob = fmt_record(128, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
    for i, (name, (fmt, labels)) in enumerate(bundle.LAYOUTS.items(), 140):
        blob += fmt_record(i, 3 + sum((2 if c == "C" else SIZES[c]) for c in fmt), name, fmt, labels)
    for i, (name, (fmt, _)) in enumerate(bundle.LAYOUTS.items(), 140):
        if name in omit:
            continue
        body = b"".join(struct.pack("<H", v) if c == "C" else pack(c, [v])
                        for c, v in zip(fmt, records[name]))
        blob += HEAD + bytes([i]) + body
    return blob


class RoverSavedBundleTests(unittest.TestCase):
    def test_two_authored_inputs_common_all_csv_and_missing(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common CSV reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.bin"
            for second in (False, True):
                path.write_bytes(original(second))
                files, report = bundle.convert(path, bundle.PIN)
                self.assertEqual(report["missing_required_roles"], [])
                self.assertEqual(sum(report["exported_counts"].values()) + sum(report["unselected_counts"].values()), report["total_messages"])
                for item in report["outputs"]:
                    if "profile" not in item:
                        self.assertEqual(item["records"], 1)
                        continue
                    target = Path(directory) / item["file"]
                    target.write_bytes(files[item["file"]])
                    run = subprocess.run([reader, str(ROOT / item["profile"]), str(target),
                        "--allow-equal-time", "--preserve-nonfinite-as-text"], capture_output=True)
                    self.assertEqual(run.returncode, 0, (item["file"], run.stderr[:300]))
                    common = json.loads(run.stdout)
                    self.assertNotIn('"Air"', json.dumps(common))
                    self.assertNotIn('"Ground"', json.dumps(common))
                angle = next(csv.DictReader(io.StringIO(files["ATT.csv"].decode())))
                self.assertAlmostEqual(float(angle["reported_roll_rad"]), 0.017453292519943295)
                self.assertIn(b"ArduRover, saved report".hex(), files["MSG.csv"].decode())
                self.assertIn(report["source_sha256"].encode(), files["GPS.csv"])
            path.write_bytes(original(omit=("BAT", "VER")))
            files, report = bundle.convert(path, bundle.PIN)
            self.assertEqual(report["missing_required_roles"], ["battery", "reported_build_identity"])
            self.assertNotIn("BAT.csv", files)

    def test_source_layout_and_old_export_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.bin"
            path.write_bytes(original())
            with self.assertRaises(ValueError):
                bundle.convert(path, "caller-rover")
            old, old_report = exporter.export_records(path, ("MODE", "BAT"))
            enriched, report = exporter.export_records(path, ("MODE", "BAT"), include_source_indices=True)
            self.assertEqual(old, enriched)
            self.assertEqual(old_report, {k: v for k, v in report.items() if k != "selected_record_refs"})
            with self.assertRaises(ValueError):
                exporter.export_records(path, ("ATT",), output_format="jsonl")
            path.write_bytes(original().replace(b"QccccCCCCB", b"QCCCCCCCCB", 1))
            with self.assertRaises(ValueError):
                bundle.convert(path, bundle.PIN)

    def test_original_interval_distinguishes_outside_from_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.bin"
            path.write_bytes(original())
            files, report = bundle.convert(path, bundle.PIN, record_window=(11, 14))
            self.assertEqual(report["exported_counts"], {"GPS": 1, "ATT": 1, "MODE": 1})
            self.assertEqual(report["original_absent_selected_types"], [])
            self.assertIn("battery", report["missing_required_roles"])
            self.assertEqual(report["outside_window_selected_type_counts"]["BAT"], 1)
            self.assertEqual(sum(report["exported_counts"].values()) + sum(report["unselected_counts"].values())
                             + sum(report["outside_window_selected_type_counts"].values()), report["total_messages"])
            self.assertEqual(report["selected_record_refs"]["GPS"][0]["index"], 11)
            with self.assertRaises(ValueError):
                bundle.convert(path, bundle.PIN, record_window=(0, 100001))
            with self.assertRaisesRegex(ValueError, "stop exceeds"):
                bundle.convert(path, bundle.PIN, record_window=(0, 22))

    def test_window_snapshot_rejects_source_truncation_before_mapping(self):
        import saved_binary_snapshot
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.bin"
            path.write_bytes(original())
            copy = saved_binary_snapshot.copy_source
            def changed(source, target):
                result = copy(source, target)
                path.write_bytes(b"")
                return result
            with patch.object(saved_binary_snapshot, "copy_source", changed):
                with self.assertRaisesRegex(ValueError, "changed during snapshot"):
                    bundle.convert(path, bundle.PIN, record_window=(11, 14))

    def test_explicit_prefix_only_known_truncated_tail_not_holes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.bin"
            path.write_bytes(original()[:-3])
            with self.assertRaises(exporter.UndecodedSpans):
                bundle.convert(path, bundle.PIN, record_window=(11, 14))
            files, report = bundle.convert(path, bundle.PIN, record_window=(11, 14), allow_truncated_tail=True)
            self.assertEqual(report["framing"], exporter.INCOMPLETE)
            self.assertEqual(report["truncated_tail_schema"]["columns"], ["TimeUS", "Instance", "V13", "V14"])
            self.assertEqual(report["exported_counts"]["GPS"], 1)
            for blob in (original() + b"\xa3\x95\xff", original()[:89] + b"junk" + original()[89:]):
                path.write_bytes(blob)
                with self.assertRaises(exporter.UndecodedSpans):
                    bundle.convert(path, bundle.PIN, record_window=(11, 14), allow_truncated_tail=True)


if __name__ == "__main__":
    unittest.main()
