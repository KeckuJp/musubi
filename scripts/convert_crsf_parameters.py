#!/usr/bin/env python3
"""Saved CRSF parameter frames -> the device's own reported stored configuration.

The input is a capture a caller already has on disk, in the same `time_us,frame_hex` form the adopted
saved-CRSF-frame converter already defines. Nothing is transmitted: this reads 0x29 and 0x2B replies
only, and it never originates a 0x2C read or a 0x2D write.

A reported configuration is what the device says it has stored. It is never an observed hardware state,
never a measurement, never proof that a setting is in effect, and never a requested write.

Producer binding, pinned exactly: the Team Blacksheep CRSF protocol specification,
tbs-fpv/tbs-crsf-spec@001b4058231c1d6f2288458500293a0f9de0a3de, crsf.md
sha256:d9dcb4c8de911ed6e94eb8eae38fbc77abe0839b316b240c3556ce19d66ccb3d - "Frame Details" (big endian,
Frame_length 2..62), "CRC", "Device Addresses", 0x29, "Chunks", 0x2B and its data_type payloads.

This is not a TBS Agent file export and makes no claim that one exists: it is the wire shape a
configuration application exchanges, as that specification defines it.
"""
import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path

try:
    from scripts.convert_crsf_link import crc8
except ModuleNotFoundError:
    from convert_crsf_link import crc8

PIN = "tbs-fpv/tbs-crsf-spec@001b4058231c1d6f2288458500293a0f9de0a3de"
PIN_SHA256 = "sha256:d9dcb4c8de911ed6e94eb8eae38fbc77abe0839b316b240c3556ce19d66ccb3d"
LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
DEVICE_INFORMATION = 0x29
PARAMETER_ENTRY = 0x2B
PARAMETER_READ = 0x2C
PARAMETER_WRITE = 0x2D
MAX_CHUNKS = 16
MAX_ENTRY_BYTES = 896
# "Device Addresses" - the names the pinned document gives. An address it does not list is retained as
# its own number rather than being attached to a neighbouring role.
DEVICE_ROLES = {0x00: "BROADCAST", 0x0E: "CLOUD", 0x10: "USB_DEVICE", 0x12: "BLUETOOTH_OR_WIFI_MODULE",
                0x13: "WIFI_RECEIVER", 0x14: "VIDEO_RECEIVER", 0x80: "OSD_OR_CORE_PNP_PRO",
                0xC0: "VOLTAGE_OR_CURRENT_SENSOR", 0xC2: "GPS", 0xC4: "BLACKBOX",
                0xC8: "FLIGHT_CONTROLLER", 0xCC: "RACE_TAG", 0xCE: "VTX", 0xEA: "REMOTE_CONTROL",
                0xEB: "REPEATER_RECEIVER", 0xEC: "RC_RECEIVER", 0xED: "REPEATER_TRANSMITTER_MODULE",
                0xEE: "RC_TRANSMITTER_MODULE"}
DEVICE_ROLES.update({address: "ESC_%d" % (address - 0x8F) for address in range(0x90, 0x98)})
ROLE_UNLISTED = "ADDRESS_NOT_LISTED_BY_THE_PINNED_SPECIFICATION"
# "Parameter Type Definitions & Hidden Bit": bit 7 is the hidden flag, bits 6-0 are the data_type.
HIDDEN_BIT = 0x80
DATA_TYPES = {0: "UINT8_DEPRECATED", 1: "INT8_DEPRECATED", 2: "UINT16_DEPRECATED",
              3: "INT16_DEPRECATED", 4: "UINT32_DEPRECATED", 5: "INT32_DEPRECATED", 8: "FLOAT",
              9: "TEXT_SELECTION", 10: "STRING", 11: "FOLDER", 12: "INFO", 13: "COMMAND",
              127: "OUT_OF_RANGE"}
FLOAT_TYPE, SELECTION_TYPE, STRING_TYPE, FOLDER_TYPE, INFO_TYPE, COMMAND_TYPE, OUT_OF_RANGE = (
    8, 9, 10, 11, 12, 13, 127)
STORED = "DEVICE_REPORTED_STORED_SETTING"
STORED_TEXT = "DEVICE_REPORTED_STORED_TEXT_RETAINED_AS_HEX"
NOT_A_SETTING = "NOT_A_CONFIGURABLE_SETTING_BY_THE_PINNED_TYPE_DEFINITION"
NO_SUCH_PARAMETER = "DEVICE_REPORTED_PARAMETER_NUMBER_OUT_OF_RANGE_SO_THERE_IS_NO_SETTING_HERE"
TYPE_UNKNOWN = "TYPE_NOT_DEFINED_BY_THE_PINNED_SPECIFICATION_SO_NO_VALUE_IS_READ"
DEPRECATED_TYPE = "TYPE_MARKED_DEPRECATED_BY_THE_PINNED_SPECIFICATION_SO_NO_VALUE_IS_READ"
UNQUALIFIED_START = "UNQUALIFIED_RUN_START_NOT_EVIDENCED_SO_NO_VALUE_IS_READ"
# The shape of what was joined. Neither value says the entry is whole: chunks_remaining 0 marks the END
# of a run, so a lone frame carrying it can equally be the last chunk of a run whose earlier frames were
# never captured - and those trailing bytes can coincidentally parse as a complete typed payload.
SINGLE_FRAME = ("SINGLE_OBSERVED_FRAME_REPORTING_NO_CHUNKS_REMAINING_WHICH_ALONE_DOES_NOT_SHOW_IT_IS_"
                "THE_WHOLE_ENTRY")
JOINED_RUN = "MULTI_FRAME_RUN_JOINED_WHILE_SELF_CONSISTENT_DOWN_TO_NO_CHUNKS_REMAINING"
# How the run's first frame was evidenced to be chunk zero. Nothing inside a run can say this, because a
# run that began at a lost frame is self-consistent down to zero exactly like a whole one.
START_OBSERVED = ("CORRELATED_0x2C_CHUNK_ZERO_READ_REQUEST_EVIDENCE_NOT_PROOF_THAT_NO_FRAME_WAS_LOST_"
                  "BETWEEN_IT_AND_THE_REPLY")
START_DECLARED = ("CALLER_DECLARED_PRECONDITION_THAT_EVERY_RUN_IN_THIS_FILE_STARTS_AT_CHUNK_ZERO_NOT_"
                  "OBSERVED_IN_BAND")
START_ABSENT = "NO_CORRELATED_CHUNK_ZERO_READ_REQUEST_AND_NO_CALLER_DECLARATION"
IDENTITY_REPORTED = "DEVICE_REPORTED_ITS_OWN_IDENTITY_IN_AN_0x29_FRAME_FROM_THIS_ORIGIN_NOT_AUTHENTICATED"
IDENTITY_ABSENT = "DEVICE_INFORMATION_NOT_REPORTED_BY_THIS_ORIGIN_IN_THIS_CAPTURE"
REQUESTED_WRITE = "REQUESTED_WRITE_NOT_A_REPORTED_CONFIGURATION"
REQUESTED_READ = "REQUESTED_READ_NOT_A_REPORTED_CONFIGURATION"
# The common reader refuses quoted CSV input, so this sentence carries no comma and no quote.
CONFIGURATION_BASIS = ("the devices own reported stored setting as the pinned parameter frame carries it;"
                       " never an observed hardware state; never a measurement; never proof that the"
                       " setting is in effect or honoured; never a requested write")
FIELDS = ["record_time_us", "crsf_origin_address", "crsf_origin_role", "crsf_destination_address",
          "crsf_device_name_hex", "crsf_device_serial_reported", "crsf_device_hardware_id_reported",
          "crsf_device_firmware_id_reported", "crsf_device_parameters_total_reported",
          "crsf_device_parameter_version_reported", "crsf_device_identity_basis",
          "crsf_parameter_number", "crsf_parameter_parent_folder", "crsf_parameter_name_hex",
          "crsf_parameter_type_code", "crsf_parameter_type_name", "crsf_parameter_hidden_reported",
          "crsf_configured_value_reported", "crsf_configured_value_label_hex", "crsf_value_min_reported",
          "crsf_value_max_reported", "crsf_value_default_reported", "crsf_value_decimal_point_reported",
          "crsf_value_step_reported", "crsf_string_max_length_reported", "crsf_value_unit_hex",
          "crsf_value_disposition", "crsf_entry_start_basis", "crsf_chain_disposition",
          "crsf_chunk_count", "crsf_payload_consumed_bytes", "crsf_payload_remainder_bytes",
          "crsf_payload_remainder_hex", "crsf_entry_sha256", "crsf_configuration_basis"]


def role(address):
    return DEVICE_ROLES.get(address, ROLE_UNLISTED)


def read_string(payload, position):
    """One null-terminated string of the pinned payloads; an unterminated one refuses the entry."""
    end = payload.find(b"\x00", position)
    if end < 0:
        raise ValueError("a reported string is not null-terminated inside the frame payload")
    return payload[position:end], end + 1


def read_int(payload, position, width, signed=False):
    """Big endian, as the pinned Frame Details section states for the whole protocol."""
    if position + width > len(payload):
        raise ValueError("the payload is shorter than the mandatory fields of its declared type")
    return int.from_bytes(payload[position:position + width], "big", signed=signed), position + width


def device_information(payload):
    """0x29 Parameter Device Information: reported provenance, never an authenticated identity."""
    name, position = read_string(payload, 0)
    serial, position = read_int(payload, position, 4)
    hardware, position = read_int(payload, position, 4)
    firmware, position = read_int(payload, position, 4)
    total, position = read_int(payload, position, 1)
    # The specification says a shortened frame may simply omit trailing optional fields, so an absent
    # parameter version is absence and never a zero.
    version = payload[position] if position < len(payload) else ""
    return dict(crsf_device_name_hex="hex:" + name.hex(), crsf_device_serial_reported=serial,
                crsf_device_hardware_id_reported=hardware, crsf_device_firmware_id_reported=firmware,
                crsf_device_parameters_total_reported=total,
                crsf_device_parameter_version_reported=version,
                crsf_device_identity_basis=IDENTITY_REPORTED)


def parameter_entry(payload, row):
    """One reassembled 0x2B entry, read exactly as the pinned payload of its declared data_type.

    Every byte of the payload is accounted for: what the declared type consumed, and what is left over.
    The specification allows a newer sender to append fields after the known ones, so a remainder is
    retained and counted rather than ignored - and never read as a value of an invented meaning.
    """
    if len(payload) < 2:
        raise ValueError("the payload is shorter than the mandatory fields of its declared type")
    declared = payload[1]
    kind = declared & ~HIDDEN_BIT
    row.update(crsf_parameter_parent_folder=payload[0], crsf_parameter_type_code=kind,
               crsf_parameter_type_name=DATA_TYPES.get(kind, "UNKNOWN_TYPE_RETAINED_AS_REPORTED"),
               crsf_parameter_hidden_reported=int(bool(declared & HIDDEN_BIT)))
    if kind not in DATA_TYPES:
        row["crsf_value_disposition"] = TYPE_UNKNOWN
        return account(payload, 2, row)
    if kind == OUT_OF_RANGE:
        row["crsf_value_disposition"] = NO_SUCH_PARAMETER
        return account(payload, 2, row)
    if kind < FLOAT_TYPE:
        # The specification marks these "Suggested for deprecation ... currently implemented via FLOAT"
        # and gives them no payload of their own, so no value is invented for them.
        row["crsf_value_disposition"] = DEPRECATED_TYPE
        return account(payload, 2, row)
    name, position = read_string(payload, 2)
    row["crsf_parameter_name_hex"] = "hex:" + name.hex()
    if kind == FLOAT_TYPE:
        value, position = read_int(payload, position, 4, signed=True)
        low, position = read_int(payload, position, 4, signed=True)
        high, position = read_int(payload, position, 4, signed=True)
        default, position = read_int(payload, position, 4, signed=True)
        decimal, position = read_int(payload, position, 1)
        # Step_size is "the recommended increment or decrement value to modify the value": a reported
        # number of the producer's own, carried as written and given no unit here.
        step, position = read_int(payload, position, 4, signed=True)
        row.update(crsf_configured_value_reported=value, crsf_value_min_reported=low,
                   crsf_value_max_reported=high, crsf_value_default_reported=default,
                   crsf_value_decimal_point_reported=decimal, crsf_value_step_reported=step,
                   crsf_value_disposition=STORED)
    elif kind == SELECTION_TYPE:
        options, position = read_string(payload, position)
        value, position = read_int(payload, position, 1)
        low, position = read_int(payload, position, 1)
        high, position = read_int(payload, position, 1)
        default, position = read_int(payload, position, 1)
        # "a 0 represents the first text": the label comes only from this frame's own list, never from
        # an external table, and an index the list does not reach is left unlabelled rather than guessed.
        choices = options.split(b";") if options else []
        row.update(crsf_configured_value_reported=value, crsf_value_min_reported=low,
                   crsf_value_max_reported=high, crsf_value_default_reported=default,
                   crsf_value_disposition=STORED,
                   crsf_configured_value_label_hex=("hex:" + choices[value].hex()
                                                    if value < len(choices) else
                                                    "OPTION_INDEX_BEYOND_THE_REPORTED_LIST"))
    elif kind == STRING_TYPE:
        value, position = read_string(payload, position)
        row.update(crsf_configured_value_reported="hex:" + value.hex(),
                   crsf_value_disposition=STORED_TEXT)
        # String_max_length is a length the device declares for its own field. It is kept in its own
        # column rather than folded into the numeric maximum of the other types.
        if position < len(payload):
            row["crsf_string_max_length_reported"] = payload[position]
            position += 1
        else:
            row["crsf_string_max_length_reported"] = "ABSENT"
    elif kind == INFO_TYPE:
        value, position = read_string(payload, position)
        row.update(crsf_configured_value_reported="hex:" + value.hex(),
                   crsf_value_disposition=NOT_A_SETTING)
    else:
        # FOLDER lists its children and COMMAND runs a function; neither is a stored setting, and this
        # converter never executes anything.
        row["crsf_value_disposition"] = NOT_A_SETTING
        return account(payload, position, row)
    if kind in (FLOAT_TYPE, SELECTION_TYPE):
        if position < len(payload):
            unit, position = read_string(payload, position)
            row["crsf_value_unit_hex"] = "hex:" + unit.hex()
        else:
            row["crsf_value_unit_hex"] = "ABSENT"
    account(payload, position, row)


def account(payload, position, row):
    """Say exactly how many payload bytes the declared type consumed and what is left over."""
    remainder = payload[position:]
    row.update(crsf_payload_consumed_bytes=position, crsf_payload_remainder_bytes=len(remainder),
               crsf_payload_remainder_hex="hex:" + remainder.hex() if remainder else "NONE")


def convert(text, caller_declares_complete_entries=False):
    """A saved capture to one row per completed parameter entry; nothing is transmitted.

    An entry only carries a value when its run's start was evidenced to be chunk zero - either by a
    captured 0x2C read request for chunk 0 whose addresses and parameter number correlate (read, never
    sent), or by the caller declaring that every run in this file starts at chunk zero. Without either,
    the entry is still accounted, but no value is read from it.

    The frame count never substitutes for that. chunks_remaining 0 marks the END of a run, so a lone
    frame carrying it is equally the last chunk of a run whose earlier frames were never captured, and
    those trailing bytes can coincidentally parse as a complete typed payload. A correlated read is
    evidence of where a run was requested to begin; it is not proof that no frame was lost between the
    request and the reply, and a later-chunk read for the same pair withdraws it.
    """
    if type(caller_declares_complete_entries) is not bool:
        raise ValueError("the complete-entry declaration is an explicit boolean precondition")
    if len(text.encode("utf-8")) > LIMIT:
        raise ValueError("input exceeds bound")
    reader = csv.DictReader(io.StringIO(text), strict=True)
    names = reader.fieldnames
    if not names or len(names) != len(set(names)) or names[:2] != ["time_us", "frame_hex"]:
        raise ValueError("expected time_us,frame_hex followed by optional retained columns")
    if any(not name or len(name) > 128 for name in names) or len(names) > 64:
        raise ValueError("invalid capture columns")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    report = dict(source_records=0, parameter_entries=0, unqualified_entries=0,
                  device_information_records=0, requested_frames=[], unsupported_records=[],
                  incomplete_chunk_runs=0, observed_chunk_zero_reads=0,
                  invalidated_chunk_zero_reads=0,
                  entry_start_declaration=START_DECLARED if caller_declares_complete_entries else
                  "NOT_DECLARED_BY_THE_CALLER",
                  clock="Unknown", specification_pin=PIN,
                  specification_sha256=PIN_SHA256, configuration_basis=CONFIGURATION_BASIS,
                  scope=("saved frames only; no frame is transmitted and no device is contacted;"
                         " this is a reusable CRSF parameter-protocol capability and not coverage of"
                         " any configuration application: no vendor application writer is identified"
                         " here and no application file export is claimed to exist"))
    identities, pending, reads, previous = {}, {}, {}, None
    for line, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError("capture row width mismatch")
        if not re.fullmatch(r"[0-9]{1,19}", row["time_us"]):
            raise ValueError("invalid capture microseconds")
        time = int(row["time_us"])
        if time >= 2**63 or (previous is not None and time < previous):
            raise ValueError("capture time out of range or decreasing")
        previous = time
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){4,64}", row["frame_hex"]):
            raise ValueError("expected bounded complete CRSF frame hex")
        frame = bytes.fromhex(row["frame_hex"])
        if frame[1] != len(frame) - 2 or not 2 <= frame[1] <= 62:
            raise ValueError("CRSF frame length outside the pinned 2..62 range or inconsistent")
        if crc8(frame[2:-1]) != frame[-1]:
            raise ValueError("CRSF frame CRC mismatch")
        report["source_records"] += 1
        kind = frame[2]
        if kind in (PARAMETER_READ, PARAMETER_WRITE):
            # A read asks for a value and a write asks to change one. Neither is something the device
            # reported about itself, so neither ever becomes a configuration row. A captured read is
            # still evidence of where a reply run began - it is read here and never sent.
            report["requested_frames"].append(dict(
                line=line, frame_type=kind,
                disposition=REQUESTED_WRITE if kind == PARAMETER_WRITE else REQUESTED_READ))
            if kind == PARAMETER_READ and len(frame) >= 8:
                asked, at_chunk = frame[5], frame[6]
                if at_chunk == 0:
                    # Correlated by both addresses: the device this read was sent to must be the origin
                    # of the reply, and the host that sent it must be the reply's destination.
                    reads[(frame[3], frame[4])] = asked
                    report["observed_chunk_zero_reads"] += 1
                elif reads.pop((frame[3], frame[4]), None) is not None:
                    # The host asked that same device for a later chunk before any reply run began, so
                    # the next reply it sends is not chunk zero. The earlier authorization no longer
                    # evidences a start and is withdrawn rather than left to qualify the wrong run.
                    report["invalidated_chunk_zero_reads"] += 1
            continue
        if kind not in (DEVICE_INFORMATION, PARAMETER_ENTRY):
            report["unsupported_records"].append(dict(line=line, frame_type=kind))
            continue
        if len(frame) < 6:
            raise ValueError("an extended header frame needs a destination and an origin address")
        destination, origin, payload = frame[3], frame[4], frame[5:-1]
        if kind == DEVICE_INFORMATION:
            identities[origin] = device_information(payload)
            report["device_information_records"] += 1
            continue
        if len(payload) < 2:
            raise ValueError("a parameter entry frame needs a number and a chunks-remaining count")
        number, remaining, chunk = payload[0], payload[1], payload[2:]
        held = pending.get(origin)
        if held is None:
            # The start basis is decided once, from what the capture showed before this first frame.
            # A read is consumed so it can never qualify a second run.
            observed = reads.pop((origin, destination), None) == number
            held = dict(number=number, remaining=remaining, data=bytearray(), chunks=0, time=time,
                        first=remaining, destination=destination, observed_start=observed)
            pending[origin] = held
        elif held["destination"] != destination:
            # Same origin, different destination: two runs interleaved on one capture. Joining them
            # would splice one device's reply to another host into this entry's bytes.
            raise ValueError("the reported chunk run breaks: the destination address changed mid-run")
        elif held["number"] != number or remaining != held["remaining"] - 1:
            # A run that skips, restarts or interleaves is not resolved by guessing which bytes belong
            # together: a wrongly joined payload would read as a value.
            raise ValueError("the reported chunk run breaks: wrong parameter number or chunk count")
        else:
            held["remaining"] = remaining
        held["data"] += chunk
        held["chunks"] += 1
        if held["chunks"] > MAX_CHUNKS or len(held["data"]) > MAX_ENTRY_BYTES:
            raise ValueError("the reported parameter entry exceeds the selected chunk bound")
        if remaining:
            continue
        del pending[origin]
        entry = bytes(held["data"])
        # The frame count never qualifies an entry. One observed frame reporting no chunks remaining is
        # the end of some run, and that run's earlier frames may simply never have been captured.
        chain = SINGLE_FRAME if held["chunks"] == 1 else JOINED_RUN
        if held["observed_start"]:
            start = START_OBSERVED
        elif caller_declares_complete_entries:
            start = START_DECLARED
        else:
            start = START_ABSENT
        line_row = dict.fromkeys(FIELDS, "")
        line_row.update(record_time_us=held["time"], crsf_origin_address=origin,
                        crsf_origin_role=role(origin), crsf_destination_address=destination,
                        crsf_parameter_number=number, crsf_chunk_count=held["chunks"],
                        crsf_entry_sha256="sha256:" + hashlib.sha256(entry).hexdigest(),
                        crsf_entry_start_basis=start, crsf_chain_disposition=chain,
                        crsf_configuration_basis=CONFIGURATION_BASIS,
                        crsf_device_identity_basis=IDENTITY_ABSENT)
        line_row.update(identities.get(origin, {}))
        if start == START_ABSENT:
            # Accounted, never valued: the run is real and its bytes are bound by the digest, but the
            # payload is not parsed at all, because its first byte is not proved to be the entry's.
            line_row.update(crsf_value_disposition=UNQUALIFIED_START,
                            crsf_payload_consumed_bytes=0,
                            crsf_payload_remainder_bytes=len(entry),
                            crsf_payload_remainder_hex="hex:" + entry.hex() if entry else "NONE")
            report["unqualified_entries"] += 1
        else:
            parameter_entry(entry, line_row)
            report["parameter_entries"] += 1
        writer.writerow(line_row)
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("expanded output exceeds bound")
    # A capture may stop at any moment, so an unfinished run is counted and emits no row: a partial
    # payload is never read as a value.
    report["incomplete_chunk_runs"] = len(pending)
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--caller-declares-complete-entries", action="store_true",
                        help="the caller asserts every chunk run in this file starts at chunk zero;"
                             " a precondition the operator states, never observed in band")
    args = parser.parse_args()
    with args.input.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("input exceeds bound")
    output, report = convert(raw.decode("utf-8"), args.caller_declares_complete_entries)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
