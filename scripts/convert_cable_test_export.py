#!/usr/bin/env python3
"""Passive conversion of a saved `ethtool --json --cable-test` export.

The adopted estate carries link counters and CRC results, and none of them is evidence that a cable
is damaged. This reads something different: the result a PHY reported when it was asked to test the
cable, saved to a file by ethtool. No device is contacted here and no ethtool command is run -- the
input is a file that already exists.

What the saved export does and does not contain is taken from the writer's own source at a pinned
commit: the pair text, the code text, and a fault length the writer divided into metres. It carries
no interface name, no timestamp, no accuracy and no connector entity, so this converter asks the
caller for the first two, states the third as not given, and never claims the fourth.
"""
import argparse
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import sys

LIMIT = 4 * 1024 * 1024
OUTPUT_LIMIT = 16 * 1024 * 1024
ENTRY_LIMIT = 4096
NAME_LIMIT = 64
# ethtool v6.11, commit c0ea4b70c71334ef038f7a3416b228a50dada406, fetched by id. The JSON shape is
# `netlink/cable_test.c` (`open_json_object` per entry, keys "pair"/"code"/"length") inside the
# top-level array `json_print.c` `new_json_obj` opens; the length is `(float)cm / 100` rendered by
# `json_writer.c` `jsonw_float` with "%g".
SOURCE_PIN = "ethtool@c0ea4b70c71334ef038f7a3416b228a50dada406"
# The four pair words that writer emits; anything else is its own "Unexpected pair" fallback.
NAMED_PAIRS = ("Pair A", "Pair B", "Pair C", "Pair D")
# The four codes it names. Its default arm prints "Unknown", which is why that word is ambiguous:
# the same build's header also defines IMPEDANCE_MISMATCH, and the kernel line has NOISE and
# RESOLUTION_NOT_POSSIBLE besides, none of which this writer has a word for.
NAMED_CODES = ("OK", "Open Circuit", "Short within Pair", "Short to another pair")
# The writer's own two fallback words. Anything else in these fields is a string this pinned writer
# does not emit at all, and it is preserved as written rather than given a meaning it never had.
FALLBACK_CODE = "Unknown"
FALLBACK_PAIR = "Unexpected pair"
UNRECOGNISED = ("A_REPORTED_STRING_THIS_PINNED_WRITER_DOES_NOT_EMIT_PRESERVED_AS_WRITTEN_WITH_NO_"
                "MEANING_ASSIGNED_AND_NOT_READ_AS_THE_WRITERS_FALLBACK_WORD")
CABLE_TEST_BASIS = (
    "A_RESULT_THE_PHY_REPORTED_WHEN_IT_WAS_ASKED_TO_TEST_THE_CABLE_SAVED_BY_ETHTOOL_AND_READ_FROM_"
    "THAT_FILE_HERE_IT_IS_NOT_DERIVED_FROM_TRAFFIC_COUNTERS_CRC_RESULTS_OR_LINK_STATE_AND_IT_IS_NOT_"
    "A_CONNECTOR_FINDING_BECAUSE_THIS_SOURCE_REPORTS_NO_CONNECTOR_ENTITY_AT_ALL_PAIR_A_TO_D_ARE_THE_"
    "WRITERS_OWN_WORDS_WITH_NO_DOCUMENTED_MAPPING_TO_A_PHYSICAL_CONDUCTOR_SO_NONE_IS_ASSIGNED_THE_"
    "EXPORT_CARRIES_NO_INTERFACE_NAME_NO_TIMESTAMP_AND_NO_ACCURACY_SO_THE_FIRST_TWO_ARE_THE_CALLERS_"
    "AND_THE_THIRD_IS_STATED_AS_NOT_GIVEN")
CLOCK_BASIS = "CALLER_DECLARED_CAPTURE_CLOCK_NOT_A_TEST_TIMESTAMP_THE_EXPORT_CARRIES_NONE"
LENGTH_BASIS = (
    "THE_KERNELS_OWN_UNIT_IS_CENTIMETRES_AND_THIS_WRITER_DIVIDED_IT_BY_ONE_HUNDRED_INTO_A_SINGLE_"
    "PRECISION_FLOAT_RENDERED_AT_SIX_SIGNIFICANT_DIGITS_SO_THE_METRE_TOKEN_IS_WHAT_THE_FILE_HOLDS_"
    "AND_THE_CENTIMETRE_FIGURE_IS_EXACT_ARITHMETIC_ON_THAT_TOKEN_ALONE_NEVER_A_CLAIM_ABOUT_WHAT_THE_"
    "PHY_MEASURED")
# `ETHTOOL_A_CABLE_FAULT_LENGTH_CM` is a u32 of centimetres, so the quantity cannot be negative and
# cannot exceed 4294967295 cm = 42949672.95 m. The writer prints metres through a single-precision
# float at six significant digits, which can round that maximum *up*, so the largest token it can
# produce for it is 4.29497e+07. Outside [0, that] the file is not something this writer wrote.
MAXIMUM_PRINTED_METRES = Decimal("42949700")
LENGTH_RANGE = ("0_TO_42949700_METRES_BECAUSE_THE_SOURCE_FIELD_IS_AN_UNSIGNED_32_BIT_COUNT_OF_"
                "CENTIMETRES_AND_SIX_SIGNIFICANT_DIGIT_ROUNDING_CAN_RAISE_ITS_MAXIMUM")
# The same unsigned field makes one centimetre the smallest positive quantity that exists, and the
# pinned printer renders it as `%g` of `(float)1 / 100`, which is `0.01`. Nothing strictly between
# zero and 0.01 is therefore an output of this writer. That gap is load-bearing rather than
# cosmetic: ordinary numeric decoding turns a sub-centimetre positive such as `1e-999` into 0.0
# without saying so, and the row would then carry an exact zero in `reported_fault_length_m` -- a
# measurement the file never contained. It is refused here instead, as a whole entry.
MINIMUM_PRINTED_METRES = Decimal("0.01")
LENGTH_FLOOR = ("A_POSITIVE_LENGTH_BELOW_ZERO_POINT_ZERO_ONE_METRES_IS_NOT_THIS_WRITERS_OUTPUT_"
                "BECAUSE_ONE_CENTIMETRE_IS_THE_SMALLEST_POSITIVE_QUANTITY_THE_UNSIGNED_SOURCE_FIELD_"
                "CAN_HOLD_AND_IT_PRINTS_AS_ZERO_POINT_ZERO_ONE_SO_A_SMALLER_POSITIVE_TOKEN_MUST_NOT_"
                "DECODE_SILENTLY_INTO_AN_EXACT_ZERO_MEASUREMENT")
# This converter names itself, in the musubi.<name>/vN form this repository already uses.
APPLIED_CONVERTER_ID = "musubi.cable-test-export/v1"
APPLIED_CONVERTER_REVISION = "1"
FIELDS = ["record_time_us", "capture_clock_basis", "declared_interface_hex", "entry_index",
          "entry_kind", "reported_pair_hex", "pair_disposition", "reported_result_code_hex",
          "result_code_disposition", "reported_fault_length_m", "reported_fault_length_cm",
          "fault_length_disposition", "measurement_uncertainty", "source_entry_hex",
          "cable_test_basis", "fault_length_basis", "source_pin", "applied_converter_id",
          "applied_converter_revision"]


def text(value):
    return "hex:" + value.encode("utf-8").hex()


def centimetres(token):
    """Whole centimetres recovered from the printed metres, or nothing rather than a rounded guess."""
    if not re.fullmatch(r"[+-]?[0-9]{1,12}(\.[0-9]{1,12})?([eE][+-]?[0-9]{1,3})?", token):
        raise ValueError("a saved fault length is not a plain decimal this converter will read")
    try:
        metres = Decimal(token)
    except InvalidOperation:
        raise ValueError("a saved fault length is not a decimal number")
    if not metres.is_finite():
        raise ValueError("a saved fault length is not finite")
    if metres < 0 or metres > MAXIMUM_PRINTED_METRES:
        # A negative or unrepresentable length cannot have come from an unsigned centimetre count,
        # so it is refused here rather than carried into common output as a physical figure.
        raise ValueError(f"a saved fault length is outside the range this writer can print: "
                         f"{LENGTH_RANGE}")
    if 0 < metres < MINIMUM_PRINTED_METRES:
        # Exact zero is a legitimate reported length and is untouched; this refuses only the
        # positive values below the printer's smallest one, which would otherwise be read as zero.
        raise ValueError(f"a saved fault length is below the smallest this writer can print: "
                         f"{LENGTH_FLOOR}")
    exact = metres * 100
    if exact != exact.to_integral_value():
        return "", ("PRINTED_METRES_DO_NOT_LAND_ON_A_WHOLE_CENTIMETRE_SO_NONE_IS_CLAIMED")
    return (int(exact), "WHOLE_CENTIMETRES_RECOVERED_FROM_THE_PRINTED_METRES")


def retained(pair, key, rendered):
    """The saved entry re-serialised from its two known fields, with the length still a number.

    The shape is finite -- a pair and either a code or a length -- so each field is serialised on
    its own. Nothing is substituted into the finished text, because a reported string that happened
    to equal a placeholder would then be rewritten into something the file never said.
    """
    return '{"pair":%s,"%s":%s}' % (json.dumps(pair, ensure_ascii=False), key, rendered)


def unique_object(pairs):
    """`json.loads` keeps the last of duplicate keys; that silently discards a reported value."""
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"a saved entry repeats the key {key!r}; this writer emits each once")
        seen[key] = value
    return seen


def refuse_constant(name):
    raise ValueError(f"the saved export carries the non-numeric constant {name}")


def entries(data):
    """The saved array exactly as written; a shape this writer never produces is refused."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized saved export")
    try:
        # The exact token matters for the length, so a float is built from the literal the file
        # holds rather than through binary floating point. Decimal also keeps numbers and strings
        # distinguishable, so a quoted "2.1" is not mistaken for the number 2.1.
        document = json.loads(data.decode("utf-8-sig"), parse_float=Decimal,
                              parse_constant=refuse_constant, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise ValueError(f"the saved export is not readable JSON: {failure}")
    if not isinstance(document, list):
        raise ValueError("this writer emits a top-level JSON array; that is not what this file holds")
    if not document:
        raise ValueError("the saved export carries no entries")
    if len(document) > ENTRY_LIMIT:
        raise ValueError("the saved export exceeds the entry bound")
    return document


def convert(data, *, interface, capture_time_us):
    if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,%d}" % NAME_LIMIT,
                                                          interface):
        # The export names no interface, so the caller must, and it is carried as their declaration.
        raise ValueError("a declared interface name is required; the export carries none")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("a declared capture clock in microseconds is required")
    document = entries(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    report = {"saved_entries": len(document), "result_entries": 0, "fault_length_entries": 0,
              "entries_with_the_writers_fallback_code": 0, "entries_with_an_unrecognised_code": 0,
              "entries_with_the_writers_fallback_pair": 0, "entries_with_an_unrecognised_pair": 0,
              "whole_centimetre_recoveries": 0, "declared_interface": interface,
              "declared_capture_time_us": capture_time_us, "source_pin": SOURCE_PIN,
              "source_sha256": digest, "clock": "Unknown",
              "applied_converter_id": APPLIED_CONVERTER_ID,
              "applied_converter_revision": APPLIED_CONVERTER_REVISION,
              "connector_findings": "NOT_REPORTED_BY_THIS_SOURCE",
              "physical_damage_records": "NOT_DERIVED"}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n", restval="")
    writer.writeheader()
    shared = {"record_time_us": capture_time_us, "capture_clock_basis": CLOCK_BASIS,
              "declared_interface_hex": text(interface), "cable_test_basis": CABLE_TEST_BASIS,
              "measurement_uncertainty": "NOT_STATED_BY_THE_SOURCE", "source_pin": SOURCE_PIN,
              "applied_converter_id": APPLIED_CONVERTER_ID,
              "applied_converter_revision": APPLIED_CONVERTER_REVISION}
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise ValueError("every entry this writer emits is a JSON object")
        keys = set(entry)
        if keys not in ({"pair", "code"}, {"pair", "length"}):
            # One open_json_object per branch, so an entry is pair+code or pair+length and never
            # both, neither, or anything else. A different shape is a different file.
            raise ValueError("an entry is not the pair/code or pair/length shape this writer emits")
        pair = entry["pair"]
        if not isinstance(pair, str) or not 1 <= len(pair) <= NAME_LIMIT:
            raise ValueError("a saved pair is not a bounded string")
        if pair in NAMED_PAIRS:
            pair_disposition = "NAMED_BY_THE_PINNED_WRITER"
        elif pair == FALLBACK_PAIR:
            pair_disposition = ("THE_WRITERS_OWN_FALLBACK_FOR_A_PAIR_NUMBER_IT_HAS_NO_WORD_FOR")
            report["entries_with_the_writers_fallback_pair"] += 1
        else:
            pair_disposition = UNRECOGNISED
            report["entries_with_an_unrecognised_pair"] += 1
        row = dict(shared, entry_index=index, reported_pair_hex=text(pair),
                   pair_disposition=pair_disposition)
        if "code" in keys:
            code = entry["code"]
            if not isinstance(code, str) or not 1 <= len(code) <= NAME_LIMIT:
                raise ValueError("a saved result code is not a bounded string")
            if code in NAMED_CODES:
                code_disposition = "NAMED_BY_THE_PINNED_WRITER"
            elif code == FALLBACK_CODE:
                # Only this literal word is the writer's default arm.
                code_disposition = ("THE_WRITERS_FALLBACK_WORD_WHICH_COVERS_ITS_UNSPEC_ARM_AND_"
                                    "EVERY_KERNEL_CODE_IT_HAS_NO_WORD_FOR_INCLUDING_IMPEDANCE_"
                                    "MISMATCH_SO_IT_NAMES_NO_SINGLE_CONDITION")
                report["entries_with_the_writers_fallback_code"] += 1
            else:
                code_disposition = UNRECOGNISED
                report["entries_with_an_unrecognised_code"] += 1
            report["result_entries"] += 1
            writer.writerow(dict(row, entry_kind="REPORTED_RESULT",
                                 reported_result_code_hex=text(code),
                                 result_code_disposition=code_disposition,
                                 source_entry_hex=text(retained(
                                     pair, "code", json.dumps(code, ensure_ascii=False)))))
        else:
            token = entry["length"]
            # "%g" prints a whole number of metres without a decimal point, so an integer token is
            # what this writer really produces for, say, two hundred centimetres.
            if isinstance(token, Decimal) or (isinstance(token, int)
                                              and not isinstance(token, bool)):
                token = str(token)
            else:
                raise ValueError("a saved fault length is not a JSON number")
            recovered, disposition = centimetres(token)
            report["fault_length_entries"] += 1
            report["whole_centimetre_recoveries"] += recovered != ""
            writer.writerow(dict(row, entry_kind="REPORTED_FAULT_LENGTH",
                                 reported_fault_length_m=token,
                                 reported_fault_length_cm=recovered,
                                 fault_length_disposition=disposition,
                                 fault_length_basis=LENGTH_BASIS,
                                 source_entry_hex=text(retained(pair, "length", token))))
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted CSV exceeds bound")
    if report["result_entries"] + report["fault_length_entries"] != report["saved_entries"]:
        raise ValueError("entry accounting does not balance")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="saved `ethtool --json --cable-test` output")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--interface", required=True,
                        help="the interface the saved test was run on; the export names none")
    parser.add_argument("--capture-time-us", type=int, required=True,
                        help="the caller's capture clock; the export carries no timestamp")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        # Read once: the bytes that were converted and hashed are the bytes that are retained.
        saved = args.input.read_bytes()
        output, report = convert(saved, interface=args.interface,
                                 capture_time_us=args.capture_time_us)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "source.json").write_bytes(saved)
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError) as failure:
        print(f"cable test export conversion failed; no successful conversion claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
