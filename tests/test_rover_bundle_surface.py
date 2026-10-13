"""Record-derived Surface qualification; authored inputs, not physical boats."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from tests.test_rover_saved_bundle import original, bundle, ROOT
from tests.test_ardupilot_record_export import pack, HEAD


def boat(second=False):
    return original(second).replace(b"FRAME_CLASS\x00\x00\x00\x00\x00" + struct.pack("<f", 1),
                                    b"FRAME_CLASS\x00\x00\x00\x00\x00" + struct.pack("<f", 2), 1)


def copter(second=False, omit=()):
    """Same authored layouts with Copter 4.5.5 VER/MSG text (equal byte lengths keep framing)."""
    return (original(second, omit).replace(b"ArduRover V4.5.5\x00", b"ArduCopter V4.5.5", 1)
            .replace(b"ArduRover, saved report", b"ArduCopter saved report", 1))


class RoverBundleSurfaceTests(unittest.TestCase):
    def test_two_boat_inputs_all_common_csv_and_position_declaration(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common CSV reader required")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.bin"
            for second in (False, True):
                source.write_bytes(boat(second))
                files, report = bundle.convert(source, bundle.PIN, qualify_surface=True)
                self.assertEqual(report["domain"], "Surface")
                self.assertEqual(report["domain_parameter_values"], [2])
                self.assertEqual(len(report["domain_parameter_refs"]), 1)
                for name, data in files.items():
                    (folder / name).write_bytes(data)
                for item in report["outputs"]:
                    if "profile" in item:
                        run = subprocess.run([reader, str(folder / item["profile"]),
                            str(folder / item["file"]), "--allow-equal-time",
                            "--preserve-nonfinite-as-text"], capture_output=True)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        common = json.loads(run.stdout)
                        self.assertIn('"Surface"', json.dumps(common))
                        self.assertIn("same_original_selected_FRAME_CLASS_2", json.dumps(common))
                    else:
                        self.assertEqual(json.loads(files[item["position_adapter"]])["domain"], "surface")
                        self.assertIn(b"same_original_selected_FRAME_CLASS_2", files[item["file"]])

    def test_missing_nonboat_conflict_and_default_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            for content in (original(), original(omit=("PARM",))):
                source.write_bytes(content)
                _, report = bundle.convert(source, bundle.PIN, qualify_surface=True)
                self.assertEqual(report["domain"], "Unknown")
            source.write_bytes(boat())
            default = bundle.convert(source, bundle.PIN)
            self.assertEqual(default, bundle.convert(source, bundle.PIN, qualify_surface=False))
            _, report = bundle.convert(source, bundle.PIN, record_window=(11, 14), qualify_surface=True)
            self.assertEqual(report["domain"], "Unknown")
            parm_index = list(bundle.LAYOUTS).index("PARM")
            record_id = 140 + parm_index
            extra = b"\xa3\x95" + bytes([record_id]) + struct.pack("<Q16sff", 3000, b"FRAME_CLASS", 1, 1)
            source.write_bytes(boat() + extra)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                bundle.convert(source, bundle.PIN, qualify_surface=True)

    def test_frame_class1_with_ardurover_ver_reaches_ground_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common CSV reader required")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.bin"
            for second in (False, True):
                source.write_bytes(original(second))
                files, report = bundle.convert(source, bundle.PIN, qualify_ground=True)
                self.assertEqual((report["domain"], report["domain_parameter_values"],
                                  report["domain_identity_families"]), ("Ground", [1], ["ArduRover"]))
                self.assertEqual((len(report["domain_parameter_refs"]), len(report["domain_identity_refs"])), (1, 1))
                for name, data in files.items():
                    (folder / name).write_bytes(data)
                for item in report["outputs"]:
                    if "profile" in item:
                        run = subprocess.run([reader, str(folder / item["profile"]),
                            str(folder / item["file"]), "--allow-equal-time",
                            "--preserve-nonfinite-as-text"], capture_output=True)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        common = json.dumps(json.loads(run.stdout))
                        self.assertIn('"Ground"', common)
                        self.assertNotIn('"Surface"', common)
                        self.assertIn("FRAME_CLASS_1_and_VER_ArduRover", common)
                    else:
                        self.assertEqual(json.loads(files[item["position_adapter"]])["domain"], "ground")

    def test_ground_needs_both_same_original_conditions(self):
        vendor = original().replace(b"ArduRover V4.5.5", b"VendorBuild-XYZ1", 1)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            for content in (boat(), original(omit=("VER",)), original(omit=("PARM",)), vendor):
                source.write_bytes(content)
                files, report = bundle.convert(source, bundle.PIN, qualify_ground=True)
                self.assertEqual((report["domain"], report["domain_basis"]),
                                 ("Unknown", "no_selected_Rover_ground_evidence"))
                self.assertFalse(any(name.endswith(".profile.toml") for name in files))
            source.write_bytes(original())
            _, report = bundle.convert(source, bundle.PIN, record_window=(11, 14), qualify_ground=True)
            self.assertEqual(report["domain"], "Unknown")
            self.assertEqual(bundle.convert(source, bundle.PIN), bundle.convert(source, bundle.PIN, qualify_ground=False))
            for flags in (dict(qualify_ground=True, qualify_surface=True), dict(qualify_ground=1)):
                with self.assertRaisesRegex(ValueError, "domain qualification"):
                    bundle.convert(source, bundle.PIN, **flags)

    def test_copter_ver_identity_reaches_air_common(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common CSV reader required")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "source.bin"
            for second in (False, True):
                source.write_bytes(copter(second))
                files, report = bundle.convert(source, bundle.COPTER_PIN, qualify_air=True)
                self.assertEqual((report["domain"], report["domain_identity_families"], report["bundle_schema"],
                                  report["missing_required_roles"]), ("Air", ["ArduCopter"], "copter-basic-saved/v1", []))
                self.assertEqual(report["domain_basis"], "same_original_selected_VER_ArduCopter_4.5.5_not_authenticated_not_airframe")
                for name, data in files.items():
                    (folder / name).write_bytes(data)
                self.assertIn(b"copter455_reported_navigation_message", files["GPS.profile.toml"])
                self.assertIn(b"copter-4.5.5", files["MODE.csv"])
                self.assertIn(bundle.COPTER_PIN.encode(), files["ATT.csv"])
                for item in report["outputs"]:
                    if "profile" in item:
                        run = subprocess.run([reader, str(folder / item["profile"]),
                            str(folder / item["file"]), "--allow-equal-time",
                            "--preserve-nonfinite-as-text"], capture_output=True)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        common = json.loads(run.stdout)
                        self.assertEqual(common["platform_domain"], "Air")
                        self.assertNotIn("frame", " ".join(common["observations"][0]["fields"]).lower())
                    else:
                        self.assertEqual(json.loads(files[item["position_adapter"]])["domain"], "air")

    def test_copter_air_needs_same_original_identity_and_exact_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            for content in (copter().replace(b"ArduCopter V4.5.5", b"VendorBuild-XYZ12", 1),
                            copter(omit=("VER",))):
                source.write_bytes(content)
                files, report = bundle.convert(source, bundle.COPTER_PIN, qualify_air=True)
                self.assertEqual((report["domain"], report["domain_basis"]),
                                 ("Unknown", "no_selected_ArduCopter_identity_evidence"))
                self.assertFalse(any(name.endswith(".profile.toml") for name in files))
            source.write_bytes(copter())
            _, report = bundle.convert(source, bundle.COPTER_PIN, record_window=(11, 14), qualify_air=True)
            self.assertEqual(report["domain"], "Unknown")
            files, report = bundle.convert(source, bundle.COPTER_PIN)
            self.assertNotIn("domain_basis", report)
            self.assertEqual(report["domain"][:7], "UNKNOWN")
            self.assertEqual({item["profile"] for item in report["outputs"] if "profile" in item} & {"GPS.profile.toml"}, set())
            for text in (b"ArduCopter V4.5.5 (abc)", b"ArduCopter saved report"):
                source.write_bytes(copter().replace(b"ArduCopter saved report", text, 1))
                self.assertEqual(bundle.convert(source, bundle.COPTER_PIN, qualify_air=True)[1]["domain"], "Air")
            for content in (original(), copter().replace(b"ArduCopter V4.5.5", b"ArduCopter V4.5.4", 1),
                            copter().replace(b"ArduCopter saved report", b"ArduPlane V4.5.5 report", 1),
                            copter().replace(b"ArduCopter saved report", b"ArduCopter V4.5.4 (abc)", 1)):
                source.write_bytes(content)
                with self.assertRaises(ValueError):
                    bundle.convert(source, bundle.COPTER_PIN, qualify_air=True)
            source.write_bytes(copter())
            for pin, flags in ((bundle.PIN, dict(qualify_air=True)), (bundle.COPTER_PIN, dict(qualify_ground=True)),
                               (bundle.COPTER_PIN, dict(qualify_air=True, qualify_surface=True)),
                               (bundle.COPTER_PIN, dict(qualify_air=1)), ("142aece2", {})):
                with self.assertRaises(ValueError):
                    bundle.convert(source, pin, **flags)

    def test_two_gps_receivers_stay_separate_position_tracks(self):
        """Interleaved GPS instances are projected per reported `I`, never as one track."""
        fmt = bundle.LAYOUTS["GPS"][0]
        record_id = 140 + list(bundle.LAYOUTS).index("GPS")

        def gps(stamp, instance, lat):
            values = (stamp, instance, 3, 1000, 2300, 9, 120, lat, 1390000000, 900, 1.0, 45.0, 0.0, 0.0, 0)
            return HEAD + bytes([record_id]) + b"".join(pack(c, [v]) for c, v in zip(fmt, values))

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(boat() + gps(1000, 1, 351000000) + gps(1100, 0, 350000100)
                               + gps(1100, 1, 351000100))
            files, report = bundle.convert(source, bundle.PIN, qualify_surface=True)
            tracks = [item for item in report["outputs"] if "position_adapter" in item]
            self.assertEqual([(t["file"], t["gps_instance"], t["records"]) for t in tracks],
                             [("GPS-I0-position-000000.jsonl", 0, 2),
                              ("GPS-I1-position-000000.jsonl", 1, 2)])
            self.assertEqual(sum(t["records"] for t in tracks), report["exported_counts"]["GPS"])
            for track in tracks:
                self.assertEqual(track["position_adapter"], "GPS-position-adapter.json")
                lines = [json.loads(x) for x in files[track["file"]].splitlines()]
                self.assertEqual({x["data"]["source_record"]["I"] for x in lines}, {str(track["gps_instance"])})
                self.assertEqual([x["data"]["TimeUS"] for x in lines], [1000, 1100])
            source.write_bytes(boat())
            _, report = bundle.convert(source, bundle.PIN)
            single = [item for item in report["outputs"] if "position_adapter" in item]
            self.assertEqual([(t["file"], t["gps_instance"]) for t in single],
                             [("GPS-position-000000.jsonl", 0)])


if __name__ == "__main__":
    unittest.main()
