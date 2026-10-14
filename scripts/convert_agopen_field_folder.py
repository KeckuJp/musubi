#!/usr/bin/env python3
"""Read a supplied fixed-writer field folder; never acquire or operate a device."""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import stat

import convert_gpx_recorded as geometry
import convert_agopen_sections as sections

PIN = "21be26aa58d09b9b5abacc715d0abcf4a021f1db"
FIELDS = ["record_time_us", "declared_origin_latitude_deg", "declared_origin_longitude_deg",
          "source_sha256", "source_timestamp_hex", "coordinate_basis"]
ARTIFACTS = {"Boundary.txt": (geometry.convert_boundary, "boundary-profile.toml"),
             "TrackLines.txt": (geometry.convert_track, "track-profile.toml"),
             "RecPath.txt": (geometry.convert_recpath, "recpath-profile.toml"),
             "Sections.txt": (None, "agopen-sections/profile.toml")}


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def read_regular(path):
    # Reject symbolic links, directories and changing files; inspect the same opened descriptor.
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("regular nonsymlink saved file required")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > geometry.INPUT_LIMIT:
            raise ValueError("bounded regular saved file required")
        raw = stream.read(geometry.INPUT_LIMIT + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise ValueError("saved input changed while reading")
        if len(raw) != before.st_size:
            raise ValueError("saved input read incomplete")
        return raw


def origin(raw, capture):
    """FieldPlaneFiles.Save nine-line layout, not arbitrary LoadOrigin permissiveness."""
    lines = raw.decode("utf-8-sig").splitlines()
    if len(lines) != 9 or lines[1:8] != ["$FieldDir", "FieldNew", "$Offsets", "0,0",
                                        "Convergence", "0", "StartFix"] or not lines[0]:
        raise ValueError("fixed FieldPlaneFiles.Save layout required")
    parts = lines[8].split(",")
    if len(parts) != 2:
        raise ValueError("exact StartFix latitude/longitude required")
    lat = geometry.decimal_value(parts[0], -90, 90, scientific=True)
    lon = geometry.decimal_value(parts[1], -180, 180, scientific=True)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(FIELDS)
    writer.writerow([capture, lat, lon, digest(raw), "hex:" + lines[0].encode().hex(),
                     "DECLARED_WGS84_ORIGIN_NOT_TRANSFORMED_LOCAL_GEOMETRY"])
    return output.getvalue()


def declared_world(north, east, latitude, longitude, drift):
    """Pinned LocalPlane inverse direction; caller drift is not inferred saved state."""
    values = (north, east, latitude, longitude, *drift)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError("finite coordinates and caller drift required")
    if not -90 < latitude < 90 or not -180 <= longitude <= 180:
        raise ValueError("nonpolar bounded origin required for declared transformation")
    rad = math.radians(latitude)
    per_lat = 111132.92 - 559.82 * math.cos(2 * rad) + 1.175 * math.cos(4 * rad) - .0023 * math.cos(6 * rad)
    lat = latitude + (north + drift[0]) / per_lat
    if not math.isfinite(lat) or not -90 < lat < 90:
        raise ValueError("derived latitude outside nonpolar range")
    rad = math.radians(lat)
    per_lon = 111412.84 * math.cos(rad) - 93.5 * math.cos(3 * rad) + .118 * math.cos(5 * rad)
    if per_lon < .000001:
        raise ValueError("longitude scale is singular near pole")
    lon = longitude + (east + drift[1]) / per_lon
    if not math.isfinite(lon) or not -180 <= lon <= 180:
        raise ValueError("derived longitude outside range; no inferred wrapping")
    return lat, lon


def derived_csv(text, latitude, longitude, drift):
    reader = csv.DictReader(io.StringIO(text))
    fields = list(reader.fieldnames)
    extra = ["derived_latitude_deg", "derived_longitude_deg", "applied_drift_north_m",
             "applied_drift_east_m", "declared_origin_latitude_deg", "declared_origin_longitude_deg",
             "world_transform_basis"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields + extra, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        north = float(row.get("northing_m", row.get("agopen_northing_m_local")))
        east = float(row.get("easting_m", row.get("agopen_easting_m_local")))
        lat, lon = declared_world(north, east, latitude, longitude, drift)
        row.update(dict(zip(extra, [lat, lon, drift[0], drift[1], latitude, longitude,
            "PINNED_LOCALPLANE_WITH_CALLER_DECLARED_DRIFT_NOT_VERIFIED_RUNTIME_OR_GEODETIC_ACCURACY"])))
        writer.writerow(row)
        if output.tell() > geometry.OUTPUT_LIMIT:
            raise ValueError("derived output exceeds existing bound")
    return output.getvalue()


def convert_folder(folder, capture, *, drift=None):
    if type(capture) is not int or not 0 <= capture < 2**63:
        raise ValueError("explicit nonnegative signed64 capture microseconds required")
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("supplied nonsymlink directory required")
    originals = {"Field.txt": read_regular(folder / "Field.txt")}
    outputs = {"Field.txt.csv": origin(originals["Field.txt"], capture)}
    origin_row = next(csv.DictReader(io.StringIO(outputs["Field.txt.csv"])))
    latitude, longitude = (float(origin_row[key]) for key in
                            ("declared_origin_latitude_deg", "declared_origin_longitude_deg"))
    if drift is not None:
        if not isinstance(drift, (tuple, list)) or len(drift) != 2:
            raise ValueError("explicit north/east drift pair required")
        declared_world(0, 0, latitude, longitude, drift)
    report = {"writer_pin": PIN, "association_basis": "CALLER_SUPPLIED_DIRECTORY_NOT_AUTHENTICATED_SESSION",
              "clock_basis": "UNKNOWN_CALLER_CAPTURE_SOURCE_TIMESTAMP_HAS_NO_ZONE",
              "geometry_transform": "NOT_PERFORMED", "artifacts": {}, "unselected_entries": []}
    report["geometry_transform"] = ("NOT_EVALUATED_DRIFT_NOT_SUPPLIED" if drift is None else
                                    "PINNED_LOCALPLANE_WITH_CALLER_DECLARED_DRIFT")
    report["drift_basis"] = "CALLER_DECLARED_NORTH_EAST_METRES_IN_SOURCE_LOCAL_PLANE_NOT_SAVED_RUNTIME_PROOF"
    report["applied_drift_north_east_m"] = drift
    for name, (converter, profile) in ARTIFACTS.items():
        path = folder / name
        if not path.exists() and not path.is_symlink():
            report["artifacts"][name] = {"state": "NOT_SUPPLIED"}
            continue
        raw = read_regular(path)
        originals[name] = raw
        if converter is None:
            result = {}
            converted = sections.convert(raw.decode("utf-8"), capture, report=result)
        elif name in ("Boundary.txt", "TrackLines.txt"):
            converted, result = converter(raw, capture, allow_empty=True)
        else:
            converted, result = converter(raw, capture)
        outputs[name + ".csv"] = converted
        report["artifacts"][name] = {"state": "CONVERTED", "source_sha256": digest(raw),
            "csv_sha256": digest(converted.encode()), "profile": profile, "report": result}
        if drift is not None:
            derived = derived_csv(converted, latitude, longitude, drift)
            outputs[name + ".world.csv"] = derived
            report["artifacts"][name]["derived_csv_sha256"] = digest(derived.encode())
    report["origin_source_sha256"] = digest(originals["Field.txt"])
    # Inventory does not parse or qualify other producer files, including links.
    report["unselected_entries"] = sorted(p.name for p in folder.iterdir()
                                            if p.name not in ARTIFACTS and p.name != "Field.txt")
    return outputs, originals, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--capture-time-us", required=True, type=int)
    parser.add_argument("--drift-north-m", type=float)
    parser.add_argument("--drift-east-m", type=float)
    args = parser.parse_args()
    try:
        if (args.drift_north_m is None) != (args.drift_east_m is None):
            raise ValueError("both caller drift components required")
        drift = None if args.drift_north_m is None else (args.drift_north_m, args.drift_east_m)
        outputs, originals, report = convert_folder(args.folder, args.capture_time_us, drift=drift)
        args.output.mkdir()  # Never replace an earlier result.
        for name, data in outputs.items():
            (args.output / name).write_text(data, encoding="utf-8")
        for name, data in originals.items():
            (args.output / ("source." + name)).write_bytes(data)
        (args.output / "report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"saved field conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
