"""Authored report composition, not a BlueROV2 recording or water measurement."""
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_ardusub_session as session
from scripts import convert_ardupilot_mode_csv as mode
from tests.test_ardusub_vertical_reports import report

ROOT = Path(__file__).resolve().parents[1]


def records(value):
    def r(kind, **data):
        return dict(meta=dict(type=kind), data=data)
    return [r("MSG", Message="ArduSub V4.5.7 (30257f01)"),
        r("CTUN", TimeUS=10, DCRt=120, CRt=-50, DAlt=1.0, Alt=-value),
        r("MODE", TimeUS=11, Mode=19, ModeNum=19, Rsn=1),
        r("BAT", TimeUS=12, Inst=0, Volt=value, Curr="NaN", CurrTot=1000,
          EnrgTot=1, RemPct=255, VoltR=value, Temp=20, Res="NaN", H=1, SH=0),
        r("BCL", TimeUS=13, Instance=0, Volt=value, **{f"V{i}": 4001 if i == 1 else 0 for i in range(1, 13)}),
        r("IMU", TimeUS=14, I=1, GyrX=value, GyrY=0, GyrZ=-1,
          AccX=1, AccY=2, AccZ=-9.81, Unknown={"retained": True}),
        r("FUTURE", Unknown="kept")]


class SubSession(unittest.TestCase):
    def convert(self, rows, **kw):
        return session.convert(report(*rows), source_pin=session.PIN, session_id="authored",
                               single_boot_session=True, **kw)

    def test_two_reports_common_preserve_meanings_and_association(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "reader-equipped mandatory stage required")
        for value in (12.0, 24.0):
            source = records(value)
            raw = report(*source)
            files, accounting = self.convert(source)
            self.assertEqual(accounting["missing_required_families"], [])
            self.assertEqual((accounting["selected_records"], accounting["not_selected_records"]), (5, 2))
            with tempfile.TemporaryDirectory() as directory:
                for item in accounting["outputs"]:
                    path = Path(directory) / item["file"]
                    path.write_bytes(files[item["file"]])
                    result = json.loads(subprocess.run([reader, str(ROOT / item["profile"]), str(path), *item["reader_options"]],
                        check=True, capture_output=True).stdout)
                    observation = result["observations"][0]
                    fields = observation["fields"]
                    index = fields["session_source_record_index"]
                    self.assertEqual(fields["session_report_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.assertEqual(json.loads(bytes.fromhex(fields["session_record_hex"][4:])), source[index])
                    self.assertEqual(observation["clock_basis"], "BootRelative")
                    self.assertEqual(result["platform_domain"], "Subsurface" if item["file"] == "CTUN.csv" else "Unknown")
                    if item["file"] == "IMU.csv":
                        self.assertEqual(fields["GyrX"], value)
                        self.assertEqual(result["profile_units"]["GyrX"], "rad/s")
                    elif item["file"] == "BAT.csv":
                        self.assertEqual(fields["battery_voltage_v"], value)
                        self.assertIsNone(fields["battery_current_a"])
                    elif item["file"] == "MODE.csv":
                        self.assertEqual(fields["reported_mode_name"], "MANUAL")
            original = mode.convert("TimeUS,Mode,ModeNum,Rsn\n11,19,19,1\n", "sub-4.5.7")
            expected = next(csv.DictReader(io.StringIO(original)))
            actual = next(csv.DictReader(io.StringIO(files["MODE.csv"].decode())))
            self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_missing_and_important_refusals(self):
        files, counts = self.convert(records(12)[:-2] + [records(12)[-1]])
        self.assertEqual(counts["missing_required_families"], ["IMU"])
        self.assertEqual(counts["disposition"], "INCOMPLETE_REQUIRED_INPUT")
        self.assertNotIn("IMU.csv", files)
        for kind in ("wrong-build", "regression", "missing-value", "wrong-instance"):
            source = copy.deepcopy(records(12))
            if kind == "wrong-build":
                source[0]["data"]["Message"] = "ArduSub V4.5.3 (80e8c23a)"
            elif kind == "regression":
                source[5]["data"]["TimeUS"] = 1
            elif kind == "missing-value":
                del source[5]["data"]["GyrX"]
            else:
                source[5]["data"]["I"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.convert(source)
        with self.assertRaises(ValueError):
            session.convert(report(*records(12)), source_pin="80e8c23a", session_id="x", single_boot_session=True)
        with self.assertRaises(ValueError):
            session.convert(report(*records(12)), source_pin=session.PIN, session_id="x", single_boot_session=False)
