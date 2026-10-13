#!/usr/bin/env python3
"""Bind saved FarmBot image resources to supplied qualified image observations, offline."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_image_original_binding as images
from scripts.convert_image_metadata import unique_object, invalid_constant

SOURCE = "6db0ea3eb0059424573da8399d082b445624b24c"
FIELDS = {
    "farmbot_resource_id": "reported_image_resource_id_not_camera_identity",
    "farmbot_device_id": "reported_device_id_not_authenticated_hardware",
    "farmbot_created_at": "resource_creation_timestamp_not_exposure_time",
    "farmbot_updated_at": "resource_update_timestamp_not_exposure_time",
    "farmbot_attachment_processed_at": "reported_attachment_processing_timestamp_not_exposure_time",
    "farmbot_resources_sha256": "sha256_exact_saved_resource_list",
    "farmbot_resource_index": "zero_based_saved_resource_array_index",
    "farmbot_mapping_sha256": "sha256_exact_caller_association_declaration",
}


def parse(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)


def identifier(value):
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError("invalid resource identity")
    return value


def timestamp(value, nullable=False):
    # Retain original lifecycle text, not a substitute capture clock.
    if value is None and nullable:
        return ""
    if not isinstance(value, str) or not value or len(value.encode()) > 128:
        raise ValueError("missing or invalid lifecycle timestamp")
    from datetime import datetime
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("lifecycle timestamp needs explicit offset")
    return value


def convert(resources, export, mapping, image_root, capture_time_us, *, kind="exif", composite_time=None):
    if len(resources) > images.EXPORT_LIMIT or len(mapping) > images.MAP_LIMIT:
        raise ValueError("resource or mapping exceeds limit")
    records, declaration = parse(resources), parse(mapping)
    if not isinstance(records, list) or not 0 < len(records) <= 4096:
        raise ValueError("resource list invalid")
    keys = {"schema_version", "source_commit", "resources_sha256", "export_sha256", "entries"}
    if not isinstance(declaration, dict) or set(declaration) != keys:
        raise ValueError("mapping shape invalid")
    if type(declaration["schema_version"]) is not int or declaration["schema_version"] != 1 or declaration["source_commit"] != SOURCE:
        raise ValueError("unqualified resource source")
    resource_sha, mapping_sha = hashlib.sha256(resources).hexdigest(), hashlib.sha256(mapping).hexdigest()
    if declaration["resources_sha256"] != resource_sha:
        raise ValueError("resource digest mismatch")
    lookup = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("meta"), dict):
            raise ValueError("invalid saved Image resource")
        resource_id, device_id = identifier(record.get("id")), identifier(record.get("device_id"))
        if resource_id in lookup:
            raise ValueError("duplicate image resource ID")
        if not isinstance(record.get("attachment_url"), str) or not record["attachment_url"]:
            raise ValueError("missing image resource URL declaration")
        values = dict(farmbot_resource_id=resource_id, farmbot_device_id=device_id,
                      farmbot_created_at=timestamp(record.get("created_at")),
                      farmbot_updated_at=timestamp(record.get("updated_at")),
                      farmbot_attachment_processed_at=timestamp(record.get("attachment_processed_at"), True),
                      farmbot_resources_sha256=resource_sha, farmbot_resource_index=index,
                      farmbot_mapping_sha256=mapping_sha)
        lookup[resource_id] = values
    entries = declaration["entries"]
    if not isinstance(entries, list) or not 0 < len(entries) <= 4096:
        raise ValueError("empty or invalid binding selection")
    selected, used_resources, originals = {}, set(), []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"source_image_index", "resource_id", "device_id", "path", "original_sha256"}:
            raise ValueError("invalid binding entry")
        index = entry["source_image_index"]
        resource_id = identifier(entry["resource_id"])
        device_id = identifier(entry["device_id"])
        if type(index) is not int or index < 1 or index in selected or resource_id in used_resources:
            raise ValueError("duplicate or invalid binding")
        if resource_id not in lookup or lookup[resource_id]["farmbot_device_id"] != device_id:
            raise ValueError("resource or device identity mismatch")
        sha = entry["original_sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid original digest")
        selected[index] = lookup[resource_id], sha
        used_resources.add(resource_id)
        originals.append({"source_image_index": index, "path": entry["path"]})
    original_mapping = json.dumps(dict(schema_version=1, export_sha256=declaration["export_sha256"], entries=originals)).encode()
    text, report, profile = images.convert(export, original_mapping, image_root, capture_time_us,
                                            kind=kind, composite_time=composite_time)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if len(rows) != len(selected):
        raise ValueError("every export entry must be bound")
    for row in rows:
        index = int(row["source_image_index"])
        if index not in selected or row["original_binding_status"] != "CALLER_ASSOCIATION_FILE_HASHED":
            raise ValueError("missing supplied original or resource binding")
        values, expected_sha = selected[index]
        if row["original_image_sha256"] != expected_sha:
            raise ValueError("original digest mismatch")
        row.update(values)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames + list(FIELDS))
    writer.writeheader(); writer.writerows(rows)
    text = output.getvalue()
    if len(text.encode()) > images.OUTPUT_LIMIT:
        raise ValueError("bound output exceeds limit")
    profile += "\n" + "\n".join(f'{name} = "{unit}"' for name, unit in FIELDS.items()) + "\n"
    report["farmbot"] = dict(source_commit=SOURCE, source_resources=len(records), bound_resources=len(selected),
                             unselected_resources=len(records) - len(selected), resources_sha256=resource_sha,
                             mapping_sha256=mapping_sha, original_mapping_sha256=hashlib.sha256(original_mapping).hexdigest(),
                             basis="CALLER_ASSOCIATION_NOT_VERIFIED_IMAGE_EXTRACTION_OR_CAPTURE",
                             unknown_fields="retained_in_exact_source_resources_not_interpreted")
    # Preserve the derived declaration referenced by inherited profile fields.
    report["farmbot"]["derived_original_mapping"] = original_mapping.decode()
    return text, report, profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("resources", "export", "mapping", "image_root", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--kind", choices=("exif", "m3m"), default="exif")
    parser.add_argument("--composite-time", choices=("exiftool-13.59",))
    args = parser.parse_args()
    try:
        if args.output.resolve().is_relative_to(args.image_root.resolve()):
            raise ValueError("output must be outside originals")
        resources = images.read_input(args.resources, images.EXPORT_LIMIT)
        export = images.read_input(args.export, images.EXPORT_LIMIT)
        mapping = images.read_input(args.mapping, images.MAP_LIMIT)
        text, report, profile = convert(resources, export, mapping, args.image_root, args.capture_time_us,
                                        kind=args.kind, composite_time=args.composite_time)
        args.output.mkdir()
        for name, data in (("resources", resources), ("export", export), ("mapping", mapping)):
            (args.output / f"source-{name}.json").write_bytes(data)
        for name, data in (("observations.csv", text), ("profile.toml", profile),
                           ("report.json", json.dumps(report, indent=2) + "\n")):
            (args.output / name).write_text(data, encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, OverflowError):
        parser.exit(2, "Saved FarmBot image association failed; no complete binding claim\n")


if __name__ == "__main__":
    main()
