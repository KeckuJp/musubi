"""Existing domain qualification through authored JSONL/BIN windows, not real logs."""
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


class WindowDomain(unittest.TestCase):
    def check_route(self, module, path, start, stop, build, provenance):
        self.assertTrue(os.environ.get("MUSUBI_MAVLOG_READER"))
        self.assertTrue(os.environ.get("MUSUBI_TELEMETRY_READER"))
        options = dict(start_record=start, stop_record=stop, build_record=build,
            source_pin=decoded.session.PIN, session_id="authored-domain-window",
            single_boot_session=True, mavlog_reader=os.environ["MUSUBI_MAVLOG_READER"])
        old = module.convert(path, **options)
        self.assertEqual(old, module.convert(path, **options, qualify_session_domain=False))
        files, report = module.convert(path, **options, qualify_session_domain=True)
        self.assertEqual(report["selected_counts"], old[1]["selected_counts"])
        self.assertEqual(report["missing_required_families"], [])
        self.assertEqual(report[provenance], old[1][provenance])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name, data in files.items():
                (output / name).write_bytes(data)
            for item in report["outputs"]:
                profile = item["profile"]
                self.assertEqual(report["generated_profiles"][profile],
                    "sha256:" + hashlib.sha256(files[profile]).hexdigest())
                observed = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(output / profile), str(output / item["file"]), *item["reader_options"]],
                    capture_output=True, check=True).stdout)
                self.assertEqual(observed["platform_domain"], "Subsurface")
                row = observed["observations"][0]
                self.assertEqual(row["clock_basis"], "BootRelative")
                self.assertEqual(row["fields"][provenance], report[provenance])
                self.assertEqual(row["fields"]["session_original_domain"],
                    "Subsurface" if item["file"] == "CTUN.csv" else "Unknown")
        for bad in (1, "true", None):
            with self.assertRaisesRegex(ValueError, "must be boolean"):
                module.convert(path, **options, qualify_session_domain=bad)

    def test_decoded_selected_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.jsonl"
            path.write_bytes(encoded(records(12)))
            self.check_route(decoded, path, 1, 6, 0, "original_jsonl_sha256")

    def test_native_independent_second_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.bin"
            path.write_bytes(preamble() + group() + group(110, 24))
            self.check_route(binary, path, 17, 23, 9, "original_bin_sha256")
