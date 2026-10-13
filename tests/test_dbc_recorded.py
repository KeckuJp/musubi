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
        # Non-choice quantities remain byte-for-value equivalent to legacy scaling.
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


J1939_DBC = b'''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 2364539904 EEC1: 8 ECU
 SG_ EngineSpeed : 24|16@1+ (0.125,0) [0|8031.875] "rpm" ECU
BO_ 2562785040 Command: 8 ECU
 SG_ Level : 0|8@1+ (1,0) [0|100] "%" ECU
BO_ 2566828032 LongBroadcast: 20 ECU
 SG_ Voltage : 0|16@1+ (0.01,0) [0|655.35] "V" ECU
 SG_ Temperature : 152|8@1- (1,0) [-128|127] "degC" ECU
BO_ 2563833600 LongAddressed: 12 ECU
 SG_ Pressure : 0|16@1+ (0.1,0) [0|6553.5] "kPa" ECU
BO_ 2566852613 ProprietaryB: 8 ECU
 SG_ Code : 0|8@1+ (1,0) [0|100] "%" ECU
'''
J1939_HEADER = "time_us,extended_id,data_hex\n"
NAME_A, NAME_B = bytes.fromhex("0102a0118003e0a2"), bytes.fromhex("0503a0118003e0a2")


def frames(rows):
    return J1939_HEADER + "".join(f"{t},{i:08x},{bytes(d).hex()}\n" for t, i, d in rows)


def bam(time, source, pgn, payload):
    count = (len(payload) + 6) // 7
    rows = [(time, 0x1cecff00 | source, bytes([32]) + len(payload).to_bytes(2, "little")
             + bytes([count, 255]) + pgn.to_bytes(3, "little"))]
    padded = payload + b"\xff" * (count * 7 - len(payload))
    rows += [(time + i + 1, 0x1cebff00 | source, bytes([i + 1]) + padded[i * 7:i * 7 + 7]) for i in range(count)]
    return rows


def rts(time, source, destination, pgn, payload, grants):
    count, target = (len(payload) + 6) // 7, pgn.to_bytes(3, "little")
    forward_cm, reverse_cm = 0x1cec0000 | destination << 8 | source, 0x1cec0000 | source << 8 | destination
    padded = payload + b"\xff" * (count * 7 - len(payload))
    rows, following = [(time, forward_cm, bytes([16]) + len(payload).to_bytes(2, "little") + bytes([count, 255]) + target)], 1
    for block in grants:
        rows.append((time, reverse_cm, bytes([17, block, following, 255, 255]) + target))
        for number in range(following, following + block):
            rows.append((time, 0x1ceb0000 | destination << 8 | source, bytes([number]) + padded[number * 7 - 7:number * 7]))
        following += block
    rows.append((time, reverse_cm, bytes([19]) + len(payload).to_bytes(2, "little") + bytes([count, 255]) + target))
    return rows


class J1939Meaning(unittest.TestCase):
    """Authored saved J1939 frames; NAME and addresses are producer reports, not identity."""

    def base_capture(self):
        speed = bytes.fromhex("ffffff401fffffff")  # 8000 * 0.125 = 1000 rpm
        long_broadcast = bytes.fromhex("b004") + bytes(17) + bytes([0xf6])  # 12.00 V, -10 degC
        long_addressed = bytes.fromhex("f503") + bytes(10)  # 101.3 kPa
        return frames([(1, 0x18eeff00, NAME_A), (5, 0x0cf00400, speed), (6, 0x0cf00401, speed),
                       (7, 0x18c02210, b"\x2a" + bytes(7)), (8, 0x18c03311, b"\x2b" + bytes(7)),
                       *bam(10, 0x20, 0xfeb0, long_broadcast),
                       *rts(20, 0x50, 0x60, 0xfeca, bytes(10), [1, 1]),
                       *rts(30, 0x30, 0x40, 0xd000, long_addressed, [2]),
                       (40, 0x18ff1005, b"\x07" + bytes(7)), (41, 0x18ff1006, b"\x07" + bytes(7))])

    def test_pgn_identity_transfers_and_claims_reach_common(self):
        output, report = convert(self.base_capture(), J1939_DBC, extended=True, j1939=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        decoded = [(r["j1939_pgn"], r["j1939_source_address"], r["j1939_destination_address"],
                    r["j1939_transfer_basis"], r["j1939_name_association_status"]) for r in rows]
        self.assertEqual(decoded, [
            ("60928", "0", "255", "", "CLAIMED_EARLIER_IN_THIS_CAPTURE"),
            ("61444", "0", "", "SINGLE_FRAME", "CLAIMED_EARLIER_IN_THIS_CAPTURE"),
            ("61444", "1", "", "SINGLE_FRAME", "NO_PRIOR_CLAIM_IN_CAPTURE"),
            ("49152", "16", "34", "SINGLE_FRAME", "NO_PRIOR_CLAIM_IN_CAPTURE"),
            ("49152", "17", "51", "SINGLE_FRAME", "NO_PRIOR_CLAIM_IN_CAPTURE"),
            ("65200", "32", "255", "BAM", "NO_PRIOR_CLAIM_IN_CAPTURE"),
            ("65200", "32", "255", "BAM", "NO_PRIOR_CLAIM_IN_CAPTURE"),
            ("53248", "48", "64", "SAVED_RTS_CTS_GRANTS_ACK_NOT_PHYSICAL_DELIVERY", "NO_PRIOR_CLAIM_IN_CAPTURE"),
            ("65296", "5", "", "SINGLE_FRAME", "NO_PRIOR_CLAIM_IN_CAPTURE")])
        self.assertEqual([round(float(r["angular_speed_rad_s"]), 6) for r in rows[1:3]], [104.719755] * 2)
        self.assertEqual(rows[1]["j1939_reported_name_hex"], "hex:" + NAME_A.hex())
        self.assertEqual(rows[0]["j1939_name_manufacturer_code"], str((NAME_A[3] << 3) | (NAME_A[2] >> 5)))
        self.assertEqual([round(float(rows[i][f]), 6) for i, f in ((5, "voltage_v"), (6, "temperature_k"), (7, "pressure_pa"))],
                         [12.0, 263.15, 101300.0])
        self.assertEqual(rows[1]["j1939_match_basis"], "PGN_ANY_SOURCE_DBC_SA_IGNORED")
        self.assertEqual(rows[8]["j1939_match_basis"], "EXACT_ID_PROPRIETARY")
        transfers = report["j1939"]["transfers"]
        self.assertEqual([t["basis"] for t in transfers], ["BAM", "SAVED_RTS_CTS_GRANTS_ACK_NOT_PHYSICAL_DELIVERY"])
        self.assertEqual(report["completed_transfers"], len(transfers))  # selected BAM and RTS, each once
        self.assertEqual(rows[7]["j1939_transfer_sha256"], transfers[1]["transfer_sha256"])
        reasons = sorted({u["reason"] for u in report["unsupported"]})
        self.assertEqual(reasons, ["UNSELECTED_ADDRESSED_TRANSPORT", "UNSELECTED_FRAME"])
        self.assertEqual(report["frames"], len(self.base_capture().splitlines()) - 1)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "j1939.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([reader, str(ROOT / "profiles/declared/dbc-recorded/profile.toml"),
                str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], len(rows))
        self.assertEqual(common["observations"][5]["fields"]["j1939_transfer_basis"], "BAM")
        default, _ = convert(self.base_capture(), J1939_DBC, extended=True)
        self.assertNotIn("j1939_pgn", default.splitlines()[0])

    def test_name_contest_move_same_time_and_window_boundary(self):
        speed = bytes.fromhex("ffffff401fffffff")
        output, report = convert(frames([
            (1, 0x18eeff00, NAME_A), (2, 0x18eeff00, NAME_B), (3, 0x0cf00400, speed),
            (4, 0x18eeff07, NAME_A), (5, 0x18eeff09, NAME_B), (6, 0x0cf00400, speed),
            (7, 0x18eeff00, NAME_A), (8, 0x0cf00407, speed), (9, 0x18eeff0a, NAME_B), (9, 0x0cf0040a, speed)]),
            J1939_DBC, extended=True, j1939=True)
        status = [(r["j1939_source_address"], r["j1939_name_association_status"], r["j1939_reported_name_hex"])
                  for r in csv.DictReader(io.StringIO(output)) if r["j1939_pgn"] == "61444"]
        self.assertEqual(status, [("0", "CONTESTED_NO_ARBITRATION_INFERRED", ""),
                                  ("0", "CONTESTED_NO_ARBITRATION_INFERRED", ""),
                                  ("7", "NAME_MOVED_TO_OTHER_ADDRESS", ""),
                                  ("10", "SAME_TIME_AS_CLAIM_ORDER_UNKNOWN", "")])
        self.assertEqual(len(report["j1939"]["claims"]), 6)
        moved, _ = convert(frames([(1, 0x18eeff00, NAME_A), (2, 0x18eeff05, NAME_A), (3, 0x18eeff00, NAME_B),
                                   (4, 0x0cf00400, speed), (5, 0x18eefffe, NAME_B), (6, 0x0cf00400, speed)]),
                           J1939_DBC, extended=True, j1939=True)
        self.assertEqual([r["j1939_name_association_status"] for r in csv.DictReader(io.StringIO(moved))
                          if r["j1939_pgn"] == "61444"], ["ADDRESS_REASSIGNED_IN_CAPTURE", "NAME_MOVED_OR_RELEASED"])
        from scripts.convert_candump_recorded import convert_path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candump.log"
            path.write_text(f"(1700000000.000001) can0 18EEFF00#{NAME_A.hex().upper()}\n"
                            f"(1700000000.000002) can0 0CF00400#{speed.hex().upper()}\n"
                            f"(1700000000.000003) can0 0CF00400#{speed.hex().upper()}\n")
            first, _ = convert_path(path, "can0", start_line=0, stop_line=2, extended=True)
            second, _ = convert_path(path, "can0", start_line=2, stop_line=3, extended=True)
            statuses = [next(r for r in csv.DictReader(io.StringIO(convert(text, J1939_DBC, extended=True, j1939=True)[0]))
                             if r["j1939_pgn"] == "61444")["j1939_name_association_status"] for text in (first, second)]
        self.assertEqual(statuses, ["CLAIMED_EARLIER_IN_THIS_CAPTURE", "NO_PRIOR_CLAIM_IN_CAPTURE"])

    def test_declaration_and_transfer_refusals(self):
        long_broadcast = bytes(20)
        good = J1939_DBC
        dbc_cases = {
            "ambiguous": good + b"BO_ 2364539905 EEC1Again: 8 ECU\n SG_ X : 0|8@1+ (1,0) [0|1] \"%\" ECU\n",
            "network": good + b"BO_ 2565799680 Claim: 8 ECU\n SG_ X : 0|8@1+ (1,0) [0|1] \"%\" ECU\n",
            "too_long": good.replace(b"LongAddressed: 12", b"LongAddressed: 1786"),
            "proprietary_long": good.replace(b"ProprietaryB: 8", b"ProprietaryB: 20")}
        capture = frames([(1, 0x0cf00400, bytes(8))])
        for name, dbc in dbc_cases.items():
            with self.subTest(dbc=name), self.assertRaises(ValueError):
                convert(capture, dbc, extended=True, j1939=True)
        rows = bam(10, 0x20, 0xfeb0, long_broadcast)
        transfer_cases = {
            "size_differs": bam(10, 0x20, 0xfeb0, bytes(21)),
            "missing_packet": rows[:-1],
            "bad_padding": rows[:-1] + [(rows[-1][0], rows[-1][1], rows[-1][2][:-1] + b"\x00")],
            "abort": rts(30, 0x30, 0x40, 0xd000, bytes(12), [1, 1])[:2]
                     + [(31, 0x1cec3040, bytes([255, 1, 255, 255, 255, 0x00, 0xd0, 0x00]))],
            "bad_claim": [(1, 0x18eeff00, NAME_A[:7])]}
        for name, case in transfer_cases.items():
            with self.subTest(transfer=name), self.assertRaises(ValueError):
                convert(frames(case), J1939_DBC, extended=True, j1939=True)
        for options in (dict(extended=False, j1939=True), dict(extended=True, fd=True, j1939=True),
                        dict(extended=True, j1939=1)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                convert(capture, J1939_DBC, **options)
        _, report = convert(frames([(1, 0x18feb020, bytes(8)), (2, 0x0cf00400, bytes(8))]),
                            J1939_DBC, extended=True, j1939=True)
        self.assertEqual([u["reason"] for u in report["unsupported"]], ["MULTI_PACKET_PGN_SENT_AS_SINGLE_FRAME"])


class J1939ReviewCorrections(unittest.TestCase):
    """Root review of 8064122: contested state, row accounting/bounds, transfer epoch, claim form."""
    SPEED = bytes.fromhex("ffffff401fffffff")

    def statuses(self, rows):
        output, _ = convert(frames(rows), J1939_DBC, extended=True, j1939=True)
        return [(r["j1939_name_association_status"], r["j1939_reported_name_hex"])
                for r in csv.DictReader(io.StringIO(output)) if r["j1939_pgn"] == "61444"]

    def test_repeated_or_third_contender_does_not_resolve_a_contest(self):
        name_c = bytes.fromhex("0905a0118003e0a2")
        for tail in ([(3, 0x18eeff00, NAME_B)], [(3, 0x18eeff00, NAME_A)], [(3, 0x18eeff00, name_c)],
                     [(3, 0x18eeff00, NAME_B), (3.5, 0x18eeff00, NAME_B)]):
            with self.subTest(tail=tail):
                rows = [(1, 0x18eeff00, NAME_A), (2, 0x18eeff00, NAME_B)] + [(int(t * 2), i, d) for t, i, d in tail]
                rows = [(t, i, d) for t, i, d in rows] + [(20, 0x0cf00400, self.SPEED)]
                self.assertEqual(self.statuses(sorted(rows, key=lambda r: r[0])),
                                 [("CONTESTED_NO_ARBITRATION_INFERRED", "")])

    def test_claim_only_rows_are_counted_and_every_row_is_bounded(self):
        _, report = convert(frames([(1, 0x18eeff00, NAME_A)]), J1939_DBC, extended=True, j1939=True)
        self.assertEqual((report["observations"], report["conversion_status"]), (1, "OBSERVATIONS_PRODUCED"))
        from unittest.mock import patch
        many = frames([(i, 0x18eeff00 | i % 200, NAME_A[:7] + bytes([i % 200])) for i in range(1, 400)])
        with patch("scripts.convert_dbc_recorded.OUTPUT_LIMIT", 4096), self.assertRaisesRegex(ValueError, "output"):
            convert(many, J1939_DBC, extended=True, j1939=True)
        with patch("scripts.convert_dbc_recorded.J1939_REPORT_LIMIT", 4096), self.assertRaisesRegex(ValueError, "report"):
            convert(many, J1939_DBC, extended=True, j1939=True)

    def test_name_change_during_transfer_is_withheld_not_relabelled(self):
        payload = bytes.fromhex("b004") + bytes(17) + bytes([0xf6])
        announce, *data = bam(10, 0x20, 0xfeb0, payload)
        for change in ((11, 0x18eeff21, NAME_A), (11, 0x18eeff20, NAME_B)):  # move away, or a contest
            with self.subTest(change=change):
                rows = [(1, 0x18eeff20, NAME_A), announce, data[0], change] + [(t + 2, i, d) for t, i, d in data[1:]]
                output, report = convert(frames(rows), J1939_DBC, extended=True, j1939=True)
                row = next(r for r in csv.DictReader(io.StringIO(output)) if r["j1939_pgn"] == "65200")
                self.assertEqual((row["j1939_name_association_status"], row["j1939_reported_name_hex"]),
                                 ("NAME_ASSOCIATION_NOT_CONSTANT_OVER_TRANSFER_WITHHELD", ""))
                transfer = report["j1939"]["transfers"][0]
                self.assertEqual(transfer["start_name_association"][0], "CLAIMED_EARLIER_IN_THIS_CAPTURE")
                self.assertNotEqual(transfer["completion_name_association"][0], "CLAIMED_EARLIER_IN_THIS_CAPTURE")

    def test_only_the_pinned_global_claim_form_is_a_claim(self):
        for identifier in (0x18eeffff, 0x18ee2200, 0x0ceeff00):  # source 255, directed, other priority
            with self.subTest(identifier=hex(identifier)):
                output, report = convert(frames([(1, identifier, NAME_A), (2, 0x0cf00400 | identifier & 0xff, self.SPEED)]),
                                         J1939_DBC, extended=True, j1939=True)
                self.assertEqual(report["j1939"]["claims"], [])
                self.assertEqual([u["reason"] for u in report["unsupported"]], ["UNQUALIFIED_ADDRESS_CLAIM_FORM"]
                                 + (["UNQUALIFIED_SOURCE_ADDRESS"] if identifier & 0xff == 0xff else []))


class J1939BoundaryCorrections(unittest.TestCase):
    """Root review of 6c7d85e: whole-interval NAME and the adopted 0..253 address range."""

    def test_same_time_claim_at_announcement_withholds_transfer_name(self):
        broadcast = bam(10, 0x20, 0xfeb0, bytes.fromhex("b004") + bytes(17) + bytes([0xf6]))
        addressed = rts(10, 0x30, 0x40, 0xd000, bytes.fromhex("f503") + bytes(10), [2])
        for name, transfer, address in (("bam", broadcast, 0x20), ("rts", addressed, 0x30)):
            with self.subTest(transfer=name):
                rows = [(10, 0x18eeff00 | address, NAME_A)] + [(10 if i == 0 else t + 5, ident, d)
                                                               for i, (t, ident, d) in enumerate(transfer)]
                output, report = convert(frames(rows), J1939_DBC, extended=True, j1939=True)
                row = next(r for r in csv.DictReader(io.StringIO(output)) if r["j1939_transfer_basis"] not in ("", "SINGLE_FRAME"))
                self.assertEqual((row["j1939_name_association_status"], row["j1939_reported_name_hex"]),
                                 ("NAME_ASSOCIATION_NOT_CONSTANT_OVER_TRANSFER_WITHHELD", ""))
                self.assertNotEqual(row.get("voltage_v") or row.get("pressure_pa"), "")
                transfer_report = report["j1939"]["transfers"][0]
                self.assertEqual(transfer_report["start_name_association"][0], "SAME_TIME_AS_CLAIM_ORDER_UNKNOWN")
                self.assertEqual(transfer_report["completion_name_association"][0], "CLAIMED_EARLIER_IN_THIS_CAPTURE")

    def test_reserved_or_non_distinct_addresses_do_not_acquire_quantities(self):
        speed = bytes.fromhex("ffffff401fffffff")
        cases = {"single_frame_source_255": [(1, 0x0cf004ff, speed)],
                 "single_frame_source_254": [(1, 0x0cf004fe, speed)],
                 "bam_sender_254": bam(1, 0xfe, 0xfeb0, bytes(20)),
                 "rts_same_address": rts(1, 0x30, 0x30, 0xd000, bytes(12), [2]),
                 "rts_destination_254": rts(1, 0x30, 0xfe, 0xd000, bytes(12), [2])}
        for name, rows in cases.items():
            with self.subTest(case=name):
                output, report = convert(frames(rows), J1939_DBC, extended=True, j1939=True)
                self.assertEqual(report["observations"], 0)
                expected = ({"UNQUALIFIED_SOURCE_ADDRESS"} if "single" in name or "bam" in name
                            else {"UNQUALIFIED_ADDRESSED_PAIR"} if name == "rts_same_address"
                            # the receiver 254 also sends the CTS/ACK frames, each a reserved source
                            else {"UNQUALIFIED_ADDRESSED_PAIR", "UNQUALIFIED_SOURCE_ADDRESS"})
                self.assertEqual({u["reason"] for u in report["unsupported"]}, expected)
        output, report = convert(frames(bam(1, 0x20, 0xfeb0, bytes(20))), J1939_DBC, extended=True, j1939=True)
        self.assertEqual((report["observations"], report["j1939"]["transfers"][0]["basis"]), (2, "BAM"))
