"""Authored CRSF link rows and plainly chosen example levels.

No real recording, no radio, no device and no provider sample appears here, and no level below
is a safe or certified level for any link: they are example numbers picked to exercise the
comparison. The evaluation itself is the adopted declared-limit bridge, imported unchanged.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_link_level_events import LINK_DOMAIN, convert
from scripts.convert_electrical_events import ELECTRICAL_DOMAIN
from scripts.convert_crsf_link import convert as convert_crsf_capture
from tests.test_crsf_link import capture, frame

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/link-level-events/profile.toml"

COLUMNS = ["record_time_us", "uplink_lq_fraction", "uplink_rssi_1_dbm", "uplink_rssi_2_dbm",
           "downlink_rssi_dbm", "destination_address"]


def observations(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in COLUMNS})
    return output.getvalue().encode()


def sample(time_us, lq=None, rssi=None, rssi2=None, destination="200"):
    """One row in the adopted CRSF link converter's own shape.

    `destination_address` is the frame's CRSF **destination** byte, which the adopted case
    declares is "not_authenticated_source_identity". It is used below only as a saved column a
    caller may group by, never as a transmitter identity.
    """
    row = {"record_time_us": time_us, "destination_address": destination}
    if lq is not None:
        row["uplink_lq_fraction"] = lq
    if rssi is not None:
        row["uplink_rssi_1_dbm"] = rssi
    if rssi2 is not None:
        row["uplink_rssi_2_dbm"] = rssi2
    return row


def levels(*entries, **overrides):
    document = {"declared_by": "an authored operator statement",
                "declared_basis": "an example level chosen to exercise the comparison",
                "not_a_certified_safe_limit": True, "limits": list(entries)}
    document.update(overrides)
    return json.dumps(document).encode()


def quality(**overrides):
    entry = {"condition": "link_quality_below_declared_level", "unit": "fraction",
             "comparison": "at_or_below", "limit": 0.5, "min_samples": 3}
    entry.update(overrides)
    return entry


def signal(**overrides):
    entry = {"condition": "link_signal_below_declared_level", "unit": "dBm",
             "comparison": "below", "limit": -90.0, "min_samples": 2}
    entry.update(overrides)
    return entry


class LinkLevelTests(unittest.TestCase):
    def evaluate(self, observed, declared, *instance_columns, clock_basis="unknown"):
        output, report = convert(observed, declared, clock_basis=clock_basis,
                                 instance_column=list(instance_columns) or None)
        return list(csv.DictReader(io.StringIO(output))), report, output

    def only(self, *args, **kwargs):
        results, report, output = self.evaluate(*args, **kwargs)
        self.assertEqual(len(results), 1, output)
        return results[0]

    def test_the_declared_level_is_crossed_sustained_or_not_reached(self):
        crossed = self.only(observations([sample(1, lq="0.9"), sample(2, lq="0.4"),
                                          sample(3, lq="0.3"), sample(4, lq="0.2")]),
                            levels(quality()))
        self.assertEqual(crossed["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual((crossed["run_sample_count"], crossed["crossing_sample_count"]), ("3", "3"))
        self.assertEqual(crossed["run_extreme_value"], "0.2")
        self.assertEqual((crossed["run_start_time_us"], crossed["run_end_time_us"]), ("2", "4"))
        above = self.only(observations([sample(1, lq="0.9"), sample(2, lq="0.8"),
                                        sample(3, lq="0.7")]), levels(quality()))
        self.assertEqual(above["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual((above["crossing_sample_count"], above["run_sample_count"]), ("0", ""))
        brief = self.only(observations([sample(1, lq="0.9"), sample(2, lq="0.4"),
                                        sample(3, lq="0.9"), sample(4, lq="0.4")]),
                          levels(quality()))
        self.assertEqual(brief["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual((brief["crossing_sample_count"], brief["run_sample_count"]), ("2", "1"))
        exact = self.only(observations([sample(1, lq="0.5")] * 1 + [sample(2, lq="0.5"),
                                                                    sample(3, lq="0.5")]),
                          levels(quality()))
        self.assertEqual(exact["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        strict = self.only(observations([sample(1, lq="0.5"), sample(2, lq="0.5"),
                                         sample(3, lq="0.5")]),
                           levels(quality(comparison="below")))
        self.assertEqual(strict["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")

    def test_a_declared_release_level_holds_one_run_open_and_never_revives_a_broken_one(self):
        held = self.only(observations([sample(1, lq="0.9"), sample(2, lq="0.4"),
                                       sample(3, lq="0.55"), sample(4, lq="0.45")]),
                         levels(quality(release_limit=0.7)))
        self.assertEqual(held["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual((held["run_sample_count"], held["crossing_sample_count"]), ("3", "2"))
        self.assertEqual(held["declared_release_limit"], "0.7")
        released = self.only(observations([sample(1, lq="0.4"), sample(2, lq="0.8"),
                                           sample(3, lq="0.4"), sample(4, lq="0.45")]),
                             levels(quality(release_limit=0.7)))
        self.assertEqual(released["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual(released["run_sample_count"], "2")

    def test_a_gap_wider_than_declared_breaks_the_run_rather_than_spanning_it(self):
        rows = [sample(1, lq="0.4"), sample(2, lq="0.4"), sample(1_000_002, lq="0.4"),
                sample(1_000_003, lq="0.4")]
        broken = self.only(observations(rows), levels(quality(max_gap_us=10)))
        self.assertEqual(broken["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual((broken["crossing_sample_count"], broken["run_sample_count"]), ("4", "2"))
        self.assertEqual(broken["declared_max_gap_us"], "10")
        joined = self.only(observations(rows), levels(quality(max_gap_us=2_000_000)))
        self.assertEqual(joined["run_sample_count"], "4")

    def test_a_missing_reading_is_a_hole_and_never_a_zero(self):
        rows = [sample(1, lq="0.4"), sample(2, lq="0.4"), sample(3), sample(4, lq="0.4"),
                sample(5, lq="0.4")]
        held = self.only(observations(rows), levels(quality()))
        self.assertEqual(held["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual((held["crossing_sample_count"], held["run_sample_count"]), ("4", "2"))
        self.assertEqual((held["evaluated_sample_count"], held["usable_sample_count"],
                          held["unusable_sample_count"]), ("5", "4", "1"))
        sentinel = self.only(observations([sample(1, lq="-1"), sample(2, lq="2"),
                                           sample(3, lq="-1")]), levels(quality()))
        self.assertEqual(sentinel["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(sentinel["unknown_reason"], "NO_USABLE_SAMPLE_FOR_THE_DECLARED_QUANTITY")
        self.assertEqual((sentinel["usable_sample_count"], sentinel["crossing_sample_count"]),
                         ("0", ""))
        absent = self.only(observations([sample(1, lq="0.4", destination="200")]),
                           levels(quality(instance_value="201")), "destination_address")
        self.assertEqual(absent["unknown_reason"], "REQUESTED_INSTANCE_NOT_PRESENT_IN_INPUT")

    def test_an_unusable_declaration_is_refused_rather_than_repaired(self):
        observed = observations([sample(1, lq="0.4"), sample(2, lq="0.4"), sample(3, lq="0.4")])
        for broken in (
                quality(limit=1.5),                     # a fraction level outside 0..1
                quality(limit=-0.1),
                quality(unit="percent"),                # not the column's declared unit
                quality(unit="dBm"),                    # the other quantity's unit
                quality(comparison="above"),            # direction contradicts the condition
                quality(comparison="at_or_above"),
                quality(release_limit=0.2),             # release on the wrong side of the level
                quality(release_limit=1.4),             # release outside a fraction's range
                quality(min_duration_us=1000),          # a duration claim without max_gap_us
                dict(quality(), min_samples=3, min_duration_us=1000),   # two windows
                {"condition": "link_quality_below_declared_level", "unit": "fraction",
                 "comparison": "at_or_below", "limit": 0.5},            # no window at all
                quality(sign_handling="magnitude"),     # belongs to a current limit
                {"condition": "low_charge", "unit": "fraction", "comparison": "below",
                 "limit": 0.25, "min_samples": 3},      # an electrical condition, here
        ):
            with self.assertRaises(ValueError, msg=broken):
                convert(observed, levels(broken), clock_basis="unknown", instance_column=None)
        with self.assertRaises(ValueError):
            convert(observed, levels(quality(), not_a_certified_safe_limit=False),
                    clock_basis="unknown", instance_column=None)

    def test_the_engines_identity_and_clock_requirements_still_hold_here(self):
        duration = quality(min_samples=None, min_duration_us=2, max_gap_us=10)
        duration.pop("min_samples")
        unknown_clock = self.only(observations([sample(1, lq="0.4"), sample(2, lq="0.4"),
                                                sample(3, lq="0.4")]), levels(duration))
        self.assertEqual(unknown_clock["unknown_reason"],
                         "DURATION_REQUIRES_A_TIMEBASE_SOURCE_CLOCK_UNKNOWN")
        repeated = self.only(observations([sample(1, lq="0.4"), sample(1, lq="0.4"),
                                           sample(1, lq="0.4")]), levels(duration),
                             clock_basis="host_received")
        self.assertEqual(repeated["unknown_reason"],
                         "DURATION_REQUIRES_AN_ORDERED_CLOCK_AMBIGUOUS_DUPLICATE_TIMESTAMPS")
        two = observations([sample(1, lq="0.4", destination="200"),
                            sample(2, lq="0.4", destination="201"),
                            sample(3, lq="0.4", destination="200"),
                            sample(4, lq="0.4", destination="200")])
        results, report, _ = self.evaluate(two, levels(quality()), "destination_address")
        self.assertEqual([row["instance_value"] for row in results], ["200", "201"])
        self.assertEqual([row["condition_status"] for row in results],
                         ["DECLARED_LIMIT_CROSSED_AND_WINDOW_MET", "CROSSED_LIMIT_NOT_SUSTAINED"])
        self.assertEqual(report["instances"], ["200", "201"])
        with self.assertRaises(ValueError):
            convert(two, levels(quality(), source_identity=["destination_address",
                                                            "declared_source_id"]),
                    clock_basis="unknown", instance_column=["destination_address"])
        with self.assertRaises(ValueError):
            convert(two, levels(quality(), source_identity=["declared_source_id"]),
                    clock_basis="unknown", instance_column=["declared_source_id"])

    def test_the_dbm_quantity_is_the_same_comparison_under_its_own_declared_unit(self):
        rows = [sample(1, rssi="-70", rssi2="-95"), sample(2, rssi="-95", rssi2="-70"),
                sample(3, rssi="-99", rssi2="-70")]
        first = self.only(observations(rows), levels(signal()))
        self.assertEqual(first["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual((first["quantity"], first["quantity_column"], first["declared_unit"]),
                         ("link_signal_dbm", "uplink_rssi_1_dbm", "dBm"))
        self.assertEqual(first["run_extreme_value"], "-99.0")
        second = self.only(observations(rows), levels(signal(column="uplink_rssi_2_dbm")))
        self.assertEqual(second["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual((second["quantity_column"], second["crossing_sample_count"],
                          second["run_sample_count"]), ("uplink_rssi_2_dbm", "1", "1"))
        with self.assertRaises(ValueError):
            convert(observations(rows), levels(signal(unit="fraction")),
                    clock_basis="unknown", instance_column=None)
        both, _, _ = self.evaluate(observations([sample(1, lq="0.4", rssi="-95"),
                                                 sample(2, lq="0.4", rssi="-95"),
                                                 sample(3, lq="0.4", rssi="-95")]),
                                   levels(quality(), signal()))
        self.assertEqual([row["quantity"] for row in both],
                         ["link_quality_fraction", "link_signal_dbm"])
        self.assertEqual({row["condition_status"] for row in both},
                         {"DECLARED_LIMIT_CROSSED_AND_WINDOW_MET"})

    def test_the_link_domain_is_closed_and_the_electrical_one_is_untouched(self):
        self.assertEqual(set(ELECTRICAL_DOMAIN["conditions"]),
                         {"voltage_sag", "overcurrent", "thermal_over_limit", "low_charge"})
        self.assertEqual(set(LINK_DOMAIN["conditions"]),
                         {"link_quality_below_declared_level", "link_signal_below_declared_level"})
        self.assertFalse(set(LINK_DOMAIN["conditions"]) & set(ELECTRICAL_DOMAIN["conditions"]))
        self.assertNotEqual(LINK_DOMAIN["interpretation"], ELECTRICAL_DOMAIN["interpretation"])
        for refused in ("never_a_radio_loss_fault_cause", "interference_obstruction_range",
                        "antenna_or_outage_finding"):
            self.assertIn(refused, LINK_DOMAIN["interpretation"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_authored_crsf_frames_reach_declared_level_rows_through_the_adopted_converter(self):
        """The advertised composition, run end to end, with nothing hand-shaped in between.

        Authored 0x14 frames -> the adopted `convert_crsf_link` -> this wrapper -> the existing
        common reader. The rows above author the post-converter column shape directly; this one
        does not, so it is what proves the chain actually joins up.
        """
        def link(rssi, lq, downlink_rssi):
            return frame(bytes([rssi, 80, lq, 251, 1, 250, 250, downlink_rssi, 50, 3]))
        converted, crsf_report = convert_crsf_capture(capture([
            (1, link(70, 99, 90)), (2, link(95, 40, 95)),
            (3, link(96, 30, 80)), (4, link(97, 20, 80))]))
        self.assertEqual((crsf_report["source_records"], crsf_report["decoded_records"]), (4, 4))
        declared = levels(quality(), signal(),
                          signal(column="downlink_rssi_dbm", min_samples=4))
        results, report, output = self.evaluate(converted.encode(), declared,
                                                "destination_address")
        self.assertEqual([row["quantity_column"] for row in results],
                         ["uplink_lq_fraction", "uplink_rssi_1_dbm", "downlink_rssi_dbm"])
        self.assertEqual([row["condition_status"] for row in results],
                         ["DECLARED_LIMIT_CROSSED_AND_WINDOW_MET",
                          "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET",
                          "CROSSED_LIMIT_NOT_SUSTAINED"])
        self.assertEqual([row["run_extreme_value"] for row in results], ["0.2", "-97.0", "-95.0"])
        self.assertEqual([row["run_sample_count"] for row in results], ["3", "3", "1"])
        self.assertEqual(report["instances"], ["200"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                     str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (3, "Unknown"))
        rows = [observation["fields"] for observation in common["observations"]]
        self.assertEqual([row["declared_unit"] for row in rows], ["fraction", "dBm", "dBm"])
        self.assertEqual([row["declared_limit"] for row in rows], [0.5, -90.0, -90.0])
        for row in rows:
            self.assertEqual(row["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")
            self.assertEqual(row["interpretation_basis"], LINK_DOMAIN["interpretation"])

        directional, _ = convert_crsf_capture(capture(
            [(1, frame(bytes([95, 20, 40, 251, 10]), kind=0x1C)),
             (2, frame(bytes([96, 20, 30, 251, 10]), kind=0x1C)),
             (3, frame(bytes([97, 20, 20, 251, 10]), kind=0x1C))]), kind="link-rx")
        missing = self.only(directional.encode(), levels(quality()))
        self.assertEqual(missing["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(missing["unknown_reason"], "NO_USABLE_SAMPLE_FOR_THE_DECLARED_QUANTITY")
        self.assertEqual((missing["usable_sample_count"], missing["evaluated_sample_count"]),
                         ("0", "3"))
        named = self.only(directional.encode(), levels(quality(column="reported_lq_fraction")))
        self.assertEqual(named["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual((named["quantity_column"], named["declared_unit"]),
                         ("reported_lq_fraction", "fraction"))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_the_emitted_rows_reach_common_observations_through_the_existing_reader(self):
        observed = observations([sample(1, lq="0.9", rssi="-70"), sample(2, lq="0.4", rssi="-95"),
                                 sample(3, lq="0.3", rssi="-96"), sample(4, lq="0.2", rssi="-97")])
        _, report, output = self.evaluate(observed, levels(quality(), signal()),
                                          "destination_address")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                     str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (2, "Unknown"))
        rows = [observation["fields"] for observation in common["observations"]]
        self.assertEqual([row["condition"] for row in rows],
                         ["link_quality_below_declared_level", "link_signal_below_declared_level"])
        self.assertEqual([row["quantity_column"] for row in rows],
                         ["uplink_lq_fraction", "uplink_rssi_1_dbm"])
        self.assertEqual([row["declared_unit"] for row in rows], ["fraction", "dBm"])
        self.assertEqual([row["declared_limit"] for row in rows], [0.5, -90.0])
        for row in rows:
            self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(row["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")
            self.assertEqual(row["interpretation_basis"], LINK_DOMAIN["interpretation"])
            self.assertEqual(row["instance_value"], 200)
            self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]), (2, 4))
            self.assertIsNone(row["transition_evidence"])
        for observation in common["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"])
        units = common["profile_units"]
        for name in ("condition", "quantity", "quantity_column", "declared_unit", "declared_limit",
                     "condition_status", "interpretation_basis", "threshold_authority",
                     "run_end_time_us", "unusable_sample_count"):
            self.assertTrue(units.get(name), name)
        self.assertIn("never_an_inferred_safety_threshold", units["declared_limit"])
        self.assertIn("never_a_radio_loss", units["interpretation_basis"])
        self.assertEqual(report["interpretation_basis"], LINK_DOMAIN["interpretation"])
        self.assertEqual(report["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")


if __name__ == "__main__":
    unittest.main()
