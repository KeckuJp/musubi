"""Authored `audit.k8s.io/v1` audit events, shaped exactly as the pinned writer emits them.

Schema, enums, timestamp format and JSONL framing come from Kubernetes v1.32.0
`70d3cc986aa8221cd1dfb1121852688902d3bf53`: `apiserver/pkg/apis/audit/v1/{types.go,register.go}`,
`apiserver/plugin/pkg/audit/log/backend.go`, `apimachinery/pkg/runtime/serializer/json/json.go`,
`apimachinery/pkg/apis/meta/v1/{micro_time.go,types.go}`.

No cluster, kubeconfig, API request or host log is involved: every record here is authored, and every
identifier, user name and URI is invented for this test.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_k8s_audit_events import EVENT_LIST_FORMAT, SOURCE_FORMAT, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-audit-events/profile.toml"
SCRIPT = ROOT / "scripts/convert_k8s_audit_events.py"


def event(audit_id, stage, *, received="2026-09-20T10:00:00.000000Z", stamp=None, level="Metadata",
          verb="create", uri="/api/v1/namespaces/authored-space/configmaps", user="authored-user",
          object_ref=None, response=None, **extra):
    """One audit event as the json backend writes it: a single JSON object, one per line."""
    record = {"kind": "Event", "apiVersion": "audit.k8s.io/v1", "level": level, "auditID": audit_id,
              "stage": stage, "requestURI": uri, "verb": verb,
              "user": {"username": user, "groups": ["authored-group"]},
              "sourceIPs": ["203.0.113.7"], "userAgent": "authored-client/1.0",
              "requestReceivedTimestamp": received, "stageTimestamp": stamp or received}
    if object_ref is not None:
        record["objectRef"] = object_ref
    if response is not None:
        record["responseStatus"] = response
    record.update(extra)
    return json.dumps(record)


CONFIGMAP = {"resource": "configmaps", "namespace": "authored-space", "name": "authored-config",
             "apiGroup": "", "apiVersion": "v1", "uid": "authored-uid-1"}

def writer_shaped_item(audit_id, stage, **kwargs):
    """An item as the webhook path actually produces it: no apiVersion/kind of its own.

    The versioning codec stamps the GVK on the outer EventList only, `EventList` implements no nested-object
    encoder, the generated `Event` conversion never touches `TypeMeta`, and `metav1.TypeMeta` carries
    `omitempty` on both fields - so these keys are simply absent from an item.
    """
    record = json.loads(event(audit_id, stage, **kwargs))
    del record["kind"], record["apiVersion"]
    return json.dumps(record)


def event_list(*events, metadata=None, **extra):
    """One saved webhook request body: the EventList envelope the webhook backend posts."""
    body = {"kind": "EventList", "apiVersion": "audit.k8s.io/v1",
            "items": [json.loads(line) for line in events]}
    if metadata is not None:
        body["metadata"] = metadata
    body.update(extra)
    return json.dumps(body)




class K8sAuditEventTests(unittest.TestCase):
    def rows_of(self, text, **kwargs):
        kwargs.setdefault("time_field", "stage_timestamp")
        output, report = convert(text, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report, output

    def observations(self, output):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            data = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(source), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
        self.assertEqual(data["platform_domain"], "Unknown", "no platform is inferred from an audit log")
        return data

    @staticmethod
    def text_of(cell):
        return bytes.fromhex(cell[4:]).decode() if cell.startswith("hex:") else cell


    def test_each_stage_is_its_own_row_with_identity_verb_object_and_outcome(self):
        text = "\n".join([
            event("authored-audit-1", "RequestReceived", received="2026-09-20T10:00:00.000000Z",
                  object_ref=CONFIGMAP),
            event("authored-audit-1", "ResponseComplete", received="2026-09-20T10:00:00.000000Z",
                  stamp="2026-09-20T10:00:00.250000Z", object_ref=CONFIGMAP,
                  response={"metadata": {}, "status": "Success", "code": 201}),
            event("authored-audit-2", "ResponseComplete", received="2026-09-20T10:00:05.500000Z",
                  stamp="2026-09-20T10:00:05.750000Z", verb="delete",
                  object_ref={"resource": "secrets", "namespace": "authored-space", "name": "authored-token",
                              "apiGroup": "", "apiVersion": "v1"},
                  response={"metadata": {}, "status": "Failure", "reason": "Forbidden", "code": 403}),
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertEqual((report["records"], report["rows"], report["audit_ids"]), (3, 3, 2))
        self.assertEqual(report["stage_counts"]["RequestReceived"], 1)
        self.assertEqual(report["stage_counts"]["ResponseComplete"], 2)
        self.assertEqual([r["audit_stage"] for r in rows],
                         ["RequestReceived", "ResponseComplete", "ResponseComplete"])
        self.assertEqual({r["audit_stage_status"] for r in rows}, {"SUPPORTED_PINNED_STAGE"})
        self.assertEqual([self.text_of(r["audit_id_hex"]) for r in rows],
                         ["authored-audit-1", "authored-audit-1", "authored-audit-2"])
        self.assertEqual([r["audit_verb"] for r in rows], ["create", "create", "delete"])
        self.assertEqual(self.text_of(rows[0]["audit_request_uri_hex"]),
                         "/api/v1/namespaces/authored-space/configmaps")
        self.assertEqual(self.text_of(rows[0]["audit_user_name_hex"]), "authored-user")
        self.assertEqual((rows[0]["audit_object_resource"], rows[0]["audit_object_api_version"]),
                         ("configmaps", "v1"))
        self.assertEqual(self.text_of(rows[0]["audit_object_name_hex"]), "authored-config")
        self.assertEqual(rows[0]["audit_object_api_group"], "", "the core group is the empty string")
        self.assertEqual([r["record_time_us"] for r in rows],
                         ["1789898400000000", "1789898400250000", "1789898405750000"])
        self.assertEqual(rows[1]["audit_request_received_timestamp_us"], "1789898400000000")
        self.assertEqual(rows[1]["audit_stage_timestamp_text"], "2026-09-20T10:00:00.250000Z")
        self.assertEqual({r["record_time_basis"] for r in rows}, {"APISERVER_REPORTED_STAGE_TIMESTAMP"})
        self.assertEqual({r["audit_time_status"] for r in rows},
                         {"BOTH_TIMESTAMPS_REPORTED_NEVER_SUBTRACTED"})
        self.assertEqual([r["audit_response_code"] for r in rows], ["", "201", "403"])
        self.assertEqual([r["audit_response_status_reported"] for r in rows], ["", "Success", "Failure"])
        self.assertEqual(rows[2]["audit_response_reason"], "Forbidden")
        self.assertEqual(rows[0]["audit_outcome_status"], "ABSENT_NO_RESPONSE_STATUS_IN_THIS_RECORD")
        self.assertEqual(rows[1]["audit_outcome_status"], "REPORTED_RESPONSE_STATUS")
        self.assertIn("NOT_BUSINESS_ACCEPTANCE", rows[1]["audit_outcome_basis"])
        self.assertIn("NOT_AUTHENTICATED", rows[0]["identity_basis"])
        self.assertEqual(rows[0]["source_pin"], "kubernetes-v1.32.0-70d3cc98")
        self.assertEqual(rows[0]["api_version_reported"], "audit.k8s.io/v1")
        self.assertEqual(report["audit_ids_without_response_complete"], 0)


    def test_extra_stages_a_request_without_a_response_and_withheld_bodies(self):
        text = "\n".join([
            event("authored-audit-3", "RequestReceived", received="2026-09-20T11:00:00.000000Z",
                  verb="watch", uri="/api/v1/pods?watch=true"),
            event("authored-audit-3", "ResponseStarted", received="2026-09-20T11:00:00.000000Z",
                  stamp="2026-09-20T11:00:00.100000Z", verb="watch", uri="/api/v1/pods?watch=true",
                  response={"metadata": {}, "code": 200}),
            event("authored-audit-4", "Panic", received="2026-09-20T11:05:00.000000Z",
                  stamp="2026-09-20T11:05:00.500000Z", verb="get", uri="/healthz",
                  response={"metadata": {}, "status": "Failure", "reason": "InternalError", "code": 500}),
            event("authored-audit-5", "ResponseComplete", received="2026-09-20T11:10:00.000000Z",
                  stamp="2026-09-20T11:10:00.750000Z", level="RequestResponse", object_ref=CONFIGMAP,
                  response={"metadata": {}, "status": "Success", "code": 200},
                  requestObject={"kind": "ConfigMap", "data": {"authored-secret-ish": "authored-value"}},
                  responseObject={"kind": "ConfigMap", "metadata": {"name": "authored-config"}},
                  annotations={"authorization.k8s.io/decision": "allow",
                               "authorization.k8s.io/reason": "authored authored"}),
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertEqual((report["records"], report["rows"], report["audit_ids"]), (4, 4, 3))
        self.assertEqual(report["stage_counts"], {"RequestReceived": 1, "ResponseStarted": 1,
                                                  "ResponseComplete": 1, "Panic": 1})
        self.assertEqual(report["audit_ids_without_response_complete"], 2)
        watch = [r for r in rows if self.text_of(r["audit_id_hex"]) == "authored-audit-3"]
        self.assertEqual([r["audit_id_has_response_complete"] for r in watch],
                         ["NO_RESPONSE_COMPLETE_IN_THIS_FILE"] * 2)
        self.assertEqual(watch[0]["audit_id_stage_count"], "2")
        self.assertEqual(watch[0]["audit_id_stages_present"], "RequestReceived;ResponseStarted")
        self.assertEqual(watch[1]["audit_response_code"], "200",
                         "a 200 on ResponseStarted is a transport report, not an outcome")
        panic = [r for r in rows if r["audit_stage"] == "Panic"][0]
        self.assertEqual(panic["audit_object_ref_status"],
                         "ABSENT_LIST_OR_NON_RESOURCE_REQUEST_OR_NOT_LOGGED")
        self.assertEqual(panic["audit_object_resource"], "")
        self.assertEqual(report["records_without_object_ref"], 3)
        bodies = [r for r in rows if r["audit_level"] == "RequestResponse"][0]
        self.assertEqual((bodies["request_body_status"], bodies["response_body_status"]),
                         ("PRESENT_WITHHELD_BY_THIS_IMPORTER",) * 2)
        self.assertGreater(int(bodies["request_body_reserialized_json_bytes"]), 0)
        self.assertEqual(report["withheld_bodies"], 2)
        self.assertEqual(bodies["audit_annotation_count"], "2")
        self.assertEqual(bodies["audit_annotation_keys"],
                         "authorization.k8s.io/decision;authorization.k8s.io/reason")
        for cell in bodies.values():
            self.assertNotIn("authored-value", cell)
            self.assertNotIn("allow", cell)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_both_authored_exports_reach_common_observations(self):
        _, _, first = self.rows_of("\n".join([
            event("authored-audit-1", "RequestReceived"),
            event("authored-audit-1", "ResponseComplete", stamp="2026-09-20T10:00:00.250000Z",
                  object_ref=CONFIGMAP, response={"metadata": {}, "status": "Success", "code": 201}),
        ]) + "\n")
        common = self.observations(first)
        self.assertEqual(common["main_rows"], 2)
        fields = common["observations"][1]["fields"]
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(common["observations"][1]["t_ms"], 1_789_898_400_250)
        self.assertEqual(fields["audit_stage"], "ResponseComplete")
        self.assertEqual(fields["audit_response_code"], 201)
        self.assertEqual(fields["audit_verb"], "create")
        self.assertEqual(fields["audit_object_resource"], "configmaps")
        self.assertIn("not_business_acceptance", common["profile_units"]["audit_outcome_basis"])
        self.assertIn("never_a_duration", common["profile_units"]["record_time_us"])

        _, report, second = self.rows_of("\n".join([
            event("authored-audit-6", "RequestReceived", received="2026-09-20T12:00:00.000000Z",
                  verb="watch", uri="/api/v1/pods?watch=true"),
        ]) + "\n", time_field="request_received_timestamp")
        variation = self.observations(second)
        self.assertEqual(variation["main_rows"], 1)
        self.assertEqual(variation["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(variation["observations"][0]["fields"]["record_time_basis"],
                         "APISERVER_REPORTED_REQUEST_RECEIVED_TIMESTAMP")
        self.assertEqual(report["audit_ids_without_response_complete"], 1)


    def test_the_declared_settings_are_required_and_checked(self):
        text = event("authored-audit-1", "ResponseComplete") + "\n"
        self.rows_of(text)
        for kwargs in ({"source_format": "k8s-audit-v1beta1-jsonl"}, {"source_format": ""},
                       {"time_field": "creationTimestamp"}, {"time_field": ""}):
            with self.assertRaises(ValueError):
                self.rows_of(text, **kwargs)

    def test_malformed_records_are_refused_rather_than_guessed(self):
        cases = {
            "not an object": '["authored"]',
            "wrong api version": event("a", "ResponseComplete").replace("audit.k8s.io/v1",
                                                                       "audit.k8s.io/v1beta1"),
            "wrong kind": event("a", "ResponseComplete").replace('"kind": "Event"', '"kind": "Policy"'),
            "missing verb": json.dumps({k: v for k, v in
                                        json.loads(event("a", "ResponseComplete")).items() if k != "verb"}),
            "empty audit id": event("", "ResponseComplete"),
            "stage not a string": event("a", "ResponseComplete").replace('"stage": "ResponseComplete"',
                                                                         '"stage": 3'),
            "duplicate json key": '{"kind": "Event", "kind": "Event"}',
            "millisecond fraction": event("a", "ResponseComplete", received="2026-09-20T10:00:00.000Z"),
            "no fraction": event("a", "ResponseComplete", received="2026-09-20T10:00:00Z"),
            "numeric offset": event("a", "ResponseComplete",
                                    received="2026-09-20T10:00:00.000000+09:00"),
            "leap second": event("a", "ResponseComplete", received="2026-09-20T23:59:60.000000Z"),
            "impossible month": event("a", "ResponseComplete", received="2026-13-20T10:00:00.000000Z"),
            "timestamp not a string": event("a", "ResponseComplete").replace(
                '"stageTimestamp": "2026-09-20T10:00:00.000000Z"', '"stageTimestamp": 1789020000'),
            "null declared clock": event("a", "ResponseComplete").replace(
                '"stageTimestamp": "2026-09-20T10:00:00.000000Z"', '"stageTimestamp": null'),
            "user not an object": event("a", "ResponseComplete").replace(
                '"user": {"username": "authored-user", "groups": ["authored-group"]}', '"user": "authored"'),
            "objectRef not an object": event("a", "ResponseComplete", object_ref="configmaps"),
            "code not an integer": event("a", "ResponseComplete",
                                         response={"metadata": {}, "code": "201"}),
            "response status not an object": event("a", "ResponseComplete", response="Success"),
            "annotations not a string map": event("a", "ResponseComplete",
                                                  annotations={"authored": {"nested": "value"}}),
            "truncated json": event("a", "ResponseComplete")[:-4],
            "empty export": "",
        }
        for name, line in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.rows_of(line if name == "empty export" else line + "\n")

    def test_unknown_stage_and_level_values_keep_their_text(self):
        text = "\n".join([
            event("authored-audit-7", "ResponseFlushed", level="RequestMetadata",
                  stamp="2026-09-20T13:00:00.000000Z"),
            event("authored-audit-7", "ResponseComplete", stamp="2026-09-20T13:00:01.000000Z"),
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertEqual(rows[0]["audit_stage"], "ResponseFlushed", "the value is kept exactly")
        self.assertEqual(rows[0]["audit_stage_status"], "UNSUPPORTED_STAGE_VALUE_RETAINED")
        self.assertEqual(rows[0]["audit_level"], "RequestMetadata")
        self.assertEqual(rows[0]["audit_level_status"], "UNSUPPORTED_LEVEL_VALUE_RETAINED")
        self.assertEqual((report["unsupported_stage_records"], report["unsupported_level_records"]), (1, 1))
        self.assertEqual(report["stage_counts"], {"RequestReceived": 0, "ResponseStarted": 0,
                                                  "ResponseComplete": 1, "Panic": 0})
        self.assertEqual(rows[1]["audit_stage_status"], "SUPPORTED_PINNED_STAGE")

    def test_a_repeated_audit_id_and_stage_is_refused(self):
        line = event("authored-audit-8", "ResponseComplete")
        self.rows_of(line + "\n")
        with self.assertRaises(ValueError):
            self.rows_of(line + "\n" + line + "\n")
        rows, report, _ = self.rows_of(
            line + "\n" + event("authored-audit-8", "RequestReceived") + "\n")
        self.assertEqual((len(rows), report["audit_ids"]), (2, 1))

    def test_unknown_fields_are_counted_and_named_without_values(self):
        text = event("authored-audit-9", "ResponseComplete", authoredFutureField={"kept": [1, None]},
                     anotherAuthoredField="authored-value") + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertIn("authoredFutureField", rows[0]["unknown_field_names"])
        self.assertIn("anotherAuthoredField", rows[0]["unknown_field_names"])
        self.assertEqual(int(rows[0]["unknown_field_count"]), report["unknown_fields_total"])
        self.assertNotIn("authored-value", ",".join(rows[0].values()))

    def test_the_declared_redaction_is_accounted_not_a_silent_drop(self):
        text = event("authored-audit-10", "ResponseComplete", object_ref=CONFIGMAP) + "\n"
        plain, _, _ = self.rows_of(text)
        rows, report, _ = self.rows_of(text, redact=True)
        self.assertEqual(rows[0]["audit_id_hex"], "REDACTED_BY_DECLARED_OPTION")
        self.assertEqual(rows[0]["audit_request_uri_hex"], "REDACTED_BY_DECLARED_OPTION")
        self.assertEqual(report["redacted_fields"], 7)
        for column in ("audit_stage", "audit_verb", "audit_response_code", "record_time_us",
                       "audit_object_resource", "record_sha256"):
            self.assertEqual(rows[0][column], plain[0][column])


    def test_the_cli_writes_the_pair_and_keeps_records_out_of_the_accounting(self):
        text = "\n".join([event("authored-audit-11", "RequestReceived"),
                          event("authored-audit-11", "ResponseComplete",
                                stamp="2026-09-20T10:00:00.900000Z", object_ref=CONFIGMAP,
                                response={"metadata": {}, "status": "Success", "code": 200})]) + "\n"
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = work / "audit.log"
            source.write_bytes(text.encode())
            target, report_path = work / "observations.csv", work / "report.json"
            command = [os.sys.executable, str(SCRIPT), str(source), str(target),
                       "--source-format", SOURCE_FORMAT, "--time-field", "stage_timestamp",
                       "--report", str(report_path)]
            done = subprocess.run(command, check=True, capture_output=True)
            for text_out in (done.stderr.decode(), report_path.read_text()):
                report = json.loads(text_out.strip())
                self.assertEqual((report["records"], report["rows"]), (2, 2))
                self.assertEqual(report["source_pin"], "kubernetes-v1.32.0-70d3cc98")
                self.assertEqual(report["source_sha256"],
                                 "sha256:" + hashlib.sha256(text.encode()).hexdigest())
                self.assertNotIn("authored-audit", text_out)
                self.assertNotIn("/api/v1", text_out)
                self.assertNotIn("authored-user", text_out)
            self.assertEqual(done.stdout, b"")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            same = work / "same.json"
            refused = subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(same),
                 "--source-format", SOURCE_FORMAT, "--time-field", "stage_timestamp",
                 "--report", str(same)], capture_output=True)
            self.assertEqual(refused.returncode, 2)
            self.assertFalse(same.exists())


    def test_a_reference_retained_nested_value_is_retrievable_through_the_row_binding(self):
        """Names in the row plus the saved input are enough to read a value the importer never copied."""
        text = "\n".join([
            event("authored-audit-20", "RequestReceived"),
            json.dumps(dict(
                json.loads(event("authored-audit-21", "ResponseComplete",
                                 stamp="2026-09-20T10:00:01.000000Z",
                                 object_ref={"resource": "configmaps", "name": "authored-config",
                                             "resourceVersion": "authored-rv-99"},
                                 response={"metadata": {}, "status": "Failure", "code": 409,
                                           "message": "authored conflict detail",
                                           "details": {"name": "authored-config"}},
                                 authoredTopLevel={"nested": {"deep": "authored-deep-value"}})),
                user={"username": "authored-user", "uid": "authored-uid-42",
                      "groups": ["authored-group-a", "authored-group-b"],
                      "extra": {"authored-key": ["authored-extra-value"]}})),
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        row = rows[1]
        self.assertEqual(row["nested_uninterpreted_names"],
                         "user.extra;user.groups;user.uid;objectRef.resourceVersion;"
                         "responseStatus.details;responseStatus.message;responseStatus.metadata")
        self.assertEqual(row["nested_uninterpreted_count"], "7")
        self.assertEqual(row["unknown_field_names"], "authoredTopLevel;sourceIPs")
        self.assertEqual((report["nested_uninterpreted_total"], report["unknown_fields_total"]), (8, 3))
        joined = ",".join(row.values())
        for value in ("authored-uid-42", "authored-group-a", "authored-rv-99", "authored conflict detail",
                      "authored-deep-value", "203.0.113.7"):
            self.assertNotIn(value, joined, "a reference-retained value must not appear in a cell")
        self.assertIn("RETAINED_BY_REFERENCE", row["retention_basis"])

        self.assertEqual(row["source_file_sha256"],
                         "sha256:" + hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(row["source_record_index"], "2")
        with tempfile.TemporaryDirectory() as work:
            saved = Path(work) / "audit.log"
            saved.write_bytes(text.encode())
            data = saved.read_bytes()
            self.assertEqual(row["source_file_sha256"], "sha256:" + hashlib.sha256(data).hexdigest(),
                             "the file digest identifies which saved export this row came from")
            line = data.decode().splitlines()[int(row["source_record_index"]) - 1]
            self.assertEqual(row["record_sha256"], "sha256:" + hashlib.sha256(line.encode()).hexdigest(),
                             "the record digest confirms the addressed line is the row's own record")
            self.assertEqual(row["record_bytes"], str(len(line.encode())))
            record = json.loads(line)
            self.assertEqual(record["user"]["uid"], "authored-uid-42")
            self.assertEqual(record["user"]["groups"], ["authored-group-a", "authored-group-b"])
            self.assertEqual(record["user"]["extra"], {"authored-key": ["authored-extra-value"]})
            self.assertEqual(record["objectRef"]["resourceVersion"], "authored-rv-99")
            self.assertEqual(record["responseStatus"]["message"], "authored conflict detail")
            self.assertEqual(record["authoredTopLevel"]["nested"]["deep"], "authored-deep-value")
            self.assertEqual(record["sourceIPs"], ["203.0.113.7"])
            self.assertEqual(row["audit_response_code"], "409")
            self.assertEqual(self.text_of(row["audit_user_name_hex"]), "authored-user")

    def test_withholding_is_counted_and_the_body_size_is_labelled_as_a_reserialization(self):
        body = {"kind": "ConfigMap", "data": {"authored-key": "authored-body-value"}}
        text = event("authored-audit-22", "ResponseComplete", level="RequestResponse",
                     requestObject=body,
                     annotations={"authorization.k8s.io/decision": "allow",
                                  "authored.example/note": "authored annotation value"}) + "\n"
        rows, report, _ = self.rows_of(text)
        row = rows[0]
        self.assertEqual(row["request_body_status"], "PRESENT_WITHHELD_BY_THIS_IMPORTER")
        self.assertEqual(row["response_body_status"], "ABSENT")
        self.assertEqual(row["response_body_reserialized_json_bytes"], "")
        self.assertEqual(row["request_body_reserialized_json_bytes"],
                         str(len(json.dumps(body, separators=(",", ":")).encode())))
        self.assertEqual((report["withheld_bodies"], report["withheld_annotation_values"]), (1, 2))
        self.assertEqual(row["audit_annotation_keys"],
                         "authored.example/note;authorization.k8s.io/decision")
        for text_out in (",".join(row.values()), json.dumps(report)):
            self.assertNotIn("authored-body-value", text_out)
            self.assertNotIn("authored annotation value", text_out)
            self.assertNotIn("allow", text_out)


    def batch_rows(self, body, **kwargs):
        """The same conversion, declared with the saved-body container instead of the line container."""
        kwargs["source_format"] = EVENT_LIST_FORMAT
        return self.rows_of(body, **kwargs)

    def test_a_saved_event_list_body_yields_the_same_rows_as_the_line_container(self):
        """No manual splitting or reformatting: the envelope is unwrapped into the same event path."""
        events = (event("authored-audit-30", "RequestReceived", object_ref=CONFIGMAP),
                  event("authored-audit-30", "ResponseComplete", stamp="2026-09-20T10:00:00.250000Z",
                        object_ref=CONFIGMAP,
                        response={"metadata": {}, "status": "Success", "code": 201}),
                  event("authored-audit-31", "ResponseComplete", received="2026-09-20T10:00:05.500000Z",
                        stamp="2026-09-20T10:00:05.750000Z", verb="delete",
                        response={"metadata": {}, "status": "Failure", "reason": "Forbidden", "code": 403}))
        body = event_list(*events, metadata={})
        rows, report, output = self.batch_rows(body)
        line_rows, line_report, _ = self.rows_of("\n".join(events) + "\n")
        provenance = {"source_container", "source_file_sha256", "source_item_pointer", "record_sha256",
                      "record_bytes", "record_digest_scope"}
        self.assertEqual([{k: v for k, v in row.items() if k not in provenance} for row in rows],
                         [{k: v for k, v in row.items() if k not in provenance} for row in line_rows])
        self.assertEqual((report["records"], report["rows"], report["audit_ids"]), (3, 3, 2))
        self.assertEqual(report["envelope_items_declared"], 3)
        self.assertEqual(report["items_selected"], 3)
        self.assertTrue(report["envelope_metadata_present"])
        self.assertEqual(report["envelope_unknown_field_names"], [])
        self.assertIn("AUDIT_WEBHOOK_VERSION", report["envelope_version_basis"])
        self.assertEqual({r["source_container"] for r in rows},
                         {"EVENTLIST_REQUEST_BODY_SAVED_BY_AN_ENGINEER"})
        self.assertEqual([r["source_item_pointer"] for r in rows], ["/items/0", "/items/1", "/items/2"])
        self.assertEqual([r["audit_stage"] for r in rows],
                         ["RequestReceived", "ResponseComplete", "ResponseComplete"])
        self.assertEqual(report["audit_ids_without_response_complete"],
                         line_report["audit_ids_without_response_complete"])
        return output

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_a_saved_event_list_body_reaches_common_observations(self):
        _, _, output = self.batch_rows(event_list(
            event("authored-audit-32", "RequestReceived", object_ref=CONFIGMAP),
            event("authored-audit-32", "ResponseComplete", stamp="2026-09-20T10:00:00.250000Z",
                  object_ref=CONFIGMAP, response={"metadata": {}, "status": "Success", "code": 201})))
        common = self.observations(output)
        self.assertEqual(common["main_rows"], 2)
        fields = common["observations"][1]["fields"]
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(fields["audit_stage"], "ResponseComplete")
        self.assertEqual(fields["audit_response_code"], 201)
        self.assertEqual(fields["source_item_pointer"], "/items/1")
        self.assertIn("not_the_original_body_bytes",
                      common["profile_units"]["record_digest_scope"].lower())

    def test_a_second_batch_reuses_the_absent_response_and_unknown_field_handling_unchanged(self):
        body = event_list(
            event("authored-audit-33", "RequestReceived", verb="watch", uri="/api/v1/pods?watch=true"),
            event("authored-audit-33", "ResponseStarted", stamp="2026-09-20T11:00:00.100000Z",
                  verb="watch", uri="/api/v1/pods?watch=true",
                  response={"metadata": {}, "code": 200}),
            event("authored-audit-34", "ResponseComplete", stamp="2026-09-20T11:05:00.000000Z",
                  authoredFutureField={"nested": "authored-item-value"}),
            authoredEnvelopeField={"authored": "envelope-value"})
        rows, report, _ = self.batch_rows(body)
        watch = [r for r in rows if self.text_of(r["audit_id_hex"]) == "authored-audit-33"]
        self.assertEqual([r["audit_id_has_response_complete"] for r in watch],
                         ["NO_RESPONSE_COMPLETE_IN_THIS_FILE"] * 2)
        self.assertEqual(report["audit_ids_without_response_complete"], 1)
        self.assertEqual(rows[0]["audit_outcome_status"], "ABSENT_NO_RESPONSE_STATUS_IN_THIS_RECORD")
        self.assertIn("authoredFutureField", rows[2]["unknown_field_names"])
        self.assertEqual(report["envelope_unknown_field_names"], ["authoredEnvelopeField"])
        joined = ",".join(",".join(row.values()) for row in rows)
        for value in ("authored-item-value", "envelope-value"):
            self.assertNotIn(value, joined)

    def test_an_empty_batch_is_a_truthful_zero_and_not_a_conversion(self):
        for name, body in (("empty list", event_list()),
                           ("null items", json.dumps({"kind": "EventList",
                                                      "apiVersion": "audit.k8s.io/v1", "items": None}))):
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    self.batch_rows(body)
                message = str(caught.exception)
                self.assertIn("zero", message)
                self.assertIn("not a successful conversion", message)

    def test_a_wrong_envelope_or_item_is_rejected(self):
        good = event("authored-audit-35", "ResponseComplete")
        cases = {
            "bare event through the batch selector": good,
            "wrong kind": event_list(good).replace('"kind": "EventList"', '"kind": "Policy"'),
            "wrong api version": event_list(good).replace("audit.k8s.io/v1", "audit.k8s.io/v1beta1"),
            "missing items": json.dumps({"kind": "EventList", "apiVersion": "audit.k8s.io/v1"}),
            "items not a list": json.dumps({"kind": "EventList", "apiVersion": "audit.k8s.io/v1",
                                            "items": {"0": json.loads(good)}}),
            "item not an object": json.dumps({"kind": "EventList", "apiVersion": "audit.k8s.io/v1",
                                              "items": ["authored"]}),
            "item missing a required field": json.dumps(
                {"kind": "EventList", "apiVersion": "audit.k8s.io/v1",
                 "items": [{k: v for k, v in json.loads(good).items() if k != "verb"}]}),
            "item with a malformed timestamp": event_list(
                event("authored-audit-36", "ResponseComplete", received="2026-09-20T10:00:00Z")),
            "duplicate json key": '{"kind": "EventList", "kind": "EventList"}',
            "jsonl handed to the batch selector": good + "\n" + good,
            "truncated body": event_list(good)[:-3],
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.batch_rows(body)
        with self.assertRaises(ValueError):
            self.rows_of(event_list(good) + "\n")

    def test_an_item_is_addressed_by_whole_file_hash_and_json_pointer(self):
        body = event_list(
            event("authored-audit-37", "ResponseComplete", object_ref=CONFIGMAP,
                  authoredFutureField={"nested": {"deep": "authored-batch-deep-value"}}))
        rows, _, _ = self.batch_rows(body)
        row = rows[0]
        self.assertEqual(row["record_digest_scope"],
                         "CANONICAL_RESERIALISED_ITEM_JSON_SORTED_KEYS_COMPACT_SEPARATORS_"
                         "NOT_THE_ORIGINAL_REQUEST_BODY_BYTES")
        with tempfile.TemporaryDirectory() as work:
            saved = Path(work) / "webhook-body.json"
            saved.write_bytes(body.encode())
            data = saved.read_bytes()
            self.assertEqual(row["source_file_sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
            self.assertEqual(row["source_item_pointer"], "/items/0")
            item = json.loads(data)["items"][int(row["source_item_pointer"].rsplit("/", 1)[1])]
            self.assertEqual(item["authoredFutureField"]["nested"]["deep"], "authored-batch-deep-value")
            self.assertNotIn("authored-batch-deep-value", ",".join(row.values()))
            canonical = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(row["record_sha256"], "sha256:" + hashlib.sha256(canonical).hexdigest())
            self.assertEqual(row["record_bytes"], str(len(canonical)))
            self.assertNotEqual(row["record_sha256"], row["source_file_sha256"])

    def test_the_cli_accepts_the_batch_container_and_reports_the_envelope(self):
        body = event_list(event("authored-audit-38", "ResponseComplete", object_ref=CONFIGMAP))
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = work / "webhook-body.json"
            source.write_bytes(body.encode())
            target, report_path = work / "observations.csv", work / "report.json"
            done = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                                   "--source-format", EVENT_LIST_FORMAT, "--time-field", "stage_timestamp",
                                   "--report", str(report_path)], check=True, capture_output=True)
            report = json.loads(done.stderr.decode())
            self.assertEqual(report["source_container"], "EVENTLIST_REQUEST_BODY_SAVED_BY_AN_ENGINEER")
            self.assertEqual((report["envelope_items_declared"], report["items_selected"]), (1, 1))
            self.assertNotIn("authored-audit", done.stderr.decode())
            self.assertEqual(json.loads(report_path.read_text())["source_sha256"],
                             "sha256:" + hashlib.sha256(body.encode()).hexdigest())

    def test_a_writer_shaped_item_without_type_meta_is_accepted_from_the_envelope(self):
        """Items of a typed list carry no apiVersion/kind; requiring them would refuse ordinary output."""
        body = event_list(
            writer_shaped_item("authored-audit-40", "RequestReceived", object_ref=CONFIGMAP),
            writer_shaped_item("authored-audit-40", "ResponseComplete",
                               stamp="2026-09-20T10:00:00.250000Z", object_ref=CONFIGMAP,
                               response={"metadata": {}, "status": "Success", "code": 201}),
            event("authored-audit-41", "ResponseComplete", stamp="2026-09-20T10:00:01.000000Z"))
        rows, report, _ = self.batch_rows(body)
        self.assertEqual((report["records"], report["rows"]), (3, 3))
        self.assertEqual(report["items_without_type_meta"], 2)
        self.assertEqual([r["record_type_identity_basis"] for r in rows],
                         ["ENVELOPE_DERIVED_TYPE_META_THIS_ITEM_CARRIED_NONE_WHICH_IS_THIS_WRITERS_NORMAL_OUTPUT",
                          "ENVELOPE_DERIVED_TYPE_META_THIS_ITEM_CARRIED_NONE_WHICH_IS_THIS_WRITERS_NORMAL_OUTPUT",
                          "ITEM_REPORTED_TYPE_META_AGREEING_WITH_THE_DECLARED_PINNED_PAIR"])
        self.assertEqual({r["api_version_reported"] for r in rows}, {"audit.k8s.io/v1"})
        self.assertEqual([r["audit_stage"] for r in rows],
                         ["RequestReceived", "ResponseComplete", "ResponseComplete"])
        self.assertEqual(rows[1]["audit_response_code"], "201")
        item = json.loads(body)["items"][0]
        self.assertNotIn("kind", item)
        canonical = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(rows[0]["record_sha256"], "sha256:" + hashlib.sha256(canonical).hexdigest())
        self.assertEqual(rows[0]["record_bytes"], str(len(canonical)))
        with self.assertRaises(ValueError):
            self.rows_of(writer_shaped_item("authored-audit-42", "ResponseComplete") + "\n")

    def test_an_item_type_conflicting_with_the_envelope_is_refused(self):
        good = json.loads(event("authored-audit-43", "ResponseComplete"))
        cases = {}
        for name, patch in (("wrong item kind", {"kind": "Policy"}),
                            ("wrong item version", {"apiVersion": "audit.k8s.io/v1beta1"}),
                            ("empty item kind", {"kind": ""}),
                            ("empty item version", {"apiVersion": ""}),
                            ("kind only, wrong", {"kind": "EventList"})):
            item = dict(good)
            item.update(patch)
            cases[name] = json.dumps({"kind": "EventList", "apiVersion": "audit.k8s.io/v1",
                                      "items": [item]})
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.batch_rows(body)
        agreeing = dict(good)
        del agreeing["apiVersion"]
        rows, _, _ = self.batch_rows(json.dumps({"kind": "EventList", "apiVersion": "audit.k8s.io/v1",
                                                 "items": [agreeing]}))
        self.assertEqual(rows[0]["record_type_identity_basis"],
                         "ITEM_REPORTED_TYPE_META_AGREEING_WITH_THE_DECLARED_PINNED_PAIR")

    def test_a_practical_export_size_stays_inside_the_bounds(self):
        text = "".join(event(f"authored-audit-{index}", "ResponseComplete",
                             received=f"2026-09-20T14:00:{index % 60:02d}.{index % 1000:06d}Z") + "\n"
                       for index in range(5000))
        rows, report, _ = self.rows_of(text)
        self.assertEqual((len(rows), report["rows"], report["audit_ids"]), (5000, 5000, 5000))
        self.assertEqual(report["audit_ids_without_response_complete"], 0)


if __name__ == "__main__":
    unittest.main()
