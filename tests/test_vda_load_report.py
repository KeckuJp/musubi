"""Authored VDA5050 state records only. No robot, no fleet manager, no broker, no real log.

The chain is the advertised one: authored state records through the **adopted** VDA5050 state
converter, through this bridge, and through the **real** common reader. The adopted converter and
its fixtures are reused, never rebuilt, and nothing in it is edited.
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

import scripts.convert_ardupilot_battery_csv as adopted
from scripts.convert_vda_load_report import FIELDS, convert
from tests.test_electrical_schema_reuse import vda

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/vda-load-report/profile.toml"
PALLET = {"loadId": "pallet-42", "loadType": "EPAL", "loadPosition": "fork", "weight": 720,
          "loadDimensions": {"length": 1.2, "width": 0.8, "height": 1.05},
          "boundingBoxReference": {"x": 0, "y": 0, "z": 0, "theta": 0.25}}


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class VdaLoadReport(unittest.TestCase):
    def state(self, *records, version="3.0.0"):
        """Authored records through the ADOPTED converter, exactly as an engineer would."""
        selector = "vda-state-" + version.rsplit(".", 1)[0]
        return adopted.convert("\n".join(json.dumps(r) for r in records), selector,
                               vda_actions=True, vda_errors=True).encode()

    def record(self, version="3.0.0", **extra):
        record = vda(version)
        record.update(orderId="order-7", orderUpdateId=3, **extra)
        return record

    def rows(self, *records, version="3.0.0"):
        output, report = convert(self.state(*records, version=version))
        return list(csv.DictReader(io.StringIO(output))), report, output

    def test_the_three_reported_load_states_the_pinned_schema_defines_stay_apart(self):
        rows, report, _ = self.rows(self.record(loads=[PALLET]),
                                    self.record(loads=[]),
                                    self.record())
        self.assertEqual([r["vda_record_kind"] for r in rows],
                         ["LOAD", "LOAD_STATE", "LOAD_STATE"])
        self.assertIn("THE_ROBOT_REPORTS_ITSELF_UNLOADED", rows[1]["load_report_state"])
        self.assertIn("DOES_NOT_REPORT_WHETHER_IT_IS_CARRYING_ANYTHING",
                      rows[2]["load_report_state"])
        self.assertNotEqual(rows[1]["load_report_state"], rows[2]["load_report_state"])
        self.assertEqual((report["records_reporting_unloaded"],
                          report["records_not_reporting_load_state"],
                          report["loads_reported"]), (1, 1, 1))

    def test_an_empty_load_id_is_neither_an_identity_nor_a_missing_field(self):
        rows, _, _ = self.rows(self.record(loads=[{"loadId": ""}, {"loadType": "EPAL"}]))
        self.assertEqual(rows[0]["load_id_hex"], "")
        self.assertIn("ABLE_TO_IDENTIFY_THE_LOAD_BUT_NOT_HAVING_IDENTIFIED_IT_YET",
                      rows[0]["load_identification"])
        self.assertIn("ABSENT_FROM_THE_REPORT", rows[1]["load_identification"])
        self.assertEqual([r["load_index"] for r in rows], ["0", "1"])

    def test_the_units_the_schema_states_are_carried_and_the_ones_it_does_not_are_not(self):
        rows, _, _ = self.rows(self.record(loads=[PALLET]))
        row = rows[0]
        self.assertEqual((row["load_weight_kg"], row["load_length_m"], row["load_width_m"],
                          row["load_height_m"]), ("720", "1.2", "0.8", "1.05"))
        self.assertEqual((row["bbox_reference_x"], row["bbox_reference_z"]), ("0", "0"))
        self.assertEqual(row["bbox_reference_theta"], "0.25")
        self.assertIn("STATES_NO_UNIT", PROFILE.read_text(encoding="utf-8").upper())
        self.assertEqual(unhex(row["load_type_hex"]), "EPAL")
        self.assertEqual(unhex(row["load_position_hex"]), "fork")

    def test_a_reported_load_stays_attached_to_the_task_and_instance_that_reported_it(self):
        rows, _, _ = self.rows(self.record(loads=[PALLET]))
        row = rows[0]
        self.assertEqual(unhex(row["order_id_hex"]), "order-7")
        self.assertEqual(row["order_update_id"], "3")
        self.assertEqual(unhex(row["instance_serial_number_hex"]), "example-1")
        self.assertEqual(unhex(row["instance_manufacturer_hex"]), "synthetic")
        self.assertTrue(row["state_record_sha256"].startswith("sha256:"))

    def test_finished_never_becomes_a_moved_item_on_any_row(self):
        """The whole point: an action that ended is not an item that moved."""
        rows, report, _ = self.rows(self.record(
            actionStates=[{"actionId": "a1", "actionType": "pick", "actionStatus": "FINISHED"}],
            loads=[]))
        for row in rows:
            self.assertIn("ACTIONSTATUS_OF_FINISHED_IS_A_REPORT_THAT_AN_ACTION_ENDED",
                          row["action_type_authority"])
            self.assertIn("NEVER_EVIDENCE_THAT_A_LOAD_WAS_PICKED_PLACED_PUT_AWAY_REPLENISHED_OR_"
                          "PHYSICALLY_MOVED", row["action_type_authority"])
            self.assertIn("PROOF_THAT_AN_ITEM_MOVED", row["load_basis"])
            self.assertIn("NOT_A_WAREHOUSE_TRANSACTION",
                          row["load_basis"].replace("NOTHING_IN_THIS_ROW_IS_A_WAREHOUSE",
                                                    "NOT_A_WAREHOUSE"))
        self.assertEqual(report["physical_movement_records"],
                         "NOT_DERIVED_A_REPORTED_LOAD_IS_NOT_A_MOVED_ITEM")
        self.assertIn("REPORTS_ITSELF_UNLOADED", rows[0]["load_report_state"])

    def test_detail_rows_are_skipped_with_accounting_and_never_counted_as_loads(self):
        state = adopted.convert(json.dumps(self.record(
            actionStates=[{"actionId": "a1", "actionStatus": "FINISHED"}],
            errors=[{"errorType": "x", "errorLevel": "WARNING",
                     "errorReferences": [{"referenceKey": "k", "referenceValue": "v"}]}],
            loads=[PALLET])), "vda-state-3.0", vda_actions=True, vda_errors=True)
        kinds = [r["vda_record_kind"] for r in csv.DictReader(io.StringIO(state))]
        self.assertEqual(kinds, ["SUMMARY", "ACTION", "ERROR", "ERROR_REFERENCE"])
        _, report = convert(state.encode())
        self.assertEqual((report["state_records_read"], report["loads_reported"]), (1, 1))
        self.assertEqual(report["detail_rows_skipped"],
                         {"ACTION": 1, "ERROR": 1, "ERROR_REFERENCE": 1})

    def test_a_row_is_never_assumed_to_be_a_summary(self):
        """An unlabelled or unknown kind is refused, not read as a state record."""
        summary_only = adopted.convert(json.dumps(self.record(loads=[PALLET])),
                                       "vda-state-3.0").encode()
        self.assertNotIn(b"vda_record_kind", summary_only.splitlines()[0])
        with self.assertRaises(ValueError) as refused:
            convert(summary_only)
        self.assertIn("vda_record_kind", str(refused.exception))

        good = self.state(self.record(loads=[PALLET]))
        header = good.decode().splitlines()[0].split(",")
        row = dict(zip(header, good.decode().splitlines()[1].split(",")))
        for kind, why in (("", "a blank kind"), ("SUMMARY_LIKE", "a kind this bridge cannot read")):
            merged = {**row, "vda_record_kind": kind}
            forged = (",".join(header) + "\n" + ",".join(merged[c] for c in header) + "\n").encode()
            with self.assertRaises(ValueError, msg=why):
                convert(forged)

    def test_the_same_six_fields_are_read_on_the_older_pinned_version(self):
        """2.1.0 and 3.0.0 define the same load fields; this is not a 3.0-only path."""
        rows, _, _ = self.rows(self.record(version="2.1.0", loads=[PALLET]), version="2.1.0")
        self.assertEqual(unhex(rows[0]["load_id_hex"]), "pallet-42")
        self.assertEqual(rows[0]["load_weight_kg"], "720")

    def test_unknown_properties_are_counted_where_they_are_declared_and_not_overclaimed(self):
        _, report, _ = self.rows(self.record(loads=[
            {**PALLET, "vendorExtra": {"a": 1},
             "loadDimensions": {"length": 1, "width": 1, "vendorDepth": 2},
             "boundingBoxReference": {"x": 0, "y": 0, "z": 0, "vendorRef": 1}}]))
        self.assertEqual(report["unknown_load_properties_at_declared_levels"], 3)
        self.assertIn("NEVER_A_CLAIM_THAT_EVERY_UNKNOWN_STRUCTURE_WAS_ACCOUNTED_FOR",
                      report["deeper_unknown_structure"])

    def test_the_seam_keeps_the_original_number_text_and_the_bridge_decides_it(self):
        """Which side rejects what, asserted rather than assumed."""
        line = json.dumps(self.record(loads=[{"weight": 1}])).replace(
            '"weight": 1', '"weight": 1e-400')
        state = adopted.convert(line, "vda-state-3.0").encode()
        preserved = bytes.fromhex(
            next(csv.DictReader(io.StringIO(state.decode())))["source_record_hex"][4:])
        self.assertIn(b"1e-400", preserved)
        with self.assertRaises(ValueError):
            convert(state)
        for token in ("1e999", "-1e-400"):
            with self.assertRaises(ValueError):
                convert(adopted.convert(
                    json.dumps(self.record(loads=[{"weight": 1}])).replace(
                        '"weight": 1', '"weight": %s' % token), "vda-state-3.0").encode())
        rows, _, _ = self.rows(self.record(loads=[{"weight": 0}]))
        self.assertEqual(rows[0]["load_weight_kg"], "0")

    def test_a_duplicate_field_is_refused_and_the_producer_refuses_it_first(self):
        text = json.dumps(self.record(loads=[{"weight": 1}])).replace(
            '{"weight": 1}', '{"weight": 1, "weight": 2}')
        with self.assertRaises(ValueError):
            adopted.convert(text, "vda-state-3.0")
        good = self.state(self.record(loads=[{"weight": 1}]))
        header, row = good.decode().splitlines()[:2]
        columns = dict(zip(header.split(","), row.split(",")))
        columns["source_record_hex"] = "hex:" + text.encode().hex()
        forged = (header + "\n" + ",".join(columns[c] for c in header.split(",")) + "\n").encode()
        with self.assertRaises(ValueError):
            convert(forged)

    def test_a_present_null_is_malformed_and_a_missing_key_is_not(self):
        """Optional in the pinned schema means the key may be left out, not set to null."""
        for loads, why in (
                (None, "loads set to null rather than left out"),
                ([{"loadDimensions": None}], "a present null loadDimensions"),
                ([{"boundingBoxReference": None}], "a present null boundingBoxReference")):
            with self.assertRaises(ValueError, msg=why):
                convert(self.state(self.record(loads=loads)))
        rows, _, _ = self.rows(self.record(loads=[{"loadId": "p"}]))
        self.assertEqual(rows[0]["load_length_m"], "")
        self.assertEqual(rows[0]["bbox_reference_x"], "")

    def test_only_the_producers_own_qualified_output_is_read(self):
        with self.assertRaises(ValueError):
            convert(b"record_time_us,source_record_hex\n1000,hex:" + b"{}".hex().encode() + b"\n")
        good = self.state(self.record(loads=[PALLET]))
        header = good.decode().splitlines()[0].split(",")
        row = dict(zip(header, good.decode().splitlines()[1].split(",")))

        def rebuilt(**changed):
            merged = {**row, **changed}
            return (",".join(header) + "\n" + ",".join(merged[c] for c in header) + "\n").encode()

        with self.assertRaises(ValueError):
            convert(rebuilt(vda_selected_version_hex="hex:" + b"2.0.0".hex()))
        with self.assertRaises(ValueError):
            convert(rebuilt(vda_order_id_hex="hex:" + b"another-order".hex()))
        with self.assertRaises(ValueError):
            convert(rebuilt(vda_instance_serial_number_hex="hex:" + b"other-robot".hex()))
        rows = list(csv.DictReader(io.StringIO(convert(rebuilt())[0])))
        self.assertIn("CHECKED_AGAINST_THE_PRESERVED_RECORD",
                      rows[0]["passthrough_identity_basis"])

    def test_a_malformed_or_impossible_report_is_refused(self):
        for loads, why in (
                ("not-an-array", "a loads value that is not an array"),
                ([["not", "an", "object"]], "a load entry that is not an object"),
                ([{"loadId": 7}], "a loadId that is not a string"),
                ([{"loadType": 7}], "a loadType that is not a string"),
                ([{"weight": "720"}], "a weight that is not a number"),
                ([{"weight": -1}], "a negative absolute weight"),
                ([{"loadDimensions": {"length": 1.2}}], "dimensions without the required width"),
                ([{"loadDimensions": {"length": -1, "width": 1}}], "a negative absolute length"),
                ([{"boundingBoxReference": {"x": 0, "y": 0}}], "a reference point without z"),
                ([{"weight": float("inf")}], "a weight that is not finite"),
                ([{} for _ in range(65)], "more reported loads than the finite bound")):
            with self.assertRaises(ValueError, msg=why):
                convert(self.state(self.record(loads=loads)))
        for data, why in (
                (b"record_time_us,source_record_hex\n1000,notfhex\n",
                 "a preserved record not in the adopted hex form"),
                (b"record_time_us,source_record_hex\n1000,hex:6e6f\n",
                 "a preserved record that is not JSON"),
                (b"record_time_us,source_record_hex\n1000,hex:5b5d\n",
                 "a preserved record that is not a JSON object"),
                (b"record_time_us,nope\n1000,x\n", "an input without the preserved record"),
                (b"record_time_us,source_record_hex\n", "a file with no rows")):
            with self.assertRaises(ValueError, msg=why):
                convert(data)

    def test_every_emitted_column_is_declared_and_the_rows_reach_common_output(self):
        output, _ = convert(self.state(self.record(loads=[PALLET]), self.record(loads=[])))
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
            self.assertEqual(fields["vda_record_kind"], "LOAD")
            self.assertEqual(fields["load_weight_kg"], 720)

    def test_the_command_line_writes_the_same_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "state.csv", base / "out"
            source.write_bytes(self.state(self.record(loads=[PALLET])))
            subprocess.run([sys.executable, "-m", "scripts.convert_vda_load_report",
                            str(source), str(out)], cwd=ROOT, check=True, capture_output=True)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["loads_reported"], 1)
            self.assertEqual(report["dispatched_orders"],
                             "NONE_THIS_TOOL_ORDERS_NOTHING_AND_EXECUTES_NOTHING")


if __name__ == "__main__":
    unittest.main()
