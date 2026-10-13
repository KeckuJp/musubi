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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from select_blackbox_csv_window import convert_path


class BlackboxWindowTests(unittest.TestCase):
    def common(self, folder, output, profile=None):
        reader = os.environ.get("MUSUBI_BLACKBOX_READER")
        self.assertTrue(reader, "postbuild Blackbox reader required")
        path = folder / "selected.csv"
        path.write_text(output)
        run = subprocess.run([reader, str(profile or ROOT / "profiles/declared/betaflight-raw-main/profile.toml"),
                              str(path), "--allow-equal-time"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_large_native_multi_channel_and_independent_input(self):
        header = "time (us), vbatLatest, amperageLatest, rxSignalReceived, rxFlightChannelsValid, failsafePhase, rssi, vendor\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "native.csv"
            prefix = "".join(f"{i}, 1200, -20, 1, 1, 0, 900, {'x' * 32768}\r\n" for i in range(513))
            tail = "600, 1100, -5, 1, 1, 0, 800, alpha\r\n600, 1090, -6, 0, 0, 1, 0, beta\r\n"
            raw = (header + prefix + tail).encode()
            self.assertGreater(len(raw), 16 * 1024 * 1024)
            path.write_bytes(raw)
            output, report = convert_path(path, start_record=513, stop_record=515)
            self.assertEqual((report["source_records"], report["selected_records"], report["excluded_records"]), (515, 2, 513))
            self.assertEqual(report["source_sha256"], hashlib.sha256(raw).hexdigest())
            common = self.common(folder, output)
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(common["equal_time_order"], "source-order-only")
            reader = os.environ["MUSUBI_BLACKBOX_READER"]
            default = subprocess.run([reader, str(ROOT / "profiles/declared/betaflight-raw-main/profile.toml"),
                                      str(folder / "selected.csv")], capture_output=True, text=True)
            self.assertNotEqual(default.returncode, 0)
            self.assertIn("strictly increasing", default.stderr)
            self.assertEqual(len(common["observations"]), 6)
            onboard = [o for o in common["observations"] if o["channel"] == "onboard"]
            self.assertEqual([o["fields"]["source_record_index"] for o in onboard], [513, 514])
            self.assertEqual([o["fields"]["amperageLatest"] for o in onboard], [-5, -6])
            self.assertEqual([o["t_boot_us"] for o in onboard], [600, 600])
            self.assertTrue(all(o["clock_basis"] == "BootRelative" and o["wall_ms"] is None for o in onboard))
            self.assertEqual(bytes.fromhex(onboard[1]["fields"]["source_record_hex"]).decode(), tail.splitlines()[1])
            self.assertEqual(common["profile_units"]["amperageLatest"], "cA")
            path.write_bytes(b"time (us), vbatLatest, unknown\n77, 1400, independent\n")
            second, second_report = convert_path(path, start_record=0, stop_record=1)
            common = self.common(folder, second)
            self.assertEqual(len(common["observations"]), 1)
            self.assertEqual(common["observations"][0]["fields"]["unknown"], "independent")
            self.assertEqual(second_report["excluded_records"], 0)

    def test_inav_native_status_profile_composition(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "inav.csv"
            path.write_bytes(b"time (us), rxSignalReceived (flags), rxFlightChannelsValid (flags), failsafePhase (flags), rssi, extension\r\n10, 1, 1, 0, 789, first\r\n10, 0, 0, 1, 0, last\r\n")
            output, report = convert_path(path, start_record=0, stop_record=2)
            common = self.common(folder, output, ROOT / "profiles/public/betaflight_blackbox_csv.toml")
            self.assertEqual(common["profile_units"], {})
            self.assertEqual(common["main_rows"], 2)
            self.assertEqual(len(common["observations"]), 6)
            rc = [o for o in common["observations"] if o["channel"] == "rc"]
            self.assertEqual([o["fields"]["rxSignalReceived"] for o in rc], [1, 0])
            self.assertEqual([o["fields"]["rxFlightChannelsValid"] for o in rc], [1, 0])
            self.assertEqual([o["fields"]["failsafePhase"] for o in rc], [0, 1])
            link = [o for o in common["observations"] if o["channel"] == "link_stats"]
            self.assertEqual([o["fields"]["rssi"] for o in link], [789, 0])
            self.assertTrue(all(o["t_boot_us"] == 10 and o["clock_basis"] == "BootRelative" for o in common["observations"]))
            self.assertEqual(report["source_records"], 2)

    def test_refusals_and_selected_native_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "native.csv"
            good = "time (us), vbatLatest\n1, 1234\n2, 1233\n"
            for text in ("time (ms), vbatLatest\n1, 1\n", good + "0, 1\n",
                         good + "3\n", "time (us), source_file_sha256\n1, fake\n",
                         "time (us), time\n1, 1\n"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    convert_path(path, start_record=0, stop_record=1)
            path.write_text(good)
            for a, b in ((True, 1), (1, 1), (0, 3)):
                with self.assertRaises(ValueError):
                    convert_path(path, start_record=a, stop_record=b)
            output, _ = convert_path(path, start_record=0, stop_record=2)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r[" vbatLatest"] for r in rows], [" 1234", " 1233"])

    def test_cli_pair_and_overwrite_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, out, report = root / "input", root / "output", root / "report"
            path.write_bytes(b"time (us), vbatLatest\n1, 1234\n")
            argv = [sys.executable, str(ROOT / "scripts/select_blackbox_csv_window.py"),
                    str(path), str(out), "--report", str(report), "--start-record", "0", "--stop-record", "1"]
            run = subprocess.run(argv, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            saved = out.read_bytes()
            self.assertEqual(json.loads(report.read_text())["output_sha256"], hashlib.sha256(saved).hexdigest())
            self.assertEqual(subprocess.run(argv, capture_output=True).returncode, 2)
            self.assertEqual(out.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
