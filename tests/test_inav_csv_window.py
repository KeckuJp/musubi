"""Independent authored native INAV CSV contract; no flight/device qualification."""
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

from scripts import convert_recorded_angles_csv as converter

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "inav-decoder-1918-si-motion"
HEADER = "time (us)," + ",".join(
    [f"gyroADC[{i}] (rad/s)" for i in range(3)]
    + [f"accSmooth[{i}] (m/s/s)" for i in range(3)]
) + ",future"
NAMES = ([f"reported_angular_velocity_{axis}_rad_s" for axis in "xyz"]
         + [f"reported_acceleration_{axis}_m_s2" for axis in "xyz"])


def record(time, values=(1.25, -2.5, 0., 9.81, -1., 0.5), extra="opaque"):
    return f"{time}," + ",".join(f"{value:.2f}" for value in values) + "," + extra


class InavCsvWindowTests(unittest.TestCase):
    def convert_file(self, path, start=0, stop=1, schema=SCHEMA):
        return converter.convert_path(path, schema, start_record=start, stop_record=stop)

    def common(self, output, directory):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required; absence is NOT_RUN, not PASS")
        path = directory / "common.csv"
        path.write_text(output)
        return json.loads(subprocess.run(
            [reader, str(ROOT / "profiles/declared/recorded-angles/profile.toml"),
             str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)

    def test_large_native_adjacent_intervals_preserve_common_and_account_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "long.csv"
            values = [(1.25, -2.5, 0., 9.81, -1., 0.5), (-3., 0.5, 2., -9.81, 2., 1.)]
            selected = [record(t, values[i % 2], f"selected-{i}")
                        for i, t in enumerate([600000, 600000, 600001, 600002])]
            with path.open("wb") as stream:
                stream.write((HEADER + "\r\n").encode())
                for i in range(513):
                    stream.write((f"{i * 1000},NaN,NaN,NaN,NaN,NaN,NaN," + "x" * 32768 + "\r\n").encode())
                stream.write("\r\n".join(selected).encode())  # legal unterminated final row
            raw = path.read_bytes()
            self.assertGreater(len(raw), 16 * 1024**2)
            digest = hashlib.sha256(raw).hexdigest()
            all_indexes = []
            for start, stop in [(513, 515), (515, 517)]:
                output, report = self.convert_file(path, start, stop)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(report["schema"], "inav-csv-record-selection/v1")
                self.assertEqual(report["source_sha256"], digest)
                self.assertEqual(report["source_bytes"], len(raw))
                self.assertEqual((report["source_records"], report["selected_records"], report["excluded_records"]), (517, 2, 515))
                self.assertEqual(report["selection"], {"start_record": start, "stop_record_exclusive": stop})
                self.assertEqual(report["excluded_values"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual(report["clock_basis"], "BootRelative")
                self.assertEqual(report["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(report["output_bytes"], len(output.encode()))
                common = self.common(output, directory)
                self.assertEqual(common["main_rows"], 2)
                for index, (row, observation) in enumerate(zip(rows, common["observations"]), start):
                    all_indexes.append(int(row["source_record_index"]))
                    self.assertEqual(row["source_file_sha256"], "sha256:" + digest)
                    self.assertEqual(bytes.fromhex(row["source_header_hex"][4:]).decode(), HEADER)
                    self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), selected[index - 513])
                    self.assertEqual([observation["fields"][name] for name in NAMES], list(values[(index - 513) % 2]))
                    self.assertEqual(observation["clock_basis"], "BootRelative")
                    self.assertEqual(observation["t_boot_us"], [600000, 600000, 600001, 600002][index - 513])
                    self.assertIsNone(observation["anchor_unix_us"])
                    self.assertEqual(int(observation["fields"]["source_record_index"]), index)
            self.assertEqual(all_indexes, [513, 514, 515, 516])

    def test_invalid_selection_schema_and_empty_source_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "small.csv"
            path.write_text(HEADER + "\n" + record(100) + "\n")
            for start, stop in [(-1, 1), (0, 0), (1, 0), (0, 2), (True, 1), (0, True),
                                (None, 1), (0, None), (0., 1), (0, 5_000_001)]:
                with self.subTest(start=start, stop=stop), self.assertRaisesRegex(ValueError, "interval"):
                    self.convert_file(path, start, stop)
            with self.assertRaisesRegex(ValueError, "schema"):
                self.convert_file(path, schema="inav-7.1.2-raw-attitude")
            for empty in ["", HEADER + "\n"]:
                path.write_text(empty)
                with self.assertRaises(ValueError):
                    self.convert_file(path)

    def test_global_framing_and_boot_order_are_checked_but_selected_si_must_be_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.csv"
            for rows in [[record(100), record(200), record(50)],
                         [record(300), record(200), record(400)]]:
                path.write_text(HEADER + "\n" + "\n".join(rows))
                with self.assertRaisesRegex(ValueError, "decreasing"):
                    self.convert_file(path, 1, 2)
            for text in [HEADER + "\n" + record(100) + "\n200,0.00",  # excluded ragged row
                         HEADER + "\n" + record(100).replace("1.25", "NaN", 1),
                         HEADER + "\n" + record(100).replace("1.25", "", 1),
                         HEADER.replace("(rad/s)", "(deg/s)") + "\n" + record(100),
                         HEADER + "\n" + record(100) + "\n",
                         HEADER + "\n" + record(100, extra="z" * 65536)]:
                if text.endswith("\n"):
                    text += "\n"
                path.write_text(text)
                with self.assertRaises(ValueError):
                    self.convert_file(path)

    def test_distinct_resource_bounds_and_cli_require_complete_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "small.csv"
            path.write_text(HEADER + "\n" + record(100) + "\n" + record(100))
            for name, limit in [("MAX_INPUT_BYTES", 1), ("MAX_RECORDS", 1), ("MAX_RECORD_BYTES", 10)]:
                with self.subTest(bound=name), patch.object(converter, name, limit, create=True), self.assertRaisesRegex(ValueError, "bound"):
                    self.convert_file(path)
            path.write_text(HEADER + "\n" + record(100, extra="q" * 600))
            self.assertLess(path.stat().st_size, 1024)
            with patch.object(converter, "LIMIT", 1024), self.assertRaisesRegex(ValueError, "bound"):
                self.convert_file(path)
            path.write_text(HEADER + "\n" + record(100))
            output, report = directory / "out.csv", directory / "report.json"
            command = [sys.executable, str(ROOT / "scripts/convert_recorded_angles_csv.py"),
                       str(path), str(output), "--schema", SCHEMA]
            for flags in [["--start-record", "0"], ["--start-record", "0", "--stop-record", "1"]]:
                result = subprocess.run(command + flags, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
            complete = command + ["--start-record", "0", "--stop-record", "1", "--report", str(report)]
            subprocess.run(complete, capture_output=True, check=True)
            before = (output.read_bytes(), report.read_bytes())
            self.assertEqual(json.loads(before[1])["selected_records"], 1)
            self.assertNotEqual(subprocess.run(complete, capture_output=True).returncode, 0)
            self.assertEqual((output.read_bytes(), report.read_bytes()), before)
