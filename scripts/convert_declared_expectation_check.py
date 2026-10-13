#!/usr/bin/env python3
"""Passive comparison of saved observations against a caller-declared expectation.

The adopted converters already carry the reported side: a named vehicle mode, a mission state, a
goal status. None of them states whether a saved value was *within what the caller expected*, or
whether a declared progress number *advanced between two saved reports*. A raw code is not that
comparison, so this bridge performs it and nothing else: it decodes no device format and reaches no
conclusion about any machine. Range mode additionally binds a saved profile and explicit interval
specification. Outside the declared expectation means only outside that expectation.
"""
import argparse
import csv
from decimal import Decimal, Inexact, localcontext
import hashlib
import io
import json
from pathlib import Path
import re
import sys

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
ROW_LIMIT = 200000
COLUMN_LIMIT = 256
DECLARATION_LIMIT = 64
CITATION_LIMIT = 200
EXPECTED_VALUE_LIMIT = 256
EXPECTED_SET_LIMIT = 64
# The supported numeric range for a difference check, stated rather than assumed: a plain decimal of
# at most 30 integer and 30 fraction digits. Exponent notation is refused outright, so a huge finite
# exponent can never reach the arithmetic. Within that range one subtraction needs at most 61
# significant digits, so the context below carries it exactly -- and traps Inexact, which turns any
# rounding into a refusal instead of a wrong answer.
DIFFERENCE_DIGITS = 30
DIFFERENCE_NUMBER = re.compile(r"-?[0-9]{1,%d}(\.[0-9]{1,%d})?" % (DIFFERENCE_DIGITS, DIFFERENCE_DIGITS))
DIFFERENCE_TOLERANCE = re.compile(r"[0-9]{1,%d}(\.[0-9]{1,%d})?" % (DIFFERENCE_DIGITS, DIFFERENCE_DIGITS))
EXACT_PRECISION = 4 * DIFFERENCE_DIGITS + 2
DIFFERENCE_FORM = (f"a plain decimal with at most {DIFFERENCE_DIGITS} integer and "
                   f"{DIFFERENCE_DIGITS} fraction digits, without an exponent")
# A finite cap on rows produced -- a work bound, not a promise about size. Size is bounded
# separately by OUTPUT_LIMIT against the real serialized bytes of each row, because a saved cell can
# be long and every text value here is hex-expanded.
WORK_ROW_LIMIT = 200000
EXPECTATION_BASIS = (
    "A_COMPARISON_AGAINST_AN_EXPECTATION_THE_CALLER_DECLARED_OVER_VALUES_SAVED_IN_THIS_FILE_ON_A_"
    "CALLER_DECLARED_CLOCK_WHOSE_WORDS_MEAN_ONLY_WHAT_THE_CITED_SOURCE_SAYS_IT_IS_NOT_A_DIAGNOSIS_"
    "NOT_A_CAUSE_NOT_A_TASK_FAILURE_NOT_A_STALL_NOT_A_COMMAND_AND_NOT_EVIDENCE_ABOUT_A_PHYSICAL_"
    "MACHINE_OUTSIDE_THE_DECLARED_EXPECTATION_MEANS_OUTSIDE_THE_LIST_THE_CALLER_DECLARED_AND_THE_"
    "LIST_MAY_SIMPLY_BE_INCOMPLETE_WHILE_UNCHANGED_MEANS_TWO_SAVED_REPORTS_CARRIED_THE_SAME_NUMBER_"
    "AND_A_DIFFERENCE_BEYOND_A_DECLARED_TOLERANCE_IS_A_DIFFERENCE_BETWEEN_TWO_SAVED_NUMBERS_THE_"
    "CALLER_SAID_SHARE_A_UNIT_NOT_A_TRACKING_ERROR_A_FAULT_OR_AN_ACTUATOR_VERDICT")
FIELDS = ["record_time_us", "check_kind", "subject_hex", "subject_columns_hex", "checked_column_hex",
          "value_meaning_hex", "reported_value_hex", "expectation_disposition",
          "declared_expectation_hex", "previous_value_hex", "previous_record_time_us", "elapsed_us",
          "advance_delta", "difference_value", "declared_tolerance", "declared_clock_basis",
          "expectation_basis", "source_sha256"]
RANGE_FIELDS = ["source_row_index", "range_spec_sha256", "range_profile_sha256", "range_unit_hex",
                "range_minimum_text", "range_maximum_text"]
RANGE_SPEC_LIMIT = 8192
RANGE_PROFILE_LIMIT = 65536
RANGE_REPORT_LIMIT = 1024 * 1024
RANGE_BASIS = ("CALLER_DECLARED_INCLUSIVE_RANGE_ON_EXACT_SAVED_DECIMALS_WITH_MATCHING_DECLARED_"
               "SUBJECT_UNIT_AND_CLOCK_NOT_SENSOR_VALIDITY_NOT_PHYSICAL_FAULT_NOT_AUTHENTICATION_"
               "UNQUALIFIED_SUBJECT_VALUES_NOT_INTERPRETED_NO_UNIT_CONVERSION")


def text(value):
    return "hex:" + value.encode("utf-8").hex()


def identity(values):
    """Reversible: a JSON array, so a separator inside one value cannot collide with another tuple."""
    return text(json.dumps(list(values), separators=(",", ":"), ensure_ascii=False))


def declared_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,64}", value):
        raise ValueError("invalid declared column name")
    return value


def guarded(reader):
    """csv refuses a field at its own size limit; that refusal is surfaced, not left to escape."""
    while True:
        try:
            yield next(reader)
        except StopIteration:
            return
        except csv.Error as failure:
            raise ValueError(f"the saved CSV cannot be parsed as written: {failure}")


def read_rows(data, time_column):
    """The saved CSV exactly as written; a duplicate header name is refused, never overwritten."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized saved observations")
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("saved observations carry no header")
    except csv.Error as failure:
        raise ValueError(f"the saved CSV cannot be parsed as written: {failure}")
    if not 1 <= len(header) <= COLUMN_LIMIT:
        raise ValueError("unsupported saved column count")
    if len(set(header)) != len(header):
        raise ValueError("duplicate saved column name")
    if time_column not in header:
        raise ValueError("declared time column is not in the saved header")
    index = {name: position for position, name in enumerate(header)}
    rows, previous = [], None
    for cells in guarded(reader):
        if len(cells) != len(header):
            raise ValueError("saved row width does not match the header")
        if len(rows) >= ROW_LIMIT:
            raise ValueError("saved observations exceed the row bound")
        moment = cells[index[time_column]]
        if not re.fullmatch(r"[0-9]{1,19}", moment):
            raise ValueError("saved time is not a non-negative integer of microseconds")
        moment = int(moment)
        if previous is not None and moment < previous:
            # A saved file is read in the order it was written; this bridge never reorders one.
            raise ValueError("saved time decreases; no reordering is performed")
        previous = moment
        rows.append((moment, cells))
    if not rows:
        raise ValueError("saved observations carry no rows")
    return header, index, rows


def subject_of(cells, index, subjects):
    """The declared identity of one saved row; a missing component is refused, never merged."""
    values = tuple(cells[index[name]] for name in subjects)
    if any(value == "" for value in values):
        raise ValueError("a saved row is missing a declared identity component")
    return values


def progress_number(value):
    if not re.fullmatch(r"-?[0-9]{1,18}", value):
        # A progress column this bridge cannot compare as a number is refused, never string-sorted.
        raise ValueError("a declared progress value is not an integer")
    return int(value)


def membership(rows, index, column, citation, expected, subjects, shared, emit, report):
    """Was this saved value one of the values the caller declared? Exact equality, nothing else."""
    declared = identity(sorted(expected))
    for moment, cells in rows:
        value = cells[index[column]]
        if value == "":
            report["values_not_reported"] += 1
            disposition, reported = "NOT_REPORTED", ""
        else:
            inside = value in expected
            disposition = ("WITHIN_DECLARED_EXPECTATION" if inside
                           else "OUTSIDE_DECLARED_EXPECTATION")
            reported = text(value)
            report["within_declared_expectation"] += inside
            report["outside_declared_expectation"] += not inside
        emit(dict(shared, record_time_us=moment, check_kind="DECLARED_VALUE_MEMBERSHIP",
                  subject_hex=identity(subject_of(cells, index, subjects)) if subjects else "",
                  checked_column_hex=text(column), value_meaning_hex=text(citation),
                  reported_value_hex=reported, expectation_disposition=disposition,
                  declared_expectation_hex=declared))


def advance(rows, index, column, citation, subjects, shared, emit, report):
    """Did a declared progress number advance between two saved reports? A number, not a verdict."""
    last = {}
    for moment, cells in rows:
        subject = subject_of(cells, index, subjects)
        value = cells[index[column]]
        if value == "":
            # Blank is not a progress value: it breaks the chain rather than being bridged across.
            report["values_not_reported"] += 1
            last[subject] = None
            disposition, reported, delta = "NOT_REPORTED", "", ""
            previous_value, previous_time = "", ""
        else:
            number = progress_number(value)
            seen = last.get(subject, "UNSEEN")
            reported = text(value)
            if seen is None or seen == "UNSEEN":
                # A value after a blank is not the first one, and saying so would be wrong.
                disposition = ("FIRST_REPORTED_VALUE" if seen == "UNSEEN"
                               else "PREVIOUS_VALUE_NOT_REPORTED")
                delta, previous_value, previous_time = "", "", ""
                report["first_reported_values" if seen == "UNSEEN"
                       else "values_after_an_unreported_one"] += 1
            else:
                delta = number - seen[0]
                disposition = ("ADVANCED" if delta > 0 else
                               "UNCHANGED" if delta == 0 else "DECREASED")
                previous_value, previous_time = text(str(seen[0])), seen[1]
                report[disposition.lower() + "_records"] += 1
            last[subject] = (number, moment)
        emit(dict(shared, record_time_us=moment, check_kind="DECLARED_PROGRESS_ADVANCE",
                  subject_hex=identity(subject) if subjects else "",
                  checked_column_hex=text(column), value_meaning_hex=text(citation),
                  reported_value_hex=reported, expectation_disposition=disposition,
                  previous_value_hex=previous_value, previous_record_time_us=previous_time,
                  elapsed_us="" if previous_time == "" else moment - previous_time,
                  advance_delta=delta))


def bounded_number(value, what, pattern=DIFFERENCE_NUMBER):
    """Inside the stated range the arithmetic is exact; outside it, this refuses rather than rounds."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{what} must be {DIFFERENCE_FORM}")
    return Decimal(value)  # exact from the literal; the regex has already excluded inf, nan and E


def range_declaration(data, raw, profile_raw, clock_basis, subjects, time_column):
    """Bind one explicit interval to bytes and declared context, never to hardware truth."""
    try:
        import tomllib
    except ImportError:
        raise ValueError("range mode requires Python 3.11+ for the standard TOML profile parser") from None
    if not isinstance(raw, bytes) or not 0 < len(raw) <= RANGE_SPEC_LIMIT:
        raise ValueError("range specification byte bound")
    if not isinstance(profile_raw, bytes) or not 0 < len(profile_raw) <= RANGE_PROFILE_LIMIT:
        raise ValueError("range source profile byte bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate range specification key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("nonfinite range specification constant")

    spec = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    keys = {"schema", "source_sha256", "profile_sha256", "column", "minimum", "maximum",
            "unit", "clock_basis", "subject_columns", "subject_values"}
    if not isinstance(spec, dict) or set(spec) != keys or spec["schema"] != "musubi.declared-range.v1":
        raise ValueError("unsupported range specification shape")
    for key, content in (("source_sha256", data), ("profile_sha256", profile_raw)):
        if spec[key] != "sha256:" + hashlib.sha256(content).hexdigest():
            raise ValueError("range specification byte binding mismatch")
    minimum = bounded_number(spec["minimum"], "minimum")
    maximum = bounded_number(spec["maximum"], "maximum")
    if minimum > maximum:
        raise ValueError("range minimum exceeds maximum")
    column = declared_name(spec["column"])
    unit = spec["unit"]
    profile = tomllib.loads(profile_raw.decode("utf-8"))
    if not isinstance(unit, str) or not 1 <= len(unit) <= DECLARATION_LIMIT:
        raise ValueError("range unit must be a bounded literal")
    if (profile.get("format") != "telemetry_csv_us" or
            not isinstance(profile.get("units"), dict) or profile["units"].get(column) != unit):
        raise ValueError("range unit differs from the source profile; no conversion is inferred")
    if (clock_basis not in ("unknown", "boot_relative") or spec["clock_basis"] != clock_basis or
            profile.get("default_clock_basis") != clock_basis or
            not isinstance(profile.get("fields"), dict) or profile["fields"].get("time") != time_column):
        raise ValueError("range source clock or time column differs from the declaration")
    if any(not isinstance(profile.get(key), str) or not profile[key] for key in ("profile_id", "version")):
        raise ValueError("range source profile needs a declared configuration id and version")
    wanted = spec["subject_values"]
    if (spec["subject_columns"] != list(subjects) or not 1 <= len(subjects) <= 8 or
            not isinstance(wanted, list) or len(wanted) != len(subjects) or
            any(not isinstance(v, str) or not 1 <= len(v) <= EXPECTED_VALUE_LIMIT for v in wanted)):
        raise ValueError("range needs one explicit nonempty subject tuple matching declared columns")
    return spec, profile


def declared_range(rows, index, spec, citation, shared, emit, report):
    minimum, maximum = Decimal(spec["minimum"]), Decimal(spec["maximum"])
    column, subjects = spec["column"], spec["subject_columns"]
    for ordinal, (moment, cells) in enumerate(rows):
        subject = [cells[index[name]] for name in subjects]
        saved = cells[index[column]]
        if subject != spec["subject_values"]:
            category, disposition = "unqualified_subject", "UNQUALIFIED_SUBJECT"
        elif saved == "":
            category, disposition = "not_reported", "NOT_REPORTED"
            report["values_not_reported"] += 1
        else:
            value = bounded_number(saved, "the selected range value")
            category = "below" if value < minimum else "above" if value > maximum else "within"
            disposition = {"below": "BELOW_DECLARED_RANGE", "above": "ABOVE_DECLARED_RANGE",
                           "within": "WITHIN_DECLARED_RANGE"}[category]
        report["range_counts"][category] += 1
        emit(dict(shared, record_time_us=moment, check_kind="DECLARED_NUMERIC_RANGE",
            subject_hex=identity(subject), checked_column_hex=text(column), value_meaning_hex=text(citation),
            reported_value_hex=text(saved) if saved else "", expectation_disposition=disposition,
            declared_expectation_hex=identity((spec["minimum"], spec["maximum"], spec["unit"])),
            source_row_index=ordinal, range_unit_hex=text(spec["unit"]),
            range_minimum_text="decimal:" + spec["minimum"], range_maximum_text="decimal:" + spec["maximum"]))


def difference(rows, index, columns, citation, tolerance, subjects, shared, emit, report):
    """Two saved numbers the caller declared share a unit, and how far apart they are."""
    first, second = columns
    for moment, cells in rows:
        left, right = cells[index[first]], cells[index[second]]
        if left == "" or right == "":
            # One side missing is not agreement and not disagreement.
            report["values_not_reported"] += 1
            disposition, value = "NOT_REPORTED", ""
        else:
            first_value = bounded_number(left, "a compared value")
            second_value = bounded_number(right, "a compared value")
            with localcontext() as context:
                # Enough digits for this subtraction, and a trap so a wrong answer is impossible:
                # if it could not be represented exactly, the run refuses instead of reporting.
                context.prec = EXACT_PRECISION
                context.traps[Inexact] = True
                try:
                    value = first_value - second_value
                    inside = abs(value) <= tolerance
                except Inexact:
                    raise ValueError("this difference cannot be represented exactly within the "
                                     f"supported range of {DIFFERENCE_FORM}")
            disposition = ("WITHIN_DECLARED_TOLERANCE" if inside else "BEYOND_DECLARED_TOLERANCE")
            report["within_declared_tolerance"] += inside
            report["beyond_declared_tolerance"] += not inside
        emit(dict(shared, record_time_us=moment, check_kind="DECLARED_PAIR_DIFFERENCE",
                  subject_hex=identity(subject_of(cells, index, subjects)) if subjects else "",
                  checked_column_hex=identity(columns), value_meaning_hex=text(citation),
                  reported_value_hex="" if disposition == "NOT_REPORTED" else identity((left, right)),
                  expectation_disposition=disposition, difference_value=value,
                  declared_tolerance=tolerance))


def convert(data, *, clock_basis, value_meaning, membership_column=None, expected_values=(),
            advance_column=None, difference_columns=(), tolerance=None, subjects=(),
            time_column="record_time_us", range_spec=None, range_profile=None):
    if not isinstance(clock_basis, str) or not 1 <= len(clock_basis) <= DECLARATION_LIMIT:
        raise ValueError("the saved clock basis must be declared by the caller")
    if not isinstance(value_meaning, str) or not 1 <= len(value_meaning) <= CITATION_LIMIT:
        # A checked column without a stated meaning source is a name, and a name is not a meaning.
        raise ValueError("the checked column requires a declared meaning source")
    chosen = [name for name, given in (("membership", membership_column is not None),
                                       ("advance", advance_column is not None),
                                       ("difference", bool(difference_columns)),
                                       ("range", range_spec is not None or range_profile is not None)) if given]
    if len(chosen) != 1:
        raise ValueError("declare exactly one check: membership, advance, difference or range")
    spec = None
    if chosen[0] == "range":
        spec, source_profile = range_declaration(data, range_spec, range_profile, clock_basis, subjects, time_column)
    if chosen[0] == "difference":
        if len(difference_columns) != 2 or len(set(difference_columns)) != 2:
            raise ValueError("a difference check compares exactly two distinct declared columns")
        columns = tuple(declared_name(name) for name in difference_columns)
        if tolerance is None:
            # The tolerance is the caller's, and a missing one is not zero.
            raise ValueError("a difference check needs a declared non-negative decimal tolerance")
        tolerance = bounded_number(tolerance, "the declared tolerance", DIFFERENCE_TOLERANCE)
        column = columns[0]
    else:
        if tolerance is not None:
            raise ValueError("a tolerance applies only to a difference check")
        columns = ()
        column = spec["column"] if spec is not None else declared_name(membership_column if advance_column is None else advance_column)
    expected = set(expected_values)
    if membership_column is not None:
        if not 1 <= len(expected) <= EXPECTED_SET_LIMIT or len(expected) != len(expected_values):
            raise ValueError("a membership check needs a non-empty set of distinct expected values")
        if any(not isinstance(item, str) or not 1 <= len(item) <= EXPECTED_VALUE_LIMIT
               for item in expected):
            raise ValueError("each declared expected value is a non-empty bounded string")
    elif expected_values:
        raise ValueError("expected values apply only to a membership check")
    subjects = tuple(declared_name(name) for name in subjects)
    if len(set(subjects)) != len(subjects):
        raise ValueError("duplicate subject column")
    header, index, rows = read_rows(data, declared_name(time_column))
    for name in subjects + (columns or (column,)):
        if name not in header:
            raise ValueError("declared column is not in the saved header")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = {"saved_rows": len(rows), "saved_columns": len(header), "checked_records": 0,
              "within_declared_expectation": 0, "outside_declared_expectation": 0,
              "first_reported_values": 0, "values_after_an_unreported_one": 0,
              "within_declared_tolerance": 0, "beyond_declared_tolerance": 0,
              "advanced_records": 0, "unchanged_records": 0,
              "decreased_records": 0, "values_not_reported": 0,
              "declared_checked_column": list(columns) if columns else column, "declared_value_meaning": value_meaning,
              "declared_expected_values": sorted(expected),
              "declared_tolerance": None if tolerance is None else str(tolerance), "declared_subject_columns": list(subjects),
              "declared_clock_basis": clock_basis, "source_sha256": digest, "output_bytes": 0,
              "clock": "Unknown", "physical_cause_records": "NOT_DERIVED"}
    output_fields = FIELDS
    if spec is not None:
        output_fields = FIELDS + RANGE_FIELDS
        report.update(range_specification=spec, range_spec_sha256="sha256:" + hashlib.sha256(range_spec).hexdigest(),
            range_profile_sha256=spec["profile_sha256"], range_profile_id=source_profile["profile_id"],
            range_profile_version=source_profile["version"],
            range_counts=dict(within=0, below=0, above=0, not_reported=0, unqualified_subject=0),
            required_original_artifacts=["source.csv", "range-spec.json", "source-profile.toml"])
    produced, budget = [], io.StringIO(newline="")
    scratch = csv.DictWriter(budget, output_fields, lineterminator="\n", restval="")
    scratch.writeheader()
    written = len(budget.getvalue().encode("utf-8"))

    def emit(row):
        # Each row's real serialized bytes are counted as it is produced, so a long saved cell
        # cannot accumulate past the output bound before anything checks.
        nonlocal written
        if len(produced) >= WORK_ROW_LIMIT:
            raise ValueError("this selection would produce more rows than the finite work cap "
                             "allows; that cap bounds work, not size")
        budget.seek(0)
        budget.truncate()
        scratch.writerow(row)
        line = budget.getvalue()
        written += len(line.encode("utf-8"))
        if written > OUTPUT_LIMIT:
            raise ValueError("the converted rows exceed the output byte bound; refused whole "
                             "rather than truncated")
        produced.append(line)
        report["checked_records"] += 1

    shared = {"subject_columns_hex": identity(subjects) if subjects else "",
              "declared_clock_basis": clock_basis, "expectation_basis": EXPECTATION_BASIS,
              "source_sha256": digest}
    if spec is not None:
        shared.update(expectation_basis=RANGE_BASIS, range_spec_sha256=report["range_spec_sha256"],
                      range_profile_sha256=spec["profile_sha256"])
        declared_range(rows, index, spec, value_meaning, shared, emit, report)
    elif chosen[0] == "membership":
        membership(rows, index, column, value_meaning, expected, subjects, shared, emit, report)
    elif chosen[0] == "advance":
        advance(rows, index, column, value_meaning, subjects, shared, emit, report)
    else:
        difference(rows, index, columns, value_meaning, tolerance, subjects, shared, emit, report)
    if not report["checked_records"]:
        raise ValueError("no saved record reached the check")
    report["output_bytes"] = written
    if spec is not None and len(json.dumps(report, indent=2).encode("utf-8")) > RANGE_REPORT_LIMIT:
        raise ValueError("range report byte bound")
    return ",".join(output_fields) + "\n" + "".join(produced), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="CSV an adopted converter already wrote")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--clock-basis", required=True,
                        help="the saved clock as the source profile declares it; retained verbatim")
    parser.add_argument("--value-meaning", required=True,
                        help="where the checked column's values are defined; a name is not a meaning")
    parser.add_argument("--time-column", default="record_time_us")
    parser.add_argument("--subject-column", action="append", default=[])
    parser.add_argument("--membership-column", help="check each saved value against --expected-value")
    parser.add_argument("--expected-value", action="append", default=[],
                        help="one value the caller expects; repeatable; membership only")
    parser.add_argument("--advance-column",
                        help="check whether a declared integer progress value advanced")
    parser.add_argument("--difference-column", action="append", default=[],
                        help="exactly two columns the caller declares share a unit; with --tolerance")
    parser.add_argument("--tolerance",
                        help="declared non-negative decimal tolerance; difference check only")
    parser.add_argument("--range-spec", type=Path, help="closed inclusive interval specification; with --range-profile")
    parser.add_argument("--range-profile", type=Path, help="source CSV profile whose units/clock the range specification binds")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        source = args.input.read_bytes()
        supplied = []
        for path, limit in ((args.range_spec, RANGE_SPEC_LIMIT), (args.range_profile, RANGE_PROFILE_LIMIT)):
            if path is not None and path.stat().st_size > limit:
                raise ValueError("range declaration input bound")
            supplied.append(None if path is None else path.read_bytes())
        output, report = convert(
            source, clock_basis=args.clock_basis, value_meaning=args.value_meaning,
            time_column=args.time_column, subjects=tuple(args.subject_column),
            membership_column=args.membership_column, expected_values=tuple(args.expected_value),
            advance_column=args.advance_column,
            difference_columns=tuple(args.difference_column), tolerance=args.tolerance,
            range_spec=supplied[0], range_profile=supplied[1])
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
        if args.range_spec is not None:
            for name, content in zip(("source.csv", "range-spec.json", "source-profile.toml"), (source, *supplied)):
                (args.output_directory / name).write_bytes(content)
    except (ValueError, OSError, csv.Error) as failure:
        print(f"declared expectation check failed; no successful conversion claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
