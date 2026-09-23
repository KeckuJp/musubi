"""Synthetic CANopen CC status semantics, not device qualification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dronecan_capture import convert_canopen, convert_canopen_dictionary, convert_canopen_pdo

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,standard_id,data_hex\n"


class CanopenRecordedTests(unittest.TestCase):
    def test_boolean_pdo_reuses_mapping_and_numeric_neighbor(self):
        import sys
        def dictionary(width, reverse=False):
            words = [0x20000000 | width, 0x20010010]
            if reverse:
                words.reverse()
            text = "[1A00]\nParameterName=Map\nObjectType=9\nSubNumber=3\n[1A00sub0]\nParameterName=Count\nDataType=5\nAccessType=ro\nDefaultValue=2\n"
            for ordinal, word in enumerate(words, 1):
                text += f"[1A00sub{ordinal}]\nParameterName=Slot\nDataType=7\nAccessType=ro\nDefaultValue={word}\n"
            return text + "[2000]\nParameterName=Reported flag\nDataType=1\nAccessType=ro\nPDOMapping=1\nUnknownFlag=retained\n[2001]\nParameterName=Voltage\nDataType=6\nAccessType=ro\nPDOMapping=1\nUnit=mV\n"
        for width, reverse, values in ((8, False, (0, 1, 255)), (1, True, (1, 0))):
            configured = dictionary(width, reverse)
            frames = []
            for ordinal, value in enumerate(values):
                voltage = 12000 + ordinal
                word = (voltage | value << 16) if reverse else (value | voltage << width)
                frames.append(f"{10 + ordinal},181,{word.to_bytes(3, 'little').hex()}\n")
            text = HEADER + "".join(frames)
            with self.assertRaises(ValueError):
                convert_canopen_pdo(text, configured, 0x181, 0x1a00, bit_fields=True)
            output, report = convert_canopen_pdo(text, configured, 0x181, 0x1a00,
                                                 bit_fields=width == 1, booleans=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            flags = [row for row in rows if row["data_type_code"] == "1"]
            numeric = [row for row in rows if row["data_type_code"] == "6"]
            self.assertEqual([r["boolean_reported"] for r in flags], ["TRUE" if v else "FALSE" for v in values])
            self.assertEqual([int(r["raw_value"]) for r in flags], list(values))
            self.assertTrue(all(not r["configured_unit"] and not r["configured_factor"] and not r["voltage_v"] for r in flags))
            self.assertEqual(report["output_records"], len(values) * 2)
            self.assertEqual(bytes.fromhex(report["source_document_hex"][4:]).decode(), configured)
            for ordinal, row in enumerate(numeric):
                self.assertAlmostEqual(float(row["voltage_v"]), (12000 + ordinal) / 1000)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory); (path / "input.csv").write_text(text); (path / "map.dcf").write_text(configured)
                command = [sys.executable, str(ROOT / "scripts/convert_dronecan_capture.py"),
                    str(path / "input.csv"), str(path / "out"), "--protocol", "canopen-pdo-python",
                    "--pdo-dictionary", str(path / "map.dcf"), "--pdo-id", "0x181", "--pdo-map", "0x1a00", "--pdo-boolean"]
                if width == 1:
                    command.append("--pdo-bit-fields")
                subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/pdo-profile.toml"),
                        str(path / "out/observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), len(rows))
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    common_flags = [obs["fields"]["boolean_reported"] for obs in common["observations"]
                                    if obs["fields"]["data_type_code"] == 1]
                    self.assertEqual(common_flags, ["TRUE" if v else "FALSE" for v in values])
            for bad in (configured.replace("UnknownFlag=retained", "Unit=V"),
                        configured.replace("UnknownFlag=retained", "Factor=1"), dictionary(2), dictionary(9)):
                with self.assertRaises(ValueError):
                    convert_canopen_pdo(text, bad, 0x181, 0x1a00, bit_fields=True, booleans=True)
            with self.assertRaises(ValueError):
                convert_canopen_pdo(HEADER + "1,181,00\n", configured, 0x181, 0x1a00, bit_fields=True, booleans=True)

    def test_integer_bit_mapping_reuses_units_and_preserves_signed_minimum(self):
        import sys
        def dictionary(signed_bits, unsigned_bits, reverse=False):
            words = [0x20000000 | signed_bits, 0x20010000 | unsigned_bits]
            if reverse:
                words.reverse()
            text = "[1A00]\nParameterName=Map\nObjectType=9\nSubNumber=3\n[1A00sub0]\nParameterName=Count\nDataType=5\nAccessType=ro\nDefaultValue=2\n"
            for index, word in enumerate(words, 1):
                text += f"[1A00sub{index}]\nParameterName=Slot\nDataType=7\nAccessType=ro\nDefaultValue={word}\n"
            return text + "[2000]\nParameterName=Current\nDataType=3\nAccessType=ro\nPDOMapping=1\nUnit=mA\n[2001]\nParameterName=Voltage\nDataType=6\nAccessType=ro\nPDOMapping=1\nUnit=mV\n"
        for signed_bits, unsigned_bits, current, volts, reverse in (
                (4, 12, -8, 1234, False), (4, 8, -1, 129, True)):
            configured = dictionary(signed_bits, unsigned_bits, reverse)
            signed_raw = current & ((1 << signed_bits) - 1)
            word = (volts | signed_raw << unsigned_bits) if reverse else (signed_raw | volts << signed_bits)
            total = signed_bits + unsigned_bits
            if total % 8:
                word |= 0xf << total
            raw = word.to_bytes((total + 7) // 8, "little").hex()
            text = HEADER + f"7,181,{raw}\n8,181,{raw}\n"
            with self.assertRaises(ValueError):
                convert_canopen_pdo(text, configured, 0x181, 0x1a00)
            output, report = convert_canopen_pdo(text, configured, 0x181, 0x1a00, bit_fields=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["output_records"], 4)
            self.assertEqual(report["retained_padding_bits"], (-total) % 8)
            by_object = {int(row["object_index"]): row for row in rows}
            self.assertAlmostEqual(float(by_object[0x2000]["current_a"]), current / 1000)
            self.assertEqual(int(by_object[0x2000]["raw_value"]), current)
            self.assertAlmostEqual(float(by_object[0x2001]["voltage_v"]), volts / 1000)
            self.assertEqual(int(rows[1]["mapped_bit_offset"]), unsigned_bits if reverse else signed_bits)
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_frame_hex"][4:]))[2], raw)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory); (path / "input.csv").write_text(text); (path / "device.dcf").write_text(configured)
                command = [sys.executable, str(ROOT / "scripts/convert_dronecan_capture.py"),
                    str(path / "input.csv"), str(path / "out"), "--protocol", "canopen-pdo-python",
                    "--pdo-dictionary", str(path / "device.dcf"), "--pdo-id", "0x181", "--pdo-map", "0x1a00", "--pdo-bit-fields"]
                self.assertEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/pdo-profile.toml"),
                        str(path / "out/observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), 4)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            with self.assertRaises(ValueError):
                convert_canopen_pdo(HEADER + "7,181,00\n", configured, 0x181, 0x1a00, bit_fields=True)
        for bad in (dictionary(0, 16), dictionary(17, 16),
                    dictionary(4, 32).replace("DataType=6", "DataType=8")):
            with self.assertRaises(ValueError):
                convert_canopen_pdo(HEADER + "7,181,0000000000\n", bad, 0x181, 0x1a00, bit_fields=True)

    def test_float_pdo_mapping_reports_bit_range_units_and_nonfinite_bits(self):
        """Byte-aligned REAL32/REAL64 only: the pinned reference can read no other float layout."""
        import math
        import struct

        def dictionary(words, objects):
            text = ("[1A00]\nParameterName=Map\nObjectType=9\nSubNumber=%d\n[1A00sub0]\n"
                    "ParameterName=Count\nDataType=5\nAccessType=ro\nDefaultValue=%d\n"
                    % (len(words) + 1, len(words)))
            for position, word in enumerate(words, 1):
                text += (f"[1A00sub{position}]\nParameterName=Slot\nDataType=7\nAccessType=ro\n"
                         f"DefaultValue=0x{word:08X}\n")
            for index, (datatype, unit, extra) in objects.items():
                text += (f"[{index:04X}]\nParameterName=Object{index:X}\nDataType={datatype}\n"
                         f"AccessType=ro\nPDOMapping=1\nUnit={unit}\n{extra}")
            return text

        set_a = dictionary([0x20000020, 0x20010010],
                           {0x2000: (8, "V", ""), 0x2001: (3, "degC", "Factor=0.1\n")})
        set_b = dictionary([0x20000010, 0x20010020],
                           {0x2000: (3, "degC", "Factor=0.1\n"), 0x2001: (8, "V", "")})
        for selected, volts, temp, float_offset in ((set_a, 24.5, 250, 0), (set_b, -3.25, -100, 16)):
            payload = (struct.pack("<fh", volts, temp) if float_offset == 0
                       else struct.pack("<hf", temp, volts))
            text = HEADER + f"7,181,{payload.hex()}\n8,181,{payload.hex()}\n"
            output, report = convert_canopen_pdo(text, selected, 0x181, 0x1a00)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["frames"], report["decoded_frames"], report["output_records"]),
                             (2, 2, 4))
            floats = [row for row in rows if row["data_type_code"] == "8"]
            others = [row for row in rows if row["data_type_code"] == "3"]
            self.assertEqual((len(floats), len(others)), (2, 2))
            self.assertEqual(float(floats[0]["voltage_v"]), volts)
            self.assertEqual(float(floats[0]["raw_value"]), volts)
            self.assertEqual(floats[0]["quantity_status"], "REPORTED_CONFIGURED_QUANTITY")
            self.assertAlmostEqual(float(others[0]["temperature_k"]), temp / 10 + 273.15)
            self.assertEqual((int(floats[0]["mapped_bit_offset"]), int(floats[0]["mapped_bit_width"])),
                             (float_offset, 32))
            self.assertEqual(int(others[0]["mapped_bit_width"]), 16)
            self.assertEqual((report["mapped_bits"], report["retained_padding_bits"]), (48, 0))
            covered = sorted((int(r["mapped_bit_offset"]), int(r["mapped_bit_width"])) for r in rows[:2])
            self.assertEqual(covered, [(0, 16), (16, 32)] if float_offset else [(0, 32), (32, 16)])
            self.assertEqual(sum(width for _, width in covered), report["mapped_bits"])
            self.assertEqual(len(payload) * 8, report["mapped_bits"])
            retained = bytes.fromhex(json.loads(bytes.fromhex(floats[0]["source_frame_hex"][4:]))[2])
            start = int(floats[0]["mapped_bit_offset"]) // 8
            self.assertEqual(struct.unpack("<f", retained[start:start + 4])[0], volts)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"
                    path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/pdo-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(len(common["observations"]), 4)
                fields = common["observations"][0 if float_offset == 0 else 1]["fields"]
                self.assertEqual(fields["voltage_v"], volts)
                self.assertEqual(fields["mapped_bit_offset"], float_offset)
                self.assertEqual(fields["mapped_bit_width"], 32)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                units = common["profile_units"]
                for column in ("mapped_bit_offset", "mapped_bit_width", "raw_value", "quantity_status",
                               "data_type_code", "configured_unit", "configured_factor", "pdo_slot",
                               "object_index", "object_subindex", "object_name_hex",
                               "dictionary_sha256", "pdo_cob_id", "pdo_mapping_index",
                               "source_frame_hex"):
                    self.assertTrue(units.get(column), f"{column} must declare its meaning")
                self.assertIn("multiple_of_8", units["mapped_bit_offset"])
                self.assertIn("never_replaced_by_zero", units["raw_value"])
                self.assertIn("not_a_standard_CANopen_guarantee", units["configured_unit"])
                self.assertIn("required_to_be_exactly_1_for_a_float", units["configured_factor"])

        wide = dictionary([0x20000040], {0x2000: (17, "V", "")})
        output, report = convert_canopen_pdo(
            HEADER + f"1,181,{struct.pack('<d', -12.75).hex()}\n", wide, 0x181, 0x1a00)
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual((float(row["voltage_v"]), row["data_type_code"]), (-12.75, "17"))
        self.assertEqual((int(row["mapped_bit_offset"]), int(row["mapped_bit_width"])), (0, 64))
        self.assertEqual((report["mapped_bits"], report["retained_padding_bits"]), (64, 0))

        for value in (float("nan"), float("inf"), float("-inf")):
            payload = struct.pack("<fh", value, 250)
            output, _ = convert_canopen_pdo(HEADER + f"1,181,{payload.hex()}\n", set_a, 0x181, 0x1a00)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(row["voltage_v"], "")
            self.assertEqual(row["raw_value"], "NONFINITE")
            self.assertEqual(row["quantity_status"], "NONFINITE_RETAINED")
            retained = bytes.fromhex(json.loads(bytes.fromhex(row["source_frame_hex"][4:]))[2])
            recovered = struct.unpack("<f", retained[:4])[0]
            self.assertEqual(struct.pack("<f", recovered), struct.pack("<f", value))
            self.assertFalse(math.isfinite(recovered))

        boundary = "float PDO mapping requires byte alignment and the full declared width"
        for words, objects, bits in (
                ([0x20000004, 0x20010020], {0x2000: (3, "mA", ""), 0x2001: (8, "V", "")}, True),
                ([0x20000010], {0x2000: (8, "V", "")}, True),
                ([0x20000020], {0x2000: (17, "V", "")}, True),
                ([0x20000010], {0x2000: (8, "V", "")}, False)):
            selected = dictionary(words, objects)
            with self.subTest(words=words, bit_fields=bits), self.assertRaises(ValueError) as caught:
                convert_canopen_pdo(HEADER + "1,181,0000000000000000\n", selected, 0x181, 0x1a00,
                                    bit_fields=bits)
            self.assertEqual(str(caught.exception), boundary)
        for selected, reason in (
                (set_a.replace("Unit=V", "Unit=V\nFactor=2"), "invalid or ambiguous physical factor"),
                (set_a.replace("Unit=V", "Unit=guessed"), "qualified engineering unit required"),
                (dictionary([0x20000040, 0x20010020], {0x2000: (17, "V", ""), 0x2001: (8, "V", "")}),
                 "mapping exceeds classic PDO")):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                convert_canopen_pdo(HEADER + f"1,181,{struct.pack('<fh', 1.5, 2).hex()}\n",
                                    selected, 0x181, 0x1a00)
            self.assertEqual(str(caught.exception), reason)
        with self.assertRaises(ValueError) as caught:
            convert_canopen_pdo(HEADER + "1,181,0000c441\n", set_a, 0x181, 0x1a00)
        self.assertEqual(str(caught.exception), "PDO size does not match selected mapping")

    def test_compact_array_dictionary_and_pdo_reuse(self):
        template = """[2000]
ParameterName=Voltage
ObjectType=8
CompactSubObj=200
DataType=6
AccessType=ro
PDOMapping=1
Unit=mV
DefaultValue=0
LowLimit=100
UnknownFlag=retained
[2000Name]
NrOfEntries=1
15=Auxiliary
[2000Value]
NrOfEntries=1
15=12345
[2000Denotation]
NrOfEntries=1
15=External reported supply
"""
        output, report = convert_canopen_dictionary(template, 123)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 202)
        bysub = {r["object_subindex"]: r for r in rows}
        decoded = lambda key: json.loads(bytes.fromhex(bysub[str(key)]["source_section_hex"][4:]))
        self.assertEqual(decoded(0)["DefaultValue"], "200")
        self.assertEqual(decoded(0)["DataType"], "5")
        self.assertEqual(decoded(0)["PDOMapping"], "0")
        self.assertEqual(decoded(200)["ParameterName"], "Voltage200")
        self.assertEqual(decoded(15)["ParameterName"], "Auxiliary")
        self.assertEqual(decoded(15)["ParameterValue"], "12345")
        self.assertEqual(decoded(15)["Denotation"], "External reported supply")
        self.assertNotIn("ParameterValue", decoded(14))
        self.assertNotIn("LowLimit", decoded(15))
        self.assertEqual(decoded(15)["UnknownFlag"], "retained")
        self.assertEqual(bytes.fromhex(report["source_document_hex"][4:]).decode(), template)
        mapping = """[1A00]
ParameterName=Mapping
ObjectType=9
SubNumber=2
[1A00sub0]
ParameterName=Count
DataType=5
AccessType=ro
DefaultValue=1
[1A00sub1]
ParameterName=Slot
DataType=7
AccessType=ro
DefaultValue=0x20000F10
"""
        for selected, raw, expected in ((template, "e02e", 12),
                                        (template.replace("Unit=mV", "Unit=V\nFactor=0.01"), "c409", 25)):
            converted, details = convert_canopen_pdo(HEADER + f"1,181,{raw}\n", selected + mapping, 0x181, 0x1a00)
            row = next(csv.DictReader(io.StringIO(converted)))
            self.assertEqual(float(row["voltage_v"]), expected)
            self.assertEqual(row["object_subindex"], "15")
            self.assertNotEqual(float(row["raw_value"]), 12345)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(converted)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/pdo-profile.toml"),
                        str(path)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["voltage_v"], expected)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/dictionary-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(len(common["observations"]), 202)
        for bad in (template.replace("CompactSubObj=200", "CompactSubObj=255"),
                    template.replace("CompactSubObj=200", "CompactSubObj=200\nSubNumber=201"),
                    template.replace("15=Auxiliary", "201=Auxiliary"),
                    template.replace("NrOfEntries=1", "NrOfEntries=2"),
                    template.replace("15=Auxiliary", "015=Auxiliary"),
                    template + "[2000sub0]\nParameterName=Conflict\nDataType=5\nAccessType=ro\n",
                    template + "[2000name]\nNrOfEntries=0\n"):
            with self.assertRaises(ValueError): convert_canopen_dictionary(bad, 1)

    def test_compact_record_mapping_equals_the_expanded_dictionary(self):
        """DS306 V1.3 4.6.3.2 RECORD** / 4.6.3.4.2: one template, so a compact map decodes identically."""
        import math
        import struct
        variables = """[2000]
ParameterName=Voltage
DataType=6
AccessType=ro
PDOMapping=1
Unit=mV
[2001]
ParameterName=Temperature
DataType=3
AccessType=ro
PDOMapping=1
Unit=degC
Factor=0.1
"""
        second = """[2100]
ParameterName=Current
DataType=7
AccessType=ro
PDOMapping=1
Unit=mA
[2101]
ParameterName=Angle
DataType=3
AccessType=ro
PDOMapping=1
Unit=deg
[2102]
ParameterName=Load
DataType=5
AccessType=ro
PDOMapping=1
Unit=%
"""

        def expanded(index, words, names):
            text = (f"[{index:04X}]\nParameterName=Transmit mapping\nObjectType=9\n"
                    f"SubNumber={len(words) + 1}\n[{index:04X}sub0]\nParameterName=NrOfObjects\n"
                    f"DataType=5\nAccessType=ro\nDefaultValue={len(words)}\n")
            for position, (word, name) in enumerate(zip(words, names), start=1):
                text += (f"[{index:04X}sub{position}]\nParameterName={name}\nDataType=7\n"
                         f"AccessType=ro\nDefaultValue=0x{word:08X}\n")
            return text

        def compact(index, words, *, parameter_values=False):
            text = (f"[{index:04X}]\nParameterName=Transmit mapping\nObjectType=9\n"
                    f"CompactSubObj={len(words)}\nDataType=7\nAccessType=ro\n")
            if not parameter_values:
                text = text.replace("AccessType=ro\n", f"AccessType=ro\nDefaultValue=0x{words[0]:08X}\n")
            text += f"[{index:04X}Value]\nNrOfEntries={len(words)}\n"
            for position, word in enumerate(words, start=1):
                text += f"{position}=0x{word:08X}\n"
            return text

        sets = (
            (0x1A00, (0x20000010, 0x20010010), ("Voltage slot", "Temperature slot"), variables,
             struct.pack("<Hh", 12500, -125), {"voltage_v": 12.5, "temperature_k": 273.15 - 12.5}),
            (0x1A01, (0x21000020, 0x21010010, 0x21020008), ("Current slot", "Angle slot", "Load slot"),
             second, struct.pack("<IhB", 1500, -90, 55),
             {"current_a": 1.5, "angle_rad": math.radians(-90), "reported_ratio": 0.55}),
        )
        for mapping_index, words, names, declarations, payload, expectations in sets:
            capture = HEADER + f"1,181,{payload.hex()}\n"
            reference, reference_report = convert_canopen_pdo(
                capture, declarations + expanded(mapping_index, words, names), 0x181, mapping_index)
            for dictionary in (declarations + compact(mapping_index, words),
                               declarations + compact(mapping_index, words, parameter_values=True)):
                converted, report = convert_canopen_pdo(capture, dictionary, 0x181, mapping_index)
                self.assertNotEqual(report["dictionary_sha256"], reference_report["dictionary_sha256"])
                compacted, expected = (list(csv.DictReader(io.StringIO(text)))
                                       for text in (converted, reference))
                self.assertEqual(len(compacted), len(words))
                for row, reference_row in zip(compacted, expected):
                    self.assertEqual({k: v for k, v in row.items() if k != "dictionary_sha256"},
                                     {k: v for k, v in reference_row.items() if k != "dictionary_sha256"})
                for field, value in expectations.items():
                    self.assertAlmostEqual(float(compacted[list(expectations).index(field)][field]),
                                           value, places=6)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "out.csv"; path.write_text(converted)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/dronecan-recorded/pdo-profile.toml"),
                            str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), len(words))
                    for field, value in expectations.items():
                        observed = [o["fields"].get(field) for o in common["observations"]]
                        self.assertAlmostEqual([v for v in observed if v is not None][0], value,
                                               places=6)

        index, words = sets[0][0], sets[0][1]
        output, _ = convert_canopen_dictionary(compact(index, words), 7)
        rows = {row["object_subindex"]: row for row in csv.DictReader(io.StringIO(output))}
        decoded = lambda key: json.loads(bytes.fromhex(rows[key]["source_section_hex"][4:]))
        self.assertEqual(rows[""]["object_kind"], "RECORD")  # the parent is still a RECORD
        self.assertEqual(rows[""]["declaration_status"], "COMPACT_TEMPLATE_EXPANDED")
        self.assertEqual((decoded("0")["ParameterName"], decoded("0")["DataType"],
                          decoded("0")["AccessType"], decoded("0")["DefaultValue"],
                          decoded("0")["PDOMapping"]), ("NrOfObjects", "5", "ro", "2", "0"))
        for position in ("1", "2"):
            self.assertEqual(rows[position]["object_kind"], "VARIABLE")
            self.assertEqual(rows[position]["data_type_reported"], "UNSIGNED32")
            self.assertEqual(rows[position]["declaration_status"],
                             "COMPACT_EXPANDED_NOT_DEVICE_VERIFIED")
            self.assertEqual(decoded(position)["ParameterValue"], f"0x{words[int(position) - 1]:08X}")
            self.assertEqual(decoded(position)["DefaultValue"], f"0x{words[0]:08X}")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "dictionary.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/dictionary-profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(len(common["observations"]), 4)
            units = common["profile_units"]
            self.assertIn("COMPACT", units["declaration_status"])
            self.assertIn("not_device_verified", units["declaration_status"].lower())

        template = compact(0x1A00, sets[0][1])
        for bad in (template.replace("CompactSubObj=2", "CompactSubObj=255"),
                    template.replace("CompactSubObj=2", "CompactSubObj=2\nSubNumber=3"),
                    template + "[1A00sub1]\nParameterName=Conflict\nDataType=7\nAccessType=ro\n",
                    template + "[1A00value]\nNrOfEntries=0\n",
                    template.replace("NrOfEntries=2", "NrOfEntries=3"),
                    template.replace("2=0x", "3=0x"),
                    "[DeviceInfo]\nCompactPDO=0x3\nNrOfTXPDO=2\n" + template):
            with self.assertRaises(ValueError):
                convert_canopen_dictionary(bad, 7)
        convert_canopen_dictionary("[DeviceInfo]\nCompactPDO=0\n" + template, 7)
        oversize = variables + compact(0x1A00, (0x20000010,) * 9)
        with self.assertRaises(ValueError):
            convert_canopen_pdo(HEADER + "1,181,0000\n", oversize, 0x181, 0x1a00)
        unexpanded = variables + template.replace("DataType=7\n", "")
        self.assertEqual([row["declaration_status"] for row in csv.DictReader(io.StringIO(
            convert_canopen_dictionary(unexpanded, 7)[0])) if row["object_index"] == "6656"],
            ["COMPACT_NOT_EXPANDED"])
        with self.assertRaises(ValueError):
            convert_canopen_pdo(HEADER + f"1,181,{sets[0][4].hex()}\n", unexpanded, 0x181, 0x1a00)

    def test_pdo_dictionary_units_reuse_and_practical_record_count(self):
        import hashlib
        import struct
        dictionary = """[1A00]
ParameterName=Transmit mapping
ObjectType=9
SubNumber=3
[1A00sub0]
ParameterName=Count
DataType=5
AccessType=ro
DefaultValue=2
[1A00sub1]
ParameterName=Voltage slot
DataType=7
AccessType=ro
DefaultValue=0x20000010
[1A00sub2]
ParameterName=Temperature slot
DataType=7
AccessType=ro
DefaultValue=0x20010010
[2000]
ParameterName=Voltage
DataType=6
AccessType=ro
PDOMapping=1
Unit=mV
UnknownField=preserved
[2001]
ParameterName=Temperature
DataType=3
AccessType=ro
PDOMapping=1
Unit=degC
Factor=0.1
"""
        for count, volts, temp in ((10000, 24000, -100), (2, 12500, 250)):
            selected = dictionary
            if count == 2:
                selected = selected.replace("DefaultValue=0x20000010", "DefaultValue=0x20000010\nParameterValue=0x20010010")
                selected = selected.replace("DefaultValue=0x20010010\n[2000]", "DefaultValue=0x20010010\nParameterValue=0x20000010\n[2000]")
            data = struct.pack("<Hh", volts, temp) if count == 10000 else struct.pack("<hH", temp, volts)
            text = HEADER + "".join(f"{index},181,{data.hex()}\n" for index in range(count)) + f"{count},701,05\n"
            output, report = convert_canopen_pdo(text, selected, 0x181, 0x1a00)
            self.assertEqual((report["frames"], report["decoded_frames"], report["output_records"]), (count + 1, count, count * 2))
            self.assertEqual(len(report["unsupported_frames"]), 1)
            self.assertEqual(bytes.fromhex(report["source_document_hex"][4:]).decode(), selected)
            rows = list(csv.DictReader(io.StringIO(output)))
            voltage_row, temp_row = (rows[0], rows[1]) if count == 10000 else (rows[1], rows[0])
            self.assertEqual(float(voltage_row["voltage_v"]), volts / 1000)
            self.assertAlmostEqual(float(temp_row["temperature_k"]), temp / 10 + 273.15)
            self.assertEqual(voltage_row["dictionary_sha256"], "sha256:" + hashlib.sha256(selected.encode()).hexdigest())
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/pdo-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), count * 2)
                    self.assertEqual(common["observations"][-1]["clock_basis"], "Unknown")
        for bad in (dictionary.replace("Unit=mV", "Unit=guessed"), dictionary.replace("Factor=0.1", "Factor=nan"),
                    dictionary.replace("DefaultValue=0x20000010", "DefaultValue=$NODEID+1"),
                    dictionary.replace("DefaultValue=0x20000010", "DefaultValue=0x20000008"),
                    dictionary.replace("PDOMapping=1", "PDOMapping=0")):
            with self.assertRaises(ValueError): convert_canopen_pdo(HEADER + "1,181,c05d9cff\n", bad, 0x181, 0x1a00)
        for body in ("1,181,00\n", "1,702,05\n", "2,181,c05d9cff\n1,181,c05d9cff\n"):
            with self.assertRaises(ValueError): convert_canopen_pdo(HEADER + body, dictionary, 0x181, 0x1a00)
        floating = dictionary.replace("DefaultValue=0x20000010", "DefaultValue=0x20000020").replace("DataType=6", "DataType=8").replace("Unit=mV", "Unit=V")
        for value in (24.5, float("nan")):
            data = struct.pack("<fh", value, 250).hex()
            output, report = convert_canopen_pdo(HEADER + f"1,181,{data}\n", floating, 0x181, 0x1a00)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(json.loads(bytes.fromhex(row["source_frame_hex"][4:]))[2], data)
            self.assertEqual(row["voltage_v"], "24.5" if value == 24.5 else "")
            self.assertEqual(row["quantity_status"], "REPORTED_CONFIGURED_QUANTITY" if value == 24.5 else "NONFINITE_RETAINED")
        with self.assertRaises(ValueError):
            convert_canopen_pdo(HEADER + "1,181,0000c441fa00\n", floating.replace("Unit=V", "Unit=V\nFactor=2"), 0x181, 0x1a00)

    def test_dictionary_two_inputs_typed_declarations_and_common_output(self):
        for name, datatype, expected in (("One", "0x10", "INTEGER24"), ("Two", "8", "REAL32")):
            text = f"""[DeviceInfo]
VendorName=Example
[2000]
ParameterName={name}
DataType={datatype}
AccessType=rw
PDOMapping=1
DefaultValue=$NODEID+0x200
Unit=vendor_native
UnknownFlag=retained
[3000]
ParameterName=Record
ObjectType=9
SubNumber=1
[3000sub0]
ParameterName=Entries
DataType=5
AccessType=ro
[4000]
ParameterName=Unknown
DataType=0x9999
AccessType=ro
"""
            if name == "Two":
                text = text.replace("UnknownFlag=retained", "UnknownFlag=retained\nParameterValue=123")
            output, report = convert_canopen_dictionary(text, 123000)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["data_type_reported"], expected)
            self.assertEqual(rows[0]["pdo_mappable_declared"], "1")
            self.assertEqual(rows[2]["object_subindex"], "0")
            self.assertEqual(rows[3]["data_type_reported"], "UNKNOWN")
            self.assertEqual(rows[3]["declared_bit_width"], "")
            original = json.loads(bytes.fromhex(rows[0]["source_section_hex"][4:]))
            self.assertEqual(original["DefaultValue"], "$NODEID+0x200")
            self.assertEqual(original["UnknownFlag"], "retained")
            self.assertEqual(original.get("ParameterValue"), "123" if name == "Two" else None)
            self.assertEqual(bytes.fromhex(report["source_document_hex"][4:]).decode(), text)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/dronecan-recorded/dictionary-profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 4)
                    self.assertEqual(common["observations"][0]["fields"]["object_index"], 8192)
                    self.assertIsNone(common["observations"][0]["anchor_unix_us"])

    def test_dictionary_invalid_duplicate_and_compact_boundary(self):
        valid = "[2000]\nParameterName=A\nDataType=1\nAccessType=ro\n"
        row = next(csv.DictReader(io.StringIO(convert_canopen_dictionary(valid, 1)[0])))
        self.assertEqual(row["declared_bit_width"], "8")
        for text in (valid + valid, valid.replace("DataType=1", "DataType=bad"),
                     valid + "PDOMapping=2\n", "[DEFAULT]\nDataType=1\n" + valid,
                     valid + "[2000sub0]\nParameterName=Orphan\nDataType=1\nAccessType=ro\n",
                     valid + "[2000sub100]\nParameterName=Invalid\n",
                     "[2000]\nParameterName=Container\nObjectType=9\nSubNumber=1\n"):
            with self.assertRaises(ValueError):
                convert_canopen_dictionary(text, 1)
        compact = "[2000]\nParameterName=Array\nObjectType=8\nCompactSubObj=5\n"
        self.assertIn("COMPACT_NOT_EXPANDED", convert_canopen_dictionary(compact, 1)[0])
        with self.assertRaises(ValueError):
            convert_canopen_dictionary(valid, None)
    def test_default_emergency_report_semantics_and_opt_in(self):
        text = HEADER + "1,081,001005aabbccddee\n2,081,0000040000000000\n3,701,05\n"
        old, old_report = convert_canopen(text)
        self.assertEqual(old_report["decoded_frames"], 1)
        output, report = convert_canopen(text, include_emergency=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["decoded_frames"], 3)
        self.assertEqual(rows[0]["canopen_error_code"], "4096")
        self.assertEqual(rows[0]["canopen_error_voltage_reported"], "1")
        self.assertEqual(rows[0]["canopen_error_current_reported"], "0")
        self.assertEqual(rows[0]["canopen_manufacturer_data_hex"], "hex:aabbccddee")
        self.assertEqual(rows[1]["canopen_error_report_kind"], "ERROR_RESET_REPORTED")
        self.assertEqual(rows[1]["canopen_error_voltage_reported"], "1")
        self.assertEqual(rows[2]["canopen_error_code"], "")
        self.assertEqual(rows[0]["canopen_error_report_kind"], "ERROR_REPORTED")
        self.assertEqual(rows[0]["canopen_error_register"], "5")
        self.assertEqual(rows[0]["canopen_error_generic_reported"], "1")
        self.assertEqual(rows[0]["canopen_error_register_disposition"], "ALL_DEFINED_REGISTER_BITS")
        for column in ("temperature", "communication", "device_profile", "reserved", "manufacturer"):
            self.assertEqual(rows[0][f"canopen_error_{column}_reported"], "0", column)
        self.assertEqual(rows[0]["source_node_id"], "1")
        self.assertEqual(rows[0]["record_time_us"], "1")
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_frames_hex"][4:]))[0][2],
                         "001005aabbccddee")
        self.assertEqual(rows[1]["canopen_error_code"], "0")
        self.assertEqual(rows[1]["canopen_error_register_disposition"], "ALL_DEFINED_REGISTER_BITS")
        reserved = HEADER + "4,081,0000c00000000000\n"
        kept, kept_report = convert_canopen(reserved, include_emergency=True)
        reserved_row = next(csv.DictReader(io.StringIO(kept)))
        self.assertEqual(kept_report["decoded_frames"], 1)
        self.assertEqual(reserved_row["canopen_error_register"], "192")
        self.assertEqual(reserved_row["canopen_error_reserved_reported"], "1")
        self.assertEqual(reserved_row["canopen_error_manufacturer_reported"], "1")
        self.assertEqual(reserved_row["canopen_error_register_disposition"],
                         "RESERVED_BIT6_SET_NONCONFORMANT_TRANSMITTER_REPORT_RETAINED")
        self.assertEqual(reserved_row["canopen_error_report_kind"], "ERROR_RESET_REPORTED")
        self.assertEqual(reserved_row["canopen_manufacturer_data_hex"], "hex:0000000000")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"
                path.write_text(output)
                result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                    str(path)], check=True, capture_output=True).stdout)
                self.assertEqual(result["main_rows"], 3)
                self.assertEqual(result["observations"][0]["fields"]["canopen_error_voltage_reported"], 1)
                self.assertEqual(result["observations"][0]["fields"]["canopen_error_register"], 5)
                self.assertEqual(result["observations"][0]["fields"]["canopen_error_register_disposition"],
                                 "ALL_DEFINED_REGISTER_BITS")
                units = result["profile_units"]
                for column in ("canopen_error_code", "canopen_error_report_kind", "canopen_error_register",
                               "canopen_error_register_disposition", "canopen_manufacturer_data_hex",
                               "canopen_error_generic_reported", "canopen_error_current_reported",
                               "canopen_error_voltage_reported", "canopen_error_temperature_reported",
                               "canopen_error_communication_reported",
                               "canopen_error_device_profile_reported", "canopen_error_reserved_reported",
                               "canopen_error_manufacturer_reported"):
                    self.assertTrue(units.get(column), f"{column} must declare its meaning")
                self.assertIn("never_normality_all_faults_cleared_or_healthy_hardware",
                              units["canopen_error_report_kind"])
                self.assertIn("never_a_diagnosed_cause", units["canopen_error_register"])
                self.assertIn("no_vendor_dictionary_lookup_happens_here",
                              units["canopen_manufacturer_data_hex"])
                self.assertIn("never_becomes_a_named_fault", units["canopen_error_code"])
                self.assertIn("never_interpreted_as_a_named_or_vendor_fault",
                              units["canopen_error_reserved_reported"])
        for body in ("1,081,00\n", "1,081,001005aabbccdd\n"):
            with self.assertRaises(ValueError):
                convert_canopen(HEADER + body, include_emergency=True)

    def test_states_accounting_unknown_and_common_output(self):
        text = HEADER + "1,701,00\n1,702,04\n2,703,05\n3,704,7f\n4,705,06\n5,123,abcd\n"
        output, report = convert_canopen(text)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["node_reported_mode"] for r in rows],
                         ["BOOT_UP", "STOPPED", "OPERATIONAL", "PRE_OPERATIONAL", "UNKNOWN_6"])
        self.assertEqual(report["frames"], report["decoded_frames"] + len(report["unsupported_frames"]))
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_frames_hex"][4:])), [["1", "701", "00"]])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"
                path.write_text(output)
                result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(result["main_rows"], 5)
                self.assertEqual(result["observations"][2]["fields"]["node_reported_mode"], "OPERATIONAL")

    def test_reject_wrong_kind_length_reserved_bit_and_time(self):
        for text in [HEADER + "1,701,\n", HEADER + "1,701,0000\n", HEADER + "1,701,85\n",
                     HEADER + "2,701,05\n1,701,05\n", HEADER + "1,800,05\n",
                     HEADER + "1,700,05\n", HEADER.replace("standard_id", "extended_id") + "1,701,05\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                convert_canopen(text)
