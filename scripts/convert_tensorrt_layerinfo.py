"""Passive fixed-consumer LayerInfo declarations, not inference or memory use.

Library API: csv_text, report = convert(Path('layers.json'), capture_time_us=1).
Save BOTH observations.csv and report.json to a new output directory, retain the
original JSON, then use the supplied profile with --allow-equal-time --allow-empty.
Partial file writes are not a completed conversion. No CLI or engine execution.
"""
import csv
import hashlib
import io
import json
import os
import stat

from scripts.convert_tensorrt_output import pairs

FIELDS = ['time_us', 'source_sha256', 'record_kind', 'layer_index', 'layer_name_hex',
          'layer_type_hex', 'layer_subtype_hex', 'child_index', 'name_hex', 'shape_hex',
          'rank', 'shape_status', 'format_hex', 'precision_status', 'count',
          'declared_type_hex', 'declared_storage_bytes', 'storage_status', 'raw_json_hex']
WIDTHS = {'Int8': 1, 'Half': 2, 'FP16': 2, 'Float': 4, 'FP32': 4, 'Int32': 4}
LIMIT = 2**53 - 1


def text(value):
    if not isinstance(value, str) or len(value) > 65536:
        raise ValueError('bounded string declaration required')
    return 'hex:' + value.encode('utf8').hex()


def integer(value, low=0, high=LIMIT):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('integer declaration outside bounds')
    return value


def convert(path, capture_time_us):
    integer(capture_time_us)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 16*1024**2:
            raise ValueError('regular input bound')
        raw = stream.read(16*1024**2+1)
        after = os.fstat(stream.fileno())
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) != before.st_size or signature(before) != signature(after):
        raise ValueError('source changed')
    def reject(value):
        raise ValueError('nonfinite JSON number')
    data = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    # JSON exponent overflow also counts as nonfinite, including unknown fields.
    json.dumps(data, allow_nan=False, separators=(',', ':'))
    if not isinstance(data, dict) or not isinstance(data.get('Layers'), list):
        raise ValueError('Layers object list required')
    layers = data['Layers']
    if len(layers) > 4096:
        raise ValueError('layer bound')
    bindings = data.get('Bindings', [])
    if not isinstance(bindings, list):
        raise ValueError('Bindings list required')
    digest = hashlib.sha256(raw).hexdigest()
    report = dict(source_sha256=digest, consumer_pin='17003e43da9858f574e3a4a1d795fcf218862fe3',
                  layers=len(layers), bindings=len(bindings), child_records=0,
                  input_tensors=0, output_tensors=0, constants=0, output_rows=0,
                  clock='caller_capture_unknown', unknown_keys=[],
                  nonclaim='declared graph, not runtime allocation, inference, or original network graph',
                  retention='retain original JSON; raw_json_hex preserves each object without lexical spelling',
                  bindings_status='PRESENT' if 'Bindings' in data else 'NOT_REPORTED')
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=FIELDS); writer.writeheader()
    def emit(kind, obj, base=None, **fields):
        row = dict(time_us=capture_time_us, source_sha256='sha256:'+digest, record_kind=kind,
                   raw_json_hex='hex:'+json.dumps(obj, allow_nan=False, separators=(',', ':')).encode().hex())
        row.update(base or {}); row.update(fields); writer.writerow(row)
        report['output_rows'] += 1
        if out.tell() > 128*1024**2:
            raise ValueError('output bound')
    def unknown(obj, known, location):
        report['unknown_keys'].extend(location + '/' + key for key in obj if key not in known)
    unknown(data, {'Layers', 'Bindings'}, 'graph')
    # One graph record preserves even an empty graph and unknown top-level values.
    emit('GRAPH', {k: v for k, v in data.items() if k not in ('Layers', 'Bindings')})
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or not {'Name', 'LayerType', 'Inputs', 'Outputs'} <= layer.keys():
            raise ValueError('detailed layer required')
        base = dict(layer_index=index, layer_name_hex=text(layer['Name']),
                    layer_type_hex=text(layer.get('ParameterType', layer['LayerType'])),
                    layer_subtype_hex=text(layer['LayerType']))
        if 'Metadata' in layer:
            text(layer['Metadata'])
        unknown(layer, {'Name', 'LayerType', 'ParameterType', 'Metadata', 'Inputs', 'Outputs', 'Weights', 'Bias'}, f'layer/{index}')
        emit('LAYER', {k: v for k, v in layer.items() if k not in ('Inputs', 'Outputs', 'Weights', 'Bias')}, base)
        for key, kind in [('Inputs', 'INPUT'), ('Outputs', 'OUTPUT')]:
            tensors = layer[key]
            if not isinstance(tensors, list):
                raise ValueError('tensor list required')
            report['child_records'] += len(tensors)
            report['input_tensors' if key == 'Inputs' else 'output_tensors'] += len(tensors)
            if report['child_records'] > 65536:
                raise ValueError('child record bound')
            for ordinal, tensor in enumerate(tensors):
                if not isinstance(tensor, dict) or not {'Name', 'Dimensions', 'Format/Datatype'} <= tensor.keys():
                    raise ValueError('activation declaration required')
                shape = tensor['Dimensions']
                if not isinstance(shape, list) or len(shape) > 32:
                    raise ValueError('shape rank bound')
                for dimension in shape:
                    integer(dimension, -1, 1000000)
                unknown(tensor, {'Name', 'Dimensions', 'Format/Datatype'}, f'layer/{index}/{key}/{ordinal}')
                emit(kind, tensor, base, child_index=ordinal, name_hex=text(tensor['Name']),
                     shape_hex=text(json.dumps(shape)), rank=len(shape),
                     shape_status='DYNAMIC_UNRESOLVED' if -1 in shape else 'DECLARED_DIMENSIONS',
                     format_hex=text(tensor['Format/Datatype']), precision_status='NOT_INFERRED')
        for key in ('Weights', 'Bias'):
            if key not in layer:
                continue
            constant = layer[key]
            if not isinstance(constant, dict) or not {'Count', 'Type'} <= constant.keys():
                raise ValueError('constant count/type required')
            count = integer(constant['Count']); datatype = constant['Type']; type_hex = text(datatype)
            width = WIDTHS.get(datatype)
            storage = integer(count*width) if width is not None else ''
            report['child_records'] += 1; report['constants'] += 1
            if report['child_records'] > 65536:
                raise ValueError('child record bound')
            unknown(constant, {'Count', 'Type'}, f'layer/{index}/{key}')
            emit(key.upper(), constant, base, count=count, declared_type_hex=type_hex,
                 declared_storage_bytes=storage,
                 storage_status='DECLARED_CONSTANT_BYTES_NOT_ALLOCATION' if width else 'UNKNOWN_TYPE')
    if report['child_records'] + len(bindings) > 65536:
        raise ValueError('child and binding bound')
    for ordinal, binding in enumerate(bindings):
        emit('BINDING', binding, child_index=ordinal, name_hex=text(binding))
    report['csv_sha256'] = hashlib.sha256(out.getvalue().encode()).hexdigest()
    report['unselected_layers'] = 0
    return out.getvalue(), report
