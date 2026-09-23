"""Authored saved envelopes; no broker or physical recording claim."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_mqtt_recorded import convert, DOCK_FORMAT
from tests.test_electrical_schema_reuse import connection, vda, ros

ROOT = Path(__file__).resolve().parents[1]


def envelope(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return dict(tst="2026-01-01T01:00:01.123456+0100", topic="example/state", qos=1,
                mid=42, retain=1, payloadlen=len(text.encode()), payload=text, future=["保持"])


def json_value_envelope(payload, *, payloadlen=None, tst="2026-01-01T01:00:01.123456+0100",
                        qos=1, mid=42, retain=1, **extra):
    """A mosquitto `%J` envelope: the payload is a JSON value, not a quoted string.

    mosquitto 2.0.22 emits the same keys in the same order for %j and %J; only the payload
    representation differs. `payloadlen` stays the original wire byte count, which is why it is
    authored here independently of how the value is spelled.
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    record = dict(tst=tst, topic="example/state", qos=qos, mid=mid, retain=retain,
                  payloadlen=len(text.encode()) if payloadlen is None else payloadlen,
                  payload=payload, future=["保持"])
    record.update(extra)
    return json.dumps(record, ensure_ascii=False)


JSON_VALUE_LINE_B = (
    '{"tst":"2026-01-01T01:00:02.500000+0100","topic":"example/state","qos":0,"retain":0,'
    '"payloadlen":211,"payload":{ "version": "2.1.0" , "serialNumber":"example-2",'
    '\t "manufacturer":"synthetic", "batteryState":{"charging":true,"batteryVoltage":48.5,'
    '"batteryCharge":42}, "timestamp":"2026-01-01T00:00:02.987654321Z",'
    '"vendorExtra":["keep",{"deep":null}]},"future":["保持"]}')


class MqttRecordedTests(unittest.TestCase):
    def test_saved_connection_envelopes_reuse_the_same_reported_state_and_clocks(self):
        """A saved %j envelope of the connection topic, not a broker session or a live client."""
        topic = "uagv/v2/authored-maker/authored-serial-1/connection"
        payloads = [connection("2.1.0", "ONLINE", header=1),
                    connection("2.1.0", "CONNECTIONBROKEN", header=2,
                               stamp="2026-01-01T00:00:02.00Z")]
        lines = []
        for index, payload in enumerate(payloads):
            text = json.dumps(payload, ensure_ascii=False)
            record = envelope(payload)
            record.update(topic=topic, payload=text, payloadlen=len(text.encode()), qos=1, retain=1,
                          tst=f"2026-01-01T01:00:0{index + 1}.123456+0100")
            lines.append(json.dumps(record, ensure_ascii=False))
        rows = list(csv.DictReader(io.StringIO(convert("\n".join(lines), topic, "vda-connection-2.1"))))
        self.assertEqual([r["vda_connection_state_reported"] for r in rows],
                         ["ONLINE", "CONNECTIONBROKEN"])
        self.assertIn("BROKER_LAST_WILL", rows[1]["vda_connection_basis"])
        self.assertEqual([r["record_time_us"] for r in rows], ["1767225601123456", "1767225602123456"])
        self.assertEqual([r["payload_record_time_us"] for r in rows],
                         ["1767225600120000", "1767225602000000"])
        self.assertEqual({r["mqtt_clock_basis"] for r in rows}, {"CAPTURE_REPORTED"})
        self.assertEqual({(r["mqtt_qos"], r["mqtt_retain"]) for r in rows}, {("1", "1")})
        self.assertEqual(bytes.fromhex(rows[0]["mqtt_topic_hex"][4:]).decode(), topic)
        self.assertIn(topic, bytes.fromhex(rows[0]["source_record_hex"][4:]).decode())
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as work:
                path = Path(work) / "observations.csv"
                path.write_text(convert("\n".join(lines), topic, "vda-connection-2.1"))
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"],
                     str(ROOT / "profiles/declared/electrical-schema-reuse/"
                         "vda-connection-profile.toml"), str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["observations"][1]["fields"]["vda_connection_state_reported"],
                             "CONNECTIONBROKEN")
            self.assertIn("not_a_wildcard_subscription", common["profile_units"]["mqtt_topic_hex"])

    def test_a_connection_envelope_keeps_the_existing_topic_and_envelope_refusals(self):
        topic = "vda5050/v3/authored-maker/authored-serial-1/connection"
        text = json.dumps(connection("3.0.0", "OFFLINE"), ensure_ascii=False)
        record = envelope(connection("3.0.0", "OFFLINE"))
        record.update(topic=topic, payload=text, payloadlen=len(text.encode()))
        line = json.dumps(record, ensure_ascii=False)
        self.assertTrue(convert(line, topic, "vda-connection-3.0"))
        with self.assertRaises(ValueError):
            convert(line, "vda5050/v3/authored-maker/other-serial/connection", "vda-connection-3.0")
        for wildcard in ("vda5050/v3/+/authored-serial-1/connection", "vda5050/v3/#"):
            with self.subTest(wildcard), self.assertRaises(ValueError):
                convert(line, wildcard, "vda-connection-3.0")
        with self.assertRaises(ValueError):
            convert(line, topic, "vda-connection-2.1")
        broken = json.loads(line)
        broken["payloadlen"] = 3
        with self.assertRaises(ValueError):
            convert(json.dumps(broken), topic, "vda-connection-3.0")

    def test_dji_batteries_reuse_same_units_and_keep_each_array_entry(self):
        from scripts.convert_ardupilot_battery_csv import DJI_FORMAT, convert as electrical
        for voltage, temperature in ((12000, 25.5), (15400, -10.0)):
            payload = {"timestamp": 12345, "data": {"battery": {"capacity_percent": 75,
                "remain_flight_time": 100, "batteries": [
                    {"index": 0, "voltage": voltage, "temperature": temperature,
                     "capacity_percent": 80, "sn": "reported-not-authenticated", "future": [1]},
                    {"index": 2, "voltage": None, "temperature": None, "capacity_percent": 70}]}}}
            first = envelope(payload); first["topic"] = "thing/product/fixture/osd"
            source = json.dumps(first, ensure_ascii=False)
            output = convert(source, first["topic"], DJI_FORMAT)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r["battery_id_reported"] for r in rows], ["0", "2"])
            self.assertEqual(float(rows[0]["battery_voltage_v"]), voltage / 1000)
            self.assertEqual(float(rows[0]["battery_temperature_k"]), temperature + 273.15)
            self.assertEqual(rows[1]["battery_voltage_v"], "")
            self.assertAlmostEqual(float(rows[1]["battery_remaining_fraction"]), .7)
            self.assertEqual(rows[0]["mqtt_envelope_sha256"], rows[1]["mqtt_envelope_sha256"])
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), source)
            self.assertEqual(rows[0]["payload_record_time_us"], rows[0]["record_time_us"])
            direct = electrical(json.dumps({"capture_time_us": 100, "payload": payload}), DJI_FORMAT)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(direct)))), 2)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(result["main_rows"], 2)
                    self.assertEqual(result["platform_domain"], "Unknown")
                    self.assertEqual(result["observations"][1]["fields"]["battery_voltage_v"], None)
            for bad_entries in ([{"index": 0}], [{"index": 0, "voltage": "12000"}],
                                [{"index": 0, "capacity_percent": 101}],
                                [{"index": 0, "temperature": -300}],
                                [{"index": 0, "voltage": 12}, {"index": 0, "voltage": 13}]):
                bad = json.loads(json.dumps(payload)); bad["data"]["battery"]["batteries"] = bad_entries
                with self.assertRaises(ValueError): electrical(json.dumps({"capture_time_us": 1, "payload": bad}), DJI_FORMAT)
            with self.assertRaises(ValueError): convert(source, "thing/product/fixture/state", DJI_FORMAT)

    def test_vda_details_reuse_payload_and_capture_bindings(self):
        import hashlib
        import sys
        old_limit = csv.field_size_limit(2 * 1024 * 1024)
        try:
            for version in ("2.1.0", "3.0.0"):
                payload = vda(version)
                payload["errors"] = [{"errorType": "reported", "errorLevel": "WARNING",
                    "errorReferences": [{"referenceKey": "nodeId", "referenceValue": version}]}]
                payload["actionStates"] = [{"actionId": "a", "actionStatus": "RUNNING"}]
                first = envelope(payload)
                second = dict(first, tst="2026-01-01T01:00:01.123457+0100")
                lines = [json.dumps(r, ensure_ascii=False) for r in (first, second)]
                fmt = "vda-state-" + version[:3]
                output = convert("\n".join(lines), "example/state", fmt, vda_actions=True, vda_errors=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(len(rows), 8)
                for index in range(2):
                    group = rows[index * 4:(index + 1) * 4]
                    normalized = bytes.fromhex(group[0]["mqtt_payload_source_hex"][4:])
                    self.assertEqual(group[0]["vda_source_sha256"], "sha256:" + hashlib.sha256(normalized).hexdigest())
                    self.assertEqual(bytes.fromhex(group[0]["source_record_hex"][4:]).decode(), lines[index])
                    self.assertTrue(all(not row["source_record_hex"] and not row["mqtt_payload_source_hex"] for row in group[1:]))
                    self.assertTrue(all(row["vda_source_record_index"] == str(index) for row in group))
                    self.assertTrue(all(row["mqtt_envelope_sha256"] == "sha256:" + hashlib.sha256(lines[index].encode()).hexdigest() for row in group))
                    self.assertTrue(all(row["record_time_us"] == group[0]["record_time_us"] for row in group))
                    self.assertNotEqual(group[0]["record_time_us"], group[0]["payload_record_time_us"])
                with tempfile.TemporaryDirectory() as directory:
                    input_path = Path(directory) / "capture.jsonl"; input_path.write_text("\n".join(lines))
                    path = Path(directory) / "out.csv"
                    command = [sys.executable, str(ROOT / "scripts/convert_mqtt_recorded.py"), str(input_path), str(path),
                               "--topic", "example/state", "--payload-format", fmt, "--vda-actions", "--vda-errors"]
                    subprocess.run(command, check=True, capture_output=True)
                    self.assertEqual(path.read_text(), output)
                    self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
                    if os.environ.get("MUSUBI_TELEMETRY_READER"):
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                        self.assertEqual(common["main_rows"], 8)
                        self.assertEqual(common["observations"][7]["fields"]["vda_error_reference_value_hex"], "hex:" + version.encode().hex())
            payload["errors"] = [{"errorType": "reported", "errorLevel": "WARNING"} for _ in range(1000)]
            text = json.dumps(envelope(payload), ensure_ascii=False)
            with self.assertRaises(ValueError): convert(text, "example/state", fmt)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(convert(text, "example/state", fmt, vda_errors=True))))), 1001)
            with self.assertRaises(ValueError): convert(text, "other", fmt, vda_errors=True)
            with self.assertRaises(ValueError): convert(text, "example/state", "vda-state-2.0", vda_errors=True)
        finally:
            csv.field_size_limit(old_limit)

    def test_reuses_payloads_preserves_clocks_unknown_fields_and_common_output(self):
        for fmt, payload in [("vda-state-2.0", vda("2.0.0")), ("vda-state-2.1", vda("2.1.0")), ("vda-state-3.0", vda("3.0.0")),
                             ("ros-battery-json", ros())]:
            first = envelope(payload)
            second = dict(first, tst="2026-01-01T00:00:01.123456+0000", retain=0)
            lines = [json.dumps(r, ensure_ascii=False) for r in [first, second]]
            output = convert("\n".join(lines), "example/state", fmt)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["record_time_us"], rows[1]["record_time_us"])
            self.assertNotEqual(rows[0]["record_time_us"], rows[0]["payload_record_time_us"])
            self.assertEqual(float(rows[0]["battery_voltage_v"]), 24.)
            self.assertEqual([r["mqtt_retain"] for r in rows], ["1", "0"])
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), lines[0])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(common["observations"][0]["fields"]["battery_voltage_v"], 24.)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_raw_botstate_reuses_converter_and_envelope_clock(self):
        records = [envelope({"location_data": {"position": {"x": x, "y": None}},
                             "informational_settings": {"busy": False}, "unknown": ["保持"]})
                   for x in (100, -250)]
        text = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        output = convert(text, "example/state", "farmbot-state")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([float(row["farm_position_x_m"]) for row in rows], [0.1, -0.25])
        for row, original in zip(rows, text.splitlines()):
            self.assertEqual(row["payload_record_time_us"], row["record_time_us"])
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), original)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"; path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertEqual(common["observations"][1]["fields"]["farm_position_x_m"], -0.25)
        for payload in ({}, {"location_data": {"position": {"x": "100"}}}, vda("2.1.0")):
            with self.assertRaises(ValueError):
                convert(json.dumps(envelope(payload)), "example/state", "farmbot-state")

    def test_json_value_envelopes_reuse_the_same_vda_state_clocks_and_provenance(self):
        """Authored mosquitto %J captures, not a broker session, a real capture or delivery proof."""
        from scripts.convert_mqtt_recorded import JSON_VALUE_ENVELOPE, JSON_VALUE_LENGTH_BASIS
        payload = vda("2.1.0")
        line_a = json_value_envelope(payload)
        text = "\n".join([line_a, JSON_VALUE_LINE_B])
        output = convert(text, "example/state", "vda-state-2.1", envelope_format="J")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)  # every line yields its row; nothing sampled
        reference = next(csv.DictReader(io.StringIO(
            convert(json.dumps(envelope(payload), ensure_ascii=False), "example/state", "vda-state-2.1"))))
        for column in ("battery_voltage_v", "battery_remaining_fraction", "battery_consumed_ah",
                       "battery_reported_charging_state", "battery_current_sign_basis",
                       "battery_temperature_basis", "record_time_us", "payload_record_time_us",
                       "mqtt_topic_hex", "mqtt_qos", "mqtt_retain", "mqtt_clock_basis"):
            self.assertEqual(rows[0][column], reference[column], column)
        self.assertEqual(float(rows[0]["battery_voltage_v"]), 24.)
        self.assertEqual(float(rows[0]["battery_remaining_fraction"]), .75)
        self.assertEqual(rows[0]["battery_reported_charging_state"], "NOT_CHARGING")
        self.assertEqual(float(rows[1]["battery_voltage_v"]), 48.5)
        self.assertEqual(float(rows[1]["battery_remaining_fraction"]), .42)
        self.assertEqual(rows[1]["battery_reported_charging_state"], "CHARGING")
        self.assertEqual([r["mqtt_qos"] for r in rows], ["1", "0"])
        self.assertEqual([r["mqtt_retain"] for r in rows], ["1", "0"])
        for row, line in zip(rows, text.splitlines()):
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), line)
            self.assertNotEqual(row["record_time_us"], row["payload_record_time_us"])
            self.assertEqual(row["mqtt_clock_basis"], "CAPTURE_REPORTED")
            self.assertEqual(row["mqtt_envelope_format"], JSON_VALUE_ENVELOPE)
            self.assertIn("NOT_ORIGINAL_PAYLOAD_BYTES", row["mqtt_envelope_format"])
            self.assertEqual(row["mqtt_payload_length_basis"], JSON_VALUE_LENGTH_BASIS)
            self.assertIn("NOT_VERIFIED", row["mqtt_payload_length_basis"])
        self.assertEqual(rows[1]["mqtt_payload_bytes_reported"], "211")
        self.assertIn("vendorExtra", bytes.fromhex(rows[1]["source_record_hex"][4:]).decode())
        self.assertIn("保持", bytes.fromhex(rows[0]["source_record_hex"][4:]).decode())
        self.assertNotIn("vendorExtra", output.splitlines()[0])
        many = "\n".join(json_value_envelope(
            payload, tst=f"2026-01-01T01:00:01.{index:06d}+0100") for index in range(200))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(
            convert(many, "example/state", "vda-state-2.1", envelope_format="J"))))), 200)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["platform_domain"], "Unknown")
                first, second = (o["fields"] for o in common["observations"])
                self.assertEqual(first["battery_voltage_v"], 24.)
                self.assertEqual(second["battery_voltage_v"], 48.5)
                self.assertEqual(second["battery_remaining_fraction"], .42)
                self.assertEqual(second["battery_reported_charging_state"], "CHARGING")
                units = common["profile_units"]
                self.assertIn("NOT_ORIGINAL_PAYLOAD_BYTES", units["mqtt_envelope_format"])
                self.assertIn("not_verified", units["mqtt_payload_length_basis"])
                self.assertIn("reported", units["mqtt_payload_bytes_reported"])
        else:  # never counted as a pass
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; common-output assertions NOT RUN")

    def test_json_value_envelope_refusals_keep_the_default_j_path_unchanged(self):
        payload = vda("2.1.0")
        j_line = json.dumps(envelope(payload), ensure_ascii=False)
        self.assertEqual(convert(j_line, "example/state", "vda-state-2.1"),
                         convert(j_line, "example/state", "vda-state-2.1", envelope_format="j"))
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / "in.jsonl", Path(directory) / "out.csv"
            source.write_text(json_value_envelope(payload))
            command = [os.sys.executable, "-m", "scripts.convert_mqtt_recorded", str(source), str(out),
                       "--topic", "example/state", "--payload-format", "vda-state-2.1"]
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)
            subprocess.run(command + ["--envelope-format", "J"], cwd=ROOT, check=True, capture_output=True)
            self.assertEqual(out.read_text(), convert(json_value_envelope(payload), "example/state",
                                                      "vda-state-2.1", envelope_format="J"))
        with self.assertRaises(ValueError) as refusal:
            convert(j_line, "example/state", "vda-state-2.1", envelope_format="J")
        self.assertEqual(str(refusal.exception), "selected %J envelope requires a JSON object payload")
        with self.assertRaises(ValueError) as refusal:
            convert(json_value_envelope(payload), "example/state", "vda-state-2.1")
        self.assertEqual(str(refusal.exception), "payload representation or byte count mismatch")
        for selector in ("", "%J", "Jj", "b", None, 1):
            with self.subTest(selector=selector), self.assertRaises(ValueError) as refusal:
                convert(j_line, "example/state", "vda-state-2.1", envelope_format=selector)
            self.assertEqual(str(refusal.exception),
                             "explicit supported mosquitto envelope format required")
        for shape in ([payload], 24, "24", True, None):
            with self.subTest(shape=shape), self.assertRaises(ValueError) as refusal:
                convert(json_value_envelope(shape), "example/state", "vda-state-2.1", envelope_format="J")
            self.assertEqual(str(refusal.exception), "selected %J envelope requires a JSON object payload")
        for length in (-1, 16 * 1024 * 1024 + 1, "216", 216.0, None):
            record = json.loads(json_value_envelope(payload))
            record["payloadlen"] = length
            if length is None:
                del record["payloadlen"]
            with self.subTest(payloadlen=length), self.assertRaises(ValueError) as refusal:
                convert(json.dumps(record, ensure_ascii=False), "example/state", "vda-state-2.1",
                        envelope_format="J")
            self.assertEqual(str(refusal.exception), "invalid reported payload byte count")
        duplicated = json_value_envelope(payload).replace('"manufacturer": "synthetic"',
            '"manufacturer": "synthetic", "manufacturer": "other"', 1)
        self.assertIn('"manufacturer": "other"', duplicated)
        with self.assertRaises(ValueError):
            convert(duplicated, "example/state", "vda-state-2.1", envelope_format="J")
        for patch, message in ((dict(topic="other"), "capture topic mismatch"),
                               (dict(qos=3), "invalid reported MQTT flag"),
                               (dict(retain=2), "invalid reported MQTT flag"),
                               (dict(qos=True), "invalid reported MQTT flag"),
                               (dict(mid=0), "invalid reported packet identifier"),
                               (dict(tst="2026-01-01T00:00:00Z"), "unsupported capture timestamp")):
            with self.subTest(patch=patch), self.assertRaises(ValueError) as refusal:
                convert(json_value_envelope(payload, **patch), "example/state", "vda-state-2.1",
                        envelope_format="J")
            self.assertEqual(str(refusal.exception), message)
        for text in (json_value_envelope(payload) + "\n" +
                     json_value_envelope(payload, tst="2026-01-01T00:00:00.000000+0100"),
                     json_value_envelope(payload) + "\n\n",
                     json_value_envelope(vda("2.0.0")), json_value_envelope({"version": "2.1.0"}),
                     '["not an object"]'):
            with self.assertRaises(ValueError):
                convert(text, "example/state", "vda-state-2.1", envelope_format="J")

    def test_rejects_wrong_envelope_and_payload_without_partial_success(self):
        base = envelope(vda("2.1.0"))
        for patch in [dict(topic="other"), dict(payloadlen=1), dict(payload={}), dict(qos=True),
                      dict(retain=2), dict(mid=0), dict(tst="2026-01-01T00:00:00Z"),
                      dict(tst="2026-02-30T00:00:00.000000+0000")]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                convert(json.dumps(dict(base, **patch)), "example/state", "vda-state-2.1")
        good = json.dumps(base)
        for text, topic, fmt in [(good, "#", "vda-state-2.1"), (good, "example/state", "vda-state-3.0"),
                (good, "example/state", "vda-state-2.0"),
                (good + "\n\n", "example/state", "vda-state-2.1"),
                (good[:-1] + ',"qos":1}', "example/state", "vda-state-2.1"),
                (good + "\n" + json.dumps(dict(base, tst="2025-01-01T00:00:00.000000+0000")),
                 "example/state", "vda-state-2.1")]:
            with self.assertRaises(ValueError):
                convert(text, topic, fmt)


DOCK_TOPIC = "thing/product/DOCK-SN-001/osd"
DOCK_PROFILE = "profiles/declared/dji-dock-osd/profile.toml"


def dock_payload(**changes):
    """One saved dock OSD payload shaped as the pinned dock1 properties page documents it.

    Authored from the specification at dji-sdk/Cloud-API-Doc
    4ec6b0c7f9472aeb09a0a47949855d19c473ea07; no dock, broker or real capture is involved.
    """
    data = {"mode_code": 4, "cover_state": 1, "putter_state": 0, "supplement_light_state": 0,
            "emergency_stop_state": 0, "alarm_state": 0, "rainfall": 1,
            "environment_temperature": 21.5, "temperature": 26.0, "humidity": 48.5,
            "wind_speed": 3.25, "network_state": {"type": 2, "quality": 4, "rate": 512.5},
            "drone_charge_state": {"state": 1, "capacity_percent": 78},
            "sub_device": {"device_sn": "AC-SN-002", "device_online_status": 1, "device_paired": 1},
            "unselected_dock_property": [1, 2]}
    data.update(changes.pop("data", {}))
    payload = {"tid": "t-1", "bid": "b-1", "timestamp": 1789000000123, "gateway": "DOCK-SN-001",
               "data": data}
    payload.update(changes)
    return payload


def dock_line(payload, *, topic=DOCK_TOPIC, tst="2026-09-22T10:00:00.000000+0000"):
    text = json.dumps(payload, ensure_ascii=False)
    return json.dumps({"topic": topic, "qos": 0, "retain": 0, "payloadlen": len(text.encode()),
                       "tst": tst, "payload": text})


class DockSavedStatusTests(unittest.TestCase):
    """Saved dock station status: the dock's own report, captured at a broker.

    The pinned tree places these properties under `dock-to-cloud` and the topic definition states the
    direction "Device > Cloud Server", so this is station-origin evidence and NOT a cloud-side export.
    """

    def rows(self, text, topic=DOCK_TOPIC):
        return list(csv.DictReader(io.StringIO(convert(text, topic, DOCK_FORMAT))))

    def test_authored_dock_status_reaches_common_output_with_selected_meanings(self):
        source = dock_line(dock_payload())
        rows = self.rows(source + "\n")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["dock_mode_code"], row["dock_mode_reported"]), ("4", "IN_OPERATION"))
        self.assertEqual(row["dock_cover_state_reported"], "ON")
        self.assertEqual(row["dock_rainfall_reported"], "LIGHT_RAIN")
        self.assertAlmostEqual(float(row["dock_environment_temperature_k"]), 294.65)
        self.assertAlmostEqual(float(row["dock_internal_temperature_k"]), 299.15)
        self.assertEqual(float(row["dock_internal_humidity_percent_reported"]), 48.5)
        self.assertEqual(float(row["dock_wind_speed_m_s"]), 3.25)
        self.assertEqual(float(row["dock_network_rate_kb_s_reported"]), 512.5)
        self.assertEqual(row["dock_network_quality_reported"], "BETTER")
        self.assertEqual(row["dock_aircraft_charge_state_reported"], "CHARGING")
        self.assertAlmostEqual(float(row["dock_aircraft_charge_capacity_fraction"]), .78)
        self.assertEqual(bytes.fromhex(row["dock_sub_device_sn_hex"][4:]).decode(), "AC-SN-002")
        self.assertEqual(bytes.fromhex(row["dock_gateway_sn_hex"][4:]).decode(), "DOCK-SN-001")
        self.assertEqual(row["dock_payload_timestamp_ms_reported"], "1789000000123")
        self.assertEqual(row["record_time_us"], "1790071200000000")
        self.assertEqual(row["mqtt_clock_basis"], "CAPTURE_REPORTED")
        self.assertIn("NOT_A_CLOUD_SIDE_EXPORT", row["dock_value_basis"])
        self.assertIn("NEITHER_AUTHENTICATED", row["dock_identity_basis"])
        retained = bytes.fromhex(row["source_record_hex"][4:]).decode()
        self.assertEqual(retained, source)
        self.assertIn("unselected_dock_property", retained)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"
                path.write_text(convert(source + "\n", DOCK_TOPIC, DOCK_FORMAT))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / DOCK_PROFILE), str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            self.assertEqual(common["platform_domain"], "Unknown")
            fields = common["observations"][0]["fields"]
            self.assertAlmostEqual(fields["dock_environment_temperature_k"], 294.65)
            self.assertAlmostEqual(fields["dock_aircraft_charge_capacity_fraction"], .78)
            self.assertEqual(fields["dock_mode_code"], 4)
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            units = common["profile_units"]
            self.assertIn("inside_dock_temperature", units["dock_internal_temperature_k"])
            self.assertIn("no_byte_base_is_documented", units["dock_network_rate_kb_s_reported"])
            self.assertIn("never_the_aircrafts_own_battery_telemetry",
                          units["dock_aircraft_charge_state_reported"])

    def test_missing_is_not_zero_and_an_unlisted_enum_code_is_retained(self):
        sparse = self.rows(dock_line({"gateway": "DOCK-SN-001", "data": {"mode_code": 0}}) + "\n")[0]
        self.assertEqual((sparse["dock_mode_code"], sparse["dock_mode_reported"]), ("0", "IDLE"))
        for absent in ("dock_cover_state_code", "dock_environment_temperature_k",
                       "dock_internal_humidity_percent_reported", "dock_wind_speed_m_s",
                       "dock_aircraft_charge_capacity_fraction", "dock_sub_device_sn_hex",
                       "dock_payload_timestamp_ms_reported"):
            self.assertEqual(sparse[absent], "", absent)
        zeros = self.rows(dock_line(dock_payload(data={"mode_code": 0, "cover_state": 0, "rainfall": 0,
                                                       "network_state": {"quality": 0}})) + "\n")[0]
        self.assertEqual(zeros["dock_cover_state_reported"], "DISABLE")
        self.assertEqual(zeros["dock_rainfall_reported"], "NO_RAIN")
        self.assertEqual((zeros["dock_network_quality_code"],
                          zeros["dock_network_quality_reported"]), ("0", "NO_SIGNAL"))
        unknown = self.rows(dock_line(dock_payload(data={"mode_code": 99})) + "\n")[0]
        self.assertEqual((unknown["dock_mode_code"], unknown["dock_mode_reported"]),
                         ("99", "UNKNOWN_CODE_RETAINED"))
        both = [self.rows(dock_line(dock_payload(data={"network_state": {"quality": q}})) + "\n")[0]
                for q in (1, 2)]
        self.assertEqual([r["dock_network_quality_reported"] for r in both], ["POOR", "POOR"])
        self.assertEqual([r["dock_network_quality_code"] for r in both], ["1", "2"])

    def test_wrong_topic_aircraft_payload_and_malformed_fields_are_refused(self):
        for label, text, topic in (
                ("non-OSD topic", dock_line(dock_payload(), topic="example/state"), "example/state"),
                ("dock state absent", dock_line({"data": {"cover_state": 1}}), DOCK_TOPIC),
                ("no data object", dock_line({"timestamp": 1}), DOCK_TOPIC),
                ("non-integer enum", dock_line(dock_payload(data={"mode_code": "4"})), DOCK_TOPIC),
                ("boolean enum", dock_line(dock_payload(data={"cover_state": True})), DOCK_TOPIC),
                ("charge percent above the declared maximum",
                 dock_line(dock_payload(data={"drone_charge_state": {"capacity_percent": 101}})), DOCK_TOPIC),
                ("non-numeric temperature",
                 dock_line(dock_payload(data={"environment_temperature": "21.5"})), DOCK_TOPIC),
                ("empty sub-device serial",
                 dock_line(dock_payload(data={"sub_device": {"device_sn": ""}})), DOCK_TOPIC),
                ("network state not an object",
                 dock_line(dock_payload(data={"network_state": 2})), DOCK_TOPIC)):
            with self.subTest(label=label), self.assertRaises(ValueError):
                convert(text + "\n", topic, DOCK_FORMAT)
        aircraft = {"timestamp": 12345, "data": {"mode_code": 0, "battery": {"capacity_percent": 75,
                    "batteries": [{"index": 0, "voltage": 12000}]}}}
        with self.assertRaises(ValueError):
            convert(dock_line(aircraft) + "\n", DOCK_TOPIC, DOCK_FORMAT)
        from scripts.convert_ardupilot_battery_csv import DJI_FORMAT
        with self.assertRaises(ValueError):
            convert(dock_line(dock_payload()) + "\n", DOCK_TOPIC, DJI_FORMAT)

    def test_an_ambiguous_sparse_payload_still_declares_its_origin_unverified(self):
        """The narrowest valid contract instance proves the selector is a declaration, not a detector.

        `{"data": {"mode_code": 0}}` satisfies this route, yet the OSD topic shape and `mode_code` are
        both shared with the aircraft, so nothing in it establishes a producer. The caller-declared,
        origin-unverified qualification must therefore reach common output, not merely the README.
        """
        ambiguous = dock_line({"data": {"mode_code": 0}})
        row = self.rows(ambiguous + "\n")[0]
        self.assertEqual(row["dock_source_declaration"],
                         "CALLER_DECLARED_DOCK1_SOURCE_NOT_AUTOMATIC_PRODUCER_IDENTIFICATION")
        self.assertIn("ORIGIN_IS_UNVERIFIED_HERE", row["dock_value_basis"])
        self.assertIn("NOT_EVERY_PARTIAL_OR_SHARED_FIELD_PAYLOAD", row["dock_value_basis"])
        for absent in ("dock_gateway_sn_hex", "dock_sub_device_sn_hex",
                       "dock_payload_timestamp_ms_reported"):
            self.assertEqual(row[absent], "", absent)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sparse.csv"
                path.write_text(convert(ambiguous + "\n", DOCK_TOPIC, DOCK_FORMAT))
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / DOCK_PROFILE), str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["dock_source_declaration"],
                             "CALLER_DECLARED_DOCK1_SOURCE_NOT_AUTOMATIC_PRODUCER_IDENTIFICATION")
            self.assertIn("ORIGIN_IS_UNVERIFIED_HERE", fields["dock_value_basis"])
            self.assertIsNone(fields["dock_gateway_sn_hex"])
            units = common["profile_units"]
            self.assertIn("is_not_a_finding_of_this_converter", units["dock_source_declaration"])
            self.assertIn("not_every_partial_or_shared_field_payload", units["dock_value_basis"])

    def test_a_separate_authored_station_reuses_the_frozen_selector(self):
        """Written after the implementation was frozen, not used to tune it. Synthetic, from the spec."""
        held_out = {"tid": "held-out", "bid": "held-out", "timestamp": 1789111111222,
                    "gateway": "DOCK-SN-HELD", "data": {
                        "mode_code": 2, "cover_state": 3, "putter_state": 2,
                        "supplement_light_state": 1, "emergency_stop_state": 1, "alarm_state": 1,
                        "rainfall": 3, "environment_temperature": -5.5, "temperature": 0.0,
                        "humidity": 91.0, "wind_speed": 17.0,
                        "network_state": {"type": 1, "quality": 5, "rate": 8.0},
                        "drone_charge_state": {"state": 0, "capacity_percent": 0},
                        "sub_device": {"device_sn": "AC-SN-HELD", "device_online_status": 0,
                                       "device_paired": 0}}}
        row = self.rows(dock_line(held_out, topic="thing/product/DOCK-SN-HELD/osd") + "\n",
                        topic="thing/product/DOCK-SN-HELD/osd")[0]
        self.assertEqual(row["dock_mode_reported"], "REMOTE_DEBUGGING")
        self.assertEqual(row["dock_cover_state_reported"], "COVER_STATE_ABNORMAL")
        self.assertEqual(row["dock_putter_state_reported"], "HALF_OPEN")
        self.assertEqual(row["dock_rainfall_reported"], "HEAVY_RAIN")
        self.assertEqual(row["dock_network_type_reported"], "FOUR_G")
        self.assertEqual(row["dock_sub_device_online_reported"], "POWER_OFF")
        self.assertAlmostEqual(float(row["dock_environment_temperature_k"]), 267.65)
        self.assertAlmostEqual(float(row["dock_internal_temperature_k"]), 273.15)
        self.assertEqual(float(row["dock_aircraft_charge_capacity_fraction"]), 0.)
        self.assertEqual(row["dock_aircraft_charge_state_reported"], "IDLE")


if __name__ == "__main__":
    unittest.main()
