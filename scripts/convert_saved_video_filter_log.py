#!/usr/bin/env python3
"""Fixed FFmpeg 8.1 plain saved filter diagnostics -> existing observed interval cues, offline."""
import argparse
import csv
import io
import json
from pathlib import Path
import re

try:
    from scripts import convert_saved_video_conditions as cue
except ModuleNotFoundError:
    import convert_saved_video_conditions as cue

MAX_LINES = 100_000
MAX_LINE_BYTES = 4096
MAX_REPORT_BYTES = 32 * 1024 * 1024
BLACK_DURATION = "lavfi.black_duration"
CONTEXT_PART = r"\[[^\[\]\x00-\x1f]{1,96} @ 0x[0-9a-fA-F]{1,16}\]"
CONTEXT = re.compile(CONTEXT_PART + r"(?: " + CONTEXT_PART + r")?")
TOKEN = r"(\S{1,64})"
BLACK = re.compile(r"black_start:" + TOKEN + r" black_end:" + TOKEN + r" black_duration:" + TOKEN)
FREEZE = re.compile(r"(lavfi\.freezedetect\.freeze_(?:start|duration|end)): " + TOKEN)
EXTRA_FIELDS = ["video_log_context_hex", "video_source_line_start", "video_source_line_end",
                "video_pts_order"]
TIME_BASIS = (cue.TIME_BASIS + "_REPORTED_FREEZE_DURATION_CAN_USE_FRAME_RATE_FALLBACK_WHEN_PTS_"
              "IS_MISSING_OR_REVERSED_NO_DURATION_IS_RECOMPUTED_FROM_A_FABRICATED_FRAME_NUMBER")


def convert(data, capture_time_us, *, black_context, freeze_context, thresholds,
            threshold_basis, decoder_status, analysed_frame_count):
    contexts = (black_context, freeze_context)
    if any(not isinstance(c, str) or not CONTEXT.fullmatch(c) for c in contexts):
        raise ValueError("explicit plain logger context prefixes required")
    if black_context == freeze_context:
        raise ValueError("black and freeze contexts must differ")
    traces = {"BLACK_INTERVAL": [], "FREEZE_INTERVAL": []}

    def parse(text, report):
        if text and not text.endswith("\n"):
            raise ValueError("unterminated saved log line")
        lines = text.split("\n")[:-1] if text else []
        if len(lines) > MAX_LINES:
            raise ValueError("saved log line bound")
        events, selected, excluded = [], [], []
        current, duration_seen = None, False
        report["highest_frame_index"] = None  # Logs carry no attachment frames.
        for number, line in enumerate(lines, 1):
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                raise ValueError("saved log line byte bound")
            if "Last message repeated" in line:
                raise ValueError("suppressed log multiplicity cannot be reconstructed")
            line = line.removesuffix("\r")
            which = next((c for c in sorted(contexts, key=len, reverse=True)
                          if line.startswith(c + " ")), None)
            body = line[len(which) + 1:] if which else ""
            black = BLACK.fullmatch(body) if which == black_context else None
            freeze = FREEZE.fullmatch(body) if which == freeze_context else None
            if black:
                # One writer line reports the three values. Order feeds the shared resolver.
                events.extend((None, None, key, value) for key, value in
                              ((cue.BLACK_START, black[1]), (BLACK_DURATION, black[3]),
                               (cue.BLACK_END, black[2])))
                traces["BLACK_INTERVAL"].append((which, number, number))
            elif freeze:
                key, value = freeze.groups()
                if key == cue.FREEZE_START:
                    if current is not None:
                        raise ValueError("second freeze start before end")
                    current, duration_seen = number, False
                    traces["FREEZE_INTERVAL"].append([which, number, None])
                elif key == cue.FREEZE_DURATION:
                    if current is None or duration_seen:
                        raise ValueError("orphan or repeated freeze duration")
                    duration_seen = True
                else:
                    if current is None or not duration_seen:
                        raise ValueError("freeze end lacks preceding start/duration")
                    traces["FREEZE_INTERVAL"][-1][2] = number
                    current = None
                events.append((None, None, key, value))
            else:
                if which and (body.startswith(("black_start", "black_end", "black_duration",
                                               "lavfi.freezedetect.freeze_"))):
                    raise ValueError("malformed or wrong-context selected cue")
                excluded.append(dict(line=number, reason="OTHER_DIAGNOSTIC" if which
                                     else "OUTSIDE_DECLARED_CONTEXT_OR_UNSUPPORTED_PREFIX"))
                continue
            selected.append(number)
        report.update(source_lines=len(lines), selected_cue_lines=selected,
                      unselected_lines=excluded, unselected_keys=0)
        return events

    output, report = cue._convert(data, capture_time_us, thresholds=thresholds,
        threshold_basis=threshold_basis, decoder_status=decoder_status,
        analysed_frame_count=analysed_frame_count, parse=parse, black_duration_key=BLACK_DURATION,
        allow_reversed_freeze=True,
        below_minimum="BELOW_THE_CALLER_DECLARED_MINIMUM_LOG_VALUES_AND_DECLARATION_NOT_RECONCILED")
    source_rows = csv.DictReader(io.StringIO(output))
    result = io.StringIO(newline="")
    writer = csv.DictWriter(result, [*source_rows.fieldnames, *EXTRA_FIELDS], lineterminator="\n")
    writer.writeheader()
    for row in source_rows:
        context, start, end = traces[row["video_record_kind"]][int(row["video_interval_index"])]
        row.update(video_log_context_hex=cue.text_hex(context), video_source_line_start=start,
                   video_source_line_end="" if end is None else end, video_time_basis=TIME_BASIS)
        row["video_pts_order"] = (
            "NO_REPORTED_END" if end is None else "UNKNOWN_REPORTED_PTS"
            if not row["video_start_s"] or not row["video_end_s"] else
            "REVERSED_PTS_WITH_REPORTED_DURATION"
            if float(row["video_end_s"]) < float(row["video_start_s"])
            else "NONDECREASING_REPORTED_PTS")
        writer.writerow(row)
    output = result.getvalue()
    if len(output.encode("utf-8")) > cue.OUTPUT_LIMIT:
        raise ValueError("cue output exceeds bound")
    report.update(pin="ffmpeg-8.1-plain-blackdetect-freezedetect-log", time_basis=TIME_BASIS,
                  declared_contexts=dict(black=black_context, freeze=freeze_context),
                  context_basis="CALLER_SELECTED_EPHEMERAL_LOG_PREFIX_NOT_IDENTITY_OR_SHARED_STREAM",
                  attachment_basis="FRAME_AND_ATTACHMENT_PTS_NOT_REPORTED_BY_THIS_LOG_FORM",
                  source_form="plain logger prefixes; caller supplied one analysis run",
                  black_end_basis="WRITER_MAY_REPORT_LAST_PICTURE_PTS_AT_EOF_NOT_LAST_FRAME_END")
    if not report["output_records"]:
        report["no_interval_basis"] = ("no selected cue in supplied log; not proof of absence or "
            "camera health: selector, minimum, loglevel, errors and incomplete collection may hide cues")
    if len(json.dumps(report, indent=2).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("saved log report bound")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--black-context", required=True)
    parser.add_argument("--freeze-context", required=True)
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--decoder-status", choices=cue.DECODER_STATUS, required=True)
    parser.add_argument("--analysed-frame-count", type=int, required=True)
    parser.add_argument("--threshold-basis", choices=cue.THRESHOLD_BASES, required=True)
    names = ("black_min_duration", "black_ratio_threshold", "black_pixel_threshold",
             "freeze_noise", "freeze_duration")
    for name in names: parser.add_argument("--" + name.replace("_", "-"), required=True)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream: data = stream.read(cue.LIMIT + 1)
        output, report = convert(data, args.capture_time_us, black_context=args.black_context,
            freeze_context=args.freeze_context, thresholds={n: getattr(args, n) for n in names},
            threshold_basis=args.threshold_basis, decoder_status=args.decoder_status,
            analysed_frame_count=args.analysed_frame_count)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (args.output_directory / "source.log.txt").write_bytes(data)
    except (ValueError, OSError):
        parser.exit(2, "saved filter log conversion failed; no successful conversion claim\n")


if __name__ == "__main__": main()
