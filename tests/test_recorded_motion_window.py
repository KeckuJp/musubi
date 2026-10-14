"""Authored capacity checks reuse fixed saved motion meanings; not flight evidence."""
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_recorded_angles_csv as converter

ROOT = Path(__file__).resolve().parents[1]
PLANE = "plane-4.3.1-decoded-att"
BF = "betaflight-4.2.0-si-motion"


class RecordedMotionWindowTests(unittest.TestCase):
    def check_large(self, schema, header, values, names, expected):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "source.csv"
            selected = [f"{600000 + i // 2},{values},unknown-{i}" for i in range(4)]
            with source.open("wb") as stream:
                stream.write((header + "\r\n").encode())
                for i in range(513):
                    stream.write((f"{i}," + ",".join(["NaN"] * len(names))
                                  + "," + "x" * 32768 + "\r\n").encode())
                stream.write("\r\n".join(selected).encode())
            raw = source.read_bytes()
            self.assertGreater(len(raw), 16 * 1024**2)
            digest = hashlib.sha256(raw).hexdigest()
            indexes = []
            reader = os.environ.get("MUSUBI_TELEMETRY_READER")
            self.assertTrue(reader, "actual common reader required; absence is NOT_RUN")
            for start, stop in [(513, 514), (514, 517)]:
                output, report = converter.convert_path(source, schema, start_record=start, stop_record=stop)
                self.assertEqual(report["schema"], "recorded-motion-csv-record-selection/v1")
                self.assertEqual(report["source_schema"], schema)
                self.assertEqual(report["source_sha256"], digest)
                self.assertEqual(report["source_bytes"], len(raw))
                self.assertEqual(report["source_records"], 517)
                self.assertEqual(report["selected_records"], stop - start)
                self.assertEqual(report["excluded_records"], 517 - (stop - start))
                self.assertEqual(report["excluded_values"], "NOT_DECODED_OR_VALIDATED")
                self.assertEqual(report["output_sha256"], hashlib.sha256(output.encode()).hexdigest())
                path = directory / f"part-{start}.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([
                    reader, str(ROOT / "profiles/declared/recorded-angles/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((len(rows), common["main_rows"], len(common["observations"])),
                                 (stop - start,) * 3)
                for index, (row, observation) in enumerate(zip(rows, common["observations"]), start):
                    indexes.append(int(row["source_record_index"]))
                    self.assertEqual(row["source_file_sha256"], "sha256:" + digest)
                    self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), selected[index - 513])
                    self.assertEqual(bytes.fromhex(row["source_header_hex"][4:]).decode(), header)
                    fields = observation["fields"]
                    self.assertEqual(int(fields["source_record_index"]), index)
                    self.assertEqual(fields["source_file_sha256"], "sha256:" + digest)
                    self.assertEqual(fields["source_record_hex"], row["source_record_hex"])
                    for name, value in zip(names, expected):
                        self.assertAlmostEqual(fields[name], value)
                    self.assertEqual(observation["clock_basis"], "BootRelative")
                    self.assertIsNone(observation["anchor_unix_us"])
                    self.assertEqual(observation["t_boot_us"], 600000 + (index - 513) // 2)
            self.assertEqual(indexes, [513, 514, 515, 516])

    def test_plane_large_adjacent_common(self):
        self.check_large(PLANE, "TimeUS,Roll,Pitch,Yaw,future", "180,-90,0",
                         [f"reported_{axis}_rad" for axis in ("roll", "pitch", "yaw")],
                         [math.pi, -math.pi / 2, 0])

    def test_betaflight_large_adjacent_common(self):
        names = ([f"reported_angular_velocity_{axis}_rad_s" for axis in "xyz"]
                 + [f"reported_acceleration_{axis}_m_s2" for axis in "xyz"])
        header = "time (us)," + ",".join([f"gyroADC[{i}] (rad/s)" for i in range(3)]
                   + [f"accSmooth[{i}] (m/s/s)" for i in range(3)]) + ",future"
        self.check_large(BF, header, "1.25,-2.50,0.00,9.81,-1.00,0.50",
                         names, [1.25, -2.5, 0, 9.81, -1, 0.5])

    def test_selected_values_unknown_selector_and_outside_reset_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "input.csv"
            header = "TimeUS,Roll,Pitch,Yaw,future\n"
            source.write_text(header + "100,180,-90,0,opaque\n")
            for schema in ["betaflight-raw", "inav-7.1.2-raw-attitude", "unknown"]:
                with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "schema"):
                    converter.convert_path(source, schema, start_record=0, stop_record=1)
            for text in [header + "100,,0,0,x\n", header + "100,NaN,0,0,x\n",
                         header + "100,180,0,0,x\n50,NaN,NaN,NaN,y\n",
                         header + "100,180,0,0,x\n200,0,0\n"]:
                source.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    converter.convert_path(source, PLANE, start_record=0, stop_record=1)


if __name__ == "__main__":
    unittest.main()
