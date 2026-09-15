"""Independent synthetic contract for recorded Plane MODE, authored fixtures."""
import copy
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_ardupilot_mode_csv.py"
HEADER = "TimeUS,Mode,ModeNum,Rsn,Extra"
APPENDED = ["reported_mode_name", "reported_mode_known"]
# Number identifiers read from the two pinned Plane enum contract, not converter data.
KNOWN = {0: "MANUAL", 1: "CIRCLE", 2: "STABILIZE", 3: "TRAINING", 4: "ACRO",
         5: "FLY_BY_WIRE_A", 6: "FLY_BY_WIRE_B", 7: "CRUISE", 8: "AUTOTUNE",
         10: "AUTO", 11: "RTL", 12: "LOITER", 13: "TAKEOFF", 14: "AVOID_ADSB",
         15: "GUIDED", 16: "INITIALISING", 17: "QSTABILIZE", 18: "QHOVER",
         19: "QLOITER", 20: "QLAND", 21: "QRTL", 22: "QAUTOTUNE", 23: "QACRO",
         24: "THERMAL", 25: "LOITER_ALT_QLAND"}


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConversionTests(unittest.TestCase):
    def test_nonfinite_reported_default_retains_unknown_without_losing_parameter(self):
        from scripts.normalize_pymavlink_jsonl import convert as normalize
        bridge = load(ROOT / "scripts/normalize_ardupilot_report.py")
        for token in ("NaN", "Infinity", "-Infinity"):
            original = ('{"meta":{"type":"PARM"},"data":{"TimeUS":100,"Name":"P0",'
                        '"Value":2.5,"Default":' + token + ',"Extra":[1,"kept"]}}\n').encode()
            safe = normalize(original)
            record = json.loads(safe)
            self.assertEqual(record["meta"]["nonfinite_json_literals"], {"/data/Default": token})
            self.assertEqual(bytes.fromhex(record["source_nonfinite_export_line_hex"][4:]), original.rstrip(b"\n"))
            report_raw = json.dumps(dict(value_basis=bridge.BASIS, source_records=[record])).encode()
            with self.assertRaises(ValueError): bridge.convert(report_raw, mode_vehicle="plane", record="parm-4.3.1")
            output, _ = bridge.convert(report_raw, mode_vehicle="plane", record="parm-4.3.1", preserve_nonfinite_default=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(rows[0]["reported_parameter_default_state"], "NONFINITE_UNUSABLE")
            self.assertEqual(rows[0]["Default"], token)
            with self.assertRaises(ValueError):
                self.converter.convert("TimeUS,Name,Value,Default\n100,P0,NaN,1\n", "plane", "parm-4.3.1", preserve_nonfinite_default=True)

    def test_nonstandard_json_normalization_refuses_ambiguous_or_overflowing_inputs(self):
        from scripts.normalize_pymavlink_jsonl import convert as normalize
        finite = b'{"meta":{"type":"PARM"},"data":{"TimeUS":1,"Name":"P0","Value":1,"Default":0}}\n'
        self.assertEqual(normalize(finite), finite)
        for raw in (b'\n', b'{}', finite.replace(b'"Value":1', b'"Value":1,"Value":2'),
                    finite.replace(b'"Value":1', b'"Value":1e999'),
                    finite.replace(b'"Default":0', b'"Default":NaN').replace(b'"type":"PARM"', b'"type":"PARM","nonfinite_json_literals":{}')):
            with self.assertRaises(ValueError): normalize(raw)
        with self.assertRaises(ValueError):
            self.converter.convert("TimeUS,Name,Value\n1,P0,2\n", "plane", "parm-4.2.3", preserve_nonfinite_default=True)

    def test_saved_state_report_reuses_existing_record_selection(self):
        bridge = load(ROOT / "scripts/normalize_ardupilot_report.py")
        cases = [
            ("arm", "ARM", dict(TimeUS=100, ArmState=1, ArmChecks=7, Forced=0, Method=2),
             "vehicle-profile.toml", "reported_arm_state", "ARMED_REPORTED"),
            ("parm-4.2.3", "PARM", dict(TimeUS=100, Name="P0", Value=-2.5),
             "parameter-profile.toml", "Value", -2.5),
            ("parm-4.3.1", "PARM", dict(TimeUS=100, Name="P0", Value=3.25, Default=1),
             "parameter-profile.toml", "Default", 1),
            ("pm-4.3.1", "PM", dict(TimeUS=100, NLon=2, NLoop=20, MaxT=5000, Mem=2048,
                Load=250, ErrL=0, IntE=0, ErrC=0, SPIC=0, I2CC=0, I2CI=0, Ex=100),
             "vehicle-profile.toml", "reported_pm_max_loop_s", 0.005),
        ]
        for record, kind, data, profile, field, expected in cases:
            for time in (100, 500):
                first = dict(data, TimeUS=time, Extra=["kept", 2])
                raw = json.dumps(dict(value_basis=bridge.BASIS, source_records=[
                    dict(meta=dict(type="MSG"), data=dict(Message="untimed retained")),
                    dict(meta=dict(type=kind), data=first),
                    dict(meta=dict(type=kind), data=dict(first, Extra="different"))])).encode()
                output, report = bridge.convert(raw, mode_vehicle="plane", record=record)
                self.assertEqual((report["selected_records"], report["not_selected_records"]), (2, 1))
                self.assertEqual(report["state_record"], record)
                if record == "arm" and time == 100:
                    with tempfile.TemporaryDirectory() as directory:
                        source = Path(directory) / "report.json"; source.write_bytes(raw)
                        out = Path(directory) / "out"
                        subprocess.run([sys.executable, str(ROOT / "scripts/normalize_ardupilot_report.py"),
                            str(source), str(out), "--vehicle", "plane", "--record", record],
                            capture_output=True, check=True)
                        self.assertEqual((out / "observations.csv").read_text(), output)
                        self.assertEqual(json.loads((out / "report.json").read_text())["state_record"], record)
            with self.assertRaises(ValueError): bridge.convert(raw, battery_format="ardupilot-bat-4.3", record=record)
            if record.startswith(("parm-", "pm-")):
                with self.assertRaises(ValueError): bridge.convert(raw, mode_vehicle="rover-4.5.5", record=record)
        with self.assertRaises(ValueError): bridge.convert(raw, mode_vehicle="plane", record="parameter-file-2.4.49")

    def test_saved_reader_report_reuses_mode_and_battery_meaning(self):
        bridge = load(ROOT / "scripts/normalize_ardupilot_report.py")
        for value in (12.5, 18.25):
            records = [dict(meta=dict(type="MSG"), data=dict(Message="retained untimed")),
                dict(meta=dict(type="MODE"), data=dict(TimeUS=1000, Mode=10, ModeNum=10, Rsn=1,
                    Future=dict(values=[1, 2], text="a,b"))),
                dict(meta=dict(type="BAT"), data=dict(TimeUS=1000, Instance=0, Volt=value,
                    Curr=2.0, CurrTot=1000, EnrgTot=2, RemPct=50, Extra="kept")),
                dict(meta=dict(type="MODE"), data=dict(TimeUS=1000, Mode=255, ModeNum=255, Rsn=2))]
            raw = json.dumps(dict(value_basis=bridge.BASIS, source_records=records)).encode()
            for options, expected_count, profile, field, expected in (
                (dict(mode_vehicle="plane"), 2, "ardupilot-mode/profile.toml", "reported_mode_name", "AUTO"),
                (dict(battery_format="ardupilot-bat-4.3"), 1, "ardupilot-battery/profile.toml", "battery_voltage_v", value)):
                output, report = bridge.convert(raw, **options)
                self.assertEqual(report["selected_records"], expected_count)
                self.assertEqual(report["not_selected_records"], 4 - expected_count)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), expected_count)
                self.assertEqual(rows[0]["source_report_record_index"], "1" if expected_count == 2 else "2")
                if expected_count == 2:
                    self.assertEqual(json.loads(bytes.fromhex(rows[0]["Future"][8:])), records[1]["data"]["Future"])

    def test_saved_reader_report_rejects_wrong_inputs_and_preserves_cli_source(self):
        bridge = load(ROOT / "scripts/normalize_ardupilot_report.py")
        good = dict(value_basis=bridge.BASIS, source_records=[dict(meta=dict(type="MODE"),
            data=dict(TimeUS=1000, Mode=10, ModeNum=10, Rsn=1))])
        for mutate in (lambda d: d.update(value_basis="raw-wire"),
                       lambda d: d.update(source_records=[]),
                       lambda d: d["source_records"][0]["data"].pop("ModeNum"),
                       lambda d: d["source_records"][0]["data"].update(TimeUS=True),
                       lambda d: d["source_records"][0]["meta"].update(type="MSG"),
                       lambda d: d["source_records"][0]["data"].update(source_report_record_index=9)):
            changed = copy.deepcopy(good); mutate(changed)
            with self.assertRaises(ValueError): bridge.convert(json.dumps(changed).encode(), mode_vehicle="plane")
        for raw in (b'{"value_basis":0,"value_basis":1}', b'{"x":NaN}', b'[]'):
            with self.assertRaises(ValueError): bridge.convert(raw, mode_vehicle="plane")
        raw = json.dumps(good, indent=2).encode()
        with self.assertRaises(ValueError): bridge.convert(raw, mode_vehicle="plane", bat_details=True)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"; source.write_bytes(raw)
            out = Path(directory) / "out"
            command = [sys.executable, str(ROOT / "scripts/normalize_ardupilot_report.py"),
                       str(source), str(out), "--mode-vehicle", "plane"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((out / "source-report.json").read_bytes(), raw)
            self.assertEqual(json.loads((out / "report.json").read_text())["selected_records"], 1)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_plane455_three_record_functions_reuse_existing_common_profiles(self):
        cases = [
            ("mode", "TimeUS,Mode,ModeNum,Rsn\n100,25,25,1\n100,255,255,2\n", "vehicle-profile.toml", "reported_mode_name", "LOITER_ALT_QLAND"),
            ("arm", "TimeUS,ArmState,ArmChecks,Forced,Method\n100,1,7,0,2\n100,0,0,1,255\n", "vehicle-profile.toml", "reported_arm_state", "ARMED_REPORTED"),
            ("parm-plane-4.5.5", "TimeUS,Name,Value,Default\n100,P0,-2.5,1\n100,P1,3,0\n", "parameter-profile.toml", "Value", -2.5),
        ]
        for record, text, profile, field, expected in cases:
            output = self.converter.convert(text, "plane-4.5.5", record)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            if record == "mode":
                self.assertEqual(rows[1]["reported_mode_name"], "UNKNOWN_255")
                self.assertEqual(rows[0]["reported_mode_schema"], "plane-4.5.5")
        with self.assertRaises(ValueError):
            self.converter.convert(cases[2][1], "plane", "parm-plane-4.5.5")
        with self.assertRaises(ValueError):
            self.converter.convert(cases[2][1], "plane-4.5.5", "parm-4.3.1")

    def test_three_qualified_parameter_vehicle_profiles_reuse(self):
        for vehicle in ("rover-4.5.5", "copter-4.5.5", "sub-4.5.7"):
            schema = "parm-" + vehicle
            text = "TimeUS,Name,Value,Default,Extra\n100,P0,-2.5,1,kept\n100,P1,4,3,second\n"
            output = self.converter.convert(text, vehicle, schema)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([row["reported_parameter_schema"] for row in rows], [schema, schema])
            with self.assertRaises(ValueError): self.converter.convert(text, "plane", schema)
            with self.assertRaises(ValueError): self.converter.convert(text, vehicle, "parm-4.3.1")

    def test_practical_parameter_snapshot_and_update_reuse(self):
        for count, sign in ((6000, -1), (2, 1)):
            source = "# retained capture note\n" + "".join(f"P{i:05d} {sign * i}.000000\n" for i in range(count))
            self.assertLess(len(source.encode()), 256000)
            snapshot = self.converter.convert_parameter_snapshot(source, 1000)
            updates = "TimeUS,Name,Value,Default,Extra\n" + "".join(
                f"1000,P{i % 2:05d},{sign * i}.000000,1.000000,kept\n" for i in range(count))
            converted = self.converter.convert(updates, "plane", "parm-4.3.1")

    def test_saved_parameter_sets_preserve_names_values_document_and_capture_clock(self):
        for text, expected in (("# captured set\r\nFOO              -2.500000\r\nBAR              7\r\n", -2.5),
                               ("FOO              1.250000\nNEW_FIELD        0\n", 1.25)):
            output = self.converter.convert_parameter_snapshot(text, 1234567)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(float(rows[0]["reported_parameter_value"]), expected)
            self.assertEqual(bytes.fromhex(rows[0]["source_document_hex"][4:]).decode(), text)
            self.assertEqual(rows[1]["source_document_hex"], "")
            self.assertEqual(rows[0]["record_time_us"], "1234567")

    def test_saved_parameter_invalid_and_duplicate_not_silently_loaded(self):
        for text in ("# empty\n", "FOO 1\nFOO 2\n", "FOO nan\n", "FOO 1e39\n", "FOO,1\n",
                     "FOO 1 extra\n", "bad 1\n", "FOO 0xff\n", "FOO 1e-999\n", "FOO 1.25\n"):
            with self.assertRaises(ValueError):
                self.converter.convert_parameter_snapshot(text, 1)
        for clock in (None, -1, True, 2**63):
            with self.assertRaises(ValueError):
                self.converter.convert_parameter_snapshot("FOO 1\n", clock)
    def test_performance_boundary_preserves_load_basis_and_missing_rate(self):
        for schema, fields in (("pm-4.3.1", "TimeUS,NLon,NLoop"), ("pm-4.3.2", "TimeUS,LR,NLon,NL")):
            header = fields + ",MaxT,Mem,Load,ErrL,IntE,ErrC,SPIC,I2CC,I2CI,Ex,Extra\n"
            for start in (100, 250):
                prefix = f"{start},2,30" if schema == "pm-4.3.1" else f"{start},50,2,30"
                load = 750 if start == 100 else 250
                source = header + prefix + f",20000,4096,{load},1,2,3,4,5,6,100,kept\n"
                output = self.converter.convert(source, "plane", schema)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(row["reported_pm_loops"], "30")
                self.assertEqual(row["reported_pm_loop_rate_hz"], "" if schema == "pm-4.3.1" else "50")
                self.assertEqual(float(row["reported_pm_max_loop_s"]), .02)
                self.assertEqual(float(row["reported_pm_scheduler_load"]), load / 1000)
                self.assertEqual(row["reported_pm_load_basis"], "SPARE_TIME_REPORTED" if schema == "pm-4.3.1"
                                 else "LOOP_RATE_OR_SPARE_TIME_REPORTED")
                self.assertEqual(float(row["reported_pm_extra_loop_s"]), .0001)
                self.assertEqual(row["IntE"], "2")
                self.assertEqual(row["Extra"], "kept")
                other = "pm-4.3.2" if schema == "pm-4.3.1" else "pm-4.3.1"
                with self.assertRaises(ValueError): self.converter.convert(source, "plane", other)
                for name, value in (("Load", "65536"), ("Mem", "4294967296"), ("Ex", "-1"), ("NLon", "0.5")):
                    cells = source.splitlines()[1].split(",")
                    cells[header.strip().split(",").index(name)] = value
                    with self.assertRaises(ValueError):
                        self.converter.convert(header + ",".join(cells), "plane", schema)

    def test_parameter_version_boundary_reuses_common_record_path(self):
        for schema, header, rows in [
            ("parm-4.2.3", "TimeUS,Name,Value,Extra", ["10,LOG_BITMASK,7,old", "10,BATT_CAPACITY,4000,kept"]),
            ("parm-4.3.1", "TimeUS,Name,Value,Default,Extra", ["20,BATT_CAPACITY,3000,3300,new", "20,LOG_BITMASK,3,1,kept"]),
        ]:
            source = header + "\n" + "\n".join(rows) + "\n"
            output = self.converter.convert(source, "plane", schema)
            parsed = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(parsed), 2)
            self.assertEqual(parsed[0]["reported_parameter_schema"], schema)
            self.assertEqual(parsed[1]["Extra"], "kept")
            self.assertEqual(parsed[0]["reported_parameter_default_present"], str(schema.endswith("4.3.1")).lower())
            for index, name in [(1, ""), (1, "A" * 17), (2, "NaN"), (2, "1e100"), (2, "true")]:
                bad = rows[0].split(","); bad[index] = name
                with self.assertRaises(ValueError):
                    self.converter.convert(header + "\n" + ",".join(bad), "plane", schema)
            other = "parm-4.3.1" if schema.endswith("4.2.3") else "parm-4.2.3"
            with self.assertRaises(ValueError):
                self.converter.convert(source, "plane", other)

    def test_reported_arming_reuses_parser_without_safety_or_cause_inference(self):
        header = "TimeUS,ArmState,ArmChecks,Forced,Method,Extra\n"
        source = header + "100,1,4294967295,0,255,kept\n100,0,0,1,0,other\n"
        output = self.converter.convert(source, "plane", "arm")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["reported_arm_state"] for r in rows], ["ARMED_REPORTED", "DISARMED_REPORTED"])
        self.assertEqual(rows[0]["reported_arm_checks_mask"], "4294967295")
        self.assertEqual(rows[0]["reported_arm_method_code"], "255")
        self.assertEqual(rows[0]["Extra"], "kept")
        self.assertEqual(rows[1]["reported_arm_forced"], "true")
        for invalid in ["1,2,0,0,0,x", "1,1,4294967296,0,0,x", "1,0,0,2,0,x", "1,0,0,0,256,x"]:
            with self.assertRaises(ValueError):
                self.converter.convert(header + invalid, "plane", "arm")
        with self.assertRaises(ValueError):
            self.converter.convert(source, "rover-4.4.0", "arm")

    def test_reported_arm_shared_exact_vehicle_schemas(self):
        header = "TimeUS,ArmState,ArmChecks,Forced,Method,Extra\n"
        for vehicle in ("rover-4.5.5", "copter-4.4.0", "copter-4.5.0", "copter-4.5.5", "sub-4.5.7"):
            for source in (header + "100,1,4294967295,0,255,kept\n100,0,0,1,0,other\n",
                           header + "250,0,1,0,3,unused\n"):
                output = self.converter.convert(source, vehicle, "arm")
                rows = list(csv.DictReader(io.StringIO(output)))
                baseline = list(csv.DictReader(io.StringIO(self.converter.convert(source, "plane", "arm"))))
                for row, old in zip(rows, baseline):
                    self.assertEqual(row.pop("reported_arm_schema"), vehicle)
                    self.assertEqual(row, old)
            for bad in ("1,2,0,0,0,x", "1,1,4294967296,0,0,x", "1,0,0,2,0,x", "1,0,0,0,256,x"):
                with self.assertRaises(ValueError):
                    self.converter.convert(header + bad, vehicle, "arm")
            with self.assertRaises(ValueError):
                self.converter.convert("TimeUS,Name,Value\n1,TEST,1\n", vehicle, "parm-4.2.3")

    @classmethod
    def setUpClass(cls):
        cls.converter = load(SCRIPT)

    def test_every_known_enum_and_every_unknown_uint8(self):
        rows = [f"{n},{n},{n},255,opaque" for n in range(256)]
        result = list(csv.reader(io.StringIO(self.converter.convert(HEADER + "\n" + "\n".join(rows), "plane"))))
        self.assertEqual(result[0], HEADER.split(",") + APPENDED)
        self.assertEqual(len(result), 257)
        for number, source in enumerate(rows):
            with self.subTest(number=number):
                self.assertEqual(result[number + 1], source.split(",") +
                                 [KNOWN.get(number, f"UNKNOWN_{number}"), "true" if number in KNOWN else "false"])

    def test_finite_vehicle_tables_share_parser_but_not_mode_meaning(self):
        for schema, number, label in [("copter-4.5.5", 0, "STABILIZE"),
                ("copter-4.4.0", 27, "AUTO_RTL"), ("copter-4.5.0", 28, "TURTLE"),
                ("copter-4.5.5", 9, "LAND"), ("rover-4.5.5", 0, "MANUAL"),
                ("rover-4.5.5", 8, "DOCK"), ("rover-4.4.0", 9, "CIRCLE"),
                ("rover-4.5.0", 12, "SMART_RTL"), ("sub-4.5.7", 9, "SURFACE"),
                ("sub-4.5.7", 21, "SURFTRAK")]:
            text = HEADER + f"\n1000,{number},{number},255,keep\n1000,254,254,0,unknown\n"
            output = self.converter.convert(text, schema)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(rows[0]["reported_mode_name"], label)
            self.assertEqual(rows[0]["reported_mode_schema"], schema)
            self.assertEqual(rows[0]["Extra"], "keep")
            self.assertEqual(rows[1]["reported_mode_name"], "UNKNOWN_254")
            with self.assertRaises(ValueError):
                self.converter.convert(HEADER + "\n1,0,1,1,\n", schema)
        with self.assertRaises(ValueError):
            self.converter.convert(HEADER + "\n1,0,0,1,\n", "sub-unknown")

    def test_exact_rover_exports_reuse_dictionary_without_claiming_intermediate_versions(self):
        expected = {0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER",
                    6: "FOLLOW", 7: "SIMPLE", 8: "DOCK", 9: "CIRCLE", 10: "AUTO",
                    11: "RTL", 12: "SMART_RTL", 15: "GUIDED", 16: "INITIALISING"}
        text = HEADER + "\n" + "\n".join(f"{n},{n},{n},255,keep" for n in range(256))
        for schema in ("rover-4.4.0", "rover-4.5.0", "rover-4.5.5"):
            rows = list(csv.DictReader(io.StringIO(self.converter.convert(text, schema))))
            self.assertEqual(len(rows), 256)
            for n, row in enumerate(rows):
                self.assertEqual(row["reported_mode_name"], expected.get(n, f"UNKNOWN_{n}"))
                self.assertEqual(row["reported_mode_known"], "true" if n in expected else "false")
                self.assertEqual(row["reported_mode_schema"], schema)
        with self.assertRaises(ValueError):
            self.converter.convert(text, "rover-4.4.4")

    def test_raw_cells_blanks_ties_and_counter_bounds_survive(self):
        source = ["0,000,0,000,", "0,255,255,255,opaque", "9223372036854775807,10,10,1,  text  "]
        result = list(csv.reader(io.StringIO(self.converter.convert(HEADER + "\n" + "\n".join(source), "plane"))))
        self.assertEqual([row[:5] for row in result[1:]], [row.split(",") for row in source])

    def test_family_must_be_explicit_plane(self):
        text = HEADER + "\n1,0,0,1,\n"
        for vehicle in ("", "copter", "copter-4.4.1", "rover", "Plane", None):
            with self.subTest(vehicle=vehicle), self.assertRaises(ValueError):
                self.converter.convert(text, vehicle)
        with self.assertRaises(TypeError):
            self.converter.convert(text)

    def test_reject_counter_disagreement_structure_and_reserved_headers(self):
        base = "1,0,0,1,opaque"
        for index in range(4):
            invalid = ["", "-1", "+1", "1.0", "1e2", "NaN", "inf", " 1", "１"]
            invalid += ["9223372036854775808"] if index == 0 else ["256"]
            for value in invalid:
                row = base.split(",")
                row[index] = value
                with self.subTest(column=index, value=value), self.assertRaises(ValueError):
                    self.converter.convert(HEADER + "\n" + ",".join(row), "plane")
        cases = ["", HEADER, "TimeUS,Other\n1,1", HEADER + "\n\n", HEADER + "\n1,0,1,1,",
                 HEADER + "\n" + base + "\n0,0,0,1,", HEADER + "\n" + base + ",extra",
                 HEADER + "\n" + base.rsplit(",", 1)[0], HEADER + "\n" + base + '\x00',
                 HEADER + "\n" + base + '\t', HEADER + "\n" + base + '\x01',
                 HEADER + "\n" + base.replace("opaque", '"opaque"'),
                 HEADER + ",Mode\n" + base + ",0", HEADER + ",reported_mode_future\n" + base + ",x",
                 HEADER.replace("Extra", " Extra") + "\n" + base,
                 HEADER.replace("Extra", "Extra (s)") + "\n" + base]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.converter.convert(text, "plane")

    def test_cli_explicit_family_exclusive_output_and_atomic_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.csv", Path(directory) / "output.csv"
            source.write_text(HEADER + "\n1,0,0,1,\n")
            command = [sys.executable, str(SCRIPT), str(source), str(target)]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(target.exists())
            good = command + ["--vehicle", "plane"]
            self.assertEqual(subprocess.run(good, capture_output=True).returncode, 0)
            prior = target.read_bytes()
            self.assertNotEqual(subprocess.run(good, capture_output=True).returncode, 0)
            self.assertEqual(target.read_bytes(), prior)
            same = [sys.executable, str(SCRIPT), str(source), str(source), "--vehicle", "plane"]
            original = source.read_bytes()
            self.assertNotEqual(subprocess.run(same, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), original)
            source.write_text(HEADER + "\n1,0,0,1,\n2,1,2,1,\n")
            bad_target = Path(directory) / "rejected.csv"
            command[3] = str(bad_target)
            self.assertNotEqual(subprocess.run(command + ["--vehicle", "plane"], capture_output=True).returncode, 0)
            self.assertFalse(bad_target.exists())




if __name__ == "__main__":
    unittest.main()
