"""Explicit saved NMEA ASCII interval; existing meanings, no receiver access."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

try:
    from scripts.convert_nmea_wind import convert
    from scripts.convert_vda_load_report import read_jsonl_window
except ModuleNotFoundError:
    from convert_nmea_wind import convert
    from convert_vda_load_report import read_jsonl_window

KINDS = ('wind', 'water-dpt-mtw', 'ais-class-a', 'marine-mixed', 'ais-class-b18')
EXTRA = ('source_file_sha256', 'source_record_index', 'source_record_byte_offset')
OUTPUT_LIMIT = 128 * 1024**2


def report_text(report):
    text = json.dumps(report, ensure_ascii=True, indent=2) + '\n'
    if len(text.encode()) > OUTPUT_LIMIT:
        raise ValueError('NMEA window report exceeds bound')
    return text


def convert_path(path, *, start_record, stop_record, kind, expected_size=None,
                 expected_sha256=None, static_pairs=False, ais_rot=False, class_b_static=False):
    if kind not in KINDS:
        raise ValueError('explicit saved NMEA selector required')
    selected, offsets, count, selected_bytes, digest = read_jsonl_window(
        path, start_record=start_record, stop_record=stop_record,
        expected_size=expected_size, expected_sha256=expected_sha256)
    text = ''.join(selected)
    options = dict(static_pairs=static_pairs, ais_rot=ais_rot, class_b_static=class_b_static)
    converted, semantics = convert(text, kind, **options)
    result = io.StringIO(newline='')
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(max(previous, 2 * 512 * 1024 + 4))
        reader = csv.DictReader(io.StringIO(converted))
        writer = csv.DictWriter(result, [*reader.fieldnames, *EXTRA], lineterminator='\n')
        writer.writeheader()
        rows = 0
        for row in reader:
            local = int(row['source_line']) - 1
            if not 0 <= local < len(selected):
                raise ValueError('semantic source line outside selected window')
            row.update(source_file_sha256='sha256:'+digest,
                       source_record_index=start_record+local,
                       source_record_byte_offset=offsets[local])
            writer.writerow(row)
            rows += 1
            if result.tell() > OUTPUT_LIMIT:
                raise ValueError('NMEA window output exceeds bound')
    finally:
        csv.field_size_limit(previous)
    output = result.getvalue()
    if len(output.encode()) > OUTPUT_LIMIT:
        raise ValueError('NMEA window output byte bound exceeded')
    report = dict(kind=kind, source_sha256=digest, source_records=count,
        start_record=start_record, stop_record_exclusive=stop_record,
        selected_records=len(selected), excluded_records=count-len(selected),
        selected_bytes=selected_bytes, selected_source_hex='hex:'+text.encode('ascii').hex(),
        selected_sha256=hashlib.sha256(text.encode('ascii')).hexdigest(),
        source_byte_offsets=offsets, semantics=semantics, output_rows=rows,
        output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        outside_selection='FRAMING_ONLY_NOT_CHECKSUM_OR_MEANING_VALIDATED',
        line_reference='SEMANTIC_LINE_IS_ONE_BASED_SELECTED; ORIGINAL_INDEX_IS_ZERO_BASED',
        clock='UNLOCATED_ZERO_NOT_EVENT_TIME_NO_CROSS_SOURCE_OR_DOMAIN_INFERENCE',
        source_retention='Keep exact original externally; selected text normalizes CRLF/final LF',
        bounds=dict(input_bytes=2*1024**3, physical_records=5_000_000,
            physical_record_bytes=512*1024, selected_bytes=16*1024**2,
            csv_bytes=OUTPUT_LIMIT, report_bytes=OUTPUT_LIMIT))
    if any(options.values()):
        report['selector_options'] = options
    report_text(report)
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output_directory', type=Path)
    parser.add_argument('--start-record', type=int, required=True)
    parser.add_argument('--stop-record', type=int, required=True)
    parser.add_argument('--kind', choices=KINDS, required=True)
    parser.add_argument('--ais-static-pairs', action='store_true')
    parser.add_argument('--ais-rot', action='store_true')
    parser.add_argument('--ais-class-b-static', action='store_true')
    args = parser.parse_args()
    try:
        output, report = convert_path(args.input, start_record=args.start_record,
                                      stop_record=args.stop_record, kind=args.kind,
                                      static_pairs=args.ais_static_pairs, ais_rot=args.ais_rot,
                                      class_b_static=args.ais_class_b_static)
        encoded_report = report_text(report)
        args.output_directory.mkdir()
        (args.output_directory / 'converted.csv').write_text(output)
        (args.output_directory / 'report.json').write_text(encoded_report)
    except (OSError, ValueError, UnicodeError):
        parser.exit(2, 'Saved NMEA window conversion failed\n')


if __name__ == '__main__':
    main()
