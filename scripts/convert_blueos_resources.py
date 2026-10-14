#!/usr/bin/env python3
"""Saved fixed BlueOS consumer resource contract; no service calls or commands."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.convert_ardupilot_battery_csv import unique_object
from scripts.recorded_units import normalize
PIN='0a0043f3f347c1663e11a32a61c723e9f79b675e'
PROFILE=ROOT/'profiles/declared/blueos-resources/profile.toml'
LIMIT=16*1024*1024
COUNTERS=('received_B','transmitted_B','total_received_B','total_transmitted_B',
    'packets_received','packets_transmitted','total_packets_received','total_packets_transmitted',
    'errors_on_received','errors_on_transmitted','total_errors_on_received','total_errors_on_transmitted')


def sha(raw):return 'sha256:'+hashlib.sha256(raw).hexdigest()


def reject(_):raise ValueError('nonfinite JSON')


def text(value):
    if not isinstance(value,str) or not 0<len(value.encode())<=4096:raise ValueError('bounded text required')
    return 'hex:'+value.encode().hex()


def quantity(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0 or value>2**53:
        raise ValueError('finite nonnegative quantity required')
    return value


def load(raw,limit):
    if not isinstance(raw,bytes) or not 0<len(raw)<=limit:raise ValueError('bounded saved input required')
    return json.loads(raw,object_pairs_hook=unique_object,parse_constant=reject)


def convert(raw,binding):
    obj=load(raw,LIMIT);config=load(binding,65536)
    if (not isinstance(obj,dict) or not isinstance(config,dict) or set(config)!=
        {'schema','source_pin','source_sha256','capture_time_us','session','citation'} or
        config['schema']!='blueos-saved-resources/v1' or config['source_pin']!=PIN or config['source_sha256']!=sha(raw)):
        raise ValueError('fixed source binding required')
    stamp=config['capture_time_us']
    if type(stamp) is not int or not 0<=stamp<2**63:raise ValueError('capture microseconds required')
    info=obj.get('info')
    if not isinstance(info,dict):raise ValueError('reported info object required')
    base=dict(capture_time_us=stamp,source_sha256=sha(raw),binding_sha256=sha(binding),
        session_hex=text(config['session']),citation_hex=text(config['citation']))
    for key in ('host_name','system_name','os_version','kernel_version'):
        base[key+'_hex']=text(info[key]) if info.get(key) is not None else ''
    exact=json.loads(raw,parse_float=Decimal,parse_constant=reject)
    unix=exact.get('unix_time_seconds')
    if unix is not None and (type(unix) not in (int,Decimal) or unix<0):raise ValueError('invalid reported Unix seconds')
    base['reported_unix_seconds_hex']=text(str(unix)) if unix is not None else ''
    rows=[];absent=[];output_bytes=0
    def add(kind,index,name,values):
        nonlocal output_bytes
        row=dict(base,resource_kind=kind,resource_index=index,resource_name_hex=text(name),**values)
        output_bytes+=sum(len(str(value)) for value in row.values())+256
        if len(rows)>=32768 or output_bytes>64*1024*1024-8192:raise ValueError('resource output bound exceeded')
        rows.append(row)
    def items(key):
        value=obj.get(key)
        if value is None:absent.append(key);return []
        if not isinstance(value,list) or len(value)>4096 or any(not isinstance(v,dict) for v in value):
            raise ValueError('bounded resource list required')
        if not value:absent.append(key)
        return value
    for i,item in enumerate(items('cpu')):
        frequency=quantity(item['frequency']);usage=quantity(item['usage'])
        if usage>100:raise ValueError('CPU usage exceeds percentage range')
        hz=frequency*1000000
        if not math.isfinite(hz):raise ValueError('frequency overflow')
        add('cpu',i,item['name'],dict(cpu_frequency_hz=hz,cpu_usage_fraction=normalize(usage,'%')[1]))
    memory=obj.get('memory')
    if memory is None:absent.append('memory')
    elif not isinstance(memory,dict):raise ValueError('memory object required')
    else:
        for i,name in enumerate(('ram','swap')):
            item=memory.get(name)
            if item is None:absent.append('memory.'+name);continue
            if not isinstance(item,dict):raise ValueError('memory quantities required')
            total=item.get('total_kB');used=item.get('used_kB')
            if total is not None:total=quantity(total)
            if used is not None:used=quantity(used)
            if total is not None and used is not None and used>total:raise ValueError('used memory exceeds total')
            ratio='';state='MISSING_QUANTITY'
            if total is not None and used is not None:
                state='ZERO_TOTAL_NO_RATIO' if total==0 else 'SAME_DECLARED_UNIT_RATIO'
                if total:ratio=used/total
            add('memory',i,name,dict(memory_total_declared_kB='' if total is None else total,
                memory_used_declared_kB='' if used is None else used,memory_used_fraction=ratio,memory_ratio_state=state))
    for i,item in enumerate(items('temperature')):
        values={}
        for key in ('temperature','maximum_temperature','critical_temperature'):
            value=item.get(key)
            values[key+'_k']='' if value is None else normalize(value,'degC')[1]
        values['temperature_state']='MISSING' if item.get('temperature') is None else 'REPORTED'
        add('temperature',i,item['name'],values)
    for i,item in enumerate(items('network')):
        values={}
        for key in COUNTERS:
            value=item.get(key)
            if value is not None and (type(value) is not int or not 0<=value<2**63):raise ValueError('exact nonnegative counter required')
            values[key]='' if value is None else value
        for key in ('is_up','is_loopback'):
            value=item.get(key)
            if value is not None and type(value) is not bool:raise ValueError('reported boolean required')
            values[key+'_reported']='' if value is None else int(value)
        add('network',i,item['name'],values)
    if not rows:raise ValueError('no selected resource quantities')
    columns=list(dict.fromkeys(key for row in rows for key in row))
    output=io.StringIO(newline='');writer=csv.DictWriter(output,fieldnames=columns,lineterminator='\n')
    writer.writeheader();writer.writerows(rows);result=output.getvalue()
    if len(rows)>32768 or len(result.encode())>64*1024*1024:raise ValueError('resource output bound exceeded')
    return result,dict(source=obj,source_sha256=sha(raw),binding=config,rows=len(rows),absent_resources=absent,
        unselected_sections=sorted(set(obj)-{'cpu','memory','temperature','network','info','unix_time_seconds'}),
        contract='BlueOS1.4.2 saved consumer System schema, not verified backend/installed response',
        nonclaim='no physical identity, capture UTC, health certification, kB byte base or inferred network rates')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path);parser.add_argument('binding',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    try:
        with args.input.open('rb') as f:raw=f.read(LIMIT+1)
        with args.binding.open('rb') as f:binding=f.read(65537)
        result,report=convert(raw,binding);args.output.mkdir()
        (args.output/'source-response.json').write_bytes(raw)
        (args.output/'observations.csv').write_text(result)
        (args.output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    except (OSError,ValueError,KeyError,TypeError):raise SystemExit('saved resource conversion refused')


if __name__=='__main__':main()
