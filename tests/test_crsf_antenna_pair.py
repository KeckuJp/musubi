"""Authored fixed CRSF reports, not physical antenna-cause evidence."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_crsf_link import convert
from tests.test_crsf_link import frame, capture

ROOT = Path(__file__).resolve().parents[1]


class AntennaPairTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_two_saved_layouts_reach_common_without_causal_claim(self):
        packets = [frame(bytes([70, 80, 99, 0, 1, 0, 0, 90, 50, 0])),
                   frame(bytes([100, 20, 0, 0, 255, 0, 0, 90, 0, 0])),
                   frame(bytes([0, 0, 100, 0, 0, 0, 0, 90, 100, 0]))]
        inputs = [capture([(7, packets[0])]),
                  'time_us,frame_hex,note,other\n' +
                  '\n'.join(f'{8+i},{p.hex()},kept,extra' for i, p in enumerate(packets[1:])) + '\n']
        for text, expected in zip(inputs, ([10], [-80, 0])):
            output, report = convert(text, compare_antennas=True)
            self.assertEqual(report['decoded_records'], len(expected))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'out.csv'
                path.write_text(output)
                common = json.loads(subprocess.run([
                    os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/crsf-link/profile.toml'),
                    str(path)], check=True, capture_output=True).stdout)
            self.assertEqual(common['main_rows'], len(expected))
            self.assertEqual(len(common['observations']), 2 * len(expected))
            for index, (observation, delta) in enumerate(zip(common['observations'], [v for v in expected for _ in range(2)])):
                fields = observation['fields']
                self.assertEqual(fields['reported_antenna_1_minus_2_db'], delta)
                self.assertEqual(fields['uplink_rssi_1_dbm'] - fields['uplink_rssi_2_dbm'], delta)
                self.assertEqual(observation['clock_basis'], 'Unknown')
                self.assertEqual(fields['antenna_comparison_basis'],
                                 'SAME_FRAME_REPORTED_DBM_DIFFERENCE_NOT_GAIN_OR_FAULT')
                if index % 2 == 0:  # provenance belongs to the existing onboard projection
                    self.assertTrue(fields['source_frame_hex'].startswith('hex:'))
                    self.assertTrue(fields['source_record_hex'].startswith('hex:'))
            if len(expected) == 2:
                self.assertEqual(common['observations'][0]['fields']['active_antenna'], 'UNKNOWN_255')

    def test_nonselected_not_missing_sample_and_bad_frame_rejected(self):
        valid = frame()
        output, report = convert(capture([(1, frame(b'abc', 0x30)), (2, valid)]), compare_antennas=True)
        self.assertEqual((report['source_records'], report['decoded_records']), (2, 1))
        self.assertEqual(len(report['unsupported_records']), 1)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 1)
        for bad in (valid[:-1], valid[:-1] + bytes([valid[-1] ^ 1]), frame(bytes(9))):
            with self.subTest(bad=bad.hex()), self.assertRaises(ValueError):
                convert(capture([(1, bad)]), compare_antennas=True)
        with self.assertRaises(ValueError):
            convert(capture([(1, valid)]), 'attitude', compare_antennas=True)


class ElrsCodeLabelTests(unittest.TestCase):
    """Fixed ExpressLRS a60b68af rf_Mode/uplink_TX_Power tables; labels, not measured RF."""

    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_two_saved_inputs_reach_common_with_raw_codes_kept(self):
        # payload: rssi1, rssi2, lq, snr, antenna, rf_mode, power, drssi, dlq, dsnr
        cases = [([(5, frame(bytes([60, 70, 100, 9, 0, 27, 7, 80, 100, 5])))],
                   [('RATE_LORA_2G4_250HZ', 250, 'CONFIGURED_LEVEL_REPORTED', 27, 7)]),
                  ([(6, frame(bytes([90, 90, 50, 0, 1, 4, 0, 95, 40, 0]))),
                    (6, frame(bytes([90, 90, 50, 0, 1, 12, 9, 95, 40, 0]))),
                    (7, frame(bytes([90, 90, 50, 0, 1, 101, 8, 95, 40, 0])))],
                   [('RATE_LORA_900_150HZ', None, 'ZERO_NOT_A_LEVEL', 4, 0),
                    ('UNKNOWN_12', None, 'UNKNOWN_CODE', 12, 9),
                    ('RATE_LORA_DUAL_150HZ', 50, 'CONFIGURED_LEVEL_REPORTED', 101, 8)])]
        for rows, expected in cases:
            output, report = convert(capture(rows), elrs_a60b68af_codes=True)
            self.assertEqual(report['decoded_records'], len(expected))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'out.csv'
                path.write_text(output)
                common = json.loads(subprocess.run([
                    os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/crsf-link/profile.toml'),
                    str(path), '--allow-equal-time'], check=True, capture_output=True).stdout)
            self.assertEqual(common['main_rows'], len(expected))
            for observation, (label, mw, status, mode, power) in zip(common['observations'][::2], expected):
                fields = observation['fields']
                self.assertEqual((fields['reported_rf_mode_enum'], fields.get('reported_tx_power_level_mw'),
                                  fields['reported_tx_power_code_status'], fields['rf_mode_code'],
                                  fields['tx_power_code']), (label, mw, status, mode, power))
                self.assertEqual(fields['elrs_code_basis'],
                                 'CALLER_DECLARED_ELRS_A60B68AF_ENUM_LABEL_NOT_MEASURED_RATE_OR_POWER')
                self.assertEqual(observation['clock_basis'], 'Unknown')

    def test_default_unchanged_and_other_kinds_or_bad_frames_refuse(self):
        valid = capture([(1, frame())])
        self.assertNotIn('reported_rf_mode_enum', convert(valid)[0])
        self.assertEqual(convert(valid)[0].splitlines()[1],
                         convert(valid, elrs_a60b68af_codes=True)[0].splitlines()[1].rsplit(',', 4)[0])
        for kind in ('link-tx', 'link-rx', 'channels-elrs-a60b68af-16ch'):
            with self.assertRaises(ValueError):
                convert(valid, kind, elrs_a60b68af_codes=True)
        with self.assertRaises(ValueError):
            convert(valid, elrs_a60b68af_codes=1)
        bad = frame()
        with self.assertRaises(ValueError):
            convert(capture([(1, bad[:-1] + bytes([bad[-1] ^ 1]))]), elrs_a60b68af_codes=True)


class CsvWindowTests(unittest.TestCase):
    """A saved frame CSV over 16MiB, read through bounded line windows into the same converter."""

    @classmethod
    def setUpClass(cls):
        pad = 'p' * 1000
        rows = []
        for index in range(17000):
            payload = bytes([60, 70, 100, 9, 0, 27, index % 9, 80, 100, 5])
            data = frame(b'\x00\x01\x00\x02\x00\x03', 0x1E) if index in (4, 100, 101, 102) else frame(payload)
            if index == 300:  # corrupt outside the selected windows: framed and hashed, not decoded
                data = data[:-1] + bytes([data[-1] ^ 1])
            rows.append(f'{1000 + index},{data.hex()},{pad}\n')
        cls.raw = ('time_us,frame_hex,note\n' + ''.join(rows)).encode()
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = Path(cls.directory.name) / 'long.csv'
        cls.path.write_bytes(cls.raw)
        assert len(cls.raw) > 16 * 1024 * 1024

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def offset(self, line):
        return sum(len(x) for x in self.raw.split(b'\n')[:line - 1]) + line - 1

    def test_selected_windows_reach_common_with_global_rows(self):
        from scripts.convert_crsf_link import convert_csv_window
        import hashlib
        digest = hashlib.sha256(self.raw).hexdigest()
        for start, stop, decoded in ((1, 11, 9), (16990, 17001, 11)):
            output, report = convert_csv_window(self.path, start_line=start, stop_line=stop,
                expected_size=len(self.raw), expected_sha256=digest, elrs_a60b68af_codes=True)
            window = report['source_window']
            self.assertEqual((window['total_physical_lines'], window['selected_physical_lines'],
                              window['excluded_physical_lines'], report['decoded_records']),
                             (17001, stop - start, 17000 - (stop - start), decoded))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertTrue(all(r['source_input_sha256'] == 'sha256:' + digest for r in rows))
            lines = [int(r['source_original_line']) for r in rows]
            self.assertEqual(lines[-1], stop)
            self.assertEqual([int(r['source_original_byte_offset']) for r in rows], [self.offset(x) for x in lines])
            self.assertEqual([int(r['record_time_us']) for r in rows], [999 + x - 1 for x in lines])
            if start == 1:
                self.assertNotIn(6, lines)  # physical line 6 is the nonselected attitude frame
                self.assertEqual([(r['original_line'], r['original_byte_offset']) for r in report['unsupported_records']],
                                 [(6, self.offset(6))])
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'window.csv'
                    path.write_text(output)
                    common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(ROOT / 'profiles/declared/crsf-link/profile.toml'), str(path)],
                        check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], decoded)
                fields = common['observations'][0]['fields']
                self.assertEqual((fields['reported_rf_mode_enum'], fields['source_original_line']),
                                 ('RATE_LORA_2G4_250HZ', lines[0]))

    def test_unused_corrupt_boundary_and_default_refusals(self):
        from scripts.convert_crsf_link import convert_csv_window
        for start, stop, kind, extra in ((101, 104, 'link', {}),            # only nonselected frames
                                         (295, 305, 'link', {}),            # corrupt selected row
                                         (0, 5, 'link', {}), (1, 17002, 'link', {}),
                                         (1, 5, 'msp-attitude-bf452', {}),
                                         (1, 5, 'link', dict(expected_size=len(self.raw), expected_sha256='0' * 64)),
                                         (1, 5, 'link-tx', dict(elrs_a60b68af_codes=True))):
            with self.subTest(start=start, stop=stop, kind=kind), self.assertRaises(ValueError):
                convert_csv_window(self.path, kind, start_line=start, stop_line=stop, **extra)
        import sys
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(ROOT / 'scripts/convert_crsf_link.py'), str(self.path)]
            self.assertEqual(subprocess.run(command + [str(Path(directory) / 'default')], capture_output=True).returncode, 2)
            self.assertFalse((Path(directory) / 'default').exists())
            ok = subprocess.run(command + [str(Path(directory) / 'window'), '--start-line', '1', '--stop-line', '3'],
                                capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn('source_original_line', (Path(directory) / 'window/observations.csv').read_text())
            mixed = subprocess.run(command + [str(Path(directory) / 'mixed'), '--start-line', '1', '--stop-line', '3',
                                              '--start-packet', '0'], capture_output=True)
            self.assertEqual(mixed.returncode, 2)


if __name__ == '__main__':
    unittest.main()
