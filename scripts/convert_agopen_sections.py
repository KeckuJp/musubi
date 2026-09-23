#!/usr/bin/env python3
"""Saved AgOpenGPS `Sections.txt` -> reported mapping coverage vertices for the common reader.

Pin: AgOpenGPS-Official/AgOpenGPS `21be26aa58d09b9b5abacc715d0abcf4a021f1db` -- the commit the adopted
TrackLines and Boundary cases already use (it does not resolve under `farmerbriantee/AgOpenGPS`).

`SourceCode/GPS/IO/SectionFiles.cs`, `SectionsFiles.Append`, appends per patch one vertex-count line and
then that many `easting,northing,<third>` lines through `FileIoUtils.FormatDouble` with 3 decimals for
the coordinates and 5 for the third component. There is no header, no timestamp and no section identity.

What a patch is, from `SourceCode/GPS/Classes/CPatches.cs` at the same pin: `TurnMappingOn` opens a patch
and pushes the section colour as the first vec3, then the left/right points of the active sections;
`AddMappingPoint` appends further left/right pairs as mapping continues; `TurnMappingOff` closes it and
keeps it only when it holds more than four vertices. So a patch is one mapping-on-to-off interval of
reported section-edge positions -- the application's own coverage record. That is **not** evidence that
material was applied or that the machine physically moved; it is what the program mapped. It is equally
not a plan: the producer keeps planned geometry in the separate track and boundary files.

The third field is **not a heading**. Every coverage vertex is built as `new vec3(point.easting,
point.northing, 0)` in `CPatches.cs`, so the writer serialises a literal zero there; only the leading
colour vertex carries a meaningful third number (its blue channel).

Units come from the same pin's adopted evidence rather than from these bytes: `LocalPlane.cs` establishes
local **metre** coordinates (already cited by the adopted Boundary case). The plane **origin** is not in
this file -- the producer keeps a declared `StartFix` in a separate `Field.txt`
(`IO/FieldPlaneFiles.cs::LoadOrigin`), which this path does not read -- so nothing geodetic is produced.
There is no clock in the file at all; the caller supplies the capture microseconds.

Line terminators: this is a .NET desktop writer, so an ordinary saved file uses CRLF, while a file moved
through other tooling can carry LF. Both are read here. Only the terminator itself is removed -- the three
saved fields keep their exact text, and `source_document_sha256` still covers the original bytes, so the
same coverage saved with the two terminators digests differently while the read values are identical.
"""
import argparse
import csv
import hashlib
import io
import math
from pathlib import Path
import re
import sys

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
# `TurnMappingOff` keeps a patch only when its serialised vertex list holds more than four entries
# (colour plus at least four coordinates). A shorter patch in a saved file did not come from this
# serialised path, so it is refused. Only this pinned path is described; another writer or an older
# count convention is not covered by this statement.
MINIMUM_SERIALISED_VERTICES = 5
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
FIELDS = ["record_time_us", "source_document_sha256", "patch_index", "vertex_index",
          "patch_vertex_count", "agopen_easting_m_local", "agopen_northing_m_local",
          "agopen_vertex_third_field", "agopen_patch_colour_line_hex", "agopen_coverage_basis",
          "agopen_plane_basis", "agopen_third_field_basis", "agopen_identity_basis",
          "source_record_hex"]
COVERAGE_BASIS = ("REPORTED_MAPPING_SECTION_EDGE_COVERAGE_BETWEEN_ONE_MAPPING_ON_AND_MAPPING_OFF_"
                  "NOT_EVIDENCE_THAT_MATERIAL_WAS_APPLIED_OR_THAT_THE_MACHINE_MOVED_AND_NOT_A_PLAN")
# LocalPlane.cs establishes local metre coordinates at this pin (the adopted Boundary case cites it);
# the origin lives in a separate Field.txt that this path does not read.
PLANE_BASIS = ("LOCAL_METRE_PLANE_PER_LOCALPLANE_AT_THIS_PIN_WITH_THE_ORIGIN_IN_A_SEPARATE_FIELD_FILE_"
               "THIS_PATH_DOES_NOT_READ_SO_NEVER_GEODETIC_ROUNDED_TO_THREE_DECIMALS_BY_THE_WRITER")
THIRD_FIELD_BASIS = ("THE_PATCH_BUILDER_WRITES_A_LITERAL_ZERO_INTO_THE_VEC3_THIRD_COMPONENT_FOR_EVERY_"
                     "COVERAGE_VERTEX_SO_THIS_IS_NOT_A_HEADING_OR_ANY_MEASURED_QUANTITY")
IDENTITY_BASIS = ("NO_SECTION_INDEX_FIELD_OR_VEHICLE_IDENTITY_IS_WRITTEN_THE_LEADING_COLOUR_VERTEX_IS_A_"
                  "DISPLAY_ATTRIBUTE_AND_NOT_A_SECTION_IDENTIFIER")


def number(text, name):
    if not NUMBER.fullmatch(text):
        raise ValueError(f"reported {name} must be a plain decimal number")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"reported {name} must be finite")
    return value


def vertex(line, index):
    """Parse one saved vertex line, keeping its exact text so the source survives the conversion."""
    parts = line.split(",")
    if len(parts) != 3:
        raise ValueError(f"patch {index} vertex needs exactly three comma separated fields")
    values = [number(part, name) for part, name in
              zip(parts, ("easting", "northing", "third field"))]
    return values, line


def saved_lines(text):
    """Split on the writer's line terminator, LF or CRLF, removing only that terminator.

    Nothing else is trimmed: no whitespace strip, no reformat. A stray CR that is not a terminator stays
    in the field text and is refused by `number` rather than silently repaired.
    """
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def patches(text):
    """Split the saved file into (colour line, vertices) patches exactly as the writer laid them out."""
    lines = saved_lines(text)
    # A file written by this path ends with exactly one line terminator, which leaves exactly one empty
    # trailing element here. That one is the terminator; any further blank line is a blank line, and is
    # refused below rather than filtered into valid geometry.
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        # CreateEmpty writes a zero-byte file; that is the writer's own "no coverage" state, and it is
        # named rather than reported as a successful read of nothing.
        raise ValueError("saved sections file records no coverage patch")
    result, position = [], 0
    while position < len(lines):
        count_text = lines[position]
        if count_text == "":
            raise ValueError("blank line in the saved patches is not a record")
        if not re.fullmatch(r"[0-9]{1,9}", count_text):
            raise ValueError("patch vertex count must be a nonnegative integer")
        count = int(count_text)
        position += 1
        if count < MINIMUM_SERIALISED_VERTICES:
            raise ValueError("patch shorter than the writer's own serialised five-vertex minimum")
        if position + count > len(lines):
            raise ValueError("declared patch vertex count exceeds the saved lines")
        parsed = []
        for offset in range(count):
            line = lines[position + offset]
            if line == "":
                raise ValueError("blank line inside the saved patches is not a record")
            parsed.append(vertex(line, len(result)))
        position += count
        # The loader states the first vertex is the RGB colour; the writer emits it through the same
        # coordinate formatter, so it is separated here and never reported as a position.
        result.append((parsed[0][1], parsed[1:]))
    return result


def convert(text, capture_time_us):
    if type(capture_time_us) is not int or type(capture_time_us) is bool or \
            not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit nonnegative signed64 capture microseconds required")
    raw = text.encode("utf-8")
    if len(raw) > MAX_INPUT:
        raise ValueError("saved sections file exceeds input bound")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for patch_index, (colour_line, vertices) in enumerate(patches(text)):
        # The exact colour line as written, not a re-formatted triple; hex because the downstream CSV
        # contract is unquoted and the line contains commas.
        colour_hex = "hex:" + colour_line.encode("utf-8").hex()
        for vertex_index, ((easting, northing, third), line) in enumerate(vertices):
            writer.writerow({
                "record_time_us": capture_time_us, "source_document_sha256": digest,
                "patch_index": patch_index, "vertex_index": vertex_index,
                "patch_vertex_count": len(vertices),
                "agopen_easting_m_local": easting, "agopen_northing_m_local": northing,
                "agopen_vertex_third_field": third, "agopen_patch_colour_line_hex": colour_hex,
                "agopen_coverage_basis": COVERAGE_BASIS, "agopen_plane_basis": PLANE_BASIS,
                "agopen_third_field_basis": THIRD_FIELD_BASIS, "agopen_identity_basis": IDENTITY_BASIS,
                # The saved line itself without its terminator, otherwise unchanged: the writer's own
                # decimal formatting survives, and the file digest above still covers the raw bytes.
                "source_record_hex": "hex:" + line.encode("utf-8").hex()})
            if output.tell() > MAX_OUTPUT:
                raise ValueError("converted output exceeds bound")
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller clock; the saved file carries no time of its own")
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8", newline="") as stream:
            text = stream.read(MAX_INPUT + 1)
        converted = convert(text, args.capture_time_us)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"AgOpenGPS sections conversion failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
