"""Authored audit files through existing required common-reader stage."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_k8s_audit_events import event, PROFILE
from scripts.convert_audit_record_window import convert_path


class AuditWindowTests(unittest.TestCase):
    def test_large_saved_file_and_second_input_reuse_common_without_global_completion(self):
        self.assertIn('MUSUBI_TELEMETRY_READER', os.environ)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for large in (True,False):
                prefix=(event('outside','RequestReceived', unselected='x'*200000)+'\r\n').encode()
                before=prefix*90 if large else prefix
                selected=(event('shared','RequestReceived')+'\r\n').encode()
                after=(event('shared','ResponseComplete',response={'code':200})+'\r\n').encode()
                raw=before+selected+after
                source=root/'source.jsonl';source.write_bytes(raw)
                start=90 if large else 1
                if large:self.assertGreater(len(raw),16*1024*1024)
                output,report=convert_path(source,start_record=start,stop_record=start+1,time_field='stage_timestamp')
                row=next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(int(row['source_record_index']),start+1)
                self.assertEqual(int(row['source_record_byte_offset']),len(before))
                self.assertEqual(row['source_file_sha256'],'sha256:'+hashlib.sha256(raw).hexdigest())
                self.assertEqual(row['record_sha256'],'sha256:'+hashlib.sha256(selected[:-2]).hexdigest())
                self.assertEqual(report['selected_semantics']['audit_ids_without_response_complete'],1)
                self.assertEqual(report['excluded_records_not_decoded'],start+1)
                self.assertIn('SELECTED_WINDOW_ONLY',row['audit_stage_accounting_scope'])
                path=root/'rows.csv';path.write_text(output)
                common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],str(PROFILE),str(path),'--allow-equal-time'],check=True,capture_output=True,text=True).stdout)
                self.assertEqual(common['main_rows'],1)
                fields=common['observations'][0]['fields']
                self.assertEqual(fields['audit_stage'],'RequestReceived')
                self.assertEqual(fields['source_record_index'],start+1)
                self.assertEqual(fields['audit_id_has_response_complete'],'NO_RESPONSE_COMPLETE_IN_SELECTED_WINDOW')
                completed,complete_report=convert_path(source,start_record=start,stop_record=start+2,time_field='stage_timestamp')
                self.assertEqual(complete_report['selected_semantics']['audit_ids_without_response_complete'],0)
                self.assertTrue(all(r['audit_id_has_response_complete']=='YES' for r in csv.DictReader(io.StringIO(completed))))

    def test_selected_corruption_and_framing_boundary_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source'
            good=event('a','RequestReceived')+'\n'
            source.write_text(good+'{broken\n')
            convert_path(source,start_record=0,stop_record=1,time_field='stage_timestamp')
            with self.assertRaises(ValueError):convert_path(source,start_record=1,stop_record=2,time_field='stage_timestamp')
            source.write_text(good+'\n')
            with self.assertRaises(ValueError):convert_path(source,start_record=0,stop_record=1,time_field='stage_timestamp')
            source.write_text(good)
            with self.assertRaises(ValueError):convert_path(source,start_record=0,stop_record=2,time_field='stage_timestamp')
