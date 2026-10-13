"""Authored gzip streams through existing IMC meanings; no real vessel recording."""
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_imc_gzip_window as m
from scripts import convert_imc_sensors as imc
from tests.test_imc_sensors import ROOT, frame


def sha(value):
    return hashlib.sha256(value).hexdigest()


class ImcGzipWindowTests(unittest.TestCase):
    def test_estimated_state_flag_reuses_native_window(self):
        from scripts import convert_imc_gzip_window as wrapper
        from scripts.convert_imc_sensors import NEPTUS_SCHEMA
        from tests.test_imc_estimated_state import packet, PROFILE
        from tests.saved_can_window_helpers import common
        body = packet([0., 0., 10, 1, 2, -3, .1, .2, .3, 1, 2, 3, 4, 5, 6, .1, .2, .3, 0, -1])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); path = folder / "input.lsf.gz"
            path.write_bytes(gzip.compress(body, mtime=0))
            for schema in (None, NEPTUS_SCHEMA):
                output, report = wrapper.convert(path, start_record=0, stop_record=1,
                    schema=schema, estimated_state=True)
                fields = common(self, folder, output, PROFILE)["observations"][0]["fields"]
                self.assertEqual(fields["estimated_offset_down_m"], -3)
                self.assertEqual(fields["source_record_index"], 0)
            with self.assertRaises(ValueError):
                wrapper.convert(path, start_record=0, stop_record=1, estimated_state=1)

    def common(self, output, folder):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not reader:
            self.skipTest('actual common reader required')
        path = folder / 'common.csv'; path.write_text(output)
        return json.loads(subprocess.run([reader, str(ROOT / 'profiles/declared/imc-sensors/profile.toml'),
            str(path), '--allow-equal-time'], capture_output=True, check=True).stdout)

    def test_large_concatenated_adjacent_windows_and_common(self):
        outside = frame(65000, bytes(60000))[:-2] + b'\0\0'
        selected = [frame(263, 20), frame(65001, b'unknown'), frame(252, -2), frame(265, 7.5)]
        raw = outside * 280 + b''.join(selected)
        self.assertGreater(len(raw), imc.LIMIT)
        compressed = gzip.compress(raw[:9000001], mtime=0) + gzip.compress(raw[9000001:], mtime=0)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); path = folder / 'Data.lsf.gz'; path.write_bytes(compressed)
            fields = []
            for start, stop in ((280, 282), (282, 284)):
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('no whole-file intake')):
                    output, report = m.convert(path, start_record=start, stop_record=stop)
                gz, selection = report['gzip_source'], report['record_selection']
                self.assertEqual((gz['compressed_sha256'], gz['expanded_lsf_sha256']), (sha(compressed), sha(raw)))
                self.assertEqual((gz['gzip_members'], gz['expanded_lsf_bytes']), (2, len(raw)))
                self.assertEqual((selection['source_records'], selection['selected_records'], selection['excluded_records']), (284, 2, 282))
                self.assertEqual(selection['selected_stream_sha256'], sha(b''.join(selected[start-280:stop-280])))
                self.assertEqual(gz['outside_native_selection'], 'PAYLOAD_AND_IMC_CRC_NOT_DECODED_OR_VALIDATED')
                if start == 280:
                    unknown = report['unsupported_frames'][0]
                    self.assertEqual((unknown['source_record_index'], unknown['frame_hex']), (281, selected[1].hex()))
                common = self.common(output, folder)
                self.assertEqual(common['platform_domain'], 'Unknown')
                for observation in common['observations']:
                    self.assertEqual(observation['clock_basis'], 'Unknown')
                    self.assertIsNone(observation['anchor_unix_us'])
                    fields.append(observation['fields'])
            self.assertEqual([f['source_record_index'] for f in fields], [280, 282, 283])
            self.assertEqual(fields[0]['temperature_k'], 293.15)
            self.assertEqual(fields[1]['electrical_current_a'], -2)
            self.assertEqual(fields[2]['depth_m'], 7.5)
            for f, n in zip(fields, [0, 2, 3]):
                self.assertEqual(f['source_file_sha256'], 'sha256:' + sha(raw))
                self.assertEqual(f['source_byte_offset'], len(outside)*280 + sum(map(len, selected[:n])))
                self.assertEqual(f['source_frame_hex'], 'hex:' + selected[n].hex())

    def test_small_native_bytes_parity_and_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); path = folder/'Data.lsf.gz'; native = folder/'Data.lsf'
            for order, schema in (('<', None), ('>', imc.NEPTUS_SCHEMA)):
                raw = frame(263, 12, order) + frame(264, 1013.25, order)
                compressed = gzip.compress(raw, mtime=0); path.write_bytes(compressed); native.write_bytes(raw)
                output, report = m.convert(path, start_record=0, stop_record=2, schema=schema)
                expected, native_report = imc.convert_path(native, start_record=0, stop_record=2, schema=schema)
                self.assertEqual(output, expected)
                self.assertEqual({k:v for k,v in report.items() if k != 'gzip_source'}, native_report)
                legacy, _ = imc.convert_input(compressed, schema=schema)
                for row, old in zip(csv.DictReader(io.StringIO(output)), csv.DictReader(io.StringIO(legacy))):
                    self.assertEqual({k:row[k] for k in old}, old)
                self.assertEqual(self.common(output, folder)['main_rows'], 2)

    def test_gzip_native_and_resource_refusals(self):
        raw = frame(263, 20); good = gzip.compress(raw, mtime=0)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); path = folder/'input.gz'
            badcrc = bytearray(good); badcrc[-8] ^= 1
            invalids = [b'', raw, good[:-1], bytes(badcrc), good+b'junk', good+b'\0',
                        gzip.compress(b'', mtime=0), good+gzip.compress(b'truncated', mtime=0),
                        gzip.compress(raw[:-1], mtime=0), gzip.compress(raw[:-2]+b'\0\0', mtime=0)]
            for content in invalids:
                path.write_bytes(content)
                with self.subTest(length=len(content)), self.assertRaises(ValueError):
                    m.convert(path, start_record=0, stop_record=1)
            path.write_bytes(good)
            for setting, limit in [('MAX_COMPRESSED_BYTES', len(good)-1), ('MAX_EXPANDED_BYTES', len(raw)-1),
                                   ('MAX_MEMBERS', 0), ('MAX_REPORT_BYTES', 1)]:
                with self.subTest(bound=setting), patch.object(m, setting, limit), self.assertRaises(ValueError):
                    m.convert(path, start_record=0, stop_record=1)
            path.write_bytes(good+good)
            with patch.object(m, 'CHUNK_BYTES', 1):
                output, report = m.convert(path, start_record=0, stop_record=2)
            self.assertEqual(report['gzip_source']['gzip_members'], 2)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 2)
            for options in [dict(start_record=True, stop_record=1), dict(start_record=0, stop_record=3),
                dict(start_record=0, stop_record=1, schema='wrong'),
                dict(start_record=0, stop_record=1, water_velocity=1)]:
                with self.assertRaises(ValueError): m.convert(path, **options)
            path.write_bytes(good + bytes(badcrc))
            with patch.object(imc, 'convert_path', side_effect=AssertionError('late CRC must fail before semantic conversion')):
                with self.assertRaises(ValueError): m.convert(path, start_record=0, stop_record=1)

    def test_legacy_water_velocity_reuses_native_and_common(self):
        for order, x in (('<', 1.25), ('>', -3.5)):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory); path = folder/'Data.lsf.gz'; native = folder/'Data.lsf'
                raw = frame(260, struct.pack(order+'Bddd', 3, x, 0., 99.), order)
                compressed = gzip.compress(raw, mtime=0)
                path.write_bytes(compressed); native.write_bytes(raw)
                options = dict(schema=imc.NEPTUS_SCHEMA, water_velocity=True)
                output, report = m.convert(path, start_record=0, stop_record=1, **options)
                expected, native_report = imc.convert_path(native, start_record=0, stop_record=1, **options)
                self.assertEqual(output, expected)
                self.assertEqual({k:v for k,v in report.items() if k != 'gzip_source'}, native_report)
                old, _ = imc.convert_input(compressed, **options)
                row = next(csv.DictReader(io.StringIO(output)))
                old_row = next(csv.DictReader(io.StringIO(old)))
                self.assertEqual({k:row[k] for k in old_row}, old_row)
                fields = self.common(output, folder)['observations'][0]['fields']
                self.assertEqual(fields['reported_water_velocity_x_m_s'], x)
                self.assertEqual(fields['reported_water_velocity_y_m_s'], 0.)
                self.assertIsNone(fields['reported_water_velocity_z_m_s'])
                self.assertEqual(fields['source_record_index'], 0)
                self.assertEqual(report['gzip_source']['compressed_sha256'], sha(compressed))
                self.assertEqual(report['gzip_source']['expanded_lsf_sha256'], sha(raw))
                with self.assertRaises(ValueError):
                    m.convert(path, start_record=0, stop_record=1, schema='wrong', water_velocity=True)

    def test_cli_fresh_output_and_late_failure_no_success_directory(self):
        raw = frame(263, 20)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); path = folder/'input.gz'; path.write_bytes(gzip.compress(raw, mtime=0))
            target = folder/'out'
            command = [os.sys.executable, str(ROOT/'scripts/convert_imc_gzip_window.py'), str(path), str(target),
                       '--start-record', '0', '--stop-record', '1']
            run = subprocess.run(command, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((target/'report.json').read_text())
            self.assertEqual(report['gzip_source']['expanded_lsf_sha256'], sha(raw))
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            path.write_bytes(path.read_bytes()[:-1]); command[3] = str(folder/'failed')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse((folder/'failed').exists())


if __name__ == '__main__':
    unittest.main()
