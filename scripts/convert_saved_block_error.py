"""Supplied Linux6.12 block_rq_error payloads -> passive common observations.

The JSONL envelope is caller-created, not a kernel export format. No tracing,
device access, authentication, physical diagnosis or reconstruction is performed.
"""
import csv
import hashlib
import io
import json
import re

from scripts.convert_journal_storage_errors import ERRNO_NAMES, ERRNO_CLASSES, UNRECOGNISED
from scripts.convert_position_csv import unique_object, invalid_constant
from scripts.convert_vda_load_report import read_jsonl_window

SOURCE_FORMAT = 'linux-6.12-block-rq-error-payload'
PIN = 'adc218676eef25575469234709c2d87185ca223a'
LIMIT = 128 * 1024**2
PATTERN = re.compile(r'([0-9]+),([0-9]+) ([A-Z]+) \(\) ([0-9]+) \+ ([0-9]+) '
                     r'([A-Za-z0-9]+),([0-9]+),([0-9]+) \[(-[0-9]+)\]')
OPS = {'W': 'WRITE', 'R': 'READ', 'D': 'DISCARD', 'DE': 'SECURE_ERASE',
       'F': 'FLUSH', 'N': 'UNKNOWN_OPERATION'}
# Exact formatter: optional preflush, one operation, then FUA/RAHEAD/SYNC/META.
RWBS = {}
for prefix in ('', 'F'):
    for op, name in OPS.items():
        for bits in range(16):
            suffix = ''.join(flag for i, flag in enumerate('FASM') if bits & (1 << i))
            RWBS[prefix+op+suffix] = name
FIELDS = ['capture_time_us', 'declared_clock_basis_hex', 'source_id_hex', 'event',
          'operation', 'rwbs', 'write_assertion', 'device_major', 'device_minor',
          'target_status', 'sector_start_512b_hex', 'request_sector_count',
          'request_sector_bytes', 'reported_negative_errno', 'errno_name', 'errno_class',
          'ioprio_class_hex', 'ioprio_hint', 'ioprio_level', 'source_record_index',
          'source_record_byte_offset', 'source_record_hex', 'source_record_sha256',
          'source_file_sha256', 'meaning_basis']


def encoded(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError('bounded nonempty declaration required')
    return 'hex:' + value.encode().hex()


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def convert_path(path, *, start_record, stop_record):
    selected, offsets, total, size, digest = read_jsonl_window(
        path, start_record=start_record, stop_record=stop_record)
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, FIELDS, lineterminator='\n'); writer.writeheader()
    report = dict(source_sha256='sha256:'+digest, source_physical_lines=total,
                  selected_physical_lines=len(selected), outside_physical_lines=total-len(selected),
                  selected_bytes=size, write_failure_reports=0, other_operation_reports=0,
                  unknown_operation_reports=0, unknown_target_reports=0, output_rows=0,
                  outside_semantics='NOT_DECODED_OR_VALIDATED', source_pin=PIN,
                  input_basis='CALLER_ENVELOPE_OF_SAVED_EXACT_EVENT_PAYLOAD_NOT_AUTHENTICATED',
                  clock_basis='UNKNOWN_NOT_SYNCHRONIZED')
    previous = None
    for index, line in enumerate(selected):
        raw = line.rstrip('\r\n').encode()
        record = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        if not isinstance(record, dict) or record.get('source_format') != SOURCE_FORMAT:
            raise ValueError('fixed declared event source required')
        if record.get('errno_abi') != 'linux-6.12-asm-generic' or record.get('event') != 'block_rq_error':
            raise ValueError('fixed errno ABI and error event required')
        time = record.get('capture_time_us')
        if type(time) is not int or not 0 <= time <= 2**53-1 or (previous is not None and time < previous):
            raise ValueError('exact nondecreasing capture time required')
        previous = time
        payload = record.get('payload')
        match = PATTERN.fullmatch(payload) if isinstance(payload, str) and len(payload) <= 4096 else None
        if match is None:
            raise ValueError('fixed printed event payload required')
        major, minor, rwbs, sector, count, priority, hint, level, error = match.groups()
        major, minor, sector, count, hint, level, error = map(int, (major, minor, sector, count, hint, level, error))
        if (major > 4095 or minor > 1048575 or sector > 2**64-1 or count > (2**32-1)>>9
                or hint > 65535 or level > 65535 or not -4095 <= error < 0):
            raise ValueError('event field range invalid')
        operation = RWBS.get(rwbs, 'UNKNOWN_OPERATION')
        target = 'UNKNOWN_NO_DISK_DEVICE' if major == minor == 0 else 'REPORTED_BLOCK_DEVICE_AND_SECTOR_NOT_FILE'
        row = dict(capture_time_us=time, declared_clock_basis_hex=encoded(record.get('clock_basis')),
            source_id_hex=encoded(record.get('source_id')), event='block_rq_error', operation=operation,
            rwbs=rwbs, write_assertion=('REPORTED_PARTIAL_REQUEST_WRITE_FAILURE' if operation == 'WRITE'
                                     else 'NOT_ESTABLISHED_UNKNOWN_OPERATION' if operation == 'UNKNOWN_OPERATION'
                                     else 'NOT_A_REPORTED_WRITE'),
            device_major=major, device_minor=minor, target_status=target,
            sector_start_512b_hex='hex:'+str(sector).encode().hex(), request_sector_count=count,
            request_sector_bytes=count*512, reported_negative_errno=error,
            errno_name=ERRNO_NAMES.get(-error, 'UNKNOWN_ERRNO'), errno_class=ERRNO_CLASSES.get(-error, UNRECOGNISED),
            ioprio_class_hex='hex:'+priority.encode().hex(), ioprio_hint=hint, ioprio_level=level,
            source_record_index=start_record+index+1, source_record_byte_offset=offsets[index],
            source_record_hex='hex:'+raw.hex(), source_record_sha256=sha(raw), source_file_sha256=report['source_sha256'],
            meaning_basis='DRIVER_REPORTED_PARTIAL_REQUEST_NOT_PHYSICAL_CAUSE_OR_SYSCALL_RESULT')
        writer.writerow(row)
        report['output_rows'] += 1
        report['write_failure_reports' if operation == 'WRITE' else
               'unknown_operation_reports' if operation == 'UNKNOWN_OPERATION' else 'other_operation_reports'] += 1
        report['unknown_target_reports'] += target.startswith('UNKNOWN')
        if output.tell() > LIMIT:
            raise ValueError('output byte limit')
    value = output.getvalue()
    if len(value.encode()) > LIMIT:
        raise ValueError('output byte limit')
    report['output_sha256'] = sha(value.encode())
    return value, report
