"""Authored synthetic tests for explicitly selecting an offline GPS decoder stream.

Ordinary decoder calls are stubbed. The explicitly gated saved-SI test consumes a real
decoder export separately produced by the CLI; private records are never bundled into CI.
Existing default-main compatibility tests must also remain in the ordinary suite.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import csv
import io
import hashlib
import math
from unittest.mock import patch


ROOT = Path(os.environ.get("MUSUBI_POSITION_CSV_ROOT", Path(__file__).resolve().parents[1]))
SCRIPT = ROOT / "scripts" / "decode_blackbox_recording.py"
GPS = (b"time (us), GPS_numSat, GPS_coord[0], GPS_coord[1], GPS_altitude, vendor\n"
       b"1001, 7, 1.25, -2.5, 1234, retained-a\n"
       b"1001, 8, 1.5, -2.0, 1235, retained-b\n")
MAIN = b"time (us), vbatLatest\n1, 2100\n2, 2101\n3, 2102\n4, 2103\n"
REPORT = "I frames 1 20 bytes avg\nP frames 3 10 bytes avg\nG frames 2 15 bytes avg\nFrames 4 20 bytes\n7 iterations are missing in total\n"


class BlackboxGpsStreamContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("blackbox_stream_under_test", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def recording(self, version="4.2.0"):
        return (self.module.MAGIC + f"H Firmware revision:Betaflight {version} (authored) GENERIC\n".encode()
                + self.module.END)

    def motion_recording(self, *, gyro=True, acc=True):
        header = b"H Firmware revision:Betaflight 4.2.0 (authored) GENERIC\n"
        if gyro:
            header += b"H gyro_scale:0x3f800000\n"
        if acc:
            header += b"H acc_1G:2048\n"
        return self.module.MAGIC + header + self.module.END

    @unittest.skipUnless(all(os.environ.get(name) for name in (
        "MUSUBI_BLACKBOX_SI_EXPORT", "MUSUBI_BLACKBOX_NATIVE_EXPORT", "MUSUBI_TELEMETRY_READER")),
        "explicit saved SI/native exports and actual reader required")
    def test_saved_pinned_si_export_reaches_common_without_second_scaling(self):
        # No decoder rerun: the preceding CLI execution has its own exit status/stderr evidence.
        spec = importlib.util.spec_from_file_location("si_angles", ROOT / "scripts/convert_recorded_angles_csv.py")
        angles = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(angles)
        export = Path(os.environ["MUSUBI_BLACKBOX_SI_EXPORT"])
        metadata = json.loads((export.parent / "decode.json").read_text())
        self.assertEqual(metadata["input_sha256"],
                         "5fb78729580c414f41654d3b2103417088a972c9049ce873303835fc5c66c8f2")
        self.assertEqual(metadata["decoder_sha256"],
                         "13eefdb1c18c99f2da3923b279a6995a2f5f83f462097094b0a964fe48d9ca17")
        self.assertEqual(metadata["main_frames"], 16774)
        self.assertEqual(metadata["missing_loop_iterations"], 117376)
        self.assertEqual(metadata["motion_scaling_headers"],
                         {"gyro_scale": "0x3f800000", "acc_1G": "2048"})
        text = export.read_text()
        lines = text.splitlines()
        decoded = list(csv.DictReader(io.StringIO(text), skipinitialspace=True))
        native = list(csv.DictReader(io.StringIO(
            Path(os.environ["MUSUBI_BLACKBOX_NATIVE_EXPORT"]).read_text()), skipinitialspace=True))
        self.assertEqual(len(decoded), len(native))
        self.assertEqual(len(decoded), metadata["main_frames"])
        # Retaining every original column makes this whole-table output exceed the existing cap.
        # Existing explicit intervals cover every row; no trimming or bound relaxation.
        with self.assertRaisesRegex(ValueError, "output exceeds bound"):
            angles.convert(text, "betaflight-4.2.0-si-motion")
        names = [f"reported_angular_velocity_{a}_rad_s" for a in "xyz"] + [
            f"reported_acceleration_{a}_m_s2" for a in "xyz"]
        fields = [f"gyroADC[{i}] (rad/s)" for i in range(3)] + [
            f"accSmooth[{i}] (m/s/s)" for i in range(3)]
        source_sha = hashlib.sha256(export.read_bytes()).hexdigest()
        total, manifests = 0, []
        with tempfile.TemporaryDirectory() as temporary:
            proof = Path(os.environ.get("MUSUBI_BLACKBOX_SI_PROOF_DIR", temporary))
            proof.mkdir(parents=True, exist_ok=True)
            for start in range(0, len(decoded), 4000):
                stop = min(start + 4000, len(decoded))
                output, report = angles.convert_path(export, "betaflight-4.2.0-si-motion",
                                                      start_record=start, stop_record=stop)
                path = proof / f"motion-{start}-{stop}.csv"
                with path.open("x") as target:
                    target.write(output)
                common_run = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/recorded-angles/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True)
                common = json.loads(common_run.stdout)
                self.assertEqual(common["main_rows"], stop - start)
                self.assertEqual(common["platform_domain"], "Unknown")
                observations = common["observations"]
                self.assertEqual(len(observations), stop - start)
                for index, observation in enumerate(observations, start):
                    values = observation["fields"]
                    self.assertEqual(observation["clock_basis"], "BootRelative")
                    self.assertEqual(values["source_record_index"], index)
                    self.assertEqual(values["source_file_sha256"], "sha256:" + source_sha)
                    self.assertEqual(values["record_time_us"], int(decoded[index]["time (us)"]))
                    self.assertEqual(decoded[index]["time (us)"], native[index]["time (us)"])
                    self.assertEqual(bytes.fromhex(values["source_record_hex"][4:]).decode(),
                                     lines[index + 1])
                    for offset, (name, field) in enumerate(zip(names, fields)):
                        expected = float(decoded[index][field])
                        self.assertEqual(values[name], expected)
                        raw_field = (f"gyroADC[{offset}]" if offset < 3
                                     else f"accSmooth[{offset - 3}]")
                        scale = math.pi / 180 if offset < 3 else 9.80665 / 2048
                        self.assertAlmostEqual(expected, float(native[index][raw_field]) * scale,
                                               delta=0.0051)
                report["common_rows"] = len(observations)
                report["common_sha256"] = hashlib.sha256(common_run.stdout).hexdigest()
                (proof / f"report-{start}-{stop}.json").write_text(json.dumps(report, indent=2) + "\n")
                manifests.append(report)
                total += len(observations)
            self.assertEqual(total, metadata["main_frames"])
            (proof / "summary.json").write_text(json.dumps({"records": total,
                "quantities_checked": total * 6, "parts": manifests}, indent=2) + "\n")

    def test_si_motion_export_is_opt_in_and_refuses_unguessable_scaling(self):
        # The default path keeps its native columns and asks the decoder for no motion units.
        with tempfile.TemporaryDirectory() as tmp:
            output, calls = self.run_stub(Path(tmp), mode="main",
                                          recording=self.motion_recording())
            self.assertNotIn("--unit-rotation", calls[0])
            self.assertNotIn("--unit-acceleration", calls[0])
            self.assertEqual(json.loads((output / "decode.json").read_text())["motion_units"],
                             "native")
        # The opt-in asks the pinned decoder for SI rotation and acceleration and records the
        # scaling headers it relied on, plus the fact that no body frame is established.
        with tempfile.TemporaryDirectory() as tmp:
            output, calls = self.run_stub(Path(tmp), mode="main",
                                          recording=self.motion_recording(), motion="si")
            self.assertEqual(calls[0][calls[0].index("--unit-rotation") + 1], "rad/s")
            # Fixed units.c parses m/s2; m/s/s is its CSV label, not an accepted CLI token.
            self.assertEqual(calls[0][calls[0].index("--unit-acceleration") + 1], "m/s2")
            decode = json.loads((output / "decode.json").read_text())
            self.assertEqual(decode["motion_units"], "si")
            self.assertEqual(decode["motion_scaling_headers"],
                             {"gyro_scale": "0x3f800000", "acc_1G": "2048"})
            self.assertEqual(decode["source_units"]["gyroADC[0]"], "rad/s")
            self.assertEqual(decode["source_units"]["accSmooth[2]"], "m/s/s")
            self.assertIn("no body frame is established", decode["motion_axis_basis"])
        # 4.2.0 writes acc_1G only under USE_ACC, and the pinned decoder silently substitutes 1
        # for a missing scaling line. Missing metadata is refused here instead of guessed.
        for missing, name in ((dict(acc=False), "acc_1G"), (dict(gyro=False), "gyro_scale")):
            with self.subTest(name=name), self.assertRaises(ValueError) as refusal:
                self.module.motion_scaling_headers(self.motion_recording(**missing))
            self.assertIn(name, str(refusal.exception))
        # The exact metadata the pinned 4.2.0 writer produces is accepted, and only that.
        self.assertEqual(self.module.motion_scaling_headers(self.motion_recording()),
                         {"gyro_scale": "0x3f800000", "acc_1G": "2048"})
        header = b"H Firmware revision:Betaflight 4.2.0 (authored) GENERIC\n"
        for body, why in (
                (b"H gyro_scale:0x3f800000\nH acc_1G:2048bad\n", "trailing garbage after acc_1G"),
                (b"H gyro_scale:0x3f800000\nH acc_1G:0\n", "zero acc_1G"),
                (b"H gyro_scale:0x3f800000\nH acc_1G:70000\n", "acc_1G beyond the writer type"),
                (b"H gyro_scale:anytext\nH acc_1G:2048\n", "gyro_scale that is not a float"),
                (b"H gyro_scale:0x3f800001\nH acc_1G:2048\n", "a gyro_scale 4.2.0 never writes"),
                (b"H gyro_scale:0x3f800000\nH gyro_scale:0x3f800000\nH acc_1G:2048\n",
                 "a repeated required header"),
                (b"H gyro_scale:0x3f800000\nH acc_1G:2048\nH acc_1G:1024\n",
                 "contradictory repeated acc_1G"),
                # A header-looking sequence after the header block is payload, not metadata.
                (b"I authored frame line\nH gyro_scale:0x3f800000\nH acc_1G:2048\n",
                 "scaling lines only in the body")):
            with self.subTest(why=why), self.assertRaises(ValueError):
                self.module.motion_scaling_headers(
                    self.module.MAGIC + header + body + self.module.END)
        # The SI schema is qualified for 4.2.0; a 4.2.x sibling is not quietly accepted under it,
        # and the native/GPS version scopes are untouched.
        self.assertEqual(self.module.require_si_firmware("Betaflight 4.2.0 (authored) GENERIC"),
                         "4.2.0")
        for revision in ("Betaflight 4.2.11 (authored) GENERIC",
                         "Betaflight 4.3.0 (authored) GENERIC", "Betaflight", ""):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.module.require_si_firmware(revision)
        self.assertIn("4.2.11", self.module.inspect_recording(self.recording("4.2.11")))
        # The refusal happens before the external decoder is ever invoked.
        with tempfile.TemporaryDirectory() as tmp:
            recording = (self.module.MAGIC
                         + b"H Firmware revision:Betaflight 4.2.11 (authored) GENERIC\n"
                         + b"H gyro_scale:0x3f800000\nH acc_1G:2048\n" + self.module.END)
            with self.assertRaises(ValueError) as refused:
                self.run_stub(Path(tmp), mode="main", recording=recording, motion="si")
            self.assertIn("qualified for Betaflight 4.2.0", str(refused.exception))

    def test_default_main_version_scope_is_unchanged(self):
        self.assertIn("4.2.0", self.module.inspect_recording(self.recording()))
        self.assertIn("4.2.11", self.module.inspect_recording(self.recording("4.2.11")))
        with self.assertRaises(ValueError):
            self.module.inspect_recording(self.recording("4.3.0"))

    def test_only_explicit_gps_mode_adds_43_family(self):
        for version in ("4.2.0", "4.2.11", "4.3.0", "4.3.2"):
            with self.subTest(version=version):
                self.assertIn(version, self.module.inspect_recording(self.recording(version), stream="gps"))
        for version in ("3.5.0", "4.1.0", "4.4.0", "5.0.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.module.inspect_recording(self.recording(version), stream="gps")

    def test_gps_mode_rejects_truncation_multilog_and_wrong_family(self):
        data = self.recording("4.3.0")
        for wrong in (data[:-1], data + data, data.replace(b"Betaflight", b"INAV"),
                      b"time,latitude\n1,2\n"):
            with self.subTest(kind=wrong[:24]), self.assertRaises(ValueError):
                self.module.inspect_recording(wrong, stream="gps")
        with self.assertRaises(ValueError):
            self.module.inspect_recording(data, stream="guess")

    def test_main_report_return_shape_stays_compatible(self):
        self.assertEqual(self.module.check_decoder_report(REPORT),
                         {"main_frames": 4, "missing_loop_iterations": 7})

    def test_gps_report_counts_g_frames_not_main_or_sampling_gaps(self):
        report = self.module.check_decoder_report(REPORT, stream="gps")
        self.assertEqual(report["main_frames"], 4)
        self.assertEqual(report["gps_frames"], 2)
        self.assertEqual(report["missing_loop_iterations"], 7)

    def test_gps_requires_one_positive_selected_frame_count(self):
        for report in ("Frames 4 20 bytes\n", REPORT.replace("G frames 2", "G frames 0"),
                       REPORT + "G frames 2 15 bytes\n", REPORT.replace("G frames 2", "G frames unknown")):
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.module.check_decoder_report(report, stream="gps")

    def test_any_corrupt_or_unreadable_report_prevents_gps_success(self):
        for suffix in ("1 frames failed to decode, rendering 0 loop iterations unreadable",
                       "0 frames failed to decode, rendering 1 loop iterations unreadable",
                       "0 frames failed to decode, rendering 0 loop iterations unreadable\n"
                       "1 frames failed to decode, rendering 0 loop iterations unreadable"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.module.check_decoder_report(REPORT + suffix, stream="gps")

    def run_stub(self, root, *, gps=GPS, report=REPORT, returncode=0, missing=False, mode="gps", recording=None, log_index=None, motion=None):
        raw, decoder, output = root / "source.bfl", root / "decoder", root / "output"
        raw.write_bytes(recording if recording is not None else self.recording("4.3.0" if mode == "gps" else "4.2.0"))
        decoder.write_bytes(b"authored decoder placeholder; never executed")
        calls = []

        def decode(argv, **kwargs):
            calls.append(argv)
            if mode == "gps":
                self.assertNotIn("--stdout", argv)
                self.assertNotIn("--merge-gps", argv)
                self.assertNotIn("--raw", argv)
                self.assertIn("--output-dir", argv)
                directory = Path(argv[argv.index("--output-dir") + 1])
                directory.resolve().relative_to(output.resolve())
                self.assertEqual(argv[argv.index("--prefix") + 1], "decoded")
                self.assertEqual(argv[argv.index("--unit-frame-time") + 1], "us")
                self.assertEqual(argv[argv.index("--unit-gps-speed") + 1], "mps")
                (directory / "decoded.01.csv").write_bytes(MAIN)
                if not missing:
                    (directory / "decoded.01.gps.csv").write_bytes(gps)
                (directory / "decoded.01.gps.gpx").write_bytes(b"<gpx/>")
                (directory / "decoded.01.event").write_bytes(b"authored event sidecar\n")
                stdout = b""
            else:
                self.assertIn("--stdout", argv)
                self.assertNotIn("--merge-gps", argv)
                stdout = MAIN
            return subprocess.CompletedProcess(argv, returncode, stdout, report.encode())

        args = [str(SCRIPT), str(decoder), str(raw), str(output)]
        if mode == "gps":
            args += ["--stream", "gps"]
        if log_index is not None:
            args += ["--log-index", str(log_index)]
        if motion is not None:
            args += ["--motion-units", motion]
        with patch.object(sys, "argv", args), patch.object(self.module.subprocess, "run", side_effect=decode):
            self.module.main()
        return output, calls

    def test_explicit_log_selection_preserves_bytes_and_default_refusal(self):
        first = self.recording("4.2.0")
        second = self.recording("4.2.11")
        dump = first + second
        with self.assertRaises(ValueError):
            self.module.inspect_recording(dump)
        for index, selected in ((1, first), (2, second)):
            sliced, selection = self.module.select_recording(dump, index)
            self.assertEqual(sliced, selected)
            self.assertEqual(selection["dump_log_candidates"], 2)
            self.assertEqual(selection["log_ranges"][1]["start_byte"], len(first))
            with tempfile.TemporaryDirectory() as tmp:
                output, calls = self.run_stub(Path(tmp), mode="main", recording=dump, log_index=index)
                self.assertEqual(Path(calls[0][-1]).read_bytes(), selected)
                self.assertEqual((output / "decoded.csv").read_bytes(), MAIN)
                metadata = json.loads((output / "decode.json").read_text())
                self.assertEqual(metadata["selected_log_index"], index)
                self.assertEqual(metadata["input_sha256"], self.module.hashlib.sha256(dump).hexdigest())
                self.assertEqual(metadata["selected_input_sha256"], self.module.hashlib.sha256(selected).hexdigest())
                self.assertEqual(metadata["log_ranges"][index - 1]["status"], "DECODED_SELECTED_STREAM_ONLY")
                self.assertEqual(metadata["log_ranges"][2 - index]["status"], "UNSELECTED_NOT_VALIDATED")
                self.assertEqual(metadata["common_output"], "NOT_RUN")
        for index in (0, -1, 3, True):
            with self.assertRaises(ValueError):
                self.module.select_recording(dump, index)
        with self.assertRaises(ValueError):
            self.module.select_recording(b"garbage" + dump, 1)
        for malformed in (dump[:-1], dump + b"\xff", first + self.recording("4.4.0")):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                self.run_stub(Path(tmp), mode="main", recording=malformed, log_index=2)
        # Selecting a complete first log does not certify the incomplete unselected second one.
        self.assertEqual(self.module.select_recording(dump[:-1], 1)[0], first)
        with tempfile.TemporaryDirectory() as tmp:
            output, calls = self.run_stub(Path(tmp), recording=dump, log_index=2)
            self.assertEqual(Path(calls[0][-1]).read_bytes(), second)
            self.assertEqual((output / "decoded.csv").read_bytes(), GPS)

    def test_selected_archived_recording_reaches_existing_common_output(self):
        source = os.environ.get("MUSUBI_BLACKBOX_RECORDING")
        decoder = os.environ.get("MUSUBI_BLACKBOX_DECODER")
        reader = os.environ.get("MUSUBI_BLACKBOX_READER")
        destination = os.environ.get("MUSUBI_BLACKBOX_SELECTION_OUTPUT")
        if not all((source, decoder, reader, destination)):
            self.skipTest("optional retained recording and installed tools not supplied")
        root = Path(destination); root.mkdir()
        original = Path(source).read_bytes()
        self.module.inspect_recording(original)
        # Constructed two-entry container, NOT a new real multi-flight recording.
        dump = root / "constructed-two-log.bbl"; dump.write_bytes(original + original)
        output = root / "selected-second"
        with patch.object(sys, "argv", [str(SCRIPT), decoder, str(dump), str(output), "--log-index", "2"]):
            self.module.main()
        self.assertEqual((output / "selected.bfl").read_bytes(), original)
        decoded = list(csv.DictReader(io.StringIO((output / "decoded.csv").read_text()), skipinitialspace=True))
        result = subprocess.run([reader,
            str(ROOT / "profiles/declared/betaflight-raw-main/profile.toml"),
            str(output / "decoded.csv")], check=True, capture_output=True)
        (root / "common-observations.json").write_bytes(result.stdout)
        common = json.loads(result.stdout)
        self.assertEqual(common["main_rows"], len(decoded))
        self.assertEqual(common["platform_domain"], "Unknown")
        onboard = [o for o in common["observations"] if o["channel"] == "onboard"]
        self.assertEqual(len(onboard), len(decoded))
        for index in (0, len(decoded) - 1):
            self.assertEqual(onboard[index]["fields"]["time"], int(decoded[index]["time (us)"]))
            self.assertEqual(onboard[index]["fields"]["vbatLatest"], int(decoded[index]["vbatLatest"]))
        report = {"constructed_container_not_new_real_recording": True,
                  "selected_bytes_equal_retained_recording": True,
                  "main_rows": len(decoded), "observations": len(common["observations"]),
                  "platform_domain": common["platform_domain"]}
        (root / "selection-result.json").write_text(json.dumps(report, indent=2))

    def test_gps_mode_selects_separate_file_and_retains_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, calls = self.run_stub(Path(tmp))
            self.assertEqual(len(calls), 1)
            self.assertEqual((output / "decoded.csv").read_bytes(), GPS)
            self.assertEqual(len((output / "decoded.csv").read_bytes().splitlines()) - 1, 2)
            self.assertTrue(list(output.rglob("decoded.01.csv")))
            self.assertTrue(list(output.rglob("decoded.01.event")))
            report = json.loads((output / "decode.json").read_bytes())
            self.assertEqual(report["stream"], "gps")
            self.assertEqual(report["gps_frames"], 2)
            self.assertEqual(report["main_frames"], 4)
            self.assertEqual(report["domain"], "Unknown")
            self.assertEqual(report["common_output"], "NOT_RUN")
            self.assertEqual(report["source_units"]["time"], "us")
            self.assertEqual(report["source_units"]["GPS_coord[0]"], "deg")
            self.assertEqual(report["source_units"]["GPS_coord[1]"], "deg")
            self.assertEqual(report["source_units"]["GPS_altitude"], "dm")
            self.assertNotIn("vbatLatest", report["source_units"])

    def test_main_default_outputs_and_native_units_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, _ = self.run_stub(Path(tmp), mode="main")
            self.assertEqual((output / "decoded.csv").read_bytes(), MAIN)
            report = json.loads((output / "decode.json").read_bytes())
            self.assertEqual(report["main_frames"], 4)
            self.assertEqual(report["source_units"],
                             {"time": "us", "vbatLatest": "cV", "amperageLatest": "cA"})

    def test_selected_file_missing_mismatch_or_failed_decoder_does_not_create_success_metadata(self):
        for options in ({"missing": True}, {"gps": GPS.splitlines()[0] + b"\n"},
                        {"gps": GPS + GPS.splitlines()[-1] + b"\n"}, {"returncode": 1},
                        {"report": REPORT + "1 frames failed to decode, rendering 0 loop iterations unreadable"}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises((ValueError, OSError)):
                    self.run_stub(Path(tmp), **options)
                self.assertFalse((Path(tmp) / "output" / "decode.json").exists())


if __name__ == "__main__":
    unittest.main()
