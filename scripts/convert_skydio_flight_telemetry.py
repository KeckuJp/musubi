#!/usr/bin/env python3
"""Saved Skydio Cloud v1 flight-telemetry response body -> common Observation CSV.

Offline and passive: a response body already on disk is read. No client, no API token, no network,
no account and no device. The documented retrieval is token-gated; that gate is outside this path
entirely, exactly as the other adopted saved-API-body routes work.

Source: Skydio Cloud API v1 `GET /v1/flight/{flight_id}/telemetry`, documented at
`apidocs.skydio.com/reference/telemetry_get_v1_flight_flight_id_telemetry.md`. The exact page content
this path was written against is pinned by digest in the private task evidence (page `updatedAt`
2026-05-27, fetched 2026-09-22, sha256
2f3153e71afb5b8925932773e143e636b404370654fef6c98e80209589a6742d).
The body carries no version field, so the operator declares the source format: a caller precondition,
never in-band authentication, and any other string is refused.

Content producer versus delivery source, kept distinct: the samples are the AIRCRAFT's reported
telemetry -- the page calls the sample stamp "Vehicle epoch time" -- and Skydio Cloud is the delivery
source of the saved body. Nothing here claims the application authored the measurements.

Units and axis orders come only from that page, and from all of it: metres for the altitudes, the
height above takeoff and the Site-frame positions; metres per second for the GPS velocity and the
speed accuracy; degrees for latitude, longitude and the orientation triples; metres for the XY and Z
accuracy estimates; a 0-to-1 fraction for the battery. The page names the axis order in the field
descriptions -- GPS velocity is north-east-down, a Site position is east-north-up -- so the component
columns carry those names rather than x/y/z. The satellite count is a count with no unit. Nothing is
scaled, rotated or given a datum, and no unit the page does not state is invented.

Fields this path does not interpret -- the declared quaternion arrays included -- are NOT dropped.
They are retained in `source_record_hex`, the adopted per-row retention column, and everything around
the samples (the envelope's keys, the `data` siblings, the flight object, the `flight_telemetry`
siblings) is retained the same way once, on the first row, in `skydio_source_context_hex`. A digest
binds bytes; it does not preserve values, so both are carried.

Numbers are retained as the exact lexemes the producer wrote. `json.loads` rounds every numeric
literal to a double, so re-encoding a parsed value is not exact; the parse keeps each lexeme beside
its double instead. The interpreted columns carry the same lexeme, but the common model reads them as
doubles -- so an interpreted quantity is double-precision, and no equality with the retained lexeme is
claimed.

`user_email` is personal data and is never emitted: it is removed from the retained text as well and
counted as a redacted key with a fixed reason, following the adopted ExpressLRS redaction rule. That
is one known key, not a broad privacy sanitisation, and nothing here claims otherwise.
"""
import argparse
import csv
from datetime import date
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys

SOURCE_FORMAT = "skydio-cloud-v1-flight-telemetry"
SOURCE_FORMAT_BASIS = "CALLER_DECLARED_PRECONDITION_THE_SAVED_BODY_CARRIES_NO_VERSION_FIELD_NOT_AUTHENTICATION"
REDACTION_REASON = "PERSONAL_DATA_KEY_NOT_EMITTED"
# The one personal-data key the pinned page documents, removed wherever it appears at the top level
# of the flight object or of a sample. One known key -- NOT a general privacy filter.
PERSONAL_KEYS = ("user_email",)
RETAINED_STATUS = "RETAINED_VALUE_EXACT_IN_source_record_hex_NOT_INTERPRETED_HERE"
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_SAMPLES = 200000
# Retention is bounded: an object larger than this is refused, never trimmed to fit.
MAX_RETAINED = 256 * 1024
EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
# Scalars the page gives a unit for, and the column that carries it.
UNIT_SCALARS = (("gps_altitude", "skydio_gps_altitude_m"),
                ("height_above_takeoff", "skydio_height_above_takeoff_m"),
                ("hybrid_altitude", "skydio_hybrid_altitude_m"),
                ("gps_latitude", "skydio_gps_latitude_deg"),
                ("gps_longitude", "skydio_gps_longitude_deg"),
                ("gps_horizontal_accuracy", "skydio_gps_horizontal_accuracy_m"),
                ("gps_vertical_accuracy", "skydio_gps_vertical_accuracy_m"),
                ("gps_speed_accuracy", "skydio_gps_speed_accuracy_m_s"))
# Fixed-length arrays the page gives a unit for, with the axis order it states in the description.
UNIT_VECTORS = (("gps_velocity", "skydio_gps_velocity_ned_m_s", ("north", "east", "down")),
                ("site_position_enu", "skydio_site_position_enu_m", ("east", "north", "up")),
                ("site_camera_position_enu", "skydio_site_camera_position_enu_m",
                 ("east", "north", "up")),
                ("site_orientation_rpy_enu", "skydio_site_orientation_rpy_enu_deg",
                 ("roll", "pitch", "yaw")),
                ("site_camera_orientation_rpy_enu", "skydio_site_camera_orientation_rpy_enu_deg",
                 ("roll", "pitch", "yaw")),
                ("hybrid_camera_orientation_rpy_ned", "skydio_hybrid_camera_orientation_rpy_ned_deg",
                 ("roll", "pitch", "yaw")))
# The page declares the battery as "a float from 0-1", so it is a declared fraction -- not a number
# out of a hundred. It is carried as reported: nothing is multiplied, divided or clamped, and a value
# outside the declared range is counted rather than silently accepted as conforming.
BATTERY_KEY = "battery_percentage"
BATTERY_COLUMN = "skydio_battery_remaining_fraction"
# A count, which the page states without a unit.
COUNT_KEY = "gps_num_satellites_used"
COUNT_COLUMN = "skydio_gps_satellites_used_reported"
# Declared by the page as a four-component quaternion [x, y, z, w] in the Site frame, and given no
# interpreted column here: this path asserts no rotation convention, so the values are retained.
QUATERNION_KEYS = ("site_orientation_quat_enu", "site_camera_orientation_quat_enu")
CLOCK_BASIS = ("PRODUCER_REPORTED_SAMPLE_TIME_THE_PAGE_CALLS_IT_VEHICLE_EPOCH_TIME_IN_MICROSECONDS_WHILE_"
               "TYPING_IT_AS_A_DATE_TIME_STRING_SO_THE_STRING_IS_READ_OFFSET_AWARE_BY_EXACT_INTEGER_EPOCH_"
               "ARITHMETIC_TO_THAT_DECLARED_MICROSECOND_RESOLUTION_AND_A_FINER_FRACTION_IS_REFUSED_RATHER_"
               "THAN_TRUNCATED_IT_IS_THE_VEHICLES_REPORTED_CLOCK_AND_NOT_AN_ATTESTED_SYNCHRONISED_OR_"
               "VERIFIED_CLOCK_AND_NOT_A_CAPTURE_TIME")
UNIT_BASIS = ("UNITS_AND_AXIS_ORDERS_ARE_TAKEN_ONLY_WHERE_THE_PINNED_ENDPOINT_PAGE_DECLARES_THEM_METRES_FOR_"
              "THE_ALTITUDES_THE_HEIGHT_ABOVE_TAKEOFF_THE_SITE_POSITIONS_AND_THE_XY_AND_Z_ACCURACY_ESTIMATES_"
              "METRES_PER_SECOND_FOR_THE_GPS_VELOCITY_AND_THE_SPEED_ACCURACY_DEGREES_FOR_LATITUDE_LONGITUDE_"
              "AND_THE_ORIENTATION_TRIPLES_AND_A_ZERO_TO_ONE_FRACTION_FOR_THE_BATTERY_THE_SATELLITE_COUNT_IS_"
              "A_COUNT_WITH_NO_UNIT_AND_NOTHING_IS_SCALED_CLAMPED_OR_GIVEN_A_UNIT_THE_PAGE_DOES_NOT_STATE")
FRAME_BASIS = ("THE_FRAME_NAMES_AND_COMPONENT_ORDERS_COME_FROM_THE_PAGES_OWN_FIELD_DESCRIPTIONS_GPS_VELOCITY_"
               "NORTH_EAST_DOWN_AND_THE_SITE_POSITIONS_EAST_NORTH_UP_NOTHING_IS_ROTATED_BETWEEN_FRAMES_HERE_"
               "AND_THE_SITE_ORIGIN_IS_NOT_IN_THIS_BODY_SO_A_SITE_POSITION_IS_AN_OFFSET_AND_NEVER_GEODETIC_"
               "LATITUDE_AND_LONGITUDE_ARE_DEGREES_AS_DECLARED_WITH_NO_DATUM_STATED_BY_THE_PAGE_OR_ASSUMED_HERE")
IDENTITY_BASIS = ("REPORTED_FLIGHT_AND_VEHICLE_IDENTIFIERS_WHICH_THE_PAGE_REQUIRES_AND_THE_BATTERY_SERIAL_"
                  "WHICH_IT_DOES_NOT_SO_THAT_COLUMN_IS_BLANK_AND_COUNTED_WHEN_ABSENT_ALL_AS_THE_SAVED_BODY_"
                  "WROTE_THEM_NEITHER_AUTHENTICATED_NOR_PHYSICALLY_VERIFIED_AND_THE_OPERATOR_EMAIL_IS_NEVER_"
                  "EMITTED")
PRODUCER_BASIS = ("THE_SAMPLES_ARE_THE_AIRCRAFTS_REPORTED_TELEMETRY_AND_SKYDIO_CLOUD_IS_THE_DELIVERY_SOURCE_"
                  "OF_THIS_SAVED_BODY_SO_THE_APPLICATION_IS_NOT_CLAIMED_TO_HAVE_AUTHORED_THE_MEASUREMENTS_"
                  "AND_THE_PAGE_DECLARES_NO_PAGINATION_OR_RECORD_COUNT_SO_NO_COMPLETENESS_IS_ASSERTED")
RETENTION_BASIS = ("EVERY_KEY_OF_THE_SAMPLE_INCLUDING_THE_DECLARED_QUATERNION_ARRAYS_AND_ANY_KEY_THIS_PATH_"
                   "DOES_NOT_INTERPRET_IS_RETAINED_AS_CANONICAL_JSON_IN_source_record_hex_AND_THE_FIRST_ROWS_"
                   "skydio_source_context_hex_RETAINS_THE_ENVELOPE_KEYS_THE_data_SIBLINGS_THE_FLIGHT_OBJECT_"
                   "AND_THE_flight_telemetry_SIBLINGS_THE_SAME_WAY_SO_NOTHING_AROUND_THE_SAMPLES_IS_DROPPED_"
                   "EVERY_NUMBER_IS_WRITTEN_AS_THE_EXACT_LEXEME_THE_PRODUCER_SENT_BECAUSE_A_JSON_PARSE_ROUNDS_"
                   "A_LITERAL_TO_A_DOUBLE_AND_A_NUMBER_WITH_NO_EXACT_JSON_FORM_SUCH_AS_A_NAN_OR_INFINITY_"
                   "LITERAL_IS_REFUSED_RATHER_THAN_RETAINED_AS_AN_APPROXIMATION_THE_INTERPRETED_COLUMNS_CARRY_"
                   "THE_SAME_LEXEME_BUT_THE_COMMON_MODEL_PARSES_THEM_AS_DOUBLES_SO_AN_INTERPRETED_QUANTITY_IS_"
                   "DOUBLE_PRECISION_AND_NO_EQUALITY_BETWEEN_IT_AND_THE_RETAINED_LEXEME_IS_CLAIMED_THE_"
                   "RETAINED_TEXT_IS_A_KEY_SORTED_REENCODING_AND_NOT_A_BYTE_COPY_OF_THE_INPUT_WHICH_IS_WHAT_"
                   "source_document_sha256_PINS_THE_ONLY_REMOVAL_IS_THE_KNOWN_user_email_KEY_AT_THE_TOP_LEVEL_"
                   "OF_A_SAMPLE_OR_OF_A_RETAINED_CONTEXT_SECTION_NOTHING_NESTED_IS_SCANNED_SO_NO_BROAD_"
                   "PRIVACY_SANITISATION_IS_PERFORMED_OR_CLAIMED_AND_ANY_OTHER_PERSONAL_DATA_THE_PRODUCER_"
                   "WROTE_WOULD_BE_RETAINED")
ENVELOPE_BASIS = ("THE_PAGE_PUTS_flight_AND_flight_telemetry_INSIDE_THE_data_OBJECT_OF_AN_ENVELOPE_THAT_ALSO_"
                  "CARRIES_status_code_skydio_error_code_meta_AND_A_NULLABLE_error_message_SO_THAT_SHAPE_IS_"
                  "WHAT_IS_READ_A_BODY_CARRYING_AN_ERROR_MESSAGE_OR_A_STATUS_CODE_OTHER_THAN_200_IS_REFUSED_"
                  "RATHER_THAN_TREATED_AS_TELEMETRY_THE_REPORTED_skydio_error_code_AND_meta_TRAVEL_INTO_THE_"
                  "REPORT_UNINTERPRETED_BECAUSE_THE_PAGE_DEFINES_NEITHER_A_SUCCESS_VALUE_NOR_A_UNIT_FOR_"
                  "meta_time_AND_THE_PAGES_OWN_required_LIST_FOR_data_NAMES_A_metadata_PROPERTY_IT_NEVER_"
                  "DEFINES_WHICH_IS_RECORDED_AS_A_PAGE_INCONSISTENCY_AND_NEITHER_REQUIRED_NOR_INVENTED_HERE")
RANGE_BASIS = ("THE_PAGE_DECLARES_THE_BATTERY_AS_A_FLOAT_FROM_0_TO_1_SO_A_VALUE_OUTSIDE_THAT_RANGE_IS_"
               "CARRIED_AS_REPORTED_AND_COUNTED_AS_A_DECLARED_RANGE_VIOLATION_IT_IS_NEITHER_RESCALED_NOR_"
               "CLAMPED_NOR_ACCEPTED_AS_CONFORMING")
UNDECLARED_BASIS = ("THE_PAGE_SETS_additionalProperties_FALSE_ON_A_SAMPLE_SO_A_KEY_IT_DOES_NOT_DECLARE_MEANS_"
                    "A_NEWER_OR_NONCONFORMING_BODY_SUCH_A_KEY_IS_RETAINED_AND_COUNTED_SEPARATELY_FROM_THE_"
                    "DECLARED_BUT_UNINTERPRETED_ONES_AND_IS_NEVER_SILENTLY_TREATED_AS_DECLARED")
FIELDS = (["record_time_us", "skydio_sample_index", "skydio_sample_count"]
          + [column for _, column in UNIT_SCALARS]
          + [f"{stem}_{part}" for _, stem, parts in UNIT_VECTORS for part in parts]
          + [BATTERY_COLUMN, COUNT_COLUMN]
          + ["skydio_flight_id_hex", "skydio_vehicle_serial_hex", "skydio_battery_serial_hex",
             "source_record_hex", "skydio_declared_uninterpreted_key_count",
             "skydio_undeclared_key_count", "skydio_source_context_hex",
             "skydio_clock_basis", "skydio_unit_basis", "skydio_frame_basis",
             "skydio_identity_basis", "skydio_producer_basis", "skydio_retention_basis",
             "skydio_envelope_basis", "source_document_sha256"])
# The timestamp is decomposed here rather than handed to `fromisoformat`, whose fractional handling is
# an interpreter-version behaviour rather than a stated policy.
DATE_TIME = re.compile(r"(?P<year>\d{4})-(?P<month>\d\d)-(?P<day>\d\d)T"
                       r"(?P<hour>\d\d):(?P<minute>\d\d):(?P<second>\d\d)"
                       r"(?:\.(?P<fraction>\d+))?"
                       r"(?:Z|(?P<sign>[+-])(?P<offset_hour>\d\d):(?P<offset_minute>\d\d))")
# Keys this path gives an interpreted column.
INTERPRETED_KEYS = frozenset(["timestamp", BATTERY_KEY, COUNT_KEY]
                             + [key for key, _ in UNIT_SCALARS]
                             + [key for key, _, _ in UNIT_VECTORS])
# Every sample key the pinned page declares. Anything else is undeclared, not merely uninterpreted.
DECLARED_SAMPLE_KEYS = INTERPRETED_KEYS | frozenset(QUATERNION_KEYS)
# The keys the page declares around the samples, per section of the response. `data.metadata` is in
# the page's own `required` list without ever being defined in its properties, so a body carrying it
# is expected-but-undefined rather than undeclared -- ENVELOPE_BASIS records that inconsistency.
DECLARED_CONTEXT_KEYS = {
    "envelope": frozenset(("data", "error_message", "meta", "skydio_error_code", "status_code")),
    "data": frozenset(("flight", "flight_telemetry", "metadata")),
    "flight": frozenset(("attachments", "battery_serial", "flight_id", "has_telemetry", "landing",
                         "markers", "sensor_package", "takeoff", "takeoff_latitude",
                         "takeoff_longitude", "user_email", "vehicle_serial")),
    "flight_telemetry": frozenset(("aligned_telemetry",)),
}


def unique_object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("saved body repeats a JSON key")
        seen[key] = value
    return seen


def text_hex(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"reported {name} must be nonempty text")
    return "hex:" + value.encode("utf-8").hex()


class SourceNumber(float):
    """A JSON number that keeps the exact lexeme the producer wrote.

    `json.loads` turns every numeric literal into a double, so re-encoding a parsed value cannot be
    value-exact: `0.12345678901234567890123456789` comes back as `0.12345678901234568`. Parsing
    through this class keeps the source digits beside the double, so retention can write what the
    producer wrote while the interpreted columns still have a number to check.
    """

    __slots__ = ("lexeme",)

    def __new__(cls, lexeme):
        value = super().__new__(cls, lexeme)
        value.lexeme = lexeme
        return value


def canonical_json(value):
    """Canonical JSON text of a parsed body, with every number written as its source lexeme.

    Deliberately small and local: the retention column and its discipline are the adopted pattern,
    and this is only the encoder that keeps it honest. A float that arrived without a lexeme -- a
    `NaN` or `Infinity` literal, which `json.loads` accepts by default -- has no exact JSON form, so
    it is REFUSED here rather than written as an approximation.
    """
    if isinstance(value, SourceNumber):
        return value.lexeme
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise ValueError("saved body carries a number with no exact JSON form, so it is refused "
                         "rather than retained as an approximation")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(item)}"
                              for key, item in sorted(value.items())) + "}"
    raise ValueError("saved body carries a value this path cannot retain exactly")


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"reported {name} must be a finite number")
    return value


def cell(value):
    """The number as the source wrote it, so the CSV text keeps the producer's own digits."""
    return value.lexeme if isinstance(value, SourceNumber) else value


def sample_time_us(value):
    """The producer's own date-time string to exact microseconds, by integer arithmetic only.

    No float is used: `timestamp()` returns a float second, so multiplying it by a million is not
    exact at arbitrary magnitudes, and `fromisoformat` handles a fractional part by interpreter
    version rather than by policy. So the string is decomposed here and the epoch computed from the
    proleptic ordinal in integers.

    Fractional precision policy, explicit and matching the page's own "[microseconds]" description:
    microseconds are the supported resolution. One to six fractional digits are read exactly. More
    digits are accepted only when every digit past the sixth is zero -- i.e. the extra precision is
    representable -- and are otherwise REFUSED by name rather than silently rounded or truncated.
    """
    if not isinstance(value, str):
        raise ValueError("each telemetry sample requires the declared date-time timestamp")
    parsed = DATE_TIME.fullmatch(value)
    if parsed is None:
        raise ValueError("each telemetry sample requires the declared date-time timestamp")
    year, month, day, hour, minute, second = (int(parsed[name]) for name in
                                              ("year", "month", "day", "hour", "minute", "second"))
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("telemetry timestamp outside the representable range")
    fraction = parsed["fraction"] or ""
    if len(fraction) > 6 and fraction[6:].strip("0"):
        raise ValueError("telemetry timestamp carries sub-microsecond precision this path cannot "
                         "represent; it is refused rather than truncated")
    micros = int((fraction[:6] + "000000")[:6]) if fraction else 0
    try:
        days = date(year, month, day).toordinal() - EPOCH_ORDINAL
    except ValueError as exc:
        raise ValueError("telemetry timestamp carries an impossible calendar date") from exc
    offset = 0
    if parsed["sign"]:
        offset_hour, offset_minute = int(parsed["offset_hour"]), int(parsed["offset_minute"])
        if offset_hour > 23 or offset_minute > 59:
            raise ValueError("telemetry timestamp offset outside the representable range")
        offset = (offset_hour * 3600 + offset_minute * 60) * (-1 if parsed["sign"] == "-" else 1)
    seconds = days * 86400 + hour * 3600 + minute * 60 + second - offset
    microseconds = seconds * 1000000 + micros
    if not 0 <= microseconds < 2**63:
        raise ValueError("telemetry timestamp outside the representable range")
    return microseconds


def vector(value, name, count):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"reported {name} must be a {count} component array")
    return [number(item, name) for item in value]


def without_personal_keys(mapping, path, redactions):
    """The object minus the one documented personal-data key, counting each removal by path."""
    kept = {}
    for key, value in mapping.items():
        if key in PERSONAL_KEYS:
            redactions[f"{path}.{key}"] = redactions.get(f"{path}.{key}", 0) + 1
        else:
            kept[key] = value
    return kept


def retained_hex(value, name):
    """Canonical JSON of a saved object, hex-encoded: recoverable and uninterpreted.

    This is the adopted `source_record_hex` retention, not a new mechanism: the object's own keys and
    values travel verbatim -- numbers as the exact lexemes the producer wrote -- so nothing is lost
    merely because this path gives it no column. It is a key-sorted re-encoding, NOT a copy of the
    source bytes; `source_document_sha256` is what pins the bytes, and whitespace and key order in the
    original are not reproduced. Retention is bounded and an oversized object is refused, not trimmed.
    """
    raw = canonical_json(value).encode("utf-8")
    if len(raw) > MAX_RETAINED:
        raise ValueError(f"saved {name} exceeds the retention bound and is refused, not trimmed")
    return "hex:" + raw.hex()


def envelope_data(document):
    """The `data` object of the documented response envelope, with an error body refused.

    The page's 200 schema requires `data`, `status_code`, `skydio_error_code` and `meta`, and gives
    `error_message` as a nullable string. A saved error body is therefore recognisable and is refused
    instead of being read as telemetry. The reported codes are not interpreted: the page states no
    success value for `skydio_error_code` and no unit for `meta.time`.
    """
    if not isinstance(document, dict):
        raise ValueError("selected saved telemetry body must be a JSON object")
    if document.get("error_message") is not None:
        raise ValueError("saved body carries an error_message, so it is not a telemetry response")
    status = document.get("status_code")
    if status is not None and status != 200:
        raise ValueError("saved body reports a status_code other than 200")
    data = document.get("data")
    if not isinstance(data, dict):
        raise ValueError("selected body requires the documented data object of the response envelope")
    meta = document.get("meta")
    return data, {"status_code": status, "skydio_error_code": document.get("skydio_error_code"),
                  # As canonical text, because `meta.time` is a number whose exact lexeme would be
                  # rounded away again by writing the report through `json.dumps`.
                  "meta_json": None if meta is None else canonical_json(meta),
                  "basis": ENVELOPE_BASIS}


def convert(text, *, source_format):
    """Declared quantities of a saved v1 body; everything else is retained, not given meaning."""
    if source_format != SOURCE_FORMAT:
        raise ValueError(f"declared source format must be exactly {SOURCE_FORMAT}")
    raw = text.encode("utf-8")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized saved telemetry body")
    document = json.loads(text, object_pairs_hook=unique_object, parse_float=SourceNumber)
    data, envelope = envelope_data(document)
    telemetry = data.get("flight_telemetry")
    if not isinstance(telemetry, dict):
        raise ValueError("selected body requires the flight_telemetry object the page requires")
    flight = data.get("flight")
    if not isinstance(flight, dict):
        # The page marks `flight` optional. This path needs reported identity, so its absence is a
        # named refusal rather than a blank identity or an invented one.
        raise ValueError("the page leaves flight optional, but this path needs its reported identity")
    samples = telemetry.get("aligned_telemetry")
    if not isinstance(samples, list) or not samples:
        raise ValueError("selected body requires a nonempty aligned_telemetry array")
    if len(samples) > MAX_SAMPLES:
        raise ValueError("saved telemetry body exceeds the sample bound")
    # `has_telemetry` is the producer's own declaration; a false claim beside real samples is a
    # contradiction in the saved body and is refused rather than silently preferred either way.
    if flight.get("has_telemetry") is False:
        raise ValueError("body declares has_telemetry false while carrying samples")
    report = {"source_format": source_format, "source_format_basis": SOURCE_FORMAT_BASIS,
              "source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
              "samples": len(samples), "redacted_keys": [], "absent_selected_fields": {},
              "declared_uninterpreted_keys": {}, "undeclared_retained_keys": {},
              "declared_range_violations": {}, "retention": RETENTION_BASIS,
              "undeclared_keys_basis": UNDECLARED_BASIS, "declared_range_basis": RANGE_BASIS,
              "context_retained_keys": {}, "undeclared_context_keys": {},
              "envelope": envelope, "clock": "Unknown",
              "completeness": "NOT_DECLARED_BY_THE_ENDPOINT_PAGE"}
    redactions = {}
    # The page requires only flight_id and vehicle_serial; the battery serial is optional, so its
    # column stays blank and its absence is counted instead of being refused or filled in.
    identity = {"skydio_flight_id_hex": text_hex(flight.get("flight_id"), "flight_id"),
                "skydio_vehicle_serial_hex": text_hex(flight.get("vehicle_serial"), "vehicle_serial"),
                "skydio_battery_serial_hex": ""}
    if "battery_serial" in flight:
        identity["skydio_battery_serial_hex"] = text_hex(flight["battery_serial"], "battery_serial")
    else:
        report["absent_selected_fields"]["flight.battery_serial"] = 1
    # Everything around the samples -- the envelope's own keys, the `data` siblings, the flight
    # object, the `flight_telemetry` siblings -- is retained once, on the first row, under the key it
    # was written at. Dropping a sibling and pointing at a hash would preserve no value, so each
    # section travels whole and each section's keys are listed in the report, with the ones the page
    # does not declare listed apart.
    context = {}
    for section, mapping in (("envelope", {key: value for key, value in document.items()
                                           if key != "data"}),
                             ("data", {key: value for key, value in data.items()
                                       if key not in ("flight", "flight_telemetry")}),
                             ("flight", flight),
                             ("flight_telemetry", {key: value for key, value in telemetry.items()
                                                   if key != "aligned_telemetry"})):
        context[section] = without_personal_keys(mapping, section, redactions)
        report["context_retained_keys"][section] = sorted(context[section])
        undeclared_context = sorted(set(context[section]) - DECLARED_CONTEXT_KEYS[section])
        if undeclared_context:
            report["undeclared_context_keys"][section] = undeclared_context
    source_context = retained_hex(context, "response context")
    digest = report["source_sha256"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError("each telemetry sample must be an object")
        retained = without_personal_keys(sample, "data.flight_telemetry.aligned_telemetry[]",
                                         redactions)
        present = set(retained)
        declared_uninterpreted = sorted((present & DECLARED_SAMPLE_KEYS) - INTERPRETED_KEYS)
        undeclared = sorted(present - DECLARED_SAMPLE_KEYS)
        for key in declared_uninterpreted:
            report["declared_uninterpreted_keys"][key] = (
                report["declared_uninterpreted_keys"].get(key, 0) + 1)
        for key in undeclared:
            report["undeclared_retained_keys"][key] = (
                report["undeclared_retained_keys"].get(key, 0) + 1)
        row = {name: "" for name in FIELDS}
        row.update(identity, record_time_us=sample_time_us(sample.get("timestamp")),
                   skydio_sample_index=index, skydio_sample_count=len(samples),
                   source_record_hex=retained_hex(retained, "telemetry sample"),
                   skydio_declared_uninterpreted_key_count=len(declared_uninterpreted),
                   skydio_undeclared_key_count=len(undeclared),
                   skydio_source_context_hex=source_context if index == 0 else "",
                   skydio_clock_basis=CLOCK_BASIS, skydio_unit_basis=UNIT_BASIS,
                   skydio_frame_basis=FRAME_BASIS, skydio_identity_basis=IDENTITY_BASIS,
                   skydio_producer_basis=PRODUCER_BASIS, skydio_retention_basis=RETENTION_BASIS,
                   skydio_envelope_basis=ENVELOPE_BASIS, source_document_sha256=digest)
        for key, column in UNIT_SCALARS + ((BATTERY_KEY, BATTERY_COLUMN), (COUNT_KEY, COUNT_COLUMN)):
            if key in sample:
                value = number(sample[key], key)
                row[column] = cell(value)
                if key == BATTERY_KEY and not 0 <= value <= 1:
                    report["declared_range_violations"][BATTERY_KEY] = (
                        report["declared_range_violations"].get(BATTERY_KEY, 0) + 1)
            else:
                report["absent_selected_fields"][key] = report["absent_selected_fields"].get(key, 0) + 1
        for key, stem, parts in UNIT_VECTORS:
            if key in sample:
                for part, value in zip(parts, vector(sample[key], key, len(parts))):
                    row[f"{stem}_{part}"] = cell(value)
            else:
                report["absent_selected_fields"][key] = report["absent_selected_fields"].get(key, 0) + 1
        writer.writerow(row)
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted telemetry exceeds output bound")
    report["redacted_keys"] = [{"path": path, "reason": REDACTION_REASON, "occurrences": count}
                               for path, count in sorted(redactions.items())]
    for name in ("declared_uninterpreted_keys", "undeclared_retained_keys"):
        report[name] = {key: {"samples": count, "status": RETAINED_STATUS}
                        for key, count in sorted(report[name].items())}
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved response body already on disk")
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--source-format", required=True, choices=(SOURCE_FORMAT,),
                        help="declared precondition; the saved body carries no version field")
    args = parser.parse_args()
    try:
        # Bound the input by its size on disk before reading it, so an oversized file is refused
        # without being loaded at all rather than after it is already in memory.
        size = args.input.stat().st_size
        if size > MAX_INPUT:
            raise ValueError(f"saved body is {size} bytes, past the {MAX_INPUT} byte input bound")
        text = args.input.read_text(encoding="utf-8")
        converted, report = convert(text, source_format=args.source_format)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"saved Skydio telemetry conversion failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
