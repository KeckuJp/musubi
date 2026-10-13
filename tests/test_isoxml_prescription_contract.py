"""Coordinator-authored finite reference contract; no new semantic decoder oracle."""
import csv
import io
import unittest

from scripts.convert_isoxml_rate import GRID_BYTE_ORDER, prescriptions


def task(devices, refs):
    zones = ''.join(
        f'<TZN A="{i}"><PDV A="0001" B="250000"{ref}/></TZN>'
        for i, ref in enumerate(refs, 1)
    )
    return (
        '<ISO11783_TaskData VersionMajor="4" VersionMinor="2">'
        + devices + '<TSK A="TSK-1"><GRD A="52" B="5" C="0.001" D="0.001" '
        f'E="{len(refs)}" F="1" G="GRD00001" I="1"/>'
        + zones + '</TSK></ISO11783_TaskData>'
    ).encode()


def device(device_id, element_id):
    return (f'<DVC A="{device_id}"><DET A="{element_id}" B="1" C="4" '
            'D="declared section" E="7" F="0"/></DVC>')


def convert(description, count, enabled=True):
    output, _ = prescriptions(
        description, bytes(range(1, count + 1)), capture_time_us=100,
        byte_order=GRID_BYTE_ORDER, grid_name='GRD00001', device_reference=enabled,
    )
    return list(csv.DictReader(io.StringIO(output)))


class PrescriptionReferenceContract(unittest.TestCase):
    def test_local_object_id_reuse_does_not_join_two_declared_devices(self):
        description = task(device('DVC-A', 'DET-A') + device('DVC-B', 'DET-B'),
                           [' D="DET-B"', ' D="DET-A"'])
        rows = convert(description, 2)
        self.assertEqual([r['declared_device_id_hex'] for r in rows],
                         ['hex:' + b'DVC-B'.hex(), 'hex:' + b'DVC-A'.hex()])
        self.assertEqual([r['declared_element_object_id'] for r in rows], ['1', '1'])
        self.assertEqual([r['prescription_device_reference_disposition'] for r in rows],
                         ['RESOLVED_DECLARATION', 'RESOLVED_DECLARATION'])
        self.assertEqual([r['grid_cell_index'] for r in rows], ['0', '1'])
        self.assertEqual([r['setpoint_volume_rate_l_ha'] for r in rows], ['25.0', '25.0'])

    def test_absent_and_dangling_are_distinct_and_not_synthetic_identities(self):
        rows = convert(task('', ['', ' D="DET-MISSING"']), 2)
        self.assertEqual([r['prescription_device_reference_disposition'] for r in rows],
                         ['REFERENCE_NOT_PROVIDED', 'REFERENCED_ELEMENT_NOT_FOUND'])
        self.assertEqual([r['declared_device_id_hex'] for r in rows], ['', ''])
        self.assertEqual(rows[1]['declared_device_element_ref_hex'],
                         'hex:' + b'DET-MISSING'.hex())

    def test_ambiguous_element_reference_is_rejected_only_in_opt_in_resolution(self):
        description = task(device('DVC-A', 'DET-X') + device('DVC-B', 'DET-X'),
                           [' D="DET-X"'])
        with self.assertRaises(ValueError):
            convert(description, 1)
        legacy = convert(description, 1, enabled=False)
        self.assertEqual(len(legacy), 1)
        self.assertNotIn('prescription_device_reference_disposition', legacy[0])
