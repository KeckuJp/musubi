#!/usr/bin/env python3
"""Explicit saved CSV row window, retaining original age declaration and byte binding."""
import argparse
import csv
import io
import json
from pathlib import Path
import sys

try:
    from . import check_saved_observation_age as age
    from .convert_recorded_angles_csv import convert_selected_csv_path
except ImportError:
    import check_saved_observation_age as age
    from convert_recorded_angles_csv import convert_selected_csv_path


def convert_path(path, specification, profile, *, start_record, stop_record):
    if (type(start_record) is not int or type(stop_record) is not int
            or not 0 < stop_record - start_record <= age.ROW_LIMIT):
        raise ValueError('invalid bounded age interval')
    if not isinstance(specification, bytes) or not 0 < len(specification) <= age.SPEC_LIMIT:
        raise ValueError('age declaration byte bound')
    spec = json.loads(specification, object_pairs_hook=age.unique)
    if not isinstance(spec, dict):
        raise ValueError('age declaration object required')
    selected = {}

    def assess(text):
        data = text.encode('utf-8')
        # Internal derived declaration binds the selected table, not the original.
        # Output provenance is restored only after whole-source SHA verification.
        derived = dict(spec, source_sha256=age.digest(data))
        output, report = age.convert(data, json.dumps(derived).encode(), profile)
        selected.update(data=data, report=report)
        return output

    output, window = convert_selected_csv_path(path, time_field=spec.get('time_column'),
        required_fields=[spec.get('clock_column'), *spec.get('subject_columns', [])],
        transform=assess, label='saved age', start_record=start_record,
        stop_record=stop_record, validate_time=False)
    original_sha = 'sha256:' + window['source_sha256']
    if spec.get('source_sha256') != original_sha:
        raise ValueError('original source digest mismatch')
    rows = csv.DictReader(io.StringIO(output))
    result = io.StringIO(newline='')
    writer = csv.DictWriter(result, rows.fieldnames, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        row.update(source_row_index=row['source_record_index'], source_sha256=original_sha,
                   spec_sha256=age.digest(specification))
        writer.writerow(row)
    rendered = result.getvalue()
    if len(rendered.encode()) > 16 * 1024**2:
        raise ValueError('selected assessment byte bound')
    report = dict(window, schema='musubi.saved-age-window.v1', clock_basis='Unknown',
        specification=spec, spec_sha256=age.digest(specification),
        profile_sha256=age.digest(profile), selected_source_sha256=age.digest(selected['data']),
        age_counts=selected['report']['counts'],
        selected_semantics='EXISTING_DECLARED_AGE_NOT_PHYSICAL_FRESHNESS',
        whole_source_validation='UTF8_SINGLE_LINE_CSV_FRAMING_WIDTH_AND_BYTE_SNAPSHOT_ONLY',
        excluded_values='NOT_SEMANTICALLY_EVALUATED_INCLUDING_TIME',
        output_sha256=age.digest(rendered.encode()), output_bytes=len(rendered.encode()))
    return rendered, report, selected['data']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'specification', 'profile', 'output'):
        parser.add_argument(name, type=Path)
    parser.add_argument('--start-record', type=int, required=True)
    parser.add_argument('--stop-record', type=int, required=True)
    args = parser.parse_args()
    try:
        with args.specification.open('rb') as handle:
            specification = handle.read(age.SPEC_LIMIT + 1)
        with args.profile.open('rb') as handle:
            profile = handle.read(age.PROFILE_LIMIT + 1)
        output, report, selected = convert_path(args.input, specification, profile,
            start_record=args.start_record, stop_record=args.stop_record)
        args.output.mkdir(exist_ok=False)
        for name, raw in [('selected-source.csv', selected), ('age-spec.json', specification),
                          ('source-profile.toml', profile), ('observations.csv', output.encode()),
                          ('report.json', (json.dumps(report, indent=2)+'\n').encode())]:
            (args.output/name).write_bytes(raw)
    except (ValueError, OSError, TypeError, KeyError, UnicodeError, csv.Error) as error:
        print('saved-age window refused: '+str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
