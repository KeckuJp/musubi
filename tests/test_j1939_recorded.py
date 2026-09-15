"""Synthetic fixed DM reports; not vehicle or SAE-wide qualification."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dronecan_capture import convert_j1939

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,extended_id,data_hex\n"


class RecordedDiagnostics(unittest.TestCase):

    def test_bam_missing_order_overlap_padding_and_mixed_method(self):
        cm = "10,1cecff80,200a0002ffcafe00\n"
        first = "11,1cebff80,0100000100020502\n"
        last = "12,1cebff80,02000083ffffffff\n"
        output, report = convert_j1939(HEADER + cm + first + last, bam=True)
        self.assertEqual(report["observations"], 1)
        self.assertEqual(len(report["unsupported_dtcs"]), 1)
        self.assertEqual(report["decoded_frames"], 3)
        self.assertIn("2", output)
        for body in (cm + first, cm + last, cm + cm, cm.replace("0a0002", "0a0003") + first + last,
                     cm + first + last.replace("ffffffff", "ffffff00"),
                     cm + first + first.replace("11,", "12,") + last):
            with self.assertRaises(ValueError):
                convert_j1939(HEADER + body, bam=True)

    def test_bam_interleaved_sources_and_maximum_packet_boundary(self):
        lines = ["1,1cecff80,200a0002ffcafe00", "1,1cecff81,200a0002ffcbfe00",
                 "2,1cebff81,0100000100020502", "2,1cebff80,0100000300020504",
                 "3,1cebff80,02000003ffffffff", "4,1cebff81,02000003ffffffff"]
        rows = list(csv.DictReader(io.StringIO(convert_j1939(HEADER + "\n".join(lines) + "\n", bam=True)[0])))
        self.assertEqual([r["source_node_id"] for r in rows], ["128", "128", "129", "129"])
        self.assertEqual([r["spn_reported"] for r in rows], ["3", "4", "1", "2"])
        payload = b"\x00\x00" + bytes.fromhex("01000205") * 445  # 1782 bytes, 255 packets.
        cm = bytes([32]) + len(payload).to_bytes(2, "little") + bytes.fromhex("ffffcafe00")
        lines = ["1,1cecff80," + cm.hex()]
        for index in range(255):
            frame = bytes([index + 1]) + payload[index*7:index*7+7].ljust(7, b"\xff")
            lines.append(f"{index+2},1cebff80,{frame.hex()}")
        _, report = convert_j1939(HEADER + "\n".join(lines) + "\n", bam=True)
        self.assertEqual((report["observations"], report["decoded_frames"]), (445, 256))


    def test_method_boundary_zero_report_and_invalid_exports(self):
        output, report = convert_j1939(HEADER + "1,18feca80,000000000000ffff\n2,18feca80,000001000080ffff\n")
        self.assertEqual(report["decoded_frames"], 1)
        self.assertEqual(len(report["unsupported_frames"]), 1)
        self.assertIn("ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH", output)
        for body in ("1,18feca80,0000\n", "1,18feca80,0000000000000000\n",
                     "1,ffffffff,000000000000ffff\n", "2,18feca80,000000000000ffff\n1,18feca80,000000000000ffff\n"):
            with self.assertRaises(ValueError):
                convert_j1939(HEADER + body)
        with self.assertRaises(ValueError):
            convert_j1939(HEADER.replace("extended_id","standard_id") + "1,123,000000000000ffff\n")
