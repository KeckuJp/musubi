"""Authored saved map declarations, not a loaded or projected Lanelet2 map."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_lanelet_declarations import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/lanelet-declarations/profile.toml"

MAP_A = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="authored">
  <!-- authored declaration, not a recording -->
  <node id="1" lat="49.0" lon="8.4"><tag k="ele" v="115.25"/></node>
  <node id="2" lat="49.0001" lon="8.4001"/>
  <node id="3" lat="49.0" lon="8.4002"><tag k="ele" v="-2.5"/><tag k="custom_marker" v="vendor"/></node>
  <node id="4" lat="49.0001" lon="8.4003"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><tag k="type" v="line_thin"/><tag k="subtype" v="solid"/></way>
  <way id="11"><nd ref="3"/><nd ref="4"/><tag k="type" v="line_thin"/></way>
  <way id="12"><nd ref="1"/><nd ref="3"/><tag k="area" v="true"/></way>
  <relation id="100">
    <member type="way" role="left" ref="10"/>
    <member type="way" role="right" ref="11"/>
    <member type="relation" role="regulatory_element" ref="200"/>
    <tag k="type" v="lanelet"/><tag k="subtype" v="road"/>
  </relation>
  <relation id="200">
    <member type="way" role="refers" ref="12"/>
    <tag k="type" v="regulatory_element"/><tag k="subtype" v="traffic_sign"/>
  </relation>
  <relation id="300">
    <member type="node" role="thing" ref="4"/>
    <tag k="type" v="vendor_extension"/>
  </relation>
</osm>
"""

MAP_B = """<osm version="0.6">
  <node id="-1" lat="-33.86" lon="151.20"/>
  <node id="-2" lat="-33.8601" lon="151.2001"/>
  <node id="-3" lat="-33.8602" lon="151.2002"><tag k="ele" v="0"/></node>
  <node id="-4" lat="-33.8603" lon="151.2003"/>
  <node id="-5" lat="-33.8604" lon="151.2004"/>
  <way id="-10"><nd ref="-1"/><nd ref="-2"/><nd ref="-3"/></way>
  <way id="-11"><nd ref="-4"/><nd ref="-5"/></way>
  <way id="-12"><nd ref="-1"/><nd ref="-5"/></way>
  <way id="-13"><nd ref="-2"/><nd ref="-4"/></way>
  <relation id="-100">
    <member type="way" role="right" ref="-11"/>
    <member type="way" role="left" ref="-10"/>
    <member type="way" role="centerline" ref="-12"/>
    <tag k="type" v="lanelet"/>
  </relation>
  <relation id="-200">
    <member type="way" role="outer" ref="-12"/>
    <member type="way" role="inner" ref="-13"/>
    <tag k="type" v="multipolygon"/><tag k="subtype" v="parking"/>
  </relation>
</osm>
"""


def rows_of(text, capture_time_us=777):
    output, report = convert(text.encode(), capture_time_us)
    return list(csv.DictReader(io.StringIO(output))), report, output


def decoded(row, column):
    value = row[column]
    return bytes.fromhex(value[4:]).decode() if value else ""


class LaneletDeclarationTests(unittest.TestCase):
    def test_two_maps_keep_coordinates_order_relations_and_reach_common_output(self):
        for text, nodes, ways, relations in ((MAP_A, 4, 3, 3), (MAP_B, 5, 4, 2)):
            rows, report, output = rows_of(text)
            self.assertEqual((report["declared_nodes"], report["declared_ways"],
                              report["declared_relations"]), (nodes, ways, relations))
            self.assertEqual(report["output_records"], len(rows))
            self.assertEqual(report["pin"], "lanelet2-1.2.1")
            self.assertEqual(report["clock"], "Unknown")
            self.assertEqual(sum(report["rows_by_kind"].values()), len(rows))
            node_rows = [row for row in rows if row["map_record_kind"] == "NODE"]
            self.assertEqual(len(node_rows), nodes)
            for row in node_rows:
                self.assertEqual(row["map_coordinate_basis"],
                                 "DECLARED_DEGREES_DATUM_NOT_IN_FILE_PROJECTION_CALLER_SUPPLIED")
                self.assertTrue(-90 <= float(row["map_latitude_deg"]) <= 90)
                self.assertTrue(-180 <= float(row["map_longitude_deg"]) <= 180)
                self.assertEqual(row["map_declaration_basis"],
                                 "DECLARED_MAP_NOT_PROJECTED_ROUTED_OR_CERTIFIED")
            for way in [row for row in rows if row["map_record_kind"] == "WAY"]:
                references = [row for row in rows if row["map_record_kind"] == "WAY_REF"
                              and row["map_owner_id"] == way["map_element_id"]]
                self.assertEqual([int(row["map_member_ordinal"]) for row in references],
                                 list(range(len(references))))
                self.assertEqual(int(way["map_reference_count"]), len(references))
                self.assertTrue(all(row["map_reference_kind"] == "node" for row in references))
            for relation in [row for row in rows if row["map_record_kind"] == "RELATION"]:
                members = [row for row in rows if row["map_record_kind"] == "MEMBER"
                           and row["map_owner_id"] == relation["map_element_id"]]
                self.assertEqual([int(row["map_member_ordinal"]) for row in members],
                                 list(range(len(members))))
                self.assertEqual(int(relation["map_reference_count"]), len(members))
            indexes = [int(row["map_element_index"]) for row in rows]
            self.assertEqual(indexes, sorted(indexes))
            self.assertEqual(len(set(indexes)), len(indexes))
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
                    first = common["observations"][0]["fields"]
                    self.assertEqual(first["map_record_kind"], "NODE")
                    self.assertAlmostEqual(first["map_latitude_deg"], 49.0 if text is MAP_A else -33.86)
            else:
                print("Lanelet declaration common-reader integration NOT_RUN: executable not configured")

    def test_elevation_roles_relation_types_and_unknown_tags_stay_honest(self):
        rows, report, _ = rows_of(MAP_A)
        nodes = {row["map_element_id"]: row for row in rows if row["map_record_kind"] == "NODE"}
        self.assertEqual(float(nodes["1"]["map_elevation_m"]), 115.25)
        self.assertEqual(nodes["1"]["map_elevation_status"], "DECLARED_METRES_OVER_WGS84_ELLIPSOID")
        self.assertEqual(float(nodes["3"]["map_elevation_m"]), -2.5)
        self.assertEqual(nodes["2"]["map_elevation_m"], "")
        self.assertEqual(nodes["2"]["map_elevation_status"], "ELEVATION_NOT_DECLARED")
        self.assertEqual(nodes["4"]["map_elevation_m"], "")
        relations = {row["map_element_id"]: row for row in rows if row["map_record_kind"] == "RELATION"}
        self.assertEqual(relations["100"]["map_relation_type"], "LANELET")
        self.assertEqual(decoded(relations["100"], "map_relation_subtype_hex"), "road")
        self.assertEqual(relations["200"]["map_relation_type"], "REGULATORY_ELEMENT")
        self.assertEqual(decoded(relations["200"], "map_relation_subtype_hex"), "traffic_sign")
        self.assertEqual(relations["300"]["map_relation_type"], "OTHER_UNQUALIFIED")
        members = [row for row in rows if row["map_record_kind"] == "MEMBER"]
        meanings = {(row["map_owner_id"], int(row["map_member_ordinal"])): row for row in members}
        self.assertEqual(meanings[("100", 0)]["map_member_role_meaning"], "LEFT_BOUNDARY")
        self.assertEqual(meanings[("100", 1)]["map_member_role_meaning"], "RIGHT_BOUNDARY")
        self.assertEqual(meanings[("100", 2)]["map_member_role_meaning"], "REGULATORY_ELEMENT")
        self.assertEqual(meanings[("100", 2)]["map_reference_kind"], "relation")
        self.assertEqual(int(meanings[("100", 2)]["map_reference_id"]), 200)
        self.assertEqual(meanings[("200", 0)]["map_member_role_meaning"], "UNQUALIFIED_ROLE")
        self.assertEqual(decoded(meanings[("200", 0)], "map_member_role_hex"), "refers")
        self.assertEqual(meanings[("300", 0)]["map_member_role_meaning"], "UNQUALIFIED_ROLE")
        tags = {(row["map_owner_id"], decoded(row, "map_tag_key_hex")): decoded(row, "map_tag_value_hex")
                for row in rows if row["map_record_kind"] == "TAG"}
        self.assertEqual(tags[("3", "custom_marker")], "vendor")
        self.assertEqual(tags[("3", "ele")], "-2.5")
        self.assertEqual(tags[("100", "type")], "lanelet")
        self.assertEqual(tags[("300", "type")], "vendor_extension")
        self.assertEqual(report["rows_by_kind"]["TAG"], len(tags))
        ways = {row["map_element_id"]: row for row in rows if row["map_record_kind"] == "WAY"}
        self.assertEqual(ways["12"]["map_way_kind"], "POLYGON_DECLARED_AREA_TAG")
        self.assertEqual(ways["10"]["map_way_kind"], "LINESTRING_DEFAULT")
        other, _, _ = rows_of(MAP_B)
        other_members = {(row["map_owner_id"], int(row["map_member_ordinal"])): row
                         for row in other if row["map_record_kind"] == "MEMBER"}
        self.assertEqual(other_members[("-100", 0)]["map_member_role_meaning"], "RIGHT_BOUNDARY")
        self.assertEqual(other_members[("-100", 2)]["map_member_role_meaning"], "CENTERLINE")
        self.assertEqual(other_members[("-200", 0)]["map_member_role_meaning"], "OUTER_RING")
        self.assertEqual(other_members[("-200", 1)]["map_member_role_meaning"], "INNER_RING")
        declared_zero = [row for row in other if row["map_record_kind"] == "NODE"
                         and row["map_element_id"] == "-3"][0]
        self.assertEqual(float(declared_zero["map_elevation_m"]), 0.0)
        self.assertEqual(declared_zero["map_elevation_status"], "DECLARED_METRES_OVER_WGS84_ELLIPSOID")

    def test_ambiguous_or_unsafe_maps_refuse(self):
        base = ('<osm version="0.6"><node id="1" lat="1.0" lon="2.0"/>'
                '<node id="2" lat="1.1" lon="2.1"/><way id="10"><nd ref="1"/><nd ref="2"/></way></osm>')
        cases = {
            "doctype": '<!DOCTYPE osm [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + base,
            "entity reference": base.replace('generator', 'generator') .replace('<node id="1"', '<node id="1" user="&x;"', 1),
            "cdata": base.replace("</osm>", "<note><![CDATA[x]]></note></osm>"),
            "malformed": base[:-6],
            "wrong root": base.replace("<osm", "<map").replace("</osm>", "</map>"),
            "unsupported child": base.replace("</osm>", "<changeset id='1'/></osm>"),
            "duplicate node id": base.replace('<node id="2"', '<node id="1"'),
            "duplicate way id": base.replace("</osm>", '<way id="10"><nd ref="1"/></way></osm>'),
            "missing latitude": base.replace('lat="1.0" ', ""),
            "missing longitude": base.replace('lon="2.0"', ""),
            "latitude out of range": base.replace('lat="1.0"', 'lat="91.0"'),
            "longitude out of range": base.replace('lon="2.0"', 'lon="181"'),
            "non-numeric latitude": base.replace('lat="1.0"', 'lat="north"'),
            "non-numeric elevation": base.replace('<node id="1" lat="1.0" lon="2.0"/>',
                                                  '<node id="1" lat="1.0" lon="2.0"><tag k="ele" v="high"/></node>'),
            "duplicate elevation": base.replace('<node id="1" lat="1.0" lon="2.0"/>',
                                                '<node id="1" lat="1.0" lon="2.0"><tag k="ele" v="1"/><tag k="ele" v="2"/></node>'),
            "way without references": base.replace('<way id="10"><nd ref="1"/><nd ref="2"/></way>', '<way id="10"/>'),
            "missing node reference": base.replace('<nd ref="2"/>', '<nd ref="9"/>'),
            "missing member reference": base.replace("</osm>",
                '<relation id="100"><member type="way" role="left" ref="99"/><tag k="type" v="lanelet"/></relation></osm>'),
            "member with unknown type": base.replace("</osm>",
                '<relation id="100"><member type="changeset" role="left" ref="10"/></relation></osm>'),
            "member without role": base.replace("</osm>",
                '<relation id="100"><member type="way" ref="10"/></relation></osm>'),
            "lanelet without both borders": base.replace("</osm>",
                '<relation id="100"><member type="way" role="left" ref="10"/><tag k="type" v="lanelet"/></relation></osm>'),
            "lanelet with two lefts": base.replace("</osm>",
                '<relation id="100"><member type="way" role="left" ref="10"/><member type="way" role="left" ref="10"/>'
                '<member type="way" role="right" ref="10"/><tag k="type" v="lanelet"/></relation></osm>'),
            "regulatory element without subtype": base.replace("</osm>",
                '<relation id="100"><member type="way" role="refers" ref="10"/>'
                '<tag k="type" v="regulatory_element"/></relation></osm>'),
            "reference to a deleted node": base.replace('<node id="2" lat="1.1" lon="2.1"/>',
                                                        '<node id="2" lat="1.1" lon="2.1" action="delete"/>'),
            "tag without value": base.replace('<way id="10">', '<way id="10"><tag k="type"/>'),
            "empty document": "",
        }
        for label, text in cases.items():
            with self.subTest(refused=label), self.assertRaises(ValueError):
                convert(text.encode(), 1)
        for capture in (None, -1, True, 2**63):
            with self.subTest(capture=capture), self.assertRaises(ValueError):
                convert(base.encode(), capture)
        commented = "<!-- hand edited -->\n" + base.replace("<way", "<!-- the way --><way", 1)
        self.assertEqual(rows_of(commented)[1]["output_records"], rows_of(base)[1]["output_records"])
        with_deleted = base.replace("</osm>", '<node id="3" lat="1.2" lon="2.2" action="delete"/></osm>')
        rows, report, _ = rows_of(with_deleted)
        self.assertEqual(report["deleted_declarations"], 1)
        deleted_row = [row for row in rows if row["map_element_id"] == "3"][0]
        self.assertEqual(deleted_row["map_load_disposition"], "DELETED_ACTION_NOT_LOADED")

    def test_practical_map_and_cli_write_the_same_declarations(self):
        parts = ['<osm version="0.6">']
        for index in range(10000):
            parts.append(f'<node id="{index + 1}" lat="{49.0 + index * 1e-5:.5f}" '
                         f'lon="{8.4 + index * 1e-5:.5f}"/>')
        for way in range(2000):
            first = way * 5 + 1
            refs = "".join(f'<nd ref="{first + offset}"/>' for offset in range(5))
            parts.append(f'<way id="{100000 + way}">{refs}</way>')
        for lanelet in range(1000):
            left, right = 100000 + lanelet * 2, 100000 + lanelet * 2 + 1
            parts.append(f'<relation id="{200000 + lanelet}">'
                         f'<member type="way" role="left" ref="{left}"/>'
                         f'<member type="way" role="right" ref="{right}"/>'
                         '<tag k="type" v="lanelet"/></relation>')
        parts.append("</osm>")
        text = "".join(parts)
        rows, report, output = rows_of(text, 123)
        self.assertEqual(report["declared_nodes"], 10000)
        self.assertEqual(report["rows_by_kind"]["WAY_REF"], 10000)
        self.assertEqual(report["rows_by_kind"]["MEMBER"], 2000)
        self.assertEqual(len(rows), report["output_records"])
        self.assertTrue(all(row["record_time_us"] == "123" for row in rows))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "map.osm"
            source.write_text(text)
            destination = Path(directory) / "out"
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_lanelet_declarations.py"),
                str(source), str(destination), "--capture-time-us", "123"],
                check=True, capture_output=True)
            self.assertEqual((destination / "observations.csv").read_text(), output)
            self.assertEqual(json.loads((destination / "report.json").read_text())["declared_ways"], 2000)
            self.assertNotEqual(subprocess.run([sys.executable,
                str(ROOT / "scripts/convert_lanelet_declarations.py"), str(source), str(destination),
                "--capture-time-us", "123"], capture_output=True).returncode, 0)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(destination / "observations.csv"), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                self.assertEqual(common["observations"][0]["fields"]["map_record_kind"], "NODE")
            refused = Path(directory) / "refused"
            bad = Path(directory) / "bad.osm"
            bad.write_text('<osm version="0.6"><node id="1" lat="1" lon="2"/>'
                           '<way id="2"><nd ref="9"/></way></osm>')
            result = subprocess.run([sys.executable, str(ROOT / "scripts/convert_lanelet_declarations.py"),
                str(bad), str(refused), "--capture-time-us", "1"], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(refused.exists())


    def test_role_context_unknown_accounting_and_ambiguous_tags(self):
        base = ('<osm version="0.6"><node id="1" lat="1.0" lon="2.0"/><node id="2" lat="1.1" lon="2.1"/>'
                '<way id="10"><nd ref="1"/><nd ref="2"/></way><way id="11"><nd ref="1"/><nd ref="2"/></way>')

        def relation(body, kind="lanelet"):
            return base + f'<relation id="100">{body}<tag k="type" v="{kind}"/></relation></osm>'

        borders = '<member type="way" role="left" ref="10"/><member type="way" role="right" ref="11"/>'
        for label, body, kind in (
                ("node left", '<member type="node" role="left" ref="1"/>' + borders[46:], "lanelet"),
                ("relation right", '<member type="way" role="left" ref="10"/>'
                 '<member type="relation" role="right" ref="100"/>', "lanelet"),
                ("node centerline", borders + '<member type="node" role="centerline" ref="1"/>', "lanelet"),
                ("node outer", '<member type="node" role="outer" ref="1"/>', "multipolygon"),
                ("node inner", '<member type="way" role="outer" ref="10"/>'
                 '<member type="node" role="inner" ref="1"/>', "multipolygon")):
            with self.subTest(refused=label), self.assertRaises(ValueError):
                convert(relation(body, kind).encode(), 1)
        for kind in ("regulatory_element", "vendor_extension"):
            text = base + (f'<relation id="100"><member type="way" role="left" ref="10"/>'
                           f'<tag k="type" v="{kind}"/>'
                           + ('<tag k="subtype" v="traffic_light"/>' if kind == "regulatory_element" else "")
                           + "</relation></osm>")
            rows, _, _ = rows_of(text)
            member = [row for row in rows if row["map_record_kind"] == "MEMBER"][0]
            with self.subTest(relation=kind):
                self.assertEqual(member["map_member_role_meaning"], "UNQUALIFIED_ROLE")
                self.assertEqual(decoded(member, "map_member_role_hex"), "left")
        rows, _, _ = rows_of(relation(borders + '<member type="way" role="regulatory_element" ref="10"/>'))
        documented = [row for row in rows if row["map_record_kind"] == "MEMBER"][2]
        self.assertEqual(documented["map_member_role_meaning"], "UNQUALIFIED_ROLE")
        self.assertEqual(documented["map_reference_kind"], "way")

        unknown = base.replace('<node id="1" lat="1.0" lon="2.0"/>',
                               '<node id="1" lat="1.0" lon="2.0" version="3" user="mapper">'
                               '<extra a="1"/><tag k="x" v="y" source="survey"/></node>') + "</osm>"
        rows, report, _ = rows_of(unknown)
        extras = [row for row in rows if row["map_record_kind"].startswith("UNKNOWN")]
        self.assertEqual({decoded(row, "map_tag_key_hex") for row in extras},
                         {"version", "user", "extra", "source"})
        self.assertEqual(report["unknown_declarations"], len(extras))
        self.assertEqual(report["rows_by_kind"]["UNKNOWN_CHILD"], 1)
        self.assertEqual(report["rows_by_kind"]["UNKNOWN_ATTRIBUTE"], 3)
        self.assertEqual(report["output_records"], len(rows))
        for row in extras:
            self.assertEqual(row["map_load_disposition"], "UNKNOWN_AT_PIN_RETAINED_BY_REFERENCE")
            self.assertNotEqual(row["map_element_index"], "")
            self.assertEqual(row["map_owner_id"], "1")
        child = [row for row in extras if decoded(row, "map_tag_key_hex") == "extra"][0]
        self.assertEqual(child["map_record_kind"], "UNKNOWN_CHILD")
        self.assertEqual(child["map_reference_count"], "1")
        self.assertEqual(sum(1 for row in rows if row["map_record_kind"] == "TAG"), 1)

        repeated = base.replace('<node id="1" lat="1.0" lon="2.0"/>',
                                '<node id="1" lat="1.0" lon="2.0"><tag k="ele" v="5.5"/>'
                                '<tag k="ele" v="5.5"/></node>') + "</osm>"
        rows, report, _ = rows_of(repeated)
        node = [row for row in rows if row["map_record_kind"] == "NODE"][0]
        self.assertEqual(float(node["map_elevation_m"]), 5.5)
        self.assertEqual(report["rows_by_kind"]["TAG"], 2)  # both declarations retained
        for label, text in (
                ("conflicting ele", base.replace('<node id="1" lat="1.0" lon="2.0"/>',
                    '<node id="1" lat="1.0" lon="2.0"><tag k="ele" v="1"/><tag k="ele" v="2"/></node>') + "</osm>"),
                ("conflicting area", base.replace('<way id="10"><nd ref="1"/><nd ref="2"/></way>',
                    '<way id="10"><nd ref="1"/><nd ref="2"/><tag k="area" v="true"/>'
                    '<tag k="area" v="false"/></way>') + "</osm>"),
                ("conflicting type", base + '<relation id="100"><member type="way" role="left" ref="10"/>'
                 '<tag k="type" v="lanelet"/><tag k="type" v="multipolygon"/></relation></osm>'),
                ("conflicting subtype", base + '<relation id="100"><member type="way" role="refers" ref="10"/>'
                 '<tag k="type" v="regulatory_element"/><tag k="subtype" v="a"/>'
                 '<tag k="subtype" v="b"/></relation></osm>')):
            with self.subTest(refused=label), self.assertRaises(ValueError):
                convert(text.encode(), 1)


    def test_reference_attributes_root_metadata_and_exact_signed64_ids(self):
        text = ('<osm version="0.6"><bounds minlat="1" minlon="2" maxlat="3" maxlon="4"/>'
                "<note>authored</note>"
                '<node id="1" lat="1.0" lon="2.0"/><node id="2" lat="1.1" lon="2.1"/>'
                '<way id="10"><nd ref="1" nd_extra="survey"/><nd ref="2"/></way>'
                '<way id="11"><nd ref="1"/><nd ref="2"/></way>'
                '<relation id="100"><member type="way" role="left" ref="10" member_extra="vendor"/>'
                '<member type="way" role="right" ref="11"/><tag k="type" v="lanelet"/></relation></osm>')
        rows, report, output = rows_of(text)
        unknown = {decoded(row, "map_tag_key_hex"): row for row in rows
                   if row["map_record_kind"] == "UNKNOWN_ATTRIBUTE"}
        self.assertEqual(set(unknown), {"nd_extra", "member_extra"})
        self.assertEqual(unknown["nd_extra"]["map_reference_kind"], "nd")
        self.assertEqual(unknown["nd_extra"]["map_owner_id"], "10")
        self.assertEqual(decoded(unknown["nd_extra"], "map_tag_value_hex"), "survey")
        self.assertEqual(unknown["member_extra"]["map_reference_kind"], "member")
        self.assertEqual(unknown["member_extra"]["map_owner_id"], "100")
        for row in unknown.values():
            self.assertEqual(row["map_load_disposition"], "UNKNOWN_AT_PIN_RETAINED_BY_REFERENCE")
            self.assertNotEqual(row["map_element_index"], "")
        self.assertEqual(report["rows_by_kind"]["UNKNOWN_ATTRIBUTE"], 2)
        self.assertEqual(report["unknown_declarations"], 2)
        self.assertEqual(report["root_metadata_elements"], 2)
        self.assertEqual([entry["element"] for entry in report["root_metadata"]],
                         ["bounds", "note"])
        self.assertEqual(report["root_metadata"][0]["attribute_names"],
                         ["maxlat", "maxlon", "minlat", "minlon"])
        self.assertTrue(all(entry["element_index"] >= 0 for entry in report["root_metadata"]))
        self.assertFalse(any(row["map_record_kind"] in ("NODE", "WAY", "RELATION")
                             and row["map_element_id"] in ("", None)
                             for row in rows))
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "unknown.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(path), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                kinds = [o["fields"]["map_record_kind"] for o in common["observations"]]
                self.assertEqual(kinds.count("UNKNOWN_ATTRIBUTE"), 2)

        big, maximum = 9007199254740993, 9223372036854775807
        exact = (f'<osm version="0.6"><node id="{big}" lat="1.0" lon="2.0"/>'
                 f'<node id="{maximum}" lat="1.1" lon="2.1"/>'
                 f'<way id="12345678901234567"><nd ref="{big}"/><nd ref="{maximum}"/></way></osm>')
        rows, _, output = rows_of(exact)
        self.assertEqual([row["map_element_id"] for row in rows if row["map_record_kind"] == "NODE"],
                         [str(big), str(maximum)])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "ids.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(PROFILE), str(path), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                fields = [o["fields"] for o in common["observations"]]
                self.assertEqual(fields[0]["map_element_id"], big)
                self.assertEqual(fields[1]["map_element_id"], maximum)
                self.assertEqual(fields[3]["map_reference_id"], big)
                self.assertEqual(fields[4]["map_reference_id"], maximum)
                self.assertEqual(fields[3]["map_owner_id"], 12345678901234567)
        else:
            print("Lanelet exact-id common-reader check NOT_RUN: executable not configured")


if __name__ == "__main__":
    unittest.main()
