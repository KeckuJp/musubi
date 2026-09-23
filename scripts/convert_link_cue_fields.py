#!/usr/bin/env python3
"""Carry a reported link measurement under the field names the adopted cause evaluation reads.

Passive. This sends nothing, executes nothing and touches no radio. It is **not** a diagnosis and
not a second evaluator: it emits no cause, no candidate, no label, no threshold and no verdict.

What it does is narrow and checkable. `crates/predemo-readers/src/cause.rs` decides its link cues by
matching a **closed, hard-coded set of field names**, each read in a unit that module already
assumes. A saved export from an adopted converter uses that producer's own column names and units,
so today it reaches that evaluation **not at all** -- and routing it there by name alone would be
worse than not routing it, because the units differ: `uplink_lq_fraction` is a fraction in 0..1
while `RQly(%)` is read as a percent against a "< 90" test, so every ordinary row would read as
degraded.

This bridge therefore carries a declared source column to a declared target name **only** when the
caller states the source unit and that unit is the one the target is read in, applying the single
stated scale where one exists and refusing anything else by name. Alongside the values it states
which of the module's link inputs this export can supply and which it cannot, so an evaluation that
cannot run is visible as a **missing input** rather than as a negative result.

Nothing here changes cause.rs, its field lists, its thresholds or its tests; they are read and
joined, not edited.
"""
import argparse
import csv
import decimal
import hashlib
import json
from pathlib import Path
import sys

try:
    from scripts.convert_declared_expectation_check import guarded, identity, text
except ModuleNotFoundError:  # running the file directly
    from convert_declared_expectation_check import guarded, identity, text

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
ROW_LIMIT = 200000
COLUMN_LIMIT = 256
MAPPING_LIMIT = 8
TIME_LIMIT = 2**63
# The field names the adopted cause module matches on, read from
# crates/predemo-readers/src/cause.rs -- LQ_FIELDS, DBM_FIELDS, RSSI_FIELDS and NOISE_FIELDS -- each
# paired with the unit that module reads it in. `RQly(%)` and `1RSS(dB)` are already declared with
# these meanings by the adopted EdgeTX handset profiles. The two RADIO_STATUS fields carry a
# device-specific reported scale and NOT a physical unit: that module compares them as a ratio and
# against a raw number, so no dBm or percent reading of them is authorised here.
TARGET_UNITS = {
    "RQly(%)": "percent",
    "1RSS(dB)": "dBm",
    "RADIO_STATUS.rssi": "device_specific_scale",
    "RADIO_STATUS.noise": "device_specific_scale",
}
TARGETS = tuple(TARGET_UNITS)
# The one scale this bridge will apply, and it is applied only because the caller declared the
# source unit. There is no inverse: a percent is never divided into a fraction here, because the
# target names above are the only destinations and none of them is read as a fraction.
SCALES = {("fraction", "percent"): (100.0, "FRACTION_TO_PERCENT_BY_DECLARED_SCALE_OF_100")}
# What each declared unit can hold, from the meaning of the unit itself and nothing else. A value
# outside its own unit's range is not a reading in that unit, so it is carried as UNAVAILABLE rather
# than handed to a test that would compare it. `dBm` deliberately has NO range here: inventing one
# would be a physical claim about a radio that this bridge has no basis to make.
UNIT_RANGES = {"fraction": (0.0, 1.0), "percent": (0.0, 100.0)}
# The producer contract for the two RADIO_STATUS target names, from the pinned definition:
#   mavlink/mavlink 3203f89c510337c0088244735c6a5056c52b5a28
#   message_definitions/v1.0/common.xml
#   sha256:04f01bbaa94e51e554765ba1fc7f3058204876cffb8de99a49b6671b2c9bfc58
#   <message id="109" name="RADIO_STATUS">
#     rssi  uint8_t invalid="UINT8_MAX" "... Values: [0-254], UINT8_MAX: invalid/unknown."
#     noise uint8_t invalid="UINT8_MAX" "... Values: [0-254], UINT8_MAX: invalid/unknown."
# A generic "device-specific scale" is NOT licence to read 255 as a measurement: the producer
# documents it as the missing code, so it is carried as unavailable. Choosing one of these target
# names is choosing that contract; a producer that does not follow it must not be mapped to them.
SENTINEL_TARGETS = {"RADIO_STATUS.rssi", "RADIO_STATUS.noise"}
SENTINEL_VALUE = 255
SENTINEL_RANGE = (0, 254)
IDENTITY_SCALE = "CARRIED_UNCHANGED_THE_DECLARED_SOURCE_UNIT_IS_THE_UNIT_THIS_NAME_IS_READ_IN"
# Which link test in the adopted module each target name feeds. Stated so that an absent input names
# the test it leaves undetermined, instead of that test quietly reading nothing.
FEEDS = {
    "RQly(%)": "REPORTED_LINK_QUALITY_TEST",
    "1RSS(dB)": "REPORTED_DBM_TEST",
    "RADIO_STATUS.rssi": "REPORTED_RSSI_RATIO_TEST",
    "RADIO_STATUS.noise": "REPORTED_NOISE_TEST",
}
MEASUREMENT_BASIS = (
    "A_REPORTED_LINK_MEASUREMENT_CARRIED_UNDER_THE_FIELD_NAME_THE_ADOPTED_CAUSE_EVALUATION_READS_"
    "AND_IN_THE_UNIT_THAT_EVALUATION_READS_IT_IN_THE_VALUE_IS_THE_PRODUCERS_OWN_REPORT_AND_IS_NEVER_"
    "A_CALIBRATED_RADIO_MEASUREMENT_THIS_ROW_STATES_NO_CAUSE_NO_CANDIDATE_NO_LABEL_NO_THRESHOLD_AND_"
    "NO_VERDICT_AND_CARRYING_A_VALUE_HERE_IS_NEVER_EVIDENCE_OF_INTERFERENCE_OBSTRUCTION_RANGE_OR_AN_"
    "ANTENNA_FAULT_WHICH_THIS_BRIDGE_DOES_NOT_DISTINGUISH_AND_DOES_NOT_CLAIM_TO_AN_ABSENT_INPUT_IS_"
    "A_MISSING_INPUT_AND_IS_NEVER_A_NEGATIVE_RESULT")
FIELDS = ["record_time_us", "RQly(%)", "1RSS(dB)", "RADIO_STATUS.rssi", "RADIO_STATUS.noise",
          "mapped_cue_inputs", "cue_inputs_carried", "cue_inputs_absent",
          "undetermined_cue_tests", "declared_clock_basis", "windowed_evaluation_input",
          "source_mapping_hex", "measurement_basis", "source_sha256"]
SEPARATOR = "+"
# Every cue test in the adopted module is decided inside a wall-clock window, and that window reads
# `wall_ms`, which the common reader populates only on a host axis (`lib.rs`: `wall_ms: if host_axis
# { Some(t_ms) } else { None }`). So a saved export the caller declares on an UNKNOWN clock supplies
# no windowed input at all -- not a weak one. That is stated on every row rather than showing up as
# an evaluation that silently found nothing.
WINDOWED = {
    "unknown": ("NO_WALL_CLOCK_AXIS_ON_A_DECLARED_UNKNOWN_BASIS_SO_THE_WINDOWED_EVALUATION_READS_"
                "NOTHING_FROM_THESE_ROWS_AT_ALL_AND_A_RESULT_OF_NO_CUES_IS_AN_ABSENT_INPUT_AND_"
                "NEVER_A_HEALTHY_LINK"),
    "boot_relative": ("BOOT_RELATIVE_ROWS_CARRY_A_WALL_CLOCK_AXIS_ONLY_WHEN_THE_CALLER_ALSO_"
                      "SUPPLIES_AN_OFFSET_ESTIMATE_AT_EVALUATION_TIME_WITHOUT_ONE_THE_WINDOWED_"
                      "EVALUATION_STILL_READS_NOTHING_FROM_THESE_ROWS_AND_THIS_DECLARATION_IS_A_"
                      "PRECONDITION_AND_NEVER_A_SUBSTITUTE_FOR_THAT_OFFSET"),
}


def mapping(spec):
    """One `TARGET=SOURCE:UNIT` declaration; nothing is guessed from a column name."""
    if not isinstance(spec, str) or spec.count("=") != 1:
        raise ValueError("a mapping is one TARGET=SOURCECOLUMN:SOURCEUNIT declaration")
    target, _, rest = spec.partition("=")
    source, colon, unit = rest.rpartition(":")
    if target not in TARGET_UNITS:
        raise ValueError(f"{target!r} is not a field name the adopted cause evaluation reads; "
                         f"the readable names are {TARGETS}")
    if not colon or not source or not unit:
        raise ValueError("a mapping states the source column and its declared unit as SOURCE:UNIT")
    wanted = TARGET_UNITS[target]
    if unit == wanted:
        return target, source, unit, 1.0, IDENTITY_SCALE
    if (unit, wanted) in SCALES:
        factor, named = SCALES[(unit, wanted)]
        return target, source, unit, factor, named
    raise ValueError(f"{target!r} is read in {wanted!r}; a source declared {unit!r} is refused "
                     "rather than converted, because naming a column authorises no conversion")


def reading(raw, source, unit, target):
    """One saved value, or `None` when the source documents it as unavailable.

    `float()` alone is not safe here. `float("1e-999")` is a silent 0.0, which would hand a test a
    zero the file never reported, and `float("1e999")` is an infinity. Both are decided against the
    exact decimal text, so an underflow or overflow is never rounded into a reading.
    """
    try:
        exact = decimal.Decimal(raw)
    except decimal.InvalidOperation:
        raise ValueError(f"saved value {raw!r} in {source!r} is not a number this bridge can "
                         "carry; it is refused rather than passed through")
    if not exact.is_finite():
        raise ValueError(f"saved value {raw!r} in {source!r} is not finite")
    value = float(exact)
    if value in (float("inf"), float("-inf")):
        raise ValueError(f"saved value {raw!r} in {source!r} overflows to infinity when read as a "
                         "number; it is refused rather than carried as a bound")
    if value == 0.0 and exact != 0:
        raise ValueError(f"saved value {raw!r} in {source!r} is not zero but underflows to zero "
                         "when read as a number; it is refused rather than carried as a zero")
    if target in SENTINEL_TARGETS:
        # The pinned producer contract, not a generic scale: an integer in [0-254], with UINT8_MAX
        # documented as invalid/unknown. The missing code stays missing.
        if exact != exact.to_integral_value():
            raise ValueError(f"{target!r} is a uint8 field in the pinned definition; the saved "
                             f"value {raw!r} in {source!r} is not an integer")
        if int(exact) == SENTINEL_VALUE:
            return None
        if not SENTINEL_RANGE[0] <= int(exact) <= SENTINEL_RANGE[1]:
            raise ValueError(f"{target!r} is documented as [0-254] with 255 invalid/unknown; the "
                             f"saved value {raw!r} in {source!r} is outside that field entirely")
    low_high = UNIT_RANGES.get(unit)
    if low_high is not None and not low_high[0] <= value <= low_high[1]:
        # Outside its own unit's range it is not a reading in that unit, so it stays unavailable
        # rather than reaching a test that would compare it. No range is invented for dBm.
        return None
    return value


def read_rows(data, time_column):
    """The saved observations exactly as written; a duplicate header name is refused."""
    if not data or len(data) > LIMIT:
        raise ValueError("empty or oversized saved observations")
    reader = csv.reader(data.decode("utf-8-sig").splitlines(True), strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("saved observations carry no header")
    except csv.Error as failure:
        raise ValueError(f"the saved CSV cannot be parsed as written: {failure}")
    if not 1 <= len(header) <= COLUMN_LIMIT or len(set(header)) != len(header):
        raise ValueError("unsupported or duplicated saved column names")
    if time_column not in header:
        raise ValueError("declared time column is not in the saved header")
    index = {name: position for position, name in enumerate(header)}
    rows, previous = [], None
    for cells in guarded(reader):
        if len(cells) != len(header):
            raise ValueError("saved row width does not match the header")
        if len(rows) >= ROW_LIMIT:
            raise ValueError("saved observations exceed the row bound")
        moment = cells[index[time_column]]
        if not moment.isdigit() or len(moment) > 19:
            raise ValueError("saved time is not a non-negative integer of microseconds")
        moment = int(moment)
        if moment >= TIME_LIMIT:
            raise ValueError("saved time is outside the supported microsecond range")
        if previous is not None and moment < previous:
            raise ValueError("saved time decreases; this bridge never reorders a saved file")
        previous = moment
        rows.append((moment, cells))
    if not rows:
        raise ValueError("saved observations carry no rows")
    return header, index, rows


def convert(data, *, mappings, clock_basis="unknown", time_column="record_time_us"):
    """One row per saved row. No cue, no candidate and no cause is produced anywhere here."""
    if not mappings:
        raise ValueError("at least one TARGET=SOURCE:UNIT mapping is required; this bridge never "
                         "guesses which saved column carries which reported quantity")
    if len(mappings) > MAPPING_LIMIT:
        raise ValueError("too many declared mappings in one bridge run")
    if clock_basis not in WINDOWED:
        raise ValueError("the declared clock basis is one this telemetry CSV format accepts: unknown or "
                         "boot_relative")
    parsed = [mapping(spec) for spec in mappings]
    if len({entry[0] for entry in parsed}) != len(parsed):
        raise ValueError("one target field is declared twice; a name is carried from one column")
    header, index, rows = read_rows(data, time_column)
    for _, source, _, _, _ in parsed:
        if source not in header:
            raise ValueError(f"declared source column {source!r} is not in the saved header")

    carried = [entry[0] for entry in parsed]
    absent = [name for name in TARGETS if name not in carried]
    # An absent input names the test it leaves undetermined. That test is not run and not failed.
    undetermined = [FEEDS[name] for name in absent]
    # A reversible JSON array in the estate's existing shape, so a separator inside one value can
    # never collide with another tuple.
    declared = [[target, source, unit, named] for target, source, unit, _, named in parsed]
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    shared = {
        # Declaration-level capability: which names the caller mapped at all. The three columns
        # below it are recomputed per row from what that row actually carried.
        "mapped_cue_inputs": SEPARATOR.join(carried),
        "cue_inputs_carried": "",
        "cue_inputs_absent": "",
        "undetermined_cue_tests": "",
        "declared_clock_basis": clock_basis,
        "windowed_evaluation_input": WINDOWED[clock_basis],
        "source_mapping_hex": identity(declared),
        "measurement_basis": MEASUREMENT_BASIS,
        "source_sha256": digest,
    }
    report = {"rows": len(rows), "declared_mappings": len(parsed),
              "mapped_cue_inputs": carried, "never_mapped_cue_inputs": absent,
              "undetermined_cue_tests_when_unmapped": undetermined,
              "readings_carried": {name: 0 for name in carried},
              "readings_unavailable": {name: 0 for name in carried},
              "rows_with_no_qualified_reading": 0,
              "declared_clock_basis": clock_basis,
              "windowed_evaluation_input": WINDOWED[clock_basis], "source_sha256": digest,
              "evaluation_performed": "NONE_THIS_BRIDGE_EVALUATES_NOTHING_AND_NAMES_NO_CAUSE",
              "sent_messages": "NONE_THIS_TOOL_NEVER_TRANSMITS",
              "truncated_rows": "NONE_EVERY_SAVED_ROW_PRODUCES_ONE_ROW",
              "measurement_basis": MEASUREMENT_BASIS}

    produced = []
    for moment, cells in rows:
        line = dict(shared, record_time_us=moment, **{name: "" for name in TARGETS})
        # Availability is decided PER ROW from the readings that are actually qualified on it. A
        # blank, an unavailable sentinel and a value outside its unit are all absent here; only a
        # mapping that produced a number on THIS row counts as carried.
        present = []
        for target, source, unit, factor, _ in parsed:
            raw = cells[index[source]]
            value = None if raw == "" else reading(raw, source, unit, target)
            if value is None:
                report["readings_unavailable"][target] += 1
                continue
            scaled = value * factor
            if scaled in (float("inf"), float("-inf")):
                raise ValueError(f"the declared scale overflows {source!r} to infinity; refused "
                                 "rather than carried as a bound")
            line[target] = repr(scaled) if scaled != int(scaled) else str(int(scaled))
            report["readings_carried"][target] += 1
            present.append(target)
        missing = [name for name in TARGETS if name not in present]
        line["cue_inputs_carried"] = SEPARATOR.join(present)
        line["cue_inputs_absent"] = SEPARATOR.join(missing)
        line["undetermined_cue_tests"] = SEPARATOR.join(FEEDS[name] for name in missing)
        if not present:
            report["rows_with_no_qualified_reading"] += 1
        produced.append(line)
    output = ",".join(FIELDS) + "\n"
    output += "".join(",".join(str(line[name]) for name in FIELDS) + "\n" for line in produced)
    encoded = len(output.encode("utf-8"))
    if encoded > OUTPUT_LIMIT:
        raise ValueError("expanded output exceeds bound")
    report["output_bytes"] = encoded
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="observations an adopted converter already wrote")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--time-column", default="record_time_us")
    parser.add_argument("--map", action="append", default=[], dest="mappings",
                        help="TARGET=SOURCECOLUMN:SOURCEUNIT, where TARGET is a field name the "
                             "adopted cause evaluation reads and SOURCEUNIT is the caller's "
                             "declaration about the saved column")
    parser.add_argument("--source-clock-basis", default="unknown",
                        choices=tuple(WINDOWED),
                        help="the caller's declaration about the saved clock; nothing is inferred")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(args.input.read_bytes(), mappings=tuple(args.mappings),
                                 clock_basis=args.source_clock_basis,
                                 time_column=args.time_column)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, csv.Error) as failure:
        print(f"link cue field bridge failed; no measurement claim: {failure}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
