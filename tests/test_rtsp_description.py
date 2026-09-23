"""Authored saved RTSP/1.0 DESCRIBE responses bound to authored RTP captures; nothing real, nothing live.

Every byte here is written by this file: there is no real camera, no real recording, no request and no
credential. The URL-looking strings carry the literal marker `AUTHORED-NOT-A-REAL-SECRET` and the tests
require that they never leave the retained original.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "profiles/declared/rtsp-description-reuse/profile.toml"
SCRIPT = ROOT / "scripts/convert_rtsp_description.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FIXTURES = load(ROOT / "tests/test_rtp_records.py", "rtp_record_fixtures")
PORT = FIXTURES.PORT

AUTHORED_TOKEN = "AUTHORED-NOT-A-REAL-SECRET"
MEDIA_CONTROL = f"rtsp://192.0.2.10/stream/trackID=1?authtoken={AUTHORED_TOKEN}"
CONTENT_BASE = f"rtsp://192.0.2.10/stream?authtoken={AUTHORED_TOKEN}"

BODY = ["v=0",
        "o=- 2890844526 2890842807 IN IP4 192.0.2.10",
        "s=Authored offline description",
        "t=0 0",
        "a=control:*",
        "a=range:npt=0-",
        "m=video 0 RTP/AVP 96",
        "a=rtpmap:96 H264/90000",
        "a=fmtp:96 packetization-mode=1",
        f"a=control:{MEDIA_CONTROL}",
        "m=audio 0 RTP/AVP 98",
        "a=rtpmap:98 opus/48000/2",
        "a=control:trackID=2"]
HEADERS = ["CSeq: 2", "Content-Type: application/sdp", f"Content-Base: {CONTENT_BASE}",
           "Date: Sat, 20 Sep 2026 00:00:00 GMT"]


def described(body=None, headers=None, status="RTSP/1.0 200 OK", length_delta=0, trailing=b"",
              newline="\r\n", newline_body=None, declared_length=None):
    """One saved RTSP response message; the declared length is computed from the authored body."""
    lines = BODY if body is None else body
    body_bytes = "".join(line + (newline_body or newline) for line in lines).encode("utf-8")
    declared = len(body_bytes) + length_delta if declared_length is None else declared_length
    head = [status] + (HEADERS if headers is None else headers) + [f"Content-Length: {declared}"]
    return "".join(line + newline for line in head).encode("utf-8") + newline.encode() \
        + body_bytes + trailing


def capture(payload_type, ticks, *, ssrc=0x2A2B2C2D, first_sequence=1000):
    packets = [FIXTURES.rtp(first_sequence + index, tick, payload_type=payload_type, ssrc=ssrc)
               for index, tick in enumerate(ticks)]
    return FIXTURES.pcap([FIXTURES.datagram(packet) for packet in packets])


class RtspDescriptionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = load(SCRIPT, "convert_rtsp_description")

    def bind(self, description=None, capture_bytes=None, *, media_index=0, payload_type=96,
             port=PORT, **kwargs):
        rows, report = self.module.convert_saved_binding(
            described() if description is None else description,
            capture(96, [0, 90000]) if capture_bytes is None else capture_bytes, port,
            media_index=media_index, payload_type=payload_type,
            pairing_declared_by_caller=True, **kwargs)
        return list(csv.DictReader(io.StringIO(rows, newline=""))), report, rows


    def test_the_selected_video_declaration_times_the_adopted_rows(self):
        rows, report, text = self.bind()
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["sdp_encoding_name"], "H264")
        self.assertEqual(first["sdp_declared_clock_rate_hz"], "90000")
        self.assertEqual(first["sdp_encoding_parameters"], "")
        self.assertEqual(first["rtp_clock_rate_hz"], "90000")
        self.assertEqual(first["rtp_clock_rate_basis"], "DECLARED_BY_CALLER_NOT_OBSERVED")
        self.assertEqual(first["rtp_clock_declaration_basis"], self.module.CLOCK_BASIS)
        self.assertEqual(first["rtsp_pairing_basis"], self.module.PAIRING_BASIS)
        self.assertEqual(second["rtp_source_relative_us"], "1000000")  # 90000 ticks at 90 kHz
        self.assertEqual(first["rtsp_status_code"], "200")
        self.assertEqual(first["rtsp_version"], "RTSP/1.0")
        self.assertEqual(first["sdp_session_id"], "2890844526")
        self.assertEqual(first["sdp_session_version"], "2890842807")
        self.assertEqual(first["sdp_media_index"], "0")
        self.assertEqual(first["sdp_media_type"], "video")
        self.assertEqual(first["sdp_media_proto"], "RTP/AVP")
        self.assertEqual(first["sdp_media_format_list"], "96")
        self.assertEqual(first["sdp_selected_payload_type"], "96")
        self.assertEqual(first["rtp_payload_type"], "96")
        self.assertEqual(first["rtsp_source_sha256"], report["source_sha256"])
        self.assertEqual(first["sdp_media_port_declared"], "0")
        self.assertEqual(first["sdp_media_port_agreement"], self.module.PORT_ZERO)
        self.assertEqual(first["rtp_destination_port"], str(PORT))
        self.assertIn("NOT_OBSERVED_IN_PACKETS", first["rtp_clock_declaration_basis"])
        self.assertIn("NOT_UTC", report["time_basis"])
        self.assertNotIn("rtsp_session_id", text)

    def test_a_second_media_payload_and_clock_reuse_the_same_path(self):
        """Different section, media type, payload type, encoding and rate through the same code."""
        rows, report, _ = self.bind(capture_bytes=capture(98, [0, 90000]), media_index=1,
                                    payload_type=98)
        first, second = rows
        self.assertEqual(first["sdp_media_index"], "1")
        self.assertEqual(first["sdp_media_type"], "audio")
        self.assertEqual(first["sdp_selected_payload_type"], "98")
        self.assertEqual(first["sdp_encoding_name"], "opus")
        self.assertEqual(first["sdp_declared_clock_rate_hz"], "48000")
        self.assertEqual(first["sdp_encoding_parameters"], "2")
        self.assertEqual(first["rtp_clock_rate_hz"], "48000")
        self.assertEqual(second["rtp_source_relative_us"], "1875000")
        self.assertEqual([section["index"] for section in report["unselected_media_sections"]], [0])
        self.assertEqual(report["unselected_media_sections"][0]["media"], "video")
        self.assertEqual(report["unselected_media_sections"][0]["formats"], ["96"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the bound rows as Observations")
    def test_the_bound_rows_reach_the_common_reader_with_the_declaration(self):
        _, _, text = self.bind()
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(text, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows)],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 2)
        self.assertEqual(common["platform_domain"], "Unknown")
        fields = common["observations"][0]["fields"]
        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        self.assertEqual(fields["sdp_declared_clock_rate_hz"], 90000)
        self.assertEqual(fields["sdp_encoding_name"], "H264")
        self.assertEqual(fields["sdp_media_type"], "video")
        self.assertEqual(fields["rtp_clock_rate_hz"], 90000)
        self.assertEqual(common["observations"][1]["fields"]["rtp_source_relative_us"], 1000000)
        units = common["profile_units"]
        for column in ("rtsp_source_sha256", "rtsp_status_code", "rtsp_version", "rtsp_pairing_basis",
                       "sdp_session_id", "sdp_session_version", "sdp_media_index", "sdp_media_type",
                       "sdp_media_proto", "sdp_media_port_declared", "sdp_media_port_agreement",
                       "sdp_media_format_list", "sdp_selected_payload_type", "sdp_encoding_name",
                       "sdp_declared_clock_rate_hz", "sdp_encoding_parameters",
                       "sdp_rtpmap_source_offset", "sdp_rtpmap_line_sha256", "sdp_media_control",
                       "sdp_media_control_reference", "sdp_session_control",
                       "rtp_clock_declaration_basis"):
            self.assertTrue(units.get(column), f"{column} must declare its meaning to the reader")
        self.assertIn("caller_declaration", units["rtsp_pairing_basis"])
        self.assertIn("not_evidence_that_these_packets_used_that_payload_type_or_clock",
                      units["rtp_clock_declaration_basis"])
        self.assertIn("never_inferred_from_packets", units["sdp_declared_clock_rate_hz"])
        self.assertIn("never_echoed_or_fetched", units["sdp_media_control"])
        self.assertIn("only_SETUP_creates", units["sdp_session_id"])
        self.assertIn("no_RTCP_sender_report_anchor", units["rtp_source_relative_us"])


    def test_unselected_sections_unknown_attributes_and_headers_are_accounted_by_offset(self):
        description = described()
        _, report, text = self.bind(description)
        self.assertEqual(report["sdp_lines"], len(BODY))
        self.assertEqual(report["consumed_lines"] + report["retained_unselected_lines"],
                         report["sdp_lines"])
        retained = {description[entry["offset"]:entry["offset"] + entry["length"]].decode("utf-8")
                    for entry in report["retained_lines"]}
        self.assertEqual(retained, {"a=range:npt=0-", "a=fmtp:96 packetization-mode=1",
                                    "m=audio 0 RTP/AVP 98", "a=rtpmap:98 opus/48000/2",
                                    "a=control:trackID=2"})
        self.assertEqual(sorted(entry["attribute"] for entry in report["retained_lines"]),
                         ["", "control", "fmtp", "range"] + ["rtpmap"])
        self.assertEqual(len(report["headers"]), len(HEADERS) + 1)
        for entry in report["headers"]:
            line = description[entry["offset"]:entry["offset"] + entry["length"]].decode("utf-8")
            self.assertTrue(line.startswith(entry["name"] + ":"))
        selected = description[report["declared_binding"]["offset"]:][:len("a=rtpmap:96 H264/90000")]
        self.assertEqual(selected.decode("utf-8"), "a=rtpmap:96 H264/90000")

    def test_control_values_are_redacted_and_never_reprinted(self):
        _, report, text = self.bind()
        serialized = json.dumps(report)
        for carrier in (text, serialized):
            self.assertNotIn(AUTHORED_TOKEN, carrier)
            self.assertNotIn("authtoken", carrier)
            self.assertNotIn("trackID", carrier)
        self.assertEqual(report["media_control"], self.module.CONTROL_REDACTED)
        self.assertEqual(report["session_control"], self.module.CONTROL_AGGREGATE)
        self.assertIn("sha256:", report["media_control_reference"])
        self.assertIn("@", report["media_control_reference"])
        offset = int(report["media_control_reference"].split("@")[1].split("+")[0])
        length = int(report["media_control_reference"].split("+")[1])
        self.assertEqual(described()[offset:offset + length].decode("utf-8"),
                         f"a=control:{MEDIA_CONTROL}")
        empty = [line for line in BODY if not line.startswith("a=control")]
        _, without, _ = self.bind(described(empty))
        self.assertEqual(without["media_control"], self.module.CONTROL_ABSENT)
        self.assertEqual(without["session_control"], self.module.CONTROL_ABSENT)


    def test_the_envelope_and_the_declaration_are_refused_rather_than_guessed(self):
        def body_without(prefix):
            return [line for line in BODY if not line.startswith(prefix)]

        cases = [
            ("RTSP response status is not 200 OK", described(status="RTSP/1.0 404 Not Found"), {}),
            ("unsupported RTSP response version", described(status="RTSP/2.0 200 OK"), {}),
            ("malformed RTSP status line", described(status="RTSP/1.0 20 OK"), {}),
            ("RTSP response Content-Type is not application/sdp",
             described(headers=["Content-Type: application/xml"]), {}),
            ("RTSP response Content-Type carries an unsupported parameter",
             described(headers=["Content-Type: application/sdp; charset=utf-8"]), {}),
            ("RTSP response declares no Content-Type", described(headers=["CSeq: 2"]), {}),
            ("duplicate RTSP envelope header",
             described(headers=["Content-Type: application/sdp",
                                "Content-Type: application/sdp"]), {}),
            ("declared Content-Length does not match the saved description body",
             described(length_delta=-1), {}),
            ("declared Content-Length does not match the saved description body",
             described(trailing=b"leftover"), {}),
            ("saved RTSP response has no CRLF CRLF header boundary", described(newline="\n"), {}),
            ("saved RTSP response has no CRLF CRLF header boundary", b"RTSP/1.0 200 OK\r\n", {}),
            ("saved RTSP response uses a bare LF or CR line ending",
             b"RTSP/1.0 200 OK\r\nCSeq: 2\nContent-Type: application/sdp\r\n"
             b"Content-Length: 0\r\n\r\n", {}),
            ("session description line is not CRLF terminated",
             described(newline_body="\n"), {}),
            ("malformed session description", described(body_without("t=")), {}),
            ("malformed session description",
             described(["v=0", "s=out of order", "o=- 1 2 IN IP4 192.0.2.10", "t=0 0",
                        "m=video 0 RTP/AVP 96", "a=rtpmap:96 H264/90000"]), {}),
            ("malformed session description",  # a session-level line after the first media section
             described(BODY + ["t=0 0"]), {}),
            ("unknown SDP type letter", described(BODY + ["q=authored"]), {}),
            ("unsupported session description version",
             described(["v=1"] + BODY[1:]), {}),
            ("selected media section is absent", described(), dict(media_index=2)),
            ("unsupported media transport protocol",
             described([line.replace("RTP/AVP 96", "RTP/SAVP 96") for line in BODY]), {}),
            ("selected payload type is not in the media format list", described(),
             dict(payload_type=97)),
            ("no rtpmap declares the clock for the selected payload type",
             described(body_without("a=rtpmap:96")), {}),
            ("ambiguous rtpmap declarations for the selected payload type",
             described(BODY[:8] + ["a=rtpmap:96 H264/8000"] + BODY[8:]), {}),
            ("declared rtpmap clock rate must be a positive bounded integer",
             described([line.replace("H264/90000", "H264/0") for line in BODY]), {}),
            ("declared rtpmap clock rate must be a positive bounded integer",
             described([line.replace("H264/90000", "H264/ninety") for line in BODY]), {}),
            ("declared rtpmap clock rate must be a positive bounded integer",
             described([line.replace("H264/90000", "H264/2000000000") for line in BODY]), {}),
            ("malformed rtpmap declaration",
             described([line.replace("a=rtpmap:96 H264/90000", "a=rtpmap:96 H264") for line in BODY]),
             {}),
            ("rtpmap outside a media section",
             described(BODY[:6] + ["a=rtpmap:96 H264/90000"] + BODY[6:]), {}),
            ("ambiguous control scope declarations",
             described(BODY[:10] + ["a=control:trackID=9"] + BODY[10:]), {}),
        ]
        for message, description, kwargs in cases:
            with self.subTest(message=message, description=description[:40]):
                with self.assertRaises(ValueError) as caught:
                    self.bind(description, **kwargs)
                self.assertEqual(str(caught.exception), message)

    def test_the_pairing_must_be_declared_by_the_caller(self):
        with self.assertRaises(ValueError) as caught:
            self.module.convert_saved_binding(described(), capture(96, [0]), PORT, media_index=0,
                                              payload_type=96)
        self.assertEqual(str(caught.exception),
                         "the caller must declare the pairing of this description with this capture")
        self.assertIn("NOT_ESTABLISHED_BY_THE_DESCRIPTION", self.module.PAIRING_BASIS)

    def test_a_capture_without_the_declared_payload_type_is_counted_not_reinterpreted(self):
        with self.assertRaises(ValueError) as caught:
            self.bind(capture_bytes=capture(98, [0, 90000]))
        self.assertEqual(str(caught.exception), "no retained RTP packet observations")
        mixed = FIXTURES.pcap([FIXTURES.datagram(FIXTURES.rtp(1000, 0, payload_type=96)),
                               FIXTURES.datagram(FIXTURES.rtp(1001, 90000, payload_type=98))])
        rows, report, _ = self.bind(capture_bytes=mixed)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["rtp"]["unsupported_packets"], 1)
        self.assertEqual(report["rtp"]["unsupported_payload_types"], [98])


    def test_a_static_payload_type_declaration_is_checked_against_the_fixed_rate(self):
        """RFC 3551 §6 fixes a rate for some RTP/AVP types; a contradicting declaration is refused."""
        static = ["v=0", "o=- 5 6 IN IP4 192.0.2.10", "s=Authored static binding", "t=0 0",
                  "m=audio 0 RTP/AVP 0", "a=rtpmap:0 PCMU/8000"]
        rows, report, _ = self.bind(described(static), capture(0, [0, 8000]), payload_type=0)
        self.assertEqual(rows[0]["sdp_encoding_name"], "PCMU")
        self.assertEqual(rows[0]["sdp_declared_clock_rate_hz"], "8000")
        self.assertEqual(rows[0]["rtp_clock_rate_hz"], "8000")
        self.assertEqual(rows[0]["rtp_clock_rate_basis"], "DECLARED_BY_CALLER_NOT_OBSERVED")
        self.assertEqual(rows[0]["rtp_clock_declaration_basis"], self.module.CLOCK_BASIS)
        self.assertEqual(rows[1]["rtp_source_relative_us"], "1000000")  # 8000 ticks at 8 kHz
        self.assertEqual(report["declared_binding"]["clock_rate_hz"], 8000)

        conflicting = [line.replace("PCMU/8000", "PCMU/90000") for line in static]
        with self.assertRaises(ValueError) as caught:
            self.bind(described(conflicting), capture(0, [0, 8000]), payload_type=0)
        self.assertEqual(str(caught.exception),
                         "declared clock rate disagrees with the RFC3551 static rate")

    def test_a_dynamic_payload_type_is_not_held_to_another_types_static_rate(self):
        dynamic = ["v=0", "o=- 7 8 IN IP4 192.0.2.10", "s=Authored dynamic binding", "t=0 0",
                   "m=video 0 RTP/AVP 96", "a=rtpmap:96 H264/8000"]
        rows, _, _ = self.bind(described(dynamic), capture(96, [0, 8000]))
        self.assertEqual(rows[0]["rtp_clock_rate_hz"], "8000")
        self.assertEqual(rows[1]["rtp_source_relative_us"], "1000000")


    def test_the_cli_writes_both_retained_originals_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as work:
            description = Path(work) / "describe.rtsp"
            description.write_bytes(described())
            saved = Path(work) / "capture.pcap"
            saved.write_bytes(capture(96, [0, 90000]))
            target = Path(work) / "out"
            command = [sys.executable, str(SCRIPT), str(description), str(saved), str(target),
                       "--udp-destination-port", str(PORT), "--media-index", "0",
                       "--payload-type", "96"]
            unpaired = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(unpaired.returncode, 2)
            self.assertFalse(target.exists())
            done = subprocess.run(command + ["--pairing-declared-by-caller"], capture_output=True,
                                  text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(sorted(path.name for path in target.iterdir()),
                             ["accounting.json", "observations.csv", "source.pcap", "source.rtsp"])
            self.assertEqual((target / "source.rtsp").read_bytes(), described())
            self.assertEqual((target / "source.pcap").read_bytes(), saved.read_bytes())
            report = json.loads((target / "accounting.json").read_text(encoding="utf-8"))
            self.assertEqual(report["schema"], self.module.SCHEMA)
            self.assertEqual(report["declared_binding"]["clock_rate_hz"], 90000)
            self.assertEqual(report["rtp"]["retained_packets"], 2)
            self.assertNotIn(AUTHORED_TOKEN, (target / "accounting.json").read_text(encoding="utf-8"))
            again = subprocess.run(command + ["--pairing-declared-by-caller"], capture_output=True,
                                   text=True, check=False)
            self.assertEqual(again.returncode, 2)
            self.assertEqual((target / "source.rtsp").read_bytes(), described())

    def test_the_retained_originals_are_owner_only_under_a_permissive_umask(self):
        """The retained originals hold whatever was saved, credentials included: never world readable."""
        with tempfile.TemporaryDirectory() as work:
            description = Path(work) / "describe.rtsp"
            description.write_bytes(described())
            saved = Path(work) / "capture.pcap"
            saved.write_bytes(capture(96, [0, 90000]))
            target = Path(work) / "out"
            command = [sys.executable, str(SCRIPT), str(description), str(saved), str(target),
                       "--udp-destination-port", str(PORT), "--media-index", "0",
                       "--payload-type", "96", "--pairing-declared-by-caller"]
            done = subprocess.run(["/bin/sh", "-c", "umask 000; exec " + shlex.join(command)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            for name in self.module.RETAINED_OUTPUTS:
                written = target / name
                self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600, name)
            self.assertIn(AUTHORED_TOKEN.encode(), (target / "source.rtsp").read_bytes())
            self.assertEqual((target / "source.pcap").read_bytes(), saved.read_bytes())
            self.assertNotIn(AUTHORED_TOKEN, (target / "observations.csv").read_text(encoding="utf-8"))
            self.assertNotIn(AUTHORED_TOKEN, (target / "accounting.json").read_text(encoding="utf-8"))
            self.assertEqual((target / "source.rtsp").stat().st_size, len(described()))

    def test_an_existing_output_path_is_refused_and_nothing_there_is_touched(self):
        with tempfile.TemporaryDirectory() as work:
            description = Path(work) / "describe.rtsp"
            description.write_bytes(described())
            saved = Path(work) / "capture.pcap"
            saved.write_bytes(capture(96, [0, 90000]))
            target = Path(work) / "out"
            target.mkdir(mode=0o755)
            kept = target / "source.rtsp"
            kept.write_bytes(b"authored evidence that must survive")
            before = (kept.read_bytes(), stat.S_IMODE(kept.stat().st_mode),
                      stat.S_IMODE(target.stat().st_mode))
            done = subprocess.run([sys.executable, str(SCRIPT), str(description), str(saved),
                                   str(target), "--udp-destination-port", str(PORT),
                                   "--media-index", "0", "--payload-type", "96",
                                   "--pairing-declared-by-caller"], capture_output=True, text=True,
                                  check=False)
            self.assertEqual(done.returncode, 2)
            self.assertEqual((kept.read_bytes(), stat.S_IMODE(kept.stat().st_mode),
                              stat.S_IMODE(target.stat().st_mode)), before)
            self.assertEqual(sorted(path.name for path in target.iterdir()), ["source.rtsp"])
            self.assertNotIn(AUTHORED_TOKEN, done.stderr)
            self.assertNotIn(AUTHORED_TOKEN, done.stdout)


if __name__ == "__main__":
    unittest.main()
