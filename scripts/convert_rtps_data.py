#!/usr/bin/env python3
"""Saved RTPS2.3 DATA interpreted with a caller-selected local BatteryState definition.

DATA has no type hash: successful decoding is not writer/type qualification.
No socket, discovery, fragment reassembly, or DDS runtime is involved.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import struct

try:
    from scripts.convert_imc_sensors import LIMIT, iter_udp_datagrams, read_pcap_records
    from scripts.convert_rosbag_observations import BASE, BATTERY, HASHES, decode
    from scripts.convert_ardupilot_battery_csv import json_quantities, json_time, unique_object
except ModuleNotFoundError:
    from convert_imc_sensors import LIMIT, iter_udp_datagrams, read_pcap_records
    from convert_rosbag_observations import BASE, BATTERY, HASHES, decode
    from convert_ardupilot_battery_csv import json_quantities, json_time, unique_object

MAX_PACKETS = 100_000
MAX_SUBMESSAGES = 100_000
MAX_SUBMESSAGES_PER_DATAGRAM = 1024
MAX_CDR_BYTES = 12000
MAX_SOURCE_BYTES = 30000
MAX_BINDING_BYTES = 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
SCHEMA = "ros2-humble-battery-cdr1"
SCHEMA_BASIS = "TYPE_HASH_ABSENT_NOT_VERIFIED_CALLER_SELECTED_LOCAL_DEFINITION_PRODUCER_UNVERIFIED"
FIELDS = BASE + ["rtps_packet_index", "rtps_submessage_index", "rtps_writer_guid_hex",
                 "rtps_reader_id_hex", "rtps_writer_sequence_high", "rtps_writer_sequence_low",
                 "rtps_timestamp_seconds", "rtps_timestamp_fraction", "rtps_timestamp_disposition",
                 "ros_source_stamp_us", "ros_frame_hex", "rtps_schema_basis", "rtps_local_type_hash",
                 "rtps_capture_sha256"]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def validate_binding(data, binding):
    keys = {"source_input_sha256", "writer_guid_hex", "schema"}
    if not isinstance(binding, dict) or set(binding) != keys or any(type(v) is not str for v in binding.values()):
        raise ValueError("exact capture/GUID/local-schema binding required; no asserted wire type hash")
    if len(encoded(binding)) > MAX_BINDING_BYTES:
        raise ValueError("binding exceeds bound")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if binding["source_input_sha256"] != digest or binding["schema"] != SCHEMA:
        raise ValueError("capture byte binding or selected local schema mismatch")
    if not re.fullmatch(r"hex:[0-9a-f]{32}", binding["writer_guid_hex"]):
        raise ValueError("exact lowercase writer GUID required")
    guid = bytes.fromhex(binding["writer_guid_hex"][4:])
    if guid[-1] != 3:
        raise ValueError("selected writer must be a user-defined no-key writer")
    return guid, digest


def selected_row(body, flags, context, saved, index, binding, digest, descriptor):
    if flags not in (4, 5):
        raise ValueError("selected DATA requires plain data, no inline QoS, key, or transformed payload")
    order = "<" if flags & 1 else ">"
    extra, offset = struct.unpack_from(order + "HH", body)
    high, low = struct.unpack_from(order + "iI", body, 12)
    if extra or offset != 16 or high < 0 or (high == 0 and low == 0):
        raise ValueError("selected DATA extension, offset, or sequence outside finite scope")
    raw = body[4 + offset:]
    if len(raw) > MAX_CDR_BYTES:
        raise ValueError("selected CDR exceeds bound")
    try:
        record = decode(raw, BATTERY)
        stamp = json_time(record, "ros-battery-json")
        quantities = json_quantities(record, "ros-battery-json")
    except (ValueError, TypeError, KeyError, IndexError, struct.error, UnicodeError, AssertionError) as exc:
        raise ValueError("selected DATA does not decode under the local BatteryState definition") from exc
    source = dict(packet_index=saved.index, submessage_index=index,
                  writer_guid_hex=binding["writer_guid_hex"], reader_id_hex=descriptor["reader_id_hex"],
                  writer_sequence_high=high, writer_sequence_low=low,
                  protocol_version="2.3", vendor_id_hex=context["vendor_id_hex"],
                  raw_cdr_hex="hex:" + raw.hex(), decoded=record,
                  schema_basis=SCHEMA_BASIS, local_type_hash=HASHES[BATTERY],
                  source_input_sha256=digest, capture_time_us=saved.capture_time_us,
                  rtps_timestamp=context["timestamp"].copy())
    retained = encoded(source)
    if len(retained) > MAX_SOURCE_BYTES:
        raise ValueError("retained source record exceeds bound")
    row = dict(zip(BASE[2:], quantities))
    timestamp = context["timestamp"]
    row.update(record_time_us=saved.capture_time_us, source_record_hex="hex:" + retained.hex(),
               rtps_packet_index=saved.index, rtps_submessage_index=index,
               rtps_writer_guid_hex=binding["writer_guid_hex"],
               rtps_reader_id_hex=descriptor["reader_id_hex"],
               rtps_writer_sequence_high=high, rtps_writer_sequence_low=low,
               rtps_timestamp_seconds=timestamp.get("seconds"),
               rtps_timestamp_fraction=timestamp.get("fraction"),
               rtps_timestamp_disposition=timestamp["disposition"],
               ros_source_stamp_us=stamp, ros_frame_hex="hex:" + record["header"]["frame_id"].encode().hex(),
               rtps_schema_basis=SCHEMA_BASIS, rtps_local_type_hash=HASHES[BATTERY], rtps_capture_sha256=digest)
    return row


def convert_pcap_rtps(data, destination_port, *, binding):
    """Return complete CSV/report or raise; no committed prefix and no type attestation."""
    if type(data) is not bytes or not 24 <= len(data) <= LIMIT:
        raise ValueError("bounded classic PCAP bytes required")
    guid, digest = validate_binding(data, binding)
    records = read_pcap_records(data)
    if len(records[1]) > MAX_PACKETS:
        raise ValueError("source packet count exceeds bound")
    report = dict(source_packets=0, selected_udp_datagrams=0, rtps_datagrams=0,
                  submessages=0, selected_data=0, unselected_data=0, output_records=0, packets=[],
                  binding=dict(binding), schema_basis=SCHEMA_BASIS, local_type_hash=HASHES[BATTERY],
                  time_basis="PCAP_CAPTURE_UNKNOWN_NOT_WRITER_CLOCK_OR_TRUSTED_UTC",
                  not_verified="writer type, producer binary, identity, delivery, physical quantity or clock alignment")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    report_size = len(encoded(report))
    for saved in iter_udp_datagrams(data, destination_port, records):
        report["source_packets"] += 1
        packet = dict(index=saved.index, capture_time_us=saved.capture_time_us,
                      frame_hex="hex:" + saved.frame.hex(), disposition="NOT_SELECTED_UDP", submessages=[])
        payload = saved.payload
        if payload is not None:
            report["selected_udp_datagrams"] += 1
            packet.update(source_address=saved.source_address, source_port=saved.source_port,
                          destination_address=saved.destination_address, destination_port=saved.destination_port,
                          udp_checksum=saved.udp_checksum, disposition="UNSUPPORTED_NON_RTPS_DATAGRAM")
        if payload is not None and payload.startswith(b"RTPS"):
            if len(payload) < 20 or payload[4:6] != b"\x02\x03":
                raise ValueError("RTPS2.3 header required")
            report["rtps_datagrams"] += 1
            packet["disposition"] = "RTPS_FRAMING_PARSED_NOT_TRANSPORT_VALIDATED"
            context = {"vendor_id_hex": "hex:" + payload[6:8].hex(),
                       "timestamp": {"disposition": "NOT_REPORTED"}}
            position = 20
            while position < len(payload):
                index = len(packet["submessages"])
                if index >= MAX_SUBMESSAGES_PER_DATAGRAM or report["submessages"] >= MAX_SUBMESSAGES:
                    raise ValueError("submessage count exceeds bound")
                if position % 4 or len(payload) - position < 4:
                    raise ValueError("misaligned or truncated submessage header")
                kind, flags = payload[position:position + 2]
                order = "<" if flags & 1 else ">"
                length = struct.unpack_from(order + "H", payload, position + 2)[0]
                start = position + 4
                end = start + length if length or kind in (1, 9) else len(payload)
                if end > len(payload):
                    raise ValueError("submessage exceeds saved datagram")
                body = payload[start:end]
                descriptor = dict(index=index, kind=kind, flags=flags, offset=position, body_bytes=len(body))
                report["submessages"] += 1
                if kind == 0x15:
                    if len(body) < 20:
                        raise ValueError("truncated DATA header")
                    reported_guid = payload[8:20] + body[8:12]
                    descriptor.update(writer_guid_hex="hex:" + reported_guid.hex(),
                                      reader_id_hex="hex:" + body[4:8].hex())
                    if reported_guid == guid:
                        row = selected_row(body, flags, context, saved, index, binding, digest, descriptor)
                        writer.writerow(row)
                        if output.tell() > LIMIT:
                            raise ValueError("converted CSV exceeds bound")
                        report["selected_data"] += 1
                        report["output_records"] += 1
                        descriptor["disposition"] = "DECODED_WITH_CALLER_LOCAL_DEFINITION_TYPE_UNVERIFIED"
                    else:
                        report["unselected_data"] += 1
                        descriptor["disposition"] = "OTHER_WRITER_NOT_DECODED"
                elif kind == 9:
                    if flags & ~3 or len(body) != (0 if flags & 2 else 8):
                        raise ValueError("invalid INFO_TS flags or length")
                    timestamp = {"disposition": "INVALIDATED"}
                    if not flags & 2:
                        seconds, fraction = struct.unpack(order + "II", body)
                        disposition = {(0xffffffff, 0xffffffff): "TIME_INVALID",
                                       (0xffffffff, 0xfffffffe): "TIME_INFINITE"}.get((seconds, fraction), "REPORTED")
                        timestamp = dict(seconds=seconds, fraction=fraction, disposition=disposition)
                    context["timestamp"] = timestamp
                    descriptor.update(disposition="REPORTED_TIME_CONTEXT", timestamp=timestamp.copy())
                elif kind == 0x0e:
                    if flags & ~1 or len(body) != 12:
                        raise ValueError("invalid INFO_DST")
                    descriptor["disposition"] = "DESTINATION_RETAINED_NOT_RECEIPT"
                elif kind in (1, 6, 7, 8):
                    descriptor["disposition"] = "PADDING_RETAINED" if kind == 1 else "NOT_INTERPRETED_CONTROL"
                else:
                    raise ValueError("unsupported RTPS submessage: source changes, fragments, security or unknown context")
                packet["submessages"].append(descriptor)
                position = end
        report_size += len(encoded(packet)) + 1
        if report_size > MAX_REPORT_BYTES:
            raise ValueError("source packet report exceeds bound")
        report["packets"].append(packet)
    if report["source_packets"] != len(records[1]):
        raise ValueError("packet accounting mismatch")
    if not report["selected_data"]:
        raise ValueError("no selected writer DATA")
    if len(encoded(report)) > MAX_REPORT_BYTES:
        raise ValueError("complete report exceeds bound")
    return output.getvalue(), report


def read_bounded(path, limit):
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("saved input exceeds bound")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--binding", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output directory already exists")
        data = read_bounded(args.input, LIMIT)
        binding_raw = read_bounded(args.binding, MAX_BINDING_BYTES)
        binding = json.loads(binding_raw.decode("utf-8"), object_pairs_hook=unique_object)
        text, report = convert_pcap_rtps(data, args.port, binding=binding)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "source.pcap").write_bytes(data)
        (args.output / "binding.json").write_bytes(binding_raw)
        (args.output / "observations.csv").write_text(text, encoding="utf-8")
        (args.output / "report.json").write_bytes(encoded(report) + b"\n")
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(2, f"RTPS saved conversion refused: {exc}\n")


if __name__ == "__main__":
    main()
