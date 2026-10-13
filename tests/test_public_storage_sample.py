"""Exercise the documented authored sample and refusal through the CLI."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PublicStorageSample(unittest.TestCase):
    def test_documented_sample_retains_source_and_distinct_free_counters(self):
        sample = ROOT / 'examples/saved-inputs/authored-filesystem.jsonl'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'output'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_saved_filesystem_capacity.py'), str(sample), str(out)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((out / 'source.jsonl').read_bytes(), sample.read_bytes())
            with (out / 'observations.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['root_free_bytes_exact'], rows[0]['user_available_bytes_exact'], rows[0]['reserved_difference_bytes_exact']), ('integer:400', 'integer:350', 'integer:50'))
            self.assertEqual(json.loads((out / 'report.json').read_text())['source_records'], 1)

    def test_inconsistent_percent_is_refused_without_output(self):
        record = json.loads((ROOT / 'examples/saved-inputs/authored-filesystem.jsonl').read_text())
        record['disk_usage']['percent'] = 60
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'invalid.jsonl'; source.write_text(json.dumps(record) + '\n')
            out = Path(tmp) / 'output'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_saved_filesystem_capacity.py'), str(source), str(out)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'percent inconsistent', result.stderr)
            self.assertFalse(out.exists())
