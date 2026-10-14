"""Finite epoch windows of saved RTKLIB LLH solutions; no solving or acquisition."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys

try:
    from scripts import convert_rtklib_solution as existing
    from scripts.convert_vda_load_report import expected_snapshot
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import convert_rtklib_solution as existing
    from scripts.convert_vda_load_report import expected_snapshot

MAX_INPUT_BYTES = 2 * 1024**3
MAX_EPOCHS = 5_000_000
MAX_PHYSICAL_LINES = 5_001_000
MAX_LINE_BYTES = 512 * 1024


def convert_path(path, *, start_record, stop_record, expected_size=None, expected_sha256=None,
                 height_reference=None, horizontal_datum=None, time_system=None, reported_velocity=False):
    if (type(start_record) is not int or type(stop_record) is not int
            or not 0 <= start_record < stop_record <= MAX_EPOCHS
            or stop_record-start_record > existing.MAX_ROWS):
        raise ValueError('finite epoch interval required')
    expectation = expected_snapshot(expected_size, expected_sha256)
    path = Path(path).resolve()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.parts[1:2] in (('sys',),('proc',),('dev',)) or info.st_size > MAX_INPUT_BYTES:
        raise ValueError('bounded saved regular solution file required')
    signature = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    digest = hashlib.sha256()
    chunks, positions = [], []
    total = epochs = selected_size = header_comments = physical_lines = blank_lines = 0
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if signature(before) != signature(info): raise ValueError('source changed before read')
        while True:
            raw = stream.readline(MAX_LINE_BYTES+1)
            if not raw: break
            if len(raw)>MAX_LINE_BYTES: raise ValueError('solution line bound exceeded')
            offset=total; total+=len(raw); physical_lines+=1
            if total>MAX_INPUT_BYTES or physical_lines>MAX_PHYSICAL_LINES:
                raise ValueError('solution source bound exceeded')
            digest.update(raw)
            native=raw[:-1].removesuffix(b'\r') if raw.endswith(b'\n') else raw
            line=native.decode('utf-8')
            if any(c in line for c in '\0\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029'):
                raise ValueError('unsupported physical line framing')
            stripped=line.strip()
            if not stripped:
                blank_lines+=1
                continue
            if stripped.startswith(existing.COMMENT):
                if epochs: raise ValueError('later header cannot redefine preceding epochs')
                header_comments+=1
                if header_comments>existing.MAX_HEADER_LINES: raise ValueError('header bound exceeded')
                chunks.append(raw)
                selected_size+=len(raw)
            else:
                if start_record<=epochs<stop_record:
                    chunks.append(raw)
                    selected_size+=len(raw)
                    positions.append((physical_lines,offset,epochs))
                epochs+=1
                if epochs>MAX_EPOCHS: raise ValueError('source epoch bound exceeded')
            if selected_size>existing.MAX_INPUT: raise ValueError('selected input bound exceeded; use smaller window')
        if signature(before)!=signature(os.fstat(stream.fileno())) or signature(before)!=signature(path.stat()) or total!=before.st_size:
            raise ValueError('source changed during read')
    if stop_record>epochs: raise ValueError('window exceeds source epochs')
    source_digest=digest.hexdigest()
    if expectation and (total!=expected_size or source_digest!=expected_sha256):
        raise ValueError('source differs from expected snapshot')
    selected=b''.join(chunks)
    output, report=existing.convert(selected,height_reference,horizontal_datum,time_system,
                                    reported_velocity=reported_velocity)
    selected_digest=report['source_sha256']
    reader=csv.DictReader(io.StringIO(output))
    result=io.StringIO(newline='')
    fields=[*reader.fieldnames,'source_record_index','source_record_byte_offset','selected_source_sha256']
    writer=csv.DictWriter(result,fields,lineterminator='\n'); writer.writeheader()
    count=0
    previous_limit=csv.field_size_limit(existing.MAX_OUTPUT)
    try:
        for row in reader:
            if count>=len(positions): raise ValueError('selected epoch accounting mismatch')
            number,offset,index=positions[count]
            row.update(source_line=number,source_record_index=index,source_record_byte_offset=offset,
                       source_sha256=source_digest,selected_source_sha256=selected_digest)
            writer.writerow(row); count+=1
            if result.tell()>existing.MAX_OUTPUT: raise ValueError('window output bound exceeded; use smaller window')
    finally:
        csv.field_size_limit(previous_limit)
    if count!=len(positions): raise ValueError('selected epoch accounting mismatch')
    report['source_sha256']=source_digest
    report['window']=dict(start_record=start_record,stop_record_exclusive=stop_record,
        source_epochs=epochs,selected_epochs=count,excluded_epochs=epochs-count,source_bytes=total,
        physical_lines=physical_lines,blank_lines=blank_lines,selected_source_sha256=selected_digest,
        selected_bytes=selected_size,outside_selection='FRAMING_AND_HEADER_PLACEMENT_ONLY_SEMANTICS_NOT_DECODED',
        statistics_scope='SELECTED_EPOCHS_ONLY_NOT_WHOLE_FILE_QUALITY_OR_CLOCK',
        retention='UNCHANGED_ORIGINAL_REQUIRED_SELECTED_BYTES_ARE_NOT_ANOTHER_RECORDING',
        bounds=dict(input_bytes=MAX_INPUT_BYTES,epochs=MAX_EPOCHS,physical_lines=MAX_PHYSICAL_LINES,line_bytes=MAX_LINE_BYTES,
                    selected_bytes=existing.MAX_INPUT,selected_epochs=existing.MAX_ROWS,output_bytes=existing.MAX_OUTPUT))
    if expectation: report['input_expectation']=expectation
    return result.getvalue(),report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path); parser.add_argument('output',type=Path)
    parser.add_argument('--start-record',type=int,required=True); parser.add_argument('--stop-record',type=int,required=True)
    parser.add_argument('--expected-size',type=int); parser.add_argument('--expected-sha256')
    parser.add_argument('--declare-height-reference',choices=existing.HEIGHT_TOKENS)
    parser.add_argument('--declare-datum',choices=existing.DATUM_TOKENS)
    parser.add_argument('--declare-time-system',choices=existing.TIME_SYSTEMS)
    parser.add_argument('--reported-velocity',action='store_true')
    args=parser.parse_args()
    try:
        output,report=convert_path(args.input,start_record=args.start_record,stop_record=args.stop_record,
            expected_size=args.expected_size,expected_sha256=args.expected_sha256,
            height_reference=args.declare_height_reference,horizontal_datum=args.declare_datum,
            time_system=args.declare_time_system,reported_velocity=args.reported_velocity)
        args.output.mkdir(parents=True,exist_ok=False)
        (args.output/'observations.csv').write_text(output,encoding='utf-8')
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        return 0
    except (ValueError,OSError,UnicodeError,csv.Error):
        print('RTKLIB solution window refused; retain input and check the declared scope',file=sys.stderr)
        return 2

if __name__=='__main__': raise SystemExit(main())
