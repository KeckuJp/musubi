"""Long authored originals, selected meaningful packets, separate provenance."""
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

from dpkt import pcapng as ng
from scripts import convert_pcapng_udp as target
from tests.test_pcapng_udp import capture, PORT, ROOT
from tests.test_imc_sensors import frame
from tests.test_nmea_wind_conversion import sentence


class PcapngWindowTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual reader required")
    def test_large_and_separate_layout_common(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for little, kind, payload, quantity, expected in (
                    (True, "imc", frame(263, 20), "temperature_k", 293.15),
                    (False, "nmea-wind", sentence("WIMWV,45,R,8,M,A").encode(), "wind_speed_m_s", 8)):
                raw = capture(payload, little, 6, 0, 2000000)
                _, metadata = target.qualify(raw)
                offset = metadata["packets"][0]["block_byte_offset"]
                block = ng.EnhancedPacketBlockLE if little else ng.EnhancedPacketBlock
                excluded = bytes(block(ts_low=1000000, pkt_data=b"x" * 60000))
                count = 290 if little else 2
                original = root / (kind + ".pcapng")
                with original.open("wb") as output:
                    output.write(raw[:offset])
                    for _ in range(count):
                        output.write(excluded)
                    output.write(raw[offset:])
                digest = "sha256:" + hashlib.sha256(original.read_bytes()).hexdigest()
                if little:
                    self.assertGreater(original.stat().st_size, 16 * 1024**2)
                    with self.assertRaises(ValueError):
                        target.convert_path(original, PORT, start_us=0, stop_us=3000000)
                with patch.object(Path, "read_bytes", side_effect=AssertionError("no whole read")):
                    text, report = target.convert_path(original, PORT, start_us=2000000,
                        stop_us=3000000, payload_format=kind)
                csv_path = root / (kind + ".csv")
                csv_path.write_text(text)
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                    str(csv_path)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                fields = json.loads(result.stdout)["observations"][0]["fields"]
                self.assertAlmostEqual(fields[quantity], expected)
                self.assertEqual(fields["pcap_packet_index"], count)
                self.assertEqual(fields["pcap_source_sha256"], digest)
                self.assertEqual(report["pcapng"]["window"]["excluded_packets"], count)
                self.assertEqual(report["selected_sources"][0]["window_packet_index"], 0)
                self.assertEqual(report["pcapng"]["packets"][0]["block_byte_offset"], offset + count * len(excluded))

    def test_small_default_and_important_window_refusals(self):
        raw = capture(frame(263, 20))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "in.pcapng"
            path.write_bytes(raw)
            legacy, _ = target.convert(raw, PORT)
            selected, _ = target.convert_path(path, PORT, start_us=0, stop_us=3000000)
            self.assertEqual(legacy, selected)
            for start, stop in [(True, 3), (3, 2), (0, None), (0, 1)]:
                with self.assertRaises(ValueError):
                    target.convert_path(path, PORT, start_us=start, stop_us=stop)
            with patch.object(target, "SOURCE_LIMIT", len(raw)-1), self.assertRaises(ValueError):
                target.convert_path(path, PORT, start_us=0, stop_us=3000000)
            _, metadata = target.qualify(raw)
            offset = metadata["packets"][0]["block_byte_offset"]
            outside = bytes(ng.EnhancedPacketBlockLE(ts_low=1, pkt_data=b"x"))
            for invalid in [raw + outside, raw + b"bad", raw[:offset] + outside[:-1]]:
                path.write_bytes(invalid)
                with self.assertRaises(ValueError):
                    target.convert_path(path, PORT, start_us=2000000, stop_us=3000000)


if __name__ == "__main__":
    unittest.main()
