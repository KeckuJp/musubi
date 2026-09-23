"""Authored ISOBUS UT/TC frames; no bus, no device, no object pool, no real capture.

Every input is written here, byte by byte, from the layouts the pinned AgIsoStack++ source declares
(`1bd6189972ef148273b31a27b281b86c91edc3d4`). The upstream framing stage is the **adopted**
`scripts/convert_candump_recorded.py`, called as a subprocess rather than reimplemented, so this test
exercises the real chain: authored can-utils log -> adopted CAN CSV -> this path -> common Observation.

Nothing is transmitted and nothing is loaded onto a terminal. Real records 0.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_isobus_ut_tc_capture.py"
CANDUMP = ROOT / "scripts/convert_candump_recorded.py"
PROFILE = ROOT / "profiles/declared/isobus-ut-tc-passive/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

INTERFACE = "can0"


def identifier(pgn, destination, source, priority=6, page=0):
    """A 29-bit identifier for a destination-specific (PDU1) group, as the PDU rule composes it.

    `page` sets the data-page bits (bit 24 DP, bit 25 EDP). A selected group is a page-0 group, so a
    nonzero page with the same PF is a different parameter group entirely.
    """
    return ((priority << 26) | (page << 24) | ((pgn & 0xFF00) << 8)
            | (destination << 8) | source)


def frame(time_us, ident, data):
    """One can-utils absolute-format line, the shape the adopted converter parses."""
    return f"({time_us // 1000000}.{time_us % 1000000:06d}) {INTERFACE} {ident:08X}#{data.hex().upper()}"


def vt_status(master=0x26, data_mask=0x2710, soft_key_mask=0x1F40, busy=0x40, command=0xA3):
    """VT Status: function 0xFE, then the fields the pinned VT client reads from bytes 1..7."""
    return bytes([0xFE, master, data_mask & 0xFF, data_mask >> 8,
                  soft_key_mask & 0xFF, soft_key_mask >> 8, busy, command])


def tc_status(status=0x01, executing_source=0x26, executing_command=0x03):
    """TC Status: command nibble 0x0E; only bytes 4..6 are defined by the pinned source."""
    return bytes([0x0E, 0xFF, 0xFF, 0xFF, status, executing_source, executing_command, 0xFF])


def tc_value(element=0x123, ddi=141, value=-2):
    """TC Value: command nibble 0x03, 12-bit element, 16-bit DDI, signed 32-bit value."""
    raw = value & 0xFFFFFFFF
    return bytes([0x03 | ((element & 0x0F) << 4), (element >> 4) & 0xFF, ddi & 0xFF, ddi >> 8,
                  raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF, (raw >> 24) & 0xFF])


class IsobusUtTcCaptureTests(unittest.TestCase):
    def capture_csv(self, lines):
        """Run the ADOPTED candump converter over an authored log; its output is this path's input."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.log"
            source.write_text("\n".join(lines) + "\n", encoding="utf-8")
            target = Path(directory) / "out"
            done = subprocess.run(
                [os.sys.executable, str(CANDUMP), str(source), str(target),
                 "--interface", INTERFACE, "--extended"],
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return (target / "capture.csv").read_text(encoding="utf-8")

    def rows(self, text):
        converted, report = module.convert(text)
        return list(csv.DictReader(io.StringIO(converted))), report, converted

    def normal_capture(self):
        return self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xE600, 0x26, 0x2A), vt_status()),
            frame(1_700_000_000_100_000, identifier(0xCB00, 0x26, 0xF0), tc_status()),
            frame(1_700_000_000_200_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=0x123, ddi=7, value=2_500_000)),
        ])

    def test_both_roles_reach_selected_columns_with_the_pinned_meanings(self):
        rows, report, _ = self.rows(self.normal_capture())
        self.assertEqual(len(rows), 3)
        terminal, controller, value = rows
        self.assertTrue(terminal["isobus_protocol_role"].startswith("UNIVERSAL_TERMINAL"))
        self.assertTrue(controller["isobus_protocol_role"].startswith("TASK_CONTROLLER"))
        self.assertEqual(terminal["isobus_message_kind"], "VT_STATUS")
        self.assertEqual(terminal["isobus_pgn"], str(0xE600))
        self.assertEqual(terminal["isobus_source_address"], "42")
        self.assertEqual(terminal["isobus_destination_address"], "38")
        self.assertEqual(terminal["ut_working_set_master_address"], "38")
        self.assertEqual(terminal["ut_data_mask_object_id"], "10000")
        self.assertEqual(terminal["ut_soft_key_mask_object_id"], "8000")
        self.assertEqual(terminal["ut_busy_codes_bitfield"], "64")
        self.assertEqual(terminal["ut_auxiliary_input_learn_mode_reported"], "1")
        self.assertEqual(terminal["ut_current_command_function_code"], str(0xA3))
        self.assertEqual(terminal["tc_status_bitfield"], "")
        self.assertEqual(controller["isobus_message_kind"], "TC_STATUS")
        self.assertEqual(controller["isobus_pgn"], str(0xCB00))
        self.assertEqual(controller["tc_status_bitfield"], "1")
        self.assertEqual(controller["tc_source_address_of_command_being_executed"], "38")
        self.assertEqual(controller["tc_command_being_executed"], "3")
        self.assertEqual(controller["tc_ddi"], "")
        self.assertIn("UNDEFINED_IN_VERSION_2", report["tc_status_semantics"])
        self.assertEqual(value["isobus_message_kind"], "TC_VALUE")
        self.assertEqual(value["tc_element_number"], "291")
        self.assertEqual(value["tc_ddi"], "7")
        self.assertEqual(value["tc_process_data_value_reported"], "2500000")
        self.assertEqual(value["tc_ddi_quantity"], "actual_mass_rate_kg_m2")
        self.assertEqual(float(value["tc_ddi_scaled_value"]), 2.5)
        self.assertEqual(bytes.fromhex(value["source_record_hex"][4:])[4:],
                         tc_value(element=0x123, ddi=7, value=2_500_000))
        self.assertEqual(report["vt_status_messages"], 1)
        self.assertEqual(report["tc_status_messages"], 1)
        self.assertEqual(report["tc_value_messages"], 1)
        for text in (value["isobus_scope_basis"], report["scope"]):
            self.assertIn("NO_OBJECT_POOL_IS_LOADED", text)
            self.assertIn("NOT_AEF_CERTIFICATION", text)

    def test_unknown_and_recognised_but_uninterpreted_messages_are_counted_not_dropped(self):
        rows, report, _ = self.rows(self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xE600, 0x26, 0x2A), bytes([0x01] + [0] * 7)),
            frame(1_700_000_000_010_000, identifier(0xE600, 0x26, 0x2A), bytes([0x7B] + [0] * 7)),
            frame(1_700_000_000_020_000, identifier(0xCB00, 0x26, 0xF0), bytes([0x00] + [0] * 7)),
            frame(1_700_000_000_030_000, identifier(0xFE00, 0x26, 0x2A), bytes(8)),
            frame(1_700_000_000_040_000, identifier(0xE600, 0x26, 0x2A), vt_status()),
        ]))
        kinds = [row["isobus_message_kind"] for row in rows]
        self.assertEqual(kinds, ["VT_FUNCTION_BUTTON_ACTIVATION_NOT_INTERPRETED_HERE",
                                 "VT_FUNCTION_UNDEFINED_BY_THE_PIN",
                                 "TC_COMMAND_TECHNICAL_CAPABILITIES_NOT_INTERPRETED_HERE",
                                 "VT_STATUS"])
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["source_record_hex"].startswith("hex:") for row in rows))
        self.assertEqual(report["uninterpreted_vt_functions"],
                         {"BUTTON_ACTIVATION": 1, "code_0x7b": 1})
        self.assertEqual(report["uninterpreted_tc_commands"], {"TECHNICAL_CAPABILITIES": 1})
        self.assertEqual(report["unselected_pgns"], {"0x0fe26": 1})
        self.assertEqual(report["capture_rows"], 5)
        self.assertEqual(report["selected_rows"], 4)

    def test_wrong_input_and_truncation_are_refused_or_counted_by_name(self):
        for text in ("", "time_us,standard_id,data_hex\n1,123,00\n",
                     "time_us,extended_id,data_hex\nnot-a-row\n"):
            with self.subTest(text=text[:24]), self.assertRaises(ValueError):
                module.convert(text)
        truncated = self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xE600, 0x26, 0x2A), vt_status()[:5]),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0x26, 0xF0), tc_status()),
        ])
        rows, report, _ = self.rows(truncated)
        self.assertEqual([row["isobus_message_kind"] for row in rows], ["TC_STATUS"])
        self.assertEqual(report["short_selected_frames"],
                         [{"row": 1, "bytes": 5, "pgn": "0x0e600"}])
        with self.assertRaises(ValueError):
            self.rows(self.capture_csv([
                frame(1_700_000_000_000_000, identifier(0xFE00, 0x26, 0x2A), bytes(8))]))
        with self.assertRaises(ValueError):
            module.convert("time_us,extended_id,data_hex\n"
                           "20,18E6262A,FE260000000000A3\n"
                           "10,18E6262A,FE260000000000A3\n")

    def test_a_reported_value_reaches_its_declared_quantity_or_state(self):
        """The finite DDI set the adopted ISOXML converter pins, reused rather than re-declared."""
        rows, report, _ = self.rows(self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=7, value=2_500_000)),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=141, value=1)),
            frame(1_700_000_000_020_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=2, ddi=161, value=0b1001)),
            frame(1_700_000_000_030_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=3, ddi=262, value=250_000)),
        ]))
        rate, state, children, moisture = rows
        self.assertEqual(rate["tc_ddi_quantity"], "actual_mass_rate_kg_m2")
        self.assertEqual(float(rate["tc_ddi_scaled_value"]), 2.5)
        self.assertEqual(rate["tc_process_data_value_reported"], "2500000")  # raw wire value kept
        self.assertEqual(rate["tc_ddi_value_disposition"], "REPORTED")
        self.assertEqual(state["tc_reported_work_state"], "ENABLED")
        self.assertEqual(state["tc_ddi_quantity"], "reported_work_state")
        self.assertEqual(children["tc_child_state_first_ordinal"], "1")
        self.assertEqual(children["tc_child_states_named"].split("|")[:3],
                         ["ENABLED", "ERROR", "DISABLED"])
        self.assertEqual(len(children["tc_child_states_named"].split("|")), 16)
        self.assertEqual(float(moisture["tc_ddi_scaled_value"]), 0.25)
        self.assertEqual(moisture["tc_ddi_quantity"], "reported_average_crop_moisture_fraction")
        self.assertEqual(moisture["tc_process_data_value_reported"], "250000")
        self.assertIn("DDI_262_AT_ADOPTED_REVISION_3_UNIT_1_PPM", moisture["tc_ddi_meaning_basis"])
        self.assertIn("STAYS_THE_REPORTED_PARTS_PER_MILLION", moisture["tc_ddi_meaning_basis"])
        self.assertIn("DDI_7_AT_ADOPTED_REVISION_1", rate["tc_ddi_meaning_basis"])
        self.assertIn("DDI_141_AT_ADOPTED_REVISION_1_OF_2012_04_02", state["tc_ddi_meaning_basis"])
        for row in rows:
            self.assertIn("DICTIONARY_POINTERS_ARE_PER_ENTRY", row["tc_ddi_meaning_basis"])
            self.assertIn("NO_DDOP_IDENTITY_AND_NO_ACTUAL_DEVICE_PROOF", row["tc_ddi_meaning_basis"])
        self.assertEqual(report["tc_value_messages"], 4)
        self.assertEqual(report["out_of_range_values"], {})
        self.assertEqual(report["unqualified_ddis"], {})

    def test_a_request_is_not_a_reported_measurement(self):
        rows, report, _ = self.rows(self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xCB00, 0x26, 0xF0),
                  bytes([0x02 | (1 << 4), 0x00, 7, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=7, value=2_500_000)),
        ]))
        request, value = rows
        self.assertEqual(request["isobus_message_kind"], "TC_REQUEST_VALUE")
        self.assertEqual(request["tc_ddi"], "7")
        self.assertEqual(request["tc_element_number"], "1")
        self.assertEqual(request["tc_process_data_value_reported"], "")
        self.assertEqual(request["tc_ddi_quantity"], "")
        self.assertEqual(request["tc_ddi_scaled_value"], "")
        self.assertEqual(request["tc_reported_work_state"], "")
        self.assertEqual(request["tc_ddi_value_disposition"], "REQUEST_NOT_A_REPORTED_MEASUREMENT")
        self.assertIn("A_REQUEST_VALUE_MESSAGE_IS_A_REQUEST_AND_NOT_A_REPORTED_MEASUREMENT",
                      request["tc_ddi_meaning_basis"])
        self.assertEqual(request["tc_request_value_payload_raw"], "-1")
        self.assertEqual((report["tc_request_value_messages"], report["tc_value_messages"]), (1, 1))
        self.assertEqual(value["tc_request_value_payload_raw"], "")

    def test_a_selected_pf_on_another_data_page_is_refused_and_accounted(self):
        rows, report, _ = self.rows(self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xE600, 0x26, 0x2A, page=1), vt_status()),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26, page=2), tc_status()),
            frame(1_700_000_000_020_000, identifier(0xE600, 0x26, 0x2A), vt_status()),
        ]))
        self.assertEqual([row["isobus_message_kind"] for row in rows], ["VT_STATUS"])
        self.assertEqual(report["other_data_page_frames"],
                         [{"row": 1, "pgn": "0x1e600", "data_page_bits": 1},
                          {"row": 2, "pgn": "0x2cb00", "data_page_bits": 2}])
        self.assertEqual(report["unselected_pgns"], {})
        self.assertEqual((report["capture_rows"], report["selected_rows"]), (3, 1))

    def test_unqualified_ddis_and_out_of_range_values_stay_raw(self):
        rows, report, _ = self.rows(self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=999, value=1234)),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=84, value=-5)),
            frame(1_700_000_000_020_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=262, value=1_500_000)),
            frame(1_700_000_000_030_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=141, value=9)),
        ]))
        unknown, negative, moisture, state = rows
        self.assertEqual(unknown["tc_ddi_value_disposition"], "NOT_QUALIFIED_BY_THIS_ADAPTER_RAW_ONLY")
        self.assertIn("WHICH_THIS_ADAPTER_DOES_NOT_QUALIFY", unknown["tc_ddi_meaning_basis"])
        self.assertIn("NOT_A_CLAIM_THAT_NO_DICTIONARY_ENTRY_FOR_IT_EXISTS_ANYWHERE",
                      unknown["tc_ddi_meaning_basis"])
        self.assertEqual((unknown["tc_ddi_quantity"], unknown["tc_ddi_scaled_value"]), ("", ""))
        self.assertEqual(unknown["tc_process_data_value_reported"], "1234")
        self.assertEqual(negative["tc_ddi_value_disposition"], "OUTSIDE_PUBLISHED_RANGE_RETAINED_RAW")
        self.assertEqual(negative["tc_ddi_quantity"], "reported_yield_mass_kg_m2")
        self.assertEqual((negative["tc_ddi_scaled_value"], negative["tc_process_data_value_reported"]),
                         ("", "-5"))
        self.assertEqual(moisture["tc_ddi_value_disposition"], "OUTSIDE_FRACTION_RANGE")
        self.assertEqual(moisture["tc_ddi_scaled_value"], "")
        self.assertEqual(moisture["tc_ddi_quantity"], "reported_average_crop_moisture_ppm")
        self.assertEqual(state["tc_reported_work_state"], "")
        self.assertEqual(state["tc_ddi_value_disposition"], "OUTSIDE_PUBLISHED_RANGE_RETAINED_RAW")
        self.assertEqual(report["unqualified_ddis"], {"999": 1})
        self.assertEqual(report["out_of_range_values"], {"84": 1, "141": 1, "262": 1})

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_selected_messages_reach_the_actual_common_reader(self):
        _, _, converted = self.rows(self.normal_capture())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(converted, encoding="utf-8")
            common = json.loads(subprocess.run(
                [READER, str(PROFILE), str(path)], capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        self.assertEqual(common["platform_domain"], "Unknown")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        terminal, controller, value = common["observations"]
        self.assertEqual(terminal["fields"]["ut_data_mask_object_id"], 10000)
        self.assertEqual(terminal["fields"]["ut_auxiliary_input_learn_mode_reported"], 1)
        self.assertEqual(controller["fields"]["tc_status_bitfield"], 1)
        self.assertEqual(value["fields"]["tc_process_data_value_reported"], 2500000)
        self.assertEqual(value["fields"]["tc_ddi"], 7)
        self.assertEqual(value["fields"]["tc_ddi_scaled_value"], 2.5)
        self.assertEqual(value["fields"]["tc_ddi_quantity"], "actual_mass_rate_kg_m2")
        self.assertEqual(value["fields"]["tc_ddi_value_disposition"], "REPORTED")
        units = common["profile_units"]
        self.assertIn("SIGNED_32_bit_little_endian", units["tc_process_data_value_reported"])
        self.assertIn("names_what_the_scaled_or_state_column_beside_it_actually_holds",
                      units["tc_ddi_quantity"])
        self.assertIn("nothing_is_clamped_here", units["tc_ddi_scaled_value"])
        self.assertIn("never_a_measured_quantity", units["tc_request_value_payload_raw"])
        self.assertIn("only_the_0x40_bit", units["ut_busy_codes_bitfield"])
        self.assertIn("outside_this_path", units["isobus_scope_basis"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_the_moisture_fraction_and_its_unit_label_agree_in_common_output(self):
        """The scaled column, the label that names its unit, and the raw ppm must agree downstream."""
        _, _, converted = self.rows(self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=3, ddi=262, value=250_000)),
        ]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(converted, encoding="utf-8")
            common = json.loads(subprocess.run(
                [READER, str(PROFILE), str(path)], capture_output=True, check=True).stdout)
        fields = common["observations"][0]["fields"]
        self.assertEqual(fields["tc_ddi"], 262)
        self.assertEqual(fields["tc_ddi_quantity"], "reported_average_crop_moisture_fraction")
        self.assertEqual(fields["tc_ddi_scaled_value"], 0.25)
        self.assertEqual(fields["tc_process_data_value_reported"], 250000)
        self.assertEqual(fields["tc_ddi_value_disposition"], "REPORTED")
        units = common["profile_units"]
        self.assertIn("for_ddi_262_it_stays_parts_per_million",
                      units["tc_process_data_value_reported"])
        self.assertIn("crop_moisture_fraction_derived_from_the_ppm_report",
                      units["tc_ddi_scaled_value"])
        self.assertIn("ddi_262_at_revision_3", units["tc_ddi_meaning_basis"])

    def test_command_line_writes_the_conversion_and_its_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.csv"
            source.write_text(self.normal_capture(), encoding="utf-8")
            target = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)],
                           check=True, capture_output=True)
            report = json.loads((target / "report.json").read_text())
            self.assertEqual(report["selected_rows"], 3)
            self.assertEqual(report["clock"], "Unknown")
            self.assertIn("PASSIVE_CAPTURE_READING_ONLY", report["scope"])


if __name__ == "__main__":
    unittest.main()
