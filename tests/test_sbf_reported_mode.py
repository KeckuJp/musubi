"""Fixed driver reported enum semantics, not physical fix evidence."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_sbf_records import module, block, PROFILE
from tests.test_saved_state_transitions import convert as transitions, unhex, PROFILE as TRANSITION_PROFILE


class SbfReportedModeTests(unittest.TestCase):
    def test_modes_unknown_and_default(self):
        raw = b''.join(block(mode=mode, error=7) for mode in (4, 0, 0x84, 15))
        old, _ = module.convert(raw, 100)
        output, report = module.convert(raw, 100, include_reported_mode=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r['sbf_reported_mode'] for r in rows], ['RTK_FIXED', 'NO_PVT', 'RTK_FIXED', ''])
        self.assertEqual(rows[2]['sbf_mode_upper_bits_raw'], '128')
        self.assertEqual(rows[3]['sbf_mode_disposition'], 'UNKNOWN_REPORTED_MODE')
        for before, after in zip(csv.DictReader(io.StringIO(old)), rows):
            self.assertEqual(before, {k: after[k] for k in before})
        self.assertEqual(report['decoded_blocks'], 4)
        with self.assertRaises(ValueError): module.convert(raw, 100, include_reported_mode='yes')

    def test_two_inputs_reuse_transition_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'common reader must be configured')
        for modes, expected in [((4, 0), ('RTK_FIXED', 'NO_PVT')), ((5, 1), ('RTK_FLOAT', 'STAND_ALONE'))]:
            parts = [module.convert(block(mode=m, latitude_deg=10+i, longitude_deg=-20-i),
                                    (i+1)*1000000, include_reported_mode=True)[0] for i,m in enumerate(modes)]
            joined = parts[0] + parts[1].split('\n',1)[1]
            output, report = transitions(joined.encode(), clock_basis='caller_capture_us',
                subjects=('sbf_block_id',), states=(('sbf_reported_mode', 'driver5613af29 mode low nibble reported only'),))
            self.assertEqual(report['reported_value_changes'], 1)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory)/'modes.csv'; source.write_text(joined)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(source), '--allow-equal-time'],check=True,capture_output=True).stdout)
                self.assertEqual(common['main_rows'],2)
                self.assertEqual([o['fields']['sbf_reported_mode'] for o in common['observations']],list(expected))
                target = Path(directory)/'transition.csv'; target.write_text(output)
                common = json.loads(subprocess.run([reader,str(TRANSITION_PROFILE),str(target),'--allow-equal-time'],check=True,capture_output=True).stdout)
                fields = common['observations'][1]['fields']
                self.assertEqual(unhex(fields['previous_state_hex']),expected[0])
                self.assertEqual(unhex(fields['reported_state_hex']),expected[1])


if __name__ == '__main__': unittest.main()
