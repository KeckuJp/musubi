#!/usr/bin/env python3
"""One fixed Betaflight decoded-CSV motor schema: commanded output demand, never measured rotation.

The decoder prints motor columns verbatim with no unit and no scaling, so the decoded CSV alone
carries motor values as undeclared raw numbers. This stage gives them their one declared meaning --
the mixer output the firmware commanded for each motor at log time -- keeps the writer's motor
index, and carries the output scale the writer declared in its own header beside the value.

It does not convert to rpm, thrust, percent or a 0..1 fraction: Betaflight 4.2.0 logs no rotation
measurement at all, and the writer's own comment records that a motor value legitimately falls
below the declared low when the command drops under mincommand, so a normalised fraction would be
negative for real rows. Rows outside the declared scale are counted and kept, never clamped or
dropped. Every source row and the source header are retained verbatim as hex, so columns this
schema does not name are preserved rather than discarded.
"""
import argparse
import csv
import io
import json
from pathlib import Path
import re

LIMIT = 16 * 1024 * 1024
# blackbox.c at 8f2d21460a9913d58bd1c33f8348c3791451fb45 defines field rows motor[0..7] under
# CONDITION(AT_LEAST_MOTORS_1..8) and writes an indexed field as name + "[%d]".
MOTOR_COLUMN = re.compile(r"motor\[([0-7])\]")
# blackboxMainState_t stores them as int16_t motor[MAX_SUPPORTED_MOTORS] (blackbox.c:308).
DEMAND_MIN, DEMAND_MAX = -32768, 32767
# The writer prints both motorOutput values with %d from a uint16_t.
SCALE_MAX = 65535
SCHEMAS = {"betaflight-4.2.0-motor-output": "time (us)"}
SCALE_BASIS = ("declared by the caller from the writer's own motorOutput header as recorded by "
               "decode_blackbox_recording.py --motor-output declared; this stage does not re-read "
               "the raw log and does not verify the scale against it")


def motor_columns(header):
    """The contiguous motor columns this schema requires, or a refusal naming what is wrong."""
    motors = [field for field in header if MOTOR_COLUMN.fullmatch(field)]
    if not motors:
        raise ValueError("no motor column in this decoded CSV")
    if motors != [f"motor[{index}]" for index in range(len(motors))]:
        raise ValueError("this schema requires the writer's contiguous motor[0..n] columns")
    return motors


def convert(text, schema, scale):
    if schema not in SCHEMAS or not text or len(text.encode()) > LIMIT or '"' in text:
        raise ValueError("unsupported motor schema or CSV representation")
    low, high = scale
    if not 0 <= low < high <= SCALE_MAX:
        raise ValueError("motor output scale must be a positive span inside the printed uint16 range")
    time_field = SCHEMAS[schema]
    lines = text.splitlines()
    rows = csv.reader(lines, strict=True)
    header = [field.strip() for field in next(rows)]
    if (not header or len(header) > 256 or len(header) != len(set(header))
            or any(not field or len(field) > 128 for field in header)
            or time_field not in header):
        raise ValueError("motor header mismatch")
    motors = motor_columns(header)
    output_fields = (["record_time_us"]
                     + [f"reported_motor_output_demand_{index}" for index in range(len(motors))]
                     + ["motor_count", "motor_output_declared_low", "motor_output_declared_high",
                        "motor_output_schema", "source_header_hex", "source_record_hex"])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, output_fields, lineterminator="\n")
    writer.writeheader()
    previous, count, below, above = None, 0, 0, 0
    rows_below, rows_above, equal_time_rows = 0, 0, 0
    for index, row in enumerate(rows, 1):
        if len(row) != len(header):
            raise ValueError("ragged motor row")
        values = dict(zip(header, row))
        time = values[time_field].strip()
        if not re.fullmatch(r"[0-9]{1,19}", time) or int(time) > 2**63 - 1:
            raise ValueError("invalid boot microseconds")
        time = int(time)
        if previous is not None and time < previous:
            raise ValueError("decreasing boot time")
        equal_time_rows += previous is not None and time == previous
        previous = time
        demands = []
        for field in motors:
            token = values[field].strip()
            if not re.fullmatch(r"-?[0-9]{1,6}", token):
                raise ValueError("motor demand must be a complete decimal the writer could hold")
            demand = int(token)
            if not DEMAND_MIN <= demand <= DEMAND_MAX:
                raise ValueError("motor demand outside the int16 the writer stores")
            below += demand < low
            above += demand > high
            demands.append(demand)
        # A row with several motors outside the span is one row, not several: the per-value and
        # per-row tallies are kept apart and named apart.
        rows_below += any(demand < low for demand in demands)
        rows_above += any(demand > high for demand in demands)
        writer.writerow(dict(zip(output_fields, [time, *demands, len(motors), low, high, schema,
                                                 "hex:" + lines[0].encode().hex(),
                                                 "hex:" + lines[index].encode().hex()])))
        count += 1
        if output.tell() > LIMIT:
            raise ValueError("motor output exceeds bound")
    if not count:
        raise ValueError("no motor records")
    report = {
        "motor_output_schema": schema, "rows": count, "motor_count": len(motors),
        "motor_columns": motors,
        "motor_output_declared_low": low, "motor_output_declared_high": high,
        "scale_basis": SCALE_BASIS,
        "source_columns_not_named_by_this_schema": len(header) - len(motors) - 1,
        "every_source_row_retained": "source_header_hex and source_record_hex hold the header and "
                                     "each row verbatim, so unnamed columns are preserved",
        "motor_values_below_the_declared_low": below,
        "motor_values_above_the_declared_high": above,
        "rows_with_any_demand_below_the_declared_low": rows_below,
        "rows_with_any_demand_above_the_declared_high": rows_above,
        "out_of_scale_counting_basis": "the value tallies count individual motor demands and the "
                                       "row tallies count records; one row with several motors "
                                       "outside the span is one row and several values",
        "out_of_scale_rows_are_kept": "a value under the declared low is legitimate when the "
                                      "command falls below mincommand; nothing is clamped or dropped",
        "rows_sharing_a_record_time_us_with_the_previous_row": equal_time_rows,
        "equal_record_time_us_policy": ("equal boot microseconds are kept in source order and not "
                                        "deduplicated here, but the shared reader refuses them by "
                                        "default with 'time must be strictly increasing'; pass its "
                                        "existing --allow-equal-time option to carry such rows into "
                                        "common output, where the order is source order only and "
                                        "asserts no event order between them"),
        "meaning": "commanded mixer output per motor at log time",
        "not": "not measured rotation, not eRPM, not thrust, and not evidence that a motor turned",
        "measured_rotation": "NOT_PRESENT_AT_THIS_SOURCE_VERSION",
        "pole_count": "NOT_APPLICABLE_NO_ERPM_FIELD_AT_THIS_SOURCE_VERSION",
        "platform_domain": "Unknown",
        "domain_basis": ("a commanded motor output does not establish vehicle mobility; a bench "
                         "log writes the same column, and nothing here is evidence of flight"),
        "domain_source_in_common_output": ("profile_declared_family: the shared reader maps only "
                                           "family ugv to Ground and leaves every other family, "
                                           "including this fpv profile, at Unknown. No Air is "
                                           "declared, derived or implied. An explicit "
                                           "platform_domain key is not available to this profile "
                                           "because the reader accepts one only on family unknown, "
                                           "so the family fallback is the operative path"),
        "common_output": "NOT_RUN"}
    return output.getvalue(), report


def parse_scale(text):
    pair = re.fullmatch(r"([0-9]{1,5}),([0-9]{1,5})", text)
    if pair is None:
        raise argparse.ArgumentTypeError("expected LOW,HIGH as the writer printed them")
    return int(pair[1]), int(pair[2])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--schema", choices=SCHEMAS, required=True)
    parser.add_argument("--output-scale", type=parse_scale, required=True,
                        help="the writer's declared motorOutput LOW,HIGH from decode.json")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        text, report = convert(args.input.read_text(encoding="utf-8"), args.schema,
                               args.output_scale)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2) + "\n")
    except (OSError, ValueError, csv.Error, StopIteration):
        parser.exit(2, "Motor output conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
