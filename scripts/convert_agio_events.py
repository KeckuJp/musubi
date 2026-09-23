#!/usr/bin/env python3
"""Saved AgIO diagnostic event text; no device, link or connection is diagnosed.

The writer is AgLibrary.Logging.Log at AgOpenGPS-Official/AgOpenGPS
21be26aa58d09b9b5abacc715d0abcf4a021f1db: EventWriter appends local HH:mm:ss, the
literal "-> ", the message and a CR, so a record is anchored only by that prefix.
The writer does not escape the message, so an exception dump embeds its own newlines
and a timestamp-shaped line inside one is indistinguishable from a real call site: a
classified record is a saved line category candidate under that delimiter assumption,
not a certified boundary or an observed network status. The trim notice carries no
timestamp, and a save failure clears the buffer. This reads what the file says and
reports what it cannot establish rather than reconstructing a session.
"""
import argparse
import csv
import io
import json
import re
from pathlib import Path

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
COLUMNS = ("unlocated_time_us", "source_line", "agio_event_class", "agio_event_class_basis",
           "agio_reported_clock_hhmmss", "agio_reported_clock_basis",
           "agio_session_date_reported", "agio_event_detail_hex",
           "agio_continuation_lines", "agio_record_hex")
ANCHOR = re.compile(r"^([0-9]{2}):([0-9]{2}):([0-9]{2})-> ")
CLOCK_BASIS = "REPORTED_LOCAL_HHMMSS_NO_DATE_ZONE_OR_ROLLOVER"
# EventWriter does not escape the message, so ex.Message and ex.ToString() can carry
# their own newlines and a line inside one can look exactly like a new record, and can
# even match a known literal, without coming from that call site. Splitting on the
# delimiter is an assumption about the saved text, not an observed event boundary.
CLASS_BASIS = "SAVED_LINE_CATEGORY_CANDIDATE_UNDER_DELIMITER_ASSUMPTION_NOT_OBSERVED_STATUS"
BOUNDARY_BASIS = ("unescaped writer: a CR/LF-delimited line matching HH:mm:ss'-> ' is taken as a "
                  "record start, so a timestamp-shaped line embedded in an exception message is "
                  "indistinguishable from a real call site and is not a certified boundary")
# Exact literals whose call site gives a definite subsystem and state. Anything not
# listed stays unclassified with its full text; nothing here is a physical diagnosis.
PREFIXES = (("UDP Network is connected: ", "UDP_NETWORK_CONNECTED"),
            ("Loopback is Connected: ", "LOOPBACK_CONNECTED"),
            ("Program Started: ", "SESSION_START"),
            ("Program Exit: ", "SESSION_EXIT"),
            ("AgIO Version: ", "VERSION_REPORTED"),
            ("Error in ", "ERROR_REPORT"))
EXACT = {"UDP Network Failed to Connect": "UDP_NETWORK_CONNECT_FAILED",
         "UDP Network Is On": "UDP_NETWORK_ON"}
DATED = ("SESSION_START", "SESSION_EXIT")
TRIM_NOTICE = re.compile(r"^Log trimmed to last ([0-9]{1,9}) lines$")


def text_hex(value):
    return "hex:" + value.encode("utf-8").hex()


def classify(message):
    """Literal match only; a near miss stays unclassified rather than guessed."""
    if message in EXACT:
        return EXACT[message], ""
    for prefix, name in PREFIXES:
        if message.startswith(prefix):
            return name, message[len(prefix):]
    return "UNCLASSIFIED_RETAINED", ""


def convert_agio_events(text):
    """Return the CSV and a report; order is the file's, not a reconstructed time."""
    if len(text.encode("utf-8")) > LIMIT:
        raise ValueError("saved AgIO event input exceeds 16MiB")
    if "\x00" in text:
        raise ValueError("saved AgIO event text must not contain NUL")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(COLUMNS)
    records, blank, leading, trim_candidates, continuations = [], 0, [], 0, 0
    # WriteAllLines rewrites terminators on trim, so all three orderings appear.
    for line in re.split(r"\r\n|\r|\n", text):
        if not line:
            blank += 1
            continue
        match = ANCHOR.match(line)
        if match:
            hour, minute, second = (int(part) for part in match.groups())
            if hour > 23 or minute > 59 or second > 59:
                raise ValueError("invalid reported AgIO clock representation")
            records.append([line[:8], line[match.end():], []])
        elif records:
            # An exception dump embeds newlines; the untimestamped trim notice is
            # indistinguishable from one, so it is counted as a candidate only.
            records[-1][2].append(line)
            continuations += 1
            if TRIM_NOTICE.match(line):
                trim_candidates += 1
        else:
            leading.append(line)
            if TRIM_NOTICE.match(line):
                trim_candidates += 1
    if not records:
        raise ValueError("no anchored AgIO event record")
    classes = {}
    for index, (clock, message, extra) in enumerate(records, 1):
        name, remainder = classify(message)
        classes[name] = classes.get(name, 0) + 1
        joined = "\n".join(extra)
        whole = clock + "-> " + message + ("\n" + joined if joined else "")
        writer.writerow([0, index, name, CLASS_BASIS, clock, CLOCK_BASIS,
                         text_hex(remainder) if name in DATED else "",
                         text_hex(remainder) if remainder and name not in DATED else "",
                         len(extra), text_hex(whole)])
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted AgIO CSV exceeds 128MiB")
    report = {
        "source": "AgOpenGPS-Official/AgOpenGPS@21be26aa58d09b9b5abacc715d0abcf4a021f1db",
        "writer": "AgLibrary.Logging.Log.EventWriter local HH:mm:ss then '-> ' then message then CR",
        "input_bytes": len(text.encode("utf-8")), "records": len(records),
        "blank_lines": blank, "continuation_lines": continuations,
        "unanchored_leading_lines": len(leading),
        # Every leading line is kept in order; the bound is on the whole report, so
        # content is never silently cut to a line count.
        "unanchored_leading": [text_hex(line) for line in leading],
        "upstream_trim_notice_candidates": trim_candidates,
        "event_classes": dict(sorted(classes.items())),
        "clock": "Unknown", "time_basis": "UNLOCATED_ZERO_NOT_EVENT_TIME",
        "date_basis": "SESSION_LINES_ONLY_NO_PER_EVENT_DATE_ZONE_OR_ROLLOVER",
        "record_boundary_basis": BOUNDARY_BASIS, "class_basis": CLASS_BASIS,
        "upstream_loss": "CheckLogSize(maxLines=100) exists but its caller was not located, so trimming is a capability whose activation and completeness are not established; FileSaveSystemEvents does unconditionally clear the buffer when a save fails. Neither loss is reconstructed here",
        "evidence_kind": "program-self-report-not-link-or-device-diagnosis"}
    if len(json.dumps(report).encode("utf-8")) > OUTPUT_LIMIT:
        raise ValueError("AgIO event report exceeds 128MiB")
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved AgIO event text; no device is contacted")
    parser.add_argument("output", type=Path, help="new directory for observations.csv and report.json")
    args = parser.parse_args(argv)
    with args.input.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("saved AgIO event input exceeds 16MiB")
    csv_text, report = convert_agio_events(raw.decode("utf-8"))
    args.output.mkdir()
    (args.output / "observations.csv").write_text(csv_text, encoding="utf-8", newline="")
    (args.output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                             encoding="utf-8", newline="")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
