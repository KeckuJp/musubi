#!/usr/bin/env python3
"""Saved MAVLink LOGGING_DATA fragments -> the saved PX4 ULog, passively; never a command or a device.

Reads an already saved TLOG (8 byte big-endian host microseconds plus one MAVLink1/MAVLink2 frame,
the framing the existing TLOG reader uses) and rebuilds the ULog file that the vehicle streamed in
LOGGING_DATA (266) / LOGGING_DATA_ACKED (267). START/STOP/ACK are never emitted, no socket is opened
and the upstream streaming tool is never executed. Losses that cannot be reconstructed are refused
with a reason code instead of being padded, re-timed or resynchronised.

Contract, bounds and the unreconstructable cases are fixed in
docs/saved-inputs.md.
"""
import argparse
import hashlib
import json
from pathlib import Path

MAVLINK1_STX = 0xFE
MAVLINK2_STX = 0xFD
LOGGING_DATA = 266
LOGGING_DATA_ACKED = 267
LOGGING_MESSAGES = {LOGGING_DATA: "LOGGING_DATA", LOGGING_DATA_ACKED: "LOGGING_DATA_ACKED"}
LOGGING_PAYLOAD_LEN = 255
LOGGING_DATA_LEN = 249
NO_MESSAGE_START = 255
SEQUENCE_MODULO = 1 << 16
REORDER_GUARD = 1 << 15  # Upstream receiver's own forward/backward split.
ULOG_MAGIC = bytes([0x55, 0x4C, 0x6F, 0x67, 0x01, 0x12, 0x35, 0x01])
ULOG_HEADER_LEN = 16  # magic and version, then uint64 start timestamp.
SCHEMA = "musubi.saved-ulog-reassembly/v1"

# Bounds are refused loudly, never silently truncated. Full 249 byte fragments reach the 32 MiB output
# bound first (about 135k fragments); sparsely filled ones reach the fragment bound first (about 12 MiB
# of output). A normal selected capture passes well inside all four.
MAX_INPUT_BYTES = 64 * 1024 * 1024  # peak input memory is this plus one byte, whatever the file's size
MAX_RECORDS = 2_000_000
# Largest possible saved record: MAVLink2 header, a full payload, the CRC and an optional signature. No
# slice taken while reading records may exceed it, which is what keeps the pass linear.
MAX_FRAME_BYTES = 10 + 255 + 2 + 13
MAX_FRAGMENTS = 200_000
MAX_ULOG_BYTES = 32 * 1024 * 1024

# Wire order of 266/267 (MAVLink size-sorted, identical for both): the layout the CRC_EXTRA is
# derived from, so neither extra is a remembered magic number.
LOGGING_FIELDS = (
    ("uint16_t", "sequence", 0),
    ("uint8_t", "target_system", 0),
    ("uint8_t", "target_component", 0),
    ("uint8_t", "length", 0),
    ("uint8_t", "first_message_offset", 0),
    ("uint8_t", "data", LOGGING_DATA_LEN),
)
PUBLISHED_LOGGING_DATA_CRC_EXTRA = 193  # mavlink_msg_logging_data.h; the derivation must reproduce it.


class Refused(ValueError):
    """A saved capture that cannot be reconstructed. `code` is a fixed token, never input content."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def crc_x25(data, crc=0xFFFF):
    """CRC-16/MCRF4XX, the MAVLink frame and CRC_EXTRA accumulator."""
    for byte in data:
        tmp = (byte ^ (crc & 0xFF)) & 0xFF
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def crc_extra(name, fields):
    """Published CRC_EXTRA derivation: message name, then each wire-ordered field, arrays add their length."""
    crc = crc_x25((name + " ").encode("ascii"))
    for field_type, field_name, count in fields:
        crc = crc_x25((field_type + " ").encode("ascii"), crc)
        crc = crc_x25((field_name + " ").encode("ascii"), crc)
        if count:
            crc = crc_x25(bytes([count]), crc)
    return ((crc & 0xFF) ^ (crc >> 8)) & 0xFF


LOGGING_CRC_EXTRA = {msgid: crc_extra(name, LOGGING_FIELDS) for msgid, name in LOGGING_MESSAGES.items()}
if LOGGING_CRC_EXTRA[LOGGING_DATA] != PUBLISHED_LOGGING_DATA_CRC_EXTRA:  # pragma: no cover - import guard
    raise AssertionError("CRC_EXTRA derivation disagrees with the published LOGGING_DATA value")


def read_bounded(path, limit=None):
    """Read at most one byte past the bound, so an oversized saved capture is refused without reading it.

    The bound is enforced *before* the bytes exist in memory: an arbitrarily large file costs one
    bounded read, not its own size.
    """
    limit = MAX_INPUT_BYTES if limit is None else limit
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused("INPUT_TOO_LARGE")
    return data


def iter_records(data):
    """Yield one dict per saved TLOG record, in recording order. Fail closed on any framing doubt.

    Every slice taken here stays inside the record being read (at most [`MAX_FRAME_BYTES`]), so the work
    is linear in the capture: the remaining tail is never copied. Payload and CRC input are handed out
    as views; only the assembler materialises the bytes it keeps.
    """
    view = memoryview(data)
    size = len(view)
    if size > MAX_INPUT_BYTES:
        raise Refused("INPUT_TOO_LARGE")
    offset = 0
    records = 0
    while offset < size:
        if records >= MAX_RECORDS:
            raise Refused("TOO_MANY_RECORDS")
        if size - offset < 8 + 6:
            raise Refused("TRUNCATED_RECORD")
        recorded_word = int.from_bytes(view[offset:offset + 8], "big")
        frame = offset + 8  # absolute start of the MAVLink frame
        stx = view[frame]
        if stx == MAVLINK2_STX:
            if size - frame < 10:
                raise Refused("TRUNCATED_FRAME")
            payload_len = view[frame + 1]
            incompat = view[frame + 2]
            if incompat & ~0x01:
                raise Refused("UNSUPPORTED_INCOMPAT_FLAG")
            signed = bool(incompat & 0x01)
            sequence = view[frame + 4]
            sysid, compid = view[frame + 5], view[frame + 6]
            msgid = int.from_bytes(view[frame + 7:frame + 10], "little")
            header_len = 10
        elif stx == MAVLINK1_STX:
            payload_len = view[frame + 1]
            signed = False
            sequence = view[frame + 2]
            sysid, compid = view[frame + 3], view[frame + 4]
            msgid = view[frame + 5]
            header_len = 6
        else:
            raise Refused("UNEXPECTED_FRAME_START")
        frame_bytes = header_len + payload_len + 2 + (13 if signed else 0)
        if size - frame < frame_bytes:
            raise Refused("FRAME_EXCEEDS_INPUT")
        payload_at = frame + header_len
        crc_at = payload_at + payload_len
        yield {
            "tlog_offset": offset,
            # The recorded 8 byte word is kept raw: its low two bits may be a writer link tag, so it is
            # not promoted to a host clock here.
            "recorded_timestamp_word": recorded_word,
            "mavlink_version": 2 if stx == MAVLINK2_STX else 1,
            "mavlink_sequence": sequence,
            "sysid": sysid,
            "compid": compid,
            "msgid": msgid,
            "signed_frame": signed,
            "wire_payload": view[payload_at:crc_at],
            "crc": int.from_bytes(view[crc_at:crc_at + 2], "little"),
            "crc_input": view[frame + 1:crc_at],
            "frame_bytes": frame_bytes,
        }
        offset += 8 + frame_bytes
        records += 1


def _logging_fragment(record):
    """Verify one 266/267 frame and read its fields. MAVLink2 trailing zeros are restored, not guessed.

    Only MAVLink2 frames can carry these ids at all (a MAVLink1 message id is one byte), so MAVLink1
    records in the same saved capture are unrelated traffic and are accounted as such.
    """
    extra = LOGGING_CRC_EXTRA[record["msgid"]]
    if crc_x25(bytes([extra]), crc_x25(record["crc_input"])) != record["crc"]:
        raise Refused("CRC_MISMATCH")
    # One bounded payload (at most 255 bytes) becomes bytes here; MAVLink2 truncation restores zeros only.
    padded = bytes(record["wire_payload"]).ljust(LOGGING_PAYLOAD_LEN, b"\x00")
    sequence = int.from_bytes(padded[0:2], "little")
    length = padded[4]
    first_message_offset = padded[5]
    if length > LOGGING_DATA_LEN:
        raise Refused("INVALID_LENGTH")
    if first_message_offset != NO_MESSAGE_START and first_message_offset > length:
        raise Refused("INVALID_FIRST_MESSAGE_OFFSET")
    return {
        "tlog_offset": record["tlog_offset"],
        "recorded_timestamp_word": record["recorded_timestamp_word"],
        "message": LOGGING_MESSAGES[record["msgid"]],
        "msgid": record["msgid"],
        "mavlink_version": record["mavlink_version"],
        "mavlink_sequence": record["mavlink_sequence"],
        "signed_frame": record["signed_frame"],
        "sysid": record["sysid"],
        "compid": record["compid"],
        "target_system": padded[2],
        "target_component": padded[3],
        "logging_sequence": sequence,
        "length": length,
        "first_message_offset": first_message_offset,
        "data": bytes(padded[6:6 + length]),
        "data_sha256": hashlib.sha256(padded[6:6 + length]).hexdigest(),
    }


def _reference(fragment, **placement):
    """One saved frame's reference: every read field except the raw bytes, which the digest stands for."""
    entry = {key: value for key, value in fragment.items() if key != "data"}
    entry.update(placement)
    return entry


def ulog_message_counts(data):
    """Walk the reconstructed ULog's message headers; refuse a tail that never completes."""
    if len(data) < ULOG_HEADER_LEN or not data.startswith(ULOG_MAGIC):
        raise Refused("NO_ULOG_HEADER")
    counts = {}
    offset = ULOG_HEADER_LEN
    while offset < len(data):
        if len(data) - offset < 3:
            raise Refused("TRUNCATED_ULOG_MESSAGE")
        size = int.from_bytes(data[offset:offset + 2], "little")
        kind = chr(data[offset + 2])
        if len(data) - offset - 3 < size:
            raise Refused("TRUNCATED_ULOG_MESSAGE")
        counts[kind] = counts.get(kind, 0) + 1
        offset += 3 + size
    return counts


def assemble(data, source=None):
    """Rebuild the streamed ULog from saved fragments. Returns (ulog_bytes, references).

    One pass over the capture: records are not kept after they are read, only the selected stream's
    references and the reconstruction itself grow, and the retention bounds are enforced while
    collecting rather than after materialising everything. The first refusal in recording order wins.
    """
    unrelated = {}
    senders = {}
    selected_source = source
    out = bytearray()
    retained = []
    previous_sequence = previous_length = previous_data = previous_offset = None
    records = logging_fragments = selected_fragments = 0
    duplicates = wraps = acked = 0
    for record in iter_records(data):
        records += 1
        if record["msgid"] not in LOGGING_MESSAGES:
            entry = unrelated.setdefault(str(record["msgid"]), {"frames": 0, "tlog_offsets": []})
            entry["frames"] += 1
            entry["tlog_offsets"].append(record["tlog_offset"])
            continue
        fragment = _logging_fragment(record)
        logging_fragments += 1
        sender = (fragment["sysid"], fragment["compid"])
        senders[sender] = senders.get(sender, 0) + 1
        if selected_source is None:
            selected_source = sender  # the first stream in recording order
        if sender != selected_source:
            if source is None:
                raise Refused("AMBIGUOUS_LOGGING_SOURCES")
            continue  # another sender's stream: counted, never merged
        selected_fragments += 1
        if selected_fragments > MAX_FRAGMENTS:
            raise Refused("TOO_MANY_FRAGMENTS")
        if fragment["msgid"] == LOGGING_DATA_ACKED:
            acked += 1
        if previous_sequence is None:
            if fragment["first_message_offset"] != 0:
                raise Refused("CAPTURE_STARTS_MID_MESSAGE")
        else:
            step = (fragment["logging_sequence"] - previous_sequence) % SEQUENCE_MODULO
            if step == 0:
                if fragment["data"] != previous_data or fragment["length"] != previous_length:
                    raise Refused("DUPLICATE_CONFLICT")
                duplicates += 1
                retained.append(_reference(fragment, collapsed_duplicate_of=previous_offset,
                                           output_start=None, output_end=None))
                continue
            if step != 1:
                raise Refused("SEQUENCE_GAP" if step <= REORDER_GUARD else "SEQUENCE_OUT_OF_ORDER")
            if fragment["logging_sequence"] < previous_sequence:
                wraps += 1
        start = len(out)
        out.extend(fragment["data"])
        if len(out) > MAX_ULOG_BYTES:
            raise Refused("ULOG_TOO_LARGE")
        retained.append(_reference(fragment, collapsed_duplicate_of=None,
                                   output_start=start, output_end=len(out)))
        previous_sequence = fragment["logging_sequence"]
        previous_length = fragment["length"]
        previous_data = fragment["data"]
        previous_offset = fragment["tlog_offset"]
    if not logging_fragments:
        raise Refused("NO_LOGGING_FRAGMENTS")
    if source is not None and source not in senders:
        raise Refused("SOURCE_NOT_PRESENT")
    other_sources = [
        {"sysid": sysid, "compid": compid, "fragments": count}
        for (sysid, compid), count in sorted(senders.items())
        if (sysid, compid) != selected_source
    ]

    ulog = bytes(out)
    counts = ulog_message_counts(ulog)
    references = {
        "schema": SCHEMA,
        "basis": "saved fragments only; reported ids and recorded timestamps are labels, not authenticated "
                 "identity, and a refusal is about the saved capture, not about the vehicle",
        "input": {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "records": records,
        },
        "selected_source": {"sysid": selected_source[0], "compid": selected_source[1]},
        "other_logging_sources": other_sources,
        "crc_extra_derived": {name: LOGGING_CRC_EXTRA[msgid] for msgid, name in LOGGING_MESSAGES.items()},
        "accounting": {
            "logging_fragments": logging_fragments,
            "selected_fragments": selected_fragments,
            "appended_fragments": selected_fragments - duplicates,
            "collapsed_duplicates": duplicates,
            "sequence_wraps": wraps,
            "acked_fragments": acked,
            "unacked_fragments": selected_fragments - acked,
            "unrelated_frames": sum(entry["frames"] for entry in unrelated.values()),
            "ulog_bytes": len(ulog),
            "ulog_message_counts": dict(sorted(counts.items())),
            "ulog_sha256": hashlib.sha256(ulog).hexdigest(),
        },
        "fragments": retained,
        "unrelated_messages": {msgid: unrelated[msgid] for msgid in sorted(unrelated, key=int)},
    }
    return ulog, references


def main():
    parser = argparse.ArgumentParser(description="Rebuild a saved PX4 ULog from saved MAVLink fragments.")
    parser.add_argument("input", type=Path, help="saved TLOG containing LOGGING_DATA/LOGGING_DATA_ACKED")
    parser.add_argument("new_output_directory", type=Path,
                        help="directory to create; existing inputs and evidence are never overwritten")
    parser.add_argument("--source", help="reported sender to select as SYSID:COMPID when several stream")
    args = parser.parse_args()
    try:
        source = None
        if args.source is not None:
            sysid, _, compid = args.source.partition(":")
            source = (int(sysid), int(compid))
            if not (0 <= source[0] <= 255 and 0 <= source[1] <= 255):
                raise ValueError("reported sender outside one byte")
        ulog, references = assemble(read_bounded(args.input), source=source)
        args.new_output_directory.mkdir()  # Preserve existing inputs and evidence.
        (args.new_output_directory / "reconstructed.ulg").write_bytes(ulog)
        (args.new_output_directory / "references.json").write_text(
            json.dumps(references, indent=2) + "\n", encoding="utf-8")
    except Refused as refusal:
        parser.exit(2, f"saved ULog reassembly refused: {refusal.code}\n")
    except (ValueError, OSError) as error:
        parser.exit(2, f"saved ULog reassembly failed: {type(error).__name__}\n")
    print(f"reconstructed {references['accounting']['ulog_bytes']} ULog bytes from "
          f"{references['accounting']['appended_fragments']} fragments "
          f"({references['accounting']['collapsed_duplicates']} duplicates collapsed, "
          f"{references['accounting']['unrelated_frames']} unrelated frames kept in the references)")


if __name__ == "__main__":
    main()
