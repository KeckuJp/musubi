"""Fixed-build domain inheritance, not device authentication."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_ardusub_session as session
from tests.test_ardusub_session import records, report, ROOT


class SessionDomain(unittest.TestCase):
    def test_two_existing_reports_actual_domain_and_original_meanings(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader)
        for value in (12.0, 24.0):
            raw = report(*records(value))
            options = dict(source_pin=session.PIN, session_id="same-source", single_boot_session=True)
            original, old_report = session.convert(raw, **options)
            files, accounting = session.convert(raw, **options, qualify_session_domain=True)
            self.assertEqual(accounting["selected_counts"], old_report["selected_counts"])
            self.assertEqual(accounting["source_report_sha256"], old_report["source_report_sha256"])
            with tempfile.TemporaryDirectory() as directory:
                for name, content in files.items():
                    (Path(directory) / name).write_bytes(content)
                for item in accounting["outputs"]:
                    result = json.loads(subprocess.run([reader, str(Path(directory) / item["profile"]),
                        str(Path(directory) / item["file"]), *item["reader_options"]],
                        check=True, capture_output=True).stdout)
                    self.assertEqual(result["platform_domain"], "Subsurface")
                    self.assertEqual(result["observations"][0]["clock_basis"], "BootRelative")
                    before = list(csv.DictReader(io.StringIO(original[item["file"]].decode())))[0]
                    after = list(csv.DictReader(io.StringIO(files[item["file"]].decode())))[0]
                    self.assertEqual({key: after[key] for key in before}, before)
                    self.assertEqual(after["session_original_domain"],
                                     "Subsurface" if item["file"] == "CTUN.csv" else "Unknown")
                    self.assertEqual(after["session_report_sha256"], accounting["source_report_sha256"])
            self.assertEqual(session.convert(raw, **options, qualify_session_domain=False), (original, old_report))

    def test_build_and_type_refuse_without_caller_name_promotion(self):
        options = dict(source_pin=session.PIN, session_id="BlueROV2", single_boot_session=True)
        for option in (1, "false", None):
            with self.assertRaises(ValueError):
                session.convert(report(*records(12)), **options, qualify_session_domain=option)
        bad = records(12)
        bad[0]["data"]["Message"] = "ArduSub V4.5.3 (80e8c23a)"
        with self.assertRaises(ValueError):
            session.convert(report(*bad), **options, qualify_session_domain=True)
        with self.assertRaises(ValueError):
            session.convert(report(*records(12)), **{**options, "source_pin": "wrong"}, qualify_session_domain=True)
        with self.assertRaises(ValueError):
            session.convert(report(*records(12)), **options, qualify_session_domain=True,
                            surface_anchor=b'{"source_document_sha256":"wrong"}')
