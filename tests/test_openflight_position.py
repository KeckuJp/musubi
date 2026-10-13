"""Authored fixed-consumer inputs, never installed FreeFlight evidence."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_electrical_schema_reuse import module, ROOT

BASIS = 'openflight-fe9ca495'


def document(keys, rows):
    return {'exchange': {'exchange_type': 'flight_logging', 'message': {
        'message_type': 'flight_logging_submission',
        'file': dict(module.GUTMA_PINS),
        'flight_data': {'aircraft': {'serial_number': 'authored-aircraft', 'product_id': 'example',
                                    'model': None, 'future': {'keep': True}}},
        'flight_logging': {'uom_system': 'Metric', 'logging_start_dtg': 'authored-start',
                          'flight_logging_keys': keys, 'flight_logging_items': rows}}}}


class OpenFlightPositionTests(unittest.TestCase):
    def convert(self, doc):
        return module.convert(json.dumps(doc), module.GUTMA_FORMAT, gutma_position_basis=BASIS)

    def test_two_layouts_common_and_default(self):
        for keys, values, lat, lon in [
            (['timestamp', 'battery_percent', 'gps_lat', 'gps_lon', 'future'], [2, 60, 35, 139, 321], 35, 139),
            (['gps_lon', 'future', 'battery_percent', 'timestamp', 'gps_lat'], [-80, 654, 40, 7, -20], -20, -80)]:
            doc = document(keys, [values])
            result = self.convert(doc)
            row = next(csv.DictReader(io.StringIO(result)))
            self.assertEqual(float(row['reported_latitude_deg']), lat)
            self.assertEqual(float(row['reported_longitude_deg']), lon)
            self.assertEqual(row['reported_position_disposition'], 'REPORTED_VALID_CONSUMER_COORDINATE')
            self.assertEqual(json.loads(bytes.fromhex(row['source_record_hex'][4:])), values)
            self.assertNotIn('reported_latitude_deg', module.convert(json.dumps(doc), module.GUTMA_FORMAT))
            reader = os.environ.get('MUSUBI_TELEMETRY_READER')
            if not reader:
                self.fail('common reader executable required for this test')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'out.csv'
                path.write_text(result)
                common = json.loads(subprocess.run([reader, str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'), str(path)], check=True, capture_output=True).stdout)
                obs = common['observations'][0]
                self.assertEqual(common['main_rows'], 1)
                self.assertEqual(obs['clock_basis'], 'Unknown')
                self.assertEqual(obs['fields']['reported_latitude_deg'], lat)
                self.assertEqual(obs['fields']['reported_longitude_deg'], lon)
                self.assertEqual(obs['fields']['reported_aircraft_serial_number_hex'], 'hex:' + b'authored-aircraft'.hex())

    def test_missing_sentinel_and_refusal(self):
        keys = ['timestamp', 'battery_percent', 'gps_lat', 'gps_lon']
        for values, status in [([0, 50, 0, 0], 'CONSUMER_ZERO_PAIR_UNAVAILABLE'),
                               ([0, 50, 500, 500], 'CONSUMER_SENTINEL_UNAVAILABLE')]:
            row = next(csv.DictReader(io.StringIO(self.convert(document(keys, [values])))))
            self.assertEqual(row['reported_position_disposition'], status)
            self.assertEqual(row['reported_latitude_deg'], '')
        missing = next(csv.DictReader(io.StringIO(self.convert(document(keys[:2], [[0, 50]])))))
        self.assertEqual(missing['reported_position_disposition'], 'COLUMNS_UNAVAILABLE')
        for bad in ([0, 50, 91, 1], [0, 50, 1, 181], [0, 50, float('nan'), 1]):
            with self.assertRaises(ValueError): self.convert(document(keys, [bad]))
        doc = document(keys, [[0, 50, 1, 2]])
        doc['exchange']['message']['flight_data']['aircraft']['serial_number'] = 3
        with self.assertRaises(ValueError): self.convert(doc)
        with self.assertRaises(ValueError): module.convert('{}', 'px4-battery', gutma_position_basis=BASIS)
        with self.assertRaises(ValueError): module.convert('{}', module.GUTMA_FORMAT, gutma_position_basis='other')


if __name__ == '__main__': unittest.main()
