"""Authored saved responses, never a device query or real recording proof."""
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
from unittest.mock import patch

from scripts import convert_onvif_recording_job as m

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/onvif-recording-job"


def response(sources="", state="PartiallyActive"):
    return ('<r:GetRecordingJobStateResponse xmlns:r="http://www.onvif.org/ver10/recording/wsdl" '
            'xmlns:t="http://www.onvif.org/ver10/schema"><r:State><t:RecordingToken>rec1</t:RecordingToken>'
            f'<t:State>{state}</t:State>{sources}<t:Future flag="raw">retained</t:Future>'
            '</r:State></r:GetRecordingJobStateResponse>')


def source(tracks="", state="Active"):
    return ('<t:Sources><t:SourceToken Type="urn:declared"><t:Token>same</t:Token></t:SourceToken>'
            f'<t:State>{state}</t:State><t:Tracks>{tracks}</t:Tracks></t:Sources>')


def track(state, error=""):
    return ('<t:Track><t:SourceTag>video</t:SourceTag><t:Destination>track1</t:Destination>'
            f'{error}<t:State>{state}</t:State></t:Track>')


def common(case, text):
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    if reader is None:
        case.skipTest("actual common reader not supplied")
    case.assertTrue(Path(reader).is_file())
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "observations.csv"
        path.write_text(text)
        run = subprocess.run([reader, str(CASE / "profile.toml"), str(path),
                              "--allow-equal-time"], capture_output=True)
        case.assertEqual(run.returncode, 0, run.stderr[:500])
        return json.loads(run.stdout)


class RecordingJobTests(unittest.TestCase):
    def test_track_states_error_absence_and_association_reach_common(self):
        xml = response(source(track("Active") + track("Error", "<t:Error/>")) +
                       source(track("FutureState", "<t:Error>opaque,detail</t:Error>"), "FutureAggregate"))
        wrapped = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>' +
                   xml + '</s:Body></s:Envelope>')
        text, report = m.convert(wrapped, "caller-job", 55)
        self.assertEqual(report["source_response"], wrapped)
        self.assertEqual(report["source_response_sha256"], "sha256:" + hashlib.sha256(wrapped.encode()).hexdigest())
        self.assertEqual((report["job_rows"], report["source_rows"], report["track_rows"]), (1, 2, 3))
        self.assertEqual(report["unknown_track_states"], 1)
        self.assertEqual(report["unselected_elements"], 1)
        self.assertEqual(report["unselected_attributes"], 1)
        data = common(self, text)
        self.assertEqual(data["main_rows"], 6)
        self.assertEqual(data["platform_domain"], "Unknown")
        observations = data["observations"]
        self.assertTrue(all(o["clock_basis"] == "Unknown" and o["anchor_unix_us"] is None
                            and o["t_boot_us"] is None for o in observations))
        rows = [o["fields"] for o in observations]
        self.assertEqual([r["recording_row_kind"] for r in rows], ["JOB", "SOURCE", "TRACK", "TRACK", "SOURCE", "TRACK"])
        self.assertEqual([rows[i]["recording_source_index"] for i in (2, 3, 5)], [0, 0, 1])
        self.assertEqual([rows[i]["recording_track_index"] for i in (2, 3, 5)], [0, 1, 0])
        self.assertEqual([rows[i]["recording_track_error_reported"] for i in (2, 3, 5)], [0, 1, None])
        self.assertEqual([rows[i]["recording_error_presence"] for i in (2, 3, 5)], ["NOT_REPORTED", "EMPTY", "REPORTED"])
        self.assertEqual(rows[3]["recording_error_hex"], "hex:")
        self.assertEqual(rows[5]["recording_error_hex"], "hex:" + b"opaque,detail".hex())
        self.assertEqual(rows[5]["recording_state_disposition"], "UNKNOWN_STATE_RETAINED")
        self.assertTrue(all(r["source_response_sha256"] == report["source_response_sha256"] for r in rows))
        self.assertTrue(all(r["recording_job_token_hex"] == "hex:" + b"caller-job".hex() for r in rows))

    def test_empty_collections_are_truthful_summaries_and_cli_is_exclusive(self):
        for xml, count in ((response(state="Idle"), 1), (response(source(), "Active"), 2)):
            text, report = m.convert(xml, "another-job", 0)
            self.assertEqual(report["track_rows"], 0)
            self.assertEqual(common(self, text)["main_rows"], count)
            self.assertTrue(all(r["recording_track_error_reported"] == "" for r in csv.DictReader(io.StringIO(text))))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved, output, report = (root / name for name in ("source.xml", "out.csv", "report.json"))
            saved.write_text(response(source(track("Idle"))))
            args = [sys.executable, str(ROOT / "scripts/convert_onvif_recording_job.py"), str(saved), str(output),
                    str(report), "--job-token", "caller", "--capture-time-us", "0"]
            run = subprocess.run(args, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            before = output.read_bytes(), report.read_bytes()
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
            self.assertEqual((output.read_bytes(), report.read_bytes()), before)

    def test_important_shape_and_resource_refusals(self):
        xml = response(source(track("Error")))
        invalid = ["", "<!DOCTYPE a>" + xml, xml.replace("<t:State>Error", "<t:State><t:n/>Error"),
                   xml.replace("<t:State>Error</t:State>", "<t:State>Error</t:State><t:State>Idle</t:State>"),
                   xml.replace("GetRecordingJobStateResponse", "SetRecordingJobModeResponse"),
                   xml.replace("<t:Destination>", "<Destination xmlns='urn:wrong'>").replace("</t:Destination>", "</Destination>"),
                   xml.replace("<t:State>Error</t:State>", '<t:State xmlns:x="http://www.w3.org/2001/XMLSchema-instance" x:nil="true"/>'),
                   xml.replace("<t:SourceTag>video</t:SourceTag>", ""), xml + "\x00"]
        for value in invalid:
            with self.subTest(value=value[:60]), self.assertRaises(ValueError):
                m.convert(value, "job", 0)
        for time in (True, -1, 2**63, 1.5):
            with self.subTest(time=time), self.assertRaises(ValueError):
                m.convert(xml, "job", time)
        for name, bound in (("MAX_XML_BYTES", 8), ("MAX_ELEMENTS", 1), ("MAX_SOURCES", 0),
                            ("MAX_TRACKS", 0), ("MAX_CSV_BYTES", 8), ("MAX_REPORT_BYTES", 8)):
            with self.subTest(bound=name), patch.object(m, name, bound), self.assertRaisesRegex(ValueError, "bound"):
                m.convert(xml, "job", 0)


if __name__ == "__main__":
    unittest.main()
