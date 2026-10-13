"""Authored documented NVDLA reports, not accelerator/model execution."""
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

from scripts import convert_ardupilot_battery_csv as target

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"


def record(report, **updates):
    return json.dumps(dict(exporter_version="l4t-36.4.3", capture_time_us=100, report=report,
                           extra={"saved": True}, **updates))


def rows(text, **options):
    output = target.convert(text, target.TEGRA_FORMAT, tegrastats_nvdla=True, **options)
    return list(csv.DictReader(io.StringIO(output))), output


class TegraNvdlaTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_three_forms_and_two_distinct_inputs_reach_common(self):
        cases = [
            ("CPU [5%@102,off] NVDLA1 37%@700 NVDLA0 0% NVDLA2 9%", [1, 0],
             [.37, 0], [700000000, None], ["UTILIZATION_AND_FREQUENCY", "UTILIZATION_ONLY"], 2),
            ("NVDLA0 0 NVDLA1 512 unknown token", [0, 1],
             [None, None], [0, 512000000], ["FREQUENCY_ONLY", "FREQUENCY_ONLY"], 2)]
        for report, indices, utilization, frequency, presence, unselected in cases:
            with self.subTest(report=report), tempfile.TemporaryDirectory() as directory:
                text = record(report)
                parsed, output = rows(text)
                self.assertEqual(bytes.fromhex(parsed[0]["source_record_hex"][4:]).decode(), text)
                self.assertTrue(all(r["source_record_hex"] == "" for r in parsed[1:]))
                digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
                self.assertTrue(all(r["tegrastats_source_sha256"] == digest for r in parsed))
                path = Path(directory) / "observations.csv"
                path.write_text(output)
                read = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                       str(path), "--allow-equal-time"], capture_output=True)
                self.assertEqual(read.returncode, 0, read.stderr.decode())
                common = json.loads(read.stdout)
                fields = [o["fields"] for o in common["observations"]]
                selected = [f for f in fields if f["tegrastats_record_kind"] == "NVDLA"]
                self.assertEqual([f["nvdla_instance_index"] for f in selected], indices)
                self.assertEqual([f["nvdla_utilization_fraction"] for f in selected], utilization)
                self.assertEqual([f["nvdla_frequency_hz"] for f in selected], frequency)
                self.assertEqual([f["nvdla_field_presence"] for f in selected], presence)
                self.assertTrue(all(f["tegrastats_unselected_token_count"] == unselected for f in fields))
                self.assertTrue(all(f["record_time_us"] == 100 for f in fields))
                self.assertTrue(all("NOT_PERFORMANCE" in f["nvdla_basis"] for f in selected))
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertTrue(all(o["clock_basis"] == "Unknown" and o["anchor_unix_us"] is None
                                    for o in common["observations"]))
                self.assertIn("MHz", common["profile_units"]["nvdla_frequency_hz"])
                self.assertTrue(all(f["nvdla_frequency_hz"] is None for f in fields
                                    if f["tegrastats_record_kind"] != "NVDLA"))

    def test_absent_instance_and_exact_frequency_boundary(self):
        parsed, _ = rows(record("CPU [off] NVDLA0 25%"))
        selected = [r for r in parsed if r["tegrastats_record_kind"] == "NVDLA"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["nvdla_instance_index"], "0")
        self.assertEqual(selected[0]["nvdla_frequency_hz"], "")
        parsed, _ = rows(record("CPU [off] NVDLA2 40%"))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["nvdla_instance_index"], "")
        self.assertEqual(parsed[0]["tegrastats_unselected_token_count"], "2")
        maximum = (2**53 - 1) // 1000000
        parsed, _ = rows(record(f"NVDLA0 100%@{maximum}"))
        self.assertEqual(int(parsed[0]["nvdla_frequency_hz"]), maximum * 1000000)
        self.assertEqual(float(parsed[0]["nvdla_utilization_fraction"]), 1.0)
        with self.assertRaises(ValueError):
            rows(record(f"NVDLA0 {maximum + 1}"))

    def test_known_group_invalid_options_and_bounds_refuse(self):
        for report in ("NVDLA0", "NVDLA0 1%@", "NVDLA0 101%", "NVDLA1 -1",
                       "NVDLA0 1.5%", "NVDLA0 1.5", "NVDLA0 +1", "NVDLA0 off",
                       "NVDLA0 10%@3MHz", "NVDLA0 5% NVDLA0 6", "NVDLA1 4 NVDLA1 5%"):
            with self.subTest(report=report), self.assertRaises(ValueError):
                rows(record("CPU [off] " + report))
        bad = json.loads(record("NVDLA0 1%"))
        bad["exporter_version"] = "l4t-unknown"
        with self.assertRaises(ValueError):
            rows(json.dumps(bad))
        for option in (1, "yes", None):
            with self.subTest(option=option), self.assertRaises(ValueError):
                target.convert(record("CPU [off]"), target.TEGRA_FORMAT, tegrastats_nvdla=option)
        with self.assertRaises(ValueError):
            target.convert("[]", target.ORT_FORMAT, capture_time_us=0, tegrastats_nvdla=True)
        with self.assertRaises(ValueError):
            rows(record("NVDLA0 1% " + "x" * 30000))

    def test_default_hashes_and_native_cli(self):
        for report, expected in (
            ("CPU [5%@102,off] GPU@31.5C NVDLA0 37%@700 NVDLA2 9%",
             "e1ab197018b44bb750298ac9d1183ebf59d365ef6daceadf1c2479ca95a062c0"),
            ("RAM 1024/7620MB (lfb 12x4MB) GR3D_FREQ 37%@[624,624] VDD_IN 4321/3210",
             "b02d3c197bda6eea4ab5f49b0ea55c632d20cdf713cbac923a53918e4fbcd686")):
            self.assertEqual(hashlib.sha256(target.convert(record(report), target.TEGRA_FORMAT)
                                           .encode()).hexdigest(), expected)
        ort = '[{"cat":"Node","ph":"X","name":"kernel","pid":1,"tid":2,"ts":5,"dur":8,"args":{}}]'
        self.assertEqual(hashlib.sha256(target.convert(ort, target.ORT_FORMAT, capture_time_us=100)
                                       .encode()).hexdigest(),
                         "ed3ad0e2bd82c86f091703106c3f38520921e0cba627d3c8485348e665413a53")
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "in.jsonl", Path(directory) / "out.csv"
            source.write_text(record("NVDLA1 12%@800"))
            command = [sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                       str(source), str(output), "--source-format", target.TEGRA_FORMAT,
                       "--tegrastats-nvdla"]
            run = subprocess.run(command, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            row = next(csv.DictReader(io.StringIO(output.read_text())))
            self.assertEqual(row["nvdla_frequency_hz"], "800000000")
            source.write_text(record("NVDLA1 101%"))
            command[3] = str(Path(directory) / "bad.csv")
            run = subprocess.run(command, capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(Path(command[3]).exists())


if __name__ == "__main__":
    unittest.main()
