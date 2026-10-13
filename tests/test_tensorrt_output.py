import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_tensorrt_output as tensor

PROFILE=Path(__file__).resolve().parents[1]/'profiles/declared/tensorrt-output/profile.toml'


class TensorOutputTests(unittest.TestCase):
    def test_two_shapes_and_empty_common(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER'); self.assertTrue(reader)
        for shape,values in (('(2, 2)',[1,2,3,4]),('()',[1.25])):
            with self.subTest(shape=shape),tempfile.TemporaryDirectory() as d:
                path=Path(d)/'input.json'; path.write_text(json.dumps([
                    dict(name='result',dimensions=shape,values=values,unknown='retained in original'),
                    dict(name='empty',dimensions='(0, 3)',values=[]),
                    dict(name='large',dimensions='(1)',values=[2**63-1])]))
                output,report=tensor.convert(path,100)
                csv=Path(d)/'data.csv'; csv.write_text(output)
                common=json.loads(subprocess.run([reader,str(PROFILE),str(csv),'--allow-equal-time'],
                    capture_output=True,check=True).stdout)
                fields=[o['fields'] for o in common['observations']]
                self.assertEqual([x['value'] for x in fields[:len(values)]],values)
                self.assertEqual(fields[-2]['value_status'],'EMPTY_TENSOR_NO_VALUE')
                self.assertIsNone(fields[-1]['value'])
                self.assertEqual(fields[-1]['exact_number_text'],'hex:'+str(2**63-1).encode().hex())
                self.assertEqual(report['unknown_fields'],1)
                self.assertEqual(report['values'],len(values)+1)
                self.assertTrue(all(x['source_dtype']=='UNKNOWN_NOT_EXPORTED' for x in fields))

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'bad.json'
            for value in (dict(name='a',dimensions='(2)',values=[1]),
                          dict(name='a',dimensions='(-1)',values=[]),
                          dict(name='a',dimensions='(1)',values=[True]),
                          dict(name='a',dimensions='(1)',values=[None])):
                path.write_text(json.dumps([value]))
                with self.subTest(value=value),self.assertRaises(ValueError): tensor.convert(path,1)
            for raw in ('[{"name":"a","dimensions":"(1)","values":[1e-999]}]',
                        '[{"name":"a","dimensions":"(1)","values":[Final shape is (1)]}]'):
                path.write_text(raw)
                with self.assertRaises(ValueError): tensor.convert(path,1)
