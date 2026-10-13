"""Source-shaped saved Sub rates; no firmware, vehicle or control execution."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.saved_ardupilot_profiles import INPUT_PROFILE

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/ardusub-vertical"
SOURCE = "ardusub-4.5.7-ctun"
BASIS = "pymavlink_decoded_no_second_wire_scaling"


def record(target=123, estimate=-45, stamp=1000):
    return dict(meta=dict(type="CTUN"), data=dict(TimeUS=stamp, DCRt=target, CRt=estimate,
        ThI=.2, DAlt=1.5, Future={"unknown": [None, False, "保持"]}))


def report(*records):
    return json.dumps(dict(value_basis=BASIS, source_records=records)).encode()


class VerticalReports(unittest.TestCase):
    def setUp(self):
        self.c = importlib.import_module("scripts.convert_ardusub_vertical_reports")

    def test_declared_surface_two_sessions_common_and_rejection_boundaries(self):
        common = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(common, "mandatory reader-equipped stage required")
        for session, z, estimate in (("first", 2.0, -3.0), ("second", -4.0, -2.0)):
            first = record(stamp=1000); first["data"].update(Alt=z, DAlt=z)
            second = record(stamp=2000); second["data"].update(Alt=estimate, DAlt=estimate + 1)
            raw = report(dict(meta=dict(type="MSG"), data=dict(Message="ArduSub V4.5.7 (30257f01)")), first, second)
            anchor = dict(schema="ardusub-declared-surface/v1", source_report_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                source_id="source", session_id=session, origin_epoch_id="origin-1", citation="authored test, not water measurement",
                anchor_record_index=1, anchor_time_us=1000, anchor_z_up_m=z, valid_start_us=1000,
                valid_stop_us=2000, origin_reset_times_us=[1000], unit="m", positive_direction="up", reference="reported_ekf_origin")
            encoded = json.dumps(anchor).encode()
            text, counts = self.c.convert(raw, source_version=SOURCE, underwater_position=True, surface_anchor=encoded)
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "out.csv"; output.write_text(text)
                observed = json.loads(subprocess.run([common, str(CASE / "underwater-profile.toml"), str(output)], check=True, capture_output=True).stdout)
            fields = observed["observations"][1]["fields"]
            self.assertEqual(fields["declared_surface_estimated_depth_down_m"], z - estimate)
            self.assertEqual(fields["declared_surface_target_depth_down_m"], z - estimate - 1)
            self.assertEqual(fields["reported_estimated_z_up_m"], estimate)
            self.assertEqual(fields["anchor_declaration_sha256"], "sha256:" + hashlib.sha256(encoded).hexdigest())
            self.assertIn("CONDITIONAL", counts["depth_disposition"])
            self.assertEqual((counts["selected_records"], counts["not_selected_records"]), (2, 1))
            for key, value in (("source_report_sha256", "sha256:wrong"), ("anchor_record_index", 0),
                    ("anchor_time_us", 1001), ("anchor_z_up_m", z + 1), ("unit", "cm"),
                    ("origin_reset_times_us", [1500]), ("valid_stop_us", 1999), ("anchor_time_us", True)):
                bad = dict(anchor, **{key: value})
                with self.subTest(key=key), self.assertRaises(ValueError):
                    self.c.convert(raw, source_version=SOURCE, underwater_position=True, surface_anchor=json.dumps(bad).encode())
            missing = dict(anchor); del missing["origin_epoch_id"]
            with self.assertRaises(ValueError):
                self.c.convert(raw, source_version=SOURCE, underwater_position=True, surface_anchor=json.dumps(missing).encode())
            self.assertEqual(self.c.convert(raw, source_version=SOURCE, underwater_position=True),
                self.c.convert(raw, source_version=SOURCE, underwater_position=True, surface_anchor=None))

    def test_underwater_two_fixed_builds_reach_common_without_surface_depth_inference(self):
        common = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(common, "mandatory reader-equipped stage required")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.csv"
            for version, git_id, altitude in (("4.5.7", "30257f01", -3.25), ("4.5.3", "80e8c23a", 0.0)):
                selected = record(); selected["data"].update(Alt=altitude, DAlt=-2.5)
                identity = dict(meta=dict(type="MSG"), data=dict(Message=f"ArduSub V{version} ({git_id})"))
                raw = report(identity, selected)
                text, counts = self.c.convert(raw, source_version=f"ardusub-{version}-ctun", underwater_position=True)
                output.write_text(text)
                observed = json.loads(subprocess.run([common, str(CASE / "underwater-profile.toml"), str(output)],
                    check=True, capture_output=True).stdout)
                self.assertEqual(observed["platform_domain"], "Subsurface")
                fields = observed["observations"][0]["fields"]
                self.assertEqual(fields["reported_estimated_z_up_m"], altitude)
                self.assertEqual(fields["reported_target_z_up_m"], -2.5)
                self.assertEqual(fields["source_report_record_index"], 1)
                self.assertEqual(observed["observations"][0]["clock_basis"], "BootRelative")
                self.assertEqual(counts["identity_record_indices"], [0])
                self.assertIn("UNQUALIFIED", counts["depth_disposition"])
                self.assertNotIn("depth_m", fields)
                self.assertEqual(counts["source_records"], counts["selected_records"] + counts["not_selected_records"])
                self.assertEqual(json.loads(bytes.fromhex(fields["source_record_hex"][4:])), selected)

    def test_underwater_rejects_unbound_identity_wrong_build_and_missing_position(self):
        selected = record(); selected["data"]["Alt"] = -1
        identity = dict(meta=dict(type="MSG"), data=dict(Message="ArduSub V4.5.7 (30257f01)"))
        mixed = report(identity, dict(meta=dict(type="MSG"), data=dict(Message="ArduCopter V4.5.7 (deadbeef)")), selected)
        with self.assertRaisesRegex(ValueError, "conflicting vehicle firmware"):
            self.c.convert(mixed, source_version=SOURCE, underwater_position=True)
        for raw in (report(selected), report(dict(meta=dict(type="MSG"), data=dict(Message="ArduSub V4.5.7 (deadbeef)")), selected),
                    report(identity, record()), report(identity, dict(meta=dict(type="CTUN"), data=dict(selected["data"], Alt=True)))):
            with self.assertRaises(ValueError):
                self.c.convert(raw, source_version=SOURCE, underwater_position=True)
        raw = report(identity, selected)
        self.assertEqual(self.c.convert(raw, source_version=SOURCE),
                         self.c.convert(raw, source_version=SOURCE, underwater_position=False))

    def test_two_actual_reader_inputs_keep_signed_rates_accounting_and_unknown_freshness(self):
        native = os.environ.get("MUSUBI_MAVLOG_READER")
        common = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not native or not common:
            self.skipTest("both actual readers required")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            profile = folder / "native.toml"; profile.write_text(INPUT_PROFILE)
            for target, estimate in ((123, -45), (-32768, 32767)):
                rows = [record(target, estimate), dict(meta=dict(type="OTHER"), data=dict(Note="kept")),
                        record(0, 0)]
                path = folder / "source.jsonl"
                path.write_text("".join(json.dumps(r) + "\n" for r in rows))
                raw = subprocess.run([native, str(profile), str(path)], check=True, capture_output=True).stdout
                text, counts = self.c.convert(raw, source_version=SOURCE)
                self.assertEqual((counts["source_records"], counts["selected_records"], counts["not_selected_records"]), (3, 2, 1))
                self.assertEqual(counts["unselected_record_types"], {"OTHER": 1})
                output = folder / "out.csv"; output.write_text(text)
                observed = json.loads(subprocess.run([common, str(CASE / "profile.toml"), str(output),
                    "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(observed["platform_domain"], "Unknown")
                self.assertEqual(observed["equal_time_order"], "source-order-only")
                self.assertEqual(observed["main_rows"], 2)
                for index, expected in enumerate(((target / 100, estimate / 100), (0, 0))):
                    obs = observed["observations"][index]; fields = obs["fields"]
                    self.assertEqual((obs["t_boot_us"], obs["clock_basis"]), (1000, "BootRelative"))
                    self.assertEqual((fields["reported_target_vertical_rate_m_s"], fields["reported_estimated_vertical_rate_m_s"]), expected)
                    self.assertEqual(fields["source_report_record_index"], index * 2)
                    self.assertEqual(fields["source_document_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.assertEqual(fields["vertical_rate_freshness"], "UNKNOWN_CTUN_HAS_NO_VALIDITY_OR_SAMPLE_AGE")
                    self.assertIn("NOT_ACTUATION", fields["vertical_rate_basis"])
                    self.assertIn("retained", observed["profile_units"]["reported_estimated_vertical_rate_m_s"])
                    self.assertNotIn("depth_m", fields)
                    self.assertNotIn("DAlt", fields)
                    retained = json.loads(bytes.fromhex(fields["source_record_hex"][4:]))
                    self.assertEqual(retained, rows[index * 2])

    def test_refuses_missing_invalid_source_clock_shape_and_bounds(self):
        for source in (None, "", "ardusub-4.5.6-ctun", "copter-4.5.5"):
            with self.assertRaises(ValueError): self.c.convert(report(record()), source_version=source)
        for key, value in (("DCRt", None), ("DCRt", True), ("DCRt", 1.5), ("CRt", "2"),
                           ("CRt", -32769), ("CRt", 32768), ("TimeUS", -1), ("TimeUS", 2**63)):
            bad = record(); bad["data"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.c.convert(report(bad), source_version=SOURCE)
        missing = record(); del missing["data"]["CRt"]
        for raw in (report(missing), report(record(stamp=2), record(stamp=1)), report(),
                    report(dict(meta=dict(type="OTHER"), data={})), report(dict(meta={}, data={})),
                    report(record()).replace(BASIS.encode(), b"wire_scaled"),
                    report(record()).replace(b'"DCRt": 123', b'"DCRt": 123, "DCRt": 456'),
                    report(record()).replace(b'"DCRt": 123', b'"DCRt": NaN')):
            with self.assertRaises(ValueError): self.c.convert(raw, source_version=SOURCE)
        for limit, value in (("MAX_INPUT", 10), ("MAX_OUTPUT", 10), ("MAX_REPORT", 10),
                             ("MAX_RECORD_BYTES", 10), ("MAX_ROWS", 1)):
            with patch.object(self.c, limit, value), self.assertRaises(ValueError):
                self.c.convert(report(record(), record()), source_version=SOURCE)

    def test_cli_retains_original_once_and_refuses_overwrite(self):
        raw = report(record(0, -1), dict(meta=dict(type="MSG"), data={}))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); source = folder / "report.json"; source.write_bytes(raw)
            output = folder / "out"
            command = [sys.executable, str(ROOT / "scripts/convert_ardusub_vertical_reports.py"),
                       str(source), str(output), "--source-version", SOURCE]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / "source-report.json").read_bytes(), raw)
            text = (output / "observations.csv").read_text()
            row = next(csv.DictReader(io.StringIO(text)))
            self.assertEqual(float(row["reported_estimated_vertical_rate_m_s"]), -.01)
            self.assertEqual(float(row["reported_target_vertical_rate_m_s"]), 0)
            self.assertEqual(json.loads((output / "report.json").read_text())["not_selected_records"], 1)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((output / "observations.csv").read_text(), text)


if __name__ == "__main__":
    unittest.main()
