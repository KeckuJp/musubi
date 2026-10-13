"""Saved can-utils2023.03 absolute log format to the existing classic CAN CSV."""
import argparse
import csv
import io
import json
from pathlib import Path
import re
import sys

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LIMIT = 16 * 1024 * 1024
LINE = re.compile(r"\(([0-9]{10,19})\.([0-9]{6})\) +([A-Za-z0-9_.:-]{1,15}) +([0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})#(\S*)(?: ([RT]))?")


def convert(text, interface, *, extended=False, fd=False, data_frame_type=False):
    if type(data_frame_type) is not bool or (data_frame_type and (extended or fd)):
        raise ValueError("explicit DATA-kind output is for classic standard frames only")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) or not text or len(text.encode()) > LIMIT:
        raise ValueError("explicit interface and bounded nonempty log required")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["time_us", "extended_id" if extended else "standard_id",
                     *(["fd_flags"] if fd else []), *(["frame_type"] if data_frame_type else []), "data_hex"])
    report = dict(source_lines=0, selected_frames=0, unconverted_lines=0,
                  clock="CAPTURE_REPORTED_NOT_AUTHENTICATED",
                  time_order_scope="SELECTED_INTERFACE_ONLY_OTHER_INTERFACES_RANGE_CHECKED_NOT_ORDERED",
                  records=[])
    previous, retained = None, 0
    for index, line in enumerate(text.splitlines(), 1):
        report["source_lines"] += 1
        entry = dict(line=index, original_hex="hex:" + line.encode().hex())
        match = LINE.fullmatch(line)
        if match is None:
            if re.fullmatch(r"DROPCOUNT: dropped [0-9]+ CAN frames? on '[A-Za-z0-9_.:-]{1,15}' socket \(total drops [0-9]+\)", line):
                entry["reason"] = "SOURCE_DROP_REPORT"
            else:
                raise ValueError("unsupported candump log line")
        else:
            seconds, micros, device, identifier, body, direction = match.groups()
            timestamp = int(seconds) * 1000000 + int(micros)
            if timestamp > 2**63 - 1:
                raise ValueError("out-of-range capture time")
            if device == interface:
                if previous is not None and timestamp < previous:
                    raise ValueError("decreasing capture time on the selected interface")
                previous = timestamp
            number, is_extended = int(identifier, 16), len(identifier) == 8
            if number > (0x3fffffff if is_extended else 0x7ff):
                raise ValueError("invalid CAN identifier")
            if re.fullmatch(r"R[0-8]?", body):
                reason = "REMOTE_REQUEST"
            elif re.fullmatch(r"#[0-9A-Fa-f](?:[0-9A-Fa-f]{2}){0,64}", body):
                reason = "ERROR_FRAME" if number & 0x20000000 else None if fd else "CAN_FD"
            elif re.fullmatch(r"(?:[0-9A-Fa-f]{2}){0,8}", body):
                reason = "ERROR_FRAME" if number & 0x20000000 else "UNSELECTED_CLASSIC" if fd else None
            else:
                raise ValueError("unsupported payload or raw DLC representation")
            if reason is None:
                if device != interface:
                    reason = "UNSELECTED_INTERFACE"
                elif is_extended != extended:
                    reason = "UNSELECTED_FRAME_KIND"
                elif extended and not body and not fd:
                    reason = "EMPTY_EXTENDED_NOT_IN_DOWNSTREAM_SCOPE"
            entry.update(interface=device, direction_reported=direction, capture_time_us=timestamp)
            if reason is None:
                writer.writerow([timestamp, identifier, body[1], body[2:]] if fd else
                                [timestamp, identifier, *(["DATA"] if data_frame_type else []), body])
                report["selected_frames"] += 1
                entry["selected_row"] = report["selected_frames"]
                entry["reason"] = "SELECTED_FD_DATA" if fd else "SELECTED_CLASSIC_DATA"
            else:
                entry["reason"] = reason
        if "selected_row" not in entry:
            report["unconverted_lines"] += 1
        retained += len(json.dumps(entry).encode())
        if retained > LIMIT or output.tell() > LIMIT:
            raise ValueError("expanded capture exceeds bound")
        report["records"].append(entry)
    if not report["selected_frames"]:
        raise ValueError("no selected data frames")
    return output.getvalue(), report


def convert_path(path, interface, *, start_line, stop_line, extended=False, fd=False,
                 expected_size=None, expected_sha256=None, data_frame_type=False):
    """Reuse the bounded physical-line selector; outside CAN semantics are not decoded."""
    import hashlib
    from scripts.convert_vda_load_report import read_jsonl_window, expected_snapshot
    selected, offsets, count, selected_bytes, digest = read_jsonl_window(
        path, start_record=start_line, stop_record=stop_line,
        expected_size=expected_size, expected_sha256=expected_sha256)
    text = "".join(selected)
    output, report = convert(text, interface, extended=extended, fd=fd, data_frame_type=data_frame_type)
    for entry in report["records"]:
        local = entry["line"] - 1
        entry.update(original_line=start_line + local + 1, original_byte_offset=offsets[local])
    report["source_window"] = dict(source_sha256="sha256:" + digest,
        start_line=start_line, stop_line=stop_line, total_physical_lines=count,
        selected_physical_lines=len(selected), excluded_physical_lines=count-len(selected),
        selected_bytes=selected_bytes, selected_normalized_sha256="sha256:"+hashlib.sha256(text.encode()).hexdigest(),
        outside_semantics="NOT_DECODED_FRAMING_AND_UTF8_CHECKED_ONLY",
        line_basis="ZERO_BASED_HALF_OPEN_PHYSICAL_LINES_ORIGINAL_LINE_IS_ONE_BASED",
        source_retention="original supplied file required; full hash is not authentication",
        input_expectation=expected_snapshot(expected_size, expected_sha256))
    if len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("expanded window capture report exceeds bound")
    return output, report


def convert_slcan(text, interface, capture_time_us, *, extended=False, timestamps=False):
    """Fixed can-utils slcanpty can2pty output; no transport or clock unwrapping."""
    if (type(capture_time_us) is not int or not 10**15 <= capture_time_us <= 2**63 - 1
            or not text or len(text.encode()) > LIMIT or not text.endswith("\r")):
        raise ValueError("bounded complete responses and explicit batch capture time required")
    frames, originals = [], []
    seconds, micros = divmod(capture_time_us, 1000000)
    for response in text[:-1].split("\r"):
        match = re.fullmatch(r"([tr])([0-9A-F]{3})([0-8])([0-9A-F]*)|([TR])([0-9A-F]{8})([0-8])([0-9A-F]*)", response)
        if match is None:
            raise ValueError("unsupported saved slcanpty response")
        command, identifier, dlc, payload = match.groups()[:4] if match[1] else match.groups()[4:]
        size = int(dlc) * 2
        if int(identifier, 16) > (0x7ff if command.islower() else 0x1fffffff):
            raise ValueError("out-of-range CAN identifier")
        if len(payload) != size + (4 if timestamps else 0):
            raise ValueError("payload length or declared timestamp mode mismatch")
        modulo_ms = int(payload[size:], 16) if timestamps else None
        if modulo_ms is not None and modulo_ms >= 60000:
            raise ValueError("slcanpty timestamp is milliseconds within minute")
        body = "R" + dlc if command.lower() == "r" else payload[:size]
        frames.append(f"({seconds}.{micros:06d}) {interface} {identifier}#{body}")
        originals.append((response, modulo_ms))
    output, report = convert("\n".join(frames), interface, extended=extended)
    report.update(source_format="can-utils-2023.03-slcanpty", clock="DECLARED_BATCH_CAPTURE_NOT_BUS_EVENT",
                  timestamp_mode="MILLISECOND_WITHIN_MINUTE" if timestamps else "NOT_REPORTED")
    retained = 0
    for entry, (response, modulo_ms) in zip(report["records"], originals):
        entry["original_hex"] = "hex:" + (response + "\r").encode().hex()
        entry["reported_millisecond_within_minute"] = modulo_ms
        retained += len(json.dumps(entry).encode())
        if retained > LIMIT:
            raise ValueError("expanded capture exceeds bound")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--fd", action="store_true", help="select saved FD frames and preserve the flags nibble")
    parser.add_argument("--data-frame-type", action="store_true",
                        help="emit explicit DATA kind for selected classic standard saved candump frames")
    parser.add_argument("--source-format", choices=("candump", "slcanpty-2023.03"), default="candump")
    parser.add_argument("--capture-time-us", type=int, help="explicit batch capture time for saved slcanpty")
    parser.add_argument("--slcan-timestamps", action="store_true", help="saved slcanpty writer had timestamps enabled")
    parser.add_argument("--start-line", type=int, help="zero-based saved physical line window start")
    parser.add_argument("--stop-line", type=int, help="exclusive saved physical line window end")
    parser.add_argument("--expected-size", type=int, help="optional original byte count, paired with SHA256")
    parser.add_argument("--expected-sha256", help="optional original lowercase SHA256, paired with size")
    args = parser.parse_args()
    try:
        window = args.start_line is not None or args.stop_line is not None
        if (args.expected_size is not None or args.expected_sha256 is not None) and not window:
            raise ValueError("expected snapshot requires explicit saved line window")
        if not window and args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        if window:
            if args.source_format != "candump" or args.capture_time_us is not None or args.slcan_timestamps:
                raise ValueError("line window requires saved candump absolute format")
            output, report = convert_path(args.input, args.interface, start_line=args.start_line,
                stop_line=args.stop_line, extended=args.extended, fd=args.fd,
                expected_size=args.expected_size, expected_sha256=args.expected_sha256,
                data_frame_type=args.data_frame_type)
        elif args.source_format == "slcanpty-2023.03":
            if args.fd or args.data_frame_type:
                raise ValueError("slcanpty scope is classic CAN")
            output, report = convert_slcan(args.input.read_bytes().decode("ascii"), args.interface,
                                          args.capture_time_us, extended=args.extended,
                                          timestamps=args.slcan_timestamps)
        else:
            if args.capture_time_us is not None or args.slcan_timestamps:
                raise ValueError("slcan options require explicit source format")
            output, report = convert(args.input.read_text(encoding="utf-8"), args.interface,
                                     extended=args.extended, fd=args.fd, data_frame_type=args.data_frame_type)
        args.output_directory.mkdir()
        (args.output_directory / "capture.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError):
        parser.exit(2, "saved candump conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
