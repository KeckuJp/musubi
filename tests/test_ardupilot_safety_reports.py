"""Authored ArduPilot MODE exports in the producer's normal shape; no vehicle, link or log.

Every record carries the columns the adopted route requires — `TimeUS,Mode,ModeNum,Rsn` — because
that is what a real export prints; nothing here is shortened to a convenient subset. Reason meaning
is pinned to ArduPilot Plane 4.5.5 `d6ebfc21844e2bc0ab28c8c688b8dae94abccf5a`
`libraries/AP_Vehicle/ModeReason.h`. A passing test proves code and schema behaviour only — never a
real vehicle, a real flight or a physical diagnosis.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_ardupilot_safety_reports import MODE_REASONS, classify, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/safety-reports/profile.toml"
SCRIPT = ROOT / "scripts/convert_ardupilot_safety_reports.py"
HEADER = "TimeUS,Mode,ModeNum,Rsn"


def export(*records, header=HEADER):
    """A saved MODE export: the producer's header, then one record per line."""
    return header + "\n" + "".join(f"{line}\n" for line in records)


def record(time_us, reason, *, mode="RTL", mode_num=11):
    return f"{time_us},{mode},{mode_num},{reason}"



def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class ArduPilotSafetyReportTests(unittest.TestCase):
    def rows_of(self, text, **kwargs):
        kwargs.setdefault("vehicle", "plane-4.5.5")
        output, report = convert(text, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report, output


    def test_a_fence_breach_and_each_named_failsafe_are_reported_with_their_kind(self):
        rows, report, _ = self.rows_of(export(
            record(1_000_000, 10),   # FENCE_BREACHED
            record(2_000_000, 4),    # BATTERY_FAILSAFE
            record(3_000_000, 3),    # RADIO_FAILSAFE
            record(4_000_000, 6),    # EKF_FAILSAFE
        ))
        self.assertEqual((report["geofence_breach_reports"], report["failsafe_reports"]), (1, 3))
        fence, battery, radio, ekf = rows
        self.assertEqual(fence["reported_mode_reason"], "FENCE_BREACHED")
        self.assertEqual(fence["safety_domain"], "GEOFENCE")
        self.assertEqual(fence["reported_geofence_report"], "BREACH_REPORTED")
        self.assertEqual(fence["reported_failsafe_kind"], "")
        self.assertIn("NO_BOUNDARY_NO_FENCE_TYPE_AND_NO_POSITION", fence["report_basis"])
        self.assertEqual([r["reported_failsafe_kind"] for r in (battery, radio, ekf)],
                         ["BATTERY", "RADIO", "EKF"])
        self.assertTrue(all(r["safety_domain"] == "FAILSAFE" for r in (battery, radio, ekf)))
        self.assertEqual(fence["record_time_us"], "1000000")
        self.assertEqual(unhex(fence["original_record_hex"]), "1000000,RTL,11,10")
        self.assertEqual(fence["producer_pin"], "ardupilot-plane-4.5.5-d6ebfc21")
        self.assertIn("NOT_THAT_THE_UNDERLYING_THING_FAILED", battery["report_basis"])
        self.assertIn("NO_QUANTITY_IS_EMITTED", fence["quantity_basis"])
        self.assertFalse([name for name in fence if "threshold" in name or "distance" in name])

    def test_a_command_is_never_read_as_a_safety_event(self):
        rows, report, _ = self.rows_of(export(
            *(record(1_000_000 + index, code) for index, code in enumerate((1, 2, 30, 32, 41, 42, 52)))
        ))
        self.assertEqual(report["commanded_changes"], 7)
        self.assertEqual((report["geofence_breach_reports"], report["failsafe_reports"]), (0, 0))
        for row in rows:
            self.assertEqual(row["reason_class"], "COMMANDED_MODE_CHANGE")
            self.assertEqual(row["safety_domain"], "", "a command carries no safety domain")
            self.assertEqual(row["reported_failsafe_kind"], "")
            self.assertEqual(row["reported_geofence_report"], "")
            self.assertIn("NEVER_READ_AS_AN_OCCURRENCE", row["report_basis"])

    def test_a_recovery_or_return_is_not_a_breach_or_a_failsafe_entry(self):
        rows, report, _ = self.rows_of(export(
            record(1_000_000, 43),   # FENCE_RETURN_PREVIOUS_MODE
            record(2_000_000, 48),   # RADIO_FAILSAFE_RECOVERY
            record(3_000_000, 15),   # AVOIDANCE_RECOVERY
        ))
        self.assertEqual(report["recovery_reports"], 3)
        self.assertEqual((report["geofence_breach_reports"], report["failsafe_reports"]), (0, 0))
        fence_return, radio_recovery, avoidance = rows
        self.assertEqual(fence_return["reported_geofence_report"],
                         "RETURN_TO_PREVIOUS_MODE_REPORTED")
        self.assertEqual(fence_return["safety_domain"], "GEOFENCE")
        self.assertEqual(radio_recovery["safety_domain"], "FAILSAFE")
        self.assertEqual(radio_recovery["reported_failsafe_kind"], "",
                         "a recovery names no failsafe entry kind")
        self.assertEqual(avoidance["safety_domain"], "")
        for row in rows:
            self.assertEqual(row["reason_class"], "RECOVERY_OR_RETURN_REPORTED")
            self.assertIn("NEVER_COUNTED_AS_A_BREACH", row["report_basis"])

    def test_the_general_failsafe_is_not_promoted_and_the_sources_unknowns_stay_unknown(self):
        rows, report, _ = self.rows_of(export(
            record(1_000_000, 25),   # FAILSAFE - the source calls this the general one
            record(2_000_000, 0),    # UNKNOWN
            record(3_000_000, 33),   # UNAVAILABLE
            record(4_000_000, 200),  # outside the pinned enum
        ))
        general, unknown, unavailable, unmapped = rows
        self.assertEqual(general["reported_failsafe_kind"], "UNSPECIFIED_GENERAL")
        self.assertIn("SPECIFIC_ONES_ARE_PREFERRED", general["report_basis"])
        self.assertEqual((unknown["reported_mode_reason"], unavailable["reported_mode_reason"]),
                         ("UNKNOWN", "UNAVAILABLE"))
        self.assertEqual(report["source_declared_unknown"], 2)
        for row in (unknown, unavailable):
            self.assertEqual(row["reason_class"], "SOURCE_DECLARED_UNKNOWN")
            self.assertEqual(row["safety_domain"], "")
        self.assertEqual(unmapped["reported_mode_reason"], "UNKNOWN_CODE_200")
        self.assertEqual(unmapped["reason_class"], "UNMAPPED_CODE_NO_MEANING_ASSIGNED")
        self.assertEqual(unmapped["safety_domain"], "")
        self.assertEqual(report["unmapped_codes"], 1)
        for code in MODE_REASONS:
            self.assertNotEqual(classify(code)[0], "UNMAPPED_CODE_NO_MEANING_ASSIGNED")
        for code in (53, 99, 255):
            self.assertEqual(classify(code), ("UNMAPPED_CODE_NO_MEANING_ASSIGNED", "", "", "", ""))


    def test_an_export_that_is_not_this_producers_shape_is_refused(self):
        for text, why in (
                ("", "an empty export"),
                (export(header="TimeUS,Mode,ModeNum"), "the required Rsn column missing"),
                (export(header="TimeUS,Mode,Rsn"), "the required ModeNum column missing"),
                (export(record(1_000_000, 300)), "a reason outside the producer's uint8 domain"),
                (export(record(1_000_000, -1)), "a negative reason"),
                (export(record(1_000_000, "FENCE")), "a non-numeric reason"),
                (export("1000000,RTL,,10"), "an empty mode number"),
                (export(header=HEADER), "a header with no record"),
        ):
            with self.assertRaises(ValueError, msg=why):
                self.rows_of(text)
        with self.assertRaises(ValueError):
            self.rows_of(export(record(1_000_000, 10)), vehicle="plane-9.9.9")
        with self.assertRaises(ValueError):
            self.rows_of(export(record(1_000_000, 10)), source_format="dataflash-binary")

    def test_every_field_of_the_saved_record_is_retained_including_unknown_ones(self):
        """The retained record is the saved record, not a rebuilt subset of the columns read.

        A real export carries columns this converter does not interpret. Rebuilding the record from
        the required columns would drop them silently and would re-quote what the writer wrote, so
        the exact characters of the saved record are retained instead.
        """
        rows, report, _ = self.rows_of(export(
            "1000000,RTL,11,10,SomethingNew,42",
            header="TimeUS,Mode,ModeNum,Rsn,Unknown,AlsoUnknown"))
        self.assertEqual(report["geofence_breach_reports"], 1)
        self.assertEqual(unhex(rows[0]["original_record_hex"]),
                         "1000000,RTL,11,10,SomethingNew,42")
        self.assertEqual(rows[0]["original_record_columns"], "6")

    def test_a_quoted_record_keeps_the_writers_own_characters(self):
        line = '1000000,"RTL, with a comma",11,10,"say ""hi"""'
        rows, report, _ = self.rows_of(export(line, header="TimeUS,Mode,ModeNum,Rsn,Note"))
        self.assertEqual(report["geofence_breach_reports"], 1)
        self.assertEqual(unhex(rows[0]["original_record_hex"]), line)
        self.assertEqual(rows[0]["original_record_columns"], "5")
        self.assertEqual(rows[0]["reported_mode_reason"], "FENCE_BREACHED")

    def test_broken_quoting_is_refused_rather_than_silently_repaired(self):
        """The default `csv` reader repairs broken quoting instead of reporting it.

        With the default settings it reads an unterminated `"cell` as `cell` and `"a"x` as `ax`, so
        the cells it hands back are not the characters the writer wrote. This writer cannot produce
        either shape, so the record is refused.
        """
        for line, why in (
                ('1000000,RTL,11,10,"unterminated', "an unterminated quote at end of record"),
                ('1000000,RTL,11,10,"a"x', "a character after a closing quote"),
        ):
            with self.assertRaises(ValueError, msg=why) as refusal:
                self.rows_of(export(line, header=HEADER + ",Note"))
            self.assertIn("broken quoting", str(refusal.exception))
        rows, _, _ = self.rows_of(export('1000000,RTL,11,10,"ok"', header=HEADER + ",Note"))
        self.assertEqual(unhex(rows[0]["original_record_hex"]), '1000000,RTL,11,10,"ok"')

    def test_an_ambiguous_header_or_a_ragged_record_is_refused_rather_than_lost(self):
        with self.assertRaises(ValueError) as duplicate:
            self.rows_of(export("1000000,RTL,11,10,99",
                                header="TimeUS,Mode,ModeNum,Rsn,Rsn"))
        self.assertIn("repeats a column name", str(duplicate.exception))
        for line, why in (("1000000,RTL,11", "one cell short"),
                          ("1000000,RTL,11,10,extra", "one cell too many"),
                          ("", "an empty record line")):
            with self.assertRaises(ValueError, msg=why) as ragged:
                self.rows_of(export(line))
            self.assertIn("ragged record", str(ragged.exception))
        with self.assertRaises(ValueError):
            self.rows_of(export(record(1_000_000, 10), header="TimeUS,Mode,ModeNum, Rsn"))

    def test_only_ascii_decimal_is_read_as_the_producers_number(self):
        for token, why in (("١٢٣", "Arabic-Indic digits int() would accept as 123"),
                           ("²", "a superscript digit int() refuses"),
                           ("+10", "a signed token"), ("1_0", "an underscore separator"),
                           (" 10", "a padded token"), ("0x0a", "a hexadecimal token")):
            with self.assertRaises(ValueError, msg=why):
                self.rows_of(export(record(1_000_000, token)))
        with self.assertRaises(ValueError):
            self.rows_of(export(record(1_000_000, 10, mode_num=256)))
        with self.assertRaises(ValueError):
            self.rows_of(export(record(2 ** 63, 10)))
        rows, _, _ = self.rows_of(export(record(2 ** 63 - 1, 10, mode_num=255)))
        self.assertEqual(rows[0]["record_time_us"], str(2 ** 63 - 1))

    def test_a_vehicle_version_the_enum_was_not_read_at_keeps_the_raw_code_unread(self):
        """The pin is Plane 4.5.5; another selector names a release whose tree was not read."""
        rows, report, _ = self.rows_of(export(record(1_000_000, 10), record(2_000_000, 4)),
                                       vehicle="copter-4.5.5")
        self.assertEqual(report["unqualified_vehicle_records"], 2)
        self.assertEqual((report["geofence_breach_reports"], report["failsafe_reports"]), (0, 0))
        for row, code in zip(rows, ("10", "4")):
            self.assertEqual(row["reported_mode_reason_code"], code, "the raw code is preserved")
            self.assertEqual(row["reported_mode_reason"],
                             "NOT_INTERPRETED_AT_THIS_DECLARED_VEHICLE_VERSION")
            self.assertEqual(row["reason_class"], "UNQUALIFIED_VEHICLE_VERSION_NOT_INTERPRETED")
            self.assertEqual((row["safety_domain"], row["reported_failsafe_kind"],
                              row["reported_geofence_report"]), ("", "", ""))
            self.assertIn("DIFFERENT_RELEASE_WHOSE_TREE_WAS_NOT_READ", row["report_basis"])
            self.assertNotIn("plane-4.5.5", row["producer_pin"])
        self.assertNotIn("plane-4.5.5", report["reason_interpretation"])


    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_both_roles_reach_common_observations(self):
        _, _, output = self.rows_of(export(
            record(1_000_000, 10) + ",extra-field",   # geofence breach, with an uninterpreted column
            record(2_000_000, 4) + ',"note, quoted"',  # battery failsafe, with the writer's quoting
            record(3_000_000, 1) + ",",              # a pilot command, which is neither
            header=HEADER + ",Note"))
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(source),
                 "--allow-equal-time"], capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        self.assertEqual(common["platform_domain"], "Unknown", "no platform is inferred from a log")
        self.assertTrue(all(o["clock_basis"] == "BootRelative" for o in common["observations"]))
        self.assertEqual(common["observations"][0]["t_ms"], 1000)
        fence, battery, command = (o["fields"] for o in common["observations"])
        self.assertEqual(fence["reported_geofence_report"], "BREACH_REPORTED")
        self.assertEqual(fence["original_record_columns"], 5)
        self.assertEqual(unhex(fence["original_record_hex"]), "1000000,RTL,11,10,extra-field")
        self.assertEqual(unhex(battery["original_record_hex"]),
                         '2000000,RTL,11,4,"note, quoted"')
        self.assertEqual(battery["reported_failsafe_kind"], "BATTERY")
        self.assertEqual(command["reason_class"], "COMMANDED_MODE_CHANGE")
        units = common["profile_units"]
        self.assertIn("not_a_geometry_evaluation", units["report_basis"])
        self.assertIn("absence_of_a_row_is_not_evidence", units["coverage_basis"])
        self.assertIn("never_a_boundary", units["reported_geofence_report"])
        self.assertIn("never_rebuilt_from_parsed_cells", units["original_record_hex"])

    def test_the_cli_writes_its_accounting_and_never_overwrites(self):
        text = export(record(1_000_000, 10), record(2_000_000, 1))
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            (base / "mode.csv").write_text(text)
            command = [os.sys.executable, str(SCRIPT), str(base / "mode.csv"), str(base / "out"),
                       "--vehicle", "plane-4.5.5", "--source-format", "ardupilot-mode-csv"]
            subprocess.run(command, check=True, capture_output=True)
            report = json.loads((base / "out/report.json").read_text())
            self.assertEqual((report["geofence_breach_reports"], report["commanded_changes"]), (1, 1))
            self.assertIn("absence", report["coverage_basis"].lower())
            self.assertEqual((base / "out/source.csv").read_text(), text)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
