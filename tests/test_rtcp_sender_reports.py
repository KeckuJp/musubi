"""Authored RTCP packets inside the capture framing the adopted RTP tests already build.

Every byte here is authored from the pinned RFC 3550 (Standards Track, July 2003). No real
stream, device, camera, goggle or network is involved: real records 0. The Ethernet/IPv4/UDP
framing and the PCAP container come from `tests.test_rtp_records` so there is one authored
capture builder, not a second one.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "profiles/declared/rtcp-sender-reports/profile.toml"
SCRIPT = ROOT / "scripts/convert_rtcp_sender_reports.py"
from tests.test_rtp_records import datagram, pcap  # noqa: E402
RTCP_PORT = 5005


def load():
    spec = importlib.util.spec_from_file_location("convert_rtcp_sender_reports", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_rtcp_sender_reports"] = module
    spec.loader.exec_module(module)
    return module


def rtcp(packet_type, body, *, count=0, version=2, padding=0, words=None):
    """One individual RTCP packet: the common header of the pin, then the given body."""
    if padding:
        body = body + b"\0" * (padding - 1) + bytes([padding])
    length = (4 + len(body)) // 4 - 1 if words is None else words
    first = (version << 6) | (0x20 if padding else 0) | count
    return struct.pack(">BBH", first, packet_type, length) + body


def sender_report(*, ssrc=0x11223344, seconds=0xE7000000, fraction=0x80000000, media=900000,
                  packets=150, octets=192000, blocks=b"", count=None, **kwargs):
    """A sender report: SSRC, then the 20 octets of sender information, then any blocks."""
    body = struct.pack(">IIIIII", ssrc, seconds, fraction, media, packets, octets) + blocks
    return rtcp(200, body, count=len(blocks) // 24 if count is None else count, **kwargs)


def report_block(ssrc=0x55667788):
    """One 24-octet reception report block, which this path counts and never decodes."""
    return struct.pack(">IIIIII", ssrc, 0x00000005, 1234, 77, 0xE7000000, 6000)


def chunk(ssrc, items):
    """One SDES chunk: identifier, items, then the null terminator padded to a 32-bit boundary."""
    body = struct.pack(">I", ssrc)
    for item_type, text in items:
        body += bytes([item_type, len(text)]) + text
    body += b"\0"
    while len(body) % 4:
        body += b"\0"
    return body


def source_description(chunks):
    return rtcp(202, b"".join(chunks), count=len(chunks))


def capture(packets, port=RTCP_PORT):
    """One authored PCAP holding one datagram per given RTCP payload."""
    return pcap([datagram(payload, port=port) for payload in packets])


def read_common(case, csv_text, expected_rows):
    """Run the configured common reader, or skip explicitly - never bypass silently."""
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    if not reader:
        case.skipTest("MUSUBI_TELEMETRY_READER not configured: actual common reader check skipped")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "observations.csv"
        path.write_text(csv_text)
        finished = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                  capture_output=True)
    case.assertEqual(finished.returncode, 0,
                     f"configured common reader failed: {finished.stderr[:400]!r}")
    common = json.loads(finished.stdout)
    case.assertEqual(common["main_rows"], expected_rows)
    return common


class RtcpSenderReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dpkt") is None:
            raise unittest.SkipTest(
                "optional pinned PCAP decoder is exercised in the external-decoder CI environment")
        cls.rtcp = load()

    def rows(self, packets, port=RTCP_PORT, **kwargs):
        text, report = self.rtcp.convert(capture(packets), port, **kwargs)
        return list(csv.DictReader(io.StringIO(text))), report, text

    def test_a_sender_report_carries_the_pairing_and_reaches_common_output(self):
        first = sender_report(seconds=0xE7000000, fraction=0x80000000, media=900000,
                              packets=150, octets=192000)
        second = sender_report(seconds=0xE7000005, fraction=0x40000000, media=1350000,
                               packets=300, octets=384000)
        names = source_description([chunk(0x11223344, [(1, b"authored@fixture.invalid")])])
        rows, report, text = self.rows([first, second, names])
        self.assertEqual([row["rtcp_record_kind"] for row in rows],
                         ["SENDER_REPORT", "SENDER_REPORT", "SOURCE_DESCRIPTION_CNAME"])
        self.assertEqual(rows[0]["sr_ntp_seconds_reported"], str(0xE7000000))
        self.assertEqual(rows[0]["sr_ntp_fraction_reported"], str(0x80000000))
        self.assertEqual(rows[0]["sr_ntp_seconds_text"], "3875536896.5")
        self.assertEqual(rows[1]["sr_ntp_seconds_text"], "3875536901.25")
        self.assertEqual(rows[0]["sr_rtp_timestamp_reported"], "900000")
        self.assertEqual(rows[0]["sr_ntp_form"],
                         "FORM_UNRESOLVED_THE_HIGH_BIT_ALONE_PROVES_NEITHER_ABSOLUTE_NOR_RELATIVE")
        self.assertEqual(rows[0]["sr_media_sender_clock_pairing"],
                         "SENDER_DECLARED_PAIRING_OF_ITS_OWN_RTP_CLOCK_AND_ITS_REPORTED_TIME")
        self.assertIn("NOT_LATENCY", rows[0]["sr_pairing_basis"])
        self.assertIn("NEVER_AN_ARRIVAL_TIME", rows[0]["sr_pairing_basis"])
        self.assertIn("NO_UTC", rows[0]["sr_ntp_basis"])
        self.assertEqual((rows[0]["sr_sender_packet_count_reported"],
                          rows[0]["sr_sender_octet_count_reported"]), ("150", "192000"))
        self.assertIn("NEVER_A_RATE", rows[0]["sr_sender_count_basis"])
        self.assertEqual(rows[2]["sdes_cname_hex"], "hex:" + b"authored@fixture.invalid".hex())
        self.assertEqual({row["rtcp_ssrc"] for row in rows}, {str(0x11223344)})
        self.assertEqual(report["sender_reports"], 2)
        self.assertEqual(report["canonical_names"], 1)
        self.assertEqual(report["packets_by_type"], {"200": 2, "202": 1})
        self.assertEqual(report["sender_report_sources"], [0x11223344])
        common = read_common(self, text, 3)
        self.assertEqual(common["observations"][0]["fields"]["sr_rtp_timestamp_reported"], 900000)

    def test_the_high_bit_alone_decides_no_form_on_either_side(self):
        absent = sender_report(seconds=0, fraction=0, media=0)
        clear = sender_report(seconds=0x00000064, fraction=0x40000000, media=4500)
        set_bit = sender_report(seconds=0xE7000000, fraction=0x80000000, media=900000)
        rows, report, _ = self.rows([absent, clear, set_bit])
        unresolved = "FORM_UNRESOLVED_THE_HIGH_BIT_ALONE_PROVES_NEITHER_ABSOLUTE_NOR_RELATIVE"
        self.assertEqual(rows[0]["sr_ntp_form"], "NO_TIME_DECLARED")
        self.assertEqual(rows[0]["sr_ntp_seconds_text"], "")
        self.assertEqual(rows[0]["sr_media_sender_clock_pairing"],
                         "NO_PAIRING_DECLARED_THE_SENDER_WROTE_NO_TIME")
        self.assertEqual([rows[1]["sr_ntp_form"], rows[2]["sr_ntp_form"]],
                         [unresolved, unresolved])
        self.assertEqual([rows[1]["sr_ntp_high_bit_reported"],
                          rows[2]["sr_ntp_high_bit_reported"]], ["0", "1"])
        self.assertEqual(rows[1]["sr_ntp_seconds_text"], "100.25")
        self.assertEqual(rows[2]["sr_ntp_seconds_text"], "3875536896.5")
        self.assertIn("UNTIL_THE_YEAR_2036", rows[1]["sr_ntp_form_basis"])
        self.assertIn("NEVER_TAKES_ONE_FROM_THE_CAPTURE_CLOCK", rows[1]["sr_ntp_form_basis"])
        self.assertIn("NO_ERA_RESOLVED", rows[1]["sr_ntp_basis"])
        self.assertIsNone(report["declared_sender_time_form"])
        self.assertEqual(rows[2]["sr_media_sender_clock_pairing"],
                         "SENDER_DECLARED_PAIRING_OF_ITS_OWN_RTP_CLOCK_AND_ITS_REPORTED_TIME")

    def test_a_caller_may_declare_the_form_and_it_is_labelled_a_declaration(self):
        rows, report, _ = self.rows([sender_report(seconds=0x00000064, fraction=0)],
                                    declared_form="RELATIVE")
        self.assertEqual(rows[0]["sr_ntp_form"],
                         "CALLER_DECLARED_RELATIVE_FORM_NOT_PROVEN_BY_THE_HIGH_BIT")
        self.assertEqual(rows[0]["sr_ntp_high_bit_reported"], "0")
        self.assertEqual(report["declared_sender_time_form"], "RELATIVE")
        rows, _, _ = self.rows([sender_report()], declared_form="ABSOLUTE")
        self.assertEqual(rows[0]["sr_ntp_form"],
                         "CALLER_DECLARED_ABSOLUTE_FORM_NOT_PROVEN_BY_THE_HIGH_BIT")
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report()], declared_form="UTC")
        self.assertIn("must be one the pinned source names", str(raised.exception))

    def test_reception_report_blocks_are_counted_and_never_decoded(self):
        rows, report, text = self.rows([sender_report(blocks=report_block() + report_block(
            ssrc=0x99aabbcc))])
        self.assertEqual(rows[0]["sr_reception_report_blocks_declared"], "2")
        self.assertEqual(report["reception_report_blocks_counted"], 2)
        self.assertIn("are_counted_and_not_decoded_here",
                      rows[0]["sr_reception_report_basis"].lower())
        for absent in ("jitter", "1234", "6000", str(0x55667788)):
            self.assertNotIn(absent, text)

    def test_every_other_packet_type_is_counted_under_its_own_number(self):
        others = [rtcp(201, struct.pack(">I", 0x11223344), count=0),
                  rtcp(203, struct.pack(">I", 0x11223344), count=1),
                  rtcp(204, struct.pack(">I", 0x11223344) + b"NAME" + b"\0\0\0\0", count=0),
                  rtcp(208, struct.pack(">I", 0x11223344), count=0)]
        rows, report, _ = self.rows([sender_report()] + others)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["packets_by_type"],
                         {"200": 1, "201": 1, "203": 1, "204": 1, "208": 1})
        self.assertEqual(report["rtcp_packets_read"], 5)

    def test_a_compound_datagram_keeps_every_packet_in_saved_order(self):
        compound = sender_report() + source_description(
            [chunk(0x11223344, [(1, b"first@fixture.invalid")])])
        rows, report, text = self.rows([compound])
        self.assertEqual([row["rtcp_packet_index_in_datagram"] for row in rows], ["0", "1"])
        self.assertEqual({row["record_time_us"] for row in rows}, {"100000000"})
        self.assertEqual(report["datagrams_selected"], 1)
        read_common(self, text, 2)

    def test_only_the_canonical_name_item_is_interpreted(self):
        named = source_description([chunk(0x11223344, [(2, b"NAME ITEM"), (1, b"cname@fixture"),
                                                       (6, b"tool 1.0")])])
        rows, report, text = self.rows([sender_report(), named])
        self.assertEqual(rows[1]["sdes_cname_hex"], "hex:" + b"cname@fixture".hex())
        self.assertEqual(report["canonical_names"], 1)
        self.assertNotIn(b"NAME ITEM".hex(), text)
        self.assertNotIn(b"tool 1.0".hex(), text)

    def test_a_chunk_without_a_canonical_name_adds_no_identity_row(self):
        rows, report, _ = self.rows([sender_report(),
                                     source_description([chunk(0x11223344, [(6, b"tool 1.0")])])])
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["canonical_names"], 0)
        self.assertEqual(report["packets_by_type"]["202"], 1)

    def test_a_declared_length_past_the_datagram_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(words=99)])
        self.assertIn("length past the end of its datagram", str(raised.exception))

    def test_a_sender_report_shorter_than_its_sender_information_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            self.rows([rtcp(200, struct.pack(">III", 0x11223344, 0xE7000000, 0x80000000))])
        self.assertIn("shorter than its own sender information", str(raised.exception))

    def test_more_declared_blocks_than_the_declared_length_can_hold_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(count=3)])
        self.assertIn("more reception report blocks than its declared length", str(raised.exception))

    def test_padding_cannot_stand_in_for_a_field_the_sender_did_not_write(self):
        short = struct.pack(">IIIII", 0x11223344, 0xE7000000, 0x80000000, 900000, 150)
        with self.assertRaises(ValueError) as raised:
            self.rows([rtcp(200, short, padding=4)])
        self.assertIn("shorter than its own sender information", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(count=1, padding=24)])
        self.assertIn("more reception report blocks than its declared length",
                      str(raised.exception))
        body = struct.pack(">I", 0x11223344) + bytes([1, 4]) + b"abcd" + b"\0\0"
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(), rtcp(202, body, count=2, padding=4)])
        self.assertIn("ends inside a chunk identifier", str(raised.exception))

    def test_a_padded_packet_keeps_its_saved_bytes_in_the_digest(self):
        import hashlib
        padded = sender_report(padding=8)
        rows, report, _ = self.rows([padded])
        self.assertEqual(rows[0]["rtcp_packet_sha256"],
                         "sha256:" + hashlib.sha256(padded).hexdigest())
        self.assertEqual(rows[0]["rtcp_padding_bytes_excluded"], "8")
        self.assertEqual(rows[0]["rtcp_padding_declared"], "1")
        self.assertEqual(report["padding_bytes_excluded"], 8)
        self.assertEqual(rows[0]["sr_rtp_timestamp_reported"], "900000")

    def test_a_padding_count_off_the_32_bit_boundary_is_refused(self):
        body = struct.pack(">IIIIII", 0x11223344, 0xE7000000, 0x80000000, 900000, 150, 192000)
        broken = struct.pack(">BBH", (2 << 6) | 0x20, 200,
                             (4 + len(body) + 4) // 4 - 1) + body + b"\0\0\0\x02"
        with self.assertRaises(ValueError) as raised:
            self.rows([broken])
        self.assertIn("multiple of four", str(raised.exception))

    def test_uninterpreted_items_and_extension_bytes_are_counted_by_kind(self):
        named = source_description([chunk(0x11223344, [(2, b"NAME ITEM"), (1, b"cname@fixture"),
                                                       (6, b"tool 1.0"), (6, b"tool 2.0")]),
                                    chunk(0x99aabbcc, [(3, b"e@mail")])])
        body = struct.pack(">IIIIII", 0x11223344, 0xE7000000, 0x80000000, 900000, 150, 192000)
        extended = rtcp(200, body + b"\xaa\xbb\xcc\xdd")
        rows, report, text = self.rows([extended, named])
        self.assertEqual(rows[0]["sr_profile_extension_bytes"], "4")
        self.assertEqual(report["sender_report_extension_bytes"], 4)
        self.assertEqual(report["uninterpreted_sdes_items_by_type"], {"2": 1, "6": 2, "3": 1})
        self.assertEqual(report["sdes_chunks_without_canonical_name"], 1)
        self.assertEqual(rows[1]["sdes_uninterpreted_items_in_chunk"], "3")
        self.assertEqual(rows[1]["sdes_unconsumed_trailing_bytes"], "0")
        for absent in (b"NAME ITEM".hex(), b"tool 1.0".hex(), b"e@mail".hex(), "aabbccdd"):
            self.assertNotIn(absent, text)

    def test_bytes_after_the_declared_chunks_are_counted_not_ignored(self):
        trailing = struct.pack(">I", 0x11223344) + bytes([1, 6]) + b"cname!" + b"\0" * 8
        rows, report, _ = self.rows([sender_report(), rtcp(202, trailing, count=1)])
        self.assertEqual(report["sdes_unconsumed_trailing_bytes"], 4)
        self.assertEqual(rows[1]["sdes_unconsumed_trailing_bytes"], "4")
        self.assertEqual(rows[1]["sdes_cname_hex"], "hex:" + b"cname!".hex())

    def test_an_impossible_padding_count_is_refused(self):
        body = struct.pack(">IIIIII", 0x11223344, 0xE7000000, 0x80000000, 900000, 150, 192000)
        broken = struct.pack(">BBH", (2 << 6) | 0x20, 200, (4 + len(body) + 4) // 4 - 1) + body + b"\0\0\0\0"
        with self.assertRaises(ValueError) as raised:
            self.rows([broken])
        self.assertIn("padding count it cannot hold", str(raised.exception))

    def test_a_version_this_path_does_not_read_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(version=1)])
        self.assertIn("version this path does not read", str(raised.exception))

    def test_an_unterminated_or_overrunning_source_description_is_refused(self):
        body = struct.pack(">I", 0x11223344) + bytes([1, 6]) + b"abcdef"
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(), rtcp(202, body, count=1)])
        self.assertIn("not terminated", str(raised.exception))
        body = struct.pack(">I", 0x11223344) + bytes([1, 200]) + b"abcd\0\0"
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(), rtcp(202, body, count=1)])
        self.assertIn("runs past its packet", str(raised.exception))

    def test_a_repeated_canonical_name_in_one_chunk_is_refused(self):
        named = source_description([chunk(0x11223344, [(1, b"one@fixture"), (1, b"two@fixture")])])
        with self.assertRaises(ValueError) as raised:
            self.rows([sender_report(), named])
        self.assertIn("repeats its canonical name", str(raised.exception))

    def test_a_capture_with_no_rtcp_on_the_declared_port_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            self.rtcp.convert(capture([sender_report()], port=5004), RTCP_PORT)
        self.assertIn("no RTCP packet on the declared port", str(raised.exception))

    def test_the_port_is_declared_and_never_inferred(self):
        with self.assertRaises(ValueError) as raised:
            self.rtcp.convert(capture([sender_report()]), 0)
        self.assertIn("explicit UDP destination port required", str(raised.exception))
        self.assertIn("never inferred", SCRIPT.read_text())

    def test_the_command_line_writes_the_csv_and_the_report_beside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "authored.pcap"
            source.write_bytes(capture([sender_report()]))
            output = Path(directory) / "converted"
            self.assertEqual(self.rtcp.main([str(source), str(output),
                                             "--rtcp-port", str(RTCP_PORT)]), 0)
            rows = list(csv.DictReader(io.StringIO((output / "observations.csv").read_text())))
            report = json.loads((output / "report.json").read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["reference"], self.rtcp.REFERENCE)
        self.assertTrue(report["source_sha256"].startswith("sha256:"))
        self.assertIn("NO_UTC", report["time_basis"])


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
