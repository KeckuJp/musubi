import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_mavsdk_telemetry_recorded import convert_path, PIN
from tests.test_mavsdk_battery import varint

ROOT = Path(__file__).resolve().parents[1]


def envelope(payload, *, nested=True):
    if nested:
        payload = b'\x0a'+varint(len(payload))+payload
    return dict(sdk_version="3.17.4", proto_commit=PIN, capture_time_us=123,
                source_id="caller-one", grpc_frame_hex=(b'\0'+len(payload).to_bytes(4,'big')+payload).hex())


class BasicResponses(unittest.TestCase):
    def common(self, folder, output):
        path = folder/'output.csv'
        path.write_text(output)
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'actual postbuild reader required')
        result = subprocess.run([reader, str(ROOT/'profiles/declared/mavsdk-basic/profile.toml'), str(path), '--allow-equal-time'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['observations']

    def test_large_position_and_independent_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); path=folder/'input.jsonl'
            payload=b'\x09'+struct.pack('<d',35.1)+b'\x11'+struct.pack('<d',139.2)+b'\x1d'+struct.pack('<f',100)+b'\x25'+struct.pack('<f',7)
            line=json.dumps(envelope(payload))+'\n'
            # Keep under the existing 5M-record cap while exceeding16MiB.
            prefix=((' '+(' '*32)+'{}\n')*500000).encode()
            raw=prefix+line.encode(); self.assertGreater(len(raw),16*1024**2)
            path.write_bytes(raw)
            out,report=convert_path(path,kind='position',start_record=500000,stop_record=500001)
            row=self.common(folder,out)[0]
            self.assertEqual(row['clock_basis'],'Unknown')
            self.assertEqual(row['fields']['altitude_amsl_m'],100)
            self.assertEqual(row['fields']['altitude_takeoff_relative_m'],7)
            self.assertEqual(row['fields']['source_record_index'],500000)
            self.assertEqual(report['source_sha256'],'sha256:'+hashlib.sha256(raw).hexdigest())
            self.assertEqual(report['excluded_records'],500000)
            other=envelope(b'\x09'+struct.pack('<d',-20)); other['source_id']='caller-two'
            path.write_text(json.dumps(other)+'\n')
            out,_=convert_path(path,kind='position',start_record=0,stop_record=1)
            row=self.common(folder,out)[0]
            self.assertEqual(row['fields']['latitude_deg'],-20)
            self.assertIn('longitude_deg',bytes.fromhex(row['fields']['proto3_absent_or_zero_fields_hex'][4:]).decode())

    def test_states_health_old_routes_and_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); path=folder/'input.jsonl'
            for kind,payload,nested,key,expected in [
                ('flight_mode',b'\x08\x63',False,'reported_flight_mode','UNKNOWN_ENUM_99'),
                ('landed_state',b'\x08\x02',False,'reported_landed_state','IN_AIR'),
                ('armed',b'\x08\x01',False,'reported_armed','TRUE_REPORTED'),
                ('in_air',b'',False,'reported_in_air','FALSE_OR_ABSENT_REPORTED'),
                ('health',b'\x08\x01\x20\x01',True,'reported_gyrometer_calibration_ok','TRUE_REPORTED'),
                ('battery',b'\x1d'+struct.pack('<f',12),True,'battery_voltage_v',12),
                ('attitude',b'\x0d'+struct.pack('<f',90),True,'attitude_roll_rad',math.pi/2),
                ('velocity',b'\x0d'+struct.pack('<f',2),True,'reported_velocity_north_m_s',2)]:
                record=envelope(payload,nested=nested)
                path.write_text(json.dumps(record)+'\n')
                out,_=convert_path(path,kind=kind,start_record=0,stop_record=1)
                row=self.common(folder,out)[0]
                if expected is not None: self.assertEqual(row['fields'][key],expected)
                if kind=='health': self.assertEqual(row['fields']['unknown_wire_fields'],1)
            good=envelope(b'\x09'+struct.pack('<d',35))
            for changes in ({'sdk_version':'other'},{'proto_commit':'wrong'},{'source_id':''},{'capture_time_us':True},{'grpc_frame_hex':'ff'}):
                record=dict(good,**changes); path.write_text(json.dumps(record)+'\n')
                with self.assertRaises(ValueError): convert_path(path,kind='position',start_record=0,stop_record=1)
            path.write_text(json.dumps(envelope(b'\x09'+struct.pack('<d',91)))+'\n')
            with self.assertRaises(ValueError): convert_path(path,kind='position',start_record=0,stop_record=1)
            path.write_text(json.dumps(envelope(b'\x09\x00'))+'\n')
            with self.assertRaises(ValueError): convert_path(path,kind='position',start_record=0,stop_record=1)
            path.write_text(json.dumps(envelope(b'\x09'+struct.pack('<d',float('nan'))))+'\n')
            out,_=convert_path(path,kind='position',start_record=0,stop_record=1)
            self.assertIsNone(self.common(folder,out)[0]['fields']['latitude_deg'])


if __name__ == '__main__': unittest.main()
