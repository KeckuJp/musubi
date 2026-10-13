import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.test_vda_factsheet_geometry import document, module, ROOT


def sample():
    data = document()
    data['mobileRobotGeometry']['envelopes3d'] = [dict(envelope3dId='body', format='CUSTOM', data={'xyz':[1,2,3]})]
    data['protocolLimits'] = dict(maximumStringLengths=dict(maximumIdLength=0, idNumericalOnly=False),
        maximumArrayLengths={'order.nodes':42}, timing=dict(minimumOrderInterval=.2, minimumStateInterval=0))
    return data


class FactsheetLimitsTests(unittest.TestCase):
    def test_two_inputs_common(self):
        from scripts.convert_vda_factsheet_limits import convert
        first, second = sample(), sample()
        second['serialNumber'] = 'other'
        second['protocolLimits']['maximumArrayLengths']['order.nodes'] = 2**63-1
        second['protocolLimits']['maximumStringLengths']['idNumericalOnly'] = True
        second['protocolLimits']['timing']['defaultStateInterval'] = .5
        second['protocolLimits']['future'] = {'keep':True}
        text = '\n'.join(json.dumps(x) for x in (first, second))
        base, extra, report = convert(text)
        self.assertEqual(base, module.convert(text, module.FACTSHEET_FORMAT, vda_geometry=True))
        rows = list(csv.DictReader(io.StringIO(extra)))
        bykey = {(r['source_record_index'], r['declaration_path']):r for r in rows}
        self.assertEqual(bykey['0','maximumStringLengths.maximumIdLength']['disposition'], 'NO_EXPLICIT_LIMIT_REPORTED_ZERO')
        self.assertEqual(bykey['0','maximumStringLengths.maximumLoadIdLength']['disposition'], 'NO_EXPLICIT_LIMIT_ABSENT')
        self.assertEqual(bykey['0','maximumStringLengths.idNumericalOnly']['truth_value'], 'false')
        self.assertEqual(bykey['0','timing.minimumStateInterval']['seconds'], '0')
        self.assertEqual(bykey['0','timing.defaultStateInterval']['disposition'], 'DEFAULT_DOCUMENT_NOT_RESOLVED')
        self.assertEqual(report['uninterpreted_3d'], 2)
        self.assertEqual(report['unknown_protocol_properties'], 1)
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'limits.csv'
            path.write_text(extra)
            result = json.loads(subprocess.run([reader, str(ROOT/'profiles/declared/vda-factsheet-limits/profile.toml'), str(path), '--allow-equal-time'], capture_output=True, check=True).stdout)
        observations = [r['fields'] for r in result['observations']]
        self.assertEqual(len(observations), report['sidecar_rows'])
        self.assertEqual(next(r for r in observations if r['source_record_index']==1 and r['declaration_path']=='maximumArrayLengths.order.nodes')['reported_integer'], 2**63-1)
        self.assertTrue(all(r['clock_basis']=='Unknown' for r in result['observations']))

    def test_refusals_and_absent(self):
        from scripts.convert_vda_factsheet_limits import convert
        cases = []
        for key, value in [('maximumIdLength',True), ('maximumIdLength',-1), ('maximumIdLength',2**63), ('idNumericalOnly',0)]:
            data=sample(); data['protocolLimits']['maximumStringLengths'][key]=value; cases.append(data)
        for value in [None, -1, float('inf'), True]:
            data=sample(); data['protocolLimits']['timing']['minimumStateInterval']=value; cases.append(data)
        data=sample(); del data['protocolLimits']['timing']['minimumOrderInterval']; cases.append(data)
        data=sample(); del data['mobileRobotGeometry']['envelopes3d'][0]['format']; cases.append(data)
        data=sample(); data['protocolLimits']=None; cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises((ValueError,TypeError)):
                convert(json.dumps(data))
        data=sample(); del data['protocolLimits']; data['mobileRobotGeometry']['envelopes3d']=[]
        _, extra, report=convert(json.dumps(data))
        self.assertEqual(report['uninterpreted_3d'],0)
        self.assertIn('PROTOCOL_LIMITS_ABSENT',extra)

    def test_cli_source_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.jsonl'
            data=json.dumps(sample()).encode()
            source.write_bytes(data)
            target=Path(temp)/'out'
            command=[sys.executable,str(ROOT/'scripts/convert_vda_factsheet_limits.py'),str(source),str(target)]
            subprocess.run(command,check=True,capture_output=True)
            self.assertEqual((target/'source.jsonl').read_bytes(),data)
            self.assertTrue((target/'geometry.csv').exists())
            self.assertEqual(subprocess.run(command,capture_output=True).returncode,2)

    def test_limits_output_byte_bound_is_the_common_reader_bound(self):
        import scripts.convert_vda_factsheet_limits as limits
        self.assertEqual(limits.READER_INPUT_BYTES, 16 * 1024 ** 2)
        text = json.dumps(sample())
        base, extra, report = limits.convert(text)
        size = len(extra.encode('utf-8'))
        original = limits.READER_INPUT_BYTES
        try:
            limits.READER_INPUT_BYTES = size
            self.assertEqual(limits.convert(text), (base, extra, report))
            limits.READER_INPUT_BYTES = size - 1
            with self.assertRaisesRegex(ValueError, 'common reader input bound'):
                limits.convert(text)
        finally:
            limits.READER_INPUT_BYTES = original
