"""Fixed source/schema reuse, not Copter runtime or safety validation."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_ardupilot_safety_reports import convert

ROOT = Path(__file__).resolve().parents[1]
PIN = "ardupilot-copter-4.5.5-142aece2"
GOLDEN = 'TimeUS,Mode,ModeNum,Rsn,extra\n1000000,AUTO,3,4,"note, future"\n1000000,AUTO,3,48,x\n'


def saved(codes):
    return "TimeUS,Mode,ModeNum,Rsn,extra\n" + "".join(
        f'1000000,AUTO,3,{code},"original, note"\n' for code in codes)


class CopterReason(unittest.TestCase):
    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required; CI supplies it")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text)
            result = subprocess.run([reader,
                str(ROOT / "profiles/declared/safety-reports/profile.toml"),
                str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)

    def test_two_copter_bodies_preserve_meaning_pin_clock_and_original_in_common(self):
        cases = (([10, 4, 48, 2], ["FENCE_BREACHED", "BATTERY_FAILSAFE", "RADIO_FAILSAFE_RECOVERY", "GCS_COMMAND"]),
                 ([25, 0, 255, 52], ["FAILSAFE", "UNKNOWN", "UNKNOWN_CODE_255", "DDS_COMMAND"]))
        for codes, names in cases:
            with self.subTest(codes=codes):
                source = saved(codes)
                text, report = convert(source, vehicle="copter-4.5.5", source_format="ardupilot-mode-csv")
                rows = list(csv.DictReader(io.StringIO(text)))
                self.assertEqual([row["reported_mode_reason"] for row in rows], names)
                self.assertEqual(report["producer_pin"], PIN)
                self.assertEqual(report["reason_interpretation"], PIN)
                self.assertEqual(report["unqualified_vehicle_records"], 0)
                self.assertEqual(report["source_records"], 4)
                self.assertEqual(sum(report[key] for key in ("geofence_breach_reports", "failsafe_reports",
                    "commanded_changes", "recovery_reports", "source_declared_unknown", "unmapped_codes",
                    "other_reported_reasons", "unqualified_vehicle_records")), 4)
                common = self.common(text)
                self.assertEqual(common["main_rows"], 4)
                self.assertEqual(common["platform_domain"], "Unknown")
                for row, observation, original in zip(rows, common["observations"], source.splitlines()[1:]):
                    fields = observation["fields"]
                    self.assertEqual(fields["producer_pin"], PIN)
                    self.assertEqual(fields["reported_mode_reason"], row["reported_mode_reason"])
                    self.assertEqual(observation["clock_basis"], "BootRelative")
                    self.assertEqual(fields["record_time_us"], 1000000)
                    self.assertEqual(bytes.fromhex(fields["original_record_hex"][4:]).decode(), original)
                    self.assertEqual(fields["original_record_columns"], 5)
                if codes[0] == 10:
                    self.assertEqual((report["geofence_breach_reports"], report["failsafe_reports"],
                                      report["recovery_reports"], report["commanded_changes"]), (1, 1, 1, 1))
                    self.assertEqual(rows[2]["safety_domain"], "FAILSAFE")
                    self.assertEqual(rows[2]["reason_class"], "RECOVERY_OR_RETURN_REPORTED")
                    self.assertEqual(rows[2]["reported_failsafe_kind"], "")
                    self.assertEqual(rows[3]["safety_domain"], "")
                else:
                    self.assertEqual(rows[0]["reported_failsafe_kind"], "UNSPECIFIED_GENERAL")
                    self.assertEqual(rows[2]["safety_domain"], "")

    def test_plane_exact_bytes_and_report_are_unchanged(self):
        text, report = convert(GOLDEN, vehicle="plane-4.5.5", source_format="ardupilot-mode-csv")
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                         "cb6b8df3b0155dcc4a2e5e5db887b00d945ee77f445aa36d98de9de801d6f592")
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(canonical.encode()).hexdigest(),
                         "ac17bfa8366a40a1b91b1b0df3e14580f9c30ccd9d69c8b562dc23436c5c375d")

    def test_wrong_version_and_malformed_selected_records_remain_unqualified_or_refused(self):
        for vehicle in ("copter-4.5.4", "copter-4.4.0", "rover-4.5.5", "plane"):
            text, report = convert(saved([4]), vehicle=vehicle, source_format="ardupilot-mode-csv")
            row = next(csv.DictReader(io.StringIO(text)))
            self.assertEqual(row["reason_class"], "UNQUALIFIED_VEHICLE_VERSION_NOT_INTERPRETED")
            self.assertEqual(report["unqualified_vehicle_records"], 1)
            self.assertEqual(row["safety_domain"], "")
            self.assertNotEqual(row["producer_pin"], PIN)
        for text in (saved([256]), saved([-1]), saved(["True"]), saved([4]).replace("Rsn", "missing"),
                     saved([4]).replace('"original, note"', '"unterminated'),
                     saved([4]).replace("Rsn,extra", "Rsn,Rsn")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                convert(text, vehicle="copter-4.5.5", source_format="ardupilot-mode-csv")
