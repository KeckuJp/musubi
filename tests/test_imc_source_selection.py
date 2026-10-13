"""Authored Neptus-console-shaped LSF: interleaved sender clocks, not a real Neptus recording.

Pinned Neptus 564e44a5 logs each received message with its sender's own header timestamp
(SystemImcMsgCommInfo.logMessage -> LsfMessageLogger.log -> IMCMessage.serialize, which only
stamps a zero timestamp) and its own sent messages with local time (ImcMsgManager), so one file
can be monotonic per source yet decrease globally.
"""
import csv
import gzip
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts import convert_imc_gzip_window as gzip_window
from scripts import convert_imc_sensors as c
from tests.saved_can_window_helpers import common

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/imc-sensors/profile.toml"
VEHICLE, CONSOLE = 31, 0x4001
STATE = [.3, -.4, 10, -1, 2, 3, .1, .2, .3, 1, 2, 3, -4, 5, -6, .1, .2, .3, 4, 0]


def frame(message, payload, source, timestamp, order="<"):
    raw = struct.pack(order + "HHHdHBHB", 0xFE54, message, len(payload), timestamp, source, 3, 65535, 255) + payload
    return raw + struct.pack(order + "H", c.crc16(raw))


def console_log(order="<"):
    """Records 0..6; the console's clock is ahead of the vehicle's, so time decreases at 1 and 4."""
    return b"".join((
        frame(3, b"entity-info", CONSOLE, 1000.0, order),                       # 0 console, unsupported
        frame(263, struct.pack(order + "f", 20), VEHICLE, 500.0, order),         # 1 vehicle
        frame(265, struct.pack(order + "f", 7.5), VEHICLE, 500.5, order),        # 2 vehicle
        frame(65000, b"console-sent", CONSOLE, 1001.0, order),                   # 3 console, unsupported
        frame(350, struct.pack(order + "dd18f", *STATE), VEHICLE, 501.0, order), # 4 vehicle
        frame(263, struct.pack(order + "f", 21), VEHICLE, 501.0, order),         # 5 vehicle, equal time
        frame(64999, b"vehicle-other", VEHICLE, 502.0, order),                   # 6 vehicle, unsupported
    ))


def write(folder, data, name="Data.lsf"):
    path = Path(folder) / name
    path.write_bytes(data)
    return path


class SourceSelectionTests(unittest.TestCase):
    def test_interleaved_clocks_select_one_source_to_common(self):
        for order in ("<", ">"):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as folder:
                source = write(folder, console_log(order))
                with self.assertRaises(ValueError):
                    c.convert_path(source, start_record=0, stop_record=7, schema=c.NEPTUS_SCHEMA,
                                   estimated_state=True)
                output, report = c.convert_path(source, start_record=0, stop_record=7, schema=c.NEPTUS_SCHEMA,
                                                estimated_state=True, source_id=VEHICLE)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([r["source_record_index"] for r in rows], ["1", "2", "4", "5"])
                self.assertEqual({r["source_id"] for r in rows}, {str(VEHICLE)})
                self.assertEqual([u["source_record_index"] for u in report["unsupported_frames"]], [6])
                selection = report["record_selection"]
                self.assertEqual((selection["source_records"], selection["selected_records"],
                                  selection["excluded_records"]), (7, 5, 2))
                self.assertEqual(selection["source_selection"]["other_source_records_by_id"], {str(CONSOLE): 2})
                self.assertEqual(selection["source_selection"]["other_source_records_in_interval"], 2)
                self.assertEqual(selection["header_clock_scope"], "SELECTED_SOURCE_REPORTED_DISPATCHER_TIME_ONLY")
                observations = common(self, Path(folder), output, PROFILE)["observations"]
                self.assertEqual(len(observations), 4)
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
                self.assertEqual(observations[2]["fields"]["estimated_offset_north_m"], -1)
                self.assertEqual(observations[0]["fields"]["temperature_k"], 293.15)
                packed = write(folder, gzip.compress(console_log(order), mtime=0), "Data.lsf.gz")
                compressed, gzip_report = gzip_window.convert(packed, start_record=0, stop_record=7,
                    schema=c.NEPTUS_SCHEMA, estimated_state=True, source_id=VEHICLE)
                self.assertEqual(compressed, output)
                self.assertEqual(gzip_report["record_selection"]["source_selection"]["source_id"], VEHICLE)

    def test_default_and_truthful_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            log = console_log()
            source = write(folder, log)
            for kwargs in (dict(source_id=CONSOLE), dict(source_id=99), dict(source_id=True),
                           dict(source_id=-1), dict(source_id=65536)):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    c.convert_path(source, start_record=0, stop_record=7, schema=c.NEPTUS_SCHEMA, **kwargs)
            with self.assertRaises(ValueError):  # interval holds only console frames
                c.convert_path(source, start_record=3, stop_record=4, source_id=VEHICLE, schema=c.NEPTUS_SCHEMA)
            reversed_vehicle = log + frame(263, struct.pack("<f", 1), VEHICLE, 499.0)
            with self.assertRaises(ValueError):  # selected source reversal still refuses
                c.convert_path(write(folder, reversed_vehicle, "r.lsf"), start_record=0, stop_record=8,
                               source_id=VEHICLE, schema=c.NEPTUS_SCHEMA, estimated_state=True)
            bad = bytearray(log)
            bad[len(frame(3, b"entity-info", CONSOLE, 1000.0)) - 1] ^= 0xFF
            output, report = c.convert_path(write(folder, bytes(bad), "crc.lsf"), start_record=0, stop_record=7,
                                            source_id=VEHICLE, schema=c.NEPTUS_SCHEMA, estimated_state=True)
            self.assertEqual(report["record_selection"]["source_selection"]["other_sources"],
                             "HEADER_SYNC_SIZE_AND_TIME_RANGE_CHECKED_ORDER_PAYLOAD_AND_CRC_NOT_VALIDATED")
            bad_sync = bytearray(log)
            bad_sync[0] = 0
            with self.assertRaises(ValueError):  # broken framing anywhere still refuses
                c.convert_path(write(folder, bytes(bad_sync), "sync.lsf"), start_record=0, stop_record=7,
                               source_id=VEHICLE, schema=c.NEPTUS_SCHEMA)
            single = b"".join(frame(263, struct.pack("<f", v), VEHICLE, 500.0 + v) for v in (1, 2))
            plain, plain_report = c.convert_path(write(folder, single, "single.lsf"), start_record=0, stop_record=2)
            self.assertNotIn("source_selection", plain_report["record_selection"])
            self.assertEqual(plain_report["record_selection"]["header_clock_scope"], "WHOLE_SOURCE_REPORTED_DISPATCHER_TIME")
            self.assertEqual((plain_report["record_selection"]["selected_records"],
                              plain_report["record_selection"]["excluded_records"]), (2, 0))
            command = ["python3", "scripts/convert_imc_sensors.py", str(source), str(Path(folder) / "out"),
                       "--source-format", c.NEPTUS_SCHEMA, "--estimated-state", "--source-id", str(VEHICLE)]
            self.assertNotEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)
            subprocess.run(command + ["--start-record", "0", "--stop-record", "7"], cwd=ROOT,
                           check=True, capture_output=True)
            self.assertEqual((Path(folder) / "out/observations.csv").read_text(),
                             c.convert_path(source, start_record=0, stop_record=7, source_id=VEHICLE,
                                            schema=c.NEPTUS_SCHEMA, estimated_state=True)[0])


if __name__ == "__main__":
    unittest.main()
