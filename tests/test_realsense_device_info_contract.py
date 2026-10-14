"""Independent contract fixtures, not camera captures; no SDK/device execution."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_software_identity_binding as bridge

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/software-identity-binding"
PAIR = "SOURCE_DEVICE=NAME:device_name_hex,FAMILY:product_line_hex"


def block(fields, annotation=""):
    return ("Device info: \n" + annotation + "".join(
        "    " + key.ljust(30) + ": \t" + value + "\n" for key, value in fields
    ) + "\n").encode()


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def unhex(value):
    if not value:
        return ""
    assert value.startswith("hex:"), value
    return bytes.fromhex(value[4:]).decode()


class RealSenseDeviceInfoContract(unittest.TestCase):
    def common(self, text, profile):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader required; no common-output proof without it")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(text, encoding="utf-8")
            result = json.loads(subprocess.run(
                [reader, str(CASE / profile), str(path), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        for observation in result["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
        return result

    def test_native_blocks_bind_same_row_pairs_to_actual_common(self):
        first = block([("Name", "camera A"), ("Product Line", "family B"),
                       ("Serial Number", "same-serial"), ("Product Id", "0ABC")])
        second = block([("Name", "camera B"), ("Product Line", "family A"),
                        ("Serial Number", "same-serial"), ("Product Id", "0DEF")])
        source = b"summary must not supply identity\n" + first + second + first
        native, report = bridge.convert_realsense_device_info(source, capture_time_us=17)
        original = rows(native)
        self.assertEqual(len(original), 3)
        self.assertEqual([r["device_index"] for r in original], ["0", "1", "2"])
        self.assertEqual([unhex(r["device_name_hex"]) for r in original],
                         ["camera A", "camera B", "camera A"])
        self.assertTrue(all(r["record_time_us"] == "17" for r in original))
        self.assertTrue(all(r["time_disposition"] == "CALLER_CAPTURE_UNKNOWN" for r in original))
        self.assertEqual(report["devices"], 3)
        self.assertEqual(report["source_input_sha256"], "sha256:" + hashlib.sha256(source).hexdigest())
        self.assertEqual(unhex(report["source_input_hex"]).encode(), source)
        self.assertEqual(report["input_lines"], len(source.splitlines()))
        self.assertEqual(report["input_lines"], report["selected_lines"] + len(report["unselected_lines"]))
        native_common = self.common(native, "realsense-device-info.toml")
        self.assertEqual(native_common["main_rows"], 3)
        self.assertEqual([unhex(o["fields"]["product_line_hex"]) for o in native_common["observations"]],
                         ["family B", "family A", "family B"])
        bound, accounting = bridge.convert(native.encode(), paired_identities=(PAIR,))
        output = rows(bound)
        tuples = {}
        for row in output:
            pair = json.loads(unhex(row["identity_value_hex"]))
            tuples[(unhex(pair["NAME"]), unhex(pair["FAMILY"]))] = int(row["identity_rows_carrying_value"])
            self.assertEqual(row["identity_role"], "SOURCE_DEVICE")
            self.assertEqual(row["identity_kind"], "NAME+FAMILY")
            self.assertEqual(row["bound_observations_sha256"], "sha256:" + hashlib.sha256(native.encode()).hexdigest())
            selector = json.loads(unhex(row["identity_join_hex"]))
            self.assertEqual(sum(all(r[k] == v for k, v in selector.items()) for r in original),
                             int(row["identity_rows_carrying_value"]))
        self.assertEqual(tuples, {("camera A", "family B"): 2, ("camera B", "family A"): 1})
        self.assertEqual(accounting["distinct_pairs_emitted"], 2)
        common = self.common(bound, "profile.toml")
        self.assertEqual(common["main_rows"], 2)
        self.assertTrue(all(o["fields"]["identity_role"] == "SOURCE_DEVICE" for o in common["observations"]))
        self.assertEqual([o["fields"]["identity_value_hex"] for o in common["observations"]],
                         [r["identity_value_hex"] for r in output])
        self.assertEqual([o["fields"]["identity_rows_carrying_value"] for o in common["observations"]],
                         [int(r["identity_rows_carrying_value"]) for r in output])

    def test_absent_empty_unknown_and_outside_text_do_not_supply_values(self):
        source = (b"Product Line: summary-only\n" + block([("Name", "absent")])
                  + block([("Name", "empty"), ("Product Line", "")])
                  + block([("Name", "unknown"), ("Product Line", "N/A"),
                           ("Future Info", "uninterpreted, value")],
                          "Playback Device (/not/to/be/opened.bag)\n"))
        native, report = bridge.convert_realsense_device_info(source.replace(b"\n", b"\r\n"))
        original = rows(native)
        self.assertEqual([r["product_line_disposition"] for r in original],
                         ["NOT_REPORTED", "REPORTED_EMPTY", "REPORTED_UNCLASSIFIED"])
        self.assertEqual([unhex(r["product_line_hex"]) for r in original], ["", "", "N/A"])
        self.assertTrue(all(r["record_time_us"] == "0" and r["time_disposition"] == "UNLOCATED_TIME"
                            for r in original))
        self.assertTrue(all(r["serial_number_hex"] == "" and r["usb_product_id_hex"] == ""
                            for r in original))
        self.assertIn("Future Info", unhex(original[2]["source_block_hex"]))
        self.assertIn("uninterpreted, value", unhex(original[2]["source_block_hex"]))
        self.assertEqual(report["clock"], "Unknown")
        bound, accounting = bridge.convert(native.encode(), paired_identities=(PAIR,))
        self.assertEqual(accounting["pairs_with_a_missing_component"], 2)
        for row in rows(bound):
            pair = json.loads(unhex(row["identity_value_hex"]))
            if unhex(pair["NAME"]) in ("absent", "empty"):
                self.assertIsNone(pair["FAMILY"])
                self.assertEqual(row["identity_missing_components"], "FAMILY")
            else:
                self.assertEqual(unhex(pair["FAMILY"]), "N/A")
        self.assertEqual(self.common(bound, "profile.toml")["main_rows"], 3)

    def test_malformed_bounds_and_identity_role_misuse_refuse(self):
        good = block([("Name", "device"), ("Product Line", "D400")])
        for source in (b"summary only\n", b"Device info: \n\n", good[:-1],
                       good.replace(b": \t", b": ", 1),
                       block([("Name", "a"), ("Name", "b")]),
                       block([("Name", "x\x00y")]), b"\xff\n",
                       block([("Name", "x" * 257)]), good * 257,
                       block([(f"Info {n}", "v") for n in range(65)]),
                       b"x" * (1024 * 1024 + 1)):
            with self.subTest(source_length=len(source)), self.assertRaises(ValueError):
                bridge.convert_realsense_device_info(source)
        for clock in (True, 1.0, -1, 2 ** 63):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                bridge.convert_realsense_device_info(good, capture_time_us=clock)
        native, _ = bridge.convert_realsense_device_info(good)
        for spec in ("SOURCE_SOFTWARE:FAMILY=product_line_hex",
                     "SOURCE_SOFTWARE:NAME=device_name_hex", "SOURCE_DEVICE:NAME=product_line_hex",
                     "SOURCE_DEVICE:FAMILY=device_name_hex", "SOURCE_DEVICE:PIN=serial_number_hex",
                     "SOURCE_DEVICE:NAME=serial_number_hex", "SOURCE_DEVICE:NAME=usb_product_id_hex",
                     "SOURCE_DEVICE:VERSION=device_name_hex", "SOURCE_DEVICE:DIGEST=source_block_hex"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                bridge.convert(native.encode(), read_components=(spec,))
        for role in ("SOURCE_SOFTWARE", "CONVERSION_TOOL", "REFERENCE_SPECIFICATION", "ADAPTER_PROFILE"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                bridge.convert(native.encode(), declared_components=(role + ":FAMILY=D400",))

    def test_cli_native_saved_artifacts_and_old_four_roles_byte_parity(self):
        old = b"record_time_us,writer_name,writer_version\n17,alpha,1\n17,beta,2\n"
        legacy, report = bridge.convert(old,
            paired_identities=("SOURCE_SOFTWARE=NAME:writer_name,VERSION:writer_version",),
            declared_components=("CONVERSION_TOOL:VERSION=1", "REFERENCE_SPECIFICATION:PIN=doc-v1",
                                 "ADAPTER_PROFILE:NAME=example"))
        # Fixed on base35f042dd before reading any new implementation.
        self.assertEqual(hashlib.sha256(legacy.encode()).hexdigest(),
                         "5328349def0b1f1aab420f59d662b1efed58fe660d189c35dd0c5240cb73b1d1")
        self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
                         "e0267a35ebd27b12c150b7522151819d1a7147a45dee830eb87e0506bcb4b221")
        source = block([("Name", "device"), ("Product Line", "D400")])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            input_ = base / "saved.txt"
            input_.write_bytes(source)
            command = [sys.executable, str(ROOT / "scripts/convert_software_identity_binding.py"),
                       str(input_), str(base / "out"), "--input-format", "realsense-device-info-v2.56.5",
                       "--capture-time-us", "17", "--read-identity", PAIR]
            done = subprocess.run(command, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            native, native_report = bridge.convert_realsense_device_info(source, capture_time_us=17)
            bound, _ = bridge.convert(native.encode(), paired_identities=(PAIR,))
            self.assertEqual((base / "out/source.txt").read_bytes(), source)
            self.assertEqual((base / "out/device_observations.csv").read_text(), native)
            self.assertEqual((base / "out/observations.csv").read_text(), bound)
            self.assertEqual(json.loads((base / "out/report.json").read_text())["native_device_info"], native_report)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((base / "out/source.txt").read_bytes(), source)
            input_.write_bytes(source[:-1])
            command[3] = str(base / "broken")
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertFalse((base / "broken").exists())


if __name__ == "__main__":
    unittest.main()
