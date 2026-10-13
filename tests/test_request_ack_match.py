"""Authored saved information requests only: no command issuance or execution."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_request_response_match as target
from tests import test_request_response_match as legacy

ROOT = Path(__file__).resolve().parents[1]
BASE = dict(mode=target.ACK_MODE, protocol_commit=target.PROTOCOL_PIN,
            request_where="message_name=COMMAND_LONG", response_where="message_name=COMMAND_ACK",
            correlate="command=command", expectations=("expected_result=result",),
            responder=("target_system=source_system", "target_component=source_component",
                       "source_system=target_system", "source_component=target_component"),
            session_column="session", window_us=500, clock_qualified=True)
FIELDS = ["record_time_us", "message_name", "command", "source_system", "source_component",
          "target_system", "target_component", "session", "confirmation", "expected_result",
          "result", "progress", "result_param2"] + ["param"+str(n) for n in range(1,8)]


def request(at=1000, session="A", **changes):
    item = dict.fromkeys(FIELDS, "")
    item.update(record_time_us=at, message_name="COMMAND_LONG", command=512, source_system=255,
                source_component=190, target_system=1, target_component=1, session=session,
                confirmation=0, expected_result=0, **{f"param{n}": "0.0" for n in range(1,8)})
    item["param1"] = "148.0"
    item.update(changes)
    return item


def response(at=1100, session="A", **changes):
    item = dict.fromkeys(FIELDS, "")
    item.update(record_time_us=at, message_name="COMMAND_ACK", command=512, source_system=1,
                source_component=1, target_system=255, target_component=190, session=session,
                result=0, progress=255, result_param2=0)
    item.update(changes)
    return item


def saved(*rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


class SavedAck(unittest.TestCase):
    def test_two_sessions_reported_result_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "required CI stage supplies actual common reader")
        for session, result, label, outcome in (("A", 0, "ACCEPTED", target.MATCHED),
                                                ("B", 5, "IN_PROGRESS", target.MISMATCHED)):
            data = saved(request(session=session), response(session=session, result=result, progress=50))
            text, report = target.convert(data, **BASE)
            row = next(csv.DictReader(io.StringIO(text)))
            self.assertEqual(row["match_outcome"], outcome)
            self.assertEqual(row["reported_ack_result"], "REPORTED_" + label)
            self.assertEqual(row["source_sha256"], "sha256:"+hashlib.sha256(data).hexdigest())
            self.assertEqual((report["source_rows"], report["selected_requests"], report["unselected_rows"]), (2, 1, 0))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/"observations.csv"
                path.write_text(text)
                run = subprocess.run([reader, str(ROOT/"profiles/declared/request-response-match/profile.toml"), str(path)], capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr.decode())
                obs = json.loads(run.stdout)["observations"][0]
                self.assertEqual(obs["fields"]["reported_ack_result"], "REPORTED_"+label)
                self.assertEqual(obs["clock_basis"], "Unknown")
                self.assertIn("NOT_EXECUTION", obs["fields"]["saved_ack_basis"])

    def test_ambiguity_unknown_and_refusals(self):
        cases = [saved(request(), response(target_system="")), saved(request(), response(target_component=0)),
                 saved(request(confirmation=1), response()), saved(request(), request(1050), response()),
                 saved(request(), response(), response(1200))]
        for data in cases:
            text, report = target.convert(data, **BASE)
            self.assertGreater(report["ambiguous"], 0)
            self.assertEqual(report["matched"], 0)
            self.assertTrue(all(row["reported_ack_result"] == "" for row in csv.DictReader(io.StringIO(text))))
        for data, options, expected in [
            (saved(request(), response(target_system=254)), {}, target.MISSING),
            (saved(request(), response()), {"clock_qualified": False}, target.AMBIGUOUS),
            (saved(request(), response(result="")), {}, target.UNKNOWN),
            (saved(request(), response(result=250)), {}, target.MISMATCHED),
        ]:
            text, _ = target.convert(data, **dict(BASE, **options))
            row = next(csv.DictReader(io.StringIO(text)))
            self.assertEqual(row["match_outcome"], expected)
            if "250" in str(data):
                self.assertEqual(row["reported_ack_result"], "REPORTED_UNKNOWN_ENUM_250")
        for data in (saved(request(param1=1), response()), saved(request(command=400), response()),
                     saved(request(), response(result=5, progress=101)), saved(request(), response(result=256))):
            with self.assertRaises(ValueError):
                target.convert(data, **BASE)
        with self.assertRaises(ValueError):
            target.convert(saved(request(), response()), **dict(BASE, protocol_commit="unqualified"))

    def test_progress_percentage_only_for_in_progress(self):
        for result in (0, 250):
            text, _ = target.convert(saved(request(), response(result=result, progress=254)), **BASE)
            row = next(csv.DictReader(io.StringIO(text)))
            self.assertEqual(row["reported_ack_progress"], "254")
        with self.assertRaises(ValueError):
            target.convert(saved(request(), response(result=5, progress=254)), **BASE)
        with self.assertRaises(ValueError):
            target.convert(saved(request(), response(result=0, progress=256)), **BASE)

    def test_legacy_exact_parity(self):
        text, report = target.convert(legacy.saved(legacy.request(1000), legacy.response(1200)), **legacy.BASE)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), "cfbbfdb3f200637f0c9e12f8a6dac216c832f9c116abee2cf7e26692fb96c02b")
        self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(), "0ce9054cf3225ab465bb92f081b72c69cc15c84ce5d6d0c1410a992f7459b01b")

    def test_nonterminal_or_unknown_ack_cannot_clear_pending_request(self):
        for result in (5, 250, ""):
            text, _ = target.convert(saved(request(), response(1050, result=result),
                                           request(1200), response(1250)), **dict(BASE, window_us=100))
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(rows[1]["match_outcome"], target.AMBIGUOUS)
            self.assertIn(target.OUTSTANDING, rows[1]["ambiguity_reasons"])


if __name__ == "__main__":
    unittest.main()
