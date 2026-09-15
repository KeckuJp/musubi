#!/usr/bin/env python3
"""Passive, bounded ISOXML dynamic timelog -> reported actual-rate changes."""
import argparse
import csv
import io
import json
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
PTN = {"A": "i", "B": "i", "C": "i", "D": "B", "E": "H", "F": "H", "G": "B", "H": "I", "I": "H"}
ACTUAL_RATES = {2: ("actual_volume_rate_l_ha", .0001),
                7: ("actual_mass_rate_kg_m2", .000001),
                12: ("actual_count_per_m2", .001)}
HARVEST = {84: ("reported_yield_mass_kg_m2", .000001),
           87: ("reported_yield_mass_kg_s", .000001),
           90: ("reported_yield_total_mass_kg", 1)}
HARVEST_FIELDS = [value[0] for value in HARVEST.values()] + [
    "reported_average_crop_moisture_ppm", "reported_average_crop_moisture_fraction",
    "reported_moisture_status"]
STATE_NAMES = ("DISABLED", "ENABLED", "ERROR", "UNDEFINED_OR_NOT_INSTALLED")
STATE_FIELDS = ["reported_work_state", "first_child_ordinal"] + [f"child_state_{i}" for i in range(1, 17)]


def convert(template, data, *, harvest=False, located=False, work_state=False):
    if not template or len(template) > 65536 or not data or len(data) > LIMIT:
        raise ValueError("empty or oversized timelog")
    # No XML DTD/entity resolution, alternate encodings or external resource references.
    text = template.decode("utf-8-sig")
    if "<!" in text or "\x00" in text:
        raise ValueError("unsupported XML declaration/entity")
    root = ET.fromstring(text)
    if root.tag != "TIM" or root.attrib != {"A": "", "D": "4"}:
        raise ValueError("requires dynamic effective TIM template")
    children = list(root)
    positions = [node for node in children if node.tag == "PTN"]
    columns = [node for node in children if node.tag == "DLV"]
    if len(positions) > 1 or not 1 <= len(columns) <= 255 or len(positions) + len(columns) != len(children):
        raise ValueError("unsupported timelog child layout")
    position = positions[0].attrib if positions else {}
    if type(located) is not bool or (located and not {"A", "B"}.issubset(position)):
        raise ValueError("located updates require dynamic north/east PTN")
    if any(key not in PTN or value != "" for key, value in position.items()):
        raise ValueError("requires dynamic supported PTN fields")
    position_size = sum(struct.calcsize(code) for key, code in PTN.items() if key in position)
    metadata = []
    for node in columns:
        if set(node.attrib) != {"A", "B", "C"} or node.attrib["B"] != "" or not node.attrib["C"]:
            raise ValueError("requires dynamic DLV with a device reference")
        if not re.fullmatch(r"[0-9a-fA-F]{4}", node.attrib["A"]):
            raise ValueError("invalid DDI")
        metadata.append((int(node.attrib["A"], 16), node.attrib["C"]))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["record_time_us", "source_row", "source_update", "source_column", "device_element_hex",
                     "source_raw_value", "actual_volume_rate_l_ha", "source_record_hex",
                     "source_ddi", "actual_mass_rate_kg_m2", "actual_count_per_m2"] +
                    (HARVEST_FIELDS if harvest else []) +
                    (STATE_FIELDS if work_state else []) +
                    (["reported_latitude_deg", "reported_longitude_deg", "position_status_raw",
                      "position_disposition"] if located else []))
    report = {"source_records": 0, "source_updates": 0, "decoded_updates": 0,
              "unsupported_updates": 0, "records_without_supported_update": 0,
              "template_hex": template.hex(), "source_records_hex": [], "clock": "Unknown"}
    offset, previous, report_bytes = 0, None, len(template) * 2
    while offset < len(data):
        start = offset
        if len(data) - offset < 7 + position_size:
            raise ValueError("truncated time/position/count")
        milliseconds, days = struct.unpack_from("<IH", data, offset)
        if milliseconds >= 86400000 or days == 65535:
            raise ValueError("invalid or unavailable local calendar time")
        timestamp = (days * 86400000 + milliseconds) * 1000
        if previous is not None and timestamp < previous:
            raise ValueError("decreasing local calendar time")
        previous = timestamp
        position_values = []
        if located:
            point, cursor = {}, offset + 6
            for key, code in PTN.items():
                if key in position:
                    point[key] = struct.unpack_from("<" + code, data, cursor)[0]
                    cursor += struct.calcsize("<" + code)
            missing = point["A"] == 2**31 - 1 or point["B"] == 2**31 - 1
            for key, limit in (("A", 900000000), ("B", 1800000000)):
                if point[key] != 2**31 - 1 and abs(point[key]) > limit:
                    raise ValueError("reported geographic coordinate outside range")
            position_values = ["" if missing else point["A"] / 10000000,
                               "" if missing else point["B"] / 10000000, point.get("D", ""),
                               "NOT_PROVIDED" if missing else "REPORTED_NOT_FIX_QUALIFIED"]
        offset += 6 + position_size
        count = data[offset]
        offset += 1
        if len(data) - offset < count * 5:
            raise ValueError("truncated DLV changes")
        changes = []
        for index in range(count):
            column, value = struct.unpack_from("<Bi", data, offset)
            offset += 5
            if column >= len(metadata):
                raise ValueError("DLV column has no template definition")
            changes.append((index, column, value))
        frame = data[start:offset].hex()
        report_bytes += len(frame) + 16
        if report_bytes > LIMIT:
            raise ValueError("source report exceeds bound")
        report["source_records_hex"].append(frame)
        report["source_records"] += 1
        selected = 0
        for index, column, value in changes:
            report["source_updates"] += 1
            ddi, device = metadata[column]
            state_selected = work_state and ddi in (141, 161, 162, 163)
            if ddi not in ACTUAL_RATES and not (harvest and (ddi in HARVEST or ddi == 262)) and not state_selected:
                report["unsupported_updates"] += 1
                continue
            if not state_selected and value < 0:
                raise ValueError("selected DDI outside published range; no sentinel assumed")
            state_values = [""] * len(STATE_FIELDS) if work_state else []
            if state_selected:
                if ddi == 141:
                    if not 0 <= value <= 3:
                        raise ValueError("actual work state outside published range")
                    state_values[0] = STATE_NAMES[value]
                else:
                    # TIM carries a signed Int32; these DDIs define its full bit pattern.
                    bits = value & 0xffffffff
                    state_values[1] = (ddi - 161) * 16 + 1
                    state_values[2:] = [STATE_NAMES[(bits >> (2 * i)) & 3] for i in range(16)]
            harvest_values = []
            if harvest:
                harvest_values = [value * scale if ddi == key else ""
                                  for key, (_, scale) in HARVEST.items()]
                # The published wire range exceeds a physical moisture fraction.
                # Preserve the report, but never clamp it into a plausible value.
                harvest_values += [value if ddi == 262 else "",
                    value / 1000000 if ddi == 262 and value <= 1000000 else "",
                    ("REPORTED" if value <= 1000000 else "OUTSIDE_FRACTION_RANGE") if ddi == 262 else ""]
            writer.writerow([timestamp, report["source_records"], index, column,
                             "hex:" + device.encode().hex(), value,
                             value * ACTUAL_RATES[2][1] if ddi == 2 else "", "hex:" + frame,
                             ddi, value * ACTUAL_RATES[7][1] if ddi == 7 else "",
                             value * ACTUAL_RATES[12][1] if ddi == 12 else ""] + harvest_values + state_values + position_values)
            selected += 1
            report["decoded_updates"] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError("converted CSV exceeds bound")
        if not selected:
            report["records_without_supported_update"] += 1
    if not report["decoded_updates"]:
        raise ValueError("no selected actual updates; setpoint is not actual")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--harvest", action="store_true", help="include qualified yield and average-moisture reports")
    parser.add_argument("--position", action="store_true", help="attach reported same-record PTN north/east to updates")
    parser.add_argument("--work-state", action="store_true", help="include reported actual element/child work states")
    args = parser.parse_args()
    try:
        if args.template.stat().st_size > 65536 or args.data.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(args.template.read_bytes(), args.data.read_bytes(), harvest=args.harvest,
                                 located=args.position, work_state=args.work_state)
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except (ValueError, OSError, ET.ParseError, struct.error):
        parser.exit(2, "ISOXML rate conversion failed; no successful conversion claim\n")


if __name__ == "__main__":
    main()
