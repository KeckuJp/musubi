#!/usr/bin/env python3
"""Saved ASPRS LAS point records to common Observation CSV.

Offline and passive: a file already on disk is read. No scanner, no receiver, no stream, no
flight, no reconstruction. A LAS file is a **result a producer already solved and saved** -
lidar returns or a photogrammetrically derived cloud - so nothing here reconstructs a model,
computes a solution or georeferences anything. The producer's own declarations are carried,
and what the file does not state is not supplied.

Every offset, size, field meaning, flag bit, classification value and scaling rule below is
taken from the pinned **ASPRS LAS Specification 1.4 - R16** (revision date 27 August 2025,
PDF build 25 August 2025, ASPRSorg/LAS commit
`a51749076f74ff9ecc9f346236ce9009741350ab`, `LAS_1_4_r16.pdf`, 313504 bytes, sha256
`c66eda174f7fb41e91d8585630642024c458c33f642ee70336948b20f04aa0f1`). No third-party reader
is vendored and no decoder dependency is added: the reader below is stdlib only.

Three things stay apart, because the specification keeps them apart:

* a **source measurement** - what a sensor reported - is not in this file at all;
* a **solved result** - the X/Y/Z the producer computed and stored - is what the rows carry;
* a **declared model** - the coordinate reference the file names, and the word the producer
  chose for a point's class - is a declaration, never a verified datum or a verified surface.

So: the coordinate reference arrives as WKT text, which is **retained and never parsed**, or as
GeoTIFF keys, of which exactly one qualified subset is read - a projected 2D Model CRS whose
own linear units key carries EPSG UOM code 9001, pinned from the OGC GeoTIFF 1.1 standard. Only
then are the metre columns stated, as the same numbers on the same axes. Everywhere else the
**units of X, Y and Z are not asserted** - metre is not assumed, and Z is not called
ellipsoidal, orthometric or mean sea level. This is not georeferencing: no axis is renamed, no
datum is resolved, no register is consulted, nothing is reprojected, and an unknown, missing or
conflicting declaration stays unresolved instead of becoming a success. Classification value 2 is the
word "Ground" as that producer classified it, not a measured ground surface. The Synthetic bit
says the producer created that point "by a technique other than direct observation such as
digitized from a photogrammetric stereo model", which is exactly the measurement/result
boundary, so it travels per point and is never folded into the class value. The declared
Min/Max extents are the file's own bounding numbers and are never presented as the shape of a
corridor, a structure or anything mapped.

A LAS file carries a GPS **time tag** and no fix status, no standard deviation and no accuracy
statement of any kind, so none is reported. No UTC conversion, no leap second and no week
rollover arithmetic is applied to that tag.
"""
import argparse
import csv
import decimal
import hashlib
import io
import json
import math
import struct
import sys
from pathlib import Path

try:
    from scripts.recorded_units import UNITS
except ModuleNotFoundError:  # running the script directly from scripts/
    from recorded_units import UNITS

REFERENCE = "ASPRS LAS Specification 1.4 - R16 (ASPRSorg/LAS@a51749076f74ff9ecc9f346236ce9009741350ab)"
SIGNATURE = b"LASF"
QUALIFIED_VERSION = (1, 4)
# "For LAS 1.4 this size is 375 bytes." A header that declares another size is not this layout.
SPEC_HEADER_SIZE = 375
HEADER = ("<4sHHIHH8sBB32s32sHHHIIBHI5I"      # signature .. legacy points by return
          "3d3d6d"                             # scale, offset, max/min X, Y, Z
          "QQIQ15Q")                           # waveform, first EVLR, EVLR count, points, returns
VLR_HEADER = "<H16sHH32s"                     # reserved, user id, record id, length, description
VLR_HEADER_SIZE = struct.calcsize(VLR_HEADER)  # the specification states 54 bytes
# Point Data Record Formats 0 and 1 only. Format 1 is format 0 "with the addition of GPS Time".
POINT_FORMATS = {0: ("<iiiHBBbBH", 20), 1: ("<iiiHBBbBHd", 28)}
CRS_USER_ID = "LASF_Projection"
WKT_COORDINATE_SYSTEM_RECORD = 2112
WKT_MATH_TRANSFORM_RECORD = 2111
GEOTIFF_KEY_DIRECTORY_RECORD = 34735
GEOTIFF_DOUBLE_PARAMS_RECORD = 34736
GEOTIFF_ASCII_PARAMS_RECORD = 34737
CRS_RECORDS = (WKT_COORDINATE_SYSTEM_RECORD, WKT_MATH_TRANSFORM_RECORD,
               GEOTIFF_KEY_DIRECTORY_RECORD, GEOTIFF_DOUBLE_PARAMS_RECORD,
               GEOTIFF_ASCII_PARAMS_RECORD)
# The one further pinned definition, used for the small qualified subset of the GeoTIFF key
# declaration below and for nothing else: OGC GeoTIFF standard 1.1 (OGC 19-008r4).
GEOTIFF_REFERENCE = "OGC GeoTIFF 1.1 (OGC 19-008r4)"
# Requirement 8.1/8.4 (GTModelTypeGeoKey), 12.1 (ProjectedCRSGeoKey), 13.1 (GeodeticCRSGeoKey),
# 14.1 (VerticalGeoKey) and 16.1 (the units keys). Only these six are read, and only when the
# directory stores the value in place; every other key is counted and left alone.
MODEL_TYPE_KEY = 1024
GEODETIC_CRS_KEY = 2048
PROJECTED_CRS_KEY = 3072
PROJ_LINEAR_UNITS_KEY = 3076
VERTICAL_CRS_KEY = 4096
VERTICAL_UNITS_KEY = 4099
QUALIFIED_KEYS = (MODEL_TYPE_KEY, GEODETIC_CRS_KEY, PROJECTED_CRS_KEY, PROJ_LINEAR_UNITS_KEY,
                  VERTICAL_CRS_KEY, VERTICAL_UNITS_KEY)
# Requirement 8.4, verbatim values: 0 undefined or unknown, 1 projected 2D, 2 geographic 2D,
# 3 geocentric Cartesian 3D, 32767 user-defined.
MODEL_TYPES = {0: "MODEL_CRS_UNDEFINED_OR_UNKNOWN_DECLARED", 1: "MODEL_CRS_PROJECTED_2D_DECLARED",
               2: "MODEL_CRS_GEOGRAPHIC_2D_DECLARED", 3: "MODEL_CRS_GEOCENTRIC_3D_DECLARED",
               32767: "MODEL_CRS_USER_DEFINED_DECLARED"}
PROJECTED_2D = 1
USER_DEFINED = 32767
# Requirement 16.5: a units key value in 1024-32766 "SHALL be EPSG Unit Of Measure (UOM) codes
# with type = length". The pinned standard gives the codes' authority, not their size: the ratio
# of any such unit to the metre lives in the EPSG register, which is NOT pinned here. So exactly
# one code is resolved - 9001, which the standard's own worked examples annotate as
# "ProjLinearUnitsGeoKey = 9001 (Linear_Meter)" and "VerticalUnitsGeoKey = 9001 (Linear_Meter)" -
# and it needs no arithmetic at all, because the declared unit already is the common one. Any
# other length code is retained unresolved rather than converted with a factor nobody pinned.
METRE_UNIT_CODE = 9001
# The common unit vocabulary is the repository's own table rather than a second one invented
# here. "m" must stay an identity mapping onto distance_m: this path emits the file's own metre
# values unchanged, so if that entry ever gained a scale or an offset the emitted columns would
# silently stop meaning metres.
METRE_UNIT = "m"
METRE_FIELD, METRE_SCALE, METRE_OFFSET = UNITS[METRE_UNIT]
# Table 9, ASPRS Standard Point Classes for Point Data Record Formats 0-5. Values 10, 11 and
# 13-31 are "Reserved for ASPRS Definition" there, so they are retained as reserved and never
# renamed to the nearest listed class.
POINT_CLASSES = {0: "CREATED_NEVER_CLASSIFIED", 1: "UNCLASSIFIED", 2: "GROUND",
                 3: "LOW_VEGETATION", 4: "MEDIUM_VEGETATION", 5: "HIGH_VEGETATION",
                 6: "BUILDING", 7: "LOW_POINT_NOISE", 8: "MODEL_KEY_POINT_MASS_POINT",
                 9: "WATER", 12: "OVERLAP_POINTS"}
RESERVED_CLASS = "RESERVED_FOR_ASPRS_DEFINITION_RETAINED"
# This path's own bounds, not limits the specification states, and not a statement that a
# survey cloud of ordinary size fits: real clouds run to hundreds of millions of points, which
# this path refuses rather than truncates. The row bound is DERIVED from the output bound so the
# two cannot disagree - a declared count this path accepts is a count it can actually finish.
# One point row is about 1.5 kB of CSV, most of it the fixed basis strings; 2048 bytes is the
# upper bound assumed per row, and the in-loop output check below is the backstop if that ever
# proves optimistic.
MAX_INPUT = 64 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_ROW_BYTES = 2048
MAX_POINT_ROWS = MAX_OUTPUT // MAX_ROW_BYTES
MAX_VLR_COUNT = 1_000
# The specification's own limit: a VLR is "limited to a data payload of 65,535 bytes".
MAX_VLR_PAYLOAD = 65_535
# Wide enough that a 17 significant digit scale times a 17 significant digit record value plus
# an offset is exact rather than quietly rounded to the default 28 digit context.
SCALING_PRECISION = 60

CAPTURE_BASIS = ("the_common_capture_clock_is_a_caller_declaration_and_never_the_producers_"
                 "clock_a_saved_point_cloud_carries_no_capture_time_of_its_own")
COORDINATE_BASIS = ("REPORTED_COORDINATE_OF_A_SOLVED_RESULT_SCALED_BY_THE_FILES_OWN_SCALE_AND_"
                    "OFFSET_IN_THE_UNITS_OF_THE_COORDINATE_REFERENCE_THE_FILE_DECLARES_WHICH_IS_"
                    "NOT_RESOLVED_HERE_SO_NO_UNIT_IS_ASSERTED_AND_Z_IS_NOT_CALLED_ELLIPSOIDAL_"
                    "ORTHOMETRIC_OR_MEAN_SEA_LEVEL")
CRS_BASIS = ("DECLARED_COORDINATE_REFERENCE_RETAINED_NOT_PARSED_AND_NOT_VERIFIED_NO_DATUM_IS_"
             "NAMED_NO_PROJECTION_IS_APPLIED_AND_NOTHING_IS_TRANSFORMED_HERE")
CLASS_BASIS = ("THE_PRODUCERS_OWN_CLASSIFICATION_WORD_FOR_THIS_POINT_NEVER_A_VERIFIED_SURFACE_"
               "A_VERIFIED_OBJECT_OR_A_CONDITION_ASSESSED_HERE_AND_THE_SYNTHETIC_KEY_POINT_AND_"
               "WITHHELD_FLAGS_ARE_SEPARATE_BITS_NEVER_FOLDED_INTO_THE_CLASS_VALUE")
EXTENT_BASIS = ("THE_FILES_OWN_DECLARED_BOUNDING_NUMBERS_IN_THE_SAME_UNRESOLVED_COORDINATE_"
                "REFERENCE_NEVER_THE_SHAPE_OF_A_MAPPED_OBJECT_A_CORRIDOR_CENTRELINE_OR_A_"
                "SURVEYED_BOUNDARY")
GPS_TIME_BASIS = ("THE_TIME_TAG_THE_PRODUCER_WROTE_IN_THE_SCALE_THE_GLOBAL_ENCODING_BIT_"
                  "DECLARES_NO_UTC_NO_LEAP_SECOND_AND_NO_WEEK_ROLLOVER_ARITHMETIC_IS_APPLIED_"
                  "AND_IT_IS_NEVER_A_FIX_STATUS_A_STANDARD_DEVIATION_OR_AN_ACCURACY")
SOURCE_BASIS = ("THE_REPORTED_POINT_SOURCE_ID_WHICH_THE_SPECIFICATION_SAYS_IS_TYPICALLY_A_"
                "FLIGHT_LINE_SORTIE_ROUTE_OR_SETUP_IDENTIFIER_AND_RESERVES_ZERO_AN_IDENTIFIER_"
                "THE_PRODUCER_WROTE_AND_NEVER_AN_AUTHENTICATED_IDENTITY")
CREATION_BASIS = ("THE_TWO_NUMBERS_THE_FILE_WROTE_A_GREENWICH_MEAN_TIME_DAY_OF_YEAR_WHERE_"
                  "JANUARY_1_IS_DAY_1_AND_A_FOUR_DIGIT_YEAR_KEPT_APART_AND_NEVER_COMPOSED_INTO_"
                  "A_DATE_OR_A_TIME_OF_ANY_SURVEY")
RECORD_BASIS = ("every_row_is_an_OBSERVED_RECORD_read_from_a_result_a_producer_already_solved_"
                "and_saved_it_is_never_a_measurement_made_here_never_a_reconstruction_never_a_"
                "georeferencing_and_never_an_accuracy_statement")
AXIS_BASIS = ("THE_MODEL_CRS_AXES_THE_DECLARED_COORDINATE_REFERENCE_DEFINES_IN_THE_ORDER_THE_"
              "FILE_STORED_THEM_NO_AXIS_IS_RENAMED_EASTING_NORTHING_LATITUDE_OR_LONGITUDE_HERE_"
              "NOTHING_IS_REPROJECTED_AND_NO_WGS84_OR_OTHER_FRAME_IS_IMPLIED")
UNQUALIFIED_UNIT_BASIS = ("UNIT_UNRESOLVED_THE_SAVED_DECLARATION_DOES_NOT_MEET_THE_ONE_"
                          "QUALIFIED_SUBSET_THIS_PATH_RESOLVES_SO_NO_METRE_VALUE_IS_STATED")
# The two ways a qualifying metre declaration can arise, kept apart because the pinned standard
# treats them differently: the units key defines the axis unit of a USER-DEFINED projected CRS
# (Requirement 12.5 with 16.8), while for an EPSG-coded CRS the register defines it and the
# register is not pinned here, so a units key beside an EPSG code is the file's own restatement
# and is reported as exactly that.
UNIT_BY_FILE_BASIS = ("METRE_DECLARED_BY_THE_FILE_ITSELF_AS_THE_LINEAR_UNIT_OF_ITS_USER_DEFINED_"
                      "PROJECTED_CRS_EPSG_UOM_CODE_9001_NO_REGISTER_WAS_CONSULTED_AND_NO_DATUM_"
                      "IS_RESOLVED")
UNIT_BESIDE_CODE_BASIS = ("METRE_DECLARED_BY_THE_FILES_OWN_UNITS_KEY_EPSG_UOM_CODE_9001_BESIDE_"
                          "AN_EPSG_CRS_CODE_WHOSE_REGISTER_IS_NOT_PINNED_HERE_SO_THE_REGISTER_"
                          "WAS_NOT_CONSULTED_AND_THE_TWO_WERE_NOT_CROSS_CHECKED")
VERTICAL_WITHOUT_CRS = ("VERTICAL_UNIT_DECLARED_BUT_NO_VERTICAL_CRS_DECLARED_SO_THE_HEIGHT_IS_A_"
                        "METRE_LENGTH_ON_AN_UNRESOLVED_VERTICAL_REFERENCE_NEVER_ELLIPSOIDAL_"
                        "ORTHOMETRIC_OR_MEAN_SEA_LEVEL")
VERTICAL_WITH_CRS = ("VERTICAL_UNIT_DECLARED_BESIDE_A_DECLARED_VERTICAL_CRS_CODE_WHICH_IS_"
                     "RETAINED_AS_A_CODE_AND_NOT_RESOLVED_TO_A_DATUM_HERE")

COLUMNS = ("record_time_us", "capture_clock_basis", "source_file_sha256", "las_record_kind",
           "las_version_declared", "file_source_id_reported", "global_encoding_code",
           "system_identifier_hex", "generating_software_hex",
           "creation_day_of_year_reported", "creation_year_reported", "creation_date_basis",
           "point_data_record_format", "point_data_record_length", "point_record_extra_bytes",
           "point_count_declared", "legacy_point_count_declared", "point_count_disposition",
           "point_records_read", "chunk_first_point_index", "chunk_last_point_index",
           "chunk_point_count", "coordinate_reference_kind", "coordinate_reference_text_hex",
           "coordinate_reference_key_count", "extended_record_count_declared",
           "coordinate_reference_basis", "model_type_code", "model_type_declared",
           "projected_crs_code", "geodetic_crs_code", "vertical_crs_code",
           "horizontal_linear_unit_code", "vertical_linear_unit_code",
           "horizontal_unit_basis", "vertical_unit_basis", "axis_basis",
           "declared_extent_min_x", "declared_extent_max_x",
           "declared_extent_min_y", "declared_extent_max_y", "declared_extent_min_z",
           "declared_extent_max_z", "extent_basis", "gps_time_type_declared",
           "synthetic_return_numbers_declared", "point_index", "point_x", "point_y", "point_z",
           "point_x_m", "point_y_m", "point_z_m",
           "point_coordinate_basis", "point_intensity_reported", "point_return_number",
           "point_return_count", "point_return_disposition", "point_scan_direction_code",
           "point_edge_of_flight_line_code", "point_classification_value",
           "point_classification_name", "point_synthetic_flag", "point_key_point_flag",
           "point_withheld_flag", "point_classification_basis", "point_scan_angle_rank_deg",
           "point_scan_angle_disposition", "point_user_data_reported",
           "point_source_id_reported", "point_source_basis", "point_gps_time_reported",
           "gps_time_basis", "record_basis")


def text_hex(raw):
    """A fixed length char array as utf8 hex, or None when it is entirely null.

    The specification warns these arrays "will not be null-terminated if all bytes are
    utilized", so the bytes up to the first null are taken when there is one and all 32
    otherwise. Hex keeps a comma, a quote or a control byte from reshaping a CSV row; the
    common reader refuses quoted input, so the bytes travel as hex rather than as text.
    """
    end = raw.find(b"\0")
    kept = raw if end < 0 else raw[:end]
    return "hex:" + kept.hex() if kept else None


def scaled(record_value, scale, offset, axis):
    """`X_coordinate = (X_record * X_scale) + X_offset`, exactly as the specification states.

    The scale and offset are IEEE doubles. Their shortest round-trip decimal spelling is what
    the producer wrote, so `repr` is what enters Decimal; the exact binary expansion would add
    seventeen digits of noise to every coordinate. The arithmetic itself runs in a wide local
    context so the product is exact rather than silently rounded.
    """
    with decimal.localcontext() as context:
        context.prec = SCALING_PRECISION
        value = decimal.Decimal(record_value) * decimal.Decimal(repr(scale)) \
            + decimal.Decimal(repr(offset))
    # The common reader parses this text as f64, so a value it cannot carry is refused rather
    # than written as inf.
    carried = float(value)
    if not math.isfinite(carried):
        raise ValueError(axis + " exceeds what the common reader can carry once the file's own "
                         "scale and offset are applied")
    # The other direction is checked on the arithmetic's own result, not argued from its inputs:
    # the product alone cannot underflow, but a product that nearly cancels against the offset
    # can leave a residual below the smallest subnormal, and writing that as 0 would state a
    # coordinate the file does not carry.
    if value != 0 and carried == 0:
        raise ValueError(axis + " is not zero in the file, but underflows to zero once the "
                         "file's own scale and offset are applied, so it cannot be carried")
    return value


def finite_double(value, what):
    """A double the output depends on; nan or inf is refused, not carried.

    The common reader parses these columns as f64 text, and `nan` or `inf` would either be
    rejected there or read as a number nobody wrote.
    """
    if not math.isfinite(value):
        raise ValueError("the declared " + what + " is not a finite number")
    return value


def read_header(raw):
    """The Public Header Block, qualified against the one version this path reads."""
    if len(raw) < SPEC_HEADER_SIZE:
        raise ValueError("saved input is shorter than a LAS public header block")
    fields = struct.unpack_from(HEADER, raw, 0)
    (signature, file_source_id, global_encoding, guid_1, guid_2, guid_3, guid_4,
     version_major, version_minor, system_identifier, generating_software,
     creation_day, creation_year, header_size, offset_to_points, vlr_count,
     point_format, point_length, legacy_count) = fields[:19]
    legacy_by_return = fields[19:24]
    scales, offsets = fields[24:27], fields[27:30]
    max_x, min_x, max_y, min_y, max_z, min_z = fields[30:36]
    waveform_start, evlr_start, evlr_count, point_count = fields[36:40]
    by_return = fields[40:55]
    if signature != SIGNATURE:
        raise ValueError("saved input does not carry the LAS file signature")
    if (version_major, version_minor) != QUALIFIED_VERSION:
        # Not a statement about LAS in general: another version's header is a different layout
        # and reading it with these offsets would report fields that were never written.
        raise ValueError("this path reads the LAS 1.4 public header block only, and the file "
                         "declares version %d.%d" % (version_major, version_minor))
    if header_size != SPEC_HEADER_SIZE:
        raise ValueError("the declared header size is not the %d bytes the pinned specification "
                         "states for LAS 1.4" % SPEC_HEADER_SIZE)
    if point_format not in POINT_FORMATS:
        # The other formats carry fields this path does not decode. That is a bound of this
        # path, not a judgement that those formats are unsupported anywhere.
        raise ValueError("this path decodes point data record formats 0 and 1, and the file "
                         "declares format %d" % point_format)
    minimum = POINT_FORMATS[point_format][1]
    if point_length < minimum:
        raise ValueError("the declared point data record length is shorter than the minimum the "
                         "pinned specification states for the declared format")
    for axis, scale in zip("xyz", scales):
        finite_double(scale, axis + " scale factor")
        if scale == 0:
            # A zero scale maps every stored integer onto the offset, which would report one
            # position for the whole cloud.
            raise ValueError("the declared " + axis + " scale factor is zero, which would erase "
                             "every stored coordinate on that axis")
    for axis, offset in zip("xyz", offsets):
        finite_double(offset, axis + " offset")
    for name, value in zip(("max x", "min x", "max y", "min y", "max z", "min z"),
                           (max_x, min_x, max_y, min_y, max_z, min_z)):
        finite_double(value, name + " extent")
    if not SPEC_HEADER_SIZE <= offset_to_points <= len(raw):
        raise ValueError("the declared offset to point data does not lie inside the saved file")
    if vlr_count > MAX_VLR_COUNT:
        raise ValueError("the declared variable length record count exceeds this path's bound")
    return dict(file_source_id=file_source_id, global_encoding=global_encoding,
                project_id_hex=(struct.pack("<IHH", guid_1, guid_2, guid_3) + guid_4).hex(),
                system_identifier=system_identifier, generating_software=generating_software,
                creation_day=creation_day, creation_year=creation_year,
                offset_to_points=offset_to_points, vlr_count=vlr_count,
                point_format=point_format, point_length=point_length,
                legacy_count=legacy_count, legacy_by_return=list(legacy_by_return),
                scales=scales, offsets=offsets, extents=(min_x, max_x, min_y, max_y, min_z, max_z),
                waveform_start=waveform_start, evlr_start=evlr_start, evlr_count=evlr_count,
                point_count=point_count, by_return=list(by_return))


def read_records(raw, header):
    """Every variable length record between the header and the point data, in file order."""
    found, offset, limit = [], SPEC_HEADER_SIZE, header["offset_to_points"]
    for _ in range(header["vlr_count"]):
        if offset + VLR_HEADER_SIZE > limit:
            raise ValueError("a declared variable length record runs past the start of the point "
                             "data, so the declared record count cannot be read")
        _, user_id, record_id, length, description = struct.unpack_from(VLR_HEADER, raw, offset)
        if length > MAX_VLR_PAYLOAD:
            raise ValueError("a variable length record declares a payload larger than the "
                             "65535 bytes the pinned specification allows")
        start = offset + VLR_HEADER_SIZE
        if start + length > limit:
            raise ValueError("a variable length record payload runs past the start of the point "
                             "data")
        found.append(dict(user_id=user_id.split(b"\0")[0].decode("ascii", "replace"),
                          record_id=record_id, length=length,
                          description_hex=text_hex(description), payload=raw[start:start + length]))
        offset = start + length
    return found, limit - offset


def unresolved(kind, **extra):
    """A coordinate reference this path does not resolve: no metre value is stated from it."""
    declaration = dict(kind=kind, wkt_hex=None, key_count=None, model_type=None,
                       projected_crs=None, geodetic_crs=None, vertical_crs=None,
                       horizontal_unit=None, vertical_unit=None,
                       horizontal_basis=UNQUALIFIED_UNIT_BASIS,
                       vertical_basis=UNQUALIFIED_UNIT_BASIS, metre_xy=False, metre_z=False)
    declaration.update(extra)
    return declaration


def geo_keys(payload):
    """The GeoKeyDirectoryTag entries this path reads, or a reason it reads none.

    Returns `(keys, count, note)`. The directory's own four-short header is checked first: the
    pinned standard requires KeyDirectoryVersion 1 (Req. 2.5), KeyRevision 1 (Req. 2.7) and a
    MinorRevision of 0 or 1 (Req. 2.9). Anything else is a directory whose layout this pin does
    not define, so its entries are not interpreted at all rather than read as if they were.

    Only the six keys named above are taken, and only when the directory stores the value in
    place - `wTIFFTagLocation == 0`, `wCount == 1`, which Req. 2.16 makes the shape that holds
    the value itself. A key whose value lives in the GeoDoubleParams or GeoAsciiParams record is
    **not** followed, because those records are not read here.

    An occurrence this path cannot read does not just disappear: it **blocks its key**. A key
    that appears once out of line and once in place is left unresolved, because letting the
    readable occurrence win would decide a declaration from the half that happened to be
    convenient. A key that appears twice in place with different values makes the whole
    declaration conflicting.
    """
    if len(payload) < 8:
        raise ValueError("the declared GeoTIFF key directory record is too short to carry its "
                         "own key count")
    version, revision, minor, count = struct.unpack_from("<4H", payload, 0)
    if len(payload) != 8 + 8 * count:
        raise ValueError("the declared GeoTIFF key directory length does not match the number "
                         "of keys it declares")
    if (version, revision) != (1, 1) or minor not in (0, 1):
        return {}, count, "HEADER_NOT_A_DEFINED_SHAPE"
    keys, blocked = {}, set()
    for index in range(count):
        key, location, entries, value = struct.unpack_from("<4H", payload, 8 + 8 * index)
        if key not in QUALIFIED_KEYS:
            continue
        if location != 0 or entries != 1:
            blocked.add(key)
            continue
        if key in keys and keys[key] != value:
            return {}, count, "CONFLICTING_KEY_VALUES"
        keys[key] = value
    for key in blocked:
        # Dropped after the walk, so the order the two occurrences appear in cannot change the
        # outcome: an unreadable occurrence leaves its key unresolved either way.
        keys.pop(key, None)
    return keys, count, ("QUALIFIED_KEY_STORED_OUTSIDE_THE_DIRECTORY" if blocked else None)


def geotiff_declaration(payload):
    """The one qualified subset of a GeoTIFF declaration this path resolves.

    Resolved only when the file's own keys say so: a projected 2D Model CRS whose linear units
    key carries EPSG UOM code 9001. Nothing is reprojected, no register is consulted, no datum
    is named, and an axis is never renamed to easting, northing, latitude or longitude. Every
    other shape - unknown, missing, conflicting, a unit code whose size this pin does not give,
    a geographic or geocentric model whose axes are not linear at all - stays unresolved.
    """
    keys, count, note = geo_keys(payload)
    if note == "CONFLICTING_KEY_VALUES":
        return unresolved("GEOTIFF_DECLARED_KEYS_CONFLICT_SO_THE_REFERENCE_IS_UNRESOLVED",
                          key_count=count)
    if note == "HEADER_NOT_A_DEFINED_SHAPE":
        return unresolved("GEOTIFF_KEY_DIRECTORY_HEADER_IS_NOT_A_SHAPE_THE_PIN_DEFINES_SO_"
                          "NOTHING_IN_IT_IS_INTERPRETED", key_count=count)
    model = keys.get(MODEL_TYPE_KEY)
    projected, vertical = keys.get(PROJECTED_CRS_KEY), keys.get(VERTICAL_CRS_KEY)
    horizontal_unit, vertical_unit = keys.get(PROJ_LINEAR_UNITS_KEY), keys.get(VERTICAL_UNITS_KEY)
    kind = "GEOTIFF_DECLARED_QUALIFIED_SUBSET_READ"
    if note:
        kind = "GEOTIFF_DECLARED_A_QUALIFIED_KEY_LIVES_IN_A_RECORD_NOT_READ_HERE"
    declaration = unresolved(kind, key_count=count, model_type=model, projected_crs=projected,
                             geodetic_crs=keys.get(GEODETIC_CRS_KEY), vertical_crs=vertical,
                             horizontal_unit=horizontal_unit, vertical_unit=vertical_unit)
    if (METRE_SCALE, METRE_OFFSET) != (1, 0):
        raise ValueError("the shared unit table no longer maps " + METRE_UNIT + " onto itself, "
                         "so this path can no longer emit the file's own metre values unchanged")
    # Horizontal: the units key states the axis unit of a projected 2D Model CRS. Requirement 8.7
    # makes a ProjectedCRSGeoKey mandatory for that model type, so a file that claims projected
    # 2D without one is not resolved either.
    if model == PROJECTED_2D and projected is not None and horizontal_unit == METRE_UNIT_CODE:
        declaration["metre_xy"] = True
        declaration["horizontal_basis"] = (UNIT_BY_FILE_BASIS if projected == USER_DEFINED
                                           else UNIT_BESIDE_CODE_BASIS)
    # Vertical: an independent declaration. A height's unit can be stated while its reference is
    # not, and that stays visible rather than becoming a datum nobody declared.
    if vertical_unit == METRE_UNIT_CODE:
        declaration["metre_z"] = True
        declaration["vertical_basis"] = (VERTICAL_WITH_CRS if vertical is not None
                                         else VERTICAL_WITHOUT_CRS)
    return declaration


def coordinate_reference(header, records):
    """What the file declares its coordinate reference to be, and what of it is resolved."""
    wkt_bit = bool(header["global_encoding"] & (1 << 4))
    owned = [record for record in records
             if record["user_id"] == CRS_USER_ID and record["record_id"] in CRS_RECORDS]
    wkt = [record for record in owned if record["record_id"] == WKT_COORDINATE_SYSTEM_RECORD]
    geotiff = [record for record in owned if record["record_id"] == GEOTIFF_KEY_DIRECTORY_RECORD]
    # "It is considered a file error to have more than one GeoTIFF (E)VLR or more than one WKT
    # (E)VLR in the file."
    if len(wkt) > 1 or len(geotiff) > 1:
        raise ValueError("the file carries more than one coordinate reference record of the same "
                         "kind, which the pinned specification calls a file error")
    # "Point Record Formats 0-5 can use either GeoTIFF or WKT, but not both simultaneously."
    if wkt and geotiff:
        raise ValueError("the file carries both a WKT and a GeoTIFF coordinate reference record, "
                         "which the pinned specification does not allow simultaneously")
    if wkt_bit and wkt:
        # A WKT string is retained whole and never parsed, so nothing about its axes, units or
        # datum is stated here and no metre value is derived from it.
        return unresolved("WKT_DECLARED_RETAINED_NOT_PARSED", wkt_hex="hex:" + wkt[0]["payload"].hex())
    if not wkt_bit and geotiff:
        return geotiff_declaration(geotiff[0]["payload"])
    if (wkt_bit and geotiff) or (not wkt_bit and wkt):
        # The Global Encoding bit and the record that is actually present disagree.
        return unresolved("COORDINATE_REFERENCE_KIND_AND_RECORD_DISAGREE_SO_IT_IS_UNRESOLVED")
    if header["evlr_count"]:
        # Absent from the variable length records is not absent from the file: the CRS may sit
        # in an extended record after the point data, which this path does not read.
        return unresolved("COORDINATE_REFERENCE_NOT_IN_THE_RECORDS_READ_AND_EXTENDED_RECORDS_EXIST")
    return unresolved("COORDINATE_REFERENCE_ABSENT_FROM_WHAT_WAS_READ_THOUGH_REQUIRED")


def point_span(header, raw, available):
    """How many bytes the declared point records occupy, refused if they leave the file.

    One implementation of that rule, so a caller that schedules work before converting anything
    - the bounded segment wrapper - asks the same question this path answers, rather than
    carrying a second decoder's opinion. The arithmetic is exact and constant time: a header may
    declare a 64 bit point count, and multiplying it is cheap, while trusting it is not.
    """
    span = available * header["point_length"]
    if header["offset_to_points"] + span > len(raw):
        raise ValueError("the declared point records run past the end of the saved file")
    return span


def point_count(header):
    """How many point records to read, and the disposition of the two declared counts."""
    declared, legacy = header["point_count"], header["legacy_count"]
    if legacy == 0:
        return declared, "LEGACY_COUNT_ZERO_SO_BACKWARD_COMPATIBILITY_IS_NOT_MAINTAINED"
    if legacy == declared:
        return declared, "LEGACY_AND_CURRENT_COUNTS_AGREE"
    # "If there is a discrepancy between a non-zero legacy field and the equivalent LAS 1.4
    # field, the LAS 1.4 reader should use the legacy value." Both numbers are reported, the
    # disagreement is named, and neither is quietly chosen.
    return legacy, ("LEGACY_AND_CURRENT_COUNTS_DISAGREE_THE_PINNED_SPECIFICATION_DIRECTS_THE_"
                    "LEGACY_VALUE_AND_BOTH_ARE_REPORTED")


def convert(raw, capture_time_us, first_point=0, point_limit=None):
    """One declaration row for the file, then one row per saved point record, in file order.

    `first_point` and `point_limit` select a **segment** of the saved point list. They exist so
    a caller can carry a cloud larger than this path's per-call row bound without dropping,
    thinning or renumbering anything: every row still carries its **absolute** original index,
    every segment repeats the file's own declaration, and the segment's own range is declared in
    that row. A segment is never a sample and never a subset of interest; the caller that walks
    them is `scripts/export_point_cloud_chunks.py`, which reconciles their totals.
    """
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit caller capture microseconds required")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized saved LAS input")
    if type(first_point) is not int or first_point < 0:
        raise ValueError("a segment starts at a non-negative saved point index")
    if point_limit is not None and (type(point_limit) is not int or point_limit <= 0):
        raise ValueError("a segment carries a positive number of saved points")
    header = read_header(raw)
    records, gap = read_records(raw, header)
    declared = coordinate_reference(header, records)
    available, disposition = point_count(header)
    if first_point > available:
        raise ValueError("a segment starts past the point records the file declares")
    reading = available - first_point if point_limit is None else min(point_limit,
                                                                     available - first_point)
    if reading > MAX_POINT_ROWS:
        # Refused rather than truncated: emitting the first rows and stopping would report the
        # rest of a saved cloud as absent. A caller with more points than this reads them as
        # consecutive segments, which loses nothing.
        raise ValueError("the declared point record count exceeds this path's own bound of %d "
                         "rows" % MAX_POINT_ROWS)
    layout, minimum = POINT_FORMATS[header["point_format"]]
    length = header["point_length"]
    span = point_span(header, raw, available)
    start = header["offset_to_points"] + first_point * length
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    minimum_x, maximum_x, minimum_y, maximum_y, minimum_z, maximum_z = header["extents"]
    fixed = {"record_time_us": capture_time_us, "capture_clock_basis": CAPTURE_BASIS,
             "source_file_sha256": digest, "record_basis": RECORD_BASIS}
    time_type = ("ADJUSTED_STANDARD_GPS_TIME_DECLARED" if header["global_encoding"] & 1
                 else "GPS_WEEK_TIME_DECLARED")
    writer.writerow(dict(
        fixed, las_record_kind="FILE_DECLARATION",
        las_version_declared="%d.%d" % QUALIFIED_VERSION,
        file_source_id_reported=header["file_source_id"],
        global_encoding_code=header["global_encoding"],
        system_identifier_hex=text_hex(header["system_identifier"]),
        generating_software_hex=text_hex(header["generating_software"]),
        creation_day_of_year_reported=header["creation_day"],
        creation_year_reported=header["creation_year"], creation_date_basis=CREATION_BASIS,
        point_data_record_format=header["point_format"], point_data_record_length=length,
        point_record_extra_bytes=length - minimum,
        point_count_declared=header["point_count"], legacy_point_count_declared=header["legacy_count"],
        point_count_disposition=disposition, point_records_read=reading,
        # The segment's own range, in the saved file's own indices, so a chunk can never be
        # mistaken for the whole cloud and its place in it is never ambiguous.
        chunk_first_point_index=first_point,
        chunk_last_point_index=(first_point + reading - 1) if reading else None,
        chunk_point_count=reading,
        coordinate_reference_kind=declared["kind"],
        coordinate_reference_text_hex=declared["wkt_hex"],
        coordinate_reference_key_count=declared["key_count"],
        extended_record_count_declared=header["evlr_count"], coordinate_reference_basis=CRS_BASIS,
        model_type_code=declared["model_type"],
        model_type_declared=(None if declared["model_type"] is None else
                             MODEL_TYPES.get(declared["model_type"],
                                             "MODEL_TYPE_CODE_RETAINED_NOT_LISTED_BY_THE_PIN")),
        projected_crs_code=declared["projected_crs"], geodetic_crs_code=declared["geodetic_crs"],
        vertical_crs_code=declared["vertical_crs"],
        horizontal_linear_unit_code=declared["horizontal_unit"],
        vertical_linear_unit_code=declared["vertical_unit"],
        horizontal_unit_basis=declared["horizontal_basis"],
        vertical_unit_basis=declared["vertical_basis"], axis_basis=AXIS_BASIS,
        declared_extent_min_x=minimum_x, declared_extent_max_x=maximum_x,
        declared_extent_min_y=minimum_y, declared_extent_max_y=maximum_y,
        declared_extent_min_z=minimum_z, declared_extent_max_z=maximum_z,
        extent_basis=EXTENT_BASIS, gps_time_type_declared=time_type,
        synthetic_return_numbers_declared=("SYNTHETIC_RETURN_NUMBERS_DECLARED"
                                           if header["global_encoding"] & (1 << 3)
                                           else "SYNTHETIC_RETURN_NUMBERS_NOT_DECLARED"),
        gps_time_basis=GPS_TIME_BASIS))
    report = dict(reference=REFERENCE, source_sha256=digest, source_bytes=len(raw),
                  point_format=header["point_format"], point_record_length=length,
                  point_record_extra_bytes=length - minimum,
                  declared_point_count=header["point_count"],
                  legacy_point_count=header["legacy_count"], point_count_disposition=disposition,
                  points_available=available, chunk_first_point_index=first_point,
                  chunk_point_count=reading,
                  points_read=0, coordinate_reference_kind=declared["kind"],
                  metre_horizontal_resolved=declared["metre_xy"],
                  metre_vertical_resolved=declared["metre_z"],
                  project_id_hex=header["project_id_hex"],
                  declared_points_by_return=header["by_return"],
                  declared_legacy_points_by_return=header["legacy_by_return"],
                  extended_record_count=header["evlr_count"],
                  reserved_global_encoding_bits_set=bool(header["global_encoding"] >> 5),
                  variable_length_records=[dict(user_id=record["user_id"],
                                                record_id=record["record_id"],
                                                payload_bytes=record["length"],
                                                description_hex=record["description_hex"])
                                           for record in records],
                  bytes_between_records_and_points=gap,
                  bytes_after_read_points=len(raw) - (header["offset_to_points"] + span))
    for index in range(reading):
        values = struct.unpack_from(layout, raw, start + index * length)
        x, y, z, intensity, bits, classification, angle, user, source = values[:9]
        returns, total = bits & 0b111, (bits >> 3) & 0b111
        # The native values, in whatever unit the declared reference uses, are always carried.
        # The metre columns are the same numbers on the same axes and appear only when the
        # file's own declaration qualified: no factor is applied, nothing is reprojected, and an
        # unresolved declaration leaves them blank rather than assuming a unit.
        east, north, up = (scaled(x, header["scales"][0], header["offsets"][0], "x"),
                           scaled(y, header["scales"][1], header["offsets"][1], "y"),
                           scaled(z, header["scales"][2], header["offsets"][2], "z"))
        writer.writerow(dict(
            # The ABSOLUTE index in the saved file, never a position within the segment:
            # a chunk that renumbered its rows would lose where its points came from.
            fixed, las_record_kind="POINT_RECORD", point_index=first_point + index,
            point_x=east, point_y=north, point_z=up,
            point_x_m=east if declared["metre_xy"] else None,
            point_y_m=north if declared["metre_xy"] else None,
            point_z_m=up if declared["metre_z"] else None,
            point_coordinate_basis=COORDINATE_BASIS, point_intensity_reported=intensity,
            point_return_number=returns, point_return_count=total,
            # "The Return Number must be between 1 and the Number of Returns, inclusive." A
            # record outside that relation is kept with the relation named, never repaired.
            point_return_disposition=("REPORTED_WITHIN_THE_SPECIFIED_RELATION"
                                      if 1 <= returns <= total
                                      else "REPORTED_OUTSIDE_THE_SPECIFIED_RELATION_RETAINED"),
            point_scan_direction_code=(bits >> 6) & 1,
            point_edge_of_flight_line_code=(bits >> 7) & 1,
            point_classification_value=classification & 0b11111,
            point_classification_name=POINT_CLASSES.get(classification & 0b11111, RESERVED_CLASS),
            point_synthetic_flag=(classification >> 5) & 1,
            point_key_point_flag=(classification >> 6) & 1,
            point_withheld_flag=(classification >> 7) & 1,
            point_classification_basis=CLASS_BASIS, point_scan_angle_rank_deg=angle,
            # "a signed one-byte integer with a valid range from -90 to +90"
            point_scan_angle_disposition=("REPORTED_WITHIN_THE_SPECIFIED_RANGE"
                                          if -90 <= angle <= 90
                                          else "REPORTED_OUTSIDE_THE_SPECIFIED_RANGE_RETAINED"),
            point_user_data_reported=user, point_source_id_reported=source,
            point_source_basis=SOURCE_BASIS,
            point_gps_time_reported=(
                repr(finite_double(values[9], "GPS time tag of point %d" % index))
                if len(values) > 9 else None),
            gps_time_basis=(GPS_TIME_BASIS if len(values) > 9 else None)))
        report["points_read"] += 1
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted LAS output exceeds bound")
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved LAS file already on disk")
    parser.add_argument("output_directory", type=Path, help="new directory for the CSV and report")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller declared capture microseconds; never the producer's clock")
    args = parser.parse_args(argv)
    # Bounded read: one byte past the limit is enough to refuse without loading a large file.
    with args.input.open("rb") as handle:
        raw = handle.read(MAX_INPUT + 1)
    converted, report = convert(raw, args.capture_time_us)
    args.output_directory.mkdir()
    (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
    (args.output_directory / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
