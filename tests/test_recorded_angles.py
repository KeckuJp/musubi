"""Finite source-qualified synthetic exports, not decoded flight qualifications."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_recorded_angles_csv import convert

ROOT = Path(__file__).resolve().parents[1]


class RecordedAnglesTests(unittest.TestCase):
    def test_betaflight_si_motion_reaches_common_output_without_a_frame_claim(self):
        """Betaflight 4.2.0 through the pinned decoder, its own schema row, not INAV's."""
        schema = "betaflight-4.2.0-si-motion"
        header = "time (us)," + ",".join([f"gyroADC[{i}] (rad/s)" for i in range(3)] +
            [f"accSmooth[{i}] (m/s/s)" for i in range(3)]) + ",rcCommand[0]"
        names = ([f"reported_angular_velocity_{a}_rad_s" for a in "xyz"]
                 + [f"reported_acceleration_{a}_m_s2" for a in "xyz"])
        for values, boot in (((0.52, -1.05, 0.0, 9.81, -0.49, 0.25), 1000),
                             ((-2.09, 0.17, 3.14, -9.81, 0.98, -0.50), 25)):
            record = f"{boot}," + ",".join(f"{v:.2f}" for v in values) + ",1500"
            text = header + "\n" + record + "\n"
            output = convert(text, schema)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual([float(row[name]) for name in names], list(values))
            self.assertEqual(row["motion_schema"], schema)
            self.assertEqual(int(row["record_time_us"]), boot)
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), record)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "decoded.csv"; source.write_text(text)
                converted = Path(directory) / "motion.csv"
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_recorded_angles_csv.py"),
                    str(source), str(converted), "--schema", schema], check=True, capture_output=True)
                self.assertEqual(converted.read_text(), output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/recorded-angles/profile.toml"),
                        str(converted), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    fields = common["observations"][0]["fields"]
                    self.assertEqual([fields[name] for name in names], list(values))
                    self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
                    units = common["profile_units"]
                    for name in names:
                        self.assertTrue(units[name])
                    self.assertNotIn("body", " ".join(units[name] for name in names).lower())
        good = header + "\n1000," + ",".join(f"{v:.2f}" for v in (0.5,) * 6) + ",1500\n"
        for bad in (good.replace("(rad/s)", ""), good.replace("(m/s/s)", "(g)"),
                    good.replace("(rad/s)", "(deg/s)"), good.replace("time (us)", "time (ms)"),
                    good.replace("0.50", "512"), good.replace("0.50", "NaN", 1),
                    good.replace("0.50", "1e-400", 1)):
            with self.assertRaises(ValueError):
                convert(bad, schema)
        self.assertNotEqual(schema, "inav-decoder-1918-si-motion")
        self.assertEqual(convert(good, schema).splitlines()[0],
                         convert(good, "inav-decoder-1918-si-motion").splitlines()[0])

    def test_si_motion_export_reuses_common_reader_without_second_scaling(self):
        schema = "inav-decoder-1918-si-motion"
        header = "time (us)," + ",".join([f"gyroADC[{i}] (rad/s)" for i in range(3)] +
            [f"accSmooth[{i}] (m/s/s)" for i in range(3)]) + ",future"
        for values in ((1.25, -2.5, 0., 9.81, -1., 0.5), (-3., 0.5, 2., -9.81, 2., 1.)):
            record = "100," + ",".join(f"{v:.2f}" for v in values) + ",opaque"
            text = header + "\n" + record + "\n" + record + "\n"
            output = convert(text, schema)
            rows = list(csv.DictReader(io.StringIO(output)))
            names = [f"reported_angular_velocity_{a}_rad_s" for a in "xyz"] + [f"reported_acceleration_{a}_m_s2" for a in "xyz"]
            self.assertEqual([float(rows[0][name]) for name in names], list(values))
            self.assertEqual(len(rows), 2)
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), record)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "input.csv"; source.write_text(text)
                output_path = Path(directory) / "output.csv"
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_recorded_angles_csv.py"),
                    str(source), str(output_path), "--schema", schema], check=True, capture_output=True)
                self.assertEqual(output_path.read_text(), output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/recorded-angles/profile.toml"),
                        str(output_path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual([common["observations"][0]["fields"][name] for name in names], list(values))
                    self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
            for bad in (text.replace("(rad/s)", "(deg/s)"), text.replace("(m/s/s)", "(g)"),
                        text.replace(f"{values[0]:.2f}", "NaN"), text.replace(f"{values[0]:.2f}", "1e-400"),
                        text.replace("time (us)", "time (ms)")):
                with self.assertRaises(ValueError): convert(bad, schema)

    def test_two_schemas_preserve_meaning_time_and_unknown_source_fields(self):
        for schema, header, values in [
            ("inav-7.1.2-raw-attitude", "time (us), attitude[0], attitude[1], attitude[2], future", "100, 900, -450, 1800, opaque"),
            ("plane-4.3.1-decoded-att", "TimeUS,Roll,Pitch,Yaw,future", "100,90,-45,180,opaque")]:
            output = convert(header + "\n" + values + "\n" + values + "\n", schema)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["record_time_us"], "100")
            self.assertAlmostEqual(float(rows[0]["reported_roll_rad"]), math.pi / 2)
            self.assertAlmostEqual(float(rows[0]["reported_pitch_rad"]), -math.pi / 4)
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), values)
            self.assertEqual(bytes.fromhex(rows[0]["source_header_hex"][4:]).decode(), header)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "angles.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/recorded-angles/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["clock_basis"], "BootRelative")
                    self.assertAlmostEqual(common["observations"][0]["fields"]["reported_yaw_rad"], math.pi)
            alternate = values.replace("900", "0").replace("90,", "0,")
            self.assertEqual(next(csv.DictReader(io.StringIO(convert(header + "\n" + alternate, schema))))["reported_roll_rad"], "0.0")

    def test_wrong_scale_header_nonfinite_and_time_fail(self):
        header = "time (us),attitude[0],attitude[1],attitude[2]\n"
        for text in [header, header + "1,90.5,0,0", header + "1,32768,0,0", header + "1,NaN,0,0",
                     header + "2,0,0,0\n1,0,0,0", header.replace("(us)", "(ms)") + "1,0,0,0",
                     header + "1,0,0", header + "1,0,0,0,extra"]:
            with self.assertRaises(ValueError):
                convert(text, "inav-7.1.2-raw-attitude")
        with self.assertRaises(ValueError):
            convert("TimeUS,Roll,Pitch,Yaw\n1,inf,0,0", "plane-4.3.1-decoded-att")
