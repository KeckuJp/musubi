#!/usr/bin/env python3
"""Supplied fixed M3M calibration reports -> quantities and existing saved-value comparison."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re

try:
    from scripts.convert_m3m_capture import (
        LIMIT, MAX_IMAGES, capture_row, documented_number, xmp, hexed, resolve,
        calendar_date, unique_object, invalid_constant)
    from scripts.convert_saved_state_transitions import convert as compare, FIELDS as COMPARISON_FIELDS
except ModuleNotFoundError:
    from convert_m3m_capture import (
        LIMIT, MAX_IMAGES, capture_row, documented_number, xmp, hexed, resolve,
        calendar_date, unique_object, invalid_constant)
    from convert_saved_state_transitions import convert as compare, FIELDS as COMPARISON_FIELDS

GEOMETRY = "caller-declared-native-unresized-uncropped"
PIXELS = tuple("calibration_" + axis + "_pixel" for axis in ("fx", "fy", "cx", "cy"))
SUBJECTS = ("camera_serial_hex", "model_reported", "image_source_hex", "band_name_reported",
            "sensor_index_code", "image_width_pixel", "image_height_pixel")
FIELDS = ("record_time_us", "source_image_index", "source_sha256") + SUBJECTS + (
    "calibration_date", "dewarp_data_hex", "calibration_status", "geometry_basis") + PIXELS


def convert(data, *, capture_times_us, geometry_basis):
    if geometry_basis != GEOMETRY:
        raise ValueError("explicit native unresized uncropped geometry declaration required")
    if not isinstance(data, bytes) or not data or len(data) > LIMIT:
        raise ValueError("empty or oversized calibration export")
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_float=Decimal, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("malformed calibration export") from exc
    if not isinstance(document, list) or not 1 <= len(document) <= MAX_IMAGES:
        raise ValueError("calibration export image bound")
    if not isinstance(capture_times_us, (list, tuple)) or len(capture_times_us) != len(document):
        raise ValueError("one caller capture time is required per image")
    if any(type(t) is not int or not 0 <= t < 2**63 for t in capture_times_us) or any(
            a > b for a, b in zip(capture_times_us, capture_times_us[1:])):
        raise ValueError("invalid or decreasing caller capture time")
    digest = hashlib.sha256(data).hexdigest()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n", restval="")
    writer.writeheader()
    report = {"source_sha256": "sha256:" + digest, "source_records": len(document),
              "calibration_records": 0, "missing_calibration_records": 0, "retained_tags": [],
              "clock": "Unknown", "geometry_basis": geometry_basis,
              "claim": "REPORTED_CALIBRATION_VALUE_CHANGE_NOT_PHYSICAL_DRIFT_OR_ACCURACY",
              "source_recovery": "original source.json + source_sha256 + one-based source_image_index"}
    for index, (record, moment) in enumerate(zip(document, capture_times_us), 1):
        # Reuse fixed exporter/writer/band qualification and existing value guards, unchanged.
        qualified, _ = capture_row(record, index, moment, digest)
        if qualified["band_identity_agreement"] != "THE_NAME_AND_THE_INDEX_AGREE":
            raise ValueError("comparison requires a qualified matching band and sensor index")
        used = {"ExifTool:ExifToolVersion", "XMP:Version", "XMP:ImageSource", "XMP:BandName", "XMP:Sensorindex"}
        serial, model = xmp(record, "CameraSerialNumber", used), xmp(record, "DroneModel", used)
        if not isinstance(serial, str) or not serial or len(serial.encode()) > 256 or "\x00" in serial:
            raise ValueError("comparison requires a bounded reported camera serial")
        if model != "M3M":
            raise ValueError("comparison requires the fixed reported M3M model")
        row = {"record_time_us": moment, "source_image_index": index, "source_sha256": "sha256:" + digest,
               "camera_serial_hex": hexed(serial), "model_reported": model,
               "image_source_hex": qualified["image_source_hex"], "band_name_reported": qualified["band_name_reported"],
               "sensor_index_code": qualified["sensor_index_code"], "geometry_basis": geometry_basis}
        for name, column in (("ImageWidth", "image_width_pixel"), ("ImageHeight", "image_height_pixel")):
            value = resolve(record, "EXIF", name, used)[0]
            if type(value) is not int or not 1 <= value <= 1000000:
                raise ValueError("comparison requires bounded positive native image dimensions")
            row[column] = value
        value = xmp(record, "DewarpData", used)
        if value is None:
            row["calibration_status"] = "NOT_REPORTED_COMPARISON_CHAIN_BROKEN"
            report["missing_calibration_records"] += 1
        else:
            if not isinstance(value, str) or len(value.encode()) > 4096:
                raise ValueError("DewarpData text bound")
            match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2});(.*)", value)
            if not match:
                raise ValueError("DewarpData date and nine-number grammar required")
            day, numbers = match.groups()
            calendar_date(day.replace("-", ":"), "calibration date")
            parts = numbers.split(",")
            if len(parts) != 9:
                raise ValueError("DewarpData requires nine numbers in source order")
            parsed = [Decimal(documented_number(part, "DewarpData")) for part in parts]
            for name, number in zip(PIXELS, parsed[:4]):
                # No Decimal.normalize(): its context can round long exact source decimals.
                text = format(number, "f")
                row[name] = "0" if number == 0 else text.rstrip("0").rstrip(".") if "." in text else text
            row.update(calibration_date=day, dewarp_data_hex=hexed(value),
                       calibration_status="REPORTED_PIXEL_CALIBRATION_NOT_APPLIED")
            report["calibration_records"] += 1
        writer.writerow(row)
        # Every emitted cell is ASCII (text inputs are hex), so tell() is also the byte count.
        if output.tell() > LIMIT:
            raise ValueError("calibration CSV exceeds comparator input bound")
        report["retained_tags"].append({"source_image_index": index,
                                        "not_interpreted": sorted(set(record) - used)})
    quantity = output.getvalue()
    if report["calibration_records"]:
        transitions, comparison = compare(quantity.encode(), clock_basis="Unknown caller capture microseconds",
                                         subjects=SUBJECTS, states=tuple((field,
                                             "DJI M3M guide 2023-08-29 p3,p9; pixel calibration on caller-declared native grid; not physical drift")
                                             for field in PIXELS))
    else:
        # The existing comparator refuses zero events. Preserve missing quantity records without
        # manufacturing a comparison observation or claiming an empty file passed that reader.
        transitions = ",".join(COMPARISON_FIELDS) + "\n"
        comparison = {"state_transitions": 0, "reported_value_changes": 0,
                      "disposition": "NOT_RUN_NO_REPORTED_CALIBRATION",
                      "source_sha256": "sha256:" + hashlib.sha256(quantity.encode()).hexdigest()}
    report["comparison"] = comparison
    if len(json.dumps(report, indent=2).encode()) > LIMIT:
        raise ValueError("calibration report exceeds bound")
    return quantity, transitions, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--capture-time-us", action="append", type=int, required=True)
    parser.add_argument("--geometry-basis", choices=[GEOMETRY], required=True)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            source = stream.read(LIMIT + 1)
        quantity, transitions, report = convert(source, capture_times_us=args.capture_time_us,
                                                geometry_basis=args.geometry_basis)
        args.output_directory.mkdir(parents=True, exist_ok=False)
        (args.output_directory / "source.json").write_bytes(source)
        (args.output_directory / "observations.csv").write_text(quantity, encoding="utf-8")
        (args.output_directory / "transitions.csv").write_text(transitions, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
