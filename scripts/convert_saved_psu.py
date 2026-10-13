#!/usr/bin/env python3
"""Saved, caller-associated Linux v6.12 Corsair PSU attributes to common CSV.

Never opens attribute paths, probes hardware or executes commands.
The JSON envelope is this bridge's capture packaging, not a native Linux export format.
"""
import csv
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from scripts.recorded_units import normalize
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from recorded_units import normalize
    from convert_position_csv import unique_object, invalid_constant

PIN = "adc218676eef25575469234709c2d87185ca223a"
MAX_RECORD = 64 * 1024
KEYS = {"schema", "source_pin", "driver", "native_units", "source_id",
        "association_basis", "capture_time_us", "attributes"}
# Mapping is fixed by corsair-psu.c read callbacks + hwmon ABI, never a name heuristic.
MEASUREMENTS = {
    **{f"in{i}_input": ("mV", "voltage_v", "V") for i in range(4)},
    **{f"curr{i}_input": ("mA", "current_a", "A") for i in range(1, 5)},
    **{f"power{i}_input": ("uW", "power_w", "W") for i in range(1, 5)},
}
HEADER = ["record_time_us", "source_record_index", "source_id_hex", "association_basis_hex",
          "source_pin", "driver", "identity_basis", "alarm_status", "attribute_count",
          "unknown_attribute_count", "unknown_attribute_names_hex", "source_record_sha256",
          "source_record_hex"]
for attribute, (_, field, unit) in MEASUREMENTS.items():
    HEADER += [f"{attribute}_{field} ({unit})", f"{attribute}_availability"]

# Fixed Linux pmbus.h + ibm-cffps.c; labels denote reported bits, not diagnoses.
STATUS_BITS = {
    '': dict(enumerate(('none_above', 'cml', 'temperature', 'vin_uv', 'iout_oc',
                       'vout_ov', 'off', 'busy', 'unknown', 'other', 'fans',
                       'power_good_n', 'manufacturer', 'input', 'iout_pout', 'vout'))),
    '_input': dict(enumerate(('pin_op_warning', 'iin_oc_warning', 'iin_oc_fault',
                             'vin_off', 'uv_fault', 'uv_warning', 'ov_warning', 'ov_fault'))),
    '_vout': {4:'uv_fault', 5:'uv_warning', 6:'ov_warning', 7:'ov_fault'},
    '_iout': dict(enumerate(('pout_op_warning', 'pout_op_fault', 'power_limiting',
                            'current_share_fault', 'uc_fault', 'oc_warning', 'oc_lv_fault', 'oc_fault'))),
    '_temp': {4:'ut_fault', 5:'ut_warning', 6:'ot_warning', 7:'ot_fault'},
    '_fan12': dict(enumerate(('airflow_warning', 'airflow_fault', 'fan2_override',
                             'fan1_override', 'fan2_warning', 'fan1_warning', 'fan2_fault', 'fan1_fault'))),
    '_mfr': dict(enumerate(('fan_fault', 'thermal_fault', 'ov_fault', 'uv_fault',
                           'ps_kill', 'oc_fault', 'vaux_fault', 'current_share_warning'))),
    '_cml': {0:'other_memory_logic', 1:'other_communication', 3:'processor', 4:'memory',
             5:'packet_error', 6:'invalid_data', 7:'invalid_command'},
    '_other': {},  # No bit meanings declared by this fixed header; retain all as unknown.
}
STATUS_HEADER = HEADER[:13] + ['page', 'status_width', 'physical_ac_presence', 'primary_status_coverage']
for suffix, bits in STATUS_BITS.items():
    prefix = 'reported_status' + suffix
    STATUS_HEADER += [prefix+'_code', prefix+'_availability', prefix+'_unknown_bits']
    STATUS_HEADER += [prefix+'_'+label for label in bits.values()]


def encoded(text):
    return "hex:" + text.encode("utf-8").hex()


def row(raw, index, *, pmbus_status=False):
    record = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if type(record) is not dict or set(record) != KEYS | ({'page', 'status_width'} if pmbus_status else set()):
        raise ValueError("exact saved capture envelope required")
    if pmbus_status:
        if (record['schema'] != 'saved-ibm-pmbus-status/v1' or record['source_pin'] != PIN
                or record['driver'] not in ('ibm_cffps1', 'ibm_cffps2')
                or record['native_units'] != 'linux-pmbus-debugfs'
                or type(record['page']) is not int
                or record['page'] not in (range(1) if record['driver']=='ibm_cffps1' else range(2))
                or type(record['status_width']) is not int or record['status_width'] not in (8, 16)):
            raise ValueError('fixed PMBus driver/page/status width required')
    elif (record["schema"] != "saved-corsair-psu/v1" or record["source_pin"] != PIN
            or record["driver"] != "corsair-psu" or record["native_units"] != "linux-hwmon-abi"):
        raise ValueError("unqualified driver, schema or native unit declaration")
    for name in ("source_id", "association_basis"):
        if type(record[name]) is not str or not record[name].strip() or len(record[name]) > 4096:
            raise ValueError("explicit source association required")
    time = record["capture_time_us"]
    if type(time) is not int or not 0 <= time <= 2**63 - 1:
        raise ValueError("capture time must be exact nonnegative microseconds")
    attributes = record["attributes"]
    if type(attributes) is not dict or len(attributes) > 1024:
        raise ValueError("saved native attributes required")
    for name, value in attributes.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", name):
            raise ValueError("attribute name must be a basename, not a path")
        if value is not None and type(value) is not str:
            raise ValueError("native saved text or explicit unavailable null required")
    if pmbus_status:
        return status_row(record, raw, index)
    if attributes.get("name") not in ("corsairpsu", "corsairpsu\n"):
        raise ValueError("selected driver's mandatory saved name must match")
    unknown = sorted(set(attributes) - set(MEASUREMENTS) - {"name"})
    output = [time, index, encoded(record["source_id"]), encoded(record["association_basis"]),
              PIN, "corsair-psu", "CALLER_ASSOCIATION_NOT_AUTHENTICATED",
              "NOT_EXPOSED_BY_SELECTED_DRIVER", len(attributes), len(unknown),
              encoded(json.dumps(unknown, separators=(",", ":"))),
              "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(), encoded(raw)]
    for name, (unit, field, _) in MEASUREMENTS.items():
        if name not in attributes:
            output += ["", "NOT_CAPTURED"]
        elif attributes[name] is None:
            output += ["", "CAPTURE_UNAVAILABLE"]
        else:
            value = attributes[name]
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,15})\n?", value):
                raise ValueError("selected native attribute is not an integer string")
            actual_field, number = normalize(int(value), unit)
            if actual_field != field:
                raise ValueError("normalized unit contract mismatch")
            output += [number, "REPORTED"]
    return output


def status_row(record, raw, index):
    attributes = record['attributes']; page = record['page']; width = record['status_width']
    recognized = {'status'+str(page)+suffix for suffix in STATUS_BITS}
    if page == 1:
        recognized.remove('status1_fan12')
    for name in attributes:
        match = re.fullmatch(r'status([0-9]+)(?:_.*)?', name)
        if match and int(match[1]) != page:
            raise ValueError('mixed page capture is not one page state')
    if page == 1 and 'status1_fan12' in attributes:
        raise ValueError('fan12 not exposed on selected page')
    unknown = sorted(set(attributes)-recognized)
    primary = ['status'+str(page)+suffix for suffix in ('', '_input', '_vout', '_iout')]
    output = [record['capture_time_us'], index, encoded(record['source_id']),
              encoded(record['association_basis']), PIN, record['driver'],
              'CALLER_ASSOCIATION_NOT_AUTHENTICATED', 'REPORTED_BITS_NOT_PHYSICAL_DIAGNOSIS',
              len(attributes), len(unknown), encoded(json.dumps(unknown, separators=(',', ':'))),
              'sha256:'+hashlib.sha256(raw.encode()).hexdigest(), encoded(raw), page, width,
              'UNKNOWN_NOT_A_CABLE_PRESENCE_TEST',
              'ALL_PRIMARY_REGISTERS_REPORTED' if all(attributes.get(n) is not None for n in primary)
              else 'INCOMPLETE_PRIMARY_REGISTERS']
    for suffix, bits in STATUS_BITS.items():
        name = 'status'+str(page)+suffix
        availability = 'REPORTED'
        value = attributes.get(name)
        if name not in recognized:
            availability = 'NOT_EXPOSED_ON_PAGE'
        elif name not in attributes:
            availability = 'NOT_CAPTURED'
        elif value is None:
            availability = 'CAPTURE_UNAVAILABLE'
        if availability != 'REPORTED':
            output += ['', availability, ''] + ['']*len(bits)
            continue
        digits = 4 if not suffix else 2
        if not re.fullmatch(r'0x[0-9a-f]{'+str(digits)+r'}\n?', value):
            raise ValueError('exact saved debugfs hex required')
        number = int(value, 16)
        if not suffix and number >= 2**width:
            raise ValueError('status value exceeds declared register width')
        mask = sum(1<<bit for bit in bits)
        output += [number, availability, number & ~mask]
        output += [('true' if number & (1<<bit) else 'false')
                   if suffix or bit < width else '' for bit in bits]
    return output


def convert(source, destination, *, pmbus_status=False):
    """Stream validated bounded records; equal capture times are not temporal ordering proof.

    Library caller owns output atomicity. CLI validates into a spool before creating output.
    No total-log size/record bound: memory is bounded by one native capture record.
    """
    if type(pmbus_status) is not bool:
        raise ValueError('explicit boolean selector required')
    writer = csv.writer(destination, lineterminator="\n")
    writer.writerow(STATUS_HEADER if pmbus_status else HEADER)
    count, previous = 0, None
    while True:
        line = source.readline(MAX_RECORD + 1)
        if not line:
            break
        if len(line.encode("utf-8")) > MAX_RECORD:
            raise ValueError("individual capture record exceeds 64 KiB")
        raw = line.rstrip("\r\n")
        if not raw:
            raise ValueError("empty capture record")
        values = row(raw, count, pmbus_status=pmbus_status)
        if previous is not None and values[0] < previous:
            raise ValueError("capture time decreased; retain separate source segments")
        writer.writerow(values)
        previous = values[0]
        count += 1
    if not count:
        raise ValueError("capture has no records")
    return count


def main(argv):
    pmbus_status = len(argv) == 3 and argv[2] == '--pmbus-status'
    if pmbus_status:
        argv = argv[:2]
    if len(argv) != 2:
        print("usage: convert_saved_psu.py SAVED.jsonl NEW.csv [--pmbus-status]", file=sys.stderr)
        return 1
    try:
        source = Path(argv[0]).resolve()
        if source.parts[1:2] in (("sys",), ("proc",), ("dev",)) or not source.is_file():
            raise ValueError("saved regular input file required")
        with source.open(encoding="utf-8", newline="") as stream, tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as spool:
            count = convert(stream, spool, pmbus_status=pmbus_status)
            spool.seek(0)
            with Path(argv[1]).open("x", encoding="utf-8", newline="") as output:
                shutil.copyfileobj(spool, output)
        print(f"converted {count} saved snapshots; not device verification")
        return 0
    except (ValueError, OSError, UnicodeError, RecursionError):
        print("PSU conversion failed; verify saved input and a new output path", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
