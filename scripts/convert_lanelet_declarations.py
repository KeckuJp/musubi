#!/usr/bin/env python3
"""Saved Lanelet2 1.2.1 OSM map declarations -> unit-normalized CSV for TelemetryCsvReader.

Declared geometry and relations only: nothing is projected, planned, driven or certified,
and no datum or elevation is inferred.
"""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

# Lanelet2 1.2.1 (a184258a3ef020648a375c5c6bac083673ecdb74).
PIN = "lanelet2-1.2.1"
LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
MAX_ELEMENTS = 200_000
MAX_ROWS = 1_000_000
# lanelet2_maps/README.md + OsmHandlerLoad.cpp: a role only carries a meaning inside the
# relation type that establishes it, and only for the member type that type resolves. A
# role string outside that context - a regulatory element's roles, or any relation whose
# type this pin does not recognise - stays retained and unqualified.
# The third element says whether the pin *enforces* the member kind (a mismatch is a
# contradiction and is refused) or merely documents it (a mismatch stays unqualified):
# OsmHandlerLoad.cpp rejects a lanelet border or an area ring that is not a way, while
# the regulatory_element member kind is only documented in lanelet2_maps/README.md.
ROLE_MEANINGS = {
    "LANELET": {"left": ("LEFT_BOUNDARY", "way", True), "right": ("RIGHT_BOUNDARY", "way", True),
                "centerline": ("CENTERLINE", "way", True),
                "regulatory_element": ("REGULATORY_ELEMENT", "relation", False)},
    "MULTIPOLYGON": {"outer": ("OUTER_RING", "way", True), "inner": ("INNER_RING", "way", True)},
}
RELATION_TYPES = {"lanelet": "LANELET", "regulatory_element": "REGULATORY_ELEMENT",
                  "multipolygon": "MULTIPOLYGON"}
# lanelet2_maps/README.md states the `ele` tag unconditionally as the distance to the
# WGS84 ellipsoid; the format declares no default for its absence, and what a caller's
# projector later does to that height is not established by the pinned files.
COORDINATE_BASIS = "DECLARED_DEGREES_DATUM_NOT_IN_FILE_PROJECTION_CALLER_SUPPLIED"
DECLARATION_BASIS = "DECLARED_MAP_NOT_PROJECTED_ROUTED_OR_CERTIFIED"
KNOWN_ATTRIBUTES = {"node": {"id", "lat", "lon", "action"}, "way": {"id", "action"},
                    "relation": {"id", "action"}, "nd": {"ref"}, "member": {"type", "role", "ref"},
                    "tag": {"k", "v"}}
STRUCTURAL_CHILDREN = {"node": {"tag"}, "way": {"tag", "nd"}, "relation": {"tag", "member"}}
FIELDS = ["map_record_kind", "map_element_index", "map_element_id", "map_owner_id",
          "map_member_ordinal", "map_reference_id", "map_reference_kind",
          "map_latitude_deg", "map_longitude_deg", "map_elevation_m", "map_elevation_status",
          "map_coordinate_basis", "map_relation_type", "map_relation_subtype_hex",
          "map_member_role_hex", "map_member_role_meaning", "map_way_kind",
          "map_reference_count", "map_tag_key_hex", "map_tag_value_hex", "map_load_disposition",
          "map_declaration_basis"]
REGULATORY_ROLES = {
    'traffic_sign': ('refers', 'cancels', 'ref_line', 'cancel_line'),
    'speed_limit': ('refers', 'cancels', 'ref_line', 'cancel_line'),
    'traffic_light': ('refers', 'ref_line'),
    'right_of_way': ('yield', 'right_of_way', 'ref_line'),
    'all_way_stop': ('yield', 'ref_line', 'refers'),
}
PARAMETER_MEANINGS = dict(refers='DECLARED_RESTRICTION_ORIGIN', cancels='DECLARED_END_MARKER',
    ref_line='DECLARED_REFERENCE_LINE', cancel_line='DECLARED_END_LINE',
    yield_='DECLARED_YIELD_LANELET', right_of_way='DECLARED_PRIORITY_LANELET')
REGULATORY_FIELDS = ['map_regulatory_subtype', 'map_regulatory_parameter_meaning',
    'map_associated_lanelet_id', 'map_stop_line_pairing', 'map_declared_speed_limit_m_s',
    'map_speed_limit_disposition', 'map_dynamic_disposition', 'map_fallback_disposition',
    'map_regulatory_basis']
GERMAN_SPEEDS_KMH = {'de274': 30, 'de274_1': 30, 'de274_1-20': 20, 'de310': 50,
    **{'de274-' + str(n): n for n in (5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130)}}
SIGN_FIELDS = ['map_speed_type_selected_hex', 'map_speed_type_source',
               'map_speed_type_alternate_hex', 'map_speed_type_conflict', 'map_traffic_rules_basis']


def speed_fields(value):
    result = {'map_speed_limit_disposition': 'NOT_DECLARED' if value is None else 'UNQUALIFIED_SIGN_TEXT'}
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(km/h|mph|mps)?', value or '')
    if match:
        if len(match[1]) > 256:
            raise ValueError('declared speed numeric text exceeds bound')
        number = Decimal(match[1])
        unit = match[2] or 'km/h'
        scaled = number / Decimal('3.6') if unit == 'km/h' else number * (Decimal('0.44704') if unit == 'mph' else 1)
        speed = float(scaled)
        if not math.isfinite(speed) or (number != 0 and speed == 0):
            raise ValueError('declared speed exceeds finite common range')
        result.update(map_declared_speed_limit_m_s=speed,
            map_speed_limit_disposition='DECLARED_NUMERIC_UNIT' if match[2] else 'DOCUMENTED_KMH_DEFAULT')
    return result


def german_speed_context(element, ways):
    """Fixed reported sign meaning, not effective road permission or driving."""
    alternate = structural_tag(element, 'sign_type')
    value, basis = alternate, 'RELATION_SIGN_TYPE_NO_SIGN_WAY'
    signs = [m for m in element.findall('member') if m.get('role') == 'refers' and m.get('type') == 'way']
    if signs:
        ref = signs[0].get('ref', '')
        if not re.fullmatch(r'-?[0-9]{1,19}', ref) or int(ref) not in ways:
            raise ValueError('speed sign references an unavailable way')
        value = structural_tag(ways[int(ref)], 'subtype')
        if value is None:
            raise ValueError('fixed TrafficSign first refers way requires subtype')
        basis = 'FIRST_REFERS_WAY_SUBTYPE_FIXED_PRECEDENCE'
    result = speed_fields(value)
    if value in GERMAN_SPEEDS_KMH:
        result.update(map_declared_speed_limit_m_s=float(Decimal(GERMAN_SPEEDS_KMH[value]) / Decimal('3.6')),
                      map_speed_limit_disposition='FIXED_GERMAN_VEHICLE_SIGN_CODE')
    result.update(map_speed_type_selected_hex=None if value is None else text_hex(value),
        map_speed_type_source=basis,
        map_speed_type_alternate_hex=None if alternate is None else text_hex(alternate),
        map_speed_type_conflict='ALTERNATE_DIFFERENT_FIXED_PRECEDENCE_NOT_ROAD_VALIDITY' if signs and alternate is not None and alternate != value else 'NONE_REPORTED',
        map_traffic_rules_basis='LANELET_1_2_1_GERMANY_VEHICLE_REPORTED_SIGN_NOT_LEGAL_OR_OPERATIONAL_VALIDITY')
    return result


def regulatory_context(element, subtype):
    known = subtype in REGULATORY_ROLES
    result = dict(map_regulatory_subtype=subtype if known else 'UNQUALIFIED_SUBTYPE',
        map_regulatory_basis='DECLARED_PARAMETERS_NOT_RULE_VALIDITY_PERMISSION_OR_EXECUTION',
        map_speed_limit_disposition='NOT_SELECTED_SUBTYPE')
    for name in ('dynamic', 'fallback'):
        value = structural_tag(element, name)
        result[f'map_{name}_disposition'] = ('ABSENT' if value is None else
            'DECLARED_' + value.upper() if value in ('yes', 'no') else 'UNQUALIFIED_DECLARATION')
    if subtype == 'speed_limit':
        value = structural_tag(element, 'sign_type')
        result.update(speed_fields(value))
    return result


def text_hex(value):
    return "hex:" + value.encode("utf-8").hex()


def identifier(element, kind):
    value = element.get("id")
    if value is None or not re.fullmatch(r"-?[0-9]{1,19}", value) or not -(2**63) <= int(value) < 2**63:
        raise ValueError("declared " + kind + " needs a signed64 id")
    return int(value)


def coordinate(element, name, limit):
    """Declared degrees. The loader defaults a missing value to 0.0; a declaration
    importer must not, so an absent or unreadable coordinate is refused instead."""
    value = element.get(name)
    if value is None:
        raise ValueError("node is missing its declared " + name)
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise ValueError("declared " + name + " is not a decimal degree value")
    number = float(value)
    if not math.isfinite(number) or abs(number) > limit:
        raise ValueError("declared " + name + " is outside the degree range")
    return number


def structural_tag(element, key):
    """One declared value for a key this converter reads structurally.

    Repeated identical declarations are non-conflicting and are kept; repeated
    conflicting ones are ambiguous and are refused, never silently resolved to the
    first or last. Every occurrence is still emitted as its own retained TAG row.
    """
    values = {tag.get("v") for tag in element.findall("tag") if tag.get("k") == key}
    if len(values) > 1:
        raise ValueError("conflicting repeated " + key + " tag")
    return values.pop() if values else None


def elevation(element):
    """`ele` tag: declared metres when present. Absent stays absent - never 0."""
    declared = [tag for tag in element.findall("tag") if tag.get("k") == "ele"]
    if not declared:
        return None, "ELEVATION_NOT_DECLARED"
    value = structural_tag(element, "ele")
    if value is None or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise ValueError("declared elevation is not a decimal metre value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("declared elevation is not finite")
    return number, "DECLARED_METRES_OVER_WGS84_ELLIPSOID"


def deleted(element):
    """OsmFile.cpp skips `action="delete"`; keep the declaration, mark it not loaded."""
    return element.get("action") == "delete"


def convert(data, capture_time_us, *, regulatory_declarations=False, traffic_rules=None):
    if type(regulatory_declarations) is not bool:
        raise ValueError('regulatory_declarations must be an explicit boolean')
    if traffic_rules is not None and (traffic_rules != 'germany-vehicle-1.2.1' or not regulatory_declarations):
        raise ValueError('explicit qualified traffic rules require regulatory declarations')
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture time required")
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized map declaration")
    text = data.decode("utf-8-sig")
    # Ordinary saved maps carry XML comments. Every other `<!` construct - DOCTYPE,
    # ENTITY, CDATA - is refused before the reader sees the text, so no DTD, entity
    # expansion or external reference is ever resolved.
    position = text.find("<!")
    while position != -1:
        if not text.startswith("<!--", position):
            raise ValueError("map XML declarations, DTD and entities unsupported")
        closing = text.find("-->", position + 4)
        if closing == -1:
            raise ValueError("unterminated XML comment")
        position = text.find("<!", closing + 3)
    if "\x00" in text:
        raise ValueError("map declaration must not contain NUL")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("unreadable map declaration XML") from error
    if root.tag != "osm":
        raise ValueError("osm root element required")
    order = {element: index for index, element in enumerate(root.iter())}
    if len(order) > MAX_ELEMENTS:
        raise ValueError("declared element bound")

    rows = []

    def append(row):
        if len(rows) >= MAX_ROWS:
            raise ValueError("declared row bound")
        rows.append(row)

    def unknown_attributes(element, kind, element_id):
        """Anything this pin does not read stays counted and referenced, never dropped.

        The row names the attribute and keeps its exact declared text; the element is
        located by the immutable document digest plus its element index, so nothing
        needs a whole-document echo to be recoverable.
        """
        for key, value in element.attrib.items():
            if key in KNOWN_ATTRIBUTES[kind]:
                continue
            if not key:
                raise ValueError("declared attribute needs a name")
            append(dict(map_record_kind="UNKNOWN_ATTRIBUTE", map_element_index=order[element],
                        map_owner_id=element_id, map_reference_kind=kind,
                        map_tag_key_hex=text_hex(key), map_tag_value_hex=text_hex(value),
                        map_load_disposition="UNKNOWN_AT_PIN_RETAINED_BY_REFERENCE"))

    def tag_rows(element, kind, element_id):
        for tag in element.findall("tag"):
            key, value = tag.get("k"), tag.get("v")
            if key is None or value is None or not key:
                raise ValueError("declared tag needs k and v")
            append(dict(map_record_kind="TAG", map_element_index=order[tag], map_owner_id=element_id,
                        map_reference_kind=kind, map_tag_key_hex=text_hex(key),
                        map_tag_value_hex=text_hex(value)))
            unknown_attributes(tag, "tag", element_id)
        for child in element:
            if child.tag in STRUCTURAL_CHILDREN[kind]:
                continue
            append(dict(map_record_kind="UNKNOWN_CHILD", map_element_index=order[child],
                        map_owner_id=element_id, map_reference_kind=kind,
                        map_tag_key_hex=text_hex(child.tag),
                        map_reference_count=len(child.attrib),
                        map_load_disposition="UNKNOWN_AT_PIN_RETAINED_BY_REFERENCE"))
        unknown_attributes(element, kind, element_id)

    nodes, ways, relations, metadata = {}, {}, {}, []
    for element in root:
        if element.tag not in ("node", "way", "relation", "bounds", "note", "meta"):
            raise ValueError("unsupported osm child element")
        if element.tag in ("bounds", "note", "meta"):
            # Not promoted to rows and not interpreted, but named and located so the
            # document's own accounting stays complete.
            metadata.append(dict(element=element.tag, element_index=order[element],
                                 attribute_names=sorted(element.attrib)))
            continue
        table = {"node": nodes, "way": ways, "relation": relations}[element.tag]
        element_id = identifier(element, element.tag)
        if element_id in table:
            raise ValueError("duplicate declared " + element.tag + " id")
        table[element_id] = element

    for element_id, element in nodes.items():
        skipped = deleted(element)
        height, status = elevation(element)
        append(dict(map_record_kind="NODE", map_element_index=order[element], map_element_id=element_id,
                    map_latitude_deg=coordinate(element, "lat", 90),
                    map_longitude_deg=coordinate(element, "lon", 180),
                    map_elevation_m=height, map_elevation_status=status,
                    map_coordinate_basis=COORDINATE_BASIS,
                    map_load_disposition="DELETED_ACTION_NOT_LOADED" if skipped else "DECLARED"))
        tag_rows(element, "node", element_id)

    for element_id, element in ways.items():
        references = element.findall("nd")
        if not references:
            raise ValueError("declared way needs at least one node reference")
        area = structural_tag(element, "area")
        skipped = deleted(element)
        append(dict(map_record_kind="WAY", map_element_index=order[element], map_element_id=element_id,
                    map_reference_count=len(references),
                    # OsmHandlerLoad.cpp: a way tagged area=true is a polygon, else a linestring.
                    map_way_kind="POLYGON_DECLARED_AREA_TAG" if area == "true"
                    else "LINESTRING_DEFAULT",
                    map_load_disposition="DELETED_ACTION_NOT_LOADED" if skipped else "DECLARED"))
        for ordinal, reference in enumerate(references):
            target = reference.get("ref")
            if target is None or not re.fullmatch(r"-?[0-9]{1,19}", target) or int(target) not in nodes:
                raise ValueError("way references a node that is not declared here")
            if deleted(nodes[int(target)]) and not skipped:
                raise ValueError("way references a deleted node")
            # `nd` order is the declared point order of the linestring.
            append(dict(map_record_kind="WAY_REF", map_element_index=order[reference],
                        map_owner_id=element_id, map_member_ordinal=ordinal,
                        map_reference_id=int(target), map_reference_kind="node"))
            unknown_attributes(reference, "nd", element_id)
        tag_rows(element, "way", element_id)

    for element_id, element in relations.items():
        declared_type = structural_tag(element, "type")
        relation_type = RELATION_TYPES.get(declared_type or "", "OTHER_UNQUALIFIED")
        subtype = structural_tag(element, "subtype")
        if relation_type == "REGULATORY_ELEMENT" and not subtype:
            raise ValueError("regulatory element relation needs a subtype tag")
        members = element.findall("member")
        regulatory = regulatory_context(element, subtype) if regulatory_declarations and relation_type == 'REGULATORY_ELEMENT' else {}
        if traffic_rules is not None and subtype == 'speed_limit' and relation_type == 'REGULATORY_ELEMENT':
            regulatory.pop('map_declared_speed_limit_m_s', None)
            regulatory.update(german_speed_context(element, ways))
        yields = [m for m in members if m.get('role') == 'yield']
        lines = [m for m in members if m.get('role') == 'ref_line']
        skipped = deleted(element)
        roles = []
        # Only members whose kind matches the established role count towards the rule.
        for ordinal, member in enumerate(members):
            kind, role, target = member.get("type"), member.get("role"), member.get("ref")
            if kind not in ("node", "way", "relation") or role is None or target is None:
                raise ValueError("declared member needs type, role and ref")
            if not re.fullmatch(r"-?[0-9]{1,19}", target):
                raise ValueError("declared member ref must be a signed64 id")
            table = {"node": nodes, "way": ways, "relation": relations}[kind]
            if int(target) not in table:
                raise ValueError("relation references a " + kind + " that is not declared here")
            if deleted(table[int(target)]) and not skipped:
                raise ValueError("relation references a deleted element")
            established = ROLE_MEANINGS.get(relation_type, {}).get(role)
            if established is None:
                meaning = "UNQUALIFIED_ROLE"
            elif established[1] == kind:
                meaning = established[0]
                roles.append(role)
            elif established[2]:
                # A boundary or ring the pin enforces as a way is a contradiction when it
                # is declared as something else, not something to qualify or re-label.
                raise ValueError("relation declares " + role + " as a " + kind + ", not a "
                                 + established[1])
            else:
                meaning = "UNQUALIFIED_ROLE"
            parameter = {}
            if regulatory_declarations:
                parameter = dict(regulatory)
                parameter['map_regulatory_parameter_meaning'] = 'UNQUALIFIED_ROLE_OR_TARGET'
                if relation_type in ('LANELET', 'MULTIPOLYGON') and role == 'regulatory_element' and kind == 'relation' and structural_tag(table[int(target)], 'type') == 'regulatory_element':
                    parameter['map_regulatory_parameter_meaning'] = 'DECLARED_ASSOCIATED_REGULATORY_ELEMENT'
                elif role in REGULATORY_ROLES.get(subtype, ()) and relation_type == 'REGULATORY_ELEMENT':
                    qualified = ((role in ('refers', 'cancels') and kind in ('node', 'way')) or
                                 (role in ('ref_line', 'cancel_line') and kind == 'way') or
                                 (role in ('yield', 'right_of_way') and kind == 'relation' and structural_tag(table[int(target)], 'type') == 'lanelet'))
                    if qualified:
                        parameter['map_regulatory_parameter_meaning'] = PARAMETER_MEANINGS['yield_' if role == 'yield' else role]
                    if subtype == 'all_way_stop' and role == 'ref_line':
                        parameter['map_stop_line_pairing'] = 'UNQUALIFIED_COUNT_OR_TARGET'
                        if qualified and len(lines) == len(yields):
                            y = yields[lines.index(member)]
                            ref = y.get('ref', '')
                            if y.get('type') == 'relation' and ref.lstrip('-').isdigit() and int(ref) in relations and structural_tag(relations[int(ref)], 'type') == 'lanelet':
                                parameter.update(map_associated_lanelet_id=int(ref), map_stop_line_pairing='DECLARED_SAME_ROLE_ORDER')
            append(dict(map_record_kind="MEMBER", map_element_index=order[member],
                        map_owner_id=element_id, map_member_ordinal=ordinal,
                        map_reference_id=int(target), map_reference_kind=kind,
                        map_member_role_hex=text_hex(role), map_member_role_meaning=meaning, **parameter))
            unknown_attributes(member, "member", element_id)
        if relation_type == "LANELET" and not skipped:
            # OsmHandlerLoad.cpp: a lanelet needs exactly one left and one right border.
            for role in ("left", "right"):
                if roles.count(role) != 1:
                    raise ValueError("lanelet relation needs exactly one " + role + " border")
        append(dict(map_record_kind="RELATION", map_element_index=order[element],
                    map_element_id=element_id, map_relation_type=relation_type,
                    map_relation_subtype_hex=None if subtype is None else text_hex(subtype),
                    map_reference_count=len(members),
                    map_load_disposition="DELETED_ACTION_NOT_LOADED" if skipped else "DECLARED", **regulatory))
        tag_rows(element, "relation", element_id)

    if not rows:
        raise ValueError("no declared map elements")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS,
                                   *(REGULATORY_FIELDS if regulatory_declarations else []),
                                   *(SIGN_FIELDS if traffic_rules is not None else [])],
                            lineterminator="\n")
    writer.writeheader()
    rows.sort(key=lambda row: row["map_element_index"])
    for row in rows:
        writer.writerow(dict(record_time_us=capture_time_us, source_document_sha256=digest,
                             map_declaration_basis=DECLARATION_BASIS,
                             **{key: "" if value is None else value for key, value in row.items()}))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("declared output exceeds bound")
    counts = {kind: sum(1 for row in rows if row["map_record_kind"] == kind)
              for kind in ("NODE", "WAY", "WAY_REF", "RELATION", "MEMBER", "TAG",
                           "UNKNOWN_CHILD", "UNKNOWN_ATTRIBUTE")}
    report = dict(output_records=len(rows), declared_nodes=len(nodes), declared_ways=len(ways),
                  declared_relations=len(relations), rows_by_kind=counts,
                  unknown_declarations=sum(1 for row in rows if row["map_record_kind"]
                                           in ("UNKNOWN_CHILD", "UNKNOWN_ATTRIBUTE")),
                  deleted_declarations=sum(1 for row in rows
                                           if row.get("map_load_disposition") == "DELETED_ACTION_NOT_LOADED"),
                  root_metadata_elements=len(metadata), root_metadata=metadata,
                  source_sha256=digest, clock="Unknown", pin=PIN,
                  scope="declared map elements only; no projection, datum, routing, traffic rule "
                        "or certification; a map does not establish a vehicle domain")
    if traffic_rules is not None:
        report['traffic_rules_interpretation'] = dict(selector=traffic_rules,
            basis='FIXED_SOURCE_DECLARATION_MAPPING_NOT_EFFECTIVE_ROAD_RULES',
            unknown_sign_codes='RETAINED_WITHOUT_NUMERIC_VALUE',
            country_defaults_and_participant_overrides='NOT_EVALUATED')
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller capture clock; the common clock basis stays Unknown")
    parser.add_argument('--regulatory-declarations', action='store_true')
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)  # one bounded read before allocation
        output, report = convert(data, args.capture_time_us, regulatory_declarations=args.regulatory_declarations)
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, ET.ParseError):
        parser.exit(2, "map declaration conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
