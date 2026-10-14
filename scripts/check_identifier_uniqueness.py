#!/usr/bin/env python3
"""Check one explicit source-local PIN -> NAME uniqueness declaration, never physical identity."""
import argparse
import csv
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_producer_software_version as shared

INPUT_LIMIT = 16 * 1024 * 1024
DECLARATION_LIMIT = 64 * 1024
ROW_LIMIT = 4096
WITNESS_LIMIT = 64
WITNESS_BYTES = 64 * 1024
OUTPUT_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 64 * 1024 * 1024
SCHEMA = "identifier-uniqueness-v1"
BASIS = "CALLER_SINGLE_NAME_PER_REPORTED_SOURCE_LOCAL_PIN_WITHIN_DECLARED_FILE_WINDOW_NOT_AUTHENTICATION_OR_PHYSICAL_COLLISION"
FIELDS = ["record_time_us", "check_kind", "source_role", "subject_kind", "subject_key_hex", "attribute_kind",
          "max_distinct_values", "scope_id_hex", "namespace_hex", "instance_hex", "window_id_hex",
          "window_sources_hex", "window_sources_reported", "observed_names_hex", "witnesses_hex", "selected_pairs", "missing_pairs",
          "distinct_values", "disposition", "insufficient_reason", "uniqueness_basis", "citation_hex",
          "declaration_sha256", "read_report_sha256", "artefact_input_sha256", "profile_source_sha256"]


def json_hex(value):
    return shared.binding.text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def convert(report_bytes, declaration_bytes):
    if not isinstance(report_bytes, bytes) or len(report_bytes) > INPUT_LIMIT:
        raise ValueError("saved report exceeds bound")
    declaration = shared.document(declaration_bytes, DECLARATION_LIMIT)
    keys = {"schema", "read_report_sha256", "scope_id", "namespace", "instance", "source_role", "subject_kind",
            "subject_key", "attribute_kind", "max_distinct_values", "windows", "citation", "capture_time_us"}
    if (not isinstance(declaration, dict) or set(declaration) != keys or declaration["schema"] != SCHEMA
            or declaration["read_report_sha256"] != shared.digest(report_bytes)
            or declaration["source_role"] != "SOURCE_SOFTWARE" or declaration["subject_kind"] != "PIN"
            or declaration["attribute_kind"] != "NAME" or type(declaration["max_distinct_values"]) is not int
            or declaration["max_distinct_values"] != 1
            or any(not shared.label(declaration[key], 200) for key in ("scope_id", "namespace", "instance", "citation"))
            or not shared.label(declaration["subject_key"])
            or type(declaration["capture_time_us"]) is not int or not 0 <= declaration["capture_time_us"] < 2**63):
        raise ValueError("explicit exact subject, single-attribute scope and report binding required")
    windows = declaration["windows"]
    if not isinstance(windows, list) or not 1 <= len(windows) <= 64:
        raise ValueError("declare 1..64 saved-file windows")
    assigned, window_ids = {}, set()
    for index, window in enumerate(windows):
        if (not isinstance(window, dict) or set(window) != {"window_id", "bound_observations_sha256"}
                or not shared.label(window["window_id"], 200) or window["window_id"] in window_ids):
            raise ValueError("invalid or repeated window declaration")
        window_ids.add(window["window_id"])
        sources = window["bound_observations_sha256"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 64:
            raise ValueError("window needs explicit saved source digest set")
        for source in sources:
            if not shared.is_digest(source) or source in assigned or len(assigned) >= 64:
                raise ValueError("source repeats across windows or exceeds finite source cap")
            assigned[source] = index
    report, profile, observations = shared.qualified_report(report_bytes)
    if len(observations) > ROW_LIMIT:
        raise ValueError("too many observations")
    groups = [[] for _ in windows]
    seen, sources_seen, source_shapes = set(), set(), {}
    excluded = 0
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or not isinstance(observation.get("fields"), dict):
            raise ValueError("invalid common observation")
        fields = observation["fields"]
        source = fields.get("bound_observations_sha256")
        if (fields.get("identity_role") != "SOURCE_SOFTWARE" or fields.get("identity_kind") != "PIN+NAME"
                or not isinstance(source, str) or source not in assigned):
            excluded += 1
            continue
        fields, pair, columns = shared.qualified_pair(observation, ("PIN", "NAME"))
        if (fields.get("identity_namespace_hex") != shared.binding.text(declaration["namespace"])
                or fields.get("identity_instance_hex") != shared.binding.text(declaration["instance"])
                or fields.get("identity_provenance_basis") != shared.binding.PROVENANCE_BASIS):
            raise ValueError("reported namespace/instance conflicts with selected declared scope")
        for column in columns:
            shared.binding.declared_name(column)
        key = (source, pair["PIN"], pair["NAME"])
        shape = (tuple(columns), fields["bound_row_count"])
        previous = source_shapes.setdefault(source, [shape, 0])
        if key in seen or previous[0] != shape:
            raise ValueError("duplicate original tuple or inconsistent source column/count basis")
        seen.add(key)
        previous[1] += fields["identity_rows_carrying_value"]
        if previous[1] > fields["bound_row_count"]:
            raise ValueError("reported tuples double-count the original source")
        if pair["PIN"] is not None and pair["PIN"] != declaration["subject_key"]:
            excluded += 1
            continue
        group = groups[assigned[source]]
        if len(group) >= WITNESS_LIMIT:
            raise ValueError("selected witnesses exceed per-window cap")
        group.append(dict(source_observation_index=index, bound_observations_sha256=source,
                          columns=columns, pin=pair["PIN"], name=pair["NAME"],
                          rows=fields["identity_rows_carrying_value"], bound_rows=fields["bound_row_count"]))
    # The paired writer groups every original row, not just the selected subject.
    # A present source digest is insufficient if some of its aggregated tuples vanished.
    sources_seen = {source for source, (shape, total) in source_shapes.items() if total == shape[1]}
    output = io.StringIO()
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    for window, witnesses in zip(windows, groups):
        names = sorted({w["name"] for w in witnesses if w["pin"] is not None and w["name"] is not None})
        missing = sum(w["pin"] is None or w["name"] is None for w in witnesses)
        reason = ("DECLARED_SOURCE_NOT_REPORTED" if any(s not in source_shapes for s in window["bound_observations_sha256"])
                  else "SOURCE_PAIR_ACCOUNTING_INCOMPLETE" if any(s not in sources_seen for s in window["bound_observations_sha256"])
                  else "PAIR_COMPONENT_NOT_REPORTED" if missing else "SUBJECT_NOT_REPORTED" if not witnesses else "")
        encoded = json_hex(witnesses)
        if len(encoded[4:]) // 2 > WITNESS_BYTES:
            raise ValueError("selected witness JSON exceeds byte cap")
        writer.writerow(dict(record_time_us=declaration["capture_time_us"], check_kind="IDENTIFIER_UNIQUENESS",
            source_role="SOURCE_SOFTWARE", subject_kind="PIN", subject_key_hex=shared.binding.text(declaration["subject_key"]),
            attribute_kind="NAME", max_distinct_values=1, scope_id_hex=shared.binding.text(declaration["scope_id"]),
            namespace_hex=shared.binding.text(declaration["namespace"]), instance_hex=shared.binding.text(declaration["instance"]),
            window_id_hex=shared.binding.text(window["window_id"]), window_sources_hex=json_hex(sorted(window["bound_observations_sha256"])),
            window_sources_reported=sum(s in sources_seen for s in window["bound_observations_sha256"]),
            observed_names_hex=json_hex(names), witnesses_hex=encoded, selected_pairs=len(witnesses), missing_pairs=missing,
            distinct_values=len(names), disposition="INSUFFICIENT" if reason else "CONTRADICTION" if len(names) > 1 else "CONSISTENT",
            insufficient_reason=reason, uniqueness_basis=BASIS, citation_hex=shared.binding.text(declaration["citation"]),
            declaration_sha256=shared.digest(declaration_bytes), read_report_sha256=shared.digest(report_bytes),
            artefact_input_sha256=report["input_sha256"], profile_source_sha256=profile["source_sha256"]))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("output exceeds byte cap")
    result = dict(schema=SCHEMA, source_report_hex="hex:" + report_bytes.hex(),
                  source_declaration_hex="hex:" + declaration_bytes.hex(), declaration_sha256=shared.digest(declaration_bytes),
                  read_report_sha256=shared.digest(report_bytes), input_observations=len(observations),
                  selected_observations=sum(map(len, groups)), unselected_observations=excluded,
                  output_rows=len(windows), output_sha256=shared.digest(output.getvalue().encode()), clock="Unknown", basis=BASIS)
    if len(json.dumps(result).encode()) > REPORT_LIMIT:
        raise ValueError("paired report exceeds byte cap")
    return output.getvalue(), result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("declaration", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        if args.output_directory.exists():
            raise ValueError("new output directory required")
        inputs = []
        for path, limit in ((args.report, INPUT_LIMIT), (args.declaration, DECLARATION_LIMIT)):
            with path.open("rb") as stream:
                inputs.append(stream.read(limit + 1))
        output, report = convert(*inputs)
        args.output_directory.mkdir()
        with (args.output_directory / "observations.csv").open("x", encoding="utf-8", newline="") as stream:
            stream.write(output)
        with (args.output_directory / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(2, f"identifier uniqueness refused: {error}\n")


if __name__ == "__main__":
    main()
