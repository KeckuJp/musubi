"""Authored synthetic DBC and independent literal CAN inputs, not real captures."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_dbc_recorded import convert

ROOT = Path(__file__).resolve().parents[1]
DBC = b'''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 256 Measurements: 4 ECU
 SG_ Voltage : 0|16@1+ (0.01,0) [0|655.35] "V" ECU
 SG_ Temperature : 16|8@1- (1,0) [-128|127] "degC" ECU
 SG_ Vendor : 24|8@1+ (1,0) [0|255] "mystery" ECU
'''
HEADER = "time_us,standard_id,data_hex\n"


class DbcSignals(unittest.TestCase):
    def test_zero_observations_remain_diagnostic_not_successful_adaptation(self):
        self.assertEqual(convert(HEADER + "1,100,e803f602\n", DBC)[1]["conversion_status"], "OBSERVATIONS_PRODUCED")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, dictionary = root / "capture.csv", root / "signals.dbc"
            source.write_text(HEADER + "1,200,00000000\n")
            dictionary.write_bytes(DBC)
            result = subprocess.run([sys.executable, "-m", "scripts.convert_dbc_recorded",
                str(source), str(dictionary), str(root / "out.csv"), str(root / "report.json")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not a successful adaptation", result.stderr)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["conversion_status"], "NO_SUPPORTED_OBSERVATIONS")
            self.assertEqual(report["observations"], 0)
            self.assertEqual(len(report["unsupported"]), 1)
            self.assertEqual(len(list(csv.DictReader(io.StringIO((root / "out.csv").read_text())))), 0)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_multi_signal_practical_capacity_reuses_common_output(self):
        count = 10000
        dbc = ('VERSION ""\nNS_ :\nBS_:\nBU_: ECU\nBO_ 256 Many: 8 ECU\n' + ''.join(
            f' SG_ V{index} : {index * 4}|4@1+ (1,0) [0|15] "V" ECU\n' for index in range(16))).encode()
        capture = HEADER + ''.join(f"{index},100,21436587a9cbed0f\n" for index in range(count))
        output, report = convert(capture, dbc)
        self.assertLess(len(capture.encode()), 16 * 1024 * 1024)
        self.assertGreater(len(output.encode()), 16 * 1024 * 1024)
        self.assertEqual((report["frames"], report["decoded_frames"], report["observations"]),
                         (count, count, count * 16))
        rows = csv.DictReader(io.StringIO(output))
        expected = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0]
        for index, row in enumerate(rows):
            self.assertEqual(float(row["voltage_v"]), expected[index % 16])
            self.assertEqual(row["source_data_hex"], "hex:21436587a9cbed0f")
            self.assertEqual(row["signal_name_hex"], "hex:" + f"V{index % 16}".encode().hex())
        self.assertEqual(index + 1, count * 16)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "out.csv"; source.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], count * 16)
            self.assertEqual(len(common["observations"]), count * 16)
            self.assertEqual(common["observations"][-1]["fields"]["voltage_v"], 0)

    def test_saved_fd_capture_reuses_decoder_to_common_output(self):
        from scripts.convert_candump_recorded import convert as candump
        for length, extended, flags in ((12, False, "1"), (64, True, "f")):
            message_id = 2147483904 if extended else 256
            identifier = "00000100" if extended else "100"
            dbc = (f'''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ {message_id} Measurements: {length} ECU
 SG_ Voltage : 0|16@1+ (0.01,0) [0|655.35] "V" ECU
 SG_ Temperature : {(length - 1) * 8}|8@1- (1,0) [-128|127] "degC" ECU
BA_DEF_ BO_ "VFrameFormat" ENUM "StandardCAN_FD","ExtendedCAN_FD";
BA_ "VFrameFormat" BO_ {message_id} {int(extended)};
''').encode()
            logs, payloads = [], []
            for index, (volts, temperature) in enumerate(((1000, -10), (2000, 20))):
                payload = bytearray(range(length)); payload[:2] = volts.to_bytes(2, "little"); payload[-1] = temperature & 255
                payloads.append(payload.hex())
                logs.append(f"(1700000000.00000{index}) can0 {identifier}##{flags}{payload.hex()}")
            logs.append(f"(1700000000.000002) can0 {identifier}#0001")
            capture, capture_report = candump("\n".join(logs), "can0", extended=extended, fd=True)
            self.assertEqual(capture_report["selected_frames"], 2)
            self.assertEqual(capture_report["records"][-1]["reason"], "UNSELECTED_CLASSIC")
            output, report = convert(capture, dbc, extended=extended, fd=True, include_enums=True)
            self.assertEqual(report["observations"], 4)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(rows[0]["voltage_v"]), 10.)
            self.assertAlmostEqual(float(rows[3]["temperature_k"]), 293.15)
            self.assertEqual(rows[0]["source_data_hex"], "hex:" + payloads[0])
            self.assertEqual(int(rows[0]["fd_flags_reported"]), int(flags, 16))
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), 4)
                    self.assertEqual(common["observations"][0]["fields"]["fd_flags_reported"], int(flags, 16))
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            with self.assertRaises(ValueError): convert(capture, dbc, extended=extended)
            wrong_kind = dbc.replace(b"StandardCAN_FD", b"StandardCAN").replace(b"ExtendedCAN_FD", b"ExtendedCAN")
            with self.assertRaises(ValueError): convert(capture, wrong_kind, extended=extended, fd=True)
            short = capture.replace(payloads[0], payloads[0][:-2])
            with self.assertRaises(ValueError): convert(short, dbc, extended=extended, fd=True)
            with self.assertRaises(ValueError):
                candump(f"(1700000000.000000) can0 {identifier}##1" + "00" * 65, "can0", extended=extended, fd=True)

    def test_raw_choice_labels_and_numeric_reuse(self):
        dbc = DBC + b'VAL_ 256 Voltage 1000 "Idle" 2000 "Active";\n'
        text = HEADER + "1,100,e803f602\n2,100,d0071403\n3,100,b80b1403\n"
        output, report = convert(text, dbc, include_enums=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["observations"], 6)
        labels = rows[::2]
        self.assertEqual([int(row["enum_raw_code"]) for row in labels], [1000, 2000, 3000])
        self.assertEqual([row["enum_status"] for row in labels], ["DECLARED_LABEL", "DECLARED_LABEL", "UNKNOWN_CODE"])
        self.assertEqual([bytes.fromhex(row["enum_label_hex"][4:]).decode() for row in labels[:2]], ["Idle", "Active"])
        self.assertTrue(all(row["voltage_v"] == "" for row in labels))
        self.assertAlmostEqual(float(rows[1]["temperature_k"]), 263.15)
        self.assertEqual(report["frames"], report["decoded_frames"])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(len(common["observations"]), 6)
                self.assertEqual(common["observations"][4]["fields"]["enum_raw_code"], 3000)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        old = list(csv.DictReader(io.StringIO(convert(text, DBC)[0])))
        current = list(csv.DictReader(io.StringIO(convert(text, DBC, include_enums=True)[0])))
        for before, after in zip(old, current):
            self.assertEqual(before, {key: after[key] for key in before})

    def test_choice_multiplex_branch_and_signed_code(self):
        dbc = b'''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 256 Multiplexed: 2 ECU
 SG_ Select M : 0|8@1+ (1,0) [0|255] "" ECU
 SG_ First m0 : 8|8@1- (2,10) [-128|127] "" ECU
 SG_ Second m1 : 8|8@1+ (1,0) [0|255] "V" ECU
VAL_ 256 Select 0 "First branch" 1 "Second branch";
VAL_ 256 First -1 "Missing" 2 "Ready";
'''
        output, report = convert(HEADER + "1,100,00ff\n2,100,010c\n", dbc, include_enums=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["observations"], 4)
        self.assertFalse(report["unsupported"])
        self.assertEqual(rows[1]["enum_raw_code"], "-1")
        self.assertEqual(bytes.fromhex(rows[1]["enum_label_hex"][4:]).decode(), "Missing")
        self.assertEqual(float(rows[3]["voltage_v"]), 12.)
        from cantools.database.errors import DecodeError
        with self.assertRaises(DecodeError):
            convert(HEADER + "1,100,02ff\n", dbc, include_enums=True)

    def test_shared_unit_table_without_quantity_role_inference(self):
        cases = [("mV", "voltage_v", .01), ("mA", "current_a", .01),
                 ("mm", "distance_m", .01), ("cm", "distance_m", .1),
                 ("m/s", "speed_m_s", 10), ("km/h", "speed_m_s", 25/9),
                 ("rad", "angle_rad", 10), ("deg", "angle_rad", math.pi/18),
                 ("rad/s", "angular_speed_rad_s", 10), ("deg/s", "angular_speed_rad_s", math.pi/18),
                 ("Pa", "pressure_pa", 10), ("hPa", "pressure_pa", 1000),
                 ("kPa", "pressure_pa", 10000), ("bar", "pressure_pa", 1000000),
                 ("%", "reported_ratio", .1), ("J", "energy_j", 10), ("Wh", "energy_j", 36000)]
        for unit, field, expected in cases:
            dbc = DBC.replace(b'"V"', ('"' + unit + '"').encode())
            output, report = convert(HEADER + "1,100,e8030000\n", dbc)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertAlmostEqual(float(row[field]), expected)
            self.assertEqual(report["observations"], 2)
        for frame, speed in (("e8030000", 25/9), ("d0070000", 50/9)):
            output, _ = convert(HEADER + "1,100," + frame + "\n", DBC.replace(b'"V"', b'"km/h"'))
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertAlmostEqual(common["observations"][0]["fields"]["speed_m_s"], speed)

    def test_cli_does_not_overwrite_prior_input_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source, dbc, report = [Path(directory) / name for name in ("in.csv", "in.dbc", "report.json")]
            text = HEADER + "1,100,e803f602\n"
            source.write_text(text)
            dbc.write_bytes(DBC)
            result = subprocess.run([sys.executable, "-m", "scripts.convert_dbc_recorded",
                str(source), str(dbc), str(source), str(report)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_text(), text)
            self.assertFalse(report.exists())

    def test_finite_unit_set_extended_and_enum_boundary(self):
        for unit, field, expected in [(b"A", "current_a", 10), (b"m", "distance_m", 10),
                (b"rpm", "angular_speed_rad_s", math.pi / 3),
                (b"K", "temperature_k", 10), (b"degK", "temperature_k", 10)]:
            dbc = DBC.replace(b'"V"', b'"' + unit + b'"').replace(b"BO_ 256", b"BO_ 2147483904")
            output, report = convert(HEADER.replace("standard_id", "extended_id") +
                "1,00000100,e8030000\n", dbc, extended=True)
            self.assertAlmostEqual(float(next(csv.DictReader(io.StringIO(output)))[field]), expected)
            self.assertEqual(report["observations"], 2)
        output, report = convert(HEADER + "1,100,e8030000\n",
            DBC + b'VAL_ 256 Voltage 1000 "Missing";\n')
        self.assertEqual(report["observations"], 1)
        self.assertEqual(len(report["unsupported"]), 2)

    def test_units_accounting_and_second_input_common_output(self):
        for frame, voltage, kelvin in [("e803f602", 10, 263.15), ("d0071403", 20, 293.15)]:
            output, report = convert(HEADER + "10,100," + frame + "\n11,101,00\n", DBC)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["frames"], 2)
            self.assertEqual(report["decoded_frames"], 1)
            self.assertEqual(report["observations"], 2)
            self.assertEqual(len(report["unsupported"]), 2)
            self.assertEqual(float(rows[0]["voltage_v"]), voltage)
            self.assertAlmostEqual(float(rows[1]["temperature_k"]), kelvin)
            self.assertEqual(rows[0]["source_data_hex"], "hex:" + frame)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"
                    path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], 2)
                    self.assertEqual(result["observations"][0]["fields"]["voltage_v"], voltage)

    def test_big_endian_and_important_rejections(self):
        dbc = DBC.replace(b"0|16@1+", b"7|16@0+")
        output, _ = convert(HEADER + "1,100,03e81400\n", dbc)
        self.assertEqual(float(next(csv.DictReader(io.StringIO(output)))["voltage_v"]), 10)
        for body in ("1,100,00\n", "1,100,0000000000\n", "2,100,e803f602\n1,100,e803f602\n"):
            with self.assertRaises(ValueError):
                convert(HEADER + body, DBC)
        with self.assertRaises(ValueError):
            convert(HEADER + "1,100,e803f602\n", DBC + DBC[DBC.index(b"BO_"):])
