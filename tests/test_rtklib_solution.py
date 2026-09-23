"""Authored RTKLIB .pos documents only. No solver, no raw measurement, no real record.

Every fixture here is written by this file, in the exact shape the pinned `outsolheads` / `outpos`
emit. Nothing is solved and no geoid, datum or time system is converted. An authored document is
code and specification confirmation; it is never a real positioning record and never physical PPK
proof.
"""
import csv
import decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_rtklib_solution import FIELDS, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/rtklib-solution/profile.toml"

OPTION_LINE = ("%% (lat/lon/height=%s/%s,Q=1:fix,2:float,3:sbas,4:dgps,5:single,6:ppp,"
               "ns=# of satellites)")
COLUMN_LINE = ("%%  %-22s latitude(deg)   longitude(deg)  height(m)   Q  ns   sdn(m)   sde(m)   "
               "sdu(m)  sdne(m)  sdeu(m)  sdun(m)   age(s)  ratio")
EPOCH = ("2024/01/15 03:04:05.000   35.681236000  139.767125000    40.1234   1   9   0.0123   "
         "0.0098   0.0210  -0.0011   0.0005  -0.0003   1.20    99.9")


def document(option=True, datum="WGS84", height="ellipsoidal", times="GPST", rows=(EPOCH,),
             columns=None, extra_header=()):
    lines = []
    if option:
        lines.append(OPTION_LINE % (datum, height))
    lines.extend(extra_header)
    lines.append(COLUMN_LINE % times if columns is None else columns)
    lines.extend(rows)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


def rows_of(data, **kwargs):
    output, report = convert(data, **kwargs)
    return list(csv.DictReader(io.StringIO(output))), report, output


class RtklibSolution(unittest.TestCase):
    def test_a_saved_solution_reaches_common_output_with_its_declared_meaning(self):
        rows, report, _ = rows_of(document())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["latitude_deg"], "35.681236000")
        self.assertEqual(row["longitude_deg"], "139.767125000")
        self.assertEqual(row["height_m"], "40.1234")
        self.assertEqual(row["solution_time_text"], "2024/01/15 03:04:05.000")
        self.assertEqual(row["solution_time_system"], "GPST")
        self.assertEqual(row["height_reference"], "ellipsoidal")
        self.assertEqual(row["horizontal_datum"], "WGS84")
        self.assertIn("PRODUCER_DECLARED", row["height_reference_basis"])
        self.assertIn("PRODUCER_DECLARED", row["solution_time_basis"])
        self.assertEqual(row["solution_quality_code"], "1")
        self.assertEqual(row["solution_quality_name"], "FIX")
        self.assertEqual(row["satellites_used_reported"], "9")
        self.assertEqual(row["sdn_m"], "0.0123")
        self.assertEqual(row["sde_m"], "0.0098")
        self.assertEqual(row["sdne_signed_m"], "-0.0011")
        self.assertEqual(row["sdun_signed_m"], "-0.0003")
        self.assertEqual(row["age_of_differential_s"], "1.20")
        self.assertEqual(row["ambiguity_ratio_reported"], "99.9")
        self.assertEqual(len(row["source_sha256"]), 64)
        self.assertIn("NEVER_VERIFIED_ACCURACY", row["quality_basis"])
        self.assertIn("SIGNED_SQUARE_ROOTS_OF_COVARIANCE_AND_NOT_STANDARD_DEVIATIONS",
                      row["deviation_basis"])
        self.assertIn("COMPUTES_NO_SOLUTION", report["solutions_computed"])
        self.assertEqual(report["verified_accuracy"],
                         "NONE_EVERY_QUALITY_AND_SPREAD_REMAINS_THE_PRODUCERS_OWN_REPORT")
        self.assertEqual(report["quality_counts"], {"FIX": 1})

    def test_the_time_system_and_height_reference_come_from_the_header_not_a_default(self):
        for times in ("GPST", "UTC", "JST"):
            with self.subTest(times=times):
                rows, _, _ = rows_of(document(times=times))
                self.assertEqual(rows[0]["solution_time_system"], times)
                self.assertEqual(int(rows[0]["record_time_us"]),
                                 int(rows_of(document(times="GPST"))[0][0]["record_time_us"]))
        for height in ("ellipsoidal", "geodetic"):
            with self.subTest(height=height):
                rows, _, _ = rows_of(document(height=height))
                self.assertEqual(rows[0]["height_reference"], height)
                self.assertEqual(rows[0]["height_m"], "40.1234")
        rows, _, _ = rows_of(document(datum="Tokyo"))
        self.assertEqual(rows[0]["horizontal_datum"], "Tokyo")

    def test_a_week_and_seconds_of_week_epoch_is_read_exactly(self):
        rows, _, _ = rows_of(document(rows=(EPOCH.replace(
            "2024/01/15 03:04:05.000", "2297 356645.000"),)))
        self.assertEqual(rows[0]["solution_time_text"], "2297 356645.000")
        expected = ((2297 * 604800 + 356645) + 315964800) * 1000000
        self.assertEqual(int(rows[0]["record_time_us"]), expected)
        with self.assertRaises(ValueError):
            convert(document(rows=(EPOCH.replace(
                "2024/01/15 03:04:05.000", "2297 356645.0000001"),)))

    def test_seconds_of_week_is_exact_under_any_decimal_context(self):
        """Regression. `Decimal` addition and `scaleb` both round at the context precision.

        Adding the GPS epoch to the seconds-of-week before resolving the fraction absorbed a nonzero
        tail beyond 28 significant digits. The fraction is now resolved first, in integers.
        """
        from scripts.convert_rtklib_solution import week_tow_time_us
        tail = "356645.00000000000000000000000000001"
        self.assertEqual(len(decimal.Decimal(tail).as_tuple().digits), 35)
        self.assertGreater(len(decimal.Decimal(tail).as_tuple().digits), decimal.getcontext().prec)
        with self.assertRaises(ValueError) as caught:
            week_tow_time_us("2297", tail)
        self.assertIn("finer than one microsecond", str(caught.exception))
        for precision in (5, 9, 28, 200):
            with self.subTest(precision=precision), decimal.localcontext() as context:
                context.prec = precision
                self.assertEqual(week_tow_time_us("2297", "356645.000"), 1705547045000000)
                self.assertEqual(week_tow_time_us("2297", "356645.000001"), 1705547045000001)
                with self.assertRaises(ValueError):
                    week_tow_time_us("2297", tail)

    def test_the_time_ranges_are_the_pinned_writers_own(self):
        """`:60` and a full week are both outside what this writer emits, so both are refused."""
        with self.assertRaises(ValueError) as caught:
            convert(document(rows=(EPOCH.replace("03:04:05.000", "03:04:60.000"),)))
        self.assertIn("never prints 60", str(caught.exception))
        self.assertIn("none is invented", str(caught.exception))
        rows, _, _ = rows_of(document(rows=(EPOCH.replace("03:04:05.000", "03:04:59.000"),)))
        self.assertEqual(rows[0]["solution_time_text"], "2024/01/15 03:04:59.000")
        with self.assertRaises(ValueError) as caught:
            convert(document(rows=(EPOCH.replace("2024/01/15 03:04:05.000", "2297 604800.000"),)))
        self.assertIn("never emits 604800", str(caught.exception))
        rows, _, _ = rows_of(document(rows=(
            EPOCH.replace("2024/01/15 03:04:05.000", "2297 604799.999999"),)))
        self.assertEqual(rows[0]["solution_time_text"], "2297 604799.999999")

    def test_a_conflicting_or_repeated_header_basis_is_refused_not_last_wins(self):
        with self.assertRaises(ValueError) as caught:
            convert(document(extra_header=(OPTION_LINE % ("Tokyo", "geodetic"),)))
        self.assertIn("contradictory basis is refused", str(caught.exception))
        rows, _, _ = rows_of(document(extra_header=(OPTION_LINE % ("WGS84", "ellipsoidal"),)))
        self.assertEqual(rows[0]["height_reference"], "ellipsoidal")
        self.assertIn("PRODUCER_DECLARED", rows[0]["height_reference_basis"])

        with self.assertRaises(ValueError) as caught:
            convert(document(extra_header=(COLUMN_LINE % "UTC",)))
        self.assertIn("second column header", str(caught.exception))

        doubled = COLUMN_LINE % "GPST" + "   height(m)"
        with self.assertRaises(ValueError) as caught:
            convert(document(columns=doubled))
        self.assertIn("repeats", str(caught.exception))

        trailing = document().decode() + "% (lat/lon/height=Tokyo/geodetic,Q=1:fix)\r\n"
        with self.assertRaises(ValueError) as caught:
            convert(trailing.encode())
        self.assertIn("retroactively change", str(caught.exception))

    def test_a_missing_option_line_needs_a_caller_declaration_and_says_it_is_one(self):
        with self.assertRaises(ValueError) as caught:
            convert(document(option=False))
        self.assertIn("refuses a missing basis", str(caught.exception))

        rows, report, _ = rows_of(document(option=False),
                                  height_reference="geodetic", horizontal_datum="WGS84")
        self.assertEqual(rows[0]["height_reference"], "geodetic")
        self.assertIn("CALLER_DECLARED", rows[0]["height_reference_basis"])
        self.assertIn("NOT_producer_provenance", rows[0]["height_reference_basis"])
        self.assertIn("CALLER_DECLARED", report["height_reference_basis"])
        self.assertIn("PRODUCER_DECLARED", rows[0]["solution_time_basis"])

    def test_a_caller_declaration_contradicting_the_header_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            convert(document(height="ellipsoidal"), height_reference="geodetic")
        self.assertIn("contradiction is refused", str(caught.exception))
        with self.assertRaises(ValueError):
            convert(document(datum="WGS84"), horizontal_datum="Tokyo")
        with self.assertRaises(ValueError):
            convert(document(times="GPST"), time_system="UTC")
        rows, _, _ = rows_of(document(height="geodetic"), height_reference="geodetic")
        self.assertIn("PRODUCER_DECLARED", rows[0]["height_reference_basis"])

    def test_the_quality_enum_is_the_producers_report_including_the_value_the_legend_omits(self):
        for code, name in ((1, "FIX"), (2, "FLOAT"), (3, "SBAS"), (4, "DGPS"), (5, "SINGLE"),
                           (6, "PPP"), (7, "DEAD_RECKONING")):
            with self.subTest(code=code):
                rows, _, _ = rows_of(document(rows=(EPOCH.replace("   1   9", "   %d   9" % code),)))
                self.assertEqual(rows[0]["solution_quality_code"], str(code))
                self.assertEqual(rows[0]["solution_quality_name"], name)
                expected = "NO_THE_PINNED_ENUMERATION" if code == 7 else "YES"
                self.assertTrue(
                    rows[0]["solution_quality_in_header_legend"].startswith(expected),
                    rows[0]["solution_quality_in_header_legend"])
        for bad in ("   0   9", "   8   9", "  -1   9"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                convert(document(rows=(EPOCH.replace("   1   9", bad),)))

    def test_columns_this_path_does_not_carry_are_accounted_not_dropped(self):
        with_velocity = COLUMN_LINE % "GPST" + ("   vn(m/s)   ve(m/s)   vu(m/s)     sdvn     sdve"
                                               "     sdvu    sdvne    sdveu    sdvun")
        row = EPOCH + "   0.010   -0.020    0.003   0.0010   0.0011   0.0012   0.0001   0.0002   0.0003"
        rows, report, output = rows_of(document(columns=with_velocity, rows=(row,)))
        self.assertEqual(rows[0]["uncarried_column_count"], "9")
        self.assertEqual(json.loads(unhex(rows[0]["uncarried_column_names_hex"]))[:3],
                         ["vn(m/s)", "ve(m/s)", "vu(m/s)"])
        self.assertEqual(report["uncarried_columns"][0], "vn(m/s)")
        self.assertNotIn("-0.020", output)

    def test_the_producers_own_provenance_lines_are_carried_unresolved(self):
        extra = ["% program   : RTKLIB ver.2.4.3 b34",
                 "% inp file  : AUTHORED-NOT-A-REAL-FILE.obs",
                 "% ref pos   :  35.000000000  139.000000000    10.0000"]
        rows, report, _ = rows_of(document(extra_header=extra))
        carried = json.loads(unhex(rows[0]["producer_header_hex"]))
        self.assertEqual(len(carried), 3)
        self.assertIn("RTKLIB ver.2.4.3 b34", carried[0])
        self.assertEqual(rows[0]["header_line_count"], "5")
        self.assertEqual(report["header_lines"], 5)

    def test_the_important_failures_are_refused_rather_than_repaired(self):
        ecef = ("%  GPST                x-ecef(m)      y-ecef(m)      z-ecef(m)   Q  ns   sdx(m)"
                "   sdy(m)   sdz(m)  sdxy(m)  sdyz(m)  sdzx(m)   age(s)  ratio")
        enu = ("%  GPST              e-baseline(m)  n-baseline(m)  u-baseline(m)   Q  ns   sde(m)"
               "   sdn(m)   sdu(m)  sden(m)  sdnu(m)  sdue(m)   age(s)  ratio")
        dms = ("%  GPST                  latitude(d'\")   longitude(d'\")  height(m)   Q  ns"
               "   sdn(m)   sde(m)   sdu(m)  sdne(m)  sdeu(m)  sdun(m)   age(s)  ratio")
        for data, why in (
                (document(columns=ecef), "an ECEF solution, a different format"),
                (document(columns=enu), "a baseline solution, a different format"),
                (document(columns=dms), "the degree/minute/second spelling of the same writer"),
                (document(height="orthometric"), "a height word the pinned writer never emits"),
                (document(datum="ITRF2014"), "a datum word the pinned writer never emits"),
                (document(times="TAI"),
                 "an unrecognised token in the time position, refused not absorbed as a column"),
                (document(rows=(EPOCH.replace("35.681236000", "95.681236000"),)),
                 "a latitude outside the range of real latitudes"),
                (document(rows=(EPOCH.replace("139.767125000", "239.767125000"),)),
                 "a longitude outside the range of real longitudes"),
                (document(rows=(EPOCH.replace("0.0123", "-0.0123"),)),
                 "a negative reported spread on a diagonal term"),
                (document(rows=(EPOCH.replace("   1.20", "   -1.20"),)),
                 "a negative differential age"),
                (document(rows=(EPOCH.replace("35.681236000", "not-a-number"),)),
                 "a latitude that is not a number"),
                (document(rows=(EPOCH.replace("2024/01/15", "2024/02/30"),)),
                 "a calendar date that does not exist"),
                (document(rows=("2024/01/15 03:04:05.000   35.68   139.76",)),
                 "a row with fewer fields than the declared layout"),
                (document(rows=()), "a header with no solution epoch"),
                (b"35.681236000 139.767125000\r\n", "a file with no header at all"),
                (b"   ", "an empty file"),
                ("% header\n", "a str rather than the saved bytes")):
            with self.assertRaises(ValueError, msg=why):
                convert(data)

    def test_an_unrecognised_time_token_is_refused_even_with_a_caller_declaration(self):
        """It must not be quietly absorbed into the uncarried-column list."""
        with self.assertRaises(ValueError) as caught:
            convert(document(times="TAI"), time_system="GPST")
        self.assertIn("refused rather than being absorbed", str(caught.exception).replace(
            "refused rather than absorbed", "refused rather than being absorbed"))
        no_token = COLUMN_LINE % ""
        with self.assertRaises(ValueError):
            convert(document(columns=no_token))
        rows, _, _ = rows_of(document(columns=no_token), time_system="UTC")
        self.assertEqual(rows[0]["solution_time_system"], "UTC")
        self.assertIn("CALLER_DECLARED", rows[0]["solution_time_basis"])
        self.assertEqual(rows[0]["uncarried_column_count"], "0")

    def test_every_emitted_column_is_declared_and_the_rows_reach_common_output(self):
        second = EPOCH.replace("03:04:05.000", "03:04:06.000").replace("   1   9", "   2   8")
        output, _ = convert(document(rows=(EPOCH, second)))
        declared = PROFILE.read_text(encoding="utf-8").split("[units]")[1]
        for name in FIELDS:
            self.assertIn("\n%s = " % name, declared, name)
        self.assertNotIn('"', output)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(path)], capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        fields = common["observations"][0]["fields"]
        self.assertEqual(fields["latitude_deg"], 35.681236)
        self.assertEqual(fields["height_reference"], "ellipsoidal")
        self.assertEqual(fields["solution_quality_name"], "FIX")
        self.assertEqual(fields["sdne_signed_m"], -0.0011)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_the_command_line_writes_the_same_rows_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "solution.pos", base / "out"
            source.write_bytes(document())
            subprocess.run([sys.executable, "-m", "scripts.convert_rtklib_solution", str(source),
                            str(out)], cwd=ROOT, check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["epochs"], 1)
            self.assertEqual(report["height_reference"], "ellipsoidal")
            self.assertEqual(report["time_system"], "GPST")
            second = subprocess.run([sys.executable, "-m", "scripts.convert_rtklib_solution",
                                     str(source), str(out)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("never overwrites", second.stderr.decode())


if __name__ == "__main__":
    unittest.main()
