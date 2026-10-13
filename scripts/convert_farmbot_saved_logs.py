"""Passive saved FarmBot API Log events; no device access or inferred severity."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.parse import quote

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.convert_ardupilot_battery_csv import unique_object, json_time
from scripts.convert_vda_load_report import read_jsonl_window

PIN = '6db0ea3eb0059424573da8399d082b445624b24c'
TYPES = {'assertion', 'busy', 'debug', 'error', 'fun', 'info', 'success', 'warn'}
LIMIT = 16 * 1024 * 1024
RECORD_LIMIT = 32 * 1024
FIELDS = ['record_time_us', 'source_record_index', 'source_file_sha256',
          'source_record_hex', 'device_identity_declared', 'device_identity_basis',
          'log_id_reported', 'log_type_reported', 'log_type_disposition',
          'log_message', 'log_created_at_seconds', 'log_time_disposition',
          'log_verbosity_reported']


def integer(value, name):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError('invalid exact nonnegative integer: ' + name)
    return value


def string(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError('missing or invalid string: ' + name)
    return value


def convert(text):
    if not text or len(text.encode('utf-8')) > LIMIT:
        raise ValueError('empty or oversized saved logs')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n'); writer.writerow(FIELDS)
    digest = hashlib.sha256(text.encode()).hexdigest()
    report = dict(schema='farmbot-saved-logs/v1', schema_commit=PIN,
                  source_sha256=digest, source_records=0, output_rows=0,
                  unknown_type_records=0, missing_time_records=0,
                  meaning='REPORTED_EVENT_CATEGORY_NOT_PHYSICAL_SEVERITY',
                  time='API_CREATED_AT_UNIX_SECONDS_CAN_BE_CALLER_SUPPLIED_NOT_ACQUISITION',
                  identity='EXPLICIT_CALLER_DECLARATION_NOT_AUTHENTICATED',
                  unknown_fields='EXACT_SOURCE_LINE_RETAINED_NOT_INTERPRETED')
    previous = None
    try:
        for index, line in enumerate(text.splitlines()):
            if not line.strip() or len(line.encode()) > RECORD_LIMIT:
                raise ValueError('blank or oversized log record')
            record = json.loads(line, object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
            if record['schema_commit'] != PIN: raise ValueError('unqualified Log schema')
            capture = json_time(record, 'farmbot-state')
            if previous is not None and capture < previous: raise ValueError('capture time reversal')
            previous = capture
            device = string(record['device_identity'], 'device_identity')
            payload = record['payload']
            if not isinstance(payload, dict): raise ValueError('Log payload must be object')
            identity = integer(payload['id'], 'id')
            kind = string(payload['type'], 'type')
            message = string(payload['message'], 'message')
            created = payload.get('created_at')
            if created is not None: integer(created, 'created_at')
            verbosity = payload.get('verbosity')
            if verbosity is not None: integer(verbosity, 'verbosity')
            disposition = 'QUALIFIED_CATEGORY' if kind in TYPES else 'UNKNOWN_CATEGORY'
            time_disposition = ('MISSING' if 'created_at' not in payload else
                                'NULL' if created is None else 'REPORTED_API_TIME')
            writer.writerow([capture, index, 'sha256:'+digest, 'hex:'+line.encode().hex(),
                             'text:'+quote(device, safe=''), 'CALLER_DECLARED_NOT_API_DEVICE_ID', identity,
                             kind if kind in TYPES else 'unknown:'+quote(kind, safe=''),
                             disposition, 'text:'+quote(message, safe=''), '' if created is None else created,
                             time_disposition, '' if verbosity is None else verbosity])
            if output.tell() > LIMIT: raise ValueError('output exceeds bound')
            report['source_records'] += 1
            report['output_rows'] += 1
            report['unknown_type_records'] += kind not in TYPES
            report['missing_time_records'] += created is None
    except (KeyError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError('incompatible saved FarmBot Log') from exc
    result = output.getvalue()
    if len(result.encode()) > LIMIT: raise ValueError('output exceeds byte bound')
    report['output_sha256'] = hashlib.sha256(result.encode()).hexdigest()
    return result, report


def convert_path(path, *, start_record, stop_record):
    selected, offsets, count, selected_bytes, digest = read_jsonl_window(
        path, start_record=start_record, stop_record=stop_record)
    converted, semantics = convert(''.join(selected))
    output = io.StringIO(newline='')
    reader = csv.DictReader(io.StringIO(converted))
    writer = csv.DictWriter(output, fieldnames=FIELDS+['source_record_byte_offset',
                           'selected_normalized_sha256'], lineterminator='\n')
    writer.writeheader()
    for local, row in enumerate(reader):
        row.update(source_record_index=start_record+local, source_record_byte_offset=offsets[local],
                   source_file_sha256='sha256:'+digest,
                   selected_normalized_sha256='sha256:'+semantics['source_sha256'])
        writer.writerow(row)
    result = output.getvalue()
    if len(result.encode()) > LIMIT: raise ValueError('window output exceeds bound')
    return result, dict(source_sha256=digest, source_records=count,
        selected_records=len(selected), selected_bytes=selected_bytes,
        excluded_records_not_decoded=count-len(selected), selected_semantics=semantics,
        start_record=start_record, stop_record=stop_record,
        output_sha256=hashlib.sha256(result.encode()).hexdigest(),
        scope='SELECTED_RECORDS_ONLY_NOT_ALL_DEVICE_EVENTS',
        retention='Keep original source. Selected lines are LF-normalized; zero-based indices/byte offsets refer to original.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path); parser.add_argument('output', type=Path)
    parser.add_argument('--start-record', type=int, required=True)
    parser.add_argument('--stop-record', type=int, required=True)
    args = parser.parse_args()
    try:
        output, report = convert_path(args.input, start_record=args.start_record, stop_record=args.stop_record)
        args.output.mkdir()
        (args.output/'observations.csv').write_text(output, encoding='utf-8')
        (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    except (ValueError, OSError, UnicodeError, csv.Error):
        parser.exit(2, 'Saved FarmBot Log conversion failed; no successful conversion claim\n')


if __name__ == '__main__': main()
