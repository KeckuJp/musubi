"""Independent ROS1 interval contract; authored bytes, not physical recordings.

The >16 MiB fixture has valid ROS1 metadata but deliberately undecodable,
oversized payloads on an excluded topic. No source/producer qualification is
inferred from this boundary test. Supply MUSUBI_TELEMETRY_READER for the actual
common-observation assertion; without it that test is explicitly SKIPPED.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from rosbags.rosbag1 import Writer

from scripts import convert_rosbag_observations as converter


ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
TIMES = {i: 1_000_000_000 + i * 1_000_000 + 123 for i in range(1, 5)}
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"


def imu_bytes(sequence):
    types = converter.ROS1_STORE.types
    vector = types["geometry_msgs/msg/Vector3"]
    message = types[converter.IMU](
        types["std_msgs/msg/Header"](sequence,
            types["builtin_interfaces/msg/Time"](42, 123456789), "reported_frame"),
        types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.), np.zeros(9),
        vector(float(sequence), 2., 3.), np.eye(3).flatten(),
        vector(0., 0., 9.81), np.zeros(9))
    return bytes(converter.ROS1_STORE.serialize_ros1(message, converter.IMU))


def write_bag(path, groups, noise_bytes=0):
    """Write out-of-order chunks; each group deliberately closes one chunk."""
    writer = Writer(path)
    writer.chunk_threshold = 64 * MIB
    with writer:
        imu = writer.add_connection("/imu", converter.IMU,
                                    typestore=converter.ROS1_STORE)
        noise = writer.add_connection("/noise", "std_msgs/msg/String",
                                      typestore=converter.ROS1_STORE) if noise_bytes else None
        for group in groups:
            for sequence in group:
                writer.write(imu, TIMES[sequence], imu_bytes(sequence))
            if noise is not None:
                writer.write(noise, TIMES[group[0]], b"\xff" * noise_bytes)
            writer.write_chunk(writer.chunks[-1])


class Ros1WindowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.large = cls.root / "multi-chunk.bag"
        write_bag(cls.large, [(2,), (1,), (3, 4)], 6 * MIB)
        assert 16 * MIB < cls.large.stat().st_size < 2 * 1024**3

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "actual common reader must be supplied explicitly")
    def test_adjacent_windows_exclude_payloads_and_preserve_common_meaning(self):
        seen = []
        for start, stop, expected in ((TIMES[1], TIMES[3], [1, 2]),
                                      (TIMES[3], TIMES[4] + 1, [3, 4])):
            with patch.object(converter, "decode", wraps=converter.decode) as decode:
                output, report = converter.convert(self.large, ros1=True,
                    start_ns=start, stop_ns=stop, topics=["/imu"])
            self.assertEqual(decode.call_count, 2)
            self.assertTrue(all(call.args[1] == converter.IMU for call in decode.call_args_list))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(report["clock"], "Unknown")
            self.assertEqual((report["source_messages"], report["decoded_messages"],
                              report["output_records"]), (2, 2, 2))
            self.assertEqual(report["unsupported_records"], [])
            selection = report["topic_selection"]
            self.assertEqual((selection["declared_selected_messages"],
                              selection["declared_excluded_messages"],
                              selection["read_messages"]), (4, 3, 2))
            self.assertEqual(selection["outside_selection"], "NOT_DECODED_OR_VALIDATED")
            self.assertEqual(selection["excluded_topics"],
                [{"topic": "/noise", "type": "std_msgs/msg/String", "declared_messages": 3}])
            window = report["capture_window"]
            self.assertEqual((window["start_ns"], window["stop_ns_exclusive"],
                              window["declared_bag_messages"], window["selected_messages"],
                              window["outside_window_messages"]), (start, stop, 7, 2, 2))
            bounds = report["ros1_bounds"]
            for key, limit in (("max_chunk_bytes", 16 * MIB),
                               ("max_metadata_bytes", 16 * MIB),
                               ("max_index_entries", 1_000_000),
                               ("max_selected_chunk_read_bytes", 128 * MIB)):
                self.assertEqual(bounds[key], limit)
            self.assertEqual(bounds["index_entries"], 7)
            self.assertGreater(bounds["selected_chunk_read_bytes"], 6 * MIB)
            self.assertLessEqual(bounds["selected_chunk_read_bytes"], 128 * MIB)
            for row, sequence in zip(rows, expected):
                source = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                self.assertEqual(source["topic"], "/imu")
                self.assertEqual(source["type_hash"], converter.ROS1_HASHES[converter.IMU])
                self.assertEqual(source["capture_ns"], str(TIMES[sequence]))
                self.assertEqual(source["raw_ros1_hex"], imu_bytes(sequence).hex())
                header = source["decoded"]["header"]
                self.assertEqual((header["seq"], header["frame_id"],
                    header["stamp"]["sec"], header["stamp"]["nanosec"]),
                    (sequence, "reported_frame", 42, 123456789))
                seen.append(source["decoded"]["header"]["seq"])
            saved = self.root / f"window-{start}.csv"
            saved.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(PROFILE), str(saved), "--allow-equal-time"], check=True,
                capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            for observation, sequence, row in zip(common["observations"], expected, rows):
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["wall_ms"])
                self.assertIsNone(observation["anchor_unix_us"])
                self.assertIsNone(observation["t_boot_us"])
                self.assertEqual(observation["t_ms"], TIMES[sequence] // 1_000_000)
                fields = observation["fields"]
                self.assertEqual(fields["angular_velocity_rad_s_x"], float(sequence))
                self.assertAlmostEqual(fields["linear_acceleration_m_s2_z"], 9.81)
                self.assertEqual(fields["source_record_hex"], row["source_record_hex"])
        self.assertEqual(seen, [1, 2, 3, 4])  # Exact boundary: no duplication or gap.

    def test_default_topic_only_ceiling_and_empty_exact_selection(self):
        for options in ({}, {"topics": ["/imu"]}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "bag too large"):
                converter.convert(self.large, ros1=True, **options)
        small = self.root / "small.bag"
        write_bag(small, [(1,)])
        for options in ({}, {"topics": ["/imu"]}):
            _, report = converter.convert(small, ros1=True, **options)
            self.assertEqual(report["output_records"], 1)
        with self.assertRaisesRegex(ValueError, "requested topic is absent"):
            converter.convert(self.large, ros1=True, start_ns=TIMES[1],
                              stop_ns=TIMES[4] + 1, topics=["/im"])
        with self.assertRaisesRegex(ValueError, "no supported messages"):
            converter.convert(self.large, ros1=True, start_ns=TIMES[4] + 1,
                              stop_ns=TIMES[4] + 2, topics=["/imu"])

    def test_oversized_uncompressed_chunk_refused_before_reader_open(self):
        oversized = self.root / "oversized-chunk.bag"
        write_bag(oversized, [(1,)], 16 * MIB)
        with patch.object(converter.Reader1, "open",
                          side_effect=AssertionError("native preflight must run before Reader1.open")) as opened:
            with self.assertRaisesRegex(ValueError, "ROS1.*bound"):
                converter.convert(oversized, ros1=True, start_ns=TIMES[1],
                                  stop_ns=TIMES[1] + 1, topics=["/imu"])
            opened.assert_not_called()

    def test_invalid_intervals_and_distinct_resource_bounds(self):
        small = self.root / "resource-controls.bag"
        write_bag(small, [(1,)])
        for options in ({"start_ns": TIMES[1]}, {"stop_ns": TIMES[1] + 1},
                        {"start_ns": TIMES[1] + 1, "stop_ns": TIMES[1]},
                        {"start_ns": True, "stop_ns": TIMES[1] + 1}):
            with self.subTest(interval=options), self.assertRaisesRegex(
                    ValueError, "explicit nonempty nanosecond interval required"):
                converter.convert(small, ros1=True, topics=["/imu"], **options)
        for limit in ("ROS1_METADATA_LIMIT", "ROS1_INDEX_LIMIT"):
            with self.subTest(limit=limit), patch.object(converter, limit, 0, create=True), \
                    patch.object(converter.Reader1, "open", side_effect=AssertionError(
                        "metadata/index must be bounded before Reader1.open")) as opened:
                with self.assertRaisesRegex(ValueError, "ROS1.*bound"):
                    converter.convert(small, ros1=True, start_ns=TIMES[1],
                                      stop_ns=TIMES[1] + 1, topics=["/imu"])
                opened.assert_not_called()
        with patch.object(converter, "ROS1_SELECTED_READ_LIMIT", 0, create=True), \
                patch.object(converter, "decode", side_effect=AssertionError(
                    "selected chunk reload must be bounded before decode")) as decode:
            with self.assertRaisesRegex(ValueError, "ROS1.*bound"):
                converter.convert(small, ros1=True, start_ns=TIMES[1],
                                  stop_ns=TIMES[1] + 1, topics=["/imu"])
            decode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
