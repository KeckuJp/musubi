"""Authored CANopenNode writer-pattern reports, not observed bus failures."""
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
from scripts.convert_dronecan_capture import convert_canopen

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "canopennode-v4.1-hbconsumer"
HEADER = "time_us,standard_id,data_hex\n"
PROFILE = ROOT / "profiles/declared/dronecan-recorded/canopen-consumer-profile.toml"
BASE = HEADER + "1,081,3081101b02000000\n1,701,05\n2,090,0000001b00000000\n3,123,00\n"


def convert(data):
    return convert_canopen(data, include_emergency=True, emergency_source=SOURCE)


class ConsumerReports(unittest.TestCase):
    def test_legacy_output_and_report_bytes_remain_unchanged(self):
        expected = [
            ("7ff827b7ec12c19090c6c0e1659f1435beeae8c330dc24e349ca905ec3a854ad", "e2fcce7090732dfbf941709644314894a67f5d9872844217be1af660f24c32c6"),
            ("2adb6d0c9dec9e8f80d1e24ec5588f91ceb5d46464909b3000fce761aea0895d", "d53096416db28c7dd9f1de88c4cbbd9303c340bdc0bc5ca2c913b0153b0b3dc3")]
        for enabled, hashes in zip((False, True), expected):
            output, report = convert_canopen(BASE, include_emergency=enabled)
            self.assertEqual((hashlib.sha256(output.encode()).hexdigest(),
                hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()), hashes)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_explicit_timeout_remote_reset_and_aggregate_clear_reach_common(self):
        inputs = [BASE,
            HEADER + "9,082,0000001c00000000\n9,082,3081101c07000000\n9,082,3081101b00000000\n9,082,3081101b00000000\n"]
        for data, events, slots, nodes in [
            (inputs[0], ["HEARTBEAT_CONSUMER_TIMEOUT_REPORTED", "", "HEARTBEAT_CONSUMER_TIMEOUT_RESET_REPORTED"],
             ["2", "", ""], [1, 1, 16]),
            (inputs[1], ["REMOTE_RESET_CONDITION_RESET_REPORTED", "REMOTE_RESET_REPORTED",
                         "HEARTBEAT_CONSUMER_TIMEOUT_REPORTED", "HEARTBEAT_CONSUMER_TIMEOUT_REPORTED"],
             ["", "7", "0", "0"], [2, 2, 2, 2])]:
            output, report = convert(data)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r["canopen_consumer_event"] for r in rows], events)
            self.assertEqual([r["canopen_monitored_slot"] for r in rows], slots)
            self.assertEqual([int(r["source_node_id"]) for r in rows], nodes)
            self.assertEqual(report["consumer_event_rows"], sum(bool(v) for v in events))
            self.assertEqual(report["frames"], len(data.splitlines()) - 1)
            self.assertEqual(report["frames"], report["decoded_frames"] + len(report["unsupported_frames"]))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "reports.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(len(common["observations"]), len(events))
            self.assertTrue(all(r["clock_basis"] == "Unknown" for r in common["observations"]))
            for row, observation in zip(rows, common["observations"]):
                self.assertEqual(observation["fields"]["source_frames_hex"], row["source_frames_hex"])
                if row["canopen_monitored_slot"]:
                    self.assertEqual(observation["fields"]["canopen_monitored_slot"], int(row["canopen_monitored_slot"]))
                if row["canopen_consumer_event"] in ("REMOTE_RESET_CONDITION_RESET_REPORTED",
                                                        "HEARTBEAT_CONSUMER_TIMEOUT_RESET_REPORTED"):
                    self.assertEqual(row["canopen_monitored_slot"], "")
                    self.assertEqual(row["canopen_consumer_scope"], "AGGREGATE_CONDITION_RESET_NOT_NODE_RECOVERY")
            self.assertIn("not_node_ID", common["profile_units"]["canopen_monitored_slot"])

    def test_unknown_patterns_preservation_and_important_refusals(self):
        data = HEADER + "1,081,3081101bff000000\n1,081,0000001b01000000\n1,082,1000101b01000000\n1,082,308150aa78563412\n"
        output, report = convert(data)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["canopen_consumer_event"] for r in rows], ["UNINTERPRETED_SOURCE_PATTERN"] * 4)
        self.assertTrue(all(r["canopen_monitored_slot"] == "" for r in rows))
        self.assertEqual(report["consumer_uninterpreted_rows"], 4)
        self.assertEqual(rows[-1]["canopen_info_code"], str(0x12345678))
        self.assertIn("NONCONFORMANT", rows[-1]["canopen_error_register_disposition"])
        for data in (HEADER + "1,081,00\n", HEADER + "2,081,3081101b02000000\n1,701,05\n",
                     HEADER + "1,081,nothex\n", HEADER + "1,081,3081101b02000000,extra\n"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                convert(data)
        for include, source in ((False, SOURCE), (True, "canopennode-other"), (True, "")):
            with self.assertRaises(ValueError):
                convert_canopen(BASE, include_emergency=include, emergency_source=source)

    def test_cli_requires_the_corresponding_protocol_and_preserves_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.csv"
            source.write_text(BASE)
            prefix = [sys.executable, "-m", "scripts.convert_dronecan_capture", str(source)]
            flags = ["--canopen-emergency-source", SOURCE]
            for protocol in ("canopen-heartbeat", "j1939-dm-single"):
                output = root / protocol
                completed = subprocess.run(prefix + [str(output), "--protocol", protocol] + flags,
                                           cwd=ROOT, capture_output=True)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(output.exists())
            output = root / "success"
            command = prefix + [str(output), "--protocol", "canopen-default-status"] + flags
            subprocess.run(command, cwd=ROOT, capture_output=True, check=True)
            before = (output / "observations.csv").read_bytes()
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)
            self.assertEqual((output / "observations.csv").read_bytes(), before)
