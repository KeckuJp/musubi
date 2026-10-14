#!/usr/bin/env python3
"""Convert fixed saved Farmduino reports; never open a serial connection."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
try:
    from scripts.convert_ardupilot_battery_csv import FARM_FIELDS, farmduino_position, unique_object
except ModuleNotFoundError:
    from convert_ardupilot_battery_csv import FARM_FIELDS, farmduino_position, unique_object

PIN='52053ec86e6cef1a39340439dfdfaf9cb2337a60'
LIMIT=16*1024*1024
EVENTS={'R01':'START_REPORTED','R02':'DONE_REPORTED','R07':'RETRY_REPORTED',
        'R09':'INVALID_COMMAND_REPORTED','R87':'EMERGENCY_STOP_REPORTED','R88':'NO_CONFIG_REPORTED'}
FIELDS=['record_time_us','source_sha256','source_record_index','source_byte_offset','source_record_hex',
        'firmware_source_pin','report_code','report_kind','report_disposition','q_reported_text',
        'error_code_reported','parameter_id','parameter_value_text','configuration_ready_reported',
        'configuration_value_basis',*['endstop_'+x for x in ('XA','XB','YA','YB','ZA','ZB')],
        'parameter_axis','parameter_reported_steps_per_mm',
        *FARM_FIELDS,'farmduino_q_reported_text','farmduino_position_basis']

def i32(text):
    value=int(text)
    if not -(2**31)<=value<2**31:raise ValueError('reported integer outside signed32 scope')
    return value

def convert(data):
    if not isinstance(data,bytes) or not data or len(data)>LIMIT:raise ValueError('bounded nonempty JSONL required')
    digest=hashlib.sha256(data).hexdigest();out=io.StringIO()
    writer=csv.DictWriter(out,FIELDS,lineterminator='\n');writer.writeheader()
    report={'source_sha256':digest,'records':0,'qualified':0,'unqualified':0,'partial_parameter':0,'by_code':{},
            'basis':'SAVED_FIRMWARE_REPORT_NOT_PHYSICAL_VERIFICATION_OR_AUTHORIZATION'}
    offset=0
    for index,line in enumerate(data.splitlines(keepends=True)):
        if len(line)>30*1024 or not line.strip():raise ValueError('invalid bounded JSONL record')
        record=json.loads(line,object_pairs_hook=unique_object,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        if type(record) is not dict or record.get('firmware_commit')!=PIN:raise ValueError('unqualified firmware pin')
        stamp=record.get('capture_time_us');response=record.get('response')
        if type(stamp) is not int or not 0<=stamp<2**63:raise ValueError('capture microseconds required')
        if type(response) is not str or not re.fullmatch(r'R[0-9]{2}[^\r\n\x00]*(?:\r\n)?',response):raise ValueError('single saved report required')
        code=response[:3];body=response.removesuffix('\r\n')
        row=dict.fromkeys(FIELDS,'');row.update(record_time_us=stamp,source_sha256=digest,
             source_record_index=index,source_byte_offset=offset,source_record_hex='hex:'+line.hex(),
             firmware_source_pin=PIN,report_code=code,report_disposition='QUALIFIED_REPORTED_NOT_PHYSICAL')
        if code=='R82':
            values=farmduino_position(record)
            row.update(zip([*FARM_FIELDS,'farmduino_q_reported_text','farmduino_position_basis'],values))
            row.update(report_kind='POSITION_REPORTED',q_reported_text=values[-2])
        elif code=='R81':
            match=re.fullmatch(r'R81 XA([01]) XB([01]) YA([01]) YB([01]) ZA([01]) ZB([01]) Q(-?[0-9]+)',body)
            if not match:raise ValueError('complete six reported endstop bits required')
            row.update(zip(['endstop_'+x for x in ('XA','XB','YA','YB','ZA','ZB')],['true' if v=='1' else 'false' for v in match.groups()[:6]]))
            row.update(report_kind='ENDSTOP_BITS_REPORTED_NOT_CONTACT',q_reported_text='i32:'+str(i32(match[7])))
        elif code in EVENTS or code=='R03':
            match=re.fullmatch(code+(r' V(-?[0-9]+)' if code=='R03' else '')+r' Q(-?[0-9]+)',body)
            if not match:raise ValueError('malformed dispatcher report')
            row.update(report_kind=EVENTS.get(code,'ERROR_REPORTED'),q_reported_text='i32:'+str(i32(match.groups()[-1])))
            if code=='R03':row['error_code_reported']=i32(match[1])
        elif code=='R21':
            match=re.fullmatch(r'R21 P([0-9]+) V(-?[0-9]+) Q(-?[0-9]+)',body)
            if not match:raise ValueError('malformed parameter report')
            key=i32(match[1]);value=i32(match[2])
            row.update(report_kind='PARAMETER_REPORTED',parameter_id=key,parameter_value_text='i32:'+str(value),
                       q_reported_text='i32:'+str(i32(match[3])),configuration_value_basis='PARAMETER_NAMESPACE_RAW_INTEGER_PHYSICAL_UNIT_UNQUALIFIED')
            if key==2:
                row.update(configuration_ready_reported='true' if value==1 else 'false',
                           configuration_value_basis='PARAM_CONFIG_OK_EQUALS_ONE_DECLARED_GATE_NOT_ACTUAL_READINESS')
            elif key in (55,56,57):
                row.update(parameter_axis={55:'X',56:'Y',57:'Z'}[key],parameter_reported_steps_per_mm=value,
                           configuration_value_basis='REPORTED_STEPS_PER_MM_NOT_CALIBRATED_OR_AUTO_APPLIED')
                if value<=0:row['configuration_value_basis']='NONPOSITIVE_REPORTED_SCALE_NOT_USABLE_FOR_POSITION'
            else:
                row['report_disposition']='PARAMETER_KEY_VALUE_RETAINED_UNIT_UNQUALIFIED'
                report['partial_parameter']+=1
        else:
            row.update(report_kind='UNQUALIFIED_REPORT',report_disposition='RAW_RETAINED_NOT_MEANING_SUCCESS')
            report['unqualified']+=1
        report['records']+=1;report['by_code'][code]=report['by_code'].get(code,0)+1
        writer.writerow(row);offset+=len(line)
        if out.tell()>LIMIT:raise ValueError('common CSV exceeds bound')
    report['qualified']=report['records']-report['unqualified']-report['partial_parameter']
    return out.getvalue(),report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    with args.input.open('rb') as handle:data=handle.read(LIMIT+1)
    output,report=convert(data);args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'observations.csv').write_text(output)
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()
