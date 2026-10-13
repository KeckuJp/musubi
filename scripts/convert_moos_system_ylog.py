#!/usr/bin/env python3
"""Passive fixed-writer MOOS system ylog; never reconstruct erased source types."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat

LIMIT = 64 * 1024**2
FIELDS = ['time_us', 'source_sha256', 'source_line', 'event_kind', 'event_key_hex',
          'source_name_hex', 'reported_relative_seconds', 'reported_time_text_hex',
          'time_basis', 'payload_text_hex', 'payload_type', 'raw_record_hex']
NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')


def hextext(value):
    return 'hex:' + value.encode('utf-8').hex()


def convert(path, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us <= 2**53-1:
        raise ValueError('explicit exactly representable capture time required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > LIMIT:
            raise ValueError('regular file byte bound')
        raw = stream.read(LIMIT+1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) > LIMIT or len(raw) != before.st_size or identity(before) != identity(after):
        raise ValueError('source changed or exceeds bound')
    body = raw.decode('utf-8', errors='strict')
    if '\x00' in body or '\r' in body:
        raise ValueError('unsupported text control or line ending')
    digest = hashlib.sha256(raw).hexdigest()
    out = io.StringIO(newline=''); writer = csv.writer(out); writer.writerow(FIELDS)
    report = dict(source_sha256=digest, source_bytes=len(raw), physical_lines=0,
                  header_lines=[], blank_lines=0, records=0, qualified_system_events=0,
                  unknown_key_records=0, clock='caller_capture_unknown',
                  payload_type='UNKNOWN_WRITER_ERASED_DOUBLE_VS_TEXT',
                  writer_newlines='removed before storage; not reconstructable')
    for index, line in enumerate(body.splitlines(), 1):
        report['physical_lines'] += 1
        if len(line.encode()) > 1024**2:
            raise ValueError('line bound')
        if line.startswith('%'):
            report['header_lines'].append(dict(line=index, raw_hex=hextext(line)))
            continue
        if not line.strip():
            report['blank_lines'] += 1
            continue
        # Widths are minimums, not truncation. Token sources are a finite condition.
        match = re.fullmatch(r'(\S+)( +)(\S+)( +)(\S+)(.*)', line)
        if not match:
            raise ValueError('unqualified ylog record layout')
        timestamp, pad1, key, pad2, source, remainder = match.groups()
        padding = ' ' * (max(20,len(source))-len(source)+1)
        if (len(timestamp)+len(pad1) != max(10,len(timestamp))+1 or
            len(key)+len(pad2) != max(20,len(key))+1 or
            not remainder.startswith(padding) or not NUMBER.fullmatch(timestamp)):
            raise ValueError('unqualified writer widths or timestamp')
        payload = remainder[len(padding):]
        number = Decimal(timestamp)
        seconds = float(number)
        if not math.isfinite(seconds) or (number != 0 and seconds == 0):
            raise ValueError('relative time is not representable')
        kind = {'MOOS_DEBUG':'REPORTED_DEBUG_EVENT', 'MOOS_SYSTEM':'REPORTED_SYSTEM_EVENT'}.get(
            key.upper(), 'UNKNOWN_KEY_UNQUALIFIED')
        report['records'] += 1
        report['unknown_key_records' if kind == 'UNKNOWN_KEY_UNQUALIFIED' else 'qualified_system_events'] += 1
        writer.writerow([capture_time_us, 'sha256:'+digest, index, kind, hextext(key),
            hextext(source), seconds, hextext(timestamp), 'REPORTED_SECONDS_FROM_LOGGER_APP_START_NOT_UTC',
            hextext(payload), report['payload_type'], hextext(line)])
        if out.tell() > 128*1024**2:
            raise ValueError('output bound')
    return out.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--capture-time-us', required=True, type=int)
    args = parser.parse_args()
    try:
        if args.output.exists(): raise ValueError('output exists')
        text, report = convert(args.input, args.capture_time_us)
        args.output.mkdir()
        (args.output/'observations.csv').write_text(text, encoding='utf-8')
        (args.output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    except Exception:
        parser.exit(2, 'Saved system log conversion failed; no success claim\n')


if __name__ == '__main__':
    main()
