"""Authored configured EMCY DATA captures, not actual settings or bus verification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_dronecan_capture as target

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,standard_id,frame_type,data_hex\n"
PROFILE = ROOT / "profiles/declared/dronecan-recorded/canopen-configured-profile.toml"


class ConfiguredEmergency(unittest.TestCase):
    def test_two_layouts_reuse_values_and_preserve_source_in_common(self):
        for node, canid in ((7, 0x181), (42, 0x301)):
            with self.subTest(node=node), tempfile.TemporaryDirectory() as directory:
                original = ["100", f"{canid:x}", "DATA", "3081501b02000000"]
                data = HEADER + ",".join(original) + f"\n100,{canid:x},DATA,0000000000000000\n"
                data += "100,707,DATA,05\n101,081,DATA,ffff80aabbccddee\n101,123,DATA,01\n"
                output, report = target.convert_canopen(data, include_emergency=True,
                                                         emergency_layout=[f"{node}:{hex(canid)}"])
                self.assertEqual((report["frames"], report["decoded_frames"], len(report["unsupported_frames"])), (5, 3, 2))
                self.assertEqual(report["configured_emergency_layout"], [{"node_id": node, "effective_cob_id": canid}])
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([r["source_node_id"] for r in rows], [str(node), str(node), "7"])
                self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_frames_hex"][4:])), [original])
                reader = os.environ.get("MUSUBI_TELEMETRY_READER")
                if not reader:
                    self.skipTest("actual common reader required for connection evidence")
                path = Path(directory) / "observations.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                fields = [o["fields"] for o in common["observations"]]
                self.assertEqual(fields[0]["canopen_error_code"], 0x8130)
                self.assertEqual(fields[0]["canopen_error_communication_reported"], 1)
                self.assertEqual(fields[0]["canopen_error_reserved_reported"], 1)
                self.assertEqual(fields[0]["canopen_emergency_cob_id"], canid)
                self.assertIn("CALLER", fields[0]["canopen_address_basis"])
                self.assertEqual(fields[1]["canopen_error_report_kind"], "ERROR_RESET_REPORTED")
                self.assertEqual(fields[2]["node_reported_mode"], "OPERATIONAL")
                self.assertIsNone(fields[2]["canopen_emergency_cob_id"])
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_flags_collisions_shapes_and_ambiguous_configuration_refuse(self):
        good = HEADER + "1,181,DATA,0100010000000000\n"
        invalid = [[], ["0:0x181"], ["128:0x181"], ["1:0x80000181"], ["1:0x20000181"],
                   ["1:0x800"], ["1:0"], ["1:0x80"], ["1:0x101"], ["1:0x180"],
                   ["1:0x581"], ["1:0x601"], ["1:0x6e0"], ["1:0x701"],
                   ["1:0x181", "2:0x181"], ["1:0x181", "1:0x201"], [True], "1:0x181"]
        for layout in invalid:
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                target.convert_canopen(good, include_emergency=True, emergency_layout=layout)
        for data in [good.replace("DATA", "RTR"), good.replace("frame_type,", "").replace("DATA,", ""),
                     good.replace("0100010000000000", "01"), good + "0,181,DATA,0100010000000000\n",
                     good.replace(",181,", ",800,")]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                target.convert_canopen(data, include_emergency=True, emergency_layout=["1:0x181"])
        for extra in [dict(include_emergency=False), dict(include_emergency=True, emergency_source=target.CANOPEN_CONSUMER_SOURCE),
                      dict(include_emergency=True, guarding_source=target.CANOPEN_GUARDING_SOURCE)]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                target.convert_canopen(good, emergency_layout=["1:0x181"], **extra)
        with self.assertRaises(ValueError):
            target.convert_canopen(HEADER + "1,182,DATA,0100010000000000\n",
                                   include_emergency=True, emergency_layout=["1:0x181"])

    def test_cli_and_default_bytes_unchanged(self):
        # Existing pre-change expected hashes, not regenerated expectations.
        from tests.test_canopen_consumer import ConsumerReports
        ConsumerReports().test_legacy_output_and_report_bytes_remain_unchanged()
        data = HEADER + "1,181,DATA,0100010000000000\n"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "capture.csv"
            source.write_text(data)
            args = [os.sys.executable, str(ROOT / "scripts/convert_dronecan_capture.py"),
                    str(source), str(base / "out"), "--protocol", "canopen-default-status",
                    "--canopen-emergency-layout", "1:0x181"]
            subprocess.run(args, check=True, capture_output=True)
            report = json.loads((base / "out/accounting.json").read_text())
            self.assertEqual(report["decoded_frames"], 1)
            saved = (base / "out/observations.csv").read_bytes()
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
            self.assertEqual((base / "out/observations.csv").read_bytes(), saved)
            args[args.index("canopen-default-status")] = "canopen-heartbeat"
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
