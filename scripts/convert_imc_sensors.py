#!/usr/bin/env python3
"""Passive finite IMC scalar sensor frames -> existing CSV Observation reader."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recorded_units import UNITS, normalize

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
SENSORS = {263: ("f", "temperature_k", 1, 273.15),
           264: ("d", "external_pressure_pa", 100, 0),
           265: ("f", "depth_m", 1, 0),
           267: ("f", "sound_speed_m_s", 1, 0),
           268: ("f", "water_density_kg_m3", 1, 0),
           270: ("f", "salinity_psu", 1, 0),
           251: ("f", "electrical_voltage_v", 1, 0),
           252: ("f", "electrical_current_a", 1, 0),
           364: ("f", "electrical_power_w", 1, 0)}
VECTORS = {256: tuple("reported_angular_velocity_" + axis + "_rad_s" for axis in "xyz"),
           257: tuple("reported_acceleration_" + axis + "_m_s2" for axis in "xyz")}
NEPTUS_SCHEMA = "neptus-2019.10.1-sensors"
NEPTUS_SENSORS = frozenset((263, 264, 265, 267, 268))


def crc16(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


def convert(data, *, allow_no_selected=False, schema=None):
    if schema not in (None, NEPTUS_SCHEMA):
        raise ValueError("unqualified IMC schema")
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized IMC stream")
    output = io.StringIO(newline="")
    names = ["record_time_us", "source_timestamp_s", "source_id", "source_entity",
             "message_id", "source_frame_hex", "measurement_status", "device_time_s",
             "device_clock_basis", "axis_frame_basis"] + [v[1] for v in SENSORS.values()] + [
                 field for fields in VECTORS.values() for field in fields]
    writer = csv.DictWriter(output, fieldnames=names, lineterminator="\n")
    writer.writeheader()
    report = {"source_frames": 0, "decoded_frames": 0, "unsupported_frames": [], "clock": "Unknown"}
    report_size = 0
    offset, previous = 0, None
    while offset < len(data):
        if len(data) - offset < 22:
            raise ValueError("truncated IMC header/footer")
        prefix = data[offset:offset + 2]
        order = "<" if prefix == b"\x54\xfe" else ">" if prefix == b"\xfe\x54" else None
        if order is None:
            raise ValueError("unsupported IMC synchronization/version")
        _, message, size, timestamp, source, entity, _, _ = struct.unpack_from(order + "HHHdHBHB", data, offset)
        end = offset + 20 + size + 2
        if end > len(data):
            raise ValueError("truncated IMC payload")
        frame = data[offset:end]
        if crc16(frame[:-2]) != struct.unpack(order + "H", frame[-2:])[0]:
            raise ValueError("invalid IMC CRC")
        if not math.isfinite(timestamp) or not 0 <= timestamp * 10**6 < 2**63:
            raise ValueError("invalid dispatcher timestamp")
        if previous is not None and timestamp < previous:
            raise ValueError("decreasing dispatcher timestamp; split source streams explicitly")
        previous = timestamp
        report["source_frames"] += 1
        if (message not in SENSORS and message not in VECTORS) or (
                schema == NEPTUS_SCHEMA and message not in NEPTUS_SENSORS):
            retained = {"offset": offset, "message_id": message, "frame_hex": frame.hex()}
            report_size += len(json.dumps(retained))
            if report_size > LIMIT:
                raise ValueError("unsupported report exceeds bound")
            report["unsupported_frames"].append(retained)
        else:
            missing = False
            if message in VECTORS:
                if size != 32:
                    raise ValueError("selected IMC vector size mismatch")
                device_time, *axes = struct.unpack(order + "dddd", frame[20:-2])
                if not all(math.isfinite(value) for value in (device_time, *axes)):
                    raise ValueError("nonfinite IMC vector value")
                quantities = dict(zip(VECTORS[message], axes))
                quantities.update(device_time_s=repr(device_time), device_clock_basis="UNSPECIFIED",
                                  axis_frame_basis="UNSPECIFIED_BY_MESSAGE_SCHEMA")
            else:
                code, field, scale, bias = SENSORS[message]
                if size != struct.calcsize(code):
                    raise ValueError("selected IMC payload size mismatch")
                value = struct.unpack(order + code, frame[20:-2])[0]
                if not math.isfinite(value):
                    raise ValueError("nonfinite IMC sensor value")
                missing = message == 267 and value < 0
                normalized = None if missing else value * scale + bias
                if normalized is not None and (not math.isfinite(normalized) or (message == 263 and normalized < 0)):
                    raise ValueError("invalid normalized sensor quantity")
                quantities = {field: normalized}
            writer.writerow({"record_time_us": int(timestamp * 10**6), "source_timestamp_s": repr(timestamp),
                "source_id": source, "source_entity": entity, "message_id": message,
                "source_frame_hex": "hex:" + frame.hex(),
                "measurement_status": "UNAVAILABLE_SOURCE_SENTINEL" if missing else "REPORTED",
                **quantities})
            report["decoded_frames"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("converted CSV exceeds bound")
        offset = end
    if not report["decoded_frames"] and not allow_no_selected:
        raise ValueError("no selected IMC sensor reports")
    return output.getvalue(), report


def convert_input(data, *, schema=None):
    """Reuse packet conversion after bounded RFC1952 member decoding, if present."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized source")
    source_hash = hashlib.sha256(data).hexdigest()
    members = 0
    if data.startswith(b"\x1f\x8b"):
        pending, decoded = data, bytearray()
        while pending:
            members += 1
            if members > 1024:
                raise ValueError("gzip member count exceeds bound")
            try:
                decoder = zlib.decompressobj(31)
                # Never pass zero (unlimited); no unbounded flush/second decode.
                part = decoder.decompress(pending, LIMIT - len(decoded) + 1)
            except zlib.error as error:
                raise ValueError("invalid gzip member") from error
            if len(decoded) + len(part) > LIMIT:
                raise ValueError("gzip expansion exceeds bound")
            if not decoder.eof or decoder.unconsumed_tail:
                raise ValueError("truncated or oversized gzip member")
            decoded.extend(part)
            pending = decoder.unused_data
        data = bytes(decoded)
    output, report = convert(data, schema=schema)
    if schema is not None:
        report["selected_schema"] = schema
    report.update(source_sha256=source_hash, compression="gzip" if members else "none",
                  gzip_members=members, decoded_input_sha256=hashlib.sha256(data).hexdigest())
    return output, report


def convert_alog(data, variable, publisher, unit, capture_time_us, *, timestamp_decimals=3):
    """Explicit typed pLogger export plus caller-qualified quantity; no broker."""
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise ValueError("bounded alog bytes required")
    if type(timestamp_decimals) is not int or timestamp_decimals not in (3, 5):
        raise ValueError("qualified logger timestamp precision required")
    if (not all(isinstance(s, str) and re.fullmatch(r"[!-~]{1,256}", s) for s in (variable, publisher))
            or unit not in UNITS or type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63):
        raise ValueError("explicit variable, publisher, qualified unit and capture time required")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    field = UNITS[unit][0]
    writer.writerow(["record_time_us", "source_line", "source_variable", "source_publisher",
        "logger_relative_time_us", "selected_unit", field, "source_sha256"])
    report = dict(source_records=0, selected_records=0, unselected_records=0,
        comment_lines=0, blank_lines=0, source_sha256=hashlib.sha256(data).hexdigest(),
        source_retention="unchanged source.alog retained with CSV/report",
        clock="CALLER_CAPTURE; logger_relative_time_us is not UTC or device uptime",
        unit_basis="caller-qualified variable/publisher contract, not inferred from variable name",
        timestamp_decimals=timestamp_decimals,
        evidence="fixed typed pLogger export code/spec, not physical operation")
    marked, start, begun = False, None, False
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            report["blank_lines"] += 1
            continue
        if line.startswith("%"):
            report["comment_lines"] += 1
            if line == "%% DATATYPE MARKING ON":
                if marked or begun: raise ValueError("duplicate or late type header")
                marked = True
            elif line.startswith("%% LOGSTART"):
                if start is not None or begun: raise ValueError("duplicate or late log start")
                start = line[len("%% LOGSTART"):].strip()
                if len(start) > 128 or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", start) or not math.isfinite(float(start)):
                    raise ValueError("finite declared log start required")
            continue
        begun = True
        if not marked or start is None or len(line) > 1024 * 1024:
            raise ValueError("typed pLogger header and bounded records required")
        parts = line.split(None, 3)
        if len(parts) != 4 or len(parts[0]) > 24 or not re.fullmatch(r"[+-]?[0-9]+\.[0-9]{" + str(timestamp_decimals) + "}", parts[0]):
            raise ValueError("selected fixed precision relative timestamp and four fields required")
        micros = int(Decimal(parts[0]) * 1000000)
        if not -(2**63) <= micros < 2**63:
            raise ValueError("relative time outside common range")
        report["source_records"] += 1
        if parts[1] != variable or parts[2] != publisher:
            report["unselected_records"] += 1
            continue
        token = parts[3].strip()
        if not token.startswith("D:"):
            raise ValueError("selected variable is not marked numeric")
        value_text = token[2:].strip()
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", value_text) or len(value_text) > 128:
            raise ValueError("finite marked numeric value required")
        value = float(value_text)
        if Decimal(value_text) != 0 and value == 0:
            raise ValueError("numeric underflow")
        _, normalized = normalize(value, unit)
        writer.writerow([capture_time_us, number, variable, publisher, micros, unit,
                         normalized, report["source_sha256"]])
        report["selected_records"] += 1
        if output.tell() > OUTPUT_LIMIT: raise ValueError("CSV exceeds bound")
    if not report["selected_records"]: raise ValueError("no selected numeric records")
    report["declared_logstart_text"] = start
    return output.getvalue(), report


def convert_slog(data, variable, unit, capture_time_us):
    """Fixed-column synchronous pLogger values; NaN has several possible causes."""
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise ValueError("bounded slog bytes required")
    if (not isinstance(variable, str) or variable == "TIME" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,255}", variable)
            or unit not in UNITS or type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63):
        raise ValueError("explicit variable, qualified unit and capture time required")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_line", "source_variable", "logger_relative_time_us",
        "logger_relative_time_text", "selected_unit", UNITS[unit][0], "value_disposition", "source_sha256"])
    report = dict(source_records=0, selected_records=0, withheld_records=0,
        source_sha256=hashlib.sha256(data).hexdigest(), source_retention="unchanged source.slog retained with CSV/report",
        clock="CALLER_CAPTURE; logger relative microseconds floored, original text retained",
        unit_basis="caller-qualified variable contract; publisher absent in slog")
    columns, label, previous = [], None, None
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        if not line.strip(): continue
        if len(line) > 1024 * 1024: raise ValueError("oversized slog line")
        if line.startswith("%"):
            declaration = re.fullmatch(r"%%\s+\(([0-9]+)\)\s+([A-Za-z_][A-Za-z0-9_]*)\s*", line)
            if declaration:
                index, name = declaration.groups()
                if label is not None or int(index) != len(columns) + 1 or name in columns:
                    raise ValueError("ambiguous or late column declaration")
                columns.append(name)
            elif line.startswith("%% TIME"):
                if line[len("%% TIME"):].lstrip().startswith("["):
                    continue  # The writer's per-column label timestamp is not an observation.
                names = line[3:].split()
                if not columns or columns[0] != "TIME" or names != columns:
                    raise ValueError("dynamic or inconsistent slog columns")
                label = names
            continue
        parts = line.split()
        if label is None or len(parts) != len(label) or variable not in label:
            raise ValueError("fixed declared columns required")
        numeric = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?"
        if len(parts[0]) > 32 or not re.fullmatch(numeric, parts[0]):
            raise ValueError("finite relative timestamp required")
        stamp = Decimal(parts[0])
        micros = int(stamp * 1000000)
        if stamp < 0 or micros >= 2**63 or (previous is not None and stamp < previous):
            raise ValueError("invalid relative clock")
        previous = stamp
        if any(value != "NaN" and (len(value) > 128 or not re.fullmatch(numeric, value)) for value in parts[1:]):
            raise ValueError("numeric or explicit NaN columns required")
        token = parts[label.index(variable)]
        report["source_records"] += 1
        value, disposition = "", "WITHHELD_NOT_FRESH_OR_NONNUMERIC_OR_MISSING"
        if token == "NaN":
            report["withheld_records"] += 1
        else:
            raw = float(token)
            if Decimal(token) != 0 and raw == 0: raise ValueError("numeric underflow")
            _, value = normalize(raw, unit)
            disposition = "NORMALIZED_REPORTED"
            report["selected_records"] += 1
        writer.writerow([capture_time_us, number, variable, micros, parts[0], unit, value,
            disposition, report["source_sha256"]])
        if output.tell() > OUTPUT_LIMIT: raise ValueError("CSV exceeds bound")
    if not report["source_records"]: raise ValueError("no slog records")
    report["conversion_status"] = "OBSERVATIONS_PRODUCED" if report["selected_records"] else "ONLY_WITHHELD_VALUES"
    return output.getvalue(), report


def packet_ipv4(packet, linktype):
    """Selected saved link envelope; None means explicitly unselected."""
    if linktype == 1:
        if len(packet) < 14:
            raise ValueError("truncated Ethernet header")
        return packet[14:] if packet[12:14] == b"\x08\x00" else None
    if len(packet) < 2:
        raise ValueError("truncated Wi-Fi frame control")
    control = int.from_bytes(packet[:2], "little")
    if control & 3:
        raise ValueError("unsupported Wi-Fi protocol version")
    if (control >> 2) & 3 != 2:
        return None
    if control & 0xF0 or control & (0x400 | 0x4000 | 0x8000):
        raise ValueError("only unprotected unfragmented non-QoS Wi-Fi data supported")
    header = 30 if control & 0x300 == 0x300 else 24
    if len(packet) < header + 8 or packet[22] & 15:
        raise ValueError("truncated or fragmented Wi-Fi data")
    if packet[header:header + 6] != b"\xaa\xaa\x03\x00\x00\x00":
        raise ValueError("RFC1042 LLC/SNAP required")
    return packet[header + 8:] if packet[header + 6:header + 8] == b"\x08\x00" else None


def convert_pcap(data, destination_port, *, payload_format="imc"):
    """Offline complete IPv4/UDP datagrams through the selected existing decoder."""
    if payload_format not in ("imc", "nmea-wind"):
        raise ValueError("explicit supported packet payload required")
    import dpkt
    if dpkt.__version__ != "1.9.8":
        raise ValueError("qualified dpkt1.9.8 required")
    if type(destination_port) is not int or not 1 <= destination_port <= 65535:
        raise ValueError("explicit UDP destination port required")
    magic = {b"\xd4\xc3\xb2\xa1": ("<", 1), b"\xa1\xb2\xc3\xd4": (">", 1),
             b"\x4d\x3c\xb2\xa1": ("<", 1000), b"\xa1\xb2\x3c\x4d": (">", 1000)}
    if not 24 <= len(data) <= LIMIT or data[:4] not in magic:
        raise ValueError("bounded classic PCAP required")
    order, divisor = magic[data[:4]]
    _, major, minor, _, _, snaplen, linktype = struct.unpack(order + "IHHIIII", data[:24])
    if (major, minor) != (2, 4) or linktype not in (1, 105) or not 0 < snaplen <= LIMIT:
        raise ValueError("PCAP2.4 Ethernet or plain IEEE802.11 layout required")
    # dpkt's reader yields short packet bodies without checking caplen. Validate
    # bounds and retain exact integer timestamps before using its packet iterator.
    entries, offset = [], 24
    while offset < len(data):
        if len(data) - offset < 16:
            raise ValueError("truncated PCAP record header")
        sec, frac, captured, original = struct.unpack_from(order + "IIII", data, offset)
        if frac >= 1000000 * divisor or captured > snaplen or captured != original or offset + 16 + captured > len(data):
            raise ValueError("truncated capture or invalid packet timestamp/length")
        entries.append(sec * 1000000 + frac // divisor)
        offset += 16 + captured
    output, writer, previous = io.StringIO(newline=""), None, None
    report = dict(source_sha256="sha256:" + hashlib.sha256(data).hexdigest(), packets=len(entries),
        selected_packets=0, unselected_packets=0, decoded_records=0, selected_sources=[], clock="Unknown",
        linktype=linktype, unselected_status="NOT_DECODED_OR_VALIDATED")
    report_size = 0
    if payload_format == "nmea-wind":
        report["valid_wind_measurements"] = 0
    for index, (_, packet) in enumerate(dpkt.pcap.Reader(io.BytesIO(data))):
        capture = entries[index]
        if previous is not None and capture < previous:
            raise ValueError("decreasing packet clock")
        previous = capture
        network = packet_ipv4(packet, linktype)
        if network is None:
            report["unselected_packets"] += 1
            continue
        try:
            ip = dpkt.ip.IP(network)
        except dpkt.UnpackError as exc:
            raise ValueError("invalid saved IPv4 packet") from exc
        if ip.v != 4 or ip.hl < 5 or ip.len < ip.hl * 4 or ip.len > len(network):
            raise ValueError("invalid IPv4 length")
        if linktype == 105 and ip.len != len(network):
            raise ValueError("Wi-Fi trailing bytes or FCS not supported")
        if ip.p != 17:
            report["unselected_packets"] += 1
            continue
        if ip.offset or ip.mf:
            raise ValueError("fragmented UDP is outside selected path")
        raw_udp = network[ip.hl * 4:ip.len]
        try:
            udp = dpkt.udp.UDP(raw_udp)
        except dpkt.UnpackError as exc:
            raise ValueError("invalid saved UDP packet") from exc
        if udp.dport != destination_port:
            report["unselected_packets"] += 1
            continue
        if udp.ulen != len(raw_udp) or udp.ulen < 8 or dpkt.in_cksum(network[:ip.hl * 4]):
            raise ValueError("invalid selected IP/UDP length or checksum")
        if udp.sum and dpkt.in_cksum(ip.src + ip.dst + struct.pack(">BBH", 0, 17, udp.ulen) + raw_udp):
            raise ValueError("invalid selected UDP checksum")
        if payload_format == "nmea-wind":
            try:
                from scripts.convert_nmea_wind import convert as wind
            except ModuleNotFoundError:
                from convert_nmea_wind import convert as wind
            normalized, payload_report = wind(bytes(udp.data).decode("ascii"), allow_no_selected=True)
            report["valid_wind_measurements"] += payload_report["valid_with_measurement"]
        else:
            normalized, payload_report = convert(bytes(udp.data), allow_no_selected=True)
        saved = dict(packet_index=index, packet_hex=packet.hex(), capture_time_us=capture,
            udp_checksum="CHECKED" if udp.sum else "NOT_PROVIDED")
        saved["nmea" if payload_format == "nmea-wind" else "imc"] = payload_report
        report_size += len(json.dumps(saved))
        if report_size > 32 * 1024 * 1024:
            raise ValueError("packet report exceeds bound")
        report["selected_sources"].append(saved)
        report["selected_packets"] += 1
        for row in csv.DictReader(io.StringIO(normalized)):
            if payload_format == "nmea-wind":
                row["nmea_unlocated_time_us"] = row.pop("unlocated_time_us")
            else:
                row["imc_payload_time_us"] = row["record_time_us"]
            row.update(record_time_us=capture,
                pcap_packet_index=index, pcap_source_sha256=report["source_sha256"],
                pcap_clock_basis="CAPTURE_REPORTED_NOT_AUTHENTICATED")
            if writer is None:
                writer = csv.DictWriter(output, list(row), lineterminator="\n")
                writer.writeheader()
            writer.writerow(row)
            report["decoded_records"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("packet CSV exceeds bound")
    if not report["decoded_records"]:
        raise ValueError("no selected packet observations")
    if payload_format == "nmea-wind":
        report["conversion_status"] = "OBSERVATIONS_PRODUCED" if report["valid_wind_measurements"] else "ONLY_WITHHELD_VALUES"
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--source-format", choices=("imc", NEPTUS_SCHEMA, "moos-typed-alog", "moos-ivp-typed-alog-f56c", "moos-fixed-slog", "pcap-udp-imc", "pcap-udp-nmea-wind"), default="imc")
    parser.add_argument("--udp-destination-port", type=int)
    parser.add_argument("--variable")
    parser.add_argument("--publisher")
    parser.add_argument("--unit", choices=UNITS)
    parser.add_argument("--capture-time-us", type=int)
    args = parser.parse_args()
    try:
        packet_format = args.source_format in ("pcap-udp-imc", "pcap-udp-nmea-wind")
        alog_format = args.source_format in ("moos-typed-alog", "moos-ivp-typed-alog-f56c")
        if not packet_format and args.udp_destination_port is not None:
            raise ValueError("UDP selection requires explicit PCAP format")
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        data = args.input.read_bytes()
        if alog_format:
            output, report = convert_alog(data, args.variable, args.publisher, args.unit, args.capture_time_us,
                timestamp_decimals=5 if args.source_format == "moos-ivp-typed-alog-f56c" else 3)
        elif args.source_format == "moos-fixed-slog":
            if args.publisher is not None: raise ValueError("slog has no publisher identity")
            output, report = convert_slog(data, args.variable, args.unit, args.capture_time_us)
        else:
            if any(value is not None for value in (args.variable, args.publisher, args.unit, args.capture_time_us)):
                raise ValueError("alog options require explicit format")
            output, report = convert_pcap(data, args.udp_destination_port,
                payload_format="nmea-wind" if args.source_format == "pcap-udp-nmea-wind" else "imc") if packet_format else convert_input(
                    data, schema=NEPTUS_SCHEMA if args.source_format == NEPTUS_SCHEMA else None)
        args.output_directory.mkdir()
        if alog_format:
            (args.output_directory / "source.alog").write_bytes(data)
        elif args.source_format == "moos-fixed-slog":
            (args.output_directory / "source.slog").write_bytes(data)
        elif packet_format:
            (args.output_directory / "source.pcap").write_bytes(data)
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, struct.error):
        parser.exit(2, "IMC sensor conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
