#!/usr/bin/env python3
"""Declared saved RMF issues; no fleet connection, task operation or diagnosis."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
try:
    from scripts import convert_ardupilot_battery_csv as existing
except ModuleNotFoundError:
    import convert_ardupilot_battery_csv as existing

FIELDS = ('record_time_us', 'rmf_source_sha256', 'rmf_source_record_index',
          'rmf_robot_key_hex', 'rmf_issue_collection', 'rmf_issue_count',
          'rmf_issue_index', 'rmf_issue_category_disposition', 'rmf_issue_category_hex',
          'rmf_issue_detail_disposition', 'rmf_issue_detail_canonical_hex', 'rmf_issue_basis')

def optional(obj, key):
    return 'MISSING' if key not in obj else 'NULL' if obj[key] is None else 'PRESENT'

def convert(text, source_format):
    if source_format not in (existing.RMF_WEB_FORMAT, existing.RMF_SOCKET_FORMAT):
        raise ValueError('explicit RMF saved schema required')
    base = existing.convert_json(text, source_format)
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    count = snapshots = issue_count = 0
    for index, line in enumerate(text.splitlines()):
        record = json.loads(line, object_pairs_hook=existing.unique_object)
        if source_format == existing.RMF_SOCKET_FORMAT:
            record = existing.rmf_socket_state(record)
        digest = 'sha256:' + hashlib.sha256(line.encode()).hexdigest()
        robots = record['state'].get('robots') or {}
        snapshots += 1
        for key, robot in (robots.items() if robots else [(None, {})]):
            collection = optional(robot, 'issues') if key is not None else 'INAPPLICABLE_NO_ROBOTS'
            issues = robot.get('issues')
            if issues is not None and (not isinstance(issues, list) or len(issues) > 4096):
                raise ValueError('invalid or oversized issue list')
            if isinstance(issues, list):
                collection = 'PRESENT' if issues else 'EMPTY'
            for ordinal, issue in (enumerate(issues) if issues else [(None, {})]):
                if not isinstance(issue, dict):
                    raise ValueError('invalid issue object')
                category, detail = issue.get('category'), issue.get('detail')
                if category is not None and (not isinstance(category, str) or len(category.encode()) > 4096):
                    raise ValueError('invalid issue category')
                if detail is not None and not isinstance(detail, (dict, list, str)):
                    raise ValueError('invalid issue detail')
                canonical = '' if detail is None else json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if len(canonical.encode()) > 65536:
                    raise ValueError('issue detail exceeds bound')
                writer.writerow(dict(record_time_us=record['capture_time_us'],
                    rmf_source_sha256=digest, rmf_source_record_index=index,
                    rmf_robot_key_hex='' if key is None else 'hex:'+key.encode().hex(),
                    rmf_issue_collection=collection,
                    rmf_issue_count='' if issues is None else len(issues),
                    rmf_issue_index='' if ordinal is None else ordinal,
                    rmf_issue_category_disposition=optional(issue, 'category') if ordinal is not None else 'INAPPLICABLE',
                    rmf_issue_category_hex='' if category is None else 'hex:'+category.encode().hex(),
                    rmf_issue_detail_disposition=optional(issue, 'detail') if ordinal is not None else 'INAPPLICABLE',
                    rmf_issue_detail_canonical_hex='' if detail is None else 'hex:'+canonical.encode().hex(),
                    rmf_issue_basis='DECLARED_ISSUE_NOT_PHYSICAL_CAUSE_DETAIL_NOT_INTERPRETED'))
                count += 1
                issue_count += ordinal is not None
                if output.tell() > 64*1024**2:
                    raise ValueError('issue output exceeds bound')
    bindings = []
    old_limit = csv.field_size_limit(2 * 1024**2)
    try:
        for row_index, row in enumerate(csv.DictReader(io.StringIO(base))):
            if row['source_record_hex']:
                bindings.append(dict(source_record_index=len(bindings), base_row_start=row_index, base_row_stop=row_index))
            if not bindings:
                raise ValueError('missing original snapshot binding')
            bindings[-1]['base_row_stop'] = row_index + 1
        if len(bindings) != snapshots:
            raise ValueError('snapshot binding accounting mismatch')
    finally:
        csv.field_size_limit(old_limit)
    return base, output.getvalue(), dict(snapshots=snapshots, reported_issues=issue_count,
        output_rows=count, source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        base_csv_sha256=hashlib.sha256(base.encode()).hexdigest(), source_record_bindings=bindings,
        source_retention='Keep original supplied text and unchanged state CSV; detail is canonical JSON, not original bytes')

def convert_participation(text, source_format):
    """Reuse state/issues and add reported commission/mutex, never authorization."""
    base, issues, report = convert(text, source_format)
    fields = ('record_time_us', 'rmf_source_sha256', 'rmf_source_record_index', 'rmf_robot_key_hex',
              'participation_kind', 'participation_collection', 'participation_field',
              'participation_disposition', 'participation_flag_reported',
              'mutex_count', 'mutex_index', 'mutex_name_hex', 'participation_basis')
    output = io.StringIO(newline=''); writer = csv.DictWriter(output, fieldnames=fields, lineterminator='\n')
    writer.writeheader(); count = 0
    for index, line in enumerate(text.splitlines()):
        record = json.loads(line, object_pairs_hook=existing.unique_object)
        if source_format == existing.RMF_SOCKET_FORMAT:
            record = existing.rmf_socket_state(record)
        robots = record['state'].get('robots') or {}
        for key, robot in (robots.items() if robots else [(None, {})]):
            common = dict(record_time_us=record['capture_time_us'],
                rmf_source_sha256='sha256:' + hashlib.sha256(line.encode()).hexdigest(),
                rmf_source_record_index=index, rmf_robot_key_hex='' if key is None else 'hex:' + key.encode().hex(),
                participation_basis='SAVED_PRODUCER_REPORT_NOT_PERMISSION_OR_ACTUAL_TASK_ACCEPTANCE')
            for group, names in (('commission', ('dispatch_tasks', 'direct_tasks', 'idle_behavior')),
                                 ('mutex_groups', ('locked', 'requesting'))):
                value = robot.get(group)
                if value is not None and not isinstance(value, dict):
                    raise ValueError('invalid participation object')
                collection = 'INAPPLICABLE_NO_ROBOTS' if key is None else optional(robot, group)
                for name in names:
                    item = (value or {}).get(name)
                    disposition = optional(value, name) if isinstance(value, dict) else 'INAPPLICABLE'
                    row = dict(common, participation_kind=group, participation_collection=collection,
                               participation_field=name, participation_disposition=disposition)
                    if group == 'commission':
                        if item is not None and type(item) is not bool:
                            raise ValueError('reported commission flag must be boolean')
                        writer.writerow(dict(row, participation_flag_reported='' if item is None else int(item)))
                        count += 1
                    else:
                        if item is not None and (not isinstance(item, list) or len(item) > 4096 or
                            any(not isinstance(v, str) or len(v.encode()) > 4096 for v in item)):
                            raise ValueError('invalid mutex names')
                        if item == []:
                            row['participation_disposition'] = 'EMPTY'
                        for ordinal, name_value in (enumerate(item) if item else [(None, None)]):
                            writer.writerow(dict(row, mutex_count='' if item is None else len(item),
                                mutex_index='' if ordinal is None else ordinal,
                                mutex_name_hex='' if name_value is None else 'hex:' + name_value.encode().hex()))
                            count += 1
                            if output.tell() > 64 * 1024**2:
                                raise ValueError('participation output exceeds bound')
                    if output.tell() > 64 * 1024**2:
                        raise ValueError('participation output exceeds bound')
    report['participation_output_rows'] = count
    report['participation_scope'] = 'fixed saved reported commission and mutex; original unknown fields retained in state.csv'
    return base, issues, output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output_directory', type=Path)
    parser.add_argument('--source-format', required=True, choices=(existing.RMF_WEB_FORMAT, existing.RMF_SOCKET_FORMAT))
    parser.add_argument('--include-participation', action='store_true')
    args = parser.parse_args()
    try:
        with args.input.open('rb') as stream:
            data = stream.read(16*1024**2+1)
        if len(data) > 16*1024**2:
            raise ValueError('input exceeds bound')
        if args.include_participation:
            base, sidecar, participation, report = convert_participation(data.decode('utf-8'), args.source_format)
        else:
            base, sidecar, report = convert(data.decode('utf-8'), args.source_format)
        args.output_directory.mkdir()
        (args.output_directory/'state.csv').write_text(base, encoding='utf-8')
        (args.output_directory/'issues.csv').write_text(sidecar, encoding='utf-8')
        if args.include_participation:
            (args.output_directory/'participation.csv').write_text(participation, encoding='utf-8')
        (args.output_directory/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    except (ValueError, OSError, TypeError, KeyError, RecursionError, OverflowError):
        parser.exit(2, 'Saved RMF issue conversion failed; no successful conversion claim\n')

if __name__ == '__main__':
    main()
