"""Authored Skydio Cloud v1 telemetry bodies; no client, token, account or real record.

Every input is built here from the shape the pinned endpoint page documents
(`apidocs.skydio.com/reference/telemetry_get_v1_flight_flight_id_telemetry.md`, whose exact content is
pinned by digest in this unit's private task evidence). Units and axis orders are asserted exactly as
that page declares them -- including the `data` envelope, the north-east-down GPS velocity, the
east-north-up Site positions and the battery's declared 0-to-1 range. Real records 0.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_skydio_flight_telemetry.py"
PROFILE = ROOT / "profiles/declared/skydio-flight-telemetry/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
FORMAT = module.SOURCE_FORMAT


def sample(stamp="2022-05-03T03:10:52.503+00:00", **changes):
    """One aligned_telemetry sample in the documented shape.

    The declared quaternion arrays are present on purpose: the page declares them, this path gives
    them no column, and they are what proves retention keeps uninterpreted values recoverable. The
    battery is inside the page's declared 0-to-1 range.
    """
    data = {"timestamp": stamp, "battery_percentage": 0.875, "gps_latitude": 37.4219,
            "gps_longitude": -122.0841, "gps_altitude": 31.25,
            "gps_velocity": [1.5, -2.25, 0.125], "gps_horizontal_accuracy": 0.8,
            "gps_vertical_accuracy": 1.2, "gps_speed_accuracy": 0.3,
            "gps_num_satellites_used": 14, "height_above_takeoff": 18.75,
            "hybrid_altitude": 30.0,
            "hybrid_camera_orientation_rpy_ned": [0.5, -12.0, 271.25],
            "site_position_enu": [10.5, -4.25, 18.0],
            "site_orientation_rpy_enu": [0.0, -1.5, 90.0],
            "site_camera_position_enu": [10.6, -4.2, 18.1],
            "site_camera_orientation_rpy_enu": [0.1, -30.0, 91.0],
            "site_orientation_quat_enu": [0.0, 0.0, 0.7071067811865476, 0.7071067811865475],
            "site_camera_orientation_quat_enu": [0.1, -0.2, 0.3, 0.927361849549570]}
    data.update(changes)
    return data


def body(samples=None, envelope_changes=None, **flight_changes):
    """The documented 200 envelope: `flight` and `flight_telemetry` sit inside `data`."""
    flight = {"flight_id": "flight-authored-1", "vehicle_serial": "veh-authored-1",
              "battery_serial": "bat-authored-1", "user_email": "operator@example.invalid",
              "takeoff": "2022-05-03T03:10:00.000+00:00",
              "landing": "2022-05-03T03:20:00.000+00:00", "has_telemetry": True,
              "markers": [{"incident_id": "authored-incident-1", "uuid": "authored-uuid-1"}]}
    flight.update(flight_changes)
    flight = {key: value for key, value in flight.items() if value is not None}  # None = omit
    document = {"data": {"flight": flight,
                         "flight_telemetry": {"aligned_telemetry": samples or [sample()]}},
                "status_code": 200, "skydio_error_code": 0, "error_message": None,
                "meta": {"time": 0.031}}
    document.update(envelope_changes or {})
    return json.dumps(document)


PLACEHOLDER = "@@LEXEME@@"
FLIGHT_LEXEME_KEYS = ("takeoff_latitude", "takeoff_longitude")


def lexeme_body(lexeme, **changes):
    """A body whose JSON text carries `lexeme` verbatim wherever PLACEHOLDER was put.

    Substituting into the text is the point: writing the number through `json.dumps` would round it
    here in the test exactly as the converter used to round it, so the regression would prove nothing.
    """
    flight_changes = {key: value for key, value in changes.items() if key in FLIGHT_LEXEME_KEYS}
    sample_changes = {key: value for key, value in changes.items()
                      if key not in FLIGHT_LEXEME_KEYS}
    text = body([sample(**sample_changes)], **flight_changes)
    return text.replace(json.dumps(PLACEHOLDER), lexeme)


class SkydioFlightTelemetryTests(unittest.TestCase):
    def rows(self, text=None):
        converted, report = module.convert(text or body(), source_format=FORMAT)
        return list(csv.DictReader(io.StringIO(converted))), report, converted

    def test_declared_quantities_and_identity_reach_the_selected_columns(self):
        rows, report, _ = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["record_time_us"], "1651547452503000")
        self.assertEqual(float(row["skydio_gps_altitude_m"]), 31.25)
        self.assertEqual(float(row["skydio_height_above_takeoff_m"]), 18.75)
        self.assertEqual(float(row["skydio_hybrid_altitude_m"]), 30.0)
        self.assertEqual(float(row["skydio_gps_latitude_deg"]), 37.4219)
        self.assertEqual(float(row["skydio_gps_longitude_deg"]), -122.0841)
        self.assertEqual(float(row["skydio_gps_horizontal_accuracy_m"]), 0.8)
        self.assertEqual(float(row["skydio_gps_vertical_accuracy_m"]), 1.2)
        self.assertEqual(float(row["skydio_gps_speed_accuracy_m_s"]), 0.3)
        self.assertEqual([float(row[f"skydio_gps_velocity_ned_m_s_{a}"])
                          for a in ("north", "east", "down")], [1.5, -2.25, 0.125])
        self.assertEqual([float(row[f"skydio_site_position_enu_m_{a}"])
                          for a in ("east", "north", "up")], [10.5, -4.25, 18.0])
        self.assertEqual(float(row["skydio_site_orientation_rpy_enu_deg_yaw"]), 90.0)
        self.assertEqual(float(row["skydio_hybrid_camera_orientation_rpy_ned_deg_yaw"]), 271.25)
        self.assertEqual(bytes.fromhex(row["skydio_flight_id_hex"][4:]).decode(), "flight-authored-1")
        self.assertEqual(bytes.fromhex(row["skydio_vehicle_serial_hex"][4:]).decode(), "veh-authored-1")
        self.assertNotIn("operator@example.invalid", json.dumps(rows))
        self.assertEqual([item["path"] for item in report["redacted_keys"]], ["flight.user_email"])
        self.assertEqual(report["completeness"], "NOT_DECLARED_BY_THE_ENDPOINT_PAGE")
        self.assertIn("NOT_AN_ATTESTED", row["skydio_clock_basis"])
        self.assertIn("DELIVERY_SOURCE", row["skydio_producer_basis"])
        self.assertEqual(report["envelope"]["status_code"], 200)
        self.assertEqual(report["envelope"]["skydio_error_code"], 0)
        self.assertEqual(report["envelope"]["meta_json"], '{"time":0.031}')
        self.assertIn("NEITHER_A_SUCCESS_VALUE_NOR_A_UNIT", report["envelope"]["basis"])

    def test_the_battery_is_the_declared_fraction_and_a_range_violation_is_counted(self):
        rows, report, _ = self.rows()
        self.assertEqual(float(rows[0]["skydio_battery_remaining_fraction"]), 0.875)
        self.assertEqual(report["declared_range_violations"], {})
        rows, report, _ = self.rows(body([sample(battery_percentage=87.5)]))
        self.assertEqual(float(rows[0]["skydio_battery_remaining_fraction"]), 87.5)
        self.assertEqual(report["declared_range_violations"], {"battery_percentage": 1})
        self.assertIn("NEITHER_RESCALED_NOR_", report["declared_range_basis"])

    def test_undeclared_units_are_reported_not_converted_and_absences_are_counted(self):
        rows, report, _ = self.rows()
        row = rows[0]
        self.assertEqual(row["skydio_gps_satellites_used_reported"], "14")
        self.assertEqual(report["absent_selected_fields"], {})
        sparse = sample()
        for key in ("gps_altitude", "site_position_enu", "battery_percentage"):
            sparse.pop(key)
        rows, report, _ = self.rows(body([sparse]))
        for column in ("skydio_gps_altitude_m", "skydio_site_position_enu_m_east",
                       "skydio_battery_remaining_fraction"):
            self.assertEqual(rows[0][column], "", column)
        self.assertEqual(report["absent_selected_fields"],
                         {"gps_altitude": 1, "battery_percentage": 1, "site_position_enu": 1})
        rows, report, _ = self.rows(body(battery_serial=None))
        self.assertEqual(rows[0]["skydio_battery_serial_hex"], "")
        self.assertEqual(report["absent_selected_fields"], {"flight.battery_serial": 1})

    def test_uninterpreted_fields_including_quaternions_stay_recoverable_and_accounted(self):
        """A digest binds bytes; it does not preserve values, so the values are retained too."""
        rows, report, converted = self.rows()
        row = rows[0]
        retained = json.loads(bytes.fromhex(row["source_record_hex"][4:]).decode())
        self.assertEqual(retained["site_orientation_quat_enu"],
                         [0.0, 0.0, 0.7071067811865476, 0.7071067811865475])
        self.assertEqual(retained["site_camera_orientation_quat_enu"][3], 0.927361849549570)
        self.assertEqual(retained, sample())
        self.assertEqual(row["skydio_declared_uninterpreted_key_count"], "2")
        self.assertEqual(row["skydio_undeclared_key_count"], "0")
        self.assertEqual(sorted(report["declared_uninterpreted_keys"]),
                         ["site_camera_orientation_quat_enu", "site_orientation_quat_enu"])
        self.assertEqual(report["declared_uninterpreted_keys"]["site_orientation_quat_enu"],
                         {"samples": 1, "status": module.RETAINED_STATUS})
        rows, report, _ = self.rows(body([sample(future_quantity=7)]))
        self.assertEqual(rows[0]["skydio_undeclared_key_count"], "1")
        self.assertEqual(report["undeclared_retained_keys"],
                         {"future_quantity": {"samples": 1, "status": module.RETAINED_STATUS}})
        self.assertEqual(json.loads(bytes.fromhex(
            rows[0]["source_record_hex"][4:]).decode())["future_quantity"], 7)
        self.assertIn("additionalProperties_FALSE", report["undeclared_keys_basis"])
        context = json.loads(bytes.fromhex(row["skydio_source_context_hex"][4:]).decode())
        self.assertEqual(context["flight"]["takeoff"], "2022-05-03T03:10:00.000+00:00")
        self.assertEqual(context["flight"]["landing"], "2022-05-03T03:20:00.000+00:00")
        self.assertEqual(context["flight"]["markers"][0]["incident_id"], "authored-incident-1")
        self.assertNotIn("user_email", context["flight"])
        rows, report, _ = self.rows(body([sample(), sample(stamp="2022-05-03T03:10:53.503+00:00")]))
        self.assertEqual(rows[1]["skydio_source_context_hex"], "")
        self.assertEqual(report["declared_uninterpreted_keys"]["site_orientation_quat_enu"],
                         {"samples": 2, "status": module.RETAINED_STATUS})
        rows, report, converted = self.rows(
            body([sample(user_email="operator@example.invalid"),
                  sample(stamp="2022-05-03T03:10:53.503+00:00",
                         user_email="operator@example.invalid")]))
        self.assertNotIn("operator@example.invalid", converted)
        self.assertEqual({item["path"]: item["occurrences"] for item in report["redacted_keys"]},
                         {"flight.user_email": 1,
                          "data.flight_telemetry.aligned_telemetry[].user_email": 2})
        with self.assertRaises(ValueError):
            self.rows(body([sample(operator_note="n" * (module.MAX_RETAINED + 1))]))
        self.assertIn("NO_BROAD_PRIVACY_SANITISATION", report["retention"])
        self.assertIn("NOT_A_BYTE_COPY", report["retention"])

    def test_numbers_are_retained_as_the_producers_exact_lexemes(self):
        """`json.loads` rounds a literal to a double, so a re-encoded value is not the source value."""
        long_form = "0.12345678901234567890123456789"
        rows, report, _ = self.rows(lexeme_body(long_form, future_quantity=PLACEHOLDER))
        retained = bytes.fromhex(rows[0]["source_record_hex"][4:]).decode()
        self.assertIn(f'"future_quantity":{long_form}', retained)
        self.assertNotEqual(repr(float(long_form)), long_form)
        self.assertNotIn(repr(float(long_form)), retained)
        precise = "37.42190000000000012345678901"
        rows, _, _ = self.rows(lexeme_body(precise, gps_latitude=PLACEHOLDER))
        self.assertEqual(rows[0]["skydio_gps_latitude_deg"], precise)
        self.assertEqual(float(rows[0]["skydio_gps_latitude_deg"]), float(precise))
        self.assertIn("NO_EQUALITY_BETWEEN_IT_AND_THE_RETAINED_LEXEME_IS_CLAIMED",
                      rows[0]["skydio_retention_basis"])
        with self.assertRaises(ValueError):
            self.rows(lexeme_body("Infinity", future_quantity=PLACEHOLDER))

    def test_context_around_the_samples_is_retained_and_accounted(self):
        """Envelope, data and flight_telemetry siblings are values too, not just a hash."""
        rows, report, _ = self.rows(body(
            envelope_changes={"pagination_token": "authored-token-1"}))
        context = json.loads(bytes.fromhex(rows[0]["skydio_source_context_hex"][4:]).decode())
        self.assertEqual(sorted(context), ["data", "envelope", "flight", "flight_telemetry"])
        self.assertEqual(context["envelope"]["status_code"], 200)
        self.assertEqual(context["envelope"]["meta"], {"time": 0.031})
        self.assertEqual(context["envelope"]["pagination_token"], "authored-token-1")
        self.assertEqual(report["context_retained_keys"]["envelope"],
                         ["error_message", "meta", "pagination_token", "skydio_error_code",
                          "status_code"])
        self.assertEqual(report["undeclared_context_keys"], {"envelope": ["pagination_token"]})
        raw = json.loads(body())
        raw["data"]["telemetry_revision"] = 7
        raw["data"]["flight_telemetry"]["sample_rate_note"] = "authored-note"
        rows, report, _ = self.rows(json.dumps(raw))
        context = json.loads(bytes.fromhex(rows[0]["skydio_source_context_hex"][4:]).decode())
        self.assertEqual(context["data"], {"telemetry_revision": 7})
        self.assertEqual(context["flight_telemetry"], {"sample_rate_note": "authored-note"})
        self.assertNotIn("aligned_telemetry", context["flight_telemetry"])
        self.assertEqual(report["undeclared_context_keys"],
                         {"data": ["telemetry_revision"],
                          "flight_telemetry": ["sample_rate_note"]})
        precise = "37.42190000000000012345678901"
        rows, _, _ = self.rows(lexeme_body(precise, takeoff_latitude=PLACEHOLDER))
        self.assertIn(f'"takeoff_latitude":{precise}',
                      bytes.fromhex(rows[0]["skydio_source_context_hex"][4:]).decode())
        self.assertIn("NOTHING_NESTED_IS_SCANNED", report["retention"])
        self.assertIn("NO_BROAD_PRIVACY_SANITISATION", report["retention"])
        self.assertEqual(report["envelope"]["meta_json"], '{"time":0.031}')

    def test_timestamp_is_exact_integer_arithmetic_with_a_declared_precision_policy(self):
        """No float second, and a fraction is either represented exactly or refused by name."""
        self.assertEqual(module.sample_time_us("1970-01-01T00:00:00.000001+00:00"), 1)
        self.assertEqual(module.sample_time_us("1970-01-01T00:00:00Z"), 0)
        for stamp in ("2022-05-03T03:10:52.503+00:00", "2022-05-03T03:10:52.503Z",
                      "2022-05-03T05:10:52.503+02:00", "2022-05-03T00:10:52.503-03:00"):
            with self.subTest(stamp=stamp):
                self.assertEqual(module.sample_time_us(stamp), 1651547452503000)
        for stamp, expected in (("2022-05-03T03:10:52.5+00:00", 1651547452500000),
                                ("2022-05-03T03:10:52.50+00:00", 1651547452500000),
                                ("2022-05-03T03:10:52.503000+00:00", 1651547452503000),
                                ("2022-05-03T03:10:52.000003+00:00", 1651547452000003)):
            with self.subTest(stamp=stamp):
                self.assertEqual(module.sample_time_us(stamp), expected)
        self.assertEqual(module.sample_time_us("2022-05-03T03:10:52.5030000+00:00"),
                         1651547452503000)
        for stamp in ("2022-05-03T03:10:52.5031234567+00:00",
                      "2022-05-03T03:10:52.503000123+00:00"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError) as caught:
                module.sample_time_us(stamp)
            self.assertIn("refused rather than truncated", str(caught.exception))
        for stamp in ("2022-02-30T00:00:00Z", "2022-05-03T25:00:00Z", "2022-05-03T03:60:00Z",
                      "1969-12-31T23:59:59Z"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                module.sample_time_us(stamp)

    def test_important_refusals_are_named_not_repaired(self):
        inner = {"flight": {"flight_id": "a", "vehicle_serial": "b"},
                 "flight_telemetry": {"aligned_telemetry": []}}
        for label, text, kwargs in (
                ("wrong declared format", body(), {"source_format": "skydio-cloud-v9"}),
                ("not an object", "[]", {"source_format": FORMAT}),
                ("no data envelope", json.dumps(inner), {"source_format": FORMAT}),
                ("no flight_telemetry", json.dumps({"data": {"flight": {}}}),
                 {"source_format": FORMAT}),
                ("empty sample array", json.dumps({"data": inner}), {"source_format": FORMAT}),
                ("duplicate JSON key", '{"data": {}, "data": {}}', {"source_format": FORMAT}),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                module.convert(text, **kwargs)
        for change in ({"error_message": "flight not found"}, {"status_code": 404}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.convert(body(envelope_changes=change), source_format=FORMAT)
        with self.assertRaises(ValueError):
            module.convert(json.dumps({"data": {"flight_telemetry": {
                "aligned_telemetry": [sample()]}}}), source_format=FORMAT)
        for stamp in (None, "", "2022-05-03 03:10:52", "2022-05-03T03:10:52.503"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                module.convert(body([sample(stamp=stamp)]), source_format=FORMAT)
        with self.assertRaises(ValueError):
            module.convert(body([sample(site_position_enu=[1.0, 2.0])]), source_format=FORMAT)
        with self.assertRaises(ValueError):
            module.convert(body([sample(gps_altitude="31.25")]), source_format=FORMAT)
        with self.assertRaises(ValueError):
            module.convert(body(has_telemetry=False), source_format=FORMAT)
        with self.assertRaises(ValueError):
            module.convert(body(vehicle_serial=""), source_format=FORMAT)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_two_samples_reach_the_actual_common_reader(self):
        second = sample(stamp="2022-05-03T03:10:53.003+00:00", gps_altitude=32.0,
                        battery_percentage=0.87)
        third = sample(stamp="2022-05-03T05:10:54.000001+02:00", gps_altitude=33.0)
        _, _, converted = self.rows(body([sample(), second, third]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.csv"
            path.write_text(converted)
            common = json.loads(subprocess.run(
                [READER, str(PROFILE), str(path)], capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        self.assertEqual(common["observations"][2]["fields"]["record_time_us"], 1651547454000001)
        self.assertEqual(common["observations"][2]["t_ms"], 1651547454000)
        self.assertEqual(common["observations"][0]["fields"]["record_time_us"], 1651547452503000)
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        fields = common["observations"][1]["fields"]
        self.assertEqual(fields["skydio_gps_altitude_m"], 32.0)
        self.assertEqual(fields["skydio_sample_index"], 1)
        self.assertEqual(fields["skydio_battery_remaining_fraction"], 0.87)
        retained = json.loads(bytes.fromhex(
            common["observations"][2]["fields"]["source_record_hex"][4:]).decode())
        self.assertEqual(retained["site_orientation_quat_enu"][2], 0.7071067811865476)
        self.assertEqual(fields["skydio_declared_uninterpreted_key_count"], 2)
        self.assertIsNone(fields["skydio_source_context_hex"])
        units = common["profile_units"]
        self.assertIn("above_sea_level", units["skydio_gps_altitude_m"])
        self.assertIn("names_NO_datum", units["skydio_gps_latitude_deg"])
        self.assertIn("float_from_0_to_1", units["skydio_battery_remaining_fraction"])
        self.assertIn("north_east_down", units["skydio_gps_velocity_ned_m_s_north"])
        self.assertIn("delivery_source", units["skydio_producer_basis"])

    def test_command_line_writes_the_conversion_and_its_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "telemetry.json"
            source.write_text(body(), encoding="utf-8")
            target = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                            "--source-format", FORMAT], check=True, capture_output=True)
            report = json.loads((target / "report.json").read_text())
            self.assertEqual(report["samples"], 1)
            self.assertEqual(report["clock"], "Unknown")
            self.assertIn("NOT_AUTHENTICATION", report["source_format_basis"])
            self.assertNotIn("operator@example.invalid", (target / "observations.csv").read_text())

    def test_command_line_refuses_an_oversized_file_by_its_size_on_disk(self):
        """The bound is checked with stat, so an oversized file is never loaded."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "telemetry.json"
            with source.open("wb") as handle:
                handle.write(b" " * (module.MAX_INPUT + 1))
            self.assertGreater(source.stat().st_size, module.MAX_INPUT)
            finished = subprocess.run([os.sys.executable, str(SCRIPT), str(source),
                                       str(Path(directory) / "out"), "--source-format", FORMAT],
                                      capture_output=True, text=True)
            self.assertEqual(finished.returncode, 2)
            self.assertIn("input bound", finished.stderr)
            self.assertFalse((Path(directory) / "out").exists())


if __name__ == "__main__":
    unittest.main()
