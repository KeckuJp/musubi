"""Passive single-process strace 6.16 saved reports, not trace acquisition."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.convert_vda_load_report import read_jsonl_window

PIN = 'ba20408d99c391c937453807285394117f8f3da7'
FIELDS = ['record_time_us', 'source_record_index', 'source_byte_offset', 'source_sha256',
          'source_record_hex', 'session_declared', 'process_id_declared', 'disposition',
          'operation_reported', 'fd_reported', 'target_escaped', 'return_reported',
          'errno_reported', 'error_class', 'requested_bytes_reported', 'offset_bytes_reported',
          'basis']
LINE = re.compile(r'(\d+)\.(\d{6}) (.*)')
CALL = re.compile(r'(write|pwrite64|fsync)\((.*)\)\s+= (-?\d+)(?: ([A-Z][A-Z0-9_]+) \([^\r\n]*\))?')
FD = re.compile(r'(\d+)(?:<([^<>\r\n]*)>)?(.*)')
BUFFER = r'(?:"(?:[^"\\\r\n]|\\.)*"(?:\.\.\.)?|NULL|0x[0-9a-f]+)'
WRITE = re.compile(r', ' + BUFFER + r', (\d+)')
PWRITE = re.compile(r', ' + BUFFER + r', (\d+), (-?\d+)')


def convert_path(path, *, start_record, stop_record, source_commit, session, process_id):
    if source_commit != PIN or not isinstance(session, str) or not session or len(session) > 256:
        raise ValueError('unqualified source/session')
    if type(process_id) is not int or not 0 < process_id <= 2**31 - 1:
        raise ValueError('explicit single-process identity required')
    lines, offsets, count, selected_bytes, digest = read_jsonl_window(
        path, start_record=start_record, stop_record=stop_record)
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    report = dict(source_records=count, selected_records=len(lines),
                  excluded_records_not_decoded=count-len(lines), selected_bytes=selected_bytes,
                  source_sha256=digest, source_commit=PIN, qualified=0, withheld=0, enospc=0,
                  scope='SAVED_SINGLE_PROCESS_REPORTS_NOT_PHYSICAL_VOLUME_FULLNESS')
    previous = None
    for local, line in enumerate(lines):
        raw = line.rstrip('\n')
        if len(raw.encode()) > 32768 or not raw.isascii() or '\x00' in raw:
            raise ValueError('unsupported record encoding/length')
        match = LINE.fullmatch(raw)
        if not match:
            raise ValueError('expected single-process microsecond Unix timestamp')
        timestamp = int(match[1])*1_000_000+int(match[2])
        if timestamp >= 2**63 or (previous is not None and timestamp < previous):
            raise ValueError('invalid or reversed reported time')
        previous = timestamp
        body = match[3]
        row = dict.fromkeys(FIELDS, '')
        row.update(record_time_us=timestamp, source_record_index=start_record+local,
                   source_byte_offset=offsets[local], source_sha256='sha256:'+digest,
                   source_record_hex='hex:'+raw.encode().hex(),
                   session_declared='text:'+quote(session, safe=''), process_id_declared=process_id,
                   disposition='WITHHELD_UNSUPPORTED_OR_INCOMPLETE',
                   basis='SAVED_REPORT_CALLER_PROCESS_SESSION_UNKNOWN_CLOCK_NO_FD_CARRYFORWARD')
        call = CALL.fullmatch(body)
        if call:
            operation, arguments, result, error = call.groups()
            fd = FD.fullmatch(arguments)
            if not fd:
                raise ValueError('malformed selected operation descriptor')
            number, target, tail = fd.groups()
            args = (WRITE if operation == 'write' else PWRITE).fullmatch(tail) if operation != 'fsync' else None
            if (operation == 'fsync' and tail) or (operation != 'fsync' and not args):
                raise ValueError('malformed selected operation arguments')
            if (int(result) < 0) != bool(error) or (error and int(result) != -1):
                raise ValueError('inconsistent reported errno result')
            row.update(operation_reported=operation, fd_reported='integer:'+number,
                       return_reported='integer:'+result, errno_reported=error or '',
                       target_escaped='' if target is None else 'strace-escaped:'+target,
                       requested_bytes_reported='' if args is None else 'integer:'+args[1],
                       offset_bytes_reported='' if operation != 'pwrite64' else 'integer:'+args[2])
            # Each pathname is printed on this operation's fd; never resolve another line's fd.
            if target and target.startswith('/'):
                row['disposition'] = 'QUALIFIED_REPORTED_OPERATION_TARGET_RESULT'
                row['error_class'] = ('REPORTED_NO_SPACE_LEFT_ON_DEVICE' if error == 'ENOSPC'
                                      else 'REPORTED_OTHER_ERRNO_UNINTERPRETED' if error
                                      else 'REPORTED_NONERROR_RESULT')
                report['qualified'] += 1
                report['enospc'] += error == 'ENOSPC'
            else:
                row['disposition'] = 'WITHHELD_TARGET_ABSENT_OR_NOT_ABSOLUTE_PATH'
        report['withheld'] += row['disposition'].startswith('WITHHELD')
        writer.writerow(row)
        if output.tell() > 16*1024*1024:
            raise ValueError('selected output exceeds bound; select a smaller interval')
    rendered = output.getvalue()
    if len(rendered.encode()) > 16*1024*1024:
        raise ValueError('selected output exceeds byte bound')
    report['output_sha256'] = hashlib.sha256(rendered.encode()).hexdigest()
    return rendered, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--start-record', type=int, required=True)
    parser.add_argument('--stop-record', type=int, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--process-id', type=int, required=True)
    args = parser.parse_args()
    try:
        rendered, report = convert_path(args.input, start_record=args.start_record,
            stop_record=args.stop_record, source_commit=args.source_commit,
            session=args.session, process_id=args.process_id)
        args.output.mkdir()
        (args.output/'observations.csv').write_text(rendered, encoding='utf-8')
        (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    except (ValueError, OSError, UnicodeError, csv.Error):
        print('REFUSED: incompatible saved syscall report', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
