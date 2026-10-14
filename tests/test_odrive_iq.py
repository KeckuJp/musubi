"""Authored source-shaped frames; no motor or CAN interface is opened."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts.convert_declared_expectation_check import convert as compare

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "odrive-fw0.5.6-cansimple"
DBC = ('VERSION ""\nNS_ :\nBS_:\nBU_: Controller Host\n' + ''.join(
    f'BO_ {node * 32 + 20} Node{node}_Iq: 8 Controller\n'
    ' SG_ Iq_Setpoint : 0|32@1+ (1,0) [0|0] "A" Host\n'
    ' SG_ Iq_Measured : 32|32@1+ (1,0) [0|0] "A" Host\n'
    f'SIG_VALTYPE_ {node * 32 + 20} Iq_Setpoint : 1;\n'
    f'SIG_VALTYPE_ {node * 32 + 20} Iq_Measured : 1;\n' for node in (0, 1))).encode()
HEADER = "time_us,standard_id,data_hex\n"


def frame(time, node, left, right):
    return f"{time},{node * 32 + 20:x},{struct.pack('<ff', left, right).hex()}\n"


def common(output, profile):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "values.csv"
        source.write_text(output)
        return json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
            str(ROOT / f"profiles/declared/{profile}/profile.toml"),
            str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)


class ReportedIq(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("scripts.convert_odrive_iq")

    def convert(self, data, **kwargs):
        with patch.object(self.module, "DBC_SHA256", hashlib.sha256(DBC).hexdigest()):
            return self.module.convert(data.encode(), DBC, source=SOURCE, node_id=0, **kwargs)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual shared reader required")
    def test_same_frame_pairs_and_declared_comparison_reach_common(self):
        data = HEADER + frame(10, 0, 2, 2.5) + frame(10, 1, 90, -90)
        data += frame(10, 0, -2, -3) + frame(10, 0, 0, 0) + frame(10, 0, 0, 0)
        output, report = self.convert(data)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([int(r["source_frame_index"]) for r in rows], [0, 2, 3, 4])
        self.assertEqual([r["setpoint_disposition"] for r in rows[-2:]],
                         ["ZERO_REPORTED_OR_MISSING_SUBSTITUTED"] * 2)
        self.assertEqual((report["frames"], report["selected_frames"], report["unselected_frames"],
                          report["pairs"]), (5, 4, 1, 4))
        self.assertEqual(report["unselected"][0]["source"], data.splitlines()[2].split(","))
        records = common(output, "odrive-iq")["observations"]
        self.assertEqual(len(records), 4)
        self.assertEqual(records[1]["fields"]["iq_measured_a"], -3)
        self.assertEqual(records[0]["clock_basis"], "Unknown")
        checked, stats = compare(output.encode(), clock_basis="capture_us_unknown",
            value_meaning="ODrive fw-v0.5.6 Get_Iq: reported measured minus reported setpoint in A",
            difference_columns=("iq_measured_a", "iq_setpoint_a"), tolerance="0.5",
            subjects=("source_frame_index", "node_id", "frame_id", "source_data_hex",
                      "source_input_sha256", "source_dbc_sha256", "producer_basis", "setpoint_disposition"))
        self.assertEqual((stats["within_declared_tolerance"], stats["beyond_declared_tolerance"]), (3, 1))
        compared = common(checked, "declared-expectation-checks")["observations"]
        self.assertEqual([r["fields"]["difference_value"] for r in compared], [.5, -1, 0, 0])

    def test_rejections_and_finite_limits(self):
        good = HEADER + frame(1, 0, 1, 2)
        for data in [HEADER, HEADER + frame(1, 1, 1, 2), HEADER + "1,14,00\n",
                     HEADER + frame(2, 0, 1, 2) + frame(1, 0, 1, 2),
                     HEADER + frame(1, 0, float("nan"), 2),
                     HEADER + frame(1, 0, 1, float("inf")),
                     HEADER + frame(1, 0, 1e-40, 2), HEADER + frame(1, 0, 1e30, 2),
                     HEADER + "1,14,00,extra\n"]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.convert(data)
        with self.assertRaises(ValueError):
            self.module.convert(good.encode(), DBC, source=SOURCE, node_id=0)
        with patch.object(self.module, "DBC_SHA256", hashlib.sha256(DBC).hexdigest()):
            for source, node in [("other", 0), (SOURCE, True), (SOURCE, 8), (SOURCE, -1)]:
                with self.assertRaises(ValueError):
                    self.module.convert(good.encode(), DBC, source=source, node_id=node)
        for limit in ("INPUT_LIMIT", "FRAME_LIMIT", "OUTPUT_LIMIT", "REPORT_LIMIT"):
            with self.subTest(limit=limit), patch.object(self.module, limit, 1), self.assertRaises(ValueError):
                self.convert(good + frame(1, 0, 2, 3))

    def test_decoder_pair_provenance_is_not_trusted_by_name_alone(self):
        good = HEADER + frame(1, 0, 1, 2)
        original = self.module.decode

        def changed(text, dbc):
            output, report = original(text, dbc)
            return output.replace("hex:49715f4d65617375726564", "hex:49715f536574706f696e74"), report

        with patch.object(self.module, "decode", changed), self.assertRaises(ValueError):
            self.convert(good)
