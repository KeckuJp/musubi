"""Authored native v9 file-zstd bags, not newly acquired physical recordings."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rosbags.rosbag2 import Writer
from rosbags.rosbag2.enums import CompressionFormat, CompressionMode
from scripts import convert_rosbag_zstd_window as window
from scripts import convert_rosbag_observations as ros
from tests.test_rosbag_observations import bag, samples

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"


class ZstdWindow(unittest.TestCase):
    def common(self, output, directory):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader not supplied")
        path = Path(directory) / "common.csv"
        path.write_text(output)
        result = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_large_native_adjacent_equal_time_and_original_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "long"
            writer = Writer(source, version=9)
            writer.set_compression(CompressionMode.FILE, CompressionFormat.ZSTD)
            with writer:
                connection = writer.add_connection("/battery", ros.BATTERY, typestore=ros.STORE)
                battery = samples()[0]
                for stamp, voltage in ((1000000, 12.), (2000000, 24.), (2000000, 36.)):
                    battery.voltage = voltage
                    writer.write(connection, stamp, ros.STORE.serialize_cdr(battery, ros.BATTERY))
                other = writer.add_connection("/unqualified", "std_msgs/msg/String", typestore=ros.STORE)
                writer.write(other, 0, b"x" * (17 * 1024**2))
                writer.write(other, 2000000, b"opaque-selected")
            metadata = (source / "metadata.yaml").read_bytes()
            member = next(source.glob("*.zstd"))
            compressed = member.read_bytes()
            expanded = ros.zstandard.ZstdDecompressor().decompress(compressed, max_output_size=20 * 1024**2)
            self.assertGreater(len(expanded), 16 * 1024**2)
            with self.assertRaisesRegex(ValueError, "window selection requires"):
                ros.convert(source, start_ns=1000000, stop_ns=2000000)
            first, a = window.convert(source, start_ns=1000000, stop_ns=2000000)
            second, b = window.convert(source, start_ns=2000000, stop_ns=3000000)
            rows = list(csv.DictReader(io.StringIO(first))) + list(csv.DictReader(io.StringIO(second)))
            self.assertEqual([row["battery_voltage_v"] for row in rows], ["12.0", "24.0", "36.0"])
            retained = [json.loads(bytes.fromhex(row["source_record_hex"][4:])) for row in rows]
            self.assertEqual([item["capture_ns"] for item in retained], ["1000000", "2000000", "2000000"])
            self.assertTrue(all(item["decoded"]["header"]["stamp"]["nanosec"] == 123456789 for item in retained))
            self.assertEqual((a["source_messages"], b["source_messages"]), (1, 3))
            self.assertEqual(b["capture_window"]["declared_bag_messages"], 5)
            self.assertEqual(len(b["unsupported_records"]), 1)
            self.assertIn("6f70617175652d73656c6563746564", json.dumps(b["unsupported_records"]))
            for output, report in ((first, a), (second, b)):
                binding = report["file_zstd_window"]
                self.assertEqual(binding["source_metadata_sha256"], hashlib.sha256(metadata).hexdigest())
                self.assertEqual(binding["members"][0]["compressed_sha256"], hashlib.sha256(compressed).hexdigest())
                self.assertEqual(binding["members"][0]["expanded_sha256"], hashlib.sha256(expanded).hexdigest())
                self.assertEqual(binding["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                self.assertEqual(report["capture_window"]["outside_window"], "NOT_DECODED_OR_VALIDATED")
                common = self.common(output, directory)
                self.assertEqual(common["main_rows"], report["output_records"])
                self.assertTrue(all(item["clock_basis"] == "Unknown" for item in common["observations"]))
                self.assertEqual([item["fields"]["battery_voltage_v"] for item in common["observations"]],
                                 [float(row["battery_voltage_v"]) for row in csv.DictReader(io.StringIO(output))])
                self.assertIsNone(common["observations"][0]["fields"]["battery_temperature_k"])
                self.assertEqual(common["observations"][0]["fields"]["source_record_hex"],
                                 next(csv.DictReader(io.StringIO(output)))["source_record_hex"])

    def test_small_legacy_csv_identity_topics_empty_and_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "small"
            bag(source, compression=CompressionMode.FILE)
            old, old_report = ros.convert(source)
            output, report = window.convert(source, start_ns=0, stop_ns=8000000000)
            self.assertEqual(output, old)
            self.assertEqual(report["unsupported_records"], old_report["unsupported_records"])
            self.assertEqual(self.common(output, directory)["main_rows"], 2)
            chosen, selected = window.convert(source, start_ns=0, stop_ns=8000000000, topics=["/example/0"])
            self.assertEqual(selected["topic_selection"]["declared_excluded_messages"], 2)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(chosen)))), 1)
            for topics, start in ((["/absent"], 0), (["/unsupported"], 0), (None, 8000000000)):
                with self.subTest(topics=topics, start=start), self.assertRaises(ValueError):
                    window.convert(source, start_ns=start, stop_ns=9000000000, topics=topics)
            target = Path(directory) / "result"
            command = [sys.executable, str(ROOT / "scripts/convert_rosbag_zstd_window.py"), str(source),
                       str(target), "--start-ns", "0", "--stop-ns", "8000000000"]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((target / "observations.csv").read_text(), old)
            saved = (target / "report.json").read_bytes()
            self.assertEqual(json.loads(saved)["file_zstd_window"]["output_sha256"], hashlib.sha256(old.encode()).hexdigest())
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((target / "report.json").read_bytes(), saved)
            self.assertEqual(subprocess.run(command[:-2], capture_output=True).returncode, 2)

    def test_metadata_path_and_interval_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "small"
            bag(source, compression=CompressionMode.FILE)
            path = source / "metadata.yaml"
            original = path.read_bytes()
            def reject(info):
                with path.open("w") as stream:
                    ros.YAML(typ="safe").dump({"rosbag2_bagfile_information": info}, stream)
                with self.assertRaises((ValueError, OSError)):
                    window.convert(source, start_ns=0, stop_ns=8000000000)
            info = ros.YAML(typ="safe").load(original)["rosbag2_bagfile_information"]
            for change in ({"version": True}, {"storage_identifier": "mcap"}, {"compression_mode": "message"},
                           {"relative_file_paths": ["../x.db3.zstd"]},
                           {"relative_file_paths": ["/x.db3.zstd"]},
                           {"relative_file_paths": info["relative_file_paths"] * 2}, {"files": []}):
                with self.subTest(change=change):
                    reject(dict(info, **change))
            path.write_bytes(original)
            for start, stop, topics in ((None, 1, None), (True, 2, None), (2, 2, None), (3, 2, None),
                                        (0, 9, []), (0, 9, ["/a", "/a"])):
                with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                    window.convert(source, start_ns=start, stop_ns=stop, topics=topics)
            member = next(source.glob("*.zstd"))
            retained = member.with_suffix(".retained")
            member.rename(retained)
            member.symlink_to(retained.name)
            with self.assertRaises(OSError):
                window.convert(source, start_ns=0, stop_ns=8000000000)

    def test_frame_and_cumulative_resource_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "small"
            bag(source, compression=CompressionMode.FILE)
            member = next(source.glob("*.zstd"))
            original = member.read_bytes()
            expanded = ros.zstandard.ZstdDecompressor().decompress(original, max_output_size=20 * 1024**2)
            checksummed = ros.zstandard.ZstdCompressor(write_checksum=True).compress(expanded)
            broken = checksummed[:-1] + bytes([checksummed[-1] ^ 1])
            for bad in (original[:-1], original + b"tail", original + original, broken):
                member.write_bytes(bad)
                with self.subTest(kind=len(bad)), self.assertRaises((ValueError, ros.zstandard.ZstdError)):
                    window.convert(source, start_ns=0, stop_ns=8000000000)
            member.write_bytes(original)
            for constant in ("MAX_METADATA_BYTES", "MAX_MEMBERS", "MAX_COMPRESSED_BYTES",
                             "MAX_EXPANDED_BYTES", "MAX_WINDOW_BYTES", "MAX_REPORT_BYTES"):
                with self.subTest(bound=constant), patch.object(window, constant, 0 if constant == "MAX_MEMBERS" else 1), self.assertRaises(ValueError):
                    window.convert(source, start_ns=0, stop_ns=8000000000)
            member.write_bytes(ros.zstandard.ZstdCompressor(write_content_size=False).compress(expanded))
            with patch.object(window, "MAX_EXPANDED_BYTES", len(expanded) - 1), self.assertRaisesRegex(ValueError, "expanded storage byte bound"):
                window.convert(source, start_ns=0, stop_ns=8000000000)


if __name__ == "__main__":
    unittest.main()
