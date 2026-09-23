"""Authored Tramp wire bytes, not a transmitter and not a capture from one.

Every frame here is built from the pinned Betaflight receiver's own rules
(`betaflight@77d01ba3b76a22909d5f09cb0628820141f95eaa`, `src/main/io/vtx_tramp.c`). No device,
no serial port, no radio and no network is involved: real records 0.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "profiles/declared/vtx-tramp-replies/profile.toml"
SCRIPT = ROOT / "scripts/convert_vtx_tramp_replies.py"
CAPTURE = 1_700_000_000_000_000


def load():
    spec = importlib.util.spec_from_file_location("convert_vtx_tramp_replies", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_vtx_tramp_replies"] = module
    spec.loader.exec_module(module)
    return module


def frame(code, payload=b"", *, header=0x0F, trailer=0, checksum=None):
    """One 16 byte Tramp frame: header, code, payload, checksum of bytes 1..13, zero trailer."""
    body = bytearray(16)
    body[0] = header
    body[1] = code if isinstance(code, int) else ord(code)
    body[2:2 + len(payload)] = payload
    total = 0
    for index in range(1, 14):
        total = (total + body[index]) & 0xFF
    body[14] = total if checksum is None else checksum
    body[15] = trailer
    return bytes(body)


def u16(value):
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def limits(minimum=5362, maximum=5945, power_max=600, **kwargs):
    """An 'r' reply: the device's own declared limits."""
    return frame("r", u16(minimum) + u16(maximum) + u16(power_max), **kwargs)


def status(frequency=5800, configured=200, control=0, pit=0, actual=200, **kwargs):
    """A 'v' reply: frequency, configured power, control mode, pit mode, actual power."""
    return frame("v", u16(frequency) + u16(configured) + bytes((control, pit)) + u16(actual),
                 **kwargs)


def temperature(word=0x001C, **kwargs):
    """An 's' reply: the temperature field lives at offset 6."""
    return frame("s", b"\0\0\0\0" + u16(word), **kwargs)


def read_common(case, csv_text, expected_rows):
    """Run the configured common reader, or skip explicitly - never bypass silently."""
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    if not reader:
        case.skipTest("MUSUBI_TELEMETRY_READER not configured: actual common reader check skipped")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "observations.csv"
        path.write_text(csv_text)
        finished = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                  capture_output=True)
    case.assertEqual(finished.returncode, 0,
                     f"configured common reader failed: {finished.stderr[:400]!r}")
    common = json.loads(finished.stdout)
    case.assertEqual(common["main_rows"], expected_rows)
    return common


class VtxTrampReplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tramp = load()

    def rows(self, raw, **kwargs):
        text, report = self.tramp.convert(raw, CAPTURE, **kwargs)
        return list(csv.DictReader(io.StringIO(text))), report, text

    def test_the_three_accepted_replies_reach_common_output_with_their_own_meanings(self):
        rows, report, text = self.rows(limits() + status() + temperature(),
                                       device_declared="authored bench unit")
        self.assertEqual([row["vtx_reply_kind"] for row in rows],
                         ["DEVICE_LIMITS_REPLY", "STATUS_REPLY", "TEMPERATURE_REPLY"])
        self.assertEqual([row["vtx_reply_code"] for row in rows], ["r", "v", "s"])
        self.assertEqual((rows[0]["frequency_min_mhz_reported"],
                          rows[0]["frequency_max_mhz_reported"],
                          rows[0]["power_max_reported"]), ("5362", "5945", "600"))
        self.assertEqual(rows[1]["frequency_mhz_reported"], "5800")
        self.assertEqual(rows[1]["frequency_range_disposition"],
                         "REPORTED_INSIDE_THE_DECLARED_SETTING_RANGE")
        self.assertEqual((rows[1]["configured_power_reported"],
                          rows[1]["actual_power_reported"]), ("200", "200"))
        self.assertEqual(rows[1]["pit_mode_reported"], "PIT_MODE_REPORTED_CLEAR")
        self.assertEqual(rows[1]["race_lock_declared"], "0")
        self.assertIn("NEVER_MEASURED_RADIATED_POWER", rows[1]["power_basis"])
        self.assertIn("NEVER_EIRP", rows[1]["power_basis"])
        self.assertEqual(rows[0]["source_device_declared"],
                         "hex:" + "authored bench unit".encode("utf-8").hex())
        self.assertIn("NO_SERIAL_NUMBER", rows[0]["device_identity_basis"])
        self.assertIn("NO_DEVICE_HEALTH_VERDICT", rows[0]["reply_readiness_basis"])
        self.assertEqual(report["accepted_by_kind"],
                         {"DEVICE_LIMITS_REPLY": 1, "STATUS_REPLY": 1, "TEMPERATURE_REPLY": 1})
        read_common(self, text, 3)

    def test_the_temperature_carries_no_unit_and_both_readings(self):
        rows, _, text = self.rows(temperature(word=0x001C) + temperature(word=0xFFF8))
        self.assertEqual((rows[0]["temperature_word_reported"],
                          rows[0]["temperature_signed_reading"],
                          rows[0]["temperature_unsigned_reading"]), ("28", "28", "28"))
        self.assertEqual((rows[1]["temperature_word_reported"],
                          rows[1]["temperature_signed_reading"],
                          rows[1]["temperature_unsigned_reading"]), ("65528", "-8", "65528"))
        self.assertIn("UNIT_UNRESOLVED", rows[0]["temperature_basis"])
        self.assertIn("NO_DEGREE_IS_ASSERTED", rows[0]["temperature_basis"])
        for absent in ("celsius", "Celsius", "degC", "°"):
            self.assertNotIn(absent, text)

    def test_a_zero_leading_value_is_the_query_sentinel_and_never_a_reading(self):
        rows, report, _ = self.rows(limits(minimum=0) + status(frequency=0)
                                    + temperature(word=0) + status())
        self.assertEqual([row["vtx_reply_kind"] for row in rows], ["STATUS_REPLY"])
        self.assertEqual(report["query_sentinel_echoes"],
                         {"DEVICE_LIMITS_REPLY": 1, "STATUS_REPLY": 1, "TEMPERATURE_REPLY": 1})
        self.assertEqual(report["accepted_frames"], 1)

    def test_the_race_lock_bit_is_the_only_bit_read_from_the_control_byte(self):
        rows, _, _ = self.rows(status(control=0x01) + status(control=0x81))
        self.assertEqual([row["race_lock_declared"] for row in rows], ["1", "1"])
        self.assertEqual([row["control_mode_code"] for row in rows], ["1", "129"])

    def test_a_frequency_outside_the_declared_setting_range_is_retained_not_refused(self):
        rows, _, _ = self.rows(status(frequency=4900) + status(frequency=6000))
        for row in rows:
            self.assertEqual(row["frequency_range_disposition"],
                             "REPORTED_OUTSIDE_THE_DECLARED_SETTING_RANGE_RETAINED")
        self.assertEqual([row["frequency_mhz_reported"] for row in rows], ["4900", "6000"])
        self.assertIn("never_enforced", rows[0]["frequency_range_basis"].lower())

    def test_a_checksum_mismatch_is_counted_and_never_decoded(self):
        good = status()
        bad = status(frequency=5820, checksum=(good[14] + 1) & 0xFF)
        rows, report, text = self.rows(bad + good)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["frequency_mhz_reported"], "5800")
        self.assertEqual(report["checksum_mismatch_frames"], 1)
        self.assertNotIn("5820", text)

    def test_a_non_zero_trailer_is_refused_by_the_pinned_rule(self):
        rows, report, _ = self.rows(status(trailer=1) + status())
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(report["non_zero_trailer_frames"], 1)

    def test_a_code_the_receiver_does_not_accept_costs_two_bytes(self):
        rows, report, _ = self.rows(frame("x", u16(5800)) + status())
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["unaccepted_code_events"][str(ord("x"))], 1)
        self.assertEqual(report["bytes_consumed_by_kind"]["unaccepted_code_events"], 2)
        self.assertEqual(report["bytes_attributed"], report["source_bytes"])

    def test_a_bad_code_byte_that_is_itself_a_header_is_consumed_not_reconsidered(self):
        rows, report, _ = self.rows(bytes((0x0F, 0x0F)) + status())
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["unaccepted_code_events"][str(0x0F)], 1)
        self.assertEqual(rows[0]["frame_offset"], "2")
        self.assertEqual(report["bytes_attributed"], report["source_bytes"])

    def test_a_rejected_window_cannot_manufacture_a_reading_from_the_bytes_inside_it(self):
        inner = status(frequency=5111, configured=25, actual=25)
        stream = bytes((0x0F, ord("v"))) + inner + b"\x00\x00"
        with self.assertRaises(ValueError) as raised:
            self.rows(stream)
        self.assertIn("no accepted Tramp reply", str(raised.exception))
        rows, report, text = self.rows(stream + status())
        self.assertEqual([(row["frame_offset"], row["frequency_mhz_reported"]) for row in rows],
                         [(str(len(stream)), "5800")])
        self.assertNotIn("5111", text)
        self.assertEqual(report["checksum_mismatch_frames"], 1)
        self.assertEqual(report["bytes_consumed_by_kind"]["checksum_mismatch_frames"], 16)
        self.assertEqual(report["bytes_attributed"], report["source_bytes"])

    def test_a_malformed_complete_frame_is_consumed_whole_and_the_next_frame_is_read(self):
        broken = status(frequency=5333, checksum=0x00) + status(frequency=5444, trailer=9)
        rows, report, text = self.rows(broken + status())
        self.assertEqual([row["frequency_mhz_reported"] for row in rows], ["5800"])
        self.assertEqual(rows[0]["frame_offset"], "32")
        self.assertEqual(report["checksum_mismatch_frames"], 1)
        self.assertEqual(report["non_zero_trailer_frames"], 1)
        self.assertEqual(report["bytes_consumed_by_kind"]["checksum_mismatch_frames"], 16)
        self.assertEqual(report["bytes_consumed_by_kind"]["non_zero_trailer_frames"], 16)
        self.assertEqual(report["bytes_attributed"], report["source_bytes"])
        for absent in ("5333", "5444"):
            self.assertNotIn(absent, text)

    def test_a_truncated_frame_is_counted_and_no_field_is_read_from_absent_bytes(self):
        rows, report, _ = self.rows(status() + limits()[:9])
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["truncated_trailing_bytes"], 9)

    def test_leading_noise_before_a_header_byte_is_counted_not_skipped(self):
        rows, report, _ = self.rows(b"\x01\x02\x03" + status())
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["bytes_before_a_header_byte"], 3)
        self.assertEqual(report["bytes_attributed"], report["source_bytes"])

    def test_every_byte_of_a_mixed_stream_is_attributed(self):
        mixed = (b"\xaa\xbb" + limits() + status(frequency=0) + frame("q", u16(1))
                 + status(checksum=0x00) + temperature(word=0x0020) + status() + b"\x0f\x76")
        rows, report, text = self.rows(mixed)
        self.assertEqual([row["vtx_reply_kind"] for row in rows],
                         ["DEVICE_LIMITS_REPLY", "TEMPERATURE_REPLY", "STATUS_REPLY"])
        self.assertEqual(report["bytes_attributed"], len(mixed))
        self.assertEqual(report["bytes_attributed"], report["source_bytes"])
        self.assertEqual(report["truncated_trailing_bytes"], 2)
        read_common(self, text, 3)

    def test_a_stream_with_nothing_acceptable_is_refused_rather_than_reported_empty(self):
        with self.assertRaises(ValueError) as raised:
            self.rows(b"\x00" * 64)
        self.assertIn("no accepted Tramp reply", str(raised.exception))

    def test_the_caller_declarations_are_checked(self):
        for bad in (-1, "1700000000000000", 2**63):
            with self.assertRaises(ValueError) as raised:
                self.tramp.convert(status(), bad)
            self.assertIn("capture microseconds", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            self.tramp.convert(status(), CAPTURE, device_declared="")
        self.assertIn("non-empty caller statement", str(raised.exception))

    def test_the_command_line_writes_the_csv_and_the_report_beside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "authored.bin"
            source.write_bytes(limits() + status() + temperature())
            output = Path(directory) / "converted"
            self.assertEqual(self.tramp.main([str(source), str(output),
                                              "--capture-time-us", str(CAPTURE)]), 0)
            rows = list(csv.DictReader(io.StringIO((output / "observations.csv").read_text())))
            report = json.loads((output / "report.json").read_text())
        self.assertEqual(len(rows), 3)
        self.assertEqual(report["reference"], self.tramp.REFERENCE)
        self.assertIsNone(report["device_declared"])
        self.assertTrue(report["source_sha256"].startswith("sha256:"))


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
