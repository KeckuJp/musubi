"""Authored supplied CAP sets; references never execute changes or authenticate messages."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_cap_alert import alert, info, AREA_ONE, AREA_TWO, PROFILE as ALERT_PROFILE
from scripts import convert_cap_alert as legacy

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/cap-saved-relations/profile.toml"
SENDER = "w-nws.webmaster@example.test"
T1, T2, T3 = [f"2026-09-12T14:0{i}:00-07:00" for i in (1, 2, 3)]


def saved(identifier, sent=T1, kind="Alert", refs=None, body=None):
    extra = "" if refs is None else f"<references>{refs}</references>"
    return alert(body, identifier=identifier, sent=sent, msg_type=kind, extra=extra)


class SavedRelationsTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module("scripts.convert_cap_saved_relations")

    def common(self, text, profile=PROFILE):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies actual common reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text(text)
            result = subprocess.run([reader, str(profile), str(path), "--allow-equal-time"],
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_original_quantities_and_explicit_update_cancel_reach_both_common_views(self):
        original = saved("a", body=info(categories=("Infra",), areas=AREA_ONE+AREA_TWO))
        update = saved("b", T2, "Update", f"{SENDER},a,{T1}", info(severity="Severe"))
        cancel = saved("c", T3, "Cancel", f"{SENDER},b,{T2}")
        documents = [cancel, original, update]  # supplied order is not inferred chronology
        outputs, relations, report = self.m.convert(documents)
        self.assertEqual(outputs, [legacy.convert(data)[0] for data in documents])
        originals = [self.common(text, ALERT_PROFILE) for text in outputs]
        self.assertEqual([r["main_rows"] for r in originals], [1, 2, 1])
        self.assertEqual(originals[1]["observations"][0]["fields"]["area_circle_radius_km"], 12.5)
        self.assertEqual(originals[2]["observations"][0]["fields"]["info_severity"], "Severe")
        common = self.common(relations)
        fields = [o["fields"] for o in common["observations"]]
        self.assertEqual([f["source_document_index"] for f in fields], [1, 2, 0])
        self.assertEqual([f["relation_disposition"] for f in fields],
                         ["NO_REFERENCES_DECLARED", "MATCHED_DECLARED_IDENTITY", "MATCHED_DECLARED_IDENTITY"])
        self.assertEqual([fields[i]["target_document_index"] for i in (1, 2)], [1, 2])
        self.assertEqual(fields[1]["target_source_sha256"], "sha256:"+hashlib.sha256(original).hexdigest())
        self.assertEqual(fields[2]["declared_relation_kind"], "CANCEL_DECLARED_REFERENCE")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        self.assertEqual((report["source_documents"], report["source_observation_rows"],
                          report["reference_tokens"], report["relation_rows"]), (3, 4, 2, 3))

    def test_ambiguous_missing_literal_time_malformed_duplicate_and_self(self):
        original = saved("a")
        # Same instant with another offset is not the same declared reference identity.
        refs = f"{SENDER},a,2026-09-12T21:01:00+00:00 {SENDER},a,{T1} broken {SENDER},b,{T1} {SENDER},a,{T1}"
        docs = [original, original, saved("b", T1, "Update", refs)]
        _, relations, report = self.m.convert(docs)
        rows = list(csv.DictReader(io.StringIO(relations)))
        source = [r for r in rows if r["source_document_index"] == "2"]
        self.assertEqual([r["reference_index"] for r in source], ["0", "1", "1", "2", "3", "4", "4"])
        self.assertEqual([r["relation_disposition"] for r in source], ["TARGET_MISSING",
            "AMBIGUOUS_SUPPLIED_IDENTITY", "AMBIGUOUS_SUPPLIED_IDENTITY", "MALFORMED_REFERENCE_RETAINED",
            "MATCHED_DECLARED_IDENTITY", "AMBIGUOUS_SUPPLIED_IDENTITY", "AMBIGUOUS_SUPPLIED_IDENTITY"])
        self.assertEqual(source[4]["target_relation_basis"], "SELF_REFERENCE_NOT_PREDECESSOR")
        self.assertEqual((report["ambiguous_references"], report["missing_references"],
                          report["malformed_references"]), (2, 1, 1))
        self.assertEqual(len(self.common(relations)["observations"]), 9)
        _, generic, _ = self.m.convert([original, saved("ack", T2, "Ack", f"{SENDER},a,{T1}")])
        self.assertIn("REFERENCE_NOT_UPDATE_OR_CANCEL", generic)
        _, absent, _ = self.m.convert([saved("empty", T1, "Cancel")])
        self.assertIn("NO_REFERENCES_DECLARED", absent)
        with self.assertRaises(ValueError): self.m.convert([saved("bad", kind="FutureType")])

    def test_refusals_bounds_and_cli_preserves_sources(self):
        documents = [saved("a"), saved("b", T2, "Update", f"{SENDER},a,{T1} {SENDER},a,{T1}")]
        for name in ("MAX_DOCUMENTS", "MAX_INPUT_BYTES", "MAX_REFERENCES", "MAX_RELATION_ROWS", "MAX_OUTPUT_BYTES"):
            with self.subTest(bound=name), patch.object(self.m, name, 1), self.assertRaises(ValueError):
                self.m.convert(documents)
        for docs in ([], [b"<bad/>"], [documents[0], b"<bad/>"], ["not bytes"]):
            with self.subTest(docs=docs), self.assertRaises(ValueError): self.m.convert(docs)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = [base/f"supplied-{i}.xml" for i in range(2)]
            for path, data in zip(paths, documents): path.write_bytes(data)
            target = base/"out"
            command = [os.sys.executable, str(ROOT/"scripts/convert_cap_saved_relations.py"),
                       "--output", str(target)] + [str(path) for path in paths]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target/"source-000.xml").read_bytes(), documents[0])
            self.assertEqual((target/"source-001.csv").read_text(), legacy.convert(documents[1])[0])
            original = (target/"report.json").read_bytes()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((target/"report.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
