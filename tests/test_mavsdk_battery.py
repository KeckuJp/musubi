"""Synthetic fixed-version SDK exports, not a MAVSDK connection or vehicle capture."""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from scripts.convert_ardupilot_battery_csv import convert

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return dict(sdk_version="3.17.4", capture_time_us=10, battery=dict(id=2,
        voltageV=24, currentBatteryA=-1, capacityConsumedAh=3, remainingPercent=75,
        temperatureDegc=20, timeRemainingS=50, batteryFunction="BATTERY_FUNCTION_PROPULSION"),
        future={"unmapped": 7})


class SdkBattery(unittest.TestCase):
    def test_two_inputs_common_units_and_retained_fields(self):
        for voltage, percent in ((24, 75), (12, 25)):
            record = sample()
            record["battery"].update(voltageV=voltage, remainingPercent=percent)
            text = json.dumps(record)
            output = convert(text, "mavsdk-battery-json")
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row["battery_remaining_fraction"]), percent / 100)
            self.assertEqual(float(row["battery_consumed_ah"]), 3)
            self.assertEqual(float(row["battery_current_a"]), -1)
            self.assertEqual(float(row["battery_temperature_k"]), 293.15)
            self.assertEqual(row["battery_consumed_j"], "")
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), text)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"
                    path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path)], check=True, capture_output=True).stdout)
                    self.assertEqual(result["observations"][0]["fields"]["battery_voltage_v"], voltage)

    def test_missing_sentinel_is_not_valid_zero_or_negative_fraction(self):
        record = sample()
        record["battery"].update(remainingPercent=-1, currentBatteryA="NaN", temperatureDegc="NaN",
                                 batteryFunction=99)
        row = next(csv.DictReader(io.StringIO(convert(json.dumps(record), "mavsdk-battery-json"))))
        self.assertEqual(row["battery_remaining_fraction"], "")
        self.assertEqual(row["battery_power_w"], "")
        self.assertEqual(row["battery_function_reported"], "UNKNOWN_99")
        for key, value in (("remainingPercent", 101), ("remainingPercent", -2), ("voltageV", True),
                           ("temperatureDegc", -274), ("capacityConsumedAh", -1),
                           ("voltageV", 1e100), ("remaining_percent", 1)):
            record = sample()
            record["battery"][key] = value
            with self.assertRaises(ValueError):
                convert(json.dumps(record), "mavsdk-battery-json")
        record = sample()
        record["sdk_version"] = "2.0.0"
        with self.assertRaises(ValueError):
            convert(json.dumps(record), "mavsdk-battery-json")


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def euler_wire(roll=None, pitch=None, yaw=None, stamp=None):
    """The selected EulerAngle on the wire, built here so the fixture needs no decoder library.

    proto3 tags: field<<3 | 5 for a 32-bit float, field<<3 | 0 for a varint. A field left None is
    simply not written, which is exactly the absent-or-zero case the accounting column names.
    """
    payload = b""
    for number, value in ((1, roll), (2, pitch), (3, yaw)):
        if value is not None:
            payload += bytes([number << 3 | 5]) + struct.pack("<f", value)
    if stamp is not None:
        payload += bytes([4 << 3]) + varint(stamp)
    return payload


def protobuf_decoder_available():
    """True only when the pinned optional decoder is importable; its absence is a skip, not a pass."""
    try:
        return importlib.util.find_spec("google.protobuf") is not None
    except ModuleNotFoundError:
        return False


def attitude(roll=12.5, pitch=-3.25, yaw=180.0, stamp=42000, capture=1000):
    """One authored saved EulerAngle envelope; never an SDK connection or a vehicle."""
    return dict(sdk_version="3.17.4", capture_time_us=capture,
                attitude_euler=dict(rollDeg=roll, pitchDeg=pitch, yawDeg=yaw, timestampUs=stamp),
                future={"unmapped": 7})


class SdkAttitude(unittest.TestCase):
    """Saved reported attitude: the declared orientation values, not a derived or fused attitude."""

    def common_fields(self, output, expected=1):
        """The actual shared reader over one converted CSV; never a silent pass.

        These new tests are skipped outright when no reader is configured, so reaching here means
        one was. A configured path that does not exist is a failure, not something to work around.
        """
        declared = os.environ["MUSUBI_TELEMETRY_READER"]
        if not Path(declared).exists():
            self.fail(f"MUSUBI_TELEMETRY_READER is set to {declared!r}, which does not exist")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([declared,
                str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                str(path)], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], expected)
        return common

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-output proof needs the built shared reader; required CI supplies it")
    def test_two_authored_messages_reach_common_output_with_declared_orientation(self):
        import math
        for roll, pitch, yaw, stamp in ((12.5, -3.25, 180.0, 42000), (0.0, 89.5, -45.125, 0)):
            record = attitude(roll, pitch, yaw, stamp)
            text = json.dumps(record)
            row = next(csv.DictReader(io.StringIO(convert(text, "mavsdk-attitude-json"))))
            self.assertAlmostEqual(float(row["attitude_roll_rad"]), math.radians(roll))
            self.assertAlmostEqual(float(row["attitude_pitch_rad"]), math.radians(pitch))
            self.assertAlmostEqual(float(row["attitude_yaw_rad"]), math.radians(yaw))
            self.assertEqual(row["attitude_device_time_us"], str(stamp))
            self.assertEqual(row["record_time_us"], "1000")
            self.assertNotEqual(row["attitude_device_time_us"], row["record_time_us"])
            self.assertIn("NOT_UTC_NOT_THE_CAPTURE_CLOCK", row["attitude_device_time_basis"])
            self.assertIn("REFERENCE_FRAME_NOT_STATED_BY_SOURCE", row["attitude_angle_basis"])
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), text)
            common = self.common_fields(convert(text, "mavsdk-attitude-json"))
            fields = common["observations"][0]["fields"]
            self.assertAlmostEqual(fields["attitude_roll_rad"], math.radians(roll))
            self.assertAlmostEqual(fields["attitude_yaw_rad"], math.radians(yaw))
            self.assertEqual(fields["attitude_device_time_us"], stamp)
            self.assertEqual(fields["record_time_us"], 1000)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            units = common["profile_units"]
            self.assertIn("reference_frame_is_explicitly_not_stated_by_the_source",
                          units["attitude_angle_basis"])
            self.assertIn("never_a_measured_verified_or_fused_attitude", units["attitude_roll_rad"])
            self.assertIn("boot_relative", units["attitude_device_time_us"])

    def test_unknown_angles_are_withheld_and_malformed_envelopes_fail(self):
        record = attitude()
        record["attitude_euler"].update(rollDeg="NaN", yawDeg="NaN")
        row = next(csv.DictReader(io.StringIO(convert(json.dumps(record), "mavsdk-attitude-json"))))
        self.assertEqual((row["attitude_roll_rad"], row["attitude_yaw_rad"]), ("", ""))
        self.assertTrue(float(row["attitude_pitch_rad"]) < 0)
        self.assertIn("RIGHT_HANDED", row["attitude_angle_basis"])
        for key, value in (("rollDeg", True), ("pitchDeg", 1e100), ("yawDeg", None),
                           ("timestampUs", -1), ("timestampUs", 1.5), ("timestampUs", "NaN"),
                           ("roll_deg", 1)):
            record = attitude()
            record["attitude_euler"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                convert(json.dumps(record), "mavsdk-attitude-json")
        for missing in ("rollDeg", "pitchDeg", "yawDeg", "timestampUs"):
            record = attitude()
            del record["attitude_euler"][missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                convert(json.dumps(record), "mavsdk-attitude-json")
        record = attitude()
        record["sdk_version"] = "2.0.0"
        with self.assertRaises(ValueError):
            convert(json.dumps(record), "mavsdk-attitude-json")

    @unittest.skipUnless(protobuf_decoder_available(),
                         "optional pinned protobuf decoder; required CI installs protobuf==6.33.5")
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "the wire proof includes its common leg; required CI supplies the reader")
    def test_saved_protobuf_and_grpc_attitude_reuse_the_adopted_decode_path(self):
        import math
        encoded = euler_wire
        base = dict(sdk_version="3.17.4", capture_time_us=1000,
                    proto_commit="d3cb35f49d1379d2d95a6d22cdbe40ff4074e9bf")
        wire = encoded(12.5, -3.25, 180.0, 42000)
        row = next(csv.DictReader(io.StringIO(convert(json.dumps(
            dict(base, attitude_protobuf_hex=wire.hex())), "mavsdk-attitude-protobuf"))))
        self.assertAlmostEqual(float(row["attitude_roll_rad"]), math.radians(12.5), places=6)
        self.assertEqual(row["attitude_device_time_us"], "42000")
        self.assertEqual(bytes.fromhex(row["protobuf_zero_or_absent_fields_hex"][4:]).decode(), "")
        self.assertEqual(row["protobuf_value_basis"], "PROTO3_DECODED_NOT_SENSOR_VALIDATED")
        level = encoded(None, -3.25, None, 0)
        row = next(csv.DictReader(io.StringIO(convert(json.dumps(
            dict(base, attitude_protobuf_hex=level.hex())), "mavsdk-attitude-protobuf"))))
        self.assertEqual(float(row["attitude_roll_rad"]), 0.0)
        self.assertEqual(bytes.fromhex(row["protobuf_zero_or_absent_fields_hex"][4:]).decode(),
                         "rollDeg,yawDeg,timestampUs")
        inner = bytes([1 << 3 | 2]) + bytes([len(wire)]) + wire
        frame = b"\x00" + len(inner).to_bytes(4, "big") + inner
        output = convert(json.dumps(dict(base, grpc_frame_hex=frame.hex())), "mavsdk-attitude-grpc")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertAlmostEqual(float(row["attitude_yaw_rad"]), math.pi, places=6)
        self.assertEqual(row["protobuf_value_basis"], "GRPC_MESSAGE_NOT_RPC_SUCCESS")
        fields = self.common_fields(output)["observations"][0]["fields"]
        self.assertAlmostEqual(fields["attitude_yaw_rad"], math.pi, places=6)
        self.assertAlmostEqual(fields["attitude_roll_rad"], math.radians(12.5), places=6)
        self.assertEqual(fields["attitude_device_time_us"], 42000)
        self.assertEqual(fields["record_time_us"], 1000)
        self.assertEqual(fields["protobuf_value_basis"], "GRPC_MESSAGE_NOT_RPC_SUCCESS")
        self.assertEqual(fields["protobuf_unknown_fields"], 0)
        self.assertIn("REFERENCE_FRAME_NOT_STATED_BY_SOURCE", fields["attitude_angle_basis"])
        partial = convert(json.dumps(dict(base, attitude_protobuf_hex=level.hex())),
                          "mavsdk-attitude-protobuf")
        zero_fields = self.common_fields(partial)["observations"][0]["fields"]
        self.assertEqual(zero_fields["attitude_roll_rad"], 0.0)
        self.assertEqual(zero_fields["attitude_device_time_us"], 0)
        self.assertEqual(bytes.fromhex(zero_fields["protobuf_zero_or_absent_fields_hex"][4:]).decode(),
                         "rollDeg,yawDeg,timestampUs")
        self.assertIn("same_bytes", self.common_fields(partial)["profile_units"][
            "protobuf_zero_or_absent_fields_hex"])
        for bad in (dict(base, attitude_protobuf_hex="zz"),
                    dict(base, attitude_protobuf_hex=wire.hex()[:-2]),
                    dict(base, attitude_protobuf_hex=wire.hex()[:6]),
                    dict(base, proto_commit="0" * 40, attitude_protobuf_hex=wire.hex())):
            with self.subTest(bad=str(bad)[-40:]), self.assertRaises(ValueError):
                convert(json.dumps(bad), "mavsdk-attitude-protobuf")
        for bad in (
                "01" + frame.hex()[2:],
                "00" + (len(inner) + 1).to_bytes(4, "big").hex() + inner.hex(),
                (b"\x00" + (0).to_bytes(4, "big")).hex(),
                frame.hex()[:8]):
            with self.subTest(frame=bad[:16]), self.assertRaises(ValueError):
                convert(json.dumps(dict(base, grpc_frame_hex=bad)), "mavsdk-attitude-grpc")
