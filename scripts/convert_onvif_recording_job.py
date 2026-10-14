"""Finite saved ONVIF recording-job reports; never query or control a recorder."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

MAX_XML_BYTES = 1024 * 1024
MAX_ELEMENTS = 10000
MAX_SOURCES = 256
MAX_TRACKS = 4096
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
TT = "{http://www.onvif.org/ver10/schema}"
TRC = "{http://www.onvif.org/ver10/recording/wsdl}"
SOAP = "{http://www.w3.org/2003/05/soap-envelope}"
NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
FIELDS = ["record_time_us", "recording_row_kind", "recording_source_index", "recording_track_index",
          "recording_job_token_hex", "recording_token_hex", "recording_source_token_hex",
          "recording_source_type_hex", "recording_source_tag_hex", "recording_destination_hex",
          "recording_state_hex", "recording_state_disposition", "recording_track_error_reported",
          "recording_error_presence", "recording_error_hex", "source_response_sha256"]


def bounded_text(value, limit=4096, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode()) > limit:
        raise ValueError("selected scalar text bound or missing value")
    return value


def hex_text(value):
    return "hex:" + value.encode().hex()


def convert(text, job_token, capture_time_us):
    if (not isinstance(text, str) or not text or len(text.encode()) > MAX_XML_BYTES
            or "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I)):
        raise ValueError("XML bound or prohibited declaration")
    bounded_text(job_token)
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit bounded capture clock required")
    try:
        document = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("malformed saved XML") from error
    nodes = list(document.iter())
    if len(nodes) > MAX_ELEMENTS:
        raise ValueError("XML element bound")
    selected, selected_attributes = {document}, set()

    def many(parent, namespace, name):
        tag = namespace + name
        if any(child.tag.rsplit("}", 1)[-1] == name and child.tag != tag for child in parent):
            raise ValueError("selected element in wrong namespace")
        found = parent.findall(tag)
        if any(NIL in node.attrib for node in found):
            raise ValueError("nil selected element")
        selected.update(found)
        return found

    def one(parent, namespace, name, required=True):
        found = many(parent, namespace, name)
        if len(found) > 1 or (required and not found):
            raise ValueError("missing or duplicated selected element")
        return found[0] if found else None

    def scalar(parent, name, limit=4096, required=True, empty=False):
        node = one(parent, TT, name, required)
        if node is None:
            return None
        if len(node):
            raise ValueError("nested selected scalar")
        return bounded_text(node.text or "", limit, empty)

    root = document
    if root.tag == SOAP + "Envelope":
        body = one(root, SOAP, "Body")
        if len(body) != 1:
            raise ValueError("one saved SOAP response required")
        root = body[0]
        selected.add(root)
    if root.tag != TRC + "GetRecordingJobStateResponse" or NIL in root.attrib:
        raise ValueError("not a saved recording-job response")
    status = one(root, TRC, "State")
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    base = dict(record_time_us=capture_time_us, recording_job_token_hex=hex_text(job_token),
                recording_token_hex=hex_text(scalar(status, "RecordingToken")), source_response_sha256=digest)
    rows = []

    def row(parent, context, kind):
        state = scalar(parent, "State", 128)
        result = dict(context, recording_row_kind=kind, recording_state_hex=hex_text(state),
                      recording_state_disposition="AGGREGATE_REPORTED_UNINTERPRETED",
                      recording_error_presence="NOT_APPLICABLE")
        if kind == "TRACK":
            known = state in ("Idle", "Active", "Error")
            result["recording_state_disposition"] = "SCHEMA_KNOWN_REPORTED" if known else "UNKNOWN_STATE_RETAINED"
            result["recording_track_error_reported"] = int(state == "Error") if known else ""
            error = scalar(parent, "Error", required=False, empty=True)
            result["recording_error_presence"] = "NOT_REPORTED" if error is None else "EMPTY" if error == "" else "REPORTED"
            if error is not None:
                result["recording_error_hex"] = hex_text(error)
        rows.append(result)

    row(status, base, "JOB")
    sources = many(status, TT, "Sources")
    if len(sources) > MAX_SOURCES:
        raise ValueError("source count bound")
    track_count = 0
    for source_index, source in enumerate(sources):
        token = one(source, TT, "SourceToken")
        context = dict(base, recording_source_index=source_index,
                       recording_source_token_hex=hex_text(scalar(token, "Token")))
        if "Type" in token.attrib:
            context["recording_source_type_hex"] = hex_text(bounded_text(token.attrib["Type"], empty=True))
            selected_attributes.add((token, "Type"))
        row(source, context, "SOURCE")
        tracks = many(one(source, TT, "Tracks"), TT, "Track")
        track_count += len(tracks)
        if track_count > MAX_TRACKS:
            raise ValueError("track count bound")
        for track_index, track in enumerate(tracks):
            row(track, dict(context, recording_track_index=track_index,
                            recording_source_tag_hex=hex_text(scalar(track, "SourceTag")),
                            recording_destination_hex=hex_text(scalar(track, "Destination"))), "TRACK")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    for item in rows:
        writer.writerow(item)
        if output.tell() > MAX_CSV_BYTES:  # All output values are ASCII (source text is hex).
            raise ValueError("CSV output bound")
    report = dict(source_response=text, source_response_sha256=digest, observations=len(rows),
                  job_rows=1, source_rows=len(sources), track_rows=track_count,
                  unknown_track_states=sum(r["recording_state_disposition"] == "UNKNOWN_STATE_RETAINED" for r in rows),
                  reported_error_fields=sum(r["recording_error_presence"] in ("EMPTY", "REPORTED") for r in rows),
                  unselected_elements=sum(node not in selected for node in nodes),
                  unselected_attributes=sum((node, key) not in selected_attributes for node in nodes for key in node.attrib),
                  clock="Unknown", source_types_schema="25.12", response_wsdl_snapshot="26.06",
                  basis="REPORTED_JOB_AND_TRACK_CONDITIONS_NOT_AUTHENTICATED_DEVICE_OR_RECORDING_SUCCESS")
    if len(json.dumps(report, indent=2).encode()) > MAX_REPORT_BYTES:
        raise ValueError("report output bound")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("input", "output", "report"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--job-token", required=True)
    parser.add_argument("--capture-time-us", required=True, type=int)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError("output artifacts already exist or coincide")
        with args.input.open("rb") as stream:
            data = stream.read(MAX_XML_BYTES + 1)
        if len(data) > MAX_XML_BYTES:
            raise ValueError("XML input bound")
        text, report = convert(data.decode("utf-8"), args.job_token, args.capture_time_us)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(2, f"conversion refused: {error}\n")


if __name__ == "__main__":
    main()
