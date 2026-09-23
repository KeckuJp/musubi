#!/usr/bin/env python3
"""Saved `exiftool -json -n -G` export -> image metadata observations; never opens an image."""
import argparse
import csv
import datetime
import decimal
import hashlib
import io
import json
import math
import re
from pathlib import Path

try:
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant

LIMIT = 16 * 1024 * 1024
MAX_IMAGES = 4096
MAX_TAGS_PER_IMAGE = 1024

# Both the group-qualified (`-G`) and the flat export are accepted. ExifTool 13.59 source
# (GPS.pm) settles what each coordinate key means: the recorded tag's ValueConv is
# `ToDegrees($val)` called **without** the sign argument and never consults the Ref tag, so
# it is an unsigned magnitude; the Composite tag's ValueConv is
# `$val[1] =~ /^S/i ? -$val[0] : $val[0]`, so it is already signed. A bare key is therefore
# resolvable from its sign together with the reference, and a normal flat export is no
# longer refused just because the key alone does not name its group.
GROUPED_KEY = re.compile(r"\A([A-Za-z][A-Za-z0-9_]*):([A-Za-z][A-Za-z0-9_]*)\Z")
NUMERIC_TEXT = re.compile(r"\A-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?\Z")
INTEGRAL_TEXT = re.compile(r"\A(?:0|[1-9][0-9]{0,8})\Z")
# `ConvertTimeStamp` builds `sprintf("%.2d:%.2d:%s")`; the seconds field keeps whatever
# decimals the rational carried, so its text is preserved exactly rather than reformatted.
GPS_TIME_TEXT = re.compile(r"\A([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2})(\.[0-9]{1,18})?\Z")
DATE_TEXT = re.compile(r"\A([0-9]{4}):([0-9]{2}):([0-9]{2})\Z")
LEAP_SECOND_SUFFIX = "_LEAP_SECOND_LEXICAL_NOT_NORMALIZED"
DATE_TIME_TEXT = re.compile(r"\A([0-9]{4}:[0-9]{2}:[0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})\Z")

# Exif 2.3 §4.6.4 p.30: values whose 0th row lies on the display's left or right side put
# the stored rows on the vertical axis, so display width and height are swapped.
ORIENTATION_MEANING = {
    1: ("ROW0_TOP_COL0_LEFT", False),
    2: ("ROW0_TOP_COL0_RIGHT", False),
    3: ("ROW0_BOTTOM_COL0_RIGHT", False),
    4: ("ROW0_BOTTOM_COL0_LEFT", False),
    5: ("ROW0_LEFT_COL0_TOP", True),
    6: ("ROW0_RIGHT_COL0_TOP", True),
    7: ("ROW0_RIGHT_COL0_BOTTOM", True),
    8: ("ROW0_LEFT_COL0_BOTTOM", True),
}
# Exif 2.3 §4.6.6 p.70 defines 0 and 1 only. ExifTool 13.59's GPS.pm PrintConv additionally
# lists 2 and 3 ("Positive/Negative Sea Level (sea-level ref)") — that is a tool release, not
# a specification revision, and the revision that defines them was not readable, so 2 and 3
# are retained without an above/below claim.
ALTITUDE_REFERENCE = {0: "SEA_LEVEL_REFERENCE_ABOVE", 1: "SEA_LEVEL_REFERENCE_BELOW"}
HEMISPHERE = {"N": 1, "S": -1, "E": 1, "W": -1}

# A tag this converter does not interpret keeps its value in the supplied export, which the
# export digest and the record index locate exactly. No copy, no encoding and no digest of
# that value is produced here.
RETAINED_IN_ORIGINAL = "RETAINED_IN_SOURCE_EXPORT_NOT_INTERPRETED"

# The one exporter release whose value conversions were read at the source. A different
# number is not a compatibility claim, so it is refused rather than read with these meanings.
QUALIFIED_EXIFTOOL_VERSION = decimal.Decimal("13.59")

COLUMNS = [
    "record_time_us", "source_image_index", "export_sha256",
    "exiftool_version_declared", "source_file_hex", "file_name_hex", "file_type_extension_hex",
    "stored_width_px", "stored_height_px", "stored_size_tag", "stored_size_conflict",
    "tiff_image_width_px", "tiff_image_height_px",
    "orientation_code", "orientation_placement", "orientation_swaps_display_axes",
    "gps_latitude_deg", "gps_latitude_ref", "gps_longitude_deg", "gps_longitude_ref",
    "gps_coordinate_status", "gps_map_datum_hex", "gps_coordinate_datum_status",
    "gps_altitude_m", "gps_altitude_ref_code", "gps_altitude_reference",
    "gps_date_stamp_utc", "gps_time_of_day_utc", "gps_time_basis",
    "datetime_original_text", "datetime_original_zone_offset_text", "datetime_original_basis",
    "camera_make_hex", "camera_model_hex", "camera_identity_basis",
    "unknown_tag_count", "retained_tag_count",
]


def hexed(text):
    """Retain a declared string without printing a live path, URL or comment."""
    if not isinstance(text, str):
        raise ValueError("declared string required")
    encoded = text.encode()
    if len(encoded) > 4096:
        raise ValueError("declared string exceeds bound")
    return "hex:" + encoded.hex()


def finite_number(value, what):
    """A JSON number, or a strictly numeric string.

    ExifTool's JSON writer quotes only values that "don't look like numbers"; that rule is
    documented but its regex sits in a part of the `exiftool` script that could not be read,
    so a quoted numeric value is accepted rather than refusing a normal export over it.
    """
    if isinstance(value, str) and NUMERIC_TEXT.match(value):
        value = float(value)
    if type(value) is bool or type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid " + what)
    return value


def resolve(record, group, name, used):
    """A tag from a grouped export, a flat export, or the exporter's Composite table.

    Returns (value, origin) with origin one of RECORDED / BARE / COMPOSITE / None. `-G`
    with no number selects family 0, whose name for the GPS IFD is `EXIF`
    (GPS.pm `GROUPS => { 0 => 'EXIF', 1 => 'GPS', 2 => 'Location' }`).

    Every key this actually reads is added to `used`, so the accounting reflects what was
    interpreted rather than a static list of names. The same tag under both a grouped and a
    bare key is a **cross-check**: equal values are both marked used, a disagreement is a
    refusal. The derived key is left to the caller that knows its sign rule; if it is not
    consumed there it stays uninterpreted and is listed as such.
    """
    grouped = group + ":" + name
    composite = "Composite:" + name
    chosen = None
    for key, origin in ((grouped, "RECORDED"), (name, "BARE"), (composite, "COMPOSITE")):
        if key not in record:
            continue
        if chosen is None:
            chosen, chosen_origin = key, origin
        elif key != composite:
            if record[key] != record[chosen]:
                raise ValueError("grouped and bare " + name + " disagree")
            used.add(key)
    if chosen is None:
        return None, None
    used.add(chosen)
    return record[chosen], chosen_origin


def dimension(value, what):
    number = integral(value, what)
    if not 1 <= number <= 2**31 - 1:
        raise ValueError("invalid " + what)
    return number


def coordinate(record, tag, limit, used):
    """Signed degrees, whichever of the two coordinate tags the export carries.

    The recorded tag is an unsigned magnitude and the Composite tag is already signed
    (ExifTool 13.59 GPS.pm), so a value and its reference together determine the answer even
    when a flat key does not say which tag it was: a negative value can only be the signed
    form, and a non-negative value with a southern/western reference can only be the
    magnitude. A sign that contradicts the reference is refused rather than resolved.
    """
    value, origin = resolve(record, "EXIF", "GPS" + tag, used)
    reference, _ = resolve(record, "EXIF", "GPS" + tag + "Ref", used)
    if value is None and reference is None:
        return None, "", "NOT_DECLARED"
    if value is None:
        raise ValueError("GPS " + tag + " reference without a value")
    number = finite_number(value, "GPS " + tag)

    if reference is None:
        if origin != "COMPOSITE":
            # A magnitude with no hemisphere establishes nothing, and is never north.
            raise ValueError("GPS " + tag + " needs its reference")
        signed, status = number, "EXPORTER_DERIVED_SIGNED_REFERENCE_TAG_ABSENT"
    else:
        if not isinstance(reference, str) or reference not in HEMISPHERE:
            raise ValueError("unknown GPS " + tag + " reference")
        if (tag == "Latitude") != (reference in ("N", "S")):
            raise ValueError("GPS " + tag + " reference belongs to the other axis")
        negative = HEMISPHERE[reference] < 0
        if origin == "RECORDED":
            if number < 0:
                raise ValueError("the recorded GPS " + tag + " tag is an unsigned magnitude")
            signed = -number if negative else number
            status = "DECLARED_WITH_EXPLICIT_REFERENCE"
        elif origin == "COMPOSITE":
            if number and (number < 0) != negative:
                raise ValueError("derived GPS " + tag + " sign contradicts its reference")
            signed = number
            status = "EXPORTER_DERIVED_SIGNED_FROM_REFERENCE"
        else:  # a flat key: the sign and the reference settle it between them
            if number < 0 and not negative:
                raise ValueError("signed GPS " + tag + " contradicts its reference")
            signed = number if number < 0 else (-number if negative else number)
            status = "DECLARED_WITH_EXPLICIT_REFERENCE"

    if abs(signed) > limit:
        raise ValueError("GPS " + tag + " outside its range")
    # When a grouped export carries both, the derived one is a cross-check, not a rival.
    derived_key = "Composite:GPS" + tag
    if derived_key in record and origin != "COMPOSITE":
        if finite_number(record[derived_key], "derived GPS " + tag) != signed:
            raise ValueError("recorded and derived GPS " + tag + " disagree")
        used.add(derived_key)  # consumed as a cross-check, so not listed as uninterpreted
    return signed, (reference if isinstance(reference, str) else ""), status


def exact_decimal(value, what):
    """A value as an exact decimal, with no float rounding introduced here.

    A JSON number arrives as a Python float; `repr` gives its shortest round-tripping
    decimal, so nothing is lost beyond what the JSON literal already fixed. A quoted value
    keeps its own digits.
    """
    if type(value) is bool:
        raise ValueError("invalid " + what)
    try:
        if isinstance(value, int):
            return decimal.Decimal(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("invalid " + what)
            return decimal.Decimal(repr(value))
        if isinstance(value, str) and NUMERIC_TEXT.match(value):
            return decimal.Decimal(value)
    except decimal.InvalidOperation as exc:
        raise ValueError("invalid " + what) from exc
    raise ValueError("invalid " + what)


def integral(value, what):
    """A whole number, strictly. `1.9` is not `1`, and a quoted value must be all digits."""
    if type(value) is not bool and isinstance(value, int):
        return value
    if isinstance(value, str) and INTEGRAL_TEXT.match(value):
        return int(value)
    raise ValueError("non-integral " + what)


def calendar_date(value, what):
    """`YYYY:MM:DD` that the calendar actually has. `2026:99:99` is not a date."""
    matched = DATE_TEXT.match(value) if isinstance(value, str) else None
    if not matched:
        raise ValueError("unsupported " + what)
    try:
        datetime.date(*(int(part) for part in matched.groups()))
    except ValueError as exc:
        raise ValueError(what + " is not a calendar date") from exc
    return value


def gps_time_of_day(value):
    """UTC time of day, and whether its seconds field is a leap-second lexical value.

    ExifTool 13.59 GPS.pm `ConvertTimeStamp` returns one string, `sprintf("%.2d:%.2d:%s")`,
    whose seconds field carries whatever decimals the rational held. Those digits are
    **preserved exactly**: nothing is rounded to a fixed width, so `23:59:59.999999` stays
    itself instead of being carried into a minute that the export never stated. A seconds
    field of `60` is retained as written and reported as a leap-second lexical value, never
    normalised away. The raw three-rational triple of Exif 2.3 is also accepted, but an
    hour or minute with a fraction is **refused**: this pin defines no basis for spilling
    one into the next field, and truncating it would silently lose the time.
    """
    if isinstance(value, str):
        matched = GPS_TIME_TEXT.match(value)
        if not matched:
            raise ValueError("unsupported GPSTimeStamp text at pin")
        hour = int(matched.group(1))
        minute = int(matched.group(2))
        second = decimal.Decimal(matched.group(3) + (matched.group(4) or ""))
        seconds_text = matched.group(3).zfill(2) + (matched.group(4) or "")
    elif isinstance(value, list) and len(value) == 3:
        parts = [exact_decimal(part, "GPSTimeStamp component") for part in value]
        for part, what in zip(parts[:2], ("hour", "minute")):
            if part != part.to_integral_value():
                raise ValueError("fractional GPSTimeStamp " + what + " has no defined basis")
        hour, minute, second = int(parts[0]), int(parts[1]), parts[2]
        digits = format(second, "f")
        whole, _, fraction = digits.partition(".")
        seconds_text = whole.zfill(2) + ("." + fraction if fraction else "")
    else:
        raise ValueError("unsupported GPSTimeStamp shape at pin")
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second < 61):
        raise ValueError("GPSTimeStamp outside its range")
    return "%02d:%02d:%s" % (hour, minute, seconds_text), second >= 60


def image_row(record, index, capture_time_us, export_digest):
    if not isinstance(record, dict):
        raise ValueError("each export entry is one image object")
    if len(record) > MAX_TAGS_PER_IMAGE:
        raise ValueError("image object exceeds tag bound")
    used = set()
    for key in record:
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("invalid export key")
    version = resolve(record, "ExifTool", "ExifToolVersion", used)[0]
    # `lib/Image/ExifTool.pm` declares `$VERSION = '13.59'`, and that is the only release
    # whose value conversions were read. Another number may well behave identically, but
    # nothing here has checked it, so it is refused instead of being read with 13.59's
    # meanings. Quoted or bare, both forms of the same version arrive here.
    if version is None:
        raise ValueError("export must declare its ExifTool version")
    if exact_decimal(version, "ExifToolVersion") != QUALIFIED_EXIFTOOL_VERSION:
        raise ValueError("export is not from the qualified ExifTool 13.59")

    row = dict.fromkeys(COLUMNS, "")
    row["record_time_us"] = capture_time_us
    row["source_image_index"] = index
    row["export_sha256"] = export_digest
    row["exiftool_version_declared"] = version if isinstance(version, str) else repr(version)
    if "SourceFile" in record:
        row["source_file_hex"] = hexed(record["SourceFile"])
        used.add("SourceFile")
    for column, name in (("file_name_hex", "FileName"),
                         ("file_type_extension_hex", "FileTypeExtension")):
        declared = resolve(record, "File", name, used)[0]
        if declared is not None:
            row[column] = hexed(declared)
    # Dimensions. The compressed-data tags describe the stored image; a TIFF width inside a
    # JPEG is out of spec, so a disagreement is reported, never resolved.
    pixel_x = resolve(record, "EXIF", "ExifImageWidth", used)[0]
    pixel_y = resolve(record, "EXIF", "ExifImageHeight", used)[0]
    tiff_x = resolve(record, "EXIF", "ImageWidth", used)[0]
    tiff_y = resolve(record, "EXIF", "ImageHeight", used)[0]
    if (pixel_x is None) != (pixel_y is None) or (tiff_x is None) != (tiff_y is None):
        raise ValueError("image dimensions must be declared as a pair")
    if tiff_x is not None:
        row["tiff_image_width_px"] = dimension(tiff_x, "ImageWidth")
        row["tiff_image_height_px"] = dimension(tiff_y, "ImageHeight")
    if pixel_x is not None:
        row["stored_width_px"] = dimension(pixel_x, "ExifImageWidth")
        row["stored_height_px"] = dimension(pixel_y, "ExifImageHeight")
        row["stored_size_tag"] = "PIXEL_X_Y_DIMENSION"
        conflict = tiff_x is not None and (row["tiff_image_width_px"] != row["stored_width_px"]
                                           or row["tiff_image_height_px"] != row["stored_height_px"])
        row["stored_size_conflict"] = "CONFLICTING_DECLARED_SIZES" if conflict else "NONE"
    elif tiff_x is not None:
        row["stored_width_px"] = row["tiff_image_width_px"]
        row["stored_height_px"] = row["tiff_image_height_px"]
        row["stored_size_tag"] = "TIFF_IMAGE_WIDTH_LENGTH"
        row["stored_size_conflict"] = "NONE"
    else:
        row["stored_size_tag"] = "NOT_DECLARED"

    # Orientation. The specification's default of 1 is not applied: an undeclared tag stays
    # undeclared and makes no display claim.
    code = resolve(record, "EXIF", "Orientation", used)[0]
    if code is not None:
        code = integral(code, "Orientation")
        row["orientation_code"] = code
        if code in ORIENTATION_MEANING:
            placement, swaps = ORIENTATION_MEANING[code]
            row["orientation_placement"] = placement
            row["orientation_swaps_display_axes"] = 1 if swaps else 0
        else:
            row["orientation_placement"] = "UNSUPPORTED_VALUE_AT_PIN"
    else:
        row["orientation_placement"] = "NOT_DECLARED"

    latitude, lat_ref, lat_status = coordinate(record, "Latitude", 90, used)
    longitude, lon_ref, lon_status = coordinate(record, "Longitude", 180, used)
    if lat_status != lon_status:
        raise ValueError("one coordinate axis without the other")
    if latitude is not None:
        row["gps_latitude_deg"] = latitude
        row["gps_longitude_deg"] = longitude
        row["gps_latitude_ref"] = lat_ref
        row["gps_longitude_ref"] = lon_ref
    row["gps_coordinate_status"] = lat_status
    datum = resolve(record, "EXIF", "GPSMapDatum", used)[0]
    if datum is not None:
        row["gps_map_datum_hex"] = hexed(datum)
        row["gps_coordinate_datum_status"] = "DECLARED_BY_RECEIVER_NOT_VERIFIED"
    else:
        row["gps_coordinate_datum_status"] = "DATUM_NOT_DECLARED"

    # Altitude. The number alone establishes no datum: the pinned specification names only
    # "sea level" and defines no geodetic or vertical datum, so none is ever printed.
    altitude, altitude_origin = resolve(record, "EXIF", "GPSAltitude", used)
    reference = resolve(record, "EXIF", "GPSAltitudeRef", used)[0]
    if altitude is not None:
        metres = finite_number(altitude, "GPSAltitude")
        if reference is None:
            row["gps_altitude_reference"] = "ALTITUDE_REFERENCE_NOT_DECLARED"
        else:
            # A reference is an enumerated byte. `1.9` is not `1`, and rounding it would
            # invent an above/below claim the export never made.
            reference = integral(reference, "GPSAltitudeRef")
            row["gps_altitude_ref_code"] = reference
            row["gps_altitude_reference"] = ALTITUDE_REFERENCE.get(
                reference, "ALTITUDE_REFERENCE_UNSUPPORTED_AT_PIN")
        if metres < 0:
            # The recorded tag's ValueConv extracts an unsigned decimal, so a negative value
            # is the Composite form, which is `-abs(...)` when the reference says below.
            if altitude_origin == "RECORDED" or row["gps_altitude_reference"] != \
                    "SEA_LEVEL_REFERENCE_BELOW":
                raise ValueError("signed GPSAltitude contradicts its reference")
            metres = -metres
        row["gps_altitude_m"] = metres
    elif reference is not None:
        raise ValueError("GPSAltitudeRef without an altitude")
    else:
        row["gps_altitude_reference"] = "NOT_DECLARED"

    # Time. The GPS pair is UTC by specification; DateTimeOriginal carries no zone and is
    # never converted. Neither is turned into the row's time column.
    date_stamp = resolve(record, "EXIF", "GPSDateStamp", used)[0]
    time_stamp = resolve(record, "EXIF", "GPSTimeStamp", used)[0]
    leap = False
    if date_stamp is None and time_stamp is None:
        row["gps_time_basis"] = "NOT_DECLARED"
    elif date_stamp is None or time_stamp is None:
        # A time of day without its date is not a moment; keep what is there, explicitly.
        if time_stamp is not None:
            row["gps_time_of_day_utc"], leap = gps_time_of_day(time_stamp)
        else:
            row["gps_date_stamp_utc"] = calendar_date(date_stamp, "GPSDateStamp")
        row["gps_time_basis"] = "EXIF_GPS_UTC_INCOMPLETE_PAIR"
    else:
        # A date is only a date if the calendar has it; `2026:99:99` is not a UTC moment.
        row["gps_date_stamp_utc"] = calendar_date(date_stamp, "GPSDateStamp")
        row["gps_time_of_day_utc"], leap = gps_time_of_day(time_stamp)
        row["gps_time_basis"] = "EXIF_GPS_UTC_DECLARED"
    if leap:
        row["gps_time_basis"] += LEAP_SECOND_SUFFIX
    text = resolve(record, "EXIF", "DateTimeOriginal", used)[0]
    if text is not None:
        matched = DATE_TIME_TEXT.match(text) if isinstance(text, str) else None
        if not matched:
            raise ValueError("unsupported DateTimeOriginal")
        # Local wall text still has to be a date and a clock reading; the zone stays unknown
        # either way, and nothing here is converted.
        calendar_date(matched.group(1), "DateTimeOriginal")
        hour, minute, second = (int(matched.group(n)) for n in (2, 3, 4))
        if not (hour <= 23 and minute <= 59 and second <= 60):
            raise ValueError("DateTimeOriginal outside its clock range")
        row["datetime_original_text"] = text
        offset = resolve(record, "EXIF", "OffsetTimeOriginal", used)[0]
        if offset is None:
            row["datetime_original_basis"] = "LOCAL_WALL_TEXT_ZONE_UNKNOWN_NOT_UTC"
        else:
            # Retained as declared text only; the revision defining its format could not be
            # read at this pin, so it is never parsed or applied to the timestamp.
            row["datetime_original_zone_offset_text"] = hexed(offset)
            row["datetime_original_basis"] = "LOCAL_WALL_TEXT_WITH_DECLARED_OFFSET_NOT_APPLIED"
        if second == 60:
            row["datetime_original_basis"] += LEAP_SECOND_SUFFIX
    else:
        row["datetime_original_basis"] = "NOT_DECLARED"

    for column, name in (("camera_make_hex", "Make"), ("camera_model_hex", "Model")):
        declared = resolve(record, "EXIF", name, used)[0]
        if declared is not None:
            row[column] = hexed(declared)
    row["camera_identity_basis"] = "DECLARED_CAMERA_STRING_NOT_A_PLATFORM_IDENTITY"

    # No silent drop, and no new fingerprint either. Every tag outside the consumed set is
    # named by its field path and counted, and the supplied export plus the record index is
    # the reference that recovers its value. Nothing value-derived is emitted: a digest of a
    # comment, a serial or a credential is still a reusable fingerprint of it, and it is not
    # preservation — the original export is the recovery source.
    unknown = [key for key in sorted(record) if key not in used]
    row["unknown_tag_count"] = len(unknown)
    row["retained_tag_count"] = len(record)
    retained = [{"field_path": key, "status": RETAINED_IN_ORIGINAL} for key in unknown]
    return row, retained


def convert(data, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("invalid capture time")
    if len(data) > LIMIT:
        raise ValueError("input exceeds bound")
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        if "\x00" in text:
            raise ValueError("NUL byte in export")
        document = json.loads(text, object_pairs_hook=unique_object,
                              parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed export") from exc
    if not isinstance(document, list) or not document:
        raise ValueError("exiftool -json export is a non-empty array of image objects")
    if len(document) > MAX_IMAGES:
        raise ValueError("export exceeds image bound")
    export_digest = hashlib.sha256(
        data if isinstance(data, (bytes, bytearray)) else data.encode()).hexdigest()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    report = {
        "export_sha256": export_digest,
        "export_bytes": len(data),
        "images": 0,
        "digest_basis": "sha256 of the supplied decoder export text only; no image was "
                        "opened or verified, and no value inside the export is digested",
        "decoder": "caller-supplied exiftool -json -n -G export; this converter neither "
                   "bundles nor launches a decoder and never opens an image",
        "pinned_tag_semantics": "Exif 2.3 (CIPA DC-008-2012); ExifTool documentation 13.59",
        "retained_tags": [],
        "unknown_tag_total": 0,
        "images_with_coordinates": 0,
        "images_with_gps_utc": 0,
    }
    try:
        for index, record in enumerate(document, start=1):
            row, retained = image_row(record, index, capture_time_us, export_digest)
            writer.writerow(row)
            report["images"] += 1
            report["unknown_tag_total"] += len(retained)
            report["retained_tags"].append({
                "source_image_index": index,
                "recovery_reference": "export_sha256 + source_image_index",
                "not_interpreted": retained,
            })
            if row["gps_coordinate_status"] == "DECLARED_WITH_EXPLICIT_REFERENCE":
                report["images_with_coordinates"] += 1
            if row["gps_time_basis"] == "EXIF_GPS_UTC_DECLARED":
                report["images_with_gps_utc"] += 1
            if output.tell() > LIMIT:
                raise ValueError("observation CSV exceeds bound")
        if len(json.dumps(report).encode()) > LIMIT:
            raise ValueError("report exceeds bound")
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("malformed export") from exc
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller read clock; not the image capture time and not UTC")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)  # one bounded read; no size/read race
        output, report = convert(data, args.capture_time_us)
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, RecursionError):
        parser.exit(2, "image metadata conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
