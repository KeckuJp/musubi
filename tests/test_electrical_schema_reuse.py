"""Selected schemas: synthetic contract checks, not real-device evidence."""
import csv
import importlib.util
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("battery", ROOT / "scripts/convert_ardupilot_battery_csv.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def inav_common_reader(case, label):
    """The configured common reader, or an explicit NOT_RUN — never a silent pass.

    `MUSUBI_TELEMETRY_READER` is a caller instruction: pointing it at something that does not
    exist is a **failure**, not a reason to skip, so a wrong path can never look like a run that
    passed. With nothing configured the caller gets an explicit NOT_RUN line and this test claims
    no common-output proof at all. Mandatory CI supplies the executable.
    """
    declared = os.environ.get("MUSUBI_TELEMETRY_READER")
    if not declared:
        print(f"{label} common-reader integration NOT_RUN: executable not configured")
        return None
    if not Path(declared).exists():
        case.fail(f"MUSUBI_TELEMETRY_READER is set to {declared!r}, which does not exist")
    return declared


def vda(version):
    old = version in ("1.1.0", "2.0.0", "2.1.0")
    return {"timestamp": "2026-01-01T00:00:00.123456789Z", "version": version,
            "manufacturer": "synthetic", "serialNumber": "example-1",
            "batteryState" if old else "powerSupply": {
                "batteryCharge" if old else "stateOfCharge": 75,
                "batteryVoltage": 24, "charging": False}, "future": {"field": [1, 2]}}


def ros():
    return {"header": {"stamp": {"sec": 2, "nanosec": 123456789}, "frame_id": "battery"},
            "voltage": 24, "current": -2, "charge": 3, "percentage": .75,
            "power_supply_status": 2, "present": True, "future": ["new"]}


def connection(version, state, *, header=1, stamp=None, **extra):
    """One authored VDA5050 connection payload, as the pinned schema requires it."""
    record = {"headerId": header, "timestamp": stamp or "2026-01-01T00:00:00.12Z", "version": version,
              "manufacturer": "authored-maker", "serialNumber": "authored-serial-1",
              "connectionState": state}
    record.update(extra)
    return record


class ElectricalReuseTests(unittest.TestCase):
    def test_saved_gutma_battery_percent_reuses_common_fraction(self):
        import hashlib
        fmt = module.GUTMA_FORMAT

        def document(keys, items, file_over=None, logging_over=None, message_over=None):
            logging = dict(uom_system="Metric", logging_start_dtg="2026-03-01T09:15:00+00:00",
                           altitude_system="WGS84", events=[dict(event_timestamp="1.5", event_info="TOF")],
                           flight_logging_keys=keys, flight_logging_items=items)
            logging.update(logging_over or {})
            declared = dict(version="1.0.0", parrot_version="1.0.3", logging_type="GUTMA_DX_JSON",
                            filename="log-synthetic.gutma", creation_dtg="2026-03-01T10:15:00+01:00")
            declared.update(file_over or {})
            message = dict(message_type="flight_logging_submission", file=declared,
                           flight_data=dict(flight_id="synthetic"), flight_logging=logging)
            message.update(message_over or {})
            return dict(exchange=dict(exchange_type="flight_logging", message=message))

        first_keys = ["timestamp", "battery_percent", "battery_voltage", "battery_current", "gps_lat", "future_column"]
        first_items = [[0, 0, 111, 11, 0, 1000], [0.5, 75, 222, 22, 1, 1001], [20, 25, 333, 33, 2, 1002]]
        second_keys = ["gps_lon", "battery_cell_voltage_0", "battery_cell_voltage_1", "battery_cell_voltage_2",
                       "battery_capacity", "timestamp", "wifi_signal", "battery_percent"]
        second_items = [[0, 41, 42, 43, 5000, 0, -1, 80], [1, 51, 52, 53, 4000, 8, -2, 60],
                        [2, 61, 62, 63, 3000, 8, -3, 30]]
        for keys, items, fractions, times in (
                (first_keys, first_items, [0.0, .75, .25], [0, 500000, 20000000]),
                (second_keys, second_items, [.8, .6, .3], [0, 8000000, 8000000])):
            text = json.dumps(document(keys, items))
            rows = list(csv.DictReader(io.StringIO(module.convert(text, fmt))))
            self.assertEqual(len(rows), 3)
            self.assertEqual([float(row["battery_remaining_fraction"]) for row in rows], fractions)
            self.assertEqual([row["gutma_item_index"] for row in rows], ["0", "1", "2"])
            self.assertEqual([int(row["record_time_us"]) for row in rows], times)
            self.assertTrue(all(row["source_document_sha256"] ==
                                "sha256:" + hashlib.sha256(text.encode()).hexdigest() for row in rows))
            self.assertEqual(bytes.fromhex(rows[0]["gutma_logging_start_reported_hex"][4:]).decode(),
                             "2026-03-01T09:15:00+00:00")
            retained = dict(zip(json.loads(bytes.fromhex(rows[2]["source_keys_hex"][4:])),
                                json.loads(bytes.fromhex(rows[2]["source_record_hex"][4:]))))
            self.assertEqual(retained, dict(zip(keys, items[2])))
            for absent in ("battery_voltage_v", "battery_current_a", "battery_cell_voltage_v",
                           "battery_full_charge_ah", "battery_voltage", "gps_lat"):
                self.assertNotIn(absent, rows[0])
        self.assertEqual(next(csv.DictReader(io.StringIO(module.convert(
            json.dumps(document(first_keys, first_items)), fmt))))["gutma_battery_percent_disposition"],
            "ZERO_REPORTED_MAY_BE_UNSET")
        for keys, items, expected in ((first_keys, first_items, .25), (second_keys, second_items, .3)):
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "flight.gutma", Path(directory) / "out.csv"
                index = keys.index("timestamp")
                many = [[value if position != index else round(step * .2, 3) for position, value in enumerate(items[step % 3])]
                        for step in range(10000)]
                source.write_text(json.dumps(document(keys, many)))
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
                converted = list(csv.DictReader(io.StringIO(target.read_text())))
                self.assertEqual(len(converted), 10000)  # a practical flight at 5 Hz
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 10000)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
                    self.assertEqual(common["observations"][2]["fields"]["battery_remaining_fraction"], expected)
                    self.assertEqual(common["observations"][2]["fields"]["record_time_us"], 400000)
                else:
                    print("GUTMA saved flight log common-reader integration NOT_RUN: executable not configured")
        for label, bad in (
                ("unit system", document(first_keys, first_items, logging_over={"uom_system": "Imperial"})),
                ("file version", document(first_keys, first_items, file_over={"version": "1.0.1"})),
                ("writer version", document(first_keys, first_items, file_over={"parrot_version": "1.0.4"})),
                ("logging type", document(first_keys, first_items, file_over={"logging_type": "OTHER_JSON"})),
                ("message type", document(first_keys, first_items, message_over={"message_type": "other"})),
                ("missing file", document(first_keys, first_items, message_over={"file": {}})),
                ("no percent column", document(["timestamp", "gps_lat"], [[0, 0]])),
                ("no timestamp column", document(["battery_percent", "gps_lat"], [[50, 0]])),
                ("duplicate column", document(["timestamp", "battery_percent", "battery_percent"], [[0, 50, 50]])),
                ("untrimmed column", document(["timestamp", " battery_percent"], [[0, 50]])),
                ("ragged item", document(first_keys, [first_items[0][:-1]])),
                ("text value", document(first_keys, [[0, "50", 111, 11, 0, 1000]])),
                ("boolean value", document(first_keys, [[0, True, 111, 11, 0, 1000]])),
                ("percent above range", document(first_keys, [[0, 100.5, 111, 11, 0, 1000]])),
                ("percent below range", document(first_keys, [[0, -1, 111, 11, 0, 1000]])),
                ("negative clock", document(first_keys, [[-1, 50, 111, 11, 0, 1000]])),
                ("decreasing clock", document(first_keys, [first_items[2], first_items[1]])),
                ("clock overflow", document(first_keys, [[1e15, 50, 111, 11, 0, 1000]])),
                ("empty items", document(first_keys, [])),
                ("missing start", document(first_keys, first_items, logging_over={"logging_start_dtg": ""}))):
            with self.subTest(bad=label), self.assertRaises(ValueError):
                module.convert(json.dumps(bad), fmt)
        valid = json.dumps(document(first_keys, first_items))
        for label, text in (("duplicate JSON field", valid.replace('"uom_system": "Metric"',
                                                                  '"uom_system": "Metric", "uom_system": "Metric"')),
                            ("truncated", valid[:-1]), ("array document", "[]"), ("empty", ""),
                            ("non-JSON number", valid.replace("111", "NaN"))):
            with self.subTest(bad=label), self.assertRaises(ValueError):
                module.convert(text, fmt)
        for options in ({"capture_time_us": 1}, {"bat_details": True}, {"vda_actions": True},
                        {"allow_missing_voltage": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                module.convert(valid, fmt, **options)
        event = dict(arsdk_xml_commit="2299c689be5951fa2104793e0154b0b5b1896c03", event="voltage",
                     payload={"voltage": 12500}, capture_time_us=777, future=[1])
        unchanged = next(csv.DictReader(io.StringIO(module.convert(json.dumps(event), module.PARROT_FORMAT))))
        self.assertEqual(float(unchanged["battery_voltage_v"]), 12.5)
        self.assertEqual(unchanged["parrot_event_reported"], "voltage")
        self.assertNotIn("gutma_value_basis", unchanged)
        with self.assertRaises(ValueError):
            module.convert(valid, module.PARROT_FORMAT)
        with self.assertRaises(ValueError):
            module.convert(json.dumps(event), fmt)

    def test_inav_saved_logging_csv_reuses_electrical_and_attitude(self):
        import math
        fmt = module.INAV_FORMAT
        first = ("timestamp,kinematicsX,kinematicsY,kinematicsZ,altitude,voltage,amperage,mAhdrawn,rssi,RC0,RC1\n"
                 "1700000000123,12.3,-4.5,359,15.25,12.3,-2.5,1234,512,1500,1000\n"
                 "1700000000223,0,0,0,0,25.5,327.67,65535,1023,1000,2000\n")
        second = ("timestamp,gyroscopeX,gyroscopeY,gyroscopeZ,accelerometerX,accelerometerY,accelerometerZ,"
                  "magnetometerX,magnetometerY,magnetometerZ,kinematicsX,kinematicsY,kinematicsZ,"
                  "gpsFix,gpsNumSat,gpsLat,gpsLon,gpsAlt,gpsSpeed,gpsGroundCourse,Motor0,Motor1,Debug0\n"
                  "1699999999000,-1,2,3,0.02,-0.04,1.01,120,-33,7,-180,90.5,-90,1,9,35.6812,139.7671,12,0,270,1000,1050,7\n"
                  "1699999999000,1,2,3,0.02,-0.04,1.01,120,-33,7,3276.7,-3276.8,32767,1,9,35.6812,139.7671,12,0,270,1050,1000,8\n")
        rows = list(csv.DictReader(io.StringIO(module.convert(first, fmt))))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["record_time_us"] for r in rows], ["1700000000123000", "1700000000223000"])
        self.assertEqual([float(r["battery_voltage_v"]) for r in rows], [12.3, 25.5])
        self.assertEqual([float(r["battery_current_a"]) for r in rows], [-2.5, 327.67])
        self.assertEqual([float(r["battery_consumed_ah"]) for r in rows], [1.234, 65.535])
        self.assertAlmostEqual(float(rows[0]["battery_power_w"]), -30.75)
        self.assertEqual(rows[0]["battery_current_sign_basis"], "UNSPECIFIED")
        self.assertEqual(rows[0]["inav_rssi_code_reported"], "512")
        self.assertEqual(rows[0]["inav_rssi_basis"], "REPORTED_CODE_DECLARED_0_1023_NOT_PERCENT_OR_DBM")
        self.assertAlmostEqual(float(rows[0]["reported_roll_rad"]), math.radians(12.3))
        self.assertAlmostEqual(float(rows[0]["reported_pitch_rad"]), math.radians(-4.5))
        self.assertAlmostEqual(float(rows[0]["reported_yaw_rad"]), math.radians(359))
        columns = bytes.fromhex(rows[0]["source_header_hex"][4:]).decode().split(",")
        retained = dict(zip(columns, bytes.fromhex(rows[0]["source_record_hex"][4:]).decode().split(",")))
        self.assertEqual([retained["altitude"], retained["RC0"], retained["RC1"]], ["15.25", "1500", "1000"])
        self.assertNotIn("altitude", rows[0])
        self.assertEqual([rows[0]["inav_gps_latitude_deg_reported"], rows[0]["inav_gps_angle_basis"],
                          rows[0]["inav_gps_unscaled_basis"]], ["", "", ""])
        attitude = list(csv.DictReader(io.StringIO(module.convert(second, fmt))))
        self.assertEqual(len(attitude), 2)
        self.assertAlmostEqual(float(attitude[0]["reported_roll_rad"]), -math.pi)
        self.assertAlmostEqual(float(attitude[0]["reported_yaw_rad"]), -math.pi / 2)
        self.assertAlmostEqual(float(attitude[1]["reported_pitch_rad"]), math.radians(-3276.8))
        self.assertTrue(all(attitude[0][field] == "" for field in module.INAV_FIELDS[:7]))
        self.assertEqual(bytes.fromhex(attitude[1]["source_header_hex"][4:]).decode(), second.splitlines()[0])
        self.assertIn("gpsLat", bytes.fromhex(attitude[0]["source_header_hex"][4:]).decode())
        self.assertEqual(float(attitude[0]["inav_gps_latitude_deg_reported"]), 35.6812)
        self.assertEqual(float(attitude[0]["inav_gps_longitude_deg_reported"]), 139.7671)
        self.assertEqual(attitude[0]["inav_gps_coordinate_disposition"], "REPORTED_WITHIN_ANGLE_DOMAIN")
        self.assertEqual(attitude[0]["inav_gps_fix_code_reported"], "1")
        self.assertEqual(attitude[0]["inav_gps_satellites_reported"], "9")
        self.assertEqual([attitude[0]["inav_gps_altitude_code_reported"],
                          attitude[0]["inav_gps_speed_code_reported"],
                          attitude[0]["inav_gps_ground_course_code_reported"]], ["12", "0", "270"])
        self.assertEqual(attitude[0]["inav_gps_angle_basis"], module.INAV_ANGLE_BASIS)
        self.assertIn("NO_UNIT_APPLIED_HERE", attitude[0]["inav_gps_unscaled_basis"])
        self.assertIn("UNADOPTED", attitude[0]["inav_gps_unscaled_basis"])

        navigation = ("timestamp,gpsFix,gpsNumSat,gpsLat,gpsLon,gpsAlt,gpsSpeed,gpsGroundCourse\n"
                      "1700000000500,3,14,-33.8688,151.2093,-25,1200,3599\n"
                      "1700000000600,0,0,0,0,0,0,0\n")
        nav = list(csv.DictReader(io.StringIO(module.convert(navigation, fmt))))
        self.assertEqual(len(nav), 2)
        self.assertEqual(float(nav[0]["inav_gps_latitude_deg_reported"]), -33.8688)
        self.assertEqual(nav[0]["inav_gps_altitude_code_reported"], "-25")
        self.assertEqual(nav[1]["inav_gps_fix_code_reported"], "0")
        self.assertEqual(float(nav[1]["inav_gps_latitude_deg_reported"]), 0.0)
        self.assertIn("ZERO_MAY_BE_UNSET", nav[1]["inav_value_basis"])
        for cell, bad in (("gpsLat", "95.0"), ("gpsLon", "-181.0")):
            columns = navigation.splitlines()[0].split(",")
            cells = navigation.splitlines()[1].split(",")
            cells[columns.index(cell)] = bad
            row = list(csv.DictReader(io.StringIO(module.convert(
                navigation.splitlines()[0] + "\n" + ",".join(cells) + "\n", fmt))))[0]
            self.assertEqual(row["inav_gps_coordinate_disposition"], "OUTSIDE_ANGLE_DOMAIN_WITHHELD")
            self.assertEqual(row["inav_gps_latitude_deg_reported"], "")
            self.assertEqual(row["inav_gps_longitude_deg_reported"], "")
            self.assertEqual(row["inav_gps_fix_code_reported"], "3")
        for cell, bad in (("gpsAlt", "40000"), ("gpsAlt", "-32769"), ("gpsAlt", "1.5"),
                          ("gpsSpeed", "65536"), ("gpsSpeed", "-1"), ("gpsGroundCourse", "65536"),
                          ("gpsFix", "256"), ("gpsNumSat", "-1"), ("gpsLon", "x"),
                          ("gpsLat", "1.12345678")):  # more fraction digits than 1e-7 can produce
            columns = navigation.splitlines()[0].split(",")
            cells = navigation.splitlines()[1].split(",")
            cells[columns.index(cell)] = bad
            with self.subTest(cell=cell, bad=bad), self.assertRaises(ValueError):
                module.convert(navigation.splitlines()[0] + "\n" + ",".join(cells) + "\n", fmt)
        for text, expected in ((first, 12.3), (second, None)):
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "inav_data_log.txt", Path(directory) / "out.csv"
                many = [text.splitlines()[0]] + [",".join([str(1700000000000 + index)]
                    + text.splitlines()[1 + index % 2].split(",")[1:]) for index in range(10000)]
                source.write_text("\n".join(many) + "\n")
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
                converted = list(csv.DictReader(io.StringIO(target.read_text())))
                self.assertEqual(len(converted), 10000)  # practical 100ms poll for ~17 minutes
                reader = inav_common_reader(self, "INAV saved logging capacity")
                if reader:
                    common = json.loads(subprocess.run([reader,
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 10000)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
                    fields = common["observations"][0]["fields"]
                    self.assertEqual(fields.get("battery_voltage_v"), expected)
                    self.assertAlmostEqual(fields["reported_roll_rad"],
                                           math.radians(12.3) if expected else -math.pi)
                    self.assertEqual(fields["record_time_us"], 1700000000000000)
                    if not expected:  # this layout carries MSP_RAW_GPS beside the attitude group
                        self.assertEqual(fields["inav_gps_latitude_deg_reported"], 35.6812)
                        self.assertEqual(fields["inav_gps_longitude_deg_reported"], 139.7671)
                        self.assertEqual(fields["inav_gps_angle_basis"], module.INAV_ANGLE_BASIS)
        reader = inav_common_reader(self, "INAV saved MSP_RAW_GPS navigation")
        if reader:
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "inav_gps_log.txt", Path(directory) / "gps.csv"
                source.write_text(navigation + "1700000000700,3,14,181.5,-33.8688,7,1200,3599\n")
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
                common = json.loads(subprocess.run([reader,
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target)], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
                reported, zero, withheld = (o["fields"] for o in common["observations"])
                self.assertEqual(reported["record_time_us"], 1700000000500000)
                self.assertEqual(reported["inav_gps_latitude_deg_reported"], -33.8688)
                self.assertEqual(reported["inav_gps_longitude_deg_reported"], 151.2093)
                self.assertEqual(reported["inav_gps_coordinate_disposition"], "REPORTED_WITHIN_ANGLE_DOMAIN")
                self.assertEqual(reported["inav_gps_fix_code_reported"], 3)
                self.assertEqual(reported["inav_gps_satellites_reported"], 14)
                self.assertEqual([reported["inav_gps_altitude_code_reported"],
                                  reported["inav_gps_speed_code_reported"],
                                  reported["inav_gps_ground_course_code_reported"]], [-25, 1200, 3599])
                self.assertEqual(reported["inav_gps_angle_basis"], module.INAV_ANGLE_BASIS)
                self.assertIn("NO_DATUM_DECLARED", reported["inav_gps_angle_basis"])
                self.assertEqual(reported["inav_gps_unscaled_basis"], module.INAV_UNSCALED_BASIS)
                self.assertIn("NO_UNIT_APPLIED_HERE", reported["inav_gps_unscaled_basis"])
                declared_unit = common["profile_units"]["inav_gps_latitude_deg_reported"]
                self.assertTrue(declared_unit.startswith("deg_"))
                self.assertIn("displays_that_identical_divided_value_as_deg", declared_unit)
                self.assertIn("no_WGS84_or_world_frame_is_asserted", declared_unit)
                self.assertEqual(zero["inav_gps_fix_code_reported"], 0)
                self.assertEqual(zero["inav_gps_latitude_deg_reported"], 0.0)
                self.assertEqual(zero["inav_gps_coordinate_disposition"], "REPORTED_WITHIN_ANGLE_DOMAIN")
                self.assertIn("ZERO_MAY_BE_UNSET", zero["inav_value_basis"])
                self.assertIn("HOST_SNAPSHOT_OF_ASYNCHRONOUS", zero["inav_value_basis"])
                self.assertEqual(withheld["inav_gps_coordinate_disposition"], "OUTSIDE_ANGLE_DOMAIN_WITHHELD")
                self.assertIsNone(withheld.get("inav_gps_latitude_deg_reported"))
                self.assertIsNone(withheld.get("inav_gps_longitude_deg_reported"))
                self.assertEqual(withheld["inav_gps_satellites_reported"], 14)
                self.assertEqual(bytes.fromhex(withheld["source_record_hex"][4:]).decode(),
                                 "1700000000700,3,14,181.5,-33.8688,7,1200,3599")
                self.assertEqual(bytes.fromhex(withheld["source_header_hex"][4:]).decode(),
                                 navigation.splitlines()[0])
        for header in ("timestamp,voltage,amperage,mAhdrawn",  # incomplete MSP_ANALOG group
                       "timestamp,kinematicsX,kinematicsY",  # incomplete MSP_ATTITUDE group
                       "timestamp,gpsFix,gpsNumSat,gpsLat",  # incomplete MSP_RAW_GPS group
                       "timestamp,altitude,gpsFix",  # no qualified group at all
                       "voltage,amperage,mAhdrawn,rssi",  # no leading timestamp column
                       "timestamp,voltage,amperage,mAhdrawn,rssi,rssi",  # duplicate column
                       "timestamp, voltage,amperage,mAhdrawn,rssi",  # untrimmed name
                       "timestamp"):
            body = ",".join(["1700000000123"] + ["1"] * (len(header.split(",")) - 1))
            with self.subTest(header=header), self.assertRaises(ValueError):
                module.convert(header + "\n" + body + "\n", fmt)
        for bad in ("", "\n", first.splitlines()[0] + "\n",  # header without any record
                    first + first,  # restarted session appended a second header line
                    first + "1700000000323,0,0,0,0,12.0,0,0,0,1000\n",  # ragged row
                    first.replace('12.3,-4.5', '"12.3",-4.5'),  # quoted representation
                    first + "1700000000023,0,0,0,0,12.0,0,0,0,1000,1000\n"):  # decreasing host clock
            with self.subTest(bad=bad[-40:]), self.assertRaises(ValueError):
                module.convert(bad, fmt)
        for column, value in (("timestamp", "-1"), ("timestamp", "1.7e12"), ("timestamp", "1700000000123 "),
                              ("voltage", "655.36"), ("voltage", "-0.1"), ("voltage", "12.345"),
                              ("voltage", "NaN"), ("voltage", ""), ("amperage", "327.68"),
                              ("amperage", "-327.69"), ("mAhdrawn", "-1"), ("mAhdrawn", "1.5"),
                              ("rssi", "65536"), ("kinematicsX", "3276.75"), ("kinematicsY", "-3276.9"),
                              ("kinematicsZ", "0.5"), ("kinematicsZ", "32768")):
            columns = first.splitlines()[0].split(",")
            cells = first.splitlines()[1].split(",")
            cells[columns.index(column)] = value
            with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                module.convert(first.splitlines()[0] + "\n" + ",".join(cells) + "\n", fmt)
        for options in ({"capture_time_us": 1}, {"bat_details": True}, {"vda_actions": True},
                        {"allow_missing_voltage": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                module.convert(first, fmt, **options)
        with self.assertRaises(ValueError):
            module.convert(first, "ardupilot-bat")
        bat = ("TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct\n100,0,12.5,2.0,1000,3600,50\n")
        unchanged = next(csv.DictReader(io.StringIO(module.convert(bat, "ardupilot-bat"))))
        self.assertEqual([unchanged["battery_voltage_v"], unchanged["battery_consumed_ah"],
                          unchanged["battery_remaining_fraction"]], ["12.5", "1.0", "0.5"])
        self.assertNotIn("inav_value_basis", unchanged)

    def test_mir_saved_status_reuses_quantities_without_running_adapter(self):
        import math
        fmt = "mir-saved-status-0c5476d"
        records = [dict(adapter_commit="0c5476d17979a8d65f603f235f7f736e731d2737", api_version="2.0.0",
            position_unit="m", capture_time_us=10+i,
            status=dict(map_id=name, battery_percentage=percentage, state_id=state,
                        position=dict(x=1.25+i, y=-2., orientation=angle), future=["unchanged"]))
            for i, (name, percentage, state, angle) in enumerate((("map-a", 25., 3, 180.), ("map-b", 80., 99, -90.)))]
        rows = list(csv.DictReader(io.StringIO(module.convert("\n".join(map(json.dumps, records)), fmt))))
        self.assertEqual([float(row["battery_remaining_fraction"]) for row in rows], [.25, .8])
        self.assertEqual([row["mir_state_reported"] for row in rows], ["READY", "UNKNOWN_RETAINED"])
        self.assertAlmostEqual(float(rows[0]["mir_map_orientation_rad"]), math.pi)
        self.assertAlmostEqual(float(rows[1]["mir_map_orientation_rad"]), -math.pi / 2)
        self.assertEqual(json.loads(bytes.fromhex(rows[1]["source_record_hex"][4:])), records[1])
        missing = dict(records[0], status={k: v for k, v in records[0]["status"].items() if k != "state_id"})
        self.assertEqual(next(csv.DictReader(io.StringIO(module.convert(json.dumps(missing), fmt))))["mir_state_reported"], "NOT_REPORTED")
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.jsonl", Path(directory) / "out.csv"
            source.write_text("\n".join(json.dumps(dict(records[i % 2], capture_time_us=i)) for i in range(10000)))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(target.read_text())))), 10000)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 10000)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertAlmostEqual(common["observations"][-1]["fields"]["mir_map_orientation_rad"], -math.pi / 2)
                self.assertEqual(common["observations"][-1]["fields"]["mir_state_reported"], "UNKNOWN_RETAINED")
            else:
                print("MiR saved status common-reader integration NOT_RUN: executable not configured")
        for key, value in (("battery_percentage", -1), ("battery_percentage", 101), ("battery_percentage", True),
                           ("map_id", ""), ("state_id", -1), ("state_id", False), ("position", {"x": 1})):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(dict(records[0], status=dict(records[0]["status"], **{key: value}))), fmt)
        for key, value in (("adapter_commit", "other"), ("api_version", "2.13.5.3"), ("position_unit", "mm"), ("capture_time_us", -1)):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(records[0], **{key: value})), fmt)
        for value in (float("nan"), 361, True):
            bad = dict(records[0], status=dict(records[0]["status"], position=dict(x=1., y=2., orientation=value)))
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), fmt)
        with self.assertRaises(ValueError): module.convert(json.dumps(records[0])[:-1], fmt)

    def test_saved_digital_sensor_readings_reuse_clock_and_source(self):
        fmt = "farmbot-digital-reading-15.30.5"
        records = [dict(schema_commit="6db0ea3eb0059424573da8399d082b445624b24c", capture_time_us=100+i,
            reading=dict(id=10+i, pin=2+i, mode=0, value=i, read_at=stamp, x=None,
                         future={"unrecognized": [1, 2]}))
            for i, stamp in enumerate(("2026-01-01T00:00:00.123456Z", "2026-01-01T09:00:00.123456+09:00"))]
        rows = list(csv.DictReader(io.StringIO(module.convert("\n".join(map(json.dumps, records)), fmt))))
        self.assertEqual([r["farm_digital_level_reported"] for r in rows], ["LOW", "HIGH"])
        self.assertEqual(rows[0]["farm_read_at_reported_us"], rows[1]["farm_read_at_reported_us"])
        self.assertEqual(rows[0]["farm_read_at_basis"], "REPORTED_OR_LEGACY_CREATED_TIME_NOT_CAPTURE")
        self.assertEqual(json.loads(bytes.fromhex(rows[1]["source_record_hex"][4:])), records[1])
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.jsonl", Path(directory) / "out.csv"
            source.write_text("\n".join(json.dumps(dict(records[i % 2], capture_time_us=i)) for i in range(10000)))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(target.read_text())))), 10000)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 10000)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["observations"][-1]["fields"]["farm_digital_level_reported"], "HIGH")
            else:
                print("FarmBot digital common-reader integration NOT_RUN: executable not configured")
        for key, value in (("mode", 1), ("mode", True), ("value", 2), ("value", False), ("pin", -1),
                           ("id", 2**63), ("read_at", None), ("read_at", "2026-01-01T00:00:00"),
                           ("read_at", "2026-01-01T00:00:60Z"), ("read_at", "2026-01-01T00:00:00.1234567Z")):
            bad = dict(records[0], reading=dict(records[0]["reading"], **{key: value}))
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), fmt)
        for bad in (dict(records[0], schema_commit="other"), dict(records[0], capture_time_us=-1)):
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), fmt)
        with self.assertRaises(ValueError): module.convert(json.dumps(records[0])[:-1], fmt)

    def test_saved_rmf_socketio_events_reuse_fleet_meaning(self):
        fmt = "rmf-socketio5-fleet-a736"
        records = []
        for index, (fleet, fraction) in enumerate((("warehouse", .25), ("outdoor", .75))):
            state = dict(name=fleet, robots={"robot": dict(battery=fraction, status="working", future=[1]),
                                             "other": dict(battery=None, status="future")})
            record = dict(schema_commit="a736ddebeee9eaf3970756d7dd452065273da918",
                capture_time_us=100+index, protocol_version=5, transport="websocket-complete-text",
                direction="received", packet="42" + json.dumps(["/fleets/"+fleet+"/state", state]))
            records.append(record)
            output = module.convert(json.dumps(record), fmt)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(float(rows[0]["battery_remaining_fraction"]), fraction)
            self.assertEqual(rows[1]["rmf_battery_disposition"], "UNAVAILABLE")
            self.assertEqual(rows[1]["rmf_status_disposition"], "UNKNOWN_RETAINED")
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), record)
            self.assertEqual(rows[1]["source_record_hex"], "")
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.jsonl", Path(directory) / "out.csv"
            source.write_text("\n".join(map(json.dumps, records)))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 4)
                self.assertEqual(common["observations"][2]["fields"]["battery_remaining_fraction"], .75)
                self.assertEqual(common["platform_domain"], "Unknown")
            else:
                print("RMF socket.io common-reader integration NOT_RUN: executable not configured")
        for key, value in (("protocol_version", 4), ("direction", "sent"), ("transport", "polling"),
                           ("packet", "2"), ("packet", "42/admin,[]"), ("packet", '42["/wrong",{}]'),
                           ("packet", '42["/fleets/x/state",{"name":"x"},1]'),
                           ("packet", "42["), ("schema_commit", "unknown")):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(records[0], **{key: value})), fmt)

    def test_saved_farmbot_points_reuse_units_not_actual_position(self):
        records = [dict(exporter_version="v15.30.5", coordinate_unit="mm", capture_time_us=100,
            point=dict(id=10+i, device_id=5, pointer_type=kind, x=x, y=2000., z=-50.,
                       meta={"future": [1, 2]}))
            for i, (kind, x) in enumerate((("GenericPointer", 1250.), ("Plant", 2750.)))]
        fmt = "farmbot-point-15.30.5"
        output = module.convert("\n".join(map(json.dumps, records)), fmt)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([float(row["farm_point_x_m"]) for row in rows], [1.25, 2.75])
        self.assertEqual(float(rows[0]["farm_point_z_m"]), -.05)
        self.assertEqual(rows[0]["farm_point_id_text"], "id:10")
        self.assertNotIn("farm_position_x_m", rows[0])
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), records[0])
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.jsonl", Path(directory) / "out.csv"
            many = [dict(records[i % 2], point=dict(records[i % 2]["point"], id=i+1)) for i in range(1000)]
            source.write_text("\n".join(map(json.dumps, many)))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", fmt], check=True, capture_output=True)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(target.read_text())))), 1000)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 1000)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["observations"][-1]["fields"]["farm_point_x_m"], 2.75)
            else:
                print("FarmBot point common-reader integration NOT_RUN: executable not configured")
        for key, value in (("exporter_version", "unknown"), ("coordinate_unit", "m")):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(records[0], **{key: value})), fmt)
        for key, value in (("x", True), ("y", None), ("z", float("nan")), ("x", 50000),
                           ("id", True), ("pointer_type", "Unknown")):
            bad = dict(records[0], point=dict(records[0]["point"], **{key: value}))
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), fmt)
        missing = dict(records[0], point={key: value for key, value in records[0]["point"].items() if key != "z"})
        with self.assertRaises(ValueError): module.convert(json.dumps(missing), fmt)

    def test_tensorrt_times_reuse_profile_writer_without_clock_fusion(self):
        for duration in (1.25, 3.5):
            event = dict(startEnqMs=1., endEnqMs=1.1, startH2dMs=10., endH2dMs=10.5,
                         startComputeMs=10.5, endComputeMs=10.5 + duration,
                         startD2hMs=10.5 + duration, endD2hMs=10.75 + duration,
                         h2dMs=.5, computeMs=duration, d2hMs=.25, latencyMs=.75 + duration,
                         future={"retained": "yes"})
            text = json.dumps([event, {**event, "startEnqMs": .5}])
            output = module.convert(text, module.TRT_FORMAT, capture_time_us=123)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(float(rows[0]["trt_compute_duration_s"]), duration / 1000)
            self.assertEqual(float(rows[1]["trt_host_enqueue_start_offset_s"]), .0005)
            self.assertEqual(float(rows[0]["trt_ideal_transfer_compute_sum_s"]), (.75 + duration) / 1000)
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["canonical_event_hex"][4:])), event)
            self.assertNotIn("runtime_pid_text", rows[0])
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "in.json", Path(directory) / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", module.TRT_FORMAT, "--capture-time-us", "123"],
                    check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["trt_compute_duration_s"], duration / 1000)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for value in (-1, True, "1", float("inf"), 10**400, 5e-324):
                with self.assertRaises(ValueError):
                    module.convert(json.dumps([{**event, "computeMs": value}]), module.TRT_FORMAT, capture_time_us=1)
            with self.assertRaises(ValueError): module.convert(text, module.ORT_FORMAT, capture_time_us=1)
        with self.assertRaises(ValueError): module.convert('[{"count":1}]', module.TRT_FORMAT, capture_time_us=1)

    def saved_layer_profile(self, second=False):
        """A file shaped like Profiler::exportJSONProfile at the pinned 10.7.0 source.

        Two separately authored documents with no shared value: the second is held out of the
        converter-level assertions below and is used only through the actual common reader.
        """
        if second:
            return [{"count": 12},
                    {"name": "Reformat_0", "timeMs": 0.875, "averageMs": 0.0729167,
                     "medianMs": 0.0718, "percentage": 12.5},
                    {"name": "7", "timeMs": 6.125, "averageMs": 0.510417,
                     "medianMs": 0.5093, "percentage": 87.5, "future": {"retained": "yes"}}]
        return [{"count": 4},
                {"name": "conv1 + relu1", "timeMs": 2.5, "averageMs": 0.625,
                 "medianMs": 0.62, "percentage": 50.0},
                {"name": "0", "timeMs": 1.5, "averageMs": 0.375, "medianMs": 0.37,
                 "percentage": 30.0, "unknownKey": [1, 2]},
                {"name": "pool1", "timeMs": 1.0, "averageMs": 0.25, "medianMs": 0.24,
                 "percentage": 20.0}]

    def test_tensorrt_layer_profile_reuses_the_saved_writer_shape(self):
        """exportProfile is a different writer from the already adopted exportTimes trace.

        Profiler::exportJSONProfile leads with { "count": N } and then emits one object per
        collected layer in insertion order, and unlike Profiler::print it does not skip a layer
        whose time is zero. timeMs is a sum over recorded invocations and percentage divides by
        the sum of every layer's samples, so nothing here is a wall clock.
        """
        document = self.saved_layer_profile()
        output = module.convert(json.dumps(document), module.TRT_PROFILE_FORMAT, capture_time_us=4242)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 3)  # the count header is not a layer row
        self.assertEqual([float(row["trt_layer_total_time_s"]) for row in rows],
                         [2.5 / 1000, 1.5 / 1000, 1. / 1000])
        self.assertEqual(float(rows[0]["trt_layer_average_time_s"]), .625 / 1000)
        self.assertEqual(float(rows[2]["trt_layer_median_time_s"]), .24 / 1000)
        self.assertEqual([bytes.fromhex(row["trt_layer_name_hex"][4:]).decode() for row in rows],
                         ["conv1 + relu1", "0", "pool1"])
        self.assertEqual([row["source_event_index"] for row in rows], ["1", "2", "3"])
        self.assertEqual(json.loads(bytes.fromhex(rows[1]["canonical_event_hex"][4:])), document[2])
        self.assertEqual({row["trt_profile_iteration_count_text"] for row in rows}, {"i32:4"})
        self.assertEqual({row["record_time_us"] for row in rows}, {"4242"})
        self.assertEqual(float(rows[0]["trt_layer_reported_percentage"]), 50.)
        self.assertEqual(float(rows[0]["trt_layer_time_share"]), .5)
        self.assertEqual({row["trt_layer_share_disposition"] for row in rows},
                         {"REPORTED_SHARE_OF_SUMMED_LAYER_TIME_NOT_WALL_CLOCK"})
        self.assertIn("NOT_WALL_CLOCK_AND_NOT_SUMMABLE_INTO_ONE", rows[0]["trt_layer_time_basis"])
        self.assertIn("INCLUDING_WARMUP", rows[0]["trt_layer_time_basis"])
        self.assertNotIn("trt_compute_duration_s", rows[0])  # not the exportTimes layout
        self.assertNotIn("runtime_pid_text", rows[0])
        zero = list(csv.DictReader(io.StringIO(module.convert(json.dumps(
            [{"count": 1}, {"name": "unused", "timeMs": 0., "averageMs": 0., "medianMs": 0.,
              "percentage": 0.}]), module.TRT_PROFILE_FORMAT, capture_time_us=1))))
        self.assertEqual(float(zero[0]["trt_layer_total_time_s"]), 0.)
        self.assertEqual(float(zero[0]["trt_layer_time_share"]), 0.)
        outside = list(csv.DictReader(io.StringIO(module.convert(json.dumps(
            [{"count": 2}, {**document[1], "percentage": 140.5}]),
            module.TRT_PROFILE_FORMAT, capture_time_us=1))))
        self.assertEqual(outside[0]["trt_layer_time_share"], "")
        self.assertEqual(float(outside[0]["trt_layer_reported_percentage"]), 140.5)
        self.assertEqual(outside[0]["trt_layer_share_disposition"], "OUTSIDE_REPORTED_SHARE_RANGE")
        many = [{"count": 50}] + [{"name": f"layer_{i}", "timeMs": 1. + i, "averageMs": (1. + i) / 50,
                                   "medianMs": (1. + i) / 50, "percentage": 100 / 64}
                                  for i in range(64)]
        self.assertEqual(len(list(csv.DictReader(io.StringIO(module.convert(
            json.dumps(many), module.TRT_PROFILE_FORMAT, capture_time_us=9))))), 64)
        layer = document[1]
        for bad in ([], [{"count": 4}], [{"count": 4, "extra": 1}, layer], [[], layer],
                    [{"count": -1}, layer], [{"count": True}, layer], [{"count": 2.0}, layer],
                    [{"count": 1}, {**layer, "name": 5}], [{"count": 1}, {**layer, "name": None}],
                    [{"count": 1}, {**layer, "timeMs": -0.5}], [{"count": 1}, {**layer, "averageMs": True}],
                    [{"count": 1}, {**layer, "medianMs": "0.62"}], [{"count": 1}, {**layer, "timeMs": 5e-324}],
                    [{"count": 1}, {**layer, "percentage": None}],
                    [{"count": 1}, {key: value for key, value in layer.items() if key != "medianMs"}],
                    [{"count": 1}, "conv1"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.convert(json.dumps(bad), module.TRT_PROFILE_FORMAT, capture_time_us=1)
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                module.convert('[{"count":1},{"name":"a","timeMs":1.0,"averageMs":1.0,'
                               f'"medianMs":1.0,"percentage":{token}}}]',
                               module.TRT_PROFILE_FORMAT, capture_time_us=1)
        with self.assertRaises(ValueError):
            module.convert('[{"count":1,"count":1},' + json.dumps(layer) + "]",
                           module.TRT_PROFILE_FORMAT, capture_time_us=1)
        with self.assertRaises(ValueError):
            module.convert(json.dumps(document), module.TRT_PROFILE_FORMAT, capture_time_us=None)
        with self.assertRaises(ValueError):
            module.convert(json.dumps(document), module.TRT_FORMAT, capture_time_us=1)
        trace = dict(startEnqMs=1., endEnqMs=1.1, startH2dMs=10., endH2dMs=10.5, startComputeMs=10.5,
                     endComputeMs=11.5, startD2hMs=11.5, endD2hMs=11.75, h2dMs=.5, computeMs=1.,
                     d2hMs=.25, latencyMs=1.75)
        with self.assertRaises(ValueError):
            module.convert(json.dumps([trace]), module.TRT_PROFILE_FORMAT, capture_time_us=1)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "the built shared reader is the point of this test, so a missing reader "
                         "is an explicit skip and never a pass")
    def test_tensorrt_layer_profile_reaches_common_observation(self):
        """The held-out second document, through the CLI and the actual common reader."""
        document = self.saved_layer_profile(second=True)
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "profile.json", Path(directory) / "profile.csv"
            source.write_text(json.dumps(document))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", module.TRT_PROFILE_FORMAT,
                "--capture-time-us", "8080"], check=True, capture_output=True)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["platform_domain"], "Unknown")
        first, second = (observation["fields"] for observation in common["observations"])
        self.assertEqual(first["trt_layer_total_time_s"], .875 / 1000)
        self.assertEqual(second["trt_layer_total_time_s"], 6.125 / 1000)
        self.assertEqual(second["trt_layer_average_time_s"], .510417 / 1000)
        self.assertEqual(second["trt_layer_median_time_s"], .5093 / 1000)
        self.assertEqual(second["trt_layer_time_share"], .875)
        self.assertEqual(bytes.fromhex(second["trt_layer_name_hex"][4:]).decode(), "7")
        self.assertEqual(first["trt_profile_iteration_count_text"], "i32:12")
        self.assertEqual({observation["clock_basis"] for observation in common["observations"]}, {"Unknown"})
        units = common["profile_units"]
        self.assertIn("never_a_share_of_a_measured_wall_clock", units["trt_layer_reported_percentage"])
        self.assertIn("includes_warmup_iterations", units["trt_profile_iteration_count_text"])
        self.assertIn("preserved_verbatim", units["trt_layer_name_hex"])

    def test_hailo_saved_inference_reuses_timing_and_common_reader(self):
        def document(changes=None, count=1):
            row = dict.fromkeys(module.HAILO_HEADER, "")
            row.update(net_name="network-1", status="0", status_description="HAILO_SUCCESS", fps="125.5",
                       num_of_frames="1000", hw_latency="2.5", overall_latency="4.25", future="kept")
            row.update(changes or {})
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(module.HAILO_HEADER) + ["future"], lineterminator="\n")
            writer.writeheader()
            writer.writerows([row] * count)
            return stream.getvalue()
        for changes, count in (({}, 2), ({"hw_latency": "", "overall_latency": "10", "num_of_frames": ""}, 10000)):
            text = document(changes, count)
            output = module.convert(text, module.HAILO_FORMAT, capture_time_us=777)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), count)
            self.assertEqual(rows[0]["runtime_hw_latency_s"], "" if changes else "0.0025")
            self.assertEqual(float(rows[0]["runtime_overall_latency_s"]), .01 if changes else .00425)
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["canonical_event_hex"][4:]))["future"], "kept")
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "hailo.csv", Path(directory) / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", module.HAILO_FORMAT, "--capture-time-us", "777"],
                    check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(common["observations"][0]["fields"]["runtime_fps_reported"], 125.5)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for changes in ({"status": "1"}, {"hw_latency": "-1"}, {"fps": "NaN"},
                        {"num_of_frames": "1.2"}, {"overall_latency": "inf"}, {"net_name": ""}):
            with self.assertRaises(ValueError):
                module.convert(document(changes), module.HAILO_FORMAT, capture_time_us=1)
        for text in ("net_name,fps\na,1\n", document().rsplit(",", 1)[0], document(count=0)):
            with self.assertRaises(ValueError): module.convert(text, module.HAILO_FORMAT, capture_time_us=1)
        with self.assertRaises(ValueError): module.convert(document(), module.HAILO_FORMAT)
        unchanged = next(csv.DictReader(io.StringIO(module.convert(document(), module.HAILO_FORMAT, capture_time_us=777))))
        self.assertEqual(unchanged["runtime_frame_count_text"], "u64:1000")
        self.assertEqual(unchanged["runtime_frame_count_basis"],
                         "REPORTED_MINIMUM_RECEIVED_ACROSS_STREAMS_IN_5_4_0_SOURCE")
        older = document({"net_name": "net-424", "fps": "60.25", "num_of_frames": "2500",
                          "hw_latency": "1.5", "overall_latency": "", "average_power": "1.10"})
        row424 = next(csv.DictReader(io.StringIO(module.convert(older, module.HAILO_FORMAT_424, capture_time_us=888))))
        self.assertEqual(float(row424["runtime_fps_reported"]), 60.25)
        self.assertEqual(row424["runtime_frame_count_text"], "u64:2500")
        self.assertEqual(float(row424["runtime_hw_latency_s"]), .0015)
        self.assertEqual(row424["runtime_overall_latency_s"], "")  # 欠測は欠測のまま
        self.assertEqual(row424["runtime_frame_count_basis"],
                         "REPORTED_MINIMUM_RECEIVED_ACROSS_STREAMS_IN_4_24_0_SOURCE")
        self.assertEqual(json.loads(bytes.fromhex(row424["canonical_event_hex"][4:]))["average_power"], "1.10")
        same = [next(csv.DictReader(io.StringIO(module.convert(older, fmt, capture_time_us=888))))
                for fmt in (module.HAILO_FORMAT, module.HAILO_FORMAT_424)]
        self.assertEqual({k: v for k, v in same[0].items() if k != "runtime_frame_count_basis"},
                         {k: v for k, v in same[1].items() if k != "runtime_frame_count_basis"})
        self.assertNotEqual(same[0]["runtime_frame_count_basis"], same[1]["runtime_frame_count_basis"])
        for wrong in ("hailort-infer-csv-4.23.0", "hailort-infer-csv", "hailort-infer-csv-5.4"):
            with self.assertRaises(ValueError): module.convert(older, wrong, capture_time_us=888)
        with self.assertRaises(ValueError): module.convert_hailo_profile(older, 888, "hailort-infer-csv-9.9.9")
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "infer424.csv", Path(directory) / "out.csv"
            source.write_text(older)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", module.HAILO_FORMAT_424,
                "--capture-time-us", "888"], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 1)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["runtime_fps_reported"], 60.25)
                self.assertEqual(fields["runtime_frame_count_text"], "u64:2500")
                self.assertEqual(fields["runtime_frame_count_basis"],
                                 "REPORTED_MINIMUM_RECEIVED_ACROSS_STREAMS_IN_4_24_0_SOURCE")
                self.assertIsNone(fields["runtime_overall_latency_s"])
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            else:
                print("Hailo 4.24.0 common-reader integration NOT_RUN: executable not configured")

    def test_runtime_profile_order_duration_and_common_output(self):
        for duration, count in ((1250, 3), (2501, 10000)):
            events = [dict(cat="Node", name="reported,kernel", ph="X", pid=12, tid=3,
                           ts=10000 - index, dur=duration, args={"future": [1, {"kept": True}]})
                      for index in range(count)]
            events[-1]["cat"] = "FutureCategory"
            text = json.dumps(events, indent=1)
            output = module.convert(text, module.ORT_FORMAT, capture_time_us=777)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), count)
            self.assertEqual({row["record_time_us"] for row in rows}, {"777"})
            self.assertEqual(float(rows[0]["runtime_duration_s"]), duration / 1000000)
            self.assertEqual(rows[-1]["runtime_start_us_text"], "i64:" + str(10001 - count))
            self.assertEqual(rows[-1]["runtime_category_status"], "UNKNOWN_RETAINED")
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["canonical_event_hex"][4:])), events[0])
            import hashlib
            self.assertEqual(rows[0]["source_document_sha256"], "sha256:" + hashlib.sha256(text.encode()).hexdigest())
            with tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "profile.json", Path(directory) / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", module.ORT_FORMAT, "--capture-time-us", "777"],
                    check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(common["observations"][0]["fields"]["runtime_duration_s"], duration / 1000000)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        base = events[0]
        for key, value in (("ph", "B"), ("dur", -1), ("dur", True), ("ts", 0.5), ("pid", 2**31),
                           ("args", []), ("name", "")):
            with self.assertRaises(ValueError):
                module.convert(json.dumps([{**base, key: value}]), module.ORT_FORMAT, capture_time_us=1)
        for text in ("[]", "{}", '[{"ph":"X","ph":"B"}]', "[NaN]"):
            with self.assertRaises(ValueError): module.convert(text, module.ORT_FORMAT, capture_time_us=1)
        with self.assertRaises(ValueError): module.convert(json.dumps([base]), module.ORT_FORMAT)
        with self.assertRaises(ValueError): module.convert("", capture_time_us=1)

    def test_mobile_sdk_keys_reuse_units_without_snapshot_inference(self):
        def record(key, value):
            return dict(sdk_version="5.18.0", sdk_source_commit="07d37cfdff865cdda9d523b00b723c9984575f8f",
                        key=key, value=value, component_index=1, capture_time_us=777, future={"retained": True})
        for voltage, current, temperature in ((24000, -2500, 20.5), (12500, 1200, -5.5)):
            entries = [record("Voltage", voltage), record("Current", current), record("BatteryTemperature", temperature),
                       record("FullChargeCapacity", 5000), record("ChargeRemaining", 2500),
                       record("ChargeRemainingInPercent", 50), record("Connection", False),
                       record("Current", None), record("ChargeRemainingInPercent", 101)]
            text = "\n".join(json.dumps(entry) for entry in entries)
            output = module.convert(text, module.MSDK_FORMAT)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 9)
            self.assertEqual(float(rows[0]["battery_voltage_v"]), voltage / 1000)
            self.assertEqual(float(rows[1]["battery_current_a"]), current / 1000)
            self.assertEqual(rows[1]["battery_current_sign_basis"], "NEGATIVE_DISCHARGING")
            self.assertEqual(float(rows[2]["battery_temperature_k"]), temperature + 273.15)
            self.assertEqual(float(rows[3]["battery_full_charge_ah"]), 5.)
            self.assertEqual(float(rows[4]["battery_remaining_charge_ah"]), 2.5)
            self.assertEqual(float(rows[5]["battery_remaining_fraction"]), .5)
            self.assertEqual(rows[6]["battery_connection_reported"], "FALSE")
            self.assertEqual(rows[7]["sdk_value_disposition"], "UNAVAILABLE_NOT_ZERO")
            self.assertEqual(rows[7]["battery_current_a"], "")
            self.assertEqual(rows[8]["battery_remaining_fraction"], "")
            self.assertTrue(all(r["sdk_component_index"] == "1" for r in rows))
            for row, original in zip(rows, text.splitlines()):
                self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), original)
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); source = base / "in.jsonl"; out = base / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(out), "--source-format", module.MSDK_FORMAT], check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(out), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 9)
                    self.assertEqual(common["observations"][1]["fields"]["battery_current_a"], current / 1000)
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            for bad in (record("Voltage", True), record("Voltage", -1), record("Current", 2**31),
                        record("Connection", 0), record("BatteryTemperature", 128), record("BatteryTemperature", True),
                        record("Other", 1), dict(entries[0], component_index=True), dict(entries[0], sdk_version="5.17.0"),
                        dict(entries[0], sdk_source_commit="wrong")):
                with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.MSDK_FORMAT)

    def test_parrot_events_reuse_electrical_units(self):
        pin = "2299c689be5951fa2104793e0154b0b5b1896c03"
        def record(event, payload):
            return dict(arsdk_xml_commit=pin, event=event, payload=payload, capture_time_us=777, future=[1, 2])
        for voltage, temperature, capacity in ((12500, 295, 2500), (8000, 310, 1200)):
            entries = [record("voltage", {"voltage": voltage}), record("temperature", {"temperature": temperature}),
                record("capacity", {"full_charge": capacity * 2, "remaining": capacity}),
                record("cell_voltage", {"index": 2, "cell_voltage": voltage // 4}),
                record("cycle_count", {"count": 54}), record("health", {"state_of_health": 75}),
                record("health", {"state_of_health": 255})]
            text = "\n".join(json.dumps(entry) for entry in entries)
            output = module.convert(text, module.PARROT_FORMAT)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 7)
            self.assertEqual(float(rows[0]["battery_voltage_v"]), voltage / 1000)
            self.assertEqual(float(rows[1]["battery_temperature_k"]), temperature)
            self.assertEqual(float(rows[2]["battery_remaining_charge_ah"]), capacity / 1000)
            self.assertEqual(rows[3]["battery_cell_index"], "2")
            self.assertEqual(float(rows[3]["battery_cell_voltage_v"]), (voltage // 4) / 1000)
            self.assertEqual(rows[4]["battery_cycle_count"], "54")
            self.assertEqual(float(rows[5]["battery_state_of_health_fraction"]), .75)
            self.assertEqual(rows[6]["battery_state_of_health_fraction"], "")
            for row, original in zip(rows, text.splitlines()):
                self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), original)
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); source = base / "input.jsonl"; out = base / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(out), "--source-format", module.PARROT_FORMAT], check=True, capture_output=True)
                self.assertEqual(out.read_text(), output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(out), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 7)
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], voltage / 1000)
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            for bad in (record("voltage", {"voltage": -1}), record("voltage", {"voltage": True}),
                        record("voltage", {"voltage": 65536}), record("voltage", {}),
                        record("capacity", {"remaining": 1}), record("unknown", {}),
                        dict(entries[0], arsdk_xml_commit="wrong"), dict(entries[0], capture_time_us=True)):
                with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.PARROT_FORMAT)

    @unittest.skipUnless(importlib.util.find_spec("google") and importlib.util.find_spec("google.protobuf"), "optional pinned protobuf decoder")
    def test_saved_protobuf_reuses_mavsdk_semantics(self):
        import struct
        def record(wire):
            return dict(sdk_version="3.17.4", proto_commit="d3cb35f49d1379d2d95a6d22cdbe40ff4074e9bf",
                        capture_time_us=123, battery_protobuf_hex=wire.hex(), future=[1])
        def grpc_record(frame):
            result = record(b""); del result["battery_protobuf_hex"]
            return dict(result, grpc_frame_hex=frame.hex())
        for voltage, current, remaining in ((24., -2., 75.), (12.5, 3., 25.)):
            wire = b"\x08\x05" + b"".join(bytes([tag]) + struct.pack("<f", value)
                for tag, value in ((0x15, 20.), (0x1d, voltage), (0x25, current),
                                   (0x2d, 2.5), (0x35, remaining), (0x3d, 30.))) + b"\x40\x02\xa0\x06\x01"
            response = b"\x0a" + bytes([len(wire)]) + wire + b"\xa8\x06\x01"
            frame = b"\x00" + len(response).to_bytes(4, "big") + response
            for source_format, entry, unknown_count in ((module.PROTO_FORMAT, record(wire), "1"),
                                                       (module.GRPC_FORMAT, grpc_record(frame), "2")):
                text = json.dumps(entry)
                rows = list(csv.DictReader(io.StringIO(module.convert(text, source_format))))
                self.assertEqual(float(rows[0]["battery_voltage_v"]), voltage)
                self.assertEqual(float(rows[0]["battery_current_a"]), current)
                self.assertEqual(float(rows[0]["battery_remaining_fraction"]), remaining / 100)
                self.assertEqual(rows[0]["protobuf_unknown_fields"], unknown_count)
                self.assertEqual(rows[0]["protobuf_zero_or_absent_fields_hex"], "hex:")
                self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
                if source_format == module.GRPC_FORMAT:
                    self.assertEqual(rows[0]["protobuf_value_basis"], "GRPC_MESSAGE_NOT_RPC_SUCCESS")
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); source = base / "input.jsonl"; out = base / "out.csv"
                    source.write_text(text)
                    subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                        str(source), str(out), "--source-format", source_format], check=True, capture_output=True)
                    if os.environ.get("MUSUBI_TELEMETRY_READER"):
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(out)], check=True, capture_output=True).stdout)
                        self.assertEqual(common["main_rows"], 1)
                        self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], voltage)
            for bad in (frame[:-1], b"\x01" + frame[1:], frame + b"\x00", b"\x00" * 5,
                        b"\x00\x00\x00\x00\x01\x00"):
                with self.assertRaises(ValueError): module.convert(json.dumps(grpc_record(bad)), module.GRPC_FORMAT)
            for bad in (wire[:-1], b"\x1d\x00", b"\x00", b"\x1d" + struct.pack("<f", -1.)):
                with self.assertRaises(ValueError): module.convert(json.dumps(record(bad)), module.PROTO_FORMAT)
        for wire in (b"", b"\x1d\x00\x00\x00\x00"):
            row = next(csv.DictReader(io.StringIO(module.convert(json.dumps(record(wire)), module.PROTO_FORMAT))))
            self.assertEqual(float(row["battery_voltage_v"]), 0.)
            self.assertIn("voltageV", bytes.fromhex(row["protobuf_zero_or_absent_fields_hex"][4:]).decode())
        defaults = module.convert(json.dumps(grpc_record(b"\x00\x00\x00\x00\x02\x0a\x00")), module.GRPC_FORMAT)
        self.assertEqual(float(next(csv.DictReader(io.StringIO(defaults)))["battery_voltage_v"]), 0.)
        unknown = b"\x25" + struct.pack("<f", float("nan")) + b"\x35" + struct.pack("<f", -1.)
        row = next(csv.DictReader(io.StringIO(module.convert(json.dumps(record(unknown)), module.PROTO_FORMAT))))
        self.assertEqual((row["battery_current_a"], row["battery_remaining_fraction"]), ("", ""))
        for patch in (dict(proto_commit="wrong"), dict(sdk_version="old"), dict(battery_protobuf_hex="0")):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(record(b""), **patch)), module.PROTO_FORMAT)

    def test_ping_distance_reuses_units_without_depth_or_confidence_inference(self):
        def entry(message, confidence, distance):
            payload = dict(distance=distance, confidence=confidence)
            if message == 1212:
                payload.update(transmit_duration=250, ping_number=4294967295,
                    scan_start=500, scan_length=10000, gain_setting=6)
            return dict(protocol_commit='1746cd03f942d58bcf08253055854caea2e33fda',
                capture_time_us=100, message_id=message, src_device_id=5, payload=payload, future=[1])
        records = [entry(1211, 0, 1250), entry(1212, 90, 2500)]
        output = module.convert('\n'.join(map(json.dumps, records)), 'ping1d-distance-1746')
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([float(r['sonar_reported_distance_m']) for r in rows], [1.25, 2.5])
        self.assertEqual([float(r['sonar_reported_confidence_fraction']) for r in rows], [0., .9])
        self.assertEqual(rows[0]['sonar_scan_length_m'], '')
        self.assertEqual(float(rows[1]['sonar_scan_start_m']), .5)
        self.assertEqual(float(rows[1]['sonar_transmit_duration_s']), .00025)
        self.assertEqual(int(rows[1]['sonar_ping_number_reported']), 4294967295)
        self.assertEqual(json.loads(bytes.fromhex(rows[0]['source_record_hex'][4:])), records[0])
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'in.jsonl', Path(tmp) / 'out.csv'
            source.write_text('\n'.join(map(json.dumps, records)))
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_ardupilot_battery_csv.py'),
                str(source), str(target), '--source-format', 'ping1d-distance-1746'], check=True, capture_output=True)
            self.assertEqual(target.read_text(), output)
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                    str(target), '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 2)
                self.assertEqual(common['observations'][1]['fields']['sonar_reported_distance_m'], 2.5)
                self.assertEqual(common['platform_domain'], 'Unknown')
                self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
        for message, maximum in ((1211, 255), (1212, 65535)):
            row = next(csv.DictReader(io.StringIO(module.convert(json.dumps(entry(message, maximum, 0)), 'ping1d-distance-1746'))))
            self.assertEqual(row['sonar_reported_confidence_fraction'], '')
            self.assertEqual(row['sonar_confidence_status'], 'OUTSIDE_PERCENT_RANGE')
        for patch in ({'protocol_commit': 'other'}, {'message_id': 1001}, {'src_device_id': -1},
                      {'payload': {'distance': True, 'confidence': 1}}, {'payload': {'distance': 2**32, 'confidence': 1}},
                      {'payload': {'distance': 1, 'confidence': 256}}, {'capture_time_us': -1}):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(records[0], **patch)), 'ping1d-distance-1746')
        with self.assertRaises(ValueError): module.convert(json.dumps(records[1])[:-1], 'ping1d-distance-1746')

    def test_farmduino_r82_reuses_farmbot_position_without_execution(self):
        def entry(response, time):
            return dict(firmware_commit="52053ec86e6cef1a39340439dfdfaf9cb2337a60",
                capture_time_us=time, steps_per_mm={"x": 80, "y": 80, "z": 400}, response=response, future=[1])
        records = [entry("R82 X1000.00 Y-250.50 Z0.00 Q-2147483648\r\n", 100),
                   entry("R82 X-1.25 Y2000.00 Z30.00 Q2147483647", 200)]
        output = module.convert("\n".join(map(json.dumps, records)), "farmduino-r82-52053ec8")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]["farm_position_x_m"]), 1)
        self.assertEqual(float(rows[0]["farm_position_y_m"]), -.2505)
        self.assertEqual(float(rows[1]["farm_position_x_m"]), -.00125)
        self.assertEqual(rows[0]["farmduino_q_reported_text"], "i32:-2147483648")
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), records[0])
        self.assertEqual(rows[0]["busy_reported"], "")
        with tempfile.TemporaryDirectory() as tmp:
            source, output_path = Path(tmp) / "in.jsonl", Path(tmp) / "out.csv"
            source.write_text("\n".join(map(json.dumps, records)))
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(output_path), "--source-format", "farmduino-r82-52053ec8"], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(output_path)], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][1]["fields"]["farm_position_y_m"], 2)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for response in ("F82 Q0", "R84 X1.00 Y2.00 Z3.00 Q0", "R82 XNaN Y0.00 Z0.00 Q0",
                         "R82 X1.00 Y2.00 Z3.00 Q2147483648", "R82 X1.00 Y2.00 Z3.00 Q0 X1.00",
                         "R82 X1.00 Y2.00 Q0", "R82 X1.00 Y2.00 Z3.00 Q0\nR02 Q0"):
            with self.assertRaises(ValueError): module.convert(json.dumps(entry(response, 100)), "farmduino-r82-52053ec8")
        for key, value in (("firmware_commit", "other"), ("steps_per_mm", {"x": 0, "y": 80, "z": 400}),
                           ("steps_per_mm", {"x": True, "y": 80, "z": 400}), ("capture_time_us", True)):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(dict(records[0], **{key: value})), "farmduino-r82-52053ec8")

    def test_tegrastats_reuses_cpu_units_and_named_thermal_reports(self):
        import hashlib
        def entry(report, time=55):
            return dict(exporter_version="l4t-36.4.3", capture_time_us=time, report=report, future=[1])
        for line, expected_load, expected_hz, expected_k, expected_rows in (
            ("RAM 220/38955MB CPU [1%@102,off,100%@204] MCPU@45C future=kept", .01, 102000000, 318.15, 5),
            ("CPU [25%,off,0%]@1200 CPU@-5.25C GR3D_FREQ 0%@[306,306]", .25, 1200000000, 267.9, 6)):
            text = json.dumps(entry(line))
            output = module.convert(text, module.TEGRA_FORMAT)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), expected_rows)
            self.assertEqual(float(rows[0]["tegrastats_reported_utilization_fraction"]), expected_load)
            self.assertEqual(int(rows[0]["tegrastats_reported_frequency_hz"]), expected_hz)
            self.assertEqual(rows[1]["tegrastats_value_disposition"], "OFF_REPORTED")
            self.assertEqual(rows[1]["tegrastats_reported_utilization_fraction"], "")
            self.assertAlmostEqual(float(rows[3]["temperature_k"]), expected_k)
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
            self.assertTrue(all(r["source_record_hex"] == "" for r in rows[1:]))
            self.assertTrue(all(r["tegrastats_source_sha256"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest() for r in rows))
            with tempfile.TemporaryDirectory() as tmp:
                source, target = Path(tmp) / "in.jsonl", Path(tmp) / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", module.TEGRA_FORMAT], check=True, capture_output=True)
                self.assertEqual(target.read_text(), output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], expected_rows)
                    self.assertEqual(common["observations"][0]["fields"]["tegrastats_reported_frequency_hz"], expected_hz)
                    self.assertAlmostEqual(common["observations"][3]["fields"]["temperature_k"], expected_k)
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        for line, gpc, fraction, rails, memory, unselected in (
            ("RAM 1024/7620MB (lfb 12x4MB) SWAP 0/3810MB (cached 0MB) GR3D_FREQ 37%@[624,624] "
             "VDD_IN 4321/3210 VDD_CPU_GPU_CV 1500/1200 EMC_FREQ 9%@2133 CPU@44.5C",
             [624000000, 624000000], .37,
             {"VDD_IN": (4.321, 3.21), "VDD_CPU_GPU_CV": (1.5, 1.2)}, (1024, 7620, "MB", 12, 4), 6),
            ("GR3D_FREQ 0%@306 RAM 220/38955MB VDD_SOC 900/850 NVDEC 115 GPU@31.5C",
             [306000000], 0.0, {"VDD_SOC": (0.9, 0.85)}, (220, 38955, "MB", None, None), 2)):
            text = json.dumps(entry(line))
            rows = list(csv.DictReader(io.StringIO(module.convert(text, module.TEGRA_FORMAT))))
            gpu_rows = [r for r in rows if r["tegrastats_record_kind"] == "GPU"]
            self.assertEqual([int(r["tegrastats_reported_frequency_hz"]) for r in gpu_rows], gpc)
            self.assertEqual([int(r["tegrastats_block_index"]) for r in gpu_rows], list(range(len(gpc))))
            self.assertTrue(all(float(r["tegrastats_reported_utilization_fraction"]) == fraction
                                for r in gpu_rows))
            self.assertTrue(all(r["tegrastats_value_disposition"]
                                == "REPORTED_GPU_PROPORTION_OVER_UNDOCUMENTED_PERIOD" for r in gpu_rows))
            used, total, label, blocks, block_size = memory
            row = next(r for r in rows if r["tegrastats_record_kind"] == "MEMORY")
            self.assertEqual((int(row["tegrastats_reported_memory_used"]),
                              int(row["tegrastats_reported_memory_total"]),
                              row["tegrastats_reported_memory_unit_label"]), (used, total, label))
            self.assertAlmostEqual(float(row["tegrastats_reported_utilization_fraction"]), used / total)
            self.assertEqual(row["tegrastats_reported_free_block_count"],
                             "" if blocks is None else str(blocks))
            self.assertEqual(row["tegrastats_reported_free_block_size"],
                             "" if block_size is None else str(block_size))
            self.assertIn("NO_BYTE_BASE_DOCUMENTED", row["tegrastats_value_disposition"])
            rail_rows = {bytes.fromhex(r["tegrastats_block_hex"][4:]).decode(): r
                         for r in rows if r["tegrastats_record_kind"] == "POWER_RAIL"}
            self.assertEqual(set(rail_rows), set(rails))
            for name, (current, average) in rails.items():
                self.assertAlmostEqual(float(rail_rows[name]["tegrastats_reported_power_current_w"]),
                                       current)
                self.assertAlmostEqual(float(rail_rows[name]["tegrastats_reported_power_average_w"]),
                                       average)
                self.assertIn("WINDOW_UNDOCUMENTED", rail_rows[name]["tegrastats_value_disposition"])
            self.assertTrue(all(int(r["tegrastats_unselected_token_count"]) == unselected for r in rows))
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "resource.csv"
                target.write_text(module.convert(text, module.TEGRA_FORMAT))
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], len(rows))
                    gpu_common = [o["fields"] for o in common["observations"]
                                  if o["fields"]["tegrastats_record_kind"] == "GPU"]
                    self.assertEqual([f["tegrastats_reported_frequency_hz"] for f in gpu_common], gpc)
                    memory_common = next(o["fields"] for o in common["observations"]
                                         if o["fields"]["tegrastats_record_kind"] == "MEMORY")
                    self.assertEqual(memory_common["tegrastats_reported_memory_total"], total)
                    self.assertEqual(memory_common["tegrastats_reported_memory_unit_label"], label)
                    units = common["profile_units"]
                    for column in ("tegrastats_block_index", "tegrastats_reported_memory_used",
                                   "tegrastats_reported_memory_total",
                                   "tegrastats_reported_memory_unit_label",
                                   "tegrastats_reported_free_block_count",
                                   "tegrastats_reported_free_block_size",
                                   "tegrastats_reported_power_current_w",
                                   "tegrastats_reported_power_average_w",
                                   "tegrastats_unselected_token_count"):
                        self.assertTrue(units.get(column), f"{column} must declare its meaning")
                    self.assertIn("no_byte_base", units["tegrastats_reported_memory_total"])
                    self.assertIn("averaging_window_is_not_documented",
                                  units["tegrastats_reported_power_average_w"])
                    self.assertIn("never_summed_across_rails",
                                  units["tegrastats_reported_power_current_w"])
        for line in ("GR3D_FREQ 101%@306", "GR3D_FREQ 5%@0", "GR3D_FREQ 5%@[306,0]",
                     "GR3D_FREQ 5%@306 GR3D_FREQ 6%@306", "GR3D_FREQ 5%@[306,]",
                     "RAM 0/0MB", "RAM 9/8MB", "RAM 1/2MB (lfb 4x1KB)", "RAM 1/2MB RAM 3/4MB",
                     "VDD_IN 100", "VDD_IN -1/2", "VDD_IN 1/2 VDD_IN 3/4",
                     "VDD_IN 4321mW/3210mW", "CPU [1%@100] VDD_IN 5mW",
                     "RAM 1/2MB (lfb broken)", "RAM 1/2MB (lfb 4x)"):
            with self.assertRaises(ValueError, msg=line):
                module.convert(json.dumps(entry(line)), module.TEGRA_FORMAT)
        thermal = next(csv.DictReader(io.StringIO(module.convert(json.dumps(entry("GPU@30C")), module.TEGRA_FORMAT))))
        self.assertEqual(thermal["tegrastats_record_kind"], "TEMPERATURE")
        for line in ("CPU [101%@100]", "CPU [1%@0]", "CPU [1%@100] CPU [2%@200]",
                     "CPU [bad]", "CPU [1%@100", "CPU [1%@100] GPU@NaNC",
                     "GPU@30C GPU@31C", "GPU@-274C", "future=kept", "CPU [1%@100]\nGPU@30C"):
            with self.assertRaises(ValueError): module.convert(json.dumps(entry(line)), module.TEGRA_FORMAT)
        for patch in ({"exporter_version": "other"}, {"capture_time_us": -1}, {"report": []}):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(entry("GPU@30C"), **patch)), module.TEGRA_FORMAT)
        large = "\n".join(json.dumps(entry("CPU [1%@102,off] MCPU@45C", i)) for i in range(10000))
        output = module.convert(large, module.TEGRA_FORMAT)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 30000)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "large.csv"
                target.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 30000)
                self.assertEqual(len(common["observations"]), 30000)

    def test_every_saved_fleet_manager_column_declares_its_meaning(self):
        """A03-05-U005. The reader emits every column, so an undeclared one reaches common output
        as a bare value. The fleet-manager response's identity, time and missing/unknown columns
        are exactly the ones that must not arrive that way."""
        import re
        profile = (ROOT / "profiles/declared/electrical-schema-reuse"
                   / "json-profile.toml").read_text(encoding="utf-8")
        declared = set(re.findall(r'(?m)^"?([A-Za-z0-9_.]+)"?\s*=', profile))
        for group in (module.RMF_WEB_FIELDS, module.RMF_FIELDS, module.RMF_LOCATION_FIELDS):
            for column in group:
                with self.subTest(column=column):
                    self.assertIn(column, declared)
        self.assertIn("rmf_robot_key_hex", module.RMF_WEB_FIELDS)
        self.assertIn("rmf_name_hex", module.RMF_WEB_FIELDS)
        for token in ("UNAVAILABLE", "UNKNOWN_RETAINED", "SCHEMA_KNOWN_REPORTED"):
            self.assertIn(token, profile)

    def test_an_idle_fleet_snapshot_is_read_without_inventing_a_robot(self):
        """A03-05-U005. An idle fleet and an omitted optional field are ordinary responses, and a
        response that describes no robot must still arrive with its envelope and capture time."""
        import hashlib
        pin = "a736ddebeee9eaf3970756d7dd452065273da918"

        def rows_for(state, capture=100):
            text = json.dumps(dict(schema_commit=pin, capture_time_us=capture, state=state))
            return text, list(csv.DictReader(io.StringIO(
                module.convert(text, module.RMF_WEB_FORMAT))))

        text, rows = rows_for(dict(name="warehouse", robots={}))
        self.assertEqual(len(rows), 1)  # never zero: the source is not silently dropped
        row = rows[0]
        self.assertEqual(row["rmf_record_kind"], "FLEET_SNAPSHOT")
        self.assertEqual(row["rmf_robot_count"], "0")
        self.assertEqual(row["rmf_robots_availability"], module.RMF_ROBOTS_EMPTY)
        self.assertEqual(bytes.fromhex(row["rmf_fleet_name_hex"][4:]).decode(), "warehouse")
        self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), text)
        self.assertEqual(row["rmf_snapshot_sha256"],
                         "sha256:" + hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(row["record_time_us"], "100")
        for empty in ("rmf_robot_key_hex", "rmf_name_hex", "rmf_task_id_hex",
                      "battery_remaining_fraction", "rmf_status_reported_hex",
                      "rmf_unix_millis_reported_text", "rmf_map_name_hex", "rmf_map_x_m",
                      "rmf_map_y_m", "rmf_map_yaw_rad"):
            self.assertEqual(row[empty], "")
        for basis in ("rmf_battery_disposition", "rmf_status_disposition", "rmf_position_basis"):
            self.assertEqual(row[basis], module.RMF_SNAPSHOT_BASIS)
            self.assertNotEqual(row[basis], "UNAVAILABLE")

        _, absent = rows_for(dict(name="warehouse"))
        self.assertEqual(len(absent), 1)
        self.assertEqual(absent[0]["rmf_robots_availability"], module.RMF_ROBOTS_UNAVAILABLE)
        self.assertEqual(absent[0]["rmf_robot_count"], "")  # absent is not zero
        self.assertNotEqual(absent[0]["rmf_robots_availability"], row["rmf_robots_availability"])

        _, unnamed = rows_for(dict(robots={}))
        self.assertEqual(unnamed[0]["rmf_fleet_name_hex"], "")
        _, blank = rows_for(dict(name="", robots={}))
        self.assertEqual(blank[0]["rmf_fleet_name_hex"], "hex:")

        for bad in (dict(name=5, robots={}), dict(name=[], robots={}), dict(name=True, robots={}),
                    dict(name="f", robots=[]), dict(name="f", robots="none"),
                    dict(name="f", robots=0), dict(name="f", robots={"r": 5})):
            with self.subTest(state=bad), self.assertRaises(ValueError):
                rows_for(bad)

        _, populated = rows_for(dict(name="f", robots={
            "a": dict(battery=.5, status="idle"), "b": {}}))
        self.assertEqual(len(populated), 2)
        for each in populated:
            self.assertEqual(each["rmf_record_kind"], module.RMF_ROBOT_KIND)
            self.assertEqual(each["rmf_robot_count"], "2")
            self.assertEqual(each["rmf_robots_availability"], module.RMF_ROBOTS_WITH)
            self.assertNotEqual(each["rmf_robot_key_hex"], "")
        self.assertEqual(populated[1]["rmf_battery_disposition"], "UNAVAILABLE")
        self.assertNotEqual(populated[1]["rmf_battery_disposition"], module.RMF_SNAPSHOT_BASIS)

        def socket(state, room="/fleets/warehouse/state"):
            return json.dumps(dict(schema_commit=pin, capture_time_us=100, protocol_version=5,
                transport="websocket-complete-text", direction="received",
                packet="42" + json.dumps([room, state])))
        empty_fleet = list(csv.DictReader(io.StringIO(module.convert(
            socket(dict(name="warehouse", robots={})), module.RMF_SOCKET_FORMAT))))
        self.assertEqual(len(empty_fleet), 1)
        self.assertEqual(empty_fleet[0]["rmf_record_kind"], "FLEET_SNAPSHOT")
        for wrong in (dict(robots={}), dict(name="other", robots={})):
            with self.subTest(state=wrong), self.assertRaises(ValueError):
                module.convert(socket(wrong), module.RMF_SOCKET_FORMAT)

        reader = inav_common_reader("electrical-schema-reuse", "rmf idle fleet")
        if reader:
            text, _ = rows_for(dict(name="warehouse", robots={}))
            with tempfile.TemporaryDirectory() as tmp:
                source, target = Path(tmp) / "in.jsonl", Path(tmp) / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable,
                    str(ROOT / "scripts/convert_ardupilot_battery_csv.py"), str(source),
                    str(target), "--source-format", module.RMF_WEB_FORMAT],
                    check=True, capture_output=True)
                common = json.loads(subprocess.run([reader,
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["rmf_record_kind"], "FLEET_SNAPSHOT")
            self.assertEqual(fields["rmf_robots_availability"], module.RMF_ROBOTS_EMPTY)
            self.assertEqual(fields["battery_remaining_fraction"], None)
            self.assertEqual(common["platform_domain"], "Unknown")

    def test_rmf_web_fleet_reuses_fraction_and_retains_each_robot(self):
        import hashlib
        first = dict(schema_commit="a736ddebeee9eaf3970756d7dd452065273da918", capture_time_us=100,
            state=dict(name="warehouse", robots={
                "robot-1": dict(name="r1", battery=.25, status="charging", task_id="job",
                    unix_millis_time=1700000000123, location=dict(map="L1", x=1, y=2, yaw=.3)),
                "robot-2": dict(name=None, battery=None, status="future-mode", future=[1, 2]),
                "robot-3": {}}))
        second = dict(schema_commit=first["schema_commit"], capture_time_us=200,
            state=dict(name="another", robots={"other": dict(battery=1., status="working", task_id="")}))
        for record, count in ((first, 3), (second, 1)):
            text = json.dumps(record)
            output = module.convert(text, module.RMF_WEB_FORMAT)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), count)
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
            self.assertTrue(all(r["source_record_hex"] == "" for r in rows[1:]))
            self.assertTrue(all(r["rmf_snapshot_sha256"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest() for r in rows))
            self.assertEqual(float(rows[0]["battery_remaining_fraction"]), .25 if count == 3 else 1.)
            if count == 3:
                self.assertEqual(rows[0]["rmf_unix_millis_reported_text"], "unix_ms:1700000000123")
                self.assertEqual(rows[1]["rmf_battery_disposition"], "UNAVAILABLE")
                self.assertEqual(rows[1]["rmf_status_disposition"], "UNKNOWN_RETAINED")
                self.assertEqual(rows[2]["rmf_status_disposition"], "UNAVAILABLE")
                self.assertEqual(rows[1]["battery_remaining_fraction"], "")
            with tempfile.TemporaryDirectory() as tmp:
                source, target = Path(tmp) / "in.jsonl", Path(tmp) / "out.csv"
                source.write_text(text)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                    str(source), str(target), "--source-format", module.RMF_WEB_FORMAT], check=True, capture_output=True)
                self.assertEqual(target.read_text(), output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count)
                    self.assertEqual(common["observations"][0]["fields"]["battery_remaining_fraction"], .25 if count == 3 else 1.)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        large = dict(first, state=dict(name="large", robots={str(i): {"battery": .5} for i in range(2000)}))
        expanded = module.convert(json.dumps(large), module.RMF_WEB_FORMAT)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(expanded)))), 2000)
        self.assertLess(len(expanded), 2_000_000)  # snapshot retained once, not per robot
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "fleet.csv"
                target.write_text(expanded)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2000)
                self.assertEqual(len(common["observations"]), 2000)
        for patch in ({"battery": 25}, {"battery": True}, {"battery": -1}, {"status": 1},
                      {"unix_millis_time": True}, {"unix_millis_time": -1}, {"name": []}):
            bad = dict(first, state=dict(name="fleet", robots={"r": patch}))
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.RMF_WEB_FORMAT)
        for patch in ({"schema_commit": "wrong"}, {"capture_time_us": -1}):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(first, **patch)), module.RMF_WEB_FORMAT)
        for patch in ({"state": {"name": "", "robots": {"r": {}}}},
                      {"state": {"name": "fleet", "robots": {}}}):
            module.convert(json.dumps(dict(first, **patch)), module.RMF_WEB_FORMAT)
        for malformed in (json.dumps(first)[:-1], json.dumps(first).replace('"battery": 0.25', '"battery": 0.25, "battery": 0.5')):
            with self.assertRaises(ValueError): module.convert(malformed, module.RMF_WEB_FORMAT)
        with self.assertRaises(ValueError): module.convert(json.dumps(first), module.RMF_FORMAT)

    def test_rmf_web_pose_and_repeated_state_association(self):
        """The saved web pose under the same declaration, and plan rows that stay separable."""
        def web(location, declare=True, **robot):
            envelope = dict(schema_commit="a736ddebeee9eaf3970756d7dd452065273da918",
                capture_time_us=2000,
                state=dict(name="authored-fleet", robots={"amr-1": dict(
                    name="amr-1", battery=.4, status="working", location=location, **robot)}))
            if declare:
                envelope.update(location_unit="m", yaw_unit="rad")
            return envelope

        posed = web({"map": "L7", "x": -8.5, "y": 3.25, "yaw": 2.75})
        row = list(csv.DictReader(io.StringIO(module.convert(json.dumps(posed), module.RMF_WEB_FORMAT))))[0]
        self.assertEqual((float(row["rmf_map_x_m"]), float(row["rmf_map_y_m"])), (-8.5, 3.25))
        self.assertEqual(float(row["rmf_map_yaw_rad"]), 2.75)
        self.assertEqual(bytes.fromhex(row["rmf_map_name_hex"][4:]).decode(), "L7")
        self.assertEqual(row["rmf_position_basis"],
                         "CALLER_DECLARED_UNITS_IN_REPORTED_MAP_FRAME_NOT_GLOBAL_OR_GEODETIC")
        self.assertEqual(bytes.fromhex(row["rmf_robot_key_hex"][4:]).decode(), "amr-1")
        self.assertEqual(float(row["battery_remaining_fraction"]), .4)
        self.assertEqual(row["rmf_status_disposition"], "SCHEMA_KNOWN_REPORTED")
        absent = list(csv.DictReader(io.StringIO(module.convert(
            json.dumps(web(None)), module.RMF_WEB_FORMAT))))[0]
        self.assertEqual((absent["rmf_map_x_m"], absent["rmf_map_y_m"], absent["rmf_map_yaw_rad"]),
                         ("", "", ""))
        self.assertEqual(absent["rmf_position_basis"], "NOT_REPORTED")
        undeclared = list(csv.DictReader(io.StringIO(module.convert(
            json.dumps(web({"map": "L7", "x": 1., "y": 2., "yaw": 0.}, declare=False)),
            module.RMF_WEB_FORMAT))))[0]
        self.assertEqual(undeclared["rmf_position_basis"], "NOT_SELECTED_WITHOUT_CALLER_UNIT_DECLARATION")
        self.assertEqual(undeclared["rmf_map_x_m"], "")
        self.assertEqual(float(undeclared["battery_remaining_fraction"]), .4)
        for patch in ({"location_unit": "cm"}, {"yaw_unit": "deg"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                module.convert(json.dumps({**posed, **patch}), module.RMF_WEB_FORMAT)
        for pose in ({"map": "", "x": 1., "y": 2., "yaw": 0.}, {"x": 1., "y": 2., "yaw": 0.}, 5):
            with self.subTest(pose=pose), self.assertRaises(ValueError):
                module.convert(json.dumps(web(pose)), module.RMF_WEB_FORMAT)
        packet = "42" + json.dumps(["/fleets/authored-fleet/state", posed["state"]])
        socket = {k: v for k, v in posed.items() if k != "state"}
        socket.update(protocol_version=5, transport="websocket-complete-text",
                      direction="received", packet=packet)
        socket_row = list(csv.DictReader(io.StringIO(module.convert(
            json.dumps(socket), module.RMF_SOCKET_FORMAT))))[0]
        self.assertEqual(float(socket_row["rmf_map_x_m"]), -8.5)
        self.assertEqual(socket_row["rmf_position_basis"],
                         "CALLER_DECLARED_UNITS_IN_REPORTED_MAP_FRAME_NOT_GLOBAL_OR_GEODETIC")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "web.csv"
                path.write_text(module.convert(json.dumps(posed), module.RMF_WEB_FORMAT))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            self.assertEqual(common["observations"][0]["fields"]["rmf_map_y_m"], 3.25)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")

        def state(x, count, index_base):
            def point(i):
                return {"t": {"sec": 1, "nanosec": 0}, "x": float(i), "y": 0., "yaw": 0.,
                        "obey_approach_speed_limit": True, "approach_speed_limit": 1.,
                        "level_name": "L1", "index": index_base + i}
            return dict(schema_commit="6990fc7418c412234b4ff64f01560d8bf3047f46",
                capture_time_us=5000, location_unit="m", yaw_unit="rad",
                state=dict(name="amr-1", model="m", task_id="t", seq=index_base,
                    mode=dict(mode=2, mode_request_id=1, performing_action=""),
                    battery_percent=50, location=point(0),
                    path=[point(i) for i in range(1, count + 1)]))
        text = "\n".join(json.dumps(r) for r in (state(1., 2, 10), state(9., 3, 20)))
        rows = list(csv.DictReader(io.StringIO(module.convert(text, module.RMF_FORMAT))))
        self.assertEqual(len(rows), 7)  # 1 + 2 and 1 + 3
        self.assertEqual({row["record_time_us"] for row in rows}, {"5000"})
        self.assertEqual({row["rmf_name_hex"] for row in rows}, {rows[0]["rmf_name_hex"]})
        self.assertEqual([row["rmf_source_record_index"] for row in rows],
                         ["0", "0", "0", "1", "1", "1", "1"])
        first_sha = {row["rmf_snapshot_sha256"] for row in rows[:3]}
        second_sha = {row["rmf_snapshot_sha256"] for row in rows[3:]}
        self.assertEqual((len(first_sha), len(second_sha)), (1, 1))
        self.assertNotEqual(first_sha, second_sha)
        self.assertTrue(all(sha.startswith("sha256:") for sha in first_sha | second_sha))
        self.assertEqual([row["rmf_record_kind"] for row in rows],
                         ["STATE", "PATH_POINT", "PATH_POINT", "STATE", "PATH_POINT", "PATH_POINT", "PATH_POINT"])
        self.assertEqual([row["rmf_path_point_count"] for row in rows], ["2"] * 3 + ["3"] * 4)
        self.assertEqual(sum(1 for row in rows if row["source_record_hex"]), 2)
        import hashlib
        self.assertEqual(rows[0]["rmf_snapshot_sha256"],
                         "sha256:" + hashlib.sha256(text.splitlines()[0].encode()).hexdigest())

    def test_rmf_reported_location_and_planned_path_reach_common_output(self):
        """Reported map-local pose and the reported plan; units are the caller's declaration."""
        def waypoint(x, y, yaw, level, index, sec=5, nanosec=250000000, limit=1.5, obey=True):
            return {"t": {"sec": sec, "nanosec": nanosec}, "x": x, "y": y, "yaw": yaw,
                    "obey_approach_speed_limit": obey, "approach_speed_limit": limit,
                    "level_name": level, "index": index}

        def record(location, path, declare=True, **extra):
            envelope = dict(schema_commit="6990fc7418c412234b4ff64f01560d8bf3047f46",
                capture_time_us=1000,
                state=dict(name="tinyRobot1", model="tinyRobot", task_id="reported-task", seq=7,
                    mode=dict(mode=2, mode_request_id=3, performing_action=""),
                    battery_percent=82.5, location=location, path=list(path)))
            if declare:
                envelope.update(location_unit="m", yaw_unit="rad")
            envelope.update(extra)
            return envelope

        first = record(waypoint(1.5, -2.25, .5, "L1", 3),
                       [waypoint(4., 5., 1.25, "L1", 4),
                        waypoint(7.5, 8., -1.25, "L2", 5, sec=9, nanosec=0, limit=.75, obey=False),
                        waypoint(-3., 0., 0., "L2", 6, sec=12, nanosec=1000)])
        second = record(None, [])
        text = "\n".join(map(json.dumps, (first, second)))
        output = module.convert(text, module.RMF_FORMAT)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["rmf_record_kind"] for row in rows],
                         ["STATE", "PATH_POINT", "PATH_POINT", "PATH_POINT", "STATE"])
        state, points, empty = rows[0], rows[1:4], rows[4]
        self.assertEqual(float(state["battery_remaining_fraction"]), .825)
        self.assertEqual(state["rmf_mode_reported"], "MODE_MOVING")
        self.assertEqual(json.loads(bytes.fromhex(state["source_record_hex"][4:])), first)
        self.assertEqual((float(state["rmf_map_x_m"]), float(state["rmf_map_y_m"])), (1.5, -2.25))
        self.assertEqual(float(state["rmf_map_yaw_rad"]), .5)
        self.assertEqual(bytes.fromhex(state["rmf_map_name_hex"][4:]).decode(), "L1")
        self.assertEqual(state["rmf_position_basis"],
                         "CALLER_DECLARED_UNITS_IN_REPORTED_MAP_FRAME_NOT_GLOBAL_OR_GEODETIC")
        self.assertEqual(state["rmf_waypoint_index_text"], "u64:3")
        self.assertEqual(state["rmf_waypoint_time_us_reported"], "5250000")
        self.assertEqual(state["record_time_us"], "1000")
        self.assertIn("NOT_THE_ROW_CLOCK", state["rmf_waypoint_time_basis"])
        self.assertTrue(all(row["rmf_path_point_count"] == "3" for row in rows[:4]))
        self.assertEqual([row["rmf_path_point_index"] for row in points], ["0", "1", "2"])
        self.assertEqual([(float(row["rmf_map_x_m"]), float(row["rmf_map_y_m"])) for row in points],
                         [(4., 5.), (7.5, 8.), (-3., 0.)])
        self.assertEqual([bytes.fromhex(row["rmf_map_name_hex"][4:]).decode() for row in points],
                         ["L1", "L2", "L2"])
        self.assertEqual([row["rmf_waypoint_index_text"] for row in points],
                         ["u64:4", "u64:5", "u64:6"])
        self.assertEqual([row["rmf_waypoint_time_us_reported"] for row in points],
                         ["5250000", "9000000", "12000001"])
        self.assertTrue(all(row["rmf_name_hex"] == state["rmf_name_hex"] for row in points))
        for row in points:
            self.assertEqual((row["battery_remaining_fraction"], row["rmf_mode_reported"],
                              row["rmf_task_id_hex"], row["source_record_hex"]), ("", "", "", ""))
        self.assertEqual(float(points[0]["rmf_approach_speed_limit_m_s"]), 1.5)
        self.assertEqual(points[0]["rmf_approach_speed_limit_disposition"], "REPORTED_LANE_LIMIT_OBEYED")
        self.assertEqual(points[1]["rmf_approach_speed_limit_m_s"], "")
        self.assertEqual(points[1]["rmf_approach_speed_limit_disposition"],
                         "REPORTED_NOT_OBEYED_VALUE_RETAINED_IN_SOURCE")
        self.assertEqual((empty["rmf_map_x_m"], empty["rmf_map_y_m"], empty["rmf_map_yaw_rad"]), ("", "", ""))
        self.assertEqual(empty["rmf_position_basis"], "NOT_REPORTED")
        self.assertEqual(empty["rmf_path_point_count"], "0")
        self.assertEqual(float(empty["battery_remaining_fraction"]), .825)
        undeclared = list(csv.DictReader(io.StringIO(module.convert(
            json.dumps(record({"future": 1}, [waypoint(1., 2., 0., "L", 1)], declare=False)),
            module.RMF_FORMAT))))
        self.assertEqual(len(undeclared), 1)
        self.assertEqual(undeclared[0]["rmf_position_basis"],
                         "NOT_SELECTED_WITHOUT_CALLER_UNIT_DECLARATION")
        self.assertEqual(undeclared[0]["rmf_path_point_count"], "")
        self.assertEqual(float(undeclared[0]["battery_remaining_fraction"]), .825)
        with tempfile.TemporaryDirectory() as tmp:
            source, path = Path(tmp) / "input.jsonl", Path(tmp) / "output.csv"
            source.write_text(text)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(path), "--source-format", module.RMF_FORMAT],
                check=True, capture_output=True)
            self.assertEqual(path.read_text(), output)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 5)
                fields = common["observations"][1]["fields"]
                self.assertEqual((fields["rmf_map_x_m"], fields["rmf_map_y_m"]), (4., 5.))
                self.assertEqual(fields["rmf_path_point_index"], 0)
                self.assertEqual(fields["rmf_path_point_count"], 3)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                declared = common["profile_units"]
                for column in module.RMF_LOCATION_FIELDS:
                    self.assertTrue(declared.get(column), f"{column} must declare its meaning")
                self.assertIn("callers_declaration", declared["rmf_map_x_m"])
                self.assertIn("never_a_global_or_geodetic_coordinate", declared["rmf_map_x_m"])
                self.assertIn("NOT_ESTABLISHED_BY_THE_PIN", declared["rmf_waypoint_time_basis"])
                self.assertIn("not_proven_travel_sequence", declared["rmf_path_point_index"])
        def plan(count):
            return record(waypoint(0., 0., 0., "L1", 0),
                          [waypoint(float(i), float(-i), 0., "L1", i) for i in range(1, count + 1)])
        large = plan(1000)
        self.assertLess(len(json.dumps(large).encode()), 512 * 1024)
        previous_limit = csv.field_size_limit(2 * 1024 * 1024)
        self.addCleanup(csv.field_size_limit, previous_limit)
        big = list(csv.DictReader(io.StringIO(module.convert(json.dumps(large), module.RMF_FORMAT))))
        self.assertEqual(len(big), 1001)
        self.assertEqual(big[0]["rmf_path_point_count"], "1000")
        self.assertEqual([int(row["rmf_path_point_index"]) for row in big[1:]], list(range(1000)))
        self.assertEqual((float(big[-1]["rmf_map_x_m"]), float(big[-1]["rmf_map_y_m"])), (1000., -1000.))
        self.assertEqual(big[-1]["rmf_waypoint_index_text"], "u64:1000")
        self.assertEqual(sum(1 for row in big if row["source_record_hex"]), 1)
        with self.assertRaises(ValueError) as caught:
            module.convert(json.dumps(plan(3500)), module.RMF_FORMAT)
        self.assertEqual(str(caught.exception), "empty or oversized JSON record")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "large.csv"
                path.write_text(module.convert(json.dumps(large), module.RMF_FORMAT))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1001)
            self.assertEqual(common["observations"][-1]["fields"]["rmf_path_point_index"], 999)
            self.assertEqual(common["observations"][-1]["fields"]["rmf_map_x_m"], 1000.)

        for label, patch in (("wrong declared unit", {"location_unit": "mm"}),
                             ("wrong yaw declaration", {"yaw_unit": "deg"})):
            with self.subTest(case=label), self.assertRaises(ValueError):
                module.convert(json.dumps({**first, **patch}), module.RMF_FORMAT)
        for reported in (99., -12.5, 6.5):
            kept = list(csv.DictReader(io.StringIO(module.convert(
                json.dumps(record(waypoint(1., 2., reported, "L", 1), [])), module.RMF_FORMAT))))[0]
            self.assertEqual(float(kept["rmf_map_yaw_rad"]), reported)
            self.assertEqual(kept["rmf_position_basis"],
                             "CALLER_DECLARED_UNITS_IN_REPORTED_MAP_FRAME_NOT_GLOBAL_OR_GEODETIC")
        for label, location in (
                ("non-object waypoint time", dict(waypoint(1., 2., 0., "L", 1), t=5)),
                ("nanosecond out of range", waypoint(1., 2., 0., "L", 1, nanosec=10**9)),
                ("negative speed limit", waypoint(1., 2., 0., "L", 1, limit=-1.)),
                ("non-string level name", dict(waypoint(1., 2., 0., "L", 1), level_name=1)),
                ("waypoint index too large", dict(waypoint(1., 2., 0., "L", 1), index=2**64)),
                ("non-boolean limit flag", dict(waypoint(1., 2., 0., "L", 1),
                                                obey_approach_speed_limit="yes"))):
            with self.subTest(case=label), self.assertRaises(ValueError):
                module.convert(json.dumps(record(location, [])), module.RMF_FORMAT)
        for label, path in (("non-object path entry", [42]), ("path is not an array", {"a": 1})):
            with self.subTest(case=label), self.assertRaises(ValueError):
                module.convert(json.dumps(record(waypoint(1., 2., 0., "L", 1), [])
                                          | {"state": dict(record(waypoint(1., 2., 0., "L", 1), [])["state"],
                                                           path=path)}), module.RMF_FORMAT)

    def test_rmf_reported_state_reuses_json_common_output(self):
        def record(percent, mode, seq):
            return dict(schema_commit="6990fc7418c412234b4ff64f01560d8bf3047f46", capture_time_us=1000,
                state=dict(name="robot,一", model="example", task_id="reported-task", seq=seq,
                    mode=dict(mode=mode, mode_request_id=2**64-1, performing_action="reported action"),
                    battery_percent=percent, location={"future": 1}, path=[]), future=[1, 2])
        first, second = record(75, 11, 2**64-1), record(0, 123, 0)
        text = "\n".join(map(json.dumps, (first, second)))
        output = module.convert(text, "rmf-robot-state-6990fc74")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]["battery_remaining_fraction"]), .75)
        self.assertEqual(float(rows[1]["battery_remaining_fraction"]), 0)
        self.assertEqual(rows[0]["rmf_sequence_reported_text"], "u64:18446744073709551615")
        self.assertEqual(rows[0]["rmf_mode_reported"], "MODE_ACTION_COMPLETED")
        self.assertEqual(rows[1]["rmf_mode_reported"], "UNKNOWN")
        self.assertEqual(rows[1]["rmf_mode_number_reported"], "123")
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), first)
        for mode in range(12):
            selected = list(csv.DictReader(io.StringIO(module.convert(json.dumps(record(100, mode, mode)),
                "rmf-robot-state-6990fc74"))))[0]
            self.assertNotEqual(selected["rmf_mode_reported"], "UNKNOWN")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.csv"
            source = Path(tmp) / "input.jsonl"
            source.write_text(text)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(path), "--source-format", "rmf-robot-state-6990fc74"], check=True, capture_output=True)
            self.assertEqual(path.read_text(), output)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][0]["fields"]["battery_remaining_fraction"], .75)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for key, value in (("battery_percent", -1), ("battery_percent", 101), ("battery_percent", True),
                           ("seq", 2**64), ("seq", 1.5), ("name", ""), ("mode", {"mode": -1})):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(dict(first, state=dict(first["state"], **{key: value}))), "rmf-robot-state-6990fc74")
        for key, value in (("schema_commit", "different"), ("capture_time_us", True)):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(first, **{key: value})), "rmf-robot-state-6990fc74")
        with self.assertRaises(ValueError):
            module.convert(json.dumps(first).replace('"battery_percent": 75', '"battery_percent": 75, "battery_percent": 50'),
                           "rmf-robot-state-6990fc74")

    def test_container_state_and_runtime_columns_all_carry_declared_meaning(self):
        """Emitted is not the same as declared: every one of these columns must say what it is.

        These reached common output with names and no `[units]` entry, so a reader saw a value with
        no meaning. Declared here from the pins the case notes already record; counts and codes stay
        counts and codes rather than being given a unit because of their names.
        """
        import re
        profile = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"
        declared = dict(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*) = \"(.*)\"$",
                                   profile.read_text(), re.M))
        hailo = ("runtime_name_hex", "runtime_status_reported", "runtime_fps_reported",
                 "runtime_frame_count_text", "runtime_hw_latency_s", "runtime_overall_latency_s",
                 "runtime_time_basis", "runtime_frame_count_basis")
        for field in module.DOCKER_STATE_FIELDS + hailo:
            with self.subTest(field=field):
                self.assertTrue(declared.get(field), f"{field} reaches common output undeclared")
        self.assertIn("never_an_independent_diagnosis", declared["container_oom_killed_reported"])
        self.assertIn("not_evidence_of_a_completed_successful_run",
                      declared["container_exit_code_reported"])
        self.assertIn("not_necessarily_the_programs_own_stderr",
                      declared["container_last_lifecycle_error_hex"])
        self.assertIn("explicitly_NOT_healthy", declared["container_health_disposition"])
        self.assertIn("never_an_identity", declared["container_name_hex"])
        self.assertIn("deliberately_unresolved", declared["container_state_basis"])
        self.assertIn("is_a_COUNT_and_not_a_quantity_with_a_unit",
                      declared["runtime_frame_count_text"])
        self.assertIn("not_a_completeness_measure", declared["runtime_frame_count_basis"])
        for field in ("runtime_hw_latency_s", "runtime_overall_latency_s"):
            self.assertTrue(declared[field].startswith("s_"), field)
            self.assertIn("mean_across_networks", declared[field])
        self.assertIn("not_a_capture_offset", declared["runtime_hw_latency_s"])
        self.assertIn("non_timeline_rules", declared["runtime_overall_latency_s"])
        self.assertIn("not_a_system_throughput", declared["runtime_fps_reported"])
        self.assertIn("never_asserts_one_producers_window_for_another",
                      declared["runtime_time_basis"])

    def test_docker_state_reports_reuse_without_crash_inference(self):
        def record(status='paused', exit_code=0, health=None):
            state = dict(Status=status, Running=status in ('paused', 'running', 'restarting'),
                Paused=status == 'paused', Restarting=status == 'restarting', OOMKilled=status == 'exited',
                Dead=False, ExitCode=exit_code, Error='', StartedAt='0001-01-01T00:00:00Z')
            if health is not None: state['Health'] = health
            return dict(daemon_version='27.3.1', capture_time_us=1,
                        inspect=dict(Id='example', State=state, Future={'keep': True}))
        items = [record(), record('exited', 137, dict(Status='unhealthy', FailingStreak=3)),
                 record('restarting', 1), record('future', -1, dict(Status='future', FailingStreak=0))]
        rows = list(csv.DictReader(io.StringIO(module.convert(
            '\n'.join(json.dumps(item) for item in items), module.DOCKER_STATE_FORMAT))))
        self.assertEqual(rows[0]['container_running_reported'], 'True')
        self.assertEqual(rows[0]['container_health_disposition'], 'NOT_PROVIDED')
        self.assertEqual(rows[1]['container_exit_code_reported'], '137')
        self.assertEqual(rows[1]['container_oom_killed_reported'], 'True')
        self.assertEqual(rows[2]['container_running_reported'], 'True')
        self.assertEqual(rows[3]['container_status_disposition'], 'UNKNOWN_RETAINED')
        self.assertEqual(rows[3]['container_health_disposition'], 'UNKNOWN_RETAINED')
        self.assertEqual(rows[0]['container_name_hex'], '')
        named = record(); named['inspect']['Name'] = '/web'
        row = next(csv.DictReader(io.StringIO(module.convert(json.dumps(named), module.DOCKER_STATE_FORMAT))))
        self.assertEqual(bytes.fromhex(row['container_name_hex'][4:]).decode(), '/web')
        self.assertEqual(len(row['container_id_hex']) - 4, len('example') * 2)
        for bad in (1, '', True, ['/web']):
            item = record(); item['inspect']['Name'] = bad
            with self.assertRaises(ValueError): module.convert(json.dumps(item), module.DOCKER_STATE_FORMAT)
        self.assertEqual(json.loads(bytes.fromhex(rows[1]['source_record_hex'][4:])), items[1])
        for key, bad in [('Running', 1), ('ExitCode', True), ('ExitCode', 2**63), ('Status', ''),
                         ('Health', {'Status': 'healthy', 'FailingStreak': -1})]:
            item = record(); item['inspect']['State'][key] = bad
            with self.assertRaises(ValueError): module.convert(json.dumps(item), module.DOCKER_STATE_FORMAT)
        item = record(); item['daemon_version'] = 'other'
        with self.assertRaises(ValueError): module.convert(json.dumps(item), module.DOCKER_STATE_FORMAT)
        item = record(); item['inspect'] = [item['inspect']]
        with self.assertRaises(ValueError): module.convert(json.dumps(item), module.DOCKER_STATE_FORMAT)
        large = record(); large['inspect']['Future']['description'] = 'x' * 40000
        self.assertGreater(len(json.dumps(large)), 30000)
        material = [large] + [record('running') for _ in range(999)]
        output = module.convert('\n'.join(json.dumps(item) for item in material), module.DOCKER_STATE_FORMAT)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 1000)
        if os.environ.get('MUSUBI_TELEMETRY_READER'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.csv'; path.write_text(output)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/electrical-schema-reuse/json-profile.toml'),
                    str(path), '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 1000)
                self.assertEqual(common['observations'][-1]['fields']['container_status_reported'], 'running')
                self.assertEqual(common['observations'][0]['clock_basis'], 'Unknown')

    def test_docker_display_units_missing_and_common_output(self):
        first = dict(exporter_version="27.3.1", os_type="linux", capture_time_us=1000,
            stats=dict(ID="example-a", CPUPerc="150.00%", MemPerc="25.00%", MemUsage="1MiB / 4MiB",
                       NetIO="1kB / 2MB", BlockIO="0B / 1.5GB", PIDs="18446744073709551615"), future=[1, 2])
        second = dict(exporter_version="27.3.1", os_type="linux", capture_time_us=2000,
            stats=dict(ID="example-b", CPUPerc="0.00%", MemPerc="50.00%", MemUsage="512KiB / 1MiB",
                       NetIO="2.5kB / 0B", BlockIO="1e+03B / 0B", PIDs="3"))
        missing = dict(first, capture_time_us=3000, stats=dict(ID="example-a", CPUPerc="--", MemPerc="--",
            MemUsage="-- / --", NetIO="--", BlockIO="--", PIDs="--"))
        text = "\n".join(map(json.dumps, (first, second, missing)))
        output = module.convert(text, module.DOCKER_FORMAT)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 3)
        self.assertEqual(float(rows[0]["docker_cpu_equivalents_reported"]), 1.5)
        self.assertEqual(float(rows[0]["docker_memory_used_approx_bytes"]), 1048576)
        self.assertEqual(float(rows[1]["docker_memory_used_approx_bytes"]), 524288)
        self.assertEqual(float(rows[0]["docker_network_tx_approx_bytes"]), 2000000)
        self.assertEqual(float(rows[1]["docker_block_read_approx_bytes"]), 1000)
        self.assertEqual(rows[0]["docker_pids_reported_text"], "u64:18446744073709551615")
        self.assertEqual(rows[0]["docker_name_reported_hex"], "")
        named = dict(first, stats=dict(first["stats"], Container="web", Name="web"))
        unnamed = dict(first, stats=dict(first["stats"], Name="--"))
        both = list(csv.DictReader(io.StringIO(module.convert(
            "\n".join(map(json.dumps, (named, unnamed))), module.DOCKER_FORMAT))))
        self.assertEqual(bytes.fromhex(both[0]["docker_name_reported_hex"][4:]).decode(), "web")
        self.assertEqual(both[1]["docker_name_reported_hex"], "")  # `--` は名前ではなく unavailable
        self.assertNotIn("docker_container_hex", both[0])  # Container は raw のまま
        self.assertEqual(json.loads(bytes.fromhex(both[0]["source_record_hex"][4:]))["stats"]["Container"], "web")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "named.csv"
                path.write_text(module.convert("\n".join(map(json.dumps, (named, unnamed))), module.DOCKER_FORMAT))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(
                    bytes.fromhex(common["observations"][0]["fields"]["docker_name_reported_hex"][4:]).decode(),
                    "web")
                self.assertIsNone(common["observations"][1]["fields"]["docker_name_reported_hex"])
        else:
            print("Docker reported-name common-reader integration NOT_RUN: executable not configured")
        for bad in (1, "", True, ["web"]):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(dict(first, stats=dict(first["stats"], Name=bad))), module.DOCKER_FORMAT)
        self.assertEqual(rows[2]["docker_cpu_equivalents_reported"], "")
        self.assertEqual(rows[2]["docker_pids_reported_text"], "UNAVAILABLE")
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), first)
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "input.jsonl", Path(tmp) / "output.csv"
            source.write_text(text)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(target), "--source-format", module.DOCKER_FORMAT], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(target)], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                self.assertEqual(common["observations"][0]["fields"]["docker_cpu_equivalents_reported"], 1.5)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for key, value in (("CPUPerc", True), ("CPUPerc", "NaN%"), ("MemUsage", "1MB / 2MB"),
                           ("NetIO", "1KiB / 2KiB"), ("BlockIO", "1e999B / 0B"),
                           ("PIDs", "18446744073709551616"), ("ID", "")):
            bad = dict(first, stats=dict(first["stats"], **{key: value}))
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.DOCKER_FORMAT)
        for key, value in (("exporter_version", "27.3.0"), ("os_type", "windows"), ("capture_time_us", True)):
            with self.assertRaises(ValueError): module.convert(json.dumps(dict(first, **{key: value})), module.DOCKER_FORMAT)
        with self.assertRaises(ValueError): module.convert(json.dumps(first["stats"]), module.DOCKER_FORMAT)

    def test_factsheet_declared_operating_quantities_reuse_common_output(self):
        def example(scale):
            return dict(timestamp="2026-01-01T00:00:00.123456789Z", version="3.0.0",
                manufacturer="example", serialNumber="declared-" + str(scale),
                physicalParameters=dict(minimumSpeed=.1, maximumSpeed=2 * scale,
                    maximumAcceleration=.5 * scale, maximumDeceleration=-.7 * scale,
                    minimumHeight=.2, maximumHeight=1 * scale, width=.8 * scale, length=1.2 * scale),
                typeSpecification=dict(maximumLoadMass=100 * scale), future={"retained": "x" * 31000})
        first, second = example(1), example(2)
        second["physicalParameters"].update(minimumAngularSpeed=.2, maximumAngularSpeed=.8)
        text = "\n".join(map(json.dumps, (first, second)))
        rows = list(csv.DictReader(io.StringIO(module.convert(text, module.FACTSHEET_FORMAT))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[1]["declared_maximum_load_mass_kg"]), 200.)
        self.assertEqual(float(rows[1]["declared_maximum_deceleration_m_s2"]), -1.4)
        self.assertEqual(rows[0]["declared_minimum_angular_speed_rad_s"], "")
        self.assertEqual(float(rows[1]["declared_maximum_angular_speed_rad_s"]), .8)
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), first)
        self.assertNotIn("vehicle_vx_m_s", rows[0])
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "input.jsonl", Path(tmp) / "output.csv"
            source.write_text(text)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(output), "--source-format", module.FACTSHEET_FORMAT], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(output), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][1]["fields"]["declared_width_m"], 1.6)
                self.assertEqual(common["observations"][0]["fields"]["factsheet_basis"], "DECLARED_NOT_MEASURED_OR_CERTIFIED")
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for key, value in (("maximumSpeed", -1), ("minimumSpeed", 3), ("width", True),
                           ("length", None), ("maximumAcceleration", "1"), ("maximumHeight", .1)):
            bad = example(1); bad["physicalParameters"][key] = value
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.FACTSHEET_FORMAT)
        for key, value in (("version", "2.1.0"), ("timestamp", "bad"), ("serialNumber", ""),
                           ("physicalParameters", {}), ("typeSpecification", {"maximumLoadMass": -1})):
            bad = example(1); bad[key] = value
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.FACTSHEET_FORMAT)
        with self.assertRaises(ValueError): module.convert(json.dumps(vda("3.0.0")), module.FACTSHEET_FORMAT)
        with self.assertRaises(ValueError): module.convert(json.dumps(first), module.FACTSHEET_FORMAT, vda_actions=True)
        with self.assertRaises(ValueError): module.convert(json.dumps(first), "vda-state-3.0")

    def test_robotnik_schema_pair_minutes_cells_and_connection_reuse_common_output(self):
        commits = ("5c44131ba354962322e969a4b0fe0b3026659686", "012c20a589c0c180dd792f41d46bd84b7391b8ed")
        examples = [dict(schema_commit=commit, capture_time_us=1000, future={"unknown": [1, 2]},
            battery=dict(voltage=24 + index, current=-2., level=50., time_remaining=3 + index,
                         time_charging=2, is_charging=True, cell_voltages=[12., 12.5]))
                    for index, commit in enumerate(commits)]
        output = module.convert("\n".join(map(json.dumps, examples)), module.ROBOTNIK_FORMAT)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 6)
        self.assertEqual([row["robotnik_row_kind"] for row in rows], ["SUMMARY", "CELL", "CELL"] * 2)
        self.assertEqual(rows[0]["battery_time_remaining_reported_s"], "180")
        self.assertEqual(rows[3]["battery_time_remaining_reported_s"], "240")
        self.assertEqual(rows[0]["battery_reported_charging_state"], "UNKNOWN")
        self.assertEqual(rows[0]["battery_current_sign_basis"], "UNSPECIFIED")
        self.assertEqual(rows[0]["battery_connection_reported"], "1")
        self.assertEqual(float(rows[2]["battery_cell_voltage_v"]), 12.5)
        self.assertEqual(rows[2]["battery_voltage_v"], "")
        self.assertEqual(json.loads(bytes.fromhex(rows[4]["source_record_hex"][4:])), examples[1])
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.jsonl"; source.write_text("\n".join(map(json.dumps, examples)))
            path = Path(tmp) / "out.csv"
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_ardupilot_battery_csv.py"),
                str(source), str(path), "--source-format", module.ROBOTNIK_FORMAT], check=True, capture_output=True)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 6)
                self.assertEqual(common["observations"][3]["fields"]["battery_power_w"], -50.)
                self.assertEqual(common["observations"][5]["fields"]["battery_cell_voltage_v"], 12.5)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for key, value in [("voltage", -1), ("level", 101), ("current", True), ("time_remaining", -1),
                           ("time_charging", 2**32), ("is_charging", 1), ("cell_voltages", [-1]),
                           ("cell_voltages", [float("nan")])]:
            bad = json.loads(json.dumps(examples[0])); bad["battery"][key] = value
            with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.ROBOTNIK_FORMAT)
        bad = dict(examples[0], schema_commit="unknown")
        with self.assertRaises(ValueError): module.convert(json.dumps(bad), module.ROBOTNIK_FORMAT)

    def test_qualified_missing_voltage_is_opt_in_and_not_zero_power(self):
        missing = ros()
        missing.update(voltage="NaN", current="NaN", charge="NaN", percentage="NaN", power_supply_status=0)
        text = json.dumps(missing) + "\n" + json.dumps(ros())
        with self.assertRaises(ValueError): module.convert(text, "ros-battery-json")
        output = module.convert(text, "ros-battery-json", allow_missing_voltage=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]["battery_voltage_disposition"], "UNAVAILABLE_SOURCE_NAN")
        self.assertEqual(rows[0]["battery_voltage_v"], "")
        self.assertEqual(rows[0]["battery_power_w"], "")
        self.assertEqual(rows[1]["battery_voltage_disposition"], "REPORTED")
        self.assertEqual(float(rows[1]["battery_power_w"]), -48.)
        for bad in ("Infinity", "-Infinity", None, True, -1):
            item = dict(missing, voltage=bad)
            with self.assertRaises(ValueError): module.convert(json.dumps(item), "ros-battery-json", allow_missing_voltage=True)
        with self.assertRaises(ValueError): module.convert(json.dumps(vda("3.0.0")), "vda-state-3.0", allow_missing_voltage=True)

    def test_vda_error_details_reuse_source_binding_and_common_output(self):
        previous_limit = csv.field_size_limit(2 * 1024 * 1024)
        try:
            for version, name in (("2.1.0", "node-A"), ("3.0.0", "action-B")):
                record = vda(version)
                record["errors"] = [{"errorType": "reported-error", "errorLevel": "URGENT",
                    "errorDescription": "reported, description", "errorHint": "not a command",
                    "errorReferences": [{"referenceKey": "reportedId", "referenceValue": name},
                                        {"referenceKey": "reportedId", "referenceValue": name}]}]
                record["actionStates"] = [{"actionId": "a", "actionStatus": "RUNNING"}]
                fmt = "vda-state-" + version[:3]
                output = module.convert(json.dumps(record), fmt, vda_errors=True, vda_actions=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([r["vda_record_kind"] for r in rows], ["SUMMARY", "ACTION", "ERROR", "ERROR_REFERENCE", "ERROR_REFERENCE"])
                self.assertEqual(rows[2]["vda_error_level_reported"], "UNKNOWN" if version == "2.1.0" else "URGENT")
                self.assertEqual(rows[2]["vda_error_reference_count"], "2")
                self.assertEqual(rows[3]["vda_error_reference_value_hex"], "hex:" + name.encode().hex())
                self.assertEqual(rows[4]["vda_error_reference_index"], "1")
                self.assertTrue(all(not row["source_record_hex"] for row in rows[1:]))
                self.assertTrue(all(row["vda_source_sha256"] == rows[0]["vda_source_sha256"] for row in rows))
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "errors.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                        self.assertEqual(len(common["observations"]), 5)
                        self.assertEqual(common["observations"][3]["fields"]["vda_error_reference_value_hex"], "hex:" + name.encode().hex())
                record["errors"] = [{"errorType": "same-type", "errorLevel": "WARNING"} for _ in range(1000)]
                many = list(csv.DictReader(io.StringIO(module.convert(json.dumps(record), fmt, vda_errors=True))))
                self.assertEqual(len(many), 1001)
                self.assertEqual(many[-1]["vda_error_index"], "999")
                self.assertEqual(many[-1]["vda_error_reference_count"], "")
        finally:
            csv.field_size_limit(previous_limit)

    def test_vda_error_details_absent_empty_and_invalid(self):
        fmt = "vda-state-3.0"
        def rows(record):
            return list(csv.DictReader(io.StringIO(module.convert(json.dumps(record), fmt, vda_errors=True))))
        record = vda("3.0.0")
        self.assertEqual(rows(record)[0]["vda_error_warning_count"], "")
        self.assertEqual(rows({**record, "errors": []})[0]["vda_error_warning_count"], "0")
        error = {"errorType": "x", "errorLevel": "FUTURE", "errorReferences": []}
        self.assertEqual(rows({**record, "errors": [error]})[1]["vda_error_reference_count"], "0")
        for invalid in ({**error, "errorHint": 1}, {**error, "errorReferences": None},
                        {**error, "errorReferences": [{}]},
                        {**error, "errorReferences": [{"referenceKey": "x", "referenceValue": 1}]}):
            with self.assertRaises(ValueError): rows({**record, "errors": [invalid]})
        with self.assertRaises(ValueError): module.convert(json.dumps(vda("2.0.0")), "vda-state-2.0", vda_errors=True)

    def test_individual_vda_actions_reuse_and_common_output(self):
        import hashlib
        previous_limit = csv.field_size_limit(2 * 1024 * 1024)
        self.addCleanup(csv.field_size_limit, previous_limit)
        for version, count in (("2.0.0", 2), ("2.1.0", 3), ("3.0.0", 1000)):
            old = version != "3.0.0"
            fmt = "vda-state-" + version[:3]
            record = vda(version)
            result_key = "resultDescription" if old else "actionResult"
            description_key = "actionDescription" if old else "actionDescriptor"
            record["actionStates"] = [dict(actionId=f"action-{i}", actionStatus="FINISHED",
                actionType="read", future={"unknown": i},
                **{result_key: f'result,{i}', description_key: 'description'}) for i in range(count)]
            record["actionStates"][-1]["actionStatus"] = "FUTURE_STATE"
            raw = json.dumps(record, ensure_ascii=False)
            converted = module.convert(raw, fmt, vda_actions=True)
            rows = list(csv.DictReader(io.StringIO(converted)))
            self.assertEqual(len(rows), count + 1)
            self.assertEqual(rows[0]["vda_record_kind"], "SUMMARY")
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), raw)
            for i, row in enumerate(rows[1:]):
                self.assertEqual(row["source_record_hex"], "")
                self.assertEqual(row["vda_source_sha256"], "sha256:" + hashlib.sha256(raw.encode()).hexdigest())
                self.assertEqual(row["vda_action_index"], str(i))
                self.assertEqual(bytes.fromhex(row["vda_action_id_hex"][4:]).decode(), f"action-{i}")
                self.assertEqual(bytes.fromhex(row["vda_action_result_hex"][4:]).decode(), f"result,{i}")
                self.assertEqual(row["vda_source_record_index"], "0")
            self.assertEqual(rows[-1]["vda_action_status_reported"], "UNKNOWN")
            self.assertEqual(bytes.fromhex(rows[-1]["vda_action_status_raw_hex"][4:]).decode(), "FUTURE_STATE")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "actions.csv"
                    path.write_text(converted)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], count + 1)
                    self.assertEqual(len(common["observations"]), count + 1)
                    self.assertEqual(common["observations"][-1]["fields"]["vda_action_status_reported"], "UNKNOWN")

    def test_individual_vda_actions_missing_wrong_types_and_legacy(self):
        record = vda("3.0.0")
        fmt = "vda-state-3.0"
        def rows(value):
            return list(csv.DictReader(io.StringIO(module.convert(json.dumps(value), fmt, vda_actions=True))))
        self.assertEqual(rows(record)[0]["vda_action_finished_count"], "")
        record["actionStates"] = []
        self.assertEqual(rows(record)[0]["vda_action_finished_count"], "0")
        action = {"actionId": "a", "actionStatus": "FINISHED", "resultDescription": "old-only"}
        record["actionStates"] = [action]
        self.assertEqual(rows(record)[1]["vda_action_result_hex"], "")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(module.convert(json.dumps(record), fmt))))), 1)
        for actions in ([action, action], [{**action, "actionResult": 1}],
                        [{**action, "actionType": None}], [{**action, "actionStatus": 1}]):
            with self.assertRaises(ValueError):
                rows({**record, "actionStates": actions})
        with self.assertRaises(ValueError):
            module.convert("", "ardupilot-bat", vda_actions=True)
        with self.assertRaises(ValueError):
            rows({**record, "large": "x" * (512 * 1024)})

    def test_vda_position_version_boundaries_and_common_map_output(self):
        for version in ("2.0.0", "2.1.0", "3.0.0"):
            old = version != "3.0.0"
            fmt = "vda-state-" + version[:3]
            key, state = ("agvPosition", "positionInitialized") if old else ("mobileRobotPosition", "localized")
            for x, map_id in ((-2., "floor-a"), (7., 'floor,"b')):
                record = vda(version)
                record[key] = dict(x=x, y=3., theta=-.5, mapId=map_id, localizationScore=.8,
                                   deviationRange=.2, future="retained", **{state: True})
                row = self.rows(record, fmt)
                self.assertEqual(float(row["vda_map_x_m"]), x)
                self.assertEqual(float(row["vda_map_theta_rad"]), -.5)
                self.assertEqual(bytes.fromhex(row["vda_map_id_hex"][4:]).decode(), map_id)
                self.assertEqual(row["vda_position_state"], "INITIALIZED_REPORTED" if old else "LOCALIZED_REPORTED")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "out.csv"
                        path.write_text(module.convert(json.dumps(record) + "\n", fmt))
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(path)], check=True, capture_output=True).stdout)
                        self.assertEqual(common["observations"][0]["fields"]["vda_map_x_m"], x)
                for unavailable in ("state", "score"):
                    record[key][state] = unavailable != "state"
                    record[key]["localizationScore"] = 0 if unavailable == "score" else .8
                    missing = self.rows(record, fmt)
                    self.assertEqual(missing["vda_map_x_m"], "")
                    self.assertEqual(missing["vda_position_state"], "UNAVAILABLE_POSITION_REPORTED")
                for bad_key, bad_value in (("theta", 4), (state, 1), ("mapId", ""), ("deviationRange", -1)):
                    original = record[key][bad_key]; record[key][bad_key] = bad_value
                    with self.assertRaises(ValueError): self.rows(record, fmt)
                    record[key][bad_key] = original
                record["mobileRobotPosition" if old else "agvPosition"] = record[key]
                with self.assertRaises(ValueError): self.rows(record, fmt)

    def test_reported_battery_temperature_reaches_common_output_with_its_reason(self):
        row = self.rows({**ros(), "temperature": 21.5}, "ros-battery-json")
        self.assertAlmostEqual(float(row["battery_temperature_k"]), 294.65)
        self.assertEqual(row["battery_temperature_basis"], "REPORTED")
        zero = self.rows({**ros(), "temperature": 0}, "ros-battery-json")
        self.assertAlmostEqual(float(zero["battery_temperature_k"]), 273.15)
        self.assertEqual(zero["battery_temperature_basis"], "REPORTED")
        for record, reason in (
            ({**ros(), "temperature": "NaN"}, "UNAVAILABLE_SOURCE_NAN"),
            (ros(), "ABSENT_FROM_SOURCE_RECORD"),
            ({**ros(), "temperature": 21.5, "present": False}, "BATTERY_NOT_PRESENT"),
        ):
            row = self.rows(record, "ros-battery-json")
            self.assertEqual(row["battery_temperature_k"], "", reason)
            self.assertEqual(row["battery_temperature_basis"], reason)
        for fmt, version in (("vda-state-2.1", "2.1.0"), ("vda-state-3.0", "3.0.0")):
            row = self.rows(vda(version), fmt)
            self.assertEqual(row["battery_temperature_k"], "")
            self.assertEqual(row["battery_temperature_basis"], "NOT_PROVIDED_BY_SELECTED_SCHEMA")
        for bad in (-273.16, -500):
            with self.assertRaises(ValueError):
                self.rows({**ros(), "temperature": bad}, "ros-battery-json")
        for bad in ("warm", None, True):
            with self.assertRaises(ValueError):
                self.rows({**ros(), "temperature": bad}, "ros-battery-json")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "temperature.csv"
                source.write_text("".join(module.convert(json.dumps(record), "ros-battery-json")
                                          for record in ({**ros(), "temperature": 21.5},)))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
            fields = common["observations"][0]["fields"]
            self.assertAlmostEqual(fields["battery_temperature_k"], 294.65)
            self.assertEqual(fields["battery_temperature_basis"], "REPORTED")
            units = common["profile_units"]
            self.assertTrue(units["battery_temperature_k"].startswith("K_converted_from"))
            self.assertIn("no_measurement_location_claimed", units["battery_temperature_k"])
            self.assertIn("NOT_PROVIDED_BY_SELECTED_SCHEMA", units["battery_temperature_basis"])

    def test_current_sign_basis_does_not_infer_direction_from_status(self):
        record = ros()
        record["power_supply_status"] = 1
        row = self.rows(record, "ros-battery-json")
        self.assertEqual(row["battery_current_sign_basis"], "NEGATIVE_DISCHARGING")
        self.assertEqual(float(row["battery_current_a"]), -2)
        self.assertEqual(float(row["battery_power_w"]), -48)
        self.assertEqual(row["battery_reported_charging_state"], "CHARGING")
        for charging in (False, True):
            record = vda("3.0.0")
            record["powerSupply"].update(batteryCurrent=-2, charging=charging)
            row = self.rows(record, "vda-state-3.0")
            self.assertEqual(row["battery_current_sign_basis"], "UNSPECIFIED")
            self.assertEqual(float(row["battery_current_a"]), -2)
        self.assertEqual(self.rows(vda("2.1.0"), "vda-state-2.1")["battery_current_sign_basis"], "UNSPECIFIED")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "signed.csv"
                source.write_text(module.convert(json.dumps(ros()), "ros-battery-json"))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["battery_current_a"], -2)
            self.assertEqual(fields["battery_current_sign_basis"], "NEGATIVE_DISCHARGING")
            units = common["profile_units"]
            self.assertIn("negative_is_discharging", units["battery_current_sign_basis"])
            self.assertIn("VDA5050_declares_none", units["battery_current_sign_basis"])
            self.assertIn("never_a_single_global_convention", units["battery_current_sign_basis"])
            self.assertIn("explicit_sign_basis", units["battery_current_a"])

    def test_vda_reported_states_version_boundaries_and_vehicle_units(self):
        for version, fmt in [("2.0.0", "vda-state-2.0"), ("2.1.0", "vda-state-2.1"), ("3.0.0", "vda-state-3.0")]:
            old = version in ("2.0.0", "2.1.0")
            record = vda(version)
            record.update(operatingMode="TEACHIN" if old else "TEACH_IN", driving=True, paused=False,
                safetyState={"eStop" if old else "activeEmergencyStop": "MANUAL", "fieldViolation": True},
                velocity={"vx": -2, "vy": .5, "omega": -.2}, orderId='order,quoted"', orderUpdateId=4,
                lastNodeId="node-2", lastNodeSequenceId=2,
                actionStates=[{"actionId": "a1", "actionStatus": "RUNNING"},
                              {"actionId": "a2", "actionStatus": "RETRIABLE"},
                              {"actionId": "a3", "actionStatus": "PAUSED"}],
                errors=[{"errorType": "x", "errorLevel": "URGENT"}])
            row = self.rows(record, fmt)
            self.assertEqual(row["vda_operating_mode_reported"], "TEACH_IN")
            self.assertEqual(row["vda_driving_reported"], "1")
            self.assertEqual(row["vda_paused_reported"], "0")
            self.assertEqual(row["vda_emergency_stop_reported"], "MANUAL")
            self.assertEqual(float(row["vehicle_vx_m_s"]), -2.)
            self.assertEqual(float(row["vehicle_yaw_rate_rad_s"]), -.2)
            self.assertEqual(bytes.fromhex(row["vda_order_id_hex"][4:]).decode(), record["orderId"])
            self.assertEqual(row["vda_action_running_count"], "1")
            self.assertEqual(row["vda_action_paused_count"], "1")
            self.assertEqual(row["vda_action_unknown_count" if old else "vda_action_retriable_count"], "1")
            self.assertEqual(row["vda_error_unknown_count" if old else "vda_error_urgent_count"], "1")

    def test_vda_selected_reports_reach_common_output_with_their_declared_meaning(self):
        record = vda("3.0.0")
        record.update(operatingMode="FUTURE", driving=True, paused=False,
                      safetyState={"activeEmergencyStop": "LATER", "fieldViolation": True},
                      orderId="order-7", orderUpdateId=3, lastNodeId="node-2", lastNodeSequenceId=9,
                      actionStates=[{"actionId": "act-1", "actionType": "pick",
                                     "actionStatus": "FINISHED", "actionDescriptor": "described",
                                     "actionResult": "reported-outcome"}],
                      errors=[{"errorType": "blocked", "errorLevel": "CRITICAL",
                               "errorDescription": "text", "errorHint": "hint",
                               "errorReferences": [{"referenceKey": "actionId",
                                                    "referenceValue": "act-1"}]}])
        summary = self.rows(record, "vda-state-3.0")
        self.assertEqual(summary["vda_operating_mode_reported"], "UNKNOWN")
        self.assertEqual(summary["vda_operating_mode_raw_hex"], "hex:" + b"FUTURE".hex())
        self.assertEqual(summary["vda_emergency_stop_reported"], "UNKNOWN")
        self.assertEqual(summary["vda_emergency_stop_raw_hex"], "hex:" + b"LATER".hex())
        self.assertEqual((summary["vda_driving_reported"], summary["vda_paused_reported"]), ("1", "0"))
        self.assertEqual(summary["vda_protective_field_violation_reported"], "1")
        self.assertEqual(summary["vda_instance_manufacturer_hex"],
                         "hex:" + record["manufacturer"].encode().hex())
        self.assertEqual(summary["vda_instance_serial_number_hex"],
                         "hex:" + record["serialNumber"].encode().hex())
        self.assertEqual(summary["vda_order_id_hex"], "hex:" + b"order-7".hex())
        self.assertEqual(summary["vda_order_update_id"], "3")
        self.assertEqual(summary["vda_last_node_id_hex"], "hex:" + b"node-2".hex())
        self.assertEqual(summary["vda_last_node_sequence"], "9")
        self.assertNotEqual(summary["vda_order_id_hex"], summary["vda_last_node_id_hex"])
        detail = list(csv.DictReader(io.StringIO(module.convert(
            json.dumps(record), "vda-state-3.0", vda_actions=True, vda_errors=True))))
        self.assertEqual([row["vda_record_kind"] for row in detail],
                         ["SUMMARY", "ACTION", "ERROR", "ERROR_REFERENCE"])
        self.assertEqual(detail[1]["vda_action_result_hex"], "hex:" + b"reported-outcome".hex())
        self.assertEqual(detail[2]["vda_error_hint_hex"], "hex:" + b"hint".hex())
        self.assertEqual(detail[3]["vda_error_index"], detail[2]["vda_error_index"])
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            return
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vda.csv"
            source.write_text(module.convert(json.dumps(record), "vda-state-3.0",
                                             vda_actions=True, vda_errors=True))
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
        kinds = [o["fields"]["vda_record_kind"] for o in common["observations"]]
        self.assertEqual(kinds, ["SUMMARY", "ACTION", "ERROR", "ERROR_REFERENCE"])
        head = common["observations"][0]["fields"]
        self.assertEqual(head["vda_operating_mode_raw_hex"], "hex:" + b"FUTURE".hex())
        self.assertEqual(head["vda_instance_serial_number_hex"],
                         "hex:" + record["serialNumber"].encode().hex())
        self.assertEqual(head["vda_order_update_id"], 3)
        action = common["observations"][1]["fields"]
        self.assertEqual(action["vda_action_id_hex"], "hex:" + b"act-1".hex())
        self.assertEqual(action["vda_action_status_reported"], "FINISHED")
        self.assertEqual(action["vda_action_type_hex"], "hex:" + b"pick".hex())
        self.assertEqual(action["vda_action_result_hex"], "hex:" + b"reported-outcome".hex())
        self.assertEqual(action["vda_action_description_hex"], "hex:" + b"described".hex())
        error, reference = (o["fields"] for o in common["observations"][2:4])
        self.assertEqual(error["vda_error_level_reported"], "CRITICAL")
        self.assertEqual(error["vda_error_type_hex"], "hex:" + b"blocked".hex())
        self.assertEqual(error["vda_error_description_hex"], "hex:" + b"text".hex())
        self.assertEqual(error["vda_error_hint_hex"], "hex:" + b"hint".hex())
        self.assertEqual(error["vda_error_reference_count"], 1)
        self.assertEqual(reference["vda_error_index"], error["vda_error_index"])
        self.assertEqual(reference["vda_error_reference_key_hex"], "hex:" + b"actionId".hex())
        self.assertEqual(reference["vda_error_reference_value_hex"], "hex:" + b"act-1".hex())
        units = common["profile_units"]
        self.assertIn("ERROR_REFERENCE", units["vda_record_kind"])
        self.assertIn("still_readable", units["vda_operating_mode_raw_hex"])
        self.assertIn("neither_authenticated_nor_a_global_identity",
                      units["vda_instance_manufacturer_hex"])
        self.assertIn("never_an_execution_receipt", units["vda_order_id_hex"])
        self.assertIn("never_verified_mission_success_or_acceptance", units["vda_action_result_hex"])
        self.assertIn("never_a_musubi_severity_judgement_or_a_physical_fault",
                      units["vda_error_level_reported"])
        self.assertIn("musubi_does_not_resolve_into_a_link", units["vda_error_reference_key_hex"])
        self.assertIn("zero_means_an_explicitly_empty_list", units["vda_error_reference_count"])

    def test_vda_version_specific_tokens_and_bad_records_stay_distinguishable(self):
        old = vda("2.1.0")
        old.update(operatingMode="TEACHIN", safetyState={"eStop": "AUTOACK", "fieldViolation": False})
        row = self.rows(old, "vda-state-2.1")
        self.assertEqual(row["vda_operating_mode_reported"], "TEACH_IN")
        self.assertEqual(row["vda_operating_mode_raw_hex"], "hex:" + b"TEACHIN".hex())
        self.assertEqual(row["vda_emergency_stop_reported"], "AUTOACK")
        modern = vda("3.0.0")
        modern.update(operatingMode="INTERVENED")
        self.assertEqual(self.rows(modern, "vda-state-3.0")["vda_operating_mode_reported"],
                         "INTERVENED")
        old.update(operatingMode="INTERVENED")
        stale = self.rows(old, "vda-state-2.1")
        self.assertEqual(stale["vda_operating_mode_reported"], "UNKNOWN")
        self.assertEqual(stale["vda_operating_mode_raw_hex"], "hex:" + b"INTERVENED".hex())
        with self.assertRaises(ValueError):
            module.convert(json.dumps(vda("3.0.0")), "vda-state-2.1")
        with self.assertRaises(ValueError):
            module.convert(json.dumps({**modern, "operatingMode": 5}), "vda-state-3.0")
        duplicated = json.dumps(modern)
        duplicated = duplicated[:-1] + ', "serialNumber": "second"}'
        with self.assertRaises(ValueError):
            module.convert(duplicated, "vda-state-3.0")
        missing = {key: value for key, value in modern.items() if key != "serialNumber"}
        with self.assertRaises(ValueError):
            module.convert(json.dumps(missing), "vda-state-3.0")

    def test_vda_absent_unknown_and_bad_state_types(self):
        record = vda("3.0.0")
        self.assertEqual(self.rows(record, "vda-state-3.0")["vda_error_fatal_count"], "")
        record.update(errors=[], actionStates=[], operatingMode="FUTURE", driving=False)
        row = self.rows(record, "vda-state-3.0")
        self.assertEqual(row["vda_error_fatal_count"], "0")
        self.assertEqual(row["vda_action_failed_count"], "0")
        self.assertEqual(row["vda_operating_mode_reported"], "UNKNOWN")
        for extra in [{"driving": 0}, {"velocity": {"vx": "2"}}, {"orderUpdateId": True},
                      {"errors": {}}, {"errors": [{"errorType": "x", "errorLevel": 2}]},
                      {"safetyState": {"eStop": "NONE", "fieldViolation": False}}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                module.convert(json.dumps({**record, **extra}), "vda-state-3.0")

    def test_saved_vda_1_1_state_reuses_the_older_layout_functions(self):
        """VDA5050 1.1 through the same selected-field functions; prose-pinned, not fleet-tested."""
        def state(version="1.1.0", **extra):
            record = {"headerId": 3, "timestamp": "2026-01-01T00:00:00.123456789Z",
                      "version": version, "manufacturer": "authored-maker",
                      "serialNumber": "authored-agv-1",
                      "batteryState": {"batteryCharge": 75, "batteryVoltage": 24, "charging": False,
                                       "batteryHealth": 90, "reach": 1200},
                      "operatingMode": "AUTOMATIC", "driving": True, "paused": False,
                      "velocity": {"vx": 1.25, "vy": -.5, "omega": .1},
                      "agvPosition": {"x": -2., "y": 3., "theta": -.5, "mapId": "floor-a",
                                      "positionInitialized": True, "localizationScore": .8,
                                      "deviationRange": .2},
                      "safetyState": {"eStop": "none", "fieldViolation": False},
                      "future": {"unknown": [1, 2]}}
            record.update(extra)
            return record

        second = state(operatingMode="TEACHIN", driving=False, paused=True)
        second["batteryState"] = {"batteryCharge": 20, "charging": True}
        second["velocity"] = {"vx": -.25, "omega": -1.5}
        second["agvPosition"] = {"x": 12.5, "y": -4., "theta": 3.14, "mapId": 'floor,"b',
                                 "positionInitialized": False}
        for record, charge, mode, vx in ((state(), .75, "AUTOMATIC", 1.25),
                                         (second, .2, "TEACH_IN", -.25)):
            row = self.rows(record, "vda-state-1.1")
            self.assertEqual(row["record_time_us"], "1767225600123456")
            self.assertEqual(float(row["battery_remaining_fraction"]), charge)
            self.assertEqual(row["battery_current_sign_basis"], "UNSPECIFIED")
            self.assertEqual(row["battery_temperature_basis"], "NOT_PROVIDED_BY_SELECTED_SCHEMA")
            self.assertEqual(float(row["vehicle_vx_m_s"]), vx)
            self.assertEqual(row["vda_operating_mode_reported"], mode)
            self.assertEqual(bytes.fromhex(row["vda_instance_serial_number_hex"][4:]).decode(),
                             "authored-agv-1")
            self.assertEqual(bytes.fromhex(row["vda_selected_version_hex"][4:]).decode(), "1.1.0")
        first = self.rows(state(), "vda-state-1.1")
        self.assertEqual(float(first["battery_voltage_v"]), 24)
        self.assertEqual(first["battery_reported_charging_state"], "NOT_CHARGING")
        self.assertEqual(float(first["vda_map_x_m"]), -2)
        self.assertEqual(float(first["vda_map_theta_rad"]), -.5)
        self.assertEqual(first["vda_position_state"], "INITIALIZED_REPORTED")
        self.assertEqual(float(first["vda_localization_score_reported"]), .8)
        self.assertEqual(float(first["vda_deviation_range_m"]), .2)
        self.assertEqual(first["vda_position_initialized_reported"], "1")
        self.assertEqual(first["battery_stored_charge_ah"], "")
        self.assertEqual(first["vda_emergency_stop_reported"], "UNKNOWN")
        self.assertEqual(bytes.fromhex(first["vda_emergency_stop_raw_hex"][4:]).decode(), "none")
        self.assertEqual(first["vda_protective_field_violation_reported"], "0")
        teaching = self.rows(state(operatingMode="TEACHING"), "vda-state-1.1")
        self.assertEqual(teaching["vda_operating_mode_reported"], "UNKNOWN")
        self.assertEqual(bytes.fromhex(teaching["vda_operating_mode_raw_hex"][4:]).decode(), "TEACHING")
        for version, fmt in (("2.1.0", "vda-state-1.1"), ("1.1.0", "vda-state-2.0"),
                             ("1.1.0", "vda-state-3.0"), ("1.1", "vda-state-1.1"),
                             ("1.1.1", "vda-state-1.1")):
            with self.subTest(version=version, fmt=fmt), self.assertRaises(ValueError):
                module.convert(json.dumps(state(version)), fmt)
        for extra in ({"mobileRobotPosition": {"x": 0}},
                      {"agvPosition": {"x": 0., "y": 0., "theta": 0., "mapId": "m",
                                       "localized": True}}):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(state(**extra)), "vda-state-1.1")
        typo = state()
        typo["agyPosition"] = typo.pop("agvPosition")
        self.assertEqual(self.rows(typo, "vda-state-1.1")["vda_position_state"], "NOT_REPORTED")
        for flag in ({"vda_actions": True}, {"vda_errors": True}):
            with self.assertRaises(ValueError):
                module.convert(json.dumps(state()), "vda-state-1.1", **flag)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "vda11.csv"
                path.write_text(module.convert("\n".join(
                    json.dumps(r) for r in (state(), second)), "vda-state-1.1"))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["battery_remaining_fraction"], .75)
            self.assertEqual(fields["vehicle_vx_m_s"], 1.25)
            self.assertEqual(fields["vda_map_x_m"], -2)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            declared = common["profile_units"]
            self.assertTrue(declared.get("vda_selected_version_hex"))
            self.assertIn("which_selected_version", declared["vda_selected_version_hex"])
            self.assertIn("1_1", declared["vda_operating_mode_reported"])

    def rows(self, record, fmt):
        text = json.dumps(record, ensure_ascii=False)
        rows = list(csv.DictReader(io.StringIO(module.convert(text, fmt))))
        self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
        return rows[0]

    def test_rover_alias_preserves_columns_and_old_result(self):
        old = "TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct\n1,0,24,2,1000,1,75\n"
        new = old.replace("Instance", "Inst")
        self.assertEqual(module.convert(new, "ardupilot-bat-inst"), module.convert(old).replace("Instance", "Inst"))
        with self.assertRaises(ValueError):
            module.convert(new)
        missing = new.replace(",2,1000,1,75", ",nan,nan,nan,255")
        row = next(csv.DictReader(io.StringIO(module.convert(missing, "ardupilot-bat-inst"))))
        self.assertEqual(float(row["battery_voltage_v"]), 24)
        self.assertEqual([row[k] for k in module.NORMALIZED[1:]], [""] * 5)

    def test_vda_version_boundary_and_optional_values(self):
        for version, fmt in [("2.1.0", "vda-state-2.1"), ("3.0.0", "vda-state-3.0")]:
            row = self.rows(vda(version), fmt)
            self.assertEqual(row["record_time_us"], "1767225600123456")
            self.assertEqual(float(row["battery_remaining_fraction"]), .75)
            self.assertEqual(float(row["battery_voltage_v"]), 24)
            self.assertEqual(row["battery_power_w"], "")
            self.assertEqual(row["battery_reported_charging_state"], "NOT_CHARGING")
        record = vda("3.0.0")
        record["powerSupply"]["batteryCurrent"] = 2
        self.assertEqual(float(self.rows(record, "vda-state-3.0")["battery_power_w"]), 48)

    def test_ros_sign_stored_not_consumed_enum_missing_and_presence(self):
        record = ros()
        row = self.rows(record, "ros-battery-json")
        self.assertEqual(row["record_time_us"], "2123456")
        self.assertEqual(float(row["battery_power_w"]), -48)
        self.assertEqual(float(row["battery_stored_charge_ah"]), 3)
        self.assertEqual(row["battery_consumed_ah"], "")
        record.update(current="NaN", charge="NaN", percentage="NaN", power_supply_status=255)
        row = self.rows(record, "ros-battery-json")
        self.assertEqual(row["battery_power_w"], "")
        self.assertEqual(row["battery_reported_charging_state"], "UNKNOWN_255")
        record["present"] = False
        self.assertTrue(all(self.rows(record, "ros-battery-json")[field] == "" for field in module.NORMALIZED))

    def test_important_failures_and_equal_time_accounting(self):
        for record, fmt in [(vda("3.0.0"), "vda-state-2.1"), (vda("2.1.0"), "vda-state-3.0")]:
            with self.assertRaises(ValueError):
                module.convert(json.dumps(record), fmt)
        for key, bad in [("current", True), ("percentage", 1.1), ("voltage", "NaN"),
                         ("present", 1), ("power_supply_status", 256)]:
            record = ros(); record[key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                module.convert(json.dumps(record), "ros-battery-json")
        for bad in ["", "{}", '{"voltage":1,"voltage":2}', '{"x":NaN}', "null", "\n"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.convert(bad, "ros-battery-json")
        text = json.dumps(ros())
        self.assertEqual(len(list(csv.DictReader(io.StringIO(module.convert(text + "\n" + text, "ros-battery-json"))))), 2)
        later = ros(); later["header"]["stamp"]["sec"] = 3
        with self.assertRaises(ValueError):
            module.convert(json.dumps(later) + "\n" + text, "ros-battery-json")


if __name__ == "__main__":
    unittest.main()

class VdaConnectionTests(unittest.TestCase):
    """Saved VDA5050 connection payloads; no broker, no live client, no physical connectivity claim."""

    PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/vda-connection-profile.toml"

    def rows_of(self, records, source_format):
        text = "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
        return list(csv.DictReader(io.StringIO(module.convert(text, source_format)))), text

    def observations(self, records, source_format):
        rows, text = self.rows_of(records, source_format)
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("required CI sets the built reader")
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "observations.csv"
            path.write_text(module.convert(text, source_format))
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(self.PROFILE), str(path), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
        return rows, common

    def test_each_pinned_version_reports_its_own_states_with_an_explicit_basis(self):
        rows, _ = self.rows_of([connection("2.1.0", "ONLINE", header=1),
                                connection("2.1.0", "OFFLINE", header=2,
                                           stamp="2026-01-01T00:00:01.00Z"),
                                connection("2.1.0", "CONNECTIONBROKEN", header=3,
                                           stamp="2026-01-01T00:00:02.00Z")], "vda-connection-2.1")
        self.assertEqual([r["vda_connection_state_reported"] for r in rows],
                         ["ONLINE", "OFFLINE", "CONNECTIONBROKEN"])
        self.assertEqual({r["vda_connection_state_status"] for r in rows}, {"SUPPORTED_PINNED_STATE"})
        self.assertEqual({r["vda_connection_version_reported"] for r in rows}, {"2.1.0"})
        self.assertEqual([r["vda_connection_header_id"] for r in rows], ["1", "2", "3"])
        for row in rows[:2]:
            self.assertIn("REPORTED_BY_THE_VEHICLE", row["vda_connection_basis"])
            self.assertIn("NOT_PHYSICAL_RF_CONNECTIVITY", row["vda_connection_basis"])
        self.assertIn("BROKER_LAST_WILL", rows[2]["vda_connection_basis"])
        self.assertIn("ALWAYS_OUTDATED", rows[2]["vda_connection_basis"])
        self.assertEqual(bytes.fromhex(rows[0]["vda_connection_manufacturer_hex"][4:]).decode(),
                         "authored-maker")
        self.assertEqual(bytes.fromhex(rows[0]["vda_connection_serial_hex"][4:]).decode(),
                         "authored-serial-1")
        self.assertNotIn("connected", ",".join(rows[0]))

        rows, _ = self.rows_of([connection("3.0.0", "ONLINE"),
                                connection("3.0.0", "HIBERNATING", header=2,
                                           stamp="2026-01-01T00:00:01.000Z"),
                                connection("3.0.0", "CONNECTION_BROKEN", header=3,
                                           stamp="2026-01-01T00:00:02.000Z")], "vda-connection-3.0")
        self.assertEqual([r["vda_connection_state_reported"] for r in rows],
                         ["ONLINE", "HIBERNATING", "CONNECTION_BROKEN"])
        self.assertEqual({r["vda_connection_state_status"] for r in rows}, {"SUPPORTED_PINNED_STATE"})
        self.assertIn("REPORTED_BY_THE_VEHICLE", rows[1]["vda_connection_basis"])
        self.assertIn("BROKER_LAST_WILL", rows[2]["vda_connection_basis"])

    def test_the_versions_are_bound_and_never_assumed_equivalent(self):
        cases = {
            "3.0 token under the 2.1 selector": (["CONNECTION_BROKEN"], "2.1.0", "vda-connection-2.1"),
            "2.1 token under the 3.0 selector": (["CONNECTIONBROKEN"], "3.0.0", "vda-connection-3.0"),
            "hibernating under 2.1": (["HIBERNATING"], "2.1.0", "vda-connection-2.1"),
            "schema prose value": (["DISCONNECTED"], "3.0.0", "vda-connection-3.0"),
            "lower case": (["online"], "3.0.0", "vda-connection-3.0"),
        }
        for name, (states, version, selector) in cases.items():
            with self.subTest(name):
                rows, _ = self.rows_of([connection(version, states[0])], selector)
                self.assertEqual(rows[0]["vda_connection_state_reported"], states[0])
                self.assertEqual(rows[0]["vda_connection_state_status"],
                                 "UNSUPPORTED_STATE_VALUE_RETAINED")
                self.assertIn("NO_PUBLISHER_OR_MEANING_IS_INFERRED", rows[0]["vda_connection_basis"])
        for version, selector in (("3.0.0", "vda-connection-2.1"), ("2.1.0", "vda-connection-3.0"),
                                  ("2.0.0", "vda-connection-2.1"), ("2.1", "vda-connection-2.1")):
            with self.subTest(version + " via " + selector), self.assertRaises(ValueError):
                self.rows_of([connection(version, "ONLINE")], selector)

    def test_missing_and_malformed_fields_are_refused_and_absence_is_not_offline(self):
        good = connection("3.0.0", "ONLINE")
        for name, patch in (("no state", {"connectionState": None}),
                            ("no header", {"headerId": None}),
                            ("negative header", {"headerId": -1}),
                            ("float header", {"headerId": 1.0}),
                            ("oversized header", {"headerId": 2 ** 32}),
                            ("empty serial", {"serialNumber": ""}),
                            ("identity not a string", {"manufacturer": 7}),
                            ("no version", {"version": None}),
                            ("bad timestamp", {"timestamp": "2026-01-01 00:00:00Z"}),
                            ("local timestamp", {"timestamp": "2026-01-01T00:00:00.000+01:00"}),
                            ("numeric timestamp", {"timestamp": 1767225600})):
            record = dict(good)
            for key, value in patch.items():
                if value is None:
                    record.pop(key)
                else:
                    record[key] = value
            with self.subTest(name), self.assertRaises(ValueError):
                self.rows_of([record], "vda-connection-3.0")
        rows, _ = self.rows_of([connection("3.0.0", "ONLINE", header=n,
                                           stamp=f"2026-01-01T00:00:0{n}.000Z") for n in (1, 2)],
                               "vda-connection-3.0")
        self.assertEqual({r["vda_connection_state_reported"] for r in rows}, {"ONLINE"})
        rows, _ = self.rows_of([connection("3.0.0", "ONLINE", header=9),
                                connection("3.0.0", "OFFLINE", header=9)], "vda-connection-3.0")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["vda_connection_header_id"] for r in rows], ["9", "9"])
        self.assertEqual(rows[0]["record_time_us"], rows[1]["record_time_us"])
        with self.assertRaises(ValueError):
            self.rows_of([connection("3.0.0", "ONLINE", stamp="2026-01-01T00:00:02.000Z"),
                          connection("3.0.0", "OFFLINE", stamp="2026-01-01T00:00:01.000Z")],
                         "vda-connection-3.0")

    def test_unknown_fields_survive_and_the_reported_state_reaches_common_observations(self):
        records = [connection("3.0.0", "ONLINE", authoredExtra={"kept": [1, None]}),
                   connection("3.0.0", "CONNECTION_BROKEN", header=2,
                              stamp="2026-01-01T00:00:05.000Z")]
        rows, common = self.observations(records, "vda-connection-3.0")
        self.assertIn("authoredExtra", bytes.fromhex(rows[0]["source_record_hex"][4:]).decode())
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(common["observations"][0]["fields"]["vda_connection_state_reported"], "ONLINE")
        self.assertEqual(common["observations"][1]["fields"]["vda_connection_state_reported"],
                         "CONNECTION_BROKEN")
        self.assertIn("never_physical_rf_connectivity",
                      common["profile_units"]["vda_connection_basis"])
        self.assertIn("never_a_failure_detection_time", common["profile_units"]["record_time_us"])
