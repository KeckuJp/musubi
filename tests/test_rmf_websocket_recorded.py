import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_rmf_websocket_recorded as bridge

ROOT = Path(__file__).resolve().parents[1]


def frame(payload, opcode=1, final=True):
    length = len(payload)
    header = bytes([(128 if final else 0) | opcode])
    return header + (bytes([length]) if length < 126 else b'\x7e' + length.to_bytes(2, 'big')) + payload


def event(name, battery):
    return ('42' + json.dumps(['/fleets/' + name + '/state', dict(name=name,
        robots={'robot': dict(battery=battery, status='working', future=[7])})],
        ensure_ascii=False)).encode()


class WebsocketTests(unittest.TestCase):
    def test_fragmented_common_and_separate_input_reuse(self):
        data = event('倉庫', .25)
        split = data.index('倉'.encode()) + 1
        source = frame(b'2') + frame(data[:split], final=False) + frame(b'ping', 9) + frame(data[split:], 0)
        output, report = bridge.convert(source, selected_messages=[1], capture_time_us=50,
            direction='server-to-client', extensions='none')
        self.assertEqual(report['source_frames'], 4)
        self.assertEqual(report['text_messages'], 2)
        self.assertEqual(report['unselected_text_messages'], 1)
        self.assertEqual(report['control_frames'], 1)
        self.assertEqual(report['messages'][1]['frame_indices'], [1, 3])
        self.assertEqual(report['messages'][0]['disposition'], 'NOT_SELECTED')
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(float(rows[0]['battery_remaining_fraction']), .25)
        self.assertIn('future', json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:]))['packet'])
        other, other_report = bridge.convert(frame(event('outside', .75)), selected_messages=[0],
            capture_time_us=51, direction='server-to-client', extensions='none')
        self.assertEqual(other_report['selected_text_messages'], 1)
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        with tempfile.TemporaryDirectory() as directory:
            for index, (text, value) in enumerate(((output, .25), (other, .75))):
                path = Path(directory) / f'{index}.csv'
                path.write_text(text)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                    str(path), '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 1)
                self.assertEqual(common['observations'][0]['fields']['battery_remaining_fraction'], value)
                self.assertEqual(common['platform_domain'], 'Unknown')

    def test_framing_selection_and_old_resolver_parity(self):
        good = frame(event('fleet', .5))
        options = dict(selected_messages=[0], capture_time_us=1, direction='server-to-client', extensions='none')
        output, _ = bridge.convert(good, **options)
        from scripts import convert_ardupilot_battery_csv as existing
        envelope = bridge.envelope(event('fleet', .5).decode(), 1, None, None)
        self.assertEqual(output, existing.convert(json.dumps(envelope), existing.RMF_SOCKET_FORMAT))
        bad = [b'', good[:-1], b'\x81\x7e\x00\x01a', b'\xc1\x01a', b'\x81\x81aaaaa',
            frame(b'x', 2), frame(b'x', 0), frame(b'x', final=False), frame(b'\xff'),
            frame(b'x', final=False) + good, frame(b'x', 9, False), frame(b'x' * 126, 9),
            frame(b'', 8), b'\x81\x7f' + (1 << 63).to_bytes(8, 'big')]
        for data in bad:
            with self.subTest(data=data[:12]), self.assertRaises(ValueError): bridge.convert(data, **options)
        for change in (dict(selected_messages=[]), dict(selected_messages=[True]),
                dict(selected_messages=[0, 0]), dict(selected_messages=[1]),
                dict(direction='client-to-server'), dict(extensions='permessage-deflate'),
                dict(capture_time_us=True), dict(location_unit='mm', yaw_unit='rad')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.convert(good, **dict(options, **change))
        with self.assertRaises(ValueError):
            bridge.convert(frame(b'', 9) * (bridge.MAX_FRAMES + 1), **options)
        with tempfile.TemporaryDirectory() as directory:
            source, target, report = [Path(directory) / name for name in ('in.bin', 'out.csv', 'report.json')]
            source.write_bytes(good)
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_rmf_websocket_recorded.py'),
                str(source), str(target), '--report', str(report), '--message-index', '0',
                '--capture-time-us', '1', '--direction', 'server-to-client', '--extensions', 'none'],
                check=True, capture_output=True)
            self.assertEqual(target.read_text(), output)
            self.assertEqual(json.loads(report.read_text())['selected_text_messages'], 1)
