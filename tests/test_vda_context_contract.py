"""Coordinator-authored same-record contract; no cross-record location inference."""
import csv
import io
import unittest

from scripts.convert_vda_load_report import convert
import tests.test_vda_load_report as fixtures

PALLET = fixtures.PALLET


def position(version, x, usable=True, score=1):
    return {"mapId": "declared-map", "x": x, "y": 2, "theta": 0.5,
            "positionInitialized" if version == "2.1.0" else "localized": usable,
            "localizationScore": score}


class VdaContextContract(unittest.TestCase):
    def test_equal_time_equal_identity_records_never_join_their_map_positions(self):
        fixture = fixtures.VdaLoadReport()
        first = fixture.record(loads=[PALLET, {"loadId": "other"}],
                               mobileRobotPosition=position("3.0.0", 11))
        second = fixture.record(loads=[], mobileRobotPosition=position("3.0.0", 22))
        data = fixture.state(first, second)
        output, _ = convert(data, state_context=True)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([row["state_summary_index"] for row in rows], ["1", "1", "2"])
        self.assertEqual([float(row["vda_map_x_m"]) for row in rows], [11, 11, 22])
        self.assertEqual([row["vda_record_kind"] for row in rows], ["LOAD", "LOAD", "LOAD_STATE"])
        self.assertEqual(rows[0]["state_record_sha256"], rows[1]["state_record_sha256"])
        self.assertNotEqual(rows[1]["state_record_sha256"], rows[2]["state_record_sha256"])
        self.assertTrue(all(row["state_context_basis"] for row in rows))
        old_output, _ = convert(data)
        self.assertNotIn("state_summary_index", next(csv.DictReader(io.StringIO(old_output))))

    def test_unavailable_coordinates_are_not_recovered_from_raw_values(self):
        fixture = fixtures.VdaLoadReport()
        for version in ("2.1.0", "3.0.0"):
            for usable, score in ((False, 1), (True, 0)):
                with self.subTest(version=version, usable=usable, score=score):
                    key = "agvPosition" if version == "2.1.0" else "mobileRobotPosition"
                    record = fixture.record(version=version, loads=[PALLET],
                        **{key: position(version, 99, usable, score)})
                    output, _ = convert(fixture.state(record, version=version), state_context=True)
                    row = next(csv.DictReader(io.StringIO(output)))
                    self.assertEqual(row["vda_map_x_m"], "")
                    self.assertEqual(row["vda_map_y_m"], "")
                    self.assertEqual(row["vda_map_theta_rad"], "")
                    self.assertEqual(row["vda_position_state"], "UNAVAILABLE_POSITION_REPORTED")
                    self.assertEqual(float(row["load_weight_kg"]), 720)

    def test_source_and_projected_context_disagreement_is_not_silently_resolved(self):
        fixture = fixtures.VdaLoadReport()
        data = fixture.state(fixture.record(loads=[PALLET],
                    mobileRobotPosition=position("3.0.0", 11)))
        records = list(csv.DictReader(io.StringIO(data.decode())))
        records[0]["vda_map_x_m"] = "999"
        changed = io.StringIO()
        writer = csv.DictWriter(changed, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
        with self.assertRaises(ValueError):
            convert(changed.getvalue().encode(), state_context=True)

    def test_instance_and_task_declarations_cannot_be_swapped_under_the_pose(self):
        fixture = fixtures.VdaLoadReport()
        data = fixture.state(fixture.record(loads=[PALLET],
                    mobileRobotPosition=position("3.0.0", 11)))
        mutations = [("vda_instance_manufacturer_hex", "hex:6f74686572"),
                     ("vda_instance_serial_number_hex", "hex:6f74686572"),
                     ("vda_order_id_hex", "hex:6f74686572"),
                     ("vda_order_update_id", "999")]
        mutations += [(field, "") for field, _ in mutations]
        for field, replacement in mutations:
            with self.subTest(field=field):
                records = list(csv.DictReader(io.StringIO(data.decode())))
                records[0][field] = replacement
                changed = io.StringIO()
                writer = csv.DictWriter(changed, fieldnames=list(records[0]))
                writer.writeheader()
                writer.writerows(records)
                with self.assertRaises(ValueError):
                    convert(changed.getvalue().encode(), state_context=True)
