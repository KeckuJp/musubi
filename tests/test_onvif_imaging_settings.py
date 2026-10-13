"""Authored saved settings responses, not camera operation or effective-frame proof."""
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

from scripts import convert_opcua_recorded as converter

ROOT = Path(__file__).resolve().parents[1]
IMAGING = "http://www.onvif.org/ver20/imaging/wsdl"
TYPES = "http://www.onvif.org/ver10/schema"
SOAP = "http://www.w3.org/2003/05/soap-envelope"


def response(body, soap=False):
    native = (f'<t:GetImagingSettingsResponse xmlns:t="{IMAGING}" xmlns:s="{TYPES}" '
              'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:other="urn:other">'
              f'<t:ImagingSettings>{body}</t:ImagingSettings></t:GetImagingSettingsResponse>')
    return f'<env:Envelope xmlns:env="{SOAP}"><env:Body>{native}</env:Body></env:Envelope>' if soap else native


class ImagingSettingsTests(unittest.TestCase):
    def convert(self, source, token="declared-camera", time=1234567):
        text, report = converter.convert_onvif_imaging_settings(source, token, time)
        return list(csv.DictReader(io.StringIO(text))), report, text

    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("actual common reader is required")
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "input.csv"
            path.write_text(text)
            result = subprocess.run([reader, str(ROOT /
                "profiles/declared/opcua-recorded/profile.toml"), str(path)],
                capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual(common["main_rows"], 1)
        self.assertEqual(common["platform_domain"], "Unknown")
        observation = common["observations"][0]
        self.assertEqual(observation["clock_basis"], "Unknown")
        self.assertIsNone(observation["anchor_unix_us"])
        return observation["fields"]

    def test_reported_units_identity_unknown_content_and_cli_reach_common(self):
        source = response('<s:Exposure><s:Mode>MANUAL</s:Mode><s:ExposureTime>12500</s:ExposureTime>'
                          '<s:Gain>-3.5</s:Gain><s:Iris>0</s:Iris><s:MinExposureTime>7</s:MinExposureTime>'
                          '<other:future>kept</other:future></s:Exposure><s:Brightness>40</s:Brightness>')
        rows, report, text = self.convert(source)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["imaging_exposure_mode_reported"], "MANUAL")
        self.assertEqual(row["imaging_exposure_mode_disposition"], "SCHEMA_KNOWN_REPORTED")
        self.assertEqual(row["source_response_sha256"], "sha256:" + hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(bytes.fromhex(row["imaging_video_source_token_hex"][4:]), b"declared-camera")
        self.assertEqual(report["source_response"], source)
        self.assertEqual((report["interpreted_quantities"], report["uninterpreted_imaging_elements"],
                          report["uninterpreted_exposure_elements"]), (3, 1, 2))
        fields = self.common(text)
        self.assertEqual((fields["imaging_exposure_time_s"], fields["imaging_gain_db"],
                          fields["imaging_iris_attenuation_db"]), (0.0125, -3.5, 0))
        self.assertEqual(fields["imaging_settings_basis"], "REPORTED_CONFIGURATION_NOT_EFFECTIVE_FRAME_PARAMETERS")
        self.assertEqual(fields["imaging_device_range_basis"], "NOT_VALIDATED_AGAINST_DEVICE_OPTIONS")
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            (base / "response.xml").write_text(source)
            command = [sys.executable, str(ROOT / "scripts/convert_opcua_recorded.py"),
                       str(base / "response.xml"), str(base / "result.csv"), str(base / "result.json"),
                       "--source-format", "onvif-imaging-settings20-exposure", "--video-source-token",
                       "declared-camera", "--capture-time-us", "1234567"]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual((base / "result.csv").read_text(), text)
            self.assertNotEqual(subprocess.run(command + ["--profile-id", "not-imaging"],
                                               capture_output=True).returncode, 0)

    def test_absent_unknown_auto_and_zero_are_distinct_snapshots(self):
        for body, disposition, mode, count in (
                ("", "EXPOSURE_NOT_REPORTED", "NOT_PROVIDED", 0),
                ('<s:Exposure><s:Mode>AUTO</s:Mode><s:Gain>0</s:Gain></s:Exposure>',
                 "EXPOSURE_REPORTED", "SCHEMA_KNOWN_REPORTED", 1),
                ('<s:Exposure><s:Mode>FUTURE_MODE</s:Mode><s:ExposureTime>0</s:ExposureTime></s:Exposure>',
                 "EXPOSURE_REPORTED", "UNKNOWN_MODE_RETAINED", 1)):
            with self.subTest(body=body):
                rows, report, text = self.convert(response(body, soap=True))
                self.assertEqual(rows[0]["imaging_exposure_disposition"], disposition)
                self.assertEqual(rows[0]["imaging_exposure_mode_disposition"], mode)
                self.assertEqual(report["interpreted_quantities"], count)
                fields = self.common(text)
                if count == 0:
                    self.assertIn(fields.get("imaging_exposure_time_s"), (None, ""))

    def test_wrong_selected_shape_nonfinite_clock_and_xml_refuse(self):
        good = '<s:Exposure><s:Mode>MANUAL</s:Mode><s:ExposureTime>1</s:ExposureTime></s:Exposure>'
        bad = [good.replace('>1</', '>NaN</'), good.replace('>1</', '>1e999</'),
               good.replace('>1</', '>1e-320</'), good.replace('<s:ExposureTime>', '<s:ExposureTime xsi:nil="true">'),
               good.replace('<s:Mode>MANUAL</s:Mode>', ''),
               good.replace('<s:Mode>MANUAL</s:Mode>', '<s:Mode>MANUAL</s:Mode>' * 2),
               good.replace('s:ExposureTime', 'other:ExposureTime'),
               good.replace('s:Exposure>', 'other:Exposure>')]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.convert(response(body))
        for source in ('', '<wrong/>', '<!DOCTYPE x [<!ENTITY e "1">]>' + response(good),
                       response(good).replace('GetImagingSettingsResponse', 'SetImagingSettings')):
            with self.subTest(source=source[:30]), self.assertRaises(ValueError):
                self.convert(source)
        for token, time in (("", 1), ("camera", True), ("camera", -1), ("camera", 2**63)):
            with self.subTest(token=token, time=time), self.assertRaises(ValueError):
                self.convert(response(good), token, time)


if __name__ == "__main__":
    unittest.main()
