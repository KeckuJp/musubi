"""Authored RINEX scale records; no receiver or position qualification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_rinex_records import convert
from tests.test_rinex_records import labelled, obs_header, obs_line, epoch_line, CAPTURE, CODES, ROOT, CASE


def scale(factor, codes=(), system='G', count=None):
    count = len(codes) if count is None else count
    rows = [labelled(f'{system} {factor:4d}  {count:2d}' + ''.join(' '+x for x in codes[:12]), 'SYS / SCALE FACTOR')]
    for n in range(12, len(codes), 12):
        rows.append(labelled(' '*10 + ''.join(' '+x for x in codes[n:n+12]), 'SYS / SCALE FACTOR'))
    return rows


def data(header, body):
    return ('\n'.join(header + body)+'\n').encode()


class ScaleTests(unittest.TestCase):
    def test_declared_all_and_prospective_selected_update_common(self):
        header = obs_header(extra=scale(10))
        payload = data(header, [epoch_line(count=1), obs_line('G01', [(123.4,'','')]*4),
            epoch_line(flag=4, count=1, second=37), *scale(100, ('L1C',)),
            epoch_line(count=1, second=38), obs_line('G02', [(123.4,'','')]*4)])
        with self.assertRaises(ValueError): convert(payload, 'obs', CAPTURE)
        result, report = convert(payload, 'obs', CAPTURE, reported_scale_factors=True)
        rows = list(csv.DictReader(io.StringIO(result)))
        self.assertEqual(rows[1]['rinex_carrier_phase_cycles'], '12.34')
        self.assertEqual(rows[5]['rinex_carrier_phase_cycles'], '1.234')
        self.assertEqual(rows[4]['rinex_scale_divisor'], '10')
        reader = os.environ.get('MUSUBI_TELEMETRY_READER', str(ROOT/'target/debug/examples/read_telemetry_csv'))
        self.assertTrue(Path(reader).is_file(), 'common reader is required')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'observations.csv'; path.write_text(result)
            common = json.loads(subprocess.check_output([reader, str(CASE/'obs-profile.toml'), str(path), '--allow-equal-time']))
        self.assertEqual(len(common['observations']), 8)
        self.assertEqual(common['observations'][5]['fields']['rinex_carrier_phase_cycles'], 1.234)
        self.assertEqual(report['header_updates'], 1)

    def test_second_input_continuation_unknown_missing_and_no_scale_default(self):
        codes = ('C1C','L1C','D1C','S1C','C1W','L1W','D1W','S1W','C2W','L2W','D2W','S2W','C9Z')
        payload = data(obs_header(codes=codes, extra=scale(1000, codes)),
                       [epoch_line(count=1), obs_line('G03', [(2500,'','')]*12 + [(13,'','')])])
        result, _ = convert(payload, 'obs', CAPTURE, reported_scale_factors=True)
        rows = list(csv.DictReader(io.StringIO(result)))
        self.assertEqual(rows[0]['rinex_pseudorange_m'], '2.5')
        self.assertEqual(rows[-1]['rinex_scale_divisor'], '1000')
        self.assertEqual(rows[-1]['rinex_pseudorange_m'], '')
        reader = os.environ.get('MUSUBI_TELEMETRY_READER', str(ROOT/'target/debug/examples/read_telemetry_csv'))
        self.assertTrue(Path(reader).is_file(), 'common reader is required')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'observations.csv'; path.write_text(result)
            common = json.loads(subprocess.check_output([reader, str(CASE/'obs-profile.toml'), str(path), '--allow-equal-time']))
        self.assertEqual(len(common['observations']), 13)
        self.assertEqual(common['observations'][0]['fields']['rinex_pseudorange_m'], 2.5)
        plain = data(obs_header(), [epoch_line(count=1), obs_line('G01', [(None,'',''),(1,'',''),(2,'',''),(3,'','')])])
        native, native_report = convert(plain,'obs',CAPTURE)
        enriched, _ = convert(plain,'obs',CAPTURE,reported_scale_factors=True)
        before = list(csv.DictReader(io.StringIO(native)))
        after = list(csv.DictReader(io.StringIO(enriched)))
        for a,b in zip(before,after):
            self.assertEqual(a, {k:b[k] for k in a})
            self.assertEqual(b['rinex_scale_divisor'],'1')
        self.assertEqual(before[0]['rinex_observation_status'],'NOT_OBSERVED')
        self.assertEqual(convert(plain,'obs',CAPTURE),(native,native_report))

    def test_malformed_scaling_and_code_remap_refuse(self):
        body = [epoch_line(count=1), obs_line('G01', [(1,'','')]*4)]
        for extra in (scale(2), scale(10,('L1C',),count=2), scale(10,('L1C',))*2,
                      scale(10,('L9Z',)), [labelled(' '*10+' L1C','SYS / SCALE FACTOR')]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                convert(data(obs_header(extra=extra),body),'obs',CAPTURE,reported_scale_factors=True)
        update = labelled('G    1 C1C','SYS / # / OBS TYPES')
        with self.assertRaises(ValueError):
            convert(data(obs_header(extra=scale(10)),body+[epoch_line(flag=4,count=1,second=37),update]),'obs',CAPTURE,reported_scale_factors=True)

    def test_subnormal_exponent_refuses_and_small_decimal_stays_nonzero(self):
        raw = 'G01' + '5E-324'.rjust(14) + '  '
        payload = data(obs_header(codes=('L1C',), extra=scale(10)), [epoch_line(count=1), raw])
        with self.assertRaisesRegex(ValueError, 'invalid observation value'):
            convert(payload, 'obs', CAPTURE, reported_scale_factors=True)
        decimal = 'G01' + '.0000000000001'.rjust(14) + '  '
        result, _ = convert(data(obs_header(codes=('L1C',), extra=scale(1000)),
                                 [epoch_line(count=1), decimal]), 'obs', CAPTURE,
                            reported_scale_factors=True)
        self.assertGreater(float(next(csv.DictReader(io.StringIO(result)))['rinex_carrier_phase_cycles']), 0)
