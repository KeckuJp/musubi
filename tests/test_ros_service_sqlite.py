"""Authored fixed service SQLite → existing MCAP semantics, no ROS service calls."""
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
from unittest.mock import patch

from tests import test_rosbag_observations as fixture
from scripts import convert_ros_service_sqlite as bridge

ROOT = Path(__file__).resolve().parents[1]


def pair(gid=fixture.GID_A, sequence=1, value=25.5):
    return [fixture.service_event(0, gid, sequence, names=["saved_gain"]),
            fixture.service_event(2, gid, sequence,
                values=[fixture.parameter_value(type=3, double_value=value)])]


def bag(path, messages=None):
    fixture.service_bag(path, pair() if messages is None else messages,
                        plugin=fixture.StoragePlugin.SQLITE3)
    return next(path.glob("*.db3"))


def mutate(path, sql):
    with sqlite3.connect(next(path.glob("*.db3"))) as db:
        db.executescript(sql)


def common(test, target, data):
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    if not reader:
        test.skipTest("actual common reader not configured")
    target.write_bytes(data)
    return json.loads(subprocess.run([reader,
        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
        str(target), "--allow-equal-time"], check=True, capture_output=True).stdout)


class ServiceSqliteTests(unittest.TestCase):
    def test_two_sources_typed_values_and_mcap_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (gid, value) in enumerate(((fixture.GID_A, 25.5), (fixture.GID_B, -1.25))):
                source = root / f"s{index}"
                dbpath = bag(source, pair(gid, index + 1, value))
                original = dbpath.read_bytes()
                outputs, report = bridge.convert(source, topic=fixture.SERVICE_TOPIC)
                rows = list(csv.DictReader(io.StringIO(outputs["observations.csv"].decode())))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["service_transaction_status"], "MATCHED")
                self.assertEqual(float(rows[0]["parameter_double_reported"]), value)
                self.assertEqual(outputs["source.db3"], original)
                self.assertEqual(report["source_sha256"], hashlib.sha256(original).hexdigest())
                self.assertEqual([r["source_message_id"] for r in report["messages"]], [1, 2])
                self.assertEqual([r["derived_index"] for r in report["messages"]], [0, 1])
                with sqlite3.connect(dbpath) as db:
                    raw = db.execute("SELECT timestamp,data FROM messages ORDER BY id").fetchall()
                for item, (stamp, data) in zip(report["messages"], raw):
                    self.assertEqual(item["capture_ns"], str(stamp))
                    self.assertEqual(item["raw_sha256"], hashlib.sha256(data).hexdigest())
                result = common(self, root / "common.csv", outputs["observations.csv"])
                obs = result["observations"][0]
                self.assertEqual(obs["fields"]["parameter_double_reported"], value)
                self.assertEqual(obs["fields"]["service_client_gid_hex"], "hex:" + gid.hex())
                self.assertEqual(obs["clock_basis"], "Unknown")
                self.assertIsNone(obs["anchor_unix_us"])
                if index == 0:
                    fixture.service_bag(root / "baseline", pair())
                    expected, _ = fixture.convert(root / "baseline")
                    self.assertEqual(outputs["observations.csv"], expected.encode())

    def test_equal_time_missing_side_and_unselected_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "saved"
            bag(source, [pair()[0]])
            mutate(source, "UPDATE messages SET timestamp=1234567890123456789;"
                "INSERT INTO topics VALUES(2,'/other','vendor/msg/Unknown','cdr','[]','');"
                "INSERT INTO messages VALUES(2,2,1234567890123456789,X'012345');")
            metadata = bridge.ros.YAML(typ="safe").load((source / "metadata.yaml").read_text())
            info = metadata["rosbag2_bagfile_information"]
            info["message_count"] = info["files"][0]["message_count"] = 2
            info["topics_with_message_count"].append(dict(message_count=1, topic_metadata=dict(
                name="/other", type="vendor/msg/Unknown", serialization_format="cdr",
                offered_qos_profiles=[], type_description_hash="")))
            with (source / "metadata.yaml").open("w") as stream:
                bridge.ros.YAML().dump(metadata, stream)
            outputs, report = bridge.convert(source, topic=fixture.SERVICE_TOPIC)
            self.assertEqual((report["source_messages"], report["selected_messages"], report["unselected_messages"]), (2, 1, 1))
            self.assertEqual(report["messages"][1]["disposition"], "UNSELECTED_NOT_DECODED")
            self.assertIsNone(report["messages"][1]["derived_index"])
            rows = list(csv.DictReader(io.StringIO(outputs["observations.csv"].decode())))
            self.assertNotEqual(rows[0]["service_transaction_status"], "MATCHED")
            self.assertEqual(common(self, root / "common.csv", outputs["observations.csv"])["main_rows"], 1)

    def test_qualification_accounting_order_and_resource_refusals(self):
        mutations = [
            "UPDATE topics SET type_description_hash='wrong'",
            "UPDATE message_definitions SET type_description_hash=''",
            "UPDATE message_definitions SET encoded_message_definition=replace(encoded_message_definition,'int64 sequence_number','int32 sequence_number')",
            "DELETE FROM message_definitions",
            "UPDATE schema SET schema_version=3",
            "UPDATE messages SET topic_id=99 WHERE id=2",
            "UPDATE messages SET timestamp=0 WHERE id=2",
            "DELETE FROM messages WHERE id=2",
            "UPDATE messages SET data=X'0000' WHERE id=2",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, sql in enumerate(mutations):
                with self.subTest(sql=sql):
                    source = root / f"bad{index}"; bag(source); mutate(source, sql)
                    with self.assertRaises((ValueError, AssertionError)):
                        bridge.convert(source, topic=fixture.SERVICE_TOPIC)
            source = root / "valid"; bag(source)
            for field, value in (("MAX_MESSAGES", 1), ("MAX_DB_BYTES", 1),
                                 ("MAX_OUTPUT_BYTES", 1), ("MAX_DERIVED_BYTES", 1)):
                with self.subTest(bound=field), patch.object(bridge, field, value):
                    with self.assertRaises(ValueError): bridge.convert(source, topic=fixture.SERVICE_TOPIC)
            with self.assertRaises(ValueError): bridge.convert(source, topic="/absent")
            (source / (next(source.glob("*.db3")).name + "-wal")).touch()
            with self.assertRaises(ValueError): bridge.convert(source, topic=fixture.SERVICE_TOPIC)

    def test_cli_exact_original_and_fresh_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "saved"; dbpath = bag(source)
            command = [os.sys.executable, str(ROOT / "scripts/convert_ros_service_sqlite.py"),
                       str(source), str(root / "out"), "--topic", fixture.SERVICE_TOPIC]
            process = subprocess.run(command, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual((root / "out/source.db3").read_bytes(), dbpath.read_bytes())
            self.assertEqual((root / "out/source-metadata.yaml").read_bytes(), (source / "metadata.yaml").read_bytes())
            report = json.loads((root / "out/report.json").read_bytes())
            self.assertEqual(report["selected_messages"], 2)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
