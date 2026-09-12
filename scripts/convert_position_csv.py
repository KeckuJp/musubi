#!/usr/bin/env python3
"""Offline recorded-position CSV to the decoded JSONL shape the existing reader already accepts.

Every mapping is declared in a closed profile. Nothing is inferred from a filename, a header's
apparent units or a value's range, because a column called `Alt` in decimetres and one in metres
are indistinguishable by inspection. No decoder, no network, no command to a vehicle, no wall
time, no fix policy and no change to the reader, adapter SPI or core. Satellite/fix columns are
retained as source text; they do not become a validated-position or domain claim here.
"""
import csv
from io import StringIO
import json
import math
from pathlib import Path
import re
import sys

MAX_INPUT = 16 * 1024 * 1024
MAX_LINE = 64 * 1024
# The downstream SPI parses and reserializes numbers before checking its 64 KiB
# bound. Reserve room for its f64 spelling; do not emit a boundary-sized success
# which that existing consumer cannot accept.
MAX_OUTPUT_LINE = MAX_LINE - 1024
MAX_PROFILE = 4096
U64_MAX = (1 << 64) - 1
PROFILE_KEYS = ("message_type", "counter_field", "counter_unit", "latitude_field",
                "longitude_field", "angle_unit", "altitude_field", "altitude_unit",
                "altitude_reference")
COUNTER_SCALE = {"us": 1, "ms": 1000}
ANGLE_DIVISOR = {"deg": 1, "degE7": 10000000}
ALTITUDE_DIVISOR = {"m": 1, "dm": 10, "mm": 1000}
# ASCII-only grammars: `\d` would accept other Unicode digit scripts, and `float` would accept
# `1_0`, `nan` and `inf`. A counter is unsigned decimal digits only -- no sign, point or exponent.
UNSIGNED = re.compile(r"[0-9]+")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FAILURE = "position csv conversion failed; verify input, profile and a new output path"
USAGE = "usage: convert_position_csv.py INPUT PROFILE NEWOUTPUT"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def invalid_constant(_):
    raise ValueError("nonfinite number")


def checked_profile(profile):
    """Copy the nine declared strings. Unknown, missing, empty and wrong-typed keys are errors."""
    if not isinstance(profile, dict) or set(profile) != set(PROFILE_KEYS):
        raise ValueError("invalid profile")
    declared = {}
    for key in PROFILE_KEYS:
        value = profile[key]
        if type(value) is not str or not value or len(value) > 256 or CONTROL.search(value):
            raise ValueError("invalid profile")
        declared[key] = value
    # `message_type` becomes `meta.type`, which the adapter SPI reads as an identifier token,
    # and `;` is that layer's top-level separator. Column names are only matched here.
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", declared["message_type"]):
        raise ValueError("invalid profile")
    if (declared["counter_unit"] not in COUNTER_SCALE
            or declared["angle_unit"] not in ANGLE_DIVISOR
            or declared["altitude_unit"] not in ALTITUDE_DIVISOR
            # Ellipsoidal, local-origin and home-relative heights are not metres above mean sea
            # level, and this converter has no datum to convert them with.
            or declared["altitude_reference"] != "msl"):
        raise ValueError("invalid profile")
    if len(json.dumps(declared).encode()) > MAX_PROFILE:
        raise ValueError("profile limit")
    return declared


def counter_us(text, scale):
    token = text.strip()
    if not UNSIGNED.fullmatch(token):
        raise ValueError("invalid counter")
    value = int(token) * scale
    if value > U64_MAX:
        raise ValueError("counter overflow")
    return value


def number(text):
    token = text.strip()
    if not NUMBER.fullmatch(token):
        raise ValueError("invalid numeric value")
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("nonfinite numeric value")
    return value


def read_table(raw):
    """Decode the whole input first; a row is never emitted from a partially validated file."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("invalid input")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("input limit")
    for line in bytes(raw).split(b"\n"):
        if len(line) > MAX_LINE:
            raise ValueError("line limit")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("invalid encoding") from error
    if "\x00" in text:
        raise ValueError("invalid encoding")
    # csv.reader(strict=True) still accepts quotes inside unquoted fields.
    # Enforce the standard quote boundary first, including doubled quotes and
    # embedded newlines in quoted fields; do not repair malformed source text.
    state = "start"
    for char in text:
        if state == "quoted":
            if char == '"':
                state = "after_quote"
        elif state == "after_quote":
            if char == '"':
                state = "quoted"
            elif char in ",\r\n":
                state = "start"
            else:
                raise ValueError("invalid csv quote")
        elif char == '"':
            if state != "start":
                raise ValueError("invalid csv quote")
            state = "quoted"
        elif char in ",\r\n":
            state = "start"
        else:
            state = "unquoted"
    if state == "quoted":
        raise ValueError("unterminated csv quote")
    try:
        # newline="" leaves line endings to the CSV reader; strict=True refuses stray and
        # unterminated quotes instead of silently reading to end of data.
        rows = list(csv.reader(StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError("invalid csv") from error
    if len(rows) < 2:
        raise ValueError("no data rows")
    header = rows[0]
    names = [name.strip() for name in header]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("blank or duplicate column name")
    for row in rows[1:]:
        if not row:
            raise ValueError("blank row")
        if len(row) != len(header):
            raise ValueError("ragged row")
    return header, names, rows[1:]


def convert(raw, profile):
    """Return UTF-8 JSONL, one record per data row in source order.

    The caller must independently establish that the declared columns mean what the profile says:
    that the counter is a counter rather than an exporter wall clock, that the coordinates are
    geodetic degrees in the declared scaling, and that the altitude is above mean sea level.
    Successful conversion cannot detect a wrong declaration. Every original column survives as its
    parsed lexical string under `data.source_record`; that sidecar is not sealed downstream.
    """
    declared = checked_profile(profile)
    header, names, rows = read_table(raw)
    selected = {}
    for key in ("counter_field", "latitude_field", "longitude_field", "altitude_field"):
        # Case-sensitive, whole-name matching: no unit suffix or dotted prefix is stripped for you.
        if declared[key] not in names:
            raise ValueError("declared column absent")
        selected[key] = names.index(declared[key])
    scale = COUNTER_SCALE[declared["counter_unit"]]
    angle = ANGLE_DIVISOR[declared["angle_unit"]]
    altitude = ALTITUDE_DIVISOR[declared["altitude_unit"]]
    output, size = [], 0
    for row in rows:
        latitude = number(row[selected["latitude_field"]]) / angle
        longitude = number(row[selected["longitude_field"]]) / angle
        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            raise ValueError("position out of range")
        metres = number(row[selected["altitude_field"]]) / altitude
        if not math.isfinite(metres):
            raise ValueError("nonfinite altitude")
        record = {
            "meta": {"type": declared["message_type"]},
            "data": {
                "TimeUS": counter_us(row[selected["counter_field"]], scale),
                "lat_deg": latitude,
                "lon_deg": longitude,
                "alt_m": metres,
                # Original header spelling and original cell text, including the exporter
                # timestamp this converter deliberately ignores. Order follows the source.
                "source_record": dict(zip(header, row)),
            },
        }
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode()
        # Keep every generated record inside the existing reader/adapter bounds rather than
        # discovering at ingest that a preserved source row made the line unreadable.
        if len(encoded) > MAX_OUTPUT_LINE:
            raise ValueError("output line limit")
        size += len(encoded) + 1
        if size > MAX_INPUT:
            raise ValueError("output limit")
        output.append(encoded)
    return b"\n".join(output) + b"\n"


def bounded_read(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file limit")
    return raw


def main(argv):
    if len(argv) != 4:
        print(USAGE, file=sys.stderr)
        return 1
    try:
        profile = json.loads(bounded_read(Path(argv[2]), MAX_PROFILE).decode("utf-8"),
                             object_pairs_hook=unique_object, parse_constant=invalid_constant)
        result = convert(bounded_read(Path(argv[1]), MAX_INPUT), profile)
        # "xb" is the refusal: an existing output is never opened for writing, and nothing is
        # created until the complete input and profile have been validated above.
        with Path(argv[3]).open("xb") as stream:
            stream.write(result)
    except (OSError, ValueError, TypeError, RecursionError):
        # Fixed text: a diagnostic carrying a path, a cell value or a traceback would copy
        # source material into logs the operator did not choose to expose.
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
