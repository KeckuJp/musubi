import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_tensorrt_layerinfo as layerinfo

PROFILE = Path(__file__).resolve().parents[1]/'profiles/declared/tensorrt-layerinfo/profile.toml'


def graph(name, shape, datatype):
    tensor = dict(Name='tensor-'+name, Dimensions=shape, **{'Format/Datatype': 'Unknown format'})
    layer = dict(Name=name, LayerType='signal', Inputs=[tensor], Outputs=[tensor],
                 Weights=dict(Count=3, Type=datatype), Metadata='original\x1econtent')
    return dict(Layers=[layer, dict(Name=name, LayerType='NoOp', Inputs=[], Outputs=[])],
                Bindings=[tensor['Name']], vendor_extension={'unknown': 17})


class LayerInfoTests(unittest.TestCase):
    def test_two_authored_inputs_actual_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER'); self.assertTrue(reader)
        # Second input fixed before implementation testing; no runtime revisions based on it.
        for name, shape, dtype, expected in [('a', [1, 2], 'Half', 6),
                                             ('b[profile 1]', [-1, 4, 0], 'FutureType', None)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)/'graph.json'
                raw = json.dumps(graph(name, shape, dtype)).encode()
                source.write_bytes(raw)
                output, report = layerinfo.convert(source, 33)
                target = Path(directory)/'observations.csv'; target.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(target), '--allow-equal-time'],
                    capture_output=True, check=True).stdout)
                rows = [o['fields'] for o in common['observations']]
                self.assertEqual([r['record_kind'] for r in rows],
                                 ['GRAPH', 'LAYER', 'INPUT', 'OUTPUT', 'WEIGHTS', 'LAYER', 'BINDING'])
                self.assertEqual(rows[4]['declared_storage_bytes'], expected)
                self.assertEqual(rows[4]['count'], 3)
                self.assertEqual(rows[1]['layer_name_hex'], rows[5]['layer_name_hex'])
                self.assertEqual(rows[2]['shape_status'], 'DYNAMIC_UNRESOLVED' if -1 in shape else 'DECLARED_DIMENSIONS')
                self.assertEqual(rows[2]['precision_status'], 'NOT_INFERRED')
                self.assertEqual(rows[2]['shape_hex'], 'hex:'+json.dumps(shape).encode().hex())
                self.assertEqual(report['source_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertEqual(report['csv_sha256'], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(report['constants'], 1)  # absent Bias not a zero constant
                self.assertEqual(report['unselected_layers'], 0)
                self.assertIn('graph/vendor_extension', report['unknown_keys'])
                self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
                self.assertEqual(json.loads(bytes.fromhex(rows[1]['raw_json_hex'][4:])),
                                 {k: v for k, v in graph(name, shape, dtype)['Layers'][0].items()
                                  if k not in ('Inputs', 'Outputs', 'Weights', 'Bias')})

    def test_important_refusals_and_empty_missing_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.json'
            original = graph('x', [2], 'Float')
            changes = [lambda g: g['Layers'][0]['Weights'].update(Count=True),
                       lambda g: g['Layers'][0]['Weights'].update(Count=-1),
                       lambda g: g['Layers'][0]['Weights'].update(Count=2**53-1),
                       lambda g: g['Layers'][0]['Inputs'][0].update(Dimensions=[-2]),
                       lambda g: g['Layers'][0]['Inputs'][0].update(Dimensions=[True]),
                       lambda g: g.update(Bindings='wrong'),
                       lambda g: g['Layers'][0].update(Inputs={}),
                       lambda g: g['Layers'][0].update(ParameterType=None)]
            for change in changes:
                value = copy.deepcopy(original); change(value); path.write_text(json.dumps(value))
                with self.assertRaises(ValueError): layerinfo.convert(path, 1)
            for raw in ['{"Layers":[],"Layers":[]}', '{"Layers":[],"unknown":NaN}',
                        '{"Layers":[],"unknown":1e999}']:
                path.write_text(raw)
                with self.assertRaises(ValueError): layerinfo.convert(path, 1)
            path.write_text('{"Layers":[]}')
            output, report = layerinfo.convert(path, 1)
            self.assertEqual(report['layers'], 0); self.assertEqual(report['output_rows'], 1)
            self.assertEqual(report['bindings_status'], 'NOT_REPORTED')
            zero = graph('zero', [], 'Int8'); zero['Layers'][0]['Weights']['Count'] = 0
            path.write_text(json.dumps(zero)); output, _ = layerinfo.convert(path, 1)
            weights = [r for r in csv.DictReader(io.StringIO(output)) if r['record_kind'] == 'WEIGHTS']
            self.assertEqual(weights[0]['declared_storage_bytes'], '0')
            with self.assertRaises(ValueError): layerinfo.convert(path, True)
