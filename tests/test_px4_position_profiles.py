"""Independent authored-only contracts for two explicit position CSV layouts.

Run with MUSUBI_PUBLIC_REPLAY_ROOT to also check the existing built public SPI.
No provider recordings or filesystem coordinates are embedded in the fixtures.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(os.environ.get("MUSUBI_POSITION_CSV_ROOT", Path(__file__).resolve().parents[1]))
CASE = Path(os.environ.get("MUSUBI_PX4_PROFILE_DIR", ROOT / "examples" / "px4-position-csv"))
SCRIPT = ROOT / "scripts" / "convert_position_csv.py"
HEADERS = {
    "legacy": "timestamp,lat,lon,alt,heading,unknown",
    "modern": "timestamp,latitude_deg,longitude_deg,altitude_msl_m,heading,unknown",
}
ROWS = {
    "legacy": "0007,125000000,-452500000,-1234,NaN,authored",
    "modern": "0007,12.5000000,-45.2500000,-1.234,NaN,authored",
}


class Px4PositionProfiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("px4_csv_contract_target", SCRIPT)
        cls.converter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.converter)

    def profile(self, layout):
        return json.loads((CASE / f"{layout}.json").read_text(encoding="utf-8"))

    def raw(self, layout, row=None):
        return (HEADERS[layout] + "\n" + (ROWS[layout] if row is None else row) + "\n").encode()

    def convert(self, layout, raw=None):
        return self.converter.convert(self.raw(layout) if raw is None else raw,
                                      self.profile(layout))

    def test_profiles_declare_exact_fields_units_and_reference(self):
        common = {"message_type": "RECORDED_POSITION", "counter_field": "timestamp",
                  "counter_unit": "us", "altitude_reference": "msl"}
        for layout, fields in {
            "legacy": {"latitude_field": "lat", "longitude_field": "lon",
                       "altitude_field": "alt", "angle_unit": "degE7", "altitude_unit": "mm"},
            "modern": {"latitude_field": "latitude_deg", "longitude_field": "longitude_deg",
                       "altitude_field": "altitude_msl_m", "angle_unit": "deg", "altitude_unit": "m"},
        }.items():
            with self.subTest(layout=layout):
                self.assertEqual(self.profile(layout), {**common, **fields})

    def test_canonical_mapping_is_unknown_and_msl(self):
        self.assertEqual(self.profile("position"), {
            "message_type": "RECORDED_POSITION", "latitude_field": "lat_deg",
            "longitude_field": "lon_deg", "altitude_field": "alt_m",
            "position_units": "deg-m-msl", "domain": "unknown",
        })

    def test_both_layouts_normalize_exactly_and_preserve_every_source_string(self):
        for layout in HEADERS:
            with self.subTest(layout=layout):
                record = json.loads(self.convert(layout))
                self.assertEqual(record, {
                    "meta": {"type": "RECORDED_POSITION"},
                    "data": {"TimeUS": 7, "lat_deg": 12.5, "lon_deg": -45.25, "alt_m": -1.234,
                             "source_record": dict(zip(HEADERS[layout].split(","),
                                                        ROWS[layout].split(",")))},
                })

    def test_unknown_nan_heading_and_quoted_text_do_not_become_position_policy(self):
        for layout in HEADERS:
            with self.subTest(layout=layout):
                header = HEADERS[layout].replace("unknown", " unknown ")
                row = ROWS[layout].replace("authored", '"future, value ""quoted"""')
                record = json.loads(self.convert(layout, (header + "\n" + row + "\n").encode()))
                self.assertEqual(record["data"]["source_record"]["heading"], "NaN")
                self.assertEqual(record["data"]["source_record"][" unknown "],
                                 'future, value "quoted"')
                self.assertEqual(set(record["data"]),
                                 {"TimeUS", "lat_deg", "lon_deg", "alt_m", "source_record"})

    def test_ties_and_regressing_counters_keep_source_order_and_count(self):
        for layout in HEADERS:
            with self.subTest(layout=layout):
                rows = [ROWS[layout].replace("0007", counter).replace("authored", label)
                        for counter, label in (("9", "first"), ("9", "second"), ("3", "third"))]
                raw = (HEADERS[layout] + "\n" + "\n".join(rows) + "\n").encode()
                records = [json.loads(line)["data"] for line in self.convert(layout, raw).splitlines()]
                self.assertEqual([r["TimeUS"] for r in records], [9, 9, 3])
                self.assertEqual([r["source_record"]["unknown"] for r in records],
                                 ["first", "second", "third"])

    def test_cross_layout_profiles_fail_in_both_directions(self):
        for selected, source in (("legacy", "modern"), ("modern", "legacy")):
            with self.subTest(selected=selected, source=source), self.assertRaises(ValueError):
                self.convert(selected, self.raw(source))

    def test_bad_or_nonfinite_selected_coordinates_and_altitudes_reject(self):
        for layout in HEADERS:
            profile = self.profile(layout)
            for column in (1, 2, 3):
                for bad in ("NaN", "nan", "inf", "-Infinity", "1e309", "", "null", "1_0"):
                    with self.subTest(layout=layout, column=column, value=bad):
                        cells = ROWS[layout].split(",")
                        cells[column] = bad
                        with self.assertRaises(ValueError):
                            self.converter.convert(self.raw(layout, ",".join(cells)), profile)

    def test_wrong_raw_formats_are_not_implicitly_decoded(self):
        wrong_inputs = (b"ULog\x01\x12\x35\x00\xff", b"\xfd\xff\x00\x01",
                        b'{"timestamp":7,"lat":125000000}\n', b"timestamp;lat;lon;alt\n7;1;2;3\n")
        for layout in HEADERS:
            profile = self.profile(layout)
            for raw in wrong_inputs:
                with self.subTest(layout=layout, raw=raw), self.assertRaises(ValueError):
                    self.converter.convert(raw, profile)

    def test_scaled_coordinate_boundaries_and_outside_values(self):
        for layout, divisor in (("legacy", 10000000), ("modern", 1)):
            profile = self.profile(layout)
            for lat, lon in ((-90, 180), (90, -180)):
                row = f"18446744073709551615,{lat * divisor},{lon * divisor},0,NaN,x"
                data = json.loads(self.convert(layout, self.raw(layout, row)))["data"]
                self.assertEqual((data["lat_deg"], data["lon_deg"]), (lat, lon))
                self.assertEqual(data["TimeUS"], (1 << 64) - 1)
            for lat, lon in ((90.1, 0), (-90.1, 0), (0, 180.1), (0, -180.1)):
                with self.subTest(layout=layout, lat=lat, lon=lon), self.assertRaises(ValueError):
                    self.converter.convert(self.raw(layout, f"1,{lat * divisor},{lon * divisor},0,NaN,x"), profile)

    def test_invalid_microsecond_counters_reject(self):
        for layout in HEADERS:
            profile = self.profile(layout)
            for bad in ("-1", "1.0", "1e3", "18446744073709551616"):
                with self.subTest(layout=layout, counter=bad), self.assertRaises(ValueError):
                    self.converter.convert(self.raw(layout, ROWS[layout].replace("0007", bad)), profile)

    def test_existing_size_bounds_still_apply(self):
        for layout in HEADERS:
            profile = self.profile(layout)
            for raw in (b"x" * (16 * 1024 * 1024 + 1),
                        self.raw(layout, ROWS[layout] + "x" * (64 * 1024))):
                with self.subTest(layout=layout, size=len(raw)), self.assertRaises(ValueError):
                    self.converter.convert(raw, profile)

    def test_late_bad_row_leaves_no_output_and_fixed_cli_failure(self):
        for layout in HEADERS:
            self.profile(layout)  # A missing profile is a test error, not the intended rejection.
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                source, output = work / "authored.csv", work / "out.jsonl"
                source.write_bytes(self.raw(layout) + b"bad,row\n")
                result = subprocess.run([sys.executable, str(SCRIPT), str(source),
                                         str(CASE / f"{layout}.json"), str(output)],
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"position csv conversion failed; verify input, profile and a new output path\n")
                self.assertFalse(output.exists())

    def test_both_layouts_replay_through_existing_public_spi(self):
        public_root = os.environ.get("MUSUBI_PUBLIC_REPLAY_ROOT")
        if not public_root:
            self.skipTest("set MUSUBI_PUBLIC_REPLAY_ROOT to a checkout with the built replay example")
        target = Path(os.environ.get("CARGO_TARGET_DIR", Path(public_root) / "target"))
        binary = Path(os.environ.get("MUSUBI_PUBLIC_REPLAY_BINARY",
                                     target / "debug" / "examples" / "ingest_recorded_jsonl"))
        self.assertTrue(binary.is_file(), "build the existing public replay example first")
        for layout in HEADERS:
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "authored.jsonl"
                source.write_bytes(self.convert(layout))
                result = subprocess.run([str(binary), str(source), str(CASE / "position.json"),
                                         "authored-source", "1789084800000"],
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                report = json.loads(result.stdout)
                self.assertEqual(len(report["records"]), 1)
                self.assertEqual(report["rejected"], [])
                self.assertEqual(report["unmapped_indices"], [])
                self.assertFalse(report["source_records_sealed"])
                self.assertEqual(len(report["mapped"]), 1)
                mapped = report["mapped"][0]
                self.assertEqual(mapped["boot_us"], 7)
                self.assertEqual(mapped["domain"], "Unknown")
                self.assertEqual(mapped["position"],
                                 {"lat_deg": 12.5, "lon_deg": -45.25, "alt_m": -1.234})


if __name__ == "__main__":
    unittest.main()
