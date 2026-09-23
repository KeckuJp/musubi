"""Authored exports shaped by Parrot's own tlmb2csv writer rules; not a real flight record."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_parrot_telemetry_export import convert, IMU_READINGS

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/parrot-telemetry-export/profile.toml"
DECLARED_IMU_NAMES = ["gyro.x", "gyro.y", "gyro.z", "acc.x", "acc.y", "acc.z", "temperature",
                      "timestamp.tv_sec", "timestamp.tv_nsec"]


def export(header, rows, newline="\n"):
    """The pinned writer emits the header with bare commas and the data with ", "."""
    return (newline.join([",".join(header), *[", ".join(row) for row in rows]])
            + newline).encode("utf-8")


def imu_export(samples=2, count=IMU_READINGS, extra=(), indices=None, names=None):
    bases = list(names or DECLARED_IMU_NAMES)
    order = list(range(count)) if indices is None else list(indices)
    header = ["time_us"] + [f"{base}_{index}" for base in bases for index in order] + list(extra)
    rows = []
    for sample in range(samples):
        cells = [str(sample * 5000)]
        for base in bases:
            for index in order:
                if base.startswith("timestamp."):
                    cells.append(str(1700 + index if base.endswith("tv_sec") else 250 + index))
                else:
                    cells.append("%f" % (index / 8 + sample))
        cells += ["0"] * len(extra)
        rows.append(cells)
    return export(header, rows)


class ParrotTelemetryExportTests(unittest.TestCase):
    def test_barometer_section_reuses_declared_units_and_caller_clock(self):
        raw = export(["time_us", "pressure", "temperature"],
                     [["0", "101325.000000", "295.150000"],
                      ["13333", "101300.500000", "295.250000"]])
        output, report = convert(raw, "sensors-barometer", 777, "relative")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["source_rows"], report["output_records"]), (2, 2))
        self.assertEqual(report["unmapped_columns"], [])
        self.assertEqual([float(row["barometer_pressure_pa"]) for row in rows], [101325.0, 101300.5])
        self.assertEqual(float(rows[1]["barometer_temperature_k"]), 295.25)
        self.assertEqual([row["parrot_export_time_us"] for row in rows], ["0", "13333"])
        self.assertTrue(all(row["record_time_us"] == "777" for row in rows))
        self.assertIn("FIRST_SAMPLE_ZERO", rows[0]["parrot_time_basis"])
        self.assertEqual(rows[0]["imu_sample_index"], "")
        self.assertIn("CHECKED_HERE_NOT_PROVEN_UPSTREAM", rows[0]["parrot_name_basis"])
        self.assertEqual(bytes.fromhex(rows[0]["parrot_section_hex"][4:]).decode(),
                         "sensors_barometer")

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); path = base / "sensors_barometer.csv"; out = base / "out"
            path.write_bytes(raw)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_parrot_telemetry_export.py"),
                str(path), str(out), "--section", "sensors-barometer", "--capture-time-us", "777",
                "--timestamp-mode", "relative"], check=True, capture_output=True)
            self.assertEqual((out / "source.csv").read_bytes(), raw)
            self.assertEqual(json.loads((out / "report.json").read_text())["selected_section"],
                             "sensors_barometer")
            reader = os.environ.get("MUSUBI_TELEMETRY_READER")
            if not reader:
                self.skipTest("MUSUBI_TELEMETRY_READER is not configured; common proof NOT RUN")
            common = json.loads(subprocess.run([reader, str(PROFILE),
                str(out / "observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["observations"][0]["fields"]["barometer_pressure_pa"], 101325.0)
        self.assertEqual(common["observations"][1]["fields"]["barometer_temperature_k"], 295.25)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_crlf_and_bound_keep_the_retained_bytes_and_their_digest_exact(self):
        rows = [["0", "101325.000000", "295.150000"]]
        unix = export(["time_us", "pressure", "temperature"], rows)
        windows = export(["time_us", "pressure", "temperature"], rows, newline="\r\n")
        self.assertNotEqual(unix, windows)
        unix_out, unix_report = convert(unix, "sensors-barometer", 5, "raw")
        crlf_out, crlf_report = convert(windows, "sensors-barometer", 5, "raw")
        strip = lambda text: [{k: v for k, v in row.items() if k != "source_sha256"}
                              for row in csv.DictReader(io.StringIO(text))]
        self.assertEqual(strip(unix_out), strip(crlf_out))
        self.assertNotEqual(unix_report["source_sha256"], crlf_report["source_sha256"])

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); path = base / "sensors_barometer.csv"; out = base / "out"
            path.write_bytes(windows)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_parrot_telemetry_export.py"),
                str(path), str(out), "--section", "sensors-barometer", "--capture-time-us", "5",
                "--timestamp-mode", "raw"], check=True, capture_output=True)
            self.assertEqual((out / "source.csv").read_bytes(), windows)
            self.assertIn(b"\r\n", (out / "source.csv").read_bytes())
        with self.assertRaises(ValueError):
            convert(b"time_us,pressure,temperature\n" + b"0, 1.0, 2.0\n" * 1_400_000,
                    "sensors-barometer", 5, "raw")

    def test_imu_mapping_is_caller_declared_and_keeps_readings_separate(self):
        output, report = convert(imu_export(samples=2), "sensors-imu", 900, "raw",
                                 DECLARED_IMU_NAMES)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["source_rows"], report["output_records"]), (2, 20))
        self.assertIn("NOT_ESTABLISHED_BY_ANY_PUBLIC_PARROT_SOURCE", report["name_binding"])
        self.assertIn("UNQUALIFIED", rows[0]["parrot_name_basis"])
        self.assertEqual([row["imu_sample_index"] for row in rows[:IMU_READINGS]],
                         [str(i) for i in range(IMU_READINGS)])
        self.assertEqual(float(rows[3]["imu_gyro_x_rad_s"]), 3 / 8)
        self.assertEqual(rows[3]["imu_sample_time_s"], "1703")
        self.assertEqual(rows[3]["imu_sample_time_ns"], "253")
        self.assertEqual(float(rows[13]["imu_gyro_y_rad_s"]), 3 / 8 + 1)
        self.assertEqual(rows[13]["source_row"], "1")

        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("MUSUBI_TELEMETRY_READER is not configured; common proof NOT RUN")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "imu.csv"; path.write_text(output)
            common = json.loads(subprocess.run([reader, str(PROFILE), str(path),
                "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 20)
        self.assertEqual(common["observations"][3]["fields"]["imu_gyro_x_rad_s"], 3 / 8)

    def test_unknown_columns_are_retained_and_counted_not_converted(self):
        output, report = convert(imu_export(samples=1, extra=("vendor_future_signal",)),
                                 "sensors-imu", 900, "raw", DECLARED_IMU_NAMES)
        self.assertEqual(report["unmapped_columns"], ["vendor_future_signal"])
        self.assertEqual(report["output_records"], IMU_READINGS)
        self.assertNotIn("vendor_future_signal", output)
        self.assertTrue(report["source_sha256"].startswith("sha256:"))

    def test_important_failures_are_refused_rather_than_guessed(self):
        good = export(["time_us", "pressure", "temperature"], [["0", "1.000000", "2.000000"]])
        self.assertTrue(convert(good, "sensors-barometer", 1, "relative")[0])
        for raw, selector, mode, names in [
            (good.replace(b"time_us", b"t_us"), "sensors-barometer", "relative", None),
            (export(["time_us", "pressure"], [["0", "1.000000"]]), "sensors-barometer", "relative", None),
            (good.replace(b"1.000000", b"1.0e5"), "sensors-barometer", "relative", None),
            (good.replace(b"2.000000", b"nan"), "sensors-barometer", "relative", None),
            (good + b"0, 1.000000\n", "sensors-barometer", "relative", None),
            (good, "sensors-barometer", "local", None),
            (b"", "sensors-barometer", "relative", None),
            (b"time_us,pressure,temperature\n", "sensors-barometer", "relative", None),
            (good, "sensors-imu", "relative", DECLARED_IMU_NAMES),
            (good, "sensors-barometer", "relative", DECLARED_IMU_NAMES),
            (imu_export(samples=1), "sensors-imu", "raw", None),
            (imu_export(samples=1), "sensors-imu", "raw", DECLARED_IMU_NAMES[:-1]),
            (imu_export(samples=1), "sensors-imu", "raw", ["gyro.x"] * 9),
        ]:
            with self.subTest(case=raw[:40], selector=selector, names=names):
                with self.assertRaises((ValueError, KeyError, IndexError)):
                    convert(raw, selector, 1, mode, names)
        with self.assertRaises(ValueError):
            convert(good, "sensors-barometer", True, "relative")

        with self.assertRaisesRegex(ValueError, "repeats a column name"):
            convert(export(["time_us", "pressure", "pressure"], [["0", "1.000000", "2.000000"]]),
                    "sensors-barometer", 1, "relative")
        with self.assertRaisesRegex(ValueError, "repeats a column name"):
            convert(imu_export(samples=1).replace(b"gyro.y_0", b"gyro.x_0", 1),
                    "sensors-imu", 1, "raw", DECLARED_IMU_NAMES)
        for count in (1, 3, 11):
            with self.subTest(count=count), self.assertRaises(ValueError):
                convert(imu_export(samples=1, count=count), "sensors-imu", 1, "raw",
                        DECLARED_IMU_NAMES)
        with self.assertRaisesRegex(ValueError, "every declared index"):
            convert(imu_export(samples=1, indices=(0, 2, 7)), "sensors-imu", 1, "raw",
                    DECLARED_IMU_NAMES)
        with self.assertRaisesRegex(ValueError, "row width"):
            convert(good + b"0, 1.000000, 2.000000, 3.000000\n", "sensors-barometer", 1, "relative")
        with self.assertRaisesRegex(ValueError, "finite"):
            convert(export(["time_us", "pressure", "temperature"],
                           [["0", "9" * 400, "2.000000"]]), "sensors-barometer", 1, "relative")
        with self.assertRaisesRegex(ValueError, "unsigned timestamp half"):
            convert(imu_export(samples=1).replace(b", 1700,", b", -1700,", 1),
                    "sensors-imu", 1, "raw", DECLARED_IMU_NAMES)


if __name__ == "__main__":
    unittest.main()
