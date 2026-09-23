#!/usr/bin/env python3
"""Saved GeoJSON (RFC 7946) inspection features -> per-item observations.

One row per `Feature`: the item's own declared identity, the geometry type, the position where the
specification gives exactly one, and whatever the caller declares carries a reported assertion or a
quantity. Everything the specification does not define -- every key inside `properties` -- is
**retained and counted, never interpreted**.

What this never does: produce a diagnosis, verify a defect, issue a certificate, or claim that
anything was inspected. An assertion inside a saved document is an assertion inside a saved
document. **A position is not a structure's condition; a line is a geometry, not an inspected
pipeline; and the third coordinate is a height above the ellipsoid, not a depth.**

Specification, quoted where it decides something (RFC 7946):
* Section 3.1.1: *"There MUST be two or more elements. The first two elements are longitude and
  latitude, or easting and northing, precisely in that order"*, and *"Implementations SHOULD NOT
  extend positions beyond three elements because the semantics of extra elements are unspecified
  and ambiguous."*
* Section 4: *"An OPTIONAL third-position element SHALL be the height in meters above or below the
  WGS 84 reference ellipsoid"*, under a CRS that is *"the World Geodetic System 1984 (WGS 84)
  datum, with longitude and latitude units of decimal degrees"*. Alternative coordinate reference
  systems were *"removed from this version of the specification"*.
* Section 3.2: an `id` is *"either a JSON string or number"*; `properties` is *"any JSON object or
  a JSON null value"*; an unlocated Feature's `geometry` is *"a JSON null value"*.
* Section 1.4: the seven case-sensitive geometry type strings.
"""
import argparse
import csv
import decimal
import hashlib
import io
import json
import math
from pathlib import Path

try:
    from scripts.convert_position_csv import unique_object, invalid_constant
    from scripts.recorded_units import UNITS, normalize
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant
    from recorded_units import UNITS, normalize

LIMIT = 8 * 1024 * 1024
MAX_FEATURES = 4096
MAX_PROPERTIES = 512
MAX_TEXT = 512
MAX_POSITIONS = 100_000
# This path's own walking bound for nested collections; the specification states none.
MAX_COLLECTION_DEPTH = 8
# Likewise this path's own bound on the optional third element. RFC 7946 gives no numeric limit.
HEIGHT_BOUND_M = 100_000

# "the term "geometry type" refers to seven case-sensitive strings" (Section 1.4).
GEOMETRY_TYPES = ("Point", "MultiPoint", "LineString", "MultiLineString", "Polygon",
                  "MultiPolygon", "GeometryCollection")
# Only a Point has exactly one position. Giving any other geometry "a position" would choose one
# vertex and call it the item's location, which the document does not say.
SINGLE_POSITION_TYPE = "Point"
UNLOCATED = "UNLOCATED_NULL_GEOMETRY"

SPEC = ("RFC 7946: WGS 84 decimal degrees, longitude then latitude, optional third element the "
        "height in metres above or below the WGS 84 reference ellipsoid")
CRS_BASIS = (
    "the_specification_FIXES_the_frame_longitude_then_latitude_in_decimal_degrees_on_the_WGS_84_"
    "datum_and_an_optional_third_element_that_is_the_height_in_METRES_above_or_below_the_WGS_84_"
    "reference_ELLIPSOID_which_is_not_a_mean_sea_level_height_not_an_orthometric_height_and_NOT_A_"
    "DEPTH_alternative_coordinate_reference_systems_were_removed_from_this_version_so_a_document_"
    "carrying_one_is_refused_rather_than_read_in_a_frame_this_path_cannot_prove")
POSITION_BASIS = (
    "a_position_is_emitted_ONLY_for_the_single_position_geometry_the_specification_defines_for_"
    "every_other_geometry_the_document_carries_MANY_positions_and_choosing_one_of_them_as_the_"
    "items_location_would_state_something_the_document_does_not_so_the_count_is_carried_instead")
ASSERTION_BASIS = (
    "REPORTED_BY_THE_SAVED_DOCUMENT_AND_NOT_VERIFIED_HERE_the_specification_defines_NO_meaning_for_"
    "any_key_inside_properties_so_this_value_is_whatever_the_writer_put_under_the_key_the_CALLER_"
    "named_it_is_never_an_independently_confirmed_defect_never_a_condition_assessment_never_a_"
    "severity_and_never_a_certificate")
IDENTITY_BASIS = (
    "which_declaration_this_identity_came_from_the_features_own_id_member_which_the_specification_"
    "says_is_a_string_or_a_number_or_a_property_key_the_CALLER_named_or_NONE_when_the_document_"
    "carried_neither_in_which_case_no_identity_is_invented_for_it")
QUANTITY_BASIS = (
    "a_value_read_from_the_property_key_the_CALLER_named_and_converted_only_through_the_shared_"
    "engineering_unit_table_using_the_unit_the_CALLER_declared_the_specification_states_no_unit_for_"
    "any_property_so_the_unit_is_the_callers_declaration_about_this_document_and_never_something_"
    "read_from_the_file_or_guessed_from_a_key_name")
RECORD_BASIS = (
    "OBSERVED_RECORD_one_feature_of_a_saved_document_read_passively_it_is_evidence_that_the_"
    "document_says_this_and_nothing_more_no_inspection_was_performed_no_condition_is_derived_no_"
    "defect_is_verified_and_no_certificate_is_issued_a_position_is_not_a_structures_condition_and_"
    "a_line_is_a_geometry_not_an_inspected_asset")
PROPERTY_BASIS = (
    "how_many_keys_the_features_properties_object_carried_and_that_NONE_of_them_is_interpreted_"
    "here_their_names_are_listed_in_the_report_so_nothing_leaves_silently_and_a_null_properties_"
    "value_which_the_specification_allows_is_carried_as_none_rather_than_as_an_empty_object")

COLUMNS = [
    "record_time_us", "source_document_sha256", "feature_index",
    "feature_id_hex", "feature_identity_basis",
    "geometry_type", "geometry_position_count", "position_basis",
    "longitude_deg", "latitude_deg", "ellipsoidal_height_m", "coordinate_reference_basis",
    "declared_assertion_key_hex", "reported_assertion_hex", "assertion_basis",
    "declared_quantity_key_hex", "declared_quantity_unit", "reported_quantity_value",
    "reported_quantity_field", "quantity_basis",
    "property_count", "property_basis", "bbox_present", "record_basis",
]


def hexed(text, what):
    if not isinstance(text, str) or not 1 <= len(text.encode()) <= MAX_TEXT:
        raise ValueError("invalid " + what)
    return "hex:" + text.encode().hex()


def number(value, what, low=None, high=None, bound="this path accepts"):
    """A finite JSON number kept exactly as written.

    The document is parsed with `parse_float=Decimal`, so a magnitude written lexically cannot
    become an infinity or a silent zero on the way to the common reader.
    """
    if isinstance(value, bool) or not isinstance(value, (int, decimal.Decimal, float)):
        raise ValueError("invalid " + what)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("invalid " + what)
        value = decimal.Decimal(str(value))
    quantity = decimal.Decimal(value)
    if not quantity.is_finite():
        raise ValueError("invalid " + what)
    if low is not None and not decimal.Decimal(low) <= quantity <= decimal.Decimal(high):
        raise ValueError(what + " outside the range " + bound)
    text = str(quantity)
    reachable = float(text)
    if not math.isfinite(reachable):
        raise ValueError(what + " cannot be carried: it overflows the reader's float")
    if reachable == 0 and quantity != 0:
        raise ValueError(what + " cannot be carried: it underflows to zero at the reader")
    return text


def position(value, what):
    """One position, under the specification's own length and order rules."""
    if not isinstance(value, list) or not 2 <= len(value) <= 3:
        # "There MUST be two or more elements" and implementations "SHOULD NOT extend positions
        # beyond three elements because the semantics of extra elements are unspecified".
        raise ValueError(what + " is two or three elements")
    longitude = number(value[0], what + " longitude", -180, 180,
                       "a WGS 84 geographic longitude occupies")
    latitude = number(value[1], what + " latitude", -90, 90,
                      "a WGS 84 geographic latitude occupies")
    # The specification states NO numeric limit for the third element; the bound below is THIS
    # PATH'S OWN, kept so an absurd figure is refused, and it is described as such wherever it
    # appears. The finite-and-reachable check inside `number` is what protects the value itself.
    height = number(value[2], what + " ellipsoidal height", -HEIGHT_BOUND_M, HEIGHT_BOUND_M,
                    "this path's own implementation bound, which the specification does not give")\
        if len(value) == 3 else ""
    return longitude, latitude, height


def refuse_alternative_frame(obj, what):
    """A `crs` member on any object that could qualify the coordinates.

    Alternative coordinate reference systems were *"removed from this version of the
    specification"*, so a `crs` member on a collection, on a Feature or on a geometry would claim a
    frame this path cannot prove. A key of that name **inside `properties`** is not such a member:
    it is an uninterpreted property like any other, and is left alone.
    """
    if isinstance(obj, dict) and "crs" in obj:
        raise ValueError("a crs member on the " + what
                         + " belongs to the superseded specification and is refused")


def checked_geometry(geometry):
    """One geometry object: its own type, and no frame claim of its own."""
    if not isinstance(geometry, dict):
        raise ValueError("a geometry is an object")
    refuse_alternative_frame(geometry, "geometry")
    kind = geometry.get("type")
    if kind not in GEOMETRY_TYPES:
        # Seven case-sensitive strings, and nothing else is a geometry in this specification.
        raise ValueError("a geometry declares one of the seven specified types")
    return geometry, kind


def ring_corner(texts):
    """A position's own NUMERIC values, with the dimension it was written in.

    `position` returns text, and comparing text would make `[1, 2]` and `[1.0, 2.0]` two different
    corners -- while the specification's rule is that the first and last positions *"MUST contain
    identical values"*, with identical representation only a SHOULD. The comparison is therefore
    on the values, and a two-element corner is still not a three-element one.
    """
    return tuple(decimal.Decimal(text) for text in texts if text != "")


def linear_ring(value, what):
    """A ring under the specification's own rule, closure included.

    *"A linear ring is a closed LineString with four or more positions"* and *"The first and last
    positions are equivalent, and they MUST contain identical values"* (Section 3.1.6). The
    right-hand rule is deliberately NOT enforced: the same section says parsers *"SHOULD NOT reject
    Polygons that do not follow the right-hand rule"*, so winding is left alone.
    """
    if not isinstance(value, list) or len(value) < 4:
        raise ValueError(what + " is a linear ring of four or more positions")
    points = [position(element, what + " position") for element in value]
    if ring_corner(points[0]) != ring_corner(points[-1]):
        raise ValueError(what + " is not closed: its first and last positions differ in value")
    return len(points)


def positions_of(kind, coordinates, what):
    """How many positions this geometry carries, under the nesting its own type fixes.

    Section 3.1.1 assigns the shape per type, and Sections 3.1.2 to 3.1.7 fix the cardinality:
    a Point is *"a single position"*; a MultiPoint *"an array of positions"*; a LineString *"an
    array of two or more positions"*; a MultiLineString *"an array of LineString coordinate
    arrays"*; a Polygon *"an array of linear ring coordinate arrays"*; a MultiPolygon *"an array of
    Polygon coordinate arrays"*. A generic flatten would accept a LineString written as one bare
    position, or a Polygon whose ring is a single point, so the nesting is walked by type instead.
    """
    if kind == SINGLE_POSITION_TYPE:
        position(coordinates, what)
        return 1
    if not isinstance(coordinates, list):
        raise ValueError(what + " is the array its geometry type fixes")
    if kind == "MultiPoint":
        # "an array of positions" -- an empty array is the empty coordinates the specification
        # says a processor MAY read as a null object; it is counted as none, never invented.
        return sum(bool(position(element, what + " position")) for element in coordinates) \
            if coordinates else 0
    if kind == "LineString":
        if len(coordinates) < 2:
            raise ValueError(what + " is an array of two or more positions")
        for element in coordinates:
            position(element, what + " position")
        return len(coordinates)
    if kind == "MultiLineString":
        return sum(positions_of("LineString", element, what + " line")
                   for element in coordinates)
    if kind == "Polygon":
        return sum(linear_ring(element, what + " ring") for element in coordinates)
    if kind == "MultiPolygon":
        return sum(positions_of("Polygon", element, what + " polygon")
                   for element in coordinates)
    raise ValueError(what + " has no defined coordinate shape")


def count_positions(geometry, kind, depth=0):
    """The geometry's positions, with the collection walked to its own bound."""
    if kind == "GeometryCollection":
        if depth >= MAX_COLLECTION_DEPTH:
            # This path's own bound, not a limit the specification states.
            raise ValueError("geometry collections are nested deeper than this path walks")
        members = geometry.get("geometries")
        if not isinstance(members, list):
            raise ValueError("a GeometryCollection carries its geometries array")
        if len(members) > MAX_FEATURES:
            raise ValueError("geometry collection exceeds this path's member bound")
        total = 0
        for member in members:
            inner, inner_kind = checked_geometry(member)
            total += count_positions(inner, inner_kind, depth + 1)
            if total > MAX_POSITIONS:
                raise ValueError("geometry exceeds this path's position bound")
        return total
    total = positions_of(kind, geometry.get("coordinates"), "coordinates")
    if total > MAX_POSITIONS:
        raise ValueError("geometry exceeds this path's position bound")
    return total


def feature_row(feature, index, declarations, digest, capture_time_us):
    if not isinstance(feature, dict):
        raise ValueError("each feature is an object")
    refuse_alternative_frame(feature, "feature")
    if feature.get("type") != "Feature":
        raise ValueError("each member of the feature list declares type Feature")
    row = dict.fromkeys(COLUMNS, "")
    row.update({
        "record_time_us": capture_time_us, "source_document_sha256": digest,
        "feature_index": index, "coordinate_reference_basis": CRS_BASIS,
        "position_basis": POSITION_BASIS, "assertion_basis": ASSERTION_BASIS,
        "feature_identity_basis": IDENTITY_BASIS, "quantity_basis": QUANTITY_BASIS,
        "property_basis": PROPERTY_BASIS, "record_basis": RECORD_BASIS,
        "bbox_present": "PRESENT" if "bbox" in feature else "ABSENT",
    })

    # "A Feature object has a member with the name \"properties\"" and one named "geometry":
    # the VALUE of either may be null, but an absent member is not the null the specification
    # describes, so it is refused rather than read as an unlocated or property-less feature.
    for required in ("geometry", "properties"):
        if required not in feature:
            raise ValueError("a Feature carries its " + required + " member, even when null")
    properties = feature["properties"]
    if properties is not None and not isinstance(properties, dict):
        # "any JSON object or a JSON null value" -- anything else is not a properties member.
        raise ValueError("a properties member is an object or null")
    keys = sorted(properties) if properties else []
    if len(keys) > MAX_PROPERTIES:
        raise ValueError("feature exceeds property bound")
    row["property_count"] = len(keys) if properties is not None else ""

    # Identity: the specification's own `id` first, then the key the caller named, then nothing.
    identity, origin = feature.get("id"), "THE_FEATURES_OWN_ID_MEMBER"
    if identity is None and declarations["identity"] is not None and properties:
        identity, origin = properties.get(declarations["identity"]), "A_CALLER_NAMED_PROPERTY_KEY"
    if identity is None:
        row["feature_identity_basis"] = "NO_IDENTITY_WAS_WRITTEN_AND_NONE_IS_INVENTED " + \
            IDENTITY_BASIS
    else:
        if isinstance(identity, bool) or not isinstance(identity, (str, int, decimal.Decimal,
                                                                  float)):
            raise ValueError("an identity is the string or number the specification allows")
        row["feature_id_hex"] = hexed(identity if isinstance(identity, str) else str(identity),
                                      "feature identity")
        row["feature_identity_basis"] = origin + " " + IDENTITY_BASIS

    geometry = feature["geometry"]
    if geometry is None:
        # "in the case that the Feature is unlocated, a JSON null value"
        row["geometry_type"] = UNLOCATED
    else:
        geometry, kind = checked_geometry(geometry)
        row["geometry_type"] = kind
        row["geometry_position_count"] = count_positions(geometry, kind)
        if kind == SINGLE_POSITION_TYPE:
            (row["longitude_deg"], row["latitude_deg"],
             row["ellipsoidal_height_m"]) = position(geometry.get("coordinates"), "position")

    if declarations["assertion"] is not None:
        row["declared_assertion_key_hex"] = hexed(declarations["assertion"], "assertion key")
        reported = properties.get(declarations["assertion"]) if properties else None
        if reported is not None:
            if isinstance(reported, bool) or not isinstance(reported, (str, int, decimal.Decimal,
                                                                       float)):
                raise ValueError("a reported assertion is a string or a number as written")
            row["reported_assertion_hex"] = hexed(
                reported if isinstance(reported, str) else str(reported), "reported assertion")
    if declarations["quantity"] is not None:
        row["declared_quantity_key_hex"] = hexed(declarations["quantity"], "quantity key")
        row["declared_quantity_unit"] = declarations["unit"]
        value = properties.get(declarations["quantity"]) if properties else None
        if value is not None:
            # Only through the shared table, and only in the unit the caller declared.
            source = decimal.Decimal(number(value, "reported quantity"))
            field, converted = normalize(float(source), declarations["unit"])
            # The conversion is a multiply-and-offset, so a source that reaches the reader can
            # still reach it as ZERO once scaled -- 1e-320 mm is not 0 m. The shared table's own
            # scale and offset decide whether that happened, rather than looking at a result that
            # is already zero and cannot say why.
            _, scale, offset = UNITS[declarations["unit"]]
            if source != 0 and offset == 0 and float(converted) == 0:
                raise ValueError("the reported quantity underflows to zero once the declared "
                                 "unit scaling is applied, so it cannot be carried")
            row["reported_quantity_field"] = field
            row["reported_quantity_value"] = number(converted, "converted quantity")
    return row, keys


def convert(data, capture_time_us, *, identity_property=None, assertion_property=None,
            quantity_property=None, quantity_unit=None):
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("invalid capture time")
    if (quantity_property is None) != (quantity_unit is None):
        raise ValueError("a declared quantity needs its declared unit, and the unit needs a key")
    if quantity_unit is not None and quantity_unit not in UNITS:
        raise ValueError("the declared unit is not one the shared table converts")
    for declared in (identity_property, assertion_property, quantity_property):
        if declared is not None and not (isinstance(declared, str)
                                         and 1 <= len(declared.encode()) <= MAX_TEXT):
            raise ValueError("a declared property key is a non-empty string")
    if len(data) > LIMIT:
        raise ValueError("input exceeds bound")
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        if "\x00" in text:
            raise ValueError("NUL byte in document")
        document = json.loads(text, object_pairs_hook=unique_object,
                              parse_constant=invalid_constant, parse_float=decimal.Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed document") from exc
    if not isinstance(document, dict):
        raise ValueError("a GeoJSON document is one JSON object")
    refuse_alternative_frame(document, "feature collection")
    if document.get("type") != "FeatureCollection":
        raise ValueError("this path reads a FeatureCollection")
    features = document.get("features")
    if not isinstance(features, list):
        raise ValueError("a FeatureCollection carries its features array")
    if len(features) > MAX_FEATURES:
        raise ValueError("document exceeds feature bound")

    digest = hashlib.sha256(
        data if isinstance(data, (bytes, bytearray)) else data.encode()).hexdigest()
    declarations = {"identity": identity_property, "assertion": assertion_property,
                    "quantity": quantity_property, "unit": quantity_unit}
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    retained, kinds = [], {}
    for index, feature in enumerate(features):
        row, keys = feature_row(feature, index, declarations, digest, capture_time_us)
        writer.writerow(row)
        kinds[row["geometry_type"]] = kinds.get(row["geometry_type"], 0) + 1
        retained.append({
            "feature_index": index,
            "recovery_reference": "source_document_sha256 + feature_index",
            "property_keys_not_interpreted": [hexed(key, "property key") for key in keys],
        })
        if output.tell() > LIMIT:
            raise ValueError("observation CSV exceeds bound")

    report = {
        "source_document_sha256": digest, "source_bytes": len(data),
        "pinned_format": SPEC, "features_read": len(features),
        "geometry_kinds": dict(sorted(kinds.items())),
        "declared_identity_property_hex": ("" if identity_property is None
                                           else hexed(identity_property, "identity key")),
        "declared_assertion_property_hex": ("" if assertion_property is None
                                            else hexed(assertion_property, "assertion key")),
        "declared_quantity_property_hex": ("" if quantity_property is None
                                           else hexed(quantity_property, "quantity key")),
        "declared_quantity_unit": quantity_unit or "",
        "properties_not_interpreted": retained,
        "not_produced": "no inspection, no condition, no diagnosis, no verified defect, no "
                        "severity, no certificate, no linear referencing along a geometry and no "
                        "depth: the third coordinate is a height above the WGS 84 ellipsoid",
    }
    if len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("report exceeds bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path, help="saved RFC 7946 FeatureCollection")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller clock for when the saved document was taken in; never an "
                             "inspection time and never when anything was observed")
    parser.add_argument("--identity-property",
                        help="property key carrying the item identity when there is no id member")
    parser.add_argument("--assertion-property",
                        help="property key whose value is the document's own reported assertion")
    parser.add_argument("--quantity-property", help="property key carrying one declared quantity")
    parser.add_argument("--quantity-unit", choices=sorted(UNITS),
                        help="the unit the caller declares for that property; never guessed")
    args = parser.parse_args()
    try:
        with args.document.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        output, report = convert(data, args.capture_time_us,
                                 identity_property=args.identity_property,
                                 assertion_property=args.assertion_property,
                                 quantity_property=args.quantity_property,
                                 quantity_unit=args.quantity_unit)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, RecursionError, KeyError):
        parser.exit(2, "saved feature conversion failed; no inspection is claimed\n")


if __name__ == "__main__":
    main()
