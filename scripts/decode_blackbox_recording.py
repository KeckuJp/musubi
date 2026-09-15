#!/usr/bin/env python3
"""Offline raw Blackbox -> native-unit CSV stage for the existing Musubi CSV reader.

One complete Betaflight log, optionally selected explicitly from a saved dump. The decoder is an operator-selected, independently installed
tool, not downloaded or executed from provider output. Default output is main/merged-slow frames.
Explicit GPS mode exports separate G records; accompanying main/event/GPX files remain sidecars.
Decoding alone is not common-model normalization.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

MAGIC = b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n"
END = b"E\xffEnd of log\0"


def inspect_recording(data, stream="main"):
    if stream not in ("main", "gps"):
        raise ValueError("unsupported stream")
    if not data.startswith(MAGIC) or data.count(MAGIC) != 1:
        raise ValueError("expected one raw Blackbox recording; CSV and multi-log files are unsupported")
    if not data.endswith(END):
        raise ValueError("missing complete End of log event; truncated/unfinalized log is unsupported")
    versions = rb"4\.[23]\.\d+" if stream == "gps" else rb"4\.2\.\d+"
    revision = re.search(rb"(?m)^H Firmware revision:(Betaflight (" + versions + rb") [^\r\n]+)", data)
    if revision is None:
        if stream == "main":
            raise ValueError("this native-unit profile supports Betaflight 4.2.x only; INAV/other versions need their own profile")
        raise ValueError("GPS export accepts Betaflight 4.2.x/4.3.x headers only; another version needs its own verified profile")
    return revision.group(1).decode("ascii")


def check_decoder_report(stderr, stream="main"):
    if stream not in ("main", "gps"):
        raise ValueError("unsupported stream")
    bad = re.findall(r"(\d+) frames failed to decode, rendering (\d+) loop iterations unreadable", stderr)
    if any(int(corrupt) or int(unreadable) for corrupt, unreadable in bad):
        raise ValueError("decoder reported corrupt/unreadable frames")
    frames = re.search(r"(?m)^Frames\s+(\d+)\s", stderr)
    if frames is None or int(frames[1]) == 0:
        raise ValueError("decoder did not report positive main-frame count")
    missing = re.search(r"(\d+) iterations are missing in total", stderr)
    counts = {"main_frames": int(frames[1]), "missing_loop_iterations": int(missing[1]) if missing else 0}
    if stream == "gps":
        gps = re.findall(r"(?m)^G frames\s+(\d+)\s", stderr)
        if len(gps) != 1 or int(gps[0]) == 0:
            raise ValueError("decoder did not report one positive GPS-frame count")
        counts["gps_frames"] = int(gps[0])
    return counts


def select_recording(data, index):
    """Use the fixed decoder's header-marker indexing, without repairing bytes."""
    if type(index) is not int or index < 1 or not data.startswith(MAGIC):
        raise ValueError("one-based log index and a header-starting dump required")
    offsets = [match.start() for match in re.finditer(re.escape(MAGIC), data)]
    if index > len(offsets):
        raise ValueError("selected log index exceeds dump entries")
    boundaries = offsets + [len(data)]
    entries = [{"index": i + 1, "start_byte": start, "end_byte_exclusive": end,
                "sha256": hashlib.sha256(data[start:end]).hexdigest(),
                "status": "SELECTED_PENDING_DECODE" if i + 1 == index else "UNSELECTED_NOT_VALIDATED"}
               for i, (start, end) in enumerate(zip(boundaries, boundaries[1:]))]
    selected = entries[index - 1]
    return data[selected["start_byte"]:selected["end_byte_exclusive"]], {
        "dump_log_candidates": len(entries), "selected_log_index": index,
        "selected_input_sha256": selected["sha256"], "log_ranges": entries}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("decoder", type=Path)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--stream", choices=("main", "gps"), default="main")
    p.add_argument("--log-index", type=int, help="explicit one-based complete log in a saved multi-log dump")
    a = p.parse_args()
    raw = a.input.read_bytes()
    selected, selection = select_recording(raw, a.log_index) if a.log_index is not None else (raw, {})
    revision = inspect_recording(selected, stream=a.stream)
    a.output.mkdir(parents=True, exist_ok=False)
    decoder_input = a.input
    if selection:
        decoder_input = a.output / "selected.bfl"
        decoder_input.write_bytes(selected)
    argv = [str(a.decoder.resolve()), "--stdout", "--index", "1", "--unit-vbat", "raw",
            "--unit-amperage", "raw", "--unit-flags", "raw", str(decoder_input.resolve())]
    if a.stream == "gps":
        decoder_output = a.output / "decoder-output"
        decoder_output.mkdir()
        argv = [str(a.decoder.resolve()), "--index", "1", "--output-dir", str(decoder_output.resolve()),
                "--prefix", "decoded", "--unit-frame-time", "us", "--unit-gps-speed", "mps",
                "--unit-vbat", "raw", "--unit-amperage", "raw", "--unit-flags", "raw", str(decoder_input.resolve())]
    result = subprocess.run(argv, capture_output=True, timeout=60, env={"PATH":os.defpath, "LC_ALL":"C"})
    (a.output / "decoder.stderr.log").write_bytes(result.stderr)
    if result.returncode:
        raise ValueError("external decoder failed")
    counts = check_decoder_report(result.stderr.decode("utf-8", errors="strict"), stream=a.stream)
    selected = (decoder_output / "decoded.01.gps.csv").read_bytes() if a.stream == "gps" else result.stdout
    text = selected.decode("utf-8", errors="strict")
    count_key = "gps_frames" if a.stream == "gps" else "main_frames"
    if len(text.splitlines()) - 1 != counts[count_key]:
        raise ValueError("CSV row count disagrees with selected decoded frames")
    (a.output / "decoded.csv").write_bytes(selected)
    metadata = {"firmware":revision, **counts,
                "input_sha256":hashlib.sha256(raw).hexdigest(),
                "decoder_sha256":hashlib.sha256(a.decoder.read_bytes()).hexdigest(),
                "source_units":{"time":"us", "vbatLatest":"cV", "amperageLatest":"cA"},
                "domain":"Unknown", "domain_basis":"firmware identity is not sufficient to prove vehicle mobility",
                "scope":"main frames with merged slow fields; GPS/events not normalized",
                "common_output":"NOT_RUN"}
    if a.stream == "gps":
        metadata.update({"stream": "gps", "scope": "separate G records; main/event/GPX are unnormalized sidecars",
                         "source_units": {"time": "us", "GPS_coord[0]": "deg", "GPS_coord[1]": "deg",
                                          "GPS_altitude": "dm", "GPS_speed": "m/s"}})
    if selection:
        selection["log_ranges"][a.log_index - 1]["status"] = "DECODED_SELECTED_STREAM_ONLY"
        metadata.update(selection)
    (a.output / "decode.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
