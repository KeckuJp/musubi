import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.convert_opcua_recorded import convert, convert_sensorthings

ROOT = Path(__file__).resolve().parents[1]


AEMP_NS = "http://www.jcmanet.or.jp/english2017/ISO/15143/-3/20190501"
PAGE_XML = (f'<CumulativeOperatingHoursMessages xmlns="{AEMP_NS}"><Links><rel>next</rel>'
    '<href>https://example.invalid/next</href></Links>'
    '<CumulativeOperatingHours datetime="2026-01-01T00:00:00Z"><Hour>1</Hour></CumulativeOperatingHours>'
    "</CumulativeOperatingHoursMessages>")
FLEET_A = (f'<Fleet xmlns="{AEMP_NS}" version="1" snapshotTime="2026-03-04T05:06:07Z">'
    '<Links><rel>self</rel><href>https://example.invalid/fleet</href></Links>'
    '<Equipment><EquipmentHeader><OEMName>authored-oem</OEMName><Model>authored-model</Model>'
    '<EquipmentID>declared-unit-1</EquipmentID><SerialNumber>SN0000000000001</SerialNumber>'
    '<PIN>PIN000000000001</PIN></EquipmentHeader>'
    '<CumulativeIdleHours datetime="2026-03-04T05:00:00Z"><Hour>5</Hour></CumulativeIdleHours>'
    '<CumulativeOperatingHours datetime="2026-03-04T05:00:00Z"><Hour>1234.5</Hour>'
    "</CumulativeOperatingHours></Equipment>"
    '<Equipment><EquipmentHeader><EquipmentID>declared-unit-2</EquipmentID></EquipmentHeader>'
    "<Distance><Odometer>12</Odometer></Distance></Equipment>"
    "<future>retained</future></Fleet>")
FLEET_B = (f'<Fleet xmlns="{AEMP_NS}" version="4294967295" snapshotTime="2026-08-09T10:11:12.500000+02:00">'
    '<Links><rel>self</rel><href>https://example.invalid/fleet/b</href></Links>'
    '<Links><rel>next</rel><href>https://example.invalid/fleet/b?page=2</href></Links>'
    '<Equipment><EquipmentHeader><UnitInstallDateTime>2019-07-01T00:00:00+09:00</UnitInstallDateTime>'
    '<SerialNumber>B</SerialNumber><vendorExtra>kept</vendorExtra></EquipmentHeader>'
    '<CumulativeOperatingHours datetime="2026-08-09T09:00:00+02:00"><Hour>0.25</Hour>'
    "</CumulativeOperatingHours></Equipment>"
    '<Equipment><EquipmentHeader><PIN>PIN00000000000AB</PIN></EquipmentHeader>'
    '<CumulativeOperatingHours datetime="2026-08-09T08:30:00Z"><Hour>9999.99</Hour>'
    "</CumulativeOperatingHours></Equipment>"
    '<Equipment><EquipmentHeader><Model>no-hours-model</Model></EquipmentHeader>'
    '<FuelRemaining><Percent>50.00</Percent></FuelRemaining>'
    "<EngineStatus><Running>false</Running></EngineStatus></Equipment>"
    "</Fleet>")


def fleet_document(identifiers, hour="1.5"):
    """Authored Fleet snapshot with one hour-reporting equipment per identifier."""
    body = "".join(f"<Equipment><EquipmentHeader><EquipmentID>{name}</EquipmentID></EquipmentHeader>"
                   f'<CumulativeOperatingHours datetime="2026-03-04T05:00:00Z"><Hour>{hour}</Hour>'
                   "</CumulativeOperatingHours></Equipment>" for name in identifiers)
    return (f'<Fleet xmlns="{AEMP_NS}" version="2" snapshotTime="2026-03-04T05:06:07Z">'
            "<Links><rel>self</rel><href>https://example.invalid/fleet</href></Links>"
            + body + "</Fleet>")


def record(value=12.5, code=0):
    return dict(capture_time_us=15, node_id="nsu=urn:synthetic;s=voltage",
        data_value=dict(UaType=11, Value=value, Status={"Code": code},
            SourceTimestamp="2026-01-01T00:00:00.123456789Z", SourcePicoseconds=4,
            ServerTimestamp="2026-01-01T00:00:01Z"), vendor={"kept": True})


class DataValues(unittest.TestCase):
    def test_har_outcomes_reuse_all_entries_without_exposing_credentials(self):
        from scripts.convert_opcua_recorded import convert_har_status
        def entry(status, stamp='2026-01-01T00:00:00Z', elapsed=250):
            return dict(startedDateTime=stamp, time=elapsed,
                request={'url': 'https://example.invalid/secret-token', 'headers': [{'value': 'secret-token'}]},
                response={'status': status, 'content': {'text': 'secret-token'}}, unknown='secret-token')
        for statuses in ((500, 200, 401, 0), (503, 204, 302, 599)):
            entries = [entry(statuses[0], '2026-01-01T00:00:01Z'),
                       *[entry(status, elapsed=12.5) for status in statuses[1:]]]
            text = json.dumps({'log': {'version': '1.2', 'entries': entries}})
            output, report = convert_har_status(text)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([int(r['har_entry_index']) for r in rows], [1, 2, 3, 0])
            self.assertEqual([int(r['http_status_reported']) for r in rows], [*statuses[1:], statuses[0]])
            self.assertEqual(float(rows[0]['request_elapsed_reported_s']), .0125)
            self.assertTrue(report['order_changed'])
            self.assertEqual((report['records'], report['source_entries']), (4, 4))
            self.assertNotIn('secret-token', output + json.dumps(report))
            if 0 in statuses: self.assertEqual(rows[2]['http_status_class'], 'UNQUALIFIED_NO_HTTP_STATUS')
        for field, value in (('status', True), ('status', 99), ('status', 600), ('time', -1), ('time', True)):
            item = entry(200)
            (item['response'] if field == 'status' else item)[field] = value
            with self.assertRaises(ValueError):
                convert_har_status(json.dumps({'log': {'version': '1.2', 'entries': [item]}}))
        for document in ({'version': '1.3', 'entries': [entry(200)]}, {'version': '1.2', 'entries': []}):
            with self.assertRaises(ValueError): convert_har_status(json.dumps({'log': document}))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / 'input.har'; output = base / 'out.csv'; report = base / 'report.json'
            source.write_text(json.dumps({'log': {'version': '1.2', 'entries': [entry(503)] * 10000}}))
            subprocess.run([sys.executable, '-m', 'scripts.convert_opcua_recorded', str(source), str(output),
                str(report), '--source-format', 'har-1.2-response-status'], cwd=ROOT, check=True, capture_output=True)
            with output.open() as stream: self.assertEqual(sum(1 for _ in csv.DictReader(stream)), 10000)
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/opcua-recorded/profile.toml'), str(output),
                    '--allow-equal-time'], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 10000)
                self.assertEqual(common['observations'][-1]['fields']['request_elapsed_reported_s'], .25)
                self.assertEqual(common['observations'][-1]['clock_basis'], 'Unknown')

    def test_onvif_saved_status_reuses_coordinates_clock_and_common_reader(self):
        from scripts.convert_opcua_recorded import convert_onvif_status
        from scripts.convert_gpx_recorded import utc_microseconds
        prefix = ('<p:GetStatusResponse xmlns:p="http://www.onvif.org/ver20/ptz/wsdl" '
                  'xmlns:t="http://www.onvif.org/ver10/schema"><p:PTZStatus>')
        position = ('<t:Position><t:PanTilt x="-0.5" y="1" '
            'space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/>'
            '<t:Zoom x="0.25" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"/></t:Position>')
        clock = '<t:UtcTime>2026-01-01T00:00:00.123456Z</t:UtcTime>'
        end = '</p:PTZStatus></p:GetStatusResponse>'
        first = prefix + position + '<t:MoveStatus><t:PanTilt>MOVING</t:PanTilt><t:Zoom>IDLE</t:Zoom></t:MoveStatus>' + clock + '<t:Future>preserved</t:Future>' + end
        second = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>' +
                  first.replace('x="-0.5"', 'x="0.75"').replace('x="0.25"', 'x="0"') + '</s:Body></s:Envelope>')
        for text, pan, zoom in ((first, -.5, .25), (second, .75, 0)):
            output, report = convert_onvif_status(text, "declared-profile", 55)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row["pan_tilt_x_normalized"]), pan)
            self.assertEqual(float(row["zoom_x_normalized"]), zoom)
            self.assertEqual(row["pan_tilt_move_reported"], "MOVING")
            self.assertEqual(int(row["status_time_us"]), utc_microseconds("2026-01-01T00:00:00.123456Z"))
            self.assertEqual(report["source_response"], text)
            self.assertEqual(report["uninterpreted_status_elements"], 1)
            self.assertEqual(report["interpreted_coordinates"], 3)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory); source = path / "response.xml"; source.write_text(text)
                subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded", str(source),
                    str(path / "out.csv"), str(path / "report.json"), "--source-format", "onvif-ptz-status-1812",
                    "--profile-id", "declared-profile", "--capture-time-us", "55"], cwd=ROOT, check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path / "out.csv")], check=True, capture_output=True).stdout)["observations"]
                    self.assertEqual(len(common), 1)
                    self.assertEqual(common[0]["fields"]["pan_tilt_x_normalized"], pan)
                    self.assertEqual(common[0]["clock_basis"], "Unknown")
                    self.assertIsNone(common[0]["anchor_unix_us"])
        missing = prefix + '<t:MoveStatus><t:Zoom>UNKNOWN</t:Zoom></t:MoveStatus><t:Error>unavailable</t:Error>' + clock + end
        row = next(csv.DictReader(io.StringIO(convert_onvif_status(missing, "p", 0)[0])))
        self.assertEqual(row["position_disposition"], "POSITION_NOT_REPORTED")
        self.assertEqual(row["zoom_x_normalized"], "")
        for text in (first.replace('x="-0.5"', 'x="-1.1"'), first.replace('x="0.25"', 'x="NaN"'),
                     first.replace('PositionGenericSpace', 'other'), first.replace(clock, clock + clock),
                     first.replace(clock, ''), first.replace('MOVING', 'done'),
                     first.replace('MOVING', 'UNKNOWN'),
                     first.replace('GetStatusResponse', 'GetStatus'), first[:-12],
                     '<!DOCTYPE x [<!ENTITY x "bad">]>' + first,
                     first.replace('.123456Z', '.1234567Z')):
            with self.assertRaises(ValueError):
                convert_onvif_status(text, "p", 0)

    def test_aemp_hours_reuses_xml_json_clock_and_common_output(self):
        from scripts.convert_opcua_recorded import convert_aemp_hours
        ns = "http://www.jcmanet.or.jp/english2017/ISO/15143/-3/20190501"
        xml = (f'<CumulativeOperatingHoursMessages xmlns="{ns}"><Links><rel>next</rel>'
            '<href>https://example.invalid/next</href></Links>'
            '<CumulativeOperatingHours datetime="2026-01-01T00:00:00Z"><Hour>12.25</Hour></CumulativeOperatingHours>'
            '<CumulativeOperatingHours datetime="2026-01-01T00:00:01Z"><Hour>0</Hour></CumulativeOperatingHours>'
            '<future>retained</future></CumulativeOperatingHoursMessages>')
        source = dict(Links=[dict(rel="self", href="https://example.invalid/page")],
            CumulativeOperatingHours=[dict(Hour=3.5, datetime="2026-01-02T00:00:00Z", future=True)])
        for text, layout, seconds in ((xml, "iso-hours-xml-20190501", [44100, 0]),
                                      (json.dumps(source), "takeuchi-v3-hours-json", [12600])):
            output, report = convert_aemp_hours(text, "declared-equipment", 55, layout)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([float(r["engine_operating_seconds_reported"]) for r in rows], seconds)
            self.assertEqual(report["records"], len(seconds))
            self.assertEqual(report["collection_scope"], "SUPPLIED_PAGE_ONLY")
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
            self.assertEqual(rows[0]["record_time_us"], "55")
            self.assertEqual(report["unsupported_root_elements"], 1 if layout.startswith("iso") else 0)
            with tempfile.TemporaryDirectory() as tmp:
                paths = [Path(tmp) / n for n in ("input", "out.csv", "report.json")]
                paths[0].write_text(text)
                subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded", *map(str, paths),
                    "--source-format", layout, "--equipment-id", "declared-equipment", "--capture-time-us", "55"],
                    cwd=ROOT, check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"), str(paths[1]),
                        "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], len(seconds))
                    self.assertEqual(common["observations"][0]["fields"]["engine_operating_seconds_reported"], seconds[0])
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
        for bad in (xml.replace(ns, "urn:wrong"), xml.replace('<Hour>12.25</Hour>', '<Hour>-1</Hour>'),
                    xml.replace('<Hour>12.25</Hour>', '<Hour>1</Hour><Hour>2</Hour>'),
                    xml.replace('datetime="2026-01-01T00:00:00Z"', ''),
                    '<!DOCTYPE x [<!ENTITY a "b">]>' + xml):
            with self.assertRaises((ValueError, KeyError)):
                convert_aemp_hours(bad, "x", 55, "iso-hours-xml-20190501")
        for bad in (True, -1, "12", None):
            changed = dict(source, CumulativeOperatingHours=[dict(Hour=bad, datetime="2026-01-01T00:00:00Z")])
            with self.assertRaises(ValueError): convert_aemp_hours(json.dumps(changed), "x", 55, "takeuchi-v3-hours-json")
        with self.assertRaises(ValueError): convert_aemp_hours(xml, "", 55, "iso-hours-xml-20190501")
        with self.assertRaises(ValueError): convert_aemp_hours(xml, "x", True, "iso-hours-xml-20190501")

    def test_fleet_snapshot_equipment_hours_reuse_seconds_and_reported_provenance(self):
        """Authored ISO15143-3 Fleet snapshots; no live AEMP API, real equipment or meter accuracy."""
        from scripts.convert_opcua_recorded import (convert_aemp_hours, FLEET_HOURS_FORMAT,
                                                    MAX_FLEET_EQUIPMENT)
        for text, expected, counts in ((FLEET_A, [4444200.0, None], (2, 1, 1, 2, 0, 1)),
                                       (FLEET_B, [900.0, 35999964.0, None], (3, 2, 1, 2, 1, 0))):
            with self.subTest(document=text[:64]):
                output, report = convert_aemp_hours(text, "caller-declared-fleet", 99, FLEET_HOURS_FORMAT)
                rows = list(csv.DictReader(io.StringIO(output)))
                equipment, with_hours, without, unselected, unknown, root_unknown = counts
                self.assertEqual(len(rows), equipment)  # one row per equipment, never a sample
                self.assertEqual((report["equipment_records"], report["equipment_with_hour_record"],
                                  report["equipment_without_hour_record"]), (equipment, with_hours, without))
                self.assertEqual((report["unselected_metric_elements"], report["unknown_elements"],
                                  report["unsupported_root_elements"]), (unselected, unknown, root_unknown))
                self.assertEqual(report["source_format"], FLEET_HOURS_FORMAT)
                self.assertEqual(report["collection_scope"], "SUPPLIED_PAGE_ONLY")
                self.assertEqual(report["clock"], "Unknown")
                self.assertEqual(report["equipment_identity"], "CALLER_DECLARED_NOT_AUTHENTICATED")
                self.assertEqual(report["equipment_provenance"],
                                 "REPORTED_HEADER_FIELDS_NOT_AUTHENTICATED_IDENTITY_OR_VEHICLE")
                self.assertIn("not validated against the XSD", report["selected_schema_reference"])
                self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
                self.assertEqual(rows[1]["source_record_hex"], "")
                self.assertEqual(report["source_sha256"][7:],
                                 hashlib.sha256(text.encode()).hexdigest())
                self.assertEqual([r["record_time_us"] for r in rows], ["99"] * equipment)
                self.assertEqual([int(r["equipment_index"]) for r in rows], list(range(equipment)))
                for index, (row, seconds) in enumerate(zip(rows, expected)):
                    self.assertNotEqual(row["record_time_us"], row["equipment_report_snapshot_time_us"])
                    if seconds is None:
                        self.assertEqual(row["engine_operating_seconds_reported"], "")
                        self.assertEqual(row["operating_hours_measurement_time_us"], "")
                        self.assertEqual(row["operating_hours_basis"], "HOUR_RECORD_NOT_REPORTED")
                        self.assertEqual(row["equipment_hours_disposition"], "HOUR_RECORD_NOT_REPORTED_NOT_ZERO")
                    else:
                        self.assertEqual(float(row["engine_operating_seconds_reported"]), seconds)
                        self.assertEqual(row["operating_hours_basis"],
                                         "REPORTED_LIFETIME_ENGINE_TIME_NOT_WORK_OR_CONTINUITY")
                        self.assertEqual(row["equipment_hours_disposition"], "REPORTED_EQUIPMENT_HOUR_RECORD")
                        self.assertNotEqual(row["operating_hours_measurement_time_us"],
                                            row["equipment_report_snapshot_time_us"])
        first = list(csv.DictReader(io.StringIO(
            convert_aemp_hours(FLEET_A, "caller-declared-fleet", 99, FLEET_HOURS_FORMAT)[0])))
        self.assertEqual(first[0]["equipment_report_snapshot_time_us"], "1772600767000000")
        self.assertEqual(first[0]["operating_hours_measurement_time_us"], "1772600400000000")
        self.assertEqual(first[0]["fleet_version_reported"], "1")
        self.assertEqual(bytes.fromhex(first[0]["equipment_id_reported_hex"][4:]).decode(), "declared-unit-1")
        self.assertEqual(bytes.fromhex(first[0]["equipment_serial_reported_hex"][4:]).decode(), "SN0000000000001")
        self.assertEqual(bytes.fromhex(first[0]["equipment_pin_reported_hex"][4:]).decode(), "PIN000000000001")
        self.assertEqual(bytes.fromhex(first[0]["equipment_oem_reported_hex"][4:]).decode(), "authored-oem")
        self.assertEqual(bytes.fromhex(first[0]["equipment_model_reported_hex"][4:]).decode(), "authored-model")
        self.assertEqual(first[1]["equipment_serial_reported_hex"], "")
        self.assertEqual(bytes.fromhex(first[1]["equipment_id_reported_hex"][4:]).decode(), "declared-unit-2")
        for text, layout in ((FLEET_A, "iso-hours-xml-20190501"), (PAGE_XML, FLEET_HOURS_FORMAT)):
            with self.subTest(layout=layout), self.assertRaises(ValueError) as refusal:
                convert_aemp_hours(text, "x", 55, layout)
            self.assertEqual(str(refusal.exception), "wrong operating-hours XML namespace/root")
        element_form = FLEET_A.replace('<CumulativeOperatingHours datetime="2026-03-04T05:00:00Z">',
            '<CumulativeOperatingHours><datetime>2026-03-04T05:00:00Z</datetime>')
        for name, bad in (
                ("datetime element", element_form),
                ("no datetime", FLEET_A.replace(' datetime="2026-03-04T05:00:00Z"><Hour>1234.5', '><Hour>1234.5')),
                ("two Hour", FLEET_A.replace("<Hour>1234.5</Hour>", "<Hour>1</Hour><Hour>2</Hour>")),
                ("negative Hour", FLEET_A.replace("<Hour>1234.5</Hour>", "<Hour>-1</Hour>")),
                ("two hour records", FLEET_A.replace("</Equipment>",
                    '<CumulativeOperatingHours datetime="2026-03-04T05:00:00Z"><Hour>2</Hour>'
                    "</CumulativeOperatingHours></Equipment>", 1)),
                ("two headers", FLEET_A.replace("<EquipmentHeader><OEMName>",
                    "<EquipmentHeader/><EquipmentHeader><OEMName>")),
                ("duplicate field", FLEET_A.replace("<EquipmentID>declared-unit-1</EquipmentID>",
                    "<EquipmentID>a</EquipmentID><EquipmentID>b</EquipmentID>")),
                ("nested header field", FLEET_A.replace("<Model>authored-model</Model>",
                    "<Model><inner>x</inner></Model>")),
                ("long serial", FLEET_A.replace("SN0000000000001", "SN" + "0" * 16)),
                ("empty serial", FLEET_A.replace("<SerialNumber>SN0000000000001</SerialNumber>",
                                                 "<SerialNumber></SerialNumber>")),
                ("no version", FLEET_A.replace(' version="1"', "")),
                ("no snapshotTime", FLEET_A.replace(' snapshotTime="2026-03-04T05:06:07Z"', "")),
                ("signed version", FLEET_A.replace('version="1"', 'version="-1"')),
                ("offsetless snapshot", FLEET_A.replace("2026-03-04T05:06:07Z", "2026-03-04T05:06:07")),
                ("offsetless reading", FLEET_A.replace("2026-03-04T05:00:00Z", "2026-03-04T05:00:00")),
                ("install time", FLEET_B.replace("2019-07-01T00:00:00+09:00", "2019-07-01")),
                ("no equipment", FLEET_A[:FLEET_A.index("<Equipment>")] + "</Fleet>"),
                ("no links", FLEET_A.replace(
                    "<Links><rel>self</rel><href>https://example.invalid/fleet</href></Links>", "")),
                ("entity", '<!DOCTYPE x [<!ENTITY a "b">]>' + FLEET_A)):
            with self.subTest(refusal=name), self.assertRaises(ValueError):
                convert_aemp_hours(bad, "x", 55, FLEET_HOURS_FORMAT)
        for name, bad in (
                ("fleet nested Hour", FLEET_A.replace("<Hour>1234.5</Hour>",
                                                      "<Hour>1<other>999</other></Hour>")),
                ("fleet nested Hour tail", FLEET_A.replace("<Hour>1234.5</Hour>",
                                                           "<Hour><other/>1234.5</Hour>")),
                ("fleet nested rel", FLEET_A.replace("<rel>self</rel>",
                                                     "<rel>self<other>x</other></rel>")),
                ("fleet nested href", FLEET_A.replace("<href>https://example.invalid/fleet</href>",
                                                      "<href>https://example.invalid/fleet<x/></href>"))):
            with self.subTest(refusal=name), self.assertRaises(ValueError) as refusal:
                convert_aemp_hours(bad, "x", 55, FLEET_HOURS_FORMAT)
            self.assertIn("scalar reported", str(refusal.exception))
        for name, bad, message in (
                ("page nested Hour", PAGE_XML.replace("<Hour>1</Hour>",
                    "<Hour>1<other>999</other></Hour>"), "scalar reported Hour value required"),
                ("page nested rel", PAGE_XML.replace("<rel>next</rel>",
                    "<rel>next<other>x</other></rel>"), "scalar reported link rel and href required"),
                ("page nested href", PAGE_XML.replace("<href>https://example.invalid/next</href>",
                    "<href>https://example.invalid/next<x/></href>"),
                 "scalar reported link rel and href required")):
            with self.subTest(refusal=name), self.assertRaises(ValueError) as refusal:
                convert_aemp_hours(bad, "x", 55, "iso-hours-xml-20190501")
            self.assertEqual(str(refusal.exception), message)
        siblings = FLEET_A.replace("</CumulativeOperatingHours>",
            "<vendorNote>kept</vendorNote></CumulativeOperatingHours>").replace(
            "<href>https://example.invalid/fleet</href>",
            "<href>https://example.invalid/fleet</href><linkNote>kept</linkNote>")
        output, report = convert_aemp_hours(siblings, "caller-declared-fleet", 99, FLEET_HOURS_FORMAT)
        self.assertEqual(report["unknown_elements"], 2)  # one beside Hour, one beside href
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(float(rows[0]["engine_operating_seconds_reported"]), 4444200.0)
        self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), siblings)
        self.assertEqual(report["links_retained_not_followed"],
                         [dict(rel="self", href="https://example.invalid/fleet")])
        self.assertEqual(MAX_FLEET_EQUIPMENT, 20000)
        many = fleet_document([f"unit-{i}" for i in range(2000)])
        output, report = convert_aemp_hours(many, "caller-declared-fleet", 99, FLEET_HOURS_FORMAT)
        previous_limit = csv.field_size_limit(2 * 1024 * 1024)
        self.addCleanup(csv.field_size_limit, previous_limit)
        parsed = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(parsed), 2000)
        self.assertEqual({row["engine_operating_seconds_reported"] for row in parsed}, {"5400.0"})
        self.assertEqual([int(row["equipment_index"]) for row in parsed], list(range(2000)))
        self.assertEqual((report["equipment_records"], report["equipment_with_hour_record"]), (2000, 2000))
        import scripts.convert_opcua_recorded as module
        self.addCleanup(setattr, module, "MAX_FLEET_EQUIPMENT", MAX_FLEET_EQUIPMENT)
        module.MAX_FLEET_EQUIPMENT = 3
        with self.assertRaises(ValueError) as refusal:
            convert_aemp_hours(many, "caller-declared-fleet", 99, FLEET_HOURS_FORMAT)
        self.assertEqual(str(refusal.exception), "saved fleet exceeds the selected equipment bound")

    def test_fleet_snapshot_reaches_the_actual_common_reader(self):
        from scripts.convert_opcua_recorded import FLEET_HOURS_FORMAT
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; common-output assertions NOT RUN")
        profile = str(ROOT / "profiles/declared/opcua-recorded/profile.toml")
        for text, seconds, rows in ((FLEET_A, 4444200.0, 2), (FLEET_B, 900.0, 3),
                                    (fleet_document([f"unit-{i}" for i in range(2000)]), 5400.0, 2000)):
            with self.subTest(document=text[:64]), tempfile.TemporaryDirectory() as tmp:
                paths = [Path(tmp) / n for n in ("input.xml", "out.csv", "report.json")]
                paths[0].write_text(text)
                subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded", *map(str, paths),
                    "--source-format", FLEET_HOURS_FORMAT, "--equipment-id", "caller-declared-fleet",
                    "--capture-time-us", "99"], cwd=ROOT, check=True, capture_output=True)
                common = json.loads(subprocess.run([reader, profile, str(paths[1]), "--allow-equal-time"],
                                                   check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], rows)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["engine_operating_seconds_reported"], seconds)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                if rows == 2000:
                    self.assertEqual(len(common["observations"]), 2000)
                    indices = [int(o["fields"]["equipment_index"]) for o in common["observations"]]
                    self.assertEqual(indices, list(range(2000)))
                    self.assertEqual({o["fields"]["engine_operating_seconds_reported"]
                                      for o in common["observations"]}, {5400.0})
                    first_row = paths[1].read_text().splitlines()[1]
                    self.assertGreater(len(first_row.encode()), 700000)  # the retained document
                    self.assertGreater(paths[1].stat().st_size, 1300000)
                    continue
                self.assertIsNone(common["observations"][-1]["fields"]["engine_operating_seconds_reported"])
                self.assertEqual(common["observations"][-1]["fields"]["equipment_hours_disposition"],
                                 "HOUR_RECORD_NOT_REPORTED_NOT_ZERO")
                units = common["profile_units"]
                self.assertTrue(units["engine_operating_seconds_reported"].startswith("seconds_reported_lifetime"))
                self.assertIn("distinct_from_capture", units["equipment_report_snapshot_time_us"])
                self.assertIn("never_authenticated_identity", units["equipment_id_reported_hex"])
                self.assertIn("absence_is_never_a_zero_reading", units["equipment_hours_disposition"])

    def test_explicit_boolean_reuses_reader_without_inventing_device_state(self):
        for node, values in (("ns=2;s=reported-a", [True, False]), ("ns=3;s=reported-b", [False, True])):
            source = [record(value) for value in values]
            for item in source:
                item["node_id"] = node
                item["data_value"]["UaType"] = 1
            source.append(record(True, 0x80000000))
            source[-1]["node_id"] = node
            source[-1]["data_value"]["UaType"] = 1
            text = "\n".join(json.dumps(item) for item in source)
            output, report = convert(text, node, boolean=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            expected = ["TRUE" if value else "FALSE" for value in values]
            self.assertEqual([row["boolean_reported"] for row in rows], expected + [""])
            self.assertEqual((report["converted_values"], report["withheld_values"]), (2, 1))
            self.assertEqual(rows[-1]["value_disposition"], "WITHHELD_STATUS_NOT_EXACT_GOOD")
            self.assertEqual(rows[0]["source_10ps"], "4")
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), source[0])
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "boolean.csv"
                path.write_text(output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 3)
                    self.assertEqual([o["fields"]["boolean_reported"] for o in common["observations"][:2]], expected)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                input_path = Path(directory) / "input.jsonl"
                input_path.write_text(text)
                subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded", str(input_path),
                    str(Path(directory) / "cli.csv"), str(Path(directory) / "report.json"),
                    "--node", node, "--boolean"], cwd=ROOT, check=True, capture_output=True)
                self.assertEqual((Path(directory) / "cli.csv").read_text(), output)
            for value in (0, 1, "true", None, [], {}):
                bad = record(value); bad["data_value"]["UaType"] = 1
                with self.assertRaises(ValueError): convert(json.dumps(bad), bad["node_id"], boolean=True)
            bad = record(); bad["data_value"].pop("Value"); bad["data_value"]["UaType"] = 1
            with self.assertRaises(ValueError): convert(json.dumps(bad), bad["node_id"], boolean=True)
            with self.assertRaises(ValueError): convert(text, node, "V", boolean=True)
            with self.assertRaises(ValueError): convert(json.dumps(record()), record()["node_id"], boolean=True)

    def test_har_selected_response_reuses_temperature_and_keeps_clock_boundary(self):
        import base64
        import copy
        from scripts.convert_opcua_recorded import convert_har_sensorthings
        prop = "urn:example:air-temperature"
        for value, encoded in ((25, False), (-10, True)):
            body = json.dumps({"value": [{"@iot.id": 1, "result": value,
                "phenomenonTime": "2026-01-01T00:00:01Z", "resultTime": None, "future": "保持",
                "Datastream": {"@iot.id": 2, "ObservedProperty": {"definition": prop},
                    "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                    "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C",
                        "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}]}, ensure_ascii=False)
            content = dict(mimeType="application/json; charset=utf-8", size=len(body.encode()), text=body)
            if encoded:
                content.update(encoding="base64", text=base64.b64encode(body.encode()).decode())
            entry = dict(startedDateTime="2026-01-01T01:00:00+01:00",
                request=dict(method="GET", headers=[{"name": "Authorization", "value": "TEST_NOT_A_CREDENTIAL"}]),
                response=dict(status=200, content=content))
            source = dict(log=dict(version="1.2", entries=[{"unselected": True}, entry]))
            text = json.dumps(source)
            output, report = convert_har_sensorthings(text, 1, prop)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row["temperature_k"]), value + 273.15)
            self.assertEqual(row["record_time_us"], "1767225600000000")
            self.assertEqual(row["phenomenon_time_us"], "1767225601000000")
            self.assertEqual(report["har_unselected_entries"], 1)
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), body)
            self.assertNotIn("TEST_NOT_A_CREDENTIAL", output + json.dumps(report))
            self.assertEqual(convert_har_sensorthings("\ufeff" + text, 1, prop)[1]["records"], 1)
            with tempfile.TemporaryDirectory() as tmp:
                source_path, target, report_path = [Path(tmp) / n for n in ("input.har", "output.csv", "report.json")]
                source_path.write_text(text)
                subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded", str(source_path),
                    str(target), str(report_path), "--source-format", "har-1.2-sensorthings",
                    "--observed-property", prop, "--har-entry", "1"], cwd=ROOT, check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"), str(target)],
                        check=True, capture_output=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["temperature_k"], value + 273.15)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for section, key, replacement in (("response", "status", 404), ("response", "status", True),
                                               ("request", "method", "POST")):
                bad = copy.deepcopy(source); bad["log"]["entries"][1][section][key] = replacement
                with self.assertRaises(ValueError): convert_har_sensorthings(json.dumps(bad), 1, prop)
            for key, replacement in (("text", None), ("encoding", "gzip"), ("mimeType", "text/html"), ("size", -1)):
                bad = copy.deepcopy(source); bad["log"]["entries"][1]["response"]["content"][key] = replacement
                with self.assertRaises(ValueError): convert_har_sensorthings(json.dumps(bad), 1, prop)
            with self.assertRaises(ValueError): convert_har_sensorthings(text, True, prop)
            with self.assertRaises(ValueError): convert_har_sensorthings(text, 2, prop)

    def test_sensorthings_period_preserves_bounds_not_a_fabricated_instant(self):
        prop = "urn:synthetic:temperature"
        def source(period):
            return {"value": [{"@iot.id": 1, "phenomenonTime": period, "resultTime": None,
                "result": 20, "future": [1], "Datastream": {"@iot.id": 2,
                "ObservedProperty": {"definition": prop},
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C",
                    "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}]}
        base = 1767225600000000
        for period, start, end in (
            ("2026-01-01T00:00:00+02:00/2026-01-01T00:00:01.000001+02:00", base-7200000000, base-7198999999),
            ("2026-01-01T00:00:00Z/2026-01-01T02:00:00+01:00", base, base+3600000000),
            ("2026-01-01T00:00:00Z/2026-01-01T01:00:00+01:00", base, base),
        ):
            text = json.dumps(source(period))
            output, report = convert_sensorthings(text, prop, 55)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(row["phenomenon_time_kind"], "PERIOD")
            self.assertEqual(row["phenomenon_time_us"], "")
            self.assertEqual(int(row["phenomenon_start_us"]), start)
            self.assertEqual(int(row["phenomenon_end_us"]), end)
            self.assertEqual(float(row["temperature_k"]), 293.15)
            self.assertEqual(bytes.fromhex(row["source_record_hex"][4:]).decode(), text)
            self.assertEqual(report["records"], 1)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"
                path.write_text(output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path)], check=True, capture_output=True).stdout)
                    fields = common["observations"][0]["fields"]
                    self.assertEqual(fields["phenomenon_start_us"], start)
                    self.assertEqual(fields["phenomenon_end_us"], end)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                original = Path(directory) / "in.json"
                original.write_text(text)
                result = Path(directory) / "cli.csv"
                subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded",
                    str(original), str(result), str(Path(directory) / "report.json"),
                    "--source-format", "sensorthings-1.1-celsius", "--observed-property", prop,
                    "--capture-time-us", "55"], cwd=ROOT, check=True, capture_output=True)
                self.assertEqual(result.read_text(), output)
        instant = next(csv.DictReader(io.StringIO(convert_sensorthings(
            json.dumps(source("2026-01-01T00:00:00Z")), prop, 55)[0])))
        self.assertEqual(instant["phenomenon_time_kind"], "INSTANT")
        self.assertEqual(int(instant["phenomenon_time_us"]), base)
        self.assertEqual(instant["phenomenon_start_us"], "")
        for period in ("2026-01-02T00:00:00Z/2026-01-01T00:00:00Z",
                       "2026-01-01T00:00:00Z/", "/2026-01-01T00:00:00Z",
                       "2026-01-01T00:00:00Z/PT1H", "a/b/c",
                       "2026-01-01T00:00:00/2026-01-01T01:00:00Z"):
            with self.assertRaises(ValueError): convert_sensorthings(json.dumps(source(period)), prop, 55)
        bad = source("2026-01-01T00:00:00Z")
        bad["value"][0]["resultTime"] = "2026-01-01T00:00:00Z/2026-01-01T01:00:00Z"
        with self.assertRaises(ValueError): convert_sensorthings(json.dumps(bad), prop, 55)

    def test_saved_page_chain_reports_only_the_supplied_pages(self):
        """A caller-bound finite chain: nothing fetched, nothing called complete."""
        from scripts.convert_opcua_recorded import (CHAIN_TERMINAL, CHAIN_UNFOLLOWED,
                                                    PAGE_URL_REDACTED, sensorthings_saved_pages)

        def body(ids, *, prop, next_link=None, count=None, period=False, celsius=True):
            def item(index):
                stamp = (f"2026-03-0{index}T00:00:00Z/2026-03-0{index}T01:00:00Z" if period
                         else f"2026-03-0{index}T00:00:00Z")
                unit = {"name": "degree Celsius", "symbol": "\u00b0C",
                        "definition": "http://unitsofmeasure.org/ucum.html#para-30"}
                if not celsius:
                    unit = dict(unit, symbol="K")
                return {"@iot.id": index, "phenomenonTime": stamp, "resultTime": None,
                        "result": float(index), "note": {"kept": index},
                        "Datastream": {"@iot.id": "stream-9", "ObservedProperty": {"definition": prop},
                            "observationType": "http://www.opengis.net/def/observationType/"
                                               "OGC-OM/2.0/OM_Measurement",
                            "unitOfMeasurement": unit}}
            page = {"value": [item(i) for i in ids]}
            if next_link is not None:
                page["@iot.nextLink"] = next_link
            if count is not None:
                page["@iot.count"] = count
            return json.dumps(page, ensure_ascii=False)

        def manifest(entries):
            return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)

        first_urls = ["https://host.invalid/v1.1/Observations?$skip=0&token=AUTHORED-NOT-REAL",
                      "https://host.invalid/v1.1/Observations?$skip=2"]
        second_urls = ["https://other.invalid/v1.1/Observations?$top=1",
                       "https://other.invalid/v1.1/Observations?$top=1&$skip=1",
                       "https://other.invalid/v1.1/Observations?$top=1&$skip=2"]
        chains = (
            ("urn:example:air-temperature", first_urls,
             [body([1, 2], prop="urn:example:air-temperature", next_link=first_urls[1], count=9),
              body([3, 4], prop="urn:example:air-temperature", period=True)],
             [{"saved_by": "engineer note"}, {}], 4, "9"),
            ("urn:example:liquid-temperature", second_urls,
             [body([1], prop="urn:example:liquid-temperature", next_link=second_urls[1]),
              body([2], prop="urn:example:liquid-temperature", next_link=second_urls[2]),
              body([3], prop="urn:example:liquid-temperature")],
             [{}, {}, {}], 3, ""),
        )
        for prop, urls, bodies, extras, expected_rows, expected_count in chains:
            entries = [dict(extras[i], declared_page_url=urls[i], page_body=bodies[i])
                       for i in range(len(urls))]
            text = manifest(entries)
            output, report = sensorthings_saved_pages(text, prop, 55)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), expected_rows)
            self.assertEqual((report["records"], report["saved_pages"]), (expected_rows, len(urls)))
            self.assertEqual(report["saved_chain_disposition"], CHAIN_TERMINAL)
            self.assertEqual(report["repeated_observation_ids"], 0)
            self.assertEqual([int(r["saved_page_index"]) for r in rows],
                             sorted(int(r["saved_page_index"]) for r in rows))
            self.assertEqual(len({r["saved_page_sha256"] for r in rows}), len(urls))
            self.assertTrue(all(int(r["saved_page_count"]) == len(urls) for r in rows))
            self.assertTrue(all(r["saved_chain_disposition"] == CHAIN_TERMINAL for r in rows))
            self.assertEqual({r["reported_collection_count"] for r in rows} - {""},
                             {expected_count} - {""})
            if expected_count:
                self.assertNotEqual(int(expected_count), len(rows))
            self.assertNotIn("AUTHORED-NOT-REAL", output)
            for url in urls:
                self.assertNotIn(url, output)
            self.assertTrue(all(r["saved_page_url_reference"].startswith(PAGE_URL_REDACTED + "@sha256:")
                                for r in rows))
            self.assertTrue(all(r["observation_repeat_disposition"] == "FIRST_REPORT_OF_THIS_ID"
                                for r in rows))
            self.assertEqual(float(rows[0]["temperature_k"]), 1 + 273.15)
            self.assertEqual(rows[-1]["phenomenon_time_kind"], "PERIOD" if len(urls) == 2 else "INSTANT")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "chain.csv"
                    path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], expected_rows)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["saved_page_count"], len(urls))
                self.assertEqual(fields["saved_chain_disposition"], CHAIN_TERMINAL)
                units = common["profile_units"]
                for column in ("saved_page_index", "saved_page_count", "saved_page_sha256",
                               "saved_page_url_reference", "saved_chain_disposition",
                               "reported_collection_count", "observation_repeat_disposition"):
                    self.assertTrue(units.get(column), f"{column} must declare its meaning")
                self.assertIn("never_a_complete_collection", units["saved_chain_disposition"])
                self.assertIn("may_differ_from_the_rows", units["reported_collection_count"])
                self.assertIn("order_is_as_saved", units["saved_page_index"])
                self.assertIn("neither_body_is_ranked", units["observation_repeat_disposition"])

        prop = "urn:example:air-temperature"
        partial = manifest([{"declared_page_url": first_urls[0],
                             "page_body": body([1], prop=prop, next_link=first_urls[1])}])
        _, partial_report = sensorthings_saved_pages(partial, prop, 55)
        self.assertEqual(partial_report["saved_chain_disposition"], CHAIN_UNFOLLOWED)
        self.assertIn("not a complete collection", partial_report["chain_basis"])

        repeated = manifest([
            {"declared_page_url": first_urls[0],
             "page_body": body([1, 2], prop=prop, next_link=first_urls[1])},
            {"declared_page_url": first_urls[1], "page_body": body([2, 5], prop=prop)}])
        output, report = sensorthings_saved_pages(repeated, prop, 55)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), report["repeated_observation_ids"]), (4, 1))
        self.assertEqual([r["observation_repeat_disposition"] for r in rows],
                         ["FIRST_REPORT_OF_THIS_ID", "FIRST_REPORT_OF_THIS_ID",
                          "ID_REPORTED_AGAIN_IN_THE_SUPPLIED_CHAIN_NEITHER_BODY_RANKED",
                          "FIRST_REPORT_OF_THIS_ID"])

        for reason, text in (
            ("saved chain break: nextLink does not match the next declared page url",
             manifest([{"declared_page_url": first_urls[0],
                        "page_body": body([1], prop=prop, next_link="https://host.invalid/elsewhere")},
                       {"declared_page_url": first_urls[1], "page_body": body([2], prop=prop)}])),
            ("saved chain break: nextLink does not match the next declared page url",
             manifest([{"declared_page_url": first_urls[0], "page_body": body([1], prop=prop)},
                       {"declared_page_url": first_urls[1], "page_body": body([2], prop=prop)}])),
            ("repeated declared page url in the saved chain",
             manifest([{"declared_page_url": first_urls[0],
                        "page_body": body([1], prop=prop, next_link=first_urls[0])},
                       {"declared_page_url": first_urls[0], "page_body": body([2], prop=prop)}])),
            ("each saved page needs a declared url and its exact saved body text",
             json.dumps({"declared_page_url": first_urls[0], "page_body": {"value": []}})),
            ("saved page manifest line must be an object", json.dumps([1])),
            ("at least one saved page required", "\n"),
            ("invalid reported collection count",
             manifest([{"declared_page_url": first_urls[0],
                        "page_body": body([1], prop=prop, count=-1)}])),
        ):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                sensorthings_saved_pages(text, prop, 55)
            self.assertEqual(str(caught.exception), reason)
        for text in (manifest([{"declared_page_url": first_urls[0], "page_body": body([1], prop=prop)}]),
                     manifest([{"declared_page_url": first_urls[0],
                                "page_body": body([1], prop=prop, celsius=False)}]),
                     manifest([{"declared_page_url": first_urls[0], "page_body": "{not json"}])):
            with self.assertRaises(ValueError):
                sensorthings_saved_pages(text, "urn:example:other", 55)

        urls = [f"https://host.invalid/v1.1/Observations?$skip={i * 1000}" for i in range(10)]
        entries = []
        for index, url in enumerate(urls):
            ids = list(range(index * 1000 + 1, index * 1000 + 1001))
            large = {"value": [{"@iot.id": i, "phenomenonTime": "2026-03-01T00:00:00Z",
                                "resultTime": None, "result": 1.5,
                                "Datastream": {"@iot.id": "s", "ObservedProperty": {"definition": prop},
                                    "observationType": "http://www.opengis.net/def/observationType/"
                                                       "OGC-OM/2.0/OM_Measurement",
                                    "unitOfMeasurement": {"name": "degree Celsius", "symbol": "\u00b0C",
                                        "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}
                               for i in ids]}
            if index + 1 < len(urls):
                large["@iot.nextLink"] = urls[index + 1]
            entries.append({"declared_page_url": url,
                            "page_body": json.dumps(large, ensure_ascii=False)})
        output, report = sensorthings_saved_pages(manifest(entries), prop, 55)
        self.assertEqual((report["records"], report["saved_pages"]), (10000, 10))
        self.assertEqual(report["saved_chain_disposition"], CHAIN_TERMINAL)
        self.assertEqual(output.count("\n"), 10001)  # header plus one row per observation
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "large-chain.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 10000)
            self.assertEqual(len(common["observations"]), 10000)
            pages = [o["fields"]["saved_page_index"] for o in common["observations"]]
            self.assertEqual(sorted(set(pages)), list(range(10)))
            self.assertTrue(all(pages.count(index) == 1000 for index in range(10)))
            self.assertEqual(len({o["fields"]["saved_page_sha256"] for o in common["observations"]}), 10)
            self.assertTrue(all(o["fields"]["saved_page_count"] == 10 for o in common["observations"]))

    def test_saved_page_chain_refuses_a_non_object_body_and_accounts_for_empty_pages(self):
        from scripts.convert_opcua_recorded import (CHAIN_TERMINAL, CHAIN_UNFOLLOWED,
                                                    sensorthings_saved_pages)
        prop = "urn:example:air-temperature"
        urls = ["https://host.invalid/v1.1/Observations?$skip=0",
                "https://host.invalid/v1.1/Observations?$skip=1",
                "https://host.invalid/v1.1/Observations?$skip=2"]

        def page(ids, next_link=None, count=None):
            items = [{"@iot.id": i, "phenomenonTime": "2026-03-01T00:0%d:00Z" % i, "resultTime": None,
                      "result": float(i), "Datastream": {"@iot.id": "s",
                          "ObservedProperty": {"definition": prop},
                          "observationType": "http://www.opengis.net/def/observationType/"
                                             "OGC-OM/2.0/OM_Measurement",
                          "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C",
                              "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}
                     for i in ids]
            body = {"value": items}
            if next_link is not None:
                body["@iot.nextLink"] = next_link
            if count is not None:
                body["@iot.count"] = count
            return json.dumps(body, ensure_ascii=False)

        def manifest(entries):
            return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)

        for body_text in ("[]", "null", '"a string body"', "3"):
            with self.subTest(body=body_text), self.assertRaises(ValueError) as caught:
                sensorthings_saved_pages(
                    manifest([{"declared_page_url": urls[0], "page_body": body_text}]), prop, 55)
            self.assertEqual(str(caught.exception), "saved page body must be a JSON object")

        empty_terminal = manifest([{"declared_page_url": urls[0], "page_body": page([1], urls[1])},
                                   {"declared_page_url": urls[1], "page_body": page([])}])
        output, report = sensorthings_saved_pages(empty_terminal, prop, 55)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), report["records"], report["saved_pages"]), (1, 1, 2))
        self.assertEqual((report["empty_pages"], report["empty_page_indices"]), (1, [1]))
        self.assertEqual(report["saved_chain_disposition"], CHAIN_TERMINAL)

        empty_middle = manifest([{"declared_page_url": urls[0], "page_body": page([1], urls[1])},
                                 {"declared_page_url": urls[1], "page_body": page([], urls[2])},
                                 {"declared_page_url": urls[2], "page_body": page([3])}])
        output, report = sensorthings_saved_pages(empty_middle, prop, 55)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([r["saved_page_index"] for r in rows], ["0", "2"])
        self.assertEqual((report["empty_pages"], report["empty_page_indices"]), (1, [1]))
        self.assertTrue(all(int(r["saved_page_count"]) == 3 for r in rows))

        only_empty = manifest([{"declared_page_url": urls[0], "page_body": page([], urls[1], count=7)},
                               {"declared_page_url": urls[1], "page_body": page([], count=7)}])
        output, report = sensorthings_saved_pages(only_empty, prop, 55)
        self.assertEqual(output, "")
        self.assertEqual((report["records"], report["saved_pages"]), (0, 2))
        self.assertEqual((report["empty_pages"], report["empty_page_indices"]), (2, [0, 1]))
        self.assertEqual(report["saved_chain_disposition"], CHAIN_TERMINAL)
        _, report = sensorthings_saved_pages(
            manifest([{"declared_page_url": urls[0], "page_body": page([], urls[1])}]), prop, 55)
        self.assertEqual(report["saved_chain_disposition"], CHAIN_UNFOLLOWED)

    def test_empty_saved_pages_pass_the_same_gate_and_keep_their_provenance(self):
        import hashlib
        from scripts.convert_opcua_recorded import (CHAIN_TERMINAL, PAGE_URL_REDACTED,
                                                    sensorthings_saved_pages)
        prop = "urn:example:air-temperature"
        urls = ["https://host.invalid/v1.1/Observations?$skip=0&sig=AUTHORED-NOT-REAL-EMPTY",
                "https://host.invalid/v1.1/Observations?$skip=1"]

        def observation(i):
            return {"@iot.id": i, "phenomenonTime": "2026-03-01T00:0%d:00Z" % i, "resultTime": None,
                    "result": float(i), "Datastream": {"@iot.id": "s",
                        "ObservedProperty": {"definition": prop},
                        "observationType": "http://www.opengis.net/def/observationType/"
                                           "OGC-OM/2.0/OM_Measurement",
                        "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C",
                            "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}

        def page(ids, **extra):
            return json.dumps({"value": [observation(i) for i in ids], **extra}, ensure_ascii=False)

        def manifest(entries):
            return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)

        empty_with_bad_count = page([], **{"@iot.count": -1})
        empty_with_null_link = page([], **{"@iot.nextLink": None})
        empty_with_empty_link = page([], **{"@iot.nextLink": ""})
        oversized_empty = json.dumps({"value": [], "note": "x" * (16 * 1024 * 1024 + 1)})
        for reason, entries in (
            ("invalid reported collection count",
             [{"declared_page_url": urls[0], "page_body": empty_with_bad_count}]),
            ("invalid nextLink", [{"declared_page_url": urls[0], "page_body": empty_with_null_link}]),
            ("invalid nextLink", [{"declared_page_url": urls[0], "page_body": empty_with_empty_link}]),
            ("invalid nextLink", [{"declared_page_url": urls[0], "page_body": empty_with_null_link},
                                  {"declared_page_url": urls[1], "page_body": page([2])}]),
            ("bounded saved collection required",
             [{"declared_page_url": urls[0], "page_body": oversized_empty}]),
        ):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                sensorthings_saved_pages(manifest(entries), prop, 55)
            self.assertEqual(str(caught.exception), reason)
        with self.assertRaises(ValueError) as caught:
            convert_sensorthings(page([1], **{"@iot.nextLink": None}), prop, 55)
        self.assertEqual(str(caught.exception), "invalid nextLink")

        only_empty = manifest([{"declared_page_url": urls[0], "page_body": page([], **{"@iot.nextLink": urls[1]})},
                               {"declared_page_url": urls[1], "page_body": page([])}])
        for reason, prop_arg, capture in (
                ("explicit capture microseconds required", prop, -1),
                ("explicit capture microseconds required", prop, "55"),
                ("explicit observed property definition required", "", 55),
                ("explicit observed property definition required", None, 55)):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                sensorthings_saved_pages(only_empty, prop_arg, capture)
            self.assertEqual(str(caught.exception), reason)

        empty_body = page([], **{"@iot.count": 7})
        entries = [{"declared_page_url": urls[0], "page_body": page([1], **{"@iot.nextLink": urls[1]})},
                   {"declared_page_url": urls[1], "page_body": empty_body, "saved_by": "engineer note",
                    "retrieved_at": "2026-03-01"}]
        text = manifest(entries)
        output, report = sensorthings_saved_pages(text, prop, 55)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), report["records"], report["empty_pages"]), (1, 1, 1))
        self.assertEqual(report["retained_manifest_keys"], ["saved_by", "retrieved_at"])
        self.assertEqual(report["manifest_sha256"],
                         "sha256:" + hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(report["empty_page_provenance"], [dict(
            saved_page_index=1, saved_page_count=2,
            saved_page_sha256="sha256:" + hashlib.sha256(empty_body.encode()).hexdigest(),
            saved_page_url_reference=PAGE_URL_REDACTED + "@sha256:"
            + hashlib.sha256(urls[1].encode()).hexdigest() + "+" + str(len(urls[1].encode())),
            saved_chain_disposition=CHAIN_TERMINAL, reported_collection_count=7,
            retained_manifest_keys=["retrieved_at", "saved_by"])])
        self.assertEqual(rows[0]["saved_page_sha256"],
                         "sha256:" + hashlib.sha256(entries[0]["page_body"].encode()).hexdigest())
        serialised = json.dumps(report)
        for secret in ("AUTHORED-NOT-REAL-EMPTY", urls[0], urls[1], "engineer note", "2026-03-01"):
            self.assertNotIn(secret, serialised)

    def test_saved_page_chain_retains_next_link_secrets_in_the_source_record_hex(self):
        from scripts.convert_opcua_recorded import (PAGE_URL_REDACTED, sensorthings_saved_pages)
        prop = "urn:example:air-temperature"
        token = "AUTHORED-NOT-REAL-TOKEN-4f1c"
        second = f"https://host.invalid/v1.1/Observations?$skip=1&sig={token}"
        first = "https://host.invalid/v1.1/Observations?$skip=0"

        def page(ids, next_link=None):
            items = [{"@iot.id": i, "phenomenonTime": "2026-03-01T00:0%d:00Z" % i, "resultTime": None,
                      "result": float(i), "Datastream": {"@iot.id": "s",
                          "ObservedProperty": {"definition": prop},
                          "observationType": "http://www.opengis.net/def/observationType/"
                                             "OGC-OM/2.0/OM_Measurement",
                          "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C",
                              "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}
                     for i in ids]
            body = {"value": items}
            if next_link is not None:
                body["@iot.nextLink"] = next_link
            return json.dumps(body, ensure_ascii=False)

        def manifest(entries):
            return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)

        output, report = sensorthings_saved_pages(
            manifest([{"declared_page_url": first, "page_body": page([1], second)},
                      {"declared_page_url": second, "page_body": page([2])}]), prop, 55)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row["saved_page_url_reference"].startswith(PAGE_URL_REDACTED + "@sha256:"))
            self.assertNotIn(token, row["saved_page_url_reference"])
        retained = bytes.fromhex(rows[0]["source_record_hex"][4:]).decode()
        self.assertIn(token, retained)
        self.assertEqual(json.loads(retained)["@iot.nextLink"], second)
        self.assertNotIn(token, output)
        self.assertIn(token.encode().hex(), rows[0]["source_record_hex"])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "token-chain.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            units = common["profile_units"]
            self.assertIn("not_secret_scrubbed", units["source_record_hex"])
            self.assertIn("not_a_redaction", units["source_record_hex"])
            self.assertIn("not_a_scrub_of_the_output", units["saved_page_url_reference"])

        with self.assertRaises(ValueError) as caught:
            sensorthings_saved_pages(
                manifest([{"declared_page_url": first, "page_body": page([1], second)},
                          {"declared_page_url": "https://host.invalid/v1.1/Observations?$skip=9",
                           "page_body": page([2])}]), prop, 55)
        self.assertEqual(str(caught.exception),
                         "saved chain break: nextLink does not match the next declared page url")
        self.assertNotIn(token, str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            sensorthings_saved_pages(
                manifest([{"declared_page_url": first,
                           "page_body": json.dumps([{"@iot.nextLink": second}])}]), prop, 55)
        self.assertEqual(str(caught.exception), "saved page body must be a JSON object")
        self.assertNotIn(token, str(caught.exception))

    def test_sensorthings_reuses_units_and_common_output_without_time_or_quality_inference(self):
        import copy
        for prop, value in (("urn:example:air-temperature", 25), ("urn:example:liquid-temperature", -10)):
            item = {"@iot.id": 1, "phenomenonTime": "2026-01-01T01:00:00+01:00", "resultTime": None,
                    "result": value, "parameters": {"unknown": "保持"}, "resultQuality": ["unassessed"],
                    "Datastream": {"@iot.id": "temperature-stream", "ObservedProperty": {"definition": prop},
                        "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                        "unitOfMeasurement": {"name": "degree Celsius", "symbol": "°C",
                            "definition": "http://unitsofmeasure.org/ucum.html#para-30"}}}
            source = {"value": [item, dict(item, **{"@iot.id": 2, "result": None})],
                      "@iot.nextLink": "https://example.invalid/next"}
            text = json.dumps(source, ensure_ascii=False)
            output, report = convert_sensorthings(text, prop, 55)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["records"], report["converted_values"], report["withheld_values"]), (2, 1, 1))
            self.assertEqual(report["collection_scope"], "PAGE_ONLY_NEXT_LINK_PRESENT")
            self.assertEqual(float(rows[0]["temperature_k"]), value + 273.15)
            self.assertEqual(rows[0]["phenomenon_time_us"], "1767225600000000")
            self.assertEqual(rows[0]["record_time_us"], "55")
            self.assertEqual(rows[0]["result_time_us"], "")
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(), text)
            self.assertEqual(rows[1]["source_record_hex"], "")
            self.assertEqual(rows[1]["value_disposition"], "NULL_NOT_PROVIDED")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["temperature_k"], value + 273.15)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                input_path = Path(directory) / "in.json"; input_path.write_text(text)
                command = [sys.executable, "-m", "scripts.convert_opcua_recorded", str(input_path),
                    str(Path(directory) / "cli.csv"), str(Path(directory) / "report.json"),
                    "--source-format", "sensorthings-1.1-celsius", "--observed-property", prop, "--capture-time-us", "55"]
                subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
                self.assertEqual((Path(directory) / "cli.csv").read_text(), output)
            bad = []
            for key, replacement in (("result", True), ("result", []), ("result", -274),
                                      ("phenomenonTime", "2026-01-01T00:00:00"), ("resultTime", 5)):
                changed = copy.deepcopy(source); changed["value"][0][key] = replacement; bad.append(changed)
            changed = copy.deepcopy(source); changed["value"][0]["Datastream"]["unitOfMeasurement"]["symbol"] = "K"; bad.append(changed)
            changed = copy.deepcopy(source); changed["value"][0]["Datastream"]["ObservedProperty"] = []; bad.append(changed)
            changed = copy.deepcopy(source); changed["value"][1]["@iot.id"] = 1; bad.append(changed)
            for changed in bad:
                with self.assertRaises(ValueError): convert_sensorthings(json.dumps(changed), prop, 55)
            with self.assertRaises(ValueError): convert_sensorthings(text, "wrong-property", 55)

    def test_reported_quality_does_not_promote_withheld_values(self):
        codes = [0xc000, 0x0400 | 0x0200 | 0x80 | 0x15, 0x0400 | 0x0100 | 0x0a,
                 0x0800 | 0x80, 0x30003000 | 0x0400 | 0x60]
        source = [record(24, code) for code in codes]
        output, report = convert("\n".join(json.dumps(item) for item in source), source[0]["node_id"], "V")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["converted_values"], report["withheld_values"]), (0, 5))
        self.assertEqual(rows[0]["metadata_refresh_required"], "1")
        self.assertEqual(rows[0]["structure_changed_reported"], "1")
        self.assertEqual(rows[0]["semantics_changed_reported"], "1")
        self.assertEqual(rows[1]["limit_reported"], "HIGH")
        self.assertEqual(rows[1]["queue_overflow_reported"], "1")
        self.assertEqual(rows[1]["historian_origin_reported"], "CALCULATED")
        self.assertEqual(rows[1]["historian_partial_reported"], "1")
        self.assertEqual(rows[1]["historian_multi_value_reported"], "1")
        self.assertEqual(rows[2]["historian_origin_reported"], "INTERPOLATED")
        self.assertEqual(rows[2]["historian_extra_data_reported"], "1")
        self.assertEqual(rows[3]["status_info_type"], "RESERVED")
        self.assertEqual(rows[3]["queue_overflow_reported"], "")
        self.assertEqual(int(rows[4]["status_reserved_bits"]), 0x30003060)
        self.assertTrue(all(row["voltage_v"] == "" for row in rows))
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "quality.csv"; path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 5)
                self.assertEqual(common["observations"][1]["fields"]["queue_overflow_reported"], 1)
                self.assertEqual(common["observations"][2]["fields"]["historian_origin_reported"], "INTERPOLATED")

    def test_shared_unit_reuse_and_scaled_integer_boundary(self):
        cases = [("degK", "temperature_k", 10), ("degC", "temperature_k", 283.15),
                 ("rpm", "angular_speed_rad_s", math.pi / 3),
                 ("mV", "voltage_v", .01), ("mA", "current_a", .01),
                 ("mm", "distance_m", .01), ("cm", "distance_m", .1),
                 ("m/s", "speed_m_s", 10), ("km/h", "speed_m_s", 10 / 3.6),
                 ("rad", "angle_rad", 10), ("deg", "angle_rad", math.pi / 18),
                 ("rad/s", "angular_speed_rad_s", 10), ("deg/s", "angular_speed_rad_s", math.pi / 18),
                 ("hPa", "pressure_pa", 1000), ("kPa", "pressure_pa", 10000),
                 ("bar", "pressure_pa", 1000000), ("%", "reported_ratio", .1),
                 ("J", "energy_j", 10), ("Wh", "energy_j", 36000)]
        for unit, field, expected in cases:
            source = record(10)
            output, _ = convert(json.dumps(source), source["node_id"], unit)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertAlmostEqual(float(row[field]), expected)
            self.assertEqual(row["selected_unit"], unit)
        for value in (36, -72):
            source = record(value)
            output, _ = convert(json.dumps(source), source["node_id"], "km/h")
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "speed.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 1)
                    self.assertEqual(common["observations"][0]["fields"]["speed_m_s"], value / 3.6)
                    self.assertEqual(common["observations"][0]["fields"]["selected_unit"], "km/h")
        source = record(); source["data_value"].update(UaType=8, Value=str(2**63 - 1))
        for unit in ("mm", "bar"):
            with self.assertRaises(ValueError):
                convert(json.dumps(source), source["node_id"], unit)
        for value, unit in ((-274, "degC"), (1e308, "Wh")):
            source = record(value)
            with self.assertRaises(ValueError):
                convert(json.dumps(source), source["node_id"], unit)

    def test_numeric_type_boundaries_without_integer_rounding(self):
        bounds = {2: (-128, 127), 3: (0, 255), 4: (-32768, 32767), 5: (0, 65535),
                  6: (-2147483648, 2147483647), 7: (0, 4294967295),
                  8: (-9223372036854775808, 9223372036854775807), 9: (0, 9223372036854775807)}
        inputs = []
        for kind, (low, high) in bounds.items():
            for value in (low, high):
                item = record(); item["data_value"].update(UaType=kind, Value=str(value) if kind >= 8 else value)
                output, _ = convert(json.dumps(item), item["node_id"], "A")
                self.assertEqual(int(next(csv.DictReader(io.StringIO(output)))["current_a"]), value)
                inputs.append(item)
            for value in (low - 1, high + 1):
                item = record(); item["data_value"].update(UaType=kind, Value=str(value) if kind >= 8 else value)
                with self.assertRaises(ValueError):
                    convert(json.dumps(item), item["node_id"], "A")
        for kind, value in ((2, 1.5), (3, True), (8, 1), (9, "1.0"), (10, 1e39)):
            item = record(); item["data_value"].update(UaType=kind, Value=value)
            with self.assertRaises(ValueError):
                convert(json.dumps(item), item["node_id"], "A")
        for kind in (10, 11):
            item = record(); item["data_value"].update(UaType=kind, Value=1.25)
            inputs.append(item)
        item = record(); item["data_value"].update(UaType=6, Value="OVERFLOW")
        with self.assertRaises(ValueError):
            convert(json.dumps(item).replace('"OVERFLOW"', '1e400'), item["node_id"], "A")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            for batch in (inputs[:8], inputs[8:]):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "numeric.csv"
                    output, _ = convert("\n".join(json.dumps(i) for i in batch), batch[0]["node_id"], "A")
                    path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(result["main_rows"], len(batch))
                    for obs, original in zip(result["observations"], batch):
                        value = original["data_value"]["Value"]
                        self.assertEqual(obs["fields"]["current_a"], int(value) if isinstance(value, str) else value)

    def test_cli_refuses_overwriting_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source, report = Path(directory) / "in.jsonl", Path(directory) / "report.json"
            text = json.dumps(record())
            source.write_text(text)
            result = subprocess.run([sys.executable, "-m", "scripts.convert_opcua_recorded",
                str(source), str(source), str(report), "--node", record()["node_id"], "--unit", "V"],
                cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_text(), text)
            self.assertFalse(report.exists())

    def test_reuse_status_clock_and_common_output(self):
        for value in (12.5, 24.0):
            source = [record(value, code) for code in (0, 0x40000000, 0x80000000, 0xc0000000, 1)]
            text = "\n".join(json.dumps(row) for row in source)
            output, report = convert(text, source[0]["node_id"], "V")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(report["converted_values"], 1)
            self.assertEqual(report["withheld_values"], 4)
            self.assertEqual(float(rows[0]["voltage_v"]), value)
            self.assertTrue(all(row["voltage_v"] == "" for row in rows[1:]))
            self.assertEqual(rows[0]["record_time_us"], "15")
            self.assertEqual(rows[0]["source_10ps"], "4")
            self.assertEqual(bytes.fromhex(rows[0]["source_timestamp_hex"][4:]).decode(), source[0]["data_value"]["SourceTimestamp"])
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), source[0])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"
                    path.write_text(output)
                    result = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(result["main_rows"], 5)
                    self.assertEqual(result["observations"][0]["fields"]["voltage_v"], value)

    def test_one_dimensional_double_arrays_reuse_the_scalar_path(self):
        """Part 6 1.05.07 5.4.5: a 1-D array is a JSON array; status and time stay per DataValue."""
        from scripts.convert_opcua_recorded import ARRAY_ELEMENT, ARRAY_EMPTY, MAX_ARRAY_ELEMENTS
        for unit, field, values, second in (("V", "voltage_v", [1.5, -2.25, 0.0], [12.0, 13.5]),
                                            ("K", "temperature_k", [300.5, 301.25], [77.0, 78.5, 79.75, 80.0])):
            first = record(values)
            later = record(second); later["capture_time_us"] = 16
            text = "\n".join(json.dumps(row) for row in (first, later))
            output, report = convert(text, first["node_id"], unit, arrays=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), len(values) + len(second))
            self.assertEqual((report["records"], report["arrays"], report["array_elements"]),
                             (2, 2, len(values) + len(second)))
            self.assertEqual(report["converted_values"], len(values) + len(second))
            self.assertEqual(report["empty_arrays"], 0)
            self.assertEqual([int(row["opcua_element_index"]) for row in rows],
                             list(range(len(values))) + list(range(len(second))))
            self.assertEqual([int(row["opcua_array_ordinal"]) for row in rows],
                             [1] * len(values) + [2] * len(second))
            self.assertEqual([int(row["opcua_array_length"]) for row in rows],
                             [len(values)] * len(values) + [len(second)] * len(second))
            self.assertEqual([float(row[field]) for row in rows], [float(v) for v in values + second])
            self.assertTrue(all(row["opcua_array_disposition"] == ARRAY_ELEMENT for row in rows))
            self.assertTrue(all(row["value_disposition"] == "NORMALIZED_REPORTED" for row in rows))
            self.assertTrue(all(row["selected_unit"] == unit for row in rows))
            self.assertEqual({row["record_time_us"] for row in rows[:len(values)]}, {"15"})
            self.assertEqual({row["source_10ps"] for row in rows}, {"4"})
            self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), first)
            self.assertEqual({row["node_id_hex"] for row in rows},
                             {"hex:" + first["node_id"].encode().hex()})
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"
                    path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/opcua-recorded/profile.toml"),
                        str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], len(rows))
                self.assertEqual([observation["fields"][field] for observation in common["observations"]],
                                 [float(v) for v in values + second])
                self.assertEqual(common["observations"][1]["fields"]["opcua_element_index"], 1)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                units = common["profile_units"]
                for column in ("opcua_array_ordinal", "opcua_array_length", "opcua_element_index",
                               "opcua_array_disposition"):
                    self.assertTrue(units.get(column), f"{column} must declare its meaning")
                self.assertIn("never_an_axis_a_component", units["opcua_element_index"])
                self.assertIn("not_a_value", units["opcua_array_length"])

        empty = record([])
        output, report = convert(json.dumps(empty), empty["node_id"], "V", arrays=True)
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["records"], report["arrays"], report["empty_arrays"],
                          report["array_elements"], report["converted_values"]), (1, 1, 1, 0, 0))
        self.assertEqual((row["opcua_array_length"], row["opcua_element_index"]), ("0", ""))
        self.assertEqual(row["opcua_array_disposition"], ARRAY_EMPTY)
        self.assertEqual(row["value_disposition"], "NO_ELEMENT_REPORTED_IN_THIS_ARRAY")
        self.assertEqual(row["voltage_v"], "")

        bad = record([1.0, 2.0], 0x80000000)
        output, report = convert(json.dumps(bad), bad["node_id"], "V", arrays=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["withheld_values"], report["converted_values"]), (2, 0))
        self.assertTrue(all(row["voltage_v"] == "" and row["status_severity"] == "BAD_REPORTED"
                            and row["value_disposition"] == "WITHHELD_STATUS_NOT_EXACT_GOOD"
                            for row in rows))
        self.assertEqual([row["opcua_element_index"] for row in rows], ["0", "1"])

        large = record([float(index) / 4 for index in range(300)])
        output, report = convert(json.dumps(large), large["node_id"], "V", arrays=True)
        self.assertEqual(report["array_elements"], 300)
        self.assertEqual(float(list(csv.DictReader(io.StringIO(output)))[299]["voltage_v"]), 74.75)
        with self.assertRaises(ValueError) as caught:
            convert(json.dumps(record([1.0] * (MAX_ARRAY_ELEMENTS + 1))), large["node_id"], "V",
                    arrays=True)
        self.assertEqual(str(caught.exception), "array exceeds the expanded element bound")

    def test_array_mode_refuses_undefined_and_unsupported_forms(self):
        node = record()["node_id"]
        for change, message in (
                ({"Value": [1.0, None]}, "array element is not a defined JSON number"),
                ({"Value": [1.0, "NaN"]}, "array element is not a defined JSON number"),
                ({"Value": ["Infinity"]}, "array element is not a defined JSON number"),
                ({"Value": [True]}, "array element is not a defined JSON number"),
                ({"Value": [[1.0]]}, "array element is not a defined JSON number"),
                ({"Value": [1.0], "Dimensions": [1]}, "unsupported array dimensions"),
                ({"Value": [1.0], "Dimensions": [True]}, "unsupported array dimensions"),
                ({"Value": [1.0], "Dimensions": [1.0]}, "unsupported array dimensions"),
                ({"Value": [1.0], "Dimensions": [2]}, "unsupported array dimensions"),
                ({"Value": [1.0, 2.0, 3.0, 4.0], "Dimensions": [2, 2]}, "unsupported array dimensions"),
                ({"Value": [1.0], "Dimensions": 1}, "unsupported array dimensions"),
                ({"Value": [1.0], "Dimensions": []}, "unsupported array dimensions"),
                ({"Value": 12.5}, "selected array DataValue required"),
                ({"Value": {"Body": [1.0]}}, "selected array DataValue required"),
                ({"Value": [1.0], "UaType": 10}, "explicit selected UaType 11 array required")):
            entry = record()
            entry["data_value"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError) as caught:
                convert(json.dumps(entry), node, "V", arrays=True)
            self.assertEqual(str(caught.exception), message)
        for value in ([1.0, [2.0]], [None], [True], ["NaN"], [{"Body": 1.0}]):
            for code in (0x80000000, 0x40000000, 1):
                entry = record(value, code)
                with self.subTest(value=value, code=code), self.assertRaises(ValueError) as caught:
                    convert(json.dumps(entry), node, "V", arrays=True)
                self.assertEqual(str(caught.exception),
                                 "array element is not a defined JSON number")
        withheld = record([1.0, 2.0], 0x40000000)
        output, report = convert(json.dumps(withheld), node, "V", arrays=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((report["withheld_values"], report["converted_values"]), (2, 0))
        self.assertTrue(all(row["voltage_v"] == "" and row["status_severity"] == "UNCERTAIN_REPORTED"
                            and row["value_disposition"] == "WITHHELD_STATUS_NOT_EXACT_GOOD"
                            for row in rows))
        self.assertEqual([row["opcua_element_index"] for row in rows], ["0", "1"])
        with self.assertRaises(ValueError) as caught:
            convert(json.dumps(record([1.0])), node, "V")
        self.assertEqual(str(caught.exception), "scalar DataValue required")
        with self.assertRaises(ValueError):
            convert(json.dumps(record(True)), node, boolean=True, arrays=True)

    def test_boundaries_and_missing_optional_time_status(self):
        r = record()
        r["data_value"] = {"UaType": 11, "Value": 2}
        output, report = convert(json.dumps(r), r["node_id"], "A")
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["source_time_us"], "")
        self.assertEqual(row["current_a"], "2.0")
        for change in ({"Value": True}, {"Value": "NaN"}, {"UaType": 1}, {"Dimensions": []},
                       {"Status": {"Code": -1}}, {"Status": {"Symbol": "Bad"}},
                       {"SourceTimestamp": "2026-02-30T00:00:00Z"}):
            r = record()
            r["data_value"].update(change)
            with self.assertRaises((ValueError, TypeError)):
                convert(json.dumps(r), r["node_id"], "V")
        with self.assertRaises(ValueError):
            convert(json.dumps(record(-1)), record()["node_id"], "K")
        for unit, field in (("K", "temperature_k"), ("m", "distance_m"), ("Pa", "pressure_pa")):
            output, _ = convert(json.dumps(record(5)), record()["node_id"], unit)
            self.assertEqual(float(next(csv.DictReader(io.StringIO(output)))[field]), 5)
