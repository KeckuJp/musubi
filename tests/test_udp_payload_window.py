"""Authored saved PCAP windows through existing payload meanings, no network."""
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_imc_sensors as legacy
from scripts import convert_udp_payload_window as target
from tests.test_agopen_steering import datagram
from tests.test_agopen_steering_window import write_capture
from tests.test_imc_sensors import frame
from tests.test_nmea_wind_conversion import sentence

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/imc-sensors/profile.toml"
PORT = 6002


class UdpPayloadWindowTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_single_vlan_window_preserves_global_indices_and_two_payload_meanings(self):
        def tagged(packet, inner=0x0800):
            return packet[:12] + struct.pack(">HHH", 0x8100, 7, inner) + packet[14:]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind, payload, field, value in (
                    ("imc", frame(263, 20), "temperature_k", 293.15),
                    ("nmea-wind", sentence("WIMWV,90,R,10,N,A").encode(), "wind_speed_m_s", 10 * 1852 / 3600)):
                source = root / (kind + ".pcap")
                selected = tagged(datagram(payload, PORT))
                unselected = tagged(datagram(payload, PORT + 1))
                write_capture(source, [(100, b"x" * 1024**2)] * 17 +
                              [(200, selected), (200, unselected), (300, b"x")])
                digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
                with patch.object(Path, "read_bytes", side_effect=AssertionError("no whole read")):
                    output, report = target.convert_path(source, PORT, start_us=200, stop_us=300,
                                                        payload_format=kind, single_vlan=True)
                fields = self.common(output, root / (kind + ".csv"))
                self.assertAlmostEqual(fields[0][field], value)
                self.assertEqual(fields[0]["pcap_packet_index"], 17)
                self.assertEqual(fields[0]["pcap_source_sha256"], digest)
                self.assertEqual(fields[0]["vlan_tci"], 7)
                self.assertEqual(report["source_sha256"], digest)
                self.assertNotEqual(report["derived_capture_sha256"], digest)
                for key, index, local in (("selected_sources", 17, 0), ("vlan_unselected", 18, 1)):
                    self.assertEqual(report[key][0]["packet_index"], index)
                    self.assertEqual(report[key][0]["window_packet_index"], local)
                    self.assertEqual(report[key][0]["vlan_inner_ethertype"], 0x0800)
                self.assertEqual(report["selected_sources"][0]["packet_hex"], selected.hex())
                self.assertEqual((report["selected_packets"], report["unselected_packets"]), (1, 1))
            bad = root / "nested.pcap"
            write_capture(bad, [(200, tagged(datagram(frame(263, 20), PORT), 0x8100))])
            with self.assertRaises(ValueError):
                target.convert_path(bad, PORT, start_us=200, stop_us=300, single_vlan=True)
            refused = subprocess.run([sys.executable, str(ROOT / "scripts/convert_udp_payload_window.py"),
                str(bad), str(root / "refused"), "--udp-destination-port", str(PORT),
                "--start-us", "200", "--stop-us", "300", "--single-vlan"], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse((root / "refused").exists())
            with self.assertRaises(ValueError):
                target.convert_path(bad, PORT, start_us=200, stop_us=300, payload_format="tcp", single_vlan=True)

    def common(self, output, path):
        path.write_text(output)
        read = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                               str(path), "--allow-equal-time"], capture_output=True)
        self.assertEqual(read.returncode, 0, read.stderr.decode())
        value = json.loads(read.stdout)
        self.assertEqual(value["platform_domain"], "Unknown")
        for obs in value["observations"]:
            self.assertEqual(obs["clock_basis"], "Unknown")
            self.assertIsNone(obs["anchor_unix_us"])
        return [obs["fields"] for obs in value["observations"]]

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_large_adjacent_imc_windows_keep_global_identity_and_quantities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "large.pcap"
            selected = [(200, datagram(frame(263, 20), PORT)),
                        (200, datagram(frame(65000, b"future"), PORT)),
                        (200, datagram(frame(263, 99), PORT + 1)),
                        (200, datagram(frame(263, 20), PORT)),
                        (300, datagram(frame(264, 1013.25), PORT))]
            write_capture(source, [(100, b"x" * 1024**2)] * 17 + selected + [(400, b"x")])
            self.assertGreater(source.stat().st_size, 16 * 1024**2)
            original = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            seen = []
            for start, stop, indices, selected_count in ((200, 300, [17, 20], 4), (300, 400, [21], 1)):
                with patch.object(Path, "read_bytes", side_effect=AssertionError("no whole read")):
                    output, report = target.convert_path(source, PORT, start_us=start, stop_us=stop)
                fields = self.common(output, root / f"{start}.csv")
                self.assertEqual([f["pcap_packet_index"] for f in fields], indices)
                self.assertTrue(all(f["pcap_source_sha256"] == original for f in fields))
                self.assertTrue(all(f["record_time_us"] == start for f in fields))
                self.assertTrue(all(f["imc_payload_time_us"] == 1700000000250000 for f in fields))
                seen.extend(indices)
                if start == 200:
                    self.assertEqual([f["temperature_k"] for f in fields], [293.15, 293.15])
                    self.assertEqual(report["selected_sources"][1]["imc"]["unsupported_frames"][0]["message_id"], 65000)
                    self.assertEqual(report["unselected_packets"], 1)
                else:
                    self.assertEqual(fields[0]["external_pressure_pa"], 101325)
                window = report["capture_window"]
                self.assertEqual((window["source_packets"], window["selected_packets"],
                                  window["outside_window_packets"]), (23, selected_count, 23-selected_count))
                self.assertEqual(window["outside_window"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual(report["source_sha256"], original)
                self.assertNotEqual(report["derived_capture_sha256"], original)
                for entry in report["selected_sources"]:
                    self.assertEqual(window["original_packet_indexes"][entry["window_packet_index"]], entry["packet_index"])
                    self.assertEqual(entry["packet_hex"], selected[entry["packet_index"]-17][1].hex())
            self.assertEqual(seen, [17, 20, 21])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_wind_reuses_units_withheld_and_within_packet_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "wind.pcap"
            payload = (sentence("WIMWV,90,R,10,M,A") + "\n" + sentence("WIMWV,0,T,0,N,V")).encode()
            write_capture(source, [(10, b"x"), (20, datagram(payload, PORT)),
                                  (20, datagram(sentence("GPXXX,1").encode(), PORT)), (30, b"x")])
            output, report = target.convert_path(source, PORT, start_us=20, stop_us=30, payload_format="nmea-wind")
            fields = self.common(output, root / "wind.csv")
            self.assertEqual([f["pcap_packet_index"] for f in fields], [1, 1])
            self.assertEqual([f["source_line"] for f in fields], [1, 2])
            self.assertAlmostEqual(fields[0]["wind_angle_rad"], math.pi/2)
            self.assertEqual(fields[0]["wind_speed_m_s"], 10)
            self.assertIsNone(fields[1]["wind_speed_m_s"])
            self.assertTrue(all(f["nmea_unlocated_time_us"] == 0 for f in fields))
            self.assertEqual(report["valid_wind_measurements"], 1)
            self.assertEqual((report["selected_packets"], report["decoded_records"]), (2, 2))
            self.assertEqual(len(report["selected_sources"]), 2)

    def test_small_parity_and_native_cli_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "small.pcap"
            write_capture(source, [(20, datagram(frame(263, 5), PORT))])
            expected, old_report = legacy.convert_pcap(source.read_bytes(), PORT)
            output, report = target.convert_path(source, PORT, start_us=0, stop_us=30)
            self.assertEqual(output, expected)
            self.assertEqual(report["source_sha256"], old_report["source_sha256"])
            command = [sys.executable, str(ROOT / "scripts/convert_udp_payload_window.py"),
                       str(source), str(root / "new"), "--udp-destination-port", str(PORT),
                       "--start-us", "0", "--stop-us", "30"]
            run = subprocess.run(command, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            self.assertEqual((root / "new/observations.csv").read_text(), expected)
            self.assertEqual(json.loads((root / "new/report.json").read_text()), report)
            again = subprocess.run(command, capture_output=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual((root / "new/observations.csv").read_text(), expected)

    def test_important_refusals_before_silent_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.pcap"
            good = datagram(frame(263, 5), PORT)
            write_capture(source, [(20, good)])
            for options in ({"start_us":True,"stop_us":30}, {"start_us":20,"stop_us":20},
                            {"start_us":0,"stop_us":30,"payload_format":"guess"}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    target.convert_path(source, PORT, **options)
            for attr in ("CSV_LIMIT", "REPORT_LIMIT"):
                with patch.object(target, attr, 1), self.assertRaises(ValueError):
                    target.convert_path(source, PORT, start_us=0, stop_us=30)
            with patch.object(legacy, "PCAP_WINDOW_SELECTED_LIMIT", 24), self.assertRaises(ValueError):
                target.convert_path(source, PORT, start_us=0, stop_us=30)
            for records, nano in (([(20, good[:-1] + bytes([good[-1]^1]))],False),
                                  ([(19,b"x"),(18,b"x"),(20,good)],False),
                                  ([(20001,good)],True), ([(20,datagram(b"x",PORT+1))],False)):
                write_capture(source, records, nano=nano)
                with self.subTest(records=records, nano=nano), self.assertRaises(ValueError):
                    target.convert_path(source, PORT, start_us=20, stop_us=30)
            write_capture(source, [(20,good)])
            with source.open("ab") as stream:
                stream.write(b"x")
            with self.assertRaises(ValueError):
                target.convert_path(source, PORT, start_us=20, stop_us=30)


if __name__ == "__main__":
    unittest.main()
