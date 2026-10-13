#!/usr/bin/env python3
"""Compare reported producer software versions with an explicit saved expectation; no compatibility inference."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_declared_expectation_check as expectation
import convert_software_identity_binding as binding

INPUT_LIMIT = 16 * 1024 * 1024
EXPECTATION_LIMIT = 64 * 1024
ROW_LIMIT = 4096
OUTPUT_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 64 * 1024 * 1024
SCHEMA = "producer-software-expectation-v1"
BASIS = "REPORTED_PRODUCER_VERSION_VS_CALLER_EXPECTED_SET_NOT_COMPATIBILITY_OR_PHYSICAL_CAUSE"
VERSION_BASIS = "SOURCE_SOFTWARE_NAME_VERSION_PAIRED_ON_ORIGINAL_SAVED_ROWS"
FIELDS = ["record_time_us", "check_kind", "software_role", "software_name_hex",
          "actual_declared_hex", "expected_set_hex", "disposition", "insufficient_reason",
          "reported_version_basis", "comparison_basis", "citation_hex", "expectation_sha256",
          "read_report_sha256", "artefact_input_sha256", "profile_source_sha256",
          "bound_observations_sha256", "source_observation_index", "identity_join_hex",
          "identity_source_hex", "identity_rows_carrying_value", "identity_evidence"]


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def document(data, limit):
    if not isinstance(data, bytes) or not data or len(data) > limit:
        raise ValueError("empty or oversized saved document")
    return json.loads(data.decode("utf-8"), object_pairs_hook=unique)


def label(value, limit=256):
    return isinstance(value, str) and 0 < len(value) <= limit


def unhex(value):
    if not isinstance(value, str) or not re.fullmatch(r"hex:(?:[0-9a-f]{2})*", value) or len(value) > 16384:
        raise ValueError("invalid bounded canonical hex")
    return bytes.fromhex(value[4:]).decode("utf-8")


def qualified_report(report_bytes):
    """Complete adopted saved paired-identity report, shared without guessing producer schema."""
    report = document(report_bytes, INPUT_LIMIT)
    if not isinstance(report, dict):
        raise ValueError("saved reader report required")
    profile = report.get("profile_identity", {})
    observations = report.get("observations")
    if (not isinstance(profile, dict) or profile.get("profile_id") != "software_identity_binding_v1"
            or profile.get("declared_version") != "1" or not is_digest(profile.get("source_sha256"))
            or not is_digest(report.get("input_sha256")) or report.get("source_role") != "recorded_export"
            or report.get("platform_domain") != "Unknown"
            or not isinstance(observations, list) or not 1 <= len(observations) <= ROW_LIMIT
            or type(report.get("main_rows")) is not int or report["main_rows"] != len(observations)):
        raise ValueError("only the complete adopted paired-identity common report is qualified")
    return report, profile, observations


def qualified_pair(observation, kinds=("NAME", "VERSION")):
    """Validate an already-selected exact two-part reported pair; roles stay caller-checked."""
    fields = observation["fields"]
    if (fields.get("identity_evidence") != "READ_FROM_THE_OBSERVATION_ROWS"
            or fields.get("identity_basis") != binding.IDENTITY_BASIS
            or observation.get("clock_basis") != "Unknown"):
        raise ValueError("selected identity is not a qualified reported pair")
    pair = json.loads(unhex(fields.get("identity_value_hex")), object_pairs_hook=unique)
    join = json.loads(unhex(fields.get("identity_join_hex")), object_pairs_hook=unique)
    columns = unhex(fields.get("identity_source_hex")).split(",")
    if (not isinstance(pair, dict) or set(pair) != set(kinds)
            or any(value is not None and not label(value) for value in pair.values())
            or len(columns) != 2 or len(set(columns)) != 2
            or not isinstance(join, dict) or set(join) != set(columns)
            or any(join[column] != (pair[kind] or "") for column, kind in zip(columns, kinds))
            or (fields.get("identity_missing_components") or "")
                != "+".join(key for key in kinds if pair[key] is None)
            or type(fields.get("identity_rows_carrying_value")) is not int
            or type(fields.get("bound_row_count")) is not int
            or not 1 <= fields["identity_rows_carrying_value"] <= fields["bound_row_count"] <= binding.ROW_LIMIT):
        raise ValueError("reported tuple/filter/count is inconsistent")
    return fields, pair, columns


def convert(report_bytes, expectation_bytes):
    report, profile, observations = qualified_report(report_bytes)
    declared = document(expectation_bytes, EXPECTATION_LIMIT)
    keys = {"schema", "read_report_sha256", "bound_observations_sha256", "source_role",
            "software_name", "expected_versions", "citation", "capture_time_us"}
    if not isinstance(declared, dict) or set(declared) != keys or declared["schema"] != SCHEMA:
        raise ValueError("explicit producer expectation schema required")
    if (declared["source_role"] != "SOURCE_SOFTWARE" or not label(declared["software_name"])
            or not label(declared["citation"], 200)
            or not is_digest(declared["bound_observations_sha256"])
            or declared["read_report_sha256"] != digest(report_bytes)
            or type(declared["capture_time_us"]) is not int
            or not 0 <= declared["capture_time_us"] < 2**63):
        raise ValueError("wrong role or missing subject/basis/time/exact report binding")
    expected = declared["expected_versions"]
    if (not isinstance(expected, list) or not 1 <= len(expected) <= 64
            or any(not label(value) for value in expected) or len(set(expected)) != len(expected)):
        raise ValueError("expected versions must be a finite explicit distinct string set")
    selected, excluded = [], 0
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or not isinstance(observation.get("fields"), dict):
            raise ValueError("invalid common observation")
        fields = observation["fields"]
        if (fields.get("identity_role") != "SOURCE_SOFTWARE" or fields.get("identity_kind") != "NAME+VERSION"
                or fields.get("bound_observations_sha256") != declared["bound_observations_sha256"]):
            excluded += 1
            continue
        fields, pair, _ = qualified_pair(observation)
        if pair["NAME"] is not None and pair["NAME"] != declared["software_name"]:
            excluded += 1
            continue
        reason = "NAME_NOT_REPORTED" if pair["NAME"] is None else "VERSION_NOT_REPORTED" if pair["VERSION"] is None else ""
        selected.append((index, fields, pair["VERSION"] if not reason else None, reason))
    if not selected:
        selected = [(None, {}, None, "SUBJECT_NOT_REPORTED")]
    # Membership equality is unchanged. This internal CSV uses Python's quoting parser;
    # only the final common-reader CSV must be unquoted (its text fields are hex).
    projected = io.StringIO()
    writer = csv.writer(projected, lineterminator="\n")
    writer.writerow(["record_time_us", "reported_version"])
    for _, _, version, _ in selected:
        writer.writerow([declared["capture_time_us"], version if version is not None else ""])
    compared, accounting = expectation.convert(projected.getvalue().encode(), clock_basis="Unknown",
        value_meaning="reported producer version exact string membership only", membership_column="reported_version",
        expected_values=tuple(expected))
    checks = list(csv.DictReader(io.StringIO(compared)))
    output = io.StringIO()
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    for (index, fields, version, reason), check in zip(selected, checks):
        disposition = {"WITHIN_DECLARED_EXPECTATION": "MATCH", "OUTSIDE_DECLARED_EXPECTATION": "MISMATCH",
                       "NOT_REPORTED": "INSUFFICIENT"}[check["expectation_disposition"]]
        writer.writerow(dict(record_time_us=declared["capture_time_us"], check_kind="PRODUCER_SOFTWARE_VERSION",
            software_role="SOURCE_SOFTWARE", software_name_hex=binding.text(declared["software_name"]),
            actual_declared_hex=binding.text(version) if version is not None else "",
            expected_set_hex=binding.text(json.dumps(sorted(expected), ensure_ascii=False, separators=(",", ":"))),
            disposition=disposition, insufficient_reason=reason, reported_version_basis=VERSION_BASIS,
            comparison_basis=BASIS, citation_hex=binding.text(declared["citation"]),
            expectation_sha256=digest(expectation_bytes), read_report_sha256=digest(report_bytes),
            artefact_input_sha256=report["input_sha256"], profile_source_sha256=profile["source_sha256"],
            bound_observations_sha256=declared["bound_observations_sha256"],
            source_observation_index="" if index is None else index,
            identity_join_hex=fields.get("identity_join_hex", ""),
            identity_source_hex=fields.get("identity_source_hex", ""),
            identity_rows_carrying_value=fields.get("identity_rows_carrying_value", ""),
            identity_evidence=fields.get("identity_evidence", "")))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("output exceeds byte bound")
    result = dict(schema=SCHEMA, source_report_hex="hex:" + report_bytes.hex(),
                  source_expectation_hex="hex:" + expectation_bytes.hex(),
                  read_report_sha256=digest(report_bytes), expectation_sha256=digest(expectation_bytes),
                  input_observations=len(observations), selected_observations=len(observations) - excluded,
                  unselected_observations=excluded, output_rows=len(selected),
                  summary_rows=int(selected[0][0] is None), membership_accounting=accounting,
                  output_sha256=digest(output.getvalue().encode()), clock="Unknown",
                  basis=BASIS)
    if len(json.dumps(result).encode()) > REPORT_LIMIT:
        raise ValueError("paired report exceeds byte bound")
    return output.getvalue(), result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("expectation", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        if args.output_directory.exists():
            raise ValueError("new output directory required")
        data = []
        for path, limit in ((args.report, INPUT_LIMIT), (args.expectation, EXPECTATION_LIMIT)):
            with path.open("rb") as stream:
                data.append(stream.read(limit + 1))
        converted, report = convert(*data)
        args.output_directory.mkdir()
        with (args.output_directory / "observations.csv").open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
        with (args.output_directory / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(2, f"producer version comparison refused: {error}\n")


if __name__ == "__main__":
    main()
