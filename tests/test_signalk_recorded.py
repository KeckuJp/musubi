"""Finite code/spec fixtures, not captured vessel data."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_signalk_recorded import convert

ROOT = Path(__file__).resolve().parents[1]


def delta(context, values, timestamp="2025-01-01T01:02:03.123456789Z"):
    return json.dumps({"context": context, "updates": [{"timestamp": timestamp,
        "$source": "sensor.reported", "values": [{"path": path, "value": value} for path, value in values]}]})


class SignalKTests(unittest.TestCase):
    def test_wind_reference_quantities_reuse_without_computing_corrections(self):
        selected = [("angleApparent", "wind_apparent_angle_port_negative_rad", -.25),
            ("angleTrueGround", "wind_true_ground_angle_port_negative_rad", -.5),
            ("angleTrueWater", "wind_true_water_angle_port_negative_rad", -.75),
            ("directionTrue", "wind_direction_true_north_rad", 1.),
            ("directionMagnetic", "wind_direction_magnetic_north_rad", 1.25),
            ("speedTrue", "wind_true_over_water_speed_m_s", 2.),
            ("speedOverGround", "wind_true_over_ground_speed_m_s", 3.),
            ("speedApparent", "wind_apparent_speed_m_s", 4.)]
        for context, scale in (("vessels.wind-a", 1), ("vessels.wind-b", 2)):
            values = [("environment.wind." + path, value * scale) for path, _, value in selected]
            values += [("environment.wind.speedApparent", None), ("environment.wind.directionChangeAlarm", .5)]
            source = delta(context, values)
            output, report = convert(source, context)
            self.assertEqual((report["source_values"], report["output_records"], report["unsupported_values"]), (10, 9, 1))
            self.assertEqual(bytes.fromhex(report["retained_records"][0][4:]).decode(), source)
            rows = list(csv.DictReader(io.StringIO(output)))
            for row, (_, field, value) in zip(rows, selected):
                self.assertEqual(float(row[field]), value * scale)
            self.assertEqual(rows[-1]["signalk_value_status"], "NOT_PROVIDED")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "wind.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 9)
                    for observation, (_, field, value) in zip(common["observations"], selected):
                        self.assertEqual(observation["fields"][field], value * scale)
                    self.assertEqual(common["platform_domain"], "Unknown")
        for path, _, _ in selected:
            for bad in (True, "2", float("inf")):
                with self.assertRaises(ValueError):
                    convert(delta("vessels.wind", [("environment.wind." + path, bad)]), "vessels.wind")

    def test_battery_id_quantities_sign_and_null_reuse(self):
        selected = [("voltage", "battery_voltage_v", 12.5), ("current", "battery_current_a", -2),
            ("temperature", "battery_temperature_k", 290), ("capacity.nominal", "battery_nominal_j", 10000),
            ("capacity.actual", "battery_actual_j", 9000), ("capacity.remaining", "battery_remaining_j", 4000),
            ("capacity.stateOfCharge", "battery_soc_reported_ratio", .5), ("capacity.stateOfHealth", "battery_soh_reported_ratio", .9),
            ("capacity.dischargeSinceFull", "battery_discharge_since_full_c", 200),
            ("capacity.timeRemaining", "battery_time_remaining_s", 1200),
            ("lifetimeDischarge", "battery_lifetime_discharge_c", 1000), ("lifetimeRecharge", "battery_lifetime_recharge_c", 1500)]
        for battery, multiplier in (("House1", 1), ("Start2", 2)):
            prefix = "electrical.batteries." + battery + "."
            values = [(prefix + path, value * multiplier) for path, _, value in selected]
            values.extend([(prefix + "current", None), (prefix + "future", "retained")])
            source = delta("vessels.battery", values)
            output, report = convert(source, "vessels.battery")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_values"], report["output_records"], report["unsupported_values"]), (14, 13, 1))
            for row, (_, field, value) in zip(rows, selected):
                self.assertEqual(float(row[field]), value * multiplier)
                self.assertEqual(bytes.fromhex(row["battery_id_hex"][4:]).decode(), battery)
            self.assertEqual(rows[1]["battery_current_sign_basis"], "POSITIVE_OUT_OF_DEVICE")
            self.assertEqual(rows[-1]["signalk_value_status"], "NOT_PROVIDED")
            self.assertEqual(bytes.fromhex(report["retained_records"][0][4:]).decode(), source)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "battery.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 13)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(common["observations"][1]["fields"]["battery_current_a"], -2 * multiplier)
        for key, value in (("voltage", -1), ("temperature", "290"), ("current", True), ("capacity.remaining", -1), ("current", 2**63)):
            with self.assertRaises(ValueError):
                convert(delta("vessels.battery", [("electrical.batteries.A." + key, value)]), "vessels.battery")

    def test_outside_quantities_share_path_without_clipping_or_merging(self):
        selected = [
            ("temperature", "outside_temperature_k", 280.25),
            ("dewPointTemperature", "outside_dew_point_temperature_k", 278.5),
            ("apparentWindChillTemperature", "outside_apparent_wind_chill_temperature_k", 275.0),
            ("theoreticalWindChillTemperature", "outside_theoretical_wind_chill_temperature_k", 274.0),
            ("heatIndexTemperature", "outside_heat_index_temperature_k", 285.0),
            ("pressure", "outside_pressure_pa", 100123.5),
            ("relativeHumidity", "outside_relative_humidity_ratio", 1.01),
            ("airDensity", "outside_air_density_kg_m3", 1.225),
            ("illuminance", "outside_illuminance_lux", 123.75),
        ]
        for context in ("vessels.weather-a", "vessels.weather-b"):
            source = delta(context, [("environment.outside."+path, value) for path, _, value in selected])
            output, report = convert(source, context)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_values"], report["output_records"], report["unsupported_values"]), (9,9,0))
            for row, (_, field, value) in zip(rows, selected):
                self.assertEqual(float(row[field]), value)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True,capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], 9)
                    self.assertEqual(result["observations"][6]["fields"]["outside_relative_humidity_ratio"], 1.01)
        for path, _, _ in selected:
            key = "environment.outside." + path
            for invalid in (-1, True, "1"):
                with self.assertRaises(ValueError):
                    convert(delta("vessels.weather-a",[(key,invalid)]),"vessels.weather-a")
            output, _ = convert(delta("vessels.weather-a",[(key,None)]),"vessels.weather-a")
            self.assertIn("NOT_PROVIDED",output)

    def test_two_contexts_reuse_units_null_unknown_and_common_observations(self):
        for index, (context, values) in enumerate([
            ("vessels.example-a", [("navigation.speedThroughWater", 2.5),
                ("environment.depth.belowTransducer", 7.125), ("future.quantity", {"raw": True})]),
            ("vessels.example-b", [("environment.water.temperature", 290.25),
                ("navigation.speedThroughWater", None)]),
        ]):
            source = delta(context, values)
            output, report = convert(source, context)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(report["source_values"], report["output_records"] + report["unsupported_values"])
            self.assertEqual(bytes.fromhex(report["retained_records"][0][4:]).decode(), source)
            self.assertEqual(rows[0]["record_time_us"], rows[1]["record_time_us"])
            if index == 0:
                self.assertEqual(rows[1]["depth_below_transducer_m"], "7.125")
                self.assertEqual(report["unsupported_values"], 1)
            else:
                self.assertEqual(rows[0]["water_temperature_k"], "290.25")
                self.assertEqual(rows[1]["signalk_value_status"], "NOT_PROVIDED")
                self.assertEqual(rows[1]["speed_through_water_m_s"], "")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "values.csv"; path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], 2)
                    self.assertEqual(result["observations"][0]["fields"]["signalk_value_status"], "REPORTED")
                    key, value = ("speed_through_water_m_s", 2.5) if index == 0 else ("water_temperature_k", 290.25)
                    self.assertEqual(result["observations"][0]["fields"][key], value)

    def test_saved_server_log_container_unwraps_to_the_same_delta_path(self):
        context = "vessels.example-a"
        selected = delta(context, [("navigation.speedThroughWater", 2.5),
                                   ("environment.depth.belowTransducer", 7.125)])
        foreign = delta("vessels.other-boat", [("navigation.speedThroughWater", 9.)])
        source = "\n".join(["1735693323500;n2k-1;" + selected,
                            "1735693323600;nmea0183-serial;$GPGGA,010203.00,0000.000,N,00000.000,E,1,04,1.0,0.0,M,0.0,M,,*61",
                            "1735693323700;n2k-1;" + foreign,
                            "1735693323800;;[object Object]"])
        output, report = convert(source, context, server_log=True)
        self.assertEqual((report["source_records"], report["output_records"]), (4, 2))
        self.assertEqual((report["unselected_context_records"], report["provider_text_records"],
                          report["opaque_object_records"]), (1, 2, 1))
        self.assertEqual(report["source_records"] - report["unselected_context_records"]
                         - report["provider_text_records"], 1)
        self.assertEqual([bytes.fromhex(record[4:]).decode() for record in report["retained_records"]],
                         source.splitlines())
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["speed_through_water_m_s"] for row in rows], ["2.5", ""])
        self.assertEqual(rows[1]["depth_below_transducer_m"], "7.125")
        for row in rows:
            self.assertEqual(row["signalk_log_time_us"], "1735693323500000")
            self.assertNotEqual(row["record_time_us"], row["signalk_log_time_us"])
            self.assertEqual(row["record_time_us"], "1735693323123456")
            self.assertEqual(bytes.fromhex(row["signalk_log_discriminator_hex"][4:]).decode(), "n2k-1")
            self.assertIn("NOT_THE_OBSERVATION_TIME", row["signalk_log_time_basis"])
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("MUSUBI_TELEMETRY_READER is not configured; common proof NOT RUN")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server-log.csv"; path.write_text(output)
            common = json.loads(subprocess.run([reader,
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["observations"][0]["fields"]["speed_through_water_m_s"], 2.5)
        self.assertEqual(common["observations"][1]["fields"]["depth_below_transducer_m"], 7.125)
        for observation in common["observations"]:
            self.assertEqual(observation["fields"]["signalk_log_time_us"], 1735693323500000)
            self.assertIn("NOT_AUTHENTICATED", observation["fields"]["signalk_log_time_basis"])

    def test_server_log_container_refuses_guessing_and_leaves_bare_deltas_unchanged(self):
        context = "vessels.example-a"
        payload = delta(context, [("navigation.speedThroughWater", 2.5)])
        line = "1735693323500;n2k-1;" + payload
        self.assertEqual(convert(payload, context)[0], convert(payload, context, server_log=False)[0])
        self.assertNotIn("signalk_log_time_us", convert(payload, context)[0])
        for bad in [payload,                                   # container shape missing entirely
                    "1735693323500;n2k-1",                     # no payload separator
                    ";n2k-1;" + payload,                       # no host write time
                    "17356933235000;n2k-1;" + payload[:40],    # truncated JSON payload
                    "1735693323500;n2k-1;" + payload.replace('"updates"', '"nested"'),
                    "1735693323500;n2k-1;" + json.dumps({"context": context, "updates": {"0": {}}}),
                    "1735693323500000000;n2k-1;" + payload,    # host time beyond the accepted width
                    "-1735693323500;n2k-1;" + payload,
                    line + "\n" + "continuation without its own container prefix",
                    line.replace("1735693323500", "1735693323500 ")]:
            with self.subTest(bad=bad[:60]), self.assertRaises((ValueError, KeyError, TypeError)):
                convert(bad, context, server_log=True)
        with self.assertRaises(ValueError):
            convert(line, context, server_log=1)
        with self.assertRaises(ValueError):
            convert("1735693323500;n2k-1;" + delta("vessels.other-boat", [("navigation.speedThroughWater", 1.)]),
                    context, server_log=True)

    def test_invalid_selected_meaning_time_and_structure_are_rejected(self):
        context = "vessels.example-a"
        valid = delta(context, [("navigation.speedThroughWater", 1.)])
        for source in ["", "[]", "{}", valid.replace(context, "vessels.other"),
                       valid.replace("123456789Z", "123+09:00"),
                       delta(context, [("navigation.speedThroughWater", True)]),
                       delta(context, [("navigation.speedThroughWater", "1.0")]),
                       delta(context, [("environment.water.temperature", -1.)]),
                       delta(context, [("navigation.speedThroughWater", float("nan"))]),
                       valid + "\n" + delta(context, [("navigation.speedThroughWater", 1.)], "2020-01-01T00:00:00Z"),
                       valid.replace('"context":', '"context":"vessels.other","context":')]:
            with self.subTest(source=source), self.assertRaises((ValueError, KeyError, TypeError)):
                convert(source, context)


if __name__ == "__main__":
    unittest.main()
