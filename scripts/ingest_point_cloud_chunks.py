#!/usr/bin/env python3
"""Verify and ingest every exported saved-cloud segment, one report at a time.

A completion manifest is written only after all segments pass the existing report viewer check.
Failed runs retain partial reports for diagnosis, without a completion manifest. Use a new output
directory on retry. This is file processing, never a survey-accuracy or physical-state assertion.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time

MAX_MANIFEST = 4 * 1024 * 1024
MAX_CSV = 16 * 1024 * 1024
MAX_REPORT = 128 * 1024 * 1024
MAX_CHUNKS = 4096


def bounded(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError('expected a regular file, without a symbolic link')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('file exceeds the input bound; export smaller segments')
    return raw


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('expected a nonnegative integer')
    return value


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def validate_manifest(manifest):
    chunks = manifest['chunks']
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_CHUNKS:
        raise ValueError('invalid chunk count')
    if integer(manifest['chunk_count']) != len(chunks):
        raise ValueError('chunk count does not reconcile')
    total = integer(manifest['points_to_carry'])
    if integer(manifest['points_carried']) != total:
        raise ValueError('point totals do not reconcile')
    first = size = 0
    names = set()
    for entry in chunks:
        name = entry['chunk']
        if not isinstance(name, str) or not re.fullmatch(r'chunk-[0-9]{6}', name) or name in names:
            raise ValueError('invalid or repeated chunk name')
        names.add(name)
        count = integer(entry['point_count'])
        if integer(entry['first_point_index']) != first:
            raise ValueError('chunk ranges are not consecutive')
        if entry['last_point_index'] != (first + count - 1 if count else None):
            raise ValueError('chunk end does not reconcile')
        if count == 0 and (total != 0 or len(chunks) != 1):
            raise ValueError('unexpected empty chunk')
        size += integer(entry['csv_bytes'])
        if entry['csv_bytes'] > MAX_CSV:
            raise ValueError('segment exceeds report input bound; export smaller segments')
        if not isinstance(entry['csv_sha256'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['csv_sha256']):
            raise ValueError('invalid CSV digest')
        first += count
    if first != total or size != integer(manifest['csv_bytes']):
        raise ValueError('manifest totals do not reconcile')
    return chunks


def validate_rows(raw, entry):
    rows = csv.DictReader(io.StringIO(raw.decode('utf-8')))
    first = entry['first_point_index']
    points = declarations = 0
    for row in rows:
        kind = row.get('las_record_kind')
        if kind == 'FILE_DECLARATION':
            declarations += 1
        elif kind == 'POINT_RECORD':
            if row.get('point_index') != str(first + points):
                raise ValueError('CSV point order does not reconcile')
            points += 1
        else:
            raise ValueError('unexpected cloud row kind')
    if points != entry['point_count'] or declarations != 1:
        raise ValueError('CSV points or declaration do not reconcile')
    return points + declarations


def ingest(directory, reader, meanings, output, ingest_bin, viewer_bin):
    directory, output = Path(directory), Path(output)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('expected a chunk directory without a symbolic link')
    manifest_raw = bounded(directory / 'manifest.json', MAX_MANIFEST)
    manifest = json.loads(manifest_raw)
    chunks = validate_manifest(manifest)
    # Snapshot declarations too; a caller cannot change them between segments of this run.
    reader_raw, meanings_raw = bounded(Path(reader), 256 * 1024), bounded(Path(meanings), 256 * 1024)
    output.mkdir()
    reader_copy, meanings_copy, csv_copy = (output / name for name in ('.reader.json', '.meanings.json', '.input.csv'))
    reader_copy.write_bytes(reader_raw)
    meanings_copy.write_bytes(meanings_raw)
    received_at = str(time.time_ns() // 1000000)
    complete = {'schema': 'point-cloud-observation-chunks/v1', 'points_carried': 0,
                'chunk_count': len(chunks), 'source_manifest_sha256': digest(manifest_raw), 'chunks': []}
    try:
        for index, entry in enumerate(chunks):
            place = directory / entry['chunk']
            if place.is_symlink() or not place.is_dir():
                raise ValueError('invalid chunk directory')
            raw = bounded(place / 'observations.csv', MAX_CSV)
            if len(raw) != entry['csv_bytes'] or digest(raw) != entry['csv_sha256']:
                raise ValueError('CSV content does not match the manifest')
            expected_rows = validate_rows(raw, entry)
            csv_copy.write_bytes(raw)
            del raw
            name = 'report-%06d.json' % index
            report_path = output / name
            # The selected executable is an explicit trusted local tool. Its own report bound
            # applies while writing; no child diagnostics or saved source text are echoed here.
            with report_path.open('xb') as stream:
                subprocess.run([str(ingest_bin), str(csv_copy), str(reader_copy), str(meanings_copy),
                                'saved-point-cloud', received_at, '--allow-equal-time'], stdout=stream,
                               stderr=subprocess.DEVNULL, check=True)
            report_raw = bounded(report_path, MAX_REPORT)
            report = json.loads(report_raw)
            if report.get('schema') != 'recorded-observation-report/v1' or len(report.get('observations', [])) != expected_rows:
                raise ValueError('common report count does not reconcile')
            subprocess.run([str(viewer_bin), '--report', '--in', str(report_path), '--check'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            complete['chunks'].append({'report': name, 'sha256': digest(report_raw),
                                       'point_count': entry['point_count']})
            complete['points_carried'] += entry['point_count']
            del report, report_raw
        if complete['points_carried'] != manifest['points_to_carry']:
            raise ValueError('completed report totals do not reconcile')
        temporary = output / '.manifest.json'
        temporary.write_text(json.dumps(complete, indent=2) + '\n', encoding='utf-8')
        temporary.rename(output / 'manifest.json')
        return complete
    finally:
        for path in (reader_copy, meanings_copy, csv_copy):
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('reader', type=Path)
    parser.add_argument('meanings', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--ingest-bin', type=Path, required=True)
    parser.add_argument('--viewer-bin', type=Path, required=True)
    args = parser.parse_args()
    try:
        complete = ingest(args.directory, args.reader, args.meanings, args.output,
                          args.ingest_bin, args.viewer_bin)
    except (OSError, ValueError, KeyError, TypeError, csv.Error, subprocess.SubprocessError):
        print('saved-cloud ingestion failed; no completion manifest was produced', file=sys.stderr)
        return 1
    print(json.dumps({'status': 'complete', 'points_carried': complete['points_carried'],
                      'chunk_count': complete['chunk_count']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
