"""Independently specified finite saved-bundle provenance; no tree execution."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import convert_bt_declarations as subject

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/bt-declarations/profile.toml"


def decoded(row, name):
    return bytes.fromhex(row[name][4:]).decode() if row[name] else ""


def bundle():
    return {"format": "btcpp4-saved-bundle-v1", "entry": "p/main.xml", "files": {
        "p/main.xml": '<root BTCPP_format="4" main_tree_to_execute="Main"><include path="sub/child.xml"/><BehaviorTree ID="Main"><SubTree ID="Child"/></BehaviorTree></root>',
        "p/sub/child.xml": '<root BTCPP_format="4"><include path="leaf.xml"/><BehaviorTree ID="Child"><Sequence><SubTree ID="Leaf"/><Vendor z="1"/></Sequence></BehaviorTree></root>',
        "p/sub/leaf.xml": '<root BTCPP_format="4"><BehaviorTree ID="Leaf"><AlwaysSuccess/></BehaviorTree></root>',
        "p/unused.xml": "opaque <not XML: retained only",
    }}


class SavedBundleContractTests(unittest.TestCase):
    def common(self, output, expected):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader, "actual common reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([reader, str(PROFILE), str(path),
                "--allow-equal-time"], check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], expected)
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_parent_relative_edges_exact_documents_and_common_output(self):
        source = bundle()
        raw = json.dumps(source).encode()
        output, report = subject.convert_bundle(raw, 37)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["input_files"], report["loaded_files"],
            report["unreferenced_files"], report["include_edges"]), (4, 3, 1, 2))
        self.assertEqual(report["output_records"], len(rows))
        self.assertEqual(report["source_bundle_hex"], "hex:" + raw.hex())
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(report["source_bundle_sha256"], digest)
        self.assertTrue(all(r["source_bundle_sha256"] == digest for r in rows))
        self.assertTrue(all(r["record_time_us"] == "37" for r in rows))
        documents = [r for r in rows if r["bt_record_kind"] == "DOCUMENT"]
        self.assertEqual([decoded(r, "bt_document_name_hex") for r in documents], list(source["files"]))
        self.assertEqual(documents[-1]["bt_document_disposition"], "UNREFERENCED_NOT_INTERPRETED")
        for doc in report["documents"]:
            original = source["files"][doc["name"]].encode()
            self.assertEqual(doc["source_xml_hex"], "hex:" + original.hex())
            self.assertEqual(doc["source_sha256"], "sha256:" + hashlib.sha256(original).hexdigest())
        trees = [r for r in rows if r["bt_record_kind"] == "TREE"]
        self.assertEqual([decoded(r, "bt_tree_id_hex") for r in trees], ["Leaf", "Child", "Main"])
        self.assertEqual([r["bt_selected_tree"] for r in trees], ["0", "0", "1"])
        self.assertEqual(report["selection_basis"], "ENTRY_DECLARATION")
        includes = [r for r in rows if r["bt_record_kind"] == "INCLUDE"]
        self.assertEqual({(decoded(r, "bt_document_name_hex"), decoded(r, "bt_include_target_hex"))
            for r in includes}, {("p/main.xml", "p/sub/child.xml"), ("p/sub/child.xml", "p/sub/leaf.xml")})
        refs = [r for r in rows if r["bt_record_kind"] == "NODE" and r["bt_subtree_ref_hex"]]
        self.assertEqual({(decoded(r, "bt_subtree_ref_hex"), decoded(r, "bt_subtree_document_hex"))
            for r in refs}, {("Child", "p/sub/child.xml"), ("Leaf", "p/sub/leaf.xml")})
        declarations = [r for r in rows if r["bt_record_kind"] in ("NODE", "TREE")]
        identities = [(r["bt_document_name_hex"], r["bt_node_uid"]) for r in declarations]
        self.assertEqual(len(identities), len(set(identities)))
        self.common(output, len(rows))

    def test_caller_selection_reuse_does_not_discard_other_tree(self):
        source = {"format": "btcpp4-saved-bundle-v1", "entry": "a.xml", "files": {
            "a.xml": '<root BTCPP_format="4" main_tree_to_execute="One"><BehaviorTree ID="One"><AlwaysSuccess/></BehaviorTree><BehaviorTree ID="Two"><AlwaysFailure/></BehaviorTree></root>'}}
        output, report = subject.convert_bundle(json.dumps(source).encode(), 0, main_tree_id="Two")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["selected_tree_id"], "Two")
        self.assertEqual(report["selection_basis"], "CALLER_ARGUMENT")
        self.assertEqual(report["declared_trees"], 2)
        self.assertEqual([decoded(r, "bt_tree_id_hex") for r in rows
            if r["bt_record_kind"] == "TREE" and r["bt_selected_tree"] == "1"], ["Two"])
        self.common(output, len(rows))

    def test_missing_or_unqualified_link_is_not_success(self):
        for replacement in ("absent.xml", "../leaf.xml", "/tmp/leaf.xml", "sub//child.xml"):
            source = bundle()
            source["files"]["p/main.xml"] = source["files"]["p/main.xml"].replace("sub/child.xml", replacement)
            with self.subTest(path=replacement), self.assertRaises(ValueError):
                subject.convert_bundle(json.dumps(source).encode(), 0)
        source = bundle()
        source["files"]["p/sub/leaf.xml"] = '<root BTCPP_format="4"><BehaviorTree ID="Leaf"><SubTree ID="Main"/></BehaviorTree></root>'
        with self.assertRaises(ValueError):
            subject.convert_bundle(json.dumps(source).encode(), 0)


if __name__ == "__main__":
    unittest.main()
