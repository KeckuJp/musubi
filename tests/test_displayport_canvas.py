"""Authored saved-operation checks, not physical display or recorded-device proof."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_displayport_canvas as canvas
from scripts.convert_crsf_link import convert as operations

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / 'profiles/declared/crsf-link'


def reply(payload, code=182):
    body = bytes([len(payload), code]) + bytes(payload)
    checksum = 0
    for byte in body:
        checksum ^= byte
    return b'$M>' + body + bytes([checksum])


def capture(frames):
    return 'time_us,frame_hex,unknown_hint\n' + ''.join(
        f'{time},{frame.hex()},retained\n' for time, frame in frames)


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class CanvasTests(unittest.TestCase):
    def common(self, text, profile):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not reader:
            self.fail('actual common reader required; set MUSUBI_TELEMETRY_READER')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.csv'
            path.write_text(text)
            result = subprocess.run([reader, str(CASE / profile), str(path),
                                     '--allow-equal-time'], capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_ordered_model_and_both_common_views(self):
        source = capture([(7, reply(p)) for p in (
            [4], [2], [3, 0, 0, 0x41, 65, 128], [3, 0, 1, 2, 255],
            [3, 1, 0, 0, 66], [3, 1, 2, 0], [4], [1], [4],
            [3, 0, 0, 0, 67], [9], [4], [0])])
        op, out, report = canvas.convert(source, rows=2, columns=3)
        expected_op, expected_report = operations(source, 'msp-displayport-bf452')
        self.assertEqual((op, report['decoder']), (expected_op, expected_report))
        result = rows(out)
        summaries = [r for r in result if r['row_kind'] == 'FRAME_SUMMARY']
        self.assertEqual([(r['known_glyph_count'], r['cleared_count'], r['unknown_count'])
                         for r in summaries], [('0', '0', '6'), ('3', '3', '0'),
                                               ('0', '0', '6'), ('0', '0', '6')])
        cells = [r for r in result if r['row_kind'] == 'GLYPH_CELL']
        self.assertEqual([r['glyph_byte'] for r in cells], ['65', '255', '66'])
        self.assertEqual([r['write_record_index'] for r in cells], ['2', '3', '4'])
        self.assertEqual((cells[0]['font_bank'], cells[0]['blink_reported']), ('1', 'True'))
        self.assertEqual((report['frames'], report['trailing_source_records']), (4, 1))
        self.assertEqual(summaries[-1]['last_invalidation_record_index'], '10')
        for text, profile, count in ((op, 'profile.toml', 12), (out, 'canvas-profile.toml', 7)):
            common = self.common(text, profile)
            self.assertEqual(common['main_rows'], count)
            self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
        self.assertEqual(self.common(out, 'canvas-profile.toml')['observations'][2]['fields']['glyph_byte'], 65)

    def test_important_refusals_and_fresh_cli(self):
        good = capture([(1, reply([4]))])
        for dims in ((True, 2), (0, 2), (257, 1), (256, 256)):
            with self.assertRaises(ValueError): canvas.convert(good, rows=dims[0], columns=dims[1])
        for source in (capture([(1, reply([3, 0, 2, 0, 65, 66])), (1, reply([4]))]),
                       capture([(1, reply([2]))]), good.replace('244d3e01b604b3', '244d3e01b604b2')):
            with self.assertRaises(ValueError): canvas.convert(source, rows=2, columns=3)
        for bound in ('MAX_RECORDS', 'MAX_FRAMES', 'MAX_ROWS', 'MAX_OUTPUT', 'MAX_REPORT'):
            with patch.object(canvas, bound, 0), self.assertRaises(ValueError):
                canvas.convert(good, rows=2, columns=3)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / 'in.csv'; source.write_text(good)
            args = [os.sys.executable, str(ROOT / 'scripts/convert_displayport_canvas.py'),
                    str(source), str(base / 'out'), '--rows', '2', '--columns', '3']
            subprocess.run(args, capture_output=True, check=True)
            self.assertEqual((base / 'out/source.csv').read_text(), good)
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)

    def test_thousand_frames_reuse_and_nonselected_accounting(self):
        frames = [(0, reply([2])), (0, reply([3, 0, 0, 0, 250]))]
        frames += [(i, reply([4])) for i in range(1000)]
        frames.insert(3, (0, reply([0], code=100)))
        source = capture(frames)
        op, out, report = canvas.convert(source, rows=1, columns=2)
        self.assertEqual((op, report['decoder']), operations(source, 'msp-displayport-bf452'))
        self.assertEqual((report['frames'], report['canvas_rows']), (1000, 2000))
        self.assertEqual(rows(out)[-1]['draw_record_index'], '1002')
        self.assertEqual(rows(out)[-1]['glyph_byte'], '250')
        self.assertNotIn('source_record_hex', out)
        self.assertEqual(self.common(out, 'canvas-profile.toml')['main_rows'], 2000)
        self.assertEqual(self.common(op, 'profile.toml')['main_rows'], 1002)
