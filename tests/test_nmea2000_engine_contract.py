"""Independent fixed-canboat saved PGN127489 contract, not physical engine evidence."""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_dronecan_capture as converter

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/dronecan-recorded/profile.toml"
HEADER = "time_us,extended_id,data_hex\n"
SAMPLE = bytes.fromhex("002f06ffffe3736505ff7f72100000ffffffffff060000007f7f")


def payload(instance=1, **changes):
    values = dict(oil=1200, oiltemp=3100, temperature=30000, voltage=-1, fuel=-1,
                  runtime=7201, coolant=23, fuelpressure=4, reserved=255,
                  status1=0, status2=0x8101, load=-1, torque=-1)
    values.update(changes)
    return struct.pack("<BHHHhhIHHBHHbb", instance, *values.values())


def frames(data, source=16, sequence=0, start=100, padding=0xA5):
    identifier = 2 << 26 | 127489 << 8 | source
    chunks = [bytes([sequence << 5, 26])+data[:6],
              bytes([sequence << 5 | 1])+data[6:13],
              bytes([sequence << 5 | 2])+data[13:20],
              bytes([sequence << 5 | 3])+data[20:]+bytes([padding])]
    return [[str(start+i), f"{identifier:08x}", chunk.hex()] for i, chunk in enumerate(chunks)]


def capture(rows):
    return HEADER + "".join(",".join(row)+"\n" for row in rows)


def decode_hex_json(value):
    assert value.startswith("hex:")
    return json.loads(bytes.fromhex(value[4:]))


class NmeaEngineDynamicContract(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader and Path(reader).is_file(), "actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"observations.csv"
            path.write_text(output)
            return json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                              capture_output=True, check=True).stdout)

    def test_source_sample_and_interleaved_other_instance_reach_common_in_completion_order(self):
        self.assertEqual(len(SAMPLE), 26)
        other = payload()
        a, b = frames(SAMPLE, 16, 3), frames(other, 41, 3)
        # Equal times permit distinct keys; B completes first even though A began first.
        rows = [a[0], b[0], a[1], b[1], a[2], b[2], b[3], a[3], ["104", "09f11210", "0000000000000000"]]
        output, report = converter.convert_nmea2000_engine_dynamic(capture(rows))
        self.assertEqual((report["frames"], report["decoded_frames"], report["completed_packets"],
                          report["observations"]), (9, 8, 2, 2))
        self.assertEqual(report["unsupported_frames"][0]["source"], rows[-1])
        self.assertEqual(report["clock"], "Unknown")
        parsed = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([int(r["source_node_id"]) for r in parsed], [41, 16])
        self.assertEqual([int(r["packet_index"]) for r in parsed], [0, 1])
        for row, packet, wire in zip(parsed, (other, SAMPLE), (b, a)):
            self.assertEqual(row["source_packet_hex"], "hex:"+packet.hex())
            self.assertEqual(decode_hex_json(row["source_frames_hex"]), wire)
            self.assertEqual((int(row["first_frame_time_us"]), int(row["record_time_us"])), (100, 103))
            self.assertEqual((int(row["fast_packet_sequence"]), int(row["packet_frame_count"])), (3, 4))
        common = self.common(output)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (2, "Unknown"))
        normal, sample = [row["fields"] for row in common["observations"]]
        for observation in common["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["t_boot_us"])
            self.assertNotIn("engine_speed_rpm", observation["fields"])
        self.assertEqual(sample["instance_code"], 0)
        self.assertEqual(sample["engine_instance_reported"], "Single Engine or Dual Engine Port")
        for name, expected in (("engine_oil_pressure_pa", 158300), ("engine_temperature_k", 296.67),
                               ("engine_alternator_potential_v", 13.81), ("engine_total_runtime_s", 4210)):
            self.assertAlmostEqual(sample[name], expected)
        self.assertIsNone(sample.get("engine_oil_temperature_k"))
        self.assertEqual(sample["engine_oil_temperature_k_status"], "NOT_AVAILABLE")
        self.assertEqual(decode_hex_json(sample["engine_discrete_status_1_reported_hex"]),
                         ["Over Temperature", "Low Oil Pressure"])
        self.assertEqual(decode_hex_json(sample["engine_discrete_status_2_reported_hex"]), [])
        self.assertEqual(normal["engine_instance_reported"], "Dual Engine Starboard")
        self.assertEqual(normal["engine_total_runtime_s"], 7201)
        self.assertIn("second", common["profile_units"]["engine_total_runtime_s"].lower())

    def test_signed_all_ones_unknown_bits_and_three_scalar_sentinels_remain_distinct(self):
        packets = [payload(instance=7, reserved=0, status1=65535)]
        packets += [payload(oil=65535-i, voltage=32767-i, load=127-i) for i in range(3)]
        rows = []
        for index, packet in enumerate(packets):
            rows.extend(frames(packet, sequence=0, start=100+index*4))  # sequence may be reused after completion
        output, report = converter.convert_nmea2000_engine_dynamic(capture(rows))
        self.assertEqual(report["completed_packets"], 4)
        common = self.common(output)
        normal = common["observations"][0]["fields"]
        for name, expected in (("engine_oil_pressure_pa", 120000), ("engine_oil_temperature_k", 310),
                               ("engine_temperature_k", 300), ("engine_alternator_potential_v", -.01),
                               ("engine_fuel_rate_l_h", -.1), ("engine_total_runtime_s", 7201),
                               ("engine_coolant_pressure_pa", 2300), ("engine_fuel_pressure_pa", 4000),
                               ("engine_load_percent", -1), ("engine_torque_percent", -1)):
            self.assertAlmostEqual(normal[name], expected)
            self.assertEqual(normal[name+"_status"], "REPORTED")
        self.assertIn("UNKNOWN", normal["engine_instance_reported"])
        self.assertEqual(normal["instance_code"], 7)
        self.assertEqual(normal["engine_discrete_status_1_code"], 65535)
        self.assertEqual(len(decode_hex_json(normal["engine_discrete_status_1_reported_hex"])), 16)
        self.assertEqual(normal["engine_discrete_status_2_code"], 0x8101)
        self.assertEqual(normal["engine_discrete_status_2_unknown_bits"], 0x8100)
        self.assertEqual(len(decode_hex_json(normal["engine_discrete_status_2_reported_hex"])), 1)
        self.assertEqual(normal["reserved_field_disposition"], "NONCONFORMANT_RESERVED_BITS_RETAINED")
        for observation, status in zip(common["observations"][1:],
                                       ("NOT_AVAILABLE", "OUT_OF_RANGE_REPORTED", "RESERVED_RANGE_REPORTED")):
            fields = observation["fields"]
            for name in ("engine_oil_pressure_pa", "engine_alternator_potential_v", "engine_load_percent"):
                self.assertIsNone(fields.get(name))
                self.assertEqual(fields[name+"_status"], status)

    def test_malformed_sequences_source_priority_lengths_clock_and_pending_bound_refuse(self):
        good = frames(SAMPLE)
        for row in good:
            row[0] = "100"  # sequence negatives must not be rejected merely by the clock check
        wrong_size = [r.copy() for r in good]
        wrong_size[0][2] = "0019" + wrong_size[0][2][4:]
        wrong_length = [r.copy() for r in good]
        wrong_length[2][2] = wrong_length[2][2][:-2]
        crossed_source = [r.copy() for r in good]
        crossed_source[2][1] = f"{int(crossed_source[2][1], 16)+1:08x}"
        priority = [r.copy() for r in good]
        priority[2][1] = f"{int(priority[2][1], 16) ^ (1 << 26):08x}"
        crossed_sequence = [r.copy() for r in good]
        crossed_sequence[2][2] = "22" + crossed_sequence[2][2][2:]
        decreasing = [r.copy() for r in good]
        decreasing[3][0] = "99"
        cases = [good[:-1], good[1:], [good[0], good[2], good[1], good[3]],
                 [good[0], good[1], good[1], good[2], good[3]], [good[0], good[0]],
                 wrong_size, wrong_length, crossed_source, priority, crossed_sequence, decreasing,
                 [["100", "09f11210", "0000000000000000"]]]
        for index, rows in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ValueError):
                converter.convert_nmea2000_engine_dynamic(capture(rows))
        # All packets would complete if the64-key bound were absent; this is not an EOF-only negative.
        packets = [frames(SAMPLE, source=i) for i in range(65)]
        pending = [packet[0] for packet in packets] + [row for packet in packets for row in packet[1:]]
        for row in pending:
            row[0] = "100"
        with self.assertRaises(ValueError):
            converter.convert_nmea2000_engine_dynamic(capture(pending))
        unselected = "100,09f11210,0000000000000000\n"
        with self.assertRaises(ValueError):
            converter.convert_nmea2000_engine_dynamic(HEADER + unselected*(16*1024**2//len(unselected)+1)
                                                       + capture(good)[len(HEADER):])

    def test_cli_reuses_common_and_refuses_existing_output_or_incomplete_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base/"saved.csv"
            source.write_text(capture(frames(SAMPLE)))
            target = base/"output"
            command = [sys.executable, str(ROOT/"scripts/convert_dronecan_capture.py"),
                       str(source), str(target), "--protocol", "nmea2000-engine-dynamic"]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            common = self.common((target/"observations.csv").read_text())
            self.assertEqual(common["main_rows"], 1)
            self.assertEqual(json.loads((target/"accounting.json").read_text())["completed_packets"], 1)
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.iterdir()})
            source.write_text(capture(frames(SAMPLE)[:-1]))
            failed = base/"failed"
            command[3] = str(failed)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertFalse(failed.exists())


if __name__ == "__main__":
    unittest.main()
