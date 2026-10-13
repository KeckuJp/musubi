"""Composition of adopted reported heartbeat and state context; not reboot diagnosis."""
import csv
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from scripts.convert_dronecan_capture import convert_canopen
from scripts.convert_saved_state_transitions import convert
from tests.saved_can_window_helpers import common, ROOT
from tests.test_saved_state_transitions import unhex, subject

HEADER = 'time_us,standard_id,data_hex\n'
CITATION = ('dronecan-recorded/README.md: heartbeat9b8beed8 BOOT_UP0; '
            'EMCYac214071 reports no NMT; unknown retained')
PROFILE = ROOT/'profiles/declared/saved-state-transitions/profile.toml'


class CanopenBootContextTests(unittest.TestCase):
    def context(self, frames, directory):
        output, capture = convert_canopen(HEADER+frames, include_emergency=True)
        common(self, directory, output)
        transitions, report = convert(output.encode(),
            clock_basis='caller_selected_capture_clock_not_synchronized',
            subjects=('source_node_id',), states=(('node_reported_mode', CITATION),))
        observations = common(self, directory, transitions, PROFILE)
        self.assertEqual(report['source_sha256'], 'sha256:'+hashlib.sha256(output.encode()).hexdigest())
        self.assertTrue(all(row['clock_basis']=='Unknown' for row in observations['observations']))
        self.assertEqual(capture['frames'], capture['decoded_frames'])
        return list(csv.DictReader(io.StringIO(transitions))), report

    def test_later_boot_and_separate_initial_missing_unknown_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            rows, report = self.context('1000000,701,05\n2000000,701,00\n3000000,701,7f\n', directory)
            self.assertEqual([unhex(r['reported_state_hex']) for r in rows],
                             ['OPERATIONAL', 'BOOT_UP', 'PRE_OPERATIONAL'])
            self.assertEqual(rows[1]['transition_disposition'], 'CHANGE_BETWEEN_TWO_REPORTED_VALUES')
            self.assertEqual(unhex(rows[1]['previous_state_hex']), 'OPERATIONAL')
            self.assertEqual(report['reported_value_changes'], 2)  # changes, NOT two reboots
            rows, report = self.context('1000000,702,00\n1000000,703,06\n'
                '2000000,082,0000000000000000\n3000000,702,00\n', directory)
            self.assertEqual([subject(r['subject_hex']) for r in rows], [['2'], ['3'], ['2']])
            self.assertEqual(rows[0]['transition_disposition'], 'FIRST_REPORTED_VALUE')
            self.assertEqual(unhex(rows[1]['reported_state_hex']), 'UNKNOWN_6')
            self.assertEqual(rows[2]['transition_disposition'], 'PREVIOUS_VALUE_NOT_REPORTED')
            self.assertEqual(rows[2]['previous_record_time_us'], '')
            self.assertEqual(report['blank_state_records'], 1)
            self.assertEqual(report['reported_value_changes'], 0)

    def test_wrong_guarding_and_unqualified_context_refused(self):
        with self.assertRaises(ValueError):
            convert_canopen(HEADER+'1,701,80\n')
        source, _ = convert_canopen(HEADER+'1,701,00\n')
        for subjects, citation in ((('absent_source',), CITATION), (('source_node_id',), '')):
            with self.assertRaises(ValueError):
                convert(source.encode(), clock_basis='caller_source_capture_clock',
                        subjects=subjects, states=(('node_reported_mode', citation),))


if __name__ == '__main__':
    unittest.main()
