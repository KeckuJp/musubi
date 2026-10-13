"""Independent authored receiver-report bytes to actual common output; no device proof."""
import csv
from decimal import Decimal
import io
import struct
import unittest

from tests import test_rtcp_sender_reports as fixtures


def block(peer, fraction, lost, sequence, jitter, lsr, delay):
    return (struct.pack(">I", peer) + bytes([fraction])
            + (lost & 0xffffff).to_bytes(3, "big")
            + struct.pack(">IIII", sequence, jitter, lsr, delay))


class ReceptionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.converter = fixtures.load()

    def convert(self, packets, **options):
        text, report = self.converter.convert(fixtures.capture(packets), fixtures.RTCP_PORT,
                                             with_reception_reports=True, **options)
        return list(csv.DictReader(io.StringIO(text))), report, text

    def test_signed_blocks_source_roles_and_delay_units_reach_common(self):
        first = block(0xaabbccdd, 255, -8388608, 0xffffffff, 90000, 0, 1)
        second = block(0x12345678, 0, 8388607, 0, 0, 0x11223344, 98304)
        sr = fixtures.sender_report(ssrc=9, blocks=first + second)
        rr = fixtures.rtcp(201, struct.pack(">I", 10) + first + first, count=2)
        empty = fixtures.rtcp(201, struct.pack(">I", 11), count=0)
        rows, report, text = self.convert([sr, rr, empty])
        self.assertEqual([row["rtcp_record_kind"] for row in rows],
                         ["SENDER_REPORT", "RECEPTION_REPORT_BLOCK", "RECEPTION_REPORT_BLOCK",
                          "RECEIVER_REPORT", "RECEPTION_REPORT_BLOCK", "RECEPTION_REPORT_BLOCK",
                          "RECEIVER_REPORT"])
        self.assertEqual(report["receiver_reports"], 2)
        self.assertEqual(report["reception_report_blocks_counted"], 4)
        self.assertEqual(report["reception_report_blocks_decoded"], 4)
        self.assertIs(report["reception_reports_selected"], True)
        for index, raw, reporter, peer, fraction, lost, delay, status in (
                (1, first, 9, 0xaabbccdd, "0.99609375", -8388608,
                 "0.0000152587890625", "LAST_SR_REFERENCE_UNRESOLVED_ZERO_FIELD"),
                (2, second, 9, 0x12345678, "0", 8388607, "1.5",
                 "LAST_SR_REFERENCE_REPORTED_NOT_ALIGNED"),
                (4, first, 10, 0xaabbccdd, "0.99609375", -8388608,
                 "0.0000152587890625", "LAST_SR_REFERENCE_UNRESOLVED_ZERO_FIELD")):
            row = rows[index]
            self.assertEqual(row["rtcp_ssrc"], str(reporter))
            self.assertEqual(row["reception_reportee_ssrc"], str(peer))
            self.assertEqual(row["reception_block_hex"], "hex:" + raw.hex())
            self.assertEqual(Decimal(row["reception_fraction_lost"]), Decimal(fraction))
            self.assertEqual(row["reception_cumulative_lost_reported"], str(lost))
            self.assertEqual(row["reception_dlsr_seconds_text"], delay)
            self.assertEqual(row["reception_last_sr_status"], status)
        self.assertEqual([rows[i]["reception_block_index"] for i in (1, 2, 4, 5)],
                         ["0", "1", "0", "1"])
        self.assertEqual(rows[1]["reception_jitter_rtp_ticks_reported"], "90000")
        self.assertEqual(rows[1]["reception_extended_highest_sequence_reported"], "4294967295")
        self.assertEqual(rows[6]["reception_blocks_status"], "NO_RECEPTION_BLOCKS_REPORTED")
        self.assertEqual(rows[6]["reception_fraction_lost"], "")
        common = fixtures.read_common(self, text, 7)
        self.assertEqual(common["platform_domain"], "Unknown")
        for observation in common["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"])
        values = common["observations"][1]["fields"]
        self.assertEqual(values["reception_cumulative_lost_reported"], -8388608)
        self.assertEqual(values["reception_fraction_lost"], 255 / 256)
        self.assertEqual(values["reception_jitter_rtp_ticks_reported"], 90000)
        self.assertEqual(values["reception_dlsr_seconds_text"], 1 / 65536)
        self.assertEqual(values["reception_block_hex"], "hex:" + first.hex())

    def test_zero_block_is_not_absence_extensions_retained_and_default_unchanged(self):
        zero = block(88, 0, 0, 0, 0, 0, 0)
        packet = fixtures.rtcp(201, struct.pack(">I", 77) + zero + b"EXT!", count=1, padding=4)
        rows, report, text = self.convert([packet])
        self.assertEqual(rows[0]["reception_blocks_status"], "RECEPTION_BLOCKS_REPORTED")
        self.assertEqual(rows[0]["reception_extension_hex"], "hex:" + b"EXT!".hex())
        self.assertEqual(report["receiver_report_extension_bytes"], 4)
        self.assertEqual(rows[1]["reception_fraction_lost_raw"], "0")
        self.assertEqual(rows[1]["reception_cumulative_lost_reported"], "0")
        self.assertEqual(rows[1]["reception_block_hex"], "hex:" + zero.hex())
        common = fixtures.read_common(self, text, 2)
        self.assertEqual(common["observations"][1]["fields"]["reception_dlsr_ticks_reported"], 0)
        raw = fixtures.capture([fixtures.sender_report(blocks=zero)])
        legacy = self.converter.convert(raw, fixtures.RTCP_PORT)
        explicit = self.converter.convert(raw, fixtures.RTCP_PORT, with_reception_reports=False)
        self.assertEqual(legacy, explicit)
        self.assertNotIn("reception_reportee_ssrc", legacy[0].splitlines()[0])

    def test_truncated_block_and_padding_are_not_decoded_as_measurements(self):
        for invalid in (
                fixtures.rtcp(201, b""),
                fixtures.rtcp(201, struct.pack(">I", 9), count=1),
                fixtures.rtcp(201, struct.pack(">I", 9), count=1, padding=24),
                fixtures.rtcp(201, struct.pack(">I", 9) + b"\0" * 20, count=1)):
            with self.subTest(size=len(invalid)), self.assertRaises(ValueError):
                self.convert([invalid])
        with self.assertRaises(ValueError):
            self.converter.convert(fixtures.capture([fixtures.sender_report()]),
                                   fixtures.RTCP_PORT, with_reception_reports=1)


if __name__ == "__main__":
    unittest.main()
