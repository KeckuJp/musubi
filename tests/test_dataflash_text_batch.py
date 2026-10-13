"""Fixed exporter authored text, not actual log evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_dataflash_text import ARRAY_BASIS, convert
from scripts import convert_ardupilot_imu_batch as batch
from tests.test_ardupilot_imu_batch import declaration


def capture(sensor=0, value=100, count=32):
    lines = ['FMT, 128, 89, FMT, BBnNZ, Type,Length,Name,Format,Columns',
        'FMT, 140, 31, ISBH, QHBBHHQf, TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate',
        'FMT, 141, 207, ISBD, QHHaaa, TimeUS,N,seqno,x,y,z',
        'FMT, 150, 4, FUTR, B, Novel', 'FUTR, 7',
        f'ISBH, 100000, 7, {sensor}, 2, 100, {count}, 90000, 1000']
    for i in range(count // 32):
        arrays = ['[' + ' '.join([str(v)] * 32) + ']' for v in (value, -value, 2*value)]
        lines.append(f'ISBD, {101000+i}, 7, {i}, ' + ', '.join(arrays))
    return ('\r\n'.join(lines)+'\r\n').encode()


class TextBatch(unittest.TestCase):
    def test_cli_selected_text_report(self):
        raw=capture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input.log';source.write_bytes(raw)
            config=root/'config.json';config.write_text(json.dumps(declaration(raw)))
            output=root/'new-output'
            subprocess.run([sys.executable,str(batch.ROOT/'scripts/convert_ardupilot_imu_batch.py'),
                str(source),str(config),str(output),'--text-basis',ARRAY_BASIS],check=True,capture_output=True)
            report=json.loads((output/'report.json').read_text())
            self.assertEqual(report['samples'],32)
            self.assertEqual(report['text_exporter_basis'],ARRAY_BASIS)
            self.assertEqual(bytes.fromhex(report['original_text_hex'][4:]),raw)
            self.assertTrue((output/'observations.csv').read_text().startswith('record_time_us,'))

    def test_two_inputs_meaning_and_original_accounting(self):
        for sensor, value, count in ((0, 100, 32), (1, 250, 64)):
            raw = capture(sensor, value, count)
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'input.log';path.write_bytes(raw)
                text, report=batch.convert(path,json.dumps(declaration(raw)).encode(),text_basis=ARRAY_BASIS)
                out=Path(directory)/'out.csv';out.write_text(text)
                common=json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(batch.PROFILE),str(out),'--allow-equal-time'],check=True,capture_output=True).stdout)
                self.assertEqual(len(common['observations']),count)
                field='acceleration_x_m_s2' if sensor==0 else 'angular_rate_x_rad_s'
                fields=common['observations'][-1]['fields']
                self.assertEqual(fields[field],value/100)
                self.assertEqual(report['source_format'],'mission-planner-text')
                self.assertEqual(bytes.fromhex(report['original_text_hex'][4:]),raw)
                self.assertEqual(report['unselected_counts']['FUTR'],1)
                start=int(fields['source_offset']);length=int(fields['source_length'])
                self.assertTrue(raw[start:start+length].startswith(b'ISBD, '))
                self.assertEqual(report['total_records'],len(raw.splitlines()))

    def test_lexical_schema_range_and_default_refusals(self):
        raw=capture()
        with self.assertRaises(ValueError):convert(raw)
        first='['+' '.join(['100']*32)+']'
        for token in ('['+' '.join(['100']*31)+']',first.replace('100','1.5',1),
                      first.replace('100','32768',1),first.replace('100','NaN',1),first[:-1]):
            with self.assertRaises(ValueError):convert(raw.replace(first.encode(),token.encode()),array_basis=ARRAY_BASIS)
        for bad in (raw.replace(b'QHHaaa',b'QHHaaB'),raw.replace(b'seqno,x,y,z',b'seqno,x,y,z,Extra'),
                    raw.replace(b'ISBD, 101000, 7, 0,',b'ISBD, 101000, 7, 1,')):
            with tempfile.TemporaryDirectory() as directory:
                self.assertNotEqual(bad,raw)
                path=Path(directory)/'bad.log';path.write_bytes(bad)
                with self.assertRaises(ValueError):batch.convert(path,json.dumps(declaration(bad)).encode(),text_basis=ARRAY_BASIS)
        with self.assertRaises(ValueError):convert(raw,array_basis='unknown')


if __name__=='__main__':unittest.main()
