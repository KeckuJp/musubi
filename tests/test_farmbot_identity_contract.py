"""Coordinator-authored boundary for reported FarmBot identity, not device identity."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_ardupilot_battery_csv import convert
from scripts.convert_software_identity_binding import convert as bind
from tests import test_farmbot_state as fixture

ROOT = Path(__file__).resolve().parents[1]


class FarmbotIdentityContract(unittest.TestCase):
    def test_same_version_different_commit_and_absent_vs_reported_empty(self):
        source = [fixture.record({"controller_version": "v1", "controller_commit": pin,
                                  "controller_uuid": "reported-only", "busy": False}, 10)
                  for pin in ("a", "b", None, "")]
        produced = convert("\n".join(json.dumps(value) for value in source), "farmbot-state")
        records = list(csv.DictReader(io.StringIO(produced)))
        paired, report = bind(produced.encode(), paired_identities=(
            "SOURCE_SOFTWARE=VERSION:controller_version_hex,PIN:controller_commit_hex",))
        pairs = list(csv.DictReader(io.StringIO(paired)))
        self.assertEqual(len(pairs), 4)
        for pair in pairs:
            selection = json.loads(bytes.fromhex(pair["identity_join_hex"][4:]))
            selected = [row for row in records if all(row[key] == value for key, value in selection.items())]
            self.assertEqual(len(selected), 1)
            self.assertEqual(pair["identity_rows_carrying_value"], "1")
            self.assertNotIn("controller_uuid_hex", selection)
        self.assertEqual(report["pairs_with_a_missing_component"], 1)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.csv"
            path.write_text(produced)
            common = json.loads(subprocess.run([reader, str(ROOT /
                "profiles/declared/electrical-schema-reuse/farmbot-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 4)
        self.assertEqual(common["platform_domain"], "Unknown")
        for field in ("controller_uuid", "controller_version", "controller_commit",
                      "firmware_version", "firmware_commit"):
            self.assertTrue(common["profile_units"].get(field + "_hex"), field)
        self.assertTrue(all(row["anchor_unix_us"] is None for row in common["observations"]))
        self.assertEqual([row["fields"]["controller_commit_hex"] for row in common["observations"]],
                         ["hex:61", "hex:62", None, "hex:"])
