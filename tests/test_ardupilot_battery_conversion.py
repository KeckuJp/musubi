"""Synthetic BAT and cell contracts, not physical battery evidence."""
import copy
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_ardupilot_battery_csv.py"
HEADER = "TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct,Temp,Extra"
ROW = "1001,0,12.5,2,1234,1.25,75,0,opaque"
NAMES = ["battery_voltage_v", "battery_current_a", "battery_consumed_ah",
         "battery_consumed_j", "battery_power_w", "battery_remaining_fraction"]


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.converter = load(SCRIPT)

    def converted(self, text):
        return list(csv.reader(io.StringIO(self.converter.convert(text))))

    def test_reported_details_reuse_and_common_output(self):
        for fmt, instance, suffix in (("ardupilot-bat-4.3", "Instance", ""),
                                      ("ardupilot-bat-inst", "Inst", ",H,SH")):
            header = f"TimeUS,{instance},Volt,Curr,CurrTot,EnrgTot,RemPct,VoltR,Temp,Res,Future{suffix}"
            tail = ",1,85" if suffix else ""
            text = header + "\n1001,2,24,-2,1234,1.25,75,24.5,-12.34,0.02,retained" + tail + "\n"
            text += "1001,3,12,1,0,0,255,nan,0,nan,other" + (",0,0" if suffix else "") + "\n"
            output = self.converter.convert(text, fmt, bat_details=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertAlmostEqual(float(rows[0]["battery_temperature_k"]), 260.81)
            self.assertEqual(rows[0]["battery_resting_voltage_estimate_v"], "24.5")
            self.assertEqual(rows[0]["battery_resistance_estimate_ohm"], "0.02")
            self.assertEqual(rows[0]["battery_temperature_state"], "REPORTED")
            self.assertEqual(rows[1]["battery_temperature_state"], "ZERO_OR_UNAVAILABLE")
            for key in ("battery_temperature_k", "battery_state_of_health_fraction",
                        "battery_resting_voltage_estimate_v", "battery_resistance_estimate_ohm"):
                self.assertEqual(rows[1][key], "")
            self.assertEqual(rows[0]["battery_health_reported"], "HEALTHY" if suffix else "")
            self.assertEqual(rows[0]["battery_state_of_health_fraction"], "0.85" if suffix else "")
            self.assertEqual(rows[1][instance], "3")
            self.assertEqual(rows[1]["Future"], "other")
            self.assertNotIn("battery_temperature_k", self.converter.convert(text, fmt))

    def test_reported_details_reject_bad_fields_and_cli_reuse(self):
        header = "TimeUS,Inst,Volt,Curr,CurrTot,EnrgTot,RemPct,VoltR,Temp,Res,H,SH"
        values = ["1", "0", "24", "1", "2", "3", "50", "24.5", "12.34", ".02", "1", "80"]
        text = header + "\n" + ",".join(values) + "\n"
        for key, bad in (("Temp", "1234"), ("Temp", "0.001"), ("Temp", "-273.16"),
                         ("Temp", "nan"), ("H", "2"), ("SH", "101"), ("Res", "-1"), ("VoltR", "inf")):
            changed = values.copy()
            changed[header.split(",").index(key)] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.converter.convert(header + "\n" + ",".join(changed), "ardupilot-bat-inst", bat_details=True)
        with self.assertRaises(ValueError):
            self.converter.convert(text, "px4-battery", bat_details=True)
        with self.assertRaises(ValueError):
            self.converter.convert(HEADER + "\n" + ROW, "ardupilot-bat-4.3", bat_details=True)
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.csv", Path(directory) / "output.csv"
            source.write_text(text)
            command = [sys.executable, str(SCRIPT), str(source), str(target),
                       "--source-format", "ardupilot-bat-inst", "--bat-details"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(target.read_text(), self.converter.convert(text, "ardupilot-bat-inst", bat_details=True))
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_reported_details_practical_record_count(self):
        count = 30000
        text = "TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct,VoltR,Temp,Res\n"
        text += "".join(f"{i},0,24,2,{i},1,75,24.5,20.25,.02\n" for i in range(count))
        converted = self.converter.convert(text, "ardupilot-bat-4.3", bat_details=True)
        self.assertEqual(len(converted.splitlines()), count + 1)

    def test_cell_banks_use_documented_offset_and_preserve_zero_and_missing(self):
        header = "TimeUS,Volt," + ",".join(f"V{i}" for i in range(1, 11)) + ",Future"
        for bank, fmt in enumerate(self.converter.CELL_FORMATS):
            encoded = [4201, 1, 0, 65535, 4101 + bank, 0, 0, 0, 0, 0]
            source_row = "1001,12.5," + ",".join(map(str, encoded)) + ",retained"
            output = self.converter.convert(header + "\n" + source_row + "\n", fmt)
            row = list(csv.DictReader(io.StringIO(output)))[0]
            self.assertEqual(row["battery_bank_selected"], str(bank))
            self.assertEqual(row["battery_cell_1_v"], "4.2")
            self.assertEqual(row["battery_cell_2_v"], "0.0")
            self.assertEqual(row["battery_cell_3_v"], "")
            self.assertEqual(row["battery_cell_3_status"], "NOT_PROVIDED")
            self.assertEqual(row["battery_cell_4_v"], "65.534")
            self.assertEqual(row["Future"], "retained")
            self.assertEqual(row["battery_current_a"], "")
        for invalid in ["-1", "65536", "1.5", "nan", ""]:
            row = "1001,12.5," + ",".join([invalid] + ["0"] * 9) + ",retained"
            with self.assertRaises(ValueError):
                self.converter.convert(header + "\n" + row, self.converter.CELL_FORMATS[0])

    def test_plane43_cells_reuse_offsets_without_inventing_pack_voltage(self):
        cases = [
            ("ardupilot-bcl-4.3", "TimeUS,Instance,Volt," + ",".join(f"V{i}" for i in range(1, 13)),
             "1001,3,48," + ",".join(map(str, [4201, 1, 0] + [4101] * 9)), "battery_cell_12_v", 4.1),
            ("ardupilot-bcl2-4.3", "TimeUS,Instance,V13,V14", "1001,4,4301,0", "battery_cell_13_v", 4.3),
        ]
        for fmt, header, values, key, expected in cases:
            text = header + "\n" + values + "\n"
            output = self.converter.convert(text, fmt)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row[key]), expected)
            self.assertEqual(row["battery_bank_selected"], "")
            self.assertEqual(row["battery_current_a"], "")
            if fmt == "ardupilot-bcl2-4.3":
                self.assertEqual(row["battery_voltage_v"], "")
                self.assertEqual(row["battery_cell_14_status"], "NOT_PROVIDED")
            else:
                self.assertEqual(row["battery_cell_2_v"], "0.0")
                self.assertEqual(row["battery_cell_3_v"], "")
            for bad in (values.replace(",3,", ",256,").replace(",4,", ",256,"), values + ",extra"):
                with self.assertRaises(ValueError): self.converter.convert(header + "\n" + bad + "\n", fmt)

    def test_units_and_all_original_cells_preserved(self):
        text = HEADER + "\n" + ROW + "\n1001,255,13,-1,2000,2.5,255,,\n"
        result = self.converted(text)
        self.assertEqual(result[0], HEADER.split(",") + NAMES)
        self.assertEqual(result[1][:9], ROW.split(","))
        self.assertEqual(result[2][:9], "1001,255,13,-1,2000,2.5,255,,".split(","))
        self.assertEqual([float(v) for v in result[1][9:]], [12.5, 2, 1.234, 4500, 25, .75])
        self.assertEqual([float(v) for v in result[2][9:-1]], [13, -1, 2, 9000, -13])
        self.assertEqual(result[2][-1], "")
        self.assertFalse(any("temperature" in name for name in result[0]))

    def test_boundary_counters_and_remaining(self):
        result = self.converted(HEADER + "\n0,0,0,0,0,0,0,0,\n"
                                "9223372036854775807,255,1,1,0,0,100,0,\n")
        self.assertEqual([float(result[i][-1]) for i in (1, 2)], [0, 1])
        self.assertEqual(result[2][0], "9223372036854775807")

    def test_reject_invalid_required_values(self):
        invalid = {
            "TimeUS": ["", "-1", "1.0", "1e3", "9223372036854775808", "NaN"],
            "Instance": ["", "-1", "256", "1.5", "NaN"],
            "Volt": ["", "NaN", "inf", "-inf", "bad", "-0.01"],
            "Curr": ["", "NaN", "inf", "-inf", "bad"],
            "CurrTot": ["", "NaN", "inf", "-inf", "bad"],
            "EnrgTot": ["", "NaN", "inf", "-inf", "bad"],
            "RemPct": ["", "-1", "1.5", "101", "254", "256", "NaN"],
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(column=name, value=value):
                    cells = ROW.split(",")
                    cells[HEADER.split(",").index(name)] = value
                    with self.assertRaises(ValueError):
                        self.converter.convert(HEADER + "\n" + ",".join(cells) + "\n")

    def test_inst_voltage_boundary_keeps_signed_current(self):
        header = HEADER.replace("Instance", "Inst")
        for voltage in ("0", "12.5"):
            row = ROW.replace("12.5,2", voltage + ",-2")
            result = list(csv.DictReader(io.StringIO(self.converter.convert(
                header + "\n" + row + "\n", "ardupilot-bat-inst"))))
            self.assertEqual(float(result[0]["battery_current_a"]), -2.0)
            self.assertEqual(float(result[0]["battery_voltage_v"]), float(voltage))
        with self.assertRaises(ValueError):
            self.converter.convert(header + "\n" + ROW.replace("12.5", "-0.01") + "\n", "ardupilot-bat-inst")

    def test_plane43_release_setting_reuses_finite_and_unavailable_current(self):
        for value in (ROW, ROW.replace("12.5,2", "24,-2")):
            text = HEADER + "\n" + value + "\n"
            self.assertEqual(self.converter.convert(text, "ardupilot-bat-4.3"), self.converter.convert(text))
        text = HEADER + "\n1001,0,12.5,NaN,nan,NaN,255,0,retained\n"
        output = self.converter.convert(text, "ardupilot-bat-4.3")
        row = next(csv.DictReader(io.StringIO(output)))
        for name in NAMES[1:]: self.assertEqual(row[name], "")
        self.assertEqual(row["Curr"], "NaN")
        self.assertEqual(row["Extra"], "retained")
        with self.assertRaises(ValueError): self.converter.convert(text)
        with self.assertRaises(ValueError): self.converter.convert(text.replace("12.5", "-1"), "ardupilot-bat-4.3")

    def test_reject_structural_ambiguity(self):
        cases = ["", HEADER + "\n", "not BAT\n1\n", HEADER + "\n" + ROW + ",extra\n",
                 HEADER + "\n" + ROW.rsplit(",", 1)[0] + "\n",
                 HEADER + "\n\n" + ROW + "\n", HEADER + "\n" + ROW + "\0\n",
                 HEADER + "\n" + ROW.replace("opaque", '"opaque"') + "\n",
                 HEADER + ",Volt\n" + ROW + ",12.5\n",
                 HEADER + ",battery_future\n" + ROW + ",1\n",
                 HEADER + ",other (V)\n" + ROW + ",1\n",
                 HEADER.replace("Volt", " Volt") + "\n" + ROW + "\n",
                 HEADER.replace("Volt", "Other") + "\n" + ROW + "\n",
                 HEADER + "\n" + ROW + "\n" + ROW.replace("1001,", "1000,", 1) + "\n"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.converter.convert(text)

    def test_reject_derived_overflow(self):
        for replacement in [ROW.replace("12.5,2", "1e308,1e308"),
                            ROW.replace(",1.25,", ",1e308,")]:
            with self.subTest(row=replacement), self.assertRaises(ValueError):
                self.converter.convert(HEADER + "\n" + replacement + "\n")

    def test_cli_and_invalid_input_does_not_replace_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.csv", Path(directory) / "output.csv"
            source.write_text(HEADER + "\n" + ROW + "\n")
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            valid = target.read_bytes()
            source.write_text(HEADER + "\n" + ROW.replace(",2,", ",NaN,", 1) + "\n")
            result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), valid)




if __name__ == "__main__":
    unittest.main()
