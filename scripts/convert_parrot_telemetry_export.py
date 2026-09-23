#!/usr/bin/env python3
"""Saved Parrot telemetry blackbox section CSV -> common Observation; no decoder is bundled.

The engineer decodes their own `.tlmb` with Parrot's own `tools/tlmb2csv.py` and passes the
per-section CSV here. This reads that writer's documented output layout and applies the units and
frames Parrot's published variable list states. Nothing here opens a binary blackbox, contacts a
drone or runs a mission.

Descriptor names are chosen freely by the producer that registered them (`producer->reg(v, "name")`
in Parrot's own example), so a documented variable name is not proof of the name inside a blackbox.
The scalar section below uses the documented names and refuses anything else; the array section
requires the caller to declare the exact base names their export actually carries.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path

INPUT_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
# Parrot-Developers/telemetry @1ff986b99ca4b758544b7ca1af7863e46635a17a, tools/tlmb2csv.py.
DECODER = "Parrot-Developers/telemetry@1ff986b99ca4b758544b7ca1af7863e46635a17a tools/tlmb2csv.py"
# developer.parrot.com "Telemetry list for Anafi AI - 8.4.2".
DEFINITIONS = "Parrot AirSDK telemetry list for Anafi AI 8.4.2"
SCALAR_NAME_BASIS = ("DOCUMENTED_VARIABLE_NAMES_REQUIRED_EXACTLY_THE_PINNED_LIST_NAMES_THE_SECTION_"
                     "AND_ITS_SCALARS_AND_THIS_CONVERTER_REFUSES_ANY_OTHER_SPELLING_RATHER_THAN_"
                     "MATCHING_LOOSELY_BUT_THE_REGISTERED_DESCRIPTOR_NAME_IS_A_PRODUCER_CHOICE_SO_"
                     "AGREEMENT_IS_CHECKED_HERE_NOT_PROVEN_UPSTREAM")
ARRAY_NAME_BASIS = ("CALLER_DECLARED_BASE_NAMES_NOT_ESTABLISHED_BY_ANY_PUBLIC_PARROT_SOURCE_THE_"
                    "VARIABLE_LIST_RENDERS_THESE_AS_GYRO_BRACKET_TEN_DOT_X_WHILE_THE_DECODER_"
                    "EMITS_REGISTERED_NAME_UNDERSCORE_INDEX_AND_THE_REGISTERED_NAME_IS_A_FREE_"
                    "PRODUCER_STRING_SO_THIS_MAPPING_IS_AN_UNQUALIFIED_OPTIONAL_DECLARATION")
TIME_BASES = {
    "relative": "SECTION_RELATIVE_MICROSECONDS_FIRST_SAMPLE_ZERO_AS_THE_DECODER_DEFAULT_SUBTRACTS_"
                "THE_FIRST_SAMPLE_OF_THE_SECTION_NOT_UTC_AND_NOT_A_DEVICE_EPOCH",
    "raw": "RAW_DRONE_CLOCK_MICROSECONDS_AS_THE_DECODER_RAW_TS_OPTION_EMITS_THE_SOURCE_STATES_NO_"
           "EPOCH_FOR_IT_SO_NONE_IS_APPLIED_AND_IT_IS_NOT_UTC",
}
# Scalar section: the pinned list states these names, units and rate directly.
BAROMETER = {"pressure": "barometer_pressure_pa", "temperature": "barometer_temperature_k"}
# Array section: the nine declared quantities, in the fixed order the caller must declare names for.
IMU_QUANTITIES = ("imu_gyro_x_rad_s", "imu_gyro_y_rad_s", "imu_gyro_z_rad_s",
                  "imu_acc_x_m_s2", "imu_acc_y_m_s2", "imu_acc_z_m_s2", "imu_temperature_k",
                  "imu_sample_time_s", "imu_sample_time_ns")
# The pinned 8.4.2 list states ten readings arrive at every IMU sample, so exactly ten are accepted.
IMU_READINGS = 10
UNSIGNED_QUANTITIES = ("imu_sample_time_s", "imu_sample_time_ns")
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
COMMON = ["record_time_us", "parrot_export_time_us", "parrot_time_basis", "parrot_section_hex",
          "parrot_name_basis", "source_row", "imu_sample_index", "source_sha256"]


def number(text, field):
    integer = field in UNSIGNED_QUANTITIES
    if not NUMBER.fullmatch(text):
        raise ValueError("selected quantity must be a plain decimal the decoder wrote")
    if integer:
        value = int(text)
        # The pinned list declares both timestamp halves uint64, so a negative one is not a value.
        if value < 0 or value >= 2**63:
            raise ValueError("declared unsigned timestamp half outside the common signed range")
        return value
    value = float(text)
    # A long plain decimal parses without notation yet can still reach infinity.
    if not math.isfinite(value):
        raise ValueError("selected quantity must be finite")
    return value


def convert(raw, selector, capture_time_us, timestamp_mode, imu_base_names=None):
    if selector not in ("sensors-barometer", "sensors-imu"):
        raise ValueError("explicit selected telemetry section required")
    if timestamp_mode not in TIME_BASES:
        raise ValueError("caller must declare which decoder timestamp mode produced this export")
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**63:
        raise ValueError("explicit capture microseconds required")
    if not isinstance(raw, bytes) or not raw or len(raw) > INPUT_LIMIT:
        raise ValueError("empty or oversized decoded export")
    array = selector == "sensors-imu"
    if array:
        if (not isinstance(imu_base_names, (list, tuple))
                or len(imu_base_names) != len(IMU_QUANTITIES)
                or len(set(imu_base_names)) != len(IMU_QUANTITIES)
                or not all(isinstance(name, str) and name and "," not in name
                           for name in imu_base_names)):
            raise ValueError("caller must declare one distinct base name per declared IMU quantity")
        columns = dict(zip(imu_base_names, IMU_QUANTITIES))
        section, basis = "sensors_imu", ARRAY_NAME_BASIS
    elif imu_base_names is not None:
        raise ValueError("base names apply only to the declared array section")
    else:
        columns, section, basis = BAROMETER, "sensors_barometer", SCALAR_NAME_BASIS

    # Hash and retain the bytes exactly as supplied; decoding must not change what is bound.
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    # Line ends are a transport detail of the supplied file, never a change to the retained bytes.
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise ValueError("decoded export carries no header")
    # The decoder writes the header with bare commas and the rows with ", ", so both are stripped.
    header = [cell.strip() for cell in lines[0].split(",")]
    if header[0] != "time_us":
        raise ValueError("decoded export must start with the decoder's time_us column")
    if len(set(header)) != len(header):
        # A repeated name would silently overwrite an earlier column and lose a selected value.
        raise ValueError("decoded header repeats a column name")

    quantities = sorted(set(columns.values()))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, [*COMMON, *quantities], lineterminator="\n")
    writer.writeheader()
    report = {"source_rows": 0, "output_records": 0, "unmapped_columns": [],
              "selected_section": section, "decoder": DECODER, "definitions": DEFINITIONS,
              "source_sha256": digest, "name_binding": basis,
              "readings_per_sample": IMU_READINGS if array else 1,
              "clock": "CALLER_CAPTURE_ORDERS_COMMON_OUTPUT; parrot_export_time_us is the decoder "
                       "column and is never merged with it",
              "time_basis": TIME_BASES[timestamp_mode],
              "evidence": "decoded export of a fixed public writer and variable list, not a real "
                          "flight, drone or physical sensor calibration"}

    positions = {}
    for column, name in enumerate(header[1:], start=1):
        base, _, suffix = name.rpartition("_")
        if array and base in columns and suffix.isdigit() and str(int(suffix)) == suffix:
            positions[(base, int(suffix))] = column
        elif not array and name in columns:
            positions[(name, 0)] = column
        else:
            report["unmapped_columns"].append(name)
    # The pinned list fixes the count, so a shorter or longer run is refused rather than accepted.
    indices = list(range(IMU_READINGS)) if array else [0]
    for base in columns:
        missing = [index for index in indices if (base, index) not in positions]
        if missing:
            raise ValueError("selected section requires every declared column at every "
                             "declared index")
    if array and any(index >= IMU_READINGS for _, index in positions):
        raise ValueError("declared array carries more readings than the pinned list states")

    for row_index, line in enumerate(lines[1:]):
        if not line.strip():
            raise ValueError("blank row inside the decoded export")
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) != len(header):
            raise ValueError("decoded row width differs from the decoder header")
        report["source_rows"] += 1
        export_time = number(cells[0], "imu_sample_time_s")
        for index in indices:
            row = {"record_time_us": capture_time_us, "parrot_export_time_us": export_time,
                   "parrot_time_basis": TIME_BASES[timestamp_mode],
                   "parrot_section_hex": "hex:" + section.encode().hex(),
                   "parrot_name_basis": basis, "source_row": row_index, "source_sha256": digest,
                   "imu_sample_index": index if array else ""}
            for base, field in columns.items():
                row[field] = number(cells[positions[(base, index)]], field)
            writer.writerow(row)
            report["output_records"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("converted export exceeds bound")
    if not report["output_records"]:
        raise ValueError("no decoded sample rows")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="one section CSV written by Parrot's tlmb2csv.py")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--section", required=True, choices=("sensors-barometer", "sensors-imu"))
    parser.add_argument("--capture-time-us", type=int, required=True)
    parser.add_argument("--timestamp-mode", required=True, choices=sorted(TIME_BASES),
                        help="which mode the engineer ran the decoder in; raw is its -r option")
    parser.add_argument("--imu-base-names", help="required for sensors-imu and UNQUALIFIED: the "
                        "exact base names this export carries, comma separated, in the order "
                        "gyro x,y,z then acc x,y,z then temperature then timestamp seconds then "
                        "timestamp nanoseconds. No public Parrot source establishes these names.")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as source:
            # Bounded before decoding, so an oversized file is refused rather than loaded.
            raw = source.read(INPUT_LIMIT + 1)
        names = args.imu_base_names.split(",") if args.imu_base_names is not None else None
        converted, report = convert(raw, args.section, args.capture_time_us, args.timestamp_mode,
                                    names)
        args.output_directory.mkdir()
        # Byte-for-byte, so the retained source still matches the digest that binds every row.
        (args.output_directory / "source.csv").write_bytes(raw)
        (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2) + "\n",
                                                           encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError,
            UnicodeDecodeError):
        parser.exit(2, "Parrot telemetry export conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
