#!/usr/bin/env python3
"""Per-row reported run/entity values bound to exact supplied quantity CSV rows."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re

try:
    from scripts import convert_software_identity_binding as binding
except ModuleNotFoundError:
    import convert_software_identity_binding as binding

OUTPUT_LIMIT = 64 * 1024 * 1024
VALUE_LIMIT = 256
BASIS = ("CALLER_SELECTED_COLUMN_ROLES_VALUES_READ_FROM_EXACT_SAVED_ROW_"
         "JOIN_BY_SOURCE_SHA_AND_ROW_INDEX_NOT_TIME_OR_NAME_"
         "EMPTY_NOT_FILLED_EQUAL_NAMES_NOT_ONE_INSTANCE_NOT_AUTHENTICATED_"
         "NOT_SOFTWARE_EXECUTION_OR_PHYSICAL_ENTITY_PROOF_CLOCK_UNKNOWN")
FIELDS = ["record_time_us", "bound_observations_sha256", "source_row_index",
          "reported_run_hex", "reported_entity_hex", "run_status", "entity_status",
          "run_column_hex", "entity_column_hex", "bound_time_column_hex", "row_identity_basis"]


def convert(data, *, run_column, entity_column, time_column="record_time_us"):
    """Return a sidecar, not a replacement for the original quantity observations."""
    if not isinstance(data, bytes):
        raise ValueError("saved observations must be bytes")
    columns = [binding.declared_name(c) for c in (run_column, entity_column, time_column)]
    if len(set(columns)) != 3:
        raise ValueError("run, entity and time columns must be distinct")
    # This route deliberately accepts the same unquoted shape as the quantity common reader.
    # CRLF is a record ending, not a control-bearing field; a bare CR is refused.
    if b'"' in data or re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\r(?!\n)", data):
        raise ValueError("quoted or control-bearing saved CSV is unsupported")
    header, index, rows = binding.read_rows(data, time_column)
    if run_column not in index or entity_column not in index:
        raise ValueError("selected run/entity column is absent")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = dict(schema="musubi.recorded-row-identity.v1", source_sha256=digest,
                  rows=len(rows), first_record_time_us=rows[0][0], last_record_time_us=rows[-1][0],
                  run_column=run_column, entity_column=entity_column, time_column=time_column,
                  missing_run_rows=0, missing_entity_rows=0,
                  unselected_columns=[name for name in header if name not in columns],
                  clock="Unknown", quantity_semantics="READ_ORIGINAL_WITH_ITS_EXISTING_PROFILE")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    byte_count = len(output.getvalue().encode())
    for row_index, (moment, cells) in enumerate(rows):
        if moment >= 2**63:
            raise ValueError("saved time exceeds signed 64-bit microseconds")
        row = dict(record_time_us=moment, bound_observations_sha256=digest,
                   source_row_index=row_index, run_column_hex=binding.text(run_column),
                   entity_column_hex=binding.text(entity_column),
                   bound_time_column_hex=binding.text(time_column), row_identity_basis=BASIS)
        for role, column in (("run", run_column), ("entity", entity_column)):
            value = cells[index[column]]
            if len(value.encode("utf-8")) > VALUE_LIMIT:
                raise ValueError("selected identity exceeds UTF-8 byte bound")
            row[f"reported_{role}_hex"] = binding.text(value) if value else ""
            row[f"{role}_status"] = "PRESENT" if value else "EMPTY"
            report[f"missing_{role}_rows"] += int(not value)
        # All emitted cells are ASCII; writerow returns the actual serialized character count.
        byte_count += writer.writerow(row)
        if byte_count > OUTPUT_LIMIT:
            raise ValueError("identity output exceeds byte bound")
    rendered = output.getvalue()
    report["output_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
    return rendered, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--run-column", required=True)
    parser.add_argument("--entity-column", required=True)
    parser.add_argument("--time-column", default="record_time_us")
    args = parser.parse_args()
    try:
        if args.output_dir.exists():
            raise ValueError("output directory already exists")
        with args.input.open("rb") as source:
            data = source.read(binding.LIMIT + 1)
        output, report = convert(data, run_column=args.run_column,
                                 entity_column=args.entity_column, time_column=args.time_column)
        args.output_dir.mkdir()
        with (args.output_dir / "source.csv").open("xb") as target:
            target.write(data)
        with (args.output_dir / "identities.csv").open("x", encoding="utf-8", newline="") as target:
            target.write(output)
        with (args.output_dir / "report.json").open("x", encoding="utf-8") as target:
            target.write(json.dumps(report, indent=2) + "\n")
    except (ValueError, OSError) as failure:
        parser.exit(2, f"{failure}\n")


if __name__ == "__main__":
    main()
