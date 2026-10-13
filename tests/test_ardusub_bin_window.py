"""Authored fixed-format BIN; not an installed Sub or actual underwater log."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from scripts import convert_ardusub_bin_window as bundle
from tests.test_ardupilot_record_export import fmt_record, pack, SIZES, HEAD


def preamble():
    raw=fmt_record(128,89,"FMT","BBnNZ","Type,Length,Name,Format,Columns")
    for i,(name,(fmt,labels)) in enumerate(bundle.exporter.SUB457_LAYOUTS.items(),140):
        raw+=fmt_record(i,3+sum(SIZES[c] for c in fmt),name,fmt,labels)
    return raw+fmt_record(150,211,"FUTR","ZZZN","A,B,C,D")


def group(stamp=10,value=12,build=b"ArduSub V4.5.7 (30257f01)"):
    rows={"MSG":(stamp,build),
        "CTUN":(stamp,0.,0.,0.,0.,1.,-value,0.,0,0,0.,120,-50),
        "MODE":(stamp,19,19,1),
        "BAT":(stamp,0,value,value,2.,100.,1.,2000,.01,90,1,100),
        "BCL":(stamp,0,value,*([4001]*12)),"BCL2":(stamp,0,4001,0),
        "IMU":(stamp,1,1.,2.,3.,4.,5.,6.,0,0,20.,1,1,100,100)}
    raw=b""
    for i,(name,(fmt,_)) in enumerate(bundle.exporter.SUB457_LAYOUTS.items(),140):
        body=b"".join(struct.pack("<h",value) if code=="h" else pack(code,[value])
                      for code,value in zip(fmt,rows[name]))
        raw+=HEAD+bytes([i])+body
    return raw


class SubBinWindow(unittest.TestCase):
    def convert(self,path,start,stop):
        self.assertTrue(os.environ.get("MUSUBI_MAVLOG_READER"))
        return bundle.convert(path,start_record=start,stop_record=stop,build_record=9,
            source_pin=bundle.window.session.PIN,session_id="authored-bin",single_boot_session=True,
            mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"])

    def test_long_original_two_intervals_common(self):
        self.assertTrue(os.environ.get("MUSUBI_TELEMETRY_READER"))
        filler=HEAD+bytes([150])+b"x"*208
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"source.bin"
            with path.open("wb") as source:
                source.write(preamble()+group())
                for _ in range(320): source.write(filler*1000)
                source.write(group(110,24))
            self.assertGreater(path.stat().st_size,64*1024**2)
            for start,stop,value in ((10,16,12),(320017,320023,24)):
                files,report=self.convert(path,start,stop)
                self.assertEqual(report["missing_required_families"],[])
                self.assertEqual(report["selected_records"],6)
                self.assertEqual(report["native_context_only_records"],1)
                native=report["native_export_report"]
                self.assertEqual(native["framing"],bundle.exporter.COMPLETE)
                self.assertEqual(native["exported_counts"]["CTUN"],1)
                self.assertEqual(native["outside_window_selected_type_counts"]["CTUN"],1)
                for item in report["outputs"]:
                    out=Path(directory)/item["file"];out.write_bytes(files[item["file"]])
                    result=json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(bundle.ROOT/item["profile"]),str(out),*item["reader_options"]],
                        capture_output=True,check=True).stdout)
                    fields=result["observations"][0]["fields"]
                    self.assertEqual(fields["original_bin_sha256"],native["source_sha256"])
                    self.assertGreaterEqual(fields["original_bin_record_index"],start)
                    self.assertLess(fields["original_bin_record_index"],stop)
                    if item["file"]=="CTUN.csv":
                        self.assertEqual(fields["reported_estimated_z_up_m"],-value)
                        self.assertEqual(fields["reported_target_vertical_rate_m_s"],1.2)
                    if item["file"]=="BAT.csv": self.assertEqual(fields["battery_voltage_v"],value)

    def test_wrong_layout_build_clock_framing_and_defaults(self):
        good=preamble()+group()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"source.bin"
            for raw in (good.replace(b"Qfffffffccfhh",b"Qfffffffccfcc",1),
                        preamble()+group(build=b"ArduCopter V4.5.7 (abcd1234)"),
                        good+group(1),good[:-1]):
                path.write_bytes(raw)
                with self.assertRaises(ValueError): self.convert(path,10,23 if raw==good+group(1) else 16)
            path.write_bytes(good)
            _,missing=self.convert(path,10,11)
            self.assertEqual(missing["missing_required_families"],["MODE","BAT","BCL","IMU"])
            self.assertEqual(missing["disposition"],"INCOMPLETE_REQUIRED_INPUT")
            old=bundle.exporter.export_window(path,("MODE","BAT"),10,16)
            explicit=bundle.exporter.export_window(path,("MODE","BAT"),10,16,sub457_jsonl=False)
            self.assertEqual(old,explicit)
            with self.assertRaises(ValueError):
                bundle.exporter.export_records(path,("CTUN",),output_format="jsonl")
            with self.assertRaises(ValueError):
                bundle.exporter.export_window(path,("CTUN",),10,16,sub457_jsonl="true")
