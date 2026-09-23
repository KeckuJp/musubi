"""Authored AIS reports, not receiver or vessel evidence."""
import csv
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from pyais.encode import encode_dict
from scripts.convert_nmea_wind import convert
from tests.test_nmea_wind_conversion import sentence

ROOT = Path(__file__).resolve().parents[1]


class AisRecordedTests(unittest.TestCase):
    def report(self, **values):
        fields = dict(msg_type=1, mmsi=123456789, lon=-70.25, lat=40.5,
                      speed=12.3, course=90., heading=80, second=59)
        fields.update(values)
        return encode_dict(fields, radio_channel="A")[0]

    def test_mixed_capture_reuses_meanings_without_duplicate_or_clock_fusion(self):
        for temperature, latitude in ((20, 40.5), (-2, -35.25)):
            lines = [sentence(f"YXMTW,{temperature},C"), self.report(lat=latitude),
                     sentence("WIMWV,90,R,10,M,A"), sentence("IIDPT,12.5,-1,50"),
                     sentence("GPXYZ,future"), sentence("WIMWV,0,T,2,M,V"),
                     sentence("YXMTW,,C")]
            source = "\r\n".join(lines) + "\r\n"
            output, report = convert(source, "marine-mixed")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["input_records"], report["selected_records"], report["unsupported_records"]), (7, 6, 1))
            self.assertEqual(report["unsupported"], [dict(line=5, raw=lines[4])])
            self.assertEqual([int(row["source_line"]) for row in rows], [1, 2, 3, 4, 6, 7])
            self.assertEqual({row["unlocated_time_us"] for row in rows}, {"0"})
            self.assertEqual(float(rows[0]["water_temperature_k"]), temperature + 273.15)
            self.assertEqual(float(rows[1]["ais_latitude_deg"]), latitude)
            self.assertEqual(float(rows[2]["wind_speed_m_s"]), 10)
            self.assertEqual(float(rows[3]["depth_below_transducer_m"]), 12.5)
            self.assertEqual(float(rows[3]["transducer_offset_m"]), -1)
            self.assertEqual(rows[4]["wind_speed_m_s"], "")
            self.assertEqual(rows[5]["water_temperature_k"], "")
            for row in rows:
                self.assertEqual(bytes.fromhex(row["source_sentence_hex"][4:]).decode(), lines[int(row["source_line"]) - 1])
            with tempfile.TemporaryDirectory() as directory:
                path, destination = Path(directory) / "in.txt", Path(directory) / "out"
                path.write_text(source)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_nmea_wind.py"),
                    str(path), str(destination), "--kind", "marine-mixed"], check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/nmea-wind/profile.toml"),
                        str(destination / "converted.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 6)
                    self.assertEqual(common["observations"][0]["fields"]["water_temperature_k"], temperature + 273.15)
                    self.assertTrue(all(row["clock_basis"] == "Unknown" for row in common["observations"]))
            with self.assertRaises(ValueError): convert(source[:-4], "marine-mixed")
        with self.assertRaises(ValueError): convert(sentence("GPXYZ,future"), "marine-mixed")
        _, report = convert(sentence("GPXYZ,future"), "marine-mixed", allow_no_selected=True)
        self.assertEqual((report["selected_records"], report["unsupported_records"]), (0, 1))

    def test_two_inputs_and_three_types_reuse_common_model(self):
        for latitude, longitude in ((40.5, -70.25), (-35.25, 150.5)):
            source = "\n".join([self.report(msg_type=kind, lat=latitude, lon=longitude)
                                 for kind in (1, 2, 3)] + [sentence("WIMWV,0,R,1,M,A")])
            output, report = convert(source, "ais-class-a")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["input_records"], report["ais_records"], report["unsupported_records"]), (4, 3, 1))
            self.assertEqual([int(row["ais_message_type"]) for row in rows], [1, 2, 3])
            self.assertEqual(float(rows[0]["ais_latitude_deg"]), latitude)
            self.assertEqual(float(rows[0]["ais_longitude_deg"]), longitude)
            self.assertAlmostEqual(float(rows[0]["ais_speed_m_s"]), 12.3 * 1852 / 3600)
            self.assertAlmostEqual(float(rows[0]["ais_course_rad"]), math.pi / 2)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/nmea-wind/profile.toml"), str(path),
                        "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 3)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    self.assertEqual(common["observations"][0]["fields"]["ais_latitude_deg"], latitude)

    def test_missing_and_censored_values_not_zero_or_exact(self):
        for speed, status in ((102.3, "UNAVAILABLE"), (102.2, "AT_LEAST")):
            output, _ = convert(self.report(lon=181, lat=91, speed=speed, course=360,
                                           heading=511, second=63), "ais-class-a")
            row = next(csv.DictReader(io.StringIO(output)))
            for name in ("ais_longitude_deg", "ais_latitude_deg", "ais_speed_m_s", "ais_course_rad", "ais_true_heading_rad"):
                self.assertEqual(row[name], "")
            self.assertEqual(row["ais_speed_disposition"], status)
            self.assertEqual(row["ais_second_code"], "63")
            if speed == 102.2:
                self.assertAlmostEqual(float(row["ais_speed_lower_bound_m_s"]), 102.2 * 1852 / 3600)

    def static(self, channel="A", **values):
        fields = dict(msg_type=5, mmsi=123456789, imo=9074729, callsign="3FOF8",
                      shipname="AUTHORED ONE", ship_type=70, to_bow=225, to_stern=70, to_port=1,
                      to_starboard=31, draught=12.2, destination="AUTHORED PORT", month=5, day=15,
                      hour=14, minute=30, dte=0, epfd=1, ais_version=2)
        fields.update(values)
        return encode_dict(fields, radio_channel=channel)

    @staticmethod
    def reframe(line, **replacements):
        """Rebuild a sentence with its checksum, so a negative case fails on its own condition."""
        body = line[1:].split("*")[0].split(",")
        for position, value in replacements.items():
            body[int(position[1:])] = value
        payload = ",".join(body)
        checksum = 0
        for value in payload.encode("ascii"):
            checksum ^= value
        return f"!{payload}*{checksum:02X}"

    def test_saved_type5_pair_reaches_common_with_declared_units_and_sentinels(self):
        first = self.static()
        second = self.static(channel="B", mmsi=987654321, imo=0, callsign="", shipname="",
                             ship_type=0, to_bow=0, to_stern=0, to_port=0, to_starboard=0,
                             draught=25.5, destination="", month=0, day=0, hour=24, minute=60,
                             dte=1, epfd=9, ais_version=0)
        second = [self.reframe(line, f3="7") for line in second]
        source = "\r\n".join([*first, *second, sentence("WIMWV,0,R,1,M,A")]) + "\r\n"
        output, report = convert(source, "ais-class-a", static_pairs=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual((report["ais_records"], report["ais_static_pairs"]), (2, 2))
        self.assertEqual(report["consumed_lines"], [1, 2, 3, 4])
        self.assertEqual((report["unsupported_records"], report["reused_sequential_id_pairs"]), (1, 0))
        self.assertEqual([int(row["source_line"]) for row in rows], [1, 3])
        self.assertEqual([row["ais_message_type"] for row in rows], ["5", "5"])
        for row in rows:
            for name in ("ais_longitude_deg", "ais_latitude_deg", "ais_speed_m_s",
                         "ais_course_rad", "ais_true_heading_rad", "ais_second_code"):
                self.assertEqual(row[name], "")
            self.assertEqual(row["ais_position_disposition"], "STATIC_REPORT_CARRIES_NO_POSITION")
        self.assertEqual(rows[0]["ais_mmsi_reported"], "123456789")
        self.assertEqual((rows[0]["ais_imo_reported"], rows[0]["ais_imo_disposition"]),
                         ("9074729", "VALID_IMO_NUMBER"))
        self.assertEqual(rows[0]["ais_ship_name_reported"], "AUTHORED ONE")
        self.assertEqual(rows[0]["ais_call_sign_reported"], "3FOF8")
        self.assertEqual(rows[0]["ais_destination_reported"], "AUTHORED PORT")
        self.assertEqual(rows[0]["ais_dte_reported"], "AVAILABLE")
        self.assertEqual(rows[0]["ais_version_code"], "2")
        self.assertEqual([rows[0][name] for name in ("ais_dimension_bow_m", "ais_dimension_stern_m",
                          "ais_dimension_port_m", "ais_dimension_starboard_m")],
                         ["225", "70", "1", "31"])
        self.assertEqual(rows[0]["ais_dimension_disposition"], "REPORTED_REFERENCE_POINT_DIMENSIONS")
        self.assertAlmostEqual(float(rows[0]["ais_draught_m"]), 12.2)
        self.assertEqual((rows[0]["ais_draught_lower_bound_m"], rows[0]["ais_draught_disposition"]),
                         ("", "REPORTED"))
        self.assertEqual([rows[0][name] for name in ("ais_eta_month_code", "ais_eta_day_code",
                          "ais_eta_hour_code", "ais_eta_minute_code")], ["5", "15", "14", "30"])
        self.assertEqual(rows[0]["ais_eta_disposition"], "REPORTED_COMPLETE_FIELDS")
        self.assertEqual(rows[0]["unlocated_time_us"], "0")  # the ETA never becomes the row time
        self.assertEqual(rows[0]["ais_fragment_binding"],
                         "DECLARED_ADJACENT_PAIR_LINES_1_2_SEQ_0_CH_A_NOT_VERIFIED_SAME_TRANSMISSION")
        censored = rows[1]
        self.assertEqual((censored["ais_imo_reported"], censored["ais_imo_disposition"]),
                         ("0", "NOT_AVAILABLE"))
        for name in ("ais_ship_name", "ais_call_sign", "ais_destination"):
            self.assertEqual(censored[name + "_reported"], "")
            self.assertEqual(censored[name + "_disposition"], "NOT_AVAILABLE_OR_BLANK")
        self.assertEqual(censored["ais_dimension_disposition"], "DEFAULT_ALL_ZERO_NOT_AVAILABLE")
        self.assertEqual(censored["ais_draught_m"], "")
        self.assertAlmostEqual(float(censored["ais_draught_lower_bound_m"]), 25.5)
        self.assertEqual(censored["ais_draught_disposition"], "AT_LEAST")
        self.assertEqual([censored[name] for name in ("ais_eta_month_code", "ais_eta_day_code",
                          "ais_eta_hour_code", "ais_eta_minute_code")], ["0", "0", "24", "60"])
        self.assertEqual(censored["ais_eta_disposition"], "UNAVAILABLE")
        self.assertEqual(censored["ais_dte_reported"], "NOT_AVAILABLE")
        self.assertEqual(censored["ais_fragment_binding"],
                         "DECLARED_ADJACENT_PAIR_LINES_3_4_SEQ_7_CH_B_NOT_VERIFIED_SAME_TRANSMISSION")
        self.assertEqual((censored["ais_ship_type_code"], censored["ais_ship_type_disposition"]),
                         ("0", "NOT_AVAILABLE_OR_NO_SHIP"))
        self.assertEqual((censored["ais_epfd_code"], censored["ais_epfd_disposition"]),
                         ("0", "UNDEFINED_DEFAULT"))
        self.assertEqual(bytes.fromhex(rows[0]["source_sentence_hex"][4:]).decode(),
                         first[0] + "\r\n" + first[1])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "static.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/nmea-wind/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields["ais_imo_reported"], 9074729)
            self.assertEqual(fields["ais_dimension_bow_m"], 225)
            self.assertAlmostEqual(fields["ais_draught_m"], 12.2)
            self.assertEqual(fields["ais_eta_month_code"], 5)
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            declared = common["profile_units"]
            for column in ("ais_imo_reported", "ais_ship_name_reported", "ais_call_sign_reported",
                           "ais_ship_type_code", "ais_ship_type_disposition", "ais_epfd_code",
                           "ais_dimension_bow_m", "ais_dimension_disposition", "ais_draught_m",
                           "ais_draught_lower_bound_m", "ais_draught_disposition",
                           "ais_eta_month_code", "ais_eta_disposition", "ais_destination_reported",
                           "ais_dte_reported", "ais_version_code", "ais_fragment_binding"):
                self.assertTrue(declared.get(column), f"{column} must declare its meaning")
            self.assertIn("never_converted_to_an_instant", declared["ais_eta_disposition"])
            self.assertIn("never_an_actual_arrival", declared["ais_eta_disposition"])
            self.assertIn("never_authenticated_identity", declared["ais_imo_reported"])
            self.assertIn("not_a_globally_unique_message_identifier", declared["ais_fragment_binding"])
            self.assertIn("25_5_m_or_greater", declared["ais_draught_lower_bound_m"])
            self.assertIn("REPORTED_CODE_IN_BASE_RANGE", declared["ais_ship_type_disposition"])
            self.assertIn("not_an_entry_by_entry_dictionary", declared["ais_ship_type_disposition"])
            self.assertNotIn("UNRESOLVED", declared["ais_ship_type_disposition"])
            self.assertIn("NOT_USED_BY_THE_REFERENCE", declared["ais_epfd_disposition"])
            self.assertNotIn("UNRESOLVED", declared["ais_epfd_disposition"])
            for column in ("ais_latitude_deg", "ais_longitude_deg", "ais_speed_m_s",
                           "ais_speed_lower_bound_m_s", "ais_speed_disposition", "ais_course_rad",
                           "ais_true_heading_rad", "ais_second_code", "ais_nav_status_code",
                           "ais_mmsi_reported", "ais_message_type", "ais_sentence_role",
                           "ais_position_disposition"):
                self.assertTrue(declared.get(column), f"{column} must declare its meaning")

    def test_saved_type5_fragment_binding_refuses_what_the_decoder_would_accept(self):
        first, second = self.static(), self.static(channel="B", mmsi=987654321,
                                                   shipname="AUTHORED TWO")
        second = [self.reframe(line, f3="7") for line in second]
        position = self.report()
        cases = {
            "cross-message pair": [first[0], second[1]],
            "reversed pair": [first[1], first[0]],
            "missing second fragment": [first[0]],
            "duplicated first fragment": [first[0], first[0]],
            "interleaved pairs": [first[0], second[0], first[1], second[1]],
            "position sentence between fragments": [first[0], position, first[1]],
            "mismatched channel": [first[0], self.reframe(first[1], f4="B")],
            "mismatched sequential id": [first[0], self.reframe(first[1], f3="4")],
            "mismatched talker": [first[0], "!AIVDM" + first[1][6:]],
            "mismatched fragment count": [self.reframe(first[0], f1="3"), first[1]],
        }
        for label, lines in cases.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                convert("\r\n".join(lines) + "\r\n", "ais-class-a", static_pairs=True)
        for line in (self.reframe(first[0], f5=first[0][1:].split("*")[0].split(",")[5][:-1]),
                     first[0][:-1] + "0"):
            with self.assertRaises(ValueError):
                convert("\r\n".join([line, first[1]]) + "\r\n", "ais-class-a", static_pairs=True)
        other = [self.reframe(first[0], f5="8" + first[0][1:].split("*")[0].split(",")[5][1:]),
                 first[1]]
        _, report = convert("\r\n".join(other) + "\r\n", "ais-class-a", allow_no_selected=True, static_pairs=True)
        self.assertEqual((report["ais_records"], report["unsupported_records"]), (0, 2))
        _, report = convert("\r\n".join([first[1], position]) + "\r\n", "ais-class-a",
                            static_pairs=True)
        self.assertEqual((report["ais_records"], report["unsupported_records"]), (1, 1))
        self.assertEqual(report["unsupported"], [dict(line=1, raw=first[1])])
        again = self.static(mmsi=111222333, shipname="AUTHORED THREE")
        _, report = convert("\r\n".join([*first, *again]) + "\r\n", "ais-class-a",
                            static_pairs=True)
        self.assertEqual((report["ais_static_pairs"], report["reused_sequential_id_pairs"]), (2, 1))

    def test_saved_type5_pairs_in_a_mixed_capture_are_accounted_once(self):
        pair = self.static()
        lines = [sentence("YXMTW,20,C"), *pair, self.report(), sentence("WIMWV,90,R,10,M,A"),
                 sentence("GPXYZ,future")]
        source = "\r\n".join(lines) + "\r\n"
        output, report = convert(source, "marine-mixed", static_pairs=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 4)  # one row for the pair, not two and not zero
        self.assertEqual((report["input_records"], report["selected_records"],
                          report["consumed_records"], report["unsupported_records"]), (6, 4, 5, 1))
        self.assertEqual(report["unsupported"], [dict(line=6, raw=lines[5])])
        static_row = next(row for row in rows if row["ais_message_type"] == "5")
        self.assertEqual((static_row["source_line"], static_row["selected_kind"]),
                         ("2", "ais-class-a"))
        self.assertEqual(static_row["ais_ship_name_reported"], "AUTHORED ONE")
        position_row = next(row for row in rows if row["ais_message_type"] == "1")
        self.assertEqual(float(position_row["ais_latitude_deg"]), 40.5)
        self.assertEqual(static_row["ais_draught_disposition"], "REPORTED")
        batch = []
        for ordinal in range(500):
            batch += self.static(mmsi=200000000 + ordinal, shipname=f"BATCH {ordinal}")
            batch.append(sentence("WIMWV,10,R,2,M,A"))
        output, report = convert("\r\n".join(batch) + "\r\n", "ais-class-a",
                                 allow_no_selected=True, static_pairs=True)
        self.assertEqual((report["ais_static_pairs"], report["unsupported_records"]), (500, 500))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 500)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "batch.csv"
                path.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/nmea-wind/profile.toml"),
                    str(path), "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 500)
            self.assertEqual(len({obs["fields"]["ais_mmsi_reported"]
                                  for obs in common["observations"]}), 500)

    @staticmethod
    def armour(bits):
        """6-bit ASCII armouring: the inverse of the payload decoding the reference defines."""
        out = ""
        for index in range(0, len(bits), 6):
            value = int(bits[index:index + 6].ljust(6, "0"), 2)
            out += chr(value + 48) if value < 40 else chr(value + 56)
        return out

    @staticmethod
    def dearmour(payload, fill):
        bits = "".join(format(ord(c) - 48 if ord(c) < 88 else ord(c) - 56, "06b") for c in payload)
        return bits[:len(bits) - fill]

    def raw_pair(self, ship_type=None, epfd=None):
        """A pair carrying exact raw codes. pyais encode_dict coerces a reserved code through its
        own enum, so the bits are authored directly at the TABLE 52 offsets instead."""
        pair = self.static()
        bits = (self.dearmour(pair[0][1:].split("*")[0].split(",")[5], 0)
                + self.dearmour(pair[1][1:].split("*")[0].split(",")[5], 2))
        self.assertEqual(len(bits), 424)
        if ship_type is not None:
            bits = bits[:232] + format(ship_type, "08b") + bits[240:]
        if epfd is not None:
            bits = bits[:270] + format(epfd, "04b") + bits[274:]
        lines = []
        for number, payload, fill in ((1, self.armour(bits[:360]), 0),
                                      (2, self.armour(bits[360:] + "00"), 2)):
            lines.append(self.reframe(f"!AIVDO,2,{number},0,A,{payload},{fill}*00"))
        return lines

    def test_raw_ship_type_and_fixing_device_codes_are_preserved_and_classified(self):
        from pyais.messages import NMEASentenceFactory, AISSentence
        for raw_type, raw_epfd, type_state, epfd_state in (
                (0, 0, "NOT_AVAILABLE_OR_NO_SHIP", "UNDEFINED_DEFAULT"),
                (1, 1, "REPORTED_CODE_IN_BASE_RANGE", "REPORTED_DEFINED_CODE"),
                (70, 8, "REPORTED_CODE_IN_BASE_RANGE", "REPORTED_DEFINED_CODE"),
                (99, 15, "REPORTED_CODE_IN_BASE_RANGE", "REPORTED_DEFINED_CODE"),
                (100, 9, "RESERVED_FOR_REGIONAL_USE", "NOT_USED_BY_THE_REFERENCE"),
                (150, 12, "RESERVED_FOR_REGIONAL_USE", "NOT_USED_BY_THE_REFERENCE"),
                (199, 14, "RESERVED_FOR_REGIONAL_USE", "NOT_USED_BY_THE_REFERENCE"),
                (200, 9, "RESERVED_FOR_FUTURE_USE", "NOT_USED_BY_THE_REFERENCE"),
                (255, 13, "RESERVED_FOR_FUTURE_USE", "NOT_USED_BY_THE_REFERENCE")):
            lines = self.raw_pair(raw_type, raw_epfd)
            with self.subTest(ship_type=raw_type, epfd=raw_epfd):
                output, _ = convert("\r\n".join(lines) + "\r\n", "ais-class-a", static_pairs=True)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(row["ais_ship_type_code"], str(raw_type))
                self.assertEqual(row["ais_ship_type_disposition"], type_state)
                self.assertEqual(row["ais_epfd_code"], str(raw_epfd))
                self.assertEqual(row["ais_epfd_disposition"], epfd_state)
                parts = [NMEASentenceFactory.produce(line.encode("ascii")) for line in lines]
                message = AISSentence.assemble_from_iterable(parts).decode()
                if raw_type > 99 or raw_type == 1:
                    self.assertEqual(int(message.ship_type), 0)
                if 9 <= raw_epfd <= 14:
                    self.assertEqual(int(message.epfd), 0)
                self.assertEqual(row["ais_mmsi_reported"], "123456789")
                self.assertAlmostEqual(float(row["ais_draught_m"]), 12.2)

    def test_type5_pairs_need_an_explicit_selection_and_an_exact_424_bit_shape(self):
        pair = self.static()
        source = "\r\n".join(pair) + "\r\n"
        for kind in ("ais-class-a", "marine-mixed"):
            with self.subTest(kind=kind):
                _, report = convert(source, kind, allow_no_selected=True)
                self.assertEqual(report["unsupported_records"], 2)
                self.assertEqual([item["line"] for item in report["unsupported"]], [1, 2])
                self.assertNotIn("ais_static_pairs", report if kind == "marine-mixed"
                                 else {k: v for k, v in report.items() if k != "ais_static_pairs"})
        with self.assertRaises(ValueError) as caught:
            convert(source, "ais-class-a")
        self.assertEqual(str(caught.exception), "no selected NMEA records")
        output, _ = convert(source, "ais-class-a", static_pairs=True)
        row = next(csv.DictReader(io.StringIO(output)))
        self.assertEqual(row["ais_fragment_binding"],
                         "DECLARED_ADJACENT_PAIR_LINES_1_2_SEQ_0_CH_A_NOT_VERIFIED_SAME_TRANSMISSION")
        singles, report = convert("\r\n".join([self.report(), sentence("WIMWV,0,R,1,M,A")]) + "\r\n",
                                  "ais-class-a")
        self.assertEqual((report["ais_records"], report["unsupported_records"]), (1, 1))
        self.assertEqual(float(next(csv.DictReader(io.StringIO(singles)))["ais_latitude_deg"]), 40.5)
        payload = pair[1][1:].split("*")[0].split(",")[5]
        for label, lines in (
                ("overlong second payload", [pair[0], self.reframe(pair[1], f5=payload + "0")]),
                ("wrong fill count", [pair[0], self.reframe(pair[1], f6="0")]),
                ("short second payload", [pair[0], self.reframe(pair[1], f5=payload[:-1])]),
                ("overlong first payload",
                 [self.reframe(pair[0], f5=pair[0][1:].split("*")[0].split(",")[5] + "0"), pair[1]]),
                ("non-digit sequential id",
                 [self.reframe(pair[0], f3="X"), self.reframe(pair[1], f3="X")]),
                ("missing fill field", ["!AIVDO,2,1,0,A," + pair[0][1:].split("*")[0].split(",")[5],
                                        pair[1]])):
            with self.subTest(case=label), self.assertRaises(ValueError):
                convert("\r\n".join(lines) + "\r\n", "ais-class-a", static_pairs=True)
        wrong = self.raw_pair()
        flipped = self.dearmour(wrong[0][1:].split("*")[0].split(",")[5], 0)
        flipped = format(1, "06b") + flipped[6:]
        wrong[0] = self.reframe(f"!AIVDO,2,1,0,A,{self.armour(flipped)},0*00")
        with self.assertRaises(ValueError):
            convert("\r\n".join(wrong) + "\r\n", "ais-class-a", static_pairs=True)

    def test_default_selectors_still_convert_a_capture_that_contains_multipart_traffic(self):
        """Normal input compatibility: multipart traffic must not reject a usable capture."""
        pair = self.static()
        first_payload = pair[0][1:].split("*")[0].split(",")[5]
        other = [self.reframe(pair[0], f3="4", f5="8" + first_payload[1:]),
                 self.reframe(pair[1], f3="4", f5="000000" + pair[1][1:].split("*")[0].split(",")[5][6:])]
        def payload_of(line):
            return line[1:].split("*")[0].split(",")[5]
        self.assertEqual((payload_of(pair[0])[:1], payload_of(other[0])[:1]), ("5", "8"))
        self.assertEqual(payload_of(pair[1])[:1], "3")
        lines = [sentence("YXMTW,20,C"), pair[0], pair[1], self.report(), other[0], other[1],
                 sentence("WIMWV,90,R,10,M,A")]
        source = "\r\n".join(lines) + "\r\n"
        output, report = convert(source, "ais-class-a")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["ais_latitude_deg"]), 40.5)
        self.assertEqual((report["input_records"], report["ais_records"],
                          report["unsupported_records"]), (7, 1, 6))
        self.assertEqual([item["line"] for item in report["unsupported"]], [1, 2, 3, 5, 6, 7])
        output, report = convert(source, "marine-mixed")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([int(row["source_line"]) for row in rows], [1, 4, 7])
        self.assertEqual((report["input_records"], report["selected_records"],
                          report["consumed_records"], report["unsupported_records"]), (7, 3, 3, 4))
        self.assertEqual([item["line"] for item in report["unsupported"]], [2, 3, 5, 6])
        self.assertEqual(float(rows[0]["water_temperature_k"]), 293.15)
        self.assertEqual(float(rows[2]["wind_speed_m_s"]), 10)
        output, report = convert(source, "marine-mixed", static_pairs=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([int(row["source_line"]) for row in rows], [1, 2, 4, 7])
        self.assertEqual((report["selected_records"], report["consumed_records"],
                          report["unsupported_records"]), (4, 5, 2))
        self.assertEqual([item["line"] for item in report["unsupported"]], [5, 6])
        malformed = self.reframe(self.report(), f1="2")
        with self.assertRaises(ValueError) as caught:
            convert(malformed + "\r\n", "ais-class-a")
        self.assertEqual(str(caught.exception),
                         "fixed AIS Class A report requires one complete 168-bit sentence")

    def test_corruption_truncation_and_reserved_values_rejected(self):
        valid = self.report()
        payload = valid[1:].split("*")[0].split(",")
        short = payload.copy(); short[5] = short[5][:-1]
        fragmented = payload.copy(); fragmented[1] = "2"
        for value in (valid[:-1], valid[:-2] + "00", sentence(",".join(short), "!"),
                      sentence(",".join(fragmented), "!"), self.report(course=360.1),
                      self.report(heading=360), self.report(lat=92), self.report(lon=182)):
            with self.assertRaises(ValueError): convert(value, "ais-class-a")
