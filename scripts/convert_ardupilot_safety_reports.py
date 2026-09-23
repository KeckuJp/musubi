#!/usr/bin/env python3
"""Saved ArduPilot MODE records -> the reported fence/failsafe trigger reason, for the common reader.

Passive import of an export an engineer already saved. No vehicle, link or log is opened, nothing is
commanded, and no geofence geometry is evaluated here.

The adopted `ardupilot-mode` case already ingests the reason **code** and declares it
`Rsn = "reported_mode_reason_code_not_verified_cause"`: the number is carried but its meaning is not,
so a reader cannot tell a fence breach from a battery failsafe from an ordinary pilot command. This
converter adds exactly that meaning and nothing else. It reads the same saved export shape and calls
`convert_ardupilot_mode_csv` for the required-column definition rather than restating it; that module
is not modified.

Meaning comes from one finite version: ArduPilot Plane 4.5.5
`d6ebfc21844e2bc0ab28c8c688b8dae94abccf5a`, `libraries/AP_Vehicle/ModeReason.h`, which is the pin the
adopted case already declares. `enum class ModeReason : uint8_t` runs 0..52 there.

What a row is, and is not:

* it is the autopilot's **own report of why a mode changed**, with the record's own boot clock and the
  original record retained;
* it is **not** a geometry evaluation - nothing here knows where any boundary is, and no position is
  compared with anything;
* it is **not** a control action: this reads a record, it never triggers or clears a failsafe;
* it is **not** a physical cause. A reported `BATTERY_FAILSAFE` says the autopilot applied its battery
  failsafe, not that a battery failed; a mode alone is never read as a cause.

Three separations the enum itself forces, so a reader cannot blur them:

* **commanded is not an event.** `RC_COMMAND 1`, `GCS_COMMAND 2`, `SERVOTEST 30`, `SCRIPTING 32`,
  `MISSION_CMD 41`, `FRSKY_COMMAND 42` and `DDS_COMMAND 52` are commands, and carry no safety domain.
  There is no fall-through: a code outside the pinned range becomes `UNKNOWN_CODE_<n>` with no domain,
  never a safety event;
* **recovery is not breach.** `RADIO_FAILSAFE_RECOVERY 48`, `AVOIDANCE_RECOVERY 15` and
  `FENCE_RETURN_PREVIOUS_MODE 43` report returning, not entering, and are never counted as a breach or
  a failsafe entry;
* **general is not specific.** `FAILSAFE 25` carries the source's own comment "general failsafes,
  prefer specific failsafes over this as much as possible", so its kind stays `UNSPECIFIED_GENERAL`.

A MODE record exists only when the mode changed, so a breach or failsafe that changed no mode is not
in this record and nothing here pretends otherwise.

Retention and refusal, following the adopted route's own pattern rather than a new parser:

* the saved record is retained as **its own characters** - every column, including ones this
  converter does not interpret, and the writer's own quoting - never rebuilt from the columns that
  were read, which would drop the rest and re-quote what remained;
* a **repeated column name** is refused: a later cell of that name would overwrite an earlier one and
  the record could not be read without losing data;
* a **ragged record** is refused in both directions: too few cells would read a neighbouring column's
  value, too many would drop the surplus;
* **broken quoting is refused, not repaired.** The `csv` reader's default settings silently read an
  unterminated `"cell` as `cell` and `"a"x` as `ax`, so the cells handed back would not be the
  characters the writer wrote; strict parsing is used and such a record is refused;
* numbers are **ASCII decimal only**, inside the producer's own `QMBB` domains. `str.isdigit()` is
  true for other Unicode digit characters that `int()` reads as a different number or refuses, so it
  is not used.

The reason enum was read at the **Plane 4.5.5 tree only**. Any other declared vehicle version names a
different release whose tree was not read, so those records keep their raw code and are explicitly
**not interpreted** - no vehicle breadth is claimed and the Plane pin is not attached to them.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_ardupilot_mode_csv import MODE_SCHEMAS, REQUIRED

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
MAX_ROWS = 500_000
MAX_LINE = 64 * 1024
PRODUCER_PIN = "ardupilot-plane-4.5.5-d6ebfc21"
SOURCE_FORMAT = "ardupilot-mode-csv"
# The reason enum was read at the Plane 4.5.5 tree only, so only that declared vehicle version is
# qualified to have its reason codes interpreted. Every other selector the adopted route accepts
# names a DIFFERENT release whose tree was not read here; those records keep their raw code and are
# not given a meaning. No vehicle breadth is claimed.
QUALIFIED_VEHICLES = frozenset({"plane-4.5.5"})
UNQUALIFIED_PIN = "NONE_THE_DECLARED_VEHICLE_VERSION_IS_NOT_THE_PINNED_PRODUCER"
# ASCII decimal only: this writer prints ASCII digits, while `str.isdigit()` also accepts other
# Unicode digit characters that `int()` either reads as a different number or refuses outright.
UNSIGNED = re.compile(r"[0-9]{1,20}")
# The MODE record is `QMBB` at the pinned producer: a uint64 microsecond time and three uint8
# fields. The time is additionally bounded to what the common reader parses, which is an i64.
FORMAT_BOUNDS = (("TimeUS", 0, 2 ** 63 - 1), ("ModeNum", 0, 0xFF), ("Rsn", 0, 0xFF))
# `libraries/AP_Vehicle/ModeReason.h` at the pin, verbatim entry names and values.
MODE_REASONS = {
    0: "UNKNOWN", 1: "RC_COMMAND", 2: "GCS_COMMAND", 3: "RADIO_FAILSAFE", 4: "BATTERY_FAILSAFE",
    5: "GCS_FAILSAFE", 6: "EKF_FAILSAFE", 7: "GPS_GLITCH", 8: "MISSION_END",
    9: "THROTTLE_LAND_ESCAPE", 10: "FENCE_BREACHED", 11: "TERRAIN_FAILSAFE", 12: "BRAKE_TIMEOUT",
    13: "FLIP_COMPLETE", 14: "AVOIDANCE", 15: "AVOIDANCE_RECOVERY", 16: "THROW_COMPLETE",
    17: "TERMINATE", 18: "TOY_MODE", 19: "CRASH_FAILSAFE",
    20: "SOARING_FBW_B_WITH_MOTOR_RUNNING", 21: "SOARING_THERMAL_DETECTED",
    22: "SOARING_THERMAL_ESTIMATE_DETERIORATED", 23: "VTOL_FAILED_TRANSITION",
    24: "VTOL_FAILED_TAKEOFF", 25: "FAILSAFE", 26: "INITIALISED", 27: "SURFACE_COMPLETE",
    28: "BAD_DEPTH", 29: "LEAK_FAILSAFE", 30: "SERVOTEST", 31: "STARTUP", 32: "SCRIPTING",
    33: "UNAVAILABLE", 34: "AUTOROTATION_START", 35: "AUTOROTATION_BAILOUT",
    36: "SOARING_ALT_TOO_HIGH", 37: "SOARING_ALT_TOO_LOW", 38: "SOARING_DRIFT_EXCEEDED",
    39: "RTL_COMPLETE_SWITCHING_TO_VTOL_LAND_RTL",
    40: "RTL_COMPLETE_SWITCHING_TO_FIXEDWING_AUTOLAND", 41: "MISSION_CMD", 42: "FRSKY_COMMAND",
    43: "FENCE_RETURN_PREVIOUS_MODE", 44: "QRTL_INSTEAD_OF_RTL", 45: "AUTO_RTL_EXIT",
    46: "LOITER_ALT_REACHED_QLAND", 47: "LOITER_ALT_IN_VTOL", 48: "RADIO_FAILSAFE_RECOVERY",
    49: "QLAND_INSTEAD_OF_RTL", 50: "DEADRECKON_FAILSAFE", 51: "MODE_TAKEOFF_FAILSAFE",
    52: "DDS_COMMAND",
}
# The reason was a command from somewhere. A command is not evidence that anything went wrong.
COMMANDED = {1, 2, 30, 32, 41, 42, 52}
# The source's own unknowns.
SOURCE_UNKNOWN = {0, 33}
# Returning or recovering, never entering.
RECOVERY = {15: "AVOIDANCE_RECOVERY", 43: "FENCE_RETURN_PREVIOUS_MODE", 48: "RADIO_FAILSAFE_RECOVERY"}
# The failsafe entries, with the kind each one names. `FAILSAFE` names none.
FAILSAFE_KINDS = {3: "RADIO", 4: "BATTERY", 5: "GCS", 6: "EKF", 11: "TERRAIN", 19: "CRASH",
                  29: "LEAK", 50: "DEADRECKON", 51: "MODE_TAKEOFF", 25: "UNSPECIFIED_GENERAL"}
FENCE_BREACH = 10
CLASS_SAFETY = "SAFETY_EVENT_REPORTED"
CLASS_COMMANDED = "COMMANDED_MODE_CHANGE"
CLASS_RECOVERY = "RECOVERY_OR_RETURN_REPORTED"
CLASS_UNKNOWN = "SOURCE_DECLARED_UNKNOWN"
CLASS_OTHER = "OTHER_REPORTED_REASON"
CLASS_UNMAPPED = "UNMAPPED_CODE_NO_MEANING_ASSIGNED"
CLASS_UNQUALIFIED = "UNQUALIFIED_VEHICLE_VERSION_NOT_INTERPRETED"
UNQUALIFIED_BASIS = (
    "THE_REASON_ENUM_WAS_READ_AT_THE_PINNED_PLANE_4_5_5_TREE_ONLY_THIS_DECLARED_VEHICLE_VERSION_IS_A_"
    "DIFFERENT_RELEASE_WHOSE_TREE_WAS_NOT_READ_SO_THE_RAW_CODE_IS_PRESERVED_AND_NO_MEANING_IS_"
    "ASSIGNED_TO_IT_HERE")
REPORT_BASIS = (
    "THE_AUTOPILOTS_OWN_REPORT_OF_WHY_A_MODE_CHANGED_READ_FROM_A_SAVED_RECORD_IT_IS_NOT_A_GEOMETRY_"
    "EVALUATION_NOT_A_CONTROL_ACTION_AND_NOT_A_PHYSICAL_CAUSE_A_REPORTED_FAILSAFE_MEANS_THE_"
    "AUTOPILOT_APPLIED_THAT_FAILSAFE_NOT_THAT_THE_UNDERLYING_THING_FAILED")
FENCE_BASIS = (
    "THE_AUTOPILOT_REPORTED_A_FENCE_BREACH_AS_THE_REASON_FOR_THIS_MODE_CHANGE_NO_BOUNDARY_NO_FENCE_"
    "TYPE_AND_NO_POSITION_IS_IN_THIS_RECORD_SO_NONE_IS_STATED_AND_NOTHING_IS_COMPARED_WITH_A_LIMIT_"
    "HERE")
COMMAND_BASIS = (
    "A_COMMANDED_MODE_CHANGE_IS_NOT_A_SAFETY_EVENT_AND_CARRIES_NO_SAFETY_DOMAIN_SO_A_COMMAND_IS_"
    "NEVER_READ_AS_AN_OCCURRENCE")
RECOVERY_BASIS = (
    "THE_SOURCE_REPORTS_RETURNING_OR_RECOVERING_RATHER_THAN_ENTERING_SO_THIS_IS_NEVER_COUNTED_AS_A_"
    "BREACH_OR_AS_A_FAILSAFE_ENTRY")
GENERAL_BASIS = (
    "THE_PINNED_SOURCE_COMMENTS_THAT_THIS_ENTRY_IS_FOR_GENERAL_FAILSAFES_AND_THAT_SPECIFIC_ONES_ARE_"
    "PREFERRED_SO_NO_PARTICULAR_FAILSAFE_KIND_IS_ASSIGNED_TO_IT")
COVERAGE_BASIS = (
    "A_MODE_RECORD_EXISTS_ONLY_WHEN_THE_MODE_CHANGED_SO_A_BREACH_OR_FAILSAFE_THAT_CHANGED_NO_MODE_IS_"
    "NOT_IN_THIS_RECORD_AND_ITS_ABSENCE_HERE_IS_NOT_EVIDENCE_THAT_NONE_OCCURRED")
NO_QUANTITY = (
    "A_MODE_RECORD_CARRIES_NO_THRESHOLD_NO_DISTANCE_AND_NO_MARGIN_SO_NO_QUANTITY_IS_EMITTED_AND_"
    "NOTHING_IS_COMPARED_WITH_A_CONFIGURED_LIMIT")
COLUMNS = ["record_time_us", "record_time_basis", "declared_vehicle", "reported_mode_number",
           "reported_mode_reason_code", "reported_mode_reason", "reason_class", "safety_domain",
           "reported_failsafe_kind", "reported_geofence_report", "report_basis", "coverage_basis",
           "quantity_basis", "original_record_hex", "original_record_columns",
           "original_record_sha256", "producer_pin", "source_format"]


def classify(code):
    """(class, domain, failsafe kind, fence report, extra basis) for one reported reason code."""
    if code not in MODE_REASONS:
        # No fall-through: an unmapped number never becomes a safety event.
        return CLASS_UNMAPPED, "", "", "", ""
    if code in SOURCE_UNKNOWN:
        return CLASS_UNKNOWN, "", "", "", ""
    if code in COMMANDED:
        return CLASS_COMMANDED, "", "", "", COMMAND_BASIS
    if code in RECOVERY:
        domain = "GEOFENCE" if code == 43 else "FAILSAFE" if code == 48 else ""
        fence = "RETURN_TO_PREVIOUS_MODE_REPORTED" if code == 43 else ""
        return CLASS_RECOVERY, domain, "", fence, RECOVERY_BASIS
    if code == FENCE_BREACH:
        return CLASS_SAFETY, "GEOFENCE", "", "BREACH_REPORTED", FENCE_BASIS
    if code in FAILSAFE_KINDS:
        kind = FAILSAFE_KINDS[code]
        return (CLASS_SAFETY, "FAILSAFE", kind, "",
                GENERAL_BASIS if kind == "UNSPECIFIED_GENERAL" else "")
    return CLASS_OTHER, "", "", "", ""


def _split(text):
    """The saved export as (raw record text, cells) pairs, header first.

    The adopted mode route's pattern is used rather than a new parser: `csv.reader` over the saved
    text, with the header checked explicitly. Each record is parsed on its own line so that the
    **exact characters of that record** can be retained; a cell carrying an embedded newline would
    leave its quote unterminated on one line and is refused by `csv` rather than silently rejoined.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines):
        # The record separator is excluded from retention, so a CRLF file retains the same bytes a
        # LF file does; nothing inside the record is altered.
        record = line[:-1] if line.endswith("\r") else line
        try:
            # `strict=True`: the default reader silently repairs broken quoting - it reads an
            # unterminated `"cell` as `cell` and `"a"x` as `ax`, so the cells it returns would not
            # be the characters the writer wrote. A record this writer cannot have produced is
            # refused instead of quietly rewritten.
            cells = next(csv.reader([record], strict=True))
        except csv.Error as broken:
            raise ValueError(f"record {number}: broken quoting this writer does not produce "
                             f"({broken}); the record is refused rather than repaired") from broken
        yield record, cells


def convert(text, *, vehicle, source_format=SOURCE_FORMAT):
    if source_format != SOURCE_FORMAT:
        raise ValueError("explicit supported saved export format required")
    if vehicle not in MODE_SCHEMAS:
        # The vehicle is the caller's declaration, exactly as the adopted mode route requires it.
        raise ValueError("explicit supported declared vehicle required")
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized export")
    if any(ord(character) < 32 and character not in "\r\n" for character in text):
        raise ValueError("the saved export carries a control character this writer does not emit")
    records = _split(text)
    try:
        header_text, header = next(records)
    except StopIteration:
        raise ValueError("the saved export carries no header") from None
    if len(set(header)) != len(header):
        # A repeated column name makes every later cell of that name overwrite an earlier one. The
        # record cannot be read without losing data, so it is refused rather than silently resolved.
        raise ValueError("the saved export repeats a column name; which cell is meant is ambiguous")
    if any(not name or name != name.strip() for name in header):
        raise ValueError("the saved export carries an empty or padded column name")
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        raise ValueError(f"the saved export is missing its required columns {missing}")
    qualified = vehicle in QUALIFIED_VEHICLES
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    report = {"producer_pin": PRODUCER_PIN, "declared_vehicle": vehicle, "source_records": 0,
              "geofence_breach_reports": 0, "failsafe_reports": 0, "commanded_changes": 0,
              "recovery_reports": 0, "source_declared_unknown": 0, "unmapped_codes": 0,
              "other_reported_reasons": 0, "unqualified_vehicle_records": 0,
              "reason_interpretation": (PRODUCER_PIN if qualified else UNQUALIFIED_PIN),
              "coverage_basis": COVERAGE_BASIS,
              "source_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest()}
    for number, (record_text, cells) in enumerate(records, 1):
        if len(record_text.encode()) > MAX_LINE:
            raise ValueError(f"record {number}: exceeds the line bound")
        report["source_records"] += 1
        if report["source_records"] > MAX_ROWS:
            raise ValueError("saved export exceeds the record bound")
        if len(cells) != len(header):
            # Too few cells would silently read a later column's value, and too many would drop the
            # surplus. Either way a cell would be lost or misread, so the record is refused.
            raise ValueError(f"record {number}: has {len(cells)} cells for {len(header)} columns; "
                             f"a ragged record cannot be read without losing or misreading one")
        values = {}
        for name, low, high in FORMAT_BOUNDS:
            token = cells[header.index(name)]
            # ASCII digits only. `str.isdigit()` is true for other Unicode digit characters, and
            # `int()` either accepts them as a different number or refuses them, so neither is what
            # this writer prints.
            if not UNSIGNED.fullmatch(token):
                raise ValueError(f"record {number}: {name} is not the producer's unsigned decimal")
            values[name] = int(token)
            if not low <= values[name] <= high:
                raise ValueError(f"record {number}: {name} is outside its declared domain "
                                 f"[{low}, {high}]")
        code = values["Rsn"]
        if qualified:
            reason_class, domain, kind, fence, extra = classify(code)
            reason = MODE_REASONS.get(code, f"UNKNOWN_CODE_{code}")
        else:
            # The reason enum was read at one producer version only. For any other declared vehicle
            # version the code is preserved exactly and no meaning is assigned to it.
            reason_class, domain, kind, fence, extra = CLASS_UNQUALIFIED, "", "", "", UNQUALIFIED_BASIS
            reason = "NOT_INTERPRETED_AT_THIS_DECLARED_VEHICLE_VERSION"
        raw = record_text.encode()
        writer.writerow({
            "record_time_us": values["TimeUS"],
            "record_time_basis": "US_SINCE_BOOT_FROM_THE_RECORDS_OWN_TIMEUS_NOT_UTC",
            "declared_vehicle": vehicle, "reported_mode_number": values["ModeNum"],
            "reported_mode_reason_code": code,
            "reported_mode_reason": reason,
            "reason_class": reason_class, "safety_domain": domain,
            "reported_failsafe_kind": kind, "reported_geofence_report": fence,
            "report_basis": " ".join(part for part in (REPORT_BASIS, extra) if part),
            "coverage_basis": COVERAGE_BASIS, "quantity_basis": NO_QUANTITY,
            "original_record_hex": "hex:" + raw.hex(),
            "original_record_columns": len(cells),
            "original_record_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "producer_pin": PRODUCER_PIN if qualified else UNQUALIFIED_PIN,
            "source_format": source_format})
        if reason_class == CLASS_SAFETY:
            report["geofence_breach_reports" if domain == "GEOFENCE" else "failsafe_reports"] += 1
        elif reason_class == CLASS_COMMANDED:
            report["commanded_changes"] += 1
        elif reason_class == CLASS_RECOVERY:
            report["recovery_reports"] += 1
        elif reason_class == CLASS_UNKNOWN:
            report["source_declared_unknown"] += 1
        elif reason_class == CLASS_UNMAPPED:
            report["unmapped_codes"] += 1
        elif reason_class == CLASS_UNQUALIFIED:
            report["unqualified_vehicle_records"] += 1
        else:
            report["other_reported_reasons"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted export exceeds bound")
    if not report["source_records"]:
        raise ValueError("the saved export carries no MODE record")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved ArduPilot MODE CSV export")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--vehicle", required=True, choices=tuple(MODE_SCHEMAS))
    parser.add_argument("--source-format", required=True, choices=(SOURCE_FORMAT,))
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        saved = args.input.read_bytes()
        output, report = convert(saved.decode("utf-8"), vehicle=args.vehicle,
                                 source_format=args.source_format)
        if report["source_sha256"] != "sha256:" + hashlib.sha256(saved).hexdigest():
            raise ValueError("reported digest does not bind the input bytes")
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "source.csv").write_bytes(saved)
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, UnicodeDecodeError, csv.Error, StopIteration) as failure:
        print(f"safety report conversion failed; no successful conversion claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
