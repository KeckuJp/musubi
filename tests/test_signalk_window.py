"""Independent native saved-log selection contract; authored bytes, not vessel evidence."""
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

from scripts import convert_signalk_recorded as converter
from tests.test_signalk_recorded import delta

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "vessels.window-example"
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"


def native(payload, host=1735693323500):
    return f"{host};reported-provider;{payload}".encode()


def value_line(values=None, timestamp="2025-01-01T01:02:03.123456789Z", host=1735693323500):
    return native(delta(CONTEXT, values or [("navigation.speedThroughWater", 2.5)], timestamp), host)


class SignalKWindowTests(unittest.TestCase):
    def selected(self, path, start, stop):
        return converter.convert_path(path, CONTEXT, start_record=start, stop_record=stop)

    def common(self, text, directory):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("MUSUBI_TELEMETRY_READER absent: actual common path NOT RUN")
        output = directory / "common-input.csv"
        output.write_text(text)
        result = subprocess.run([reader, str(PROFILE), str(output), "--allow-equal-time"],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_large_native_adjacent_windows_duplicate_values_and_exact_source_reach_common(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            first = value_line([("navigation.speedThroughWater", 2.5),
                                ("environment.depth.belowTransducer", 7.125)])
            second = value_line([("environment.water.temperature", 290.25),
                                 ("navigation.speedThroughWater", None),
                                 ("navigation.speedThroughWater", 0)], host=1735693323400)
            outside = b"1;p;{" + b"x" * 28990 + b"\r\n"
            raw = outside * 600 + first + b"\r\n" + second + b"\n" + second
            self.assertGreater(len(raw), 16 * 1024**2)
            source = directory / "large.log"
            source.write_bytes(raw)
            with self.assertRaises(ValueError):
                converter.convert(raw.decode(), CONTEXT, server_log=True)
            all_indices = []
            for start, stop, originals, expected_count in (
                    (600, 601, [first], 2), (601, 603, [second, second], 6)):
                output, report = self.selected(source, start, stop)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), expected_count)
                counts = [2] if start == 600 else [3, 3]
                expected_indices = [str(index) for index, count in zip(range(start, stop), counts)
                                    for _ in range(count)]
                self.assertEqual([r["source_record_index"] for r in rows], expected_indices)
                all_indices.extend(expected_indices)
                expanded = [record for record, count in zip(originals, counts) for _ in range(count)]
                for row, record in zip(rows, expanded):
                    self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]), record)
                    self.assertEqual(row["source_file_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.assertEqual(row["record_time_us"], "1735693323123456")
                    self.assertEqual(row["signalk_update_index"], "0")
                self.assertEqual([r["signalk_value_index"] for r in rows],
                                 [str(i) for count in counts for i in range(count)])
                selection = report["record_selection"]
                for key, value in {"source_sha256": hashlib.sha256(raw).hexdigest(),
                                   "source_bytes": len(raw), "source_records": 603,
                                   "selected_records": stop-start, "excluded_records": 603-(stop-start),
                                   "start_record": start, "stop_record_exclusive": stop,
                                   "outside_selection": "NOT_DECODED_OR_VALIDATED",
                                   "delta_clock_scope": "SELECTED_RECORDS_ONLY_NO_CROSS_WINDOW_ORDER_CLAIM"}.items():
                    self.assertEqual(selection[key], value)
                self.assertEqual(selection["bounds"], {
                    "max_input_bytes": 2 * 1024**3, "max_records": 5_000_000,
                    "max_record_bytes": 30000, "max_selected_source_bytes": 16 * 1024**2,
                    "max_output_bytes": 16 * 1024**2, "max_report_bytes": 64 * 1024**2})
                self.assertEqual(report["source_records"], stop-start)
                self.assertEqual(report["output_records"], expected_count)
                self.assertEqual(report["output_bytes"], len(output.encode()))
                self.assertEqual(report["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                common = self.common(output, directory)
                self.assertEqual(common["main_rows"], expected_count)
                self.assertEqual(common["platform_domain"], "Unknown")
                for observation, index in zip(common["observations"], expected_indices):
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertIsNone(observation["t_boot_us"])
                    self.assertIsNone(observation["anchor_unix_us"])
                    self.assertEqual(observation["fields"]["source_record_index"], int(index))
                if start == 600:
                    self.assertEqual(common["observations"][0]["fields"]["speed_through_water_m_s"], 2.5)
                    self.assertEqual(common["observations"][1]["fields"]["depth_below_transducer_m"], 7.125)
                else:
                    for offset in (0, 3):
                        self.assertEqual(common["observations"][offset]["fields"]["water_temperature_k"], 290.25)
                        absent = common["observations"][offset+1]["fields"]
                        self.assertEqual(absent["signalk_value_status"], "NOT_PROVIDED")
                        self.assertIn(absent.get("speed_through_water_m_s"), (None, ""))
                        self.assertEqual(common["observations"][offset+2]["fields"]["speed_through_water_m_s"], 0)
            self.assertEqual(all_indices, ["600"]*2 + ["601"]*3 + ["602"]*3)

    def test_selected_accounting_intervals_empty_and_cli_no_overwrite(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            lines = [value_line([("navigation.speedThroughWater", 1), ("future.path", {"raw": 1})]),
                     native("$RAW;provider;text"), native("[object Object]"),
                     native(delta("vessels.other", [("navigation.speedThroughWater", 9)]))]
            source = directory / "mixed.log"
            source.write_bytes(b"\n".join(lines))
            output, report = self.selected(source, 0, 4)
            self.assertEqual((report["source_records"], report["source_values"], report["output_records"],
                              report["unsupported_values"], report["provider_text_records"],
                              report["opaque_object_records"], report["unselected_context_records"]),
                             (4, 2, 1, 1, 2, 1, 1))
            self.assertEqual([bytes.fromhex(r[4:]) for r in report["retained_records"]], lines)
            for start, stop in ((-1, 1), (0, 0), (2, 1), (False, 1), (0, True),
                                (None, 1), (0, None), (0, 1.5), (0, 5), (0, 5_000_001)):
                with self.subTest(start=start, stop=stop), self.assertRaisesRegex(ValueError, "record interval"):
                    self.selected(source, start, stop)
            with self.assertRaisesRegex(ValueError, "no selected values"):
                self.selected(source, 1, 4)
            empty = directory / "empty.log"
            empty.write_bytes(b"")
            with self.assertRaises(ValueError):
                self.selected(empty, 0, 1)
            target = directory / "out"
            command = [sys.executable, str(ROOT / "scripts/convert_signalk_recorded.py"),
                       str(source), str(target), "--context", CONTEXT, "--server-log",
                       "--start-record", "0", "--stop-record", "4"]
            completed = subprocess.run(command, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual((target / "observations.csv").read_text(), output)
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, before)
            for omitted in ("--server-log", "--stop-record"):
                invalid = command.copy()
                invalid[3] = str(directory / ("missing" + omitted))
                index = invalid.index(omitted)
                del invalid[index:index+(2 if omitted == "--stop-record" else 1)]
                self.assertEqual(subprocess.run(invalid, capture_output=True).returncode, 2)
                self.assertFalse(Path(invalid[3]).exists())

    def test_whole_source_framing_and_distinct_resource_bounds(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "source.log"
            valid = value_line()
            for outside in (b"missing separators", b"", b"-1;p;text", b"1;p;\xff",
                            b"1;p;text\x00", b"1;p;text\x0bmore", b"1;p;text\rmore",
                            b"1;p;" + b"x"*30000):
                with self.subTest(outside=outside[:30]):
                    source.write_bytes(outside + b"\n" + valid)
                    with self.assertRaises((ValueError, UnicodeError)):
                        self.selected(source, 1, 2)
            source.write_bytes(valid)
            for name, bound in (("MAX_INPUT_BYTES", 1), ("MAX_RECORDS", 1),
                                ("MAX_RECORD_BYTES", 1), ("LIMIT", 1),
                                ("LIMIT", len(valid)+1), ("MAX_REPORT_BYTES", 1)):
                with self.subTest(resource=name, bound=bound), patch.object(converter, name, bound):
                    source.write_bytes(valid + b"\n" + valid if name == "MAX_RECORDS" else valid)
                    with self.assertRaisesRegex(ValueError, "bound"):
                        self.selected(source, 0, 1)

    def test_selected_delta_order_is_not_host_or_unselected_delta_order_and_legacy_unchanged(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "source.log"
            first = value_line(host=2000)
            second = value_line(host=1000)
            source.write_bytes(native("{invalid-json") + b"\n" + first + b"\n" + second)
            output, _ = self.selected(source, 1, 3)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r["signalk_log_time_us"] for r in rows], ["2000000", "1000000"])
            self.assertEqual(rows[0]["record_time_us"], rows[1]["record_time_us"])
            for invalid in (value_line(timestamp="2020-01-01T00:00:00Z"),
                            value_line([("navigation.speedThroughWater", True)]),
                            value_line([("navigation.speedThroughWater", float("nan"))])):
                source.write_bytes(first + b"\n" + invalid)
                with self.assertRaises(ValueError):
                    self.selected(source, 0, 2)
            bare = delta(CONTEXT, [("navigation.speedThroughWater", 2.5)])
            legacy, _ = converter.convert(bare, CONTEXT)
            self.assertEqual(legacy, converter.convert(bare, CONTEXT, server_log=False)[0])
            self.assertNotIn("source_file_sha256", legacy)
            self.assertNotIn("source_record_index", legacy)


if __name__ == "__main__":
    unittest.main()
