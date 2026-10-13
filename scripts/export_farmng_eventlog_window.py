"""Bounded native-record windows for the existing saved farm-ng FilterState path."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

try:
    from scripts import export_farmng_eventlog_filter_state as legacy
except ModuleNotFoundError:
    import export_farmng_eventlog_filter_state as legacy

MAX_INPUT_BYTES = 2 * 1024**3
MAX_RECORDS = 5_000_000
MAX_SELECTED_BYTES = 16 * 1024**2
MAX_SELECTED_RECORDS = 65_536
REPORT_LIMIT = 16 * 1024**2


def _signature(status):
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def export_filter_state_window(path, *, start_record, stop_record, event_path,
                               clock_name, semantics, max_part_bytes=legacy.BRIDGE_LIMIT):
    """Scan native framing, reuse selected decoder, preserve original coordinates."""
    if (type(start_record) is not int or type(stop_record) is not int or
            not 0 <= start_record < stop_record <= MAX_RECORDS):
        raise ValueError('invalid EventLog record window bound')
    if not event_path or not clock_name or not semantics:
        raise ValueError('explicit event path and source clock selection required')
    if type(max_part_bytes) is not int or not 1 <= max_part_bytes <= legacy.BRIDGE_LIMIT:
        raise ValueError('invalid part limit')
    event_class, _ = legacy.selected_messages()
    selected, offsets, excluded = bytearray(), [], {}
    source_hash = hashlib.sha256()
    records = source_bytes = 0
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('EventLog input must be a regular file')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_INPUT_BYTES:
            raise ValueError('EventLog input bound exceeded')
        while True:
            event, raw = legacy.read_event_record(stream, event_class)
            source_hash.update(raw)
            offset = source_bytes
            source_bytes += len(raw)
            if source_bytes > MAX_INPUT_BYTES:
                raise ValueError('EventLog input bound exceeded')
            if event is None:
                if raw:
                    raise ValueError('incomplete trailing EventLog record')
                break
            if records >= MAX_RECORDS:
                raise ValueError('EventLog record count bound exceeded')
            inside = start_record <= records < stop_record
            if inside:
                if len(offsets) >= MAX_SELECTED_RECORDS or len(selected) + len(raw) > MAX_SELECTED_BYTES:
                    raise ValueError('EventLog selected interval bound exceeded')
                offsets.append(offset)
                selected.extend(raw)
            if not inside or event.uri.path != event_path:
                excluded[event.uri.path] = excluded.get(event.uri.path, 0) + 1
            if len(set(excluded) | {event_path}) > legacy.DISTINCT_PATHS:
                raise ValueError('EventLog distinct path bound exceeded')
            records += 1
        if _signature(before) != _signature(os.fstat(stream.fileno())) or source_bytes != before.st_size:
            raise ValueError('EventLog source changed during read')
    if stop_record > records:
        raise ValueError('EventLog record window exceeds source bound')
    with tempfile.TemporaryDirectory(prefix='musubi-farmng-window-') as temp:
        interval = Path(temp) / 'selected.bin'
        interval.write_bytes(selected)
        old_parts, report = legacy.export_filter_state_events(
            interval, event_path=event_path, clock_name=clock_name, semantics=semantics,
            max_part_bytes=max_part_bytes)
    digest = source_hash.hexdigest()
    parts, current, part_bytes, total_bytes = [], [], 0, 0
    for old_part in old_parts:
        for line in old_part.splitlines():
            envelope = json.loads(line)
            source = envelope['source']
            local = source['record_index']
            source.update(record_index=start_record + local, record_offset=offsets[local], file_sha256=digest)
            line = json.dumps(envelope, allow_nan=False) + '\n'
            size = len(line.encode('utf-8'))
            if size > max_part_bytes:
                raise ValueError('single bound record exceeds the part limit')
            total_bytes += size
            if total_bytes > legacy.OUTPUT_LIMIT:
                raise ValueError('EventLog rebound output bound exceeded')
            if part_bytes + size > max_part_bytes:
                parts.append(''.join(current))
                current, part_bytes = [], 0
            current.append(line)
            part_bytes += size
    if current:
        parts.append(''.join(current))
    report.update(input_bytes=source_bytes, input_sha256=digest, source_records=records,
                  unselected_records=records-report['selected_records'],
                  unselected_paths=dict(sorted(excluded.items())), output_bytes=total_bytes,
                  selection_sha256=hashlib.sha256(selected).hexdigest(),
                  parts=[{'records':p.count('\n'), 'bytes':len(p.encode('utf-8')),
                          'sha256':hashlib.sha256(p.encode('utf-8')).hexdigest()} for p in parts],
                  record_window={'start_record':start_record, 'stop_record_exclusive':stop_record,
                      'interval_records':len(offsets), 'outside_records':records-len(offsets),
                      'outside_payload':'NOT_DECODED_OR_VALIDATED',
                      'bounds':{'input_bytes':MAX_INPUT_BYTES, 'records':MAX_RECORDS,
                                'selected_bytes':MAX_SELECTED_BYTES, 'selected_records':MAX_SELECTED_RECORDS}})
    if len((json.dumps(report, indent=2, sort_keys=True)+'\n').encode('utf-8')) > REPORT_LIMIT:
        raise ValueError('EventLog report bound exceeded')
    return parts, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('output',type=Path,help='new directory; original source must be retained')
    parser.add_argument('--start-record',type=int,required=True)
    parser.add_argument('--stop-record',type=int,required=True)
    parser.add_argument('--event-path',required=True)
    parser.add_argument('--timestamp-clock',required=True)
    parser.add_argument('--timestamp-semantics',required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink() or args.output.resolve() == args.input.resolve():
            raise ValueError('output must be new and different from input')
        parts, report = export_filter_state_window(args.input,start_record=args.start_record,
            stop_record=args.stop_record,event_path=args.event_path,clock_name=args.timestamp_clock,
            semantics=args.timestamp_semantics)
        legacy.write_parts(parts,report,args.output,True)
        with (args.output/'report.json').open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(json.dumps(report,indent=2,sort_keys=True)+'\n')
    except (OSError,ValueError) as exc:
        print(f'saved EventLog window refused: {exc}',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
