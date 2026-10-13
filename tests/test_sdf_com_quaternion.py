"""Declared inertial local CoM orientation, not physical inertia or world pose."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_sdf_airframe import convert

ROOT = Path(__file__).resolve().parents[1]


def model(q, attributes='rotation_format="quat_xyzw"'):
    return (f'<sdf version="1.11"><model name="m"><link name="body"><inertial>'
        f'<mass>2</mass><pose {attributes}>100 0 20 {q}</pose></inertial></link>'
        '<link name="missing"/></model></sdf>').encode()


def selected(raw, **kwargs):
    return convert(raw, 123, mass_unit='kg', position_unit='cm',
                   unit_basis='operator-declared', com_quaternion=True, **kwargs)


class ComQuaternionTests(unittest.TestCase):
    def test_two_models_actual_common_and_source(self):
        for q, expected in (('0 0 0.707107 0.707107', (0,0,2**-.5,2**-.5)),
                            ('-1e308 1e308 1e308 -1e308', (-.5,.5,.5,-.5))):
            raw = model(q)
            output, report = selected(raw)
            self.assertEqual(bytes.fromhex(report['source_xml_hex'][4:]), raw)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'out.csv'; path.write_text(output)
                observations = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT/'profiles/declared/sdf-airframe/profile.toml'),
                    str(path),'--allow-equal-time'], check=True,capture_output=True).stdout)['observations']
            links = [o for o in observations if o['fields']['airframe_record_kind']=='LINK']
            self.assertEqual(len(links), 2)
            fields = links[0]['fields']
            self.assertEqual(fields['airframe_mass_kg'], 2)
            self.assertEqual(fields['airframe_com_x_m'], 1)
            self.assertEqual(fields['airframe_com_declared_rotation_format'], 'quat_xyzw')
            for axis,value in zip('xyzw',expected):
                self.assertAlmostEqual(fields['airframe_com_q'+axis], value)
            self.assertIn(q, bytes.fromhex(fields['airframe_com_pose_xml_hex'][4:]).decode())
            self.assertEqual(links[0]['clock_basis'],'Unknown')
            self.assertIsNone(links[1]['fields']['airframe_com_qw'])

    def test_refusals_and_euler_default_unchanged(self):
        for q in ('0 0 0 0','0 0 NaN 1','0 0 1e999 1','0 0 1'):
            with self.assertRaises(ValueError): selected(model(q))
        for attrs in ('rotation_format="quat_xyzw" relative_to="other"',
                      'rotation_format="quat_xyzw" degrees="true"'):
            with self.assertRaises(ValueError): selected(model('0 0 0 1',attrs))
        with self.assertRaises(ValueError): selected(model('0 0 0 1').replace(b'1.11',b'1.5'))
        with self.assertRaises(ValueError): convert(model('0 0 0 1'),123,com_quaternion=True)
        with self.assertRaises(ValueError): convert(model('0 0 0 1'),123)
        euler=model('0 0 0','')
        plain,report=convert(euler,123)
        again,again_report=convert(euler,123,com_quaternion=False)
        self.assertEqual((plain,report),(again,again_report))
        opted,_=selected(euler)
        row=list(csv.DictReader(io.StringIO(opted)))[1]
        self.assertEqual(row['airframe_com_qw'],'')


if __name__=='__main__': unittest.main()
