"""Authored UDP composition; not real CTD or TCP evidence."""
import csv
import io
from pathlib import Path
import struct
import tempfile
import unittest
from scripts import convert_imc_sensors as imc
from scripts.convert_udp_payload_window import convert_path
from tests.test_agopen_steering import datagram, pcap
from tests.test_agopen_steering_window import write_capture
from tests.test_imc_sensors import frame
from tests.test_imc_xr620_depth import declaration, PROFILE
from tests.saved_can_window_helpers import common

FLOW = dict(udp_source_address="192.0.2.1", udp_destination_address="192.0.2.2",
            udp_source_port=6001, udp_destination_port=6002)


class QualifiedUdpTests(unittest.TestCase):
    def test_estimated_state_bytes_and_window_reuse_common(self):
        from tests.test_imc_estimated_state import packet
        payload = packet([.2, -.3, 8, -2, 3, 4, .1, .2, .3, 1, 2, 3, 4, 5, 6, .1, .2, .3, -1, 0])
        raw = pcap([datagram(payload, 6002)])
        output, report = imc.convert_pcap(raw, 6002, estimated_state=True)
        self.assertEqual(report["decoded_records"], 1)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fields = common(self, folder, output, PROFILE)["observations"][0]["fields"]
            self.assertEqual(fields["estimated_offset_north_m"], -2)
            self.assertEqual(fields["imc_payload_time_us"], 1700000000250000)
            source = folder / "window.pcap"
            write_capture(source, [(100, b"outside"), (200, datagram(payload, 6002))])
            selected, bound = convert_path(source, 6002, start_us=200, stop_us=300, estimated_state=True)
            fields = common(self, folder, selected, PROFILE)["observations"][0]["fields"]
            self.assertEqual(fields["pcap_packet_index"], 1)
            self.assertEqual(fields["record_time_us"], 200)
            self.assertEqual(fields["estimated_depth_disposition"], "WITHHELD_NEGATIVE_INVALID_ESTIMATE")
            self.assertEqual(bound["capture_window"]["outside_window_packets"], 1)
            for options in ({"estimated_state": 1}, {"estimated_state": True, "payload_format": "nmea-wind"}):
                with self.assertRaises(ValueError):
                    convert_path(source, 6002, start_us=200, stop_us=300, **options)
                with self.assertRaises(ValueError):
                    imc.convert_pcap(raw, 6002, **options)

    def test_two_captures_and_original_window_binding(self):
        for order, depth in (("<", 7.5), (">", -1.25)):
            payload = (frame(265, depth, order) + frame(260, struct.pack(order + "Bddd", 3, 0., 2., 99.), order)
                       + frame(65000, b"future", order))
            raw = pcap([datagram(payload, 6002)])
            default = imc.convert_pcap(raw, 6002)
            options = dict(water_velocity=True, writer_flow=FLOW)
            output, report = imc.convert_pcap(raw, 6002, writer_declaration=declaration(raw), **options)
            self.assertEqual(imc.convert_pcap(raw, 6002), default)
            self.assertEqual(report["xr620_writer_qualification"]["qualified_depth_rows"], 1)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                observations = common(self, root, output, PROFILE)["observations"]
                self.assertEqual(observations[0]["fields"]["depth_m"], depth)
                self.assertEqual(observations[1]["fields"]["reported_water_velocity_x_m_s"], 0)
                self.assertIsNone(observations[1]["fields"]["reported_water_velocity_z_m_s"])
                source = root / "capture.pcap"
                write_capture(source, [(100, b"x" * 1024**2)] * 17 + [(200, datagram(payload, 6002))])
                original = source.read_bytes()
                selected, selected_report = convert_path(source, 6002, start_us=200, stop_us=300,
                    writer_declaration=declaration(original), **options)
                (root / "window").mkdir()
                fields = common(self, root / "window", selected, PROFILE)["observations"][0]["fields"]
                self.assertEqual(fields["pcap_packet_index"], 17)
                self.assertEqual(fields["depth_source_sha256"], "sha256:" + declaration(original)["source_sha256"])
                self.assertEqual(fields["depth_m"], depth)
                self.assertEqual(selected_report["capture_window"]["outside_window_packets"], 17)
                self.assertNotEqual(selected_report["derived_capture_sha256"], selected_report["source_sha256"])

    def test_wrong_declaration_and_option_refusals(self):
        raw = pcap([datagram(frame(265, 1), 6002)])
        for flow in (None, dict(FLOW, udp_source_address="192.0.2.9"), dict(FLOW, udp_source_port=True)):
            with self.assertRaises(ValueError):
                imc.convert_pcap(raw, 6002, writer_declaration=declaration(raw), writer_flow=flow)
        for key, value in (("source_sha256", "0" * 64), ("source_entity", 8), ("driver", "Keller")):
            declared = declaration(raw); declared[key] = value
            with self.assertRaises(ValueError):
                imc.convert_pcap(raw, 6002, writer_declaration=declared, writer_flow=FLOW)
        with self.assertRaises(ValueError):
            imc.convert_pcap(raw, 6002, water_velocity=1)


if __name__ == "__main__":
    unittest.main()
