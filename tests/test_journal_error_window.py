"""Authored saved journal windows, not access to this host's journal."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from tests import test_journal_storage_errors as existing
from scripts.convert_journal_error_window import convert_path
from scripts.convert_vda_load_report import read_jsonl_window

errno_entry, entry, RECEPTION_PROFILE = existing.errno_entry, existing.entry, existing.RECEPTION_PROFILE


class JournalWindowTests(unittest.TestCase):
    def test_blank_lines_keep_original_positions_and_default_refusal(self):
        data = ('\r\n'+errno_entry(1,5)+'\r\n  \r\n'+entry(2)+'\r\n'+errno_entry(3,30)+'\r\n').encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'journal.jsonl'; path.write_bytes(data)
            output, report, paired, _ = convert_path(path,start_record=0,stop_record=5,
                clock='journal_reception_realtime')
            rows = list(csv.DictReader(io.StringIO(output)))
            records = [json.loads(line) for line in paired.splitlines()]
            self.assertEqual([r['source_record_index'] for r in rows], ['2','5'])
            self.assertEqual([r['entry_number'] for r in records[1:]], [2,5])
            self.assertEqual(rows[0]['source_record_byte_offset'], '2')
            self.assertEqual(report['selected_blank_lines'],2)
            self.assertEqual(report['selected_semantics']['entries']+report['selected_blank_lines'],5)
            self.assertEqual(report['selected_semantics']['entries_without_selector'],1)
            self.assertEqual(report['source_sha256'],'sha256:'+hashlib.sha256(data).hexdigest())
            for options in ({}, {'allow_blank':'true'}):
                with self.assertRaises(ValueError):
                    read_jsonl_window(path,start_record=1,stop_record=2,**options)
            with self.assertRaises(ValueError):
                convert_path(path,start_record=0,stop_record=1,clock='journal_reception_realtime')

    def test_large_original_paired_global_provenance_and_reuse(self):
        prefix = ('{"unknown":"' + 'x'*1024 + '"}\r\n')*17000
        records = [errno_entry(1,28, unknown={"a":[1,2]}), entry(2), errno_entry(3,5)]
        data = (prefix+'\r\n'.join(records)+'\r\n').encode()
        self.assertGreater(len(data), 16*1024**2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'journal.jsonl'; path.write_bytes(data)
            output, report, paired, selected = convert_path(path, start_record=17000,
                stop_record=17003, clock='journal_reception_realtime')
            rows = list(csv.DictReader(io.StringIO(output)))
            evidence = [json.loads(line) for line in paired.splitlines()]
            sha = 'sha256:'+hashlib.sha256(data).hexdigest()
            self.assertEqual([r['source_record_index'] for r in rows], ['17001','17003'])
            self.assertEqual([r['entry_number'] for r in evidence[1:]], [17001,17003])
            self.assertEqual(rows[0]['source_record_byte_offset'], str(len(prefix.encode())))
            for row, record in zip(rows, evidence[1:]):
                self.assertEqual(row['source_file_sha256'], sha)
                self.assertEqual(record['source_file_sha256'], sha)
                self.assertEqual(row['record_sha256'], record['record_sha256'])
            self.assertEqual(evidence[0]['source_sha256'], sha)
            self.assertEqual(evidence[1]['preserved_fields']['unknown'], {'a':[1,2]})
            self.assertNotIn('MESSAGE', evidence[1]['preserved_fields'])
            self.assertEqual(report['selected_semantics']['entries_without_selector'],1)
            self.assertEqual(report['selected_semantics']['absent_source_time_entries'],2)
            self.assertEqual((report['source_physical_lines'], report['selected_physical_lines'],
                              report['outside_physical_lines']), (17003,3,17000))
            self.assertEqual(existing.JournalStorageErrorTests().observations(output, RECEPTION_PROFILE)['main_rows'],2)
            other = (errno_entry(4,122)+'\n'+errno_entry(5,30)+'\n').encode()
            path.write_bytes(other)
            output, report, paired, _ = convert_path(path, start_record=1, stop_record=2,
                clock='journal_reception_realtime')
            self.assertEqual(list(csv.DictReader(io.StringIO(output)))[0]['reported_errno'],'30')
            self.assertEqual(existing.JournalStorageErrorTests().observations(output, RECEPTION_PROFILE)['main_rows'],1)

    def test_selected_missing_invalid_and_bounds_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'journal.jsonl'
            for data, clock in [(errno_entry(1,5), 'source_realtime'), ('{bad}', 'journal_reception_realtime'),
                                (entry(1), 'journal_reception_realtime'), ('\n', 'journal_reception_realtime')]:
                path.write_text(data+'\n')
                with self.assertRaises(ValueError):
                    convert_path(path,start_record=0,stop_record=1,clock=clock)
            path.write_text(errno_entry(1,5)+'\n')
            with self.assertRaises(ValueError):
                convert_path(path,start_record=0,stop_record=2,clock='journal_reception_realtime')
