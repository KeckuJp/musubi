"""Authored normalized electrical rows and plainly chosen example limits.

No real recording, no device and no provider sample appears here, and no limit below is a
safe limit for any hardware: they are example numbers picked to exercise the comparison.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_electrical_events import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-events/profile.toml"

COLUMNS = ["record_time_us", "battery_voltage_v", "battery_current_a",
           "battery_current_sign_basis", "battery_temperature_k", "battery_temperature_state",
           "battery_id_reported"]


def observations(rows, columns=COLUMNS):
    """Rows in the shape the adopted electrical converter already emits."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in columns})
    return output.getvalue().encode()


def sample(time_us, volts=None, amps=None, kelvin=None, instance="1",
           sign="UNSPECIFIED", temperature_state=None):
    row = {"record_time_us": time_us, "battery_id_reported": instance}
    if volts is not None:
        row["battery_voltage_v"] = volts
    if amps is not None:
        row["battery_current_a"] = amps
        row["battery_current_sign_basis"] = sign
    if kelvin is not None:
        row["battery_temperature_k"] = kelvin
    row["battery_temperature_state"] = (
        temperature_state if temperature_state is not None
        else ("REPORTED" if kelvin is not None else "ZERO_OR_UNAVAILABLE"))
    return row


def limits(*entries, declared_by="authored test operator"):
    return json.dumps({
        "declared_by": declared_by,
        "declared_basis": "example numbers chosen for this test, not a safe limit",
        "not_a_certified_safe_limit": True,
        "limits": list(entries),
    }).encode()


def sag(**overrides):
    """A plainly chosen example: below 22.0 V, sustained. Not a real battery limit."""
    entry = {"condition": "voltage_sag", "unit": "V", "comparison": "below", "limit": 22.0,
             "min_duration_us": 400_000, "max_gap_us": 300_000}
    entry.update(overrides)
    return entry


def overcurrent(**overrides):
    entry = {"condition": "overcurrent", "unit": "A", "comparison": "above", "limit": 60.0,
             "min_samples": 3, "sign_handling": "magnitude"}
    entry.update(overrides)
    return entry


def thermal(**overrides):
    entry = {"condition": "thermal_over_limit", "unit": "K", "comparison": "at_or_above",
             "limit": 330.0, "min_samples": 2}
    entry.update(overrides)
    return entry


def run(rows, declared, clock_basis="boot_relative", instance_column="battery_id_reported"):
    output, report = convert(observations(rows), declared, clock_basis=clock_basis,
                             instance_column=instance_column)
    return list(csv.DictReader(io.StringIO(output))), report, output


def only(rows):
    assert len(rows) == 1, rows
    return rows[0]


class ElectricalEventTests(unittest.TestCase):
    def test_sustained_sag_is_reported_and_one_low_sample_is_not(self):
        sustained = [sample(0, volts=24.1), sample(100_000, volts=23.8),
                     sample(200_000, volts=21.5), sample(400_000, volts=21.2),
                     sample(600_000, volts=21.0), sample(700_000, volts=21.4),
                     sample(800_000, volts=23.9), sample(900_000, volts=24.0),
                     sample(1_000_000, volts=24.2), sample(1_100_000, volts=24.1)]
        row = only(run(sustained, limits(sag()))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["condition"], "voltage_sag")
        self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]), ("200000", "700000"))
        self.assertEqual(row["run_span_us"], "500000")
        self.assertEqual(row["run_sample_count"], "4")
        self.assertEqual(float(row["run_extreme_value"]), 21.0)
        self.assertEqual(row["record_time_us"], "0")  # the anchor, not the onset
        self.assertEqual(row["evaluation_anchor_basis"], "FIRST_SOURCE_ROW_TIME_NOT_EVENT_ONSET")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")
        self.assertEqual((row["quantity_column"], row["declared_unit"]), ("battery_voltage_v", "V"))
        self.assertEqual(row["duration_basis"], "SOURCE_AXIS_MICROSECONDS_ON_BOOT_RELATIVE")
        self.assertEqual(row["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")
        self.assertEqual(row["interpretation_basis"],
                         "observed_against_declared_limit_not_a_cause_fault_or_safe_limit")
        self.assertEqual(row["evaluated_sample_count"], "10")

        row = only(run(sustained, limits(sag(min_duration_us=900_000)))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual(row["crossing_sample_count"], "4")

        isolated = [sample(0, volts=24.1), sample(100_000, volts=12.0),
                    sample(200_000, volts=24.0), sample(300_000, volts=24.2)]
        row = only(run(isolated, limits(sag()))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual((row["run_sample_count"], row["run_span_us"]), ("1", "0"))
        self.assertEqual(float(row["run_extreme_value"]), 12.0)

        row = only(run([sample(n * 100_000, volts=24.0) for n in range(6)], limits(sag()))[0])
        self.assertEqual(row["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual((row["crossing_sample_count"], row["usable_sample_count"]), ("0", "6"))

    def test_overcurrent_uses_declared_sign_handling_and_window(self):
        discharging = [sample(0, amps=-20.0, sign="NEGATIVE_DISCHARGING"),
                       sample(100_000, amps=-72.5, sign="NEGATIVE_DISCHARGING"),
                       sample(200_000, amps=-80.0, sign="NEGATIVE_DISCHARGING"),
                       sample(300_000, amps=-65.0, sign="NEGATIVE_DISCHARGING"),
                       sample(400_000, amps=-18.0, sign="NEGATIVE_DISCHARGING")]
        row = only(run(discharging, limits(overcurrent()))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["run_sample_count"], "3")
        self.assertEqual(float(row["run_extreme_value"]), 80.0)
        self.assertEqual(row["observed_sign_basis"], "NEGATIVE_DISCHARGING")
        self.assertEqual(row["declared_sign_handling"], "magnitude")
        row = only(run(discharging, limits(overcurrent(sign_handling="signed_as_recorded")))[0])
        self.assertEqual(row["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        mixed = discharging[:2] + [sample(200_000, amps=80.0, sign="UNSPECIFIED")]
        row = only(run(mixed, limits(overcurrent()))[0])
        self.assertEqual(row["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(row["unknown_reason"], "INCONSISTENT_CURRENT_SIGN_BASIS")

    def test_a_temperature_reading_is_not_a_thermal_event(self):
        normal = [sample(n * 100_000, kelvin=300.0 + n) for n in range(5)]
        row = only(run(normal, limits(thermal()))[0])
        self.assertEqual(row["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(row["usable_sample_count"], "5")

        unavailable = [sample(n * 100_000, kelvin=None) for n in range(5)]
        row = only(run(unavailable, limits(thermal()))[0])
        self.assertEqual(row["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(row["unknown_reason"], "NO_USABLE_SAMPLE_FOR_THE_DECLARED_QUANTITY")
        self.assertEqual((row["evaluated_sample_count"], row["usable_sample_count"]), ("5", "0"))

        hot = [sample(0, kelvin=320.0), sample(100_000, kelvin=331.0),
               sample(200_000, kelvin=336.5), sample(300_000, kelvin=329.0)]
        row = only(run(hot, limits(thermal()))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(float(row["run_extreme_value"]), 336.5)
        row = only(run(hot, limits(overcurrent()))[0])
        self.assertEqual(row["unknown_reason"], "NO_USABLE_SAMPLE_FOR_THE_DECLARED_QUANTITY")

    def test_clock_units_boundary_hysteresis_and_gaps(self):
        dip = [sample(0, volts=24.0), sample(100_000, volts=22.0), sample(200_000, volts=21.0),
               sample(300_000, volts=21.8), sample(400_000, volts=23.0)]
        row = only(run(dip, limits(sag()), clock_basis="unknown")[0])
        self.assertEqual(row["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(row["unknown_reason"], "DURATION_REQUIRES_A_TIMEBASE_SOURCE_CLOCK_UNKNOWN")
        counted = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 2}
        row = only(run(dip, limits(counted), clock_basis="unknown")[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["duration_basis"], "SAMPLE_COUNT_NO_TIMEBASE_NEEDED")

        strict = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                  "limit": 22.0, "min_samples": 2}
        inclusive = dict(strict, comparison="at_or_below")
        self.assertEqual(only(run(dip, limits(strict), clock_basis="unknown")[0])
                         ["run_sample_count"], "2")
        self.assertEqual(only(run(dip, limits(inclusive), clock_basis="unknown")[0])
                         ["run_sample_count"], "3")

        held = {"condition": "voltage_sag", "unit": "V", "comparison": "below", "limit": 22.0,
                "release_limit": 23.5, "min_samples": 3}
        row = only(run(dip, limits(held), clock_basis="unknown")[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["run_sample_count"], "3")
        self.assertEqual(row["declared_release_limit"], "23.5")

        gapped = [sample(0, volts=24.0), sample(50_000, volts=21.0),
                  sample(100_000, volts=21.0), sample(5_000_000, volts=21.0),
                  sample(5_100_000, volts=21.0)]
        bounded = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 4, "max_gap_us": 200_000}
        row = only(run(gapped, limits(bounded))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual(row["run_sample_count"], "2")
        self.assertEqual(row["crossing_sample_count"], "4")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")

    def test_per_instance_identity_and_refusals(self):
        two = [sample(0, volts=24.0, instance="1"), sample(0, volts=21.0, instance="2"),
               sample(100_000, volts=24.1, instance="1"), sample(100_000, volts=20.5, instance="2"),
               sample(200_000, volts=24.0, instance="1"), sample(200_000, volts=20.9, instance="2")]
        three_low = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                     "limit": 22.0, "min_samples": 3}
        rows, report, _ = run(two, limits(three_low))
        self.assertEqual(len(rows), 2)
        by_instance = {row["instance_value"]: row for row in rows}
        self.assertEqual(by_instance["1"]["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(by_instance["2"]["condition_status"],
                         "CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION")
        self.assertEqual(by_instance["2"]["transition_evidence"], "NO_OBSERVED_BASELINE_BEFORE_RUN")
        self.assertEqual(report["instances"], ["1", "2"])
        self.assertEqual(report["status_counts"]["CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION"], 1)

        declared = limits(sag())
        rows_csv = observations(two)
        for bad in ("battery_missing", "not a column"):
            with self.assertRaises(ValueError):
                convert(rows_csv, declared, clock_basis="boot_relative", instance_column=bad)
        with self.assertRaises(ValueError):
            convert(rows_csv, declared, clock_basis="made_up", instance_column=None)
        for document in (
                {"declared_by": "x", "declared_basis": "y", "limits": [sag()]},
                {"declared_by": "x", "declared_basis": "y", "not_a_certified_safe_limit": False,
                 "limits": [sag()]},
                {"declared_by": "x", "declared_basis": "y", "not_a_certified_safe_limit": True,
                 "limits": []}):
            with self.assertRaises(ValueError):
                convert(rows_csv, json.dumps(document).encode(), clock_basis="boot_relative",
                        instance_column=None)
        for entry in (sag(unit="mV"), overcurrent(unit="mA"), thermal(unit="C"),
                      sag(comparison="above"), overcurrent(comparison="below"),
                      sag(min_samples=2), {"condition": "voltage_sag", "unit": "V",
                                           "comparison": "below", "limit": 22.0},
                      overcurrent(sign_handling=None), thermal(sign_handling="magnitude"),
                      sag(release_limit=21.0), {"condition": "brownout", "unit": "V",
                                                "comparison": "below", "limit": 1.0,
                                                "min_samples": 1}):
            with self.assertRaises(ValueError):
                convert(rows_csv, limits(entry), clock_basis="boot_relative", instance_column=None)

    def test_reaches_the_common_reader_with_a_practical_batch(self):
        rows = []
        for index in range(600):
            volts = 21.4 if 100 <= index < 140 else 24.0
            rows.append(sample(index * 100_000, volts=volts, amps=-15.0,
                               sign="NEGATIVE_DISCHARGING", kelvin=305.0))
        output, report = convert(observations(rows), limits(sag(), overcurrent(), thermal()),
                                 clock_basis="boot_relative",
                                 instance_column="battery_id_reported")
        results = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(results), 3)
        self.assertEqual(report["source_rows"], 600)
        statuses = {row["condition"]: row["condition_status"] for row in results}
        self.assertEqual(statuses["voltage_sag"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(statuses["overcurrent"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(statuses["thermal_over_limit"], "NO_SAMPLE_CROSSED_LIMIT")

        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conditions.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(path),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 3)
            self.assertEqual(common["platform_domain"], "Unknown")
            for observation in common["observations"]:
                self.assertEqual(observation["channel"], "onboard")
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertEqual(observation["fields"]["threshold_authority"],
                                 "OPERATOR_DECLARED_NOT_CERTIFIED")
                self.assertIn("not_a_cause", observation["fields"]["interpretation_basis"])
            by_condition = {o["fields"]["condition"]: o["fields"] for o in common["observations"]}
            self.assertEqual(by_condition["voltage_sag"]["declared_limit"], 22.0)
            self.assertEqual(by_condition["voltage_sag"]["condition_status"],
                             "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(by_condition["voltage_sag"]["run_span_us"], 3_900_000)
            self.assertEqual(by_condition["thermal_over_limit"]["declared_limit"], 330.0)

    def test_the_declared_criterion_chooses_the_run_and_gaps_never_resurrect_one(self):
        """A duration criterion wants the longest span, which can be a sparser run than the
        one with the most samples; picking by count alone would miss it."""
        rows = [sample(0, amps=3.0), sample(1, amps=3.0), sample(2, amps=3.0),
                sample(3, amps=1.0), sample(10, amps=3.0), sample(200, amps=3.0)]
        by_span = {"condition": "overcurrent", "unit": "A", "comparison": "above",
                   "limit": 2.0, "min_duration_us": 100, "max_gap_us": 1000,
                   "sign_handling": "signed_as_recorded"}
        row = only(run(rows, limits(by_span))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]), ("10", "200"))
        self.assertEqual((row["run_span_us"], row["run_sample_count"]), ("190", "2"))
        by_count = dict(by_span, min_samples=3)
        del by_count["min_duration_us"]
        row = only(run(rows, limits(by_count))[0])
        self.assertEqual((row["run_sample_count"], row["run_span_us"]), ("3", "2"))

        unbounded = dict(by_span)
        del unbounded["max_gap_us"]
        with self.assertRaises(ValueError):
            convert(observations(rows), limits(unbounded), clock_basis="boot_relative",
                    instance_column="battery_id_reported")

        held = {"condition": "voltage_sag", "unit": "V", "comparison": "below", "limit": 22.0,
                "release_limit": 23.5, "min_samples": 2, "max_gap_us": 150_000}
        carried = [sample(0, volts=24.0), sample(100_000, volts=21.0),
                   sample(900_000, volts=23.0), sample(1_000_000, volts=23.2)]
        row = only(run(carried, limits(held))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual(row["run_sample_count"], "1")

    def test_time_is_exact_and_discontinuities_are_boundaries(self):
        sag_two = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 2}
        big = [sample(9_007_199_254_740_991, volts=24.0),
               sample(9_007_199_254_740_993, volts=21.0),
               sample(9_007_199_254_740_995, volts=21.0)]
        row = only(run(big, limits(sag_two))[0])
        self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]),
                         ("9007199254740993", "9007199254740995"))
        self.assertEqual(row["run_span_us"], "2")
        for bad in ("1000.5", "1e6", "", " 10", "0x10"):
            with self.assertRaises(ValueError):
                convert(observations([sample(bad, volts=21.0)]), limits(sag_two),
                        clock_basis="boot_relative", instance_column="battery_id_reported")

        holed = [sample(0, volts=24.0), sample(100_000, volts=21.0),
                 sample(200_000, volts=None), sample(300_000, volts=21.0)]
        row = only(run(holed, limits(sag_two))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual((row["run_sample_count"], row["usable_sample_count"]), ("1", "3"))
        self.assertEqual((row["unusable_sample_count"], row["evaluated_sample_count"]), ("1", "4"))
        self.assertEqual(row["crossing_sample_count"], "2")

        duration = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                    "limit": 22.0, "min_duration_us": 100_000, "max_gap_us": 10_000_000}
        for series, order in (
                ([sample(500_000, volts=24.0), sample(100_000, volts=21.0),
                  sample(600_000, volts=21.0)], "BACKWARDS_OR_RESTARTED"),
                ([sample(0, volts=24.0), sample(100_000, volts=21.0),
                  sample(100_000, volts=21.0)], "AMBIGUOUS_DUPLICATE_TIMESTAMPS")):
            row = only(run(series, limits(duration))[0])
            self.assertEqual(row["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
            self.assertEqual(row["unknown_reason"], "DURATION_REQUIRES_AN_ORDERED_CLOCK_" + order)
            self.assertEqual(row["source_clock_order"], order)
            counted = only(run(series, limits(sag_two))[0])
            self.assertEqual(counted["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")

    def test_anchor_absent_target_and_duplicate_header(self):
        current = {"condition": "overcurrent", "unit": "A", "comparison": "above",
                   "limit": 1.0, "min_samples": 1, "sign_handling": "magnitude"}
        row = only(run([sample(5_000_000, volts=24.0)], limits(current))[0])
        self.assertEqual(row["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(row["record_time_us"], "5000000")
        self.assertEqual(row["evaluation_anchor_basis"], "FIRST_SOURCE_ROW_TIME_NOT_EVENT_ONSET")
        self.assertEqual((row["run_start_time_us"], row["run_span_us"]), ("", ""))
        output, report = convert(observations([sample(5_000_000, volts=24.0)]), limits(current),
                                 clock_basis="boot_relative", instance_column=None,
                                 analysis_time_us=42)
        row = only(list(csv.DictReader(io.StringIO(output))))
        self.assertEqual(row["record_time_us"], "42")
        self.assertEqual(row["evaluation_anchor_basis"], "CALLER_ANALYSIS_TIME_NOT_EVENT_ONSET")
        self.assertEqual(report["evaluation_anchor_us"], 42)

        asked = {"condition": "voltage_sag", "unit": "V", "comparison": "below", "limit": 22.0,
                 "min_samples": 2, "instance_value": "9"}
        rows, report, _ = run([sample(0, volts=24.0, instance="1")], limits(asked))
        row = only(rows)
        self.assertEqual((row["instance_value"], row["condition_status"]),
                         ("9", "UNKNOWN_INSUFFICIENT_EVIDENCE"))
        self.assertEqual(row["unknown_reason"], "REQUESTED_INSTANCE_NOT_PRESENT_IN_INPUT")
        self.assertEqual(report["instances_absent_from_input"], ["9"])
        self.assertEqual(report["evaluations"], 1)

        duplicated = b"record_time_us,battery_voltage_v,battery_voltage_v\n0,24.0,21.0\n"
        with self.assertRaises(ValueError):
            convert(duplicated, limits(asked), clock_basis="boot_relative", instance_column=None)

    def test_an_already_low_recording_is_not_a_proven_sag(self):
        sag_two = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 2}
        row = only(run([sample(n * 100_000, volts=18.0) for n in range(6)], limits(sag_two))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION")
        self.assertEqual(row["transition_evidence"], "NO_OBSERVED_BASELINE_BEFORE_RUN")
        self.assertEqual((row["run_sample_count"], row["crossing_sample_count"]), ("6", "6"))
        self.assertEqual(float(row["run_extreme_value"]), 18.0)

        holed = [sample(0, volts=None), sample(100_000, volts=21.0), sample(200_000, volts=21.0)]
        self.assertEqual(only(run(holed, limits(sag_two))[0])["condition_status"],
                         "CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION")

        row = only(run([sample(0, volts=24.0), sample(100_000, volts=21.0),
                        sample(200_000, volts=21.0)], limits(sag_two))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")

        with self.assertRaises(ValueError):
            convert(observations([sample(0, volts=21.0)]),
                    limits(dict(sag_two, min_samples=1)), clock_basis="boot_relative",
                    instance_column=None)
        for entry in (overcurrent(min_samples=1), thermal(min_samples=1)):
            rows = [sample(0, amps=-99.0, sign="NEGATIVE_DISCHARGING", kelvin=400.0)]
            self.assertEqual(only(run(rows, limits(entry))[0])["condition_status"],
                             "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")

    def test_evaluation_count_is_bounded_and_multi_instance_stays_usable(self):
        rows = [sample(t * 100_000, volts=24.0 if t % 5 else 21.0, instance=str(n))
                for t in range(40) for n in range(12)]
        entries = [{"condition": "voltage_sag", "unit": "V", "comparison": "below",
                    "limit": 22.0, "min_samples": 2}] * 8
        results, report, _ = run(rows, limits(*entries))
        self.assertEqual(len(results), 12 * 8)
        self.assertEqual(report["source_rows"], 480)
        self.assertEqual(len(report["instances"]), 12)
        many = [{"condition": "voltage_sag", "unit": "V", "comparison": "below",
                 "limit": 22.0, "min_samples": 2}] * 64
        wide = [sample(0, volts=24.0, instance=str(n)) for n in range(80)]
        with self.assertRaises(ValueError):
            convert(observations(wide), limits(*many), clock_basis="boot_relative",
                    instance_column="battery_id_reported")

    def test_whole_path_from_the_adopted_battery_converter(self):
        """Authored BAT rows -> the adopted converter -> this bridge -> the common reader.

        The normalized CSV is produced here by `scripts/convert_ardupilot_battery_csv.py`
        itself, so the connection is exercised rather than assumed. Its time column is
        `TimeUS` and its identity column is `Instance`.
        """
        from scripts import convert_ardupilot_battery_csv as adopted
        authored = ["TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct"]
        for index in range(30):
            volts = 21.0 if 10 <= index < 20 else 24.0
            authored.append("%d,0,%s,2.0,1000,3600,50" % (index * 100_000, volts))
        normalized = adopted.convert("\n".join(authored) + "\n", "ardupilot-bat")
        header = normalized.splitlines()[0].split(",")
        self.assertIn("battery_voltage_v", header)
        self.assertIn("TimeUS", header)

        declared = limits({"condition": "voltage_sag", "unit": "V", "comparison": "below",
                           "limit": 22.0, "min_duration_us": 500_000, "max_gap_us": 150_000})
        output, report = convert(normalized.encode(), declared, clock_basis="boot_relative",
                                 instance_column="Instance", time_column="TimeUS")
        row = only(list(csv.DictReader(io.StringIO(output))))
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")
        self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]),
                         ("1000000", "1900000"))
        self.assertEqual(row["run_span_us"], "900000")
        self.assertEqual((row["instance_column"], row["instance_value"]), ("Instance", "0"))
        self.assertEqual(report["time_column"], "TimeUS")
        self.assertEqual(report["source_rows"], 30)

        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conditions.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(path),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(fields["run_span_us"], 900000)
            self.assertEqual(fields["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")

    def test_a_later_genuine_sag_is_not_hidden_by_an_earlier_transitionless_run(self):
        """Selection must not settle on the biggest run before asking whether it can be a
        sag at all: a recording that begins low, recovers, and then genuinely sags must
        report the sag."""
        counted = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 2, "max_gap_us": 200_000}
        mixed = [sample(0, volts=18.0), sample(100_000, volts=18.1),
                 sample(200_000, volts=18.2), sample(300_000, volts=18.3),
                 sample(400_000, volts=24.0),
                 sample(500_000, volts=21.0), sample(600_000, volts=21.1)]
        row = only(run(mixed, limits(counted))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")
        self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]),
                         ("500000", "600000"))
        self.assertEqual(row["run_sample_count"], "2")
        self.assertEqual(row["crossing_sample_count"], "6")

        timed = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                 "limit": 22.0, "min_duration_us": 100_000, "max_gap_us": 200_000}
        row = only(run(mixed, limits(timed))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual((row["run_start_time_us"], row["run_span_us"]), ("500000", "100000"))

        strict = dict(counted, min_samples=4)
        row = only(run(mixed, limits(strict))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")
        self.assertEqual(row["run_start_time_us"], "500000")

        row = only(run([sample(n * 100_000, volts=18.0) for n in range(4)],
                       limits(counted))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION")
        self.assertEqual((row["run_sample_count"], row["run_start_time_us"]), ("4", "0"))

        rows = [sample(0, amps=3.0), sample(1, amps=3.0), sample(2, amps=3.0),
                sample(3, amps=1.0), sample(10, amps=3.0), sample(200, amps=3.0)]
        by_span = {"condition": "overcurrent", "unit": "A", "comparison": "above",
                   "limit": 2.0, "min_duration_us": 100, "max_gap_us": 1000,
                   "sign_handling": "signed_as_recorded"}
        row = only(run(rows, limits(by_span))[0])
        self.assertEqual((row["run_start_time_us"], row["run_end_time_us"]), ("10", "200"))

    def test_a_distant_baseline_is_not_adjacent_evidence(self):
        """The gap that breaks a run also breaks baseline adjacency, whether or not a run
        was open: a normal sample far before the first low one is not this crossing's
        baseline."""
        bounded = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 2, "max_gap_us": 200}
        far = [sample(0, volts=24.0), sample(1000, volts=21.0), sample(1100, volts=21.0)]
        row = only(run(far, limits(bounded))[0])
        self.assertEqual(row["condition_status"], "CROSSED_LIMIT_WITHOUT_OBSERVED_TRANSITION")
        self.assertEqual(row["transition_evidence"], "NO_OBSERVED_BASELINE_BEFORE_RUN")
        self.assertEqual(row["run_sample_count"], "2")

        near = [sample(0, volts=24.0), sample(100, volts=21.0), sample(200, volts=21.0)]
        row = only(run(near, limits(bounded))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["transition_evidence"], "OBSERVED_NON_CROSSING_SAMPLE_BEFORE_RUN")

        holed = [sample(0, volts=24.0), sample(100, volts=None), sample(150, volts=21.0),
                 sample(200, volts=21.0)]
        self.assertEqual(only(run(holed, limits(bounded))[0])["transition_evidence"],
                         "NO_OBSERVED_BASELINE_BEFORE_RUN")

    def test_timestamps_outside_the_common_reader_range_are_refused(self):
        """`musubi_decoded_csv::parse` reads the time cell as i64 then u64, so its range is
        0..=i64::MAX; a value the reader would reject is refused here instead of being
        written into output it cannot read."""
        counted = {"condition": "voltage_sag", "unit": "V", "comparison": "below",
                   "limit": 22.0, "min_samples": 2}
        for bad in (-5, "-5", -1, 2**63, str(2**63), 2**64):
            with self.assertRaises(ValueError):
                convert(observations([sample(bad, volts=21.0)]), limits(counted),
                        clock_basis="boot_relative", instance_column=None)
        with self.assertRaises(ValueError):
            convert(observations([sample(0, volts=21.0)]), limits(counted),
                    clock_basis="boot_relative", instance_column=None,
                    analysis_time_us=2**63)
        row = only(run([sample(0, volts=24.0), sample(2**63 - 2, volts=21.0),
                        sample(2**63 - 1, volts=21.0)], limits(counted))[0])
        self.assertEqual(row["run_end_time_us"], "9223372036854775807")

    def test_cli_writes_both_artifacts_and_claims_nothing_on_failure(self):
        rows = [sample(n * 100_000, volts=21.0 if n else 24.0) for n in range(6)]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "observations.csv").write_bytes(observations(rows))
            (base / "limits.json").write_bytes(limits(sag()))
            out = base / "conditions"
            subprocess.run(["python3", str(ROOT / "scripts/convert_electrical_events.py"),
                            str(base / "observations.csv"), str(base / "limits.json"), str(out),
                            "--source-clock-basis", "boot_relative",
                            "--instance-column", "battery_id_reported"],
                           check=True, capture_output=True, cwd=ROOT)
            written = list(csv.DictReader(io.StringIO((out / "observations.csv").read_text())))
            self.assertEqual(only(written)["condition_status"],
                             "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(json.loads((out / "report.json").read_text())["source_rows"], 6)
            failed = subprocess.run(["python3", str(ROOT / "scripts/convert_electrical_events.py"),
                                     str(base / "observations.csv"), str(base / "limits.json"),
                                     str(base / "other"), "--source-clock-basis", "unknown"],
                                    capture_output=True, cwd=ROOT)
            self.assertEqual(failed.returncode, 0)  # unknown clock is a reported Unknown, not a crash
            unknown_row = only(list(csv.DictReader(io.StringIO(
                (base / "other" / "observations.csv").read_text()))))
            self.assertEqual(unknown_row["unknown_reason"],
                             "DURATION_REQUIRES_A_TIMEBASE_SOURCE_CLOCK_UNKNOWN")
            broken = subprocess.run(["python3", str(ROOT / "scripts/convert_electrical_events.py"),
                                     str(base / "limits.json"), str(base / "limits.json"),
                                     str(base / "third"), "--source-clock-basis", "boot_relative"],
                                    capture_output=True, cwd=ROOT)
            self.assertEqual(broken.returncode, 2)
            self.assertIn(b"no condition is claimed", broken.stderr)

    def test_a_met_overcurrent_and_a_met_thermal_row_reach_the_common_reader(self):
        """The existing reader batch carries a met sag beside two NO_SAMPLE_CROSSED rows, so a
        qualified overcurrent or thermal result had never been read back through the profile."""
        rows = [sample(index * 100_000,
                       amps=-72.5 if 3 <= index <= 6 else -10.0,
                       sign="NEGATIVE_DISCHARGING",
                       kelvin=331.5 if 5 <= index <= 7 else 300.0)
                for index in range(10)]
        results, report, output = run(rows, limits(overcurrent(), thermal()))
        statuses = {row["condition"]: row["condition_status"] for row in results}
        self.assertEqual(statuses["overcurrent"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(statuses["thermal_over_limit"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")

        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conditions.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(path),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = {o["fields"]["condition"]: o["fields"] for o in common["observations"]}
            current = fields["overcurrent"]
            self.assertEqual(current["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(current["declared_limit"], 60.0)
            self.assertEqual(current["declared_unit"], "A")
            self.assertEqual(current["run_sample_count"], 4)
            self.assertEqual(current["run_extreme_value"], 72.5)  # magnitude, not the signed value
            self.assertEqual(current["declared_sign_handling"], "magnitude")
            self.assertEqual(current["observed_sign_basis"], "NEGATIVE_DISCHARGING")
            heat = fields["thermal_over_limit"]
            self.assertEqual(heat["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(heat["declared_limit"], 330.0)
            self.assertEqual(heat["declared_unit"], "K")
            self.assertEqual(heat["run_sample_count"], 3)
            self.assertEqual(heat["run_extreme_value"], 331.5)
            for found in (current, heat):
                self.assertEqual(found["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")
                self.assertIn("not_a_cause", found["interpretation_basis"])


FRACTION_COLUMNS = ["record_time_us", "battery_remaining_fraction", "battery_id_reported"]


def charge(time_us, fraction=None, instance="1"):
    row = {"record_time_us": time_us, "battery_id_reported": instance}
    if fraction is not None:
        row["battery_remaining_fraction"] = fraction
    return row


def low_charge(**overrides):
    """A plainly chosen example: below 0.25 of the reported capacity. Not a real limit."""
    entry = {"condition": "low_charge", "unit": "fraction", "comparison": "below", "limit": 0.25,
             "min_samples": 3}
    entry.update(overrides)
    return entry


def charge_run(rows, declared, clock_basis="boot_relative"):
    output, report = convert(observations(rows, FRACTION_COLUMNS), declared,
                             clock_basis=clock_basis, instance_column="battery_id_reported")
    return list(csv.DictReader(io.StringIO(output))), report, output


class SignBasisTests(unittest.TestCase):
    """A signed comparison needs somebody to have said what the recorded sign means."""

    def unstated(self, index):
        return sample(index * 100_000, amps=-72.5 if index >= 2 else -10.0, sign="")

    def test_magnitude_still_succeeds_where_a_signed_comparison_must_abstain(self):
        rows = [self.unstated(index) for index in range(6)]
        row = only(run(rows, limits(overcurrent()))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["observed_sign_basis"], "")
        self.assertEqual(row["declared_sign_basis"], "")
        signed = only(run(rows, limits(overcurrent(sign_handling="signed_as_recorded")))[0])
        self.assertEqual(signed["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(signed["unknown_reason"],
                         "UNSTATED_CURRENT_SIGN_BASIS_FOR_SIGNED_COMPARISON")

    def test_an_absent_basis_column_abstains_for_a_signed_comparison(self):
        columns = [name for name in COLUMNS if name != "battery_current_sign_basis"]
        output, _ = convert(observations([sample(n * 100_000, amps=70.0) for n in range(4)],
                                         columns),
                            limits(overcurrent(sign_handling="signed_as_recorded")),
                            clock_basis="boot_relative", instance_column="battery_id_reported")
        row = only(list(csv.DictReader(io.StringIO(output))))
        self.assertEqual(row["unknown_reason"], "UNSTATED_CURRENT_SIGN_BASIS_FOR_SIGNED_COMPARISON")

    def test_an_operator_declaration_supplies_or_contradicts_the_missing_basis(self):
        rows = [self.unstated(index) for index in range(6)]
        declared = overcurrent(sign_handling="signed_as_recorded", sign_basis="POSITIVE_DISCHARGING")
        supplied = only(run(rows, limits(declared))[0])
        self.assertEqual(supplied["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(supplied["declared_sign_basis"], "POSITIVE_DISCHARGING")
        self.assertEqual(supplied["observed_sign_basis"], "")
        stated = [sample(index * 100_000, amps=70.0, sign="POSITIVE_DISCHARGING")
                  for index in range(4)]
        agreeing = only(run(stated, limits(declared))[0])
        self.assertEqual(agreeing["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(agreeing["observed_sign_basis"], "POSITIVE_DISCHARGING")
        contradicted = only(run([sample(index * 100_000, amps=70.0, sign="NEGATIVE_DISCHARGING")
                                 for index in range(4)], limits(declared))[0])
        self.assertEqual(contradicted["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
        self.assertEqual(contradicted["unknown_reason"],
                         "DECLARED_CURRENT_SIGN_BASIS_CONTRADICTS_INPUT")

    def test_a_whitespace_only_basis_states_nothing(self):
        for blank in ("   ", "\t", " \t "):
            rows = [sample(index * 100_000, amps=-72.5 if index >= 2 else -10.0, sign=blank)
                    for index in range(6)]
            signed = only(run(rows, limits(overcurrent(sign_handling="signed_as_recorded")))[0])
            self.assertEqual(signed["condition_status"], "UNKNOWN_INSUFFICIENT_EVIDENCE")
            self.assertEqual(signed["unknown_reason"],
                             "UNSTATED_CURRENT_SIGN_BASIS_FOR_SIGNED_COMPARISON")
            self.assertEqual(signed["observed_sign_basis"], "")
            self.assertEqual(only(run(rows, limits(overcurrent()))[0])["condition_status"],
                             "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            supplied = only(run(rows, limits(overcurrent(
                sign_handling="signed_as_recorded", sign_basis="NEGATIVE_DISCHARGING")))[0])
            self.assertEqual(supplied["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
            self.assertEqual(supplied["declared_sign_basis"], "NEGATIVE_DISCHARGING")
        partial = [sample(0, amps=-72.5, sign="NEGATIVE_DISCHARGING"),
                   sample(100_000, amps=-72.5, sign="  "),
                   sample(200_000, amps=-72.5, sign="NEGATIVE_DISCHARGING")]
        self.assertEqual(only(run(partial, limits(overcurrent(
            sign_handling="signed_as_recorded")))[0])["unknown_reason"],
            "UNSTATED_CURRENT_SIGN_BASIS_FOR_SIGNED_COMPARISON")
        conflicting = [sample(0, amps=-72.5, sign="NEGATIVE_DISCHARGING"),
                       sample(100_000, amps=-72.5, sign="POSITIVE_DISCHARGING"),
                       sample(200_000, amps=-72.5, sign="  ")]
        self.assertEqual(only(run(conflicting, limits(overcurrent(
            sign_handling="signed_as_recorded")))[0])["unknown_reason"],
            "INCONSISTENT_CURRENT_SIGN_BASIS")

    def test_two_observed_bases_still_abstain_and_a_useless_declaration_is_refused(self):
        mixed = [sample(0, amps=70.0, sign="NEGATIVE_DISCHARGING"),
                 sample(100_000, amps=70.0, sign="POSITIVE_DISCHARGING")]
        self.assertEqual(only(run(mixed, limits(overcurrent()))[0])["unknown_reason"],
                         "INCONSISTENT_CURRENT_SIGN_BASIS")
        for useless in (overcurrent(sign_basis="POSITIVE_DISCHARGING"),
                        sag(sign_basis="POSITIVE_DISCHARGING"),
                        overcurrent(sign_handling="signed_as_recorded", sign_basis="not a name!")):
            with self.assertRaises(ValueError):
                convert(observations([sample(0, amps=70.0, volts=24.0, sign="X")]),
                        limits(useless), clock_basis="boot_relative", instance_column=None)


class LowChargeTests(unittest.TestCase):
    """A declared remaining-charge fraction, never a calibrated state of charge."""

    def test_a_declared_fraction_crossing_is_reported_and_a_higher_one_is_not(self):
        rows = [charge(n * 100_000, 0.18 if n >= 4 else 0.62) for n in range(9)]
        row = only(charge_run(rows, limits(low_charge()))[0])
        self.assertEqual(row["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(row["quantity"], "remaining_charge_fraction")
        self.assertEqual(row["quantity_column"], "battery_remaining_fraction")
        self.assertEqual(row["declared_unit"], "fraction")
        self.assertEqual(row["run_sample_count"], "5")
        self.assertEqual(row["run_extreme_value"], "0.18")
        self.assertEqual(row["declared_sign_handling"], "")
        high = only(charge_run([charge(n * 100_000, 0.55) for n in range(9)],
                               limits(low_charge()))[0])
        self.assertEqual(high["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        begins_low = only(charge_run([charge(n * 100_000, 0.10) for n in range(4)],
                                     limits(low_charge()))[0])
        self.assertEqual(begins_low["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")

    def test_absent_and_out_of_range_fractions_are_holes_not_empty_packs(self):
        rows = [charge(0, 0.62), charge(100_000), charge(200_000, -1),
                charge(300_000, 1.5), charge(400_000, 0.62)]
        row, report, _ = charge_run(rows, limits(low_charge()))
        row = only(row)
        self.assertEqual(row["evaluated_sample_count"], "5")
        self.assertEqual(row["usable_sample_count"], "2")
        self.assertEqual(row["unusable_sample_count"], "3")  # blank, -1 sentinel and 1.5
        self.assertEqual(row["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(row["crossing_sample_count"], "0")  # never read as an empty pack
        nothing = only(charge_run([charge(0), charge(100_000, -1)], limits(low_charge()))[0])
        self.assertEqual(nothing["unknown_reason"], "NO_USABLE_SAMPLE_FOR_THE_DECLARED_QUANTITY")
        for refused in (low_charge(limit=1.5), low_charge(comparison="above"),
                        low_charge(unit="%"), low_charge(release_limit=2.0)):
            with self.assertRaises(ValueError):
                charge_run([charge(0, 0.5)], limits(refused))

    def test_a_low_charge_duration_claim_abstains_without_a_usable_clock(self):
        rows = [charge(n * 100_000, 0.10) for n in range(5)]
        spanned = low_charge(min_samples=None, min_duration_us=300_000, max_gap_us=150_000)
        spanned.pop("min_samples")
        self.assertEqual(only(charge_run(rows, limits(spanned))[0])["condition_status"],
                         "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        unknown_clock = only(charge_run(rows, limits(spanned), clock_basis="unknown")[0])
        self.assertEqual(unknown_clock["unknown_reason"],
                         "DURATION_REQUIRES_A_TIMEBASE_SOURCE_CLOCK_UNKNOWN")
        restarted = [charge(0, 0.10), charge(100_000, 0.10), charge(50_000, 0.10)]
        self.assertEqual(only(charge_run(restarted, limits(spanned))[0])["unknown_reason"],
                         "DURATION_REQUIRES_AN_ORDERED_CLOCK_BACKWARDS_OR_RESTARTED")
        gapped = [charge(0, 0.10), charge(100_000, 0.10), charge(5_000_000, 0.10),
                  charge(5_100_000, 0.10)]
        broken = only(charge_run(gapped, limits(spanned))[0])
        self.assertEqual(broken["condition_status"], "CROSSED_LIMIT_NOT_SUSTAINED")
        self.assertEqual(broken["crossing_sample_count"], "4")

    def test_a_separate_authored_input_reaches_the_common_reader(self):
        rows = [charge(n * 250_000, 0.44 if n < 3 else 0.21, instance="2") for n in range(8)]
        results, _, output = charge_run(rows, limits(low_charge(instance_value="2")))
        self.assertEqual(only(results)["condition_status"],
                         "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conditions.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(path),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["condition"], "low_charge")
            self.assertEqual(fields["condition_status"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(fields["declared_limit"], 0.25)
            self.assertEqual(fields["declared_unit"], "fraction")
            self.assertEqual(fields["instance_value"], 2)  # reader types a numeric-looking cell
            self.assertEqual(fields["run_sample_count"], 5)
            self.assertEqual(fields["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")
            self.assertIn("not_a_cause", fields["interpretation_basis"])


class DigestTests(unittest.TestCase):
    def test_both_digests_are_the_exact_input_bytes(self):
        rows = [sample(n * 100_000, volts=21.0 if n else 24.0) for n in range(6)]
        observed, declared = observations(rows), limits(sag())
        output, report = convert(observed, declared, clock_basis="boot_relative",
                                 instance_column="battery_id_reported")
        row = only(list(csv.DictReader(io.StringIO(output))))
        for column, raw in (("source_observations_sha256", observed),
                            ("declared_limits_sha256", declared)):
            self.assertEqual(row[column], hashlib.sha256(raw).hexdigest())
            self.assertEqual(report[column], row[column])
        moved, _ = convert(observations(rows).replace(b"24.0", b"24.1"), declared,
                           clock_basis="boot_relative", instance_column="battery_id_reported")
        changed = only(list(csv.DictReader(io.StringIO(moved))))
        self.assertNotEqual(changed["source_observations_sha256"],
                            row["source_observations_sha256"])
        self.assertEqual(changed["declared_limits_sha256"], row["declared_limits_sha256"])


if __name__ == "__main__":
    unittest.main()


GCS_TAIL = ",sig ,Len,40,crc16,4242"


def gcs_status_row(voltage_mv, current_ca, remaining_pct, *, compid=" 1",
                   stamp="2026-09-20T11:22:33.456"):
    header = f"FD,1F, 0, 0,2B, A,{compid},     1"
    pairs = (("onboard_control_sensors_present", 1467087), ("voltage_battery", voltage_mv),
             ("current_battery", current_ca), ("battery_remaining", remaining_pct),
             ("drop_rate_comm", 0))
    body = ",".join(f"{name},{value}" for name, value in pairs)
    return f"{stamp},{header},mavlink_sys_status_t,{body}{GCS_TAIL}"


def gcs_observations(*rows, capture_time_us=1_789_000_000_000_000):
    """Authored raw export -> the adopted GCS converter. Never a real recording."""
    from scripts.convert_gcs_tlog_csv import convert as gcs_convert
    output, _ = gcs_convert("\n".join(rows) + "\n", "mavlink_sys_status_t",
                            capture_time_us=capture_time_us)
    return output.encode()


def gcs_limits(*entries):
    return limits(*entries)


def gcs_source_limits(*entries):
    """The pinned source binding: a SYS_STATUS row is identified by system AND component."""
    document = json.loads(limits(*entries))
    document["source_identity"] = ["source_sysid", "source_compid"]
    return json.dumps(document).encode()


def current_from_gcs(**overrides):
    entry = {"condition": "overcurrent", "unit": "A", "column": "electrical_current_a",
             "comparison": "above", "limit": 60.0, "min_samples": 3, "sign_handling": "magnitude"}
    entry.update(overrides)
    return entry


def charge_from_gcs(**overrides):
    entry = {"condition": "low_charge", "unit": "fraction", "comparison": "below", "limit": 0.25,
             "min_samples": 3}
    entry.update(overrides)
    return entry


class AdoptedProducerReuseTests(unittest.TestCase):
    """The same evaluator on another adopted producer's output, with no CSV rewriting."""

    def evaluate(self, observed, declared, *instance_columns, clock_basis="unknown"):
        output, report = convert(observed, declared, clock_basis=clock_basis,
                                 instance_column=list(instance_columns) or None)
        return list(csv.DictReader(io.StringIO(output))), report, output

    def test_gcs_export_reaches_common_output_through_the_same_evaluator(self):
        observed = gcs_observations(*[gcs_status_row(24000, 1000, 62)] * 3,
                                    *[gcs_status_row(21000, -7000, 18)] * 4)
        results, report, output = self.evaluate(
            observed, gcs_limits(current_from_gcs(), charge_from_gcs()),
            "source_sysid", "source_compid")
        found = {row["condition"]: row for row in results}
        self.assertEqual(found["overcurrent"]["condition_status"],
                         "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(found["overcurrent"]["quantity_column"], "electrical_current_a")
        self.assertEqual(found["overcurrent"]["run_extreme_value"], "70.0")
        self.assertEqual(found["low_charge"]["condition_status"],
                         "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(found["low_charge"]["quantity_column"], "battery_remaining_fraction")
        self.assertEqual(found["low_charge"]["run_extreme_value"], "0.18")
        for row in results:
            self.assertEqual(row["source_clock_order"], "AMBIGUOUS_DUPLICATE_TIMESTAMPS")
            self.assertEqual(row["instance_column"], "source_sysid|source_compid")
            self.assertEqual(row["instance_value"], "10|1")  # hex header ids parsed to decimal
            self.assertEqual(row["source_observations_sha256"],
                             hashlib.sha256(observed).hexdigest())
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("common reader binary not configured")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conditions.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(path),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = {o["fields"]["condition"]: o["fields"] for o in common["observations"]}
            self.assertEqual(fields["overcurrent"]["quantity_column"], "electrical_current_a")
            self.assertEqual(fields["overcurrent"]["condition_status"],
                             "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
            self.assertEqual(fields["low_charge"]["declared_unit"], "fraction")
            for found in fields.values():
                self.assertEqual(found["threshold_authority"], "OPERATOR_DECLARED_NOT_CERTIFIED")
                self.assertIn("not_a_cause", found["interpretation_basis"])

    def test_two_components_of_one_system_are_never_pooled(self):
        observed = gcs_observations(*[gcs_status_row(24000, 1000, 62)] * 4,
                                    *[gcs_status_row(21000, -7000, 18, compid="BF")] * 4)
        results, _, _ = self.evaluate(observed, gcs_limits(current_from_gcs()),
                                      "source_sysid", "source_compid")
        self.assertEqual({row["instance_value"] for row in results}, {"10|1", "10|191"})
        statuses = {row["instance_value"]: row["condition_status"] for row in results}
        self.assertEqual(statuses["10|1"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(statuses["10|191"], "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        with self.assertRaises(ValueError):
            self.evaluate(observed, gcs_source_limits(current_from_gcs()), "source_sysid")
        with self.assertRaises(ValueError):
            self.evaluate(observed, gcs_limits(current_from_gcs(instance_value="10")),
                          "source_sysid", "source_compid")
        targeted, _, _ = self.evaluate(
            observed, gcs_limits(current_from_gcs(instance_value=["10", "191"])),
            "source_sysid", "source_compid")
        self.assertEqual(only(targeted)["condition_status"],
                         "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")

    def test_a_withheld_sentinel_stays_a_hole_and_never_a_reading(self):
        observed = gcs_observations(gcs_status_row(24000, 1000, 62),
                                    gcs_status_row(65535, -1, -1),          # all three withheld
                                    *[gcs_status_row(21000, -7000, 18)] * 3)
        results, _, _ = self.evaluate(observed, gcs_limits(current_from_gcs(), charge_from_gcs()),
                                      "source_sysid", "source_compid")
        found = {row["condition"]: row for row in results}
        for row in found.values():
            self.assertEqual(row["evaluated_sample_count"], "5")
            self.assertEqual(row["usable_sample_count"], "4")
            self.assertEqual(row["unusable_sample_count"], "1")
        self.assertEqual(found["overcurrent"]["condition_status"],
                         "DECLARED_LIMIT_CROSSED_AND_WINDOW_MET")
        self.assertEqual(found["overcurrent"]["run_sample_count"], "3")

    def test_the_declared_binding_refuses_what_it_cannot_honestly_answer(self):
        observed = gcs_observations(*[gcs_status_row(21000, -7000, 18)] * 4)
        with self.assertRaises(ValueError):
            self.evaluate(observed, gcs_limits(current_from_gcs(column="battery_current_a")),
                          "source_sysid", "source_compid")
        for wrong in (current_from_gcs(unit="mA"), current_from_gcs(unit="A", condition="low_charge"),
                      charge_from_gcs(unit="percent"), current_from_gcs(column="not a column")):
            with self.assertRaises(ValueError):
                self.evaluate(observed, gcs_limits(wrong), "source_sysid", "source_compid")
        with self.assertRaises(ValueError):
            self.evaluate(observed, gcs_limits(current_from_gcs()), "battery_id_reported")
        signed = only(self.evaluate(observed, gcs_limits(
            current_from_gcs(sign_handling="signed_as_recorded")),
            "source_sysid", "source_compid")[0])
        self.assertEqual(signed["unknown_reason"],
                         "UNSTATED_CURRENT_SIGN_BASIS_FOR_SIGNED_COMPARISON")
        declared = only(self.evaluate(observed, gcs_limits(current_from_gcs(
            sign_handling="signed_as_recorded", sign_basis="NEGATIVE_DISCHARGING")),
            "source_sysid", "source_compid")[0])
        self.assertEqual(declared["condition_status"], "NO_SAMPLE_CROSSED_LIMIT")
        self.assertEqual(declared["declared_sign_basis"], "NEGATIVE_DISCHARGING")
        spanned = charge_from_gcs(min_duration_us=300_000, max_gap_us=150_000)
        spanned.pop("min_samples")
        self.assertEqual(only(self.evaluate(observed, gcs_limits(spanned),
                                            "source_sysid", "source_compid")[0])["unknown_reason"],
                         "DURATION_REQUIRES_A_TIMEBASE_SOURCE_CLOCK_UNKNOWN")

    def test_a_blank_or_ambiguous_identity_cell_is_refused(self):
        observed = gcs_observations(*[gcs_status_row(21000, -7000, 18)] * 3)
        header, *rows = observed.decode().splitlines()
        columns = header.split(",")
        blanked = [",".join("" if name == "source_compid" else cell
                            for name, cell in zip(columns, row.split(","))) for row in rows]
        with self.assertRaises(ValueError):
            self.evaluate("\n".join([header, *blanked]).encode() + b"\n",
                          gcs_limits(current_from_gcs()), "source_sysid", "source_compid")
        separated = [",".join("1|9" if name == "source_compid" else cell
                              for name, cell in zip(columns, row.split(","))) for row in rows]
        with self.assertRaises(ValueError):
            self.evaluate("\n".join([header, *separated]).encode() + b"\n",
                          gcs_limits(current_from_gcs()), "source_sysid", "source_compid")

    def test_the_pinned_source_binding_refuses_an_incomplete_identity(self):
        observed = gcs_observations(*[gcs_status_row(24000, 1000, 62)] * 4,
                                    *[gcs_status_row(21000, -7000, 18, compid="BF")] * 4)
        results, _, _ = self.evaluate(observed, gcs_source_limits(current_from_gcs()),
                                      "source_sysid", "source_compid")
        self.assertEqual({row["instance_value"] for row in results}, {"10|1", "10|191"})
        self.assertEqual(len(self.evaluate(observed, gcs_source_limits(current_from_gcs()),
                                           "source_compid", "source_sysid")[0]), 2)
        for incomplete in (("source_sysid",), ("source_compid",), ()):
            with self.assertRaises(ValueError):
                self.evaluate(observed, gcs_source_limits(current_from_gcs()), *incomplete)
        absent = json.loads(gcs_source_limits(current_from_gcs()))
        absent["source_identity"] = ["source_sysid", "battery_id_reported"]
        with self.assertRaises(ValueError):
            self.evaluate(observed, json.dumps(absent).encode(),
                          "source_sysid", "source_compid")
        for invalid in ([], "source_sysid", ["source_sysid", "source_sysid"], [None], ["a b"]):
            broken = json.loads(gcs_source_limits(current_from_gcs()))
            broken["source_identity"] = invalid
            with self.assertRaises(ValueError):
                self.evaluate(observed, json.dumps(broken).encode(),
                              "source_sysid", "source_compid")

    def test_a_present_but_null_declaration_is_refused_not_ignored(self):
        observed = gcs_observations(*[gcs_status_row(21000, -7000, 18)] * 4)
        default = only(self.evaluate(observed, gcs_limits(
            {"condition": "low_charge", "unit": "fraction", "comparison": "below",
             "limit": 0.25, "min_samples": 3}), "source_sysid", "source_compid")[0])
        self.assertEqual(default["quantity_column"], "battery_remaining_fraction")
        for null_key in ("column", "sign_basis", "instance_value"):
            entry = current_from_gcs(sign_handling="signed_as_recorded",
                                     sign_basis="NEGATIVE_DISCHARGING", instance_value=["10", "1"])
            entry[null_key] = None
            with self.assertRaises(ValueError):
                self.evaluate(observed, gcs_limits(entry), "source_sysid", "source_compid")
