"""Authored ISOBUS UT/TC frames; no bus, no device, no object pool, no real capture.

Every input is written here, byte by byte, from the layouts the pinned AgIsoStack++ source declares
(`1bd6189972ef148273b31a27b281b86c91edc3d4`). The upstream framing stage is the **adopted**
`scripts/convert_candump_recorded.py`, called as a subprocess rather than reimplemented, so this test
exercises the real chain: authored can-utils log -> adopted CAN CSV -> this path -> common Observation.

Nothing is transmitted and nothing is loaded onto a terminal. Real records 0.
"""
import csv
import hashlib
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
    @unittest.skipUnless(READER, "actual common reader required")
    def test_actual_crop_moisture_reuses_common_meaning(self):
        for sender, element, values in ((42, 3, [0, 123456]), (43, 4095, [1000000, 2147483647])):
            with self.subTest(sender=sender):
                text = self.capture_csv([frame(1700000000000000 + i,
                    identifier(0xCB00, 240, sender), tc_value(element, 99, value))
                    for i, value in enumerate(values)])
                converted, report = module.convert(text)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "observations.csv"
                    path.write_text(converted)
                    common = json.loads(subprocess.run([READER, str(PROFILE), str(path),
                        "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(report["tc_value_messages"], 2)
                for observation, value in zip(common["observations"], values):
                    row = observation["fields"]
                    self.assertEqual(row["tc_element_number"], element)
                    self.assertEqual(row["isobus_source_address"], sender)
                    self.assertEqual(row["tc_process_data_value_reported"], value)
                    self.assertIn("DDI_99_AT_ADOPTED_REVISION_1", row["tc_ddi_meaning_basis"])
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertEqual(row["source_document_sha256"], "sha256:" + hashlib.sha256(text.encode()).hexdigest())
                    self.assertTrue(row["source_record_hex"])
                    if value <= 1000000:
                        self.assertEqual(row["tc_ddi_quantity"], "reported_crop_moisture_fraction")
                        self.assertEqual(row["tc_ddi_scaled_value"], value / 1000000)
                    else:
                        self.assertEqual(row["tc_ddi_quantity"], "reported_crop_moisture_ppm")
                        self.assertIsNone(row["tc_ddi_scaled_value"])
                        self.assertEqual(row["tc_ddi_value_disposition"], "OUTSIDE_FRACTION_RANGE")

    def test_actual_moisture_request_negative_and_unknown_accounting(self):
        request = bytearray(tc_value(ddi=99, value=-1))
        request[0] = (request[0] & 0xF0) | 2
        text = self.capture_csv([frame(1700000000000000 + i, identifier(0xCB00, 240, 42), data) for i, data in enumerate([
            tc_value(ddi=99, value=-1), bytes(request), tc_value(ddi=12345, value=7),
            tc_value(ddi=262, value=250000)])])
        rows, report, _ = self.rows(text)
        self.assertEqual(rows[0]["tc_ddi_value_disposition"], "OUTSIDE_PUBLISHED_RANGE_RETAINED_RAW")
        self.assertEqual(rows[0]["tc_ddi_scaled_value"], "")
        self.assertEqual(rows[1]["tc_ddi_value_disposition"], "REQUEST_NOT_A_REPORTED_MEASUREMENT")
        self.assertEqual(rows[1]["tc_ddi_quantity"], "")
        self.assertEqual(report["unqualified_ddis"], {"12345": 1})
        self.assertEqual(report["tc_request_value_messages"], 1)
        self.assertEqual(rows[3]["tc_ddi_quantity"], "reported_average_crop_moisture_fraction")
        self.assertEqual(float(rows[3]["tc_ddi_scaled_value"]), .25)

    def speed_capture(self, values):
        return self.capture_csv([
            frame(1700000000000000, (3 << 26) | (0xF022 << 8) | 42,
                  speed.to_bytes(2, "little") + distance.to_bytes(4, "little")
                  + bytes([0xFF, direction | (source << 2) | 0xE0]))
            for speed, distance, direction, source in values])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "actual common reader required")
    def test_machine_selected_speed_native_to_common(self):
        text = self.speed_capture([(1234, 123456, 0, 0), (0, 0, 1, 4), (64255, 4211081215, 1, 3),
                                   (65535, 4294967295, 3, 7)])
        converted, report = module.convert(text, machine_selected_speed=True)
        self.assertEqual(report["machine_selected_speed_messages"], 4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(converted)
            common = json.loads(subprocess.run(
                [READER, str(PROFILE), str(path), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 4)
        a, b, c, missing = [o["fields"] for o in common["observations"]]
        self.assertIsNone(missing.get("machine_speed_m_s"))
        self.assertIsNone(missing.get("machine_distance_m"))
        self.assertEqual(missing["machine_speed_raw_mm_s"], 65535)
        self.assertEqual(missing["machine_speed_source_reported"], "NOT_AVAILABLE")
        self.assertIn("PASSIVE_SAVED_MACHINE_SELECTED_SPEED_REPORT_ONLY", a["isobus_scope_basis"])
        self.assertIn("MACHINE_SELECTED_SPEED_ISO11783_7", a["isobus_protocol_role"])
        self.assertEqual((a["machine_speed_m_s"], a["machine_distance_m"]), (1.234, 123.456))
        self.assertEqual(a["machine_direction_reported"], "REVERSE")
        self.assertEqual((b["machine_speed_m_s"], b["machine_distance_m"]), (0, 0))
        self.assertEqual(b["machine_speed_source_reported"], "SIMULATED")
        self.assertEqual((c["machine_speed_m_s"], c["machine_distance_m"]), (64.255, 4211081.215))
        self.assertEqual([o["fields"]["isobus_capture_index"] for o in common["observations"]], [1, 2, 3, 4])
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        self.assertTrue(all(o["fields"]["source_document_sha256"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
                            for o in common["observations"]))
        self.assertIn("mm_per_second", common["profile_units"]["machine_speed_raw_mm_s"])

    def test_machine_selected_speed_missing_and_default_unchanged(self):
        text = self.speed_capture([(64256, 4211081216, 2, 5), (65535, 4294967295, 3, 7)])
        converted, _ = module.convert(text, machine_selected_speed=True)
        rows = list(csv.DictReader(io.StringIO(converted)))
        for row in rows:
            self.assertEqual(row["machine_speed_m_s"], "")
            self.assertEqual(row["machine_distance_m"], "")
            self.assertEqual(row["machine_speed_status"], "OUTSIDE_SOURCE_VALID_RANGE")
            self.assertEqual(row["machine_distance_status"], "OUTSIDE_SOURCE_VALID_RANGE")
        self.assertEqual(rows[0]["machine_direction_reported"], "ERROR")
        self.assertEqual(rows[0]["machine_speed_source_reported"], "RESERVED_5")
        self.assertEqual(rows[1]["machine_direction_reported"], "NOT_AVAILABLE")
        self.assertEqual(rows[1]["machine_speed_raw_mm_s"], "65535")
        legacy = "time_us,extended_id,data_hex\n1000000,18E6262A,fe261027401f40a3\n"
        csv_text, report = module.convert(legacy)
        self.assertEqual(hashlib.sha256(csv_text.encode()).hexdigest(),
                         "bc01ccf93eefc1c96884cb050c908b58af2fa1c6e3be01f3c7bfd1391325d1ec")
        self.assertEqual(hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
                         "0b202a689055f8aba7c493e2131caf280b8d4bccc3413406c166a60419c40e55")
        mixed, _ = module.convert(legacy + text.split("\n", 1)[1], machine_selected_speed=True)
        self.assertEqual(list(csv.DictReader(io.StringIO(mixed)))[0]["machine_speed_m_s"], "")

    def test_machine_selected_speed_guards_and_cli(self):
        text = self.speed_capture([(1000, 1000, 1, 1)])
        for flag in (1, None, "true"):
            with self.assertRaises(ValueError):
                module.convert(text, machine_selected_speed=flag)
        with self.assertRaisesRegex(ValueError, "no UT or TC"):
            module.convert(text)
        extra = "1700000000000000,0DF0222A,010001000000ffff\n1700000000000000,0CF0222A,01\n1700000000000000,0CFD432A,010001000000ffff\n"
        _, report = module.convert(text + extra, machine_selected_speed=True)
        self.assertEqual(report["machine_selected_speed_messages"], 1)
        self.assertEqual(len(report["short_selected_frames"]), 1)
        self.assertEqual(sum(report["unselected_pgns"].values()) + len(report["other_data_page_frames"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            path.write_text(text)
            out = Path(directory) / "out"
            done = subprocess.run([os.sys.executable, str(SCRIPT), str(path), str(out),
                                   "--machine-selected-speed"], capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads((out / "report.json").read_text())["machine_selected_speed_messages"], 1)

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
        # The two roles stay apart: each row says which protocol it belongs to.
        self.assertTrue(terminal["isobus_protocol_role"].startswith("UNIVERSAL_TERMINAL"))
        self.assertTrue(controller["isobus_protocol_role"].startswith("TASK_CONTROLLER"))
        # UT: the VT Status fields, exactly as the pinned VT client reads them.
        self.assertEqual(terminal["isobus_message_kind"], "VT_STATUS")
        self.assertEqual(terminal["isobus_pgn"], str(0xE600))
        self.assertEqual(terminal["isobus_source_address"], "42")
        self.assertEqual(terminal["isobus_destination_address"], "38")
        self.assertEqual(terminal["ut_working_set_master_address"], "38")
        self.assertEqual(terminal["ut_data_mask_object_id"], "10000")
        self.assertEqual(terminal["ut_soft_key_mask_object_id"], "8000")
        self.assertEqual(terminal["ut_busy_codes_bitfield"], "64")
        # The one busy bit the pin defines, and nothing else claimed about that byte.
        self.assertEqual(terminal["ut_auxiliary_input_learn_mode_reported"], "1")
        self.assertEqual(terminal["ut_current_command_function_code"], str(0xA3))
        self.assertEqual(terminal["tc_status_bitfield"], "")
        # TC status: only the three bytes the pinned source says matter.
        self.assertEqual(controller["isobus_message_kind"], "TC_STATUS")
        self.assertEqual(controller["isobus_pgn"], str(0xCB00))
        self.assertEqual(controller["tc_status_bitfield"], "1")
        self.assertEqual(controller["tc_source_address_of_command_being_executed"], "38")
        self.assertEqual(controller["tc_command_being_executed"], "3")
        self.assertEqual(controller["tc_ddi"], "")
        self.assertIn("UNDEFINED_IN_VERSION_2", report["tc_status_semantics"])
        # TC value: element, DDI, the raw signed wire value and the pinned DDI's own unit.
        self.assertEqual(value["isobus_message_kind"], "TC_VALUE")
        self.assertEqual(value["tc_element_number"], "291")
        self.assertEqual(value["tc_ddi"], "7")
        self.assertEqual(value["tc_process_data_value_reported"], "2500000")
        # The pinned dictionary entry gives it a unit; the raw wire integer stays beside it.
        self.assertEqual(value["tc_ddi_quantity"], "actual_mass_rate_kg_m2")
        self.assertEqual(float(value["tc_ddi_scaled_value"]), 2.5)
        # Retention: identifier plus all eight bytes stay recoverable beside the interpreted columns.
        self.assertEqual(bytes.fromhex(value["source_record_hex"][4:])[4:],
                         tc_value(element=0x123, ddi=7, value=2_500_000))
        self.assertEqual(report["vt_status_messages"], 1)
        self.assertEqual(report["tc_status_messages"], 1)
        self.assertEqual(report["tc_value_messages"], 1)
        # Scope is carried in the output itself, not only in prose.
        for text in (value["isobus_scope_basis"], report["scope"]):
            self.assertIn("NO_OBJECT_POOL_IS_LOADED", text)
            self.assertIn("NOT_AEF_CERTIFICATION", text)

    def test_unknown_and_recognised_but_uninterpreted_messages_are_counted_not_dropped(self):
        # A VT function the pin names but this path does not decode, a VT function the pin does not
        # define at all, a TC command the pin names but this path does not decode, and an unselected PGN.
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
        # Every one of them still produced a row with its bytes retained: nothing is silently dropped.
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["source_record_hex"].startswith("hex:") for row in rows))
        self.assertEqual(report["uninterpreted_vt_functions"],
                         {"BUTTON_ACTIVATION": 1, "code_0x7b": 1})
        self.assertEqual(report["uninterpreted_tc_commands"], {"TECHNICAL_CAPABILITIES": 1})
        # The unselected PGN is accounted separately, with its number, and never guessed at. This one is
        # a PDU2 (broadcast) group, so by the PDU rule its group-extension byte belongs to the PGN
        # itself rather than being a destination: 0xFE00 with 0x26 in that byte is PGN 0xFE26.
        self.assertEqual(report["unselected_pgns"], {"0x0fe26": 1})
        self.assertEqual(report["capture_rows"], 5)
        self.assertEqual(report["selected_rows"], 4)

    def test_wrong_input_and_truncation_are_refused_or_counted_by_name(self):
        # A capture that is not the adopted converter's output is refused, not guessed at.
        for text in ("", "time_us,standard_id,data_hex\n1,123,00\n",
                     "time_us,extended_id,data_hex\nnot-a-row\n"):
            with self.subTest(text=text[:24]), self.assertRaises(ValueError):
                module.convert(text)
        # A selected PGN carrying fewer than eight bytes is counted and refused for that row -- never
        # zero-filled into a status that was not sent.
        truncated = self.capture_csv([
            frame(1_700_000_000_000_000, identifier(0xE600, 0x26, 0x2A), vt_status()[:5]),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0x26, 0xF0), tc_status()),
        ])
        rows, report, _ = self.rows(truncated)
        self.assertEqual([row["isobus_message_kind"] for row in rows], ["TC_STATUS"])
        self.assertEqual(report["short_selected_frames"],
                         [{"row": 1, "bytes": 5, "pgn": "0x0e600"}])
        # A capture with no UT or TC message at all is refused rather than returned empty.
        with self.assertRaises(ValueError):
            self.rows(self.capture_csv([
                frame(1_700_000_000_000_000, identifier(0xFE00, 0x26, 0x2A), bytes(8))]))
        # Decreasing capture time is refused rather than reordered.
        with self.assertRaises(ValueError):
            module.convert("time_us,extended_id,data_hex\n"
                           "20,18E6262A,FE260000000000A3\n"
                           "10,18E6262A,FE260000000000A3\n")

    def test_a_reported_value_reaches_its_declared_quantity_or_state(self):
        """The finite DDI set the adopted ISOXML converter pins, reused rather than re-declared."""
        rows, report, _ = self.rows(self.capture_csv([
            # DDI 7, actual mass rate: the adopted scale is 1e-6, so 2 500 000 is 2.5 kg/m^2.
            frame(1_700_000_000_000_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=7, value=2_500_000)),
            # DDI 141, actual work state: 1 is ENABLED in the adopted four-state vocabulary.
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=141, value=1)),
            # DDI 161: sixteen two-bit child states; 0b01 for child 1 and 0b10 for child 2.
            frame(1_700_000_000_020_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=2, ddi=161, value=0b1001)),
            # DDI 262, crop moisture in ppm: 250 000 ppm is the 0.25 fraction the adopted rule allows.
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
        # The label names what the scaled column holds -- the fraction -- while the raw column keeps ppm.
        self.assertEqual(moisture["tc_ddi_quantity"], "reported_average_crop_moisture_fraction")
        self.assertEqual(moisture["tc_process_data_value_reported"], "250000")
        self.assertIn("DDI_262_AT_ADOPTED_REVISION_3_UNIT_1_PPM", moisture["tc_ddi_meaning_basis"])
        self.assertIn("STAYS_THE_REPORTED_PARTS_PER_MILLION", moisture["tc_ddi_meaning_basis"])
        # Per-entry revisions, not one blanket revision.
        self.assertIn("DDI_7_AT_ADOPTED_REVISION_1", rate["tc_ddi_meaning_basis"])
        self.assertIn("DDI_141_AT_ADOPTED_REVISION_1_OF_2012_04_02", state["tc_ddi_meaning_basis"])
        # The meaning's provenance and its limits travel in the row itself.
        for row in rows:
            self.assertIn("DICTIONARY_POINTERS_ARE_PER_ENTRY", row["tc_ddi_meaning_basis"])
            self.assertIn("NO_DDOP_IDENTITY_AND_NO_ACTUAL_DEVICE_PROOF", row["tc_ddi_meaning_basis"])
        self.assertEqual(report["tc_value_messages"], 4)
        self.assertEqual(report["out_of_range_values"], {})
        self.assertEqual(report["unqualified_ddis"], {})

    def test_a_request_is_not_a_reported_measurement(self):
        rows, report, _ = self.rows(self.capture_csv([
            # A request for DDI 7 from the controller, with the 0xFF filler the pinned server writes.
            frame(1_700_000_000_000_000, identifier(0xCB00, 0x26, 0xF0),
                  bytes([0x02 | (1 << 4), 0x00, 7, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])),
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=7, value=2_500_000)),
        ]))
        request, value = rows
        self.assertEqual(request["isobus_message_kind"], "TC_REQUEST_VALUE")
        # The requested entity is named, because the request says which one it asks about.
        self.assertEqual(request["tc_ddi"], "7")
        self.assertEqual(request["tc_element_number"], "1")
        # But no measurement is claimed: no reported value, no quantity, no scale, no state.
        self.assertEqual(request["tc_process_data_value_reported"], "")
        self.assertEqual(request["tc_ddi_quantity"], "")
        self.assertEqual(request["tc_ddi_scaled_value"], "")
        self.assertEqual(request["tc_reported_work_state"], "")
        self.assertEqual(request["tc_ddi_value_disposition"], "REQUEST_NOT_A_REPORTED_MEASUREMENT")
        self.assertIn("A_REQUEST_VALUE_MESSAGE_IS_A_REQUEST_AND_NOT_A_REPORTED_MEASUREMENT",
                      request["tc_ddi_meaning_basis"])
        # The payload is still retained, exactly as it arrived, as a raw signed integer.
        self.assertEqual(request["tc_request_value_payload_raw"], "-1")
        # And it is counted apart from reported values, so a request can never inflate a value count.
        self.assertEqual((report["tc_request_value_messages"], report["tc_value_messages"]), (1, 1))
        self.assertEqual(value["tc_request_value_payload_raw"], "")

    def test_a_selected_pf_on_another_data_page_is_refused_and_accounted(self):
        # Same PF as each selected group but with the data-page bit set: a different parameter group.
        # It must not be read as UT or TC traffic, and it must still be accounted with its bytes.
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
            # A DDI this repository pins no dictionary entry for: raw only, never invented.
            frame(1_700_000_000_000_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=999, value=1234)),
            # DDI 84 negative: the adopted converter assumes no sentinel, so it is not made a quantity.
            frame(1_700_000_000_010_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=84, value=-5)),
            # DDI 262 above the published fraction range: kept whole, never clamped.
            frame(1_700_000_000_020_000, identifier(0xCB00, 0xF0, 0x26),
                  tc_value(element=1, ddi=262, value=1_500_000)),
            # DDI 141 outside its published 0..3 range: no state named.
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
        # With the fraction withheld, the label names the ppm report that is actually present.
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
        # A terminal and a task controller are control functions, not a declared platform.
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
        # The label names the fraction, and the scaled value IS a 0..1 fraction.
        self.assertEqual(fields["tc_ddi_quantity"], "reported_average_crop_moisture_fraction")
        self.assertEqual(fields["tc_ddi_scaled_value"], 0.25)
        # The raw column stays the ppm wire value, so the two are not confused for one another.
        self.assertEqual(fields["tc_process_data_value_reported"], 250000)
        self.assertEqual(fields["tc_ddi_value_disposition"], "REPORTED")
        # And the profile's own declarations say the same thing about that pairing.
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
