"""A Rover BAT export must not report the Air domain.

Authored input only, in the pinned 12-field `Inst` layout this case records for ArduRover
4.5.0-4.5.5 (`docs/saved-input-comparisons.md`). No vehicle, no
device, no real battery record: **real records 0**. The existing zero-record Rover BAT archive
stays zero and is not a source here.

The original BAT cases check only what the profile selection declares: the same converted bytes
through three profiles that differ *solely* in `family`. The BCL2 route case additionally checks
the documented profile choices with the unchanged converter; general conversion and refusal
coverage remains in `tests/test_ardupilot_battery_conversion.py`.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/ardupilot-battery"
SCRIPT = ROOT / "scripts/convert_ardupilot_battery_csv.py"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# The declaration this case records at the six ArduRover 4.5 release commits:
# BAT "QBfffffcfBBB" TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH.
# `Future` is an unknown source field, kept so its retention is visible in common output.
ROVER_BAT = (
    "TimeUS,Inst,Volt,Curr,CurrTot,EnrgTot,RemPct,VoltR,Temp,Res,Future,H,SH\n"
    "2500,0,25.9,-3.5,1480,2.75,68,26.1,21.5,0.018,retained,1,92\n"
    "2500,1,25.4,-1.25,610,1.1,71,25.8,21.0,0.021,other,1,90\n")


def converted():
    """The Rover export through the adopted converter, exactly as the documented command runs it."""
    return module.convert(ROVER_BAT, "ardupilot-bat-inst", bat_details=True)


def read_with(case, profile_name, text):
    """Read the converted CSV through the actual common reader with one named profile."""
    configured = os.environ.get("MUSUBI_TELEMETRY_READER")
    if configured:
        case.assertTrue(Path(configured).exists(),
                        f"MUSUBI_TELEMETRY_READER is set to {configured!r}, which does not exist; "
                        "an explicitly configured reader that is missing is a failure, not a skip")
    elif not Path(READER).exists():
        case.skipTest("no reader configured and the default build is absent, so the domain proof "
                      "cannot run; this is an explicit skip and never a pass")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "bat.csv"
        path.write_text(text)
        finished = subprocess.run(
            [READER, str(CASE / profile_name), str(path), "--allow-equal-time",
             "--preserve-nonfinite-as-text"], capture_output=True)
    case.assertEqual(finished.returncode, 0,
                     f"{profile_name} failed: {finished.stderr[:400]!r}")
    return json.loads(finished.stdout)


class RoverBatteryDomainTests(unittest.TestCase):
    def test_bcl2_qualified_rover_route_keeps_cells_missing_and_declared_domain(self):
        """Authored BCL2 rows reuse the converter; provenance is caller-declared, not detected."""
        text = module.convert(
            "TimeUS,Instance,V13,V14,Future\n"
            "2500,0,0,1,retained\n2500,1,4301,65535,other\n",
            "ardupilot-bcl2-4.3")
        for profile, domain in (("rover-profile.toml", "Ground"),
                                ("unknown-profile.toml", "Unknown")):
            with self.subTest(profile=profile):
                common = read_with(self, profile, text)
                self.assertEqual(common["platform_domain"], domain)
                self.assertEqual(common["domain_source"], "profile_declared_family")
                self.assertEqual(common["main_rows"], 2)
                observations = common["observations"]
                self.assertEqual([o["t_boot_us"] for o in observations], [2500, 2500])
                self.assertTrue(all(o["clock_basis"] == "BootRelative" for o in observations))
                fields = [o["fields"] for o in observations]
                self.assertEqual([f["Instance"] for f in fields], [0, 1])
                self.assertEqual([f["Future"] for f in fields], ["retained", "other"])
                self.assertEqual([f["battery_cell_13_v"] for f in fields], [None, 4.3])
                self.assertEqual([f["battery_cell_14_v"] for f in fields], [0, 65.534])
                self.assertEqual([f["battery_cell_13_status"] for f in fields],
                                 ["NOT_PROVIDED", "REPORTED"])
                self.assertTrue(all(f["battery_cell_14_status"] == "REPORTED" for f in fields))
                for f in fields:
                    self.assertIsNone(f["battery_voltage_v"])
                    self.assertIsNone(f["battery_current_a"])
                self.assertEqual(common["profile_units"]["battery_cell_13_v"], "V")
                self.assertEqual(common["profile_units"]["battery_cell_14_v"], "V")
                self.assertEqual(common["profile_units"]["TimeUS"], "us_since_boot")
        # Keep the source-qualified BCL2 entry usable, not pointed at the Plane declaration.
        readme = (ROOT / "docs/saved-input-comparisons.md").read_text().split(
            "## Rover 4.5.0–4.5.5 BAT/BCL/BCL2 qualification", 1)[1]
        self.assertIn("rover-profile.toml", readme)
        self.assertIn("unknown-profile.toml", readme)
        self.assertNotIn("this case's `profile.toml`", readme)

    def test_a_rover_export_reaches_common_observation_as_ground_not_air(self):
        """The documented Rover route must not report Air; everything else is unchanged."""
        text = converted()
        common = read_with(self, "rover-profile.toml", text)
        self.assertEqual(common["platform_domain"], "Ground")
        self.assertNotEqual(common["platform_domain"], "Air")
        # Both instance rows survive at the same timestamp, as the adopted path already allowed.
        self.assertEqual(common["main_rows"], 2)
        self.assertTrue(all(o["clock_basis"] == "BootRelative" for o in common["observations"]))
        fields = common["observations"][0]["fields"]
        self.assertEqual(fields["battery_voltage_v"], 25.9)
        self.assertEqual(fields["battery_current_a"], -3.5)
        self.assertEqual(fields["battery_remaining_fraction"], 0.68)
        self.assertEqual(fields["TimeUS"], 2500)
        self.assertEqual(common["observations"][1]["fields"]["Inst"], 1)
        # The unknown source field is still retained, and the sign convention is still disclaimed.
        units = common["profile_units"]
        self.assertEqual(fields["Future"], "retained")
        self.assertIn("no_discharging_or_charging_direction_is_established",
                      units["battery_current_a"])
        self.assertEqual(units["TimeUS"], "us_since_boot")

    def test_the_plane_profile_is_untouched_and_its_reported_domain_is_unchanged(self):
        """Measured, not assumed: this reader reports Unknown for the fixed-wing family.

        `read_telemetry_csv` resolves the domain as `family == Ugv ? Ground : Unknown`; it does not
        call `Family::platform_domain()`, where `FixedWing` maps to Air. So on this documented route
        the Plane profile reports Unknown, and the Air mapping is latent rather than active. Pinned
        here so that if the reader is ever changed to use the shared table, the ground-vehicle
        consequence of pointing a Rover at this profile becomes visible instead of silent.
        """
        common = read_with(self, "profile.toml", converted())
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertEqual(common["domain_source"], "profile_declared_family")
        self.assertEqual(common["main_rows"], 2)
        # The declared family is still fixed_wing, which is why a ground export must not use it.
        self.assertIn('family = "fixed_wing"', (CASE / "profile.toml").read_text())

    def test_without_established_provenance_the_domain_is_unknown_not_guessed(self):
        """A battery record carries no platform, so the default declares none."""
        common = read_with(self, "unknown-profile.toml", converted())
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertEqual(common["main_rows"], 2)

    def test_the_domain_is_declared_by_the_profile_and_never_read_from_the_columns(self):
        """One byte-identical input, three profiles: only the declaration moves the domain."""
        text = converted()
        domains = {name: read_with(self, name, text)["platform_domain"]
                   for name in ("profile.toml", "rover-profile.toml", "unknown-profile.toml")}
        self.assertEqual(domains, {"profile.toml": "Unknown", "rover-profile.toml": "Ground",
                                   "unknown-profile.toml": "Unknown"})
        # Ground is reachable only by declaring it, and the same bytes decline it twice over.
        self.assertEqual([name for name, d in domains.items() if d == "Ground"],
                         ["rover-profile.toml"])
        # The three differ only in the declared family: same units, same time field, same clock.
        blocks = {}
        for name in domains:
            lines = (CASE / name).read_text().splitlines()
            blocks[name] = [line for line in lines[lines.index("[units]"):] if line]
        self.assertEqual(blocks["rover-profile.toml"], blocks["profile.toml"])
        self.assertEqual(blocks["unknown-profile.toml"], blocks["profile.toml"])
        for name in domains:
            declared = (CASE / name).read_text()
            self.assertIn('time = "TimeUS"', declared)
            self.assertIn('default_clock_basis = "boot_relative"', declared)


if __name__ == "__main__":
    unittest.main()
