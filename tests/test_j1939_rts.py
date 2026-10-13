"""Authored complete saved handshakes, never a CAN sender or vehicle proof."""
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
from scripts.convert_dronecan_capture import convert_j1939

ROOT = Path(__file__).resolve().parents[1]
HEADER = "time_us,extended_id,data_hex\n"


def transfer(source=128, destination=33, pgn=65226, payload=bytes.fromhex("1be44523230702000003")):
    count = (len(payload) + 6) // 7
    target = pgn.to_bytes(3, "little")
    cm = 0x1cec0000 | destination << 8 | source
    reply = 0x1cec0000 | source << 8 | destination
    dt = 0x1ceb0000 | destination << 8 | source
    rows = [[10, cm, b"\x10" + len(payload).to_bytes(2, "little") + bytes([count, 1]) + target]]
    for index in range(count):
        rows.append([11 + index * 2, reply, bytes([17, 1, index + 1, 255, 255]) + target])
        rows.append([12 + index * 2, dt, bytes([index + 1]) + payload[index*7:index*7+7].ljust(7, b"\xff")])
    rows.append([12 + count * 2, reply, b"\x13" + len(payload).to_bytes(2, "little") + bytes([count, 255]) + target])
    return rows


def granted(blocks, maximum=255, source=128, destination=33, pgn=65226,
            payload=bytes.fromhex("1be445232307020000030400000505000006")):
    """One saved RTS, then CTS(count, next) followed by exactly that many DT, then ACK."""
    count = (len(payload) + 6) // 7
    target = pgn.to_bytes(3, "little")
    cm = 0x1cec0000 | destination << 8 | source
    reply = 0x1cec0000 | source << 8 | destination
    dt = 0x1ceb0000 | destination << 8 | source
    rows = [[20, cm, b"\x10" + len(payload).to_bytes(2, "little") + bytes([count, maximum]) + target]]
    following = 1
    for block in blocks:
        rows.append([rows[-1][0] + 1, reply, bytes([17, block, following, 255, 255]) + target])
        for number in range(following, following + block):
            rows.append([rows[-1][0] + 1, dt, bytes([number]) + payload[number*7-7:number*7].ljust(7, b"\xff")])
        following += block
    rows.append([rows[-1][0] + 1, reply, b"\x13" + len(payload).to_bytes(2, "little") + bytes([count, 255]) + target])
    return rows


def capture(rows):
    return HEADER + "".join(f"{time},{identifier:08x},{payload.hex()}\n" for time, identifier, payload in rows)


class SavedRTS(unittest.TestCase):
    def test_two_reports_all_frames_clocks_and_common(self):
        self.assertIn("MUSUBI_TELEMETRY_READER", os.environ)
        for source, destination, pgn in ((128, 33, 65226), (19, 70, 65227)):
            raw = transfer(source, destination, pgn)
            for row in raw: row[0] = 10
            other = [10, 0x18feee80, bytes(8)]
            text = capture([other] + raw)
            out, report = convert_j1939(text, rts_pair=(source, destination))
            rows = list(csv.DictReader(io.StringIO(out)))
            self.assertEqual((report["frames"], report["decoded_frames"], len(report["unsupported_frames"])), (7, 6, 1))
            self.assertEqual(report["completed_transfers"], 1)
            self.assertEqual([r["spn_reported"] for r in rows], ["74565", "2"])
            self.assertEqual(rows[0]["diagnostic_report_kind"], "ACTIVE_DTC_REPORTED" if pgn == 65226 else "PREVIOUS_DTC_REPORTED")
            original = list(csv.reader(io.StringIO(capture(raw))))[1:]
            evidence = report["transfers"][0]
            original_bytes = bytes.fromhex(evidence["source_frames_hex"][4:])
            self.assertEqual(json.loads(original_bytes), original)
            self.assertEqual(rows[0]["source_transfer_sha256"], "sha256:" + hashlib.sha256(original_bytes).hexdigest())
            self.assertTrue(all(r["source_frames_hex"] == "" and r["source_transfer_index"] == "0"
                                and r["source_transfer_sha256"] == evidence["source_transfer_sha256"] for r in rows))
            self.assertEqual(rows[0]["j1939_destination_node_id"], str(destination))
            self.assertEqual(rows[0]["transport_packet_count"], "2")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(out)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"), str(path),
                    "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertIn("not_physical", common["profile_units"]["transport_basis"])
            for row in common["observations"]:
                self.assertEqual(row["clock_basis"], "Unknown")
                self.assertIsNone(row["anchor_unix_us"])
                self.assertEqual([row["fields"][key] for key in ("record_time_us", "first_frame_time_us", "payload_completion_time_us")], [10, 10, 10])
                self.assertEqual(row["fields"]["source_node_id"], source)

    def test_missing_duplicate_order_peer_abort_shape_and_boundary(self):
        good = transfer()
        bad = [good[:-1], good[1:], good[:1] + good[2:], good[:2] + good[1:], good[:3] + good[2:],
               good[:1] + good[:1] + good[1:], good[:1] + [good[3], good[2]] + good[3:]]
        for index, offset, value in ((0, 3, 3), (0, 4, 0), (1, 1, 0), (1, 1, 2), (1, 2, 2),
                                     (1, 3, 0), (5, 1, 11), (5, 3, 3), (5, 4, 0), (5, 5, 0), (4, 7, 0)):
            rows = [[t, i, p] for t, i, p in good]
            payload = bytearray(rows[index][2]); payload[offset] = value; rows[index][2] = bytes(payload)
            bad.append(rows)
        rows = [[t, i, p] for t, i, p in good]; rows[1][1] ^= 1; bad.append(rows)
        rows = [[t, i, p] for t, i, p in good]; rows[2][1] ^= 1 << 26; bad.append(rows)
        rows = [[t, i, p] for t, i, p in good]; rows[1][2] = b"\xff" + rows[1][2][1:]; bad.append(rows)
        for rows in bad:
            # Protocol errors must be refused independently of decreasing capture clocks.
            rows = [[10, identifier, payload] for _, identifier, payload in rows]
            with self.subTest(rows=rows), self.assertRaises(ValueError): convert_j1939(capture(rows), rts_pair=(128, 33))
        for pair in ((), (128,), (128,128), (True,33), (128,255), [128,33]):
            with self.assertRaises(ValueError): convert_j1939(capture(good), rts_pair=pair)
        with self.assertRaises(ValueError): convert_j1939(capture(good), bam=True, rts_pair=(128,33))
        with self.assertRaises(ValueError): convert_j1939(capture(good), rts_pair=(1,2))
        payload = bytes(2) + bytes.fromhex("01000205") * 445
        output, report = convert_j1939(capture(transfer(payload=payload)), rts_pair=(128,33))
        self.assertEqual((report["decoded_frames"], report["observations"]), (512,445))
        self.assertLess(len(output.encode()), 1024 * 1024)
        self.assertEqual(len(json.loads(bytes.fromhex(report["transfers"][0]["source_frames_hex"][4:]))), 512)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maximum.csv"; path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"), str(path),
                "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"],445)
        self.assertTrue(all(r["fields"]["source_transfer_sha256"] == report["transfers"][0]["source_transfer_sha256"] for r in common["observations"]))
        with patch("scripts.convert_dronecan_capture.MAX_J1939_REPORT", 100), self.assertRaisesRegex(ValueError,"report exceeds"):
            convert_j1939(capture(good), rts_pair=(128,33))

    def test_method_zero_and_completion_clock_cli(self):
        rows = transfer(payload=bytes.fromhex("00000000000001000285"))
        out, report = convert_j1939(capture(rows), rts_pair=(128,33))
        row = next(csv.DictReader(io.StringIO(out)))
        self.assertEqual((report["zero_spn_reports"], len(report["unsupported_dtcs"])), (1,1))
        self.assertEqual(row["diagnostic_report_kind"], "ZERO_SPN_REPORTED_NOT_PHYSICAL_HEALTH")
        self.assertEqual([row[k] for k in ("first_frame_time_us", "payload_completion_time_us", "record_time_us")], ["10","14","16"])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "in.csv"; source.write_text(capture(rows))
            command = [sys.executable, str(ROOT / "scripts/convert_dronecan_capture.py"), str(source)]
            subprocess.run(command + [str(base / "good"), "--protocol", "j1939-dm-rts", "--rts-source", "128", "--rts-destination", "33"], check=True, capture_output=True)
            self.assertEqual((base / "good/observations.csv").read_text(), out)
            for args in (["--protocol", "j1939-dm-rts"], ["--protocol", "j1939-dm-single", "--rts-source", "128"]):
                self.assertNotEqual(subprocess.run(command + [str(base / "bad")] + args, capture_output=True).returncode, 0)
                self.assertFalse((base / "bad").exists())

    def test_serial_transfers_keep_distinct_indices_with_equal_capture_times(self):
        first, second = transfer(), transfer(pgn=65227)
        for row in first + second: row[0] = 10
        output, report = convert_j1939(capture(first + second), rts_pair=(128,33))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["source_transfer_index"] for r in rows], ["0", "0", "1", "1"])
        self.assertEqual((report["frames"], report["decoded_frames"], report["completed_transfers"]), (12,12,2))
        for index, evidence in enumerate(report["transfers"]):
            self.assertEqual(evidence["source_transfer_index"], index)
            self.assertTrue(all(r["source_transfer_sha256"] == evidence["source_transfer_sha256"] for r in rows[index*2:index*2+2]))
        self.assertNotEqual(report["transfers"][0]["source_transfer_sha256"], report["transfers"][1]["source_transfer_sha256"])

    def test_cts_grants_follow_the_pinned_sender_and_reach_common(self):
        old = transfer()
        for blocks, maximum in (([2, 1], 255), ([1, 1, 1], 2), ([3], 3)):
            rows = granted(blocks, maximum)
            output, report = convert_j1939(capture(old + rows), rts_pair=(128, 33))
            csv_rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["frames"], report["decoded_frames"], report["completed_transfers"]),
                             (6 + len(rows), 6 + len(rows), 2))
            self.assertEqual(report["transport_basis"], "SAVED_RTS_CTS_GRANTS_ACK_NOT_PHYSICAL_DELIVERY")
            first, second = report["transfers"]
            self.assertEqual((first["transport_basis"], first["rts_max_packets"], first["cts_grants_next_count"]),
                             ("SAVED_RTS_CTS1_ACK_NOT_PHYSICAL_DELIVERY", 1, [[1, 1], [2, 1]]))
            starts = [sum(blocks[:i]) + 1 for i in range(len(blocks))]
            self.assertEqual((second["rts_max_packets"], second["cts_grants_next_count"]),
                             (maximum, [list(pair) for pair in zip(starts, blocks)]))
            self.assertEqual(json.loads(bytes.fromhex(second["source_frames_hex"][4:])),
                             list(csv.reader(io.StringIO(capture(rows))))[1:])
            self.assertEqual([r["spn_reported"] for r in csv_rows], ["74565", "2", "74565", "2", "4", "5"])
            self.assertEqual([r["transport_basis"] for r in csv_rows],
                             ["SAVED_RTS_CTS1_ACK_NOT_PHYSICAL_DELIVERY"] * 2
                             + ["SAVED_RTS_CTS_GRANTS_ACK_NOT_PHYSICAL_DELIVERY"] * 4)
            self.assertEqual({(r["transport_packet_count"], r["payload_completion_time_us"])
                              for r in csv_rows[2:]}, {("3", str(rows[-2][0]))})
            self.assertEqual(second["payload_completion_time_us"], rows[-2][0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grants.csv"; path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"), str(path),
                "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 6)
        self.assertIn("CTS_grants", common["profile_units"]["transport_basis"])
        self.assertTrue(all(row["clock_basis"] == "Unknown" for row in common["observations"]))

    def test_unqualified_grants_fail_instead_of_repair(self):
        good = granted([2, 1], 2)
        bad = [granted([3], 2), granted([0, 2, 1]), granted([2, 2]), granted([1, 2], 1),
               good[:3] + good[4:], good[:4] + good[3:], good[:3] + good[1:2] + good[3:],
               good[:-1], good[:4] + [good[1]] + good[4:]]
        rows = [list(row) for row in good]; rows[4][2] = bytes([17, 1, 1, 255, 255]) + rows[4][2][5:]; bad.append(rows)
        rows = [list(row) for row in good]; rows[2][1] = 0x1ceb0000 | 128 << 8 | 33; bad.append(rows)
        rows = [list(row) for row in good]; rows[4][2] = b"\xff" + rows[4][2][1:]; bad.append(rows)
        for rows in bad:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                convert_j1939(capture(rows), rts_pair=(128, 33))

    def test_old_single_and_bam_bytes_unchanged(self):
        cases = [(HEADER + "1,18feca80,1be445232307ffff\n", False,
                  "7e5e67515901ff70930e4b52975bc96b666a7847904f1eff7f160e54f6368526", "6a79d43caa4afc98068d48b26e6c1be795d944addbff76bf96432599137bdc41"),
                 (HEADER + "10,1cecff80,200a0002ffcafe00\n11,1cebff80,011be44523230702\n12,1cebff80,02000003ffffffff\n", True,
                  "75ddecb03290dd046f6be3d7069fc1766ee92e911a209a24db8b9a337a2d0275", "06242d93edfef0cbc29d235c767fcf5a321ee10a550e384a752b646cb4df3341")]
        for text, bam, output_hash, report_hash in cases:
            out, report = convert_j1939(text,bam=bam)
            self.assertEqual(hashlib.sha256(out.encode()).hexdigest(), output_hash)
            self.assertEqual(hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest(), report_hash)
