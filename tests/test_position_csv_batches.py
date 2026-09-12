"""Authored synthetic contracts for bounded, lossless position CSV batches."""
import copy
from contextlib import redirect_stderr
import hashlib
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(os.environ.get("MUSUBI_POSITION_CSV_ROOT", Path(__file__).resolve().parents[1]))
SCRIPT = ROOT / "scripts" / "convert_position_csv_batches.py"
PROFILE = {
    "message_type": "RECORDED_POSITION", "counter_field": "clock", "counter_unit": "us",
    "latitude_field": "north", "longitude_field": "east", "angle_unit": "deg",
    "altitude_field": "height", "altitude_unit": "m", "altitude_reference": "msl",
}
HEADER = b"clock,north,east,height,note\n"
SIMPLE = HEADER + b"3,1,2,3,first\n3,4,5,6,second\n2,7,8,9,last\n"
MIB = 1024 * 1024


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(ROOT / "scripts")] + sys.path):
        spec.loader.exec_module(module)
    return module


class PositionCsvBatchContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = load_script("batch_under_test", SCRIPT)
        cls.old = load_script("old_position_under_test", ROOT / "scripts" / "convert_position_csv.py")

    def test_small_result_is_byte_identical_and_does_not_mutate_profile(self):
        profile = copy.deepcopy(PROFILE)
        parts = self.batch.convert_batches(SIMPLE, profile)
        self.assertEqual(parts, [self.old.convert(SIMPLE, PROFILE)])
        self.assertEqual(profile, PROFILE)

    def test_parts_keep_whole_records_source_order_equal_times_and_unknown_text(self):
        raw = HEADER + b'3,1,2,3,"a,quote"\n3,4,5,6,"line\nbreak"\n2,7,8,9,\xe6\x9c\xaa\xe7\x9f\xa5\n'
        expected = self.old.convert(raw, PROFILE)
        longest = max(map(len, expected.splitlines(keepends=True)))
        parts = self.batch.convert_batches(raw, PROFILE, max_part_bytes=longest)
        self.assertEqual(len(parts), 3)
        self.assertEqual(b"".join(parts), expected)
        self.assertTrue(all(type(part) is bytes and part.endswith(b"\n") for part in parts))
        self.assertTrue(all(len(part) <= longest for part in parts))
        rows = [json.loads(part) for part in parts]
        self.assertEqual([r["data"]["TimeUS"] for r in rows], [3, 3, 2])
        self.assertEqual([r["data"]["source_record"]["note"] for r in rows],
                         ["a,quote", "line\nbreak", "未知"])

    def test_exact_record_boundary_allowed_but_smaller_part_rejected(self):
        raw = HEADER + b"1,2,3,4,x\n"
        size = len(self.old.convert(raw, PROFILE))
        self.assertEqual(len(self.batch.convert_batches(raw, PROFILE, size)[0]), size)
        with self.assertRaises(ValueError):
            self.batch.convert_batches(raw, PROFILE, size - 1)

    def test_part_limit_requires_exact_bounded_integer(self):
        for value in (True, False, None, "1024", 1024.0, 0, -1, 16 * MIB + 1):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.batch.convert_batches(SIMPLE, PROFILE, value)

    def test_invalid_units_missing_fields_and_invalid_late_rows_rejected(self):
        for changes in ({"counter_unit": "s"}, {"angle_unit": "rad"},
                        {"altitude_reference": "ellipsoid"}, {"counter_field": "absent"},
                        {"extra": "ignored"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.batch.convert_batches(SIMPLE, {**PROFILE, **changes})
        for raw in (SIMPLE + b"4,not-a-number,2,3,sensitive-marker\n",
                    SIMPLE + b"4,1,2\n", HEADER,
                    b'{"data":{"TimeUS":1}}\n', b"x" * (16 * MIB + 1)):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.batch.convert_batches(raw, PROFILE)

    def test_total_output_bound_counts_all_parts_and_newlines(self):
        expected = self.old.convert(SIMPLE, PROFILE)
        part_limit = max(map(len, expected.splitlines(keepends=True)))
        with patch.object(self.batch, "MAX_TOTAL_OUTPUT", len(expected)):
            self.assertEqual(b"".join(self.batch.convert_batches(SIMPLE, PROFILE, part_limit)), expected)
        with patch.object(self.batch, "MAX_TOTAL_OUTPUT", len(expected) - 1):
            with self.assertRaises(ValueError):
                self.batch.convert_batches(SIMPLE, PROFILE, part_limit)

    def test_real_scale_expansion_is_batched_without_relaxing_old_converter(self):
        # Authored repeated cells, not a recorded input. JSON escaping makes output
        # larger than 16 MiB while the original CSV stays below its 16 MiB limit.
        row = b"1,2,3,4," + ("未知" * 100).encode() + b"\n"
        count = 13000
        raw = HEADER + row * count
        self.assertLess(len(raw), 16 * MIB)
        with self.assertRaises(ValueError):
            self.old.convert(raw, PROFILE)
        parts = self.batch.convert_batches(raw, PROFILE)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < len(part) <= 16 * MIB for part in parts))
        self.assertLessEqual(sum(map(len, parts)), 64 * MIB)
        self.assertEqual(sum(part.count(b"\n") for part in parts), count)
        single = self.old.convert(HEADER + row, PROFILE)
        self.assertTrue(all(line + b"\n" == single for part in parts for line in part.splitlines()))


class PositionCsvBatchCliContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = load_script("batch_cli_under_test", SCRIPT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sensitive-source.csv"
        self.profile = self.root / "sensitive-profile.json"
        self.output = self.root / "new-output"
        self.source.write_bytes(SIMPLE)
        # Exact-byte hashes must reflect whitespace, not a reserialized object.
        self.profile.write_bytes((json.dumps(PROFILE, indent=2) + "\n").encode())

    def invoke(self):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.source),
                               str(self.profile), str(self.output)], capture_output=True, timeout=30)

    def assert_generic_failure(self, result):
        self.assertEqual(result.returncode, 1)
        output = result.stdout + result.stderr
        self.assertNotIn(str(self.root).encode(), output)
        for forbidden in (b"sensitive-source", b"sensitive-profile", b"sensitive-marker", b"Traceback"):
            self.assertNotIn(forbidden, output)

    def assert_manifest(self):
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        self.assertEqual(manifest["schema"], "position-csv-batch/v1")
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(manifest["profile_sha256"], hashlib.sha256(self.profile.read_bytes()).hexdigest())
        index = 0
        names = ["manifest.json"]
        for number, entry in enumerate(manifest["parts"], 1):
            name = f"part-{number:06d}.jsonl"
            self.assertEqual(entry["name"], name)
            names.append(name)
            content = (self.output / name).read_bytes()
            self.assertEqual(entry["first_record_index"], index)
            self.assertEqual(entry["records"], len(content.splitlines()))
            self.assertEqual(entry["bytes"], len(content))
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
            index += entry["records"]
        self.assertEqual(manifest["records"], index)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), sorted(names))
        return manifest

    def test_cli_success_manifest_covers_every_part(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.assert_manifest()["records"], 3)

    def test_manifest_offsets_for_multiple_parts_and_manifest_written_last(self):
        real_convert = self.batch.convert_batches
        def small_parts(raw, profile):
            return real_convert(raw, profile, max_part_bytes=300)
        opened = []
        real_open = Path.open
        def track_open(path, *args, **kwargs):
            if path.parent == self.output:
                opened.append(path.name)
            return real_open(path, *args, **kwargs)
        with patch.object(self.batch, "convert_batches", small_parts), patch.object(Path, "open", track_open):
            result = self.batch.main([str(SCRIPT), str(self.source), str(self.profile), str(self.output)])
        self.assertEqual(result, 0)
        manifest = self.assert_manifest()
        self.assertEqual(len(manifest["parts"]), 3)
        self.assertEqual(opened[-1], "manifest.json")

    def test_late_malformed_record_does_not_create_output_directory(self):
        self.source.write_bytes(SIMPLE + b"4,bad,2,3,sensitive-marker\n")
        self.assert_generic_failure(self.invoke())
        self.assertFalse(self.output.exists())

    def test_invalid_profile_and_input_reject_before_creating_directory(self):
        self.profile.write_bytes(b'{"counter_unit":"sensitive-marker"}')
        self.assert_generic_failure(self.invoke())
        self.assertFalse(self.output.exists())

    def test_existing_directory_file_and_symlink_are_never_modified(self):
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                self.output = self.root / kind
                if kind == "directory":
                    self.output.mkdir()
                    (self.output / "keep").write_bytes(b"unchanged")
                elif kind == "file":
                    self.output.write_bytes(b"unchanged")
                else:
                    self.output.symlink_to(self.root / "absent-target", target_is_directory=True)
                self.assert_generic_failure(self.invoke())
                if kind == "directory":
                    self.assertEqual([p.name for p in self.output.iterdir()], ["keep"])
                    self.assertEqual((self.output / "keep").read_bytes(), b"unchanged")
                elif kind == "file":
                    self.assertEqual(self.output.read_bytes(), b"unchanged")
                else:
                    self.assertTrue(self.output.is_symlink())
                    self.assertFalse((self.root / "absent-target").exists())

    def test_part_write_failure_never_leaves_a_completion_manifest(self):
        real_open = Path.open
        def fail_part(path, *args, **kwargs):
            if path.parent == self.output and path.name.startswith("part-"):
                raise OSError("sensitive-marker")
            return real_open(path, *args, **kwargs)
        error = StringIO()
        with patch.object(Path, "open", fail_part), redirect_stderr(error):
            result = self.batch.main([str(SCRIPT), str(self.source), str(self.profile), str(self.output)])
        self.assertEqual(result, 1)
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertNotIn("sensitive-marker", error.getvalue())
        self.assertNotIn(str(self.root), error.getvalue())

    def test_manifest_write_failure_does_not_leave_an_incomplete_completion_marker(self):
        real_open = Path.open
        class BrokenWrite:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, value):
                self.stream.write(value[:1])
                raise OSError("sensitive-marker")
        def fail_manifest(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            if path.parent == self.output and path.name == "manifest.json":
                return BrokenWrite(stream)
            return stream
        error = StringIO()
        with patch.object(Path, "open", fail_manifest), redirect_stderr(error):
            result = self.batch.main([str(SCRIPT), str(self.source), str(self.profile), str(self.output)])
        self.assertEqual(result, 1)
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertNotIn("sensitive-marker", error.getvalue())
        self.assertNotIn(str(self.root), error.getvalue())


if __name__ == "__main__":
    unittest.main()
