#!/usr/bin/env python3
"""Offline reported DisplayPort address state; not a renderer or device observation."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__:
    from .convert_crsf_link import convert as decode, LIMIT
else:
    from convert_crsf_link import convert as decode, LIMIT

MAX_RECORDS = 100000
MAX_FRAMES = 4096
MAX_ROWS = 250000
MAX_OUTPUT = 128 * 1024 * 1024
MAX_REPORT = 32 * 1024 * 1024
FIELDS = ['record_time_us', 'row_kind', 'frame_index', 'draw_record_index',
          'source_sha256', 'canvas_rows', 'canvas_columns', 'known_glyph_count',
          'cleared_count', 'unknown_count', 'last_clear_record_index',
          'last_invalidation_record_index', 'cell_row', 'cell_column', 'glyph_byte',
          'font_bank', 'blink_reported', 'write_record_index', 'write_time_us']


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def convert(text, *, rows, columns):
    if any(type(n) is not int or not 1 <= n <= 256 for n in (rows, columns)) or rows * columns > 4096:
        raise ValueError('caller geometry requires integers 1..256 and at most 4096 cells')
    # This is the sole framing/operation validator; do not create a second wire decoder.
    operations, decoder = decode(text, 'msp-displayport-bf452')
    if decoder['source_records'] > MAX_RECORDS or len(operations.encode('utf-8')) > LIMIT:
        raise ValueError('source records or operations output exceed bound')
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    written = {}
    cleared = False
    last_clear = last_invalid = None
    frame_count = row_count = 0
    last_draw = None
    digest = 'sha256:' + sha(text)

    def emit(values):
        nonlocal row_count
        if row_count >= MAX_ROWS:
            raise ValueError('canvas row count exceeds bound')
        writer.writerow(values)
        row_count += 1
        # All emitted strings are ASCII and bounded, so character count is byte count.
        if output.tell() > MAX_OUTPUT:
            raise ValueError('canvas CSV exceeds bound')

    # Iterate original logical rows to retain indices even for unsupported operations.
    for index, record in enumerate(csv.DictReader(io.StringIO(text), strict=True)):
        frame = bytes.fromhex(record['frame_hex'])
        if frame[4] != 182:
            continue
        operation = frame[5]
        time = int(record['time_us'])
        if operation == 1 or operation not in (0, 1, 2, 3, 4):
            written.clear()
            cleared = False
            last_invalid = index
        elif operation == 2:
            written.clear()
            cleared = True
            last_clear = index
        elif operation == 3:
            row, column, attributes = frame[6:9]
            glyphs = frame[9:-1]
            if row >= rows or column >= columns or column + len(glyphs) > columns:
                raise ValueError('write exceeds caller geometry; no clipping or wrapping inferred')
            for offset, glyph in enumerate(glyphs):
                written[(row, column + offset)] = (glyph, attributes & 3,
                                                    bool(attributes & 0x40), index, time)
        elif operation == 4:
            if frame_count >= MAX_FRAMES:
                raise ValueError('DRAW count exceeds bound')
            count = len(written)
            common = dict(record_time_us=time, frame_index=frame_count,
                          draw_record_index=index, source_sha256=digest,
                          canvas_rows=rows, canvas_columns=columns,
                          last_clear_record_index=last_clear,
                          last_invalidation_record_index=last_invalid)
            emit(dict(common, row_kind='FRAME_SUMMARY', known_glyph_count=count,
                      cleared_count=rows * columns - count if cleared else 0,
                      unknown_count=0 if cleared else rows * columns - count))
            for (row, column), (glyph, bank, blink, write_index, write_time) in sorted(written.items()):
                emit(dict(common, row_kind='GLYPH_CELL', cell_row=row, cell_column=column,
                          glyph_byte=glyph, font_bank=bank, blink_reported=blink,
                          write_record_index=write_index, write_time_us=write_time))
            frame_count += 1
            last_draw = index
    if last_draw is None:
        raise ValueError('no explicit DRAW; no completed reported frame')
    canvas = output.getvalue()
    report = dict(schema='musubi.saved-displayport-canvas.v1', clock='Unknown',
                  basis='CALLER_GEOMETRY_REPORTED_ADDRESS_MODEL_NOT_PHYSICAL_DISPLAY',
                  source_sha256=sha(text), operations_sha256=sha(operations),
                  canvas_sha256=sha(canvas), decoder=decoder, frames=frame_count,
                  canvas_rows=row_count, glyph_rows=row_count - frame_count,
                  source_records=decoder['source_records'], operation_rows=decoder['decoded_records'],
                  trailing_source_records=decoder['source_records'] - last_draw - 1,
                  geometry=dict(rows=rows, columns=columns),
                  limits=dict(input_bytes=LIMIT, operations_bytes=LIMIT, records=MAX_RECORDS,
                              frames=MAX_FRAMES, canvas_rows=MAX_ROWS,
                              canvas_bytes=MAX_OUTPUT, report_bytes=MAX_REPORT))
    if len((json.dumps(report, indent=2, ensure_ascii=True) + '\n').encode()) > MAX_REPORT:
        raise ValueError('report exceeds bound')
    return operations, canvas, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--rows', type=int, required=True)
    parser.add_argument('--columns', type=int, required=True)
    args = parser.parse_args()
    try:
        with args.input.open('rb') as stream:
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('input exceeds bound')
        operations, canvas, report = convert(data.decode('utf-8'), rows=args.rows, columns=args.columns)
        args.output.mkdir(parents=False, exist_ok=False)
        (args.output / 'source.csv').write_bytes(data)
        (args.output / 'operations.csv').write_text(operations, encoding='utf-8')
        (args.output / 'canvas.csv').write_text(canvas, encoding='utf-8')
        (args.output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    except (ValueError, OSError, UnicodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
