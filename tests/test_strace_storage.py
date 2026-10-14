import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_strace_storage as target

ROOT = Path(__file__).resolve().parents[1]


class SavedStorage(unittest.TestCase):
    def convert(self, directory, text, **kwargs):
        path = Path(directory)/'saved.trace'
        path.write_text(text)
        options = dict(start_record=0, stop_record=len(text.splitlines()), source_commit=target.PIN,
                       session='authored-A', process_id=123)
        options.update(kwargs)
        return target.convert_path(path, **options)

    def test_two_saved_sessions_common(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'required stage must supply common reader')
        samples = [('A', '1700000000.000001 write(3</tmp/a>, "abc", 3) = -1 ENOSPC (No space left on device)\n'),
                   ('B', '1700000001.000001 pwrite64(3</tmp/b>, "x", 1, 9007199254740993) = -1 ENOSPC (No space left on device)\n'
                    '1700000001.000002 fsync(3</tmp/c>) = -1 ENOSPC (No space left on device)\n')]
        for session, text in samples:
            with tempfile.TemporaryDirectory() as directory:
                rendered, report = self.convert(directory, text, session=session)
                rows = list(csv.DictReader(io.StringIO(rendered)))
                self.assertEqual(report['enospc'], len(rows))
                self.assertEqual(report['withheld'], 0)
                if session == 'B':
                    self.assertEqual(rows[0]['offset_bytes_reported'], 'integer:9007199254740993')
                    self.assertNotEqual(rows[0]['target_escaped'], rows[1]['target_escaped'])
                output = Path(directory)/'out.csv'; output.write_text(rendered)
                run = subprocess.run([reader, str(ROOT/'profiles/declared/strace-storage/profile.toml'), str(output)], capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr.decode())
                observations = json.loads(run.stdout)['observations']
                self.assertEqual(len(observations), len(rows))
                self.assertEqual(observations[0]['fields']['error_class'], 'REPORTED_NO_SPACE_LEFT_ON_DEVICE')
                self.assertEqual(observations[0]['clock_basis'], 'Unknown')

    def test_withheld_no_fd_guess_and_window_accounting(self):
        bodies = ['write(3</tmp/a>, "x", 1 <unfinished ...>', '<... write resumed> ) = -1 ENOSPC (No space left on device)',
                  'fsync(3) = -1 ENOSPC (No space left on device)', 'fsync(3<relative>) = -1 ENOSPC (No space left on device)',
                  'fsync(3</tmp/b>) = -1 EDQUOT (Disk quota exceeded)', 'fsync(3</tmp/c>) = 0']
        text = ''.join(f'1700000000.00000{i} {body}\n' for i, body in enumerate(bodies))
        with tempfile.TemporaryDirectory() as directory:
            rendered, report = self.convert(directory, text, start_record=1, stop_record=5)
            rows = list(csv.DictReader(io.StringIO(rendered)))
            self.assertEqual((report['source_records'], report['selected_records'], report['excluded_records_not_decoded']), (6,4,2))
            self.assertEqual((report['withheld'], report['qualified'], report['enospc']), (3,1,0))
            self.assertEqual(rows[0]['source_record_index'], '1')
            self.assertEqual(int(rows[0]['source_byte_offset']), len(text.splitlines(keepends=True)[0]))
            self.assertEqual(rows[-1]['error_class'], 'REPORTED_OTHER_ERRNO_UNINTERPRETED')

    def test_refusals(self):
        for text in ['not a timestamp\n', '1.1234567 fsync(3</a>) = 0\n',
                     '1.000001 write(3</a>, "x") = -1 ENOSPC (x)\n',
                     '1.000001 fsync(3</a>) = 0 ENOSPC (x)\n',
                     '2.000001 fsync(3</a>) = 0\n1.000001 fsync(3</a>) = 0\n']:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.convert(directory, text)
        for options in [dict(source_commit='unqualified'), dict(process_id=0), dict(session='')]:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.convert(directory, '1.000001 fsync(3</a>) = 0\n', **options)


if __name__ == '__main__':
    unittest.main()
