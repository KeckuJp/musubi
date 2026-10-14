#!/usr/bin/env python3
"""Read an explicit interval of saved host JSONL reports, without host access."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

try:
    from scripts import convert_ardupilot_battery_csv as host
    from scripts.convert_vda_load_report import read_jsonl_window, MAX_RECORD_BYTES, expected_snapshot
except ModuleNotFoundError:
    import convert_ardupilot_battery_csv as host
    from convert_vda_load_report import read_jsonl_window, MAX_RECORD_BYTES, expected_snapshot

FORMATS = (host.DOCKER_FORMAT, host.DOCKER_STATE_FORMAT, host.TEGRA_FORMAT)
EXTRA = ('source_file_sha256', 'source_record_index', 'source_record_byte_offset')
CSV_LIMIT = 128 * 1024**2
REPORT_LIMIT = 40 * 1024**2


def report_text(report):
    text = json.dumps(report, ensure_ascii=True, indent=2)+'\n'
    if len(text.encode()) > REPORT_LIMIT:
        raise ValueError('host window report exceeds bound')
    return text


def convert_path(path, *, start_record, stop_record, source_format, tegrastats_nvdla=False,
                 expected_size=None, expected_sha256=None):
    if source_format not in FORMATS:
        raise ValueError('explicit supported saved host format required')
    if type(tegrastats_nvdla) is not bool or (tegrastats_nvdla and source_format != host.TEGRA_FORMAT):
        raise ValueError('NVDLA requires selected tegrastats format and boolean option')
    selected, offsets, count, selected_bytes, digest = read_jsonl_window(
        path, start_record=start_record, stop_record=stop_record,
        expected_size=expected_size, expected_sha256=expected_sha256)
    source_text = ''.join(selected)
    original = host.convert(source_text, source_format, tegrastats_nvdla=tegrastats_nvdla)
    output = io.StringIO(newline='')
    previous_limit = csv.field_size_limit(max(csv.field_size_limit(), 2*MAX_RECORD_BYTES+4))
    try:
        reader = csv.DictReader(io.StringIO(original))
        writer = csv.DictWriter(output, [*reader.fieldnames, *EXTRA], lineterminator='\n')
        writer.writeheader()
        local = -1
        rows = 0
        for row in reader:
            # Adopted host resolver retains source text once at the first row of
            # each record; tegrastats detail rows intentionally carry no copy.
            if row['source_record_hex']:
                local += 1
                if local >= len(selected) or row['source_record_hex'] != 'hex:'+selected[local][:-1].encode().hex():
                    raise ValueError('host resolver source-record accounting mismatch')
            if local < 0:
                raise ValueError('host detail before source record')
            row.update(source_file_sha256='sha256:'+digest,
                source_record_index=start_record+local, source_record_byte_offset=offsets[local])
            writer.writerow(row)
            rows += 1
            if output.tell() > CSV_LIMIT:
                raise ValueError('host window CSV exceeds bound')
        if local+1 != len(selected):
            raise ValueError('host resolver omitted a selected record')
    finally:
        csv.field_size_limit(previous_limit)
    text = output.getvalue()
    encoded = text.encode()
    if len(encoded) > CSV_LIMIT:
        raise ValueError('host window CSV byte bound exceeded')
    report = dict(source_format=source_format, source_sha256=digest,
        source_records=count, start_record=start_record, stop_record_exclusive=stop_record,
        selected_records=len(selected), excluded_records=count-len(selected),
        selected_bytes=selected_bytes, selected_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
        selected_jsonl_hex='hex:'+source_text.encode().hex(), source_byte_offsets=offsets,
        output_rows=rows, output_bytes=len(encoded), output_sha256=hashlib.sha256(encoded).hexdigest(),
        outside_selection='NOT_DECODED_OR_VALIDATED',
        framing_scope='WHOLE_SOURCE_UTF8_PHYSICAL_LINES_ONLY',
        clock_scope='SELECTED_NONDECREASING_CALLER_CAPTURE_UNKNOWN',
        source_retention='Keep original file externally; selected normalized LF text is retained here',
        bounds=dict(input_bytes=2*1024**3,physical_records=5_000_000,physical_record_bytes=512*1024,
            selected_bytes=16*1024**2,csv_bytes=CSV_LIMIT,report_bytes=REPORT_LIMIT))
    if tegrastats_nvdla:
        report['tegrastats_nvdla'] = True
    expectation = expected_snapshot(expected_size, expected_sha256)
    if expectation:
        report['input_expectation'] = expectation
    report_text(report)
    return text, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('output_dir',type=Path)
    parser.add_argument('--source-format',choices=FORMATS,required=True)
    parser.add_argument('--start-record',type=int,required=True)
    parser.add_argument('--stop-record',type=int,required=True)
    parser.add_argument('--tegrastats-nvdla',action='store_true')
    parser.add_argument('--expected-size',type=int)
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    try:
        if args.output_dir.exists():
            raise ValueError('output directory exists; no overwrite')
        text, report = convert_path(args.input,start_record=args.start_record,
            stop_record=args.stop_record,source_format=args.source_format,
            tegrastats_nvdla=args.tegrastats_nvdla,
            expected_size=args.expected_size,expected_sha256=args.expected_sha256)
        serialized = report_text(report)
        args.output_dir.mkdir()
        (args.output_dir/'observations.csv').write_text(text,encoding='utf-8')
        (args.output_dir/'report.json').write_text(serialized,encoding='utf-8')
    except (ValueError,OSError) as exc:
        parser.exit(2,f'Host window conversion failed: {exc}\n')


if __name__=='__main__': main()
