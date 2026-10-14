"""Fixed schema authored captures, not a real vehicle diagnosis or recording."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_mavlink_statustext as status
from scripts.assemble_saved_mavlink_ulog import crc_x25

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'profiles/declared/tlog-state/statustext-profile.toml'


def frame(text, *, identity=0, sequence=0, severity=6, sysid=1, compid=1, stamp=100,
          msgid=253, trim=True, signed=False):
    payload = bytes([severity]) + text.ljust(50, b'\0') + identity.to_bytes(2, 'little') + bytes([sequence])
    if trim:
        payload = payload.rstrip(b'\0') or b'\0'
    header = bytes([len(payload), int(signed), 0, 23, sysid, compid]) + msgid.to_bytes(3, 'little')
    checksum = crc_x25(b'\x53', crc_x25(header + payload)).to_bytes(2, 'little')
    return stamp.to_bytes(8, 'big') + b'\xfd' + header + payload + checksum + (b'\0' * 13 if signed else b'')


def convert(data, layout='unsigned-us'):
    text, report = status.convert(data, timestamp_layout=layout)
    return list(csv.DictReader(io.StringIO(text))), text, report


class StatusTextTests(unittest.TestCase):
    def common(self, text):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.csv'; path.write_text(text)
            result = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_complete_interleaved_utf8_and_common(self):
        # A multibyte character crosses the wire chunk boundary; decode only after assembly.
        raw = b'A' * 49 + '温度'.encode() + b' reported'
        source = (frame(raw[:50], identity=7, stamp=100) +
                  frame(b'other system', identity=7, sysid=2, stamp=101) +
                  frame(raw[50:], identity=7, sequence=1, stamp=102, signed=True) +
                  frame(b'X' * 50, identity=0, severity=250, stamp=102) +
                  frame(b'unselected', msgid=42, stamp=103))
        rows, text, report = convert(source)
        self.assertEqual([r['text_utf8'] for r in rows], ['utf8:other system', 'utf8:' + raw.decode(), 'utf8:' + 'X' * 50])
        self.assertTrue(all(r['assembly_status'] == 'COMPLETE_REPORTED_TEXT' for r in rows))
        self.assertEqual(rows[-1]['severity_name'], 'UNKNOWN')
        refs = json.loads(bytes.fromhex(rows[1]['frame_references_hex']))
        self.assertEqual([r['source_record_index'] for r in refs], [0, 2])
        self.assertTrue(refs[-1]['signed_but_not_authenticated'])
        self.assertEqual((report['source_records'], report['selected_chunks'],
                          report['unselected_records'], report['complete_messages']), (5, 4, 1, 3))
        common = self.common(text)
        self.assertEqual(common['main_rows'], 3)
        self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
        self.assertEqual(common['observations'][1]['fields']['text_utf8'], 'utf8:' + raw.decode())

    def test_incomplete_reuse_and_capacity(self):
        source = (frame(b'A' * 50, identity=3, stamp=108) +
                  frame(b'gap', identity=3, sequence=2, stamp=112) +
                  frame(b'B' * 50, identity=4, stamp=116) +
                  frame(b'restarted', identity=4, stamp=120) +
                  frame(b'C' * 50, identity=5, stamp=124) +
                  frame(b'changed', identity=5, sequence=1, severity=4, stamp=128) +
                  frame(b'\xff', identity=0, stamp=132) +
                  frame(b'D' * 50, identity=6, stamp=104))
        rows, text, report = convert(source, 'mavproxy-link')
        self.assertEqual(rows[0]['assembly_status'], 'INCOMPLETE_EOF_NO_TERMINATOR')
        self.assertIn('INCOMPLETE_NONCONTIGUOUS_SEQUENCE', [r['assembly_status'] for r in rows])
        self.assertIn('INCOMPLETE_RESTART_BEFORE_TERMINATOR', [r['assembly_status'] for r in rows])
        self.assertIn('INCOMPLETE_SEVERITY_CHANGED', [r['assembly_status'] for r in rows])
        self.assertEqual(rows[-1]['text_hex'], 'hex:ff')
        self.assertEqual(rows[-1]['text_encoding'], 'INVALID_UTF8_BYTES_RETAINED')
        self.assertEqual(sum(int(r['chunk_count']) for r in rows), report['selected_chunks'])
        self.assertEqual(self.common(text)['main_rows'], 8)
        # A separately constructed ordinary selected recording is not restricted to tiny examples.
        large = b''.join(frame(f'message {i}'.encode(), stamp=1000+i) for i in range(2000))
        large_rows, large_csv, large_report = convert(large)
        self.assertEqual((len(large_rows), large_report['complete_messages']), (2000, 2000))
        self.assertEqual(self.common(large_csv)['main_rows'], 2000)

    def test_link_boundaries_and_text_not_numeric(self):
        source = (frame(b'A' * 50, identity=9, stamp=101) +
                  frame(b'B' * 50, identity=9, stamp=102) +
                  frame(b'end A', identity=9, sequence=1, stamp=105) +
                  frame(b'end B', identity=9, sequence=1, stamp=106) +
                  frame(b'001', stamp=108) + frame(b'NaN', stamp=108))
        rows, text, _ = convert(source, 'mavproxy-link')
        self.assertEqual([r['link_tag'] for r in rows[:2]], ['1', '2'])
        self.assertEqual(rows[0]['text_utf8'], 'utf8:' + 'A' * 50 + 'end A')
        self.assertEqual(rows[1]['text_utf8'], 'utf8:' + 'B' * 50 + 'end B')
        common = self.common(text)
        self.assertEqual([o['fields']['text_utf8'] for o in common['observations'][-2:]],
                         ['utf8:001', 'utf8:NaN'])

    def test_refusals_layout_and_fresh_cli(self):
        good = frame(b'hello', stamp=103)
        self.assertEqual(convert(good, 'mavproxy-link')[0][0]['capture_time_us'], '100')
        self.assertEqual(convert(good, 'mavproxy-link')[0][0]['link_tag'], '3')
        for data in (good[:-1], good[:-2] + b'\x00\x00', b'wrong format', b''):
            with self.assertRaises(ValueError): convert(data)
        with self.assertRaises(ValueError): convert(good, 'guess')
        for bound, data in (('MAX_OUTPUT', good), ('MAX_CHUNKS', good),
                            ('MAX_ACTIVE', frame(b'A' * 50, identity=1))):
            with patch.object(status, bound, 0), self.assertRaises(ValueError): convert(data)
        # A valid MAVLink1-selected frame is outside this contract, never silently a complete v2 message.
        payload = b'\x06' + b'one'.ljust(50, b'\0')
        header = bytes([51, 1, 1, 1, 253])
        v1 = b'\0'*8 + b'\xfe' + header + payload + crc_x25(b'\x53', crc_x25(header+payload)).to_bytes(2, 'little')
        with self.assertRaisesRegex(ValueError, 'MAVLINK1'): convert(v1)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); path = base/'input.tlog'; path.write_bytes(good)
            args = [os.sys.executable, str(ROOT/'scripts/convert_mavlink_statustext.py'),
                    str(path), str(base/'out'), '--timestamp-layout', 'unsigned-us']
            subprocess.run(args, capture_output=True, check=True)
            self.assertEqual((base/'out/source.tlog').read_bytes(), good)
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)


if __name__ == '__main__':
    unittest.main()
