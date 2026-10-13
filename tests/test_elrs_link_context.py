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
from scripts import convert_elrs_link_context as subject
from tests.test_crsf_link import frame, capture

ROOT = Path(__file__).resolve().parents[1]


class ElrsLinkContextTests(unittest.TestCase):
    def test_cli_preserves_crlf_original_digests(self):
        settings, capture_text, binding = self.inputs()
        settings = json.dumps(json.loads(settings), indent=2).replace("\n", "\r\n")
        capture_text = capture_text.replace("\n", "\r\n")
        binding["settings_sha256"] = hashlib.sha256(settings.encode()).hexdigest()
        binding["capture_sha256"] = hashlib.sha256(capture_text.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "settings.json").write_bytes(settings.encode())
            (base / "capture.csv").write_bytes(capture_text.encode())
            (base / "binding.json").write_text(json.dumps(binding))
            result = subprocess.run([sys.executable, "-m", "scripts.convert_elrs_link_context",
                str(base / "settings.json"), str(base / "capture.csv"), str(base / "binding.json"),
                str(base / "out")], cwd=ROOT, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/elrs-link-context/profile.toml"),
                str(base / "out/observations.csv")], check=True, capture_output=True).stdout)
            fields = common["observations"][0]["fields"]
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(fields["settings_sha256"], binding["settings_sha256"])
            self.assertEqual(fields["capture_sha256"], binding["capture_sha256"])

    def inputs(self, model=3, rate=7, ratio=5):
        settings = json.dumps({"config": {"model": {str(model): {"packet-rate": rate, "telemetry-ratio": ratio}}, "uid": [1, 2, 3]}})
        handset_frame = b"\xea" + frame()[1:]
        report = capture([(12, handset_frame), (15, handset_frame)])
        binding = dict(settings_sha256=hashlib.sha256(settings.encode()).hexdigest(), capture_sha256=hashlib.sha256(report.encode()).hexdigest(),
            session_label="operator-case", model_index=model, radio="sx128x", start_record=0, stop_record=2, settings_capture_time_us=1)
        return settings, report, binding

    def test_two_models_settings_and_reported_quality_reach_common(self):
        for model, rate, ratio, hz, denominator in ((3, 7, 5, 150, 16), (4, 6, 8, 250, 2)):
            text, report = subject.convert(*self.inputs(model, rate, ratio))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(text)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(ROOT / "profiles/declared/elrs-link-context/profile.toml"), str(path)], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["packet_rate_configured_hz"], hz)
            self.assertEqual(fields["telemetry_ratio_denominator"], denominator)
            self.assertEqual(fields["uplink_rssi_1_dbm"], -70)
            self.assertEqual(fields["uplink_lq_fraction"], .99)
            self.assertEqual(fields["binding_source_record_index"], 0)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            self.assertTrue(report["settings"]["redacted_fields"])

    def test_missing_unknown_and_mismatched_binding(self):
        settings, capture_text, binding = self.inputs()
        missing = json.dumps({"config": {"model": {"3": {"model-match": 1}}}})
        binding["settings_sha256"] = hashlib.sha256(missing.encode()).hexdigest()
        text, _ = subject.convert(missing, capture_text, binding)
        row = next(csv.DictReader(io.StringIO(text)))
        self.assertEqual(row["packet_rate_status"], "NOT_REPORTED")
        self.assertEqual(row["packet_rate_configured_hz"], "")
        self.assertIn("keep-me", bytes.fromhex(row["source_record_hex"][4:]).decode())
        unknown = self.inputs(rate=15, ratio=15)
        unknown_text, _ = subject.convert(*unknown)
        unknown_row = next(csv.DictReader(io.StringIO(unknown_text)))
        self.assertEqual(unknown_row["packet_rate_configured_hz"], "")
        self.assertIn("UNKNOWN", unknown_row["telemetry_ratio_status"])
        for key, value in (("settings_sha256", "0"*64), ("capture_sha256", "1"*64), ("model_index", 9), ("stop_record", 3), ("model_index", True)):
            invalid = dict(binding); invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                subject.convert(missing, capture_text, invalid)
        wrong_destination = capture([(0, frame())])
        wrong = dict(binding, capture_sha256=hashlib.sha256(wrong_destination.encode()).hexdigest(), stop_record=1)
        with self.assertRaisesRegex(ValueError, "destination"):
            subject.convert(missing, wrong_destination, wrong)
