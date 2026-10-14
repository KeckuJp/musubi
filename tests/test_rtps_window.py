"""Authored windows only; reuse RTPS/CDR semantics and actual common reader."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_rtps_window import convert_path
from tests.test_rtps_data import capture, wire, data_message, binding, PORT
from tests.test_rtp_records import datagram, pcap

ROOT = Path(__file__).resolve().parents[1]


class RtpsWindowTests(unittest.TestCase):
    def common(self, text, folder, count):
        path = folder / "common.csv"
        path.write_text(text)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required")
        run = subprocess.run([reader, str(ROOT / "profiles/declared/rtps-data/profile.toml"),
                              str(path), "--allow-equal-time"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        obj = json.loads(run.stdout)
        self.assertEqual(obj["main_rows"], count)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in obj["observations"]))
        self.assertTrue(all(o["fields"]["battery_voltage_v"] == 24. for o in obj["observations"]))

    def test_small_and_large_original_reuse_meaning_and_global_indexes(self):
        # Second separately authored capture uses BE submessage and another sequence.
        large = pcap([datagram(b"x" * 60000, port=1)] * 281 +
                     [datagram(wire(data_message(order=">", seq=9)), port=PORT)])
        self.assertGreater(len(large), 16 * 1024**2)
        cases = [(capture(b"unselected", wire(data_message())), 101001000, 102000000, 1),
                 (large, 381281000, 382000000, 281)]
        for raw, start, stop, original in cases:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                path = folder / "original.pcap"
                path.write_bytes(raw)
                text, report = convert_path(path, PORT, start_us=start, stop_us=stop, binding=binding(raw))
                row = list(csv.DictReader(io.StringIO(text)))[0]
                self.assertEqual(row["rtps_packet_index"], str(original))
                self.assertEqual(row["rtps_capture_sha256"], binding(raw)["source_input_sha256"])
                self.assertEqual(row["rtps_timestamp_disposition"], "NOT_REPORTED")
                source = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                self.assertEqual(source["packet_index"], original)
                self.assertEqual(source["window_packet_index"], 0)
                self.assertNotEqual(source["source_input_sha256"], source["derived_capture_sha256"])
                self.assertEqual(report["source_packets"], 1)
                self.assertEqual(report["capture_window"]["source_packets"], original + 1)
                self.assertEqual(report["capture_window"]["outside_window_packets"], original)
                self.assertEqual(report["packets"][0]["index"], original)
                self.assertIn("TYPE_HASH_ABSENT", row["rtps_schema_basis"])
                self.common(text, folder, 1)
                self.assertEqual(path.read_bytes(), raw)

    def test_bad_binding_interval_truncation_and_selected_payload_refuse(self):
        good = capture(wire(data_message()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.pcap"
            path.write_bytes(good)
            for start, stop, declared in [(0, 0, binding(good)), (0, 1, binding(good)),
                                        (0, 200000000, binding(good + b"x"))]:
                with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                    convert_path(path, PORT, start_us=start, stop_us=stop, binding=declared)
            for bad in [good[:-1], good[:-1] + bytes([good[-1] ^ 1]),
                        capture(wire(data_message(flags=7)))]:
                path.write_bytes(bad)
                with self.assertRaises(ValueError):
                    convert_path(path, PORT, start_us=0, stop_us=200000000, binding=binding(bad))

    def test_cli_preserves_binding_and_refuses_existing_destination(self):
        raw = capture(wire(data_message()))
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source, declare, output = folder / "source.pcap", folder / "binding.json", folder / "out"
            source.write_bytes(raw)
            declare.write_text(json.dumps(binding(raw)))
            cmd = [sys.executable, str(ROOT / "scripts/convert_rtps_window.py"), str(source), str(output),
                   "--binding", str(declare), "--port", str(PORT), "--start-us", "0", "--stop-us", "200000000"]
            first = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual((output / "binding.json").read_bytes(), declare.read_bytes())
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["binding"], binding(raw))
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
            self.assertEqual(source.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
