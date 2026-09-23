"""Authored CAP 1.2 alert files only. No alerting service, no live feed, no real message.

Every fixture here is written by this file. Nothing is published, acknowledged or dispatched, and
no network is touched. The chain is the advertised one: authored CAP XML through this converter
and through the **real** common reader.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.convert_cap_alert import FIELDS, MAX_OUTPUT, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/cap-alert/profile.toml"

AREA_ONE = """
    <area>
      <areaDesc>Left bank between the two road bridges</areaDesc>
      <circle>38.47,-120.14 12.5</circle>
      <geocode><valueName>SAME</valueName><value>006113</value></geocode>
      <altitude>120</altitude>
      <ceiling>350</ceiling>
    </area>"""
AREA_TWO = """
    <area>
      <areaDesc>Downstream flood plain</areaDesc>
      <polygon>38.47,-120.14 38.34,-119.95 38.52,-119.74 38.47,-120.14</polygon>
    </area>"""


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


def alert(info=None, identifier="KSTO-2026-0912-001", sender="w-nws.webmaster@example.test",
          sent="2026-09-12T14:05:00-07:00", status="Actual", msg_type="Alert",
          scope="Public", extra=""):
    blocks = "" if info is None else info
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>%s</identifier>
  <sender>%s</sender>
  <sent>%s</sent>
  <status>%s</status>
  <msgType>%s</msgType>
  <scope>%s</scope>%s%s
</alert>
""" % (identifier, sender, sent, status, msg_type, scope, extra, blocks)).encode("utf-8")


def info(categories=("Met",), event="Flood observation", urgency="Expected", severity="Moderate",
         certainty="Observed", areas=AREA_ONE, extra=""):
    return ("""
  <info>
%s
    <event>%s</event>
    <urgency>%s</urgency>
    <severity>%s</severity>
    <certainty>%s</certainty>%s%s
  </info>""" % ("\n".join("    <category>%s</category>" % c for c in categories),
                event, urgency, severity, certainty, extra, areas))


def rows_of(data):
    output, report = convert(data)
    return list(csv.DictReader(io.StringIO(output))), report, output


class CapAlert(unittest.TestCase):
    def test_a_reported_alert_reaches_common_output_with_its_declaration_attached(self):
        rows, report, _ = rows_of(alert(info(areas=AREA_ONE + AREA_TWO)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(report["areas_read"], 2)
        first, second = rows
        self.assertEqual(first["cap_row_kind"], "AREA")
        self.assertEqual(first["alert_identifier"], "KSTO-2026-0912-001")
        self.assertEqual(first["alert_msg_type"], "Alert")
        self.assertEqual(first["alert_scope"], "Public")
        self.assertEqual(first["info_severity"], "Moderate")
        self.assertEqual(first["info_certainty"], "Observed")
        self.assertEqual(unhex(first["info_event_hex"]), "Flood observation")
        self.assertEqual(int(first["record_time_us"]), 1789247100 * 1000000)
        self.assertEqual(first["alert_sent_offset"], "-07:00")
        self.assertEqual(first["record_time_us"], second["record_time_us"])
        self.assertEqual(first["area_circle_latitude_deg"], "38.47")
        self.assertEqual(first["area_circle_longitude_deg"], "-120.14")
        self.assertEqual(first["area_circle_radius_km"], "12.5")
        self.assertEqual(first["area_altitude_ft"], "120")
        self.assertEqual(first["area_ceiling_ft"], "350")
        self.assertEqual(json.loads(unhex(first["area_geocode_pairs_hex"])), [["SAME", "006113"]])
        self.assertIn("KILOMETRES", first["geometry_basis"])
        self.assertIn("NOT_PROOF_OF_COMPATIBILITY", first["geometry_basis"])
        self.assertEqual(second["area_polygon_count"], "1")
        self.assertEqual(second["area_polygon_point_count"], "4")
        self.assertEqual(second["area_index"], "1")
        self.assertEqual(second["area_count"], "2")
        self.assertIn("NOT_INDEPENDENTLY_VERIFIED", first["classification_basis"])
        self.assertEqual(report["verified_incidents"],
                         "NONE_EVERY_CLASSIFICATION_ON_EVERY_ROW_REMAINS_THE_SENDERS_REPORT")
        self.assertIn("PUBLISHES_NOTHING", report["published_alerts"])

    def test_every_declared_category_is_carried_not_only_the_first(self):
        rows, _, _ = rows_of(alert(info(categories=("Rescue", "Safety", "Infra"))))
        self.assertEqual(rows[0]["info_categories"], "Rescue+Safety+Infra")
        self.assertEqual(rows[0]["info_category_count"], "3")

    def test_a_declared_unknown_is_kept_apart_from_an_absent_element(self):
        stated, _, _ = rows_of(alert(info()))
        self.assertIn("NONE_the_producer_gave_a_specific_value",
                      stated[0]["declared_unknown_basis"])
        unknown, _, _ = rows_of(alert(info(severity="Unknown", certainty="Unknown")))
        self.assertEqual(unknown[0]["info_severity"], "Unknown")
        self.assertIn("EXPLICITLY_DECLARED_Unknown_FOR_severity+certainty",
                      unknown[0]["declared_unknown_basis"])
        with self.assertRaises(ValueError):
            convert(alert(info().replace("<severity>Moderate</severity>", "")))

    def test_a_message_that_declares_less_still_produces_exactly_one_row(self):
        no_area, report, _ = rows_of(alert(info(areas="")))
        self.assertEqual(len(no_area), 1)
        self.assertEqual(no_area[0]["cap_row_kind"], "INFO_WITHOUT_AREA")
        self.assertEqual(no_area[0]["area_desc_hex"], "")
        self.assertIn("NO_AREA_BLOCK", no_area[0]["geometry_basis"])
        self.assertEqual(report["areas_read"], 0)

        no_info, report, _ = rows_of(alert())
        self.assertEqual(len(no_info), 1)
        self.assertEqual(no_info[0]["cap_row_kind"], "ALERT_WITHOUT_INFO")
        self.assertEqual(no_info[0]["info_categories"], "")
        self.assertIn("NO_INFO_BLOCK", no_info[0]["declared_unknown_basis"])
        self.assertEqual(report["info_blocks_read"], 0)
        self.assertEqual(no_info[0]["alert_identifier"], "KSTO-2026-0912-001")

    def test_several_info_blocks_each_keep_their_own_classification(self):
        rows, _, _ = rows_of(alert(info(categories=("Met",), areas="")
                                   + info(categories=("Rescue",), severity="Severe", areas="")))
        self.assertEqual([r["info_index"] for r in rows], ["0", "1"])
        self.assertEqual([r["info_count"] for r in rows], ["2", "2"])
        self.assertEqual([r["info_categories"] for r in rows], ["Met", "Rescue"])
        self.assertEqual([r["info_severity"] for r in rows], ["Moderate", "Severe"])

    def test_an_update_carries_its_references_and_says_when_it_has_none(self):
        references = ("<references>w-nws.webmaster@example.test,KSTO-2026-0912-001,"
                      "2026-09-12T14:05:00-07:00</references>")
        rows, _, _ = rows_of(alert(info(), msg_type="Update", extra="\n  " + references))
        self.assertEqual(rows[0]["alert_msg_type"], "Update")
        self.assertEqual(rows[0]["alert_reference_count"], "1")
        self.assertEqual(unhex(rows[0]["alert_references_hex"]).count(","), 2)
        self.assertIn("UPDATE_REFERENCING_1_EARLIER", rows[0]["update_basis"])

        bare, _, _ = rows_of(alert(info(), msg_type="Update"))
        self.assertIn("NOT_IDENTIFIED_BY_THIS_FILE", bare[0]["update_basis"])

    def test_several_circles_are_all_kept_and_none_is_chosen(self):
        two = """
    <area>
      <areaDesc>Two reported points</areaDesc>
      <circle>38.47,-120.14 12.5</circle>
      <circle>39.10,-121.00 3</circle>
    </area>"""
        rows, _, _ = rows_of(alert(info(areas=two)))
        self.assertEqual(rows[0]["area_circle_count"], "2")
        self.assertEqual(rows[0]["area_circle_latitude_deg"], "")
        self.assertEqual(json.loads(unhex(rows[0]["area_circle_list_hex"])),
                         ["38.47,-120.14 12.5", "39.10,-121.00 3"])

    def test_a_magnitude_is_judged_exactly_and_never_through_a_float(self):
        def circled(text):
            return alert(info(areas="""
    <area><areaDesc>x</areaDesc><circle>%s</circle></area>""" % text))
        with self.assertRaises(ValueError, msg="a radius that underflows to zero at the reader"):
            convert(circled("38.47,-120.14 1e-400"))
        with self.assertRaises(ValueError, msg="a radius that overflows the reader's float"):
            convert(circled("38.47,-120.14 1e400"))
        self.assertEqual(float("90.00000000000000001"), 90.0)
        with self.assertRaises(ValueError, msg="a latitude just outside the real range"):
            convert(circled("90.00000000000000001,-120.14 5"))
        with self.assertRaises(ValueError, msg="a longitude just outside the real range"):
            convert(circled("38.47,-180.00000000000000001 5"))
        rows, _, _ = rows_of(circled("90,-180 0"))
        self.assertEqual(rows[0]["area_circle_latitude_deg"], "90")

    def test_polygon_closure_is_compared_by_value_not_by_spelling(self):
        same = alert(info(areas="""
    <area><areaDesc>x</areaDesc>
      <polygon>38.47,-120.14 38.34,-119.95 38.52,-119.74 38.470,-120.1400</polygon></area>"""))
        rows, _, _ = rows_of(same)
        self.assertEqual(rows[0]["area_polygon_point_count"], "4")
        with self.assertRaises(ValueError):
            convert(alert(info(areas="""
    <area><areaDesc>x</areaDesc>
      <polygon>38.47,-120.14 38.34,-119.95 38.52,-119.74 38.471,-120.14</polygon></area>""")))

    def test_only_a_real_utc_offset_is_accepted(self):
        rows, _, _ = rows_of(alert(info(), sent="2026-09-12T14:05:00+14:00"))
        self.assertEqual(rows[0]["alert_sent_offset"], "+14:00")
        for offset, why in (("+14:59", "14 hours permits no minutes beside it"),
                            ("+15:00", "no zone is 15 hours from UTC"),
                            ("-14:01", "one minute past the largest real offset")):
            with self.assertRaises(ValueError, msg=why):
                convert(alert(info(), sent="2026-09-12T14:05:00" + offset))

    def test_a_dtd_is_refused_whatever_the_encoding_or_the_offset(self):
        body = '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"/>'
        for data, why in (
                (('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE alert>' + body)
                 .encode("utf-16"), "a DOCTYPE declared in UTF-16, which a byte scan would miss"),
                (b"<!--" + b"x" * 9000 + b"--><!DOCTYPE alert>" + body.encode(),
                 "a DOCTYPE pushed past a fixed-size prefix by a long comment"),
                (b'<!DOCTYPE l [<!ENTITY a "aa"><!ENTITY b "&a;&a;">]>' + body.encode(),
                 "entity declarations this converter will not expand"),
                (b'<!DOCTYPE a SYSTEM "http://example.test/e.dtd">' + body.encode(),
                 "an external DTD reference")):
            with self.assertRaises(ValueError, msg=why):
                convert(data)
        utf16 = ('<?xml version="1.0" encoding="UTF-16"?>'
                 + alert(info()).decode().split("?>", 1)[1]).encode("utf-16")
        rows, _, _ = rows_of(utf16)
        self.assertEqual(rows[0]["alert_identifier"], "KSTO-2026-0912-001")

    def test_input_and_output_are_bounded(self):
        from scripts.convert_cap_alert import MAX_INPUT, MAX_ROWS
        with self.assertRaises(ValueError, msg="an input past the byte bound"):
            convert(b"<!--" + b"x" * (MAX_INPUT + 1) + b"-->")
        areas = "<area><areaDesc>a</areaDesc></area>" * 256
        many = alert(info(areas=areas) * 20)
        self.assertLess(len(many), MAX_INPUT)
        with self.assertRaises(ValueError, msg="more rows than the output bound"):
            convert(many)
        fine = alert(info(areas=areas) * 15)
        self.assertEqual(len(rows_of(fine)[0]), 15 * 256)
        self.assertLessEqual(15 * 256, MAX_ROWS)

    def test_the_expanded_output_is_bounded_while_it_is_written(self):
        data = alert(info(event="E" * 20000,
                          areas="<area><areaDesc>a</areaDesc></area>" * 256))
        self.assertLess(len(data), 100 * 1024)
        expanded, report = convert(data, output_budget=MAX_OUTPUT)
        self.assertEqual(report["rows"], 256)
        self.assertGreater(len(expanded), 100 * len(data))
        with self.assertRaises(ValueError, msg="an expansion past the byte budget") as caught:
            convert(data, output_budget=1_000_000)
        self.assertIn("Nothing is truncated", str(caught.exception))

    def test_the_output_budget_counts_utf8_bytes_not_characters(self):
        data = alert(info(areas="<area><areaDesc>a</areaDesc></area>" * 64),
                     sender="雨" * 100)
        output, report = convert(data, output_budget=MAX_OUTPUT)
        self.assertEqual(report["rows"], 64)
        characters, utf8 = len(output), len(output.encode("utf-8"))
        self.assertGreater(utf8, characters)
        between = (characters + utf8) // 2
        self.assertLess(characters, between)
        self.assertLess(between, utf8)
        with self.assertRaises(ValueError, msg="an output past the byte budget") as caught:
            convert(data, output_budget=between)
        self.assertIn("UTF-8 bytes", str(caught.exception))
        self.assertIn("Nothing is truncated", str(caught.exception))

    def test_a_scalar_field_with_nested_children_is_refused_not_half_read(self):
        nested = alert(info()).replace(
            b"<event>Flood observation</event>",
            b"<event>Flood observation<extension>and more</extension></event>")
        with self.assertRaises(ValueError, msg="a scalar field hiding a child element") as caught:
            convert(nested)
        self.assertIn("event", str(caught.exception))
        rows, _, _ = rows_of(alert(info(areas=AREA_ONE)))
        self.assertEqual(json.loads(unhex(rows[0]["area_geocode_pairs_hex"])), [["SAME", "006113"]])

    def test_a_duplicate_singleton_is_refused_rather_than_half_read(self):
        for data, why in (
                (alert(info()).replace(b"<scope>Public</scope>",
                                       b"<scope>Public</scope><scope>Private</scope>"),
                 "two different scopes, where find() would have taken the first"),
                (alert(info()).replace(b"<severity>Moderate</severity>",
                                       b"<severity>Moderate</severity><severity>Extreme</severity>"),
                 "two different severities"),
                (alert(info(areas="""
    <area><areaDesc>one</areaDesc><areaDesc>two</areaDesc></area>""")),
                 "two area descriptions")):
            with self.assertRaises(ValueError, msg=why):
                convert(data)
        rows, _, _ = rows_of(alert(info(categories=("Met", "Safety"))))
        self.assertEqual(rows[0]["info_categories"], "Met+Safety")

    def test_elements_given_no_column_are_counted_and_never_claimed_to_be_carried(self):
        rich = alert(info(extra="<headline>Levee overtopping</headline>"
                                "<description>Water over the crown.</description>"
                                "<instruction>Avoid the left bank.</instruction>"
                                '<parameter><valueName>k</valueName><value>v</value></parameter>'),
                     extra="\n  <note>internal</note><code>profile:x</code>")
        rows, report, _ = rows_of(rich)
        omitted = dict(json.loads(unhex(rows[0]["omitted_known_hex"])))
        self.assertEqual(omitted, {"alert/note": 1, "alert/code": 1, "info/headline": 1,
                                   "info/description": 1, "info/instruction": 1,
                                   "info/parameter": 1})
        self.assertEqual(rows[0]["omitted_known_count"], "6")
        self.assertEqual(report["known_elements_not_carried"], omitted)
        for secret in ("Levee overtopping", "Water over the crown.", "Avoid the left bank."):
            self.assertNotIn(secret.encode().hex(), rows_of(rich)[2])
        self.assertIn("NOT_CARRIED", rows[0]["recovery_basis"])
        self.assertIn("THE_CALLER_RETAINS", rows[0]["recovery_basis"])
        self.assertEqual(rows[0]["source_path"], "/alert/info[0]/area[0]")
        self.assertEqual(len(rows[0]["source_sha256"]), 64)

    def test_the_report_total_counts_each_scope_once_and_every_scope(self):
        data = alert(info(extra="<headline>one</headline>", areas="")
                     + info(extra="<headline>two</headline>", areas=""),
                     extra="\n  <note>n</note>")
        rows, report, _ = rows_of(data)
        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["known_elements_not_carried"],
                         {"alert/note": 1, "info/headline": 2})
        self.assertEqual([r["omitted_known_count"] for r in rows], ["2", "2"])

    def test_an_element_outside_the_pinned_schema_is_counted_not_dropped(self):
        rows, report, _ = rows_of(alert(info(
            extra='<x:localField xmlns:x="urn:example:vendor">99</x:localField>')))
        self.assertEqual(rows[0]["unknown_element_count"], "1")
        self.assertEqual(json.loads(unhex(rows[0]["unknown_element_hex"])),
                         [["{urn:example:vendor}localField", 1]])
        self.assertEqual(report["unknown_elements_not_carried"],
                         {"{urn:example:vendor}localField": 1})
        self.assertNotIn("99".encode().hex(), rows_of(alert(info(
            extra='<x:localField xmlns:x="urn:example:vendor">99</x:localField>')))[2])

    def test_a_repeat_containing_the_join_character_still_round_trips(self):
        tricky = """
    <area><areaDesc>x</areaDesc>
      <circle>38.47,-120.14 1.5e+1</circle>
      <geocode><valueName>ZONE+A</valueName><value>1+2</value></geocode>
      <geocode><valueName>B</valueName><value>3</value></geocode></area>"""
        rows, _, _ = rows_of(alert(info(areas=tricky)))
        self.assertEqual(json.loads(unhex(rows[0]["area_geocode_pairs_hex"])),
                         [["ZONE+A", "1+2"], ["B", "3"]])
        self.assertEqual(json.loads(unhex(rows[0]["area_circle_list_hex"])),
                         ["38.47,-120.14 1.5e+1"])
        self.assertEqual(rows[0]["area_circle_radius_km"], "1.5e+1")

    def test_the_producers_own_number_text_is_not_reformatted(self):
        exact = """
    <area>
      <areaDesc>Exact text</areaDesc>
      <circle>38.4700,-120.140 1.250</circle>
    </area>"""
        rows, _, _ = rows_of(alert(info(areas=exact)))
        self.assertEqual(rows[0]["area_circle_latitude_deg"], "38.4700")
        self.assertEqual(rows[0]["area_circle_radius_km"], "1.250")

    def test_the_two_utc_spellings_are_distinguished_rather_than_normalised(self):
        required, _, _ = rows_of(alert(info(), sent="2026-09-12T21:05:00-00:00"))
        self.assertIn("MINUS_ZERO_EXACTLY_AS_THE_PINNED_SCHEMA_REQUIRES",
                      required[0]["cap_time_basis"])
        deviating, _, _ = rows_of(alert(info(), sent="2026-09-12T21:05:00+00:00"))
        self.assertIn("DEVIATES_FROM_THE_PINNED_SCHEMA", deviating[0]["cap_time_basis"])
        self.assertEqual(required[0]["record_time_us"], deviating[0]["record_time_us"])
        self.assertEqual(deviating[0]["alert_sent_text"], "2026-09-12T21:05:00+00:00")

    def test_the_important_failures_are_refused_rather_than_repaired(self):
        for data, why in (
                (alert(info(), sent="2026-09-12T21:05:00Z"),
                 "the alphabetic designator the pinned schema forbids"),
                (alert(info(), sent="2026-09-12T21:05:00"), "a time with no offset at all"),
                (alert(info(), sent="2026-09-12T21:05:00.1234567-00:00"),
                 "precision finer than a microsecond, which would have to be rounded away"),
                (alert(info(), sent="2026-02-30T21:05:00-00:00"), "a date that does not exist"),
                (alert(info(), sent="2026-09-12T25:05:00-00:00"), "an impossible hour"),
                (alert(info(), sent="1969-09-12T21:05:00-00:00"), "a time before the epoch"),
                (alert(info(), status="Live"), "a status outside the pinned enumeration"),
                (alert(info(), msg_type="Revision"), "a msgType outside the pinned enumeration"),
                (alert(info(), scope="Internal"), "a scope outside the pinned enumeration"),
                (alert(info(categories=("Flood",))), "a category outside the pinned enumeration"),
                (alert(info(urgency="Soon")), "an urgency outside the pinned enumeration"),
                (alert(info(severity="Catastrophic")), "a severity outside the pinned enumeration"),
                (alert(info(certainty="Certain")), "a certainty outside the pinned enumeration"),
                (alert(info(categories=())), "an info block with no category at all"),
                (alert(info()).replace(b"<identifier>KSTO-2026-0912-001</identifier>", b""),
                 "an alert with no identifier"),
                (alert(info(), identifier="KSTO,2026"),
                 "an identifier containing the comma the pinned schema forbids"),
                (alert(info(), sender="a sender with spaces"),
                 "a sender containing the space the pinned schema forbids"),
                (alert(info(areas=AREA_ONE.replace("<altitude>120</altitude>", ""))),
                 "a ceiling without the altitude the pinned schema requires with it"),
                (alert(info(areas="""
    <area><areaDesc>Short</areaDesc>
      <polygon>38.47,-120.14 38.34,-119.95 38.47,-120.14</polygon></area>""")),
                 "a polygon with fewer than the four pairs the pinned schema requires"),
                (alert(info(areas="""
    <area><areaDesc>Open</areaDesc>
      <polygon>38.47,-120.14 38.34,-119.95 38.52,-119.74 38.99,-119.00</polygon></area>""")),
                 "a polygon that does not close"),
                (alert(info(areas="""
    <area><areaDesc>Bad</areaDesc><circle>138.47,-120.14 12.5</circle></area>""")),
                 "a latitude outside the range of real latitudes"),
                (alert(info(areas="""
    <area><areaDesc>Bad</areaDesc><circle>38.47,-120.14 -3</circle></area>""")),
                 "a negative circle radius"),
                (alert(info(areas="""
    <area><areaDesc>Bad</areaDesc><circle>38.47 12.5</circle></area>""")),
                 "a circle centre that is not a latitude,longitude pair"),
                (alert(info(areas="""
    <area><circle>38.47,-120.14 12.5</circle></area>""")),
                 "an area with no areaDesc"),
                (alert(info(areas="""
    <area><areaDesc>Bad</areaDesc>
      <geocode><valueName>SAME</valueName></geocode></area>""")),
                 "a geocode with a name and no value"),
                (b"""<?xml version="1.0"?><!DOCTYPE alert [<!ENTITY a "b">]>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"/>""",
                 "a document type declaration"),
                (b'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.1"><identifier>x</identifier>'
                 b"</alert>", "a different CAP version read as if it were 1.2"),
                (b"<alert><identifier>x</identifier></alert>", "an alert in no namespace"),
                (b"<alert xmlns='urn:oasis:names:tc:emergency:cap:1.2'>", "malformed XML"),
                (b"   ", "an empty file"),
                ("<alert/>", "a str rather than the saved bytes")):
            with self.assertRaises(ValueError, msg=why):
                convert(data)

    def test_every_emitted_column_is_declared_and_the_rows_reach_common_output(self):
        output, _ = convert(alert(info(categories=("Rescue", "Infra"),
                                       areas=AREA_ONE + AREA_TWO)))
        declared = PROFILE.read_text(encoding="utf-8").split("[units]")[1]
        for name in FIELDS:
            self.assertIn("\n%s = " % name, declared, name)
        self.assertNotIn('"', output)
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [reader, str(PROFILE), str(path), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["info_categories"], "Rescue+Infra")
            self.assertEqual(fields["info_severity"], "Moderate")
            self.assertEqual(fields["area_circle_radius_km"], 12.5)

    def test_a_row_that_declares_less_survives_the_reader_as_absent_not_as_zero(self):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        geocode_only = """
    <area><areaDesc>Only a code</areaDesc>
      <geocode><valueName>SAME</valueName><value>006113</value></geocode></area>"""
        for data, kind in ((alert(), "ALERT_WITHOUT_INFO"),
                           (alert(info(areas="")), "INFO_WITHOUT_AREA"),
                           (alert(info(areas=geocode_only)), "AREA")):
            output, _ = convert(data)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "observations.csv"
                path.write_text(output, encoding="utf-8")
                common = json.loads(subprocess.run(
                    [reader, str(PROFILE), str(path), "--allow-equal-time"],
                    capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 1, kind)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["cap_row_kind"], kind)
                self.assertIsNone(fields.get("area_circle_latitude_deg"), kind)
                self.assertIsNone(fields.get("area_circle_radius_km"), kind)
                self.assertTrue(fields["geometry_basis"], kind)

    def test_the_command_line_writes_the_same_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "alert.xml", base / "out"
            source.write_bytes(alert(info()))
            subprocess.run([sys.executable, "-m", "scripts.convert_cap_alert",
                            str(source), str(out)], cwd=ROOT, check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["rows"], 1)
            self.assertEqual(report["alert_identifier"], "KSTO-2026-0912-001")
            self.assertIn("PUBLISHES_NOTHING", report["published_alerts"])


if __name__ == "__main__":
    unittest.main()
