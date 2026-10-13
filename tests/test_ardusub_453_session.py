"""Fixed source compatibility with authored reports, not installed BlueOS proof."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_ardusub_session as session
from scripts import convert_ardupilot_mode_csv as mode
from tests.test_ardusub_session import records, report, ROOT

PIN = "80e8c23a7a07d0653fc1ce6b6a55ee17889c77af"


def source(value):
    rows = records(value)
    rows[0]["data"]["Message"] = "ArduSub V4.5.3 (80e8c23a)"
    return rows


class Sub453Session(unittest.TestCase):
    def convert(self, rows, **options):
        return session.convert(report(*rows), source_pin=PIN,
            session_id="authored-453", single_boot_session=True, **options)

    def test_common_families_reuse_and_explicit_domain(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "reader-equipped mandatory stage required")
        for value, domain in ((12.0, False), (24.0, True)):
            rows = source(value)
            files, accounting = self.convert(rows, qualify_session_domain=domain)
            self.assertEqual(accounting["schema"], "saved-sub453-session/v1")
            self.assertEqual(accounting["source_pin"], PIN)
            self.assertEqual(accounting["missing_required_families"], [])
            self.assertEqual(accounting["selected_records"], 5)
            with tempfile.TemporaryDirectory() as directory:
                for name, data in files.items():
                    (Path(directory) / name).write_bytes(data)
                for output in accounting["outputs"]:
                    profile = Path(directory) / output["profile"] if domain else ROOT / output["profile"]
                    result = json.loads(subprocess.run([reader, str(profile),
                        str(Path(directory) / output["file"]), *output["reader_options"]],
                        check=True, capture_output=True).stdout)
                    self.assertEqual(len(result["observations"]), 1)
                    obs = result["observations"][0]
                    fields = obs["fields"]
                    self.assertEqual(obs["clock_basis"], "BootRelative")
                    self.assertEqual(result["platform_domain"],
                        "Subsurface" if domain or output["file"] == "CTUN.csv" else "Unknown")
                    self.assertEqual(fields["session_report_sha256"],
                        "sha256:" + hashlib.sha256(report(*rows)).hexdigest())
                    self.assertEqual(json.loads(bytes.fromhex(fields["session_record_hex"][4:])),
                        rows[fields["session_source_record_index"]])
                    if output["file"] == "MODE.csv":
                        self.assertEqual(fields["reported_mode_name"], "MANUAL")
                    elif output["file"] == "IMU.csv":
                        self.assertEqual(fields["GyrX"], value)
                        self.assertEqual(result["profile_units"]["GyrX"], "rad/s")
                    elif output["file"] == "BAT.csv":
                        self.assertEqual(fields["battery_voltage_v"], value)
                        self.assertIsNone(fields["battery_current_a"])

    def test_cross_build_refusals_and_missing_family(self):
        with self.assertRaises(ValueError):
            mode.convert("TimeUS,Mode,ModeNum,Rsn\n1,19,19,1\n", "sub-4.5.3", record="ver")
        with self.assertRaises(ValueError):
            self.convert(records(12))
        with self.assertRaises(ValueError):
            session.convert(report(*source(12)), source_pin=session.PIN,
                session_id="wrong", single_boot_session=True)
        for mutation in ("missing-msg", "mixed", "missing-axis", "regression"):
            rows = copy.deepcopy(source(12))
            if mutation == "missing-msg":
                rows.pop(0)
            elif mutation == "mixed":
                rows.append(records(12)[0])
            elif mutation == "missing-axis":
                del rows[5]["data"]["AccZ"]
            else:
                rows[5]["data"]["TimeUS"] = 0
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.convert(rows)
        _, accounting = self.convert(source(12)[:5])
        self.assertEqual(accounting["missing_required_families"], ["IMU"])
        self.assertEqual(accounting["disposition"], "INCOMPLETE_REQUIRED_INPUT")
