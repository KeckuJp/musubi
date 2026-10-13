"""Saved native controller capture composition, using adopted fixtures/decoders."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.convert_candump_recorded import convert_path
from scripts.convert_dronecan_capture import convert
from tests.saved_can_window_helpers import native, common
from tests.test_dronecan_capture import NODE_CASES, node_info, node_status, esc_status, service_frames, message_frames


def capture(case):
    info = service_frames(node_info(hardware=case['hardware'], software=case['software'],
        flags=case['flags'], unique=case['unique'], name=case['name'], coa=case['coa'],
        commit=case['commit'], crc=case['crc']), source=case['source'], destination=case['destination'], transfer_id=5)
    status = message_frames(node_status(*case['status']), source=case['source'], transfer_id=1)
    motor = message_frames(esc_status(case['esc_index'], case['rpm']), source=case['source'], transfer_id=2)
    return list(status)+list(motor)+list(info), len(info)


class DroneCANWindow(unittest.TestCase):
    def test_large_native_and_other_node_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); path=folder/'capture.log'
            for i,case in enumerate(NODE_CASES[:2]):
                frames,_=capture(case)
                selected=native(''.join(f'1,{f.message_id:x},{f.bytes.hex()}\n' for f in frames)).encode()
                prefix=b'(1700000000.000000) can1 09f20110#0000000000000000\n'*350000 if i==0 else b''
                raw=prefix+selected; path.write_bytes(raw)
                if i==0: self.assertGreater(len(raw),16*1024**2)
                start=350000 if i==0 else 0
                csv,window=convert_path(path,'can0',start_line=start,stop_line=start+len(frames),extended=True)
                output,report=convert(csv)
                self.assertEqual(report['decoded_transfers'],3)
                observations=common(self,folder,output)['observations']
                self.assertEqual(len(observations),3)
                self.assertEqual({row['fields']['source_node_id'] for row in observations},{case['source']})
                self.assertEqual({row['clock_basis'] for row in observations},{'Unknown'})
                self.assertEqual(window['source_window']['source_sha256'],'sha256:'+hashlib.sha256(raw).hexdigest())
                self.assertEqual(window['records'][0]['original_line'],start+1)
                self.assertEqual(window['records'][0]['original_byte_offset'],len(prefix))
                self.assertEqual(window['source_window']['excluded_physical_lines'],start)
                self.assertEqual(observations[-1]['fields']['destination_node_id'],case['destination'])
                self.assertEqual(observations[1]['fields']['reported_rpm'],case['rpm'])
                self.assertEqual(observations[-1]['fields']['node_hardware_major_reported'],case['hardware'][0])
                self.assertEqual(observations[-1]['fields']['node_hardware_unique_id_hex'],case['expect']['uid'])

    def test_selected_service_cut_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.log'
            frames,ninfo=capture(NODE_CASES[0]); frames=frames[-ninfo:]
            path.write_text(native(''.join(f'1,{f.message_id:x},{f.bytes.hex()}\n' for f in frames)))
            for start,stop in ((0,len(frames)-1),(1,len(frames))):
                csv,_=convert_path(path,'can0',start_line=start,stop_line=stop,extended=True)
                with self.assertRaises(ValueError): convert(csv)


if __name__=='__main__': unittest.main()
