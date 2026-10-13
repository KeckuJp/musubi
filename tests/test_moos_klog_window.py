"""Saved split-klog windows; authored records, not a marine deployment."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_moos_alog_window import convert_path

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "moos-ivp-split-klog-f56c"


def record(name, speed):
    return f"0.00001 NODE_REPORT pNodeReporter NAME={name},SPD={speed},HDG=90,X=1,Y=2,DEP=3,future=value\n".encode()


class KlogWindow(unittest.TestCase):
    def convert(self, path, start, stop, **changes):
        args = dict(start_line=start, stop_line=stop, source_format=FORMAT,
                    variable="NODE_REPORT", publisher="pNodeReporter", capture_time_us=9,
                    declared_start="1700000000", unit_basis="producer-documentation-stated")
        args.update(changes)
        return convert_path(path, **args)

    def test_long_and_separate_input_reuse_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader must be supplied")
        outside = b"not decoded outside " + b"x" * 29900 + b"\n"
        fixtures = [(outside * 600 + record("alpha", 2), 600, 601, 2),
                    (record("beta", 4) + record("gamma", 0), 0, 2, 4)]
        self.assertGreater(len(fixtures[0][0]), 16 * 1024**2)
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder)/"source.klog", Path(folder)/"out.csv"
            for raw, start, stop, speed in fixtures:
                source.write_bytes(raw)
                text, report = self.convert(source, start, stop)
                window = report["line_selection"]
                self.assertEqual(window["header_lines"], 0)
                self.assertEqual(window["excluded_lines"], start)
                self.assertEqual(report["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["selected_records"], stop-start)
                rows = list(csv.DictReader(io.StringIO(text)))
                self.assertEqual(int(rows[0]["source_line"]), start+1)
                self.assertEqual(window["outside_selection"], "NOT_DECODED_OR_VALIDATED")
                output.write_text(text)
                result = subprocess.run([reader, str(ROOT/"profiles/declared/moos-node-report/profile.toml"), str(output), "--allow-equal-time"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                common = json.loads(result.stdout)
                self.assertEqual(common["main_rows"], stop-start)
                obs = common["observations"][0]
                self.assertEqual(obs["clock_basis"], "Unknown")
                self.assertEqual(obs["fields"]["node_speed_m_s"], speed)
                self.assertEqual(obs["fields"]["node_depth_m"], 3)

    def test_wrong_representation_declaration_and_window_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"bad.klog"
            for raw in (b"%% LOGSTART 1\n" + record("a", 2),
                        record("a", 2).replace(b"NAME=", b"S:NAME="),
                        record("a", 2) + b"%% DATATYPE MARKING ON\n"):
                source.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.convert(source, 0, 1)
            source.write_bytes(record("a", 2))
            for changes in (dict(declared_start=None), dict(declared_start="NaN"),
                            dict(stop_line=2), dict(declared_units={"SPD":"V"}),
                            dict(source_format="moos-ivp-node-report-f56c")):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.convert(source, 0, 1, **changes)

    def test_cli_explicit_split_declaration_and_no_overwrite(self):
        import sys
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder)/"source.klog", Path(folder)/"out"
            source.write_bytes(record("cli", 2))
            command = [sys.executable, str(ROOT/"scripts/convert_moos_alog_window.py"),
                       str(source), str(output), "--start-line", "0", "--stop-line", "1",
                       "--source-format", FORMAT, "--declared-logstart", "1700000000",
                       "--variable", "NODE_REPORT", "--publisher", "pNodeReporter",
                       "--capture-time-us", "9", "--unit-basis", "producer-documentation-stated"]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output/"report.json").read_text())
            self.assertEqual(report["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(report["declared_logstart_text"], "1700000000")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
