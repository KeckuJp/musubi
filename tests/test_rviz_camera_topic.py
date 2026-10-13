"""Saved Camera image settings, not CameraInfo or rendering qualification."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import test_rviz_config as fixture
from tests.test_rviz_image_topic import document


class CameraTopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rviz = fixture.load()

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual reader required")
    def test_two_configurations_reuse_common_without_camera_info_claim(self):
        for setting, topic, count in (
            ("      Topic:\n        Value: /front/image/compressed\n"
             "        Depth: 9\n        Reliability Policy: Reliable\n",
             "/front/image/compressed", 2),
            ("      Topic: /rear/image\n", "/rear/image", 0),
        ):
            source = document(setting, "rviz_default_plugins/Camera")
            output, report = self.rviz.convert(source, fixture.CAPTURE)
            self.assertEqual((report["topic_rows"], report["qos_rows"]), (1, count))
            self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(source).hexdigest())
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "camera.csv"
                path.write_text(output)
                common = json.loads(subprocess.run(
                    [os.environ["MUSUBI_TELEMETRY_READER"], str(fixture.PROFILE), str(path),
                     "--allow-equal-time"], capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], report["declaration_rows"])
            observations = common["observations"]
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in observations))
            selected = [o["fields"] for o in observations
                        if o["fields"]["declaration_kind"] == "rviz_display_topic"]
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["setting_value_hex"], "hex:" + topic.encode().hex())
            self.assertEqual(selected[0]["display_index_path"], "/1")
            self.assertEqual(selected[0]["source_document_sha256"], report["source_sha256"])
            self.assertEqual(selected[0]["class_topic_layout_basis"], self.rviz.TOPIC_LAYOUT_QUALIFIED)
            self.assertEqual(selected[0]["row_basis"], self.rviz.TOPIC_BASIS)
            self.assertTrue(any(o["fields"]["setting_value_hex"] == "hex:" + b"retained".hex()
                                for o in observations))

    def test_camera_validation_and_custom_retention(self):
        for invalid in ("      Topic:\n        Depth: 0\n",
                        "      Topic:\n        History Policy: Sometimes\n",
                        "      Topic: [one, two]\n"):
            with self.assertRaises(self.rviz.Refused):
                self.rviz.convert(document(invalid, "rviz_default_plugins/Camera"), fixture.CAPTURE)
            output, report = self.rviz.convert(document(invalid, "vendor/Camera"), fixture.CAPTURE)
            self.assertEqual((report["topic_rows"], report["qos_rows"]), (0, 0))
            self.assertEqual(report["unqualified_topic_lookalike_properties"], 1)
            self.assertIn(self.rviz.LOOKALIKE_BASIS, output)


if __name__ == "__main__":
    unittest.main()
