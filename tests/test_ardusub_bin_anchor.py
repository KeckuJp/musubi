"""Authored native declarations, not physical surface or installed identity."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_ardusub_bin_window as binary
from tests.test_ardusub_bin_window import preamble, group
from tests.test_ardusub_window_anchor import anchor


class NativeAnchor(unittest.TestCase):
    def convert(self, path, start, stop, relation, pin=binary.window.session.PIN):
        return binary.convert(path, start_record=start, stop_record=stop, build_record=9,
            source_pin=pin, session_id="authored", single_boot_session=True,
            mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"],
            surface_anchor=None if relation is None else json.dumps(relation).encode())

    def relation(self, raw):
        result=anchor(raw)
        result.update(schema="ardusub-bin-declared-surface/v1", anchor_record_index=10)
        result["original_bin_sha256"]=result.pop("original_jsonl_sha256")
        return result

    def test_two_native_inputs_common_and_default(self):
        for pin, build, value in ((binary.window.session.PIN,b"ArduSub V4.5.7 (30257f01)",24),
                ("80e8c23a7a07d0653fc1ce6b6a55ee17889c77af",b"ArduSub V4.5.3 (80e8c23a)",30)):
            raw=preamble()+group(build=build)+group(110,value,build)
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/"input.bin";path.write_bytes(raw)
                for start,stop,depth,context in ((10,16,0,0),(17,23,value-12,1)):
                    files,report=self.convert(path,start,stop,self.relation(raw),pin)
                    self.assertEqual(report["selected_records"],6)
                    self.assertEqual(report["native_anchor_context_only_records"],context)
                    self.assertEqual(report["native_anchor_record_index"],10)
                    self.assertEqual(report["selected_counts"]["CTUN"],1)
                    item=next(i for i in report["outputs"] if i["file"]=="CTUN.csv")
                    out=Path(directory)/"ctun.csv";out.write_bytes(files["CTUN.csv"])
                    common=json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(binary.ROOT/item["profile"]),str(out),*item["reader_options"]],
                        check=True,capture_output=True).stdout)
                    fields=common["observations"][0]["fields"]
                    self.assertEqual(fields["declared_surface_estimated_depth_down_m"],depth)
                    self.assertEqual(fields["original_bin_record_index"],start)
                    self.assertEqual(fields["native_anchor_record_index"],10)
                    self.assertEqual(fields["original_bin_anchor_sha256"],report["original_bin_anchor_sha256"])
                    self.assertEqual(len(common["observations"]),1)
                self.assertEqual(self.convert(path,10,16,None,pin),binary.convert(path,
                    start_record=10,stop_record=16,build_record=9,source_pin=pin,
                    session_id="authored",single_boot_session=True,
                    mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"]))

    def test_important_refusals(self):
        raw=preamble()+group()+group(110,24)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"input.bin";path.write_bytes(raw)
            for key,value in (("original_bin_sha256","sha256:"+"0"*64),
                    ("anchor_record_index",11),("anchor_record_index",99),
                    ("origin_reset_times_us",[50]),("anchor_time_us",11),
                    ("anchor_z_up_m",0),("session_id","other")):
                relation=self.relation(raw);relation[key]=value
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                    self.convert(path,17,23,relation)
            relation=self.relation(raw);relation.pop("citation")
            with self.assertRaises(ValueError):self.convert(path,17,23,relation)
            with self.assertRaises(ValueError):self.convert(path,18,23,self.relation(raw))
