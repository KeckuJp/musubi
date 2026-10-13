"""Authored saved bags, not acquisition completeness or real hardware evidence."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from rosbags.rosbag2 import Writer
from scripts.convert_rosbag_observations import STORE, YAML
from scripts.check_saved_topics import check

ROOT = Path(__file__).resolve().parents[1]


class SavedTopicTests(unittest.TestCase):
    def test_direct_frame_edges_reuse_and_refuse(self):
        from scripts.convert_rosbag_observations import TF, LIMIT
        for index, pair in enumerate((("map", "base"), ("odom", "sensor"))):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); bag = root / "bag"
                t = STORE.types
                entry = t["geometry_msgs/msg/TransformStamped"](
                    t["std_msgs/msg/Header"](t["builtin_interfaces/msg/Time"](9, 0), pair[0]), pair[1],
                    t["geometry_msgs/msg/Transform"](t["geometry_msgs/msg/Vector3"](1., 2., 3.),
                        t["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.)))
                with Writer(bag, version=9) as writer:
                    tf = writer.add_connection("/tf", TF, typestore=STORE)
                    extent = writer.add_connection("/extent", "std_msgs/msg/String", typestore=STORE)
                    for stamp in (100, 200):
                        writer.write(extent, stamp, STORE.serialize_cdr(t["std_msgs/msg/String"]("x"), extent.msgtype))
                    payload = STORE.serialize_cdr(t[TF]([entry] * (200 if index else 1)), TF)
                    if index:
                        self.assertGreater(len(payload), 12000)
                    writer.write(tf, 150, payload)
                db = next(bag.glob("*.db3"))
                expected = dict(schema="saved-frame-expectation/v1", clock="BAG_RECORD_TIMESTAMP_NS",
                    database_sha256=hashlib.sha256(db.read_bytes()).hexdigest(),
                    metadata_sha256=hashlib.sha256((bag / "metadata.yaml").read_bytes()).hexdigest(),
                    start_ns=100, stop_ns=201, edges=[dict(topic="/tf", parent=pair[0], child=pair[1]),
                        dict(topic="/tf", parent=pair[1], child=pair[0])])
                rows, text, report = self.run_case(root, bag, expected)
                self.assertEqual([r["disposition"] for r in rows], ["OBSERVED", "NOT_OBSERVED_IN_SAVED_WINDOW"])
                self.assertEqual(report["decoded_tf_records"], 1)
                self.assertEqual(rows[0]["window_transforms"], "200" if index else "1")
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    output = root / "out.csv"; output.write_text(text)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/rosbag-electrical/topic-expectations.toml"),
                        str(output), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), 2)
                    self.assertEqual(common["observations"][0]["fields"]["disposition"], "OBSERVED")
                    self.assertEqual(common["observations"][0]["fields"]["window_transforms"], 200 if index else 1)
                outside = dict(expected, start_ns=201, stop_ns=300)
                self.assertTrue(all(r["disposition"] == "INDETERMINATE_WINDOW_COVERAGE"
                    for r in self.run_case(root, bag, outside)[0]))
                if index == 0:
                    with sqlite3.connect(db) as conn:
                        conn.execute("UPDATE messages SET data=? WHERE timestamp=150", (b"x" * (LIMIT + 1),))
                    expected["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
                    with self.assertRaisesRegex(ValueError, "decode bound"):
                        self.run_case(root, bag, expected)
                    entry.transform.rotation.w = 2.
                    with sqlite3.connect(db) as conn:
                        conn.execute("UPDATE messages SET data=? WHERE timestamp=150", (bytes(STORE.serialize_cdr(t[TF]([entry]), TF)),))
                    expected["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
                    with self.assertRaisesRegex(ValueError, "unit quaternion"):
                        self.run_case(root, bag, expected)
                else:
                    with sqlite3.connect(db) as conn:
                        conn.execute("UPDATE topics SET type_description_hash='wrong' WHERE name='/tf'")
                    expected["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
                    with self.assertRaisesRegex(ValueError, "declarations disagree"):
                        self.run_case(root, bag, expected)
                    metadata = bag / "metadata.yaml"
                    info = YAML(typ="safe").load(metadata.read_bytes())
                    for topic in info["rosbag2_bagfile_information"]["topics_with_message_count"]:
                        if topic["topic_metadata"]["name"] == "/tf":
                            topic["topic_metadata"]["type_description_hash"] = "wrong"
                    with metadata.open("w") as stream:
                        YAML().dump(info, stream)
                    expected["metadata_sha256"] = hashlib.sha256(metadata.read_bytes()).hexdigest()
                    with self.assertRaisesRegex(ValueError, "schema hash"):
                        self.run_case(root, bag, expected)

    def test_empty_frame_expectation_and_wrong_topic_type(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bag, expected = self.make(root, [])
            expected.pop("topics")
            expected.update(schema="saved-frame-expectation/v1", edges=[dict(topic="/tf", parent="map", child="base")])
            rows, _, report = self.run_case(root, bag, expected)
            self.assertEqual(rows[0]["disposition"], "INDETERMINATE_WINDOW_COVERAGE")
            self.assertEqual(report["decoded_tf_records"], 0)
            expected["edges"][0]["topic"] = "/empty"
            with self.assertRaisesRegex(ValueError, "fixed TFMessage"):
                self.run_case(root, bag, expected)

    def test_duplicate_expectation_and_orphan_record_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bag, expected = self.make(root, [("/measured", 150)])
            declaration = root / "expected.json"
            declaration.write_text(json.dumps(expected)[:-1] + ', "start_ns": 0}')
            with self.assertRaises(ValueError):
                check(bag, declaration)
            db = next(bag.glob("*.db3"))
            with sqlite3.connect(db) as connection:
                connection.execute("INSERT INTO messages(topic_id,timestamp,data) VALUES(9999,160,?)", (b"orphan",))
            expected["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "orphan topic"):
                self.run_case(root, bag, expected)

    def make(self, root, records):
        bag = root / "bag"
        typename = "std_msgs/msg/String"
        with Writer(bag, version=9) as writer:
            connections = {name: writer.add_connection(name, typename, typestore=STORE)
                           for name in {name for name, _ in records} | {"/empty"}}
            for name, timestamp in records:
                writer.write(connections[name], timestamp, STORE.serialize_cdr(STORE.types[typename]("saved"), typename))
        info = YAML(typ="safe").load((bag / "metadata.yaml").read_bytes())["rosbag2_bagfile_information"]
        expected = dict(schema="saved-topic-expectation/v1", clock="BAG_RECORD_TIMESTAMP_NS",
            database_sha256=hashlib.sha256((bag / info["relative_file_paths"][0]).read_bytes()).hexdigest(),
            metadata_sha256=hashlib.sha256((bag / "metadata.yaml").read_bytes()).hexdigest(),
            start_ns=100, stop_ns=201, topics=["/measured", "/empty", "/not_declared"])
        return bag, expected

    def run_case(self, root, bag, expected):
        path = root / "expected.json"; path.write_text(json.dumps(expected))
        text, report = check(bag, path)
        return list(csv.DictReader(io.StringIO(text))), text, report

    def test_two_saved_inputs_common_presence_and_uncertainty(self):
        for records in ([('/extent', 100), ('/measured', 150), ('/extent', 200)],
                        [('/measured', 50), ('/extent', 100), ('/extent', 200), ('/measured', 250)]):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); bag, expected = self.make(root, records)
                rows, text, report = self.run_case(root, bag, expected)
                self.assertEqual(rows[0]["disposition"], "OBSERVED" if len(records) == 3 else "NOT_OBSERVED_IN_SAVED_WINDOW")
                self.assertTrue(all(r["disposition"] == "NOT_OBSERVED_IN_SAVED_WINDOW" for r in rows[1:]))
                self.assertEqual(report["source_records"], len(records))
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    output = root / "out.csv"; output.write_text(text)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/rosbag-electrical/topic-expectations.toml"),
                        str(output), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(len(common["observations"]), 3)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    self.assertEqual(common["observations"][0]["fields"]["disposition"], rows[0]["disposition"])
                expected.update(start_ns=0, stop_ns=1000)
                rows, _, _ = self.run_case(root, bag, expected)
                self.assertEqual(rows[1]["disposition"], "INDETERMINATE_WINDOW_COVERAGE")

    def test_empty_digest_count_and_corrupt_are_not_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bag, expected = self.make(root, [])
            rows, _, _ = self.run_case(root, bag, expected)
            self.assertTrue(all(r["disposition"] == "INDETERMINATE_WINDOW_COVERAGE" for r in rows))
            for change in ({"clock": "UTC"}, {"database_sha256": "0" * 64}, {"topics": ["/a", "/a"]}):
                with self.assertRaises(ValueError):
                    self.run_case(root, bag, dict(expected, **change))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); bag, expected = self.make(root, [("/measured", 150)])
            db = next(bag.glob("*.db3"))
            with sqlite3.connect(db) as connection:
                connection.execute("DELETE FROM messages")
            expected["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                self.run_case(root, bag, expected)
            db.write_bytes(b"not a sqlite database")
            expected["database_sha256"] = hashlib.sha256(db.read_bytes()).hexdigest()
            with self.assertRaises(Exception):
                self.run_case(root, bag, expected)
