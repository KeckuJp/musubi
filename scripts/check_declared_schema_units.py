#!/usr/bin/env python3
"""Compare a declared expectation with a saved artefact's own declared metadata and structure.

Passive comparison of two documents an engineer already has. No reader is run, no artefact is
converted, no device is touched, and nothing here decides which side is correct.

What it reports is **a specific disagreement between two declarations**, with both sides, each side's
source, and the digests that bind them. What it never does:

* **no unit is inferred from a column name or from a plausible value.** Both sides are declared
  strings, read from a declaration. A column called `speed_m_s` whose profile declares no unit gets
  `UNKNOWN`, not a guess;
* **malformed input is a refusal, not a mismatch.** A document that is not a saved read report is
  refused by name, so an unreadable file never appears as a disagreement;
* **a mismatch is not a cause.** It is not an integration fault, not a physical finding, and not a
  statement that either side is wrong.

The two sides:

1. the caller's **expectation** - JSON: optional `profile_id` / `declared_version`, and `columns`
   mapping a column name to the declared unit string expected for it, or `null` when the caller
   declares the column but no unit;
2. a **saved read report from the adopted common reader** - the artefact's own metadata and
   structure. Exactly four of its fields are read: `source_columns` (the artefact's actual columns),
   `profile_units` (the profile's actual declared unit per column), `profile_identity`
   (`profile_id`, `declared_version`, `source_sha256`) and `input_sha256` (the artefact's digest).

Unit interpretation is added **only** when both declared strings are symbols in
`scripts/recorded_units.py` `UNITS` - the repository's finite, source-qualified table, imported
read-only and never extended. Then the difference is named from that table alone: a **scale/offset**
difference, a **quantity-kind** difference, or - when the table gives both spellings the *same*
quantity, scale and offset, as it does for `K` and `degK` - **equivalent under a different declared
spelling**, which is not a mismatch and is never reported as a scale difference. A symbol outside
that finite table is never interpreted: the two declared strings are compared as text and reported
as written.

Two declarations that cannot be read unambiguously are refused rather than resolved: a **repeated
JSON key** on either side (`json.loads` would silently keep the last), and an explicit **null
identity** in the expectation (the identity keys are optional, so a caller who does not want the
check omits the key; a null declares nothing to compare against).
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.recorded_units import UNITS

LIMIT = 4 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
MAX_SUBJECTS = 4096
NAME_LIMIT = 256
VALUE_LIMIT = 1024
# The saved read report must carry these, or it is not one and is refused rather than half-read.
REQUIRED_REPORT_FIELDS = ("source_columns", "profile_units", "profile_identity", "input_sha256")
IDENTITY_KEYS = ("profile_id", "declared_version")
EXPECTATION_KEYS = frozenset(IDENTITY_KEYS) | {"columns"}
KIND_IDENTITY = "PROFILE_IDENTITY"
KIND_SCHEMA = "SCHEMA_COLUMN"
KIND_UNIT = "COLUMN_UNIT"
MATCH = "MATCHES_DECLARED_EXPECTATION"
ABSENT = "SCHEMA_COLUMN_ABSENT_FROM_ARTEFACT"
UNEXPECTED = "SCHEMA_COLUMN_NOT_EXPECTED"
UNIT_TEXT = "UNIT_DECLARATION_DIFFERENT"
UNIT_SCALE = "UNIT_SCALE_DIFFERENT"
UNIT_KIND = "UNIT_QUANTITY_KIND_DIFFERENT"
# Two declared spellings the finite table gives the SAME quantity, scale and offset - `K` and `degK`
# are the table's own example. The spellings differ and both are preserved, but there is no scale or
# quantity disagreement to claim, so this is not counted as a unit mismatch.
UNIT_EQUIVALENT = "UNIT_EQUIVALENT_UNDER_A_DIFFERENT_DECLARED_SPELLING"
UNKNOWN_ARTEFACT = "UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT"
UNKNOWN_EXPECTED = "UNKNOWN_NO_DECLARED_UNIT_IN_EXPECTATION"
IDENTITY_DIFFERENT = "IDENTITY_DIFFERENT"
EXPECTED_SOURCE = "THE_CALLERS_DECLARED_EXPECTATION_DOCUMENT"
ACTUAL_COLUMN_SOURCE = "THE_SAVED_READ_REPORTS_SOURCE_COLUMNS_THE_ARTEFACTS_ACTUAL_COLUMNS"
ACTUAL_UNIT_SOURCE = "THE_SAVED_READ_REPORTS_PROFILE_UNITS_THE_PROFILES_ACTUAL_DECLARED_UNIT"
ACTUAL_IDENTITY_SOURCE = "THE_SAVED_READ_REPORTS_PROFILE_IDENTITY"
COMPARISON_BASIS = (
    "A_DISAGREEMENT_BETWEEN_TWO_DECLARATIONS_READ_AS_WRITTEN_IT_IS_NOT_A_STATEMENT_THAT_EITHER_SIDE_"
    "IS_WRONG_NOT_AN_INTEGRATION_CAUSE_NOT_A_PHYSICAL_FINDING_AND_NEVER_A_UNIT_INFERRED_FROM_A_"
    "COLUMN_NAME_OR_FROM_A_VALUE")
INTERPRETED_BASIS = (
    "BOTH_DECLARED_SYMBOLS_ARE_IN_THE_REPOSITORYS_FINITE_SOURCE_QUALIFIED_UNIT_TABLE_SO_THE_"
    "DIFFERENCE_IS_NAMED_AS_A_SCALE_OFFSET_OR_QUANTITY_KIND_DIFFERENCE_FROM_THAT_TABLE_ALONE")
EQUIVALENT_BASIS = (
    "BOTH_DECLARED_SYMBOLS_ARE_IN_THE_FINITE_TABLE_AND_IT_GIVES_THEM_THE_SAME_QUANTITY_SCALE_AND_"
    "OFFSET_SO_THE_SPELLINGS_DIFFER_BUT_THERE_IS_NO_SCALE_OR_QUANTITY_DISAGREEMENT_TO_CLAIM")
TEXT_ONLY_BASIS = (
    "AT_LEAST_ONE_DECLARED_STRING_IS_NOT_A_SYMBOL_IN_THAT_FINITE_TABLE_SO_THE_TWO_DECLARATIONS_ARE_"
    "COMPARED_AS_TEXT_AND_NEITHER_IS_INTERPRETED")
UNKNOWN_BASIS = (
    "ONE_SIDE_DECLARES_NO_UNIT_AT_ALL_SO_THERE_IS_NO_BASIS_TO_COMPARE_THIS_IS_MISSING_EVIDENCE_AND_"
    "NOT_A_DISAGREEMENT")
CLOCK_BASIS = "CALLER_DECLARED_CAPTURE_MICROSECONDS_A_COMPARISON_HAS_NO_CLOCK_OF_ITS_OWN"
COLUMNS = ["record_time_us", "record_time_basis", "check_kind", "check_subject_hex",
           "expected_declared_hex", "actual_declared_hex", "disposition", "evidence_state",
           "expected_quantity_kind", "actual_quantity_kind", "expected_scale", "actual_scale",
           "expected_offset", "actual_offset", "unit_interpretation_basis", "comparison_basis",
           "expected_source", "actual_source", "expectation_sha256", "read_report_sha256",
           "artefact_input_sha256", "profile_source_sha256"]


def _unique(pairs):
    """Object hook that refuses a repeated key instead of keeping the last value.

    `json.loads` resolves a duplicate by keeping the last, so a declaration that named a column's
    unit twice would be read as one of them with nothing marking the loss. Which one was meant
    cannot be known, so the document is refused.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"the document declares {key!r} more than once; which declaration is "
                             f"meant cannot be known, so it is refused rather than resolved")
        seen[key] = value
    return seen


def _hex(value):
    return "hex:" + value.encode("utf-8").hex()


def _declared(value, what, subject):
    """One declared string, or None when the declaration says there is none."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > VALUE_LIMIT:
        raise ValueError(f"{what} for {subject!r} is not a declared string within bounds")
    return value


def compare_unit(expected, actual):
    """(disposition, interpretation basis, expected triple, actual triple) for two declared units.

    A triple is (quantity kind, scale, offset) and exists only when that side's declared string is a
    symbol in the finite table. Nothing outside the table is interpreted.
    """
    if expected is None:
        return UNKNOWN_EXPECTED, UNKNOWN_BASIS, None, None
    if actual is None:
        return UNKNOWN_ARTEFACT, UNKNOWN_BASIS, None, None
    known = expected in UNITS and actual in UNITS
    first = UNITS[expected] if expected in UNITS else None
    second = UNITS[actual] if actual in UNITS else None
    if expected == actual:
        return MATCH, INTERPRETED_BASIS if known else TEXT_ONLY_BASIS, first, second
    if not known:
        return UNIT_TEXT, TEXT_ONLY_BASIS, first, second
    if first == second:
        # Same quantity, same scale, same offset: the table gives these spellings one meaning, so
        # claiming a scale difference would be false. The two spellings are still carried as written.
        return UNIT_EQUIVALENT, EQUIVALENT_BASIS, first, second
    if first[0] != second[0]:
        return UNIT_KIND, INTERPRETED_BASIS, first, second
    return UNIT_SCALE, INTERPRETED_BASIS, first, second


def _read_report(document):
    missing = [name for name in REQUIRED_REPORT_FIELDS if name not in document]
    if missing:
        raise ValueError(f"this is not a saved common-reader read report; it lacks {missing}")
    columns, units = document["source_columns"], document["profile_units"]
    identity = document["profile_identity"]
    if not isinstance(columns, list) or not all(isinstance(name, str) for name in columns):
        raise ValueError("the read report's source_columns is not a list of column names")
    # The artefact's own column names are held to the same constraints as the declared ones, so a
    # malformed report is refused rather than compared against.
    if any(not name or len(name) > NAME_LIMIT for name in columns):
        raise ValueError("the read report carries an empty or over-long source column name")
    if not isinstance(units, dict) or not isinstance(identity, dict):
        raise ValueError("the read report's profile_units or profile_identity is not an object")
    if len(columns) != len(set(columns)):
        raise ValueError("the read report repeats a source column; the structure is ambiguous")
    return columns, units, identity


def convert(expectation_text, report_text, *, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2 ** 63:
        raise ValueError("an explicit non-negative capture time in microseconds is required")
    for text, what in ((expectation_text, "expectation"), (report_text, "read report")):
        if not text or len(text.encode()) > LIMIT:
            raise ValueError(f"empty or oversized {what}")
    try:
        expectation = json.loads(expectation_text, object_pairs_hook=_unique)
        document = json.loads(report_text, object_pairs_hook=_unique)
    except json.JSONDecodeError as broken:
        raise ValueError(f"a document is not JSON: {broken}") from broken
    if not isinstance(expectation, dict) or not isinstance(document, dict):
        raise ValueError("both documents must be JSON objects")
    unknown_keys = sorted(set(expectation) - EXPECTATION_KEYS)
    if unknown_keys:
        # A declaration this check cannot answer must not be silently ignored. The read report
        # carries no format field, so a `format` expectation is refused here rather than dropped.
        raise ValueError(f"the expectation declares {unknown_keys}, which this check cannot compare "
                         f"against a saved read report")
    columns, units, identity = _read_report(document)
    expected_columns = expectation.get("columns")
    if not isinstance(expected_columns, dict) or not expected_columns:
        raise ValueError("the expectation must declare a non-empty columns object")
    if len(expected_columns) + len(columns) > MAX_SUBJECTS:
        raise ValueError("declared subjects exceed the bound")
    for name in expected_columns:
        if not isinstance(name, str) or not name or len(name) > NAME_LIMIT:
            raise ValueError("an expected column name is not a usable string")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    digests = {"expectation_sha256": "sha256:" + hashlib.sha256(expectation_text.encode()).hexdigest(),
               "read_report_sha256": "sha256:" + hashlib.sha256(report_text.encode()).hexdigest(),
               "artefact_input_sha256": str(document["input_sha256"]),
               "profile_source_sha256": str(identity.get("source_sha256", ""))}
    report = {"declared_subjects": 0, "matches": 0, "schema_mismatches": 0, "unit_mismatches": 0,
              "identity_mismatches": 0, "missing_evidence": 0, "equivalent_spellings": 0,
              "comparison_basis": COMPARISON_BASIS,
              **digests}

    def emit(kind, subject, expected, actual, disposition, interpretation="",
             expected_triple=None, actual_triple=None, actual_source=ACTUAL_UNIT_SOURCE):
        row = {"record_time_us": capture_time_us, "record_time_basis": CLOCK_BASIS,
               "check_kind": kind, "check_subject_hex": _hex(subject),
               "expected_declared_hex": _hex(expected) if expected is not None else "",
               "actual_declared_hex": _hex(actual) if actual is not None else "",
               "disposition": disposition,
               "evidence_state": ("MISSING_DECLARED_BASIS" if disposition in
                                  (UNKNOWN_ARTEFACT, UNKNOWN_EXPECTED) else "BOTH_SIDES_DECLARED"),
               "unit_interpretation_basis": interpretation,
               "comparison_basis": COMPARISON_BASIS, "expected_source": EXPECTED_SOURCE,
               "actual_source": actual_source, **digests}
        for side, triple in (("expected", expected_triple), ("actual", actual_triple)):
            row[f"{side}_quantity_kind"] = triple[0] if triple else ""
            row[f"{side}_scale"] = repr(triple[1]) if triple else ""
            row[f"{side}_offset"] = repr(triple[2]) if triple else ""
        writer.writerow(row)
        report["declared_subjects"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("the comparison exceeds its output bound")

    for key in IDENTITY_KEYS:
        if key not in expectation:
            continue
        if expectation[key] is None:
            # The contract makes these keys optional: a caller who does not want the check omits
            # the key. An explicit null declares nothing, so it is refused rather than compared
            # against a present value and reported as a disagreement.
            raise ValueError(f"the expectation declares {key!r} as null; omit the key instead, "
                             f"because a null declares no identity to compare")
        expected = _declared(expectation[key], "the declared identity", key)
        actual = identity.get(key)
        actual = _declared(actual, "the reported identity", key) if actual is not None else None
        if actual is None:
            emit(KIND_IDENTITY, key, expected, None, UNKNOWN_ARTEFACT, UNKNOWN_BASIS,
                 actual_source=ACTUAL_IDENTITY_SOURCE)
            report["missing_evidence"] += 1
            continue
        same = expected == actual
        emit(KIND_IDENTITY, key, expected, actual, MATCH if same else IDENTITY_DIFFERENT,
             actual_source=ACTUAL_IDENTITY_SOURCE)
        report["matches" if same else "identity_mismatches"] += 1

    present = set(columns)
    for name in sorted(expected_columns):
        if name not in present:
            emit(KIND_SCHEMA, name, "DECLARED_EXPECTED_COLUMN", None, ABSENT,
                 actual_source=ACTUAL_COLUMN_SOURCE)
            report["schema_mismatches"] += 1
            continue
        emit(KIND_SCHEMA, name, "DECLARED_EXPECTED_COLUMN", "PRESENT_IN_THE_ARTEFACT", MATCH,
             actual_source=ACTUAL_COLUMN_SOURCE)
        report["matches"] += 1
        expected_unit = _declared(expected_columns[name], "the declared unit", name)
        declared = units.get(name)
        actual_unit = _declared(declared, "the reported unit", name) if declared is not None else None
        disposition, interpretation, first, second = compare_unit(expected_unit, actual_unit)
        emit(KIND_UNIT, name, expected_unit, actual_unit, disposition, interpretation, first, second)
        if disposition == MATCH:
            report["matches"] += 1
        elif disposition == UNIT_EQUIVALENT:
            # Not a mismatch: the finite table gives both spellings one meaning.
            report["equivalent_spellings"] += 1
        elif disposition in (UNKNOWN_ARTEFACT, UNKNOWN_EXPECTED):
            report["missing_evidence"] += 1
        else:
            report["unit_mismatches"] += 1

    for name in sorted(present - set(expected_columns)):
        emit(KIND_SCHEMA, name, None, "PRESENT_IN_THE_ARTEFACT", UNEXPECTED,
             actual_source=ACTUAL_COLUMN_SOURCE)
        report["schema_mismatches"] += 1
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("expected", type=Path, help="the caller's declared expectation, JSON")
    parser.add_argument("read_report", type=Path,
                        help="a saved read report from the adopted common reader, JSON")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="the caller's capture clock; a comparison has no clock of its own")
    args = parser.parse_args()
    try:
        for path in (args.expected, args.read_report):
            if path.stat().st_size > LIMIT:
                raise ValueError("input exceeds bound")
        expectation_text = args.expected.read_text(encoding="utf-8")
        report_text = args.read_report.read_text(encoding="utf-8")
        output, report = convert(expectation_text, report_text,
                                 capture_time_us=args.capture_time_us)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, UnicodeDecodeError, csv.Error) as failure:
        print(f"declared schema/unit comparison failed; no comparison claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
