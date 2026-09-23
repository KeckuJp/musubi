"""Nav2 1.1.20 saved map: declaration plus the caller-supplied image, decoded to occupancy cells.

Authored maps and authored PGM bytes. No real map, no Registry, no credentials. The occupancy
values are what the pinned Nav2 rule computes from the supplied pixels; they are not obstacle
truth, not a georeference and not a navigation result.
"""
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

from scripts import convert_rosbag_observations as conv
from scripts.convert_rosbag_observations import (
    MAP_CELL_LIMIT,
    MAP_OUTPUT_LIMIT,
    convert_map_metadata,
    occupancy_of,
    read_pgm_p5,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/nav2-map-content/profile.toml"

YAML = ("image: floor.pgm\nresolution: 0.05\norigin: [-2.0, 3.0, 0.25]\n"
        "negate: false\nfree_thresh: 0.25\noccupied_thresh: 0.65\n")


def pgm(width, height, samples):
    """Binary PGM the way nav2_map_server's own saver writes one."""
    return b"P5\n%d %d\n255\n" % (width, height) + bytes(samples)


def convert(yaml_text, image=None, digest=None, time_us=1_000_000, directory=None):
    base = Path(directory)
    source = base / "map.yaml"
    source.write_text(yaml_text)
    path = None
    if image is not None:
        path = base / "floor.pgm"
        path.write_bytes(image)
        if digest is None:
            digest = hashlib.sha256(image).hexdigest()
    return convert_map_metadata(source, time_us, path, digest)


class Nav2MapContentTests(unittest.TestCase):
    def test_declaration_alone_is_unchanged_and_never_opens_the_referenced_image(self):
        """The adopted metadata result is the default and still holds, byte for byte."""
        with tempfile.TemporaryDirectory() as directory:
            output, report = convert(YAML, directory=directory)
            self.assertEqual(report["output_records"], 1)
            self.assertEqual(report["clock"], "Unknown")
            self.assertIn("image pixels, frame identity and physical map not verified",
                          report["scope"])
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(row["map_declaration_disposition"], "DECLARED_ONLY_IMAGE_NOT_READ")
            self.assertEqual(row["map_origin_basis"], "CELL_0_0_BOTTOM_LEFT_NOT_GEODETIC")
            self.assertEqual(row["map_load_time_state"], "NOT_IN_YAML")
            self.assertNotIn("map_cell_occupancy", row)
            self.assertFalse((Path(directory) / "floor.pgm").exists())

    def test_two_asymmetric_maps_reach_the_common_output_with_every_cell_accounted(self):
        """Two authored maps, neither square nor symmetric, through the real common reader."""
        first = pgm(3, 2, [255, 255, 0, 0, 0, 255])
        second = pgm(4, 3, [0] * 4 + [128] * 4 + [255] * 4)
        for image, width, height in ((first, 3, 2), (second, 4, 3)):
            with tempfile.TemporaryDirectory() as directory:
                output, report = convert(YAML, image, directory=directory)
                rows = list(csv.DictReader(io.StringIO(output)))
                head = rows[0]
                self.assertEqual(head["map_declaration_disposition"],
                                 "DECLARED_AND_CALLER_SUPPLIED_IMAGE_READ")
                self.assertEqual(int(head["map_cell_columns"]), width)
                self.assertEqual(int(head["map_cell_rows"]), height)
                self.assertEqual(int(head["map_cell_total"]), width * height)
                self.assertEqual(int(head["map_cell_accounted"]), width * height)
                self.assertEqual(head["map_image_sha256"], hashlib.sha256(image).hexdigest())
                self.assertEqual(head["map_image_format"], "PGM_P5_MAXVAL_255")
                self.assertEqual(head["map_image_reference_match"],
                                 "CALLER_FILE_NAME_MATCHES_DECLARED_REFERENCE")
                self.assertEqual(report["output_records"], len(rows))

                cells = rows[1:]
                covered = []
                for cell in cells:
                    start, length = int(cell["map_cell_run_start_index"]), int(cell["map_cell_run_length"])
                    self.assertEqual(int(cell["map_cell_run_end_index"]), start + length - 1)
                    self.assertEqual(int(cell["map_cell_run_start_row"]), start // width)
                    self.assertEqual(int(cell["map_cell_run_start_column"]), start % width)
                    covered.extend(range(start, start + length))
                    value = int(cell["map_cell_occupancy"])
                    self.assertTrue(value == -1 or 0 <= value <= 100, value)
                self.assertEqual(covered, list(range(width * height)), "every cell exactly once")

                if width == 3:
                    flat = []
                    for cell in cells:
                        flat.extend([int(cell["map_cell_occupancy"])] * int(cell["map_cell_run_length"]))
                    self.assertEqual(flat, [100, 100, 0, 0, 0, 100])

                converted = Path(directory) / "converted.csv"
                converted.write_text(output)
                reader = ROOT / "target/debug/examples/read_telemetry_csv"
                if reader.exists():
                    result = subprocess.run(
                        [str(reader), str(PROFILE), str(converted), "--allow-equal-time"],
                        capture_output=True, text=True, check=True)
                    common = json.loads(result.stdout)
                    self.assertEqual(common["equal_time_order"], "source-order-only")
                    observations = common["observations"]
                    self.assertEqual(len(observations), len(rows))
                    self.assertEqual(observations[0]["clock_basis"], "Unknown")
                    self.assertEqual(common["platform_domain"], "Unknown")
                    units = common["profile_units"]
                    self.assertIn("percent", units["map_cell_occupancy"])
                    self.assertIn("not_an_obstacle_measurement", units["map_cell_occupancy"])
                    self.assertIn("m_per_cell", units["map_resolution_m_per_cell"])
                    carried = [o["fields"]["map_cell_occupancy"] for o in observations[1:]]
                    self.assertEqual(
                        carried, [int(c["map_cell_occupancy"]) for c in rows[1:]])

    def test_each_mode_follows_the_pinned_rule_including_unknown_and_negate(self):
        """All three Nav2 modes, kept whole; none was dropped to make a test pass."""
        free, occupied = 0.25, 0.65
        self.assertEqual(occupancy_of(0, "trinary", False, free, occupied), 100)
        self.assertEqual(occupancy_of(255, "trinary", False, free, occupied), 0)
        self.assertEqual(occupancy_of(128, "trinary", False, free, occupied), -1)
        self.assertEqual(occupancy_of(0, "trinary", True, free, occupied), 0)
        self.assertEqual(occupancy_of(255, "trinary", True, free, occupied), 100)
        self.assertEqual(occupancy_of(128, "scale", False, free, occupied), 62)
        self.assertEqual(occupancy_of(0, "scale", False, free, occupied), 100)
        self.assertEqual(occupancy_of(255, "scale", False, free, occupied), 0)
        self.assertEqual(occupancy_of(0, "raw", False, free, occupied), 0)
        self.assertEqual(occupancy_of(100, "raw", False, free, occupied), 100)
        self.assertEqual(occupancy_of(101, "raw", False, free, occupied), -1)
        self.assertEqual(occupancy_of(255, "raw", False, free, occupied), -1)
        self.assertEqual(occupancy_of(60, "raw", True, free, occupied),
                         occupancy_of(60, "raw", False, free, occupied))

        for mode, expected in (("trinary", -1), ("scale", 62), ("raw", -1)):
            with tempfile.TemporaryDirectory() as directory:
                output, _ = convert(YAML + f"mode: {mode}\n", pgm(1, 1, [128]), directory=directory)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(rows[0]["map_mode"], mode.upper())
                self.assertEqual(int(rows[1]["map_cell_occupancy"]), expected, mode)

    def test_a_practical_map_runs_at_real_size_without_dropping_cells(self):
        """250 x 200 = 50 000 cells, carried compactly rather than by shrinking the map."""
        width, height = 250, 200
        samples = bytearray(width * height)
        for row in range(height):
            for column in range(width):
                border = row < 2 or column < 2 or row >= height - 2 or column >= width - 2
                samples[row * width + column] = 0 if border or (40 < row < 90 and 30 < column < 200) else 255
        image = bytes(pgm(width, height, samples))
        with tempfile.TemporaryDirectory() as directory:
            output, report = convert(YAML, image, directory=directory)
            rows = list(csv.DictReader(io.StringIO(output)))
            head = rows[0]
            self.assertEqual(int(head["map_cell_total"]), width * height)
            self.assertEqual(int(head["map_cell_accounted"]), width * height)
            self.assertEqual(int(head["map_cell_run_count"]), len(rows) - 1)
            self.assertLess(len(rows), width * height // 10)
            covered = sum(int(cell["map_cell_run_length"]) for cell in rows[1:])
            self.assertEqual(covered, width * height)
            self.assertEqual(report["output_records"], len(rows))

    def test_bad_inputs_are_refused_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            good = pgm(2, 2, [0, 255, 0, 255])
            with self.assertRaises(ValueError):
                convert(YAML, good, digest="0" * 64, directory=directory)
            with self.assertRaises(ValueError):
                convert(YAML, good, digest="not-hex", directory=directory)
            source = Path(directory) / "map.yaml"
            source.write_text(YAML)
            image = Path(directory) / "floor.pgm"
            image.write_bytes(good)
            with self.assertRaises(ValueError):
                convert_map_metadata(source, 1, image, None)
            with self.assertRaises(ValueError):
                convert_map_metadata(source, 1, None, hashlib.sha256(good).hexdigest())

        for bad, why in (
                (b"P2\n2 2\n255\n0 255 0 255\n", "ASCII PGM is not qualified"),
                (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "PNG is not qualified"),
                (b"P5\n2 2\n65535\n" + b"\x00" * 8, "16-bit maxval is not scaled here"),
                (b"P5\n2 2\n255\n\x00\x00", "truncated pixel data"),
                (b"P5\n2 2\n255\n" + b"\x00" * 6, "bytes beyond the declared dimensions"),
                (b"P5\n0 2\n255\n", "zero width"),
                (b"P52 3\n255\n" + b"\x00" * 6, "magic not followed by whitespace"),
                (b"P5X\n2 2\n255\n" + b"\x00" * 4, "magic followed by a non-space byte"),
                (b"P5\n2 x\n255\n", "non-decimal header token"),
                (b"", "empty file")):
            with self.assertRaises(ValueError, msg=why):
                read_pgm_p5(bad)

        width, height, samples = read_pgm_p5(b"P5\n# saved by a map saver\n2 2\n255\n\x00\x01\x02\x03")
        self.assertEqual((width, height, bytes(samples)), (2, 2, b"\x00\x01\x02\x03"))

        self.assertEqual(MAP_CELL_LIMIT, 16 * 1024 * 1024)
        with self.assertRaises(ValueError):
            read_pgm_p5(b"P5\n65536 65536\n255\n")

    def test_a_high_transition_map_stays_within_the_output_bound_and_refuses_beyond_it(self):
        """Worst-case row expansion: alternating cells, so nearly every cell is its own run."""
        width, height = 160, 120
        samples = bytearray(
            (0 if (row * width + column + row) % 2 else 255)
            for row in range(height) for column in range(width))
        image = bytes(pgm(width, height, samples))
        with tempfile.TemporaryDirectory() as directory:
            output, report = convert(YAML, image, directory=directory)
            rows = list(csv.DictReader(io.StringIO(output)))
            head = rows[0]
            total = width * height
            self.assertEqual(int(head["map_cell_total"]), total)
            self.assertEqual(int(head["map_cell_accounted"]), total)
            self.assertGreater(int(head["map_cell_run_count"]), total * 9 // 10)
            self.assertEqual(report["output_records"], len(rows))
            self.assertEqual(sum(int(c["map_cell_run_length"]) for c in rows[1:]), total)
            self.assertLess(len(output), MAP_OUTPUT_LIMIT)
            self.assertEqual(rows[1]["map_resolution_m_per_cell"], "")
            self.assertEqual(rows[1]["map_declaration_schema"], "")
            self.assertNotEqual(rows[1]["record_time_us"], "")

            converted = Path(directory) / "converted.csv"
            converted.write_text(output)
            reader = ROOT / "target/debug/examples/read_telemetry_csv"
            if reader.exists():
                result = subprocess.run(
                    [str(reader), str(PROFILE), str(converted), "--allow-equal-time"],
                    capture_output=True, text=True, check=True)
                self.assertEqual(len(json.loads(result.stdout)["observations"]), len(rows))

        with tempfile.TemporaryDirectory() as directory:
            original = conv.MAP_OUTPUT_LIMIT
            try:
                conv.MAP_OUTPUT_LIMIT = 4096
                with self.assertRaises(ValueError) as caught:
                    convert(YAML, image, directory=directory)
            finally:
                conv.MAP_OUTPUT_LIMIT = original
            self.assertIn("exceeds bound", str(caught.exception))
            self.assertIn("no cell was sampled or dropped", str(caught.exception))
        self.assertEqual(MAP_OUTPUT_LIMIT, 128 * 1024 * 1024)

    def test_the_cli_route_needs_the_map_declaration_and_refuses_a_stray_image(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "map.yaml").write_text(YAML)
            image = pgm(2, 2, [0, 255, 0, 255])
            (base / "floor.pgm").write_bytes(image)
            out = base / "cli"
            command = [sys.executable, "-m", "scripts.convert_rosbag_observations",
                       str(base / "map.yaml"), str(out), "--map-metadata-only",
                       "--capture-time-us", "5", "--map-image", str(base / "floor.pgm"),
                       "--map-image-sha256", hashlib.sha256(image).hexdigest()]
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            done = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            rows = list(csv.DictReader(io.StringIO((out / "observations.csv").read_text())))
            self.assertEqual(rows[0]["map_declaration_disposition"],
                             "DECLARED_AND_CALLER_SUPPLIED_IMAGE_READ")
            self.assertEqual(sum(int(r["map_cell_run_length"]) for r in rows[1:]), 4)

            for extra in (["--parameters-only", "--capture-time-us", "5"], ["--metadata-only"]):
                stray = subprocess.run(
                    [sys.executable, "-m", "scripts.convert_rosbag_observations",
                     str(base / "map.yaml"), str(base / "other"), *extra,
                     "--map-image", str(base / "floor.pgm")],
                    capture_output=True, text=True, cwd=ROOT, env=env)
                self.assertNotEqual(stray.returncode, 0, extra)
                self.assertFalse((base / "other").exists(), extra)


if __name__ == "__main__":
    unittest.main()
