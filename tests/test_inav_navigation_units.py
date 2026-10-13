"""Fixed saved host snapshots: authored inputs, not actual flight evidence."""
import csv
import io
import tempfile
from pathlib import Path
import unittest

from tests.test_electrical_schema_reuse import module
from tests.saved_can_window_helpers import common

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"
HEADER = "timestamp,gpsFix,gpsNumSat,gpsLat,gpsLon,gpsAlt,gpsSpeed,gpsGroundCourse"


class InavNavigationUnitsTests(unittest.TestCase):
    def test_qualified_course_two_layouts_common_and_default_unchanged(self):
        basis = "inav-7.1.0-aa854365-ublox"
        for text, expected in (
            (HEADER + "\n100,3,14,35,139,-25,1200,3599\n101,0,0,0,0,0,0,0\n", 359.9),
            ("timestamp,voltage,amperage,mAhdrawn,rssi," + HEADER.split(",", 1)[1]
             + "\n200,12.5,2.5,600,100,2,9,-33,151,4,500,65535\n", 6553.5),
        ):
            before = list(csv.DictReader(io.StringIO(module.convert_inav_logging(text))))
            output = module.convert(text, module.INAV_FORMAT, inav_course_basis=basis)
            after = list(csv.DictReader(io.StringIO(output)))
            for old, new in zip(before, after):
                self.assertEqual(old, {key: new[key] for key in old})
            with tempfile.TemporaryDirectory() as directory:
                report = common(self, Path(directory), output, PROFILE)
            fields = report["observations"][0]["fields"]
            self.assertEqual(fields["inav_gps_ground_course_deg_reported"], expected)
            self.assertIn("NOT_YAW_REFERENCE_UNKNOWN", fields["inav_gps_ground_course_basis"])
            self.assertEqual(fields["inav_gps_ground_course_code_reported"], int(expected * 10))
            if len(after) == 2:
                self.assertEqual(float(after[1]["inav_gps_ground_course_deg_reported"]), 0)

    def test_course_qualification_and_bad_wire_values_refuse(self):
        basis = "inav-7.1.0-aa854365-ublox"
        for value in ("NaN", "inf", "-1", "65536", "1.5"):
            with self.assertRaises(ValueError):
                module.convert_inav_logging(HEADER + f"\n1,3,4,35,139,2,3,{value}\n", course_basis=basis)
        for text, fmt, selected in (
            (HEADER + "\n1,3,4,35,139,2,3,4\n", module.INAV_FORMAT, "degrees"),
            ("", "ardupilot-bat", basis),
            ("timestamp,kinematicsX,kinematicsY,kinematicsZ\n1,1,2,3\n", module.INAV_FORMAT, basis),
        ):
            with self.assertRaises(ValueError):
                module.convert(text, fmt, inav_course_basis=selected)

    def test_two_layouts_units_raw_unknown_clock_and_absent_group(self):
        sources = (
            HEADER + ",future\r\n100,3,14,-33.8688,151.2093,-25,1200,3599,kept\r\n"
            "101,0,0,0,0,0,0,0,unset\r\n",
            "timestamp,voltage,amperage,mAhdrawn,rssi," + HEADER.split(",", 1)[1]
            + "\n200,12.5,2.5,600,100,2,9,35,139,32767,65535,270\n",
        )
        for text, altitude, speed in zip(sources, (-25, 32767), (12, 655.35)):
            output = module.convert_inav_logging(text)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(rows[0]["inav_gps_altitude_m_reported"]), altitude)
            self.assertEqual(float(rows[0]["inav_gps_speed_m_s_reported"]), speed)
            self.assertEqual(rows[0]["inav_gps_altitude_datum"], "UNKNOWN")
            self.assertIn("ZERO_MAY_BE_UNSET", rows[0]["inav_value_basis"])
            self.assertEqual(bytes.fromhex(rows[0]["source_record_hex"][4:]).decode(),
                             text.splitlines()[1])
            with tempfile.TemporaryDirectory() as directory:
                report = common(self, Path(directory), output, PROFILE)
            values = report["observations"][0]["fields"]
            self.assertEqual(values["inav_gps_altitude_m_reported"], altitude)
            self.assertEqual(values["inav_gps_speed_m_s_reported"], speed)
            self.assertEqual(values["inav_gps_altitude_datum"], "UNKNOWN")
            if len(rows) == 2:
                self.assertEqual(float(rows[1]["inav_gps_speed_m_s_reported"]), 0)
        absent = module.convert_inav_logging("timestamp,kinematicsX,kinematicsY,kinematicsZ\n1,1,2,3\n")
        row = next(csv.DictReader(io.StringIO(absent)))
        self.assertEqual(row["inav_gps_altitude_m_reported"], "")
        self.assertEqual(row["inav_gps_navigation_unit_basis"], "")

    def test_incomplete_noninteger_and_range_refusal(self):
        for altitude, speed in (("NaN", "1"), ("32768", "1"), ("1", "-1"), ("1", "65536")):
            with self.assertRaises(ValueError):
                module.convert_inav_logging(HEADER + f"\n1,3,4,35,139,{altitude},{speed},1\n")
        with self.assertRaises(ValueError):
            module.convert_inav_logging("timestamp,gpsAlt,gpsSpeed\n1,2,3\n")


if __name__ == "__main__":
    unittest.main()
