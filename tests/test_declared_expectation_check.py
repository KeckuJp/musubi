"""Authored inputs; the mode chain runs the adopted converter itself. No device, no real log."""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_declared_expectation_check import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/declared-expectation-checks/profile.toml"
MODE_CITATION = "ardupilot-mode/README.md: reported plane mode name, not a command or a cause"
PROGRESS_CITATION = "nav2-passive-progress/README.md: pinned GoalStatus and reported goal progress"
CLOCK = "us_since_boot_declared_by_the_source_profile"


def saved(rows, header="record_time_us,subject_id,reported_state"):
    return (header + "\n" + "".join(",".join(str(cell) for cell in row) + "\n"
                                    for row in rows)).encode()


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


try:  # the adopted ROS route's own dependency; absent here means an explicit skip, never a pass
    import rosbags  # noqa: F401
    ROSBAGS = True
except ImportError:
    ROSBAGS = False


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeclaredExpectationCheckTests(unittest.TestCase):
    def test_a_saved_mode_is_compared_with_the_declared_expectation_through_the_adopted_chain(self):
        """Authored ArduPilot records, the adopted normaliser's own CSV, then this comparison."""
        normalise = load(ROOT / "scripts/normalize_ardupilot_report.py")
        records = [dict(meta=dict(type="MODE"), data=dict(TimeUS=1000, Mode=10, ModeNum=10, Rsn=1)),
                   dict(meta=dict(type="MODE"), data=dict(TimeUS=2000, Mode=0, ModeNum=0, Rsn=2)),
                   dict(meta=dict(type="MODE"), data=dict(TimeUS=3000, Mode=10, ModeNum=10, Rsn=1))]
        raw = json.dumps(dict(value_basis=normalise.BASIS, source_records=records)).encode()
        produced, _ = normalise.convert(raw, mode_vehicle="plane")
        names = [row["reported_mode_name"] for row in csv.DictReader(io.StringIO(produced))]
        self.assertEqual(names, ["AUTO", "MANUAL", "AUTO"])  # the adopted converter's own words
        output, report = convert(produced.encode(), clock_basis=CLOCK, time_column="TimeUS",
                                 value_meaning=MODE_CITATION,
                                 membership_column="reported_mode_name",
                                 expected_values=("AUTO", "GUIDED"))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(r["record_time_us"], unhex(r["reported_value_hex"]),
                           r["expectation_disposition"]) for r in rows],
                         [("1000", "AUTO", "WITHIN_DECLARED_EXPECTATION"),
                          ("2000", "MANUAL", "OUTSIDE_DECLARED_EXPECTATION"),
                          ("3000", "AUTO", "WITHIN_DECLARED_EXPECTATION")])
        self.assertEqual(json.loads(unhex(rows[1]["declared_expectation_hex"])), ["AUTO", "GUIDED"])
        self.assertEqual(unhex(rows[1]["value_meaning_hex"]), MODE_CITATION)
        self.assertIn("NOT_A_DIAGNOSIS", rows[1]["expectation_basis"])
        self.assertEqual((report["within_declared_expectation"],
                          report["outside_declared_expectation"]), (2, 1))
        self.assertEqual(report["physical_cause_records"], "NOT_DERIVED")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "modes.csv").write_text(produced)
            subprocess.run([os.sys.executable,
                str(ROOT / "scripts/convert_declared_expectation_check.py"),
                str(base / "modes.csv"), str(base / "out"), "--clock-basis", CLOCK,
                "--time-column", "TimeUS", "--value-meaning", MODE_CITATION,
                "--membership-column", "reported_mode_name",
                "--expected-value", "AUTO", "--expected-value", "GUIDED"],
                check=True, capture_output=True)
            self.assertEqual((base / "out/observations.csv").read_text(), output)
            if not os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output half of "
                              "this test is the point, so a missing reader is an explicit skip")
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(base / "out/observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        fields = common["observations"][1]["fields"]
        self.assertEqual(fields["expectation_disposition"], "OUTSIDE_DECLARED_EXPECTATION")
        self.assertEqual(fields["check_kind"], "DECLARED_VALUE_MEMBERSHIP")
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertIn("may_simply_be_incomplete", common["profile_units"]["expectation_basis"])

    def test_declared_progress_says_whether_it_moved_and_never_calls_it_a_stall(self):
        header = "record_time_us,action_uuid_hex,mission_current_sequence"
        output, report = convert(
            saved([(1000, "hex:aa", 3), (2000, "hex:aa", 3), (3000, "hex:aa", 4),
                   (4000, "hex:aa", ""), (5000, "hex:aa", 5), (6000, "hex:bb", 9),
                   (7000, "hex:aa", 2)], header=header),
            clock_basis=CLOCK, value_meaning=PROGRESS_CITATION,
            advance_column="mission_current_sequence", subjects=("action_uuid_hex",))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(r["expectation_disposition"], r["advance_delta"], r["elapsed_us"])
                          for r in rows],
                         [("FIRST_REPORTED_VALUE", "", ""), ("UNCHANGED", "0", "1000"),
                          ("ADVANCED", "1", "1000"), ("NOT_REPORTED", "", ""),
                          ("PREVIOUS_VALUE_NOT_REPORTED", "", ""),
                          ("FIRST_REPORTED_VALUE", "", ""), ("DECREASED", "-3", "2000")])
        self.assertEqual(json.loads(unhex(rows[5]["subject_hex"])), ["hex:bb"])
        self.assertEqual(unhex(rows[6]["previous_value_hex"]), "5")
        self.assertEqual((report["advanced_records"], report["unchanged_records"],
                          report["decreased_records"], report["values_not_reported"],
                          report["values_after_an_unreported_one"]), (1, 1, 1, 1, 1))
        self.assertEqual(report["checked_records"], 7)

    @unittest.skipUnless(ROSBAGS, "rosbags is not installed; the adopted Nav2 chain is the point of "
                                  "this test, so a missing dependency is an explicit skip")
    def test_a_saved_nav2_goal_status_establishes_a_reported_incomplete_state(self):
        """The adopted ROS converter, read-only, then this comparison. Its file is not edited."""
        from tests.test_nav2_progress import status, feedback, feedback_definition, GOAL_A, GOAL_B
        from tests.test_rosbag_observations import bag
        from scripts.convert_rosbag_observations import NAV2_FEEDBACK
        from scripts.convert_rosbag_observations import convert as ros_convert
        citation = ("nav2-passive-progress/README.md and action_msgs GoalStatus: EXECUTING is a "
                    "reported in-progress goal and ABORTED a reported terminal non-success")
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "bag"
            bag(source, messages=[feedback(GOAL_A),
                                  status((GOAL_A, 2, (7, 500)), (GOAL_B, 4, (7, 600))),
                                  status((GOAL_A, 6, (7, 700)))],
                msgdefs={NAV2_FEEDBACK: feedback_definition()})
            produced, ros_report = ros_convert(source)
            self.assertEqual([row["action_status_reported"]
                              for row in csv.DictReader(io.StringIO(produced))],
                             ["", "EXECUTING_REPORTED", "SUCCEEDED_REPORTED", "ABORTED_REPORTED"])
            output, report = convert(produced.encode(), clock_basis="us_bag_record_time_not_utc",
                                     value_meaning=citation,
                                     membership_column="action_status_reported",
                                     expected_values=("SUCCEEDED_REPORTED",),
                                     subjects=("action_uuid_hex",))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([(unhex(r["reported_value_hex"]), r["expectation_disposition"])
                              for r in rows],
                             [("", "NOT_REPORTED"),
                              ("EXECUTING_REPORTED", "OUTSIDE_DECLARED_EXPECTATION"),
                              ("SUCCEEDED_REPORTED", "WITHIN_DECLARED_EXPECTATION"),
                              ("ABORTED_REPORTED", "OUTSIDE_DECLARED_EXPECTATION")])
            self.assertEqual((report["within_declared_expectation"],
                              report["outside_declared_expectation"],
                              report["values_not_reported"]), (1, 2, 1))
            self.assertEqual(json.loads(unhex(rows[1]["subject_hex"])), ["hex:" + GOAL_A.hex()])
            self.assertEqual(json.loads(unhex(rows[2]["subject_hex"])), ["hex:" + GOAL_B.hex()])
            self.assertEqual(unhex(rows[1]["value_meaning_hex"]), citation)
            self.assertEqual(ros_report["clock"], "Unknown")
            if not os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output half of "
                              "this test is the point, so a missing reader is an explicit skip")
            target = Path(work) / "checks.csv"
            target.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(target), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 4)
        self.assertEqual([o["fields"]["expectation_disposition"] for o in common["observations"]],
                         ["NOT_REPORTED", "OUTSIDE_DECLARED_EXPECTATION",
                          "WITHIN_DECLARED_EXPECTATION", "OUTSIDE_DECLARED_EXPECTATION"])

    def test_two_columns_the_caller_declares_share_a_unit_are_compared_with_a_declared_tolerance(self):
        citation = "ardupilot-mode/imu-profile.toml: ATT.DesRoll and ATT.Roll are both declared deg"
        header = "record_time_us,ATT.Roll,ATT.DesRoll"
        output, report = convert(saved([(1000, "2.5", "2.0"), (2000, "-14.0", "3.0"),
                                        (3000, "", "3.0")], header=header),
                                 clock_basis=CLOCK, value_meaning=citation,
                                 difference_columns=("ATT.Roll", "ATT.DesRoll"), tolerance="5")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(r["expectation_disposition"], r["difference_value"],
                           r["declared_tolerance"]) for r in rows],
                         [("WITHIN_DECLARED_TOLERANCE", "0.5", "5"),
                          ("BEYOND_DECLARED_TOLERANCE", "-17.0", "5"),
                          ("NOT_REPORTED", "", "5")])  # one side absent is neither answer
        self.assertEqual(json.loads(unhex(rows[1]["checked_column_hex"])),
                         ["ATT.Roll", "ATT.DesRoll"])
        self.assertEqual(json.loads(unhex(rows[1]["reported_value_hex"])), ["-14.0", "3.0"])
        self.assertIn("NOT_A_TRACKING_ERROR", rows[1]["expectation_basis"])
        self.assertEqual((report["within_declared_tolerance"], report["beyond_declared_tolerance"],
                          report["values_not_reported"]), (1, 1, 1))
        for kwargs, why in (
                (dict(difference_columns=("ATT.Roll",), tolerance="5"), "only one column"),
                (dict(difference_columns=("ATT.Roll", "ATT.Roll"), tolerance="5"), "the same column twice"),
                (dict(difference_columns=("ATT.Roll", "ATT.DesRoll")), "no declared tolerance"),
                (dict(difference_columns=("ATT.Roll", "ATT.DesRoll"), tolerance="-1"),
                 "a negative tolerance"),
                (dict(difference_columns=("ATT.Roll", "nope"), tolerance="5"),
                 "a column not in the saved header")):
            with self.assertRaises(ValueError, msg=why):
                convert(saved([(1000, "1", "1")], header=header), clock_basis=CLOCK,
                        value_meaning=citation, **kwargs)
        with self.assertRaises(ValueError):  # a non-numeric cell is refused, never string-compared
            convert(saved([(1000, "left", "1")], header=header), clock_basis=CLOCK,
                    value_meaning=citation, difference_columns=("ATT.Roll", "ATT.DesRoll"),
                    tolerance="5")

    def test_a_difference_inside_the_supported_range_is_exact_and_beyond_it_is_refused(self):
        """The default decimal context would round this to 1 and call it within tolerance."""
        header = "record_time_us,left,right"
        def compared(left, right, tolerance="1"):
            return convert(saved([(1000, left, right)], header=header), clock_basis=CLOCK,
                           value_meaning="case.md: both sides declared in the same unit",
                           difference_columns=("left", "right"), tolerance=tolerance)
        output, _ = compared("1.00000000000000000000000000001", "0")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual((row["expectation_disposition"], row["difference_value"]),
                         ("BEYOND_DECLARED_TOLERANCE", "1.00000000000000000000000000001"))
        row = next(csv.DictReader(io.StringIO(compared("1.000000000000000000000000000", "0")[0])))
        self.assertEqual((row["expectation_disposition"], row["difference_value"]),
                         ("WITHIN_DECLARED_TOLERANCE", "1.000000000000000000000000000"))
        row = next(csv.DictReader(io.StringIO(
            compared("9" * 30 + "." + "9" * 30, "0", tolerance="9" * 30)[0])))
        self.assertEqual((row["expectation_disposition"], row["difference_value"]),
                         ("BEYOND_DECLARED_TOLERANCE", "9" * 30 + "." + "9" * 30))
        for value, why in (("1E+999999999", "an enormous finite exponent"),
                           ("1e-999999999", "a tiny exponent"),
                           ("nan", "a not-a-number literal"), ("Infinity", "an infinity"),
                           ("1" * 31, "more integer digits than the supported range"),
                           ("1." + "2" * 31, "more fraction digits than the supported range")):
            with self.assertRaises(ValueError, msg=why) as refusal:
                compared(value, "0")
            self.assertIn("plain decimal with at most 30", str(refusal.exception))
        with self.assertRaises(ValueError):  # the tolerance is held to the same stated range
            compared("1", "0", tolerance="1E+9")

    def test_what_it_cannot_supply_itself_is_refused(self):
        good = saved([(1000, "a", "AUTO"), (2000, "a", "MANUAL")])
        base = dict(clock_basis=CLOCK, value_meaning=MODE_CITATION)
        for kwargs, why in (
                (dict(), "no check declared at all"),
                (dict(membership_column="reported_state", expected_values=("AUTO",),
                      advance_column="reported_state"), "both checks declared at once"),
                (dict(membership_column="reported_state"), "an empty expected set"),
                (dict(membership_column="reported_state", expected_values=("AUTO", "AUTO")),
                 "a repeated expected value"),
                (dict(membership_column="nope", expected_values=("AUTO",)),
                 "a column that is not in the saved header"),
                (dict(advance_column="reported_state", expected_values=("AUTO",)),
                 "expected values on an advance check"),
                (dict(advance_column="reported_state"), "a progress column that is not an integer"),
                (dict(membership_column="reported_state", expected_values=("AUTO",), tolerance="1"),
                 "a tolerance on a membership check"),
                (dict(membership_column="reported_state", expected_values=("AUTO",),
                      subjects=("nope",)), "a subject column that is not in the saved header")):
            with self.assertRaises(ValueError, msg=why):
                convert(good, **dict(base, **kwargs))
        with self.assertRaises(ValueError):  # a name is not a meaning
            convert(good, clock_basis=CLOCK, value_meaning="",
                    membership_column="reported_state", expected_values=("AUTO",))
        with self.assertRaises(ValueError):  # the clock is the caller's word, and it is required
            convert(good, clock_basis="", value_meaning=MODE_CITATION,
                    membership_column="reported_state", expected_values=("AUTO",))
        for broken, why in (
                (saved([(2000, "a", "AUTO"), (1000, "a", "AUTO")]), "a saved time that decreases"),
                (saved([(1000, "a", "AUTO")], header="record_time_us,subject_id,record_time_us"),
                 "a duplicate saved column name"),
                (saved([(1000, "", "AUTO")]), "a missing declared identity component"),
                (b"record_time_us,subject_id,reported_state\n", "a file with no rows")):
            with self.assertRaises(ValueError, msg=why):
                convert(broken, clock_basis=CLOCK, value_meaning=MODE_CITATION,
                        membership_column="reported_state", expected_values=("AUTO",),
                        subjects=("subject_id",))
        wide, report = convert(saved([(1000, "a", "V" * 60000)]), clock_basis=CLOCK,
                               value_meaning=MODE_CITATION, membership_column="reported_state",
                               expected_values=("AUTO",))
        self.assertEqual(report["output_bytes"], len(wide.encode("utf-8")))
        self.assertGreater(len(wide.encode("utf-8")), 120000)
        with self.assertRaises(ValueError) as unparsed:
            convert(saved([(1000, "a", "V" * 200000)]), clock_basis=CLOCK,
                    value_meaning=MODE_CITATION, membership_column="reported_state",
                    expected_values=("AUTO",))
        self.assertIn("cannot be parsed as written", str(unparsed.exception))


if __name__ == "__main__":
    unittest.main()
