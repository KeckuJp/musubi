#!/usr/bin/env python3
"""Saved Ping-protocol binary frames -> the decoded JSONL envelope the adopted converter reads.

This is the input step only. mm->m, the confidence rules, capture time and the common Observation
are already adopted in `convert_ardupilot_battery_csv.py --source-format ping1d-distance-1746`
and are reused unchanged; nothing semantic is repeated here.

Frame layout and checksum are taken from ping-python `brping/pingmessage.py` at
7f27d04588916fa0157374ca10625b1415117bb4, and the 1211/1212 payload fields from ping-protocol
`src/definitions/ping1d.json` at 1746cd03f942d58bcf08253055854caea2e33fda:

    0  start_1 = 0x42 'B'
    1  start_2 = 0x52 'R'
    2  payload_length u16 LE
    4  message_id     u16 LE
    6  src_device_id  u8
    7  dst_device_id  u8          (headerLength = 8, header_format "BBHHBB", endianess "<")
    8  payload[payload_length]
    8+n checksum      u16 LE      (checksumLength = 2)

The checksum is **not a CRC**: `sum(msg_data[0:headerLength + payload_length]) & 0xffff`, a plain
byte sum over the start bytes, header and payload, with the checksum bytes excluded.

Reads saved bytes from a file. **No serial, UDP, network or device access**, and no third-party
decoder is bundled or required. A valid checksum proves frame integrity, not device identity, not
firmware authenticity and not measurement correctness. A distance is a reported acoustic return,
never vehicle depth, altitude, calibrated accuracy or target identity.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import sys

PROTOCOL_COMMIT = "1746cd03f942d58bcf08253055854caea2e33fda"
PING_PYTHON_COMMIT = "7f27d04588916fa0157374ca10625b1415117bb4"

START = b"BR"
HEADER = struct.Struct("<BBHHBB")
HEADER_LENGTH = 8
CHECKSUM = struct.Struct("<H")
CHECKSUM_LENGTH = 2
# The adopted converter's own input/output bound; no smaller record cap is invented here.
LIMIT = 16 * 1024 * 1024

# Ordered exactly as ping1d.json declares them at the pinned commit. Units are carried by name to
# the adopted layer, which already owns every conversion.
SELECTED = {
    1211: (struct.Struct("<IB"), ("distance", "confidence")),
    1212: (struct.Struct("<IHHIIII"),
           ("distance", "confidence", "transmit_duration", "ping_number", "scan_start",
            "scan_length", "gain_setting")),
}


def new_report():
    return {"frames_read": 0, "records_emitted": 0, "unselected_messages": {},
            "protocol_commit": PROTOCOL_COMMIT, "framing_commit": PING_PYTHON_COMMIT}


def decode_frames(raw, capture_time_us, report, allow_other_ids=False):
    """Yield one envelope dict per selected frame, counting everything into `report`.

    Frames are read **contiguously from byte 0**: each begins exactly where the previous ended.
    There is no scan for the start bytes and no resynchronisation, so corruption can never be
    skipped past and counted as success.
    """
    if not raw:
        raise ValueError("saved Ping capture is empty")
    offset = 0
    while offset < len(raw):
        index = report["frames_read"]
        if len(raw) - offset < HEADER_LENGTH + CHECKSUM_LENGTH:
            raise ValueError(f"frame {index} is truncated before its header and checksum")
        if raw[offset:offset + 2] != START:
            raise ValueError(f"frame {index} does not start with the Ping start bytes; this "
                             "reader never rescans for them, so nothing is skipped silently")
        _, _, payload_length, message_id, src, dst = HEADER.unpack_from(raw, offset)
        end = offset + HEADER_LENGTH + payload_length + CHECKSUM_LENGTH
        if end > len(raw):
            raise ValueError(f"frame {index} declares {payload_length} payload bytes that the "
                             "capture does not contain")
        frame = raw[offset:end]
        body = frame[:HEADER_LENGTH + payload_length]
        stored = CHECKSUM.unpack_from(frame, HEADER_LENGTH + payload_length)[0]
        if stored != sum(body) & 0xFFFF:
            raise ValueError(f"frame {index} checksum does not match its bytes")
        report["frames_read"] += 1
        offset = end
        if message_id not in SELECTED:
            if not allow_other_ids:
                raise ValueError(f"frame {index} carries message id {message_id}, which is outside "
                                 "the selected 1211/1212 distance responses")
            key = str(message_id)
            report["unselected_messages"][key] = report["unselected_messages"].get(key, 0) + 1
            continue
        layout, names = SELECTED[message_id]
        payload = body[HEADER_LENGTH:]
        if len(payload) < layout.size:
            raise ValueError(f"frame {index} payload is shorter than message {message_id} declares")
        record = {"protocol_commit": PROTOCOL_COMMIT, "capture_time_us": capture_time_us,
                  "message_id": message_id, "src_device_id": src, "dst_device_id": dst,
                  "payload": dict(zip(names, layout.unpack_from(payload, 0))),
                  "frame_index": index, "frame_sha256": hashlib.sha256(frame).hexdigest()}
        extra = payload[layout.size:]
        if extra:
            # Longer than the definition: kept and accounted rather than dropped, which is what the
            # pinned decoder silently does with the trailing bytes.
            record["payload_extra_hex"] = extra.hex()
        report["records_emitted"] += 1
        yield record


def convert(path, capture_time_us, allow_other_ids=False):
    """Saved capture -> (JSONL text, report). Bounded while writing."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us <= 2**63 - 1:
        raise ValueError("explicit caller capture microsecond timestamp required; the Ping frame "
                         "carries no time and none is invented")
    with Path(path).open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("saved Ping capture exceeds the supported size")
    output = io.StringIO(newline="")
    report = new_report()
    for record in decode_frames(raw, capture_time_us, report, allow_other_ids):
        output.write(json.dumps(record, sort_keys=True) + "\n")
        if output.tell() > LIMIT:
            raise ValueError("converted Ping JSONL exceeds bound; no frame was skipped or dropped")
    if not report["records_emitted"]:
        raise ValueError("no selected Ping distance response in the saved capture")
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    report["clock"] = "caller-declared capture time for the whole capture; frames carry none"
    report["scope"] = ("saved Ping frame validation and field extraction only; units, confidence "
                       "rules and common output remain the adopted decoded-report conversion")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved binary Ping-protocol frame capture")
    parser.add_argument("output", type=Path, help="JSONL destination for the adopted converter")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller-declared capture clock; the frame carries no timestamp")
    parser.add_argument("--allow-other-message-ids", action="store_true",
                        help="count message ids outside 1211/1212 in the report instead of refusing")
    args = parser.parse_args()
    try:
        text, report = convert(args.input, args.capture_time_us, args.allow_other_message_ids)
        if args.output.exists():
            raise ValueError("destination already exists")
        args.output.write_text(text)
    except (OSError, ValueError, struct.error, KeyError):
        print("saved Ping frame conversion failed; no successful conversion claim", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
