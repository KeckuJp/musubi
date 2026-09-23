#!/usr/bin/env python3
"""Offline recorded MODE CSV -> explicitly selected vehicle names, no commands."""
import argparse
import csv
import io
import math
from pathlib import Path
import re

# Protocol numbers, not modes enabled on the recorded hardware/build.
# Exact producer source references are in the integration case.
PLANE_MODES = {
    0: "MANUAL", 1: "CIRCLE", 2: "STABILIZE", 3: "TRAINING", 4: "ACRO",
    5: "FLY_BY_WIRE_A", 6: "FLY_BY_WIRE_B", 7: "CRUISE", 8: "AUTOTUNE",
    10: "AUTO", 11: "RTL", 12: "LOITER", 13: "TAKEOFF", 14: "AVOID_ADSB",
    15: "GUIDED", 16: "INITIALISING", 17: "QSTABILIZE", 18: "QHOVER",
    19: "QLOITER", 20: "QLAND", 21: "QRTL", 22: "QAUTOTUNE", 23: "QACRO",
    24: "THERMAL", 25: "LOITER_ALT_QLAND",
}
MODE_SCHEMAS = {
    "plane": PLANE_MODES,
    "plane-4.5.5": PLANE_MODES,
    "rover-4.5.5": {0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER",
        6: "FOLLOW", 7: "SIMPLE", 8: "DOCK", 9: "CIRCLE", 10: "AUTO", 11: "RTL",
        12: "SMART_RTL", 15: "GUIDED", 16: "INITIALISING"},
    "copter-4.5.5": {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
        5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP",
        15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
        20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW", 24: "ZIGZAG",
        25: "SYSTEMID", 26: "AUTOROTATE", 27: "AUTO_RTL", 28: "TURTLE"},
    "sub-4.5.7": {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED",
        7: "CIRCLE", 9: "SURFACE", 16: "POSHOLD", 19: "MANUAL", 20: "MOTOR_DETECT", 21: "SURFTRAK"},
}
REQUIRED = ("TimeUS", "Mode", "ModeNum", "Rsn")
# AP_Logger_Backend::Write_VER() copies `fwver.fw_string` -- not `fw_string_original` --
# into the FWS char[64]. AP_FWVersionDefine.h builds that member as
# `ACTIVE_FWSTR " (" GIT_VERSION ")"` when GIT_VERSION is defined, and ACTIVE_FWSTR is
# AP_CUSTOM_FIRMWARE_STRING whenever a vendor sets one in hwdef.dat, otherwise THISFIRMWARE.
# So the legitimate FWS forms are: the producer string, that string with a build
# annotation, and an arbitrary vendor string. All three are accepted below.
# The same writer does set `pkt.build_type = fwver.vehicle_type`, so VER *does* carry a
# vehicle-type byte in BU -- an APM_BUILD_DIRECTORY code, not a MAV_TYPE, a frame class, a
# product or a vendor. It survives lexically and is deliberately not decoded here. What
# this path establishes is a reported firmware family and version for the VER rows it read.
VER_REQUIRED = ("TimeUS", "Maj", "Min", "Pat", "FWS")
VER_FIELDS = ["reported_firmware_family", "reported_firmware_version",
              "reported_firmware_suffix", "reported_firmware_build_annotation",
              "reported_vehicle_identity_basis", "declared_vehicle_agreement",
              "reported_release_channel", "reported_release_channel_code",
              "reported_board_target_id"]
# Write_VER sets `fw_type: fwver.fw_type`, and AP_FWVersionDefine.h sets `.fw_type = FW_TYPE`,
# where each vehicle's version.h defines FW_TYPE as exactly one *named* FIRMWARE_VERSION_TYPE
# constant. Nothing in that writer compares fw_type against a range, so only the named codes are
# mapped here; an intermediate byte is not silently qualified into a neighbouring channel. The
# banded reading used for AUTOPILOT_VERSION's flight_sw_version low byte is a different field and
# is not imported. This is the publisher's own build declaration, not a certification.
RELEASE_CHANNELS = {0: "DEV", 64: "ALPHA", 128: "BETA", 192: "RC", 255: "OFFICIAL"}
# Write_VER assigns `_APJ_BOARD_ID` only inside `#ifdef APJ_BOARD_ID`, so an undefined macro
# leaves the zero initialiser -- but a build that defines it as zero produces the same byte, and
# which happened is not recoverable from the record. Zero is therefore reported without a cause.
BOARD_TARGET_ZERO = "ZERO_OR_NOT_PROVIDED"


def build_identity(values):
    """The two build-identity fields VER actually carries, or blanks when it does not.

    Absent columns stay blank rather than defaulted: an older writer layout simply did not
    declare them, and that is not the same as a build declaring DEV or board target zero.
    """
    if "FWT" in values:
        code = unsigned(values["FWT"], 255)
        channel = RELEASE_CHANNELS.get(code, f"UNKNOWN_{code}")
        reported = [channel, str(code)]
    else:
        reported = ["", ""]
    if "APJ" in values:
        target = unsigned(values["APJ"], 65535)
        reported.append(BOARD_TARGET_ZERO if target == 0 else str(target))
    else:
        reported.append("")
    return reported

VER_IDENTITY_BASIS = "FIRMWARE_FAMILY_ONLY_NOT_PRODUCT_OR_VENDOR_IDENTITY"
VER_CUSTOM_BASIS = "CUSTOM_FIRMWARE_STRING_NO_UPSTREAM_FAMILY_ESTABLISHED"
FIRMWARE_FAMILIES = {"ArduRover": "rover", "ArduSub": "sub",
                     "ArduPlane": "plane", "ArduCopter": "copter"}
# Anchored to the producer prefix so an occurrence embedded in other text is not accepted;
# a documented release suffix such as -beta1, -rc2 or -dev is preserved, not stripped, and
# so is the parenthesised build annotation the write site appends.
FIRMWARE_STRING = re.compile(
    r"(Ardu[A-Za-z]+) V([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})"
    r"([-+][0-9A-Za-z][0-9A-Za-z.\-]{0,31})?"
    r"(?: \(([0-9A-Za-z][0-9A-Za-z.\-]{0,39})\))?")
# A vendor string is legitimate but arbitrary, so it is bounded to the char[64] the writer
# declares and to printable ASCII the unquoted CSV contract can carry.
# Printable ASCII minus the comma and the double quote, which the unquoted contract cannot
# carry, and minus any control character.
CUSTOM_FIRMWARE_STRING = re.compile(r"[ !#-+\--~]{1,63}")
# A selector naming an exact release binds the version; a bare legacy name deliberately
# does not, and is then only ever corroborated at family level.
RELEASE_SELECTOR = re.compile(r"(rover|sub|copter|plane)-([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})")
ARM_REQUIRED = ("TimeUS", "ArmState", "ArmChecks", "Forced", "Method")
ARM_FIELDS = ["reported_arm_state", "reported_arm_forced", "reported_arm_checks_mask", "reported_arm_method_code"]
ARM_SCHEMAS = {"plane", "plane-4.5.5", "rover-4.5.5", "copter-4.5.5", "sub-4.5.7"}
PARAM_SCHEMAS = {"parm-4.2.3": ("TimeUS", "Name", "Value"),
                 "parm-4.3.1": ("TimeUS", "Name", "Value", "Default")}
PARAM_VEHICLES = {schema: "plane" for schema in PARAM_SCHEMAS}
for _vehicle in ("plane-4.5.5", "rover-4.5.5", "copter-4.5.5", "sub-4.5.7"):
    PARAM_SCHEMAS["parm-" + _vehicle] = PARAM_SCHEMAS["parm-4.3.1"]
    PARAM_VEHICLES["parm-" + _vehicle] = _vehicle
PM_COMMON = ("MaxT", "Mem", "Load", "ErrL", "IntE", "ErrC", "SPIC", "I2CC", "I2CI", "Ex")
PM_SCHEMAS = {"pm-4.3.1": ("TimeUS", "NLon", "NLoop", *PM_COMMON),
              "pm-4.3.2": ("TimeUS", "LR", "NLon", "NL", *PM_COMMON)}
PM_FIELDS = ["reported_pm_schema", "reported_pm_long_loops", "reported_pm_loops",
             "reported_pm_loop_rate_hz", "reported_pm_max_loop_s", "reported_pm_memory_bytes",
             "reported_pm_scheduler_load", "reported_pm_extra_loop_s", "reported_pm_load_basis"]
# Exact source-qualified exports; no claim about every intervening build.
for _schema in ("rover-4.4.0", "rover-4.5.0"):
    MODE_SCHEMAS[_schema] = MODE_SCHEMAS["rover-4.5.5"]
for _schema in ("copter-4.4.0", "copter-4.5.0"):
    MODE_SCHEMAS[_schema] = MODE_SCHEMAS["copter-4.5.5"]
    ARM_SCHEMAS.add(_schema)
# Marine upstream families. Rover/mode.h at Rover-4.5.7
# 52bed8d542003c4767fea21b0f5a78fe864a377f and ArduSub/mode.h at Sub-4.7.1
# dbe792162d06cab66c3475fd5556bf7a120f119e were read and carry exactly the tables already
# adopted for rover-4.5.5 and sub-4.5.7, so these are verified aliases, not new meanings.
MODE_SCHEMAS["rover-4.5.7"] = MODE_SCHEMAS["rover-4.5.5"]
MODE_SCHEMAS["sub-4.7.1"] = MODE_SCHEMAS["sub-4.5.7"]
# ARM compatibility does not follow from mode.h, so it rests on its own evidence:
# AP_Logger/LogStructure.h at both pins declares ARM as "QBIBB" with fields
# TimeUS,ArmState,ArmChecks,Forced,Method and struct log_Arm_Disarm carrying
# uint64 time_us, uint8 arm_state, uint32 arm_checks, uint8 forced, uint8 method -- the
# same writer at both. PARM and PM are deliberately NOT extended to these selectors,
# because no parameter or performance writer evidence was read at these pins.
ARM_SCHEMAS.update({"rover-4.5.7", "sub-4.7.1"})
# The six ArduRover 4.5 release points below share one table because their selected writer
# files are byte-identical, not because they share a filename. Fetched by commit SHA:
#   4.5.0 53ad2c2a2c7e7ae220cd140013510459c4c2e6a4  4.5.3 548b0b2c3260cd0cff42e2093537066431a3620b
#   4.5.1 71a2f169b7c5a99c15ccc4f8985b3e4d641913a1  4.5.4 fe4831dd2ecdf9c68769c629566bf9833ec55f54
#   4.5.2 291be84828041815142e31374de15ba5bcf1bba0  4.5.5 9064e22695992593b12e71a2b2ad25faba2f7277
# At all six, Rover/mode.h is 33947 bytes sha256 2df7e74aae0f7fbf93b3e5f8b666d2759113a3504e41fa92f1efd5a2c3af95b3
# (the same 14 protocol numbers already adopted) and libraries/AP_Logger/LogStructure.h is 47880 bytes
# sha256 df93d79e70d806758067e12289cfe6884a2503f4e2799b0531b29f20e29e2c1a, declaring MODE "QMBB"
# TimeUS,Mode,ModeNum,Rsn, ARM "QBIBB" TimeUS,ArmState,ArmChecks,Forced,Method and PARM "QNff"
# TimeUS,Name,Value,Default -- so MODE, ARM and PARM carry one meaning across these exact points.
# Each selector stays an exact release string and is echoed into the output; there is no version
# wildcard, and no other vehicle or Rover release is implied. DOCK 8 is enclosed by
# MODE_DOCK_ENABLED, so a protocol number is still not proof that a build enabled that mode, and PM
# stays unqualified here because no performance writer evidence was read at these pins.
ROVER_45_RELEASES = ("4.5.0", "4.5.1", "4.5.2", "4.5.3", "4.5.4", "4.5.5")
for _patch in ROVER_45_RELEASES:
    _schema = "rover-" + _patch
    MODE_SCHEMAS[_schema] = MODE_SCHEMAS["rover-4.5.5"]
    ARM_SCHEMAS.add(_schema)
    PARAM_SCHEMAS["parm-" + _schema] = PARAM_SCHEMAS["parm-4.3.1"]
    PARAM_VEHICLES["parm-" + _schema] = _schema
# The six ArduCopter 4.5 release points, on the same basis: byte identity of the selected
# files, never a shared filename or a shared vehicle. Fetched by commit SHA:
#   4.5.0 53ad2c2a2c7e7ae220cd140013510459c4c2e6a4  4.5.3 ee14ff3822161368ec403700bf96d02b84def06c
#   4.5.1 71a2f169b7c5a99c15ccc4f8985b3e4d641913a1  4.5.4 fd1bcc6182bf7527643ca1a4648c3b68ca016ce4
#   4.5.2 291be84828041815142e31374de15ba5bcf1bba0  4.5.5 142aece2da56146d94dcad0f6b539af6960538f6
# The first three are the very commits already pinned above for Rover-4.5.0/1/2 -- one commit
# releases several vehicles -- so the shared-library evidence is the same bytes, not a re-reading:
# libraries/AP_Logger/LogStructure.h is 47880 bytes sha256 df93d79e...29e2c1a at all six and
# libraries/AP_Logger/LogFile.cpp (Write_Mode, Write_Parameter) is 19377 bytes sha256
# 5b13f941...c8e67f98de4. ArduCopter/mode.h is 68981 bytes sha256
# b9ca890b3963ba63fd00dcbb62d881363e2d44126ee30135755c4f064d72263c at all six and carries exactly
# the 26 protocol numbers adopted for copter-4.5.5; ArduCopter/mode.cpp (38609 bytes sha256
# c7fb5d1b...406a0e0b) and ArduCopter/AP_Arming.cpp (28991 bytes sha256 958c51e9...09216d529) are
# likewise one content each. The Copter arm/disarm overrides delegate to AP_Arming::arm/disarm,
# which is where Log_Write_Arm/Log_Write_Disarm run, so ARM rests on the base writer already
# qualified: libraries/AP_Arming/AP_Arming.cpp has three contents here (c1c3b896... at 4.5.0-4.5.1,
# 5a5edc42... at 4.5.2-4.5.4, 08b1e4ec... at 4.5.5 -- the same three blobs pinned for Rover, whose
# selected functions are byte-identical across them).
# Copter mode numbers are Copter's own: 0 is STABILIZE here and MANUAL on Rover, and no label is
# carried between vehicles. Two Copter-only ways a MODE row appears without a mode change, both
# read in the source above: AP_Arming_Copter::arm repeats the current mode/reason on every
# successful arm, and ModeAuto logs AUTO_RTL entry and exit. As always a protocol number is not
# proof a build enabled that mode -- the MODE_*_ENABLED gates sit around the mode classes and
# inside set_mode -- and PM stays unqualified because no performance writer evidence was read.
COPTER_45_RELEASES = ("4.5.0", "4.5.1", "4.5.2", "4.5.3", "4.5.4", "4.5.5")
for _patch in COPTER_45_RELEASES:
    _schema = "copter-" + _patch
    MODE_SCHEMAS[_schema] = MODE_SCHEMAS["copter-4.5.5"]
    ARM_SCHEMAS.add(_schema)
    PARAM_SCHEMAS["parm-" + _schema] = PARAM_SCHEMAS["parm-4.3.1"]
    PARAM_VEHICLES["parm-" + _schema] = _schema


def unsigned(value, maximum):
    if not re.fullmatch(r"[0-9]+", value) or int(value) > maximum:
        raise ValueError("invalid MODE integer")
    return int(value)


def convert_parameter_snapshot(text, capture_time_us):
    """Read a fixed saved parameter set, never load it onto a vehicle."""
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    if len(text.encode("utf-8")) > 256000:
        raise ValueError("parameter snapshot too large")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "parameter_name", "reported_parameter_value",
                     "parameter_unit_basis", "parameter_time_basis", "source_line",
                     "source_value_hex", "source_document_hex"])
    names = set()
    for line_number, original in enumerate(text.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", parts[0]):
            raise ValueError("invalid selected saved-parameter syntax")
        name, value = parts
        if name in names:
            raise ValueError("duplicate parameter name is not a snapshot")
        if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]{6})?", value):
            raise ValueError("finite numeric saved parameter required")
        number = float(value)
        if not math.isfinite(number) or abs(number) > 3.4028234663852886e38:
            raise ValueError("saved parameter outside finite float32 range")
        writer.writerow([capture_time_us, name, value, "NATIVE_PARAMETER_UNIT_UNDECLARED",
                         "CALLER_CAPTURE_NOT_DEVICE_OR_FILE_TIME", line_number,
                         "hex:" + value.encode().hex(), "hex:" + text.encode().hex() if not names else ""])
        names.add(name)
    if not names:
        raise ValueError("empty parameter set")
    return output.getvalue()


def convert(text: str, vehicle: str, record: str = "mode", *, preserve_nonfinite_default=False) -> str:
    parameter = record in PARAM_SCHEMAS
    performance = record in PM_SCHEMAS
    if type(preserve_nonfinite_default) is not bool or (preserve_nonfinite_default and
            (not parameter or "Default" not in PARAM_SCHEMAS[record])):
        raise ValueError("nonfinite default requires an explicit QNff parameter record")
    if ((record not in ("mode", "arm", "ver") and not parameter and not performance)
            or (parameter and vehicle != PARAM_VEHICLES[record])
            or (performance and vehicle != "plane")
            or (record == "arm" and vehicle not in ARM_SCHEMAS)):
        raise ValueError("unsupported recorded state schema")
    required = (PARAM_SCHEMAS[record] if parameter else PM_SCHEMAS[record] if performance
                else REQUIRED if record == "mode" else VER_REQUIRED if record == "ver"
                else ARM_REQUIRED)
    if vehicle not in MODE_SCHEMAS:
        raise ValueError("explicit supported vehicle required")
    modes = MODE_SCHEMAS[vehicle]
    if '"' in text or any(ord(c) < 32 and c not in '\r\n' for c in text):
        raise ValueError("unsupported CSV representation")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("missing MODE header or records")
    header = rows[0]
    if (len(set(header)) != len(header) or not set(required).issubset(header)
            or any(not c or c != c.strip() or '(' in c or ')' in c
                   or c.startswith(("reported_mode_", "reported_arm_", "reported_parameter_", "reported_pm_",
                                     "reported_firmware_", "reported_vehicle_", "declared_vehicle_")) for c in header)
            or (record == "parm-4.2.3" and "Default" in header)
            or (record == "pm-4.3.1" and ("LR" in header or "NL" in header))
            or (record == "pm-4.3.2" and "NLoop" in header)):
        raise ValueError("incompatible MODE header")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    appended = ARM_FIELDS if record == "arm" else ["reported_mode_name", "reported_mode_known"] + ([] if vehicle == "plane" else ["reported_mode_schema"])
    if record == "arm" and vehicle != "plane":
        appended = appended + ["reported_arm_schema"]
    if parameter:
        appended = ["reported_parameter_schema", "reported_parameter_default_present"]
        if preserve_nonfinite_default:
            appended += ["reported_parameter_default_state"]
    if performance:
        appended = PM_FIELDS
    if record == "ver":
        appended = VER_FIELDS
    writer.writerow(header + appended)
    previous = None
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError("ragged or blank MODE row")
        values = dict(zip(header, row))
        time = unsigned(values["TimeUS"], 2**63 - 1)
        if previous is not None and time < previous:
            raise ValueError("decreasing MODE boot time")
        previous = time
        if performance:
            parsed = {key: unsigned(values[key], 65535 if key in ("LR", "NLon", "NLoop", "NL", "Load", "ErrL")
                                    else 2**32 - 1) for key in required[1:]}
            writer.writerow(row + [record, parsed["NLon"], parsed["NLoop" if record == "pm-4.3.1" else "NL"],
                parsed.get("LR", ""), parsed["MaxT"] / 1e6, parsed["Mem"], parsed["Load"] / 1000,
                parsed["Ex"] / 1e6, "SPARE_TIME_REPORTED" if record == "pm-4.3.1" else "LOOP_RATE_OR_SPARE_TIME_REPORTED"])
        elif parameter:
            name = values["Name"]
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", name):
                raise ValueError("invalid recorded parameter name")
            for key in required[2:]:
                value = values[key]
                if preserve_nonfinite_default and key == "Default" and value in ("NaN", "Infinity", "-Infinity"):
                    continue
                if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
                    raise ValueError("parameter value requires explicit finite numeric export")
                number = float(value)
                if not math.isfinite(number) or abs(number) > 3.4028234663852886e38:
                    raise ValueError("parameter value exceeds finite float32 range")
            writer.writerow(row + [record, "true" if "Default" in required else "false"] + (
                ["NONFINITE_UNUSABLE" if values["Default"] in ("NaN", "Infinity", "-Infinity") else "FINITE_REPORTED"]
                if preserve_nonfinite_default else []))
        elif record == "ver":
            major, minor, patch = (unsigned(values[key], 255) for key in ("Maj", "Min", "Pat"))
            reported = values["FWS"]
            match = FIRMWARE_STRING.fullmatch(reported)
            release = RELEASE_SELECTOR.fullmatch(vehicle)
            declared = release.group(1) if release else vehicle.split("-")[0]
            if not match:
                # A vendor build legitimately replaces the whole string, so this is retained
                # rather than refused -- and it establishes no family, so it cross-checks the
                # caller's declaration against nothing. Maj/Min/Pat are separate numeric
                # fields at the write site, so they are still reported.
                if not CUSTOM_FIRMWARE_STRING.fullmatch(reported):
                    raise ValueError("VER firmware string is not a writable char[64] value")
                writer.writerow(row + ["", f"{major}.{minor}.{patch}", "", "",
                                       VER_CUSTOM_BASIS,
                                       "CUSTOM_FIRMWARE_STRING_NO_FAMILY_ESTABLISHED_RETAINED"]
                                + build_identity(values))
                continue
            logged = (int(match.group(2)), int(match.group(3)), int(match.group(4)))
            if logged != (major, minor, patch):
                raise ValueError("VER firmware string contradicts its own version fields")
            family = FIRMWARE_FAMILIES.get(match.group(1))
            # A pre-release suffix is legitimate and is preserved, but it means the string is
            # not the pinned stable release even when the numbers match. The build annotation
            # is a separate thing: it names a commit this converter cannot resolve.
            suffix = match.group(5) or ""
            build = match.group(6) or ""
            if family is None:
                # Unknown upstream family is retained, never guessed into a known one.
                agreement = "UNKNOWN_FIRMWARE_FAMILY_RETAINED"
            elif family != declared:
                raise ValueError("declared vehicle contradicts the logged firmware family")
            elif release is None:
                # A legacy selector binds no release, so this is family-level only.
                agreement = "AGREES_WITH_DECLARED_FAMILY_ONLY_NO_RELEASE_BOUND"
            elif tuple(int(release.group(index)) for index in (2, 3, 4)) != logged:
                raise ValueError("declared release contradicts the logged firmware version")
            elif suffix:
                agreement = "AGREES_ON_NUMERIC_BASE_UNQUALIFIED_SUFFIX_NOT_EXACT_STABLE"
            elif build:
                # The numbers are the declared release, but the annotated commit is not
                # resolved here, so this is not a claim about which build ran.
                agreement = "AGREES_WITH_DECLARED_RELEASE_BUILD_COMMIT_NOT_CHECKED"
            else:
                agreement = "AGREES_WITH_DECLARED_RELEASE"
            writer.writerow(row + [match.group(1), f"{major}.{minor}.{patch}", suffix, build,
                                   VER_IDENTITY_BASIS, agreement] + build_identity(values))
        elif record == "arm":
            state = unsigned(values["ArmState"], 1)
            forced = unsigned(values["Forced"], 1)
            checks = unsigned(values["ArmChecks"], 2**32 - 1)
            method = unsigned(values["Method"], 255)
            writer.writerow(row + ["ARMED_REPORTED" if state else "DISARMED_REPORTED",
                                   "true" if forced else "false", checks, method]
                            + ([] if vehicle == "plane" else [vehicle]))
        else:
            mode, alias, _reason = (unsigned(values[k], 255) for k in REQUIRED[1:])
            if mode != alias:
                raise ValueError("MODE aliases disagree")
            writer.writerow(row + [modes.get(mode, f"UNKNOWN_{mode}"),
                                   "true" if mode in modes else "false"]
                            + ([] if vehicle == "plane" else [vehicle]))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vehicle", choices=list(MODE_SCHEMAS), required=True)
    parser.add_argument("--record", choices=("mode", "arm", "ver", *PARAM_SCHEMAS, *PM_SCHEMAS, "parameter-file-2.4.49"), default="mode")
    parser.add_argument("--capture-time-us", type=int, help="explicit local capture clock for untimed parameter file only")
    parser.add_argument("--preserve-nonfinite-default", action="store_true")
    args = parser.parse_args()
    try:
        with args.input.open(encoding="utf-8", newline="") as source:
            text = source.read(256001) if args.record == "parameter-file-2.4.49" else source.read()
        if args.record == "parameter-file-2.4.49":
            if args.preserve_nonfinite_default:
                raise ValueError("snapshot has no reported default field")
            converted = convert_parameter_snapshot(text, args.capture_time_us)
        else:
            if args.capture_time_us is not None:
                raise ValueError("capture override is not valid for timed records")
            converted = convert(text, args.vehicle, args.record, preserve_nonfinite_default=args.preserve_nonfinite_default)
        with args.output.open("x", encoding="utf-8", newline="") as stream:
            stream.write(converted)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"MODE conversion failed: {exc}\n")


if __name__ == "__main__":
    main()
