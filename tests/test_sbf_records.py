"""Authored Septentrio SBF PVTGeodetic blocks, not a receiver or a vendor capture.

Every frame in this module is built here from the pinned layout
(`septentrio-gnss/septentrio_gnss_driver@5613af2969e3f4aac4ddc0b819e773d43a71ce9b`). No real
receiver log, no device and no network is involved: real records 0.
"""
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sbf", ROOT / "scripts/convert_sbf_records.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PROFILE = ROOT / "profiles/declared/sbf-records/profile.toml"


def body(mode=4, error=0, latitude_deg=52.0, longitude_deg=4.5, height=45.25, undulation=43.0,
         nr_sv=11, time_system=0, datum=0, alert=0, reference_id=7, mean_corr_age=100,
         sentinel=False, vn=0.5, ve=-0.25, vu=0.125, cog=90.0):
    """The base PVTGeodetic field set in the pinned order and byte order."""
    if sentinel:
        latitude = longitude = height = undulation_value = module.FLOAT_NOT_AVAILABLE
        vn = ve = vu = cog = module.FLOAT_NOT_AVAILABLE
    else:
        latitude, longitude = math.radians(latitude_deg), math.radians(longitude_deg)
        undulation_value = undulation
    return struct.pack(module.BODY, mode, error, latitude, longitude, height, undulation_value,
                       vn, ve, vu, cog, 1.5, 0.75, time_system, datum, nr_sv, 0,
                       reference_id, mean_corr_age, 0x1234, alert)


def block(payload=None, identifier=4007, revision=0, tow=123456000, wnc=2300, extra=b"",
          bad_crc=False, length_override=None, **values):
    """One framed block: sync, CRC over bytes 4..length-1, id/revision split, little-endian."""
    payload = body(**values) if payload is None else payload
    payload += extra
    length = module.HEADER_SIZE + len(payload) if length_override is None else length_override
    identity = (identifier & 8191) | (revision << 13)
    framed = struct.pack(module.HEADER, 0x24, 0x40, 0, identity, length, tow, wnc) + payload
    crc = module.crc16(framed[4:length]) ^ (0xFFFF if bad_crc else 0)
    return struct.pack(module.HEADER, 0x24, 0x40, crc, identity, length, tow, wnc) + payload


def fix_pair():
    """Authored set one: a positive-fix pair, the second at revision 1 with appended bytes."""
    first = block(latitude_deg=52.0, longitude_deg=4.5, height=45.25, nr_sv=11, mode=4)
    second = block(latitude_deg=-33.8688, longitude_deg=151.2093, height=-12.5, nr_sv=7,
                   mode=6, error=0, tow=123457000, wnc=2301, revision=1,
                   extra=struct.pack("<BH", 2, 30))
    return first, second


def sentinel_block():
    """Authored set two: do-not-use position with non-zero error and sentinel counters."""
    return block(sentinel=True, mode=0, error=1, nr_sv=255, tow=module.U32_NOT_AVAILABLE,
                 wnc=module.U16_NOT_AVAILABLE, reference_id=module.U16_NOT_AVAILABLE)


def batch(count=2000):
    """A practical multi-block file, used by both the converter and the reader checks."""
    return b"".join(block(latitude_deg=(index % 90) * 0.5, longitude_deg=1.25,
                          tow=123456000 + index, nr_sv=(index % 20))
                    for index in range(count))


def rows_of(raw, capture=1000):
    text, report = module.convert(raw, capture)
    return list(csv.DictReader(io.StringIO(text))), report, text


def read_common(case, csv_text, expected_rows):
    """Run the configured common reader, or skip explicitly - never bypass silently.

    Without MUSUBI_TELEMETRY_READER the actual-reader check is reported as a skip, so a run
    without the built reader cannot look like a passing common-output proof. With the variable
    set, a missing or failing reader is a failure rather than a silent bypass: CI configures it,
    so the common checks must execute there.
    """
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


class SbfRecordsTests(unittest.TestCase):
    def test_authored_fix_blocks_reach_common_output_with_declared_units(self):
        first, second = fix_pair()
        rows, report, text = rows_of(first + second)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(float(rows[0]["sbf_latitude_deg"]), 52.0, places=9)
        self.assertAlmostEqual(float(rows[0]["sbf_longitude_deg"]), 4.5, places=9)
        self.assertAlmostEqual(float(rows[1]["sbf_latitude_deg"]), -33.8688, places=9)
        self.assertEqual(rows[0]["sbf_position_disposition"], "REPORTED_NOT_VERIFIED")
        self.assertIn("NOT_A_VERIFIED_DATUM", rows[0]["sbf_position_basis"])
        self.assertEqual(float(rows[0]["sbf_height_ellipsoidal_m"]), 45.25)
        self.assertEqual(float(rows[0]["sbf_undulation_m"]), 43.0)
        self.assertEqual(rows[0]["sbf_height_disposition"], "REPORTED_ELLIPSOIDAL_NOT_MEAN_SEA_LEVEL")
        self.assertEqual(float(rows[1]["sbf_height_ellipsoidal_m"]), -12.5)
        self.assertEqual((float(rows[0]["sbf_velocity_north_m_s"]),
                          float(rows[0]["sbf_velocity_east_m_s"]),
                          float(rows[0]["sbf_velocity_up_m_s"])), (0.5, -0.25, 0.125))
        self.assertEqual(float(rows[0]["sbf_course_over_ground_deg"]), 90.0)
        self.assertEqual((rows[0]["sbf_velocity_disposition"], rows[0]["sbf_course_disposition"]),
                         ("REPORTED_NOT_VERIFIED", "REPORTED_NOT_VERIFIED"))
        self.assertIn("NOT_A_PNT_SOLUTION", rows[0]["sbf_velocity_basis"])
        self.assertNotIn("sbf_speed_m_s", rows[0])
        self.assertEqual((rows[0]["sbf_mode_code"], rows[0]["sbf_error_code"]), ("4", "0"))
        self.assertEqual((rows[0]["sbf_nr_sv_reported"], rows[1]["sbf_nr_sv_reported"]), ("11", "7"))
        self.assertEqual(rows[1]["sbf_mode_code"], "6")
        self.assertEqual(rows[0]["sbf_code_basis"],
                         "REPORTED_CODE_VALUE_TABLE_NOT_ESTABLISHED_BY_THE_PINNED_SOURCE")
        self.assertEqual((rows[0]["sbf_tow_reported"], rows[0]["sbf_wnc_reported"]),
                         ("123456000", "2300"))
        self.assertEqual((rows[1]["sbf_tow_reported"], rows[1]["sbf_wnc_reported"]),
                         ("123457000", "2301"))
        self.assertEqual({row["record_time_us"] for row in rows}, {"1000"})
        self.assertIn("NO_UTC_AND_NO_LEAP_SECOND_APPLIED", rows[0]["sbf_time_basis"])
        self.assertEqual([row["sbf_block_offset"] for row in rows], ["0", str(len(first))])
        self.assertEqual([row["sbf_block_id"] for row in rows], ["4007", "4007"])
        self.assertEqual([row["sbf_block_revision"] for row in rows], ["0", "1"])
        self.assertEqual([row["sbf_block_extra_bytes"] for row in rows], ["0", "3"])
        self.assertEqual(rows[0]["sbf_block_sha256"],
                         "sha256:" + hashlib.sha256(first).hexdigest())
        self.assertEqual(report["revision_appended_fields_present"], 1)
        self.assertEqual((report["blocks_scanned"], report["decoded_blocks"]), (2, 2))
        self.assertEqual((report["unselected_blocks"], report["invalid_blocks"],
                          report["unsupported_revision_blocks"]), (0, 0, 0))
        self.assertEqual((report["bytes_in_blocks"], report["bytes_outside_blocks"]),
                         (len(first + second), 0))
        self.assertEqual(report["source_sha256"],
                         "sha256:" + hashlib.sha256(first + second).hexdigest())
        self.assertEqual(report["reference"], module.REFERENCE)
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "authored.sbf", Path(tmp) / "out"
            source.write_bytes(first + second)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_sbf_records.py"),
                            str(source), str(out), "--capture-time-us", "1000"],
                           check=True, capture_output=True)
            self.assertEqual((out / "observations.csv").read_text(), text)
            self.assertEqual(json.loads((out / "report.json").read_text())["decoded_blocks"], 2)

    def test_authored_sentinel_set_and_failure_accounting(self):
        sentinel = sentinel_block()
        rows, report, _ = rows_of(sentinel)
        row = rows[0]
        self.assertEqual((row["sbf_latitude_deg"], row["sbf_longitude_deg"]), ("", ""))
        self.assertEqual(row["sbf_position_disposition"], "NOT_AVAILABLE_SENTINEL")
        self.assertEqual((row["sbf_height_ellipsoidal_m"], row["sbf_height_disposition"]),
                         ("", "NOT_AVAILABLE_SENTINEL"))
        self.assertEqual(row["sbf_undulation_m"], "")
        self.assertEqual((row["sbf_velocity_north_m_s"], row["sbf_velocity_east_m_s"],
                          row["sbf_velocity_up_m_s"], row["sbf_course_over_ground_deg"]),
                         ("", "", "", ""))
        self.assertEqual((row["sbf_velocity_disposition"], row["sbf_course_disposition"]),
                         ("NOT_AVAILABLE_SENTINEL", "NOT_AVAILABLE_SENTINEL"))
        self.assertEqual((row["sbf_tow_reported"], row["sbf_wnc_reported"]), ("", ""))
        self.assertEqual(row["sbf_nr_sv_reported"], "")
        self.assertEqual((row["sbf_mode_code"], row["sbf_error_code"]), ("0", "1"))
        self.assertEqual(report["decoded_blocks"], 1)

        good = block(latitude_deg=1.5, longitude_deg=2.5)
        other = block(identifier=4006)
        corrupt = block(bad_crc=True)
        future = block(revision=3, extra=struct.pack("<BHHHHB", 1, 2, 3, 4, 5, 6))
        noise = b"\x00\x11\x22\x33\x44\x55\x66\x77"
        rows, report, _ = rows_of(good + other + corrupt + future + noise)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["sbf_latitude_deg"]), 1.5, places=9)
        self.assertEqual((report["blocks_scanned"], report["decoded_blocks"],
                          report["unselected_blocks"], report["invalid_blocks"],
                          report["unsupported_revision_blocks"]), (4, 1, 1, 1, 1))
        self.assertEqual(report["unselected"][0]["id"], 4006)
        self.assertEqual(report["unselected"][0]["offset"], len(good))
        self.assertEqual(report["invalid"][0]["reason"], "CRC_MISMATCH")
        self.assertEqual(report["invalid"][0]["sha256"],
                         "sha256:" + hashlib.sha256(corrupt).hexdigest())
        self.assertEqual(report["unsupported_revision"][0]["revision"], 3)
        self.assertEqual(report["unsupported_revision"][0]["offset"],
                         len(good + other + corrupt))
        self.assertEqual(report["bytes_in_blocks"], len(good + other + corrupt + future))
        self.assertEqual(report["bytes_outside_blocks"], len(noise))
        self.assertEqual(report["blocks_scanned"], report["decoded_blocks"]
                         + report["unselected_blocks"] + report["invalid_blocks"]
                         + report["unsupported_revision_blocks"])

    def test_framing_and_argument_failures_refuse_by_name(self):
        good = block()
        for label, raw in (
                ("no sync at all", b"\x00" * 64),
                ("sync but truncated header", b"$@" + b"\x00" * 4),
                ("length below the header", block(length_override=8)),
                ("body shorter than this revision's fields",
                 block(payload=body()[:-4], revision=0)),
                ("truncated block", good[:len(good) - 6]),
                ("only a bad CRC block", block(bad_crc=True)),
                ("only another block id", block(identifier=4006)),
                ("only an unsupported revision", block(revision=4))):
            with self.subTest(case=label), self.assertRaises(ValueError):
                module.convert(raw, 1000)
        for capture in (-1, True, 1.5, 2**63):
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                module.convert(good, capture)
        with self.assertRaises(ValueError):
            module.convert(b"", 1000)

    def test_practical_multi_block_capacity_reaches_common_output(self):
        many = batch()
        rows, report, text = rows_of(many)
        self.assertEqual(len(rows), 2000)
        self.assertEqual((report["blocks_scanned"], report["decoded_blocks"]), (2000, 2000))
        self.assertEqual(report["bytes_outside_blocks"], 0)
        self.assertEqual(rows[-1]["sbf_tow_reported"], str(123456000 + 1999))
        self.assertEqual([int(row["sbf_block_offset"]) for row in rows[:2]], [0, len(block())])
        self.assertLess(len(many), module.MAX_INPUT)


    def test_fix_pair_reaches_the_actual_common_reader(self):
        first, second = fix_pair()
        text = rows_of(first + second)[2]
        common = read_common(self, text, 2)
        fields = common["observations"][0]["fields"]
        self.assertAlmostEqual(fields["sbf_latitude_deg"], 52.0, places=9)
        self.assertEqual(fields["sbf_height_ellipsoidal_m"], 45.25)
        self.assertEqual(fields["sbf_nr_sv_reported"], 11)
        self.assertEqual(fields["sbf_undulation_m"], 43.0)
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        declared = common["profile_units"]
        for column in module.COLUMNS:
            self.assertTrue(declared.get(column), f"{column} must declare its meaning")
        self.assertIn("never_mean_sea_level", declared["sbf_height_ellipsoidal_m"])
        self.assertIn("not_added_to_or_subtracted_from_the_height", declared["sbf_undulation_m"])
        self.assertIn("converted_from_the_sources_stated_radians", declared["sbf_latitude_deg"])
        self.assertIn("not_established_by_the_pinned_source", declared["sbf_mode_code"])
        self.assertIn("no_week_rollover_arithmetic", declared["sbf_wnc_reported"])
        self.assertEqual((fields["sbf_velocity_north_m_s"], fields["sbf_velocity_east_m_s"],
                          fields["sbf_velocity_up_m_s"]), (0.5, -0.25, 0.125))
        self.assertEqual(fields["sbf_course_over_ground_deg"], 90.0)
        self.assertIn("m_s_exactly_as_the_pinned_message_declares",
                      declared["sbf_velocity_north_m_s"])
        self.assertIn("not_declared_true_or_magnetic", declared["sbf_course_over_ground_deg"])
        self.assertIn("is_not_a_position_velocity_time_solution", declared["sbf_velocity_basis"])

    def test_a_reported_course_and_the_components_are_withheld_independently(self):
        """The pinned source ties them by no rule, so neither substitutes for the other."""
        course_only = block(vn=module.FLOAT_NOT_AVAILABLE, ve=module.FLOAT_NOT_AVAILABLE,
                            vu=module.FLOAT_NOT_AVAILABLE, cog=271.5)
        components_only = block(vn=1.25, ve=2.5, vu=-0.75, cog=module.FLOAT_NOT_AVAILABLE)
        rows = rows_of(course_only + components_only)[0]
        self.assertEqual((rows[0]["sbf_velocity_north_m_s"], rows[0]["sbf_velocity_east_m_s"],
                          rows[0]["sbf_velocity_up_m_s"]), ("", "", ""))
        self.assertEqual(rows[0]["sbf_velocity_disposition"], "NOT_AVAILABLE_SENTINEL")
        self.assertEqual(float(rows[0]["sbf_course_over_ground_deg"]), 271.5)
        self.assertEqual(rows[0]["sbf_course_disposition"], "REPORTED_NOT_VERIFIED")
        self.assertEqual((float(rows[1]["sbf_velocity_north_m_s"]),
                          float(rows[1]["sbf_velocity_east_m_s"]),
                          float(rows[1]["sbf_velocity_up_m_s"])), (1.25, 2.5, -0.75))
        self.assertEqual(rows[1]["sbf_velocity_disposition"], "REPORTED_NOT_VERIFIED")
        self.assertEqual(rows[1]["sbf_course_over_ground_deg"], "")
        self.assertEqual(rows[1]["sbf_course_disposition"], "NOT_AVAILABLE_SENTINEL")
        partial = rows_of(block(vu=module.FLOAT_NOT_AVAILABLE))[0][0]
        self.assertEqual(partial["sbf_velocity_disposition"], "NOT_AVAILABLE_SENTINEL")
        self.assertEqual(partial["sbf_velocity_up_m_s"], "")
        self.assertEqual(float(partial["sbf_velocity_north_m_s"]), 0.5)
        self.assertEqual(float(partial["sbf_velocity_east_m_s"]), -0.25)

    def test_sentinel_row_reaches_the_actual_common_reader(self):
        common = read_common(self, rows_of(sentinel_block())[2], 1)
        fields = common["observations"][0]["fields"]
        self.assertIsNone(fields["sbf_latitude_deg"])
        self.assertIsNone(fields["sbf_height_ellipsoidal_m"])
        self.assertEqual(fields["sbf_mode_code"], 0)
        self.assertEqual(fields["sbf_error_code"], 1)

    def test_practical_2000_block_batch_reaches_the_actual_common_reader(self):
        common = read_common(self, rows_of(batch())[2], 2000)
        self.assertEqual(len(common["observations"]), 2000)
        self.assertEqual(common["observations"][-1]["fields"]["sbf_tow_reported"], 123456000 + 1999)
        self.assertEqual(common["observations"][-1]["fields"]["sbf_block_id"], 4007)

    def test_a_configured_but_unusable_reader_fails_instead_of_passing_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-a-reader"
            previous = os.environ.get("MUSUBI_TELEMETRY_READER")
            os.environ["MUSUBI_TELEMETRY_READER"] = str(missing)
            try:
                with self.assertRaises((AssertionError, OSError)):
                    read_common(self, rows_of(fix_pair()[0])[2], 1)
            finally:
                if previous is None:
                    del os.environ["MUSUBI_TELEMETRY_READER"]
                else:
                    os.environ["MUSUBI_TELEMETRY_READER"] = previous


    def test_candidate_bound_refuses_instead_of_dropping_the_remainder(self):
        previous = module.MAX_BLOCKS
        self.addCleanup(setattr, module, "MAX_BLOCKS", previous)
        module.MAX_BLOCKS = 3
        at_limit = b"".join(block(tow=index) for index in range(3))
        rows, report, _ = rows_of(at_limit)
        self.assertEqual((len(rows), report["decoded_blocks"]), (3, 3))
        self.assertEqual(report["bytes_outside_blocks"], 0)
        with self.assertRaises(ValueError) as caught:
            module.convert(b"".join(block(tow=index) for index in range(4)), 1000)
        self.assertEqual(str(caught.exception), "saved SBF input exceeds the candidate block bound")
        module.MAX_BLOCKS = previous
        self.assertEqual(module.MAX_BLOCKS, 65536)

    def test_overlapping_candidate_keeps_byte_accounting_non_negative(self):
        inner = block(tow=77)
        outer = block(payload=b"\x00\x00" + inner, bad_crc=True)
        rows, report, _ = rows_of(outer)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sbf_tow_reported"], "77")
        self.assertEqual((report["blocks_scanned"], report["decoded_blocks"],
                          report["invalid_blocks"]), (2, 1, 1))
        self.assertEqual(report["overlapping_candidates"], 1)
        self.assertGreaterEqual(report["bytes_outside_blocks"], 0)
        self.assertEqual(report["bytes_in_blocks"] + report["bytes_outside_blocks"], len(outer))
        self.assertLessEqual(report["bytes_in_blocks"], len(outer))
        self.assertEqual(report["invalid"][0]["reason"], "CRC_MISMATCH")
        self.assertEqual(report["invalid"][0]["offset"], 0)
        self.assertEqual(report["invalid"][0]["sha256"],
                         "sha256:" + hashlib.sha256(outer).hexdigest())

    def test_truncated_candidate_digest_binds_the_bytes_that_exist(self):
        good = block(tow=5)
        tail = block(tow=6)[:20]
        rows, report, _ = rows_of(good + tail)
        self.assertEqual(len(rows), 1)
        entry = report["invalid"][0]
        self.assertEqual(entry["reason"], "TRUNCATED_BLOCK")
        self.assertEqual(entry["retained_bytes"], len(tail))
        self.assertEqual(entry["sha256"], "sha256:" + hashlib.sha256(tail).hexdigest())
        self.assertNotEqual(entry["sha256"], "sha256:" + hashlib.sha256(b"").hexdigest())
        self.assertEqual(report["bytes_in_blocks"] + report["bytes_outside_blocks"],
                         len(good + tail))
        short_header = good + b"$@\x01\x02"
        report = rows_of(short_header)[1]
        header_entry = report["invalid"][0]
        self.assertEqual(header_entry["reason"], "TRUNCATED_HEADER")
        self.assertEqual(header_entry["retained_bytes"], 4)
        self.assertEqual(header_entry["sha256"],
                         "sha256:" + hashlib.sha256(b"$@\x01\x02").hexdigest())

    def test_cli_refuses_an_oversized_file_before_reading_it_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "big.sbf", Path(tmp) / "out"
            with source.open("wb") as handle:
                handle.truncate(module.MAX_INPUT + 1)
            finished = subprocess.run([sys.executable, str(ROOT / "scripts/convert_sbf_records.py"),
                                       str(source), str(out), "--capture-time-us", "1000"],
                                      capture_output=True)
            self.assertNotEqual(finished.returncode, 0)
            self.assertIn(b"oversized saved SBF input", finished.stderr)
            self.assertFalse(out.exists())

    def test_candidate_byte_budget_bounds_overlapping_resync_work(self):
        inner = b"".join(block(tow=index) for index in range(20))
        overlapping = block(payload=b"\x00\x00" + inner, bad_crc=True)
        previous = module.MAX_CANDIDATE_BYTES
        self.addCleanup(setattr, module, "MAX_CANDIDATE_BYTES", previous)
        self.assertEqual(previous, 4 * module.MAX_INPUT)
        module.MAX_CANDIDATE_BYTES = 600
        with self.assertRaises(ValueError) as caught:
            module.convert(overlapping, 1000)
        self.assertEqual(str(caught.exception),
                         "saved SBF input exceeds the candidate byte budget")
        module.MAX_CANDIDATE_BYTES = previous
        rows, report, _ = rows_of(overlapping)
        self.assertEqual((len(rows), report["decoded_blocks"]), (20, 20))
        self.assertEqual(report["overlapping_candidates"], 20)
        self.assertGreaterEqual(report["bytes_outside_blocks"], 0)
        self.assertEqual(report["bytes_in_blocks"] + report["bytes_outside_blocks"],
                         len(overlapping))
        self.assertGreater(report["candidate_bytes_examined"], len(overlapping))
        self.assertEqual(report["candidate_byte_budget"], 4 * module.MAX_INPUT)
        for raw in (block(), batch()):
            examined = rows_of(raw)[1]["candidate_bytes_examined"]
            self.assertEqual(examined, len(raw))
        self.assertEqual(module.MAX_BLOCKS, 65536)


if __name__ == "__main__":
    unittest.main()
