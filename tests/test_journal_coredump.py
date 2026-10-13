"""Authored fixed-writer metadata only; no crash execution or memory dump."""
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

from scripts import convert_journal_coredump as module

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/journal-coredump/profile.toml"
PRODUCER = "systemd-v257-kernel-coredump"
COREDUMP_ID = "fc2e22bc6ee647b6b90729ab34a250b1"
T = 1700000000000000


def entry(**changes):
    result = {"MESSAGE_ID": COREDUMP_ID, "__REALTIME_TIMESTAMP": str(T + 10),
              "COREDUMP_TIMESTAMP": str(T), "COREDUMP_PID": "431", "COREDUMP_SIGNAL": "11",
              "COREDUMP_SIGNAL_NAME": "SIGSEGV", "COREDUMP_COMM": "worker",
              "COREDUMP_EXE": "/app/worker", "COREDUMP_UNIT": "worker.service",
              "_PID": "901", "_BOOT_ID": "abcd", "__CURSOR": "a"}
    result.update(changes)
    return result


def saved(*entries):
    return ("\r\n".join(json.dumps(e) for e in entries) + "\r\n").encode()


def rows(output):
    return list(csv.DictReader(io.StringIO(output)))


def hx(value):
    return "hex:" + value.encode().hex()


class JournalCoredumpTests(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not supplied; not integration evidence")
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "observations.csv"
            path.write_text(output)
            return json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                             capture_output=True, check=True).stdout)

    def test_kernel_report_and_equal_time_reach_common(self):
        data = saved(entry(), entry(__CURSOR="b", COREDUMP_PID="432", COREDUMP_SIGNAL="6",
                                    COREDUMP_SIGNAL_NAME="SIGABRT"))
        output, report = module.convert(data, producer=PRODUCER)
        self.assertEqual(report["selected_entries"], 2)
        self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
        result = rows(output)
        self.assertEqual([r["source_record_index"] for r in result], ["1", "2"])
        self.assertEqual(result[0]["record_sha256"], "sha256:" + hashlib.sha256(data.splitlines()[0]).hexdigest())
        common = self.common(output)
        self.assertEqual(common["main_rows"], 2)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        first, second = [o["fields"] for o in common["observations"]]
        self.assertEqual(first["reported_crash_time_us"], T)
        self.assertEqual(first["reported_crash_time_precision_us"], 1000000)
        self.assertEqual(first["reported_pid"], 431)
        self.assertEqual(first["reporter_pid_hex"], hx("901"))
        self.assertEqual(first["reported_signal_name_hex"], hx("SIGSEGV"))
        self.assertEqual(second["reported_signal"], 6)
        self.assertEqual(first["reported_boot_id_hex"], hx("abcd"))
        self.assertEqual(first["crash_event"], "PRODUCER_REPORTED_COREDUMP")

    def test_separate_missing_forwarded_and_excluded_reports(self):
        missing = {"MESSAGE_ID": COREDUMP_ID, "__REALTIME_TIMESTAMP": str(T),
                   "COREDUMP_PID": None, "COREDUMP_TIMESTAMP": None, "COREDUMP": None,
                   "COREDUMP_COMM": ["one", "two"], "COREDUMP_FORWARDED": "1", "__CURSOR": None,
                   "EXTRA": "unknown raw value"}
        other = {"MESSAGE_ID": "1f4e0a44a88649939aaea34fc6da8c95", "COREDUMP_SIGNAL": "9"}
        unknown = entry(__CURSOR="z", COREDUMP_SIGNAL="123", COREDUMP_SIGNAL_NAME="SIGFUTURE")
        output, report = module.convert(saved(other, missing, {"ERRNO": "12"}, unknown), producer=PRODUCER)
        self.assertEqual(report["excluded_record_indices"], [1, 3])
        self.assertEqual(report["entries"], 4)
        result = rows(output)
        self.assertEqual([r["source_record_index"] for r in result], ["2", "4"])
        self.assertEqual(result[0]["reported_pid"], "")
        self.assertEqual(result[0]["reported_pid_status"], "WITHHELD_NULL")
        self.assertEqual(result[0]["reported_signal_status"], "ABSENT")
        self.assertEqual(result[0]["reported_crash_time_us"], "")
        self.assertEqual(result[0]["reported_forwarded_hex"], hx("1"))
        self.assertIn("COREDUMP_COMM:MULTIPLE_VALUES_IN_SOURCE_INPUT", result[0]["identity_encoding_notes"])
        self.assertIn("__CURSOR:WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL", result[0]["identity_encoding_notes"])
        self.assertIn("EXTRA", json.loads(bytes.fromhex(result[0]["uninterpreted_field_names_hex"][4:])))
        self.assertNotIn("unknown raw value", output)
        common = self.common(output)
        self.assertEqual(common["main_rows"], 2)
        fields = common["observations"][0]["fields"]
        self.assertIsNone(fields["reported_pid"])
        self.assertIsNone(fields["reported_signal"])
        self.assertEqual(common["observations"][1]["fields"]["reported_signal"], 123)

    def test_important_refusals_bounds_and_cli(self):
        invalid = [b'{"MESSAGE_ID":"x","MESSAGE_ID":"y"}', saved([]),
                   saved(entry(MESSAGE_ID=[COREDUMP_ID])), saved(entry(COREDUMP="secret memory")),
                   saved({"COREDUMP": [1, 2, 3]}), saved(entry(COREDUMP_PID="0")),
                   saved(entry(COREDUMP_SIGNAL=["11", "6"])), saved(entry(COREDUMP_SIGNAL=True)),
                   saved(entry(COREDUMP_TIMESTAMP=str(T + 1))),
                   saved(entry(COREDUMP_TIMESTAMP="9007199255000000")),
                   saved(entry(__REALTIME_TIMESTAMP=None)), saved(entry(), entry()),
                   saved(entry(), entry(__CURSOR="b", __REALTIME_TIMESTAMP=str(T))),
                   saved({"MESSAGE_ID": "1f4e0a44a88649939aaea34fc6da8c95"})]
        for data in invalid:
            with self.subTest(data=data[:60]), self.assertRaises(ValueError):
                module.convert(data, producer=PRODUCER)
        with self.assertRaises(ValueError):
            module.convert(saved(entry()), producer="guess-from-message")
        for constant in ("LIMIT", "MAX_LINE", "MAX_ENTRIES", "MAX_FIELDS", "OUTPUT_LIMIT"):
            with self.subTest(bound=constant), patch.object(module, constant, 1), self.assertRaises(ValueError):
                module.convert(saved(entry(), entry(__CURSOR="b")), producer=PRODUCER)
        with tempfile.TemporaryDirectory() as work:
            path, destination = Path(work) / "saved.jsonl", Path(work) / "result"
            path.write_bytes(saved(entry()))
            command = [sys.executable, str(ROOT / "scripts/convert_journal_coredump.py"), str(path),
                       str(destination), "--producer", PRODUCER]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual({p.name for p in destination.iterdir()}, {"observations.csv", "report.json"})
            before = (destination / "observations.csv").read_bytes()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((destination / "observations.csv").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
