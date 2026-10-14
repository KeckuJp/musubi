#!/usr/bin/env python3
"""Assess saved observation age against an explicit same-clock declaration; never wall time."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tomllib

try:
    from .convert_declared_expectation_check import guarded, declared_name, text, identity
except ImportError:
    from convert_declared_expectation_check import guarded, declared_name, text, identity

INPUT_LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 64 * 1024 * 1024
ROW_LIMIT = 200_000
SPEC_LIMIT = 8192
PROFILE_LIMIT = 65536
FIELDS = ('evaluation_time_us', 'evaluation_time_us_exact', 'source_time_us_exact',
          'maximum_age_us_exact', 'age_us_exact', 'age_disposition', 'subject_disposition',
          'clock_disposition', 'source_row_index', 'subject_hex', 'clock_domain_hex',
          'source_sha256', 'profile_sha256', 'spec_sha256')


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def integer(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,19}', value):
        raise ValueError('time must be an explicit nonnegative integer microsecond string')
    result = int(value)
    if result > 0x7fffffffffffffff:
        raise ValueError('time exceeds the common signed microsecond range')
    return result


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate declaration key')
        result[key] = value
    return result


def convert(data, specification, profile_bytes):
    for raw, limit in ((data, INPUT_LIMIT), (specification, SPEC_LIMIT), (profile_bytes, PROFILE_LIMIT)):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
            raise ValueError('empty or oversized input')
    spec = json.loads(specification, object_pairs_hook=unique)
    keys = {'schema', 'source_sha256', 'profile_sha256', 'time_column', 'time_unit', 'clock_basis',
            'subject_columns', 'subject_values', 'clock_column', 'clock_domain',
            'evaluation_time_us', 'maximum_age_us'}
    if not isinstance(spec, dict) or set(spec) != keys or spec['schema'] != 'musubi.saved-age.v1':
        raise ValueError('unsupported age declaration shape')
    if spec['source_sha256'] != digest(data) or spec['profile_sha256'] != digest(profile_bytes):
        raise ValueError('source/profile digest mismatch')
    evaluation, maximum = integer(spec['evaluation_time_us']), integer(spec['maximum_age_us'])
    time_column, clock_column = declared_name(spec['time_column']), declared_name(spec['clock_column'])
    columns, wanted = spec['subject_columns'], spec['subject_values']
    if (not isinstance(columns, list) or not 1 <= len(columns) <= 8 or
            any(not isinstance(c, str) for c in columns) or len(set(columns)) != len(columns) or
            not isinstance(wanted, list) or len(wanted) != len(columns) or
            any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in wanted)):
        raise ValueError('explicit subject tuple required')
    for column in columns:
        declared_name(column)
    domain = spec['clock_domain']
    if (not isinstance(domain, str) or not 1 <= len(domain.strip()) <= 256 or
            domain.strip().lower() in ('unknown', 'unspecified', 'none')):
        raise ValueError('explicit nonunknown evaluation clock domain required')
    profile = tomllib.loads(profile_bytes.decode('utf-8'))
    if (profile.get('format') != 'telemetry_csv_us' or
            not isinstance(profile.get('fields'), dict) or not isinstance(profile.get('units'), dict) or
            profile.get('fields', {}).get('time') != time_column or
            spec['time_unit'] not in ('us', 'us_since_boot') or
            profile.get('units', {}).get(time_column) != spec['time_unit'] or
            spec['clock_basis'] not in ('unknown', 'boot_relative') or
            profile.get('default_clock_basis') != spec['clock_basis'] or
            any(not isinstance(profile.get(k), str) or not profile[k] for k in ('profile_id', 'version'))):
        raise ValueError('profile time field, integer microsecond unit or clock declaration mismatch')
    # Unlike read_rows, an absent source time remains an explicit unassessed row.
    reader = guarded(csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True))
    header = next(reader, [])
    if not 1 <= len(header) <= 256 or len(set(header)) != len(header):
        raise ValueError('missing/duplicate/excess source columns')
    if any(c not in header for c in [time_column, clock_column, *columns]):
        raise ValueError('declared source column missing')
    indices = {name: i for i, name in enumerate(header)}
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    written = len(output.getvalue().encode())
    counts, row_count = {}, 0
    for ordinal, cells in enumerate(reader):
        if ordinal >= ROW_LIMIT or len(cells) != len(header):
            raise ValueError('source row bound or width violation')
        raw_time = cells[indices[time_column]]
        moment = None if raw_time == '' else integer(raw_time)
        subject = [cells[indices[c]] for c in columns]
        saved_clock = cells[indices[clock_column]]
        subject_state = ('SUBJECT_NOT_REPORTED' if '' in subject else
                         'QUALIFIED_SUBJECT' if subject == wanted else 'UNQUALIFIED_SUBJECT')
        clock_state = ('CLOCK_NOT_REPORTED' if not saved_clock else
                       'QUALIFIED_CLOCK' if saved_clock == domain else 'UNQUALIFIED_CLOCK')
        difference = None
        if subject_state != 'QUALIFIED_SUBJECT':
            disposition = subject_state
        elif clock_state != 'QUALIFIED_CLOCK':
            disposition = clock_state
        elif moment is None:
            disposition = 'SOURCE_TIME_NOT_REPORTED'
        else:
            difference = evaluation - moment
            disposition = ('SOURCE_TIME_AFTER_EVALUATION' if difference < 0 else
                           'STALE_BY_DECLARATION' if difference > maximum else 'WITHIN_DECLARED_AGE')
        row = dict(evaluation_time_us=evaluation, evaluation_time_us_exact='integer:' + str(evaluation),
                   source_time_us_exact='' if moment is None else 'integer:' + raw_time,
                   maximum_age_us_exact='integer:' + str(maximum),
                   age_us_exact='' if difference is None else 'integer:' + str(difference),
                   age_disposition=disposition, subject_disposition=subject_state,
                   clock_disposition=clock_state, source_row_index=ordinal,
                   subject_hex=identity(subject), clock_domain_hex=text(saved_clock),
                   source_sha256=spec['source_sha256'], profile_sha256=spec['profile_sha256'],
                   spec_sha256=digest(specification))
        temporary = io.StringIO(newline='')
        csv.DictWriter(temporary, fieldnames=FIELDS, lineterminator='\n').writerow(row)
        line = temporary.getvalue()
        written += len(line.encode())
        if written > OUTPUT_LIMIT:
            raise ValueError('age output byte bound')
        output.write(line)
        counts[disposition] = counts.get(disposition, 0) + 1
        row_count += 1
    if not row_count:
        raise ValueError('no source records')
    return output.getvalue(), dict(schema='musubi.saved-age-result.v1', source_records=row_count,
        counts=counts, specification=spec, spec_sha256=digest(specification), output_bytes=written,
        clock_basis='Unknown', output_time='EXPLICIT_EVALUATION_TIME_NOT_SOURCE_TIMESTAMP',
        clock_comparison='CALLER_DECLARED_NOT_PHYSICAL_SYNCHRONIZATION',
        claim='DECLARED_TIMESTAMP_AGE_NOT_FROZEN_VALUE_PRODUCER_FRESHNESS_OR_PHYSICAL_HEALTH',
        required_original_artifacts=['source.csv', 'age-spec.json', 'source-profile.toml'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'specification', 'profile', 'output'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    try:
        contents = []
        for path, limit in ((args.input, INPUT_LIMIT), (args.specification, SPEC_LIMIT), (args.profile, PROFILE_LIMIT)):
            with path.open('rb') as handle:
                contents.append(handle.read(limit + 1))
        output, report = convert(*contents)
        args.output.mkdir(exist_ok=False)
        for name, raw in zip(('source.csv', 'age-spec.json', 'source-profile.toml'), contents):
            (args.output/name).write_bytes(raw)
        (args.output/'observations.csv').write_text(output, encoding='utf-8')
        (args.output/'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    except (ValueError, OSError) as error:
        print('saved-age refused: ' + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
