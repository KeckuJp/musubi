"""Bounded authored direct-state files, never real warehouse or robot evidence."""
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

from scripts import convert_vda_load_report as bridge
from tests import test_vda_load_report as fixtures

PALLET, PROFILE, ROOT = fixtures.PALLET, fixtures.PROFILE, fixtures.ROOT


class VdaLoadWindow(unittest.TestCase):
    def test_expected_pair_refuses_mismatch_and_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            raw = json.dumps(self.record()).encode() + b"\r\n"
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            original = bridge.convert_path(path, "vda-state-3.0", start_record=0, stop_record=1)
            checked = bridge.convert_path(path, "vda-state-3.0", start_record=0, stop_record=1,
                                          expected_size=len(raw), expected_sha256=digest)
            self.assertEqual(original[0], checked[0])
            checked[1].pop("input_expectation")
            self.assertEqual(original[1], checked[1])
            for size, checksum in ((len(raw)-1, digest), (len(raw), "0"*64), (True, digest),
                                   (None, digest), (len(raw), None), (-1, digest),
                                   (bridge.MAX_INPUT_BYTES+1, digest), (len(raw), "A"*64),
                                   (len(raw), "sha256:"+digest)):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    bridge.read_jsonl_window(path, start_record=0, stop_record=1,
                                             expected_size=size, expected_sha256=checksum)
            real_fstat, calls = os.fstat, []
            def replace_at_final_check(fd):
                info = real_fstat(fd)
                calls.append(fd)
                if len(calls) == 2:
                    replacement = Path(directory) / "replacement"
                    replacement.write_bytes(raw)
                    replacement.replace(path)
                return info
            with patch.object(bridge.os, "fstat", side_effect=replace_at_final_check):
                with self.assertRaisesRegex(ValueError, "changed during read"):
                    bridge.read_jsonl_window(path, start_record=0, stop_record=1,
                                             expected_size=len(raw), expected_sha256=digest)

    def record(self, version="3.0.0", **extra):
        return fixtures.VdaLoadReport().record(version, **extra)

    def rows(self, output):
        return list(csv.DictReader(io.StringIO(output)))

    def test_long_file_adjacent_windows_preserve_every_load_and_original_identity(self):
        pad = json.dumps(self.record(unknown="x" * 28000)).encode() + b"\r\n"
        records = [self.record(loads=[dict(PALLET, future={"kept": 23}), {"loadId": "second"}],
                              unknown="x" * 70000,
                              actionStates=[{"actionId": "reported-action", "actionStatus": "FINISHED"}]),
                   self.record(loads=[]), self.record()]
        raw_lines = [json.dumps(r).encode() for r in records]
        data = pad * 600 + b"\n".join(raw_lines)
        self.assertGreater(len(data), 16 * 1024**2)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "state.jsonl"; source.write_bytes(data)
            outputs = [bridge.convert_path(source, "vda-state-3.0", start_record=a, stop_record=b,
                                          expected_size=len(data), expected_sha256=hashlib.sha256(data).hexdigest())
                       for a, b in ((600, 601), (601, 603))]
            all_rows = [r for output, _ in outputs for r in self.rows(output)]
            self.assertEqual([int(r["source_record_index"]) for r in all_rows], [600, 600, 601, 602])
            self.assertEqual([r["load_index"] for r in all_rows], ["0", "1", "", ""])
            self.assertEqual(len({r["record_time_us"] for r in all_rows}), 1)
            offsets = [len(pad) * 600, len(pad) * 600 + len(raw_lines[0]) + 1,
                       len(pad) * 600 + len(raw_lines[0]) + len(raw_lines[1]) + 2]
            self.assertEqual([int(r["source_record_byte_offset"]) for r in all_rows],
                             [offsets[0], offsets[0], offsets[1], offsets[2]])
            for row, record_index in zip(all_rows, (0, 0, 1, 2)):
                self.assertEqual(row["source_file_sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
                self.assertEqual(row["state_record_sha256"],
                                 "sha256:" + hashlib.sha256(raw_lines[record_index]).hexdigest())
            self.assertNotEqual(all_rows[-1]["load_report_state"], all_rows[-2]["load_report_state"])
            reader = os.environ.get("MUSUBI_TELEMETRY_READER")
            if not reader:
                self.skipTest("actual common reader required for integration evidence")
            common_rows = []
            for index, (output, report) in enumerate(outputs):
                selected = report["record_selection"]
                self.assertEqual(selected["source_records"], 603)
                self.assertEqual(selected["selected_records"], (1, 2)[index])
                self.assertEqual(selected["excluded_records"], (602, 601)[index])
                self.assertEqual(selected["outside_selection"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual(report["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(report["rows"], report["loads_reported"] +
                    report["records_reporting_unloaded"] + report["records_not_reporting_load_state"])
                self.assertEqual(report["unknown_load_properties_at_declared_levels"], (1, 0)[index])
                self.assertEqual(report["detail_rows_skipped"]["ACTION"], (1, 0)[index])
                self.assertLess(len(output), 15000)  # unknown original is not copied per load
                out = Path(directory) / f"{index}.csv"; out.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(out),
                    "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["platform_domain"], "Unknown")
                common_rows += common["observations"]
            self.assertEqual(common_rows[0]["fields"]["load_weight_kg"], 720)
            self.assertEqual(common_rows[-1]["fields"]["source_record_index"], 602)
            for row in common_rows:
                self.assertEqual(row["clock_basis"], "Unknown")
                self.assertIsNone(row["anchor_unix_us"])

    def test_selected_semantics_versions_outside_scope_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.jsonl"
            for version, selector in (("2.1.0", "vda-state-2.1"), ("3.0.0", "vda-state-3.0")):
                state = self.record(version, loads=[PALLET])
                raw = json.dumps(state).encode()
                path.write_bytes(b"{invalid outside\n" + raw + b"\n")
                output, report = bridge.convert_path(path, selector, start_record=1, stop_record=2)
                legacy = bridge.convert(fixtures.VdaLoadReport().state(state, version=version), state_context=True)
                row = self.rows(output)[0]
                for key, value in self.rows(legacy[0])[0].items():
                    self.assertEqual(row[key], value)
                self.assertEqual(report["state_records_read"], 1)
                with self.assertRaises(ValueError):
                    bridge.convert_path(path, selector, start_record=0, stop_record=1)
                state["loads"] = None
                path.write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    bridge.convert_path(path, selector, start_record=0, stop_record=1)
            path.write_text(json.dumps(self.record()) + "\n" + json.dumps(self.record(timestamp="2023-01-01T00:00:00Z")))
            with self.assertRaises(ValueError):
                bridge.convert_path(path, "vda-state-3.0", start_record=0, stop_record=2)

    def test_boundaries_and_cli_are_explicit(self):
        original_field_limit = csv.field_size_limit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.jsonl"
            raw = json.dumps(self.record(loads=[])).encode()
            path.write_bytes(raw + b"\n" + raw)
            for start, stop in ((True, 1), (0, None), (1, 1), (-1, 1), (0, 3)):
                with self.assertRaises(ValueError):
                    bridge.convert_path(path, "vda-state-3.0", start_record=start, stop_record=stop)
            with self.assertRaises(ValueError):
                bridge.convert_path(path, "vda-state-2.0", start_record=0, stop_record=1)
            for constant, limit in (("MAX_INPUT_BYTES", 1), ("MAX_RECORDS", 1),
                                    ("MAX_RECORD_BYTES", 1), ("MAX_SELECTED_BYTES", 1),
                                    ("MAX_WINDOW_OUTPUT_BYTES", 1)):
                with patch.object(bridge, constant, limit), self.assertRaises(ValueError):
                    bridge.convert_path(path, "vda-state-3.0", start_record=0, stop_record=1)
            for invalid in (b"\n", b"\xff\n", b"{}\x00\n", b"{}\r{}\n"):
                path.write_bytes(raw + b"\n" + invalid)
                with self.assertRaises(ValueError):
                    bridge.convert_path(path, "vda-state-3.0", start_record=0, stop_record=1)
            path.write_bytes(raw)
            out = Path(directory) / "out"
            command = [sys.executable, "-m", "scripts.convert_vda_load_report", str(path), str(out),
                       "--source-format", "vda-state-3.0", "--start-record", "0", "--stop-record", "1"]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            expected = bridge.convert_path(path, "vda-state-3.0", start_record=0, stop_record=1)
            self.assertEqual((out / "observations.csv").read_text(), expected[0])
            self.assertEqual(json.loads((out / "report.json").read_text()), expected[1])
            self.assertEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 2)
            self.assertEqual((out / "observations.csv").read_text(), expected[0])
            self.assertEqual(subprocess.run(command[:-2], cwd=ROOT, capture_output=True).returncode, 2)
        self.assertEqual(csv.field_size_limit(), original_field_limit)
