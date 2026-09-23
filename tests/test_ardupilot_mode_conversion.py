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


def write_csv(path, text):
    """Write the composed newlines verbatim; Path.write_text has no newline= before 3.10."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


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
            if os.environ.get("MUSUBI_MAVLOG_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "input.jsonl"; path.write_bytes(original)
                    command = [os.environ["MUSUBI_MAVLOG_READER"],
                        str(ROOT / "profiles/declared/arduplane-jsonl/profile.toml"), str(path)]
                    self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
                    path.write_bytes(safe)
                    report_raw = subprocess.run(command, capture_output=True, check=True).stdout
            with self.assertRaises(ValueError): bridge.convert(report_raw, mode_vehicle="plane", record="parm-4.3.1")
            output, _ = bridge.convert(report_raw, mode_vehicle="plane", record="parm-4.3.1", preserve_nonfinite_default=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(rows[0]["reported_parameter_default_state"], "NONFINITE_UNUSABLE")
            self.assertEqual(rows[0]["Default"], token)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode/parameter-profile.toml"),
                        str(path), "--preserve-nonfinite-as-text"], capture_output=True, check=True).stdout)
                    self.assertEqual(common["main_rows"], 1)
                    self.assertEqual(common["observations"][0]["fields"]["Value"], 2.5)
                    self.assertIsInstance(common["observations"][0]["fields"]["Default"], str)
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
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "out.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/ardupilot-mode" / profile),
                            str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                        self.assertEqual(common["main_rows"], 2)
                        self.assertEqual(common["observations"][0]["fields"][field], expected)
                        self.assertEqual(common["observations"][1]["fields"]["Extra"], "different")
                        self.assertEqual(common["observations"][0]["fields"]["TimeUS"], time)
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
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "out.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared" / profile), str(path),
                            "--allow-equal-time"], check=True, capture_output=True).stdout)
                        self.assertEqual(common["main_rows"], expected_count)
                        self.assertEqual(common["observations"][0]["fields"][field], expected)
                        self.assertEqual(common["observations"][0]["fields"]["source_report_sha256"], report["source_report_sha256"])

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
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode" / profile),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"][field], expected)
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
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode/parameter-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["Value"], -2.5)
                    self.assertEqual(common["observations"][1]["fields"]["Default"], 3)

    def test_practical_parameter_snapshot_and_update_reuse(self):
        for count, sign in ((6000, -1), (2, 1)):
            source = "# retained capture note\n" + "".join(f"P{i:05d} {sign * i}.000000\n" for i in range(count))
            self.assertLess(len(source.encode()), 256000)
            snapshot = self.converter.convert_parameter_snapshot(source, 1000)
            updates = "TimeUS,Name,Value,Default,Extra\n" + "".join(
                f"1000,P{i % 2:05d},{sign * i}.000000,1.000000,kept\n" for i in range(count))
            converted = self.converter.convert(updates, "plane", "parm-4.3.1")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    for output, profile, field in ((snapshot, "parameter-snapshot.toml", "reported_parameter_value"),
                                                   (converted, "parameter-profile.toml", "Value")):
                        path = Path(directory) / "out.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/ardupilot-mode" / profile),
                            str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                        self.assertEqual(common["main_rows"], count)
                        self.assertEqual(common["observations"][-1]["fields"][field], sign * (count - 1))
                        self.assertEqual(common["platform_domain"], "Unknown")
                        if profile == "parameter-snapshot.toml":
                            retained = common["observations"][0]["fields"]["source_document_hex"]
                            self.assertEqual(bytes.fromhex(retained[4:]).decode(), source)
                        else:
                            self.assertEqual(common["observations"][-1]["fields"]["Extra"], "kept")
                            self.assertEqual(common["observations"][-1]["fields"]["Name"], f"P{(count - 1) % 2:05d}")

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
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode/parameter-snapshot.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["reported_parameter_value"], expected)
                    self.assertIsNone(common["observations"][0]["anchor_unix_us"])

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
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "pm.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"),
                            str(path)], capture_output=True, check=True).stdout)
                        self.assertEqual(common["main_rows"], 1)
                        self.assertEqual(common["platform_domain"], "Unknown")
                        self.assertEqual(common["observations"][0]["fields"]["reported_pm_scheduler_load"], load / 1000)
                        self.assertEqual(common["observations"][0]["t_boot_us"], start)
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
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "parameters.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode/parameter-profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    for observation, original in zip(common["observations"], parsed):
                        self.assertEqual(observation["fields"]["Name"], original["Name"])
                        self.assertEqual(observation["fields"]["Value"], float(original["Value"]))
                        self.assertEqual(observation["t_boot_us"], int(original["TimeUS"]))
                        self.assertIsNone(observation["anchor_unix_us"])

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
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            for source in [source, header + "200,0,1,0,3,unused-value\n"]:
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "arm.csv"; path.write_text(self.converter.convert(source, "plane", "arm"))
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertIn(common["observations"][0]["fields"]["reported_arm_state"], ["ARMED_REPORTED", "DISARMED_REPORTED"])

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
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "arm.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"),
                            str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                        self.assertEqual(common["main_rows"], len(rows))
                        self.assertEqual(common["platform_domain"], "Unknown")
                        for observation, row in zip(common["observations"], rows):
                            self.assertEqual(observation["fields"]["reported_arm_schema"], vehicle)
                            self.assertEqual(observation["fields"]["Extra"], row["Extra"])
                            self.assertEqual(observation["t_boot_us"], int(row["TimeUS"]))
            for bad in ("1,2,0,0,0,x", "1,1,4294967296,0,0,x", "1,0,0,2,0,x", "1,0,0,0,256,x"):
                with self.assertRaises(ValueError):
                    self.converter.convert(header + bad, vehicle, "arm")
            with self.assertRaises(ValueError):
                self.converter.convert("TimeUS,Name,Value\n1,TEST,1\n", vehicle, "parm-4.2.3")

    @classmethod
    def setUpClass(cls):
        cls.converter = load(SCRIPT)

    def test_reported_arm_fields_carry_their_declared_meaning_to_the_reader(self):
        """The profile the case prescribes for ARM must declare what those fields mean.

        Their meaning was stated only in the case prose, so a reader reported `reported_arm_checks_mask`
        and the rest with no unit at all. The mask statement matters most: a zero at disarm is what the
        writer records, not evidence that a check passed.
        """
        header = "TimeUS,ArmState,ArmChecks,Forced,Method\n"
        source = header + "1000,1,4294967295,0,0\n2000,0,0,1,5\n"
        profile = ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"
        output = self.converter.convert(source, "copter-4.5.5", "arm")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((rows[1]["reported_arm_state"], rows[1]["reported_arm_checks_mask"],
                          rows[1]["reported_arm_forced"], rows[1]["reported_arm_method_code"]),
                         ("DISARMED_REPORTED", "0", "true", "5"))
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("declared-meaning check needs the built reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "arm.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(profile),
                                                str(path), "--allow-equal-time"],
                                               capture_output=True, check=True).stdout)
        units = common["profile_units"]
        for column in ("ArmState", "ArmChecks", "Forced", "Method", "reported_arm_state",
                       "reported_arm_forced", "reported_arm_checks_mask", "reported_arm_method_code",
                       "reported_arm_schema"):
            self.assertIn(column, units, "every emitted arm field needs a declared meaning")
            self.assertTrue(units[column])
        mask = units["reported_arm_checks_mask"]
        self.assertIn("zero_mask_on_a_disarm", mask)
        self.assertIn("not_evidence_that_any_check_passed", mask)
        self.assertIn("never_a_command_an_enablement_a_safety_verdict_or_a_physical_actuator_state",
                      units["reported_arm_state"])
        self.assertIn("kept_raw_and_unnamed_here_not_a_verified_cause",
                      units["reported_arm_method_code"])
        self.assertIn("not_an_authority_permission_or_override_claim", units["reported_arm_forced"])
        for column in ("ArmState", "ArmChecks", "Forced", "Method"):
            self.assertIn("exactly_as_logged", units[column])
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertEqual(common["observations"][1]["clock_basis"], "BootRelative")
        fields = common["observations"][1]["fields"]
        self.assertEqual((fields["reported_arm_checks_mask"], fields["reported_arm_method_code"],
                          fields["reported_arm_forced"], fields["reported_arm_state"]),
                         (0, 5, "true", "DISARMED_REPORTED"))
        self.assertEqual(common["observations"][1]["t_boot_us"], 2000)

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
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "mode.csv"; source.write_text(output)
                    result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"),
                        str(source), "--allow-equal-time"], capture_output=True, check=True)
                    common = json.loads(result.stdout)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["reported_mode_name"], label)
                    self.assertIsNone(common["observations"][0]["anchor_unix_us"])
        with self.assertRaises(ValueError):
            self.converter.convert(HEADER + "\n1,0,0,1,\n", "sub-unknown")

    def test_marine_release_aliases_reuse_verified_tables_without_new_meaning(self):
        """Rover-4.5.7 and Sub-4.7.1 carry the tables already adopted at the older pins."""
        MODE_SCHEMAS = self.converter.MODE_SCHEMAS
        self.assertEqual(MODE_SCHEMAS["rover-4.5.7"], MODE_SCHEMAS["rover-4.5.5"])
        self.assertEqual(MODE_SCHEMAS["sub-4.7.1"], MODE_SCHEMAS["sub-4.5.7"])
        self.assertEqual(MODE_SCHEMAS["rover-4.5.7"][0], "MANUAL")
        self.assertEqual(MODE_SCHEMAS["sub-4.7.1"][0], "STABILIZE")
        self.assertEqual(MODE_SCHEMAS["sub-4.7.1"][19], "MANUAL")
        self.assertEqual(MODE_SCHEMAS["rover-4.5.7"][10], "AUTO")
        self.assertEqual(MODE_SCHEMAS["sub-4.7.1"][3], "AUTO")
        self.assertNotIn(19, MODE_SCHEMAS["rover-4.5.7"])
        self.assertNotIn(10, MODE_SCHEMAS["sub-4.7.1"])

    def test_rover_four_five_release_points_share_one_verified_table(self):
        """Rover 4.5.0..4.5.5 carry one MODE/ARM/PARM meaning because their selected writer
        files are byte-identical at those six release commits - not because names match.

        Rover/mode.h (33947 B, sha256 2df7e74a...) and AP_Logger/LogStructure.h (47880 B,
        sha256 df93d79e...) are identical at 53ad2c2a, 71a2f169, 291be848, 548b0b2c,
        fe4831dd and 9064e226. No experiment per release: the table identity is asserted
        once and the record paths are exercised at differing pins.
        """
        MODE_SCHEMAS = self.converter.MODE_SCHEMAS
        points = ["rover-4.5.0", "rover-4.5.1", "rover-4.5.2", "rover-4.5.3",
                  "rover-4.5.4", "rover-4.5.5"]
        self.assertEqual([f"rover-{p}" for p in self.converter.ROVER_45_RELEASES], points)
        for vehicle in points:
            self.assertEqual(MODE_SCHEMAS[vehicle], MODE_SCHEMAS["rover-4.5.5"])
            self.assertIn(vehicle, self.converter.ARM_SCHEMAS)
            self.assertEqual(self.converter.PARAM_SCHEMAS["parm-" + vehicle],
                             self.converter.PARAM_SCHEMAS["parm-4.3.1"])
            self.assertEqual(self.converter.PARAM_VEHICLES["parm-" + vehicle], vehicle)
        self.assertEqual(MODE_SCHEMAS["rover-4.5.2"][12], "SMART_RTL")
        self.assertNotIn(2, MODE_SCHEMAS["rover-4.5.3"])
        self.assertNotIn("rover-4.5.6", MODE_SCHEMAS)
        self.assertNotIn("rover-4.5.6", self.converter.ARM_SCHEMAS)
        self.assertNotIn("parm-rover-4.5.6", self.converter.PARAM_SCHEMAS)
        self.assertNotIn("copter-4.4.1", MODE_SCHEMAS)  # another vehicle's release, unqualified
        self.assertNotIn("rover-4.4.0", self.converter.ARM_SCHEMAS)  # MODE-only, unchanged
        self.assertNotIn("parm-rover-4.4.0", self.converter.PARAM_SCHEMAS)
        mode_source = (HEADER + "\n100,4,4,1,kept\n200,8,8,0,gate\n300,13,13,2,unknown\n")
        rows = list(csv.DictReader(io.StringIO(
            self.converter.convert(mode_source, "rover-4.5.2", "mode"))))
        self.assertEqual([r["reported_mode_name"] for r in rows], ["HOLD", "DOCK", "UNKNOWN_13"])
        self.assertEqual([r["reported_mode_known"] for r in rows], ["true", "true", "false"])
        self.assertEqual({r["reported_mode_schema"] for r in rows}, {"rover-4.5.2"})
        self.assertEqual(rows[0]["TimeUS"], "100")  # boot microseconds, not a wall clock
        self.assertEqual(rows[2]["Extra"], "unknown")
        arm_header = "TimeUS,ArmState,ArmChecks,Forced,Method,Extra\n"
        arm_source = arm_header + "400,1,305419896,0,17,kept\n400,0,0,1,0,other\n"
        arm_rows = list(csv.DictReader(io.StringIO(
            self.converter.convert(arm_source, "rover-4.5.3", "arm"))))
        self.assertEqual([r["reported_arm_state"] for r in arm_rows],
                         ["ARMED_REPORTED", "DISARMED_REPORTED"])
        self.assertEqual(arm_rows[0]["reported_arm_checks_mask"], "305419896")
        self.assertEqual(arm_rows[0]["reported_arm_method_code"], "17")
        self.assertEqual(arm_rows[1]["reported_arm_forced"], "true")
        self.assertEqual({r["reported_arm_schema"] for r in arm_rows}, {"rover-4.5.3"})
        parm_rows = list(csv.DictReader(io.StringIO(self.converter.convert(
            "TimeUS,Name,Value,Default\n500,WP_SPEED,2.5,4\n", "rover-4.5.1",
            "parm-rover-4.5.1"))))
        self.assertEqual(parm_rows[0]["reported_parameter_schema"], "parm-rover-4.5.1")
        self.assertEqual(parm_rows[0]["reported_parameter_default_present"], "true")
        self.assertEqual((parm_rows[0]["Name"], parm_rows[0]["Value"]), ("WP_SPEED", "2.5"))
        for vehicle, record in (("rover-4.5.6", "mode"), ("rover-4.5.6", "arm"),
                                ("copter-4.4.1", "mode")):
            with self.subTest(vehicle=vehicle, record=record), self.assertRaises(Exception):
                self.converter.convert(mode_source if record == "mode" else arm_source,
                                       vehicle, record)
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; common-output assertions NOT RUN")
        profile = str(ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml")
        with tempfile.TemporaryDirectory() as directory:
            cases = (("rover-4.5.4", "mode", HEADER + "\n700,9,9,4,independent\n",
                      "reported_mode_name", "CIRCLE"),
                     ("rover-4.5.0", "arm", arm_header + "800,1,7,1,4,independent\n",
                      "reported_arm_state", "ARMED_REPORTED"))
            for index, (vehicle, record, source, key, expected) in enumerate(cases):
                path = Path(directory) / f"rover{index}.csv"
                path.write_text(self.converter.convert(source, vehicle, record))
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"], profile, str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
                fields = common["observations"][0]["fields"]
                self.assertEqual(common["main_rows"], 1)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(fields[key], expected)
                self.assertEqual(fields[f"reported_{record}_schema"], vehicle)
                self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
                units = common["profile_units"]
                self.assertEqual(units[f"reported_{record}_schema"].split("_")[0], "explicit")
                self.assertEqual(units["TimeUS"], "us_since_boot")
            parm_path = Path(directory) / "rover-parm.csv"
            parm_path.write_text(self.converter.convert(
                "TimeUS,Name,Value,Default,Extra\n"
                "900,ATC_STR_RAT_P,0.125,0.2,independent\n"
                "900,CRUISE_SPEED,-1,2.5,second\n",
                "rover-4.5.3", "parm-rover-4.5.3"))
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"],
                 str(ROOT / "profiles/declared/ardupilot-mode/parameter-profile.toml"),
                 str(parm_path), "--allow-equal-time"], capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["platform_domain"], "Unknown")
            first, second = (o["fields"] for o in common["observations"])
            self.assertEqual((first["Name"], first["Value"], first["Default"]),
                             ("ATC_STR_RAT_P", 0.125, 0.2))
            self.assertEqual((second["Name"], second["Value"], second["Default"]),
                             ("CRUISE_SPEED", -1, 2.5))
            self.assertEqual({o["fields"]["reported_parameter_schema"] for o in common["observations"]},
                             {"parm-rover-4.5.3"})
            self.assertEqual({o["fields"]["reported_parameter_default_present"]
                              for o in common["observations"]}, {"true"})
            self.assertEqual({o["clock_basis"] for o in common["observations"]}, {"BootRelative"})
            self.assertEqual({o["t_ms"] for o in common["observations"]}, {0})  # 900 us since boot
            self.assertEqual(second["Extra"], "second")
            units = common["profile_units"]
            self.assertEqual(units["TimeUS"], "us_since_boot")
            self.assertEqual(units["Value"], "reported_parameter_value_unit_not_inferred")
            self.assertEqual(units["Default"], "reported_board_config_default_unit_not_inferred")
            self.assertIn("not_firmware_attestation", units["reported_parameter_schema"])
            self.assertIn("not_default_validity", units["reported_parameter_default_present"])

    def test_copter_four_five_release_points_share_one_verified_table(self):
        """Copter 4.5.0..4.5.5 carry one MODE/ARM/PARM meaning on the same basis as Rover:
        byte identity of the selected files at those six release commits.

        ArduCopter/mode.h (68981 B, sha256 b9ca890b...), ArduCopter/mode.cpp (38609 B, sha256
        c7fb5d1b...) and ArduCopter/AP_Arming.cpp (28991 B, sha256 958c51e9...) are identical
        at 53ad2c2a, 71a2f169, 291be848, ee14ff38, fd1bcc61 and 142aece2, and the shared
        AP_Logger files are the same blobs already pinned for Rover. Copter's numbers are
        still Copter's own, so the tables are asserted not to have merged.
        """
        MODE_SCHEMAS = self.converter.MODE_SCHEMAS
        points = ["copter-4.5.0", "copter-4.5.1", "copter-4.5.2", "copter-4.5.3",
                  "copter-4.5.4", "copter-4.5.5"]
        self.assertEqual([f"copter-{p}" for p in self.converter.COPTER_45_RELEASES], points)
        for vehicle in points:
            self.assertEqual(MODE_SCHEMAS[vehicle], MODE_SCHEMAS["copter-4.5.5"])
            self.assertIn(vehicle, self.converter.ARM_SCHEMAS)
            self.assertEqual(self.converter.PARAM_SCHEMAS["parm-" + vehicle],
                             self.converter.PARAM_SCHEMAS["parm-4.3.1"])
            self.assertEqual(self.converter.PARAM_VEHICLES["parm-" + vehicle], vehicle)
        self.assertEqual(MODE_SCHEMAS["copter-4.5.2"][0], "STABILIZE")
        self.assertEqual(MODE_SCHEMAS["rover-4.5.2"][0], "MANUAL")
        self.assertEqual(MODE_SCHEMAS["copter-4.5.3"][21], "SMART_RTL")
        self.assertEqual(MODE_SCHEMAS["rover-4.5.3"][12], "SMART_RTL")
        self.assertNotIn(8, MODE_SCHEMAS["copter-4.5.4"])  # DOCK 8 is a Rover number
        self.assertNotIn(21, MODE_SCHEMAS["rover-4.5.4"])
        self.assertNotIn("copter-4.5.6", MODE_SCHEMAS)
        self.assertNotIn("copter-4.5.6", self.converter.ARM_SCHEMAS)
        self.assertNotIn("parm-copter-4.5.6", self.converter.PARAM_SCHEMAS)
        self.assertNotIn("copter-4.4.1", MODE_SCHEMAS)
        self.assertNotIn("parm-copter-4.4.0", self.converter.PARAM_SCHEMAS)
        mode_source = (HEADER + "\n100,17,17,1,kept\n200,27,27,6,auto_rtl\n300,8,8,2,unknown\n")
        rows = list(csv.DictReader(io.StringIO(
            self.converter.convert(mode_source, "copter-4.5.4", "mode"))))
        self.assertEqual([r["reported_mode_name"] for r in rows],
                         ["BRAKE", "AUTO_RTL", "UNKNOWN_8"])
        self.assertEqual([r["reported_mode_known"] for r in rows], ["true", "true", "false"])
        self.assertEqual({r["reported_mode_schema"] for r in rows}, {"copter-4.5.4"})
        self.assertEqual(rows[0]["TimeUS"], "100")  # boot microseconds, not a wall clock
        self.assertEqual(rows[2]["Extra"], "unknown")
        arm_header = "TimeUS,ArmState,ArmChecks,Forced,Method,Extra\n"
        arm_source = arm_header + "1100,1,2271560481,0,82,kept\n1100,0,0,1,3,other\n"
        arm_rows = list(csv.DictReader(io.StringIO(
            self.converter.convert(arm_source, "copter-4.5.1", "arm"))))
        self.assertEqual([r["reported_arm_state"] for r in arm_rows],
                         ["ARMED_REPORTED", "DISARMED_REPORTED"])
        self.assertEqual(arm_rows[0]["reported_arm_checks_mask"], "2271560481")
        self.assertEqual(arm_rows[0]["reported_arm_method_code"], "82")
        self.assertEqual(arm_rows[1]["reported_arm_forced"], "true")
        self.assertEqual({r["reported_arm_schema"] for r in arm_rows}, {"copter-4.5.1"})
        parm_rows = list(csv.DictReader(io.StringIO(self.converter.convert(
            "TimeUS,Name,Value,Default\n1200,ATC_RAT_PIT_P,0.135,0.135\n", "copter-4.5.2",
            "parm-copter-4.5.2"))))
        self.assertEqual(parm_rows[0]["reported_parameter_schema"], "parm-copter-4.5.2")
        self.assertEqual(parm_rows[0]["reported_parameter_default_present"], "true")
        self.assertEqual((parm_rows[0]["Name"], parm_rows[0]["Value"]),
                         ("ATC_RAT_PIT_P", "0.135"))
        for vehicle, record in (("copter-4.5.6", "mode"), ("copter-4.5.6", "arm"),
                                ("copter-4.4.1", "mode"), ("copter-4.5.6", "parm-copter-4.5.6"),
                                ("copter-4.5.2", "parm-copter-4.5.3")):
            with self.subTest(vehicle=vehicle, record=record), self.assertRaises(Exception):
                self.converter.convert(arm_source if record == "arm" else mode_source,
                                       vehicle, record)
        with self.assertRaises(ValueError):  # Mode and ModeNum are the same producer byte
            self.converter.convert(HEADER + "\n400,17,18,1,corrupt\n", "copter-4.5.3", "mode")
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; common-output assertions NOT RUN")
        profile = str(ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml")
        with tempfile.TemporaryDirectory() as directory:
            cases = (("copter-4.5.3", "mode", HEADER + "\n1300,26,26,7,held_out\n",
                      "reported_mode_name", "AUTOROTATE"),
                     ("copter-4.5.2", "arm", arm_header + "1400,1,19,1,11,held_out\n",
                      "reported_arm_state", "ARMED_REPORTED"))
            for index, (vehicle, record, source, key, expected) in enumerate(cases):
                path = Path(directory) / f"copter{index}.csv"
                path.write_text(self.converter.convert(source, vehicle, record))
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"], profile, str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
                fields = common["observations"][0]["fields"]
                self.assertEqual(common["main_rows"], 1)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(fields[key], expected)
                self.assertEqual(fields[f"reported_{record}_schema"], vehicle)
                self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
                self.assertEqual(common["profile_units"]["TimeUS"], "us_since_boot")
            parm_path = Path(directory) / "copter-parm.csv"
            parm_path.write_text(self.converter.convert(
                "TimeUS,Name,Value,Default,Extra\n"
                "1500,PSC_POSXY_P,1.875,1,held_out\n"
                "1500,WPNAV_SPEED_DN,-150,150,second\n",
                "copter-4.5.0", "parm-copter-4.5.0"))
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"],
                 str(ROOT / "profiles/declared/ardupilot-mode/parameter-profile.toml"),
                 str(parm_path), "--allow-equal-time"], capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            first, second = (o["fields"] for o in common["observations"])
            self.assertEqual((first["Name"], first["Value"], first["Default"]),
                             ("PSC_POSXY_P", 1.875, 1))
            self.assertEqual((second["Name"], second["Value"], second["Default"]),
                             ("WPNAV_SPEED_DN", -150, 150))
            self.assertEqual({o["fields"]["reported_parameter_schema"] for o in common["observations"]},
                             {"parm-copter-4.5.0"})
            self.assertEqual({o["clock_basis"] for o in common["observations"]}, {"BootRelative"})
            self.assertEqual(second["Extra"], "second")
            self.assertEqual(common["profile_units"]["Value"],
                             "reported_parameter_value_unit_not_inferred")

    def test_reported_build_identity_reaches_common_output_without_inventing_a_revision(self):
        """VER carries a release channel and a board target; it carries no hardware revision."""
        profile = ROOT / "profiles/declared/marine-ardu-reuse/profile.toml"
        header = "TimeUS,BT,BST,Maj,Min,Pat,FWT,GH,FWS,APJ,BU\n"
        cases = ((255, "OFFICIAL", 9, "9", 9),
                 (128, "BETA", 0, "ZERO_OR_NOT_PROVIDED", "ZERO_OR_NOT_PROVIDED"))
        with tempfile.TemporaryDirectory() as directory:
            for index, (code, channel, apj, expected_target, common_target) in enumerate(cases):
                text = header + f"1000,5,17,4,5,7,{code},305419896,ArduRover V4.5.7,{apj},12\n"
                converted = self.converter.convert(text, "rover-4.5.7", "ver")
                row = next(csv.DictReader(io.StringIO(converted)))
                self.assertEqual(row["reported_release_channel"], channel)
                self.assertEqual(row["reported_release_channel_code"], str(code))
                self.assertEqual(row["reported_board_target_id"], expected_target)
                self.assertEqual((row["BT"], row["BST"], row["GH"]), ("5", "17", "305419896"))
                self.assertNotIn("hardware_revision", converted)
                self.assertNotIn("build_number", converted)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    target = Path(directory) / f"ver{index}.csv"
                    target.write_text(converted)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(profile), str(target)], check=True, capture_output=True).stdout)
                    fields = common["observations"][0]["fields"]
                    self.assertEqual(fields["reported_release_channel"], channel)
                    self.assertEqual(fields["reported_board_target_id"], common_target)
                    self.assertEqual(fields["reported_firmware_version"], "4.5.7")
                    units = common["profile_units"]
                    self.assertIn("never_a_certification", units["reported_release_channel"])
                    self.assertIn("qualifies_no_intermediate_value", units["reported_release_channel"])
                    self.assertIn("not_a_hardware_revision", units["reported_board_target_id"])
                    self.assertIn("cannot_tell_them_apart", units["reported_board_target_id"])
                    self.assertIn("neither_a_build_number_nor_a_full_image_hash", units["GH"])
                    self.assertIn("not_a_hardware_revision", units["BT"])
            for code, expected in ((0, "DEV"), (64, "ALPHA"), (128, "BETA"), (192, "RC"),
                                   (255, "OFFICIAL"), (1, "UNKNOWN_1"), (63, "UNKNOWN_63"),
                                   (65, "UNKNOWN_65"), (193, "UNKNOWN_193"), (200, "UNKNOWN_200")):
                odd = self.converter.convert(
                    header + f"1000,5,17,4,5,7,{code},1,ArduRover V4.5.7,9,12\n",
                    "rover-4.5.7", "ver")
                row = next(csv.DictReader(io.StringIO(odd)))
                self.assertEqual((row["reported_release_channel"],
                                  row["reported_release_channel_code"]), (expected, str(code)))
            older = self.converter.convert(
                "TimeUS,Maj,Min,Pat,FWS\n1000,4,5,7,ArduRover V4.5.7\n", "rover-4.5.7", "ver")
            older_row = next(csv.DictReader(io.StringIO(older)))
            self.assertEqual((older_row["reported_release_channel"],
                              older_row["reported_release_channel_code"],
                              older_row["reported_board_target_id"]), ("", "", ""))
            for bad in ("1000,5,17,4,5,7,notanumber,1,ArduRover V4.5.7,9,12",
                        "1000,5,17,4,5,7,256,1,ArduRover V4.5.7,9,12",
                        "1000,5,17,4,5,7,255,1,ArduRover V4.5.7,70000,12"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    self.converter.convert(header + bad + "\n", "rover-4.5.7", "ver")

    def test_two_marine_layouts_reach_common_observation_with_reported_identity(self):
        """BlueBoat and BlueROV2 upstream families through the adopted reader."""
        profile = ROOT / "profiles/declared/marine-ardu-reuse/profile.toml"
        mode_header = "TimeUS,Mode,ModeNum,Rsn\n"
        ver_header = "TimeUS,BT,BST,Maj,Min,Pat,FWT,GH,FWS,APJ,BU,FV,IMI,ICI\n"
        cases = (("rover-4.5.7", "ArduRover V4.5.7", (0, 4, 10, 16), ("MANUAL", "HOLD", "AUTO", "INITIALISING"), "4,5,7"),
                 ("sub-4.7.1", "ArduSub V4.7.1", (0, 9, 19, 21), ("STABILIZE", "SURFACE", "MANUAL", "SURFTRAK"), "4,7,1"))
        with tempfile.TemporaryDirectory() as directory:
            for vehicle, firmware, numbers, names, version in cases:
                modes = mode_header + "".join(
                    f"{(index + 1) * 1000},{number},{number},{index}\n"
                    for index, number in enumerate(numbers))
                converted = self.converter.convert(modes, vehicle, "mode")
                rows = list(csv.DictReader(io.StringIO(converted)))
                self.assertEqual([row["reported_mode_name"] for row in rows], list(names))
                self.assertTrue(all(row["reported_mode_known"] == "true" for row in rows))
                self.assertTrue(all(row["reported_mode_schema"] == vehicle for row in rows))
                major, minor, patch = version.split(",")
                identity = self.converter.convert(ver_header + f"500,1,2,{major},{minor},{patch},3,9,{firmware},0,1,2,3,4\n",
                                   vehicle, "ver")
                record = next(csv.DictReader(io.StringIO(identity)))
                self.assertEqual(record["reported_firmware_family"], firmware.split()[0])
                self.assertEqual(record["reported_firmware_version"], version.replace(",", "."))
                self.assertEqual(record["declared_vehicle_agreement"], "AGREES_WITH_DECLARED_RELEASE")
                self.assertIn("NOT_PRODUCT_OR_VENDOR_IDENTITY", record["reported_vehicle_identity_basis"])
                self.assertEqual(record["FWS"], firmware)
                self.assertEqual(record["GH"], "9")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    for name, text in (("mode", converted), ("ver", identity)):
                        path = Path(directory) / f"{vehicle}-{name}.csv"
                        write_csv(path, text)
                        common = json.loads(subprocess.run(
                            [os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(path),
                             "--allow-equal-time"], check=True, capture_output=True).stdout)
                        self.assertEqual(common["platform_domain"], "Unknown")
                        for observation in common["observations"]:
                            self.assertEqual(observation["clock_basis"], "BootRelative")
                        if name == "mode":
                            self.assertEqual(common["main_rows"], len(numbers))
                            self.assertEqual([o["fields"]["reported_mode_name"]
                                              for o in common["observations"]], list(names))
                        else:
                            self.assertEqual(common["observations"][0]["fields"]
                                             ["reported_firmware_family"], firmware.split()[0])

    def test_logged_identity_is_checked_against_the_declaration_and_unknown_preserved(self):
        ver = "TimeUS,Maj,Min,Pat,FWS\n"
        for vehicle, firmware, version in (("sub-4.7.1", "ArduRover V4.5.7", "4,5,7"),
                                           ("rover-4.5.7", "ArduSub V4.7.1", "4,7,1"),
                                           ("plane", "ArduSub V4.7.1", "4,7,1")):
            major, minor, patch = version.split(",")
            with self.assertRaises(ValueError, msg=f"{vehicle} vs {firmware}"):
                self.converter.convert(ver + f"1,{major},{minor},{patch},{firmware}\n", vehicle, "ver")
        with self.assertRaises(ValueError):
            self.converter.convert(ver + "1,4,5,6,ArduRover V4.5.7\n", "rover-4.5.7", "ver")
        for bad in ("1,4,5,7,\n", "1,256,5,7,ArduRover V256.5.7\n"):
            with self.assertRaises(ValueError):
                self.converter.convert(ver + bad, "rover-4.5.7", "ver")
        custom = next(csv.DictReader(io.StringIO(
            self.converter.convert(ver + "1,4,5,7,Rover 4.5.7\n", "rover-4.5.7", "ver"))))
        self.assertEqual(custom["reported_firmware_family"], "")
        self.assertEqual(custom["declared_vehicle_agreement"],
                         "CUSTOM_FIRMWARE_STRING_NO_FAMILY_ESTABLISHED_RETAINED")
        self.assertEqual(custom["reported_vehicle_identity_basis"],
                         "CUSTOM_FIRMWARE_STRING_NO_UPSTREAM_FAMILY_ESTABLISHED")
        self.assertEqual(custom["reported_firmware_version"], "4.5.7")
        self.assertEqual(custom["FWS"], "Rover 4.5.7")
        row = next(csv.DictReader(io.StringIO(
            self.converter.convert(ver + "1,4,5,7,ArduBoatX V4.5.7\n", "rover-4.5.7", "ver"))))
        self.assertEqual(row["reported_firmware_family"], "ArduBoatX")
        self.assertEqual(row["declared_vehicle_agreement"], "UNKNOWN_FIRMWARE_FAMILY_RETAINED")

    def test_release_selector_binds_the_version_and_the_prefix_is_anchored(self):
        """An exact release selector binds Maj/Min/Pat; a legacy name binds family only."""
        ver = "TimeUS,Maj,Min,Pat,FWS\n"

        def agreement(vehicle, firmware, version):
            major, minor, patch = version
            return next(csv.DictReader(io.StringIO(self.converter.convert(
                ver + f"1,{major},{minor},{patch},{firmware}\n", vehicle, "ver"))))\
                ["declared_vehicle_agreement"]

        self.assertEqual(agreement("rover-4.5.7", "ArduRover V4.5.7", (4, 5, 7)),
                         "AGREES_WITH_DECLARED_RELEASE")
        for suffix in ("-beta1", "-rc2", "-dev", "+build7"):
            self.assertEqual(agreement("sub-4.7.1", f"ArduSub V4.7.1{suffix}", (4, 7, 1)),
                             "AGREES_ON_NUMERIC_BASE_UNQUALIFIED_SUFFIX_NOT_EXACT_STABLE",
                             msg=suffix)
        record = next(csv.DictReader(io.StringIO(self.converter.convert(
            ver + "1,4,7,1,ArduSub V4.7.1-beta1\n", "sub-4.7.1", "ver"))))
        self.assertEqual(record["reported_firmware_suffix"], "-beta1")
        self.assertEqual(record["reported_firmware_version"], "4.7.1")
        self.assertEqual(record["FWS"], "ArduSub V4.7.1-beta1")
        bare = next(csv.DictReader(io.StringIO(self.converter.convert(
            ver + "1,4,7,1,ArduSub V4.7.1\n", "sub-4.7.1", "ver"))))
        self.assertEqual(bare["reported_firmware_suffix"], "")
        self.assertEqual(agreement("plane", "ArduPlane V4.5.5", (4, 5, 5)),
                         "AGREES_WITH_DECLARED_FAMILY_ONLY_NO_RELEASE_BOUND")
        self.assertEqual(agreement("plane", "ArduPlane V3.9.0", (3, 9, 0)),
                         "AGREES_WITH_DECLARED_FAMILY_ONLY_NO_RELEASE_BOUND")
        for vehicle, firmware, version in (("rover-4.5.7", "ArduRover V4.5.5", (4, 5, 5)),
                                           ("sub-4.7.1", "ArduSub V4.5.7", (4, 5, 7)),
                                           ("rover-4.5.5", "ArduRover V4.5.7", (4, 5, 7))):
            with self.assertRaises(ValueError, msg=f"{vehicle} vs {firmware}"):
                agreement(vehicle, firmware, version)
        annotated = next(csv.DictReader(io.StringIO(self.converter.convert(
            ver + "1,4,5,7,ArduRover V4.5.7 (5cbb35f5)\n", "rover-4.5.7", "ver"))))
        self.assertEqual(annotated["declared_vehicle_agreement"],
                         "AGREES_WITH_DECLARED_RELEASE_BUILD_COMMIT_NOT_CHECKED")
        self.assertEqual(annotated["reported_firmware_build_annotation"], "5cbb35f5")
        self.assertEqual(annotated["reported_firmware_suffix"], "")
        self.assertEqual(annotated["reported_firmware_family"], "ArduRover")
        both = next(csv.DictReader(io.StringIO(self.converter.convert(
            ver + "1,4,7,1,ArduSub V4.7.1-beta1 (deadbeef)\n", "sub-4.7.1", "ver"))))
        self.assertEqual(both["declared_vehicle_agreement"],
                         "AGREES_ON_NUMERIC_BASE_UNQUALIFIED_SUFFIX_NOT_EXACT_STABLE")
        self.assertEqual(both["reported_firmware_suffix"], "-beta1")
        self.assertEqual(both["reported_firmware_build_annotation"], "deadbeef")
        for firmware in ("log from ArduRover V4.5.7", "ArduRover V4.5.7 EXTRA",
                         "xArduRover V4.5.7", "ArduRover V4.5.7."):
            self.assertEqual(agreement("rover-4.5.7", firmware, (4, 5, 7)),
                             "CUSTOM_FIRMWARE_STRING_NO_FAMILY_ESTABLISHED_RETAINED",
                             msg=firmware)
        for firmware in ("A" * 64, "Ardu\tRover V4.5.7", 'ArduRover "V4.5.7"'):
            with self.assertRaises(ValueError, msg=firmware):
                agreement("rover-4.5.7", firmware, (4, 5, 7))

    def test_marine_electrical_reuses_the_instance_battery_selector(self):
        """The BAT writer at both pins emits Inst, so ardupilot-bat-inst is the selector."""
        battery = load(ROOT / "scripts/convert_ardupilot_battery_csv.py")
        header = "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH"
        rows = ("1000,0,15.8,16.0,3.25,120.5,1.75,21.5,0.012,87,1,100",
                "2000,0,15.6,15.9,4.00,130.0,1.80,21.6,0.012,85,1,100")
        converted = battery.convert(header + "\n" + "\n".join(rows) + "\n", "ardupilot-bat-inst")
        observed = list(csv.DictReader(io.StringIO(converted)))
        self.assertEqual([row["battery_voltage_v"] for row in observed], ["15.8", "15.6"])
        self.assertEqual([row["battery_current_a"] for row in observed], ["3.25", "4.0"])
        self.assertEqual(observed[0]["battery_consumed_ah"], "0.1205")
        self.assertEqual(observed[0]["battery_remaining_fraction"], "0.87")
        with self.assertRaises(ValueError):
            battery.convert(header + "\n" + rows[0] + "\n", "ardupilot-bat")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bat.csv"
                write_csv(path, converted)
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"],
                     str(ROOT / "profiles/declared/ardupilot-battery/profile.toml"),
                     str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], 15.8)
                self.assertEqual(common["platform_domain"], "Unknown")

    def test_marine_selectors_expose_only_the_records_that_actually_run(self):
        """ARM rests on its own LogStructure evidence; PARM and PM are not extended."""
        for vehicle in ("rover-4.5.7", "sub-4.7.1"):
            arm = self.converter.convert(
                "TimeUS,ArmState,ArmChecks,Forced,Method\n7,1,0,0,3\n", vehicle, "arm")
            record = next(csv.DictReader(io.StringIO(arm)))
            self.assertEqual(record["reported_arm_state"], "ARMED_REPORTED")
            self.assertEqual(record["reported_arm_schema"], vehicle)
            self.assertNotIn("parm-" + vehicle, self.converter.PARAM_SCHEMAS)
            with self.assertRaises(ValueError):
                self.converter.convert("TimeUS,Name,Value,Default\n1,P,2,3\n", vehicle, "parm-4.3.1")
            with self.assertRaises(ValueError):
                self.converter.convert(
                    "TimeUS,NLon,NLoop,MaxT,Mem,Load,ErrL,IntE,ErrC,SPIC,I2CC,I2CI,Ex\n"
                    "1,0,1,2,3,4,5,6,7,8,9,10,11\n", vehicle, "pm-4.3.1")
        unknown = next(csv.DictReader(io.StringIO(
            self.converter.convert("TimeUS,Mode,ModeNum,Rsn\n1,13,13,0\n", "sub-4.7.1", "mode"))))
        self.assertEqual((unknown["reported_mode_name"], unknown["reported_mode_known"]),
                         ("UNKNOWN_13", "false"))

    def test_practical_marine_mode_length_through_the_common_reader(self):
        count = 4000
        numbers = (0, 9, 19, 21)
        text = "TimeUS,Mode,ModeNum,Rsn\n" + "".join(
            f"{(index + 1) * 1000},{numbers[index % 4]},{numbers[index % 4]},{index % 7}\n"
            for index in range(count))
        converted = self.converter.convert(text, "sub-4.7.1", "mode")
        rows = list(csv.DictReader(io.StringIO(converted)))
        self.assertEqual(len(rows), count)
        self.assertEqual(rows[-1]["reported_mode_name"], "SURFTRAK")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "long.csv"
                write_csv(path, converted)
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"],
                     str(ROOT / "profiles/declared/marine-ardu-reuse/profile.toml"),
                     str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], count)
                self.assertEqual(common["platform_domain"], "Unknown")

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




class CopterConfigurationIsNotDistinguished(unittest.TestCase):
    """The `copter-*` pin names a firmware family and release, never an airframe.

    A family label can group helicopter and multirotor configurations. The pinned
    ArduCopter source builds **two** configurations out of one vehicle directory, selected at compile
    time: `#if FRAME_CONFIG == HELI_FRAME` appears three times in `ArduCopter/mode.h` at the archived
    Copter-4.5.0 pin (68 981 B, sha256 b9ca890b…72263c), giving `ModeStabilize_Heli`, `ModeAcro_Heli`
    and `AutoTune : AC_AutoTune_Heli` beside `AC_AutoTune_Multi`.

    Those heli classes **share their protocol numbers** with the multirotor ones -- STABILIZE is 0 and
    ACRO is 1 in both -- so a logged mode number cannot say which configuration ran. `AUTOROTATE = 26`
    sits in the enum unconditionally and is gated by `MODE_AUTOROTATE_ENABLED`, so its presence is
    suggestive at best and its absence proves nothing.

    This guard pins that blindness so no later change quietly infers rotor topology from the word
    "copter". It adds no column: the configuration is absent from the source, not from the output.
    """

    PROFILE = ROOT / "profiles/declared/ardupilot-mode/vehicle-profile.toml"

    def setUp(self):
        self.converter = load(SCRIPT)

    def rows(self, mode_number, vehicle="copter-4.5.3"):
        text = "TimeUS,Mode,ModeNum,Rsn\n100,%d,%d,1\n" % (mode_number, mode_number)
        return list(csv.DictReader(io.StringIO(self.converter.convert(text, vehicle, "mode"))))

    def test_the_declaration_says_the_pin_is_not_a_frame_configuration(self):
        declared = self.PROFILE.read_text(encoding="utf-8")
        for required in ("NOT_a_frame_or_airframe_configuration",
                         "TWO_configurations_from_ONE_vehicle_directory",
                         "SHARE_THEIR_PROTOCOL_NUMBERS",
                         "rotor_topology_is_NEVER_inferred_from_the_word_copter"):
            self.assertIn(required, declared, required)

    def test_shared_mode_numbers_carry_the_same_identity_whichever_configuration_ran(self):
        identities = set()
        for number, name in ((0, "STABILIZE"), (1, "ACRO"), (26, "AUTOROTATE")):
            with self.subTest(mode=name):
                row = self.rows(number)[0]
                self.assertEqual(row["reported_mode_name"], name)
                self.assertEqual(row["reported_mode_schema"], "copter-4.5.3")
                identities.add(row["reported_mode_schema"])
                self.assertEqual([key for key in row
                                  if "frame" in key.lower() or "config" in key.lower()], [])
        self.assertEqual(identities, {"copter-4.5.3"})

    def test_no_configuration_qualified_selector_exists_to_smuggle_a_topology_claim(self):
        for selector in ("copter-4.5.3-heli", "heli-4.5.3", "copter-heli-4.5.3",
                         "multirotor-4.5.3"):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                self.converter.convert("TimeUS,Mode,ModeNum,Rsn\n100,0,0,1\n", selector, "mode")

    def test_the_identity_reaches_common_output_without_a_configuration_field(self):
        output = self.converter.convert(
            "TimeUS,Mode,ModeNum,Rsn\n100,26,26,1\n", "copter-4.5.3", "mode")
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_text(output)
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(self.PROFILE), str(path),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
        fields = common["observations"][0]["fields"]
        self.assertEqual(common["main_rows"], 1)
        self.assertEqual(fields["reported_mode_schema"], "copter-4.5.3")
        self.assertEqual(fields["reported_mode_name"], "AUTOROTATE")
        self.assertEqual([key for key in fields
                          if "frame" in key.lower() or "config" in key.lower()], [])


class ParameterSnapshotDeclaredMeanings(unittest.TestCase):
    """The snapshot's qualifying columns must say what they mean in common output.

    The common reader does not enforce declaration -- it passes an undeclared column straight
    through -- and six of the eight columns this path writes had no declared meaning, including
    the two that carry the qualifications themselves: `parameter_unit_basis`, which is what says
    the native unit is undeclared, and `parameter_time_basis`, which is what says the row time is
    the caller's capture and not a device clock. A reader of common output saw those sentences
    with nothing stating what they were.
    """

    SNAPSHOT = ROOT / "profiles/declared/ardupilot-mode/parameter-snapshot.toml"
    EMITTED = ("record_time_us", "parameter_name", "reported_parameter_value",
               "parameter_unit_basis", "parameter_time_basis", "source_line",
               "source_value_hex", "source_document_hex")

    def snapshot(self, text):
        return list(csv.DictReader(io.StringIO(
            load(SCRIPT).convert_parameter_snapshot(text, 1_700_000_000_000_000))))

    def test_every_written_column_is_declared(self):
        import re
        declared = set(re.findall(r"^\s*([A-Za-z_][\w.-]*)\s*=",
                                  self.SNAPSHOT.read_text(encoding="utf-8").split("[units]")[1],
                                  re.M))
        self.assertEqual([name for name in self.EMITTED if name not in declared], [])

    def test_a_calibration_looking_name_is_still_only_an_undeclared_parameter(self):
        """The counterexample: this path does NOT turn a parameter into a calibration report.

        A name that reads like a magnetometer offset arrives as an opaque identifier whose unit
        the path states it does not know. Nothing here qualifies it as a bias or scale for a named
        sensor, gives it a reference, or makes two captures comparable as drift.
        """
        rows = self.snapshot("COMPASS_OFS_X 12.500000\nINS_ACCSCAL_X 1.002000\n")
        self.assertEqual([row["parameter_name"] for row in rows],
                         ["COMPASS_OFS_X", "INS_ACCSCAL_X"])
        for row in rows:
            self.assertEqual(row["parameter_unit_basis"], "NATIVE_PARAMETER_UNIT_UNDECLARED")
            self.assertEqual(row["parameter_time_basis"],
                             "CALLER_CAPTURE_NOT_DEVICE_OR_FILE_TIME")
        self.assertNotIn("sensor_identity", rows[0])
        self.assertTrue(rows[0]["source_document_hex"].startswith("hex:"))
        self.assertEqual(rows[1]["source_document_hex"], "")


if __name__ == "__main__":
    unittest.main()
