"""Authored fixed-writer diagnostic text; no SDK or device enumeration."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_software_identity_binding as binding

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/software-identity-binding"


def block(fields, ending="\n"):
    return ("Device info: " + ending + "".join(
        "    " + key.ljust(30) + ": \t" + value + ending for key, value in fields)
        + ending).encode()


class RealSenseDeviceInfoTests(unittest.TestCase):
    def test_native_blocks_and_same_row_family_binding_reach_common(self):
        original = (b"Device Name  Serial Number  Firmware Version\r\n"
                    + block([("Name", "Authored camera A"), ("Product Line", "D400"),
                             ("Serial Number", "001"), ("Product Id", "0ABC"),
                             ("Future Field", "unknown,value\u2028retained")], "\r\n")
                    + block([("Name", "Authored camera B"), ("Product Line", "FutureFamily")]))
        output, report = binding.convert_realsense_device_info(original)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["record_time_us"] for r in rows], ["0", "0"])
        self.assertEqual([r["time_disposition"] for r in rows], ["UNLOCATED_TIME"] * 2)
        self.assertEqual(rows[0]["source_input_sha256"], "sha256:" + hashlib.sha256(original).hexdigest())
        self.assertEqual(bytes.fromhex(report["source_input_hex"][4:]), original)
        self.assertEqual(report["input_lines"], report["selected_lines"] + len(report["unselected_lines"]))
        bound, _ = binding.convert(output.encode(), paired_identities=(
            "SOURCE_DEVICE=NAME:device_name_hex,FAMILY:product_line_hex",))
        pairs = list(csv.DictReader(io.StringIO(bound)))
        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(r["identity_collision"] for r in pairs))
        self.assertTrue(all(r["identity_role"] == "SOURCE_DEVICE" for r in pairs))
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader is required")
        with tempfile.TemporaryDirectory() as directory:
            for name, data, profile in (("native", output, "realsense-device-info.toml"),
                                        ("bound", bound, "profile.toml")):
                path = Path(directory) / (name + ".csv")
                path.write_text(data)
                run = subprocess.run([reader, str(CASE / profile), str(path), "--allow-equal-time"],
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                common = json.loads(run.stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_missing_empty_capture_and_role_refusal(self):
        data = block([("Name", "A")]) + block([("Name", "A"), ("Product Line", "")])
        output, _ = binding.convert_realsense_device_info(data, capture_time_us=17)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["product_line_disposition"] for r in rows], ["NOT_REPORTED", "REPORTED_EMPTY"])
        self.assertEqual([r["product_line_hex"] for r in rows], ["", ""])
        self.assertEqual(rows[0]["time_disposition"], "CALLER_CAPTURE_UNKNOWN")
        for spec in ("SOURCE_SOFTWARE:FAMILY=product_line_hex", "SOURCE_SOFTWARE:NAME=product_line_hex",
                     "SOURCE_DEVICE:PIN=serial_number_hex", "SOURCE_DEVICE:VERSION=device_name_hex",
                     "SOURCE_DEVICE:NAME=product_line_hex"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                binding.convert(output.encode(), read_components=(spec,))
        for moment in (True, -1, 2**63, 1.0):
            with self.assertRaises(ValueError):
                binding.convert_realsense_device_info(data, capture_time_us=moment)

    def test_bounded_refusals_and_native_cli(self):
        valid = block([("Name", "A"), ("Product Line", "D400")])
        for data in (b"", b"No device detected. Is it plugged in?\n", valid[:-1],
                     block([("Name", "A"), ("Name", "B")]), b"Device info: \n\n",
                     valid + b"Device info:\n",
                     block([("Name", "x" * 257)]), valid * 257, b"x" * (1024**2 + 1),
                     valid.replace(b"D400", b"D\x00")):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                binding.convert_realsense_device_info(data)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "saved.txt"
            source.write_bytes(valid)
            dest = Path(directory) / "out"
            run = subprocess.run(["python3", str(ROOT / "scripts/convert_software_identity_binding.py"),
                                  str(source), str(dest), "--input-format", "realsense-device-info-v2.56.5",
                                  "--read-identity", "SOURCE_DEVICE=NAME:device_name_hex,FAMILY:product_line_hex"],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((dest / "source.txt").read_bytes(), valid)
            self.assertTrue((dest / "device_observations.csv").is_file())
            bad = Path(directory) / "bad"
            refused = subprocess.run(["python3", str(ROOT / "scripts/convert_software_identity_binding.py"),
                                      str(source), str(bad), "--input-format", "realsense-device-info-v2.56.5",
                                      "--time-column", "device_index", "--read-component",
                                      "SOURCE_DEVICE:FAMILY=product_line_hex"], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(bad.exists())


if __name__ == "__main__":
    unittest.main()
