#!/usr/bin/env python3
"""Carry quoted CAP info text once, related to unchanged saved area observations."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

try:
    from . import convert_cap_alert as cap
except ImportError:
    import convert_cap_alert as cap

MAX_OUTPUT_BYTES = cap.MAX_OUTPUT
TEXT_NAMES = ('headline', 'description', 'instruction')
AUDIENCE_NAMES = ('addresses', 'restriction')
AUDIENCE_FIELDS = tuple(f'alert_{name}_{suffix}' for name in AUDIENCE_NAMES
                        for suffix in ('hex', 'disposition')) + ('audience_basis',)
CONTEXT = ('record_time_us', 'alert_sender', 'alert_identifier', 'alert_sent_text',
           'alert_sent_offset', 'cap_time_basis', 'alert_status', 'alert_msg_type',
           'alert_scope', 'info_index', 'info_count', 'info_language_hex',
           'info_categories', 'info_event_hex', 'info_urgency', 'info_severity',
           'info_certainty', 'info_effective_text', 'info_onset_text', 'info_expires_text',
           'area_count', 'source_sha256', 'classification_basis')
FIELDS = (*CONTEXT, 'cap_info_state', 'source_info_path', 'base_csv_sha256',
          'base_row_start', 'base_row_count',
          *(f'info_{name}_{suffix}' for name in TEXT_NAMES for suffix in ('hex', 'disposition')),
          'text_basis')
TEXT_BASIS = 'PRODUCER_QUOTED_XML_CHARACTERS_NOT_EXECUTED_NOT_RECOMMENDED_NOT_AUTHORITY_SOURCE_XML_RETAINS_EXACT_BYTES'


def convert(data, *, include_audience=False):
    if type(include_audience) is not bool:
        raise ValueError('include_audience must be an explicit boolean')
    # Both readings use the same bounded, entity-refusing parser. The ordinary
    # converter owns validation/classification/time/area semantics and stays unchanged.
    base, base_report = cap.convert(data, output_budget=MAX_OUTPUT_BYTES)
    root = cap.read_alert(data)
    audience = {}
    audience_selected = {}
    if include_audience:
        for name in AUDIENCE_NAMES:
            value = cap.text_of(cap.child(root, name))
            audience[f'alert_{name}_hex'] = cap.hexed(value)
            audience[f'alert_{name}_disposition'] = ('ABSENT' if value is None else
                'PRESENT_EMPTY' if value == '' else 'REPORTED_TEXT')
            audience_selected[name] = int(value is not None)
        audience['audience_basis'] = 'PRODUCER_DECLARED_AUDIENCE_TEXT_NOT_RESOLVED_AUTHENTICATED_OR_AUTHORIZATION_SCOPE_NOT_RECONCILED'
    fields = (*FIELDS, *AUDIENCE_FIELDS) if include_audience else FIELDS
    infos = cap.children(root, 'info')
    previous = csv.field_size_limit(cap.MAX_OUTPUT)
    try:
        base_rows = list(csv.DictReader(io.StringIO(base)))
    finally:
        csv.field_size_limit(previous)
    base_digest = hashlib.sha256(base.encode('utf-8')).hexdigest()
    groups = {}
    for index, row in enumerate(base_rows):
        groups.setdefault(row['info_index'], []).append((index, row))

    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(fields)
    written = len(base.encode('utf-8')) + len(output.getvalue().encode('utf-8'))
    selected = {name: 0 for name in TEXT_NAMES}
    for index, element in enumerate(infos or [None]):
        group = groups[str(index) if element is not None else '']
        start, first = group[0]
        # Explicit positional relation within generated output, never a time/name join.
        if [i for i, _ in group] != list(range(start, start + len(group))):
            raise ValueError('CAP info area rows are not contiguous')
        row = {key: first[key] for key in CONTEXT}
        row.update(audience)
        row.update(cap_info_state='INFO_PRESENT' if element is not None else 'ALERT_WITHOUT_INFO',
                   source_info_path=f'/alert/info[{index}]' if element is not None else '/alert',
                   base_csv_sha256=base_digest, base_row_start=start, base_row_count=len(group),
                   text_basis=TEXT_BASIS)
        for name in TEXT_NAMES:
            value = cap.text_of(cap.child(element, name)) if element is not None else None
            row[f'info_{name}_hex'] = cap.hexed(value)
            row[f'info_{name}_disposition'] = ('ABSENT' if value is None else
                                               'PRESENT_EMPTY' if value == '' else 'REPORTED_TEXT')
            selected[name] += int(value is not None)
        buffer = io.StringIO(newline='')
        csv.writer(buffer, lineterminator='\n').writerow([row[key] for key in fields])
        chunk = buffer.getvalue()
        written += len(chunk.encode('utf-8'))
        if written > MAX_OUTPUT_BYTES:
            raise ValueError('CAP bundle output exceeds byte bound; nothing truncated')
        output.write(chunk)

    omissions = dict(base_report['known_elements_not_carried'])
    for name, count in selected.items():
        if omissions.pop('info/' + name, 0) != count:
            raise ValueError('CAP selected text accounting mismatch')
    for name, count in audience_selected.items():
        if omissions.pop('alert/' + name, 0) != count:
            raise ValueError('CAP selected audience accounting mismatch')
    report = dict(schema='cap-info-text/v1', source_sha256=base_report['source_sha256'],
                  original_file='source.xml', base_report=base_report,
                  base_csv_sha256=base_digest, base_observation_rows=len(base_rows),
                  text_info_rows=len(infos), text_output_rows=max(1, len(infos)),
                  selected_text_elements=sum(selected.values()), selected_text_counts=selected,
                  bundle_known_elements_not_carried=omissions,
                  bundle_unknown_elements_not_carried=base_report['unknown_elements_not_carried'],
                  omission_basis='BASE_REPORT_AND_OBSERVATIONS_REMAIN_UNCHANGED_BUNDLE_COUNTS_EXCLUDE_CARRIED_TEXT',
                  relation_basis='SAME_SOURCE_SHA256_INFO_INDEX_AND_GENERATED_BASE_CSV_DIGEST_ROW_RANGE_NOT_AUTHENTICATION',
                  text_basis=TEXT_BASIS, csv_output_bytes=written)
    if include_audience:
        report.update(selected_audience_elements=sum(audience_selected.values()),
                      selected_audience_counts=audience_selected,
                      audience_basis=audience['audience_basis'])
    return {'observations.csv': base, 'info-text.csv': output.getvalue()}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--include-audience', action='store_true',
                        help='retain declared audience strings; does not resolve recipients or grant permissions')
    args = parser.parse_args()
    try:
        if args.source.stat().st_size > cap.MAX_INPUT:
            raise ValueError('CAP input exceeds byte bound')
        with args.source.open('rb') as stream:
            data = stream.read(cap.MAX_INPUT + 1)
        outputs, report = convert(data, include_audience=args.include_audience)
        args.output.mkdir(parents=True, exist_ok=False)
        for name, value in outputs.items():
            (args.output / name).write_text(value, encoding='utf-8')
        (args.output / 'source.xml').write_bytes(data)
        (args.output / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    except (ValueError, OSError) as error:
        parser.exit(2, f'CAP text conversion failed: {error}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
