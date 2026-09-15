#!/usr/bin/env python3
"""Observe one local filesystem entry without reading its contents or following its leaf symlink."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import stat
import time


def capture(path):
    if os.name != "posix":
        raise ValueError("only POSIX filesystem time semantics are qualified")
    supplied = os.fspath(path)
    if not isinstance(supplied, str):
        raise ValueError("UTF-8 text path required")
    encoded = supplied.encode("utf-8")
    if not encoded or len(encoded) > 4096 or "\x00" in supplied:
        raise ValueError("path outside finite scope")
    # POSIX trailing slash can dereference a symlink even with lstat.
    if supplied != "/" and supplied.endswith("/"):
        raise ValueError("trailing slash is outside leaf-no-follow scope")
    try:
        metadata = os.lstat(supplied)
    except FileNotFoundError:
        metadata = None
    # Other OS errors must not be represented as evidence of absence.
    observed_ns = time.time_ns()
    if not 0 <= observed_ns < 2**63:
        raise ValueError("capture clock outside common range")
    kind = "ABSENT"
    if metadata is not None:
        kind = ("REGULAR_FILE" if stat.S_ISREG(metadata.st_mode) else
                "DIRECTORY" if stat.S_ISDIR(metadata.st_mode) else
                "SYMLINK_NOT_FOLLOWED" if stat.S_ISLNK(metadata.st_mode) else "OTHER_ENTRY")
    row = {"record_time_us": observed_ns // 1000, "capture_clock_unix_ns": observed_ns,
           "capture_clock_basis": "LOCAL_SYSTEM_REPORTED_NOT_AUTHENTICATED",
           "source_path_hex": "hex:" + encoded.hex(), "entry_present": int(metadata is not None),
           "entry_kind": kind, "regular_file_present": int(kind == "REGULAR_FILE"),
           "regular_file_size_bytes": None, "filesystem_mtime_ns": None, "filesystem_ctime_ns": None,
           "filesystem_time_basis": "OS_REPORTED_NOT_CONTENT_OR_CREATION_TIME"}
    if metadata is not None:
        for name, value in (("filesystem_mtime_ns", metadata.st_mtime_ns),
                            ("filesystem_ctime_ns", metadata.st_ctime_ns)):
            if type(value) is not int or not -(2**63) <= value < 2**63:
                raise ValueError("filesystem timestamp outside common range")
            row[name] = value
        if kind == "REGULAR_FILE":
            if not 0 <= metadata.st_size < 2**63:
                raise ValueError("file size outside common range")
            row["regular_file_size_bytes"] = metadata.st_size
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(row), lineterminator="\n")
    writer.writeheader(); writer.writerow(row)
    return output.getvalue(), {"observations": 1, "content_read": False,
        "leaf_symlink_followed": False, "atomic_with_later_reads": False,
        "scope": "one local entry; existence is not content validity or ingestion success"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        output, report = capture(args.input)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        parser.exit(2, "file metadata observation failed; no absence or success claim\n")


if __name__ == "__main__":
    main()
