"""Independent finite PX4 battery subset tests; no flight captures are accessed."""
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
HEADER = "timestamp,id,voltage_v,current_a,discharged_mah,remaining,temperature"
ROW = "1001,1,12.5,2,1234,0.75,nan"
DERIVED = ["battery_voltage_v", "battery_current_a", "battery_consumed_ah",
           "battery_consumed_j", "battery_power_w", "battery_remaining_fraction"]


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConversionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "post-build CI supplies common reader")
    def test_reported_reserve_reuses_common_and_preserves_default(self):
        for rows in (("1001,1,12.5,2,1234,0.75,nan,135.25", "1001,1,12.5,0,0,0,nan,0"),
                     ("2000,2,24,3,1,0.5,nan,42", "2001,2,24,3,1,0.5,nan,nan")):
            text = HEADER + ",time_remaining_s\n" + "\n".join(rows) + "\n"
            default = self.converter.convert(text, "px4-battery")
            self.assertEqual(default, self.converter.convert(text, "px4-battery", px4_remaining_time=False))
            output = self.converter.convert(text, "px4-battery", px4_remaining_time=True)
            parsed = list(csv.reader(io.StringIO(output)))
            self.assertEqual([r[:-2] for r in parsed], list(csv.reader(io.StringIO(default))))
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); source = base / 'input.csv'; target = base / 'out.csv'
                source.write_text(text); target.write_text(output)
                completed = subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/px4-battery/profile.toml'),
                    str(target), '--allow-equal-time', '--preserve-nonfinite-as-text'], capture_output=True)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                common = json.loads(completed.stdout)
                self.assertEqual(common['main_rows'], 2)
                for row, observed in zip(rows, common['observations']):
                    raw = row.split(',')[-1]; fields = observed['fields']
                    self.assertEqual(fields['battery_time_remaining_s'], None if raw == 'nan' else float(raw))
                    self.assertIsNone(observed['anchor_unix_us'])
                    self.assertEqual(observed['clock_basis'], 'BootRelative')
                subprocess.run([sys.executable, str(SCRIPT), str(source), str(base / 'cli.csv'),
                    '--source-format','px4-battery','--px4-remaining-time'], check=True, capture_output=True)
                self.assertEqual((base / 'cli.csv').read_text(), output)
        for raw in ('-1', 'inf', '-inf', '', '3.5e38', '1e-400'):
            with self.assertRaises(ValueError):
                self.converter.convert(HEADER + ',time_remaining_s\n' + ROW + ',' + raw,
                                       'px4-battery', px4_remaining_time=True)
        for flag in ('true', 1, None):
            with self.assertRaises(ValueError): self.converter.convert('', 'px4-battery', px4_remaining_time=flag)
        with self.assertRaises(ValueError): self.converter.convert(HEADER + '\n' + ROW, 'px4-battery', px4_remaining_time=True)
        with self.assertRaises(ValueError): self.converter.convert('', 'ardupilot-bat', px4_remaining_time=True)

    @classmethod
    def setUpClass(cls):
        cls.converter = load(SCRIPT)

    def converted(self, rows):
        text = HEADER + "\n" + "\n".join(rows) + "\n"
        return list(csv.reader(io.StringIO(self.converter.convert(text, source_format="px4-battery"))))

    def test_measured_units_signed_current_no_energy_and_raw_nonfinite_preserved(self):
        rows = [ROW, "1001,255,12.5,-2,0,0,inf", "2000,0,12.5,0,1000,1,-inf"]
        converted = self.converted(rows)
        self.assertEqual(converted[0], HEADER.split(",") + DERIVED)
        for index, row in enumerate(rows, 1):
            self.assertEqual(converted[index][:7], row.split(","))
            self.assertEqual(converted[index][10], "")
        self.assertEqual([float(v) for i, v in enumerate(converted[1][7:]) if i != 3],
                         [12.5, 2, 1.234, 25, .75])
        self.assertEqual(float(converted[2][11]), -25)
        self.assertEqual(float(converted[3][11]), 0)

    def test_missing_sentinels_and_power_dependency(self):
        rows = ["0,0,0,-1,-1,-1,", "0,1,12,-1,0,0,", "9223372036854775807,255,0,2,1,1,"]
        converted = self.converted(rows)
        self.assertEqual(converted[1][7:], [""] * 6)
        self.assertEqual([converted[i][11] for i in (1, 2, 3)], ["", "", ""])
        self.assertEqual(float(converted[2][9]), 0)
        self.assertEqual(float(converted[3][8]), 2)
        self.assertEqual(converted[3][0], "9223372036854775807")

    def test_invalid_selected_fields_and_ranges_reject(self):
        invalid = {0: ["", "-1", "1.0", "1e3", "9223372036854775808"],
                   1: ["", "-1", "256", "1.5"],
                   2: ["", "-1", "NaN", "inf", "bad"],
                   3: ["", "NaN", "inf", "bad"],
                   4: ["", "-2", "-.5", "NaN", "inf", "bad"],
                   5: ["", "-2", "-.5", "1.01", "NaN", "inf", "bad"]}
        for index, values in invalid.items():
            for value in values:
                row = ROW.split(",")
                row[index] = value
                with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                    self.converted([",".join(row)])
        with self.assertRaises(ValueError):
            self.converted(["1,0,1e308,1e308,0,0,"])

    def test_shared_structure_source_selection_and_default_bat_bytes(self):
        text = HEADER + "\n" + ROW + "\n"
        for malformed in ["", HEADER, text + "\n", text.replace(",nan", ',"nan"'),
                          text.replace("temperature", "battery_future"),
                          text.replace("temperature", "id"), text.replace("voltage_v", "missing"),
                          text + "1000,1,12,2,0,0,nan\n", text.replace(",nan", ",nan,extra")]:
            with self.subTest(text=malformed), self.assertRaises(ValueError):
                self.converter.convert(malformed, source_format="px4-battery")
        with self.assertRaises(ValueError):
            self.converter.convert(text, source_format="guessed")
        with self.assertRaises(ValueError):
            self.converter.convert(text)
        bat = "TimeUS,Instance,Volt,Curr,CurrTot,EnrgTot,RemPct\n1,0,12.5,2,1234,1.25,75\n"
        expected = bat.splitlines()[0] + "," + ",".join(DERIVED) + "\n" + \
            "1,0,12.5,2,1234,1.25,75,12.5,2.0,1.234,4500.0,25.0,0.75\n"
        self.assertEqual(self.converter.convert(bat), expected)
        self.assertEqual(self.converter.convert(bat, source_format="ardupilot-bat"), expected)

    def test_cli_source_flag_exclusive_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.csv", Path(directory) / "output.csv"
            source.write_text(HEADER + "\n" + ROW + "\n")
            command = [sys.executable, str(SCRIPT), str(source), str(output), "--source-format", "px4-battery"]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = output.read_bytes()
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), expected)
            output2 = Path(directory) / "invalid.csv"
            command[3] = str(output2)
            source.write_text(HEADER + "\n" + ROW.replace(",2,", ",NaN,", 1) + "\n")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(output2.exists())




if __name__ == "__main__":
    unittest.main()
