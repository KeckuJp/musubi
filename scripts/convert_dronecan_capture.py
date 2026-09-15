#!/usr/bin/env python3
"""Offline classic extended CAN CSV -> selected DroneCAN measurements, never a node."""
import argparse
import csv
import hashlib
from configparser import RawConfigParser, Error as ConfigError
import io
import json
import math
from pathlib import Path
import re
import struct

if __package__:
    from .convert_ardupilot_battery_csv import NORMALIZED, integer
    from .recorded_units import UNITS, normalize
else:
    from convert_ardupilot_battery_csv import NORMALIZED, integer
    from recorded_units import UNITS, normalize

TYPES = {341: "NodeStatus", 1092: "BatteryInfo", 1027: "RawAirData", 1002: "MagneticFieldStrength2",
         1034: "ESCStatus", 1045: "RPM", 1011: "ActuatorStatus"}


def convert_canopen_dictionary(text, capture_time_us):
    """Read object declarations only; never evaluate expressions or access a node."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63 or len(text.encode()) > 256000:
        raise ValueError("bounded dictionary and explicit capture clock required")
    parser = RawConfigParser(inline_comment_prefixes=(";",), strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except ConfigError as error:
        raise ValueError("invalid dictionary INI") from error
    if parser.defaults() or len(parser.sections()) > 4096:
        raise ValueError("dictionary inheritance or section bound")
    def number(value, maximum):
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
            raise ValueError("explicit declaration integer required")
        result = int(value, 16 if value.lower().startswith("0x") else 10)
        if result > maximum:
            raise ValueError("declaration integer outside range")
        return result
    types = {1: ("BOOLEAN", 8), 2: ("INTEGER8", 8), 3: ("INTEGER16", 16), 4: ("INTEGER32", 32),
             5: ("UNSIGNED8", 8), 6: ("UNSIGNED16", 16), 7: ("UNSIGNED32", 32), 8: ("REAL32", 32),
             9: ("VISIBLE_STRING", None), 10: ("OCTET_STRING", None), 11: ("UNICODE_STRING", None),
             12: ("TIME_OF_DAY", None), 13: ("TIME_DIFFERENCE", None), 15: ("DOMAIN", None),
             16: ("INTEGER24", 24), 17: ("REAL64", 64), 18: ("INTEGER40", 40), 19: ("INTEGER48", 48),
             20: ("INTEGER56", 56), 21: ("INTEGER64", 64), 22: ("UNSIGNED24", 24),
             24: ("UNSIGNED40", 40), 25: ("UNSIGNED48", 48), 26: ("UNSIGNED56", 56), 27: ("UNSIGNED64", 64)}
    entries = {}
    for section in parser.sections():
        match = re.fullmatch(r"([0-9A-Fa-f]{4})(?:[Ss]ub([0-9A-Fa-f]{1,2}))?", section)
        if not match:
            if re.match(r"[0-9A-Fa-f]{4}[Ss]ub", section):
                raise ValueError("invalid dictionary subindex")
            continue
        key = (int(match[1], 16), None if match[2] is None else int(match[2], 16))
        if key in entries:
            raise ValueError("duplicate canonical dictionary entry")
        values = dict(parser.items(section))
        if not values.get("ParameterName"):
            raise ValueError("dictionary name missing")
        entries[key] = (values, number(values.get("ObjectType", "7"), 255))
    expanded = set()
    for (index, subindex), (values, kind) in list(entries.items()):
        if subindex is not None or "CompactSubObj" not in values:
            continue
        count = number(values["CompactSubObj"], 255)
        if not count:
            continue
        # Incomplete/other compact forms stay explicitly unexpanded. Only the
        # fixed DS306 array template is interpreted, not implicit PDOs or nodes.
        if kind != 8 or "DataType" not in values or "AccessType" not in values:
            continue
        if count == 255 or values.get("SubNumber", "") not in ("", "0", "0x0"):
            raise ValueError("unsupported compact array extent or SubNumber")
        if any(i == index and s is not None for i, s in entries):
            raise ValueError("compact and explicit subentries conflict")
        lists = {}
        for suffix in ("Name", "Value", "Denotation"):
            matches = [s for s in parser.sections() if s.lower() == f"{index:04x}{suffix.lower()}"]
            if len(matches) > 1:
                raise ValueError("duplicate compact list")
            items = dict(parser.items(matches[0])) if matches else {"NrOfEntries": "0"}
            expected = number(items.pop("NrOfEntries", ""), 254)
            selected = {}
            for position, value in items.items():
                if not re.fullmatch(r"[1-9][0-9]*", position) or not 1 <= int(position) <= count or not value:
                    raise ValueError("invalid compact list entry")
                selected[int(position)] = value
            if len(selected) != expected:
                raise ValueError("compact list count mismatch")
            lists[suffix] = selected
        if len(entries) + count + 1 > 65536:
            raise ValueError("expanded dictionary entry bound")
        entries[index, 0] = (dict(ParameterName="NrOfObjects", ObjectType="7", DataType="5",
                                  AccessType="ro", DefaultValue=str(count), PDOMapping="0"), 7)
        expanded.add((index, 0))
        for position in range(1, count + 1):
            derived = {k: v for k, v in values.items() if k not in
                       ("CompactSubObj", "SubNumber", "LowLimit", "HighLimit", "ParameterValue", "Denotation")}
            derived.update(ObjectType="7", ParameterName=lists["Name"].get(position, values["ParameterName"] + str(position)))
            if position in lists["Value"]:
                derived["ParameterValue"] = lists["Value"][position]
            if position in lists["Denotation"]:
                derived["Denotation"] = lists["Denotation"][position]
            entries[index, position] = (derived, 7)
            expanded.add((index, position))
    output = io.StringIO(newline="")
    fields = ["record_time_us", "object_index", "object_subindex", "object_name_hex", "object_kind",
              "object_type_code", "data_type_code", "data_type_reported", "declared_bit_width",
              "access_reported", "pdo_mappable_declared", "declaration_status", "source_section_hex"]
    writer = csv.DictWriter(output, fields, lineterminator="\n"); writer.writeheader()
    for (index, subindex), (values, kind) in entries.items():
        if subindex is not None and ((index, None) not in entries or entries[index, None][1] not in (8, 9)):
            raise ValueError("orphan dictionary subentry")
        data_type = number(values["DataType"], 65535) if "DataType" in values else None
        if kind in (2, 7) and (data_type is None or "AccessType" not in values):
            raise ValueError("variable declaration lacks type or access")
        if "SubNumber" in values and not number(values.get("CompactSubObj", "0"), 255):
            count = sum(i == index and s is not None for i, s in entries)
            if number(values["SubNumber"], 256) != count:
                raise ValueError("declared subentry count mismatch")
        name, width = types.get(data_type, ("UNKNOWN" if data_type is not None else "NOT_DECLARED", None))
        writer.writerow(dict(record_time_us=capture_time_us, object_index=index, object_subindex=subindex,
            object_name_hex="hex:" + values["ParameterName"].encode().hex(), object_type_code=kind,
            object_kind={2: "DOMAIN", 7: "VARIABLE", 8: "ARRAY", 9: "RECORD"}.get(kind, "UNKNOWN"),
            data_type_code=data_type, data_type_reported=name, declared_bit_width=width,
            access_reported=values.get("AccessType", "").lower(),
            pdo_mappable_declared=number(values.get("PDOMapping", "0"), 1),
            declaration_status=("COMPACT_EXPANDED_NOT_DEVICE_VERIFIED" if (index, subindex) in expanded
                else "COMPACT_TEMPLATE_EXPANDED" if (index, 0) in expanded and subindex is None
                else "COMPACT_NOT_EXPANDED" if number(values.get("CompactSubObj", "0"), 255)
                else "DECLARED_NOT_DEVICE_VERIFIED"),
            source_section_hex="hex:" + json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode().hex()))
    if not entries:
        raise ValueError("no explicit dictionary entries")
    return output.getvalue(), dict(observations=len(entries), sections=len(parser.sections()),
        source_document_hex="hex:" + text.encode().hex(), scope="declarations only; no expressions or node access")
FIELDS = list(NORMALIZED) + [
    "battery_temperature_k", "battery_remaining_energy_j", "battery_health_fraction",
    "node_uptime_s", "node_reported_health", "node_reported_mode",
    "air_static_pressure_pa", "air_differential_pressure_pa", "air_temperature_k",
    "magnetic_body_x_t", "magnetic_body_y_t", "magnetic_body_z_t", "sensor_id",
    "esc_voltage_v", "esc_current_a", "esc_temperature_k", "esc_reported_error_count",
    "esc_index", "reported_rpm", "esc_demand_fraction", "rpm_reported_health", "rpm_unknown_flags",
    "actuator_id", "actuator_unit_basis", "actuator_position_m", "actuator_position_rad",
    "actuator_force_n", "actuator_torque_n_m", "actuator_linear_speed_m_s",
    "actuator_angular_speed_rad_s", "actuator_demand_fraction"]
META = ["record_time_us", "first_frame_time_us", "source_node_id", "transfer_id",
        "data_type", "source_frames_hex"]


def convert_canopen_pdo(text, dictionary, cob_id, mapping_index, *, bit_fields=False, booleans=False):
    """Caller-qualified saved TPDO; no node, SDO or configuration application."""
    if type(booleans) is not bool:
        raise ValueError("explicit Boolean selection required")
    if type(cob_id) is not int or not 1 <= cob_id <= 0x7ff or type(mapping_index) is not int or not 0x1a00 <= mapping_index <= 0x1bff:
        raise ValueError("explicit standard COB-ID and TPDO map required")
    # Reuse the adopted dictionary validation; its capture zero is internal only,
    # never an Observation time. Actual rows use the saved frame's capture clock.
    declared, dictionary_report = convert_canopen_dictionary(dictionary, 0)
    entries = {}
    for row in csv.DictReader(io.StringIO(declared)):
        key = (int(row["object_index"]), int(row["object_subindex"]) if row["object_subindex"] else None)
        entries[key] = row, json.loads(bytes.fromhex(row["source_section_hex"][4:]))
    def literal(values):
        value = values.get("ParameterValue", values.get("DefaultValue", ""))
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]{1,8}|[0-9]{1,10})", value):
            raise ValueError("explicit mapping value required; expressions not executed")
        result = int(value, 16 if value.lower().startswith("0x") else 10)
        if result > 0xffffffff:
            raise ValueError("mapping value exceeds uint32")
        return result
    try:
        if entries[mapping_index, 0][0]["data_type_code"] != "5":
            raise ValueError("mapping count must be UNSIGNED8")
        count = literal(entries[mapping_index, 0][1])
        if not 1 <= count <= 8:
            raise ValueError("nonempty classic PDO mapping required")
        slots, total = [], 0
        for position in range(1, count + 1):
            if entries[mapping_index, position][0]["data_type_code"] != "7":
                raise ValueError("mapping word must be UNSIGNED32")
            mapping = literal(entries[mapping_index, position][1])
            index, subindex, bits = mapping >> 16, (mapping >> 8) & 255, mapping & 255
            row, values = entries.get((index, subindex), entries.get((index, None)) if subindex == 0 else None)
            datatype = int(row["data_type_code"])
            if (row["object_kind"] != "VARIABLE" or row["pdo_mappable_declared"] != "1"
                    or row["declaration_status"] not in ("DECLARED_NOT_DEVICE_VERIFIED", "COMPACT_EXPANDED_NOT_DEVICE_VERIFIED")
                    or (datatype not in (2, 3, 4, 5, 6, 7, 8, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 27)
                        and not (booleans and datatype == 1))
                    or (datatype == 1 and bits not in (1, 8))
                    or not 1 <= bits <= int(row["declared_bit_width"])
                    or (not bit_fields and (bits % 8 or bits != int(row["declared_bit_width"])))
                    or (datatype in (8, 17) and (total % 8 or bits != int(row["declared_bit_width"])))):
                raise ValueError("unsupported scalar PDO mapping")
            unit = values.get("Unit")
            if datatype == 1 and ("Unit" in values or "Factor" in values):
                raise ValueError("Boolean must not have engineering unit or factor")
            if datatype != 1 and unit not in UNITS:
                raise ValueError("qualified engineering unit required")
            factor = float(values.get("Factor", "1"))
            if not math.isfinite(factor) or (datatype in (8, 17) and factor != 1):
                raise ValueError("invalid or ambiguous physical factor")
            slots.append((index, subindex, bits, datatype, unit, factor, row["object_name_hex"]))
            total += bits
        if total > 64:
            raise ValueError("mapping exceeds classic PDO")
    except (KeyError, TypeError) as error:
        raise ValueError("mapping entry is missing") from error
    digest = "sha256:" + hashlib.sha256(dictionary.encode()).hexdigest()
    fields = ["record_time_us", "source_frame_hex", "dictionary_sha256", "pdo_cob_id", "pdo_mapping_index",
              "pdo_slot", "object_index", "object_subindex", "object_name_hex", "data_type_code",
              "raw_value", "configured_unit", "configured_factor", "quantity_status", *dict.fromkeys(v[0] for v in UNITS.values())]
    if bit_fields:
        fields += ["mapped_bit_offset", "mapped_bit_width"]
    if booleans:
        fields += ["boolean_reported"]
    output = io.StringIO(newline=""); writer = csv.DictWriter(output, fields, lineterminator="\n"); writer.writeheader()
    report = dict(dictionary_report, dictionary_sha256=digest, frames=0, decoded_frames=0,
                  output_records=0, unsupported_frames=[], qualification="caller-configured TPDO and python Unit/Factor extensions; not device verified")
    if bit_fields:
        report.update(mapped_bits=total, retained_padding_bits=(-total) % 8)
    report_bytes = len(json.dumps(report).encode())
    for line, original, timestamp, identifier, payload in capture_rows(text, extended=False):
        report["frames"] += 1
        if identifier != cob_id:
            report["unsupported_frames"].append(dict(line=line, frame=original, reason="UNSELECTED_COB_ID"))
            report_bytes += len(json.dumps(report["unsupported_frames"][-1]).encode())
            if report_bytes > 16 * 1024 * 1024:
                raise ValueError("PDO accounting exceeds bound")
            continue
        if len(payload) != (total + 7) // 8:
            raise ValueError("PDO size does not match selected mapping")
        offset = 0
        for slot, (index, subindex, bits, datatype, unit, factor, name) in enumerate(slots):
            if datatype in (8, 17):
                data = payload[offset // 8:(offset + bits) // 8]
                value = struct.unpack("<f" if datatype == 8 else "<d", data)[0]
            else:
                value = (int.from_bytes(payload, "little") >> offset) & ((1 << bits) - 1)
                if datatype in (2, 3, 4, 16, 18, 19, 20, 21) and value & (1 << (bits - 1)):
                    value -= 1 << bits
            row = dict(record_time_us=timestamp, source_frame_hex="hex:" + json.dumps(original).encode().hex(),
                       dictionary_sha256=digest, pdo_cob_id=cob_id, pdo_mapping_index=mapping_index,
                       pdo_slot=slot, object_index=index, object_subindex=subindex, object_name_hex=name,
                       data_type_code=datatype, raw_value=value if math.isfinite(value) else "NONFINITE",
                       configured_unit=unit, configured_factor=factor, quantity_status="NONFINITE_RETAINED")
            if bit_fields:
                row.update(mapped_bit_offset=offset, mapped_bit_width=bits)
            offset += bits
            if datatype == 1:
                row.update(boolean_reported="TRUE" if value else "FALSE",
                           configured_factor="", quantity_status="BOOLEAN_REPORTED_NOT_PHYSICAL_STATE")
            elif math.isfinite(value):
                if factor != 1 and type(value) is int and abs(value) > 2**53:
                    raise ValueError("scaled PDO integer loses precision")
                field, normalized = normalize(value if factor == 1 else value * factor, unit)
                row.update({field: normalized, "quantity_status": "REPORTED_CONFIGURED_QUANTITY"})
            writer.writerow(row); report["output_records"] += 1
            if output.tell() > 128 * 1024 * 1024:
                raise ValueError("PDO output exceeds bound")
        report["decoded_frames"] += 1
    if not report["decoded_frames"]:
        raise ValueError("no selected PDO frames")
    report.update(observations=report["output_records"], scope="selected mapped measurements; dictionary declarations retained in source document")
    return output.getvalue(), report


def finite(value, *, scale=1):
    value = float(value)
    if math.isnan(value):
        return None
    if not math.isfinite(value * scale):
        raise ValueError("nonfinite reported quantity")
    return value * scale


def meanings(type_id, payload, actuator_layout=None):
    result = dict.fromkeys(FIELDS)
    if type_id == 1092:
        result.update(battery_voltage_v=finite(payload.voltage),
                      battery_current_a=finite(payload.current),
                      battery_temperature_k=finite(payload.temperature),
                      battery_remaining_energy_j=finite(payload.remaining_capacity_wh, scale=3600),
                      sensor_id=int(payload.battery_id))
        for source, target in [(payload.state_of_charge_pct, "battery_remaining_fraction"),
                               (payload.state_of_health_pct, "battery_health_fraction")]:
            if source > 100 and source != 127:
                raise ValueError("invalid reported percent")
            result[target] = None if source == 127 else source / 100
        v, a = result["battery_voltage_v"], result["battery_current_a"]
        result["battery_power_w"] = None if v is None or a is None else finite(v * a)
        for name in ("battery_voltage_v", "battery_temperature_k", "battery_remaining_energy_j"):
            if result[name] is not None and result[name] < 0:
                raise ValueError("negative absolute quantity")
    elif type_id == 341:
        result.update(node_uptime_s=int(payload.uptime_sec),
                      node_reported_health=("OK", "WARNING", "ERROR", "CRITICAL")[payload.health],
                      node_reported_mode={0: "OPERATIONAL", 1: "INITIALIZATION", 2: "MAINTENANCE",
                                          3: "SOFTWARE_UPDATE", 7: "OFFLINE"}.get(payload.mode, f"UNKNOWN_{payload.mode}"))
    elif type_id == 1027:
        result.update(air_static_pressure_pa=finite(payload.static_pressure),
                      air_differential_pressure_pa=finite(payload.differential_pressure),
                      air_temperature_k=finite(payload.static_air_temperature))
        for name in ("air_static_pressure_pa", "air_temperature_k"):
            if result[name] is not None and result[name] < 0:
                raise ValueError("negative absolute quantity")
    elif type_id == 1002:
        result["sensor_id"] = int(payload.sensor_id)
        for axis, value in zip("xyz", payload.magnetic_field_ga):
            result[f"magnetic_body_{axis}_t"] = finite(value, scale=1e-4)
    elif type_id == 1034:
        result.update(esc_voltage_v=finite(payload.voltage), esc_current_a=finite(payload.current),
                      esc_temperature_k=finite(payload.temperature), esc_index=int(payload.esc_index),
                      esc_reported_error_count=int(payload.error_count), reported_rpm=int(payload.rpm),
                      esc_demand_fraction=int(payload.power_rating_pct) / 100)
        for name in ("esc_voltage_v", "esc_temperature_k"):
            if result[name] is not None and result[name] < 0:
                raise ValueError("negative ESC absolute quantity")
    elif type_id == 1045:
        rpm = finite(payload.rpm)
        if rpm is None:
            raise ValueError("RPM message has no declared NaN sentinel")
        flags = int(payload.flags)
        result.update(sensor_id=int(payload.sensor_id), reported_rpm=rpm,
                      rpm_reported_health="UNHEALTHY" if flags & 1 else "NO_UNHEALTHY_FLAG",
                      rpm_unknown_flags=flags & ~1)
    elif type_id == 1011:
        identifier = int(payload.actuator_id)
        kind = (actuator_layout or {}).get(identifier)
        if kind not in ("linear", "angular"):
            raise ValueError("actuator unit configuration missing for this ID")
        keys = (("actuator_position_m", "actuator_force_n", "actuator_linear_speed_m_s")
                if kind == "linear" else
                ("actuator_position_rad", "actuator_torque_n_m", "actuator_angular_speed_rad_s"))
        result.update(zip(keys, [finite(payload.position), finite(payload.force), finite(payload.speed)]))
        percent = int(payload.power_rating_pct)
        if 100 < percent < 127:
            raise ValueError("invalid actuator demand percent")
        result.update(actuator_id=identifier, actuator_unit_basis="OPERATOR_DECLARED_" + kind.upper(),
                      actuator_demand_fraction=None if percent == 127 else percent / 100)
    return [result[key] for key in FIELDS]


def capture_rows(text, *, extended, fd=False):
    """Shared bounded selected-frame export parsing; never opens a CAN device."""
    if len(text.encode()) > 16 * 1024 * 1024 or '"' in text:
        raise ValueError("unsupported capture representation or size")
    rows = list(csv.reader(io.StringIO(text)))
    id_column = "extended_id" if extended else "standard_id"
    if len(rows) < 2 or rows[0] != ["time_us", id_column, *(["fd_flags"] if fd else []), "data_hex"]:
        raise ValueError("expected explicit selected CAN export")
    previous = None
    for line, row in enumerate(rows[1:], 2):
        if len(row) != (4 if fd else 3):
            raise ValueError("ragged frame record")
        timestamp = integer(row[0], 2**63 - 1, "capture time")
        if previous is not None and timestamp < previous:
            raise ValueError("decreasing capture time")
        previous = timestamp
        id_pattern = r"[0-9a-fA-F]{1,8}" if extended else r"[0-9a-fA-F]{1,3}"
        data_pattern = r"(?:[0-9a-fA-F]{2}){0,64}" if fd else r"(?:[0-9a-fA-F]{2}){1,8}" if extended else r"(?:[0-9a-fA-F]{2}){0,8}"
        if fd and not re.fullmatch(r"[0-9a-fA-F]", row[2]):
            raise ValueError("invalid captured FD flags nibble")
        if not re.fullmatch(id_pattern, row[1]) or not re.fullmatch(data_pattern, row[-1]):
            raise ValueError("invalid classic frame")
        identifier = int(row[1], 16)
        if identifier > (0x1fffffff if extended else 0x7ff):
            raise ValueError("identifier exceeds selected frame kind")
        yield line, row, timestamp, identifier, bytes.fromhex(row[-1])


def convert(text, *, actuator_layout=None):
    # Import the external decoder only. No make_node, driver, subscriber or socket.
    import dronecan
    from dronecan.transport import Frame, Transfer
    layout = {} if actuator_layout is None else actuator_layout
    if not isinstance(layout, dict) or any(type(k) is not int or not 0 <= k <= 255
            or v not in ("linear", "angular") for k, v in layout.items()):
        raise ValueError("invalid actuator unit configuration")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(META + FIELDS)
    pending = {}
    report = {"frames": 0, "decoded_transfers": 0, "decoded_frames": 0, "unsupported_frames": [],
              "operator_declared_actuator_layout": layout}
    for line, row, timestamp, frame_id, data in capture_rows(text, extended=True):
        report["frames"] += 1
        type_id = (frame_id >> 8) & 0xffff
        if frame_id & 0x80 or not frame_id & 0x7f or type_id not in TYPES or (type_id == 1011 and not layout):
            report["unsupported_frames"].append({"line": line, "source": row})
            continue
        frame = Frame(frame_id, data)
        # Avoid the decoder manager's implicit replacement/timeout discard behavior.
        key = frame_id
        if frame.start_of_transfer:
            if key in pending:
                raise ValueError("incomplete supported transfer replaced by a new start")
            pending[key] = ([], [], timestamp)
        if key not in pending:
            raise ValueError("orphan supported continuation")
        frames, originals, first_time = pending[key]
        frames.append(frame); originals.append(row)
        if len(frames) > 128 or len(pending) > 1024 or timestamp - first_time > 2_000_000:
            raise ValueError("supported transfer bound exceeded")
        if not frame.end_of_transfer:
            continue
        transfer = Transfer()
        try:
            transfer.from_frames(frames)
            # The pinned decoder does not reject every trailing/truncated payload.
            # Length of its re-encoded typed payload must match (CRC excluded).
            wire_length = sum(len(f.bytes) - 1 for f in frames) - (2 if len(frames) > 1 else 0)
            if len(Transfer(payload=transfer.payload).payload) != wire_length:
                raise ValueError("payload length does not match selected DSDL")
            values = meanings(type_id, transfer.payload, layout)
        except (dronecan.UAVCANException, ValueError, IndexError, TypeError, AttributeError) as exc:
            raise ValueError("invalid supported DroneCAN transfer") from exc
        source = "hex:" + json.dumps(originals, separators=(",", ":")).encode().hex()
        writer.writerow([timestamp, first_time, transfer.source_node_id, transfer.transfer_id,
                         TYPES[type_id], source] + ["" if v is None else v for v in values])
        report["decoded_transfers"] += 1
        report["decoded_frames"] += len(frames)
        del pending[key]
        if output.tell() > 16 * 1024 * 1024:
            raise ValueError("converted CSV too large; split at transfer boundaries")
    if pending:
        raise ValueError("incomplete supported transfer at end of capture")
    if not report["decoded_transfers"]:
        raise ValueError("no supported transfer; input remains unsupported")
    return output.getvalue(), report


def parse_layout(entries):
    layout = {}
    for entry in entries:
        identifier, separator, kind = entry.partition(":")
        identifier = integer(identifier, 255, "actuator ID")
        if separator != ":" or kind not in ("linear", "angular") or identifier in layout:
            raise ValueError("duplicate or invalid actuator unit declaration")
        layout[identifier] = kind
    return layout


def convert_canopen(text, *, include_emergency=False):
    """Explicit CANopen CC heartbeat data-frame export; no node/transport creation."""
    output = io.StringIO(newline="")
    fields = ["record_time_us", "source_node_id", "node_reported_mode", "canopen_state_code", "source_frames_hex"]
    error_names = ["generic", "current", "voltage", "temperature", "communication", "device_profile", "reserved", "manufacturer"]
    error_fields = ["canopen_error_code", "canopen_error_report_kind", "canopen_error_register", "canopen_manufacturer_data_hex"]
    error_fields += ["canopen_error_" + name + "_reported" for name in error_names if name != "reserved"]
    if include_emergency:
        fields += error_fields
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(fields)
    report = {"frames": 0, "decoded_frames": 0, "unsupported_frames": []}
    for _line, row, timestamp, identifier, payload in capture_rows(text, extended=False):
        report["frames"] += 1
        retained = "hex:" + json.dumps([row], separators=(",", ":")).encode().hex()
        if include_emergency and 0x81 <= identifier <= 0xff:
            if len(row[2]) != 16:
                raise ValueError("invalid emergency payload length")
            code, register = int.from_bytes(payload[:2], "little"), payload[2]
            if register & 0x40:
                raise ValueError("reserved error register bit")
            writer.writerow([timestamp, identifier - 0x80, "", "", retained,
                code, "ERROR_RESET_REPORTED" if code == 0 else "ERROR_REPORTED", register,
                "hex:" + payload[3:].hex()]
                + [int(bool(register & (1 << bit))) for bit in range(8) if bit != 6])
            report["decoded_frames"] += 1
            if output.tell() > 16 * 1024 * 1024:
                raise ValueError("converted CSV too large")
            continue
        if not 0x701 <= identifier <= 0x77f:
            report["unsupported_frames"].append(row)
            continue
        if len(row[2]) != 2:
            raise ValueError("invalid heartbeat payload length")
        code = int(row[2], 16)
        if code & 0x80:
            raise ValueError("reserved heartbeat bit or node-guarding frame")
        state = {0: "BOOT_UP", 4: "STOPPED", 5: "OPERATIONAL", 127: "PRE_OPERATIONAL"}.get(code, f"UNKNOWN_{code}")
        writer.writerow([timestamp, identifier - 0x700, state, code, retained]
                        + ([""] * len(error_fields) if include_emergency else []))
        report["decoded_frames"] += 1
        if output.tell() > 16 * 1024 * 1024:
            raise ValueError("converted CSV too large")
    if not report["decoded_frames"]:
        raise ValueError("no supported heartbeat")
    return output.getvalue(), report


def diagnostic_messages(text, report, bam):
    pending = {}
    for line, row, timestamp, identifier, payload in capture_rows(text, extended=True):
        report["frames"] += 1
        pgn = (identifier >> 8) & 0x3ffff
        source = identifier & 255
        if pgn in (65226, 65227):
            if len(payload) != 8 or payload[6:] != b"\xff\xff":
                raise ValueError("not the selected single-frame DM layout")
            yield timestamp, timestamp, source, pgn, payload[:6], [row], line
        elif bam and pgn == 0xecff:
            if source in pending:
                raise ValueError("overlapping source transport")
            if len(payload) != 8:
                raise ValueError("invalid transport announcement length")
            target = int.from_bytes(payload[5:8], "little")
            if payload[0] != 32 or target not in (65226, 65227):
                report["unsupported_frames"].append(dict(line=line, source=row, reason="unselected transport"))
                continue
            size = int.from_bytes(payload[1:3], "little")
            if not 10 <= size <= 1785 or (size - 2) % 4 or payload[3] != (size + 6) // 7 or payload[4] != 255:
                raise ValueError("invalid diagnostic BAM declaration")
            pending[source] = dict(size=size, packets=payload[3], pgn=target,
                                   first=timestamp, rows=[row], data=bytearray(), next=1)
        elif bam and pgn == 0xebff and source in pending:
            transfer = pending[source]
            if len(payload) != 8 or payload[0] != transfer["next"]:
                raise ValueError("missing, duplicate or reordered transport packet")
            transfer["rows"].append(row)
            transfer["data"].extend(payload[1:])
            transfer["next"] += 1
            if payload[0] == transfer["packets"]:
                size = transfer["size"]
                if any(value != 255 for value in transfer["data"][size:]):
                    raise ValueError("non-reserved transport padding")
                del pending[source]
                yield timestamp, transfer["first"], source, transfer["pgn"], bytes(transfer["data"][:size]), transfer["rows"], line
        else:
            report["unsupported_frames"].append(dict(line=line, source=row, reason="unselected PGN or orphan transport"))
    if pending:
        raise ValueError("incomplete selected diagnostic transport")


def convert_j1939(text, *, bam=False):
    """Fixed single-frame DM1/DM2, method0, recorded reports (not diagnosis)."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    lamps = ["malfunction_indicator", "red_stop", "amber_warning", "protect"]
    fmi_names = ["ABOVE_NORMAL_MOST_SEVERE", "BELOW_NORMAL_MOST_SEVERE", "DATA_ERRATIC",
                 "VOLTAGE_ABOVE_NORMAL", "VOLTAGE_BELOW_NORMAL", "CURRENT_BELOW_NORMAL",
                 "CURRENT_ABOVE_NORMAL", "MECHANICAL_SYSTEM_NOT_RESPONDING", "ABNORMAL_FREQUENCY",
                 "ABNORMAL_UPDATE_RATE", "ABNORMAL_RATE_OF_CHANGE", "ROOT_CAUSE_NOT_KNOWN",
                 "BAD_INTELLIGENT_DEVICE", "OUT_OF_CALIBRATION", "SPECIAL_INSTRUCTIONS",
                 "ABOVE_NORMAL_LEAST_SEVERE", "ABOVE_NORMAL_MODERATELY_SEVERE",
                 "BELOW_NORMAL_LEAST_SEVERE", "BELOW_NORMAL_MODERATELY_SEVERE",
                 "RECEIVED_NETWORK_DATA_IN_ERROR", "DATA_DRIFTED_HIGH", "DATA_DRIFTED_LOW"]
    fields = ["record_time_us", "source_node_id", "j1939_pgn", "diagnostic_report_kind", "spn_reported",
              "fmi_reported", "fmi_reported_name", "occurrence_count_code", "source_frames_hex",
              "dtc_index", "declared_dtc_count", "first_frame_time_us"]
    fields += [f"{name}_{kind}_code" for kind in ("lamp", "flash") for name in lamps]
    writer.writerow(fields)
    report = {"frames": 0, "decoded_frames": 0, "unsupported_frames": [],
              "observations": 0, "unsupported_dtcs": []}
    for timestamp, first, source, pgn, payload, rows, line in diagnostic_messages(text, report, bam):
        retained = "hex:" + json.dumps(rows, separators=(",", ":")).encode().hex()
        count = (len(payload) - 2) // 4
        decoded = False
        for index in range(count):
            dtc = payload[2 + 4 * index:6 + 4 * index]
            if dtc[3] & 128:
                report["unsupported_dtcs"].append(dict(line=line, dtc_index=index, raw_dtc_hex=dtc.hex(),
                                                       reason="unqualified SPN conversion method"))
                if len(report["unsupported_dtcs"]) > 50000:
                    raise ValueError("unsupported DTC accounting limit")
                continue
            spn = dtc[0] | (dtc[1] << 8) | ((dtc[2] & 224) << 11)
            kind = "ACTIVE_DTC_REPORTED" if pgn == 65226 else "PREVIOUS_DTC_REPORTED"
            if spn == 0:
                kind = "ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH"
            fmi = dtc[2] & 31
            meaning = fmi_names[fmi] if fmi < len(fmi_names) else ("NOT_AVAILABLE" if fmi == 31 else f"UNKNOWN_{fmi}")
            writer.writerow([timestamp, source, pgn, kind, spn, fmi, meaning, dtc[3] & 127,
                             retained, index, count, first]
                            + [(value >> shift) & 3 for value in payload[:2] for shift in (6,4,2,0)])
            decoded = True
            report["observations"] += 1
            if output.tell() > 16 * 1024 * 1024:
                raise ValueError("converted CSV too large")
        if decoded:
            report["decoded_frames"] += len(rows)
        else:
            report["unsupported_frames"].extend(dict(completed_at_line=line, source=row, reason="no qualified DTC") for row in rows)
    if not report["decoded_frames"]:
        raise ValueError("no qualified diagnostic report")
    return output.getvalue(), report


def convert_nmea2000_weather(text, *, heading=False):
    """Finite classic single-frame wind/temperature reports, not vessel qualification."""
    quantities = ("wind_speed_m_s", "wind_angle_rad", "actual_temperature_k", "reported_set_temperature_k")
    if heading:
        quantities = ("heading_rad", "heading_deviation_rad", "heading_variation_rad", "turn_rate_rad_s")
    fields = ["record_time_us", "source_node_id", "nmea2000_pgn", "source_frames_hex",
              "sid_code", "instance_code", "wind_reference_code", "wind_reference_reported",
              "temperature_source_code"]
    if heading:
        fields += ["heading_reference_code", "heading_reference_reported"]
    fields += [item for name in quantities for item in (name, name + "_status")]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    report = dict(frames=0, decoded_frames=0, unsupported_frames=[], clock="Unknown")
    retained_size = 0
    for line, original, timestamp, identifier, payload in capture_rows(text, extended=True):
        report["frames"] += 1
        pgn = (identifier >> 8) & 0x3ffff
        if pgn not in ((127250, 127251) if heading else (130306, 130312)):
            entry = dict(line=line, source=original, reason="unselected PGN")
            retained_size += len(json.dumps(entry))
            if retained_size > 16 * 1024 * 1024:
                raise ValueError("unsupported report exceeds bound")
            report["unsupported_frames"].append(entry)
            continue
        if len(payload) != 8:
            raise ValueError("selected NMEA2000 PGN requires eight bytes")
        row = dict(record_time_us=timestamp, source_node_id=identifier & 255, nmea2000_pgn=pgn,
                   sid_code=payload[0], source_frames_hex="hex:" + json.dumps([original], separators=(",", ":")).encode().hex())

        def scalar(name, offset, scale, *, size=2, signed=False):
            raw = int.from_bytes(payload[offset:offset + size], "little", signed=signed)
            missing = (1 << (size * 8 - int(signed))) - 1
            status = "NOT_AVAILABLE" if raw == missing else "OUT_OF_RANGE_REPORTED" if raw == missing - 1 else "REPORTED"
            row[name + "_status"] = status
            if status == "REPORTED":
                row[name] = raw * scale

        if pgn == 127250:
            scalar("heading_rad", 1, .0001)
            scalar("heading_deviation_rad", 3, .0001, signed=True)
            scalar("heading_variation_rad", 5, .0001, signed=True)
            reference = payload[7] & 3
            row.update(heading_reference_code=reference, heading_reference_reported=
                       ("TRUE_REPORTED", "MAGNETIC_REPORTED", "ERROR_REPORTED", "UNKNOWN_3")[reference])
        elif pgn == 127251:
            scalar("turn_rate_rad_s", 1, 3.125e-8, size=4, signed=True)
        elif pgn == 130306:
            scalar("wind_speed_m_s", 1, .01)
            scalar("wind_angle_rad", 3, .0001)
            reference = payload[5] & 7
            meanings = ("TRUE_GROUND_NORTH", "MAGNETIC_GROUND_NORTH", "APPARENT",
                        "TRUE_BOAT", "TRUE_WATER")
            row.update(wind_reference_code=reference, wind_reference_reported=
                       meanings[reference] if reference < 5 else f"UNKNOWN_{reference}")
        else:
            row.update(instance_code=payload[1], temperature_source_code=payload[2])
            scalar("actual_temperature_k", 3, .01)
            scalar("reported_set_temperature_k", 5, .01)
        writer.writerow(row)
        report["decoded_frames"] += 1
        if output.tell() > 16 * 1024 * 1024:
            raise ValueError("converted CSV exceeds bound")
    if not report["decoded_frames"]:
        raise ValueError("no selected NMEA2000 report")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("new_output_directory", type=Path)
    parser.add_argument("--protocol", choices=("dronecan", "canopen-heartbeat", "canopen-default-status", "canopen-dictionary", "canopen-pdo-python", "j1939-dm-single", "j1939-dm-bam", "nmea2000-weather", "nmea2000-heading"), default="dronecan")
    parser.add_argument("--pdo-dictionary", type=Path)
    parser.add_argument("--pdo-id", type=lambda value: int(value, 0))
    parser.add_argument("--pdo-map", type=lambda value: int(value, 0))
    parser.add_argument("--pdo-bit-fields", action="store_true", help="explicit configured integer bit mappings; no device access")
    parser.add_argument("--pdo-boolean", action="store_true", help="explicit saved Boolean reports; no physical-state inference")
    parser.add_argument("--capture-time-us", type=int, help="explicit capture clock for untimed dictionary only")
    parser.add_argument("--actuator", action="append", default=[], metavar="ID:linear|angular",
                        help="explicit configured feedback units; never sends actuator commands")
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8", newline="") as source:
            text = source.read(256001 if args.protocol == "canopen-dictionary" else 16 * 1024 * 1024 + 1)
        if args.capture_time_us is not None and args.protocol != "canopen-dictionary":
            raise ValueError("capture override is not valid for recorded frames")
        if args.protocol != "canopen-pdo-python" and (args.pdo_bit_fields or args.pdo_boolean or any(value is not None for value in (args.pdo_dictionary, args.pdo_id, args.pdo_map))):
            raise ValueError("PDO configuration requires its explicit selector")
        if args.protocol == "canopen-pdo-python":
            if args.actuator or args.pdo_dictionary is None:
                raise ValueError("explicit dictionary required without actuator configuration")
            with args.pdo_dictionary.open(encoding="utf-8", newline="") as source:
                dictionary = source.read(256001)
            converted, report = convert_canopen_pdo(text, dictionary, args.pdo_id, args.pdo_map,
                                                  bit_fields=args.pdo_bit_fields, booleans=args.pdo_boolean)
        elif args.protocol == "canopen-dictionary":
            if args.actuator:
                raise ValueError("actuator mapping is not a dictionary option")
            converted, report = convert_canopen_dictionary(text, args.capture_time_us)
        elif args.protocol in ("nmea2000-weather", "nmea2000-heading"):
            if args.actuator:
                raise ValueError("actuator layout is not a weather-report option")
            converted, report = convert_nmea2000_weather(text, heading=args.protocol == "nmea2000-heading")
        elif args.protocol in ("j1939-dm-single", "j1939-dm-bam"):
            if args.actuator:
                raise ValueError("actuator layout is not a diagnostic option")
            converted, report = convert_j1939(text, bam=args.protocol == "j1939-dm-bam")
        elif args.protocol.startswith("canopen-"):
            if args.actuator:
                raise ValueError("actuator layout is not a CANopen heartbeat option")
            converted, report = convert_canopen(text, include_emergency=args.protocol == "canopen-default-status")
        else:
            converted, report = convert(text, actuator_layout=parse_layout(args.actuator))
        args.new_output_directory.mkdir()  # Preserve existing inputs and evidence.
        (args.new_output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.new_output_directory / "accounting.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, ImportError):
        parser.exit(2, "CAN capture conversion failed; check format, dependency and transfer integrity\n")


if __name__ == "__main__":
    main()
