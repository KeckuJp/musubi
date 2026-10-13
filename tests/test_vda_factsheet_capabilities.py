"""Saved VDA3.0 e9ba560b factsheet type/protocol/load/configuration declarations, not certification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.test_vda_factsheet_limits import sample
from tests.test_vda_factsheet_geometry import ROOT

PROFILE = ROOT / 'profiles/declared/vda-factsheet-limits/capabilities-profile.toml'


def full():
    data = sample()
    data['typeSpecification'].update(seriesName='シリーズ1', seriesDescription='', mobileRobotKinematics='DIFFERENTIAL',
        mobileRobotClass='CARRIER', localizationTypes=['NATURAL', 'UWB'],
        navigationTypes=['VIRTUAL_LINE_GUIDED', 'FREELY_NAVIGATING'], supportedZones=['BLOCKED', 'TELEPORT'])
    data['protocolFeatures'] = dict(
        optionalParameters=[dict(parameter='order.nodes.nodePosition.allowedDeviationTheta', support='REQUIRED',
                                 description='why')],
        mobileRobotActions=[dict(actionType='pick', actionScopes=['NODE', 'INSTANT'], pauseAllowed=False,
                                 cancelAllowed='true', blockingTypes=['HARD'],
                                 actionParameters=[dict(key='lhd', valueDataType='STRING', isOptional=True),
                                                   dict(key='height', valueDataType='NUMBER')]),
                            dict(actionType='vendorBeep', actionScopes=[], pauseAllowed=True, cancelAllowed=True)])
    data['loadSpecification'] = dict(loadPositions=['front'], loadSets=[dict(
        setName='DEFAULT', loadType='EPAL', loadPositions=[], boundingBoxReference=dict(x=0.1, y=-0.2, z=0, theta=1.5),
        loadDimensions=dict(length=1.2, width=0.8), maximumWeight=800, minimumLoadhandlingHeight=0,
        maximumLoadhandlingHeight=1.5, maximumLoadhandlingDepth=-0.1, minimumLoadhandlingTilt=-0.05,
        maximumSpeed=0.5, maximumAcceleration=0.3, maximumDeceleration=-0.4, pickTime=12, dropTime=9.5)])
    data['mobileRobotConfiguration'] = {'versions': [dict(key='softwareVersion', value='v1.2')],
        'network': dict(ntpServers=['ntp.invalid'], localIpAddress='192.0.2.10'),
        'batteryCharging ': dict(criticalLowChargingLevel=10, minimumChargingTime=600)}
    return data


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


class FactsheetCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        from scripts.convert_vda_factsheet_limits import convert_capabilities
        self.convert = convert_capabilities

    def test_two_documents_reach_common_with_units_and_statuses(self):
        first, second = full(), full()
        second['serialNumber'] = 'other'
        # Schema-required spelling and the spec's charging key, each as the only spelling present.
        second['typeSpecification']['mobileRobotKinematic'] = second['typeSpecification'].pop('mobileRobotKinematics')
        second['mobileRobotConfiguration']['batteryCharging'] = second['mobileRobotConfiguration'].pop('batteryCharging ')
        del second['typeSpecification']['supportedZones']
        second['loadSpecification'] = {}
        second['protocolFeatures']['future'] = 1
        # Unknown keys inside mapped nested objects are accounted too, not only at section level.
        second['protocolFeatures']['mobileRobotActions'][0]['vendorHint'] = 'x'
        second['mobileRobotConfiguration']['batteryCharging']['vendorCurve'] = [1]
        second['protocolFeatures']['mobileRobotActions'][0]['actionParameters'][1]['range'] = {}
        text = '\n'.join(json.dumps(x) for x in (first, second))
        output, report = self.convert(text)
        rows = rows_of(output)
        at = lambda i, path, item='', sub='': next(r for r in rows if (r['source_record_index'], r['declaration_path'],
                                                                         r['item_index'], r['sub_index']) == (str(i), path, item, sub))
        hexed = lambda value: 'hex:' + value.encode().hex()
        self.assertEqual(at(0, 'typeSpecification.mobileRobotKinematics')['enum_status'], 'KNOWN')
        self.assertEqual(at(1, 'typeSpecification.mobileRobotKinematics')['disposition'],
                         'REPORTED_UNDER_SCHEMA_REQUIRED_SPELLING_mobileRobotKinematic')
        self.assertEqual(at(0, 'typeSpecification.seriesDescription')['value_hex'], 'hex:')
        self.assertEqual(at(0, 'typeSpecification.localizationTypes', '1')['enum_status'], 'EXTENSIBLE_UNRECOGNIZED')
        self.assertEqual(at(0, 'typeSpecification.navigationTypes', '0')['value_hex'], hexed('VIRTUAL_LINE_GUIDED'))
        self.assertEqual(at(0, 'typeSpecification.supportedZones', '1')['enum_status'], 'OUTSIDE_PINNED_ENUM_RETAINED')
        self.assertEqual(at(1, 'typeSpecification.supportedZones')['disposition'], 'ABSENT')
        self.assertEqual(at(0, 'protocolFeatures.optionalParameters.support', '0')['value_hex'], hexed('REQUIRED'))
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.pauseAllowed', '0')['truth_value'], 'false')
        cancel = at(0, 'protocolFeatures.mobileRobotActions.cancelAllowed', '0')
        self.assertEqual((cancel['truth_value'], cancel['disposition']), ('true', 'REPORTED_STRING_FORM_PER_SCHEMA'))
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.actionScopes', '0', '1')['value_hex'], hexed('INSTANT'))
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.actionParameters.isOptional', '0', '1')['disposition'], 'ABSENT')
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.actionParameters.valueDataType', '0', '1')['value_hex'], hexed('NUMBER'))
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.actionScopes', '1')['disposition'], 'EMPTY_DECLARED_LIST')
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.actionParameters', '1')['disposition'], 'ABSENT_NO_PARAMETERS')
        # Default: the pin's conflicting units stay unresolved; the number is never a speed.
        speed_row = at(0, 'loadSpecification.loadSets.maximumSpeed', '0')
        self.assertEqual((speed_row['speed_m_s'], speed_row['unit_conflict_value'], speed_row['disposition']),
                         ('', '0.5', 'REPORTED_UNIT_CONFLICT_UNRESOLVED'))
        self.assertEqual(report['load_speed_unit_policy'], 'unresolved')
        self.assertEqual(at(0, 'loadSpecification.loadSets.maximumDeceleration', '0')['acceleration_m_s2'], '-0.4')
        self.assertEqual(at(0, 'loadSpecification.loadSets.maximumLoadhandlingDepth', '0')['length_m'], '-0.1')
        self.assertEqual(at(0, 'loadSpecification.loadSets.minimumLoadhandlingTilt', '0')['angle_rad'], '-0.05')
        self.assertEqual(at(0, 'loadSpecification.loadSets.dropTime', '0')['duration_s'], '9.5')
        self.assertEqual(at(0, 'loadSpecification.loadSets.maximumWeight', '0')['mass_kg'], '800')
        box = at(0, 'loadSpecification.loadSets.boundingBoxReference.theta', '0')
        self.assertEqual((box['coordinate_unit_not_stated'], box['angle_rad']), ('1.5', ''))
        self.assertEqual(at(0, 'loadSpecification.loadSets.loadDimensions.height', '0')['disposition'], 'ABSENT')
        self.assertEqual(at(0, 'loadSpecification.loadSets.loadPositions', '0')['disposition'], 'EMPTY_DECLARED_LIST_ALL_POSITIONS')
        self.assertEqual(at(1, 'loadSpecification.loadPositions')['disposition'], 'ABSENT_NO_LOAD_HANDLING_DEVICE_DECLARED')
        self.assertEqual(at(0, 'mobileRobotConfiguration.batteryCharging.criticalLowChargingLevel')['percent'], '10')
        self.assertEqual(at(0, 'mobileRobotConfiguration.batteryCharging.criticalLowChargingLevel')['disposition'],
                         'REPORTED_UNDER_SCHEMA_KEY_WITH_TRAILING_SPACE')
        self.assertEqual(at(1, 'mobileRobotConfiguration.batteryCharging.minimumChargingTime')['duration_s'], '600')
        self.assertEqual(at(0, 'mobileRobotConfiguration.network.localIpAddress')['value_hex'], hexed('192.0.2.10'))
        self.assertEqual(at(0, 'mobileRobotConfiguration.versions.value', '0')['value_hex'], hexed('v1.2'))
        self.assertEqual(report['unknown_capability_properties'], 4)
        self.assertEqual(report['capability_output_bytes'], len(output.encode('utf-8')))
        # Multibyte declared text travels as UTF-8 hex, so the sidecar itself is ASCII.
        self.assertEqual(at(0, 'typeSpecification.seriesName')['value_hex'], hexed('シリーズ1'))
        self.assertTrue(output.isascii())
        self.assertEqual(report['capability_rows'], len(rows))
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'capabilities.csv'
            path.write_text(output)
            result = json.loads(subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'],
                                               capture_output=True, check=True).stdout)
        observed = [o['fields'] for o in result['observations']]
        self.assertEqual(len(observed), len(rows))
        self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in result['observations']))
        speed = next(o for o in observed if o['declaration_path'] == 'loadSpecification.loadSets.maximumSpeed')
        self.assertEqual((speed.get('speed_m_s'), speed['unit_conflict_value']), (None, 0.5))
        # Explicit caller policy: the specification-text reading, recorded on the row and the report.
        chosen, chosen_report = self.convert(text, load_speed_policy='spec-text')
        chosen_row = next(r for r in rows_of(chosen) if r['declaration_path'] == 'loadSpecification.loadSets.maximumSpeed')
        self.assertEqual((chosen_row['speed_m_s'], chosen_row['unit_conflict_value']), ('0.5', ''))
        self.assertIn('CALLER_POLICY_SPEC_TEXT', chosen_row['value_basis'])
        self.assertEqual(chosen_report['load_speed_unit_policy'], 'spec-text')
        with self.assertRaises(ValueError):
            self.convert(text, load_speed_policy='m/s')
        for column in ('unit_conflict_value', 'speed_m_s', 'coordinate_unit_not_stated', 'percent', 'enum_status', 'truth_value'):
            self.assertTrue(result['profile_units'].get(column), column)

    def test_refusals(self):
        def variant(edit):
            data = full(); edit(data); return data
        cases = [
            lambda d: d['typeSpecification'].pop('seriesName'),
            lambda d: d['typeSpecification'].pop('mobileRobotKinematics'),
            lambda d: d['typeSpecification'].update(mobileRobotKinematic='DIFFERENTIAL'),
            lambda d: d['typeSpecification'].update(navigationTypes='FREELY_NAVIGATING'),
            lambda d: d['typeSpecification'].update(localizationTypes=[1]),
            lambda d: d.pop('protocolFeatures'),
            lambda d: d['protocolFeatures'].pop('mobileRobotActions'),
            lambda d: d['protocolFeatures']['optionalParameters'][0].pop('support'),
            lambda d: d['protocolFeatures']['mobileRobotActions'][0].update(pauseAllowed='yes'),
            lambda d: d['protocolFeatures']['mobileRobotActions'][0].update(cancelAllowed=1),
            lambda d: d['protocolFeatures']['mobileRobotActions'][1].pop('actionScopes'),
            lambda d: d['protocolFeatures']['mobileRobotActions'][0]['actionParameters'][0].pop('valueDataType'),
            lambda d: d['protocolFeatures']['mobileRobotActions'][0]['actionParameters'][0].update(isOptional='true'),
            lambda d: d['loadSpecification']['loadSets'][0].pop('loadType'),
            lambda d: d['loadSpecification']['loadSets'][0].update(maximumWeight=-1),
            lambda d: d['loadSpecification']['loadSets'][0].update(pickTime=float('inf')),
            lambda d: d['loadSpecification']['loadSets'][0].update(maximumSpeed='0.5'),
            lambda d: d['loadSpecification']['loadSets'][0]['boundingBoxReference'].pop('z'),
            lambda d: d['loadSpecification']['loadSets'][0]['loadDimensions'].pop('width'),
            lambda d: d['mobileRobotConfiguration'].update(batteryCharging={}),
            lambda d: d['mobileRobotConfiguration']['batteryCharging '].update(criticalLowChargingLevel=101),
            lambda d: d['mobileRobotConfiguration']['batteryCharging '].update(minimumChargingTime=1.5),
            lambda d: d['mobileRobotConfiguration']['versions'][0].pop('value'),
        ]
        for edit in cases:
            data = variant(edit)
            with self.subTest(data=data), self.assertRaises((ValueError, TypeError, KeyError)):
                self.convert(json.dumps(data))

    def test_default_outputs_unchanged_and_cli_opt_in(self):
        from scripts.convert_vda_factsheet_limits import convert
        text = json.dumps(full())
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.jsonl'
            source.write_text(text)
            plain, opted = Path(temp) / 'plain', Path(temp) / 'opted'
            script = str(ROOT / 'scripts/convert_vda_factsheet_limits.py')
            subprocess.run([sys.executable, script, str(source), str(plain)], check=True, capture_output=True)
            subprocess.run([sys.executable, script, str(source), str(opted), '--capabilities'], check=True, capture_output=True)
            base, limits, report = convert(text)
            self.assertEqual((plain / 'geometry.csv').read_text(), base)
            self.assertEqual((plain / 'limits.csv').read_text(), limits)
            self.assertEqual(json.loads((plain / 'report.json').read_text()), report)
            self.assertFalse((plain / 'capabilities.csv').exists())
            for name in ('geometry.csv', 'limits.csv', 'source.jsonl'):
                self.assertEqual((opted / name).read_bytes(), (plain / name).read_bytes())
            self.assertEqual((opted / 'capabilities.csv').read_text(), self.convert(text)[0])
            opted_report = json.loads((opted / 'report.json').read_text())
            self.assertEqual({k: v for k, v in opted_report.items() if k not in ('capability_rows', 'unknown_capability_properties',
                                                                     'capability_output_bytes', 'load_speed_unit_policy')}, report)
            self.assertEqual(opted_report['load_speed_unit_policy'], 'unresolved')
            chosen = Path(temp) / 'chosen'
            subprocess.run([sys.executable, script, str(source), str(chosen), '--capabilities',
                            '--load-speed-unit-policy', 'spec-text'], check=True, capture_output=True)
            self.assertEqual(json.loads((chosen / 'report.json').read_text())['load_speed_unit_policy'], 'spec-text')
            self.assertEqual(subprocess.run([sys.executable, script, str(source), str(Path(temp) / 'x'),
                                             '--load-speed-unit-policy', 'spec-text'], capture_output=True).returncode, 2)

    def test_output_byte_bound_is_the_common_reader_bound(self):
        import scripts.convert_vda_factsheet_limits as limits
        self.assertEqual(limits.READER_INPUT_BYTES, 16 * 1024 ** 2)
        text = json.dumps(full())
        output, report = self.convert(text)
        size = len(output.encode('utf-8'))
        original = limits.READER_INPUT_BYTES
        try:
            limits.READER_INPUT_BYTES = size
            self.assertEqual(self.convert(text)[0], output)
            # One byte under the encoded size: refused.
            limits.READER_INPUT_BYTES = size - 1
            with self.assertRaisesRegex(ValueError, 'common reader input bound'):
                self.convert(text)
        finally:
            limits.READER_INPUT_BYTES = original
