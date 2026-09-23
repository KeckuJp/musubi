#!/usr/bin/env python3
"""Saved Septentrio Binary Format PVTGeodetic blocks to common Observation CSV.

Offline and passive: a file already on disk is read. No receiver, no stream, no serial or
network connection, no positioning solution, no correction feed and no command of any kind.

Every framing rule, field offset, unit and do-not-use value below is taken from **Septentrio's
own** published driver, pinned at
`github.com/septentrio-gnss/septentrio_gnss_driver` commit
`5613af2969e3f4aac4ddc0b819e773d43a71ce9b` (tag v1.4.8, BSD-3-Clause, "© Copyright 2020,
Septentrio NV/SA."): `include/septentrio_gnss_driver/parsers/sbf_blocks.hpp`,
`src/septentrio_gnss_driver/crc/crc.cpp` and `msg/PVTGeodetic.msg`. That source defines the
format **operationally, in code** - it is a driver, not the normative specification, and the
firmware reference guide was not acquired. No third-party code is vendored and no decoder
dependency is added: the frame and block reader below is stdlib only.

What the pinned source does **not** state is not invented here. The `Mode`, `Error`,
`time_system` and `datum` value tables, the units of the receiver time counters, and the
meaning of a block revision beyond "appends fields" are all absent from the files read, so
those values travel as reported codes with an explicit disposition saying so.

Height is "m (ellipsoidal)" in the pinned message definition. It is never called MSL, and no
mean-sea-level height is derived from it and the reported undulation.

The reported velocity components and course are carried for the same reason and under the same
rule. `msg/PVTGeodetic.msg` declares `vn # m/s`, `ve # m/s`, `vu # m/s` and `cog # deg`, so those
units are stated; the same file declares **no** reference frame, datum or orientation convention
for the components and **no** reference (true north, magnetic north, course made good) for the
course, so none is named or applied. Nothing is rotated and no speed or course is derived from the
components. Carrying them does not make this a PNT solution: the time is still raw receiver
counters and the quality is still uninterpreted codes.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import struct
import sys
from pathlib import Path

# Septentrio driver pin these meanings come from; recorded in the output report.
REFERENCE = ("septentrio-gnss/septentrio_gnss_driver@"
             "5613af2969e3f4aac4ddc0b819e773d43a71ce9b")
SYNC = b"$@"                      # SBF_SYNC_1 = 0x24, SBF_SYNC_2 = 0x40
HEADER = "<BBHHHIH"               # sync_1, sync_2, crc, ID, length, tow, wnc (little-endian)
HEADER_SIZE = struct.calcsize(HEADER)
PVT_GEODETIC_ID = 4007
# mode, error, lat, lon, height, undulation, vn, ve, vu, cog, rx_clk_bias, rx_clk_drift,
# time_system, datum, nr_sv, wa_corr_info, reference_id, mean_corr_age, signal_info, alert_flag
BODY = "<BBdddffff fdfBBBBHHIB".replace(" ", "")
BODY_SIZE = struct.calcsize(BODY)
# Do-not-use values the pinned driver converts away: u16 65535, u32 4294967295, and the
# on-wire legacy float/double value -2e10, which it maps to NaN.
U16_NOT_AVAILABLE = 65535
U32_NOT_AVAILABLE = 4294967295
FLOAT_NOT_AVAILABLE = -2e10
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_BLOCKS = 65536
# The pinned PVTGeodetic parser defines exactly three shapes: revision 0 is the base field set,
# `revision > 0` appends `nr_bases u8, ppp_info u16` (3 bytes) and `revision > 1` appends
# `latency u16, h_accuracy u16, v_accuracy u16, misc u8` (7 more). It has no clause beyond that, so
# only 0, 1 and 2 are accepted here. A higher revision is counted as unsupported rather than decoded:
# the append order of past revisions is not evidence that a future one is compatible.
ACCEPTED_REVISIONS = {0: 0, 1: 3, 2: 10}
# A bad-CRC candidate resyncs two bytes on, so overlapping declared lengths can make the bytes
# examined far exceed the file: up to MAX_BLOCKS * 65535 from a 16 MiB input. The input cap does not
# bound that duplicated work, so an explicit aggregate budget does. Four times the input cap is
# generous for any ordinary file - a non-overlapping scan examines at most the file's own length -
# and finite for a crafted one. Exceeding it refuses; it never reports unexamined frames as bytes
# outside a block.
MAX_CANDIDATE_BYTES = 4 * MAX_INPUT
POSITION_BASIS = ("REPORTED_GEODETIC_DEGREES_CONVERTED_FROM_THE_SOURCES_STATED_RADIANS_"
                  "IN_THE_RECEIVERS_REPORTED_FRAME_NOT_A_VERIFIED_DATUM")
TIME_BASIS = ("RECEIVER_REPORTED_COUNTERS_EXACTLY_AS_READ_NO_UTC_AND_NO_LEAP_SECOND_APPLIED_"
              "AND_THE_COUNTER_UNITS_ARE_NOT_STATED_BY_THE_PINNED_SOURCE")
VELOCITY_BASIS = ("REPORTED_COMPONENTS_AND_COURSE_IN_THE_UNITS_THE_PINNED_MESSAGE_DECLARES_WHOSE_"
                  "REFERENCE_FRAME_DATUM_AND_COURSE_REFERENCE_THAT_SOURCE_DOES_NOT_STATE_SO_NOTHING_IS_"
                  "ROTATED_AND_NO_SPEED_OR_COURSE_IS_DERIVED_FROM_THEM_AND_THIS_IS_NOT_A_PNT_SOLUTION")
CODE_BASIS = "REPORTED_CODE_VALUE_TABLE_NOT_ESTABLISHED_BY_THE_PINNED_SOURCE"
CAPTURE_BASIS = "the_common_capture_clock_is_a_caller_declaration_not_the_receiver_clock"
COLUMNS = ("record_time_us", "capture_clock_basis", "sbf_block_offset", "sbf_block_length",
           "sbf_block_id", "sbf_block_revision", "sbf_block_sha256", "sbf_block_extra_bytes",
           "sbf_tow_reported", "sbf_wnc_reported", "sbf_time_basis",
           "sbf_latitude_deg", "sbf_longitude_deg", "sbf_position_disposition",
           "sbf_position_basis", "sbf_height_ellipsoidal_m", "sbf_height_disposition",
           "sbf_undulation_m", "sbf_velocity_north_m_s", "sbf_velocity_east_m_s",
           "sbf_velocity_up_m_s", "sbf_velocity_disposition", "sbf_course_over_ground_deg",
           "sbf_course_disposition", "sbf_velocity_basis",
           "sbf_mode_code", "sbf_error_code", "sbf_nr_sv_reported",
           "sbf_alert_flag_code", "sbf_time_system_code", "sbf_datum_code", "sbf_code_basis")


def crc16(data):
    """The pinned validator's CRC: seed 0, MSB-first, table from polynomial 0x1021.

    `crc.cpp` computes `crc = (crc << 8) ^ CRC_LOOK_UP[uint8_t((crc >> 8) ^ buf[i])]` with
    "Seed is 0, as suggested by the firmware". Its 256-entry table was compared entry by entry
    against a table generated from polynomial 0x1021: all 256 match, so the polynomial is
    established by comparison rather than by the name "CCITT", and the table is generated here
    instead of copying the vendor's array.
    """
    crc = 0
    for byte in data:
        index = ((crc >> 8) ^ byte) & 0xFF
        entry = index << 8
        for _ in range(8):
            entry = ((entry << 1) ^ 0x1021) & 0xFFFF if entry & 0x8000 else (entry << 1) & 0xFFFF
        crc = ((crc << 8) & 0xFFFF) ^ entry
    return crc


def float_value(value):
    """A reported float, or None when the source said not available.

    The pinned driver maps the on-wire -2e10 and non-finite values onto a not-available NaN, so
    neither becomes a measurement here and neither becomes zero.
    """
    if not math.isfinite(value) or value == FLOAT_NOT_AVAILABLE:
        return None
    return value


def blocks(raw):
    """Candidates in file order plus the bytes examined: (candidates, examined).

    Each candidate is `(offset, header fields, retained bytes, reason)`.

    A candidate starts at the sync pair. `reason` is None when the block is framed and its CRC
    matches the pinned rule - CRC-16 over bytes 4 to length-1, compared with the header value.
    A candidate that fails is returned with its reason so the caller can count and retain it
    rather than silently accept or silently drop it.
    """
    found, offset, total, examined = [], 0, len(raw), 0

    def spend(count):
        """Charge the aggregate budget before any slice, digest or CRC pass over those bytes."""
        nonlocal examined
        examined += count
        if examined > MAX_CANDIDATE_BYTES:
            raise ValueError("saved SBF input exceeds the candidate byte budget")

    while True:
        offset = raw.find(SYNC, offset)
        if offset < 0:
            return found, examined
        if len(found) >= MAX_BLOCKS:
            # Refused rather than returned short: stopping here would emit rows for the first
            # MAX_BLOCKS candidates and label the unexamined remainder as bytes outside any block,
            # which would report required information as absent.
            raise ValueError("saved SBF input exceeds the candidate block bound")
        if offset + HEADER_SIZE > total:
            # Retain the bytes that are actually there, not an empty placeholder.
            spend(total - offset)
            found.append((offset, None, raw[offset:total], "TRUNCATED_HEADER"))
            return found, examined
        _, _, crc, identifier, length, tow, wnc = struct.unpack_from(HEADER, raw, offset)
        header = dict(crc=crc, id=identifier & 8191, revision=identifier >> 13,
                      length=length, tow=tow, wnc=wnc)
        if length < HEADER_SIZE:
            spend(HEADER_SIZE)
            found.append((offset, header, raw[offset:offset + HEADER_SIZE], "LENGTH_BELOW_HEADER"))
            offset += 2
            continue
        if offset + length > total:
            # The declared length runs past the file: keep the bounded span that does exist, so the
            # digest binds real bytes instead of hashing nothing.
            spend(total - offset)
            found.append((offset, header, raw[offset:total], "TRUNCATED_BLOCK"))
            return found, examined
        # Charged before the slice and the CRC pass, which are the expensive part.
        spend(length)
        block = raw[offset:offset + length]
        if crc16(block[4:length]) != crc:
            found.append((offset, header, block, "CRC_MISMATCH"))
            offset += 2
            continue
        found.append((offset, header, block, None))
        offset += length


def convert(raw, capture_time_us):
    """Selected PVTGeodetic fields of validated blocks; never a receiver or a fix guarantee."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit caller capture microseconds required")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized saved SBF input")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    report = dict(reference=REFERENCE, source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                  source_bytes=len(raw), blocks_scanned=0, decoded_blocks=0,
                  unselected_blocks=0, invalid_blocks=0, unsupported_revision_blocks=0,
                  unselected=[], invalid=[], unsupported_revision=[],
                  revision_appended_fields_present=0,
                  # Byte-loss accounting: how much of the file the framed blocks cover and how much
                  # lies outside any block, so a gap is visible instead of silently ignored.
                  bytes_in_blocks=0, bytes_outside_blocks=len(raw),
                  overlapping_candidates=0, candidate_bytes_examined=0,
                  candidate_byte_budget=MAX_CANDIDATE_BYTES, clock="Unknown",
                  capture_clock_basis=CAPTURE_BASIS, position_basis=POSITION_BASIS,
                  time_basis=TIME_BASIS, code_basis=CODE_BASIS, velocity_basis=VELOCITY_BASIS,
                  height_basis="ELLIPSOIDAL_AS_THE_PINNED_MESSAGE_STATES_NEVER_MEAN_SEA_LEVEL")
    covered, reach = 0, 0
    candidates, examined = blocks(raw)
    report["candidate_bytes_examined"] = examined
    for offset, header, block, reason in candidates:
        report["blocks_scanned"] += 1
        # Union coverage: a resynced candidate may start inside an earlier one, so only the bytes
        # past the furthest point already counted are added. Each candidate keeps its own offset,
        # length and digest, so the resync evidence is preserved and no byte is counted twice.
        end = offset + len(block)
        if offset < reach:
            report["overlapping_candidates"] += 1
        covered += max(0, end - max(offset, reach))
        reach = max(reach, end)
        digest = "sha256:" + hashlib.sha256(block).hexdigest()
        if reason is not None:
            # Retained by offset and digest, counted, and never treated as decoded.
            report["invalid_blocks"] += 1
            report["invalid"].append(dict(offset=offset, reason=reason, sha256=digest,
                                          retained_bytes=len(block),
                                          declared_length=None if header is None else header["length"],
                                          declared_id=None if header is None else header["id"]))
            continue
        if header["id"] != PVT_GEODETIC_ID:
            report["unselected_blocks"] += 1
            report["unselected"].append(dict(offset=offset, id=header["id"],
                                             revision=header["revision"],
                                             length=header["length"], sha256=digest))
            continue
        if header["revision"] not in ACCEPTED_REVISIONS:
            # Not decoded: the pinned source defines no layout clause for this revision.
            report["unsupported_revision_blocks"] += 1
            report["unsupported_revision"].append(dict(offset=offset, id=header["id"],
                                                      revision=header["revision"],
                                                      length=header["length"], sha256=digest))
            continue
        body = block[HEADER_SIZE:]
        required = BODY_SIZE + ACCEPTED_REVISIONS[header["revision"]]
        if len(body) < required:
            report["invalid_blocks"] += 1
            report["invalid"].append(dict(offset=offset,
                                          reason="BODY_SHORTER_THAN_THIS_REVISIONS_FIELDS",
                                          sha256=digest, declared_length=header["length"],
                                          declared_id=header["id"], declared_revision=header["revision"],
                                          required_body_bytes=required, body_bytes=len(body)))
            continue
        (mode, error, latitude, longitude, height, undulation, north, east, up, course,
         _bias, _drift, time_system, datum, nr_sv, _corr, _reference, _age, _signal,
         alert) = struct.unpack_from(BODY, body)
        extra = len(body) - BODY_SIZE
        if extra:
            # Revision-appended bytes exist; their meanings past the append order are not
            # established by the pinned files, so the count is reported and nothing is decoded.
            report["revision_appended_fields_present"] += 1
        radians = [float_value(latitude), float_value(longitude)]
        metres = float_value(height)
        # The three components come from one solution, so they share a disposition exactly as the
        # position pair already does: it reports whether the solution was fully available, and it
        # does not gate the values. Every component the source reported is carried either way, and
        # only an unavailable one is blank. The course is dispositioned separately: the pinned source
        # gives no rule tying the two, so an unavailable course is not read as unavailable motion
        # and neither value is ever derived from the other.
        velocity = [float_value(north), float_value(east), float_value(up)]
        bearing = float_value(course)
        writer.writerow({
            "record_time_us": capture_time_us, "capture_clock_basis": CAPTURE_BASIS,
            "sbf_block_offset": offset, "sbf_block_length": header["length"],
            "sbf_block_id": header["id"], "sbf_block_revision": header["revision"],
            "sbf_block_sha256": digest, "sbf_block_extra_bytes": extra,
            "sbf_tow_reported": None if header["tow"] == U32_NOT_AVAILABLE else header["tow"],
            "sbf_wnc_reported": None if header["wnc"] == U16_NOT_AVAILABLE else header["wnc"],
            "sbf_time_basis": TIME_BASIS,
            "sbf_latitude_deg": None if radians[0] is None else math.degrees(radians[0]),
            "sbf_longitude_deg": None if radians[1] is None else math.degrees(radians[1]),
            "sbf_position_disposition": ("NOT_AVAILABLE_SENTINEL" if None in radians
                                         else "REPORTED_NOT_VERIFIED"),
            "sbf_position_basis": POSITION_BASIS,
            "sbf_height_ellipsoidal_m": metres,
            "sbf_height_disposition": ("NOT_AVAILABLE_SENTINEL" if metres is None
                                       else "REPORTED_ELLIPSOIDAL_NOT_MEAN_SEA_LEVEL"),
            "sbf_undulation_m": float_value(undulation),
            "sbf_velocity_north_m_s": velocity[0], "sbf_velocity_east_m_s": velocity[1],
            "sbf_velocity_up_m_s": velocity[2],
            "sbf_velocity_disposition": ("NOT_AVAILABLE_SENTINEL" if None in velocity
                                         else "REPORTED_NOT_VERIFIED"),
            "sbf_course_over_ground_deg": bearing,
            "sbf_course_disposition": ("NOT_AVAILABLE_SENTINEL" if bearing is None
                                       else "REPORTED_NOT_VERIFIED"),
            "sbf_velocity_basis": VELOCITY_BASIS,
            "sbf_mode_code": mode, "sbf_error_code": error,
            "sbf_nr_sv_reported": None if nr_sv == 255 else nr_sv,
            "sbf_alert_flag_code": alert, "sbf_time_system_code": time_system,
            "sbf_datum_code": datum, "sbf_code_basis": CODE_BASIS})
        report["decoded_blocks"] += 1
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted SBF output exceeds bound")
    report["bytes_in_blocks"] = covered
    report["bytes_outside_blocks"] = len(raw) - covered
    if not 0 <= covered <= len(raw):
        raise ValueError("SBF source byte accounting mismatch")
    if not report["decoded_blocks"]:
        raise ValueError("no validated PVTGeodetic block in the saved input")
    if report["blocks_scanned"] != (report["decoded_blocks"] + report["unselected_blocks"]
                                   + report["invalid_blocks"]
                                   + report["unsupported_revision_blocks"]):
        raise ValueError("SBF block accounting mismatch")
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved SBF file already on disk")
    parser.add_argument("output_directory", type=Path, help="new directory for the CSV and report")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller declared capture microseconds; not the receiver clock")
    args = parser.parse_args(argv)
    # Bounded read: one byte past the limit is enough to refuse without loading a large file.
    with args.input.open("rb") as handle:
        raw = handle.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized saved SBF input")
    converted, report = convert(raw, args.capture_time_us)
    args.output_directory.mkdir()
    (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
    (args.output_directory / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
