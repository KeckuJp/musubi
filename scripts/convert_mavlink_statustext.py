#!/usr/bin/env python3
"""Saved MAVLink2 status-text chunks to reported diagnostic text; no live IO or commands."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

try:
    from .assemble_saved_mavlink_ulog import iter_records, read_bounded, crc_x25, Refused
except ImportError:
    from assemble_saved_mavlink_ulog import iter_records, read_bounded, crc_x25, Refused

MAX_CHUNKS = 200_000
MAX_ACTIVE = 1024
MAX_OUTPUT = 128 * 1024 * 1024
SEVERITIES = ('EMERGENCY', 'ALERT', 'CRITICAL', 'ERROR', 'WARNING', 'NOTICE', 'INFO', 'DEBUG')
FIELDS = ('capture_time_us', 'first_capture_time_us', 'sysid_reported', 'compid_reported',
          'link_tag', 'message_id_reported', 'severity_reported', 'severity_name',
          'assembly_status', 'text_utf8', 'text_hex', 'text_encoding', 'chunk_count',
          'first_chunk_sequence', 'last_chunk_sequence', 'source_sha256', 'frame_references_hex')


def convert(data, *, timestamp_layout):
    """One caller-associated producer session; never infer a boot boundary or authenticate IDs."""
    if timestamp_layout not in ('unsigned-us', 'mavproxy-link'):
        raise Refused('TIMESTAMP_LAYOUT_REQUIRED')
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    completed_rows = []
    digest = hashlib.sha256(data).hexdigest()
    active = {}
    counts = dict(source_records=0, selected_chunks=0, unselected_records=0,
                  observations=0, complete_messages=0, incomplete_segments=0,
                  invalid_utf8_segments=0)
    output_bytes = len(output.getvalue().encode())

    def emit(group, status):
        nonlocal output_bytes
        text = b''.join(group['pieces'])
        try:
            decoded = text.decode('utf-8')
            encoding = 'UTF8'
        except UnicodeDecodeError:
            decoded, encoding = '', 'INVALID_UTF8_BYTES_RETAINED'
            counts['invalid_utf8_segments'] += 1
        refs = group['refs']
        row = dict(capture_time_us=refs[-1]['capture_time_us'],
                   first_capture_time_us=refs[0]['capture_time_us'],
                   sysid_reported=group['key'][1], compid_reported=group['key'][2],
                   link_tag=group['key'][0], message_id_reported=group['key'][3],
                   severity_reported=group['severity'],
                   severity_name=SEVERITIES[group['severity']] if group['severity'] < 8 else 'UNKNOWN',
                   assembly_status=status, text_utf8=('utf8:' + decoded) if encoding == 'UTF8' else '',
                   text_hex='hex:' + text.hex(),
                   text_encoding=encoding, chunk_count=len(refs),
                   first_chunk_sequence=refs[0]['chunk_sequence'],
                   last_chunk_sequence=refs[-1]['chunk_sequence'], source_sha256='sha256:' + digest,
                   frame_references_hex=json.dumps(refs, separators=(',', ':')).encode().hex())
        row_buffer = io.StringIO(newline='')
        csv.DictWriter(row_buffer, fieldnames=FIELDS, lineterminator='\n').writerow(row)
        encoded_row = row_buffer.getvalue()
        output_bytes += len(encoded_row.encode('utf-8'))
        if output_bytes > MAX_OUTPUT:
            raise Refused('OUTPUT_TOO_LARGE')
        completed_rows.append((refs[-1]['capture_time_us'], refs[-1]['source_record_index'], encoded_row))
        counts['observations'] += 1
        counts['complete_messages' if status == 'COMPLETE_REPORTED_TEXT' else 'incomplete_segments'] += 1

    for record in iter_records(data):
        index = counts['source_records']
        counts['source_records'] += 1
        if record['msgid'] != 253:
            counts['unselected_records'] += 1
            continue
        if record['mavlink_version'] != 2:
            raise Refused('SELECTED_MAVLINK1_OUT_OF_SCOPE')
        payload = bytes(record['wire_payload'])
        if not 1 <= len(payload) <= 54:
            raise Refused('STATUSTEXT_PAYLOAD_LENGTH')
        if crc_x25(bytes([83]), crc_x25(record['crc_input'])) != record['crc']:
            raise Refused('CRC_MISMATCH')
        counts['selected_chunks'] += 1
        if counts['selected_chunks'] > MAX_CHUNKS:
            raise Refused('TOO_MANY_SELECTED_CHUNKS')
        payload = payload.ljust(54, b'\0')
        severity, identity, sequence = payload[0], int.from_bytes(payload[51:53], 'little'), payload[53]
        chunk = payload[1:51]
        terminated = b'\0' in chunk
        piece = chunk.split(b'\0', 1)[0]
        raw_time = record['recorded_timestamp_word']
        capture_time = raw_time if timestamp_layout == 'unsigned-us' else raw_time & ~3
        link = '' if timestamp_layout == 'unsigned-us' else raw_time & 3
        if capture_time > 0x7fffffffffffffff:
            raise Refused('CAPTURE_TIME_OUT_OF_RANGE')
        key = (link, record['sysid'], record['compid'], identity)
        reference = dict(source_record_index=index, tlog_offset=record['tlog_offset'],
                         recorded_timestamp_word=raw_time, capture_time_us=capture_time,
                         chunk_sequence=sequence, mavlink_sequence=record['mavlink_sequence'],
                         signed_but_not_authenticated=record['signed_frame'],
                         wire_payload_hex=bytes(record['wire_payload']).hex())
        group = active.get(key)
        if group is not None and (sequence == 0 or sequence != group['refs'][-1]['chunk_sequence'] + 1
                                  or severity != group['severity']):
            reason = 'RESTART_BEFORE_TERMINATOR' if sequence == 0 else (
                'SEVERITY_CHANGED' if severity != group['severity'] else 'NONCONTIGUOUS_SEQUENCE')
            emit(group, 'INCOMPLETE_' + reason)
            del active[key]
            group = None
        if group is None:
            group = dict(key=key, severity=severity, pieces=[], refs=[], starts_at_zero=sequence == 0)
        group['pieces'].append(piece)
        group['refs'].append(reference)
        if identity == 0 or terminated:
            complete = identity == 0 or group['starts_at_zero']
            emit(group, 'COMPLETE_REPORTED_TEXT' if complete else 'INCOMPLETE_MISSING_PREFIX')
            active.pop(key, None)
        else:
            active[key] = group
            if len(active) > MAX_ACTIVE:
                raise Refused('TOO_MANY_ACTIVE_MESSAGES')
    for group in active.values():
        emit(group, 'INCOMPLETE_EOF_NO_TERMINATOR' if group['starts_at_zero']
             else 'INCOMPLETE_PREFIX_AND_TERMINATOR_MISSING')
    if counts['selected_chunks'] == 0:
        raise Refused('NO_SELECTED_STATUSTEXT')
    for _, _, encoded_row in sorted(completed_rows):
        output.write(encoded_row)
    csv_text = output.getvalue()
    return csv_text, dict(schema='musubi.saved-statustext.v1', **counts,
                         source_sha256=digest, csv_sha256=hashlib.sha256(csv_text.encode()).hexdigest(),
                         timestamp_layout=timestamp_layout, clock_basis='Unknown',
                         claim='REPORTED_TEXT_ONLY_NOT_AUTHENTICATED_OR_PHYSICAL_FAILURE',
                         other_message_crc='NOT_CHECKED',
                         limits=dict(selected_chunks=MAX_CHUNKS, active_messages=MAX_ACTIVE,
                                     csv_bytes=MAX_OUTPUT, input_bytes=64 * 1024 * 1024))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--timestamp-layout', choices=('unsigned-us', 'mavproxy-link'), required=True)
    args = parser.parse_args()
    try:
        data = read_bounded(args.input)
        csv_text, report = convert(data, timestamp_layout=args.timestamp_layout)
        args.output.mkdir(exist_ok=False)
        (args.output / 'source.tlog').write_bytes(data)
        (args.output / 'statustext.csv').write_text(csv_text, encoding='utf-8')
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    except (ValueError, OSError) as error:
        print(error.code if isinstance(error, Refused) else 'SAVED_STATUSTEXT_IO_FAILURE', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
