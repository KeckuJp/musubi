#!/usr/bin/env python3
"""Saved RTSP/1.0 DESCRIBE response -> the declared RTP clock of one selected binding; never a client.

One explicitly selected media section and payload type inside an **already saved** successful
`RTSP/1.0 200 OK` response whose body is `application/sdp` yield the declared encoding name and clock
rate from that section's `a=rtpmap` (RFC 4566 §6), and that exact declared rate and payload type are
handed to the adopted offline RTP conversion path so the common rows carry the declared binding, the
source reference and the session/media scope.

No socket is opened, no request is sent, no RTSP session, SETUP, PLAY or TEARDOWN exists here, nothing
is authenticated or decrypted, no device is contacted and **no URL is ever fetched**: URL-bearing values
(`a=control`, `Content-Base`, `Content-Location`) may carry credentials or query secrets, so they are
displayed only as a fixed redaction marker plus a digest, offset and length that point back into the
retained private original.

This module owns no packet parser and no clock arithmetic. It calls `convert_rtp_records.convert_pcap_rtp`,
which keeps the single implementation of the saved-capture framing (PCAP / Ethernet / IPv4 / UDP), the
RFC 3550 header parse and all sequence/timestamp extension. A declared clock is a declaration: it is not
evidence that the paired capture's packets used that payload type or that rate, the pairing itself is a
caller declaration, and a DESCRIBE response establishes no transport, session id, SSRC, UTC anchor or
authenticity (RFC 2326 §10.2/§10.4; wall-clock correspondence lives in an RTCP Sender Report, RFC 3550
§6.4.1, which is not part of DESCRIBE).

Contract, accepted envelope and refusal policy:
docs/saved-inputs.md.
"""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat

try:  # same import shape the existing converters use for each other
    from scripts.convert_imc_sensors import OUTPUT_LIMIT
    from scripts.convert_rtp_records import (DEFAULT_MAX_FORWARD_SECONDS, MAX_CLOCK_RATE,
                                             convert_pcap_rtp, read_bounded)
except ModuleNotFoundError:
    from convert_imc_sensors import OUTPUT_LIMIT
    from convert_rtp_records import (DEFAULT_MAX_FORWARD_SECONDS, MAX_CLOCK_RATE, convert_pcap_rtp,
                                    read_bounded)

SCHEMA = "musubi.rtsp-saved-description-binding/v1"
DESCRIPTION_LIMIT = 1024 * 1024
RTSP_VERSION = "RTSP/1.0"
SUCCESS_STATUS = "200"
SDP_CONTENT_TYPE = "application/sdp"
SUPPORTED_PROTO = "RTP/AVP"
SDP_VERSION_LINE = "v=0"

# RFC 4566 §5: the complete set of type letters. Anything else refuses the description rather than
# leaving a line silently unread (the fail-closed form of "MUST completely ignore" the description).
SDP_LETTERS = set("vosiuepcbtrzkam")
MEDIA_LETTERS = set("micbka")
MAX_TOKEN = 128
MAX_FORMATS = 64
MAX_SDP_LINES = 4096

# Bases, written as strings into the rows so an engineer reads them with the numbers.
PAIRING_BASIS = "PAIRED_BY_CALLER_DECLARATION_NOT_ESTABLISHED_BY_THE_DESCRIPTION"
CLOCK_BASIS = "SDP_RTPMAP_DECLARED_IN_THE_SAVED_DESCRIPTION_NOT_OBSERVED_IN_PACKETS"
CONTROL_REDACTED = "REDACTED_URL_SEE_RETAINED_PRIVATE_SOURCE"
CONTROL_AGGREGATE = "ASTERISK_INHERITS_BASE_NOT_RESOLVED_HERE"
CONTROL_ABSENT = "NOT_DECLARED"
PORT_SAME = "SAME_AS_DECLARED"
PORT_ZERO = "DECLARED_ZERO_NO_PORT_DECLARED_BY_THE_DESCRIPTION"
PORT_DIFFERENT = "CALLER_SELECTED_PORT_DIFFERS_FROM_THE_DECLARED_MEDIA_PORT"
# The retained originals are the private copies the redacted references point at: owner-only, exclusive
# creation, fixed names.
DIRECTORY_MODE = 0o700
OUTPUT_MODE = 0o600
RETAINED_OUTPUTS = ("observations.csv", "accounting.json", "source.rtsp", "source.pcap")
RETENTION_BASIS = ("UNSELECTED_AND_UNKNOWN_LINES_ARE_LISTED_BY_TYPE_OFFSET_LENGTH_AND_DIGEST_AND_THE_"
                   "EXACT_BYTES_STAY_IN_THE_RETAINED_ORIGINAL_NOT_REPRINTED_HERE")


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _token(value, what):
    """Bounded retained token; a description is refused rather than truncated into a shorter claim."""
    if not value or len(value) > MAX_TOKEN:
        raise ValueError(f"{what} is empty or exceeds the retained bound")
    return value


def split_envelope(data):
    """RFC 2326 §4.4/§7.1 response envelope -> (status line parts, headers, body, body offset).

    Bytes in, strict decode, no newline translation: the digest binds the file that was saved.
    """
    if len(data) > DESCRIPTION_LIMIT:
        raise ValueError("saved RTSP response exceeds the bounded input size")
    boundary = data.find(b"\r\n\r\n")
    if boundary < 0:
        raise ValueError("saved RTSP response has no CRLF CRLF header boundary")
    try:
        head = data[:boundary].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("saved RTSP response header is not valid UTF-8") from error
    body_offset = boundary + 4
    lines, offset = [], 0
    for line in head.split("\r\n"):
        if "\n" in line or "\r" in line:
            raise ValueError("saved RTSP response uses a bare LF or CR line ending")
        lines.append((offset, line))
        offset += len(line.encode("utf-8")) + 2

    status = lines[0][1].split(" ")
    if len(status) < 2:
        raise ValueError("malformed RTSP status line")
    if status[0] != RTSP_VERSION:
        raise ValueError("unsupported RTSP response version")
    if not (len(status[1]) == 3 and status[1].isdigit()):
        raise ValueError("malformed RTSP status line")
    if status[1] != SUCCESS_STATUS:
        raise ValueError("RTSP response status is not 200 OK")

    headers, seen = [], {}
    for line_offset, line in lines[1:]:
        if line.startswith((" ", "\t")):
            raise ValueError("continued RTSP header lines are not admitted")
        name, separator, value = line.partition(":")
        if not separator or not name or name.strip() != name or " " in name:
            raise ValueError("malformed RTSP header line")
        entry = dict(name=name, offset=line_offset, length=len(line.encode("utf-8")),
                     sha256=_digest(line.encode("utf-8")))
        headers.append(entry)
        seen.setdefault(name.lower(), []).append(value.strip())
    return status, headers, seen, data[body_offset:], body_offset


def _envelope_body(seen, body):
    """Content-Type / Content-Length are the two headers this envelope is allowed to depend on."""
    for name in ("content-type", "content-length"):
        if len(seen.get(name, [])) > 1:
            raise ValueError("duplicate RTSP envelope header")
    content_type = seen.get("content-type", [None])[0]
    if content_type is None:
        raise ValueError("RTSP response declares no Content-Type")
    if ";" in content_type:
        raise ValueError("RTSP response Content-Type carries an unsupported parameter")
    if content_type.lower() != SDP_CONTENT_TYPE:
        raise ValueError("RTSP response Content-Type is not application/sdp")
    declared = seen.get("content-length", [None])[0]
    if declared is None:
        raise ValueError("RTSP response declares no Content-Length")
    if not declared.isdigit():
        raise ValueError("malformed RTSP Content-Length")
    if int(declared) != len(body):
        # Short body (truncated save) and trailing bytes are the same refusal: the body is not
        # determinable, so nothing is read from it.
        raise ValueError("declared Content-Length does not match the saved description body")
    return content_type.lower(), int(declared)


def parse_sdp(body, body_offset):
    """RFC 4566 §5 lines -> (session fields, media sections, per-line classification input)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("session description is not valid UTF-8") from error
    if not text.endswith("\r\n"):
        raise ValueError("session description line is not CRLF terminated")
    raw = text.split("\r\n")[:-1]
    if len(raw) > MAX_SDP_LINES:
        raise ValueError("session description exceeds the retained line bound")

    lines, offset = [], body_offset
    for line in raw:
        if "\n" in line or "\r" in line:
            raise ValueError("session description uses a bare LF or CR line ending")
        if len(line) < 2 or line[1] != "=":
            raise ValueError("malformed session description line")
        letter, value = line[0], line[2:]
        if letter not in SDP_LETTERS:
            raise ValueError("unknown SDP type letter")
        encoded = line.encode("utf-8")
        lines.append(dict(letter=letter, value=value, offset=offset, length=len(encoded),
                          sha256=_digest(encoded)))
        offset += len(encoded) + 2

    if len(lines) < 4 or lines[0]["letter"] != "v" or lines[1]["letter"] != "o" \
            or lines[2]["letter"] != "s":
        raise ValueError("malformed session description")
    if f"v={lines[0]['value']}" != SDP_VERSION_LINE:
        raise ValueError("unsupported session description version")
    origin = lines[1]["value"].split(" ")
    if len(origin) != 6:
        raise ValueError("malformed session description origin")

    sections, session_controls, seen_time = [], [], False
    for index, line in enumerate(lines[3:], start=3):
        letter = line["letter"]
        if letter == "m":
            sections.append(dict(index=len(sections), line=line, lines=[line], rtpmaps=[],
                                 controls=[]))
            continue
        if sections:
            if letter not in MEDIA_LETTERS:
                # A session-level line after the first media section: the RFC fixes the order, so this
                # is malformed rather than something to re-attach by guesswork.
                raise ValueError("malformed session description")
            sections[-1]["lines"].append(line)
            if letter == "a":
                if line["value"].startswith("rtpmap:"):
                    sections[-1]["rtpmaps"].append(line)
                elif line["value"].startswith("control:"):
                    sections[-1]["controls"].append(line)
            continue
        if letter == "t":
            seen_time = True
        if letter == "a":
            if line["value"].startswith("rtpmap:"):
                # RFC 4566 §6 defines rtpmap as media-level only; a misplaced one has no defined
                # meaning, so it is refused instead of attached to a section it does not belong to.
                raise ValueError("rtpmap outside a media section")
            if line["value"].startswith("control:"):
                session_controls.append(line)
    if not seen_time:
        raise ValueError("malformed session description")

    for section in sections:
        fields = section["line"]["value"].split(" ")
        if len(fields) < 4:
            raise ValueError("malformed media description")
        media, port_field, proto = fields[0], fields[1], fields[2]
        formats = fields[3:]
        port, _, port_count = port_field.partition("/")
        if not port.isdigit() or (port_count and not port_count.isdigit()):
            raise ValueError("malformed media description port")
        if len(formats) > MAX_FORMATS:
            raise ValueError("media description exceeds the retained format bound")
        section.update(media=_token(media, "media type"), proto=_token(proto, "media protocol"),
                       port=int(port), port_count=int(port_count) if port_count else 1,
                       formats=[_token(value, "media format") for value in formats])
    return dict(session_id=_token(origin[1], "session id"),
                session_version=_token(origin[2], "session version"),
                session_controls=session_controls), sections, lines


def parse_rtpmap(line):
    """RFC 4566 §6 `a=rtpmap:<pt> <encoding name>/<clock rate>[/<encoding parameters>]`."""
    payload, separator, mapping = line["value"][len("rtpmap:"):].partition(" ")
    if not separator or not payload.isdigit() or not 0 <= int(payload) <= 127:
        raise ValueError("malformed rtpmap declaration")
    parts = mapping.split("/")
    if len(parts) not in (2, 3):
        raise ValueError("malformed rtpmap declaration")
    encoding, rate = _token(parts[0], "rtpmap encoding name"), parts[1]
    if not rate.isdigit():
        raise ValueError("declared rtpmap clock rate must be a positive bounded integer")
    clock_rate = int(rate)
    if not 0 < clock_rate <= MAX_CLOCK_RATE:
        # Zero is the one RFC 4566 leaves undefined; it is refused fail-closed rather than used as a
        # divisor, and nothing here invents a rate from the packets.
        raise ValueError("declared rtpmap clock rate must be a positive bounded integer")
    parameters = _token(parts[2], "rtpmap encoding parameters") if len(parts) == 3 else ""
    return dict(payload_type=int(payload), encoding_name=encoding, clock_rate_hz=clock_rate,
                encoding_parameters=parameters, offset=line["offset"], sha256=line["sha256"])


def _control_reference(line):
    """A control value may be a URL carrying credentials or a query: never echoed, never fetched."""
    if line is None:
        return CONTROL_ABSENT, ""
    value = line["value"][len("control:"):]
    display = CONTROL_AGGREGATE if value == "*" else CONTROL_REDACTED
    return display, f"{line['sha256']}@{line['offset']}+{line['length']}"


def select_binding(description, *, media_index, payload_type):
    """Saved description bytes + an explicit selection -> the declared binding and the accounting."""
    if type(media_index) is not int or media_index < 0:
        raise ValueError("explicit zero-based media index required")
    if type(payload_type) is not int or not 0 <= payload_type <= 127:
        raise ValueError("explicit RTP payload type must be 0..127")
    status, headers, seen, body, body_offset = split_envelope(description)
    content_type, content_length = _envelope_body(seen, body)
    session, sections, lines = parse_sdp(body, body_offset)

    if media_index >= len(sections):
        raise ValueError("selected media section is absent")
    selected = sections[media_index]
    if selected["proto"] != SUPPORTED_PROTO:
        # RTP/SAVP and every other proto: the fmt meaning is protocol specific and nothing here
        # decrypts, so the binding is refused as unsupported instead of read as RTP/AVP.
        raise ValueError("unsupported media transport protocol")
    if str(payload_type) not in selected["formats"]:
        raise ValueError("selected payload type is not in the media format list")
    declared = [parse_rtpmap(line) for line in selected["rtpmaps"]]
    matching = [entry for entry in declared if entry["payload_type"] == payload_type]
    if not matching:
        # A static payload type without an rtpmap declares nothing in SDP; the RFC 3551 static table
        # stays the adopted converter's own selector rather than a silent fallback here.
        raise ValueError("no rtpmap declares the clock for the selected payload type")
    if len(matching) > 1:
        # RFC 4566 §6 admits at most one rtpmap per format and defines no winner.
        raise ValueError("ambiguous rtpmap declarations for the selected payload type")
    binding = matching[0]
    if len(selected["controls"]) > 1 or len(session["session_controls"]) > 1:
        # The emitted media scope must be one value; a repeated control declares two scopes and no
        # RFC rule picks one.
        raise ValueError("ambiguous control scope declarations")
    media_control_line = selected["controls"][0] if selected["controls"] else None
    session_control_line = session["session_controls"][0] if session["session_controls"] else None

    consumed = {id(lines[0]), id(lines[1]), id(lines[2]), id(selected["line"])}
    consumed.update(id(line) for line in selected["rtpmaps"] if line["offset"] == binding["offset"])
    consumed.update(id(line) for line in lines if line["letter"] == "t")
    consumed.update(id(line) for line in (session_control_line, media_control_line)
                    if line is not None)
    retained = [dict(sdp_type=line["letter"], offset=line["offset"], length=line["length"],
                     sha256=line["sha256"],
                     attribute=line["value"].split(":", 1)[0] if line["letter"] == "a" else "")
                for line in lines if id(line) not in consumed]

    media_control, media_control_ref = _control_reference(media_control_line)
    session_control, session_control_ref = _control_reference(session_control_line)
    report = dict(
        schema=SCHEMA, source_sha256=_digest(description), source_bytes=len(description),
        status_code=int(status[1]), rtsp_version=RTSP_VERSION, content_type=content_type,
        content_length=content_length, body_offset=body_offset, headers=headers,
        sdp_lines=len(lines), consumed_lines=len(consumed), retained_unselected_lines=len(retained),
        retained_lines=retained, retention_basis=RETENTION_BASIS,
        media_sections=len(sections), selected_media_index=media_index,
        unselected_media_sections=[
            dict(index=section["index"], media=section["media"], proto=section["proto"],
                 port_declared=section["port"], formats=list(section["formats"]),
                 lines=len(section["lines"]))
            for section in sections if section["index"] != media_index],
        declared_binding=dict(binding), session_id=session["session_id"],
        session_version=session["session_version"], media_control=media_control,
        media_control_reference=media_control_ref, session_control=session_control,
        session_control_reference=session_control_ref,
        clock_rate_declaration_basis=CLOCK_BASIS, pairing_basis=PAIRING_BASIS)
    if report["sdp_lines"] != report["consumed_lines"] + report["retained_unselected_lines"]:
        raise ValueError("session description line accounting does not add up")
    return dict(selected=selected, session=session, binding=binding), report


def _declared_columns(report, binding_report, destination_port):
    selected = binding_report["selected"]
    binding = binding_report["binding"]
    if selected["port"] == 0:
        agreement = PORT_ZERO
    elif selected["port"] == destination_port:
        agreement = PORT_SAME
    else:
        agreement = PORT_DIFFERENT
    return {
        "rtsp_source_sha256": report["source_sha256"],
        "rtsp_status_code": report["status_code"],
        "rtsp_version": report["rtsp_version"],
        "rtsp_pairing_basis": PAIRING_BASIS,
        "sdp_session_id": report["session_id"],
        "sdp_session_version": report["session_version"],
        "sdp_media_index": report["selected_media_index"],
        "sdp_media_type": selected["media"],
        "sdp_media_proto": selected["proto"],
        "sdp_media_port_declared": selected["port"],
        "sdp_media_port_agreement": agreement,
        "sdp_media_format_list": "|".join(selected["formats"]),
        "sdp_selected_payload_type": binding["payload_type"],
        "sdp_encoding_name": binding["encoding_name"],
        "sdp_declared_clock_rate_hz": binding["clock_rate_hz"],
        "sdp_encoding_parameters": binding["encoding_parameters"],
        "sdp_rtpmap_source_offset": binding["offset"],
        "sdp_rtpmap_line_sha256": binding["sha256"],
        "sdp_media_control": report["media_control"],
        "sdp_media_control_reference": report["media_control_reference"],
        "sdp_session_control": report["session_control"],
        "rtp_clock_declaration_basis": CLOCK_BASIS,
    }


def _append_columns(rows, declared):
    """The adopted rows, unchanged, plus the declared binding on every row. No row is rewritten."""
    reader = csv.reader(io.StringIO(rows, newline=""))
    header = next(reader)
    collision = sorted(set(header) & set(declared))
    if collision:
        raise ValueError(f"declared column would shadow an RTP column: {collision[0]}")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header + list(declared))
    values = [declared[name] for name in declared]
    for row in reader:
        writer.writerow(row + values)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("description-bound CSV exceeds bound")
    return output.getvalue()


def convert_saved_binding(description, capture, destination_port, *, media_index, payload_type,
                          pairing_declared_by_caller=False, ssrc=None,
                          max_forward_seconds=DEFAULT_MAX_FORWARD_SECONDS, packet_range=None):
    """Saved description + saved capture -> (common CSV rows, JSON-ready report)."""
    if pairing_declared_by_caller is not True:
        # Nothing in a description proves which capture it belongs to: the caller has to say so, and
        # the declaration travels into every row rather than being assumed here.
        raise ValueError("the caller must declare the pairing of this description with this capture")
    binding_report, report = select_binding(description, media_index=media_index,
                                            payload_type=payload_type)
    # `rfc3551_static=True` does not choose the rate here - the declared one always wins as the rate
    # this conversion runs on - it turns on the adopted `_selected_clock_rate` disagreement check. An
    # `RTP/AVP` binding on a payload type that RFC 3551 §6 tables 4/5 already fix a rate for is refused
    # when the description declares a contradicting one, instead of converting ticks on a rate the
    # profile cannot have used. A dynamic type (96-127, or anything outside that table) has no static
    # entry, so that path is untouched and the declared rate is still the only source.
    rows, rtp_report = convert_pcap_rtp(
        capture, destination_port, clock_rate=binding_report["binding"]["clock_rate_hz"],
        payload_type=payload_type, rfc3551_static=True, ssrc=ssrc,
        max_forward_seconds=max_forward_seconds, packet_range=packet_range)
    declared = _declared_columns(report, binding_report, destination_port)
    converted = _append_columns(rows, declared)
    report = dict(report, declared_columns=declared, rtp=rtp_report,
                  selected_udp_destination_port=destination_port,
                  time_basis=rtp_report["time_basis"],
                  clock_pairing=("the declared rate drove the adopted RTP conversion because the caller "
                                 "paired these two files; the packets themselves prove neither the "
                                 "payload type mapping nor the rate"))
    return converted, report


def _private_directory(path):
    """Create the output directory owner-only. An existing path is a failure, never an overwrite."""
    path.mkdir(mode=DIRECTORY_MODE)  # no parents, no exist_ok
    path.chmod(DIRECTORY_MODE)  # a permissive umask cannot widen what was asked for
    if stat.S_IMODE(path.stat().st_mode) != DIRECTORY_MODE:
        raise ValueError("retained output directory is not owner-only")
    return path


def _write_private(directory, name, payload):
    """One fixed output, created exclusively at 0600 and written completely.

    The retained originals hold whatever the saved description and capture hold, credentials included,
    so they are never created world- or group-readable and never opened on an existing path. The name
    comes from this module's fixed set: no caller-supplied path component reaches here. This is a
    private complete write, not a crash-atomic one - no rename dance and no claim of durability.
    """
    if name not in RETAINED_OUTPUTS:
        raise ValueError("output name is not one of the fixed retained outputs")
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, OUTPUT_MODE)
    with open(descriptor, "wb", closefd=True) as stream:
        written = stream.write(payload)
    if written != len(payload):
        raise ValueError("retained output was not written completely")
    path.chmod(OUTPUT_MODE)
    if stat.S_IMODE(path.stat().st_mode) != OUTPUT_MODE or path.stat().st_size != len(payload):
        raise ValueError("retained output is not owner-only and complete")


def main():
    parser = argparse.ArgumentParser(
        description="Bind one selected media/payload declaration in a saved RTSP/1.0 DESCRIBE response "
                    "to a saved RTP capture through the adopted offline conversion path.")
    parser.add_argument("description", type=Path, help="saved RTSP/1.0 DESCRIBE response bytes")
    parser.add_argument("capture", type=Path, help="saved classic PCAP capture")
    parser.add_argument("new_output_directory", type=Path,
                        help="directory to create; existing inputs and evidence are never overwritten")
    parser.add_argument("--udp-destination-port", type=int, required=True)
    parser.add_argument("--media-index", type=int, required=True,
                        help="zero-based index of the selected m= section")
    parser.add_argument("--payload-type", type=int, required=True,
                        help="selected RTP payload type (0..127), which must be in that m= fmt list")
    parser.add_argument("--pairing-declared-by-caller", action="store_true",
                        help="required: the description is paired with this capture by your declaration, "
                             "not by any evidence in either file")
    parser.add_argument("--ssrc", type=lambda v: int(v, 0), help="select exactly one SSRC")
    parser.add_argument("--max-forward-seconds", type=int, default=DEFAULT_MAX_FORWARD_SECONDS)
    parser.add_argument("--packet-range", help="declared segment FIRST:LAST of capture packet indices")
    args = parser.parse_args()
    try:
        description = read_bounded(args.description, DESCRIPTION_LIMIT)
        capture = read_bounded(args.capture)
        segment = None
        if args.packet_range is not None:
            first, _, last = args.packet_range.partition(":")
            segment = (int(first), int(last))
        converted, report = convert_saved_binding(
            description, capture, args.udp_destination_port, media_index=args.media_index,
            payload_type=args.payload_type,
            pairing_declared_by_caller=args.pairing_declared_by_caller, ssrc=args.ssrc,
            max_forward_seconds=args.max_forward_seconds, packet_range=segment)
        directory = _private_directory(args.new_output_directory)  # existing evidence is never touched
        _write_private(directory, "observations.csv", converted.encode("utf-8"))
        _write_private(directory, "accounting.json",
                       (json.dumps(report, indent=2) + "\n").encode("utf-8"))
        # The exact private originals: the redacted display above points here, and nothing else does.
        _write_private(directory, "source.rtsp", description)
        _write_private(directory, "source.pcap", capture)
    except (ValueError, OSError, ImportError):
        parser.exit(2, "saved RTSP description binding failed; check the envelope, the selected "
                       "media/payload declaration and the declared pairing\n")


if __name__ == "__main__":
    main()
