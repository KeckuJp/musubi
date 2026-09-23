"""Authored sanitized fixtures only: no real token, account, host, capture or credential appears here."""
import base64
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_oauth_error_responses.py"
PROFILE = ROOT / "profiles/declared/oauth-reported-rejection/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SECRET = "PLACEHOLDER-NEVER-A-REAL-SECRET"


def entry(status, *, body=None, mime="application/json;charset=UTF-8", challenge=None,
          stamp="2026-01-01T00:00:00Z", encoding=None, headers=None):
    content = {"mimeType": mime, "size": 0}
    if body is not None:
        content["text"] = base64.b64encode(body.encode()).decode() if encoding else body
        if encoding:
            content["encoding"] = encoding
    saved = [{"name": "Set-Cookie", "value": "session=" + SECRET}]
    if challenge is not None:
        saved.append({"name": "WWW-Authenticate", "value": challenge})
    return dict(
        startedDateTime=stamp, time=12.5,
        request={"method": "POST", "url": "https://example.invalid/token?client_secret=" + SECRET,
                 "headers": [{"name": "Authorization", "value": "Basic " + SECRET}],
                 "cookies": [{"name": "session", "value": SECRET}]},
        response={"status": status, "content": content,
                  "headers": saved if headers is None else headers,
                  "cookies": [{"name": "session", "value": SECRET}]},
        vendor=SECRET)


def har(*entries):
    return json.dumps({"log": {"version": "1.2", "entries": list(entries)}})


def error_body(code=None, **extra):
    document = {} if code is None else {"error": code}
    document.update(extra)
    return json.dumps(document)


class SavedRejections(unittest.TestCase):
    def row(self, text, index=0, role="token", grant="client_credentials"):
        output, report = module.convert_saved_rejection(text, index, role, grant)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), module.FIELDS)
        self.assertNotIn(SECRET, output + json.dumps(report))
        return rows[0], report

    def test_a_token_endpoint_error_is_the_producers_own_reported_code(self):
        expected = {
            "invalid_request": "SERVER_REPORTED_THE_REQUEST_WAS_MALFORMED_OR_REPEATED_OR_MISUSED_PARAMETERS",
            "invalid_client": "SERVER_REPORTED_CLIENT_AUTHENTICATION_FAILED",
            "invalid_grant": "SERVER_REPORTED_THE_PRESENTED_GRANT_OR_REFRESH_TOKEN_WAS_NOT_ACCEPTED",
            "unauthorized_client": "SERVER_REPORTED_THE_AUTHENTICATED_CLIENT_MAY_NOT_USE_THIS_GRANT_TYPE",
            "unsupported_grant_type": "SERVER_REPORTED_IT_DOES_NOT_SUPPORT_THIS_GRANT_TYPE",
            "invalid_scope": "SERVER_REPORTED_THE_REQUESTED_SCOPE_WAS_NOT_ACCEPTED",
        }
        self.assertEqual(set(expected), set(module.TOKEN_ERRORS))
        for code, meaning in expected.items():
            row, report = self.row(har(entry(400, body=error_body(code))))
            self.assertEqual(row["oauth_error_reported"], code)
            self.assertEqual(row["oauth_rejection_reported"], meaning)
            self.assertEqual(row["oauth_error_source"], "JSON_BODY_ERROR_MEMBER_RFC6749_5_2")
            self.assertEqual(row["oauth_error_registry_disposition"],
                             "DEFINED_BY_THE_PINNED_SECTION_FOR_THIS_ENDPOINT_ROLE")
            self.assertEqual(row["oauth_status_agreement"],
                             "STATUS_IS_THE_SECTIONS_DEFAULT_400_FOR_A_TOKEN_ERROR_RESPONSE")
            self.assertEqual(row["oauth_endpoint_role"], "AUTHORIZATION_SERVER_TOKEN_ENDPOINT")
            self.assertEqual(row["oauth_grant_type_declared"], "client_credentials")
            self.assertEqual(row["record_time_us"], "1767225600000000")
            self.assertEqual(row["har_clock_basis"], "REPORTED_REQUEST_START_NOT_MEASUREMENT_OR_RESPONSE_END")
            self.assertEqual(report["records"], 1)
            self.assertIn("never proof that a credential or token", row["oauth_rejection_basis"])
            self.assertIn("never a network or power or service outage", row["oauth_rejection_basis"])
            self.assertIn("not authenticated", row["oauth_identity_basis"])
        allowed, _ = self.row(har(entry(401, body=error_body("invalid_client"))))
        self.assertEqual(allowed["oauth_status_agreement"], "SECTION_EXPLICITLY_ALLOWS_401_FOR_INVALID_CLIENT")
        other, _ = self.row(har(entry(401, body=error_body("invalid_grant"))))
        self.assertEqual(other["oauth_status_agreement"],
                         "STATUS_IS_NOT_THE_SECTIONS_DEFAULT_AND_IS_CARRIED_AS_REPORTED")

    def test_optional_text_and_unknown_members_are_accounted_not_copied(self):
        body = error_body("invalid_grant", error_description="refresh token " + SECRET,
                          error_uri="https://example.invalid/help?trace=" + SECRET,
                          trace_id=SECRET, correlation_id=SECRET)
        row, report = self.row(har(entry(400, body=body)))
        copied = "REPORTED_BUT_DELIBERATELY_NOT_COPIED_OUT_OF_THE_RETAINED_ORIGINAL"
        self.assertEqual(row["oauth_error_description_disposition"], copied)
        self.assertEqual(row["oauth_error_uri_disposition"], copied)
        self.assertEqual(row["oauth_unknown_member_count"], "2")
        self.assertEqual(report["unknown_error_members"], 2)
        self.assertEqual(row["oauth_error_body_sha256"][:7], "sha256:")
        bare, _ = self.row(har(entry(400, body=error_body("invalid_grant"))))
        self.assertEqual((bare["oauth_error_description_disposition"], bare["oauth_error_uri_disposition"]),
                         ("ABSENT", "ABSENT"))
        self.assertEqual(bare["oauth_unknown_member_count"], "0")
        encoded, _ = self.row(har(entry(400, body=body, encoding="base64")))
        self.assertEqual(encoded["oauth_error_body_sha256"], row["oauth_error_body_sha256"])

    def test_a_status_alone_never_names_an_oauth_rejection(self):
        unqualified = ("UNQUALIFIED_NO_PROTOCOL_ERROR_REPORTED_SO_THE_STATUS_ALONE_NAMES_NO_OAUTH_"
                       "REJECTION")
        row, report = self.row(har(entry(401, body=error_body())))
        self.assertEqual(row["oauth_error_source"], "NO_ERROR_REPORTED")
        self.assertEqual(row["oauth_error_reported"], "")
        self.assertEqual(row["oauth_rejection_reported"], unqualified)
        self.assertEqual(row["oauth_status_agreement"],
                         "STATUS_NOT_QUALIFIED_BY_THE_SECTION_BECAUSE_NO_ERROR_CODE_WAS_REPORTED")
        self.assertEqual(report["error_reported"], "")
        for status, challenge in ((401, 'Bearer realm="example"'), (403, None)):
            resource, _ = module.convert_saved_rejection(
                har(entry(status, challenge=challenge)), 0, "protected-resource")
            record = next(csv.DictReader(io.StringIO(resource)))
            self.assertEqual(record["oauth_error_source"], "NO_ERROR_REPORTED")
            self.assertEqual(record["oauth_rejection_reported"], unqualified)
            self.assertEqual(record["oauth_challenge_scheme_reported"],
                             "BEARER" if challenge else "NO_CHALLENGE_REPORTED")
            self.assertEqual(record["oauth_unknown_member_count"], "")
            self.assertEqual(record["oauth_error_body_sha256"],
                             "BODY_NOT_READ_FOR_A_PROTECTED_RESOURCE_REJECTION")

    def test_a_protected_resource_rejection_reads_only_the_challenge_error(self):
        self.assertEqual(module.RESOURCE_SHOULD_STATUS,
                         {"invalid_request": 400, "invalid_token": 401, "insufficient_scope": 403})
        self.assertEqual(set(module.RESOURCE_ERRORS), set(module.RESOURCE_SHOULD_STATUS))
        for code, status in (("invalid_request", 400), ("invalid_token", 401), ("insufficient_scope", 403)):
            challenge = ('Bearer realm="example", error="%s", error_description="%s", '
                         'scope="urn:example:placeholder"' % (code, SECRET))
            text = har(entry(status, challenge=challenge))
            output, report = module.convert_saved_rejection(text, 0, "protected-resource")
            self.assertNotIn(SECRET, output + json.dumps(report))
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(row["oauth_error_reported"], code)
            self.assertEqual(row["oauth_rejection_reported"], module.RESOURCE_ERRORS[code])
            self.assertEqual(row["oauth_error_source"], "BEARER_CHALLENGE_ERROR_ATTRIBUTE_RFC6750_3")
            self.assertEqual(row["oauth_status_agreement"],
                             "STATUS_MATCHES_THE_SECTIONS_SHOULD_FOR_THIS_CODE")
            self.assertEqual(row["oauth_scope_attribute_disposition"],
                             "REQUIRED_SCOPE_HINT_REPORTED_BUT_ITS_VALUE_IS_NOT_COPIED")
            self.assertEqual(row["oauth_error_description_disposition"],
                             "REPORTED_BUT_DELIBERATELY_NOT_COPIED_OUT_OF_THE_RETAINED_ORIGINAL")
            self.assertEqual(row["oauth_error_uri_disposition"], "ABSENT")
            self.assertEqual(row["oauth_grant_type_declared"], "NOT_APPLICABLE_TO_THIS_ENDPOINT_ROLE")
        mismatch, _ = module.convert_saved_rejection(
            har(entry(500, challenge='Bearer error="invalid_token"')), 0, "protected-resource")
        self.assertEqual(next(csv.DictReader(io.StringIO(mismatch)))["oauth_status_agreement"],
                         "STATUS_DIFFERS_FROM_THE_SECTIONS_SHOULD_AND_IS_CARRIED_AS_REPORTED")

    def test_a_code_is_never_borrowed_across_roles_or_invented(self):
        row, _ = self.row(har(entry(401, body=error_body("invalid_token"))))
        self.assertEqual(row["oauth_error_reported"], "invalid_token")
        self.assertEqual(row["oauth_error_registry_disposition"],
                         "NOT_DEFINED_FOR_THIS_ENDPOINT_ROLE_BY_THE_PINNED_SECTIONS_RETAINED_AS_REPORTED")
        self.assertEqual(row["oauth_rejection_reported"], "REPORTED_CODE_NOT_NAMED_BY_THE_PINNED_SECTIONS")
        borrowed, _ = module.convert_saved_rejection(
            har(entry(403, challenge='Bearer error="unsupported_grant_type"')), 0, "protected-resource")
        self.assertEqual(next(csv.DictReader(io.StringIO(borrowed)))["oauth_error_registry_disposition"],
                         "NOT_DEFINED_FOR_THIS_ENDPOINT_ROLE_BY_THE_PINNED_SECTIONS_RETAINED_AS_REPORTED")
        for code in ("slow_down", "authorization_pending", "invalid_target"):
            unlisted, _ = self.row(har(entry(400, body=error_body(code))))
            self.assertEqual(unlisted["oauth_error_reported"], code)
            self.assertEqual(unlisted["oauth_error_registry_disposition"],
                             "NOT_DEFINED_BY_THE_PINNED_SECTIONS_RETAINED_AS_REPORTED")
            self.assertEqual(unlisted["oauth_rejection_reported"],
                             "REPORTED_CODE_NOT_NAMED_BY_THE_PINNED_SECTIONS")

    def test_declared_context_and_input_shape_are_required(self):
        body = error_body("invalid_grant")
        good = har(entry(400, body=body))
        for index, role, grant in ((0, "token", None), (0, "token", "urn:ietf:params:oauth:grant-type:saml2-bearer"),
                                   (0, "authorization", "password"), (0, "protected-resource", "password"),
                                   (1, "token", "password"), (-1, "token", "password"),
                                   (True, "token", "password"), ("0", "token", "password")):
            with self.assertRaises(ValueError):
                module.convert_saved_rejection(good, index, role, grant)
        for status in (0, 200, 204, 302, 399):
            with self.assertRaises(ValueError):
                self.row(har(entry(status, body=body)))
        for content in ({"mimeType": "text/html", "text": "<html>" + SECRET + "</html>", "size": 0},
                        {"mimeType": "application/json", "text": "[]", "size": 0},
                        {"mimeType": "application/json", "text": "not json", "size": 0},
                        {"mimeType": "application/json", "size": 0},
                        {"mimeType": "application/json", "text": "x", "encoding": "gzip", "size": 0},
                        {"mimeType": "application/json", "text": "{" + '"a":1,' * 20000 + '"b":2}',
                         "size": 0}):
            item = entry(400, body=body)
            item["response"]["content"] = content
            with self.assertRaises(ValueError):
                self.row(har(item))
        for value in ("", 7, None, True, 'has"quote', "has\\backslash", "line\nbreak", "x" * 257,
                      "has,comma"):
            with self.assertRaises(ValueError):
                self.row(har(entry(400, body=json.dumps({"error": value}))))
        with self.assertRaises(ValueError):
            module.convert_saved_rejection(har(entry(401, challenge='Bearer error="has,comma"')), 0,
                                           "protected-resource")
        over = error_body("invalid_grant", **{"k%d" % n: 1 for n in range(module.MAX_UNKNOWN_MEMBERS + 1)})
        with self.assertRaises(ValueError):
            self.row(har(entry(400, body=over)))
        self.assertEqual(self.row(har(entry(400, body=error_body(
            "invalid_grant", **{"k%d" % n: 1 for n in range(module.MAX_UNKNOWN_MEMBERS)}))))[0]
            ["oauth_unknown_member_count"], str(module.MAX_UNKNOWN_MEMBERS))

    def test_a_malformed_challenge_is_refused_rather_than_resolved(self):
        for challenge in ('Basic realm="example"',                       # not the Bearer scheme
                          'Bearer error="invalid_token", error="invalid_request"',   # S3: at most once
                          'Bearer error="invalid_token", error_description="a", error_description="b"',
                          'Bearer error=""',                             # empty is not a reported code
                          'Bearer realm="' + "x" * 5000 + '"'):          # past the declared bound
            with self.assertRaises(ValueError):
                module.convert_saved_rejection(har(entry(401, challenge=challenge)), 0,
                                               "protected-resource")
        for headers in ("not-a-list", [{"value": "Bearer"}], [{"name": "WWW-Authenticate"}],
                        [{"name": "WWW-Authenticate", "value": 'Bearer error="invalid_token"'}] * 2,
                        [{"name": "x", "value": "y"}] * (module.MAX_HEADERS + 1)):
            with self.assertRaises(ValueError):
                module.convert_saved_rejection(har(entry(401, headers=headers)), 0, "protected-resource")

    def challenge_row(self, challenge, status=401):
        output, report = module.convert_saved_rejection(
            har(entry(status, challenge=challenge)), 0, "protected-resource")
        self.assertNotIn(SECRET, output + json.dumps(report))
        return next(csv.DictReader(io.StringIO(output)))

    def test_a_challenge_is_parsed_at_its_auth_param_boundaries(self):
        unqualified = ("UNQUALIFIED_NO_PROTOCOL_ERROR_REPORTED_SO_THE_STATUS_ALONE_NAMES_NO_OAUTH_"
                       "REJECTION")
        normal = self.challenge_row('Bearer realm="example", error="invalid_token"')
        self.assertEqual(normal["oauth_error_reported"], "invalid_token")
        self.assertEqual(normal["oauth_error_source"], "BEARER_CHALLENGE_ERROR_ATTRIBUTE_RFC6750_3")
        self.assertEqual(normal["oauth_unknown_challenge_attribute_count"], "0")
        for realm in ("a, error=invalid_token", 'closing \\" error=\\"invalid_token\\" still realm',
                      "error=invalid_token"):
            planted = self.challenge_row('Bearer realm="%s"' % realm)
            self.assertEqual(planted["oauth_error_source"], "NO_ERROR_REPORTED")
            self.assertEqual(planted["oauth_error_reported"], "")
            self.assertEqual(planted["oauth_rejection_reported"], unqualified)
            self.assertEqual(planted["oauth_challenge_scheme_reported"], "BEARER")
        described = self.challenge_row('Bearer error="insufficient_scope", '
                                       'error_description="try error=\\"invalid_token\\" next"',
                                       status=403)
        self.assertEqual(described["oauth_error_reported"], "insufficient_scope")
        upper = self.challenge_row('bearer Realm="example", ERROR="invalid_token"')
        self.assertEqual(upper["oauth_error_reported"], "invalid_token")
        self.assertEqual(upper["oauth_error_registry_disposition"],
                         "DEFINED_BY_THE_PINNED_SECTION_FOR_THIS_ENDPOINT_ROLE")
        cased = self.challenge_row('Bearer error="Invalid_Token"')
        self.assertEqual(cased["oauth_error_reported"], "Invalid_Token")
        self.assertEqual(cased["oauth_error_registry_disposition"],
                         "NOT_DEFINED_BY_THE_PINNED_SECTIONS_RETAINED_AS_REPORTED")
        bare = self.challenge_row('Bearer realm="example", error=invalid_token')
        self.assertEqual(bare["oauth_error_reported"], "invalid_token")
        extra = self.challenge_row('Bearer realm="example", error="invalid_token", '
                                   'trace_id="%s", vendor_hint="%s"' % (SECRET, SECRET))
        self.assertEqual(extra["oauth_unknown_challenge_attribute_count"], "2")
        self.assertEqual(extra["oauth_error_reported"], "invalid_token")

    def test_an_ambiguous_or_malformed_challenge_is_refused_not_borrowed(self):
        for challenge in ('Bearer realm="example", Basic realm="other"',   # a second scheme after params
                          'Bearer error="invalid_token", Newauth realm="x"',
                          'Basic realm="example"',                         # not the Bearer scheme at all
                          'Bearer error="unterminated',                    # malformed quoted value
                          'Bearer realm="a\\',                             # malformed quoted pair
                          'Bearer error=',                                 # no value at all
                          'Bearer error="invalid_token" realm="x"',        # no separator between params
                          'Bearer =invalid_token',                         # no name at all
                          'Bearer error="a", Error="b"'):                  # repeated, case-insensitively
            with self.assertRaises(ValueError) as refusal:
                self.challenge_row(challenge)
            self.assertNotIn(SECRET, str(refusal.exception))
            self.assertNotIn("realm", str(refusal.exception))
        secret_bearing = 'Bearer realm="%s", error="invalid_token" trailing="%s"' % (SECRET, SECRET)
        with self.assertRaises(ValueError) as refusal:
            self.challenge_row(secret_bearing)
        self.assertNotIn(SECRET, str(refusal.exception))
        empty = self.challenge_row("Bearer")
        self.assertEqual(empty["oauth_error_source"], "NO_ERROR_REPORTED")
        self.assertEqual(empty["oauth_unknown_challenge_attribute_count"], "0")
        with self.assertRaises(ValueError):
            self.challenge_row("Bearer " + ", ".join('k%d="v"' % n
                                                     for n in range(module.MAX_CHALLENGE_PARAMS + 1)))

    def test_the_shared_har_intake_is_reused_unmodified(self):
        from scripts import convert_opcua_recorded
        self.assertIs(module.har_log, convert_opcua_recorded.har_log)
        body = error_body("invalid_grant")
        self.assertEqual(self.row("﻿" + har(entry(400, body=body)))[1]["records"], 1)
        for document in ({"version": "1.3", "entries": [entry(400, body=body)]},
                         {"version": "1.2", "entries": {}}):
            with self.assertRaises(ValueError):
                self.row(json.dumps({"log": document}))
        row, report = self.row(har(entry(200), entry(400, body=body), entry(503)), index=1)
        self.assertEqual((report["har_entries"], report["har_unselected_entries"]), (3, 2))
        self.assertEqual(row["har_entry_index"], "1")

    def test_the_command_line_writes_one_declared_row_for_the_common_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = base / "saved.har", base / "out.csv", base / "report.json"
            source.write_text(har(entry(400, body=error_body("invalid_client",
                                                            error_description=SECRET))), encoding="utf-8")
            subprocess.run([sys.executable, "-m", "scripts.convert_oauth_error_responses", str(source),
                            str(output), str(report), "--har-entry", "0", "--endpoint-role", "token",
                            "--grant-type", "refresh_token"], cwd=ROOT, check=True, capture_output=True)
            converted = output.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, converted + report.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(report.read_text())["error_reported"], "invalid_client")
            failed = subprocess.run([sys.executable, "-m", "scripts.convert_oauth_error_responses",
                                     str(source), str(base / "b.csv"), str(base / "b.json"),
                                     "--har-entry", "0", "--endpoint-role", "protected-resource",
                                     "--grant-type", "password"], cwd=ROOT, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([READER, str(PROFILE), str(output)],
                                                   capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 1)
                observation = common["observations"][0]
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertEqual(observation["fields"]["oauth_error_reported"], "invalid_client")
                self.assertEqual(observation["fields"]["oauth_rejection_reported"],
                                 "SERVER_REPORTED_CLIENT_AUTHENTICATION_FAILED")


if __name__ == "__main__":
    unittest.main()
