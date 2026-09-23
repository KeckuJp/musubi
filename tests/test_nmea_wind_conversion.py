"""Independent synthetic NMEA MWV tests; no capture files are accessed."""
import copy
import csv
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_nmea_wind.py"
COLUMNS = ["unlocated_time_us", "source_line", "talker", "wind_reference", "reported_valid",
           "wind_angle_rad", "wind_speed_m_s", "source_sentence_hex"]


def sentence(payload, prefix="$" ):
    checksum = 0
    for byte in payload.encode("ascii"):
        checksum ^= byte
    return f"{prefix}{payload}*{checksum:02X}"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.converter = load(SCRIPT)

    def decode(self, text):
        converted, report = self.converter.convert(text)
        rows = list(csv.reader(io.StringIO(converted)))
        self.assertEqual(rows[0], COLUMNS)
        return [dict(zip(COLUMNS, row)) for row in rows[1:]], report

    def test_water_sentences_reuse_checksum_units_and_common_output(self):
        import os
        for temperature, depth, offset in ((-2., 12.5, -.7), (25., 0., 1.2)):
            lines = [sentence(f"YXMTW,{temperature},C"), sentence(f"SDDPT,{depth},{offset},100"),
                     sentence("SDDPT,,,"), sentence("YXMTW,,C"), sentence("XXABC,retained")]
            text = "\n".join(lines) + "\n"
            output, report = self.converter.convert(text, "water-dpt-mtw")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 4)
            self.assertAlmostEqual(float(rows[0]["water_temperature_k"]), temperature + 273.15)
            self.assertEqual(float(rows[1]["depth_below_transducer_m"]), depth)
            self.assertEqual(float(rows[1]["transducer_offset_m"]), offset)
            self.assertEqual(rows[2]["measurement_disposition"], "UNAVAILABLE")
            self.assertEqual(rows[2]["depth_below_transducer_m"], "")
            self.assertEqual(report["water_measurements"], 2)
            self.assertEqual(report["unsupported_records"], 1)
            self.assertEqual(report["unsupported"][0]["raw"], lines[-1])
            self.assertEqual(bytes.fromhex(rows[0]["source_sentence_hex"][4:]).decode(), lines[0])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory); source = path / "water.txt"; source.write_text(text)
                subprocess.run([sys.executable, str(SCRIPT), str(source), str(path / "out"),
                    "--kind", "water-dpt-mtw"], cwd=ROOT, check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    observations = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/nmea-wind/profile.toml"),
                        str(path / "out/converted.csv"), "--allow-equal-time"], check=True,
                        capture_output=True).stdout)["observations"]
                    self.assertEqual(len(observations), 4)
                    self.assertAlmostEqual(observations[0]["fields"]["water_temperature_k"], temperature + 273.15)
                    self.assertEqual(observations[1]["fields"]["depth_below_transducer_m"], depth)
                    self.assertTrue(all(o["clock_basis"] == "Unknown" and o["anchor_unix_us"] is None for o in observations))
        _, report = self.converter.convert(sentence("YXMTW,,C"), "water-dpt-mtw")
        self.assertEqual(report["measurement_result"], "ONLY_WITHHELD_VALUES")
        for payload in ("YXMTW,1,F", "YXMTW,-274,C", "YXMTW,NaN,C", "SDDPT,-1,0,100",
                        "SDDPT,1,NaN,100", "SDDPT,1,0", "SDDPT,1,0,-1", "XXABC,no_selected"):
            with self.assertRaises(ValueError):
                self.converter.convert(sentence(payload), "water-dpt-mtw")
        with self.assertRaises(ValueError):
            self.converter.convert(sentence("YXMTW,1,C")[:-1] + "Z", "water-dpt-mtw")

    def test_units_references_and_signed_angle_boundaries(self):
        cases = [(0, "R", 10, "M", 0, 10), (180, "T", 36, "K", math.pi, 10),
                 (270, "R", 3600, "N", -math.pi / 2, 1852),
                 (360, "T", 0, "M", 0, 0)]
        lines = [sentence(f"WIMWV,{angle},{ref},{speed},{unit},A") for angle, ref, speed, unit, _, _ in cases]
        rows, report = self.decode("\r\n".join(lines) + "\r\n")
        self.assertEqual(report["valid_with_measurement"], 4)
        for index, (row, case) in enumerate(zip(rows, cases)):
            with self.subTest(case=case):
                self.assertEqual(row["unlocated_time_us"], "0")
                self.assertEqual(row["source_line"], str(index + 1))
                self.assertEqual(row["talker"], "WI")
                self.assertEqual(row["reported_valid"], "true")
                self.assertEqual(row["wind_reference"], "apparent_relative_to_vessel" if case[1] == "R"
                                 else "theoretical_relative_to_vessel")
                self.assertAlmostEqual(float(row["wind_angle_rad"]), case[4])
                self.assertAlmostEqual(float(row["wind_speed_m_s"]), case[5])
                self.assertEqual(bytes.fromhex(row["source_sentence_hex"][4:]).decode("ascii"), lines[index])

    def test_missing_invalid_unknown_extras_and_accounting(self):
        lines = [sentence("WIMWV,,R,2,M,A,extra,\"quoted\""), sentence("GPGGA,unsupported,\"raw\""),
                 sentence("IIMWV,90,T,,N,A"), sentence("WIMWV,,R,,M,A"),
                 sentence("WIMWV,90,R,4,M,V"), sentence("AIVDM,unsupported", "!")]
        rows, report = self.decode("\n".join(lines))
        self.assertEqual((report["input_records"], report["mwv_records"], report["unsupported_records"]), (6, 4, 2))
        self.assertEqual(report["invalid_status_records"], 1)
        self.assertEqual(report["valid_with_measurement"], 2)
        self.assertEqual(report["blank_lines"], 0)
        self.assertEqual(report["unsupported"], [{"line": 2, "raw": lines[1]}, {"line": 6, "raw": lines[5]}])
        self.assertEqual([row["source_line"] for row in rows], ["1", "3", "4", "5"])
        self.assertEqual([row["wind_speed_m_s"] for row in rows[1:]], ["", "", ""])
        self.assertEqual([row["wind_angle_rad"] for row in (rows[0], rows[2], rows[3])], ["", "", ""])
        self.assertEqual(rows[3]["reported_valid"], "false")
        self.assertEqual(rows[0]["source_sentence_hex"], "hex:" + lines[0].encode("ascii").hex())

    def test_reject_framing_checksum_blank_foreign_and_zero_mwv(self):
        good = sentence("WIMWV,90,R,2,M,A")
        cases = ["", "\n", sentence("GPGGA,unsupported"), "foreign", "\n" + good, good + "\n\n",
                 good + "\nforeign", good[:-1], good[:-2] + "ZZ", good[:-2] + "00",
                 good.replace("90", "91"), good + "trailing", good + "\x00",
                 sentence("WIMWV,90,R,2,M,A,extra") + "\t", sentence("MWV,90,R,2,M,A"),
                 sentence("WIMWV,90,R,2,M,A").replace("$", "#"), good + "é"]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.converter.convert(text)

    def test_reject_numeric_range_unit_reference_status_and_short_payload(self):
        fields = ["90", "R", "2", "M", "A"]
        invalid = {0: ["-1", "361", "NaN", "inf", "junk"], 1: ["", "X"],
                   2: ["-1", "NaN", "inf", "junk", "1e999"], 3: ["", "X"], 4: ["", "X"]}
        for index, values in invalid.items():
            for value in values:
                source = fields.copy()
                source[index] = value
                with self.subTest(field=index, value=value), self.assertRaises(ValueError):
                    self.converter.convert(sentence("WIMWV," + ",".join(source)))
        for payload in ("WIMWV,90,R,2,M", "WIMWV,junk,R,2,M,V", "WIMWV,90,R,-1,M,V"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.converter.convert(sentence(payload))

    def test_cli_exclusive_directory_and_validate_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.log", Path(directory) / "result"
            source.write_text(sentence("WIMWV,90,R,2,M,A"))
            command = [sys.executable, str(SCRIPT), str(source), str(output)]
            run = subprocess.run(command, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual({p.name for p in output.iterdir()}, {"converted.csv", "report.json"})
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)
            source.write_text(sentence("WIMWV,90,R,2,M,A") + "\nforeign")
            command[-1] = str(Path(directory) / "invalid-result")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(Path(command[-1]).exists())




class DeclaredColumnMeanings(unittest.TestCase):
    """Every column any selected kind writes must have a declared meaning in the profile.

    The common reader does not enforce this: it passes an undeclared column straight through to
    common output, where a reader then sees a value such as `REPORTED_BELOW_TRANSDUCER` with
    nothing saying what it means. Two columns of the water path had reached common output that
    way: `sentence_kind`, and `measurement_disposition`, which is what names the reference a
    depth is measured from and records that a reading is not calibrated.

    The key pattern below allows whitespace on either side of `=`, because TOML does and this
    profile uses both spellings -- a stricter pattern reports a declared key as missing.

    Checked against the converter's own column tuples, so this needs no decoder and no reader.
    """

    def test_every_written_column_is_declared(self):
        module = load(SCRIPT)
        declared = set(re.findall(
            r"^\s*([A-Za-z_][\w.-]*)\s*=",
            (ROOT / "profiles/declared/nmea-wind/profile.toml")
            .read_text(encoding="utf-8").split("[units]")[1], re.M))
        for kind, columns in (("wind", module.COLUMNS), ("ais-class-a", module.AIS_COLUMNS),
                              ("water-dpt-mtw", module.WATER_COLUMNS)):
            with self.subTest(kind=kind):
                self.assertEqual(sorted(name for name in columns if name not in declared), [])


if __name__ == "__main__":
    unittest.main()
