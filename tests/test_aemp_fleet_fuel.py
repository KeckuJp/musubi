"""Authored fixed-schema fuel reports; not equipment/API evidence."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_opcua_recorded import convert_aemp_hours, FLEET_HOURS_FORMAT

NS = 'http://www.jcmanet.or.jp/english2017/ISO/15143/-3/20190501'
STAMP = '2026-10-12T01:02:03Z'
PROFILE = Path(__file__).resolve().parents[1]/'profiles/declared/opcua-recorded/profile.toml'

def fleet(body):
    return f'<Fleet xmlns="{NS}" version="1" snapshotTime="{STAMP}"><Links><rel>self</rel><href>saved-only</href></Links>{body}</Fleet>'

def equipment(body, identity='A'):
    return f'<Equipment><EquipmentHeader><EquipmentID>{identity}</EquipmentID></EquipmentHeader>{body}</Equipment>'

def fuel(used='100', percent='0'):
    return f'<FuelUsed datetime="{STAMP}"><FuelUnits>litre</FuelUnits><FuelConsumed>{used}</FuelConsumed><vendor>x</vendor></FuelUsed><FuelUsedLast24 datetime="2026-10-11T01:02:03Z"><FuelUnits>litre</FuelUnits><FuelConsumed>8</FuelConsumed></FuelUsedLast24><FuelRemaining datetime="{STAMP}"><Percent>{percent}</Percent><FuelTankCapacityUnits>litre</FuelTankCapacityUnits><FuelTankCapacity>300</FuelTankCapacity></FuelRemaining>'

class FleetFuelTests(unittest.TestCase):
    def convert(self, text, **kwargs):
        return convert_aemp_hours(text, 'declared-fleet', 42, FLEET_HOURS_FORMAT, **kwargs)

    def test_quantities_common_and_reuse(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader)
        second = f'<FuelRemaining datetime="{STAMP}"><Percent>25.5</Percent></FuelRemaining>'
        for text, count, fraction in [(fleet(equipment(fuel())+equipment('', 'B')),2,0),
                                     (fleet(equipment(second,'C')),1,.255)]:
            old, old_report = self.convert(text)
            output, report = self.convert(text, include_fuel=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(rows[0]['fuel_remaining_fraction_reported']),fraction)
            self.assertEqual(bytes.fromhex(rows[0]['source_record_hex'][4:]).decode(),text)
            self.assertEqual(rows[0]['record_time_us'],'42')
            self.assertNotEqual(rows[0]['fuel_remaining_measurement_time_us'],'42')
            for before, after in zip(csv.DictReader(io.StringIO(old)),rows):
                self.assertEqual(before,{k:after[k] for k in before})
            if count == 2:
                self.assertEqual(rows[0]['fuel_used_litre_reported'],'100')
                self.assertEqual(rows[0]['fuel_used_last24_litre_reported'],'8')
                self.assertNotEqual(rows[0]['fuel_used_measurement_time_us'],rows[0]['fuel_used_last24_measurement_time_us'])
                self.assertEqual(rows[1]['fuel_remaining_fraction_reported'],'')
                self.assertEqual(report['fuel_unknown_elements'],1)
                self.assertEqual(report['unselected_metric_elements'],0)
            with tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'fuel.csv';source.write_text(output)
                common=json.loads(subprocess.run([reader,str(PROFILE),str(source),'--allow-equal-time'],check=True,capture_output=True).stdout)
                self.assertEqual(common['main_rows'],count)
                self.assertEqual(common['observations'][0]['fields']['fuel_remaining_fraction_reported'],fraction)

    def test_refusals_and_absence(self):
        good=fleet(equipment(fuel()))
        bad=[good.replace('litre','gallon'),good.replace('<Percent>0','<Percent>101'),
             good.replace('<FuelConsumed>100','<FuelConsumed>-1'),
             good.replace('<FuelConsumed>100','<FuelConsumed>1<x/>'),
             good.replace('</FuelUsed>','</FuelUsed>'+fuel().split('<FuelUsedLast24')[0]),
             good.replace('2026-10-12T01:02:03Z','2026-10-12T01:02:03'),
             good.replace('<FuelTankCapacityUnits>litre</FuelTankCapacityUnits>','')]
        for text in bad:
            with self.subTest(text=text),self.assertRaises((ValueError,KeyError)):
                self.convert(text,include_fuel=True)
        with self.assertRaises(ValueError):self.convert(good,include_fuel='yes')
        output,report=self.convert(fleet(equipment('')),include_fuel=True)
        row=next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row['fuel_used_disposition'],'NOT_REPORTED_NOT_ZERO')
        self.assertEqual(report['fuel_selected_records'],0)

if __name__ == '__main__':unittest.main()
