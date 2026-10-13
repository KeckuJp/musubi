"""Exercise the documented authored sample through the public command-line entrypoint."""
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FactsheetSample(unittest.TestCase):
    def test_version_and_original_paths_are_preserved(self):
        source = ROOT / 'examples/saved-inputs/authored-vda21-factsheet.jsonl'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_vda_factsheet_limits.py'),
                str(source), str(output), '--source-format', 'vda-factsheet-2.1', '--capabilities'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / 'source.jsonl').read_bytes(), source.read_bytes())
            rows = list(csv.DictReader(io.StringIO((output / 'limits.csv').read_text())))
            self.assertTrue(rows)
            self.assertTrue(all(row['factsheet_version'] == '2.1.0' for row in rows))
            self.assertIn('NOT_IN_VERSION', {row['disposition'] for row in rows})
            self.assertTrue((output / 'capabilities.csv').is_file())
            self.assertIsInstance(json.loads((output / 'report.json').read_text()), dict)

    def test_default_version_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_vda_factsheet_limits.py'),
                str(ROOT / 'examples/saved-inputs/authored-vda21-factsheet.jsonl'), str(output)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('no successful conversion claim', result.stderr)
            self.assertFalse(output.exists())
