"""Fixed two-pin authored composition, not real installed recordings."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_ardusub_session_window as decoded
from scripts import convert_ardusub_bin_window as binary
from tests.test_ardusub_session import records
from tests.test_ardusub_session_window import encoded
from tests.test_ardusub_bin_window import preamble, group

PIN = "80e8c23a7a07d0653fc1ce6b6a55ee17889c77af"
BUILD = "ArduSub V4.5.3 (80e8c23a)"


class VersionWindows(unittest.TestCase):
    def check(self, module, path, start, stop, build, original):
        options = dict(start_record=start, stop_record=stop, build_record=build,
            source_pin=PIN, session_id="authored-sub453", single_boot_session=True,
            mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"], qualify_session_domain=True)
        files, report = module.convert(path, **options)
        self.assertEqual(report["missing_required_families"], [])
        self.assertEqual(report[original], "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name, data in files.items():
                (output / name).write_bytes(data)
            self.assertEqual({i["file"] for i in report["outputs"]} - {"BCL2.csv"},
                             {"CTUN.csv", "MODE.csv", "BAT.csv", "BCL.csv", "IMU.csv"})
            for item in report["outputs"]:
                profile = item["profile"]
                self.assertEqual(report["generated_profiles"][profile],
                                 "sha256:" + hashlib.sha256(files[profile]).hexdigest())
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(output / profile), str(output / item["file"]), *item["reader_options"]],
                    capture_output=True, check=True).stdout)
                self.assertEqual(common["platform_domain"], "Subsurface")
                self.assertEqual(common["observations"][0]["fields"][original], report[original])
        for wrong in (decoded.session.PIN, "unknown"):
            with self.assertRaises(ValueError):
                module.convert(path, **dict(options, source_pin=wrong))

    def test_decoded453(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            rows = records(12)
            rows[0]["data"]["Message"] = BUILD
            path.write_bytes(encoded(rows))
            self.check(decoded, path, 1, 6, 0, "original_jsonl_sha256")
            rows.append(records(12)[0])
            path.write_bytes(encoded(rows))
            with self.assertRaises(ValueError):
                decoded.convert(path, start_record=1, stop_record=len(rows), build_record=0,
                    source_pin=PIN, session_id="mixed", single_boot_session=True,
                    mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"])

    def test_native453_and_legacy_export_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.bin"
            path.write_bytes(preamble() + group(build=BUILD.encode()))
            self.check(binary, path, 10, 16, 9, "original_bin_sha256")
            export = binary.exporter.export_window
            self.assertEqual(export(path, ("MSG",), 9, 10, sub457_jsonl=True),
                export(path, ("MSG",), 9, 10, sub_source_pin=decoded.session.PIN))
            with self.assertRaises(ValueError):
                export(path, ("MSG",), 9, 10, sub457_jsonl=True, sub_source_pin=PIN)
            with self.assertRaises(ValueError):
                export(path, ("MSG",), 9, 10, sub_source_pin="unknown")
