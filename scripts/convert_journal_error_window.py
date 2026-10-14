#!/usr/bin/env python3
"""Saved journal error interval with mandatory original-bound paired evidence."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import convert_journal_storage_errors as journal
from scripts.convert_vda_load_report import read_jsonl_window


def convert_path(path, *, start_record, stop_record, clock):
    selected, offsets, total, size, digest = read_jsonl_window(
        path, start_record=start_record, stop_record=stop_record, allow_blank=True)
    text = ''.join(selected)
    blank_lines = sum(not line.strip() for line in selected)
    converted, semantics, sidecar = journal.convert(text, clock=clock)
    source_sha = 'sha256:' + digest
    selected_sha = 'sha256:' + hashlib.sha256(text.encode()).hexdigest()
    records = [json.loads(line) for line in sidecar.splitlines()]
    records[0].update(source_sha256=source_sha, selected_normalized_sha256=selected_sha,
        source_selection_scope='SELECTED_INTERVAL_ONLY',
        selected_entries=semantics['entries'], original_physical_lines=total)
    reader = csv.DictReader(io.StringIO(converted))
    output = io.StringIO(newline='')
    names = [*reader.fieldnames, 'source_file_sha256', 'source_record_index',
             'source_record_byte_offset', 'selected_normalized_sha256']
    writer = csv.DictWriter(output, names, lineterminator='\n'); writer.writeheader()
    written = 0
    for ordinal, row in enumerate(reader, 1):
        record = records[ordinal]
        local = record['entry_number'] - 1
        if not 0 <= local < len(selected) or row['record_sha256'] != record['record_sha256']:
            raise ValueError('paired journal record accounting mismatch')
        provenance = dict(source_file_sha256=source_sha, source_record_index=start_record+local+1,
            source_record_byte_offset=offsets[local], selected_normalized_sha256=selected_sha)
        row.update(provenance); writer.writerow(row)
        record.update(provenance, selected_entry_number=record['entry_number'],
                      entry_number=start_record+local+1)
        written += 1
        if output.tell() > journal.OUTPUT_LIMIT:
            raise ValueError('journal window output bound')
    if written != len(records)-1 or written != semantics['selected_entries']:
        raise ValueError('paired journal record count mismatch')
    paired = ''.join(json.dumps(record, sort_keys=True)+'\n' for record in records)
    result = output.getvalue()
    if max(len(result.encode()), len(paired.encode())) > journal.OUTPUT_LIMIT:
        raise ValueError('journal window paired byte bound')
    report = dict(schema='musubi.journal-error-window.v1', source_sha256=source_sha,
        selected_normalized_sha256=selected_sha, source_physical_lines=total,
        selected_physical_lines=len(selected), selected_blank_lines=blank_lines,
        outside_physical_lines=total-len(selected),
        start_record=start_record, stop_record_exclusive=stop_record, selected_bytes=size,
        selected_semantics=semantics, output_rows=written, sidecar_records=written,
        outside_semantics='NOT_DECODED_OR_VALIDATED',
        framing_scope='WHOLE_SOURCE_UTF8_PHYSICAL_LINES_ONLY',
        retention='Keep unchanged original externally; selected normalized snapshot is separate',
        output_sha256='sha256:'+hashlib.sha256(result.encode()).hexdigest(),
        sidecar_sha256='sha256:'+hashlib.sha256(paired.encode()).hexdigest())
    return result, report, paired, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path); parser.add_argument('output', type=Path)
    parser.add_argument('--start-record', type=int, required=True)
    parser.add_argument('--stop-record', type=int, required=True)
    parser.add_argument('--clock', choices=tuple(journal.CLOCKS), required=True)
    args = parser.parse_args()
    try:
        output, report, sidecar, selected = convert_path(args.input,
            start_record=args.start_record, stop_record=args.stop_record, clock=args.clock)
        args.output.mkdir(exist_ok=False)
        for name, content in [('observations.csv', output), ('records.jsonl', sidecar),
                              ('selected-source.jsonl', selected),
                              ('report.json', json.dumps(report, indent=2)+'\n')]:
            (args.output/name).write_text(content, encoding='utf-8')
    except (ValueError, OSError, UnicodeError, csv.Error) as error:
        parser.exit(2, 'Saved journal window refused: '+str(error)+'\n')


if __name__ == '__main__':
    main()
