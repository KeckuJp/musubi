"""Authored native frames only; configuration READ, no send or device execution."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_request_response_match import convert, MATCHED, MISMATCHED, MISSING, AMBIGUOUS

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/tlog-param-comparison/profile.toml"
COMMON = ROOT / "profiles/declared/request-response-match/profile.toml"
BASE = dict(request_where="msg=PARAM_REQUEST_READ", response_where="msg=PARAM_VALUE",
            correlate="param_id=param_id", expectations=("expected_param_type=param_type",),
            responder=("target_sys=src_sys", "target_comp=src_comp"), index_column="param_index",
            session_column="session", window_us=100, clock_qualified=True)


def packet(message, payload, *, v2=False, system=1, component=1, trim=False):
    if trim:
        payload = payload.rstrip(b"\0") or b"\0"
    head = (bytes([0xfd, len(payload), 0, 0, 4, system, component, message, 0, 0]) if v2
            else bytes([0xfe, len(payload), 4, system, component, message]))
    data = head + payload
    crc = 0xffff
    for byte in data[1:] + bytes([{20: 214, 22: 220}.get(message, 0)]):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return data + struct.pack("<H", crc)


def req(key=b"A", index=-1, target=1, **kw):
    return packet(20, struct.pack("<hBB16s", index, target, 1, key),
                  system=kw.pop("system", 255), component=190, **kw)


def res(key=b"A", kind=9, value=b"\x00\x00\xc0\x7f", **kw):
    return packet(22, value + struct.pack("<HH16sB", 12, 0, key, kind), **kw)


def saved(*pairs):
    return b"".join(struct.pack(">Q", stamp) + frame for stamp, frame in pairs)


class TlogParameterComparison(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = os.environ.get("MUSUBI_TLOG_PARAM_EXPORTER")
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.exporter or not cls.reader:
            raise unittest.SkipTest("actual native exporter and common reader required")

    def export(self, raw, *, profile=None, expected="9", session="declared-A"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p.toml").write_text(profile or PROFILE.read_text())
            (root / "source.tlog").write_bytes(raw)
            return subprocess.run([self.exporter, str(root / "p.toml"), str(root / "source.tlog"),
                                   "--requester", "255:190", "--session", session,
                                   "--expected-type", expected], capture_output=True)

    def compare(self, exported, **options):
        text, report = convert(exported, **dict(BASE, **options))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comparison.csv"
            path.write_text(text)
            result = subprocess.run([self.reader, str(COMMON), str(path), "--allow-equal-time"],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            common = json.loads(result.stdout)
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertTrue(all(row["clock_basis"] == "Unknown" for row in common["observations"]))
        compared = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual([row["fields"]["match_outcome"] for row in common["observations"]],
                         [row["match_outcome"] for row in compared])
        self.assertEqual([row["fields"]["source_sha256"] for row in common["observations"]],
                         [row["source_sha256"] for row in compared])
        return compared, report, common

    def test_two_native_inputs_reach_existing_comparison_and_common(self):
        for v2, kind, outcome in ((False, 9, MATCHED), (True, 6, MISMATCHED)):
            with self.subTest(v2=v2):
                stamp = 2**53 + 1
                wires = [(stamp, req(b"ABCDEFGHIJKLMNOP", v2=v2)),
                         (stamp + 1, req(b"OTHER", v2=v2, system=254)),
                         (stamp + 2, packet(200, b"opaque", v2=v2)),
                         (stamp + 20, res(b"ABCDEFGHIJKLMNOP", kind=kind, v2=v2))]
                raw = saved(*wires)
                result = self.export(raw)
                self.assertEqual(result.returncode, 0, result.stderr)
                records = list(csv.DictReader(io.StringIO(result.stdout.decode())))
                self.assertEqual(len(records), 4)
                self.assertEqual([r["source_record_index"] for r in records], ["0", "1", "2", "3"])
                self.assertEqual(records[0]["record_time_us"], str(stamp))
                self.assertEqual(records[3]["param_value_raw_hex"], "hex:0000c07f")
                self.assertEqual(records[0]["param_index"], "-1")
                for record, (_, wire) in zip(records, wires):
                    self.assertEqual(record["source_raw_frame_hex"], wire.hex())
                    self.assertEqual(record["source_input_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.assertEqual([record[k] for k in ("source_total_records", "source_request_records",
                                     "source_response_records", "source_other_records")], ["4", "1", "1", "2"])
                compared, report, common = self.compare(result.stdout)
                self.assertEqual(compared[0]["match_outcome"], outcome)
                self.assertEqual(compared[0]["response_row_index"], "3")
                self.assertEqual(compared[0]["source_sha256"], "sha256:" + hashlib.sha256(result.stdout).hexdigest())
                self.assertEqual(common["main_rows"], 1)
                self.assertIn("param_type", compared[0]["expectation_pairs_hex"] and bytes.fromhex(
                    compared[0]["expectation_pairs_hex"][4:]).decode())

    def test_missing_multiple_equal_time_and_unqualified_bases_do_not_match(self):
        cases = [([(1, req()), (2, res(b"B"))], MISSING),
                 ([(1, req()), (2, res()), (3, res())], AMBIGUOUS),
                 ([(1, req()), (1, res())], MISSING),
                 ([(1, req(index=0)), (2, res())], AMBIGUOUS),
                 ([(1, req(target=0)), (2, res())], AMBIGUOUS),
                 ([(1, req(b"")), (2, res(b""))], AMBIGUOUS)]
        for pairs, expected in cases:
            with self.subTest(expected=expected, pairs=pairs):
                result = self.export(saved(*pairs))
                self.assertEqual(result.returncode, 0, result.stderr)
                compared, _, _ = self.compare(result.stdout)
                self.assertEqual(compared[0]["match_outcome"], expected)
        result = self.export(saved((1, req(v2=True, trim=True)), (2, res(kind=0, v2=True, trim=True))))
        self.assertEqual(result.returncode, 0, result.stderr)
        compared, _, _ = self.compare(result.stdout, clock_qualified=False)
        self.assertEqual(compared[0]["match_outcome"], AMBIGUOUS)
        self.assertIn("CLOCK_BASIS_NOT_QUALIFIED", compared[0]["ambiguity_reasons"])

    def test_corrupt_malformed_profile_and_declaration_refuse_without_partial_stdout(self):
        normal = saved((1, req()), (2, res()))
        bad_crc = bytearray(normal)
        bad_crc[-1] ^= 1
        bad = [bytes(bad_crc), normal[:-1], saved((1, req(b"\xff"))),
               saved((1, packet(20, b"\0" * 21, v2=True))),
               saved((1, packet(22, b"\0" * 26, v2=True))),
               saved((1, packet(20, b"\0" * 19))), saved((1, res()))]
        for raw in bad:
            with self.subTest(raw=raw):
                result = self.export(raw)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
        for options in ({"profile": PROFILE.read_text() + "\n[selected_sender]\nsystem_id=1\ncomponent_id=1\n"},
                        {"profile": PROFILE.read_text().replace('default_clock_basis = "unknown"',
                                                                'default_clock_basis = "host_received"')},
                        {"expected": "256"}, {"session": "A,B"}, {"session": ""}):
            result = self.export(normal, **options)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
