"""Authored native LSF interval checks, not a physical marine recording."""
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import convert_imc_sensors as c
from tests.test_imc_sensors import ROOT, frame


class ImcRecordWindowTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_neptus_long_native_windows_reuse_only_qualified_meanings(self):
        outside = frame(65000, bytes(60000))[:-2] + b'\0\0'
        packets = [frame(mid, value) for mid, value in
                   ((263, 20), (264, 1013.25), (265, 7.5), (267, -1),
                    (268, 1025), (270, 35), (251, 24), (252, -2), (364, 48))]
        packets += [frame(mid, struct.pack('<dddd', 9., 1., -2., 3.)) for mid in (256, 257)]
        packets += [frame(mid, struct.pack('<Bddd', 5, 0., -2., 3.)) for mid in (259, 260)]
        raw = outside * 280 + b''.join(packets)
        self.assertGreater(len(raw), c.LIMIT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Data.lsf'; path.write_bytes(raw)
            fields, unsupported = [], []
            for start, stop in ((280, 287), (287, 293)):
                output, report = c.convert_path(path, start_record=start, stop_record=stop, schema=c.NEPTUS_SCHEMA)
                selected = report['record_selection']
                self.assertEqual(report['selected_schema'], c.NEPTUS_SCHEMA)
                self.assertEqual(selected['source_records'], 293)
                self.assertEqual(selected['selected_records'], stop - start)
                self.assertEqual(selected['selected_records'] + selected['excluded_records'], 293)
                self.assertEqual(report['decoded_frames'] + len(report['unsupported_frames']), stop - start)
                self.assertEqual(selected['source_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(selected['outside_selection'], 'PAYLOAD_AND_CRC_NOT_DECODED_OR_VALIDATED')
                unsupported.extend(report['unsupported_frames'])
                target = Path(directory) / f'{start}.csv'; target.write_text(output)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/imc-sensors/profile.toml'),
                    str(target), '--allow-equal-time'], check=True, capture_output=True).stdout)
                for observation in common['observations']:
                    self.assertEqual(observation['clock_basis'], 'Unknown')
                    self.assertIsNone(observation['anchor_unix_us'])
                    fields.append(observation['fields'])
                self.assertEqual(common['platform_domain'], 'Unknown')
            expected_indexes = [i for i in range(280, 293) if i not in (285, 288, 292)]
            self.assertEqual([f['source_record_index'] for f in fields], expected_indexes)
            self.assertEqual({f['message_id'] for f in fields}, c.NEPTUS_SENSORS)
            for item, index in zip(fields, expected_indexes):
                self.assertEqual(item['source_byte_offset'], len(outside) * 280 + sum(map(len, packets[:index - 280])))
                self.assertEqual(item['source_frame_hex'], 'hex:' + packets[index - 280].hex())
                self.assertEqual(item['source_file_sha256'], 'sha256:' + hashlib.sha256(raw).hexdigest())
                self.assertIsNone(item['salinity_psu'])
                self.assertIsNone(item['electrical_power_w'])
            self.assertEqual(fields[0]['temperature_k'], 293.15)
            self.assertEqual(fields[1]['external_pressure_pa'], 101325)
            self.assertEqual(fields[2]['depth_m'], 7.5)
            self.assertIsNone(fields[3]['sound_speed_m_s'])
            self.assertEqual(fields[-1]['reported_ground_velocity_x_m_s'], 0)
            self.assertIsNone(fields[-1]['reported_ground_velocity_y_m_s'])
            self.assertEqual([u['message_id'] for u in unsupported], [270, 364, 260])
            for item, index in zip(unsupported, (285, 288, 292)):
                self.assertEqual(item['source_record_index'], index)
                self.assertEqual(item['offset'], len(outside) * 280 + sum(map(len, packets[:index - 280])))
                self.assertEqual(item['frame_hex'], packets[index - 280].hex())
            cli = Path(directory) / 'cli'
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_imc_sensors.py'), str(path),
                str(cli), '--source-format', c.NEPTUS_SCHEMA, '--start-record', '287', '--stop-record', '293'],
                check=True, capture_output=True)
            self.assertEqual((cli / 'observations.csv').read_text(), output)

    def test_neptus_window_default_and_refusal_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'Data.lsf'
            raw = frame(263, 12, '>') + frame(270, 35, '>')
            path.write_bytes(raw)
            current = c.convert_path(path, start_record=0, stop_record=2)
            self.assertEqual(current, c.convert_path(path, start_record=0, stop_record=2, schema=None))
            output, report = c.convert_path(path, start_record=0, stop_record=2, schema=c.NEPTUS_SCHEMA)
            expected, expected_report = c.convert(raw, schema=c.NEPTUS_SCHEMA)
            row = next(csv.DictReader(io.StringIO(output)))
            for key, value in next(csv.DictReader(io.StringIO(expected))).items():
                self.assertEqual(row[key], value)
            self.assertEqual(report['decoded_frames'], expected_report['decoded_frames'])
            for arguments in ({'schema': 'neptus-other'},
                              {'schema': 'neptus-other', 'water_velocity': True},
                              {'schema': c.NEPTUS_SCHEMA, 'water_velocity': 1}):
                with patch.object(Path, 'open', side_effect=AssertionError('must reject before IO')):
                    with self.assertRaises(ValueError):
                        c.convert_path(path, start_record=0, stop_record=1, **arguments)
            for raw in (frame(270, 35), gzip.compress(frame(263, 20)), frame(263, 20)[:-1]):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    c.convert_path(path, start_record=0, stop_record=1, schema=c.NEPTUS_SCHEMA)

    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_large_native_adjacent_intervals_preserve_common_and_source(self):
        outside = frame(65000, bytes(60000))[:-2] + b'\0\0'
        packets = [frame(mid, value) for mid, value in
                   ((263, 20), (264, 1013.25), (265, 7.5), (267, -1),
                    (268, 1025), (270, 35), (251, 24), (252, -2), (364, 48))]
        packets += [frame(mid, struct.pack('<dddd', 9., 1., -2., 3.)) for mid in (256, 257)]
        packets += [frame(mid, struct.pack('<Bddd', mask, 0., -2., 3.))
                    for mid, mask in ((259, 7), (260, 5))]
        packets.insert(6, frame(65001, b'unknown selected'))
        raw = outside * 280 + b''.join(packets)
        self.assertGreater(len(raw), c.LIMIT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'saved.lsf'; path.write_bytes(raw)
            fields, common_times, unsupported = [], [], []
            for start, stop in ((280, 287), (287, 294)):
                output, report = c.convert_path(path, start_record=start, stop_record=stop, water_velocity=True)
                selection = report['record_selection']
                self.assertEqual((selection['source_records'], selection['selected_records'],
                                  selection['excluded_records']), (294, 7, 287))
                self.assertEqual(selection['source_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(selection['outside_selection'], 'PAYLOAD_AND_CRC_NOT_DECODED_OR_VALIDATED')
                selected = b''.join(packets[start - 280:stop - 280])
                self.assertEqual(selection['selected_stream_sha256'], hashlib.sha256(selected).hexdigest())
                unsupported.extend(report['unsupported_frames'])
                target = Path(directory) / f'{start}.csv'; target.write_text(output)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/imc-sensors/profile.toml'),
                    str(target), '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['platform_domain'], 'Unknown')
                for observation in common['observations']:
                    self.assertEqual(observation['clock_basis'], 'Unknown')
                    self.assertIsNone(observation['anchor_unix_us'])
                    fields.append(observation['fields'])
                common_times.extend(row['record_time_us'] for row in csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(fields), 13)
            expected_indexes = [i for i in range(280, 294) if i != 286]
            self.assertEqual([f['source_record_index'] for f in fields], expected_indexes)
            for item, index in zip(fields, expected_indexes):
                self.assertEqual(item['source_byte_offset'], len(outside) * 280 + sum(map(len, packets[:index - 280])))
                self.assertEqual(item['source_file_sha256'], 'sha256:' + hashlib.sha256(raw).hexdigest())
                self.assertEqual(item['source_frame_hex'], 'hex:' + packets[index - 280].hex())
            self.assertEqual(len(set(common_times)), 1)
            self.assertEqual(fields[0]['temperature_k'], 293.15)
            self.assertIsNone(fields[3]['sound_speed_m_s'])
            self.assertEqual(fields[-1]['reported_water_velocity_x_m_s'], 0)
            self.assertIsNone(fields[-1]['reported_water_velocity_y_m_s'])
            self.assertEqual((unsupported[0]['source_record_index'], unsupported[0]['offset']),
                             (286, len(outside) * 280 + sum(map(len, packets[:6]))))
            self.assertEqual(unsupported[0]['selected_stream_offset'], sum(map(len, packets[:6])))
            self.assertEqual(unsupported[0]['frame_hex'], packets[6].hex())
            cli_output = Path(directory) / 'cli'
            run = subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_imc_sensors.py'),
                str(path), str(cli_output), '--start-record', '287', '--stop-record', '294',
                '--water-velocity'], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((cli_output / 'observations.csv').read_text(), output)

    def test_interval_framing_clock_and_selected_semantics_refuse(self):
        good = frame(263, 20)
        invalid_value = frame(263, float('nan'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'saved.lsf'
            path.write_bytes(invalid_value + good)
            c.convert_path(path, start_record=1, stop_record=2)
            with self.assertRaises(ValueError): c.convert_path(path, start_record=0, stop_record=1)
            for start, stop in ((True, 1), (0, False), (None, 1), (1, 1), (-1, 1), (0, 3)):
                with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                    c.convert_path(path, start_record=start, stop_record=stop)
            for raw in (gzip.compress(good), good + b'broken',
                        frame(65000, b'x', timestamp=1700000001) + good,
                        frame(65000, b'x', timestamp=float('nan')) + good,
                        frame(65000, b'x'), good[:-1] + bytes([good[-1] ^ 1])):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:8]), self.assertRaises(ValueError):
                    c.convert_path(path, start_record=0, stop_record=1)
            path.write_bytes(good)
            with self.assertRaises(ValueError): c.convert_path(path, start_record=0, stop_record=1, water_velocity=1)

    def test_resource_bounds_and_cli_do_not_read_whole_input(self):
        good = frame(263, 20)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); path = base / 'saved.lsf'; path.write_bytes(good * 2)
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file read')):
                c.convert_path(path, start_record=0, stop_record=1)
            original_stat = path.stat()
            changed = SimpleNamespace(**{key: getattr(original_stat, key) for key in
                ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')})
            changed.st_mtime_ns += 1
            with patch.object(c.os, 'fstat', side_effect=[original_stat, changed]), self.assertRaisesRegex(ValueError, 'source changed'):
                c.convert_path(path, start_record=0, stop_record=1)
            for name, limit in (('IMC_WINDOW_INPUT_LIMIT', 1), ('IMC_WINDOW_RECORD_LIMIT', 1),
                                ('LIMIT', 1), ('OUTPUT_LIMIT', 1), ('IMC_WINDOW_REPORT_LIMIT', 1)):
                with self.subTest(bound=name), patch.object(c, name, limit), self.assertRaisesRegex(ValueError, 'bound'):
                    c.convert_path(path, start_record=0, stop_record=1)
            cli = [os.sys.executable, str(ROOT / 'scripts/convert_imc_sensors.py'), str(path)]
            args = ['--start-record', '0', '--stop-record', '1']
            result = subprocess.run(cli + [str(base / 'out')] + args, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = (base / 'out/observations.csv').read_bytes()
            self.assertEqual(subprocess.run(cli + [str(base / 'out')] + args, capture_output=True).returncode, 2)
            self.assertEqual((base / 'out/observations.csv').read_bytes(), before)
            for flags in (['--start-record', '0'], args + ['--source-format', 'pcap-udp-imc'],
                          args + ['--source-format', 'moos-typed-alog', '--water-velocity']):
                self.assertEqual(subprocess.run(cli + [str(base / 'bad')] + flags, capture_output=True).returncode, 2)
                self.assertFalse((base / 'bad').exists())
