#!/usr/bin/env python3
"""Saved RTP transport records -> per-source ordering/timing Observations; never a socket or a decoder.

Reads RTP v2 packets that are already inside a saved PCAP capture and reports, per (tuple, SSRC),
what the recording shows about ordering and cadence: extended sequence with its wrap cycles,
duplicate / reorder / discontinuity, the raw and extended RTP timestamp and a source-relative time
under an explicitly declared clock rate. No socket is opened, no RTSP session is created, no device is
contacted, nothing is decrypted, no payload is rendered, no fragment is reassembled and no codec claim
is made. A sequence gap here is a property of the recording, not proven packet or frame loss.

The saved-capture framing (classic PCAP 2.4, Ethernet/802.11, complete IPv4/UDP, checksum and length
validation, non-decreasing capture clock) is the admitted one: this module calls
`convert_imc_sensors.read_pcap_records` and `iter_udp_datagrams`, the shared walker both passive
converters now use, so there is exactly one implementation of those rules.
Contract, ordering/wrap policy and limits: docs/saved-inputs.md.
"""
import argparse
from collections import deque
import csv
import hashlib
import io
import json
from pathlib import Path
import struct

try:  # same import shape the existing converters use for each other
    from scripts.convert_imc_sensors import (LIMIT, OUTPUT_LIMIT, iter_udp_datagrams,
                                             read_pcap_records)
except ModuleNotFoundError:
    from convert_imc_sensors import LIMIT, OUTPUT_LIMIT, iter_udp_datagrams, read_pcap_records

# RFC 3550 §5.1 and appendix A.1 (Standards Track, July 2003).
RTP_VERSION = 2
RTP_SEQ_MOD = 1 << 16
RTP_TS_MOD = 1 << 32
MAX_DROPOUT = 3000
MAX_MISORDER = 100
MIN_SEQUENTIAL = 2
FIXED_HEADER = 12

# RFC 3551 §6 tables 4/5: the payload types that document actually fixes a clock rate for. Dynamic
# types (96-127) are deliberately absent - their rate comes from out-of-band signalling only.
RFC3551_CLOCK_RATE = {0: 8000, 3: 8000, 4: 8000, 5: 8000, 6: 16000, 7: 8000, 8: 8000, 9: 8000,
                      10: 44100, 11: 44100, 12: 8000, 13: 8000, 14: 90000, 15: 8000, 16: 11025,
                      17: 22050, 18: 8000, 25: 90000, 26: 90000, 28: 90000, 31: 90000, 32: 90000,
                      33: 90000, 34: 90000}
MAX_CLOCK_RATE = 1_000_000_000
DEFAULT_MAX_FORWARD_SECONDS = 10
# Retained per-source evidence of already observed sequence numbers. A repeat inside this window is
# named as a repeat even when it is not the highest; beyond it there is no evidence and the A.1
# windows alone decide (documented, not silently widened).
RECENT_SEQUENCES = 512
MAX_EXTENSION_BYTES = 4096
MAX_SOURCES = 1024
REPORT_LIMIT = 32 * 1024 * 1024

# Raw hex cells carry this prefix for the same reason digests carry `sha256:` (D-32 / R-10 §3.1 #17):
# a bare hex string such as `806003e80000006411223344` parses as a number in exponent form and becomes
# infinity for the CSV reader and a rewritten value for a spreadsheet.
HEX_PREFIX = "hex:"
CAPTURE_BASIS = "CAPTURE_REPORTED_NOT_AUTHENTICATED"
RATE_DECLARED = "DECLARED_BY_CALLER_NOT_OBSERVED"
RATE_STATIC = "RFC3551_STATIC_PAYLOAD_TYPE"


def _selected_clock_rate(payload_type, declared_rate, use_static):
    """The clock rate must be declared or fixed by RFC 3551; it is never inferred from the packets."""
    static = RFC3551_CLOCK_RATE.get(payload_type) if use_static else None
    if declared_rate is not None:
        if type(declared_rate) is not int or not 0 < declared_rate <= MAX_CLOCK_RATE:
            raise ValueError("declared RTP clock rate must be a positive bounded integer")
        if static is not None and static != declared_rate:
            raise ValueError("declared clock rate disagrees with the RFC3551 static rate")
        return declared_rate, RATE_DECLARED
    if static is None:
        raise ValueError("payload type has no RFC3551 static clock rate; declare one explicitly")
    return static, RATE_STATIC


def read_bounded(path, limit=None):
    """Read at most one byte past the bound, so an oversized capture is refused without reading it in.

    Same shape as the adopted saved-ULog entrypoint: the bound is enforced before the bytes exist, so
    an arbitrarily large file costs one bounded read rather than its own size.
    """
    limit = LIMIT if limit is None else limit
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("saved capture exceeds the bounded input size")
    return data


def parse_rtp(datagram):
    """RFC 3550 §5.1 header; returns the parsed fields. Malformed selected traffic raises, never drops."""
    if len(datagram) < FIXED_HEADER:
        raise ValueError("truncated RTP fixed header")
    first, second = datagram[0], datagram[1]
    if first >> 6 != RTP_VERSION:
        raise ValueError("unsupported RTP version")
    padding = bool(first & 0x20)
    extension = bool(first & 0x10)
    csrc_count = first & 0x0F
    marker = bool(second & 0x80)
    payload_type = second & 0x7F
    sequence, timestamp, ssrc = struct.unpack_from(">HII", datagram, 2)
    offset = FIXED_HEADER + 4 * csrc_count
    if len(datagram) < offset:
        raise ValueError("truncated RTP CSRC list")
    csrc = [int.from_bytes(datagram[FIXED_HEADER + 4 * i:FIXED_HEADER + 4 * i + 4], "big")
            for i in range(csrc_count)]
    extension_profile = extension_bytes = None
    if extension:
        if len(datagram) < offset + 4:
            raise ValueError("truncated RTP extension header")
        extension_profile, words = struct.unpack_from(">HH", datagram, offset)
        if 4 * words > MAX_EXTENSION_BYTES:
            raise ValueError("RTP extension exceeds the retained bound")
        offset += 4
        if len(datagram) < offset + 4 * words:
            raise ValueError("truncated RTP extension body")
        extension_bytes = datagram[offset:offset + 4 * words]
        offset += 4 * words
    payload = datagram[offset:]
    pad_bytes = 0
    if padding:
        if not payload:
            raise ValueError("padding declared with no payload octet")
        pad_bytes = payload[-1]
        if pad_bytes == 0 or pad_bytes > len(payload):
            raise ValueError("invalid RTP padding length")
        payload = payload[:len(payload) - pad_bytes]
    return dict(marker=marker, payload_type=payload_type, sequence=sequence, timestamp=timestamp,
                ssrc=ssrc, csrc=csrc, extension_profile=extension_profile,
                extension_bytes=extension_bytes, padding_bytes=pad_bytes, payload=payload,
                header_bytes=datagram[:offset])


class _Source:
    """Per (tuple, SSRC) ordering state. A.1 semantics; nothing here is a loss count."""

    def __init__(self):
        self.highest = None
        self.cycles = 0
        self.epoch = 0
        self.probation = 0
        self.bad_sequence = None
        self.timestamp_cycles = 0
        self.highest_timestamp = None
        self.first_extended_timestamp = None
        self.payload_type = None
        # Bounded evidence of what was already observed in this epoch, keyed by the **extended**
        # sequence (epoch, cycles + sequence) -> payload digest, oldest evicted first. Keying the raw
        # 16 bit number would make a legitimate post-wrap packet collide with the same number one
        # cycle earlier; the extended value is the identity that survives a wrap. A repeat older than
        # this window has no retained evidence and is reported by the A.1 windows only.
        self.seen = {}
        self.seen_order = deque()
        self.counts = dict(packets=0, in_order=0, duplicate=0, repeated_sequence_new_payload=0,
                           reorder=0, discontinuity_unknown=0, restarts=0)

    def _remember(self, extended, digest):
        key = (self.epoch, extended)
        if key not in self.seen:
            self.seen_order.append(key)
            if len(self.seen_order) > RECENT_SEQUENCES:
                self.seen.pop(self.seen_order.popleft(), None)
        self.seen[key] = digest

    def _repeat(self, extended, digest):
        """Observed-before state for this extended sequence, or None when no evidence is retained."""
        known = self.seen.get((self.epoch, extended))
        if known is None:
            return None
        if known == digest:
            self.counts["duplicate"] += 1
            return "duplicate"
        # The number repeats but the bytes differ: say so instead of implying one payload.
        self.counts["repeated_sequence_new_payload"] += 1
        return "repeated_sequence_new_payload"

    def _resolve(self, sequence):
        """A.1 classification of one raw number: (kind, candidate extended value or None, step)."""
        delta = (sequence - self.highest) % RTP_SEQ_MOD
        if delta == 0:
            return "repeat_of_highest", self.cycles + self.highest, 0
        if delta < MAX_DROPOUT:
            # Forward, including the 65535 -> 0 wrap: one cycle more than the current one.
            return "forward", self.cycles + (RTP_SEQ_MOD if sequence < self.highest else 0) + sequence, delta
        if delta > RTP_SEQ_MOD - MAX_MISORDER:
            # Late: a raw number above the current highest belongs to the cycle before the last wrap;
            # if that cycle was never observed the extended value is withheld rather than invented.
            extended = self.cycles - (RTP_SEQ_MOD if sequence > self.highest else 0) + sequence
            return "late", (None if extended < 0 else extended), delta - RTP_SEQ_MOD
        return "unknown", None, None

    def order(self, sequence, digest):
        """Returns (state, extended sequence or None, step or None); `restart` names a new epoch.

        A large jump stays unknown: no gap size is claimed and the extended value is withheld until
        two consecutive observed packets confirm the new numbering (RFC 3550 A.1 `MIN_SEQUENTIAL`).
        """
        self.counts["packets"] += 1
        if self.highest is None:
            self.highest = sequence
            self.probation = MIN_SEQUENTIAL - 1
            self.counts["in_order"] += 1
            self._remember(self.cycles + sequence, digest)
            return "first", self.cycles + sequence, None
        kind, extended, step = self._resolve(sequence)
        if extended is not None:
            repeat = self._repeat(extended, digest)
            if repeat is not None:
                # This exact extended value was observed before in this epoch, wherever it sits: a
                # repeat, whatever the A.1 window would have said. Nothing moves, and an earlier
                # reordered arrival is not counted twice. A post-wrap number that merely looks like an
                # earlier one resolves to a different extended value and is not caught here.
                self.bad_sequence = None
                self._remember(extended, digest)
                return repeat, extended, step
        if kind == "repeat_of_highest":
            # Repeat of the highest with no retained evidence of the first arrival.
            self.counts["duplicate"] += 1
            self.bad_sequence = None
            self._remember(extended, digest)
            return "duplicate", extended, 0
        if kind == "forward":
            if sequence < self.highest:
                self.cycles += RTP_SEQ_MOD  # wrapped 65535 -> 0
            self.highest = sequence
            if self.probation:
                self.probation -= 1
            self.bad_sequence = None  # a normal arrival breaks any pending restart confirmation
            self.counts["in_order"] += 1
            self._remember(extended, digest)
            return "in_order", extended, step
        if kind == "late":
            self.counts["reorder"] += 1
            self.bad_sequence = None  # a late arrival is not the consecutive confirmation either
            if extended is not None:
                self._remember(extended, digest)
            return "reorder", extended, step
        # Outside both windows: a restart or a jump nobody can size from the recording alone.
        self.counts["discontinuity_unknown"] += 1
        if self.bad_sequence is not None and sequence == self.bad_sequence:
            # Two consecutive observed packets at the new numbering: a new epoch, not a measured loss.
            self.highest = sequence
            self.cycles = 0
            self.epoch += 1
            self.probation = MIN_SEQUENTIAL - 1
            self.bad_sequence = None
            self.timestamp_cycles = 0
            self.highest_timestamp = None
            self.first_extended_timestamp = None
            self.seen.clear()
            self.seen_order.clear()
            self.counts["restarts"] += 1
            self._remember(self.cycles + sequence, digest)  # the new epoch's evidence starts here
            return "restart", self.cycles + sequence, None
        self.bad_sequence = (sequence + 1) % RTP_SEQ_MOD
        return "discontinuity_unknown", None, None

    def tick(self, timestamp, max_forward):
        """Extended RTP tick under the declared forward bound, or an explicit unknown. No guessed wrap."""
        if self.highest_timestamp is None:
            self.highest_timestamp = timestamp
            self.first_extended_timestamp = timestamp
            return "first", timestamp
        forward = (timestamp - self.highest_timestamp) % RTP_TS_MOD
        backward = (self.highest_timestamp - timestamp) % RTP_TS_MOD
        if forward == 0:
            return "same_timestamp", self.timestamp_cycles + self.highest_timestamp
        if forward <= max_forward:
            if timestamp < self.highest_timestamp:
                self.timestamp_cycles += RTP_TS_MOD  # wrapped past 2^32
            self.highest_timestamp = timestamp
            return "forward", self.timestamp_cycles + timestamp
        if backward <= max_forward:
            # A late packet inside the same bound keeps the current cycle and never moves the state.
            # A raw value above the current highest is a pre-wrap tick: it belongs to the cycle before
            # the last wrap, not almost a whole modulo into the future. If that earlier cycle was never
            # observed (a late packet from before this source's first retained tick) the extended value
            # is withheld instead of invented.
            previous_cycle = timestamp > self.highest_timestamp
            extended = self.timestamp_cycles - (RTP_TS_MOD if previous_cycle else 0) + timestamp
            return "backward", (None if extended < 0 else extended)
        return "discontinuity_unknown", None


def convert_pcap_rtp(data, destination_port, *, clock_rate=None, rfc3551_static=False,
                     payload_type=None, ssrc=None, max_forward_seconds=DEFAULT_MAX_FORWARD_SECONDS,
                     packet_range=None):
    """Saved capture -> (CSV of per-packet ordering/timing rows, JSON-ready report)."""
    # The shared walker enforces the pinned decoder version and the destination port.
    if payload_type is not None and (type(payload_type) is not int or not 0 <= payload_type <= 127):
        raise ValueError("explicit RTP payload type must be 0..127")
    if ssrc is not None and (type(ssrc) is not int or not 0 <= ssrc < RTP_TS_MOD):
        raise ValueError("explicit SSRC must be a 32 bit value")
    if type(max_forward_seconds) is not int or not 0 < max_forward_seconds <= 3600:
        raise ValueError("explicit bounded forward window required")
    if packet_range is not None:
        first_index, last_index = packet_range
        if (type(first_index) is not int or type(last_index) is not int
                or not 0 <= first_index <= last_index):
            raise ValueError("packet range must be two ordered non-negative capture indices")
    if payload_type is None and rfc3551_static:
        raise ValueError("the RFC3551 static mapping needs the explicitly selected payload type")
    if payload_type is not None:
        selected_rate, rate_basis = _selected_clock_rate(payload_type, clock_rate, rfc3551_static)
    elif clock_rate is not None:
        selected_rate, rate_basis = _selected_clock_rate(None, clock_rate, False)
    else:
        raise ValueError("an explicit clock rate or a static payload type selection is required")
    max_forward = selected_rate * max_forward_seconds
    if max_forward >= RTP_TS_MOD // 2:
        # Forward and backward distances are both taken modulo 2^32; once the declared window reaches
        # half the modulus the two directions overlap and "forward" would win a coin toss. Refuse the
        # declaration rather than resolve it by magnitude.
        raise ValueError("declared forward window must stay below half the RTP timestamp modulus")

    records = read_pcap_records(data)  # the shared container validation
    linktype, captures = records

    source_sha = "sha256:" + hashlib.sha256(data).hexdigest()
    report = dict(source_sha256=source_sha, packets=len(captures), selected_packets=0,
                  unselected_packets=0, unsupported_packets=0, retained_packets=0,
                  out_of_range_packets=0, packet_range=list(packet_range) if packet_range else None,
                  linktype=linktype, clock="Unknown", observed_payload_type=None,
                  clock_rate_hz=selected_rate, clock_rate_basis=rate_basis,
                  selected_payload_type=payload_type, selected_ssrc=ssrc,
                  max_forward_ticks=max_forward, unselected_status="NOT_DECODED_OR_VALIDATED",
                  unsupported_payload_types=[], sources=[])
    output, writer = io.StringIO(newline=""), None
    states, keys, report_size = {}, [], 0
    observed_payload_type = None

    for saved in iter_udp_datagrams(data, destination_port, records):
        index = saved.index
        if packet_range is not None and not packet_range[0] <= index <= packet_range[1]:
            # A declared segment of the same capture: outside it nothing is counted as traffic.
            report["out_of_range_packets"] += 1
            continue
        if saved.payload is None:
            report["unselected_packets"] += 1
            continue
        capture = saved.capture_time_us
        rtp = parse_rtp(saved.payload)  # selected traffic: malformed raises, never silently dropped
        if ssrc is not None and rtp["ssrc"] != ssrc:
            report["unselected_packets"] += 1
            continue
        report["selected_packets"] += 1
        if payload_type is None:
            # No explicit filter: the declared rate belongs to one profile, so the selected traffic must
            # stay on one payload type. A mid-stream change is refused, never timed with another rate.
            if observed_payload_type is None:
                observed_payload_type = rtp["payload_type"]
                report["observed_payload_type"] = observed_payload_type
            elif rtp["payload_type"] != observed_payload_type:
                raise ValueError(
                    "selected traffic changes payload type; declare --payload-type to select one")
        if payload_type is not None and rtp["payload_type"] != payload_type:
            # Well formed, but not the declared stream: counted and named, never silently discarded.
            report["unsupported_packets"] += 1
            if rtp["payload_type"] not in report["unsupported_payload_types"]:
                report["unsupported_payload_types"].append(rtp["payload_type"])
            continue

        addresses = (saved.source_address, saved.source_port,
                     saved.destination_address, saved.destination_port)
        key = (*addresses, rtp["ssrc"])
        if key not in states:
            if len(states) >= MAX_SOURCES:
                raise ValueError("selected capture exceeds the source bound")
            states[key] = _Source()
            keys.append(key)
        state = states[key]
        payload_digest = hashlib.sha256(rtp["payload"]).hexdigest()
        order_state, extended_sequence, step = state.order(rtp["sequence"], payload_digest)
        if order_state == "discontinuity_unknown":
            # An unsized jump may belong to a different epoch: its timestamp must not move the
            # extension state either, so the tick is reported unknown rather than guessed.
            timestamp_state, extended_ticks = "discontinuity_unknown", None
        else:
            timestamp_state, extended_ticks = state.tick(rtp["timestamp"], max_forward)
        relative_us = ""
        if extended_ticks is not None and state.first_extended_timestamp is not None:
            relative_us = str(((extended_ticks - state.first_extended_timestamp) * 1_000_000)
                              // selected_rate)
        row = {
            "record_time_us": capture,
            "pcap_packet_index": index,
            "pcap_source_sha256": source_sha,
            "pcap_clock_basis": CAPTURE_BASIS,
            "rtp_source_address": addresses[0],
            "rtp_source_port": addresses[1],
            "rtp_destination_address": addresses[2],
            "rtp_destination_port": addresses[3],
            "rtp_ssrc": rtp["ssrc"],
            "rtp_payload_type": rtp["payload_type"],
            "rtp_marker": int(rtp["marker"]),
            "rtp_sequence": rtp["sequence"],
            "rtp_extended_sequence": "" if extended_sequence is None else extended_sequence,
            "rtp_sequence_cycles": state.cycles // RTP_SEQ_MOD,
            "rtp_sequence_epoch": state.epoch,
            "rtp_sequence_step": "" if step is None else step,
            "rtp_order_state": order_state,
            "rtp_timestamp": rtp["timestamp"],
            "rtp_extended_timestamp": "" if extended_ticks is None else extended_ticks,
            "rtp_timestamp_state": timestamp_state,
            "rtp_source_relative_us": relative_us,
            "rtp_clock_rate_hz": selected_rate,
            "rtp_clock_rate_basis": rate_basis,
            "rtp_csrc_count": len(rtp["csrc"]),
            "rtp_csrc_list": "|".join(HEX_PREFIX + f"{c:08x}" for c in rtp["csrc"]),
            "rtp_extension_profile": "" if rtp["extension_profile"] is None
                                     else HEX_PREFIX + f"{rtp['extension_profile']:04x}",
            "rtp_extension_hex": "" if rtp["extension_bytes"] is None
                                 else HEX_PREFIX + rtp["extension_bytes"].hex(),
            "rtp_padding_bytes": rtp["padding_bytes"],
            "rtp_payload_bytes": len(rtp["payload"]),
            "rtp_payload_sha256": "sha256:" + payload_digest,
            "rtp_header_hex": HEX_PREFIX + rtp["header_bytes"].hex(),
            "rtp_packet_sha256": "sha256:" + hashlib.sha256(saved.payload).hexdigest(),
        }
        if writer is None:
            writer = csv.DictWriter(output, list(row), lineterminator="\n")
            writer.writeheader()
        writer.writerow(row)
        report["retained_packets"] += 1
        report_size += len(row["rtp_extension_hex"]) + len(row["rtp_header_hex"]) + 256
        if report_size > REPORT_LIMIT:
            raise ValueError("packet report exceeds bound")
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("packet CSV exceeds bound")

    if ssrc is not None and not any(key[4] == ssrc for key in keys):
        raise ValueError("the selected SSRC is not present in this capture")
    if not report["retained_packets"]:
        raise ValueError("no retained RTP packet observations")
    for key in keys:
        state = states[key]
        report["sources"].append(dict(
            source_address=key[0], source_port=key[1], destination_address=key[2],
            destination_port=key[3], ssrc=key[4], highest_sequence=state.highest,
            sequence_cycles=state.cycles // RTP_SEQ_MOD, sequence_epoch=state.epoch,
            first_extended_timestamp=state.first_extended_timestamp,
            highest_timestamp=state.highest_timestamp, **state.counts))
    report["sequence_gap_basis"] = ("RECORDED_SEQUENCE_ONLY_NOT_PROVEN_PACKET_OR_FRAME_LOSS")
    report["time_basis"] = "RTP_SOURCE_RELATIVE_TICKS_NOT_UTC_NO_RTCP_ANCHOR_CONSUMED"
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(
        description="Report per-source RTP ordering and timing from a saved PCAP capture.")
    parser.add_argument("input", type=Path, help="saved classic PCAP capture")
    parser.add_argument("new_output_directory", type=Path,
                        help="directory to create; existing inputs and evidence are never overwritten")
    parser.add_argument("--udp-destination-port", type=int, required=True)
    parser.add_argument("--payload-type", type=int, help="selected RTP payload type (0..127)")
    parser.add_argument("--clock-rate", type=int, help="declared clock rate in Hz (never inferred)")
    parser.add_argument("--rfc3551-static", action="store_true",
                        help="take the rate from the RFC3551 static table for the selected payload type")
    parser.add_argument("--ssrc", type=lambda v: int(v, 0), help="select exactly one SSRC")
    parser.add_argument("--max-forward-seconds", type=int, default=DEFAULT_MAX_FORWARD_SECONDS)
    parser.add_argument("--packet-range", help="declared segment FIRST:LAST of capture packet indices "
                                               "(inclusive); each segment is reported on its own")
    args = parser.parse_args()
    try:
        data = read_bounded(args.input)  # bounded before allocation
        segment = None
        if args.packet_range is not None:
            first, _, last = args.packet_range.partition(":")
            segment = (int(first), int(last))
        converted, report = convert_pcap_rtp(
            data, args.udp_destination_port, clock_rate=args.clock_rate,
            rfc3551_static=args.rfc3551_static, payload_type=args.payload_type, ssrc=args.ssrc,
            max_forward_seconds=args.max_forward_seconds, packet_range=segment)
        args.new_output_directory.mkdir()  # Preserve existing inputs and evidence.
        (args.new_output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.new_output_directory / "accounting.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (args.new_output_directory / "source.pcap").write_bytes(data)
    except (ValueError, OSError, ImportError):
        parser.exit(2, "saved RTP conversion failed; check capture, selection and declared clock\n")


if __name__ == "__main__":
    main()
