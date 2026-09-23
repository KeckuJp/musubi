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
# Services live in their own 0..255 number space, resolved at the pin by (id, KIND_SERVICE), so this
# table is separate from TYPES and is consulted only when the service bit of the CAN ID is set.
# Response direction only: a request frame is never decoded, and none is ever generated or sent.
SERVICES = {1: "GetNodeInfo.Response"}
# PGN 127257 Attitude at the adopted canboat pin declares only "Attitude" and the field names Yaw,
# Pitch and Roll: no reference frame, no axis convention and no true/magnetic wording anywhere.
# canboat ENGINE_INSTANCE.yaml declares exactly these two values; any other code is retained as a
# reported number and named unknown rather than the lookup being extended.
ENGINE_INSTANCE = {0: "Single Engine or Dual Engine Port", 1: "Dual Engine Starboard"}
ENGINE_TRIM_SCALE_BASIS = ("PERCENT_AT_SCALE_ONE_ESTABLISHED_BY_THE_PINNED_DECODER_NOT_BY_A_DOCUMENTED_SCHEMA_DEFAULT_"
                           "THE_FIELD_TYPE_DECLARES_THE_UNIT_PERCENT_AND_DECLARES_NO_RESOLUTION_"
                           "AND_THE_DECODER_FIXES_ONE_POINT_ZERO_FOR_A_NUMBER_ROOTED_FIELD_THAT_"
                           "HAS_NONE_SO_THE_REPORTED_SIGNED_COUNT_IS_THE_PERCENT_VALUE")
ATTITUDE_REFERENCE_BASIS = ("REPORTED_YAW_PITCH_ROLL_RADIANS_THE_PINNED_DECLARATION_STATES_NO_"
                            "REFERENCE_FRAME_NO_AXIS_CONVENTION_AND_NO_TRUE_OR_MAGNETIC_REFERENCE")


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
    for section in parser.sections():
        if section.lower() != "deviceinfo":
            continue
        for option, value in parser.items(section):
            # DS306 4.6.3.4.1/5.3.3.1: a non-zero CompactPDO bitmask means PDO objects are *implicit* -
            # their mapping words are not in the file and their types/defaults come from CiA301, which
            # this repo does not pin. Refuse rather than emit a dictionary with those objects missing.
            if option.lower() == "compactpdo" and number(value, 255):
                raise ValueError("implicit CompactPDO definitions are not expanded")
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
        # Incomplete/other compact forms stay explicitly unexpanded. Only the fixed DS306 template is
        # interpreted, not implicit PDOs or nodes. DS306 V1.3 defines no separate compact RECORD form:
        # its 4.6.3.2 obligation table admits non-zero CompactSubObj on ARRAY** and RECORD** alike and
        # routes both to the one template of 4.6.3.4.2, where DataType/AccessType are mandatory and all
        # sub-objects share them. A RECORD whose sub-objects are not homogeneous therefore has no
        # compact form at all, and none is invented here.
        if kind not in (8, 9) or "DataType" not in values or "AccessType" not in values:
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
    "node_uptime_s", "node_reported_health", "node_health_code", "node_reported_mode",
    "node_mode_code", "node_sub_mode_reported", "node_vendor_status_code",
    "air_static_pressure_pa", "air_differential_pressure_pa", "air_temperature_k",
    "magnetic_body_x_t", "magnetic_body_y_t", "magnetic_body_z_t", "sensor_id",
    "esc_voltage_v", "esc_current_a", "esc_temperature_k", "esc_reported_error_count",
    "esc_index", "reported_rpm", "esc_demand_fraction", "rpm_reported_health", "rpm_unknown_flags",
    "actuator_id", "actuator_unit_basis", "actuator_position_m", "actuator_position_rad",
    "actuator_force_n", "actuator_torque_n_m", "actuator_linear_speed_m_s",
    "actuator_angular_speed_rad_s", "actuator_demand_fraction",
    # Self-reported version and identity declarations from the GetNodeInfo response. They describe
    # the node that sent them and are never an authenticated device, a commercial model or a
    # verified firmware image.
    "node_hardware_major_reported", "node_hardware_minor_reported", "node_hardware_unique_id_hex",
    "node_unique_id_disposition", "node_certificate_of_authenticity_hex",
    "node_software_major_reported", "node_software_minor_reported",
    "node_software_version_disposition", "node_software_optional_flags_reported",
    "node_software_vcs_commit_hex", "node_software_image_crc_hex", "node_name_reported_hex",
    "node_version_identity_basis"]
META = ["record_time_us", "first_frame_time_us", "source_node_id", "transfer_id",
        "data_type", "transfer_kind", "destination_node_id", "source_frames_hex"]
IDENTITY_BASIS = ("SELF_REPORTED_DECLARATION_BY_THE_SOURCE_NODE_NOT_AUTHENTICATED_DEVICE_MODEL_OR_"
                  "VERIFIED_FIRMWARE_IMAGE_BOUND_ONLY_BY_SOURCE_NODE_ID_WITHIN_THIS_CAPTURE")


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
                    or not 1 <= bits <= int(row["declared_bit_width"])):
                raise ValueError("unsupported scalar PDO mapping")
            # The pinned reference cannot read a float anywhere else, so this is checked before the
            # shared width rule below and reported as its own cause. Its non-byte-aligned branch
            # shift/masks the struct-unpacked value, which is a Python float for <f/<d and raises;
            # its aligned branch slices the *declared* width and ignores the mapped width, so a
            # short mapped float would be silently mis-read there. Neither is guessed at here.
            if datatype in (8, 17) and (total % 8 or bits != int(row["declared_bit_width"])):
                raise ValueError("float PDO mapping requires byte alignment and the full declared width")
            if not bit_fields and (bits % 8 or bits != int(row["declared_bit_width"])):
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
    # Every slot reports the bits it came from, not only the integer bit-field mode: a byte-aligned
    # float is the only float this reference can read, so without these columns a reported float
    # quantity would carry no source bit range, and a nonfinite one could not be located in the
    # retained frame. They also make the payload coverage checkable slot by slot.
    fields += ["mapped_bit_offset", "mapped_bit_width"]
    if booleans:
        fields += ["boolean_reported"]
    output = io.StringIO(newline=""); writer = csv.DictWriter(output, fields, lineterminator="\n"); writer.writeheader()
    report = dict(dictionary_report, dictionary_sha256=digest, frames=0, decoded_frames=0,
                  output_records=0, unsupported_frames=[], qualification="caller-configured TPDO and python Unit/Factor extensions; not device verified")
    # Stated for both modes: how many payload bits the mapping actually covers, and how many are
    # retained padding. In the byte-aligned float mode the padding is zero, and saying so is what
    # makes "no unmapped loss" checkable rather than assumed.
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


def node_info_meanings(payload):
    """The saved GetNodeInfo response: the adopted NodeStatus meanings plus what the node declares.

    Every version field is the sender's own statement about itself. The optional software fields are
    read only when their declared flag is set, and a zeroed unique id stays undefined, both exactly
    as the pinned definitions state.
    """
    software, hardware = payload.software_version, payload.hardware_version
    flags = int(software.optional_field_flags)
    unique = bytes(hardware.unique_id)
    values = dict(zip(FIELDS, meanings(341, payload.status)))
    values.update(
        node_hardware_major_reported=int(hardware.major),
        node_hardware_minor_reported=int(hardware.minor),
        # The adopted "hex:" prefix keeps these text in the common output: a bare "010203" or an
        # all-zero id would otherwise be read as the number 10203 or 0, losing leading zeros and
        # the distinction between an identifier and a quantity.
        node_hardware_unique_id_hex="hex:" + unique.hex(),
        # "All zeros is not a valid UID. If filled with zeros, assume that the value is undefined."
        node_unique_id_disposition="ALL_ZERO_UNDEFINED_PER_DEFINITION" if not any(unique)
        else "REPORTED_BY_NODE_NOT_VERIFIED",
        node_certificate_of_authenticity_hex="hex:" + bytes(hardware.certificate_of_authenticity).hex(),
        node_software_major_reported=int(software.major),
        node_software_minor_reported=int(software.minor),
        # "If both fields are set to zero, the version is considered unknown." The definitions say
        # nothing equivalent about the hardware numbers, so nothing is claimed for those.
        node_software_version_disposition="VERSION_REPORTED_UNKNOWN_BOTH_ZERO"
        if not software.major and not software.minor else "REPORTED_BY_NODE_NOT_VERIFIED",
        node_software_optional_flags_reported=flags,
        node_software_vcs_commit_hex=f"hex:{int(software.vcs_commit):08x}" if flags & 1 else None,
        node_software_image_crc_hex=f"hex:{int(software.image_crc):016x}" if flags & 2 else None,
        node_name_reported_hex="hex:" + bytes(payload.name).hex(),
        node_version_identity_basis=IDENTITY_BASIS)
    return [values[key] for key in FIELDS]


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
        # Every selected NodeStatus report reaches the row: the numeric code beside its name, and the
        # two codes the sender defines for itself. sub_mode and the vendor code have no standard
        # meaning, so they are carried as numbers and never named.
        result.update(node_uptime_s=int(payload.uptime_sec),
                      node_health_code=int(payload.health),
                      node_reported_health=("OK", "WARNING", "ERROR", "CRITICAL")[payload.health],
                      node_mode_code=int(payload.mode),
                      node_reported_mode={0: "OPERATIONAL", 1: "INITIALIZATION", 2: "MAINTENANCE",
                                          3: "SOFTWARE_UPDATE", 7: "OFFLINE"}.get(payload.mode, f"UNKNOWN_{payload.mode}"),
                      node_sub_mode_reported=int(payload.sub_mode),
                      node_vendor_status_code=int(payload.vendor_specific_status_code))
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
        # Bit 7 is the pin's service-not-message flag; the two number spaces never share a table.
        service = bool(frame_id & 0x80)
        if service:
            # id = service_id<<16 | request_not_response<<15 | dest<<8 | 1<<7 | source
            type_id, destination = (frame_id >> 16) & 0xff, (frame_id >> 8) & 0x7f
            # A request is accounted, never decoded as a response, and never generated here.
            selected = type_id in SERVICES and not frame_id & 0x8000 and destination
        else:
            type_id, destination = (frame_id >> 8) & 0xffff, None
            selected = type_id in TYPES and not (type_id == 1011 and not layout)
        if not frame_id & 0x7f or not selected:
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
            if service and (not transfer.service_not_message or transfer.request_not_response
                            or transfer.dest_node_id != destination):
                raise ValueError("service transfer direction or destination does not match its frames")
            values = (node_info_meanings(transfer.payload) if service
                      else meanings(type_id, transfer.payload, layout))
        except (dronecan.UAVCANException, ValueError, IndexError, TypeError, AttributeError) as exc:
            raise ValueError("invalid supported DroneCAN transfer") from exc
        source = "hex:" + json.dumps(originals, separators=(",", ":")).encode().hex()
        writer.writerow([timestamp, first_time, transfer.source_node_id, transfer.transfer_id,
                         (SERVICES if service else TYPES)[type_id],
                         "SERVICE_RESPONSE" if service else "MESSAGE",
                         "" if destination is None else destination,
                         source] + ["" if v is None else v for v in values])
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
    error_fields = ["canopen_error_code", "canopen_error_report_kind", "canopen_error_register",
                    "canopen_error_register_disposition", "canopen_manufacturer_data_hex"]
    # Every register bit gets a column, bit 6 included. CANopenNode v4.1 (ac214071) declares
    # CO_ERR_REG_RESERVED 0x40 "bit 6, reserved (always 0)" - a rule for the transmitter - and its own
    # receive path passes data[2] through untouched (301/CO_Emergency.c pFunctSignalRx), rejecting
    # nothing for that bit. A node that sets it has sent a well-formed 8-byte frame whose register value
    # is nonconformant, so the bit is retained and labelled as such, never discarded and never read as a
    # vendor fault.
    error_fields += ["canopen_error_" + name + "_reported" for name in error_names]
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
            writer.writerow([timestamp, identifier - 0x80, "", "", retained,
                code, "ERROR_RESET_REPORTED" if code == 0 else "ERROR_REPORTED", register,
                "RESERVED_BIT6_SET_NONCONFORMANT_TRANSMITTER_REPORT_RETAINED" if register & 0x40
                else "ALL_DEFINED_REGISTER_BITS",
                "hex:" + payload[3:].hex()]
                + [int(bool(register & (1 << bit))) for bit in range(8)])
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
              "observations": 0, "unsupported_dtcs": [], "zero_spn_reports": 0}
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
                # The J1939 "no active DTC" idiom. It is emitted and counted as a row, but it is not a
                # qualified fault report, so it is also counted separately instead of hiding inside
                # `observations`. The reporting PGN still says whether this came from DM1 or DM2.
                kind = "ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH"
                report["zero_spn_reports"] += 1
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


def convert_nmea2000_weather(text, *, heading=False, battery=False, attitude=False, engine=False):
    """Finite classic single-frame reports, not vessel or battery qualification.

    Battery adds PGN 127508 on the same framing, accounting and sentinel rules. Its declared
    quantities are what the device reported, never state of charge, chemistry, capacity or health.
    Attitude adds PGN 127257 the same way: its declaration states no reference frame at all, so the
    heading reference of 127250 is never carried over to it.
    Engine adds PGN 127488, which the pinned definition marks `type: Single`. Its quantities are
    reported engine state, never measured thrust, a command or engine health, and the group covers
    only this one PGN.
    """
    if sum((heading, battery, attitude, engine)) > 1:
        raise ValueError("one selected NMEA2000 report group at a time")
    quantities = ("wind_speed_m_s", "wind_angle_rad", "actual_temperature_k", "reported_set_temperature_k")
    if heading:
        quantities = ("heading_rad", "heading_deviation_rad", "heading_variation_rad", "turn_rate_rad_s")
    elif battery:
        quantities = ("battery_voltage_v", "battery_current_a", "battery_temperature_k")
    elif attitude:
        # Declared wire order is yaw, pitch, roll - not the usual roll/pitch/yaw recital.
        quantities = ("attitude_yaw_rad", "attitude_pitch_rad", "attitude_roll_rad")
    elif engine:
        quantities = ("engine_speed_rpm", "engine_boost_pressure_pa", "engine_tilt_trim_percent")
    fields = ["record_time_us", "source_node_id", "nmea2000_pgn", "source_frames_hex",
              "sid_code", "instance_code", "wind_reference_code", "wind_reference_reported",
              "temperature_source_code"]
    if heading:
        fields += ["heading_reference_code", "heading_reference_reported"]
    if attitude:
        fields += ["attitude_reference_basis", "reserved_field_code", "reserved_field_disposition"]
    if engine:
        fields += ["engine_instance_reported", "engine_trim_scale_basis",
                   "reserved_field_code", "reserved_field_disposition"]
    fields += [item for name in quantities for item in (name, name + "_status")]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, lineterminator="\n")
    writer.writeheader()
    report = dict(frames=0, decoded_frames=0, unsupported_frames=[], clock="Unknown")
    retained_size = 0
    for line, original, timestamp, identifier, payload in capture_rows(text, extended=True):
        report["frames"] += 1
        pgn = (identifier >> 8) & 0x3ffff
        selected = ((127250, 127251) if heading else (127508,) if battery
                    else (127257,) if attitude else (127488,) if engine else (130306, 130312))
        if pgn not in selected:
            entry = dict(line=line, source=original, reason="unselected PGN")
            retained_size += len(json.dumps(entry))
            if retained_size > 16 * 1024 * 1024:
                raise ValueError("unsupported report exceeds bound")
            report["unsupported_frames"].append(entry)
            continue
        if len(payload) != 8:
            raise ValueError("selected NMEA2000 PGN requires eight bytes")
        # 130306/130312/127250/127251 put SID first; 127508 declares it LAST, after temperature,
        # with Instance in byte 0 (canboat 127508-batteryStatus.yaml field order).
        row = dict(record_time_us=timestamp, source_node_id=identifier & 255, nmea2000_pgn=pgn,
                   # 127488 declares no SID field at all; byte 0 is its Instance lookup.
                   sid_code="" if pgn == 127488 else payload[7 if pgn == 127508 else 0],
                   source_frames_hex="hex:" + json.dumps([original], separators=(",", ":")).encode().hex())

        def scalar(name, offset, scale, *, size=2, signed=False):
            """Every selected quantity here is a fixed-point NUMBER, so all three codes apply.

            The pinned source reserves three top-of-range values for a field of eight bits or more:
            the maximum is data not available, maximum minus one is out of range, and maximum minus
            two is reserved. For a signed field these are the maximum *positive* values, not the
            all-ones encoding, so an all-ones byte stays ordinary data. Withholding only the first
            two turned a declared reserved code into a measurement, which is why the third is
            applied to every group here rather than to the engine group alone. Enumerated code
            fields are untouched: this rule is about numbers.
            """
            raw = int.from_bytes(payload[offset:offset + size], "little", signed=signed)
            missing = (1 << (size * 8 - int(signed))) - 1
            status = ("NOT_AVAILABLE" if raw == missing
                      else "OUT_OF_RANGE_REPORTED" if raw == missing - 1
                      else "RESERVED_RANGE_REPORTED" if raw == missing - 2
                      else "REPORTED")
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
        elif pgn == 127257:
            # ANGLE_FIX16 -> FIX16 -> SIGNED_FIXED_POINT_NUMBER: signed, resolution 0.0001, and
            # `physical: ANGLE` gives `unit: rad`. Signed means the unavailable and error codes are
            # 0x7fff/0x7ffe, which the shared helper already computes.
            scalar("attitude_yaw_rad", 1, .0001, signed=True)
            scalar("attitude_pitch_rad", 3, .0001, signed=True)
            scalar("attitude_roll_rad", 5, .0001, signed=True)
            # The declaration names no frame, no axis convention and no true/magnetic reference;
            # 127250's heading reference belongs to that PGN and is never carried over here.
            row.update(attitude_reference_basis=ATTITUDE_REFERENCE_BASIS,
                       reserved_field_code=payload[7],
                       # "All reserved bits shall be 1": a frame that breaks it is kept and named,
                       # never accepted as a qualified value.
                       reserved_field_disposition="ALL_BITS_SET_AS_DECLARED" if payload[7] == 0xff
                       else "NONCONFORMANT_RESERVED_BITS_RETAINED")
        elif pgn == 127488:
            # canboat 127488-engineParametersRapidUpdate.yaml: Instance LOOKUP 8, Speed
            # ROTATION_UFIX16_RPM (0.25, unit rpm), Boost Pressure PRESSURE_UFIX16_HPA (100.0,
            # no unit of its own so `physical: PRESSURE` gives Pa), Tilt/Trim PERCENTAGE_INT8
            # (signed, unit '%', and NO resolution declared), then 16 reserved bits.
            instance = payload[0]
            row.update(instance_code=instance, engine_instance_reported=
                       ENGINE_INSTANCE.get(instance, f"UNKNOWN_INSTANCE_CODE_{instance}"),
                       engine_trim_scale_basis=ENGINE_TRIM_SCALE_BASIS)
            scalar("engine_speed_rpm", 1, .25)
            scalar("engine_boost_pressure_pa", 3, 100.0)
            # Scale 1 is the decoder's, not the schema's: the field type declares unit '%' and no
            # resolution, and print.c fixes 1.0 when a NUMBER-rooted field has none, so the signed
            # count is the percent value. 0xff is -1, a value, not a sentinel; the sentinels are
            # the maximum positive codes 127/126/125.
            scalar("engine_tilt_trim_percent", 5, 1, size=1, signed=True)
            row.update(reserved_field_code=int.from_bytes(payload[6:8], "little"),
                       # "All reserved bits shall be 1": a frame that breaks it is kept and named.
                       reserved_field_disposition="ALL_BITS_SET_AS_DECLARED"
                       if payload[6:8] == b"\xff\xff" else "NONCONFORMANT_RESERVED_BITS_RETAINED")
        elif pgn == 127508:
            # Voltage and current are FIX16, i.e. *signed*, so their unavailable and error codes are
            # 0x7fff/0x7ffe rather than 0xffff/0xfffe; reading them unsigned would turn 0x7fff into
            # a 327.67 V reading. Temperature is UFIX16.
            row.update(instance_code=payload[0])
            scalar("battery_voltage_v", 1, .01, signed=True)
            scalar("battery_current_a", 3, .1, signed=True)
            scalar("battery_temperature_k", 5, .01)
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
    parser.add_argument("--protocol", choices=("dronecan", "canopen-heartbeat", "canopen-default-status", "canopen-dictionary", "canopen-pdo-python", "j1939-dm-single", "j1939-dm-bam", "nmea2000-weather", "nmea2000-heading", "nmea2000-battery", "nmea2000-attitude", "nmea2000-engine-rapid"), default="dronecan")
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
        # nmea2000-attitude was reachable as a choice but missing from this guard, so the CLI sent
        # it to the DroneCAN converter and it failed; nmea2000-engine-rapid is added with it.
        elif args.protocol in ("nmea2000-weather", "nmea2000-heading", "nmea2000-battery",
                               "nmea2000-attitude", "nmea2000-engine-rapid"):
            if args.actuator:
                raise ValueError("actuator layout is not a weather-report option")
            converted, report = convert_nmea2000_weather(
                text, heading=args.protocol == "nmea2000-heading",
                battery=args.protocol == "nmea2000-battery",
                attitude=args.protocol == "nmea2000-attitude",
                engine=args.protocol == "nmea2000-engine-rapid")
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
