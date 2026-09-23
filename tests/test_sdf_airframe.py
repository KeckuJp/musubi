"""Authored SDF airframe declarations; finite spec checks, not a real or built aircraft."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from scripts.convert_sdf_airframe import BASIS, MASS_BASIS, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/sdf-airframe/profile.toml"


def model(body, version="1.11"):
    return f"<sdf version='{version}'><model name='plane'>{body}</model></sdf>".encode()


LINKS = ("<link name='body'><inertial><mass>1.5</mass>"
         "<pose>0.1 -0.2 0.05 0 0 0</pose></inertial></link>"
         "<link name='wing'><inertial><mass>0.25</mass></inertial></link>"
         "<link name='sensor'/>")
JOINTS = ("<joint name='wing_joint' type='revolute'><parent>body</parent><child>wing</child></joint>"
          "<joint name='mount' type='fixed'><parent>body</parent><child>sensor</child></joint>")


class SdfAirframeTests(unittest.TestCase):
    def rows(self, document, capture=555):
        output, report = convert(document, capture)
        return list(csv.DictReader(io.StringIO(output))), report

    def test_declared_structure_mass_and_frames_reach_the_rows(self):
        rows, report = self.rows(model(LINKS + JOINTS))
        self.assertEqual([row["airframe_record_kind"] for row in rows],
                         ["MODEL", "LINK", "LINK", "LINK", "JOINT", "JOINT"])
        links = {bytes.fromhex(row["airframe_link_name_hex"][4:]).decode(): row
                 for row in rows if row["airframe_record_kind"] == "LINK"}
        self.assertEqual(links["body"]["airframe_link_depth"], "0")
        self.assertEqual(links["body"]["airframe_link_child_count"], "2")
        self.assertEqual(links["wing"]["airframe_link_depth"], "1")
        self.assertEqual(bytes.fromhex(links["wing"]["airframe_link_parent_hex"][4:]).decode(),
                         "body")
        self.assertEqual(links["wing"]["airframe_link_parent_resolution"],
                         "DECLARED_IN_THIS_DOCUMENT")
        self.assertEqual(links["body"]["airframe_reported_mass"], "1.5")
        self.assertEqual(links["body"]["airframe_mass_disposition"], "REPORTED")
        for column in ("airframe_mass_kg", "airframe_com_x_m", "airframe_com_y_m",
                       "airframe_com_z_m"):
            self.assertEqual(links["body"][column], "")
        self.assertIn("NONE_DECLARED", links["body"]["airframe_unit_declaration"])
        self.assertIn("RETAINED_DATA", links["body"]["airframe_unit_declaration"])
        self.assertEqual(links["sensor"]["airframe_reported_mass"], "")
        self.assertEqual(links["sensor"]["airframe_mass_disposition"], "NOT_REPORTED")
        self.assertEqual(report["links_without_declared_mass"], 1)
        self.assertEqual((links["body"]["airframe_com_x"], links["body"]["airframe_com_y"],
                          links["body"]["airframe_com_z"]), ("0.1", "-0.2", "0.05"))
        self.assertIn("CENTRE_OF_MASS_FRAME_C_RELATIVE_TO_THE_LINK_FRAME_L",
                      links["body"]["airframe_com_frame_basis"])
        self.assertIn("RADIANS_BY_THE_DECLARED_VERSIONS_OWN_STATED_DEFAULT",
                      links["body"]["airframe_com_rotation_unit"])
        self.assertEqual(links["sensor"]["airframe_com_x"], "")
        self.assertEqual(links["sensor"]["airframe_com_frame_basis"], "")
        joints = [row for row in rows if row["airframe_record_kind"] == "JOINT"]
        self.assertEqual(joints[0]["airframe_joint_type"], "revolute")
        self.assertEqual(joints[0]["airframe_child_resolution"], "DECLARED_IN_THIS_DOCUMENT")
        self.assertIn("EXPRESSED_IN_THE_CHILD_LINK_FRAME", joints[0]["airframe_joint_frame_basis"])
        for absent in ("NOTHING_IS_SIMULATED_FLOWN_ACTUATED_OR_MEASURED",
                       "NEITHER_IS_CLAIMED_HERE", "NO_NAMED_PART_IS_ESTABLISHED_TO_EXIST"):
            self.assertIn(absent, rows[0]["airframe_declaration_basis"])
        self.assertIn("STATE_NO_UNIT", MASS_BASIS)
        self.assertIn("NEVER_DEFAULTED", MASS_BASIS)
        self.assertIn("CORROBORATION_FOR_A_DIFFERENT_ELEMENT_AND_NOT_A_DECLARATION", MASS_BASIS)
        self.assertEqual(report["declared_root_links"], 1)
        self.assertEqual(report["declared_sdf_version"], "1.11")

    def test_declared_units_frames_and_world_attachment_follow_the_specification(self):
        rows, _ = self.rows(model("<link name='a'><inertial>"
                                  "<pose degrees='true'>0 0 0 0 0 90</pose></inertial></link>"))
        self.assertIn("DEGREES_DECLARED_BY_THE_DOCUMENTS_OWN_POSE_ATTRIBUTE",
                      rows[1]["airframe_com_rotation_unit"])
        rows, report = self.rows(model("<link name='a'/>"
                                       "<joint name='j' type='fixed'>"
                                       "<parent>world</parent><child>a</child></joint>"))
        joint = rows[-1]
        self.assertEqual(joint["airframe_parent_is_world"], "1")
        self.assertEqual(joint["airframe_parent_resolution"], "WORLD_FRAME")
        self.assertEqual(report["world_attached_joints"], 1)
        self.assertEqual(report["unresolved_joint_references"], 0)
        self.assertEqual(rows[1]["airframe_link_depth"], "0")   # the walk stops at the world

    def test_an_unresolved_included_reference_stays_unknown_rather_than_refused(self):
        rows, report = self.rows(model(
            "<link name='a'/><include merge='true'><uri>model://LW20</uri></include>"
            "<joint name='j' type='fixed'><parent>a</parent><child>lw20_link</child></joint>"))
        joint = rows[-1]
        self.assertEqual(joint["airframe_child_resolution"],
                         "NOT_DECLARED_IN_THIS_DOCUMENT_NOT_RESOLVED_HERE")
        self.assertEqual(report["unresolved_joint_references"], 1)
        self.assertEqual(report["unselected_model_children"], 1)   # the include, counted not dropped
        self.assertIn("never followed", report["unresolved_basis"])
        self.assertIn("model://LW20", bytes.fromhex(report["source_xml_hex"][4:]).decode())

    def test_declared_units_become_canonical_meaning_and_are_never_defaulted(self):
        """The stated function needs mass and geometry WITH units. The specification states none
        for either element in either read version, and no global SI statement exists at the pin,
        so the unit is the caller's explicit declaration or the canonical columns stay empty."""
        document = model("<link name='a'><inertial><mass>1.5</mass>"
                         "<pose>100 -20 5 0 0 0</pose></inertial></link>")
        rows, report = self.rows(document)
        self.assertIn("NONE_DECLARED", report["unit_declaration"])
        self.assertEqual(rows[1]["airframe_mass_kg"], "")

        output, report = convert(document, 555, mass_unit="kg", position_unit="cm",
                                 unit_basis="operator-declared")
        row = list(csv.DictReader(io.StringIO(output)))[1]
        self.assertEqual(float(row["airframe_mass_kg"]), 1.5)
        self.assertEqual(float(row["airframe_com_x_m"]), 1.0)
        self.assertEqual(float(row["airframe_com_y_m"]), -0.2)
        self.assertEqual(row["airframe_com_x"], "100")   # the declared figure still stands
        self.assertIn("declared_against_sdf_version=1.11", row["airframe_unit_declaration"])
        self.assertIn("basis=operator-declared", row["airframe_unit_declaration"])
        self.assertIn("never_a_default", row["airframe_unit_declaration"])
        for mass, position, basis in (("kg", None, None), (None, None, "operator-declared"),
                                      ("g", None, "operator-declared"),
                                      ("lb", None, "operator-declared"),
                                      (None, "kg", "operator-declared"),
                                      (None, "deg", "operator-declared"),
                                      ("kg", None, "because-i-say-so")):
            with self.subTest(mass=mass, position=position), self.assertRaises(ValueError):
                convert(document, 555, mass_unit=mass, position_unit=position, unit_basis=basis)

    def test_only_versions_whose_definitions_were_read_are_accepted(self):
        """A version name alone never qualifies compatibility, and a later version's pose meaning
        is never inherited by an older document."""
        def pose(version, attribute=""):
            return (f"<sdf version='{version}'><model name='p'><link name='a'><inertial>"
                    f"<pose{attribute}>0 0 0 0 0 0</pose></inertial></link></model></sdf>").encode()

        rows, _ = self.rows(pose("1.5"))
        self.assertIn("NOT_STATED_BY_THE_DECLARED_VERSIONS_OWN_POSE_DEFINITION",
                      rows[1]["airframe_com_rotation_unit"])
        self.assertIn("NO_LATER_VERSIONS_DEFAULT_IS_INHERITED",
                      rows[1]["airframe_com_rotation_unit"])
        rows, _ = self.rows(pose("1.11"))
        self.assertIn("RADIANS_BY_THE_DECLARED_VERSIONS_OWN_STATED_DEFAULT",
                      rows[1]["airframe_com_rotation_unit"])
        rows, _ = self.rows(pose("1.5", " frame='base'"))
        self.assertEqual(bytes.fromhex(rows[1]["airframe_com_pose_frame_hex"][4:]).decode(), "base")
        self.assertIn("FRAME_NAMED_BY_THE_POSE_FRAME_ATTRIBUTE", rows[1]["airframe_com_frame_basis"])
        for version, attribute in (("1.5", " degrees='true'"), ("1.5", " rotation_format='euler_rpy'"),
                                   ("1.11", " frame='base'")):
            with self.subTest(version=version, attribute=attribute), self.assertRaises(ValueError):
                convert(pose(version, attribute), 555)
        for unread in ("1.9", "1.12", "1.4", "2.0", "latest", ""):
            with self.subTest(version=unread), self.assertRaises(ValueError):
                convert(pose(unread), 555)

    def test_an_officially_distributed_fixed_wing_model_is_read_when_supplied(self):
        """Applicability against a real official model, when an operator supplies one.

        The model is not bundled: it belongs to its own project under its own licence, and this
        repository fetches nothing. Without it the check is NOT_RUN by name, never a silent pass.
        """
        supplied = os.environ.get("MUSUBI_SDF_AIRFRAME_MODEL")
        if not supplied:
            self.skipTest("MUSUBI_SDF_AIRFRAME_MODEL not supplied; the official-model check is "
                          "NOT_RUN, not passed")
        document = Path(supplied).read_bytes()
        rows, report = self.rows(document)
        self.assertIn(report["declared_sdf_version"], report["accepted_sdf_versions"])
        self.assertGreaterEqual(report["declared_links"], 1)
        self.assertEqual(report["declared_root_links"], 1)
        self.assertEqual(len(rows), 1 + report["declared_links"] + report["declared_joints"])
        for row in rows:
            if row["airframe_record_kind"] == "LINK":
                self.assertEqual(row["airframe_mass_disposition"],
                                 "REPORTED" if row["airframe_reported_mass"] else "NOT_REPORTED")

    def test_the_declared_canonical_meaning_reaches_common_output(self):
        """The actual function: declared kg and cm arrive in common observations as canonical
        kilograms and metres, carrying their basis, their version qualification and their frame,
        with absence still absent."""
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        document = model("<link name='body'><inertial><mass>1.5</mass>"
                         "<pose>100 -20 5 0 0 0</pose></inertial></link>"
                         "<link name='sensor'/>"
                         "<joint name='mount' type='fixed'>"
                         "<parent>body</parent><child>sensor</child></joint>")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "model.sdf"
            source.write_bytes(document)
            out = base / "converted"
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_sdf_airframe.py"),
                            str(source), str(out), "--capture-time-us", "555",
                            "--mass-unit", "kg", "--position-unit", "cm",
                            "--unit-basis", "project-documentation-declared"],
                           check=True, capture_output=True)
            self.assertEqual((out / "source.sdf").exists(), False)   # no second copy is invented
            if not reader:
                print("sdf airframe canonical common-reader integration NOT_RUN: reader not "
                      "configured")
                return
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(out / "observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        body = common["observations"][1]["fields"]
        sensor = common["observations"][2]["fields"]
        self.assertEqual(body["airframe_mass_kg"], 1.5)
        self.assertEqual(body["airframe_com_x_m"], 1.0)
        self.assertEqual(body["airframe_com_y_m"], -0.2)
        self.assertEqual(body["airframe_com_z_m"], 0.05)
        self.assertEqual(body["airframe_com_x"], 100)
        self.assertEqual(body["airframe_reported_mass"], 1.5)
        declaration = body["airframe_unit_declaration"]
        self.assertIn("mass=kg", declaration)
        self.assertIn("position=cm", declaration)
        self.assertIn("basis=project-documentation-declared", declaration)
        self.assertIn("declared_against_sdf_version=1.11", declaration)
        self.assertIn("CENTRE_OF_MASS_FRAME_C_RELATIVE_TO_THE_LINK_FRAME_L",
                      body["airframe_com_frame_basis"])
        self.assertIn("DECLARED_VERSIONS_OWN_DEFINITION", body["airframe_com_frame_basis"])
        self.assertIsNone(sensor["airframe_mass_kg"])
        self.assertIsNone(sensor["airframe_com_x_m"])
        self.assertIsNone(sensor["airframe_reported_mass"])
        self.assertEqual(sensor["airframe_mass_disposition"], "NOT_REPORTED")
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_values_that_cannot_survive_the_journey_are_refused_before_conversion(self):
        """The token pattern admits 1e999 and 1e-999; written out, they reach common output as a
        non-number or as a false zero. They are refused here instead, and the scaled position is
        checked after its conversion as well as before."""
        def link(body):
            return model(f"<link name='a'><inertial>{body}</inertial></link>")

        for overflowing in ("1e999", "-1e999", "1e400"):
            with self.subTest(mass=overflowing), self.assertRaises(ValueError):
                convert(link(f"<mass>{overflowing}</mass>"), 555)
        for underflowing in ("1e-999", "-1e-999"):
            with self.subTest(mass=underflowing), self.assertRaises(ValueError):
                convert(link(f"<mass>{underflowing}</mass>"), 555)
        for bad in ("1e999", "1e-999"):
            with self.subTest(position=bad), self.assertRaises(ValueError):
                convert(link(f"<pose>{bad} 0 0 0 0 0</pose>"), 555)
        document = link("<pose>1e-322 0 0 0 0 0</pose>")
        rows, _ = self.rows(document)                      # fine with nothing declared
        self.assertEqual(rows[1]["airframe_com_x"], "1e-322")
        with self.assertRaises(ValueError):
            convert(document, 555, position_unit="mm", unit_basis="operator-declared")
        rows, _ = self.rows(link("<pose>0 0 0 0 0 0</pose>"))
        self.assertEqual(rows[1]["airframe_com_x"], "0")

    def test_a_mass_that_is_not_positive_is_qualified_rather_than_presented_as_valid(self):
        for value in ("-2", "0", "-0.0"):
            with self.subTest(mass=value):
                rows, report = self.rows(model(
                    f"<link name='a'><inertial><mass>{value}</mass></inertial></link>"))
                row = rows[1]
                self.assertEqual(row["airframe_reported_mass"], value)
                self.assertEqual(row["airframe_mass_disposition"],
                                 "REPORTED_NOT_A_POSITIVE_VALUE_RETAINED_AND_NOT_PRESENTED_AS_A_"
                                 "CANONICAL_MASS")
                self.assertNotEqual(row["airframe_mass_disposition"], "NOT_REPORTED")
                self.assertEqual(report["links_with_a_non_positive_declared_mass"], 1)
                output, _ = convert(model(f"<link name='a'><inertial><mass>{value}</mass>"
                                          "</inertial></link>"), 555, mass_unit="kg",
                                    unit_basis="operator-declared")
                self.assertEqual(list(csv.DictReader(io.StringIO(output)))[1]["airframe_mass_kg"],
                                 "")

    def test_a_repeated_selected_element_is_refused_rather_than_silently_taking_the_first(self):
        for broken in (
            model("<link name='a'><inertial><mass>1</mass></inertial>"
                  "<inertial><mass>2</mass></inertial></link>"),
            model("<link name='a'><inertial><mass>1</mass><mass>2</mass></inertial></link>"),
            model("<link name='a'><inertial><pose>0 0 0 0 0 0</pose>"
                  "<pose>1 1 1 0 0 0</pose></inertial></link>"),
            model("<link name='a'/><link name='b'/><joint name='j' type='fixed'>"
                  "<parent>a</parent><parent>b</parent><child>b</child></joint>"),
            model("<link name='a'/><link name='b'/><joint name='j' type='fixed'>"
                  "<parent>a</parent><child>b</child><child>a</child></joint>"),
        ):
            with self.subTest(document=broken[:70]), self.assertRaises(ValueError):
                convert(broken, 555)

    def test_duplicate_cyclic_and_misdeclared_documents_are_refused(self):
        for broken in (
            model("<link name='a'/><link name='a'/>"),                       # duplicate link
            model("<link name='a'/><link name='b'/>"
                  "<joint name='j' type='fixed'><parent>a</parent><child>b</child></joint>"
                  "<joint name='j' type='fixed'><parent>b</parent><child>a</child></joint>"),
            model("<link name='a'/><link name='b'/>"                          # two parents
                  "<joint name='j' type='fixed'><parent>a</parent><child>b</child></joint>"
                  "<joint name='k' type='fixed'><parent>a</parent><child>b</child></joint>"),
            model("<link name='a'/><link name='b'/>"                          # cycle
                  "<joint name='j' type='fixed'><parent>a</parent><child>b</child></joint>"
                  "<joint name='k' type='fixed'><parent>b</parent><child>a</child></joint>"),
            model("<link name='a'/><joint name='j' type='fixed'>"             # world as child
                  "<parent>a</parent><child>world</child></joint>"),
            model("<link name='a'><inertial><mass>heavy</mass></inertial></link>"),
            model("<link name='a'><inertial><pose rotation_format='quat_xyzw'>"
                  "0 0 0 0 0 0 1</pose></inertial></link>"),                  # not euler
            model("<link name='a'><inertial><pose degrees='yes'>0 0 0 0 0 0</pose>"
                  "</inertial></link>"),
            model("<link name='a'><inertial><pose>0 0 0 0 0</pose></inertial></link>"),
            model("<link name='a'/>", version="latest"),                      # unstated version
            model("<joint name='j' type='fixed'><parent>a</parent><child>b</child></joint>"),
            b"<sdf version='1.11'><model name='a'/><model name='b'/></sdf>",
            b"<robot name='a'><link name='b'/></robot>",                      # URDF is not read
            b"<sdf version='1.11'><model name='plane'><link/></model></sdf>",
            b"", b"<sdf version='1.11'>", b"\xff\xfe",
        ):
            with self.subTest(document=broken[:60]), self.assertRaises(
                    (ValueError, ET.ParseError, UnicodeDecodeError)):
                convert(broken, 555)
        for capture in (-1, 1.5, True, None):
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                convert(model(LINKS), capture)

    def test_the_declared_structure_reaches_common_output_without_a_unit_declaration(self):
        """Structure only. With no unit declared this proves the rows arrive and that the
        retained figures are retained -- it is NOT evidence of canonical mass or geometry
        meaning, which the declared-unit test below is for."""
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "model.sdf"
            source.write_bytes(model(LINKS + JOINTS))
            out = base / "converted"
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_sdf_airframe.py"),
                            str(source), str(out), "--capture-time-us", "555"],
                           check=True, capture_output=True)
            self.assertEqual(json.loads((out / "report.json").read_text())["declared_links"], 3)
            self.assertNotEqual(0, subprocess.run(
                [os.sys.executable, str(ROOT / "scripts/convert_sdf_airframe.py"),
                 str(source), str(out), "--capture-time-us", "555"],
                capture_output=True).returncode)
            if not reader:
                print("sdf airframe common-reader integration NOT_RUN: reader not configured")
                return
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(out / "observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 6)
        onboard = common["observations"]
        self.assertEqual(onboard[1]["fields"]["airframe_reported_mass"], 1.5)
        self.assertEqual(onboard[1]["fields"]["airframe_link_child_count"], 2)
        self.assertEqual(onboard[3]["fields"]["airframe_mass_disposition"], "NOT_REPORTED")
        self.assertEqual(onboard[3]["fields"]["airframe_reported_mass"], None)  # absent, not zero
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in onboard))
        self.assertEqual(common["platform_domain"], "Unknown")
        units = common["profile_units"]
        self.assertIn("never_a_weighed_one", units["airframe_reported_mass"])
        self.assertIn("never_written_as_zero", units["airframe_mass_disposition"])
        self.assertIn("never_evidence_that_such_a_part_was_built", units["airframe_link_name_hex"])
        self.assertIn("NOTHING_IS_SIMULATED_FLOWN_ACTUATED_OR_MEASURED", BASIS)


if __name__ == "__main__":
    unittest.main()
