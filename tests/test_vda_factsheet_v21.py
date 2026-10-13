"""Saved VDA5050 2.1.0 (511d01d7) factsheets through the existing 3.0 pipeline; authored, not real records."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.test_vda_factsheet_geometry import ROOT, module
from tests.test_vda_factsheet_capabilities import full as full_30

V21 = module.FACTSHEET_21_FORMAT
CASE = ROOT / 'profiles/declared'


def fixture_a():
    return {
        'headerId': 3, 'timestamp': '2026-02-01T00:00:00.5Z', 'version': '2.1.0',
        'manufacturer': 'example', 'serialNumber': 'v21-a',
        'typeSpecification': dict(seriesName='A', agvKinematic='DIFF', agvClass='CARRIER', maxLoadMass=500,
                                  localizationTypes=['NATURAL'], navigationTypes=['AUTONOMOUS', 'VIRTUAL_LINE_GUIDED']),
        'physicalParameters': dict(speedMin=0.1, speedMax=1.5, accelerationMax=0.6, decelerationMax=0.8,
                                   heightMax=1.1, width=0.7, length=1.3),
        'protocolLimits': dict(maxStringLens=dict(msgLen=1000, enumLen=12, idNumericalOnly=True),
                               maxArrayLens={'order.nodes': 50}, timing=dict(minOrderInterval=1, minStateInterval=0.5)),
        'protocolFeatures': dict(optionalParameters=[dict(parameter='order.nodes.nodeMarker', support='SUPPORTED')],
            agvActions=[dict(actionType='pick', actionScopes=['NODE', 'EDGE'], blockingTypes=['HARD', 'SINGLE'],
                             resultDescription='done', actionParameters=[dict(key='h', valueDataType='FLOAT')])]),
        'agvGeometry': dict(
            wheelDefinitions=[dict(type='DRIVE', isActiveDriven=True, isActiveSteered=False,
                                   position=dict(x=0.4, y=-0.3), diameter=0.2, width=0.05)],
            envelopes2d=[dict(set='outline', polygonPoints=[dict(x=-0.6, y=-0.4), dict(x=0.7, y=-0.4), dict(x=0.7, y=0.4)]),
                         dict(set='outline', polygonPoints=[], description='second outline in the same set')],
            envelopes3d=[dict(set='body', format='DXF', url='https://invalid.example/no-fetch', description='text')]),
        'loadSpecification': dict(loadPositions=['top'], loadSets=[dict(setName='DEFAULT', loadType='EPAL',
            maxWeight=400, agvSpeedLimit=0.9, agvAccelerationLimit=0.2, agvDecelerationLimit=-0.3,
            minLoadhandlingTilt=-0.1, pickTime=5)]),
        'vehicleConfig': dict(versions=[dict(key='fw', value='2.1-a')], network=dict(netmask='255.255.255.0')),
    }


def fixture_b():
    data = fixture_a()
    data.update(serialNumber='v21-b', timestamp='2026-02-01T00:00:01Z')
    data['typeSpecification'].update(agvKinematic='SKID', agvClass='TUGGER', seriesDescription='b')
    data['typeSpecification']['vendorTag'] = 'kept'
    data['physicalParameters'].update(heightMin=0.2, angularSpeedMax=1.2)
    data['agvGeometry']['envelopes3d'] = [dict(set='body', format='STEP', description=7)]
    del data['vehicleConfig']
    return data


def lines(*records):
    return '\n'.join(json.dumps(record) for record in records)


def rows_of(text):
    limit = csv.field_size_limit(4 * 1024 ** 2)
    try:
        return list(csv.DictReader(io.StringIO(text)))
    finally:
        csv.field_size_limit(limit)


def common(profile, text):
    reader = os.environ.get('MUSUBI_TELEMETRY_READER')
    if not reader:
        raise unittest.SkipTest('actual common reader required')
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'out.csv'
        path.write_text(text)
        return json.loads(subprocess.run([reader, str(profile), str(path), '--allow-equal-time'],
                                         capture_output=True, check=True).stdout)


class FactsheetV21Tests(unittest.TestCase):
    def test_two_v21_documents_reach_common_under_their_own_version(self):
        from scripts.convert_vda_factsheet_limits import convert, convert_capabilities
        a, b = fixture_a(), fixture_b()
        text = lines(a, b)
        # 1 header/type/physical + 4 geometry: the shared converter, the existing selector route.
        base = module.convert(text, V21, vda_geometry=True)
        rows = rows_of(base)
        summaries = [r for r in rows if r['geometry_row_kind'] == 'SUMMARY']
        self.assertEqual(len(summaries), 2)
        for summary, record in zip(summaries, (a, b)):
            original = bytes.fromhex(summary['source_record_hex'][4:]).decode()
            self.assertEqual(json.loads(original), record)  # the original 2.1 line, never relabelled
            self.assertEqual(json.loads(original)['version'], '2.1.0')
            self.assertEqual(summary['geometry_source_sha256'], 'sha256:' + hashlib.sha256(original.encode()).hexdigest())
            self.assertIn('VDA2_1_0', summary['factsheet_basis'])
        self.assertEqual(summaries[0]['declared_minimum_height_m'], '')            # heightMin optional in 2.1
        self.assertEqual(summaries[1]['declared_minimum_height_m'], '0.2')
        self.assertEqual(summaries[0]['declared_maximum_angular_speed_rad_s'], '')
        self.assertEqual(summaries[1]['declared_maximum_angular_speed_rad_s'], '1.2')  # spec-table-only key
        self.assertEqual(summaries[0]['declared_maximum_speed_m_s'], '1.5')
        self.assertEqual(float(summaries[0]['declared_maximum_load_mass_kg']), 500)
        envelopes = [r for r in rows if r['geometry_row_kind'] == 'ENVELOPE2D' and r['geometry_source_record_index'] == '0']
        self.assertEqual([r['geometry_id_hex'] for r in envelopes], ['hex:' + b'outline'.hex()] * 2)  # duplicate set names kept
        self.assertEqual(len([r for r in rows if r['geometry_row_kind'] == 'VERTEX' and r['geometry_source_record_index'] == '0']), 3)
        self.assertEqual(summaries[1]['geometry_unknown_key_count'], '0')
        result = common(CASE / 'electrical-schema-reuse/json-profile.toml', base)
        self.assertEqual(len(result['observations']), len(rows))
        self.assertEqual(result['observations'][0]['fields']['declared_maximum_speed_m_s'], 1.5)
        self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in result['observations']))

        # 2 protocol limits and 3D bookkeeping.
        _, limits, report = convert(text, V21)
        self.assertEqual((report['factsheet_version'], report['uninterpreted_3d']), ('2.1.0', 2))
        by = {(r['source_record_index'], r['vda_3_0_meaning_path'], r['item_index']): r for r in rows_of(limits)}
        self.assertEqual(by['0', 'NONE_VDA2_1_0_ONLY', '']['reported_integer'], '12')
        self.assertEqual(by['0', 'NONE_VDA2_1_0_ONLY', '']['declaration_path'], 'maxStringLens.enumLen')
        message = by['0', 'maximumStringLengths.maximumMessageLength', '']
        self.assertEqual((message['declaration_path'], message['reported_integer']), ('maxStringLens.msgLen', '1000'))
        zones = by['0', 'maximumArrayLengths.zoneSet.zones', '']
        self.assertEqual((zones['declaration_path'], zones['disposition']), ('', 'NOT_IN_VERSION'))
        self.assertEqual(by['0', 'maximumArrayLengths.state.loads', '']['disposition'], 'NO_EXPLICIT_LIMIT_ABSENT')
        self.assertEqual(by['0', 'timing.minimumStateInterval', '']['declaration_path'], 'timing.minStateInterval')
        self.assertEqual(by['0', 'mobileRobotGeometry.envelopes3d', '0']['declaration_path'], 'agvGeometry.envelopes3d')
        conflict = by['1', 'mobileRobotGeometry.envelopes3d', '0']
        self.assertEqual(conflict['description_hex'], '')
        self.assertIn('DESCRIPTION_INTEGER_PER_2_1_SCHEMA', conflict['disposition'])
        self.assertTrue(by['0', 'mobileRobotGeometry.envelopes3d', '0']['description_hex'])
        self.assertEqual(len(common(CASE / 'vda-factsheet-limits/profile.toml', limits)['observations']), report['sidecar_rows'])

        # 3 actions, 5 loads, 6 configuration (and type enums) in the capability sidecar.
        output, details = convert_capabilities(text, source_format=V21)
        cap = rows_of(output)
        at = lambda i, meaning, item='', sub='': next(r for r in cap if (r['source_record_index'], r['vda_3_0_meaning_path'],
                                                                         r['item_index'], r['sub_index']) == (str(i), meaning, item, sub))
        kin = at(0, 'typeSpecification.mobileRobotKinematics')
        self.assertEqual((kin['declaration_path'], kin['value_hex'], kin['enum_status']),
                         ('typeSpecification.agvKinematic', 'hex:' + b'DIFF'.hex(), 'KNOWN'))
        self.assertEqual(at(1, 'typeSpecification.mobileRobotKinematics')['enum_status'], 'EXTENSIBLE_UNRECOGNIZED')
        self.assertEqual(at(0, 'typeSpecification.navigationTypes', '0')['enum_status'], 'KNOWN')  # AUTONOMOUS at 2.1
        self.assertEqual(at(0, 'typeSpecification.supportedZones')['disposition'], 'NOT_IN_VERSION')
        pause = at(0, 'protocolFeatures.mobileRobotActions.pauseAllowed', '0')
        self.assertEqual((pause['disposition'], pause['truth_value'], pause['declaration_path']), ('NOT_IN_VERSION', '', ''))
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.blockingTypes', '0', '1')['enum_status'],
                         'OUTSIDE_PINNED_ENUM_RETAINED')  # SINGLE is 3.0, not 2.1
        float_type = at(0, 'protocolFeatures.mobileRobotActions.actionParameters.valueDataType', '0', '0')
        self.assertEqual((float_type['enum_status'], float_type['declaration_path']),
                         ('KNOWN', 'protocolFeatures.agvActions.actionParameters.valueDataType'))
        self.assertEqual(at(0, 'protocolFeatures.mobileRobotActions.actionResult', '0')['declaration_path'],
                         'protocolFeatures.agvActions.resultDescription')
        speed = at(0, 'loadSpecification.loadSets.maximumSpeed', '0')
        self.assertEqual((speed['declaration_path'], speed['speed_m_s'], speed['unit_conflict_value']),
                         ('loadSpecification.loadSets.agvSpeedLimit', '', '0.9'))
        self.assertEqual(at(0, 'loadSpecification.loadSets.maximumDeceleration', '0')['acceleration_m_s2'], '-0.3')
        self.assertEqual(at(0, 'loadSpecification.loadSets.maximumWeight', '0')['mass_kg'], '400')
        battery = at(0, 'mobileRobotConfiguration.batteryCharging.criticalLowChargingLevel')
        self.assertEqual((battery['disposition'], battery['declaration_path']), ('NOT_IN_VERSION', ''))
        self.assertEqual(at(0, 'mobileRobotConfiguration.network.netmask')['declaration_path'], 'vehicleConfig.network.netmask')
        self.assertEqual(at(1, 'mobileRobotConfiguration')['declaration_path'], 'vehicleConfig')
        self.assertEqual(details['unknown_capability_properties'], 1)  # b's typeSpecification.vendorTag
        self.assertTrue(all(r['factsheet_version'] == '2.1.0' for r in cap))
        chosen = rows_of(convert_capabilities(text, 'spec-text', source_format=V21)[0])
        self.assertEqual(next(r for r in chosen if r['vda_3_0_meaning_path'] == 'loadSpecification.loadSets.maximumSpeed')['speed_m_s'], '0.9')
        result = common(CASE / 'vda-factsheet-limits/capabilities-profile.toml', output)
        self.assertEqual(len(result['observations']), len(cap))
        self.assertEqual(result['observations'][0]['fields']['factsheet_version'], '2.1.0')

    def test_selector_version_timestamp_and_ambiguity_refusals(self):
        from scripts.convert_vda_factsheet_limits import convert_capabilities
        a = fixture_a()
        three = full_30()
        cases = [(lines(a), module.FACTSHEET_FORMAT), (json.dumps(three), V21)]
        with self.assertRaisesRegex(ValueError, 'wrong factsheet version'):
            module.convert(json.dumps(three), V21)
        for edit in (lambda d: d.pop('timestamp'), lambda d: d.update(timestamp='2026-02-01 00:00:00'),
                     lambda d: d.update(version='2.0.0'),
                     lambda d: d['physicalParameters'].update(maximumSpeed=1.5),        # 3.0 name in 2.1
                     lambda d: d['typeSpecification'].update(supportedZones=['BLOCKED']),
                     lambda d: d['protocolFeatures']['agvActions'][0].update(pauseAllowed=True),
                     lambda d: d.update(mobileRobotGeometry={}),
                     lambda d: d['protocolLimits']['maxArrayLens'].update({'zoneSet.zones': 3}),
                     lambda d: d['physicalParameters'].pop('heightMax')):
            data = copy.deepcopy(a)
            edit(data)
            cases.append((json.dumps(data), V21))
        for text, selector in cases:
            with self.subTest(text=text[:60], selector=selector), self.assertRaises(ValueError):
                convert_capabilities(text, source_format=selector)
        with self.assertRaises(ValueError):
            module.convert(lines(a), module.FACTSHEET_FORMAT, vda_geometry=True)

    def test_cli_selector_and_unchanged_30_default(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.jsonl'
            text = lines(fixture_a(), fixture_b())
            source.write_text(text)
            script = str(ROOT / 'scripts/convert_vda_factsheet_limits.py')
            out = Path(temp) / 'out'
            subprocess.run([sys.executable, script, str(source), str(out), '--source-format', V21, '--capabilities'],
                           check=True, capture_output=True)
            self.assertEqual((out / 'source.jsonl').read_text(), text)
            self.assertEqual(json.loads((out / 'report.json').read_text())['factsheet_version'], '2.1.0')
            self.assertEqual(subprocess.run([sys.executable, script, str(source), str(Path(temp) / 'x')],
                                            capture_output=True).returncode, 2)
