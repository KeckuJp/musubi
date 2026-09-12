#!/usr/bin/env python3
"""Opt-in bounded JSONL parts using the unchanged recorded-position CSV converter.

No decoder, network, inferred units or reader/core change. Validate everything
before creating a new output directory; a manifest is written only after all parts.
The manifest supplies byte identity and record offsets, not source authentication.
"""
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

import convert_position_csv as single

MAX_INPUT = single.MAX_INPUT
MAX_PART_BYTES = single.MAX_INPUT
MAX_TOTAL_OUTPUT = 64 * 1024 * 1024
FAILURE = "position csv batch conversion failed; verify input, profile and a new output directory"
USAGE = "usage: convert_position_csv_batches.py INPUT PROFILE NEWDIRECTORY"


def convert_batches(raw, profile, max_part_bytes=MAX_PART_BYTES):
    """Return bounded JSONL parts in source order, retaining complete records.

    Input/record limits and semantics are the original converter's. The optional
    Python limit can only reduce a part's bound; the CLI does not expose tuning.
    The entire result is bounded to 64 MiB, not an unbounded streaming promise.
    """
    if type(max_part_bytes) is not int or not 1 <= max_part_bytes <= MAX_PART_BYTES:
        raise ValueError("invalid part limit")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_INPUT:
        raise ValueError("input limit")
    declared = single.checked_profile(profile)
    header, _, rows = single.read_table(raw)
    parts, current, current_size, total_size = [], [], 0, 0
    for row in rows:
        # Re-quoting retains parsed header/cell strings, not original CSV spelling.
        # Use the existing validation and conversion for every complete row.
        table = io.StringIO(newline="")
        writer = csv.writer(table, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerow(row)
        encoded = single.convert(table.getvalue().encode("utf-8"), declared)
        if len(encoded) > max_part_bytes:
            raise ValueError("record exceeds part limit")
        total_size += len(encoded)
        if total_size > MAX_TOTAL_OUTPUT:
            raise ValueError("total output limit")
        if current_size + len(encoded) > max_part_bytes:
            parts.append(b"".join(current))
            current, current_size = [], 0
        current.append(encoded)
        current_size += len(encoded)
    if current:
        parts.append(b"".join(current))
    return parts


def main(argv):
    if len(argv) != 4:
        print(USAGE, file=sys.stderr)
        return 1
    try:
        raw = single.bounded_read(Path(argv[1]), MAX_INPUT)
        profile_raw = single.bounded_read(Path(argv[2]), single.MAX_PROFILE)
        profile = json.loads(profile_raw.decode("utf-8"),
                             object_pairs_hook=single.unique_object,
                             parse_constant=single.invalid_constant)
        parts = convert_batches(raw, profile)
        target = Path(argv[3])
        # Atomic creation refuses existing files, directories and symlinks. Only
        # the selected new directory is written; parents are never created here.
        target.mkdir()
        manifest = {"schema": "position-csv-batch/v1",
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "profile_sha256": hashlib.sha256(profile_raw).hexdigest(),
                    "records": 0, "parts": []}
        for index, part in enumerate(parts, 1):
            name = f"part-{index:06d}.jsonl"
            count = part.count(b"\n")
            with (target / name).open("xb") as output:
                output.write(part)
            manifest["parts"].append({"name": name,
                                      "first_record_index": manifest["records"],
                                      "records": count, "bytes": len(part),
                                      "sha256": hashlib.sha256(part).hexdigest()})
            manifest["records"] += count
        # Last file. Remove our own incomplete completion marker on write failure.
        # Consumers still verify all counts/offsets/hashes: existence is not proof
        # against process termination, later changes or filesystem failure.
        marker = target / "manifest.json"
        created = False
        try:
            with marker.open("xb") as output:
                created = True
                output.write((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        except OSError:
            if created:
                marker.unlink(missing_ok=True)
            raise
    except (OSError, ValueError, TypeError, RecursionError):
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
