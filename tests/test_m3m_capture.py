"""The pinned exporter's own output for an authored packet -- not a hand-shaped fixture.

Every key name, type and value in `OBSERVED` below was **produced by ExifTool 13.59** from the
authored XMP packet and the minimal TIFF built in this module, and `test_the_pinned_exporter_still
_emits_this_shape` re-derives it whenever `MUSUBI_EXIFTOOL` names that exporter, so the fixture
cannot drift away from the decoder it claims to represent.

No image was opened, no aircraft flew and no real record appears here: the TIFF is 112 authored
bytes of container and the packet carries the guide's own value examples.
"""
import csv
import io
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_m3m_capture import QUALIFIED_METADATA_VERSION, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/m3m-capture/profile.toml"
CAPTURE_US = 1_700_000_000_000_000
BAND_INDEX = {"Green": 1, "Red": 2, "RedEdge": 3, "NIR": 4}
BAND_FREQ = {"Green": "560 (+/-16) nm", "Red": "650 (+/-16) nm",
             "RedEdge": "730 (+/-16) nm", "NIR": "860 (+/-26) nm"}

PACKET_FIELDS = [
    ("Version", "1.6"), ("ImageSource", "MS_NIR_CAMERA"), ("GpsStatus", "RTK"),
    ("AltitudeType", "RtkAlt"), ("GpsLatitude", "22.000000\u00b0N"),
    ("GpsLongitude", "113.000000\u00b0E"), ("AbsoluteAltitude", "+50.000"),
    ("RelativeAltitude", "+30.000"), ("GimbalRollDegree", "+0.00"),
    ("GimbalYawDegree", "+1.20"), ("GimbalPitchDegree", "-90.00"),
    ("FlightRollDegree", "+0.10"), ("FlightYawDegree", "+2.00"),
    ("FlightPitchDegree", "-1.00"), ("FlightXSpeed", "+3.00"), ("FlightYSpeed", "-1.50"),
    ("FlightZSpeed", "+0.10"), ("RtkFlag", "50"), ("RtkStdLon", "0.01224"),
    ("RtkStdLat", "0.01624"), ("RtkStdHgt", "0.03406"), ("RtkDiffAge", "1.60000"),
    ("SurveyingMode", "1"), ("DewarpFlag", "0"), ("VignettingFlag", "0"),
    ("VignettingData", "-0.000070832,1.829488e-06,-5.307911e-09,8.820567e-12,"
                       "-6.663875e-15,1.885447e-18"),
    ("LS_type", "1"), ("LS_status", "2"), ("Package_idx", "165"), ("Cfg_cnt", "1"),
    ("RawData", "11682.000,10389.000,12836.000,9945.000"), ("BandName", "NIR"),
    ("BandFreq", "860 (+/-26) nm"), ("Irradiance", "2000.000"), ("SensorGain", "1.044"),
    ("ExposureTime", "1000"), ("SensorGainAdjustment", "1.002"), ("Sensorindex", "4"),
    ("BlackLevel", "3200"), ("DroneID", "AUTHORED_SERIAL_NOT_A_REAL_DEVICE"),
]

OBSERVED = {
    "ExifTool:ExifToolVersion": 13.59,
    "EXIF:ImageWidth": 1,
    "EXIF:ImageHeight": 1,
    "EXIF:BitsPerSample": 16,
    "EXIF:Compression": 1,
    "EXIF:PhotometricInterpretation": 1,
    "EXIF:SamplesPerPixel": 1,
    "EXIF:StripByteCounts": 2,
    "EXIF:GPSVersionID": "2 3 0 0",
    "EXIF:GPSLatitudeRef": "N",
    "EXIF:GPSLatitude": 22,
    "EXIF:GPSLongitudeRef": "E",
    "EXIF:GPSLongitude": 113,
    "EXIF:GPSTimeStamp": "02:47:57",
    "EXIF:GPSDateStamp": "2023:03:09",
    "XMP:XMPToolkit": "Image::ExifTool 13.59",
    "XMP:AbsoluteAltitude": "+50.000",
    "XMP:AltitudeType": "RtkAlt",
    "XMP:BandFreq": "860 (+/-26) nm",
    "XMP:BandName": "NIR",
    "XMP:BlackLevel": 3200,
    "XMP:Cfg_cnt": 1,
    "XMP:DewarpFlag": 0,
    "XMP:DroneID": "AUTHORED_SERIAL_NOT_A_REAL_DEVICE",
    "XMP:ExposureTime": 1000,
    "XMP:FlightPitchDegree": -1.0,
    "XMP:FlightRollDegree": "+0.10",
    "XMP:FlightXSpeed": "+3.00",
    "XMP:FlightYSpeed": -1.5,
    "XMP:FlightYawDegree": "+2.00",
    "XMP:FlightZSpeed": "+0.10",
    "XMP:GimbalPitchDegree": -90.0,
    "XMP:GimbalRollDegree": "+0.00",
    "XMP:GimbalYawDegree": "+1.20",
    "XMP:GPSLatitude": 22,
    "XMP:GPSLongitude": 113,
    "XMP:GpsStatus": "RTK",
    "XMP:ImageSource": "MS_NIR_CAMERA",
    "XMP:Irradiance": 2000.0,
    "XMP:LS_status": 2,
    "XMP:LS_type": 1,
    "XMP:Package_idx": 165,
    "XMP:RawData": "11682.000,10389.000,12836.000,9945.000",
    "XMP:RelativeAltitude": "+30.000",
    "XMP:RtkDiffAge": 1.6,
    "XMP:RtkFlag": 50,
    "XMP:RtkStdHgt": 0.03406,
    "XMP:RtkStdLat": 0.01624,
    "XMP:RtkStdLon": 0.01224,
    "XMP:SensorGain": 1.044,
    "XMP:SensorGainAdjustment": 1.002,
    "XMP:Sensorindex": 4,
    "XMP:SurveyingMode": 1,
    "XMP:Version": 1.6,
    "XMP:VignettingData": "-0.000070832,1.829488e-06,-5.307911e-09,8.820567e-12,"
                          "-6.663875e-15,1.885447e-18",
    "XMP:VignettingFlag": 0,
    "Composite:ImageSize": "1 1",
    "Composite:Megapixels": 1e-06,
    "Composite:ShutterSpeed": 1000,
    "Composite:GPSDateTime": "2023:03:09 02:47:57Z",
    "Composite:GPSLatitude": 22,
    "Composite:GPSLongitude": 113,
    "Composite:GPSPosition": "22 113",
}


def authored_tiff():
    """A minimal 1x1 16-bit TIFF: a container for the packet, not a photograph."""
    def entry(tag, typ, count, value):
        return struct.pack("<HHI4s", tag, typ, count, value)
    data = b"\x00\x08"
    ifd = [entry(256, 3, 1, struct.pack("<HH", 1, 0)),
           entry(257, 3, 1, struct.pack("<HH", 1, 0)),
           entry(258, 3, 1, struct.pack("<HH", 16, 0)),
           entry(259, 3, 1, struct.pack("<HH", 1, 0)),
           entry(262, 3, 1, struct.pack("<HH", 1, 0)),
           entry(273, 4, 1, struct.pack("<I", 8 + 2 + 12 * 8 + 4)),
           entry(277, 3, 1, struct.pack("<HH", 1, 0)),
           entry(279, 4, 1, struct.pack("<I", len(data)))]
    return (struct.pack("<2sHI", b"II", 42, 8) + struct.pack("<H", len(ifd))
            + b"".join(ifd) + struct.pack("<I", 0) + data)


def authored_packet(fields=PACKET_FIELDS):
    attributes = "\n   ".join('drone-dji:%s="%s"' % item for item in fields)
    return ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '<rdf:Description rdf:about="" '
            'xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"\n   '
            + attributes + '/></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def image(band="NIR", **overrides):
    """The observed export record, optionally with a documented field changed."""
    record = dict(OBSERVED)
    record["SourceFile"] = "/authored/m3m/DJI_0001_MS_" + band + ".TIF"
    if band != "NIR":
        record.update({"XMP:BandName": band, "XMP:BandFreq": BAND_FREQ[band],
                       "XMP:Sensorindex": BAND_INDEX[band],
                       "XMP:ImageSource": "MS_" + band.upper() + "_CAMERA"})
    for key, value in overrides.items():
        if value is None:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def rows_of(*records, capture_time_us=CAPTURE_US):
    output, report = convert(json.dumps(list(records)).encode(), capture_time_us)
    return list(csv.DictReader(io.StringIO(output))), report, output


class M3mCaptureTests(unittest.TestCase):
    def only(self, *records, **kwargs):
        rows, report, output = rows_of(*records, **kwargs)
        self.assertEqual(len(rows), 1, output)
        return rows[0], report

    def test_each_documented_band_carries_its_identity_centre_and_half_width(self):
        rows, report, _ = rows_of(*(image(band) for band in BAND_FREQ))
        self.assertEqual([row["band_name_reported"] for row in rows], list(BAND_FREQ))
        self.assertEqual(report["bands_seen"], list(BAND_FREQ))
        self.assertEqual([row["band_central_wavelength_nm"] for row in rows],
                         ["560", "650", "730", "860"])
        self.assertEqual([row["band_half_width_nm"] for row in rows], ["16", "16", "16", "26"])
        for row in rows:
            self.assertEqual(row["band_name_status"], "LISTED_BY_THE_PINNED_GUIDE")
            self.assertEqual(row["band_identity_agreement"], "THE_NAME_AND_THE_INDEX_AGREE")
            self.assertIn("HWHM", row["band_width_basis"].upper())
        with self.assertRaises(ValueError):
            rows_of(image("NIR", **{"XMP:Sensorindex": 1}))
        unlisted, _ = self.only(image("NIR", **{"XMP:BandName": "Panchromatic",
                                                "XMP:BandFreq": None,
                                                "XMP:Sensorindex": None}))
        self.assertEqual(unlisted["band_name_reported"], "Panchromatic")
        self.assertTrue(unlisted["band_name_status"].startswith("UNLISTED"))
        self.assertEqual(unlisted["band_identity_agreement"],
                         "NO_SENSOR_INDEX_WAS_WRITTEN_SO_ONLY_THE_NAME_IS_KNOWN")
        self.assertEqual((unlisted["band_central_wavelength_nm"],
                          unlisted["band_half_width_nm"]), ("", ""))
        stray, _ = self.only(image("NIR", **{"XMP:Sensorindex": 9}))
        self.assertEqual(stray["band_identity_agreement"],
                         "THE_SENSOR_INDEX_IS_NOT_ONE_THE_GUIDE_LISTS")

    def test_the_capture_fix_keeps_its_hemisphere_altitudes_and_rtk_meaning(self):
        row, _ = self.only(image())
        self.assertEqual((row["capture_latitude_deg"], row["capture_longitude_deg"]),
                         ("22", "113"))
        self.assertEqual(row["capture_coordinate_basis"],
                         "WRITTEN_AS_A_SIGNED_NUMBER_WITHOUT_A_HEMISPHERE_LETTER")
        self.assertEqual(row["capture_coordinate_agreement"],
                         "THE_EXIF_GPS_PAIR_AGREES_WITH_THE_XMP_PAIR")
        south, _ = self.only(image(**{"XMP:GPSLatitude": "22.000000\u00b0S",
                                      "XMP:GPSLongitude": "113.000000\u00b0W",
                                      "EXIF:GPSLatitude": None, "EXIF:GPSLatitudeRef": None,
                                      "EXIF:GPSLongitude": None, "EXIF:GPSLongitudeRef": None,
                                      "Composite:GPSLatitude": None,
                                      "Composite:GPSLongitude": None}))
        self.assertEqual((south["capture_latitude_deg"], south["capture_longitude_deg"]),
                         ("-22.000000", "-113.000000"))
        self.assertEqual((row["absolute_altitude_m"], row["relative_altitude_m"]),
                         ("50.000", "30.000"))
        self.assertEqual(row["altitude_type_reported"], "RtkAlt")
        self.assertIn("names_NO_datum", row["altitude_basis"] + PROFILE.read_text())
        self.assertEqual((row["rtk_flag_code"], row["rtk_positioning_quality"]),
                         ("50", "FIXED_SOLUTION_POSITIONING_CENTIMETER_LEVEL_ACCURACY"))
        self.assertEqual(row["surveying_mode_reported"],
                         "RECOMMENDED_THE_ACCURACY_CAN_BE_GUARANTEED")
        for code, meaning in ((0, "FAILED_TO_POSITION"),
                              (16, "SINGLE_POINT_POSITIONING_METER_LEVEL_ACCURACY"),
                              (40, "FLOATING_POINT_SOLUTION_DECIMETER_TO_METER_LEVEL_ACCURACY")):
            other, _ = self.only(image(**{"XMP:RtkFlag": code}))
            self.assertEqual(other["rtk_positioning_quality"], meaning)
        unlisted, _ = self.only(image(**{"XMP:RtkFlag": 51}))
        self.assertTrue(unlisted["rtk_positioning_quality"].startswith("UNLISTED"))

    def test_the_two_clocks_stay_apart(self):
        row, _ = self.only(image())
        self.assertEqual(row["record_time_us"], str(CAPTURE_US))
        self.assertEqual((row["gps_date_stamp_utc"], row["gps_time_of_day_utc"]),
                         ("2023:03:09", "02:47:57"))
        self.assertIn("not_the_callers_capture_clock_in_record_time_us",
                      row["capture_clock_basis"])
        without, _ = self.only(image(**{"EXIF:GPSDateStamp": None, "EXIF:GPSTimeStamp": None}))
        self.assertEqual((without["gps_date_stamp_utc"], without["gps_time_of_day_utc"]), ("", ""))
        self.assertEqual(without["record_time_us"], str(CAPTURE_US))

    def test_the_calibration_inputs_are_carried_and_never_applied(self):
        row, report = self.only(image())
        self.assertEqual(row["irradiance_reported"], "2000.0")
        self.assertEqual([row["sun_sensor_raw_green"], row["sun_sensor_raw_red"],
                          row["sun_sensor_raw_red_edge"], row["sun_sensor_raw_nir"]],
                         ["11682.000", "10389.000", "12836.000", "9945.000"])
        self.assertEqual(row["sun_sensor_status"], "VALID_AND_COMPENSATING_STATE")
        self.assertEqual((row["sensor_gain"], row["sensor_gain_adjustment"]), ("1.044", "1.002"))
        self.assertEqual(row["exposure_time_us"], "1000")
        self.assertEqual((row["black_level_code"], row["bits_per_sample"]), ("3200", "16"))
        self.assertEqual(row["vignetting_state"], "COMPENSATION_DISABLED")
        self.assertEqual(row["dewarp_state"], "NOT_DEWARPED")
        self.assertTrue(row["vignetting_coefficients_hex"].startswith("hex:"))
        for forbidden in ("reflectance", "radiance", "ndvi", "index_value"):
            self.assertNotIn(forbidden, [name.lower() for name in row
                                         if name not in ("reflectance_basis",)])
        self.assertIn("no_reflectance", row["reflectance_basis"])
        self.assertIn("no reflectance", report["not_produced"])
        dongle, _ = self.only(image(**{"XMP:LS_status": 0}))
        self.assertEqual(dongle["sun_sensor_status"], "INVALID_STATE_DUE_TO_INSERTION_OF_USB_DONGLE")
        self.assertEqual(dongle["irradiance_reported"], "2000.0")

    def test_absent_fields_stay_blank_and_never_become_zero(self):
        stripped = image(**{key: None for key in (
            "XMP:Irradiance", "XMP:RawData", "XMP:SensorGain", "XMP:SensorGainAdjustment",
            "XMP:ExposureTime", "XMP:BlackLevel", "XMP:RtkFlag", "XMP:SurveyingMode",
            "XMP:AbsoluteAltitude", "XMP:RelativeAltitude", "XMP:LS_status",
            "XMP:VignettingFlag", "XMP:DewarpFlag", "XMP:GimbalYawDegree")})
        row, _ = self.only(stripped)
        for column in ("irradiance_reported", "sun_sensor_raw_green", "sensor_gain",
                       "exposure_time_us", "black_level_code", "rtk_flag_code",
                       "rtk_positioning_quality", "surveying_mode_reported", "absolute_altitude_m",
                       "relative_altitude_m", "sun_sensor_status", "vignetting_state",
                       "dewarp_state", "gimbal_yaw_deg"):
            self.assertEqual(row[column], "", column)
        zeroed, _ = self.only(image(**{"XMP:Irradiance": 0, "XMP:RtkFlag": 0}))
        self.assertEqual(zeroed["irradiance_reported"], "0")
        self.assertEqual(zeroed["rtk_positioning_quality"], "FAILED_TO_POSITION")

    def test_unqualified_writers_and_unusable_declarations_are_refused(self):
        for broken in (
                {"ExifTool:ExifToolVersion": 13.60},              # unqualified exporter release
                {"ExifTool:ExifToolVersion": None},
                {"XMP:Version": "1.5"},                            # unqualified writer contract
                {"XMP:Version": None},
                {"XMP:ImageSource": None},
                {"XMP:BandName": None},
                {"XMP:BandFreq": "860 nm"},                        # not the documented form
                {"XMP:BandFreq": "860 (+/-26)"},
                {"XMP:GPSLatitude": "22.000000\u00b0E"},          # hemisphere of the other axis
                {"XMP:GPSLongitude": None},                        # one axis without the other
                {"XMP:GPSLatitude": "91.000000\u00b0N"},          # outside its range
                {"XMP:RawData": "1.0,2.0,3.0"},                    # not the four documented values
                {"XMP:RawData": "1,,2,3,4"},                       # an empty interior position
                {"XMP:RawData": "1,2,3,4,"},                       # an empty trailing position
                {"XMP:RawData": ",1,2,3"},                         # an empty leading position
                {"XMP:VignettingData": "1,2,3,4,5"},               # not the six documented ones
                {"XMP:Irradiance": "not-a-number"},
                {"XMP:ExposureTime": 1.5},                         # documented as an integer
                {"XMP:AbsoluteAltitude": "+1e999"},                # overflows the reader
                {"XMP:Irradiance": "1e-400"},                      # underflows to zero there
                {"EXIF:GPSLatitudeRef": "E"},
                {"EXIF:GPSLatitude": -22, "Composite:GPSLatitude": -22},
        ):
            with self.assertRaises(ValueError, msg=broken):
                rows_of(image(**broken))
        with self.assertRaises(ValueError):
            rows_of(image(**{"EXIF:GPSLatitude": 23.0, "EXIF:GPSLatitudeRef": "N",
                             "EXIF:GPSLongitude": 113.0, "EXIF:GPSLongitudeRef": "E"}))

    def test_identity_and_endpoint_fields_are_counted_but_never_emitted(self):
        row, report = self.only(image())
        retained = [item["field_path"]
                    for item in report["retained_tags"][0]["not_interpreted"]]
        self.assertEqual(retained, [
            "Composite:GPSDateTime", "Composite:GPSLatitude", "Composite:GPSLongitude",
            "Composite:GPSPosition", "Composite:ImageSize", "Composite:Megapixels",
            "Composite:ShutterSpeed", "EXIF:Compression", "EXIF:GPSVersionID",
            "EXIF:ImageHeight", "EXIF:ImageWidth", "EXIF:PhotometricInterpretation",
            "EXIF:SamplesPerPixel", "EXIF:StripByteCounts", "SourceFile", "XMP:DroneID",
            "XMP:XMPToolkit"])
        self.assertNotIn("XMP:GPSLatitude", retained)
        self.assertEqual(row["unknown_tag_count"], str(len(retained)))
        self.assertEqual(int(row["unknown_tag_count"]) + int(row["interpreted_tag_count"]),
                         len(image()))
        serialised = json.dumps(report) + ",".join(row.values())
        for secret in ("AUTHORED_SERIAL_NOT_A_REAL_DEVICE", "/authored/m3m/"):
            self.assertNotIn(secret, serialised)

    def test_one_row_is_one_exposure_and_never_a_track(self):
        rows, _, _ = rows_of(image("Green"), image("Red"), image("RedEdge"))
        self.assertEqual([row["source_image_index"] for row in rows], ["1", "2", "3"])
        self.assertEqual({row["record_time_us"] for row in rows}, {str(CAPTURE_US)})
        for row in rows:
            self.assertIn("never_a_vehicle_telemetry_series", row["telemetry_basis"])
            self.assertEqual(row["flight_north_speed_m_s"], "3.00")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_the_documented_meanings_reach_common_observations(self):
        _, _, output = rows_of(image("Green"), image("NIR"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE),
                                     str(path), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            common = json.loads(result.stdout)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (2, "Unknown"))
        fields = [observation["fields"] for observation in common["observations"]]
        self.assertEqual([row["band_name_reported"] for row in fields], ["Green", "NIR"])
        self.assertEqual([row["band_central_wavelength_nm"] for row in fields], [560, 860])
        self.assertEqual([row["band_half_width_nm"] for row in fields], [16, 26])
        self.assertEqual([row["capture_latitude_deg"] for row in fields], [22.0, 22.0])
        self.assertEqual([row["exposure_time_us"] for row in fields], [1000, 1000])
        for row in fields:
            self.assertEqual(str(row["metadata_version_declared"]), QUALIFIED_METADATA_VERSION)
            self.assertEqual(row["altitude_type_reported"], "RtkAlt")
            self.assertEqual(row["sun_sensor_status"], "VALID_AND_COMPENSATING_STATE")
        for observation in common["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"])
        units = common["profile_units"]
        for name in ("band_central_wavelength_nm", "band_half_width_nm", "exposure_time_us",
                     "absolute_altitude_m", "relative_altitude_m", "irradiance_reported",
                     "capture_latitude_deg", "reflectance_basis", "telemetry_basis",
                     "sun_sensor_status", "black_level_code", "unknown_tag_count"):
            self.assertTrue(units.get(name), name)
        self.assertIn("HALF_width", units["band_half_width_nm"])
        self.assertIn("MICROSECONDS", units["exposure_time_us"])
        self.assertIn("names_NO_datum_for_it", units["absolute_altitude_m"])
        self.assertIn("NO_reflectance", units["reflectance_basis"])
        self.assertIn("ONE_saved_exposure", units["telemetry_basis"])


    def test_a_written_name_a_csv_row_cannot_carry_is_preserved_not_lost(self):
        row, _ = self.only(image(**{"XMP:BandName": "Pan,chromatic",
                                    "XMP:BandFreq": None, "XMP:Sensorindex": None,
                                    "XMP:GpsStatus": 'say "RTK"'}))
        self.assertEqual(row["band_name_reported"], "hex:" + "Pan,chromatic".encode().hex())
        self.assertEqual(row["gps_status_reported"], "hex:" + 'say "RTK"'.encode().hex())
        self.assertTrue(row["band_name_status"].startswith("UNLISTED"))
        plain, _ = self.only(image(**{"XMP:BandName": "Panchromatic", "XMP:BandFreq": None,
                                      "XMP:Sensorindex": None}))
        self.assertEqual(plain["band_name_reported"], "Panchromatic")

    def test_the_stored_pixel_statement_follows_this_records_own_flags(self):
        both_zero, _ = self.only(image())
        self.assertIn("both_declared_flags_in_THIS_record_are_zero", both_zero["stored_pixel_basis"])
        for changed in ({"XMP:VignettingFlag": 1}, {"XMP:DewarpFlag": 1},
                        {"XMP:VignettingFlag": None}, {"XMP:DewarpFlag": None}):
            row, _ = self.only(image(**changed))
            self.assertIn("no_claim_is_made_here_that_the_stored_band_image_is_uncompensated",
                          row["stored_pixel_basis"])
            self.assertNotIn("both_declared_flags_in_THIS_record_are_zero",
                             row["stored_pixel_basis"])

    @unittest.skipUnless(os.environ.get("MUSUBI_EXIFTOOL"), "set MUSUBI_EXIFTOOL to re-derive")
    def test_the_pinned_exporter_still_emits_this_shape(self):
        """Re-derive OBSERVED from the pinned exporter, so the fixture cannot drift.

        `MUSUBI_EXIFTOOL` is the command that runs ExifTool 13.59 (for example
        `perl /path/Image-ExifTool-13.59/exiftool`). Nothing is installed or bundled here.
        """
        exporter = shlex.split(os.environ["MUSUBI_EXIFTOOL"])
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "authored.tif").write_bytes(authored_tiff())
            (work / "packet.xmp").write_text(authored_packet(), encoding="utf-8")
            subprocess.run(exporter + ["-overwrite_original", "-xmp<=" + str(work / "packet.xmp"),
                                       str(work / "authored.tif")], check=True, capture_output=True)
            subprocess.run(exporter + ["-overwrite_original", "-GPSDateStamp=2023:03:09",
                                       "-GPSTimeStamp=02:47:57", "-GPSLatitude=22.0",
                                       "-GPSLatitudeRef=N", "-GPSLongitude=113.0",
                                       "-GPSLongitudeRef=E", str(work / "authored.tif")],
                           check=True, capture_output=True)
            result = subprocess.run(exporter + ["-json", "-n", "-G", str(work / "authored.tif")],
                                    check=True, capture_output=True)
            produced = json.loads(result.stdout)[0]
        version = produced.get("ExifTool:ExifToolVersion")
        self.assertEqual(version, 13.59, "this proof is pinned to one exporter release")
        stable = {key: value for key, value in produced.items()
                  if not key.startswith("File:") and key != "SourceFile"
                  and key != "EXIF:StripOffsets"}
        self.assertEqual(stable, OBSERVED)
        produced["SourceFile"] = "/authored/m3m/DJI_0001_MS_NIR.TIF"
        rows, _, _ = rows_of(produced)
        self.assertEqual(rows[0]["band_name_reported"], "NIR")
        self.assertEqual(rows[0]["exposure_time_us"], "1000")


if __name__ == "__main__":
    unittest.main()
