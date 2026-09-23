"""Authored rows only. Nothing is sent, no device, no real log, no captured command."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.convert_request_response_match import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/request-response-match/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))
HEADER = ("record_time_us,msg,param_id,param_index,target_sys,target_comp,src_sys,src_comp,"
          "session,param_value")
REQUEST, RESPONSE = "PARAM_REQUEST_READ", "PARAM_VALUE"
BASE = dict(request_where="msg=" + REQUEST, response_where="msg=" + RESPONSE,
            correlate="param_id=param_id",
            responder=("target_sys=src_sys", "target_comp=src_comp"),
            index_column="param_index", session_column="session",
            window_us=500_000, clock_qualified=True, expectations=("param_id=param_id",))


def saved(*rows):
    return (HEADER + "\n" + "".join(",".join(str(c) for c in row) + "\n" for row in rows)).encode()


def request(at, param="RC1_MIN", index=-1, sys_id=1, comp=1, session="A"):
    return (at, REQUEST, param, index, sys_id, comp, "", "", session, "")


def response(at, param="RC1_MIN", index=0, sys_id=1, comp=1, session="A", value="1100"):
    return (at, RESPONSE, param, index, "", "", sys_id, comp, session, value)


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class RequestResponseMatch(unittest.TestCase):
    def rows(self, data, **kwargs):
        output, report = convert(data, **dict(BASE, **kwargs))
        rows = list(csv.DictReader(io.StringIO(output)))
        for row in rows:
            self.assertIn("NEVER_EVIDENCE_THAT_THE_REQUEST_WAS_CARRIED_OUT", row["correlation_basis"])
            self.assertIn("SENDS_NOTHING_AND_EXECUTES_NOTHING", row["correlation_basis"])
            self.assertIn("NOTHING_UNKNOWN_EVER_PRODUCES_A_POSITIVE_PAIRING",
                          row["correlation_basis"])
            self.assertIn("NEVER_READ_AS_CAUSALITY", row["equal_time_policy"])
        self.assertEqual(report["sent_messages"], "NONE_THIS_TOOL_NEVER_TRANSMITS")
        self.assertEqual(report["execution_records"], "NOT_DERIVED_A_REPLY_IS_NOT_AN_EXECUTION")
        self.assertEqual(report["truncated_rows"],
                         "NONE_EVERY_SELECTED_REQUEST_PRODUCES_ONE_ROW")
        return rows, report

    def test_a_fully_qualified_single_reply_is_matched_and_joins_back(self):
        rows, report = self.rows(saved(request(1000), response(1200)))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["match_outcome"], "MATCHED_DECLARED_FIELDS_AGREE")
        self.assertEqual(row["ambiguity_reasons"], "")
        self.assertEqual((row["candidate_count"], row["response_row_index"]), ("1", "1"))
        self.assertEqual(row["response_elapsed_us"], "200")
        self.assertEqual(json.loads(unhex(row["request_identity_hex"])), ["1", "1"])
        self.assertEqual(row["declared_identity_basis"], "CALLER_DECLARED_2_IDENTITY_COLUMN_PAIRS")
        self.assertEqual(json.loads(unhex(row["request_join_hex"])), [["param_id", "RC1_MIN"]])
        self.assertEqual(json.loads(unhex(row["response_join_hex"])), [["param_id", "RC1_MIN"]])
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["standing_basis_gaps"], [])
        self.assertIn("CONSISTENT_WITH_THIS_REQUEST_AND_NEVER_THAT_IT_IS_THE_REPLY_TO_IT",
                      row["correlation_basis"])
        composite, _ = self.rows(saved(request(1000), response(1200)),
                                 responder=("target_sys=src_sys",), identity_composite=True)
        self.assertEqual(composite[0]["match_outcome"], "MATCHED_DECLARED_FIELDS_AGREE")
        self.assertEqual(composite[0]["declared_identity_basis"],
                         "CALLER_DECLARED_A_COMPOSITE_COLUMN_CARRYING_SYSTEM_AND_COMPONENT")

    def test_a_pairing_with_nothing_declared_to_compare_never_claims_agreement(self):
        rows, report = self.rows(saved(request(1000), response(1200)), expectations=())
        self.assertEqual(rows[0]["match_outcome"],
                         "PAIRED_BUT_NO_DECLARED_FIELD_EXPECTATION_SO_NOTHING_WAS_COMPARED")
        self.assertEqual(rows[0]["response_row_index"], "1")
        self.assertEqual((report["matched"], report["paired_without_expectation"]), (0, 1))

    def test_a_differing_declared_field_is_a_mismatch_and_names_the_pair(self):
        rows, report = self.rows(saved(request(1000), response(1200, value="9999")),
                                 expectations=("param_id=param_id", "param_index=param_index"))
        self.assertEqual(rows[0]["match_outcome"], "MISMATCHED_DECLARED_FIELD_DIFFERS")
        self.assertEqual(json.loads(unhex(rows[0]["differing_expectation_hex"])),
                         [["param_index", "param_index"]])
        self.assertEqual(report["mismatched"], 1)

    def test_no_reply_in_the_window_is_missing_and_never_a_failed_command(self):
        rows, report = self.rows(saved(request(1000), response(9_000_000)))
        self.assertEqual(rows[0]["match_outcome"], "MISSING_NO_RESPONSE_IN_THE_DECLARED_WINDOW")
        self.assertEqual(rows[0]["candidate_count"], "0")
        self.assertEqual(report["missing"], 1)
        self.assertIn("NEVER_A_FAILED_COMMAND", rows[0]["correlation_basis"])
        edge, _ = self.rows(saved(request(1000), response(501_000)))
        self.assertEqual(edge[0]["candidate_count"], "1")
        same, _ = self.rows(saved(request(1000), response(1000)))
        self.assertEqual(same[0]["match_outcome"], "MISSING_NO_RESPONSE_IN_THE_DECLARED_WINDOW")

    def test_an_empty_declared_field_stays_unknown(self):
        rows, report = self.rows(saved(request(1000), response(1200, value="")),
                                 expectations=("param_id=param_value",))
        self.assertEqual(rows[0]["match_outcome"], "UNKNOWN_DECLARED_FIELD_NOT_REPORTED")
        self.assertEqual(json.loads(unhex(rows[0]["unknown_expectation_hex"])),
                         [["param_id", "param_value"]])
        self.assertEqual(report["unknown_field"], 1)

    def test_an_unreported_basis_is_ambiguity_and_never_a_positive_pair(self):
        """Nothing unknown may produce a pairing: no basis, empty basis, or an empty key."""
        cases = {
            "RESPONDER_IDENTITY_NOT_DECLARED": ({"responder": ()}, saved(request(1000),
                                                                        response(1200))),
            "RESPONDER_IDENTITY_INCOMPLETE_SYSTEM_AND_COMPONENT_NOT_BOTH_DECLARED":
                ({"responder": ("target_sys=src_sys",)}, saved(request(1000), response(1200))),
            "SESSION_NOT_DECLARED": ({"session_column": None}, saved(request(1000), response(1200))),
            "INDEX_BASIS_NOT_DECLARED": ({"index_column": None}, saved(request(1000),
                                                                      response(1200))),
            "CLOCK_BASIS_NOT_QUALIFIED": ({"clock_qualified": False}, saved(request(1000),
                                                                           response(1200))),
            "IDENTITY_FIELD_NOT_REPORTED": ({}, saved(request(1000, sys_id=""), response(1200))),
            "SESSION_FIELD_NOT_REPORTED": ({}, saved(request(1000, session=""), response(1200))),
            "INDEX_FIELD_NOT_REPORTED": ({}, saved(request(1000, index=""), response(1200))),
            "CORRELATION_KEY_NOT_REPORTED": ({}, saved(request(1000, param=""),
                                                       response(1200, param=""))),
            "INDEX_RULE_MAKES_THE_KEY_UNRELIABLE": ({}, saved(request(1000, index=7),
                                                              response(1200))),
        }
        for reason, (extra, data) in cases.items():
            rows, report = self.rows(data, **extra)
            self.assertEqual(rows[0]["match_outcome"], "AMBIGUOUS", reason)
            self.assertIn(reason, rows[0]["ambiguity_reasons"].split("+"), reason)
            self.assertEqual(rows[0]["response_row_index"], "", reason)
            self.assertEqual(rows[0]["differing_expectation_hex"], "", reason)
            self.assertEqual(report["matched"], 0, reason)

    def test_the_competing_answer_reasons_are_reported_and_never_resolved(self):
        cases = {
            "MULTIPLE_CANDIDATE_RESPONSES":
                (saved(request(1000), response(1200), response(1300)), 0),
            "ANOTHER_REQUEST_OUTSTANDING_ON_THE_SAME_KEY":
                (saved(request(1000), request(1100), response(1200)), 1),
            "RETRY_BEFORE_ANY_RESPONSE":
                (saved(request(1000), request(1400), response(9_000_000)), 0),
            "MIXED_SESSIONS":
                (saved(request(1000), response(1200, session="B")), 0),
        }
        for reason, (data, which) in cases.items():
            rows, report = self.rows(data)
            self.assertEqual(rows[which]["match_outcome"], "AMBIGUOUS", reason)
            self.assertIn(reason, rows[which]["ambiguity_reasons"].split("+"), reason)
            self.assertEqual(rows[which]["response_row_index"], "", reason)
        both, _ = self.rows(saved(request(1000), response(1200), response(1300)),
                            clock_qualified=False)
        self.assertEqual(sorted(both[0]["ambiguity_reasons"].split("+")),
                         ["CLOCK_BASIS_NOT_QUALIFIED", "MULTIPLE_CANDIDATE_RESPONSES"])

    def test_a_foreign_reply_never_answers_or_clears_a_request(self):
        """A reply from another identity or another session is not an answer, and it does not
        silently close an earlier request either."""
        wrong, _ = self.rows(saved(request(1000, sys_id=1), response(1200, sys_id=2)))
        self.assertEqual(wrong[0]["match_outcome"], "MISSING_NO_RESPONSE_IN_THE_DECLARED_WINDOW")
        wrong_component, _ = self.rows(saved(request(1000, comp=1), response(1200, comp=7)))
        self.assertEqual(wrong_component[0]["match_outcome"],
                         "MISSING_NO_RESPONSE_IN_THE_DECLARED_WINDOW")
        other_session, _ = self.rows(saved(request(1000, session="A"),
                                           response(1200, session="B")))
        self.assertIn("MIXED_SESSIONS", other_session[0]["ambiguity_reasons"].split("+"))
        rows, _ = self.rows(saved(request(1000), response(1100, sys_id=9, comp=9, session="Z"),
                                  request(1200), response(1300)))
        self.assertIn("ANOTHER_REQUEST_OUTSTANDING_ON_THE_SAME_KEY",
                      rows[1]["ambiguity_reasons"].split("+"))
        cleared, _ = self.rows(saved(request(1000), response(1100), request(1200), response(1300)))
        self.assertEqual(cleared[1]["match_outcome"], "MATCHED_DECLARED_FIELDS_AGREE")
        unreported, _ = self.rows(saved(request(1000, sys_id=""), response(1100, sys_id=""),
                                        request(1200, sys_id=""), response(1300, sys_id="")))
        self.assertEqual(sorted(unreported[1]["ambiguity_reasons"].split("+")),
                         ["ANOTHER_REQUEST_OUTSTANDING_ON_THE_SAME_KEY",
                          "IDENTITY_FIELD_NOT_REPORTED"])

    def test_a_moderate_multi_key_multi_session_file_is_read_whole(self):
        """Ordinary volume: every selected request produces a row, nothing is truncated, and the
        per-key window scan keeps it quick."""
        rows_in, at = [], 1000

        def party(index):
            return dict(sys_id=1 + index % 3, comp=1 + index % 5, session="S%d" % (index % 4))

        for index in range(400):
            rows_in.append(request(at, param="ANS%03d" % index, **party(index)))
            rows_in.append(response(at + 100, param="ANS%03d" % index, **party(index)))
            at += 20_000
        for index in range(150):
            rows_in.append(request(at, param="NON%03d" % index, **party(index)))
            at += 20_000
        for index in range(50):
            rows_in.append(request(at, param="TWO%03d" % index, **party(index)))
            at += 20_000
            rows_in.append(request(at, param="TWO%03d" % index, **party(index)))
            at += 20_000
        rows_in.sort(key=lambda row: row[0])
        started = time.monotonic()
        rows, report = self.rows(saved(*rows_in), window_us=5_000)
        elapsed = time.monotonic() - started
        self.assertEqual(len(rows), 650)
        self.assertEqual(report["selected_requests"], 650)
        self.assertEqual(report["distinct_request_keys"], 600)
        self.assertEqual(report["matched"] + report["missing"] + report["ambiguous"], 650)
        self.assertEqual(report["matched"], 400)
        self.assertEqual(report["missing"], 200)
        self.assertEqual(report["ambiguous"], 50)
        self.assertEqual(report["ambiguity_reasons"],
                         {"ANOTHER_REQUEST_OUTSTANDING_ON_THE_SAME_KEY": 50})
        self.assertEqual(report["truncated_rows"],
                         "NONE_EVERY_SELECTED_REQUEST_PRODUCES_ONE_ROW")
        self.assertLess(elapsed, 10.0)

    def test_repeated_polling_of_one_key_is_scanned_by_key_and_declared_party(self):
        """Ordinary polling: one parameter asked over and over on one key, a second session whose
        retries are never answered, and replies that belong to somebody else."""
        key, rows_in = "RC_SPEED", []
        for index in range(3000):
            at = 1_000_000 + index * 20_000
            rows_in.append(request(at, param=key, session="S1"))
            rows_in.append(response(at + 5_000, param=key, session="S1"))
        for index in range(60):
            rows_in.append(request(100_000_000 + index * 4_000, param=key, session="S2"))
        rows_in.append(request(200_000_000, param=key, sys_id=7, comp=7, session="S1"))
        rows_in.append(response(200_003_000, param=key, session="S1"))
        rows_in.append(request(300_000_000, param=key, session="S3"))
        rows_in.append(response(300_003_000, param=key, session="S1"))
        rows_in.sort(key=lambda row: row[0])
        started = time.monotonic()
        rows, report = self.rows(saved(*rows_in), window_us=10_000)
        elapsed = time.monotonic() - started
        self.assertEqual((len(rows), report["selected_requests"]), (3062, 3062))
        self.assertEqual(report["selected_responses"], 3002)
        self.assertEqual(report["distinct_request_keys"], 1)
        self.assertEqual(report["distinct_request_parties"], 4)
        self.assertEqual(report["matched"], 3000)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["ambiguous"], 61)
        self.assertEqual(report["ambiguity_reasons"],
                         {"ANOTHER_REQUEST_OUTSTANDING_ON_THE_SAME_KEY": 59,
                          "RETRY_BEFORE_ANY_RESPONSE": 59, "MIXED_SESSIONS": 1})
        self.assertEqual(report["truncated_rows"],
                         "NONE_EVERY_SELECTED_REQUEST_PRODUCES_ONE_ROW")
        self.assertLess(elapsed, 5.0)

    def test_a_wrong_declaration_or_unreadable_file_is_refused(self):
        good = saved(request(1000), response(1200))
        for kwargs, why in (
                ({"request_where": "nope=x"}, "a selector column not in the header"),
                ({"correlate": "param_id=nope"}, "a correlation column not in the header"),
                ({"expectations": ("param_id=nope",)}, "an expectation column not in the header"),
                ({"responder": ("target_sys=nope",)}, "a responder column not in the header"),
                ({"session_column": "nope"}, "a session column not in the header"),
                ({"index_column": "nope"}, "an index column not in the header"),
                ({"request_where": "msg=NOT_PRESENT"}, "a selector that selects no row"),
                ({"window_us": 0}, "a non-positive window"),
                ({"window_us": True}, "a boolean window"),
                ({"clock_qualified": "yes"}, "a non-boolean clock declaration"),
                ({"identity_composite": "yes"}, "a non-boolean composite declaration"),
                ({"identity_composite": True}, "a composite claim over two declared pairs"),
                ({"correlate": "param_id"}, "a correlation that is not a pair"),
                ({"expectations": tuple("param_id=param_id" for _ in range(17))},
                 "more expectation pairs than the bound"),
                ({"responder": tuple("target_sys=src_sys" for _ in range(9))},
                 "more identity pairs than the bound"),
                ({"responder": ("target_sys=src_sys", "target_sys=src_sys")},
                 "one system-only mapping declared twice"),
                ({"responder": ("target_sys=src_sys", "target_sys=src_comp")},
                 "one request column declared against two response columns"),
                ({"responder": ("target_sys=src_sys", "target_comp=src_sys")},
                 "two request columns read from one response column"),
                ({"time_column": "nope"}, "a time column not in the header")):
            with self.assertRaises(ValueError, msg=why):
                convert(good, **dict(BASE, **kwargs))
        for broken, why in (
                (saved(request(2000), response(1200)), "a saved time that decreases"),
                (b"record_time_us,msg\n", "a file with no rows"),
                (saved(request(1000)).replace(b"1000,", b"x,", 1), "a non-integer saved time"),
                (saved(request(1000)).replace(b"1000,", b"9223372036854775808,", 1),
                 "a saved time outside the supported range"),
                ((HEADER + "\n1000," + REQUEST + ',"RC1"X,-1,1,1,,,A,\n').encode(),
                 "a stray quote after a quoted field"),
                ((HEADER + "\n1000," + REQUEST + ',"RC1,-1,1,1,,,A,\n').encode(),
                 "an unterminated quoted field")):
            with self.assertRaises(ValueError, msg=why):
                convert(broken, **BASE)
        with self.assertRaises(ValueError):
            convert(saved((9_223_000_000_000_000_000, REQUEST, "P", -1, 1, 1, "", "", "A", "")),
                    **dict(BASE, window_us=2**53))

    def test_the_command_line_writes_declared_rows_for_the_common_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "saved.csv", base / "out"
            source.write_bytes(saved(request(1000), response(1200),
                                     request(2000, param="RC2_MIN"),
                                     request(3000, param="RC3_MIN"), response(3100, param="RC3_MIN"),
                                     response(3200, param="RC3_MIN")))
            subprocess.run([sys.executable, "-m", "scripts.convert_request_response_match",
                            str(source), str(out), "--request-where", "msg=" + REQUEST,
                            "--response-where", "msg=" + RESPONSE,
                            "--correlate", "param_id=param_id",
                            "--responder", "target_sys=src_sys",
                            "--responder", "target_comp=src_comp",
                            "--index-column", "param_index", "--session-column", "session",
                            "--window-us", "500000", "--clock-qualified",
                            "--expect", "param_id=param_id"],
                           cwd=ROOT, check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual((report["matched"], report["missing"], report["ambiguous"]), (1, 1, 1))
            self.assertEqual(report["standing_basis_gaps"], [])
            rows = list(csv.DictReader(io.StringIO((out / "observations.csv").read_text())))
            self.assertEqual([r["match_outcome"] for r in rows],
                             ["MATCHED_DECLARED_FIELDS_AGREE",
                              "MISSING_NO_RESPONSE_IN_THE_DECLARED_WINDOW", "AMBIGUOUS"])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run(
                    [READER, str(PROFILE), str(out / "observations.csv"), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                self.assertEqual(common["observations"][0]["fields"]["match_outcome"],
                                 "MATCHED_DECLARED_FIELDS_AGREE")


if __name__ == "__main__":
    unittest.main()
