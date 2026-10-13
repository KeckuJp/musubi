"""Authored saved native captures; no physical receiver qualification."""
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
from scripts import convert_crsf_link as subject
from tests.test_msp_stream import attitude, reply
from tests.test_sport_stream_contract import packet, wire

ROOT = Path(__file__).resolve().parents[1]


class SerialFrameWindowTests(unittest.TestCase):
    def common(self, directory, text, profile, empty=False):
        path = Path(directory) / "observations.csv"
        path.write_text(text)
        args = [os.environ["MUSUBI_TELEMETRY_READER"], str(ROOT / "profiles/declared/crsf-link" / profile), str(path), "--allow-equal-time"]
        if empty:
            args.append("--allow-empty-rows")
        return json.loads(subprocess.run(args, check=True, capture_output=True).stdout)

    def test_large_original_both_framings_and_global_provenance(self):
        for kind, frame, profile in ((subject.MSP_SERIAL_KIND, attitude(), "msp-profile.toml"),
                                     (subject.SPORT_SERIAL_KIND, wire(packet(0x0210, 1234)), "profile.toml")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "capture.bin"
                prefix = b"n" * (subject.LIMIT + 1)
                source = prefix + frame + frame
                path.write_bytes(source)
                text, report = subject.convert_native_file(path, kind, capture_time_us=123, start_packet=1, stop_packet=2)
                row = next(csv.DictReader(io.StringIO(text)))
                self.assertEqual(int(row["source_packet_index"]), 1)
                self.assertEqual(int(row["source_start_offset"]), len(prefix) + len(frame))
                self.assertEqual(row["source_input_sha256"], "sha256:" + hashlib.sha256(source).hexdigest())
                native = report["native_stream"]
                self.assertEqual((native["complete_framing_records"], native["unselected_framing_records"]), (2, 1))
                self.assertEqual(native["discarded_bytes"], len(prefix))
                self.assertLess(len(json.dumps(report)), 10000)
                self.assertEqual(self.common(directory, text, profile)["main_rows"], 1)

    def test_unknown_only_tail_selected_crc_and_default_parity(self):
        valid = attitude()
        unknown = reply(b"x", 1)
        corrupt = valid[:-1] + bytes([valid[-1] ^ 1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.bin"
            path.write_bytes(unknown + corrupt + valid + b"$M>\x06")
            text, report = subject.convert_native_file(path, subject.MSP_SERIAL_KIND, capture_time_us=0, start_packet=0, stop_packet=1)
            self.assertEqual(report["decoded_records"], 0)
            self.assertEqual(len(report["unsupported_records"]), 1)
            self.assertEqual(report["window_meaning_status"], "KNOWN_MEANING_ZERO_UNSUPPORTED_PRESENT")
            self.assertEqual(report["native_stream"]["discarded_bytes_by_reason"], {"INCOMPLETE_AT_EOF": 4})
            self.assertEqual(self.common(directory, text, "msp-profile.toml", True)["main_rows"], 0)
            with self.assertRaises(ValueError):
                subject.convert_native_file(path, subject.MSP_SERIAL_KIND, capture_time_us=0, start_packet=1, stop_packet=2)
            path.write_bytes(valid)
            window, _ = subject.convert_native_file(path, subject.MSP_SERIAL_KIND, capture_time_us=0, start_packet=0, stop_packet=1)
            old, _ = subject.convert_msp_stream(valid, capture_time_us=0)
            self.assertEqual(window, old)
            self.assertEqual(self.common(directory, window, "msp-profile.toml")["main_rows"], 1)

    def test_refusals_and_changed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.bin"; path.write_bytes(attitude())
            for start, stop in ((None, 1), (True, 1), (0, 0), (0, 4097), (0, 2)):
                with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                    subject.convert_native_file(path, subject.MSP_SERIAL_KIND, capture_time_us=0, start_packet=start, stop_packet=stop)
            link = Path(directory) / "link"; link.symlink_to(path)
            with self.assertRaises(ValueError):
                subject.convert_native_file(link, subject.MSP_SERIAL_KIND, capture_time_us=0, start_packet=0, stop_packet=1)
            original = subject.convert_msp_stream
            def replace(data, **kwargs):
                result = original(data, **kwargs)
                replacement = Path(directory) / "replacement"; replacement.write_bytes(attitude())
                replacement.replace(path)
                return result
            with patch.object(subject, "convert_msp_stream", side_effect=replace), self.assertRaisesRegex(ValueError, "changed"):
                subject.convert_native_file(path, subject.MSP_SERIAL_KIND, capture_time_us=0, start_packet=0, stop_packet=1)
