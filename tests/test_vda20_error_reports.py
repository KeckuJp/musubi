"""Fixed VDA2.0 authored error reports, not physical fault evidence."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_electrical_schema_reuse import module, vda, ROOT


class Vda20ErrorTests(unittest.TestCase):
    def test_distinct_inputs_to_common_and_unknown_fields(self):
        for level, references in (("WARNING", [{"referenceKey": "nodeId", "referenceValue": "n7"}]),
                                  ("URGENT", [])):
            record = vda("2.0.0")
            record["errors"] = [{"errorType": "reported", "errorLevel": level,
                                 "errorDescription": "", "errorReferences": references,
                                 "errorHint": {"foreign": "unqualified"}}]
            text = json.dumps(record)
            output = module.convert(text, "vda-state-2.0", vda_errors=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(rows[1]["vda_error_level_reported"], level if level == "WARNING" else "UNKNOWN")
            self.assertEqual(rows[1]["vda_error_level_raw_hex"], "hex:" + level.encode().hex())
            self.assertEqual(rows[1]["vda_error_description_hex"], "hex:")
            self.assertEqual(rows[1]["vda_error_hint_hex"], "")
            self.assertEqual(rows[1]["vda_error_reference_count"], str(len(references)))
            self.assertIn("errorHint", bytes.fromhex(rows[0]["source_record_hex"][4:]).decode())
            reader = os.environ.get("MUSUBI_TELEMETRY_READER")
            if not reader:
                self.skipTest("common reader executable not configured")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "errors.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"), str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(len(common["observations"]), 2 + len(references))
                fields = common["observations"][1]["fields"]
                self.assertEqual(fields["vda_error_level_raw_hex"], rows[1]["vda_error_level_raw_hex"])
                self.assertEqual(fields["vda_source_sha256"], rows[0]["vda_source_sha256"])

    def test_absent_empty_and_invalid(self):
        record = vda("2.0.0")
        error = {"errorType": "x", "errorLevel": "FATAL"}
        output = module.convert(json.dumps({**record, "errors": [error]}), "vda-state-2.0", vda_errors=True)
        row = list(csv.DictReader(io.StringIO(output)))[1]
        self.assertEqual(row["vda_error_reference_count"], "")
        self.assertEqual(row["vda_error_description_hex"], "")
        for bad in ({**error, "errorDescription": None}, {**error, "errorReferences": [{}]},
                    {**error, "errorReferences": None}):
            with self.assertRaises(ValueError):
                module.convert(json.dumps({**record, "errors": [bad]}), "vda-state-2.0", vda_errors=True)
        with self.assertRaises(ValueError):
            module.convert(json.dumps(vda("2.1.0")), "vda-state-2.0", vda_errors=True)
