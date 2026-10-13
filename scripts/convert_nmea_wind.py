#!/usr/bin/env python3
"""Passive selected NMEA reports; no clock inferred or live connection."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re

COLUMNS = ("unlocated_time_us", "source_line", "talker", "wind_reference",
           "reported_valid", "wind_angle_rad", "wind_speed_m_s", "source_sentence_hex")
AIS_COLUMNS = ("unlocated_time_us", "source_line", "ais_sentence_role", "ais_message_type",
    "ais_mmsi_reported", "ais_nav_status_code", "ais_longitude_deg", "ais_latitude_deg",
    "ais_position_disposition", "ais_speed_m_s", "ais_speed_lower_bound_m_s", "ais_speed_disposition",
    "ais_course_rad", "ais_true_heading_rad", "ais_second_code",
    # Class A type 5 static and voyage related data (ITU-R M.1371-5 TABLE 52, 424 bits, two slots).
    "ais_imo_reported", "ais_imo_disposition", "ais_ship_name_reported", "ais_ship_name_disposition",
    "ais_call_sign_reported", "ais_call_sign_disposition", "ais_ship_type_code",
    "ais_ship_type_disposition", "ais_epfd_code", "ais_epfd_disposition", "ais_dimension_bow_m",
    "ais_dimension_stern_m", "ais_dimension_port_m", "ais_dimension_starboard_m",
    "ais_dimension_disposition", "ais_draught_m", "ais_draught_lower_bound_m",
    "ais_draught_disposition", "ais_eta_month_code", "ais_eta_day_code", "ais_eta_hour_code",
    "ais_eta_minute_code", "ais_eta_disposition", "ais_destination_reported",
    "ais_destination_disposition", "ais_dte_reported", "ais_version_code", "ais_fragment_binding",
    "source_sentence_hex")
WATER_COLUMNS = ("unlocated_time_us", "source_line", "talker", "sentence_kind",
    "water_temperature_k", "depth_below_transducer_m", "transducer_offset_m",
    "measurement_disposition", "source_sentence_hex")
AIS_ROT_COLUMNS = ("ais_rot_code", "ais_rot_rad_s", "ais_rot_disposition", "ais_rot_direction")
AIS_B_COLUMNS = ("ais_class_b_unit", "ais_display_reported", "ais_dsc_reported", "ais_band_reported",
    "ais_message22_reported", "ais_assigned_reported", "ais_raim_reported", "ais_accuracy_flag_reported",
    "ais_reserved_1_raw", "ais_reserved_2_raw", "ais_radio_state_raw", "ais_repeat_raw", "ais_second_disposition")
AIS_B24_COLUMNS = ("ais_b24_part", "ais_vendor_manufacturer_reported", "ais_vendor_model_code",
    "ais_vendor_serial_code", "ais_vendor_disposition", "ais_b24_dimension_field_raw", "ais_b24_association")
B24_ASSOCIATION = "SEPARATE_PART_NOT_JOINED_SAME_MMSI_IS_NOT_PROOF_OF_ONE_STATION_OR_TRANSMISSION"


def ais_rot_quantities(line):
    """ITU-R M.1371-5 raw ROT: do not reuse pyais's rounded deg/min attribute."""
    from pyais.messages import NMEASentenceFactory
    raw = NMEASentenceFactory.produce(line.encode("ascii")).bv.get(42, 8)
    code = raw - 256 if raw >= 128 else raw
    direction = "UNKNOWN" if code == -128 else "ZERO_QUANTIZED" if code == 0 else "RIGHT" if code > 0 else "LEFT"
    if code == -128:
        return [code, None, "NO_INFORMATION", direction]
    if abs(code) == 127:
        return [code, None, "NO_TURN_INDICATOR", direction]
    if abs(code) == 126:
        return [code, None, "RANGE_END_MAGNITUDE_UNRESOLVED", direction]
    rate = math.radians(math.copysign((abs(code) / 4.733) ** 2, code)) / 60
    if not math.isfinite(rate):
        raise ValueError("finite inverse-quantized ROT required")
    return [code, rate, "INVERSE_QUANTIZED_ESTIMATE", direction]


def water_quantities(fields):
    try:
        from scripts.recorded_units import normalize
    except ModuleNotFoundError:
        from recorded_units import normalize
    def decimal(raw):
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", raw):
            raise ValueError("decimal water quantity required")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("finite water quantity required")
        return value
    if fields[0].endswith("MTW"):
        if len(fields) != 3 or fields[2] != "C":
            raise ValueError("qualified MTW Celsius layout required")
        value = normalize(decimal(fields[1]), "degC")[1] if fields[1] else None
        return [value, None, None, "REPORTED_NOT_CALIBRATED" if value is not None else "UNAVAILABLE"]
    if len(fields) != 4:
        raise ValueError("qualified DPT depth/offset/range layout required")
    depth = quantity(fields[1])
    offset = decimal(fields[2]) if fields[2] else None
    # Retained optional range is not used to clamp or validate a measured depth.
    quantity(fields[3])
    return [None, depth, offset, "REPORTED_BELOW_TRANSDUCER" if depth is not None else "UNAVAILABLE"]


def ais_quantities(line, fields, class_b=False):
    from pyais import decode
    if (len(fields) != 7 or fields[1:3] != ["1", "1"] or fields[4] not in ("A", "B")
            or not re.fullmatch(r"[0-9]?", fields[3]) or fields[6] != "0"
            or not re.fullmatch(r"[0-W`-w]{28}", fields[5])):
        raise ValueError("fixed AIS Class A report requires one complete 168-bit sentence" if not class_b
                         else "fixed AIS Class B18 requires one complete 168-bit sentence")
    data = decode(line.encode("ascii"))
    if data.msg_type not in ((18,) if class_b else (1, 2, 3)) or not 0 <= data.mmsi <= 999999999:
        raise ValueError("invalid selected AIS identity or type")
    lon, lat, speed, course, heading = data.lon, data.lat, data.speed, data.course, data.heading
    if ((lon != 181 and not -180 <= lon <= 180) or (lat != 91 and not -90 <= lat <= 90)
            or not 0 <= speed <= 102.3 or not 0 <= course <= 360
            or (heading != 511 and not 0 <= heading <= 359)):
        raise ValueError("reserved or invalid AIS reported quantity")
    missing_position = lon == 181 or lat == 91
    values = ["OWN_VESSEL_REPORTED" if fields[0] == "AIVDO" else "RECEIVED_REPORT",
        data.msg_type, data.mmsi, None if class_b else int(data.status), None if lon == 181 else lon, None if lat == 91 else lat,
        "INCOMPLETE_REPORTED_POSITION" if missing_position else "REPORTED_NOT_AUTHENTICATED",
        speed * 1852 / 3600 if speed < 102.2 else None,
        speed * 1852 / 3600 if speed == 102.2 else None,
        "UNAVAILABLE" if speed == 102.3 else "AT_LEAST" if speed == 102.2 else "REPORTED",
        None if course == 360 else math.radians(course),
        None if heading == 511 else math.radians(heading), data.second]
    if class_b:
        if data.cs and data.second > 60:
            raise ValueError("Class B CS does not qualify timestamp codes 61..63")
        second = "REPORTED_UTC_SECOND_NOT_ABSOLUTE_TIME" if data.second < 60 else (
            "UNAVAILABLE", "MANUAL_POSITION_INPUT_REPORTED", "ESTIMATED_POSITION_REPORTED",
            "POSITIONING_SYSTEM_INOPERATIVE_REPORTED")[data.second - 60]
        values += ["CS_REPORTED" if data.cs else "SOTDMA_REPORTED", int(data.display), int(data.dsc),
                   int(data.band), int(data.msg22), int(data.assigned), int(data.raim), int(data.accuracy),
                   data.reserved_1, data.reserved_2, data.radio, data.repeat, second]
    return values


def ais_static_quantities(first, second, first_fields, second_fields):
    """One caller-grouped pair of saved type 5 fragments; nothing is fetched and nothing reassembled
    across captures.

    pyais 3.2.2 binds fragments by their numbers alone: a reversed pair silently decodes as a
    different message type, and one fragment of message A combined with a fragment of message B
    decodes as a complete report. Every binding condition is therefore checked here, before the
    decoder is called, and an empty sequential id is refused rather than guessed at.
    """
    from pyais.messages import NMEASentenceFactory, AISSentence
    if len(first_fields) != 7 or len(second_fields) != 7:
        # Field count is checked before any positional access below.
        raise ValueError("saved AIS type 5 pair requires two matching adjacent fragments")
    talker, channel, sequence = first_fields[0], first_fields[4], first_fields[3]
    if (first_fields[1:3] != ["2", "1"] or second_fields[1:3] != ["2", "2"]
            or second_fields[0] != talker or second_fields[4] != channel
            or second_fields[3] != sequence or channel not in ("A", "B")
            or first[0] != "!" or second[0] != "!" or first_fields[6] != "0"
            or not re.fullmatch(r"[0-W`-w]{60}", first_fields[5])
            or not re.fullmatch(r"[0-W`-w]{11}", second_fields[5])
            or second_fields[6] != "2"):
        # TABLE 52 fixes the message at 424 bits: 60 + 11 six-bit characters with two fill bits on
        # the second fragment. No other shape is this message, and no padding is guessed as valid.
        raise ValueError("saved AIS type 5 pair requires two matching adjacent fragments")
    # The single-sentence path already qualifies this field as one NMEA digit; a pair may not carry
    # arbitrary text into the binding identity, and an empty id would let the decoder combine
    # anything, so the caller must declare it.
    if not re.fullmatch(r"[0-9]", sequence):
        raise ValueError("saved AIS type 5 pair requires an explicit shared sequential id")
    # Assembled through the decoder's own sentence factory so its bit vector is available: the
    # enum-coerced attributes lose the raw type-of-ship and fixing-device codes, and the length
    # check below is the only thing that excludes an over-long or wrongly padded assembly.
    parts = [NMEASentenceFactory.produce(line.encode("ascii")) for line in (first, second)]
    assembled = AISSentence.assemble_from_iterable(parts)
    if len(assembled.bv) != 424:
        raise ValueError("saved AIS type 5 report must assemble to exactly 424 bits")
    data = assembled.decode()
    if data.msg_type != 5 or not 0 <= data.mmsi <= 999999999:
        raise ValueError("invalid selected AIS identity or type")
    for name in ("imo", "callsign", "shipname", "ship_type", "to_bow", "to_stern", "to_port",
                 "to_starboard", "epfd", "month", "day", "hour", "minute", "draught",
                 "destination", "dte", "ais_version"):
        if getattr(data, name, None) is None:
            # The decoder leaves fields past the end of a short assembly as None instead of failing.
            raise ValueError("incomplete saved AIS type 5 report")
    imo = int(data.imo)
    imo_state = ("NOT_AVAILABLE" if imo == 0 else "RANGE_NOT_USED_BY_THE_REFERENCE" if imo <= 999999
                 else "VALID_IMO_NUMBER" if imo <= 9999999 else "OFFICIAL_FLAG_STATE_NUMBER")
    def text(value):
        value = str(value)
        return [value, "REPORTED" if value else "NOT_AVAILABLE_OR_BLANK"]
    # The decoder's enums map every code they do not name - raw 1 and all 100..255 for the type of
    # ship, 9..14 for the fixing device - onto their zero member, which would report a reserved code
    # as "not available". The raw fields are therefore read from the assembled bit vector at the
    # TABLE 52 offsets (type of ship: bits 232..239 after ID 6 + repeat 2 + MMSI 30 + version 2 +
    # IMO 30 + call sign 42 + name 120; fixing device: bits 270..273 after the 30-bit dimensions).
    # This is the pinned decoder's own accessor, not a second AIS decoder.
    ship_type_raw = assembled.bv.get(232, 8)
    epfd_raw = assembled.bv.get(270, 4)
    # The pinned table gives 1..99 as one range ("as defined in 3.3.2"), not an entry-by-entry
    # dictionary, so the label stays range-level: some codes inside it may be unassigned there.
    ship_type_state = ("NOT_AVAILABLE_OR_NO_SHIP" if ship_type_raw == 0 else
                       "REPORTED_CODE_IN_BASE_RANGE" if ship_type_raw <= 99 else
                       "RESERVED_FOR_REGIONAL_USE" if ship_type_raw <= 199 else
                       "RESERVED_FOR_FUTURE_USE")
    epfd_state = ("UNDEFINED_DEFAULT" if epfd_raw == 0 else
                  "REPORTED_DEFINED_CODE" if epfd_raw <= 8 or epfd_raw == 15 else
                  "NOT_USED_BY_THE_REFERENCE")
    bow, stern, port, starboard = (int(data.to_bow), int(data.to_stern),
                                  int(data.to_port), int(data.to_starboard))
    dimensions = [bow, stern, port, starboard,
                  "DEFAULT_ALL_ZERO_NOT_AVAILABLE" if not any((bow, stern, port, starboard))
                  else "REPORTED_REFERENCE_POINT_DIMENSIONS"]
    # TABLE 52: in 1/10 m, 255 = 25.5 m or greater, 0 = not available. The decoder already divides
    # by ten, so 25.5 is the "or greater" code and gets a lower bound, never an exact draught.
    draught = float(data.draught)
    if not 0 <= draught <= 25.5:
        raise ValueError("reserved or invalid AIS reported quantity")
    draught_values = ([None, None, "UNAVAILABLE"] if draught == 0 else
                      [None, draught, "AT_LEAST"] if draught == 25.5 else
                      [draught, None, "REPORTED"])
    month, day, hour, minute = int(data.month), int(data.day), int(data.hour), int(data.minute)
    if not (0 <= month <= 12 and 0 <= day <= 31 and 0 <= hour <= 24 and 0 <= minute <= 60):
        raise ValueError("reserved or invalid AIS reported quantity")
    unavailable = [month == 0, day == 0, hour == 24, minute == 60]
    # MMDDHHMM with no year: an estimate as reported, never an instant and never an arrival.
    eta_state = ("UNAVAILABLE" if all(unavailable) else "PARTIALLY_UNAVAILABLE" if any(unavailable)
                 else "REPORTED_COMPLETE_FIELDS")
    return ["OWN_VESSEL_REPORTED" if talker == "AIVDO" else "RECEIVED_REPORT", data.msg_type,
        data.mmsi, None, None, None, "STATIC_REPORT_CARRIES_NO_POSITION", None, None,
        "NOT_IN_THIS_MESSAGE", None, None, None,
        imo, imo_state, *text(data.shipname), *text(data.callsign), ship_type_raw, ship_type_state,
        epfd_raw, epfd_state, *dimensions, *draught_values, month, day, hour, minute, eta_state,
        *text(data.destination), "NOT_AVAILABLE" if data.dte else "AVAILABLE", int(data.ais_version)]


def ais_b24_quantities(line, fields):
    """One saved type 24 part. TABLE 78: Part A is exactly 160 bits (27 characters, 2 fill bits);
    TABLE 79: Part B is exactly 168 bits (28 characters, no fill).

    The pinned pyais 3.2.2 models Part A with an extra 8-bit spare, folds Part B's 4-bit type of
    electronic position fixing device into a 6-bit spare, and reads the 30-bit dimension field as a
    mothership MMSI for 98XXXYYYY identities, which TABLE 79 does not define. Every Part B field is
    therefore read from the decoder's own bit vector at the TABLE 79 offsets; no second decoder.
    """
    from pyais.messages import NMEASentenceFactory
    shapes = {"0": (27, "2"), "1": (28, "0")}
    if len(fields) != 7 or fields[1:3] != ["1", "1"] or fields[4] not in ("A", "B") \
            or not re.fullmatch(r"[0-9]?", fields[3]) or not re.fullmatch(r"[0-W`-w]+", fields[5]):
        raise ValueError("fixed AIS type 24 requires one complete single sentence")
    bv = NMEASentenceFactory.produce(line.encode("ascii")).bv
    if len(bv) < 40 or bv.get(0, 6) != 24:
        raise ValueError("invalid selected AIS identity or type")
    part = str(bv.get(38, 2))
    if part not in shapes or (len(fields[5]), fields[6]) != shapes[part]:
        raise ValueError("saved AIS type 24 part must be exactly 160 (A) or 168 (B) bits")
    mmsi = bv.get(8, 30)
    if not 0 <= mmsi <= 999999999:
        raise ValueError("invalid selected AIS identity or type")
    def text(start, chars):
        value = "".join(chr(code + 64 if code < 32 else code)
                        for code in (bv.get(start + 6 * i, 6) for i in range(chars)))
        value = value.split("@", 1)[0].rstrip(" ")
        return [value, "REPORTED" if value else "NOT_AVAILABLE_OR_BLANK"]
    absent = "NOT_IN_THIS_PART"
    position = [None, None, None, "STATIC_REPORT_CARRIES_NO_POSITION", None, None, "NOT_IN_THIS_MESSAGE",
                None, None, None]
    static = dict(imo=[None, "NOT_IN_THIS_MESSAGE"], name=[None, absent], call=[None, absent],
                  ship=[None, absent], epfd=[None, absent], dims=[None] * 4 + [absent])
    vendor = [None, None, None, absent, None]
    if part == "0":
        static["name"] = text(40, 20)
    else:
        ship = bv.get(40, 8)
        static["ship"] = [ship, "NOT_AVAILABLE_OR_NO_SHIP" if ship == 0 else
                          "REPORTED_CODE_IN_BASE_RANGE" if ship <= 99 else
                          "RESERVED_FOR_REGIONAL_USE" if ship <= 199 else "RESERVED_FOR_FUTURE_USE"]
        manufacturer = text(48, 3)[0]
        model, serial = bv.get(66, 4), bv.get(70, 20)
        vendor = [manufacturer, model, serial,
                  "NOT_AVAILABLE_DEFAULT" if bv.get(48, 42) == 0 else
                  "REPORTED_SERIAL_CODING_MANUFACTURER_DEFINED", None]
        static["call"] = text(90, 7)
        raw = bv.get(132, 30)
        bow, stern, port, starboard = bv.get(132, 9), bv.get(141, 9), bv.get(150, 6), bv.get(156, 6)
        static["dims"] = [bow, stern, port, starboard,
                          "DEFAULT_ALL_ZERO_NOT_AVAILABLE" if raw == 0 else "REPORTED_REFERENCE_POINT_DIMENSIONS"]
        vendor[4] = raw
        epfd = bv.get(162, 4)
        static["epfd"] = [epfd, "UNDEFINED_DEFAULT" if epfd == 0 else
                          "REPORTED_DEFINED_CODE" if epfd <= 8 or epfd == 15 else "NOT_USED_BY_THE_REFERENCE"]
    row = ["OWN_VESSEL_REPORTED" if fields[0] == "AIVDO" else "RECEIVED_REPORT", 24, mmsi, *position,
           *static["imo"], *static["name"], *static["call"], *static["ship"], *static["epfd"],
           *static["dims"], None, None, "NOT_IN_THIS_MESSAGE", None, None, None, None,
           "NOT_IN_THIS_MESSAGE", None, "NOT_IN_THIS_MESSAGE", None, None, None]
    b_fields = [None] * (len(AIS_B_COLUMNS) - 2) + [bv.get(6, 2), "NOT_IN_THIS_MESSAGE"]
    return row, b_fields, ["A" if part == "0" else "B", *vendor, B24_ASSOCIATION]


def quantity(raw, maximum=None):
    if raw == "":
        return None
    if raw != raw.strip():
        raise ValueError("whitespace in MWV quantity")
    try:
        value = float(raw)
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid MWV quantity") from exc
    if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        raise ValueError("MWV quantity outside range")
    return value


def convert_mixed(text, *, allow_no_selected=False, static_pairs=False, ais_rot=False):
    if len(text) > 16 * 1024 * 1024:
        raise ValueError("mixed NMEA input exceeds bound")
    rows, selected, reports = [], set(), {}
    for selector in ("wind", "ais-class-a", "water-dpt-mtw"):
        output, report = convert(text, selector, allow_no_selected=True, static_pairs=static_pairs,
                                 ais_rot=ais_rot and selector == "ais-class-a")
        reports[selector] = report
        for row in csv.DictReader(io.StringIO(output)):
            index = int(row["source_line"])
            if index in selected:
                raise ValueError("overlapping NMEA selector outputs")
            selected.add(index)
            row["selected_kind"] = selector
            rows.append(row)
    if not rows and not allow_no_selected:
        raise ValueError("no selected mixed NMEA records")
    columns = list(dict.fromkeys((*COLUMNS, *AIS_COLUMNS, *WATER_COLUMNS, "selected_kind")))
    if ais_rot:
        columns.extend(AIS_ROT_COLUMNS)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, columns, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda row: int(row["source_line"])):
        writer.writerow(row)
        if output.tell() > 128 * 1024 * 1024:
            raise ValueError("mixed NMEA output exceeds bound")
    # A type 5 pair emits one row but consumes two input lines, so the second fragment is counted as
    # consumed rather than left in the unsupported list, where it would read as unused.
    consumed = set(selected) | set(reports["ais-class-a"].get("consumed_lines", []))
    unsupported = [item for item in reports["wind"]["unsupported"] if item["line"] not in consumed]
    count = reports["wind"]["input_records"]
    if len(consumed) + len(unsupported) != count:
        raise ValueError("mixed NMEA record accounting mismatch")
    return output.getvalue(), dict(input_records=count, selected_records=len(selected),
        consumed_records=len(consumed),
        unsupported_records=len(unsupported), unsupported=unsupported, selector_reports=reports,
        clock="Unknown", time_basis="UNLOCATED_ZERO_NOT_EVENT_TIME",
        qualification="SELECTED_REPORTED_MEANINGS_NOT_SENSOR_FUSION")


def convert(text: str, kind="wind", *, allow_no_selected=False, static_pairs=False,
            ais_rot=False, class_b_static=False) -> tuple[str, dict]:
    """`static_pairs` is the caller's explicit opt-in to the adjacent type 5 pair contract.

    Adjacency plus a shared channel and sequential id does not prove two fragments came from one
    transmission, so no selector assembles a pair unless the caller asks for it by name.
    """
    if type(static_pairs) is not bool:
        raise ValueError("explicit static pair selection required")
    if type(class_b_static) is not bool or (class_b_static and kind != "ais-class-b18"):
        raise ValueError("Class B static reports require explicit Class B18 selection")
    if kind == "ais-class-b18" and static_pairs:
        raise ValueError("Class B18 does not select static fragment pairs")
    if type(ais_rot) is not bool or (ais_rot and kind not in ("ais-class-a", "marine-mixed")):
        raise ValueError("AIS ROT requires explicit AIS or mixed selection")
    if kind == "marine-mixed":
        return convert_mixed(text, allow_no_selected=allow_no_selected, static_pairs=static_pairs,
                             ais_rot=ais_rot)
    if kind not in ("wind", "ais-class-a", "ais-class-b18", "water-dpt-mtw"):
        raise ValueError("unknown NMEA selection")
    ais = kind in ("ais-class-a", "ais-class-b18")
    class_b = kind == "ais-class-b18"
    water = kind == "water-dpt-mtw"
    if (ais or water) and len(text) > 16 * 1024 * 1024:
        raise ValueError("AIS input exceeds bound")
    if ais:
        from importlib.metadata import version
        if version("pyais") != "3.2.2" or version("attrs") != "25.3.0":
            raise ValueError("qualified AIS decoder versions required")
    if not text.isascii() or any(ord(c) < 32 and c not in '\r\n' for c in text):
        raise ValueError("unsupported NMEA representation")
    lines = text.splitlines()
    if not lines or any(not line for line in lines):
        raise ValueError("empty capture or blank record")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(AIS_COLUMNS + (AIS_ROT_COLUMNS if ais_rot else ()) + (AIS_B_COLUMNS if class_b else ())
                    + (AIS_B24_COLUMNS if class_b_static else ()) if ais else WATER_COLUMNS if water else COLUMNS)
    report = dict(input_records=len(lines), mwv_records=0, valid_with_measurement=0,
                  invalid_status_records=0, unsupported_records=0, unsupported=[], blank_lines=0)
    if ais:
        report.update(ais_records=0, clock="Unknown", time_basis="UNLOCATED_ZERO_NOT_EVENT_TIME",
                      decoder="pyais-3.2.2", semantics="ITU-R-M.1371-5-Class-A-1-2-3-and-5",
                      ais_static_pairs=0, consumed_lines=[], reused_sequential_ids=[],
                      reused_sequential_id_pairs=0,
                      static_binding="ADJACENT_PAIR_DECLARED_BY_THE_CALLER_NOT_GLOBALLY_UNIQUE",
                      eta_basis="REPORTED_ESTIMATE_WITHOUT_A_YEAR_NEVER_AN_ARRIVAL_OR_OBSERVATION_TIME")
        if ais_rot:
            report["rot_basis"] = "RAW_SIGNED_CODE_INVERSE_QUANTIZED_ESTIMATE_NOT_PHYSICAL_RATE_END_CODES_WITHHELD"
        if class_b:
            report["semantics"] = "ITU-R-M.1371-5-Table70-Class-B18_ONLY_REPORTED_NOT_AUTHENTICATED"
            report["static_binding"] = "NOT_SELECTED"
        if class_b_static:
            report.update(semantics="ITU-R-M.1371-5-Table70-Class-B18-and-Tables78-79-type24_ONLY_REPORTED_NOT_AUTHENTICATED",
                          ais_b24_part_a=0, ais_b24_part_b=0, b24_association=B24_ASSOCIATION)
    pending = None

    def write_ais_row(row, retained, rot=None, b_fields=None, b24=None):
        if len(row) != len(AIS_COLUMNS) - 1:
            raise ValueError("AIS row width does not match the declared columns")
        extra = (rot if rot is not None else [None, None, "NOT_IN_THIS_MESSAGE", "UNKNOWN"]) if ais_rot else []
        if class_b:
            if b_fields is None or len(b_fields) != len(AIS_B_COLUMNS):
                raise ValueError("Class B fields required")
            extra += b_fields
        if class_b_static:
            extra += b24 if b24 is not None else [None, None, None, None, "NOT_IN_THIS_MESSAGE", None, None]
        writer.writerow([*row, "hex:" + retained.encode("ascii").hex(), *extra])
    if water:
        report.update(water_records=0, water_measurements=0, clock="Unknown",
                      time_basis="UNLOCATED_ZERO_NOT_EVENT_TIME", semantics="gpsd-e128-DPT-MTW-fields-not-offset-correction")
    for index, line in enumerate(lines, 1):
        match = re.fullmatch(r"[$!]([^*]+)\*([0-9A-Fa-f]{2})", line)
        if match is None:
            raise ValueError(f"invalid NMEA framing at line {index}")
        payload, checksum = match.groups()
        expected = 0
        for value in payload.encode("ascii"):
            expected ^= value
        if expected != int(checksum, 16):
            raise ValueError(f"NMEA checksum mismatch at line {index}")
        fields = payload.split(",")
        identifier = fields[0]
        if not re.fullmatch(r"[A-Z0-9]{3,12}", identifier):
            raise ValueError("invalid NMEA identifier")
        if water:
            if identifier.endswith(("DPT", "MTW")):
                if line[0] != "$" or len(identifier) != 5:
                    raise ValueError("qualified water sentence identifier required")
                values = water_quantities(fields)
                writer.writerow([0, index, identifier[:2], identifier[2:], *values,
                                 "hex:" + line.encode("ascii").hex()])
                report["water_records"] += 1
                report["water_measurements"] += int(values[0] is not None or values[1] is not None)
                if stream.tell() > 128 * 1024 * 1024:
                    raise ValueError("water observations exceed bound")
            else:
                report["unsupported"].append(dict(line=index, raw=line))
                report["unsupported_records"] += 1
            continue
        if ais:
            multipart = identifier in ("AIVDM", "AIVDO") and len(fields) > 5 and fields[1:2] != ["1"]
            # A continuation fragment carries arbitrary payload characters, so it must never be
            # classified by its own first character the way a single sentence is.
            single = (identifier in ("AIVDM", "AIVDO") and len(fields) > 5
                      and fields[1:2] == ["1"] and fields[5][:1] in (("B",) if class_b else ("1", "2", "3")))
            b24 = (class_b_static and identifier in ("AIVDM", "AIVDO") and len(fields) > 5
                   and fields[1:2] == ["1"] and fields[5][:1] == "H")
            # Without the opt-in nothing is assembled, and a multipart sentence stays unsupported
            # exactly as it was before this contract existed - a capture whose useful wind or
            # position records sit beside multipart traffic must keep converting. The one legacy
            # refusal kept is a *first* fragment that presents itself as a selected Class A report:
            # that is a malformed selected report, and the adopted gate still says so. A
            # continuation fragment carries no type character, so it is never read as one.
            malformed_selected = (not static_pairs and multipart and fields[2:3] == ["1"]
                                  and fields[5][:1] in (("B",) if class_b else ("1", "2", "3")))
            if pending is not None and not (multipart and fields[2:3] == ["2"]):
                # Adjacency is the caller's declared grouping; anything between the fragments means
                # the pair was interleaved, reordered or left incomplete.
                raise ValueError("saved AIS type 5 pair requires two matching adjacent fragments")
            if b24:
                if line[0] != "!":
                    raise ValueError("AIS delimiter required")
                row, b_fields, b24_fields = ais_b24_quantities(line, fields)
                write_ais_row([0, index, *row], line, None, b_fields, b24_fields)
                report["ais_records"] += 1
                report["ais_b24_part_a" if b24_fields[0] == "A" else "ais_b24_part_b"] += 1
            elif single or malformed_selected:
                if line[0] != "!":
                    raise ValueError("AIS delimiter required")
                values = ais_quantities(line, fields, class_b)
                write_ais_row([0, index, *values[:13], *[""] * (len(AIS_COLUMNS) - 16)], line,
                              ais_rot_quantities(line) if ais_rot else None,
                              values[13:] if class_b else None)
                report["ais_records"] += 1
            elif multipart and not static_pairs:
                # No assembly without the caller's explicit selection: accounted, not refused and
                # not silently turned into a static report.
                report["unsupported"].append(dict(line=index, raw=line))
                report["unsupported_records"] += 1
            elif multipart and fields[1:3] == ["2", "1"] and fields[5][:1] == "5":
                if line[0] != "!":
                    raise ValueError("AIS delimiter required")
                pending = (index, line, fields)
            elif multipart and fields[2:3] == ["2"] and pending is not None:
                first_index, first_line, first_fields = pending
                pending = None
                values = ais_static_quantities(first_line, line, first_fields, fields)
                binding = (f"DECLARED_ADJACENT_PAIR_LINES_{first_index}_{index}"
                           f"_SEQ_{first_fields[3]}_CH_{first_fields[4]}"
                           "_NOT_VERIFIED_SAME_TRANSMISSION")
                write_ais_row([0, first_index, *values, binding], first_line + "\r\n" + line)
                report["ais_records"] += 1
                report["ais_static_pairs"] += 1
                report["consumed_lines"] += [first_index, index]
                if first_fields[3] in report["reused_sequential_ids"]:
                    report["reused_sequential_id_pairs"] += 1
                report["reused_sequential_ids"].append(first_fields[3])
            else:
                # A continuation fragment carries no type character, so one that follows no selected
                # first fragment cannot be attributed to any message: it is accounted here rather
                # than combined with something, and rather than refusing a capture that simply
                # contains other multipart traffic. A selected pair whose *second* fragment is
                # missing still refuses, above and at the end of the input.
                report["unsupported"].append(dict(line=index, raw=line))
                report["unsupported_records"] += 1
            if stream.tell() > 128 * 1024 * 1024:
                raise ValueError("AIS output exceeds bound")
            continue
        if not identifier.endswith("MWV"):
            report["unsupported"].append(dict(line=index, raw=line))
            report["unsupported_records"] += 1
            continue
        if line[0] != '$' or len(identifier) != 5 or len(fields) < 6:
            raise ValueError("invalid MWV structure")
        angle, reference, speed, unit, status = fields[1:6]
        if reference not in ("R", "T") or unit not in ("N", "K", "M") or status not in ("A", "V"):
            raise ValueError("unsupported MWV reference, unit or validity")
        angle, speed = quantity(angle, 360), quantity(speed)
        valid = status == "A"
        report["mwv_records"] += 1
        report["invalid_status_records"] += int(not valid)
        report["valid_with_measurement"] += int(valid and (angle is not None or speed is not None))
        # The zero counter is explicitly unlocated, never an event timestamp or boot anchor.
        normalized_angle = ""
        if valid and angle is not None:
            degrees = angle % 360
            normalized_angle = repr(math.radians(degrees - 360 if degrees > 180 else degrees))
        normalized_speed = ""
        if valid and speed is not None:
            normalized_speed = repr(speed * {"N": 1852 / 3600, "K": 1 / 3.6, "M": 1}[unit])
        writer.writerow([0, index, identifier[:2],
                         "apparent_relative_to_vessel" if reference == "R" else "theoretical_relative_to_vessel",
                         "true" if valid else "false", normalized_angle, normalized_speed,
                         "hex:" + line.encode("ascii").hex()])
    if pending is not None:
        # A missing second fragment is never a success: no partial static report is emitted.
        raise ValueError("saved AIS type 5 pair is missing its second fragment")
    if not report["ais_records" if ais else "water_records" if water else "mwv_records"] and not allow_no_selected:
        raise ValueError("no selected NMEA records")
    if water:
        report["measurement_result"] = "HAS_REPORTED_VALUES" if report["water_measurements"] else "ONLY_WITHHELD_VALUES"
    return stream.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--kind", choices=("wind", "ais-class-a", "ais-class-b18", "water-dpt-mtw", "marine-mixed"), default="wind")
    parser.add_argument("--ais-class-b-static", action="store_true",
                        help="with --kind ais-class-b18: also read single-sentence type 24 Part A/B as separate rows")
    parser.add_argument("--ais-static-pairs", action="store_true",
                        help="explicitly select the adjacent saved type 5 fragment pair contract")
    parser.add_argument("--ais-rot", action="store_true",
                        help="interpret Class A reported ROT codes; no physical turn or clock inference")
    args = parser.parse_args()
    try:
        converted, report = convert(args.input.read_text(encoding="ascii"), args.kind,
                                    static_pairs=args.ais_static_pairs, ais_rot=args.ais_rot,
                                    class_b_static=args.ais_class_b_static)
        args.output_directory.mkdir()
        (args.output_directory / "converted.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"NMEA conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
