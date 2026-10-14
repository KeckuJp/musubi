"""Authored AgOpenGPS `Sections.txt` documents; no device, no field, no real recording.

Every input is built from the pinned writer's own layout
(AgOpenGPS-Official/AgOpenGPS 21be26aa58d09b9b5abacc715d0abcf4a021f1db,
SourceCode/GPS/IO/SectionFiles.cs with the patch semantics from Classes/CPatches.cs).
"""
import csv
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/agopen-sections"
SCRIPT = ROOT / "scripts/convert_agopen_sections.py"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
COLOUR = "27.000,151.000,160.00000"


def patch(vertices):
    """One patch exactly as the writer lays it out: count line, colour vertex, then coordinates."""
    return "\n".join([str(len(vertices) + 1), COLOUR] + vertices)


def rows(text, capture=4242):
    return list(csv.DictReader(io.StringIO(module.convert(text, capture))))


class AgOpenSectionsTests(unittest.TestCase):
    def test_worked_vertices_are_read_and_the_colour_vertex_is_not_a_position(self):
        document = "\n".join([
            patch(["100.125,200.250,0.00000", "101.125,200.250,0.00000", "100.130,201.500,0.00000",
                   "101.130,201.500,0.00000", "100.140,202.750,0.00000"]),
            patch(["300.000,400.000,0.00000", "301.000,400.000,0.00000",
                   "300.000,401.000,0.00000", "301.000,401.000,0.00000"])])
        observed = rows(document)
        # Two patches of 6 and 5 saved vertices give 5 + 4 coordinate rows: the colour is separated.
        self.assertEqual(len(observed), 9)
        self.assertEqual([r["patch_index"] for r in observed], ["0"] * 5 + ["1"] * 4)
        self.assertEqual([r["vertex_index"] for r in observed[:5]], ["0", "1", "2", "3", "4"])
        self.assertEqual((observed[0]["agopen_easting_m_local"], observed[0]["agopen_northing_m_local"]),
                         ("100.125", "200.25"))
        self.assertEqual(observed[5]["agopen_easting_m_local"], "300.0")
        self.assertEqual(observed[0]["patch_vertex_count"], "5")
        self.assertEqual(observed[5]["patch_vertex_count"], "4")
        # The colour line travels beside the geometry verbatim, never as a coordinate or a section id.
        colours = {bytes.fromhex(r["agopen_patch_colour_line_hex"][4:]).decode() for r in observed}
        self.assertEqual(colours, {COLOUR})
        self.assertNotIn("27.0", [r["agopen_easting_m_local"] for r in observed])
        # The third field is the literal zero the patch builder writes, not a heading.
        self.assertEqual({r["agopen_vertex_third_field"] for r in observed}, {"0.0"})
        self.assertIn("NOT_EVIDENCE_THAT_MATERIAL_WAS_APPLIED", observed[0]["agopen_coverage_basis"])
        self.assertIn("LOCAL_METRE_PLANE_PER_LOCALPLANE", observed[0]["agopen_plane_basis"])
        self.assertIn("NOT_A_HEADING", observed[0]["agopen_third_field_basis"])
        self.assertIn("NO_SECTION_INDEX", observed[0]["agopen_identity_basis"])
        self.assertEqual({r["record_time_us"] for r in observed}, {"4242"})

    def test_the_saved_text_survives_byte_for_byte(self):
        """source_record_hex promises the original line, so the writer's formatting must survive."""
        vertices = ["100.125,200.250,0.00000", "101.125,200.250,0.00000",
                    "100.130,201.500,0.00000", "101.130,201.500,0.00000"]
        observed = rows(patch(vertices))
        # Not the reparsed "100.125,200.25,0.0": the writer's own trailing zeros are preserved.
        self.assertEqual([bytes.fromhex(r["source_record_hex"][4:]).decode() for r in observed],
                         vertices)
        self.assertEqual(bytes.fromhex(observed[0]["agopen_patch_colour_line_hex"][4:]).decode(),
                         COLOUR)
        # The parsed numbers stay available beside the retained text.
        self.assertEqual(observed[0]["agopen_easting_m_local"], "100.125")
        # A non-zero third field is not refused: it is retained exactly as the file carried it.
        odd = rows(patch([vertices[0].replace("0.00000", "1.25000")] + vertices[1:]))
        self.assertEqual(odd[0]["agopen_vertex_third_field"], "1.25")
        self.assertEqual(bytes.fromhex(odd[0]["source_record_hex"][4:]).decode(),
                         "100.125,200.250,1.25000")

    def test_crlf_and_lf_terminators_read_the_same_coverage(self):
        """The pin is a .NET desktop writer, so an ordinary saved file is CRLF; LF must read too."""
        vertices = ["100.125,200.250,0.00000", "101.125,200.250,0.00000",
                    "100.130,201.500,0.00000", "101.130,201.500,0.00000"]
        document = patch(vertices)
        lf, crlf = rows(document + "\n"), rows(document.replace("\n", "\r\n") + "\r\n")
        # The count line and every vertex line parse the same; only the terminator differed.
        self.assertEqual(len(crlf), 4)
        self.assertEqual(crlf[0]["patch_vertex_count"], "4")
        self.assertEqual((crlf[0]["agopen_easting_m_local"], crlf[0]["agopen_northing_m_local"]),
                         ("100.125", "200.25"))
        # The digest covers the original bytes, so the two terminators are not the same document...
        self.assertNotEqual(crlf[0]["source_document_sha256"], lf[0]["source_document_sha256"])
        # ...while every other field, the retained text included, is identical.
        for expected, observed in zip(lf, crlf):
            self.assertEqual({k: v for k, v in observed.items() if k != "source_document_sha256"},
                             {k: v for k, v in expected.items() if k != "source_document_sha256"})
        self.assertEqual(bytes.fromhex(crlf[0]["source_record_hex"][4:]).decode(), vertices[0])
        self.assertEqual(bytes.fromhex(crlf[0]["agopen_patch_colour_line_hex"][4:]).decode(), COLOUR)
        # Only a terminating CR is removed: a CR inside a field is refused, never repaired away.
        with self.assertRaises(ValueError):
            module.convert(document.replace(vertices[0], "100.125\r,200.250,0.00000"), 1)

    def test_a_blank_line_inside_the_patches_is_refused_not_filtered(self):
        vertices = ["1.000,2.000,0.00000", "3.000,4.000,0.00000",
                    "5.000,6.000,0.00000", "7.000,8.000,0.00000"]
        document = patch(vertices)
        # One trailing terminator is the writer's own and stays acceptable, in either convention.
        module.convert(document + "\n", 1)
        module.convert(document.replace("\n", "\r\n") + "\r\n", 1)
        for label, broken in (("blank between vertices", document.replace(vertices[1], "\n" + vertices[1])),
                              ("blank before a count line", document + "\n\n" + patch(vertices)),
                              ("blank line past the final terminator", document + "\n\n"),
                              ("the same past a CRLF terminator",
                               document.replace("\n", "\r\n") + "\r\n\r\n")):
            with self.subTest(label=label), self.assertRaises(ValueError):
                module.convert(broken, 1)

    def test_the_writers_own_limits_are_refusals_not_repairs(self):
        good = patch(["1.000,2.000,0.00000", "3.000,4.000,0.00000",
                      "5.000,6.000,0.00000", "7.000,8.000,0.00000"])
        for label, document in (
                ("blank line is not the zero-byte writer state", "\n"),
                ("whitespace is not the zero-byte writer state", " "),
                ("bare carriage return is not empty", "\r"),
                ("non-integer count", "x\n1.0,2.0,3.0"),
                ("count beyond the saved lines", "9\n1.0,2.0,3.0"),
                ("shorter than the writer's own minimum", "4\n1.0,2.0,3.0\n1.0,2.0,3.0\n1.0,2.0,3.0\n1.0,2.0,3.0"),
                ("vertex with two fields", "5\n1.0,2.0\n1.0,2.0,3.0\n1.0,2.0,3.0\n1.0,2.0,3.0\n1.0,2.0,3.0"),
                ("non-numeric vertex", "5\n" + COLOUR + "\nnorth,2.0,3.0\n1.0,2.0,3.0\n1.0,2.0,3.0\n1.0,2.0,3.0"),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                module.convert(document, 1)
        for capture in (None, -1, True, 1.5):
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                module.convert(good, capture)

    def test_command_line_and_common_reader_carry_the_worked_geometry(self):
        configured = os.environ.get("MUSUBI_TELEMETRY_READER")
        if configured:
            self.assertTrue(Path(configured).exists(),
                            f"MUSUBI_TELEMETRY_READER is set to {configured!r}, which does not exist; "
                            "an explicitly configured reader that is missing is a failure, not a skip")
        elif not Path(READER).exists():
            self.skipTest("no reader configured and the default build is absent, so the common-output "
                          "proof cannot run; this is an explicit skip and never a pass")
        document = patch(["10.500,20.250,0.00000", "11.500,20.250,0.00000",
                          "10.500,21.750,0.00000", "11.500,21.750,0.00000"])
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "Sections.txt", Path(directory) / "out.csv"
            # Saved exactly as the .NET desktop writer leaves it: CRLF, and the CLI reads the file
            # without newline translation, so this is the ordinary input reaching the common reader.
            source.write_bytes((document.replace("\n", "\r\n") + "\r\n").encode())
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                            "--capture-time-us", "8080"], check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(target), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 4)
        self.assertEqual(common["platform_domain"], "Unknown")
        fields = common["observations"][0]["fields"]
        self.assertEqual((fields["agopen_easting_m_local"], fields["agopen_northing_m_local"]),
                         (10.5, 20.25))
        self.assertEqual(fields["agopen_vertex_third_field"], 0.0)
        self.assertEqual(fields["patch_vertex_count"], 4)
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        units = common["profile_units"]
        self.assertIn("metre_basis_comes_from_LocalPlane", units["agopen_easting_m_local"])
        self.assertIn("contains_no_timestamp_of_any_kind", units["record_time_us"])
        self.assertIn("not_evidence_that_material_was_applied", units["agopen_coverage_basis"])

    def test_empty_writer_state_has_zero_common_observations_and_source_accounting(self):
        report = {}
        self.assertEqual(list(csv.DictReader(io.StringIO(module.convert("", 1, report=report)))), [])
        self.assertEqual(report["source_document_sha256"], "sha256:" + hashlib.sha256(b"").hexdigest())
        self.assertEqual((report["source_bytes"], report["patch_count"], report["observation_count"]), (0, 0, 0))
        self.assertEqual(report["state"], "NO_SAVED_PATCHES")
        if not Path(READER).exists():
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                self.fail("configured common reader missing")
            self.skipTest("common reader not built")
        with tempfile.TemporaryDirectory() as directory:
            source, target, result = [Path(directory) / name for name in ("Sections.txt", "out.csv", "report.json")]
            source.write_bytes(b"")
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                            "--capture-time-us", "1", "--report", str(result)], check=True, capture_output=True)
            self.assertEqual(json.loads(result.read_text()), report)
            common = json.loads(subprocess.run([READER, str(CASE / "profile.toml"), str(target),
                                                "--allow-equal-time", "--allow-empty-rows"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 0)
        self.assertEqual(common["observations"], [])


if __name__ == "__main__":
    unittest.main()
