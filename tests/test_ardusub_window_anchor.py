"""Authored original-bound anchors; no actual water-reference evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tests.test_ardusub_session_window import encoded
from tests.test_ardusub_session import records, ROOT
from scripts import convert_ardusub_session_window as window


def anchor(raw):
    return dict(schema="ardusub-jsonl-declared-surface/v1",
        original_jsonl_sha256="sha256:"+hashlib.sha256(raw).hexdigest(),
        source_id="authored-source", session_id="authored", origin_epoch_id="epoch-1",
        citation="authored surface declaration, not a measurement", anchor_record_index=1,
        anchor_time_us=10, anchor_z_up_m=-12, valid_start_us=10, valid_stop_us=200,
        origin_reset_times_us=[], unit="m", positive_direction="up", reference="reported_ekf_origin")


class WindowAnchor(unittest.TestCase):
    def convert(self, path, start, stop, relation):
        self.assertTrue(os.environ.get("MUSUBI_MAVLOG_READER"))
        return window.convert(path, start_record=start, stop_record=stop, build_record=0,
            source_pin=window.session.PIN, session_id="authored", single_boot_session=True,
            mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"],
            surface_anchor=json.dumps(relation).encode() if relation is not None else None)

    def test_long_original_external_and_selected_anchor_common(self):
        self.assertTrue(os.environ.get("MUSUBI_TELEMETRY_READER"))
        second = records(24)
        for row in second[1:6]:
            row["data"]["TimeUS"] += 100
        filler = dict(meta=dict(type="FUTURE"), data=dict(blob="x"*29000))
        raw = encoded(records(12)+[filler]*600+second)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"source.jsonl"; path.write_bytes(raw)
            self.assertGreater(len(raw),16*1024**2)
            for start,stop,depth,context in ((1,6,0,0),(608,613,12,1)):
                files,report=self.convert(path,start,stop,anchor(raw))
                self.assertEqual(report["selected_records"],5)
                self.assertEqual(report["selected_counts"]["CTUN"],1)
                self.assertEqual(report["anchor_context_only_records"],context)
                self.assertEqual(report["vertical_report"]["selected_records"],1+context)
                self.assertEqual(report["missing_required_families"],[])
                self.assertEqual(report["window_unselected_records"],0)
                derived=json.loads(files["derived-surface-anchor.json"])
                self.assertEqual(derived["source_report_sha256"],"sha256:"+hashlib.sha256(files["source-report.json"]).hexdigest())
                item=next(i for i in report["outputs"] if i["file"]=="CTUN.csv")
                self.assertEqual(item["records"],1)
                out=Path(directory)/"ctun.csv";out.write_bytes(files["CTUN.csv"])
                result=json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],str(ROOT/item["profile"]),str(out),*item["reader_options"]],check=True,capture_output=True).stdout)
                self.assertEqual(len(result["observations"]),1)
                fields=result["observations"][0]["fields"]
                self.assertEqual(fields["declared_surface_estimated_depth_down_m"],depth)
                self.assertEqual(fields["anchor_original_record_index"],1)
                self.assertEqual(fields["original_jsonl_record_index"],start)
            first=self.convert(path,1,6,None)
            second=window.convert(path,start_record=1,stop_record=6,build_record=0,
                source_pin=window.session.PIN,session_id="authored",single_boot_session=True,
                mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"])
            self.assertEqual(first,second)

    def test_wrong_anchor_boundaries_and_changed_original_refuse(self):
        raw=encoded(records(12))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"source.jsonl"
            for key,value in (("original_jsonl_sha256","sha256:"+"0"*64),
                    ("anchor_record_index",2),("anchor_time_us",11),("anchor_z_up_m",0),
                    ("valid_stop_us",9),("origin_reset_times_us",[10,11]),("session_id","other")):
                path.write_bytes(raw); relation=anchor(raw);relation[key]=value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    self.convert(path,1,6,relation)
            path.write_bytes(raw)
            with self.assertRaises(ValueError): self.convert(path,2,6,anchor(raw))
            later=records(24)
            later[1]["data"]["TimeUS"]=201
            boundary=encoded(records(12)+later)
            path.write_bytes(boundary)
            with self.assertRaises(ValueError): self.convert(path,8,9,anchor(boundary))
            mixed=records(12)
            mixed[-1]=dict(meta=dict(type="MSG"),data=dict(Message="ArduCopter V4.5.7 (abcd1234)"))
            mixed_raw=encoded(mixed);path.write_bytes(mixed_raw)
            with self.assertRaises(ValueError): self.convert(path,1,7,anchor(mixed_raw))
            path.write_bytes(raw)
            original=window.read_jsonl_window
            def changed(*args,**kwargs):
                result=original(*args,**kwargs)
                path.write_bytes(raw+b"{}\n")
                return result
            with patch.object(window,"read_jsonl_window",changed),self.assertRaises(ValueError):
                self.convert(path,2,6,anchor(raw))
