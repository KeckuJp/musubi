#!/usr/bin/env python3
"""Saved AgOpenGPS steering-module reported state (PGN 253) to common Observation CSV.

Passive and offline: a capture already on disk is read, nothing is sent, no device or
network is touched, and no steering command is produced or carried. The packet contains
no commanded or target angle at all.

Wire layout, scaling, sentinels and meanings are fixed by two refs that were read:
`AgOpenGPS-Official/Boards` 60e8d894625c1a0dfd0bfeecd8247dc224cb69a8
`ArduinoModules/UDP/Autosteer_UDP_v5/Autosteer_UDP_v5.ino` (the writer) and
`AgOpenGPS-Official/AgOpenGPS` 21be26aa58d09b9b5abacc715d0abcf4a021f1db
`SourceCode/GPS/Forms/UDPComm.Designer.cs` `case 253` (the paired decoder). Both apply the
same scale, so degrees come from the source rather than from a field name.

The same sketch has a second IMU branch (CMPS14) that is indistinguishable on the wire and
whose register units it does not state, so the caller must declare `--imu-source`. Only the
qualified BNO08x declaration emits heading/roll degrees; the transmitted int16 is retained
either way, and the declaration travels with every row and with the report.

Datagram framing, the capture clock and the IPv4/UDP checks are reused unchanged from the
adopted `convert_imc_sensors.iter_udp_datagrams`; no second PCAP parser exists here and no
capture timestamp is invented.
"""
import argparse
import csv
import hashlib
import io
import json
import struct
import sys
from pathlib import Path

try:  # The adopted dual-import idiom: usable as a script and as a package module.
    from scripts.convert_imc_sensors import iter_udp_datagrams
except ImportError:
    from convert_imc_sensors import iter_udp_datagrams

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
# The unsupported sample is bounded by bytes, not by a record ceiling: every unsupported
# frame is always counted, and each retained entry carries the exact digest of its whole
# payload, so a bounded sample never turns into a dropped record.
MAX_UNSUPPORTED_REPORT = 64 * 1024
UNSUPPORTED_PREFIX_BYTES = 64
FRAME_BYTES = 14
HEADER = b"\x80\x81"
SOURCE_ID = 126
PGN = 253
DECLARED_LENGTH = 8
# The no-IMU branch writes these exact int16 values; the client compares before scaling.
HEADING_UNAVAILABLE = 9999
ROLL_UNAVAILABLE = 8888
# The wire cannot say which IMU branch the build used, so the caller declares it and the
# declaration travels with every row. Nothing is inferred from the payload.
IMU_BNO = "bno08x-degrees"
IMU_NONE = "none-declared"
IMU_UNQUALIFIED = "unqualified"
IMU_SOURCES = (IMU_BNO, IMU_NONE, IMU_UNQUALIFIED)
IMU_BASES = {
    IMU_BNO: "BNO08X_BRANCH_DECLARED_DEGREES_FROM_THE_PINNED_WRITER_TIMES_TEN",
    IMU_NONE: "NO_IMU_BUILD_DECLARED_ONLY_THE_DOCUMENTED_SENTINELS_ARE_ACCEPTED",
    IMU_UNQUALIFIED: "IMU_SOURCE_UNQUALIFIED_RAW_INT16_RETAINED_NO_DEGREE_MEANING_ESTABLISHED"}
ANGLE_BASIS = ("MEASURED_WAS_ANGLE_SCALED_BY_MODULE_CONFIGURATION_OFFSET_COUNTS_AND_ACKERMAN"
               "_NOT_A_COMMAND_OR_CALIBRATED_ABSOLUTE")
SIGN_BASIS = "SIGN_AS_TRANSMITTED_UNDER_THE_MODULE_INVERTWAS_WIRING_NOT_A_GLOBAL_CONVENTION"
SWITCH_BASIS = "BITS_AS_TRANSMITTED_ACTIVE_SENSE_NOT_DECLARED_BY_THE_WRITER_NOT_ENGAGED_OR_SAFE"
PWM_BASIS = "DRIVE_SIDE_DISPLAY_BYTE_NEVER_ASSIGNED_IN_THE_PINNED_SKETCH_NOT_A_MEASURED_STATE"
FIELDS = ["record_time_us", "source_packet_index", "source_frame_hex", "source_frame_sha256",
          "agopen_pgn_code", "agopen_source_id_code", "agopen_declared_payload_length",
          "agopen_steer_angle_deg", "agopen_steer_angle_basis", "agopen_steer_angle_sign_basis",
          "agopen_imu_source_declared",
          "agopen_heading_deg", "agopen_heading_status", "agopen_heading_raw_int16",
          "agopen_roll_deg", "agopen_roll_status", "agopen_roll_raw_int16",
          "agopen_imu_scale_basis", "agopen_switch_byte_code", "agopen_work_bit",
          "agopen_steer_bit", "agopen_remote_bit", "agopen_switch_sense_basis",
          "agopen_pwm_display_code", "agopen_pwm_display_basis",
          "agopen_reported_source_address", "agopen_reported_source_port"]
FAILURE = "agopen steering conversion failed; verify capture, port and a new output path"
USAGE = ("usage: convert_agopen_steering.py CAPTURE.pcap NEW_OUTPUT_DIRECTORY"
         " --udp-destination-port PORT --imu-source bno08x-degrees|none-declared|unqualified")


def checksum(frame):
    """The writer's own sum: bytes 2..12 inclusive, truncated to 8 bits."""
    return sum(frame[2:FRAME_BYTES - 1]) & 0xFF


def signed16(frame, index):
    """Little-endian int16, exactly as the writer stores low byte then high byte."""
    return struct.unpack_from("<h", frame, index)[0]


def declares_selected_pgn(payload):
    """Recognition only: header, source and PGN. Length and checksum are validation.

    A frame that declares this packet but fails validation is corruption in the selected
    path, not a different message, so it must refuse rather than be filed as unsupported.
    """
    return (len(payload) >= 4 and payload[:2] == HEADER
            and payload[2] == SOURCE_ID and payload[3] == PGN)


def decode_frame(frame, imu_source):
    if imu_source not in IMU_SOURCES:
        raise ValueError("explicit declared IMU source required")
    if len(frame) != FRAME_BYTES:
        raise ValueError("selected AgOpen steering frame length mismatch")
    if frame[4] != DECLARED_LENGTH:
        raise ValueError("selected AgOpen steering frame declared length mismatch")
    if checksum(frame) != frame[FRAME_BYTES - 1]:
        raise ValueError("selected AgOpen steering frame checksum mismatch")
    heading, roll = signed16(frame, 7), signed16(frame, 9)
    switches = frame[11]
    row = {"agopen_pgn_code": frame[3], "agopen_source_id_code": frame[2],
           "agopen_declared_payload_length": frame[4],
           # int16 x100 at the writer, x0.01 at the client: degrees, sign untouched.
           "agopen_steer_angle_deg": signed16(frame, 5) / 100,
           "agopen_steer_angle_basis": ANGLE_BASIS,
           "agopen_steer_angle_sign_basis": SIGN_BASIS,
           "agopen_imu_source_declared": imu_source,
           "agopen_imu_scale_basis": IMU_BASES[imu_source],
           "agopen_switch_byte_code": switches,
           # Declarations, not a state: the writer packs bit0 work, bit1 steer, bit2 remote.
           "agopen_work_bit": switches & 1,
           "agopen_steer_bit": (switches >> 1) & 1,
           "agopen_remote_bit": (switches >> 2) & 1,
           "agopen_switch_sense_basis": SWITCH_BASIS,
           "agopen_pwm_display_code": frame[12], "agopen_pwm_display_basis": PWM_BASIS}
    for name, value, absent in (("heading", heading, HEADING_UNAVAILABLE),
                                ("roll", roll, ROLL_UNAVAILABLE)):
        # The exact transmitted int16 is always retained, whatever the declaration says.
        row["agopen_" + name + "_raw_int16"] = value
        if imu_source == IMU_NONE and value != absent:
            # A declared no-IMU build must carry the documented sentinel; a real value here
            # contradicts the declaration, and guessing which one is right is not our call.
            raise ValueError("declared no-IMU build reported a value instead of its sentinel")
        if value == absent:
            # Compared before scaling, so a sentinel never becomes 99.99 or 888.8 degrees.
            row["agopen_" + name + "_status"] = "UNAVAILABLE_NO_IMU_SENTINEL_" + str(absent)
            continue
        if imu_source != IMU_BNO:
            # CMPS14 or unknown: the bytes are kept as a reference, with no degree meaning.
            row["agopen_" + name + "_status"] = "UNQUALIFIED_IMU_SOURCE_NO_DEGREES"
            continue
        row["agopen_" + name + "_status"] = "REPORTED"
        row["agopen_" + name + "_deg"] = value / 10
    return row


def convert(data, destination_port, imu_source=None):
    """Return the CSV plus an accounting report. Nothing selected is silently dropped.

    `imu_source` is the caller's declaration of which IMU branch the build used. The wire
    cannot say, so it is required and is never inferred from the payload.
    """
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_INPUT:
        raise ValueError("saved capture input limit")
    if imu_source not in IMU_SOURCES:
        raise ValueError("explicit declared IMU source required")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    report = {"source_datagrams": 0, "selected_datagrams": 0, "decoded_frames": 0,
              "unselected_datagrams": 0, "unsupported_frames_total": 0,
              "unsupported_frames_retained": 0, "unsupported_frames": [],
              "unsupported_retention": "every unsupported frame is counted; each retained entry"
                                       " carries the exact digest of its whole payload and a"
                                       " labelled prefix, and the sample is bounded by bytes",
              "udp_destination_port": destination_port,
              "declared_imu_source": imu_source,
              "declared_imu_basis": IMU_BASES[imu_source],
              "imu_source_basis": "caller declaration; the wire does not identify the IMU branch",
              "clock": "Unknown",
              "clock_basis": "PCAP capture microseconds of the recording host; not a trusted UTC anchor",
              "source_sha256": "sha256:" + hashlib.sha256(bytes(data)).hexdigest(),
              "writer_source": "AgOpenGPS-Official/Boards 60e8d894625c1a0dfd0bfeecd8247dc224cb69a8 Autosteer_UDP_v5.ino",
              "client_source": "AgOpenGPS-Official/AgOpenGPS 21be26aa58d09b9b5abacc715d0abcf4a021f1db UDPComm.Designer.cs case 253",
              "evidence_kind": "record-conversion-not-physical-or-autosteer-certification"}
    retained_bytes = 0
    for datagram in iter_udp_datagrams(bytes(data), destination_port):
        report["source_datagrams"] += 1
        if datagram.payload is None:
            report["unselected_datagrams"] += 1
            continue
        report["selected_datagrams"] += 1
        if not declares_selected_pgn(datagram.payload):
            # Another message on the same port: counted always, and referenced exactly by
            # digest even when only a labelled prefix of its bytes fits the sample budget.
            payload = bytes(datagram.payload)
            report["unsupported_frames_total"] += 1
            entry = {"packet_index": datagram.index, "bytes": len(payload),
                     "frame_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                     "frame_prefix_hex": payload[:UNSUPPORTED_PREFIX_BYTES].hex(),
                     "prefix_bytes": min(len(payload), UNSUPPORTED_PREFIX_BYTES),
                     "disposition": "NOT_THE_SELECTED_PGN_253_FRAME_RETAINED"}
            if retained_bytes + len(json.dumps(entry)) <= MAX_UNSUPPORTED_REPORT:
                retained_bytes += len(json.dumps(entry))
                report["unsupported_frames"].append(entry)
                report["unsupported_frames_retained"] += 1
            continue
        frame = bytes(datagram.payload)
        row = decode_frame(frame, imu_source)
        row.update(record_time_us=datagram.capture_time_us, source_packet_index=datagram.index,
                   source_frame_hex="hex:" + frame.hex(),
                   source_frame_sha256="sha256:" + hashlib.sha256(frame).hexdigest(),
                   agopen_reported_source_address=datagram.source_address,
                   agopen_reported_source_port=datagram.source_port)
        writer.writerow(row)
        report["decoded_frames"] += 1
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted CSV exceeds bound")
    if not report["decoded_frames"]:
        raise ValueError("no selected AgOpen steering frame in the capture")
    report["unsupported_sample_truncated"] = (
        report["unsupported_frames_retained"] != report["unsupported_frames_total"])
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved capture already on disk; no device is contacted")
    parser.add_argument("output_directory", type=Path, help="new directory for observations.csv and report.json")
    parser.add_argument("--udp-destination-port", type=int, required=True,
                        help="exact destination port of the module's reported state; never guessed")
    parser.add_argument("--imu-source", required=True, choices=IMU_SOURCES,
                        help="declared IMU branch of the build; the wire cannot say, and only "
                             "the qualified BNO08x declaration may emit heading/roll degrees")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as stream:
            data = stream.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT:
            raise ValueError("saved capture input limit")
        text, report = convert(data, args.udp_destination_port, args.imu_source)
        args.output_directory.mkdir()  # Refuse an existing output, including the input path.
        with (args.output_directory / "observations.csv").open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        (args.output_directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, ImportError) as error:
        # Fixed text: a diagnostic carrying capture bytes or a path would copy source material
        # into logs the operator did not choose to expose.
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
