"""Authored saved log text; no CAN device or real capture claim."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_candump_recorded import convert, convert_slcan
from scripts.convert_dronecan_capture import convert_canopen, convert_j1939

ROOT = Path(__file__).resolve().parents[1]


class SavedCandump(unittest.TestCase):


    def test_unconverted_records_and_drop_notice_are_never_data_frames(self):
        bodies = ["can0 701#05", "can1 702#04", "can0 00000701#05", "can0 123#R8",
                  "can0 123##10011", "can0 20000004#0000000000000000"]
        text = "\n".join("(1700000000.000000) " + body for body in bodies)
        text += "\nDROPCOUNT: dropped 2 CAN frames on 'can0' socket (total drops 2)\n"
        output, report = convert(text, "can0")
        self.assertEqual((report["source_lines"], report["selected_frames"], report["unconverted_lines"]), (7, 1, 6))
        self.assertEqual([row["reason"] for row in report["records"]][1:],
                         ["UNSELECTED_INTERFACE", "UNSELECTED_FRAME_KIND", "REMOTE_REQUEST", "CAN_FD", "ERROR_FRAME", "SOURCE_DROP_REPORT"])
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 1)

    def test_important_format_time_and_selection_failures(self):
        good = "(1700000000.000000) can0 701#05"
        for text in ("", "(0.000000) can0 701#05", good.replace("701#05", "800#05"),
                     good.replace("701#05", "701#0"), good.replace("701#05", "701#" + "00"*9),
                     good + "\n" + good.replace("1700000000", "1699999999"),
                     good + "\nDROPCOUNT: corrupted", good.replace("can0", "can1")):
            with self.assertRaises(ValueError):
                convert(text, "can0")


if __name__ == "__main__":
    unittest.main()
