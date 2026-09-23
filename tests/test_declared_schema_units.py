"""Authored expectation documents and saved read reports; no reader is run and no artefact is read.

The "actual" side is a saved read report in the shape the adopted common reader emits. Nothing here
opens a device, a log or a network, and a passing test proves code and schema behaviour only.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.check_declared_schema_units import compare_unit, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/schema-unit-checks/profile.toml"
SCRIPT = ROOT / "scripts/check_declared_schema_units.py"
CAPTURE = 1_789_000_000_000_000
ARTEFACT_DIGEST = "sha256:" + "a1" * 32
PROFILE_DIGEST = "sha256:" + "b2" * 32


def read_report(columns, units, *, profile_id="rover_pack_v1", version="1"):
    """A saved read report in the shape the adopted reader emits."""
    return json.dumps({
        "source_columns": list(columns), "profile_units": dict(units),
        "profile_identity": {"profile_id": profile_id, "declared_version": version,
                             "source_sha256": PROFILE_DIGEST},
        "input_sha256": ARTEFACT_DIGEST, "main_rows": 3, "source_role": "recorded_export",
        "platform_domain": "Unknown", "observations": []})


def expectation(columns, **extra):
    return json.dumps({"columns": dict(columns), **extra})


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class DeclaredSchemaUnitTests(unittest.TestCase):
    def rows_of(self, expected, actual, **kwargs):
        kwargs.setdefault("capture_time_us", CAPTURE)
        output, report = convert(expected, actual, **kwargs)
        rows = list(csv.DictReader(io.StringIO(output)))
        return rows, report, output

    def subject(self, rows, kind, name):
        found = [r for r in rows if r["check_kind"] == kind and unhex(r["check_subject_hex"]) == name]
        self.assertEqual(len(found), 1, f"exactly one {kind} row for {name}")
        return found[0]


    def test_a_matching_declaration_reports_a_match_on_every_subject(self):
        rows, report, _ = self.rows_of(
            expectation({"speed": "m/s", "pack_voltage": "V"}, profile_id="rover_pack_v1",
                        declared_version="1"),
            read_report(["speed", "pack_voltage"], {"speed": "m/s", "pack_voltage": "V"}))
        self.assertEqual((report["schema_mismatches"], report["unit_mismatches"],
                          report["identity_mismatches"], report["missing_evidence"]), (0, 0, 0, 0))
        self.assertTrue(all(r["disposition"] == "MATCHES_DECLARED_EXPECTATION" for r in rows))
        unit = self.subject(rows, "COLUMN_UNIT", "speed")
        self.assertEqual((unhex(unit["expected_declared_hex"]), unhex(unit["actual_declared_hex"])),
                         ("m/s", "m/s"))
        self.assertEqual(unit["evidence_state"], "BOTH_SIDES_DECLARED")
        self.assertEqual(unit["artefact_input_sha256"], ARTEFACT_DIGEST)
        self.assertEqual(unit["profile_source_sha256"], PROFILE_DIGEST)
        self.assertIn("NEVER_A_UNIT_INFERRED_FROM_A_COLUMN_NAME", unit["comparison_basis"])


    def test_a_scale_difference_and_a_quantity_kind_difference_are_named_apart(self):
        rows, report, _ = self.rows_of(
            expectation({"range": "m", "pack": "V", "probe": "K"}),
            read_report(["range", "pack", "probe"],
                        {"range": "mm", "pack": "A", "probe": "degC"}))
        self.assertEqual(report["unit_mismatches"], 3)
        scaled = self.subject(rows, "COLUMN_UNIT", "range")
        self.assertEqual(scaled["disposition"], "UNIT_SCALE_DIFFERENT")
        self.assertEqual((scaled["expected_quantity_kind"], scaled["actual_quantity_kind"]),
                         ("distance_m", "distance_m"))
        self.assertEqual((scaled["expected_scale"], scaled["actual_scale"]), ("1", "0.001"))
        self.assertIn("FINITE_SOURCE_QUALIFIED_UNIT_TABLE", scaled["unit_interpretation_basis"])
        kind = self.subject(rows, "COLUMN_UNIT", "pack")
        self.assertEqual(kind["disposition"], "UNIT_QUANTITY_KIND_DIFFERENT")
        self.assertEqual((kind["expected_quantity_kind"], kind["actual_quantity_kind"]),
                         ("voltage_v", "current_a"))
        offset = self.subject(rows, "COLUMN_UNIT", "probe")
        self.assertEqual(offset["disposition"], "UNIT_SCALE_DIFFERENT")
        self.assertEqual((offset["expected_offset"], offset["actual_offset"]), ("0", "273.15"))
        self.assertEqual((offset["expected_scale"], offset["actual_scale"]), ("1", "1"))

    def test_two_spellings_the_table_gives_one_meaning_are_not_a_scale_difference(self):
        """`K` and `degK` both map to ('temperature_k', 1, 0) in the finite table.

        The declared spellings differ and both are preserved, but there is no scale or quantity
        disagreement to claim, so this must not be reported as a scale difference.
        """
        rows, report, _ = self.rows_of(
            expectation({"probe": "K"}), read_report(["probe"], {"probe": "degK"}))
        self.assertEqual((report["unit_mismatches"], report["equivalent_spellings"]), (0, 1))
        unit = self.subject(rows, "COLUMN_UNIT", "probe")
        self.assertEqual(unit["disposition"], "UNIT_EQUIVALENT_UNDER_A_DIFFERENT_DECLARED_SPELLING")
        self.assertEqual((unhex(unit["expected_declared_hex"]), unhex(unit["actual_declared_hex"])),
                         ("K", "degK"))
        self.assertEqual((unit["expected_quantity_kind"], unit["actual_quantity_kind"]),
                         ("temperature_k", "temperature_k"))
        self.assertEqual((unit["expected_scale"], unit["actual_scale"]), ("1", "1"))
        self.assertEqual((unit["expected_offset"], unit["actual_offset"]), ("0", "0"))
        self.assertIn("NO_SCALE_OR_QUANTITY_DISAGREEMENT_TO_CLAIM",
                      unit["unit_interpretation_basis"])
        self.assertEqual(compare_unit("K", "degK")[0],
                         "UNIT_EQUIVALENT_UNDER_A_DIFFERENT_DECLARED_SPELLING")
        self.assertEqual(compare_unit("m", "mm")[0], "UNIT_SCALE_DIFFERENT")

    def test_a_repeated_declared_key_is_refused_rather_than_resolved(self):
        good_report = read_report(["a"], {"a": "m"})
        for expected, actual, why in (
                ('{"columns": {"a": "m", "a": "mm"}}', good_report, "a duplicate column unit"),
                ('{"columns": {"a": "m"}, "columns": {"a": "mm"}}', good_report,
                 "a duplicate top-level key"),
                (expectation({"a": "m"}),
                 '{"source_columns": ["a"], "profile_units": {"a": "m", "a": "mm"},'
                 ' "profile_identity": {}, "input_sha256": "x"}',
                 "a duplicate unit in the read report"),
        ):
            with self.assertRaises(ValueError, msg=why) as refusal:
                self.rows_of(expected, actual)
            self.assertIn("more than once", str(refusal.exception))

    def test_a_null_identity_expectation_is_refused_not_read_as_a_disagreement(self):
        with self.assertRaises(ValueError) as refusal:
            self.rows_of(json.dumps({"columns": {"a": "m"}, "profile_id": None}),
                         read_report(["a"], {"a": "m"}))
        self.assertIn("null", str(refusal.exception))
        rows, report, _ = self.rows_of(expectation({"a": "m"}), read_report(["a"], {"a": "m"}))
        self.assertFalse([r for r in rows if r["check_kind"] == "PROFILE_IDENTITY"])
        self.assertEqual(report["identity_mismatches"], 0)

    def test_an_uninterpretable_unit_string_is_compared_as_text_and_never_guessed(self):
        rows, report, _ = self.rows_of(
            expectation({"speed_m_s": "m/s"}),
            read_report(["speed_m_s"],
                        {"speed_m_s": "km_per_hour_reported_by_the_producer_not_verified"}))
        self.assertEqual(report["unit_mismatches"], 1)
        unit = self.subject(rows, "COLUMN_UNIT", "speed_m_s")
        self.assertEqual(unit["disposition"], "UNIT_DECLARATION_DIFFERENT")
        self.assertEqual((unit["actual_quantity_kind"], unit["actual_scale"]), ("", ""))
        self.assertIn("NEITHER_IS_INTERPRETED", unit["unit_interpretation_basis"])

    def test_schema_and_identity_disagreements_are_reported_with_both_sides(self):
        rows, report, _ = self.rows_of(
            expectation({"speed": "m/s", "absent_here": "V"}, profile_id="expected_pack_v2"),
            read_report(["speed", "surprise"], {"speed": "m/s"}))
        self.assertEqual((report["schema_mismatches"], report["identity_mismatches"]), (2, 1))
        absent = self.subject(rows, "SCHEMA_COLUMN", "absent_here")
        self.assertEqual(absent["disposition"], "SCHEMA_COLUMN_ABSENT_FROM_ARTEFACT")
        self.assertEqual(absent["actual_declared_hex"], "")
        surprise = self.subject(rows, "SCHEMA_COLUMN", "surprise")
        self.assertEqual(surprise["disposition"], "SCHEMA_COLUMN_NOT_EXPECTED")
        self.assertEqual(surprise["expected_declared_hex"], "")
        identity = self.subject(rows, "PROFILE_IDENTITY", "profile_id")
        self.assertEqual(identity["disposition"], "IDENTITY_DIFFERENT")
        self.assertEqual((unhex(identity["expected_declared_hex"]),
                          unhex(identity["actual_declared_hex"])),
                         ("expected_pack_v2", "rover_pack_v1"))
        self.assertFalse([r for r in rows if r["check_kind"] == "COLUMN_UNIT"
                          and unhex(r["check_subject_hex"]) == "absent_here"])


    def test_a_missing_declared_unit_is_unknown_and_not_a_disagreement(self):
        rows, report, _ = self.rows_of(
            expectation({"declared_both": "V", "artefact_silent": "V", "caller_silent": None}),
            read_report(["declared_both", "artefact_silent", "caller_silent"],
                        {"declared_both": "V", "caller_silent": "A"}))
        self.assertEqual((report["missing_evidence"], report["unit_mismatches"]), (2, 0))
        silent = self.subject(rows, "COLUMN_UNIT", "artefact_silent")
        self.assertEqual(silent["disposition"], "UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT")
        self.assertEqual(silent["evidence_state"], "MISSING_DECLARED_BASIS")
        self.assertEqual(silent["actual_declared_hex"], "")
        self.assertIn("MISSING_EVIDENCE_AND_NOT_A_DISAGREEMENT", silent["unit_interpretation_basis"])
        caller = self.subject(rows, "COLUMN_UNIT", "caller_silent")
        self.assertEqual(caller["disposition"], "UNKNOWN_NO_DECLARED_UNIT_IN_EXPECTATION")
        self.assertEqual(caller["evidence_state"], "MISSING_DECLARED_BASIS")
        self.assertEqual(compare_unit(None, "V")[0], "UNKNOWN_NO_DECLARED_UNIT_IN_EXPECTATION")
        self.assertEqual(compare_unit("V", None)[0], "UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT")


    def test_a_document_that_is_not_this_shape_is_refused_not_reported_as_a_mismatch(self):
        good_report = read_report(["speed"], {"speed": "m/s"})
        good_expectation = expectation({"speed": "m/s"})
        for expected, actual, why in (
                (good_expectation, "{not json", "a read report that is not JSON"),
                ("{not json", good_report, "an expectation that is not JSON"),
                (good_expectation, json.dumps({"source_columns": ["speed"]}),
                 "a JSON object that is not a read report"),
                (good_expectation, json.dumps([1, 2]), "a JSON array instead of an object"),
                (good_expectation, json.dumps({"source_columns": ["a", "a"], "profile_units": {},
                                               "profile_identity": {}, "input_sha256": "x"}),
                 "a read report repeating a source column"),
                (good_expectation, json.dumps({"source_columns": "speed", "profile_units": {},
                                               "profile_identity": {}, "input_sha256": "x"}),
                 "source_columns that is not a list"),
                (good_expectation, json.dumps({"source_columns": ["speed", ""], "profile_units": {},
                                               "profile_identity": {}, "input_sha256": "x"}),
                 "an empty actual column name"),
                (good_expectation, json.dumps({"source_columns": ["speed", "x" * 300],
                                               "profile_units": {}, "profile_identity": {},
                                               "input_sha256": "x"}),
                 "an over-long actual column name"),
                (expectation({}), good_report, "an expectation declaring no columns"),
                (json.dumps({"columns": {"speed": "m/s"}, "format": "telemetry_csv_us"}),
                 good_report, "a format expectation the read report cannot answer"),
                ("", good_report, "an empty expectation"),
        ):
            with self.assertRaises(ValueError, msg=why):
                self.rows_of(expected, actual)
        for bad in (None, -1, 2 ** 63, "1000"):
            with self.assertRaises(ValueError):
                self.rows_of(good_expectation, good_report, capture_time_us=bad)


    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_a_meaningful_mismatch_reaches_common_observation(self):
        _, _, output = self.rows_of(
            expectation({"range": "m", "artefact_silent": "V"}, profile_id="expected_pack_v2"),
            read_report(["range", "artefact_silent"], {"range": "mm"}))
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(source),
                 "--allow-equal-time"], capture_output=True, check=True).stdout)
        self.assertEqual(common["platform_domain"], "Unknown")
        fields = [o["fields"] for o in common["observations"]]
        dispositions = {f["disposition"] for f in fields}
        self.assertIn("UNIT_SCALE_DIFFERENT", dispositions)
        self.assertIn("IDENTITY_DIFFERENT", dispositions)
        self.assertIn("UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT", dispositions)
        scaled = next(f for f in fields if f["disposition"] == "UNIT_SCALE_DIFFERENT")
        self.assertEqual((scaled["expected_quantity_kind"], scaled["actual_quantity_kind"]),
                         ("distance_m", "distance_m"))
        self.assertEqual(scaled["artefact_input_sha256"], ARTEFACT_DIGEST)
        units = common["profile_units"]
        self.assertIn("never_a_unit_inferred_from_a_column_name", units["comparison_basis"])
        self.assertIn("never_confused_with_absent_evidence", units["evidence_state"])

    def test_the_cli_writes_its_accounting_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            (base / "expected.json").write_text(expectation({"range": "m"}))
            (base / "report.json").write_text(read_report(["range"], {"range": "mm"}))
            command = [os.sys.executable, str(SCRIPT), str(base / "expected.json"),
                       str(base / "report.json"), str(base / "out"),
                       "--capture-time-us", str(CAPTURE)]
            subprocess.run(command, check=True, capture_output=True)
            accounting = json.loads((base / "out/report.json").read_text())
            self.assertEqual(accounting["unit_mismatches"], 1)
            self.assertIn("NOT_AN_INTEGRATION_CAUSE", accounting["comparison_basis"])
            self.assertEqual(accounting["artefact_input_sha256"], ARTEFACT_DIGEST)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
