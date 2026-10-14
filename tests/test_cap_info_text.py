"""Authored CAP text and contexts, not actual municipal messages."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import convert_cap_info_text as text
from tests.test_cap_alert import alert, info, AREA_ONE, AREA_TWO, unhex

ROOT = Path(__file__).resolve().parents[1]


def rows(value):
    previous = csv.field_size_limit(64 * 1024 * 1024)
    try:
        return list(csv.DictReader(io.StringIO(value)))
    finally:
        csv.field_size_limit(previous)


class CapInfoTextTests(unittest.TestCase):
    def test_audience_option_refuses_non_boolean_before_parsing(self):
        for value in ('false', 1, None, []):
            with self.assertRaisesRegex(ValueError, 'explicit boolean'):
                text.convert(b'not XML', include_audience=value)

    def test_declared_audience_private_restricted_and_missing_reach_common(self):
        for scope, extra in [('Private', '<addresses>a@example.test b@example.test</addresses>'),
                             ('Restricted', '<restriction>Declared group only</restriction>')]:
            raw = alert(info(extra='<language>ja-JP</language><headline>Notice</headline>'), scope=scope, extra=extra)
            legacy, _ = text.convert(raw)
            outputs, report = text.convert(raw, include_audience=True)
            self.assertEqual(outputs['observations.csv'], legacy['observations.csv'])
            common = self.common(outputs['info-text.csv'], 'profiles/declared/cap-info-text/profile.toml')
            fields = common['observations'][0]['fields']
            name = 'addresses' if scope == 'Private' else 'restriction'
            expected = 'a@example.test b@example.test' if scope == 'Private' else 'Declared group only'
            self.assertEqual(unhex(fields[f'alert_{name}_hex']), expected)
            self.assertEqual(fields[f'alert_{name}_disposition'], 'REPORTED_TEXT')
            self.assertEqual(fields['alert_scope'], scope)
            self.assertEqual(common['observations'][0]['clock_basis'], 'Unknown')
            self.assertEqual(report['selected_audience_elements'], 1)
            self.assertEqual(report['bundle_known_elements_not_carried'], {})
        outputs, report = text.convert(alert(info() + info(), scope='Private', extra='<restriction/><future>x</future>'), include_audience=True)
        row = rows(outputs['info-text.csv'])[0]
        self.assertEqual(row['alert_addresses_disposition'], 'ABSENT')
        self.assertEqual(row['alert_restriction_disposition'], 'PRESENT_EMPTY')
        self.assertEqual(report['selected_audience_elements'], 1)
        self.assertTrue(report['bundle_unknown_elements_not_carried'])
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'source.xml', Path(tmp) / 'out'
            source.write_bytes(alert(scope='Public', extra='<addresses>declared-but-not-reconciled</addresses>'))
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_cap_info_text.py'),
                            str(source), str(output), '--include-audience'], check=True, capture_output=True)
            self.assertEqual((output / 'source.xml').read_bytes(), source.read_bytes())
            fields = self.common((output / 'info-text.csv').read_text(),
                'profiles/declared/cap-info-text/profile.toml')['observations'][0]['fields']
            self.assertEqual(fields['alert_scope'], 'Public')
            self.assertEqual(unhex(fields['alert_addresses_hex']), 'declared-but-not-reconciled')
        for extra in ('<addresses>a</addresses><addresses>b</addresses>', '<restriction><x/></restriction>'):
            with self.assertRaises(ValueError):
                text.convert(alert(extra=extra), include_audience=True)

    def common(self, data, profile):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not reader:
            self.skipTest('actual common reader not configured')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.csv'; path.write_text(data)
            return json.loads(subprocess.run([reader, str(ROOT / profile), str(path),
                '--allow-equal-time'], check=True, capture_output=True).stdout)

    def test_multilingual_same_info_and_area_relation_reach_common(self):
        for sender in ('first@example.test', 'second@example.test'):
            raw = alert(info(areas=AREA_ONE + AREA_TWO, extra='<language>ja-JP</language>'
                '<headline>報告, &quot;引用&quot;</headline><description>一行\n二行</description>'
                '<instruction>これは発信者の引用です</instruction>') +
                info(areas='', extra='<language>en-US</language><headline>Different variant</headline>'), sender=sender)
            outputs, report = text.convert(raw)
            base = self.common(outputs['observations.csv'], 'profiles/declared/cap-alert/profile.toml')
            quoted = self.common(outputs['info-text.csv'], 'profiles/declared/cap-info-text/profile.toml')
            self.assertEqual((base['main_rows'], quoted['main_rows']), (3, 2))
            self.assertEqual(report['text_info_rows'], 2)
            self.assertEqual(report['selected_text_elements'], 4)
            self.assertEqual(report['bundle_known_elements_not_carried'], {})
            self.assertEqual(report['base_report']['known_elements_not_carried']['info/headline'], 2)
            for index, row in enumerate(quoted['observations']):
                fields = row['fields']; start = int(fields['base_row_start'])
                count = int(fields['base_row_count'])
                self.assertEqual(row['clock_basis'], 'Unknown')
                self.assertEqual(fields['info_index'], index)
                self.assertEqual(fields['alert_sender'], sender)
                self.assertEqual(fields['source_info_path'], f'/alert/info[{index}]')
                self.assertEqual(fields['base_csv_sha256'], hashlib.sha256(outputs['observations.csv'].encode()).hexdigest())
                for related in base['observations'][start:start+count]:
                    for key in ('source_sha256', 'info_index', 'record_time_us', 'info_language_hex', 'info_categories'):
                        self.assertEqual(fields[key], related['fields'][key])
            first, second = [row['fields'] for row in quoted['observations']]
            self.assertEqual(unhex(first['info_headline_hex']), '報告, "引用"')
            self.assertEqual(unhex(first['info_description_hex']), '一行\n二行')
            self.assertEqual(second['info_description_disposition'], 'ABSENT')
            self.assertIn('NOT_EXECUTED', first['text_basis'])

    def test_missing_empty_unknown_and_no_info(self):
        outputs, report = text.convert(alert(info(extra='<headline/><description> </description>'
            '<future>uninterpreted</future><contact>retained in original</contact>')))
        row = rows(outputs['info-text.csv'])[0]
        self.assertEqual(row['info_headline_hex'], 'hex:')
        self.assertEqual(row['info_headline_disposition'], 'PRESENT_EMPTY')
        self.assertEqual(row['info_description_disposition'], 'REPORTED_TEXT')
        self.assertEqual(row['info_instruction_disposition'], 'ABSENT')
        self.assertEqual(report['bundle_known_elements_not_carried'], {'info/contact': 1})
        self.assertTrue(report['base_report']['unknown_elements_not_carried'])
        outputs, report = text.convert(alert())
        row = rows(outputs['info-text.csv'])[0]
        self.assertEqual(row['cap_info_state'], 'ALERT_WITHOUT_INFO')
        self.assertEqual(row['info_index'], '')
        self.assertEqual(row['source_info_path'], '/alert')
        self.assertEqual(report['text_info_rows'], 0)

    def test_bounds_rejections_and_nonduplicated_large_text(self):
        raw = alert(info(areas=AREA_ONE * 200, extra='<description>' + '量' * 70000 + '</description>'))
        outputs, report = text.convert(raw)
        self.assertEqual(len(rows(outputs['observations.csv'])), 200)
        self.assertEqual(len(rows(outputs['info-text.csv'])), 1)
        self.assertEqual(len(unhex(rows(outputs['info-text.csv'])[0]['info_description_hex'])), 70000)
        common = self.common(outputs['info-text.csv'], 'profiles/declared/cap-info-text/profile.toml')
        self.assertEqual(common['main_rows'], 1)
        self.assertEqual(common['observations'][0]['fields']['base_row_count'], 200)
        total = sum(len(value.encode()) for value in outputs.values())
        with patch.object(text, 'MAX_OUTPUT_BYTES', total - 1):
            with self.assertRaisesRegex(ValueError, 'bundle output'): text.convert(raw)
        for extra in ('<headline>a</headline><headline>b</headline>', '<description>a<x/>b</description>'):
            with self.assertRaises(ValueError): text.convert(alert(info(extra=extra)))
        with self.assertRaises(ValueError): text.convert(alert().replace(b'cap:1.2', b'cap:1.1'))
        with self.assertRaises(ValueError): text.convert(alert(sent='2026-09-12T14:05:00Z'))
        with self.assertRaises(ValueError): text.convert(b'<!DOCTYPE alert><alert/>')

    def test_legacy_bytes_and_cli_original_preservation(self):
        raw = alert(info(areas=AREA_ONE + AREA_TWO, extra='<headline>Authored notice</headline>'))
        outputs, report = text.convert(raw)
        self.assertEqual(hashlib.sha256(outputs['observations.csv'].encode()).hexdigest(),
                         '471cd466ef53c11e9cb829dd1e99f6872a400c7ffad130e72b9414efca3df8a6')
        self.assertEqual(hashlib.sha256(json.dumps(report['base_report'], sort_keys=True).encode()).hexdigest(),
                         '9453cb06ddbeaeae8d295e298b5b6cca09a6c9af4dfbf51391eb81d1ca967133')
        with tempfile.TemporaryDirectory() as tmp:
            path, out = Path(tmp) / 'input.xml', Path(tmp) / 'out'; path.write_bytes(raw)
            command = [os.sys.executable, str(ROOT / 'scripts/convert_cap_info_text.py'), str(path), str(out)]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((out / 'source.xml').read_bytes(), raw)
            self.assertEqual((out / 'observations.csv').read_text(), outputs['observations.csv'])
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
