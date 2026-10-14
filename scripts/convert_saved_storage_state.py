#!/usr/bin/env python3
"""Read saved Linux mount / NVMe health reports. Never acquire device state."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

try:
    from scripts.convert_ardupilot_battery_csv import unique_object
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import unique_object

MOUNT = 'linux-psutil-7.0.0-ea5b55605f857affa4e65fa27eb80f4f2bfebd63-disk_partitions-all'
SMART = 'smartctl-7.4-0c0bada5eedb759c9d7bb50ff54959cf0809c5a9-nvme-status'
BITS = ('spare_below_threshold', 'temperature_above_or_below_threshold',
        'reliability_degraded', 'media_read_only', 'volatile_memory_backup_failed',
        'persistent_memory_region_unreliable')
FIELDS = ['capture_time_us', 'subject_hex', 'source_format', 'source_record_index',
          'partition_ordinal', 'partition_count', 'device_hex', 'mountpoint_hex',
          'filesystem_type_hex', 'options_hex', 'mount_access_report', 'health_report',
          'warning_byte', 'unknown_warning_bits', *BITS, 'source_record_hex',
          'source_record_sha256', 'source_sha256', 'association_basis']
LIMIT = 16 * 1024**2
OUTPUT_LIMIT = 128 * 1024**2


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def encoded(value, allow_empty=False):
    if not isinstance(value, str) or len(value.encode()) > 4096 or (not value and not allow_empty):
        raise ValueError('bounded string required')
    return 'hex:' + value.encode().hex()


def convert(data):
    if not data or len(data) > LIMIT:
        raise ValueError('empty or oversized source')
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, FIELDS, lineterminator='\n')
    writer.writeheader()
    report = dict(source_records=0, output_rows=0, partitions=0, empty_inventories=0,
                  missing_health=0, source_sha256=digest(data))
    previous = None
    lines = data.decode('utf-8').split('\n')
    if lines[-1] == '':
        lines.pop()
    for index, line in enumerate(lines, 1):
        raw = line.removesuffix('\r').encode()
        if not raw or len(raw) > 512*1024 or index > 200000:
            raise ValueError('invalid physical record bound')
        record = json.loads(raw, object_pairs_hook=unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        if not isinstance(record, dict) or record.get('source_format') not in (MOUNT, SMART):
            raise ValueError('fixed source declaration required')
        time = record.get('capture_time_us')
        if type(time) is not int or not 0 <= time <= 2**53-1 or (previous is not None and time < previous):
            raise ValueError('nondecreasing exact capture time required')
        previous = time
        row = dict(capture_time_us=time, subject_hex=encoded(record.get('subject')),
                   source_format=record['source_format'], source_record_index=index,
                   source_record_hex='hex:' + raw.hex(), source_record_sha256=digest(raw),
                   source_sha256=report['source_sha256'],
                   association_basis='CALLER_SUBJECT_ONLY_NOT_PHYSICAL_IDENTITY_OR_TIME_SYNCHRONIZATION')
        rows = []
        if record['source_format'] == MOUNT:
            partitions = record.get('partitions')
            if not isinstance(partitions, list) or len(partitions) > 4096:
                raise ValueError('bounded partitions array required')
            row['partition_count'] = len(partitions)
            for ordinal, part in enumerate(partitions):
                if not isinstance(part, dict):
                    raise ValueError('partition object required')
                result = dict(row, partition_ordinal=ordinal)
                for source, target in (('device', 'device_hex'), ('mountpoint', 'mountpoint_hex'),
                                       ('fstype', 'filesystem_type_hex'), ('opts', 'options_hex')):
                    result[target] = encoded(part.get(source), allow_empty=source in ('device', 'opts'))
                tokens = part['opts'].split(',')
                modes = set(tokens) & {'ro', 'rw'}
                result['mount_access_report'] = ('REPORTED_' + next(iter(modes)).upper()
                    if len(modes) == 1 else 'UNKNOWN' if not modes else 'AMBIGUOUS_RO_RW')
                rows.append(result)
            report['partitions'] += len(partitions)
            if not partitions:
                rows.append(dict(row, mount_access_report='REPORTED_EMPTY_MOUNT_INVENTORY'))
                report['empty_inventories'] += 1
        else:
            # The outer subject/device are explicit caller labels, never derived identities.
            row['device_hex'] = encoded(record.get('device'))
            value = record.get('smart_status')
            if value is None:
                row['health_report'] = ('UNKNOWN_NULL_STATUS' if 'smart_status' in record
                                        else 'UNKNOWN_MISSING_STATUS')
                report['missing_health'] += 1
            else:
                if not isinstance(value, dict) or type(value.get('passed')) is not bool:
                    raise ValueError('reported boolean passed required')
                nvme = value.get('nvme')
                if not isinstance(nvme, dict) or type(nvme.get('value')) is not int or not 0 <= nvme['value'] <= 255:
                    raise ValueError('NVMe warning byte required')
                bits = nvme['value']
                if value['passed'] != (bits == 0):
                    raise ValueError('health contradicts fixed writer warning byte')
                row.update(health_report='REPORTED_PASSED' if value['passed'] else 'REPORTED_FAILED',
                           warning_byte=bits, unknown_warning_bits=bits & 0xc0)
                for number, name in enumerate(BITS):
                    expected = bool(bits & (1 << number))
                    if name in nvme and (type(nvme[name]) is not bool or nvme[name] != expected):
                        raise ValueError('warning detail contradicts byte')
                    # Writer omits all details for zero; bit meanings still follow the byte.
                    row[name] = 'reported_true' if expected else 'reported_false'
                if 'other' in nvme and (type(nvme['other']) is not int or nvme['other'] != bits & 0xc0):
                    raise ValueError('unknown warning bits contradict byte')
            rows.append(row)
        for item in rows:
            writer.writerow(item)
            report['output_rows'] += 1
            if output.tell() > OUTPUT_LIMIT:
                raise ValueError('output bound exceeded')
        report['source_records'] += 1
    result = output.getvalue()
    report.update(output_sha256=digest(result.encode()), clock='CALLER_CAPTURE_UNKNOWN',
                  evidence='SOURCE_SHAPED_REPORT_NOT_PHYSICAL_DIAGNOSIS',
                  retention='all original record bytes retained; unknown fields are not interpreted')
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    try:
        if args.output_dir.exists():
            raise ValueError('output exists; no overwrite')
        with args.input.open('rb') as stream:
            data = stream.read(LIMIT+1)
        result, report = convert(data)
        args.output_dir.mkdir()
        (args.output_dir / 'source.jsonl').write_bytes(data)
        (args.output_dir / 'observations.csv').write_text(result, encoding='utf-8')
        (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    except (ValueError, OSError, UnicodeError):
        parser.exit(2, 'Saved storage state refused; check fixed source, types and bounds.\n')


if __name__ == '__main__':
    main()
