"""Authored records through actual saved report, comparison and bounded candidate output."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/schema-unit-checks/profile.toml"
SCRIPT = ROOT / "scripts/check_declared_schema_units.py"


class DeclaredComparisonCueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.reader:
            raise unittest.SkipTest("actual common reader required")

    def common(self, units, expected, *, mutate=None, context=False, enabled=True):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            profile = base / "source.toml"
            profile.write_text('profile_id="authored_probe"\nversion="1"\nfamily="unknown"\n'
                               'source_role="recorded_export"\nformat="telemetry_csv_us"\n'
                               'extensions=["csv"]\ndefault_clock_basis="unknown"\nchannels=["onboard"]\n'
                               '[fields]\ntime="t"\n[units]\n' +
                               "".join(f'{name}={json.dumps(unit)}\n' for name, unit in units.items()))
            source = base / "source.csv"
            source.write_text("t,probe,silent\n1000,300,\n")
            actual = subprocess.run([self.reader, str(profile), str(source)],
                                    check=True, capture_output=True).stdout.decode()
            (base / "report.json").write_text(actual)
            expectation = json.dumps({"columns": expected})
            (base / "expected.json").write_text(expectation)
            subprocess.run([os.sys.executable, str(SCRIPT), str(base / "expected.json"),
                            str(base / "report.json"), str(base / "out"), "--capture-time-us", "1000"],
                           check=True, capture_output=True)
            output = base / "out/observations.csv"
            if mutate:
                rows = list(csv.DictReader(io.StringIO(output.read_text())))
                selected = next(row for row in rows if row["disposition"] == "UNIT_SCALE_DIFFERENT")
                selected.update(mutate)
                buf = io.StringIO()
                writer = csv.DictWriter(buf, selected)
                writer.writeheader()
                writer.writerow(selected)
                output.write_text(buf.getvalue())
            args = [self.reader, str(PROFILE), str(output), "--allow-equal-time"]
            if enabled:
                args.append("--recorded-failure-candidates")
            if context:
                ctx = base / "context.json"
                ctx.write_text(json.dumps({"record_sha256": "sha256:" + "0" * 64,
                                           "expected_core_limit_bytes": 1, "observed_core_limit_bytes": 2}))
                args += ["--recorded-failure-context", str(ctx)]
            return json.loads(subprocess.run(args, check=True, capture_output=True).stdout)

    def test_actual_saved_report_schema_and_unit_candidates_remain_ambiguous(self):
        common = self.common({"probe": "K"}, {"t": None, "probe": "degC", "silent": "V", "absent": None})
        reports = common["recorded_failure_candidates"]
        eligible = [r for r in reports if r["outcome"] == "ambiguous"]
        self.assertEqual(len(eligible), 2)
        self.assertEqual(len(reports), 7)
        for report in eligible:
            self.assertEqual(report["clock_basis"], "Unknown")
            self.assertIsNone(report["time_window"])
            self.assertEqual(len(report["evidence_digests"]), 1)
            self.assertEqual(len(report["candidates"]), 2)
            self.assertTrue(report["indistinguishable_by"])
            self.assertEqual(report["context_basis"], "expectation_document_declared_not_authenticated")
            for key in ("expectation_sha256", "read_report_sha256", "artefact_input_sha256", "profile_source_sha256"):
                self.assertRegex(report["comparison"][key], r"^sha256:[0-9a-f]{64}$")
            for candidate in report["candidates"]:
                self.assertEqual(candidate["logic_confidence"], "leading_indicator")
                self.assertEqual(candidate["supporting"], report["evidence_digests"])
                self.assertTrue(candidate["discriminating_missing"])

    def test_match_equivalent_missing_and_text_only_stay_distinct(self):
        equivalent = self.common({"probe": "K"}, {"t": None, "probe": "degK", "silent": "V"})
        self.assertTrue(all(r["outcome"] == "unknown" for r in equivalent["recorded_failure_candidates"]))
        text_only = self.common({"probe": "producer_word"}, {"t": None, "probe": "other_word", "silent": None})
        eligible = [r for r in text_only["recorded_failure_candidates"] if r["outcome"] == "ambiguous"]
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["comparison"]["disposition"], "UNIT_DECLARATION_DIFFERENT")
        different_kind = self.common({"probe": "K"}, {"t": None, "probe": "V"})
        self.assertEqual({r["comparison"]["disposition"] for r in different_kind["recorded_failure_candidates"]
                          if r["outcome"] == "ambiguous"},
                         {"UNIT_QUANTITY_KIND_DIFFERENT", "SCHEMA_COLUMN_NOT_EXPECTED"})
        plain = self.common({"probe": "K"}, {"probe": "degC"}, enabled=False)
        self.assertNotIn("recorded_failure_candidates", plain)

    def test_invalid_or_contradictory_evidence_and_storage_context_abstain(self):
        for patch in ({"read_report_sha256": "bad"}, {"profile_source_sha256": ""},
                      {"actual_source": "another_origin"}, {"comparison_basis": "arbitrary"},
                      {"evidence_state": "MISSING_DECLARED_BASIS"}, {"check_subject_hex": "hex:zz"},
                      {"expected_declared_hex": "hex:4b"},
                      {"expected_offset": "0", "expected_scale": "1"},
                      {"expected_scale": "-1"}, {"actual_declared_hex": ""}):
            with self.subTest(patch=patch):
                report = self.common({"probe": "K"}, {"probe": "degC"}, mutate=patch)
                self.assertEqual(report["recorded_failure_candidates"][0]["outcome"], "unknown")
        report = self.common({"probe": "K"}, {"probe": "degC"}, context=True)
        self.assertTrue(all(r["outcome"] == "unknown" for r in report["recorded_failure_candidates"]))

    def test_existing_storage_json_is_unchanged_with_opt_in_off_and_on(self):
        from tests.test_recorded_failure_cues import RecordedFailureCueTests
        old = RecordedFailureCueTests()
        old.reader = self.reader
        # Pin the shipped public profile bytes as well as the complete output.
        for enabled, digest in (
            (False, "1ad5f55d67a8813d67b5cd75ed0085fb78c84046b06c38027b3f592358848d5d"),
            (True, "320800c7c57dd0e9ad23ac71208b760f52dd60fc68b1c4d062d92b5644cff2d7"),
        ):
            with self.subTest(enabled=enabled):
                output = old.read({"ERRNO": "28"}, enabled=enabled)
                self.assertEqual(hashlib.sha256(json.dumps(output, sort_keys=True).encode()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
