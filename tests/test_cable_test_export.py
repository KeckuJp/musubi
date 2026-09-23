"""Authored exports shaped by the pinned ethtool printer; no device and no ethtool run."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_cable_test_export import (APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION,
                                               SOURCE_PIN, convert)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/cable-test-export/profile.toml"
CAPTURE = 1700000000000000


def export(entries):
    """The writer opens a pretty-printed array and one object per entry."""
    return json.dumps(entries, indent=4).encode()


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class CableTestExportTests(unittest.TestCase):
    def test_a_saved_cable_test_reaches_common_output_with_its_pair_code_and_length(self):
        saved = export([{"pair": "Pair A", "code": "OK"},
                        {"pair": "Pair B", "code": "Open Circuit"},
                        {"pair": "Pair B", "length": 2.1}])
        output, report = convert(saved, interface="eth0", capture_time_us=CAPTURE)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([(r["entry_kind"], unhex(r["reported_pair_hex"]),
                           unhex(r["reported_result_code_hex"]), r["reported_fault_length_m"],
                           r["reported_fault_length_cm"]) for r in rows],
                         [("REPORTED_RESULT", "Pair A", "OK", "", ""),
                          ("REPORTED_RESULT", "Pair B", "Open Circuit", "", ""),
                          ("REPORTED_FAULT_LENGTH", "Pair B", "", "2.1", "210")])
        self.assertEqual(rows[2]["fault_length_disposition"],
                         "WHOLE_CENTIMETRES_RECOVERED_FROM_THE_PRINTED_METRES")
        self.assertEqual(rows[2]["measurement_uncertainty"], "NOT_STATED_BY_THE_SOURCE")
        self.assertTrue(all(unhex(r["declared_interface_hex"]) == "eth0" for r in rows))
        self.assertTrue(all(r["record_time_us"] == str(CAPTURE) for r in rows))
        self.assertIn("NOT_A_TEST_TIMESTAMP", rows[0]["capture_clock_basis"])
        self.assertIn("NOT_DERIVED_FROM_TRAFFIC_COUNTERS", rows[0]["cable_test_basis"])
        self.assertIn("NOT_A_CONNECTOR_FINDING", rows[0]["cable_test_basis"])
        self.assertEqual(rows[0]["source_pin"], SOURCE_PIN)
        self.assertEqual(unhex(rows[2]["source_entry_hex"]), '{"pair":"Pair B","length":2.1}')
        self.assertEqual((report["result_entries"], report["fault_length_entries"],
                          report["saved_entries"]), (2, 1, 3))
        self.assertEqual(report["connector_findings"], "NOT_REPORTED_BY_THIS_SOURCE")
        self.assertEqual(report["physical_damage_records"], "NOT_DERIVED")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "cable.json").write_bytes(saved)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_cable_test_export.py"),
                str(base / "cable.json"), str(base / "out"), "--interface", "eth0",
                "--capture-time-us", str(CAPTURE)], check=True, capture_output=True)
            self.assertEqual((base / "out/observations.csv").read_text(), output)
            self.assertEqual((base / "out/source.json").read_bytes(), saved)
            if not os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output half of "
                              "this test is the point, so a missing reader is an explicit skip")
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(base / "out/observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        fields = common["observations"][2]["fields"]
        self.assertEqual(fields["entry_kind"], "REPORTED_FAULT_LENGTH")
        self.assertEqual(fields["reported_fault_length_cm"], 210)
        self.assertEqual(fields["measurement_uncertainty"], "NOT_STATED_BY_THE_SOURCE")
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertIn("no_documented_mapping", common["profile_units"]["reported_pair_hex"])
        self.assertIn("reports_no_connector_entity", common["profile_units"]["cable_test_basis"])

    def test_the_writers_fallback_words_are_carried_with_what_they_hide(self):
        output, report = convert(export([{"pair": "Pair C", "code": "Unknown"},
                                         {"pair": "Unexpected pair", "code": "OK"},
                                         {"pair": "Pair D", "length": 1.23456}]),
                                 interface="enp3s0", capture_time_us=CAPTURE)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertIn("IMPEDANCE_MISMATCH", rows[0]["result_code_disposition"])
        self.assertIn("NAMES_NO_SINGLE_CONDITION", rows[0]["result_code_disposition"])
        self.assertEqual(rows[1]["pair_disposition"],
                         "THE_WRITERS_OWN_FALLBACK_FOR_A_PAIR_NUMBER_IT_HAS_NO_WORD_FOR")
        self.assertEqual((rows[2]["reported_fault_length_m"], rows[2]["reported_fault_length_cm"],
                          rows[2]["fault_length_disposition"]),
                         ("1.23456", "", "PRINTED_METRES_DO_NOT_LAND_ON_A_WHOLE_CENTIMETRE_SO_NONE_"
                                         "IS_CLAIMED"))
        self.assertEqual((report["entries_with_the_writers_fallback_code"],
                          report["entries_with_the_writers_fallback_pair"],
                          report["whole_centimetre_recoveries"]), (1, 1, 0))

    def test_a_string_this_writer_never_emits_is_preserved_rather_than_explained(self):
        """Only the literal fallback words are the writer's fallbacks; the rest are just strings."""
        output, report = convert(export([{"pair": "Pair A", "code": "Totally new word"},
                                         {"pair": "Pair Z", "code": "OK"}]),
                                 interface="eth0", capture_time_us=CAPTURE)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(unhex(rows[0]["reported_result_code_hex"]), "Totally new word")
        for disposition in (rows[0]["result_code_disposition"], rows[1]["pair_disposition"]):
            self.assertIn("DOES_NOT_EMIT_PRESERVED_AS_WRITTEN", disposition)
            self.assertIn("NOT_READ_AS_THE_WRITERS_FALLBACK_WORD", disposition)
        self.assertEqual((report["entries_with_an_unrecognised_code"],
                          report["entries_with_an_unrecognised_pair"],
                          report["entries_with_the_writers_fallback_code"]), (1, 1, 0))

    def test_a_reported_pair_is_never_rewritten_by_the_retention(self):
        """A pair that happens to equal an internal marker must survive untouched."""
        output, _ = convert(export([{"pair": "@@MUSUBI_LENGTH@@", "length": 2.1}]),
                            interface="eth0", capture_time_us=CAPTURE)
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(unhex(row["reported_pair_hex"]), "@@MUSUBI_LENGTH@@")
        self.assertEqual(unhex(row["source_entry_hex"]),
                         '{"pair":"@@MUSUBI_LENGTH@@","length":2.1}')
        self.assertEqual((row["reported_fault_length_m"], row["reported_fault_length_cm"]),
                         ("2.1", "210"))

    def test_a_length_outside_the_unsigned_centimetre_range_is_refused(self):
        """The source field is a u32 of centimetres, so a negative or huge metre value is not it."""
        from scripts.convert_cable_test_export import MAXIMUM_PRINTED_METRES
        for value, why in ((-1, "a negative length"), ("1e999", "an unrepresentable exponent"),
                           (42949701, "one metre past what the maximum can print"),
                           ("-0.01", "a small negative length")):
            body = ('[{"pair":"Pair A","length":%s}]' % value).encode()
            with self.assertRaises(ValueError, msg=why) as refusal:
                convert(body, interface="eth0", capture_time_us=CAPTURE)
            self.assertIn("outside the range this writer can print", str(refusal.exception))
        output, _ = convert(('[{"pair":"Pair A","length":%s}]' % MAXIMUM_PRINTED_METRES).encode(),
                            interface="eth0", capture_time_us=CAPTURE)
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual((row["reported_fault_length_m"], row["reported_fault_length_cm"]),
                         ("42949700", "4294970000"))
        zero = next(csv.DictReader(io.StringIO(
            convert(b'[{"pair":"Pair A","length":0}]', interface="eth0",
                    capture_time_us=CAPTURE)[0])))
        self.assertEqual((zero["reported_fault_length_m"], zero["reported_fault_length_cm"]),
                         ("0", "0"))

    def test_a_positive_length_this_printer_cannot_produce_is_refused_not_decoded_to_zero(self):
        """`(float)1 / 100` printed by `%g` is `0.01`, the smallest positive this writer emits.

        A token strictly between zero and that is not its output, and it cannot be waved through:
        ordinary numeric decoding flattens a sub-centimetre positive to 0.0 without saying so, and
        `reported_fault_length_m` would then carry an exact zero the file never contained.
        """
        from scripts.convert_cable_test_export import MINIMUM_PRINTED_METRES
        self.assertEqual(float("1e-999"), 0.0)
        for value, why in (("1e-999", "a positive that common decoding turns into zero"),
                           ("0.005", "half a centimetre"),
                           ("0.009999", "just under the smallest printable value")):
            body = ('[{"pair":"Pair A","length":%s}]' % value).encode()
            with self.assertRaises(ValueError, msg=why) as refusal:
                convert(body, interface="eth0", capture_time_us=CAPTURE)
            self.assertIn("below the smallest this writer can print", str(refusal.exception))
        saved = ('[{"pair":"Pair A","length":%s}]' % MINIMUM_PRINTED_METRES).encode()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "cable.json").write_bytes(saved)
            command = [os.sys.executable, str(ROOT / "scripts/convert_cable_test_export.py"),
                       str(base / "cable.json"), str(base / "out"), "--interface", "eth0",
                       "--capture-time-us", str(CAPTURE)]
            subprocess.run(command, check=True, capture_output=True)
            row = next(csv.DictReader(io.StringIO((base / "out/observations.csv").read_text())))
            self.assertEqual((row["reported_fault_length_m"], row["reported_fault_length_cm"]),
                             ("0.01", "1"))
            self.assertEqual(row["fault_length_disposition"],
                             "WHOLE_CENTIMETRES_RECOVERED_FROM_THE_PRINTED_METRES")
            (base / "tiny.json").write_bytes(b'[{"pair":"Pair A","length":1e-999}]')
            refused = subprocess.run(command[:2] + [str(base / "tiny.json"), str(base / "tiny-out")]
                                     + command[4:], capture_output=True)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("no successful conversion claim", refused.stderr.decode())
            self.assertFalse((base / "tiny-out").exists())
            if not os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output half of "
                              "this test is the point, so a missing reader is an explicit skip")
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(base / "out/observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 1)
        self.assertEqual(common["observations"][0]["fields"]["reported_fault_length_cm"], 1)

    def test_a_truncated_or_differently_shaped_file_is_refused(self):
        good = export([{"pair": "Pair A", "code": "OK"}])
        for broken, why in (
                (good[:-3], "a truncated file"),
                (b"", "an empty file"),
                (b"[]", "an array with no entries"),
                (json.dumps({"pair": "Pair A", "code": "OK"}).encode(), "an object, not an array"),
                (export(["Pair A"]), "an entry that is not an object"),
                (export([{"pair": "Pair A"}]), "an entry with neither code nor length"),
                (export([{"pair": "Pair A", "code": "OK", "length": 1.0}]),
                 "an entry with both, which this writer never emits"),
                (export([{"pair": "Pair A", "code": "OK", "extra": 1}]), "an unknown key"),
                (export([{"code": "OK"}]), "an entry with no pair"),
                (export([{"pair": 1, "code": "OK"}]), "a pair that is not a string"),
                (export([{"pair": "Pair A", "code": 1}]), "a code that is not a string"),
                (export([{"pair": "Pair A", "length": "2.1"}]), "a length that is not a number"),
                (export([{"pair": "Pair A", "length": float("inf")}]), "a non-finite length"),
                (b'[{"pair":"Pair A","pair":"Pair B","code":"OK"}]',
                 "a repeated key, which json would otherwise resolve by overwriting"),
                (b'[{"pair":"Pair A","code":"OK","code":"Open Circuit"}]', "a repeated code"),
                (b'[{"pair":"Pair A","length":NaN}]', "a NaN constant")):
            with self.assertRaises(ValueError, msg=why):
                convert(broken, interface="eth0", capture_time_us=CAPTURE)
        for kwargs, why in ((dict(interface="", capture_time_us=CAPTURE), "no interface"),
                            (dict(interface="eth 0", capture_time_us=CAPTURE), "a malformed name"),
                            (dict(interface="eth0", capture_time_us=-1), "a negative clock"),
                            (dict(interface="eth0", capture_time_us=1.5), "a non-integer clock")):
            with self.assertRaises(ValueError, msg=why):
                convert(good, **kwargs)


if __name__ == "__main__":
    unittest.main()
