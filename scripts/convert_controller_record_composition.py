"""Bind selected adopted saved reports to a caller-declared controller; no temporal join."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from scripts import convert_dronecan_capture as can
from scripts import convert_dbc_recorded as dbc
from scripts.convert_software_identity_binding import read_rows
from scripts.convert_ardupilot_battery_csv import unique_object

ROOT=Path(__file__).resolve().parents[1]
BASIS='CALLER_DECLARED_ASSOCIATION_NOT_AUTHENTICATED'
ROLES={'state','diagnostic','firmware','signal'}
FORMATS={'dronecan':'dronecan-recorded/profile.toml','canopen':'dronecan-recorded/profile.toml',
         'j1939':'dronecan-recorded/profile.toml','dbc':'dbc-recorded/profile.toml'}
FIELDS=('record_time_us','source_capture_time_us','association_clock_basis','input_index','input_sha256','source_row_index','source_namespace',
        'source_key','source_clock_hex','session_hex','entity_hex','citation_hex','manifest_sha256',
        'association_basis','qualified_roles','row_disposition')


def digest(data):
    return 'sha256:'+hashlib.sha256(data).hexdigest()


def text(value):
    if not isinstance(value,str) or not 1<=len(value.encode())<=1024 or any(ord(x)<32 for x in value):
        raise ValueError('invalid declaration')
    return 'hex:'+value.encode().hex()


def number(value):
    if value=='':
        return False
    try:
        if not math.isfinite(float(value)):
            raise ValueError('nonfinite quantity')
    except (ValueError,OverflowError):
        raise ValueError('invalid quantity')
    return True


def row_roles(row,kind):
    roles=set()
    if kind=='dronecan':
        modes={'0':'OPERATIONAL','1':'INITIALIZATION','2':'MAINTENANCE','3':'SOFTWARE_UPDATE','7':'OFFLINE'}
        if row['data_type']=='NodeStatus' and row['transfer_kind']=='MESSAGE' and row['node_mode_code'] in modes:
            if row['node_reported_mode']!=modes[row['node_mode_code']] or row['node_health_code'] not in ('0','1','2','3'):
                raise ValueError('inconsistent node state')
            roles.add('state')
        if row['data_type']=='GetNodeInfo.Response' and row['transfer_kind']=='SERVICE_RESPONSE' and row['node_software_version_disposition']=='REPORTED_BY_NODE_NOT_VERIFIED':
            major,minor=row['node_software_major_reported'],row['node_software_minor_reported']
            if not all(x.isdigit() and 0<=int(x)<=255 for x in (major,minor)) or int(major)+int(minor)==0:
                raise ValueError('invalid qualified software version')
            roles.add('firmware')
        if row['data_type']=='ESCStatus' and row['transfer_kind']=='MESSAGE' and any([number(row[x]) for x in ('esc_voltage_v','esc_current_a','esc_temperature_k','reported_rpm')]):
            roles.add('signal')
    elif kind=='canopen':
        modes={'0':'BOOT_UP','4':'STOPPED','5':'OPERATIONAL','127':'PRE_OPERATIONAL'}
        if row['canopen_state_code'] in modes:
            if row['node_reported_mode']!=modes[row['canopen_state_code']]:
                raise ValueError('inconsistent default heartbeat')
            roles.add('state')
        if row['canopen_error_report_kind'] in ('ERROR_REPORTED','ERROR_RESET_REPORTED'):
            code=row['canopen_error_code']
            if not code.isdigit() or not 0<=int(code)<=65535:
                raise ValueError('invalid error code')
            if (int(code)==0)!=(row['canopen_error_report_kind']=='ERROR_RESET_REPORTED'):
                raise ValueError('inconsistent emergency code')
            roles.add('diagnostic')
    elif kind=='j1939':
        if row['diagnostic_report_kind'] in ('ACTIVE_DTC_REPORTED','PREVIOUS_DTC_REPORTED'):
            if row['j1939_pgn'] not in ('65226','65227') or not row['spn_reported'].isdigit() or not 1<=int(row['spn_reported'])<=524287:
                raise ValueError('invalid qualified DTC')
            if (row['j1939_pgn']=='65226')!=(row['diagnostic_report_kind']=='ACTIVE_DTC_REPORTED'):
                raise ValueError('inconsistent DTC kind')
            for field,limit in [('fmi_reported',31),('occurrence_count_code',127)]:
                if not row[field].isdigit() or not 0<=int(row[field])<=limit:
                    raise ValueError('invalid DTC code')
            roles.add('diagnostic')
    else:
        if row['frame_kind'] not in ('standard','extended','standard_fd','extended_fd'):
            raise ValueError('unqualified frame kind')
        if any([number(row[x]) for x in dbc.FIELDS[6:]]):
            roles.add('signal')
    return roles


def convert(inputs,manifest):
    if not isinstance(inputs,list) or not 1<=len(inputs)<=8 or any(not isinstance(x,bytes) or len(x)>16*1024**2 for x in inputs) or sum(map(len,inputs))>64*1024**2:
        raise ValueError('input budget')
    if not isinstance(manifest,bytes) or len(manifest)>16384:
        raise ValueError('manifest budget')
    spec=json.loads(manifest,object_pairs_hook=unique_object)
    if set(spec)!={'schema','basis','session','entity','citation','sources'} or spec['schema']!='controller-record-composition-v1' or spec['basis']!=BASIS:
        raise ValueError('unsupported declaration')
    labels={key+'_hex':text(spec[key]) for key in ('session','entity','citation')}
    if not isinstance(spec['sources'],list) or len(spec['sources'])!=len(inputs):
        raise ValueError('source count')
    output=io.StringIO(newline=''); writer=csv.DictWriter(output,FIELDS,lineterminator='\n'); writer.writeheader()
    reports=[]; union=set(); total=0; count=0; seen=set(); manifest_digest=digest(manifest)
    for i,(data,source) in enumerate(zip(inputs,spec['sources'])):
        if not isinstance(source,dict) or set(source)!={'format','sha256','profile_sha256','key','clock','start_us','stop_us','dictionary_sha256'}:
            raise ValueError('source declaration shape')
        kind=source['format']
        source_digest=digest(data)
        if kind not in FORMATS or source['sha256']!=source_digest:
            raise ValueError('source format/digest mismatch')
        profile=(ROOT/'profiles/declared'/FORMATS[kind]).read_bytes()
        if source['profile_sha256']!=digest(profile):
            raise ValueError('fixed profile digest mismatch')
        start,stop=source['start_us'],source['stop_us']
        if type(start) is not int or type(stop) is not int or not 0<=start<stop<=2**63-1:
            raise ValueError('invalid source interval')
        key=source['key']
        maxkey=2**29-1 if kind=='dbc' else 253 if kind=='j1939' else 127
        if type(key) is not int or not (0 if kind in ('dbc','j1939') else 1)<=key<=maxkey:
            raise ValueError('invalid subject key')
        clock=text(source['clock'])
        dictionary=source['dictionary_sha256']
        if kind=='dbc' and (not isinstance(dictionary,str) or len(dictionary)!=71 or not dictionary.startswith('sha256:') or any(c not in '0123456789abcdef' for c in dictionary[7:])):
            raise ValueError('invalid dictionary digest')
        if (source['sha256'],kind,key,start,stop) in seen:
            raise ValueError('duplicate selection')
        seen.add((source['sha256'],kind,key,start,stop))
        header,_,rows=read_rows(data,'record_time_us'); total+=len(rows)
        if total>200000:
            raise ValueError('row budget')
        expected=set(can.META+can.FIELDS) if kind=='dronecan' else set(dbc.FIELDS) if kind=='dbc' else {
            'record_time_us','source_node_id','source_frames_hex'}|({'node_reported_mode','canopen_state_code','canopen_error_code','canopen_error_report_kind','canopen_error_register'} if kind=='canopen' else {'j1939_pgn','diagnostic_report_kind','spn_reported','fmi_reported','occurrence_count_code'})
        if not expected<=set(header):
            raise ValueError('adopted schema required')
        selected=0; qualified=0
        for ordinal,(_,values) in enumerate(rows):
            row=dict(zip(header,values)); moment=int(row['record_time_us'])
            subject=row['frame_id' if kind=='dbc' else 'source_node_id']
            if subject!=str(key) or not start<=moment<stop:
                continue
            if kind=='dbc' and (not source['dictionary_sha256'] or row['dbc_sha256']!=source['dictionary_sha256']):
                raise ValueError('dictionary binding mismatch')
            if kind!='dbc' and source['dictionary_sha256'] is not None:
                raise ValueError('unexpected dictionary')
            roles=row_roles(row,kind); union|=roles; selected+=1; qualified+=bool(roles)
            writer.writerow(dict(labels,record_time_us=0,source_capture_time_us=moment,
                association_clock_basis='UNLOCATED_ASSOCIATION_NOT_CAPTURE',input_index=i,input_sha256=source_digest,
                source_row_index=ordinal,source_namespace=kind,source_key=key,source_clock_hex=clock,
                manifest_sha256=manifest_digest,association_basis=BASIS,
                qualified_roles='|'.join(sorted(roles)) or 'NONE',row_disposition='QUALIFIED_REPORT' if roles else 'NO_REQUIRED_ROLE'))
            count+=1
            if output.tell()>32*1024**2:
                raise ValueError('output budget')
        if selected==0:
            raise ValueError('selected subject/interval absent')
        reports.append(dict(input_index=i,rows=len(rows),selected=selected,excluded=len(rows)-selected,
                            qualified=qualified,clock=source['clock'],profile_path=FORMATS[kind]))
    return output.getvalue(),dict(complete=union==ROLES,missing_roles=sorted(ROLES-union),sources=reports,
        output_rows=count,clock_policy='PER_SOURCE_NO_COMPARISON_OR_SYNCHRONIZATION',
        retention='Read unchanged source CSV with fixed profile AND this association sidecar; no version carryforward')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path); parser.add_argument('output',type=Path)
    parser.add_argument('inputs',type=Path,nargs='+'); args=parser.parse_args()
    try:
        def read(path,bound):
            with path.open('rb') as f: data=f.read(bound+1)
            if len(data)>bound: raise ValueError('input bound')
            return data
        manifest=read(args.manifest,16384)
        if len(args.inputs)>8: raise ValueError('source count')
        inputs=[]; total=0
        for path in args.inputs:
            data=read(path,16*1024**2); total+=len(data)
            if total>64*1024**2: raise ValueError('aggregate budget')
            inputs.append(data)
        sidecar,report=convert(inputs,manifest); args.output.mkdir()
        (args.output/'associations.csv').write_text(sidecar,encoding='utf-8')
        (args.output/'manifest.json').write_bytes(manifest)
        for i,data in enumerate(inputs): (args.output/f'source-{i}.csv').write_bytes(data)
        (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError):
        parser.exit(2,'Saved controller composition failed; no completion claim\n')


if __name__=='__main__': main()
