import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_farmduino_reports import PIN, convert
from scripts.convert_ardupilot_battery_csv import FARM_FIELDS, farmduino_position

ROOT=Path(__file__).resolve().parents[1]
def record(text,time=1):
    return dict(firmware_commit=PIN,capture_time_us=time,response=text,steps_per_mm=dict(x=80,y=80,z=400))
def encoded(records):return b''.join(json.dumps(r).encode()+b'\n' for r in records)

class FarmduinoReportsTests(unittest.TestCase):
    def test_two_saved_groups_common_and_accounting(self):
        reader=os.environ.get('MUSUBI_TELEMETRY_READER');self.assertTrue(reader)
        first=[record(x) for x in ['R01 Q4','R02 Q4','R03 V2 Q4','R07 Q4','R09 Q4','R87 Q4','R88 Q4',
             'R81 XA1 XB0 YA0 YB1 ZA1 ZB0 Q4','R82 X1000.00 Y-250.50 Z0.00 Q4',
             'R21 P2 V1 Q4','R21 P55 V80 Q4','R21 P71 V400 Q4','R99 diagnostic text']]
        second=[record(x,2) for x in ['R81 XA0 XB1 YA1 YB0 ZA0 ZB1 Q-2','R21 P2 V0 Q-2','R21 P57 V0 Q-2']]
        with tempfile.TemporaryDirectory() as d:
            for index,records in enumerate((first,second)):
                raw=encoded(records);output,report=convert(raw)
                path=Path(d)/f'{index}.csv';path.write_text(output)
                process=subprocess.run([reader,str(ROOT/'profiles/declared/electrical-schema-reuse/json-profile.toml'),str(path),'--allow-equal-time'],capture_output=True)
                self.assertEqual(process.returncode,0,process.stderr.decode())
                common=json.loads(process.stdout);fields=[x['fields'] for x in common['observations']]
                self.assertEqual(len(fields),len(records));self.assertEqual(report['records'],len(records))
                self.assertEqual(report['qualified']+report['unqualified']+report['partial_parameter'],len(records))
                for n,row in enumerate(fields):
                    self.assertEqual(row['source_record_index'],n)
                    self.assertEqual(bytes.fromhex(row['source_record_hex'][4:]),raw.splitlines(keepends=True)[n])
                if index==0:
                    self.assertEqual(fields[2]['error_code_reported'],2)
                    self.assertEqual(fields[7]['endstop_XA'],'true')
                    self.assertEqual(fields[8]['farm_position_x_m'],1)
                    self.assertEqual(fields[9]['configuration_ready_reported'],'true')
                    self.assertEqual(fields[10]['parameter_reported_steps_per_mm'],80)
                    self.assertEqual((report['partial_parameter'],report['unqualified']),(1,1))
                    old=farmduino_position(records[8]);parsed=list(csv.DictReader(io.StringIO(output)))[8]
                    self.assertEqual([parsed[k] for k in FARM_FIELDS],[str(x) if x is not None else '' for x in old[:-2]])
                else:
                    self.assertEqual(fields[1]['configuration_ready_reported'],'false')
                    self.assertEqual(fields[2]['configuration_value_basis'],'NONPOSITIVE_REPORTED_SCALE_NOT_USABLE_FOR_POSITION')
    def test_important_refusals(self):
        for text in ['R81 XA2 XB0 YA0 YB0 ZA0 ZB0 Q0','R03 Q0','R21 P2 V1.0 Q0','R01 Q2147483648','R01 Q0\nR02 Q0']:
            with self.assertRaises(ValueError):convert(encoded([record(text)]))
        bad=record('R01 Q0');bad['firmware_commit']='other'
        with self.assertRaises(ValueError):convert(encoded([bad]))
        with self.assertRaises(ValueError):convert(b'{"a":1,"a":2}\n')
