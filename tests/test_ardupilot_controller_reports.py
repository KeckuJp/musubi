"""Authored ArduPilot ERR/PM reader reports; no vehicle, device, decoder run or real log.

Every input is written here from the field lists the pinned ArduPilot source declares
(`a64bad1a0da4382da9cd38a52dbf6379aa7b51b5`). The saved-report shape is the one the adopted
`scripts/normalize_ardupilot_report.py` already consumes, so this path reuses that contract rather than
inventing a second one. Real records 0.
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
SCRIPT = ROOT / "scripts/convert_ardupilot_controller_reports.py"
PROFILE = ROOT / "profiles/declared/controller-reported-failure/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

WATCHDOG_MASK = 1 << 11
MAIN_LOOP_STUCK_MASK = 1 << 15


def record(kind, data):
    return {"meta": {"type": kind}, "data": data}


def err(time_us, subsystem, code):
    """One ERR record: TimeUS, Subsys, ECode -- the pinned "QBB" field list."""
    return record("ERR", {"TimeUS": time_us, "Subsys": subsystem, "ECode": code})


def pm(time_us, mask, *, field="IntE", line=1234, count=2, extra=None):
    """One PM record, carrying the internal-error mask, line and count plus adopted quantities."""
    data = {"TimeUS": time_us, "LR": 400, "NLon": 0, "NL": 400, "MaxT": 2500, "Mem": 65536,
            "Load": 512, "ErrL": line, field: mask, "ErrC": count}
    data.update(extra or {})
    return record("PM", data)


WDG_FULL_TEXT = ("WDG: T5 SL0 FL1234 FT3 FAdeadbeef FTP182 FLR8004a1b FICSR11 MM33 MC400 "
                 "IE2048 IEC7 TN:idle")
WDG_CUT = 50


def wdg(time_us, text=None, *, chunk_id=7, seq=0, caller_format=None):
    """One MSG record shaped as the pinned writer actually saves it: the text cut at the GCS length.

    `caller_format` sets the explicit declaration a saved record must carry before an over-long text may
    be read as a caller-reassembled one; without it such a text stays unqualified.
    """
    data = {"TimeUS": time_us, "Message": WDG_FULL_TEXT[:WDG_CUT] if text is None else text}
    if chunk_id is not None:
        data["ID"] = chunk_id
    if seq is not None:
        data["Seq"] = seq
    entry = record("MSG", data)
    if caller_format is not None:
        entry["meta"][module.CALLER_FORMAT_KEY] = caller_format
    return entry


def report(records):
    return json.dumps({"value_basis": module.READER_BASIS,
                       "source_records": records}).encode("utf-8")


class ArduPilotControllerReportTests(unittest.TestCase):
    def rows(self, records):
        converted, summary = module.convert(report(records))
        return list(csv.DictReader(io.StringIO(converted))), summary, converted

    def test_reported_failure_and_internal_error_reach_the_selected_columns(self):
        rows, summary, _ = self.rows([
            record("MODE", {"TimeUS": 1_000, "Mode": 10}),          # unselected type
            err(2_000_000, 6, 1),                                    # FAILSAFE_BATT / FAILSAFE_OCCURRED
            err(2_100_000, 12, 1),                                   # CRASH_CHECK / CRASH_CHECK_CRASH
            pm(2_200_000, WATCHDOG_MASK | MAIN_LOOP_STUCK_MASK),
        ])
        self.assertEqual(len(rows), 3)
        battery, crash, performance = rows
        self.assertEqual(battery["controller_event_kind"], "REPORTED_SUBSYSTEM_ERROR")
        self.assertEqual(battery["controller_actor_reported"], "FAILSAFE_BATT")
        self.assertEqual(battery["err_subsystem_code"], "6")
        self.assertEqual(battery["err_reported_code"], "1")
        self.assertEqual(battery["err_reported_code_name"], "FAILSAFE_OCCURRED")
        self.assertEqual(battery["err_code_disposition"],
                         "NAMED_BY_A_PINNED_WRITER_CALLSITE_FOR_THIS_SUBSYSTEM")
        self.assertEqual(battery["record_time_us"], "2000000")
        self.assertEqual(crash["controller_actor_reported"], "CRASH_CHECK")
        self.assertEqual(crash["err_reported_code_name"], "CRASH_CHECK_CRASH")
        self.assertIn("NO_CRASH_NO_ROOT_CAUSE", crash["controller_scope_basis"])
        self.assertEqual(performance["controller_event_kind"], "REPORTED_INTERNAL_ERROR_ACCOUNTING")
        self.assertEqual(performance["controller_actor_reported"], "AUTOPILOT_INTERNAL_ERROR_ACCOUNTING")
        self.assertEqual(performance["pm_internal_error_mask_field"], "IntE")
        self.assertEqual(performance["pm_internal_error_mask_reported"],
                         str(WATCHDOG_MASK | MAIN_LOOP_STUCK_MASK))
        self.assertEqual(performance["pm_internal_error_bits_named"], "watchdog_reset|main_loop_stuck")
        self.assertEqual(performance["pm_watchdog_reset_reported"], "1")
        self.assertEqual(performance["pm_internal_error_line_reported"], "1234")
        self.assertEqual(performance["pm_internal_error_count_reported"], "2")
        self.assertEqual(performance["err_subsystem_code"], "")
        self.assertEqual(json.loads(bytes.fromhex(battery["source_record_hex"][4:]).decode()),
                         err(2_000_000, 6, 1))
        self.assertEqual(summary["err_records_by_subsystem"], {"FAILSAFE_BATT": 1, "CRASH_CHECK": 1})
        self.assertEqual(summary["internal_error_bits"], {"watchdog_reset": 1, "main_loop_stuck": 1})
        self.assertEqual(summary["watchdog_reset_reports"], 1)
        self.assertEqual(summary["unselected_record_types"], {"MODE": 1})
        self.assertEqual(summary["clock"], "BootRelative")

    def test_a_normal_reboot_or_quiet_controller_is_never_a_watchdog_or_a_crash(self):
        """The core meaning check: absence, silence and a clean mask report nothing."""
        rows, summary, _ = self.rows([pm(1_000_000, 0, line=0, count=0)])
        self.assertEqual(rows[0]["pm_watchdog_reset_reported"], "0")
        self.assertEqual(rows[0]["pm_internal_error_bits_named"], "")
        self.assertEqual(summary["watchdog_reset_reports"], 0)
        self.assertEqual(summary["internal_error_bits"], {})
        self.assertIn("A_ZERO_MASK_MEANS_THE_PRODUCER_REPORTED_NO_INTERNAL_ERROR",
                      rows[0]["controller_watchdog_basis"])
        with self.assertRaises(ValueError):
            self.rows([pm(9_000_000, 0), pm(1_000, 0)])
        with self.assertRaises(ValueError):
            self.rows([record("MODE", {"TimeUS": 10, "Mode": 0}),
                       record("MSG", {"TimeUS": 20, "Message": "PreArm: check"})])
        rows, summary, _ = self.rows([err(1_000_000, 2, 2), err(60_000_000, 2, 2)])
        self.assertEqual([row["controller_event_kind"] for row in rows],
                         ["REPORTED_SUBSYSTEM_ERROR"] * 2)
        self.assertEqual(summary["watchdog_reset_reports"], 0)

    def test_unknown_subsystems_codes_and_bits_are_counted_never_guessed(self):
        rows, summary, _ = self.rows([
            err(1_000_000, 200, 7),        # subsystem the pin does not define
            err(1_100_000, 12, 9),         # defined subsystem, code the pin groups nowhere
            err(1_200_000, 3, 4),          # defined subsystem, general code UNHEALTHY
            pm(1_300_000, (1 << 30) | WATCHDOG_MASK),   # bit 30 is not named by the pin
        ])
        unknown_subsystem, unknown_code, general, mask = rows
        self.assertEqual(unknown_subsystem["err_subsystem_name"], "")
        self.assertEqual(unknown_subsystem["controller_actor_reported"],
                         "SUBSYSTEM_200_UNDEFINED_BY_THE_PIN")
        self.assertEqual(unknown_subsystem["err_reported_code"], "7")
        self.assertEqual(unknown_subsystem["err_code_disposition"],
                         "SUBSYSTEM_UNDEFINED_BY_THE_PIN_SO_THE_CODE_IS_NOT_NAMED")
        self.assertEqual(unknown_code["err_reported_code_name"], "")
        self.assertEqual(unknown_code["err_code_disposition"],
                         "CODE_NOT_SOURCE_QUALIFIED_FOR_THIS_SUBSYSTEM_SO_IT_STAYS_RAW")
        self.assertEqual(general["err_subsystem_name"], "COMPASS")
        self.assertEqual(general["err_reported_code"], "4")
        self.assertEqual(general["err_reported_code_name"], "")
        self.assertEqual(general["err_code_disposition"],
                         "CODE_NOT_SOURCE_QUALIFIED_FOR_THIS_SUBSYSTEM_SO_IT_STAYS_RAW")
        self.assertEqual(mask["pm_internal_error_bits_named"], "watchdog_reset")
        self.assertEqual(mask["pm_internal_error_unnamed_bit_positions"], "30")
        self.assertEqual(summary["undefined_err_subsystems"], {"200": 1})
        self.assertEqual(summary["unqualified_err_codes"],
                         {"CRASH_CHECK:9": 1, "COMPASS:4": 1})
        self.assertEqual(summary["undefined_internal_error_bits"], {"30": 1})

    def test_wrong_format_and_malformed_records_are_refused(self):
        for label, raw in (
                ("not bytes", "{}"),
                ("empty", b""),
                ("not an object", b"[]"),
                ("raw JSONL rather than a reader report",
                 b'{"source_records": [{"meta": {"type": "ERR"}, "data": {}}]}'),
                ("wrong value basis",
                 json.dumps({"value_basis": "something-else", "source_records": []}).encode()),
                ("no source records", json.dumps({"value_basis": module.READER_BASIS,
                                                  "source_records": []}).encode()),
                ("duplicate JSON key",
                 b'{"value_basis": "x", "value_basis": "y"}'),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                module.convert(raw)
        for label, records in (
                ("record is not an object", [["ERR"]]),
                ("missing data", [{"meta": {"type": "ERR"}}]),
                ("missing type", [{"meta": {}, "data": {"TimeUS": 1}}]),
                ("missing TimeUS", [record("ERR", {"Subsys": 1, "ECode": 1})]),
                ("subsystem wider than its byte", [err(1, 300, 1)]),
                ("negative code", [err(1, 1, -1)]),
                ("boolean instead of a number", [err(1, True, 1)]),
                ("PM without a mask field",
                 [record("PM", {"TimeUS": 1, "Load": 10, "ErrL": 0, "ErrC": 0})]),
        ):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.rows(records)


    def test_a_code_is_named_only_for_a_source_qualified_subsystem_pairing(self):
        """No general code is applied to a subsystem the source does not pair it with."""
        rows, summary, _ = self.rows([
            err(1_000_000, 3, 1),    # COMPASS with code 1
            err(1_100_000, 3, 4),    # COMPASS with code 4
            err(1_200_000, 19, 1),   # CPU with code 1
            err(1_300_000, 19, 4),   # CPU with code 4
            err(1_400_000, 26, 1),   # FAILSAFE_SENSORS with code 1
        ])
        for row in rows:
            self.assertEqual(row["err_reported_code_name"], "", row["err_subsystem_name"])
            self.assertEqual(row["err_code_disposition"],
                             "CODE_NOT_SOURCE_QUALIFIED_FOR_THIS_SUBSYSTEM_SO_IT_STAYS_RAW")
            self.assertTrue(row["err_subsystem_name"])
            self.assertTrue(row["err_reported_code"])
        self.assertEqual(summary["unqualified_err_codes"],
                         {"COMPASS:1": 1, "COMPASS:4": 1, "CPU:1": 1, "CPU:4": 1,
                          "FAILSAFE_SENSORS:1": 1})
        rows, _, _ = self.rows([err(1_000_000, 17, 1), err(1_100_000, 1, 1), err(1_200_000, 18, 2)])
        ekf_inav, main, baro = rows
        self.assertEqual((ekf_inav["err_reported_code_name"], ekf_inav["err_code_disposition"]),
                         ("FAILSAFE_OCCURRED", "NAMED_BY_A_PINNED_WRITER_CALLSITE_FOR_THIS_SUBSYSTEM"))
        self.assertEqual((main["err_reported_code_name"], main["err_code_disposition"]),
                         ("MAIN_INS_DELAY", "NAMED_BY_THE_PINNED_HEADER_GROUP_FOR_THIS_SUBSYSTEM"))
        self.assertEqual((baro["err_reported_code_name"], baro["err_code_disposition"]),
                         ("BARO_GLITCH", "NAMED_BY_THE_PINNED_HEADER_GROUP_FOR_THIS_SUBSYSTEM"))

    def test_a_fence_code_is_a_breach_bitmask_not_an_enum_value(self):
        """The pinned fence call site casts a breach bitmask into the code type."""
        rows, summary, _ = self.rows([err(1_000_000, 9, 0), err(1_100_000, 9, 5)])
        resolved, breach = rows
        self.assertEqual(resolved["err_reported_code_name"], "ERROR_RESOLVED")
        self.assertEqual(resolved["err_code_disposition"],
                         "NAMED_BY_A_PINNED_WRITER_CALLSITE_FOR_THIS_SUBSYSTEM")
        self.assertEqual(breach["err_subsystem_name"], "FAILSAFE_FENCE")
        self.assertEqual(breach["err_reported_code"], "5")
        self.assertEqual(breach["err_reported_code_name"], "")
        self.assertEqual(breach["err_code_disposition"],
                         "FENCE_BREACH_BITMASK_CAST_INTO_THE_CODE_TYPE_NOT_AN_ENUM_CODE")
        self.assertEqual(summary["unqualified_err_codes"], {"FAILSAFE_FENCE:5": 1})

    def test_the_pm_field_alias_is_source_qualified_and_a_contradiction_is_refused(self):
        for field, source in (("InE", "CURRENT_PIN_LOG_FORMAT"), ("IntE", "ADOPTED_PM_4_3_SCHEMA")):
            with self.subTest(field=field):
                rows, _, _ = self.rows([pm(1_000_000, WATCHDOG_MASK, field=field)])
                self.assertEqual(rows[0]["pm_internal_error_mask_field"], field)
                self.assertEqual(rows[0]["pm_internal_error_mask_field_source"], source)
        for name, source in (("ErC", "CURRENT_PIN_LOG_FORMAT"), ("ErrC", "ADOPTED_PM_4_3_SCHEMA")):
            with self.subTest(count=name):
                data = {"TimeUS": 1_000_000, "Load": 10, "ErrL": 3, "InE": 0, name: 4}
                rows, _, _ = self.rows([record("PM", data)])
                self.assertEqual(rows[0]["pm_internal_error_count_reported"], "4")
                self.assertEqual(rows[0]["pm_internal_error_count_field_source"], source)
        with self.assertRaises(ValueError):
            self.rows([record("PM", {"TimeUS": 1, "InE": 0, "IntE": WATCHDOG_MASK, "ErrL": 0})])
        with self.assertRaises(ValueError):
            self.rows([record("PM", {"TimeUS": 1, "InE": 0, "ErC": 1, "ErrC": 2})])
        rows, _, _ = self.rows([record("PM", {"TimeUS": 1, "InE": 0})])
        self.assertEqual(rows[0]["pm_internal_error_line_reported"], "")
        self.assertEqual(rows[0]["pm_internal_error_count_reported"], "")
        self.assertEqual(rows[0]["pm_internal_error_count_field_source"], "")
        with self.assertRaises(ValueError):
            self.rows([record("PM", {"TimeUS": 1, "InE": "2048"})])

    def test_the_boot_after_watchdog_record_decodes_only_what_survives_the_producer_cut(self):
        """The producer chain truncates this text before it is logged; nothing past the cut is invented."""
        rows, summary, _ = self.rows([
            record("MSG", {"TimeUS": 500_000, "Message": "ArduCopter V4.6.0 (abcdef12)", "ID": 6,
                           "Seq": 0}),
            wdg(1_000_000),
        ])
        self.assertEqual(len(rows), 1)
        boot = rows[0]
        self.assertEqual(boot["controller_event_kind"], "REPORTED_BOOT_AFTER_WATCHDOG_RESET")
        self.assertEqual(boot["controller_actor_reported"], "AUTOPILOT_WATCHDOG_PERSISTENT_DATA")
        self.assertEqual(boot["wdg_text_shape"], "NATIVE_LOGGER_MSG_TEXT_AT_THE_PRODUCER_CUT")
        self.assertEqual(boot["wdg_text_retained_bytes"], str(WDG_CUT))
        self.assertEqual(boot["wdg_text_disposition"],
                         "PREFIX_FIELDS_DECODED_TAIL_LOST_TO_THE_PRODUCER_CUT")
        self.assertEqual((boot["msg_chunk_id_reported"], boot["msg_chunk_sequence_reported"]), ("7", "0"))
        self.assertEqual(boot["wdg_scheduler_task_reported"], "5")
        self.assertEqual(boot["wdg_fault_line_reported"], "1234")
        self.assertEqual(boot["wdg_fault_type_reported"], "3")
        self.assertEqual(boot["wdg_fault_address_reported"], str(0xdeadbeef))
        self.assertEqual(boot["wdg_fault_thread_priority_reported"], "182")
        self.assertEqual(boot["wdg_fault_link_register_reported"], "")
        self.assertEqual(boot["wdg_internal_errors_mask_reported"], "")
        self.assertEqual(boot["wdg_internal_error_bits_named"], "")
        self.assertEqual(boot["wdg_internal_error_count_reported"], "")
        self.assertEqual(boot["wdg_thread_name_hex"], "")
        self.assertEqual(boot["wdg_fields_unrecoverable"].split("|")[0],
                         "wdg_fault_link_register_reported")
        for name in ("wdg_internal_errors_mask_reported", "wdg_internal_error_count_reported",
                     "wdg_thread_name_hex"):
            self.assertIn(name, boot["wdg_fields_unrecoverable"].split("|"))
        self.assertEqual(summary["watchdog_boot_reports"], 1)
        self.assertEqual(summary["watchdog_unrecoverable_fields"]["wdg_thread_name_hex"], 1)
        self.assertIn("NEVER_RECOVERABLE_FROM_A_NATIVE_RECORD", boot["controller_truncation_basis"])
        self.assertIn("NEVER_AN_INFERENCE_FROM_A_RESTARTED_CLOCK", boot["controller_reboot_basis"])
        self.assertEqual(summary["unselected_record_types"], {"MSG_WITHOUT_THE_WATCHDOG_PREFIX": 1})

    def test_a_later_chunk_or_an_unparsable_prefix_is_counted_never_joined(self):
        rows, summary, _ = self.rows([wdg(1_000_000, seq=1), wdg(1_100_000)])
        self.assertEqual(rows[0]["wdg_text_disposition"],
                         "NONZERO_CHUNK_SEQUENCE_SO_NOT_A_COMPLETE_NATIVE_WDG_RECORD")
        self.assertEqual(rows[0]["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
        self.assertEqual(rows[0]["controller_actor_reported"],
                         "UNQUALIFIED_NOT_A_CONFIRMED_PRODUCER_SHAPED_BOOT_REPORT")
        self.assertEqual(rows[1]["controller_event_kind"], "REPORTED_BOOT_AFTER_WATCHDOG_RESET")
        self.assertEqual(rows[0]["wdg_scheduler_task_reported"], "")
        self.assertEqual(rows[0]["msg_chunk_sequence_reported"], "1")
        self.assertEqual((summary["watchdog_nonzero_chunk_records"], summary["watchdog_boot_reports"]),
                         (1, 1))
        self.assertIn("REFUSED_FOR_DECODING_AND_COUNTED_RATHER_THAN_JOINED", rows[0]["controller_chunk_basis"])
        rows, summary, _ = self.rows([wdg(1_000_000, chunk_id=None, seq=None)])
        self.assertEqual((rows[0]["msg_chunk_id_reported"], rows[0]["msg_chunk_sequence_reported"]),
                         ("", ""))
        self.assertEqual(summary["watchdog_records_without_chunk_metadata"], 1)
        self.assertEqual(rows[0]["wdg_text_shape"],
                         "AT_THE_PRODUCER_CUT_BUT_CHUNK_METADATA_INCOMPLETE_SO_NATIVE_SHAPE_UNKNOWN")
        rows, _, _ = self.rows([wdg(1_000_000, chunk_id=None, seq=0)])
        self.assertEqual(rows[0]["msg_chunk_sequence_reported"], "0")
        self.assertEqual(rows[0]["wdg_text_shape"],
                         "AT_THE_PRODUCER_CUT_BUT_CHUNK_METADATA_INCOMPLETE_SO_NATIVE_SHAPE_UNKNOWN")
        unshaped = "WDG: something else entirely".ljust(WDG_CUT, "x")
        rows, summary, _ = self.rows([wdg(1_000_000, text=unshaped)])
        self.assertEqual(rows[0]["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
        self.assertEqual(rows[0]["wdg_text_disposition"],
                         "TOKEN_OUTSIDE_THE_PINNED_FORMATS_OWN_CONVERSION_SO_THE_TEXT_IS_NOT_PRODUCER_SHAPED")
        self.assertEqual(rows[0]["wdg_scheduler_task_reported"], "")
        self.assertEqual((summary["watchdog_boot_reports"], summary["watchdog_malformed_texts"]),
                         (0, 1))

    def test_a_long_text_qualifies_only_with_an_explicit_caller_format_declaration(self):
        """Being longer than the cut is not evidence of reassembly; only a declaration qualifies it."""
        rows, summary, _ = self.rows([wdg(1_000_000, text=WDG_FULL_TEXT)])
        undeclared = rows[0]
        self.assertGreater(len(WDG_FULL_TEXT.encode("utf-8")), WDG_CUT)
        self.assertEqual(undeclared["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
        self.assertEqual(undeclared["wdg_text_shape"],
                         "LONGER_THAN_THE_PRODUCER_CUT_WITH_NO_DECLARED_CALLER_FORMAT")
        self.assertEqual(undeclared["wdg_text_disposition"],
                         "TEXT_LONGER_THAN_THE_PRODUCER_CUT_AND_NO_SUPPORTED_CALLER_FORMAT_DECLARED")
        self.assertEqual(undeclared["wdg_scheduler_task_reported"], "")
        self.assertEqual(undeclared["wdg_internal_errors_mask_reported"], "")
        self.assertEqual(summary["watchdog_undeclared_long_texts"], 1)
        self.assertEqual(summary["watchdog_boot_reports"], 0)
        rows, summary, _ = self.rows([wdg(1_000_000, text=WDG_FULL_TEXT, caller_format="something-else")])
        self.assertEqual(rows[0]["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
        self.assertEqual(summary["watchdog_undeclared_long_texts"], 1)
        rows, summary, _ = self.rows([wdg(1_000_000, text=WDG_FULL_TEXT,
                                          caller_format=module.SUPPORTED_CALLER_FORMAT)])
        row = rows[0]
        self.assertEqual(row["wdg_text_shape"],
                         "CALLER_DECLARED_REASSEMBLED_TEXT_NOT_A_NATIVE_LOGGER_RECORD")
        self.assertEqual(row["wdg_text_disposition"], "EVERY_PINNED_FIELD_PRESENT_IN_THE_RETAINED_TEXT")
        self.assertEqual(row["wdg_internal_errors_mask_reported"], str(WATCHDOG_MASK))
        self.assertEqual(row["wdg_internal_error_bits_named"], "watchdog_reset")
        self.assertEqual(row["wdg_internal_error_count_reported"], "7")
        self.assertEqual(bytes.fromhex(row["wdg_thread_name_hex"][4:]).decode(), "idle")
        self.assertEqual(row["wdg_fields_unrecoverable"], "")
        self.assertEqual(summary["watchdog_boot_reports"], 1)

    def test_impossible_writer_text_never_qualifies_as_a_producer_report(self):
        """Python's int() would accept these; the writer's own conversions cannot emit them."""
        for label, original, broken in (
                ("underscore digits", "SL0", "SL0_0"),
                ("explicit plus sign", "T5", "T+5"),
                ("magnitude past the conversion", "SL0", "SL99999999999999999999"),
                ("uppercase hex the %x conversion never emits", "FAdeadbeef", "FADEADBEEF"),
                ("thread name longer than the %.4s conversion", "TN:idle", "TN:toolong"),
        ):
            with self.subTest(label=label):
                rows, summary, _ = self.rows([
                    wdg(1_000_000, text=WDG_FULL_TEXT.replace(original, broken),
                        caller_format=module.SUPPORTED_CALLER_FORMAT)])
                row = rows[0]
                self.assertEqual(row["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
                self.assertEqual(row["wdg_text_disposition"],
                                 "TOKEN_OUTSIDE_THE_PINNED_FORMATS_OWN_CONVERSION_SO_THE_TEXT_IS_NOT_"
                                 "PRODUCER_SHAPED")
                self.assertEqual(row["wdg_scheduler_task_reported"], "")
                self.assertEqual(row["wdg_internal_errors_mask_reported"], "")
                self.assertEqual((summary["watchdog_boot_reports"], summary["watchdog_malformed_texts"]),
                                 (0, 1))

    def test_a_text_shorter_than_the_producer_cut_never_qualifies(self):
        """The pinned format cannot produce a short text, so its missing tail is not the producer's cut."""
        for text in ("WDG: T1", "WDG: T5 SL0 FL1234", WDG_FULL_TEXT[:WDG_CUT - 1]):
            with self.subTest(text=text):
                rows, summary, _ = self.rows([wdg(1_000_000, text=text)])
                row = rows[0]
                self.assertLess(len(text.encode("utf-8")), WDG_CUT)
                self.assertEqual(row["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
                self.assertEqual(row["wdg_text_shape"],
                                 "SHORTER_THAN_THE_PRODUCER_CUT_SO_NOT_A_COMPLETE_PRODUCER_TEXT")
                self.assertEqual(row["wdg_text_disposition"],
                                 "SHORTER_THAN_THE_PRODUCER_CUT_SO_ITS_MISSING_TAIL_IS_NOT_THE_"
                                 "PRODUCERS_TRUNCATION")
                self.assertEqual(row["wdg_scheduler_task_reported"], "")
                self.assertEqual((summary["watchdog_boot_reports"], summary["watchdog_short_texts"]),
                                 (0, 1))

    def test_a_declared_caller_text_must_parse_completely(self):
        """A declared reassembled text with anything left over is not that format."""
        for label, text in (
                ("trailing extra token", WDG_FULL_TEXT + " EXTRA42"),
                ("missing a middle field", WDG_FULL_TEXT.replace(" FICSR11", "") + " PADPADPADPAD"),
        ):
            with self.subTest(label=label):
                rows, summary, _ = self.rows([
                    wdg(1_000_000, text=text, caller_format=module.SUPPORTED_CALLER_FORMAT)])
                row = rows[0]
                self.assertEqual(row["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
                self.assertEqual(row["wdg_scheduler_task_reported"], "")
                self.assertEqual(row["wdg_fields_unrecoverable"], "")
                self.assertEqual(summary["watchdog_boot_reports"], 0)
                self.assertEqual(summary["watchdog_malformed_texts"]
                                 + summary["watchdog_incomplete_declared_texts"], 1)

    def test_the_cut_is_measured_in_bytes_not_characters(self):
        text = "WDG: T5 SL0 FL1234 FT3 FAdeadbeef FTP182 FLR80" + "\u00e9" * 3
        self.assertLess(len(text), WDG_CUT)
        self.assertGreater(len(text.encode("utf-8")), WDG_CUT)
        rows, summary, _ = self.rows([wdg(1_000_000, text=text)])
        self.assertEqual(rows[0]["wdg_text_retained_bytes"], str(len(text.encode("utf-8"))))
        self.assertEqual(rows[0]["controller_event_kind"], "UNQUALIFIED_WATCHDOG_PREFIXED_MSG")
        self.assertEqual(summary["watchdog_undeclared_long_texts"], 1)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_reported_reason_and_watchdog_reach_the_actual_common_reader(self):
        _, _, converted = self.rows([
            err(2_000_000, 6, 1),
            pm(2_200_000, WATCHDOG_MASK, field="InE", line=99, count=1),
            wdg(2_400_000),
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(converted, encoding="utf-8")
            common = json.loads(subprocess.run(
                [READER, str(PROFILE), str(path)], capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 3)
        self.assertEqual(common["platform_domain"], "Unknown")
        failure, watchdog, boot = common["observations"]
        self.assertEqual(failure["fields"]["controller_actor_reported"], "FAILSAFE_BATT")
        self.assertEqual(failure["fields"]["err_reported_code_name"], "FAILSAFE_OCCURRED")
        self.assertEqual(failure["fields"]["err_reported_code"], 1)
        self.assertEqual(watchdog["fields"]["pm_watchdog_reset_reported"], 1)
        self.assertEqual(watchdog["fields"]["pm_internal_error_bits_named"], "watchdog_reset")
        self.assertEqual(watchdog["fields"]["pm_internal_error_mask_field"], "InE")
        self.assertEqual(boot["fields"]["controller_event_kind"], "REPORTED_BOOT_AFTER_WATCHDOG_RESET")
        self.assertEqual(boot["fields"]["wdg_fault_line_reported"], 1234)
        self.assertEqual(boot["fields"]["wdg_text_shape"],
                         "NATIVE_LOGGER_MSG_TEXT_AT_THE_PRODUCER_CUT")
        self.assertIsNone(boot["fields"]["wdg_internal_errors_mask_reported"])
        self.assertIn("wdg_thread_name_hex", boot["fields"]["wdg_fields_unrecoverable"])
        self.assertTrue(all(o["clock_basis"] == "BootRelative" for o in common["observations"]))
        units = common["profile_units"]
        self.assertIn("never_used_here_to_infer_that_a_reboot_occurred", units["record_time_us"])
        self.assertIn("one_only_when_bit_11", units["pm_watchdog_reset_reported"])
        self.assertIn("finite_alias_qualified_by_two_sources", units["pm_internal_error_mask_field_source"])
        self.assertIn("left_unreported_rather_than_inferred", units["controller_reboot_basis"])
        self.assertIn("cuts_this_text_before_it_is_logged", units["controller_truncation_basis"])
        self.assertIn("THIS_EXACT_subsystem_and_code_pairing", units["err_reported_code_name"])

    def test_command_line_writes_the_conversion_and_its_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "reader-report.json"
            source.write_bytes(report([err(1_000_000, 16, 2), pm(1_100_000, 0)]))
            target = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)],
                           check=True, capture_output=True)
            summary = json.loads((target / "report.json").read_text())
            self.assertEqual(summary["selected_rows"], 2)
            self.assertEqual(summary["err_records_by_subsystem"], {"EKFCHECK": 1})
            self.assertEqual(summary["watchdog_reset_reports"], 0)
            self.assertIn("NOT_A_WATCHDOG", summary["scope"])


if __name__ == "__main__":
    unittest.main()
