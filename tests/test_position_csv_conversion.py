"""Authored synthetic contracts for an offline CSV position adaptation component.

Run against a source checkout via MUSUBI_POSITION_CSV_ROOT, or place in its tests/.
No real recordings, private source coordinates or provider data are embedded here.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(os.environ.get("MUSUBI_POSITION_CSV_ROOT", Path(__file__).resolve().parents[1]))
SCRIPT = ROOT / "scripts" / "convert_position_csv.py"
PROFILE = {
    "message_type": "RECORDED_POSITION",
    "counter_field": "clock",
    "counter_unit": "us",
    "latitude_field": "north",
    "longitude_field": "east",
    "angle_unit": "deg",
    "altitude_field": "height",
    "altitude_unit": "m",
    "altitude_reference": "msl",
}
HEADER = b"clock,north,east,height,note\n"
SIMPLE = HEADER + b"1001,12.5,-45.25,123.4,authored\n"


class PositionCsvContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("position_csv_under_test", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def convert(self, raw=SIMPLE, **changes):
        return self.module.convert(raw, {**PROFILE, **changes})

    def records(self, raw=SIMPLE, **changes):
        output = self.convert(raw, **changes)
        self.assertIsInstance(output, bytes)
        self.assertTrue(output.endswith(b"\n"))
        return [json.loads(line) for line in output.splitlines()]

    def rejects(self, raw=SIMPLE, **changes):
        with self.assertRaises(ValueError):
            self.convert(raw, **changes)

    def test_declared_fields_map_to_exact_common_record_shape(self):
        self.assertEqual(self.records(), [{
            "meta": {"type": "RECORDED_POSITION"},
            "data": {"TimeUS": 1001, "lat_deg": 12.5, "lon_deg": -45.25, "alt_m": 123.4,
                     "source_record": {"clock": "1001", "north": "12.5", "east": "-45.25",
                                       "height": "123.4", "note": "authored"}},
        }])

    def test_exporter_timestamp_is_retained_but_not_used_as_clock(self):
        raw = b"timestamp,TimeUS,Lat,Lng,Alt\n1700000000.25,1007,1.25,-2.5,30.0\n"
        row = self.records(raw, counter_field="TimeUS", latitude_field="Lat",
                           longitude_field="Lng", altitude_field="Alt")[0]
        self.assertEqual(row["data"]["TimeUS"], 1007)
        self.assertEqual(row["data"]["source_record"]["timestamp"], "1700000000.25")
        self.assertNotIn("observed_at", row["data"])
        self.assertNotIn("domain", row["data"])

    def test_unseen_names_and_dotted_columns_are_profile_selected(self):
        raw = b"Vendor.Clock,Geo.Lat,Geo.Lon,Geo.Alt,rev\n17,3.25,-4.5,87,unseen\n"
        row = self.records(raw, counter_field="Vendor.Clock", counter_unit="ms",
                           latitude_field="Geo.Lat", longitude_field="Geo.Lon",
                           altitude_field="Geo.Alt")[0]
        self.assertEqual(row["data"]["TimeUS"], 17000)
        self.assertEqual(row["data"]["source_record"]["rev"], "unseen")

    def test_dm_altitude_converted_once_not_inferred_from_header(self):
        raw = b"clock,north,east,height,note\n2,-0.25,0.125,1234,authored\n"
        row = self.records(raw, altitude_unit="dm")[0]["data"]
        self.assertAlmostEqual(row["alt_m"], 123.4)
        self.assertEqual(row["source_record"]["height"], "1234")

    def test_millimeters_and_scaled_degrees_are_explicit(self):
        raw = HEADER + b"7,501234567,-10000000,-1234,authored\n"
        row = self.records(raw, angle_unit="degE7", altitude_unit="mm")[0]["data"]
        self.assertAlmostEqual(row["lat_deg"], 50.1234567)
        self.assertEqual(row["lon_deg"], -1.0)
        self.assertAlmostEqual(row["alt_m"], -1.234)
        self.rejects(raw)  # Same values must not silently infer a scaled coordinate representation.

    def test_original_header_and_lexical_values_remain_available(self):
        raw = b' clock , north , east , height , note \r\n 0007 , 1.2500 , -2.500 , 03.00 ,"vendor, text"\r\n'
        row = self.records(raw)[0]["data"]
        self.assertEqual(row["TimeUS"], 7)
        self.assertEqual(row["source_record"][" clock "], " 0007 ")
        self.assertEqual(row["source_record"][" note "], "vendor, text")

    def test_equal_and_out_of_order_times_are_not_sorted_or_deduplicated(self):
        raw = HEADER + b"3,1,2,3,a\n3,4,5,6,b\n2,7,8,9,c\n"
        rows = self.records(raw)
        self.assertEqual([r["data"]["TimeUS"] for r in rows], [3, 3, 2])
        self.assertEqual([r["data"]["source_record"]["note"] for r in rows], ["a", "b", "c"])

    def test_fix_or_satellite_values_are_preserved_not_filtered_or_promoted(self):
        raw = b"clock,north,east,height,Status,GPS_numSat\n1,0,0,0,0,0\n"
        row = self.records(raw)[0]
        self.assertEqual(row["data"]["source_record"]["Status"], "0")
        self.assertEqual(row["data"]["source_record"]["GPS_numSat"], "0")
        self.assertNotIn("fix", row["data"])
        self.assertNotIn("confidence", row["data"])

    def test_u64_microseconds_and_boundary_coordinates(self):
        row = self.records(HEADER + b"18446744073709551615,-90,180,-1,x\n")[0]["data"]
        self.assertEqual(row["TimeUS"], 18446744073709551615)
        self.assertEqual(row["lat_deg"], -90.0)
        self.assertEqual(row["lon_deg"], 180.0)

    def test_unsigned_integer_counter_grammar_and_overflow(self):
        for bad in ("-1", "+1", "1.0", "1e3", "true", "", "18446744073709551616"):
            with self.subTest(counter=bad):
                self.rejects(HEADER + f"{bad},1,2,3,x\n".encode())
        self.rejects(HEADER + b"18446744073709552,1,2,3,x\n", counter_unit="ms")
        row = self.records(HEADER + b"18446744073709551,1,2,3,x\n", counter_unit="ms")[0]
        self.assertEqual(row["data"]["TimeUS"], 18446744073709551000)

    def test_invalid_position_values_are_rejected_not_coerced(self):
        for bad in ("NaN", "nan", "inf", "-Infinity", "1e309", "true", "", "null"):
            for index in (1, 2, 3):
                with self.subTest(value=bad, column=index):
                    values = ["1", "2", "3", "4", "x"]
                    values[index] = bad
                    self.rejects(HEADER + (",".join(values) + "\n").encode())

    def test_coordinate_ranges(self):
        for latitude, longitude in ((90.01, 0), (-90.01, 0), (0, 180.01), (0, -180.01)):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.rejects(HEADER + f"1,{latitude},{longitude},0,x\n".encode())

    def test_profile_has_exact_nine_required_keys(self):
        for key in PROFILE:
            with self.subTest(missing=key):
                profile = {k: v for k, v in PROFILE.items() if k != key}
                with self.assertRaises(ValueError):
                    self.module.convert(SIMPLE, profile)
        self.rejects(extra="ignored")
        self.rejects(fix_field="Status", min_fix=3)

    def test_unsupported_units_reference_and_profile_types(self):
        for key, bad in (("counter_unit", "s"), ("angle_unit", "rad"),
                         ("altitude_unit", "cm"), ("altitude_reference", "relative-home"),
                         ("altitude_reference", "ellipsoid"), ("message_type", ""),
                         ("latitude_field", ""), ("counter_field", 1),
                         ("longitude_field", True), ("altitude_field", None)):
            with self.subTest(key=key, value=bad):
                self.rejects(**{key: bad})
        for profile in (None, [], "profile"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.module.convert(SIMPLE, profile)

    def test_missing_blank_and_duplicate_column_names(self):
        for raw in (b"clock,north,east\n1,2,3\n",
                    b"clock,north,east,height,height\n1,2,3,4,5\n",
                    b"clock,north,east,height, height \n1,2,3,4,5\n",
                    b"clock,north,east,height,\n1,2,3,4,5\n"):
            with self.subTest(header=raw.splitlines()[0]):
                self.rejects(raw)

    def test_bad_csv_empty_and_wrong_formats(self):
        for raw in (b"", HEADER, HEADER + b"\n", HEADER + b"1,2,3\n",
                    HEADER + b"1,2,3,4,x,extra\n", HEADER + b'1,2,3,4,"unterminated\n',
                    b'{"meta":{"type":"POSITION"},"data":{"TimeUS":1}}\n',
                    b"H Product:Blackbox flight data recorder by Nicholas Sherlock\n",
                    HEADER + b"1,2,3,4,\xff\n", HEADER + b"1,2,3,4,bad\0text\n"):
            with self.subTest(kind=raw[:30]):
                self.rejects(raw)

    def test_input_and_line_size_limits(self):
        self.rejects(b" " * (16 * 1024 * 1024 + 1))
        self.rejects(HEADER + b"1,2,3,4," + b"a" * (64 * 1024) + b"\n")

    def test_deterministic_no_profile_mutation(self):
        profile = copy.deepcopy(PROFILE)
        first = self.module.convert(SIMPLE, profile)
        self.assertEqual(profile, PROFILE)
        self.assertEqual(first, self.module.convert(SIMPLE, profile))


class PositionCsvCliContract(unittest.TestCase):
    def invoke(self, input_bytes=SIMPLE, profile_bytes=None, existing=None):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, profile, output = root / "secret-source.csv", root / "profile.json", root / "out.jsonl"
            source.write_bytes(input_bytes)
            profile.write_bytes(profile_bytes if profile_bytes is not None else json.dumps(PROFILE).encode())
            if existing is not None:
                output.write_bytes(existing)
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(profile), str(output)],
                                    capture_output=True, timeout=20)
            return result, output.read_bytes() if output.exists() else None

    def assert_generic_failure(self, result):
        self.assertEqual(result.returncode, 1)
        text = result.stdout + result.stderr
        self.assertNotIn(b"secret-source", text)
        self.assertNotIn(b"Traceback", text)
        self.assertNotIn(b"private-marker", text)

    def test_success_writes_one_output(self):
        result, output = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(output)["data"]["TimeUS"], 1001)

    def test_existing_output_is_never_overwritten(self):
        result, output = self.invoke(existing=b"retain-existing")
        self.assert_generic_failure(result)
        self.assertEqual(output, b"retain-existing")

    def test_late_malformed_row_does_not_leave_partial_output(self):
        result, output = self.invoke(SIMPLE + b"2,private-marker,3,4,x\n")
        self.assert_generic_failure(result)
        self.assertIsNone(output)

    def test_duplicate_profile_keys_rejected(self):
        profile = json.dumps(PROFILE).replace('"counter_unit": "us"', '"counter_unit":"ms","counter_unit":"us"')
        result, output = self.invoke(profile_bytes=profile.encode())
        self.assert_generic_failure(result)
        self.assertIsNone(output)

    def test_profile_limit_and_bad_json_rejected(self):
        for profile in (b" " * 4097, b'{"private-marker":', b"[]", b"null"):
            with self.subTest(size=len(profile)):
                result, output = self.invoke(profile_bytes=profile)
                self.assert_generic_failure(result)
                self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
