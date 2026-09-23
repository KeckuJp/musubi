"""Declared GPX1.1 point fixtures, not new field recordings."""
import csv
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from xml.etree.ElementTree import ParseError
from scripts.convert_gpx_recorded import (convert, convert_boundary, convert_track,
                                          deere_points_to_gpx, DEERE_WGS84)

ROOT = Path(__file__).resolve().parents[1]


def document(body):
    return ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:x="urn:fixture" version="1.1" creator="authored fixture">' + body + '</gpx>').encode()


class GpxRecordedTests(unittest.TestCase):
    def test_bare_kml_document_reuses_the_same_semantics_as_the_zip_container(self):
        from scripts.convert_gpx_recorded import convert_kml, convert_kmz
        point = "139.25,35.5,12.5"
        body = ('<Placemark><ExtendedData><Point><coordinates>0,0</coordinates></Point></ExtendedData>'
            '<MultiGeometry><Point><coordinates>' + point + '</coordinates></Point>'
            '<LineString><altitudeMode>absolute</altitudeMode><coordinates>' + point + ' ' + point + '</coordinates></LineString>'
            '<Point><altitudeMode>relativeToGround</altitudeMode><coordinates>' + point + '</coordinates></Point>'
            '</MultiGeometry></Placemark>')
        document = ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + body
                    + '</Document></kml>').encode()
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr("doc.kml", document.decode())
            archive.writestr("unused.bin", b"retained resource")
        archive_bytes = packed.getvalue()

        bare_output, bare_report = convert_kml(document, 777)
        zip_output, zip_report = convert_kmz(archive_bytes, 777)

        strip = lambda text: [{k: v for k, v in row.items() if k != "source_sha256"}
                              for row in csv.DictReader(io.StringIO(text))]
        self.assertEqual(strip(bare_output), strip(zip_output))
        self.assertEqual((bare_report["points"], bare_report["selected_geometries"],
                          bare_report["ignored_altitudes"], bare_report["unselected_subtrees"]),
                         (4, 3, 1, 1))
        for key in ("points", "selected_geometries", "ignored_altitudes", "unselected_subtrees",
                    "clock", "evidence"):
            self.assertEqual(bare_report[key], zip_report[key])
        rows = list(csv.DictReader(io.StringIO(bare_output)))
        self.assertEqual(rows[0]["declared_altitude_m"], "")
        self.assertEqual(rows[1]["altitude_basis"], "SEA_LEVEL_DECLARED")
        self.assertEqual(rows[3]["altitude_basis"], "GROUND_RELATIVE_DECLARED")

        self.assertEqual(bare_report["source_sha256"], "sha256:" + hashlib.sha256(document).hexdigest())
        self.assertEqual(zip_report["source_sha256"], "sha256:" + hashlib.sha256(archive_bytes).hexdigest())
        self.assertNotEqual(bare_report["source_sha256"], zip_report["source_sha256"])
        self.assertEqual(bare_report["source_retention"], "unchanged source.kml required")
        self.assertEqual(zip_report["source_retention"], "unchanged source.kmz required")
        self.assertTrue(all(row["source_sha256"] == bare_report["source_sha256"] for row in rows))

        with self.assertRaisesRegex(ValueError, "KML2.2 namespace required"):
            convert_kml(document.replace(b"kml/2.2", b"kml/2.3"), 777)
        for bad in (b'<?xml version="1.0"?><!DOCTYPE kml SYSTEM "kml.dtd">' + document,
                    document[:len(document) // 2],
                    document.replace(b"absolute", b"relativeToSeaFloor"),
                    document.replace(point.encode(), b"181,0,1"),
                    document.replace(b"<LineString>", b'<LineString><altitudeOffset xmlns="http://www.google.com/kml/ext/2.2">2</altitudeOffset>', 1),
                    b"", b"<kml/>"):
            with self.subTest(bad=bad[:40]), self.assertRaises((ValueError, ParseError)):
                convert_kml(bad, 777)
        with self.assertRaises(ValueError):
            convert_kml(document, True)
        with self.assertRaises((ValueError, UnicodeDecodeError)):
            convert_kml(archive_bytes, 777)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); path = base / "in.kml"; out = base / "out"
            path.write_bytes(document)
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"), str(path),
                str(out), "--source-format", "kml22", "--capture-time-us", "777"],
                check=True, capture_output=True)
            self.assertEqual((out / "source.kml").read_bytes(), document)
            reader = os.environ.get("MUSUBI_TELEMETRY_READER")
            if not reader:
                self.skipTest("MUSUBI_TELEMETRY_READER is not configured; common proof NOT RUN")
            common = json.loads(subprocess.run([reader,
                str(ROOT / "profiles/declared/gpx-points/kmz-profile.toml"),
                str(out / "observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 4)
            self.assertEqual(common["observations"][1]["fields"]["declared_altitude_m"], 12.5)
            self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))

    def test_kmz_declared_geometry_reuses_common_output(self):
        from scripts.convert_gpx_recorded import convert_kmz
        def packed(body, duplicate=False):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.writestr("doc.kml", '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + body + '</Document></kml>')
                archive.writestr("unused.bin", b"retained resource")
                if duplicate: archive.writestr("second.kml", "not selected")
            return data.getvalue()
        for lon, lat in ((139.25, 35.5), (-73.5, -45.25)):
            point = f"{lon},{lat},12.5"
            body = ('<Placemark><ExtendedData><Point><coordinates>0,0</coordinates></Point></ExtendedData>'
                '<MultiGeometry><Point><coordinates>' + point + '</coordinates></Point>'
                '<LineString><altitudeMode>absolute</altitudeMode><coordinates>' + point + ' ' + point + '</coordinates></LineString>'
                '<Point><altitudeMode>relativeToGround</altitudeMode><coordinates>' + point + '</coordinates></Point>'
                '</MultiGeometry></Placemark>')
            raw = packed(body)
            output, report = convert_kmz(raw, 777)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["points"], report["selected_geometries"], report["ignored_altitudes"], report["unselected_subtrees"]), (4, 3, 1, 1))
            self.assertEqual(rows[0]["declared_altitude_m"], "")
            self.assertEqual(rows[1]["altitude_basis"], "SEA_LEVEL_DECLARED")
            self.assertEqual(rows[3]["altitude_basis"], "GROUND_RELATIVE_DECLARED")
            self.assertTrue(all(float(r["latitude_deg"]) == lat for r in rows))
            self.assertEqual([r["point_index"] for r in rows], ["0", "0", "1", "0"])
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); path = base / "in.kmz"; out = base / "out"
                path.write_bytes(raw)
                subprocess.run([sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"), str(path), str(out),
                    "--source-format", "kmz-kml22", "--capture-time-us", "777"], check=True, capture_output=True)
                self.assertEqual((out / "source.kmz").read_bytes(), raw)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/gpx-points/kmz-profile.toml"),
                        str(out / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 4)
                    self.assertEqual(common["observations"][1]["fields"]["declared_altitude_m"], 12.5)
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            for bad in (body.replace(point, "181,0,1"), body.replace(point, "0,nan,1"),
                        body.replace("absolute", "relativeToSeaFloor"), body.replace(point, "1,2"),
                        body.replace("<LineString>", '<LineString><altitudeOffset xmlns="http://www.google.com/kml/ext/2.2">2</altitudeOffset>', 1)):
                with self.assertRaises(ValueError): convert_kmz(packed(bad), 777)
            with self.assertRaises(ValueError): convert_kmz(packed(body, True), 777)
            with self.assertRaises(ValueError): convert_kmz(raw, True)
            with self.assertRaises(zipfile.BadZipFile): convert_kmz(raw[:30], 777)
        large, report = convert_kmz(packed('<LineString><coordinates>' + '1,2,3 ' * 20002 + '</coordinates></LineString>'), 777)
        self.assertEqual(report["points"], 20002)
        self.assertEqual(len(large.splitlines()), 20003)

    def test_wpl_declared_points_keep_altitude_basis_and_unselected_accounting(self):
        from scripts.convert_gpx_recorded import convert_wpl
        for latitude, longitude in ((35.5, 139.25), (-45.25, -73.5)):
            def line(frame=0, command=16, lat=latitude, lon=longitude, seq=0):
                return f"{seq}\t0\t{frame}\t{command}\t0\t1\t0\tnan\t{lat}\t{lon}\t12.5\t1\n"
            raw = ("QGC WPL 110\n# retained comment\n" + line() + line(3) + line(10)
                   + line(1) + line(command=999)).encode()
            output, report = convert_wpl(raw, 777)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_rows"], report["decoded_points"], report["unselected_rows"]), (5, 3, 2))
            self.assertEqual([r["altitude_basis"] for r in rows],
                ["MSL_DECLARED", "HOME_RELATIVE_DECLARED", "TERRAIN_RELATIVE_DECLARED"])
            self.assertTrue(all(float(r["latitude_deg"]) == latitude for r in rows))
            self.assertTrue(all(r["declared_sequence"] == "0" for r in rows))
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); path = base / "in.waypoints"; out = base / "out"
                path.write_bytes(raw)
                subprocess.run([sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"),
                    str(path), str(out), "--source-format", "qgc-wpl110", "--capture-time-us", "777"],
                    check=True, capture_output=True)
                self.assertEqual((out / "source.waypoints").read_bytes(), raw)
                self.assertEqual((out / "observations.csv").read_text(), output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/gpx-points/wpl-profile.toml"),
                        str(out / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 3)
                    self.assertEqual(common["observations"][1]["fields"]["declared_altitude_m"], 12.5)
                    self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
            for body in (line(lat=91), line(lon=181), line(lat="nan"), line(seq=65536),
                         line().replace("\t0\t0\t16", "\t2\t0\t16"),
                         line().rsplit("\t", 1)[0], line(1), line(command=999)):
                with self.assertRaises(ValueError):
                    convert_wpl(("QGC WPL 110\n" + body).encode(), 777)
            with self.assertRaises(ValueError): convert_wpl(raw.replace(b"110", b"100", 1), 777)
            with self.assertRaises(ValueError): convert_wpl(raw, True)

    @unittest.skipUnless(importlib.util.find_spec("shapefile"), "optional pinned shapefile decoder")
    def test_deere_point_projection_and_preserved_source(self):
        import shapefile
        for longitude, latitude in ((139.25, 35.5), (-73.25, 40.5)):
            buffers = {key: io.BytesIO() for key in ("shp", "shx", "dbf")}
            with shapefile.Writer(**buffers, shapeType=1) as writer:
                writer.field("IsoTime", "C", 24)
                writer.field("Other", "N", 10, 2)
                for index in range(2):
                    writer.point(longitude + index, latitude)
                    writer.record("2026-01-01T00:00:00.123Z", index + .5)
            parts = {"sample." + key: value.getvalue() for key, value in buffers.items()}
            parts["sample.prj"] = DEERE_WGS84.encode()
            parts["sample-Deere-Metadata.json"] = json.dumps({"Version": "1.0",
                "DataAttributes": [{"Name": "IsoTime"}, {"Name": "Other", "Unit": "unknown"}]}).encode()

            def packed(values):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    for name, value in values.items(): archive.writestr(name, value)
                return output.getvalue()

            raw = packed(parts)
            derived, projection = deere_points_to_gpx(raw)
            output, report = convert(derived, 777)
            common = self.common(output, 2)
            if common:
                self.assertEqual(common["observations"][0]["fields"]["latitude_deg"], latitude)
                self.assertEqual(common["observations"][-1]["fields"]["longitude_deg"], longitude + 1)
                self.assertEqual(common["observations"][0]["fields"]["point_time_unix_us"], 1767225600123000)
            self.assertEqual(projection["unconverted_fields"], ["Other"])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "in.zip"; path.write_bytes(raw)
                out = Path(directory) / "out"
                subprocess.run([sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"),
                    str(path), str(out), "--capture-time-us", "777", "--source-format", "deere-point-v1"],
                    capture_output=True, check=True)
                self.assertEqual((out / "source.zip").read_bytes(), raw)
                self.assertEqual((out / "source.gpx").read_bytes(), derived)
            bad = dict(parts); bad["sample.prj"] = b"unqualified CRS"
            with self.assertRaises(ValueError): deere_points_to_gpx(packed(bad))
            bad = dict(parts); bad["other"] = b"additional dataset"
            with self.assertRaises(ValueError): deere_points_to_gpx(packed(bad))
            bad = dict(parts); bad["sample.shp"] += b"unaccounted trailing bytes"
            with self.assertRaises(ValueError): deere_points_to_gpx(packed(bad))
            bad = dict(parts); dbf = bytearray(bad["sample.dbf"])
            dbf[int.from_bytes(dbf[8:10], "little")] = ord("*")
            bad["sample.dbf"] = bytes(dbf)
            with self.assertRaises(ValueError): deere_points_to_gpx(packed(bad))
            bad = dict(parts); dbf = bytearray(bad["sample.dbf"]); dbf[4:8] = (3).to_bytes(4, "little")
            bad["sample.dbf"] = bytes(dbf)
            with self.assertRaises(ValueError): deere_points_to_gpx(packed(bad))

    def common(self, output, expected, profile="profile.toml"):
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            return None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"; path.write_text(output)
            result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/gpx-points" / profile),
                str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
            self.assertEqual(result["main_rows"], expected)
            self.assertEqual(result["platform_domain"], "Unknown")
            self.assertEqual(result["observations"][0]["channel"], "event")
            return result

    def test_boundary_writer_two_inputs_preserve_local_meaning(self):
        for east in (1200.125, -43.25):
            source = f"$Boundary\nFalse\n2\n{east},12.75,1E-05\n0,-9.5,3.14159\nTrue\n0\nFalse\n1\n3,4,-0.5\n".encode()
            output, report = convert_boundary(source, 997)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["points"], report["rings"], report["empty_rings"]), (3, 3, 1))
            self.assertEqual(report["empty_ring_declarations"], [{"ring_index": 1, "drive_through_reported": 1}])
            self.assertEqual(float(rows[0]["easting_m"]), east)
            self.assertEqual(float(rows[0]["heading_rad"]), 0.00001)
            self.assertEqual(rows[-1]["ring_index"], "2")
            self.assertEqual(rows[-1]["ring_role"], "INNER_DECLARED")
            result = self.common(output, 3, "boundary-profile.toml")
            if result:
                self.assertEqual(result["observations"][0]["fields"]["easting_m"], east)
                self.assertEqual(result["observations"][0]["fields"]["northing_m"], 12.75)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "Boundary.txt"; path.write_bytes(source)
                out = Path(directory) / "out"
                subprocess.run([sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"),
                    str(path), str(out), "--capture-time-us", "997", "--source-format",
                    "agopen-boundary-21be26aa"], capture_output=True, check=True)
                self.assertEqual((out / "source.boundary.txt").read_bytes(), source)
                self.assertEqual((out / "observations.csv").read_text(), output)
        for raw in (b"$Boundary\n", b"$Boundary\nTrue\n2\n1,2,3\n",
                    b"$Boundary\nFalse\nFalse\n1\n1,2,3\n",
                    b"$Boundary\nTrue\n1\n1,200,2,3\n", b"$Boundary\nFalse\n-1\n",
                    b"$Boundary\nTrue\n1\nNaN,2,3\n", b"$Boundary\nTrue\n1\n1e-999,2,3\n"):
            with self.assertRaises(ValueError): convert_boundary(raw, 1)
        for capture in (True, -1, 2**63):
            with self.assertRaises(ValueError): convert_boundary(source, capture)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-reader proof needs the built shared reader; required CI supplies it")
    def test_track_writer_two_inputs_keep_declared_modes_points_and_order(self):
        """Saved TrackLines.txt guidance declarations: a plan, never a driven or executed path."""
        def track(name, heading, a, b, nudge, mode, visible, points):
            return "\n".join([name, heading, a, b, nudge, mode, visible, str(len(points)), *points])

        first = ("$TrackLines\n"
                 + track("AB east", "1.5708", "10.5,20.25", "110.5,20.25", "0", "2", "True", [])
                 + "\n"
                 + track("curve north", "0", "0,0", "0,50", "-1.25", "4", "False",
                         ["1.5,2.5,0.10000", "3.5,4.5,0.20000", "5.5,6.5,-0.30000"]) + "\n").encode()
        output, report = convert_track(first, 4242)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["tracks"], report["points"]), (2, 7))
        self.assertEqual((report["reference_points"], report["curve_points"]), (4, 3))
        self.assertEqual((report["empty_curve_tracks"], report["unrecognised_modes"]), (1, []))
        self.assertEqual([row["point_role"] for row in rows[:3]],
                         ["REFERENCE_A_DECLARED", "REFERENCE_B_DECLARED", "REFERENCE_A_DECLARED"])
        self.assertEqual([row["track_index"] for row in rows], ["0", "0", "1", "1", "1", "1", "1"])
        self.assertEqual([row["point_in_track"] for row in rows[2:]], ["0", "1", "2", "3", "4"])
        common = self.common(output, 7, "track-profile.toml")
        ab_a, ab_b, curve_a = (o["fields"] for o in common["observations"][:3])
        curve_points = [o["fields"] for o in common["observations"][4:]]
        self.assertEqual((ab_a["easting_m"], ab_a["northing_m"]), (10.5, 20.25))
        self.assertEqual((ab_b["easting_m"], ab_b["northing_m"]), (110.5, 20.25))
        self.assertEqual(ab_a["track_mode_reported"], "AB")
        self.assertEqual(ab_a["track_geometry_basis"], "REFERENCE_LINE_A_TO_B_DECLARED")
        self.assertEqual(ab_a["track_heading_rad"], 1.5708)
        self.assertEqual(ab_a["track_curve_point_count"], 0)
        self.assertEqual(ab_a["track_visible_reported"], 1)
        self.assertEqual(bytes.fromhex(ab_a["track_name_hex"][4:]).decode(), "AB east")
        self.assertIsNone(ab_a["heading_rad"])
        self.assertIsNone(ab_b["heading_rad"])
        self.assertEqual(curve_a["track_mode_reported"], "Curve")
        self.assertEqual(curve_a["track_geometry_basis"], "CURVE_POINT_LIST_DECLARED")
        self.assertEqual(curve_a["track_nudge_distance_m"], -1.25)
        self.assertEqual(curve_a["track_visible_reported"], 0)
        self.assertEqual([(p["easting_m"], p["northing_m"], p["heading_rad"]) for p in curve_points],
                         [(1.5, 2.5, 0.1), (3.5, 4.5, 0.2), (5.5, 6.5, -0.3)])
        for observation in common["observations"]:
            fields = observation["fields"]
            self.assertEqual(fields["coordinate_basis"], "LOCAL_FIELD_ORIGIN_UNRESOLVED_NOT_WGS84")
            self.assertEqual(fields["record_time_us"], 4242)
            self.assertEqual(observation["clock_basis"], "Unknown")
        units = common["profile_units"]
        self.assertIn("stores_no_origin", units["coordinate_basis"])
        self.assertIn("never_a_measured_vehicle_heading", units["track_heading_rad"])
        self.assertIn("never_an_executed_movement", units["track_nudge_distance_m"])

        second = ("$TrackLines\n"
                  + track("pivot", "-2.5", "-7.25,-8.5", "1E-05,9", "0.5", "64", "True",
                          ["-1.5,-2.5,3.14159", "2E-05,0,-1.00000"]) + "\n"
                  + track("odd", "0.25", "5,5", "6,6", "0", "7", "False", ["7,8,0.50000"]) + "\n"
                  + track("outer", "3", "0,0", "1,1", "-0.125", "8", "True", []) + "\n").encode()
        output, report = convert_track(second, 99)
        self.assertEqual((report["tracks"], report["points"]), (3, 9))
        self.assertEqual(report["unrecognised_modes"], [{"track_index": 1, "mode_code": 7}])
        common = self.common(output, 9, "track-profile.toml")
        fields = [o["fields"] for o in common["observations"]]
        self.assertEqual(fields[0]["track_mode_reported"], "waterPivot")
        self.assertEqual(fields[0]["track_geometry_basis"], "CURVE_POINT_LIST_DECLARED_AS_PIVOT_TRACK")
        self.assertEqual((fields[0]["easting_m"], fields[0]["northing_m"]), (-7.25, -8.5))
        self.assertEqual(fields[1]["easting_m"], 1e-05)
        self.assertEqual((fields[2]["easting_m"], fields[2]["heading_rad"]), (-1.5, 3.14159))
        unknown = [row for row in fields if row["track_index"] == 1]
        self.assertEqual({row["track_mode_code"] for row in unknown}, {7})
        self.assertEqual({row["track_mode_reported"] for row in unknown}, {"UNKNOWN_7"})
        self.assertEqual({row["track_geometry_basis"] for row in unknown},
                         {"GEOMETRY_ROLE_UNQUALIFIED_UNRECOGNISED_MODE"})
        self.assertEqual([row["easting_m"] for row in unknown], [5.0, 6.0, 7.0])
        self.assertEqual(fields[-1]["track_mode_reported"], "bndTrackOuter")
        self.assertEqual(fields[-1]["track_geometry_basis"],
                         "CURVE_POINT_LIST_DECLARED_FROM_OUTER_BOUNDARY")
        self.assertEqual({row["source_sha256"] for row in fields},
                         {"sha256:" + hashlib.sha256(second).hexdigest()})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TrackLines.txt"
            path.write_bytes(second)
            out = Path(directory) / "out"
            subprocess.run([sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"),
                str(path), str(out), "--capture-time-us", "99", "--source-format",
                "agopen-track-21be26aa"], capture_output=True, check=True)
            self.assertEqual((out / "source.tracklines.txt").read_bytes(), second)
            self.assertEqual((out / "observations.csv").read_text(), output)

        many = ["$TrackLines", track("long curve", "0.75", "0,0", "0,1", "0", "4", "True",
                                     [f"{index}.5,{index}.25,0.00100" for index in range(250)]), ""]
        output, report = convert_track("\n".join(many).encode(), 7)
        self.assertEqual((report["tracks"], report["curve_points"]), (1, 250))
        common = self.common(output, 252, "track-profile.toml")
        self.assertEqual(common["observations"][-1]["fields"]["easting_m"], 249.5)
        self.assertEqual(common["observations"][-1]["fields"]["point_in_track"], 251)

    def test_track_refusals_keep_unreadable_shapes_out_of_the_output(self):
        body = track_body = ("goodname\n0.5\n1,2\n3,4\n0\n2\nTrue\n1\n5,6,0.1\n")
        self.assertEqual(convert_track(("$TrackLines\n" + body).encode(), 1)[1]["tracks"], 1)
        for raw in (
                b"$TrackLines\n",                                   # header only, no track
                b"$Boundary\nname\n0\n1,2\n3,4\n0\n2\nTrue\n0\n",   # wrong header
                b"$TwolTracks\nname\n0\n1,2\n3,4\n0\n2\nTrue\n0\nTrue\n1.5\n",
                ("$TrackLines\n\n" + body).encode(),                # empty name line
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2\nTrue\n",    # truncated block
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2\nTrue\n2\n5,6,0.1\n",   # count exceeds lines
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2\nyes\n0\n",  # not a declared boolean
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2.5\nTrue\n0\n",  # non-integer mode
                b"$TrackLines\nname\n0\n1,2,3\n3,4\n0\n2\nTrue\n0\n",  # three cells in point A
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2\nTrue\n1\n5,6\n",  # curve point missing heading
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2\nTrue\n1\nNaN,6,0.1\n",   # non-finite
                b"$TrackLines\nname\n0\n1,2\n3,4\n0\n2\nTrue\n1\n1e-999,6,0.1\n",
                b"$TrackLines\nname\nabc\n1,2\n3,4\n0\n2\nTrue\n0\n"):  # unparsable heading
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                convert_track(raw, 1)
        for capture in (True, -1, 2**63, 1.5):
            with self.assertRaises(ValueError):
                convert_track(("$TrackLines\n" + track_body).encode(), capture)

    def test_three_point_roles_segment_boundaries_and_unset_time(self):
        for latitude in (35.5, -12.75):
            source = document(f'''<!-- retained comment -->
              <metadata><name>retained document</name></metadata>
              <wpt lat="{latitude}" lon="139"><name>点,one</name><extensions><x:q>9</x:q></extensions></wpt>
              <rte><rtept lat="1" lon="2"><ele>-3</ele><time>2026-01-02T01:00:00.123456+01:00</time></rtept></rte>
              <rte/><trk><trkseg><trkpt lat="3" lon="4"><time>2026-01-02T00:00:00.123456Z</time></trkpt></trkseg>
              <trkseg/><trkseg><trkpt lat="5" lon="6"/></trkseg></trk><trk/>''')
            output, report = convert(source, 999)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["points"], report["timed_points"], report["untimed_points"]), (4, 2, 2))
            self.assertEqual((report["routes"], report["empty_routes"], report["tracks"], report["empty_tracks"],
                              report["segments"], report["empty_segments"]), (2, 1, 2, 1, 3, 1))
            self.assertEqual(rows[1]["point_time_unix_us"], rows[2]["point_time_unix_us"])
            self.assertEqual(rows[0]["point_time_unix_us"], "")
            self.assertEqual(rows[3]["segment_index"], "2")
            self.assertEqual(rows[3]["point_in_segment"], "0")
            self.assertEqual(bytes.fromhex(rows[0]["point_name_hex"][4:]).decode(), "点,one")
            common = self.common(output, 4)
            if common:
                self.assertEqual(common["observations"][0]["fields"]["latitude_deg"], latitude)
                self.assertEqual(common["observations"][1]["fields"]["elevation_m"], -3)
                self.assertEqual(common["observations"][0]["fields"]["record_time_us"], 999)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input.gpx"; path.write_bytes(source)
                out = Path(directory) / "out"
                command = [sys.executable, str(ROOT / "scripts/convert_gpx_recorded.py"),
                           str(path), str(out), "--capture-time-us", "999"]
                subprocess.run(command, capture_output=True, check=True)
                self.assertEqual((out / "source.gpx").read_bytes(), source)
                self.assertEqual((out / "observations.csv").read_text(), output)
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_normal_sized_track_to_common_output_without_time_invention(self):
        count = 30000
        source = document('<trk><trkseg>' + '<trkpt lat="35.25" lon="139.125"/>' * count + '</trkseg></trk>')
        output, report = convert(source, 1000)
        self.assertEqual(report["points"], count)
        self.assertEqual(report["untimed_points"], count)
        common = self.common(output, count)
        if common:
            self.assertEqual(common["observations"][-1]["fields"]["point_index"], count - 1)
            self.assertIsNone(common["observations"][-1]["fields"]["point_time_unix_us"])

    def test_important_wrong_layout_quantity_time_and_xml_rejections(self):
        for body in ('', '<rte/>', '<wpt lat="90.000000000000000001" lon="0"/>',
                     '<wpt lat="0" lon="180"/>', '<wpt lat="NaN" lon="0"/>',
                     '<wpt lat="0"/>', '<wpt lat="0" lon="0"><ele>Inf</ele></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-02-30T00:00:00Z</time></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-01-01T00:00:00</time></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-01-01T00:00:00.1234567Z</time></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-01-01T00:00:00+14:01</time></wpt>',
                     '<wpt lat="0" lon="0"><ele>1</ele><ele>2</ele></wpt>',
                     '<trk><trkpt lat="0" lon="0"/></trk>'):
            with self.assertRaises(ValueError): convert(document(body), 1)
        good = document('<wpt lat="-90" lon="-180"/>')
        for source in (good.replace(b'version="1.1"', b'version="1.0"'),
                       b'<!DOCTYPE gpx [<!ENTITY x "bad">]>' + good, good.replace(b'/GPX/1/1', b'/GPX/1/0')):
            with self.assertRaises(ValueError): convert(source, 1)
        for capture in (None, True, -1, 2**63):
            with self.assertRaises(ValueError): convert(good, capture)
        with self.assertRaises(ParseError): convert(good[:-1], 1)


if __name__ == "__main__":
    unittest.main()
