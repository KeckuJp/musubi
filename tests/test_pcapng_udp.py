"""Authored limited PCAPNG, not real capture qualification."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from dpkt import pcapng as ng
from scripts import convert_pcapng_udp as target
from tests.test_agopen_steering import datagram
from tests.test_imc_sensors import frame
from tests.test_nmea_wind_conversion import sentence

ROOT = Path(__file__).resolve().parents[1]
PORT = 6002


def capture(payload, little=True, resolution=6, offset=0, ticks=2000000):
    endian = "<" if little else ">"
    sh, interface, packet, option = ((ng.SectionHeaderBlockLE, ng.InterfaceDescriptionBlockLE,
        ng.EnhancedPacketBlockLE, ng.PcapngOptionLE) if little else
        (ng.SectionHeaderBlock, ng.InterfaceDescriptionBlock, ng.EnhancedPacketBlock, ng.PcapngOption))
    idb = interface(linktype=1, snaplen=65535)
    idb.opts = [option(code=9, data=bytes([resolution])),
        option(code=14, data=struct.pack(endian + "q", offset)), option()]
    epb = packet(ts_high=ticks >> 32, ts_low=ticks & 0xffffffff,
        pkt_data=datagram(payload, PORT))
    return bytes(sh()) + bytes(idb) + bytes(epb)


class PcapngTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual reader required")
    def test_two_layouts_actual_common(self):
        with tempfile.TemporaryDirectory() as folder:
            for little, kind, payload, field, expected, res, ticks in [
                (True, "imc", frame(263, 20), "temperature_k", 293.15, 6, 2000000),
                (False, "nmea-wind", sentence("WIMWV,90,R,10,M,A").encode(), "wind_speed_m_s", 10, 128 + 10, 2048)]:
                raw = capture(payload, little, res, -1, ticks)
                text, report = target.convert(raw, PORT, payload_format=kind)
                path = Path(folder) / (kind + ".csv")
                path.write_text(text)
                profile = ROOT / "profiles/declared/imc-sensors/profile.toml"
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(path)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                obs = json.loads(result.stdout)["observations"][0]
                self.assertAlmostEqual(obs["fields"][field], expected)
                self.assertEqual(obs["clock_basis"], "Unknown")
                self.assertEqual(obs["fields"]["pcap_source_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["pcapng"]["packets"][0]["capture_time_us"], 1000000)
                self.assertEqual(report["pcapng"]["packets"][0]["block_ordinal"], 2)
                self.assertNotEqual(report["source_sha256"], report["derived_capture_sha256"])

    def test_important_refusals(self):
        good = capture(frame(263, 20))
        _, proof = target.qualify(good)
        start = proof["packets"][0]["block_byte_offset"]
        cases = [good[:-1], capture(frame(263, 20), resolution=9, ticks=1), good + bytes(ng.SectionHeaderBlockLE())]
        for offset, value in [(start + 8, 1), (start + 24, 9999), (start + 4, 13), (start, 3)]:
            bad = bytearray(good)
            struct.pack_into("<I", bad, offset, value)
            cases.append(bytes(bad))
        bad = bytearray(good)
        struct.pack_into("<H", bad, 28 + 16, 100)
        cases.append(bytes(bad))
        for raw in cases:
            with self.subTest(raw=raw[:12]), self.assertRaises(ValueError):
                target.convert(raw, PORT)

    def test_cli_exact_original_and_no_overwrite(self):
        raw = capture(frame(263, 20))
        with tempfile.TemporaryDirectory() as folder:
            source, dest = Path(folder) / "in.pcapng", Path(folder) / "output"
            source.write_bytes(raw)
            command = [sys.executable, str(ROOT / "scripts/convert_pcapng_udp.py"), str(source), str(dest),
                "--udp-destination-port", str(PORT)]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((dest / "source.pcapng").read_bytes(), raw)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
