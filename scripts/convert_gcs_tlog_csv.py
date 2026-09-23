#!/usr/bin/env python3
"""Mission Planner tlog->CSV export of one selected MAVLink message -> CSV for TelemetryCsvReader.

Passive import of a saved export. No GCS, device, link or mission is involved, and the exporter is never
executed here: inputs are read in exactly the shape the pinned writer produces.

Writer, read at MissionPlanner 1.3.82 `ca3731f5c19f10f32740fe1519c01b4f444d715a`:
`Log/MavlinkLog.cs` `BUT_convertcsv_Click` writes one row per packet as
`lastlogread.ToString("yyyy-MM-ddTHH:mm:ss.fff") + "," + DebugPacket(packet, ref text, false, ",")`, and
`ExtLibs/ArduPilot/Mavlink/MAVLinkInterface.cs` `DebugPacket` builds that text as eight width-padded
**hex** header fields, then the message struct name, then `fieldName,value` pairs, then a
`,sig <b64>,Len,<len>,crc16,<crc>` tail. Rows are therefore self-labelling: a selected struct name plus
named fields is a position-independent mapping, and no column order is assumed.

The adjacent release `MissionPlanner1.3.81` (`2f3e02b2`) writes these two messages with the same functions,
so it is accepted as a second declared `--source-format` whose only effect is the recorded exporter pin.
The compatibility is a source comparison of every commit between the two tags, not an assumption from the
version numbers; see the integration case for the compared functions.

Units come from the pinned dialect (`mavlink/c_library_v2`
`a3661c0a85dc1de1826f5c95863048af3e2b0d04`), never from a column's spelling, because `DebugPacket` writes
`fieldValue.ToString()` with no scaling: ATTITUDE `[ms]`/`[rad]`/`[rad/s]`, SYS_STATUS `[mV]`/`[cA]`/`[%]`
with the documented `UINT16_MAX`/`-1` "not sent by autopilot" sentinels.

`DebugPacket` renders an **array** field as its name followed by an empty value: it writes
`field.Name + delimeter`, then appends content only for `byte[]`, `char[]` and `short[]` — there is
no `float[]` branch — then appends `delimeter` again. So a `float[4]` such as
GIMBAL_DEVICE_ATTITUDE_STATUS `q` is written as `q,,` and the `fieldName,value` pairing is preserved
with an empty value. Nothing here reads that field; the point is that a normal row containing it
parses rather than being refused.

A third selection, `mavlink_gimbal_device_attitude_status_t` (285), carries a gimbal device's own
reported condition bitmask. Its bits are reports the device makes about itself, separated here from
any physical cause: `GIMBAL_DEVICE_ERROR_FLAGS_ENCODER_ERROR` means the gimbal said there is an error
with its **position** encoders - a mechanical sense, never a video or media encoder - with no per-axis
attribution, no encoder measurement and no diagnosis of what is physically wrong. Five of that enum's
ten entries are not errors at all (travel limits, a running calibration, an unassigned manager), so
they are carried as themselves and excluded from the reported fault count.

A fourth selection, `mavlink_storage_information_t` (261), carries a camera's own reported **storage
condition**: `EMPTY` is the source saying storage is missing, `NOT_SUPPORTED` that it supplies no
storage status at all. None of it is a physical cause, a media-health finding or evidence that a
recording failed. The dialect's own rule that a capacity "will be ignored" unless the storage is
READY is applied, with the written token retained beside the withheld value, and the capacities are
**MiB** (mebibytes), never MB. Both pinned releases declare the same eleven fields for this message,
so both are accepted; neither declares `storage_usage`, so its absence is a limit of the exporter
build and never the sender withholding it.

The exporter's first column is `lastlogread`, which that source computes as
`DateTime(1970,1,1,UTC).AddMilliseconds(dateint / 1000).ToLocalTime()` — a local-time rendering of a Unix
instant at millisecond resolution with no zone recorded. It is retained as text and never converted to
UTC. See the private integration case for the full contract.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import sys

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
MAX_ROWS = 500_000
MAX_LINE = 64 * 1024
SOURCE_FORMAT = "missionplanner-1.3.82-tlog-csv"
EXPORTER_PIN = "missionplanner-1.3.82-ca3731f5"
# The adjacent release writes these two messages with the same functions, so one declared selector per
# release is enough - no second converter. Tag `MissionPlanner1.3.81` = 2f3e02b2 and
# `MissionPlanner1.3.82` = ca3731f5; `Log/MavlinkLog.cs`, `MAVLinkInterface.DebugPacket`,
# `readlogPacketMavlink`, `MAVLinkMessage.cs`, `MavlinkCRC.cs` and the generated `mavlink_attitude_t` /
# `mavlink_sys_status_t` structs are identical at both pins and at every commit between them, so the row
# shape, field types, units, sentinels and the local-time stamp are the same. The selector only records
# WHICH release produced the file; it never changes how a value is read.
SOURCE_FORMATS = {SOURCE_FORMAT: EXPORTER_PIN,
                  "missionplanner-1.3.81-tlog-csv": "missionplanner-1.3.81-2f3e02b2"}
DIALECT_PIN = "c_library_v2-a3661c0a"
# `DebugPacket` writes these eight header fields first, each with `:X` (hexadecimal) and width padding.
HEADER_FIELDS = ("header", "payloadlength", "incompat_flags", "compat_flags", "seq", "sysid", "compid",
                 "msgid")
# The tail `DebugPacket` appends after the last field pair.
TAIL_MARKERS = ("sig", "Len", "crc16")
LOCAL_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\Z")
HEX_TOKEN = re.compile(r"[0-9A-Fa-f]{1,8}\Z")
DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z")
UNSIGNED = re.compile(r"[0-9]{1,10}\Z")
SIGNED = re.compile(r"[+-]?[0-9]{1,10}\Z")

# selected struct -> (clock source, {dialect field: (output column, scale, sentinel test)}).
# Every unit below is quoted from the pinned dialect header, not inferred from the field name.
ATTITUDE = {
    "roll": ("reported_roll_rad", 1.0),            # [rad]
    "pitch": ("reported_pitch_rad", 1.0),          # [rad]
    "yaw": ("reported_yaw_rad", 1.0),              # [rad]
    "rollspeed": ("reported_angular_velocity_x_rad_s", 1.0),   # [rad/s]
    "pitchspeed": ("reported_angular_velocity_y_rad_s", 1.0),  # [rad/s]
    "yawspeed": ("reported_angular_velocity_z_rad_s", 1.0),    # [rad/s]
}
# Integer fields carry their dialect type domain and the sentinel the dialect documents. A signed current
# stays legitimate inside its domain: only the type range and the documented percentage range are checked.
STATUS = {
    # [mV] uint16_t, UINT16_MAX: "Voltage not sent by autopilot"
    "voltage_battery": ("electrical_voltage_v", 0.001, 65535, 0, 65535),
    # [cA] int16_t, -1: "Current not sent by autopilot"; other negatives are a real sign convention
    "current_battery": ("electrical_current_a", 0.01, -1, -32768, 32767),
    # [%] int8_t, -1: "not sent by autopilot"; the documented quantity range is 0-100
    "battery_remaining": ("battery_remaining_fraction", 0.01, -1, 0, 100),
}
# GIMBAL_DEVICE_ATTITUDE_STATUS 285 was read at `mavlink/c_library_v2`
# `04fffaab116486ffdf7501c37a3f7393eb7beffc` (the id this repo already fixes for MAVLink message
# decoding), cross-checked against `mavlink/mavlink` `common.xml`
# `3203f89c510337c0088244735c6a5056c52b5a28`, where the enum and field set are identical. That id is
# recorded here per selection: this message was NOT read at the `a3661c0a` pin the two older
# selections declare, so it does not claim it, and their pin is not changed.
GIMBAL_DIALECT_PIN = "c_library_v2-04fffaab"
# The three [rad/s] quantities the message carries. The dialect declares `invalid="NaN"` on each, so a
# written NaN is the source saying "unknown" - see NAN_IS_UNKNOWN.
GIMBAL = {
    "angular_velocity_x": ("gimbal_angular_velocity_x_rad_s", 1.0),   # [rad/s]
    "angular_velocity_y": ("gimbal_angular_velocity_y_rad_s", 1.0),   # [rad/s]
    "angular_velocity_z": ("gimbal_angular_velocity_z_rad_s", 1.0),   # [rad/s]
}
# Fields whose dialect definition declares `invalid="NaN"`. For those, the writer's NaN token is a
# documented unknown, not a malformed quantity: it is withheld and named, never refused and never
# read as zero. C# renders float.NaN with the culture's NaNSymbol, which is `NaN` under the declared
# dot culture; any other spelling is refused rather than guessed at.
NAN_IS_UNKNOWN = frozenset(GIMBAL)
NAN_TOKEN = "NaN"
# `GIMBAL_DEVICE_ERROR_FLAGS` at the gimbal pin: bit, column, and whether the pinned description words
# it as an error at all. Five of the ten entries do not - a hardware travel limit, a running
# calibration and an unassigned manager are reported conditions, not faults. The enum's NAME is not
# allowed to stand for its CONTENT, so only the five "There is an error with ..." entries are counted.
GIMBAL_ERROR_BITS = (
    (1, "gimbal_at_roll_limit_reported", False),      # Gimbal device is limited by hardware roll limit.
    (2, "gimbal_at_pitch_limit_reported", False),     # ... hardware pitch limit.
    (4, "gimbal_at_yaw_limit_reported", False),       # ... hardware yaw limit.
    (8, "gimbal_encoder_error_reported", True),       # There is an error with the gimbal encoders.
    (16, "gimbal_power_error_reported", True),        # ... with the gimbal power source.
    (32, "gimbal_motor_error_reported", True),        # ... with the gimbal motors.
    (64, "gimbal_software_error_reported", True),     # ... with the gimbal's software.
    (128, "gimbal_comms_error_reported", True),       # ... with the gimbal's communication.
    (256, "gimbal_calibration_running_reported", False),  # Gimbal device is currently calibrating.
    (512, "gimbal_no_manager_reported", False),       # Not assigned to a gimbal manager.
)
GIMBAL_KNOWN_BITS = 0x3FF  # the ten pinned entries; anything above is retained, never discarded
GIMBAL_FAILURE_BASIS = ("THE_GIMBAL_DEVICES_OWN_REPORTED_BITMASK_ZERO_MEANS_THE_DEVICE_REPORTED_NO_"
                        "FAILURE_NOT_AN_INDEPENDENTLY_VERIFIED_HEALTHY_GIMBAL_AND_NO_PHYSICAL_CAUSE_"
                        "IS_ASSIGNED_HERE")
GIMBAL_ENCODER_BASIS = ("A_REPORTED_ERROR_WITH_THE_GIMBALS_OWN_POSITION_ENCODERS_A_MECHANICAL_SENSE_"
                        "NEVER_A_VIDEO_OR_MEDIA_ENCODER_CARRYING_NO_PER_AXIS_ATTRIBUTION_NO_ENCODER_"
                        "COUNT_NO_MEASUREMENT_AND_NO_PHYSICAL_CAUSE_IN_THIS_SOURCE")
GIMBAL_FLAGS_BASIS = ("RAW_GIMBAL_DEVICE_FLAGS_WORD_OPERATING_MODE_AND_LOCK_STATE_RETAINED_"
                      "UNINTERPRETED_IN_THIS_PASS_AND_NEVER_A_HEALTH_VERDICT")
# `MAVLINK_MSG_ID_ATTITUDE 30` / `MAVLINK_MSG_ID_SYS_STATUS 1` at the pinned dialect: the struct name the
# exporter wrote and the hex msgid in the same row must agree, or the row is not what it claims to be.
GIMBAL_MESSAGE = "mavlink_gimbal_device_attitude_status_t"
# STORAGE_INFORMATION 261 was read at the same `04fffaab` pin (CRC_EXTRA 179, MIN_LEN 27, LEN 61) and
# cross-checked in `common.xml` at `3203f89c…`. `time_boot_ms` was checked rather than assumed to
# match the gimbal message: it is `uint32_t [ms]` in the dialect and `uint time_boot_ms` in the
# exporter's own struct, so the token is an unsigned integer and ms->us is exact.
STORAGE_MESSAGE = "mavlink_storage_information_t"
STORAGE_DIALECT_PIN = "c_library_v2-04fffaab"
# The dialect's own units. MiB is a mebibyte (1048576 bytes) and is NOT MB: no byte or MB figure is
# derived here, so the two are never conflated. The three capacities each carry the sentence "If
# storage is not ready (STORAGE_STATUS_READY) value will be ignored"; the two speeds carry no such
# clause, so the ignore-rule is applied to the capacities only.
STORAGE = {
    "total_capacity": ("storage_total_capacity_mib", 1.0),       # [MiB]
    "used_capacity": ("storage_used_capacity_mib", 1.0),         # [MiB]
    "available_capacity": ("storage_available_capacity_mib", 1.0),  # [MiB]
    "read_speed": ("storage_read_speed_mib_s", 1.0),             # [MiB/s]
    "write_speed": ("storage_write_speed_mib_s", 1.0),           # [MiB/s]
}
STORAGE_CAPACITIES = (("total_capacity", "storage_total_capacity_mib", "storage_total_capacity_raw"),
                      ("used_capacity", "storage_used_capacity_mib", "storage_used_capacity_raw"),
                      ("available_capacity", "storage_available_capacity_mib",
                       "storage_available_capacity_raw"))
STORAGE_READY = 2
# Exactly the fields the pinned exporter builds declare for this struct, plus the extension a newer
# build could add. Nothing else is this writer's output for message 261.
STORAGE_WRITTEN_FIELDS = frozenset(
    ("time_boot_ms", "total_capacity", "used_capacity", "available_capacity", "read_speed",
     "write_speed", "storage_id", "storage_count", "status", "type", "name", "storage_usage"))
STORAGE_STATUS_NAMES = {0: "EMPTY", 1: "UNFORMATTED", 2: "READY", 3: "NOT_SUPPORTED"}
STORAGE_TYPE_NAMES = {0: "UNKNOWN", 1: "USB_STICK", 2: "SD", 3: "MICROSD", 4: "CF", 5: "CFE",
                      6: "XQD", 7: "HD", 254: "OTHER"}
STORAGE_STATUS_BASIS = ("THE_CAMERAS_OWN_REPORTED_STORAGE_CONDITION_EMPTY_MEANS_THE_SOURCE_SAID_"
                        "STORAGE_IS_MISSING_AND_NOT_SUPPORTED_MEANS_IT_SUPPLIES_NO_STORAGE_STATUS_"
                        "AT_ALL_NONE_OF_THESE_IS_A_PHYSICAL_CAUSE_A_MEDIA_HEALTH_FINDING_OR_"
                        "EVIDENCE_THAT_A_RECORDING_FAILED")
STORAGE_IDENTITY_BASIS = ("REPORTED_STORAGE_ID_COUNTED_FROM_ONE_WITHIN_THE_REPORTED_STORAGE_COUNT_"
                          "A_ZERO_IS_UNDOCUMENTED_IN_A_REPORT_BECAUSE_ZERO_MEANS_ALL_ONLY_IN_THE_"
                          "REQUEST_COMMAND_AND_NEITHER_THE_ID_NOR_THE_SENDER_IS_AUTHENTICATED")
STORAGE_UNIT_BASIS = ("MEBIBYTES_OF_1048576_BYTES_AS_THE_DIALECT_DECLARES_NOT_MEGABYTES_AND_NO_BYTE_"
                      "OR_MB_FIGURE_IS_DERIVED_HERE")
STORAGE_SPEED_BASIS = ("REPORTED_MEBIBYTES_PER_SECOND_THE_DIALECT_ATTACHES_ITS_IGNORE_WHEN_NOT_READY_"
                       "RULE_TO_THE_THREE_CAPACITIES_ONLY_SO_THESE_ARE_CARRIED_AS_WRITTEN_AND_ARE_"
                       "NEITHER_A_MEASURED_THROUGHPUT_NOR_A_MEDIA_HEALTH_FIGURE")
STORAGE_READY_VALIDITY = "REPORTED_WITH_THE_STORAGE_READY"
STORAGE_IGNORE_VALIDITY = ("IGNORED_BECAUSE_THE_SOURCE_STATES_A_CAPACITY_IS_TO_BE_IGNORED_UNLESS_THE_"
                           "STORAGE_IS_READY_THE_WRITTEN_TOKEN_IS_RETAINED_BESIDE_IT")
STORAGE_NEGATIVE_VALIDITY = ("UNQUALIFIED_NEGATIVE_CAPACITY_THE_DIALECT_DEFINES_NO_MEANING_FOR_ONE_SO_"
                             "IT_IS_RETAINED_RAW_RATHER_THAN_CLAMPED_TO_ZERO")
STORAGE_NAME_BASIS = "REPORTED_STORAGE_NAME_TEXT_RETAINED_AS_WRITTEN_AND_NOT_INTERPRETED"
STORAGE_EMPTY_NAME_BASIS = ("AN_EMPTY_NAME_IS_MEANINGFUL_NOT_UNKNOWN_THE_DIALECT_SAYS_THE_GENERIC_"
                            "TYPE_IS_SHOWN_TO_THE_USER_INSTEAD")
# Neither pinned release declares `storage_usage` in its generated struct, so a Mission Planner
# export cannot print it for ANY sender. Its absence is a limit of the exporting build and is never
# reported as the sender withholding it. A newer build that does print it is carried raw rather than
# dropped, and the flags are not decoded here: the dialect says bit 1 SET is what indicates the field
# is supported at all.
STORAGE_USAGE_ABSENT = ("NOT_PRINTED_BY_EITHER_PINNED_EXPORTER_BUILD_WHOSE_GENERATED_STRUCT_HAS_NO_"
                        "SUCH_FIELD_SO_THIS_IS_A_LIMIT_OF_THE_EXPORTER_AND_NOT_THE_SENDER_WITHHOLDING_IT")
STORAGE_USAGE_PRESENT = ("RAW_REPORTED_STORAGE_USAGE_FLAGS_RETAINED_UNDECODED_BIT_ONE_IS_WHAT_THE_"
                         "DIALECT_SAYS_INDICATES_THE_FIELD_IS_SUPPORTED_AT_ALL")
SELECTED = {
    "mavlink_attitude_t": ("time_boot_ms", ATTITUDE, 30),
    "mavlink_sys_status_t": (None, STATUS, 1),
    GIMBAL_MESSAGE: ("time_boot_ms", GIMBAL, 285),
    STORAGE_MESSAGE: ("time_boot_ms", STORAGE, 261),
}
# Fields a selection needs beyond its quantity table and its clock. They are read by a dedicated block
# rather than the quantity loop, but their absence must still refuse the row rather than pass an
# invented default.
REQUIRED_EXTRA = {GIMBAL_MESSAGE: ("failure_flags", "flags"),
                  # `type` and `name` are dialect extensions, but both pinned exporter builds declare
                  # them in the struct, so this writer always prints them. A row without one is not
                  # this writer's output and is refused rather than filled in.
                  STORAGE_MESSAGE: ("storage_id", "storage_count", "status", "type", "name")}
# A selection restricted to the releases whose generated struct was actually read for it. The two
# older selections are unrestricted, because their structs were source-compared across both releases.
# `ExtLibs/Mavlink/Mavlink.cs` `mavlink_gimbal_device_attitude_status_t` is NOT the same at the two
# pins: at 1.3.82 `ca3731f5` it declares twelve fields including the `delta_yaw`,
# `delta_yaw_velocity` and `gimbal_device_id` extensions, and at 1.3.81 `2f3e02b2` it declares only
# nine and has no `gimbal_device_id` at all. A 1.3.81 export therefore cannot render that field for
# any sender, so reading its absence as "the sender did not provide the extension" would blame the
# source for a limit of the exporting build. The combination is refused instead of misattributed.
SELECTION_RELEASES = {GIMBAL_MESSAGE: (SOURCE_FORMAT,)}
# `DebugPacket` writes MAVLink header fields, so each has a wire domain; msgid is the 24-bit field.
HEADER_BOUNDS = {"header": (0xFD, 0xFE), "payloadlength": (0, 255), "incompat_flags": (0, 255),
                 "compat_flags": (0, 255), "seq": (0, 255), "sysid": (0, 255), "compid": (0, 255),
                 "msgid": (0, 0xFFFFFF)}
FLOAT32_MAX = 3.4028234663852886e+38
ATTITUDE_FIELDS = ["record_time_us", "record_time_basis", "source_local_time_text", "source_msgid",
                   "source_sysid", "source_compid", "source_row_hex", "selected_message",
                   "exporter_pin", "dialect_pin"] + [column for column, _ in ATTITUDE.values()]
STATUS_FIELDS = ["record_time_us", "record_time_basis", "source_local_time_text", "source_msgid",
                 "source_sysid", "source_compid", "source_row_hex", "selected_message", "exporter_pin",
                 "dialect_pin", "measurement_status"] + [entry[0] for entry in STATUS.values()]
GIMBAL_FIELDS = (["record_time_us", "record_time_basis", "source_local_time_text", "source_msgid",
                  "source_sysid", "source_compid", "source_row_hex", "selected_message", "exporter_pin",
                  "dialect_pin", "measurement_status", "gimbal_failure_flags_raw",
                  "gimbal_failure_flags_unknown_bits", "gimbal_reported_fault_count",
                  "gimbal_failure_flags_basis", "gimbal_encoder_error_basis", "gimbal_flags_raw",
                  "gimbal_flags_basis", "gimbal_device_id_raw", "gimbal_device_id_basis"]
                 + [column for _, column, _ in GIMBAL_ERROR_BITS]
                 + [column for column, _ in GIMBAL.values()])
STORAGE_FIELDS = (["record_time_us", "record_time_basis", "source_local_time_text", "source_msgid",
                   "source_sysid", "source_compid", "source_row_hex", "selected_message",
                   "exporter_pin", "dialect_pin", "storage_status_code", "storage_status_reported",
                   "storage_status_basis", "storage_id_raw", "storage_count_raw",
                   "storage_identity_basis", "storage_total_capacity_raw",
                   "storage_used_capacity_raw", "storage_available_capacity_raw",
                   "storage_capacity_validity", "storage_capacity_unit_basis", "storage_speed_basis",
                   "storage_type_code", "storage_type_reported", "storage_name_hex",
                   "storage_name_basis", "storage_usage_raw", "storage_usage_availability"]
                  + [column for column, _ in STORAGE.values()])


def _tokens(line, number):
    """Split one exported row. `DebugPacket` pads with `,` and never quotes, so cells are plain."""
    if '"' in line:
        raise ValueError(f"row {number}: this writer never quotes a cell")
    return [token.strip() for token in line.split(",")]


def _pairs(tokens, number):
    """The `fieldName,value` region between the struct name and the `sig`/`Len`/`crc16` tail.

    Token 0 is the stamp, 1-8 the hex header fields and 9 the struct name, so pairs start at 10. The
    writer leaves one empty token before the tail, because the last pair already ends with a delimiter
    and the tail prepends another.
    """
    body = tokens[10:]
    # The writer always closes a row with exactly `<empty>,sig <b64>,Len,<len>,crc16,<crc>`; check that
    # shape and those values, and nothing more. The frame bytes are not in the export, so this is a
    # syntax and range check of the written tail - it is NOT a CRC verification of anything.
    if len(body) < 6:
        raise ValueError(f"row {number}: shorter than the writer's closing tail")
    tail, body = body[-5:], body[:-5]
    if not tail[0].startswith(TAIL_MARKERS[0]):
        raise ValueError(f"row {number}: the row does not close with the writer's sig/Len/crc16 tail")
    signature = tail[0][len(TAIL_MARKERS[0]):].strip()
    if signature and not re.fullmatch(r"[A-Za-z0-9+/]{1,64}={0,2}", signature):
        raise ValueError(f"row {number}: the sig token is not the writer's base64 form")
    if tail[1] != TAIL_MARKERS[1] or tail[3] != TAIL_MARKERS[2]:
        raise ValueError(f"row {number}: the writer's Len/crc16 labels are missing or out of order")
    if not UNSIGNED.fullmatch(tail[2]) or not 8 <= int(tail[2]) <= 280:
        raise ValueError(f"row {number}: Len is not a plausible written frame length")
    if not UNSIGNED.fullmatch(tail[4]) or int(tail[4]) > 0xFFFF:
        raise ValueError(f"row {number}: crc16 is not the writer's decimal uint16 token")
    if body and body[-1] == "":
        body.pop()
    # An empty VALUE is legitimate here: `DebugPacket` renders an array field as its name, then
    # content only for `byte[]`/`char[]`/`short[]` up to a zero element, then the delimiter. So a
    # `float[]`, or a `byte[]` whose text is empty, is written as `name,,` - and when that field is
    # the LAST one the row ends in two empty tokens. STORAGE_INFORMATION `name` is exactly that case
    # and the dialect states an empty name is meaningful. Refusing it would reject a normal row. The
    # malformed cases stay caught: an odd body length is refused just below, and an empty field NAME
    # is refused in the loop.
    if len(body) % 2:
        # A comma decimal separator adds a token and breaks the pairing; refuse instead of guessing.
        raise ValueError(f"row {number}: field name/value pairing does not close; check the export culture")
    fields = {}
    for name, value in zip(body[0::2], body[1::2]):
        if not name or len(name) > 64:
            raise ValueError(f"row {number}: unusable field name")
        if name in fields:
            raise ValueError(f"row {number}: duplicate field name {name}")
        fields[name] = value
    return fields


def _unsigned(token, name, number, high):
    """One of the dialect's unsigned integer fields, read inside its own wire domain."""
    if not UNSIGNED.fullmatch(token) or int(token) > high:
        raise ValueError(f"row {number}: {name} must be the dialect's unsigned value at most {high}")
    return int(token)


def _gimbal(fields, row, report, number):
    """The gimbal device's own reported condition bitmask, its raw state word and its device id.

    Every named bit is a REPORT by the device about itself. Nothing here is a physical cause: a
    reported encoder error is what the gimbal said about its encoders, never a finding about a
    magnet, a bearing, wiring or a connector, and a zero mask is the device reporting no failure
    rather than an independently verified healthy gimbal.
    """
    failure = _unsigned(fields["failure_flags"], "failure_flags", number, 0xFFFFFFFF)
    row["gimbal_failure_flags_raw"] = failure
    # Bits this pinned enum does not define are kept as themselves, not dropped and not interpreted.
    row["gimbal_failure_flags_unknown_bits"] = failure & ~GIMBAL_KNOWN_BITS
    faults = 0
    for bit, column, is_fault in GIMBAL_ERROR_BITS:
        reported = failure & bit != 0
        row[column] = int(reported)
        if reported and is_fault:
            faults += 1
    row["gimbal_reported_fault_count"] = faults
    row["gimbal_failure_flags_basis"] = GIMBAL_FAILURE_BASIS
    row["gimbal_encoder_error_basis"] = GIMBAL_ENCODER_BASIS
    row["gimbal_flags_raw"] = _unsigned(fields["flags"], "flags", number, 0xFFFF)
    row["gimbal_flags_basis"] = GIMBAL_FLAGS_BASIS
    # `gimbal_device_id` is a MAVLink2 extension, so it can be absent from a row, and the dialect
    # documents 1-6 as the declared range with 0 meaning "this field is not required here". A zero
    # therefore cannot be told apart from an unavailable extension, and neither becomes device zero.
    if "gimbal_device_id" not in fields:
        row["gimbal_device_id_raw"] = ""
        row["gimbal_device_id_basis"] = "NOT_PRESENT_IN_THE_EXPORTED_ROW_AN_ABSENT_MAVLINK2_EXTENSION"
        report["withheld_values"] += 1
        return
    device = _unsigned(fields["gimbal_device_id"], "gimbal_device_id", number, 0xFF)
    row["gimbal_device_id_raw"] = device
    if 1 <= device <= 6:
        basis = "DECLARED_GIMBAL_DEVICE_ID_1_TO_6_REPORTED_NOT_AUTHENTICATED_IDENTITY"
    elif device == 0:
        basis = ("UNQUALIFIED_ZERO_THE_FIELD_IS_NOT_REQUIRED_WHEN_MANAGER_AND_DEVICE_SHARE_A_"
                 "COMPONENT_ID_AND_AN_ABSENT_EXTENSION_CANNOT_BE_TOLD_APART")
    else:
        basis = "AN_UNDOCUMENTED_CODE_OUTSIDE_THE_PINNED_0_TO_6_RANGE_RETAINED_WITH_NO_MEANING_ASSIGNED"
    row["gimbal_device_id_basis"] = basis


def _storage(fields, row, report, number):
    """The camera's own reported storage condition, its identity, type and name.

    A reported condition, never a physical cause: `EMPTY` is the source saying storage is missing,
    `NOT_SUPPORTED` is the source saying it supplies no storage status at all, and neither is a media
    health finding or evidence that a recording failed.
    """
    # `name` is free text written raw, so a name containing the delimiter splits into extra tokens.
    # One extra token makes the body odd and is already refused; two would pair up again and would
    # silently truncate the name to its first fragment. This writer prints exactly the struct's own
    # fields for this message, so any name outside that set means the row did not survive splitting.
    unexpected = sorted(set(fields) - STORAGE_WRITTEN_FIELDS)
    if unexpected:
        raise ValueError(f"row {number}: this writer prints only the storage struct's own fields; "
                         f"{unexpected} suggests a value containing the delimiter, such as a storage "
                         f"name, and the row is refused rather than mis-paired")
    status = _unsigned(fields["status"], "status", number, 0xFF)
    row["storage_status_code"] = status
    row["storage_status_reported"] = STORAGE_STATUS_NAMES.get(status, f"UNKNOWN_{status}")
    row["storage_status_basis"] = STORAGE_STATUS_BASIS
    row["storage_id_raw"] = _unsigned(fields["storage_id"], "storage_id", number, 0xFF)
    row["storage_count_raw"] = _unsigned(fields["storage_count"], "storage_count", number, 0xFF)
    row["storage_identity_basis"] = STORAGE_IDENTITY_BASIS
    kind = _unsigned(fields["type"], "type", number, 0xFF)
    row["storage_type_code"] = kind
    row["storage_type_reported"] = STORAGE_TYPE_NAMES.get(kind, f"UNKNOWN_{kind}")
    # `name` is the writer's rendering of a NULL-terminated char array: the characters up to the
    # first zero byte, as written. It is retained hex-encoded and never parsed or interpreted.
    name = fields["name"]
    row["storage_name_hex"] = "hex:" + name.encode().hex() if name else ""
    row["storage_name_basis"] = STORAGE_NAME_BASIS if name else STORAGE_EMPTY_NAME_BASIS
    if "storage_usage" in fields:
        row["storage_usage_raw"] = _unsigned(fields["storage_usage"], "storage_usage", number, 0xFF)
        row["storage_usage_availability"] = STORAGE_USAGE_PRESENT
    else:
        row["storage_usage_raw"] = ""
        row["storage_usage_availability"] = STORAGE_USAGE_ABSENT
    row["storage_capacity_unit_basis"] = STORAGE_UNIT_BASIS
    row["storage_speed_basis"] = STORAGE_SPEED_BASIS
    return status


def _storage_capacity(fields, row, report, status):
    """Apply the source's own validity rule to the three capacities, keeping every written token.

    The dialect says of each capacity: "If storage is not ready (STORAGE_STATUS_READY) value will be
    ignored." So the quantity is emitted only under READY; otherwise it is withheld and the token the
    file holds is retained beside it. A negative capacity has no defined meaning, so it is withheld
    rather than clamped.
    """
    ready = status == STORAGE_READY
    validity = STORAGE_READY_VALIDITY if ready else STORAGE_IGNORE_VALIDITY
    for name, column, raw_column in STORAGE_CAPACITIES:
        row[raw_column] = fields[name]
        if not ready:
            row[column] = ""
            report["withheld_values"] += 1
            continue
        if row[column] != "" and float(row[column]) < 0:
            row[column] = ""
            validity = STORAGE_NEGATIVE_VALIDITY
            report["withheld_values"] += 1
    row["storage_capacity_validity"] = validity


def convert(text, selected_message, *, decimal_separator="dot", capture_time_us=None,
            source_format=SOURCE_FORMAT):
    if selected_message not in SELECTED:
        raise ValueError("explicit supported selected message required")
    if source_format not in SOURCE_FORMATS:
        # Only the two source-compared releases are accepted; anything else is refused, never assumed
        # to share this writer.
        raise ValueError("explicit supported source format required")
    exporter_pin = SOURCE_FORMATS[source_format]
    allowed = SELECTION_RELEASES.get(selected_message)
    if allowed is not None and source_format not in allowed:
        raise ValueError(f"{selected_message} was read in the generated struct of {allowed} only; "
                         f"another release declares a different field set for it")
    if decimal_separator != "dot":
        # The writer passes no CultureInfo, so a comma-decimal export exists; it is declared, not guessed.
        raise ValueError("only a dot decimal separator export is supported; declare the export culture")
    clock_field, table, expected_msgid = SELECTED[selected_message]
    if clock_field is None:
        if type(capture_time_us) is not int or not 0 <= capture_time_us < 2 ** 63:
            raise ValueError("this selection has no message clock; an explicit capture time is required")
    elif capture_time_us is not None:
        raise ValueError("this selection carries its own boot clock; a capture time must not override it")
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized export")
    output = io.StringIO(newline="")
    # Each selection records the dialect id its fields were actually read at.
    dialect_pin = DIALECT_PIN
    if table is GIMBAL:
        dialect_pin = GIMBAL_DIALECT_PIN
    elif table is STORAGE:
        dialect_pin = STORAGE_DIALECT_PIN
    if table is GIMBAL:
        names = GIMBAL_FIELDS
    elif table is STORAGE:
        names = STORAGE_FIELDS
    else:
        names = ATTITUDE_FIELDS if clock_field else STATUS_FIELDS
    writer = csv.DictWriter(output, names, lineterminator="\n")
    writer.writeheader()
    report = {"exporter_pin": exporter_pin, "dialect_pin": dialect_pin,
              "selected_message": selected_message, "source_rows": 0, "selected_rows": 0,
              "unselected_rows": 0, "withheld_values": 0,
              # The digest is of the text handed in, so it binds the file only if the caller read it
              # without newline translation. `main` reads bytes and re-checks this equality.
              "source_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
              "clock": "Unknown",
              "clock_basis": ("BOOT_MS_FROM_THE_SELECTED_MESSAGE_NOT_UTC" if clock_field
                              else "CALLER_DECLARED_CAPTURE_MICROSECONDS_NOT_UTC"),
              "exporter_local_time_basis": ("EXPORTER_LOCAL_TIME_TEXT_RETAINED_ONLY_MILLISECOND_RESOLUTION_"
                                            "ZONE_NOT_RECORDED_NEVER_CONVERTED_TO_UTC")}
    previous = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode()) > MAX_LINE:
            raise ValueError(f"row {number}: exceeds the line bound")
        tokens = _tokens(line, number)
        if len(tokens) < 11:
            raise ValueError(f"row {number}: shorter than the writer's timestamp, header and struct name")
        report["source_rows"] += 1
        if not LOCAL_TIME.fullmatch(tokens[0]):
            raise ValueError(f"row {number}: the writer's yyyy-MM-ddTHH:mm:ss.fff stamp is required")
        header = {}
        for index, name in enumerate(HEADER_FIELDS, 1):
            if not HEX_TOKEN.fullmatch(tokens[index]):
                raise ValueError(f"row {number}: header field {name} is not the writer's hex form")
            header[name] = int(tokens[index], 16)
            low, high = HEADER_BOUNDS[name]
            if not low <= header[name] <= high:
                raise ValueError(f"row {number}: header field {name} is outside its MAVLink domain")
        if tokens[9] != selected_message:
            report["unselected_rows"] += 1
            continue
        if header["msgid"] != expected_msgid:
            raise ValueError(f"row {number}: struct name and hex msgid disagree; the row is not "
                             f"{selected_message}")
        fields = _pairs(tokens, number)
        required = list(table) + list(REQUIRED_EXTRA.get(selected_message, ()))
        if clock_field:
            required.append(clock_field)
        missing = [name for name in required if name not in fields]
        if missing:
            raise ValueError(f"row {number}: selected message is missing {missing}")
        row = {"source_local_time_text": tokens[0],
               "source_msgid": header["msgid"], "source_sysid": header["sysid"],
               # One row's own bytes, excluding the CRLF the writer appends: this is per-row provenance,
               # not whole-file retention. The file-level digest in the report binds the actual bytes.
               "source_compid": header["compid"], "source_row_hex": "hex:" + line.encode().hex(),
               "selected_message": selected_message, "exporter_pin": exporter_pin,
               "dialect_pin": dialect_pin, "record_time_basis": report["clock_basis"]}
        if clock_field:
            token = fields[clock_field]
            if not UNSIGNED.fullmatch(token) or int(token) >= 2 ** 32:
                raise ValueError(f"row {number}: {clock_field} must be the dialect's uint32 milliseconds")
            row["record_time_us"] = int(token) * 1000
            if previous is not None and row["record_time_us"] < previous:
                raise ValueError(f"row {number}: decreasing boot clock; split exports explicitly")
            previous = row["record_time_us"]
        else:
            row["record_time_us"] = capture_time_us
            row["measurement_status"] = "REPORTED"
        if table is GIMBAL:
            row["measurement_status"] = "REPORTED"
            _gimbal(fields, row, report, number)
        storage_status = _storage(fields, row, report, number) if table is STORAGE else None
        for name, entry in table.items():
            token = fields[name]
            if len(entry) > 2:
                column, scale, sentinel, low, high = entry
                if not SIGNED.fullmatch(token):
                    raise ValueError(f"row {number}: {name} must be the dialect's integer form")
                value = int(token)
                if value == sentinel:
                    # The dialect documents this exact value as "not sent by autopilot".
                    row[column] = ""
                    row["measurement_status"] = "UNAVAILABLE_NOT_SENT_BY_AUTOPILOT"
                    report["withheld_values"] += 1
                    continue
                if not low <= value <= high:
                    # Outside the dialect's own type domain or documented quantity range. A negative
                    # current inside its int16 domain is a real sign convention and stays accepted.
                    raise ValueError(f"row {number}: {name}={value} is outside the dialect range "
                                     f"[{low}, {high}] excluding its {sentinel} sentinel")
                row[column] = value * scale
                continue
            column, scale = entry
            if name in NAN_IS_UNKNOWN and token == NAN_TOKEN:
                # This dialect field declares `invalid="NaN"`: the source is saying the quantity is
                # unknown. Withheld and named - never refused as malformed, and never read as zero.
                row[column] = ""
                row["measurement_status"] = "UNKNOWN_NOT_PROVIDED_BY_THE_SOURCE"
                report["withheld_values"] += 1
                continue
            if not DECIMAL.fullmatch(token):
                raise ValueError(f"row {number}: {name} is not a plain decimal under the declared culture")
            value = float(token)
            # The dialect field is a wire float32, so the written value must be finite and representable
            # there; `1e999` parses to inf and `1e39` overflows float32, and neither is writer-producible.
            if value != value or value in (float("inf"), float("-inf")) or abs(value) > FLOAT32_MAX:
                raise ValueError(f"row {number}: {name} is not a finite float32 quantity")
            try:
                single = struct.unpack("<f", struct.pack("<f", value))[0]
            except (OverflowError, struct.error) as error:
                raise ValueError(f"row {number}: {name} is not representable as the dialect's float32"
                                 ) from error
            if single != single or (single == 0.0 and value != 0.0):
                raise ValueError(f"row {number}: {name} underflows or overflows the dialect's float32")
            row[column] = value * scale
        if table is STORAGE:
            _storage_capacity(fields, row, report, storage_status)
        writer.writerow(row)
        report["selected_rows"] += 1
        if report["selected_rows"] > MAX_ROWS or output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted export exceeds bound")
    if not report["selected_rows"]:
        raise ValueError("no rows of the selected message")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-format", required=True, choices=tuple(SOURCE_FORMATS),
                        help="the release that wrote the export; it selects the recorded exporter pin")
    parser.add_argument("--selected-message", required=True, choices=tuple(SELECTED))
    parser.add_argument("--decimal-separator", required=True, choices=("dot", "comma"),
                        help="the exporting machine's numeric culture; only dot is supported")
    parser.add_argument("--capture-time-us", type=int,
                        help="required for a selection without a message clock")
    parser.add_argument("--report", type=Path,
                        help="write the conversion accounting as JSON to this new file")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("input exceeds bound")
        # This writer ends every row with CRLF. Reading as text would translate those newlines, so the
        # digest would bind a rewritten string instead of the file: read bytes and decode strictly.
        text = data.decode("utf-8")
        output, report = convert(text, args.selected_message,
                                 decimal_separator=args.decimal_separator,
                                 capture_time_us=args.capture_time_us,
                                 source_format=args.source_format)
        if report["source_sha256"] != "sha256:" + hashlib.sha256(data).hexdigest():
            raise ValueError("reported digest does not bind the input bytes")
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(output)
        # The accounting is the user's, not something to drop on the floor. It carries counts, pins,
        # clock basis and the input digest only - never a source record or a converted value.
        summary = json.dumps(report, sort_keys=True)
        if args.report is not None:
            with args.report.open("x", encoding="utf-8") as stream:
                stream.write(summary + "\n")
        print(summary, file=sys.stderr)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"GCS export conversion failed; no successful conversion claim: {error}\n")


if __name__ == "__main__":
    main()
