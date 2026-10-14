"""Authored fixed-decoder CompactPDO declarations, never CAN traffic."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_dronecan_capture import convert_canopen_dictionary, convert_canopen_pdo

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "[DeviceInfo]\nCompactPDO=0x3f\nNrOfRxPDO=1\nNrOfTxPDO=1\n"
MAP = """[1A00]
ParameterName=Selected map
ObjectType=9
SubNumber=2
[1A00sub0]
ParameterName=Count
DataType=5
AccessType=ro
DefaultValue=1
[1A00sub1]
ParameterName=Slot
DataType=7
AccessType=ro
DefaultValue=0x20000010
[2000]
ParameterName=Reported voltage
DataType=6
AccessType=ro
PDOMapping=1
Unit=mV
Future=retained
"""


class CompactPdoTests(unittest.TestCase):
    def test_device_info_case_does_not_drop_pdo_count(self):
        text = PREFIX + MAP
        alternate = text.replace('DeviceInfo', 'deviceinfo').replace('CompactPDO', 'compactpdo').replace('NrOfRxPDO', 'nrofrxpdo').replace('NrOfTxPDO', 'NROFTXPDO')
        normal, _ = convert_canopen_dictionary(text, 7, compact_pdo_node_id=1)
        output, report = convert_canopen_dictionary(alternate, 7, compact_pdo_node_id=1)
        self.assertEqual(normal, output)
        self.assertIn('1800', report['compact_generated_objects'])
        with self.assertRaises(ValueError):
            convert_canopen_dictionary(alternate + '[devicecomissioning]\nnodeid=2\n', 7, compact_pdo_node_id=1)

    def test_masks_counts_and_explicit_override(self):
        text = PREFIX + MAP
        output, report = convert_canopen_dictionary(text, 7, compact_pdo_node_id=1)
        rows = {(int(r['object_index']), r['object_subindex']): r for r in csv.DictReader(io.StringIO(output))}
        values = lambda key: json.loads(bytes.fromhex(rows[key]['source_section_hex'][4:]))
        self.assertEqual(values((0x1800, '1'))['DefaultValue'], '385')
        self.assertEqual(values((0x1800, '1'))['MusubiOriginalDefaultValue'], '$NODEID+0x180')
        self.assertEqual(values((0x1800, '0'))['DefaultValue'], '6')
        self.assertEqual(values((0x1600, '0'))['DefaultValue'], '64')
        self.assertEqual(values((0x1a00, '0'))['DefaultValue'], '1')
        self.assertEqual(bytes.fromhex(report['source_document_hex'][4:]).decode(), text)
        explicit = text + '[1800]\nParameterName=Override\nObjectType=9\nSubNumber=1\n[1800sub1]\nParameterName=COB\nDataType=7\nAccessType=rw\nDefaultValue=0x181\nParameterValue=0x182\n'
        out, result = convert_canopen_dictionary(explicit, 8, compact_pdo_node_id=2)
        self.assertNotIn('1800', result['compact_generated_objects'])
        self.assertIn('Override'.encode().hex(), out)
        for mask in (1, 2, 4, 8, 16, 32):
            out, _ = convert_canopen_dictionary(text.replace('0x3f', str(mask)), 0, compact_pdo_node_id=1)
            selected = [r for r in csv.DictReader(io.StringIO(out)) if r['object_index'] == str(0x1800)]
            self.assertEqual(len(selected), 3)

    def test_two_dictionaries_reach_existing_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual common reader required')
        for node, unit, raw, expected, field in ((1, 'mV', 12000, 12, 'voltage_v'),
                                                 (7, 'mA', 2500, 2.5, 'current_a')):
            text = PREFIX + MAP.replace('Unit=mV', 'Unit=' + unit)
            frame = f'time_us,standard_id,data_hex\n5,{0x180+node:X},{raw.to_bytes(2,"little").hex()}\n'
            out, report = convert_canopen_pdo(frame, text, 0x180+node, 0x1a00, compact_pdo_node_id=node)
            self.assertEqual(report['decoded_frames'], 1)
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'observations.csv'; path.write_text(out)
                common = json.loads(subprocess.run([reader, str(ROOT / 'profiles/declared/dronecan-recorded/pdo-profile.toml'), str(path)], check=True, capture_output=True).stdout)
            obs = common['observations'][0]
            self.assertEqual(obs['fields'][field], expected)
            self.assertEqual(obs['fields']['compact_pdo_node_id'], node)
            self.assertEqual(obs['clock_basis'], 'Unknown')
            with self.assertRaises(ValueError):
                convert_canopen_pdo(frame, text, 0x180+node+1, 0x1a00, compact_pdo_node_id=node)

    def test_refusals_and_default_unchanged(self):
        text = PREFIX + MAP
        with self.assertRaisesRegex(ValueError, 'not expanded'):
            convert_canopen_dictionary(text, 0)
        plain = MAP
        self.assertEqual(convert_canopen_dictionary(plain, 0), convert_canopen_dictionary(plain, 0, compact_pdo_node_id=None))
        for node in (True, 0, 128, '1'):
            with self.assertRaises(ValueError):
                convert_canopen_dictionary(text, 0, compact_pdo_node_id=node)
        for bad in (text.replace('0x3f', '64'), text.replace('NrOfTxPDO=1', 'NrOfTxPDO=513'),
                    text + '[1800sub1]\nParameterName=Orphan\n',
                    text.replace('DefaultValue=1', 'DefaultValue=$NODEID*2'),
                    text + '[DeviceComissioning]\nNodeID=2\n',
                    text.replace('NrOfTxPDO=1', 'NrOfTxPDO=0') + '[1800]\nParameterName=Explicit\nObjectType=9\n',
                    text + '[deviceinfo]\nCompactPDO=1\n'):
            with self.assertRaises(ValueError):
                convert_canopen_dictionary(bad, 0, compact_pdo_node_id=1)


if __name__ == '__main__':
    unittest.main()
