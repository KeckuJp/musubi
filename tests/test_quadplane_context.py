"""Authored decoded records -> real reader -> reported context -> real common output."""
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def record(kind, **fields):
    return {"meta": {"type": kind}, "data": fields}


def mode(time, number=25):
    return record("MODE", TimeUS=time, Mode=number, ModeNum=number, Rsn=1, Extra="kept")


def parameter(time, value):
    return record("PARM", TimeUS=time, Name="Q_ENABLE", Value=value, Default=0, Extra=["kept", 1])


def decode_hex(value):
    return json.loads(bytes.fromhex(value.removeprefix("hex:")).decode())


class QuadplaneContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT / "scripts/convert_quadplane_context.py"
        spec = importlib.util.spec_from_file_location("quadplane_context_contract", source)
        cls.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bridge)
        cls.input_reader = os.environ.get("MUSUBI_MAVLOG_JSON_READER")
        cls.output_reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.input_reader or not cls.output_reader:
            raise unittest.SkipTest("both real reader binaries are required")

    def report(self, records):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "input.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in records))
            result = subprocess.run([self.input_reader, str(ROOT /
                "profiles/declared/arduplane-jsonl/profile.toml"), str(path)],
                capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return result.stdout

    def convert(self, raw, **options):
        args = dict(vehicle="plane-4.5.5", session_id="authored-session", single_boot_session=True)
        args.update(options)
        return self.bridge.convert(raw, **args)

    def common(self, text, count):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "context.csv"
            path.write_text(text)
            result = subprocess.run([self.output_reader, str(ROOT /
                "profiles/declared/ardupilot-mode/quadplane-context-profile.toml"),
                str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual(common["main_rows"], count)
        self.assertEqual(common["platform_domain"], "Unknown")
        for row in common["observations"]:
            self.assertEqual(row["clock_basis"], "BootRelative")
            self.assertIsNone(row["anchor_unix_us"])
        return common

    def test_prior_report_is_not_activation_and_mode25_is_not_enablement(self):
        records = [mode(10), parameter(20, 0), mode(30), parameter(40, 1), mode(50, 200),
                   parameter(60, 2), mode(70, 20), record("MSG", Message="untimed kept")]
        raw = self.report(records)
        text, report = self.convert(raw)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([row["quadplane_context_status"] for row in rows],
                         ["NO_PRIOR_PARAMETER_REPORT", "LATEST_PRIOR_REPORTED_0_OR_1",
                          "LATEST_PRIOR_REPORTED_0_OR_1", "LATEST_PRIOR_REPORTED_2"])
        self.assertEqual([float(row["quadplane_q_enable_reported"]) if row["quadplane_q_enable_reported"]
                          else None for row in rows], [None, 0, 1, 2])
        self.assertEqual([int(row["source_report_record_index"]) for row in rows], [0, 2, 4, 6])
        self.assertEqual(rows[0]["reported_mode_name"], "LOITER_ALT_QLAND")
        self.assertEqual(rows[1]["reported_mode_name"], "LOITER_ALT_QLAND")
        self.assertEqual(rows[2]["reported_mode_known"], "false")
        self.assertEqual(rows[1]["quadplane_next_parameter_report_us"], "40")
        self.assertEqual(decode_hex(rows[1]["quadplane_parameter_record_indices_hex"]), [1])
        self.assertEqual(decode_hex(rows[1]["quadplane_parameter_records_hex"]), [records[1]])
        self.assertEqual(report["source_report_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual((report["source_records"], report["mode_records"], report["q_enable_records"],
                          report["other_records"]), (8, 4, 3, 1))
        for row in rows:
            self.assertEqual(row["Extra"], "kept")
            self.assertIn("NOT_ACTIVE_CONFIG", row["quadplane_context_basis"])
            self.assertEqual(bytes.fromhex(row["quadplane_session_id_hex"][4:]), b"authored-session")
        common = self.common(text, 4)
        self.assertEqual(common["observations"][1]["fields"]["quadplane_q_enable_reported"], 0)
        self.assertEqual(common["observations"][3]["fields"]["quadplane_q_enable_setting_reported"], "ENABLE_VTOL_AUTO")

    def test_reported_setting_is_not_active_state(self):
        records = [parameter(1, 0), mode(2), parameter(3, 1), mode(4),
                   parameter(5, 2), mode(6), parameter(7, 3), mode(8),
                   parameter(9, 2), mode(9), parameter(10, 1), parameter(10, 2), mode(11)]
        text, _ = self.convert(self.report(records))
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([row["quadplane_q_enable_setting_reported"] for row in rows],
                         ["DISABLE", "ENABLE", "ENABLE_VTOL_AUTO", "", "", ""])
        self.assertEqual([row["quadplane_context_status"] for row in rows[3:]],
                         ["UNINTERPRETED_PARAMETER_VALUE", "SAME_TIMESTAMP_UNORDERED", "CONFLICTING_PARAMETER_REPORTS"])
        self.common(text, 6)

    def test_same_time_is_unordered_and_conflict_or_duplicate_evidence_is_not_discarded(self):
        records = [parameter(10, 1), parameter(20, 0), mode(20), parameter(20, 1), mode(30),
                   parameter(40, 0), parameter(40, 0.0), mode(50, 17)]
        text, _ = self.convert(self.report(records))
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([row["quadplane_context_status"] for row in rows],
                         ["SAME_TIMESTAMP_UNORDERED", "CONFLICTING_PARAMETER_REPORTS",
                          "LATEST_PRIOR_REPORTED_0_OR_1"])
        self.assertEqual(rows[0]["quadplane_q_enable_reported"], "")
        self.assertEqual(rows[1]["quadplane_q_enable_reported"], "")
        self.assertEqual(decode_hex(rows[0]["quadplane_parameter_record_indices_hex"]), [1, 3])
        self.assertEqual(decode_hex(rows[2]["quadplane_parameter_record_indices_hex"]), [5, 6])
        self.assertEqual(decode_hex(rows[2]["quadplane_parameter_records_hex"]), records[5:7])
        self.common(text, 3)

    def test_declarations_identity_missing_time_and_reset_refuse(self):
        raw = self.report([parameter(10, 1), mode(20)])
        for options in ({"single_boot_session": False}, {"single_boot_session": 1},
                        {"session_id": "bad\nlabel"}, {"vehicle": "rover-4.5.5"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.convert(raw, **options)
        report = json.loads(raw)
        for kind in ("input_hash", "profile_hash", "missing_time", "boolean_time", "reset"):
            invalid = copy.deepcopy(report)
            if kind == "input_hash":
                invalid["input_sha256"] = "unbound"
            elif kind == "profile_hash":
                invalid["profile_identity"]["source_sha256"] = "unbound"
            elif kind == "missing_time":
                del invalid["source_records"][0]["data"]["TimeUS"]
            elif kind == "boolean_time":
                invalid["source_records"][0]["data"]["TimeUS"] = True
            else:
                invalid["source_records"][1]["data"]["TimeUS"] = 1
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.convert(json.dumps(invalid).encode())


if __name__ == "__main__":
    unittest.main()
