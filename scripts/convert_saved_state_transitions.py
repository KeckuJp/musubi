#!/usr/bin/env python3
"""Passive bridge: saved reported-state transitions and observation windows from a common CSV.

The adopted converters already carry reported quality, link, presence and GNSS-fix *states*. What
none of them carries is a transition between two saved states, or a count of observations over a
declared window against a stated expected interval. Both are readable from a CSV an adopted
converter already wrote, so this decodes nothing and adds no format: it re-reads the common export.
It also judges nothing. A change between two saved words is a change between two saved words.
"""
import argparse
import bisect
import csv
from decimal import Decimal
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
# Two separate bounds, because they bound two different things and neither implies the other.
# WORK_ROW_LIMIT caps how many rows this converter will produce at all: a finite work cap, and
# explicitly NOT a promise about size. The size bound is OUTPUT_LIMIT, enforced against the actual
# serialized bytes of each row as it is produced -- a saved cell can be long and every text value is
# hex-expanded, so no per-row width can be asserted in advance.
WORK_ROW_LIMIT = 200000
TRANSITION_BASIS = (
    "A_CHANGE_BETWEEN_TWO_VALUES_SAVED_IN_THIS_FILE_ON_A_CALLER_DECLARED_CLOCK_WHOSE_WORDS_MEAN_"
    "ONLY_WHAT_THE_CITED_SOURCE_SAYS_THEY_MEAN_IT_IS_NOT_AN_EVENT_THE_DEVICE_REPORTED_NOT_A_TIMEOUT_"
    "NOT_A_RECONNECT_NOT_A_REBOOT_NOT_A_PHYSICAL_CAUSE_AND_THE_ABSENCE_OF_A_TRANSITION_IS_NOT_"
    "EVIDENCE_THAT_THE_STATE_HELD")
RATE_BASIS = (
    "A_COUNT_OF_RECORDS_PRESENT_IN_THIS_FILE_ON_A_CALLER_DECLARED_CLOCK_AGAINST_A_CALLER_DECLARED_"
    "EXPECTED_INTERVAL_IT_IS_NOT_PACKET_LOSS_NOT_LINK_THROUGHPUT_NOT_A_PHYSICAL_SAMPLING_RATE_AND_A_"
    "COUNT_BELOW_THE_EXPECTATION_IS_NOT_EVIDENCE_THAT_ANYTHING_WAS_LOST_IN_TRANSIT_BECAUSE_THE_FILE_"
    "MAY_SIMPLY_NOT_CONTAIN_THOSE_RECORDS_COVERAGE_IS_JUDGED_ONLY_BY_THE_LAST_OBSERVED_RECORD_OR_BY_"
    "A_RECORDING_END_THE_CALLER_DECLARED_AND_IS_NEVER_EXTRAPOLATED_FROM_THE_EXPECTED_INTERVAL")
FIELDS = ["record_time_us", "event_kind", "subject_hex", "subject_columns_hex", "state_column_hex",
          "state_meaning_hex", "previous_state_hex", "reported_state_hex", "transition_disposition",
          "transition_index", "previous_record_time_us", "elapsed_us", "window_index",
          "window_start_us", "window_end_us", "window_observations", "window_expected_observations",
          "window_observed_span_us", "declared_expected_interval_us", "window_rate_ratio",
          "window_disposition", "declared_recording_end_us", "declared_clock_basis",
          "state_transition_basis", "observation_rate_basis", "source_sha256"]


def text(value):
    return "hex:" + value.encode("utf-8").hex()


def identity(values):
    """A reversible encoding: a JSON array, so a separator inside a value cannot collide."""
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


def transitions(rows, index, column, citation, subjects, digest, clock, emit, report):
    """Per subject: the first reported value, then each change. A blank is not a state."""
    last, counts = {}, {}
    for moment, cells in rows:
        subject = subject_of(cells, index, subjects)
        value = cells[index[column]]
        if value == "":
            # Blank is not a value: it is counted, and it breaks the chain rather than bridging it.
            report["blank_state_records"] += 1
            last[subject] = None
            continue
        seen = last.get(subject, "UNSEEN")
        if seen is not None and seen != "UNSEEN" and seen[0] == value:
            # Not a transition -- but the record still reported the value, so the timing advances.
            # Otherwise the next change would name the *earliest* record holding the old value as
            # the previous one, which is a different quantity and would be mislabelled.
            last[subject] = (value, moment)
            report["repeated_state_records"] += 1
            continue
        if seen == "UNSEEN":
            disposition, previous_value, previous_time = "FIRST_REPORTED_VALUE", "", ""
        elif seen is None:
            disposition, previous_value, previous_time = "PREVIOUS_VALUE_NOT_REPORTED", "", ""
        else:
            disposition, previous_value, previous_time = ("CHANGE_BETWEEN_TWO_REPORTED_VALUES",
                                                          seen[0], seen[1])
        counts[subject] = counts.get(subject, 0) + 1
        emit({
            "record_time_us": moment, "event_kind": "STATE_TRANSITION",
            "subject_hex": identity(subject) if subjects else "",
            "subject_columns_hex": identity(subjects) if subjects else "",
            "state_column_hex": text(column), "state_meaning_hex": text(citation),
            "previous_state_hex": text(previous_value) if previous_value != "" else "",
            "reported_state_hex": text(value), "transition_disposition": disposition,
            "transition_index": counts[subject],
            "previous_record_time_us": previous_time,
            "elapsed_us": "" if previous_time == "" else moment - previous_time,
            "declared_clock_basis": clock, "state_transition_basis": TRANSITION_BASIS,
            "observation_rate_basis": "", "source_sha256": digest})
        report["state_transitions"] += 1
        if disposition == "CHANGE_BETWEEN_TWO_REPORTED_VALUES":
            report["reported_value_changes"] += 1
        last[subject] = (value, moment)


def subject_of(cells, index, subjects):
    """The declared identity of one saved row; a missing component is refused, never merged."""
    values = tuple(cells[index[name]] for name in subjects)
    if any(value == "" for value in values):
        # Blank components would collapse unrelated unknown subjects into one shared history.
        raise ValueError("a saved row is missing a declared identity component")
    return values


def window_plan(rows, index, subjects, span):
    """Per subject, the sorted times and how many windows the saved span would tile into."""
    series = {}
    for moment, cells in rows:
        series.setdefault(subject_of(cells, index, subjects), []).append(moment)
    planned = 0
    for moments in series.values():
        moments.sort()
        planned += (moments[-1] - moments[0]) // span + 1
    return series, planned


def windows(series, subjects, span, expected, recording_end, digest, clock, emit, report):
    """Counts of records this file contains, tiled from each subject's own first record.

    The tiling follows the saved span, which can be arbitrarily wide for two rows far apart, so the
    number of windows is bounded before any row is built. Each count advances through the sorted
    times by bisection instead of rescanning every moment for every window.
    """
    for subject, moments in series.items():
        start, last = moments[0], moments[-1]
        covered = last if recording_end is None else max(last, recording_end)
        position, ordinal = start, 0
        while position <= last:
            end = position + span
            ordinal += 1
            observed = bisect.bisect_left(moments, end) - bisect.bisect_left(moments, position)
            # Coverage is judged by what was actually observed, or by a recording end the caller
            # declared. It is never extrapolated from the expected interval: that would invent
            # capture past the last saved record.
            partial = end > covered
            declared_coverage = partial is False and end > last
            emit({
                "record_time_us": position, "event_kind": "OBSERVATION_WINDOW",
                "subject_hex": identity(subject) if subject else "",
                "subject_columns_hex": identity(subjects) if subjects else "",
                "window_index": ordinal, "window_start_us": position, "window_end_us": end,
                "window_observations": observed,
                # A window the file does not span carries no expectation at all, so a truncated
                # recording is never reported as a shortfall.
                "window_expected_observations": "" if partial else span // expected,
                "window_observed_span_us": last - start,
                "declared_expected_interval_us": expected,
                "window_rate_ratio": "" if partial else
                (Decimal(observed * expected) / Decimal(span)).quantize(Decimal("0.000001")),
                "window_disposition": "PARTIAL_WINDOW_NOT_COMPARABLE" if partial
                else "COMPARED_WITHIN_THE_DECLARED_RECORDING_BOUNDARY" if declared_coverage
                else "COMPARED_WITH_THE_DECLARED_EXPECTATION",
                "declared_recording_end_us": "" if recording_end is None else recording_end,
                "declared_clock_basis": clock, "state_transition_basis": "",
                "observation_rate_basis": RATE_BASIS, "source_sha256": digest})
            report["observation_windows"] += 1
            report["partial_windows"] += partial
            report["windows_covered_by_declaration"] += declared_coverage
            position = end


def convert(data, *, clock_basis, states=(), subjects=(), window_us=None, expected_interval_us=None,
            recording_end_us=None, time_column="record_time_us"):
    if not isinstance(clock_basis, str) or not 1 <= len(clock_basis) <= DECLARATION_LIMIT:
        raise ValueError("the saved clock basis must be declared by the caller")
    for column, citation in states:
        declared_name(column)
        if not isinstance(citation, str) or not 1 <= len(citation) <= CITATION_LIMIT:
            # A state word without a stated meaning source is a name, and a name is not a meaning.
            raise ValueError("every selected state column requires a declared meaning source")
    if len({column for column, _ in states}) != len(states):
        raise ValueError("duplicate selected state column")
    subjects = tuple(declared_name(name) for name in subjects)
    if len(set(subjects)) != len(subjects):
        raise ValueError("duplicate subject column")
    if (window_us is None) != (expected_interval_us is None):
        raise ValueError("a window needs both its length and the declared expected interval")
    if window_us is not None:
        if type(window_us) is not int or type(expected_interval_us) is not int:
            raise ValueError("window declarations must be integers of microseconds")
        if not 0 < expected_interval_us <= window_us or window_us > 2**62:
            raise ValueError("window declarations out of range")
        if window_us % expected_interval_us:
            # Rounding an expectation into existence would invent the number being compared against.
            raise ValueError("the window must be an exact multiple of the expected interval")
    if recording_end_us is not None:
        if window_us is None:
            raise ValueError("a declared recording end applies only to a window")
        if type(recording_end_us) is not int or not 0 <= recording_end_us < 2**63:
            raise ValueError("a declared recording end must be microseconds on the saved clock")
    if not states and window_us is None:
        raise ValueError("nothing selected: declare a state column or a window")
    header, index, rows = read_rows(data, declared_name(time_column))
    for name in subjects + tuple(column for column, _ in states):
        if name not in header:
            raise ValueError("declared column is not in the saved header")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = {"saved_rows": len(rows), "saved_columns": len(header), "state_transitions": 0,
              "reported_value_changes": 0, "blank_state_records": 0, "observation_windows": 0,
              "partial_windows": 0, "repeated_state_records": 0,
              "windows_covered_by_declaration": 0, "planned_windows": 0,
              "declared_recording_end_us": recording_end_us, "declared_clock_basis": clock_basis,
              "declared_state_columns": {column: citation for column, citation in states},
              "declared_subject_columns": list(subjects), "source_sha256": digest,
              "first_record_time_us": rows[0][0], "last_record_time_us": rows[-1][0],
              "clock": "Unknown", "output_bytes": 0, "physical_cause_records": "NOT_DERIVED"}
    if recording_end_us is not None and recording_end_us < rows[-1][0]:
        # A declared end before the last saved record would contradict the file it describes.
        raise ValueError("the declared recording end precedes the last saved record")
    produced, budget = [], io.StringIO(newline="")
    scratch = csv.DictWriter(budget, FIELDS, lineterminator="\n", restval="")
    scratch.writeheader()
    written = len(budget.getvalue().encode("utf-8"))

    def emit(row):
        # Each row is serialized as it is produced and its real bytes are added to the budget, so a
        # long saved cell cannot accumulate past the output bound before anything checks.
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
            # Refused whole: nothing is written short, and no row is dropped to fit.
            raise ValueError("the converted rows exceed the output byte bound; refused whole "
                             "rather than truncated")
        produced.append((row["record_time_us"], len(produced), line))

    series, planned = ({}, 0) if window_us is None else window_plan(rows, index, subjects, window_us)
    report["planned_windows"] = planned
    # The saved span between two rows can be arbitrarily wide, so the tiling is counted before any
    # row is built rather than discovered by running out of memory.
    if planned > WORK_ROW_LIMIT:
        raise ValueError("the declared window would tile the saved span into more windows than the "
                         "finite work cap allows; declare a longer window or a narrower file")
    for column, citation in states:
        transitions(rows, index, column, citation, subjects, digest, clock_basis, emit, report)
    if window_us is not None:
        windows(series, subjects, window_us, expected_interval_us, recording_end_us, digest,
                clock_basis, emit, report)
    if not report["state_transitions"] and not report["observation_windows"]:
        raise ValueError("no selected state or window reached the output")
    # The common contract is a time-ordered file. Only this converter's own rows are ordered here;
    # the saved input is read in the order it was written and is never reordered. Production order
    # breaks ties, so the output is deterministic.
    header = ",".join(FIELDS) + "\n"
    body = "".join(line for _, _, line in sorted(produced, key=lambda item: item[:2]))
    report["output_bytes"] = written
    return header + body, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="CSV an adopted converter already wrote")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--clock-basis", required=True,
                        help="the saved clock as the source profile declares it; retained verbatim")
    parser.add_argument("--time-column", default="record_time_us")
    parser.add_argument("--state-column", action="append", default=[],
                        help="a reported-state column to follow; repeatable")
    parser.add_argument("--state-meaning", action="append", default=[],
                        help="COLUMN=CITATION naming where those values are defined; required")
    parser.add_argument("--subject-column", action="append", default=[],
                        help="identity column a state belongs to; repeatable")
    parser.add_argument("--window-us", type=int)
    parser.add_argument("--expected-interval-us", type=int)
    parser.add_argument("--recording-end-us", type=int,
                        help="when the caller knows the recording itself ended; windows up to it "
                             "are comparable, and coverage is never extrapolated from the interval")
    args = parser.parse_args()
    try:
        meanings = {}
        for item in args.state_meaning:
            name, separator, citation = item.partition("=")
            if not separator or name in meanings:
                raise ValueError("each --state-meaning is one COLUMN=CITATION, declared once")
            meanings[name] = citation
        if set(meanings) != set(args.state_column):
            raise ValueError("declare a meaning source for exactly the selected state columns")
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(
            args.input.read_bytes(), clock_basis=args.clock_basis, time_column=args.time_column,
            states=tuple((column, meanings[column]) for column in args.state_column),
            subjects=tuple(args.subject_column), window_us=args.window_us,
            expected_interval_us=args.expected_interval_us,
            recording_end_us=args.recording_end_us)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, csv.Error) as failure:
        print(f"saved state conversion failed; no successful conversion claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
