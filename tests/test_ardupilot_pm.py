"""Fixed Copter4.5.5 PM writer, authored saved input only; no controller execution."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import convert_ardupilot_mode_csv as converter
from scripts import normalize_ardupilot_report as bridge

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/ardupilot-mode"
HEADER = "TimeUS,LR,NLon,NL,MaxT,Mem,Load,ErrL,IntE,ErrC,SPIC,I2CC,I2CI,Ex"
ROW = "100,400,2,30,20000,4096,750,1,2,3,4,5,6,100"
SCHEMA = "pm-copter-4.5.5"


class PerformanceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER") and os.environ.get("MUSUBI_MAVLOG_READER"),
                         "actual saved JSONL and common readers required")
    def test_rover_fixed_writer_reuses_two_readers_and_rejects_cross_vehicle_selector(self):
        data = dict(zip(HEADER.split(","), map(int, ROW.split(","))))
        data.update(TimeUS=200, LR=49, NLon=1, NL=200, MaxT=21000, Mem=32768, Load=125, Ex=250)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "rover.jsonl"
            source.write_text(json.dumps(dict(meta=dict(type="PM"), data=data)) + "\n")
            raw = subprocess.run([os.environ["MUSUBI_MAVLOG_READER"],
                str(CASE / "imu-unclassified-profile.toml"), str(source)], check=True, capture_output=True).stdout
            output, report = bridge.convert(raw, mode_vehicle="rover-4.5.5", record="pm-rover-4.5.5")
            self.assertEqual((report["selected_records"], report["not_selected_records"]), (1, 0))
            target = path / "rover.csv"; target.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(CASE / "performance-profile.toml"), str(target)], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            self.assertEqual(common["platform_domain"], "Unknown")
            obs = common["observations"][0]
            self.assertEqual((obs["t_boot_us"], obs["clock_basis"]), (200, "BootRelative"))
            for key, expected in {"loop_rate_hz": 49, "long_loops": 1, "loops": 200,
                                  "max_loop_s": .021, "memory_bytes": 32768,
                                  "scheduler_load": .125, "extra_loop_s": .00025}.items():
                self.assertEqual(obs["fields"]["reported_pm_" + key], expected)
            self.assertEqual(obs["fields"]["reported_pm_schema"], "pm-rover-4.5.5")
            meaning = common["profile_units"]["reported_pm_schema"]
            self.assertIn("9064e22695992593b12e71a2b2ad25faba2f7277", meaning)
            self.assertIn("142aece2da56146d94dcad0f6b539af6960538f6", meaning)
            for vehicle in ("copter-4.5.5", "rover-4.5.4"):
                with self.assertRaises(ValueError):
                    bridge.convert(raw, mode_vehicle=vehicle, record="pm-rover-4.5.5")
            for key, value in (("Load", 1001), ("NL", True)):
                invalid = dict(data, **{key: value})
                supplied = json.dumps(dict(value_basis=bridge.BASIS,
                    source_records=[dict(meta=dict(type="PM"), data=invalid)])).encode()
                with self.assertRaises(ValueError):
                    bridge.convert(supplied, mode_vehicle="rover-4.5.5", record="pm-rover-4.5.5")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER") and os.environ.get("MUSUBI_MAVLOG_READER"),
                         "actual saved JSONL and common readers required")
    def test_saved_reader_report_to_common_preserves_quantities_clock_and_accounting(self):
        data = dict(zip(HEADER.split(","), map(int, ROW.split(","))))
        second = dict(data, LR=397, NLon=0, NL=50, MaxT=2500, Mem=8192, Load=1000, Ex=0)
        records = [dict(meta=dict(type="PM"), data=dict(data, Extra={"kept": True})),
                   dict(meta=dict(type="MSG"), data=dict(TimeUS=100, Message="unselected")),
                   dict(meta=dict(type="PM"), data=second)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "input.jsonl"
            source.write_text("".join(json.dumps(r) + "\n" for r in records))
            raw = subprocess.run([os.environ["MUSUBI_MAVLOG_READER"],
                str(CASE / "imu-unclassified-profile.toml"), str(source)], check=True, capture_output=True).stdout
            output, report = bridge.convert(raw, mode_vehicle="copter-4.5.5", record=SCHEMA)
            self.assertEqual((report["source_records"], report["selected_records"], report["not_selected_records"]), (3, 2, 1))
            self.assertEqual(report["source_type_counts"], {"PM": 2, "MSG": 1})
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r["source_report_record_index"] for r in rows], ["0", "2"])
            self.assertEqual(rows[0]["source_report_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["Extra"][8:])), {"kept": True})
            target = path / "out.csv"; target.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(CASE / "performance-profile.toml"), str(target), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["platform_domain"], "Unknown")
            for index, expected in enumerate([(400, 2, 30, .02, 4096, .75, .0001),
                                               (397, 0, 50, .0025, 8192, 1, 0)]):
                obs = common["observations"][index]
                self.assertEqual(obs["t_boot_us"], 100)
                self.assertEqual(obs["clock_basis"], "BootRelative")
                fields = obs["fields"]
                names = ["loop_rate_hz", "long_loops", "loops", "max_loop_s", "memory_bytes", "scheduler_load", "extra_loop_s"]
                for name, value in zip(names, expected):
                    self.assertEqual(fields["reported_pm_" + name], value)
                    self.assertIn("reported_pm_" + name, common["profile_units"])
                self.assertEqual(fields["reported_pm_schema"], SCHEMA)
                self.assertEqual(fields["reported_pm_load_basis"], "LOOP_RATE_OR_SPARE_TIME_REPORTED")
                self.assertEqual(fields["IntE"], 2)

    def test_refuses_wrong_layout_missing_invalid_and_bounds(self):
        source = HEADER + "\n" + ROW + "\n"
        for vehicle, schema in [("rover-4.5.5", SCHEMA), ("plane", SCHEMA), ("copter-4.5.4", SCHEMA),
                                ("copter-4.5.5", "pm-copter-4.5.4")]:
            with self.assertRaises(ValueError): converter.convert(source, vehicle, schema)
        for name, value in [("Load", "1001"), ("LR", "65536"), ("Mem", "4294967296"),
                            ("NL", ""), ("NLon", "true"), ("MaxT", "NaN"), ("Ex", "-1"), ("LR", "4.5")]:
            cells = ROW.split(","); cells[HEADER.split(",").index(name)] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                converter.convert(HEADER + "\n" + ",".join(cells) + "\n", "copter-4.5.5", SCHEMA)
        for bad in [source.replace("LR,", ""), source.replace("LR,", "NL,"),
                    source.replace("LR,", "NLoop,"), source + ROW.replace("100,", "99,", 1) + "\n",
                    source.replace(",Ex", ",reported_pm_schema"), source.replace("LR,", "LR,NLoop,")]:
            with self.assertRaises(ValueError): converter.convert(bad, "copter-4.5.5", SCHEMA)
        for limit, bound, raw in [("MAX_COPTER_PM_INPUT", 10, source),
                                  ("MAX_COPTER_PM_ROWS", 1, source + ROW + "\n"),
                                  ("MAX_COPTER_PM_OUTPUT", 10, source)]:
            with patch.object(converter, limit, bound), self.assertRaises(ValueError):
                converter.convert(raw, "copter-4.5.5", SCHEMA)
        zero = "0," + ",".join(["0"] * 13)
        row = next(csv.DictReader(io.StringIO(converter.convert(HEADER + "\n" + zero, "copter-4.5.5", SCHEMA))))
        self.assertEqual(row["reported_pm_loops"], "0")
        self.assertEqual(row["reported_pm_memory_bytes"], "0")

    def test_cli_and_prior_csv_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.csv", Path(directory) / "out.csv"
            source.write_text(HEADER + "\n" + ROW + "\n")
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_ardupilot_mode_csv.py"),
                str(source), str(target), "--vehicle", "copter-4.5.5", "--record", SCHEMA], check=True, capture_output=True)
            self.assertEqual(target.read_text(), converter.convert(source.read_text(), "copter-4.5.5", SCHEMA))
        cases = [
            ("mode", "TimeUS,Mode,ModeNum,Rsn\n100,0,0,1\n", "d8a395c471f81c4ba2191f10ed293e9b787d5bb345eb717156732d8ae6847039"),
            ("pm-4.3.1", HEADER.replace("LR,NLon,NL", "NLon,NLoop") + "\n" + ROW.replace("100,400,", "100,", 1) + "\n",
             "d3feb17b7c867cddde6f0952156e70ea7b3f5232f32fd54cc50542de97529afb"),
            ("pm-4.3.2", HEADER + "\n" + ROW.replace("100,400,", "100,50,", 1) + "\n",
             "394560f4c8bd4bc5073bf89198194dcd71a90f6255216084b3ac94932f227edd")]
        for record, source, expected in cases:
            self.assertEqual(hashlib.sha256(converter.convert(source, "plane", record).encode()).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
