import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_farmbot_point_list import convert

ROOT = Path(__file__).resolve().parents[1]


def point(i=1, kind="Plant"):
    return dict(id=i, device_id=4, pointer_type=kind, x=1250, y=2000, z=-25, meta={"future": [1, 2]})


class PointList(unittest.TestCase):
    def test_distinct_lists_common_and_empty(self):
        for points in ([point(1, k) for k in ("Plant", "ToolSlot", "Weed", "GenericPointer")],
                       [dict(point(i+1), x=i) for i in range(1001)], []):
            raw = json.dumps(points).encode()
            text, report = convert(raw, version="v15.30.5", unit="mm", capture_time_us=17)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(len(rows), len(points))
            self.assertEqual(report["source_items"], len(points))
            for i, row in enumerate(rows):
                self.assertEqual(int(row["source_array_index"]), i)
                original = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
                self.assertEqual(original["point"], points[i])
                self.assertAlmostEqual(float(row["farm_point_x_m"]), points[i]["x"]/1000)
            with tempfile.TemporaryDirectory() as d:
                p = Path(d)/"out.csv"; p.write_text(text)
                cmd = [os.environ["MUSUBI_TELEMETRY_READER"],
                       str(ROOT/"profiles/declared/electrical-schema-reuse/json-profile.toml"),
                       str(p), "--allow-equal-time"]
                if not points:
                    cmd.append("--allow-empty-rows")
                common = json.loads(subprocess.run(cmd, check=True, capture_output=True).stdout)
                self.assertEqual(len(common["observations"]), len(points))

    def test_refusals(self):
        for raw in (b'{}', b'null', b'[null]', b'[{"id":1,"id":2}]',
                    json.dumps([point(),dict(point(),pointer_type="Future")]).encode(),
                    json.dumps([dict(point(),x=True)]).encode()):
            with self.assertRaises(ValueError):
                convert(raw, version="v15.30.5", unit="mm", capture_time_us=0)
        for kwargs in (dict(version="other",unit="mm",capture_time_us=0),
                       dict(version="v15.30.5",unit="m",capture_time_us=0),
                       dict(version="v15.30.5",unit="mm",capture_time_us=True)):
            with self.assertRaises(ValueError):
                convert(b'[]', **kwargs)

    def test_cli_original_retention(self):
        raw = json.dumps([point()]).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"response.json").write_bytes(raw)
            command = [os.sys.executable, str(ROOT/"scripts/convert_farmbot_point_list.py"),
                       str(root/"response.json"), str(root/"result"), "--version", "v15.30.5",
                       "--unit", "mm", "--capture-time-us", "0"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((root/"result/source-response.json").read_bytes(), raw)
            report = json.loads((root/"result/report.json").read_text())
            self.assertEqual(report["converted_items"], 1)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
            self.assertEqual((root/"result/source-response.json").read_bytes(), raw)
