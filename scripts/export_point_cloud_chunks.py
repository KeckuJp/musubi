#!/usr/bin/env python3
"""Carry EVERY selected point of a saved cloud into common observations, in bounded segments.

The adopted path (`scripts/convert_saved_point_cloud.py`) reads a saved ASPRS LAS 1.4 file and
emits one row per point. Measured on this machine, one of its rows costs about 1.34 kB of CSV
and about 36 kB of resident memory in the common reader, so a million-point cloud in one file
would need tens of gigabytes to read back. Its own row bound - 65536 - is therefore a real
bound, and a bigger constant would not fix it.

This wrapper is the smallest composition that does: it walks the declared point list in
consecutive segments, calls the adopted converter once per segment, and writes each segment as
its own directory. Nothing else changes - the header, the variable length records, the
coordinate reference qualification, the scaling, every disposition and every refusal are the
adopted path's, executed per segment.

What it guarantees, and what the manifest proves:

* **every selected point, once**: the segment ranges are consecutive, ascending,
  non-overlapping, and cover the whole declared count exactly; the totals must reconcile or the
  manifest is not written;
* **no decimation, no sampling, no subset**: there is no option to take fewer points, and a
  failure raises instead of shortening the walk;
* **source order**: segments are in file order and rows inside a segment are in file order;
* **absolute identity**: `point_index` stays the index in the saved file, every segment repeats
  the same file declaration and the same whole-input digest, and each segment declares its own
  first and last original index;
* **bounded memory**: only one segment's rows exist at a time, and no whole-run report is
  accumulated - the manifest holds counts, ranges and digests, not rows.

Each segment is independently consumable by the common reader with the adopted profile:

    python3 scripts/export_point_cloud_chunks.py CLOUD.las OUT --capture-time-us N
    for chunk in OUT/chunk-*; do
      read_telemetry_csv profiles/declared/saved-point-cloud/profile.toml \\
        "$chunk/observations.csv" --allow-equal-time
    done

Contract, measurements and limits:
docs/saved-inputs.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

try:
    from scripts.convert_saved_point_cloud import (MAX_INPUT, MAX_POINT_ROWS, convert,
                                                   point_count, point_span, read_header)
except ModuleNotFoundError:  # running the script directly from scripts/
    from convert_saved_point_cloud import (MAX_INPUT, MAX_POINT_ROWS, convert, point_count,
                                           point_span, read_header)

# The default leaves room for the recorded report envelope and source declarations.
# CSV and report byte limits are still enforced; unusual declarations may need smaller chunks.
DEFAULT_POINTS_PER_CHUNK = 1024
# This path's own bound on how many directories one run may produce, so a mistaken segment size
# cannot fill a disk with millions of directories. At the default segment it allows more points
# than the adopted 64 MiB input ceiling can hold.
MAX_CHUNKS = 4096


def segments(total, points_per_chunk):
    """Consecutive `(first, count)` ranges covering `[0, total)` exactly, in ascending order.

    The segment bound is checked **by arithmetic, before anything is built**. A saved header may
    declare a 64 bit point count, and one point per segment is a legal request, so counting the
    ranges by making them would loop and allocate for as long as a corrupt or hostile
    declaration asked. `ceil(total / points_per_chunk)` answers the same question in constant
    time, and the caller has already checked that the file can hold what it declares.
    """
    if type(total) is not int or total < 0:
        raise ValueError("a declared point count is a non-negative whole number")
    if type(points_per_chunk) is not int or not 0 < points_per_chunk <= MAX_POINT_ROWS:
        raise ValueError("a segment carries between 1 and %d points, which is the adopted "
                         "path's own per-call bound" % MAX_POINT_ROWS)
    planned = -(-total // points_per_chunk)
    if planned > MAX_CHUNKS:
        raise ValueError("this walk would write %d segments, more than this path's bound of %d; "
                         "nothing is written, and no part of the cloud is dropped to fit"
                         % (planned, MAX_CHUNKS))
    found, first = [], 0
    while first < total:
        count = min(points_per_chunk, total - first)
        found.append((first, count))
        first += count
    if len(found) != planned:
        raise ValueError("the planned and the built segment counts disagree")
    # The covering property is checked here rather than assumed: it is the whole point.
    covered = sum(count for _, count in found)
    if covered != total or any(found[index][0] != found[index - 1][0] + found[index - 1][1]
                               for index in range(1, len(found))):
        raise ValueError("segment ranges do not cover the declared points exactly")
    return found


def export(raw, capture_time_us, directory, points_per_chunk=DEFAULT_POINTS_PER_CHUNK):
    """Write every segment and the manifest; return the manifest.

    The whole input is hashed once, the header is read once to learn how many points the file
    declares, and then each segment is converted and written in turn. Only the current segment's
    CSV is held in memory.
    """
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized saved LAS input")
    directory = Path(directory)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    header = read_header(raw)
    total, disposition = point_count(header)
    # Scheduling happens only after the file is known to hold what it declares. The span rule is
    # the adopted path's own, so a huge declared count is refused here by the same question the
    # converter would ask, before any range, directory or row exists.
    point_span(header, raw, total)
    ranges = segments(total, points_per_chunk)
    directory.mkdir()
    manifest = dict(source_sha256=digest, source_bytes=len(raw),
                    point_format=header["point_format"],
                    declared_point_count=header["point_count"],
                    legacy_point_count=header["legacy_count"],
                    point_count_disposition=disposition,
                    points_to_carry=total, points_per_chunk=points_per_chunk,
                    chunk_count=len(ranges) or 1, points_carried=0, csv_bytes=0, chunks=[],
                    capture_time_us=capture_time_us,
                    reader_route=("read_telemetry_csv "
                                  "profiles/declared/saved-point-cloud/"
                                  "profile.toml <chunk>/observations.csv --allow-equal-time"),
                    carriage_basis=("EVERY_DECLARED_POINT_IS_CARRIED_ONCE_IN_SOURCE_ORDER_"
                                    "ACROSS_CONSECUTIVE_SEGMENTS_NOTHING_IS_SAMPLED_THINNED_"
                                    "TRUNCATED_OR_RENUMBERED_AND_THE_TOTALS_BELOW_MUST_"
                                    "RECONCILE_WITH_THE_DECLARED_COUNT"))
    # A file that declares no points still declares itself, and its declaration is evidence. The
    # walk would otherwise be empty, the adopted validations - the caller's capture time, the
    # variable length records, the coordinate reference qualification - would never run, and a
    # manifest would report success for a file nothing had read. So a zero-point cloud is
    # carried as one declaration-only segment, through the same adopted call as any other.
    manifest["declaration_only_chunk"] = not ranges
    for index, (first, count) in enumerate(ranges or [(0, None)]):
        converted, report = convert(raw, capture_time_us, first, count)
        if report["points_read"] != (count if count is not None else 0):
            raise ValueError("a segment carried %d points where %d were selected"
                             % (report["points_read"], count or 0))
        count = report["points_read"]
        place = directory / ("chunk-%06d" % index)
        place.mkdir()
        (place / "observations.csv").write_text(converted, encoding="utf-8")
        (place / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
        encoded = converted.encode("utf-8")
        manifest["chunks"].append(dict(
            chunk=place.name, first_point_index=first,
            # No last index when the segment carries no point: an empty range has no end, and
            # `first - 1` would name a point the file does not have.
            last_point_index=(first + count - 1) if count else None,
            point_count=count, csv_bytes=len(encoded),
            csv_sha256="sha256:" + hashlib.sha256(encoded).hexdigest()))
        manifest["points_carried"] += count
        manifest["csv_bytes"] += len(encoded)
        # Released before the next segment is converted: one segment at a time is the bound.
        del converted, encoded, report
    if manifest["points_carried"] != total:
        raise ValueError("the carried points do not reconcile with the declared count")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                             encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved LAS file already on disk")
    parser.add_argument("output_directory", type=Path,
                        help="new directory for the chunk directories and the manifest")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller declared capture microseconds; never the producer's clock")
    parser.add_argument("--points-per-chunk", type=int, default=DEFAULT_POINTS_PER_CHUNK,
                        help="points per segment (default %d, maximum %d). This is a memory "
                             "bound, never a selection: every point is carried whatever the "
                             "value" % (DEFAULT_POINTS_PER_CHUNK, MAX_POINT_ROWS))
    args = parser.parse_args(argv)
    with args.input.open("rb") as handle:
        raw = handle.read(MAX_INPUT + 1)
    manifest = export(raw, args.capture_time_us, args.output_directory, args.points_per_chunk)
    print(json.dumps({key: manifest[key] for key in
                      ("points_to_carry", "points_carried", "chunk_count", "csv_bytes")},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
