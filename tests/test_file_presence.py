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
