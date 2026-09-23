#!/usr/bin/env python3
"""Saved ArduPilot ERR / PM records -> reported controller failure and watchdog events.

Passive and offline. A saved `read_mavlog_json` report already on disk is read; no vehicle, device,
decoder invocation or network is involved, and nothing is commanded.

What this path is FOR: carrying the producer's OWN reported controller health/failure statement and its
OWN reported watchdog/internal-error accounting into common Observations, with the actor, the clock, the
original reported reason and explicit unknown accounting.

What this path deliberately is NOT:

* It is **not a cause classifier**. No crash, no root cause and no failure explanation is computed here.
  `crates/predemo-readers/src/cause.rs` and every synthetic cause candidate are untouched and unused.
* **A normal reboot is not a crash, and a missing heartbeat is not a watchdog.** No row is ever
  synthesised from an absence, a log restart, a gap or a silence: a row exists only where the producer
  wrote a record. `watchdog_reset` is reported only when the producer's own bit is set.
* **A diagnostic bit alone is not a root cause.** Bits, codes and counters travel as reported conditions
  with their own accounting, never as a resolved cause.

Meanings come from ArduPilot at pin `a64bad1a0da4382da9cd38a52dbf6379aa7b51b5`:
  libraries/AP_Logger/LogStructure.h            ERR is "QBB" = TimeUS,Subsys,ECode; PM's ErrL/InE/ErC
  libraries/AP_Logger/AP_Logger.h               LogErrorSubsystem (1..31) and LogErrorCode
  libraries/AP_InternalError/AP_InternalError.h error_t bits, including watchdog_reset = 1 << 11
  ArduCopter/{events,fence,ekf_check}.cpp        the writer call sites: which (subsystem, code) pairs the
                                                 producer actually passes, fence bitmask cast included
  libraries/AP_Vehicle/AP_Vehicle.cpp            send_watchdog_reset_statustext(), emitted only when
                                                 was_watchdog_reset() is true
  libraries/GCS_MAVLink/GCS_Common.cpp           GCS::send_textv(): only the first
                                                 MAVLINK_MSG_STATUSTEXT_FIELD_TEXT_LEN bytes are logged
  libraries/AP_Logger/LogFile.cpp                Write_Message()/Write_MessageChunk(): 64-byte chunks,
                                                 per-message id (re-use race noted in source), seq from 0

The pinned `LogErrorCode` enum carries its own warning that it "has lots of duplicate values", so a code
is named ONLY for a (subsystem, code) pairing a source qualifies -- a writer call site at the same pin, or
the pinned header's own per-subsystem comment group. There is NO general fallback: a defined subsystem the
sources do not pair with code 1 or 4 gets no name, and FAILSAFE_FENCE's code is a breach bitmask rather
than an enum value at all. Performance quantities (load, memory, long loops, loop rate,
maximum loop time) are NOT re-implemented here: they are already adopted in
`scripts/convert_ardupilot_mode_csv.py` and its case, and this path adds only the reported error reason
and the internal-error mask that those columns leave out.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

MAX_INPUT = 128 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_ROWS = 200000
# The adopted reader report's own basis string; anything else is refused rather than guessed at.
READER_BASIS = "pymavlink_decoded_no_second_wire_scaling"
# AP_Logger.h `enum class LogErrorSubsystem`. Comments marked "not used" in the source are kept, because
# a saved log may still carry them and renaming or dropping them would lose the producer's own value.
ERROR_SUBSYSTEMS = {1: "MAIN", 2: "RADIO", 3: "COMPASS", 4: "OPTFLOW", 5: "FAILSAFE_RADIO",
                    6: "FAILSAFE_BATT", 7: "FAILSAFE_GPS", 8: "FAILSAFE_GCS", 9: "FAILSAFE_FENCE",
                    10: "FLIGHT_MODE", 11: "GPS", 12: "CRASH_CHECK", 13: "FLIP", 14: "AUTOTUNE",
                    15: "PARACHUTES", 16: "EKFCHECK", 17: "FAILSAFE_EKFINAV", 18: "BARO", 19: "CPU",
                    20: "FAILSAFE_ADSB", 21: "TERRAIN", 22: "NAVIGATION", 23: "FAILSAFE_TERRAIN",
                    24: "EKF_PRIMARY", 25: "THRUST_LOSS_CHECK", 26: "FAILSAFE_SENSORS",
                    27: "FAILSAFE_LEAK", 28: "PILOT_INPUT", 29: "FAILSAFE_VIBE",
                    30: "INTERNAL_ERROR", 31: "FAILSAFE_DEADRECKON"}
# A code is named ONLY for a (subsystem, code) pair the pinned source qualifies. There is deliberately
# NO general fallback: the pinned `LogErrorCode` enum states it reuses values across subsystems, so
# applying its general codes to any subsystem that happens to lack an explicit mapping would invent
# meaning. `FAILSAFE_FENCE` is the standing proof of why -- see FENCE_BREACH_SUBSYSTEM below.
#
# Group one: pairs the pinned AP_Logger.h groups under a named subsystem in its own comment blocks.
HEADER_GROUPED_CODES = {
    1: {1: "MAIN_INS_DELAY"},                       # "subsystem specific error codes -- main"
    2: {2: "RADIO_LATE_FRAME"},                     # "-- radio"
    5: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},   # "-- failsafe_thr, batt, gps"
    6: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},
    7: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},
    11: {2: "GPS_GLITCH"},                          # "GPS specific error codes"
    12: {1: "CRASH_CHECK_CRASH", 2: "CRASH_CHECK_LOSS_OF_CONTROL"},   # "-- crash checker"
    13: {2: "FLIP_ABANDONED"},                      # "-- flip"
    15: {2: "PARACHUTE_TOO_LOW", 3: "PARACHUTE_LANDED"},   # parachute comment block
    16: {0: "EKFCHECK_VARIANCE_CLEARED", 2: "EKFCHECK_BAD_VARIANCE"},  # "EKF check definitions"
    18: {2: "BARO_GLITCH", 3: "BAD_DEPTH"},         # "Baro specific error codes"
    21: {2: "MISSING_TERRAIN_DATA"},                # "-- terrain"
    22: {2: "FAILED_TO_SET_DESTINATION", 3: "RESTARTED_RTL", 4: "FAILED_CIRCLE_INIT",
         5: "DEST_OUTSIDE_FENCE", 6: "RTL_MISSING_RNGFND"},            # "-- navigation"
    30: {1: "INTERNAL_ERRORS_DETECTED"},            # "-- internal_error"
}
# Group two: pairs an actual writer call site passes at the same pin. Read from
# ArduCopter/events.cpp, ArduCopter/fence.cpp and ArduCopter/ekf_check.cpp, which is where the
# failsafe subsystems the header's comments do NOT cover get their codes.
CALLSITE_CODES = {
    6: {1: "FAILSAFE_OCCURRED"},                    # events.cpp, battery failsafe
    8: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},    # events.cpp, GCS failsafe
    5: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},    # events.cpp, radio failsafe
    9: {0: "ERROR_RESOLVED"},                       # fence.cpp, breach cleared -- see below
    11: {0: "ERROR_RESOLVED", 2: "GPS_GLITCH"},     # ekf_check.cpp / events.cpp
    16: {0: "EKFCHECK_VARIANCE_CLEARED", 2: "EKFCHECK_BAD_VARIANCE"},  # ekf_check.cpp
    17: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},   # ekf_check.cpp, EKF-INAV failsafe
    23: {0: "ERROR_RESOLVED", 1: "FAILSAFE_OCCURRED"},      # events.cpp, terrain failsafe
    29: {0: "FAILSAFE_RESOLVED", 1: "FAILSAFE_OCCURRED"},   # events.cpp, vibration failsafe
    31: {1: "FAILSAFE_OCCURRED"},                   # events.cpp, dead-reckoning failsafe
}
# ArduCopter/fence.cpp writes `LOGGER_WRITE_ERROR(LogErrorSubsystem::FAILSAFE_FENCE,
# LogErrorCode(fence_breaches.new_breaches))` -- a fence-breach BITMASK cast into the code type. So for
# this subsystem a nonzero code is not an enum value at all, and naming it from the enum would be false.
FENCE_BREACH_SUBSYSTEM = 9
# AP_InternalError.h `enum class error_t : uint32_t`, bit position -> name.
INTERNAL_ERROR_BITS = {0: "logger_mapfailure", 1: "logger_missing_logstructure",
                       2: "logger_logwrite_missingfmt", 3: "logger_too_many_deletions",
                       4: "logger_bad_getfilename", 5: "panic", 6: "logger_flushing_without_sem",
                       7: "logger_bad_current_block", 8: "logger_blockcount_mismatch",
                       9: "logger_dequeue_failure", 10: "constraining_nan", 11: "watchdog_reset",
                       12: "iomcu_reset", 13: "iomcu_fail", 14: "spi_fail", 15: "main_loop_stuck",
                       16: "gcs_bad_missionprotocol_link", 17: "bitmask_range", 18: "gcs_offset",
                       19: "i2c_isr", 20: "flow_of_control", 21: "switch_full_sector_recursion",
                       22: "bad_rotation", 23: "stack_overflow", 24: "imu_reset", 25: "gpio_isr",
                       26: "mem_guard", 27: "dma_fail", 28: "params_restored",
                       29: "invalid_arg_or_result"}
WATCHDOG_RESET_BIT = 11
# AP_Vehicle::send_watchdog_reset_statustext() at the same pin: after a watchdog reset -- and only then --
# the vehicle emits this statustext, which a saved log carries as an MSG record. Its format string is
#   "WDG: T%d SL%u FL%u FT%u FA%x FTP%u FLR%x FICSR%u MM%u MC%u IE%u IEC%u TN:%.4s"
# over `hal.util->last_persistent_data`, so every field below is the producer's own persistent-data value.
# This is a REPORTED boot-after-watchdog statement, not an inference from a restarted clock.
# Two field names for the same PM column, each qualified by a source rather than by resemblance:
# `InE` / `ErC` are the names in the CURRENT pin's own LOG_FORMAT line -- "PM", "QHHHIIHHIIIIIIQ",
# "TimeUS,LR,NLon,NL,MaxT,Mem,Load,ErrL,InE,ErC,SPIC,I2CC,I2CI,Ex,R" -- and `IntE` / `ErrC` are the names
# the adopted `scripts/convert_ardupilot_mode_csv.py` pins for its pm-4.3.1 / pm-4.3.2 schemas. That is a
# FINITE alias qualified by two pinned schemas, NOT an assumption that a matching name means a compatible
# version: nothing else is accepted, and a record carrying BOTH names is contradictory and is refused
# rather than resolved by picking one.
MASK_FIELD_SOURCES = {"InE": "CURRENT_PIN_LOG_FORMAT", "IntE": "ADOPTED_PM_4_3_SCHEMA"}
COUNT_FIELD_SOURCES = {"ErC": "CURRENT_PIN_LOG_FORMAT", "ErrC": "ADOPTED_PM_4_3_SCHEMA"}
WATCHDOG_TEXT_PREFIX = "WDG:"
# How much of that text a saved log can actually contain, read from the producer chain rather than
# assumed. GCS::send_textv() copies the formatted text into
#   char first_piece_of_text[MAVLINK_MSG_STATUSTEXT_FIELD_TEXT_LEN+1]{};
#   memcpy(first_piece_of_text, statustext_printf_buffer, ARRAY_SIZE(first_piece_of_text)-1);
# and then logs ONLY that: `logger->Write_Message(first_piece_of_text)`. So a GCS_SEND_TEXT reaches the
# log cut to the MAVLink STATUSTEXT text-field width, which is what that symbol names.
GCS_TEXT_CUT_BYTES = 50
# AP_Logger_Backend::Write_Message() chunks at 64 bytes with a per-message id and a chunk_seq starting
# at 0, and its own comment records a race in which that id can be re-used by another thread. Because
# the GCS cut (50) is SMALLER than the chunk size (64), a statustext logged through that path is always
# a single chunk with chunk_seq 0 -- so this path never joins chunks, and never needs to.
LOGGER_CHUNK_BYTES = 64
# The pinned format string, token by token in its own order. Each entry is (token prefix, column, base).
# The pinned format string, token by token in its own order, each with the conversion the format
# actually uses: %d is a signed int, %u an unsigned int, %x lowercase hexadecimal, %.4s at most four
# characters. Accepting anything wider than those would let text the writer cannot produce qualify as a
# producer report, so each token is validated lexically AND against that conversion's domain.
WATCHDOG_TOKENS = (("T", "wdg_scheduler_task_reported", "d"),
                   ("SL", "wdg_semaphore_line_reported", "u"),
                   ("FL", "wdg_fault_line_reported", "u"),
                   ("FT", "wdg_fault_type_reported", "u"),
                   ("FA", "wdg_fault_address_reported", "x"),
                   ("FTP", "wdg_fault_thread_priority_reported", "u"),
                   ("FLR", "wdg_fault_link_register_reported", "x"),
                   ("FICSR", "wdg_fault_icsr_reported", "u"),
                   ("MM", "wdg_last_mavlink_message_id_reported", "u"),
                   ("MC", "wdg_last_mavlink_command_reported", "u"),
                   ("IE", "wdg_internal_errors_mask_reported", "u"),
                   ("IEC", "wdg_internal_error_count_reported", "u"),
                   ("TN:", "wdg_thread_name_hex", "s"))
# Lexeme shapes and domains for those conversions. `int()` on its own would accept underscores, a plus
# sign and unbounded magnitudes, none of which the writer can emit.
TOKEN_LEXEMES = {"d": re.compile(r"-?[0-9]{1,10}"), "u": re.compile(r"[0-9]{1,10}"),
                 "x": re.compile(r"[0-9a-f]{1,8}")}
TOKEN_DOMAINS = {"d": (-(2 ** 31), 2 ** 31 - 1), "u": (0, 2 ** 32 - 1), "x": (0, 2 ** 32 - 1)}
THREAD_NAME_MAX_BYTES = 4
# A text longer than the producer's cut is NOT evidence of anything by itself. It qualifies only when the
# saved record declares, in its own metadata, exactly this supported caller format; otherwise the record
# stays unqualified. This is one declared format, not a format engine.
CALLER_FORMAT_KEY = "musubi_caller_text_format"
SUPPORTED_CALLER_FORMAT = "ardupilot_watchdog_statustext_reassembled_v1"
UNQUALIFIED_EVENT_KIND = "UNQUALIFIED_WATCHDOG_PREFIXED_MSG"
UNQUALIFIED_ACTOR = "UNQUALIFIED_NOT_A_CONFIRMED_PRODUCER_SHAPED_BOOT_REPORT"
CLOCK_BASIS = ("REPORTED_TimeUS_MICROSECONDS_SINCE_SYSTEM_STARTUP_AS_THE_PINNED_LOG_STRUCTURE_DOCUMENTS_IT_"
               "SO_THE_ROW_CLOCK_IS_BOOT_RELATIVE_AND_IS_NOT_UTC_NOT_SYNCHRONISED_AND_NOT_AUTHENTICATED_"
               "AND_BECAUSE_IT_RESTARTS_AT_EACH_BOOT_IT_IS_NEVER_USED_HERE_TO_INFER_THAT_A_REBOOT_HAPPENED")
ACTOR_BASIS = ("THE_ACTOR_IS_WHAT_THE_PRODUCER_ITSELF_NAMED_FOR_AN_ERR_RECORD_THE_SUBSYSTEM_FROM_THE_PINNED_"
               "LogErrorSubsystem_ENUM_AND_FOR_A_PM_RECORD_THE_AUTOPILOTS_OWN_INTERNAL_ERROR_ACCOUNTING_NO_"
               "COMPONENT_IDENTITY_SERIAL_OR_COMPANION_COMPUTER_IS_RESOLVED_AND_NO_ACTOR_IS_INFERRED_FROM_"
               "ABSENCE")
REASON_BASIS = ("THE_REPORTED_REASON_IS_THE_PRODUCERS_OWN_CODE_THE_RAW_VALUE_IS_ALWAYS_RETAINED_AND_A_NAME_"
                "IS_ADDED_ONLY_WHERE_THE_PINNED_SOURCE_GROUPS_THAT_CODE_UNDER_THAT_SUBSYSTEM_BECAUSE_THE_"
                "PINNED_LogErrorCode_ENUM_STATES_IT_REUSES_VALUES_ACROSS_SUBSYSTEMS_SO_A_CODE_IS_NEVER_"
                "NAMED_GLOBALLY")
SCOPE_BASIS = ("THESE_ROWS_ARE_REPORTED_CONDITIONS_AND_ACCOUNTING_NOT_A_CAUSE_NO_CRASH_NO_ROOT_CAUSE_AND_NO_"
               "FAILURE_EXPLANATION_IS_COMPUTED_HERE_A_NORMAL_REBOOT_IS_NOT_A_CRASH_A_MISSING_HEARTBEAT_IS_"
               "NOT_A_WATCHDOG_AND_NO_ROW_IS_EVER_SYNTHESISED_FROM_AN_ABSENCE_A_LOG_RESTART_A_GAP_OR_A_"
               "SILENCE_A_SINGLE_DIAGNOSTIC_BIT_IS_NOT_A_ROOT_CAUSE")
WATCHDOG_BASIS = ("A_WATCHDOG_RESET_IS_REPORTED_ONLY_WHEN_BIT_11_OF_THE_PRODUCERS_OWN_INTERNAL_ERROR_MASK_IS_"
                  "SET_WHICH_THE_PINNED_AP_InternalError_HEADER_NAMES_watchdog_reset_A_ZERO_MASK_MEANS_THE_"
                  "PRODUCER_REPORTED_NO_INTERNAL_ERROR_IN_THAT_RECORD_AND_IS_NEVER_READ_AS_A_WATCHDOG_A_"
                  "CRASH_OR_A_REBOOT_AND_THE_MASK_IS_ACCUMULATED_BY_THE_AUTOPILOT_SO_IT_STATES_THAT_THE_"
                  "CONDITION_HAS_BEEN_DETECTED_NOT_WHEN_IT_OCCURRED")
REBOOT_BASIS = ("A_BOOT_AFTER_A_WATCHDOG_RESET_IS_REPORTED_BY_THE_PRODUCER_ITSELF_THE_PINNED_AP_Vehicle_"
                "send_watchdog_reset_statustext_EMITS_ITS_WDG_TEXT_ONLY_WHEN_was_watchdog_reset_IS_TRUE_SO_"
                "THE_PRESENCE_OF_THAT_LOGGED_MESSAGE_IS_THE_REPORTED_BOOT_FACT_AND_NEVER_AN_INFERENCE_FROM_A_"
                "RESTARTED_CLOCK_A_NEW_LOG_OR_A_MISSING_HEARTBEAT_A_NORMAL_POWER_CYCLE_REBOOT_IS_NOT_REPORTED_"
                "BY_ANY_RECORD_THIS_PATH_READS_AT_THIS_PIN_SO_IT_IS_LEFT_UNREPORTED_RATHER_THAN_INFERRED")
TRUNCATION_BASIS = ("THE_PRODUCER_CHAIN_CUTS_THIS_TEXT_BEFORE_IT_IS_LOGGED_GCS_send_textv_COPIES_ONLY_"
                    "MAVLINK_MSG_STATUSTEXT_FIELD_TEXT_LEN_BYTES_INTO_first_piece_of_text_AND_LOGS_ONLY_THAT_"
                    "SO_A_SAVED_MSG_CARRIES_AT_MOST_THAT_MANY_BYTES_OF_THE_WDG_TEXT_ARITHMETIC_AT_THIS_PIN_"
                    "EVEN_THE_SMALLEST_POSSIBLE_FIELD_VALUES_PRODUCE_A_65_BYTE_TEXT_SO_THE_TRAILING_IE_IEC_"
                    "AND_TN_FIELDS_ARE_NEVER_RECOVERABLE_FROM_A_NATIVE_RECORD_AND_WITH_ORDINARY_VALUES_"
                    "FICSR_MM_AND_MC_ARE_LOST_TOO_ONLY_FIELDS_WHOLLY_INSIDE_THE_RETAINED_PREFIX_ARE_DECODED_"
                    "AND_A_TOKEN_TOUCHING_THE_CUT_IS_TREATED_AS_TRUNCATED_RATHER_THAN_READ")
CHUNK_BASIS = ("THE_PINNED_LOGGER_WRITES_MSG_IN_64_BYTE_CHUNKS_WITH_A_PER_MESSAGE_ID_AND_A_chunk_seq_"
               "STARTING_AT_ZERO_AND_ITS_OWN_COMMENT_RECORDS_THAT_THE_ID_CAN_BE_REUSED_BY_ANOTHER_THREAD_"
               "BECAUSE_THE_GCS_CUT_IS_SMALLER_THAN_THAT_CHUNK_SIZE_A_STATUSTEXT_LOGGED_THROUGH_THAT_PATH_IS_"
               "ALWAYS_A_SINGLE_CHUNK_WITH_SEQUENCE_ZERO_SO_THIS_PATH_JOINS_NOTHING_A_WDG_RECORD_CARRYING_A_"
               "NONZERO_SEQUENCE_IS_REFUSED_FOR_DECODING_AND_COUNTED_RATHER_THAN_JOINED_TO_ANY_OTHER_MESSAGE_"
               "OR_ANY_OTHER_BOOT")
FIELDS = ["record_time_us", "source_report_record_index", "controller_event_kind",
          "controller_actor_reported", "err_subsystem_code", "err_subsystem_name",
          "err_reported_code", "err_reported_code_name", "err_code_disposition",
          "pm_internal_error_mask_field", "pm_internal_error_mask_field_source",
          "pm_internal_error_count_field_source", "pm_internal_error_mask_reported",
          "pm_internal_error_bits_named", "pm_internal_error_unnamed_bit_positions",
          "pm_watchdog_reset_reported", "pm_internal_error_line_reported",
          "pm_internal_error_count_reported",
          "wdg_scheduler_task_reported", "wdg_semaphore_line_reported",
          "wdg_fault_line_reported", "wdg_fault_type_reported", "wdg_fault_address_reported",
          "wdg_fault_thread_priority_reported", "wdg_fault_link_register_reported",
          "wdg_fault_icsr_reported", "wdg_last_mavlink_message_id_reported",
          "wdg_last_mavlink_command_reported", "wdg_internal_errors_mask_reported",
          "wdg_internal_error_bits_named", "wdg_internal_error_count_reported",
          "wdg_thread_name_hex", "wdg_text_disposition", "wdg_text_shape",
          "wdg_text_retained_bytes", "wdg_fields_unrecoverable",
          "msg_chunk_id_reported", "msg_chunk_sequence_reported",
          "controller_clock_basis", "controller_actor_basis", "controller_reason_basis",
          "controller_scope_basis", "controller_watchdog_basis", "controller_reboot_basis",
          "controller_truncation_basis", "controller_chunk_basis",
          "source_record_hex", "source_document_sha256"]


def unique_object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("saved reader report repeats a JSON key")
        seen[key] = value
    return seen


def reject_constant(_value):
    raise ValueError("nonfinite JSON constant in the saved reader report")


def integer(value, name, limit):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= limit:
        raise ValueError(f"reported {name} must be an unsigned integer within its documented width")
    return value


def retained_hex(record):
    """The record's own keys and values, canonical and hex-encoded, beside the interpreted columns."""
    return "hex:" + json.dumps(record, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8").hex()


def decode_err(data, row, report):
    """One ERR record: the subsystem that reported, and its own subsystem-specific code."""
    subsystem = integer(data.get("Subsys"), "ERR.Subsys", 0xFF)
    code = integer(data.get("ECode"), "ERR.ECode", 0xFF)
    name = ERROR_SUBSYSTEMS.get(subsystem)
    row["controller_event_kind"] = "REPORTED_SUBSYSTEM_ERROR"
    row["err_subsystem_code"] = subsystem
    row["err_subsystem_name"] = name or ""
    row["err_reported_code"] = code
    row["controller_actor_reported"] = name or f"SUBSYSTEM_{subsystem}_UNDEFINED_BY_THE_PIN"
    if name is None:
        report["undefined_err_subsystems"][str(subsystem)] = (
            report["undefined_err_subsystems"].get(str(subsystem), 0) + 1)
        row["err_code_disposition"] = "SUBSYSTEM_UNDEFINED_BY_THE_PIN_SO_THE_CODE_IS_NOT_NAMED"
        return
    callsite = CALLSITE_CODES.get(subsystem, {}).get(code)
    header = HEADER_GROUPED_CODES.get(subsystem, {}).get(code)
    if callsite is not None:
        row["err_reported_code_name"] = callsite
        row["err_code_disposition"] = "NAMED_BY_A_PINNED_WRITER_CALLSITE_FOR_THIS_SUBSYSTEM"
    elif header is not None:
        row["err_reported_code_name"] = header
        row["err_code_disposition"] = "NAMED_BY_THE_PINNED_HEADER_GROUP_FOR_THIS_SUBSYSTEM"
    elif subsystem == FENCE_BREACH_SUBSYSTEM:
        # The pinned fence call site casts a breach BITMASK into the code type, so a nonzero code here
        # is not an enum value and is never named from the enum.
        row["err_code_disposition"] = "FENCE_BREACH_BITMASK_CAST_INTO_THE_CODE_TYPE_NOT_AN_ENUM_CODE"
        key = f"{name}:{code}"
        report["unqualified_err_codes"][key] = report["unqualified_err_codes"].get(key, 0) + 1
    else:
        # No source qualifies this pairing, and the pinned enum reuses values across subsystems, so the
        # code stays a number. Applying a general code here would be inventing meaning.
        row["err_code_disposition"] = "CODE_NOT_SOURCE_QUALIFIED_FOR_THIS_SUBSYSTEM_SO_IT_STAYS_RAW"
        key = f"{name}:{code}"
        report["unqualified_err_codes"][key] = report["unqualified_err_codes"].get(key, 0) + 1
    report["err_records_by_subsystem"][name] = report["err_records_by_subsystem"].get(name, 0) + 1


def named_internal_error_bits(mask, report):
    """The set bits of an internal-error mask, named from the pin; unknown positions are listed."""
    named, unnamed = [], []
    for position in range(32):
        if not mask & (1 << position):
            continue
        if position in INTERNAL_ERROR_BITS:
            named.append(INTERNAL_ERROR_BITS[position])
            report["internal_error_bits"][INTERNAL_ERROR_BITS[position]] = (
                report["internal_error_bits"].get(INTERNAL_ERROR_BITS[position], 0) + 1)
        else:
            unnamed.append(str(position))
            report["undefined_internal_error_bits"][str(position)] = (
                report["undefined_internal_error_bits"].get(str(position), 0) + 1)
    return named, unnamed


def decode_watchdog_message(source_record, row, report):
    """One MSG record whose text may be the producer's own boot-after-watchdog statement.

    The event is qualified LAST, and only after a valid producer-shaped prefix has actually been read.
    A record that is refused -- a later chunk, a malformed token, an over-long text without a declared
    caller format -- stays an unqualified MSG and never advertises a boot event.
    """
    data = source_record["data"]
    text = data["Message"]
    raw = text.encode("utf-8")
    # Byte length throughout: the producer's cut is a byte count, not a character count.
    retained = len(raw)
    row["wdg_text_retained_bytes"] = retained
    row["controller_event_kind"] = UNQUALIFIED_EVENT_KIND
    row["controller_actor_reported"] = UNQUALIFIED_ACTOR

    def unqualified(disposition, bucket):
        row["wdg_text_disposition"] = disposition
        report[bucket] += 1

    # Chunk metadata, as the pinned MSG format declares it. Absence is stated, never assumed away.
    sequence = data.get("Seq")
    if "ID" in data:
        row["msg_chunk_id_reported"] = integer(data["ID"], "MSG.ID", 0xFF)
    if sequence is not None:
        row["msg_chunk_sequence_reported"] = integer(sequence, "MSG.Seq", 0xFF)
        if sequence != 0:
            # A native WDG text is always one chunk, so a later chunk cannot be read on its own and is
            # never joined to anything. It stays unqualified and is counted.
            row["wdg_text_shape"] = "LATER_CHUNK_SO_NOT_A_COMPLETE_PRODUCER_SHAPED_TEXT"
            unqualified("NONZERO_CHUNK_SEQUENCE_SO_NOT_A_COMPLETE_NATIVE_WDG_RECORD",
                        "watchdog_nonzero_chunk_records")
            return
    else:
        report["watchdog_records_without_chunk_metadata"] += 1

    # Two shapes qualify, and nothing else. A native record reaches the producer's cut exactly -- the
    # pinned format cannot produce a shorter text, so anything under the cut is not a producer WDG text
    # and its missing tail must never be blamed on the cut. A longer text qualifies only when the record
    # declares the one supported caller format AND parses completely.
    declared_caller = source_record["meta"].get(CALLER_FORMAT_KEY) == SUPPORTED_CALLER_FORMAT
    truncated = retained == GCS_TEXT_CUT_BYTES
    require_complete = False
    if retained > GCS_TEXT_CUT_BYTES:
        if not declared_caller:
            # Being long is not evidence of anything. Without an explicit supported declaration this is
            # simply a text the producer could not have logged, and it is left unqualified.
            row["wdg_text_shape"] = "LONGER_THAN_THE_PRODUCER_CUT_WITH_NO_DECLARED_CALLER_FORMAT"
            unqualified("TEXT_LONGER_THAN_THE_PRODUCER_CUT_AND_NO_SUPPORTED_CALLER_FORMAT_DECLARED",
                        "watchdog_undeclared_long_texts")
            return
        row["wdg_text_shape"] = "CALLER_DECLARED_REASSEMBLED_TEXT_NOT_A_NATIVE_LOGGER_RECORD"
        require_complete = True
    elif retained < GCS_TEXT_CUT_BYTES:
        row["wdg_text_shape"] = "SHORTER_THAN_THE_PRODUCER_CUT_SO_NOT_A_COMPLETE_PRODUCER_TEXT"
        unqualified("SHORTER_THAN_THE_PRODUCER_CUT_SO_ITS_MISSING_TAIL_IS_NOT_THE_PRODUCERS_TRUNCATION",
                    "watchdog_short_texts")
        return
    elif sequence is None or "ID" not in data:
        # At the cut, but the saved report kept no chunk identifier and sequence pair, so nothing proves
        # this was chunk zero of a native record. Both are required before the native label is used.
        row["wdg_text_shape"] = "AT_THE_PRODUCER_CUT_BUT_CHUNK_METADATA_INCOMPLETE_SO_NATIVE_SHAPE_UNKNOWN"
    else:
        row["wdg_text_shape"] = "NATIVE_LOGGER_MSG_TEXT_AT_THE_PRODUCER_CUT"

    words = text.split(" ")
    if not words or words[0] != "WDG:":
        unqualified("PREFIX_MATCHED_BUT_TEXT_DOES_NOT_START_WITH_THE_PINNED_TOKEN",
                    "unparsed_watchdog_messages")
        return
    decoded, unrecoverable, position, malformed = 0, [], 1, False
    for index, (token, column, kind) in enumerate(WATCHDOG_TOKENS):
        word = words[position] if position < len(words) else None
        last_word = position == len(words) - 1
        if word is None:
            unrecoverable.extend(name for _, name, _ in WATCHDOG_TOKENS[index:])
            break
        if not word.startswith(token):
            malformed = True
            unrecoverable.extend(name for _, name, _ in WATCHDOG_TOKENS[index:])
            break
        if truncated and last_word:
            # The retained text reached the producer's cut, so this final token may be missing digits.
            # It is NOT read, and neither is anything after it.
            unrecoverable.extend(name for _, name, _ in WATCHDOG_TOKENS[index:])
            break
        body = word[len(token):]
        if kind == "s":
            if len(body.encode("utf-8")) > THREAD_NAME_MAX_BYTES:
                malformed = True
                unrecoverable.extend(name for _, name, _ in WATCHDOG_TOKENS[index:])
                break
            row[column] = "hex:" + body.encode("utf-8").hex()
        else:
            if not TOKEN_LEXEMES[kind].fullmatch(body):
                # Not a lexeme this conversion can produce: no underscores, no plus sign, no unbounded
                # magnitude. Impossible writer text never qualifies as a producer report.
                malformed = True
                unrecoverable.extend(name for _, name, _ in WATCHDOG_TOKENS[index:])
                break
            value = int(body, 16 if kind == "x" else 10)
            low, high = TOKEN_DOMAINS[kind]
            if not low <= value <= high:
                malformed = True
                unrecoverable.extend(name for _, name, _ in WATCHDOG_TOKENS[index:])
                break
            row[column] = value
            if column == "wdg_internal_errors_mask_reported":
                named, _ = named_internal_error_bits(value, report)
                row["wdg_internal_error_bits_named"] = "|".join(named)
        decoded += 1
        position += 1
    row["wdg_fields_unrecoverable"] = "|".join(unrecoverable)
    for name in unrecoverable:
        report["watchdog_unrecoverable_fields"][name] = (
            report["watchdog_unrecoverable_fields"].get(name, 0) + 1)
    if malformed:
        # A token the writer's own conversion cannot produce means this text is not producer-shaped, so
        # nothing about it is qualified -- including whatever parsed before the malformed token.
        for _, column, _ in WATCHDOG_TOKENS:
            row[column] = ""
        row["wdg_internal_error_bits_named"] = ""
        # Not producer-shaped, so its missing fields are not the producer's truncation either.
        row["wdg_fields_unrecoverable"] = ""
        unqualified("TOKEN_OUTSIDE_THE_PINNED_FORMATS_OWN_CONVERSION_SO_THE_TEXT_IS_NOT_PRODUCER_SHAPED",
                    "watchdog_malformed_texts")
        return
    if not decoded:
        unqualified("NO_FIELD_LIES_WHOLLY_INSIDE_THE_RETAINED_PREFIX", "unparsed_watchdog_messages")
        return
    if require_complete and (unrecoverable or position != len(words)):
        # A declared reassembled text must account for the whole pinned format and carry nothing after
        # it; a missing field or a trailing extra word means it is not that format.
        for _, column, _ in WATCHDOG_TOKENS:
            row[column] = ""
        row["wdg_internal_error_bits_named"] = ""
        row["wdg_fields_unrecoverable"] = ""
        unqualified("DECLARED_CALLER_TEXT_DID_NOT_PARSE_COMPLETELY_AGAINST_THE_PINNED_FORMAT",
                    "watchdog_incomplete_declared_texts")
        return
    # Only now, with a valid producer-shaped prefix actually read, is the event qualified.
    row["controller_event_kind"] = "REPORTED_BOOT_AFTER_WATCHDOG_RESET"
    row["controller_actor_reported"] = "AUTOPILOT_WATCHDOG_PERSISTENT_DATA"
    row["wdg_text_disposition"] = ("PREFIX_FIELDS_DECODED_TAIL_LOST_TO_THE_PRODUCER_CUT" if unrecoverable
                                   else "EVERY_PINNED_FIELD_PRESENT_IN_THE_RETAINED_TEXT")
    report["watchdog_boot_reports"] += 1


def decode_pm(data, row, report):
    """One PM record: the autopilot's own internal-error mask, line and count. Quantities stay elsewhere."""
    present = [name for name in MASK_FIELD_SOURCES if name in data]
    if not present:
        raise ValueError("selected PM record carries no internal error mask field")
    if len(present) > 1:
        # Two names for the same column in one record is contradictory: the pinned schemas each use one
        # name, so this record matches neither. Refused rather than resolved by picking the first.
        raise ValueError("PM record carries more than one internal error mask field name; "
                         "the pinned schemas each declare exactly one, so this is refused")
    field = present[0]
    mask = integer(data.get(field), f"PM.{field}", 0xFFFFFFFF)
    row["controller_event_kind"] = "REPORTED_INTERNAL_ERROR_ACCOUNTING"
    row["controller_actor_reported"] = "AUTOPILOT_INTERNAL_ERROR_ACCOUNTING"
    row["pm_internal_error_mask_field"] = field
    row["pm_internal_error_mask_field_source"] = MASK_FIELD_SOURCES[field]
    row["pm_internal_error_mask_reported"] = mask
    named, unnamed = named_internal_error_bits(mask, report)
    row["pm_internal_error_bits_named"] = "|".join(named)
    row["pm_internal_error_unnamed_bit_positions"] = "|".join(unnamed)
    # The one bit this unit's watchdog role rests on, read from the producer's mask and nothing else.
    row["pm_watchdog_reset_reported"] = 1 if mask & (1 << WATCHDOG_RESET_BIT) else 0
    if row["pm_watchdog_reset_reported"]:
        report["watchdog_reset_reports"] += 1
    if "ErrL" in data:
        row["pm_internal_error_line_reported"] = integer(data["ErrL"], "PM.ErrL", 0xFFFF)
    counts = [name for name in COUNT_FIELD_SOURCES if name in data]
    if len(counts) > 1:
        raise ValueError("PM record carries more than one internal error count field name; "
                         "the pinned schemas each declare exactly one, so this is refused")
    if counts:
        row["pm_internal_error_count_reported"] = integer(data[counts[0]], f"PM.{counts[0]}",
                                                          0xFFFFFFFF)
        row["pm_internal_error_count_field_source"] = COUNT_FIELD_SOURCES[counts[0]]


def convert(raw):
    """Selected ERR and PM records of a saved reader report; every other record type is counted."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT:
        raise ValueError("bounded nonempty saved reader report required")
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    if not isinstance(document, dict) or document.get("value_basis") != READER_BASIS:
        raise ValueError("expected a decoded read_mavlog_json report, not raw JSONL or wire data")
    records = document.get("source_records")
    if not isinstance(records, list) or not records:
        raise ValueError("source_records required")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    report = {"source_sha256": digest, "source_records": len(records), "selected_rows": 0,
              "err_records_by_subsystem": {}, "unqualified_err_codes": {},
              "undefined_err_subsystems": {}, "internal_error_bits": {},
              "undefined_internal_error_bits": {}, "watchdog_reset_reports": 0,
              "watchdog_boot_reports": 0, "unparsed_watchdog_messages": 0,
              "watchdog_nonzero_chunk_records": 0, "watchdog_records_without_chunk_metadata": 0,
              "watchdog_undeclared_long_texts": 0, "watchdog_malformed_texts": 0,
              "watchdog_short_texts": 0, "watchdog_incomplete_declared_texts": 0,
              "watchdog_unrecoverable_fields": {},
              "reboot": REBOOT_BASIS, "truncation": TRUNCATION_BASIS, "chunking": CHUNK_BASIS,
              "unselected_record_types": {}, "clock": "BootRelative",
              "clock_basis": CLOCK_BASIS, "actor_basis": ACTOR_BASIS, "reason_basis": REASON_BASIS,
              "scope": SCOPE_BASIS, "watchdog_basis": WATCHDOG_BASIS}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    previous = None
    for index, record in enumerate(records):
        if (not isinstance(record, dict) or not isinstance(record.get("meta"), dict)
                or not isinstance(record.get("data"), dict)):
            raise ValueError("invalid source record shape")
        kind = record["meta"].get("type")
        if not isinstance(kind, str) or not kind or len(kind) > 256:
            raise ValueError("invalid source record type")
        if kind == "MSG":
            text = record["data"].get("Message")
            if not isinstance(text, str) or not text.startswith(WATCHDOG_TEXT_PREFIX):
                # A general MSG is not a controller health statement, so it is counted, never read.
                key = "MSG" if not isinstance(text, str) else "MSG_WITHOUT_THE_WATCHDOG_PREFIX"
                report["unselected_record_types"][key] = report["unselected_record_types"].get(key, 0) + 1
                continue
        elif kind not in ("ERR", "PM"):
            report["unselected_record_types"][kind] = report["unselected_record_types"].get(kind, 0) + 1
            continue
        if report["selected_rows"] >= MAX_ROWS:
            raise ValueError("saved report exceeds the selected row bound")
        data = record["data"]
        timestamp = integer(data.get("TimeUS"), f"{kind}.TimeUS", 2**63 - 1)
        if previous is not None and timestamp < previous:
            raise ValueError("selected records must not go backwards in reported boot time")
        previous = timestamp
        row = {name: "" for name in FIELDS}
        row.update(record_time_us=timestamp, source_report_record_index=index,
                   controller_clock_basis=CLOCK_BASIS, controller_actor_basis=ACTOR_BASIS,
                   controller_reason_basis=REASON_BASIS, controller_scope_basis=SCOPE_BASIS,
                   controller_watchdog_basis=WATCHDOG_BASIS, controller_reboot_basis=REBOOT_BASIS,
                   controller_truncation_basis=TRUNCATION_BASIS, controller_chunk_basis=CHUNK_BASIS,
                   source_record_hex=retained_hex(record),
                   source_document_sha256=digest)
        if kind == "ERR":
            decode_err(data, row, report)
        elif kind == "PM":
            decode_pm(data, row, report)
        else:
            decode_watchdog_message(record, row, report)
        writer.writerow(row)
        report["selected_rows"] += 1
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted controller reports exceed the output bound")
    if not report["selected_rows"]:
        raise ValueError("no ERR, PM or reported watchdog-boot MSG record was present in the report")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved read_mavlog_json report already on disk")
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        size = args.input.stat().st_size
        if size > MAX_INPUT:
            raise ValueError(f"saved report is {size} bytes, past the {MAX_INPUT} byte input bound")
        converted, report = convert(args.input.read_bytes())
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"saved ArduPilot controller report conversion failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
