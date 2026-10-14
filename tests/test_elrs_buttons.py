"""Saved declarations only; no button, radio or configuration write."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_elrs_settings import convert_elrs_settings, SOURCE_FORMAT

ROOT = Path(__file__).resolve().parents[1]


def convert(document, enabled=True):
    return convert_elrs_settings(json.dumps(document), source_format=SOURCE_FORMAT,
                                 capture_time_us=9, role='tx', include_button_actions=enabled)


class ButtonDeclarationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('MUSUBI_TELEMETRY_READER'), 'actual common reader required')
    def test_two_distinct_layouts_common_and_cli(self):
        first = {'config': {'uid': [1, 2, 3], 'button-actions': [{'action': [
            {'is-long-press': False, 'count': 0, 'action': 1},
            {'is-long-press': True, 'count': 7, 'action': 0}]}]}}
        second = {'config': {'button-actions': [{'color': 255, 'action': [
            {'is-long-press': True, 'count': 2, 'action': 8, 'extra': 'PRIVATE_SECRET'},
            {'is-long-press': True, 'count': 2, 'action': 8}]}, {'action': [
            {'is-long-press': False, 'count': 7, 'action': 15}, {'action': 5}]}]}}
        for index, document in enumerate((first, second)):
            text, report = convert(document)
            expected_count = 2 if index == 0 else 4
            self.assertEqual(report['output_records'], expected_count)
            self.assertNotIn('PRIVATE_SECRET', text + json.dumps(report))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'out.csv'; path.write_text(text)
                common = json.loads(subprocess.run([
                    os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/elrs-settings/profile.toml'),
                    str(path), '--allow-equal-time'], check=True, capture_output=True).stdout)
                source = Path(directory) / 'input.json'; source.write_text(json.dumps(document))
                target = Path(directory) / 'converted'
                subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_elrs_settings.py'),
                                str(source), str(target), '--source-format', SOURCE_FORMAT,
                                '--capture-time-us', '9', '--role', 'tx', '--include-button-actions'],
                               check=True, capture_output=True)
                self.assertEqual((target / 'observations.csv').read_text(), text)
            self.assertEqual(common['main_rows'], expected_count)
            fields = [o['fields'] for o in common['observations']]
            self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
            self.assertTrue(all(f['elrs_source_sha256'].startswith('sha256:') for f in fields))
            if index == 0:
                self.assertEqual([f['button_matching_callback_count'] for f in fields], [1, 8])
                self.assertEqual(fields[0]['elrs_value_label'], 'ACTION_INCREASE_POWER')
                self.assertEqual(fields[1]['elrs_value_status'], 'DISABLED_DECLARATION')
            else:
                self.assertEqual([f['button_action_index'] for f in fields], [0, 1, 0, 1])
                self.assertEqual(fields[2]['elrs_value_status'], 'UNKNOWN_ENUM_RETAINED')
                self.assertEqual(fields[3]['elrs_value_status'], 'INCOMPLETE_DECLARATION')
                self.assertIsNone(fields[3]['button_matching_callback_count'])
                self.assertTrue(any('extra' in f['path'] for f in report['unknown_retained_fields']))

    def test_shape_range_secret_and_default_boundary(self):
        action = {'is-long-press': False, 'count': 0, 'action': 1}
        valid = {'config': {'fan-mode': 2, 'button-actions': [{'action': [action, action]}]}}
        default, _ = convert(valid, False)
        self.assertNotIn('button_index', default)
        for bad in ({}, [None], [{'action': []}], [{'action': [action] * 3}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                convert({'config': {'button-actions': bad}})
        for key, value in (('count', -1), ('count', 8), ('count', True),
                           ('action', 16), ('action', 1.5), ('is-long-press', 1)):
            bad = dict(action, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                convert({'config': {'button-actions': [{'action': [bad, action]}]}})
        secret = dict(action, password='PRIVATE_SECRET')
        output, report = convert({'config': {'button-actions': [{'action': [secret, action]}]}})
        self.assertEqual(report['output_records'], 1)
        self.assertEqual(len(report['redacted_fields']), 1)
        self.assertNotIn('PRIVATE_SECRET', output + json.dumps(report))
        with self.assertRaises(ValueError):
            convert_elrs_settings('{"config":{"button-actions":[],"button-actions":[]}}',
                source_format=SOURCE_FORMAT, capture_time_us=0, role='tx', include_button_actions=True)


if __name__ == '__main__':
    unittest.main()
