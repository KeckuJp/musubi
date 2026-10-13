"""Passive fixed writer IMU batches; caller configuration is not attestation."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sys
from importlib.metadata import version

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import export_ardupilot_records as export
from scripts.convert_ardupilot_battery_csv import unique_object

PIN='9064e22695992593b12e71a2b2ad25faba2f7277'
PROFILE=ROOT/'profiles/declared/ardupilot-imu-batch/profile.toml'
LAYOUTS={'ISBH':('QHBBHHQf','TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate'),
         'ISBD':('QHHaaa','TimeUS,N,seqno,x,y,z')}
CONFIG={'schema','source_pin','source_sha256','ins_bat_lgct','sensor_rate_logging','single_boot','citation'}


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('integer outside qualified range')
    return value


def saved_records(path, raw, text_basis):
    """Feed the same batch semantics, retaining original byte locations."""
    if text_basis is not None:
        from scripts.convert_dataflash_text import convert as text_convert
        from pymavlink.DFReader import DFFormat
        records = text_convert(raw, array_basis=text_basis).splitlines()
        lines = raw.splitlines(keepends=True)
        if len(records) != len(lines):
            raise ValueError('text record accounting differs')
        formats = {}; offset = 0
        for encoded, line in zip(records, lines):
            item = json.loads(encoded); name = item['meta']['type']; data = item['data']
            if name == 'FMT':
                formats[data['Name']] = DFFormat(data['Type'], data['Name'], data['Length'],
                                                data['Format'], data['Columns'])
            fmt = formats[name]
            yield name, fmt, data, offset, offset + len(line)
            offset += len(line)
        return
    from pymavlink.DFReader import DFReader_binary
    reader = DFReader_binary(str(path))
    try:
        if reader.data_len != len(raw) or bytes(reader.data_map[:reader.data_len]) != raw:
            raise ValueError('original changed during decoding')
        while True:
            message = reader.recv_msg()
            if message is None:
                break
            end = reader.offset
            data = message.to_dict() if message.fmt.name in LAYOUTS else {}
            yield message.fmt.name, message.fmt, data, end-message.fmt.len, end
    finally:
        reader.data_map.close(); reader.filehandle.close()


def convert(path, declaration, *, text_basis=None):
    if not isinstance(declaration,bytes) or not 0<len(declaration)<=65536:
        raise ValueError('bounded declaration required')
    config=json.loads(declaration,object_pairs_hook=unique_object)
    raw=export.bounded_read(path,export.MAX_INPUT)
    digest='sha256:'+hashlib.sha256(raw).hexdigest()
    config_digest='sha256:'+hashlib.sha256(declaration).hexdigest()
    if (not isinstance(config,dict) or set(config)!=CONFIG or config['schema']!='ardupilot-imu-batch/v1'
        or config['source_pin']!=PIN or config['source_sha256']!=digest
        or type(config['ins_bat_lgct']) is not int or config['ins_bat_lgct']!=32
        or config['sensor_rate_logging'] is not False or config['single_boot'] is not True
        or not isinstance(config['citation'],str) or not 0<len(config['citation'].encode())<=4096):
        raise ValueError('fixed source/configuration declaration required')
    if version('pymavlink')!=export.PYMAVLINK:
        raise ValueError('fixed passive decoder required')
    records=saved_records(path,raw,text_basis)
    rows=[];unselected=[];unselected_counts={};cursor=0;total=0;header=None;consumed=0;batches=0;previous=-1
    size=0; schemas={}
    try:
        for name,fmt,data,start,end in records:
            total+=1
            if total>export.MAX_MESSAGES:raise ValueError('record bound exceeded')
            if start!=cursor or end>len(raw):raise ValueError('undecoded or overlapping bytes')
            cursor=end
            if name not in LAYOUTS:
                unselected_counts[name]=unselected_counts.get(name,0)+1
                if unselected and unselected[-1]['stop_record']==total-1:
                    unselected[-1].update(stop_record=total,stop_offset=end)
                else:
                    unselected.append(dict(start_record=total-1,stop_record=total,
                                           start_offset=start,stop_offset=end))
                continue
            if (fmt.format,','.join(fmt.columns))!=LAYOUTS[name]:raise ValueError('unqualified batch FMT')
            schema=export.schema_of(fmt)
            if name in schemas and schemas[name]!=schema:raise ValueError('changed batch declaration')
            schemas[name]=schema
            stamp=integer(data['TimeUS'],0,2**63-1)
            if stamp<previous:raise ValueError('recording clock reset')
            previous=stamp
            if name=='ISBH':
                if header is not None and consumed!=header['smp_cnt']:raise ValueError('incomplete preceding batch')
                integer(data['type'],0,1);integer(data['mul'],1,65535)
                integer(data['smp_cnt'],32,65504)
                integer(data['SampleUS'],0,2**63-1)
                if data['smp_cnt']%32 or data['SampleUS']>stamp:raise ValueError('invalid sample count/clock')
                rate=data['smp_rate']
                if type(rate) not in (float,int) or not math.isfinite(rate) or rate<=0:raise ValueError('invalid sample rate')
                header=dict(data,source_index=total-1);consumed=0;batches+=1
                continue
            if header is None or data['N']!=header['N'] or data['seqno']!=consumed//32:
                raise ValueError('missing header or inconsistent batch sequence')
            if consumed+32>header['smp_cnt']:raise ValueError('batch exceeds declared count')
            arrays=[data[axis] for axis in 'xyz']
            if any(len(values)!=32 for values in arrays):raise ValueError('invalid array length')
            for i in range(32):
                values=[integer(values[i],-32768,32767) for values in arrays]
                offset=(consumed+i)/header['smp_rate']
                if not math.isfinite(offset):raise ValueError('nominal offset overflow')
                row=dict(record_time_us=stamp,batch_first_sample_us=header['SampleUS'],nominal_offset_s=offset,
                    sample_index=consumed+i,batch_number_reported=header['N'],batch_header_record_index=header['source_index'],
                    source_record_index=total-1,source_offset=start,source_length=end-start,packet_sequence=data['seqno'],
                    array_index=i,sensor_type_reported=header['type'],instance_reported=header['instance'],
                    quantization_multiplier=header['mul'],sample_rate_hz=header['smp_rate'],source_sha256=digest,
                    configuration_sha256=config_digest)
                for axis,value in zip('xyz',values):
                    row['raw_'+axis]=value
                    row['acceleration_'+axis+'_m_s2']=value/header['mul'] if header['type']==0 else ''
                    row['angular_rate_'+axis+'_rad_s']=value/header['mul'] if header['type']==1 else ''
                size+=sum(len(str(value)) for value in row.values())+len(row)
                if len(rows)>=1000000 or size>export.MAX_OUTPUT-4096:raise ValueError('output bound exceeded')
                rows.append(row)
            consumed+=32
        if cursor!=len(raw):raise ValueError('undecoded trailing bytes')
        if header is None or consumed!=header['smp_cnt']:raise ValueError('absent/incomplete batch')
    finally:
        records.close()
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=list(rows[0]),lineterminator='\n')
    writer.writeheader();writer.writerows(rows)
    report=dict(source_sha256=digest,source_bytes=len(raw),source_pin=PIN,
        configuration=config,total_records=total,batches=batches,samples=len(rows),schemas=schemas,
        unselected_spans=unselected,unselected_counts=unselected_counts,framing=export.COMPLETE,
        clock='recording TimeUS boot-relative; SampleUS first sample only; offsets nominal not measured timestamps',
        nonclaim='no real-record validation, sensor identity, absolute clock, calibration, physical frame or all-message coverage')
    if text_basis is not None:
        report.update(source_format='mission-planner-text',text_exporter_basis=text_basis,
            framing='complete source-line accounting; offsets/lengths refer to original text bytes, not binary',
            original_text_hex='hex:'+raw.hex())
    return stream.getvalue(),report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('declaration',type=Path);parser.add_argument('output',type=Path)
    from scripts.convert_dataflash_text import ARRAY_BASIS
    parser.add_argument('--text-basis',choices=[ARRAY_BASIS])
    args=parser.parse_args()
    try:
        text,report=convert(args.input,export.bounded_read(args.declaration,65536),text_basis=args.text_basis)
        args.output.mkdir()
        export.write(args.output/'observations.csv',text)
        export.write(args.output/'report.json',json.dumps(report,indent=2,allow_nan=False)+'\n')
    except (ValueError,OSError,KeyError,TypeError):
        raise SystemExit('saved batch conversion refused')


if __name__=='__main__':main()
