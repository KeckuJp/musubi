"""Authored rows only. No radio, no device, no live link, no capture, no real log.

The chain proved here is the advertised one: authored CRSF 0x14 frames through the **adopted** link
converter, through this bridge, and through the **real** common reader -- not a hand-shaped
intermediate CSV. What the adopted cause evaluation then does with the result is asserted separately
in `crates/predemo-readers/tests/link_cue_join.rs`, against the real module and the real catalog.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_link_cue_fields import TARGET_UNITS, convert
from scripts.convert_crsf_link import convert as crsf_convert
from tests.test_crsf_link import capture, frame

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/link-cue-fields"
PROFILE = CASE / "profile.toml"
BOOT_PROFILE = CASE / "profile-boot-relative.toml"
QUALITY = "RQly(%)=uplink_lq_fraction:fraction"
SIGNAL = "1RSS(dB)=uplink_rssi_1_dbm:dBm"


def saved(*rows):
    header = "record_time_us,uplink_lq_fraction,uplink_rssi_1_dbm"
    return (header + "\n" + "".join(",".join(str(c) for c in r) + "\n" for r in rows)).encode()


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class LinkCueFields(unittest.TestCase):
    def rows(self, data, **kwargs):
        output, report = convert(data, **dict(dict(mappings=(QUALITY, SIGNAL)), **kwargs))
        return list(csv.DictReader(io.StringIO(output))), report, output

    def test_the_declared_fraction_is_carried_as_the_percent_that_name_is_read_in(self):
        rows, report, _ = self.rows(saved((1_000_000, "0.98", "-70"), (2_000_000, "0.4", "-104")))
        self.assertEqual([r["RQly(%)"] for r in rows], ["98", "40"])
        self.assertEqual([r["1RSS(dB)"] for r in rows], ["-70", "-104"])
        self.assertEqual(report["readings_carried"], {"RQly(%)": 2, "1RSS(dB)": 2})
        self.assertEqual(json.loads(unhex(rows[0]["source_mapping_hex"])), [
            ["RQly(%)", "uplink_lq_fraction", "fraction",
             "FRACTION_TO_PERCENT_BY_DECLARED_SCALE_OF_100"],
            ["1RSS(dB)", "uplink_rssi_1_dbm", "dBm",
             "CARRIED_UNCHANGED_THE_DECLARED_SOURCE_UNIT_IS_THE_UNIT_THIS_NAME_IS_READ_IN"]])
        for row in rows:
            self.assertIn("STATES_NO_CAUSE_NO_CANDIDATE_NO_LABEL", row["measurement_basis"])
            self.assertIn("NEVER_EVIDENCE_OF_INTERFERENCE_OBSTRUCTION_RANGE_OR_AN_ANTENNA_FAULT",
                          row["measurement_basis"].replace("_IS_", "_"))

    def test_what_this_export_cannot_supply_is_named_rather_than_left_empty(self):
        rows, report, _ = self.rows(saved((1_000_000, "0.98", "-70")))
        self.assertEqual(rows[0]["mapped_cue_inputs"], "RQly(%)+1RSS(dB)")
        self.assertEqual(rows[0]["cue_inputs_carried"], "RQly(%)+1RSS(dB)")
        self.assertEqual(rows[0]["cue_inputs_absent"],
                         "RADIO_STATUS.rssi+RADIO_STATUS.noise")
        self.assertEqual(rows[0]["undetermined_cue_tests"],
                         "REPORTED_RSSI_RATIO_TEST+REPORTED_NOISE_TEST")
        self.assertEqual(report["never_mapped_cue_inputs"],
                         ["RADIO_STATUS.rssi", "RADIO_STATUS.noise"])
        self.assertEqual(report["evaluation_performed"],
                         "NONE_THIS_BRIDGE_EVALUATES_NOTHING_AND_NAMES_NO_CAUSE")

    def test_a_blank_row_is_not_a_carried_observation(self):
        """Availability is decided per row, not once from the header."""
        rows, report, _ = self.rows(saved((1_000_000, "0.98", "-70"),
                                          (2_000_000, "", "-70"),
                                          (3_000_000, "", "")))
        self.assertTrue(all(r["mapped_cue_inputs"] == "RQly(%)+1RSS(dB)" for r in rows))
        self.assertEqual([r["cue_inputs_carried"] for r in rows],
                         ["RQly(%)+1RSS(dB)", "1RSS(dB)", ""])
        self.assertEqual(rows[1]["cue_inputs_absent"],
                         "RQly(%)+RADIO_STATUS.rssi+RADIO_STATUS.noise")
        self.assertIn("REPORTED_LINK_QUALITY_TEST", rows[1]["undetermined_cue_tests"])
        self.assertEqual(rows[2]["cue_inputs_absent"],
                         "RQly(%)+1RSS(dB)+RADIO_STATUS.rssi+RADIO_STATUS.noise")
        self.assertEqual(report["rows_with_no_qualified_reading"], 1)
        self.assertEqual(report["readings_unavailable"], {"RQly(%)": 2, "1RSS(dB)": 1})

    def test_a_value_that_underflows_or_overflows_is_refused_not_rounded(self):
        """`float()` alone would make a nonzero reading a zero, and a zero can cross a test."""
        for raw, why in (("1e-999", "a nonzero value that underflows to zero"),
                         ("-1e-999", "a negative nonzero value that underflows"),
                         ("1e999", "a value that overflows to infinity"),
                         ("nan", "not a number at all")):
            with self.assertRaises(ValueError, msg=why):
                convert(saved((1_000_000, raw, "-70")), mappings=(QUALITY,))
        with self.assertRaises(ValueError):
            convert(saved((1_000_000, "0.9", "1e-999")), mappings=(SIGNAL,))
        rows, _, _ = self.rows(saved((1_000_000, "0.0", "-70")))
        self.assertEqual(rows[0]["RQly(%)"], "0")
        self.assertIn("RQly(%)", rows[0]["cue_inputs_carried"])

    def test_a_percent_outside_its_own_range_never_reaches_a_cue_value(self):
        """-1 would be below the adopted "< 90" test and inject a false degradation cue."""
        rows, report, _ = self.rows(saved((1_000_000, "-1", "-70"),
                                          (2_000_000, "300", "-70"),
                                          (3_000_000, "55", "-70")),
                                    mappings=("RQly(%)=uplink_lq_fraction:percent", SIGNAL))
        self.assertEqual([r["RQly(%)"] for r in rows], ["", "", "55"])
        self.assertEqual([("RQly(%)" in r["cue_inputs_carried"]) for r in rows],
                         [False, False, True])
        self.assertEqual(report["readings_unavailable"]["RQly(%)"], 2)
        wide, _, _ = self.rows(saved((1_000_000, "0.9", "-140"), (2_000_000, "0.9", "20")))
        self.assertEqual([r["1RSS(dB)"] for r in wide], ["-140", "20"])

    def test_the_pinned_radio_status_missing_code_stays_missing(self):
        """mavlink common.xml 3203f89c: uint8, values [0-254], UINT8_MAX invalid/unknown."""
        data = b"record_time_us,rs\n1000000,200\n2000000,255\n3000000,254\n"
        rows, report, _ = self.rows(data,
                                    mappings=("RADIO_STATUS.rssi=rs:device_specific_scale",))
        self.assertEqual([r["RADIO_STATUS.rssi"] for r in rows], ["200", "", "254"])
        self.assertEqual(rows[1]["cue_inputs_carried"], "")
        self.assertIn("REPORTED_RSSI_RATIO_TEST", rows[1]["undetermined_cue_tests"])
        self.assertEqual(report["readings_unavailable"]["RADIO_STATUS.rssi"], 1)
        for raw, why in ((b"256", "above the documented uint8 field"),
                         (b"-1", "below the documented uint8 field"),
                         (b"200.5", "not an integer in a uint8 field")):
            with self.assertRaises(ValueError, msg=why):
                convert(b"record_time_us,rs\n1000000," + raw + b"\n",
                        mappings=("RADIO_STATUS.rssi=rs:device_specific_scale",))

    def test_an_unqualified_clock_is_disclosed_as_supplying_no_windowed_input(self):
        rows, _, _ = self.rows(saved((1_000_000, "0.4", "-104")))
        self.assertEqual(rows[0]["declared_clock_basis"], "unknown")
        self.assertIn("READS_NOTHING_FROM_THESE_ROWS_AT_ALL", rows[0]["windowed_evaluation_input"])
        self.assertIn("NEVER_A_HEALTHY_LINK", rows[0]["windowed_evaluation_input"])
        boot, _, _ = self.rows(saved((1_000_000, "0.4", "-104")), clock_basis="boot_relative")
        self.assertIn("ONLY_WHEN_THE_CALLER_ALSO_SUPPLIES_AN_OFFSET_ESTIMATE",
                      boot[0]["windowed_evaluation_input"])
        self.assertIn("NEVER_A_SUBSTITUTE_FOR_THAT_OFFSET", boot[0]["windowed_evaluation_input"])

    def test_an_unreported_or_impossible_reading_is_a_hole_and_never_a_value(self):
        rows, report, _ = self.rows(saved((1_000_000, "", "-70"),
                                          (2_000_000, "7", "-70")))
        self.assertEqual([r["RQly(%)"] for r in rows], ["", ""])
        self.assertEqual(report["readings_unavailable"]["RQly(%)"], 2)
        self.assertEqual(report["readings_carried"]["RQly(%)"], 0)
        self.assertTrue(all("RQly(%)" not in r["cue_inputs_carried"] for r in rows))

    def test_a_wrong_or_missing_declaration_is_refused(self):
        good = saved((1_000_000, "0.98", "-70"))
        for kwargs, why in (
                ({"mappings": ()}, "no mapping at all; nothing is guessed"),
                ({"mappings": ("1RSS(dB)=uplink_lq_fraction:fraction",)},
                 "a fraction may not be carried into a dBm name"),
                ({"mappings": ("RADIO_STATUS.noise=uplink_rssi_1_dbm:dBm",)},
                 "a dBm source may not be carried into a device-specific scale"),
                ({"mappings": ("uplink_lq_fraction=uplink_lq_fraction:fraction",)},
                 "a target the cause evaluation does not read"),
                ({"mappings": ("RQly(%)=nope:fraction",)}, "a source column not in the header"),
                ({"mappings": ("RQly(%)=uplink_lq_fraction",)}, "a mapping with no declared unit"),
                ({"mappings": (QUALITY, QUALITY)}, "one target declared twice"),
                ({"clock_basis": "gps_locked"}, "a clock basis this format does not accept"),
                ({"time_column": "nope"}, "a time column not in the header")):
            with self.assertRaises(ValueError, msg=why):
                convert(good, **dict(dict(mappings=(QUALITY, SIGNAL)), **kwargs))
        for broken, why in (
                (saved((2_000_000, "0.9", "-70"), (1_000_000, "0.9", "-70")),
                 "a saved time that decreases"),
                (b"record_time_us,uplink_lq_fraction\n", "a file with no rows"),
                (saved((1_000_000, "banana", "-70")), "a value that is not a number"),
                (saved((1_000_000, "0.9", "inf")), "a value that is not finite"),
                (saved((1_000_000, "0.9", "-inf")), "a negative infinity")):
            with self.assertRaises(ValueError, msg=why):
                convert(broken, mappings=(QUALITY, SIGNAL))

    def test_the_unit_check_stops_a_conversion_and_never_reads_the_callers_mind(self):
        """The limit, stated rather than implied, exactly as the adopted link bridge states it."""
        rows, _, _ = self.rows(saved((1_000_000, "0.98", "-70")),
                               mappings=("RQly(%)=uplink_lq_fraction:percent",))
        self.assertEqual(rows[0]["RQly(%)"], "0.98")
        self.assertEqual(json.loads(unhex(rows[0]["source_mapping_hex"]))[0][2], "percent")
        with self.assertRaises(ValueError):
            convert(saved((1_000_000, "0.98", "-70")),
                    mappings=("1RSS(dB)=uplink_lq_fraction:fraction",))

    def test_authored_crsf_frames_reach_the_common_reader_through_the_adopted_converter(self):
        """The advertised chain, run as advertised: adopted converter, bridge, real reader."""
        link = crsf_convert(capture([(1_000_000, frame()), (2_000_000, frame())]), kind="link")[0]
        self.assertIn("uplink_lq_fraction", link.splitlines()[0])
        output, report = convert(link.encode(), mappings=(QUALITY, SIGNAL))
        self.assertEqual(report["rows"], 2)
        self.assertNotIn('"', output)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        for profile in (PROFILE, BOOT_PROFILE):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "observations.csv"
                path.write_text(output, encoding="utf-8")
                common = json.loads(subprocess.run(
                    [reader, str(profile), str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                observation = common["observations"][0]
                self.assertEqual(observation["channel"], "link_stats")
                for name in TARGET_UNITS:
                    self.assertIn(name, observation["fields"])

    def test_the_command_line_writes_the_same_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "link.csv", base / "out"
            source.write_bytes(saved((1_000_000, "0.98", "-70")))
            subprocess.run([sys.executable, "-m", "scripts.convert_link_cue_fields",
                            str(source), str(out), "--map", QUALITY, "--map", SIGNAL,
                            "--source-clock-basis", "boot_relative"],
                           cwd=ROOT, check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["declared_mappings"], 2)
            self.assertEqual(report["declared_clock_basis"], "boot_relative")
            rows = list(csv.DictReader(io.StringIO((out / "observations.csv").read_text())))
            self.assertEqual(rows[0]["RQly(%)"], "98")


if __name__ == "__main__":
    unittest.main()
