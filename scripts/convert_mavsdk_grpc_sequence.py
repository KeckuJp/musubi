#!/usr/bin/env python3
"""Saved, already-reassembled single-RPC message body; no transport or SDK execution."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import zlib

try:
    from scripts.convert_ardupilot_battery_csv import convert as convert_message
    from scripts.convert_mavsdk_telemetry_recorded import decode as decode_basic
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.convert_ardupilot_battery_csv import convert as convert_message
    from scripts.convert_mavsdk_telemetry_recorded import decode as decode_basic

INPUT_LIMIT = 16 * 1024**2
CSV_LIMIT = 16 * 1024**2
REPORT_LIMIT = 64 * 1024**2
MESSAGE_LIMIT = 4096
FRAME_LIMIT = 14000  # Adopted decoder's complete frame hex limit / 2.
PROTO_COMMIT = "d3cb35f49d1379d2d95a6d22cdbe40ff4074e9bf"
METHODS = {
    "/mavsdk.rpc.telemetry.TelemetryService/SubscribeBattery": "mavsdk-battery-grpc",
    "/mavsdk.rpc.telemetry.TelemetryService/SubscribeAttitudeEuler": "mavsdk-attitude-grpc",
    **{"/mavsdk.rpc.telemetry.TelemetryService/Subscribe" + method: kind for method, kind in (
        ("Position", "position"), ("VelocityNed", "velocity"), ("FlightMode", "flight_mode"),
        ("Armed", "armed"), ("InAir", "in_air"), ("LandedState", "landed_state"), ("Health", "health"))},
}
EXTRA_FIELDS = ("source_stream_sha256", "source_message_index", "source_byte_offset",
                "source_frame_bytes", "source_frame_sha256", "rpc_method_hex", "grpc_sequence_basis")
BASIS = "CALLER_SELECTED_SINGLE_RPC_REASSEMBLED_BODY_NOT_TYPE_AUTHENTICATION_OR_RPC_SUCCESS"


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def report_text(report):
    text = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if len(text.encode("utf-8")) > REPORT_LIMIT:
        raise ValueError("paired original-body report exceeds bound")
    return text


def convert(data, *, rpc_method, sdk_version, proto_commit, capture_time_us, message_encoding=None, source_id=None):
    """Return CSV plus mandatory original-body report, or refuse the whole input."""
    if type(data) is not bytes or not data or len(data) > INPUT_LIMIT:
        raise ValueError("nonempty bounded saved response-body bytes required")
    if not isinstance(rpc_method, str) or rpc_method not in METHODS:
        raise ValueError("explicit qualified single RPC method required")
    if sdk_version != "3.17.4" or proto_commit != PROTO_COMMIT:
        raise ValueError("unqualified SDK/protobuf declaration")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("capture_time_us must be a nonnegative signed63 integer")
    if message_encoding not in (None, "gzip"):
        raise ValueError("only explicit gzip or absent message encoding is qualified")
    kind = METHODS[rpc_method]
    basic = not kind.startswith("mavsdk-")
    if basic and (not isinstance(source_id, str) or not source_id or len(source_id.encode()) > 256
                  or any(ord(c) < 32 for c in source_id)):
        raise ValueError("explicit bounded caller source_id required for basic response methods")
    digest = sha(data)
    output = io.StringIO(newline="")
    writer = None
    frames = []
    offset = 0
    # DATA boundaries have already been reassembled by the caller. Only the
    # protocol's Length-Prefixed-Message boundary is interpreted here.
    while offset < len(data):
        if len(frames) >= MESSAGE_LIMIT:
            raise ValueError("saved message count exceeds bound")
        if len(data) - offset < 5:
            raise ValueError("incomplete gRPC message prefix")
        compressed = data[offset]
        if compressed not in (0, 1) or compressed and message_encoding != "gzip":
            raise ValueError("gRPC flag requires qualified explicit message encoding")
        size = 5 + int.from_bytes(data[offset + 1:offset + 5], "big")
        if size > FRAME_LIMIT:
            raise ValueError("declared gRPC frame exceeds adopted decoder bound")
        if size > len(data) - offset:
            raise ValueError("incomplete gRPC message body")
        wire = data[offset:offset + size]
        decoded_wire = wire
        if compressed:
            # A fresh context per message, positive bounded output; never unlimited
            # decompress/flush. This selected path accepts exactly one gzip member.
            decoder = zlib.decompressobj(31)
            try:
                payload = decoder.decompress(wire[5:], FRAME_LIMIT - 4)
            except zlib.error as exc:
                raise ValueError("invalid gzip message") from exc
            if len(payload) > FRAME_LIMIT - 5:
                raise ValueError("expanded gRPC message exceeds adopted decoder bound")
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("gzip message incomplete, concatenated or trailing data")
            decoded_wire = b"\x00" + len(payload).to_bytes(4, "big") + payload
        envelope = json.dumps(dict(sdk_version=sdk_version, proto_commit=proto_commit,
                                   capture_time_us=capture_time_us, grpc_frame_hex=decoded_wire.hex()),
                              separators=(",", ":"), ensure_ascii=True)
        # Use the existing fixed descriptors, sentinels, enum handling, unit
        # conversions and original-derived-envelope retention without copying them.
        if basic:
            if kind == "velocity":
                rows = list(csv.DictReader(io.StringIO(convert_message(envelope, "mavsdk-velocity-ned-grpc"))))
            else:
                rows = [decode_basic(json.loads(envelope), kind)]
            if len(rows) != 1:
                raise ValueError("adopted basic resolver row accounting changed")
            rows[0].update(record_time_us=capture_time_us, source_id_hex="hex:" + source_id.encode().hex(),
                           message_kind=kind, source_record_hex="hex:" + envelope.encode().hex(),
                           interpretation_basis="CALLER_SOURCE_UNAUTHENTICATED_CAPTURE_UNKNOWN_NO_RPC_SUCCESS_NO_PHYSICAL_STATE_GUARANTEE")
            names = list(rows[0])
        else:
            decoded = csv.DictReader(io.StringIO(convert_message(envelope, kind)))
            rows = list(decoded)
            names = decoded.fieldnames
        if len(rows) != 1 or not names or set(names) & set(EXTRA_FIELDS):
            raise ValueError("selected decoder row/header accounting changed")
        if writer is None:
            writer = csv.DictWriter(output, [*names, *EXTRA_FIELDS], lineterminator="\n")
            writer.writeheader()
        elif list(names) != writer.fieldnames[:-len(EXTRA_FIELDS)]:
            raise ValueError("selected decoder header changed inside one RPC body")
        index = len(frames)
        frame_digest = sha(wire)
        rows[0].update(zip(EXTRA_FIELDS, (digest, index, offset, size, frame_digest,
                                        "hex:" + rpc_method.encode().hex(), BASIS)))
        writer.writerow(rows[0])
        # All selected output is ASCII (binary and free text are hex-encoded),
        # so StringIO's count is the exact UTF8 byte count. Check final encoding too.
        if output.tell() > CSV_LIMIT:
            raise ValueError("converted CSV exceeds bound; supply a smaller complete body")
        frames.append(dict(index=index, offset=offset, frame_bytes=size, sha256=frame_digest))
        if message_encoding is not None:
            frames[-1].update(compressed_flag=compressed, decoded_frame_bytes=len(decoded_wire),
                              decoded_frame_sha256=sha(decoded_wire))
        offset += size
    text = output.getvalue()
    if len(text.encode("utf-8")) > CSV_LIMIT:
        raise ValueError("converted CSV exceeds byte bound")
    report = dict(format="mavsdk-grpc-sequence-v1", source_stream_sha256=digest,
                  source_stream_hex="hex:" + data.hex(), input_bytes=len(data), accounted_bytes=offset,
                  message_count=len(frames), output_rows=len(frames), frames=frames,
                  sdk_version=sdk_version, proto_commit=proto_commit, rpc_method=rpc_method,
                  capture_time_us=capture_time_us, capture_clock_basis="Unknown",
                  capture_basis="ONE_CALLER_CAPTURE_FOR_ALL_MESSAGES_NO_ARRIVAL_SPACING_INFERRED",
                  source_record_basis="DERIVED_JSON_ENVELOPE_ORIGINAL_BYTES_IN_THIS_PAIRED_REPORT",
                  selection_basis=BASIS, output_csv_sha256=sha(text.encode("utf-8")))
    if message_encoding is not None:
        report.update(message_encoding=message_encoding,
                      message_encoding_basis="CALLER_DECLARED_HEADER_NOT_CAPTURED_OR_AUTHENTICATED",
                      compression_scope="ONE_COMPLETE_GZIP_MEMBER_PER_FLAG1_MESSAGE",
                      source_record_basis="DERIVED_UNCOMPRESSED_FRAME_JSON_ORIGINAL_BODY_IN_THIS_PAIRED_REPORT")
    report_text(report)  # API and CLI share the exact serialized report bound.
    return text, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--rpc-method", required=True, choices=METHODS)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--proto-commit", required=True)
    parser.add_argument("--capture-time-us", required=True, type=int)
    parser.add_argument("--message-encoding", choices=("gzip",))
    parser.add_argument("--source-id", help="Required caller-declared identity for basic response methods; not authenticated")
    args = parser.parse_args()
    try:
        if args.output_dir.exists():
            raise ValueError("output directory already exists; no overwrite")
        with args.input.open("rb") as source:
            data = source.read(INPUT_LIMIT + 1)
        text, report = convert(data, rpc_method=args.rpc_method, sdk_version=args.sdk_version,
                               proto_commit=args.proto_commit, capture_time_us=args.capture_time_us,
                               message_encoding=args.message_encoding, source_id=args.source_id)
        serialized = report_text(report)
        args.output_dir.mkdir()  # No output before complete semantic/framing validation.
        (args.output_dir / "observations.csv").write_text(text, encoding="utf-8")
        (args.output_dir / "report.json").write_text(serialized, encoding="utf-8")
    except (ValueError, OSError) as exc:
        print(f"refused: {exc}; if output directory exists it may be partial", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
