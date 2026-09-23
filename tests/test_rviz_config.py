"""Authored saved RViz configurations; no real or vendor configuration, screenshot or log is used.

Every fixture here is written in this file against the pinned RViz2 11.2.3 writer schema
(eedb2acca2b79cb8af75f22f9852692ee11f2b78). Nothing is executed: no RViz, no ROS graph, no subscription.
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "scripts/convert_rviz_config.py"
PROFILE = ROOT / "profiles/declared/rviz-config/rviz-config-profile.toml"
CAPTURE = 1_789_000_000_000_000


def load():
    spec = importlib.util.spec_from_file_location("convert_rviz_config", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["convert_rviz_config"] = module
    spec.loader.exec_module(module)
    return module


LAYOUT_A = """\
Panels:
  - Class: rviz_common/Displays
    Name: Displays
    Property Tree Widget:
      Expanded: ~
Visualization Manager:
  Class: ""
  Displays:
    - Alpha: 0.5
      Class: rviz_default_plugins/Grid
      Enabled: true
      Name: Grid
      Plane Cell Count: 10
    - Class: rviz_common/Group
      Displays:
        - Class: rviz_default_plugins/LaserScan
          Enabled: true
          Name: Front Scan
          Size (m): 0.02
          Topic:
            Depth: 5
            Durability Policy: Volatile
            History Policy: Keep Last
            Reliability Policy: Best Effort
            Value: /front/scan
        - Class: rviz_default_plugins/PointCloud2
          Enabled: false
          Name: Grid
          Topic:
            Depth: 10
            Durability Policy: Transient Local
            History Policy: Keep All
            Reliability Policy: Reliable
            Value: /points
      Enabled: true
      Name: Sensors
    - Class: rviz_default_plugins/Image
      Enabled: true
      Name: Camera
      Topic: /camera/image_raw
      Unknown Future Setting:
        Nested: 42
  Global Options:
    Background Color: 48; 48; 48
    Fixed Frame: base_link
    Frame Rate: 30
  Tools:
    - Class: rviz_default_plugins/MoveCamera
Window Geometry:
  Height: 846
  Width: 1200
"""

LAYOUT_B = """\
Visualization Manager:
  Displays:
    - Class: rviz_default_plugins/TF
      Enabled: false
      Name: TF
      Show Names: true
    - Class: rviz_common/Group
      Displays:
        - Class: rviz_common/Group
          Displays:
            - Class: rviz_default_plugins/Path
              Enabled: true
              Name: Planned Path
              Topic:
                Depth: 1
                Value: /plan
          Enabled: true
          Name: Inner
        - Name: Unnamed Class
      Enabled: true
      Name: Outer
  Global Options:
    Fixed Frame: odom
  Views:
    Current:
      Class: rviz_default_plugins/Orbit
      Target Frame: ""
"""


LAYOUT_C = """\
Visualization Manager:
  Displays:
    - Class: 42
      Enabled: true
      Name: Numeric Class
      Topic:
        Depth: 7
        History Policy: Keep Some
        Value: /looks/like/a/topic
    - Class: vendor/Odd, Name
      Enabled: true
      Name: Comma Class
      Topic: /flat/lookalike
    - Class: rviz_default_plugins/Path
      Enabled: true
      Name: Qualified Flat
      Topic: /plan
  Global Options:
    Fixed Frame: base_link
    Future Option:
      Items:
        - a
        - b
      Nested Choice: 0100
"""

class RvizConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rviz = load()

    def rows_of(self, text, capture=CAPTURE):
        output, report = self.rviz.convert(text.encode(), capture)
        rows = list(csv.DictReader(io.StringIO(output)))
        for row in rows:  # decode the lossless transport back to the authored strings
            for column in ("display_path_hex", "display_name_hex", "display_class_hex",
                           "setting_key_hex", "setting_key_path_hex", "setting_value_hex"):
                value = row[column]
                row[column[:-len("_hex")]] = (bytes.fromhex(value[4:]).decode()
                                              if value.startswith("hex:") else "")
        return rows, report

    def refusal(self, text):
        with self.assertRaises(self.rviz.Refused) as caught:
            self.rviz.convert(text.encode() if isinstance(text, str) else text, CAPTURE)
        return caught.exception.code


    def test_whole_path_declarations_scopes_and_order(self):
        rows, report = self.rows_of(LAYOUT_A)
        self.assertEqual(report["pin"], "rviz2-11.2.3-eedb2ac")
        self.assertEqual(report["displays"], 5)
        self.assertEqual(report["groups"], 1)
        self.assertEqual(report["max_depth"], 2)
        self.assertEqual(report["fixed_frame_state"], "DECLARED")
        self.assertEqual(report["declaration_rows"], len(rows))
        self.assertEqual(report["clock"], "Unknown")
        self.assertIn("NOT_SIMULATION_TIME", report["clock_basis"])
        self.assertTrue(all(r["record_time_us"] == str(CAPTURE) for r in rows))

        frame = [r for r in rows if r["setting_key"] == "Fixed Frame"]
        self.assertEqual([r["setting_value"] for r in frame], ["base_link"])
        self.assertEqual(frame[0]["declaration_kind"], "rviz_global_option")
        self.assertEqual(frame[0]["scope_kind"], "global_options")
        self.assertEqual(frame[0]["setting_key_path"], "Visualization Manager/Global Options/Fixed Frame")
        self.assertTrue(frame[0]["row_basis"].startswith("DECLARED_FRAME_NAME_ONLY_NOT_A_TRANSFORM"))

        displays = [r for r in rows if r["declaration_kind"] in ("rviz_display", "rviz_display_group")]
        self.assertEqual([(r["display_index_path"], r["display_class"], r["display_name"]) for r in displays],
                         [("/1", "rviz_default_plugins/Grid", "Grid"),
                          ("/2", "rviz_common/Group", "Sensors"),
                          ("/2/1", "rviz_default_plugins/LaserScan", "Front Scan"),
                          ("/2/2", "rviz_default_plugins/PointCloud2", "Grid"),
                          ("/3", "rviz_default_plugins/Image", "Camera")])
        self.assertEqual([r["display_path"] for r in displays if r["display_index_path"] == "/2/1"],
                         ["2:Sensors/1:Front Scan"])
        self.assertEqual([r["declaration_kind"] for r in displays if r["display_name"] == "Sensors"],
                         ["rviz_display_group"])
        self.assertEqual([r["display_depth"] for r in displays], ["1", "1", "2", "2", "1"])
        self.assertEqual([r["display_name_occurrence"] for r in displays if r["display_name"] == "Grid"],
                         ["1", "2"])
        enabled = {r["display_index_path"]: (r["display_enabled"], r["display_enabled_state"])
                   for r in displays}
        self.assertEqual(enabled["/1"], ("1", "DECLARED"))
        self.assertEqual(enabled["/2/2"], ("0", "DECLARED"))
        self.assertTrue(all("ENABLED_IS_A_SAVED_UI_SETTING" in r["row_basis"] for r in displays))
        self.assertTrue(all("NOT_A_DEVICE_IDENTITY" in r["row_basis"] for r in displays))

    def test_topic_and_qos_declarations(self):
        rows, report = self.rows_of(LAYOUT_A)
        self.assertEqual(report["topic_rows"], 2)
        self.assertEqual(report["qos_rows"], 8)
        topics = [r for r in rows if r["declaration_kind"] == "rviz_display_topic"]
        self.assertEqual([(r["display_index_path"], r["setting_value"], r["topic_form"]) for r in topics],
                         [("/2/1", "/front/scan", "NESTED_TOPIC_VALUE"),
                          ("/2/2", "/points", "NESTED_TOPIC_VALUE")])
        image = next(r for r in rows if r["setting_key_path"]
                     == "Visualization Manager/Displays/3/Topic")
        self.assertEqual((image["declaration_kind"], image["setting_value"], image["topic_form"]),
                         ("rviz_display_property", "/camera/image_raw", ""))
        self.assertTrue(image["row_basis"].startswith("PROPERTY_NAMED_TOPIC_UNDER_A_CLASS"))
        self.assertEqual(report["unqualified_topic_lookalike_properties"], 1)
        qualification = {r["display_index_path"]: r["class_topic_layout_basis"] for r in rows
                         if r["declaration_kind"] in ("rviz_display", "rviz_display_group")}
        self.assertTrue(qualification["/2/1"].startswith("DECLARED_CLASS_INHERITS_THE_PINNED"))
        self.assertTrue(qualification["/2/2"].startswith("DECLARED_CLASS_INHERITS_THE_PINNED"))
        for unqualified in ("/1", "/2", "/3"):
            self.assertTrue(qualification[unqualified].startswith("DECLARED_CLASS_IS_NOT_SOURCE_QUALIFIED"),
                            unqualified)
        self.assertIn("rviz_default_plugins/LaserScan", report["topic_layout_qualified_classes"])
        self.assertIn("rviz_default_plugins/Image", report["topic_layout_not_qualified_classes"])
        self.assertTrue(all("NOT_EVIDENCE_THAT_ANYTHING_PUBLISHED" in r["row_basis"] for r in topics))
        self.assertEqual([r["setting_key_path"] for r in topics if r["display_index_path"] == "/2/1"],
                         ["Visualization Manager/Displays/2/Displays/1/Topic/Value"])

        qos = [r for r in rows if r["declaration_kind"] == "rviz_display_qos"]
        scan = {r["qos_setting"]: (r["setting_value"], r["qos_option"], r["value_kind"])
                for r in qos if r["display_index_path"] == "/2/1"}
        self.assertEqual(scan["DEPTH"], ("5", "", "integer"))
        self.assertEqual(scan["HISTORY_POLICY"], ("Keep Last", "KEEP_LAST", "enumeration"))
        self.assertEqual(scan["RELIABILITY_POLICY"], ("Best Effort", "BEST_EFFORT", "enumeration"))
        self.assertEqual(scan["DURABILITY_POLICY"], ("Volatile", "VOLATILE", "enumeration"))
        cloud = {r["qos_setting"]: r["qos_option"] for r in qos if r["display_index_path"] == "/2/2"}
        self.assertEqual(cloud, {"DEPTH": "", "HISTORY_POLICY": "KEEP_ALL",
                                 "RELIABILITY_POLICY": "RELIABLE",
                                 "DURABILITY_POLICY": "TRANSIENT_LOCAL"})
        self.assertEqual([r["value_number"] for r in qos if r["qos_setting"] == "DEPTH"], ["5", "10"])
        self.assertEqual([r for r in qos if r["display_index_path"] == "/3"], [])
        self.assertIn("initial option", " ".join(report["not_established_by_this_pin"]))

    def test_uninterpreted_settings_stay_reference_linked(self):
        rows, report = self.rows_of(LAYOUT_A)
        paths = {r["setting_key_path"]: r for r in rows}
        self.assertEqual(paths["Visualization Manager/Displays/3/Unknown Future Setting/Nested"]
                         ["setting_value"], "42")
        self.assertEqual(paths["Visualization Manager/Displays/3/Unknown Future Setting/Nested"]
                         ["value_kind"], "declared_scalar_untyped")
        self.assertTrue(paths["Visualization Manager/Displays/3/Unknown Future Setting/Nested"]
                        ["row_basis"].startswith("RETAINED_AS_DECLARED"))
        self.assertEqual(paths["Visualization Manager/Displays/1/Alpha"]["setting_value"], "0.5")
        self.assertEqual(paths["Visualization Manager/Displays/1/Alpha"]["display_index_path"], "/1")
        self.assertEqual(paths["Panels"]["declaration_kind"], "rviz_root_section")
        self.assertEqual(paths["Window Geometry/Height"]["setting_value"], "846")
        self.assertEqual(paths["Panels/1/Class"]["setting_value"], "rviz_common/Displays")
        self.assertEqual(paths["Panels/1/Property Tree Widget/Expanded"]["value_kind"], "null")
        self.assertEqual(paths["Visualization Manager/Tools/1/Class"]["setting_value"],
                         "rviz_default_plugins/MoveCamera")
        self.assertEqual(report["root_sections"], 3)
        self.assertGreater(report["retained_property_rows"], 10)
        self.assertIn("MAPPING_KEY_ORDER_IS_THE_WRITERS_SORTED_ORDER", report["key_order_basis"])
        for row in rows:
            self.assertLessEqual(int(row["source_line"]), int(row["source_line_end"]))
            self.assertLessEqual(int(row["source_line_end"]), len(LAYOUT_A.splitlines()) + 1)

    def test_the_second_authored_layout_reuses_the_same_importer(self):
        rows, report = self.rows_of(LAYOUT_B)
        self.assertEqual(report["displays"], 5)
        self.assertEqual(report["groups"], 2)
        self.assertEqual(report["max_depth"], 3)
        self.assertEqual(report["fixed_frame_state"], "DECLARED")
        displays = [r for r in rows if r["declaration_kind"] in ("rviz_display", "rviz_display_group")]
        self.assertEqual([r["display_index_path"] for r in displays],
                         ["/1", "/2", "/2/1", "/2/1/1", "/2/2"])
        self.assertEqual([r["display_path"] for r in displays if r["display_index_path"] == "/2/1/1"],
                         ["2:Outer/1:Inner/1:Planned Path"])
        unnamed = next(r for r in displays if r["display_index_path"] == "/2/2")
        self.assertEqual((unnamed["display_class"], unnamed["display_class_state"]),
                         ("", "NOT_DECLARED_IN_THIS_FILE"))
        self.assertEqual((unnamed["display_enabled"], unnamed["display_enabled_state"]),
                         ("", "NOT_DECLARED_IN_THIS_FILE"))
        self.assertEqual([r["setting_value"] for r in rows
                          if r["setting_key_path"] == "Visualization Manager/Global Options/Fixed Frame"],
                         ["odom"])
        empty = next(r for r in rows if r["setting_key_path"] == "Visualization Manager/Views/Current/Target Frame")
        self.assertEqual((empty["setting_value"], empty["value_kind"]), ("", "empty_string"))
        self.assertNotEqual(report["source_sha256"], self.rows_of(LAYOUT_A)[1]["source_sha256"])

    def test_unknown_global_options_and_class_literals_are_retained_exactly(self):
        rows, report = self.rows_of(LAYOUT_C)
        numeric = next(r for r in rows if r["display_index_path"] == "/1")
        self.assertEqual(numeric["display_class"], "42")
        self.assertEqual(numeric["display_class_hex"], "hex:" + "42".encode().hex())
        self.assertEqual(numeric["display_class_state"], "DECLARED")
        comma = next(r for r in rows if r["display_index_path"] == "/2")
        self.assertEqual(comma["display_class"], "vendor/Odd, Name")
        paths = {r["setting_key_path"]: r for r in rows}
        option = "Visualization Manager/Global Options/Future Option"
        self.assertEqual(paths[option]["value_kind"], "mapping")
        self.assertEqual(paths[option]["scope_kind"], "global_options")
        self.assertEqual(paths[f"{option}/Nested Choice"]["setting_value"], "0100")
        self.assertEqual(paths[f"{option}/Items"]["value_kind"], "sequence")
        self.assertEqual(paths[f"{option}/Items/2"]["setting_value"], "b")
        self.assertTrue(paths[f"{option}/Nested Choice"]["row_basis"].startswith("RETAINED_AS_DECLARED"))
        frame = paths["Visualization Manager/Global Options/Fixed Frame"]
        self.assertEqual((frame["declaration_kind"], frame["setting_value"]),
                         ("rviz_global_option", "base_link"))
        self.assertEqual(report["fixed_frame_state"], "DECLARED")
        self.assertEqual(report["global_options"], 1, "only scalar options are global-option rows")
        self.assertEqual(self.refusal("Visualization Manager:\n  Displays:\n    - Class: A\n"
                                      "  Global Options:\n    Fixed Frame:\n      a: 1\n"),
                         "GLOBAL_OPTION_MUST_BE_A_SCALAR")

    def test_a_topic_lookalike_under_an_unqualified_class_is_retained_not_interpreted(self):
        rows, report = self.rows_of(LAYOUT_C)
        paths = {r["setting_key_path"]: r for r in rows}
        lookalike = "Visualization Manager/Displays/1/Topic"
        self.assertEqual(paths[lookalike]["value_kind"], "mapping")
        self.assertEqual(paths[f"{lookalike}/Value"]["setting_value"], "/looks/like/a/topic")
        self.assertEqual(paths[f"{lookalike}/Depth"]["setting_value"], "7")
        self.assertEqual(paths[f"{lookalike}/Depth"]["value_kind"], "declared_scalar_untyped")
        self.assertEqual(paths[f"{lookalike}/History Policy"]["setting_value"], "Keep Some")
        for key in ("Value", "Depth", "History Policy"):
            row = paths[f"{lookalike}/{key}"]
            self.assertEqual((row["declaration_kind"], row["qos_setting"], row["qos_option"],
                              row["topic_form"]), ("rviz_display_property", "", "", ""))
            self.assertTrue(row["row_basis"].startswith("PROPERTY_NAMED_TOPIC_UNDER_A_CLASS"))
            self.assertEqual(row["source_line"], row["source_line_end"])
        self.assertEqual(paths["Visualization Manager/Displays/2/Topic"]["setting_value"],
                         "/flat/lookalike")
        self.assertEqual(report["unqualified_topic_lookalike_properties"], 2)
        qualified = [r for r in rows if r["declaration_kind"] == "rviz_display_topic"]
        self.assertEqual([(r["display_index_path"], r["setting_value"], r["topic_form"])
                          for r in qualified],
                         [("/3", "/plan", "LEGACY_FLAT_TOPIC_SCALAR")])
        self.assertEqual(report["topic_rows"], 1)
        self.assertEqual(report["qos_rows"], 0)
        self.assertEqual(report["topic_layout_qualified_classes"]["rviz_default_plugins/Path"],
                         "MessageFilterDisplay<nav_msgs::msg::Path>")


    def test_malformed_unsupported_and_ambiguous_inputs_are_refused(self):
        cases = [  # pairs, not a dict: two shapes may legitimately share one refusal code
            ("CONFIGURATION_ROOT_MUST_BE_A_MAPPING", "- just a list\n"),
            ("SAVED_CONFIGURATION_HAS_NO_VISUALIZATION_MANAGER", "Panels: {}\n"),
            ("VISUALIZATION_MANAGER_MUST_BE_A_MAPPING", "Visualization Manager: text\n"),
            ("VISUALIZATION_MANAGER_HAS_NO_DISPLAY_LIST",
             "Visualization Manager:\n  Global Options:\n    Fixed Frame: map\n"),
            ("DISPLAY_LIST_MUST_BE_A_SEQUENCE", "Visualization Manager:\n  Displays: none\n"),
            ("DISPLAY_ENTRY_MUST_BE_A_MAPPING", "Visualization Manager:\n  Displays:\n    - text\n"),
            ("DISPLAY_ENABLED_MUST_BE_THE_WRITERS_TRUE_OR_FALSE",
             "Visualization Manager:\n  Displays:\n    - Class: A\n      Enabled: yes\n"),
            ("DISPLAY_CLASS_MUST_BE_A_SCALAR",
             "Visualization Manager:\n  Displays:\n    - Class: {a: 1}\n"),
            ("GLOBAL_OPTION_MUST_BE_A_SCALAR",
             "Visualization Manager:\n  Displays: []\n  Global Options:\n    Fixed Frame: {a: 1}\n"),
            ("GLOBAL_OPTIONS_MUST_BE_A_MAPPING",
             "Visualization Manager:\n  Displays: []\n  Global Options: map\n"),
            ("QOS_POLICY_OUTSIDE_THE_PINNED_ENUMERATION",
             "Visualization Manager:\n  Displays:\n    - Class: rviz_default_plugins/Path\n"
             "      Topic:\n        History Policy: Keep Some\n        Value: /a\n"),
            ("QOS_DEPTH_OUTSIDE_THE_PINNED_INT_PROPERTY_RANGE",
             "Visualization Manager:\n  Displays:\n    - Class: rviz_default_plugins/Path\n"
             "      Topic:\n        Depth: 0\n        Value: /a\n"),
            ("QOS_DEPTH_OUTSIDE_THE_PINNED_INT_PROPERTY_RANGE",
             "Visualization Manager:\n  Displays:\n    - Class: rviz_default_plugins/Path\n"
             "      Topic:\n        Depth: 2147483648\n        Value: /a\n"),
            ("DUPLICATE_MAPPING_KEY",
             "Visualization Manager:\n  Displays: []\n  Displays: []\n"),
            ("YAML_MERGE_KEY_UNSUPPORTED",
             "Visualization Manager:\n  Displays: []\n  <<: {a: 1}\n"),
            ("YAML_ALIASES_ANCHORS_TAGS_AND_DIRECTIVES_UNSUPPORTED",
             "Visualization Manager: &m\n  Displays: []\nPanels: *m\n"),
            ("YAML_ALIASES_ANCHORS_TAGS_AND_DIRECTIVES_UNSUPPORTED",
             "Visualization Manager:\n  Displays: !!python/object:os.system []\n"),
            ("ONE_CONFIGURATION_DOCUMENT_REQUIRED",
             "Visualization Manager:\n  Displays: []\n---\nPanels: {}\n"),
            ("UNREADABLE_CONFIGURATION_YAML", "Visualization Manager:\n\tDisplays: []\n"),
            ("UNREADABLE_CONFIGURATION_YAML", "Visualization Manager: {Displays: [\n"),
            ("NO_DECLARED_DISPLAY_IN_THE_SELECTED_FILE", "Visualization Manager:\n  Displays: []\n"),
        ]
        for code, text in cases:
            self.assertEqual(self.refusal(text), code, text)
        for capture in (-1, 1.5, "0", None):
            with self.assertRaises(self.rviz.Refused):
                self.rviz.convert(b"Visualization Manager:\n  Displays: []\n", capture)
        for data in (b"", "x" * (self.rviz.LIMIT + 1), b"\xff\xfe: 1\n"):
            with self.assertRaises(self.rviz.Refused):
                self.rviz.convert(data if isinstance(data, bytes) else data.encode(), CAPTURE)
        deep = "Visualization Manager:\n  Displays:\n" + "".join(
            f"{' ' * (4 + index * 4)}- Class: rviz_common/Group\n"
            f"{' ' * (6 + index * 4)}Name: G{index}\n{' ' * (6 + index * 4)}Displays:\n"
            for index in range(self.rviz.MAX_DEPTH + 1))
        self.assertEqual(self.refusal(deep), "NESTING_EXCEEDS_BOUND")

    def test_a_practical_size_configuration_stays_inside_the_bounds(self):
        groups = []
        for group in range(10):
            entries = "".join(
                f"        - Class: rviz_default_plugins/LaserScan\n"
                f"          Enabled: true\n          Name: Scan {group}-{index}\n"
                f"          Topic:\n            Depth: 5\n"
                f"            Durability Policy: Volatile\n            History Policy: Keep Last\n"
                f"            Reliability Policy: Reliable\n            Value: /scan/{group}/{index}\n"
                for index in range(30))
            groups.append(f"    - Class: rviz_common/Group\n      Enabled: true\n"
                          f"      Name: Group {group}\n      Displays:\n{entries}")
        text = ("Visualization Manager:\n  Displays:\n" + "".join(groups)
                + "  Global Options:\n    Fixed Frame: base_link\n")
        rows, report = self.rows_of(text)
        self.assertEqual(report["displays"], 310)
        self.assertEqual(report["groups"], 10)
        self.assertEqual(report["topic_rows"], 300)
        self.assertEqual(report["qos_rows"], 1200)
        self.assertEqual(len(rows), report["declaration_rows"])
        self.assertLess(len(rows), self.rviz.MAX_ROWS)
        self.assertLess(len(json.dumps(report)), self.rviz.REPORT_LIMIT)


    def test_cli_writes_rows_accounting_and_the_retained_source(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "layout.rviz"
            source.write_text(LAYOUT_A, encoding="utf-8")
            target = Path(work) / "out"
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target),
                                   "--capture-time-us", str(CAPTURE)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             ["accounting.json", "observations.csv", "source.rviz"])
            self.assertEqual((target / "source.rviz").read_text(encoding="utf-8"), LAYOUT_A)
            report = json.loads((target / "accounting.json").read_text(encoding="utf-8"))
            self.assertEqual(report["pin"], "rviz2-11.2.3-eedb2ac")
            self.assertEqual(report["clock"], "Unknown")
            again = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target),
                                    "--capture-time-us", str(CAPTURE)],
                                   capture_output=True, text=True, check=False)
            self.assertEqual(again.returncode, 2)
            bad = Path(work) / "bad.rviz"
            bad.write_text("Panels: {}\n", encoding="utf-8")
            refused = subprocess.run([sys.executable, str(SCRIPT), str(bad), str(Path(work) / "no"),
                                      "--capture-time-us", str(CAPTURE)],
                                     capture_output=True, text=True, check=False)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("SAVED_CONFIGURATION_HAS_NO_VISUALIZATION_MANAGER", refused.stderr)
            self.assertFalse((Path(work) / "no").exists())

    def test_a_failure_after_the_directory_exists_says_the_contents_are_incomplete(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "layout.rviz"
            source.write_text(LAYOUT_B, encoding="utf-8")
            target = Path(work) / "partial"
            written = Path.write_text

            def fail_on_the_accounting(self, *arguments, **keywords):
                if self.name == "accounting.json":
                    raise OSError("no space left on device")
                return written(self, *arguments, **keywords)

            stderr = io.StringIO()
            with mock.patch.object(Path, "write_text", fail_on_the_accounting), \
                 mock.patch.object(sys, "argv", ["convert_rviz_config.py", str(source), str(target),
                                                 "--capture-time-us", str(CAPTURE)]), \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
                self.rviz.main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertTrue((target / "observations.csv").exists())
            self.assertIn("incomplete", stderr.getvalue())
            self.assertNotIn("nothing was written", stderr.getvalue())

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the declarations as common Observations")
    def test_class_literals_and_unqualified_structure_reach_common_observations(self):
        output, report = self.rviz.convert(LAYOUT_C.encode(), CAPTURE)
        with tempfile.TemporaryDirectory() as work:
            rows = Path(work) / "observations.csv"
            rows.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], report["declaration_rows"])
        self.assertEqual(common["platform_domain"], "Unknown")

        def authored(fields, column):
            value = fields.get(column) or ""
            return bytes.fromhex(value[4:]).decode() if value.startswith("hex:") else ""

        displays = [o["fields"] for o in common["observations"]
                    if o["fields"]["declaration_kind"] == "rviz_display"]
        self.assertEqual([authored(f, "display_class_hex") for f in displays],
                         ["42", "vendor/Odd, Name", "rviz_default_plugins/Path"])
        self.assertEqual([f["class_topic_layout_basis"][:30] for f in displays],
                         ["DECLARED_CLASS_IS_NOT_SOURCE_Q"] * 2 + ["DECLARED_CLASS_INHERITS_THE_PI"])
        lookalike = [o["fields"] for o in common["observations"]
                     if authored(o["fields"], "setting_key_path_hex")
                     == "Visualization Manager/Displays/1/Topic/Depth"]
        self.assertEqual(len(lookalike), 1)
        self.assertIsNone(lookalike[0]["qos_setting"])
        self.assertEqual(authored(lookalike[0], "setting_value_hex"), "7")
        nested = {authored(o["fields"], "setting_key_path_hex"): authored(o["fields"], "setting_value_hex")
                  for o in common["observations"]
                  if authored(o["fields"], "setting_key_path_hex").startswith(
                      "Visualization Manager/Global Options/Future Option")}
        self.assertEqual(nested["Visualization Manager/Global Options/Future Option/Items/1"], "a")
        self.assertEqual(nested["Visualization Manager/Global Options/Future Option/Nested Choice"],
                         "0100")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "set MUSUBI_TELEMETRY_READER to read the declarations as common Observations")
    def test_declarations_reach_common_observations(self):
        for text, expected_frame in ((LAYOUT_A, "base_link"), (LAYOUT_B, "odom")):
            output, report = self.rviz.convert(text.encode(), CAPTURE)
            with tempfile.TemporaryDirectory() as work:
                rows = Path(work) / "observations.csv"
                rows.write_text(output, encoding="utf-8")
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(rows),
                     "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], report["declaration_rows"])
            self.assertEqual(common["platform_domain"], "Unknown",
                             "no domain is inferred from a saved visualisation configuration")
            first = common["observations"][0]
            self.assertEqual(first["clock_basis"], "Unknown")
            self.assertEqual(first["t_ms"] * 1000, CAPTURE)

            def authored(fields, column):
                value = fields.get(column) or ""
                return bytes.fromhex(value[4:]).decode() if value.startswith("hex:") else ""

            frame = [o["fields"] for o in common["observations"]
                     if o["fields"]["declaration_kind"] == "rviz_global_option"
                     and authored(o["fields"], "setting_key_hex") == "Fixed Frame"]
            self.assertEqual([authored(f, "setting_value_hex") for f in frame], [expected_frame])
            displays = [o["fields"] for o in common["observations"]
                        if o["fields"]["declaration_kind"] in ("rviz_display", "rviz_display_group")]
            self.assertEqual(len(displays), report["displays"])
            self.assertEqual([f["display_index_path"] for f in displays],
                             [r["display_index_path"] for r in self.rows_of(text)[0]
                              if r["declaration_kind"] in ("rviz_display", "rviz_display_group")])
            if text is LAYOUT_A:
                topics = {authored(o["fields"], "setting_value_hex")
                          for o in common["observations"]
                          if o["fields"]["declaration_kind"] == "rviz_display_topic"}
                self.assertEqual(topics, {"/front/scan", "/points"},
                                 "an unqualified class contributes no topic row")
                depth = [o["fields"] for o in common["observations"]
                         if o["fields"]["qos_setting"] == "DEPTH"]
                self.assertEqual([f["value_number"] for f in depth], [5.0, 10.0])
                self.assertEqual({f["qos_option"] for o in common["observations"]
                                  if (f := o["fields"])["qos_setting"] == "HISTORY_POLICY"},
                                 {"KEEP_LAST", "KEEP_ALL"})


if __name__ == "__main__":
    unittest.main()
