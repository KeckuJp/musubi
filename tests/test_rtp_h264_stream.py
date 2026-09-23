"""Authored RTP/H.264 mode 1 packets through the qualified offline decoder; no real capture is used.

The videos are generated here by the locally qualified ffmpeg; nothing is downloaded, no capture or
vendor sample is replayed, and every subprocess is a fixed argv over a local file (no URL, no protocol
other than file).
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BRIDGE = ROOT / "scripts/convert_rtp_h264_stream.py"
VIDEO_CONVERTER = ROOT / "scripts/convert_video_frames.py"
VIDEO_PROFILE = ROOT / "profiles/declared/video-frame-export/profile.toml"
PORT = 5006
PAYLOAD_TYPE = 96
CLOCK = 90000
QUALIFIED_VERSION = "8.1"

from tests.test_rtp_records import datagram, pcap_batch, rtp  # authored framing, reused as-is


def load():
    spec = importlib.util.spec_from_file_location("convert_rtp_h264_stream", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_rtp_h264_stream"] = module
    spec.loader.exec_module(module)
    return module


def tool(name):
    """The locally qualified ffmpeg/ffprobe, or None. Never downloaded, never installed here."""
    path = os.environ.get(f"MUSUBI_{name.upper()}") or shutil.which(name)
    if not path:
        return None
    banner = subprocess.run([path, "-version"], capture_output=True, text=True, check=False).stdout
    return path if banner.startswith(f"{name} version {QUALIFIED_VERSION}") else None


FFMPEG, FFPROBE = tool("ffmpeg"), tool("ffprobe")
DECODER_REASON = (f"locally qualified ffmpeg/ffprobe {QUALIFIED_VERSION} not available: this is a "
                  "dependency hold, not a pass")



def author_video(directory, name, *, size="64x48", rate=5, duration=1.2, keyint=3, reordered=False):
    """Generate a small H.264 Annex B elementary stream with the qualified encoder (fixed argv).

    `reordered=True` asks the encoder for B pictures, so decode order differs from output order: the
    stream a constant-rate mux may *not* be claimed to time correctly.
    """
    target = Path(directory) / f"{name}.h264"
    encoder = ["-c:v", "libx264", "-preset", "medium", "-bf", "2", "-x264-params",
               f"keyint={keyint}:bframes=2"] if reordered else [
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-x264-params", f"keyint={keyint}"]
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc=size={size}:rate={rate}:duration={duration}"] + encoder
                   + ["-bsf:v", "h264_mp4toannexb", "-f", "h264", str(target)],
                   check=True, capture_output=True)
    return target.read_bytes()


def annex_b_units(stream):
    """Split an Annex B stream into NAL units (the authored side of the round trip)."""
    starts = [m.start() for m in re.finditer(b"\x00\x00\x01", stream)]
    units = []
    for index, start in enumerate(starts):
        begin = start + 3
        end = starts[index + 1] if index + 1 < len(starts) else len(stream)
        while end > begin + 1 and stream[end - 1] == 0:
            end -= 1
        units.append(stream[begin:end])
    return units


def access_units(units):
    """Group NAL units into access units: parameter sets ride with the slice that follows them."""
    grouped, pending = [], []
    for unit in units:
        pending.append(unit)
        if unit[0] & 0x1F in (1, 5):
            grouped.append(pending)
            pending = []
    if pending:
        grouped.append(pending)
    return grouped


def packetize(grouped, *, layout, mtu=200, first_sequence=1000, ssrc=0x0A0B0C0D):
    """Two distinct mode 1 layouts over the same access units (RFC 6184 sections 5.6, 5.7.1, 5.8)."""
    packets, sequence = [], first_sequence
    for index, unit in enumerate(grouped):
        timestamp = (index * (CLOCK // 5)) % (1 << 32)
        emitted = []
        if layout == "stap-a":
            sets = [nal for nal in unit if nal[0] & 0x1F in (7, 8)]
            rest = [nal for nal in unit if nal[0] & 0x1F not in (7, 8)]
            if sets:
                body = bytes([0x18 | max(nal[0] & 0x60 for nal in sets)])
                for nal in sets:
                    body += struct.pack(">H", len(nal)) + nal
                emitted.append(body)
            emitted.extend(rest)
        elif layout == "fu-a":
            for nal in unit:
                if len(nal) <= mtu:
                    emitted.append(nal)
                    continue
                header, body = nal[0], nal[1:]
                chunks = [body[at:at + mtu] for at in range(0, len(body), mtu)]
                for position, chunk in enumerate(chunks):
                    start = 0x80 if position == 0 else 0
                    end = 0x40 if position == len(chunks) - 1 else 0
                    emitted.append(bytes([(header & 0xE0) | 28,
                                          start | end | (header & 0x1F)]) + chunk)
        else:
            raise AssertionError("unknown layout")
        for position, body in enumerate(emitted):
            packets.append(rtp(sequence % (1 << 16), timestamp, ssrc=ssrc,
                               payload_type=PAYLOAD_TYPE, payload=body,
                               marker=position == len(emitted) - 1))
            sequence += 1
    return packets


def capture_of(packets):
    return pcap_batch([datagram(packet, port=PORT) for packet in packets])


class RtpH264BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dpkt") is None:
            raise unittest.SkipTest(
                "optional pinned PCAP decoder is exercised in the external-decoder CI environment")
        cls.bridge = load()

    def rebuild(self, packets, **kwargs):
        kwargs.setdefault("payload_type", PAYLOAD_TYPE)
        return self.bridge.depacketize(capture_of(packets), PORT, **kwargs)


    def test_single_nal_and_stap_a_rebuild_exactly_what_was_packetized(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(60))
        stap = bytes([0x18 | 0x60]) + struct.pack(">H", len(sps)) + sps + struct.pack(">H", len(pps)) + pps
        packets = [rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=stap),
                   rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)]
        stream, report = self.rebuild(packets)
        self.assertEqual(stream, b"".join(b"\x00\x00\x00\x01" + nal for nal in (sps, pps, slice_nal)))
        self.assertEqual(report["access_unit_count"], 1)
        unit = report["access_units"][0]
        self.assertEqual(unit["nal_types"], [7, 8, 5])
        self.assertEqual(unit["nal_sizes"], [len(sps), len(pps), len(slice_nal)])
        self.assertEqual(unit["rtp_sequences"], [10, 11])
        self.assertEqual(unit["packet_indices"], [0, 1])
        self.assertEqual(unit["marker_packet_index"], 1)
        self.assertEqual(unit["elementary_offset"], 0)
        self.assertEqual(unit["elementary_bytes"], len(stream))
        self.assertEqual(report["parameter_sets_before_first_slice"], [7, 8])
        self.assertEqual(report["nal_type_counts"], {"7": 1, "8": 1, "5": 1})
        self.assertEqual(report["retained_packets"], 2)
        self.assertIn("NOT_UTC", report["rtp_time_basis"])

    def test_fu_a_chain_rebuilds_the_original_header_and_body(self):
        nal = bytes([0x65]) + bytes(range(120))
        chunks = [nal[1:41], nal[41:81], nal[81:]]
        packets = [rtp(20, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x85]) + chunks[0]),
                   rtp(21, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x05]) + chunks[1]),
                   rtp(22, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x45]) + chunks[2],
                       marker=True)]
        packets = [rtp(19, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x67, 0x42, 0x00, 0x0A]))] + \
                  [rtp(p_index + 20, 0, payload_type=PAYLOAD_TYPE,
                       payload=packets[p_index]["payload"] if isinstance(packets[p_index], dict)
                       else packets[p_index][12:], marker=p_index == 2) for p_index in range(3)]
        packets = [rtp(19, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x67, 0x42, 0x00, 0x0A])),
                   rtp(20, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x68, 0xCE, 0x0F, 0x19])),
                   rtp(21, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x85]) + chunks[0]),
                   rtp(22, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x05]) + chunks[1]),
                   rtp(23, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x45]) + chunks[2],
                       marker=True)]
        stream, report = self.rebuild(packets)
        self.assertEqual(stream.count(b"\x00\x00\x00\x01"), 3)
        self.assertTrue(stream.endswith(nal), "the fragmented NAL is rebuilt byte for byte")
        unit = report["access_units"][0]
        self.assertEqual(unit["nal_types"], [7, 8, 5])
        self.assertEqual(unit["nal_sizes"][-1], len(nal))
        self.assertEqual(len(unit["rtp_sequences"]), 5)

    def test_every_declared_refusal_is_enforced(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))
        head = [rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps)]
        fragment = bytes([0x7C, 0x85]) + slice_nal[1:20]
        cases = {
            "stap-b": head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x19, 0, 0]))],
            "mtap16": head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x1A, 0, 0]))],
            "fu-b": head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x1D, 0x85, 1]))],
            "reserved": head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x1F, 0]))],
            "continuation_without_start": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x05]) + b"\x01\x02")],
            "start_and_end": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0xC5]) + b"\x01")],
            "reserved_bit": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0xA5]) + b"\x01")],
            "type_change_mid_chain": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=fragment),
                rtp(13, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x7C, 0x01]) + b"\x02")],
            "unfinished_chain": head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=fragment)],
            "chain_interrupted_by_single_nal": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=fragment),
                rtp(13, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)],
            "chain_interrupted_by_new_timestamp": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=fragment),
                rtp(13, 18000, payload_type=PAYLOAD_TYPE,
                    payload=bytes([0x7C, 0x45]) + b"\x02", marker=True)],
            "stap_zero_size": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x18]) + struct.pack(">H", 0))],
            "stap_overrun": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE,
                    payload=bytes([0x18]) + struct.pack(">H", 40) + slice_nal[:10])],
            "stap_trailing": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x18])
                    + struct.pack(">H", len(slice_nal)) + slice_nal + b"\x01")],
            "stap_nested_fragment": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0x18])
                    + struct.pack(">H", 3) + bytes([0x7C, 0x85, 0x01]))],
            "sequence_gap": head + [rtp(13, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal,
                                        marker=True)],
            "duplicate": head + [rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps)],
            "forbidden_zero_bit": head + [
                rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=bytes([0xE5]) + bytes(20),
                    marker=True)],
            "packet_after_marker": [
                rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps, marker=True),
                rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps)],
            "no_parameter_sets": [rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal,
                                      marker=True)],
            "empty_payload": head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=b"")],
        }
        for name, packets in cases.items():
            with self.assertRaises(ValueError, msg=f"{name} must be refused, not assembled"):
                self.rebuild(packets)
        good = head + [rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)]
        with self.assertRaises(ValueError):
            self.rebuild(good, clock_rate=8000)
        with self.assertRaises(ValueError):
            self.rebuild(good, payload_type=200)
        with self.assertRaises(ValueError):
            self.rebuild(good, ssrc=0xDEAD)
        stream, report = self.rebuild(good)
        self.assertEqual(report["access_unit_count"], 1)
        self.assertTrue(stream.startswith(b"\x00\x00\x00\x01" + sps))

    def test_the_parameter_set_prerequisite_is_both_sets_and_a_coded_slice(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))
        for sets in ([sps], [pps]):
            packets = [rtp(10 + index, 0, payload_type=PAYLOAD_TYPE, payload=nal)
                       for index, nal in enumerate(sets)]
            packets.append(rtp(10 + len(sets), 0, payload_type=PAYLOAD_TYPE, payload=slice_nal,
                               marker=True))
            with self.assertRaises(ValueError):
                self.rebuild(packets)
        with self.assertRaises(ValueError):
            self.rebuild([rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                          rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps, marker=True)])
        with self.assertRaises(ValueError):
            self.rebuild([rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal),
                          rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                          rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=pps, marker=True)])
        sets_first, report = self.rebuild([rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                                           rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps),
                                           rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal,
                                               marker=True)])
        self.assertEqual(report["access_units"][0]["nal_types"], [7, 8, 5],
                         "the same three NALs in the other order are the accepted shape")
        self.assertEqual(self.bridge._prerequisites([{"nal_types": [5, 7, 8]}])[0], [])
        self.assertEqual(self.bridge._prerequisites([{"nal_types": [7, 5, 8]}])[0], [7])
        with self.assertRaises(ValueError):
            self.rebuild([rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True),
                          rtp(11, 18000, payload_type=PAYLOAD_TYPE, payload=sps),
                          rtp(12, 18000, payload_type=PAYLOAD_TYPE, payload=pps),
                          rtp(13, 18000, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)])
        _, report = self.rebuild([rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                                  rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps),
                                  rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal,
                                      marker=True)])
        self.assertEqual(report["parameter_sets_before_first_slice"], [7, 8])
        self.assertEqual(report["coded_slice_access_units"], 1)
        self.assertIn("NOT_PARAMETER_ID_OR_PROFILE", report["parameter_set_check_basis"])

    def test_timestamp_wrap_reorder_and_jumps_never_produce_negative_or_invented_timing(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))

        def stream_of(stamps):
            packets, sequence = [], 10
            for index, stamp in enumerate(stamps):
                if index == 0:
                    packets.append(rtp(sequence, stamp, payload_type=PAYLOAD_TYPE, payload=sps))
                    sequence += 1
                    packets.append(rtp(sequence, stamp, payload_type=PAYLOAD_TYPE, payload=pps))
                    sequence += 1
                packets.append(rtp(sequence, stamp, payload_type=PAYLOAD_TYPE, payload=slice_nal,
                                   marker=True))
                sequence += 1
            return packets

        step = CLOCK // 5
        base = (1 << 32) - step
        _, report = self.rebuild(stream_of([base, (base + step) % (1 << 32),
                                            (base + 2 * step) % (1 << 32)]))
        elapsed = [unit["rtp_ticks_since_first"] for unit in report["access_units"]]
        self.assertEqual(elapsed, [0, step, 2 * step])
        self.assertTrue(all(value >= 0 for value in elapsed))
        self.assertEqual([unit["rtp_timestamp"] for unit in report["access_units"]],
                         [base, (base + step) % (1 << 32), (base + 2 * step) % (1 << 32)],
                         "the raw stamps are kept exactly as recorded")
        self.assertEqual(report["rtp_cadence_ticks"], step)
        self.assertEqual(report["declared_mux_frame_rate"], 5)
        self.assertGreater(report["declared_mux_frame_rate"], 0)
        self.assertTrue(report["rtp_timing_monotonic"])

        _, report = self.rebuild(stream_of([0, 2 * step, step, 3 * step]))
        states = [unit["rtp_time_state"] for unit in report["access_units"]]
        self.assertEqual(states,
                         ["first", "forward", "nonmonotonic_backward", "unknown_after_nonmonotonic"],
                         "the elapsed axis ends at the first unmeasurable step and is not resumed")
        self.assertIsNone(report["access_units"][3]["rtp_ticks_since_first"])
        self.assertIsNone(report["access_units"][2]["rtp_ticks_since_first"])
        self.assertIsNone(report["access_units"][2]["rtp_source_relative_us"])
        self.assertFalse(report["rtp_timing_monotonic"])
        self.assertIsNone(report["rtp_cadence_ticks"])
        self.assertIsNone(report["declared_mux_frame_rate"])
        self.assertIn("NOT_OFFERED", report["mux_timing_basis"])

        _, report = self.rebuild(stream_of([0, step, step + self.bridge.MAX_FORWARD_TICKS + 1]))
        self.assertEqual(report["access_units"][2]["rtp_time_state"], "unknown_forward_jump")
        self.assertIsNone(report["access_units"][2]["rtp_ticks_since_first"])
        self.assertIsNone(report["declared_mux_frame_rate"])

    def test_a_second_source_may_not_fill_the_selected_sources_sequence_gap(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))
        packets = [rtp(10, 0, ssrc=0xAA, payload_type=PAYLOAD_TYPE, payload=sps),
                   rtp(11, 0, ssrc=0xAA, payload_type=PAYLOAD_TYPE, payload=pps),
                   rtp(12, 0, ssrc=0xBB, payload_type=97, payload=b"\x01\x02\x03"),
                   rtp(13, 0, ssrc=0xAA, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)]
        with self.assertRaises(ValueError):
            self.rebuild(packets)
        with self.assertRaises(ValueError):
            self.rebuild(packets, ssrc=0xAA)
        packets[3] = rtp(12, 0, ssrc=0xAA, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)
        _, report = self.rebuild(packets, ssrc=0xAA)
        self.assertEqual(report["retained_packets"], 3)
        self.assertEqual(report["selected_source"]["ssrc"], 0xAA)
        self.assertEqual(report["unselected_packets"], 1)

    def test_the_prerequisite_scan_stays_linear_on_a_large_authored_list(self):
        units = [{"nal_types": [7, 8, 5] if index == 0 else [1],
                  "nal_sizes": [4, 4, 40] if index == 0 else [900],
                  "rtp_timestamp": index * 18000, "index": index} for index in range(20_000)]
        start = time.perf_counter()
        parameter_sets, slice_units = self.bridge._prerequisites(units)
        elapsed = time.perf_counter() - start
        self.assertEqual(parameter_sets, [7, 8])
        self.assertEqual(len(slice_units), len(units))
        self.assertIs(slice_units[0], units[0])
        self.assertIs(slice_units[-1], units[-1])
        self.assertLess(elapsed, 1.0, f"prerequisite scan took {elapsed:.3f}s for {len(units)} units")
        mixed = [{"nal_types": [7, 8], "nal_sizes": [4, 4]},
                 {"nal_types": [6], "nal_sizes": [20]},
                 {"nal_types": [5], "nal_sizes": [40]},
                 {"nal_types": [1, 1], "nal_sizes": [30, 30]}]
        parameter_sets, slice_units = self.bridge._prerequisites(mixed)
        self.assertEqual(parameter_sets, [7, 8])
        self.assertEqual(len(slice_units), 2, "a two slice picture is still one unit that carries one")

    def test_another_payload_type_is_counted_not_assembled(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))
        packets = [rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                   rtp(11, 0, payload_type=97, payload=b"\x01\x02\x03"),
                   rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=pps),
                   rtp(13, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)]
        stream, report = self.rebuild(packets)
        self.assertEqual(report["unsupported_packets"], 1)
        self.assertEqual(report["unsupported_payload_types"], [97])
        self.assertEqual(report["retained_packets"], 3)
        self.assertEqual(report["access_units"][0]["nal_types"], [7, 8, 5])
        self.assertEqual(len(stream), sum(4 + len(n) for n in (sps, pps, slice_nal)))


@unittest.skipUnless(FFMPEG and FFPROBE, DECODER_REASON)
class RtpH264DecodedFrameTests(unittest.TestCase):
    """The qualified offline decoder establishes the frame records; header parsing alone is not it."""

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dpkt") is None:
            raise unittest.SkipTest("the pinned PCAP decoder is required for this path")
        cls.bridge = load()

    def probe(self, path, work, *, muxed=None):
        """The qualified ffprobe invocation the video case already fixes, over a local file only."""
        target = Path(work) / "probe.json"
        command = [FFPROBE, "-loglevel", "error"]
        if muxed is None:
            command += ["-f", "h264"]
        command += ["-show_program_version", "-show_streams", "-show_frames", "-count_frames",
                    "-of", "json", str(path)]
        done = subprocess.run(command, capture_output=True, text=True, check=True)
        target.write_text(done.stdout, encoding="utf-8")
        return target, json.loads(done.stdout)

    def rebuild_authored(self, work, name, layout, **kwargs):
        authored = author_video(work, name, **kwargs)
        grouped = access_units(annex_b_units(authored))
        packets = packetize(grouped, layout=layout)
        stream, report = self.bridge.depacketize(capture_of(packets), PORT,
                                                 payload_type=PAYLOAD_TYPE)
        return authored, grouped, stream, report

    def test_two_authored_videos_in_two_layouts_rebuild_byte_identical_streams(self):
        with tempfile.TemporaryDirectory() as work:
            for name, layout, size in (("stap", "stap-a", "64x48"), ("frag", "fu-a", "128x96")):
                authored, grouped, stream, report = self.rebuild_authored(work, name, layout,
                                                                          size=size)
                self.assertEqual(annex_b_units(stream), annex_b_units(authored),
                                 f"{layout}: every NAL unit must come back unchanged")
                self.assertEqual(report["access_unit_count"], len(grouped))
                self.assertEqual(report["rtp_cadence_ticks"], CLOCK // 5)
                self.assertEqual(report["declared_mux_frame_rate"], 5)
                self.assertEqual(report["parameter_sets_before_first_slice"], [7, 8])
                packets = packetize(grouped, layout=layout)
                fragments = [p for p in packets if p[12] & 0x1F == 28]
                aggregations = [p for p in packets if p[12] & 0x1F == 24]
                if layout == "fu-a":
                    self.assertTrue(fragments, "this layout must fragment")
                    self.assertFalse(aggregations)
                else:
                    self.assertFalse(fragments)
                    self.assertTrue(aggregations, "this layout must aggregate the parameter sets")
                elementary = Path(work) / f"{name}.rebuilt.h264"
                elementary.write_bytes(stream)
                _, probed = self.probe(elementary, work)
                self.assertEqual(probed["program_version"]["version"], QUALIFIED_VERSION)
                self.assertEqual(int(probed["streams"][0]["nb_read_frames"]), len(grouped))
                self.assertEqual(probed["streams"][0]["codec_name"], "h264")
                self.assertEqual(f"{probed['streams'][0]['width']}x{probed['streams'][0]['height']}",
                                 size)

    def test_a_multi_frame_case_reaches_common_frame_observations_with_source_linkage(self):
        frames, rate = 30, 5
        with tempfile.TemporaryDirectory() as work:
            authored, grouped, stream, report = self.rebuild_authored(
                work, "multi", "fu-a", size="96x64", duration=frames / rate, keyint=10)
            self.assertEqual(len(grouped), frames)
            self.assertEqual(report["access_unit_count"], frames)
            elementary = Path(work) / "multi.rebuilt.h264"
            elementary.write_bytes(stream)
            self.assertEqual(report["declared_mux_frame_rate"], rate)
            muxed = Path(work) / "multi.mp4"
            subprocess.run([FFMPEG, "-loglevel", "error", "-f", "h264", "-r",
                            str(report["declared_mux_frame_rate"]), "-i", str(elementary),
                            "-c", "copy", "-video_track_timescale", str(CLOCK), str(muxed)],
                           check=True, capture_output=True)
            probe_json, probed = self.probe(muxed, work, muxed=True)
            stream_info = probed["streams"][0]
            self.assertEqual(int(stream_info["nb_read_frames"]), frames)
            self.assertEqual(stream_info["time_base"], f"1/{CLOCK}")

            out = Path(work) / "frames"
            subprocess.run([sys.executable, str(VIDEO_CONVERTER), str(probe_json), "0", str(out)],
                           check=True, capture_output=True)
            rows = list(csv.DictReader(io.StringIO((out / "observations.csv").read_text())))
            self.assertEqual(len(rows), frames, "one common frame row per authored frame")
            self.assertEqual({r["width_px"] for r in rows}, {"96"})
            self.assertEqual({r["height_px"] for r in rows}, {"64"})
            decoder_ticks = [int(r["presentation_timestamp_ticks"]) for r in rows]
            rtp_ticks = [unit["rtp_ticks_since_first"] for unit in report["access_units"]]
            self.assertEqual(decoder_ticks, rtp_ticks)
            self.assertEqual(decoder_ticks[1] - decoder_ticks[0], report["rtp_cadence_ticks"])
            self.assertNotIn("rtp_timestamp", rows[0], "RTP timing is not merged into frame rows")
            probed_sizes = [int(f["pkt_size"]) for f in probed["frames"]]
            self.assertEqual(len(probed_sizes), len(report["access_units"]))
            for probed_size, unit in zip(probed_sizes, report["access_units"]):
                self.assertEqual(probed_size, unit["elementary_bytes"],
                                 "muxed sample sizes account for the bytes the bridge rebuilt")
            self.assertEqual(sum(probed_sizes), len(stream))
            self.assertEqual(sum(len(u["packet_indices"]) for u in report["access_units"]),
                             report["retained_packets"])
            self.assertEqual([u["index"] for u in report["access_units"]], list(range(frames)))
            verdict = self.bridge.verify_decoder_join(report, probed)
            self.assertTrue(verdict["timing_equivalent"], verdict["failed_checks"])
            self.assertEqual(verdict["failed_checks"], [])
            self.assertTrue(all(verdict["checks"].values()))
            if not os.environ.get("MUSUBI_TELEMETRY_READER"):
                return
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(VIDEO_PROFILE),
                 str(out / "observations.csv")], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], frames)
            self.assertEqual(common["observations"][0]["fields"]["width_px"], 96)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")


@unittest.skipUnless(FFMPEG and FFPROBE, DECODER_REASON)
class RtpH264DecoderJoinTests(unittest.TestCase):
    """Arithmetic alone never establishes frame timing: the decoder has to agree, or no claim."""

    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dpkt") is None:
            raise unittest.SkipTest("the pinned PCAP decoder is required for this path")
        cls.bridge = load()

    def test_a_reordered_stream_is_refused_the_timing_claim_by_the_decoder_join(self):
        with tempfile.TemporaryDirectory() as work:
            authored = author_video(work, "bframes", size="96x64", duration=2.0, keyint=15,
                                    reordered=True)
            grouped = access_units(annex_b_units(authored))
            stream, report = self.bridge.depacketize(
                capture_of(packetize(grouped, layout="fu-a")), PORT, payload_type=PAYLOAD_TYPE)
            self.assertEqual(annex_b_units(stream), annex_b_units(authored),
                             "the bytes still come back unchanged; only the timing claim is at stake")
            elementary = Path(work) / "bframes.h264"
            elementary.write_bytes(stream)
            self.assertEqual(report["declared_mux_frame_rate"], 5)
            self.assertIn("REQUIRES_A_DECODER_JOIN", report["mux_timing_basis"])
            muxed = Path(work) / "bframes.mp4"
            subprocess.run([FFMPEG, "-loglevel", "error", "-f", "h264", "-r", "5", "-i",
                            str(elementary), "-c", "copy", "-video_track_timescale", str(CLOCK),
                            str(muxed)], check=True, capture_output=True)
            probed = json.loads(subprocess.run(
                [FFPROBE, "-loglevel", "error", "-show_streams", "-show_frames", "-count_frames",
                 "-of", "json", str(muxed)], check=True, capture_output=True, text=True).stdout)
            self.assertTrue(any(frame.get("pict_type") == "B" for frame in probed["frames"]),
                            "this fixture must really contain reordering")
            verdict = self.bridge.verify_decoder_join(report, probed)
            self.assertFalse(verdict["timing_equivalent"])
            self.assertFalse(verdict["input_valid"])
            self.assertTrue(any(name.endswith(".pkt_dts") for name in verdict["missing_or_invalid"]),
                            verdict["missing_or_invalid"])
            self.assertEqual(verdict["failed_checks"], ["required_decoder_metadata"])

    def two_unit_report(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))
        _, report = self.bridge.depacketize(capture_of([
            rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
            rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps),
            rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True),
            rtp(13, 18000, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)]),
            PORT, payload_type=PAYLOAD_TYPE)
        return report

    @staticmethod
    def probe_of(frames, *, index=0, has_b_frames=0, field_order="progressive", extra_streams=()):
        """A complete probe in the shape the qualified ffprobe actually emits."""
        stream = {"index": index, "codec_type": "video", "codec_name": "h264",
                  "has_b_frames": str(has_b_frames), "field_order": field_order,
                  "time_base": f"1/{CLOCK}", "nb_read_frames": str(len(frames))}
        built = [{"media_type": "video", "stream_index": index, "pts": pts, "pkt_dts": dts,
                  "pict_type": kind, "interlaced_frame": 0} for pts, dts, kind in frames]
        return {"streams": [stream, *extra_streams], "frames": built}

    def test_missing_or_malformed_decoder_metadata_is_never_a_timing_claim(self):
        report = self.two_unit_report()
        good = self.probe_of([(0, 0, "I"), (18000, 18000, "P")])
        self.assertTrue(self.bridge.verify_decoder_join(report, good)["timing_equivalent"],
                        "the complete probe must still pass, or the negatives prove nothing")
        cases = {}
        for field in ("has_b_frames", "field_order", "nb_read_frames", "time_base", "codec_type"):
            probe = self.probe_of([(0, 0, "I"), (18000, 18000, "P")])
            probe["streams"][0].pop(field)
            cases[f"stream_without_{field}"] = probe
        for field in ("pict_type", "interlaced_frame", "pkt_dts", "pts", "stream_index",
                      "media_type"):
            probe = self.probe_of([(0, 0, "I"), (18000, 18000, "P")])
            probe["frames"][1].pop(field)
            cases[f"frame_without_{field}"] = probe
        for field, value in (("pts", "abc"), ("pts", 1.5), ("pkt_dts", None),
                             ("interlaced_frame", "maybe"), ("stream_index", "x")):
            probe = self.probe_of([(0, 0, "I"), (18000, 18000, "P")])
            probe["frames"][1][field] = value
            cases[f"frame_{field}_malformed"] = probe
        malformed_stream = self.probe_of([(0, 0, "I"), (18000, 18000, "P")])
        malformed_stream["streams"][0]["has_b_frames"] = "many"
        cases["stream_has_b_frames_malformed"] = malformed_stream
        cases["frames_not_a_list"] = {"streams": good["streams"], "frames": {}}
        cases["no_streams"] = {"streams": [], "frames": good["frames"]}
        for name, probe in cases.items():
            verdict = self.bridge.verify_decoder_join(report, probe)  # never raises
            self.assertFalse(verdict["timing_equivalent"], name)
            self.assertFalse(verdict["input_valid"], name)
            self.assertTrue(verdict["missing_or_invalid"], name)
            self.assertEqual(verdict["failed_checks"], ["required_decoder_metadata"], name)

    def test_exactly_one_video_stream_must_be_identified(self):
        report = self.two_unit_report()
        frames = [(0, 0, "I"), (18000, 18000, "P")]
        second = {"index": 1, "codec_type": "video", "has_b_frames": "0",
                  "field_order": "progressive", "time_base": f"1/{CLOCK}", "nb_read_frames": "9"}
        ambiguous = self.probe_of(frames, extra_streams=(second,))
        verdict = self.bridge.verify_decoder_join(report, ambiguous)
        self.assertFalse(verdict["input_valid"])
        self.assertIn("exactly_one_video_stream_or_an_explicit_stream_index",
                      verdict["missing_or_invalid"])
        ambiguous["frames"].append({"media_type": "video", "stream_index": 1, "pts": 0,
                                    "pkt_dts": 0, "pict_type": "I", "interlaced_frame": 0})
        ambiguous["frames"].append({"media_type": "audio", "stream_index": 2, "pts": 0,
                                    "pkt_dts": 0, "pict_type": "", "interlaced_frame": 0})
        verdict = self.bridge.verify_decoder_join(report, ambiguous, stream_index=0)
        self.assertTrue(verdict["input_valid"], verdict["missing_or_invalid"])
        self.assertEqual(verdict["selected_stream_index"], 0)
        self.assertTrue(verdict["timing_equivalent"], verdict["failed_checks"])
        self.assertFalse(self.bridge.verify_decoder_join(report, ambiguous,
                                                         stream_index=7)["input_valid"])
        audio_only = {"streams": [{"index": 0, "codec_type": "audio"}], "frames": []}
        self.assertFalse(self.bridge.verify_decoder_join(report, audio_only)["input_valid"])

    def test_a_well_formed_probe_with_a_b_picture_fails_the_reorder_checks(self):
        report = self.two_unit_report()
        probe = self.probe_of([(0, 0, "I"), (18000, 18000, "B")], has_b_frames=1)
        verdict = self.bridge.verify_decoder_join(report, probe)
        self.assertTrue(verdict["input_valid"], verdict["missing_or_invalid"])
        self.assertFalse(verdict["timing_equivalent"])
        self.assertEqual(sorted(verdict["failed_checks"]), ["no_b_pictures", "zero_reorder_depth"])
        interlaced = self.probe_of([(0, 0, "I"), (18000, 18000, "P")], field_order="tt")
        self.assertIn("progressive_only",
                      self.bridge.verify_decoder_join(report, interlaced)["failed_checks"])
        for unknown in ("?", "BI", "SP", "X"):
            probe = self.probe_of([(0, 0, "I"), (18000, 18000, unknown)])
            verdict = self.bridge.verify_decoder_join(report, probe)
            self.assertTrue(verdict["input_valid"], unknown)
            self.assertFalse(verdict["timing_equivalent"], unknown)
            self.assertIn("no_b_pictures", verdict["failed_checks"], unknown)
        self.assertEqual(sorted(self.bridge.QUALIFIED_PICTURE_KINDS), ["I", "P"])

    def test_the_join_refuses_a_frame_count_that_does_not_match_the_coded_slice_units(self):
        report = self.two_unit_report()
        self.assertEqual(report["coded_slice_access_units"], 2)
        probe = self.probe_of([(0, 0, "I"), (18000, 18000, "P"), (36000, 36000, "P")])
        verdict = self.bridge.verify_decoder_join(report, probe)
        self.assertTrue(verdict["input_valid"], verdict["missing_or_invalid"])
        self.assertFalse(verdict["timing_equivalent"])
        self.assertIn("frame_count_matches_coded_slice_access_units", verdict["failed_checks"])
        self.assertIn("decoder_ticks_equal_rtp_ticks", verdict["failed_checks"])


class RtpH264CommandLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("dpkt") is None:
            raise unittest.SkipTest("the pinned PCAP decoder is required for this path")
        cls.bridge = load()

    def test_cli_writes_the_stream_and_linkage_and_never_overwrites(self):
        sps, pps = bytes([0x67, 0x42, 0x00, 0x0A]), bytes([0x68, 0xCE, 0x0F, 0x19])
        slice_nal = bytes([0x65]) + bytes(range(40))
        packets = [rtp(10, 0, payload_type=PAYLOAD_TYPE, payload=sps),
                   rtp(11, 0, payload_type=PAYLOAD_TYPE, payload=pps),
                   rtp(12, 0, payload_type=PAYLOAD_TYPE, payload=slice_nal, marker=True)]
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "capture.pcap"
            source.write_bytes(capture_of(packets))
            target = Path(work) / "out"
            done = subprocess.run([sys.executable, str(BRIDGE), str(source), str(target),
                                   "--udp-destination-port", str(PORT),
                                   "--payload-type", str(PAYLOAD_TYPE)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             ["access_units.json", "elementary.h264"])
            report = json.loads((target / "access_units.json").read_text())
            self.assertEqual(report["access_unit_count"], 1)
            self.assertEqual((target / "elementary.h264").read_bytes()[:4], b"\x00\x00\x00\x01")
            done = subprocess.run([sys.executable, str(BRIDGE), str(source), str(target),
                                   "--udp-destination-port", str(PORT),
                                   "--payload-type", str(PAYLOAD_TYPE)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2)
            refused = Path(work) / "refused"
            done = subprocess.run([sys.executable, str(BRIDGE), str(source), str(refused),
                                   "--udp-destination-port", str(PORT), "--payload-type", "97"],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2)
            self.assertFalse(refused.exists())


if __name__ == "__main__":
    unittest.main()
