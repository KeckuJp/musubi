#!/usr/bin/env python3
"""Saved RINEX 3.04 GPS observation and navigation records to common Observation CSV.

Offline and passive: a file already on disk is read. No device, no stream, no positioning
solution, no correction feed, no orbit propagation and no navigation command of any kind.

Every field layout, unit, sentinel and flag meaning below is fixed by the official IGS
specification **RINEX Version 3.04** (`RINEX 3.04.IGS.RTCM.doc 2018-11-23`,
https://files.igs.org/pub/data/format/rinex304.pdf, 1776317 bytes, sha256
830811bb3e6db991559ec7ea1c0b6669baceb452b9f0c22dd70f8728a2fb9ec0): Table A2 for the
observation header, Table A3 for the observation data records, Table A6 for the GPS
navigation record. Only GPS is decoded; any other constellation is retained and counted.

Time is never converted. The epoch is exported exactly as written together with the
declared time system, and the common capture clock is the caller's own declaration.
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import re
import sys
from pathlib import Path

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_LINE = 1024
MAX_HEADER_RECORDS = 1024
MAX_RETAINED_REPORT = 64 * 1024
VERSION = "3.04"
LABEL = slice(60, 80)
# Table A2: type / band / attribute. Only these four types are qualified here.
TYPE_UNITS = {"C": ("rinex_pseudorange_m", "PSEUDORANGE_METERS"),
              "L": ("rinex_carrier_phase_cycles", "PHASE_FULL_CYCLES"),
              "D": ("rinex_doppler_hz", "DOPPLER_HZ"),
              "S": ("rinex_signal_strength_value", "SIGNAL_STRENGTH")}
UNQUALIFIED_TYPES = ("I", "X")
OBSERVATION_CODE = re.compile(r"[A-Z][0-9][A-Z]")
# Table 4 "RINEX Version 3.04 GPS Observation Codes", transcribed exactly. Each band lists
# the attributes the table prints; the codeless rows deliberately have no pseudorange, so
# C1N and C2N are absent from the table and are therefore not admitted here.
GPS_CODE_ATTRIBUTES = {"1": "CSLXPWYM", "2": "CDSLXPWYM", "5": "IQX"}
GPS_CODELESS = {"1": "N", "2": "N", "5": ""}
GPS_OBSERVATION_CODES = frozenset(
    [kind + band + attribute for band, attributes in GPS_CODE_ATTRIBUTES.items()
     for attribute in attributes for kind in "CLDS"]
    + [kind + band + attribute for band, attributes in GPS_CODELESS.items()
       for attribute in attributes for kind in "LDS"])  # codeless: no pseudorange row
UNKNOWN_CODE_STATUS = "UNKNOWN_GPS_OBSERVATION_CODE_NO_TYPED_VALUE"
# Table A6 footnote: E, e, D and d are allowed, with zero-padded two-digit exponents.
NAV_FLOAT = re.compile(r"[+-]?(?:[0-9]*\.[0-9]+|[0-9]+\.?)(?:[DdEe][+-][0-9]{2})?")
# Table A6 footnote **): transmission time is 0.9999E9 when not known.
TRANSMISSION_UNKNOWN = 0.9999e9
SSI_MEANING = {0: "NOT_KNOWN_DONT_CARE", 1: "MINIMUM_POSSIBLE_SIGNAL_STRENGTH",
               5: "AVERAGE_OR_GOOD_S_N_RATIO", 9: "MAXIMUM_POSSIBLE_SIGNAL_STRENGTH"}
LLI_BITS = {0: "LOST_LOCK_CYCLE_SLIP_POSSIBLE_PHASE_ONLY", 1: "HALF_CYCLE_AMBIGUITY_OR_SLIP",
            2: "GALILEO_BOC_TRACKING_OF_MBOC_SIGNAL"}
EPOCH_FLAGS = {0: "OK", 1: "POWER_FAILURE_BETWEEN_PREVIOUS_AND_CURRENT_EPOCH"}
EVENT_FLAGS = {2: "START_MOVING_ANTENNA", 3: "NEW_SITE_OCCUPATION", 4: "HEADER_INFORMATION_FOLLOWS",
               5: "EXTERNAL_EVENT", 6: "CYCLE_SLIP_RECORDS_FOLLOW"}
TIME_SYSTEMS = ("GPS", "GLO", "GAL", "QZS", "BDT", "IRN")
# Figure 1 (3.5): the only satellite system identifiers the specification defines.
SATELLITE_SYSTEMS = ("G", "R", "S", "E", "C", "J", "I")
# Lines per navigation record, read from the framing tables: the SV/EPOCH/SV CLK line plus
# the numbered BROADCAST ORBIT lines. A6 GPS 7, A8 Galileo 7, A10 GLONASS 3, A12 QZSS 7,
# A14 BeiDou 7, A16 SBAS 3, A18 IRNSS 7. Never guessed.
NAV_RECORD_LINES = {"G": 8, "E": 8, "J": 8, "C": 8, "I": 8, "R": 4, "S": 4}
# 5.5: a SYS / SCALE FACTOR record means the stored phase data was multiplied before
# storage. This path does not de-scale, so its presence is refused rather than ignored.
SCALE_FACTOR_LABEL = "SYS / SCALE FACTOR"
LEAP_SECOND_BASIS = "LEAP_SECOND_REPRESENTATION_RETAINED_NOT_CONVERTED"
SIGNAL_UNIT_UNRESOLVED = "RECEIVER_DEPENDENT_UNIT_NOT_RESOLVED_BY_THE_FILE"
SIGNAL_UNIT_DBHZ = "DBHZ_DECLARED_BY_THE_SIGNAL_STRENGTH_UNIT_HEADER"
TIME_BASIS = "EXACT_DECLARED_GNSS_TIME_SYSTEM_NO_UTC_CONVERSION_APPLIED"
CAPTURE_BASIS = "CALLER_DECLARED_CAPTURE_MICROSECONDS_SEPARATE_FROM_THE_GNSS_EPOCH"
IDENTITY_BASIS = "DECLARED_SATELLITE_AND_RECEIVER_IDENTITY_NOT_AUTHENTICITY"
OBS_FIELDS = [
    "record_time_us", "capture_clock_basis", "source_line_index", "source_line_hex",
    "rinex_version", "rinex_file_type", "rinex_satellite_system_declared",
    "rinex_epoch_time_system", "rinex_epoch_time_basis", "rinex_epoch_year",
    "rinex_epoch_month", "rinex_epoch_day", "rinex_epoch_hour", "rinex_epoch_minute",
    "rinex_epoch_second_text", "rinex_epoch_flag_code", "rinex_epoch_flag_reported",
    "rinex_receiver_clock_offset_s", "rinex_satellite_id", "rinex_satellite_identity_basis",
    "rinex_observation_code", "rinex_observation_band", "rinex_observation_attribute",
    "rinex_pseudorange_m", "rinex_carrier_phase_cycles", "rinex_doppler_hz",
    "rinex_signal_strength_value", "rinex_signal_strength_unit_basis",
    "rinex_observation_status", "rinex_observation_raw_hex", "rinex_lli_code",
    "rinex_lli_bits_reported", "rinex_ssi_code", "rinex_ssi_meaning"]
NAV_ORBIT = [
    ("iode_issue_of_data_ephemeris", ""), ("crs_m", "METERS"),
    ("delta_n_rad_per_s", "RADIANS_PER_SECOND"), ("m0_rad", "RADIANS"),
    ("cuc_rad", "RADIANS"), ("eccentricity", ""), ("cus_rad", "RADIANS"),
    ("sqrt_a_sqrt_m", "SQRT_METERS"),
    ("toe_s_of_gps_week", "SECONDS_OF_GPS_WEEK"), ("cic_rad", "RADIANS"),
    ("omega0_rad", "RADIANS"), ("cis_rad", "RADIANS"),
    ("i0_rad", "RADIANS"), ("crc_m", "METERS"), ("omega_rad", "RADIANS"),
    ("omega_dot_rad_per_s", "RADIANS_PER_SECOND"),
    ("idot_rad_per_s", "RADIANS_PER_SECOND"), ("codes_on_l2_channel_code", ""),
    ("gps_week_continuous", ""), ("l2_p_data_flag_code", ""),
    ("sv_accuracy_m", "METERS"), ("sv_health_bits_code", ""), ("tgd_s", "SECONDS"),
    ("iodc_issue_of_data_clock", ""),
    ("transmission_time_s_of_gps_week", "SECONDS_OF_GPS_WEEK"),
    ("fit_interval_hours", "HOURS"), ("spare_1", ""), ("spare_2", "")]
NAV_FIELDS = [
    "record_time_us", "capture_clock_basis", "source_line_index", "source_record_hex",
    "rinex_version", "rinex_file_type", "rinex_satellite_system_declared",
    "rinex_satellite_id", "rinex_satellite_identity_basis", "rinex_toc_time_system",
    "rinex_epoch_time_basis", "rinex_toc_year", "rinex_toc_month", "rinex_toc_day",
    "rinex_toc_hour", "rinex_toc_minute", "rinex_toc_second",
    "rinex_sv_clock_bias_s", "rinex_sv_clock_drift_s_per_s", "rinex_sv_clock_drift_rate_s_per_s2",
] + ["rinex_" + name for name, _ in NAV_ORBIT] + [
    "rinex_transmission_time_status", "rinex_fit_interval_status"]
FAILURE = "rinex conversion failed; verify the file, the declared kind and a new output path"


def text(line, start, end):
    """Fixed-column slice, padded: a record that ends early simply yields blanks (6.3)."""
    return line[start:end].ljust(end - start)


def integer(raw, low, high, what, *, blank=None):
    token = raw.strip()
    if not token:
        if blank is None:
            raise ValueError(f"{what} required")
        return blank
    if not re.fullmatch(r"[0-9]+", token) or not low <= int(token) <= high:
        raise ValueError(f"invalid {what}")
    return int(token)


def observation_value(raw):
    """F14.3. Blank or 0.0 both mean not observed (Table A3), never a zero measurement."""
    token = raw.strip()
    if not token:
        return None
    if not re.fullmatch(r"[+-]?(?:[0-9]*\.[0-9]+|[0-9]+\.?)", token):
        raise ValueError("invalid observation value")
    value = float(token)
    return None if value == 0.0 else value


def nav_value(raw, what):
    token = raw.strip()
    if not token:
        return None
    if not NAV_FLOAT.fullmatch(token):
        raise ValueError(f"invalid navigation {what} representation")
    return float(token.replace("D", "E").replace("d", "e"))


def satellite_identifier(system, number, *, what="satellite identifier"):
    """Figure 1: one system character plus a two-digit number. G00 is not a PRN."""
    if system not in SATELLITE_SYSTEMS:
        raise ValueError("unknown satellite system identifier")
    if not re.fullmatch(r"[ 0-9]{2}", number) or not number.strip():
        raise ValueError(f"invalid {what}")
    value = int(number.replace(" ", "0"))
    if value < 1:
        # The upper bound is not printed in the tables read, so only the impossible zero
        # is refused here; a larger number is retained as the file declares it.
        raise ValueError(f"invalid {what}")
    return "%s%02d" % (system, value)


def checked_date(year, month, day, hour, minute, what):
    """A real Gregorian date and a real clock; February 31 is not a date."""
    try:
        datetime.date(year, month, day)
    except ValueError as error:
        raise ValueError(f"invalid {what} date") from error
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid {what} time")


def checked_seconds(token, what):
    """Return (text, basis). 60 is kept as a declared leap representation, never converted."""
    if not re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]+)?", token):
        raise ValueError(f"invalid {what} seconds")
    value = float(token)
    if value >= 61.0:
        raise ValueError(f"invalid {what} seconds")
    return token, LEAP_SECOND_BASIS if value >= 60.0 else None


def new_header_state():
    return {"rinex_version": None, "rinex_file_type": None, "satellite_system": None,
            "time_system": None, "time_system_basis": None, "signal_unit": SIGNAL_UNIT_UNRESOLVED,
            "interval_s": None, "systems": {}, "codes": [], "labels": {}, "records": 0,
            "current_system": None, "updates": []}


def header_records(lines, header, *, first=False, event=False):
    """Apply header records to the state. Shared by the file header and a flag-4 update.

    Per-system continuation state is kept, so `G` followed by `R` never disturbs the GPS
    declaration, a duplicate system declaration or an excess descriptor is refused rather
    than silently absorbed, and an update that would change a meaning this path does not
    model is refused instead of being applied to later measurements under a stale schema.
    """
    # The duplicate rule is per header section: a flag-4 update legitimately redeclares a
    # system, and that replacement is exactly the update being applied.
    header["current_system"] = None
    section, singletons = set(), {}
    for index, line in enumerate(lines):
        header["records"] += 1
        if header["records"] > MAX_HEADER_RECORDS:
            raise ValueError("header record count exceeds bound")
        label = text(line, 60, 80).strip()
        header["labels"][label] = header["labels"].get(label, 0) + 1
        if label == SCALE_FACTOR_LABEL:
            # 5.5: the stored phase data was multiplied. Ignoring that would report a
            # wrong number, so an unhandled semantic update refuses before any success.
            raise ValueError("SYS / SCALE FACTOR changes stored values and is not supported")
        if first and index == 0:
            if label != "RINEX VERSION / TYPE":
                raise ValueError("RINEX VERSION / TYPE must be the first record")
            header["rinex_version"] = text(line, 0, 9).strip()
            header["rinex_file_type"] = text(line, 20, 21).strip().upper()
            header["satellite_system"] = text(line, 40, 41).strip().upper()
            continue
        if label == "RINEX VERSION / TYPE":
            # A singleton record may not be redeclared, in the header or in an update.
            raise ValueError("RINEX VERSION / TYPE may appear only as the first record")
        if label == "SYS / # / OBS TYPES":
            system = text(line, 0, 1).strip().upper()
            descriptors = [text(line, 7 + 4 * i, 10 + 4 * i).strip() for i in range(13)]
            if system:
                if system not in SATELLITE_SYSTEMS:
                    raise ValueError("unknown satellite system in SYS / # / OBS TYPES")
                if system in section:
                    raise ValueError("duplicate SYS / # / OBS TYPES declaration for a system")
                section.add(system)
                header["systems"][system] = {
                    "declared": integer(text(line, 3, 6), 0, 999, "observation type count"),
                    "codes": []}
                header["current_system"] = system
            elif header["current_system"] is None:
                raise ValueError("a SYS / # / OBS TYPES continuation needs its declaration")
            entry = header["systems"][header["current_system"]]
            for descriptor in descriptors:
                if not descriptor:
                    continue
                if len(entry["codes"]) >= entry["declared"]:
                    raise ValueError("more observation descriptors than the declared count")
                if not OBSERVATION_CODE.fullmatch(descriptor):
                    raise ValueError("invalid observation descriptor")
                if descriptor in entry["codes"]:
                    raise ValueError("duplicate observation descriptor for a system")
                entry["codes"].append(descriptor)
            continue
        header["current_system"] = None
        if label in ("TIME OF FIRST OBS", "SIGNAL STRENGTH UNIT", "INTERVAL"):
            # A singleton may not contradict itself inside one header section. A flag-4
            # update is a new section, so an intentional change there is allowed.
            value = text(line, 0, 60)
            if label in singletons and singletons[label] != value:
                raise ValueError(f"conflicting {label} records in one header section")
            singletons[label] = value
        if label == "TIME OF FIRST OBS":
            system = text(line, 48, 51).strip().upper()
            if system and system not in TIME_SYSTEMS:
                raise ValueError("unknown declared time system")
            if not system:
                # "Compulsory in mixed GNSS files"; the GPS default is for pure GPS files.
                if header["satellite_system"] != "G":
                    raise ValueError("a mixed file must declare its time system")
                if header["time_system_basis"] == "DECLARED_IN_TIME_OF_FIRST_OBS":
                    # A blank record may not quietly replace an explicit declaration.
                    raise ValueError("a blank time system contradicts an explicit declaration")
                header["time_system"] = "GPS"
                header["time_system_basis"] = (
                    "DEFAULTED_TO_GPS_FOR_A_PURE_GPS_FILE_BY_THE_SPECIFICATION")
            else:
                if event and system != header["time_system"]:
                    header["updates"].append("TIME_SYSTEM")
                header["time_system"] = system
                header["time_system_basis"] = "DECLARED_IN_TIME_OF_FIRST_OBS"
        elif label == "SIGNAL STRENGTH UNIT":
            unit = SIGNAL_UNIT_DBHZ if text(line, 0, 20).strip().upper() == "DBHZ" else SIGNAL_UNIT_UNRESOLVED
            if event and unit != header["signal_unit"]:
                header["updates"].append("SIGNAL_STRENGTH_UNIT")
            header["signal_unit"] = unit
        elif label == "INTERVAL":
            header["interval_s"] = nav_value(text(line, 0, 10), "interval")
    # Completeness is decided by the declared count against the descriptors read, in
    # finish_header. A complete declaration may legitimately be the last line of an update,
    # so a still-set current system is not evidence of anything.
    return header


def finish_header(header, kind):
    """Check what only makes sense once a header section is complete."""
    for system, entry in header["systems"].items():
        if len(entry["codes"]) != entry["declared"]:
            raise ValueError("declared observation type count disagrees with the descriptors read")
    header["codes"] = list(header["systems"].get("G", {}).get("codes", []))
    if header["rinex_version"] != VERSION:
        raise ValueError("only the pinned RINEX 3.04 version is qualified")
    if header["rinex_file_type"] != ("O" if kind == "obs" else "N"):
        raise ValueError("declared RINEX file type contradicts the selected kind")
    if header["satellite_system"] not in ("G", "M"):
        raise ValueError("only a GPS or mixed satellite system declaration is accepted")
    if "END OF HEADER" not in header["labels"]:
        raise ValueError("END OF HEADER record required")
    if kind == "obs":
        if not header["codes"]:
            raise ValueError("SYS / # / OBS TYPES for GPS required")
        if header["time_system"] is None:
            raise ValueError("TIME OF FIRST OBS record required")
    return header


def read_header(lines, kind):
    """Return the declared header facts, the GPS observation codes and the record counts."""
    return finish_header(header_records(lines, new_header_state(), first=True), kind)


def split_header(lines):
    for index, line in enumerate(lines):
        if text(line, 60, 80).strip() == "END OF HEADER":
            return lines[:index + 1], lines[index + 1:], index + 1
    raise ValueError("END OF HEADER record required")


def epoch_fields(line, header):
    """EPOCH record, Table A3: > 1X,I4 4(1X,I2.2) F11.7 2X,I1 I3 6X F15.12."""
    flag = integer(text(line, 31, 32), 0, 6, "epoch flag")
    count = integer(text(line, 32, 35), 0, 999, "epoch record count", blank=0)
    blank_epoch = not text(line, 2, 29).strip()
    if blank_epoch and flag < 2:
        raise ValueError("an observation epoch requires its time fields")
    fields = {"rinex_epoch_flag_code": flag,
              "rinex_epoch_flag_reported": EPOCH_FLAGS.get(flag) or EVENT_FLAGS[flag],
              "rinex_epoch_time_system": header["time_system"],
              "rinex_epoch_time_basis": TIME_BASIS}
    if not blank_epoch:
        for name, start, end, low, high in (("year", 2, 6, 0, 9999), ("month", 7, 9, 1, 12),
                                            ("day", 10, 12, 1, 31), ("hour", 13, 15, 0, 23),
                                            ("minute", 16, 18, 0, 59)):
            fields["rinex_epoch_" + name] = integer(text(line, start, end), low, high, name)
        checked_date(*(fields["rinex_epoch_" + name] for name in
                       ("year", "month", "day", "hour", "minute")), "epoch")
        second, leap = checked_seconds(text(line, 18, 29).strip(), "epoch")
        fields["rinex_epoch_second_text"] = second
        if leap:
            fields["rinex_epoch_time_basis"] = TIME_BASIS + "_" + leap
    offset = text(line, 41, 56).strip()
    if offset:
        fields["rinex_receiver_clock_offset_s"] = nav_value(offset, "receiver clock offset")
    return fields, flag, count, blank_epoch


def convert_observations(lines, header, capture_time_us, report):
    """Yield rows as they are produced, so the output budget is enforced during expansion."""
    order, retained = None, 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if text(line, 0, 1) != ">":
            raise ValueError("an epoch record was expected")
        fields, flag, count, blank_epoch = epoch_fields(line, header)
        report["epoch_records"] += 1
        index += 1
        if flag >= 2:
            # Events: the count is the number of special records to follow (Table A3, 6.8).
            # They are skipped and accounted, never decoded as observations.
            report["event_records"] += 1
            report["event_payload_records"] += count
            payload = lines[index:index + count]
            if len(payload) != count:
                raise ValueError("event payload record count exceeds the file")
            entry = {"line_index": report["header_lines"] + index - 1, "epoch_flag": flag,
                     "reported": fields["rinex_epoch_flag_reported"],
                     "payload_records": count, "epoch_declared": not blank_epoch,
                     "payload_lines": payload}
            if flag == 4:
                # "4: header information follows" — applying it is the only honest choice:
                # continuing with the previous code order, time system or signal-strength
                # unit would relabel every later measurement.
                before = list(header["codes"])
                header_records(payload, header, event=True)
                finish_header(header, "obs")
                entry["applied_header_update"] = True
                entry["observation_codes_after"] = list(header["codes"])
                if header["codes"] != before:
                    report["observation_code_updates"] += 1
                    report["observation_codes"] = list(header["codes"])
                report["signal_strength_unit_basis"] = header["signal_unit"]
                report["header_updates"] += 1
            if retained + len(json.dumps(entry)) <= MAX_RETAINED_REPORT:
                retained += len(json.dumps(entry))
                report["events"].append(entry)
            else:
                report["event_sample_truncated"] = True
            index += count
            continue
        # Order numerically; the seconds text itself stays exact in the row (6.5 requires
        # increasing, non-repeating observation epochs).
        key = tuple(fields.get("rinex_epoch_" + part) for part in
                    ("year", "month", "day", "hour", "minute")) + (
                        float(fields["rinex_epoch_second_text"]),)
        if order is not None and key <= order:
            raise ValueError("observation epochs must increase and not repeat")
        order = key
        for _ in range(count):
            if index >= len(lines):
                raise ValueError("declared satellite count exceeds the records present")
            yield from satellite_rows(lines[index], header, fields, capture_time_us,
                                      report["header_lines"] + index, report)
            index += 1
        report["satellite_records"] += count


def satellite_rows(line, header, epoch, capture_time_us, line_index, report):
    system, number = text(line, 0, 1).strip().upper(), text(line, 1, 3)
    identifier = satellite_identifier(system, number)
    if system != "G":
        # Another constellation in a mixed file: retained and counted, never read as GPS.
        report["non_gps_satellite_records"] += 1
        report["non_gps_systems"][system] = report["non_gps_systems"].get(system, 0) + 1
        return []
    base = {"record_time_us": capture_time_us, "capture_clock_basis": CAPTURE_BASIS,
            "source_line_index": line_index, "source_line_hex": "hex:" + line.encode().hex(),
            "rinex_version": header["rinex_version"], "rinex_file_type": header["rinex_file_type"],
            "rinex_satellite_system_declared": header["satellite_system"],
            "rinex_satellite_id": identifier, "rinex_satellite_identity_basis": IDENTITY_BASIS}
    base.update(epoch)
    rows = []
    for position, code in enumerate(header["codes"]):
        start = 3 + 16 * position
        kind = code[0]
        row = dict(base, rinex_observation_code=code, rinex_observation_band=code[1],
                   rinex_observation_attribute=code[2])
        raw = text(line, start, start + 14)
        if kind in UNQUALIFIED_TYPES:
            report["unqualified_code_records"] += 1
            row["rinex_observation_status"] = "UNQUALIFIED_OBSERVATION_TYPE_NO_VALUE_TYPED"
            row["rinex_observation_raw_hex"] = "hex:" + raw.encode().hex()
            rows.append(row)
            continue
        if kind not in TYPE_UNITS:
            raise ValueError("unknown observation type character")
        if code not in GPS_OBSERVATION_CODES:
            # Table 4 lists the GPS codes. One outside it is retained with its exact source
            # field and no typed value; it is never counted as a qualified GPS observation.
            report["unknown_gps_code_records"] += 1
            report["unknown_gps_codes"][code] = report["unknown_gps_codes"].get(code, 0) + 1
            row["rinex_observation_status"] = UNKNOWN_CODE_STATUS
            row["rinex_observation_raw_hex"] = "hex:" + raw.encode().hex()
            rows.append(row)
            continue
        value = observation_value(raw)
        lli = text(line, start + 14, start + 15).strip()
        ssi = text(line, start + 15, start + 16).strip()
        if lli:
            code_value = integer(lli, 0, 7, "LLI")
            row["rinex_lli_code"] = code_value
            row["rinex_lli_bits_reported"] = "|".join(
                name for bit, name in LLI_BITS.items() if code_value >> bit & 1) or "OK_OR_NOT_KNOWN"
        if ssi:
            code_value = integer(ssi, 0, 9, "SSI")
            row["rinex_ssi_code"] = code_value
            row["rinex_ssi_meaning"] = SSI_MEANING.get(
                code_value, "PROJECTED_INTERVAL_1_TO_9_BETWEEN_MINIMUM_AND_MAXIMUM")
        if value is None:
            row["rinex_observation_status"] = "NOT_OBSERVED"
        else:
            row["rinex_observation_status"] = "REPORTED"
            row[TYPE_UNITS[kind][0]] = value
            if kind == "S":
                row["rinex_signal_strength_unit_basis"] = header["signal_unit"]
        rows.append(row)
        report["observation_values"] += 1
    return rows


def convert_navigation(lines, header, capture_time_us, report):
    """Yield rows as they are produced. Record length is the system's qualified framing."""
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        line = lines[index]
        system = text(line, 0, 1).strip().upper()
        if system not in NAV_RECORD_LINES:
            raise ValueError("unknown satellite system in a navigation record")
        span = NAV_RECORD_LINES[system]
        record = lines[index:index + span]
        if len(record) < span:
            raise ValueError("a navigation record needs all of its qualified lines")
        for continuation in record[1:]:
            # 4X: an orbit line starts with four blanks, so a following record's identifier
            # can never be consumed as one of the previous record's orbit lines.
            if continuation[:4].strip():
                raise ValueError("a navigation continuation line must keep its 4X indentation")
        if system != "G":
            report["non_gps_navigation_records"] += 1
            report["non_gps_systems"][system] = report["non_gps_systems"].get(system, 0) + 1
            report["non_gps_navigation_lines"] += span
            index += span
            continue
        number = text(line, 1, 3)
        row = {"record_time_us": capture_time_us, "capture_clock_basis": CAPTURE_BASIS,
               "source_line_index": report["header_lines"] + index,
               "source_record_hex": "hex:" + "\n".join(record).encode().hex(),
               "rinex_version": header["rinex_version"],
               "rinex_file_type": header["rinex_file_type"],
               "rinex_satellite_system_declared": header["satellite_system"],
               "rinex_satellite_id": satellite_identifier(system, number),
               "rinex_satellite_identity_basis": IDENTITY_BASIS,
               # Table A6 states the epoch is Toc - Time of Clock (GPS).
               "rinex_toc_time_system": "GPS", "rinex_epoch_time_basis": TIME_BASIS}
        for name, start, end, low, high in (("year", 4, 8, 0, 9999), ("month", 9, 11, 1, 12),
                                            ("day", 12, 14, 1, 31), ("hour", 15, 17, 0, 23),
                                            ("minute", 18, 20, 0, 59)):
            row["rinex_toc_" + name] = integer(text(line, start, end), low, high, name)
        checked_date(*(row["rinex_toc_" + name] for name in
                       ("year", "month", "day", "hour", "minute")), "time of clock")
        second, leap = checked_seconds(text(line, 21, 23).strip(), "time of clock")
        row["rinex_toc_second"] = second
        if leap:
            row["rinex_epoch_time_basis"] = TIME_BASIS + "_" + leap
        for position, name in enumerate(("bias_s", "drift_s_per_s", "drift_rate_s_per_s2")):
            start = 23 + 19 * position
            row["rinex_sv_clock_" + name] = nav_value(text(line, start, start + 19), name)
        for orbit in range(7):
            for column in range(4):
                name, _ = NAV_ORBIT[orbit * 4 + column]
                start = 4 + 19 * column
                row["rinex_" + name] = nav_value(text(record[orbit + 1], start, start + 19), name)
        transmission = row["rinex_transmission_time_s_of_gps_week"]
        if transmission is not None and transmission == TRANSMISSION_UNKNOWN:
            # Table A6 footnote **): 0.9999E9 means not known, so it is not a time.
            row["rinex_transmission_time_s_of_gps_week"] = None
            row["rinex_transmission_time_status"] = "TRANSMISSION_TIME_NOT_KNOWN"
        else:
            row["rinex_transmission_time_status"] = (
                "REPORTED" if transmission is not None else "NOT_DECLARED")
        row["rinex_fit_interval_status"] = (
            "REPORTED" if row["rinex_fit_interval_hours"] is not None else "NOT_DECLARED")
        yield row
        report["navigation_records"] += 1
        index += span


def convert(data, kind, capture_time_us):
    """Return the CSV and an accounting report. Nothing selected is silently dropped."""
    if kind not in ("obs", "nav"):
        raise ValueError("explicit obs or nav kind required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2 ** 63:
        raise ValueError("explicit nonnegative signed64 capture microseconds required")
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_INPUT:
        raise ValueError("saved RINEX input limit")
    try:
        body = bytes(data).decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("RINEX records are ASCII") from error
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [line.rstrip("\r") for line in lines]
    if any(len(line) > MAX_LINE for line in lines):
        raise ValueError("RINEX line exceeds bound")
    head, records, header_lines = split_header(lines)
    header = read_header(head, kind)
    report = {"kind": kind, "header_lines": header_lines, "header_labels": header["labels"],
              "rinex_version": header["rinex_version"], "rinex_file_type": header["rinex_file_type"],
              "satellite_system_declared": header["satellite_system"],
              "observation_codes": header["codes"], "interval_s": header["interval_s"],
              "signal_strength_unit_basis": header["signal_unit"],
              "time_system": header["time_system"], "time_system_basis": header["time_system_basis"],
              "epoch_records": 0, "satellite_records": 0, "observation_values": 0,
              "navigation_records": 0, "event_records": 0, "event_payload_records": 0,
              "unqualified_code_records": 0, "unknown_gps_code_records": 0,
              "unknown_gps_codes": {}, "admitted_gps_codes": len(GPS_OBSERVATION_CODES),
              "non_gps_satellite_records": 0,
              "non_gps_navigation_records": 0, "non_gps_navigation_lines": 0,
              "non_gps_systems": {}, "events": [], "event_sample_truncated": False,
              "header_updates": 0, "observation_code_updates": 0,
              "navigation_record_lines_by_system": NAV_RECORD_LINES,
              "clock": "Unknown", "capture_clock_basis": CAPTURE_BASIS,
              "epoch_time_basis": TIME_BASIS,
              "specification": "RINEX 3.04 IGS, sha256:830811bb3e6db991559ec7ea1c0b6669baceb452b9f0c22dd70f8728a2fb9ec0",
              "source_sha256": "sha256:" + hashlib.sha256(bytes(data)).hexdigest(),
              "retention_basis": "the header and event payload lines below are the exact source"
                                 " text; label counts alone are not retention, and the original"
                                 " file remains the reference for anything truncated here",
              "evidence_kind": "record-conversion-not-a-positioning-solution-or-real-recording"}
    # The exact header text is retained, bounded, with truncation stated rather than implied.
    retained_header, budget = [], 0
    for line in head:
        if budget + len(line) + 2 > MAX_RETAINED_REPORT:
            report["header_sample_truncated"] = True
            break
        budget += len(line) + 2
        retained_header.append(line)
    else:
        report["header_sample_truncated"] = False
    report["header_source_lines"] = retained_header
    rows = (convert_observations(records, header, capture_time_us, report) if kind == "obs"
            else convert_navigation(records, header, capture_time_us, report))
    fields = OBS_FIELDS if kind == "obs" else NAV_FIELDS
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    written = 0
    # Rows are written as they are produced, so the budget is enforced *during* expansion
    # rather than after a whole file has already been built in memory.
    for row in rows:
        writer.writerow(row)
        written += 1
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted CSV exceeds bound")
    if not written:
        raise ValueError("no selected GPS record in the file")
    report["output_records"] = written
    report["output_bytes"] = output.tell()
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved RINEX 3.04 file already on disk")
    parser.add_argument("output_directory", type=Path, help="new directory for the CSV and report")
    parser.add_argument("--kind", required=True, choices=("obs", "nav"),
                        help="declared record kind; the header's file type must agree")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller capture microseconds, separate from the GNSS epoch")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as stream:
            data = stream.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT:
            raise ValueError("saved RINEX input limit")
        text_output, report = convert(data, args.kind, args.capture_time_us)
        args.output_directory.mkdir()  # Refuse an existing output, including the input path.
        with (args.output_directory / "observations.csv").open("w", encoding="utf-8", newline="") as stream:
            stream.write(text_output)
        (args.output_directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError) as error:
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
