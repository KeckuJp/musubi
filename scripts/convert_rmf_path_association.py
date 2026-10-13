#!/usr/bin/env python3
"""Associate supplied saved RMF reports; no inferred identity, time or frame join."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
try:
    from scripts import convert_rmf_issue_reports as issues
except ModuleNotFoundError:
    import convert_rmf_issue_reports as issues

existing = issues.existing
LIMIT = 16 * 1024**2
ADDED = ('association_id_hex', 'association_basis', 'association_declaration_sha256',
         'association_input_sha256', 'association_input_record_index', 'association_role')

def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()

def parse(raw, limit):
    if not isinstance(raw, bytes) or not raw or len(raw) > limit:
        raise ValueError('bounded nonempty bytes required')
    return raw.decode('utf-8')

def convert(web_raw, ros_raw, declaration_raw):
    web_text, ros_text = parse(web_raw, LIMIT), parse(ros_raw, LIMIT)
    declaration = json.loads(parse(declaration_raw, 65536), object_pairs_hook=existing.unique_object)
    required = {'schema', 'web_sha256', 'ros_sha256', 'web_record_index', 'ros_record_index',
                'web_robot_key', 'ros_robot_name', 'association_id', 'citation'}
    if not isinstance(declaration, dict) or set(declaration) != required or declaration['schema'] != 'rmf-path-association/v1':
        raise ValueError('exact association schema required')
    for key in ('web_robot_key', 'ros_robot_name', 'association_id', 'citation'):
        if not isinstance(declaration[key], str) or not declaration[key].strip() or len(declaration[key].encode()) > 4096:
            raise ValueError('bounded explicit identity/citation required')
    for role, raw in (('web', web_raw), ('ros', ros_raw)):
        if declaration[role + '_sha256'] != digest(raw):
            raise ValueError('source digest mismatch')
        if type(declaration[role + '_record_index']) is not int or declaration[role + '_record_index'] < 0:
            raise ValueError('explicit nonnegative record index required')
    # Reuse full source/schema/quantity validation, never qualify arbitrary columns by manifest.
    state, issue_csv, participation, web_report = issues.convert_participation(web_text, existing.RMF_WEB_FORMAT)
    ros_csv = existing.convert_json(ros_text, existing.RMF_FORMAT)
    selected = {}
    for role, text in (('web', web_text), ('ros', ros_text)):
        lines = text.splitlines(); index = declaration[role + '_record_index']
        if index >= len(lines):
            raise ValueError('selected source record absent')
        selected[role] = json.loads(lines[index], object_pairs_hook=existing.unique_object)
        if selected[role].get('location_unit') != 'm' or selected[role].get('yaw_unit') != 'rad':
            raise ValueError('both location units must be explicitly qualified')
    robot = selected['web']['state'].get('robots', {}).get(declaration['web_robot_key'])
    ros_state = selected['ros']['state']
    if not isinstance(robot, dict) or not isinstance(robot.get('location'), dict):
        raise ValueError('selected web robot location absent')
    if ros_state['name'] != declaration['ros_robot_name'] or not isinstance(ros_state.get('location'), dict) or not isinstance(ros_state.get('path'), list):
        raise ValueError('selected ROS robot/location/path absent or mismatched')
    outputs = {}; counts = {}
    originals = {'web-state': state, 'web-issues': issue_csv, 'web-participation': participation, 'ros-state-path': ros_csv}
    old_limit = csv.field_size_limit(2 * 1024**2)
    try:
        for name, original in originals.items():
            source_role = 'ros' if name == 'ros-state-path' else 'web'
            reader = csv.DictReader(io.StringIO(original))
            if set(ADDED) & set(reader.fieldnames):
                raise ValueError('reserved association field')
            output = io.StringIO(newline=''); writer = csv.DictWriter(output, fieldnames=list(reader.fieldnames) + list(ADDED), lineterminator='\n'); writer.writeheader()
            bound = unbound = 0; record_index = -1
            for row in reader:
                if name in ('web-state', 'ros-state-path'):
                    if row['source_record_hex']:
                        record_index += 1
                else:
                    record_index = int(row['rmf_source_record_index'])
                key = row['rmf_name_hex'] if source_role == 'ros' else row['rmf_robot_key_hex']
                expected = declaration['ros_robot_name'] if source_role == 'ros' else declaration['web_robot_key']
                matches = record_index == declaration[source_role + '_record_index'] and key == 'hex:' + expected.encode().hex()
                row.update(association_id_hex='hex:' + declaration['association_id'].encode().hex() if matches else '',
                    association_basis='CALLER_DECLARED_ASSOCIATION_NOT_AUTHENTICATION_TIME_OR_FRAME_ALIGNMENT' if matches else 'UNBOUND_SOURCE_ROW',
                    association_declaration_sha256=digest(declaration_raw), association_input_sha256=declaration[source_role + '_sha256'],
                    association_input_record_index=record_index, association_role=name)
                writer.writerow(row); bound += matches; unbound += not matches
                if output.tell() > 128 * 1024**2:
                    raise ValueError('annotated output exceeds bound')
            if not bound:
                raise ValueError('required role produced no associated rows')
            outputs[name] = output.getvalue(); counts[name] = dict(bound_rows=bound, unbound_rows=unbound)
    finally:
        csv.field_size_limit(old_limit)
    return outputs, originals, dict(declaration_sha256=digest(declaration_raw), counts=counts,
        web_source_records=web_report['snapshots'], ros_source_records=len(ros_text.splitlines()),
        clocks='Separate original capture and waypoint clocks retained; no synchronization',
        frames='Original map/level names retained; association asserts no shared frame',
        truth='Caller association/citation unverified; not observed execution or physical identity')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('web', type=Path); parser.add_argument('ros', type=Path)
    parser.add_argument('declaration', type=Path); parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        raw = []
        for path, limit in ((args.web, LIMIT), (args.ros, LIMIT), (args.declaration, 65536)):
            with path.open('rb') as stream:
                raw.append(stream.read(limit + 1))
        outputs, originals, report = convert(*raw)
        args.output.mkdir()
        for name, data in zip(('web.jsonl', 'ros.jsonl', 'declaration.json'), raw):
            (args.output / name).write_bytes(data)
        for name, text in outputs.items():
            (args.output / (name + '.csv')).write_text(text, encoding='utf-8')
            (args.output / (name + '.original.csv')).write_text(originals[name], encoding='utf-8')
        (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    except (ValueError, TypeError, KeyError, OSError, RecursionError, OverflowError):
        parser.exit(2, 'Saved RMF association failed; no completed association claim\n')

if __name__ == '__main__':
    main()
