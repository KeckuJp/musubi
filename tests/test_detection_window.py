"""Independent fixed-contract saved detection windows; authored inputs, no SDK/media run."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_video_frames as converter

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/video-frame-export/detection-profile.toml"


def deepstream():
    return dict(exporter_version="9.0.0", coordinate_basis="pipeline_output_pixels",
                pipeline_width=640, pipeline_height=480, capture_time_us=1001,
                image_id="authored-deepstream", model_id="declared-model",
                objects=[dict(class_id=2, obj_label="box", object_id=2**64-2,
                              confidence=.8, rect_params=dict(left=20, top=30, width=40, height=50))])


def yolo(normalized=False):
    return dict(exporter_version="8.3.0", normalized=normalized, image_width=640,
                image_height=480, capture_time_us=1001, image_id="authored-yolo", model_id="declared-model",
                detections=[dict(name="box", **{"class": 2}, confidence=.75,
                                box=dict(x1=.1, y1=.2, x2=.5, y2=.6) if normalized
                                else dict(x1=10, y1=20, x2=50, y2=60))])


def line(record):
    return json.dumps(record, separators=(",", ":")).encode()


class DetectionWindowContract(unittest.TestCase):
    def common(self, output):
        executable = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(executable and Path(executable).is_file(), "actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selected.csv"
            path.write_text(output)
            result = subprocess.run([executable, str(PROFILE), str(path), "--allow-equal-time"],
                                    capture_output=True, check=True)
        return json.loads(result.stdout)

    def accounting(self, native, output, report, start, stop, count):
        digest = hashlib.sha256(native).hexdigest()
        selection = report["record_selection"]
        expected = dict(source_sha256=digest, source_bytes=len(native), source_records=count,
                        selected_records=stop-start, excluded_records=count-(stop-start),
                        start_record=start, stop_record_exclusive=stop,
                        outside_selection="NOT_DECODED_OR_VALIDATED",
                        capture_clock_scope="SELECTED_RECORDS_ONLY_NO_CROSS_WINDOW_ORDER_CLAIM")
        for key, value in expected.items():
            self.assertEqual(selection[key], value, key)
        self.assertEqual(selection["bounds"], dict(max_input_bytes=2*1024**3, max_records=5_000_000,
                         max_record_bytes=1024**2, max_selected_source_bytes=16*1024**2,
                         max_output_bytes=16*1024**2, max_report_bytes=32*1024**2))
        self.assertEqual(report["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
        self.assertEqual(report["output_bytes"], len(output.encode()))
        self.assertEqual([r["index"] for r in report["source_records"]], list(range(start, stop)))
        for row in csv.DictReader(io.StringIO(output)):
            self.assertEqual(row["source_file_sha256"], "sha256:"+digest)
        return list(csv.DictReader(io.StringIO(output)))

    def test_large_deepstream_adjacent_windows_preserve_global_identity_and_common_meaning(self):
        first = deepstream()
        second_object = copy.deepcopy(first["objects"][0])
        second_object.update(object_id=2**64-1, confidence=-0.10000000149011612)
        first["objects"].append(second_object)
        empty = dict(deepstream(), objects=[])
        # Outside payload is deliberately not JSON. Each physical line fits the 1MiB bound.
        prefix = (b"not-json:" + b"x"*(1024*1024-16) + b"\n") * 17
        native = prefix + line(first) + b"\r\n" + line(empty)  # final unterminated line
        self.assertGreater(len(native), 16*1024**2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"long.jsonl"
            path.write_bytes(native)
            for start, stop in ((17, 18), (18, 19)):
                output, report = converter.convert_detections_path(path, 3, deepstream=True,
                                                                   start_record=start, stop_record=stop)
                rows = self.accounting(native, output, report, start, stop, 19)
                record = first if start == 17 else empty
                self.assertEqual(report["source_records"][0]["original_line"], line(record).decode())
                self.assertTrue(all(int(r["source_record_index"]) == start for r in rows))
                self.assertTrue(all(r["source_record_sha256"] == "sha256:"+hashlib.sha256(line(record)).hexdigest()
                                    for r in rows))
                common = self.common(output)
                self.assertEqual(common["main_rows"], 2 if start == 17 else 1)
                for observation in common["observations"]:
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertIsNone(observation["t_boot_us"])
                    self.assertEqual(observation["fields"]["record_time_us"], 1001)
                    self.assertEqual(observation["fields"]["source_file_sha256"], "sha256:"+hashlib.sha256(native).hexdigest())
                    self.assertEqual(observation["fields"]["source_record_index"], start)
                if start == 17:
                    self.assertEqual((report["detections"], report["empty_records"]), (2, 0))
                    self.assertEqual([r["detection_index"] for r in rows], ["0", "1"])
                    fields, missing = [o["fields"] for o in common["observations"]]
                    self.assertEqual(fields["box_x2_px"], 60)
                    self.assertEqual(fields["box_coordinate_basis"], "PIPELINE_LAST_COMPONENT_PIXELS")
                    self.assertEqual(fields["track_id_text"], "u64:18446744073709551614")
                    self.assertEqual(missing["model_score_status"], "UNAVAILABLE_SOURCE_SENTINEL")
                    self.assertEqual(missing["tracking_status"], "UNTRACKED_REPORTED")
                    self.assertIsNone(missing.get("model_score_reported"))
                else:
                    self.assertEqual((report["detections"], report["empty_records"]), (0, 1))
                    self.assertEqual(rows[0]["detection_status"], "NO_DETECTIONS_REPORTED")
                    self.assertEqual(rows[0]["box_x1_px"], "")

    def test_yolo_pixel_and_normalized_selected_time_only_and_legacy_unchanged(self):
        first, second = yolo(), yolo(True)
        second["detections"][0]["track_id"] = 17
        native = b'{"capture_time_us":999999,"bad":\n' + line(first) + b"\n" + line(second) + b"\n"
        native += b'{"capture_time_us":0,"bad":'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"yolo.jsonl"
            path.write_bytes(native)
            output, report = converter.convert_detections_path(path, 9, start_record=1, stop_record=3)
            rows = self.accounting(native, output, report, 1, 3, 4)
            self.assertEqual([int(r["source_record_index"]) for r in rows], [1, 2])
            common = self.common(output)
            self.assertEqual(common["main_rows"], 2)
            a, b = [o["fields"] for o in common["observations"]]
            self.assertEqual((a["box_x1_px"], b["box_x1_px"], b["box_y2_px"]), (10, 64, 288))
            self.assertEqual(b["track_id_reported"], 17)
            legacy, legacy_report = converter.convert_detections(line(first).decode(), 9)
            self.assertNotIn("source_file_sha256", next(csv.reader(io.StringIO(legacy))))
            self.assertNotIn("record_selection", legacy_report)
            path.write_bytes(line(first))
            selected, _ = converter.convert_detections_path(path, 9, start_record=0, stop_record=1)
            selected_row = next(csv.DictReader(io.StringIO(selected)))
            selected_row.pop("source_file_sha256")
            self.assertEqual(selected_row, next(csv.DictReader(io.StringIO(legacy))))

    def test_intervals_selected_schema_and_global_framing_refuse(self):
        good = line(deepstream())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"input.jsonl"
            path.write_bytes(good + b"\n" + good)
            for start, stop in ((0, 0), (-1, 1), (2, 1), (0, 3), (False, 1), (0, True), (0.0, 1)):
                with self.subTest(interval=(start, stop)), self.assertRaises(ValueError):
                    converter.convert_detections_path(path, 0, deepstream=True, start_record=start, stop_record=stop)
            for suffix in (b"\n\n", b"\n\xff", b"\nx\x00", b"\nx\ry", b"\nx\v", b"\nx\xe2\x80\xa8y"):
                path.write_bytes(good + suffix)
                with self.subTest(framing=suffix), self.assertRaises(ValueError):
                    converter.convert_detections_path(path, 0, deepstream=True, start_record=0, stop_record=1)
            bad_records = [b"{bad", good.replace(b'"9.0.0"', b'"8.3.0"'),
                           good.replace(b'"confidence":0.8', b'"confidence":null'),
                           good.replace(b'"capture_time_us":1001,', b""),
                           good.replace(b'"class_id":2', b'"class_id":2,"class_id":3')]
            for bad in bad_records:
                path.write_bytes(bad)
                with self.subTest(selected=bad[:40]), self.assertRaises(ValueError):
                    converter.convert_detections_path(path, 0, deepstream=True, start_record=0, stop_record=1)
            path.write_bytes(good + b"\n" + good.replace(b'"capture_time_us":1001', b'"capture_time_us":1000'))
            with self.assertRaises(ValueError):
                converter.convert_detections_path(path, 0, deepstream=True, start_record=0, stop_record=2)
            path.write_bytes(b"")
            with self.assertRaises(ValueError):
                converter.convert_detections_path(path, 0, deepstream=True, start_record=0, stop_record=1)

    def test_bounded_resources_and_cli_complete_flags_existing_output(self):
        good = line(yolo())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base/"input.jsonl"
            path.write_bytes(good+b"\n"+good)
            for constant, value in (("MAX_INPUT_BYTES", len(good)), ("MAX_RECORDS", 1),
                                    ("MAX_RECORD_BYTES", len(good)-1), ("LIMIT", len(good)-1),
                                    ("MAX_WINDOW_OUTPUT_BYTES", 1)):
                with self.subTest(bound=constant), patch.object(converter, constant, value, create=True):
                    with self.assertRaises(ValueError):
                        converter.convert_detections_path(path, 0, start_record=0, stop_record=1)
            # Fixed report serialization is the actual pretty CLI JSON, not its smaller compact form.
            output_csv, baseline = converter.convert_detections_path(path, 0, start_record=0, stop_record=1)
            probe = copy.deepcopy(baseline)
            selected_limit = 1000
            for _ in range(4):
                probe["record_selection"]["bounds"]["max_selected_source_bytes"] = selected_limit
                probe["record_selection"]["bounds"]["max_report_bytes"] = 2*selected_limit
                compact_size = len(json.dumps(probe, separators=(",", ":")).encode())
                pretty_size = len(json.dumps(probe, indent=2).encode())
                selected_limit = (compact_size+pretty_size)//4
            self.assertLess(len(good)+1, selected_limit)
            self.assertLess(len(output_csv.encode()), 8*selected_limit)  # existing core CSV limit
            self.assertLess(compact_size, 2*selected_limit)
            self.assertGreater(pretty_size, 2*selected_limit)
            with patch.object(converter, "LIMIT", selected_limit), self.assertRaises(ValueError):
                converter.convert_detections_path(path, 0, start_record=0, stop_record=1)
            command = [sys.executable, str(ROOT/"scripts/convert_video_frames.py"), str(path), "4"]
            output = base/"out"
            flags = ["--detections-830", "--start-record", "1", "--stop-record", "2"]
            completed = subprocess.run(command+[str(output)]+flags, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads((output/"report.json").read_text())
            self.assertEqual(report["record_selection"]["start_record"], 1)
            self.assertEqual(self.common((output/"observations.csv").read_text())["main_rows"], 1)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            self.assertEqual(subprocess.run(command+[str(output)]+flags, capture_output=True).returncode, 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            for index, invalid in enumerate((flags[:-2], ["--start-record", "0", "--stop-record", "1"],
                                             flags+["--deepstream-900"])):
                destination = base/f"bad-{index}"
                self.assertEqual(subprocess.run(command+[str(destination)]+invalid,
                                               capture_output=True).returncode, 2)
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
