#!/usr/bin/env python3
"""Caller-declared fixed-offset still/flight clock relation, never inferred synchronization."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_image_original_binding as original
from scripts import convert_image_metadata as metadata
from scripts.convert_gpx_recorded import utc_microseconds
from scripts.convert_recorded_angles_csv import convert as angles

RELATION = "CALLER_SAME_EVENT_ANCHOR_EQUAL_CLOCK_RATE_CONSTANT_OFFSET_NOT_VERIFIED"
LABELS = ("session_id", "camera_id", "flight_entity_id", "image_clock_id", "flight_clock_id", "citation")
KEYS = {"schema", "export_sha256", "mapping_sha256", "flight_sha256", "relation", "image_time_source",
        "anchor_image_index", "anchor_flight_index", "valid_image_start_us", "valid_image_end_us",
        "max_abs_delta_us", *LABELS}
UNITS = {"flight_connection_status": "declared_clock_connection_or_explicit_withheld_not_measured_sync",
         "derived_flight_time_us": "us_on_caller_declared_flight_clock_under_equal_rate_constant_offset",
         "image_source_epoch_us": "us_from_selected_reported_image_clock_not_verified_UTC",
         "flight_source_sha256": "sha256_exact_supplied_Plane_decoded_ATT_CSV",
         "clock_declaration_sha256": "sha256_exact_caller_clock_relation_declaration",
         "flight_relation": "caller_relation_not_inferred_or_authenticated",
         "anchor_image_index": "one_based_export_entry",
         "anchor_flight_index": "one_based_flight_data_row",
         "anchor_image_time_us": "selected_reported_image_anchor_us",
         "anchor_flight_time_us": "reported_flight_anchor_boot_us",
         "valid_image_start_us": "caller_declared_inclusive_lower_validity_limit",
         "valid_image_end_us": "caller_declared_inclusive_upper_validity_limit",
         "max_abs_delta_us": "caller_declared_maximum_distance_from_anchor_not_accuracy",
         "flight_exact_sample_index": "one_based_flight_row_only_when_unique_timestamp_exactly_matches",
         "flight_roll_rad": "rad_reported_at_exact_matching_sample_no_interpolation",
         "flight_pitch_rad": "rad_reported_at_exact_matching_sample_no_interpolation",
         "flight_yaw_rad": "rad_reported_at_exact_matching_sample_no_interpolation",
         **{name + "_hex": "utf8_hex_caller_declared_" + name + "_not_verified" for name in LABELS}}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def integer(value, low=0):
    if type(value) is not int or not low <= value < 2**63:
        raise ValueError("invalid bounded relation integer")
    return value


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError("explicit bounded identity/clock/citation required")
    return "hex:" + value.encode().hex()


def image_time(row, source):
    if source == "exiftool-composite":
        if row.get("image_capture_time_status") != "EXPLICIT_OFFSET_DERIVED":
            return None
        return integer(int(row["image_capture_epoch_us"]))
    day, time = row.get("gps_date_stamp_utc"), row.get("gps_time_of_day_utc")
    if not day or not time:
        return None
    try:
        return integer(utc_microseconds(day.replace(":", "-") + "T" + time + "Z"))
    except ValueError:
        # Original lexical fields/basis remain: leap seconds and sub-us precision are not rounded.
        return None


def convert(export, mapping, image_root, flight, declaration, capture_time_us, *, kind="exif",
            max_file_bytes=512 * 1024 * 1024, max_total_bytes=16 * 1024**3):
    if len(declaration) > 16 * 1024 or len(flight) > 16 * 1024 * 1024:
        raise ValueError("flight/declaration exceeds bound")
    declared = json.loads(declaration.decode(), object_pairs_hook=metadata.unique_object,
                          parse_constant=metadata.invalid_constant)
    if not isinstance(declared, dict) or set(declared) != KEYS or declared["schema"] != "still-flight-clock-v1" or declared["relation"] != RELATION:
        raise ValueError("closed explicit relation required")
    for key, raw in (("export_sha256", export), ("mapping_sha256", mapping), ("flight_sha256", flight)):
        if declared[key] != digest(raw):
            raise ValueError("relation digest mismatch")
    labels = {name + "_hex": label(declared[name]) for name in LABELS}
    for key in ("anchor_image_index", "anchor_flight_index", "max_abs_delta_us"):
        integer(declared[key], 1)
    start, stop = integer(declared["valid_image_start_us"]), integer(declared["valid_image_end_us"])
    if stop < start or declared["image_time_source"] not in ("gps-utc", "exiftool-composite"):
        raise ValueError("invalid image clock/validity declaration")
    composite = "exiftool-13.59" if declared["image_time_source"] == "exiftool-composite" else None
    photo_csv, report, profile = original.convert(export, mapping, image_root, capture_time_us,
        kind=kind, composite_time=composite, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes)
    photo_reader = csv.DictReader(io.StringIO(photo_csv))
    photos = list(photo_reader)
    flight_csv = angles(flight.decode(), "plane-4.3.1-decoded-att")
    states = list(csv.DictReader(io.StringIO(flight_csv)))
    ai, af = declared["anchor_image_index"], declared["anchor_flight_index"]
    if ai > len(photos) or af > len(states):
        raise ValueError("anchor entry absent")
    if photos[ai-1]["original_binding_status"] != "CALLER_ASSOCIATION_FILE_HASHED":
        raise ValueError("anchor image is not bound to supplied original")
    image_anchor = image_time(photos[ai-1], declared["image_time_source"])
    if image_anchor is None or not start <= image_anchor <= stop:
        raise ValueError("anchor clock unresolved or outside validity")
    flight_anchor = int(states[af-1]["record_time_us"])
    samples = {}
    for index, state in enumerate(states, 1):
        samples.setdefault(int(state["record_time_us"]), []).append((index, state))
    lower, upper = min(samples), max(samples)
    common = dict(flight_source_sha256=digest(flight), clock_declaration_sha256=digest(declaration),
        flight_relation=RELATION, anchor_image_index=ai, anchor_flight_index=af,
        anchor_image_time_us=image_anchor, anchor_flight_time_us=flight_anchor,
        valid_image_start_us=start, valid_image_end_us=stop, max_abs_delta_us=declared["max_abs_delta_us"], **labels)
    counts = {"source_entries": len(photos), "connected_entries": 0, "withheld_entries": 0,
              "exact_sample_entries": 0, "statuses": {}, "flight_rows": len(states),
              "basis": RELATION, "declaration": declared, "primary_clock_unchanged": True}
    for row in photos:
        row.update(common)
        image_us = image_time(row, declared["image_time_source"])
        if image_us is not None:
            row["image_source_epoch_us"] = image_us
        if row["original_binding_status"] != "CALLER_ASSOCIATION_FILE_HASHED":
            status = "WITHHELD_ORIGINAL_UNBOUND"
        elif image_us is None:
            status = "WITHHELD_IMAGE_CLOCK_UNRESOLVED"
        elif not start <= image_us <= stop or abs(image_us-image_anchor) > declared["max_abs_delta_us"]:
            status = "WITHHELD_DECLARED_INTERVAL"
        else:
            mapped = flight_anchor + image_us - image_anchor
            if not 0 <= mapped < 2**63 or not lower <= mapped <= upper:
                status = "WITHHELD_FLIGHT_RANGE"
            else:
                row["derived_flight_time_us"] = mapped
                exact = samples.get(mapped, [])
                status = "CLOCK_CONNECTED_NO_EXACT_SAMPLE"
                if len(exact) > 1:
                    status = "CLOCK_CONNECTED_AMBIGUOUS_SAMPLE"
                elif len(exact) == 1:
                    status = "CLOCK_CONNECTED_EXACT_SAMPLE"
                    index, state = exact[0]
                    row["flight_exact_sample_index"] = index
                    for axis in ("roll", "pitch", "yaw"):
                        row["flight_" + axis + "_rad"] = state["reported_" + axis + "_rad"]
                    counts["exact_sample_entries"] += 1
        row["flight_connection_status"] = status
        counts["withheld_entries" if status.startswith("WITHHELD") else "connected_entries"] += 1
        counts["statuses"][status] = counts["statuses"].get(status, 0) + 1
    counts["all_entries_clock_connected"] = counts["withheld_entries"] == 0
    report["flight_binding"] = counts
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=photo_reader.fieldnames + list(UNITS))
    writer.writeheader()
    writer.writerows(photos)
    text = output.getvalue()
    if len(text.encode()) > 32 * 1024 * 1024:
        raise ValueError("connected output exceeds bound")
    profile = "\n".join('profile_id = "image_flight_declared_v1"' if line.startswith("profile_id =") else line
                        for line in profile.splitlines()) + "\n"
    profile += "\n".join(f'{key} = "{unit}"' for key, unit in UNITS.items()) + "\n"
    flight_profile = (original.ROOT / "profiles/declared/recorded-angles/profile.toml").read_text()
    return text, report, profile, flight_csv, flight_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("export", "mapping", "image_root", "flight", "declaration", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--kind", choices=("exif", "m3m"), default="exif")
    parser.add_argument("--max-file-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=16 * 1024**3)
    args = parser.parse_args()
    try:
        if args.output.resolve().is_relative_to(args.image_root.resolve()):
            raise ValueError("output must be outside original root")
        inputs = [original.read_input(path, limit) for path, limit in (
            (args.export, original.EXPORT_LIMIT), (args.mapping, original.MAP_LIMIT),
            (args.flight, 16*1024*1024), (args.declaration, 16*1024))]
        export, mapping, flight, declaration = inputs
        text, report, profile, flight_csv, flight_profile = convert(export, mapping, args.image_root,
            flight, declaration, args.capture_time_us, kind=args.kind,
            max_file_bytes=args.max_file_bytes, max_total_bytes=args.max_total_bytes)
        args.output.mkdir()
        for name, raw in zip(("source-export.json", "source-mapping.json", "source-flight.csv", "source-declaration.json"), inputs):
            (args.output/name).write_bytes(raw)
        for name, value in (("observations.csv",text),("profile.toml",profile),("flight.csv",flight_csv),
                            ("flight-profile.toml",flight_profile),("report.json",json.dumps(report,indent=2)+"\n")):
            (args.output/name).write_text(value, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        parser.exit(2,"Saved image/flight connection failed; no complete session claim\n")


if __name__ == "__main__":
    main()
