"""Independent authored MAVLink wire -> native export -> declared comparison -> common.

No recorded device evidence, physical route truth, or authenticated sender is claimed.
Both binaries must be supplied explicitly; absence is a skip, never a passing chain.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NATIVE_PROFILE = ROOT / "profiles/declared/tlog-state/gcs-recorded-profile.toml"
COMMON_PROFILE = ROOT / "profiles/declared/declared-expectation-checks/profile.toml"
COLUMNS = (
    "record_time_us", "reported_xtrack_m", "declared_reference_m", "source_input_sha256",
    "source_profile_sha256", "source_selected_index", "source_observation_digest",
    "reported_system_id", "reported_component_id", "reported_message_id",
    "source_raw_frame_hex", "source_selected_records", "source_unselected_records",
    "source_value_basis", "condition_basis",
)
SUBJECTS = COLUMNS[2:]
CLOCK = "host_recorded_us_unknown_not_device_or_receipt_utc"
MEANING = ("tlog-state/gcs-recorded-profile.toml: NAV_CONTROLLER_OUTPUT.xtrack_error_m_reported "
           "is reported metres; declared_reference_m and tolerance are caller conditions, "
           "not route ground truth")


def frame(xtrack, *, v2=False, system=9, message=62):
    payload = (struct.pack("<fffffhhH", -1.5, 2.25, -3.5, 4.25, xtrack, -179, 180, 65535)
               if message == 62 else bytes([7]))
    header = (bytes([0xfd, len(payload), 0, 0, 8, system, 10, message, 0, 0]) if v2
              else bytes([0xfe, len(payload), 8, system, 10, message]))
    wire = header + payload
    crc = 0xffff
    for byte in wire[1:] + bytes([183 if message == 62 else 0]):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return wire + struct.pack("<H", crc)


def recorded(time_us, wire):
    return struct.pack(">Q", time_us) + wire


def unhex(value):
    return bytes.fromhex(value.removeprefix("hex:")).decode()


class TlogNavComparisonContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = os.environ.get("MUSUBI_TLOG_NAV_EXPORTER")
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.exporter or not cls.reader:
            raise unittest.SkipTest("MUSUBI_TLOG_NAV_EXPORTER and MUSUBI_TELEMETRY_READER "
                                    "are required for the actual binary chain")

    def export(self, directory, raw, reference="1.25", *, selector=True, profile_text=None):
        directory.mkdir(parents=True, exist_ok=True)
        profile = profile_text if profile_text is not None else NATIVE_PROFILE.read_text()
        if selector:
            profile += "\n[selected_sender]\nsystem_id = 9\ncomponent_id = 10\n"
        (directory / "source.toml").write_text(profile)
        (directory / "source.tlog").write_bytes(raw)
        result = subprocess.run([self.exporter, str(directory / "source.toml"),
                                 str(directory / "source.tlog"), "--reference-m", reference],
                                capture_output=True)
        return result, profile.encode()

    def compare(self, directory, exported, tolerance="2"):
        path = directory / "exported.csv"
        path.write_bytes(exported)
        command = [sys.executable, str(ROOT / "scripts/convert_declared_expectation_check.py"),
                   str(path), str(directory / "comparison"), "--clock-basis", CLOCK,
                   "--value-meaning", MEANING, "--difference-column", "reported_xtrack_m",
                   "--difference-column", "declared_reference_m"]
        if tolerance is not None:
            command += ["--tolerance", tolerance]
        for column in SUBJECTS:
            command += ["--subject-column", column]
        return subprocess.run(command, capture_output=True)

    def common(self, directory):
        result = subprocess.run([self.reader, str(COMMON_PROFILE),
                                 str(directory / "comparison/observations.csv"),
                                 "--allow-equal-time"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def assert_chain(self, directory, raw, frames, times, xtracks, reference, tolerance,
                     expected, differences, unselected):
        exported, profile = self.export(directory, raw, reference)
        self.assertEqual(exported.returncode, 0, exported.stderr.decode())
        parsed = csv.DictReader(io.StringIO(exported.stdout.decode()))
        rows = list(parsed)
        self.assertEqual(tuple(parsed.fieldnames), COLUMNS)
        self.assertEqual(len(rows), len(frames))
        self.assertEqual([r["record_time_us"] for r in rows], list(map(str, times)))
        self.assertEqual([r["source_selected_index"] for r in rows],
                         list(map(str, range(len(frames)))))
        for row, wire, value in zip(rows, frames, xtracks):
            self.assertEqual(row["reported_xtrack_m"], "" if value is None else str(value))
            self.assertEqual(row["declared_reference_m"], reference)
            self.assertEqual(row["source_input_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
            self.assertEqual(row["source_profile_sha256"], "sha256:" + hashlib.sha256(profile).hexdigest())
            self.assertRegex(row["source_observation_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(row["source_raw_frame_hex"], wire.hex())
            self.assertEqual((row["reported_system_id"], row["reported_component_id"]), ("9", "10"))
            self.assertEqual(row["reported_message_id"], "200" if value is None else "62")
            self.assertEqual(row["source_selected_records"], str(len(frames)))
            self.assertEqual(row["source_unselected_records"], str(unselected))
            self.assertTrue(all(row[column] for column in SUBJECTS))
        compared = self.compare(directory, exported.stdout, tolerance)
        self.assertEqual(compared.returncode, 0, compared.stderr.decode())
        common = self.common(directory)
        self.assertEqual(common["main_rows"], len(rows))
        self.assertEqual(len(common["observations"]), len(rows))
        self.assertEqual(common["platform_domain"], "Unknown")
        for observation, source, disposition, difference in zip(
                common["observations"], rows, expected, differences):
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["t_boot_us"])
            self.assertIsNone(observation["anchor_unix_us"])
            fields = observation["fields"]
            self.assertEqual(fields["record_time_us"], int(source["record_time_us"]))
            self.assertEqual(fields["check_kind"], "DECLARED_PAIR_DIFFERENCE")
            self.assertEqual(fields["expectation_disposition"], disposition)
            self.assertEqual(fields["declared_tolerance"], float(tolerance))
            self.assertEqual(fields["declared_clock_basis"], CLOCK)
            self.assertEqual(unhex(fields["value_meaning_hex"]), MEANING)
            self.assertEqual(json.loads(unhex(fields["subject_columns_hex"])), list(SUBJECTS))
            self.assertEqual(json.loads(unhex(fields["subject_hex"])),
                             [source[column] for column in SUBJECTS])
            self.assertEqual(fields["source_sha256"], "sha256:" + hashlib.sha256(exported.stdout).hexdigest())
            self.assertEqual(json.loads(unhex(fields["checked_column_hex"])),
                             ["reported_xtrack_m", "declared_reference_m"])
            self.assertIn("NOT_A_TRACKING_ERROR", fields["expectation_basis"])
            self.assertIn("NOT_A_DIAGNOSIS", fields["expectation_basis"])
            if difference is None:
                self.assertIn(fields.get("difference_value"), (None, ""))
                self.assertIn(fields.get("reported_value_hex"), (None, ""))
            else:
                self.assertEqual(fields["difference_value"], difference)
                self.assertEqual(json.loads(unhex(fields["reported_value_hex"])),
                                 [source["reported_xtrack_m"], reference])

    def test_mav1_and_mav2_signed_boundary_missing_and_provenance_reach_actual_common(self):
        for v2 in (False, True):
            with self.subTest(v2=v2), tempfile.TemporaryDirectory() as work:
                values = [-1.75, -0.75, 0.25, 1.25, 2.25, 3.25, 4.25, None]
                frames = [frame(value, v2=v2, message=200 if value is None else 62)
                          for value in values]
                times = [1_234_567, 1_234_567, 1_234_568, 1_234_569,
                         1_234_570, 1_234_571, 1_234_572, 1_234_573]
                raw = b"".join(recorded(time, wire) for time, wire in zip(times, frames))
                # Peer is excluded, never compared, while whole-recording counts remain exact.
                raw = (recorded(times[0], frames[0])
                       + recorded(times[0], frame(999, v2=v2, system=11))
                       + raw[len(recorded(times[0], frames[0])):])
                self.assert_chain(Path(work), raw, frames, times, values, "1.25", "2",
                                  ["BEYOND_DECLARED_TOLERANCE"]
                                  + ["WITHIN_DECLARED_TOLERANCE"] * 5
                                  + ["BEYOND_DECLARED_TOLERANCE", "NOT_REPORTED"],
                                  [-3, -2, -1, 0, 1, 2, 3, None], 1)

    def test_distinct_caller_reference_and_fractional_tolerance_are_not_implicit_zero(self):
        with tempfile.TemporaryDirectory() as work:
            values = [-2.5, -2.0, -1.5]
            frames = [frame(value, v2=True) for value in values]
            times = [9_876_543, 9_876_543, 9_876_544]
            raw = b"".join(recorded(time, wire) for time, wire in zip(times, frames))
            # The exporter may canonically render an integral decoded float as "-2".
            self.assert_chain(Path(work), raw, frames, times, [-2.5, -2, -1.5], "-2.00", "0.5",
                              ["WITHIN_DECLARED_TOLERANCE"] * 3, [-0.5, 0, 0.5], 0)

    def test_corruption_invalid_conditions_or_missing_selection_refuse_without_partial_export(self):
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            valid = recorded(1_234_567, frame(1.25))
            corrupt = bytearray(frame(1.25))
            corrupt[-1] ^= 1
            for name, raw, reference, selector, profile in (
                    ("crc", valid + recorded(1_234_568, corrupt), "0", True, None),
                    ("truncated", valid + valid[:-1], "0", True, None),
                    ("nonfinite", recorded(1, frame(float("nan"))), "0", True, None),
                    ("finite-too-small", recorded(1, frame(2 ** -149)), "0", True, None),
                    ("finite-too-large", recorded(1, frame(2 ** 120)), "0", True, None),
                    ("nan-reference", valid, "NaN", True, None),
                    ("exponent-reference", valid, "1e2", True, None),
                    ("large-reference", valid, "1" * 31, True, None),
                    ("comma-reference", valid, "0,1", True, None),
                    ("quote-reference", valid, '"0"', True, None),
                    ("newline-reference", valid, "0\n1", True, None),
                    ("no-selector", valid, "0", False, None),
                    ("no-selected-records", recorded(1, frame(1.25, system=11)), "0", True, None),
                    ("wrong-clock", valid, "0", True,
                     NATIVE_PROFILE.read_text().replace('time = "host_recorded_us"',
                                                        'time = "time_boot_ms"'))):
                with self.subTest(failure=name):
                    result, _ = self.export(base / name, raw, reference, selector=selector,
                                            profile_text=profile)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, b"")
                    self.assertTrue(result.stderr)
            exported, _ = self.export(base / "valid", valid)
            self.assertEqual(exported.returncode, 0, exported.stderr.decode())
            for index, tolerance in enumerate((None, "-1", "NaN")):
                directory = base / f"bad-tolerance-{index}"
                directory.mkdir()
                result = self.compare(directory, exported.stdout, tolerance)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "comparison/observations.csv").exists())
            # No sorting or equal-time collapse: a decreasing recorded clock is refused.
            directory = base / "decrease"
            decreased, _ = self.export(directory, valid + recorded(1_234_566, frame(2.25)))
            if decreased.returncode == 0:
                result = self.compare(directory, decreased.stdout)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "comparison/observations.csv").exists())
            else:
                self.assertEqual(decreased.stdout, b"")


if __name__ == "__main__":
    unittest.main()
