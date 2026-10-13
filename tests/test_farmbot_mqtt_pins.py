"""Authored saved envelopes; no broker, physical GPIO or firmware execution."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_mqtt_recorded import convert, convert_path
from tests.test_mqtt_recorded import envelope

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'profiles/declared/farmbot-saved-pins/mqtt-profile.toml'


def payload(x, pins):
    return dict(location_data=dict(position=dict(x=x)),
                informational_settings=dict(busy=False, soc_temp=20, uptime=123),
                pins=pins, future='保持')


class FarmBotMqttPinsTests(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'this module requires the post-build common reader')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'out.csv'
            path.write_text(output)
            run = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                                 check=True, capture_output=True, text=True)
        return json.loads(run.stdout)

    def test_state_pin_meaning_same_envelope_and_default_parity(self):
        records = [envelope(payload(100, {'1': dict(mode=0, value=1), '2': dict(mode=-1, value=0),
                                         '3': dict(mode=1, value=20)})),
                   envelope(payload(-250, {})), envelope(payload(0, None))]
        missing = payload(1, {}); del missing['pins']
        records.append(envelope(missing))
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        text = '\n'.join(lines)
        default = convert(text, 'example/state', 'farmbot-state')
        self.assertEqual(default, convert(text, 'example/state', 'farmbot-state', farmbot_pins=False))
        output = convert(text, 'example/state', 'farmbot-state', farmbot_pins=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 7)
        self.assertEqual([r['farm_pin_status'] for r in rows],
                         ['REPORTED', 'REPORTED_DIGITAL', 'MODE_UNKNOWN', 'ANALOG_NOT_CONVERTED',
                          'EMPTY', 'NULL', 'NOT_REPORTED'])
        states = [r for r in rows if r['farm_pin_record_kind'] == 'SNAPSHOT']
        for old, row, line in zip(csv.DictReader(io.StringIO(default)), states, lines):
            for key, value in old.items():
                self.assertEqual(row[key], value)
            self.assertEqual(row['mqtt_envelope_sha256'], 'sha256:' + hashlib.sha256(line.encode()).hexdigest())
        self.assertEqual([r['mqtt_source_record_index'] for r in rows], ['0'] * 4 + ['1','2','3'])
        self.assertEqual(sum(bool(r['source_record_hex']) for r in rows), 4)
        common = self.common(output)
        self.assertEqual(len(common['observations']), 7)
        self.assertEqual(common['observations'][0]['fields']['farm_position_x_m'], .1)
        self.assertEqual(common['observations'][1]['fields']['digital_value_reported'], 1)
        self.assertIsNone(common['observations'][2]['fields']['digital_value_reported'])
        self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))

    def test_window_json_value_other_input_global_binding_and_cli(self):
        record = envelope(payload(500, {'255': dict(mode=0, value=0.0), '2': dict(mode=0, value=None)}))
        record['payload'] = json.loads(record['payload'])
        record['payloadlen'] = 999  # %J reported bytes, deliberately not claimed verified.
        line = json.dumps(record)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.jsonl'
            source.write_text('x' * 40000 + '\n' + line + '\n')
            output, report = convert_path(source, 'example/state', 'farmbot-state', start_record=1,
                                          stop_record=2, envelope_format='J', farmbot_pins=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['farm_source_record_index'] for r in rows], ['1'] * 3)
            self.assertEqual([r['mqtt_source_record_index'] for r in rows], ['1'] * 3)
            self.assertEqual([r['mqtt_source_record_byte_offset'] for r in rows], ['40001'] * 3)
            self.assertEqual(report['excluded_records_not_decoded'], 1)
            self.assertEqual(report['output_rows'], 3)
            common = self.common(output)
            self.assertEqual(common['observations'][1]['fields']['digital_value_reported'], 0)
            self.assertEqual(common['observations'][0]['fields']['farm_position_x_m'], .5)
            target, log = Path(directory) / 'cli.csv', Path(directory) / 'report.json'
            run = subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_mqtt_recorded.py'),
                str(source), str(target), '--topic', 'example/state', '--payload-format', 'farmbot-state',
                '--farmbot-pins', '--envelope-format', 'J', '--start-record', '1', '--stop-record', '2',
                '--report', str(log)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(target.read_text(), output)

    def test_wrong_selector_envelope_pin_and_clock_refused(self):
        record = envelope(payload(1, {'1': dict(mode=0, value=1)}))
        for flag, selector in ((1, 'farmbot-state'), (True, 'vda-state-2.1')):
            with self.assertRaises(ValueError):
                convert(json.dumps(record), 'example/state', selector, farmbot_pins=flag)
        for change in (dict(topic='wrong'), dict(payloadlen=0), dict(qos=True), dict(tst='bad')):
            with self.assertRaises(ValueError):
                convert(json.dumps(record | change), 'example/state', 'farmbot-state', farmbot_pins=True)
        bad = envelope(payload(1, {'1': dict(mode=0, value=True)}))
        with self.assertRaises(ValueError):
            convert(json.dumps(bad), 'example/state', 'farmbot-state', farmbot_pins=True)
        earlier = record | dict(tst='2025-01-01T01:00:01.123456+0100')
        with self.assertRaises(ValueError):
            convert(json.dumps(record)+'\n'+json.dumps(earlier), 'example/state', 'farmbot-state', farmbot_pins=True)
