#!/usr/bin/env python3
"""Offline raw Blackbox -> native-unit CSV stage for the existing Musubi CSV reader.

Single complete Betaflight log only. The decoder is an operator-selected, independently installed
tool, not downloaded or executed from provider output. Main/merged-slow frames are the output
scope; GPS/events remain in the original archived recording and are not claimed as normalized.
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


def inspect_recording(data):
    if not data.startswith(MAGIC) or data.count(MAGIC) != 1:
        raise ValueError("expected one raw Blackbox recording; CSV and multi-log files are unsupported")
    if not data.endswith(END):
        raise ValueError("missing complete End of log event; truncated/unfinalized log is unsupported")
    revision = re.search(rb"(?m)^H Firmware revision:(Betaflight (4\.2\.\d+) [^\r\n]+)", data)
    if revision is None:
        raise ValueError("this native-unit profile supports Betaflight 4.2.x only; INAV/other versions need their own profile")
    return revision.group(1).decode("ascii")


def check_decoder_report(stderr):
    bad = re.search(r"(\d+) frames failed to decode, rendering (\d+) loop iterations unreadable", stderr)
    if bad and (int(bad[1]) or int(bad[2])):
        raise ValueError("decoder reported corrupt/unreadable frames")
    frames = re.search(r"(?m)^Frames\s+(\d+)\s", stderr)
    if frames is None or int(frames[1]) == 0:
        raise ValueError("decoder did not report positive main-frame count")
    missing = re.search(r"(\d+) iterations are missing in total", stderr)
    return {"main_frames": int(frames[1]), "missing_loop_iterations": int(missing[1]) if missing else 0}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("decoder", type=Path)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    a = p.parse_args()
    raw = a.input.read_bytes()
    revision = inspect_recording(raw)
    a.output.mkdir(parents=True, exist_ok=False)
    argv = [str(a.decoder.resolve()), "--stdout", "--index", "1", "--unit-vbat", "raw",
            "--unit-amperage", "raw", "--unit-flags", "raw", str(a.input.resolve())]
    result = subprocess.run(argv, capture_output=True, timeout=60, env={"PATH":os.defpath, "LC_ALL":"C"})
    (a.output / "decoder.stderr.log").write_bytes(result.stderr)
    if result.returncode:
        raise ValueError("external decoder failed")
    counts = check_decoder_report(result.stderr.decode("utf-8", errors="strict"))
    text = result.stdout.decode("utf-8", errors="strict")
    if len(text.splitlines()) - 1 != counts["main_frames"]:
        raise ValueError("CSV row count disagrees with decoded main frames")
    (a.output / "decoded.csv").write_bytes(result.stdout)
    metadata = {"firmware":revision, **counts,
                "input_sha256":hashlib.sha256(raw).hexdigest(),
                "decoder_sha256":hashlib.sha256(a.decoder.read_bytes()).hexdigest(),
                "source_units":{"time":"us", "vbatLatest":"cV", "amperageLatest":"cA"},
                "domain":"Unknown", "domain_basis":"firmware identity is not sufficient to prove vehicle mobility",
                "scope":"main frames with merged slow fields; GPS/events not normalized",
                "common_output":"NOT_RUN"}
    (a.output / "decode.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
