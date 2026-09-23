#!/usr/bin/env python3
"""Passive finite IMC scalar sensor frames -> existing CSV Observation reader."""
import argparse
import collections
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
# 259 GroundVelocity is laid out the other way round from 256/257: `validity` (uint8_t, prefix
# "VAL") comes FIRST and there is no device time, so the payload is 1 + 3x8 = 25 bytes. Its three
# bits are the dictionary's own `VEL_X`/`VEL_Y`/`VEL_Z`, and the message states the velocity is
# "relative to the ground" while saying nothing at all about an axis frame.
# There is no BodyVelocity message in this dictionary, nor in the 5.4.11 one; 260 WaterVelocity has
# an identical layout but a different stated reference, so it is not selected by shape.
VELOCITY_BITS = (("x", 0x01), ("y", 0x02), ("z", 0x04))
VELOCITIES = {259: ("reported_ground_velocity_", "ground_velocity_",
                    "REPORTED_RELATIVE_TO_GROUND_AXIS_FRAME_UNSPECIFIED_BY_MESSAGE_SCHEMA")}
VELOCITY_BASIS = ("velocity_validity_mask_reported", "velocity_unknown_validity_bits",
                  "velocity_reference_basis")


def velocity_columns(message):
    """The per-axis value and disposition column names of one selected velocity message."""
    values, dispositions, _ = VELOCITIES[message]
    return ([values + axis + "_m_s" for axis, _ in VELOCITY_BITS]
            + [dispositions + axis + "_disposition" for axis, _ in VELOCITY_BITS])
NEPTUS_SCHEMA = "neptus-2019.10.1-sensors"
# IMC 5.4.11, the dictionary bundled as `xml/IMC.xml` in `lib/libimc.jar` of the pinned Neptus tree,
# declares these ids with the same type, unit, wire order and - for the two vectors - the same absence of
# a frame as the current dictionary, so they carry the same meaning under this selector. Deliberately
# excluded, from that same file: 270 Salinity, whose field has no `unit` attribute at all (and no `PSU`
# entry exists in its `<units>` list), and 364 Power, which that dictionary does not declare.
# 259 joins them on the same evidence standard: in the 5.4.11 file its whole definition is
# byte-identical to the current one apart from the added non-wire attribute `category="Sensors"`.
NEPTUS_SENSORS = frozenset((251, 252, 256, 257, 259, 263, 264, 265, 267, 268))


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
                 field for fields in VECTORS.values() for field in fields] + [
                 field for message in VELOCITIES for field in velocity_columns(message)
             ] + list(VELOCITY_BASIS)
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
        if (message not in SENSORS and message not in VECTORS and message not in VELOCITIES) or (
                schema == NEPTUS_SCHEMA and message not in NEPTUS_SENSORS):
            retained = {"offset": offset, "message_id": message, "frame_hex": frame.hex()}
            report_size += len(json.dumps(retained))
            if report_size > LIMIT:
                raise ValueError("unsupported report exceeds bound")
            report["unsupported_frames"].append(retained)
        else:
            missing, status = False, None
            if message in VELOCITIES:
                values, dispositions, reference = VELOCITIES[message]
                if size != 25:
                    raise ValueError("selected IMC velocity size mismatch")
                validity, *axes = struct.unpack(order + "Bddd", frame[20:-2])
                quantities = {"velocity_validity_mask_reported": validity,
                              # Bits outside the three the dictionary defines are kept as reported
                              # and can never make a component valid.
                              "velocity_unknown_validity_bits": validity & ~0x07,
                              "velocity_reference_basis": reference}
                for (axis, bit), value in zip(VELOCITY_BITS, axes):
                    valid = bool(validity & bit)
                    if valid and not math.isfinite(value):
                        raise ValueError("nonfinite IMC velocity component reported valid")
                    # An invalid component is withheld, never zero-filled; a reported zero whose
                    # bit is set stays zero.
                    quantities[values + axis + "_m_s"] = value if valid else None
                    quantities[dispositions + axis + "_disposition"] = (
                        "REPORTED_VALID" if valid else "WITHHELD_COMPONENT_NOT_REPORTED_VALID")
                status = "REPORTED" if validity & 0x07 else "ONLY_WITHHELD_COMPONENTS"
            elif message in VECTORS:
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
                "measurement_status": status or ("UNAVAILABLE_SOURCE_SENTINEL" if missing else "REPORTED"),
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


def _typed_alog_records(data, variable, publisher, report, timestamp_decimals,
                        *, declared_start=None):
    """The one typed-alog scan every selector uses: headers, bounds, four fields, selection.

    Factored out rather than copied so the selectors can never drift on what a pLogger record is.
    It yields only the selected records' value token; what that token may contain is each
    selector's own business.

    `declared_start` serves the split `.klog` case. `SplitHandler` writes each original record line
    unchanged but skips every `%` line, so the type header and `LOGSTART` are absent from the split
    file by construction. Rather than fabricate either, the caller states the log start it read
    from the split's own `summary.klog`, and the type marker requirement is lifted for that input
    only -- the caller then has to declare the value's type and unit explicitly, because the file no
    longer says them.
    """
    marked, start, begun = declared_start is not None, declared_start, False
    report["declared_logstart_text"] = declared_start
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            report["blank_lines"] += 1
            continue
        if line.startswith("%"):
            report["comment_lines"] += 1
            if declared_start is not None:
                # A split record file carries no `%` lines at all, so one here means the caller
                # pointed at something other than what was declared.
                raise ValueError("declared split records carry no header lines")
            if line == "%% DATATYPE MARKING ON":
                if marked or begun: raise ValueError("duplicate or late type header")
                marked = True
            elif line.startswith("%% LOGSTART"):
                if start is not None or begun: raise ValueError("duplicate or late log start")
                start = line[len("%% LOGSTART"):].strip()
                if len(start) > 128 or not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", start) or not math.isfinite(float(start)):
                    raise ValueError("finite declared log start required")
                report["declared_logstart_text"] = start
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
        yield number, micros, parts[3].strip()


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
    for number, micros, token in _typed_alog_records(data, variable, publisher, report,
                                                     timestamp_decimals):
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
    return output.getvalue(), report


# The two variables pMarineViewer subscribes to by default when nothing is configured
# (PMV_MOOSApp.cpp:1034-1035). A deployment may configure other names; that is a named remaining
# difference, not something guessed at here.
NODE_REPORT_FORMAT = "moos-ivp-node-report-f56c"
# The alogview split of the same report. `SplitHandler` writes each original record line
# unchanged but skips every `%` line, so the type header and LOGSTART are absent by
# construction. Neither is fabricated: the caller declares the start it read from the
# split's own summary file, and the value type and units are declared too.
NODE_SPLIT_FORMAT = "moos-ivp-split-klog-f56c"
NODE_REPORT_VARIABLES = ("NODE_REPORT", "NODE_REPORT_LOCAL")
# The aliases the pinned parser itself folds together (NodeRecordUtils.cpp:154-188).
NODE_REPORT_ALIASES = {"SPEED": "SPD", "HEADING": "HDG", "DEPTH": "DEP", "ALTITUDE": "ALT",
                       "UTC_TIME": "TIME", "LEN": "LENGTH"}
NODE_REPORT_STRINGS = ("NAME", "TYPE", "MODE", "GROUP")
# Reported numerics. No unit appears in any column name: NodeRecord.h/.cpp/NodeRecordUtils.cpp
# declare none for any of them, so naming one here would be an invention.
NODE_REPORT_NUMBERS = ("X", "Y", "LAT", "LON", "SPD", "HDG", "DEP", "ALT")
NODE_REPORT_SELECTED = NODE_REPORT_STRINGS + NODE_REPORT_NUMBERS + ("TIME",)
# The record declares no unit, but a deployment legitimately can. A caller declaration is accepted
# for exactly these fields and exactly these units, and it is converted by the **shared**
# engineering-unit table -- the same one every other converter uses -- which is what turns a
# declared degree into the canonical radian and a declared metre into the canonical metre. Nothing
# is defaulted and nothing is inferred from a column name: with no declaration the canonical
# columns stay blank and say so, and the reported value is still carried unchanged beside them.
NODE_REPORT_DECLARABLE = {
    "SPD": ("node_speed_m_s", ("m/s", "km/h"), "speed_m_s"),
    "HDG": ("node_heading_rad", ("deg", "rad"), "angle_rad"),
    "X": ("node_x_m", ("m", "cm", "mm"), "distance_m"),
    "Y": ("node_y_m", ("m", "cm", "mm"), "distance_m"),
    "DEP": ("node_depth_m", ("m", "cm", "mm"), "distance_m")}
NODE_REPORT_CANONICAL = [NODE_REPORT_DECLARABLE[key][0] for key in ("SPD", "HDG", "X", "Y", "DEP")]
# The published pNodeReporter documentation, section 2.1, states these units for the report's own
# fields: "Speed is given in meters per second, heading is in degrees in the range [0, 360), depth
# is in meters, and the local x-y coordinates are also in meters." That is source-established
# meaning, not a caller's assertion, so it gets its own basis and its own fixed table. Altitude,
# latitude and longitude are NOT in that sentence and are therefore not documentable here.
NODE_REPORT_DOCUMENTED = {"SPD": "m/s", "HDG": "deg", "DEP": "m", "X": "m", "Y": "m"}
NODE_REPORT_DOCUMENTATION = "producer-documentation-stated"
# A declaration with no stated origin is not evidence, so the caller has to say where it came from.
# The first cites the producer's own documentation and is checked against it; the other two are
# statements about a deployment and are the caller's own.
NODE_REPORT_UNIT_BASES = (NODE_REPORT_DOCUMENTATION, "mission-configuration-declared",
                          "operator-declared")
# Either the caller's own capture moment, or the log's own record time: the declared LOGSTART epoch
# plus the record's relative offset. The second is a caller declaration about the logger's clock,
# never a verified one, and it is offered only because the alog record time genuinely exists.
NODE_REPORT_CLOCKS = ("caller-capture", "unix-epoch-logstart-relative")
NODE_REPORT_CAPTURE_CLOCK = ("CALLER_CAPTURE_DECLARATION_NOT_THE_LOGGERS_OWN_CLOCK_AND_NOT_UTC")
NODE_REPORT_LOG_CLOCK = ("DECLARED_UNIX_EPOCH_LOGSTART_PLUS_THIS_RECORDS_OWN_RELATIVE_OFFSET_THE_"
                         "EPOCH_IS_THE_CALLERS_DECLARATION_ABOUT_THE_LOGGER_AND_IS_NOT_VERIFIED_"
                         "HERE_NO_TIME_SOURCE_IS_AUTHENTICATED_AND_NO_SKEW_OR_WARP_IS_CORRECTED")
NODE_REPORT_BASIS = (
    "THE_RECORDS_OWN_SOURCE_FILES_ANNOTATE_NO_UNIT_BUT_THE_PRODUCERS_PUBLISHED_DOCUMENTATION_"
    "STATES_METRES_PER_SECOND_FOR_SPEED_DEGREES_IN_ZERO_TO_360_FOR_HEADING_AND_METRES_FOR_DEPTH_"
    "AND_THE_LOCAL_X_Y_SO_A_UNIT_HERE_IS_EITHER_THAT_DOCUMENTED_MEANING_OR_AN_EXPLICIT_CALLER_"
    "DECLARATION_AND_NEVER_A_DEFAULT;"
    "ALTITUDE_LATITUDE_AND_LONGITUDE_ARE_NOT_IN_THAT_DOCUMENTED_SENTENCE_AND_STAY_UNQUALIFIED;"
    "LOCAL_X_Y_FRAME_ORIGIN_IS_NOT_CARRIED_IN_THE_RECORD_SO_THE_FRAME_STAYS_UNRESOLVED_BY_IT;"
    "REPORTED_TIME_IS_DOCUMENTED_AS_UTC_FROM_THE_MOOSDB_HOSTS_SYSTEM_CLOCK_WHICH_IS_A_REPORTED_"
    "CLAIM_NOT_A_VERIFIED_OR_AUTHENTICATED_CLOCK_AND_IS_NEVER_THE_ROW_CLOCK;"
    "YAW_EXCLUDED_THE_PINNED_EMITTER_WRITES_RADIANS_FROM_HEADING_WHILE_ITS_OWN_EXAMPLE_SHOWS_"
    "DEGREES_EQUAL_TO_HDG")
NODE_REPORT_FIELDS = (["record_time_us", "record_clock_basis", "source_line", "source_variable",
    "source_publisher", "logger_relative_time_us", "source_sha256", "node_report_form"]
    + [f"node_{key.lower()}_hex" for key in NODE_REPORT_STRINGS]
    + [f"node_reported_{key.lower()}" for key in NODE_REPORT_NUMBERS]
    + NODE_REPORT_CANONICAL
    + ["node_unit_declaration", "node_reported_time_text", "node_absent_fields",
       "node_unselected_key_count", "node_report_hex", "node_declaration_basis"])


def _node_report_entries(composite):
    """Split on the separator the pinned parser uses, with its own brace protection.

    `parseStringZ(s, ',', "{")` protects commas inside braces, so a braced value such as a
    trajectory does not break the record into keyless fragments.
    """
    parts, depth, current = [], 0, []
    for character in composite:
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(depth - 1, 0)
        if character == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def node_report_values(composite):
    """The selected fields of one comma-separated node report, or a refusal naming what is wrong.

    Two deliberate divergences from the pinned parser, both so that nothing disappears quietly:
    it applies repeated keys in order so the last would win, and its `else if(isNumber(value))`
    has no else, so a non-numeric value for a numeric key is silently ignored and the field stays
    unset. Both are refused here instead.
    """
    if composite.startswith("{"):
        raise ValueError("braced JSON node report form is not qualified by this selector")
    if not composite or len(composite) > 8192:
        raise ValueError("bounded node report composite required")
    selected, unselected, seen = {}, 0, set()
    for entry in _node_report_entries(composite):
        if not entry.strip() or "=" not in entry:
            raise ValueError("node report entry without a key")
        left, _, raw = entry.partition("=")
        key = left.strip().upper()
        key = NODE_REPORT_ALIASES.get(key, key)
        raw = raw.strip()
        if not key:
            raise ValueError("node report entry without a key")
        if key in seen:
            raise ValueError("repeated node report key")
        seen.add(key)
        if key in NODE_REPORT_STRINGS:
            if len(raw) > 256:
                raise ValueError("node report text exceeds bound")
            selected[key] = raw
        elif key in NODE_REPORT_NUMBERS or key == "TIME":
            if (len(raw) > 64 or not re.fullmatch(
                    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", raw)
                    or not math.isfinite(float(raw))):
                raise ValueError("finite reported node report number required")
            selected[key] = raw
        else:
            unselected += 1
    if not selected.get("NAME"):
        # This selector's own boundary: a row with no reported identity describes no platform.
        raise ValueError("node report without a reported name")
    return selected, unselected


def convert_node_report(data, variable, publisher, capture_time_us, *, timestamp_decimals=5,
                        declared_units=None, unit_basis=None, record_clock="caller-capture",
                        declared_start=None):
    """Saved NODE_REPORT composite -> selected reported fields, plus declared canonical meaning."""
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise ValueError("bounded alog bytes required")
    if type(timestamp_decimals) is not int or timestamp_decimals not in (3, 5):
        raise ValueError("qualified logger timestamp precision required")
    if (not isinstance(publisher, str) or not re.fullmatch(r"[!-~]{1,256}", publisher)
            or variable not in NODE_REPORT_VARIABLES
            or type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63):
        raise ValueError("selected node report variable, exact publisher and capture time required")
    if record_clock not in NODE_REPORT_CLOCKS:
        raise ValueError("qualified record clock required")
    declared = dict(declared_units or {})
    if unit_basis == NODE_REPORT_DOCUMENTATION:
        # Citing the producer's documentation means taking what it actually says. An empty
        # declaration adopts the documented units; a stated one must match them, so the citation
        # can never be used to carry a unit that sentence does not state.
        for key, unit in declared.items():
            if NODE_REPORT_DOCUMENTED.get(key) != unit:
                raise ValueError("the cited documentation does not state this unit for this field")
        declared = dict(NODE_REPORT_DOCUMENTED) if not declared else declared
    elif declared:
        # A declaration with no stated origin is a bare assertion, so it is refused.
        if unit_basis not in NODE_REPORT_UNIT_BASES:
            raise ValueError("a node report unit declaration requires its stated basis")
    elif unit_basis is not None:
        raise ValueError("node report unit basis without a declaration")
    for key, unit in declared.items():
        if key not in NODE_REPORT_DECLARABLE or unit not in NODE_REPORT_DECLARABLE[key][1]:
            raise ValueError("unqualified node report unit declaration")
    if declared_start is not None and not re.fullmatch(
            r"[+-]?[0-9]{1,15}(?:\.[0-9]{1,6})?", declared_start):
        # Refused rather than rounded: a start the caller cannot state exactly in microseconds
        # cannot become a record time without inventing precision.
        raise ValueError("declared split log start must be exact to the microsecond")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(NODE_REPORT_FIELDS)
    report = dict(source_records=0, selected_records=0, unselected_records=0,
        comment_lines=0, blank_lines=0, source_sha256=hashlib.sha256(data).hexdigest(),
        source_retention="unchanged source.alog retained with CSV/report",
        clock="CALLER_CAPTURE; logger_relative_time_us is not UTC or device uptime",
        node_report_form="comma_separated_key_value_the_braced_JSON_form_is_refused_by_name",
        node_declaration_basis=NODE_REPORT_BASIS,
        excluded_keys="YAW HDG_OG COG SPD_OG INDEX LENGTH TRANSPARENCY TRAJECTORY ALLSTOP COLOR "
                      "VSOURCE LOAD_WARNING THRUST_MODE_REVERSE retained in node_report_hex",
        timestamp_decimals=timestamp_decimals,
        record_clock=record_clock,
        unit_declaration=(" ".join(f"{key}={declared[key]}" for key in sorted(declared))
                          + (f" basis={unit_basis}" if declared else "")) or "NONE_DECLARED",
        header_basis=("DECLARED_SPLIT_RECORDS_WITHOUT_THE_WRITERS_TYPE_HEADER" if declared_start
                      else "TYPED_LOGGER_HEADER_READ_FROM_THE_FILE"),
        evidence="fixed node report code/spec, not a vessel or a viewer session")
    stated = report["unit_declaration"]
    marked = declared_start is None
    for number, micros, token in _typed_alog_records(data, variable, publisher, report,
                                                     timestamp_decimals,
                                                     declared_start=declared_start):
        if marked and not token.startswith("S:"):
            raise ValueError("selected node report is not marked as a string")
        elif not marked and token.startswith(("S:", "D:")):
            # A split record file carries the original line unchanged, and the writer only adds a
            # marker when marking was on. A marker here means the caller declared the wrong input.
            raise ValueError("declared unmarked split record carries a type marker")
        composite = token[2:].strip() if marked else token
        selected, unselected = node_report_values(composite)
        absent = [key for key in NODE_REPORT_SELECTED if key not in selected]
        if record_clock == "caller-capture":
            moment, clock = capture_time_us, NODE_REPORT_CAPTURE_CLOCK
        else:
            start = report["declared_logstart_text"]
            if start is None or not re.fullmatch(r"[+-]?[0-9]{1,15}(?:\.[0-9]{1,6})?", start):
                raise ValueError("record clock needs a log start exact to the microsecond")
            moment = int(Decimal(start) * 1000000) + micros
            if not 0 <= moment < 2**63:
                raise ValueError("record time outside common range")
            clock = NODE_REPORT_LOG_CLOCK
        canonical = []
        for key in ("SPD", "HDG", "X", "Y", "DEP"):
            unit = declared.get(key)
            if unit is None or key not in selected:
                canonical.append("")
                continue
            field, value = normalize(float(selected[key]), unit)
            if field != NODE_REPORT_DECLARABLE[key][2]:
                raise ValueError("declared unit does not produce this field's canonical quantity")
            canonical.append(value)
        writer.writerow([moment, clock, number, variable, publisher, micros,
            report["source_sha256"], "CSP"]
            + ["" if key not in selected else "hex:" + selected[key].encode("utf-8").hex()
               for key in NODE_REPORT_STRINGS]
            + [selected.get(key, "") for key in NODE_REPORT_NUMBERS]
            + canonical
            + [stated, selected.get("TIME", ""), " ".join(absent), unselected,
               "hex:" + composite.encode("utf-8").hex(), NODE_REPORT_BASIS])
        report["selected_records"] += 1
        if output.tell() > OUTPUT_LIMIT: raise ValueError("CSV exceeds bound")
    if not report["selected_records"]: raise ValueError("no selected node report records")
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


SavedDatagram = collections.namedtuple(
    "SavedDatagram",
    "index capture_time_us payload udp_checksum frame "
    "source_address source_port destination_address destination_port")


def read_pcap_records(data):
    """Validate the classic PCAP container; return (linktype, capture microseconds per record).

    dpkt's reader yields short packet bodies without checking caplen, so bounds and exact integer
    timestamps are validated here before its packet iterator is used.
    """
    magic = {b"\xd4\xc3\xb2\xa1": ("<", 1), b"\xa1\xb2\xc3\xd4": (">", 1),
             b"\x4d\x3c\xb2\xa1": ("<", 1000), b"\xa1\xb2\x3c\x4d": (">", 1000)}
    if not 24 <= len(data) <= LIMIT or data[:4] not in magic:
        raise ValueError("bounded classic PCAP required")
    order, divisor = magic[data[:4]]
    _, major, minor, _, _, snaplen, linktype = struct.unpack(order + "IHHIIII", data[:24])
    if (major, minor) != (2, 4) or linktype not in (1, 105) or not 0 < snaplen <= LIMIT:
        raise ValueError("PCAP2.4 Ethernet or plain IEEE802.11 layout required")
    entries, offset = [], 24
    while offset < len(data):
        if len(data) - offset < 16:
            raise ValueError("truncated PCAP record header")
        sec, frac, captured, original = struct.unpack_from(order + "IIII", data, offset)
        if frac >= 1000000 * divisor or captured > snaplen or captured != original or offset + 16 + captured > len(data):
            raise ValueError("truncated capture or invalid packet timestamp/length")
        entries.append(sec * 1000000 + frac // divisor)
        offset += 16 + captured
    return linktype, entries


def iter_udp_datagrams(data, destination_port, records=None):
    """Yield one [`SavedDatagram`] per saved record, in recording order.

    `payload` is None when the record is not selected (not IPv4, not UDP, another destination port);
    the caller counts those. Anything malformed in the selected path raises instead of being dropped:
    decreasing capture clock, invalid IPv4 length, fragmentation, invalid UDP length or checksum.
    Reported addresses and ports are what the recording says, not authenticated identities.
    """
    import dpkt
    if dpkt.__version__ != "1.9.8":
        raise ValueError("qualified dpkt1.9.8 required")
    if type(destination_port) is not int or not 1 <= destination_port <= 65535:
        raise ValueError("explicit UDP destination port required")
    linktype, entries = records if records is not None else read_pcap_records(data)
    previous = None
    for index, (_, packet) in enumerate(dpkt.pcap.Reader(io.BytesIO(data))):
        capture = entries[index]
        if previous is not None and capture < previous:
            raise ValueError("decreasing packet clock")
        previous = capture
        frame = bytes(packet)
        unselected = SavedDatagram(index, capture, None, None, frame, None, None, None, None)
        network = packet_ipv4(packet, linktype)
        if network is None:
            yield unselected
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
            yield unselected
            continue
        if ip.offset or ip.mf:
            raise ValueError("fragmented UDP is outside selected path")
        raw_udp = network[ip.hl * 4:ip.len]
        try:
            udp = dpkt.udp.UDP(raw_udp)
        except dpkt.UnpackError as exc:
            raise ValueError("invalid saved UDP packet") from exc
        if udp.dport != destination_port:
            yield unselected
            continue
        if udp.ulen != len(raw_udp) or udp.ulen < 8 or dpkt.in_cksum(network[:ip.hl * 4]):
            raise ValueError("invalid selected IP/UDP length or checksum")
        if udp.sum and dpkt.in_cksum(ip.src + ip.dst + struct.pack(">BBH", 0, 17, udp.ulen) + raw_udp):
            raise ValueError("invalid selected UDP checksum")
        address = lambda raw: ".".join(str(byte) for byte in raw)
        yield SavedDatagram(index, capture, bytes(udp.data),
                            "CHECKED" if udp.sum else "NOT_PROVIDED", frame,
                            address(ip.src), udp.sport, address(ip.dst), udp.dport)


SavedEcho = collections.namedtuple(
    "SavedEcho", "index capture_time_us kind identifier sequence payload "
    "source_address destination_address frame")
# How many distinct echo keys may be awaiting a reply at once. A key is an address pair plus
# identifier and sequence, so the space is far wider than the 16-bit sequence field and a capture
# with many hosts or identifiers can exceed this; such a capture is refused rather than held
# unbounded. It bounds simultaneously outstanding requests, not the capture length.
MAX_OUTSTANDING = 65536


def iter_icmp_echoes(data, records=None):
    """Saved complete IPv4 ICMP echo requests and replies, passively.

    The shared classic-PCAP framing and link extraction are reused exactly as the UDP path uses
    them; the IPv4 checks are re-applied here so that path keeps its own behaviour unchanged.
    """
    import dpkt
    if dpkt.__version__ != "1.9.8":
        raise ValueError("qualified dpkt1.9.8 required")
    linktype, entries = records if records is not None else read_pcap_records(data)
    previous = None
    for index, (_, packet) in enumerate(dpkt.pcap.Reader(io.BytesIO(data))):
        capture = entries[index]
        if previous is not None and capture < previous:
            raise ValueError("decreasing packet clock")
        previous = capture
        network = packet_ipv4(packet, linktype)
        unselected = SavedEcho(index, capture, None, None, None, None, None, None, packet)
        if network is None:
            yield unselected
            continue
        try:
            ip = dpkt.ip.IP(network)
        except dpkt.UnpackError as exc:
            raise ValueError("invalid saved IPv4 packet") from exc
        if ip.v != 4 or ip.hl < 5 or ip.len < ip.hl * 4 or ip.len > len(network):
            raise ValueError("invalid IPv4 length")
        if linktype == 105 and ip.len != len(network):
            raise ValueError("Wi-Fi trailing bytes or FCS not supported")
        if ip.p != 1:
            yield unselected
            continue
        if ip.offset or ip.mf:
            raise ValueError("fragmented ICMP is outside selected path")
        if dpkt.in_cksum(network[:ip.hl * 4]):
            raise ValueError("invalid selected IPv4 header checksum")
        message = network[ip.hl * 4:ip.len]
        if len(message) < 8:
            raise ValueError("truncated selected ICMP message")
        kind, code, _ = message[0], message[1], message[2:4]
        if kind not in (0, 8) or code:
            # Other ICMP types and codes are reported as unselected, never validated or dropped.
            yield unselected
            continue
        # RFC792: the sum runs from the Type field over header and data, the checksum field
        # taken as zero; an odd total length is padded with one zero octet for computation.
        if dpkt.in_cksum(message):
            raise ValueError("invalid selected ICMP echo checksum")
        identifier, sequence = struct.unpack_from(">HH", message, 4)
        address = lambda raw: ".".join(str(part) for part in raw)
        yield SavedEcho(index, capture, kind, identifier, sequence, message[8:],
                        address(ip.src), address(ip.dst), packet)


def convert_icmp_echo(data):
    """Round-trip elapsed time between a saved echo request and its matching saved reply.

    The elapsed value is capture-location time and includes whatever the remote host took to
    answer. It is not one-way delay, not clock-synchronised and not a performance certification.
    """
    records = read_pcap_records(data)
    linktype, entries = records
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = dict(source_sha256=digest, packets=len(entries), selected_packets=0,
                  unselected_packets=0, decoded_records=0, paired_exchanges=0,
                  unmatched_requests=0, ambiguous_exchanges=0, replies_without_request=0,
                  returned_data_mismatches=0, clock="Unknown", linktype=linktype,
                  capture_resolution_us=1, unselected_status="NOT_DECODED_OR_VALIDATED")
    outstanding, poisoned, resolved, accounted, rows = {}, set(), set(), set(), []

    def emit(status, request, reply, rtt=None):
        echo = request if request is not None else reply
        for part in (request, reply):
            if part is not None:
                accounted.add(part.index)
        rows.append(dict(
            record_time_us=echo.capture_time_us, icmp_pair_status=status,
            icmp_rtt_us="" if rtt is None else rtt,
            icmp_rtt_s="" if rtt is None else rtt / 1000000,
            icmp_source_ip=echo.source_address, icmp_destination_ip=echo.destination_address,
            icmp_identifier=echo.identifier, icmp_sequence=echo.sequence,
            icmp_request_packet_index="" if request is None else request.index,
            icmp_reply_packet_index="" if reply is None else reply.index,
            icmp_request_capture_us="" if request is None else request.capture_time_us,
            icmp_reply_capture_us="" if reply is None else reply.capture_time_us,
            icmp_payload_bytes=len(echo.payload),
            icmp_payload_sha256="sha256:" + hashlib.sha256(echo.payload).hexdigest(),
            pcap_packet_index=echo.index, pcap_source_sha256=digest,
            pcap_clock_basis="CAPTURE_REPORTED_NOT_AUTHENTICATED",
            icmp_rtt_basis="CAPTURE_LOCATION_ELAPSED_INCLUDING_REMOTE_PROCESSING"))

    for echo in iter_icmp_echoes(data, records):
        if echo.kind is None:
            report["unselected_packets"] += 1
            continue
        report["selected_packets"] += 1
        if echo.kind == 8:
            key = (echo.source_address, echo.destination_address, echo.identifier, echo.sequence)
            if key in poisoned:
                # The key is already known ambiguous; nothing on it can be attributed again.
                report["ambiguous_exchanges"] += 1
                emit("AMBIGUOUS_DUPLICATE_KEY", echo, None)
                continue
            if key in resolved:
                # A key that already completed an exchange has come back. A later reply on it
                # cannot be told from a delayed earlier one, and the selected capture contract
                # does not permit reuse, so this is refused rather than silently attributed.
                raise ValueError("selected capture reuses an ICMP echo key")
            held = outstanding.get(key)
            if held is not None:
                # Two live requests share a key, so no reply on it could be attributed. RFC792
                # gives no rule for choosing, and nearest-in-time is a guess, not a match. The
                # key stays poisoned for the rest of the capture.
                poisoned.add(key)
                del outstanding[key]
                for request in held["requests"] + [echo]:
                    report["ambiguous_exchanges"] += 1
                    emit("AMBIGUOUS_DUPLICATE_KEY", request, None)
                continue
            if len(outstanding) >= MAX_OUTSTANDING:
                raise ValueError("outstanding ICMP echo requests exceed bound")
            outstanding[key] = {"requests": [echo]}
            continue
        key = (echo.destination_address, echo.source_address, echo.identifier, echo.sequence)
        if key in poisoned:
            # Keep the reply's own packet index rather than discarding it with the ambiguity.
            report["ambiguous_exchanges"] += 1
            emit("AMBIGUOUS_DUPLICATE_KEY", None, echo)
            continue
        held = outstanding.get(key)
        if held is None:
            report["replies_without_request"] += 1
            emit("REPLY_WITHOUT_REQUEST_IN_CAPTURE", None, echo)
            continue
        request = held["requests"][0]
        del outstanding[key]
        if echo.payload != request.payload:
            # RFC792 requires the data back unchanged, so this is not that request's reply and
            # the key is no longer trustworthy for attribution.
            poisoned.add(key)
            report["returned_data_mismatches"] += 1
            emit("RETURNED_DATA_MISMATCH", request, echo)
            continue
        resolved.add(key)
        report["paired_exchanges"] += 1
        emit("PAIRED", request, echo, echo.capture_time_us - request.capture_time_us)
    for held in outstanding.values():
        for request in held["requests"]:
            # The reply may simply be outside this capture; that is not proven loss.
            report["unmatched_requests"] += 1
            emit("NO_REPLY_IN_CAPTURE", request, None)
    report["accounted_selected_packets"] = len(accounted)
    if not rows:
        raise ValueError("no selected ICMP echo observations")
    rows.sort(key=lambda row: (row["record_time_us"], row["pcap_packet_index"]))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
        report["decoded_records"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("packet CSV exceeds bound")
    return output.getvalue(), report


def convert_pcap(data, destination_port, *, payload_format="imc"):
    """Offline complete IPv4/UDP datagrams through the selected existing decoder."""
    if payload_format not in ("imc", "nmea-wind"):
        raise ValueError("explicit supported packet payload required")
    records = read_pcap_records(data)
    linktype, entries = records
    output, writer = io.StringIO(newline=""), None
    report = dict(source_sha256="sha256:" + hashlib.sha256(data).hexdigest(), packets=len(entries),
        selected_packets=0, unselected_packets=0, decoded_records=0, selected_sources=[], clock="Unknown",
        linktype=linktype, unselected_status="NOT_DECODED_OR_VALIDATED")
    report_size = 0
    if payload_format == "nmea-wind":
        report["valid_wind_measurements"] = 0
    for saved_datagram in iter_udp_datagrams(data, destination_port, records):
        index, capture = saved_datagram.index, saved_datagram.capture_time_us
        datagram = saved_datagram.payload
        if datagram is None:
            report["unselected_packets"] += 1
            continue
        if payload_format == "nmea-wind":
            try:
                from scripts.convert_nmea_wind import convert as wind
            except ModuleNotFoundError:
                from convert_nmea_wind import convert as wind
            normalized, payload_report = wind(datagram.decode("ascii"), allow_no_selected=True)
            report["valid_wind_measurements"] += payload_report["valid_with_measurement"]
        else:
            normalized, payload_report = convert(datagram, allow_no_selected=True)
        saved = dict(packet_index=index, packet_hex=saved_datagram.frame.hex(),
            capture_time_us=capture, udp_checksum=saved_datagram.udp_checksum)
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
    parser.add_argument("--node-unit", action="append", metavar="KEY=UNIT",
        help="declare one selected node report field's unit, e.g. SPD=m/s or HDG=deg")
    parser.add_argument("--unit-basis", choices=NODE_REPORT_UNIT_BASES,
        help="where the node unit declaration came from; required whenever one is given")
    parser.add_argument("--record-clock", choices=NODE_REPORT_CLOCKS, default="caller-capture")
    parser.add_argument("--declared-logstart",
        help="the log start read from the split summary; the split selector only")
    parser.add_argument("--source-format", choices=("imc", NEPTUS_SCHEMA, "moos-typed-alog", "moos-ivp-typed-alog-f56c", NODE_REPORT_FORMAT, NODE_SPLIT_FORMAT, "moos-fixed-slog", "pcap-udp-imc", "pcap-udp-nmea-wind", "pcap-icmp-echo"), default="imc")
    parser.add_argument("--udp-destination-port", type=int)
    parser.add_argument("--variable")
    parser.add_argument("--publisher")
    parser.add_argument("--unit", choices=UNITS)
    parser.add_argument("--capture-time-us", type=int)
    args = parser.parse_args()
    try:
        packet_format = args.source_format in ("pcap-udp-imc", "pcap-udp-nmea-wind")
        node_report_format = args.source_format in (NODE_REPORT_FORMAT, NODE_SPLIT_FORMAT)
        split_format = args.source_format == NODE_SPLIT_FORMAT
        alog_format = node_report_format or args.source_format in ("moos-typed-alog", "moos-ivp-typed-alog-f56c")
        if not packet_format and args.udp_destination_port is not None:
            raise ValueError("UDP selection requires explicit PCAP format")
        # An option that only one branch consumes must not be accepted and then quietly not
        # applied: a caller who asked for a clock or a unit and did not get one would have no way
        # to tell. Supplying any of these outside the node report selectors is refused by name.
        # `--unit` keeps its existing behaviour and is checked by the branches that already own it.
        if not node_report_format and (args.node_unit or args.unit_basis is not None
                                       or args.declared_logstart is not None
                                       or args.record_clock != NODE_REPORT_CLOCKS[0]):
            raise ValueError("node report options require a node report source format")
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        data = args.input.read_bytes()
        if node_report_format:
            # The composite carries no single quantity, so a bare --unit would be a unit this
            # selector never applies; it is refused rather than accepted and ignored. Per-field
            # declarations go through --node-unit and must state their basis.
            if args.unit is not None:
                raise ValueError("node report selects no single caller unit")
            declared = {}
            for item in args.node_unit or []:
                key, sign, unit = item.partition("=")
                if not sign or not unit or key in declared:
                    raise ValueError("one KEY=UNIT node unit declaration per selected field")
                declared[key] = unit
            if (args.declared_logstart is not None) != split_format:
                raise ValueError("a declared log start belongs to the split selector only")
            output, report = convert_node_report(data, args.variable, args.publisher,
                args.capture_time_us, declared_units=declared, unit_basis=args.unit_basis,
                record_clock=args.record_clock, declared_start=args.declared_logstart)
        elif alog_format:
            output, report = convert_alog(data, args.variable, args.publisher, args.unit, args.capture_time_us,
                timestamp_decimals=5 if args.source_format == "moos-ivp-typed-alog-f56c" else 3)
        elif args.source_format == "moos-fixed-slog":
            if args.publisher is not None: raise ValueError("slog has no publisher identity")
            output, report = convert_slog(data, args.variable, args.unit, args.capture_time_us)
        else:
            if any(value is not None for value in (args.variable, args.publisher, args.unit, args.capture_time_us)):
                raise ValueError("alog options require explicit format")
            if args.source_format == "pcap-icmp-echo":
                if args.udp_destination_port is not None:
                    raise ValueError("ICMP echo selects no UDP port")
                output, report = convert_icmp_echo(data)
            else:
                output, report = convert_pcap(data, args.udp_destination_port,
                    payload_format="nmea-wind" if args.source_format == "pcap-udp-nmea-wind" else "imc") if packet_format else convert_input(
                        data, schema=NEPTUS_SCHEMA if args.source_format == NEPTUS_SCHEMA else None)
        args.output_directory.mkdir()
        if split_format:
            (args.output_directory / "source.klog").write_bytes(data)
        elif alog_format:
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
