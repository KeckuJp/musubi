"""Authored saved corner summaries, no model/image or inference execution."""
import copy
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
from scripts.convert_video_frames import convert_detections, convert_detections_path
from tests.test_detection_window import yolo, deepstream

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/video-frame-export/detection-profile.toml"


def obb(normalized=False):
    result = yolo(normalized)
    result["image_width"], result["image_height"] = 200, 100
    coords = (120,10, 160,50, 120,90, 80,50)
    result["detections"][0]["box"] = {f"{axis}{n}": coords[2*(n-1)+(axis=='y')] / (200 if axis=='x' else 100)
        if normalized else coords[2*(n-1)+(axis=='y')] for n in range(1,5) for axis in "xy"}
    result["detections"][0]["unknown"] = {"label":"原本"}
    return result


class SavedOBB(unittest.TestCase):
    def common(self, output):
        self.assertIn("MUSUBI_TELEMETRY_READER", os.environ)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"out.csv"; path.write_text(output)
            return json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],str(PROFILE),str(path),
                "--allow-equal-time"],check=True,capture_output=True).stdout)

    def test_pixel_normalized_order_empty_and_common(self):
        items=[obb(),obb(True),dict(obb(),detections=[])]
        items[1]["image_width"],items[1]["image_height"]=400,200
        items[1]["detections"][0]["box"]["x4"]=-.1
        text="\n".join(json.dumps(item,ensure_ascii=False) for item in items)
        output,report=convert_detections(text,3,obb=True)
        rows=list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["detections"],report["empty_records"],len(rows)),(2,1,3))
        self.assertEqual([row["obb_extent_disposition"] for row in rows],
            ["WITHIN_DECLARED_IMAGE_EXTENT","EXTENDS_OUTSIDE_DECLARED_IMAGE",""])
        self.assertTrue(all(rows[0][key]=="" for key in ("box_x1_px","box_y1_px","box_x2_px","box_y2_px")))
        self.assertEqual(rows[2]["detection_status"],"NO_DETECTIONS_REPORTED")
        self.assertEqual(rows[2]["obb_corner1_x_px"],"")
        common=self.common(output)
        self.assertEqual((common["main_rows"],common["platform_domain"]),(3,"Unknown"))
        for index,item in enumerate(items):
            self.assertEqual(json.loads(report["source_records"][index]["original_line"]),item)
            self.assertEqual(common["observations"][index]["clock_basis"],"Unknown")
            self.assertIsNone(common["observations"][index]["anchor_unix_us"])
        for index,item in enumerate(items[:2]):
            for n in range(1,5):
                for axis in "xy":
                    expected=item["detections"][0]["box"][f"{axis}{n}"]
                    if item["normalized"]: expected*=item["image_width" if axis=="x" else "image_height"]
                    key=f"obb_corner{n}_{axis}_px"
                    self.assertAlmostEqual(common["observations"][index]["fields"][key],expected)
                    self.assertIn("not_world",common["profile_units"][key])
        self.assertEqual(rows[0]["obb_corner_order"],"SOURCE_ORDER_NOT_REORDERED")

    def test_shape_finite_missing_and_selection_refusals(self):
        item=obb()
        variants=[yolo(),dict(item,exporter_version="8.3.1"),dict(item,normalized=1),dict(item,image_width=0)]
        for value in (None,True,".5",float("nan"),float("inf"),1e309):
            bad=copy.deepcopy(item);bad["detections"][0]["box"]["x1"]=value;variants.append(bad)
        for key in ("x4","confidence"):
            bad=copy.deepcopy(item)
            del (bad["detections"][0]["box"] if key=="x4" else bad["detections"][0])[key]
            variants.append(bad)
        bad=copy.deepcopy(item);bad["detections"][0]["box"]["extra"]=1;variants.append(bad)
        bad=obb(True);bad["detections"][0]["box"]["x1"]=1e308;variants.append(bad)
        for bad in variants:
            with self.subTest(item=bad),self.assertRaises(ValueError): convert_detections(json.dumps(bad),3,obb=True)
        for options in ({},{"deepstream":True,"obb":True},{"obb":1}):
            with self.assertRaises(ValueError): convert_detections(json.dumps(item),3,**options)
        # Repeated corners can be rounded source reports; never repaired into a rectangle.
        for key in item["detections"][0]["box"]: item["detections"][0]["box"][key]=0
        self.assertEqual(convert_detections(json.dumps(item),3,obb=True)[1]["detections"],1)

    def test_existing_window_and_cli_preserve_record_binding(self):
        item=obb(True);line=json.dumps(item)
        raw=("not-json\n"+line+"\r\n").encode()
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);path=base/"in.jsonl";path.write_bytes(raw)
            output,report=convert_detections_path(path,3,obb=True,start_record=1,stop_record=2)
            row=next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(row["source_record_index"],"1")
            self.assertEqual(row["source_file_sha256"],"sha256:"+hashlib.sha256(raw).hexdigest())
            self.assertEqual(report["record_selection"]["excluded_records"],1)
            self.assertEqual(report["record_selection"]["outside_selection"],"NOT_DECODED_OR_VALIDATED")
            self.assertEqual(self.common(output)["main_rows"],1)
            command=[sys.executable,str(ROOT/"scripts/convert_video_frames.py"),str(path),"3",str(base/"good"),
                     "--obb-830","--start-record","1","--stop-record","2"]
            subprocess.run(command,check=True,capture_output=True)
            self.assertEqual((base/"good/observations.csv").read_text(),output)
            for flag in ("--detections-830","--deepstream-900","--compact","--progress-81"):
                rejected = command.copy()
                target = base / flag[2:]
                rejected[4] = str(target)
                self.assertNotEqual(subprocess.run(rejected+[flag],capture_output=True).returncode,0)
                self.assertFalse(target.exists())

    def test_axis_aligned_and_deepstream_hashes_unchanged(self):
        for item,ds,expected in [(yolo(),False,("a66f6db523a8661c0f950b590f427f560a1f67dc78d014c88b5861b2471fdef7","2ad2171b95180ffc268aa673cf73be0dc2e1b0d62e4d193e048335a113419644")),
            (deepstream(),True,("50bbae642fd3032a7c0546d9ed831fe32f8d8a96a8b0241ffa0a36a30680423f","4ab8706ff4167fc88c5e82ab3df768c6dd2a379f0ad04277a9a7e8e717a739f8"))]:
            output,report=convert_detections(json.dumps(item),3,deepstream=ds)
            self.assertEqual((hashlib.sha256(output.encode()).hexdigest(),hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()),expected)
