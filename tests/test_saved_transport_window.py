import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_rmf_websocket_recorded as ws
from scripts import convert_mqtt_publish_packets as mqtt
from scripts import saved_binary_snapshot as snapshot
from tests import test_rmf_websocket_recorded as w
from tests import test_mqtt_publish_packets as m


class TransportWindowTests(unittest.TestCase):
    def common(self, directory, text, field, value):
        target = Path(directory)/'observations.csv'
        target.write_text(text)
        result = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
            str(w.ROOT/'profiles/declared/electrical-schema-reuse/json-profile.toml'),
            str(target), '--allow-equal-time'], check=True, capture_output=True).stdout)
        self.assertEqual(result['main_rows'], 1)
        self.assertEqual(result['observations'][0]['fields'][field], value)

    def test_websocket_large_original_and_independent_input(self):
        opts = dict(selected_messages=[300], capture_time_us=50,
                    direction='server-to-client', extensions='none')
        prefix = w.frame(b'x'*60000)*300
        event = w.event('warehouse', .25)
        data = prefix+w.frame(event[:20], final=False)+w.frame(b'p', 9)+w.frame(event[20:], 0)
        self.assertGreater(len(data), ws.MAX_INPUT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input'; path.write_bytes(data)
            output, report = ws.convert_path(path, **opts)
            self.assertEqual(report['source_sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual((report['source_frames'], report['text_messages'], report['control_frames']), (303,301,1))
            self.assertEqual(report['omitted_frame_details'], 300)
            self.assertEqual(report['messages'][0]['frame_indices'], [300,302])
            self.assertEqual(report['frames'][0]['byte_offset'], len(prefix))
            row = next(csv.DictReader(io.StringIO(output)))
            record = json.loads(bytes.fromhex(row['source_record_hex'][4:]))
            self.assertEqual(record['saved_message_index'], 300)
            self.assertEqual(record['saved_source_sha256'], report['source_sha256'])
            self.common(directory, output, 'battery_remaining_fraction', .25)
            with self.assertRaises(ValueError): ws.convert(data, **opts)
            other = w.frame(w.event('second', .75)); path.write_bytes(other)
            output, report = ws.convert_path(path, **dict(opts, selected_messages=[0]))
            self.common(directory, output, 'battery_remaining_fraction', .75)
            for bad in (other[:-1], w.frame(b'part', final=False), w.frame(b'part',0)):
                path.write_bytes(bad)
                with self.assertRaises(ValueError): ws.convert_path(path, **dict(opts, selected_messages=[0]))

    def test_mqtt_large_original_and_independent_input(self):
        prefix = m.packet('x'*60000, topic='unselected')*300
        selected = m.packet(m.vda('2.1.0'))
        data = prefix+selected
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input'; path.write_bytes(data)
            opts = dict(m.options(), selected_packets=[300])
            output, report = mqtt.convert_path(path, **opts)
            self.assertGreater(len(data), mqtt.existing.LIMIT)
            self.assertEqual((report['source_packets'], report['unselected_packets']), (301,300))
            self.assertEqual(report['packets'][0]['index'],300)
            self.assertEqual(report['packets'][0]['byte_offset'],len(prefix))
            self.assertEqual(report['source_sha256'],hashlib.sha256(data).hexdigest())
            row = next(csv.DictReader(io.StringIO(output)))
            record = json.loads(bytes.fromhex(row['source_record_hex'][4:]))
            self.assertEqual(record['raw_packet_index'],300)
            self.assertEqual(record['saved_source_sha256'],report['source_sha256'])
            self.common(directory, output, 'battery_voltage_v', float(row['battery_voltage_v']))
            with self.assertRaises(ValueError): mqtt.convert(data, **opts)
            path.write_bytes(m.packet(m.ros(),qos=2))
            output, _ = mqtt.convert_path(path, **dict(m.options(),payload_format='ros-battery-json'))
            self.common(directory, output, 'battery_voltage_v',float(next(csv.DictReader(io.StringIO(output)))['battery_voltage_v']))
            for bad in (selected[:-1],b'\x30\x80\x00',b'\x30\xff\xff\xff\xff\x01'):
                path.write_bytes(bad)
                with self.assertRaises(ValueError): mqtt.convert_path(path, **m.options())

    def test_snapshot_selection_and_changed_file_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input'; path.write_bytes(m.packet(m.vda('2.1.0')))
            for selection in ([True],[],[1],[0,0]):
                with self.assertRaises(ValueError):
                    mqtt.convert_path(path, **dict(m.options(),selected_packets=selection))
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):mqtt.convert_path(link,**m.options())
            with self.assertRaises(ValueError):mqtt.convert_path(Path(directory),**m.options())
            original=mqtt.convert
            def changed(data,**options):
                result=original(data,**options)
                path.write_bytes(b'x'*path.stat().st_size)
                return result
            with self.assertRaisesRegex(ValueError,'snapshot identity changed'):
                snapshot.convert_snapshot(path,changed,**m.options())

    def test_copy_time_change_refuses_before_mapping_or_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input'; path.write_bytes(m.packet(m.vda('2.1.0')))
            original=snapshot.copy_source
            def changed(source, target):
                result=original(source,target)
                path.write_bytes(b'')
                return result
            with patch.object(snapshot,'copy_source',side_effect=changed), \
                    patch.object(snapshot.mmap,'mmap',side_effect=AssertionError('must refuse before map')), \
                    self.assertRaisesRegex(ValueError,'changed during copy'):
                mqtt.convert_path(path,**m.options())
