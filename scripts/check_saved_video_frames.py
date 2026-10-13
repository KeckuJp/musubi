"""Compare explicit expected PTS ticks with saved ffprobe8.1 frame metadata."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_video_frames as video

KEYS = {"schema", "export_sha256", "stream_index", "time_base", "start_pts", "stop_pts", "expected_pts"}
FIELDS = ["record_time_us", "expected_pts", "stream_index", "timebase_numerator",
          "timebase_denominator", "presence", "matched_frame_count", "matched_frame_indices_hex",
          "source_export_sha256", "expectation_sha256", "start_pts", "stop_pts"]


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def integer(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("bounded integer PTS/stream required")
    return value


def convert(export, expectation):
    if type(export) is not bytes or type(expectation) is not bytes:
        raise ValueError("exact original bytes required")
    if len(export) > 32*1024**2 or len(expectation) > 256*1024:
        raise ValueError("input exceeds finite window bound")
    declared = json.loads(expectation.decode(), object_pairs_hook=video.unique_object,
                         parse_constant=video.invalid_constant)
    if not isinstance(declared, dict) or set(declared) != KEYS or declared["schema"] != "saved-video-pts-expectation/v1":
        raise ValueError("closed expectation required")
    export_digest, expectation_digest = digest(export), digest(expectation)
    if declared["export_sha256"] != export_digest:
        raise ValueError("export digest mismatch")
    selected = integer(declared["stream_index"])
    start, stop = integer(declared["start_pts"]), integer(declared["stop_pts"])
    expected = declared["expected_pts"]
    if start >= stop or not isinstance(expected, list) or not 1 <= len(expected) <= 4096:
        raise ValueError("nonempty finite expectation window required")
    for tick in expected:
        integer(tick)
        if not start <= tick < stop:
            raise ValueError("expected tick outside half-open window")
    if len(set(expected)) != len(expected):
        raise ValueError("duplicate expected tick")
    text, report = video.convert(export.decode(), selected, compact=True)
    stream = json.loads(bytes.fromhex(report["selected_stream_hex"][4:]))
    if declared["time_base"] != stream["time_base"]:
        raise ValueError("expectation timebase mismatch")
    numerator, denominator = map(int, stream["time_base"].split("/"))
    matches = {}
    for row in csv.DictReader(io.StringIO(text)):
        matches.setdefault(int(row["presentation_timestamp_ticks"]), []).append(int(row["source_frame_index"]))
    low, high = min(matches), max(matches)
    # One tick beyond the final saved tick permits a half-open window containing it.
    covered = start >= low and stop <= high + 1
    absence_known = covered and report["untimed_video_frames"] == 0
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, FIELDS, lineterminator="\n")
    writer.writeheader()
    counts = {name: 0 for name in ("OBSERVED", "NOT_OBSERVED_IN_SAVED_WINDOW", "INDETERMINATE")}
    matched = 0
    for tick in expected:
        indices = matches.get(tick, [])
        status = "OBSERVED" if indices else "NOT_OBSERVED_IN_SAVED_WINDOW" if absence_known else "INDETERMINATE"
        counts[status] += 1
        matched += len(indices)
        writer.writerow(dict(record_time_us=0, expected_pts=tick, stream_index=selected,
            timebase_numerator=numerator, timebase_denominator=denominator,
            presence=status, matched_frame_count=len(indices),
            matched_frame_indices_hex="hex:" + json.dumps(indices, separators=(",", ":")).encode().hex(),
            source_export_sha256=export_digest, expectation_sha256=expectation_digest,
            start_pts=start, stop_pts=stop))
    result = out.getvalue()
    report["expectation_check"] = dict(declaration=declared, counts=counts, expected_count=len(expected),
        saved_extent_pts=[low, high], window_bracketed=covered, matched_frames=matched,
        timed_frames_not_matching_expectations=report["timed_video_frames"]-matched,
        basis="EXPLICIT_CALLER_SCHEDULE_SAVED_PTS_ONLY_NOT_PHYSICAL_LOSS",
        source_export_sha256=export_digest, expectation_sha256=expectation_digest,
        output_sha256=digest(result.encode()), original_retention="keep exact export and expectation bytes")
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("expectation", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with args.export.open("rb") as f:
            export = f.read(32*1024**2+1)
        with args.expectation.open("rb") as f:
            expectation = f.read(256*1024+1)
        text, report = convert(export, expectation)
        args.output.mkdir()
        (args.output/"source-export.json").write_bytes(export)
        (args.output/"expectation.json").write_bytes(expectation)
        (args.output/"observations.csv").write_text(text)
        (args.output/"report.json").write_text(json.dumps(report, allow_nan=False, indent=2)+"\n")
    except (ValueError, OSError, TypeError, KeyError, UnicodeError):
        parser.exit(2, "saved frame expectation refused; check input, declarations and bounds\n")


if __name__ == "__main__":
    main()
