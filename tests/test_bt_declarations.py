"""Authored saved tree declarations, not a running behaviour tree."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_bt_declarations import convert
from scripts import convert_bt_declarations as bt

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/bt-declarations/profile.toml"

# Layout A: control + decorator + generic condition with a declared model, an unknown
# custom node with unknown ports, and a subtree reference with autoremap and {=}.
LAYOUT_A = """<root BTCPP_format="4" main_tree_to_execute="Main">
  <BehaviorTree ID="Main">
    <Sequence name="root_seq">
      <Inverter>
        <Condition ID="BatteryOk" threshold="0.25" state="{battery_state}"/>
      </Inverter>
      <MyVendorAction goal="{target}" retries="3" _skipIf="done == true"/>
      <SubTree ID="Recover" _autoremap="true" attempt="{=}" limit="2"/>
    </Sequence>
  </BehaviorTree>
  <BehaviorTree ID="Recover">
    <Fallback>
      <AlwaysSuccess/>
      <AlwaysFailure/>
    </Fallback>
  </BehaviorTree>
  <TreeNodesModel>
    <Condition ID="BatteryOk">
      <input_port name="threshold"/>
      <output_port name="state"/>
    </Condition>
  </TreeNodesModel>
</root>
"""

# Layout B: a different shape entirely - reactive control, nested decorators, no model,
# no main tree declared, generic Action with ID, and a differently ordered sibling list.
LAYOUT_B = """<root BTCPP_format="4">
  <BehaviorTree ID="Patrol">
    <ReactiveFallback>
      <Timeout msec="500">
        <ForceSuccess>
          <Action ID="GoTo" waypoint="{next_wp}" speed="0.4"/>
        </ForceSuccess>
      </Timeout>
      <Parallel success_count="1" failure_count="3">
        <Script code="counter := counter + 1"/>
        <OtherVendorNode topic="/scan"/>
      </Parallel>
    </ReactiveFallback>
  </BehaviorTree>
</root>
"""


def rows_of(text, capture_time_us=777):
    output, report = convert(text.encode(), capture_time_us)
    return list(csv.DictReader(io.StringIO(output))), report, output


def decoded(row, column):
    value = row[column]
    return bytes.fromhex(value[4:]).decode() if value else ""


class BehaviorTreeBundleTests(unittest.TestCase):
    def bundle(self, files=None, entry="project/main.xml"):
        return json.dumps(dict(format="btcpp4-saved-bundle-v1", entry=entry, files=files or {
            entry: '<root BTCPP_format="4" main_tree_to_execute="Main"><include path="sub/child.xml"/><BehaviorTree ID="Main"><SubTree ID="Child" goal="{target}"/></BehaviorTree></root>',
            "project/sub/child.xml": '<root BTCPP_format="4"><BehaviorTree ID="Child"><VendorNode speed="0.2"/></BehaviorTree></root>',
            "unused.xml": 'opaque <not interpreted',
        })).encode()

    def test_two_bundle_shapes_and_default_exact_parity(self):
        for data, selected in ((self.bundle(), "Main"), (self.bundle({
                "entry.xml": '<root BTCPP_format="4"><include path="child.xml"/></root>',
                "child.xml": '<root BTCPP_format="4"><BehaviorTree ID="Only"><AlwaysSuccess/></BehaviorTree></root>',
                }, "entry.xml"), "Only")):
            output, report = bt.convert_bundle(data, 777)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["selected_tree_id"], selected)
            self.assertEqual(report["input_files"], report["loaded_files"] + report["unreferenced_files"])
            self.assertEqual(report["source_bundle_hex"], "hex:" + data.hex())
            self.assertEqual(sum(r["bt_selected_tree"] == "1" for r in rows), 1)
            self.assertEqual(report["output_records"], len(rows))
            self.assertEqual(report["include_edges"], 1)
            self.assertEqual(len(report["documents"]), report["input_files"])
            self.assertTrue(all(r["record_time_us"] == "777" for r in rows))
            for doc in report["documents"]:
                original = json.loads(data)["files"][doc["name"]].encode()
                self.assertEqual(doc["source_xml_hex"], "hex:" + original.hex())
                self.assertEqual(doc["source_sha256"], "sha256:" + hashlib.sha256(original).hexdigest())
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "observations.csv"
                    path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(PROFILE), str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], len(rows))
                    self.assertEqual(result["platform_domain"], "Unknown")
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in result["observations"]))
            else:
                self.fail("actual common reader required for bundle proof")
        for layout, csv_sha, report_sha in ((LAYOUT_A,
                "3ff7396256b3ddea9b4b745226c923f9e364d9ff5bfc6edd385715e9470ade89",
                "7b4e046818a5838183f29806e9535613ca12a73fc431df8df229c094da58c717"),
                (LAYOUT_B, "cb80812e1c9ef476e66f66c9524e1fe46b378b600ed6fea99ec3b0fd310ef058",
                 "ff23dfbe742f0b5772c26565f9a9d5ef4af4f3f0bf8a381a509485397d8b2c10")):
            output, report = convert(layout.encode(), 777)
            self.assertEqual(hashlib.sha256(output.encode()).hexdigest(), csv_sha)
            self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(), report_sha)

    def test_closed_bundle_rejections_and_cli(self):
        base = json.loads(self.bundle())
        bad = []
        for path in ("../child.xml", "/child.xml", "sub//child.xml", "./child.xml", "https://host/a", "sub\\child.xml"):
            value = json.loads(self.bundle())
            value["files"][value["entry"]] = value["files"][value["entry"]].replace("sub/child.xml", path)
            bad.append(json.dumps(value).encode())
        for xml in ('<root BTCPP_format="4"><include path="absent.xml"/></root>',
                    '<root BTCPP_format="4"><include path="main.xml"/></root>',
                    '<root BTCPP_format="4"><include path="sub/child.xml" ros_pkg="x"/></root>',
                    '<root BTCPP_format="4"><BehaviorTree ID="Main"><SubTree ID="Absent"/></BehaviorTree></root>',
                    '<root BTCPP_format="4"><BehaviorTree ID="Main"><SubTree ID="Main"/></BehaviorTree></root>'):
            value = json.loads(self.bundle())
            value["files"][value["entry"]] = xml
            bad.append(json.dumps(value).encode())
        bad += [b'{"format":"btcpp4-saved-bundle-v1","entry":"a","entry":"b","files":{}}',
                json.dumps({**base, "extra": True}).encode(), b"x" * (bt.BUNDLE_LIMIT + 1)]
        for data in bad:
            with self.subTest(data=data[:100]), self.assertRaises(ValueError):
                bt.convert_bundle(data, 0)
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "bundle.json", Path(directory) / "out"
            source.write_bytes(self.bundle())
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_bt_declarations.py"),
                str(source), str(destination), "--capture-time-us", "1", "--bundle-json",
                "--main-tree-id", "Child"], check=True, capture_output=True)
            report = json.loads((destination / "report.json").read_text())
            self.assertEqual(report["selection_basis"], "CALLER_ARGUMENT")
            self.assertEqual(report["selected_tree_id"], "Child")

    def test_graph_ambiguity_and_global_bounds_are_explicit_refusals(self):
        mutations = []
        for child in ('<root BTCPP_format="4"><BehaviorTree ID="Main"><AlwaysSuccess/></BehaviorTree></root>',
                      '<root BTCPP_format="4"><BehaviorTree ID="Child"><SubTree ID="Main"/></BehaviorTree></root>'):
            value = json.loads(self.bundle())
            value["files"]["project/sub/child.xml"] = child
            mutations.append(value)
        value = json.loads(self.bundle())
        value["files"][value["entry"]] = value["files"][value["entry"]].replace(
            '<include path="sub/child.xml"/>', '<include path="sub/child.xml"/>' * 2)
        mutations.append(value)
        value = json.loads(self.bundle())
        value["files"].update({f"unused{i}.xml": "opaque" for i in range(bt.MAX_FILES)})
        mutations.append(value)
        value = json.loads(self.bundle())
        value["files"]["unused.xml"] = "é" * (bt.LIMIT // 2 + 1)
        mutations.append(value)
        value = json.loads(self.bundle())
        value["files"]["project/sub/child.xml"] = '<root BTCPP_format="4"><BehaviorTree ID="Child"><Sequence>' + '<AlwaysSuccess/>' * bt.MAX_NODES + '</Sequence></BehaviorTree></root>'
        mutations.append(value)
        for value in mutations:
            with self.subTest(files=len(value["files"])), self.assertRaises(ValueError):
                bt.convert_bundle(json.dumps(value).encode(), 0)
        for choice in ("", "Absent", False, []):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                bt.convert_bundle(self.bundle(), 0, main_tree_id=choice)


class BehaviorTreeDeclarationTests(unittest.TestCase):
    def test_two_layouts_keep_identity_order_ports_and_reach_common_output(self):
        for layout, tree_ids, main_declared in ((LAYOUT_A, ["Main", "Recover"], True),
                                                (LAYOUT_B, ["Patrol"], False)):
            rows, report, output = rows_of(layout)
            nodes = [row for row in rows if row["bt_record_kind"] == "NODE"]
            trees = [row for row in rows if row["bt_record_kind"] == "TREE"]
            self.assertEqual([decoded(row, "bt_tree_id_hex") for row in trees], tree_ids)
            self.assertEqual(report["declared_trees"], len(tree_ids))
            self.assertEqual(report["main_tree_declared"], main_declared)
            self.assertEqual(report["declared_nodes"], len(nodes))
            self.assertEqual(report["pin"], "behaviortree-cpp-4.6.2")
            # Ordered edges: every non-root node points at a parent that is declared earlier,
            # and siblings carry 0..n-1 in document order.
            by_uid = {row["bt_node_uid"]: row for row in rows if row["bt_record_kind"] in ("NODE", "TREE")}
            for row in nodes:
                self.assertIn(row["bt_parent_uid"], by_uid)
                self.assertLess(int(row["bt_parent_uid"]), int(row["bt_node_uid"]))
                self.assertEqual(row["bt_declaration_basis"], "DECLARED_STRUCTURE_NOT_EXECUTED_OR_TICKED")
            for parent in {row["bt_parent_uid"] for row in nodes}:
                ordinals = [int(row["bt_child_ordinal"]) for row in nodes if row["bt_parent_uid"] == parent]
                self.assertEqual(ordinals, list(range(len(ordinals))))
                self.assertEqual(int(by_uid[parent]["bt_child_count"]), len(ordinals))
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "observations.csv"
                    path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(PROFILE), str(path), "--allow-equal-time"],
                        check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], len(rows))
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
                    self.assertEqual(common["observations"][0]["fields"]["bt_record_kind"], "TREE")
            else:
                print("BT declaration common-reader integration NOT_RUN: executable not configured")

    def test_categories_ports_blackboard_and_unknown_extensions_are_preserved(self):
        rows, report, _ = rows_of(LAYOUT_A)
        nodes = {decoded(row, "bt_tag_hex"): row for row in rows if row["bt_record_kind"] == "NODE"}
        self.assertEqual(nodes["Sequence"]["bt_node_category"], "CONTROL")
        self.assertEqual(nodes["Inverter"]["bt_node_category"], "DECORATOR")
        self.assertEqual(nodes["AlwaysSuccess"]["bt_node_category"], "ACTION_OR_CONDITION")
        self.assertEqual(nodes["Condition"]["bt_node_category"], "CONDITION")
        self.assertEqual(decoded(nodes["Condition"], "bt_declared_id_hex"), "BatteryOk")
        self.assertEqual(decoded(nodes["Sequence"], "bt_node_name_hex"), "root_seq")
        # A vendor node is retained and explicitly unqualified - never guessed from its name.
        custom = nodes["MyVendorAction"]
        self.assertEqual(custom["bt_node_category"], "UNKNOWN_NOT_REGISTERED")
        self.assertEqual(custom["bt_category_basis"], "UNQUALIFIED_PLUGIN_NOT_RESOLVABLE_OFFLINE")
        # SubTree reference joins to a tree declared in the same document.
        self.assertEqual(decoded(nodes["SubTree"], "bt_subtree_ref_hex"), "Recover")
        ports = {(row["bt_node_uid"], decoded(row, "bt_attribute_name_hex")): row
                 for row in rows if row["bt_record_kind"] in ("PORT", "RESERVED")}
        condition_uid = nodes["Condition"]["bt_node_uid"]
        threshold = ports[(condition_uid, "threshold")]
        state = ports[(condition_uid, "state")]
        self.assertEqual(threshold["bt_attribute_kind"], "STATIC_DECLARATION_NOT_RESOLVED_VALUE")
        self.assertEqual(threshold["bt_port_direction"], "INPUT")   # from TreeNodesModel
        self.assertEqual(state["bt_attribute_kind"], "BLACKBOARD_KEY_REFERENCE")
        self.assertEqual(decoded(state, "bt_blackboard_key_hex"), "battery_state")
        self.assertEqual(state["bt_port_direction"], "OUTPUT")
        custom_uid = custom["bt_node_uid"]
        self.assertEqual(ports[(custom_uid, "goal")]["bt_port_direction"],
                         "UNKNOWN_WITHOUT_DECLARED_MODEL")
        self.assertEqual(decoded(ports[(custom_uid, "goal")], "bt_blackboard_key_hex"), "target")
        self.assertEqual(ports[(custom_uid, "retries")]["bt_attribute_kind"],
                         "STATIC_DECLARATION_NOT_RESOLVED_VALUE")
        # Reserved framework attributes are kept, and are not called ports.
        skip = ports[(custom_uid, "_skipIf")]
        self.assertEqual(skip["bt_attribute_kind"], "FRAMEWORK_RESERVED_NOT_PORT")
        self.assertEqual(skip["bt_port_direction"], "NOT_A_PORT")
        self.assertEqual(decoded(skip, "bt_attribute_value_hex"), "done == true")
        subtree_uid = nodes["SubTree"]["bt_node_uid"]
        self.assertEqual(ports[(subtree_uid, "_autoremap")]["bt_attribute_kind"],
                         "SUBTREE_DECLARED_AUTOREMAP")
        attempt = ports[(subtree_uid, "attempt")]
        self.assertEqual(attempt["bt_attribute_kind"], "BLACKBOARD_SAME_NAME_REMAP")
        self.assertEqual(decoded(attempt, "bt_blackboard_key_hex"), "attempt")
        models = [row for row in rows if row["bt_record_kind"] == "MODEL_PORT"]
        self.assertEqual({decoded(row, "bt_attribute_name_hex") for row in models},
                         {"threshold", "state"})
        # Layout B has no model at all, so no direction is invented.
        other, _, _ = rows_of(LAYOUT_B)
        directions = {row["bt_port_direction"] for row in other if row["bt_record_kind"] == "PORT"}
        self.assertEqual(directions, {"UNKNOWN_WITHOUT_DECLARED_MODEL"})
        self.assertEqual({decoded(row, "bt_tag_hex") for row in other
                          if row["bt_node_category"] == "UNKNOWN_NOT_REGISTERED"}, {"OtherVendorNode"})

    def test_unsafe_or_ambiguous_declarations_refuse(self):
        base = ('<root BTCPP_format="4"><BehaviorTree ID="T">'
                "<Sequence><AlwaysSuccess/></Sequence></BehaviorTree></root>")
        cases = {
            "doctype": '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + base,
            "entity reference": base.replace("<AlwaysSuccess/>", "<AlwaysSuccess name='&x;'/>"),
            "malformed": base[:-8],
            "wrong format": base.replace('BTCPP_format="4"', 'BTCPP_format="3"'),
            "wrong root": base.replace("<root ", "<Root ").replace("</root>", "</Root>"),
            "include": base.replace("<BehaviorTree", '<include path="other.xml"/><BehaviorTree', 1),
            "duplicate tree id": base.replace("</root>",
                '<BehaviorTree ID="T"><AlwaysSuccess/></BehaviorTree></root>'),
            "unresolved subtree": base.replace("<AlwaysSuccess/>", '<SubTree ID="Absent"/>'),
            "cyclic subtree": ('<root BTCPP_format="4"><BehaviorTree ID="A"><SubTree ID="B"/>'
                               '</BehaviorTree><BehaviorTree ID="B"><SubTree ID="A"/>'
                               "</BehaviorTree></root>"),
            "self cycle": ('<root BTCPP_format="4"><BehaviorTree ID="A"><SubTree ID="A"/>'
                           "</BehaviorTree></root>"),
            "decorator two children": base.replace("<Sequence>", "<Inverter>")
                .replace("</Sequence>", "</Inverter>")
                .replace("<AlwaysSuccess/>", "<AlwaysSuccess/><AlwaysFailure/>"),
            "empty control": base.replace("<Sequence><AlwaysSuccess/></Sequence>", "<Sequence/>"),
            "subtree with child": base.replace("<AlwaysSuccess/>",
                '<SubTree ID="T"><AlwaysSuccess/></SubTree>'),
            "generic without id": base.replace("<AlwaysSuccess/>", "<Action/>"),
            "registered with id": base.replace("<AlwaysSuccess/>", '<AlwaysSuccess ID="X"/>'),
            "two tree roots": base.replace("<AlwaysSuccess/>",
                "<AlwaysSuccess/></Sequence><Sequence><AlwaysFailure/>"),
            "absent main tree": base.replace("<root ", '<root main_tree_to_execute="Missing" '),
            "unknown model port": base.replace("</root>",
                '<TreeNodesModel><Action ID="X"><weird name="p"/></Action></TreeNodesModel></root>'),
            "unknown root child": base.replace("</root>", "<Other/></root>"),
            "empty document": "",
        }
        for label, text in cases.items():
            with self.subTest(refused=label), self.assertRaises(ValueError):
                convert(text.encode(), 1)
        for capture in (None, -1, True, 2**63):
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                convert(base.encode(), capture)

    def test_practical_tree_and_cli_write_the_same_declarations(self):
        # 500 leaf declarations under nested controls: not a toy example.
        leaves = "".join(f'<MyVendorAction id_{index}="{index}" target="{{wp_{index}}}"/>'
                         for index in range(500))
        big = ('<root BTCPP_format="4" main_tree_to_execute="Big"><BehaviorTree ID="Big">'
               f"<Sequence><ReactiveFallback>{leaves}</ReactiveFallback>"
               "<AlwaysSuccess/></Sequence></BehaviorTree></root>")
        rows, report, output = rows_of(big, 123)
        nodes = [row for row in rows if row["bt_record_kind"] == "NODE"]
        self.assertEqual(report["declared_nodes"], 503)
        self.assertEqual(len(nodes), 503)
        self.assertEqual(sum(1 for row in rows if row["bt_record_kind"] == "PORT"), 1000)
        ordinals = [int(row["bt_child_ordinal"]) for row in nodes
                    if row["bt_parent_uid"] == nodes[1]["bt_node_uid"]]
        self.assertEqual(ordinals, list(range(500)))
        self.assertTrue(all(row["record_time_us"] == "123" for row in rows))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tree.xml"
            source.write_text(big)
            destination = Path(directory) / "out"
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_bt_declarations.py"),
                str(source), str(destination), "--capture-time-us", "123"],
                check=True, capture_output=True)
            self.assertEqual((destination / "observations.csv").read_text(), output)
            self.assertEqual(json.loads((destination / "report.json").read_text())["output_records"],
                             len(rows))
            # An existing output directory is refused rather than overwritten.
            self.assertNotEqual(subprocess.run([sys.executable,
                str(ROOT / "scripts/convert_bt_declarations.py"), str(source), str(destination),
                "--capture-time-us", "123"], capture_output=True).returncode, 0)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(destination / "observations.csv"), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                self.assertEqual(common["observations"][-1]["fields"]["record_time_us"], 123)
            bad = Path(directory) / "bad.xml"
            bad.write_text('<root BTCPP_format="4"><BehaviorTree ID="A"><SubTree ID="A"/></BehaviorTree></root>')
            refused = Path(directory) / "refused"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/convert_bt_declarations.py"),
                str(bad), str(refused), "--capture-time-us", "1"], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(refused.exists())


    def test_leaf_rule_contradictions_indexes_comments_and_bounded_reading(self):
        base = ('<root BTCPP_format="4"><BehaviorTree ID="T">'
                "<Sequence><AlwaysSuccess/></Sequence></BehaviorTree></root>")
        model = '<TreeNodesModel><Action ID="Vend"/></TreeNodesModel></root>'
        # The established leaf rule now covers named built-ins and modelled leaves, and a
        # generic tag may not assert a category its own ID contradicts.
        for label, text in (
                ("built-in leaf with a child",
                 base.replace("<AlwaysSuccess/>", "<AlwaysSuccess><AlwaysFailure/></AlwaysSuccess>")),
                ("modelled action with a child",
                 base.replace("</root>", model).replace("<AlwaysSuccess/>", "<Vend><AlwaysSuccess/></Vend>")),
                ("generic Action naming a control built-in",
                 base.replace("<AlwaysSuccess/>", '<Action ID="Sequence"/>')),
                ("generic Action with a control model",
                 base.replace("</root>", '<TreeNodesModel><Control ID="Grp"/></TreeNodesModel></root>')
                     .replace("<AlwaysSuccess/>", '<Action ID="Grp"/>')),
                ("generic Decorator with a leaf model",
                 base.replace("</root>", model).replace("<AlwaysSuccess/>", '<Decorator ID="Vend"><AlwaysSuccess/></Decorator>')),
                ("subtree ID naming a registered node",
                 base.replace("<AlwaysSuccess/>", '<SubTree ID="Sequence"/>'))):
            with self.subTest(refused=label), self.assertRaises(ValueError):
                convert(text.encode(), 1)
        # Same leaf family is not a contradiction, and an unknown tag stays unconstrained.
        same_family = base.replace("</root>", model).replace("<AlwaysSuccess/>", '<Condition ID="Vend"/>')
        rows, _, _ = rows_of(same_family)
        condition = [row for row in rows if decoded(row, "bt_declared_id_hex") == "Vend"][0]
        self.assertEqual(condition["bt_node_category"], "CONDITION")
        self.assertEqual(condition["bt_category_basis"], "GENERIC_TAG_WITH_ID_AND_DECLARED_MODEL")
        unknown, _, _ = rows_of(base.replace("<AlwaysSuccess/>", "<Vendor><AlwaysSuccess/></Vendor>"))
        vendor = [row for row in unknown if decoded(row, "bt_tag_hex") == "Vendor"][0]
        self.assertEqual(vendor["bt_node_category"], "UNKNOWN_NOT_REGISTERED")
        self.assertEqual(vendor["bt_child_count"], "1")

        # Every emitted declaration carries its own document element index.
        indexed = ('<root BTCPP_format="4"><BehaviorTree ID="M"><Sequence>'
                   '<Condition ID="C" p="{k}" _uid="9"/></Sequence></BehaviorTree>'
                   '<TreeNodesModel><Condition ID="C"><input_port name="p"/></Condition>'
                   "</TreeNodesModel></root>")
        rows, _, _ = rows_of(indexed)
        self.assertTrue(all(row["bt_element_index"] != "" for row in rows))
        node = [row for row in rows if decoded(row, "bt_tag_hex") == "Condition"][0]
        for kind in ("PORT", "RESERVED"):
            attribute = [row for row in rows if row["bt_record_kind"] == kind][0]
            self.assertEqual(attribute["bt_element_index"], node["bt_node_uid"])
            self.assertEqual(attribute["bt_node_uid"], node["bt_node_uid"])
        model_port = [row for row in rows if row["bt_record_kind"] == "MODEL_PORT"][0]
        self.assertNotEqual(model_port["bt_element_index"], model_port["bt_node_uid"])
        self.assertEqual(int(model_port["bt_element_index"]), int(model_port["bt_node_uid"]) + 1)
        self.assertEqual(sorted(int(row["bt_element_index"]) for row in rows
                                if row["bt_record_kind"] in ("TREE", "NODE", "MODEL_PORT")),
                         [1, 2, 3, 6])

        # Ordinary saved trees carry comments; they declare nothing and lose nothing.
        commented = ("<!-- saved by hand -->\n" + base.replace("<BehaviorTree", "<!-- main --><BehaviorTree", 1)
                     .replace("<AlwaysSuccess/>", "<!-- step --><AlwaysSuccess/>"))
        with_comments, report, output = rows_of(commented)
        plain, plain_report, _ = rows_of(base)
        self.assertEqual([row["bt_record_kind"] for row in with_comments],
                         [row["bt_record_kind"] for row in plain])
        self.assertEqual(report["declared_nodes"], plain_report["declared_nodes"])
        self.assertEqual([decoded(row, "bt_tag_hex") for row in with_comments],
                         [decoded(row, "bt_tag_hex") for row in plain])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "commented.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(path), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(with_comments))
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for label, text in (("doctype", "<!DOCTYPE r []>" + base),
                            ("entity declaration", base.replace("<root", '<!ENTITY x "y"><root', 1)),
                            ("cdata", base.replace("<AlwaysSuccess/>", "<AlwaysSuccess><![CDATA[x]]></AlwaysSuccess>")),
                            ("unterminated comment", "<!-- oops" + base)):
            with self.subTest(refused=label), self.assertRaises(ValueError):
                convert(text.encode(), 1)

        # Model-only rows count against the declared row bound like any other row.
        import scripts.convert_bt_declarations as module
        original = module.MAX_ROWS
        try:
            module.MAX_ROWS = 3
            with self.assertRaises(ValueError):
                convert(indexed.encode(), 1)
        finally:
            module.MAX_ROWS = original

        # The CLI reads once, bounded: an oversized file is refused without a size race.
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory) / "big.xml"
            filler = "<!-- " + "x" * 2048 + " -->"
            oversized.write_text(base.replace("</root>", filler * 512 + "</root>"))
            self.assertGreater(oversized.stat().st_size, module.LIMIT)
            destination = Path(directory) / "out"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/convert_bt_declarations.py"),
                str(oversized), str(destination), "--capture-time-us", "1"], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
