"""Authored RViz2 11.2.3 Image declarations, no ROS or image transport execution."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import test_rviz_config as fixture


def document(topic, class_id="rviz_default_plugins/Image"):
    return ("Visualization Manager:\n  Displays:\n"
            f"    - Class: {class_id}\n      Name: Saved Image\n      Enabled: false\n"
            + topic + "      Future Image Setting: retained\n").encode()


NESTED = document("      Topic:\n        Value: /camera/image_raw/compressed\n"
                  "        Depth: 7\n        History Policy: Keep Last\n"
                  "        Reliability Policy: Best Effort\n        Durability Policy: Volatile\n")
FLAT = document("      Topic: /another/image_raw\n")


class RvizImageTopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rviz = fixture.load()

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_two_saved_image_declarations_reach_common(self):
        for source, topic, qos_count, form in (
                (NESTED, "/camera/image_raw/compressed", 4, "NESTED_TOPIC_VALUE"),
                (FLAT, "/another/image_raw", 0, "LEGACY_FLAT_TOPIC_SCALAR")):
            with self.subTest(form=form):
                output, report = self.rviz.convert(source, fixture.CAPTURE)
                self.assertEqual((report["topic_rows"], report["qos_rows"]), (1, qos_count))
                self.assertEqual(report["source_bytes"], len(source))
                self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(source).hexdigest())
                with tempfile.TemporaryDirectory() as work:
                    path = Path(work) / "rows.csv"
                    path.write_text(output)
                    common = json.loads(subprocess.run(
                        [os.environ["MUSUBI_TELEMETRY_READER"], str(fixture.PROFILE), str(path),
                         "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], report["declaration_rows"])
                self.assertEqual(common["platform_domain"], "Unknown")
                observations = common["observations"]
                self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
                self.assertTrue(all(o["t_ms"] * 1000 == fixture.CAPTURE for o in observations))
                rows = [o["fields"] for o in observations]
                selected = [r for r in rows if r["declaration_kind"] == "rviz_display_topic"]
                self.assertEqual(len(selected), 1)
                value = selected[0]
                self.assertEqual(value["setting_value_hex"], "hex:" + topic.encode().hex())
                self.assertEqual(value["display_index_path"], "/1")
                self.assertEqual(value["topic_form"], form)
                self.assertEqual(value["row_basis"], self.rviz.TOPIC_BASIS)
                self.assertGreater(value["source_line"], 0)
                self.assertEqual(value["source_document_sha256"], report["source_sha256"])
                self.assertEqual(value["class_topic_layout_basis"], self.rviz.TOPIC_LAYOUT_QUALIFIED)
                qos = [r for r in rows if r["declaration_kind"] == "rviz_display_qos"]
                self.assertEqual(len(qos), qos_count)
                if qos:
                    self.assertEqual({r["qos_option"] for r in qos},
                                     {None, "KEEP_LAST", "BEST_EFFORT", "VOLATILE"})
                    self.assertEqual([r["value_number"] for r in qos if r["qos_setting"] == "DEPTH"], [7.0])
                    self.assertTrue(all(r["row_basis"] == self.rviz.QOS_BASIS for r in qos))
                self.assertTrue(any(r["setting_value_hex"] == "hex:" + b"retained".hex() for r in rows))
                display = next(r for r in rows if r["declaration_kind"] == "rviz_display")
                self.assertEqual(display["display_enabled"], 0.0)

    def test_qualified_validation_does_not_qualify_custom_lookalikes(self):
        for invalid in ("      Topic:\n        Depth: 0\n",
                        "      Topic:\n        Reliability Policy: Sometimes\n",
                        "      Topic: [one, two]\n"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(self.rviz.Refused):
                    self.rviz.convert(document(invalid), fixture.CAPTURE)
                for class_id in ("vendor/Image", "vendor/Camera"):
                    output, report = self.rviz.convert(document(invalid, class_id), fixture.CAPTURE)
                    self.assertEqual((report["topic_rows"], report["qos_rows"]), (0, 0))
                    self.assertEqual(report["unqualified_topic_lookalike_properties"], 1)
                    self.assertIn(self.rviz.LOOKALIKE_BASIS, output)

    def test_non_image_csv_is_byte_unchanged(self):
        output, _ = self.rviz.convert(fixture.LAYOUT_B.encode(), fixture.CAPTURE)
        self.assertEqual(hashlib.sha256(output.encode()).hexdigest(),
                         "a57302c41b11acc47b130b4d4755e6742676dd2f825e03bc8b9594e72e86f207")


if __name__ == "__main__":
    unittest.main()
