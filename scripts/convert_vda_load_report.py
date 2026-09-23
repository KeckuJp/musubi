#!/usr/bin/env python3
"""The load a mobile robot reports it is carrying, from a saved VDA5050 state record.

Passive. Nothing is dispatched, ordered, executed or acknowledged. This is not warehouse
management, not robot dispatch, and **never evidence that a physical item moved**.

Why it exists. The adopted VDA5050 state path already carries the order and node identifiers, the
reporting instance, the action id/status/type, the pose with its localization qualification, the
errors and the clock -- but it emits **no load column at all**, and `loads` is the only statement
the standard makes about what the vehicle is actually carrying. Without it an engineer has an
`actionStatus` of `FINISHED` and a free-text `actionType`, and the pinned schema is explicit that
neither is authoritative:

    actionStates[].actionType -- "Optional: Only for informational or visualization purposes.
                                  Order knows the type."

Only `actionId` and `actionStatus` are required there, and `actionType` carries **no enum**, so a
saved *state* record cannot tell a standard action from a vendor's custom one. This bridge does not
invent that distinction; it states it.

The `loads` semantics below are the pinned schema's own words, at the commits this estate already
adopted -- VDA5050 2.1.0 `511d01d71587e8a3dc5e71d4f41dc52466f4284c` and 3.0.0
`e9ba560b0e2d3f66526550ad5d61b8a2ad936172`, `json_schemas/state.schema`. Both define the same six
load fields, all optional.

No new decoder is added: the adopted converter already preserves the exact state record in
`source_record_hex`, and this reads the load array out of that.
"""
import argparse
import csv
import decimal
import hashlib
import json
from pathlib import Path
import sys

try:
    from scripts.convert_declared_expectation_check import guarded, text
    # The adopted producer's OWN strict object hook, imported rather than re-implemented, so this
    # bridge enforces the same duplicate-key rule at its own entry instead of a similar one.
    from scripts.convert_ardupilot_battery_csv import unique_object
except ModuleNotFoundError:  # running the file directly
    from convert_declared_expectation_check import guarded, text
    from convert_ardupilot_battery_csv import unique_object

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
ROW_LIMIT = 200000
COLUMN_LIMIT = 256
LOAD_LIMIT = 64
TIME_LIMIT = 2**63
# The two versions whose `json_schemas/state.schema` was actually read for the load semantics below.
# Another version is refused at entry rather than read under these meanings.
QUALIFIED_VERSIONS = ("2.1.0", "3.0.0")
# Columns the adopted VDA state converter always writes. Requiring them is how this bridge refuses
# arbitrary JSON: it reads that producer's output, not any file with a hex blob in it.
PRODUCER_COLUMNS = ("vda_record_kind", "vda_selected_version_hex",
                    "vda_instance_manufacturer_hex", "vda_instance_serial_number_hex",
                    "vda_order_id_hex", "vda_order_update_id")
# The producer labels every row it writes when it is asked for detail rows, and only the row it
# labels SUMMARY carries the state record. The detail kinds repeat that record by reference, so
# reading one as a state record would count the same reported load again.
SUMMARY_KIND = "SUMMARY"
DETAIL_KINDS = ("ACTION", "ERROR", "ERROR_REFERENCE")
# What the preserved record must agree with, when the producer supplied a value for it.
IDENTITY_CHECKS = (("vda_order_id_hex", "orderId", True),
                   ("vda_order_update_id", "orderUpdateId", False),
                   ("vda_instance_manufacturer_hex", "manufacturer", True),
                   ("vda_instance_serial_number_hex", "serialNumber", True))
CHECKED = ("CARRIED_FROM_THE_ADOPTED_PRODUCERS_OWN_SUMMARY_COLUMNS_AND_CHECKED_AGAINST_THE_"
           "PRESERVED_RECORD_THEY_IDENTIFY_SO_A_ROW_CANNOT_NAME_AN_ORDER_OR_AN_INSTANCE_THE_"
           "RECORD_DOES_NOT_REPORT")
NOT_SUPPLIED = ("NOT_SUPPLIED_BY_THE_PRODUCER_OUTPUT_FOR_THIS_RECORD_SO_IT_IS_LEFT_BLANK_AND_IS_"
                "NEVER_DERIVED_OR_INVENTED_HERE")
# The pinned schema's own wording for the three states of the `loads` array. The distinction is the
# whole point: a robot that cannot tell whether it is loaded and a robot that reports itself empty
# are different reports, and neither is an observation of a physical item.
ABSENT = ("LOADS_ARRAY_ABSENT_THE_ROBOT_DOES_NOT_REPORT_WHETHER_IT_IS_CARRYING_ANYTHING_THE_PINNED_"
          "SCHEMA_SAYS_LEAVE_THE_ARRAY_OUT_IF_THE_ROBOT_CANNOT_DETERMINE_LOAD_STATE_SO_THIS_IS_"
          "UNAVAILABLE_AND_IS_NEVER_READ_AS_EMPTY_OR_AS_UNLOADED")
EMPTY = ("LOADS_ARRAY_EMPTY_THE_ROBOT_REPORTS_ITSELF_UNLOADED_WHICH_THE_PINNED_SCHEMA_STATES_"
         "DIRECTLY_IT_IS_THE_ROBOTS_OWN_REPORT_AND_NOT_A_VERIFIED_OBSERVATION_THAT_NOTHING_IS_"
         "PHYSICALLY_PRESENT")
REPORTED = ("LOAD_REPORTED_BY_THE_ROBOT_IN_ITS_OWN_STATE_MESSAGE_IT_IS_A_SELF_REPORT_AND_IS_NEVER_"
            "A_SCAN_A_WEIGHING_CERTIFICATE_OR_EVIDENCE_THAT_AN_ITEM_WAS_PICKED_PLACED_OR_MOVED")
# `loadId`: "Unique identification number of the load (e.g., barcode or RFID). Empty field, if the
# robot can identify the load, but did not identify the load yet." An empty string is therefore a
# third state and not a missing field.
IDENTIFIED = "LOAD_ID_REPORTED_AS_THE_ROBOT_STATED_IT_AND_NEVER_VERIFIED_AGAINST_ANY_REGISTRY"
UNIDENTIFIED = ("LOAD_ID_EMPTY_WHICH_THE_PINNED_SCHEMA_DEFINES_AS_THE_ROBOT_BEING_ABLE_TO_IDENTIFY_"
                "THE_LOAD_BUT_NOT_HAVING_IDENTIFIED_IT_YET_SO_IT_IS_NEITHER_AN_IDENTITY_NOR_AN_"
                "ABSENT_FIELD")
NO_ID = "LOAD_ID_ABSENT_FROM_THE_REPORT_THE_FIELD_IS_OPTIONAL_AND_WAS_NOT_SENT"
ACTION_AUTHORITY = (
    "THE_SAVED_STATE_MESSAGE_CANNOT_TELL_A_STANDARD_ACTION_FROM_A_CUSTOM_ONE_THE_PINNED_SCHEMA_"
    "REQUIRES_ONLY_ACTIONID_AND_ACTIONSTATUS_IN_ACTIONSTATES_AND_DESCRIBES_ACTIONTYPE_AS_OPTIONAL_"
    "ONLY_FOR_INFORMATIONAL_OR_VISUALIZATION_PURPOSES_WITH_THE_ORDER_KNOWING_THE_TYPE_AND_IT_"
    "DECLARES_NO_ENUM_OF_TYPES_SO_A_FREE_TEXT_ACTIONTYPE_SUPPLIES_NO_VENDOR_SEMANTICS_AND_AN_"
    "ACTIONSTATUS_OF_FINISHED_IS_A_REPORT_THAT_AN_ACTION_ENDED_AND_IS_NEVER_EVIDENCE_THAT_A_LOAD_"
    "WAS_PICKED_PLACED_PUT_AWAY_REPLENISHED_OR_PHYSICALLY_MOVED")
LOAD_BASIS = (
    "A_LOAD_THE_ROBOT_REPORTED_ABOUT_ITSELF_IN_ONE_SAVED_STATE_RECORD_CARRIED_WITH_THE_ORDER_AND_"
    "INSTANCE_IDENTIFIERS_THAT_RECORD_ALREADY_CARRIED_WEIGHT_IS_KILOGRAMS_AND_THE_BOUNDING_BOX_"
    "DIMENSIONS_ARE_METRES_BECAUSE_THE_PINNED_SCHEMA_STATES_THOSE_UNITS_THE_BOUNDING_BOX_REFERENCE_"
    "COORDINATES_CARRY_NO_UNIT_IN_THAT_SCHEMA_AND_ARE_NOT_GIVEN_ONE_HERE_NOTHING_IN_THIS_ROW_IS_A_"
    "WAREHOUSE_TRANSACTION_A_DISPATCH_AN_INVENTORY_FACT_OR_PROOF_THAT_AN_ITEM_MOVED")
FIELDS = ["record_time_us", "vda_record_kind", "load_report_state", "load_index",
          "load_id_hex", "load_identification", "load_type_hex", "load_position_hex",
          "load_weight_kg", "load_length_m", "load_width_m", "load_height_m",
          "bbox_reference_x", "bbox_reference_y", "bbox_reference_z", "bbox_reference_theta",
          "order_id_hex", "order_update_id", "instance_manufacturer_hex",
          "instance_serial_number_hex", "action_type_authority", "load_basis",
          "passthrough_identity_basis", "state_record_sha256", "source_sha256"]
# Carried through from the adopted converter's own summary columns when present, never re-derived.
PASSTHROUGH = {"order_id_hex": "vda_order_id_hex", "order_update_id": "vda_order_update_id",
               "instance_manufacturer_hex": "vda_instance_manufacturer_hex",
               "instance_serial_number_hex": "vda_instance_serial_number_hex"}


def number(value, name, *, unit):
    """One reported quantity, refused rather than rounded when it is not one.

    The value arrives as an `int` or a `decimal.Decimal` carrying the record's exact digits, so a
    nonzero quantity that `float()` would flatten to zero is still nonzero when it is checked here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, decimal.Decimal)):
        raise ValueError(f"{name} is not a reported number")
    exact = decimal.Decimal(value)
    if not exact.is_finite():
        raise ValueError(f"{name} is not finite")
    carried = float(exact)
    if carried in (float("inf"), float("-inf")):
        raise ValueError(f"{name} overflows to infinity when read as a number; it is refused "
                         "rather than carried as a bound")
    if carried == 0.0 and exact != 0:
        raise ValueError(f"{name} is not zero but underflows to zero when read as a number; it is "
                         "refused rather than carried as a zero")
    if unit == "kg" and exact < 0:
        # "Absolute weight of the load measured in kg" -- an absolute weight is not negative.
        raise ValueError(f"{name} is negative, which the pinned schema's absolute weight is not")
    if unit == "m" and exact < 0:
        raise ValueError(f"{name} is a negative absolute length, which the pinned schema excludes")
    # The record's own digits, not a float round-trip of them.
    return str(int(exact)) if exact == exact.to_integral_value() else str(exact)


def load_row(entry, index):
    """One reported load, exactly as the pinned schema defines its six optional fields."""
    if not isinstance(entry, dict):
        raise ValueError("a reported load is an object in the pinned schema")
    unknown = set(entry) - {"loadId", "loadType", "loadPosition", "boundingBoxReference",
                            "loadDimensions", "weight"}
    row = {"vda_record_kind": "LOAD", "load_report_state": REPORTED, "load_index": index,
           "load_identification": NO_ID}
    if "loadId" in entry:
        value = entry["loadId"]
        if not isinstance(value, str):
            raise ValueError("loadId is a string in the pinned schema")
        row["load_id_hex"] = text(value) if value else ""
        row["load_identification"] = IDENTIFIED if value else UNIDENTIFIED
    for key, column in (("loadType", "load_type_hex"), ("loadPosition", "load_position_hex")):
        if key in entry:
            if not isinstance(entry[key], str):
                raise ValueError(f"{key} is a string in the pinned schema")
            row[column] = text(entry[key])
    if "weight" in entry:
        row["load_weight_kg"] = number(entry["weight"], "weight", unit="kg")
    if "loadDimensions" in entry:
        dimensions = entry["loadDimensions"]
        # Optional is not nullable: the pinned schema types this as an object and permits no null,
        # so a present null is malformed and is refused rather than read as "not reported".
        if not isinstance(dimensions, dict) or not {"length", "width"} <= set(dimensions):
            raise ValueError("loadDimensions requires length and width in the pinned schema")
        for key, column in (("length", "load_length_m"), ("width", "load_width_m"),
                            ("height", "load_height_m")):
            if key in dimensions:
                row[column] = number(dimensions[key], key, unit="m")
        unknown |= {"loadDimensions." + k for k in set(dimensions) - {"length", "width", "height"}}
    if "boundingBoxReference" in entry:
        reference = entry["boundingBoxReference"]
        # The same rule: a present null is refused, an absent key is genuinely not reported.
        if not isinstance(reference, dict) or not {"x", "y", "z"} <= set(reference):
            raise ValueError("boundingBoxReference requires x, y and z in the pinned schema")
        for key in ("x", "y", "z", "theta"):
            if key in reference:
                # No unit: the pinned schema states one for the dimensions and for weight, and
                # states none for these coordinates. None is invented here.
                row["bbox_reference_" + key] = number(reference[key], key, unit="")
        unknown |= {"boundingBoxReference." + k
                    for k in set(reference) - {"x", "y", "z", "theta"}}
    return row, unknown


def read_rows(data, time_column, source_column):
    """The adopted converter's own output, exactly as it wrote it."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized saved observations")
    reader = csv.reader(data.decode("utf-8-sig").splitlines(True), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("saved observations carry no header")
    except csv.Error as failure:
        raise ValueError(f"the saved CSV cannot be parsed as written: {failure}")
    if not 1 <= len(header) <= COLUMN_LIMIT or len(set(header)) != len(header):
        raise ValueError("unsupported or duplicated saved column names")
    for needed in (time_column, source_column, *PRODUCER_COLUMNS):
        if needed not in header:
            raise ValueError(
                f"column {needed!r} is not in the saved header; this bridge reads the adopted "
                "VDA5050 state converter's own output and refuses any other file. Run that "
                "converter with --vda-actions (or --vda-errors), which is what makes it label "
                "every row with an explicit record kind; its summary-only output does not carry "
                "that label, and a row is never assumed to be a summary here")
    index = {name: position for position, name in enumerate(header)}
    rows, previous = [], None
    for cells in guarded(reader):
        if len(cells) != len(header):
            raise ValueError("saved row width does not match the header")
        if len(rows) >= ROW_LIMIT:
            raise ValueError("saved observations exceed the row bound")
        moment = cells[index[time_column]]
        if not moment.isdigit() or len(moment) > 19 or int(moment) >= TIME_LIMIT:
            raise ValueError("saved time is not a non-negative integer of microseconds in range")
        moment = int(moment)
        if previous is not None and moment < previous:
            raise ValueError("saved time decreases; this bridge never reorders a saved file")
        previous = moment
        rows.append((moment, cells))
    if not rows:
        raise ValueError("saved observations carry no rows")
    return header, index, rows


def convert(data, *, time_column="record_time_us", source_column="source_record_hex",
            kind_column="vda_record_kind"):
    """One row per reported load, and exactly one row when a record reports none."""
    header, index, rows = read_rows(data, time_column, source_column)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    shared = {"action_type_authority": ACTION_AUTHORITY, "load_basis": LOAD_BASIS,
              "source_sha256": digest}
    report = {"state_records_read": 0, "loads_reported": 0,
              "detail_rows_skipped": {kind: 0 for kind in DETAIL_KINDS},
              "records_reporting_unloaded": 0, "records_not_reporting_load_state": 0,
              "unknown_load_properties_at_declared_levels": 0,
              "deeper_unknown_structure": ("RETAINED_ONLY_BY_THE_SOURCE_BINDING_AND_NOT_COUNTED_"
                                           "THE_COUNT_ABOVE_IS_THE_LOAD_ENTRY_AND_ITS_TWO_DECLARED_"
                                           "NESTED_OBJECTS_ONLY_AND_IS_NEVER_A_CLAIM_THAT_EVERY_"
                                           "UNKNOWN_STRUCTURE_WAS_ACCOUNTED_FOR"),
              "dispatched_orders": "NONE_THIS_TOOL_ORDERS_NOTHING_AND_EXECUTES_NOTHING",
              "physical_movement_records": "NOT_DERIVED_A_REPORTED_LOAD_IS_NOT_A_MOVED_ITEM",
              "action_type_authority": ACTION_AUTHORITY, "source_sha256": digest}
    produced = []
    for moment, cells in rows:
        # Only the row the producer labelled SUMMARY carries the exact record. A detail row is
        # skipped and counted, so the skip is visible rather than silent; anything else -- a blank
        # label or a kind this bridge does not know -- is refused, and is never read AS a summary.
        kind = cells[index[kind_column]]
        if kind in DETAIL_KINDS:
            report["detail_rows_skipped"][kind] += 1
            continue
        if kind != SUMMARY_KIND:
            raise ValueError(
                f"row {len(produced)} carries the record kind {kind!r}; this bridge evaluates only "
                f"rows the producer labelled {SUMMARY_KIND!r}, skips {DETAIL_KINDS} with "
                "accounting, and refuses an unlabelled or unknown kind rather than assuming it is "
                "a summary")
        raw = cells[index[source_column]]
        if not raw.startswith("hex:"):
            raise ValueError("the preserved state record is not in the adopted hex form")
        try:
            record = json.loads(
                bytes.fromhex(raw[4:]).decode("utf-8"),
                # The producer's own duplicate-key rule, so a repeated field is refused here too
                # rather than silently resolved to the last one.
                object_pairs_hook=unique_object,
                # The exact decimal text, so a nonzero value that float() would flatten to zero is
                # still nonzero when it is checked. The producer preserves the original spelling in
                # `source_record_hex`, which is what makes this decidable at all.
                parse_float=decimal.Decimal,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("NaN and Infinity are not JSON numbers")))
        except (ValueError, UnicodeDecodeError) as failure:
            raise ValueError(f"the preserved state record does not read back as JSON: {failure}")
        if not isinstance(record, dict):
            raise ValueError("a saved state record is a JSON object")
        report["state_records_read"] += 1
        selected = cells[index["vda_selected_version_hex"]]
        if not selected.startswith("hex:"):
            raise ValueError("the producer's selected-version column is not in the adopted hex form")
        selected = bytes.fromhex(selected[4:]).decode("utf-8")
        if selected not in QUALIFIED_VERSIONS:
            raise ValueError(
                f"the producer qualified this record as VDA5050 {selected!r}; the load semantics "
                f"here were read from the pinned schemas of {QUALIFIED_VERSIONS} only, so another "
                "version is refused at entry rather than read under meanings never checked for it")
        if record.get("version") != selected:
            raise ValueError("the preserved record's version contradicts the version the producer "
                             "qualified it as; the record and the row would not describe each other")
        blank = []
        for column, key, hexed in IDENTITY_CHECKS:
            supplied = cells[index[column]]
            if supplied == "":
                # The producer did not carry this one for this record; it is left blank and is
                # never derived from the preserved record to fill the gap.
                blank.append(column)
                continue
            reported = record.get(key, KeyError)
            expected = ("hex:" + str(reported).encode("utf-8").hex() if hexed
                        else str(reported)) if reported is not KeyError else None
            if expected != supplied:
                raise ValueError(
                    f"the producer column {column!r} does not match {key!r} in the record it "
                    "preserves; a row must not name an order or an instance the record does not "
                    "report")
        identity_basis = CHECKED if not blank else (
            NOT_SUPPLIED if len(blank) == len(IDENTITY_CHECKS)
            else CHECKED + "_EXCEPT_NOT_SUPPLIED_FOR_" + "+".join(blank))
        base = dict(shared, passthrough_identity_basis=identity_basis, record_time_us=moment,
                    state_record_sha256="sha256:" + hashlib.sha256(
                        bytes.fromhex(raw[4:])).hexdigest())
        for column, source in PASSTHROUGH.items():
            base[column] = cells[index[source]] if source in index else ""
        if "loads" in record and record["loads"] is None:
            # The pinned schema types `loads` as an array and permits no null. Optional means the
            # key may be left out, which is a different report -- so this is refused rather than
            # quietly counted as "the robot cannot determine its load state".
            raise ValueError("the reported loads value is null, which the pinned schema does not "
                             "permit; an unreported load state leaves the key out entirely")
        loads = record.get("loads", KeyError)
        if loads is KeyError:
            report["records_not_reporting_load_state"] += 1
            produced.append(dict(base, vda_record_kind="LOAD_STATE", load_report_state=ABSENT,
                                 load_identification="", load_index=""))
            continue
        if not isinstance(loads, list):
            raise ValueError("the reported loads value is an array in the pinned schema")
        if not loads:
            report["records_reporting_unloaded"] += 1
            produced.append(dict(base, vda_record_kind="LOAD_STATE", load_report_state=EMPTY,
                                 load_identification="", load_index=""))
            continue
        if len(loads) > LOAD_LIMIT:
            raise ValueError("more reported loads in one record than the finite bound allows")
        for position, entry in enumerate(loads):
            row, unknown = load_row(entry, position)
            report["loads_reported"] += 1
            report["unknown_load_properties_at_declared_levels"] += len(unknown)
            produced.append(dict(base, **row))
    if not produced:
        raise ValueError("no saved state summary row was selected; nothing is reported")
    output = ",".join(FIELDS) + "\n"
    output += "".join(",".join(str(line.get(name, "")) for name in FIELDS) + "\n"
                      for line in produced)
    encoded = len(output.encode("utf-8"))
    if encoded > OUTPUT_LIMIT:
        raise ValueError("expanded output exceeds bound")
    report["output_bytes"] = encoded
    report["rows"] = len(produced)
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="the adopted VDA state converter's own output")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--time-column", default="record_time_us")
    parser.add_argument("--source-column", default="source_record_hex")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(args.input.read_bytes(), time_column=args.time_column,
                                 source_column=args.source_column)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, csv.Error) as failure:
        print(f"reported-load conversion failed; no load claim: {failure}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
