"""Authored LAS 1.4 R16 Tables 11/12, not recorded survey evidence."""
import hashlib
import json
import struct
import unittest

from tests.test_saved_point_cloud import (CAPTURE, las, metre_projected, module,
                                         point, read_common, rows_of)


class PointCloudRgbTests(unittest.TestCase):
    def test_both_rgb_layouts_reuse_coordinates_and_common_without_color_conversion(self):
        for fmt, tick, length, rgb in ((2, None, 26, (1, 256, 65535)),
                                      (3, 12345.5, 34, (65535, 0, 513))):
            with self.subTest(format=fmt):
                raw = las([point(x=123, y=-456, z=789, synthetic=1, source=17,
                                 gps_time=tick) + struct.pack("<3H", *rgb)],
                          records=[metre_projected()], point_format=fmt, point_length=length,
                          global_encoding=0)
                rows, report, csv_text = rows_of(raw)
                row = rows[1]
                self.assertEqual([int(row[f"point_{c}_reported_u16"]) for c in
                                  ("red", "green", "blue")], list(rgb))
                self.assertEqual(row["point_x_m"], "1.23")
                self.assertEqual(row["point_synthetic_flag"], "1")
                self.assertEqual(row["point_source_id_reported"], "17")
                self.assertEqual(row["point_gps_time_reported"], "" if tick is None else str(tick))
                self.assertEqual(row["source_file_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["points_read"], 1)
                common = read_common(self, csv_text, 2)
                carried = common["observations"][1]["fields"]
                self.assertEqual([carried[f"point_{c}_reported_u16"] for c in
                                  ("red", "green", "blue")], list(rgb))
                serialized = json.dumps(common)
                for c in ("red", "green", "blue"):
                    self.assertIn(f"point_{c}_reported_u16", serialized)
                self.assertIn("REPORTED_NORMALIZED_16BIT_RGB_NO_COLORSPACE_ASSERTION", serialized)

    def test_rgb_range_selection_keeps_original_index_and_extra_byte_accounting(self):
        raw = las([point(x=i, gps_time=None) + struct.pack("<3H", i, 22, 33) + b"XY"
                   for i in range(5)], point_format=2, point_length=28)
        import csv
        import io
        text, report = module.convert(raw, CAPTURE, first_point=2, point_limit=2)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([r["point_index"] for r in rows[1:]], ["2", "3"])
        self.assertEqual([r["point_red_reported_u16"] for r in rows[1:]], ["2", "3"])
        self.assertEqual(report["point_record_extra_bytes"], 2)
        self.assertEqual(report["points_read"], 2)
        self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
        read_common(self, text, 3)

    def test_truncation_bad_layout_and_nonfinite_time_are_not_rgb_points(self):
        p2 = point(gps_time=None) + struct.pack("<3H", 1, 2, 3)
        p3 = point(gps_time=float("nan")) + struct.pack("<3H", 1, 2, 3)
        for raw in (las([p2], point_format=2, point_length=25),
                    las([p2[:-1]], point_format=2, point_length=26),
                    las([p2], point_format=2, point_length=26, version=(1, 3)),
                    las([p2], point_format=6, point_length=26),
                    las([p3], point_format=3, point_length=34)):
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError):
                    module.convert(raw, CAPTURE)


if __name__ == "__main__":
    unittest.main()
