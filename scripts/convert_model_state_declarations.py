#!/usr/bin/env python3
"""Compose a declared SDF link and saved pose file; no simulation or inferred correspondence."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_sdf_airframe as sdf
from convert_pose_text import convert as pose_convert
from convert_software_identity_binding import read_rows, text as text_hex

STATE_LIMIT = 16 * 1024 * 1024
DECLARATION_LIMIT = 16 * 1024
LINE_LIMIT = 64 * 1024
ROW_LIMIT = 100000
OUTPUT_LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 64 * 1024 * 1024
SCHEMA = "model-state-declaration-v1"
FRAME_RELATION = "STATE_CHILD_IS_DECLARED_MODEL_LINK"
MODEL_TIME = "TIMELESS_CONFIGURATION"
BASIS = "FILE_SCOPE_CALLER_DECLARATION_NOT_PRODUCER_ENTITY_FRAME_OR_CLOCK_VERIFICATION"
LABELS = ("model_name", "link_name", "state_entity", "state_child_frame",
          "state_reference_frame", "state_clock_id", "run_id", "citation")
KEYS = {"schema", "model_sha256", "state_sha256", "frame_relation", "model_time_relation",
        "capture_time_us", "mass_unit", "unit_basis", *LABELS}
EXTRA = ["source_model_sha256", "source_state_sha256", "source_declaration_sha256",
         "source_state_row_index", "model_element_index", "model_sdf_version",
         "model_capture_time_us_hex", "state_time_exact_hex", "model_reported_mass",
         "model_mass_kg", "model_mass_disposition", "model_unit_declaration_hex",
         *[name + "_hex" for name in LABELS], "frame_relation", "model_time_relation",
         "declaration_basis"]


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def bounded(data, limit, what):
    if not isinstance(data, bytes) or not data or len(data) > limit:
        raise ValueError(f"empty or oversized {what}")
    return data.decode("utf-8")


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate declaration key")
        result[key] = value
    return result


def convert(model, state, declaration):
    """Return the complete CSV and mandatory paired provenance report, or refuse whole."""
    bounded(model, sdf.LIMIT, "model")
    state_text = bounded(state, STATE_LIMIT, "state")
    declared = json.loads(bounded(declaration, DECLARATION_LIMIT, "declaration"),
                          object_pairs_hook=unique_object)
    if not isinstance(declared, dict) or set(declared) != KEYS:
        raise ValueError("declaration needs exactly the v1 fields")
    if (declared["schema"] != SCHEMA or declared["frame_relation"] != FRAME_RELATION
            or declared["model_time_relation"] != MODEL_TIME):
        raise ValueError("unsupported schema or frame/time relation")
    for key in LABELS:
        value = declared[key]
        if (not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("explicit bounded entity/frame/clock/run/citation required")
    for key, data in (("model_sha256", model), ("state_sha256", state)):
        if (not isinstance(declared[key], str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", declared[key])
                or declared[key] != digest(data)):
            raise ValueError("declaration does not bind these exact source bytes")
    if declared["mass_unit"] != "kg":
        raise ValueError("explicit kg declaration required")
    # Existing SDF code validates capture time, version, unit basis, duplicate names and topology.
    model_csv, model_report = sdf.convert(model, declared["capture_time_us"],
                                         mass_unit=declared["mass_unit"],
                                         unit_basis=declared["unit_basis"])
    model_rows = list(csv.DictReader(io.StringIO(model_csv)))
    selected = [row for row in model_rows if row["airframe_record_kind"] == "LINK"
                and row["airframe_model_name_hex"] == text_hex(declared["model_name"])
                and row["airframe_link_name_hex"] == text_hex(declared["link_name"])]
    if len(selected) != 1:
        raise ValueError("selected model/link is not uniquely declared in this document")
    link = selected[0]
    count = 0
    for line in state_text.splitlines():
        if len(line.encode("utf-8")) > LINE_LIMIT:
            raise ValueError("state line exceeds bound")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        count += 1
        if count > ROW_LIMIT:
            raise ValueError("state row count exceeds bound")
        cells = line.split()
        if len(cells) != 8:
            raise ValueError("state requires the existing eight-field pose shape")
        # Refuse representable-as-zero underflow too, before common turns tokens into f64.
        values = [sdf.finite_value(cell, "state component") for cell in cells[1:]]
        if all(value == 0 for value in values[3:]):
            raise ValueError("all-zero quaternion is not a pose orientation")
    state_csv, state_report = pose_convert(state_text, allow_equal_time=True)
    # PR472's common header/unit and exact row/time accounting helper is reused unchanged.
    header, index, rows = read_rows(state_csv.encode("utf-8"), "record_time (us)")
    if len(rows) != count:
        raise ValueError("state accounting mismatch")
    common = dict(source_model_sha256=digest(model), source_state_sha256=digest(state),
                  source_declaration_sha256=digest(declaration),
                  model_element_index=link["airframe_element_index"],
                  model_sdf_version=link["airframe_declared_sdf_version"],
                  model_capture_time_us_hex=text_hex(str(declared["capture_time_us"])),
                  model_reported_mass=link["airframe_reported_mass"],
                  model_mass_kg=link["airframe_mass_kg"],
                  model_mass_disposition=link["airframe_mass_disposition"],
                  model_unit_declaration_hex=text_hex(link["airframe_unit_declaration"]),
                  frame_relation=FRAME_RELATION, model_time_relation=MODEL_TIME,
                  declaration_basis=BASIS,
                  **{key + "_hex": text_hex(declared[key]) for key in LABELS})
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header + EXTRA)
    written = len(output.getvalue().encode("utf-8"))
    for row_index, (_, cells) in enumerate(rows):
        fields = dict(common, source_state_row_index=row_index,
                      state_time_exact_hex=text_hex(cells[index["timestamp_unix_s"]]))
        before = output.tell()
        writer.writerow(cells + [fields[key] for key in EXTRA])
        # Every emitted cell is numeric, fixed enum, digest or hex: output is ASCII.
        written += output.tell() - before
        if written > OUTPUT_LIMIT:
            raise ValueError("composed output exceeds bound")
    report = dict(schema=SCHEMA, declaration_basis=BASIS, declaration=declared,
                  source_model_sha256=digest(model), source_state_sha256=digest(state),
                  source_declaration_sha256=digest(declaration),
                  source_state_hex="hex:" + state.hex(),
                  source_declaration_hex="hex:" + declaration.hex(),
                  model_report=model_report, state_report=state_report,
                  model_output_rows=len(model_rows), model_selected_links=1,
                  model_nonselected_rows=len(model_rows) - 1,
                  selected_model_element_index=int(link["airframe_element_index"]),
                  state_rows=count, output_rows=len(rows),
                  first_state_time_us=rows[0][0], last_state_time_us=rows[-1][0],
                  derived_pose_csv_sha256=digest(state_csv.encode("utf-8")),
                  output_sha256=digest(output.getvalue().encode("utf-8")),
                  output_bytes=written, common_clock="Unknown",
                  clock_basis="state exact microseconds; model timeless; capture is intake only",
                  correspondence="caller file-scope declaration; not a verified per-row entity")
    if len(json.dumps(report, ensure_ascii=True).encode("utf-8")) > REPORT_LIMIT:
        raise ValueError("paired report exceeds bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("declaration", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        if args.output_directory.exists():
            raise ValueError("output directory must be new")
        inputs = []
        for path, limit in ((args.model, sdf.LIMIT), (args.state, STATE_LIMIT),
                            (args.declaration, DECLARATION_LIMIT)):
            with path.open("rb") as stream:
                inputs.append(stream.read(limit + 1))
        converted, report = convert(*inputs)
        args.output_directory.mkdir()
        with (args.output_directory / "observations.csv").open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
        with (args.output_directory / "report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True)
    except (ValueError, OSError, UnicodeError, sdf.ET.ParseError) as error:
        parser.exit(2, f"model/state conversion refused: {error}\n")


if __name__ == "__main__":
    main()
