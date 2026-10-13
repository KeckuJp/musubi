#!/usr/bin/env python3
"""Saved complete CRSF frames -> selected reported quantities via the existing CSV reader."""
import argparse
import csv
import hashlib
import io
import json
import math
import mmap
import os
from pathlib import Path
import re
import struct
import stat

LIMIT = 16 * 1024 * 1024
SPORT_MAX_PACKETS = 65536
SPORT_MAX_DISCARDED_RANGES = 65536
MSP_SERIAL_KIND = "msp-serial-attitude-bf452"
MSP_DISPLAY_SERIAL_KIND = "msp-serial-displayport-bf452"
MSP_MAX_PACKETS = 65536
MSP_MAX_DISCARDED_RANGES = 65536
SPORT_SERIAL_KIND = "sport-serial-fb8622dd"
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
CHANNEL_KIND = "channels-elrs-a60b68af-16ch"
CHANNEL_FIELDS = ([f"rc_channel_{i:02d}_ticks" for i in range(1, 17)] +
                  [f"rc_channel_{i:02d}_nominal_us" for i in range(1, 17)] +
                  ["rc_channel_configuration_basis", "rc_channel_value_basis"])
# ExpressLRS a60b68af: rf_Mode = ModParams->enum_rate (src/include/common.h expresslrs_RFrates_e,
# set in tx_main.cpp/rx_main.cpp) and uplink_TX_Power = powerToCrsfPower(PowerLevels_e)
# (src/lib/POWERMGNT/POWERMGNT.cpp). Labels only; nothing here is a measured rate or power.
ELRS_RATES = dict(enumerate((
    "RATE_LORA_900_25HZ", "RATE_LORA_900_50HZ", "RATE_LORA_900_100HZ", "RATE_LORA_900_100HZ_8CH",
    "RATE_LORA_900_150HZ", "RATE_LORA_900_200HZ", "RATE_LORA_900_200HZ_8CH", "RATE_LORA_900_250HZ",
    "RATE_LORA_900_333HZ_8CH", "RATE_LORA_900_500HZ", "RATE_LORA_900_50HZ_DVDA",
    "RATE_FSK_900_1000HZ_8CH")))
ELRS_RATES.update(enumerate((
    "RATE_LORA_2G4_25HZ", "RATE_LORA_2G4_50HZ", "RATE_LORA_2G4_100HZ", "RATE_LORA_2G4_100HZ_8CH",
    "RATE_LORA_2G4_150HZ", "RATE_LORA_2G4_200HZ", "RATE_LORA_2G4_200HZ_8CH", "RATE_LORA_2G4_250HZ",
    "RATE_LORA_2G4_333HZ_8CH", "RATE_LORA_2G4_500HZ", "RATE_FLRC_2G4_250HZ_DVDA",
    "RATE_FLRC_2G4_500HZ_DVDA", "RATE_FLRC_2G4_500HZ", "RATE_FLRC_2G4_1000HZ",
    "RATE_FSK_2G4_250HZ_DVDA", "RATE_FSK_2G4_500HZ_DVDA", "RATE_FSK_2G4_1000HZ"), 20))
ELRS_RATES.update({100: "RATE_LORA_DUAL_100HZ_8CH", 101: "RATE_LORA_DUAL_150HZ"})
# Code 0 is the default branch, never one of the eight levels ("0 is 0 power which we never use").
ELRS_POWER_MW = {1: 10, 2: 25, 3: 100, 4: 500, 5: 1000, 6: 2000, 7: 250, 8: 50}
ELRS_CODE_FIELDS = ["reported_rf_mode_enum", "reported_tx_power_level_mw",
                    "reported_tx_power_code_status", "elrs_code_basis"]
KINDS = ("link", "attitude", "link-rx", "link-tx", "msp-attitude-bf452", "msp-displayport-bf452", "sport-electrical-fb8622dd", CHANNEL_KIND) + tuple(
    f"battery-{version}-{source}" for version in ("bf443", "bf450", "bf451", "bf452")
    for source in ("pack", "cell"))


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (0xD5 if value & 0x80 else 0)) & 0xFF
    return value


def convert(text, kind="link", *, _allow_no_selected=False, compare_antennas=False, elrs_a60b68af_codes=False):
    if kind not in KINDS:
        raise ValueError("unknown selected CRSF quantity")
    battery = kind.startswith("battery-")
    display = kind == "msp-displayport-bf452"
    msp = kind in ("msp-attitude-bf452", "msp-displayport-bf452")
    sport = kind == "sport-electrical-fb8622dd"
    channels = kind == CHANNEL_KIND
    selected_type = 0x16 if channels else 1 if sport else 182 if display else 108 if msp else 0x08 if battery else {"link": 0x14, "attitude": 0x1E, "link-rx": 0x1C, "link-tx": 0x1D}[kind]
    if compare_antennas and kind != "link":
        raise ValueError("antenna comparison requires qualified link 0x14 reports")
    if type(elrs_a60b68af_codes) is not bool or (elrs_a60b68af_codes and kind != "link"):
        raise ValueError("ELRS code labels require qualified link 0x14 reports")
    fields = FIELDS if kind == "link" else ATTITUDE_FIELDS if kind == "attitude" or msp else DIRECTIONAL_FIELDS
    if compare_antennas:
        fields = fields + ["reported_antenna_1_minus_2_db", "antenna_comparison_basis"]
    if elrs_a60b68af_codes:
        fields = fields + ELRS_CODE_FIELDS
    if msp:
        fields = fields + ["attitude_wire_basis"]
    if battery:
        fields = BATTERY_FIELDS
    if sport:
        fields = SPORT_FIELDS
    if display:
        fields = DISPLAY_FIELDS
    if channels:
        fields = CHANNEL_FIELDS
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
            if channels:
                if len(frame) != 26:
                    raise ValueError("selected ELRS channels require exactly 22 payload bytes")
                packed = int.from_bytes(frame[3:-1], "little")
                ticks = [(packed >> (11 * index)) & 2047 for index in range(16)]
                nominal_us = []
                for tick in ticks:
                    # Fixed ELRS CRSF_to_US/fmap: C++ signed division truncates toward zero.
                    numerator = (tick - 172) * (2012 - 988) * 2
                    scaled = (abs(numerator) // (1811 - 172)) * (-1 if numerator < 0 else 1)
                    nominal_us.append(max(0, min(65535, (scaled + 988 * 2 + 1) // 2)))
                values = ticks + nominal_us + [
                    "CALLER_DECLARED_ELRS_FULL_RES_16CH_NOT_VERIFIED_FROM_FRAME",
                    "ELRS_A60B68AF_CRSF_TO_US_INTEGER_MAPPING_NOT_MEASURED_PWM"]
            elif sport:
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
                if compare_antennas:
                    values += [r2 - r1, "SAME_FRAME_REPORTED_DBM_DIFFERENCE_NOT_GAIN_OR_FAULT"]
                if elrs_a60b68af_codes:
                    values += [ELRS_RATES.get(mode, f"UNKNOWN_{mode}"), ELRS_POWER_MW.get(power),
                               "CONFIGURED_LEVEL_REPORTED" if power in ELRS_POWER_MW
                               else "ZERO_NOT_A_LEVEL" if power == 0 else "UNKNOWN_CODE",
                               "CALLER_DECLARED_ELRS_A60B68AF_ENUM_LABEL_NOT_MEASURED_RATE_OR_POWER"]
            writer.writerow(dict(record_time_us=time, destination_address=None if msp or sport else frame[0],
                source_frame_hex="hex:" + frame.hex(), source_record_hex="hex:" + raw.hex(),
                **dict(zip(fields, values))))
            report["decoded_records"] += 1
            if output.tell() > LIMIT:
                raise ValueError("output exceeds bound")
    except csv.Error as error:
        raise ValueError("malformed capture CSV") from error
    if not report["decoded_records"] and not _allow_no_selected:
        raise ValueError("no selected frame reports")
    return output.getvalue(), report


CSV_WINDOW_KINDS = ("link", "link-rx", "link-tx", "attitude", CHANNEL_KIND) + tuple(
    kind for kind in KINDS if kind.startswith("battery-"))
WINDOW_FIELDS = ["source_input_sha256", "source_original_line", "source_original_byte_offset"]


def convert_csv_window(path, kind="link", *, start_line, stop_line, expected_size=None,
                       expected_sha256=None, compare_antennas=False, elrs_a60b68af_codes=False):
    """Saved CRSF frame CSV larger than 16MiB: reuse the bounded physical-line selector, then
    the unchanged `convert`. Line 0 is the header; outside rows are framed/hashed, not decoded."""
    if __package__:
        from .convert_vda_load_report import read_jsonl_window, expected_snapshot, MAX_RECORD_BYTES
    else:
        from convert_vda_load_report import read_jsonl_window, expected_snapshot, MAX_RECORD_BYTES
    if kind not in CSV_WINDOW_KINDS:
        raise ValueError("CSV line window requires a saved CRSF frame selector")
    if type(start_line) is not int or start_line < 1:
        raise ValueError("data line window starts at one; line zero is the header")
    path = Path(path)
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    before = path.stat()
    with path.open("rb") as stream:
        header = stream.readline(MAX_RECORD_BYTES + 3)
    if not header.endswith(b"\n"):
        raise ValueError("expected a terminated time_us,frame_hex header line")
    selected, offsets, count, selected_bytes, digest = read_jsonl_window(
        path, start_record=start_line, stop_record=stop_line,
        expected_size=expected_size, expected_sha256=expected_sha256)
    if signature(path.stat()) != signature(before):
        raise ValueError("source changed between header and window read")
    text = header.decode("utf-8").removesuffix("\n").removesuffix("\r") + "\n" + "".join(selected)
    output, report = convert(text, kind, compare_antennas=compare_antennas,
                             elrs_a60b68af_codes=elrs_a60b68af_codes)
    # `convert` refuses on any bad row, so every selected row is decoded or unsupported.
    unsupported = {record["line"] for record in report["unsupported_records"]}
    for record in report["unsupported_records"]:
        local = record["line"] - 2
        record.update(original_line=start_line + local + 1, original_byte_offset=offsets[local])
    decoded = [local for local in range(len(selected)) if local + 2 not in unsupported]
    rows = list(csv.DictReader(io.StringIO(output)))
    if len(rows) != len(decoded):
        raise ValueError("window accounting changed")
    for row, local in zip(rows, decoded):
        row.update(source_input_sha256="sha256:" + digest, source_original_line=start_line + local + 1,
                   source_original_byte_offset=offsets[local])
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(csv.DictReader(io.StringIO(output)).fieldnames) + WINDOW_FIELDS,
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    output = stream.getvalue()
    report["source_window"] = dict(source_sha256="sha256:" + digest, start_line=start_line,
        stop_line=stop_line, total_physical_lines=count, selected_physical_lines=len(selected),
        excluded_physical_lines=count - len(selected) - 1, selected_bytes=selected_bytes,
        outside_semantics="NOT_DECODED_LINE_FRAMING_AND_UTF8_CHECKED_ONLY",
        line_basis="ZERO_BASED_HALF_OPEN_PHYSICAL_LINES_HEADER_IS_ZERO_ORIGINAL_LINE_IS_ONE_BASED",
        time_order_scope="NONDECREASING_WITHIN_WINDOW_ONLY_NOT_ACROSS_WINDOWS",
        source_retention="original supplied file required; full hash is not authentication",
        input_expectation=expected_snapshot(expected_size, expected_sha256))
    if len(output.encode("utf-8")) > LIMIT or len(json.dumps(report).encode("utf-8")) > LIMIT:
        raise ValueError("expanded window output or report exceeds bound")
    return output, report


def convert_sport_stream(data: bytes, *, capture_time_us: int, _window=None):
    """Offline fixed OpenTX receive framing; quantities still use convert()."""
    if not isinstance(data, (bytes, mmap.mmap)) or len(data) > (2**31 if _window else LIMIT):
        raise ValueError("S.Port native input exceeds bound or is not bytes")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("S.Port caller capture microseconds required")
    digest = hashlib.sha256(data).hexdigest()
    provenance = ["source_input_sha256", "source_packet_index", "source_start_offset",
                  "source_stop_offset_exclusive", "source_wire_hex"]
    derived = io.StringIO(newline="")
    writer = csv.DictWriter(derived, fieldnames=["time_us", "frame_hex"] + provenance,
                            lineterminator="\n")
    writer.writeheader()
    discarded = []
    discard_summary = {}
    complete_bytes = packet_count = 0
    start, idle_start = None, 0
    packet = bytearray()
    first_literal = escaped = False

    def discard(begin, end, reason):
        if begin < end:
            if _window:
                discard_summary[reason] = discard_summary.get(reason, 0) + end - begin
                discarded[:] = [dict(start_offset=begin, stop_offset_exclusive=end, reason=reason)]
                return
            if len(discarded) >= SPORT_MAX_DISCARDED_RANGES:
                raise ValueError("S.Port discarded ranges exceed bound")
            discarded.append(dict(start_offset=begin, stop_offset_exclusive=end, reason=reason))

    for offset in range(len(data)):
        byte = data[offset]
        if start is None:
            if byte == 0x7e:
                discard(idle_start, offset, "UNFRAMED_BYTES")
                start, first_literal = offset, True
            continue
        # The first physical-ID byte after an idle delimiter is literal in the
        # pinned receiver; after a reset delimiter normal unstuffing applies.
        if first_literal and byte != 0x7e:
            packet.append(byte)
            first_literal = False
        elif escaped:
            packet.append(byte ^ 0x20)
            escaped = False
        elif byte == 0x7e:
            discard(start, offset, "INCOMPLETE_BEFORE_DELIMITER")
            start, first_literal = offset, False
            packet.clear()
        elif byte == 0x7d:
            escaped = True
        else:
            packet.append(byte)
        if len(packet) != 9:
            continue
        if packet_count >= (5_000_000 if _window else SPORT_MAX_PACKETS):
            raise ValueError("S.Port complete packets exceed bound")
        if _window is None or _window[0] <= packet_count < _window[1]:
            writer.writerow(dict(time_us=capture_time_us, frame_hex=packet.hex(),
                source_input_sha256="sha256:" + digest, source_packet_index=packet_count,
                source_start_offset=start, source_stop_offset_exclusive=offset + 1,
                source_wire_hex="hex:" + data[start:offset + 1].hex()))
        if derived.tell() > LIMIT:  # Derived data is entirely ASCII.
            raise ValueError("S.Port derived packet CSV exceeds bound")
        packet_count += 1
        complete_bytes += offset + 1 - start
        start, idle_start = None, offset + 1
        packet.clear()
    discard(idle_start if start is None else start, len(data),
            "UNFRAMED_BYTES" if start is None else "INCOMPLETE_AT_EOF")

    output, report = _convert_native_capture(derived.getvalue(), "sport-electrical-fb8622dd",
                                             provenance, "S.Port", allow_no_selected=_window is not None)
    report["native_stream"] = dict(source_sha256=digest, source_bytes=len(data),
        complete_packet_bytes=complete_bytes,
        discarded_bytes=sum(item["stop_offset_exclusive"] - item["start_offset"] for item in discarded),
        capture_time_us=capture_time_us, capture_time_basis="CALLER_CAPTURE_LABEL_NOT_PACKET_TIME",
        discarded_ranges=discarded, framing_basis="OPENTX_FB8622DD_SPORT_RECEIVE_BYTES")
    if _window:
        finish_window(report, _window, packet_count, discard_summary)
    if len(json.dumps(report, indent=2).encode("utf-8")) > LIMIT:
        raise ValueError("S.Port report exceeds bound")
    return output, report


def _convert_native_capture(text, kind, provenance, label, *, allow_no_selected=False):
    """Reuse the selected complete-frame decoder and expose native byte binding."""
    output, report = convert(text, kind, _allow_no_selected=allow_no_selected)
    # Expose original-container provenance directly to the common CSV reader.
    # source_record_hex is the derived packet row, never called the native file.
    rows = csv.DictReader(io.StringIO(output))
    enriched = io.StringIO(newline="")
    result_writer = csv.DictWriter(enriched, fieldnames=rows.fieldnames + provenance, lineterminator="\n")
    result_writer.writeheader()
    for row in rows:
        original = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
        row.update({key: original[key] for key in provenance})
        result_writer.writerow(row)
        if enriched.tell() > LIMIT:  # Every output field here is ASCII.
            raise ValueError(f"{label} output exceeds bound")
    return enriched.getvalue(), report


def convert_msp_stream(data: bytes, *, capture_time_us: int, _window=None, canvas_geometry=None):
    """Frame already saved ordinary MSPv1 replies; no port or clock inference."""
    if not isinstance(data, (bytes, mmap.mmap)) or len(data) > (2**31 if _window else LIMIT):
        raise ValueError("MSP native input exceeds bound or is not bytes")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("MSP caller capture microseconds required")
    digest = hashlib.sha256(data).hexdigest()
    provenance = ["source_input_sha256", "source_packet_index", "source_start_offset",
                  "source_stop_offset_exclusive", "source_wire_hex"]
    derived = io.StringIO(newline="")
    writer = csv.DictWriter(derived, fieldnames=["time_us", "frame_hex"] + provenance,
                            lineterminator="\n")
    writer.writeheader()
    discarded = []
    discard_summary = {}
    cursor = idle_start = packet_count = complete_bytes = 0

    def discard(begin, end, reason):
        if begin < end:
            if _window:
                discard_summary[reason] = discard_summary.get(reason, 0) + end - begin
                discarded[:] = [dict(start_offset=begin, stop_offset_exclusive=end, reason=reason)]
                return
            if len(discarded) >= MSP_MAX_DISCARDED_RANGES:
                raise ValueError("MSP discarded ranges exceed bound")
            discarded.append(dict(start_offset=begin, stop_offset_exclusive=end,
                                  reason=reason, source_wire_hex="hex:" + data[begin:end].hex()))

    while cursor < len(data):
        start = data.find(b"$", cursor)
        if start < 0:
            break
        remaining = len(data) - start
        if remaining == 1 or data[start:start + 2] in (b"$M", b"$X"):
            discard(idle_start, start, "UNFRAMED_BYTES")
            if remaining < 3:
                discard(start, len(data), "INCOMPLETE_AT_EOF")
                idle_start = len(data)
                break
            if data[start:start + 3] != b"$M>":
                raise ValueError("MSP native selector requires ordinary v1 replies")
            if remaining >= 4 and data[start + 3] == 255:
                raise ValueError("MSP JUMBO framing is unsupported")
            if remaining >= 5 and data[start + 4] == 255:
                raise ValueError("MSPv2-over-v1 framing is unsupported")
            stop = start + 6 + data[start + 3] if remaining >= 4 else len(data) + 1
            if stop > len(data):
                discard(start, len(data), "INCOMPLETE_AT_EOF")
                idle_start = len(data)
                break
            if packet_count >= (5_000_000 if _window else MSP_MAX_PACKETS):
                raise ValueError("MSP complete packets exceed bound")
            native = data[start:stop]
            if _window is None or _window[0] <= packet_count < _window[1]:
                writer.writerow(dict(time_us=capture_time_us, frame_hex=native.hex(),
                    source_input_sha256="sha256:" + digest, source_packet_index=packet_count,
                    source_start_offset=start, source_stop_offset_exclusive=stop,
                    source_wire_hex="hex:" + native.hex()))
            if derived.tell() > LIMIT:
                raise ValueError("MSP derived packet CSV exceeds bound")
            packet_count += 1
            complete_bytes += stop - start
            cursor = idle_start = stop
        else:
            cursor = start + 1
    discard(idle_start, len(data), "UNFRAMED_BYTES")
    meaning = "msp-attitude-bf452" if canvas_geometry is None else "msp-displayport-bf452"
    output, report = _convert_native_capture(derived.getvalue(), meaning,
                                             provenance, "MSP", allow_no_selected=_window is not None)
    report["native_stream"] = dict(source_sha256=digest, source_bytes=len(data),
        complete_packet_bytes=complete_bytes,
        discarded_bytes=sum(item["stop_offset_exclusive"] - item["start_offset"] for item in discarded),
        capture_time_us=capture_time_us, capture_time_basis="CALLER_CAPTURE_LABEL_NOT_PACKET_TIME",
        discarded_ranges=discarded, framing_basis="BETAFLIGHT_024F8E13_MSP_V1_REPLY_BYTES")
    if _window:
        finish_window(report, _window, packet_count, discard_summary)
    if len(json.dumps(report, indent=2).encode("utf-8")) > LIMIT:
        raise ValueError("MSP report exceeds bound")
    if canvas_geometry is not None:
        canvas, canvas_report = native_canvas(derived.getvalue(), canvas_geometry)
        report["canvas"] = canvas_report
        if len(json.dumps(report, indent=2).encode("utf-8")) > LIMIT:
            raise ValueError("native canvas report exceeds bound")
        return output, canvas, report
    return output, report


def native_canvas(derived, geometry):
    """Use the adopted state machine; bind its local indices to original packets."""
    if __package__:
        from .convert_displayport_canvas import convert as canvas_convert, MAX_OUTPUT
    else:
        from convert_displayport_canvas import convert as canvas_convert, MAX_OUTPUT
    if not isinstance(geometry, tuple) or len(geometry) != 2:
        raise ValueError("explicit canvas rows and columns required")
    _, canvas, report = canvas_convert(derived, rows=geometry[0], columns=geometry[1])
    source = list(csv.DictReader(io.StringIO(derived)))
    rows = csv.DictReader(io.StringIO(canvas))
    extra = ["original_input_sha256", "original_draw_packet_index", "original_draw_start_offset",
             "original_draw_stop_offset_exclusive", "original_write_packet_index",
             "original_write_start_offset", "original_write_stop_offset_exclusive"]
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=rows.fieldnames + extra, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        row["original_input_sha256"] = source[0]["source_input_sha256"]
        for role in ("draw", "write"):
            value = row[role + "_record_index"]
            if value != "":
                native = source[int(value)]
                for suffix, key in (("packet_index", "source_packet_index"), ("start_offset", "source_start_offset"),
                                    ("stop_offset_exclusive", "source_stop_offset_exclusive")):
                    row[f"original_{role}_{suffix}"] = native[key]
        writer.writerow(row)
        if output.tell() > MAX_OUTPUT:
            raise ValueError("native canvas output exceeds bound")
    report["initial_state_basis"] = "UNKNOWN_AT_SELECTED_INTERVAL_START_NO_PRIOR_WINDOW_REPLAY"
    report["local_index_basis"] = "SELECTED_DERIVED_FRAME_ROWS_SEE_ORIGINAL_INDEX_COLUMNS"
    return output.getvalue(), report


def finish_window(report, window, count, discarded):
    if window[1] > count:
        raise ValueError("selected frame interval exceeds complete framing records")
    native = report["native_stream"]
    last_range = native.pop("discarded_ranges")
    native.update(last_discarded_range=last_range, discarded_bytes=sum(discarded.values()),
                  discarded_bytes_by_reason=discarded, complete_framing_records=count,
                  selected_frame_start=window[0], selected_frame_stop=window[1],
                  selected_framing_records=window[1]-window[0],
                  unselected_framing_records=count-(window[1]-window[0]),
                  unselected_validation="FRAMING_ONLY_CRC_AND_PAYLOAD_MEANING_NOT_VALIDATED",
                  source_retention="ORIGINAL_FILE_REQUIRED_FOR_ALL_RAW_BYTES")
    report["window_meaning_status"] = ("SELECTED_MEANING_PRESENT" if report["decoded_records"]
                                        else "KNOWN_MEANING_ZERO_UNSUPPORTED_PRESENT")


def convert_native_file(path, kind, *, capture_time_us, start_packet, stop_packet, canvas_geometry=None):
    if kind not in (SPORT_SERIAL_KIND, MSP_SERIAL_KIND, MSP_DISPLAY_SERIAL_KIND):
        raise ValueError("native serial selector required")
    if (kind == MSP_DISPLAY_SERIAL_KIND) != (canvas_geometry is not None):
        raise ValueError("canvas geometry requires explicit native DisplayPort selector")
    if any(type(v) is not int for v in (start_packet, stop_packet)) or not 0 <= start_packet < stop_packet <= 5_000_000 or stop_packet-start_packet > 4096:
        raise ValueError("explicit at-most4096 complete-frame interval required")
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("regular saved file required")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 2**31:
            raise ValueError("nonempty saved file within2GiB required")
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            converter = convert_sport_stream if kind == SPORT_SERIAL_KIND else convert_msp_stream
            extra = {"canvas_geometry": canvas_geometry} if kind == MSP_DISPLAY_SERIAL_KIND else {}
            result = converter(data, capture_time_us=capture_time_us, _window=(start_packet, stop_packet), **extra)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(os.fstat(source.fileno())) or signature(before) != signature(path.lstat()):
            raise ValueError("saved input changed during conversion")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--kind", choices=KINDS + (SPORT_SERIAL_KIND, MSP_SERIAL_KIND, MSP_DISPLAY_SERIAL_KIND), default="link")
    parser.add_argument("--capture-time-us", type=int)
    parser.add_argument("--start-packet", type=int)
    parser.add_argument("--stop-packet", type=int)
    parser.add_argument("--canvas-rows", type=int)
    parser.add_argument("--canvas-columns", type=int)
    parser.add_argument("--compare-antennas", action="store_true")
    parser.add_argument("--elrs-a60b68af-codes", action="store_true")
    parser.add_argument("--start-line", type=int, help="zero-based physical line; line 0 is the header")
    parser.add_argument("--stop-line", type=int, help="exclusive physical line window end")
    parser.add_argument("--expected-size", type=int)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    if (args.compare_antennas or args.elrs_a60b68af_codes) and args.kind != "link":
        parser.error("--compare-antennas and --elrs-a60b68af-codes require --kind link")
    try:
        window = args.start_packet is not None or args.stop_packet is not None
        line_window = args.start_line is not None or args.stop_line is not None
        if line_window and (window or args.capture_time_us is not None):
            raise ValueError("CSV line window cannot be combined with native packet selectors")
        if (args.expected_size is not None or args.expected_sha256 is not None) and not line_window:
            raise ValueError("expected snapshot requires an explicit CSV line window")
        display = args.kind == MSP_DISPLAY_SERIAL_KIND
        geometry = (args.canvas_rows, args.canvas_columns) if display else None
        if not display and (args.canvas_rows is not None or args.canvas_columns is not None):
            raise ValueError("canvas geometry requires native DisplayPort selector")
        if display and not window:
            raise ValueError("native DisplayPort requires explicit complete-frame interval")
        if not window and not line_window and args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        if line_window:
            output, report = convert_csv_window(args.input, args.kind, start_line=args.start_line,
                stop_line=args.stop_line, expected_size=args.expected_size,
                expected_sha256=args.expected_sha256, compare_antennas=args.compare_antennas,
                elrs_a60b68af_codes=args.elrs_a60b68af_codes)
        elif window:
            result = convert_native_file(args.input, args.kind, capture_time_us=args.capture_time_us,
                start_packet=args.start_packet, stop_packet=args.stop_packet, canvas_geometry=geometry)
            if display:
                output, canvas, report = result
            else:
                output, report = result
        elif args.kind == SPORT_SERIAL_KIND:
            output, report = convert_sport_stream(args.input.read_bytes(), capture_time_us=args.capture_time_us)
        elif args.kind == MSP_SERIAL_KIND:
            output, report = convert_msp_stream(args.input.read_bytes(), capture_time_us=args.capture_time_us)
        else:
            if args.capture_time_us is not None:
                raise ValueError("capture time flag requires native serial selector")
            output, report = convert(args.input.read_text(encoding="utf-8"), args.kind,
                                     compare_antennas=args.compare_antennas,
                                     elrs_a60b68af_codes=args.elrs_a60b68af_codes)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        if display:
            (args.output_directory / "canvas.csv").write_text(canvas, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError):
        parser.exit(2, "Recorded-frame conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
