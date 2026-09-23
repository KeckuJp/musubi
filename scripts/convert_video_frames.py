#!/usr/bin/env python3
"""Saved ffprobe8.1 JSON -> full frame metadata observations; never opens media."""
import argparse
import csv
import io
import json
import hashlib
import math
import struct
from pathlib import Path
import re

try:
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant

LIMIT = 16 * 1024 * 1024


def unsigned(value, maximum=2**63 - 1):
    if type(value) not in (str, int) or not re.fullmatch(r"[0-9]{1,19}", str(value)):
        raise ValueError("invalid unsigned value")
    result = int(value)
    if result > maximum:
        raise ValueError("integer outside finite scope")
    return result


def retained(value):
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(result) > 12000:
        raise ValueError("retained frame/stream exceeds bound")
    return "hex:" + result.hex()


def convert(text, selected_stream, *, compact=False):
    if type(compact) is not bool:
        raise ValueError("explicit compact boolean required")
    if len(text.encode()) > (2 * LIMIT if compact else LIMIT):
        raise ValueError("input exceeds bound")
    try:
        source = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        if source["program_version"]["version"] != "8.1" or type(selected_stream) is not int or selected_stream < 0:
            raise ValueError("unconfirmed exporter version or selected stream")
        if "error" in source or not isinstance(source["streams"], list) or not isinstance(source["frames"], list):
            raise ValueError("failed or incomplete probe export")
        streams = {}
        for stream in source["streams"]:
            index = unsigned(stream["index"])
            if index in streams:
                raise ValueError("duplicate stream index")
            streams[index] = stream
        stream = streams[selected_stream]
        if stream["codec_type"] != "video":
            raise ValueError("selected stream is not video")
        parts = stream["time_base"].split("/")
        if len(parts) != 2:
            raise ValueError("invalid rational timebase")
        numerator, denominator = (unsigned(x, 2**31 - 1) for x in parts)
        if not numerator or not denominator:
            raise ValueError("nonpositive timebase")
        declared_count = unsigned(stream["nb_read_frames"])
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["record_time_us", "source_frame_index", "stream_index",
            "presentation_timestamp_ticks", "timebase_numerator", "timebase_denominator",
            "width_px", "height_px", "key_frame_reported", "picture_type_reported",
            "source_frame_hex", "source_stream_hex"] + (["source_stream_sha256"] if compact else []), lineterminator="\n")
        writer.writeheader()
        report = {"source_frames": len(source["frames"]), "selected_video_frames": 0, "timed_video_frames": 0,
            "untimed_video_frames": 0, "unsupported_frames": 0, "retained_unconverted_frames": [],
            "clock": "Unknown", "source_metadata": {k: v for k, v in source.items() if k != "frames"}}
        previous, report_bytes = None, len(json.dumps(report).encode())
        stream_hex = retained(stream)
        stream_sha = "sha256:" + hashlib.sha256(bytes.fromhex(stream_hex[4:])).hexdigest()
        if compact:
            report.update(selected_stream_hex=stream_hex, selected_stream_sha256=stream_sha,
                          stream_binding="canonical retained stream JSON bytes; not authentication")
            report_bytes = len(json.dumps(report).encode())
            if report_bytes > LIMIT:
                raise ValueError("source metadata report exceeds bound")
        for index, frame in enumerate(source["frames"]):
            if not isinstance(frame, dict):
                raise ValueError("invalid frame object")
            stream_index = unsigned(frame["stream_index"])
            selected = stream_index == selected_stream
            if selected and frame["media_type"] != "video":
                raise ValueError("stream/frame type disagreement")
            if selected:
                report["selected_video_frames"] += 1
            untimed = selected and frame.get("pts") in (None, "N/A")
            if not selected or untimed:
                record = {"index": index, "reason": "MISSING_PTS" if untimed else "UNSELECTED_STREAM", "frame": frame}
                report_bytes += len(json.dumps(record).encode())
                if report_bytes > LIMIT:
                    raise ValueError("unconverted report exceeds bound")
                report["retained_unconverted_frames"].append(record)
                report["untimed_video_frames" if untimed else "unsupported_frames"] += 1
                continue
            pts = unsigned(frame["pts"])
            time_us = pts * numerator * 1000000 // denominator
            if time_us > 2**63 - 1 or (previous is not None and pts < previous):
                raise ValueError("out-of-range or decreasing PTS; no timestamp repair")
            previous = pts
            width, height = unsigned(frame["width"], 65535), unsigned(frame["height"], 65535)
            if not width or not height:
                raise ValueError("invalid frame dimensions")
            key = unsigned(frame["key_frame"], 1)
            picture = frame.get("pict_type")
            # Keep unknown labels through exact source; no fabricated coding semantics.
            picture = picture if picture in ("I", "P", "B", "S", "SI", "SP", "BI") else "UNKNOWN"
            writer.writerow({"record_time_us": time_us, "source_frame_index": index, "stream_index": selected_stream,
                "presentation_timestamp_ticks": pts, "timebase_numerator": numerator, "timebase_denominator": denominator,
                "width_px": width, "height_px": height, "key_frame_reported": key, "picture_type_reported": picture,
                "source_frame_hex": retained(frame), "source_stream_hex": "" if compact else stream_hex,
                **({"source_stream_sha256": stream_sha} if compact else {})})
            report["timed_video_frames"] += 1
            if output.tell() > (8 * LIMIT if compact else LIMIT):
                raise ValueError("converted output exceeds bound")
        if report["selected_video_frames"] != declared_count or not report["timed_video_frames"]:
            raise ValueError("decoded frame count mismatch or no timed selected frame")
        return output.getvalue(), report
    except (KeyError, TypeError, AttributeError, IndexError, json.JSONDecodeError) as error:
        raise ValueError("malformed frame export") from error


def deepstream_detections(record):
    """Caller-saved NvDs metadata, not nvmsgconv JSON or a running SDK binding."""
    if record["exporter_version"] != "9.0.0" or record["coordinate_basis"] != "pipeline_output_pixels":
        raise ValueError("unqualified DeepStream version or coordinate basis")
    objects = record["objects"]
    if not isinstance(objects, list):
        raise ValueError("saved object list required")
    detections = []
    for obj in objects:
        rect = obj["rect_params"]
        values = [rect[key] for key in ("left", "top", "width", "height")]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("invalid pipeline rectangle")
        left, top, width, height = values
        identifier = obj["object_id"]
        if type(identifier) is not int or not 0 <= identifier <= 2**64 - 1:
            raise ValueError("invalid uint64 object id")
        score = obj["confidence"]
        unavailable = type(score) is float and score in (-.1, struct.unpack("<f", struct.pack("<f", -.1))[0])
        detections.append(dict(name=obj["obj_label"], **{"class": obj["class_id"]},
            confidence=None if unavailable else score, score_status="UNAVAILABLE_SOURCE_SENTINEL" if unavailable else "REPORTED",
            box=dict(x1=left, y1=top, x2=left + width, y2=top + height),
            track_id_text="" if identifier == 2**64 - 1 else "u64:" + str(identifier),
            tracking_status="UNTRACKED_REPORTED" if identifier == 2**64 - 1 else "REPORTED_NOT_AUTHENTICATED"))
    return dict(record, image_width=record["pipeline_width"], image_height=record["pipeline_height"],
                normalized=False, detections=detections)


# `-progress` block keys ffmpeg 8.1 writes for a video output (fftools/ffmpeg.c print_report).
# `out_time_ms` prints the same pts as `out_time_us`, so it is microseconds despite its name.
PROGRESS_COUNTERS = ("frame", "dup_frames", "drop_frames", "total_size")
PROGRESS_TEXT = ("fps", "bitrate", "speed", "out_time", "out_time_ms")
PROGRESS_FIELDS = ["record_time_us", "progress_block_index", "output_frames_reported",
                   "dup_frames_reported", "drop_frames_reported", "out_time_us_reported",
                   "out_time_text_reported", "total_size_bytes_reported", "fps_text_reported",
                   "bitrate_text_reported", "speed_text_reported", "progress_state",
                   "frame_counter_basis", "counter_scope", "time_basis", "recording_stop_disposition",
                   "unterminated_final_block_bytes", "source_block_offset",
                   "source_block_length", "source_block_hex", "source_sha256"]
# What the saved file records about the end of the writer's own run. A property of the file, so it
# is the same on every row of one run. None of these says a camera stopped capturing or that a
# recording succeeded: an absent `end` block is not a failure, and a truncated final block says the
# saved file stops mid-block, nothing about the device.
STOP_END_LAST = "REPORTED_END_BLOCK_AS_THE_LAST_SAVED_BLOCK"
STOP_END_EARLIER = "REPORTED_END_BLOCK_WITH_LATER_SAVED_BLOCKS"
STOP_TRUNCATED = "NO_END_BLOCK_FINAL_SAVED_BLOCK_TRUNCATED"
STOP_NO_TERMINAL = "NO_END_BLOCK_LAST_SAVED_BLOCK_WAS_CONTINUE"
# The counters come from one output stream's filter graph, never from a camera or a link.
COUNTER_BASIS = "PROCESSING_FRAME_RATE_CONVERSION_NOT_CAPTURE_OR_TRANSPORT_LOSS"
COUNTER_SCOPE = "FIRST_VIDEO_OUTPUT_STREAM_NOT_AGGREGATED"
# print_report receives the scheduler's transcode timestamp, while the counters above come from
# the first video output stream, so the time is not that stream's own frame timestamp.
TIME_BASIS = "SCHEDULER_TRANSCODE_TIMESTAMP_NOT_THE_COUNTED_STREAMS_FRAME_TIME"


def progress_blocks(text):
    """Blocks with the exact byte span each one occupies in the input.

    A block ends at its `progress=` line. A trailing block without one is an interrupted run,
    returned as incomplete rather than dropped or completed.
    """
    total = len(text.encode())
    blocks, block, start, offset = [], {}, 0, 0
    for line in text.split("\n"):
        width = len(line.encode()) + 1  # the separator the writer put after this line
        # A trailing carriage return is separator, not value; the retained span still keeps it.
        line = line[:-1] if line.endswith("\r") else line
        if not line.strip():
            offset += width
            if not block:
                start = min(offset, total)  # blank space before a block is not part of it
            continue
        key, separator, value = line.partition("=")
        if not separator or not key:
            raise ValueError("invalid progress line")
        if key in block:
            raise ValueError("duplicate key in one progress block")
        block[key] = value
        offset += width
        if key == "progress":
            blocks.append((block, start, min(offset, total), True))
            block, start = {}, min(offset, total)
    if block:
        blocks.append((block, start, min(offset, total), False))
    if not blocks:
        raise ValueError("no progress block")
    return blocks


def convert_progress(text):
    """Saved ffmpeg8.1 `-progress` blocks -> reported processing counters. No media is opened."""
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized progress input")
    raw = text.encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=PROGRESS_FIELDS, lineterminator="\n")
    writer.writeheader()
    report = {"blocks": 0, "complete_blocks": 0, "timed_blocks": 0, "untimed_blocks": 0,
              "retained_untimed_blocks": [], "incomplete_final_block": None, "unknown_keys": [],
              "terminal_state": "INCOMPLETE_NO_TERMINAL_BLOCK", "source_sha256": digest,
              "clock": "Unknown", "counter_basis": COUNTER_BASIS, "counter_scope": COUNTER_SCOPE,
              "time_basis": TIME_BASIS}
    previous = {name: None for name in ("frame", "dup_frames", "drop_frames")}
    blocks = progress_blocks(text)
    # The verdict about the end of the run is known only from the whole block list, so it is derived
    # here and then carried by every row. Two states the report used to share are now distinct: a
    # file whose last saved block is complete but is not an `end` block reports no terminal record,
    # which is not the same evidence as a file that stops mid-block.
    ends = [index for index, (block, _, _, terminated) in enumerate(blocks)
            if terminated and block.get("progress") == "end"]
    if ends:
        disposition = STOP_END_LAST if ends[-1] == len(blocks) - 1 else STOP_END_EARLIER
    elif not blocks[-1][3]:
        disposition = STOP_TRUNCATED
    else:
        disposition = STOP_NO_TERMINAL
        report["terminal_state"] = "NO_TERMINAL_BLOCK_LAST_BLOCK_WAS_CONTINUE"
    trailing_bytes = 0 if blocks[-1][3] else blocks[-1][2] - blocks[-1][1]
    report["recording_stop_disposition"] = disposition
    for index, (block, begin, stop, terminated) in enumerate(blocks):
        report["blocks"] += 1
        span = raw[begin:stop]
        retained_block = "hex:" + span.hex()
        for key in block:
            if key not in PROGRESS_COUNTERS + PROGRESS_TEXT + ("out_time_us", "progress") \
                    and key not in report["unknown_keys"]:
                # Named here, and their values stay recoverable from the retained raw span.
                report["unknown_keys"].append(key)
        if not terminated:
            if index != len(blocks) - 1:
                raise ValueError("unterminated block before the end of the input")
            # An interrupted run stops mid-block. It is retained exactly as written, with no
            # counter or time invented for it, and it is not a row.
            report["incomplete_final_block"] = {
                "index": index, "reason": "NO_TERMINAL_LINE_INCOMPLETE_FINAL_BLOCK",
                "source_block_offset": begin, "source_block_length": stop - begin,
                "source_block_hex": retained_block, "keys_present": sorted(block)}
            continue
        state = block["progress"]
        if state not in ("continue", "end"):
            # Only the writer's own terminators are known; anything else is not this format.
            raise ValueError("unsupported progress terminator")
        report["complete_blocks"] += 1
        if state == "end":
            report["terminal_state"] = "END" if index == len(blocks) - 1 else "END_BEFORE_LAST_BLOCK"
        values = {}
        for name in PROGRESS_COUNTERS:
            if name not in block:
                raise ValueError("progress block is missing a declared counter")
            values[name] = unsigned(block[name]) if block[name] != "N/A" else None
            if name in previous and values[name] is not None:
                # The writer never resets these; a decrease is not a reset to absorb.
                if previous[name] is not None and values[name] < previous[name]:
                    raise ValueError("progress counter decreased; no reset assumed")
                previous[name] = values[name]
        micro, milli = block.get("out_time_us", "N/A"), block.get("out_time_ms", "N/A")
        if micro != "N/A" and milli != "N/A" and unsigned(micro) != unsigned(milli):
            # The pinned writer prints the same pts under both names; a disagreement is not
            # a unit difference to reconcile.
            raise ValueError("out_time_us and out_time_ms disagree")
        if micro == "N/A":
            report["untimed_blocks"] += 1
            report["retained_untimed_blocks"].append({
                "index": index, "reason": "NO_OUT_TIME", "source_block_offset": begin,
                "source_block_length": stop - begin, "source_block_hex": retained_block})
            continue
        time_us = unsigned(micro)
        writer.writerow({"record_time_us": time_us, "progress_block_index": index,
            "output_frames_reported": values["frame"], "dup_frames_reported": values["dup_frames"],
            "drop_frames_reported": values["drop_frames"], "out_time_us_reported": time_us,
            "out_time_text_reported": block.get("out_time", ""),
            "total_size_bytes_reported": values["total_size"],
            "fps_text_reported": block.get("fps", ""), "bitrate_text_reported": block.get("bitrate", ""),
            "speed_text_reported": block.get("speed", ""), "progress_state": state,
            "frame_counter_basis": COUNTER_BASIS, "counter_scope": COUNTER_SCOPE,
            "time_basis": TIME_BASIS, "recording_stop_disposition": disposition,
            "unterminated_final_block_bytes": trailing_bytes, "source_block_offset": begin,
            "source_block_length": stop - begin, "source_block_hex": retained_block,
            "source_sha256": digest})
        report["timed_blocks"] += 1
        if output.tell() > LIMIT:
            raise ValueError("converted output exceeds bound")
    if not report["timed_blocks"]:
        # An input with nothing usable is a refusal, never an empty success.
        raise ValueError("no timed complete progress block")
    report["unknown_keys"].sort()
    return output.getvalue(), report


def convert_detections(text, selected_stream, *, deepstream=False):
    """Saved qualified Ultralytics8.3.0 axis-aligned detections, no inference."""
    if len(text.encode()) > LIMIT or type(selected_stream) is not int or selected_stream < 0 or type(deepstream) is not bool:
        raise ValueError("invalid detection input bound or stream")
    output = io.StringIO(newline="")
    names = ["record_time_us", "stream_index_configured", "source_record_index", "source_record_sha256",
             "image_id_hex", "model_id_hex", "width_px", "height_px", "detection_count",
             "detection_status", "detection_index", "class_id_reported", "class_name_hex",
             "model_score_reported", "box_x1_px", "box_y1_px", "box_x2_px", "box_y2_px",
             "coordinates_normalized_in_source", "track_id_reported", "box_coordinate_basis",
             "model_score_status", "track_id_text", "tracking_status"]
    writer = csv.DictWriter(output, names, lineterminator="\n")
    writer.writeheader()
    report = dict(source_records=[], detections=0, empty_records=0, clock="Unknown",
                  extras="unknown, mask and keypoint fields retained, not interpreted")
    previous = None
    try:
        lines = text.splitlines()
        if not lines:
            raise ValueError("empty detection export")
        for index, line in enumerate(lines):
            record = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            if deepstream:
                record = deepstream_detections(record)
            elif record["exporter_version"] != "8.3.0" or type(record["normalized"]) is not bool:
                raise ValueError("unqualified detection exporter or coordinate basis")
            time = unsigned(record["capture_time_us"])
            width, height = unsigned(record["image_width"], 65535), unsigned(record["image_height"], 65535)
            if not width or not height or (previous is not None and time < previous):
                raise ValueError("invalid dimensions or decreasing capture time")
            previous = time
            for key in ("image_id", "model_id"):
                if not isinstance(record[key], str) or not record[key] or len(record[key].encode()) > 1024:
                    raise ValueError("explicit image/model identifier required")
            detections = record["detections"]
            if not isinstance(detections, list):
                raise ValueError("detection list required")
            digest = "sha256:" + hashlib.sha256(line.encode()).hexdigest()
            report["source_records"].append(dict(index=index, sha256=digest, original_line=line))
            base = dict(record_time_us=time, stream_index_configured=selected_stream, source_record_index=index,
                        source_record_sha256=digest, image_id_hex="hex:" + record["image_id"].encode().hex(),
                        model_id_hex="hex:" + record["model_id"].encode().hex(), width_px=width, height_px=height,
                        detection_count=len(detections), coordinates_normalized_in_source=int(record["normalized"]),
                        box_coordinate_basis="PIPELINE_LAST_COMPONENT_PIXELS" if deepstream else "ORIGINAL_IMAGE_PIXELS")
            if not detections:
                writer.writerow(dict(base, detection_status="NO_DETECTIONS_REPORTED"))
                report["empty_records"] += 1
            for ordinal, item in enumerate(detections):
                class_id = item["class"]
                if type(class_id) is not int or not 0 <= class_id <= 2**31 - 1:
                    raise ValueError("invalid class identifier")
                if not isinstance(item["name"], str) or not item["name"] or len(item["name"].encode()) > 1024:
                    raise ValueError("invalid class label")
                score = item["confidence"]
                if not (deepstream and score is None and item["score_status"] == "UNAVAILABLE_SOURCE_SENTINEL") and (
                        type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1):
                    raise ValueError("invalid model score")
                box = item["box"]
                if not isinstance(box, dict) or set(box) != {"x1", "y1", "x2", "y2"}:
                    raise ValueError("axis-aligned box required; OBB/classification not accepted")
                coordinates = {}
                for key, value in box.items():
                    bound = width if key.startswith("x") else height
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= (1 if record["normalized"] else bound):
                        raise ValueError("invalid image-space coordinate")
                    coordinates["box_" + key + "_px"] = value * bound if record["normalized"] else value
                if coordinates["box_x1_px"] > coordinates["box_x2_px"] or coordinates["box_y1_px"] > coordinates["box_y2_px"]:
                    raise ValueError("inverted image-space box")
                track = item.get("track_id")
                if "track_id" in item and (type(track) is not int or not 0 <= track <= 2**63 - 1):
                    raise ValueError("invalid reported track id")
                writer.writerow(dict(base, detection_status="MODEL_REPORTED_NOT_VERIFIED", detection_index=ordinal,
                    class_id_reported=class_id, class_name_hex="hex:" + item["name"].encode().hex(),
                    model_score_reported=score, track_id_reported=track,
                    model_score_status=item["score_status"] if deepstream else "REPORTED",
                    track_id_text=item["track_id_text"] if deepstream else "",
                    tracking_status=item["tracking_status"] if deepstream else "", **coordinates))
                report["detections"] += 1
                if output.tell() > 8 * LIMIT:
                    raise ValueError("detection CSV exceeds bound")
        if len(json.dumps(report).encode()) > 2 * LIMIT:
            raise ValueError("detection report exceeds bound")
        return output.getvalue(), report
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("malformed detection export") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("stream_index", type=int)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--compact", action="store_true", help="retain selected stream once in report.json and bind rows by SHA256")
    parser.add_argument("--detections-830", action="store_true", help="qualified saved detection envelopes; stream index is caller configured")
    parser.add_argument("--deepstream-900", action="store_true", help="caller-saved NvDs object metadata in pipeline pixel coordinates")
    parser.add_argument("--progress-81", action="store_true", help="saved ffmpeg8.1 -progress blocks; reported processing counters, never capture or transport loss")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > (2 * LIMIT if args.compact else LIMIT):
            raise ValueError("input exceeds bound")
        if (args.detections_830 and args.deepstream_900) or ((args.detections_830 or args.deepstream_900) and args.compact):
            raise ValueError("separate detection/ffprobe selections")
        if args.progress_81 and (args.compact or args.detections_830 or args.deepstream_900):
            raise ValueError("separate progress/ffprobe selections")
        # The progress digest and block spans must bind the exact input, so that mode reads bytes
        # and decodes strictly. The older modes keep their own read, and their digests with it.
        text = (args.input.read_bytes().decode("utf-8") if args.progress_81
                else args.input.read_text(encoding="utf-8"))
        output, report = (convert_progress(text) if args.progress_81 else
                          convert_detections(text, args.stream_index, deepstream=args.deepstream_900)
                          if args.detections_830 or args.deepstream_900 else convert(text, args.stream_index, compact=args.compact))
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError):
        parser.exit(2, "video frame conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
