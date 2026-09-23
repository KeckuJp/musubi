"""Authored RFC 7946 documents in the shape the specification states.

Nothing here was inspected, flown over, dived on or measured. These are a code and specification
proof: a saved document reaches common Observations as *what the document says*, with the frame
the specification fixes and with everything it leaves undefined left undefined.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_inspection_features import convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-inspection-features/profile.toml"
CAPTURE_US = 1_700_000_000_000_000
DECLARED = {"assertion_property": "observation", "quantity_property": "crack_width_mm",
            "quantity_unit": "mm"}


def without(record, key):
    """The same feature with one member missing entirely."""
    record = dict(record)
    record.pop(key)
    return record


def feature(identity="BR-014-SPAN-3", geometry=None, properties=None, **overrides):
    record = {"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [8.545594, 47.397742, 412.5]}
              if geometry is None else geometry,
              "properties": {"observation": "spalling reported at the north bearing",
                             "crack_width_mm": 3.5, "surveyor_note": "authored"}
              if properties is None else properties}
    if identity is not None:
        record["id"] = identity
    for key, value in overrides.items():
        if value is None:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def document(features=None, **overrides):
    record = {"type": "FeatureCollection",
              "features": [feature()] if features is None else features}
    for key, value in overrides.items():
        if value is None:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def rows_of(record, capture_time_us=CAPTURE_US, **declarations):
    output, report = convert(json.dumps(record).encode(), capture_time_us,
                             **(declarations or DECLARED))
    return list(csv.DictReader(io.StringIO(output))), report, output


def unhex(cell):
    return bytes.fromhex(cell[4:]).decode() if cell.startswith("hex:") else cell


class SavedInspectionFeatureTests(unittest.TestCase):
    def test_one_item_arrives_with_its_identity_frame_and_reported_assertion(self):
        rows, report, _ = rows_of(document())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(unhex(row["feature_id_hex"]), "BR-014-SPAN-3")
        self.assertIn("THE_FEATURES_OWN_ID_MEMBER", row["feature_identity_basis"])
        self.assertEqual((row["geometry_type"], row["geometry_position_count"]), ("Point", "1"))
        self.assertEqual((row["longitude_deg"], row["latitude_deg"], row["ellipsoidal_height_m"]),
                         ("8.545594", "47.397742", "412.5"))
        self.assertEqual(unhex(row["declared_assertion_key_hex"]), "observation")
        self.assertEqual(unhex(row["reported_assertion_hex"]),
                         "spalling reported at the north bearing")
        self.assertIn("NOT_VERIFIED_HERE", row["assertion_basis"])
        self.assertIn("never_an_independently_confirmed_defect", row["assertion_basis"])
        self.assertEqual((row["reported_quantity_field"], row["reported_quantity_value"]),
                         ("distance_m", "0.0035"))
        self.assertEqual(row["declared_quantity_unit"], "mm")
        self.assertEqual(row["property_count"], "3")
        self.assertEqual([unhex(key) for key in
                          report["properties_not_interpreted"][0]["property_keys_not_interpreted"]],
                         ["crack_width_mm", "observation", "surveyor_note"])
        self.assertIn("no verified defect", report["not_produced"])
        self.assertIn("no certificate", report["not_produced"])
        self.assertIn("OBSERVED_RECORD", row["record_basis"])

    def test_a_line_carries_a_count_and_never_a_chosen_vertex(self):
        line = {"type": "LineString", "coordinates": [[8.5, 47.3], [8.6, 47.4], [8.7, 47.5]]}
        rows, report, _ = rows_of(document([feature(identity=42, geometry=line,
                                                    properties={"observation": "route walked"})]))
        row = rows[0]
        self.assertEqual((row["geometry_type"], row["geometry_position_count"]), ("LineString", "3"))
        self.assertEqual((row["longitude_deg"], row["latitude_deg"], row["ellipsoidal_height_m"]),
                         ("", "", ""))
        self.assertIn("choosing_one_of_them_as_the_items_location", row["position_basis"])
        self.assertIn("a_line_is_a_geometry_not_an_inspected_asset", row["record_basis"])
        self.assertEqual(unhex(row["feature_id_hex"]), "42")
        collection = {"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [1, 2]},
            {"type": "MultiPoint", "coordinates": [[3, 4], [5, 6]]}]}
        nested = rows_of(document([feature(geometry=collection)]))[0][0]
        self.assertEqual((nested["geometry_type"], nested["geometry_position_count"]),
                         ("GeometryCollection", "3"))
        self.assertEqual(report["geometry_kinds"], {"LineString": 1})

    def test_missing_identity_geometry_and_properties_stay_missing(self):
        rows, _, _ = rows_of(document([
            dict(feature(identity=None), geometry=None),
            dict(feature(identity=None), properties=None)]))
        unlocated, bare = rows
        self.assertEqual(unlocated["geometry_type"], "UNLOCATED_NULL_GEOMETRY")
        self.assertEqual((unlocated["longitude_deg"], unlocated["geometry_position_count"]),
                         ("", ""))
        for row in rows:
            self.assertEqual(row["feature_id_hex"], "")
            self.assertIn("NO_IDENTITY_WAS_WRITTEN_AND_NONE_IS_INVENTED",
                          row["feature_identity_basis"])
        self.assertEqual(bare["property_count"], "")
        self.assertEqual(bare["reported_assertion_hex"], "")
        named = rows_of(document([feature(identity=None,
                                          properties={"asset_tag": "ROOF-7", "observation": "x"})]),
                        identity_property="asset_tag", assertion_property="observation")[0][0]
        self.assertEqual(unhex(named["feature_id_hex"]), "ROOF-7")
        self.assertIn("A_CALLER_NAMED_PROPERTY_KEY", named["feature_identity_basis"])

    def test_each_geometry_keeps_the_nesting_its_own_type_fixes(self):
        """A generic flatten would accept a line of one point and a ring that is not a ring."""
        for kind, coordinates, count in (
                ("MultiPoint", [], 0),
                ("MultiPoint", [[1, 2], [3, 4]], 2),
                ("LineString", [[0, 0], [1, 1]], 2),
                ("MultiLineString", [[[0, 0], [1, 1]], [[2, 2], [3, 3]]], 4),
                ("Polygon", [[[0, 0], [1, 0], [1, 1], [0, 0]]], 4),
                ("MultiPolygon", [[[[0, 0], [1, 0], [1, 1], [0, 0]]]], 4)):
            row = rows_of(document([feature(geometry={"type": kind,
                                                      "coordinates": coordinates})]))[0][0]
            self.assertEqual((row["geometry_type"], row["geometry_position_count"]),
                             (kind, str(count)), kind)
        for kind, coordinates in (
                ("LineString", [1, 2]),                       # one bare position, not an array
                ("LineString", [[1, 2]]),                     # "two or more positions"
                ("MultiPoint", [1, 2]),
                ("Polygon", [[[1, 2]]]),                      # a ring of one point
                ("Polygon", [[[0, 0], [1, 0], [1, 1]]]),      # "four or more positions"
                ("Polygon", [[[0, 0], [1, 0], [1, 1], [0, 1]]]),   # not closed
                ("Polygon", [[[0, 0], [1, 0], [1, 1], [0, 0, 5]]]),  # closed only in two of three
                ("MultiPolygon", [[[0, 0], [1, 0], [1, 1], [0, 0]]]),  # a polygon, not an array
                ("MultiLineString", [[0, 0], [1, 1]]),
                ("GeometryCollection", None)):
            with self.assertRaises(ValueError, msg=(kind, coordinates)):
                rows_of(document([feature(geometry={"type": kind, "coordinates": coordinates})]))
    def test_a_ring_closes_on_values_not_on_spelling(self):
        """`[1, 2]` and `[1.0, 2.0]` are the same corner; the RFC requires identical VALUES.

        The document is built as TEXT here on purpose: serialising a Python dict would normalise
        `0.00` to `0.0` and the two spellings would never reach the converter apart.
        """
        def ring_document(last):
            return ('{"type":"FeatureCollection","features":[{"type":"Feature","properties":null,'
                    '"geometry":{"type":"Polygon","coordinates":[[[1,2],[3,0],[3,3],'
                    + last + ']]}}]}').encode()

        for spelling in ("[1.0,2.0]", "[1e0,2e0]", "[1.00,2.000]", "[1,2]"):
            output, _ = convert(ring_document(spelling), CAPTURE_US, **DECLARED)
            row = list(csv.DictReader(io.StringIO(output)))[0]
            self.assertEqual(row["geometry_position_count"], "4", spelling)
        for spelling in ("[1,2.5]", "[1.0001,2]", "[1,2,0]"):
            with self.assertRaises(ValueError, msg=spelling):
                convert(ring_document(spelling), CAPTURE_US, **DECLARED)

    def test_a_crs_member_is_refused_wherever_it_could_qualify_the_coordinates(self):
        collection = document(crs={"type": "name", "properties": {"name": "EPSG:27700"}})
        on_feature = document([dict(feature(), crs={"type": "name"})])
        on_geometry = document([feature(geometry={"type": "Point", "coordinates": [1, 2],
                                                  "crs": {"type": "name"}})])
        nested = document([feature(geometry={"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [1, 2], "crs": {"type": "name"}}]})])
        for broken in (collection, on_feature, on_geometry, nested):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(broken)
        row = rows_of(document([feature(properties={"crs": "a note the writer left",
                                                    "observation": "x"})]))[0][0]
        self.assertEqual(row["property_count"], "2")

    def test_a_quantity_that_would_scale_to_zero_is_refused(self):
        """A conversion is a multiply: a source that reaches the reader can still reach it as 0."""
        def with_value(literal):
            body = json.dumps(document([feature(properties={"crack_width_mm": 1,
                                                            "observation": "x"})])).encode()
            return body.replace(b'"crack_width_mm": 1', b'"crack_width_mm": ' + literal)

        rows = list(csv.DictReader(io.StringIO(
            convert(with_value(b"1e-320"), CAPTURE_US, **DECLARED)[0])))
        self.assertNotEqual(float(rows[0]["reported_quantity_value"]), 0.0)
        for literal in (b"1e-323", b"5e-324"):
            with self.assertRaises(ValueError, msg=literal):
                convert(with_value(literal), CAPTURE_US, **DECLARED)
        with self.assertRaises(ValueError):
            convert(with_value(b"1e-400"), CAPTURE_US, **DECLARED)

    def test_a_frame_this_path_cannot_prove_is_refused(self):
        with self.assertRaises(ValueError):
            rows_of(document(crs={"type": "name", "properties": {"name": "EPSG:27700"}}))
        self.assertEqual(len(rows_of(document())[0]), 1)

    def test_unusable_documents_and_positions_are_refused(self):
        for broken in (document(type="Feature"), document(type=None),
                       {"type": "FeatureCollection"}, {"type": "FeatureCollection",
                                                       "features": {}},
                       document(features=[{"type": "Point"}]),
                       document(features=["not an object"])):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(broken)
        for geometry in ({"type": "Circle", "coordinates": [1, 2]},
                         {"type": "point", "coordinates": [1, 2]},
                         {"type": "Point", "coordinates": [1]},
                         {"type": "Point", "coordinates": [1, 2, 3, 4]},
                         {"type": "Point", "coordinates": [181, 2]},
                         {"type": "Point", "coordinates": [1, 91]},
                         {"type": "Point", "coordinates": ["8.5", 47.3]},
                         {"type": "Point", "coordinates": [True, 47.3]},
                         {"type": "GeometryCollection"}):
            with self.assertRaises(ValueError, msg=geometry):
                rows_of(document([feature(geometry=geometry)]))
        for properties in ("text", 7, []):
            with self.assertRaises(ValueError, msg=properties):
                rows_of(document([feature(properties=properties)]))
        for missing in ("geometry", "properties"):
            with self.assertRaises(ValueError, msg=missing):
                rows_of(document([without(feature(), missing)]))
        for identity in (True, [], {}):
            with self.assertRaises(ValueError, msg=identity):
                rows_of(document([feature(identity=identity)]))
        with self.assertRaises(ValueError):
            rows_of(document(), quantity_property="crack_width_mm")
        with self.assertRaises(ValueError):
            rows_of(document(), quantity_property="crack_width_mm", quantity_unit="furlong")
        with self.assertRaises(ValueError):
            convert(b'{"type":"FeatureCollection","type":"FeatureCollection","features":[]}',
                    CAPTURE_US)
        with self.assertRaises(ValueError):
            convert(json.dumps(document([feature(properties={"crack_width_mm": 1})])).encode()
                    .replace(b'"crack_width_mm": 1', b'"crack_width_mm": 1e-400'),
                    CAPTURE_US, **DECLARED)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_the_declared_meanings_reach_common_observations(self):
        line = {"type": "LineString", "coordinates": [[8.5, 47.3], [8.6, 47.4]]}
        _, _, output = rows_of(document([
            feature(),
            feature(identity="PIPE-2", geometry=line,
                    properties={"observation": "corridor recorded"})]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                     str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (2, "Unknown"))
        fields = [observation["fields"] for observation in common["observations"]]
        self.assertEqual([row["geometry_type"] for row in fields], ["Point", "LineString"])
        self.assertEqual(fields[0]["latitude_deg"], 47.397742)
        self.assertEqual(fields[0]["ellipsoidal_height_m"], 412.5)
        self.assertEqual(fields[0]["reported_quantity_value"], 0.0035)
        self.assertIsNone(fields[1]["latitude_deg"])
        for observation in common["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"])
        units = common["profile_units"]
        for name in ("feature_id_hex", "geometry_type", "ellipsoidal_height_m",
                     "reported_assertion_hex", "assertion_basis", "record_basis",
                     "geometry_position_count", "declared_quantity_unit", "property_count"):
            self.assertTrue(units.get(name), name)
        self.assertIn("NEVER_A_DEPTH", units["ellipsoidal_height_m"])
        self.assertIn("NOT_VERIFIED_HERE", units["assertion_basis"])
        self.assertIn("never_an_inspection_order", units["feature_index"])
        self.assertIn("no_defect_is_verified", units["record_basis"])


if __name__ == "__main__":
    unittest.main()
