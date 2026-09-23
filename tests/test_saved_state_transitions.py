"""Authored CSVs in the shape adopted converters already write; no device and no real log."""
import csv
import io
import json
import os
from pathlib import Path
import time
import subprocess
import tempfile
import unittest

from scripts.convert_saved_state_transitions import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-state-transitions/profile.toml"
CITATION = "tlog-state/gps-reported-sentinel.md"
CLOCK = "us_local_capture_clock_declared_by_the_source_profile"


def saved(rows, header="record_time_us,subject_id,reported_state"):
    return (header + "\n" + "".join(",".join(str(cell) for cell in row) + "\n"
                                    for row in rows)).encode()


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


def subject(value):
    return json.loads(unhex(value))


class SavedStateTransitionTests(unittest.TestCase):
    def test_a_reported_state_change_is_carried_with_its_subject_clock_and_meaning(self):
        data = saved([(0, "gps-1", "3D_FIX"), (1000, "gps-2", "NO_FIX"), (2000, "gps-1", "3D_FIX"),
                      (3000, "gps-1", "NO_FIX"), (4000, "gps-2", "NO_FIX")])
        output, report = convert(data, clock_basis=CLOCK, subjects=("subject_id",),
                                 states=(("reported_state", CITATION),))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 3)  # two first values, one change; a repeat is not a transition
        self.assertEqual([(r["record_time_us"], subject(r["subject_hex"])[0],
                           unhex(r["previous_state_hex"]), unhex(r["reported_state_hex"]),
                           r["transition_disposition"], r["transition_index"]) for r in rows],
                         [("0", "gps-1", "", "3D_FIX", "FIRST_REPORTED_VALUE", "1"),
                          ("1000", "gps-2", "", "NO_FIX", "FIRST_REPORTED_VALUE", "1"),
                          ("3000", "gps-1", "3D_FIX", "NO_FIX",
                           "CHANGE_BETWEEN_TWO_REPORTED_VALUES", "2")])
        self.assertEqual((rows[2]["previous_record_time_us"], rows[2]["elapsed_us"]),
                         ("2000", "1000"))
        self.assertEqual(unhex(rows[2]["state_meaning_hex"]), CITATION)
        self.assertEqual(rows[2]["declared_clock_basis"], CLOCK)
        self.assertIn("NOT_A_PHYSICAL_CAUSE", rows[2]["state_transition_basis"])
        self.assertEqual((report["state_transitions"], report["reported_value_changes"]), (3, 1))
        self.assertEqual(report["physical_cause_records"], "NOT_DERIVED")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "saved.csv").write_bytes(data)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_saved_state_transitions.py"),
                str(base / "saved.csv"), str(base / "out"), "--clock-basis", CLOCK,
                "--subject-column", "subject_id", "--state-column", "reported_state",
                "--state-meaning", f"reported_state={CITATION}"], check=True, capture_output=True)
            self.assertEqual((base / "out/observations.csv").read_text(), output)
            self.assertEqual(json.loads((base / "out/report.json").read_text())["saved_rows"], 5)

    def test_a_blank_cell_breaks_the_chain_instead_of_being_read_as_a_state(self):
        data = saved([(0, "a", "UP"), (1000, "a", ""), (2000, "a", "UP")])
        output, report = convert(data, clock_basis=CLOCK, subjects=("subject_id",),
                                 states=(("reported_state", CITATION),))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["transition_disposition"] for r in rows],
                         ["FIRST_REPORTED_VALUE", "PREVIOUS_VALUE_NOT_REPORTED"])
        self.assertEqual(rows[1]["previous_state_hex"], "")
        self.assertEqual((report["blank_state_records"], report["reported_value_changes"]), (1, 0))

    def test_a_window_states_what_the_file_holds_against_the_declared_expectation(self):
        sparse = [(step * 250000, "a", "UP") for step in range(4)]
        dense = [(1000000 + step * 100000, "a", "UP") for step in range(10)]
        output, report = convert(saved(sparse + dense), clock_basis=CLOCK,
                                 subjects=("subject_id",), window_us=1000000,
                                 expected_interval_us=100000)
        rows = [r for r in csv.DictReader(io.StringIO(output))
                if r["event_kind"] == "OBSERVATION_WINDOW"]
        self.assertEqual([(r["window_index"], r["window_observations"],
                           r["window_expected_observations"], r["window_rate_ratio"],
                           r["window_disposition"]) for r in rows],
                         [("1", "4", "10", "0.400000", "COMPARED_WITH_THE_DECLARED_EXPECTATION"),
                          ("2", "10", "", "", "PARTIAL_WINDOW_NOT_COMPARABLE")])
        self.assertIn("NOT_PACKET_LOSS", rows[0]["observation_rate_basis"])
        self.assertIn("NEVER_EXTRAPOLATED_FROM_THE_EXPECTED_INTERVAL", rows[0]["observation_rate_basis"])
        self.assertEqual((rows[0]["window_observed_span_us"], report["partial_windows"]),
                         ("1900000", 1))
        declared, declared_report = convert(saved(sparse + dense), clock_basis=CLOCK,
                                            subjects=("subject_id",), window_us=1000000,
                                            expected_interval_us=100000,
                                            recording_end_us=2000000)
        closing = [r for r in csv.DictReader(io.StringIO(declared))
                   if r["event_kind"] == "OBSERVATION_WINDOW"][1]
        self.assertEqual((closing["window_expected_observations"], closing["window_rate_ratio"],
                          closing["window_disposition"], closing["declared_recording_end_us"]),
                         ("10", "1.000000", "COMPARED_WITHIN_THE_DECLARED_RECORDING_BOUNDARY",
                          "2000000"))
        self.assertEqual(declared_report["windows_covered_by_declaration"], 1)
        with self.assertRaises(ValueError):  # a declared end before the file contradicts the file
            convert(saved(sparse + dense), clock_basis=CLOCK, subjects=("subject_id",),
                    window_us=1000000, expected_interval_us=100000, recording_end_us=1000)

    def test_a_window_the_file_does_not_span_is_not_reported_as_a_shortfall(self):
        output, report = convert(saved([(step * 100000, "a", "UP") for step in range(13)]),
                                 clock_basis=CLOCK, subjects=("subject_id",),
                                 window_us=1000000, expected_interval_us=100000)
        rows = [r for r in csv.DictReader(io.StringIO(output))
                if r["event_kind"] == "OBSERVATION_WINDOW"]
        self.assertEqual(rows[0]["window_disposition"], "COMPARED_WITH_THE_DECLARED_EXPECTATION")
        self.assertEqual((rows[1]["window_observations"], rows[1]["window_expected_observations"],
                          rows[1]["window_rate_ratio"], rows[1]["window_disposition"]),
                         ("3", "", "", "PARTIAL_WINDOW_NOT_COMPARABLE"))
        self.assertEqual(report["partial_windows"], 1)

    def test_a_repeated_state_advances_the_timing_it_reports(self):
        """A@0, A@9, B@10: the previous record is the last A, not the first one."""
        output, report = convert(saved([(0, "a", "A"), (9, "a", "A"), (10, "a", "B")]),
                                 clock_basis=CLOCK, subjects=("subject_id",),
                                 states=(("reported_state", CITATION),))
        change = list(csv.DictReader(io.StringIO(output)))[1]
        self.assertEqual((change["previous_record_time_us"], change["elapsed_us"]), ("9", "1"))
        self.assertEqual(unhex(change["previous_state_hex"]), "A")
        self.assertEqual((report["repeated_state_records"], report["state_transitions"]), (1, 2))
        broken, _ = convert(saved([(0, "a", "A"), (9, "a", ""), (10, "a", "B")]),
                            clock_basis=CLOCK, subjects=("subject_id",),
                            states=(("reported_state", CITATION),))
        after = list(csv.DictReader(io.StringIO(broken)))[1]
        self.assertEqual((after["transition_disposition"], after["previous_record_time_us"],
                          after["elapsed_us"]), ("PREVIOUS_VALUE_NOT_REPORTED", "", ""))

    def test_identity_is_reversible_and_a_missing_component_is_refused(self):
        """Two distinct pairs whose naive join would collide keep separate histories."""
        header = "record_time_us,left,right,reported_state"
        data = saved([(0, "a\u001fb", "c", "UP"), (1000, "a", "b\u001fc", "UP"),
                      (2000, "a\u001fb", "c", "DOWN")], header=header)
        output, _ = convert(data, clock_basis=CLOCK, subjects=("left", "right"),
                            states=(("reported_state", CITATION),))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([subject(r["subject_hex"]) for r in rows],
                         [["a\u001fb", "c"], ["a", "b\u001fc"], ["a\u001fb", "c"]])
        self.assertEqual([r["transition_disposition"] for r in rows],
                         ["FIRST_REPORTED_VALUE", "FIRST_REPORTED_VALUE",
                          "CHANGE_BETWEEN_TWO_REPORTED_VALUES"])
        self.assertEqual(subject(rows[0]["subject_columns_hex"]), ["left", "right"])
        with self.assertRaises(ValueError):
            convert(saved([(0, "a", "", "UP"), (1000, "b", "", "DOWN")], header=header),
                    clock_basis=CLOCK, subjects=("left", "right"),
                    states=(("reported_state", CITATION),))

    def test_a_wide_saved_gap_is_refused_before_the_work_is_done(self):
        """Two rows far apart must not tile into unbounded windows or memory."""
        started = time.monotonic()
        with self.assertRaises(ValueError) as refusal:
            convert(saved([(0, "a", "UP"), (2**60, "a", "UP")]), clock_basis=CLOCK,
                    subjects=("subject_id",), window_us=1000, expected_interval_us=1000)
        self.assertIn("more windows than the finite work cap", str(refusal.exception))
        self.assertLess(time.monotonic() - started, 5)
        rows = [(step * 100000, "a", "UP" if step % 2500 else "DOWN") for step in range(10000)]
        started = time.monotonic()
        output, report = convert(saved(rows), clock_basis=CLOCK, subjects=("subject_id",),
                                 states=(("reported_state", CITATION),),
                                 window_us=10000000, expected_interval_us=100000)
        self.assertEqual((report["saved_rows"], report["observation_windows"],
                          report["planned_windows"]), (10000, 100, 100))
        self.assertEqual(report["state_transitions"], 8)
        self.assertLess(time.monotonic() - started, 20)
        self.assertLess(len(output), 64 * 1024 * 1024)

    def test_long_saved_cells_are_counted_as_real_bytes_and_refused_whole(self):
        """A saved cell can be long and every text value is hex-expanded, so no per-row width holds."""
        long_state = "S" * 60000
        output, report = convert(saved([(0, "a", long_state), (1000, "a", "UP")]),
                                 clock_basis=CLOCK, subjects=("subject_id",),
                                 states=(("reported_state", CITATION),))
        self.assertGreater(len(output.encode("utf-8")), 240000)
        self.assertEqual(report["output_bytes"], len(output.encode("utf-8")))
        self.assertEqual(unhex(list(csv.DictReader(io.StringIO(output)))[1]["previous_state_hex"]),
                         long_state)
        wide = "I" * 50000
        rows = [(step * 1000, wide, "UP" if step % 2 else "DOWN") for step in range(3)]
        rows.append((999000, wide, "UP"))
        with self.assertRaises(ValueError) as refusal:
            convert(saved(rows), clock_basis=CLOCK, subjects=("subject_id",),
                    states=(("reported_state", CITATION),),
                    window_us=1000, expected_interval_us=1000)
        self.assertIn("exceed the output byte bound", str(refusal.exception))
        self.assertIn("refused whole rather than truncated", str(refusal.exception))
        with self.assertRaises(ValueError) as unparsed:
            convert(saved([(0, "a", "X" * 200000)]), clock_basis=CLOCK,
                    subjects=("subject_id",), states=(("reported_state", CITATION),))
        self.assertIn("cannot be parsed as written", str(unparsed.exception))

    def test_the_declarations_it_cannot_supply_itself_are_refused(self):
        good = saved([(0, "a", "UP"), (1000, "a", "DOWN")])
        for kwargs, why in (
                (dict(states=(("reported_state", ""),)), "a state column with no meaning source"),
                (dict(states=(("nope", CITATION),)), "a column that is not in the saved header"),
                (dict(subjects=("nope",), states=(("reported_state", CITATION),)),
                 "a subject column that is not in the saved header"),
                (dict(), "nothing selected at all"),
                (dict(window_us=1000000), "a window with no declared expectation"),
                (dict(window_us=300000, expected_interval_us=200000),
                 "a window that is not a multiple of the expected interval"),
                (dict(window_us=1000000, expected_interval_us=0), "a zero expected interval"),
                (dict(recording_end_us=5000000), "a recording end with no window at all")):
            with self.assertRaises(ValueError, msg=why):
                convert(good, clock_basis=CLOCK, **kwargs)
        with self.assertRaises(ValueError):  # the clock is the caller's word, and it is required
            convert(good, clock_basis="", states=(("reported_state", CITATION),))
        for broken, why in (
                (saved([(1000, "a", "UP"), (0, "a", "DOWN")]), "a saved time that decreases"),
                (saved([(0, "a", "UP")], header="record_time_us,subject_id,record_time_us"),
                 "a duplicate saved column name"),
                (saved([("-1", "a", "UP")]), "a negative saved time"),
                (b"record_time_us,subject_id,reported_state\n", "a file with no rows")):
            with self.assertRaises(ValueError, msg=why):
                convert(broken, clock_basis=CLOCK, subjects=("subject_id",),
                        states=(("reported_state", CITATION),))

    def test_a_saved_gnss_source_reaches_a_fix_status_transition_through_the_adopted_converter(self):
        """Authored Septentrio SBF, the adopted converter's own CSV, then this bridge.

        This is the adapter-chain proof for the GNSS transition function: the CSV is written by
        `scripts/convert_sbf_records.py` at the pin its case already carries, not typed by hand.
        """
        from tests.test_sbf_records import block  # authored blocks at the case's own pin
        citation = "sbf-records/README.md: PVTGeodetic Mode as transmitted, value table not established"
        captures = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for index, (mode, moment) in enumerate(((4, 1000000), (0, 2000000))):
                saved_block = base / f"capture{index}.sbf"
                saved_block.write_bytes(block(mode=mode, sentinel=mode == 0))
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_sbf_records.py"),
                    str(saved_block), str(base / f"out{index}"), "--capture-time-us", str(moment)],
                    check=True, capture_output=True)
                captures.append((base / f"out{index}/observations.csv").read_text())
            chained = (captures[0] + captures[1].split("\n", 1)[1]).encode()
            output, report = convert(chained, clock_basis="us_caller_capture_clock_of_the_sbf_export",
                                     subjects=("sbf_block_id",),
                                     states=(("sbf_mode_code", citation),))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([(r["record_time_us"], unhex(r["reported_state_hex"]),
                               r["transition_disposition"]) for r in rows],
                             [("1000000", "4", "FIRST_REPORTED_VALUE"),
                              ("2000000", "0", "CHANGE_BETWEEN_TWO_REPORTED_VALUES")])
            self.assertEqual(unhex(rows[1]["previous_state_hex"]), "4")
            self.assertEqual(subject(rows[1]["subject_hex"]), ["4007"])
            self.assertEqual(report["reported_value_changes"], 1)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                target = base / "transitions.csv"
                target.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(target), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                fields = common["observations"][1]["fields"]
                self.assertEqual(fields["transition_disposition"],
                                 "CHANGE_BETWEEN_TWO_REPORTED_VALUES")
                self.assertEqual(unhex(fields["state_column_hex"]), "sbf_mode_code")
                self.assertIn("value table not established", unhex(fields["state_meaning_hex"]))

    def test_the_transitions_and_windows_reach_common_output(self):
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output proof is the "
                          "point of this test, so a missing reader is an explicit skip and not a pass")
        data = saved([(0, "gps-1", "3D_FIX"), (500000, "gps-1", "NO_FIX"),
                      (1000000, "gps-1", "NO_FIX")])
        output, _ = convert(data, clock_basis=CLOCK, subjects=("subject_id",),
                            states=(("reported_state", CITATION),),
                            window_us=1000000, expected_interval_us=250000)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observations.csv"
            source.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(source), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        kinds = [o["fields"]["event_kind"] for o in common["observations"]]
        self.assertEqual(kinds.count("STATE_TRANSITION"), 2)
        self.assertEqual(kinds.count("OBSERVATION_WINDOW"), 2)
        change = next(o["fields"] for o in common["observations"]
                      if o["fields"]["transition_disposition"] == "CHANGE_BETWEEN_TWO_REPORTED_VALUES")
        self.assertEqual(unhex(change["reported_state_hex"]), "NO_FIX")
        self.assertEqual(unhex(change["previous_state_hex"]), "3D_FIX")
        self.assertEqual(unhex(change["state_meaning_hex"]), CITATION)
        self.assertEqual(common["platform_domain"], "Unknown")
        units = common["profile_units"]
        self.assertIn("never_read_as_a_state", units["previous_state_hex"])
        self.assertIn("not_packet_loss", units["observation_rate_basis"])
        self.assertIn("never_maps_ranks_or_compares_the_words_itself", units["state_meaning_hex"])


if __name__ == "__main__":
    unittest.main()
