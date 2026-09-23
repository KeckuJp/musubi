"""Bounded segment export of a saved point cloud: every point, once, in source order.

The clouds here are **generated on demand** from the authored builders in
`tests.test_saved_point_cloud`; no capture file is committed and none is real. An authored
capacity file demonstrates the mechanism and is not field evidence.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "profiles/declared/saved-point-cloud/profile.toml"
SCRIPT = ROOT / "scripts/export_point_cloud_chunks.py"
from tests.test_saved_point_cloud import CAPTURE, las, metre_projected, point, vlr  # noqa: E402


def vlr_pair():
    """Two WKT coordinate reference records, which the adopted path calls a file error."""
    return (vlr(), vlr(payload=b"OTHER\x00"))


def load():
    spec = importlib.util.spec_from_file_location("export_point_cloud_chunks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_point_cloud_chunks"] = module
    spec.loader.exec_module(module)
    return module


def cloud(count, point_format=0, **kwargs):
    """A generated-on-demand authored cloud: point i carries value i, so order is checkable."""
    points = tuple(point(x=index, y=-index, z=index % 5000, intensity=index % 65536,
                         classification=index % 32, source=1,
                         gps_time=None if point_format == 0 else float(index))
                   for index in range(count))
    return las(points=points, point_format=point_format, global_encoding=0,
               records=(metre_projected(vertical=5703),), **kwargs)


def read_common(case, csv_path):
    """Run the configured common reader on one chunk, or skip explicitly."""
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    if not reader:
        case.skipTest("MUSUBI_TELEMETRY_READER not configured: actual common reader check skipped")
    finished = subprocess.run([reader, str(PROFILE), str(csv_path), "--allow-equal-time"],
                              capture_output=True)
    case.assertEqual(finished.returncode, 0,
                     f"configured common reader failed: {finished.stderr[:400]!r}")
    return json.loads(finished.stdout)


class PointCloudChunkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.export = load()

    def run_export(self, raw, points_per_chunk=None, directory=None):
        place = Path(directory) / "out"
        if points_per_chunk is None:
            return self.export.export(raw, CAPTURE, place), place
        return self.export.export(raw, CAPTURE, place, points_per_chunk), place

    def test_every_point_of_a_multi_chunk_cloud_reaches_common_output_once(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, place = self.run_export(cloud(1000), 256, directory)
            self.assertEqual((manifest["points_to_carry"], manifest["points_carried"],
                              manifest["chunk_count"]), (1000, 1000, 4))
            expected, seen = 0, []
            for chunk in manifest["chunks"]:
                self.assertEqual(chunk["first_point_index"], expected)
                self.assertEqual(chunk["last_point_index"], expected + chunk["point_count"] - 1)
                expected += chunk["point_count"]
                seen.append(chunk["point_count"])
            self.assertEqual(expected, 1000)
            self.assertEqual(seen, [256, 256, 256, 232])
            carried, declarations = [], 0
            for chunk in manifest["chunks"]:
                path = place / chunk["chunk"] / "observations.csv"
                common = read_common(self, path)
                fields = [row["fields"] for row in common["observations"]]
                declaration = fields[0]
                self.assertEqual(declaration["las_record_kind"], "FILE_DECLARATION")
                declarations += 1
                self.assertEqual(declaration["source_file_sha256"], manifest["source_sha256"])
                self.assertEqual(declaration["point_count_declared"], 1000)
                self.assertEqual(declaration["chunk_first_point_index"],
                                 chunk["first_point_index"])
                self.assertEqual(declaration["chunk_last_point_index"], chunk["last_point_index"])
                self.assertEqual(declaration["chunk_point_count"], chunk["point_count"])
                self.assertEqual(common["main_rows"], chunk["point_count"] + 1)
                carried.extend(row["point_index"] for row in fields[1:])
            self.assertEqual(declarations, 4)
            self.assertEqual(carried, list(range(1000)))

    def test_the_points_carried_are_the_saved_values_not_a_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, place = self.run_export(cloud(40), 16, directory)
            rows = []
            for chunk in manifest["chunks"]:
                text = (place / chunk["chunk"] / "observations.csv").read_text()
                rows.extend(row for row in csv.DictReader(io.StringIO(text))
                            if row["las_record_kind"] == "POINT_RECORD")
        self.assertEqual([int(row["point_index"]) for row in rows], list(range(40)))
        self.assertEqual([row["point_x"] for row in rows[:3]], ["0.00", "0.01", "0.02"])
        self.assertEqual(rows[39]["point_x"], "0.39")
        self.assertEqual([int(row["point_intensity_reported"]) for row in rows[:3]], [0, 1, 2])

    def test_an_exact_multiple_and_a_short_final_segment_both_cover_the_cloud(self):
        module = self.export
        self.assertEqual(module.segments(8, 4), [(0, 4), (4, 4)])
        self.assertEqual(module.segments(9, 4), [(0, 4), (4, 4), (8, 1)])
        self.assertEqual(module.segments(1, 4), [(0, 1)])
        self.assertEqual(module.segments(0, 4), [])
        for total in (1, 7, 8, 9, 33):
            covered = sum(count for _, count in module.segments(total, 4))
            self.assertEqual(covered, total)

    def test_a_segment_past_the_adopted_per_call_bound_is_refused(self):
        module = self.export
        with self.assertRaises(ValueError) as raised:
            module.segments(10, module.MAX_POINT_ROWS + 1)
        self.assertIn("per-call bound", str(raised.exception))
        for bad in (0, -1, 2.5):
            with self.assertRaises(ValueError):
                module.segments(10, bad)

    def test_too_many_segments_refuses_instead_of_dropping_points(self):
        module = self.export
        with self.assertRaises(ValueError) as raised:
            module.segments(module.MAX_CHUNKS + 1, 1)
        message = str(raised.exception)
        self.assertIn("nothing is written", message)
        self.assertIn("no part of the cloud is dropped", message)

    def test_the_segment_bound_is_arithmetic_and_never_built_first(self):
        module = self.export
        started = time.monotonic()
        for total in (2**64 - 1, 10**18, module.MAX_CHUNKS * 8192 + 1):
            with self.assertRaises(ValueError) as raised:
                module.segments(total, 1)
            self.assertIn("more than this path's bound", str(raised.exception))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(module.segments(module.MAX_CHUNKS, 1)), module.MAX_CHUNKS)

    def test_a_huge_declared_count_in_a_tiny_file_refuses_before_any_scheduling(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = cloud(4, point_count=2**64 - 1, legacy_count=0)
            place = Path(directory) / "out"
            started = time.monotonic()
            with self.assertRaises(ValueError) as raised:
                self.export.export(raw, CAPTURE, place, 1)
            self.assertIn("run past the end", str(raised.exception))
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertFalse(place.exists())

    def test_a_zero_point_cloud_is_carried_as_one_declaration_and_still_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = las(points=(), global_encoding=0, records=(metre_projected(vertical=5703),))
            manifest, place = self.run_export(raw, 8, directory)
            self.assertEqual((manifest["points_to_carry"], manifest["points_carried"],
                              manifest["chunk_count"]), (0, 0, 1))
            self.assertTrue(manifest["declaration_only_chunk"])
            chunk = manifest["chunks"][0]
            self.assertEqual((chunk["point_count"], chunk["first_point_index"],
                              chunk["last_point_index"]), (0, 0, None))
            text = (place / chunk["chunk"] / "observations.csv").read_text()
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual([row["las_record_kind"] for row in rows], ["FILE_DECLARATION"])
            self.assertEqual(rows[0]["point_count_declared"], "0")
            self.assertEqual(rows[0]["chunk_point_count"], "0")
            self.assertEqual(rows[0]["chunk_last_point_index"], "")
            self.assertEqual(rows[0]["coordinate_reference_kind"],
                             "GEOTIFF_DECLARED_QUALIFIED_SUBSET_READ")
            common = read_common(self, place / chunk["chunk"] / "observations.csv")
            self.assertEqual(common["main_rows"], 1)

    def test_the_zero_point_route_still_refuses_a_bad_capture_time(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = las(points=(), global_encoding=0, records=(metre_projected(),))
            for bad in (-1, "1700000000000000", 2**63):
                place = Path(directory) / ("out-%s" % str(bad)[:4])
                with self.assertRaises(ValueError) as raised:
                    self.export.export(raw, bad, place, 8)
                self.assertIn("capture microseconds", str(raised.exception))
                self.assertFalse((place / "manifest.json").exists())

    def test_the_zero_point_route_still_refuses_a_malformed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            for raw, expected in (
                    (las(points=(), signature=b"NOPE"), "does not carry the LAS file signature"),
                    (las(points=(), version=(1, 2)), "reads the LAS 1.4 public header block"),
                    (las(points=(), scales=(0.0, 0.01, 0.001)), "erase every stored coordinate"),
                    (las(points=(), records=(vlr_pair())), "more than one coordinate reference")):
                place = Path(directory) / ("out-%d" % len(expected))
                with self.assertRaises(ValueError) as raised:
                    self.export.export(raw, CAPTURE, place, 8)
                self.assertIn(expected, str(raised.exception))
                self.assertFalse((place / "manifest.json").exists())

    def test_a_declared_count_that_the_file_cannot_hold_is_refused_before_any_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = cloud(10, point_count=40, legacy_count=40)
            place = Path(directory) / "out"
            with self.assertRaises(ValueError) as raised:
                self.export.export(raw, CAPTURE, place, 4)
            self.assertIn("run past the end", str(raised.exception))
            self.assertFalse((place / "manifest.json").exists())

    def test_a_truncated_point_block_is_refused_rather_than_carried_short(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = cloud(20)[:-40]  # two whole point records removed, count unchanged
            place = Path(directory) / "out"
            with self.assertRaises(ValueError) as raised:
                self.export.export(raw, CAPTURE, place, 8)
            self.assertIn("run past the end", str(raised.exception))
            self.assertFalse((place / "manifest.json").exists())

    def test_the_manifest_reconciles_and_binds_the_exact_input(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            raw = cloud(100)
            manifest, place = self.run_export(raw, 30, directory)
            self.assertEqual(manifest["source_sha256"],
                             "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertEqual(manifest["points_carried"], manifest["points_to_carry"])
            self.assertEqual(sum(chunk["point_count"] for chunk in manifest["chunks"]), 100)
            for chunk in manifest["chunks"]:
                written = (place / chunk["chunk"] / "observations.csv").read_bytes()
                self.assertEqual(chunk["csv_sha256"],
                                 "sha256:" + hashlib.sha256(written).hexdigest())
                self.assertEqual(chunk["csv_bytes"], len(written))
            self.assertEqual(manifest["csv_bytes"],
                             sum(chunk["csv_bytes"] for chunk in manifest["chunks"]))
            self.assertIn("--allow-equal-time", manifest["reader_route"])
            self.assertIn("NOTHING_IS_SAMPLED_THINNED", manifest["carriage_basis"])
            saved = json.loads((place / "manifest.json").read_text())
            self.assertEqual(saved, manifest)

    def test_a_segment_range_outside_the_file_is_refused_by_the_adopted_path(self):
        from tests.test_saved_point_cloud import module as adopted
        raw = cloud(10)
        with self.assertRaises(ValueError) as raised:
            adopted.convert(raw, CAPTURE, 11)
        self.assertIn("starts past the point records", str(raised.exception))
        for bad in (-1, "0"):
            with self.assertRaises(ValueError):
                adopted.convert(raw, CAPTURE, bad)
        with self.assertRaises(ValueError):
            adopted.convert(raw, CAPTURE, 0, 0)
        text, report = adopted.convert(raw, CAPTURE, 8, 99)
        self.assertEqual(report["points_read"], 2)
        rows = [row for row in csv.DictReader(io.StringIO(text))
                if row["las_record_kind"] == "POINT_RECORD"]
        self.assertEqual([row["point_index"] for row in rows], ["8", "9"])

    def test_the_command_line_writes_chunks_and_a_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "authored.las"
            source.write_bytes(cloud(50))
            place = Path(directory) / "out"
            self.assertEqual(self.export.main([str(source), str(place),
                                               "--capture-time-us", str(CAPTURE),
                                               "--points-per-chunk", "20"]), 0)
            manifest = json.loads((place / "manifest.json").read_text())
            self.assertEqual(manifest["chunk_count"], 3)
            self.assertEqual(sorted(path.name for path in place.glob("chunk-*")),
                             ["chunk-000000", "chunk-000001", "chunk-000002"])
            self.assertTrue((place / "chunk-000002" / "report.json").exists())


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
