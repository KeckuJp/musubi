"""Fixed producer interpretation; authored maps, not road-rule certification."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from scripts.convert_lanelet_declarations import convert, GERMAN_SPEEDS_KMH
from tests.test_lanelet_declarations import MAP_A, PROFILE
from tests.saved_can_window_helpers import common

OPTIONS = dict(regulatory_declarations=True, traffic_rules='germany-vehicle-1.2.1')


def document(code, alternate='50 km/h', sign=True):
    data = MAP_A.replace('v="traffic_sign"', 'v="speed_limit"')
    data = data.replace('<tag k="area" v="true"/>', '<tag k="area" v="true"/><tag k="subtype" v="' + code + '"/>')
    data = data.replace('<tag k="subtype" v="speed_limit"/>', '<tag k="subtype" v="speed_limit"/><tag k="sign_type" v="' + alternate + '"/>')
    if not sign:
        data = data.replace('<member type="way" role="refers" ref="12"/>', '')
    return data.encode()


def selected(output):
    return next(r for r in csv.DictReader(io.StringIO(output)) if r['map_record_kind'] == 'RELATION' and r['map_element_id'] == '200')


class LaneletSignCodeTests(unittest.TestCase):
    def test_reported_source_precedence_and_other_map_common(self):
        cases = ((document('de274-30'), 30 / 3.6, 'FIRST_REFERS_WAY'),
                 (document('unused', 'de310', False), 50 / 3.6, 'RELATION_SIGN_TYPE'))
        for data, expected, basis in cases:
            default = convert(data, 42)
            output, report = convert(data, 42, **OPTIONS)
            row = selected(output)
            self.assertAlmostEqual(float(row['map_declared_speed_limit_m_s']), expected)
            self.assertTrue(row['map_speed_type_source'].startswith(basis))
            self.assertEqual(convert(data, 42), default)
            with tempfile.TemporaryDirectory() as tmp:
                observations = common(self, Path(tmp), output, PROFILE)['observations']
                target = next(o for o in observations if o['fields'].get('map_record_kind') == 'RELATION' and o['fields'].get('map_element_id') == 200)
                self.assertAlmostEqual(target['fields']['map_declared_speed_limit_m_s'], expected)
                self.assertEqual(target['clock_basis'], 'Unknown')

    def test_all_fixed_codes_unknown_and_important_refusals(self):
        self.assertEqual(len(GERMAN_SPEEDS_KMH), 19)
        for code, kmh in GERMAN_SPEEDS_KMH.items():
            row = selected(convert(document(code), 42, **OPTIONS)[0])
            self.assertAlmostEqual(float(row['map_declared_speed_limit_m_s']), kmh / 3.6)
        for code in ('DE:274', 'de999', 'fr274'):
            row = selected(convert(document(code), 42, **OPTIONS)[0])
            self.assertEqual(row['map_declared_speed_limit_m_s'], '')
            self.assertEqual(row['map_speed_limit_disposition'], 'UNQUALIFIED_SIGN_TEXT')
            self.assertIn('ALTERNATE_DIFFERENT', row['map_speed_type_conflict'])
        self.assertAlmostEqual(float(selected(convert(document('20 mph'), 42, **OPTIONS)[0])['map_declared_speed_limit_m_s']), 8.9408)
        missing = document('de274').replace(b'<tag k="subtype" v="de274"/>', b'')
        with self.assertRaises(ValueError):
            convert(missing, 42, **OPTIONS)
        for wrong in (True, 1, '', 'france-vehicle-1.2.1'):
            with self.assertRaises(ValueError):
                convert(document('de274'), 42, regulatory_declarations=True, traffic_rules=wrong)
        with self.assertRaises(ValueError):
            convert(document('de274'), 42, traffic_rules='germany-vehicle-1.2.1')


if __name__ == '__main__':
    unittest.main()
