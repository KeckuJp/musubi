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


def ais_quantities(line, fields):
    from pyais import decode
    if (len(fields) != 7 or fields[1:3] != ["1", "1"] or fields[4] not in ("A", "B")
            or not re.fullmatch(r"[0-9]?", fields[3]) or fields[6] != "0"
            or not re.fullmatch(r"[0-W`-w]{28}", fields[5])):
        raise ValueError("fixed AIS Class A report requires one complete 168-bit sentence")
    data = decode(line.encode("ascii"))
    if data.msg_type not in (1, 2, 3) or not 0 <= data.mmsi <= 999999999:
        raise ValueError("invalid selected AIS identity or type")
    lon, lat, speed, course, heading = data.lon, data.lat, data.speed, data.course, data.heading
    if ((lon != 181 and not -180 <= lon <= 180) or (lat != 91 and not -90 <= lat <= 90)
            or not 0 <= speed <= 102.3 or not 0 <= course <= 360
            or (heading != 511 and not 0 <= heading <= 359)):
        raise ValueError("reserved or invalid AIS reported quantity")
    missing_position = lon == 181 or lat == 91
    return ["OWN_VESSEL_REPORTED" if fields[0] == "AIVDO" else "RECEIVED_REPORT",
        data.msg_type, data.mmsi, int(data.status), None if lon == 181 else lon, None if lat == 91 else lat,
        "INCOMPLETE_REPORTED_POSITION" if missing_position else "REPORTED_NOT_AUTHENTICATED",
        speed * 1852 / 3600 if speed < 102.2 else None,
        speed * 1852 / 3600 if speed == 102.2 else None,
        "UNAVAILABLE" if speed == 102.3 else "AT_LEAST" if speed == 102.2 else "REPORTED",
        None if course == 360 else math.radians(course),
        None if heading == 511 else math.radians(heading), data.second]


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


def convert_mixed(text, *, allow_no_selected=False, static_pairs=False):
    if len(text) > 16 * 1024 * 1024:
        raise ValueError("mixed NMEA input exceeds bound")
    rows, selected, reports = [], set(), {}
    for selector in ("wind", "ais-class-a", "water-dpt-mtw"):
        output, report = convert(text, selector, allow_no_selected=True, static_pairs=static_pairs)
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


def convert(text: str, kind="wind", *, allow_no_selected=False, static_pairs=False) -> tuple[str, dict]:
    """`static_pairs` is the caller's explicit opt-in to the adjacent type 5 pair contract.

    Adjacency plus a shared channel and sequential id does not prove two fragments came from one
    transmission, so no selector assembles a pair unless the caller asks for it by name.
    """
    if type(static_pairs) is not bool:
        raise ValueError("explicit static pair selection required")
    if kind == "marine-mixed":
        return convert_mixed(text, allow_no_selected=allow_no_selected, static_pairs=static_pairs)
    if kind not in ("wind", "ais-class-a", "water-dpt-mtw"):
        raise ValueError("unknown NMEA selection")
    ais = kind == "ais-class-a"
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
    writer.writerow(AIS_COLUMNS if ais else WATER_COLUMNS if water else COLUMNS)
    report = dict(input_records=len(lines), mwv_records=0, valid_with_measurement=0,
                  invalid_status_records=0, unsupported_records=0, unsupported=[], blank_lines=0)
    if ais:
        report.update(ais_records=0, clock="Unknown", time_basis="UNLOCATED_ZERO_NOT_EVENT_TIME",
                      decoder="pyais-3.2.2", semantics="ITU-R-M.1371-5-Class-A-1-2-3-and-5",
                      ais_static_pairs=0, consumed_lines=[], reused_sequential_ids=[],
                      reused_sequential_id_pairs=0,
                      static_binding="ADJACENT_PAIR_DECLARED_BY_THE_CALLER_NOT_GLOBALLY_UNIQUE",
                      eta_basis="REPORTED_ESTIMATE_WITHOUT_A_YEAR_NEVER_AN_ARRIVAL_OR_OBSERVATION_TIME")
    pending = None

    def write_ais_row(row, retained):
        if len(row) != len(AIS_COLUMNS) - 1:
            raise ValueError("AIS row width does not match the declared columns")
        writer.writerow([*row, "hex:" + retained.encode("ascii").hex()])
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
                      and fields[1:2] == ["1"] and fields[5][:1] in ("1", "2", "3"))
            # Without the opt-in nothing is assembled, and a multipart sentence stays unsupported
            # exactly as it was before this contract existed - a capture whose useful wind or
            # position records sit beside multipart traffic must keep converting. The one legacy
            # refusal kept is a *first* fragment that presents itself as a selected Class A report:
            # that is a malformed selected report, and the adopted gate still says so. A
            # continuation fragment carries no type character, so it is never read as one.
            malformed_selected = (not static_pairs and multipart and fields[2:3] == ["1"]
                                  and fields[5][:1] in ("1", "2", "3"))
            if pending is not None and not (multipart and fields[2:3] == ["2"]):
                # Adjacency is the caller's declared grouping; anything between the fragments means
                # the pair was interleaved, reordered or left incomplete.
                raise ValueError("saved AIS type 5 pair requires two matching adjacent fragments")
            if single or malformed_selected:
                if line[0] != "!":
                    raise ValueError("AIS delimiter required")
                # The adopted gate refuses the fragmented case by name; only a single complete
                # sentence reaches a row.
                values = ais_quantities(line, fields)
                write_ais_row([0, index, *values, *[""] * (len(AIS_COLUMNS) - 16)], line)
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
    parser.add_argument("--kind", choices=("wind", "ais-class-a", "water-dpt-mtw", "marine-mixed"), default="wind")
    parser.add_argument("--ais-static-pairs", action="store_true",
                        help="explicitly select the adjacent saved type 5 fragment pair contract")
    args = parser.parse_args()
    try:
        converted, report = convert(args.input.read_text(encoding="ascii"), args.kind,
                                    static_pairs=args.ais_static_pairs)
        args.output_directory.mkdir()
        (args.output_directory / "converted.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"NMEA conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
