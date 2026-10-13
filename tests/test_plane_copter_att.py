"""Fixed producer-qualified authored ATT exports, not real flights."""
import csv
import hashlib
import io
import math
from pathlib import Path
import tempfile
import unittest
from scripts.convert_recorded_angles_csv import convert, convert_path
from tests.saved_can_window_helpers import common, ROOT


class PlaneCopterAttTests(unittest.TestCase):
    def test_two_fixed_schemas_window_and_common(self):
        for schema, values in (("plane-4.5.5-decoded-att", (1.25, -2.5, 90)),
                               ("copter-4.5.5-decoded-att", (-45, 10.5, 359.99))):
            source = "TimeUS,Roll,Pitch,Yaw,Extra\r\n" + "".join(
                f"{t},{values[0]},{values[1]},{values[2]},kept\r\n" for t in (1, 2, 2))
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                path = folder / "ATT.csv"
                path.write_bytes(source.encode())
                selected, report = convert_path(path, schema, start_record=1, stop_record=3)
                rows = list(csv.DictReader(io.StringIO(selected)))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["angle_schema"], schema)
                for name, value in zip(("roll", "pitch", "yaw"), values):
                    self.assertAlmostEqual(float(rows[0][f"reported_{name}_rad"]), value * math.pi / 180)
                self.assertIn("kept", bytes.fromhex(rows[0]["source_record_hex"][4:]).decode())
                self.assertIn(hashlib.sha256(source.encode()).hexdigest(), str(report))
                observations = common(self, folder, selected,
                    ROOT / "profiles/declared/recorded-angles/profile.toml")
                self.assertEqual(len(observations["observations"]), 2)
                self.assertIn("BootRelative", str(observations))
                old = list(csv.DictReader(io.StringIO(convert(source, "rover-4.5.5-decoded-att"))))
                self.assertEqual(rows[0]["reported_roll_rad"], old[1]["reported_roll_rad"])

    def test_unqualified_schema_and_bad_selected_values_refuse(self):
        good = "TimeUS,Roll,Pitch,Yaw\n1,1,2,3\n"
        for schema in ("plane-4.5.4-decoded-att", "copter-4.5.0-decoded-att"):
            with self.assertRaises(ValueError):
                convert(good, schema)
        for schema in ("plane-4.5.5-decoded-att", "copter-4.5.5-decoded-att"):
            for bad in (good.replace("Roll", "roll_wire"), good.replace("1,1,2,3", "1,nan,2,3")):
                with self.assertRaises(ValueError):
                    convert(bad, schema)
