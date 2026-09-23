#!/usr/bin/env python3
"""Saved QGroundControl `.plan` file -> mission configuration observations.

A `.plan` is a **configuration record an operator saved**, and that is all this path treats it as.
Reading one is never a transmission, an actuation, an execution, an acceptance or an authorisation:
no link is opened, no vehicle is contacted, nothing is armed and nothing is sent. A plan that
contains `MAV_CMD_DO_SET_SERVO` is a file that contains that field.

**Task configured is not task accepted, and accepted is not executed.** No row here says a mission
ran, reached an item, or was approved by anything.

Schema, from the QGroundControl **Plan File Format**: `fileType` is `"Plan"`, `version` is `1`,
`groundStation` is the writing station; the `mission` object carries `version` (2), `firmwareType`
(MAV_AUTOPILOT), `vehicleType` (MAV_TYPE), `cruiseSpeed` ("the default forward speed for Fixed wing
or VTOL vehicles"), `hoverSpeed` ("the default forward speed for multi-rotor vehicles"),
`plannedHomePosition` (`[latitude, longitude, AMSL altitude]`) and `items`; a `SimpleItem` carries
`command` (MAV_CMD), `frame` (MAV_FRAME), `autoContinue`, `doJumpId` and `params` -- which that
format states are **"MISSION_ITEM.param1,2,3,4,x,y,z (values depends on the particular MAV_CMD)"**,
so they are retained positionally and **not one of them is interpreted here**.
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
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant

LIMIT = 8 * 1024 * 1024
MAX_ITEMS = 4096
MAX_PARAMS = 7
MAX_TEXT = 512

# The one file format read. A file that declares another version is refused rather than read with
# these meanings -- the same discipline the adopted image path applies to its exporter release.
QUALIFIED_FILE_TYPE = "Plan"
QUALIFIED_FILE_VERSION = 1
QUALIFIED_MISSION_VERSION = 2
FORMAT = ("QGroundControl Plan File Format: fileType Plan, version 1, mission version 2; "
          "params are MISSION_ITEM.param1,2,3,4,x,y,z and their meaning depends on the command")

# Enum names read at the dialect this repository already pins,
# mavlink/mavlink@3203f89c510337c0088244735c6a5056c52b5a28: MAV_CMD and MAV_FRAME in
# `message_definitions/v1.0/common.xml`, MAV_AUTOPILOT and MAV_TYPE in `minimal.xml` of the same
# commit. This is a FINITE NAMED SUBSET: that common.xml declares 170 MAV_CMD entries and nine are
# named here. Every other code is retained as an unknown code beside its enum space -- no name is
# supplied from memory, and an unnamed code is never treated as absent.
DIALECT = "mavlink/mavlink@3203f89c510337c0088244735c6a5056c52b5a28"
MAV_CMD_NAMED = {
    16: "MAV_CMD_NAV_WAYPOINT", 20: "MAV_CMD_NAV_RETURN_TO_LAUNCH", 21: "MAV_CMD_NAV_LAND",
    22: "MAV_CMD_NAV_TAKEOFF", 178: "MAV_CMD_DO_CHANGE_SPEED", 179: "MAV_CMD_DO_SET_HOME",
    181: "MAV_CMD_DO_SET_RELAY", 183: "MAV_CMD_DO_SET_SERVO", 189: "MAV_CMD_DO_LAND_START",
}
MAV_CMD_DECLARED_ENTRIES = 170
MAV_FRAME_NAMED = {
    0: "MAV_FRAME_GLOBAL", 1: "MAV_FRAME_LOCAL_NED", 2: "MAV_FRAME_MISSION",
    3: "MAV_FRAME_GLOBAL_RELATIVE_ALT", 6: "MAV_FRAME_GLOBAL_RELATIVE_ALT_INT",
    10: "MAV_FRAME_GLOBAL_TERRAIN_ALT", 11: "MAV_FRAME_GLOBAL_TERRAIN_ALT_INT",
}
MAV_AUTOPILOT_NAMED = {0: "MAV_AUTOPILOT_GENERIC", 3: "MAV_AUTOPILOT_ARDUPILOTMEGA",
                       12: "MAV_AUTOPILOT_PX4"}
MAV_TYPE_NAMED = {1: "MAV_TYPE_FIXED_WING", 2: "MAV_TYPE_QUADROTOR", 10: "MAV_TYPE_GROUND_ROVER",
                  20: "MAV_TYPE_VTOL_TAILSITTER_QUADROTOR"}
UNKNOWN_CODE = "UNKNOWN_CODE_RETAINED"

PLAN_DECLARATION = "PLAN_DECLARATION"
MISSION_ITEM = "MISSION_ITEM"
# The one non-simple type this format revision names. Anything else is unrecognised,
# and calling it a ComplexItem would state something the file does not.
COMPLEX_ITEM = "ComplexItem"
RECORD_BASIS = (
    "OBSERVED_RECORD_a_configuration_file_an_operator_saved_read_passively_it_is_never_a_"
    "transmission_an_actuation_an_execution_an_acceptance_or_an_authorisation_and_a_command_field_"
    "inside_it_is_a_field_inside_a_file_task_configured_is_not_task_accepted_and_accepted_is_not_"
    "executed")
PARAM_BASIS = (
    "the_seven_positional_parameters_exactly_as_written_the_plan_format_states_their_values_depend_"
    "on_the_particular_command_so_none_of_them_is_interpreted_named_or_scaled_here")
HOME_BASIS = (
    "the_PLANNED_home_position_the_file_declares_for_the_map_which_is_a_configured_value_and_never_"
    "an_observed_position_a_measured_fix_or_a_take_off_that_happened")
SPEED_BASIS = (
    "the_plans_own_default_speeds_in_metres_per_second_the_format_states_cruise_is_for_fixed_wing_"
    "or_VTOL_and_hover_is_for_multi_rotor_neither_is_a_commanded_or_a_measured_speed")
ITEM_KIND_BASIS = (
    "how_many_items_of_the_saved_list_this_path_read_as_SimpleItem_rows_how_many_it_counted_as_the_"
    "formats_own_ComplexItem_without_decoding_and_how_many_carried_a_type_word_this_format_revision_"
    "does_not_name_which_are_counted_separately_because_an_unrecognised_kind_is_NOT_a_ComplexItem_"
    "and_flattening_or_relabelling_either_would_state_something_the_file_does_not")
ENUM_BASIS = (
    "which_enum_space_each_code_belongs_to_as_the_plan_format_states_command_is_MAV_CMD_frame_is_"
    "MAV_FRAME_firmwareType_is_MAV_AUTOPILOT_and_vehicleType_is_MAV_TYPE_names_come_only_from_the_"
    "pinned_dialect_and_an_unlisted_code_is_retained_rather_than_guessed")

COLUMNS = [
    "record_time_us", "source_plan_sha256", "plan_record_kind",
    "file_type_declared", "file_version_declared", "ground_station_hex",
    "mission_version_declared", "firmware_type_code", "firmware_type_name",
    "vehicle_type_code", "vehicle_type_name", "enum_space_basis",
    "planned_home_latitude_deg", "planned_home_longitude_deg", "planned_home_amsl_altitude_m",
    "planned_home_basis", "plan_cruise_speed_m_s", "plan_hover_speed_m_s", "plan_speed_basis",
    "simple_item_count", "complex_item_count", "unrecognised_item_count", "item_kind_basis",
    "geofence_section_present", "rally_points_section_present",
    "item_index", "item_do_jump_id", "item_command_code", "item_command_name",
    "item_frame_code", "item_frame_name", "item_auto_continue_code", "item_auto_continue_reported",
    "item_param_1", "item_param_2", "item_param_3", "item_param_4", "item_param_5",
    "item_param_6", "item_param_7", "item_param_basis", "record_basis",
]


def hexed(text, what):
    if not isinstance(text, str) or not 1 <= len(text.encode()) <= MAX_TEXT:
        raise ValueError("invalid " + what)
    return "hex:" + text.encode().hex()


def whole(value, what, low=0, high=2**31 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("invalid " + what)
    return value


def number(value, what, low=None, high=None):
    """A finite JSON number kept exactly as written; a blank is never a zero.

    The document is parsed with `parse_float=Decimal`, so a magnitude the writer put in the file
    lexically cannot become an infinity or a silent zero on the way to the common reader.
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
        raise ValueError(what + " outside its documented range")
    text = str(quantity)
    reachable = float(text)
    if not math.isfinite(reachable):
        raise ValueError(what + " cannot be carried: it overflows the reader's float")
    if reachable == 0 and quantity != 0:
        raise ValueError(what + " cannot be carried: it underflows to zero at the reader")
    return text


def named(code, table):
    """A code with its pinned name, or the code retained as an explicit unknown."""
    return table.get(code, UNKNOWN_CODE)


def plan_declaration(document, mission, digest, capture_time_us, simple, complex_items,
                     unrecognised_items):
    row = dict.fromkeys(COLUMNS, "")
    home = mission.get("plannedHomePosition")
    row.update({
        "record_time_us": capture_time_us, "source_plan_sha256": digest,
        "plan_record_kind": PLAN_DECLARATION,
        "file_type_declared": document["fileType"],
        "file_version_declared": document["version"],
        "mission_version_declared": mission["version"],
        "enum_space_basis": ENUM_BASIS, "planned_home_basis": HOME_BASIS,
        "plan_speed_basis": SPEED_BASIS, "item_kind_basis": ITEM_KIND_BASIS,
        "record_basis": RECORD_BASIS, "item_param_basis": PARAM_BASIS,
        "simple_item_count": simple, "complex_item_count": complex_items,
        "unrecognised_item_count": unrecognised_items,
        "geofence_section_present": "PRESENT" if "geoFence" in document else "ABSENT",
        "rally_points_section_present": "PRESENT" if "rallyPoints" in document else "ABSENT",
    })
    if "groundStation" in document:
        row["ground_station_hex"] = hexed(document["groundStation"], "groundStation")
    for column, key, table in (("firmware_type", "firmwareType", MAV_AUTOPILOT_NAMED),
                               ("vehicle_type", "vehicleType", MAV_TYPE_NAMED)):
        if key in mission:
            code = whole(mission[key], key, 0, 255)
            row[column + "_code"], row[column + "_name"] = code, named(code, table)
    for column, key in (("plan_cruise_speed_m_s", "cruiseSpeed"),
                        ("plan_hover_speed_m_s", "hoverSpeed")):
        if key in mission:
            row[column] = number(mission[key], key, 0, 1000)
    if home is not None:
        # "[latitude, longitude, AMSL altitude]" -- three positions, in that documented order.
        if not isinstance(home, list) or len(home) != 3:
            raise ValueError("plannedHomePosition is three positions")
        row["planned_home_latitude_deg"] = number(home[0], "planned home latitude", -90, 90)
        row["planned_home_longitude_deg"] = number(home[1], "planned home longitude", -180, 180)
        row["planned_home_amsl_altitude_m"] = number(home[2], "planned home altitude",
                                                     -20_000, 100_000)
    return row


def mission_item(item, index, header):
    """One SimpleItem, in the file's own field names. Nothing is added and nothing is reordered."""
    row = dict(header)
    row.update({"plan_record_kind": MISSION_ITEM, "item_index": index,
                "simple_item_count": "", "complex_item_count": "", "unrecognised_item_count": "",
                "geofence_section_present": "", "rally_points_section_present": "",
                "planned_home_latitude_deg": "", "planned_home_longitude_deg": "",
                "planned_home_amsl_altitude_m": "", "planned_home_basis": "",
                "plan_cruise_speed_m_s": "", "plan_hover_speed_m_s": "", "plan_speed_basis": "",
                "item_kind_basis": ""})
    for key in ("command", "frame", "params", "doJumpId", "autoContinue"):
        if key not in item:
            raise ValueError("a SimpleItem must declare " + key)
    command = whole(item["command"], "command", 0, 65_535)
    frame = whole(item["frame"], "frame", 0, 255)
    row["item_command_code"], row["item_command_name"] = command, named(command, MAV_CMD_NAMED)
    row["item_frame_code"], row["item_frame_name"] = frame, named(frame, MAV_FRAME_NAMED)
    row["item_do_jump_id"] = whole(item["doJumpId"], "doJumpId", 0, 2**31 - 1)
    auto = item["autoContinue"]
    if not isinstance(auto, bool):
        raise ValueError("autoContinue is the boolean the format declares")
    row["item_auto_continue_code"] = 1 if auto else 0
    row["item_auto_continue_reported"] = ("AUTO_CONTINUE_DECLARED_TRUE" if auto
                                          else "AUTO_CONTINUE_DECLARED_FALSE")
    params = item["params"]
    if not isinstance(params, list) or len(params) != MAX_PARAMS:
        # The format names seven positions. A shorter list would silently shift every later
        # parameter into a position that means something else.
        raise ValueError("a SimpleItem carries exactly seven positional parameters")
    for position, value in enumerate(params, start=1):
        if value is None:
            # A written null is an absent parameter, not a zero -- the format uses it for the
            # positions a particular command does not use.
            continue
        row["item_param_%d" % position] = number(value, "param %d" % position)
    return row


def convert(data, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("invalid capture time")
    if len(data) > LIMIT:
        raise ValueError("input exceeds bound")
    try:
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data
        if "\x00" in text:
            raise ValueError("NUL byte in plan")
        document = json.loads(text, object_pairs_hook=unique_object,
                              parse_constant=invalid_constant, parse_float=decimal.Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed plan") from exc
    if not isinstance(document, dict):
        raise ValueError("a plan file is one JSON object")
    if document.get("fileType") != QUALIFIED_FILE_TYPE:
        raise ValueError("only a file declaring the qualified fileType is read")
    # `True == 1` in Python, so a JSON `true` would otherwise pass as the pinned version. The
    # existing integer guard refuses a boolean by type before the value is compared at all.
    if whole(document.get("version"), "plan file version", 0, 255) != QUALIFIED_FILE_VERSION:
        raise ValueError("only the qualified plan file version is read")
    mission = document.get("mission")
    if not isinstance(mission, dict):
        raise ValueError("a plan file carries its mission object")
    if whole(mission.get("version"), "mission version", 0, 255) != QUALIFIED_MISSION_VERSION:
        raise ValueError("only the qualified mission version is read")
    items = mission.get("items")
    if not isinstance(items, list):
        raise ValueError("the mission object carries its item list")
    if len(items) > MAX_ITEMS:
        raise ValueError("plan exceeds item bound")

    digest = hashlib.sha256(
        data if isinstance(data, (bytes, bytearray)) else data.encode()).hexdigest()
    # The item index is the position in the SAVED list, so it is taken from that list and never
    # from a filtered one: an item this path does not read leaves a GAP in the indices rather than
    # renumbering everything after it. `doJumpId` is the file's own identifier and stays whatever
    # the file wrote, independent of this index.
    simple, complex_kinds, unrecognised_kinds = [], [], []
    for document_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("each mission item is an object")
        kind = item.get("type")
        declared = item.get("complexItemType")
        if declared is not None and (not isinstance(declared, str) or not declared):
            raise ValueError("a declared complexItemType is a non-empty string")
        if kind == "SimpleItem":
            simple.append((document_index, item))
        elif kind == COMPLEX_ITEM:
            # The format's own complex item: counted with its declared type retained, and never
            # flattened into items this path did not read.
            complex_kinds.append((kind, declared))
        elif isinstance(kind, str) and kind:
            # A type this format revision does not name. It is NOT a ComplexItem and must not be
            # counted as one: it is retained by its own declared word as an unrecognised kind.
            unrecognised_kinds.append((kind, declared))
        else:
            raise ValueError("each mission item declares its type")

    header = plan_declaration(document, mission, digest, capture_time_us, len(simple),
                              len(complex_kinds), len(unrecognised_kinds))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(header)
    for document_index, item in simple:
        writer.writerow(mission_item(item, document_index, header))
        if output.tell() > LIMIT:
            raise ValueError("observation CSV exceeds bound")

    def tally(pairs):
        counted = {}
        for pair in pairs:
            counted[pair] = counted.get(pair, 0) + 1
        return [{"declared_type_hex": hexed(kind, "item type"),
                 "declared_complex_item_type_hex": ("" if declared is None
                                                    else hexed(declared, "complexItemType")),
                 "count": count}
                for (kind, declared), count in sorted(counted.items(),
                                                      key=lambda pair: (pair[0][0],
                                                                        pair[0][1] or ""))]
    report = {
        "source_plan_sha256": digest, "source_bytes": len(data),
        "file_type_declared": document["fileType"], "file_version_declared": document["version"],
        "mission_version_declared": mission["version"],
        "pinned_format": FORMAT, "pinned_dialect": DIALECT,
        "named_command_codes": len(MAV_CMD_NAMED),
        "declared_command_entries_at_the_pin": MAV_CMD_DECLARED_ENTRIES,
        "simple_items_read": len(simple),
        "complex_items_counted_not_decoded": tally(complex_kinds),
        "unrecognised_item_types_retained": tally(unrecognised_kinds),
        "item_index_basis": "the position in the saved item list; an item this path does not read "
                            "leaves a gap in the indices rather than renumbering the rest",
        "unnamed_command_codes": sorted({code for code in
                                         (whole(item["command"], "command", 0, 65_535)
                                          for _, item in simple)
                                         if code not in MAV_CMD_NAMED}),
        "record_basis": RECORD_BASIS,
        "not_produced": "no transmission, no actuation, no arming, no acceptance, no execution "
                        "claim, no parameter interpretation and no mission authoring or "
                        "optimisation",
    }
    if len(json.dumps(report).encode()) > LIMIT:
        raise ValueError("report exceeds bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="saved .plan file")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller clock for when the saved plan was taken in; never a mission "
                             "time and never an execution time")
    args = parser.parse_args()
    try:
        with args.plan.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        output, report = convert(data, args.capture_time_us)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, RecursionError):
        parser.exit(2, "saved plan conversion failed; no mission configuration is claimed\n")


if __name__ == "__main__":
    main()
