#!/usr/bin/env python3
"""Saved RTP/H.264 mode 1 payloads -> offline Annex B elementary stream; never a socket or a decoder.

Selected RTP packets that are already inside a saved PCAP are depacketized per RFC 6184 packetization
mode 1 (single NAL unit packets, STAP-A, FU-A) into an elementary stream file that the already
qualified local ffprobe/`scripts/convert_video_frames.py` path turns into common frame Observations.
This module opens no socket, starts no RTSP session, contacts no device, decrypts nothing, decodes no
pixels and launches no program: it only rebuilds the bytes the recording already contains.

Access-unit assembly is refused rather than guessed whenever the recording is ambiguous (sequence
gap, duplicate, reorder, broken fragment chain, malformed aggregation, missing parameter sets), so a
damaged access unit is never written as if it were good video, and a gap is never turned into an
invented frame. The 90 kHz RTP timing stays in this module's report; decoder timestamps stay with the
decoder. Contract and refusal policy:
docs/saved-inputs.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

try:  # same import shape the existing converters use for each other
    from scripts.convert_imc_sensors import LIMIT, iter_udp_datagrams, read_pcap_records
    from scripts.convert_rtp_records import RTP_SEQ_MOD, parse_rtp, read_bounded
except ModuleNotFoundError:
    from convert_imc_sensors import LIMIT, iter_udp_datagrams, read_pcap_records
    from convert_rtp_records import RTP_SEQ_MOD, parse_rtp, read_bounded

# RFC 6184 (Standards Track, May 2011) table 1 in section 5.2 and the mode 1 rule in section 5.4.
SINGLE_NAL = range(1, 24)
STAP_A = 24
FU_A = 28
FORBIDDEN_IN_MODE_1 = {25: "STAP-B", 26: "MTAP16", 27: "MTAP24", 29: "FU-B"}
RESERVED = {0, 30, 31}
NAL_SPS = 7
NAL_PPS = 8
CODED_SLICE = {1, 5}
H264_CLOCK_RATE = 90000  # section 5.1: "A 90 kHz clock rate MUST be used"
START_CODE = b"\x00\x00\x00\x01"

MAX_ELEMENTARY_BYTES = 64 * 1024 * 1024
MAX_ACCESS_UNITS = 65536
MAX_NALS_PER_ACCESS_UNIT = 4096
MAX_NAL_BYTES = 8 * 1024 * 1024
RTP_TS_MOD = 1 << 32
# Bounded forward window for the modular elapsed rule, well under half the modulus so forward and
# backward can never both fit (the same shape the adopted RTP unit validates).
MAX_FORWARD_TICKS = H264_CLOCK_RATE * 10
REPORT_LIMIT = 32 * 1024 * 1024

SCHEMA = "musubi.rtp-h264-elementary-stream/v1"
RTP_TIME_BASIS = "RTP_90KHZ_SOURCE_RELATIVE_NOT_UTC_NO_RTCP_ANCHOR"
DECODER_BASIS = "DECODED_FRAME_RECORDS_COME_FROM_THE_QUALIFIED_OFFLINE_DECODER_NOT_FROM_THIS_MODULE"
PARAMETER_SET_BASIS = ("STRUCTURAL_SPS_AND_PPS_BEFORE_FIRST_CODED_SLICE_NOT_PARAMETER_ID_OR_PROFILE_"
                       "COMPATIBILITY_WHICH_ONLY_THE_DECODER_ESTABLISHES")
MUX_BASIS = ("CANDIDATE_CONSTANT_CADENCE_REQUIRES_A_DECODER_JOIN_BEFORE_ANY_FRAME_TIMING_CLAIM")
MUX_NOT_OFFERED = ("NOT_OFFERED_CADENCE_NOT_CONSTANT_POSITIVE_AND_DIVIDING_THE_CLOCK")
JOIN_BASIS = ("decoded frame timing equals the RTP access unit timing only because the decoder's own "
              "output agrees on every check above; nothing is inferred from the declared mux rate")
JOIN_INVALID_BASIS = ("required decoder metadata was absent, malformed or ambiguous: no timing claim "
                      "is possible from this probe, and absence was not read as a passing value")
# Every field the qualified ffprobe emits for this path and this join needs. Absence fails closed.
REQUIRED_STREAM_FIELDS = ("index", "codec_type", "has_b_frames", "field_order", "time_base",
                          "nb_read_frames")
REQUIRED_FRAME_FIELDS = ("media_type", "stream_index", "pts", "pkt_dts", "pict_type",
                         "interlaced_frame")
# The only picture kinds this path qualifies for a timing claim: a non-reordered intra or predicted
# frame. Everything else the decoder can report ("B", "BI", "S", "SI", "SP", the unknown "?") fails.
QUALIFIED_PICTURE_KINDS = frozenset(("I", "P"))


class _AccessUnit:
    """One access unit under assembly: every packet of it shares the RTP timestamp (RFC 3550 §5.1)."""

    def __init__(self, timestamp, sequence, packet_index):
        self.timestamp = timestamp
        self.nals = []
        self.packet_indices = [packet_index]
        self.sequences = [sequence]
        self.marker_at = None

    def add_packet(self, sequence, packet_index):
        self.packet_indices.append(packet_index)
        self.sequences.append(sequence)

    def add_nal(self, nal):
        if len(nal) > MAX_NAL_BYTES:
            raise ValueError("NAL unit exceeds the retained bound")
        if len(self.nals) >= MAX_NALS_PER_ACCESS_UNIT:
            raise ValueError("access unit exceeds the NAL unit bound")
        if nal[0] & 0x80:
            # F bit: "possible syntax violation". Not emitted as good video.
            raise ValueError("forbidden_zero_bit set on a retained NAL unit")
        self.nals.append(nal)


def _single_nal(payload):
    """Section 5.6: the payload is the NAL unit itself."""
    return [payload]


def _stap_a(payload):
    """Section 5.7.1: STAP-A header octet, then 16 bit network-order sizes and their NAL units."""
    units, offset = [], 1
    while offset < len(payload):
        if len(payload) - offset < 2:
            raise ValueError("STAP-A trailing bytes without a size field")
        size = int.from_bytes(payload[offset:offset + 2], "big")
        offset += 2
        if size == 0:
            raise ValueError("STAP-A aggregation unit of size zero")
        if offset + size > len(payload):
            raise ValueError("STAP-A aggregation unit overruns the payload")
        unit = payload[offset:offset + size]
        kind = unit[0] & 0x1F
        if kind == STAP_A or kind == FU_A or kind in FORBIDDEN_IN_MODE_1 or kind in RESERVED:
            raise ValueError("STAP-A must not nest aggregation or fragmentation units")
        units.append(unit)
        offset += size
    if not units:
        raise ValueError("STAP-A carries no aggregation unit")
    return units


class _Fragments:
    """FU-A chain state (section 5.8). A chain that is not exactly start..end is refused."""

    def __init__(self):
        self.header = None
        self.parts = []
        self.sequence = None
        self.timestamp = None

    @property
    def open(self):
        return self.header is not None

    def start(self, indicator, fu_header, body, sequence, timestamp):
        self.header = bytes([(indicator & 0xE0) | (fu_header & 0x1F)])
        self.parts = [body]
        self.sequence = sequence
        self.timestamp = timestamp

    def extend(self, indicator, fu_header, body, sequence, timestamp):
        if (indicator & 0xE0) | (fu_header & 0x1F) != self.header[0]:
            raise ValueError("FU-A fragment changes the NAL header mid chain")
        if timestamp != self.timestamp:
            raise ValueError("FU-A chain interrupted by a new access unit timestamp")
        if sequence != (self.sequence + 1) % RTP_SEQ_MOD:
            raise ValueError("FU-A fragments are not in consecutive RTP sequence")
        self.parts.append(body)
        self.sequence = sequence

    def finish(self):
        nal = self.header + b"".join(self.parts)
        self.header, self.parts, self.sequence, self.timestamp = None, [], None, None
        return nal


def depacketize(data, destination_port, *, payload_type, clock_rate=H264_CLOCK_RATE, ssrc=None):
    """Saved capture -> (Annex B elementary stream bytes, JSON-ready report with source linkage)."""
    if type(payload_type) is not int or not 0 <= payload_type <= 127:
        raise ValueError("an explicit RTP payload type must be declared for H.264")
    if clock_rate != H264_CLOCK_RATE:
        raise ValueError("RFC 6184 fixes a 90 kHz clock rate for H.264")
    if ssrc is not None and (type(ssrc) is not int or not 0 <= ssrc < (1 << 32)):
        raise ValueError("explicit SSRC must be a 32 bit value")

    records = read_pcap_records(data)
    linktype, captures = records
    report = dict(schema=SCHEMA, source_sha256="sha256:" + hashlib.sha256(data).hexdigest(),
                  packets=len(captures), selected_packets=0, unselected_packets=0,
                  unsupported_packets=0, retained_packets=0, linktype=linktype,
                  selected_payload_type=payload_type, selected_ssrc=ssrc,
                  clock_rate_hz=clock_rate, rtp_time_basis=RTP_TIME_BASIS,
                  decoder_basis=DECODER_BASIS, unsupported_payload_types=[],
                  nal_type_counts={}, access_units=[])

    stream = bytearray()
    units = []
    current = None
    fragments = _Fragments()
    previous_sequence = None
    selected_source = None

    def close(unit):
        """Finish one access unit: append its NALs to the stream and record the linkage."""
        if fragments.open:
            raise ValueError("FU-A chain never ended before the access unit closed")
        if not unit.nals:
            raise ValueError("access unit carries no NAL unit")
        start = len(stream)
        for nal in unit.nals:
            stream.extend(START_CODE)
            stream.extend(nal)
            if len(stream) > MAX_ELEMENTARY_BYTES:
                raise ValueError("elementary stream exceeds the bounded size")
            kind = str(nal[0] & 0x1F)
            report["nal_type_counts"][kind] = report["nal_type_counts"].get(kind, 0) + 1
        if len(units) >= MAX_ACCESS_UNITS:
            raise ValueError("capture exceeds the access unit bound")
        units.append(dict(
            index=len(units), rtp_timestamp=unit.timestamp,
            packet_indices=unit.packet_indices, rtp_sequences=unit.sequences,
            nal_types=[nal[0] & 0x1F for nal in unit.nals],
            nal_sizes=[len(nal) for nal in unit.nals],
            marker_packet_index=unit.marker_at,
            elementary_offset=start, elementary_bytes=len(stream) - start,
            sha256="sha256:" + hashlib.sha256(bytes(stream[start:])).hexdigest()))

    for saved in iter_udp_datagrams(data, destination_port, records):
        if saved.payload is None:
            report["unselected_packets"] += 1
            continue
        rtp = parse_rtp(saved.payload)
        source = (saved.source_address, saved.source_port, saved.destination_address,
                  saved.destination_port, rtp["ssrc"])
        if ssrc is not None and rtp["ssrc"] != ssrc:
            report["unselected_packets"] += 1
            continue
        # The source is bound *before* its sequence space is used, and mixing is refused for every
        # payload type: otherwise another SSRC's packet could occupy a gap in the selected source's
        # numbering and make a real discontinuity look continuous.
        if selected_source is None:
            selected_source = source
            report["selected_source"] = dict(source_address=source[0], source_port=source[1],
                                             destination_address=source[2],
                                             destination_port=source[3], ssrc=source[4])
        elif source != selected_source:
            raise ValueError("selected traffic mixes sources; declare one SSRC")
        report["selected_packets"] += 1
        if previous_sequence is not None and rtp["sequence"] != (previous_sequence + 1) % RTP_SEQ_MOD:
            # A gap, duplicate or reorder makes offline access-unit assembly ambiguous. Refuse; never
            # invent a frame and never silently splice around the hole. The check runs over the whole
            # sequence space of this one source (RFC 3550 §5.1), so another payload type of the *same*
            # source occupies its number without breaking continuity.
            raise ValueError("selected RTP sequence is not consecutive; assembly would be ambiguous")
        previous_sequence = rtp["sequence"]
        if rtp["payload_type"] != payload_type:
            # Well formed, but not the declared stream: counted and named, never silently dropped.
            report["unsupported_packets"] += 1
            if rtp["payload_type"] not in report["unsupported_payload_types"]:
                report["unsupported_payload_types"].append(rtp["payload_type"])
            continue
        report["retained_packets"] += 1

        payload = rtp["payload"]
        if not payload:
            raise ValueError("selected RTP packet carries no H.264 payload")
        kind = payload[0] & 0x1F
        if kind in FORBIDDEN_IN_MODE_1:
            raise ValueError(f"{FORBIDDEN_IN_MODE_1[kind]} must not be used in packetization mode 1")
        if kind in RESERVED:
            raise ValueError("reserved NAL type in the selected stream")

        if current is not None and rtp["timestamp"] != current.timestamp:
            close(current)
            current = None
        if current is None:
            current = _AccessUnit(rtp["timestamp"], rtp["sequence"], saved.index)
        else:
            current.add_packet(rtp["sequence"], saved.index)
        if current.marker_at is not None:
            # The marker is the very last packet of an access unit (section 5.1): another packet of
            # the same timestamp after it contradicts the recording.
            raise ValueError("packet follows a marked access unit under the same timestamp")

        if kind in SINGLE_NAL:
            if fragments.open:
                raise ValueError("FU-A chain interrupted by another NAL unit")
            for nal in _single_nal(payload):
                current.add_nal(nal)
        elif kind == STAP_A:
            if fragments.open:
                raise ValueError("FU-A chain interrupted by an aggregation packet")
            for nal in _stap_a(payload):
                current.add_nal(nal)
        elif kind == FU_A:
            if len(payload) < 2:
                raise ValueError("truncated FU-A header")
            indicator, fu_header, body = payload[0], payload[1], payload[2:]
            start_bit, end_bit, reserved_bit = fu_header & 0x80, fu_header & 0x40, fu_header & 0x20
            if reserved_bit:
                raise ValueError("FU-A reserved bit must be zero")
            if start_bit and end_bit:
                raise ValueError("FU-A start and end bits must not both be set")
            fragment_type = fu_header & 0x1F
            if fragment_type in RESERVED or fragment_type in FORBIDDEN_IN_MODE_1 or \
                    fragment_type in (STAP_A, FU_A):
                raise ValueError("FU-A carries a type that cannot be fragmented")
            if start_bit:
                if fragments.open:
                    raise ValueError("FU-A start inside an unfinished chain")
                fragments.start(indicator, fu_header, body, rtp["sequence"], rtp["timestamp"])
            else:
                if not fragments.open:
                    raise ValueError("FU-A continuation without a start")
                fragments.extend(indicator, fu_header, body, rtp["sequence"], rtp["timestamp"])
                if end_bit:
                    current.add_nal(fragments.finish())
        else:  # pragma: no cover - the ranges above are exhaustive over 0..31
            raise ValueError("unhandled NAL type")

        if rtp["marker"]:
            current.marker_at = saved.index

    if fragments.open:
        raise ValueError("capture ends inside an unfinished FU-A chain")
    if current is not None:
        close(current)
    if not units:
        raise ValueError("no retained H.264 access unit in the selected capture")
    if ssrc is not None and selected_source is None:
        raise ValueError("the selected SSRC is not present in this capture")

    parameter_sets, slice_units = _prerequisites(units)
    # The finite promised prerequisite: *both* SPS and PPS before the first coded slice, and at least
    # one coded slice to decode. SPS alone, or a parameter-set-only capture with no slice at all, is
    # not a decodable stream and is refused instead of written. This is a structural check only: it
    # does not claim the parameter set IDs the slices reference actually match, or that any profile or
    # level is supported. The qualified offline decoder remains the authority, and its output is the
    # evidence kept alongside this report.
    if NAL_SPS not in parameter_sets or NAL_PPS not in parameter_sets:
        raise ValueError("both SPS and PPS must precede the first coded slice")
    if not slice_units:
        raise ValueError("no coded slice in the selected capture; nothing to decode")

    timing = _timing(units, clock_rate)
    report["access_units"] = units
    report["access_unit_count"] = len(units)
    report["coded_slice_access_units"] = len(slice_units)
    report["elementary_bytes"] = len(stream)
    report["elementary_sha256"] = "sha256:" + hashlib.sha256(bytes(stream)).hexdigest()
    report["parameter_sets_before_first_slice"] = parameter_sets
    report["parameter_set_check_basis"] = PARAMETER_SET_BASIS
    report.update(timing)
    if len(json.dumps(report)) > REPORT_LIMIT:
        raise ValueError("access unit report exceeds bound")
    return bytes(stream), report


def _timing(units, clock_rate):
    """Bounded modular elapsed ticks per access unit, or an explicit unknown. Raw stamps are kept.

    The RTP timestamp is 32 bit and wraps, so a raw subtraction turns a legitimate wrap into a huge
    negative elapsed value. The same bounded modular rule the adopted RTP unit uses is applied here:
    a forward step within [`MAX_FORWARD_TICKS`] extends the elapsed count (wrapping the cycle), a step
    outside it is left unknown, and a backward step - a non-monotonic presentation order, as B frames
    produce - is marked and never retimed into a constant display cadence.
    """
    elapsed = 0
    previous = None
    steps = []
    monotonic = True
    for unit in units:
        stamp = unit["rtp_timestamp"]
        if previous is None:
            unit["rtp_time_state"] = "first"
            unit["rtp_ticks_since_first"] = 0
            unit["rtp_source_relative_us"] = 0
            previous = stamp
            continue
        if not monotonic:
            # The elapsed axis ended at the first unmeasurable step: resuming it after a reordering or
            # an unsized jump would publish a shifted axis as if it were the source's own elapsed time.
            unit["rtp_time_state"] = "unknown_after_nonmonotonic"
            unit["rtp_ticks_since_first"] = None
            unit["rtp_source_relative_us"] = None
            previous = stamp
            continue
        forward = (stamp - previous) % RTP_TS_MOD
        backward = (previous - stamp) % RTP_TS_MOD
        if forward <= MAX_FORWARD_TICKS:
            elapsed += forward
            steps.append(forward)
            unit["rtp_time_state"] = "forward"
            unit["rtp_ticks_since_first"] = elapsed
            unit["rtp_source_relative_us"] = elapsed * 1_000_000 // clock_rate
            previous = stamp
            continue
        monotonic = False
        unit["rtp_ticks_since_first"] = None
        unit["rtp_source_relative_us"] = None
        unit["rtp_time_state"] = ("nonmonotonic_backward" if backward <= MAX_FORWARD_TICKS
                                  else "unknown_forward_jump")
        previous = stamp
    cadence = steps[0] if steps and len(set(steps)) == 1 else None
    constant = monotonic and cadence is not None and cadence > 0
    rate = clock_rate // cadence if constant and clock_rate % cadence == 0 else None
    return dict(rtp_cadence_ticks=cadence if constant else None,
                rtp_timing_monotonic=monotonic,
                declared_mux_frame_rate=rate,
                mux_timing_basis=MUX_BASIS if rate else MUX_NOT_OFFERED)


def verify_decoder_join(report, probe, *, stream_index=None):
    """Join this module's access units with the qualified decoder's own JSON output.

    Arithmetic alone never establishes that decoded frame timing equals the RTP access-unit timing: an
    access unit may carry no primary coded picture, a picture may be split over several NAL units, a
    field-coded picture produces one access unit per field, and any reordering (B pictures or a
    positive reorder depth) means decode order is not output order - RFC 6184 section 6.3 fixes
    transmission order as *decoding* order while RFC 3550 section 5.1 allows non-monotonic sampling
    timestamps. So the claim is only made after the decoder's own output agrees, field by field.

    `probe` is the ffprobe JSON the operator already produced (nothing is executed here). Required
    metadata that is absent, malformed or ambiguous makes the verdict **invalid**, never equivalent: a
    missing `has_b_frames` is not "zero", a missing `field_order` is not "progressive" and a missing
    `pict_type` is not "not a B picture". Exactly one video stream must be identified - either the
    probe contains exactly one, or `stream_index` names it - and every frame must bind itself to a
    stream by `media_type` and `stream_index`.
    """
    invalid = []
    streams = probe.get("streams")
    frames_in = probe.get("frames")
    if not isinstance(streams, list) or not isinstance(frames_in, list):
        return _invalid(["probe.streams", "probe.frames"], stream_index=None)
    video = [entry for entry in streams
             if isinstance(entry, dict) and entry.get("codec_type") == "video"]
    if stream_index is None:
        if len(video) != 1:
            return _invalid(["exactly_one_video_stream_or_an_explicit_stream_index"], None)
        stream = video[0]
    else:
        named = [entry for entry in video if _integer(entry.get("index")) == stream_index]
        if len(named) != 1:
            return _invalid([f"named_video_stream_index_{stream_index}"], stream_index)
        stream = named[0]
    selected_index = _integer(stream.get("index"))
    if selected_index is None:
        return _invalid(["stream.index"], stream_index)
    for field in REQUIRED_STREAM_FIELDS:
        if field not in stream:
            invalid.append(f"stream.{field}")
    read_frames = _integer(stream.get("nb_read_frames"))
    reorder_depth = _integer(stream.get("has_b_frames"))
    if "nb_read_frames" in stream and read_frames is None:
        invalid.append("stream.nb_read_frames")
    if "has_b_frames" in stream and reorder_depth is None:
        invalid.append("stream.has_b_frames")

    frames = []
    for position, frame in enumerate(frames_in):
        if not isinstance(frame, dict):
            invalid.append(f"frames[{position}]")
            continue
        missing = [field for field in REQUIRED_FRAME_FIELDS if field not in frame]
        if missing:
            invalid.extend(f"frames[{position}].{field}" for field in missing)
            continue
        owner = _integer(frame.get("stream_index"))
        if owner is None:
            invalid.append(f"frames[{position}].stream_index")
            continue
        if frame.get("media_type") != "video" or owner != selected_index:
            continue  # another properly bound stream's frame: not ours, and not a defect
        pts, dts = _integer(frame.get("pts")), _integer(frame.get("pkt_dts"))
        interlaced = _integer(frame.get("interlaced_frame"))
        if pts is None:
            invalid.append(f"frames[{position}].pts")
        if dts is None:
            invalid.append(f"frames[{position}].pkt_dts")
        if interlaced is None:
            invalid.append(f"frames[{position}].interlaced_frame")
        if not isinstance(frame.get("pict_type"), str) or not frame["pict_type"]:
            invalid.append(f"frames[{position}].pict_type")
        frames.append(dict(pts=pts, dts=dts, interlaced=interlaced,
                           pict_type=frame.get("pict_type")))
    if invalid:
        return _invalid(sorted(set(invalid)), selected_index)
    if not frames:
        return _invalid(["no_frame_bound_to_the_selected_video_stream"], selected_index)

    pts = [frame["pts"] for frame in frames]
    checks = {
        # (i) the decoder counted as many frames as this module rebuilt access units *with a primary
        # coded picture* - parameter-set-only units are not frames.
        "frame_count_matches_coded_slice_access_units":
            read_frames == report.get("coded_slice_access_units") == len(frames),
        # (ii)/(iii) no reordering: no B picture, zero reorder depth, and presentation equal to decode.
        # A picture kind this path has not qualified - "B", the reordered "BI", the switching "S",
        # "SI", "SP", or the decoder's unknown "?" - is never read as "not a B picture". Only the
        # finite qualified non-reordered kinds pass.
        "no_b_pictures": all(frame["pict_type"] in QUALIFIED_PICTURE_KINDS for frame in frames),
        "zero_reorder_depth": reorder_depth == 0,
        "presentation_equals_decode": all(frame["pts"] == frame["dts"] for frame in frames),
        "presentation_strictly_increasing": all(a < b for a, b in zip(pts, pts[1:])),
        # (iv) progressive only: a field-coded picture is one access unit per field.
        "progressive_only": (stream.get("field_order") == "progressive"
                             and all(frame["interlaced"] == 0 for frame in frames)),
        # (v) this module's own timing must be monotonic in decode order to be comparable at all.
        "rtp_timing_monotonic": bool(report.get("rtp_timing_monotonic")),
        # (vi) the actual join: decoder ticks equal the RTP elapsed ticks, unit by unit.
        "decoder_ticks_equal_rtp_ticks": _ticks_agree(report, pts, stream),
    }
    reasons = sorted(name for name, passed in checks.items() if not passed)
    return {"timing_equivalent": not reasons, "input_valid": True, "checks": checks,
            "failed_checks": reasons, "missing_or_invalid": [],
            "selected_stream_index": selected_index, "basis": JOIN_BASIS}


def _invalid(fields, stream_index):
    """Fail closed: required decoder metadata absent, malformed or ambiguous is never a success."""
    return {"timing_equivalent": False, "input_valid": False, "checks": {},
            "failed_checks": ["required_decoder_metadata"], "missing_or_invalid": list(fields),
            "selected_stream_index": stream_index, "basis": JOIN_INVALID_BASIS}


def _integer(value):
    """Exact integer from the probe's JSON (ffprobe writes numbers as strings); never a guess."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return None


def _ticks_agree(report, pts, stream):
    """Decoder PTS ticks against the RTP elapsed ticks of the coded-slice access units."""
    if str(stream.get("time_base")) != f"1/{report.get('clock_rate_hz')}":
        return False
    coded = [unit for unit in report.get("access_units", [])
             if any(kind in CODED_SLICE for kind in unit["nal_types"])]
    if len(coded) != len(pts) or not coded:
        return False
    base = coded[0].get("rtp_ticks_since_first")
    if base is None:
        return False
    for unit, value in zip(coded, pts):
        ticks = unit.get("rtp_ticks_since_first")
        if ticks is None or value != ticks - base:
            return False
    return True


def _prerequisites(units):
    """(parameter set types seen before the first coded slice, access units carrying a coded slice)."""
    seen, slice_units, before_first_slice = [], [], True
    for unit in units:
        # One membership decision per unit instead of a growing-list lookup per NAL, but the
        # "before the first slice" flag still flips at the slice itself: parameter sets that follow a
        # slice *inside the same access unit* are late, exactly as they are in a later unit.
        carries_slice = False
        for kind in unit["nal_types"]:
            if kind in CODED_SLICE:
                carries_slice = True
                before_first_slice = False
            elif before_first_slice and kind in (NAL_SPS, NAL_PPS) and kind not in seen:
                seen.append(kind)
        if carries_slice:
            slice_units.append(unit)
    return seen, slice_units


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild an offline H.264 elementary stream from saved RTP mode 1 payloads.")
    parser.add_argument("input", type=Path, help="saved classic PCAP capture")
    parser.add_argument("new_output_directory", type=Path,
                        help="directory to create; existing inputs and evidence are never overwritten")
    parser.add_argument("--udp-destination-port", type=int, required=True)
    parser.add_argument("--payload-type", type=int, required=True,
                        help="the declared dynamic RTP payload type carrying H.264")
    parser.add_argument("--ssrc", type=lambda v: int(v, 0), help="select exactly one SSRC")
    args = parser.parse_args()
    try:
        data = read_bounded(args.input)  # bounded before allocation
        stream, report = depacketize(data, args.udp_destination_port,
                                     payload_type=args.payload_type, ssrc=args.ssrc)
        args.new_output_directory.mkdir()  # Preserve existing inputs and evidence.
        (args.new_output_directory / "elementary.h264").write_bytes(stream)
        (args.new_output_directory / "access_units.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, ImportError):
        parser.exit(2, "saved RTP H.264 depacketization failed; nothing was written\n")
    print(f"rebuilt {report['elementary_bytes']} elementary stream bytes from "
          f"{report['access_unit_count']} access units "
          f"({report['retained_packets']} retained packets, "
          f"{report['unsupported_packets']} unsupported, {report['unselected_packets']} unselected)")


if __name__ == "__main__":
    main()
