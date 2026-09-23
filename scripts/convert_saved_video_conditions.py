#!/usr/bin/env python3
"""Saved FFmpeg 8.1 blackdetect/freezedetect metadata -> observed interval cues for the CSV reader.

Passive and offline: this **never opens media**. It reads the saved output of an analysis the
operator already ran with their own local decoder, exactly as the adopted ffprobe path does.

Every row is an **observed cue**, never a fault. A black interval is not a camera failure, a lens
cap, a signal loss or a night scene; a freeze interval is not a camera failure either, because a
legitimately static scene is frozen by definition. Neither filter claims a cause and none is added
here. Missing frames and decode failures are never reported as black or freeze.
"""
import argparse
import csv
import hashlib
import io
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

# The decoder whose documented filter output this reads. Already this repository's video pin.
PIN = "ffmpeg-8.1-blackdetect-freezedetect"
LIMIT = 8 * 1024 * 1024
OUTPUT_LIMIT = 16 * 1024 * 1024
MAX_FRAMES = 2_000_000
MAX_INTERVALS = 4096
# `metadata=mode=print` writes this header before each frame's keys.
FRAME_LINE = re.compile(r"frame:(\d{1,9})\s+pts:(-?\d{1,19})\s+pts_time:(\S{1,64})\s*$")
ENTRY_LINE = re.compile(r"(lavfi\.[A-Za-z0-9_.]{1,64})=(.{0,128})$")
# `av_ts2timestr` prints this literal when a timestamp is unset, so it is a value, not a parse
# failure, and it is carried as unknown rather than turned into a zero.
NOPTS = "NOPTS"
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?")
# The five keys this selector reads. Every other key is counted and left in the retained source.
BLACK_START, BLACK_END = "lavfi.black_start", "lavfi.black_end"
FREEZE_START = "lavfi.freezedetect.freeze_start"
FREEZE_DURATION = "lavfi.freezedetect.freeze_duration"
FREEZE_END = "lavfi.freezedetect.freeze_end"
SELECTED = (BLACK_START, BLACK_END, FREEZE_START, FREEZE_DURATION, FREEZE_END)
DECODER_STATUS = ("completed-without-reported-errors", "errors-reported")
THRESHOLD_BASES = ("operator-declared", "analysis-script-declared")
CUE_BASIS = ("AN_OBSERVED_INTERVAL_CUE_AND_NEVER_A_FAULT_NO_CAMERA_LENS_LINK_OR_SCENE_CONCLUSION_"
             "FOLLOWS_FROM_IT_NEITHER_FILTERS_DOCUMENTATION_MAKES_A_CAUSAL_CLAIM_AND_NONE_IS_ADDED_"
             "HERE_A_DARK_SCENE_AND_A_DELIBERATELY_STATIC_SCENE_PRODUCE_THESE_SAME_CUES")
TIME_BASIS = ("SECONDS_BEING_THE_FRAMES_PRESENTATION_TIMESTAMP_TIMES_THE_STREAM_TIME_BASE_THE_"
              "FILTERS_DO_NOT_RE_ORIGINATE_ANYTHING_SO_THE_ORIGIN_IS_THE_ANALYSED_STREAMS_OWN_PTS_"
              "ORIGIN_AND_IS_NOT_UTC_NOT_A_CAPTURE_WALL_CLOCK_AND_NOT_A_DEVICE_CLOCK")
FIELDS = ["video_record_kind", "video_interval_index", "video_start_s", "video_end_s",
          "video_duration_s", "video_duration_source", "video_end_disposition",
          "video_start_attachment_frame", "video_start_attachment_pts_time_s",
          "video_minimum_duration_qualification", "video_threshold_declaration",
          "video_decoder_status", "video_analysed_frame_count", "video_unselected_key_count",
          "video_time_basis", "video_cue_basis"]


def text_hex(value):
    return "hex:" + value.encode("utf-8").hex()


def timestamp(token, what):
    """One emitted timestamp in seconds, or the unknown the decoder itself printed.

    A value the decoder could not express is `NOPTS`; it stays unknown rather than becoming a
    zero. Anything that cannot survive as a finite number is refused before it can reach common
    output as an infinity or a false zero.
    """
    if token == NOPTS:
        return None
    if not NUMBER.fullmatch(token):
        raise ValueError(f"declared {what} is neither a number nor the decoder's own NOPTS")
    try:
        exact = Decimal(token)
    except InvalidOperation:
        raise ValueError(f"declared {what} is not a number")
    value = float(exact)
    if not math.isfinite(value):
        raise ValueError(f"declared {what} cannot be represented as a finite number")
    if exact != 0 and value == 0:
        raise ValueError(f"declared nonzero {what} underflows to zero")
    return value


def parse_metadata(text, report):
    """Every selected key in document order, with the frame it was attached to.

    The attachment frame is deliberately kept apart from the value: `freeze_start` is attached to
    the frame at which the declared duration was crossed, while its value is the first frozen
    frame. Reading the attachment as the event time would move the cue.
    """
    events, frame, pts_time, previous = [], None, None, None
    report["highest_frame_index"] = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        header = FRAME_LINE.match(line.strip())
        if header:
            frame = int(header[1])
            if previous is not None and frame <= previous:
                raise ValueError("analysed frame numbers must increase")
            if frame > MAX_FRAMES:
                raise ValueError("analysed frame number exceeds bound")
            previous = frame
            report["highest_frame_index"] = frame
            pts_time = timestamp(header[3], "frame presentation time")
            report["analysed_frame_blocks"] += 1
            continue
        entry = ENTRY_LINE.match(line.strip())
        if entry is None:
            raise ValueError(f"line {number} is neither a frame header nor a metadata entry")
        if frame is None:
            raise ValueError("a metadata entry appeared before any frame header")
        key, value = entry[1], entry[2].strip()
        if key not in SELECTED:
            report["unselected_keys"] += 1
            continue
        events.append((frame, pts_time, key, value))
    return events


def intervals(events, kind, start_key, end_key, duration_key=None):
    """Pair the selected starts and ends into closed or open intervals, or refuse.

    Three distinctions this keeps, each of which a simpler pairing loses:

    * an interval still open when the analysis ended is carried **open** -- the decoder reported no
      end at all -- while an end the decoder reported but could not express (its own `NOPTS`) is a
      **reported unknown**. Collapsing the two would say "nothing was reported" about something
      that was;
    * a repeated selected singleton is **refused**, not overwritten, and presence is tracked by
      its own flag rather than by the value -- a duration reported as `NOPTS` was still *reported*,
      so testing the value for presence let a later one silently replace it;
    * a duration that cannot be true -- negative -- is refused rather than carried.
    """
    found, open_interval = [], None
    for frame, pts_time, key, value in events:
        if key == start_key:
            if open_interval is not None:
                raise ValueError(f"a second {kind} start arrived before the previous one ended")
            open_interval = dict(start=timestamp(value, f"{kind} start"), frame=frame,
                                 attachment=pts_time, duration=None, duration_reported=False,
                                 end=None, end_reported=False)
        elif key == end_key:
            if open_interval is None:
                raise ValueError(f"a {kind} end arrived with no start before it")
            if open_interval["end_reported"]:
                raise ValueError(f"a repeated {kind} end arrived for one interval")
            open_interval["end"] = timestamp(value, f"{kind} end")
            open_interval["end_reported"] = True
            found.append(open_interval)
            open_interval = None
        elif duration_key is not None and key == duration_key:
            if open_interval is None:
                raise ValueError(f"a {kind} duration arrived with no start before it")
            # Presence is its own flag, never the value: a duration reported as the decoder's own
            # NOPTS is `None` as a value but WAS reported, so testing the value for presence let a
            # later one silently replace it. That replacement is the loss this guard prevents.
            if open_interval["duration_reported"]:
                raise ValueError(f"a repeated {kind} duration arrived for one interval")
            reported = timestamp(value, f"{kind} duration")
            if reported is not None and reported < 0:
                raise ValueError(f"a reported {kind} duration cannot be negative")
            open_interval["duration"] = reported
            open_interval["duration_reported"] = True
    if open_interval is not None:
        found.append(open_interval)
    for interval in found:
        if (interval["start"] is not None and interval["end"] is not None
                and interval["end"] < interval["start"]):
            raise ValueError(f"a {kind} interval ends before it starts")
    return found


def convert(data, capture_time_us, *, thresholds, threshold_basis, decoder_status,
            analysed_frame_count):
    if not isinstance(data, bytes) or len(data) > LIMIT:
        raise ValueError("bounded saved analysis output required")
    # An empty export is the ORDINARY no-cue case, not a malformed one: verified at 8.1, a clip
    # with no black and no freeze produces a zero-byte metadata file. Refusing it would have
    # rejected the commonest honest input, or forced a caller to fabricate a header.
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit caller capture time required")
    if decoder_status not in DECODER_STATUS:
        raise ValueError("the decoder run's own status must be declared")
    if threshold_basis not in THRESHOLD_BASES:
        raise ValueError("the threshold declaration needs its stated basis")
    if type(analysed_frame_count) is not int or analysed_frame_count <= 0:
        # Without a positive analysed count, "no cue" could not be told from "nothing was read".
        raise ValueError("a positive declared analysed frame count is required")
    required = ("black_min_duration", "black_ratio_threshold", "black_pixel_threshold",
                "freeze_noise", "freeze_duration")
    if set(thresholds) != set(required):
        raise ValueError("every analysis threshold must be declared")
    declared = {}
    for name in required:
        token = str(thresholds[name])
        if not NUMBER.fullmatch(token):
            raise ValueError(f"declared {name} must be a complete number")
        declared[name] = timestamp(token, name)
        if declared[name] is None or declared[name] < 0:
            raise ValueError(f"declared {name} must be a non-negative number")
    statement = " ".join(f"{name}={thresholds[name]}" for name in required)
    statement += (f" basis={threshold_basis} "
                  "not_recoverable_from_the_analysis_output_which_does_not_record_the_parameters_"
                  "it_was_run_with_so_this_is_the_callers_declaration_of_what_was_run")
    text = data.decode("utf-8")
    report = dict(analysed_frame_blocks=0, unselected_keys=0)
    events = parse_metadata(text, report)
    # The analysis cannot have examined fewer frames than it emitted blocks for, and the frame
    # index is zero-based, so a declared count below either is inconsistent with the very file it
    # describes. Only that much is enforced: no count is invented and none is derived.
    observed = max(report["analysed_frame_blocks"],
                   0 if report["highest_frame_index"] is None
                   else report["highest_frame_index"] + 1)
    if analysed_frame_count < observed:
        raise ValueError("the declared analysed frame count is below what this output itself shows")
    report["declared_count_consistency"] = (
        "AT_OR_ABOVE_THE_FRAMES_THIS_OUTPUT_ITSELF_SHOWS" if observed
        else "NOT_CONSTRAINED_BY_THIS_OUTPUT_WHICH_SHOWS_NO_FRAME_BLOCK_AT_ALL")
    black = intervals(events, "black", BLACK_START, BLACK_END)
    freeze = intervals(events, "freeze", FREEZE_START, FREEZE_END, FREEZE_DURATION)
    if len(black) + len(freeze) > MAX_INTERVALS:
        raise ValueError("reported intervals exceed bound")
    rows = []
    for kind, found, minimum in (("BLACK_INTERVAL", black, declared["black_min_duration"]),
                                 ("FREEZE_INTERVAL", freeze, declared["freeze_duration"])):
        for index, interval in enumerate(found):
            reported = interval["duration"]
            derived = None
            if reported is None and interval["start"] is not None and interval["end"] is not None:
                derived = interval["end"] - interval["start"]
                # Two finite endpoints can still differ by more than a float can hold, and the
                # subtraction is this path's own arithmetic, so its result is checked too.
                if not math.isfinite(derived):
                    raise ValueError(f"a derived {kind} duration is not finite")
                if derived < 0:
                    raise ValueError(f"a derived {kind} duration cannot be negative")
            duration = reported if reported is not None else derived
            rows.append(dict(
                video_record_kind=kind, video_interval_index=index,
                video_start_s=interval["start"], video_end_s=interval["end"],
                video_duration_s=duration,
                # blackdetect emits no duration metadata at all, so a black duration is arithmetic
                # on two reported timestamps; a freeze duration is the decoder's own figure.
                # A duration the decoder reported but could not express is NOT the same as one it
                # never reported. Where the endpoints allow a fallback the derivation is stated
                # explicitly, so the reported unknown is qualified rather than erased.
                video_duration_source=(
                    "REPORTED_BY_THE_DECODER" if reported is not None
                    else "DERIVED_FROM_THE_REPORTED_START_AND_END_BECAUSE_THE_REPORTED_DURATION_"
                         "WAS_ITSELF_UNKNOWN" if derived is not None
                         and interval["duration_reported"]
                    else "DERIVED_FROM_THE_REPORTED_START_AND_END" if derived is not None
                    else "NOT_AVAILABLE_THE_REPORTED_DURATION_WAS_ITSELF_UNKNOWN"
                    if interval["duration_reported"]
                    else "NOT_AVAILABLE_A_REPORTED_TIMESTAMP_WAS_ITSELF_UNKNOWN"
                    if interval["end_reported"] or interval["start"] is None
                    else "NOT_AVAILABLE_NO_END_WAS_REPORTED"),
                video_end_disposition=(
                    "REPORTED" if interval["end"] is not None
                    else "END_REPORTED_BUT_THE_DECODER_COULD_NOT_EXPRESS_ITS_TIMESTAMP"
                    if interval["end_reported"]
                    else "NO_END_REPORTED_BEFORE_THE_ANALYSIS_ENDED"),
                video_start_attachment_frame=interval["frame"],
                video_start_attachment_pts_time_s=interval["attachment"],
                # The metadata form is not filtered by the declared minimum, so an interval may be
                # shorter than it. Saying which is the whole point of declaring the minimum.
                video_minimum_duration_qualification=(
                    "UNKNOWN_WITHOUT_A_DURATION" if duration is None
                    else "AT_OR_ABOVE_THE_DECLARED_MINIMUM" if duration >= minimum
                    else "BELOW_THE_DECLARED_MINIMUM_THE_METADATA_FORM_REPORTS_EVERY_RUN_"
                         "REGARDLESS_OF_IT"),
                video_threshold_declaration=statement))
    if not rows:
        # Verified at 8.1: a clip with no black and no freeze produces a zero-byte export, so this
        # is the ordinary clean-analysis shape and is reported honestly rather than refused.
        report["no_interval_basis"] = (
            "no selected interval was reported; an empty or cue-free export is the ordinary shape "
            "when neither filter matched, and the declared analysed frame count is what says how "
            "much was examined; under errors-reported this is NOT evidence that none occurred, "
            "and under any status it is not evidence that a camera worked")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, ["record_time_us", "source_document_sha256", *FIELDS],
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(record_time_us=capture_time_us, source_document_sha256=digest,
                             video_decoder_status=decoder_status,
                             video_analysed_frame_count=analysed_frame_count,
                             video_unselected_key_count=report["unselected_keys"],
                             video_time_basis=TIME_BASIS, video_cue_basis=CUE_BASIS,
                             **{key: "" if value is None else value for key, value in row.items()}))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("cue output exceeds bound")
    report.update(output_records=len(rows), black_intervals=len(black), freeze_intervals=len(freeze),
                  open_intervals=sum(1 for i in black + freeze if not i["end_reported"]),
                  intervals_with_an_unknown_reported_end=sum(
                      1 for i in black + freeze if i["end_reported"] and i["end"] is None),
                  intervals_with_an_unknown_reported_duration=sum(
                      1 for i in black + freeze
                      if i["duration_reported"] and i["duration"] is None),
                  declared_thresholds=statement, decoder_status=decoder_status,
                  analysed_frame_count=analysed_frame_count, source_sha256=digest,
                  source_text_hex=text_hex(text), clock="Unknown", pin=PIN,
                  time_basis=TIME_BASIS, cue_basis=CUE_BASIS,
                  scope="observed interval cues from a saved decoder analysis; media is never "
                        "opened, no codec or pixel work happens here, and no cause is diagnosed")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller capture clock; the common clock basis stays Unknown")
    parser.add_argument("--decoder-status", choices=DECODER_STATUS, required=True)
    parser.add_argument("--analysed-frame-count", type=int, required=True)
    parser.add_argument("--threshold-basis", choices=THRESHOLD_BASES, required=True)
    for name in ("black-min-duration", "black-ratio-threshold", "black-pixel-threshold",
                 "freeze-noise", "freeze-duration"):
        parser.add_argument(f"--{name}", required=True,
                            help="the value the analysis was run with; not recoverable from output")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)  # one bounded read; no size/read race
        output, report = convert(
            data, args.capture_time_us, threshold_basis=args.threshold_basis,
            decoder_status=args.decoder_status, analysed_frame_count=args.analysed_frame_count,
            thresholds=dict(black_min_duration=args.black_min_duration,
                            black_ratio_threshold=args.black_ratio_threshold,
                            black_pixel_threshold=args.black_pixel_threshold,
                            freeze_noise=args.freeze_noise,
                            freeze_duration=args.freeze_duration))
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
        (args.output_directory / "source.metadata.txt").write_bytes(data)
    except (ValueError, OSError, UnicodeDecodeError):
        parser.exit(2, "saved video condition conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
