#!/usr/bin/env python3
"""Bind a declared or reported software identity to the observations it produced.

The adopted converters already carry plenty of software identity: which side wrote an ISOXML file,
a node's self-reported software version, which exporter release and which dialect commit a
conversion applied. What is missing in several routes is the *join* -- the identity sits in a
sidecar report, in a case note, or on the rows of a different export, so an engineer cannot tell
which adaptation produced the observations in front of them.

This bridge states that binding and nothing else. It authenticates nothing, installs nothing, reads
no second file and compares no versions. Where it can, it reads the identity from the observation
rows themselves and says how many rows carried each value; otherwise it carries the caller's
declaration as a declaration. What nobody supplied stays unknown.
"""
import argparse
import csv
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
VALUE_LIMIT = 256
CITATION_LIMIT = 200
COMPONENT_LIMIT = 64
# A finite cap on produced rows: a work bound, not a promise about size. Size is bounded separately
# by OUTPUT_LIMIT against each row's real serialized bytes, because every text value is hex-expanded.
WORK_ROW_LIMIT = 200000
# Four original roles plus the explicit reported hardware subject; never interchange them.
# CONVERSION_TOOL is the artifact that actually read the data; REFERENCE_SPECIFICATION is a
# document or upstream source an adaptation was written against, which never executed here.
ROLES = ("SOURCE_SOFTWARE", "CONVERSION_TOOL", "ADAPTER_PROFILE", "REFERENCE_SPECIFICATION", "SOURCE_DEVICE")
KINDS = ("NAME", "VERSION", "PIN", "DIGEST", "FAMILY")
DEVICE_BASIS = ("SAVED_REPORT_HARDWARE_SUBJECT_ONLY_NOT_AUTHENTICATED_NOT_PROOF_OF_EXISTENCE_"
                "OWNERSHIP_OR_DRIVER_EXECUTION_OR_PERMISSION_PRODUCT_FAMILY_NOT_FIRMWARE_FAMILY_"
                "EQUAL_NAMES_OR_SERIALS_DO_NOT_ESTABLISH_ONE_PHYSICAL_DEVICE")
DEVICE_FIELDS = ["record_time_us", "time_disposition", "device_index", "product_line_hex",
                 "product_line_disposition", "device_name_hex", "serial_number_hex", "usb_product_id_hex",
                 "source_block_hex", "source_input_sha256", "device_identity_basis"]
# The kinds of one paired identity are named with a single separator, and the list of the parts a
# saved row left empty uses that same separator -- so `identity_missing_components` reads as a
# subset of `identity_kind` in one notation: NAME+PIN against NAME+PIN+VERSION. It is deliberately
# NOT a comma: the common reader refuses quoted input, so one comma inside one cell would make the
# whole emitted file unreadable, which is how a legitimate two-part gap used to be lost.
KIND_SEPARATOR = "+"
# Nothing this converter emits may need CSV quoting, because the common reader refuses quoted input.
# Every text value is hex-encoded and every other cell is an enum, a count or a digest, so this can
# only fire on a defect in this file -- and it fires as a refusal instead of writing a file the
# reader would reject whole.
NEEDS_QUOTING = re.compile(r'[,"\r\n]')
IDENTITY_BASIS = (
    "AN_IDENTITY_BOUND_TO_ONE_EXACT_SET_OF_SAVED_OBSERVATIONS_NAMED_BY_THEIR_DIGEST_ROW_COUNT_AND_"
    "TIME_SPAN_IT_IS_WHAT_THE_FILE_OR_THE_CALLER_SAYS_AND_IS_NOT_AUTHENTICATED_NOT_AN_INSTALLED_"
    "PACKAGE_FACT_NOT_A_BUILD_ATTESTATION_AND_NOT_EVIDENCE_THAT_THE_NAMED_SOFTWARE_PRODUCED_"
    "ANYTHING_A_ROLE_IS_NEVER_INTERCHANGED_SO_THE_PROGRAM_THAT_WROTE_THE_DATA_IS_NEVER_REPORTED_AS_"
    "THE_TOOL_THAT_READ_IT_A_REFERENCE_SPECIFICATION_IS_A_DOCUMENT_OR_UPSTREAM_SOURCE_AN_ADAPTATION_"
    "WAS_WRITTEN_AGAINST_AND_NEVER_A_TOOL_THAT_EXECUTED_A_CONVERSION_TOOL_DIGEST_IDENTIFIES_THE_"
    "BYTES_OF_A_NAMED_ARTIFACT_AND_IS_NOT_A_RUNTIME_ATTESTATION_THAT_THOSE_BYTES_RAN_A_CALLER_"
    "DECLARED_COMPONENT_BINDS_TO_THIS_FILE_AS_THE_CALLERS_ASSERTION_AND_IS_NOT_EVIDENCE_THAT_IT_"
    "HOLDS_FOR_ANY_INDIVIDUAL_ROW_AND_TWO_EQUAL_NAMES_ARE_NEVER_ASSUMED_TO_BE_ONE_INSTANCE")
# A citation and a namespace are different things and are kept in separate columns. A citation says
# where the caller says these values are defined -- a document, a page, an agreement. A namespace says
# whose naming authority the caller says the value belongs to. Neither is read from the data, neither
# is verified, and neither confers authority: they are the caller's own words, carried so that the same
# value declared under two different namespaces stays two distinguishable claims.
PROVENANCE_BASIS = (
    "THE_CITATION_AND_THE_NAMESPACE_ON_THIS_ROW_ARE_CALLER_SUPPLIED_WORDS_CARRIED_UNCHANGED_AND_ARE_"
    "NOT_READ_FROM_THE_BOUND_DATA_NOT_VERIFIED_AND_NOT_AUTHENTICATED_A_CITATION_NAMES_WHERE_THE_CALLER_"
    "SAYS_THESE_VALUES_ARE_DEFINED_AND_A_NAMESPACE_NAMES_WHOSE_NAMING_AUTHORITY_THE_CALLER_SAYS_THE_"
    "VALUE_BELONGS_TO_NEITHER_STRING_CONFERS_THAT_AUTHORITY_NO_AUTHORITY_IS_INFERRED_FROM_EITHER_TEXT_"
    "THEY_ARE_NEVER_EVIDENCE_OF_AUTHENTICITY_OWNERSHIP_OR_LINEAGE_AND_THE_SAME_VALUE_DECLARED_UNDER_"
    "TWO_NAMESPACES_STAYS_TWO_DISTINCT_CLAIMS_THAT_ARE_NEVER_MERGED")
FIELDS = ["record_time_us", "identity_role", "identity_kind", "identity_value_hex",
          "identity_evidence", "identity_instance_hex", "identity_source_hex",
          "identity_citation_hex", "identity_namespace_hex", "identity_collision",
          "identity_rows_carrying_value", "identity_join_hex", "identity_missing_components",
          "bound_observations_sha256", "bound_row_count",
          "bound_first_record_time_us", "bound_last_record_time_us", "bound_time_column_hex",
          "identity_provenance_basis", "identity_basis"]


def text(value):
    return "hex:" + value.encode("utf-8").hex()


def convert_realsense_device_info(data, *, capture_time_us=None):
    """Saved v2.56.5 device-info blocks only; never invoke SDK or open a playback path."""
    if not isinstance(data, bytes) or not data or len(data) > 1024 * 1024:
        raise ValueError("saved device info must be nonempty bytes within 1 MiB")
    if capture_time_us is not None and (type(capture_time_us) is not int
                                      or not 0 <= capture_time_us < 2**63):
        raise ValueError("caller capture time must be integer microseconds below 2^63")
    source = data.decode("utf-8")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\r(?!\n)", source):
        raise ValueError("unsupported control or binary device info")
    # Only the writer's LF/CRLF frames rows; Unicode separators inside values are data.
    parts = source.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = dict(devices=0, input_lines=len(lines), selected_lines=0, unselected_lines=[],
                  source_input_sha256=digest, source_input_hex="hex:" + data.hex(), clock="Unknown")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, DEVICE_FIELDS, lineterminator="\n")
    writer.writeheader()
    block, info, annotation = None, {}, False
    for index, original in enumerate(lines, 1):
        line = original.removesuffix("\n").removesuffix("\r")
        if block is None:
            if line != "Device info: ":
                if line.startswith("Device info:"):
                    raise ValueError("malformed device-info header")
                report["unselected_lines"].append(dict(line=index, text=original))
                continue
            block, info, annotation = [original], {}, False
            report["selected_lines"] += 1
            continue
        block.append(original)
        report["selected_lines"] += 1
        if line == "":
            if not info or report["devices"] >= 256:
                raise ValueError("empty device info or device count exceeds 256")
            product = info.get("Product Line")
            row = dict(record_time_us=0 if capture_time_us is None else capture_time_us,
                       time_disposition="UNLOCATED_TIME" if capture_time_us is None else "CALLER_CAPTURE_UNKNOWN",
                       device_index=report["devices"], product_line_hex=text(product) if product else "",
                       product_line_disposition="NOT_REPORTED" if product is None else
                           "REPORTED_EMPTY" if product == "" else "REPORTED_UNCLASSIFIED",
                       source_block_hex=text("".join(block)), source_input_sha256=digest,
                       device_identity_basis=DEVICE_BASIS)
            for column, key in (("device_name_hex", "Name"), ("serial_number_hex", "Serial Number"),
                                ("usb_product_id_hex", "Product Id")):
                row[column] = text(info[key]) if info.get(key) else ""
            writer.writerow(row)
            if output.tell() > 16 * 1024 * 1024:
                raise ValueError("device CSV exceeds 16 MiB")
            report["devices"] += 1
            block = None
            continue
        if not info and not annotation and line.startswith("Playback Device (") and line.endswith(")"):
            annotation = True
            continue
        match = re.fullmatch(r"    ([A-Za-z][A-Za-z0-9 /_-]{0,63}): \t(.*)", line)
        if not match:
            raise ValueError("malformed or unterminated device-info block")
        padded, value = match.groups()
        key = padded.rstrip(" ")
        if padded != key.ljust(30) or key in info or len(info) >= 64 or len(value.encode()) > 256:
            raise ValueError("duplicate, malformed or oversized device-info field")
        info[key] = value
    if block is not None or not report["devices"]:
        raise ValueError("incomplete device info or no selected device block")
    return output.getvalue(), report


def validate_role_kind(role, kind):
    if (kind == "FAMILY" and role != "SOURCE_DEVICE") or (
            role == "SOURCE_DEVICE" and kind not in ("NAME", "FAMILY")):
        raise ValueError("hardware FAMILY/NAME and software version/pin roles cannot be interchanged")


# A saved column name, in the two shapes this estate's converters actually write. Several adopted
# recorded-output converters -- pose text, trajectory CSV, recorded angles, SBF records, blackbox
# motor output -- write the header as `name (unit)`, and the common reader already reads that shape
# against a profile declaring the bare name. Refusing it here meant those outputs could not be given
# a declared identity at all, so the optional suffix is accepted with a bounded unit charset. The
# bare name is unchanged, and a comma, a quote or a newline is still refused, because the common
# reader refuses quoted input.
COLUMN_NAME = re.compile(r"[A-Za-z0-9_.\[\]-]{1,64}(?: \([A-Za-z0-9_/%^.-]{1,16}\))?")


def declared_name(value):
    if not isinstance(value, str) or not COLUMN_NAME.fullmatch(value):
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
    """The saved observations exactly as written; a duplicate header name is refused."""
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
            raise ValueError("saved time decreases; this bridge never reorders a saved file")
        previous = moment
        rows.append((moment, cells))
    if not rows:
        raise ValueError("saved observations carry no rows")
    return header, index, rows


def component(spec):
    """One `ROLE:KIND=VALUE` declaration, with an empty value meaning explicitly unknown."""
    if not isinstance(spec, str) or len(spec) > VALUE_LIMIT + 64:
        raise ValueError("a declared component is one bounded ROLE:KIND=VALUE string")
    head, separator, value = spec.partition("=")
    role, colon, kind = head.partition(":")
    if not separator or not colon or role not in ROLES or kind not in KINDS:
        raise ValueError(f"each component is ROLE:KIND=VALUE with ROLE in {ROLES} and KIND in {KINDS}")
    validate_role_kind(role, kind)
    if len(value) > VALUE_LIMIT:
        raise ValueError("a declared component value exceeds the supported length")
    return role, kind, value


def read_component(spec):
    """One `ROLE:KIND=COLUMN` selection read from the rows themselves."""
    role, kind, column = component(spec)
    if not column:
        raise ValueError("a column-read component needs a column name")
    return role, kind, declared_name(column)


def read_identity(spec):
    """One `ROLE=KIND:COLUMN,KIND:COLUMN` group whose parts stay paired on the same saved row."""
    if not isinstance(spec, str) or len(spec) > VALUE_LIMIT + 256:
        raise ValueError("a paired identity is one bounded ROLE=KIND:COLUMN,... string")
    role, separator, body = spec.partition("=")
    if not separator or role not in ROLES or not body:
        raise ValueError(f"a paired identity is ROLE=KIND:COLUMN,... with ROLE in {ROLES}")
    parts = []
    for item in body.split(","):
        kind, colon, column = item.partition(":")
        if not colon or kind not in KINDS:
            raise ValueError(f"each part of a paired identity is KIND:COLUMN with KIND in {KINDS}")
        validate_role_kind(role, kind)
        parts.append((kind, declared_name(column)))
    if len(parts) < 2 or len({column for _, column in parts}) != len(parts):
        raise ValueError("a paired identity needs at least two parts with distinct columns")
    if len({kind for kind, _ in parts}) != len(parts):
        # The tuple is carried as a JSON object keyed by kind, so two parts of the same kind would
        # overwrite one another there while the joined kinds still claimed both. Refused, not merged.
        raise ValueError("a paired identity needs a distinct kind for each part")
    return role, tuple(parts)


def convert(data, *, read_components=(), declared_components=(), paired_identities=(),
            instance=None, source_citation=None, namespace=None, time_column="record_time_us"):
    if not read_components and not declared_components and not paired_identities:
        raise ValueError("nothing selected: declare a component or name a column to read")
    if len(read_components) + len(declared_components) + len(paired_identities) > COMPONENT_LIMIT:
        raise ValueError("too many identity components in one binding")
    for value, what in ((instance, "the instance label"), (source_citation, "the source citation"),
                        (namespace, "the declared namespace")):
        if value is not None and (not isinstance(value, str)
                                  or not 1 <= len(value) <= CITATION_LIMIT):
            raise ValueError(f"{what} must be a bounded non-empty string")
    header, index, rows = read_rows(data, declared_name(time_column))
    parsed_read = [read_component(spec) for spec in read_components]
    for _, _, column in parsed_read:
        if column not in header:
            raise ValueError("a column to read is not in the saved header")
    parsed_declared = [component(spec) for spec in declared_components]
    parsed_pairs = [read_identity(spec) for spec in paired_identities]
    for _, parts in parsed_pairs:
        for _, column in parts:
            if column not in header:
                raise ValueError("a paired identity column is not in the saved header")
    if "device_identity_basis" in index:
        allowed = {"device_name_hex": ("SOURCE_DEVICE", "NAME"),
                   "product_line_hex": ("SOURCE_DEVICE", "FAMILY")}
        selectors = parsed_read + [(role, kind, column) for role, parts in parsed_pairs for kind, column in parts]
        for role, kind, column in selectors:
            if allowed.get(column) != (role, kind):
                raise ValueError("native device identity column has a different role or meaning")
    if len({(role, kind, column) for role, kind, column in parsed_read}) != len(parsed_read):
        raise ValueError("duplicate column-read component")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    bound = {"bound_observations_sha256": digest, "bound_row_count": len(rows),
             "bound_first_record_time_us": rows[0][0], "bound_last_record_time_us": rows[-1][0],
             "bound_time_column_hex": text(time_column),
             "identity_instance_hex": "" if instance is None else text(instance),
             "identity_source_hex": "" if source_citation is None else text(source_citation),
             # Carried on EVERY emitted row, including the rows whose identity_source_hex is later
             # overwritten with the column the value was read from. Before this, a read row lost the
             # caller's citation entirely and it survived only in the report.
             "identity_citation_hex": "" if source_citation is None else text(source_citation),
             "identity_namespace_hex": "" if namespace is None else text(namespace),
             "identity_provenance_basis": PROVENANCE_BASIS,
             "identity_basis": IDENTITY_BASIS}
    report = {"bound_rows": len(rows), "bound_columns": len(header),
              "identity_components": 0, "components_read_from_rows": 0,
              "components_caller_declared": 0, "components_unknown": 0,
              "components_with_a_collision": 0, "distinct_values_emitted": 0,
              "paired_identities": 0, "distinct_pairs_emitted": 0,
              "pairs_with_a_missing_component": 0,
              "declared_instance": instance, "declared_source_citation": source_citation,
              "declared_namespace": namespace,
              "bound_observations_sha256": digest, "bound_row_count": len(rows),
              "clock": "Unknown", "authenticated_identity_records": "NOT_DERIVED",
              "installed_package_records": "NOT_DERIVED"}
    produced, budget = [], io.StringIO(newline="")
    scratch = csv.DictWriter(budget, FIELDS, lineterminator="\n", restval="")
    scratch.writeheader()
    written = len(budget.getvalue().encode("utf-8"))

    def emit(row):
        nonlocal written
        if row.get("identity_role") == "SOURCE_DEVICE":
            row["identity_basis"] = DEVICE_BASIS
        for name, value in row.items():
            if NEEDS_QUOTING.search(str(value)):
                raise ValueError(f"{name} would need CSV quoting, which the common reader refuses; "
                                 "refused here rather than written as an unreadable file")
        if len(produced) >= WORK_ROW_LIMIT:
            raise ValueError("this binding would produce more rows than the finite work cap allows; "
                             "that cap bounds work, not size")
        budget.seek(0)
        budget.truncate()
        scratch.writerow(row)
        line = budget.getvalue()
        written += len(line.encode("utf-8"))
        if written > OUTPUT_LIMIT:
            raise ValueError("the bound rows exceed the output byte bound; refused whole rather "
                             "than truncated")
        produced.append(line)

    moment = rows[0][0]  # every binding row belongs to the first observation it binds
    for role, kind, column in parsed_read:
        counts = {}
        for _, cells in rows:
            value = cells[index[column]]
            counts[value] = counts.get(value, 0) + 1
        reported = {value: count for value, count in counts.items() if value != ""}
        report["identity_components"] += 1
        if not reported:
            # The column exists but no row carried a value: unknown, and said so.
            report["components_unknown"] += 1
            emit(dict(bound, record_time_us=moment, identity_role=role, identity_kind=kind,
                      identity_value_hex="", identity_evidence="UNKNOWN_NOT_REPORTED",
                      identity_collision="", identity_rows_carrying_value=0,
                      identity_source_hex=text(column)))
            continue
        report["components_read_from_rows"] += 1
        # Two distinct values are two identities. Neither is chosen, and they are never merged.
        collision = ("TWO_OR_MORE_DISTINCT_VALUES_IN_THE_BOUND_OBSERVATIONS" if len(reported) > 1
                     else "")
        report["components_with_a_collision"] += bool(collision)
        for value, count in sorted(reported.items()):
            report["distinct_values_emitted"] += 1
            emit(dict(bound, record_time_us=moment, identity_role=role, identity_kind=kind,
                      identity_value_hex=text(value),
                      identity_evidence="READ_FROM_THE_OBSERVATION_ROWS",
                      identity_collision=collision, identity_rows_carrying_value=count,
                      # The same reproducible filter a paired identity carries: this column with
                      # this value, against the bound digest, selects the rows it was read from.
                      identity_join_hex=text(json.dumps({column: value}, separators=(",", ":"),
                                                        ensure_ascii=False)),
                      identity_source_hex=text(column)))
    for role, parts in parsed_pairs:
        # One row per distinct tuple as it appears **on a saved row**, so a name stays with the
        # version beside it and is never crossed with another row's.
        tuples = {}
        for _, cells in rows:
            key = tuple(cells[index[column]] for _, column in parts)
            tuples[key] = tuples.get(key, 0) + 1
        report["paired_identities"] += 1
        kinds = KIND_SEPARATOR.join(kind for kind, _ in parts)
        collision = ("TWO_OR_MORE_DISTINCT_VALUES_IN_THE_BOUND_OBSERVATIONS" if len(tuples) > 1
                     else "")
        report["components_with_a_collision"] += bool(collision)
        for key, count in sorted(tuples.items()):
            missing = [kind for (kind, _), value in zip(parts, key) if value == ""]
            report["distinct_pairs_emitted"] += 1
            report["pairs_with_a_missing_component"] += bool(missing)
            # The join is reproducible from this row alone: the bound digest names the file, and
            # these column/value pairs are the filter that selects the rows it came from.
            join = {column: value for (_, column), value in zip(parts, key)}
            paired = {kind: (None if value == "" else value)
                      for (kind, _), value in zip(parts, key)}
            emit(dict(bound, record_time_us=moment, identity_role=role, identity_kind=kinds,
                      identity_value_hex=text(json.dumps(paired, separators=(",", ":"),
                                                         ensure_ascii=False)),
                      identity_evidence="READ_FROM_THE_OBSERVATION_ROWS",
                      identity_collision=collision, identity_rows_carrying_value=count,
                      identity_join_hex=text(json.dumps(join, separators=(",", ":"),
                                                        ensure_ascii=False)),
                      # Every part the saved row left empty, named in the kind column's own
                      # notation. Two or three gaps are ordinary and are kept whole; none is
                      # dropped to keep the cell comma-free.
                      identity_missing_components=KIND_SEPARATOR.join(missing),
                      identity_source_hex=text(",".join(column for _, column in parts))))
    for role, kind, value in parsed_declared:
        report["identity_components"] += 1
        unknown = value == ""
        report["components_unknown" if unknown else "components_caller_declared"] += 1
        report["distinct_values_emitted"] += not unknown
        emit(dict(bound, record_time_us=moment, identity_role=role, identity_kind=kind,
                  identity_value_hex="" if unknown else text(value),
                  identity_evidence="UNKNOWN_NOT_REPORTED" if unknown else "CALLER_DECLARED",
                  identity_collision="", identity_rows_carrying_value=""))
    report["output_bytes"] = written
    return ",".join(FIELDS) + "\n" + "".join(produced), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="observations CSV an adopted converter already wrote")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--time-column", default="record_time_us")
    parser.add_argument("--input-format", choices=("csv", "realsense-device-info-v2.56.5"), default="csv")
    parser.add_argument("--capture-time-us", type=int, help="caller capture clock for saved device-info text only")
    parser.add_argument("--read-component", action="append", default=[],
                        help="ROLE:KIND=COLUMN read from the observation rows themselves")
    parser.add_argument("--declare-component", action="append", default=[],
                        help="ROLE:KIND=VALUE stated by the caller; an empty VALUE stays unknown")
    parser.add_argument("--read-identity", action="append", default=[],
                        help="ROLE=KIND:COLUMN,KIND:COLUMN read as one paired identity per saved row")
    parser.add_argument("--instance", help="caller label keeping reported instances apart; never authenticated")
    parser.add_argument("--source-citation", help="where the caller says these values are defined")
    parser.add_argument("--namespace",
                        help="whose naming authority the caller says the value belongs to; opt-in, "
                             "blank by default, never verified and never a grant of that authority")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > (LIMIT if args.input_format == "csv" else 1024 * 1024):
            raise ValueError("input exceeds bound")
        data = args.input.read_bytes()
        native, native_report = None, None
        if args.input_format != "csv":
            if args.time_column != "record_time_us":
                raise ValueError("native device-info time column cannot be reinterpreted")
            native, native_report = convert_realsense_device_info(data, capture_time_us=args.capture_time_us)
        elif args.capture_time_us is not None:
            raise ValueError("capture override is not allowed for existing CSV observations")
        output, report = convert(data if native is None else native.encode(), time_column=args.time_column,
                                 read_components=tuple(args.read_component),
                                 declared_components=tuple(args.declare_component),
                                 paired_identities=tuple(args.read_identity),
                                 instance=args.instance, source_citation=args.source_citation,
                                 namespace=args.namespace)
        if native_report is not None:
            report["native_device_info"] = native_report
        args.output_directory.mkdir()
        if native is not None:
            (args.output_directory / "source.txt").write_bytes(data)
            (args.output_directory / "device_observations.csv").write_text(native, encoding="utf-8")
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, csv.Error) as failure:
        print(f"software identity binding failed; no successful binding claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
