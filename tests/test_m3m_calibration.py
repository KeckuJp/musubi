"""Authored calibration reports; no real camera, image calibration or drift evidence."""
import csv
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_m3m_capture import authored_packet

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/m3m-calibration/profile.toml"
TRANSITIONS = ROOT / "profiles/declared/saved-state-transitions/profile.toml"
BASIS = "caller-declared-native-unresized-uncropped"
DEWARP = "2022-10-24;2200.899902343750,2200.219970703125,10.609985351562,-6.575988769531,0.0081,-0.0429,-0.0003,0.0002,0"


def record(**changes):
    value = {"ExifTool:ExifToolVersion": 13.59, "XMP:Version": 1.6,
             "XMP:ImageSource": "MS_NIR_CAMERA", "XMP:BandName": "NIR", "XMP:Sensorindex": 4,
             "XMP:DroneModel": "M3M", "XMP:CameraSerialNumber": "AUTHORED-CAMERA",
             "EXIF:ImageWidth": 2592, "EXIF:ImageHeight": 1944, "XMP:DewarpData": DEWARP,
             "XMP:Uninterpreted": "unknown,0"}
    value.update(changes)
    return value


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class CalibrationTests(unittest.TestCase):
    def module(self):
        return importlib.import_module("scripts.convert_m3m_calibration")

    def common(self, text, profile):
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("actual common reader not supplied")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.csv"
            path.write_text(text)
            run = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(path),
                                  "--allow-equal-time"], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            return json.loads(run.stdout)

    def test_fixed_exporter_shape(self):
        exporter = os.environ.get("MUSUBI_EXIFTOOL")
        if not exporter:
            self.skipTest("fixed exporter not supplied; no export-shape success claim")
        fields = [("Version", "1.6"), ("ImageSource", "MS_NIR_CAMERA"), ("BandName", "NIR"),
                  ("Sensorindex", "4"), ("DroneModel", "M3M"),
                  ("CameraSerialNumber", "AUTHORED-CAMERA"), ("DewarpData", DEWARP)]
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / "authored.xmp"
            packet.write_text(authored_packet(fields))
            run = subprocess.run(["perl", exporter, "-json", "-n", "-G", str(packet)], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            exported = json.loads(run.stdout)[0]
            self.assertEqual(exported["ExifTool:ExifToolVersion"], 13.59)
            for name, expected in fields[1:]:
                self.assertEqual(str(exported["XMP:" + name]), expected)

    def test_quantity_and_existing_comparison_reach_common(self):
        module = self.module()
        sequence = [record(), record(**{"XMP:DewarpData": DEWARP.replace("2200.899902343750", "2201")}),
                    record(**{"XMP:DewarpData": DEWARP.replace("2200.899902343750", "2201.0").replace("10-24", "10-25")}),
                    record(**{"XMP:CameraSerialNumber": "OTHER"}), record(**{"EXIF:ImageWidth": 1296}),
                    record(**{"XMP:BandName": "Red", "XMP:Sensorindex": 2, "XMP:ImageSource": "MS_RED_CAMERA"})]
        source = json.dumps(sequence).encode()
        output, comparison, report = module.convert(source, capture_times_us=[1, 2, 2, 3, 4, 5], geometry_basis=BASIS)
        quantity, events = self.common(output, PROFILE), self.common(comparison, TRANSITIONS)
        values = [item["fields"] for item in quantity["observations"]]
        self.assertEqual(len(values), 6)
        self.assertEqual(values[0]["calibration_cx_pixel"], 10.609985351562)
        self.assertEqual(values[0]["calibration_cy_pixel"], -6.575988769531)
        self.assertEqual(values[1]["calibration_fx_pixel"], 2201)
        self.assertEqual(values[2]["calibration_date"], "2022-10-25")
        self.assertEqual(values[-1]["source_image_index"], 6)
        self.assertEqual(values[0]["source_sha256"], "sha256:" + hashlib.sha256(source).hexdigest())
        self.assertEqual(quantity["observations"][0]["clock_basis"], "Unknown")
        self.assertIsNone(quantity["observations"][0]["anchor_unix_us"])
        self.assertIn("pixel", quantity["profile_units"]["calibration_cx_pixel"])
        changes = [item["fields"] for item in events["observations"] if
                   item["fields"]["transition_disposition"] == "CHANGE_BETWEEN_TWO_REPORTED_VALUES"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["source_sha256"], "sha256:" + hashlib.sha256(output.encode()).hexdigest())
        self.assertEqual(report["comparison"]["reported_value_changes"], 1)
        self.assertIn("XMP:Uninterpreted", report["retained_tags"][0]["not_interpreted"])

    def test_missing_breaks_chain_and_important_refusals(self):
        module = self.module()
        blank = record(**{"XMP:DewarpData": None})
        zero = record(**{"XMP:DewarpData": DEWARP.replace("10.609985351562", "0")})
        output, comparison, report = module.convert(json.dumps([record(), blank, zero]).encode(),
                                                    capture_times_us=[1, 1, 2], geometry_basis=BASIS)
        values = self.common(output, PROFILE)["observations"]
        self.assertIsNone(values[1]["fields"]["calibration_cx_pixel"])
        self.assertEqual(values[2]["fields"]["calibration_cx_pixel"], 0)
        self.assertEqual(report["missing_calibration_records"], 1)
        self.assertEqual(report["comparison"]["reported_value_changes"], 0)
        self.assertEqual(sum(row["transition_disposition"] == "PREVIOUS_VALUE_NOT_REPORTED" for row in rows(comparison)), 4)
        missing_output, no_events, missing_report = module.convert(json.dumps([blank]).encode(),
                                                                  capture_times_us=[1], geometry_basis=BASIS)
        self.assertEqual(len(self.common(missing_output, PROFILE)["observations"]), 1)
        self.assertEqual(rows(no_events), [])
        self.assertEqual(missing_report["comparison"]["state_transitions"], 0)
        for changes in ({"XMP:DewarpData": ""}, {"XMP:DewarpData": DEWARP.replace("10-24", "02-30")},
                        {"XMP:DewarpData": DEWARP + ",0"}, {"XMP:DewarpData": DEWARP.replace("2200.899902343750", "1e-400")},
                        {"XMP:CameraSerialNumber": ""}, {"XMP:CameraSerialNumber": None}, {"XMP:DroneModel": "Other"},
                        {"EXIF:ImageWidth": True}, {"EXIF:ImageHeight": 0}, {"XMP:Version": 2},
                        {"XMP:Sensorindex": 1}, {"XMP:BandName": "Unknown"}, {"ExifTool:ExifToolVersion": 14}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                module.convert(json.dumps([record(**changes)]).encode(), capture_times_us=[1], geometry_basis=BASIS)
        for times, basis in (([], BASIS), ([True], BASIS), ([-1], BASIS), ([2**63], BASIS), ([1], "guessed")):
            with self.subTest(times=times), self.assertRaises(ValueError):
                module.convert(json.dumps([record()]).encode(), capture_times_us=times, geometry_basis=basis)
        with self.assertRaises(ValueError):
            module.convert(json.dumps([record(), record()]).encode(), capture_times_us=[2, 1], geometry_basis=BASIS)
        with self.assertRaises(ValueError):
            module.convert(b'[{"XMP:Version":1.6,"XMP:Version":1.6}]', capture_times_us=[1], geometry_basis=BASIS)
        for constant, bound in (("LIMIT", 10), ("MAX_IMAGES", 0)):
            with patch.object(module, constant, bound), self.assertRaises(ValueError):
                module.convert(json.dumps([record()]).encode(), capture_times_us=[1], geometry_basis=BASIS)

    def test_cli_thousand_reports_preserve_last_and_source_once(self):
        module = self.module()
        source = json.dumps([record(**{"XMP:DewarpData": DEWARP.replace("2200.899902343750", str(2200 + i))})
                             for i in range(1000)]).encode()
        output, comparison, report = module.convert(source, capture_times_us=list(range(1000)), geometry_basis=BASIS)
        self.assertEqual(len(self.common(output, PROFILE)["observations"]), 1000)
        self.assertEqual(len(self.common(comparison, TRANSITIONS)["observations"]), 1003)
        self.assertEqual(rows(output)[-1]["source_image_index"], "1000")
        self.assertEqual(report["comparison"]["reported_value_changes"], 999)
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "input.json"; original.write_bytes(json.dumps([record()]).encode())
            target = Path(directory) / "output"
            command = [os.sys.executable, str(ROOT / "scripts/convert_m3m_calibration.py"), str(original),
                       str(target), "--capture-time-us", "1", "--geometry-basis", BASIS]
            run = subprocess.run(command, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr.decode())
            self.assertEqual((target / "source.json").read_bytes(), original.read_bytes())
            self.assertEqual(set(p.name for p in target.iterdir()), {"source.json", "observations.csv", "transitions.csv", "report.json"})
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
