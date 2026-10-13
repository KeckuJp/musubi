"""Saved ffprobe8.1 frame tags from FFmpeg8.1 blackdetect/freezedetect -> reported intervals.

Never opens media, runs a filter or judges pixels. Each row is one interval the filter
reported, timed only where the pinned filter source says which frame its value names.
A detector report is not physical black, frozen video or a fault.
"""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_video_frames as video
from scripts.check_saved_video_frames import digest, integer
from scripts.convert_image_flight_binding import label

SCHEMA = "saved-video-detector-intervals/v1"
LINK = "CALLER_DECLARED_FILTER_RUN_INPUT_NOT_VERIFIED_FRAME_INDICES_NOT_INTERCHANGEABLE"
LABELS = ("filter_graph", "session_id", "camera_id", "citation")
KEYS = {"schema", "export_sha256", "stream_index", "detectors", "unfiltered_export_sha256",
        "unfiltered_relation", *LABELS}
BLACK_START, BLACK_END = "lavfi.black_start", "lavfi.black_end"
FREEZE_START = "lavfi.freezedetect.freeze_start"
FREEZE_DURATION = "lavfi.freezedetect.freeze_duration"
FREEZE_END = "lavfi.freezedetect.freeze_end"
OWNER = {BLACK_START: "blackdetect", BLACK_END: "blackdetect", FREEZE_START: "freezedetect",
         FREEZE_DURATION: "freezedetect", FREEZE_END: "freezedetect"}
BASIS = "FFMPEG_8_1_FILTER_METADATA_REPORT_NOT_PHYSICAL_BLACK_FROZEN_OR_FAULT_TRUTH"
# vf_blackdetect.c sets both tags whatever black_min_duration is; only its log line applies it.
MIN_BASIS = {"blackdetect": "TAGS_EMITTED_REGARDLESS_OF_BLACK_MIN_DURATION",
             "freezedetect": "TAGS_EMITTED_ONLY_AFTER_THE_DURATION_OPTION_ELAPSED"}
SOURCE_PIN = {"ffmpeg_commit": "9047fa1b084f76b1b4d065af2d743df1b40dfb56",
    "libavfilter/vf_blackdetect.c": "sha256:46e888e2fb7b194450966aef5209a72ad67fc8d4fe69a95eef89fb6b7a1bfc56",
    "libavfilter/vf_freezedetect.c": "sha256:0b01bf68836d044ef482013c3fa0f3e5a142f9ec06e86b27a1cd24e0f7e93aca",
    "libavutil/timestamp.c": "sha256:59f8ee71d0468de72b44d55af779aa4392007d82a68d52eac9675cae2406d15f"}
FIELDS = ["record_time_us", "detector", "detector_interval_index", "interval_status",
          "start_tag_frame_index", "start_frame_index", "start_frame_resolution", "start_pts",
          "start_time_text_reported", "end_frame_index", "end_pts_exclusive", "end_time_text_reported",
          "interval_ticks", "freeze_duration_text_reported", "freeze_duration_us_from_reported_text",
          "freeze_duration_precision_basis",
          "stream_index", "timebase_numerator", "timebase_denominator", "min_duration_basis",
          "detector_basis", "filtered_export_sha256", "declaration_sha256",
          *(name + "_hex" for name in LABELS), "unfiltered_export_sha256", "unfiltered_relation"]


def time_text(ts, numerator, denominator):
    """libavutil/timestamp.c av_ts_make_time_string2 at the pin, for one integer timestamp."""
    value = numerator / denominator * ts
    log = -math.inf if value == 0 else math.floor(math.log10(abs(value)))
    precision = -log + 5 if math.isfinite(log) and log < 0 else 6
    text = "%.*f" % (precision, value)
    last = min(len(text), 31) - 1
    while last and text[last] == "0":
        last -= 1
    while last and text[last] != "f" and (text[last] < "0" or text[0] > "9"):
        last -= 1
    return text[:last + 1]


# av_ts_make_time_string2 prints NOPTS or a finite %f with trailing zeros and point removed.
TIME_TEXT = re.compile(r"NOPTS|-?[0-9]{1,30}(\.[0-9]{0,29}[1-9])?")
DURATION_TYPED = "US_PARSED_FROM_ROUNDED_REPORTED_TEXT_REPRINTS_EXACTLY_NOT_PROVEN_ORIGINAL_COUNTER"
DURATION_UNTYPED = "REPORTED_TEXT_ONLY_NO_INTEGER_MICROSECOND_REPRINTS_IT"


def duration_us(text):
    """Microseconds read back from the rounded freeze_duration text, only if they reprint it.

    A reprint shows consistency with the text, not the filter's original integer counter.
    """
    if not re.fullmatch(r"[0-9]{1,19}(\.[0-9]{1,30})?", text):
        raise ValueError("freeze duration is not a reported decimal")
    whole, _, fraction = text.partition(".")
    micro = int(whole) * 10**6 + int((fraction + "000000")[:6])
    return micro if time_text(micro, 1, 10**6) == text else None


def convert(export, declaration):
    if type(export) is not bytes or type(declaration) is not bytes:
        raise ValueError("exact original bytes required")
    if len(export) > 32*1024**2 or len(declaration) > 16*1024:
        raise ValueError("input exceeds finite bound")
    declared = json.loads(declaration.decode(), object_pairs_hook=video.unique_object,
                          parse_constant=video.invalid_constant)
    if not isinstance(declared, dict) or set(declared) != KEYS or declared["schema"] != SCHEMA:
        raise ValueError("closed detector-interval declaration required")
    export_digest = digest(export)
    if declared["export_sha256"] != export_digest:
        raise ValueError("filtered export digest mismatch")
    detectors = declared["detectors"]
    if (not isinstance(detectors, list) or not detectors or len(set(detectors)) != len(detectors)
            or not set(detectors) <= set(MIN_BASIS)):
        raise ValueError("declared detector list required")
    labels = {name + "_hex": label(declared[name]) for name in LABELS}
    other, relation = declared["unfiltered_export_sha256"], declared["unfiltered_relation"]
    if (other, relation) != (None, None) and (
            not isinstance(other, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", other)
            or other == export_digest or relation != LINK):
        raise ValueError("unfiltered link needs a distinct digest and the explicit relation")
    selected = integer(declared["stream_index"])
    frames_csv, frame_report = video.convert(export.decode(), selected, compact=True)
    numerator, denominator = (int(x) for x in json.loads(bytes.fromhex(
        frame_report["selected_stream_hex"][4:]))["time_base"].split("/"))
    pts = {int(r["source_frame_index"]): int(r["presentation_timestamp_ticks"])
           for r in csv.DictReader(io.StringIO(frames_csv))}
    source = json.loads(export.decode(), object_pairs_hook=video.unique_object,
                        parse_constant=video.invalid_constant)
    common = dict(record_time_us=0, stream_index=selected, timebase_numerator=numerator,
        timebase_denominator=denominator, detector_basis=BASIS, filtered_export_sha256=export_digest,
        declaration_sha256=digest(declaration), unfiltered_export_sha256=other or "",
        unfiltered_relation=relation or "", **labels)
    intervals, tagged, unknown = [], [], {}
    unselected_tags = 0
    open_ = {"blackdetect": None, "freezedetect": None}
    freeze_floor = 0  # the reference frame may be the previous freeze end frame itself

    def text_at(index):
        return time_text(pts[index], numerator, denominator) if index in pts else "NOPTS"

    def checked(tags, key, index):
        value = tags[key]
        if value != text_at(index):
            # The pinned filter writes this key from the carrying frame's own pts in its input
            # timebase; any difference means the export is not that filter's output stream.
            raise ValueError("detector tag time differs from the frame that carries it")
        return value

    def interval(detector, **values):
        row = dict(common, detector=detector, min_duration_basis=MIN_BASIS[detector], **values)
        intervals.append(row)
        return row

    def close(row, index, value):
        row.update(end_frame_index=index, end_pts_exclusive=pts.get(index, ""), end_time_text_reported=value)
        if isinstance(row.get("start_pts"), int) and index in pts:
            row["interval_ticks"] = pts[index] - row["start_pts"]

    for index, frame in enumerate(source["frames"]):
        tags = frame.get("tags", {})
        if not isinstance(tags, dict) or any(not isinstance(v, str) for v in tags.values()):
            raise ValueError("frame tags must be a string map")
        if not tags:
            continue
        if video.unsigned(frame["stream_index"]) != selected:
            unselected_tags += 1
            continue
        tagged.append({"index": index, "tags": tags})
        for key in tags:
            if key not in OWNER:
                unknown[key] = unknown.get(key, 0) + 1
            elif OWNER[key] not in detectors:
                raise ValueError("tag from an undeclared detector")
        if BLACK_START in tags and BLACK_END in tags:
            raise ValueError("black start and end cannot share a frame")
        if BLACK_START in tags:
            if open_["blackdetect"]:
                raise ValueError("black start repeated without an end")
            value = checked(tags, BLACK_START, index)
            open_["blackdetect"] = interval("blackdetect", interval_status="OPEN_NO_END_TAG_IN_EXPORT",
                start_tag_frame_index=index, start_frame_index=index, start_pts=pts.get(index, ""),
                start_frame_resolution="CARRYING_FRAME" if index in pts else "CARRYING_FRAME_UNTIMED",
                start_time_text_reported=value)
        if BLACK_END in tags:
            value = checked(tags, BLACK_END, index)
            row = open_["blackdetect"] or interval("blackdetect",
                start_frame_resolution="NO_START_TAG_IN_EXPORT")
            row["interval_status"] = "CLOSED" if open_["blackdetect"] else "END_WITHOUT_START_TAG_IN_EXPORT"
            close(row, index, value)
            open_["blackdetect"] = None
        if (FREEZE_END in tags) != (FREEZE_DURATION in tags):
            raise ValueError("freeze end and duration are written together")
        if FREEZE_START in tags:
            if open_["freezedetect"]:
                raise ValueError("freeze start repeated without an end")
            value = tags[FREEZE_START]
            if not TIME_TEXT.fullmatch(value):
                raise ValueError("freeze start is not text the pinned formatter can print")
            # Written on a later frame; the value is the reference frame's pts. Resolve that frame
            # only by an exact reprint, never by assigning the interval to the carrying frame.
            found = [i for i in range(freeze_floor, index) if i in pts and text_at(i) == value]
            unique = len(found) == 1
            open_["freezedetect"] = interval("freezedetect", interval_status="OPEN_NO_END_TAG_IN_EXPORT",
                start_tag_frame_index=index, start_frame_index=found[0] if unique else "",
                start_pts=pts[found[0]] if unique else "", start_time_text_reported=value,
                start_frame_resolution="UNIQUE_EARLIER_FRAME_EXACT_REPRINT" if unique else
                "UNRESOLVED_NO_UNIQUE_EARLIER_FRAME")
        if FREEZE_END in tags:
            value = checked(tags, FREEZE_END, index)
            row = open_["freezedetect"] or interval("freezedetect",
                start_frame_resolution="NO_START_TAG_IN_EXPORT")
            row["interval_status"] = "CLOSED" if open_["freezedetect"] else "END_WITHOUT_START_TAG_IN_EXPORT"
            close(row, index, value)
            reported = tags[FREEZE_DURATION]
            micro = duration_us(reported)
            row.update(freeze_duration_text_reported=reported,
                       freeze_duration_us_from_reported_text="" if micro is None else micro,
                       freeze_duration_precision_basis=DURATION_UNTYPED if micro is None else DURATION_TYPED)
            open_["freezedetect"], freeze_floor = None, index
    for detector in detectors:
        if not any(r["detector"] == detector for r in intervals):
            interval(detector, interval_status="NO_INTERVAL_REPORTED")
    order = {}
    intervals.sort(key=lambda r: (r.get("start_tag_frame_index", r.get("end_frame_index", math.inf))
                                  if r["interval_status"] != "NO_INTERVAL_REPORTED" else math.inf, r["detector"]))
    for row in intervals:
        row["detector_interval_index"] = order[row["detector"]] = order.get(row["detector"], -1) + 1
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(intervals)
    text = output.getvalue()
    counts = {}
    for row in intervals:
        key = row["detector"] + ":" + row["interval_status"]
        counts[key] = counts.get(key, 0) + 1
    report = dict(declaration=declared, source_pin=SOURCE_PIN, basis=BASIS, counts=counts,
        intervals=len(intervals), tagged_frames=tagged, unknown_tag_keys=unknown,
        tagged_unselected_frames=unselected_tags, frame_export=frame_report,
        filtered_export_sha256=export_digest, declaration_sha256=digest(declaration),
        output_sha256=digest(text.encode()), primary_clock="Unknown; association order only",
        frame_index_scope="filtered export only; not interchangeable with the unfiltered export")
    if len(text.encode()) > 16*1024**2 or len(json.dumps(report).encode()) > 32*1024**2:
        raise ValueError("output exceeds bound")
    return text, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("declaration", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with args.export.open("rb") as f:
            export = f.read(32*1024**2+1)
        with args.declaration.open("rb") as f:
            declaration = f.read(16*1024+1)
        text, report = convert(export, declaration)
        args.output.mkdir()
        (args.output/"source-export.json").write_bytes(export)
        (args.output/"declaration.json").write_bytes(declaration)
        (args.output/"observations.csv").write_text(text)
        (args.output/"report.json").write_text(json.dumps(report, allow_nan=False, indent=2)+"\n")
    except (ValueError, OSError, TypeError, KeyError, UnicodeError):
        parser.exit(2, "detector interval conversion refused; check export, declaration and bounds\n")


if __name__ == "__main__":
    main()
