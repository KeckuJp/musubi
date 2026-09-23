#!/usr/bin/env python3
"""Saved complete CRSF frames -> selected reported quantities via the existing CSV reader."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import struct

LIMIT = 16 * 1024 * 1024
FIELDS = ["uplink_rssi_1_dbm", "uplink_rssi_2_dbm", "uplink_lq_fraction",
          "uplink_snr_db", "active_antenna", "rf_mode_code", "tx_power_code",
          "downlink_rssi_dbm", "downlink_lq_fraction", "downlink_snr_db"]
ATTITUDE_FIELDS = ["reported_pitch_rad", "reported_roll_rad", "reported_yaw_rad"]
DIRECTIONAL_FIELDS = ["reported_link_side", "reported_rssi_dbm", "reported_rssi_fraction",
                      "reported_lq_fraction", "reported_snr_db", "reported_rf_power_dbm",
                      "reported_rf_frames_per_second"]
BATTERY_FIELDS = ["battery_voltage_v", "battery_average_cell_voltage_v", "battery_current_a",
                  "battery_consumed_charge_c", "battery_remaining_reported_ratio", "battery_wire_basis"]
SPORT_FIELDS = ["battery_voltage_v", "battery_current_a", "sport_physical_id_reported",
                "sport_application_id", "sport_value_basis"]
DISPLAY_FIELDS = ["display_operation_reported", "display_row", "display_column", "display_font_bank",
                  "display_blink_reported", "display_glyph_bytes_hex", "display_glyph_count"]
KINDS = ("link", "attitude", "link-rx", "link-tx", "msp-attitude-bf452", "msp-displayport-bf452", "sport-electrical-fb8622dd") + tuple(
    f"battery-{version}-{source}" for version in ("bf443", "bf450", "bf451", "bf452")
    for source in ("pack", "cell"))


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (0xD5 if value & 0x80 else 0)) & 0xFF
    return value


def convert(text, kind="link"):
    if kind not in KINDS:
        raise ValueError("unknown selected CRSF quantity")
    battery = kind.startswith("battery-")
    display = kind == "msp-displayport-bf452"
    msp = kind in ("msp-attitude-bf452", "msp-displayport-bf452")
    sport = kind == "sport-electrical-fb8622dd"
    selected_type = 1 if sport else 182 if display else 108 if msp else 0x08 if battery else {"link": 0x14, "attitude": 0x1E, "link-rx": 0x1C, "link-tx": 0x1D}[kind]
    fields = FIELDS if kind == "link" else ATTITUDE_FIELDS if kind == "attitude" or msp else DIRECTIONAL_FIELDS
    if msp:
        fields = fields + ["attitude_wire_basis"]
    if battery:
        fields = BATTERY_FIELDS
    if sport:
        fields = SPORT_FIELDS
    if display:
        fields = DISPLAY_FIELDS
    if len(text.encode("utf-8")) > LIMIT:
        raise ValueError("input exceeds bound")
    reader = csv.DictReader(io.StringIO(text), strict=True)
    names = reader.fieldnames
    if not names or len(names) != len(set(names)) or names[:2] != ["time_us", "frame_hex"]:
        raise ValueError("expected time_us,frame_hex followed by optional retained columns")
    if any(not name or len(name) > 128 for name in names) or len(names) > 64:
        raise ValueError("invalid capture columns")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["record_time_us", "destination_address",
        "source_frame_hex", "source_record_hex"] + fields, lineterminator="\n")
    writer.writeheader()
    report = {"source_records": 0, "decoded_records": 0, "unsupported_records": [], "clock": "Unknown"}
    previous, retained_size = None, 0
    try:
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("capture row width mismatch")
            if not re.fullmatch(r"[0-9]{1,19}", row["time_us"]):
                raise ValueError("invalid capture microseconds")
            time = int(row["time_us"])
            if time >= 2**63 or (previous is not None and time < previous):
                raise ValueError("capture time out of range or decreasing")
            previous = time
            value = row["frame_hex"]
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){6,260}" if msp else r"(?:[0-9a-fA-F]{2}){4,64}", value):
                raise ValueError("expected bounded complete selected frame hex")
            frame = bytes.fromhex(value)
            if sport:
                if len(frame) != 9:
                    raise ValueError("S.Port requires nine unstuffed receiver packet bytes")
                checksum = 0
                for byte in frame[1:]:
                    checksum += byte
                    checksum = (checksum + (checksum >> 8)) & 0xff
                if checksum != 0xff:
                    raise ValueError("S.Port checksum mismatch")
                application = int.from_bytes(frame[2:4], "little")
                message_type = int(frame[1] == 0x10 and 0x0200 <= application <= 0x021f)
            elif msp:
                checksum = 0
                for byte in frame[3:]:
                    checksum ^= byte
                if frame[:3] != b"$M>" or frame[3] == 255 or frame[3] != len(frame) - 6 or checksum:
                    raise ValueError("MSPv1 reply direction, length or checksum mismatch")
                message_type = frame[4]
            else:
                if frame[1] != len(frame) - 2 or crc8(frame[2:-1]) != frame[-1]:
                    raise ValueError("CRSF frame length or CRC mismatch")
                message_type = frame[2]
            raw = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(raw) > 12000:
                raise ValueError("retained source record exceeds bound")
            report["source_records"] += 1
            if display and message_type == selected_type and len(frame) == 6:
                raise ValueError("DisplayPort subcommand required")
            if message_type != selected_type or (display and frame[5] not in (0, 1, 2, 3, 4)):
                record = {"line": line, "frame_hex": frame.hex(), "source_record_hex": raw.hex()}
                retained_size += len(json.dumps(record))
                if retained_size > LIMIT:
                    raise ValueError("unsupported report exceeds bound")
                report["unsupported_records"].append(record)
                continue
            if sport:
                value = int.from_bytes(frame[4:8], "little", signed=True)
                voltage = application >= 0x0210
                if voltage and value < 0:
                    raise ValueError("negative reported VFAS voltage")
                values = [value / 100 if voltage else None, None if voltage else value / 10,
                          frame[0] & 0x1f, application, "SPORT_UNCALIBRATED_REPORT_CURRENT_SIGN_UNSPECIFIED"]
            elif display:
                payload = frame[5:-1]
                operation = payload[0]
                if operation == 3:
                    if not 4 <= len(payload) <= 34 or payload[3] & ~0x43 or 0 in payload[4:]:
                        raise ValueError("fixed DisplayPort string length/attribute/glyph mismatch")
                    values = ["WRITE_GLYPHS", payload[1], payload[2], payload[3] & 3,
                              bool(payload[3] & 0x40), "hex:" + payload[4:].hex(), len(payload) - 4]
                else:
                    if len(payload) != 1:
                        raise ValueError("fixed DisplayPort operation length mismatch")
                    values = [{0: "HEARTBEAT_REPORTED", 1: "RELEASE_REPORTED", 2: "CLEAR_REPORTED",
                               4: "DRAW_REPORTED"}[operation], None, None, None, None, None, None]
            elif msp:
                if len(frame) != 12:
                    raise ValueError("fixed MSP attitude requires six payload bytes")
                roll, pitch, yaw = struct.unpack("<hhH", frame[5:-1])
                if abs(roll) > 1800 or abs(pitch) > 900 or yaw > 360:
                    raise ValueError("attitude outside selected writer range")
                values = [math.radians(pitch / 10), math.radians(roll / 10), math.radians(yaw),
                          "BF452_MSP1_REPORTED_FRAME_UNQUALIFIED"]
            elif battery:
                if len(frame) != 12:
                    raise ValueError("selected battery payload must be eight bytes")
                voltage, current = struct.unpack(">HH", frame[3:7])
                capacity = int.from_bytes(frame[7:10], "big")
                remaining = frame[10]
                if remaining > 100 or voltage > 6554:
                    raise ValueError("battery percentage or voltage outside selected writer range")
                pack = kind.endswith("-pack")
                values = [voltage / 10 if pack else None, None if pack else voltage / 10,
                          current / 10, capacity * 3.6, remaining / 100,
                          kind.split("-")[1].upper() + "_NONNEGATIVE_CURRENT_NO_CAPACITY_WRAP"]
            elif kind in ("link-rx", "link-tx"):
                expected = 9 if kind == "link-rx" else 10
                if len(frame) != expected:
                    raise ValueError("selected directional link payload length mismatch")
                rssi, percent, quality, snr, power = struct.unpack("BBBbB", frame[3:8])
                if percent > 100 or quality > 100:
                    raise ValueError("invalid reported directional percentage")
                values = ["RX_REPORTED" if kind == "link-rx" else "TX_REPORTED",
                          -rssi, percent / 100, quality / 100, snr, power,
                          frame[8] * 10 if kind == "link-tx" else None]
            elif kind == "attitude":
                if len(frame) < 10:
                    raise ValueError("selected attitude payload requires six bytes")
                values = [value / 10000 for value in struct.unpack(">hhh", frame[3:9])]
                if any(abs(value) > math.pi for value in values):
                    raise ValueError("attitude outside declared plus/minus pi range")
                # Future appended fields remain in source_frame_hex, not interpreted.
            else:
                if len(frame) != 14:
                    raise ValueError("selected link-statistics payload must be exactly ten bytes")
                r1, r2, lq, snr, antenna, mode, power, dr, dlq, dsnr = struct.unpack("BBBbBBBBBb", frame[3:-1])
                if lq > 100 or dlq > 100:
                    raise ValueError("invalid reported link-quality percent")
                values = [-r1, -r2, lq / 100, snr,
                          {0: "ANTENNA_1", 1: "ANTENNA_2"}.get(antenna, f"UNKNOWN_{antenna}"),
                          mode, power, -dr, dlq / 100, dsnr]
            writer.writerow(dict(record_time_us=time, destination_address=None if msp or sport else frame[0],
                source_frame_hex="hex:" + frame.hex(), source_record_hex="hex:" + raw.hex(),
                **dict(zip(fields, values))))
            report["decoded_records"] += 1
            if output.tell() > LIMIT:
                raise ValueError("output exceeds bound")
    except csv.Error as error:
        raise ValueError("malformed capture CSV") from error
    if not report["decoded_records"]:
        raise ValueError("no selected frame reports")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--kind", choices=KINDS, default="link")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(args.input.read_text(encoding="utf-8"), args.kind)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError):
        parser.exit(2, "Recorded-frame conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
