"""Two authored MP exports; fixed writer meanings, not a new flight-record qualification."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_gcs_tlog_csv as g
from tests.test_gcs_tlog_csv import row, sys_status, STATUS_HEADER

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/gcs-csv-reuse/basic-observations-profile.toml"


def position(boot=100, heading=1234, lat=350000000, lon=1390000000):
    return row("FD,1C,0,0,1,1,1,21", g.POSITION_MESSAGE,
               (("time_boot_ms", boot), ("lat", lat), ("lon", lon), ("alt", 123456),
                ("relative_alt", -2345), ("vx", -123), ("vy", 234), ("vz", -345),
                ("hdg", heading), ("future", 789)))


def heartbeat(state=4, mode=129, custom=123456):
    return row("FD,9,0,0,2,1,1,0", g.HEARTBEAT_MESSAGE,
               (("custom_mode", custom), ("type", 1), ("autopilot", 3), ("base_mode", mode),
                ("system_status", state), ("mavlink_version", 3)))


def status(mask=33):
    return row(STATUS_HEADER, "mavlink_sys_status_t",
               (("onboard_control_sensors_present", mask), ("onboard_control_sensors_enabled", 32),
                ("onboard_control_sensors_health", 0), ("voltage_battery", 12000),
                ("current_battery", 123), ("battery_remaining", 50)))


class GcsBasicTests(unittest.TestCase):
    def common(self, text, position_selection=False):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "dedicated post-build stage requires common reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "converted.csv"
            path.write_text(text)
            profile = PROFILE.with_name("position-profile.toml") if position_selection else PROFILE
            result = subprocess.run([reader, str(profile), str(path), "--allow-equal-time"],
                                    check=True, text=True, capture_output=True)
            return json.loads(result.stdout)

    def test_two_exports_position_state_and_masks_to_common(self):
        for version, state, mask in ((g.SOURCE_FORMAT, 4, 33),
                                     (next(k for k in g.SOURCE_FORMATS if k != g.SOURCE_FORMAT), 250, 2**31)):
            source = position() + "\r\n" + heartbeat(state) + "\r\n" + status(mask) + "\r\n"
            for selected, options in ((g.POSITION_MESSAGE, {}),
                    (g.HEARTBEAT_MESSAGE, {"capture_time_us": 999}),
                    ("mavlink_sys_status_t", {"capture_time_us": 999, "status_masks": True})):
                output, report = g.convert(source, selected, source_format=version, **options)
                item = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual((report["source_rows"], report["selected_rows"], report["unselected_rows"]), (3, 1, 2))
                common = self.common(output, selected == g.POSITION_MESSAGE)
                self.assertEqual(common["main_rows"], 1)
                observation = common["observations"][0]
                meaning = observation["fields"]
                self.assertEqual(observation["clock_basis"], "BootRelative" if selected == g.POSITION_MESSAGE else "Unknown")
                if selected == g.POSITION_MESSAGE:
                    self.assertEqual(float(item["latitude_deg"]), 35)
                    self.assertAlmostEqual(float(item["altitude_msl_m"]), 123.456)
                    self.assertAlmostEqual(float(item["reported_relative_altitude_m"]), -2.345)
                    self.assertEqual(float(item["velocity_down_m_s"]), -3.45)
                    self.assertEqual(meaning["velocity_down_m_s"], -3.45)
                    self.assertAlmostEqual(meaning["altitude_msl_m"], 123.456)
                    self.assertEqual(observation["t_ms"], 100)
                    self.assertIn("future,789", bytes.fromhex(item["source_row_hex"][4:]).decode())
                elif selected == g.HEARTBEAT_MESSAGE:
                    self.assertEqual(item["system_status_reported"], "ACTIVE" if state == 4 else "UNKNOWN_250")
                    self.assertEqual(item["mode_safety_armed_reported"], "true")
                    self.assertEqual(item["mode_reserved_custom_mode_reported"], "true")
                    self.assertEqual(meaning["system_status_reported"], item["system_status_reported"])
                    self.assertEqual(meaning["mode_safety_armed_reported"], "true")
                else:
                    self.assertEqual(item["sensor_gps_health_reported"], "false")
                    self.assertEqual(int(item["sensor_present_unknown_bits"]), mask & 2**31)
                    self.assertEqual(meaning["sensor_gps_health_reported"], "false")
                    self.assertEqual(meaning["electrical_voltage_v"], 12)

    def test_heading_missing_and_important_rejections(self):
        output, report = g.convert(position(heading=65535, lat=0, lon=0), g.POSITION_MESSAGE)
        item = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(item["heading_deg"], "")
        self.assertEqual(item["measurement_status"], "UNKNOWN_HEADING")
        self.assertEqual(report["withheld_values"], 1)
        self.assertEqual(float(item["latitude_deg"]), 0)
        for bad in (position(heading=36000), position(lat=900000001), position(boot=-1),
                    position(boot=2) + "\n" + position(boot=1)):
            with self.assertRaises(ValueError):
                g.convert(bad, g.POSITION_MESSAGE)
        for options in ({}, {"capture_time_us": -1}):
            with self.assertRaises(ValueError):
                g.convert(heartbeat(), g.HEARTBEAT_MESSAGE, **options)
        with self.assertRaises(ValueError):
            g.convert(heartbeat(mode=256), g.HEARTBEAT_MESSAGE, capture_time_us=0)
        with self.assertRaises(ValueError):
            g.convert(sys_status(12000, 1, 50), "mavlink_sys_status_t", capture_time_us=0, status_masks=True)
        with self.assertRaises(ValueError):
            g.convert(position(), g.POSITION_MESSAGE, status_masks=True)

    def test_status_default_unchanged(self):
        text = sys_status(12000, 100, 50)
        default = g.convert(text, "mavlink_sys_status_t", capture_time_us=1)
        self.assertEqual(default, g.convert(text, "mavlink_sys_status_t", capture_time_us=1, status_masks=False))
        self.assertNotIn("sensor_mask_basis", default[0])

    def test_large_original_distinct_windows_and_truncated_selected_row(self):
        first = (position(10) + "\r\n\r\n" + heartbeat() + "\r\n" + position(20) + "\r\n").encode()
        repeat = (position(100) + "\r\n").encode()
        repetitions = g.LIMIT // len(repeat) + 1
        data = first + repeat * repetitions
        self.assertGreater(len(data), g.LIMIT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.csv"
            path.write_bytes(data)
            for start, stop, indices, expected_offsets in (
                (0, 4, [0, 3], [0, len(first) - len((position(20) + "\r\n").encode())]),
                (repetitions + 2, repetitions + 4, [repetitions + 2, repetitions + 3],
                 [len(first) + (repetitions - 2) * len(repeat), len(first) + (repetitions - 1) * len(repeat)])):
                output, report = g.convert_path(path, g.POSITION_MESSAGE, start_record=start, stop_record=stop)
                items = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([int(x["source_record_index"]) for x in items], indices)
                self.assertEqual([int(x["source_record_byte_offset"]) for x in items], expected_offsets)
                self.assertEqual(report["source_file_sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
                self.assertEqual(report["source_physical_records"], repetitions + 4)
                self.assertEqual(report["outside_window_records"], repetitions + 4 - (stop - start))
                self.assertEqual(self.common(output, True)["main_rows"], 2)
            path.write_bytes((position() + "\r\n" + position()[:-12]).encode())
            with self.assertRaises(ValueError):
                g.convert_path(path, g.POSITION_MESSAGE, start_record=1, stop_record=2)
            with self.assertRaises(ValueError):
                g.convert_path(path, g.POSITION_MESSAGE, start_record=0, stop_record=None)
            with self.assertRaises(ValueError):
                g.convert_path(path, g.POSITION_MESSAGE, start_record=0, stop_record=3)
