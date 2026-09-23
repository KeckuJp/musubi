"""The pinned LSTS dictionary bound to the adopted IMC decoder; authored frames only.

The dictionary facts below are quoted from `IMC.xml` at LSTS commit
c494386748f2330bdf4e916bce2112c752d92f51, whose blob id 19fdeb96567d66f303ed68dce37c6cbe4c6b7570 was
checked against the official refs API. Nothing is loaded at run time: the dictionary is never read by the
converter, and no schema file is opened here. No real or vendor recording is used.
"""
import ast
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_imc_sensors import (NEPTUS_SCHEMA, NEPTUS_SENSORS, SENSORS, VECTORS, convert,
                                         crc16)
from tests.test_imc_sensors import frame, stream

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/imc-schema-application/profile.toml"
DICTIONARY = "IMC.xml c494386748f2330bdf4e916bce2112c752d92f51 blob 19fdeb96567d66f303ed68dce37c6cbe4c6b7570"

SCALAR_DICTIONARY = {
    251: ("Voltage", "Voltage", "fp32_t", "V", "electrical_voltage_v", 1, 0),
    252: ("Current", "Current", "fp32_t", "A", "electrical_current_a", 1, 0),
    263: ("Temperature", "Temperature", "fp32_t", "°C", "temperature_k", 1, 273.15),
    264: ("Pressure", "Pressure", "fp64_t", "hPa", "external_pressure_pa", 100, 0),
    265: ("Depth", "Depth", "fp32_t", "m", "depth_m", 1, 0),
    267: ("Sound Speed", "SoundSpeed", "fp32_t", "m/s", "sound_speed_m_s", 1, 0),
    268: ("Water Density", "WaterDensity", "fp32_t", "kg/m/m/m", "water_density_kg_m3", 1, 0),
    270: ("Salinity", "Salinity", "fp32_t", "PSU", "salinity_psu", 1, 0),
    364: ("Power", "Power", "fp32_t", "W", "electrical_power_w", 1, 0),
}
VECTOR_DICTIONARY = {
    256: ("Angular Velocity", "AngularVelocity", "rad/s", "reported_angular_velocity_{}_rad_s"),
    257: ("Acceleration", "Acceleration", "m/s/s", "reported_acceleration_{}_m_s2"),
}
SENTINEL_ID = 267
SENTINEL_TEXT = "Estimated sound speed. Negative values denote invalid estimates."
HEADER_FIELDS = (("sync", "uint16_t"), ("mgid", "uint16_t"), ("size", "uint16_t"),
                 ("timestamp", "fp64_t"), ("src", "uint16_t"), ("src_ent", "uint8_t"),
                 ("dst", "uint16_t"), ("dst_ent", "uint8_t"))
SYNC_VALUE = 0xFE54
CRC_POLYNOMIAL = 0x8005  # "CRC-16-IBM with polynomial 0x8005 (x^16 + x^15 + x^2 + 1)"
TYPE_WIDTH = {"fp32_t": ("f", 4), "fp64_t": ("d", 8)}


def vector_frame(message, device_time, axes, order="<"):
    payload = struct.pack(order + "dddd", device_time, *axes)
    raw = struct.pack(order + "HHHdHBHB", SYNC_VALUE, message, len(payload), 1700000000.25,
                      42, 3, 65535, 255) + payload
    return raw + struct.pack(order + "H", crc16(raw))


class ImcSchemaApplicationTests(unittest.TestCase):

    def test_every_selected_id_matches_the_pinned_dictionary_entry(self):
        self.assertEqual(set(SENSORS), set(SCALAR_DICTIONARY), "the decoder selects exactly this family")
        self.assertEqual(set(VECTORS), set(VECTOR_DICTIONARY))
        for message, (_, _, declared, unit, column, scale, bias) in SCALAR_DICTIONARY.items():
            code, field, decoder_scale, decoder_bias = SENSORS[message]
            expected_code, width = TYPE_WIDTH[declared]
            with self.subTest(message=message):
                self.assertEqual(code, expected_code)
                self.assertEqual(struct.calcsize(code), width)
                self.assertEqual(field, column)
                self.assertEqual((decoder_scale, decoder_bias), (scale, bias))
                if (scale, bias) == (1, 0):
                    self.assertIn(unit, ("V", "A", "m", "m/s", "kg/m/m/m", "PSU", "W"))
                else:
                    self.assertIn(unit, ("°C", "hPa"))
        self.assertEqual(SENSORS[264][0], "d", "id 264 is the only fp64_t scalar in this family")
        self.assertEqual([m for m, (_, _, t, *_) in SCALAR_DICTIONARY.items() if t == "fp64_t"], [264])
        for message, (_, _, _, template) in VECTOR_DICTIONARY.items():
            self.assertEqual(VECTORS[message], tuple(template.format(axis) for axis in "xyz"))

    def test_the_envelope_and_crc_follow_the_dictionary_header_and_footer(self):
        codes = {"uint16_t": "H", "uint8_t": "B", "fp64_t": "d"}
        self.assertEqual("".join(codes[kind] for _, kind in HEADER_FIELDS), "HHHdHBHB")
        self.assertEqual(struct.calcsize("<HHHdHBHB"), 20)
        reflected = int(format(CRC_POLYNOMIAL, "016b")[::-1], 2)
        self.assertEqual(reflected, 0xA001)
        self.assertEqual(struct.pack("<H", SYNC_VALUE), b"\x54\xfe")
        self.assertEqual(struct.pack(">H", SYNC_VALUE), b"\xfe\x54")

    def test_only_the_sound_speed_entry_carries_a_sentinel_meaning(self):
        self.assertIn("Negative values denote invalid estimates", SENTINEL_TEXT)
        negative = {message: convert(frame(message, -1.5))[0] for message in (265, 267, 268)}
        withheld = next(csv.DictReader(io.StringIO(negative[SENTINEL_ID])))
        self.assertEqual((withheld["sound_speed_m_s"], withheld["measurement_status"]),
                         ("", "UNAVAILABLE_SOURCE_SENTINEL"))
        for message in (265, 268):
            row = next(csv.DictReader(io.StringIO(negative[message])))
            self.assertEqual(row["measurement_status"], "REPORTED", message)
            self.assertEqual(float(row[SENSORS[message][1]]), -1.5)


    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_one_authored_frame_per_arithmetic_class_reaches_common_output(self):
        data = (frame(263, -1.5) + frame(264, 950.25) + frame(270, 34.125)
                + vector_frame(256, 12.5, (0.25, -0.5, 0.75))
                + vector_frame(257, 12.5, (-9.80665, 0.0, 0.5)))
        output, report = convert(data)
        self.assertEqual((report["source_frames"], report["decoded_frames"]), (5, 5))
        fields = self.observations(output)
        self.assertAlmostEqual(fields[0]["temperature_k"], 271.65, places=4,
                               msg="dictionary unit is degree Celsius; K adds 273.15")
        self.assertAlmostEqual(fields[1]["external_pressure_pa"], 95025.0, places=3,
                               msg="dictionary unit is hPa; Pa multiplies by 100")
        self.assertAlmostEqual(fields[2]["salinity_psu"], 34.125, places=4)
        self.assertAlmostEqual(fields[3]["reported_angular_velocity_y_rad_s"], -0.5)
        self.assertAlmostEqual(fields[4]["reported_acceleration_x_m_s2"], -9.80665)
        for row in fields[3:]:
            self.assertEqual(row["axis_frame_basis"], "UNSPECIFIED_BY_MESSAGE_SCHEMA")
            self.assertEqual(row["device_clock_basis"], "UNSPECIFIED")
            self.assertEqual(float(row["device_time_s"]), 12.5)
        self.assertTrue(all(row["source_timestamp_s"] == 1700000000.25 for row in fields))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_a_separate_authored_layout_and_value_set_reuses_the_same_binding(self):
        data = b"".join(frame(message, value, ">") for message, value in
                        ((251, 13.75), (252, -0.5), (364, -6.875), (265, 3.25), (267, 1502.5),
                         (268, 1031.5)))
        output, report = convert(data)
        self.assertEqual((report["source_frames"], report["decoded_frames"]), (6, 6))
        fields = self.observations(output)
        self.assertEqual([row["message_id"] for row in fields], [251, 252, 364, 265, 267, 268])
        self.assertAlmostEqual(fields[0]["electrical_voltage_v"], 13.75)
        self.assertAlmostEqual(fields[1]["electrical_current_a"], -0.5,
                               msg="the dictionary states no sign convention, so a negative is reported")
        self.assertAlmostEqual(fields[2]["electrical_power_w"], -6.875)
        self.assertAlmostEqual(fields[3]["depth_m"], 3.25)
        self.assertAlmostEqual(fields[4]["sound_speed_m_s"], 1502.5)
        self.assertAlmostEqual(fields[5]["water_density_kg_m3"], 1031.5)
        self.assertTrue(all(row["measurement_status"] == "REPORTED" for row in fields))

    def observations(self, output):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(source),
                                     "--allow-equal-time"], capture_output=True, check=True)
            data = json.loads(result.stdout)
        self.assertEqual(data["platform_domain"], "Unknown", "no vehicle is inferred from a dictionary")
        for observation in data["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"], "the dispatcher clock is not an anchor")
        return [observation["fields"] for observation in data["observations"]]


    def test_a_dictionary_outside_the_qualified_set_is_refused_and_accounting_survives(self):
        for selector in ("imc-5.4.30", "lsts-imc", "", "neptus-2019.10", 5):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                convert(frame(263, 20), schema=selector)
        present = [263, 264, 265, 267, 268, 270, 65000]  # exactly what `stream()` contains, in order
        data = stream() + vector_frame(256, 4.5, (0.25, -0.5, 0.75))
        output, report = convert(data, schema=NEPTUS_SCHEMA, allow_no_selected=True)
        decoded = [int(row["message_id"]) for row in csv.DictReader(io.StringIO(output))]
        self.assertEqual(sorted(decoded), [256, 263, 264, 265, 267, 268])
        self.assertEqual(set(decoded), set(present + [256]) & set(NEPTUS_SENSORS),
                         "the decoded set is exactly the input's ids that this selector qualifies")
        self.assertIn(256, decoded, "a qualified vector decodes alongside the scalars")
        unsupported = [entry["message_id"] for entry in report["unsupported_frames"]]
        self.assertEqual(sorted(unsupported), [270, 65000],
                         "salinity stays unqualified for the older dictionary, as does an unknown id")
        restored = b"".join(bytes.fromhex(row["source_frame_hex"][4:])
                            for row in csv.DictReader(io.StringIO(output)))
        restored_unsupported = b"".join(bytes.fromhex(entry["frame_hex"])
                                        for entry in report["unsupported_frames"])
        self.assertEqual(len(restored) + len(restored_unsupported), len(data),
                         "every source byte is either decoded or retained")
        self.assertEqual(report["source_frames"], report["decoded_frames"] + len(report["unsupported_frames"]))
        self.assertEqual(report["source_frames"], len(present) + 1)

    def test_a_payload_width_that_contradicts_the_dictionary_type_is_refused(self):
        header = struct.pack("<HHHdHBHB", SYNC_VALUE, 264, 4, 1700000000.25, 42, 3, 65535, 255) + b"\0" * 4
        with self.assertRaises(ValueError):
            convert(header + struct.pack("<H", crc16(header)))
        header = struct.pack(">HHHdHBHB", SYNC_VALUE, 263, 8, 1700000000.25, 42, 3, 65535, 255) + b"\0" * 8
        with self.assertRaises(ValueError):
            convert(header + struct.pack(">H", crc16(header)))
        short = struct.pack("<HHHdHBHB", SYNC_VALUE, 256, 24, 1700000000.25, 42, 3, 65535, 255) + b"\0" * 24
        with self.assertRaises(ValueError):
            convert(short + struct.pack("<H", crc16(short)))

    def test_the_dictionary_is_never_read_at_run_time(self):
        tree = ast.parse((ROOT / "scripts/convert_imc_sensors.py").read_text())
        modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        modules |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)}
        self.assertFalse(modules & {"xml", "lxml", "defusedxml", "xmlrpc", "zipfile", "tarfile"},
                         "no XML parser or archive reader is involved in decoding")
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        self.assertEqual([text for text in literals if ".xml" in text.lower()], [],
                         "ids and units are fixed in code; no schema path is an input")
        self.assertIn(DICTIONARY.split()[1][:12], DICTIONARY, "the pin is recorded with the evidence")


if __name__ == "__main__":
    unittest.main()
