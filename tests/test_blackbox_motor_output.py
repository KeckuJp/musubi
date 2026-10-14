"""Betaflight 4.2.0 commanded motor output: declared meaning, refusals, and the common connection.

Authored fixtures and a stubbed decoder only. Real records: zero. The adopted case's 4.2.0
real-input evidence is historical and is neither re-run nor extended to motors here.
"""
import json
import csv
import io
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import decode_blackbox_recording
from decode_blackbox_recording import (MAGIC, END, header_lines, motor_field_names,
                                       motor_output_declaration, require_motor_firmware)
from convert_blackbox_motor_output import convert

SCRIPT = ROOT / "scripts/decode_blackbox_recording.py"
PROFILE = ROOT / "profiles/declared/betaflight-motor-main/profile.toml"
SCHEMA = "betaflight-4.2.0-motor-output"


class MotorWindowTests(unittest.TestCase):
    def test_large_adjacent_and_independent_motor_windows_reach_common(self):
        from convert_blackbox_motor_output import convert_path
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.csv"
            header = "time (us),motor[0],motor[1],unknown\n"
            with path.open("w") as stream:
                stream.write(header)
                for index in range(513):
                    stream.write(f"{index},unexamined,unexamined," + "x" * 32768 + "\n")
                stream.write("600,980,2001,first\n600,1500,1600,second\n601,1000,2000,third\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertGreater(path.stat().st_size, 16 * 1024**2)
            seen = []
            for start, stop in ((513, 514), (514, 516)):
                output, report = convert_path(path, SCHEMA, (1000, 2000),
                                              start_record=start, stop_record=stop)
                self.assertEqual(report["source_sha256"], digest)
                self.assertEqual(report["excluded_records"], 516 - (stop - start))
                self.assertEqual(report["excluded_values"], "NOT_DECODED_OR_VALIDATED")
                rows = list(csv.DictReader(io.StringIO(output)))
                seen.extend(int(row["source_record_index"]) for row in rows)
                selected = Path(directory) / f"{start}.csv"
                selected.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(selected),
                                    "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(len(common["observations"]), stop - start)
                self.assertEqual(common["observations"][0]["fields"]["source_file_sha256"],
                                 "sha256:" + digest)
                self.assertEqual(common["observations"][0]["fields"]["reported_motor_output_demand_0"],
                                 980 if start == 513 else 1500)
                self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
            self.assertEqual(seen, [513, 514, 515])
            path.write_text("time (us),motor[0],note\n10,0,independent\n")
            output, report = convert_path(path, SCHEMA, (0, 100), start_record=0, stop_record=1)
            self.assertEqual(report["motor_semantics"]["motor_count"], 1)
            self.assertEqual(list(csv.DictReader(io.StringIO(output)))[0]["reported_motor_output_demand_0"], "0")
            selected = Path(directory) / "independent.csv"
            selected.write_text(output)
            common = json.loads(subprocess.run([reader, str(PROFILE), str(selected),
                                "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["observations"][0]["fields"]["reported_motor_output_demand_0"], 0)
            cli_output = Path(directory) / "cli.csv"
            cli_report = Path(directory) / "cli.json"
            command = [sys.executable, str(ROOT / "scripts/convert_blackbox_motor_output.py"),
                       str(path), str(cli_output), "--schema", SCHEMA, "--output-scale", "0,100",
                       "--report", str(cli_report), "--start-record", "0"]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(cli_output.exists())
            subprocess.run(command + ["--stop-record", "1"], check=True, capture_output=True)
            self.assertEqual(cli_output.read_text(), output)

    def test_motor_window_refuses_bad_header_selected_demand_and_excluded_reset(self):
        from convert_blackbox_motor_output import convert_path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            for data in ("time (us),motor[1]\n1,1000\n", "time (us),motor[0]\n1,NaN\n",
                         "time (us),motor[0]\n2,1000\n1,unexamined\n"):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    convert_path(path, SCHEMA, (1000, 2000), start_record=0, stop_record=1)
FIELD_NAMES = b"H Field I name:loopIteration,time,motor[0],motor[1],motor[2],motor[3]\n"
DECODED_CSV = (b"time (us), vbatLatest, motor[0], motor[1], motor[2], motor[3]\n"
               b"1001, 2281, 1000, 1200, 1400, 1600\n"
               b"2001, 2280, 980, 2000, 1401, 1601\n")


def recording(motor_output=b"H motorOutput:1000,2000\n", names=FIELD_NAMES,
              revision=b"H Firmware revision:Betaflight 4.2.0 (norev) STM32F745\n"):
    return MAGIC + revision + names + motor_output + END


class MotorOutputDeclarationTest(unittest.TestCase):
    def test_writer_declared_scale_and_contiguous_columns(self):
        declaration = motor_output_declaration(recording())
        self.assertEqual(declaration, {"low": 1000, "high": 2000,
                                       "columns": ["motor[0]", "motor[1]", "motor[2]", "motor[3]"]})
        # One motor is as valid as four; the writer omits the rest by condition, not by fault.
        single = recording(names=b"H Field I name:time,motor[0]\n")
        self.assertEqual(motor_output_declaration(single)["columns"], ["motor[0]"])

    def test_absent_repeated_or_partial_scale_is_refused_not_guessed(self):
        for wrong in (
            b"",                                        # header absent
            b"H motorOutput:1000,2000\nH motorOutput:1000,2000\n",   # repeated
            b"H motorOutput:1000\n",                    # one value only
            b"H motorOutput:1000,2000bad\n",            # trailing garbage
            b"H motorOutput:1000, 2000\n",              # not what the writer prints
            b"H motorOutput:-100,2000\n",               # %d of a uint16 is never signed
            b"H motorOutput:1000,70000\n",              # outside the printed uint16 range
            b"H motorOutput:2000,2000\n",               # no positive span to declare
            b"H motorOutput:2000,1000\n",               # inverted
        ):
            with self.subTest(header=wrong), self.assertRaises(ValueError):
                motor_output_declaration(recording(motor_output=wrong))

    def test_scale_is_read_from_the_header_block_only(self):
        # A header-looking sequence after the block ends must not supply missing metadata.
        payload = MAGIC + b"H Firmware revision:Betaflight 4.2.0 (norev) STM32F745\n" + FIELD_NAMES \
            + b"I\x01\x02\n" + b"H motorOutput:1000,2000\n" + END
        self.assertEqual(len(header_lines(payload)), 3)
        with self.assertRaises(ValueError):
            motor_output_declaration(payload)

    def test_missing_or_broken_motor_columns_are_refused(self):
        for names in (
            b"H Field I name:loopIteration,time,vbatLatest\n",       # logged no motor at all
            b"H Field I name:time,motor[1],motor[2]\n",              # not contiguous from zero
            b"H Field I name:time,motor[0],motor[2]\n",              # gap
            b"H Field I name:time,motor[1],motor[0]\n",              # out of order
            b"H Field I name:time,motor[0],motor[0]\n",              # duplicated
        ):
            with self.subTest(names=names), self.assertRaises(ValueError):
                motor_output_declaration(recording(names=names))
        with self.assertRaises(ValueError):   # exactly one main-frame field header is required
            motor_field_names(header_lines(recording(names=FIELD_NAMES + FIELD_NAMES)))

    def test_declaration_is_qualified_to_the_one_release_actually_read(self):
        self.assertEqual(require_motor_firmware("Betaflight 4.2.0 (norev) STM32F745"), "4.2.0")
        for wrong in ("Betaflight 4.2.11 (norev) STM32F745", "Betaflight 4.3.0 (norev) F7",
                      "INAV 4.2.0 (norev) F7", "Betaflight", ""):
            with self.subTest(revision=wrong), self.assertRaises(ValueError):
                require_motor_firmware(wrong)

    def run_exporter(self, root, *, raw, extra=()):
        """The exporter end to end with the external decoder stubbed; no decoder is executed."""
        source, decoder, output = root / "source.bfl", root / "decoder", root / "output"
        source.write_bytes(raw)
        decoder.write_bytes(b"authored decoder placeholder; never executed")
        report = "Frames 2 20 bytes\n7 iterations are missing in total\n"
        argv = [str(SCRIPT), str(decoder), str(source), str(output), *extra]
        completed = subprocess.CompletedProcess(argv, 0, stdout=DECODED_CSV,
                                                stderr=report.encode())
        with patch.object(decode_blackbox_recording.subprocess, "run", return_value=completed), \
                patch.object(sys, "argv", argv):
            decode_blackbox_recording.main()
        return json.loads((output / "decode.json").read_text())

    def test_the_declaration_is_opt_in_and_the_default_export_is_untouched(self):
        raw = (MAGIC + b"H Firmware revision:Betaflight 4.2.0 (authored) GENERIC\n"
               + FIELD_NAMES + b"H motorOutput:1000,2000\n" + END)
        with tempfile.TemporaryDirectory() as tmp:
            default = self.run_exporter(Path(tmp), raw=raw)
        self.assertNotIn("motor_output", default)
        # The default source_units keep exactly the adopted native electrical/time declaration.
        self.assertEqual(default["source_units"], {"time": "us", "vbatLatest": "cV",
                                                   "amperageLatest": "cA"})
        with tempfile.TemporaryDirectory() as tmp:
            declared = self.run_exporter(Path(tmp), raw=raw,
                                         extra=["--motor-output", "declared"])
        motor = declared["motor_output"]
        self.assertEqual((motor["declared_low"], motor["declared_high"]), (1000, 2000))
        self.assertEqual(motor["motor_count"], 4)
        self.assertEqual(motor["measured_rpm"], "ABSENT_AT_BETAFLIGHT_4_2_0")
        self.assertEqual(motor["pole_count"], "NOT_APPLICABLE_NO_ERPM_FIELD_AT_THIS_VERSION")
        self.assertIn("not measured rotation", motor["not"])
        # The opt-in adds motor declarations and removes none of the adopted ones.
        self.assertEqual({key: declared["source_units"][key]
                          for key in ("time", "vbatLatest", "amperageLatest")},
                         default["source_units"])
        for column in motor["columns"]:
            self.assertIn("demand_raw", declared["source_units"][column])
        # No CSV column and no decoder argument changed: the recorded scale is header metadata.
        self.assertEqual(declared["main_frames"], default["main_frames"])

    def test_the_declaration_refuses_the_gps_stream(self):
        raw = (MAGIC + b"H Firmware revision:Betaflight 4.2.0 (authored) GENERIC\n"
               + FIELD_NAMES + b"H motorOutput:1000,2000\n" + END)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            self.run_exporter(Path(tmp), raw=raw,
                              extra=["--stream", "gps", "--motor-output", "declared"])


class MotorOutputConversionTest(unittest.TestCase):
    decoded = ("time (us),loopIteration,motor[0],motor[1],motor[2],motor[3],rssi\n"
               "1001,0,1000,1200,1400,1600,1023\n"
               "2001,8,980,2000,1401,1601,1022\n")

    def test_declared_meaning_index_and_source_rows_are_all_preserved(self):
        text, report = convert(self.decoded, SCHEMA, (1000, 2000))
        rows = text.splitlines()
        self.assertEqual(rows[0], "record_time_us,reported_motor_output_demand_0,"
                                  "reported_motor_output_demand_1,reported_motor_output_demand_2,"
                                  "reported_motor_output_demand_3,motor_count,"
                                  "motor_output_declared_low,motor_output_declared_high,"
                                  "motor_output_schema,source_header_hex,source_record_hex")
        first = rows[1].split(",")
        # The writer's per-motor index survives: column k carries motor[k], unreordered.
        self.assertEqual(first[:5], ["1001", "1000", "1200", "1400", "1600"])
        self.assertEqual(first[5:9], ["4", "1000", "2000", SCHEMA])
        # rssi is not named by this schema and is still not lost.
        self.assertEqual(bytes.fromhex(first[9][4:]).decode(),
                         self.decoded.splitlines()[0])
        self.assertEqual(bytes.fromhex(first[10][4:]).decode(),
                         self.decoded.splitlines()[1])
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["motor_count"], 4)
        self.assertEqual(report["source_columns_not_named_by_this_schema"], 2)
        self.assertEqual(report["platform_domain"], "Unknown")
        self.assertEqual(report["measured_rotation"], "NOT_PRESENT_AT_THIS_SOURCE_VERSION")
        self.assertEqual(report["pole_count"], "NOT_APPLICABLE_NO_ERPM_FIELD_AT_THIS_SOURCE_VERSION")
        self.assertEqual(report["common_output"], "NOT_RUN")

    def test_values_outside_the_declared_scale_are_counted_and_kept_never_clamped(self):
        # 980 is below the declared low and 2001 above the declared high; both are real data.
        decoded = ("time (us),motor[0],motor[1]\n"
                   "1001,980,2001\n")
        text, report = convert(decoded, SCHEMA, (1000, 2000))
        self.assertEqual(text.splitlines()[1].split(",")[:3], ["1001", "980", "2001"])
        self.assertEqual(report["motor_values_below_the_declared_low"], 1)
        self.assertEqual(report["motor_values_above_the_declared_high"], 1)
        self.assertEqual(report["rows_with_any_demand_below_the_declared_low"], 1)
        self.assertEqual(report["rows_with_any_demand_above_the_declared_high"], 1)

    def test_several_motors_outside_the_span_in_one_row_is_one_row_and_several_values(self):
        # Three of four motors below the low and two above the high, all in a single record.
        decoded = ("time (us),motor[0],motor[1],motor[2],motor[3]\n"
                   "1001,900,910,920,2500\n"
                   "2001,1500,1500,1500,2600\n")
        _, report = convert(decoded, SCHEMA, (1000, 2000))
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["motor_values_below_the_declared_low"], 3)
        self.assertEqual(report["rows_with_any_demand_below_the_declared_low"], 1)
        self.assertEqual(report["motor_values_above_the_declared_high"], 2)
        self.assertEqual(report["rows_with_any_demand_above_the_declared_high"], 2)

    def test_no_rotation_thrust_or_fraction_is_ever_produced(self):
        text, report = convert(self.decoded, SCHEMA, (1000, 2000))
        for invented in ("rpm", "erpm", "thrust", "fraction", "percent", "pole"):
            with self.subTest(term=invented):
                self.assertNotIn(invented, text.splitlines()[0])
        self.assertIn("not measured rotation", report["not"])
        # The declaration must not silently become a measurement claim.
        self.assertNotIn("measured", report["meaning"])

    def test_important_failures_are_refused_rather_than_repaired(self):
        for broken in (
            "time (us),motor[0]\n",                              # header only, no record
            "time (us),vbatLatest\n1001,2281\n",                 # no motor column
            "time (us),motor[1]\n1001,1000\n",                   # not contiguous from zero
            "time (us),motor[0],motor[0]\n1001,1,1\n",           # duplicate column name
            "time (us),motor[0]\n1001,1000,7\n",                 # ragged row
            "time (us),motor[0]\n2001,1000\n1001,1000\n",        # decreasing boot time
            "time (us),motor[0]\n-1,1000\n",                     # boot time is unsigned
            "time (us),motor[0]\n1001,\n",                       # blank demand
            "time (us),motor[0]\n1001,10.5\n",                   # not an integer the writer holds
            "time (us),motor[0]\n1001,40000\n",                  # outside the stored int16
            "time (us),motor[0]\n1001,-40000\n",
            "TimeUS,motor[0]\n1001,1000\n",                      # a different producer's time column
            'time (us),motor[0]\n1001,"1000"\n',                 # quoted CSV representation
            "",
        ):
            with self.subTest(input=broken), self.assertRaises((ValueError, StopIteration)):
                convert(broken, SCHEMA, (1000, 2000))
        with self.assertRaises(ValueError):                      # another firmware is not this schema
            convert(self.decoded, "betaflight-4.5.1-motor-output", (1000, 2000))
        for scale in ((2000, 1000), (1000, 1000), (0, 70000), (-1, 2000)):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                convert(self.decoded, SCHEMA, scale)


class MotorOutputCommonConnectionTest(unittest.TestCase):
    def test_declared_motor_columns_reach_the_shared_reader(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("MUSUBI_TELEMETRY_READER not supplied; the common connection is NOT_RUN, "
                          "not passed")
        decoded = ("time (us),motor[0],motor[1],rssi\n"
                   "1001,1000,1200,1023\n"
                   "2001,980,1300,1022\n")
        text, report = convert(decoded, SCHEMA, (1000, 2000))
        with tempfile.TemporaryDirectory() as tmp:
            converted = Path(tmp) / "motor.csv"
            converted.write_text(text, encoding="utf-8")
            result = subprocess.run([reader, str(PROFILE), str(converted)],
                                    check=True, capture_output=True)
        common = json.loads(result.stdout)
        onboard = [o for o in common["observations"] if o["channel"] == "onboard"]
        self.assertEqual(len(onboard), report["rows"])
        for index, expected in enumerate(((1000, 1200), (980, 1300))):
            fields = onboard[index]["fields"]
            self.assertEqual(fields["reported_motor_output_demand_0"], expected[0])
            self.assertEqual(fields["reported_motor_output_demand_1"], expected[1])
            self.assertEqual(fields["motor_count"], 2)
        units = common["profile_units"]
        self.assertIn("commanded mixer output".replace(" ", "_"),
                      units["reported_motor_output_demand_0"])
        self.assertIn("NOT_measured_rotation", units["reported_motor_output_demand_0"])
        # The domain the converter reports is the one common output actually carries. The reader
        # maps only family ugv to Ground, so this fpv profile stays Unknown: a bench log is never
        # labelled Air, and no domain is derived from a motor value.
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertEqual(common["platform_domain"], report["platform_domain"])
        self.assertEqual(common["domain_source"], "profile_declared_family")
        self.assertIn(common["domain_source"], report["domain_source_in_common_output"])

    def test_equal_record_times_survive_the_roundtrip_under_the_readers_own_option(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("MUSUBI_TELEMETRY_READER not supplied; the equal-time roundtrip is "
                          "NOT_RUN, not passed")
        # This converter keeps equal boot microseconds; the shared reader refuses them by default
        # rather than dropping them, and carries every one of them under its existing opt-in.
        decoded = ("time (us),motor[0],motor[1]\n"
                   "1001,1000,1200\n"
                   "1001,1010,1210\n"
                   "2001,980,1300\n")
        text, report = convert(decoded, SCHEMA, (1000, 2000))
        self.assertEqual(report["rows_sharing_a_record_time_us_with_the_previous_row"], 1)
        self.assertIn("--allow-equal-time", report["equal_record_time_us_policy"])
        with tempfile.TemporaryDirectory() as tmp:
            converted = Path(tmp) / "motor.csv"
            converted.write_text(text, encoding="utf-8")
            default = subprocess.run([reader, str(PROFILE), str(converted)], capture_output=True)
            self.assertNotEqual(default.returncode, 0)   # refused by name, never a silent drop
            self.assertIn(b"strictly increasing", default.stderr)
            allowed = subprocess.run([reader, str(PROFILE), str(converted), "--allow-equal-time"],
                                     check=True, capture_output=True)
        common = json.loads(allowed.stdout)
        onboard = [o for o in common["observations"] if o["channel"] == "onboard"]
        self.assertEqual(report["rows"], 3)
        self.assertEqual(len(onboard), report["rows"])   # every equal-time row is carried
        self.assertEqual(common["main_rows"], report["rows"])
        self.assertEqual([o["fields"]["reported_motor_output_demand_0"] for o in onboard],
                         [1000, 1010, 980])
        self.assertEqual(common["equal_time_order"], "source-order-only")


class MotorOutputCaseDeclarationTest(unittest.TestCase):
    def test_profile_declares_every_column_the_schema_can_emit(self):
        declared = PROFILE.read_text(encoding="utf-8")
        emitted, _ = convert("time (us)," + ",".join(f"motor[{i}]" for i in range(8)) + "\n"
                             + "1001," + ",".join("1000" for _ in range(8)) + "\n",
                             SCHEMA, (1000, 2000))
        for column in emitted.splitlines()[0].split(","):
            with self.subTest(column=column):
                self.assertIn(f"\n{column} = \"", declared)
        self.assertIn('format = "telemetry_csv_us"', declared)   # the shared reader, not a new one
        self.assertIn('default_clock_basis = "boot_relative"', declared)


if __name__ == "__main__":
    unittest.main()
