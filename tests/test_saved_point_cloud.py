"""Authored LAS 1.4 bytes, not a scanner, a survey or a vendor file.

Every byte in this module is built here from the pinned ASPRS LAS Specification 1.4 - R16
(ASPRSorg/LAS@a51749076f74ff9ecc9f346236ce9009741350ab). No real point cloud, no device, no
flight and no network is involved: real records 0.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("point_cloud",
                                              ROOT / "scripts/convert_saved_point_cloud.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PROFILE = ROOT / "profiles/declared/saved-point-cloud/profile.toml"
CAPTURE = 1_700_000_000_000_000
WKT = b'COMPD_CS["authored",PROJCS["authored grid"],VERT_CS["authored vertical"]]\x00'


def vlr(record_id=module.WKT_COORDINATE_SYSTEM_RECORD, payload=WKT, user_id=b"LASF_Projection",
        description=b"authored", length_override=None):
    """One 54 byte variable length record header and its payload, little-endian."""
    length = len(payload) if length_override is None else length_override
    return struct.pack(module.VLR_HEADER, 0, user_id.ljust(16, b"\0"), record_id, length,
                       description.ljust(32, b"\0")) + payload


def geotiff_keys(entries=(), count=None, version=(1, 1, 0)):
    """A GeoKeyDirectoryTag payload: the four uint16 directory header, then the key entries.

    Each entry is `(key, location, count, value)` exactly as the pinned standard lays out its
    four unsigned shorts, so a test can place a value out of line or repeat a key.
    """
    entries = tuple(entries)
    declared = len(entries) if count is None else count
    payload = struct.pack("<4H", version[0], version[1], version[2], declared)
    return payload + b"".join(struct.pack("<4H", *entry) for entry in entries)


def in_place(key, value):
    """One SHORT key whose value the directory stores in place, which is what this path reads."""
    return (key, 0, 1, value)


def metre_projected(projected=32631, vertical=None, model=module.PROJECTED_2D,
                    horizontal_unit=module.METRE_UNIT_CODE,
                    vertical_unit=module.METRE_UNIT_CODE):
    """A projected 2D declaration in metres, the one subset this path resolves."""
    entries = [in_place(module.MODEL_TYPE_KEY, model)]
    if projected is not None:
        entries.append(in_place(module.PROJECTED_CRS_KEY, projected))
    if horizontal_unit is not None:
        entries.append(in_place(module.PROJ_LINEAR_UNITS_KEY, horizontal_unit))
    if vertical is not None:
        entries.append(in_place(module.VERTICAL_CRS_KEY, vertical))
    if vertical_unit is not None:
        entries.append(in_place(module.VERTICAL_UNITS_KEY, vertical_unit))
    return vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD, payload=geotiff_keys(entries))


def point(x=0, y=0, z=0, intensity=0, returns=1, total=1, scan_direction=0, edge=0,
          classification=1, synthetic=0, key_point=0, withheld=0, angle=0, user=0, source=1,
          gps_time=0.0, extra=b""):
    """One point data record: format 1 by default, or format 0 when the time tag is None.

    Zero is what the specification directs for the GPS time of an aggregate model system's
    point, so it is a legitimate default for an authored record rather than a stand-in.
    """
    bits = (returns & 0b111) | ((total & 0b111) << 3) | (scan_direction << 6) | (edge << 7)
    flags = ((classification & 0b11111) | (synthetic << 5) | (key_point << 6) | (withheld << 7))
    packed = struct.pack("<iiiHBBbBH", x, y, z, intensity, bits, flags, angle, user, source)
    if gps_time is not None:
        packed += struct.pack("<d", gps_time)
    return packed + extra


def las(points=(), records=(), point_format=1, point_length=None, version=(1, 4),
        signature=module.SIGNATURE, header_size=module.SPEC_HEADER_SIZE, global_encoding=1 << 4,
        scales=(0.01, 0.01, 0.001), offsets=(0.0, 0.0, 0.0),
        extents=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0), point_count=None, legacy_count=None,
        evlr_count=0, vlr_count=None, offset_override=None, creation=(251, 2026),
        file_source_id=7, tail=b""):
    """A whole authored file: public header block, variable length records, point records."""
    body = b"".join(records)
    payload = b"".join(points)
    minimum = module.POINT_FORMATS[point_format][1] if point_format in module.POINT_FORMATS else 20
    length = minimum if point_length is None else point_length
    offset = module.SPEC_HEADER_SIZE + len(body) if offset_override is None else offset_override
    count = len(points) if point_count is None else point_count
    legacy = count if legacy_count is None else legacy_count
    minimum_x, maximum_x, minimum_y, maximum_y, minimum_z, maximum_z = extents
    header = struct.pack(
        module.HEADER, signature, file_source_id, global_encoding, 0, 0, 0, b"\0" * 8,
        version[0], version[1], b"AUTHORED FIXTURE".ljust(32, b"\0"),
        b"musubi authored test".ljust(32, b"\0"), creation[0], creation[1], header_size,
        offset, len(records) if vlr_count is None else vlr_count, point_format, length,
        legacy, *([0] * 5), *scales, *offsets,
        maximum_x, minimum_x, maximum_y, minimum_y, maximum_z, minimum_z,
        0, 0, evlr_count, count, *([0] * 15))
    return header + body + payload + tail


def rows_of(raw, capture=CAPTURE):
    """Converted rows, the report, and the exact CSV text."""
    text, report = module.convert(raw, capture)
    return list(csv.DictReader(io.StringIO(text))), report, text


def read_common(case, csv_text, expected_rows):
    """Run the configured common reader, or skip explicitly - never bypass silently.

    Without MUSUBI_TELEMETRY_READER the actual-reader check is reported as a skip, so a run
    without the built reader cannot look like a passing common-output proof. With the variable
    set, a missing or failing reader is a failure rather than a silent bypass.
    """
    reader = os.environ.get("MUSUBI_TELEMETRY_READER")
    if not reader:
        case.skipTest("MUSUBI_TELEMETRY_READER not configured: actual common reader check skipped")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "observations.csv"
        path.write_text(csv_text)
        finished = subprocess.run([reader, str(PROFILE), str(path), "--allow-equal-time"],
                                  capture_output=True)
    case.assertEqual(finished.returncode, 0,
                     f"configured common reader failed: {finished.stderr[:400]!r}")
    common = json.loads(finished.stdout)
    case.assertEqual(common["main_rows"], expected_rows)
    return common


class SavedPointCloudTests(unittest.TestCase):
    def test_the_pinned_record_layouts_are_the_sizes_the_specification_states(self):
        self.assertEqual(struct.calcsize(module.HEADER), 375)
        self.assertEqual(module.VLR_HEADER_SIZE, 54)
        self.assertEqual(struct.calcsize(module.POINT_FORMATS[0][0]), 20)
        self.assertEqual(struct.calcsize(module.POINT_FORMATS[1][0]), 28)

    def test_an_authored_cloud_reaches_common_output_with_every_point_retained(self):
        points = (point(x=100, y=-250, z=12345, intensity=900, gps_time=213456.75,
                        classification=2, angle=-12, source=4),
                  point(x=101, y=-249, z=12300, intensity=100, gps_time=213456.80,
                        classification=5, returns=1, total=2, angle=0, source=4),
                  point(x=102, y=-248, z=12280, intensity=0, gps_time=213456.85,
                        classification=6, returns=2, total=2, angle=12, source=4))
        rows, report, text = rows_of(las(points=points, records=(vlr(),)))
        self.assertEqual(len(rows), 4)
        declaration, first, second, third = rows
        self.assertEqual(declaration["las_record_kind"], "FILE_DECLARATION")
        self.assertEqual(declaration["las_version_declared"], "1.4")
        self.assertEqual(declaration["point_count_declared"], "3")
        self.assertEqual(declaration["point_records_read"], "3")
        self.assertEqual(declaration["point_count_disposition"], "LEGACY_AND_CURRENT_COUNTS_AGREE")
        self.assertEqual([row["las_record_kind"] for row in rows[1:]], ["POINT_RECORD"] * 3)
        self.assertEqual([row["point_index"] for row in rows[1:]], ["0", "1", "2"])
        self.assertEqual((first["point_x"], first["point_y"], first["point_z"]),
                         ("1.00", "-2.50", "12.345"))
        self.assertEqual(second["point_x"], "1.01")
        self.assertEqual(third["point_z"], "12.280")
        self.assertIn("NOT_RESOLVED_HERE", first["point_coordinate_basis"])
        self.assertEqual((first["point_classification_value"], first["point_classification_name"]),
                         ("2", "GROUND"))
        self.assertEqual(first["point_gps_time_reported"], "213456.75")
        self.assertEqual(declaration["gps_time_type_declared"], "GPS_WEEK_TIME_DECLARED")
        self.assertIn("NEVER_A_FIX_STATUS", first["gps_time_basis"])
        self.assertIn("never_a_reconstruction", first["record_basis"])
        self.assertEqual(report["points_read"], 3)
        read_common(self, text, 4)

    def test_a_photogrammetric_point_keeps_its_synthetic_flag_apart_from_its_class(self):
        rows, _, _ = rows_of(las(points=(point(classification=2, synthetic=1, key_point=1),
                                         point(classification=2)), records=(vlr(),)))
        derived, observed = rows[1], rows[2]
        self.assertEqual(derived["point_classification_value"], "2")
        self.assertEqual(derived["point_classification_name"], "GROUND")
        self.assertEqual((derived["point_synthetic_flag"], derived["point_key_point_flag"]),
                         ("1", "1"))
        self.assertEqual(observed["point_classification_value"], "2")
        self.assertEqual((observed["point_synthetic_flag"], observed["point_key_point_flag"]),
                         ("0", "0"))
        self.assertIn("never_a_verified_surface", derived["point_classification_basis"].lower())

    def test_a_withheld_point_is_carried_with_its_flag_not_dropped(self):
        rows, report, _ = rows_of(las(points=(point(x=5, withheld=1), point(x=6)),
                                      records=(vlr(),)))
        self.assertEqual(report["points_read"], 2)
        self.assertEqual([row["point_withheld_flag"] for row in rows[1:]], ["1", "0"])
        self.assertEqual(rows[1]["point_x"], "0.05")

    def test_a_reserved_class_value_is_retained_and_never_renamed(self):
        rows, _, _ = rows_of(las(points=(point(classification=13), point(classification=10)),
                                 records=(vlr(),)))
        for row in rows[1:]:
            self.assertEqual(row["point_classification_name"], module.RESERVED_CLASS)
        self.assertEqual(rows[1]["point_classification_value"], "13")
        self.assertEqual(rows[2]["point_classification_value"], "10")

    def test_the_declared_coordinate_reference_is_retained_exactly_and_never_parsed(self):
        rows, report, _ = rows_of(las(points=(point(),), records=(vlr(),)))
        declaration = rows[0]
        self.assertEqual(declaration["coordinate_reference_kind"],
                         "WKT_DECLARED_RETAINED_NOT_PARSED")
        self.assertEqual(declaration["coordinate_reference_text_hex"], "hex:" + WKT.hex())
        self.assertEqual(declaration["coordinate_reference_key_count"], "")
        self.assertIn("NOT_PARSED", declaration["coordinate_reference_basis"])
        self.assertEqual(report["variable_length_records"][0]["record_id"], 2112)

    def test_a_declared_metre_projected_crs_reaches_common_output_as_metres(self):
        points = (point(x=100, y=-250, z=12345, gps_time=213456.75),
                  point(x=101, y=-249, z=12300, gps_time=213456.80))
        rows, report, text = rows_of(las(points=points, global_encoding=0,
                                         records=(metre_projected(vertical=5703),)))
        declaration = rows[0]
        self.assertEqual(declaration["coordinate_reference_kind"],
                         "GEOTIFF_DECLARED_QUALIFIED_SUBSET_READ")
        self.assertEqual(declaration["model_type_declared"], "MODEL_CRS_PROJECTED_2D_DECLARED")
        self.assertEqual((declaration["projected_crs_code"], declaration["vertical_crs_code"]),
                         ("32631", "5703"))
        self.assertEqual((declaration["horizontal_linear_unit_code"],
                          declaration["vertical_linear_unit_code"]), ("9001", "9001"))
        self.assertIn("EPSG_UOM_CODE_9001", declaration["horizontal_unit_basis"])
        self.assertIn("NOT_PINNED_HERE", declaration["horizontal_unit_basis"])
        self.assertIn("NOT_RESOLVED_TO_A_DATUM", declaration["vertical_unit_basis"])
        self.assertIn("NO_WGS84", declaration["axis_basis"])
        self.assertEqual([(row["point_x_m"], row["point_y_m"], row["point_z_m"])
                          for row in rows[1:]],
                         [("1.00", "-2.50", "12.345"), ("1.01", "-2.49", "12.300")])
        self.assertEqual([(row["point_x"], row["point_y"], row["point_z"])
                          for row in rows[1:]],
                         [("1.00", "-2.50", "12.345"), ("1.01", "-2.49", "12.300")])
        self.assertTrue(report["metre_horizontal_resolved"] and report["metre_vertical_resolved"])
        common = read_common(self, text, 3)
        carried = common["observations"][1]["fields"]
        self.assertEqual((carried["point_x_m"], carried["point_y_m"], carried["point_z_m"]),
                         (1.0, -2.5, 12.345))
        self.assertTrue(common["profile_units"]["point_x_m"].startswith("m_"))
        self.assertIn("not_resolved_here", common["profile_units"]["point_x"])

    def test_a_metre_unit_beside_an_epsg_code_is_reported_as_the_files_own_restatement(self):
        rows, _, _ = rows_of(las(points=(point(),), global_encoding=0,
                                 records=(metre_projected(projected=module.USER_DEFINED),)))
        self.assertIn("USER_DEFINED_PROJECTED_CRS", rows[0]["horizontal_unit_basis"])
        rows, _, _ = rows_of(las(points=(point(),), global_encoding=0,
                                 records=(metre_projected(projected=32631),)))
        self.assertIn("BESIDE_AN_EPSG_CRS_CODE", rows[0]["horizontal_unit_basis"])

    def test_a_unit_code_this_pin_does_not_size_stays_unresolved(self):
        rows, report, _ = rows_of(las(points=(point(x=100),), global_encoding=0,
                                      records=(metre_projected(horizontal_unit=9002,
                                                               vertical_unit=9002),)))
        self.assertEqual(rows[0]["horizontal_linear_unit_code"], "9002")
        self.assertIn("UNIT_UNRESOLVED", rows[0]["horizontal_unit_basis"])
        self.assertIn("UNIT_UNRESOLVED", rows[0]["vertical_unit_basis"])
        self.assertEqual((rows[1]["point_x_m"], rows[1]["point_z_m"]), ("", ""))
        self.assertEqual(rows[1]["point_x"], "1.00")
        self.assertFalse(report["metre_horizontal_resolved"] or report["metre_vertical_resolved"])

    def test_a_height_whose_unit_is_declared_without_a_vertical_crs_stays_on_an_unresolved_reference(self):
        rows, _, _ = rows_of(las(points=(point(z=2000),), global_encoding=0,
                                 records=(metre_projected(vertical=None),)))
        self.assertEqual(rows[0]["vertical_crs_code"], "")
        self.assertIn("NO_VERTICAL_CRS_DECLARED", rows[0]["vertical_unit_basis"])
        self.assertIn("NEVER_ELLIPSOIDAL_ORTHOMETRIC_OR_MEAN_SEA_LEVEL",
                      rows[0]["vertical_unit_basis"])
        self.assertEqual(rows[1]["point_z_m"], "2.000")

    def test_a_geographic_model_is_not_given_metre_axes(self):
        rows, _, _ = rows_of(las(points=(point(x=100),), global_encoding=0,
                                 records=(metre_projected(model=2, projected=None,
                                                          vertical_unit=None),)))
        self.assertEqual(rows[0]["model_type_declared"], "MODEL_CRS_GEOGRAPHIC_2D_DECLARED")
        self.assertIn("UNIT_UNRESOLVED", rows[0]["horizontal_unit_basis"])
        self.assertEqual(rows[1]["point_x_m"], "")
        rows, _, _ = rows_of(las(points=(point(x=100),), global_encoding=0,
                                 records=(metre_projected(projected=None),)))
        self.assertIn("UNIT_UNRESOLVED", rows[0]["horizontal_unit_basis"])
        self.assertEqual(rows[1]["point_x_m"], "")

    def test_conflicting_or_out_of_line_keys_stay_unresolved(self):
        conflicting = vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD,
                          payload=geotiff_keys((in_place(module.MODEL_TYPE_KEY, 1),
                                                in_place(module.PROJ_LINEAR_UNITS_KEY, 9001),
                                                in_place(module.PROJ_LINEAR_UNITS_KEY, 9002))))
        rows, _, _ = rows_of(las(points=(point(),), global_encoding=0, records=(conflicting,)))
        self.assertEqual(rows[0]["coordinate_reference_kind"],
                         "GEOTIFF_DECLARED_KEYS_CONFLICT_SO_THE_REFERENCE_IS_UNRESOLVED")
        self.assertEqual(rows[1]["point_x_m"], "")
        elsewhere = vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD,
                        payload=geotiff_keys((in_place(module.MODEL_TYPE_KEY, 1),
                                              in_place(module.PROJECTED_CRS_KEY, 32631),
                                              (module.PROJ_LINEAR_UNITS_KEY, 34737, 7, 0))))
        rows, _, _ = rows_of(las(points=(point(),), global_encoding=0, records=(elsewhere,)))
        self.assertEqual(rows[0]["coordinate_reference_kind"],
                         "GEOTIFF_DECLARED_A_QUALIFIED_KEY_LIVES_IN_A_RECORD_NOT_READ_HERE")
        self.assertEqual(rows[0]["horizontal_linear_unit_code"], "")
        self.assertEqual(rows[1]["point_x_m"], "")

    def test_an_unreadable_occurrence_blocks_its_key_even_when_another_is_in_place(self):
        for entries in (((module.PROJ_LINEAR_UNITS_KEY, 34737, 7, 0),
                         in_place(module.PROJ_LINEAR_UNITS_KEY, module.METRE_UNIT_CODE)),
                        (in_place(module.PROJ_LINEAR_UNITS_KEY, module.METRE_UNIT_CODE),
                         (module.PROJ_LINEAR_UNITS_KEY, 34737, 7, 0))):
            both = vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD,
                       payload=geotiff_keys((in_place(module.MODEL_TYPE_KEY, 1),
                                             in_place(module.PROJECTED_CRS_KEY, 32631)) + entries))
            rows, report, _ = rows_of(las(points=(point(x=100),), global_encoding=0,
                                          records=(both,)))
            self.assertEqual(rows[0]["coordinate_reference_kind"],
                             "GEOTIFF_DECLARED_A_QUALIFIED_KEY_LIVES_IN_A_RECORD_NOT_READ_HERE")
            self.assertEqual(rows[0]["horizontal_linear_unit_code"], "")
            self.assertIn("UNIT_UNRESOLVED", rows[0]["horizontal_unit_basis"])
            self.assertEqual(rows[1]["point_x_m"], "")
            self.assertFalse(report["metre_horizontal_resolved"])
            self.assertEqual(rows[0]["projected_crs_code"], "32631")
            self.assertEqual(rows[1]["point_x"], "1.00")

    def test_a_key_directory_header_the_pin_does_not_define_is_not_interpreted(self):
        qualified = (in_place(module.MODEL_TYPE_KEY, 1),
                     in_place(module.PROJECTED_CRS_KEY, 32631),
                     in_place(module.PROJ_LINEAR_UNITS_KEY, module.METRE_UNIT_CODE),
                     in_place(module.VERTICAL_UNITS_KEY, module.METRE_UNIT_CODE))
        for header in ((2, 1, 1), (1, 2, 0), (1, 1, 2)):
            record = vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD,
                         payload=geotiff_keys(qualified, version=header))
            rows, report, _ = rows_of(las(points=(point(x=100),), global_encoding=0,
                                          records=(record,)))
            self.assertEqual(rows[0]["coordinate_reference_kind"],
                             "GEOTIFF_KEY_DIRECTORY_HEADER_IS_NOT_A_SHAPE_THE_PIN_DEFINES_SO_"
                             "NOTHING_IN_IT_IS_INTERPRETED")
            self.assertEqual(rows[0]["coordinate_reference_key_count"], "4")
            self.assertEqual((rows[0]["model_type_code"], rows[0]["projected_crs_code"],
                              rows[0]["horizontal_linear_unit_code"]), ("", "", ""))
            self.assertEqual((rows[1]["point_x_m"], rows[1]["point_z_m"]), ("", ""))
            self.assertFalse(report["metre_horizontal_resolved"]
                             or report["metre_vertical_resolved"])
            self.assertEqual(rows[1]["point_x"], "1.00")
        for minor in (0, 1):
            record = vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD,
                         payload=geotiff_keys(qualified, version=(1, 1, minor)))
            rows, _, _ = rows_of(las(points=(point(x=100),), global_encoding=0,
                                     records=(record,)))
            self.assertEqual(rows[0]["coordinate_reference_kind"],
                             "GEOTIFF_DECLARED_QUALIFIED_SUBSET_READ")
            self.assertEqual(rows[1]["point_x_m"], "1.00")

    def test_a_key_directory_whose_length_contradicts_its_count_is_refused(self):
        short = vlr(record_id=module.GEOTIFF_KEY_DIRECTORY_RECORD,
                    payload=geotiff_keys((in_place(module.MODEL_TYPE_KEY, 1),), count=4))
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), global_encoding=0, records=(short,)), CAPTURE)
        self.assertIn("does not match the number of keys it declares", str(raised.exception))

    def test_a_wkt_string_is_never_mined_for_units(self):
        rows, _, _ = rows_of(las(points=(point(x=100),), records=(vlr(),)))
        self.assertEqual(rows[0]["coordinate_reference_kind"], "WKT_DECLARED_RETAINED_NOT_PARSED")
        self.assertEqual(rows[0]["coordinate_reference_text_hex"], "hex:" + WKT.hex())
        self.assertEqual(rows[0]["horizontal_linear_unit_code"], "")
        self.assertIn("UNIT_UNRESOLVED", rows[0]["horizontal_unit_basis"])
        self.assertEqual((rows[1]["point_x_m"], rows[1]["point_z_m"]), ("", ""))

    def test_a_reference_kind_that_contradicts_the_record_present_stays_unresolved(self):
        rows, _, _ = rows_of(las(points=(point(),), global_encoding=1 << 4,
                                 records=(metre_projected(),)))
        self.assertEqual(rows[0]["coordinate_reference_kind"],
                         "COORDINATE_REFERENCE_KIND_AND_RECORD_DISAGREE_SO_IT_IS_UNRESOLVED")
        self.assertEqual(rows[1]["point_x_m"], "")

    def test_both_a_wkt_and_a_geotiff_reference_are_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), global_encoding=0,
                               records=(vlr(), metre_projected())), CAPTURE)
        self.assertIn("not allow simultaneously", str(raised.exception))

    def test_a_reference_that_may_sit_in_an_extended_record_is_not_reported_as_absent(self):
        rows, _, _ = rows_of(las(points=(point(),), evlr_count=1))
        self.assertEqual(rows[0]["coordinate_reference_kind"],
                         "COORDINATE_REFERENCE_NOT_IN_THE_RECORDS_READ_AND_EXTENDED_RECORDS_EXIST")
        self.assertEqual(rows[0]["extended_record_count_declared"], "1")
        rows, _, _ = rows_of(las(points=(point(),)))
        self.assertEqual(rows[0]["coordinate_reference_kind"],
                         "COORDINATE_REFERENCE_ABSENT_FROM_WHAT_WAS_READ_THOUGH_REQUIRED")

    def test_disagreeing_point_counts_report_both_and_follow_the_pinned_rule(self):
        points = (point(x=1), point(x=2), point(x=3))
        rows, report, _ = rows_of(las(points=points, point_count=3, legacy_count=2,
                                      records=(vlr(),)))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["point_count_declared"], "3")
        self.assertEqual(rows[0]["legacy_point_count_declared"], "2")
        self.assertIn("DISAGREE", rows[0]["point_count_disposition"])
        self.assertIn("LEGACY_VALUE", rows[0]["point_count_disposition"])
        self.assertEqual(rows[0]["point_records_read"], "2")
        self.assertEqual(report["bytes_after_read_points"], 28)
        rows, _, _ = rows_of(las(points=points, legacy_count=0, records=(vlr(),)))
        self.assertEqual(rows[0]["point_count_disposition"],
                         "LEGACY_COUNT_ZERO_SO_BACKWARD_COMPATIBILITY_IS_NOT_MAINTAINED")
        self.assertEqual(rows[0]["point_records_read"], "3")

    def test_extra_record_bytes_are_counted_and_not_decoded(self):
        rows, report, _ = rows_of(las(points=(point(x=700, gps_time=1.5, extra=b"\x01\x02\x03\x04"),),
                                      point_length=32, records=(vlr(),)))
        self.assertEqual(rows[0]["point_record_extra_bytes"], "4")
        self.assertEqual(report["point_record_extra_bytes"], 4)
        self.assertEqual(rows[1]["point_x"], "7.00")
        self.assertEqual(rows[1]["point_gps_time_reported"], "1.5")

    def test_values_outside_the_specifications_stated_ranges_are_retained_with_that_named(self):
        rows, _, _ = rows_of(las(points=(point(angle=120, returns=3, total=2),
                                         point(angle=-90, returns=1, total=1)),
                                 records=(vlr(),)))
        self.assertEqual(rows[1]["point_scan_angle_rank_deg"], "120")
        self.assertEqual(rows[1]["point_scan_angle_disposition"],
                         "REPORTED_OUTSIDE_THE_SPECIFIED_RANGE_RETAINED")
        self.assertEqual(rows[1]["point_return_disposition"],
                         "REPORTED_OUTSIDE_THE_SPECIFIED_RELATION_RETAINED")
        self.assertEqual(rows[2]["point_scan_angle_disposition"],
                         "REPORTED_WITHIN_THE_SPECIFIED_RANGE")
        self.assertEqual(rows[2]["point_return_disposition"],
                         "REPORTED_WITHIN_THE_SPECIFIED_RELATION")

    def test_a_zero_scale_factor_is_refused_rather_than_collapsing_the_cloud(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(x=1), point(x=2)), scales=(0.01, 0.0, 0.001),
                               records=(vlr(),)), CAPTURE)
        self.assertIn("erase every stored coordinate", str(raised.exception))

    def test_a_coordinate_the_common_reader_could_not_carry_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(x=2),), scales=(1e308, 0.01, 0.001),
                               records=(vlr(),)), CAPTURE)
        self.assertIn("exceeds what the common reader can carry", str(raised.exception))

    def test_a_coordinate_that_cancels_to_a_residual_below_f64_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(x=9),), scales=(5e-324, 0.01, 0.001),
                               offsets=(-4.4e-323, 0.0, 0.0), records=(vlr(),)), CAPTURE)
        self.assertIn("underflows to zero", str(raised.exception))
        rows, _, _ = rows_of(las(points=(point(x=9),), scales=(5e-324, 0.01, 0.001),
                                 offsets=(-3.95e-323, 0.0, 0.0), records=(vlr(),)))
        self.assertNotEqual(float(rows[1]["point_x"]), 0.0)

    def test_the_row_bound_is_derived_from_the_output_bound_and_refuses_beyond_it(self):
        self.assertEqual(module.MAX_POINT_ROWS, module.MAX_OUTPUT // module.MAX_ROW_BYTES)
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), point_count=module.MAX_POINT_ROWS + 1,
                               legacy_count=module.MAX_POINT_ROWS + 1, records=(vlr(),)), CAPTURE)
        message = str(raised.exception)
        self.assertIn("this path's own bound", message)
        self.assertIn(str(module.MAX_POINT_ROWS), message)

    def test_a_declared_point_count_past_the_end_of_the_file_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), point_count=4, records=(vlr(),)), CAPTURE)
        self.assertIn("run past the end", str(raised.exception))

    def test_two_coordinate_reference_records_of_one_kind_are_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), records=(vlr(), vlr(payload=b"OTHER\x00"))),
                           CAPTURE)
        self.assertIn("more than one coordinate reference record", str(raised.exception))

    def test_a_variable_length_record_running_into_the_point_data_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), records=(vlr(length_override=4096),)), CAPTURE)
        self.assertIn("runs past the start of the point data", str(raised.exception))

    def test_refusals_name_this_paths_own_scope_not_a_global_verdict(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), point_format=6, records=(vlr(),)), CAPTURE)
        message = str(raised.exception)
        self.assertIn("this path decodes point data record formats 0 and 1", message)
        self.assertNotIn("unsupported", message)
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), version=(1, 2), records=(vlr(),)), CAPTURE)
        self.assertIn("this path reads the LAS 1.4 public header block only",
                      str(raised.exception))

    def test_a_file_that_is_not_a_las_file_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            module.convert(las(points=(point(),), signature=b"NOPE"), CAPTURE)
        self.assertIn("does not carry the LAS file signature", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            module.convert(b"LASF" + b"\0" * 40, CAPTURE)
        self.assertIn("shorter than a LAS public header block", str(raised.exception))

    def test_a_missing_configured_reader_fails_rather_than_bypassing_the_common_check(self):
        _, _, text = rows_of(las(points=(point(),), records=(vlr(),)))
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent-reader"
            previous = os.environ.get("MUSUBI_TELEMETRY_READER")
            os.environ["MUSUBI_TELEMETRY_READER"] = str(missing)
            try:
                with self.assertRaises(Exception):
                    read_common(self, text, 2)
            finally:
                if previous is None:
                    del os.environ["MUSUBI_TELEMETRY_READER"]
                else:
                    os.environ["MUSUBI_TELEMETRY_READER"] = previous

    def test_the_command_line_writes_the_csv_and_the_report_beside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "authored.las"
            source.write_bytes(las(points=(point(x=10, gps_time=3.25),), records=(vlr(),)))
            output = Path(directory) / "converted"
            self.assertEqual(module.main([str(source), str(output),
                                          "--capture-time-us", str(CAPTURE)]), 0)
            rows = list(csv.DictReader(io.StringIO((output / "observations.csv").read_text())))
            report = json.loads((output / "report.json").read_text())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["point_x"], "0.10")
        self.assertEqual(report["points_read"], 1)
        self.assertTrue(report["source_sha256"].startswith("sha256:"))
        self.assertEqual(report["reference"], module.REFERENCE)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
