import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_mqtt_publish_packets as bridge
from tests.test_electrical_schema_reuse import vda, ros

ROOT = Path(__file__).resolve().parents[1]


def packet(payload, topic='example/state', qos=1, dup=0, mid=42):
    topic = topic.encode()
    body = len(topic).to_bytes(2, 'big') + topic
    if qos:
        body += mid.to_bytes(2, 'big')
    body += json.dumps(payload).encode()
    remaining, length = bytearray(), len(body)
    while True:
        byte, length = length % 128, length // 128
        remaining.append(byte | (128 if length else 0))
        if not length: break
    return bytes([0x31 | qos << 1 | dup << 3]) + remaining + body


def options(**kwargs):
    return dict(selected_packets=[0], topic='example/state', payload_format='vda-state-2.1',
        capture_timestamp='2026-01-01T00:00:01.123456+0000', protocol='3.1.1', **kwargs)


class PublishTests(unittest.TestCase):
    def test_vda20_connection_native_and_window(self):
        payload=dict(headerId=7,timestamp='2026-01-01T00:00:00Z',version='2.0.0',
                     manufacturer='maker',serialNumber='unit',connectionState='CONNECTIONBROKEN')
        opts=dict(options(),payload_format='vda-connection-2.0')
        output,_=bridge.convert(packet(payload),**opts)
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'input'
            second=dict(payload,headerId=8,connectionState='DISCONNECTED')
            source.write_bytes(packet(payload)+packet(second))
            other,report=bridge.convert_path(source,**dict(opts,selected_packets=[1]))
            for text,state in ((output,'CONNECTIONBROKEN'),(other,'DISCONNECTED')):
                row=next(csv.DictReader(io.StringIO(text)))
                self.assertEqual(row['vda_connection_state_reported'],state)
                if state=='DISCONNECTED':
                    self.assertEqual(row['vda_connection_state_status'],'UNSUPPORTED_STATE_VALUE_RETAINED')
                path=Path(directory)/'common.csv';path.write_text(text)
                common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT/'profiles/declared/electrical-schema-reuse/vda-connection-profile.toml'),
                    str(path),'--allow-equal-time'],check=True,capture_output=True).stdout)
                self.assertEqual(common['main_rows'],1)
            with self.assertRaises(ValueError):bridge.convert(packet(dict(payload,version='2.1.0')),**opts)
            with self.assertRaises(ValueError):bridge.convert(packet(payload),**opts,vda_actions=True)

    def test_vda_details_native_window_and_default_parity(self):
        for version in ('2.0.0', '2.1.0', '3.0.0'):
            payload=vda(version)
            payload['actionStates']=[dict(actionId='a',actionStatus='RUNNING',
                actionType='move',actionDescription='declared',resultDescription='reported')]
            if version=='2.0.0':
                payload['actionStates'].append(dict(actionId='other',actionStatus='RETRIABLE'))
            payload['errors']=[dict(errorType='reported',errorLevel='WARNING',
                errorReferences=[dict(referenceKey='nodeId',referenceValue=version)])]
            payload['errors'][0]['errorHint']='foreign-or-declared-hint'
            data=packet(payload)
            opts=dict(options(),payload_format='vda-state-'+version[:3])
            flags=dict(vda_actions=True,vda_errors=True)
            old,_=bridge.convert(data,**opts)
            self.assertEqual(old,bridge.convert(data,**opts,vda_actions=False,vda_errors=False)[0])
            output,report=bridge.convert(data,**opts,**flags)
            rows=list(csv.DictReader(io.StringIO(output)))
            count=5 if version=='2.0.0' else 4
            self.assertEqual(len(rows),count)
            self.assertEqual(len({r['mqtt_envelope_sha256'] for r in rows}),1)
            if version=='2.0.0':
                self.assertTrue(all(not row['vda_error_hint_hex'] for row in rows))
                self.assertEqual(rows[1]['vda_action_description_hex'],'hex:'+b'declared'.hex())
                self.assertEqual(rows[1]['vda_action_result_hex'],'hex:'+b'reported'.hex())
                self.assertEqual(rows[2]['vda_action_status_reported'],'UNKNOWN')
                self.assertEqual(rows[2]['vda_action_status_raw_hex'],'hex:'+b'RETRIABLE'.hex())
            with tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'input';source.write_bytes(packet(ros(),topic='excluded')+data)
                selected,window=bridge.convert_path(source,**dict(opts,selected_packets=[1]),
                    **flags)
                group=list(csv.DictReader(io.StringIO(selected)))
                raw=json.loads(bytes.fromhex(group[0]['source_record_hex'][4:]))
                self.assertEqual(raw['raw_packet_index'],1)
                self.assertEqual(raw['saved_source_sha256'],window['source_sha256'])
                self.assertEqual(raw['raw_byte_offset'],window['packets'][0]['byte_offset'])
                for text in (output,selected):
                    path=Path(directory)/'common.csv';path.write_text(text)
                    common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(ROOT/'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                        str(path),'--allow-equal-time'],check=True,capture_output=True).stdout)
                    self.assertEqual(common['main_rows'],count)
                    self.assertEqual(common['observations'][-1]['fields']['vda_error_reference_value_hex'],
                                     'hex:'+version.encode().hex())
            for change in (dict(vda_actions='false'),dict(vda_errors=1),
                           dict(payload_format='vda-state-1.1'),dict(payload_format='ros-battery-json')):
                with self.assertRaises(ValueError):
                    bridge.convert(data,**{**opts,'vda_actions':True,'vda_errors':True,**change})

    def test_selected_repeat_and_independent_schema_common(self):
        first = packet(vda('2.1.0'))
        source = packet(vda('2.1.0'), topic='other/state') + first + packet(vda('2.1.0'), dup=1)
        opts = dict(options(), selected_packets=[1, 2])
        output, report = bridge.convert(source, **opts)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(report['source_packets'], 3)
        self.assertEqual(report['unselected_packets'], 1)
        self.assertEqual([x['dup'] for x in report['packets']], [0, 0, 1])
        envelope = json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:]))
        self.assertEqual(envelope['source_basis'], 'RECONSTRUCTED_SAVED_MQTT311_PUBLISH_NOT_MOSQUITTO_CAPTURE')
        self.assertEqual(envelope['raw_byte_offset'], report['packets'][1]['byte_offset'])
        other, _ = bridge.convert(packet(ros(), qos=2), **dict(options(), payload_format='ros-battery-json'))
        self.assertTrue(os.environ.get('MUSUBI_TELEMETRY_READER'))
        with tempfile.TemporaryDirectory() as directory:
            for index, (text, count) in enumerate(((output, 2), (other, 1))):
                path = Path(directory) / f'{index}.csv'
                path.write_text(text)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                    str(path), '--allow-equal-time'], capture_output=True, check=True).stdout)
                self.assertEqual(common['main_rows'], count)
                self.assertIn('battery_voltage_v', common['observations'][0]['fields'])
                self.assertEqual(common['platform_domain'], 'Unknown')

    def test_refusals_and_cli(self):
        good = packet(vda('2.1.0'))
        for data in (b'', good[:-1], b'\x30\x80\x00', b'\x30\xff\xff\xff\xff\x01',
                bytes([0x37]) + good[1:], packet(vda('2.1.0'), qos=0, dup=1),
                packet(vda('2.1.0'), mid=0), packet(vda('2.1.0'), topic='a/+'),
                packet(vda('2.1.0'), topic='a/\x00'), b'\xc0\x00', b'\x30\x03\x00\x01\xff'):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError): bridge.convert(data, **options())
        for change in (dict(selected_packets=[]), dict(selected_packets=[True]),
                dict(selected_packets=[0, 0]), dict(selected_packets=[1]), dict(topic='wrong'),
                dict(protocol='5'), dict(capture_timestamp='no-time')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.convert(good, **dict(options(), **change))
        expected, _ = bridge.convert(good, **options())
        with tempfile.TemporaryDirectory() as directory:
            source, out, report = [Path(directory) / name for name in ('in.bin', 'out.csv', 'report.json')]
            source.write_bytes(good)
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_mqtt_publish_packets.py'),
                str(source), str(out), '--report', str(report), '--packet-index', '0',
                '--topic', 'example/state', '--payload-format', 'vda-state-2.1', '--protocol', '3.1.1',
                '--capture-timestamp', options()['capture_timestamp']], check=True, capture_output=True)
            self.assertEqual(out.read_text(), expected)
