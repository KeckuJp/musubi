#!/usr/bin/env python3
"""Passive saved Linux psutil 7.0.0 disk_usage; no host acquisition."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path

try:
    from scripts.convert_ardupilot_battery_csv import unique_object
    from scripts.convert_declared_expectation_check import text
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import unique_object
    from convert_declared_expectation_check import text

FORMAT='linux-psutil-7.0.0-ea5b55605f857affa4e65fa27eb80f4f2bfebd63-disk_usage'
LIMIT=16*1024**2
OUTPUT_LIMIT=128*1024**2
ROW_LIMIT=200000
QUANTITIES=('total','used','user_available','root_free','reserved_difference')
FIELDS=['capture_time_us','subject_hex','path_hex','reported_percent','percent_denominator_bytes_exact',
        *[f'{q}_bytes' for q in QUANTITIES],*[f'{q}_bytes_exact' for q in QUANTITIES],
        'source_record_hex','source_sha256','source_format','interpretation']


def integer(value, name, minimum=-(2**63)):
    if type(value) is not int or not minimum <= value <= 2**63-1:
        raise ValueError(f'{name} must be an integer in the declared range')
    return value


def convert(data):
    if not data or len(data)>LIMIT:
        raise ValueError('empty or oversized saved report')
    output=io.StringIO(newline=''); writer=csv.DictWriter(output,FIELDS,lineterminator='\n')
    writer.writeheader(); count=0; previous=None
    digest='sha256:'+hashlib.sha256(data).hexdigest()
    lines=data.decode('utf-8').split('\n')
    if lines[-1]=='': lines.pop()
    for line in lines:
        line=line.removesuffix('\r')
        if not line or len(line.encode())>512*1024 or count>=ROW_LIMIT:
            raise ValueError('empty, oversized or excessive report records')
        record=json.loads(line,object_pairs_hook=unique_object)
        if not isinstance(record,dict) or record.get('source_format')!=FORMAT:
            raise ValueError('explicit fixed Linux source declaration required')
        moment=integer(record.get('capture_time_us'),'capture_time_us',0)
        if previous is not None and moment<previous:
            raise ValueError('decreasing capture time; no reordering')
        previous=moment
        for field in ('subject','path'):
            if not isinstance(record.get(field),str) or not 1<=len(record[field].encode())<=4096:
                raise ValueError('nonempty bounded subject and path required')
        usage=record.get('disk_usage')
        if not isinstance(usage,dict): raise ValueError('disk_usage object required')
        total=integer(usage.get('total'),'total',0)
        used=integer(usage.get('used'),'used',0)
        available=integer(usage.get('free'),'free')
        if used>total or available>total-used:
            raise ValueError('inconsistent Linux free/used counters')
        percent=usage.get('percent')
        if type(percent) not in (int,float) or not math.isfinite(percent):
            raise ValueError('finite reported percent required')
        denominator=used+available
        # The pinned writer computes with float then round(..., 1); not used/total.
        expected=round(float(used)/denominator*100,1) if denominator else 0.0
        if percent!=expected:
            raise ValueError('percent inconsistent with fixed writer denominator')
        row=dict(capture_time_us=moment,subject_hex=text(record['subject']),path_hex=text(record['path']),
            reported_percent=percent,percent_denominator_bytes_exact=f'integer:{denominator}',
            source_record_hex='hex:'+line.encode().hex(),source_sha256=digest,source_format=FORMAT,
            interpretation='REPORTED_FILESYSTEM_COUNTERS_NOT_PHYSICAL_HEALTH_OR_ALLOCATION_GUARANTEE')
        values=(total,used,available,total-used,total-used-available)
        for name,value in zip(QUANTITIES,values):
            row[f'{name}_bytes_exact']=f'integer:{value}'
            row[f'{name}_bytes']=value if abs(value)<=2**53 else ''
        writer.writerow(row); count+=1
        if output.tell()>OUTPUT_LIMIT: raise ValueError('CSV output exceeds byte bound')
    if not count: raise ValueError('no report records')
    result=output.getvalue()
    report=dict(source_sha256=digest,source_records=count,output_rows=count,
        output_sha256='sha256:'+hashlib.sha256(result.encode()).hexdigest(),source_format=FORMAT,
        clock='CALLER_CAPTURE_UNKNOWN_NOT_PHYSICAL_SYNCHRONIZATION',
        evidence='SOURCE_SHAPED_INPUT_NOT_AUTHENTICATED_PRODUCER',
        source_retention='original bytes required; all record fields also retained as hex',
        bounds=dict(input_bytes=LIMIT,record_bytes=512*1024,records=ROW_LIMIT,output_bytes=OUTPUT_LIMIT))
    return result,report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path); parser.add_argument('output_dir',type=Path)
    args=parser.parse_args()
    try:
        if args.output_dir.exists(): raise ValueError('output directory exists; no overwrite')
        with args.input.open('rb') as stream: data=stream.read(LIMIT+1)
        output,report=convert(data)
        args.output_dir.mkdir()
        (args.output_dir/'source.jsonl').write_bytes(data)
        (args.output_dir/'observations.csv').write_text(output,encoding='utf-8')
        (args.output_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    except (ValueError,OSError,UnicodeError) as error:
        parser.exit(2,f'Saved filesystem conversion failed: {error}\n')


if __name__=='__main__': main()
