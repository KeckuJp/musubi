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
    "ais_course_rad", "ais_true_heading_rad", "ais_second_code", "source_sentence_hex")
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


def convert_mixed(text, *, allow_no_selected=False):
    if len(text) > 16 * 1024 * 1024:
        raise ValueError("mixed NMEA input exceeds bound")
    rows, selected, reports = [], set(), {}
    for selector in ("wind", "ais-class-a", "water-dpt-mtw"):
        output, report = convert(text, selector, allow_no_selected=True)
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
    unsupported = [item for item in reports["wind"]["unsupported"] if item["line"] not in selected]
    count = reports["wind"]["input_records"]
    if len(selected) + len(unsupported) != count:
        raise ValueError("mixed NMEA record accounting mismatch")
    return output.getvalue(), dict(input_records=count, selected_records=len(selected),
        unsupported_records=len(unsupported), unsupported=unsupported, selector_reports=reports,
        clock="Unknown", time_basis="UNLOCATED_ZERO_NOT_EVENT_TIME",
        qualification="SELECTED_REPORTED_MEANINGS_NOT_SENSOR_FUSION")


def convert(text: str, kind="wind", *, allow_no_selected=False) -> tuple[str, dict]:
    if kind == "marine-mixed":
        return convert_mixed(text, allow_no_selected=allow_no_selected)
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
                      decoder="pyais-3.2.2", semantics="ITU-R-M.1371-5-Class-A-1-2-3")
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
            if identifier in ("AIVDM", "AIVDO") and len(fields) > 5 and fields[5][:1] in ("1", "2", "3"):
                if line[0] != "!":
                    raise ValueError("AIS delimiter required")
                values = ais_quantities(line, fields)
                writer.writerow([0, index, *values, "hex:" + line.encode("ascii").hex()])
                report["ais_records"] += 1
                if stream.tell() > 128 * 1024 * 1024:
                    raise ValueError("AIS output exceeds bound")
            else:
                report["unsupported"].append(dict(line=index, raw=line))
                report["unsupported_records"] += 1
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
    args = parser.parse_args()
    try:
        converted, report = convert(args.input.read_text(encoding="ascii"), args.kind)
        args.output_directory.mkdir()
        (args.output_directory / "converted.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"NMEA conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
