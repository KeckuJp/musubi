"""Independent synthetic contracts for explicit boot-counter remapping."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "remap_jsonl_counter.py"
SPEC = importlib.util.spec_from_file_location("counter_remap", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def encode(rows):
    return "\n".join(json.dumps(row) for row in rows).encode()


def profile(field="T", unit="ms"):
    return {"message_type": "GPS", "counter_field": field, "counter_unit": unit}


class CounterRemapTests(unittest.TestCase):
    def test_copter_gps_schema_migration_requires_updated_clock_profile(self):
        # Generated values: only field shapes follow the recorded schema distinction.
        position = {"Lat": 21.125, "Lng": -42.75, "Alt": 18.5, "Status": 3}
        unknown = {"future": [False, {"tag": "synthetic-only"}]}
        old = {"meta": {"type": "GPS"}, "data": {
            **position, **unknown, "T": 2468, "TimeMS": 345678000, "Week": 2345}}
        modern = {"meta": {"type": "GPS"}, "data": {
            **position, **unknown, "TimeUS": 2468000, "GMS": 456789000, "GWk": 2456, "I": 0}}
        with self.assertRaises(ValueError):
            MODULE.remap(encode([modern]), profile("T", "ms"))
        results = [json.loads(MODULE.remap(encode([source]), clock))
                   for source, clock in [(old, profile("T", "ms")),
                                         (modern, profile("TimeUS", "us"))]]
        for source, result in zip([old, modern], results):
            self.assertEqual(result["source_record"], source)
            self.assertEqual(result["data"]["future"], unknown["future"])
            self.assertEqual(result["data"]["TimeUS"], 2468000)
            self.assertEqual({key: result["data"][key] for key in position}, position)
        self.assertNotIn("TimeMS", results[0]["data"])
        self.assertEqual(results[0]["source_record"]["data"]["TimeMS"], 345678000)
        self.assertEqual(results[1]["data"]["GMS"], 456789000)
        self.assertNotEqual(results[0]["data"]["TimeUS"], old["data"]["TimeMS"] * 1000)
        self.assertNotEqual(results[1]["data"]["TimeUS"], modern["data"]["GMS"] * 1000)

    def test_explicit_boot_counter_preserves_complete_original_and_other_rows(self):
        rows = [
            {"meta": {"type": "GPS", "extra": 7}, "data": {
                "TimeMS": 543210000, "T": 1234, "Lat": 12.5, "unknown": [1, {"v": True}]},
             "extra": {"nested": "synthetic"}},
            {"meta": {"type": "OTHER"}, "data": {"TimeMS": 19}},
        ]
        got = [json.loads(line) for line in MODULE.remap(encode(rows), profile()).splitlines()]
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["source_record"], rows[0])
        self.assertEqual(got[0]["data"]["TimeUS"], 1234000)
        self.assertNotIn("TimeMS", got[0]["data"])
        self.assertEqual(got[0]["data"]["unknown"], rows[0]["data"]["unknown"])
        self.assertEqual(got[0]["extra"], rows[0]["extra"])
        self.assertEqual(got[0]["data"]["T"], 1234)
        self.assertEqual(got[1], rows[1])

    @unittest.skipUnless(os.environ.get("MUSUBI_PUBLIC_REPLAY_ROOT"),
                         "public-only E2E: set MUSUBI_PUBLIC_REPLAY_ROOT to projected public workspace")
    def test_copter_gps_migration_and_third_input_reach_same_spi_contract(self):
        position = {"Lat": 21.125, "Lng": -42.75, "Alt": 18.5, "Status": 3}
        unknown = {"future": [False, {"tag": "synthetic-only"}]}
        old = {"meta": {"type": "GPS"}, "data": {
            **position, **unknown, "T": 2468, "TimeMS": 345678000, "Week": 2345}}
        modern = {"meta": {"type": "GPS"}, "data": {
            **position, **unknown, "TimeUS": 2468000, "GMS": 456789000, "GWk": 2456, "I": 0}}
        third = {"meta": {"type": "GPS"}, "data": {
            "Lat": -11.25, "Lng": 62.125, "Alt": -7.5, "Status": 3,
            "future": {"other": [9, True]}, "TimeUS": 9876543,
            "GMS": 567890000, "GWk": 2567, "I": 0}}
        updated_profile = profile("TimeUS", "us")
        with self.assertRaises(ValueError):
            MODULE.remap(encode([modern]), profile("T", "ms"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"message_type": "GPS", "latitude_field": "Lat",
                "longitude_field": "Lng", "altitude_field": "Alt", "position_units": "deg-m-msl"}))
            mapped_results = []
            for observation, clock, expected_boot in [(old, profile("T", "ms"), 2468000),
                    (modern, updated_profile, 2468000), (third, updated_profile, 9876543)]:
                rows = [observation, {"meta": {"type": "META"}, "data": {}},
                        {"meta": {"type": "OTHER"}, "data": {"TimeUS": 8}}]
                converted = MODULE.remap(encode(rows), clock)
                source = root / "remapped.jsonl"
                source.write_bytes(converted)
                completed = subprocess.run(["cargo", "run", "--quiet", "--offline", "--locked",
                    "-p", "musubi-adapter-spi", "--example", "ingest_recorded_jsonl", "--",
                    str(source), str(mapping), "synthetic", "1700000000123"],
                    cwd=Path(os.environ["MUSUBI_PUBLIC_REPLAY_ROOT"]), capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                report = json.loads(completed.stdout)
                self.assertEqual(report["records"], [json.loads(line) for line in converted.splitlines()])
                self.assertEqual(report["records"][0]["source_record"], observation)
                self.assertEqual(report["records"][0]["data"]["future"], observation["data"]["future"])
                self.assertEqual(report["untimed_indices"], [1])
                self.assertEqual(report["unmapped_indices"], [2])
                self.assertEqual(report["rejected"], [])
                self.assertEqual(len(report["mapped"]), 1)
                mapped = report["mapped"][0]
                self.assertEqual((mapped["record_index"], mapped["boot_us"]), (0, expected_boot))
                self.assertEqual(mapped["domain"], "Unknown")
                self.assertEqual(mapped["time_confidence"], 0)
                self.assertIsNone(mapped["observed_at"])
                self.assertEqual(mapped["received_at"], 1700000000123)
                self.assertEqual(mapped["position"], {"lat_deg": observation["data"]["Lat"],
                    "lon_deg": observation["data"]["Lng"], "alt_m": observation["data"]["Alt"]})
                mapped_results.append(mapped)
            for key in ["boot_us", "position", "domain", "time_confidence", "observed_at", "received_at"]:
                self.assertEqual(mapped_results[0][key], mapped_results[1][key])

    def test_new_microseconds_and_unseen_field_values_use_same_implementation(self):
        for field, unit, value, expected in [
            ("TimeUS", "us", 9007199254740993, 9007199254740993),
            ("BootCounter", "ms", 9876, 9876000),
            ("BootCounter", "us", 18446744073709551615, 18446744073709551615),
        ]:
            original = {"meta": {"type": "GPS"}, "data": {field: value, "extra": False}}
            result = json.loads(MODULE.remap(encode([original]), profile(field, unit)))
            self.assertEqual(result["data"]["TimeUS"], expected)
            self.assertEqual(result["source_record"], original)

    def test_invalid_counters_and_source_collision_rejected(self):
        for value in [-1, 1.5, True, None, "12", 18446744073709552]:
            with self.subTest(value=value), self.assertRaises(Exception):
                MODULE.remap(encode([{"meta": {"type": "GPS"}, "data": {"T": value}}]), profile())
        for row in [
            {"meta": {"type": "GPS"}, "data": {}},
            {"meta": {"type": "GPS"}, "data": {"T": 1}, "source_record": {}},
            {"meta": {"type": "OTHER"}, "data": {"T": 1}},
        ]:
            with self.subTest(row=row), self.assertRaises(Exception):
                MODULE.remap(encode([row]), profile())

    def test_strict_json_and_closed_profile(self):
        valid = encode([{"meta": {"type": "GPS"}, "data": {"T": 1}}])
        for raw in [b"", b"{", b"[]", b"\xff",
                    b'{"meta":{"type":"GPS"},"data":{"T":1,"T":2}}',
                    b'{"meta":{"type":"GPS"},"data":{"T":1,"v":NaN}}']:
            with self.subTest(raw=raw), self.assertRaises(Exception):
                MODULE.remap(raw, profile())
        for bad in [{}, {**profile(), "extra": True}, {**profile(), "counter_unit": "seconds"},
                    {**profile(), "counter_field": ""}, {**profile(), "message_type": 7}]:
            with self.subTest(bad=bad), self.assertRaises(Exception):
                MODULE.remap(valid, bad)

    def test_bounds_and_late_failure_are_atomic(self):
        row = {"meta": {"type": "GPS"}, "data": {"T": 1}}
        deep = 0
        for _ in range(34):
            deep = [deep]
        for raw in [encode([{**row, "pad": "x" * 65536}]), b" " * (16 * 1024 * 1024 + 1),
                    encode([{**row, "deep": deep}]), encode([row]) + b"\n{"]:
            with self.assertRaises(Exception):
                MODULE.remap(raw, profile())

    def test_output_expansion_is_bounded_and_empty_message_type_is_rejected(self):
        row = {"meta": {"type": "GPS"}, "data": {"T": 1}, "pad": "x" * 33000}
        self.assertLess(len(encode([row])), 65536)
        with self.assertRaises(ValueError):
            MODULE.remap(encode([row]), profile())
        # Each row fits individually; retained originals make the whole output too large.
        smaller = {**row, "pad": "x" * 16000}
        raw = encode([smaller] * 525)
        self.assertLess(len(raw), 16 * 1024 * 1024)
        with self.assertRaises(ValueError):
            MODULE.remap(raw, profile())
        with self.assertRaises(ValueError):
            MODULE.remap(encode([
                {"meta": {"type": "GPS"}, "data": {"T": 1}},
                {"meta": {"type": ""}, "data": {}},
            ]), profile())

    @unittest.skipUnless(os.environ.get("MUSUBI_PUBLIC_REPLAY_ROOT"),
                         "public-only E2E: set MUSUBI_PUBLIC_REPLAY_ROOT to projected public workspace")
    def test_public_reader_and_spi_reuse_old_new_and_held_out_counters(self):
        workspace = Path(os.environ["MUSUBI_PUBLIC_REPLAY_ROOT"])
        cases = [("T", "ms", 1234, 1234000),
                 ("TimeUS", "us", 9007199254740993, 9007199254740993),
                 ("BootCounter", "ms", 9876, 9876000)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"message_type": "GPS", "latitude_field": "Lat",
                "longitude_field": "Lng", "altitude_field": "Alt", "position_units": "deg-m-msl"}))
            for field, unit, value, expected in cases:
                with self.subTest(counter=field):
                    rows = [{"meta": {"type": "GPS"}, "data": {"TimeMS": 543210000,
                            field: value, "Lat": -12.25, "Lng": 73.5, "Alt": -4.0,
                            "unknown": [True, {"v": 7}]}},
                            {"meta": {"type": "META"}, "data": {"note": "synthetic"}},
                            {"meta": {"type": "OTHER"}, "data": {"TimeUS": 9}}]
                    remapped = MODULE.remap(encode(rows), profile(field, unit))
                    source = root / "remapped.jsonl"
                    source.write_bytes(remapped)
                    completed = subprocess.run(["cargo", "run", "--quiet", "--offline", "--locked",
                        "-p", "musubi-adapter-spi", "--example", "ingest_recorded_jsonl", "--",
                        str(source), str(mapping), "synthetic", "1700000000123"],
                        cwd=workspace, capture_output=True, check=False)
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                    report = json.loads(completed.stdout)
                    self.assertEqual(report["records"], [json.loads(line) for line in remapped.splitlines()])
                    self.assertEqual(report["records"][0]["source_record"], rows[0])
                    self.assertEqual(report["untimed_indices"], [1])
                    self.assertEqual(report["unmapped_indices"], [2])
                    self.assertEqual(report["rejected"], [])
                    self.assertEqual(len(report["mapped"]), 1)
                    result = report["mapped"][0]
                    self.assertEqual((result["record_index"], result["boot_us"]), (0, expected))
                    self.assertEqual(result["domain"], "Unknown")
                    self.assertEqual(result["time_confidence"], 0)
                    self.assertIsNone(result["observed_at"])
                    self.assertEqual(result["received_at"], 1700000000123)
                    self.assertEqual(result["position"], {"lat_deg": -12.25, "lon_deg": 73.5, "alt_m": -4.0})

    def test_cli_new_file_only_profile_strictness_and_no_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, config, output = root / "input.jsonl", root / "profile.json", root / "out.jsonl"
            source.write_bytes(encode([{"meta": {"type": "GPS"}, "data": {"T": 7}}]))
            config.write_text(json.dumps(profile()))
            def run():
                return subprocess.run([sys.executable, str(SCRIPT), str(source), str(config), str(output)],
                                      capture_output=True, check=False)
            self.assertEqual(run().returncode, 0)
            initial = output.read_bytes()
            self.assertNotEqual(run().returncode, 0)
            self.assertEqual(output.read_bytes(), initial)
            output.unlink()
            source.write_bytes(source.read_bytes() + b"\n{")
            self.assertNotEqual(run().returncode, 0)
            self.assertFalse(output.exists())
            source.write_bytes(encode([{"meta": {"type": "GPS"}, "data": {"T": 7}}]))
            for text in ['{"message_type":"GPS","counter_field":"T","counter_unit":"ms","counter_unit":"us"}',
                         json.dumps(profile()) + " " * 4096]:
                config.write_text(text)
                self.assertNotEqual(run().returncode, 0)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
