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


# Betaflight 4.2.0 writes `gyro_scale` as castFloatBytesToInt(1.0f), unconditionally and always
# this value. Accepting exactly it is a qualification of that one release, not general
# compatibility with any float another version might write.
SI_GYRO_SCALE = "0x3f800000"
# `acc_1G` is printed with %u from acc.dev.acc_1G, a uint16_t, so a complete positive token only.
SI_ACC_1G_MAX = 65535
# The SI motion export is qualified for this exact release; the native and GPS paths keep their
# own wider version scopes untouched.
SI_FIRMWARE_VERSION = "4.2.0"

# Betaflight 4.2.0 writes `motorOutput` unconditionally (no #ifdef, unlike the `acc_1G` line four
# lines below it), printing two uint16_t values with "%d,%d". So each is a complete unsigned token
# in this range, and the pair is the output scale the firmware itself declared.
MOTOR_OUTPUT_MAX = 65535
# The writer defines field rows motor[0]..motor[7] under CONDITION(AT_LEAST_MOTORS_1..8) and emits
# an indexed field as name + "[%d]", so the logged columns are a contiguous motor[0..n-1], n <= 8.
MOTOR_INDEX_PATTERN = r"motor\[[0-7]\]"
# The motor declaration is qualified for this exact release, like the SI export above.
MOTOR_FIRMWARE_VERSION = "4.2.0"


def header_lines(data):
    """The initial H header block only, stopping at the first line that is not one.

    A header-looking byte sequence inside frame payload is not a header, so the scaling metadata
    is never satisfied by something the writer did not put in the header block.
    """
    lines, offset = [], 0
    while True:
        end = data.find(b"\n", offset)
        if end < 0 or not data[offset:end].startswith(b"H "):
            return lines
        lines.append(data[offset:end])
        offset = end + 1


def motion_scaling_headers(data):
    """The header lines the SI motion conversion needs, or a refusal naming what is absent.

    Betaflight 4.2.0 writes `gyro_scale` unconditionally but `acc_1G` only under `#if USE_ACC`,
    and the pinned decoder silently substitutes 1 for a missing scaling line instead of warning.
    Missing metadata is refused here rather than guessed, and each value must be a complete token
    the pinned writer could have produced; a partial or trailing-garbage match is not accepted.
    """
    lines, found = header_lines(data), {}
    for name in ("gyro_scale", "acc_1G"):
        prefix = b"H " + name.encode("ascii") + b":"
        values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        if not values:
            raise ValueError(f"SI motion export requires the {name} header; it is absent from the "
                             "header block and cannot be assumed")
        if len(values) > 1:
            # The pinned writer emits each of these once, so a repeat is malformed rather than a
            # value to choose between.
            raise ValueError(f"SI motion export refuses a repeated {name} header")
        found[name] = values[0].decode("ascii", errors="strict")
    if found["gyro_scale"] != SI_GYRO_SCALE:
        raise ValueError("SI motion export accepts only the gyro_scale Betaflight 4.2.0 writes")
    if not re.fullmatch(r"[0-9]{1,5}", found["acc_1G"]) or not 1 <= int(found["acc_1G"]) <= SI_ACC_1G_MAX:
        raise ValueError("SI motion export requires a complete positive acc_1G header value")
    return found


def require_si_firmware(revision):
    """The SI schema is qualified for one release; a 4.2.x sibling is not quietly accepted."""
    version = revision.split(" ")[1] if revision.count(" ") >= 2 else ""
    if version != SI_FIRMWARE_VERSION:
        raise ValueError("SI motion export is qualified for Betaflight " + SI_FIRMWARE_VERSION
                         + " only; another version needs its own qualification")
    return version


def require_motor_firmware(revision):
    """The motor declaration is qualified for one release; a 4.2.x sibling is not quietly accepted.

    The name is checked as well as the number: `inspect_recording` has already rejected other
    firmware on the main stream, but a version token alone would let a same-numbered release of
    different firmware qualify, and a matching number is not a qualification.
    """
    parts = revision.split(" ")
    version = parts[1] if len(parts) >= 3 and parts[0] == "Betaflight" else ""
    if version != MOTOR_FIRMWARE_VERSION:
        raise ValueError("motor output export is qualified for Betaflight " + MOTOR_FIRMWARE_VERSION
                         + " only; another version needs its own qualification")
    return version


def motor_output_declaration(data):
    """The motor columns and the output scale the writer declared, or a refusal naming what is absent.

    Read from the initial header block only, like the SI motion metadata, so a header-looking byte
    sequence inside frame payload cannot supply a scale the writer did not declare. The scale is
    what the firmware stated about its own output, never a measurement, and it is not inferred from
    the logged values when the header is missing.
    """
    lines = header_lines(data)
    prefix = b"H motorOutput:"
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if not values:
        raise ValueError("motor output export requires the motorOutput header; it is absent from "
                         "the header block and cannot be assumed")
    if len(values) > 1:
        # The pinned writer emits it once, so a repeat is malformed rather than a value to choose.
        raise ValueError("motor output export refuses a repeated motorOutput header")
    pair = re.fullmatch(r"([0-9]{1,5}),([0-9]{1,5})",
                        values[0].decode("ascii", errors="strict"))
    if pair is None:
        raise ValueError("motor output export requires two complete unsigned motorOutput values; a "
                         "partial or trailing-garbage match is not accepted")
    low, high = int(pair[1]), int(pair[2])
    if low > MOTOR_OUTPUT_MAX or high > MOTOR_OUTPUT_MAX:
        raise ValueError("motor output export requires motorOutput values in the uint16 range the "
                         "writer prints")
    if low >= high:
        # Without a positive span there is no output scale to declare, so this is refused rather
        # than interpreted into one.
        raise ValueError("motor output export requires a positive declared output span")
    return {"low": low, "high": high, "columns": motor_field_names(lines)}


def motor_field_names(lines):
    """The contiguous motor columns the main-frame field header names, or a refusal."""
    prefix = b"H Field I name:"
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        raise ValueError("motor output export requires exactly one main-frame field name header")
    names = values[0].decode("ascii", errors="strict").split(",")
    motors = [name for name in names if re.fullmatch(MOTOR_INDEX_PATTERN, name)]
    if not motors:
        raise ValueError("this recording declares no motor field; the writer omits them when the "
                         "motor conditions do not hold, and their absence is not a failure to repair")
    if motors != [f"motor[{index}]" for index in range(len(motors))]:
        raise ValueError("motor output export requires the writer's contiguous motor[0..n] columns")
    return motors


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
    p.add_argument("--motion-units", choices=("native", "si"), default="native",
                   help="ask the decoder for SI rotation and acceleration; the default native "
                        "path and its columns are unchanged")
    p.add_argument("--motor-output", choices=("omitted", "declared"), default="omitted",
                   help="record the writer's own motorOutput scale and motor columns in "
                        "decode.json; no CSV column and no existing metadata changes")
    a = p.parse_args()
    raw = a.input.read_bytes()
    selected, selection = select_recording(raw, a.log_index) if a.log_index is not None else (raw, {})
    revision = inspect_recording(selected, stream=a.stream)
    a.output.mkdir(parents=True, exist_ok=False)
    decoder_input = a.input
    if selection:
        decoder_input = a.output / "selected.bfl"
        decoder_input.write_bytes(selected)
    motion = {}
    if a.motion_units == "si":
        if a.stream != "main":
            raise ValueError("SI motion export applies to the main stream")
        require_si_firmware(revision)
        motion = motion_scaling_headers(selected)
    motor = {}
    if a.motor_output == "declared":
        if a.stream != "main":
            raise ValueError("motor output export applies to the main stream")
        require_motor_firmware(revision)
        motor = motor_output_declaration(selected)
    argv = [str(a.decoder.resolve()), "--stdout", "--index", "1", "--unit-vbat", "raw",
            "--unit-amperage", "raw", "--unit-flags", "raw"]
    if motion:
        argv += ["--unit-rotation", "rad/s", "--unit-acceleration", "m/s/s"]
    argv.append(str(decoder_input.resolve()))
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
                "motion_units": a.motion_units,
                "domain":"Unknown", "domain_basis":"firmware identity is not sufficient to prove vehicle mobility",
                "scope":"main frames with merged slow fields; GPS/events not normalized",
                "common_output":"NOT_RUN"}
    if motion:
        metadata["motion_scaling_headers"] = motion
        metadata["source_units"].update({f"gyroADC[{i}]": "rad/s" for i in range(3)})
        metadata["source_units"].update({f"accSmooth[{i}]": "m/s/s" for i in range(3)})
        metadata["motion_axis_basis"] = ("decoder index 0/1/2 after the firmware's own sensor and "
                                         "board alignment; 4.2.0 logs no alignment header, so no "
                                         "body frame is established")
    if motor:
        metadata["motor_output"] = {
            "declared_low": motor["low"], "declared_high": motor["high"],
            "columns": motor["columns"], "motor_count": len(motor["columns"]),
            "scale_basis": ("the firmware's own motorOutput header line; the writer declared this "
                            "about its own output and nothing measured it"),
            "meaning": "commanded mixer output per motor at log time",
            "not": ("not measured rotation, not eRPM, not thrust, not current, and not evidence "
                    "that any motor turned; an unplugged or stalled motor logs the same column"),
            "below_low_is_legitimate": ("the writer predicts motor[0] from motorOutputLow but its "
                                        "own comment records that motors drop under minthrottle "
                                        "when the stick falls below mincommand, so a value under "
                                        "the declared low is data, not corruption"),
            "measured_rpm": "ABSENT_AT_BETAFLIGHT_4_2_0",
            "pole_count": "NOT_APPLICABLE_NO_ERPM_FIELD_AT_THIS_VERSION"}
        metadata["source_units"].update(
            {name: "motor_output_demand_raw_in_the_declared_motorOutput_scale"
             for name in motor["columns"]})
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
