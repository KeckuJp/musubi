"""Typed local refusal evidence, never an accepted observation or physical cause."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PROFILE = ('profile_id="refusal_fixture"\nversion="1"\nfamily="unknown"\n'
           'source_role="recorded_export"\nformat="telemetry_csv_us"\n'
           'extensions=["csv"]\ndefault_clock_basis="unknown"\nchannels=["onboard"]\n'
           '[fields]\ntime="t"\n')
GOOD = b"t,value\n1000,3\n2000,4\n"


class ReadRefusalTests(unittest.TestCase):
    def test_expected_bytes_preserved_when_source_cannot_be_read(self):
        digest = "sha256:" + hashlib.sha256(GOOD).hexdigest()
        options = ("--expected-input-size", str(len(GOOD)), "--expected-input-sha256", digest)
        with tempfile.TemporaryDirectory() as directory:
            result, path, _, _ = self.run_reader(Path(directory), data=None, options=options)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            report = json.loads(path.read_bytes())
            self.assertEqual(report["failure"]["kind"], "Io")
            self.assertIsNone(report["input"]["sha256"])
            self.assertEqual(report["input"]["expected_bytes"], {
                "size": len(GOOD), "sha256": digest,
                "basis": "caller_declared_not_authenticated", "comparison": "NOT_PERFORMED"})

    def test_expected_bytes_binds_loaded_original_and_refuses_valid_prefix(self):
        options = ("--expected-input-size", str(len(GOOD)), "--expected-input-sha256",
                   "sha256:" + hashlib.sha256(GOOD).hexdigest())
        for data in (GOOD.splitlines(keepends=True)[0] + GOOD.splitlines(keepends=True)[1],
                     GOOD.replace(b",4", b",5")):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                result, path, _, source = self.run_reader(Path(directory), data, options=options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                report = json.loads(path.read_bytes())
                self.assertEqual(report["failure"]["kind"], "ExpectedBytesMismatch")
                self.assertEqual(report["stage"], "input_expectation")
                self.assertEqual(report["accepted_observations"], 0)
                self.assertEqual(report["input"]["sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
                self.assertEqual(report["input"]["expected_bytes"]["sha256"], options[-1])
                self.assertEqual(report["failure_outcome"]["candidates"], [])
                self.assertEqual(source.read_bytes(), data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, _, _, _ = self.run_reader(root)
            new, path, _, _ = self.run_reader(root, options=options)
            self.assertEqual((new.returncode, new.stdout), (old.returncode, old.stdout))
            self.assertFalse(path.exists())
        for bad in (("--expected-input-size", "1"),
                    ("--expected-input-size", "-1", "--expected-input-sha256", options[-1]),
                    ("--expected-input-size", "1", "--expected-input-sha256", "sha256:xyz")):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                result, path, _, _ = self.run_reader(Path(directory), options=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertFalse(path.exists())

    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.reader:
            raise unittest.SkipTest("actual built reader required")

    def run_reader(self, root, data=GOOD, profile=PROFILE, *, options=(), sidecar=True):
        p, source, target = root / "profile.toml", root / "input.csv", root / "refusal.json"
        p.write_bytes(profile.encode() if isinstance(profile, str) else profile)
        if data is not None:
            source.write_bytes(data)
        args = [self.reader, str(p), str(source), *options]
        if sidecar:
            args += ["--read-refusal-report", str(target)]
        return subprocess.run(args, capture_output=True), target, p, source

    def test_success_and_legacy_failure_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, _, _, _ = self.run_reader(root, sidecar=False)
            new, target, _, _ = self.run_reader(root)
            self.assertEqual((old.returncode, new.returncode), (0, 0), new.stderr)
            self.assertEqual(new.stdout, old.stdout)
            self.assertFalse(target.exists())
            self.assertEqual(json.loads(new.stdout)["main_rows"], 2)
            equal = GOOD.replace(b"2000", b"1000")
            old, _, _, _ = self.run_reader(root, equal, options=("--allow-equal-time",), sidecar=False)
            new, target, _, _ = self.run_reader(root, equal, options=("--allow-equal-time",))
            self.assertEqual((new.returncode, new.stdout), (0, old.stdout))
            self.assertFalse(target.exists())
            old, target, _, _ = self.run_reader(root, b"t,value\n1000\n", sidecar=False)
            self.assertNotEqual(old.returncode, 0)
            self.assertEqual(old.stdout, b"")
            self.assertFalse(target.exists())

    def test_malformed_input_keeps_exact_bytes_and_line_not_observation(self):
        for data, offset in ((b"t,value\n1000,3\n2000\n", 3), (b"\xff", 1)):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                result, path, profile, source = self.run_reader(Path(directory), data)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(path.exists(), result.stderr)
                report = json.loads(path.read_bytes())
                self.assertEqual(report["status"], "READ_REFUSED")
                self.assertEqual(report["accepted_observations"], 0)
                self.assertIsNone(report["input_record_count"])
                self.assertIsNone(report["parsed_prefix_records"])
                self.assertEqual(report["failure"]["kind"], "Malformed")
                self.assertEqual(report["failure"]["locator"], {"value": offset, "unit": "source_line_or_zero_unavailable"})
                self.assertEqual(report["stage"], "input_decode")
                self.assertEqual(report["input"]["sha256"], "sha256:" + hashlib.sha256(data).hexdigest())
                self.assertEqual(report["profile"]["sha256"], "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest())
                self.assertEqual(source.read_bytes(), data)
                outcome = report["failure_outcome"]
                self.assertEqual(outcome["outcome"], "unknown")
                self.assertEqual(outcome["unknown_reason"], "insufficient-evidence")
                self.assertEqual(outcome["candidates"], [])
                self.assertTrue(report["unresolved_alternatives"])
                self.assertEqual(report["clock_basis"], "Unknown")
                self.assertIsNone(report["time_window"])

    def test_profile_io_and_bounds_are_not_called_corrupt(self):
        cases = [(GOOD, PROFILE.replace('format="telemetry_csv_us"', 'format="mavlink_tlog"'), "Profile", "input_decode"),
                 (GOOD, "not toml !", "Profile", "profile_decode"),
                 (GOOD, b"\xff", "ProfileEncoding", "profile_decode"),
                 (None, PROFILE, "Io", "input_read"),
                 (b"x" * (16 * 1024**2 + 1), PROFILE, "BoundExceeded", "input_read")]
        for data, profile, kind, stage in cases:
            with self.subTest(kind=kind, stage=stage), tempfile.TemporaryDirectory() as directory:
                result, path, _, _ = self.run_reader(Path(directory), data, profile)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(path.exists(), result.stderr)
                report = json.loads(path.read_bytes())
                self.assertEqual((report["failure"]["kind"], report["stage"]), (kind, stage))
                self.assertEqual(report["failure_outcome"]["outcome"], "unknown")
                if stage != "input_decode":
                    self.assertIsNone(report["input"]["sha256"])
                if kind == "Io":
                    self.assertEqual(report["failure"]["io_kind"], "NotFound")
                    self.assertEqual(report["input"]["operation"], "metadata")
                if kind == "Profile" and stage == "input_decode":
                    self.assertEqual(report["profile_declaration"]["format"], "mavlink_tlog")
                    self.assertEqual(report["reader"], "telemetry_csv_us")

    def test_fresh_sidecar_alias_and_option_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "refusal.json").write_bytes(b"preserve")
            result, path, p, source = self.run_reader(root, b"bad")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), b"preserve")
            source.unlink()
            command = [self.reader, str(p), str(source), "--read-refusal-report", str(source)]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(source.exists())
            destination = root / "new.json"
            command = [self.reader, str(p), str(source), "--read-refusal-report", str(destination),
                       "--recorded-failure-candidates"]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertFalse(destination.exists())

    def test_bounded_diagnostic_and_unavailable_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = PROFILE.replace('family="unknown"', 'family="' + "x" * 6000 + '"')
            result, target, p, source = self.run_reader(root, profile=profile)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(target.read_bytes())
            self.assertEqual(report["failure"]["kind"], "Profile")
            self.assertTrue(report["diagnostic"]["truncated"])
            self.assertEqual(len(report["diagnostic"]["quoted_text_prefix"].encode()), 4096)
            self.assertGreater(report["diagnostic"]["original_bytes"], 4096)
            self.assertLessEqual(target.stat().st_size, 64 * 1024)
            self.assertIsNone(report["input"]["sha256"])
            missing_parent = root / "absent/refusal.json"
            failed = subprocess.run([self.reader, str(p), str(source),
                "--read-refusal-report", str(missing_parent)], capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, b"")
            self.assertFalse(missing_parent.exists())


if __name__ == "__main__":
    unittest.main()
