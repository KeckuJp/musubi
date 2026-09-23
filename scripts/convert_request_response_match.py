#!/usr/bin/env python3
"""Passive comparison of a saved request against the saved responses that may answer it.

This sends nothing, executes nothing and is not a command executor. It reads one file the caller
already has, and states for each explicitly selected request whether a response matched, mismatched,
was missing, was ambiguous, or left a declared field unknown.

An observed response is evidence that a reply was recorded. It is never evidence that anything
physically happened, that a value is current, or that a request was carried out.

The selection this was written for is a benign configuration READ -- MAVLink `PARAM_REQUEST_READ`
answered by `PARAM_VALUE`, pinned at mavlink/mavlink 3203f89c510337c0088244735c6a5056c52b5a28,
message_definitions/v1.0/common.xml sha256:04f01bbaa94e51e554765ba1fc7f3058204876cffb8de99a49b6671b2
c9bfc58. Two facts from that pin shape everything below: `PARAM_VALUE` carries no target_system and
no target_component, and neither message carries any sequence or transaction id, so the only linkage
is the parameter id or index and a matching reply may exist for some other reason entirely.

Nothing unknown is ever allowed to produce a positive pairing. A missing identity, session or index
basis is an ambiguity in its own right, an empty key never matches another empty key, and a pairing
with no declared field comparison is reported as exactly that rather than as agreement.
"""
import argparse
import bisect
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

try:
    from scripts.convert_declared_expectation_check import declared_name, guarded, identity, text
except ModuleNotFoundError:  # running the file directly
    from convert_declared_expectation_check import declared_name, guarded, identity, text

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
ROW_LIMIT = 200000
COLUMN_LIMIT = 256
VALUE_LIMIT = 256
EXPECTATION_LIMIT = 16
IDENTITY_PAIR_LIMIT = 8
# The common range this estate already uses for a saved microsecond clock. A request time plus the
# declared window must stay inside it, so a window can never push a comparison out of range.
TIME_LIMIT = 2**63
WINDOW_LIMIT = 2**53
# The pinned index rule, in the specification's own words: "Send -1 to use the param ID field as
# identifier (else the param id will be ignored)". An index other than -1 is therefore not keyed on
# the id at all -- and an index nobody declared is unknown, never assumed to have been -1.
INDEX_SENTINEL = "-1"
MATCHED = "MATCHED_DECLARED_FIELDS_AGREE"
NO_EXPECTATION = "PAIRED_BUT_NO_DECLARED_FIELD_EXPECTATION_SO_NOTHING_WAS_COMPARED"
MISMATCHED = "MISMATCHED_DECLARED_FIELD_DIFFERS"
MISSING = "MISSING_NO_RESPONSE_IN_THE_DECLARED_WINDOW"
UNKNOWN = "UNKNOWN_DECLARED_FIELD_NOT_REPORTED"
AMBIGUOUS = "AMBIGUOUS"
# Ambiguity reasons. The first group is "more than one thing could answer"; the second is "the basis
# for saying anything at all was never established".
MULTIPLE = "MULTIPLE_CANDIDATE_RESPONSES"
OUTSTANDING = "ANOTHER_REQUEST_OUTSTANDING_ON_THE_SAME_KEY"
RETRY = "RETRY_BEFORE_ANY_RESPONSE"
MIXED_SESSIONS = "MIXED_SESSIONS"
UNQUALIFIED_CLOCK = "CLOCK_BASIS_NOT_QUALIFIED"
INDEX_RULE = "INDEX_RULE_MAKES_THE_KEY_UNRELIABLE"
INDEX_UNDECLARED = "INDEX_BASIS_NOT_DECLARED"
INDEX_EMPTY = "INDEX_FIELD_NOT_REPORTED"
IDENTITY_UNDECLARED = "RESPONDER_IDENTITY_NOT_DECLARED"
IDENTITY_INCOMPLETE = "RESPONDER_IDENTITY_INCOMPLETE_SYSTEM_AND_COMPONENT_NOT_BOTH_DECLARED"
IDENTITY_EMPTY = "IDENTITY_FIELD_NOT_REPORTED"
SESSION_UNDECLARED = "SESSION_NOT_DECLARED"
SESSION_EMPTY = "SESSION_FIELD_NOT_REPORTED"
KEY_EMPTY = "CORRELATION_KEY_NOT_REPORTED"
EQUAL_TIME_POLICY = ("ROWS_SHARING_ONE_SAVED_TIME_KEEP_THE_ORDER_THE_FILE_GIVES_THEM_AND_THAT_ORDER_"
                     "IS_NEVER_READ_AS_CAUSALITY_A_RESPONSE_AT_THE_REQUESTS_OWN_INSTANT_IS_NOT_AFTER_"
                     "IT_AND_IS_NEVER_A_CANDIDATE")
CORRELATION_BASIS = (
    "A_COMPARISON_BETWEEN_ONE_SAVED_REQUEST_AND_THE_SAVED_ROWS_THE_CALLER_DECLARED_MIGHT_ANSWER_IT_"
    "WITHIN_A_CALLER_DECLARED_WINDOW_ON_A_CALLER_DECLARED_CLOCK_AN_OBSERVED_RESPONSE_IS_EVIDENCE_"
    "THAT_A_REPLY_WAS_RECORDED_AND_IS_NEVER_EVIDENCE_THAT_THE_REQUEST_WAS_CARRIED_OUT_THAT_ANY_"
    "VALUE_IS_CURRENT_OR_THAT_ANYTHING_PHYSICAL_HAPPENED_A_MISSING_RESPONSE_IS_ABSENCE_IN_THIS_"
    "SAVED_FILE_AND_IS_NEVER_A_FAILED_COMMAND_A_TIMEOUT_VERDICT_OR_EVIDENCE_THE_REQUEST_WAS_LOST_"
    "WHERE_MORE_THAN_ONE_ROW_COULD_ANSWER_OR_THE_LINKAGE_IS_NOT_UNIQUE_THE_OUTCOME_IS_AMBIGUOUS_AND_"
    "NO_CANDIDATE_IS_CHOSEN_NOTHING_UNKNOWN_EVER_PRODUCES_A_POSITIVE_PAIRING_AN_EMPTY_KEY_NEVER_"
    "MATCHES_ANOTHER_EMPTY_KEY_AND_A_MISSING_IDENTITY_SESSION_OR_INDEX_BASIS_IS_ITSELF_AMBIGUITY_"
    "A_MATCHED_ROW_SAYS_ONLY_THAT_A_SAVED_REPLY_IS_CONSISTENT_WITH_THIS_REQUEST_AND_NEVER_THAT_IT_"
    "IS_THE_REPLY_TO_IT_THIS_TOOL_SENDS_NOTHING_AND_EXECUTES_NOTHING")
FIELDS = ["record_time_us", "request_row_index", "request_key_hex", "request_identity_hex",
          "request_session_hex", "match_outcome", "ambiguity_reasons", "candidate_count",
          "response_row_index", "response_record_time_us", "response_elapsed_us",
          "expectation_pairs_hex", "differing_expectation_hex", "unknown_expectation_hex",
          "declared_window_us", "declared_clock_qualification", "declared_identity_basis",
          "equal_time_policy", "request_join_hex", "response_join_hex", "correlation_basis",
          "source_sha256"]


def pair(spec, what):
    """One `LEFT=RIGHT` declaration of two saved column names."""
    if not isinstance(spec, str) or spec.count("=") != 1:
        raise ValueError(f"{what} is one LEFT=RIGHT pair of saved column names")
    left, _, right = spec.partition("=")
    return declared_name(left), declared_name(right)


def selector(spec, what):
    """One `COLUMN=VALUE` row selector; the value is compared as the exact saved text."""
    if not isinstance(spec, str) or "=" not in spec:
        raise ValueError(f"{what} is one COLUMN=VALUE selector")
    column, _, value = spec.partition("=")
    if len(value) > VALUE_LIMIT:
        raise ValueError(f"{what} value exceeds the supported length")
    return declared_name(column), value


def read_rows(data, time_column):
    """The saved CSV exactly as written; a duplicate header name is refused, never overwritten."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized saved observations")
    # strict: a malformed quote is a refusal, not a silently repaired field.
    reader = csv.reader(data.decode("utf-8-sig").splitlines(True), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("saved observations carry no header")
    except csv.Error as failure:
        raise ValueError(f"the saved CSV cannot be parsed as written: {failure}")
    if not 1 <= len(header) <= COLUMN_LIMIT or len(set(header)) != len(header):
        raise ValueError("unsupported or duplicated saved column names")
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
        if moment >= TIME_LIMIT:
            raise ValueError("saved time is outside the supported microsecond range")
        if previous is not None and moment < previous:
            raise ValueError("saved time decreases; this comparison never reorders a saved file")
        previous = moment
        rows.append((moment, cells))
    if not rows:
        raise ValueError("saved observations carry no rows")
    return header, index, rows


def convert(data, *, request_where, response_where, correlate, expectations=(), responder=(),
            identity_composite=False, index_column=None, session_column=None, window_us,
            clock_qualified=False, time_column="record_time_us"):
    """One row per selected request. Nothing is sent; no candidate is ever chosen for the caller."""
    if type(window_us) is not int or type(window_us) is bool or not 1 <= window_us <= WINDOW_LIMIT:
        raise ValueError("an explicit positive declared window in microseconds is required")
    for flag, what in ((clock_qualified, "the clock qualification"),
                       (identity_composite, "the composite identity declaration")):
        if type(flag) is not bool:
            raise ValueError(f"{what} is an explicit boolean the caller declares")
    if isinstance(responder, str):
        responder = (responder,)
    if len(expectations) > EXPECTATION_LIMIT:
        raise ValueError("too many declared expectation pairs in one comparison")
    if len(responder) > IDENTITY_PAIR_LIMIT:
        raise ValueError("too many declared responder identity pairs in one comparison")
    if identity_composite and len(responder) != 1:
        raise ValueError("a composite identity declaration describes exactly one declared pair")
    request_column, request_value = selector(request_where, "the request selector")
    response_column, response_value = selector(response_where, "the response selector")
    request_key_column, response_key_column = pair(correlate, "the correlation")
    parsed_expectations = [pair(spec, "an expectation") for spec in expectations]
    identity_pairs = [pair(spec, "the responder") for spec in responder]
    if (len({left for left, _ in identity_pairs}) != len(identity_pairs)
            or len({right for _, right in identity_pairs}) != len(identity_pairs)):
        # Two copies of one system-only mapping are one declared fact repeated, never system AND
        # component, so a repeated column can never be counted towards a complete identity.
        raise ValueError("each declared responder pair names a distinct request column and a "
                         "distinct response column")
    header, index, rows = read_rows(data, declared_name(time_column))
    needed = [request_column, response_column, request_key_column, response_key_column]
    needed += [column for spec in parsed_expectations for column in spec]
    needed += [column for spec in identity_pairs for column in spec]
    if index_column is not None:
        needed.append(declared_name(index_column))
    if session_column is not None:
        needed.append(declared_name(session_column))
    for column in needed:
        if column not in header:
            raise ValueError(f"declared column {column!r} is not in the saved header")

    def cell(row, column):
        return row[1][index[column]]

    # The basis the caller established once, for every row: it never depends on the data.
    standing = []
    if not clock_qualified:
        standing.append(UNQUALIFIED_CLOCK)
    if not identity_pairs:
        standing.append(IDENTITY_UNDECLARED)
    elif len(identity_pairs) < 2 and not identity_composite:
        # The pinned reply carries neither target_system nor target_component, so one declared
        # column alone does not establish both unless the caller says it is a composite.
        standing.append(IDENTITY_INCOMPLETE)
    if session_column is None:
        standing.append(SESSION_UNDECLARED)
    if index_column is None:
        # An index nobody declared is unknown; it is never assumed to have been the -1 sentinel.
        standing.append(INDEX_UNDECLARED)
    identity_basis = ("CALLER_DECLARED_A_COMPOSITE_COLUMN_CARRYING_SYSTEM_AND_COMPONENT"
                      if identity_composite else
                      "CALLER_DECLARED_%d_IDENTITY_COLUMN_PAIRS" % len(identity_pairs)
                      if identity_pairs else "NO_RESPONDER_IDENTITY_DECLARED")

    request_identity_columns = [left for left, _ in identity_pairs]
    response_identity_columns = [right for _, right in identity_pairs]

    def party(row, columns):
        """The caller's declared party of a saved row: its identity values and its session.

        Read from the request side of each declared pair for a request and from the response side
        for a response, so identity and session are applied identically to answers, to outstanding
        requests and to retries.
        """
        return (tuple(cell(row, name) for name in columns),
                cell(row, session_column) if session_column is not None else None)

    # Every index below is built ONCE in a single pass over the saved rows, and each question a
    # request asks afterwards is a bisect over one of them. No request ever scans the other
    # requests, and no request ever looks at another key's or another party's rows.
    requests = []
    place_in_party = {}      # request row position -> that request's own index within `peers`
    peers = {}               # (key, party) -> the saved times of that key and party's requests
    answers = {}             # (key, party) -> the responses of that key that reported that party
    answer_times = {}        # (key, party) -> their saved times
    blank_identity = {}      # key -> times of responses with an unreported identity field
    blank_session = {}       # key -> times of responses with an unreported session field
    reporting = {}           # key -> times of responses that reported identity and session
    reporting_session = {}   # (key, session) -> the same, for one declared session
    selected_responses, response_keys = 0, set()
    for position, row in enumerate(rows):
        if cell(row, request_column) == request_value:
            key = cell(row, request_key_column)
            bucket = (key, party(row, request_identity_columns))
            requests.append((position, row, key, bucket))
            place_in_party[position] = len(peers.setdefault(bucket, []))
            peers[bucket].append(row[0])
        if cell(row, response_column) != response_value:
            continue
        selected_responses += 1
        key = cell(row, response_key_column)
        response_keys.add(key)
        if any(cell(row, name) == "" for name in response_identity_columns):
            blank_identity.setdefault(key, []).append(row[0])
        elif session_column is not None and cell(row, session_column) == "":
            blank_session.setdefault(key, []).append(row[0])
        else:
            bucket = (key, party(row, response_identity_columns))
            answers.setdefault(bucket, []).append((position, row))
            answer_times.setdefault(bucket, []).append(row[0])
            reporting.setdefault(key, []).append(row[0])
            reporting_session.setdefault((key, bucket[1][1]), []).append(row[0])
    if not requests:
        raise ValueError("the request selector selected no saved row")

    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    shared = {"declared_window_us": window_us, "source_sha256": digest,
              "correlation_basis": CORRELATION_BASIS, "equal_time_policy": EQUAL_TIME_POLICY,
              "declared_identity_basis": identity_basis,
              "declared_clock_qualification": ("CALLER_DECLARED_THE_CLOCK_BASIS_QUALIFIED_FOR_THIS_"
                                               "WINDOW" if clock_qualified else
                                               "CLOCK_BASIS_NOT_DECLARED_QUALIFIED_SO_NO_WINDOW_"
                                               "BOUNDS_ANYTHING_HERE")}
    report = {"selected_requests": len(requests),
              "selected_responses": selected_responses,
              "distinct_request_keys": len({entry[2] for entry in requests}),
              "distinct_response_keys": len(response_keys),
              "distinct_request_parties": len(peers),
              "matched": 0, "paired_without_expectation": 0, "mismatched": 0, "missing": 0,
              "ambiguous": 0, "unknown_field": 0, "ambiguity_reasons": {},
              "standing_basis_gaps": standing, "declared_identity_basis": identity_basis,
              "declared_window_us": window_us, "clock_qualified": clock_qualified,
              "equal_time_policy": EQUAL_TIME_POLICY, "source_sha256": digest,
              "truncated_rows": "NONE_EVERY_SELECTED_REQUEST_PRODUCES_ONE_ROW",
              "sent_messages": "NONE_THIS_TOOL_NEVER_TRANSMITS",
              "execution_records": "NOT_DERIVED_A_REPLY_IS_NOT_AN_EXECUTION",
              "correlation_basis": CORRELATION_BASIS}

    def how_many(times, moment, until):
        """How many saved times fall strictly after `moment` and up to `until`: two bisects."""
        return bisect.bisect_right(times, until) - bisect.bisect_right(times, moment)

    produced = []
    for position, row, key, bucket in requests:
        moment = row[0]
        if moment + window_us >= TIME_LIMIT:
            raise ValueError("the declared window pushes a request beyond the supported range")
        until = moment + window_us
        reasons = list(standing)
        if key == "":
            # An unreported key is not a key. It never pairs, least of all with another empty one.
            reasons.append(KEY_EMPTY)
        if index_column is not None:
            declared_index = cell(row, index_column)
            if declared_index == "":
                reasons.append(INDEX_EMPTY)
            elif declared_index != INDEX_SENTINEL:
                reasons.append(INDEX_RULE)
        if any(cell(row, request_column_name) == "" for request_column_name, _ in identity_pairs):
            reasons.append(IDENTITY_EMPTY)
        if session_column is not None and cell(row, session_column) == "":
            reasons.append(SESSION_EMPTY)

        # A response is indexed under its own key, so only this key's rows can answer; an unreported
        # key has no group at all and never pairs, least of all with another unreported key.
        mine = answer_times.get(bucket, ()) if key != "" else ()
        candidates = []
        if key != "":
            if how_many(blank_identity.get(key, ()), moment, until):
                reasons.append(IDENTITY_EMPTY)
            if how_many(blank_session.get(key, ()), moment, until):
                reasons.append(SESSION_EMPTY)
            # A reporting reply on this key that is not in this request's declared session is mixed
            # sessions; one in this session from another identity simply does not answer.
            if how_many(reporting.get(key, ()), moment, until) > how_many(
                    reporting_session.get((key, bucket[1][1]), ()), moment, until):
                reasons.append(MIXED_SESSIONS)
            group = answers.get(bucket, ())
            candidates = group[bisect.bisect_right(mine, moment):bisect.bisect_right(mine, until)]
        if len(candidates) > 1:
            reasons.append(MULTIPLE)

        # Outstanding and retry are read off this key and party's own request times, so another
        # system's or another session's reply can never clear this request and a retry is never
        # resolved by the nearest reply. The LATEST other request from this party at or before this
        # one settles the whole class: a reply that answered it would answer every earlier one too,
        # so if that one is still unanswered the class is outstanding, and if it is answered none
        # of them is.
        own, asked = place_in_party[position], peers[bucket]
        upto = bisect.bisect_right(asked, moment)
        previous = upto - 2 if upto - 1 == own else upto - 1
        if previous >= 0 and not how_many(mine, asked[previous], moment):
            reasons.append(OUTSTANDING)
        if bisect.bisect_right(asked, until) > upto:
            reasons.append(RETRY)
        reasons = sorted(set(reasons))

        line = dict(shared, record_time_us=moment, request_row_index=position,
                    request_key_hex=text(key), match_outcome="", ambiguity_reasons="",
                    candidate_count=len(candidates), response_row_index="",
                    response_record_time_us="", response_elapsed_us="",
                    expectation_pairs_hex=identity([list(spec) for spec in parsed_expectations]),
                    differing_expectation_hex="", unknown_expectation_hex="",
                    request_identity_hex=identity(
                        [cell(row, request_column_name)
                         for request_column_name, _ in identity_pairs]) if identity_pairs else "",
                    request_session_hex=text(cell(row, session_column)) if session_column else "",
                    request_join_hex=identity([[request_key_column, key]]),
                    response_join_hex="")
        if reasons:
            # Ambiguity is decided before any matching and is never downgraded to a verdict.
            line.update(match_outcome=AMBIGUOUS, ambiguity_reasons="+".join(reasons))
            report["ambiguous"] += 1
            for reason in reasons:
                report["ambiguity_reasons"][reason] = report["ambiguity_reasons"].get(reason, 0) + 1
        elif not candidates:
            line["match_outcome"] = MISSING
            report["missing"] += 1
        else:
            answer_position, answer = candidates[0]
            line.update(response_row_index=answer_position, response_record_time_us=answer[0],
                        response_elapsed_us=answer[0] - moment,
                        response_join_hex=identity([[response_key_column,
                                                     cell(answer, response_key_column)]]))
            unknown = [spec for spec in parsed_expectations if cell(answer, spec[1]) == ""]
            differing = [spec for spec in parsed_expectations
                         if spec not in unknown and cell(row, spec[0]) != cell(answer, spec[1])]
            if not parsed_expectations:
                # A qualified pairing with nothing compared is exactly that, never agreement.
                line["match_outcome"] = NO_EXPECTATION
                report["paired_without_expectation"] += 1
            elif unknown:
                line.update(match_outcome=UNKNOWN,
                            unknown_expectation_hex=identity([list(s) for s in unknown]))
                report["unknown_field"] += 1
            elif differing:
                line.update(match_outcome=MISMATCHED,
                            differing_expectation_hex=identity([list(s) for s in differing]))
                report["mismatched"] += 1
            else:
                line["match_outcome"] = MATCHED
                report["matched"] += 1
        produced.append(line)
    output = ",".join(FIELDS) + "\n"
    output += "".join(",".join(str(line[name]) for name in FIELDS) + "\n" for line in produced)
    encoded = len(output.encode("utf-8"))
    if encoded > OUTPUT_LIMIT:
        raise ValueError("expanded output exceeds bound")
    report["output_bytes"] = encoded
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved observations an adopted converter wrote")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--time-column", default="record_time_us")
    parser.add_argument("--request-where", required=True, help="COLUMN=VALUE selecting request rows")
    parser.add_argument("--response-where", required=True, help="COLUMN=VALUE selecting response rows")
    parser.add_argument("--correlate", required=True,
                        help="REQUESTCOLUMN=RESPONSECOLUMN, the only declared linkage")
    parser.add_argument("--expect", action="append", default=[],
                        help="REQUESTCOLUMN=RESPONSECOLUMN compared as exact saved text")
    parser.add_argument("--responder", action="append", default=[],
                        help="REQUESTTARGETCOLUMN=RESPONSESOURCECOLUMN; declare system AND component, "
                             "or one composite column with --identity-composite")
    parser.add_argument("--identity-composite", action="store_true",
                        help="the caller declares the single responder pair carries system and "
                             "component together")
    parser.add_argument("--index-column",
                        help="the request's index column; a value other than -1 makes the id key "
                             "unreliable by the pinned rule, and an undeclared index stays unknown")
    parser.add_argument("--session-column")
    parser.add_argument("--window-us", type=int, required=True)
    parser.add_argument("--clock-qualified", action="store_true",
                        help="the caller declares the clock basis qualified for this window; "
                             "without it every row is ambiguous on the clock")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(
            args.input.read_bytes(), request_where=args.request_where,
            response_where=args.response_where, correlate=args.correlate,
            expectations=tuple(args.expect), responder=tuple(args.responder),
            identity_composite=args.identity_composite, index_column=args.index_column,
            session_column=args.session_column, window_us=args.window_us,
            clock_qualified=args.clock_qualified, time_column=args.time_column)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, csv.Error) as failure:
        print(f"request/response comparison failed; no comparison claim: {failure}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
