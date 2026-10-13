"""Passive saved drm_info v2.7.0 connector link reports, not mechanical diagnosis."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

PIN = 'drm_info@c1f5ca4cf750b26eb26c1d9d5c2ef057acbcfefc_linux@adc218676eef25575469234709c2d87185ca223a'
BASIS = 'PRODUCER_REPORTED_CONNECTOR_LINK_NOT_MECHANICAL_DAMAGE_OR_AUTHENTICATED_HEALTH'
LIMIT = 16 * 1024 * 1024
FIELDS = ['record_time_us','capture_basis','node_index','node_path_hex','connector_index',
          'connector_id','connector_type_reported','connection_status_raw','link_status',
          'link_value_hex','link_label_hex','property_id','property_disposition',
          'other_properties_count','source_sha256','meaning_basis','source_pin']


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate saved JSON key')
        result[key] = value
    return result


def uint(value, bits=32):
    if type(value) is not int or not 0 <= value < 2**bits:
        raise ValueError('reported unsigned integer invalid')
    return value


def text(value):
    if not isinstance(value, str) or len(value.encode()) > 4096:
        raise ValueError('bounded reported text required')
    return 'hex:' + value.encode().hex()


def convert(raw, capture_time_us):
    if not raw or len(raw) > LIMIT:
        raise ValueError('empty or oversized saved JSON')
    uint(capture_time_us,63)
    root = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(root, dict) or not 1 <= len(root) <= 64:
        raise ValueError('bounded node map required')
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output,FIELDS,lineterminator='\n'); writer.writeheader()
    report = dict(source_sha256=digest,connectors=0,dispositions={},nodes_without_connectors=0,
                  other_properties_count=0,original_retained=True,meaning_basis=BASIS)
    for ni,(path,node) in enumerate(root.items()):
        path_hex = text(path)
        if not isinstance(node,dict) or not isinstance(node.get('connectors'),list):
            raise ValueError('saved node connectors array required')
        connectors = node['connectors']
        if not connectors:
            report['nodes_without_connectors'] += 1
        ids = set()
        for ci,connector in enumerate(connectors):
            if not isinstance(connector,dict):
                raise ValueError('connector object required')
            cid = uint(connector.get('id'))
            if cid in ids:
                raise ValueError('duplicate connector identifier within saved node')
            ids.add(cid)
            props = connector.get('properties')
            if props is not None and not isinstance(props,dict):
                raise ValueError('property map or writer null required')
            state,disposition,value_hex,label_hex,pid = 'UNKNOWN','PROPERTIES_UNAVAILABLE','','',''
            others = 0
            if props is not None:
                others = len(props) - int('link-status' in props)
                disposition = 'LINK_STATUS_NOT_REPORTED'
                if 'link-status' in props:
                    prop = props['link-status']
                    if not isinstance(prop,dict) or prop.get('type') != 8 or isinstance(prop.get('type'),bool):
                        raise ValueError('link-status must be an enum property')
                    flags = uint(prop.get('flags'))
                    if flags & (2|8|16|32|0xffc0) != 8:
                        raise ValueError('property flags contradict enum type')
                    pid = uint(prop.get('id'))
                    value = uint(prop.get('raw_value'),64)
                    if uint(prop.get('value'),64) != value:
                        raise ValueError('enum value differs from raw value')
                    spec = prop.get('spec')
                    if not isinstance(spec,list) or len(spec) > 256:
                        raise ValueError('bounded enum declaration required')
                    labels = {}
                    for item in spec:
                        if not isinstance(item,dict):
                            raise ValueError('enum item invalid')
                        code = uint(item.get('value'),64)
                        label = item.get('name'); text(label)
                        if code in labels:
                            raise ValueError('duplicate enum code')
                        labels[code] = label
                    if labels.get(0) != 'Good' or labels.get(1) != 'Bad':
                        raise ValueError('link-status declaration contradicts qualified enum')
                    state = {0:'GOOD_REPORTED',1:'BAD_REPORTED'}.get(value,'UNKNOWN_REPORTED_VALUE')
                    disposition = 'REPORTED'
                    value_hex = 'hex:' + format(value,'016x')
                    label_hex = text(labels[value]) if value in labels else ''
            writer.writerow(dict(record_time_us=capture_time_us,capture_basis='CALLER_CAPTURE_UNKNOWN_NOT_EVENT_TIME',
                node_index=ni,node_path_hex=path_hex,connector_index=ci,connector_id=cid,
                connector_type_reported=uint(connector.get('type')),connection_status_raw=uint(connector.get('status')),
                link_status=state,link_value_hex=value_hex,link_label_hex=label_hex,property_id=pid,
                property_disposition=disposition,other_properties_count=others,source_sha256=digest,
                meaning_basis=BASIS,source_pin=PIN))
            report['connectors'] += 1
            report['other_properties_count'] += others
            report['dispositions'][disposition] = report['dispositions'].get(disposition,0)+1
            if report['connectors'] > 4096 or output.tell() > 64*1024*1024:
                raise ValueError('connector output bound exceeded')
    if not report['connectors']:
        raise ValueError('no reported connector observations')
    return output.getvalue(),report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path); parser.add_argument('new_output_directory',type=Path)
    parser.add_argument('--capture-time-us',required=True,type=int)
    args=parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError('input bound exceeded')
        raw=args.input.read_bytes()
        converted,report=convert(raw,args.capture_time_us)
        args.new_output_directory.mkdir()
        (args.new_output_directory/'source.json').write_bytes(raw)
        (args.new_output_directory/'observations.csv').write_text(converted,encoding='utf-8')
        (args.new_output_directory/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    except (ValueError,OSError,RecursionError):
        parser.exit(2,'saved connector report conversion failed; check source and conditions\n')


if __name__ == '__main__':
    main()
