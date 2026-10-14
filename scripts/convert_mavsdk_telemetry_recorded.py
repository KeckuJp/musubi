"""Finite saved MAVSDK responses; no RPC, runtime schema loading or clock inference."""
import csv
import io
import json
import math
import re

from scripts import convert_ardupilot_battery_csv as prior
from scripts.convert_vda_load_report import read_jsonl_window

PIN = "d3cb35f49d1379d2d95a6d22cdbe40ff4074e9bf"
MODES = "UNKNOWN READY TAKEOFF HOLD MISSION RETURN_TO_LAUNCH LAND OFFBOARD FOLLOW_ME MANUAL ALTCTL POSCTL ACRO STABILIZED RATTITUDE".split()
LANDED = "UNKNOWN ON_GROUND IN_AIR TAKING_OFF LANDING".split()
HEALTH = {1: "gyrometer_calibration_ok", 2: "accelerometer_calibration_ok", 3: "magnetometer_calibration_ok", 5: "local_position_ok", 6: "global_position_ok", 7: "home_position_ok", 8: "armable"}
OLD = {"battery": prior.GRPC_FORMAT, "attitude": prior.ATTITUDE_GRPC, "velocity": prior.VELOCITY_GRPC}
NEW = {"position", "flight_mode", "armed", "in_air", "landed_state", "health"}
META = ["record_time_us", "source_id_hex", "message_kind", "source_file_sha256", "source_record_index", "source_record_byte_offset", "source_record_hex", "proto3_absent_or_zero_fields_hex", "unknown_wire_fields", "interpretation_basis"]


def decode(record, kind):
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, unknown_fields
    import google.protobuf
    from google.protobuf.message import DecodeError
    if google.protobuf.__version__ != "6.33.5":
        raise ValueError("protobuf6.33.5 required")
    wire = record.get("grpc_frame_hex")
    if not isinstance(wire, str) or len(wire) > 28000 or not re.fullmatch(r"(?:[a-fA-F0-9]{2})*", wire):
        raise ValueError("bounded complete response hex required")
    data = bytes.fromhex(wire)
    if len(data) < 5 or data[0] != 0 or int.from_bytes(data[1:5], "big") != len(data)-5:
        raise ValueError("one complete noncompressed response required")
    schema = descriptor_pb2.FileDescriptorProto(name="selected.proto", package="selected", syntax="proto3")
    nested = kind in ("position", "health")
    fields = ({1: ("latitude_deg", 1), 2: ("longitude_deg", 1), 3: ("altitude_amsl_m", 2), 4: ("altitude_takeoff_relative_m", 2)} if kind == "position" else
              {n: ("reported_"+name, 8) for n, name in HEALTH.items()} if kind == "health" else
              {1: ("reported_"+kind, 8 if kind in ("armed", "in_air") else 14)})
    if kind in ("flight_mode", "landed_state"):
        enum = schema.enum_type.add(name="ReportedState")
        for number, name in enumerate(MODES if kind == "flight_mode" else LANDED):
            enum.value.add(name="STATE_"+name, number=number)
    descriptor = schema.message_type.add(name="Value" if nested else "Response")
    for number, (name, field_type) in fields.items():
        field = descriptor.field.add(name=name, number=number, label=1, type=field_type)
        if field_type == 14:
            field.type_name = ".selected.ReportedState"
    if nested:
        response = schema.message_type.add(name="Response")
        response.field.add(name="value", number=1, label=1, type=11, type_name=".selected.Value")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    message = message_factory.GetMessageClass(pool.FindMessageTypeByName("selected.Response"))()
    try:
        message.ParseFromString(data[5:])
    except DecodeError as exc:
        raise ValueError("malformed selected response") from exc
    unknown = len(unknown_fields.UnknownFieldSet(message))
    if nested:
        if not message.HasField("value"):
            raise ValueError("selected nested response absent")
        message = message.value
        unknown += len(unknown_fields.UnknownFieldSet(message))
    present = {field.name for field, _ in message.ListFields()}
    result = {}
    for name, field_type in fields.values():
        value = getattr(message, name)
        if field_type in (1, 2):
            if math.isnan(value):
                value = ""
            elif not math.isfinite(value):
                raise ValueError("infinite reported position")
            elif name in ("latitude_deg", "longitude_deg") and abs(value) > (90 if name == "latitude_deg" else 180):
                raise ValueError("reported coordinate outside definition")
        elif field_type == 8:
            value = "TRUE_REPORTED" if value else "FALSE_OR_ABSENT_REPORTED"
        else:
            names = MODES if kind == "flight_mode" else LANDED
            result[name+"_code"] = value
            value = names[value] if 0 <= value < len(names) else "UNKNOWN_ENUM_"+str(value)
        result[name] = value
    result["proto3_absent_or_zero_fields_hex"] = "hex:"+",".join(name for name, _ in fields.values() if name not in present).encode().hex()
    result["unknown_wire_fields"] = unknown
    return result


def convert_path(path, *, kind, start_record, stop_record, expected_size=None, expected_sha256=None):
    if kind not in NEW | OLD.keys():
        raise ValueError("explicit qualified response selector required")
    lines, offsets, count, selected_bytes, digest = read_jsonl_window(path, start_record=start_record, stop_record=stop_record,
        expected_size=expected_size, expected_sha256=expected_sha256)
    rows = []
    for index, line in enumerate(lines):
        record = json.loads(line, object_pairs_hook=prior.unique_object)
        if not isinstance(record, dict) or record.get("sdk_version") != "3.17.4" or record.get("proto_commit") != PIN:
            raise ValueError("qualified SDK and proto pin required")
        source = record.get("source_id")
        stamp = record.get("capture_time_us")
        if not isinstance(source, str) or not source or len(source.encode()) > 256 or any(ord(c) < 32 for c in source):
            raise ValueError("explicit bounded source declaration required")
        if type(stamp) is not int or not 0 <= stamp <= 2**63-1:
            raise ValueError("capture integer microseconds required")
        if kind in OLD:
            prior_rows = list(csv.DictReader(io.StringIO(prior.convert(line, OLD[kind]))))
            if len(prior_rows) != 1:
                raise ValueError("unexpected adopted resolver row count")
            result = prior_rows[0]
        else:
            result = decode(record, kind)
        result.update(record_time_us=stamp, source_id_hex="hex:"+source.encode().hex(), message_kind=kind,
            source_file_sha256="sha256:"+digest, source_record_index=start_record+index,
            source_record_byte_offset=offsets[index], source_record_hex="hex:"+line[:-1].encode().hex(),
            interpretation_basis="CALLER_SOURCE_UNAUTHENTICATED_CAPTURE_UNKNOWN_NO_RPC_SUCCESS_NO_PHYSICAL_STATE_GUARANTEE")
        rows.append(result)
    names = list(dict.fromkeys(META+[key for row in rows for key in row]))
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, names, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
        if out.tell() > 128*1024**2:
            raise ValueError("bounded output exceeded")
    return out.getvalue(), dict(source_sha256="sha256:"+digest, physical_records=count,
        selected_records=len(lines), excluded_records=count-len(lines), start_record=start_record,
        stop_record=stop_record, selected_bytes=selected_bytes, outside_semantics="NOT_DECODED",
        source_id_basis="CALLER_DECLARED_NOT_AUTHENTICATED", proto_commit=PIN, kind=kind)
