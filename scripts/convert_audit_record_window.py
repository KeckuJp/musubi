"""Bounded saved audit JSONL selection; no cluster access or global completion inference."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.convert_vda_load_report import read_jsonl_window
from scripts.convert_k8s_audit_events import convert, TIME_FIELDS, OUTPUT_LIMIT

SCOPE='SELECTED_WINDOW_ONLY_NOT_WHOLE_REQUEST_OR_FILE_COMPLETENESS'


def convert_path(path, *, start_record, stop_record, time_field, redact=False):
    selected,offsets,count,selected_bytes,digest=read_jsonl_window(
        path,start_record=start_record,stop_record=stop_record)
    text=''.join(selected)
    converted,semantics=convert(text,time_field=time_field,redact=redact)
    selected_digest='sha256:'+hashlib.sha256(text.encode()).hexdigest()
    output=io.StringIO(newline='')
    previous=csv.field_size_limit(2*1024*1024)
    try:
        reader=csv.DictReader(io.StringIO(converted))
        writer=csv.DictWriter(output,fieldnames=reader.fieldnames+[
            'source_record_byte_offset','selected_normalized_sha256','audit_stage_accounting_scope'],lineterminator='\n')
        writer.writeheader()
        rows=0
        for row in reader:
            local=int(row['source_record_index'])-1
            if local != rows or not 0<=local<len(selected):
                raise ValueError('audit selected record accounting mismatch')
            row.update(source_record_index=start_record+local+1,
                source_record_byte_offset=offsets[local],source_file_sha256='sha256:'+digest,
                selected_normalized_sha256=selected_digest,audit_stage_accounting_scope=SCOPE)
            row['audit_id_has_response_complete'] = row['audit_id_has_response_complete'].replace(
                'IN_THIS_FILE', 'IN_SELECTED_WINDOW')
            writer.writerow(row);rows+=1
            if output.tell()>OUTPUT_LIMIT:raise ValueError('window CSV exceeds bound')
        if rows!=len(selected):raise ValueError('audit selected record omitted')
    finally:csv.field_size_limit(previous)
    return output.getvalue(),dict(source_sha256='sha256:'+digest,
        source_records=count,start_record=start_record,stop_record=stop_record,
        selected_records=len(selected),selected_bytes=selected_bytes,
        excluded_records_not_decoded=count-len(selected),output_rows=rows,
        selected_normalized_sha256=selected_digest,selected_semantics=semantics,
        audit_stage_accounting_scope=SCOPE,
        retention='Keep unchanged full source; selected LF-normalized digest is separate. '
        'Indices are one-based original physical lines; offsets are zero-based bytes. '
        'Outside JSON and stage meanings are NOT decoded; boundary-absent stages are not lost requests.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--start-record',type=int,required=True)
    parser.add_argument('--stop-record',type=int,required=True)
    parser.add_argument('--time-field',choices=tuple(TIME_FIELDS),required=True)
    parser.add_argument('--redact',action='store_true')
    args=parser.parse_args()
    try:
        output,report=convert_path(args.input,start_record=args.start_record,stop_record=args.stop_record,
            time_field=args.time_field,redact=args.redact)
        args.output.mkdir()
        (args.output/'observations.csv').write_text(output,encoding='utf-8')
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    except (ValueError,OSError,UnicodeError,csv.Error):
        parser.exit(2,'Saved audit window conversion failed; no successful conversion claim\n')


if __name__=='__main__':main()
