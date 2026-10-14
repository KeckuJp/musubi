"""Authored long saved solutions; no solver run or measured accuracy."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tests.test_rtklib_solution import document, EPOCH, PROFILE


class RtklibWindowTests(unittest.TestCase):
    def test_long_file_windows_and_separate_input_reuse(self):
        from scripts.convert_rtklib_solution_window import convert_path
        reader=os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader,'required actual common reader')
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); source=base/'long.pos'
            header=document(rows=())
            record=(EPOCH+'\r\n').encode()
            total=240000
            with source.open('wb') as stream:
                stream.write(header)
                for _ in range(total): stream.write(record)
            self.assertGreater(source.stat().st_size,32*1024**2)
            for start,stop in ((0,2),(total-2,total)):
                output,report=convert_path(source,start_record=start,stop_record=stop)
                rows=list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([int(r['source_record_index']) for r in rows],[start,start+1])
                self.assertEqual(int(rows[0]['source_record_byte_offset']),len(header)+start*len(record))
                self.assertEqual(int(rows[0]['source_line']),len(header.splitlines())+start+1)
                self.assertEqual((report['window']['source_epochs'],report['window']['selected_epochs'],report['window']['excluded_epochs']),(total,2,total-2))
                path=base/'rows.csv'; path.write_text(output)
                run=subprocess.run([reader,str(PROFILE),str(path),'--allow-equal-time'],capture_output=True,text=True)
                self.assertEqual(run.returncode,0,run.stderr)
                observations=json.loads(run.stdout)['observations']
                self.assertEqual(observations[-1]['fields']['solution_quality_code'],1)
                self.assertEqual(observations[-1]['fields']['height_m'],40.1234)
            # A separately authored shorter file is completely covered by adjacent windows.
            raw=document(times='UTC',rows=(EPOCH,EPOCH.replace('03:04:05.000','03:04:06.000'),EPOCH.replace('03:04:05.000','03:04:07.000')))
            source.write_bytes(raw)
            expected=hashlib.sha256(raw).hexdigest()
            all_rows=[]
            for start,stop in ((0,1),(1,3)):
                output,report=convert_path(source,start_record=start,stop_record=stop,
                    expected_size=len(raw),expected_sha256=expected)
                all_rows.extend(csv.DictReader(io.StringIO(output)))
                path=base/'confirmation.csv'; path.write_text(output)
                run=subprocess.run([reader,str(PROFILE),str(path),'--allow-equal-time'],capture_output=True,text=True)
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertEqual(len(json.loads(run.stdout)['observations']),stop-start)
            self.assertEqual([r['source_record_index'] for r in all_rows],['0','1','2'])
            self.assertTrue(all(r['source_sha256']==expected for r in all_rows))

    def test_bad_bound_header_and_selected_semantics(self):
        from scripts.convert_rtklib_solution_window import convert_path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'input.pos'
            for raw,start,stop in ((document(rows=(EPOCH,'% later declaration',EPOCH)),0,1),
                                   (document(),0,2),(document(rows=('invalid',)),0,1)):
                path.write_bytes(raw)
                with self.assertRaises(ValueError): convert_path(path,start_record=start,stop_record=stop)
            path.write_bytes(document(rows=(EPOCH,'invalid')))
            _,report=convert_path(path,start_record=0,stop_record=1)
            self.assertEqual(report['window']['excluded_epochs'],1)
            with self.assertRaises(ValueError): convert_path(path,start_record=0,stop_record=1,expected_size=path.stat().st_size,expected_sha256='0'*64)

    def test_velocity_cli_reuses_existing_semantics(self):
        from scripts.convert_rtklib_solution import VELOCITY_COLUMNS, VELOCITY_FIELDS, convert
        from scripts.convert_rtklib_solution_window import convert_path
        from tests.test_rtklib_solution import COLUMN_LINE
        data=document(times='UTC',columns=(COLUMN_LINE%'UTC')+' '+' '.join(VELOCITY_COLUMNS),
                      rows=(EPOCH+' 1 2 3 .1 .2 .3 -.1 -.2 -.3',))
        expected,_=convert(data,reported_velocity=True)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); source=base/'input.pos'; source.write_bytes(data)
            output,report=convert_path(source,start_record=0,stop_record=1,reported_velocity=True)
            row=next(csv.DictReader(io.StringIO(output)))
            old=next(csv.DictReader(io.StringIO(expected)))
            for field in old: self.assertEqual(row[field],old[field])
            run=subprocess.run([sys.executable,'-m','scripts.convert_rtklib_solution_window',str(source),str(base/'out'),
                '--start-record','0','--stop-record','1','--reported-velocity'],
                cwd=PROFILE.parents[3],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual((base/'out'/'observations.csv').read_text(),output)

if __name__=='__main__': unittest.main()
