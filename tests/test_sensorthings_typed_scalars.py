"""Authored typed observations, not service/device verification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_sensorthings_dataarray as target
from tests.test_sensorthings_dataarray import metadata, group, LINK, PROPERTY, TIME, PROFILE


class TypedScalarTests(unittest.TestCase):
    def run_case(self, kind, values, unit=None):
        meta = metadata()
        meta['observationType'] = 'http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_' + {'count': 'CountObservation', 'truth': 'TruthObservation'}[kind]
        meta['unitOfMeasurement'] = dict.fromkeys(('name', 'symbol', 'definition')) if unit is None else unit
        source = {'value': [group([[i, TIME, None, v] for i, v in enumerate(values)])]}
        return target.convert(json.dumps(source), json.dumps(meta), datastream_link=LINK,
                              property_definition=PROPERTY, capture_time_us=50, result_kind=kind)

    def test_two_kinds_common_and_null(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'required actual common reader')
        for kind, values, field in [('count', [-(2**53-1), 0, 2**53-1, None], 'count_reported'),
                                    ('truth', [True, False, None], 'truth_reported')]:
            output, report = self.run_case(kind, values)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'data.csv'; path.write_text(output)
                run = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            observations = json.loads(run.stdout)['observations']
            expected = values if kind == 'count' else ['true', 'false', None]
            self.assertEqual([o['fields'][field] for o in observations], expected)
            self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in observations))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['result_kind'] for r in rows], [kind]*len(values))
            self.assertNotIn('temperature_k', rows[0])
            self.assertEqual((report['converted_values'], report['withheld_values']), (len(values)-1, 1))
            self.assertEqual(json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:]))['value'][0]['dataArray'][0][-1], values[0])

    def test_important_type_unit_and_precision_refusals(self):
        for kind, invalid in [('count', [True, 1.0, 2**53, -(2**53), '1']), ('truth', [0, 1, 'true', []])]:
            for value in invalid:
                with self.subTest(kind=kind, value=value), self.assertRaises(ValueError): self.run_case(kind, [value])
        for kind in ('count', 'truth'):
            with self.assertRaises(ValueError): self.run_case(kind, [None], {'name': 'count', 'symbol': None, 'definition': None})

    def test_cli_and_wrong_selected_kind(self):
        output, _ = self.run_case('truth', [False])
        row = next(csv.DictReader(io.StringIO(output)))
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, meta = base/'source.json', base/'metadata.json'
            source.write_bytes(bytes.fromhex(row['source_record_hex'][4:]))
            meta.write_bytes(bytes.fromhex(row['dataarray_metadata_hex'][4:]))
            args = [sys.executable, str(Path(target.__file__)), str(source), str(meta), str(base/'out.csv'),
                    '--report', str(base/'report.json'), '--datastream-link', LINK,
                    '--observed-property', PROPERTY, '--capture-time-us', '50']
            wrong = subprocess.run(args + ['--result-kind', 'count'], capture_output=True, text=True)
            self.assertNotEqual(wrong.returncode, 0)
            self.assertFalse((base/'out.csv').exists())
            good = subprocess.run(args + ['--result-kind', 'truth'], capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual((base/'out.csv').read_text(), output)


if __name__ == '__main__': unittest.main()
