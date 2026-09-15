import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_opcua_recorded import convert, convert_sensorthings

ROOT = Path(__file__).resolve().parents[1]


def record(value=12.5, code=0):
    return dict(capture_time_us=15, node_id="nsu=urn:synthetic;s=voltage",
        data_value=dict(UaType=11, Value=value, Status={"Code": code},
            SourceTimestamp="2026-01-01T00:00:00.123456789Z", SourcePicoseconds=4,
            ServerTimestamp="2026-01-01T00:00:01Z"), vendor={"kept": True})


class DataValues(unittest.TestCase):









    def test_cli_refuses_overwriting_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source, report = Path(directory) / "in.jsonl", Path(directory) / "report.json"
            text = json.dumps(record())
            source.write_text(text)
            result = subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded",
                str(source), str(source), str(report), "--node", record()["node_id"], "--unit", "V"],
                cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_text(), text)
            self.assertFalse(report.exists())


    def test_boundaries_and_missing_optional_time_status(self):
        r = record()
        r["data_value"] = {"UaType": 11, "Value": 2}
        output, report = convert(json.dumps(r), r["node_id"], "A")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["source_time_us"], "")
        self.assertEqual(row["current_a"], "2.0")
        for change in ({"Value": True}, {"Value": "NaN"}, {"UaType": 1}, {"Dimensions": []},
                       {"Status": {"Code": -1}}, {"Status": {"Symbol": "Bad"}},
                       {"SourceTimestamp": "2026-02-30T00:00:00Z"}):
            r = record()
            r["data_value"].update(change)
            with self.assertRaises((ValueError, TypeError)):
                convert(json.dumps(r), r["node_id"], "V")
        with self.assertRaises(ValueError):
            convert(json.dumps(record(-1)), record()["node_id"], "K")
        for unit, field in (("K", "temperature_k"), ("m", "distance_m"), ("Pa", "pressure_pa")):
            output, _ = convert(json.dumps(record(5)), record()["node_id"], unit)
            self.assertEqual(float(next(csv.DictReader(io.StringIO(output)))[field]), 5)
