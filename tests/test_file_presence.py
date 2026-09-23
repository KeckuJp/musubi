import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.record_file_presence import capture

ROOT = Path(__file__).resolve().parents[1]


class FilePresenceTests(unittest.TestCase):
    def test_real_local_entry_states_and_shared_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in (("empty.dat", b""), ("separate.dat", b"separate-content")):
                source = root / name; source.write_bytes(content)
                os.utime(source, ns=(1000000000, 1234567890))
                output, report = capture(source)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(row["regular_file_size_bytes"], str(len(content)))
                self.assertEqual(row["entry_kind"], "REGULAR_FILE")
                self.assertEqual(int(row["filesystem_mtime_ns"]), source.stat().st_mtime_ns)
                self.assertNotEqual(int(row["record_time_us"]), source.stat().st_mtime_ns // 1000)
                self.assertEqual(bytes.fromhex(row["source_path_hex"][4:]).decode(), str(source))
                self.assertFalse(report["content_read"])
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    csv_path = root / "output.csv"; csv_path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/file-presence/profile.toml"),
                        str(csv_path)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 1)
                    self.assertEqual(common["observations"][0]["fields"]["regular_file_size_bytes"], len(content))
                    self.assertIsNone(common["observations"][0]["anchor_unix_us"])
            absent_csv = capture(root / "missing")[0]
            absent = next(csv.DictReader(io.StringIO(absent_csv)))
            self.assertEqual((absent["entry_present"], absent["entry_kind"]), ("0", "ABSENT"))
            self.assertEqual(absent["regular_file_size_bytes"], "")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                csv_path = root / "absent.csv"; csv_path.write_text(absent_csv)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/file-presence/profile.toml"),
                    str(csv_path)], check=True, capture_output=True).stdout)
                self.assertEqual(common["observations"][0]["fields"]["entry_present"], 0)
                self.assertEqual(common["observations"][0]["fields"]["entry_kind"], "ABSENT")
            link = root / "link"; link.symlink_to(root / "missing")
            linked = next(csv.DictReader(io.StringIO(capture(link)[0])))
            self.assertEqual((linked["entry_present"], linked["entry_kind"]), ("1", "SYMLINK_NOT_FOLLOWED"))
            self.assertEqual(linked["regular_file_size_bytes"], "")
            self.assertEqual(next(csv.DictReader(io.StringIO(capture(root)[0])))["entry_kind"], "DIRECTORY")

    def test_permission_failure_is_not_absence_and_cli_preserves_existing_output(self):
        with patch("scripts.record_file_presence.os.lstat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                capture("not-read")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory); marker = path / "keep"; marker.write_text("unchanged")
            result = subprocess.run([os.sys.executable, str(ROOT / "scripts/record_file_presence.py"),
                                     str(path / "absent"), str(path)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(marker.read_text(), "unchanged")
            self.assertFalse((path / "observations.csv").exists())

    def test_supplied_spelling_preserved_and_trailing_slash_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file").write_bytes(b"x")
            supplied = directory + "/./file"
            row = next(csv.DictReader(io.StringIO(capture(supplied)[0])))
            self.assertEqual(bytes.fromhex(row["source_path_hex"][4:]).decode(), supplied)
            target = root / "target"; target.mkdir()
            link = root / "link"; link.symlink_to(target)
            self.assertEqual(next(csv.DictReader(io.StringIO(capture(str(link))[0])))["entry_kind"],
                             "SYMLINK_NOT_FOLLOWED")
            for invalid in (str(link) + "/", "", "bad\x00path", b"bytes"):
                with self.assertRaises(ValueError):
                    capture(invalid)


if __name__ == "__main__":
    unittest.main()
