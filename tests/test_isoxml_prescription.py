"""Authored ISOXML prescription layout/meaning checks; an intended rate is never an application."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from decimal import Decimal
from unittest import mock
from scripts import convert_isoxml_rate
from scripts.convert_isoxml_rate import GRID_BYTE_ORDER, prescriptions

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/convert_isoxml_rate.py'
PROFILE = ROOT / 'profiles/declared/isoxml-prescription-reuse/profile.toml'
CAPTURE = 1757980800000000


def grid(kind, columns, rows, *, name='GRD00001', extra='', north='52.10000000', east='5.25000000'):
    return ('<GRD A="{}" B="{}" C="0.00010000" D="0.00020000" E="{}" F="{}" G="{}" I="{}"{}/>'
            .format(north, east, columns, rows, name, kind, extra))


def zone(code, variables, *, designator=None, extra='', body=''):
    attributes = ' A="{}"'.format(code) + ('' if designator is None else ' B="{}"'.format(designator))
    body += ''.join('<PDV A="{}" B="{}"{}/>'.format(ddi, value, more) for ddi, value, more in variables)
    return '<TZN{}{}>{}</TZN>'.format(attributes, extra, body)


def document(*parts, task='A="TSK-1" B="North block"'):
    return ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2"><TSK {}>{}</TSK>'
            '</ISO11783_TaskData>'.format(task, ''.join(parts))).encode()


def cells(*values):
    return b''.join(struct.pack('<i', value) for value in values)


class IsoxmlPrescriptionTests(unittest.TestCase):
    def convert(self, description, binary, **kwargs):
        kwargs.setdefault('capture_time_us', CAPTURE)
        kwargs.setdefault('byte_order', GRID_BYTE_ORDER)
        kwargs.setdefault('grid_name', 'GRD00001')
        return prescriptions(description, binary, **kwargs)

    def refuses(self, description, binary, **kwargs):
        with self.assertRaises((ValueError, struct.error)):
            self.convert(description, binary, **kwargs)

    def test_zone_coded_cells_reach_common_observations(self):
        description = document(grid(1, 2, 2),
                               zone(3, [('0001', 250000, ' C="PDT-1" D="DET-1"')], designator='Full'),
                               zone(7, [('0006', 12000, ''), ('000B', 25000, '')], designator='Edge'))
        binary = bytes([3, 7, 7, 3])
        output, report = self.convert(description, binary)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 6)  # every cell, not the first cell and not metadata only
        self.assertEqual(report['declared_cells'], report['cells_read'])
        self.assertEqual([r['grid_cell_index'] for r in rows], ['0', '1', '1', '2', '2', '3'])
        self.assertEqual([r['treatment_zone_code'] for r in rows], ['3', '7', '7', '7', '7', '3'])
        self.assertEqual([r['source_ddi'] for r in rows], ['1', '6', '11', '6', '11', '1'])
        self.assertEqual(rows[0]['setpoint_volume_rate_l_ha'], str(250000 * .0001))
        self.assertEqual(rows[1]['setpoint_mass_rate_kg_m2'], str(12000 * .000001))
        self.assertEqual(rows[2]['setpoint_count_per_m2'], str(25000 * .001))
        self.assertEqual(rows[0]['source_value_origin'], 'TREATMENT_ZONE_DECLARATION')
        self.assertEqual(rows[0]['source_cell_hex'], 'hex:03')  # the exact selecting byte
        self.assertEqual(rows[0]['declared_product_ref_hex'], 'hex:' + b'PDT-1'.hex())
        self.assertEqual(rows[0]['treatment_zone_designator_hex'], 'hex:' + b'Full'.hex())
        self.assertEqual(rows[0]['task_id_hex'], 'hex:' + b'TSK-1'.hex())
        self.assertEqual(report['prescription_values'], 6)
        self.assertEqual(report['actual_application_records'], 'NOT_OBSERVED')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            result = self.command(base, description, binary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((base / 'out/observations.csv').read_text(), output)
            self.assertEqual(json.loads((base / 'out/report.json').read_text())['grid_sha256'],
                             report['grid_sha256'])
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'], str(PROFILE),
                    str(base / 'out/observations.csv'), '--allow-equal-time'],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 6)
                self.assertEqual(common['platform_domain'], 'Unknown')
                fields = common['observations'][0]['fields']
                self.assertEqual(fields['setpoint_volume_rate_l_ha'], 250000 * .0001)
                self.assertEqual(fields['grid_coordinate_basis'],
                                 'DECLARED_DEGREES_NO_DATUM_IN_SOURCE_NOT_PROJECTED')
                self.assertTrue(all(observation['clock_basis'] == 'Unknown'
                                    and observation['anchor_unix_us'] is None
                                    for observation in common['observations']))
                self.assertEqual(fields['record_time_us'], CAPTURE)
                self.assertIn('caller_supplied_capture_microseconds',
                              common['profile_units']['record_time_us'])

    def command(self, base, description, binary, *, name='GRD00001', extra=()):
        (base / 'task.xml').write_bytes(description)
        (base / (name + '.bin')).write_bytes(binary)
        return subprocess.run([os.sys.executable, str(SCRIPT), str(base / 'task.xml'),
            str(base / (name + '.bin')), str(base / 'out'), '--prescription',
            '--capture-time-us', str(CAPTURE), '--grid-byte-order', GRID_BYTE_ORDER, *extra],
            capture_output=True, text=True)

    def test_cell_values_carry_their_own_bytes_and_unused_layout_is_reported(self):
        description = document(grid(2, 3, 2, extra=' J="5" H="48"'),
                               zone(5, [('0001', 0, ' E="VPN-1"'), ('0006', 0, '')], designator='Rx'),
                               zone(9, [('000B', 400, '')], designator='Spare'))
        binary = cells(10000, 500, 20000, 600, 30000, 700, 40000, 800, 50000, 900, 60000, 1000)
        output, report = self.convert(description, binary)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 12)
        self.assertEqual(report['cells_read'], 6)
        self.assertEqual((report['zones_declared'], report['zones_used']), (2, 1))
        self.assertEqual(report['unused_zone_codes'], [9])  # named, not left to a count difference
        self.assertEqual([r['source_raw_value'] for r in rows[:4]], ['10000', '500', '20000', '600'])
        self.assertEqual([r['process_data_index'] for r in rows[:4]], ['0', '1', '0', '1'])
        self.assertEqual(rows[0]['source_cell_hex'], 'hex:' + struct.pack('<i', 10000).hex())
        self.assertEqual(rows[0]['source_value_origin'], 'GRID_CELL_BINARY')
        self.assertEqual(rows[0]['setpoint_volume_rate_l_ha'], str(10000 * .0001))
        self.assertEqual(rows[1]['setpoint_mass_rate_kg_m2'], str(500 * .000001))
        self.assertEqual(rows[0]['declared_value_presentation_ref_hex'], 'hex:' + b'VPN-1'.hex())
        self.assertEqual(rows[0]['value_presentation_disposition'],
                         'DECLARED_VALUE_PRESENTATION_NOT_APPLIED')
        self.assertEqual(rows[1]['value_presentation_disposition'], 'NOT_PROVIDED')
        self.assertEqual(report['value_presentation_declarations'], 1)
        self.assertEqual([r['declared_grid_columns'] for r in rows[:1]], ['3'])
        self.assertEqual(rows[0]['grid_cell_order_basis'],
                         'ROW_MAJOR_NORTHWARD_FROM_MINIMUM_CORNER_PER_DECODER_EXAMPLE_NOT_NORMATIVE')
        self.assertEqual(rows[0]['cell_position_basis'],
                         'RELATIVE_TO_DECLARED_GRID_ORIGIN_NOT_AN_ABSOLUTE_FIX')
        self.assertEqual([name for name in rows[0]
                          if 'latitude' in name or 'longitude' in name], [])

    def test_asymmetric_layouts_bind_each_cell_to_its_row_and_column(self):
        for columns, rows, expected in ((3, 2, [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]),
                                        (2, 3, [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)])):
            description = document(grid(2, columns, rows, extra=' J="1"'),
                                   zone(1, [('0001', 0, '')]))
            binary = cells(*range(10, 10 + columns * rows))
            output, report = self.convert(description, binary)
            table = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(table), columns * rows)
            self.assertEqual([(int(r['grid_cell_row']), int(r['grid_cell_column'])) for r in table],
                             expected)
            self.assertEqual([r['source_raw_value'] for r in table],
                             [str(value) for value in range(10, 10 + columns * rows)])
            self.assertEqual([r['declared_cell_north_offset_deg'] for r in table],
                             [str(Decimal('0.00010000') * row) for row, _ in expected])
            self.assertEqual([r['declared_cell_east_offset_deg'] for r in table],
                             [str(Decimal('0.00020000') * column) for _, column in expected])
            self.assertEqual({r['declared_grid_columns'] for r in table}, {str(columns)})
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                self.assertEqual(self.command(base, description, binary).returncode, 0)
                if os.environ.get('MUSUBI_TELEMETRY_READER'):
                    common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(PROFILE), str(base / 'out/observations.csv'), '--allow-equal-time'],
                        check=True, capture_output=True).stdout)
                    self.assertEqual(common['main_rows'], columns * rows)
                    last = common['observations'][-1]['fields']
                    self.assertEqual((last['grid_cell_row'], last['grid_cell_column']),
                                     (rows - 1, columns - 1))
                    self.assertEqual(last['cell_position_basis'],
                                     'RELATIVE_TO_DECLARED_GRID_ORIGIN_NOT_AN_ABSOLUTE_FIX')

    def test_unknown_ddi_keeps_its_value_without_a_unit(self):
        description = document(grid(2, 2, 1, extra=' J="1"'),
                               zone(1, [('0003', 0, ''), ('0001', 0, '')]))
        output, report = self.convert(description, cells(-5, 70000, 900, 80000))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r['source_ddi'] for r in rows], ['3', '1', '3', '1'])
        self.assertEqual(rows[0]['prescription_value_status'], 'UNKNOWN_DDI_NO_UNIT_ASSUMED')
        self.assertEqual(rows[0]['source_raw_value'], '-5')  # retained, including a negative value
        self.assertEqual(rows[0]['source_cell_hex'], 'hex:' + struct.pack('<i', -5).hex())
        self.assertEqual([rows[0][name] for name, _ in convert_isoxml_rate.SETPOINT_RATES.values()],
                         ['', '', ''])
        self.assertEqual(rows[1]['prescription_value_status'], 'SETPOINT_DECLARED')
        self.assertEqual((report['unsupported_values'], report['prescription_values']), (2, 2))

    def test_declared_zone_roles_come_from_the_task_not_from_the_number(self):
        variables = [('0001', 100, '')]
        plain = document(grid(1, 2, 1), zone(253, variables), zone(254, variables))
        rows = list(csv.DictReader(io.StringIO(self.convert(plain, bytes([253, 254]))[0])))
        self.assertEqual([r['treatment_zone_role'] for r in rows], ['DECLARED_ZONE', 'DECLARED_ZONE'])
        assigned = document(grid(1, 2, 1), zone(253, variables), zone(254, variables),
                            task='A="TSK-1" H="253" I="253" J="254"')
        rows = list(csv.DictReader(io.StringIO(self.convert(assigned, bytes([253, 254]))[0])))
        self.assertEqual([r['treatment_zone_role'] for r in rows],
                         ['DECLARED_DEFAULT_ZONE|DECLARED_POSITION_LOST_ZONE',
                          'DECLARED_OUT_OF_FIELD_ZONE'])
        self.assertEqual(rows[0]['task_designator_hex'], '')

    def test_a_zone_without_process_data_still_accounts_for_its_cell(self):
        description = document(grid(1, 2, 1), zone(1, [('0001', 100, '')]),
                               zone(2, [], body='<PLN A="1"/>'))
        output, report = self.convert(description, bytes([1, 2]))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['prescription_value_status'], 'ZONE_DECLARES_NO_PROCESS_DATA')
        self.assertEqual((rows[1]['source_ddi'], rows[1]['source_raw_value']), ('', ''))
        self.assertEqual(rows[1]['source_value_origin'], 'NOT_PROVIDED')
        self.assertEqual(rows[1]['source_cell_hex'], 'hex:02')
        self.assertEqual(report['cells_read'], 2)
        self.assertEqual(report['unread_polygon_declarations'], 1)
        self.assertEqual(report['zone_codes_without_process_data'], [2])
        self.assertEqual(report['unused_zone_codes'], [])  # both zones are selected by the grid

    def test_truncation_dimension_and_reference_faults_refuse(self):
        variables = [('0001', 100, ''), ('0006', 200, '')]
        two = document(grid(2, 2, 1, extra=' J="4"'), zone(4, variables))
        self.convert(two, cells(1, 2, 3, 4))  # the exact size is the only accepted one
        for broken in (cells(1, 2, 3, 4) + b'\x00', cells(1, 2, 3, 4)[:-1], cells(1, 2, 3),
                       cells(1, 2, 3, 4) + cells(5, 6), b''):
            self.refuses(two, broken)
        one = document(grid(1, 2, 2), zone(1, variables))
        self.convert(one, bytes([1, 1, 1, 1]))
        for broken in (bytes([1, 1, 1]), bytes([1, 1, 1, 1, 1]), bytes([1, 1, 1, 2])):
            self.refuses(one, broken)  # including a cell whose zone is not declared
        self.refuses(document(grid(2, 2, 1), zone(4, variables)), cells(1, 2, 3, 4))  # no J
        self.refuses(document(grid(2, 2, 1, extra=' J="9"'), zone(4, variables)), cells(1, 2, 3, 4))
        self.refuses(document(grid(2, 2, 1, extra=' J="4"'), zone(4, [])), b'')
        self.refuses(document(grid(2, 2, 1, extra=' J="4" H="15"'), zone(4, variables)), cells(1, 2, 3, 4))
        self.refuses(document(grid(1, 2, 1), zone(1, variables), zone(1, variables)), bytes([1, 1]))
        self.refuses(document(grid(1, 2, 1), grid(1, 2, 1), zone(1, variables)), bytes([1, 1]))
        self.refuses(document(grid(1, 2, 1)), bytes([1, 1]))
        self.refuses(one, bytes([1, 1, 1, 1]), grid_name='OTHER')  # the declared name must match
        self.refuses(one, bytes([1, 1, 1, 1]), byte_order='big-endian')
        self.refuses(one, bytes([1, 1, 1, 1]), capture_time_us='0')
        self.refuses(document(grid(1, 2, 2, north='91.0'), zone(1, variables)), bytes([1, 1, 1, 1]))
        self.refuses(document(grid(1, 2, 2, extra=' K="1"'), zone(1, variables)), bytes([1, 1, 1, 1]))
        self.refuses(document(grid(1, 2, 2).replace('C="0.00010000"', 'C="0"'), zone(1, variables)),
                     bytes([1, 1, 1, 1]))
        self.refuses(document(grid(1, 2, 2), zone(1, [('01', 5, '')])), bytes([1, 1, 1, 1]))
        self.refuses(document(grid(1, 2, 2), zone(1, [('0001', -1, '')])), bytes([1, 1, 1, 1]))
        self.refuses(document(grid(1, 2, 1, extra=' J="0"'), zone(0, [('0001', 0, '')])),
                     cells(-1, 5), grid_name='GRD00001')  # a selected DDI has no negative range

    def test_the_parser_guards_and_bounds_are_preserved(self):
        variables = [('0001', 100, '')]
        binary = bytes([1, 1])
        self.refuses(document(grid(1, 2, 1), zone(1, variables)).replace(
            b'<TSK', b'<!DOCTYPE TSK [<!ENTITY x "y">]><TSK', 1), binary)
        self.refuses(document(grid(1, 2, 1), zone(1, variables)).replace(b'VersionMinor="2"',
                                                                        b'VersionMinor="3"'), binary)
        self.refuses(document(grid(1, 2, 1), zone(1, variables)).replace(b'ISO11783_TaskData',
                                                                        b'TaskData'), binary)
        self.refuses(document(grid(1, 2, 1), zone(1, variables)) + b'\x00', binary)
        self.refuses(b'', binary)
        self.refuses(b'x' * (convert_isoxml_rate.DESCRIPTION_LIMIT + 1), binary)
        description = document(grid(1, 200, 5), zone(1, variables))
        self.assertEqual(self.convert(description, bytes([1]) * 1000)[1]['cells_read'], 1000)
        with mock.patch.object(convert_isoxml_rate, 'OUTPUT_LIMIT', 4096):
            self.refuses(description, bytes([1]) * 1000)
        empty = document(grid(1, 200, 5), zone(1, variables), zone(2, []))
        for binary in (bytes([2]) * 999 + bytes([1]), bytes([1]) + bytes([2]) * 999):
            self.assertEqual(self.convert(empty, binary)[1]['cells_read'], 1000)
            with mock.patch.object(convert_isoxml_rate, 'OUTPUT_LIMIT', 4096):
                self.refuses(empty, binary)
        with mock.patch.object(convert_isoxml_rate, 'GRID_LIMIT', 512):
            self.refuses(description, bytes([1]) * 1000)

    def test_prescription_and_timelog_options_never_mix(self):
        description = document(grid(1, 2, 1), zone(1, [('0001', 100, '')]))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.assertEqual(self.command(base, description, bytes([1, 1])).returncode, 0)
            for extra in (('--harvest',), ('--position',), ('--work-state',),
                          ('--taskdata', str(base / 'task.xml'))):
                failed = self.command(base, description, bytes([1, 1]), extra=extra)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(failed.stderr.splitlines()[-1],
                                 'ISOXML rate conversion failed; no successful conversion claim')
            plain = subprocess.run([os.sys.executable, str(SCRIPT), str(base / 'task.xml'),
                str(base / 'GRD00001.bin'), str(base / 'other'), '--capture-time-us', str(CAPTURE)],
                capture_output=True, text=True)
            self.assertEqual(plain.returncode, 2)  # prescription options require the mode
            self.assertFalse((base / 'other').exists())


    def test_each_value_carries_the_references_the_document_actually_declares(self):
        description = document(grid(1, 2, 2, name='GRD00007'),
                               zone(3, [('0001', 250000, ' C="PDT-1" D="DET-1" E="VPN-1"')],
                                    designator='Full rate'),
                               zone(7, [('0006', 12000, ' C="PDT-2"')], designator='Edge'))
        rows = list(csv.DictReader(io.StringIO(
            self.convert(description, bytes([3, 7, 7, 3]), grid_name='GRD00007')[0])))
        self.assertEqual(len(rows), 4)
        self.assertEqual({r['task_id_hex'] for r in rows}, {'hex:' + b'TSK-1'.hex()})
        self.assertEqual({r['task_designator_hex'] for r in rows},
                         {'hex:' + b'North block'.hex()})
        self.assertEqual({r['declared_grid_file_ref_hex'] for r in rows},
                         {'hex:' + b'GRD00007'.hex()})
        self.assertEqual([r['treatment_zone_code'] for r in rows], ['3', '7', '7', '3'])
        self.assertEqual([r['treatment_zone_designator_hex'] for r in rows],
                         ['hex:' + b'Full rate'.hex(), 'hex:' + b'Edge'.hex(),
                          'hex:' + b'Edge'.hex(), 'hex:' + b'Full rate'.hex()])
        self.assertEqual([r['declared_product_ref_hex'] for r in rows],
                         ['hex:' + b'PDT-1'.hex(), 'hex:' + b'PDT-2'.hex(),
                          'hex:' + b'PDT-2'.hex(), 'hex:' + b'PDT-1'.hex()])
        self.assertEqual(rows[0]['declared_device_element_ref_hex'], 'hex:' + b'DET-1'.hex())
        self.assertEqual(rows[1]['declared_device_element_ref_hex'], '')
        self.assertEqual({r['device_description_sha256'] for r in rows},
                         {'sha256:' + hashlib.sha256(description).hexdigest()})
        self.assertEqual({r['grid_sha256'] for r in rows},
                         {'sha256:' + hashlib.sha256(bytes([3, 7, 7, 3])).hexdigest()})
        self.assertEqual([r['source_ddi'] for r in rows], ['1', '6', '6', '1'])
        self.assertEqual(rows[0]['setpoint_volume_rate_l_ha'], str(250000 * .0001))
        self.assertEqual(rows[1]['setpoint_mass_rate_kg_m2'], str(12000 * .000001))

    def test_a_second_task_or_a_dangling_zone_reference_is_refused(self):
        variables = [('0001', 100, '')]
        two = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2">'
               '<TSK A="TSK-1">' + grid(1, 2, 1) + zone(1, variables) + '</TSK>'
               '<TSK A="TSK-2">' + grid(1, 2, 1, name='GRD00002') + zone(1, variables) + '</TSK>'
               '</ISO11783_TaskData>').encode()
        self.refuses(two, bytes([1, 1]))
        self.refuses(document(grid(1, 2, 1), zone(1, variables)), bytes([1, 9]))
        self.refuses(document(grid(2, 2, 1, extra=' J="4"'), zone(1, variables)), cells(5, 6))
        self.refuses(document(grid(1, 2, 1, name='GRD00007'), zone(1, variables)),
                     bytes([1, 1]), grid_name='GRD00001')


if __name__ == '__main__':
    unittest.main()
