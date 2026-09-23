#!/usr/bin/env python3
"""Captured ISOBUS Universal-Terminal / Task-Controller messages -> common Observation CSV.

PASSIVE READ ONLY. This reads a capture that already exists on disk. It has no socket, sends no CAN
frame, issues no command, controls no implement, and **never uploads or loads an object pool**. A
complete, ACTIVE UT or TC capability necessarily writes to the bus -- a VT client must upload an object
pool and answer terminal requests, a TC client must answer process-data requests and acknowledge -- and
that is deliberately OUTSIDE this path. Nothing here is AEF certification, conformance or compliance:
that is a test-house process this repo does not perform and does not assert.

Input is the classic CAN CSV the adopted `scripts/convert_candump_recorded.py` already emits from a
saved can-utils log (`time_us,extended_id,data_hex`), so framing, capture clock and per-line accounting
stay with that adopted step and are not reimplemented here.

Two roles are kept apart, never merged:

* **UT (Universal Terminal / Virtual Terminal, ISO 11783-6)** -- PGN `0xE600` "VirtualTerminalToECU".
  Byte 0 is the VT function code; `0xFE` is the VT Status message.
* **TC (Task Controller, ISO 11783-10)** -- PGN `0xCB00` "ProcessData". The low nibble of byte 0 is the
  process-data command; `0x0E` is the Task Controller Status message, `0x03` a Value, `0x02` a request.

Every field meaning below is taken from AgIsoStack++ (MIT) at pin
`1bd6189972ef148273b31a27b281b86c91edc3d4`:
  isobus/include/isobus/isobus/can_general_parameter_group_numbers.hpp   (the two PGNs)
  isobus/include/isobus/isobus/isobus_virtual_terminal_base.hpp          (VT `Function` enum, 0xFE)
  isobus/src/isobus_virtual_terminal_client.cpp                          (VT Status byte fields)
  isobus/include/isobus/isobus/isobus_task_controller_client.hpp         (`ProcessDataCommands`)
  isobus/src/isobus_task_controller_client.cpp                           (TC Status / Value byte fields)
  isobus/src/isobus_task_controller_server.cpp                           (producer side: status filler
                                                                          bytes, and 0xFF in a request)
No PGN, byte offset, bit or enumeration is guessed from a name, and a code the pin does not define is
carried as a reported number with its bytes retained -- never given an invented meaning.

DDI meanings for one finite set come from the adopted `scripts/convert_isoxml_rate.py`, IMPORTED at run
time rather than restated, so the bus path and the saved-task-file path cannot drift. Its dictionary
pointers are PER ENTRY -- DDI 262 is revision 3, the others revision 1 -- and no single revision is
claimed for the whole set. A DDI outside the set is simply NOT QUALIFIED BY THIS ADAPTER, which says
nothing about whether an entry for it exists elsewhere. A dictionary entry fixes a unit and resolution
only: it proves no device-descriptor identity and no actual device.
"""
import argparse
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys

# The DDI meanings are not restated here. They are IMPORTED from the adopted ISOXML converter, so this
# path and `profiles/declared/isoxml-rate/` use one table, one set of scale factors and
# one state vocabulary rather than two copies that could drift. That file is read, never modified.
ISOXML_PATH = Path(__file__).resolve().parent / "convert_isoxml_rate.py"
_spec = importlib.util.spec_from_file_location("musubi_adopted_isoxml_rate", ISOXML_PATH)
_isoxml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_isoxml)

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_ROWS = 200000
# can_general_parameter_group_numbers.hpp: VirtualTerminalToECU = 0xE600, ProcessData = 0xCB00.
VT_TO_ECU_PGN = 0xE600
PROCESS_DATA_PGN = 0xCB00
# isobus_virtual_terminal_base.hpp `enum class Function`: VTStatusMessage = 0xFE. Other codes are
# named for accounting only; this path interprets no VT function but the status message.
VT_STATUS_FUNCTION = 0xFE
VT_FUNCTION_NAMES = {0x00: "SOFT_KEY_ACTIVATION", 0x01: "BUTTON_ACTIVATION", 0x02: "POINTING_EVENT",
                     0x03: "VT_SELECT_INPUT_OBJECT", 0x04: "VT_ESC", 0x05: "VT_CHANGE_NUMERIC_VALUE",
                     0x06: "VT_CHANGE_ACTIVE_MASK", 0x07: "VT_CHANGE_SOFT_KEY_MASK",
                     0x08: "VT_CHANGE_STRING_VALUE", 0x09: "VT_ON_USER_LAYOUT_HIDE_SHOW",
                     0x0A: "VT_CONTROL_AUDIO_SIGNAL_TERMINATION", 0x11: "OBJECT_POOL_TRANSFER",
                     0x12: "END_OF_OBJECT_POOL", 0x21: "AUXILIARY_INPUT_TYPE_ONE_STATUS",
                     0x26: "AUXILIARY_INPUT_TYPE_TWO_STATUS", 0xFE: "VT_STATUS"}
# isobus_task_controller_client.hpp `enum class ProcessDataCommands`.
TC_COMMAND_NAMES = {0x00: "TECHNICAL_CAPABILITIES", 0x01: "DEVICE_DESCRIPTOR", 0x02: "REQUEST_VALUE",
                    0x03: "VALUE", 0x04: "MEASUREMENT_TIME_INTERVAL",
                    0x05: "MEASUREMENT_DISTANCE_INTERVAL", 0x06: "MEASUREMENT_MINIMUM_THRESHOLD",
                    0x07: "MEASUREMENT_MAXIMUM_THRESHOLD", 0x08: "MEASUREMENT_CHANGE_THRESHOLD",
                    0x09: "PEER_CONTROL_ASSIGNMENT", 0x0A: "SET_VALUE_AND_ACKNOWLEDGE",
                    0x0B: "RESERVED_1", 0x0C: "RESERVED_2", 0x0D: "PROCESS_DATA_ACKNOWLEDGE",
                    0x0E: "TC_STATUS", 0x0F: "CLIENT_TASK"}
TC_STATUS_COMMAND = 0x0E
TC_VALUE_COMMANDS = {0x02: "TC_REQUEST_VALUE", 0x03: "TC_VALUE"}
TC_REQUEST_VALUE_COMMAND = 0x02
# The one busy-code bit the pin defines: VirtualTerminalClient::get_auxiliary_input_learn_mode_enabled()
# returns `0x40 == (busyCodesBitfield & 0x40)`. No other bit of that byte is defined in the pinned
# source, so no other bit is named here.
VT_BUSY_AUX_LEARN_MODE_BIT = 0x40
CLOCK_BASIS = ("CAPTURE_REPORTED_TIME_CARRIED_THROUGH_FROM_THE_ADOPTED_CANDUMP_CONVERSION_IT_IS_THE_"
               "CAPTURING_HOSTS_OWN_STAMP_AND_NOT_A_BUS_CLOCK_NOT_SYNCHRONISED_AND_NOT_AUTHENTICATED")
IDENTITY_BASIS = ("THE_SOURCE_AND_DESTINATION_ADDRESSES_ARE_THE_29_BIT_IDENTIFIERS_OWN_BYTES_A_REPORTED_"
                  "BUS_ADDRESS_IS_NOT_A_DEVICE_IDENTITY_AND_IS_NOT_A_NAME_NO_ADDRESS_CLAIM_IS_READ_ON_THIS_"
                  "PATH_SO_NO_MANUFACTURER_OR_FUNCTION_INSTANCE_IS_RESOLVED")
SEMANTIC_BASIS = ("FIELD_MEANINGS_COME_ONLY_FROM_THE_PINNED_AGISOSTACK_PLUS_PLUS_SOURCE_AT_"
                  "1bd6189972ef148273b31a27b281b86c91edc3d4_THE_VT_STATUS_BYTES_FROM_THE_VT_CLIENT_AND_THE_"
                  "TC_STATUS_AND_VALUE_BYTES_FROM_THE_TC_CLIENT_A_FUNCTION_OR_COMMAND_CODE_THAT_PIN_DOES_"
                  "NOT_DEFINE_IS_REPORTED_AS_A_NUMBER_WITH_ITS_BYTES_RETAINED_AND_NEVER_INTERPRETED")
SCOPE_BASIS = ("PASSIVE_CAPTURE_READING_ONLY_NO_CAN_FRAME_IS_SENT_NO_COMMAND_IS_ISSUED_NO_IMPLEMENT_IS_"
               "CONTROLLED_AND_NO_OBJECT_POOL_IS_LOADED_A_COMPLETE_ACTIVE_UT_OR_TC_CAPABILITY_WOULD_"
               "REQUIRE_WRITING_TO_THE_BUS_AND_IS_OUTSIDE_THIS_PATH_THIS_IS_NOT_AEF_CERTIFICATION_"
               "CONFORMANCE_OR_COMPLIANCE_AND_NONE_IS_CLAIMED")
# Scaled quantities: the adopted converter's own tables, imported rather than restated. `ACTUAL_RATES`
# gives DDI 2/7/12 and `HARVEST` gives DDI 84/87/90, each as (column name, scale to the named unit).
SCALED_DDI = {**_isoxml.ACTUAL_RATES, **_isoxml.HARVEST}
# Reported states: DDI 141 is the whole value, DDI 161/162/163 are sixteen two-bit child states each,
# with the first child ordinal the adopted converter computes as (ddi - 161) * 16 + 1.
WORK_STATE_DDI = 141
CHILD_STATE_DDIS = (161, 162, 163)
STATE_NAMES = _isoxml.STATE_NAMES
# DDI 262 is a parts-per-million report whose published wire range exceeds a physical fraction; the
# adopted converter keeps the report and refuses to clamp it, and that rule is reused verbatim.
MOISTURE_DDI = 262
MOISTURE_PPM_LIMIT = 1000000
# Dictionary revisions are PER ENTRY, not one blanket revision: the adopted case's README attributes
# DDI 141 and 161/162/163 to revision 1 of 2012-04-02, DDI 2, 7, 12, 84, 87 and 90 to revision 1, and
# DDI 262 to REVISION 3 (1 ppm). Those pointers are reused as written there; nothing is re-derived.
DDI_ADOPTED_REVISION = {2: "REVISION_1", 7: "REVISION_1", 12: "REVISION_1", 84: "REVISION_1",
                        87: "REVISION_1", 90: "REVISION_1", 262: "REVISION_3_UNIT_1_PPM",
                        141: "REVISION_1_OF_2012_04_02", 161: "REVISION_1_OF_2012_04_02",
                        162: "REVISION_1_OF_2012_04_02", 163: "REVISION_1_OF_2012_04_02"}
DDI_MEANING_BASIS = ("DDI_MEANINGS_UNITS_SCALES_AND_STATE_VOCABULARY_ARE_IMPORTED_FROM_THE_ADOPTED_"
                     "scripts_convert_isoxml_rate_py_AND_ITS_CASE_README_WHOSE_DICTIONARY_POINTERS_ARE_"
                     "PER_ENTRY_DDI_141_AND_161_162_163_REVISION_1_OF_2012_04_02_DDI_2_7_12_84_87_AND_"
                     "90_REVISION_1_AND_DDI_262_REVISION_3_WITH_A_1_PPM_UNIT_NO_SINGLE_REVISION_IS_"
                     "CLAIMED_FOR_THE_WHOLE_SET_A_DDI_OUTSIDE_THIS_SET_IS_NOT_QUALIFIED_BY_THIS_ADAPTER_"
                     "WHICH_IS_A_STATEMENT_ABOUT_THIS_ADAPTER_AND_NOT_A_CLAIM_THAT_NO_DICTIONARY_ENTRY_"
                     "FOR_IT_EXISTS_ANYWHERE_A_DICTIONARY_ENTRY_FIXES_THE_UNIT_AND_RESOLUTION_OF_A_"
                     "PROCESS_DATA_ENTITY_AND_SAYS_NOTHING_ABOUT_WHICH_PHYSICAL_DEVICE_ELEMENT_REPORTED_"
                     "IT_SO_NO_DDOP_IDENTITY_AND_NO_ACTUAL_DEVICE_PROOF_IS_CLAIMED_HERE")
MOISTURE_SCALE_BASIS = ("_FOR_DDI_262_THE_RAW_tc_process_data_value_reported_STAYS_THE_REPORTED_PARTS_PER_"
                        "MILLION_WIRE_VALUE_WHILE_tc_ddi_scaled_value_IS_THE_DERIVED_0_TO_1_FRACTION_SO_"
                        "tc_ddi_quantity_NAMES_THE_FRACTION_WHEN_ONE_IS_EMITTED_AND_NAMES_THE_PPM_REPORT_"
                        "WHEN_THE_FRACTION_IS_WITHHELD")


def meaning_basis_for(ddi):
    """The shared basis plus this row's own entry qualification, so a row states its own revision."""
    revision = DDI_ADOPTED_REVISION.get(ddi)
    text = DDI_MEANING_BASIS + (f"_THIS_ROW_USES_DDI_{ddi}_AT_ADOPTED_{revision}" if revision else
                                f"_THIS_ROW_USES_DDI_{ddi}_WHICH_THIS_ADAPTER_DOES_NOT_QUALIFY")
    return text + MOISTURE_SCALE_BASIS if ddi == MOISTURE_DDI else text
REQUEST_PAYLOAD_BASIS = ("A_REQUEST_VALUE_MESSAGE_IS_A_REQUEST_AND_NOT_A_REPORTED_MEASUREMENT_THE_PINNED_"
                         "TASK_CONTROLLER_SERVER_WRITES_0xFF_INTO_BYTES_4_TO_7_WHEN_IT_SENDS_ONE_AND_THE_"
                         "PINNED_CLIENT_ANSWERS_IT_FROM_A_FRESH_ZERO_INITIALISED_VALUE_SO_THE_PAYLOAD_IS_"
                         "RETAINED_RAW_AND_IS_NEVER_SCALED_NEVER_GIVEN_A_DDI_UNIT_AND_NEVER_COUNTED_AS_AN_"
                         "OBSERVED_QUANTITY_OR_STATE")
TC_STATUS_BASIS = ("THE_PINNED_TC_CLIENT_STATES_THAT_MANY_VALUES_IN_THE_STATUS_MESSAGE_WERE_UNDEFINED_IN_"
                   "VERSION_2_AND_BEFORE_AND_THAT_THE_STANDARD_TELLS_IMPLEMENTERS_TO_IGNORE_THOSE_"
                   "ATTRIBUTES_SO_ONLY_THE_STATUS_BITFIELD_THE_SOURCE_ADDRESS_OF_THE_COMMAND_BEING_"
                   "EXECUTED_AND_THE_COMMAND_BEING_EXECUTED_ARE_INTERPRETED_HERE_AND_THE_REMAINING_BYTES_"
                   "TRAVEL_ONLY_INSIDE_source_record_hex_THE_SAME_PIN_CONFIRMS_THIS_FROM_THE_PRODUCER_"
                   "SIDE_TaskControllerServer_send_status_message_COMPOSES_BYTE_0_AS_THE_STATUS_COMMAND_"
                   "WITH_0xF0_BYTES_1_TO_3_AND_BYTE_7_AS_0xFF_FILLER_AND_BYTES_4_5_6_AS_THE_STATUS_BYTE_"
                   "THE_COMMAND_SOURCE_ADDRESS_AND_THE_COMMAND_BYTE_SO_THE_THREE_FIELDS_DECODED_HERE_ARE_"
                   "THE_ONES_THAT_PRODUCER_ACTUALLY_FILLS_AND_NO_VERSION_DEPENDENT_ATTRIBUTE_IS_"
                   "INTERPRETED_NO_INDIVIDUAL_BIT_OF_THE_STATUS_BYTE_IS_NAMED_BY_THE_PIN_SO_NONE_IS_"
                   "NAMED_HERE")
FIELDS = ["record_time_us", "isobus_capture_index", "isobus_protocol_role", "isobus_message_kind",
          "isobus_pgn", "isobus_source_address", "isobus_destination_address",
          "isobus_function_or_command_code",
          "ut_working_set_master_address", "ut_data_mask_object_id", "ut_soft_key_mask_object_id",
          "ut_busy_codes_bitfield", "ut_auxiliary_input_learn_mode_reported",
          "ut_current_command_function_code",
          "tc_status_bitfield", "tc_source_address_of_command_being_executed",
          "tc_command_being_executed", "tc_element_number", "tc_ddi",
          "tc_process_data_value_reported", "tc_request_value_payload_raw",
          "tc_ddi_quantity", "tc_ddi_scaled_value", "tc_ddi_value_disposition",
          "tc_reported_work_state", "tc_child_state_first_ordinal", "tc_child_states_named",
          "tc_ddi_meaning_basis",
          "isobus_clock_basis", "isobus_identity_basis", "isobus_semantic_basis",
          "isobus_scope_basis", "source_record_hex", "source_document_sha256"]
HEADER = "time_us,extended_id,data_hex"
ROW = re.compile(r"(?P<time>[0-9]{1,19}),(?P<identifier>[0-9A-Fa-f]{8}),(?P<data>(?:[0-9A-Fa-f]{2})*)$")


def pgn_of(identifier):
    """PGN, destination and source of a 29-bit identifier, by the J1939/ISO 11783 PDU rule.

    The PGN is the COMPLETE 18-bit field, page bits included: bit 25 is the extended data page and bit
    24 the data page, then the PF byte, then -- only when PF >= 240 (PDU2, broadcast) -- the group
    extension byte. Below 240 the PDU is destination specific (PDU1), that byte is the destination
    address, and the PGN's low byte is zero.

    Taking the page bits is the adopted precedent, not a new convention:
    `scripts/convert_dronecan_capture.py` reads `(identifier >> 8) & 0x3ffff`, an 18-bit field that
    already spans both page bits. Dropping them would let a message on a different data page share a
    PGN with a selected one, so both selected groups below are page-0 values and are compared as such.
    """
    pf = (identifier >> 16) & 0xFF
    page = (identifier >> 24) & 0x03
    source = identifier & 0xFF
    if pf < 240:
        return (page << 16) | (pf << 8), (identifier >> 8) & 0xFF, source
    return (page << 16) | (pf << 8) | ((identifier >> 8) & 0xFF), None, source


def decode_vt(data, row, report):
    """The UT role. Only the VT Status message is interpreted; other functions are named and retained."""
    function = data[0]
    row["isobus_function_or_command_code"] = function
    name = VT_FUNCTION_NAMES.get(function)
    if function != VT_STATUS_FUNCTION:
        row["isobus_message_kind"] = f"VT_FUNCTION_{name}_NOT_INTERPRETED_HERE" if name else \
            "VT_FUNCTION_UNDEFINED_BY_THE_PIN"
        key = name or f"code_{function:#04x}"
        report["uninterpreted_vt_functions"][key] = report["uninterpreted_vt_functions"].get(key, 0) + 1
        return
    # isobus_virtual_terminal_client.cpp, Function::VTStatusMessage: byte 1 is the active working set
    # master address, bytes 2-3 and 4-5 the active data and soft key mask object IDs, byte 6 the busy
    # codes bitfield and byte 7 the current command function code.
    row["isobus_message_kind"] = "VT_STATUS"
    row["ut_working_set_master_address"] = data[1]
    row["ut_data_mask_object_id"] = data[2] | (data[3] << 8)
    row["ut_soft_key_mask_object_id"] = data[4] | (data[5] << 8)
    row["ut_busy_codes_bitfield"] = data[6]
    row["ut_auxiliary_input_learn_mode_reported"] = 1 if data[6] & VT_BUSY_AUX_LEARN_MODE_BIT else 0
    row["ut_current_command_function_code"] = data[7]
    report["vt_status_messages"] += 1


def apply_ddi_meaning(ddi, value, raw, row, report):
    """Give a reported Value its declared quantity or state, for the pinned DDI set only.

    Every meaning here comes from the adopted ISOXML converter's own tables, so the bus path and the
    saved-task-file path agree by construction. Anything outside that set stays a raw signed integer
    with its DDI number, because no dictionary entry for it is pinned in this repository.

    One rule differs from the file path on purpose, and it is a scope difference rather than a
    semantic one: the ISOXML converter REFUSES the whole document when a selected DDI carries a value
    outside its published range. A capture is a stream of independent frames from an untrusted bus, so
    refusing everything on one odd frame would discard the rest of the evidence. Here the frame is kept
    with its raw value, no scaled quantity is produced, and the disposition says exactly that.
    """
    if ddi == WORK_STATE_DDI:
        if 0 <= value < len(STATE_NAMES):
            row["tc_reported_work_state"] = STATE_NAMES[value]
            row["tc_ddi_quantity"] = "reported_work_state"
            row["tc_ddi_value_disposition"] = "REPORTED"
        else:
            row["tc_ddi_value_disposition"] = "OUTSIDE_PUBLISHED_RANGE_RETAINED_RAW"
            report["out_of_range_values"][str(ddi)] = report["out_of_range_values"].get(str(ddi), 0) + 1
        return
    if ddi in CHILD_STATE_DDIS:
        # These DDIs define the full 32 bits as sixteen two-bit child states, so the signed wire value
        # is read as its bit pattern -- the adopted converter does exactly this.
        row["tc_child_state_first_ordinal"] = (ddi - 161) * 16 + 1
        row["tc_child_states_named"] = "|".join(STATE_NAMES[(raw >> (2 * index)) & 3] for index in range(16))
        row["tc_ddi_quantity"] = "reported_condensed_child_states"
        row["tc_ddi_value_disposition"] = "REPORTED"
        return
    if ddi == MOISTURE_DDI:
        if 0 <= value <= MOISTURE_PPM_LIMIT:
            # The adopted converter carries both: the ppm report and the derived fraction. The raw
            # column keeps the ppm wire value, so the quantity label names the SCALED column -- the
            # fraction -- using that converter's own name for it.
            row["tc_ddi_quantity"] = "reported_average_crop_moisture_fraction"
            row["tc_ddi_scaled_value"] = value / MOISTURE_PPM_LIMIT
            row["tc_ddi_value_disposition"] = "REPORTED"
        else:
            # The fraction is withheld, so the label names the ppm report that is actually present.
            row["tc_ddi_quantity"] = "reported_average_crop_moisture_ppm"
            # The adopted converter's wording: the published wire range exceeds a physical fraction, so
            # the report is preserved and never clamped into a plausible value.
            row["tc_ddi_value_disposition"] = "OUTSIDE_FRACTION_RANGE"
            report["out_of_range_values"][str(ddi)] = report["out_of_range_values"].get(str(ddi), 0) + 1
        return
    if ddi in SCALED_DDI:
        name, scale = SCALED_DDI[ddi]
        row["tc_ddi_quantity"] = name
        if value < 0:
            # Same rule as the adopted converter: no sentinel is assumed for these DDIs, so a negative
            # report is not turned into a quantity.
            row["tc_ddi_value_disposition"] = "OUTSIDE_PUBLISHED_RANGE_RETAINED_RAW"
            report["out_of_range_values"][str(ddi)] = report["out_of_range_values"].get(str(ddi), 0) + 1
        else:
            row["tc_ddi_scaled_value"] = value * scale
            row["tc_ddi_value_disposition"] = "REPORTED"
        return
    row["tc_ddi_value_disposition"] = "NOT_QUALIFIED_BY_THIS_ADAPTER_RAW_ONLY"
    report["unqualified_ddis"][str(ddi)] = report["unqualified_ddis"].get(str(ddi), 0) + 1


def decode_tc(data, row, report):
    """The TC role. The status message and the value messages are interpreted; other commands are named."""
    command = data[0] & 0x0F
    row["isobus_function_or_command_code"] = command
    name = TC_COMMAND_NAMES.get(command)
    if command == TC_STATUS_COMMAND:
        # isobus_task_controller_client.cpp, ProcessDataCommands::StatusMessage: the status bitfield,
        # the source address of the command being executed, and the command being executed. The same
        # source says the remaining attributes were undefined in version 2 and before.
        row["isobus_message_kind"] = "TC_STATUS"
        row["tc_status_bitfield"] = data[4]
        row["tc_source_address_of_command_being_executed"] = data[5]
        row["tc_command_being_executed"] = data[6]
        report["tc_status_messages"] += 1
        return
    if command in TC_VALUE_COMMANDS:
        # Same file, ProcessDataCommands::Value and RequestValue: a 12-bit element number split across
        # the high nibble of byte 0 and byte 1, a 16-bit DDI in bytes 2-3, and a SIGNED 32-bit process
        # data value in bytes 4-7, all little endian. The element number is a DEVICE ELEMENT LOCAL
        # reference into a device descriptor this path does not hold, never a physical part.
        row["isobus_message_kind"] = TC_VALUE_COMMANDS[command]
        row["tc_element_number"] = (data[0] >> 4) | (data[1] << 4)
        ddi = data[2] | (data[3] << 8)
        row["tc_ddi"] = ddi
        raw = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)
        value = raw - (1 << 32) if raw & (1 << 31) else raw
        if command == TC_REQUEST_VALUE_COMMAND:
            # A request asks for a value; it does not report one. The pinned server fills these bytes
            # with 0xFF and the pinned client answers from a fresh zero, so the payload is retained raw
            # and given no quantity, unit or state.
            row["tc_request_value_payload_raw"] = value
            row["tc_ddi_value_disposition"] = "REQUEST_NOT_A_REPORTED_MEASUREMENT"
            row["tc_ddi_meaning_basis"] = REQUEST_PAYLOAD_BASIS
            report["tc_request_value_messages"] += 1
            return
        row["tc_process_data_value_reported"] = value
        row["tc_ddi_meaning_basis"] = meaning_basis_for(ddi)
        apply_ddi_meaning(ddi, value, raw, row, report)
        report["tc_value_messages"] += 1
        return
    row["isobus_message_kind"] = f"TC_COMMAND_{name}_NOT_INTERPRETED_HERE" if name else \
        "TC_COMMAND_UNDEFINED_BY_THE_PIN"
    key = name or f"code_{command:#04x}"
    report["uninterpreted_tc_commands"][key] = report["uninterpreted_tc_commands"].get(key, 0) + 1


def convert(text):
    """Selected UT/TC messages of a captured classic-CAN CSV; every other row is counted, not dropped."""
    raw = text.encode("utf-8")
    if not raw or len(raw) > MAX_INPUT:
        raise ValueError("empty or oversized capture CSV")
    lines = text.splitlines()
    if not lines or lines[0].strip() != HEADER:
        raise ValueError(f"this path reads the adopted extended-frame capture CSV whose header is {HEADER}")
    report = {"source_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
              "capture_rows": 0, "selected_rows": 0, "vt_status_messages": 0,
              "tc_status_messages": 0, "tc_value_messages": 0, "tc_request_value_messages": 0,
              "out_of_range_values": {}, "unqualified_ddis": {},
              "other_data_page_frames": [],
              "uninterpreted_vt_functions": {}, "uninterpreted_tc_commands": {},
              "unselected_pgns": {}, "short_selected_frames": [],
              "clock": "Unknown", "clock_basis": CLOCK_BASIS, "scope": SCOPE_BASIS,
              "semantics": SEMANTIC_BASIS, "tc_status_semantics": TC_STATUS_BASIS,
              "ddi_meaning": DDI_MEANING_BASIS, "request_payload": REQUEST_PAYLOAD_BASIS}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    previous = None
    for index, line in enumerate(lines[1:], 1):
        if not line:
            continue
        report["capture_rows"] += 1
        if report["capture_rows"] > MAX_ROWS:
            raise ValueError("capture exceeds the selected row bound")
        match = ROW.fullmatch(line)
        if match is None:
            raise ValueError(f"capture row {index} is not the adopted converter's output")
        timestamp = int(match["time"])
        identifier = int(match["identifier"], 16)
        if identifier > 0x1FFFFFFF:
            raise ValueError("capture row carries more than a 29 bit identifier")
        if timestamp >= 2**63 or (previous is not None and timestamp < previous):
            raise ValueError("out-of-range or decreasing capture time")
        previous = timestamp
        data = bytes.fromhex(match["data"])
        pgn, destination, source = pgn_of(identifier)
        if pgn not in (VT_TO_ECU_PGN, PROCESS_DATA_PGN):
            if (pgn & 0x0FF00) in (VT_TO_ECU_PGN, PROCESS_DATA_PGN) and pgn >> 16:
                # Same PF as a selected group but on another data page: a different parameter group
                # entirely. It is refused for decoding and accounted apart, never read as the selected
                # protocol, because this path pins only the page-0 groups.
                report["other_data_page_frames"].append(
                    {"row": index, "pgn": f"{pgn:#07x}", "data_page_bits": pgn >> 16})
                continue
            key = f"{pgn:#07x}"
            report["unselected_pgns"][key] = report["unselected_pgns"].get(key, 0) + 1
            continue
        if len(data) != 8:
            # Both selected messages are single-frame eight-byte messages in the pinned source. A
            # shorter selected frame is refused for this row and counted, never zero-filled.
            report["short_selected_frames"].append({"row": index, "bytes": len(data),
                                                    "pgn": f"{pgn:#07x}"})
            continue
        row = {name: "" for name in FIELDS}
        row.update(record_time_us=timestamp, isobus_capture_index=index, isobus_pgn=pgn,
                   isobus_source_address=source, isobus_destination_address=destination,
                   isobus_clock_basis=CLOCK_BASIS, isobus_identity_basis=IDENTITY_BASIS,
                   isobus_semantic_basis=SEMANTIC_BASIS, isobus_scope_basis=SCOPE_BASIS,
                   source_record_hex="hex:" + identifier.to_bytes(4, "big").hex() + data.hex(),
                   source_document_sha256=report["source_sha256"])
        if pgn == VT_TO_ECU_PGN:
            row["isobus_protocol_role"] = "UNIVERSAL_TERMINAL_ISO11783_6_VIRTUAL_TERMINAL"
            decode_vt(data, row, report)
        else:
            row["isobus_protocol_role"] = "TASK_CONTROLLER_ISO11783_10_PROCESS_DATA"
            decode_tc(data, row, report)
        writer.writerow(row)
        report["selected_rows"] += 1
        if output.tell() > MAX_OUTPUT:
            raise ValueError("converted capture exceeds output bound")
    if not report["selected_rows"]:
        raise ValueError("no UT or TC message was present in the selected capture")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path,
                        help="classic CAN CSV from scripts/convert_candump_recorded.py (extended)")
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        size = args.input.stat().st_size
        if size > MAX_INPUT:
            raise ValueError(f"capture CSV is {size} bytes, past the {MAX_INPUT} byte input bound")
        converted, report = convert(args.input.read_text(encoding="utf-8"))
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(converted, encoding="utf-8")
        (args.output_directory / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"captured ISOBUS UT/TC conversion failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
