"""Reported black/freeze intervals -> the caller-declared flight clock, through exact PTS.

Composes the adopted converters unchanged: detector intervals from a filtered ffprobe8.1 export
(convert_video_detector_intervals) and frame/flight-clock rows from the unfiltered export
(convert_video_flight_binding). A closed relation declaration binds every original digest and
states that the filter graph did not retime the selected stream. That statement is checked, not
trusted: the selected stream's time_base and full PTS sequence must be equal in both exports.
Endpoints then join only on a unique exact integer PTS; frame indices are never interchanged.
Never opens media or runs a filter. Not authentication, measured synchronization or a fault.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_video_detector_intervals as detector
from scripts import convert_video_flight_binding as flight_binding
from scripts import convert_video_frames as video
from scripts.check_saved_video_frames import digest

SCHEMA = "video-cue-flight-relation-v1"
RELATION = "CALLER_SAME_MEDIA_SELECTED_STREAM_NO_PTS_OR_TIMEBASE_TRANSFORM_CHECKED_NOT_AUTHENTICATED"
KEYS = {"schema", "relation", "filtered_export_sha256", "unfiltered_export_sha256",
        "flight_sha256", "detector_declaration_sha256", "flight_declaration_sha256", "citation"}
PRECONDITION = "SELECTED_STREAM_TIME_BASE_AND_FULL_PTS_SEQUENCE_EQUAL_IN_BOTH_EXPORTS"
ENDPOINT = ("unfiltered_frame_index", "endpoint_match", "flight_connection_status",
            "derived_flight_time_us", "derived_flight_time_us_exact", "derived_flight_time_us_floor")
ENDPOINT_UNITS = {
    "unfiltered_frame_index": "unfiltered_export_frame_with_the_same_exact_pts_not_the_filtered_index",
    "endpoint_match": "UNIQUE_EXACT_PTS_or_NO_ENDPOINT_PTS_or_NO_EXACT_PTS_MATCH_or_AMBIGUOUS_DUPLICATE_PTS",
    "flight_connection_status": "status_of_the_matched_unfiltered_frame_from_the_video_flight_binding",
    "derived_flight_time_us": "exact_integer_us_on_declared_flight_clock_blank_unless_exact",
    "derived_flight_time_us_exact": "ratio_us_authoritative_only_with_rational_clock",
    "derived_flight_time_us_floor": "quantized_floor_never_exact_sample_or_timestamp_evidence",
}
FIELDS_EXTRA = [f"{end}_{name}" for end in ("start", "end") for name in ENDPOINT] + [
    "cue_flight_relation", "relation_declaration_sha256", "unfiltered_export_sha256_bound",
    "flight_source_sha256", "flight_declaration_sha256", "flight_entity_id_hex",
    "media_clock_id_hex", "flight_clock_id_hex", "flight_relation", "precondition"]
UNITS = {**{f"{end}_{name}": unit for end in ("start", "end") for name, unit in ENDPOINT_UNITS.items()},
    "cue_flight_relation": "caller_relation_plus_consistency_check_not_authentication_or_measured_sync",
    "relation_declaration_sha256": "sha256_caller_relation_declaration_bytes",
    "unfiltered_export_sha256_bound": "sha256_unfiltered_ffprobe_export_bytes_not_media_authentication",
    "flight_source_sha256": "sha256_supplied_Plane_decoded_ATT_bytes",
    "flight_declaration_sha256": "sha256_existing_video_flight_clock_declaration_bytes",
    "flight_entity_id_hex": "utf8_hex_caller_declared_flight_entity_not_verified",
    "media_clock_id_hex": "utf8_hex_caller_declared_media_clock_not_verified",
    "flight_clock_id_hex": "utf8_hex_caller_declared_flight_clock_not_verified",
    "flight_relation": "caller_same_event_constant_offset_assertion_not_verified",
    "precondition": "checked_equal_time_base_and_pts_sequence_not_frame_identity"}
ROOT = Path(__file__).resolve().parents[1]


def closed(raw, limit):
    if type(raw) is not bytes or len(raw) > limit:
        raise ValueError("bounded exact declaration bytes required")
    value = json.loads(raw.decode(), object_pairs_hook=video.unique_object,
                       parse_constant=video.invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("declaration object required")
    return value


def selected_sequence(export, stream):
    """time_base and PTS (None if missing) of every selected frame, in export order."""
    text, report = video.convert(export.decode(), stream, compact=True)
    time_base = json.loads(bytes.fromhex(report["selected_stream_hex"][4:]))["time_base"]
    order = {int(r["source_frame_index"]): int(r["presentation_timestamp_ticks"])
             for r in csv.DictReader(io.StringIO(text))}
    order.update({r["index"]: None for r in report["retained_unconverted_frames"]
                  if r["reason"] == "MISSING_PTS"})
    return time_base, [order[i] for i in sorted(order)]


def convert(filtered, unfiltered, flight, detector_declaration, flight_declaration,
            relation_declaration, *, rational_clock=False):
    """Returns (csv, report, profile). Caller retains all six original inputs beside them."""
    if any(type(raw) is not bytes for raw in (filtered, unfiltered, flight)):
        raise ValueError("exact input bytes required")
    relation = closed(relation_declaration, 16*1024)
    if set(relation) != KEYS or relation["schema"] != SCHEMA or relation["relation"] != RELATION:
        raise ValueError("closed explicit cue-flight relation required")
    detector.label(relation["citation"])
    bound = {"filtered_export_sha256": filtered, "unfiltered_export_sha256": unfiltered,
             "flight_sha256": flight, "detector_declaration_sha256": detector_declaration,
             "flight_declaration_sha256": flight_declaration}
    if any(type(raw) is not bytes or relation[key] != digest(raw) for key, raw in bound.items()):
        raise ValueError("relation digest mismatch")
    cue_declared = closed(detector_declaration, 16*1024)
    flight_declared = closed(flight_declaration, 16*1024)
    if (cue_declared.get("unfiltered_export_sha256") != digest(unfiltered)
            or cue_declared.get("unfiltered_relation") != detector.LINK):
        raise ValueError("detector declaration must link this unfiltered export")
    for name in ("session_id", "camera_id"):
        if cue_declared.get(name) != flight_declared.get(name):
            raise ValueError("detector and flight declarations name different " + name)
    # Existing converters, unchanged, validate each original and its own declaration.
    cue_csv, cue_report = detector.convert(filtered, detector_declaration)
    frame_csv, frame_report, _, flight_csv, _ = flight_binding.convert(
        unfiltered, flight, flight_declaration, rational_clock=rational_clock)
    filtered_sequence = selected_sequence(filtered, cue_declared["stream_index"])
    if filtered_sequence != selected_sequence(unfiltered, flight_declared["stream_index"]):
        raise ValueError("selected time_base or PTS sequence differs; no PTS join")
    by_pts = {}
    frames = list(csv.DictReader(io.StringIO(frame_csv)))
    for row in frames:
        by_pts.setdefault(int(row["presentation_timestamp_ticks"]), []).append(row)
    common = {"cue_flight_relation": RELATION, "relation_declaration_sha256": digest(relation_declaration),
              "unfiltered_export_sha256_bound": digest(unfiltered), "precondition": PRECONDITION,
              "flight_source_sha256": frames[0]["flight_source_sha256"],
              "flight_declaration_sha256": frames[0]["clock_declaration_sha256"],
              **{k: frames[0][k] for k in ("flight_entity_id_hex", "media_clock_id_hex",
                                           "flight_clock_id_hex", "flight_relation")}}
    rows, counts = [], {}
    cues = csv.DictReader(io.StringIO(cue_csv))
    for cue in cues:
        row = dict(cue, **common)
        for end, column in (("start", "start_pts"), ("end", "end_pts_exclusive")):
            matches = by_pts.get(int(cue[column]), []) if cue[column] else []
            match = ("NO_ENDPOINT_PTS" if not cue[column] else "UNIQUE_EXACT_PTS" if len(matches) == 1
                     else "NO_EXACT_PTS_MATCH" if not matches else "AMBIGUOUS_DUPLICATE_PTS")
            row[end + "_endpoint_match"] = match
            if match == "UNIQUE_EXACT_PTS":
                frame = matches[0]
                row[end + "_unfiltered_frame_index"] = frame["source_frame_index"]
                for name in ENDPOINT[2:]:
                    row[f"{end}_{name}"] = frame.get(name, "")
            key = f"{end}:{match}:{row.get(end + '_flight_connection_status', '')}"
            counts[key] = counts.get(key, 0) + 1
        rows.append(row)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, cues.fieldnames + FIELDS_EXTRA, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    text = output.getvalue()
    if len(text.encode()) > 16*1024**2:
        raise ValueError("output exceeds bound")
    report = dict(relation=relation, endpoint_counts=counts, intervals=len(rows),
        precondition=PRECONDITION, selected_time_base=filtered_sequence[0],
        selected_frames=len(filtered_sequence[1]), rational_clock=rational_clock,
        detector_report=cue_report, flight_binding=frame_report["flight_binding"],
        flight_csv_sha256=digest(flight_csv.encode()), output_sha256=digest(text.encode()),
        primary_clock="Unknown; association order only",
        basis="caller relation plus equality check of the selected stream; not authentication, "
              "frame identity, measured synchronization or physical black/frozen truth")
    profile = (ROOT/"profiles/declared/video-frame-export/detector-interval-profile.toml").read_text()
    profile = profile.replace("saved_video_detector_intervals_v1",
        "video_cue_flight_declared_rational_v1" if rational_clock else "video_cue_flight_declared_v1")
    profile += "\n".join(f'{key} = "{unit}"' for key, unit in UNITS.items()) + "\n"
    return text, report, profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("filtered_export", "unfiltered_export", "flight", "detector_declaration",
             "flight_declaration", "relation_declaration")
    for name in names:
        parser.add_argument(name, type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--rational-clock", action="store_true",
                        help="connect submicrosecond endpoints by exact ratio (convert_video_flight_binding)")
    args = parser.parse_args()
    try:
        raw = {}
        for name in names:
            with getattr(args, name).open("rb") as f:
                raw[name] = f.read(32*1024**2 + 1)
        text, report, profile = convert(*(raw[n] for n in names), rational_clock=args.rational_clock)
        args.output.mkdir()
        for name in names:
            (args.output / ("source-" + name.replace("_", "-"))).write_bytes(raw[name])
        (args.output / "observations.csv").write_text(text)
        (args.output / "profile.toml").write_text(profile)
        (args.output / "report.json").write_text(json.dumps(report, allow_nan=False, indent=2) + "\n")
    except (ValueError, OSError, TypeError, KeyError, UnicodeError):
        parser.exit(2, "cue flight binding refused; check exports, declarations and relation\n")


if __name__ == "__main__":
    main()
