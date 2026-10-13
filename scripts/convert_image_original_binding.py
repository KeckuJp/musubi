#!/usr/bin/env python3
"""Associate supplied original bytes with saved metadata; never verify capture authenticity."""
import argparse
from contextlib import ExitStack
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_image_metadata, convert_m3m_capture

ROOT = Path(__file__).resolve().parents[1]
MAP_LIMIT = 2 * 1024 * 1024
EXPORT_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 32 * 1024 * 1024
FIELDS = {"original_binding_status": "caller_association_not_metadata_extraction_or_authenticity_verification",
          "original_image_sha256": "sha256_of_supplied_original_regular_file_bytes_not_image_content_validation",
          "original_image_bytes": "bytes_in_supplied_file",
          "binding_mapping_sha256": "sha256_exact_caller_mapping_document",
          "binding_mapping_index": "zero_based_mapping_array_index_blank_for_unmapped_export_entry"}


class MissingOriginal(Exception):
    pass


def initial_entry_stat(name, parent):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise MissingOriginal() from exc


def identity(value):
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def budget(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("invalid byte budget")
    return value


def relative_path(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError("invalid original path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in value or "\x00" in value or PurePosixPath(value).is_absolute():
        raise ValueError("original path must be a plain relative path")
    return parts


def hash_original(root, parts, maximum):
    """FD-anchored no-follow traversal; before/after checks catch ordinary replacement."""
    with ExitStack() as stack:
        root_before = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_before.st_mode):
            raise ValueError("root must be a non-symlink directory")
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stack.callback(os.close, parent)
        if identity(os.fstat(parent)) != identity(root_before):
            raise ValueError("root changed while opening")
        links = []
        for part in parts[:-1]:
            before = initial_entry_stat(part, parent)
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError("path parent must be a directory without links")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, child)
            if identity(os.fstat(child)) != identity(before):
                raise ValueError("path parent changed while opening")
            links.append((parent, part, before))
            parent = child
        before = initial_entry_stat(parts[-1], parent)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("original must be a regular file without links")
        if before.st_size > maximum:
            raise ValueError("original byte budget exceeded")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        if identity(os.fstat(fd)) != identity(before):
            raise ValueError("original changed while opening")
        digest, count = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, maximum - count + 1))
            if not chunk:
                break
            count += len(chunk)
            if count > maximum:
                raise ValueError("original grew beyond budget")
            digest.update(chunk)
        if count != before.st_size or identity(os.fstat(fd)) != identity(before):
            raise ValueError("original changed while hashing")
        links.append((parent, parts[-1], before))
        for ancestor, name, old in links:
            if identity(os.stat(name, dir_fd=ancestor, follow_symlinks=False)) != identity(old):
                raise ValueError("original path changed while hashing")
        if identity(os.stat(root, follow_symlinks=False)) != identity(root_before):
            raise ValueError("root changed while hashing")
        return digest.hexdigest(), count, (before.st_dev, before.st_ino)


def convert(export, mapping, image_root, capture_time_us, *, kind="exif", composite_time=None,
            max_file_bytes=512 * 1024 * 1024, max_total_bytes=16 * 1024**3):
    budget(max_file_bytes)
    budget(max_total_bytes)
    if len(export) > EXPORT_LIMIT or len(mapping) > MAP_LIMIT:
        raise ValueError("export or mapping exceeds bound")
    declaration = json.loads(mapping.decode("utf-8"), object_pairs_hook=convert_image_metadata.unique_object,
                             parse_constant=convert_image_metadata.invalid_constant)
    if not isinstance(declaration, dict) or set(declaration) != {"schema_version", "export_sha256", "entries"}:
        raise ValueError("mapping shape invalid")
    if type(declaration["schema_version"]) is not int or declaration["schema_version"] != 1:
        raise ValueError("mapping version invalid")
    if declaration["export_sha256"] != hashlib.sha256(export).hexdigest():
        raise ValueError("mapping export digest mismatch")
    if kind == "exif":
        text, report = convert_image_metadata.convert(export, capture_time_us, composite_time=composite_time)
        case = "image-metadata"
    elif kind == "m3m" and composite_time is None:
        text, report = convert_m3m_capture.convert(export, capture_time_us)
        case = "m3m-capture"
    else:
        raise ValueError("unsupported metadata path")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    entries = declaration["entries"]
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ValueError("mapping count invalid")
    selected, paths = {}, set()
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"source_image_index", "path"}:
            raise ValueError("mapping entry invalid")
        index = entry["source_image_index"]
        if type(index) is not int or not 1 <= index <= len(rows) or index in selected:
            raise ValueError("mapping index invalid or repeated")
        parts = relative_path(entry["path"])
        if entry["path"] in paths:
            raise ValueError("mapping repeats original path")
        paths.add(entry["path"])
        selected[index] = ordinal, parts
    binding = {"source_entries": len(rows), "mapping_entries": len(entries), "bound_entries": 0,
               "missing_entries": 0, "unmapped_entries": 0, "read_bytes": 0,
               "basis": "CALLER_ASSOCIATION_NOT_VERIFIED_EXTRACTION_OR_CAPTURE_AUTHENTICITY",
               "max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes}
    digest = hashlib.sha256(mapping).hexdigest()
    identities = set()
    for row in rows:
        index = int(row["source_image_index"])
        row.update(binding_mapping_sha256=digest, original_binding_status="UNMAPPED_EXPORT_ENTRY")
        if index not in selected:
            binding["unmapped_entries"] += 1
            continue
        ordinal, parts = selected[index]
        row["binding_mapping_index"] = ordinal
        remaining = max_total_bytes - binding["read_bytes"]
        try:
            sha, count, file_identity = hash_original(image_root, parts, min(max_file_bytes, remaining))
        except MissingOriginal:
            row["original_binding_status"] = "MISSING_SUPPLIED_FILE"
            binding["missing_entries"] += 1
            continue
        if file_identity in identities:
            raise ValueError("mapping repeats an original file identity")
        identities.add(file_identity)
        row.update(original_binding_status="CALLER_ASSOCIATION_FILE_HASHED",
                   original_image_sha256=sha, original_image_bytes=count)
        binding["bound_entries"] += 1
        binding["read_bytes"] += count
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames + list(FIELDS))
    writer.writeheader()
    writer.writerows(rows)
    text = output.getvalue()
    if len(text.encode()) > OUTPUT_LIMIT:
        raise ValueError("bound output exceeds limit")
    report["binding"] = binding
    profile = (ROOT / "profiles/declared" / case / "profile.toml").read_text()
    # Existing profiles end in [units]; append new field meanings without copying its semantics.
    if profile.rsplit("[", 1)[1].split("]", 1)[0] != "units":
        raise ValueError("adopted profile layout changed")
    profile = "\n".join(f'profile_id = "image_original_binding_{kind}_v1"' if line.startswith("profile_id =") else line
                        for line in profile.splitlines()) + "\n"
    profile += "\n" + "\n".join(f'{name} = "{unit}"' for name, unit in FIELDS.items()) + "\n"
    return text, report, profile


def read_input(path, limit):
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("saved input must be bounded regular file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError("saved input changed")
        data = stream.read(limit + 1)
        if len(data) > limit or identity(os.fstat(stream.fileno())) != identity(before) or identity(os.stat(path, follow_symlinks=False)) != identity(before):
            raise ValueError("saved input changed")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("mapping", type=Path)
    parser.add_argument("image_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--kind", choices=("exif", "m3m"), default="exif")
    parser.add_argument("--composite-time", choices=("exiftool-13.59",))
    parser.add_argument("--max-file-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=16 * 1024**3)
    args = parser.parse_args()
    try:
        if args.output.resolve().is_relative_to(args.image_root.resolve()):
            raise ValueError("output must be outside original root")
        export, mapping = read_input(args.export, EXPORT_LIMIT), read_input(args.mapping, MAP_LIMIT)
        text, report, profile = convert(export, mapping, args.image_root, args.capture_time_us, kind=args.kind,
            composite_time=args.composite_time, max_file_bytes=args.max_file_bytes, max_total_bytes=args.max_total_bytes)
        args.output.mkdir()
        for name, data in (("source-export.json", export), ("source-mapping.json", mapping)):
            (args.output / name).write_bytes(data)
        for name, data in (("observations.csv", text), ("profile.toml", profile),
                           ("report.json", json.dumps(report, indent=2) + "\n")):
            (args.output / name).write_text(data, encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        parser.exit(2, "Saved original-image association failed; no complete association claim\n")


if __name__ == "__main__":
    main()
