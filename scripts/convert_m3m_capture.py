#!/usr/bin/env python3
"""Saved `exiftool -json -n -G` export of a DJI M3M multispectral band image -> capture rows.

Reads the fields the vendor's own *Mavic 3M Image Processing Guide* (2023-08-29) documents, in the
unit and meaning that guide states. It never opens an image, never runs a decoder, never contacts a
device, and it produces **no reflectance, no index and no aircraft telemetry series**: one row per
band image, carrying the capture fix, the band identity and the recorded calibration inputs exactly
as written.

The EXIF half of such an export is already served by the adopted `convert_image_metadata.py`, which
this module does not modify: its exporter qualification and value helpers are imported here.
"""
import argparse
import csv
import decimal
import hashlib
import io
import json
import math
import re
from pathlib import Path

try:
    from scripts.convert_image_metadata import (
        LEAP_SECOND_SUFFIX, LIMIT, MAX_IMAGES, MAX_TAGS_PER_IMAGE, QUALIFIED_EXIFTOOL_VERSION,
        RETAINED_IN_ORIGINAL, calendar_date, gps_time_of_day, hexed, integral, resolve)
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from convert_image_metadata import (
        LEAP_SECOND_SUFFIX, LIMIT, MAX_IMAGES, MAX_TAGS_PER_IMAGE, QUALIFIED_EXIFTOOL_VERSION,
        RETAINED_IN_ORIGINAL, calendar_date, gps_time_of_day, hexed, integral, resolve)
    from convert_position_csv import unique_object, invalid_constant

# The one documented writer contract read at the source. The guide's XMP table opens with
# `Version  1.6  string  -  XMP version.`, so a different value is refused rather than read with
# these meanings -- the same discipline the adopted image path applies to the exporter release.
QUALIFIED_METADATA_VERSION = "1.6"
GUIDE = ("DJI Mavic 3M Image Processing Guide 2023-08-29 EN, 673803 bytes, sha256:"
         "6f7a39fd3977f977b36171af6da4179051f948f8bcaaf35b4a05743c0b1dc711"
         " (EXIF IFD0, EXIF GPS and XMP drone-dji tables)")

# Every table below is the guide's own wording. A code it does not list is retained, never guessed.
GPS_STATUS = ("Normal", "RTK", "Invalid")
ALTITUDE_TYPE = ("PressureAlt", "GPSFusionAlt", "RtkAlt")
SURVEYING_MODE = {
    0: "NOT_RECOMMENDED_THE_ACCURACY_CANNOT_BE_GUARANTEED",
    1: "RECOMMENDED_THE_ACCURACY_CAN_BE_GUARANTEED",
}
SUN_SENSOR_STATUS = {
    0: "INVALID_STATE_DUE_TO_INSERTION_OF_USB_DONGLE",
    1: "VALID_STATE",
    2: "VALID_AND_COMPENSATING_STATE",
}
# Both codes are the guide's own, quoted from its XMP table rather than inferred as a
# complementary boolean:
#   `VignettingFlag  0  integer  -  Vignetting compensation flag. 0: Disabled, 1: Enabled.
#    Fixed 0.`
#   `DewarpFlag  0  integer  -  Whether the camera parameters have been dewarped or not.
#    0: Not dewarped. 1: Dewarped. Fixed 0.`
# "Fixed 0" says what this writer emits; it does not remove the documented meaning of 1, which
# is why 1 is a listed meaning here and not an unlisted code. What the record itself declares
# still decides the stored-pixel statement below.
VIGNETTING_STATE = {0: "COMPENSATION_DISABLED", 1: "COMPENSATION_ENABLED"}
DEWARP_STATE = {0: "NOT_DEWARPED", 1: "DEWARPED"}
# "Green: 1, Red: 2, RedEdge: 3, NIR: 4", and the band names the guide lists for BandName.
SENSOR_INDEX_BAND = {1: "Green", 2: "Red", 3: "RedEdge", 4: "NIR"}
# "Sun sensor raw values. Order: Green, Red, RedEdge, NIR" -- four positions; and
# "Coefficients of vignetting compensation. (k[0],k[1],k[2],k[3],k[4],k[5])" -- six.
RAW_DATA_VALUES = 4
VIGNETTING_COEFFICIENTS = 6
BAND_NAMES = tuple(SENSOR_INDEX_BAND.values())
UNLISTED = "UNLISTED_BY_THE_PINNED_GUIDE_RETAINED_AS_WRITTEN"
LISTED = "LISTED_BY_THE_PINNED_GUIDE"

# The decoder's key is its TAG name, not the writer's XML element name: DJI writes the element
# `drone-dji:GpsLatitude`, and ExifTool 13.59's DJI XMP table maps it to `Name => 'GPSLatitude'`
# (`lib/Image/ExifTool/DJI.pm`), so `-json -n -G` emits `XMP:GPSLatitude`. Observed, not assumed.
# Under `-n` the value arrives EITHER as a signed number (when the packet was written through a
# tag writer) OR as the guide's own `22.000000°N` magnitude-with-letter text (when the packet
# carries it verbatim). Both were produced from the pinned release and both are read here.
COORDINATE_TEXT = re.compile(r"\A([0-9]{1,3}(?:\.[0-9]{1,12})?)\s*°?\s*([NSEW])\Z")
# `BandFreq  860 (+/-26) nm  string  -  Narrow band wavelength. Format is
#  "Central wavelength (+/- HWHM) nm".`  HWHM, not FWHM: the half width at half maximum.
BAND_FREQ_TEXT = re.compile(
    r"\A([0-9]{1,5}(?:\.[0-9]{1,6})?)\s*\(\s*\+/-\s*([0-9]{1,5}(?:\.[0-9]{1,6})?)\s*\)\s*nm\Z")
DOCUMENTED_NUMBER_TEXT = re.compile(
    r"\A[+-]?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,15})?(?:[eE][+-]?[0-9]{1,3})?\Z")
SIGN = {"N": 1, "S": -1, "E": 1, "W": -1}
AXIS = {"GPSLatitude": ("N", "S", 90), "GPSLongitude": ("E", "W", 180)}
# A CSV cell the common reader can carry: it refuses quoted input outright
# ("quoted or binary input is unsupported"), so a writer string containing a comma,
# a quote or a control character is carried as `hex:` rather than lost or mangled.
CSV_UNSAFE = re.compile(r'[,"\r\n\t]|[\x00-\x1f]')

REFLECTANCE_BASIS = (
    "no_reflectance_no_radiance_and_no_vegetation_index_is_produced_here_the_irradiance_gain_"
    "exposure_black_level_and_vignetting_coefficients_are_carried_as_the_recorded_inputs_the_"
    "guide_documents_and_none_of_them_is_applied_because_the_guides_extracted_text_states_no_"
    "conversion_procedure_so_deriving_one_would_be_invention")
# The guide says both flags are "Fixed 0", but a record is evidence only of what IT declares:
# a missing or non-zero flag must never be reported as an uncompensated image.
STORED_PIXEL_UNCOMPENSATED = (
    "both_declared_flags_in_THIS_record_are_zero_so_as_written_the_stored_band_image_is_neither_"
    "vignetting_compensated_nor_dewarped_and_beyond_those_two_declared_flags_no_claim_is_made_"
    "about_what_the_stored_pixel_values_represent")
STORED_PIXEL_UNKNOWN = (
    "one_or_both_of_the_vignetting_and_dewarp_flags_is_absent_or_non_zero_in_THIS_record_so_"
    "although_the_pinned_guide_calls_each_of_them_fixed_at_zero_no_claim_is_made_here_that_the_"
    "stored_band_image_is_uncompensated_or_undewarped_read_the_two_flag_columns_themselves")
TELEMETRY_BASIS = (
    "one_row_per_saved_band_image_which_is_a_single_exposure_and_never_a_vehicle_telemetry_series_"
    "nothing_is_interpolated_between_exposures_no_track_is_formed_and_every_attitude_and_speed_"
    "field_is_the_value_the_writer_recorded_when_that_photo_was_taken")
ATTITUDE_BASIS = ("degrees_in_the_writers_declared_NED_coordinate_system_with_rotation_order_ZYX_"
                  "when_the_photo_was_taken_not_a_calibrated_or_verified_orientation")
SPEED_BASIS = ("metres_per_second_in_the_writers_declared_north_east_and_elevation_directions_when_"
               "the_photo_was_taken_a_single_reported_instant_and_never_a_measured_ground_track")
ALTITUDE_BASIS = ("metres_the_guide_calls_AbsoluteAltitude_a_geodetic_altitude_and_names_no_datum_"
                  "for_it_while_RelativeAltitude_is_relative_to_the_altitude_of_the_take_off_point_"
                  "so_the_two_are_different_quantities_and_neither_is_a_terrain_height")
IRRADIANCE_BASIS = ("the_sun_sensor_value_after_compensation_by_the_writers_built_in_algorithm_the_"
                    "guide_states_no_unit_for_it_so_none_is_asserted_and_it_is_never_read_as_"
                    "watts_per_square_metre")
BLACK_LEVEL_BASIS = ("the_reported_black_level_whose_documented_examples_are_3200_at_16_bit_and_12_"
                     "at_8_bit_so_the_EXIF_bits_per_sample_is_carried_beside_it_and_neither_is_"
                     "subtracted_from_anything_here")
CLOCK_BASIS = ("the_writers_GPS_date_and_time_of_the_moment_the_photo_was_taken_which_is_not_the_"
               "callers_capture_clock_in_record_time_us_and_is_not_a_boot_or_device_uptime_axis")

COLUMNS = [
    "record_time_us", "source_image_index", "export_sha256", "exiftool_version_declared",
    "metadata_version_declared", "image_source_hex",
    "band_name_reported", "band_name_status", "sensor_index_code", "sensor_index_band",
    "band_identity_agreement", "band_freq_text_hex", "band_central_wavelength_nm",
    "band_half_width_nm", "band_width_basis",
    "capture_latitude_deg", "capture_longitude_deg", "capture_coordinate_basis",
    "capture_coordinate_agreement", "gps_status_reported", "gps_status_listing",
    "altitude_type_reported", "altitude_type_listing", "absolute_altitude_m",
    "relative_altitude_m", "altitude_basis",
    "gps_date_stamp_utc", "gps_time_of_day_utc", "capture_clock_basis",
    "rtk_flag_code", "rtk_positioning_quality", "rtk_std_lat", "rtk_std_lon", "rtk_std_hgt",
    "rtk_diff_age", "surveying_mode_code", "surveying_mode_reported",
    "gimbal_roll_deg", "gimbal_yaw_deg", "gimbal_pitch_deg",
    "flight_roll_deg", "flight_yaw_deg", "flight_pitch_deg", "attitude_basis",
    "flight_north_speed_m_s", "flight_east_speed_m_s", "flight_elevation_speed_m_s", "speed_basis",
    "irradiance_reported", "irradiance_basis", "sun_sensor_raw_green", "sun_sensor_raw_red",
    "sun_sensor_raw_red_edge", "sun_sensor_raw_nir", "sun_sensor_status_code",
    "sun_sensor_status", "sun_sensor_type_code", "sun_sensor_package_index",
    "sun_sensor_config_count",
    "sensor_gain", "sensor_gain_adjustment", "exposure_time_us", "black_level_code",
    "bits_per_sample", "black_level_basis",
    "vignetting_flag_code", "vignetting_state", "vignetting_coefficients_hex",
    "dewarp_flag_code", "dewarp_state", "stored_pixel_basis", "reflectance_basis",
    "telemetry_basis", "unknown_tag_count", "interpreted_tag_count",
]


def xmp(record, name, used):
    """One XMP field, read ONLY from the group-qualified key.

    This path requires `-G`, and deliberately does not fall back to a bare key or to the
    exporter's `Composite:` table. The composite coordinates are ExifTool's own value **derived
    from the EXIF pair**, so accepting one here would quietly substitute the EXIF fix for the
    writer's XMP field -- the very thing this case promises never to do -- and a bare key in a
    flat export cannot be told apart from the EXIF tag of the same name.
    """
    key = "XMP:" + name
    if key not in record:
        return None
    used.add(key)
    return record[key]


def csv_text(value):
    """A writer string as written, or its utf8 hex when a CSV row could not carry it."""
    return hexed(value) if CSV_UNSAFE.search(value) else value


def documented_number(value, what, low=None, high=None):
    """A numeric value in the forms this writer actually uses.

    The guide's own examples carry an explicit leading plus (`AbsoluteAltitude +50.000`,
    `GimbalYawDegree +0.00`) and scientific notation (`VignettingData 1.829488e-06`), neither of
    which the adopted EXIF path's grammar accepts -- that grammar is right for its own tags and is
    not changed. Exactness is kept with `Decimal`, so nothing is rounded through a float, and an
    infinity, a NaN or an absurd magnitude is refused rather than carried.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, decimal.Decimal)):
        raise ValueError("invalid " + what)
    if isinstance(value, str):
        if not DOCUMENTED_NUMBER_TEXT.match(value.strip()):
            raise ValueError("invalid " + what)
        number = decimal.Decimal(value.strip())
    elif isinstance(value, float):
        # The export is parsed with `parse_float=Decimal`, so a float only reaches here from a
        # caller; an infinity or a NaN is refused rather than carried.
        if not math.isfinite(value):
            raise ValueError("invalid " + what)
        number = decimal.Decimal(str(value))
    else:
        number = decimal.Decimal(value)
    if not number.is_finite():
        raise ValueError("invalid " + what)
    if low is not None and not decimal.Decimal(low) <= number <= decimal.Decimal(high):
        raise ValueError(what + " outside its documented range")
    text = str(number)
    # The emitted cell must still mean this value where it is actually read. The common reader
    # parses a numeric cell as f64, so a magnitude the exporter wrote lexically -- ExifTool 13.59
    # writes `1e999` and `1e-400` into its JSON verbatim -- would arrive there as an infinity or
    # as a zero. Both are refused by name instead of being emitted as a false figure.
    reachable = float(text)
    if not math.isfinite(reachable):
        raise ValueError(what + " cannot be carried: it overflows the reader's float")
    if reachable == 0 and number != 0:
        raise ValueError(what + " cannot be carried: it underflows to zero at the reader")
    return text


def listing(value, allowed):
    return LISTED if value in allowed else UNLISTED


def coordinate(record, name, used):
    """`22.000000°N` as the guide writes it, or a signed number, resolved to signed degrees."""
    value = xmp(record, name, used)
    if value is None:
        return None, ""
    positive, negative, limit = AXIS[name]
    if isinstance(value, str):
        matched = COORDINATE_TEXT.match(value.strip())
        if matched:
            letter = matched.group(2)
            if letter not in (positive, negative):
                raise ValueError(name + " hemisphere belongs to the other axis")
            magnitude = decimal.Decimal(matched.group(1))
            if not 0 <= magnitude <= limit:
                raise ValueError(name + " outside its range")
            return SIGN[letter] * magnitude, "WRITTEN_AS_A_MAGNITUDE_WITH_A_HEMISPHERE_LETTER"
    # The table types these float, so a plain signed number is read as one rather than refused.
    number = decimal.Decimal(documented_number(value, name, -limit, limit))
    return number, "WRITTEN_AS_A_SIGNED_NUMBER_WITHOUT_A_HEMISPHERE_LETTER"


def exif_coordinate(record, tag, used):
    """The EXIF pair, read only to cross-check the XMP value; never a substitute for it."""
    value, _ = resolve(record, "EXIF", "GPS" + tag, used)
    reference, _ = resolve(record, "EXIF", "GPS" + tag + "Ref", used)
    if value is None or reference is None:
        return None
    positive, negative, limit = ("N", "S", 90) if tag == "Latitude" else ("E", "W", 180)
    if not isinstance(reference, str) or reference not in (positive, negative):
        # A latitude cannot be referenced E/W, nor a longitude N/S: such a pair is malformed and
        # must never be allowed to "agree" with the writer's own coordinate.
        raise ValueError("EXIF GPS" + tag + "Ref does not belong to this axis")
    magnitude = decimal.Decimal(documented_number(value, "EXIF GPS" + tag, -limit, limit))
    if magnitude < 0:
        # ExifTool 13.59 records the tag as an unsigned magnitude and signs only the Composite
        # form, so a negative value is the signed one -- and its sign must then agree with the
        # reference rather than contradict it.
        if SIGN[reference] > 0:
            raise ValueError("EXIF GPS" + tag + " is negative under a " + reference + " reference")
        return magnitude
    return SIGN[reference] * magnitude


def band_frequency(value):
    """`860 (+/-26) nm` -> centre and **half** width at half maximum, both in nanometres."""
    if not isinstance(value, str):
        raise ValueError("BandFreq is a string in the documented format")
    matched = BAND_FREQ_TEXT.match(value.strip())
    if not matched:
        raise ValueError("BandFreq is not the documented Central wavelength (+/- HWHM) nm form")
    return decimal.Decimal(matched.group(1)), decimal.Decimal(matched.group(2))


def coefficients(value, what, count):
    """A documented comma-separated numeric list: exact count, exact positions, same order.

    Empty positions are **refused**, not skipped. `1,,2,3,4` is five positions of which one is
    empty, and compacting it to four readings would silently move every later value into the
    wrong slot -- which for `RawData` means reading the Red value as Green. No writer evidence
    for a trailing delimiter was found in the pinned guide, so a trailing empty is refused too.
    """
    if not isinstance(value, str):
        raise ValueError(what + " is a string of comma separated numbers")
    parts = value.split(",")
    if len(parts) != count:
        raise ValueError(what + " does not carry its documented number of values")
    if any(not part.strip() for part in parts):
        raise ValueError(what + " has an empty position, which is not a value")
    return [documented_number(part.strip(), what) for part in parts]


def enumerated(value, table, what, low=0, high=2**31 - 1):
    """A documented integer code with its documented meaning; an unlisted code is retained."""
    if value is None:
        return "", ""
    code = integral(value, what)
    if not low <= code <= high:
        raise ValueError(what + " outside its documented range")
    return str(code), table.get(code, UNLISTED)


def rtk_quality(value):
    """0 / 16 / 32-49 / 50 exactly as the guide defines them; anything else is retained."""
    if value is None:
        return "", ""
    code = integral(value, "RtkFlag")
    if code == 0:
        meaning = "FAILED_TO_POSITION"
    elif code == 16:
        meaning = "SINGLE_POINT_POSITIONING_METER_LEVEL_ACCURACY"
    elif 32 <= code <= 49:
        meaning = "FLOATING_POINT_SOLUTION_DECIMETER_TO_METER_LEVEL_ACCURACY"
    elif code == 50:
        meaning = "FIXED_SOLUTION_POSITIONING_CENTIMETER_LEVEL_ACCURACY"
    else:
        meaning = UNLISTED
    return str(code), meaning


def capture_row(record, index, capture_time_us, export_digest):
    """One saved band image. Absent documented fields stay blank; none becomes a zero."""
    if not isinstance(record, dict):
        raise ValueError("each export entry is an object")
    if len(record) > MAX_TAGS_PER_IMAGE:
        raise ValueError("image record exceeds tag bound")
    used = set()
    version = resolve(record, "ExifTool", "ExifToolVersion", used)[0]
    if version is None:
        raise ValueError("the export must declare ExifToolVersion")
    if decimal.Decimal(documented_number(version, "ExifToolVersion")) != QUALIFIED_EXIFTOOL_VERSION:
        raise ValueError("only the qualified exiftool release is read")
    metadata_version = xmp(record, "Version", used)
    if metadata_version is None:
        raise ValueError("the export must declare the writer's metadata Version")
    # Observed, not assumed: the pinned decoder emits `"XMP:Version": 1.6` as a JSON NUMBER, so a
    # string-only check would refuse every real export. The value is compared exactly, as a
    # decimal, in whichever form it arrives; a different version is not a compatibility claim.
    if decimal.Decimal(documented_number(metadata_version, "Version")) \
            != decimal.Decimal(QUALIFIED_METADATA_VERSION):
        raise ValueError("only the qualified XMP metadata version is read")
    image_source = xmp(record, "ImageSource", used)
    if not isinstance(image_source, str) or not image_source:
        raise ValueError("the export must declare the writer's ImageSource camera type")

    row = dict.fromkeys(COLUMNS, "")
    row.update({
        "record_time_us": capture_time_us, "source_image_index": index,
        "export_sha256": export_digest, "exiftool_version_declared": str(QUALIFIED_EXIFTOOL_VERSION),
        "metadata_version_declared": QUALIFIED_METADATA_VERSION,
        "image_source_hex": hexed(image_source),
        "attitude_basis": ATTITUDE_BASIS, "speed_basis": SPEED_BASIS,
        "altitude_basis": ALTITUDE_BASIS, "irradiance_basis": IRRADIANCE_BASIS,
        "black_level_basis": BLACK_LEVEL_BASIS, "capture_clock_basis": CLOCK_BASIS,
        "reflectance_basis": REFLECTANCE_BASIS,
        "telemetry_basis": TELEMETRY_BASIS,
    })

    band = xmp(record, "BandName", used)
    if not isinstance(band, str) or not band:
        # This path reads a multispectral band capture; an image without one is not refused
        # quietly into a blank row.
        raise ValueError("the export must declare the writer's BandName for a band image")
    row["band_name_reported"] = csv_text(band)
    row["band_name_status"] = listing(band, BAND_NAMES)
    index_code, index_band = enumerated(xmp(record, "Sensorindex", used), SENSOR_INDEX_BAND,
                                        "Sensorindex")
    row["sensor_index_code"], row["sensor_index_band"] = index_code, index_band
    if index_code == "":
        row["band_identity_agreement"] = "NO_SENSOR_INDEX_WAS_WRITTEN_SO_ONLY_THE_NAME_IS_KNOWN"
    elif index_band == UNLISTED:
        row["band_identity_agreement"] = "THE_SENSOR_INDEX_IS_NOT_ONE_THE_GUIDE_LISTS"
    elif index_band != band:
        raise ValueError("BandName and Sensorindex name different bands")
    else:
        row["band_identity_agreement"] = "THE_NAME_AND_THE_INDEX_AGREE"
    frequency = xmp(record, "BandFreq", used)
    if frequency is not None:
        centre, half = band_frequency(frequency)
        row["band_freq_text_hex"] = hexed(frequency)
        row["band_central_wavelength_nm"] = str(centre)
        row["band_half_width_nm"] = str(half)
        row["band_width_basis"] = ("the_guide_states_the_format_is_central_wavelength_plus_or_"
                                   "minus_HWHM_in_nanometres_so_this_is_the_half_width_at_half_"
                                   "maximum_and_never_the_full_width")

    latitude, latitude_basis = coordinate(record, "GPSLatitude", used)
    longitude, longitude_basis = coordinate(record, "GPSLongitude", used)
    if (latitude is None) != (longitude is None):
        raise ValueError("a written coordinate needs both of its axes")
    if latitude is not None:
        row["capture_latitude_deg"] = str(latitude)
        row["capture_longitude_deg"] = str(longitude)
        row["capture_coordinate_basis"] = (latitude_basis if latitude_basis == longitude_basis
                                           else "THE_TWO_AXES_WERE_WRITTEN_IN_DIFFERENT_FORMS")
        checks = [exif_coordinate(record, "Latitude", used),
                  exif_coordinate(record, "Longitude", used)]
        if checks[0] is None and checks[1] is None:
            row["capture_coordinate_agreement"] = "NO_EXIF_GPS_PAIR_WAS_PRESENT_TO_CROSS_CHECK"
        elif None in checks:
            row["capture_coordinate_agreement"] = "ONLY_ONE_EXIF_GPS_AXIS_WAS_PRESENT_TO_CHECK"
        elif checks == [latitude, longitude]:
            row["capture_coordinate_agreement"] = "THE_EXIF_GPS_PAIR_AGREES_WITH_THE_XMP_PAIR"
        else:
            raise ValueError("the EXIF and XMP coordinates disagree")

    status = xmp(record, "GpsStatus", used)
    if status is not None:
        if not isinstance(status, str):
            raise ValueError("GpsStatus is a string")
        row["gps_status_reported"] = csv_text(status)
        row["gps_status_listing"] = listing(status, GPS_STATUS)
    altitude_type = xmp(record, "AltitudeType", used)
    if altitude_type is not None:
        if not isinstance(altitude_type, str):
            raise ValueError("AltitudeType is a string")
        row["altitude_type_reported"] = csv_text(altitude_type)
        row["altitude_type_listing"] = listing(altitude_type, ALTITUDE_TYPE)
    for column, name in (("absolute_altitude_m", "AbsoluteAltitude"),
                         ("relative_altitude_m", "RelativeAltitude")):
        value = xmp(record, name, used)
        if value is not None:
            row[column] = documented_number(value, name, -100_000, 100_000)

    date_stamp = resolve(record, "EXIF", "GPSDateStamp", used)[0]
    time_stamp = resolve(record, "EXIF", "GPSTimeStamp", used)[0]
    if date_stamp is not None:
        row["gps_date_stamp_utc"] = calendar_date(date_stamp, "GPSDateStamp")
    if time_stamp is not None:
        # The adopted helper returns the text with a flag for a 60th second, which is kept as a
        # lexical note rather than normalised away into the next minute.
        text, leap_second = gps_time_of_day(time_stamp)
        row["gps_time_of_day_utc"] = text + (LEAP_SECOND_SUFFIX if leap_second else "")

    row["rtk_flag_code"], row["rtk_positioning_quality"] = rtk_quality(xmp(record, "RtkFlag", used))
    for column, name in (("rtk_std_lat", "RtkStdLat"), ("rtk_std_lon", "RtkStdLon"),
                         ("rtk_std_hgt", "RtkStdHgt"), ("rtk_diff_age", "RtkDiffAge")):
        value = xmp(record, name, used)
        if value is not None:
            row[column] = documented_number(value, name, 0, 1_000_000)
    row["surveying_mode_code"], row["surveying_mode_reported"] = enumerated(
        xmp(record, "SurveyingMode", used), SURVEYING_MODE, "SurveyingMode")

    for column, name in (("gimbal_roll_deg", "GimbalRollDegree"),
                         ("gimbal_yaw_deg", "GimbalYawDegree"),
                         ("gimbal_pitch_deg", "GimbalPitchDegree"),
                         ("flight_roll_deg", "FlightRollDegree"),
                         ("flight_yaw_deg", "FlightYawDegree"),
                         ("flight_pitch_deg", "FlightPitchDegree")):
        value = xmp(record, name, used)
        if value is not None:
            row[column] = documented_number(value, name, -360, 360)
    for column, name in (("flight_north_speed_m_s", "FlightXSpeed"),
                         ("flight_east_speed_m_s", "FlightYSpeed"),
                         ("flight_elevation_speed_m_s", "FlightZSpeed")):
        value = xmp(record, name, used)
        if value is not None:
            row[column] = documented_number(value, name, -1000, 1000)

    irradiance = xmp(record, "Irradiance", used)
    if irradiance is not None:
        row["irradiance_reported"] = documented_number(irradiance, "Irradiance")
    raw = xmp(record, "RawData", used)
    if raw is not None:
        # "Sun sensor raw values. Order: Green, Red, RedEdge, NIR" -- the order is the guide's.
        values = coefficients(raw, "RawData", RAW_DATA_VALUES)
        (row["sun_sensor_raw_green"], row["sun_sensor_raw_red"],
         row["sun_sensor_raw_red_edge"], row["sun_sensor_raw_nir"]) = values
    row["sun_sensor_status_code"], row["sun_sensor_status"] = enumerated(
        xmp(record, "LS_status", used), SUN_SENSOR_STATUS, "LS_status")
    for column, name in (("sun_sensor_type_code", "LS_type"),
                         ("sun_sensor_package_index", "Package_idx"),
                         ("sun_sensor_config_count", "Cfg_cnt")):
        value = xmp(record, name, used)
        if value is not None:
            row[column] = str(integral(value, name))

    for column, name, low, high in (("sensor_gain", "SensorGain", 0, 10_000),
                                    ("sensor_gain_adjustment", "SensorGainAdjustment", 0, 10_000)):
        value = xmp(record, name, used)
        if value is not None:
            row[column] = documented_number(value, name, low, high)
    exposure = xmp(record, "ExposureTime", used)
    if exposure is not None:
        # The guide's unit column says micro-second, and its type column says integer. This is
        # not the EXIF ExposureTime, which is a duration in seconds.
        row["exposure_time_us"] = str(integral(exposure, "ExposureTime"))
    black_level = xmp(record, "BlackLevel", used)
    if black_level is not None:
        row["black_level_code"] = str(integral(black_level, "BlackLevel"))
    bits = resolve(record, "EXIF", "BitsPerSample", used)[0]
    if bits is not None:
        row["bits_per_sample"] = str(integral(bits, "BitsPerSample"))

    row["vignetting_flag_code"], row["vignetting_state"] = enumerated(
        xmp(record, "VignettingFlag", used), VIGNETTING_STATE, "VignettingFlag")
    vignetting = xmp(record, "VignettingData", used)
    if vignetting is not None:
        # The guide writes the coefficients as `(k[0],k[1],k[2],k[3],k[4],k[5])`, so the
        # documented count is six -- not an arbitrary list whose length nobody checks.
        coefficients(vignetting, "VignettingData", VIGNETTING_COEFFICIENTS)
        row["vignetting_coefficients_hex"] = hexed(vignetting)
    row["dewarp_flag_code"], row["dewarp_state"] = enumerated(
        xmp(record, "DewarpFlag", used), DEWARP_STATE, "DewarpFlag")
    row["stored_pixel_basis"] = (
        STORED_PIXEL_UNCOMPENSATED
        if (row["vignetting_flag_code"], row["dewarp_flag_code"]) == ("0", "0")
        else STORED_PIXEL_UNKNOWN)

    # Every key this path did not read is named in the report -- including `SourceFile`, whose
    # value is a live local path and is therefore neither emitted nor digested here. The two
    # counts add up to the keys the export actually carried, so nothing leaves silently.
    retained = sorted(key for key in record if key not in used)
    row["unknown_tag_count"] = len(retained)
    row["interpreted_tag_count"] = len(used)
    return row, [{"field_path": key, "status": RETAINED_IN_ORIGINAL} for key in retained]


def convert(data, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("invalid capture time")
    if len(data) > LIMIT:
        raise ValueError("input exceeds bound")
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        if "\x00" in text:
            raise ValueError("NUL byte in export")
        # `parse_float=Decimal` keeps the exporter's own text: ExifTool 13.59 writes `1e-400`
        # verbatim, which a float parse would turn into a silent zero before anything could
        # notice. The value is then either carried exactly or refused by name.
        document = json.loads(text, object_pairs_hook=unique_object,
                              parse_constant=invalid_constant, parse_float=decimal.Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed export") from exc
    if not isinstance(document, list) or not document:
        raise ValueError("exiftool -json export is a non-empty array of image objects")
    if len(document) > MAX_IMAGES:
        raise ValueError("export exceeds image bound")
    digest = hashlib.sha256(
        data if isinstance(data, (bytes, bytearray)) else data.encode()).hexdigest()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    report = {
        "export_sha256": digest, "export_bytes": len(data), "images": 0,
        "decoder": "caller-supplied exiftool -json -n -G export; no decoder is bundled or "
                   "launched and no image is opened",
        "pinned_writer_contract": GUIDE,
        "pinned_metadata_version": QUALIFIED_METADATA_VERSION,
        "pinned_exiftool_version": str(QUALIFIED_EXIFTOOL_VERSION),
        "produced": "capture fix, band identity and the recorded calibration inputs as written",
        "not_produced": "no reflectance, no radiance, no vegetation index, no aircraft telemetry "
                        "series, no battery state and no aircraft identity binding",
        "bands_seen": [], "retained_tags": [], "unknown_tag_total": 0,
    }
    try:
        for index, record in enumerate(document, start=1):
            row, retained = capture_row(record, index, capture_time_us, digest)
            writer.writerow(row)
            report["images"] += 1
            report["unknown_tag_total"] += len(retained)
            report["bands_seen"].append(row["band_name_reported"])
            report["retained_tags"].append({
                "source_image_index": index,
                "recovery_reference": "export_sha256 + source_image_index",
                "not_interpreted": retained,
            })
            if output.tell() > LIMIT:
                raise ValueError("observation CSV exceeds bound")
        if len(json.dumps(report).encode()) > LIMIT:
            raise ValueError("report exceeds bound")
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("malformed export") from exc
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="saved exiftool -json -n -G export")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller clock for when the export was taken in; never a capture time")
    args = parser.parse_args()
    try:
        with args.export.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        output, report = convert(data, args.capture_time_us)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, RecursionError):
        parser.exit(2, "M3M capture conversion failed; no capture is claimed\n")


if __name__ == "__main__":
    main()
