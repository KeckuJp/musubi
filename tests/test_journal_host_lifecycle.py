"""Authored saved metadata; no host restart or real-record claim."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_journal_host_lifecycle as module

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/journal-host-lifecycle/profile.toml"
START, FAILED = tuple(module.EVENTS)


def entry(message=START, time="1700000000000000", **extra):
    return {"MESSAGE_ID": message, "__REALTIME_TIMESTAMP": time, **extra}


def saved(*records):
    return b"\n".join(json.dumps(record).encode() for record in records)


class LifecycleTests(unittest.TestCase):
    def common(self, data):
        output, report = module.convert(data, producer=module.PRODUCER)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not supplied")
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "observations.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                               check=True, capture_output=True).stdout)
        return common, report

    def test_both_events_equal_time_and_unknown_accounting_common(self):
        result, report = self.common(saved(entry(_BOOT_ID="a", EXTRA="original"),
                                          entry(FAILED, _MACHINE_ID="host")))
        self.assertEqual(result["main_rows"], 2)
        self.assertEqual(report["uninterpreted_fields_total"], 1)
        first, second = [row["fields"] for row in result["observations"]]
        self.assertEqual(first["lifecycle_event"], "SYSTEM_MANAGER_STARTUP_FINISHED")
        self.assertEqual(second["lifecycle_event"], "POST_CRASH_FINAL_ACTION_FAILED")
        self.assertIn("POWER_OFF_OR_REBOOT", second["operation_limit"])
        self.assertEqual(first["reported_boot_id_hex"], "hex:61")
        self.assertTrue(all(row["clock_basis"] == "Unknown" for row in result["observations"]))

    def test_separate_reuse_keeps_missing_identity_and_excluded_input(self):
        result, report = self.common(saved(entry("98268866d1d54a499c4e98921d93bc40"),
                                          entry(time="1800000000000001", USERSPACE_USEC="99")))
        self.assertEqual(report["excluded_record_indices"], [1])
        fields = result["observations"][0]["fields"]
        self.assertEqual(fields["source_record_index"], 2)
        self.assertIn("_BOOT_ID:ABSENT", fields["identity_encoding_notes"])
        self.assertIsNone(fields["reported_boot_id_hex"])
        self.assertEqual(fields["uninterpreted_field_count"], 1)

    def test_important_refusals_and_cli(self):
        bad = [b"{}", b'{"x":1,"x":2}', saved(entry(COREDUMP=[1])),
               saved(entry(time="bad")), saved(entry(), entry(time="1")),
               saved(entry(__CURSOR="a"), entry(__CURSOR="a")), saved(entry("invalid"))]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                module.convert(data, producer=module.PRODUCER)
        with self.assertRaises(ValueError):
            module.convert(saved(entry()), producer="other")
        with tempfile.TemporaryDirectory() as work:
            source, out = Path(work) / "in.jsonl", Path(work) / "out"
            source.write_bytes(saved(entry()))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_journal_host_lifecycle.py"),
                            str(source), str(out), "--producer", module.PRODUCER], check=True)
            self.assertEqual(json.loads((out / "report.json").read_text())["selected_entries"], 1)
