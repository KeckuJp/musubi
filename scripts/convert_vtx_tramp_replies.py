#!/usr/bin/env python3
"""Saved Tramp replies from a video transmitter -> reported configuration, as the device said it.

Offline and passive: a byte stream already on disk is read. **No command is generated, nothing
is transmitted, no serial port is opened, no device is contacted and no radio is keyed.** The
only thing here is a reader for bytes a VTX already produced and somebody already saved.

Every frame rule, offset, field and acceptance condition below is taken from Betaflight's own
receiver, pinned at commit `77d01ba3b76a22909d5f09cb0628820141f95eaa` (4.5.1),
`src/main/io/vtx_tramp.c` (22091 bytes, sha256
`26350cc21dee0786077276ce0be9c8344e8a5446d43d5c21fea1b14c2c049bf8`), with
`src/main/io/vtx_tramp.h` and `src/main/drivers/vtx_common.h` for the units those files state.
That source defines the protocol **operationally, in code**: it is a flight controller's
implementation, not a vendor specification, and no vendor document was acquired.

What the pinned receiver accepts, and therefore what this path accepts: a 16 byte frame whose
first byte is `0x0F`, whose second byte is one of `r`, `v` or `s`, whose byte 14 equals the 8
bit sum of bytes 1..13, and whose byte 15 is zero. Anything else is counted, never decoded.

Meanings that the source settles, and the ones it does not:

* **Frequency is MHz** - `vtx_tramp.h` states `VTX_TRAMP_MIN_FREQUENCY_MHZ 5000` and
  `VTX_TRAMP_MAX_FREQUENCY_MHZ 5999`, "min freq in MHz".
* **Power is the same quantity as the milliwatt table this firmware matches it against** -
  `vtx_common.h` annotates the Tramp table `"---", 25, 100, 200, 400, 600 mW`, `trampPowerTable`
  holds exactly those values, and `vtxTrampGetPowerIndex` looks the reply's configured power up
  in it. The wire itself declares no unit, and that provenance is what the row states.
* **Temperature has NO unit in the pinned source** - no degree, no scale, nothing - and its sign
  is not even settled there: the decode casts `(int16_t)`, the stored value is `int16_t`, and
  the accessor is `uint16_t vtxTrampGetCurrentTemp()`. So the raw word travels with both
  readings and an explicit unresolved unit, rather than acquiring a degree nobody wrote.
* **Zero is a query sentinel, not a measurement.** `trampHandleResponse` rejects `r` with a zero
  minimum frequency, `v` with a zero frequency and `s` with a zero temperature as "reading the
  request". A genuine zero is therefore indistinguishable from an echo in this protocol, and
  that is reported rather than resolved.
* **Configuration is not verified radio output.** A reported power is what the device says it is
  set to, or says it is producing; it is never measured radiated power, never EIRP, never a
  compliance statement and never output quality. No fault, interference or diagnosis is derived.
* **Readiness is not in a reply.** `vtxTrampIsReady` reads the flight controller's own state
  machine, so a saved reply carries configuration and reported state, and this path reports
  which reply kinds arrived - never a device health verdict.

Contract and limits: docs/saved-inputs.md.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

REFERENCE = "betaflight/betaflight@77d01ba3b76a22909d5f09cb0628820141f95eaa (4.5.1) io/vtx_tramp.c"
HEADER_BYTE = 0x0F
FRAME_LENGTH = 16
# `trampChecksum`: `for (int i = 1 ; i < 14 ; i++) cksum += trampBuf[i];`
CHECKSUM_FIRST, CHECKSUM_LAST = 1, 13
CHECKSUM_OFFSET, TRAILER_OFFSET = 14, 15
# Only these three codes advance the pinned receiver out of S_WAIT_CODE.
LIMITS_REPLY, STATUS_REPLY, TEMPERATURE_REPLY = ord("r"), ord("v"), ord("s")
ACCEPTED_CODES = {LIMITS_REPLY: "DEVICE_LIMITS_REPLY", STATUS_REPLY: "STATUS_REPLY",
                  TEMPERATURE_REPLY: "TEMPERATURE_REPLY"}
# `TRAMP_CONTROL_RACE_LOCK (0x01)`; the source says the byte is "Currently only used for race
# lock", so every other bit is retained and not interpreted.
RACE_LOCK_BIT = 0x01
# `vtx_tramp.h`: "min freq in MHz" / max, and `vtx_common.h` states the same for the setting.
DECLARED_MIN_MHZ, DECLARED_MAX_MHZ = 5000, 5999
MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_ROWS = 100_000

CAPTURE_BASIS = ("the_common_capture_clock_is_a_caller_declaration_because_a_Tramp_reply_"
                 "carries_no_time_of_its_own_at_all")
DEVICE_BASIS = ("THE_DEVICE_IS_WHATEVER_THE_CALLER_DECLARED_THIS_SAVED_STREAM_CAME_FROM_THE_"
                "PROTOCOL_CARRIES_NO_SERIAL_NUMBER_NO_MODEL_AND_NO_IDENTITY_SO_NONE_IS_"
                "INVENTED_HERE")
SOURCE_BASIS = ("BYTES_A_VIDEO_TRANSMITTER_PRODUCED_AND_SOMEBODY_SAVED_READ_UNDER_THE_PINNED_"
                "FLIGHT_CONTROLLER_RECEIVERS_OWN_ACCEPTANCE_RULES_WHICH_ARE_AN_IMPLEMENTATION_"
                "AND_NOT_A_VENDOR_SPECIFICATION")
POWER_BASIS = ("THE_SAME_QUANTITY_AS_THE_MILLIWATT_TABLE_THE_PINNED_FIRMWARE_MATCHES_THIS_"
               "FIELD_AGAINST_THE_WIRE_DECLARES_NO_UNIT_ITSELF_AND_A_REPORTED_POWER_IS_A_"
               "CONFIGURATION_THE_DEVICE_STATES_NEVER_MEASURED_RADIATED_POWER_NEVER_EIRP_"
               "NEVER_OUTPUT_QUALITY_AND_NEVER_A_COMPLIANCE_STATEMENT")
TEMPERATURE_BASIS = ("UNIT_UNRESOLVED_THE_PINNED_SOURCE_STATES_NO_UNIT_FOR_THIS_FIELD_AND_DOES_"
                     "NOT_SETTLE_ITS_SIGN_EITHER_THE_DECODE_CASTS_SIGNED_WHILE_THE_ACCESSOR_"
                     "RETURNS_UNSIGNED_SO_THE_RAW_WORD_AND_BOTH_READINGS_ARE_CARRIED_AND_NO_"
                     "DEGREE_IS_ASSERTED")
PIT_BASIS = ("THE_PIT_MODE_FLAG_THE_DEVICE_REPORTS_ABOUT_ITSELF_WHICH_IS_A_STATED_MODE_AND_"
             "NEVER_EVIDENCE_THAT_RADIO_FREQUENCY_OUTPUT_WAS_ACTUALLY_SUPPRESSED")
READINESS_BASIS = ("WHICH_REPLY_KIND_ARRIVED_NOTHING_MORE_READINESS_IN_THE_PINNED_FIRMWARE_IS_"
                   "ITS_OWN_STATE_MACHINE_NOT_A_FIELD_IN_ANY_REPLY_SO_NO_DEVICE_HEALTH_VERDICT_"
                   "NO_FAULT_AND_NO_INTERFERENCE_DIAGNOSIS_IS_REACHED_HERE")
RANGE_BASIS = ("WHETHER_THE_REPORTED_FREQUENCY_FALLS_INSIDE_THE_5000_TO_5999_MHZ_RANGE_THE_"
               "PINNED_SOURCE_DECLARES_FOR_ITS_OWN_SETTING_REPORTED_AND_NEVER_ENFORCED_BECAUSE_"
               "A_DEVICE_MAY_REPORT_OUTSIDE_ITS_CONTROLLERS_DECLARED_RANGE")
RECORD_BASIS = ("every_row_is_an_OBSERVED_RECORD_read_from_saved_bytes_it_is_never_a_command_"
                "never_a_transmission_never_a_device_contact_and_never_a_measurement_made_here")

COLUMNS = ("record_time_us", "capture_clock_basis", "source_sha256", "source_device_declared",
           "device_identity_basis", "frame_offset", "frame_hex", "vtx_reply_kind",
           "vtx_reply_code", "reply_readiness_basis", "frequency_mhz_reported",
           "frequency_range_disposition", "frequency_range_basis",
           "frequency_min_mhz_reported", "frequency_max_mhz_reported",
           "power_max_reported", "configured_power_reported", "actual_power_reported",
           "power_basis", "control_mode_code", "race_lock_declared", "pit_mode_code",
           "pit_mode_reported", "pit_mode_basis", "temperature_word_reported",
           "temperature_signed_reading", "temperature_unsigned_reading", "temperature_basis",
           "source_basis", "record_basis")


def checksum(frame):
    """The pinned receiver's checksum: the 8 bit sum of bytes 1..13 of the 16 byte frame."""
    total = 0
    for index in range(CHECKSUM_FIRST, CHECKSUM_LAST + 1):
        total = (total + frame[index]) & 0xFF
    return total


def word(frame, offset):
    """One little-endian 16 bit field, exactly as the pinned decode reads it."""
    return frame[offset] | (frame[offset + 1] << 8)


def frames(raw, report):
    """Every accepted frame, in saved order, with every byte the walk consumed accounted for.

    This follows the pinned receiver's **byte consumption**, not just its acceptance test. That
    receiver reads a stream one byte at a time and never pushes a byte back, so what it rejects
    it has already eaten:

    * a byte that is not `0x0F` is consumed in `S_WAIT_LEN` and discarded - **1 byte**;
    * `0x0F` followed by a code that is not `r`, `v` or `s` consumes **2 bytes**, and the bad
      code byte is gone even when it is itself `0x0F`, because it was read in `S_WAIT_CODE`;
    * a complete frame consumes **all 16 bytes** before any test: `trampResetReceiver()` is
      called at `trampReceivePos == 16`, *before* the checksum and trailer comparison, so a
      frame that fails either one is consumed whole and reading continues after it;
    * a frame whose response handler returns zero - the "reading the request" echo - is likewise
      already consumed, and the loop simply keeps reading.

    Advancing one byte from a rejected frame instead would let a `0x0F` sitting inside that
    frame's payload start a new candidate, so a **rejected frame could manufacture an accepted
    reading** that the pinned receiver would never produce. It cannot here: a rejected frame's
    payload is never re-examined.

    Nothing is skipped silently - each event is counted under its own reason, and the bytes
    consumed are reconciled with the input length by the caller.
    """
    offset, accepted = 0, []
    while offset < len(raw):
        if raw[offset] != HEADER_BYTE:
            report["bytes_before_a_header_byte"] += 1
            offset += 1
            continue
        if offset + 1 >= len(raw):
            # A header byte with no code byte after it: the receiver would simply wait for more.
            report["truncated_trailing_bytes"] += len(raw) - offset
            break
        code = raw[offset + 1]
        if code not in ACCEPTED_CODES:
            # Header and code are both consumed; the code byte is never reconsidered.
            key = str(code)
            report["unaccepted_code_events"][key] = report["unaccepted_code_events"].get(key, 0) + 1
            offset += 2
            continue
        if offset + FRAME_LENGTH > len(raw):
            # The stream stops inside a frame: the bytes that are there are counted, and no
            # field is read out of bytes the recording does not have.
            report["truncated_trailing_bytes"] += len(raw) - offset
            break
        frame = raw[offset:offset + FRAME_LENGTH]
        # From here the sixteen bytes are consumed whatever the outcome, exactly as the pinned
        # receiver resets its buffer before judging the frame.
        offset += FRAME_LENGTH
        if frame[CHECKSUM_OFFSET] != checksum(frame):
            report["checksum_mismatch_frames"] += 1
            continue
        if frame[TRAILER_OFFSET] != 0:
            report["non_zero_trailer_frames"] += 1
            continue
        # `trampHandleResponse` rejects a zero leading value as "reading the request". That is
        # the firmware's own rule, so an echo is counted here and never carried as a reading.
        sentinel = {LIMITS_REPLY: word(frame, 2), STATUS_REPLY: word(frame, 2),
                    TEMPERATURE_REPLY: word(frame, 6)}[code]
        if sentinel == 0:
            name = ACCEPTED_CODES[code]
            report["query_sentinel_echoes"][name] = report["query_sentinel_echoes"].get(name, 0) + 1
            continue
        accepted.append((offset - FRAME_LENGTH, frame))
    return accepted


def convert(raw, capture_time_us, device_declared=None):
    """One row per accepted reply, in saved order; never a command and never a measurement."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit caller capture microseconds required")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized saved Tramp input")
    if device_declared is not None and (type(device_declared) is not str or not device_declared):
        raise ValueError("a declared device is a non-empty caller statement or nothing at all")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    report = dict(reference=REFERENCE, source_sha256=digest, source_bytes=len(raw),
                  accepted_frames=0, accepted_by_kind={}, bytes_before_a_header_byte=0,
                  truncated_trailing_bytes=0,
                  # Events and the bytes they consumed are reported apart, because they are not
                  # the same number: a rejected code costs two bytes and a rejected frame costs
                  # sixteen, which is what the pinned receiver eats before it judges them.
                  unaccepted_code_events={}, checksum_mismatch_frames=0,
                  non_zero_trailer_frames=0, query_sentinel_echoes={},
                  bytes_consumed_by_kind={}, device_declared=device_declared,
                  time_basis="CALLER_DECLARED_CAPTURE_MICROSECONDS_THE_PROTOCOL_CARRIES_NO_TIME")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    fixed = {"record_time_us": capture_time_us, "capture_clock_basis": CAPTURE_BASIS,
             "source_sha256": digest,
             "source_device_declared": ("hex:" + device_declared.encode("utf-8").hex()
                                        if device_declared else None),
             "device_identity_basis": DEVICE_BASIS, "source_basis": SOURCE_BASIS,
             "reply_readiness_basis": READINESS_BASIS, "record_basis": RECORD_BASIS}
    for offset, frame in frames(raw, report):
        kind = ACCEPTED_CODES[frame[1]]
        row = dict(fixed, frame_offset=offset, frame_hex="hex:" + frame.hex(),
                   vtx_reply_kind=kind, vtx_reply_code=chr(frame[1]))
        if frame[1] == LIMITS_REPLY:
            row.update(frequency_min_mhz_reported=word(frame, 2),
                       frequency_max_mhz_reported=word(frame, 4),
                       power_max_reported=word(frame, 6), power_basis=POWER_BASIS)
        elif frame[1] == STATUS_REPLY:
            frequency, control = word(frame, 2), frame[6]
            row.update(frequency_mhz_reported=frequency,
                       frequency_range_disposition=(
                           "REPORTED_INSIDE_THE_DECLARED_SETTING_RANGE"
                           if DECLARED_MIN_MHZ <= frequency <= DECLARED_MAX_MHZ
                           else "REPORTED_OUTSIDE_THE_DECLARED_SETTING_RANGE_RETAINED"),
                       frequency_range_basis=RANGE_BASIS,
                       configured_power_reported=word(frame, 4),
                       actual_power_reported=word(frame, 8), power_basis=POWER_BASIS,
                       control_mode_code=control,
                       race_lock_declared=int(bool(control & RACE_LOCK_BIT)),
                       pit_mode_code=frame[7],
                       pit_mode_reported=("PIT_MODE_REPORTED_SET" if frame[7]
                                          else "PIT_MODE_REPORTED_CLEAR"),
                       pit_mode_basis=PIT_BASIS)
        else:
            raw_word = word(frame, 6)
            row.update(temperature_word_reported=raw_word,
                       temperature_signed_reading=(raw_word - 0x10000 if raw_word & 0x8000
                                                   else raw_word),
                       temperature_unsigned_reading=raw_word,
                       temperature_basis=TEMPERATURE_BASIS)
        writer.writerow(row)
        report["accepted_frames"] += 1
        report["accepted_by_kind"][kind] = report["accepted_by_kind"].get(kind, 0) + 1
        if report["accepted_frames"] > MAX_ROWS:
            raise ValueError("saved Tramp input exceeds this path's row bound")
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted Tramp output exceeds bound")
    # Byte accounting, in the units each event actually consumed: everything the walk ate is
    # attributed, so a gap is visible rather than quietly absorbed.
    consumed = dict(
        accepted_frames=report["accepted_frames"] * FRAME_LENGTH,
        query_sentinel_echoes=sum(report["query_sentinel_echoes"].values()) * FRAME_LENGTH,
        checksum_mismatch_frames=report["checksum_mismatch_frames"] * FRAME_LENGTH,
        non_zero_trailer_frames=report["non_zero_trailer_frames"] * FRAME_LENGTH,
        # Header plus code, per the pinned S_WAIT_CODE reset.
        unaccepted_code_events=sum(report["unaccepted_code_events"].values()) * 2,
        bytes_before_a_header_byte=report["bytes_before_a_header_byte"],
        truncated_trailing_bytes=report["truncated_trailing_bytes"])
    report["bytes_consumed_by_kind"] = consumed
    report["bytes_attributed"] = sum(consumed.values())
    examined = report["bytes_attributed"]
    if examined != len(raw):
        raise ValueError("saved Tramp byte accounting mismatch")
    if not report["accepted_frames"]:
        raise ValueError("no accepted Tramp reply in the saved input")
    return output.getvalue(), report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved Tramp byte stream already on disk")
    parser.add_argument("output_directory", type=Path, help="new directory for the CSV and report")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="caller declared capture microseconds; the protocol carries no time")
    parser.add_argument("--declared-device", default=None,
                        help="optional caller statement of which device this stream came from; "
                             "the protocol carries no identity, so nothing is inferred")
    args = parser.parse_args(argv)
    with args.input.open("rb") as handle:
        raw = handle.read(MAX_INPUT + 1)
    converted, report = convert(raw, args.capture_time_us, args.declared_device)
    args.output_directory.mkdir()
    (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
    (args.output_directory / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
