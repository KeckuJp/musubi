"""Offline synthetic CLI checks; build the ingest_recorded_csv example first."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get("MUSUBI_PUBLIC_REPLAY_ROOT", Path(__file__).resolve().parents[1]))
TARGET = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
BINARY = TARGET / "debug" / "examples" / "ingest_recorded_csv"
PROFILE = {"message_type": "POS", "latitude_field": "Lat", "longitude_field": "Lng",
           "altitude_field": "Alt", "position_units": "deg-m-msl"}


class RecordedCsvCliTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BINARY.is_file(), "build the ingest_recorded_csv example first")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.input = Path(temporary.name) / "synthetic input.csv"
        self.profile = Path(temporary.name) / "profile.json"
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")

    def invoke(self, source, *options):
        self.input.write_bytes(source)
        return subprocess.run(
            [str(BINARY), str(self.input), str(self.profile), "synthetic-csv",
             "1700000000123", "Tick", *options], cwd=ROOT, capture_output=True, timeout=20)

    def test_success_retains_lexical_source_once_and_maps_declared_position(self):
        source = b"Tick (us),Lat (deg),Lng (deg),Alt (m),Opaque\r\n0007, 12.50,-45.25,-1.234,kept-token\r\n"
        result = self.invoke(source)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        report = json.loads(result.stdout)
        self.assertEqual(report["source_csv"], source.decode())
        self.assertEqual(result.stdout.count(b"kept-token"), 1)
        self.assertEqual(report["source_record_count"], 1)
        self.assertEqual(report["clock_basis"], "boot-microseconds-declared")
        self.assertFalse(report["source_records_sealed"])
        self.assertEqual(report["rejected"], [])
        mapped, = report["mapped"]
        self.assertEqual((mapped["record_index"], mapped["boot_us"]), (0, 7))
        self.assertEqual(mapped["position"], {"lat_deg": 12.5, "lon_deg": -45.25, "alt_m": -1.234})
        self.assertEqual(mapped["domain"], "Unknown")
        self.assertIsNone(mapped["observed_at"])
        self.assertEqual((mapped["confidence"], mapped["time_confidence"]), (0, 0))

    def test_indexed_rejection_emits_complete_report_and_nonzero_exit(self):
        result = self.invoke(b"Tick,Lat,Lng,Alt\n0,1,2,3\n1,,2,3\n2,4,5,6\n")
        self.assertEqual((result.returncode, result.stderr), (1, b""))
        report = json.loads(result.stdout)
        self.assertEqual(report["source_record_count"], 3)
        self.assertEqual([r["record_index"] for r in report["mapped"]], [0, 2])
        rejected, = report["rejected"]
        self.assertEqual((rejected["record_index"], rejected["mark"]), (1, "Invalid"))
        self.assertEqual(rejected["source_id"], "synthetic-csv")

    def test_equal_time_and_nonfinite_options_are_explicit(self):
        source = b"Tick,Lat,Lng,Alt,Unknown\n0,1,2,3,NaN\n0,4,5,6,inf\n"
        for options in [(), ("--allow-equal-time",), ("--preserve-nonfinite-as-text",)]:
            result = self.invoke(source, *options)
            self.assertEqual((result.returncode, result.stdout), (1, b""))
            self.assertEqual(json.loads(result.stderr), {"error": "invalid-csv"})
        result = self.invoke(source, "--allow-equal-time", "--preserve-nonfinite-as-text")
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(len(json.loads(result.stdout)["mapped"]), 2)

    def test_structure_and_unknown_option_errors_do_not_echo_source_or_paths(self):
        cases = [(b"Tick,Lat,Lng,Alt\n0,1,2,3\nsentinel-sensitive-value\n", (), "invalid-csv"),
                 (b"Tick,Lat,Lng,Alt\n0,1,2,3\n", ("--unknown-sensitive-value",), "unknown-option")]
        for source, options, reason in cases:
            with self.subTest(reason=reason):
                result = self.invoke(source, *options)
                self.assertEqual((result.returncode, result.stdout), (1, b""))
                self.assertEqual(result.stderr, (json.dumps({"error": reason}, separators=(",", ":")) + "\n").encode())
        self.profile.unlink()
        result = self.invoke(b"Tick,Lat,Lng,Alt\n0,1,2,3\n")
        self.assertEqual((result.returncode, result.stdout), (1, b""))
        self.assertEqual(json.loads(result.stderr), {"error": "input-unreadable"})


if __name__ == "__main__":
    unittest.main()
