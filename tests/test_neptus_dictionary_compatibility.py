"""The legacy Neptus-bundled IMC dictionary against the adopted passive decoder; authored frames only.

Facts below are quoted from `xml/IMC.xml` (424267 bytes) inside `lib/libimc.jar` (1890705 bytes, git blob
032010d7a252d52d4be876d8d924a2e3a3c9ce06) of the Neptus 2019.10.1 tree
564e44a5aa623eade12ae176cc1fe1388731b919; that entry was read with stdlib zipfile, read-only, under a size
bound, with no Java executed and nothing written back into the archive. The artefact stays in the private
evidence archive: only its hashes appear here. Nothing is loaded at run time.
"""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_imc_sensors import (NEPTUS_SCHEMA, NEPTUS_SENSORS, SENSORS, VECTORS,
                                         VELOCITIES, convert, crc16, velocity_columns)
from tests.test_imc_sensors import frame, stream

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/imc-sensors/profile.toml"
LEGACY_JAR = "libimc.jar 1890705 bytes blob 032010d7a252d52d4be876d8d924a2e3a3c9ce06"
LEGACY_DICTIONARY = "xml/IMC.xml 424267 bytes blob ce66989bbff26059854dc2a4e5b99f61ae690d96 version 5.4.11"

LEGACY_SCALARS = {
    251: ("Voltage", "Voltage", "fp32_t", "V"),
    252: ("Current", "Current", "fp32_t", "A"),
    263: ("Temperature", "Temperature", "fp32_t", "°C"),
    264: ("Pressure", "Pressure", "fp64_t", "hPa"),
    265: ("Depth", "Depth", "fp32_t", "m"),
    267: ("Sound Speed", "SoundSpeed", "fp32_t", "m/s"),
    268: ("Water Density", "WaterDensity", "fp32_t", "kg/m/m/m"),
    270: ("Salinity", "Salinity", "fp32_t", None),
}
LEGACY_VECTORS = {256: ("Angular Velocity", "rad/s"), 257: ("Acceleration", "m/s/s")}
LEGACY_VELOCITIES = {259: ("Ground Velocity", "m/s")}
LEGACY_UNSELECTED = {260: ("Water Velocity", "m/s")}
LEGACY_ABSENT = {364: "Power"}  # 5.4.11 declares no id 364; 360-363 are other messages.
LEGACY_SYNC = 0xFE54
LEGACY_CRC_SENTENCE = ("The check sum field is computed using the CRC-16-IBM with polynomial 0x8005 "
                       "(x^16 + x^15 + x^2 + 1). The data contributing for the CRC includes all "
                       "preceding header and message bytes.")
LEGACY_SENTINEL = {267: "Estimated sound speed. Negative values denote invalid estimates."}


def vector_frame(message, device_time, axes, order="<"):
    payload = struct.pack(order + "dddd", device_time, *axes)
    raw = struct.pack(order + "HHHdHBHB", LEGACY_SYNC, message, len(payload), 1700000000.25,
                      42, 3, 65535, 255) + payload
    return raw + struct.pack(order + "H", crc16(raw))


class NeptusDictionaryCompatibilityTests(unittest.TestCase):

    def test_the_selector_admits_exactly_the_legacy_qualified_ids(self):
        qualified = {message for message, (*_, unit) in LEGACY_SCALARS.items() if unit is not None}
        qualified |= set(LEGACY_VECTORS) | set(LEGACY_VELOCITIES)
        self.assertEqual(set(NEPTUS_SENSORS), qualified,
                         "every id the legacy dictionary declares with a unit, and only those")
        self.assertIsNone(LEGACY_SCALARS[270][3], "5.4.11 declares Salinity with no unit attribute")
        self.assertNotIn(270, NEPTUS_SENSORS)
        self.assertNotIn(364, NEPTUS_SENSORS)
        self.assertEqual(set(LEGACY_ABSENT), {364})
        self.assertNotIn(364, set(LEGACY_SCALARS) | set(LEGACY_VECTORS))
        for message in (251, 252):
            code, field, scale, bias = SENSORS[message]
            self.assertEqual((code, scale, bias), ("f", 1, 0), message)
            self.assertEqual(LEGACY_SCALARS[message][2], "fp32_t")
        for message, (_, unit) in LEGACY_VECTORS.items():
            self.assertIn(message, NEPTUS_SENSORS)
            self.assertTrue(VECTORS[message][0].endswith("rad_s" if unit == "rad/s" else "m_s2"))
        for message, (_, unit) in LEGACY_VELOCITIES.items():
            self.assertIn(message, NEPTUS_SENSORS)
            self.assertEqual(unit, "m/s")
            self.assertTrue(all(column.endswith("_m_s") for column in velocity_columns(message)[:3]))
            self.assertIn("RELATIVE_TO_GROUND", VELOCITIES[message][2])
        self.assertEqual(set(LEGACY_UNSELECTED), {260})
        self.assertNotIn(260, NEPTUS_SENSORS)
        self.assertNotIn(260, set(VELOCITIES))
        self.assertIn("polynomial 0x8005", LEGACY_CRC_SENTENCE)
        self.assertEqual(struct.pack("<H", LEGACY_SYNC), b"\x54\xfe")
        self.assertIn("Negative values denote invalid estimates", LEGACY_SENTINEL[267])

    def test_ids_the_legacy_dictionary_does_not_qualify_stay_retained_with_their_bytes(self):
        data = frame(270, 34.125) + frame(364, 12.5) + frame(263, 20.0)
        output, report = convert(data, schema=NEPTUS_SCHEMA)
        decoded = [int(row["message_id"]) for row in csv.DictReader(io.StringIO(output))]
        self.assertEqual(decoded, [263], "only the qualified id decodes under the legacy selector")
        retained = {entry["message_id"]: entry["frame_hex"] for entry in report["unsupported_frames"]}
        self.assertEqual(sorted(retained), [270, 364])
        self.assertEqual(bytes.fromhex(retained[270]), frame(270, 34.125),
                         "an unqualified id keeps its exact bytes, never a borrowed unit")
        self.assertEqual(bytes.fromhex(retained[364]), frame(364, 12.5))
        self.assertEqual(report["source_frames"], report["decoded_frames"] + len(report["unsupported_frames"]))


    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_the_newly_admitted_ids_reach_common_output_under_the_legacy_selector(self):
        data = (frame(251, 14.25) + frame(252, 2.5)
                + vector_frame(256, 8.25, (0.125, -0.25, 0.5))
                + vector_frame(257, 8.25, (0.75, -9.5, 0.0)))
        output, report = convert(data, schema=NEPTUS_SCHEMA)
        self.assertEqual((report["source_frames"], report["decoded_frames"]), (4, 4))
        self.assertEqual(report["unsupported_frames"], [])
        fields = self.observations(output)
        self.assertAlmostEqual(fields[0]["electrical_voltage_v"], 14.25)
        self.assertAlmostEqual(fields[1]["electrical_current_a"], 2.5)
        self.assertAlmostEqual(fields[2]["reported_angular_velocity_z_rad_s"], 0.5)
        self.assertAlmostEqual(fields[3]["reported_acceleration_y_m_s2"], -9.5)
        for row in fields[2:]:
            self.assertEqual(row["axis_frame_basis"], "UNSPECIFIED_BY_MESSAGE_SCHEMA",
                             "5.4.11 states no frame either")
            self.assertEqual(row["device_clock_basis"], "UNSPECIFIED")
            self.assertAlmostEqual(row["device_time_s"], 8.25)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_a_separate_authored_layout_mixes_admitted_and_retained_ids(self):
        data = (frame(252, -3.125, ">") + frame(270, 35.5, ">") + frame(267, 1495.25, ">")
                + frame(364, 48.0, ">") + frame(263, -2.5, ">"))
        output, report = convert(data, schema=NEPTUS_SCHEMA)
        fields = self.observations(output)
        self.assertEqual([row["message_id"] for row in fields], [252, 267, 263])
        self.assertAlmostEqual(fields[0]["electrical_current_a"], -3.125,
                               msg="the dictionary states no sign convention, so a negative is reported")
        self.assertAlmostEqual(fields[1]["sound_speed_m_s"], 1495.25)
        self.assertAlmostEqual(fields[2]["temperature_k"], 270.65, places=4)
        self.assertEqual(sorted(entry["message_id"] for entry in report["unsupported_frames"]), [270, 364])
        self.assertEqual(report["decoded_frames"], 3)

    def observations(self, output):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            data = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                              str(source), "--allow-equal-time"],
                                             capture_output=True, check=True).stdout)
        self.assertEqual(data["platform_domain"], "Unknown")
        for observation in data["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
        return [observation["fields"] for observation in data["observations"]]


    def test_wrong_payload_width_and_unqualified_selector_still_fail_on_the_legacy_path(self):
        short = struct.pack("<HHHdHBHB", LEGACY_SYNC, 256, 24, 1700000000.25, 42, 3, 65535, 255) + b"\0" * 24
        with self.assertRaises(ValueError):
            convert(short + struct.pack("<H", crc16(short)), schema=NEPTUS_SCHEMA)
        narrow = struct.pack("<HHHdHBHB", LEGACY_SYNC, 264, 4, 1700000000.25, 42, 3, 65535, 255) + b"\0" * 4
        with self.assertRaises(ValueError):
            convert(narrow + struct.pack("<H", crc16(narrow)), schema=NEPTUS_SCHEMA)
        for selector in ("neptus-2019.10", "imc-5.4.11", "libimc", 11):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                convert(frame(263, 20.0), schema=selector)

    def test_the_current_dictionary_default_is_unchanged_by_the_legacy_widening(self):
        output, report = convert(stream())
        decoded = sorted(int(row["message_id"]) for row in csv.DictReader(io.StringIO(output)))
        self.assertEqual(decoded, [263, 264, 265, 267, 268, 270],
                         "the current dictionary qualifies Salinity, which the legacy selector does not")
        self.assertEqual([entry["message_id"] for entry in report["unsupported_frames"]], [65000])
        self.assertEqual(report["decoded_frames"], len(decoded))
        self.assertEqual(report["source_frames"], report["decoded_frames"] + len(report["unsupported_frames"]))


if __name__ == "__main__":
    unittest.main()
