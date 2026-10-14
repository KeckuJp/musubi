#!/usr/bin/env python3
"""Saved TensorRT10.7 normal exportOutput tensors; no inference or dtype guessing."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat

FIELDS = ['time_us','source_sha256','tensor_index','tensor_name_hex','shape_hex',
          'rank','volume','element_index','value','exact_number_text','value_status',
          'source_dtype','order_basis','unknown_keys_hex']


def pairs(items):
    result = {}
    for key, value in items:
        if key in result: raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def convert(path, capture_time_us):
    if type(capture_time_us) is not int or not 0 <= capture_time_us <= 2**53-1:
        raise ValueError('explicit capture time required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 16*1024**2:
            raise ValueError('regular file input bound')
        raw = stream.read(16*1024**2+1); after = os.fstat(stream.fileno())
    signature = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if len(raw)!=before.st_size or signature(before)!=signature(after):
        raise ValueError('source changed')
    def reject(value): raise ValueError('nonfinite JSON number')
    data = json.loads(raw, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=reject)
    if not isinstance(data,list) or len(data)>4096: raise ValueError('tensor collection bound')
    digest = hashlib.sha256(raw).hexdigest(); seen=set(); count=0
    out=io.StringIO(newline=''); writer=csv.writer(out); writer.writerow(FIELDS)
    report=dict(source_sha256=digest,tensors=len(data),values=0,empty_tensors=0,output_rows=0,
                unknown_fields=0, source_dtype='UNKNOWN_NOT_EXPORTED',clock='caller_capture_unknown',
                retention='retain exact input identified by source_sha256; unknown values not duplicated')
    for index,tensor in enumerate(data):
        if not isinstance(tensor,dict) or not {'name','dimensions','values'}<=tensor.keys():
            raise ValueError('normal exportOutput tensor required')
        name,shape,values=tensor['name'],tensor['dimensions'],tensor['values']
        if not isinstance(name,str) or len(name)>4096 or name in seen: raise ValueError('invalid tensor name')
        seen.add(name)
        if not isinstance(shape,str) or not re.fullmatch(r'\((?:[0-9]+(?:, [0-9]+)*)?\)',shape):
            raise ValueError('concrete writer shape required')
        dims=[] if shape=='()' else [int(x) for x in shape[1:-1].split(', ')]
        if len(dims)>32 or any(d>1000000 for d in dims): raise ValueError('shape bound')
        volume=math.prod(dims)
        if volume>1000000 or not isinstance(values,list) or len(values)!=volume:
            raise ValueError('shape/value cardinality mismatch')
        count+=volume
        if count>1000000: raise ValueError('aggregate value bound')
        unknown=sorted(set(tensor)-{'name','dimensions','values'})
        report['unknown_fields']+=len(unknown)
        if not volume: report['empty_tensors']+=1
        for ordinal,value in enumerate(values if volume else [None]):
            numeric=''; exact=''; status='EMPTY_TENSOR_NO_VALUE'
            if volume:
                if type(value) not in (int,Decimal): raise ValueError('reported numeric value required')
                exact=str(value)
                if len(exact)>1024: raise ValueError('numeric text bound')
                if type(value) is int and abs(value)>2**53-1:
                    status='EXACT_INTEGER_TEXT_ONLY'
                else:
                    numeric=float(value)
                    if not math.isfinite(numeric) or (value!=0 and numeric==0):
                        raise ValueError('number cannot reach common without overflow/underflow')
                    status='REPORTED_NUMBER_DTYPE_UNKNOWN'
            writer.writerow([capture_time_us,'sha256:'+digest,index,'hex:'+name.encode().hex(),
                'hex:'+shape.encode().hex(),len(dims),volume,ordinal if volume else '',numeric,
                'hex:'+exact.encode().hex() if volume else '',status,report['source_dtype'],
                'LOGICAL_ROW_MAJOR_LAST_AXIS_FASTEST','hex:'+json.dumps(unknown).encode().hex()])
            report['output_rows']+=1
            if out.tell()>128*1024**2: raise ValueError('output bound')
    report['values']=count
    return out.getvalue(),report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path); parser.add_argument('output',type=Path)
    parser.add_argument('--capture-time-us',type=int,required=True); args=parser.parse_args()
    try:
        if args.output.exists(): raise ValueError('output exists')
        output,report=convert(args.input,args.capture_time_us); args.output.mkdir()
        (args.output/'observations.csv').write_text(output,encoding='utf8')
        (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    except Exception:
        parser.exit(2,'Saved tensor conversion failed; unsupported/malformed export, no success claim\n')


if __name__=='__main__': main()
