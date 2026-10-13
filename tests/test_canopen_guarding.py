"""Fixed saved DATA response meaning; no polling or physical guarding proof."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import convert_dronecan_capture as converter

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,standard_id,frame_type,data_hex\n"
SOURCE = "canopennode-v4.1-node-guarding"
PROFILE = ROOT / "profiles/declared/dronecan-recorded/canopen-guarding-profile.toml"


class GuardingTests(unittest.TestCase):
    def common(self, output, directory):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required; absence is NOT_RUN")
        path = directory / "common.csv"
        path.write_text(output)
        return json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                         capture_output=True, check=True).stdout)

    def test_two_inputs_state_toggle_originals_and_common(self):
        # Initial toggle1, repeated toggles, same-time responses and unknown state
        # are saved reports, not evidence of loss/reboot/physical liveness.
        for frames in [[(0x701, 0x80), (0x701, 0x85), (0x701, 0x85), (0x702, 0x04)],
                       [(0x77f, 0xff), (0x77f, 0x05), (0x703, 0xaa), (0x704, 0x00)]]:
            originals = [["100", f"{node:x}", "DATA", f"{value:02x}"] for node, value in frames]
            text = HEADER + "\n".join(",".join(row) for row in originals) + "\n100,123,DATA,ab\n"
            output, report = converter.convert_canopen(text, guarding_source=SOURCE)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["guarding_source"], SOURCE)
            self.assertEqual((report["frames"], report["decoded_frames"], len(report["unsupported_frames"])), (5, 4, 1))
            self.assertEqual(report["unsupported_frames"], [["100", "123", "DATA", "ab"]])
            with tempfile.TemporaryDirectory() as temporary:
                common = self.common(output, Path(temporary))
            self.assertEqual(common["main_rows"], 4)
            self.assertEqual(common["platform_domain"], "Unknown")
            for row, observed, original, (node, value) in zip(rows, common["observations"], originals, frames):
                state = value & 127
                name = {0: "INITIALIZING", 4: "STOPPED", 5: "OPERATIONAL", 127: "PRE_OPERATIONAL"}.get(state, f"UNKNOWN_{state}")
                fields = observed["fields"]
                self.assertEqual(fields["node_reported_mode"], name)
                self.assertEqual(fields["canopen_state_code"], state)
                self.assertEqual(fields["source_node_id"], node - 0x700)
                self.assertEqual(fields["canopen_guarding_toggle"], value >> 7)
                self.assertEqual(fields["canopen_guarding_raw_byte"], value)
                self.assertEqual(fields["canopen_guarding_source_basis"], SOURCE)
                self.assertEqual(json.loads(bytes.fromhex(fields["source_frames_hex"][4:])), [original])
                self.assertEqual(observed["clock_basis"], "Unknown")
                self.assertIsNone(observed["anchor_unix_us"])
                self.assertEqual(row["record_time_us"], "100")

    def test_shape_source_mode_and_old_high_bit_refusals(self):
        good = HEADER + "100,701,DATA,85\n"
        for text in [good.replace(",DATA,", ",RTR,"), good.replace(",DATA,", ",data,"),
                     good.replace("frame_type,", "").replace("DATA,", ""),
                     good.replace(",85", ","), good.replace(",85", ",0000"),
                     good.replace(",85", ",gg"), good + "99,701,DATA,05\n",
                     HEADER + "100,123,DATA,85\n", HEADER,
                     good + "100,123,RTR,\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                converter.convert_canopen(text, guarding_source=SOURCE)
        for flags in [dict(guarding_source="unknown"), dict(guarding_source=True),
                      dict(guarding_source=SOURCE, include_emergency=True),
                      dict(guarding_source=SOURCE, emergency_source="canopennode-v4.1-hbconsumer")]:
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                converter.convert_canopen(good, **flags)
        with self.assertRaisesRegex(ValueError, "node-guarding"):
            converter.convert_canopen("time_us,standard_id,data_hex\n100,701,85\n")

    def test_cli_explicit_mode_and_incompatible_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path, out = directory / "input.csv", directory / "out"
            path.write_text(HEADER + "100,701,DATA,80\n100,701,DATA,00\n")
            command = [sys.executable, "-m", "scripts.convert_dronecan_capture", str(path), str(out),
                       "--protocol", "canopen-node-guarding"]
            for options in [["--canopen-emergency-source", "canopennode-v4.1-hbconsumer"],
                            ["--capture-time-us", "100"], ["--rts-source", "1"], ["--actuator", "1:linear"]]:
                self.assertNotEqual(subprocess.run(command + options, cwd=ROOT, capture_output=True).returncode, 0)
                self.assertFalse(out.exists())
            subprocess.run(command, cwd=ROOT, capture_output=True, check=True)
            csv_text = (out / "observations.csv").read_text()
            self.assertEqual(json.loads((out / "accounting.json").read_text())["decoded_frames"], 2)
            self.assertEqual(self.common(csv_text, directory)["main_rows"], 2)
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)
            self.assertEqual((out / "observations.csv").read_text(), csv_text)


if __name__ == "__main__":
    unittest.main()
