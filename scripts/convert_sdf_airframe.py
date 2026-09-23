#!/usr/bin/env python3
"""Saved SDF airframe descriptions -> unit-normalized CSV for TelemetryCsvReader.

Declared configuration context only: no simulator is run, no plugin is loaded, no controller is
started and no flightworthiness, performance or physical measurement is claimed. A description
file says what someone wrote down about a model; it does not establish that an aircraft exists or
that any named part was built or measured.
"""
import argparse
import csv
import hashlib
import io
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recorded_units import normalize

# gazebosim/sdformat e7063bf2c13c1d6a4eb58c1ff741d9a5b97c01fe, sdf/1.11/{inertial,joint,link}.sdf.
PIN = "sdformat-e7063bf2"
LIMIT = 4 * 1024 * 1024
OUTPUT_LIMIT = 16 * 1024 * 1024
MAX_LINKS = 1024
MAX_JOINTS = 1024
MAX_ROWS = 8192
NAME = re.compile(r"[!-~][ -~]{0,127}")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?")
# `sdf/1.11/joint.sdf`: <parent> is "Name of the parent frame or "world"", and <child> is "Name of
# the child frame. The value "world" may not be specified." So a parent of `world` is a declared
# attachment to the world frame, not a reference to a link this document failed to declare.
WORLD = "world"
EULER = "euler_rpy"
# Only the versions whose element definitions were read at the pin are accepted. A version string
# this selector has not read is refused: a version name alone never qualifies compatibility, and
# reading a document under a different version's definitions is exactly the silent inheritance this
# path exists to avoid. At the pin, `sdf/1.5/pose.sdf` is nine lines long and its whole description
# is "A position(x,y,z) and orientation(roll, pitch yaw) with respect to the specified frame." with
# a single `frame` attribute -- it has NO `degrees` and NO `rotation_format` attribute, and states
# no unit at all. `sdf/1.11/pose.sdf` adds both attributes and the radians-by-default sentence.
VERSIONS = {
    "1.5": dict(pose_attributes=("frame",),
                rotation="NOT_STATED_BY_THE_DECLARED_VERSIONS_OWN_POSE_DEFINITION_WHICH_NAMES_NO_"
                         "UNIT_AND_HAS_NO_DEGREES_ATTRIBUTE_SO_NO_LATER_VERSIONS_DEFAULT_IS_"
                         "INHERITED_HERE",
                com_frame="CENTRE_OF_MASS_FRAME_C_RELATIVE_TO_THE_FRAME_NAMED_BY_THE_POSE_FRAME_"
                          "ATTRIBUTE_OR_THE_LINK_FRAME_L_WHEN_THAT_ATTRIBUTE_IS_EMPTY_OR_ABSENT_"
                          "AS_THE_DECLARED_VERSIONS_OWN_DEFINITION_STATES"),
    "1.11": dict(pose_attributes=("degrees", "rotation_format", "relative_to"),
                 rotation="RADIANS_BY_THE_DECLARED_VERSIONS_OWN_STATED_DEFAULT_THE_DOCUMENT_"
                          "DECLARED_NO_DEGREES_ATTRIBUTE",
                 com_frame="CENTRE_OF_MASS_FRAME_C_RELATIVE_TO_THE_LINK_FRAME_L_THE_POSITION_IS_"
                           "THE_VECTOR_FROM_THE_LINK_FRAME_ORIGIN_TO_THE_CENTRE_OF_MASS_AS_THE_"
                           "DECLARED_VERSIONS_OWN_DEFINITION_STATES")}
ROTATION_DECLARED = "DEGREES_DECLARED_BY_THE_DOCUMENTS_OWN_POSE_ATTRIBUTE"
# No unit statement was found in the files inspected at the pin -- the two versions' element
# definitions, `doc/`, `README.md`, `Migration.md` -- which is a statement about those files and not
# about every file or about any external standard. `<mass>` reads "The mass of the link." in BOTH
# 1.5 and 1.11, with no unit. So a unit is never defaulted; it is declared by the caller, against
# the version the document itself declares, or the canonical columns stay empty.
MASS_UNITS = ("kg",)            # identity only; no conversion is performed for mass
POSITION_UNITS = ("m", "cm", "mm")   # converted by the shared engineering-unit table
UNIT_BASES = ("project-documentation-declared", "operator-declared")
JOINT_FRAME = ("BY_DEFAULT_THE_POSE_OF_A_JOINT_IS_EXPRESSED_IN_THE_CHILD_LINK_FRAME_AS_THE_PINNED_"
               "SPECIFICATION_STATES_THIS_PATH_READS_THE_DECLARED_ATTACHMENT_AND_RESOLVES_NO_"
               "TRANSFORM_AND_NO_GEOMETRY")
MASS_BASIS = ("BOTH_READ_VERSIONS_SAY_ONLY_THE_MASS_OF_THE_LINK_AND_STATE_NO_UNIT_AND_NO_UNIT_"
              "STATEMENT_WAS_FOUND_IN_THE_FILES_INSPECTED_AT_THE_PIN_WHICH_IS_NOT_A_CLAIM_ABOUT_"
              "EVERY_FILE_OR_ABOUT_ANY_EXTERNAL_STANDARD_SO_A_MASS_UNIT_IS_NEVER_DEFAULTED_THE_"
              "SPECIFICATIONS_"
              "FLUID_ADDED_MASS_ENTRIES_DO_SAY_IN_KG_AT_1_11_WHICH_IS_CORROBORATION_FOR_A_DIFFERENT_"
              "ELEMENT_AND_NOT_A_DECLARATION_FOR_THIS_ONE_A_LINK_THAT_DECLARED_NO_MASS_IS_BLANK_"
              "AND_NAMED_NOT_ZERO")
BASIS = ("DECLARED_DESCRIPTION_ONLY_NOTHING_IS_SIMULATED_FLOWN_ACTUATED_OR_MEASURED_AND_THE_"
         "SPECIFICATION_DOES_NOT_SAY_WHETHER_A_DESCRIPTION_FILE_DESCRIBES_A_DESIGNED_OR_A_BUILT_"
         "ARTICLE_SO_NEITHER_IS_CLAIMED_HERE_AND_NO_NAMED_PART_IS_ESTABLISHED_TO_EXIST")
FIELDS = ["airframe_record_kind", "airframe_element_index", "airframe_declared_sdf_version",
          "airframe_model_name_hex", "airframe_link_name_hex", "airframe_joint_name_hex",
          "airframe_joint_type", "airframe_parent_name_hex", "airframe_child_name_hex",
          "airframe_parent_is_world", "airframe_parent_resolution", "airframe_child_resolution",
          "airframe_link_parent_hex", "airframe_link_parent_resolution", "airframe_link_depth",
          "airframe_link_child_count", "airframe_reported_mass", "airframe_mass_disposition",
          "airframe_mass_kg", "airframe_com_x", "airframe_com_y", "airframe_com_z",
          "airframe_com_x_m", "airframe_com_y_m", "airframe_com_z_m",
          "airframe_unit_declaration", "airframe_com_rotation_unit",
          "airframe_com_pose_frame_hex", "airframe_com_frame_basis", "airframe_joint_frame_basis",
          "airframe_unselected_child_count", "airframe_declaration_basis"]


def text_hex(value):
    return "hex:" + value.encode("utf-8").hex()


def finite_value(token, what):
    """The declared token as a value that survives the journey, or a refusal naming what is wrong.

    The token pattern alone admits `1e999` and `1e-999`. The first becomes an infinity and the
    second becomes a zero once anything reads the CSV as a number, so a declared figure would
    arrive in common output as a non-number or as a false zero. Both are refused here, before any
    conversion, rather than written out and discovered downstream. The source token itself is never
    rounded or rewritten: it stays in the reported column exactly as the document wrote it.
    """
    try:
        exact = Decimal(token)
    except InvalidOperation:
        raise ValueError(f"declared {what} is not a number")
    value = float(exact)
    if not math.isfinite(value):
        raise ValueError(f"declared {what} cannot be represented as a finite number")
    if exact != 0 and value == 0:
        raise ValueError(f"declared nonzero {what} underflows to zero")
    return value


def only_child(element, tag, what):
    """The one selected child, or a refusal. Taking the first of several would discard a
    declaration silently, which is the loss this refusal exists to prevent."""
    found = element.findall(tag)
    if len(found) > 1:
        raise ValueError(f"a declared {what} carries more than one {tag} element")
    return found[0] if found else None


def declared_name(element, what):
    name = element.get("name")
    if name is None or not NAME.fullmatch(name):
        raise ValueError(f"declared {what} name required")
    return name


def pose_values(element, version):
    """One `<pose>` read under the definitions of the version the document itself declares.

    Only the position triple is selected; the orientation is never converted, composed or
    projected. An attribute the declared version's own definition does not have is refused rather
    than read with a later version's meaning -- that inheritance is the whole hazard here.
    """
    if element is None:
        return None, None, None, None, None
    supported = VERSIONS[version]["pose_attributes"]
    for attribute in element.attrib:
        if attribute not in supported:
            raise ValueError(f"the declared SDF version's pose definition has no {attribute} "
                             "attribute; a later version's meaning is not inherited")
    if (element.get("rotation_format") or EULER) != EULER:
        raise ValueError("only the euler_rpy rotation format is qualified by this selector")
    degrees = element.get("degrees")
    if degrees not in (None, "true", "false"):
        raise ValueError("declared pose degrees attribute must be the boolean the schema declares")
    tokens = (element.text or "").split()
    if len(tokens) != 6 or not all(NUMBER.fullmatch(token) for token in tokens):
        raise ValueError("a declared euler pose needs six complete numbers")
    for token in tokens[:3]:
        finite_value(token, "pose position component")
    rotation = ROTATION_DECLARED if degrees == "true" else VERSIONS[version]["rotation"]
    # 1.5 names the reference frame in an attribute; it is carried, never resolved.
    return (*tokens[:3], rotation, element.get("frame") or None)


def mass_disposition(link):
    """What the link said about its mass, keeping "not declared" and "not positive" apart.

    The read versions state no non-negativity constraint, so this is a statement about what may be
    presented as a canonical quantity, not a claim about physics.
    """
    if link["mass_value"] is None:
        return "NOT_REPORTED"
    if link["mass_value"] > 0:
        return "REPORTED"
    return "REPORTED_NOT_A_POSITIVE_VALUE_RETAINED_AND_NOT_PRESENTED_AS_A_CANONICAL_MASS"


def canonical_position(link, position_unit):
    """The declared position in the canonical metre, or three blanks when nothing was declared.

    The conversion is the shared engineering-unit table every other converter uses; a declared
    centimetre becomes the canonical metre there and not in a second table written here.
    """
    if position_unit is None or link["x"] is None:
        return None, None, None
    converted = []
    for axis in ("x", "y", "z"):
        declared = finite_value(link[axis], "pose position component")
        field, value = normalize(declared, position_unit)
        if field != "distance_m":
            raise ValueError("the declared position unit is not a distance")
        # The scale can carry a representable figure out of range in either direction, so the
        # converted value is checked too rather than only its input.
        if not math.isfinite(value) or (declared != 0 and value == 0):
            raise ValueError("the declared position does not survive its declared unit conversion")
        converted.append(value)
    return tuple(converted)


def declared_units(mass_unit, position_unit, unit_basis, version):
    """The caller's unit declaration, checked, or a refusal. Nothing is ever defaulted."""
    if mass_unit is not None and mass_unit not in MASS_UNITS:
        raise ValueError("unqualified declared mass unit")
    if position_unit is not None and position_unit not in POSITION_UNITS:
        raise ValueError("unqualified declared position unit")
    if mass_unit is None and position_unit is None:
        if unit_basis is not None:
            raise ValueError("a unit basis without any unit declaration")
        return "NONE_DECLARED_THE_REPORTED_VALUES_ARE_RETAINED_DATA_AND_NOT_A_CONVERTED_QUANTITY"
    if unit_basis not in UNIT_BASES:
        # A declaration with no stated origin is a bare assertion, so it is refused.
        raise ValueError("a unit declaration requires its stated basis")
    stated = " ".join(f"{key}={value}" for key, value in
                      (("mass", mass_unit), ("position", position_unit)) if value is not None)
    # Version-qualified on purpose: the declaration was made against the definitions of the
    # version this document declares, and says nothing about any other version.
    return (f"{stated} basis={unit_basis} "
            f"declared_against_sdf_version={version} "
            "the_specification_states_no_unit_for_these_elements_so_this_is_the_callers_"
            "declaration_and_never_a_default")


def convert(data, capture_time_us, *, mass_unit=None, position_unit=None, unit_basis=None):
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise ValueError("bounded saved description required")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit caller capture time required")
    text = data.decode("utf-8")
    root = ET.fromstring(text)
    if root.tag != "sdf":
        raise ValueError("a saved SDF description is required")
    version = root.get("version")
    if version not in VERSIONS:
        # Only the versions whose definitions were read at the pin are accepted; a version name
        # alone never qualifies compatibility.
        raise ValueError("the declared SDF version is not one this selector has read definitions for")
    declaration = declared_units(mass_unit, position_unit, unit_basis, version)
    models = root.findall("model")
    if len(models) != 1:
        raise ValueError("exactly one declared model is read at a time")
    model = models[0]
    model_name = declared_name(model, "model")
    rows, unselected = [], 0
    links, joints = {}, {}
    for index, element in enumerate(list(model)):
        if element.tag == "link":
            name = declared_name(element, "link")
            if name in links:
                raise ValueError("duplicate declared link name")
            if len(links) >= MAX_LINKS:
                raise ValueError("declared links exceed bound")
            inertial = only_child(element, "inertial", "link")
            mass = None if inertial is None else only_child(inertial, "mass", "inertial")
            mass_text = None if mass is None else (mass.text or "").strip()
            mass_value = None
            if mass_text is not None:
                if not NUMBER.fullmatch(mass_text):
                    raise ValueError("a declared mass must be a complete number")
                mass_value = finite_value(mass_text, "mass")
            x, y, z, rotation, frame = pose_values(
                None if inertial is None else only_child(inertial, "pose", "inertial"), version)
            links[name] = dict(index=index, mass=mass_text, mass_value=mass_value,
                               x=x, y=y, z=z, rotation=rotation, frame=frame)
        elif element.tag == "joint":
            name = declared_name(element, "joint")
            if name in joints:
                raise ValueError("duplicate declared joint name")
            if len(joints) >= MAX_JOINTS:
                raise ValueError("declared joints exceed bound")
            parent_element = only_child(element, "parent", "joint")
            child_element = only_child(element, "child", "joint")
            parent = "" if parent_element is None else (parent_element.text or "").strip()
            child = "" if child_element is None else (child_element.text or "").strip()
            if not NAME.fullmatch(parent) or not NAME.fullmatch(child):
                raise ValueError("a declared joint needs a parent and a child frame name")
            if child == WORLD:
                # joint.sdf: the value "world" may not be specified for a child.
                raise ValueError("the declared child frame may not be the world frame")
            joints[name] = dict(index=index, parent=parent, child=child,
                                type=element.get("type") or "")
        else:
            # Every other element, `<include>` among them, is counted and left in the retained
            # document. No include is ever followed: nothing is read from disk or the network.
            unselected += 1
    if not links:
        raise ValueError("a declared model with no link describes no structure")
    parents, children, unresolved = {}, {}, 0
    for name, joint in joints.items():
        # A name this document does not declare is NOT malformed. `<include>` is real and is never
        # followed here, so such a reference is an unresolved external one: it is recorded as
        # unknown and counted, rather than refused or quietly resolved to something.
        for role in ("parent", "child"):
            referenced = joint[role]
            joint[role + "_resolution"] = ("WORLD_FRAME" if referenced == WORLD
                                           else "DECLARED_IN_THIS_DOCUMENT" if referenced in links
                                           else "NOT_DECLARED_IN_THIS_DOCUMENT_NOT_RESOLVED_HERE")
            unresolved += joint[role + "_resolution"].startswith("NOT_DECLARED")
        if joint["child"] not in links:
            continue
        if joint["child"] in parents:
            # A second parent makes the description no longer a tree; it is refused rather than
            # silently resolved to whichever joint happened to be read first.
            raise ValueError("a declared link has more than one parent joint")
        parents[joint["child"]] = joint["parent"]
        if joint["parent"] in links:
            children[joint["parent"]] = children.get(joint["parent"], 0) + 1
    depths = {}
    for name in links:
        seen, walk, depth = {name}, name, 0
        # A parent that is the world frame, or that this document does not declare, ends the walk:
        # the depth is the depth within what was actually declared here.
        while walk in parents and parents[walk] in links:
            walk = parents[walk]
            depth += 1
            if walk in seen:
                raise ValueError("declared parent references form a cycle")
            seen.add(walk)
        depths[name] = depth
    rows.append(dict(airframe_record_kind="MODEL", airframe_element_index=0,
                     airframe_declared_sdf_version=version,
                     airframe_model_name_hex=text_hex(model_name),
                     airframe_unselected_child_count=unselected))
    for name, link in sorted(links.items(), key=lambda item: item[1]["index"]):
        rows.append(dict(airframe_record_kind="LINK", airframe_element_index=link["index"],
                         airframe_declared_sdf_version=version,
                         airframe_model_name_hex=text_hex(model_name),
                         airframe_link_name_hex=text_hex(name),
                         airframe_link_parent_hex=(text_hex(parents[name]) if name in parents
                                                   else None),
                         airframe_link_parent_resolution=(
                             None if name not in parents
                             else "WORLD_FRAME" if parents[name] == WORLD
                             else "DECLARED_IN_THIS_DOCUMENT" if parents[name] in links
                             else "NOT_DECLARED_IN_THIS_DOCUMENT_NOT_RESOLVED_HERE"),
                         airframe_link_depth=depths[name],
                         airframe_link_child_count=children.get(name, 0),
                         airframe_reported_mass=link["mass"],
                         airframe_mass_disposition=mass_disposition(link),
                         # A value that is not positive is never presented as a canonical mass:
                         # the declared figure stays in the reported column and the disposition
                         # says what it was, rather than an impossible quantity being offered as
                         # a valid one.
                         airframe_mass_kg=(link["mass_value"] if mass_unit
                                           and link["mass_value"] is not None
                                           and link["mass_value"] > 0 else None),
                         airframe_com_x=link["x"], airframe_com_y=link["y"],
                         airframe_com_z=link["z"],
                         **dict(zip(("airframe_com_x_m", "airframe_com_y_m", "airframe_com_z_m"),
                                    canonical_position(link, position_unit))),
                         airframe_unit_declaration=declaration,
                         airframe_com_rotation_unit=link["rotation"],
                         airframe_com_pose_frame_hex=(text_hex(link["frame"]) if link["frame"]
                                                      else None),
                         airframe_com_frame_basis=(VERSIONS[version]["com_frame"]
                                                   if link["x"] is not None else None)))
    for name, joint in sorted(joints.items(), key=lambda item: item[1]["index"]):
        rows.append(dict(airframe_record_kind="JOINT", airframe_element_index=joint["index"],
                         airframe_declared_sdf_version=version,
                         airframe_model_name_hex=text_hex(model_name),
                         airframe_joint_name_hex=text_hex(name),
                         airframe_joint_type=joint["type"],
                         airframe_parent_name_hex=text_hex(joint["parent"]),
                         airframe_child_name_hex=text_hex(joint["child"]),
                         airframe_parent_is_world=int(joint["parent"] == WORLD),
                         airframe_parent_resolution=joint["parent_resolution"],
                         airframe_child_resolution=joint["child_resolution"],
                         airframe_joint_frame_basis=JOINT_FRAME))
    if len(rows) > MAX_ROWS:
        raise ValueError("declared rows exceed bound")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(record_time_us=capture_time_us, source_document_sha256=digest,
                             airframe_declaration_basis=BASIS,
                             **{key: "" if value is None else value for key, value in row.items()}))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("declared output exceeds bound")
    report = dict(output_records=len(rows), declared_links=len(links), declared_joints=len(joints),
                  declared_masses=sum(1 for link in links.values() if link["mass"] is not None),
                  links_with_a_non_positive_declared_mass=sum(
                      1 for link in links.values()
                      if link["mass_value"] is not None and link["mass_value"] <= 0),
                  links_without_declared_mass=sum(1 for link in links.values()
                                                  if link["mass"] is None),
                  declared_root_links=sum(1 for name in links
                                          if name not in parents or parents[name] == WORLD),
                  world_attached_joints=sum(1 for joint in joints.values()
                                            if joint["parent"] == WORLD),
                  unselected_model_children=unselected,
                  unresolved_joint_references=unresolved,
                  unresolved_basis="a joint frame this document does not declare is left unknown "
                                   "and counted; includes are never followed so nothing is resolved",
                  declared_sdf_version=version,
                  source_sha256=digest, source_xml_hex=text_hex(text), clock="Unknown",
                  pin=PIN, mass_basis=MASS_BASIS, declaration_basis=BASIS,
                  unit_declaration=declaration,
                  accepted_sdf_versions=sorted(VERSIONS),
                  version_basis="element definitions for this version were read at the pin; a "
                                "version name alone never qualifies compatibility",
                  scope="declared description only; no simulator, plugin, controller, flight, "
                        "actuation or measurement; includes are counted and never followed")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller capture clock; the common clock basis stays Unknown")
    parser.add_argument("--mass-unit", choices=MASS_UNITS,
                        help="the unit the declared masses are in; the specification states none")
    parser.add_argument("--position-unit", choices=POSITION_UNITS,
                        help="the unit the declared centre-of-mass positions are in")
    parser.add_argument("--unit-basis", choices=UNIT_BASES,
                        help="where the unit declaration came from; required whenever one is given")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)  # one bounded read; no size/read race
        output, report = convert(data, args.capture_time_us, mass_unit=args.mass_unit,
                                 position_unit=args.position_unit, unit_basis=args.unit_basis)
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, ET.ParseError, RecursionError, UnicodeDecodeError):
        parser.exit(2, "airframe description conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
