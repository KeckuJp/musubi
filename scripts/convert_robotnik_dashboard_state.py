#!/usr/bin/env python3
"""Saved robotnik_msgs/State records as one official dashboard consumes them.

rqt_rcomponent 0.4.13 (4786511a6ab6046756b90c6dded8c94268e8b7c8) subscribes to exactly one type,
`robotnik_msgs/State`, shows its state_description and colours four of the producer's names. This
reads saved records of that subscription; it never subscribes, commands or reaches a device.
"""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path

LIMIT = 16 * 1024 * 1024
RECORD_LIMIT = 30000
OUTPUT_LIMIT = 16 * 1024 * 1024
FLOAT32_MAX = 3.4028234663852886e38
# msg/State.msg at the qualified commit. State.msg is absent (404) at the ROS2 snapshot this repo
# qualifies for BatteryStatus, so that pin deliberately does not qualify this message.
SCHEMA_COMMIT = "012c20a589c0c180dd792f41d46bd84b7391b8ed"
# The message's own constants. 700 keeps the source's misspelling instead of being tidied up.
STATE_NAMES = {100: "INIT", 200: "STANDBY", 300: "READY", 400: "EMERGENCY",
               500: "FAILURE", 600: "SHUTDOWN", 700: "UNKOWN_STATE_700"}
# rcomponent publishes one of two description tables, modelled here as the two functions they are.
# C++ getStateString(int) returns the short names, default "UNKNOWN", with no case for 700; Python
# state_to_string returns the _STATE names, else 'UNKNOWN_STATE'. Neither has a 700 branch.
SHORT_TEXT = {100: "INIT", 200: "STANDBY", 300: "READY", 400: "EMERGENCY",
              500: "FAILURE", 600: "SHUTDOWN"}
LONG_TEXT = {code: name + "_STATE" for code, name in SHORT_TEXT.items()}
CPP_DEFAULT, PYTHON_DEFAULT = "UNKNOWN", "UNKNOWN_STATE"
# What the pinned dashboard actually does with the text. Everything else, FAILURE and SHUTDOWN
# included, falls into its default branch: displayed, not highlighted.
HIGHLIGHTS = {"READY": "GREEN_READY", "READY_STATE": "GREEN_READY",
              "INIT": "GRAY_INIT", "INIT_STATE": "GRAY_INIT",
              "EMERGENCY": "RED_EMERGENCY", "EMERGENCY_STATE": "RED_EMERGENCY",
              "STANDBY": "DARK_YELLOW_STANDBY", "STANDBY_STATE": "DARK_YELLOW_STANDBY"}
FREQUENCY_BASIS = "COMPONENT_SELF_MEASURED_LOOP_RATE_NOT_DASHBOARD_ARRIVAL_RATE"
TIME_BASIS = "CALLER_CAPTURE_CLOCK_MESSAGE_CARRIES_NO_STAMP_OR_FRAME"
FIELDS = ["record_time_us", "source_record_index", "source_record_hex", "schema_commit",
          "rcomponent_topic_hex", "reported_state_code", "reported_state_name",
          "reported_state_description_hex", "state_description_convention",
          "state_description_matches_code", "dashboard_highlight",
          "desired_loop_frequency_hz", "real_loop_frequency_hz", "real_loop_frequency_status",
          "loop_frequency_basis", "state_time_basis"]
STATE_KEYS = ("state", "desired_freq", "real_freq", "state_description")


def unique_pairs(pairs):
    """Refuse a duplicated key anywhere in the record rather than silently keeping one value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in saved state record")
        result[key] = value
    return result


def frequency(value, name):
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError(f"invalid {name} type")
    try:
        value = float(value)  # A JSON integer is unbounded; float() on a huge one overflows.
    except OverflowError:
        raise ValueError(f"{name} outside finite float32 range")
    if not math.isfinite(value) or abs(value) > FLOAT32_MAX:
        raise ValueError(f"{name} outside finite float32 range")
    return value


def describe(code, text):
    """Name the producer convention by replaying both tables; never guess from the text."""
    # Each default branch covers every code its table has no case for, 700 included.
    if text == SHORT_TEXT.get(code, CPP_DEFAULT):
        return "SHORT_CPP", "MATCHES_CODE"
    if text == LONG_TEXT.get(code, PYTHON_DEFAULT):
        return "LONG_PYTHON", "MATCHES_CODE"
    if text in SHORT_TEXT.values() or text == CPP_DEFAULT:
        return "SHORT_CPP", "DISAGREES_WITH_CODE"
    if text in LONG_TEXT.values() or text == PYTHON_DEFAULT:
        return "LONG_PYTHON", "DISAGREES_WITH_CODE"
    return "UNRECOGNISED_TEXT", "NOT_A_PRODUCER_NAME"


def convert(text):
    if not text or len(text) > LIMIT:
        raise ValueError("empty or oversized saved state log")
    if "\x00" in text:
        raise ValueError("NUL byte in saved state log")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")

    def write(row):
        writer.writerow(row)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted CSV exceeds bound")

    write(FIELDS)
    report = {"records": 0, "unknown_state_codes": 0, "descriptions_disagreeing_with_code": 0,
              "unrecognised_descriptions": 0, "nonpositive_real_frequencies": 0,
              "unread_declarations": [], "highlighted_rows": 0,
              "source_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
              "schema_commit": SCHEMA_COMMIT, "clock": "Unknown",
              "live_subscription_records": "NOT_OBSERVED"}
    # Split on record separators only; text.splitlines() would also break a record on \x0b, \x0c
    # or  . A trailing \r is a separator byte, so source_record_hex excludes it.
    for index, line in enumerate(text.split("\n")):
        line = line[:-1] if line.endswith("\r") else line
        if not line.strip():
            continue
        if len(line) > RECORD_LIMIT:
            raise ValueError("saved state record exceeds bound")
        record = json.loads(line, object_pairs_hook=unique_pairs)
        if not isinstance(record, dict):
            raise ValueError("state record must be an object")
        # Declarations this selection does not read are reported, never silently dropped.
        for key in sorted(set(record) - {"capture_time_us", "schema_commit", "topic", "state"}):
            if key not in report["unread_declarations"]:
                report["unread_declarations"].append(key)
        if not {"capture_time_us", "topic", "state"} <= set(record):
            raise ValueError("saved state record is missing a required declaration")
        if record.get("schema_commit") != SCHEMA_COMMIT:
            raise ValueError("unqualified robotnik_msgs State schema")
        capture = record["capture_time_us"]
        if type(capture) is not int or type(capture) is bool or not 0 <= capture < 2**63:
            raise ValueError("explicit recorder microsecond timestamp required")
        topic = record["topic"]
        if not isinstance(topic, str) or not topic:
            raise ValueError("declared state topic required")
        state = record["state"]
        if not isinstance(state, dict) or set(state) != set(STATE_KEYS):
            raise ValueError("State carries exactly its four declared fields")
        code = state["state"]
        if type(code) is not int or type(code) is bool or not -2**31 <= code < 2**31:
            raise ValueError("invalid int32 state")
        description = state["state_description"]
        if not isinstance(description, str):
            raise ValueError("invalid state_description")
        desired = frequency(state["desired_freq"], "desired_freq")
        real = frequency(state["real_freq"], "real_freq")
        name = STATE_NAMES.get(code, "UNKNOWN_RETAINED")
        if name == "UNKNOWN_RETAINED":
            report["unknown_state_codes"] += 1
        convention, agreement = describe(code, description)
        if agreement == "DISAGREES_WITH_CODE":
            report["descriptions_disagreeing_with_code"] += 1
        elif agreement == "NOT_A_PRODUCER_NAME":
            report["unrecognised_descriptions"] += 1
        highlight = HIGHLIGHTS.get(description, "DEFAULT_NO_HIGHLIGHT")
        if highlight != "DEFAULT_NO_HIGHLIGHT":
            report["highlighted_rows"] += 1
        # A non-positive reported rate is retained as reported, never sanitised into a measurement.
        status = "REPORTED" if real > 0 else "NONPOSITIVE_RETAINED"
        if status == "NONPOSITIVE_RETAINED":
            report["nonpositive_real_frequencies"] += 1
        write([capture, index, "hex:" + line.encode().hex(), SCHEMA_COMMIT,
               "hex:" + topic.encode().hex(), code, name,
               "hex:" + description.encode().hex(), convention, agreement, highlight,
               desired, real, status, FREQUENCY_BASIS, TIME_BASIS])
        report["records"] += 1
    if not report["records"]:
        raise ValueError("no saved State record")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        with args.input.open("rb") as stream:
            raw = stream.read(LIMIT + 1)  # one bounded read before decoding
        if len(raw) > LIMIT:
            raise ValueError("input exceeds bound")
        # Strict decode with no universal-newline translation: the digest must bind the exact
        # input bytes, CRLF included, not a normalised copy of them.
        output, report = convert(raw.decode("utf-8"))
        args.output_directory.mkdir()  # Refuse an existing output/evidence directory.
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, KeyError, OverflowError, UnicodeDecodeError, json.JSONDecodeError):
        parser.exit(2, "Robotnik dashboard state conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
