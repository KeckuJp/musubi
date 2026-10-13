"""Authored saved messages; no device, driver or physical record."""
import copy
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

import numpy as np
from tests.test_rosbag_observations import bag, STORE, samples, ROOT
from scripts.convert_rosbag_observations import POINT_CLOUD, convert
from scripts.convert_realsense_pointcloud import convert_saved, BASIS


def cloud(ordered=False, texture="none", count=4):
    types = STORE.types
    header = copy.deepcopy(samples()[0].header)
    header.frame_id = "saved_camera_depth_optical_frame"
    fields = [types["sensor_msgs/msg/PointField"](n, i * 4, 7, 1)
              for i, n in enumerate(("x", "y", "z"))]
    point = struct.pack("<ffff", 1.25, -2.5, 3.75, 0.)
    if texture != "none":
        fields.append(types["sensor_msgs/msg/PointField"](
            "rgb" if texture == "RGB8" else "intensity", 16, 7, 1))
        point += b"\x12\x34\x56\x00"
    height = 2 if ordered else 1
    width = count // height
    return types[POINT_CLOUD](header, height, width, fields, False, len(point),
        width * len(point), np.frombuffer(point * count, dtype=np.uint8), not ordered)


class RealSensePointCloudTests(unittest.TestCase):
    def test_two_layouts_common_and_reuse(self):
        for ordered, texture in ((False, "none"), (True, "RGB8")):
            with self.subTest(ordered=ordered), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bag"
                message = cloud(ordered, texture)
                bag(path, messages=[message])
                baseline, old_report = convert(path, pointcloud_unit="m", pointcloud_compact=True,
                                               topics=["/example/0"])
                output, report = convert_saved(path, topic="/example/0", camera_name="saved_camera",
                    ordered=ordered, texture=texture)
                rows = list(csv.DictReader(io.StringIO(output)))
                old_rows = list(csv.DictReader(io.StringIO(baseline)))
                for row, old in zip(rows, old_rows):
                    for key in old:
                        if key not in ("source_record_hex", "point_unit_basis"):
                            self.assertEqual(row[key], old[key])
                    self.assertEqual(row["point_unit_basis"], BASIS)
                self.assertEqual(report["pointcloud_sources"], old_report["pointcloud_sources"])
                self.assertEqual(report["output_records"], 4)
                self.assertEqual(report["topic_selection"]["declared_excluded_messages"], 1)
                reader = os.environ.get("MUSUBI_TELEMETRY_READER")
                if not reader:
                    self.fail("actual common reader required")
                target = Path(directory) / "points.csv"
                target.write_text(output)
                common = json.loads(subprocess.check_output([reader,
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"]))
                self.assertEqual(len(common["observations"]), 4)
                self.assertEqual(common["observations"][-1]["fields"]["point_y_m"], -2.5)
                self.assertEqual(common["observations"][-1]["clock_basis"], "Unknown")
                self.assertEqual(common["observations"][-1]["fields"]["point_unit_basis"], BASIS)
                if ordered:
                    destination = Path(directory) / "converted"
                    subprocess.run([sys.executable, str(ROOT / "scripts/convert_realsense_pointcloud.py"),
                        str(path), str(destination), "--topic", "/example/0", "--camera-name",
                        "saved_camera", "--layout", "ordered", "--texture", "RGB8"], check=True)
                    self.assertEqual((destination / "observations.csv").read_text(), output)

    def test_refuse_mismatched_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"; bag(path, messages=[cloud()])
            options = dict(topic="/example/0", camera_name="saved_camera", ordered=False, texture="none")
            for change in (dict(camera_name="other"), dict(camera_name="bad/name"),
                           dict(ordered=1), dict(ordered=True), dict(texture="RGB8"),
                           dict(texture="unsupported"), dict(topic="/unsupported")):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    convert_saved(path, **dict(options, **change))
        for field in ("point_step", "is_bigendian", "nonpositive_z"):
            with tempfile.TemporaryDirectory() as directory:
                message = cloud()
                if field == "point_step":
                    message.fields[0].offset = 4  # overlapping xyz rejected by reused decoder
                elif field == "is_bigendian":
                    message.is_bigendian = True
                else:
                    raw = bytearray(message.data.tobytes())
                    struct.pack_into("<f", raw, 8, 0.)
                    message.data = np.frombuffer(raw, dtype=np.uint8)
                path = Path(directory) / "bag"; bag(path, messages=[message])
                with self.assertRaises(ValueError):
                    convert_saved(path, topic="/example/0", camera_name="saved_camera",
                                  ordered=False, texture="none")

    def test_compact_capacity_is_not_reduced_by_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bag"; bag(path, messages=[cloud(count=100000)])
            output, report = convert_saved(path, topic="/example/0", camera_name="saved_camera",
                                          ordered=False, texture="none")
            self.assertEqual(report["output_records"], 100000)
            self.assertEqual(sum(1 for _ in csv.DictReader(io.StringIO(output))), 100000)
            self.assertLess(len(output.encode()), 128 * 1024 * 1024)

    def test_vga_frame_four_parts_to_common_without_missing_or_duplicate_points(self):
        with tempfile.TemporaryDirectory() as directory:
            message = cloud(ordered=True, count=640 * 480)
            message.width, message.height = 640, 480
            message.row_step = 640 * message.point_step
            path = Path(directory) / "bag"; bag(path, messages=[message])
            options = dict(topic="/example/0", camera_name="saved_camera", ordered=True, texture="none")
            with self.assertRaises(ValueError):
                convert_saved(path, **options)  # default bound not silently relaxed
            seen = set()
            source_digests = set()
            source_clocks = set()
            for start, stop in ((0, 100000), (100000, 200000), (200000, 300000), (300000, 307200)):
                output, report = convert_saved(path, **options, point_window=(start, stop))
                source = report["pointcloud_sources"][0]
                source_digests.add(source["source_cdr_sha256"])
                source_clocks.add((source["capture_ns"], source["decoded"]["header"]["stamp"]["sec"],
                                   source["decoded"]["header"]["stamp"]["nanosec"]))
                self.assertEqual(source["point_index_window"]["original_points"], 307200)
                self.assertEqual(source["point_index_window"]["outside_points"], 307200 - (stop - start))
                csv_path = Path(directory) / f"part-{start}.csv"; csv_path.write_text(output)
                with (Path(directory) / "common.json").open("wb") as destination:
                    subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(csv_path), "--allow-equal-time"], stdout=destination, check=True)
                common = json.loads((Path(directory) / "common.json").read_text())
                indexes = {item["fields"]["point_index"] for item in common["observations"]}
                self.assertEqual(indexes, set(range(start, stop)))
                self.assertTrue(all(item["clock_basis"] == "Unknown" for item in common["observations"]))
                self.assertTrue(all(item["fields"]["point_count"] == 307200 for item in common["observations"]))
                self.assertFalse(seen & indexes)
                seen.update(indexes)
            self.assertEqual(len(seen), 307200)
            self.assertEqual(len(source_digests), 1)
            self.assertEqual(source_clocks, {("7000123456", 2, 123456789)})
            for interval in ((-1, 2), (2, 2), (0, 100001), (300000, 307201), (None, 4)):
                with self.assertRaises(ValueError):
                    convert_saved(path, **options, point_window=interval)


if __name__ == "__main__":
    unittest.main()
