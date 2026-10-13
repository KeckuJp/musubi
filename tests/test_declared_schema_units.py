"""Authored expectations/records, legacy reports and actual common-reader report integration.

Older cases author a legacy names-only report; the explicit end-to-end case runs both readers.
No device or network, and a passing test proves code and schema behaviour only.
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

from scripts.check_declared_schema_units import compare_unit, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/schema-unit-checks/profile.toml"
SCRIPT = ROOT / "scripts/check_declared_schema_units.py"
CAPTURE = 1_789_000_000_000_000
ARTEFACT_DIGEST = "sha256:" + "a1" * 32
PROFILE_DIGEST = "sha256:" + "b2" * 32


def read_report(columns, units, *, profile_id="rover_pack_v1", version="1"):
    """Authored legacy names-only saved report, not the actual reader's canonical object shape."""
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
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_actual_reader_report_through_comparison_cli_to_common(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            profile = base / "source.toml"
            profile.write_text('profile_id = "authored_probe"\nversion = "1"\nfamily = "unknown"\n'
                               'source_role = "recorded_export"\nformat = "telemetry_csv_us"\n'
                               'extensions = ["csv"]\ndefault_clock_basis = "unknown"\nchannels = ["onboard"]\n'
                               '[fields]\ntime = "t"\n[units]\nprobe = "K"\n')
            source = base / "source.csv"
            source.write_text("t,probe,silent\n1000,300,\n")
            reader = os.environ["MUSUBI_TELEMETRY_READER"]
            actual_text = subprocess.run([reader, str(profile), str(source)],
                                         capture_output=True, check=True).stdout.decode()
            actual = json.loads(actual_text)
            self.assertEqual(actual["source_columns"], [
                {"name": "t", "unit": None}, {"name": "probe", "unit": None},
                {"name": "silent", "unit": None}])
            saved = base / "report.json"
            saved.write_text(actual_text)
            expected_text = expectation({"t": None, "probe": "degC", "silent": "V", "absent": None})
            expected = base / "expected.json"
            expected.write_text(expected_text)
            out = base / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(expected), str(saved), str(out),
                            "--capture-time-us", str(CAPTURE)], check=True, capture_output=True)
            common = json.loads(subprocess.run([reader, str(PROFILE), str(out / "observations.csv"),
                                               "--allow-equal-time"], check=True, capture_output=True).stdout)
            fields = [o["fields"] for o in common["observations"]]
            self.assertEqual(common["main_rows"], 7)
            self.assertEqual({f["disposition"] for f in fields}, {
                "SCHEMA_COLUMN_ABSENT_FROM_ARTEFACT", "MATCHES_DECLARED_EXPECTATION",
                "UNIT_SCALE_DIFFERENT", "UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT",
                "UNKNOWN_NO_DECLARED_UNIT_IN_EXPECTATION"})
            scaled = next(f for f in fields if f["disposition"] == "UNIT_SCALE_DIFFERENT")
            self.assertEqual((scaled["expected_offset"], scaled["actual_offset"]), (273.15, 0))
            for f, observation in zip(fields, common["observations"]):
                self.assertEqual(f["artefact_input_sha256"], actual["input_sha256"])
                self.assertEqual(f["profile_source_sha256"], actual["profile_identity"]["source_sha256"])
                self.assertEqual(f["read_report_sha256"], "sha256:" + hashlib.sha256(actual_text.encode()).hexdigest())
                self.assertEqual(f["expectation_sha256"], "sha256:" + hashlib.sha256(expected_text.encode()).hexdigest())
                self.assertEqual(observation["clock_basis"], "Unknown")

    def test_canonical_columns_reject_mixed_duplicate_and_malformed_declarations(self):
        good = {"name": "probe", "unit": "K"}
        for columns in ([good, "other"], [good, good], [{"name": "probe"}],
                        [{**good, "extra": 1}], [{"name": 1, "unit": "K"}],
                        [{"name": "", "unit": "K"}], [{"name": "x" * 257, "unit": None}],
                        [{"name": "probe", "unit": 3}], [{"name": "probe", "unit": ""}],
                        [{"name": "probe", "unit": "x" * 1025}],
                        [good, {"name": "probe", "unit": "degC"}]):
            with self.subTest(columns=columns):
                document = json.loads(read_report([], {"probe": "K"}))
                document["source_columns"] = columns
                with self.assertRaises(ValueError):
                    self.rows_of(expectation({"probe": "K"}), json.dumps(document))

    def test_canonical_and_legacy_meaning_match_with_legacy_bytes_unchanged(self):
        old = read_report(["probe"], {"probe": "degC"})
        expected = expectation({"probe": "K"})
        rows, report, output = self.rows_of(expected, old)
        self.assertEqual(hashlib.sha256(output.encode()).hexdigest(),
                         "51b91ce0bd2e2d031c8748464a8223e74bf06c4c13d63f155f3c57a344048fc1")
        self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
                         "dc5569e734fa33a6828c2292cf608fdd698b44e03c409ae472640bea1dc19cb5")
        document = json.loads(old)
        # Header annotation is a separate origin; it must not overwrite profile_units.
        document["source_columns"] = [{"name": "probe", "unit": "opaque-header-declaration"}]
        canonical_rows, canonical_report, _ = self.rows_of(expected, json.dumps(document))
        for row in rows + canonical_rows:
            row.pop("read_report_sha256")
        report.pop("read_report_sha256")
        canonical_report.pop("read_report_sha256")
        self.assertEqual(rows, canonical_rows)
        self.assertEqual(report, canonical_report)
        document["profile_units"] = {}
        missing_rows, _, _ = self.rows_of(expected, json.dumps(document))
        self.assertEqual(self.subject(missing_rows, "COLUMN_UNIT", "probe")["disposition"],
                         "UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT")

    def rows_of(self, expected, actual, **kwargs):
        kwargs.setdefault("capture_time_us", CAPTURE)
        output, report = convert(expected, actual, **kwargs)
        rows = list(csv.DictReader(io.StringIO(output)))
        return rows, report, output

    def subject(self, rows, kind, name):
        found = [r for r in rows if r["check_kind"] == kind and unhex(r["check_subject_hex"]) == name]
        self.assertEqual(len(found), 1, f"exactly one {kind} row for {name}")
        return found[0]

    # ----------------------------------------------------------------- the matching pair

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
        # Both digests the read report carries travel with every row.
        self.assertEqual(unit["artefact_input_sha256"], ARTEFACT_DIGEST)
        self.assertEqual(unit["profile_source_sha256"], PROFILE_DIGEST)
        self.assertIn("NEVER_A_UNIT_INFERRED_FROM_A_COLUMN_NAME", unit["comparison_basis"])

    # ----------------------------------------------------------------- meaningful mismatches

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
        # degC differs from K by an OFFSET, not a scale: both must be reported.
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
        # Both spellings are still carried as written.
        self.assertEqual((unhex(unit["expected_declared_hex"]), unhex(unit["actual_declared_hex"])),
                         ("K", "degK"))
        # And the table's identical triple is shown on both sides.
        self.assertEqual((unit["expected_quantity_kind"], unit["actual_quantity_kind"]),
                         ("temperature_k", "temperature_k"))
        self.assertEqual((unit["expected_scale"], unit["actual_scale"]), ("1", "1"))
        self.assertEqual((unit["expected_offset"], unit["actual_offset"]), ("0", "0"))
        self.assertIn("NO_SCALE_OR_QUANTITY_DISAGREEMENT_TO_CLAIM",
                      unit["unit_interpretation_basis"])
        self.assertEqual(compare_unit("K", "degK")[0],
                         "UNIT_EQUIVALENT_UNDER_A_DIFFERENT_DECLARED_SPELLING")
        # A real scale difference is still a scale difference.
        self.assertEqual(compare_unit("m", "mm")[0], "UNIT_SCALE_DIFFERENT")

    def test_a_repeated_declared_key_is_refused_rather_than_resolved(self):
        # json.loads keeps the last value for a duplicate key, so a column whose unit was declared
        # twice would be read as one of them with nothing marking the loss.
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
        # The identity keys are optional: a caller who does not want the check omits the key. An
        # explicit null declares nothing, so it must not be compared against a present value.
        with self.assertRaises(ValueError) as refusal:
            self.rows_of(json.dumps({"columns": {"a": "m"}, "profile_id": None}),
                         read_report(["a"], {"a": "m"}))
        self.assertIn("null", str(refusal.exception))
        # Omitting the key simply skips that subject.
        rows, report, _ = self.rows_of(expectation({"a": "m"}), read_report(["a"], {"a": "m"}))
        self.assertFalse([r for r in rows if r["check_kind"] == "PROFILE_IDENTITY"])
        self.assertEqual(report["identity_mismatches"], 0)

    def test_an_uninterpretable_unit_string_is_compared_as_text_and_never_guessed(self):
        # Real profiles declare descriptive strings, not symbols. They are compared as written.
        rows, report, _ = self.rows_of(
            expectation({"speed_m_s": "m/s"}),
            read_report(["speed_m_s"],
                        {"speed_m_s": "km_per_hour_reported_by_the_producer_not_verified"}))
        self.assertEqual(report["unit_mismatches"], 1)
        unit = self.subject(rows, "COLUMN_UNIT", "speed_m_s")
        self.assertEqual(unit["disposition"], "UNIT_DECLARATION_DIFFERENT")
        # No interpretation at all: the column name says m_s and that is deliberately ignored.
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
        # An absent column has no unit row at all: there is nothing to compare.
        self.assertFalse([r for r in rows if r["check_kind"] == "COLUMN_UNIT"
                          and unhex(r["check_subject_hex"]) == "absent_here"])

    # ----------------------------------------------------------------- missing evidence

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
        # The comparison helper keeps the same rule on its own.
        self.assertEqual(compare_unit(None, "V")[0], "UNKNOWN_NO_DECLARED_UNIT_IN_EXPECTATION")
        self.assertEqual(compare_unit("V", None)[0], "UNKNOWN_NO_DECLARED_UNIT_IN_ARTEFACT")

    # ----------------------------------------------------------------- refusals

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
        # A comparison has no clock of its own, so the capture time is required and bounded.
        for bad in (None, -1, 2 ** 63, "1000"):
            with self.assertRaises(ValueError):
                self.rows_of(good_expectation, good_report, capture_time_us=bad)

    # ----------------------------------------------------------------- common output

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
        # The mismatch itself reaches common output, not only a CLI error.
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
