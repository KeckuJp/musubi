"""Authored same-row identity reports, explicit uniqueness scope, actual common candidates."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_identifier_uniqueness as module
from scripts import convert_software_identity_binding as binding

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/identifier-uniqueness"
IDENTITY = ROOT / "profiles/declared/software-identity-binding/profile.toml"


class IdentifierUniquenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.reader:
            raise unittest.SkipTest("actual rebuilt reader required")

    def read(self, source, profile=IDENTITY, enabled=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text(source)
            args = [self.reader, str(profile), str(path), "--allow-equal-time"]
            if enabled:
                args.append("--recorded-failure-candidates")
            return subprocess.run(args, check=True, capture_output=True).stdout

    def fixture(self, tables=None):
        tables = tables or [b"record_time_us,key,name\n1,k,alpha\n1,k,beta\n1,k,alpha\n1,other,gamma\n"]
        parts = []
        for table in tables:
            text, _ = binding.convert(table, paired_identities=("SOURCE_SOFTWARE=PIN:key,NAME:name",),
                namespace="saved-id-domain", instance="source-A", source_citation="authored saved report")
            parts.append(text if not parts else text.split("\n", 1)[1])
        return self.read("".join(parts)), [module.shared.digest(t) for t in tables]

    def declaration(self, report, sources, **changes):
        value = dict(schema=module.SCHEMA, read_report_sha256=module.shared.digest(report),
            scope_id="declared-allocation", namespace="saved-id-domain", instance="source-A",
            source_role="SOURCE_SOFTWARE", subject_kind="PIN", subject_key="k", attribute_kind="NAME",
            max_distinct_values=1, windows=[dict(window_id="window-A", bound_observations_sha256=sources)],
            citation="explicit single-name-per-key expectation", capture_time_us=25)
        value.update(changes)
        return json.dumps(value).encode()

    def convert_common(self, raw, declaration):
        output, report = module.convert(raw, declaration)
        return json.loads(self.read(output, CASE / "profile.toml", True)), output, report

    def test_same_declared_scope_conflict_reaches_three_candidates(self):
        raw, sources = self.fixture()
        declared = self.declaration(raw, sources)
        common, output, report = self.convert_common(raw, declared)
        fields = common["observations"][0]["fields"]
        self.assertEqual((fields["disposition"], fields["distinct_values"], fields["selected_pairs"]),
                         ("CONTRADICTION", 2, 2))
        self.assertEqual((report["input_observations"], report["selected_observations"], report["unselected_observations"]), (3, 2, 1))
        witnesses = json.loads(module.shared.unhex(fields["witnesses_hex"]))
        self.assertEqual(sorted(w["rows"] for w in witnesses), [1, 2])
        self.assertEqual({w["pin"] for w in witnesses}, {"k"})
        self.assertEqual({w["name"] for w in witnesses}, {"alpha", "beta"})
        self.assertEqual(bytes.fromhex(report["source_report_hex"][4:]), raw)
        self.assertEqual(bytes.fromhex(report["source_declaration_hex"][4:]), declared)
        result = common["recorded_failure_candidates"][0]
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["clock_basis"], "Unknown")
        self.assertIsNone(result["time_window"])
        self.assertEqual(result["comparison"]["scope_id_hex"], binding.text("declared-allocation"))
        self.assertTrue(result["indistinguishable_by"])
        for item in result["candidates"]:
            self.assertEqual(item["logic_confidence"], "leading_indicator")
            self.assertEqual(item["supporting"], result["evidence_digests"])
        plain = json.loads(self.read(output, CASE / "profile.toml"))
        self.assertNotIn("recorded_failure_candidates", plain)
        self.assertEqual(plain["observations"], common["observations"])

    def test_other_subject_separate_windows_repeat_and_missing_abstain(self):
        tables = [b"record_time_us,key,name\n1,k,alpha\n1,k,alpha\n1,other,beta\n",
                  b"record_time_us,key,name\n1,k,beta\n"]
        raw, sources = self.fixture(tables)
        windows = [dict(window_id=str(i), bound_observations_sha256=[s]) for i, s in enumerate(sources)]
        common, _, report = self.convert_common(raw, self.declaration(raw, sources, windows=windows))
        self.assertEqual([o["fields"]["disposition"] for o in common["observations"]], ["CONSISTENT"] * 2)
        self.assertTrue(all(c["outcome"] == "unknown" for c in common["recorded_failure_candidates"]))
        self.assertEqual(report["unselected_observations"], 1)
        for table in (b"record_time_us,key,name\n1,k,\n", b"record_time_us,key,name\n1,,beta\n",
                      b"record_time_us,key,name\n1,other,beta\n"):
            missing, digests = self.fixture([table])
            common, _, _ = self.convert_common(missing, self.declaration(missing, digests))
            self.assertEqual(common["observations"][0]["fields"]["disposition"], "INSUFFICIENT")
            self.assertEqual(common["recorded_failure_candidates"][0]["outcome"], "unknown")
        common, _, _ = self.convert_common(raw, self.declaration(raw, sources + ["sha256:" + "0" * 64]))
        self.assertEqual(common["observations"][0]["fields"]["insufficient_reason"], "DECLARED_SOURCE_NOT_REPORTED")
        raw, sources = self.fixture()
        truncated = json.loads(raw)
        # Remove the beta tuple but keep the surviving report structurally complete.
        truncated["observations"] = [o for o in truncated["observations"]
            if json.loads(module.shared.unhex(o["fields"]["identity_value_hex"]))["NAME"] != "beta"]
        truncated["main_rows"] = len(truncated["observations"])
        incomplete = json.dumps(truncated).encode()
        common, _, _ = self.convert_common(incomplete, self.declaration(incomplete, sources))
        self.assertEqual(common["observations"][0]["fields"]["insufficient_reason"], "SOURCE_PAIR_ACCOUNTING_INCOMPLETE")
        self.assertEqual(common["observations"][0]["fields"]["window_sources_reported"], 0)
        self.assertEqual(common["observations"][0]["fields"]["disposition"], "INSUFFICIENT")
        self.assertEqual(common["recorded_failure_candidates"][0]["outcome"], "unknown")

    def test_invalid_scope_qualification_and_mutated_candidate_abstain(self):
        raw, sources = self.fixture()
        for change in ({"namespace": "another"}, {"instance": "other"}, {"scope_id": ""},
                       {"subject_kind": "UUID"}, {"attribute_kind": "VERSION"},
                       {"max_distinct_values": True}, {"subject_key": ""}, {"source_role": "ADAPTER_PROFILE"},
                       {"windows": []}, {"capture_time_us": True}, {"read_report_sha256": "bad"},
                       {"windows": [dict(window_id="A", bound_observations_sha256=sources),
                                    dict(window_id="B", bound_observations_sha256=sources)]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.convert(raw, self.declaration(raw, sources, **change))
        original = json.loads(raw)
        for change in ("duplicate", "filter", "count", "caller", "missing", "clock"):
            doc = json.loads(raw)
            f = doc["observations"][0]["fields"]
            if change == "duplicate":
                doc["observations"].append(doc["observations"][0]); doc["main_rows"] += 1
            if change == "filter": f["identity_join_hex"] = binding.text('{"key":"bad","name":"alpha"}')
            if change == "count": f["identity_rows_carrying_value"] = True
            if change == "caller": f["identity_evidence"] = "CALLER_DECLARED"
            if change == "missing": f["identity_missing_components"] = "PIN"
            if change == "clock": doc["observations"][0]["clock_basis"] = "BootRelative"
            data = json.dumps(doc).encode()
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.convert(data, self.declaration(data, sources))
        self.assertEqual(original["profile_identity"]["declared_version"], "1")
        _, output, _ = self.convert_common(raw, self.declaration(raw, sources))
        row = next(csv.DictReader(io.StringIO(output)))
        witnesses = json.loads(module.shared.unhex(row["witnesses_hex"]))
        wrong_pin = json.loads(json.dumps(witnesses)); wrong_pin[0]["pin"] = "other"
        for mutation in ({"source_role": "ADAPTER_PROFILE"}, {"uniqueness_basis": "guessed"},
                         {"scope_id_hex": ""}, {"distinct_values": "1"}, {"missing_pairs": "1"},
                         {"max_distinct_values": "2"}, {"declaration_sha256": "bad"},
                         {"witnesses_hex": binding.text(json.dumps(wrong_pin))},
                         {"witnesses_hex": binding.text(json.dumps(witnesses + witnesses))}):
            buf = io.StringIO(); writer = csv.DictWriter(buf, row); writer.writeheader()
            writer.writerow(dict(row, **mutation))
            common = json.loads(self.read(buf.getvalue(), CASE / "profile.toml", True))
            with self.subTest(mutation=mutation):
                self.assertEqual(common["recorded_failure_candidates"][0]["outcome"], "unknown")

    def test_bounds_schema_and_cli(self):
        raw, sources = self.fixture()
        declared = self.declaration(raw, sources)
        for cap in ("INPUT_LIMIT", "DECLARATION_LIMIT", "ROW_LIMIT", "WITNESS_LIMIT", "OUTPUT_LIMIT", "REPORT_LIMIT", "WITNESS_BYTES"):
            with patch.object(module, cap, 1), self.assertRaises(ValueError):
                module.convert(raw, declared)
        for malformed in (declared[:-1] + b',"scope_id":"duplicate"}', self.declaration(raw, sources, extra=1)):
            with self.assertRaises(ValueError): module.convert(raw, malformed)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "report.json").write_bytes(raw); (base / "declaration.json").write_bytes(declared)
            args = [os.sys.executable, str(ROOT / "scripts/check_identifier_uniqueness.py"),
                    str(base / "report.json"), str(base / "declaration.json"), str(base / "out")]
            done = subprocess.run(args, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((base / "out/observations.csv").read_text(), module.convert(raw, declared)[0])
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
