"""Authored RINEX 3.04 GPS records; specification-shaped text, not a real GNSS recording.

Every byte is constructed here from the pinned specification's field tables. No receiver,
no stream, no positioning solution and no real or vendor sample record is involved.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_rinex_records import (
    GPS_OBSERVATION_CODES, TRANSMISSION_UNKNOWN, UNKNOWN_CODE_STATUS, convert)

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/rinex-records"
SCRIPT = ROOT / "scripts/convert_rinex_records.py"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
CAPTURE = 1700000000000000
CODES = ("C1C", "L1C", "D1C", "S1C")


def labelled(body, label):
    """Header label occupies columns 61-80 (Table A2)."""
    return body.ljust(60)[:60] + label.ljust(20)


def system_codes(system, codes, declared=None):
    """A SYS / # / OBS TYPES declaration plus its continuation lines."""
    count = len(codes) if declared is None else declared
    first, rest = codes[:13], codes[13:]
    lines = [labelled("%s  %3d" % (system, count)
                      + "".join(" %-3s" % code for code in first), "SYS / # / OBS TYPES")]
    while rest:  # "Use continuation line(s) for more than 13 observation descriptors."
        lines.append(labelled(" " * 6 + "".join(" %-3s" % code for code in rest[:13]),
                              "SYS / # / OBS TYPES"))
        rest = rest[13:]
    return lines


def obs_header(codes=CODES, unit="DBHZ", system="G", time_system="GPS", version="3.04",
               file_type="O", declared=None, end=True, interval=None, extra=()):
    lines = [labelled("%9s%11s%1s%19s%1s" % (version, "", file_type, "", system),
                      "RINEX VERSION / TYPE")]
    lines += system_codes("G", codes, declared)
    lines += list(extra)
    lines.append(labelled("%6d%6d%6d%6d%6d%13.7f%5s%-3s"
                          % (2006, 3, 24, 13, 10, 36.0, "", time_system), "TIME OF FIRST OBS"))
    if interval is not None:
        lines.append(labelled("%10.3f" % interval, "INTERVAL"))
    if unit:
        lines.append(labelled(unit, "SIGNAL STRENGTH UNIT"))
    lines.append(labelled("Authored for a specification check", "COMMENT"))
    return lines + ([labelled("", "END OF HEADER")] if end else [])


def epoch_line(flag=0, count=2, second=36.0, minute=10, offset=None, blank=False):
    if blank:  # "For events without significant epoch the epoch fields ... can be left blank"
        line = ">" + " " * 28
    else:
        line = "> %04d %02d %02d %02d %02d%11.7f" % (2006, 3, 24, 13, minute, second)
    line += "  %1d%3d" % (flag, count)
    if offset is not None:
        line += " " * 6 + "%15.12f" % offset
    return line


def obs_line(satellite, triplets):
    """A1,I2.2 then m(F14.3,I1,I1) — each triplet is exactly 16 columns."""
    line = satellite
    for value, lli, ssi in triplets:
        line += ("%14.3f" % value if value is not None else " " * 14) + (lli or " ") + (ssi or " ")
    return line.rstrip()


def exponent(value, letter="D"):
    mantissa, power = ("%19.12E" % value).split("E")
    return "%19s" % (mantissa.strip() + letter + power)


def nav_record(prn=6, transmission=302400.0, fit=4.0, letter="D", lines=8, second=44):
    head = "G%02d %04d %02d %02d %02d %02d %02d" % (prn, 1999, 9, 2, 17, 51, second)
    head += "".join(exponent(v, letter) for v in (-8.39701388031e-04, -1.65982783074e-10, 0.0))
    values = [1.0, 91.0, 4.842e-09, 1.6209e+00,
              4.84101474285e-06, 6.26740418375e-03, 6.52112066746e-06, 5.15365489006e+03,
              4.09904e+05, -2.42143869400e-07, 3.29237003460e+00, -5.96046447754e-07,
              1.11541663136e+00, 3.26593750000e+02, 2.06958726335e+00, -6.38312302555e-08,
              3.07155651409e-09, 0.0, 1.02500000000e+03, 0.0,
              4.0, 0.0, -9.31322574615e-09, 9.10000000000e+02,
              transmission, fit, 0.0, 0.0]
    record = [head]
    for orbit in range(7):
        record.append("    " + "".join(exponent(v, letter) for v in values[orbit * 4:orbit * 4 + 4]))
    return record[:lines]


def nav_header(system="G", version="3.04", file_type="N"):
    return [labelled("%9s%11s%1s%19s%1s" % (version, "", file_type, "", system),
                     "RINEX VERSION / TYPE"),
            labelled("   18", "LEAP SECONDS"),
            labelled("", "END OF HEADER")]


def rows_of(lines, kind, capture=CAPTURE):
    text, report = convert("\n".join(lines).encode(), kind, capture)
    return list(csv.DictReader(io.StringIO(text))), report


class RinexRecordTests(unittest.TestCase):
    def test_observations_reach_common_output_with_specification_units(self):
        body = obs_header(interval=30.0) + [
            epoch_line(offset=-0.123456789012),
            obs_line("G06", [(23629347.915, "", "6"), (0.300, "1", "8"),
                             (-353.0, "", "4"), (45.0, "", "7")]),
            obs_line("G09", [(20891534.648, "", "4"), (None, "", ""), (0.0, "", ""), (41.0, "", "5")])]
        observed, report = rows_of(body, "obs")
        self.assertEqual(report["output_records"], 8)
        self.assertEqual(report["observation_codes"], list(CODES))
        self.assertEqual(report["interval_s"], 30.0)
        first = observed[0]
        self.assertEqual(float(first["rinex_pseudorange_m"]), 23629347.915)
        self.assertEqual(float(observed[1]["rinex_carrier_phase_cycles"]), 0.300)
        self.assertEqual(float(observed[2]["rinex_doppler_hz"]), -353.0)
        self.assertEqual(float(observed[3]["rinex_signal_strength_value"]), 45.0)
        self.assertIn("DBHZ_DECLARED", observed[3]["rinex_signal_strength_unit_basis"])
        self.assertEqual(observed[1]["rinex_lli_code"], "1")
        self.assertIn("LOST_LOCK_CYCLE_SLIP_POSSIBLE", observed[1]["rinex_lli_bits_reported"])
        self.assertEqual(observed[1]["rinex_ssi_code"], "8")
        self.assertEqual(observed[0]["rinex_ssi_meaning"],
                         "PROJECTED_INTERVAL_1_TO_9_BETWEEN_MINIMUM_AND_MAXIMUM")
        self.assertEqual(observed[7]["rinex_ssi_meaning"], "AVERAGE_OR_GOOD_S_N_RATIO")
        self.assertEqual(observed[5]["rinex_observation_status"], "NOT_OBSERVED")
        self.assertEqual(observed[5]["rinex_carrier_phase_cycles"], "")
        self.assertEqual(observed[6]["rinex_observation_status"], "NOT_OBSERVED")
        self.assertEqual(observed[6]["rinex_doppler_hz"], "")
        self.assertEqual((first["rinex_epoch_year"], first["rinex_epoch_month"],
                          first["rinex_epoch_day"], first["rinex_epoch_hour"],
                          first["rinex_epoch_minute"], first["rinex_epoch_second_text"]),
                         ("2006", "3", "24", "13", "10", "36.0000000"))
        self.assertEqual(first["rinex_epoch_time_system"], "GPS")
        self.assertIn("NO_UTC_CONVERSION_APPLIED", first["rinex_epoch_time_basis"])
        self.assertEqual(float(first["rinex_receiver_clock_offset_s"]), -0.123456789012)
        self.assertEqual(first["record_time_us"], str(CAPTURE))
        self.assertIn("SEPARATE_FROM_THE_GNSS_EPOCH", first["capture_clock_basis"])

        if not Path(READER).exists():
            print("rinex observation common-reader integration NOT_RUN: reader not built")
            return
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "authored.obs"
            source.write_text("\n".join(body) + "\n")
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(out),
                            "--kind", "obs", "--capture-time-us", str(CAPTURE)],
                           check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "obs-profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 8)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            self.assertEqual(common["observations"][0]["fields"]["rinex_pseudorange_m"], 23629347.915)
            self.assertEqual(common["observations"][1]["fields"]["rinex_carrier_phase_cycles"], 0.3)
            self.assertIsNone(common["observations"][5]["fields"]["rinex_carrier_phase_cycles"])

    def test_navigation_reaches_common_output_with_every_stated_unit(self):
        body = nav_header() + nav_record() + nav_record(prn=13, transmission=TRANSMISSION_UNKNOWN,
                                                        letter="E")
        observed, report = rows_of(body, "nav")
        self.assertEqual(report["navigation_records"], 2)
        first, second = observed
        self.assertEqual(first["rinex_satellite_id"], "G06")
        self.assertEqual(first["rinex_toc_time_system"], "GPS")
        self.assertEqual((first["rinex_toc_year"], first["rinex_toc_hour"], first["rinex_toc_second"]),
                         ("1999", "17", "44"))
        self.assertEqual(float(first["rinex_sv_clock_bias_s"]), -8.39701388031e-04)
        self.assertEqual(float(first["rinex_sv_clock_drift_s_per_s"]), -1.65982783074e-10)
        self.assertEqual(float(first["rinex_crs_m"]), 91.0)
        self.assertEqual(float(first["rinex_delta_n_rad_per_s"]), 4.842e-09)
        self.assertEqual(float(first["rinex_sqrt_a_sqrt_m"]), 5.15365489006e+03)
        self.assertEqual(float(first["rinex_toe_s_of_gps_week"]), 4.09904e+05)
        self.assertEqual(float(first["rinex_omega_dot_rad_per_s"]), -6.38312302555e-08)
        self.assertEqual(float(first["rinex_gps_week_continuous"]), 1025.0)
        self.assertEqual(float(first["rinex_tgd_s"]), -9.31322574615e-09)
        self.assertEqual(float(first["rinex_transmission_time_s_of_gps_week"]), 302400.0)
        self.assertEqual(first["rinex_transmission_time_status"], "REPORTED")
        self.assertEqual(float(first["rinex_fit_interval_hours"]), 4.0)
        self.assertEqual(second["rinex_satellite_id"], "G13")
        self.assertEqual(second["rinex_transmission_time_s_of_gps_week"], "")
        self.assertEqual(second["rinex_transmission_time_status"], "TRANSMISSION_TIME_NOT_KNOWN")
        self.assertEqual(second["rinex_sqrt_a_sqrt_m"], first["rinex_sqrt_a_sqrt_m"])

        if not Path(READER).exists():
            print("rinex navigation common-reader integration NOT_RUN: reader not built")
            return
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "authored.nav"
            source.write_text("\n".join(body) + "\n")
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(out),
                            "--kind", "nav", "--capture-time-us", str(CAPTURE)],
                           check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "nav-profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            self.assertEqual(common["observations"][0]["fields"]["rinex_sqrt_a_sqrt_m"], 5153.65489006)
            self.assertIsNone(common["observations"][1]["fields"]["rinex_transmission_time_s_of_gps_week"])

    def test_second_authored_layout_continuation_events_and_retained_material(self):
        """A different shape: 14 codes over a continuation line, events, and non-GPS payload."""
        codes = tuple("C1C L1C D1C S1C C2W L2W D2W S2W C5Q L5Q D5Q S5Q I1 X1".split()[:12]
                      + ["I1 ", "X1 "])
        codes = ("C1C", "L1C", "D1C", "S1C", "C2W", "L2W", "D2W", "S2W",
                 "C5Q", "L5Q", "D5Q", "S5Q", "I1 ", "X1 ")
        codes = tuple(code.strip() for code in codes)
        codes = ("C1C", "L1C", "D1C", "S1C", "C2W", "L2W", "D2W", "S2W",
                 "C5Q", "L5Q", "D5Q", "S5Q", "I1C", "X1C")
        body = obs_header(codes=codes, unit=None, system="M") + [
            epoch_line(flag=4, count=1, blank=True),
            labelled("An event payload record that is never decoded", "COMMENT"),
            epoch_line(flag=1, count=2, second=37.0),
            obs_line("G06", [(1.0, "", "")] * 14),
            obs_line("R04", [(2.0, "", "")] * 14),
            epoch_line(flag=6, count=1, second=38.0),
            obs_line("G06", [(0.001, "", "")] * 14)]
        observed, report = rows_of(body, "obs")
        self.assertEqual(len(report["observation_codes"]), 14)
        self.assertEqual(report["observation_codes"][13], "X1C")
        self.assertEqual((report["event_records"], report["event_payload_records"]), (2, 2))
        self.assertEqual([entry["epoch_flag"] for entry in report["events"]], [4, 6])
        self.assertFalse(report["events"][0]["epoch_declared"])
        self.assertEqual(report["non_gps_satellite_records"], 1)
        self.assertEqual(report["non_gps_systems"], {"R": 1})
        self.assertTrue(all(row["rinex_satellite_id"] == "G06" for row in observed))
        unqualified = [row for row in observed if row["rinex_observation_code"] in ("I1C", "X1C")]
        self.assertEqual(len(unqualified), 2)
        for row in unqualified:
            self.assertEqual(row["rinex_observation_status"],
                             "UNQUALIFIED_OBSERVATION_TYPE_NO_VALUE_TYPED")
            self.assertTrue(all(row[name] == "" for name in
                                ("rinex_pseudorange_m", "rinex_carrier_phase_cycles",
                                 "rinex_doppler_hz", "rinex_signal_strength_value")))
        self.assertEqual(report["unqualified_code_records"], 2)
        strength = next(row for row in observed if row["rinex_observation_code"] == "S1C")
        self.assertEqual(strength["rinex_signal_strength_unit_basis"],
                         "RECEIVER_DEPENDENT_UNIT_NOT_RESOLVED_BY_THE_FILE")
        self.assertEqual(observed[0]["rinex_epoch_flag_reported"],
                         "POWER_FAILURE_BETWEEN_PREVIOUS_AND_CURRENT_EPOCH")

    def test_refusals_keep_an_unqualified_file_out(self):
        good = obs_header() + [epoch_line(count=1),
                               obs_line("G06", [(1.0, "", "")] * 4)]
        rows_of(good, "obs")  # the control: this one converts
        for broken, why in (
                (obs_header(version="3.03") + good[len(obs_header()):], "version"),
                (obs_header(file_type="N") + good[len(obs_header()):], "file type"),
                (obs_header(system="E") + good[len(obs_header()):], "satellite system"),
                (obs_header(declared=5) + good[len(obs_header()):], "declared code count"),
                (obs_header(time_system="XXX") + good[len(obs_header()):], "time system"),
                (obs_header(end=False) + good[len(obs_header()):], "END OF HEADER"),
                (obs_header() + [epoch_line(count=2), obs_line("G06", [(1.0, "", "")] * 4)],
                 "satellite count"),
                (obs_header() + [epoch_line(count=1), obs_line("G06", [(1.0, "", "")] * 4),
                                 epoch_line(count=1), obs_line("G06", [(1.0, "", "")] * 4)],
                 "repeated epoch"),
                (obs_header() + [epoch_line(count=1, second=37.0),
                                 obs_line("G06", [(1.0, "", "")] * 4),
                                 epoch_line(count=1, second=36.0),
                                 obs_line("G06", [(1.0, "", "")] * 4)], "decreasing epoch"),
                (obs_header() + [epoch_line(count=1).replace("36.0000000", "99.9999999x"),
                                 obs_line("G06", [(1.0, "", "")] * 4)], "epoch seconds"),
                (obs_header() + [epoch_line(count=1), "Q06" + " " * 10], "satellite identifier"),
        ):
            with self.assertRaises(ValueError, msg=why):
                rows_of(broken, "obs")
        with self.assertRaises(ValueError):
            rows_of(good, "nav")
        for capture in (None, -1, 1.5, "1"):
            with self.assertRaises(ValueError):
                rows_of(good, "obs", capture)
        with self.assertRaises(ValueError):
            rows_of(nav_header() + nav_record(lines=5), "nav")
        broken = nav_header() + nav_record()
        broken[-1] = broken[-1].replace("D+00", "D+0 ")
        with self.assertRaises(ValueError):
            rows_of(broken, "nav")
        with self.assertRaises(ValueError):  # nothing GPS in the file at all
            rows_of(nav_header(system="M") + ["R01 1999 09 02 17 51 44" + " " * 57] + [" "] * 7, "nav")

    def test_mixed_header_keeps_per_system_state_in_either_order(self):
        """G-then-R and R-then-G both leave the GPS declaration intact."""
        other = system_codes("R", ("C1C", "L1C", "D1C"))
        for extra, where in ((other, "G then R"), ([], "G only")):
            body = obs_header(system="M", extra=extra) + [
                epoch_line(count=1), obs_line("G06", [(1.0, "", "")] * 4)]
            observed, report = rows_of(body, "obs")
            self.assertEqual(report["observation_codes"], list(CODES), msg=where)
            self.assertEqual(len(observed), 4, msg=where)
        lines = [labelled("%9s%11s%1s%19s%1s" % ("3.04", "", "O", "", "M"),
                          "RINEX VERSION / TYPE")]
        lines += system_codes("R", ("C1C", "L1C", "D1C")) + system_codes("G", CODES)
        lines += [labelled("%6d%6d%6d%6d%6d%13.7f%5s%-3s"
                           % (2006, 3, 24, 13, 10, 36.0, "", "GPS"), "TIME OF FIRST OBS"),
                  labelled("", "END OF HEADER"),
                  epoch_line(count=2), obs_line("G06", [(1.0, "", "")] * 4),
                  obs_line("R07", [(2.0, "", "")] * 3)]
        observed, report = rows_of(lines, "obs")
        self.assertEqual(report["observation_codes"], list(CODES))
        self.assertEqual(len(observed), 4)
        self.assertEqual(report["non_gps_satellite_records"], 1)
        long_codes = CODES + ("C2W", "L2W", "D2W", "S2W", "C5Q", "L5Q", "D5Q", "S5Q", "C1W", "L1W")
        body = obs_header(codes=long_codes, system="M", extra=system_codes("R", ("C1C",))) + [
            epoch_line(count=1), obs_line("G06", [(1.0, "", "")] * 14)]
        _, report = rows_of(body, "obs")
        self.assertEqual(len(report["observation_codes"]), 14)

    def test_conflicting_or_excess_header_declarations_refuse(self):
        tail = [epoch_line(count=1), obs_line("G06", [(1.0, "", "")] * 4)]
        cases = {
            "duplicate system": obs_header(extra=system_codes("G", CODES)) + tail,
            "excess descriptors": obs_header(codes=CODES, declared=3) + tail,
            "too few descriptors": obs_header(codes=CODES, declared=5) + tail,
            "duplicate code": obs_header(codes=("C1C", "L1C", "C1C", "S1C")) + tail,
            "orphan continuation": ([labelled("%9s%11s%1s%19s%1s" % ("3.04", "", "O", "", "G"),
                                              "RINEX VERSION / TYPE"),
                                     labelled(" " * 6 + " C1C", "SYS / # / OBS TYPES")] + tail),
            "second version record": obs_header(
                extra=[labelled("%9s%11s%1s%19s%1s" % ("3.04", "", "O", "", "G"),
                                "RINEX VERSION / TYPE")]) + tail,
            "unknown system": obs_header(extra=system_codes("Z", ("C1C",))) + tail,
            "scale factor": obs_header(extra=[labelled("G  10  1 L1C", "SYS / SCALE FACTOR")]) + tail,
            "mixed blank time system": obs_header(system="M", time_system="") + tail,
        }
        for why, body in cases.items():
            with self.assertRaises(ValueError, msg=why):
                rows_of(body, "obs")
        _, report = rows_of(obs_header(time_system="") + tail, "obs")
        self.assertEqual(report["time_system"], "GPS")
        self.assertIn("DEFAULTED_TO_GPS_FOR_A_PURE_GPS_FILE", report["time_system_basis"])

    def test_event_header_update_is_applied_not_silently_ignored(self):
        """Flag 4 says header information follows; later rows must not use the old mapping."""
        reordered = ("L1C", "C1C", "S1C", "D1C")
        body = obs_header(unit=None) + [
            epoch_line(count=1, second=36.0),
            obs_line("G06", [(23000000.0, "", ""), (5.0, "", ""), (-100.0, "", ""), (44.0, "", "")]),
            epoch_line(flag=4, count=2, second=37.0),
            labelled("G    4 L1C C1C S1C D1C", "SYS / # / OBS TYPES"),
            labelled("DBHZ", "SIGNAL STRENGTH UNIT"),
            epoch_line(count=1, second=38.0),
            obs_line("G06", [(5.0, "", ""), (23000001.0, "", ""), (44.0, "", ""), (-100.0, "", "")])]
        observed, report = rows_of(body, "obs")
        self.assertEqual(report["header_updates"], 1)
        self.assertEqual(report["observation_code_updates"], 1)
        self.assertEqual(report["observation_codes"], list(reordered))
        self.assertEqual(observed[0]["rinex_observation_code"], "C1C")
        self.assertEqual(float(observed[0]["rinex_pseudorange_m"]), 23000000.0)
        after = observed[4:]
        self.assertEqual([row["rinex_observation_code"] for row in after], list(reordered))
        self.assertEqual(float(after[0]["rinex_carrier_phase_cycles"]), 5.0)
        self.assertEqual(float(after[1]["rinex_pseudorange_m"]), 23000001.0)
        self.assertEqual(observed[3]["rinex_signal_strength_unit_basis"],
                         "RECEIVER_DEPENDENT_UNIT_NOT_RESOLVED_BY_THE_FILE")
        self.assertIn("DBHZ_DECLARED", after[2]["rinex_signal_strength_unit_basis"])
        event = report["events"][0]
        self.assertTrue(event["applied_header_update"])
        self.assertEqual(event["observation_codes_after"], list(reordered))
        self.assertIn("SYS / # / OBS TYPES", event["payload_lines"][0])
        unsupported = obs_header() + [
            epoch_line(flag=4, count=1, second=37.0),
            labelled("G  10  1 L1C", "SYS / SCALE FACTOR"),
            epoch_line(count=1, second=38.0), obs_line("G06", [(1.0, "", "")] * 4)]
        with self.assertRaises(ValueError):
            rows_of(unsupported, "obs")

    def test_navigation_framing_uses_each_system_qualified_line_count(self):
        """GLONASS and SBAS are four lines; guessing eight would eat the next record."""
        def short(system, prn):
            head = "%s%02d %04d %02d %02d %02d %02d %02d" % (system, prn, 1999, 9, 2, 17, 51, 44)
            head += "".join(exponent(v) for v in (1.0, 2.0, 3.0))
            return [head] + ["    " + "".join(exponent(4.0) for _ in range(4))
                             for _ in range(3)]
        body = (nav_header(system="M") + short("R", 4) + nav_record(prn=6)
                + short("S", 20) + nav_record(prn=13))
        observed, report = rows_of(body, "nav")
        self.assertEqual(report["navigation_records"], 2)
        self.assertEqual([row["rinex_satellite_id"] for row in observed], ["G06", "G13"])
        self.assertEqual(report["non_gps_navigation_records"], 2)
        self.assertEqual(report["non_gps_systems"], {"R": 1, "S": 1})
        self.assertEqual(report["non_gps_navigation_lines"], 8)
        self.assertEqual(report["navigation_record_lines_by_system"]["R"], 4)
        self.assertEqual(report["navigation_record_lines_by_system"]["E"], 8)
        broken = nav_header() + nav_record()
        broken[4] = "G07" + broken[4][3:]  # an orbit line that looks like a new record
        with self.assertRaises(ValueError):
            rows_of(broken, "nav")
        with self.assertRaises(ValueError):
            rows_of(nav_header(system="M") + short("Z", 1) + nav_record(), "nav")

    def test_dates_times_and_identifiers_are_validated(self):
        tail = [obs_line("G06", [(1.0, "", "")] * 4)]
        for why, line in (("seconds 99", epoch_line(count=1, second=99.0)),
                          ("seconds 61", epoch_line(count=1, second=61.0)),
                          ("february 31", "> 2006 02 31 13 10 36.0000000  0  1"),
                          ("month 13", "> 2006 13 24 13 10 36.0000000  0  1"),
                          ("hour 24", "> 2006 03 24 24 10 36.0000000  0  1")):
            with self.assertRaises(ValueError, msg=why):
                rows_of(obs_header() + [line] + tail, "obs")
        observed, _ = rows_of(obs_header() + [epoch_line(count=1, second=60.5)] + tail, "obs")
        self.assertEqual(observed[0]["rinex_epoch_second_text"], "60.5000000")
        self.assertIn("LEAP_SECOND_REPRESENTATION_RETAINED_NOT_CONVERTED",
                      observed[0]["rinex_epoch_time_basis"])
        for satellite in ("G00", "Z06", "G  "):
            with self.assertRaises(ValueError, msg=satellite):
                rows_of(obs_header() + [epoch_line(count=1),
                                        obs_line(satellite, [(1.0, "", "")] * 4)], "obs")
        with self.assertRaises(ValueError):
            rows_of(nav_header() + nav_record(second=99), "nav")
        with self.assertRaises(ValueError):
            rows_of(nav_header() + nav_record(prn=0), "nav")

    def test_output_budget_is_enforced_during_expansion(self):
        from scripts import convert_rinex_records as module
        codes = tuple(sorted(GPS_OBSERVATION_CODES))[:60]  # genuine Table 4 codes only
        body = obs_header(codes=codes)
        for index in range(200):
            body.append(epoch_line(count=1, second=float(index % 60), minute=10 + index // 60))
            body.append(obs_line("G06", [(1.0, "", "")] * 2))  # short rows, many blank columns
        original = module.MAX_OUTPUT
        try:
            module.MAX_OUTPUT = 4096  # a budget the expansion passes long before the end
            with self.assertRaises(ValueError):
                rows_of(body, "obs")
        finally:
            module.MAX_OUTPUT = original
        observed, report = rows_of(body, "obs")
        self.assertEqual(report["output_records"], 200 * len(codes))
        self.assertLess(report["output_bytes"], original)
        self.assertTrue(any("SYS / # / OBS TYPES" in line for line in report["header_source_lines"]))
        self.assertIs(report["header_sample_truncated"], False)
        self.assertIn("label counts alone are not retention", report["retention_basis"])

    def test_code_only_event_update_completes_without_a_trailing_record(self):
        """A complete declaration may be the last line of a flag-4 update."""
        replacement = ("L1C", "C1C", "S1C", "D1C")
        body = obs_header() + [
            epoch_line(count=1, second=36.0), obs_line("G06", [(1.0, "", "")] * 4),
            epoch_line(flag=4, count=1, second=37.0),
            labelled("G    4 L1C C1C S1C D1C", "SYS / # / OBS TYPES"),
            epoch_line(count=1, second=38.0),
            obs_line("G06", [(5.0, "", ""), (23000001.0, "", ""), (44.0, "", ""), (-100.0, "", "")])]
        observed, report = rows_of(body, "obs")
        self.assertEqual(report["observation_codes"], list(replacement))
        self.assertEqual(report["observation_code_updates"], 1)
        after = observed[4:]
        self.assertEqual(float(after[0]["rinex_carrier_phase_cycles"]), 5.0)
        self.assertEqual(float(after[1]["rinex_pseudorange_m"]), 23000001.0)

        long_codes = CODES + ("C2W", "L2W", "D2W", "S2W", "C5Q", "L5Q", "D5Q", "S5Q",
                              "C2C", "L2C", "D2C", "S2C", "C5I", "L5I")
        update = system_codes("G", long_codes)
        self.assertEqual(len(update), 2)  # a declaration plus one continuation
        body = obs_header() + [
            epoch_line(count=1, second=36.0), obs_line("G06", [(1.0, "", "")] * 4),
            epoch_line(flag=4, count=len(update), second=37.0)] + update + [
            epoch_line(count=1, second=38.0), obs_line("G06", [(2.0, "", "")] * 18)]
        observed, report = rows_of(body, "obs")
        self.assertEqual(report["observation_codes"], list(long_codes))
        self.assertEqual(len(observed), 4 + 18)

        short = obs_header() + [
            epoch_line(count=1, second=36.0), obs_line("G06", [(1.0, "", "")] * 4),
            epoch_line(flag=4, count=1, second=37.0),
            labelled("G    6 L1C C1C S1C D1C", "SYS / # / OBS TYPES"),
            epoch_line(count=1, second=38.0), obs_line("G06", [(2.0, "", "")] * 4)]
        with self.assertRaises(ValueError):
            rows_of(short, "obs")

    def test_singleton_header_records_may_not_contradict_themselves(self):
        tail = [epoch_line(count=1), obs_line("G06", [(1.0, "", "")] * 4)]
        conflicting_unit = obs_header(unit="DBHZ",
                                      extra=[labelled("OTHERUNIT", "SIGNAL STRENGTH UNIT")]) + tail
        with self.assertRaises(ValueError):
            rows_of(conflicting_unit, "obs")
        conflicting_interval = obs_header(interval=30.0,
                                          extra=[labelled("%10.3f" % 1.0, "INTERVAL")]) + tail
        with self.assertRaises(ValueError):
            rows_of(conflicting_interval, "obs")
        blank_after_explicit = obs_header(
            time_system="GLO",
            extra=[labelled("%6d%6d%6d%6d%6d%13.7f%5s%-3s"
                            % (2006, 3, 24, 13, 10, 36.0, "", ""), "TIME OF FIRST OBS")]) + tail
        with self.assertRaises(ValueError):
            rows_of(blank_after_explicit, "obs")
        repeated = obs_header(time_system="GPS",
                              extra=[labelled("%6d%6d%6d%6d%6d%13.7f%5s%-3s"
                                              % (2006, 3, 24, 13, 10, 36.0, "", "GPS"),
                                              "TIME OF FIRST OBS")]) + tail
        _, report = rows_of(repeated, "obs")
        self.assertEqual(report["time_system_basis"], "DECLARED_IN_TIME_OF_FIRST_OBS")
        changed = obs_header(unit=None) + [
            epoch_line(count=1, second=36.0), obs_line("G06", [(1.0, "", "")] * 4),
            epoch_line(flag=4, count=1, second=37.0),
            labelled("DBHZ", "SIGNAL STRENGTH UNIT"),
            epoch_line(count=1, second=38.0), obs_line("G06", [(1.0, "", "")] * 4)]
        observed, _ = rows_of(changed, "obs")
        self.assertEqual(observed[3]["rinex_signal_strength_unit_basis"],
                         "RECEIVER_DEPENDENT_UNIT_NOT_RESOLVED_BY_THE_FILE")
        self.assertIn("DBHZ_DECLARED", observed[7]["rinex_signal_strength_unit_basis"])

    def test_only_table_four_gps_codes_carry_a_typed_value(self):
        """An invented band or attribute is retained, not counted as a qualified GPS code."""
        self.assertEqual(len(GPS_OBSERVATION_CODES), 86)
        for code in ("C1C", "L2W", "S5Q", "L1N", "C2D"):
            self.assertIn(code, GPS_OBSERVATION_CODES)
        for code in ("C1N", "C2N", "C3C", "L9Z", "C1Z"):
            self.assertNotIn(code, GPS_OBSERVATION_CODES)
        codes = ("C1C", "C3C", "L1N", "C1N")
        body = obs_header(codes=codes) + [
            epoch_line(count=1),
            obs_line("G06", [(23000000.0, "", "7"), (11.0, "", "6"),
                             (0.5, "1", "5"), (99.0, "", "4")])]
        observed, report = rows_of(body, "obs")
        self.assertEqual(len(observed), 4)
        typed = {row["rinex_observation_code"]: row for row in observed}
        self.assertEqual(float(typed["C1C"]["rinex_pseudorange_m"]), 23000000.0)
        self.assertEqual(typed["C1C"]["rinex_observation_status"], "REPORTED")
        self.assertEqual(float(typed["L1N"]["rinex_carrier_phase_cycles"]), 0.5)
        for code in ("C3C", "C1N"):
            row = typed[code]
            self.assertEqual(row["rinex_observation_status"], UNKNOWN_CODE_STATUS)
            self.assertTrue(all(row[name] == "" for name in
                                ("rinex_pseudorange_m", "rinex_carrier_phase_cycles",
                                 "rinex_doppler_hz", "rinex_signal_strength_value")))
            self.assertTrue(row["rinex_observation_raw_hex"].startswith("hex:"))
        self.assertEqual(bytes.fromhex(typed["C3C"]["rinex_observation_raw_hex"][4:]).decode().strip(),
                         "11.000")
        self.assertEqual(report["unknown_gps_code_records"], 2)
        self.assertEqual(report["unknown_gps_codes"], {"C3C": 1, "C1N": 1})
        self.assertEqual(report["admitted_gps_codes"], 86)
        self.assertEqual(report["observation_values"], 2)

    def test_practical_record_counts_and_cli(self):
        epochs = 500
        body = obs_header()
        for index in range(epochs):
            body.append(epoch_line(count=2, second=float(index % 60), minute=10 + index // 60))
            body.append(obs_line("G06", [(23000000.0 + index, "", "5"), (index * 0.5, "", "6"),
                                         (-100.0 - index, "", "4"), (40.0, "", "7")]))
            body.append(obs_line("G09", [(24000000.0 + index, "", "5"), (index * 0.25, "", "6"),
                                         (-200.0 - index, "", "4"), (42.0, "", "7")]))
        observed, report = rows_of(body, "obs")
        self.assertEqual(report["epoch_records"], epochs)
        self.assertEqual(report["satellite_records"], epochs * 2)
        self.assertEqual(len(observed), epochs * 2 * 4)
        self.assertEqual(float(observed[-4]["rinex_pseudorange_m"]), 24000000.0 + epochs - 1)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "many.obs"
            source.write_text("\n".join(body) + "\n")
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(out),
                            "--kind", "obs", "--capture-time-us", str(CAPTURE)],
                           check=True, capture_output=True)
            saved = json.loads((out / "report.json").read_text())
            self.assertEqual(saved["output_records"], epochs * 2 * 4)
            self.assertIn("830811bb", saved["specification"])
            self.assertEqual(subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(out),
                 "--kind", "obs", "--capture-time-us", str(CAPTURE)],
                capture_output=True).returncode, 1)
            for arguments in (["--kind", "obs"], ["--capture-time-us", str(CAPTURE)]):
                self.assertNotEqual(subprocess.run(
                    [os.sys.executable, str(SCRIPT), str(source),
                     str(Path(directory) / "x")] + arguments, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
