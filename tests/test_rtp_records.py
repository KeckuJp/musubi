"""Authored RTP-in-PCAP packet tests; no real or public sample capture is used."""
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
PROFILE = ROOT / "profiles/declared/rtp-recorded/profile.toml"
SCRIPT = ROOT / "scripts/convert_rtp_records.py"
PORT = 5004


def load():
    spec = importlib.util.spec_from_file_location("convert_rtp_records", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_rtp_records"] = module
    spec.loader.exec_module(module)
    return module



def checksum(data):
    data += b"\0" * (len(data) % 2)
    total = sum(struct.unpack(">" + "H" * (len(data) // 2), data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return 65535 - total


def datagram(payload, port=PORT, source_port=5002, source=(192, 0, 2, 1), destination=(192, 0, 2, 2)):
    """Ethernet + IPv4 + UDP around one authored RTP packet (the admitted saved-capture framing)."""
    addresses = bytes(source) + bytes(destination)
    udp = struct.pack(">HHHH", source_port, port, 8 + len(payload), 0) + payload
    check = checksum(addresses + struct.pack(">BBH", 0, 17, len(udp)) + udp) or 65535
    udp = udp[:6] + struct.pack(">H", check) + udp[8:]
    ip = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0) + addresses
    ip = ip[:10] + struct.pack(">H", checksum(ip)) + ip[12:]
    return b"\0" * 12 + b"\x08\x00" + ip + udp


def pcap(packets, order="<", linktype=1):
    header = struct.pack(order + "IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, linktype)
    return header + b"".join(
        struct.pack(order + "IIII", 100 + index, 1000 * index, len(packet), len(packet)) + packet
        for index, packet in enumerate(packets))


def pcap_batch(packets, order="<", linktype=1):
    """Same container as `pcap`, with a capture clock that stays inside one microsecond field."""
    header = struct.pack(order + "IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, linktype)
    return header + b"".join(
        struct.pack(order + "IIII", 100 + index // 1000, (index % 1000) * 1000, len(packet),
                    len(packet)) + packet
        for index, packet in enumerate(packets))


def rtp(sequence, timestamp, *, ssrc=0x11223344, payload_type=96, marker=False, payload=b"\xde\xad",
        csrc=(), extension=None, padding=0, version=2):
    first = (version << 6) | (0x20 if padding else 0) | (0x10 if extension is not None else 0) | len(csrc)
    header = bytes([first, (0x80 if marker else 0) | payload_type]) + struct.pack(
        ">HII", sequence, timestamp, ssrc)
    for source in csrc:
        header += struct.pack(">I", source)
    if extension is not None:
        profile, body = extension
        assert len(body) % 4 == 0
        header += struct.pack(">HH", profile, len(body) // 4) + body
    body = payload
    if padding:
        body = payload + b"\0" * (padding - 1) + bytes([padding])
    return header + body


class RtpRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dpkt") is None:
            raise unittest.SkipTest(
                "optional pinned PCAP decoder is exercised in the external-decoder CI environment")
        cls.rtp = load()

    def convert(self, packets, **kwargs):
        kwargs.setdefault("clock_rate", 90000)
        return self.rtp.convert_pcap_rtp(pcap([datagram(p) for p in packets]), PORT, **kwargs)

    def rows(self, packets, **kwargs):
        output, report = self.convert(packets, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report

    def test_interleaved_sources_are_ordered_separately_and_can_be_selected(self):
        packets = [rtp(10, 900, ssrc=0xAA), rtp(500, 4000, ssrc=0xBB), rtp(11, 3600, ssrc=0xAA),
                   rtp(501, 7600, ssrc=0xBB), rtp(12, 7200, ssrc=0xAA)]
        rows, report = self.rows(packets)
        self.assertEqual(report["retained_packets"], 5)
        self.assertEqual(report["packets"], 5)
        self.assertEqual(report["unselected_packets"], 0)
        self.assertEqual(len(report["sources"]), 2)
        first = [r for r in rows if r["rtp_ssrc"] == str(0xAA)]
        second = [r for r in rows if r["rtp_ssrc"] == str(0xBB)]
        self.assertEqual([r["rtp_order_state"] for r in first], ["first", "in_order", "in_order"])
        self.assertEqual([r["rtp_order_state"] for r in second], ["first", "in_order"])
        self.assertEqual([r["rtp_extended_sequence"] for r in first], ["10", "11", "12"])
        self.assertEqual([r["rtp_source_relative_us"] for r in first], ["0", "30000", "70000"])
        self.assertEqual([r["rtp_source_relative_us"] for r in second], ["0", "40000"])
        self.assertEqual([r["record_time_us"] for r in first], ["100000000", "102002000", "104004000"])
        self.assertTrue(all(r["pcap_clock_basis"] == "CAPTURE_REPORTED_NOT_AUTHENTICATED" for r in rows))

        selected, chosen = self.rows(packets, ssrc=0xBB)
        self.assertEqual([r["rtp_ssrc"] for r in selected], [str(0xBB)] * 2)
        self.assertEqual(chosen["unselected_packets"], 3)
        self.assertEqual(chosen["retained_packets"], 2)
        with self.assertRaises(ValueError):
            self.convert(packets, ssrc=0xCC)

    def test_wrap_duplicate_reorder_and_an_unsized_jump_stay_distinct(self):
        packets = [rtp(65534, 100), rtp(65535, 3700), rtp(0, 7300), rtp(0, 7300), rtp(1, 10900),
                   rtp(65533, 100), rtp(9000, 40000), rtp(9001, 43600)]
        rows, report = self.rows(packets)
        states = [r["rtp_order_state"] for r in rows]
        self.assertEqual(states, ["first", "in_order", "in_order", "duplicate", "in_order",
                                  "reorder", "discontinuity_unknown", "restart"])
        self.assertEqual([r["rtp_extended_sequence"] for r in rows[:3]], ["65534", "65535", "65536"])
        self.assertEqual(rows[2]["rtp_sequence_cycles"], "1")
        self.assertEqual(rows[2]["rtp_sequence_step"], "1")
        self.assertEqual(rows[3]["rtp_extended_sequence"], rows[2]["rtp_extended_sequence"])
        self.assertEqual(rows[3]["rtp_sequence_step"], "0")
        self.assertEqual(rows[5]["rtp_extended_sequence"], "65533")
        self.assertEqual(rows[5]["rtp_sequence_step"], "-4")
        self.assertEqual(rows[6]["rtp_extended_sequence"], "")
        self.assertEqual(rows[6]["rtp_sequence_step"], "")
        self.assertEqual(rows[6]["rtp_timestamp_state"], "discontinuity_unknown")
        self.assertEqual(rows[6]["rtp_source_relative_us"], "")
        self.assertEqual(rows[7]["rtp_sequence_epoch"], "1")
        self.assertEqual(rows[7]["rtp_extended_sequence"], "9001")
        self.assertEqual(rows[7]["rtp_source_relative_us"], "0", "a new epoch restarts the relative clock")
        source = report["sources"][0]
        self.assertEqual((source["duplicate"], source["reorder"], source["discontinuity_unknown"],
                          source["restarts"]), (1, 1, 2, 1))
        self.assertNotIn("lost", json.dumps(report).lower())

    def test_timestamp_wrap_inside_the_bound_and_an_unsafe_jump_left_unknown(self):
        near = (1 << 32) - 90000  # one second before the 32 bit wrap
        packets = [rtp(1, near), rtp(2, near + 90000 - 1), rtp(3, 89999), rtp(4, 89999),
                   rtp(5, 89999 + 90000 * 60)]
        rows, _ = self.rows(packets, max_forward_seconds=10)
        self.assertEqual([r["rtp_timestamp_state"] for r in rows],
                         ["first", "forward", "forward", "same_timestamp", "discontinuity_unknown"])
        self.assertEqual(int(rows[2]["rtp_extended_timestamp"]), (1 << 32) + 89999)
        self.assertEqual(rows[2]["rtp_source_relative_us"], "1999988")
        self.assertEqual(rows[3]["rtp_extended_timestamp"], rows[2]["rtp_extended_timestamp"])
        self.assertEqual(rows[4]["rtp_extended_timestamp"], "")
        self.assertEqual(rows[4]["rtp_source_relative_us"], "")
        self.assertEqual(rows[4]["rtp_order_state"], "in_order", "ordering is unaffected by the clock")
        wider, _ = self.rows(packets, max_forward_seconds=120)
        self.assertEqual(wider[4]["rtp_timestamp_state"], "forward")
        self.assertEqual(wider[4]["rtp_source_relative_us"], "61999988")

    def test_a_late_pre_wrap_timestamp_lands_in_the_previous_cycle_or_is_withheld(self):
        before, after, late = 0xFFFFFFF0, 0x10, 0xFFFFFFF8
        rows, _ = self.rows([rtp(1, before), rtp(2, after), rtp(3, late)])
        self.assertEqual([r["rtp_timestamp_state"] for r in rows], ["first", "forward", "backward"])
        self.assertEqual(int(rows[1]["rtp_extended_timestamp"]), (1 << 32) + after)
        self.assertEqual(int(rows[2]["rtp_extended_timestamp"]), late,
                         "the late pre-wrap tick stays in the cycle it came from")
        self.assertLess(int(rows[2]["rtp_extended_timestamp"]), int(rows[1]["rtp_extended_timestamp"]))
        self.assertEqual(rows[2]["rtp_source_relative_us"],
                         str((late - before) * 1_000_000 // 90000))
        pre_origin, _ = self.rows([rtp(1, 1000), rtp(2, 1500), rtp(3, 0xFFFFFF00)])
        self.assertEqual(pre_origin[2]["rtp_timestamp_state"], "backward")
        self.assertEqual(pre_origin[2]["rtp_extended_timestamp"], "")
        self.assertEqual(pre_origin[2]["rtp_source_relative_us"], "")
        self.assertEqual(pre_origin[2]["rtp_order_state"], "in_order", "ordering is unaffected")

    def test_an_ambiguous_declared_window_is_refused_not_resolved(self):
        packets = [rtp(1, 100), rtp(2, 3700)]
        for rate, seconds in ((536_870_912, 4), (1 << 28, 8), (1_000_000_000, 10)):
            with self.assertRaises(ValueError, msg=f"{rate}x{seconds} is ambiguous"):
                self.convert(packets, clock_rate=rate, max_forward_seconds=seconds)
        rows, report = self.rows(packets, clock_rate=(1 << 31) // 8, max_forward_seconds=4)
        self.assertEqual(report["max_forward_ticks"], 1 << 30)
        self.assertEqual(rows[1]["rtp_timestamp_state"], "forward")

    def test_a_repeat_below_the_highest_is_a_duplicate_and_differing_bytes_say_so(self):
        packets = [rtp(100, 100), rtp(102, 7300), rtp(101, 3700), rtp(101, 3700)]
        rows, report = self.rows(packets)
        self.assertEqual([r["rtp_order_state"] for r in rows],
                         ["first", "in_order", "reorder", "duplicate"])
        self.assertEqual(report["sources"][0]["reorder"], 1)
        self.assertEqual(report["sources"][0]["duplicate"], 1)
        differing = [rtp(100, 100), rtp(102, 7300), rtp(101, 3700),
                     rtp(101, 3700, payload=b"\x01\x02")]
        rows, report = self.rows(differing)
        self.assertEqual(rows[3]["rtp_order_state"], "repeated_sequence_new_payload")
        self.assertNotEqual(rows[3]["rtp_payload_sha256"], rows[2]["rtp_payload_sha256"])
        self.assertEqual(report["sources"][0]["duplicate"], 0)
        self.assertEqual(report["sources"][0]["repeated_sequence_new_payload"], 1)
        self.assertEqual(self.rtp.RECENT_SEQUENCES, 512)

    def test_a_sparse_wrap_reuses_a_number_without_becoming_a_duplicate(self):
        numbers = list(range(0, 65_001, 2_500)) + [0]
        self.assertLess(max(b - a for a, b in zip(numbers, numbers[1:-1])), self.rtp.MAX_DROPOUT)
        self.assertLess(len(numbers), self.rtp.RECENT_SEQUENCES,
                        "the first packet must still be in the cache: nothing may be evicted here")
        packets = [rtp(number, 100 + 3_600 * index) for index, number in enumerate(numbers)]
        rows, report = self.rows(packets)
        states = [r["rtp_order_state"] for r in rows]
        self.assertEqual(states, ["first"] + ["in_order"] * (len(numbers) - 1), states)
        self.assertEqual(rows[0]["rtp_extended_sequence"], "0")
        self.assertEqual(rows[0]["rtp_sequence_cycles"], "0")
        self.assertEqual(rows[-1]["rtp_extended_sequence"], "65536",
                         "the reused number is one cycle on, not the first packet again")
        self.assertEqual(rows[-1]["rtp_sequence_cycles"], "1")
        self.assertEqual(rows[-1]["rtp_sequence_step"], "536")
        self.assertEqual(rows[-2]["rtp_extended_sequence"], "65000")
        counts = report["sources"][0]
        self.assertEqual(counts["in_order"], len(numbers))
        self.assertEqual((counts["duplicate"], counts["repeated_sequence_new_payload"],
                          counts["reorder"], counts["discontinuity_unknown"]), (0, 0, 0, 0))
        self.assertEqual(report["retained_packets"], len(numbers))

        with_late_repeat = packets + [rtp(65_500, 100), rtp(65_500, 100)]
        rows, report = self.rows(with_late_repeat)
        self.assertEqual(rows[-2]["rtp_order_state"], "reorder")
        self.assertEqual(rows[-2]["rtp_extended_sequence"], "65500")
        self.assertEqual(rows[-2]["rtp_sequence_step"], "-36")
        self.assertEqual(rows[-1]["rtp_order_state"], "duplicate",
                         "the same extended value seen twice is a repeat, across the wrap too")
        self.assertEqual(rows[-1]["rtp_extended_sequence"], "65500")
        self.assertEqual(rows[-1]["rtp_sequence_step"], "-36")
        self.assertEqual(rows[-1]["rtp_payload_sha256"], rows[-2]["rtp_payload_sha256"])
        counts = report["sources"][0]
        self.assertEqual((counts["duplicate"], counts["reorder"]), (1, 1))
        self.assertEqual(counts["packets"], len(with_late_repeat))
        self.assertEqual(report["retained_packets"], len(with_late_repeat))
        self.assertEqual((self.rtp.MAX_DROPOUT, self.rtp.RECENT_SEQUENCES), (3_000, 512))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the sparse-wrap rows as Observations")
    def test_the_sparse_wrap_rows_reach_the_common_reader_with_their_extended_values(self):
        numbers = list(range(0, 65_001, 2_500)) + [0, 65_500, 65_500]
        packets = [rtp(number, 100 + 3_600 * index) for index, number in enumerate(numbers)]
        output, _ = self.convert(packets)
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], len(numbers))
        fields = [o["fields"] for o in common["observations"]]
        self.assertEqual(fields[0]["rtp_extended_sequence"], 0)
        self.assertEqual(fields[len(numbers) - 3]["rtp_extended_sequence"], 65536)
        self.assertEqual(fields[len(numbers) - 3]["rtp_order_state"], "in_order")
        self.assertEqual(fields[-1]["rtp_extended_sequence"], 65500)
        self.assertEqual(fields[-1]["rtp_order_state"], "duplicate")
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")

    def test_a_normal_arrival_between_a_jump_and_its_candidate_prevents_a_false_restart(self):
        packets = [rtp(10, 100), rtp(11, 3700), rtp(9000, 40000), rtp(12, 7300), rtp(9001, 43600)]
        rows, report = self.rows(packets)
        self.assertEqual([r["rtp_order_state"] for r in rows],
                         ["first", "in_order", "discontinuity_unknown", "in_order",
                          "discontinuity_unknown"])
        self.assertEqual(report["sources"][0]["restarts"], 0)
        self.assertTrue(all(r["rtp_sequence_epoch"] == "0" for r in rows))
        self.assertEqual(rows[4]["rtp_extended_sequence"], "")
        confirmed, report = self.rows([rtp(10, 100), rtp(11, 3700), rtp(9000, 40000),
                                       rtp(9001, 43600)])
        self.assertEqual(confirmed[3]["rtp_order_state"], "restart")
        self.assertEqual(report["sources"][0]["restarts"], 1)

    def test_a_mid_stream_payload_type_change_is_refused_unless_one_type_is_selected(self):
        mixed = [rtp(1, 100, payload_type=96), rtp(2, 3700, payload_type=97)]
        with self.assertRaises(ValueError):
            self.convert(mixed, clock_rate=90000)
        with self.assertRaises(ValueError):
            self.convert([rtp(1, 100, payload_type=96, ssrc=0xAA),
                          rtp(1, 100, payload_type=97, ssrc=0xBB)], clock_rate=90000)
        rows, report = self.rows(mixed, payload_type=96, clock_rate=90000)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["unsupported_packets"], 1)
        self.assertEqual(report["unsupported_payload_types"], [97])
        same, report = self.rows([rtp(1, 100, payload_type=96), rtp(2, 3700, payload_type=96)],
                                 clock_rate=90000)
        self.assertEqual(report["observed_payload_type"], 96)
        self.assertEqual(len(same), 2)

    def test_extension_csrc_and_padding_are_retained_or_refused(self):
        extension = (0xBEDE, b"\x01\x02\x03\x04\x05\x06\x07\x08")
        packets = [rtp(1, 100, csrc=(0xA1B2C3D4, 0x00000001), extension=extension, padding=3,
                       payload=b"\x01\x02\x03", marker=True)]
        rows, _ = self.rows(packets)
        row = rows[0]
        self.assertEqual(row["rtp_csrc_count"], "2")
        self.assertEqual(row["rtp_csrc_list"], "hex:a1b2c3d4|hex:00000001")
        self.assertEqual(row["rtp_extension_profile"], "hex:bede")
        self.assertEqual(row["rtp_extension_hex"], "hex:" + extension[1].hex(),
                         "unknown extension kept verbatim")
        self.assertEqual(row["rtp_padding_bytes"], "3")
        self.assertEqual(row["rtp_payload_bytes"], "3", "padding is removed from the payload length")
        self.assertEqual(row["rtp_marker"], "1")
        import hashlib
        self.assertEqual(row["rtp_payload_sha256"],
                         "sha256:" + hashlib.sha256(b"\x01\x02\x03").hexdigest())
        self.assertTrue(row["rtp_header_hex"].startswith("hex:b2"),
                        "prefixed hex: version 2, padding, extension, 2 CSRC")
        self.assertEqual(row["pcap_packet_index"], "0")

        bad = [
            rtp(1, 100, version=1),
            rtp(1, 100, csrc=(1, 2))[:16],
            rtp(1, 100, extension=(0xBEDE, b"\x01\x02\x03\x04"), payload=b"")[:-2],
            rtp(1, 100, padding=3)[:-1] + b"\xff",
            rtp(1, 100, padding=3)[:-1] + b"\x00",
            b"\x80\x60\x00",
        ]
        for index, packet in enumerate(bad):
            with self.assertRaises(ValueError, msg=f"malformed packet {index} must fail, not drop"):
                self.convert([packet])
        with self.assertRaises(ValueError):  # extension past the retained bound
            self.rtp.parse_rtp(rtp(1, 100, extension=(1, b"\0" * (self.rtp.MAX_EXTENSION_BYTES + 4))))

    def test_the_clock_rate_must_be_declared_or_fixed_by_rfc3551(self):
        packets = [rtp(1, 100, payload_type=96), rtp(2, 3700, payload_type=96)]
        with self.assertRaises(ValueError):  # dynamic payload type, nothing declared
            self.rtp.convert_pcap_rtp(pcap([datagram(p) for p in packets]), PORT,
                                      payload_type=96, rfc3551_static=True)
        for rate in (0, -1, 2**31, 1.5):
            with self.assertRaises(ValueError):
                self.convert(packets, clock_rate=rate)
        static = [rtp(1, 100, payload_type=26), rtp(2, 3700, payload_type=26)]
        rows, report = self.rows(static, payload_type=26, rfc3551_static=True, clock_rate=None)
        self.assertEqual(report["clock_rate_hz"], 90000)
        self.assertEqual(report["clock_rate_basis"], "RFC3551_STATIC_PAYLOAD_TYPE")
        self.assertEqual(rows[1]["rtp_source_relative_us"], "40000")
        with self.assertRaises(ValueError):  # a declared rate that contradicts the static table
            self.convert(static, payload_type=26, rfc3551_static=True, clock_rate=8000)
        declared, report = self.rows(packets, payload_type=96, clock_rate=48000)
        self.assertEqual(report["clock_rate_basis"], "DECLARED_BY_CALLER_NOT_OBSERVED")
        self.assertEqual(declared[1]["rtp_source_relative_us"], "75000")
        mixed, report = self.rows(packets + [rtp(3, 7300, payload_type=97)], payload_type=96,
                                  clock_rate=48000)
        self.assertEqual(len(mixed), 2)
        self.assertEqual(report["selected_packets"], 3)
        self.assertEqual(report["unsupported_packets"], 1)
        self.assertEqual(report["unsupported_payload_types"], [97])
        self.assertEqual(report["retained_packets"], 2)

    def test_unselected_traffic_is_counted_and_the_capture_framing_still_refuses_bad_input(self):
        good = datagram(rtp(1, 100))
        other_port = datagram(rtp(2, 3700), port=PORT + 1)
        output, report = self.rtp.convert_pcap_rtp(pcap([good, other_port, good]), PORT,
                                                   clock_rate=90000)
        self.assertEqual((report["packets"], report["selected_packets"], report["unselected_packets"],
                          report["retained_packets"]), (3, 2, 1, 2))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 2)
        data = pcap([good])
        for bad in (data[:-1], data + b"x", b"\0" * 24, data[:20]):
            with self.assertRaises(ValueError):
                self.rtp.convert_pcap_rtp(bad, PORT, clock_rate=90000)
        with self.assertRaises(ValueError):  # no RTP traffic on the selected port at all
            self.rtp.convert_pcap_rtp(pcap([other_port]), PORT, clock_rate=90000)

    def test_a_practical_batch_reaches_the_existing_common_reader(self):
        batch = [rtp(seq, 100 + 3600 * index, marker=index % 3 == 2)
                 for index, seq in enumerate(range(1000, 1400))]
        output, report = self.rows(batch, payload_type=96, clock_rate=90000)
        self.assertEqual(report["retained_packets"], 400)
        self.assertEqual(len(output), 400)
        self.assertEqual(report["sources"][0]["in_order"], 400)
        self.assertEqual(report["sources"][0]["discontinuity_unknown"], 0)
        self.assertEqual(output[-1]["rtp_source_relative_us"], str(399 * 3600 * 1000000 // 90000))
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "capture.pcap"
            source.write_bytes(pcap([datagram(p) for p in batch]))
            target = Path(work) / "out"
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target),
                                   "--udp-destination-port", str(PORT), "--payload-type", "96",
                                   "--clock-rate", "90000"], capture_output=True, text=True,
                                  check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             ["accounting.json", "observations.csv", "source.pcap"])
            self.assertEqual((target / "source.pcap").read_bytes(), source.read_bytes())
            accounting = json.loads((target / "accounting.json").read_text(encoding="utf-8"))
            self.assertEqual(accounting["retained_packets"], 400)
            self.assertIn("NOT_PROVEN_PACKET_OR_FRAME_LOSS", accounting["sequence_gap_basis"])
            self.assertIn("NOT_UTC", accounting["time_basis"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the rows as common Observations")
    def test_the_batch_reads_back_as_common_observations(self):
        batch = [rtp(seq, 100 + 3600 * index, marker=index % 3 == 2)
                 for index, seq in enumerate(range(1000, 1400))]
        output, _ = self.convert(batch, payload_type=96, clock_rate=90000)
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 400)
        self.assertEqual(common["platform_domain"], "Unknown")
        observation = common["observations"][0]
        self.assertEqual(observation["clock_basis"], "Unknown")
        self.assertEqual(observation["fields"]["rtp_sequence"], 1000)
        self.assertEqual(observation["fields"]["rtp_ssrc"], 0x11223344)
        self.assertEqual(observation["fields"]["rtp_source_relative_us"], 0)
        self.assertEqual(observation["t_ms"] * 1000, 100000000)
        self.assertEqual(common["observations"][-1]["fields"]["rtp_order_state"], "in_order")
        self.assertEqual(common["observations"][-1]["fields"]["rtp_extended_sequence"], 1399)

    def test_a_ten_thousand_packet_batch_stays_inside_the_bounds_and_can_be_segmented(self):
        count = 10_000
        batch = [rtp((1000 + index) % 65536, (100 + 3600 * index) % (1 << 32), marker=index % 3 == 2)
                 for index in range(count)]
        data = pcap_batch([datagram(p) for p in batch])
        output, report = self.rtp.convert_pcap_rtp(data, PORT, clock_rate=90000, payload_type=96)
        self.assertEqual(report["retained_packets"], count)
        self.assertEqual(report["sources"][0]["in_order"], count)
        self.assertEqual(report["sources"][0]["discontinuity_unknown"], 0)
        rows = output.count("\n") - 1
        self.assertEqual(rows, count)
        self.assertLess(len(output) // rows, 600)
        self.assertLess(len(output), self.rtp.OUTPUT_LIMIT)
        half = count // 2
        first_output, first_report = self.rtp.convert_pcap_rtp(
            data, PORT, clock_rate=90000, payload_type=96, packet_range=(0, half - 1))
        second_output, second_report = self.rtp.convert_pcap_rtp(
            data, PORT, clock_rate=90000, payload_type=96, packet_range=(half, count - 1))
        self.assertEqual(first_report["retained_packets"] + second_report["retained_packets"], count)
        self.assertEqual(first_report["out_of_range_packets"], half)
        self.assertEqual(second_report["packet_range"], [half, count - 1])
        second_rows = list(csv.DictReader(io.StringIO(second_output)))
        self.assertEqual(second_rows[0]["rtp_order_state"], "first",
                         "a segment starts its own ordering; continuity across parts is not claimed")
        self.assertEqual(second_rows[0]["rtp_source_relative_us"], "0")
        self.assertEqual(second_rows[0]["pcap_packet_index"], str(half))
        self.assertEqual(len(first_output.splitlines()) + len(second_output.splitlines()),
                         rows + 2, "each part keeps its own header row")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the large batch as common Observations")
    def test_the_ten_thousand_packet_batch_reads_back_as_common_observations(self):
        count = 10_000
        batch = [rtp((1000 + index) % 65536, (100 + 3600 * index) % (1 << 32)) for index in range(count)]
        output, _ = self.rtp.convert_pcap_rtp(pcap_batch([datagram(p) for p in batch]), PORT,
                                              clock_rate=90000, payload_type=96)
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], count)
        self.assertEqual(len(common["observations"]), count)
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(common["observations"][-1]["fields"]["rtp_sequence"], (1000 + count - 1) % 65536)
        self.assertEqual(common["observations"][-1]["fields"]["rtp_extended_sequence"], 1000 + count - 1)

    def test_the_cli_refuses_without_a_declared_clock_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "capture.pcap"
            source.write_bytes(pcap([datagram(rtp(1, 100))]))
            target = Path(work) / "existing"
            target.mkdir()
            (target / "evidence.txt").write_text("keep", encoding="utf-8")
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target),
                                   "--udp-destination-port", str(PORT), "--clock-rate", "90000"],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2)
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["evidence.txt"])
            undeclared = subprocess.run([sys.executable, str(SCRIPT), str(source), str(work + "/new"),
                                         "--udp-destination-port", str(PORT)],
                                        capture_output=True, text=True, check=False)
            self.assertEqual(undeclared.returncode, 2)
            self.assertIn("declared clock", undeclared.stderr)
            self.assertFalse((Path(work) / "new").exists())
            requested = []

            class CountingPath:
                def __init__(self, data):
                    self.data = data

                def open(self, mode="rb"):
                    class Handle(io.BytesIO):
                        def read(self, size=-1):
                            requested.append(size)
                            return super().read(size)

                    return Handle(self.data)

            keep = self.rtp.LIMIT
            try:
                self.rtp.LIMIT = 1_000
                with self.assertRaises(ValueError):
                    self.rtp.read_bounded(CountingPath(b"\0" * 50_000))
                self.assertEqual(requested, [1_001], "exactly one read of the bound plus one byte")
            finally:
                self.rtp.LIMIT = keep
            calls = []
            real_read = self.rtp.read_bounded
            self.rtp.read_bounded = lambda path, limit=None: (calls.append(path)
                                                             or real_read(path, limit))
            try:
                argv = sys.argv
                sys.argv = [str(SCRIPT), str(source), str(work + "/wired"),
                            "--udp-destination-port", str(PORT), "--clock-rate", "90000"]
                self.rtp.main()
                sys.argv = argv
            finally:
                self.rtp.read_bounded = real_read
            self.assertEqual([str(p) for p in calls], [str(source)],
                             "the command line reads through the bounded read")
            sparse = Path(work) / "sparse.pcap"
            with sparse.open("wb") as handle:
                handle.truncate(self.rtp.LIMIT + 1)
            self.assertEqual(sparse.stat().st_size, self.rtp.LIMIT + 1)
            oversized = subprocess.run([sys.executable, str(SCRIPT), str(sparse), str(work + "/big"),
                                        "--udp-destination-port", str(PORT), "--clock-rate", "90000"],
                                       capture_output=True, text=True, check=False)
            self.assertEqual(oversized.returncode, 2)
            self.assertFalse((Path(work) / "big").exists())


if __name__ == "__main__":
    unittest.main()
