#!/usr/bin/env python3
"""Passive saved SmartAudio response interpretation; no device or command interface."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from convert_crsf_link import crc8

BASIS = "bf452-024f8e13-smartaudio21"
LIMIT = 16 * 1024 * 1024
FIELDS = ["record_time_us", "source_record_hex", "source_capture_sha256", "source_record_index",
          "source_frame_hex", "vtx_response_code", "vtx_disposition", "vtx_frequency_field_hz",
          "vtx_current_power_reported_dbm", "vtx_channel_raw", "vtx_legacy_power_raw",
          "vtx_mode_raw", "vtx_mode_unknown_bits", "vtx_frequency_mode", "vtx_pit_mode_reported",
          "vtx_in_range_pit_flag", "vtx_out_range_pit_flag", "vtx_unlocked_flag",
          "vtx_deferred_frequency_flag", "vtx_supported_power_bytes_hex", "vtx_value_basis"]


def convert(text, source_basis):
    if source_basis != BASIS:
        raise ValueError("fixed caller-selected SmartAudio consumer basis required")
    raw = text.encode("utf-8")
    if len(raw) > LIMIT:
        raise ValueError("capture exceeds 16 MiB; split at complete record boundaries")
    source_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(io.StringIO(text), strict=True)
    names = reader.fieldnames
    if (not names or names[:2] != ["time_us", "frame_hex"] or len(names) != len(set(names)) or
            len(names) > 64 or any(not name or len(name) > 128 for name in names)):
        raise ValueError("expected unique time_us,frame_hex and optional retained columns")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    report = dict(source_records=0, decoded_records=0, unsupported_records=0, clock="Unknown",
                  source_capture_sha256=source_sha, source_basis=source_basis)
    previous = None
    for index, record in enumerate(reader):
        if None in record or any(v is None for v in record.values()):
            raise ValueError("capture row width mismatch")
        if not re.fullmatch(r"[0-9]{1,19}", record["time_us"]):
            raise ValueError("invalid recorder microseconds")
        time = int(record["time_us"])
        if time >= 2**63 or (previous is not None and time < previous):
            raise ValueError("recorder timestamp overflow/decrease")
        previous = time
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){5,24}", record["frame_hex"]):
            raise ValueError("complete bounded response hex required")
        frame = bytes.fromhex(record["frame_hex"])
        if frame[:2] != b"\xaa\x55" or len(frame) != 5 + frame[3] or frame[3] > 19:
            raise ValueError("invalid response preamble/length")
        if crc8(frame[2:-1]) != frame[-1]:
            raise ValueError("response CRC mismatch")
        row = dict.fromkeys(FIELDS, "")
        row.update(record_time_us=time, source_record_hex="hex:" + json.dumps(record, ensure_ascii=False).encode().hex(),
                   source_capture_sha256=source_sha, source_record_index=index,
                   source_frame_hex="hex:" + frame.hex(), vtx_response_code=frame[2])
        report["source_records"] += 1
        if frame[2] != 0x11:
            row["vtx_disposition"] = "UNSUPPORTED_RESPONSE"
            report["unsupported_records"] += 1
        else:
            payload = frame[4:-1]
            if len(payload) < 8 or len(payload) != 8 + payload[6]:
                raise ValueError("incomplete selected V2.1 supported-power layout")
            mode = payload[2]
            row.update(vtx_disposition="REPORTED_SETTINGS_NOT_PHYSICAL_MEASUREMENT",
                vtx_frequency_field_hz=int.from_bytes(payload[3:5], "big") * 1000000,
                vtx_current_power_reported_dbm=payload[5], vtx_channel_raw=payload[0],
                vtx_legacy_power_raw=payload[1], vtx_mode_raw=mode, vtx_mode_unknown_bits=mode & 0xc0,
                vtx_frequency_mode="REPORTED_FREQUENCY_FIELD" if mode & 1 else "CHANNEL_TABLE_NOT_RESOLVED",
                vtx_supported_power_bytes_hex="hex:" + payload[7:].hex(),
                vtx_value_basis="CALLER_SELECTED_BF452_RESPONSE_FIELD_NOT_ACTUAL_RF_NO_AUTHENTICATED_ID")
            for name, bit in (("vtx_pit_mode_reported", 2), ("vtx_in_range_pit_flag", 4),
                              ("vtx_out_range_pit_flag", 8), ("vtx_unlocked_flag", 16),
                              ("vtx_deferred_frequency_flag", 32)):
                row[name] = "TRUE" if mode & bit else "FALSE"
            report["decoded_records"] += 1
        writer.writerow(row)
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("converted output exceeds 128 MiB")
    if not report["source_records"]:
        raise ValueError("empty saved capture")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--source-basis", required=True, choices=(BASIS,))
    args = parser.parse_args()
    try:
        if len({p.resolve() for p in (args.input, args.output, args.report)}) != 3 or args.output.exists() or args.report.exists():
            raise ValueError("distinct new output/report paths required")
        with args.input.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("capture too large")
        output, report = convert(raw.decode("utf-8"), args.source_basis)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(output)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True)
    except (ValueError, OSError, csv.Error) as exc:
        parser.exit(2, f"Saved SmartAudio conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
