"""Authored native alog windows; no broker, vehicle or simulation execution."""
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.convert_imc_sensors import convert_alog, convert_node_report

ROOT = Path(__file__).resolve().parents[1]
HEADER = b"%% LOGSTART 1700000000\r\n%% DATATYPE MARKING ON\n"
NODE = "moos-ivp-node-report-f56c"
SCALAR = "moos-ivp-typed-alog-f56c"


def node(time="0.00001", name="alpha", speed=2, end="\n"):
    return (f"{time} NODE_REPORT pNodeReporter S:NAME={name},SPD={speed},HDG=180,X=1,Y=2,DEP=3,future=raw" + end).encode()


class AlogWindow(unittest.TestCase):
    def setUp(self):
        from scripts import convert_moos_alog_window
        self.module = convert_moos_alog_window

    def convert(self, path, start=2, stop=3, **changes):
        args = dict(start_line=start, stop_line=stop, source_format=NODE, variable="NODE_REPORT",
                    publisher="pNodeReporter", capture_time_us=7000,
                    unit_basis="producer-documentation-stated")
        args.update(changes)
        return self.module.convert_path(path, **args)

    def common(self, text, scalar=False):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required; CI supplies it")
        profile = "imc-sensors/alog-profile.toml" if scalar else "moos-node-report/profile.toml"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            result = subprocess.run([reader, str(ROOT / "profiles/declared" / profile),
                                     str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)

    def test_long_native_adjacent_windows_original_accounting_common(self):
        excluded = b"unparsed outside payload " + b"x" * 29900 + b"\n"
        selected = [node(), node(name="beta", speed=4, end="\r\n"),
                    node("-0.00001", name="gamma", speed=0, end="")]
        raw = HEADER + excluded * 600 + b"".join(selected)
        self.assertGreater(len(raw), 16 * 1024**2)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.alog"
            source.write_bytes(raw)
            collected = []
            for start, stop in ((602, 603), (603, 605)):
                with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
                    text, report = self.convert(source, start, stop)
                rows = list(csv.DictReader(io.StringIO(text)))
                window = report["line_selection"]
                self.assertEqual((window["source_lines"], window["selected_lines"], window["excluded_lines"]),
                                 (605, stop-start, 605-(stop-start)))
                self.assertEqual(window["source_bytes"], len(raw))
                self.assertEqual(window["outside_selection"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual(report["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["output_sha256"], hashlib.sha256(text.encode()).hexdigest())
                derived = HEADER + b"".join(selected[start-602:stop-602])
                self.assertEqual(report["derived_source_hex"], "hex:" + derived.hex())
                self.assertEqual(report["derived_source_sha256"], hashlib.sha256(derived).hexdigest())
                for entry in window["derived_lines"]:
                    begin = entry["source_byte_offset"]
                    self.assertEqual(len(raw[begin:begin+entry["source_line_bytes"]]), entry["source_line_bytes"])
                common = self.common(text)
                self.assertEqual(common["main_rows"], stop-start)
                for observation, row, index in zip(common["observations"], rows, range(start, stop)):
                    self.assertEqual(int(row["source_line"]), index+1)
                    self.assertEqual(int(row["source_byte_offset"]), len(HEADER)+600*len(excluded)+sum(map(len, selected[:index-602])))
                    self.assertEqual(row["source_sha256"], report["source_sha256"])
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertEqual(observation["fields"]["record_time_us"], 7000)
                    self.assertAlmostEqual(observation["fields"]["node_heading_rad"], math.pi)
                    self.assertEqual(observation["fields"]["node_depth_m"], 3)
                    collected.append(observation["fields"]["node_speed_m_s"])
            self.assertEqual(collected, [2, 4, 0])

    def test_small_native_and_scalar_reuse_existing_semantic_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "small.alog"
            raw = HEADER + node()
            source.write_bytes(raw)
            text, report = self.convert(source, 0, 3)
            old, _ = convert_node_report(raw, "NODE_REPORT", "pNodeReporter", 7000,
                                         unit_basis="producer-documentation-stated")
            row = next(csv.DictReader(io.StringIO(text)))
            old_row = next(csv.DictReader(io.StringIO(old)))
            self.assertEqual({key: row[key] for key in old_row}, old_row)
            self.assertEqual(report["line_selection"]["derived_lines"][2]["source_line"], 3)
            raw = HEADER + b"-0.12345 VOLT sensor D:12.5\n"
            source.write_bytes(raw)
            text, report = self.convert(source, 0, 3, source_format=SCALAR, variable="VOLT",
                                        publisher="sensor", unit="V", unit_basis=None)
            old, _ = convert_alog(raw, "VOLT", "sensor", "V", 7000, timestamp_decimals=5)
            row = next(csv.DictReader(io.StringIO(text)))
            old_row = next(csv.DictReader(io.StringIO(old)))
            self.assertEqual({key: row[key] for key in old_row}, old_row)
            observation = self.common(text, scalar=True)["observations"][0]
            self.assertEqual(observation["fields"]["voltage_v"], 12.5)
            self.assertEqual(observation["fields"]["logger_relative_time_us"], -123450)

    def test_refusals_header_options_selected_values_outside_encoding_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.alog"
            good = HEADER + node()
            for raw in (b"%% LOGSTART 1\n" + node(), HEADER + HEADER + node(),
                        HEADER + node() + b"%% LOGSTART 2\n", HEADER + b"0.00001 NODE_REPORT pNodeReporter D:12\n",
                        HEADER + node() + b"\xff\n", HEADER + node() + b"\v\n"):
                source.write_bytes(raw)
                with self.subTest(raw=raw[-30:]), self.assertRaises(ValueError):
                    self.convert(source)
            source.write_bytes(good)
            for changes in (dict(start_line=False), dict(start_line=-1), dict(stop_line=99),
                            dict(stop_line=2), dict(source_format="moos-fixed-slog"), dict(unit="V"),
                            dict(declared_units={"SPD":"V"}), dict(capture_time_us=True),
                            dict(source_format=SCALAR, variable="VOLT", publisher="sensor", unit="V")):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.convert(source, **changes)
            for constant in ("INPUT_LIMIT", "LINE_LIMIT", "LINE_COUNT_LIMIT", "HEADER_LIMIT",
                             "SELECTED_LIMIT", "CSV_LIMIT", "REPORT_LIMIT"):
                with self.subTest(constant=constant), patch.object(self.module, constant, 1), self.assertRaises(ValueError):
                    self.convert(source)

    def test_cli_pair_no_overwrite_or_successful_empty_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.alog"
            source.write_bytes(HEADER + node())
            output = Path(directory) / "out"
            command = [sys.executable, str(ROOT / "scripts/convert_moos_alog_window.py"), str(source), str(output),
                       "--start-line", "2", "--stop-line", "3", "--source-format", NODE,
                       "--variable", "NODE_REPORT", "--publisher", "pNodeReporter",
                       "--capture-time-us", "7000", "--unit-basis", "producer-documentation-stated"]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["selected_records"], 1)
            self.assertEqual(report["output_sha256"], hashlib.sha256((output / "observations.csv").read_bytes()).hexdigest())
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            command[3] = str(Path(directory) / "empty")
            command[5:8] = ["0", "--stop-line", "2"]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(Path(command[3]).exists())
