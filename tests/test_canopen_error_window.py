"""Existing native window and qualified consumer reports, no new decoder."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.convert_candump_recorded import convert_path
from tests.saved_can_window_helpers import native, common
from tests.test_canopen_consumer import BASE, HEADER, PROFILE, convert


class ConsumerWindow(unittest.TestCase):
    def test_large_original_and_second_report_set_common(self):
        cases = [BASE, HEADER + '1,082,3081101c07000000\n1,082,0000001c00000000\n1,082,308110aa78563412\n']
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); path=folder/'capture.log'
            for index, data in enumerate(cases):
                selected=native('\n'.join(data.splitlines()[1:])).encode()
                prefix=b'(1700000000.000000) can1 09f20110#0000000000000000\n'*350000 if index==0 else b''
                raw=prefix+selected; path.write_bytes(raw)
                if index==0: self.assertGreater(len(raw),16*1024**2)
                start=350000 if index==0 else 0
                csv,window=convert_path(path,'can0',start_line=start,stop_line=start+len(data.splitlines())-1)
                output,report=convert(csv)
                observations=common(self,folder,output,PROFILE)['observations']
                self.assertEqual(len(observations),3)
                self.assertEqual({r['clock_basis'] for r in observations},{'Unknown'})
                self.assertEqual(window['source_window']['source_sha256'],'sha256:'+hashlib.sha256(raw).hexdigest())
                self.assertEqual(window['records'][0]['original_line'],start+1)
                self.assertEqual(window['records'][0]['original_byte_offset'],len(prefix))
                self.assertEqual(window['source_window']['excluded_physical_lines'],start)
                self.assertEqual(report['frames'],report['decoded_frames']+len(report['unsupported_frames']))
                fields=[r['fields'] for r in observations]
                self.assertEqual(fields[0]['canopen_monitored_slot'],2 if index==0 else 7)
                self.assertEqual(fields[0]['source_node_id'],1 if index==0 else 2)
                if index==0:
                    self.assertEqual(fields[0]['canopen_consumer_event'],'HEARTBEAT_CONSUMER_TIMEOUT_REPORTED')
                    self.assertEqual(len(report['unsupported_frames']),1)
                else:
                    self.assertEqual(fields[0]['canopen_consumer_event'],'REMOTE_RESET_REPORTED')
                    self.assertEqual(fields[1]['canopen_consumer_scope'],'AGGREGATE_CONDITION_RESET_NOT_NODE_RECOVERY')
                    self.assertEqual(fields[2]['canopen_consumer_event'],'UNINTERPRETED_SOURCE_PATTERN')
                    self.assertEqual(fields[2]['canopen_info_code'],0x12345678)

    def test_selected_short_error_payload_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'capture.log'
            path.write_text(native('1,081,3081101b020000\n'))
            selected,_=convert_path(path,'can0',start_line=0,stop_line=1)
            with self.assertRaises(ValueError): convert(selected)


if __name__=='__main__': unittest.main()
