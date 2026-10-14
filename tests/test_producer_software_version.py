"""Actual paired common reports -> exact producer expectation -> common candidates; authored only."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import compare_producer_software_version as module
from scripts import convert_software_identity_binding as binding

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/producer-software-version"
IDENTITY = ROOT / "profiles/declared/software-identity-binding/profile.toml"


class ProducerVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.reader:
            raise unittest.SkipTest("actual rebuilt common reader required")

    def read(self, text, profile=IDENTITY, enabled=False):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text(text)
            args = [self.reader, str(profile), str(source), "--allow-equal-time"]
            if enabled:
                args.append("--recorded-failure-candidates")
            return subprocess.run(args, check=True, capture_output=True).stdout

    def fixture(self, source=None):
        source = source or b"record_time_us,name,version\n1,alpha,1.0\n1,alpha,2.0\n1,beta,9\n1,alpha,\n"
        paired, _ = binding.convert(source,
            paired_identities=("SOURCE_SOFTWARE=NAME:name,VERSION:version",),
            namespace="authored-writer", instance="run-A", source_citation="authored saved table")
        return self.read(paired), module.digest(source)

    def expected(self, report, source, **changes):
        expected = dict(schema=module.SCHEMA, read_report_sha256=module.digest(report),
            bound_observations_sha256=source, source_role="SOURCE_SOFTWARE", software_name="alpha",
            expected_versions=["1.0", "1.1"], citation="declared experiment setup", capture_time_us=987)
        expected.update(changes)
        return json.dumps(expected).encode()

    def test_paired_actual_common_matches_mismatches_missing_and_alternatives(self):
        raw, source = self.fixture()
        expected = self.expected(raw, source)
        csv_text, report = module.convert(raw, expected)
        common = json.loads(self.read(csv_text, CASE / "profile.toml", True))
        fields = [row["fields"] for row in common["observations"]]
        self.assertEqual(common["main_rows"], 3)
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertEqual({r["disposition"] for r in fields}, {"MATCH", "MISMATCH", "INSUFFICIENT"})
        self.assertEqual((report["input_observations"], report["selected_observations"], report["unselected_observations"]), (4, 3, 1))
        self.assertEqual(bytes.fromhex(report["source_report_hex"][4:]), raw)
        self.assertEqual(bytes.fromhex(report["source_expectation_hex"][4:]), expected)
        candidates = common["recorded_failure_candidates"]
        selected = [r for r in candidates if r["outcome"] == "ambiguous"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(selected[0]["candidates"]), 2)
        self.assertEqual(selected[0]["comparison"]["actual_declared_hex"], binding.text("2.0"))
        self.assertEqual(selected[0]["comparison"]["bound_observations_sha256"], source)
        self.assertEqual(selected[0]["clock_basis"], "Unknown")
        self.assertIsNone(selected[0]["time_window"])
        self.assertTrue(selected[0]["indistinguishable_by"])
        for candidate in selected[0]["candidates"]:
            self.assertEqual(candidate["logic_confidence"], "leading_indicator")
            self.assertEqual(candidate["supporting"], selected[0]["evidence_digests"])
        self.assertEqual(sum(r["outcome"] == "unknown" for r in candidates), 2)
        plain = json.loads(self.read(csv_text, CASE / "profile.toml"))
        self.assertNotIn("recorded_failure_candidates", plain)
        self.assertEqual(plain["observations"], common["observations"])

    def test_missing_subject_roles_and_profile_version_are_not_producer_versions(self):
        raw, source = self.fixture()
        for changes in ({"software_name": "absent"}, {"bound_observations_sha256": "sha256:" + "0" * 64}):
            output, report = module.convert(raw, self.expected(raw, source, **changes))
            common = json.loads(self.read(output, CASE / "profile.toml", True))
            self.assertEqual(report["summary_rows"], 1)
            self.assertEqual(common["observations"][0]["fields"]["disposition"], "INSUFFICIENT")
            self.assertEqual(common["recorded_failure_candidates"][0]["outcome"], "unknown")
        original = json.loads(raw)
        for change in ("wrong_profile", "wrong_role", "caller_only", "bad_filter", "bool_count", "wrong_clock", "missing_conflict"):
            document = json.loads(raw)
            if change == "wrong_profile":
                document["profile_identity"]["profile_id"] = "another_profile"
            else:
                fields = document["observations"][0]["fields"]
                if change == "wrong_role": fields["identity_role"] = "ADAPTER_PROFILE"
                if change == "caller_only": fields["identity_evidence"] = "CALLER_DECLARED"
                if change == "bad_filter": fields["identity_join_hex"] = binding.text('{"name":"wrong","version":""}')
                if change == "bool_count": fields["identity_rows_carrying_value"] = True
                if change == "wrong_clock": document["observations"][0]["clock_basis"] = "BootRelative"
                if change == "missing_conflict": fields["identity_missing_components"] = "NAME"
            changed = json.dumps(document).encode()
            if change == "wrong_role":
                _, report = module.convert(changed, self.expected(changed, source))
                self.assertEqual(report["unselected_observations"], 2)
            else:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    module.convert(changed, self.expected(changed, source))
        self.assertEqual(original["profile_identity"]["declared_version"], "1")
        missing_raw, missing_source = self.fixture(b"record_time_us,name,version\n1,,9\n1,alpha,\n")
        output, report = module.convert(missing_raw, self.expected(missing_raw, missing_source))
        self.assertEqual(report["membership_accounting"]["values_not_reported"], 2)
        self.assertEqual({r["disposition"] for r in csv.DictReader(io.StringIO(output))}, {"INSUFFICIENT"})

    def test_runtime_rejects_mutated_qualification_or_membership(self):
        raw, source = self.fixture()
        text, _ = module.convert(raw, self.expected(raw, source))
        row = next(r for r in csv.DictReader(io.StringIO(text)) if r["disposition"] == "MISMATCH")
        for change in ({"software_role": "ADAPTER_PROFILE"}, {"check_kind": "PROFILE_IDENTITY"},
                       {"reported_version_basis": "profile_version"}, {"comparison_basis": "arbitrary"},
                       {"read_report_sha256": "bad"}, {"identity_evidence": "CALLER_DECLARED"},
                       {"expected_set_hex": binding.text('["2.0"]')}, {"actual_declared_hex": ""},
                       {"software_name_hex": binding.text("beta")}, {"source_observation_index": "-1"},
                       {"identity_rows_carrying_value": "0"}, {"insufficient_reason": "missing"},
                       {"identity_join_hex": binding.text('{"name":"wrong","name":"alpha","version":"2.0"}')}):
            changed = dict(row, **change)
            buf = io.StringIO()
            writer = csv.DictWriter(buf, row)
            writer.writeheader(); writer.writerow(changed)
            common = json.loads(self.read(buf.getvalue(), CASE / "profile.toml", True))
            with self.subTest(change=change):
                self.assertEqual(common["recorded_failure_candidates"][0]["outcome"], "unknown")
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "clock.toml"
            profile.write_text((CASE / "profile.toml").read_text().replace(
                'default_clock_basis = "unknown"', 'default_clock_basis = "boot_relative"'))
            common = json.loads(self.read(text, profile, True))
            self.assertTrue(all(row["outcome"] == "unknown" for row in common["recorded_failure_candidates"]))

    def test_expectation_bounds_and_cli(self):
        raw, source = self.fixture()
        expected = self.expected(raw, source)
        for change in ({"expected_versions": []}, {"expected_versions": ["1", "1"]},
                       {"expected_versions": [None]}, {"source_role": "ADAPTER_PROFILE"},
                       {"software_name": ""}, {"citation": ""}, {"capture_time_us": True},
                       {"read_report_sha256": "sha256:" + "0" * 64}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.convert(raw, self.expected(raw, source, **change))
        with self.assertRaises(ValueError):
            module.convert(raw, expected[:-1] + b',"schema":"duplicate"}')
        for cap in ("INPUT_LIMIT", "EXPECTATION_LIMIT", "ROW_LIMIT", "OUTPUT_LIMIT", "REPORT_LIMIT"):
            with patch.object(module, cap, 1), self.assertRaises(ValueError):
                module.convert(raw, expected)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "report.json").write_bytes(raw)
            (base / "expected.json").write_bytes(expected)
            args = [os.sys.executable, str(ROOT / "scripts/compare_producer_software_version.py"),
                    str(base / "report.json"), str(base / "expected.json"), str(base / "out")]
            result = subprocess.run(args, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((base / "out/observations.csv").read_text(), module.convert(raw, expected)[0])
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
