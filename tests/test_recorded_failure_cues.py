"""Authored journal export -> adopted converter -> real Observation -> bounded candidate."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_journal_storage_errors import convert, TRUNCATED_CORE_ID

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/storage-reported-errors/reception-profile.toml"


class RecordedFailureCueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.reader:
            raise unittest.SkipTest("real common reader not configured")
        if not Path(cls.reader).is_file():
            raise AssertionError("configured common reader does not exist")

    def read(self, entry, *, context=None, enabled=True, mutate=None):
        raw = json.dumps({"__REALTIME_TIMESTAMP": "1789500000000000", **entry})
        output, _, _ = convert(raw, clock="journal_reception_realtime")
        if mutate:
            rows = list(csv.DictReader(io.StringIO(output)))
            rows[0].update(mutate)
            buf = io.StringIO()
            writer = csv.DictWriter(buf, rows[0])
            writer.writeheader()
            writer.writerows(rows)
            output = buf.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved.csv"
            path.write_text(output)
            args = [self.reader, str(PROFILE), str(path)]
            if enabled:
                args.append("--recorded-failure-candidates")
            if context is not None:
                context = {"record_sha256": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(), **context}
                cfg = Path(tmp) / "context.json"
                cfg.write_text(json.dumps(context))
                args.extend(["--recorded-failure-context", str(cfg)])
            return json.loads(subprocess.run(args, check=True, capture_output=True).stdout)

    def cue(self, entry, **kwargs):
        common = self.read(entry, **kwargs)
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertIsNone(common["observations"][0]["wall_ms"])
        cue = common["recorded_failure_candidates"][0]
        self.assertIsNone(cue["time_window"])
        self.assertEqual(len(cue["evidence_digests"]), 1)
        for candidate in cue["candidates"]:
            self.assertEqual(candidate["logic_confidence"], "leading_indicator")
        return cue

    def test_errno_capacity_quota_readonly_io_and_unknown_stay_distinct(self):
        full = self.cue({"ERRNO": "28"})
        self.assertEqual(full["outcome"], "consistent")
        self.assertEqual(full["candidates"][0]["signature_id"], "reported_storage_no_space")
        readonly = self.cue({"ERRNO": "30"})
        self.assertEqual(readonly["outcome"], "ambiguous")
        self.assertEqual({c["signature_id"] for c in readonly["candidates"]},
                         {"reported_readonly_intended", "reported_readonly_after_error"})
        self.assertTrue(readonly["indistinguishable_by"])
        for code in (5, 122, 4000):
            with self.subTest(errno=code):
                cue = self.cue({"ERRNO": str(code)})
                self.assertEqual(cue["outcome"], "unknown")
                self.assertEqual(cue["reported_errno"], code)

    def test_configured_limit_needs_exact_record_bound_asserted_discrepancy(self):
        entry = {"MESSAGE_ID": TRUNCATED_CORE_ID}
        self.assertEqual(self.cue(entry)["outcome"], "unknown")
        mismatch = dict(expected_core_limit_bytes=8192, observed_core_limit_bytes=4096)
        cue = self.cue(entry, context=mismatch)
        self.assertEqual(cue["outcome"], "consistent")
        self.assertEqual(cue["candidates"][0]["signature_id"], "reported_configuration_discrepancy")
        self.assertEqual(cue["context_basis"], "caller_asserted_not_authenticated")
        equal = dict(expected_core_limit_bytes=4096, observed_core_limit_bytes=4096)
        self.assertEqual(self.cue(entry, context=equal)["outcome"], "unknown")
        unbound = dict(mismatch, record_sha256="sha256:" + "0" * 64)
        self.assertEqual(self.cue(entry, context=unbound)["outcome"], "unknown")

    def test_contradictory_class_or_unqualified_abi_abstains(self):
        for mutate in ({"storage_event_class": "REPORTED_IO_ERROR"},
                       {"reported_errno_name": "EIO"}, {"errno_abi_pin": "other"},
                       {"source_pin": "other"}, {"reported_errno": "28.5"}):
            with self.subTest(mutate=mutate):
                self.assertEqual(self.cue({"ERRNO": "28"}, mutate=mutate)["outcome"], "unknown")

    def test_no_source_time_or_operation_cannot_become_a_timed_database_claim(self):
        common = self.read({"ERRNO": "5", "_KERNEL_DEVICE": "b8:0"})
        self.assertIsNone(common["observations"][0]["fields"]["journal_source_realtime_us"])
        cue = common["recorded_failure_candidates"][0]
        self.assertEqual(cue["outcome"], "unknown")
        self.assertIsNone(cue["time_window"])
        self.assertEqual(cue["candidates"], [])

    def test_default_reader_output_stays_opt_in(self):
        common = self.read({"ERRNO": "28"}, enabled=False)
        self.assertNotIn("recorded_failure_candidates", common)

    def test_asserted_context_is_typed_and_requires_explicit_opt_in(self):
        context = dict(expected_core_limit_bytes=8192, observed_core_limit_bytes=4096)
        for bad in (dict(context, observed_core_limit_bytes=True),
                    dict(context, observed_core_limit_bytes=-1),
                    dict(context, observed_core_limit_bytes="4096"),
                    dict(context, unrelated_command="never interpreted")):
            with self.subTest(context=bad), self.assertRaises(subprocess.CalledProcessError):
                self.read({"MESSAGE_ID": TRUNCATED_CORE_ID}, context=bad)
        with self.assertRaises(subprocess.CalledProcessError):
            self.read({"MESSAGE_ID": TRUNCATED_CORE_ID}, context=context, enabled=False)

    def test_context_read_has_actual_byte_bound(self):
        with self.assertRaises(subprocess.CalledProcessError) as result:
            self.read({"MESSAGE_ID": TRUNCATED_CORE_ID}, context={"padding": "x" * 8192})
        self.assertIn(b"context exceeds bound", result.exception.stderr)


if __name__ == "__main__":
    unittest.main()
