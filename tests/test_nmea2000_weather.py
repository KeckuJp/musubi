"""Synthetic classic frames against finite canboat-defined physical fields."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_dronecan_capture import convert_nmea2000_weather

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,extended_id,data_hex\n"


class NmeaWeather(unittest.TestCase):
    def test_heading_and_signed_turn_reuse_common_output(self):
        for reference, raw_rate in [(0, 32000000), (1, -16000000)]:
            heading = bytes([7]) + (10000).to_bytes(2, "little") + (-100).to_bytes(2, "little", signed=True) + (200).to_bytes(2, "little", signed=True) + bytes([reference])
            turn = bytes([7]) + raw_rate.to_bytes(4, "little", signed=True) + bytes([255]*3)
            output, report = convert_nmea2000_weather(HEADER +
                f"10,09f1122a,{heading.hex()}\n10,09f1132a,{turn.hex()}\n", heading=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["decoded_frames"], 2)
            self.assertEqual(float(rows[0]["heading_rad"]), 1)
            self.assertEqual(float(rows[0]["heading_deviation_rad"]), -.01)
            self.assertEqual(float(rows[0]["heading_variation_rad"]), .02)
            self.assertEqual(rows[0]["heading_reference_reported"], ("TRUE_REPORTED", "MAGNETIC_REPORTED")[reference])
            self.assertEqual(float(rows[1]["turn_rate_rad_s"]), raw_rate * 3.125e-8)
            self.assertEqual(rows[1]["heading_rad"], "")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "out.csv"
                    source.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], 2)
                    self.assertEqual(result["observations"][1]["fields"]["turn_rate_rad_s"], raw_rate * 3.125e-8)

    def test_heading_missing_error_and_wrong_length(self):
        output, report = convert_nmea2000_weather(HEADER +
            "1,09f1122a,00ffffff7ffe7fff\n2,09f1132a,00ffffff7fffffff\n3,09fd022a,00\n", heading=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["heading_rad_status"], "NOT_AVAILABLE")
        self.assertEqual(rows[0]["heading_deviation_rad_status"], "NOT_AVAILABLE")
        self.assertEqual(rows[0]["heading_variation_rad_status"], "OUT_OF_RANGE_REPORTED")
        self.assertEqual(rows[0]["heading_reference_reported"], "UNKNOWN_3")
        self.assertEqual(rows[1]["turn_rate_rad_s_status"], "NOT_AVAILABLE")
        self.assertEqual(len(report["unsupported_frames"]), 1)
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(HEADER + "1,09f1122a,00\n", heading=True)

    def test_two_inputs_units_reference_and_common_observation(self):
        for frame, speed, angle in [("0064001027faffff", 1, 1), ("01c800204efbffff", 2, 2)]:
            text = HEADER + "10,09fd022a," + frame + "\n10,15fd080e,ff0001c572ffffff\n"
            output, report = convert_nmea2000_weather(text)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["decoded_frames"], 2)
            self.assertEqual(float(rows[0]["wind_speed_m_s"]), speed)
            self.assertEqual(float(rows[0]["wind_angle_rad"]), angle)
            self.assertEqual(rows[0]["wind_reference_reported"], "APPARENT" if speed == 1 else "TRUE_BOAT")
            self.assertAlmostEqual(float(rows[1]["actual_temperature_k"]), 293.81)
            self.assertEqual(rows[1]["reported_set_temperature_k"], "")
            self.assertEqual(rows[1]["reported_set_temperature_k_status"], "NOT_AVAILABLE")
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_frames_hex"][4:]))[0][2], frame)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "out.csv"
                    source.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], 2)
                    self.assertEqual(result["observations"][0]["fields"]["wind_speed_m_s"], speed)

    def test_battery_status_reuses_the_same_framing_units_and_sentinels(self):
        """PGN 127508 on the adopted path: published canboat samples, signed sentinels, SID last.

        The canboat 4cc490f5 definition orders the fields Instance, Voltage, Current, Temperature,
        SID -- so SID is the *last* byte here, not the first as in the wind/temperature PGNs, and
        Voltage and Current are FIX16, i.e. signed. Device-reported electrical quantities only:
        no state of charge, capacity, chemistry or health is claimed.
        """
        battery_id = f"{(6 << 26) | (127508 << 8) | 60:08x}"
        published = [("001c0a0700ff70ff", 25.88, 0.7, 289.27),
                     ("00330aedff0271ff", 26.11, -1.9, 289.30),
                     ("001e0a0a000671ff", 25.90, 1.0, 289.34)]
        text = HEADER + "".join(
            f"{index + 1},{battery_id},{frame}\n" for index, (frame, *_) in enumerate(published))
        output, report = convert_nmea2000_weather(text, battery=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["decoded_frames"], 3)
        self.assertEqual(report["clock"], "Unknown")
        for row, (frame, volts, amps, kelvin) in zip(rows, published):
            self.assertEqual(row["nmea2000_pgn"], "127508")
            self.assertEqual(row["instance_code"], "0", "Instance is byte 0")
            self.assertEqual(row["sid_code"], "255", "SID is byte 7, and its 255 code is retained")
            self.assertAlmostEqual(float(row["battery_voltage_v"]), volts)
            self.assertAlmostEqual(float(row["battery_current_a"]), amps)
            self.assertAlmostEqual(float(row["battery_temperature_k"]), kelvin)
            self.assertEqual(json.loads(bytes.fromhex(row["source_frames_hex"][4:]))[0][2], frame)
        self.assertLess(float(rows[1]["battery_current_a"]), 0)

        sentinels = HEADER + f"1,{battery_id},07ff7ffe7fffff0c\n"
        row = next(csv.DictReader(io.StringIO(
            convert_nmea2000_weather(sentinels, battery=True)[0])))
        self.assertEqual(row["battery_voltage_v_status"], "NOT_AVAILABLE")
        self.assertEqual(row["battery_current_a_status"], "OUT_OF_RANGE_REPORTED")
        self.assertEqual(row["battery_temperature_k_status"], "NOT_AVAILABLE")
        self.assertEqual([row["battery_voltage_v"], row["battery_current_a"],
                          row["battery_temperature_k"]], ["", "", ""])
        self.assertEqual(row["instance_code"], "7")
        self.assertEqual(row["sid_code"], "12", "a real SID is read from the last byte")
        self.assertNotIn("327", output)

        with self.assertRaises(ValueError):
            convert_nmea2000_weather(HEADER + f"1,{battery_id},001c0a07\n", battery=True)
        _, other = convert_nmea2000_weather(
            HEADER + "1,09fd022a,0064001027faffff\n" + f"2,{battery_id},001c0a0700ff70ff\n",
            battery=True)
        self.assertEqual(other["decoded_frames"], 1)
        self.assertEqual(len(other["unsupported_frames"]), 1)
        self.assertEqual(other["unsupported_frames"][0]["reason"], "unselected PGN")
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(text, heading=True, battery=True)

        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            second = HEADER + f"5,{battery_id},0290091801e070ff\n"
            for converted, expected in ((output, 25.88),
                                        (convert_nmea2000_weather(second, battery=True)[0], 24.48)):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "out.csv"
                    source.write_text(converted)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertAlmostEqual(
                        result["observations"][0]["fields"]["battery_voltage_v"], expected)
                    self.assertEqual(result["profile_units"]["battery_voltage_v"], "V")
                    self.assertIn("reported_source_sign_convention",
                                  result["profile_units"]["battery_current_a"])
                    self.assertTrue(
                        all(o["clock_basis"] == "Unknown" for o in result["observations"]))

    def test_missing_error_and_unselected_frames_not_normal_values(self):
        output, report = convert_nmea2000_weather(HEADER +
            "1,09fd022a,fffffffffeffffff\n2,09fd032a,0000\n")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["wind_speed_m_s"], "")
        self.assertEqual(row["wind_speed_m_s_status"], "NOT_AVAILABLE")
        output, _ = convert_nmea2000_weather(HEADER + "1,09fd022a,00fffffeffffffff\n")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["wind_angle_rad_status"], "OUT_OF_RANGE_REPORTED")
        self.assertEqual(row["wind_reference_reported"], "UNKNOWN_7")
        self.assertEqual(len(report["unsupported_frames"]), 1)
        for body in ("1,09fd022a,0000\n", "2,15fd080e,ff0001c572ffffff\n1,15fd080e,ff0001c572ffffff\n"):
            with self.assertRaises(ValueError):
                convert_nmea2000_weather(HEADER + body)


def attitude_frame(yaw, pitch, roll, *, sid=0, source=14, reserved=0xFF, microseconds=10):
    """One authored PGN 127257 frame: SID, then yaw, pitch, roll - the declared wire order."""
    payload = bytes([sid])
    for raw in (yaw, pitch, roll):
        payload += raw.to_bytes(2, "little", signed=True)
    payload += bytes([reserved])
    return f"{microseconds},{(127257 << 8 | source):x},{payload.hex()}\n"


class NmeaAttitude(unittest.TestCase):
    """PGN 127257 through the adopted passive path; a reported attitude, never a heading."""

    def common(self, output, expected):
        declared = os.environ["MUSUBI_TELEMETRY_READER"]
        if not Path(declared).exists():
            self.fail(f"MUSUBI_TELEMETRY_READER is set to {declared!r}, which does not exist")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([declared,
                str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], expected)
        return common

    def test_published_pin_samples_decode_to_their_stated_values(self):
        text = HEADER + "".join((
            f"1000,{127257 << 8 | 14:x},ff597a51009fffff\n",
            f"2000,{127257 << 8 | 20:x},00ff7f3d0026ffff\n",
            f"3000,{127257 << 8 | 14:x},fffe79590 0a8ffff".replace(" ", "") + "\n"))
        output, report = convert_nmea2000_weather(text, attitude=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["frames"], report["decoded_frames"]), (3, 3))
        self.assertEqual([round(float(row["attitude_yaw_rad"]), 4) if row["attitude_yaw_rad"] else None
                          for row in rows], [3.1321, None, 3.1230])
        self.assertEqual([round(float(row["attitude_pitch_rad"]), 4) for row in rows],
                         [0.0081, 0.0061, 0.0089])
        self.assertEqual([round(float(row["attitude_roll_rad"]), 4) for row in rows],
                         [-0.0097, -0.0218, -0.0088])
        self.assertEqual(rows[1]["attitude_yaw_rad_status"], "NOT_AVAILABLE")
        self.assertEqual(rows[1]["attitude_pitch_rad_status"], "REPORTED")
        self.assertEqual([row["sid_code"] for row in rows], ["255", "0", "255"])
        self.assertEqual([row["source_node_id"] for row in rows], ["14", "20", "14"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-output proof needs the built shared reader; required CI supplies it")
    def test_two_authored_frames_reach_common_output_without_a_heading_claim(self):
        for source, sid, (yaw, pitch, roll) in ((14, 0, (15708, 1000, -2500)),
                                                (37, 9, (-31000, -500, 12345))):
            text = HEADER + attitude_frame(yaw, pitch, roll, sid=sid, source=source)
            output, report = convert_nmea2000_weather(text, attitude=True)
            self.assertEqual(report["decoded_frames"], 1)
            fields = self.common(output, 1)["observations"][0]["fields"]
            self.assertAlmostEqual(fields["attitude_yaw_rad"], yaw * 1e-4)
            self.assertAlmostEqual(fields["attitude_pitch_rad"], pitch * 1e-4)
            self.assertAlmostEqual(fields["attitude_roll_rad"], roll * 1e-4)
            self.assertEqual(fields["nmea2000_pgn"], 127257)
            self.assertEqual(fields["source_node_id"], source)
            self.assertEqual(fields["sid_code"], sid)
            self.assertEqual(fields["record_time_us"], 10)
            self.assertEqual(fields["reserved_field_disposition"], "ALL_BITS_SET_AS_DECLARED")
            self.assertIn("STATES_NO_REFERENCE_FRAME", fields["attitude_reference_basis"])
            self.assertIsNone(fields.get("heading_reference_reported"))
            self.assertIsNone(fields.get("heading_rad"))
            self.assertIn(attitude_frame(yaw, pitch, roll, sid=sid, source=source).strip().split(",")[-1],
                          bytes.fromhex(fields["source_frames_hex"][4:]).decode())
            common = self.common(output, 1)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            units = common["profile_units"]
            self.assertIn("no_true_or_magnetic_reference", units["attitude_yaw_rad"])
            self.assertIn("never_a_heading", units["attitude_reference_basis"].replace("nothing_here_is_a_heading", "never_a_heading"))
            self.assertIn("NONCONFORMANT_RESERVED_BITS_RETAINED", units["reserved_field_disposition"])

    def test_sentinels_reserved_bits_and_malformed_attitude_frames(self):
        for index, name in enumerate(("attitude_yaw_rad", "attitude_pitch_rad", "attitude_roll_rad")):
            for code, status in ((0x7FFF, "NOT_AVAILABLE"), (0x7FFE, "OUT_OF_RANGE_REPORTED")):
                raws = [100, 200, 300]
                raws[index] = code - 65536 if code > 32767 else code
                row = next(csv.DictReader(io.StringIO(convert_nmea2000_weather(
                    HEADER + attitude_frame(*raws), attitude=True)[0])))
                with self.subTest(name=name, status=status):
                    self.assertEqual(row[name + "_status"], status)
                    self.assertEqual(row[name], "")
                    others = [q for q in ("attitude_yaw_rad", "attitude_pitch_rad",
                                          "attitude_roll_rad") if q != name]
                    self.assertTrue(all(row[q + "_status"] == "REPORTED" for q in others))
                    self.assertTrue(all(float(row[q]) != 0 for q in others))
        row = next(csv.DictReader(io.StringIO(convert_nmea2000_weather(
            HEADER + attitude_frame(100, 200, 300, reserved=0x7F), attitude=True)[0])))
        self.assertEqual(row["reserved_field_code"], "127")
        self.assertEqual(row["reserved_field_disposition"], "NONCONFORMANT_RESERVED_BITS_RETAINED")
        self.assertEqual(float(row["attitude_yaw_rad"]), 0.01)
        other = HEADER + "10,09f1122a," + (bytes([7]) + (10000).to_bytes(2, "little")
                                           + bytes(5)).hex() + "\n"
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(other, attitude=True)
        mixed, report = convert_nmea2000_weather(other.replace(HEADER, HEADER + attitude_frame(1, 2, 3)),
                                                 attitude=True)
        self.assertEqual(report["decoded_frames"], 1)
        self.assertEqual([entry["reason"] for entry in report["unsupported_frames"]], ["unselected PGN"])
        for bad in (attitude_frame(1, 2, 3)[:-1].rsplit(",", 1)[0] + ",00112233\n",  # wrong length
                    attitude_frame(1, 2, 3).replace(",", ";", 1),                    # malformed row
                    attitude_frame(1, 2, 3) + attitude_frame(1, 2, 3, microseconds=1)):  # decreasing
            with self.subTest(bad=bad[:24]), self.assertRaises(ValueError):
                convert_nmea2000_weather(HEADER + bad, attitude=True)
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(HEADER + attitude_frame(1, 2, 3), attitude=True, heading=True)


def heading_frame(heading, deviation, variation, *, sid=0, reference=0, source=14,
                  microseconds=10):
    """One authored PGN 127250 frame: SID, heading (unsigned), deviation, variation (signed)."""
    payload = bytes([sid]) + heading.to_bytes(2, "little")
    payload += deviation.to_bytes(2, "little", signed=True)
    payload += variation.to_bytes(2, "little", signed=True) + bytes([reference])
    return f"{microseconds},{(127250 << 8 | source):x},{payload.hex()}\n"


class NmeaReservedSentinelBoundary(unittest.TestCase):
    """The pinned source reserves three top-of-range codes, not two, for every numeric field.

    The adopted groups withheld only the maximum and maximum-minus-one, so a declared *reserved*
    code reached common output as if it were a measurement. This covers the shared helper's
    boundary on both signs, in an already adopted group rather than only the new one.
    """

    def test_reserved_code_is_withheld_for_unsigned_and_signed_fields(self):
        text = HEADER + "".join((
            heading_frame(65533, 32765, -1, microseconds=10),
            heading_frame(65532, 32764, 32766, microseconds=20)))
        rows = list(csv.DictReader(io.StringIO(
            convert_nmea2000_weather(text, heading=True)[0])))
        self.assertEqual((rows[0]["heading_rad_status"],
                          rows[0]["heading_deviation_rad_status"]),
                         ("RESERVED_RANGE_REPORTED", "RESERVED_RANGE_REPORTED"))
        self.assertEqual((rows[0]["heading_rad"], rows[0]["heading_deviation_rad"]), ("", ""))
        self.assertEqual(rows[0]["heading_variation_rad_status"], "REPORTED")
        self.assertAlmostEqual(float(rows[0]["heading_variation_rad"]), -1 * 1e-4)
        self.assertEqual((rows[1]["heading_rad_status"],
                          rows[1]["heading_deviation_rad_status"]), ("REPORTED", "REPORTED"))
        self.assertAlmostEqual(float(rows[1]["heading_rad"]), 65532 * 1e-4)
        self.assertAlmostEqual(float(rows[1]["heading_deviation_rad"]), 32764 * 1e-4)
        self.assertEqual(rows[1]["heading_variation_rad_status"], "OUT_OF_RANGE_REPORTED")
        self.assertTrue(all(row["source_frames_hex"].startswith("hex:") for row in rows))

    def test_the_same_rule_holds_for_the_other_adopted_scalar_groups(self):
        battery = (bytes([3]) + (32765).to_bytes(2, "little", signed=True)
                   + (100).to_bytes(2, "little", signed=True)
                   + (65533).to_bytes(2, "little") + bytes([0]))
        text = HEADER + f"10,{127508 << 8 | 14:x},{battery.hex()}\n"
        row = next(csv.DictReader(io.StringIO(
            convert_nmea2000_weather(text, battery=True)[0])))
        self.assertEqual(row["battery_voltage_v_status"], "RESERVED_RANGE_REPORTED")
        self.assertEqual(row["battery_voltage_v"], "")
        self.assertEqual(row["battery_temperature_k_status"], "RESERVED_RANGE_REPORTED")
        self.assertEqual(row["battery_current_a_status"], "REPORTED")
        wind = (bytes([0]) + (65533).to_bytes(2, "little") + (1000).to_bytes(2, "little")
                + bytes([2, 255, 255]))
        text = HEADER + f"10,{130306 << 8 | 14:x},{wind.hex()}\n"
        row = next(csv.DictReader(io.StringIO(convert_nmea2000_weather(text)[0])))
        self.assertEqual(row["wind_speed_m_s_status"], "RESERVED_RANGE_REPORTED")
        self.assertEqual(row["wind_angle_rad_status"], "REPORTED")
        self.assertEqual(row["wind_reference_reported"], "APPARENT")

def engine_frame(instance, speed, boost, trim, *, source=14, reserved=b"\xff\xff",
                 microseconds=10):
    """One authored PGN 127488 frame in the declared order: Instance, Speed, Boost, Tilt/Trim."""
    payload = bytes([instance]) + speed.to_bytes(2, "little") + boost.to_bytes(2, "little")
    payload += trim.to_bytes(1, "little", signed=True) + reserved
    return f"{microseconds},{(127488 << 8 | source):x},{payload.hex()}\n"


class NmeaEngineRapid(unittest.TestCase):
    """PGN 127488 through the adopted passive path: reported engine state, never thrust."""

    def common(self, output, expected):
        declared = os.environ["MUSUBI_TELEMETRY_READER"]
        if not Path(declared).exists():
            self.fail(f"MUSUBI_TELEMETRY_READER is set to {declared!r}, which does not exist")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([declared,
                str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], expected)
        return common

    def test_published_pin_samples_decode_to_their_stated_values(self):
        text = HEADER + "".join((
            f"1000,{127488 << 8 | 14:x},000000ffffffffff\n",   # speed 0.0, boost ~, tiltTrim -1
            f"2000,{127488 << 8 | 16:x},00920cffff7fffff\n",   # speed 804.5, boost ~, tiltTrim ~
            f"3000,{127488 << 8 | 16:x},00470cffff7fffff\n"))  # speed printed 785.8 by canboat
        output, report = convert_nmea2000_weather(text, engine=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["frames"], report["decoded_frames"]), (3, 3))
        self.assertEqual([float(row["engine_speed_rpm"]) for row in rows], [0.0, 804.5, 785.75])
        self.assertEqual(rows[2]["engine_speed_rpm_status"], "REPORTED")
        self.assertTrue(all(row["engine_boost_pressure_pa_status"] == "NOT_AVAILABLE"
                            for row in rows))
        self.assertTrue(all(row["engine_boost_pressure_pa"] == "" for row in rows))
        self.assertEqual((rows[0]["engine_tilt_trim_percent"],
                          rows[0]["engine_tilt_trim_percent_status"]), ("-1", "REPORTED"))
        self.assertEqual(rows[1]["engine_tilt_trim_percent_status"], "NOT_AVAILABLE")
        self.assertTrue(all(row["engine_instance_reported"] == "Single Engine or Dual Engine Port"
                            for row in rows))
        self.assertTrue(all(row["sid_code"] == "" for row in rows))
        self.assertEqual([row["source_node_id"] for row in rows], ["14", "16", "16"])

    def test_both_declared_instances_and_the_three_sentinel_levels(self):
        text = HEADER + "".join((
            engine_frame(1, 4000, 12, 25, source=20, microseconds=10),
            engine_frame(7, 65534, 65533, 126, source=21, microseconds=20),
            engine_frame(0, 65533, 65535, 125, source=22, microseconds=30)))
        output, report = convert_nmea2000_weather(text, engine=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["decoded_frames"], 3)
        self.assertEqual(rows[0]["engine_instance_reported"], "Dual Engine Starboard")
        self.assertEqual(rows[1]["engine_instance_reported"], "UNKNOWN_INSTANCE_CODE_7")
        self.assertEqual(rows[1]["instance_code"], "7")
        self.assertEqual((rows[0]["engine_speed_rpm"], rows[0]["engine_boost_pressure_pa"],
                          rows[0]["engine_tilt_trim_percent"]), ("1000.0", "1200.0", "25"))
        self.assertEqual((rows[1]["engine_speed_rpm_status"],
                          rows[1]["engine_boost_pressure_pa_status"],
                          rows[1]["engine_tilt_trim_percent_status"]),
                         ("OUT_OF_RANGE_REPORTED", "RESERVED_RANGE_REPORTED", "OUT_OF_RANGE_REPORTED"))
        self.assertEqual((rows[2]["engine_speed_rpm_status"],
                          rows[2]["engine_boost_pressure_pa_status"],
                          rows[2]["engine_tilt_trim_percent_status"]),
                         ("RESERVED_RANGE_REPORTED", "NOT_AVAILABLE", "RESERVED_RANGE_REPORTED"))
        self.assertTrue(all(rows[1][name] == "" for name in
                            ("engine_speed_rpm", "engine_boost_pressure_pa",
                             "engine_tilt_trim_percent")))
        self.assertIn("NOT_BY_A_DOCUMENTED_SCHEMA_DEFAULT", rows[0]["engine_trim_scale_basis"])

    def test_unselected_frames_reserved_bits_and_malformed_length(self):
        text = (HEADER + engine_frame(0, 100, 200, 5)
                + f"20,{127257 << 8 | 14:x},00102030405060ff\n")
        output, report = convert_nmea2000_weather(text, engine=True)
        self.assertEqual((report["frames"], report["decoded_frames"]), (2, 1))
        self.assertEqual(len(report["unsupported_frames"]), 1)
        self.assertEqual(report["unsupported_frames"][0]["reason"], "unselected PGN")
        kept = convert_nmea2000_weather(HEADER + engine_frame(0, 100, 200, 5,
                                                             reserved=b"\x00\xff"), engine=True)[0]
        row = next(csv.DictReader(io.StringIO(kept)))
        self.assertEqual(row["reserved_field_disposition"], "NONCONFORMANT_RESERVED_BITS_RETAINED")
        ok = next(csv.DictReader(io.StringIO(convert_nmea2000_weather(
            HEADER + engine_frame(0, 100, 200, 5), engine=True)[0])))
        self.assertEqual(ok["reserved_field_disposition"], "ALL_BITS_SET_AS_DECLARED")
        for bad in (f"10,{127488 << 8 | 14:x},00920cffff7fff\n",       # seven bytes
                    f"10,{127488 << 8 | 14:x},00920cffff7fffffff\n"):  # nine bytes
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                convert_nmea2000_weather(HEADER + bad, engine=True)
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(HEADER + engine_frame(0, 1, 1, 1), engine=True, attitude=True)
        with self.assertRaises(ValueError):
            convert_nmea2000_weather(HEADER + f"10,{127257 << 8 | 14:x},00102030405060ff\n",
                                     engine=True)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-output proof needs the built shared reader; required CI supplies it")
    def test_two_authored_frames_reach_common_output_with_declared_units(self):
        for source, instance, (speed, boost, trim) in ((14, 0, (2400, 1013, -30)),
                                                       (37, 1, (9000, 2500, 64))):
            text = HEADER + engine_frame(instance, speed, boost, trim, source=source)
            output, report = convert_nmea2000_weather(text, engine=True)
            self.assertEqual(report["decoded_frames"], 1)
            fields = self.common(output, 1)["observations"][0]["fields"]
            self.assertAlmostEqual(fields["engine_speed_rpm"], speed * 0.25)
            self.assertAlmostEqual(fields["engine_boost_pressure_pa"], boost * 100.0)
            self.assertEqual(fields["engine_tilt_trim_percent"], trim)
            self.assertEqual(fields["engine_instance_reported"],
                             "Single Engine or Dual Engine Port" if instance == 0
                             else "Dual Engine Starboard")

    def test_the_engine_selector_actually_routes_through_the_command_line(self):
        for protocol, text in (("nmea2000-engine-rapid", HEADER + engine_frame(0, 2400, 1013, -30)),
                               ("nmea2000-attitude",
                                HEADER + f"10,{127257 << 8 | 14:x},00102030405060ff\n")):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                base.joinpath("in.csv").write_text(text)
                subprocess.run([sys.executable, str(ROOT / "scripts/convert_dronecan_capture.py"),
                    str(base / "in.csv"), str(base / "out"), "--protocol", protocol],
                    check=True, capture_output=True)
                with (base / "out" / "observations.csv").open() as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
