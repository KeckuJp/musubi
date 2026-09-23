"""Authored Mission Planner tlog->CSV exports, shaped exactly as the pinned writer produces them.

Row shape is taken from MissionPlanner 1.3.82 ca3731f5c19f10f32740fe1519c01b4f444d715a:
`Log/MavlinkLog.cs` BUT_convertcsv_Click writes `lastlogread.ToString("yyyy-MM-ddTHH:mm:ss.fff") + "," +
DebugPacket(packet, ref text, false, ",")`, and MAVLinkInterface.DebugPacket writes eight width-padded
hexadecimal header fields, the struct name, `fieldName,value` pairs, then a `,sig <b64>,Len,<n>,crc16,<n>`
tail. Units come from mavlink/c_library_v2 a3661c0a85dc1de1826f5c95863048af3e2b0d04.

No exporter is executed and no real or vendor export is used: every input here is authored.
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

from scripts.convert_gcs_tlog_csv import (GIMBAL_MESSAGE, SOURCE_FORMAT, SOURCE_FORMATS,
                                          STORAGE_MESSAGE, convert)

ROOT = Path(__file__).resolve().parents[1]
ATTITUDE_PROFILE = ROOT / "profiles/declared/gcs-csv-reuse/attitude-profile.toml"
STATUS_PROFILE = ROOT / "profiles/declared/gcs-csv-reuse/status-profile.toml"
GIMBAL_PROFILE = (ROOT
                  / "profiles/declared/gcs-csv-reuse/gimbal-device-status-profile.toml")
STORAGE_PROFILE = (ROOT
                   / "profiles/declared/gcs-csv-reuse/storage-information-profile.toml")
SCRIPT = ROOT / "scripts/convert_gcs_tlog_csv.py"
CAPTURE = 1_789_000_000_000_000
ATTITUDE_HEADER = "FD,1C, 0, 0,2A, A, 1,    1E"
STATUS_HEADER = "FD,1F, 0, 0,2B, A, 1,     1"
GIMBAL_HEADER = "FD,31, 0, 0,2C, A, 1,   11D"
STORAGE_HEADER = "FD,3D, 0, 0,2D, A, 1,   105"
TAIL = ",sig ,Len,40,crc16,4242"


def row(header, struct, pairs, *, stamp="2026-09-20T11:22:33.456", tail=TAIL):
    """One exported row: stamp, hex header, struct name, name/value pairs, writer tail."""
    body = ",".join(f"{name},{value}" for name, value in pairs)
    return f"{stamp},{header},{struct},{body},{tail}"


def attitude(boot_ms, roll, pitch, yaw, rollspeed, pitchspeed, yawspeed, **kwargs):
    return row(ATTITUDE_HEADER, "mavlink_attitude_t",
               (("time_boot_ms", boot_ms), ("roll", roll), ("pitch", pitch), ("yaw", yaw),
                ("rollspeed", rollspeed), ("pitchspeed", pitchspeed), ("yawspeed", yawspeed)), **kwargs)


def sys_status(voltage_mv, current_ca, remaining_pct, **kwargs):
    return row(STATUS_HEADER, "mavlink_sys_status_t",
               (("onboard_control_sensors_present", 1467087), ("voltage_battery", voltage_mv),
                ("current_battery", current_ca), ("battery_remaining", remaining_pct),
                ("drop_rate_comm", 0)), **kwargs)


def gimbal(boot_ms, failure_flags, vx, vy, vz, *, flags=16, device_id=2, **kwargs):
    """One authored GIMBAL_DEVICE_ATTITUDE_STATUS row, in the writer's own field order.

    `q[4]` is always present, because the message always carries it and the struct always declares
    it. `DebugPacket` writes an array as `field.Name + delimeter`, then content only for `byte[]`,
    `char[]` or `short[]` — there is no `float[]` branch — then `delimeter` again, so a `float[4]`
    is written as `q,,`: the name with an empty value. Field order is the declaration order of
    `mavlink_gimbal_device_attitude_status_t` at `ca3731f5`, which is what `Type.GetFields()` walks.
    """
    pairs = [("time_boot_ms", boot_ms), ("q", ""), ("angular_velocity_x", vx),
             ("angular_velocity_y", vy), ("angular_velocity_z", vz),
             ("failure_flags", failure_flags), ("flags", flags),
             ("target_system", 1), ("target_component", 154)]
    if device_id is not None:
        pairs += [("delta_yaw", "NaN"), ("delta_yaw_velocity", "NaN"),
                  ("gimbal_device_id", device_id)]
    return row(GIMBAL_HEADER, GIMBAL_MESSAGE, tuple(pairs), **kwargs)


def storage(boot_ms, status, *, total=32768.0, used=1024.5, available=31743.5, read=41.5,
            write=22.25, storage_id=1, count=2, kind=3, name="microSD 1", usage=None, **kwargs):
    """One authored STORAGE_INFORMATION row, in the writer's own field order.

    All eleven fields the pinned exporter's `mavlink_storage_information_t` declares are present at
    both releases, so a real row always carries them. `name` is `byte[]` in that struct, so
    `DebugPacket` writes the characters up to the first zero byte — the text itself — and an empty
    name is written as `name,,`. `storage_usage` is absent because neither pinned build declares it;
    it is included only when a test is exercising a hypothetical newer build.
    """
    pairs = [("time_boot_ms", boot_ms), ("total_capacity", total), ("used_capacity", used),
             ("available_capacity", available), ("read_speed", read), ("write_speed", write),
             ("storage_id", storage_id), ("storage_count", count), ("status", status),
             ("type", kind), ("name", name)]
    if usage is not None:
        pairs.append(("storage_usage", usage))
    return row(STORAGE_HEADER, STORAGE_MESSAGE, tuple(pairs), **kwargs)


class GcsTlogCsvTests(unittest.TestCase):
    def rows_of(self, text, selected, **kwargs):
        output, report = convert(text, selected, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report, output

    def observations(self, output, profile):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            data = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(source), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
        self.assertEqual(data["platform_domain"], "Unknown", "no vehicle is inferred from an export")
        return data


    def test_attitude_rows_carry_dialect_quantities_and_the_boot_clock(self):
        text = "\n".join([
            attitude(1000, 0.25, -0.5, 1.75, 0.01, -0.02, 0.03),
            row(STATUS_HEADER, "mavlink_heartbeat_t", (("custom_mode", 0), ("type", 2))),
            attitude(2500, -0.125, 0.0625, -3.0, 0.0, 0.5, -0.25),
            attitude(2500, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),  # duplicate boot time is allowed, not merged
        ]) + "\n"
        rows, report, _ = self.rows_of(text, "mavlink_attitude_t")
        self.assertEqual((report["source_rows"], report["selected_rows"], report["unselected_rows"]),
                         (4, 3, 1), "an unselected struct is counted, never guessed at")
        self.assertEqual([r["record_time_us"] for r in rows], ["1000000", "2500000", "2500000"])
        self.assertEqual(report["clock"], "Unknown")
        self.assertIn("BOOT_MS", report["clock_basis"])
        self.assertIn("NEVER_CONVERTED_TO_UTC", report["exporter_local_time_basis"])
        first = rows[0]
        self.assertEqual(float(first["reported_roll_rad"]), 0.25)
        self.assertEqual(float(first["reported_pitch_rad"]), -0.5)
        self.assertEqual(float(first["reported_yaw_rad"]), 1.75)
        self.assertEqual(float(first["reported_angular_velocity_x_rad_s"]), 0.01)
        self.assertEqual(float(first["reported_angular_velocity_y_rad_s"]), -0.02)
        self.assertEqual(float(first["reported_angular_velocity_z_rad_s"]), 0.03)
        self.assertEqual((first["source_msgid"], first["source_sysid"], first["source_compid"]),
                         ("30", "10", "1"))
        self.assertEqual(first["source_local_time_text"], "2026-09-20T11:22:33.456")
        self.assertEqual(bytes.fromhex(first["source_row_hex"][4:]).decode(), text.splitlines()[0])
        self.assertEqual(first["exporter_pin"], "missionplanner-1.3.82-ca3731f5")
        self.assertEqual(first["dialect_pin"], "c_library_v2-a3661c0a")

    def test_sys_status_converts_the_dialect_units_and_withholds_its_sentinels(self):
        text = "\n".join([
            sys_status(12600, 1550, 87, stamp="2026-09-20T09:00:00.001"),
            sys_status(65535, -1, -1, stamp="2026-09-20T09:00:01.002"),
        ]) + "\n"
        rows, report, output = self.rows_of(text, "mavlink_sys_status_t", capture_time_us=CAPTURE)
        self.assertEqual((report["selected_rows"], report["withheld_values"]), (2, 3))
        self.assertEqual(float(rows[0]["electrical_voltage_v"]), 12.6)
        self.assertAlmostEqual(float(rows[0]["electrical_current_a"]), 15.5)
        self.assertAlmostEqual(float(rows[0]["battery_remaining_fraction"]), 0.87)
        self.assertEqual(rows[0]["measurement_status"], "REPORTED")
        self.assertEqual((rows[1]["electrical_voltage_v"], rows[1]["electrical_current_a"],
                          rows[1]["battery_remaining_fraction"]), ("", "", ""))
        self.assertEqual(rows[1]["measurement_status"], "UNAVAILABLE_NOT_SENT_BY_AUTOPILOT")
        self.assertEqual([r["record_time_us"] for r in rows], [str(CAPTURE)] * 2)
        self.assertIn("CALLER_DECLARED", report["clock_basis"])
        self.assertEqual(rows[0]["source_msgid"], "1")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_both_selections_reach_common_observations(self):
        attitude_rows, _, attitude_output = self.rows_of(
            attitude(4000, 0.5, -0.25, 2.0, -0.75, 0.125, 0.0) + "\n", "mavlink_attitude_t")
        common = self.observations(attitude_output, ATTITUDE_PROFILE)
        self.assertEqual(common["main_rows"], 1)
        fields = common["observations"][0]["fields"]
        self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
        self.assertEqual(common["observations"][0]["t_ms"], 4000)
        self.assertEqual(fields["reported_roll_rad"], 0.5)
        self.assertEqual(fields["reported_angular_velocity_x_rad_s"], -0.75)
        self.assertIn("dialect_attitude_roll", common["profile_units"]["reported_roll_rad"])
        self.assertIn("never_converted_to_utc", common["profile_units"]["source_local_time_text"])

        _, _, status_output = self.rows_of(sys_status(11400, 2075, 42) + "\n",
                                          "mavlink_sys_status_t", capture_time_us=CAPTURE)
        common = self.observations(status_output, STATUS_PROFILE)
        fields = common["observations"][0]["fields"]
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(fields["electrical_voltage_v"], 11.4)
        self.assertAlmostEqual(fields["electrical_current_a"], 20.75)
        self.assertAlmostEqual(fields["battery_remaining_fraction"], 0.42)
        self.assertIn("uint16max_means_not_sent", common["profile_units"]["electrical_voltage_v"])


    def test_the_declared_export_settings_are_required_and_checked(self):
        good = attitude(1000, 0.25, -0.5, 1.75, 0.01, -0.02, 0.03) + "\n"
        with self.assertRaises(ValueError):
            convert(good, "mavlink_attitude_t", decimal_separator="comma")
        comma = attitude(1000, "0,25", "-0,5", "1,75", "0,01", "-0,02", "0,03") + "\n"
        with self.assertRaises(ValueError):
            convert(comma, "mavlink_attitude_t")
        with self.assertRaises(ValueError):
            convert(good, "mavlink_attitude_t", capture_time_us=CAPTURE)
        with self.assertRaises(ValueError):
            convert(sys_status(12000, 100, 50) + "\n", "mavlink_sys_status_t")
        for unsupported in ("mavlink_gps_raw_int_t", "attitude", ""):
            with self.assertRaises(ValueError):
                convert(good, unsupported)

    def test_malformed_missing_and_out_of_shape_rows_are_refused(self):
        cases = {
            "tail": attitude(1000, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1).replace(TAIL, ",extra"),
            "stamp": attitude(1000, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, stamp="2026-09-20 11:22:33"),
            "header": attitude(1000, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1).replace("FD,1C", "FD,ZZ"),
            "missing": row(ATTITUDE_HEADER, "mavlink_attitude_t",
                           (("time_boot_ms", 1000), ("roll", 0.1), ("pitch", 0.1), ("yaw", 0.1),
                            ("rollspeed", 0.1), ("pitchspeed", 0.1))),
            "clock": attitude(-5, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1),
            "clock_range": attitude(2 ** 32, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1),
            "quoted": attitude(1000, '"0.1"', 0.1, 0.1, 0.1, 0.1, 0.1),
            "value": attitude(1000, "NaN?", 0.1, 0.1, 0.1, 0.1, 0.1),
            "short": "2026-09-20T11:22:33.456,FD,1C,mavlink_attitude_t",
        }
        for name, text in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                convert(text + "\n", "mavlink_attitude_t")
        with self.assertRaises(ValueError):
            convert(attitude(5000, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1) + "\n"
                    + attitude(1000, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1) + "\n", "mavlink_attitude_t")
        with self.assertRaises(ValueError):
            convert(row(STATUS_HEADER, "mavlink_heartbeat_t", (("type", 2),)) + "\n",
                    "mavlink_attitude_t")

    def test_non_finite_and_non_float32_quantities_are_refused(self):
        for token in ("1e999", "-1e999", "1e39", "-3.5e38", "1e-50"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                convert(attitude(1000, token, 0.1, 0.1, 0.1, 0.1, 0.1) + "\n", "mavlink_attitude_t")
        rows, _, _ = self.rows_of(attitude(1000, "3.4028234e38", "-3.4028234e38", 0.0, "1.4e-45",
                                           -0.5, 0.25) + "\n", "mavlink_attitude_t")
        self.assertEqual(float(rows[0]["reported_yaw_rad"]), 0.0)
        self.assertEqual(float(rows[0]["reported_angular_velocity_y_rad_s"]), -0.5)

    def test_dialect_integer_domains_and_the_documented_range_are_enforced(self):
        for voltage, current, remaining in ((70000, 1550, 87), (12600, 40000, 87), (12600, 1550, 200),
                                            (12600, 1550, 101), (-5, 1550, 87), (12600, -40000, 87)):
            with self.subTest(values=(voltage, current, remaining)), self.assertRaises(ValueError):
                convert(sys_status(voltage, current, remaining) + "\n", "mavlink_sys_status_t",
                        capture_time_us=CAPTURE)
        rows, report, _ = self.rows_of("\n".join([sys_status(12600, -2500, 0),
                                                  sys_status(65534, 32767, 100)]) + "\n",
                                       "mavlink_sys_status_t", capture_time_us=CAPTURE)
        self.assertAlmostEqual(float(rows[0]["electrical_current_a"]), -25.0)
        self.assertEqual(float(rows[0]["battery_remaining_fraction"]), 0.0)
        self.assertAlmostEqual(float(rows[1]["electrical_voltage_v"]), 65.534)
        self.assertAlmostEqual(float(rows[1]["battery_remaining_fraction"]), 1.0)
        self.assertEqual(report["withheld_values"], 0)

    def test_the_writers_closing_tail_must_be_exact(self):
        good = attitude(1000, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03)
        broken = {
            "crc16 pair removed": good.replace(",crc16,4242", ""),   # Codex's probe
            "crc16 value removed": good.replace(",crc16,4242", ",crc16"),
            "crc16 label renamed": good.replace("crc16", "crc"),
            "Len label renamed": good.replace(",Len,", ",Length,"),
            "labels swapped": good.replace(",Len,40,crc16,4242", ",crc16,4242,Len,40"),
            "Len not decimal": good.replace(",Len,40,", ",Len,0x28,"),
            "Len implausible": good.replace(",Len,40,", ",Len,4,"),
            "crc16 above uint16": good.replace(",crc16,4242", ",crc16,70000"),
            "sig not base64": good.replace(",sig ,", ",sig not_b64!,"),
            "tail dropped": good.replace(TAIL, ""),
        }
        for name, text in broken.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                convert(text + "\n", "mavlink_attitude_t")
        rows, _, _ = self.rows_of(good.replace(",sig ,", ",sig AAECAwQFBgc=,") + "\n",
                                  "mavlink_attitude_t")
        self.assertEqual(len(rows), 1)

    def test_the_struct_name_must_agree_with_the_hex_msgid_and_header_domains(self):
        mislabelled = attitude(1000, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03).replace(",    1E,", ",     1,")
        with self.assertRaises(ValueError):
            convert(mislabelled + "\n", "mavlink_attitude_t")
        wrong_status = sys_status(12600, 1550, 87).replace(",     1,", ",    1E,")
        with self.assertRaises(ValueError):
            convert(wrong_status + "\n", "mavlink_sys_status_t", capture_time_us=CAPTURE)
        for broken in (attitude(1000, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03).replace("FD,1C", "AB,1C"),
                       attitude(1000, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03).replace("FD,1C", "FD,1FF"),
                       attitude(1000, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03).replace(",2A, A,", ",2A,1FF,")):
            with self.subTest(row=broken[:40]), self.assertRaises(ValueError):
                convert(broken + "\n", "mavlink_attitude_t")

    def test_each_selector_accepts_reordered_and_unknown_extra_fields(self):
        reordered_attitude = row(ATTITUDE_HEADER, "mavlink_attitude_t",
                                 (("yawspeed", 0.875), ("roll", -1.25), ("future_field", 7),
                                  ("pitchspeed", -0.375), ("time_boot_ms", 9100), ("yaw", 2.5),
                                  ("rollspeed", 0.125), ("pitch", 0.625)))
        rows, report, _ = self.rows_of(reordered_attitude + "\n", "mavlink_attitude_t")
        self.assertEqual(rows[0]["record_time_us"], "9100000")
        self.assertEqual(float(rows[0]["reported_roll_rad"]), -1.25)
        self.assertEqual(float(rows[0]["reported_pitch_rad"]), 0.625)
        self.assertEqual(float(rows[0]["reported_angular_velocity_z_rad_s"]), 0.875)
        self.assertEqual(report["selected_rows"], 1)
        self.assertIn("future_field", bytes.fromhex(rows[0]["source_row_hex"][4:]).decode(),
                      "an unknown field stays in the retained row")

        reordered_status = row(STATUS_HEADER, "mavlink_sys_status_t",
                               (("battery_remaining", 63), ("errors_count1", 0),
                                ("voltage_battery", 14850), ("current_battery", 875),
                                ("future_field", "abc")))
        rows, report, _ = self.rows_of(reordered_status + "\n", "mavlink_sys_status_t",
                                       capture_time_us=CAPTURE)
        self.assertAlmostEqual(float(rows[0]["electrical_voltage_v"]), 14.85)
        self.assertAlmostEqual(float(rows[0]["electrical_current_a"]), 8.75)
        self.assertAlmostEqual(float(rows[0]["battery_remaining_fraction"]), 0.63)
        self.assertEqual(rows[0]["measurement_status"], "REPORTED")
        self.assertIn("future_field", bytes.fromhex(rows[0]["source_row_hex"][4:]).decode())

    def test_unselected_rows_are_counted_without_claiming_they_were_decoded(self):
        text = "\n".join([
            row(STATUS_HEADER, "mavlink_heartbeat_t", (("type", 2), ("custom_mode", "1e999"))),
            attitude(1000, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03),
        ]) + "\n"
        rows, report, _ = self.rows_of(text, "mavlink_attitude_t")
        self.assertEqual((report["source_rows"], report["selected_rows"], report["unselected_rows"]),
                         (2, 1, 1))
        self.assertEqual(len(rows), 1, "only the selected row is decoded")
        self.assertEqual(report["withheld_values"], 0)

    def test_the_cli_hands_its_user_the_accounting_without_echoing_records(self):
        text = "\n".join([attitude(2000, 0.5, 0.25, -1.0, 0.1, 0.2, 0.3),
                          row(STATUS_HEADER, "mavlink_heartbeat_t", (("type", 2),))]) + "\n"
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "2026-09-20 11-22-33.csv"
            source.write_text(text)
            target = Path(work) / "observations.csv"
            report_path = Path(work) / "report.json"
            done = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                                   "--source-format", SOURCE_FORMAT, "--selected-message",
                                   "mavlink_attitude_t", "--decimal-separator", "dot",
                                   "--report", str(report_path)], check=True, capture_output=True)
            for text_out in (done.stderr.decode(), report_path.read_text()):
                report = json.loads(text_out.strip())
                self.assertEqual((report["source_rows"], report["selected_rows"],
                                  report["unselected_rows"], report["withheld_values"]), (2, 1, 1, 0))
                self.assertEqual(report["selected_message"], "mavlink_attitude_t")
                self.assertEqual(report["exporter_pin"], "missionplanner-1.3.82-ca3731f5")
                self.assertIn("BOOT_MS", report["clock_basis"])
                self.assertNotIn("mavlink_heartbeat_t", text_out)
                self.assertNotIn("2026-09-20T", text_out)
                self.assertNotIn("0.25", text_out)
            self.assertEqual(done.stdout, b"")

    def test_the_cli_writes_the_same_output_and_never_overwrites(self):
        text = attitude(7000, 0.5, 0.25, -1.0, 0.1, 0.2, 0.3) + "\n"
        expected, _, _ = self.rows_of(text, "mavlink_attitude_t")
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "2026-09-20 11-22-33.csv"
            source.write_text(text)
            target = Path(work) / "observations.csv"
            command = [os.sys.executable, str(SCRIPT), str(source), str(target),
                       "--source-format", SOURCE_FORMAT,
                       "--selected-message", "mavlink_attitude_t", "--decimal-separator", "dot"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(list(csv.DictReader(io.StringIO(target.read_text()))), expected)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            refused = subprocess.run(command[:-2] + ["--decimal-separator", "comma"],
                                     capture_output=True)
            self.assertEqual(refused.returncode, 2)
            self.assertIn(b"no successful conversion claim", refused.stderr)

    def test_the_reported_digest_binds_a_crlf_export_as_written(self):
        """This .NET writer ends rows with CRLF, so the digest must be of those bytes, not of an LF copy."""
        text = "".join(attitude(index * 500, 0.5, 0.25, -1.0, 0.1, 0.2, 0.3) + "\r\n"
                       for index in range(3))
        raw = text.encode()
        self.assertIn(b"\r\n", raw)
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "2026-09-20 11-22-33.csv"
            source.write_bytes(raw)
            target = Path(work) / "observations.csv"
            done = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                                   "--source-format", SOURCE_FORMAT, "--selected-message",
                                   "mavlink_attitude_t", "--decimal-separator", "dot"],
                                  check=True, capture_output=True)
            report = json.loads(done.stderr.decode())
            self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertNotEqual(report["source_sha256"],
                                "sha256:" + hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                                "an LF-normalised digest would not bind the exported file")
            rows = list(csv.DictReader(io.StringIO(target.read_text())))
            self.assertEqual([entry["record_time_us"] for entry in rows], ["0", "500000", "1000000"])
            self.assertEqual(rows[0]["reported_pitch_rad"], "0.25")
            self.assertNotIn("0d0a", rows[0]["source_row_hex"])
            self.assertEqual(report["selected_rows"], 3)

    def test_the_declared_release_selector_only_changes_the_recorded_pin(self):
        """1.3.81 and 1.3.82 write these two messages with the same functions, so only the pin differs."""
        self.assertEqual(SOURCE_FORMATS, {"missionplanner-1.3.82-tlog-csv": "missionplanner-1.3.82-ca3731f5",
                                          "missionplanner-1.3.81-tlog-csv": "missionplanner-1.3.81-2f3e02b2"})
        cases = (("mavlink_attitude_t", attitude(4000, 0.5, -0.25, 2.0, -0.75, 0.125, 0.0) + "\n", {}),
                 ("mavlink_sys_status_t", sys_status(12600, 1550, 87) + "\n", {"capture_time_us": CAPTURE}))
        for selected, text, extra in cases:
            default_rows, default_report, _ = self.rows_of(text, selected, **extra)
            self.assertEqual(default_report["exporter_pin"], "missionplanner-1.3.82-ca3731f5")
            for source_format, pin in SOURCE_FORMATS.items():
                rows, report, _ = self.rows_of(text, selected, source_format=source_format, **extra)
                self.assertEqual(report["exporter_pin"], pin)
                self.assertEqual([r["exporter_pin"] for r in rows], [pin] * len(rows))
                self.assertEqual([{k: v for k, v in r.items() if k != "exporter_pin"} for r in rows],
                                 [{k: v for k, v in r.items() if k != "exporter_pin"}
                                  for r in default_rows])
                self.assertEqual({k: v for k, v in report.items() if k != "exporter_pin"},
                                 {k: v for k, v in default_report.items() if k != "exporter_pin"})
            for unknown in ("missionplanner-1.3.83-tlog-csv", "missionplanner-1.3.80-tlog-csv", ""):
                with self.assertRaises(ValueError):
                    convert(text, selected, source_format=unknown, **extra)

    def test_the_cli_records_the_declared_release_and_refuses_another(self):
        text = attitude(7000, 0.5, 0.25, -1.0, 0.1, 0.2, 0.3) + "\n"
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "2026-09-20 11-22-33.csv"
            source.write_text(text)
            command = [os.sys.executable, str(SCRIPT), str(source), str(Path(work) / "out-81.csv"),
                       "--source-format", "missionplanner-1.3.81-tlog-csv",
                       "--selected-message", "mavlink_attitude_t", "--decimal-separator", "dot"]
            done = subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(json.loads(done.stderr.decode())["exporter_pin"],
                             "missionplanner-1.3.81-2f3e02b2")
            rows = list(csv.DictReader(io.StringIO((Path(work) / "out-81.csv").read_text())))
            self.assertEqual(rows[0]["exporter_pin"], "missionplanner-1.3.81-2f3e02b2")
            self.assertEqual(rows[0]["reported_roll_rad"], "0.5")
            refused = subprocess.run(command[:4] + ["--source-format", "missionplanner-1.3.83-tlog-csv"]
                                     + command[6:], capture_output=True)
            self.assertEqual(refused.returncode, 2)


    def test_gimbal_rows_carry_the_reported_condition_bits_without_a_physical_cause(self):
        text = "\n".join([
            gimbal(1000, 0, 0.05, -0.125, 0.0),
            gimbal(2000, 8 | 32 | 4, -0.25, 0.5, 0.75, device_id=3),
        ]) + "\n"
        rows, report, _ = self.rows_of(text, GIMBAL_MESSAGE)
        self.assertEqual((report["selected_rows"], report["withheld_values"]), (2, 0))
        self.assertEqual(report["dialect_pin"], "c_library_v2-04fffaab")
        self.assertEqual([r["record_time_us"] for r in rows], ["1000000", "2000000"])
        clean, faulted = rows
        self.assertEqual(clean["gimbal_failure_flags_raw"], "0")
        self.assertEqual(clean["gimbal_encoder_error_reported"], "0")
        self.assertEqual(clean["gimbal_reported_fault_count"], "0")
        self.assertIn("NOT_AN_INDEPENDENTLY_VERIFIED_HEALTHY_GIMBAL", clean["gimbal_failure_flags_basis"])
        self.assertEqual(faulted["gimbal_encoder_error_reported"], "1")
        self.assertEqual(faulted["gimbal_motor_error_reported"], "1")
        self.assertEqual(faulted["gimbal_at_yaw_limit_reported"], "1")
        self.assertEqual(faulted["gimbal_at_roll_limit_reported"], "0")
        self.assertEqual(faulted["gimbal_reported_fault_count"], "2", "a travel limit is not a fault")
        self.assertEqual(faulted["gimbal_failure_flags_unknown_bits"], "0")
        self.assertIn("MECHANICAL_SENSE_NEVER_A_VIDEO_OR_MEDIA_ENCODER", faulted["gimbal_encoder_error_basis"])
        self.assertFalse([name for name in faulted if "cause" in name])
        self.assertEqual(float(faulted["gimbal_angular_velocity_y_rad_s"]), 0.5)
        self.assertEqual(faulted["gimbal_device_id_raw"], "3")
        self.assertIn("DECLARED_GIMBAL_DEVICE_ID", faulted["gimbal_device_id_basis"])
        self.assertEqual(faulted["gimbal_flags_raw"], "16")
        self.assertIn("UNINTERPRETED", faulted["gimbal_flags_basis"])
        self.assertEqual((faulted["source_msgid"], faulted["source_sysid"]), ("285", "10"))

    def test_a_complete_writer_shaped_row_with_its_array_field_parses(self):
        """A real 285 row always carries `q[4]`, so the fixture must too.

        `MAVLinkInterface.DebugPacket` at `ca3731f5` writes an array field as
        `textoutput + field.Name + delimeter`, then content only inside `byte[]`, `char[]` and
        `short[]` type tests, then `textoutput + delimeter`. `q` is `float[]` with
        `[MarshalAs(UnmanagedType.ByValArray,SizeConst=4)]`, so no branch matches and the value
        token is empty. The `name,value` pairing survives, which is the whole question here.
        """
        line = gimbal(6000, 8, 0.5, -0.25, 0.125, device_id=4)
        self.assertIn(",q,,angular_velocity_x,", line)
        self.assertLess(line.index("time_boot_ms"), line.index(",q,,"))
        self.assertLess(line.index("target_component"), line.index("delta_yaw"))
        self.assertLess(line.index("delta_yaw_velocity"), line.index("gimbal_device_id"))
        rows, report, _ = self.rows_of(line + "\n", GIMBAL_MESSAGE)
        self.assertEqual((report["selected_rows"], report["unselected_rows"]), (1, 0))
        self.assertNotIn("q", rows[0])
        self.assertFalse([name for name in rows[0] if "quaternion" in name or "orientation" in name])
        self.assertNotIn("delta_yaw", rows[0])
        self.assertEqual(rows[0]["gimbal_encoder_error_reported"], "1")
        self.assertEqual(rows[0]["gimbal_device_id_raw"], "4")
        self.assertEqual(float(rows[0]["gimbal_angular_velocity_x_rad_s"]), 0.5)
        self.assertEqual(bytes.fromhex(rows[0]["source_row_hex"][4:]).decode(), line)

    def test_the_gimbal_selection_is_restricted_to_the_release_whose_struct_was_read(self):
        """`mavlink_gimbal_device_attitude_status_t` is not the same struct at the two pins.

        At 1.3.82 `ca3731f5` it declares twelve fields; at 1.3.81 `2f3e02b2` it declares nine and
        has no `gimbal_device_id`. That release cannot render the field for any sender, so its
        absence must not be reported as the sender withholding an extension.
        """
        line = gimbal(6000, 8, 0.0, 0.0, 0.0) + "\n"
        with self.assertRaises(ValueError) as refusal:
            self.rows_of(line, GIMBAL_MESSAGE, source_format="missionplanner-1.3.81-tlog-csv")
        self.assertIn("different field set", str(refusal.exception))
        rows, _, _ = self.rows_of(attitude(1000, 0.25, -0.5, 1.75, 0.01, -0.02, 0.03) + "\n",
                                  "mavlink_attitude_t",
                                  source_format="missionplanner-1.3.81-tlog-csv")
        self.assertEqual(rows[0]["exporter_pin"], "missionplanner-1.3.81-2f3e02b2")
        rows, _, _ = self.rows_of(line, GIMBAL_MESSAGE, source_format=SOURCE_FORMAT)
        self.assertEqual(rows[0]["exporter_pin"], "missionplanner-1.3.82-ca3731f5")

    def test_an_unknown_failure_bit_is_retained_rather_than_read_as_a_known_one(self):
        rows, _, _ = self.rows_of(gimbal(3000, 0x4000_0008, 0.0, 0.0, 0.0) + "\n", GIMBAL_MESSAGE)
        self.assertEqual(rows[0]["gimbal_failure_flags_raw"], str(0x4000_0008))
        self.assertEqual(rows[0]["gimbal_failure_flags_unknown_bits"], str(0x4000_0000))
        self.assertEqual(rows[0]["gimbal_encoder_error_reported"], "1")
        self.assertEqual(rows[0]["gimbal_reported_fault_count"], "1")

    def test_the_dialects_nan_marker_is_an_unknown_not_a_refusal_or_a_zero(self):
        rows, report, _ = self.rows_of(gimbal(4000, 8, "NaN", 0.25, "NaN") + "\n", GIMBAL_MESSAGE)
        self.assertEqual((rows[0]["gimbal_angular_velocity_x_rad_s"],
                          rows[0]["gimbal_angular_velocity_z_rad_s"]), ("", ""))
        self.assertEqual(float(rows[0]["gimbal_angular_velocity_y_rad_s"]), 0.25)
        self.assertEqual(rows[0]["measurement_status"], "UNKNOWN_NOT_PROVIDED_BY_THE_SOURCE")
        self.assertEqual(report["withheld_values"], 2)
        self.assertEqual(rows[0]["gimbal_encoder_error_reported"], "1")
        for spelling in ("nan", "-NaN", "NaN%"):
            with self.assertRaises(ValueError):
                self.rows_of(gimbal(4000, 0, spelling, 0.0, 0.0) + "\n", GIMBAL_MESSAGE)

    def test_an_absent_device_id_extension_is_named_and_a_zero_stays_unqualified(self):
        rows, report, _ = self.rows_of(gimbal(5000, 0, 0.0, 0.0, 0.0, device_id=None) + "\n",
                                       GIMBAL_MESSAGE)
        self.assertEqual(rows[0]["gimbal_device_id_raw"], "")
        self.assertIn("ABSENT_MAVLINK2_EXTENSION", rows[0]["gimbal_device_id_basis"])
        self.assertEqual(report["withheld_values"], 1)
        rows, _, _ = self.rows_of(gimbal(5000, 0, 0.0, 0.0, 0.0, device_id=0) + "\n", GIMBAL_MESSAGE)
        self.assertIn("UNQUALIFIED_ZERO", rows[0]["gimbal_device_id_basis"])
        self.assertEqual(rows[0]["gimbal_device_id_raw"], "0", "a zero is retained, not blanked")
        rows, _, _ = self.rows_of(gimbal(5000, 0, 0.0, 0.0, 0.0, device_id=7) + "\n", GIMBAL_MESSAGE)
        self.assertIn("UNDOCUMENTED_CODE", rows[0]["gimbal_device_id_basis"])

    def test_a_gimbal_row_that_does_not_carry_its_condition_is_refused(self):
        broken = [
            row(GIMBAL_HEADER, GIMBAL_MESSAGE,
                (("time_boot_ms", 1000), ("angular_velocity_x", 0.0), ("angular_velocity_y", 0.0),
                 ("angular_velocity_z", 0.0), ("flags", 16))),
            row(GIMBAL_HEADER, GIMBAL_MESSAGE,
                (("time_boot_ms", 1000), ("angular_velocity_x", 0.0), ("angular_velocity_y", 0.0),
                 ("angular_velocity_z", 0.0), ("failure_flags", 8))),
            gimbal(1000, 2 ** 32, 0.0, 0.0, 0.0),
            gimbal(1000, -8, 0.0, 0.0, 0.0),
            gimbal(1000, 8, 0.0, 0.0, 0.0, flags=65536),
            row(ATTITUDE_HEADER, GIMBAL_MESSAGE,
                (("time_boot_ms", 1000), ("angular_velocity_x", 0.0), ("angular_velocity_y", 0.0),
                 ("angular_velocity_z", 0.0), ("failure_flags", 8), ("flags", 16))),
        ]
        for text in broken:
            with self.assertRaises(ValueError):
                self.rows_of(text + "\n", GIMBAL_MESSAGE)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_the_gimbal_selection_reaches_common_observations(self):
        _, _, output = self.rows_of(gimbal(6000, 8 | 256, 0.5, "NaN", -0.125, device_id=1) + "\n",
                                    GIMBAL_MESSAGE)
        common = self.observations(output, GIMBAL_PROFILE)
        self.assertEqual(common["main_rows"], 1)
        observation = common["observations"][0]
        self.assertEqual((observation["clock_basis"], observation["t_ms"]), ("BootRelative", 6000))
        fields = observation["fields"]
        self.assertEqual(fields["gimbal_encoder_error_reported"], 1)
        self.assertEqual(fields["gimbal_calibration_running_reported"], 1)
        self.assertEqual(fields["gimbal_reported_fault_count"], 1, "a calibration is not a fault")
        self.assertEqual(fields["gimbal_angular_velocity_x_rad_s"], 0.5)
        self.assertIn("gimbal_angular_velocity_y_rad_s", fields)
        self.assertIsNone(fields["gimbal_angular_velocity_y_rad_s"])
        self.assertEqual(fields["measurement_status"], "UNKNOWN_NOT_PROVIDED_BY_THE_SOURCE")
        units = common["profile_units"]
        self.assertIn("mechanical_encoder_never_a_video_encoder", units["gimbal_encoder_error_reported"])
        self.assertIn("not_a_fault", units["gimbal_calibration_running_reported"])
        self.assertIn("rad_s", units["gimbal_angular_velocity_x_rad_s"])


    def test_a_ready_storage_row_carries_its_condition_identity_and_qualified_capacity(self):
        rows, report, _ = self.rows_of(storage(7000, 2) + "\n", STORAGE_MESSAGE)
        self.assertEqual((report["selected_rows"], report["withheld_values"]), (1, 0))
        self.assertEqual(report["dialect_pin"], "c_library_v2-04fffaab")
        entry = rows[0]
        self.assertEqual(entry["record_time_us"], "7000000", "uint32 milliseconds times one thousand")
        self.assertEqual((entry["storage_status_code"], entry["storage_status_reported"]), ("2", "READY"))
        self.assertIn("NONE_OF_THESE_IS_A_PHYSICAL_CAUSE", entry["storage_status_basis"])
        self.assertIn("EVIDENCE_THAT_A_RECORDING_FAILED", entry["storage_status_basis"])
        self.assertEqual((entry["storage_id_raw"], entry["storage_count_raw"]), ("1", "2"))
        self.assertEqual((entry["storage_type_code"], entry["storage_type_reported"]), ("3", "MICROSD"))
        self.assertEqual(float(entry["storage_total_capacity_mib"]), 32768.0)
        self.assertEqual(float(entry["storage_available_capacity_mib"]), 31743.5)
        self.assertEqual(entry["storage_capacity_validity"], "REPORTED_WITH_THE_STORAGE_READY")
        self.assertIn("NOT_MEGABYTES", entry["storage_capacity_unit_basis"])
        self.assertEqual(float(entry["storage_write_speed_mib_s"]), 22.25)
        self.assertEqual(bytes.fromhex(entry["storage_name_hex"][4:]).decode(), "microSD 1")
        self.assertEqual(entry["storage_usage_raw"], "")
        self.assertIn("LIMIT_OF_THE_EXPORTER", entry["storage_usage_availability"])

    def test_a_capacity_is_withheld_unless_the_source_says_the_storage_is_ready(self):
        for status, name in ((0, "EMPTY"), (1, "UNFORMATTED"), (3, "NOT_SUPPORTED")):
            rows, report, _ = self.rows_of(storage(8000, status) + "\n", STORAGE_MESSAGE)
            entry = rows[0]
            self.assertEqual(entry["storage_status_reported"], name)
            self.assertEqual((entry["storage_total_capacity_mib"], entry["storage_used_capacity_mib"],
                              entry["storage_available_capacity_mib"]), ("", "", ""))
            self.assertIn("IGNORED_BECAUSE", entry["storage_capacity_validity"])
            self.assertEqual(report["withheld_values"], 3)
            self.assertEqual(float(entry["storage_total_capacity_raw"]), 32768.0)
            self.assertEqual(float(entry["storage_available_capacity_raw"]), 31743.5)
            self.assertEqual(float(entry["storage_read_speed_mib_s"]), 41.5)
        rows, _, _ = self.rows_of(storage(8000, 2, used=-1.0) + "\n", STORAGE_MESSAGE)
        self.assertEqual(rows[0]["storage_used_capacity_mib"], "")
        self.assertEqual(float(rows[0]["storage_used_capacity_raw"]), -1.0)
        self.assertIn("UNQUALIFIED_NEGATIVE", rows[0]["storage_capacity_validity"])
        self.assertEqual(float(rows[0]["storage_total_capacity_mib"]), 32768.0)

    def test_an_unknown_code_and_an_empty_name_are_named_not_guessed(self):
        line = storage(9000, 9, kind=200, name="")
        self.assertTrue(line.split(",sig")[0].endswith("name,,"))
        rows, _, _ = self.rows_of(line + "\n", STORAGE_MESSAGE)
        entry = rows[0]
        self.assertEqual(entry["storage_status_reported"], "UNKNOWN_9")
        self.assertEqual(entry["storage_type_reported"], "UNKNOWN_200")
        self.assertEqual(entry["storage_name_hex"], "")
        self.assertIn("EMPTY_NAME_IS_MEANINGFUL", entry["storage_name_basis"])
        self.assertEqual(entry["storage_total_capacity_mib"], "")
        rows, _, _ = self.rows_of(storage(9000, 2, usage=7) + "\n", STORAGE_MESSAGE)
        self.assertEqual(rows[0]["storage_usage_raw"], "7")
        self.assertIn("RETAINED_UNDECODED", rows[0]["storage_usage_availability"])

    def test_a_storage_row_that_is_not_this_writers_shape_is_refused(self):
        broken = [
            row(STORAGE_HEADER, STORAGE_MESSAGE,
                (("time_boot_ms", 1), ("total_capacity", 1.0), ("used_capacity", 1.0),
                 ("available_capacity", 1.0), ("read_speed", 1.0), ("write_speed", 1.0),
                 ("storage_count", 1), ("status", 2), ("type", 2), ("name", "x"))),
            row(STORAGE_HEADER, STORAGE_MESSAGE,
                (("time_boot_ms", 1), ("total_capacity", 1.0), ("used_capacity", 1.0),
                 ("available_capacity", 1.0), ("read_speed", 1.0), ("write_speed", 1.0),
                 ("storage_id", 1), ("storage_count", 1), ("status", 2), ("type", 2))),
            storage(2 ** 32, 2),          # boot time outside the dialect's uint32 domain
            storage("1.5", 2),            # a fractional boot time is not this uint32 field
            storage(-1, 2),               # a negative boot time is not an unsigned field
            storage(1000, 300),           # a status outside the uint8 domain
            storage(1000, 2, kind=-1),    # a type outside the uint8 domain
            storage(1000, 2, total="NaN"),  # no NaN sentinel is defined for a capacity
            storage(1000, 2, name="SD, internal"),
            storage(1000, 2, name="SD, internal, spare"),
            row(ATTITUDE_HEADER, STORAGE_MESSAGE,
                (("time_boot_ms", 1), ("total_capacity", 1.0), ("used_capacity", 1.0),
                 ("available_capacity", 1.0), ("read_speed", 1.0), ("write_speed", 1.0),
                 ("storage_id", 1), ("storage_count", 1), ("status", 2), ("type", 2),
                 ("name", "x"))),        # struct name and hex msgid disagree
        ]
        for text in broken:
            with self.assertRaises(ValueError):
                self.rows_of(text + "\n", STORAGE_MESSAGE)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_the_storage_selection_reaches_common_observations(self):
        text = "\n".join([storage(11000, 2, storage_id=1),
                          storage(11000, 0, storage_id=2, name="Internal Memory")]) + "\n"
        _, _, output = self.rows_of(text, STORAGE_MESSAGE)
        common = self.observations(output, STORAGE_PROFILE)
        self.assertEqual(common["main_rows"], 2)
        ready, missing = (o["fields"] for o in common["observations"])
        self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
        self.assertEqual(common["observations"][0]["t_ms"], 11000)
        self.assertEqual(ready["storage_status_reported"], "READY")
        self.assertEqual(ready["storage_total_capacity_mib"], 32768.0)
        self.assertEqual(ready["storage_id_raw"], 1)
        self.assertEqual(missing["storage_status_reported"], "EMPTY")
        self.assertIsNone(missing["storage_total_capacity_mib"])
        self.assertEqual(missing["storage_available_capacity_raw"], 31743.5)
        units = common["profile_units"]
        self.assertIn("mebibytes", units["storage_total_capacity_mib"])
        self.assertIn("not_megabytes", units["storage_capacity_unit_basis"])
        self.assertIn("never_a_physical_cause", units["storage_status_basis"])

    def test_a_practical_export_size_stays_inside_the_bounds(self):
        text = "".join(attitude(index * 100, 0.1, 0.2, 0.3, 0.01, 0.02, 0.03) + "\n"
                       for index in range(5000))
        rows, report, _ = self.rows_of(text, "mavlink_attitude_t")
        self.assertEqual((len(rows), report["selected_rows"]), (5000, 5000))
        self.assertEqual(rows[-1]["record_time_us"], str(4999 * 100 * 1000))


if __name__ == "__main__":
    unittest.main()
