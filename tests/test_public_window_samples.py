"""Shipped authored sample and CLI refusal checks; no device recording."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PublicWindowSamples(unittest.TestCase):
    def test_selected_can_window_preserves_original_line_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result'
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_candump_recorded.py'),
                str(ROOT / 'examples/saved-inputs/authored-can-window.candump'), str(output),
                '--interface', 'can0', '--start-line', '0', '--stop-line', '3'], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            with (output / 'capture.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row['data_hex'] for row in rows], ['0102', '0506'])
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['selected_frames'], 2)
            self.assertEqual(report['unconverted_lines'], 1)
            self.assertEqual([row['original_line'] for row in report['records']], [1, 2, 3])
            self.assertEqual(report['source_window']['total_physical_lines'], 4)
            self.assertEqual(report['source_window']['excluded_physical_lines'], 1)

    def test_malformed_gzip_cli_returns_controlled_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'authored-invalid.gz'
            source.write_bytes(b'authored invalid gzip bytes')
            output = Path(tmp) / 'result'
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_imc_gzip_window.py'),
                str(source), str(output), '--start-record', '0', '--stop-record', '1'], capture_output=True)
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertIn(b'conversion refused', run.stderr)
            self.assertNotIn(b'Traceback', run.stderr)
            self.assertFalse(output.exists())
