"""Authored native windows through the existing addressed diagnostic resolver."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.convert_candump_recorded import convert_path
from scripts.convert_dronecan_capture import convert_j1939
from tests.saved_can_window_helpers import native, common
from tests.test_j1939_rts import capture, transfer


class J1939Window(unittest.TestCase):
    def test_large_dm1_and_independent_dm2_common_report_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "capture.log"
            for source, destination, pgn, prefix_count in ((128, 33, 65226, 350000), (19, 70, 65227, 0)):
                frames = capture(transfer(source, destination, pgn)).split("\n", 1)[1]
                prefix = b"(1700000000.000000) can1 18feee80#0000000000000000\n" * prefix_count
                raw = prefix + native(frames).encode()
                if prefix_count:
                    self.assertGreater(len(raw), 16 * 1024 * 1024)
                path.write_bytes(raw)
                selected, provenance = convert_path(path, "can0", start_line=prefix_count,
                    stop_line=prefix_count + 6, extended=True,
                    expected_size=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest())
                window = provenance["source_window"]
                self.assertEqual(window["source_sha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
                self.assertEqual(window["excluded_physical_lines"], prefix_count)
                self.assertEqual([r["original_line"] for r in provenance["records"]], list(range(prefix_count + 1, prefix_count + 7)))
                self.assertEqual(provenance["records"][0]["original_byte_offset"], len(prefix))
                output, report = convert_j1939(selected, rts_pair=(source, destination))
                self.assertEqual((report["frames"], report["decoded_frames"], report["completed_transfers"]), (6, 6, 1))
                evidence = report["transfers"][0]
                frame_bytes = bytes.fromhex(evidence["source_frames_hex"][4:])
                self.assertEqual(json.loads(frame_bytes), list(csv.reader(io.StringIO(selected)))[1:])
                self.assertEqual(evidence["source_transfer_sha256"], "sha256:" + hashlib.sha256(frame_bytes).hexdigest())
                observations = common(self, folder, output)["observations"]
                self.assertEqual(len(observations), 2)
                fields = observations[0]["fields"]
                self.assertEqual(fields["spn_reported"], 74565)
                self.assertEqual(fields["j1939_destination_node_id"], destination)
                self.assertEqual(fields["diagnostic_report_kind"], "ACTIVE_DTC_REPORTED" if pgn == 65226 else "PREVIOUS_DTC_REPORTED")
                self.assertEqual(fields["source_transfer_sha256"], evidence["source_transfer_sha256"])
                self.assertEqual(observations[0]["clock_basis"], "Unknown")

    def test_selected_incomplete_exchange_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.log"
            path.write_text(native(capture(transfer()).split("\n", 1)[1]))
            for start, stop in ((0, 5), (1, 6)):
                selected, _ = convert_path(path, "can0", start_line=start, stop_line=stop, extended=True)
                with self.assertRaises(ValueError):
                    convert_j1939(selected, rts_pair=(128, 33))
