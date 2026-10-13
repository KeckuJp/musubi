"""Independent saved-PCAP interval contract; authored bytes, no steering run.

Written against base 6436325 and the frozen API contract, before implementation
handoff. Supply the actual common reader explicitly; otherwise integration skips.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import convert_agopen_steering as converter
from tests.test_agopen_steering import (
    CASE, PORT, IMU_BNO, HEADING_UNAVAILABLE, ROLL_UNAVAILABLE,
    datagram, steering_frame)

MIB = 1024 * 1024
START = 200_000_000
BOUNDARY = 201_000_000
STOP = 202_000_000


def write_capture(path, records, *, nano=False):
    """Native framing only; intentionally permits invalid network payloads."""
    divisor = 1_000_000_000 if nano else 1_000_000
    magic = 0xa1b23c4d if nano else 0xa1b2c3d4
    with path.open("wb") as stream:
        stream.write(struct.pack("<IHHIIII", magic, 2, 4, 0, 0, 16 * MIB, 1))
        for stamp, packet in records:
            second, fraction = divmod(stamp, divisor)
            stream.write(struct.pack("<IIII", second, fraction, len(packet), len(packet)))
            stream.write(packet)


class AgOpenSteeringWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.first = steering_frame()
        cls.absent = steering_frame(angle_hundredths=3276,
            heading_tenths=HEADING_UNAVAILABLE, roll_tenths=ROLL_UNAVAILABLE)
        cls.last = steering_frame(angle_hundredths=-32768, heading_tenths=-1, roll_tenths=0)
        cls.inside = [(START, datagram(cls.first)), (START, datagram(cls.absent)),
                      (START, datagram(steering_frame(pgn=250))),
                      (START, datagram(cls.first, port=PORT + 1)),
                      (BOUNDARY, datagram(cls.last))]
        cls.large = cls.root / "large.pcap"
        write_capture(cls.large, [(100_000_000, b"\xff" * MIB)] * 17
                      + cls.inside + [(STOP, b"\x00")])
        assert 16 * MIB < cls.large.stat().st_size < 2 * 1024**3

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "actual common reader must be supplied explicitly")
    def test_large_adjacent_intervals_keep_global_indexes_and_common_meaning(self):
        seen = []
        digest = "sha256:" + hashlib.sha256(self.large.read_bytes()).hexdigest()
        for start, stop, expected_indexes, frames, counts, packets in (
            (START, BOUNDARY, [17, 18], [self.first, self.absent], (4, 3, 1, 2), self.inside[:4]),
            (BOUNDARY, STOP, [21], [self.last], (1, 1, 0, 1), self.inside[4:])):
            output, report = converter.convert_path(self.large, PORT, IMU_BNO,
                                                     start_us=start, stop_us=stop)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([int(row["source_packet_index"]) for row in rows], expected_indexes)
            expected_angles = [-12.34, 32.76] if start == START else [-327.68]
            self.assertEqual([float(row["agopen_steer_angle_deg"]) for row in rows], expected_angles)
            self.assertEqual([int(row["record_time_us"]) for row in rows], [start] * len(rows))
            self.assertEqual(float(rows[0]["agopen_heading_deg"]), 123.4 if start == START else -0.1)
            self.assertEqual(float(rows[0]["agopen_roll_deg"]), -5.6 if start == START else 0.0)
            seen.extend(expected_indexes)
            self.assertEqual(report["source_sha256"], digest)
            self.assertEqual(report["clock"], "Unknown")
            self.assertEqual(tuple(report[key] for key in (
                "source_datagrams", "selected_datagrams", "unselected_datagrams", "decoded_frames")), counts)
            window = report["capture_window"]
            self.assertEqual((window["start_us"], window["stop_us_exclusive"]), (start, stop))
            self.assertEqual((window["source_packets"], window["selected_packets"],
                              window["outside_window_packets"]), (23, len(packets), 23 - len(packets)))
            self.assertEqual(window["outside_window"], "NOT_DECODED_OR_VALIDATED")
            self.assertEqual(window["selected_capture_bytes"], 24 + sum(16 + len(packet) for _, packet in packets))
            self.assertEqual((window["max_input_bytes"], window["max_selected_capture_bytes"],
                              window["max_source_packets"]), (2 * 1024**3, 16 * MIB, 1_000_000))
            self.assertEqual(report["unsupported_frames_total"], 1 if start == START else 0)
            if start == START:
                self.assertEqual(report["unsupported_frames"][0]["packet_index"], 19)
                self.assertEqual(rows[1]["agopen_heading_deg"], "")
                self.assertEqual(rows[1]["agopen_roll_deg"], "")
                self.assertEqual(rows[1]["agopen_heading_status"], "UNAVAILABLE_NO_IMU_SENTINEL_9999")
            for row, frame in zip(rows, frames):
                self.assertEqual(row["source_frame_hex"], "hex:" + frame.hex())
                self.assertEqual(row["source_frame_sha256"], "sha256:" + hashlib.sha256(frame).hexdigest())
                self.assertEqual(row["agopen_reported_source_address"], "192.0.2.1")
                self.assertEqual(row["agopen_reported_source_port"], "6001")
                self.assertIn("NOT_A_COMMAND", row["agopen_steer_angle_basis"])
            path = self.root / f"selected-{start}.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(CASE / "profile.toml"), str(path), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], len(rows))
            for observation, row in zip(common["observations"], rows):
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["wall_ms"])
                self.assertIsNone(observation["anchor_unix_us"])
                self.assertIsNone(observation["t_boot_us"])
                self.assertEqual(observation["t_ms"], int(row["record_time_us"]) // 1000)
                fields = observation["fields"]
                self.assertEqual(fields["agopen_steer_angle_deg"], float(row["agopen_steer_angle_deg"]))
                self.assertEqual(fields["source_frame_hex"], row["source_frame_hex"])
                if row["agopen_heading_deg"]:
                    self.assertEqual(fields["agopen_heading_deg"], float(row["agopen_heading_deg"]))
                else:
                    self.assertIsNone(fields.get("agopen_heading_deg"))
                if row["agopen_roll_deg"]:
                    self.assertEqual(fields["agopen_roll_deg"], float(row["agopen_roll_deg"]))
                else:
                    self.assertIsNone(fields.get("agopen_roll_deg"))
        self.assertEqual(seen, [17, 18, 21])  # Equal-time preserved; boundary assigned once.

    def test_default_route_empty_selection_and_selected_corruption(self):
        with self.assertRaises(ValueError):
            converter.convert_path(self.large, PORT, IMU_BNO)
        small = self.root / "small.pcap"
        write_capture(small, self.inside[:2])
        self.assertEqual(converter.convert_path(small, PORT, IMU_BNO),
                         converter.convert(small.read_bytes(), PORT, IMU_BNO))
        for port, start, stop in ((PORT + 2, START, STOP), (PORT, STOP + 1, STOP + 2)):
            with self.subTest(port=port, start=start), self.assertRaisesRegex(
                    ValueError, "no selected AgOpen steering frame"):
                converter.convert_path(self.large, port, IMU_BNO, start_us=start, stop_us=stop)
        for index, packet in enumerate((datagram(steering_frame(bad_checksum=True)),
                                         datagram(self.first[:-1]), b"\x00")):
            corrupt = self.root / f"selected-corrupt-{index}.pcap"
            write_capture(corrupt, [(START, datagram(self.first)), (START + 1, packet)])
            with self.subTest(corruption=index), self.assertRaises(ValueError):
                converter.convert_path(corrupt, PORT, IMU_BNO, start_us=START, stop_us=STOP)

    def test_interval_validation_native_order_and_selected_budget(self):
        for start, stop in ((None, STOP), (START, None), (STOP, START), (START, START),
                            (True, STOP), (START, False), (-1, STOP), (START, 2**63)):
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                converter.convert_path(self.large, PORT, IMU_BNO, start_us=start, stop_us=stop)
        budget = self.root / "other-port-budget.pcap"
        other = datagram(b"x" * 60000, port=PORT + 1)
        write_capture(budget, [(START, datagram(self.first))] + [(START, other)] * 300)
        self.assertGreater(budget.stat().st_size, 16 * MIB)
        with patch.object(converter, "iter_udp_datagrams",
                          side_effect=AssertionError("selected native budget precedes UDP decode")) as decode:
            with self.assertRaises(ValueError):
                converter.convert_path(budget, PORT, IMU_BNO, start_us=START, stop_us=STOP)
            decode.assert_not_called()
        for index, records in enumerate(([(100, b"x"), (99, b"x"), (START, datagram(self.first))],
                                         [(START, datagram(self.first))])):
            invalid = self.root / f"native-{index}.pcap"
            write_capture(invalid, records)
            if index == 1:
                with invalid.open("ab") as stream:
                    stream.write(b"\x00")
            with self.subTest(native=index), self.assertRaises(ValueError):
                converter.convert_path(invalid, PORT, IMU_BNO, start_us=START, stop_us=STOP)

    def test_nanoseconds_selected_exactness_and_outside_order(self):
        nano = self.root / "nanoseconds.pcap"
        write_capture(nano, [(START * 1000 - 1, b"x"),
                             (START * 1000, datagram(self.first)),
                             (BOUNDARY * 1000 + 1, b"x")], nano=True)
        output, report = converter.convert_path(nano, PORT, IMU_BNO,
                                                start_us=START, stop_us=BOUNDARY)
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual((row["record_time_us"], row["source_packet_index"]), (str(START), "1"))
        self.assertEqual(report["capture_window"]["selected_packets"], 1)
        for index, records in enumerate(([(START * 1000 + 1, datagram(self.first))],
                [(START * 1000 - 1, b"x"), (START * 1000 - 2, b"x"),
                 (START * 1000, datagram(self.first))])):
            invalid = self.root / f"nano-invalid-{index}.pcap"
            write_capture(invalid, records, nano=True)
            with self.subTest(nanoseconds=index), self.assertRaises(ValueError):
                converter.convert_path(invalid, PORT, IMU_BNO,
                                       start_us=START, stop_us=BOUNDARY)


if __name__ == "__main__":
    unittest.main()
