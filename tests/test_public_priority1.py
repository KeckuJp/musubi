"""Boundary regressions for source-reported scalar values and selected CAN clocks."""
import csv
import io
import unittest
from scripts.recorded_units import normalize
from scripts.convert_candump_recorded import convert

class ScalarBoundaries(unittest.TestCase):
    def test_nonzero_underflow_is_refused(self):
        for value in (5e-324, -5e-324):
            for unit in ('mV', 'mm', 'deg'):
                with self.subTest(value=value, unit=unit):
                    with self.assertRaisesRegex(ValueError, 'underflow'):
                        normalize(value, unit)

    def test_representable_zero_identity_and_offset(self):
        self.assertEqual(normalize(0, 'mV'), ('voltage_v', 0.0))
        self.assertEqual(normalize(5e-324, 'V'), ('voltage_v', 5e-324))
        self.assertEqual(normalize(1250, 'mV'), ('voltage_v', 1.25))
        self.assertEqual(normalize(-273.15, 'degC'), ('temperature_k', 0.0))

class SelectedCanClock(unittest.TestCase):
    def test_other_interface_decrease_preserved_without_sorting(self):
        text = '(1700000000.000003) can0 123#0102\n(1700000000.000001) can1 123#0304\n(1700000000.000004) can0 123#0506\n'
        output, report = convert(text, 'can0')
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([x['data_hex'] for x in rows], ['0102', '0506'])
        self.assertEqual(report['selected_frames'], 2)
        self.assertEqual(report['unconverted_lines'], 1)
        self.assertEqual(report['records'][1]['reason'], 'UNSELECTED_INTERFACE')
        self.assertEqual(report['records'][1]['capture_time_us'], 1700000000000001)

    def test_selected_decrease_still_refuses(self):
        with self.assertRaisesRegex(ValueError, 'decreasing capture time'):
            convert('(1700000000.000003) can0 123#01\n(1700000000.000002) can0 123#02\n', 'can0')

    def test_unselected_out_of_range_still_refuses(self):
        with self.assertRaisesRegex(ValueError, 'out-of-range'):
            convert('(1700000000.000003) can0 123#01\n(9999999999999999999.000000) can1 123#02\n', 'can0')
