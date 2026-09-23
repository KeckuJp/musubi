#!/usr/bin/env python3
"""Saved Kubernetes `audit.k8s.io/v1` audit events (JSONL) -> CSV for TelemetryCsvReader.

Passive import of audit records an engineer already saved. No cluster, no kubeconfig, no API request, no
authentication and no policy decision: one row is one **reported audit stage event**, never a merged
operation and never a verdict.

Schema read at Kubernetes tag `v1.32.0` (`70d3cc986aa8221cd1dfb1121852688902d3bf53`):

* `staging/src/k8s.io/apiserver/pkg/apis/audit/v1/types.go` - `Event` with required `level`, `auditID`,
  `stage`, `requestURI`, `verb`, `user`, optional `objectRef`, `responseStatus`, `requestObject`,
  `responseObject`, `annotations`, `userAgent`, `sourceIPs`, `impersonatedUser`; the four `Stage` values and
  the four `Level` values; `userAgent` "must not be trusted" and for `sourceIPs` "all but the last IP can be
  arbitrarily set by the client".
* `register.go` - `GroupName = "audit.k8s.io"`, version `v1`, so records carry `apiVersion: audit.k8s.io/v1`.
* `plugin/pkg/audit/log/backend.go` with `runtime/serializer/json/json.go` - the `json` format encodes each
  event through `json.NewEncoder(w).Encode(obj)`, which terminates every object with a newline: one JSON
  object per line.
* `apimachinery/pkg/apis/meta/v1/micro_time.go` - both timestamps are `MicroTime`, marshalled as
  `t.UTC().Format("2006-01-02T15:04:05.000000Z07:00")`: a fixed six-digit fraction, always UTC, and `null`
  when zero.
* `apimachinery/pkg/apis/meta/v1/types.go` - `Status` carries `status`, `message`, `reason`, `code`;
  `StatusSuccess = "Success"`, `StatusFailure = "Failure"`.

Deliberately not claimed: `ResponseComplete` is defined there as "the response body has been completed, and
no more bytes will be sent", and nothing in that source calls a stage or an HTTP code business acceptance. A
request whose file holds no `ResponseComplete` row is counted and named, never dropped and never called
success. The two timestamps are kept apart: no subtraction, no latency, no duration.

Two saved containers, one set of event meanings. `k8s-audit-v1-jsonl` is the log backend's file, one
`Event` object per line. `k8s-audit-v1-eventlist-json` is a **saved webhook request body**: the webhook
backend builds an `EventList` (`plugin/pkg/audit/webhook/webhook.go`, `list.Items = append(list.Items, *e)`,
then `RestClient.Post().Body(&list)`) whose `Items []Event` field has no `omitempty`, and whose kind and
group version come from the same registered scheme. This importer only **unwraps that envelope** and runs
the identical per-event validation and mapping: it is not an HTTP client, it validates no signature, it says
nothing about delivery, retry or reliability, and it never splits or reformats the body by hand. A saved
body's items keep their order and each stage stays its own row.

An item of that typed list carries **no `apiVersion`/`kind` of its own** in ordinary output: the versioning
codec stamps the group/version/kind on the **outer** object only (`serializer/versioning/versioning.go`),
`EventList` implements no nested-object encoder, the generated conversion for an `Event` never touches its
`TypeMeta`, and `metav1.TypeMeta` has `omitempty` on both fields. So inner `TypeMeta` is **optional** for this
container - absent means envelope-derived, stated per row in `record_type_identity_basis` - while any type an
item *does* declare must agree with the already validated envelope or the item is refused. The JSONL
container is unchanged: a line record is self-describing and must declare the pinned pair itself.

Because an item inside one JSON body has no raw bytes of its own, its `record_sha256` is taken over a
**canonical re-serialisation** (sorted keys, compact separators) and `record_digest_scope` says exactly that:
it is **not** the original request body's bytes. The original is addressed by `source_file_sha256` over the
whole saved file plus the item's JSON pointer in `source_item_pointer`. The JSONL container keeps its raw
line binding unchanged.

Three retention classes, kept distinct and stated in every row's `retention_basis`:

* **selected and copied** - the fields this importer interprets: `auditID`, `stage`, `level`, `verb`,
  `requestURI`, `user.username`, `userAgent`, the seven `objectRef` names, `responseStatus.code/status/reason`,
  both timestamps, annotation **key names**, and the counts below;
* **retained by reference only** - unknown top-level fields and uninterpreted sub-fields of `user`,
  `objectRef` and `responseStatus` (for example `user.uid`, `user.groups`, `objectRef.resourceVersion`,
  `responseStatus.message`), plus `sourceIPs` and `impersonatedUser`. Their **names** appear in the row;
  their values do not. They are recoverable only from the original input, addressed by the row's
  `source_file_sha256` + `source_record_index` + `record_sha256`. **Naming a key is not preserving a value**,
  so the saved input must be kept beside the CSV for any later adaptation;
* **withheld by design** - `requestObject`/`responseObject` values and annotation values. No body value is
  copied into the CSV, the report or any diagnostic. No separate body-only digest is emitted, but
  `record_sha256` and `source_file_sha256` are taken over bytes that include those objects, so they are not a
  blanket no-oracle guarantee.
"""
import argparse
import csv
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

try:
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
MAX_RECORDS = 200_000
# Audit records carry URIs, user info and annotations, so they are larger than a journal line.
MAX_LINE = 256 * 1024
SOURCE_FORMAT = "k8s-audit-v1-jsonl"
# The webhook backend builds an `EventList` and POSTs it, so a saved request body is one JSON object with
# `items`. Both containers carry the same `Event` items and are read by the same per-event code below.
EVENT_LIST_FORMAT = "k8s-audit-v1-eventlist-json"
SOURCE_FORMATS = (SOURCE_FORMAT, EVENT_LIST_FORMAT)
LIST_KIND = "EventList"
# `pkg/util/webhook/webhook.go` sets the client's serializer to `codecFactory.LegacyCodec(groupVersions...)`,
# and `pkg/server/options/audit.go` supplies that group version from `--audit-webhook-version`, whose default
# is `audit.k8s.io/v1`. So the envelope's apiVersion/kind are written by that codec at the version the
# operator declared - which is why this importer requires the pinned pair in the saved body instead of
# assuming every deployment emitted it.
ENVELOPE_VERSION_BASIS = ("ENVELOPE_ENCODED_BY_THE_AUDIT_LEGACY_CODEC_AT_THE_OPERATOR_DECLARED_"
                          "AUDIT_WEBHOOK_VERSION_DEFAULT_AUDIT_K8S_IO_V1_REQUIRED_HERE_NOT_ASSUMED")
# A typed list's identity is on the envelope: `versioning.go`'s codec stamps the GVK on the outer object
# only, `EventList` implements no nested-object encoder, the generated conversion never touches an item's
# `TypeMeta`, and `metav1.TypeMeta` carries `omitempty` on both fields - so ordinary items carry no
# `apiVersion`/`kind` at all. Requiring them per item would refuse this writer's own output.
ITEM_TYPE_REPORTED = "ITEM_REPORTED_TYPE_META_AGREEING_WITH_THE_DECLARED_PINNED_PAIR"
ITEM_TYPE_ENVELOPE = "ENVELOPE_DERIVED_TYPE_META_THIS_ITEM_CARRIED_NONE_WHICH_IS_THIS_WRITERS_NORMAL_OUTPUT"
CONTAINER_JSONL = "JSONL_ONE_EVENT_OBJECT_PER_LINE"
CONTAINER_EVENT_LIST = "EVENTLIST_REQUEST_BODY_SAVED_BY_AN_ENGINEER"
# Two different digest scopes, never conflated: a JSONL record has its own raw bytes, while an item inside
# one JSON body does not, so its digest is over a canonical re-serialisation and says so.
SCOPE_RAW_LINE = "RAW_SOURCE_LINE_BYTES_EXCLUDING_THE_LINE_TERMINATOR"
SCOPE_CANONICAL_ITEM = ("CANONICAL_RESERIALISED_ITEM_JSON_SORTED_KEYS_COMPACT_SEPARATORS_"
                        "NOT_THE_ORIGINAL_REQUEST_BODY_BYTES")
SOURCE_PIN = "kubernetes-v1.32.0-70d3cc98"
API_VERSION = "audit.k8s.io/v1"
KIND = "Event"
# `types.go` declares exactly these four stages and four levels at the pin. An unlisted value keeps its text
# and is marked unsupported; it is never mapped onto one of these.
STAGES = ("RequestReceived", "ResponseStarted", "ResponseComplete", "Panic")
LEVELS = ("None", "Metadata", "Request", "RequestResponse")
COMPLETION_STAGE = "ResponseComplete"
SUPPORTED_STAGE = "SUPPORTED_PINNED_STAGE"
UNSUPPORTED_STAGE = "UNSUPPORTED_STAGE_VALUE_RETAINED"
SUPPORTED_LEVEL = "SUPPORTED_PINNED_LEVEL"
UNSUPPORTED_LEVEL = "UNSUPPORTED_LEVEL_VALUE_RETAINED"
OUTCOME_REPORTED = "REPORTED_RESPONSE_STATUS"
OUTCOME_ABSENT = "ABSENT_NO_RESPONSE_STATUS_IN_THIS_RECORD"
OUTCOME_BASIS = ("APISERVER_REPORTED_HTTP_STATUS_AND_STAGE_ONLY_NOT_BUSINESS_ACCEPTANCE_"
                 "RESPONSE_COMPLETE_MEANS_THE_RESPONSE_BODY_ENDED")
IDENTITY_BASIS = ("REPORTED_AUDIT_FIELDS_NOT_AUTHENTICATED_USER_AGENT_UNTRUSTED_AND_ALL_BUT_THE_LAST_"
                  "SOURCE_IP_CLIENT_SETTABLE")
BODY_ABSENT = "ABSENT"
BODY_WITHHELD = "PRESENT_WITHHELD_BY_THIS_IMPORTER"
# What the three retention classes mean, stated in every row so a name-only cell is never mistaken for a
# copied value.
RETENTION_BASIS = ("SELECTED_FIELDS_COPIED_UNINTERPRETED_AND_UNKNOWN_FIELDS_RETAINED_BY_REFERENCE_ONLY_"
                   "VIA_SOURCE_FILE_SHA256_PLUS_RECORD_INDEX_PLUS_RECORD_SHA256_SO_THE_ORIGINAL_INPUT_"
                   "MUST_BE_KEPT_BESIDE_THIS_CSV_AND_BODY_VALUES_ARE_WITHHELD_BY_DESIGN")
REDACTED = "REDACTED_BY_DECLARED_OPTION"
# `MicroTime` marshals with a fixed six-digit fraction in UTC; anything else is not this writer's output.
MICRO_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{6})Z\Z")
# declared selector -> (schema field, short basis name for the row, the full basis statement)
TIME_FIELDS = {
    "stage_timestamp": ("stageTimestamp", "APISERVER_REPORTED_STAGE_TIMESTAMP",
                        "STAGE_TIMESTAMP_WHEN_THE_REQUEST_REACHED_THIS_AUDIT_STAGE_APISERVER_WALL_CLOCK_"
                        "UTC_MICROSECONDS_NOT_VERIFIED_AND_NEVER_A_DURATION"),
    "request_received_timestamp": ("requestReceivedTimestamp", "APISERVER_REPORTED_REQUEST_RECEIVED_TIMESTAMP",
                                   "REQUEST_RECEIVED_TIMESTAMP_WHEN_THE_REQUEST_REACHED_THE_APISERVER_WALL_"
                                   "CLOCK_UTC_MICROSECONDS_NOT_VERIFIED_AND_NEVER_A_DURATION")}
REQUIRED = ("level", "auditID", "stage", "requestURI", "verb", "user")
OBJECT_REF = (("resource", "audit_object_resource"), ("subresource", "audit_object_subresource"),
              ("apiGroup", "audit_object_api_group"), ("apiVersion", "audit_object_api_version"),
              ("namespace", "audit_object_namespace_hex"), ("name", "audit_object_name_hex"),
              ("uid", "audit_object_uid_hex"))
# Free text goes out hex-transported so the reader cannot retype an identifier as a number.
HEX_COLUMNS = ("audit_id_hex", "audit_request_uri_hex", "audit_user_name_hex", "audit_user_agent_hex",
               "audit_object_namespace_hex", "audit_object_name_hex", "audit_object_uid_hex")
INTERPRETED = {"apiVersion", "kind", "level", "auditID", "stage", "requestURI", "verb", "user", "userAgent",
               "objectRef", "responseStatus", "requestObject", "responseObject", "annotations",
               "requestReceivedTimestamp", "stageTimestamp"}
MAX_KEYS_IN_CELL = 16
COLUMNS = ["record_time_us", "record_time_basis", "time_field_basis",
           "audit_stage_timestamp_us", "audit_request_received_timestamp_us",
           "audit_stage_timestamp_text", "audit_request_received_timestamp_text", "audit_time_status",
           "audit_id_hex", "audit_stage", "audit_stage_status", "audit_level", "audit_level_status",
           "audit_verb", "audit_request_uri_hex", "audit_user_name_hex", "audit_user_agent_hex",
           "audit_object_resource", "audit_object_subresource", "audit_object_api_group",
           "audit_object_api_version", "audit_object_namespace_hex", "audit_object_name_hex",
           "audit_object_uid_hex", "audit_object_ref_status",
           "audit_response_code", "audit_response_status_reported", "audit_response_reason",
           "audit_outcome_status", "audit_outcome_basis",
           "request_body_status", "request_body_reserialized_json_bytes",
           "response_body_status", "response_body_reserialized_json_bytes",
           "audit_annotation_count", "audit_annotation_keys",
           "audit_id_stage_count", "audit_id_stages_present", "audit_id_has_response_complete",
           "source_container", "source_file_sha256", "source_record_index", "source_item_pointer",
           "record_sha256", "record_bytes", "record_digest_scope",
           "record_type_identity_basis",
           "unknown_field_count", "unknown_field_names", "nested_uninterpreted_count",
           "nested_uninterpreted_names", "retention_basis", "identity_basis", "source_pin",
           "api_version_reported"]


def _hex(value):
    return "hex:" + value.encode().hex() if value else ""


def _string(record, name, index, *, required=False):
    if name not in record:
        if required:
            raise ValueError(f"record {index}: required field {name} is missing")
        return ""
    value = record[name]
    if not isinstance(value, str):
        raise ValueError(f"record {index}: {name} is not the schema's string")
    if required and not value:
        raise ValueError(f"record {index}: required field {name} is empty")
    return value


def _micro_time(record, name, index, *, required):
    """One `MicroTime` field: the fixed-fraction UTC text, or `null`/absent.

    The writer emits exactly `2006-01-02T15:04:05.000000Z07:00` in UTC, so a three-digit fraction, a
    numeric offset or a missing fraction is not this writer's output and is refused rather than guessed at.
    A zero time marshals to `null`; that is an absence, not an instant.
    """
    if name not in record or record[name] is None:
        if required:
            raise ValueError(f"record {index}: the declared clock needs {name}, which this record lacks")
        return None, ""
    value = record[name]
    if not isinstance(value, str):
        raise ValueError(f"record {index}: {name} is not the schema's timestamp string")
    match = MICRO_TIME.fullmatch(value)
    if not match:
        raise ValueError(f"record {index}: {name} is not the writer's RFC3339 microsecond UTC form")
    year, month, day, hour, minute, second, fraction = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 60):
        raise ValueError(f"record {index}: {name} is outside the calendar domain")
    try:
        instant = datetime.datetime(year, month, day, hour, minute, min(second, 59),
                                    tzinfo=datetime.timezone.utc)
    except ValueError as error:
        raise ValueError(f"record {index}: {name} is not a real instant ({error})")
    microseconds = int(instant.timestamp()) * 1_000_000 + fraction
    if second == 60:
        # A leap second is not representable as a POSIX instant; refuse rather than silently shift it.
        raise ValueError(f"record {index}: {name} carries second 60, which this importer will not reinterpret")
    if microseconds < 0:
        raise ValueError(f"record {index}: {name} precedes the epoch, which the reader's time cell cannot hold")
    return microseconds, value


def _bodies(record, index):
    """Account for request/response objects without copying any part of their content.

    No separate body-only digest is emitted. That is not a no-oracle guarantee: `record_sha256` and
    `source_file_sha256` are taken over bytes that include these objects, so anyone holding a candidate
    record can test it against those digests. The point here is only that this importer copies no body
    value into its own output.

    The reported size is the length of a **re-serialised compact JSON** rendering of the value, not the
    byte extent the body occupied in the source line, which this importer does not measure.
    """
    out = {}
    for name, prefix in (("requestObject", "request_body"), ("responseObject", "response_body")):
        if name not in record or record[name] is None:
            out[f"{prefix}_status"] = BODY_ABSENT
            out[f"{prefix}_reserialized_json_bytes"] = ""
            continue
        out[f"{prefix}_status"] = BODY_WITHHELD
        out[f"{prefix}_reserialized_json_bytes"] = len(
            json.dumps(record[name], separators=(",", ":")).encode())
    return out


def _nested_uninterpreted(record, index):
    """Path-qualified names of sub-fields this importer reads past inside the objects it does interpret.

    Only names leave here. The values stay in the original input and are reachable through the row's
    `source_file_sha256` + `source_record_index` + `record_sha256` binding, which is why that input has to
    be kept beside the CSV.
    """
    names = []
    for parent, consumed in (("user", ("username",)), ("objectRef", tuple(name for name, _ in OBJECT_REF)),
                             ("responseStatus", ("status", "reason", "code"))):
        value = record.get(parent)
        if isinstance(value, dict):
            names.extend(f"{parent}.{key}" for key in sorted(value) if key not in consumed)
    return names


def _canonical(item):
    """The one canonicalisation this importer uses for an item that has no raw bytes of its own."""
    return json.dumps(item, sort_keys=True, separators=(",", ":")).encode()


def _from_jsonl(text):
    """One `Event` object per line, as the log backend writes it: each record has its own raw bytes."""
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode()) > MAX_LINE:
            raise ValueError(f"record {index}: exceeds the line bound")
        try:
            record = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except json.JSONDecodeError as error:
            raise ValueError(f"record {index}: not one complete JSON object per line ({error.msg})")
        if not isinstance(record, dict):
            raise ValueError(f"record {index}: this writer emits one JSON object per line")
        yield index, "", record, line.encode(), SCOPE_RAW_LINE


def _from_event_list(text, report):
    """A saved webhook request body: one `EventList` object whose `items` are the same `Event` objects.

    This unwraps the envelope and nothing else. It is not an HTTP client, it validates no signature and it
    says nothing about delivery: the body was obtained earlier by an engineer and is read as a file. An item
    has no raw bytes of its own inside the body, so it is addressed by its JSON pointer and digested over a
    canonical re-serialisation, which the row labels as such.
    """
    try:
        body = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise ValueError(f"the saved body is not one complete JSON object ({error.msg})")
    if not isinstance(body, dict):
        raise ValueError("the saved body must be one EventList JSON object")
    if _string(body, "apiVersion", 0, required=True) != API_VERSION:
        raise ValueError(f"only an {API_VERSION} body is imported")
    if _string(body, "kind", 0, required=True) != LIST_KIND:
        raise ValueError(f"only a {LIST_KIND} body is imported; a bare {KIND} belongs to the JSONL format")
    if "items" not in body:
        # `items` has no omitempty in the pinned type, so its absence is a different input, not an empty
        # batch.
        raise ValueError("the body declares no items field")
    items = body["items"]
    if items is None:
        # `Items` is a Go slice with no omitempty, so an empty batch can reach JSON as `null` rather than
        # `[]`; which one a given encoder writes is not established, so both are read as zero items.
        raise ValueError("the body is a well-formed EventList whose items is null, so it declares zero "
                         "events; there is nothing to convert and this is not a successful conversion")
    if not isinstance(items, list):
        raise ValueError("items is not the schema's list")
    report["envelope_items_declared"] = len(items)
    report["envelope_metadata_present"] = "metadata" in body
    envelope_unknown = sorted(name for name in body if name not in {"apiVersion", "kind", "items",
                                                                    "metadata"})
    report["envelope_unknown_field_names"] = envelope_unknown
    if len(items) > MAX_RECORDS:
        raise ValueError("the body exceeds the item bound")
    if not items:
        # Truthful zero: a well-formed batch that carries nothing is not a conversion, and calling it one
        # would report data where there is none.
        raise ValueError("the body is a well-formed EventList that declares zero items; "
                         "there is nothing to convert and this is not a successful conversion")
    report["envelope_version_basis"] = ENVELOPE_VERSION_BASIS
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"item {index}: items must each be an Event object")
        yield index, f"/items/{index - 1}", item, _canonical(item), SCOPE_CANONICAL_ITEM


def convert(text, *, time_field, source_format=SOURCE_FORMAT, redact=False):
    if source_format not in SOURCE_FORMATS:
        raise ValueError("explicit supported source format required")
    if time_field not in TIME_FIELDS:
        raise ValueError("explicit supported time field required")
    clock_field, clock_name, clock_basis = TIME_FIELDS[time_field]
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized audit export")
    report = {"source_pin": SOURCE_PIN, "api_version": API_VERSION, "records": 0, "rows": 0,
              "time_field": clock_field, "record_time_basis": clock_name,
              "time_field_basis": clock_basis,
              "stage_counts": {stage: 0 for stage in STAGES}, "unsupported_stage_records": 0,
              "unsupported_level_records": 0, "audit_ids": 0, "audit_ids_without_response_complete": 0,
              "records_without_object_ref": 0, "withheld_bodies": 0, "redacted_fields": 0,
              "absent_other_timestamp_records": 0, "annotation_keys_total": 0, "unknown_fields_total": 0,
              "nested_uninterpreted_total": 0, "withheld_annotation_values": 0,
              "items_without_type_meta": 0,
              "retention_basis": RETENTION_BASIS,
              "source_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
              "outcome_basis": OUTCOME_BASIS, "identity_basis": IDENTITY_BASIS,
              "source_container": (CONTAINER_JSONL if source_format == SOURCE_FORMAT
                                   else CONTAINER_EVENT_LIST),
              "record_digest_scope": (SCOPE_RAW_LINE if source_format == SOURCE_FORMAT
                                      else SCOPE_CANONICAL_ITEM)}
    container = report["source_container"]
    parsed = []
    seen = set()
    stages_by_id = {}
    records = (_from_jsonl(text) if source_format == SOURCE_FORMAT else _from_event_list(text, report))
    for index, pointer, record, raw, scope in records:
        report["records"] += 1
        if report["records"] > MAX_RECORDS:
            raise ValueError("audit export exceeds the record bound")
        if source_format == SOURCE_FORMAT:
            # One object per line is self-describing: the line record must declare the pinned pair.
            if _string(record, "apiVersion", index, required=True) != API_VERSION:
                raise ValueError(f"record {index}: only {API_VERSION} records are imported")
            if _string(record, "kind", index, required=True) != KIND:
                raise ValueError(f"record {index}: only {KIND} records are imported")
            type_basis = ITEM_TYPE_REPORTED
        elif "apiVersion" not in record and "kind" not in record:
            # A typed list item inherits its identity from the envelope, which was already validated as
            # exactly `audit.k8s.io/v1` `EventList`. This is the pinned writer's normal shape.
            type_basis = ITEM_TYPE_ENVELOPE
            report["items_without_type_meta"] += 1
        else:
            # Whatever type an item does declare must agree with that envelope; a disagreement is refused
            # rather than silently preferring one of the two.
            if "apiVersion" in record and _string(record, "apiVersion", index) != API_VERSION:
                raise ValueError(f"item {index}: declares a type version that conflicts with the envelope")
            if "kind" in record and _string(record, "kind", index) != KIND:
                raise ValueError(f"item {index}: declares a kind that conflicts with the envelope")
            type_basis = ITEM_TYPE_REPORTED
        for name in REQUIRED:
            if name not in record:
                raise ValueError(f"record {index}: required field {name} is missing")
        audit_id = _string(record, "auditID", index, required=True)
        stage = _string(record, "stage", index, required=True)
        if (audit_id, stage) in seen:
            # One event exists per stage per request, so a repeat is a duplicated record, not a second
            # observation of the same stage.
            raise ValueError(f"record {index}: this auditID already carries a {stage} record")
        seen.add((audit_id, stage))
        stages_by_id.setdefault(audit_id, []).append(stage)
        parsed.append((index, pointer, record, raw, scope, audit_id, stage, type_basis))

    if not parsed:
        raise ValueError("no audit record was present")
    report["items_selected"] = len(parsed)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    report["audit_ids"] = len(stages_by_id)
    report["audit_ids_without_response_complete"] = sum(
        1 for stages in stages_by_id.values() if COMPLETION_STAGE not in stages)
    for index, pointer, record, raw, scope, audit_id, stage, type_basis in parsed:
        level = _string(record, "level", index, required=True)
        stage_supported = stage in STAGES
        level_supported = level in LEVELS
        if stage_supported:
            report["stage_counts"][stage] += 1
        else:
            report["unsupported_stage_records"] += 1
        if not level_supported:
            report["unsupported_level_records"] += 1
        stage_us, stage_text = _micro_time(record, "stageTimestamp", index,
                                          required=clock_field == "stageTimestamp")
        received_us, received_text = _micro_time(record, "requestReceivedTimestamp", index,
                                                required=clock_field == "requestReceivedTimestamp")
        other = received_us if clock_field == "stageTimestamp" else stage_us
        if other is None:
            report["absent_other_timestamp_records"] += 1
        user = record["user"]
        if not isinstance(user, dict):
            raise ValueError(f"record {index}: user is not the schema's object")
        row = {"record_time_us": stage_us if clock_field == "stageTimestamp" else received_us,
               # The reader admits only unknown/boot_relative for this format, so the wall clock is named
               # here rather than declared to it as an absolute basis.
               "record_time_basis": clock_name, "time_field_basis": clock_basis,
               "audit_stage_timestamp_us": "" if stage_us is None else stage_us,
               "audit_request_received_timestamp_us": "" if received_us is None else received_us,
               "audit_stage_timestamp_text": stage_text, "audit_request_received_timestamp_text":
               received_text,
               "audit_time_status": ("BOTH_TIMESTAMPS_REPORTED_NEVER_SUBTRACTED" if other is not None
                                     else "THE_OTHER_TIMESTAMP_IS_ABSENT_FROM_THIS_RECORD"),
               "audit_id_hex": _hex(audit_id), "audit_stage": stage,
               "audit_stage_status": SUPPORTED_STAGE if stage_supported else UNSUPPORTED_STAGE,
               "audit_level": level,
               "audit_level_status": SUPPORTED_LEVEL if level_supported else UNSUPPORTED_LEVEL,
               "audit_verb": _string(record, "verb", index, required=True),
               "audit_request_uri_hex": _hex(_string(record, "requestURI", index, required=True)),
               "audit_user_name_hex": _hex(_string(user, "username", index)),
               "audit_user_agent_hex": _hex(_string(record, "userAgent", index)),
               "audit_outcome_basis": OUTCOME_BASIS, "identity_basis": IDENTITY_BASIS,
               "source_record_index": index, "source_pin": SOURCE_PIN, "api_version_reported": API_VERSION,
               "audit_id_stage_count": len(stages_by_id[audit_id]),
               "audit_id_stages_present": ";".join(stages_by_id[audit_id]),
               # A plain fact about this file: it says nothing about what the request did or was allowed.
               "audit_id_has_response_complete":
               "YES" if COMPLETION_STAGE in stages_by_id[audit_id] else "NO_RESPONSE_COMPLETE_IN_THIS_FILE"}
        reference = record.get("objectRef")
        if reference is None:
            row["audit_object_ref_status"] = "ABSENT_LIST_OR_NON_RESOURCE_REQUEST_OR_NOT_LOGGED"
            report["records_without_object_ref"] += 1
            for _, column in OBJECT_REF:
                row[column] = ""
        elif isinstance(reference, dict):
            row["audit_object_ref_status"] = "REPORTED_TARGET_REFERENCE_NOT_AN_EXISTENCE_PROOF"
            for name, column in OBJECT_REF:
                value = _string(reference, name, index)
                row[column] = _hex(value) if column.endswith("_hex") else value
        else:
            raise ValueError(f"record {index}: objectRef is not the schema's object")
        status = record.get("responseStatus")
        if status is None:
            row.update({"audit_response_code": "", "audit_response_status_reported": "",
                        "audit_response_reason": "", "audit_outcome_status": OUTCOME_ABSENT})
        elif isinstance(status, dict):
            code = status.get("code")
            if code is not None and (type(code) is not int or not 0 <= code <= 2 ** 31 - 1):
                raise ValueError(f"record {index}: responseStatus.code is not the schema's int32")
            row.update({"audit_response_code": "" if code is None else code,
                        "audit_response_status_reported": _string(status, "status", index),
                        "audit_response_reason": _string(status, "reason", index),
                        "audit_outcome_status": OUTCOME_REPORTED})
        else:
            raise ValueError(f"record {index}: responseStatus is not the schema's object")
        row.update(_bodies(record, index))
        report["withheld_bodies"] += sum(1 for name in ("request_body_status", "response_body_status")
                                         if row[name] == BODY_WITHHELD)

        annotations = record.get("annotations")
        if annotations is None:
            keys = []
        elif isinstance(annotations, dict) and all(isinstance(key, str) and isinstance(value, str)
                                                   for key, value in annotations.items()):
            keys = sorted(annotations)
        else:
            raise ValueError(f"record {index}: annotations is not the schema's string map")
        # Keys only: an authorization decision value is never imported as a verdict.
        shown = keys[:MAX_KEYS_IN_CELL]
        row["audit_annotation_count"] = len(keys)
        row["audit_annotation_keys"] = ";".join(shown) + (
            f";+{len(keys) - len(shown)}_more" if len(keys) > len(shown) else "")
        report["annotation_keys_total"] += len(keys)
        # Each key is one value this importer deliberately does not copy; the value stays in the input.
        report["withheld_annotation_values"] += len(keys)
        # Names only, for both the unknown top-level fields and the sub-fields inside the objects this
        # importer does interpret. Neither is a copy of a value: both are retained by reference.
        unknown = sorted(name for name in record if name not in INTERPRETED)
        report["unknown_fields_total"] += len(unknown)
        row["unknown_field_count"] = len(unknown)
        row["unknown_field_names"] = ";".join(unknown[:MAX_KEYS_IN_CELL]) + (
            f";+{len(unknown) - MAX_KEYS_IN_CELL}_more" if len(unknown) > MAX_KEYS_IN_CELL else "")
        nested = _nested_uninterpreted(record, index)
        report["nested_uninterpreted_total"] += len(nested)
        row["nested_uninterpreted_count"] = len(nested)
        row["nested_uninterpreted_names"] = ";".join(nested[:MAX_KEYS_IN_CELL]) + (
            f";+{len(nested) - MAX_KEYS_IN_CELL}_more" if len(nested) > MAX_KEYS_IN_CELL else "")
        row["retention_basis"] = RETENTION_BASIS
        row["source_file_sha256"] = report["source_sha256"]
        # JSONL: the record's own bytes without the newline the encoder appends. EventList: a canonical
        # re-serialisation of the item, which is NOT the original body's bytes - the scope column says so,
        # and the whole-file digest plus the JSON pointer are what address the original.
        row["record_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        row["record_bytes"] = len(raw)
        row["record_digest_scope"] = scope
        row["source_item_pointer"] = pointer
        row["source_container"] = container
        row["record_type_identity_basis"] = type_basis
        if redact:
            for column in HEX_COLUMNS:
                if row[column]:
                    row[column] = REDACTED
                    report["redacted_fields"] += 1
        writer.writerow(row)
        report["rows"] += 1
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("output exceeds the bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-format", required=True, choices=SOURCE_FORMATS,
                        help="the saved container: one Event per line, or one EventList request body")
    parser.add_argument("--time-field", required=True, choices=tuple(TIME_FIELDS),
                        help="which schema timestamp becomes the record time; the other is retained beside it")
    parser.add_argument("--redact", action="store_true",
                        help="replace the hex-transported free-text cells with a declared redaction marker, "
                             "counted in the accounting rather than silently dropped")
    parser.add_argument("--report", type=Path, help="write the accounting as JSON to this new file")
    args = parser.parse_args()
    written = []
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("input exceeds bound")
        text = data.decode("utf-8")
        output, report = convert(text, time_field=args.time_field, source_format=args.source_format,
                                redact=args.redact)
        if report["source_sha256"] != "sha256:" + hashlib.sha256(data).hexdigest():
            raise ValueError("reported digest does not bind the input bytes")
        summary = json.dumps(report, sort_keys=True)
        targets = [("converted CSV", args.output)]
        if args.report is not None:
            targets.append(("accounting report", args.report))
        seen = {}
        for label, target in targets:
            if target.exists():
                raise ValueError(f"the {label} target already exists; this importer never overwrites")
            key = os.path.realpath(target)
            if key in seen:
                raise ValueError(f"the {label} and the {seen[key]} target are the same path")
            seen[key] = label
        for label, target, body in (("accounting report", args.report,
                                     None if args.report is None else summary + "\n"),
                                    ("converted CSV", args.output, output)):
            if body is None:
                continue
            with target.open("x", encoding="utf-8") as stream:
                written.append(target)
                stream.write(body)
        # Counts, pins and bases only - no URI, name, user, auditID or annotation key.
        print(summary, file=sys.stderr)
    except (OSError, ValueError, csv.Error) as error:
        for target in written:
            try:
                target.unlink()
            except OSError:
                pass
        parser.exit(2, f"audit event import failed; no successful conversion claim: {error}\n")


if __name__ == "__main__":
    main()
