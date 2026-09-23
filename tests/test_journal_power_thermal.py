"""Authored `journalctl -o json` exports shaped by the pinned producers; no journal and no device.

Every input here is authored to the shape systemd `v257`
(`70bae7648f2c18010187c9cf20093155eaa26029`) documents and to the exact format strings the pinned
kernel sources write. Nothing is captured, nothing is executed, and a passing test proves code and
schema behaviour only - never a real device, a real capture or a physical diagnosis.
"""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import unittest.mock

from scripts.convert_journal_power_thermal import classify, convert

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/power-thermal-reports/reception-profile.toml"
SCRIPT = ROOT / "scripts/convert_journal_power_thermal.py"
RECEIVED = 1_789_000_000_000_000


def entry(message, *, transport="kernel", received=RECEIVED, source=None, priority="0", **extra):
    """One saved export line, with the documented field encodings."""
    record = {"__REALTIME_TIMESTAMP": str(received), "_TRANSPORT": transport, "PRIORITY": priority,
              "_BOOT_ID": "3d1a7b2c4e5f4a6b8c9d0e1f2a3b4c5d", "_COMM": "kernel"}
    if message is not None:
        record["MESSAGE"] = message
    if source is not None:
        record["_SOURCE_REALTIME_TIMESTAMP"] = str(source)
    record.update(extra)
    return json.dumps(record)


DECLARED_UPS = "upsmon-2.8.2-stock-notifymsg"


def ups(message, *, received=RECEIVED, comm="upsmon", **extra):
    """One saved upsmon notification line: syslog transport, the daemon's own `_COMM`."""
    return entry(message, transport="syslog", received=received, priority="5", _COMM=comm,
                 SYSLOG_IDENTIFIER="upsmon", **extra)


def export(*lines):
    return "\n".join(lines) + "\n"


def unhex(value):
    return "" if value == "" else bytes.fromhex(value[4:]).decode()


class JournalPowerThermalTests(unittest.TestCase):
    def rows_of(self, text, **kwargs):
        kwargs.setdefault("clock", "journal_reception_realtime")
        output, report = convert(text, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report, output


    def test_a_thermal_trip_and_its_protective_shutdown_are_carried_apart(self):
        rows, report, _ = self.rows_of(export(
            entry("thermal thermal_zone0: x86_pkg_temp: critical temperature reached"),
            entry("reboot: HARDWARE PROTECTION shutdown (Temperature too high)", received=RECEIVED + 40),
        ))
        self.assertEqual((report["selected_entries"], report["thermal_entries"],
                          report["power_entries"]), (2, 2, 0))
        trip, shutdown = rows
        self.assertEqual(trip["reported_condition"], "CRITICAL_TEMPERATURE_TRIP_REACHED")
        self.assertEqual(trip["condition_domain"], "THERMAL")
        self.assertEqual(trip["record_time_us"], str(RECEIVED))
        self.assertEqual(unhex(trip["reported_device_label_hex"]),
                         "thermal thermal_zone0: x86_pkg_temp")
        self.assertIn("WOULD_INVENT_A_STRUCTURE", trip["device_label_basis"])
        self.assertIn("NO_TEMPERATURE_AND_NO_VOLTAGE", trip["measured_value_status"])
        self.assertFalse([name for name in trip if "temperature_c" in name or "_celsius" in name])
        self.assertEqual(shutdown["reported_condition"], "HARDWARE_PROTECTION_SHUTDOWN_INITIATED")
        self.assertEqual(unhex(shutdown["reported_shutdown_reason_hex"]), "Temperature too high")
        self.assertIn("INITIATED_SHUTDOWN_IS_NOT_A_COMPLETED_ONE", shutdown["shutdown_reason_basis"])
        for row in rows:
            self.assertEqual(row["report_class"], "ASSERTED_EVENT_THE_PRODUCER_ITSELF_REPORTED")
            self.assertIn("NEVER_EMITS_A_MEASUREMENT", row["report_class_basis"])
            self.assertIn("GAP_IN_OBSERVATIONS_IS_NEVER_READ_AS_A_CONDITION", row["report_class_basis"])
            self.assertEqual(unhex(row["original_report_hex"]).endswith("\n"), False)
            self.assertEqual(row["journal_trusted_transport"], "kernel")

    def test_an_undervoltage_edge_is_carried_without_claiming_an_interruption(self):
        rows, report, _ = self.rows_of(export(
            entry("hwmon hwmon1: Undervoltage detected!", priority="2"),
            entry("hwmon hwmon1: Voltage normalised", received=RECEIVED + 2_000_000, priority="6"),
        ))
        self.assertEqual((report["power_entries"], report["thermal_entries"]), (2, 0))
        entered, cleared = rows
        self.assertEqual(entered["reported_condition"], "UNDERVOLTAGE_CONDITION_ENTERED")
        self.assertEqual(entered["condition_transition"], "ENTERED")
        self.assertEqual(cleared["reported_condition"], "UNDERVOLTAGE_CONDITION_CLEARED")
        self.assertEqual(cleared["condition_transition"], "CLEARED")
        self.assertEqual(unhex(entered["reported_device_label_hex"]), "hwmon hwmon1")
        for row in rows:
            self.assertIn("NO_PRODUCER_READ_HERE_ASSERTS_THAT_A_POWER_SUPPLY_WAS_INTERRUPTED",
                          row["interruption_assertion"])
            self.assertIn("ABSENCE_IS_NOT_THE_ABSENCE_OF_AN_INTERRUPTION", row["interruption_assertion"])
            self.assertEqual(row["condition_domain"], "POWER")
        self.assertIn("ABSENCE_IS_NOT_THE_ABSENCE_OF_AN_INTERRUPTION", report["interruption_assertion"])
        self.assertEqual(entered["journal_trusted_boot_id"], "3d1a7b2c4e5f4a6b8c9d0e1f2a3b4c5d")

    def test_a_protective_shutdown_for_another_reason_keeps_the_reason_and_no_domain(self):
        rows, report, _ = self.rows_of(export(
            entry("reboot: HARDWARE PROTECTION shutdown (regulator over-current)"),
            entry("reboot: Hardware protection timed-out. Trying forced poweroff", received=RECEIVED + 10),
            entry("reboot: Hardware protection shutdown failed. Trying emergency restart",
                  received=RECEIVED + 20),
        ))
        self.assertEqual(report["thermal_entries"], 0)
        self.assertEqual([r["condition_domain"] for r in rows], ["UNKNOWN"] * 3)
        self.assertEqual(unhex(rows[0]["reported_shutdown_reason_hex"]), "regulator over-current")
        self.assertEqual([r["reported_condition"] for r in rows[1:]],
                         ["HARDWARE_PROTECTION_FORCED_POWEROFF", "HARDWARE_PROTECTION_EMERGENCY_RESTART"])
        self.assertIn("NOT_A_TRANSITION_REPORT", rows[1]["condition_transition"])


    def test_a_declared_ups_report_is_a_supply_report_and_not_a_mains_failure(self):
        """NUT's stock texts, carried as what they say and nothing more.

        `clients/upsmon.h` `notifylist[]` at NUT v2.8.2 has `"UPS %s on battery"` / `"UPS %s on line
        power"`, and `clients/upsmon.c` fires each only on the OB/OL change. There is **no**
        mains-failure notify type, so nothing here is renamed into one.
        """
        rows, report, _ = self.rows_of(export(
            ups("UPS rack-ups-1 on battery"),
            ups("UPS rack-ups-1 on line power", received=RECEIVED + 90_000_000),
        ), ups_producer=DECLARED_UPS)
        self.assertEqual((report["ups_entries"], report["power_entries"]), (2, 2))
        self.assertEqual(report["declared_ups_producer"], DECLARED_UPS)
        onbatt, online = rows
        self.assertEqual(onbatt["reported_condition"], "UPS_REPORTED_ON_BATTERY")
        self.assertEqual(onbatt["condition_transition"], "ENTERED")
        self.assertEqual(online["reported_condition"], "UPS_REPORTED_ON_LINE_POWER")
        self.assertEqual(online["condition_transition"], "CLEARED")
        self.assertEqual(unhex(onbatt["reported_device_label_hex"]), "rack-ups-1")
        self.assertIn("NEVER_AN_AUTHENTICATED_DEVICE_IDENTITY", onbatt["device_label_basis"])
        for row in rows:
            self.assertEqual(row["condition_domain"], "POWER")
            self.assertEqual(row["declared_producer"], DECLARED_UPS)
            self.assertEqual(row["producer_pin"], "nut-v2.8.2-440ca234")
            self.assertIn("NOT_A_MAINS_FAILURE", row["interruption_assertion"])
            self.assertIn("DOWNSTREAM_COMPUTER_POWER_LOSS", row["interruption_assertion"])
            self.assertIn("NOT_A_PHYSICAL_FAILED_POWER_CAUSE", row["interruption_assertion"])
            self.assertIn("ADMINISTRATOR_CAN_REPLACE_WITH_NOTIFYMSG", row["match_basis"])
            self.assertIn("CHOSEN_BY_NOTIFYFLAG", row["match_basis"])

    def test_a_ups_sentence_without_its_declaration_or_daemon_is_not_that_report(self):
        line = ups("UPS rack-ups-1 on battery")
        with self.assertRaises(ValueError):
            self.rows_of(export(line))
        self.assertIsNone(classify("UPS rack-ups-1 on battery"))
        impostor = ups("UPS rack-ups-1 on battery", comm="logger")
        with self.assertRaises(ValueError):
            self.rows_of(export(impostor), ups_producer=DECLARED_UPS)
        rows, report, _ = self.rows_of(export(impostor, ups("UPS rack-ups-1 on battery")),
                                       ups_producer=DECLARED_UPS)
        self.assertEqual((len(rows), report["entries_not_from_the_declared_ups_daemon"]), (1, 1))
        with self.assertRaises(ValueError):
            self.rows_of(export(line), ups_producer="upsmon-9.9.9-stock-notifymsg")
        rows, _, _ = self.rows_of(export(entry("hwmon hwmon1: Undervoltage detected!")),
                                  ups_producer=DECLARED_UPS)
        self.assertEqual(rows[0]["declared_producer"], "")


    def test_unqualified_entries_are_counted_and_never_selected(self):
        rows, report, _ = self.rows_of(export(
            entry("hwmon hwmon1: Undervoltage detected!", transport="journal", _COMM="logger"),
            entry("EXT4-fs (sda1): mounted filesystem"),          # a kernel line, not a pinned one
            entry("thermal thermal_zone0: cpu-thermal: trip point 0 triggered"),  # a HOT trip
            entry(None),                                           # no MESSAGE at all
            json.dumps({"__REALTIME_TIMESTAMP": str(RECEIVED), "_TRANSPORT": "kernel",
                        "MESSAGE": None}),                         # withheld by the size limit
            json.dumps({"__REALTIME_TIMESTAMP": str(RECEIVED), "_TRANSPORT": "kernel",
                        "MESSAGE": [255, 254]}),                   # non-UTF8 byte array
            entry("hwmon hwmon1: Undervoltage detected!"),         # one that does qualify
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual((report["source_entries"], report["selected_entries"]), (7, 1))
        self.assertEqual(report["entries_not_kernel_transport"], 1)
        self.assertEqual(report["entries_without_a_pinned_assertion"], 2)
        self.assertEqual(report["entries_without_a_message_field"], 1)
        self.assertEqual(report["entries_with_an_unreadable_message"], 2)
        self.assertIsNone(classify("thermal thermal_zone0: cpu-thermal: trip point 0 triggered"))

    def test_a_pinned_sentence_inside_a_longer_line_is_not_that_assertion(self):
        """The producers' real shapes decide the match, not the presence of the words.

        `kernel/reboot.c` opens with `#define pr_fmt(fmt) "reboot: " fmt`, so its `pr_*` lines carry
        that literal prefix and nothing else - there is no device prefix to allow for. And
        `drivers/base/core.c` `__dev_printk()` emits a `dev_*` line as `"%s %s: %pV"` of the driver
        string, the device name and the message, so an arbitrary prefix is not that producer either.
        """
        for text, why in (
                ("kernel test: reboot: HARDWARE PROTECTION shutdown (Temperature too high)",
                 "a prefixed copy of the unprefixed producer's line"),
                ("HARDWARE PROTECTION shutdown (Temperature too high)",
                 "the sentence without the pr_fmt prefix this producer always writes"),
                ("reboot: HARDWARE PROTECTION shutdown (Temperature too high) trailing",
                 "trailing text after the whole-message producer"),
                ("audit: reboot: Hardware protection timed-out. Trying forced poweroff",
                 "a prefixed copy of an exact-match producer line"),
                ("something Undervoltage detected!",
                 "a single-token prefix, not the driver-plus-name shape"),
                ("Undervoltage detected!", "no device prefix at all"),
                ("a b: c d: Voltage normalised extra", "trailing text after the pinned sentence"),
        ):
            self.assertIsNone(classify(text), why)
            with self.assertRaises(ValueError, msg=why):
                self.rows_of(export(entry(text)))
        self.assertIsNotNone(classify("reboot: HARDWARE PROTECTION shutdown (Temperature too high)"))
        self.assertIsNotNone(classify("hwmon hwmon1: Undervoltage detected!"))
        self.assertIsNotNone(classify(" thermal_zone0: cpu-thermal: critical temperature reached"))

    def test_the_entry_bound_counts_entries_read_not_rows_written(self):
        """Every unselected entry reaches a `continue`, so a bound checked only where a row is
        written is never reached by an export made of unselected entries.

        The bound is scaled down here rather than writing two hundred thousand fixture lines: the
        entries stay the writer's normal shape and ordinary size, and only the limit moves. Note the
        honest ordering - for entries of realistic size the 16 MiB **byte** bound binds first, so
        this entry bound is a secondary guard, not the one that usually fires.
        """
        unselected = entry("EXT4-fs (sda1): mounted filesystem")
        qualifying = entry("hwmon hwmon1: Undervoltage detected!")
        with unittest.mock.patch("scripts.convert_journal_power_thermal.MAX_ENTRIES", 4):
            with self.assertRaises(ValueError) as refusal:
                self.rows_of(export(*([unselected] * 4), qualifying))
            self.assertIn("entry bound", str(refusal.exception))
            rows, report, _ = self.rows_of(export(*([unselected] * 3), qualifying))
            self.assertEqual((report["source_entries"], len(rows)), (4, 1))

    def test_the_export_and_clock_contract_is_checked_and_refused(self):
        good = entry("reboot: HARDWARE PROTECTION shutdown (Temperature too high)")
        for text, why in (
                ("", "an empty export"),
                ("not json\n", "a line that is not JSON"),
                ('["a"]\n', "a JSON array instead of an object"),
                (export(entry("EXT4-fs (sda1): mounted filesystem")), "no qualifying entry"),
                (export(json.dumps({"_TRANSPORT": "kernel",
                                    "MESSAGE": "reboot: HARDWARE PROTECTION shutdown (Temperature too high)"})),
                 "the declared clock field is absent"),
                (export(json.dumps({"__REALTIME_TIMESTAMP": "not-a-number", "_TRANSPORT": "kernel",
                                    "MESSAGE": "reboot: HARDWARE PROTECTION shutdown (Temperature too high)"})),
                 "a malformed timestamp"),
                (export(json.dumps({"__REALTIME_TIMESTAMP": str(RECEIVED), "_TRANSPORT": ["kernel"],
                                    "MESSAGE": "reboot: HARDWARE PROTECTION shutdown (Temperature too high)"})),
                 "a transport carrying more than one value"),
        ):
            with self.assertRaises(ValueError, msg=why):
                self.rows_of(text)
        with self.assertRaises(ValueError):
            self.rows_of(export(good), clock="wall_clock_guess")
        with self.assertRaises(ValueError):
            self.rows_of(export(good), source_format="journalctl-json-v999")
        with self.assertRaises(ValueError):
            self.rows_of(export(good), clock="source_realtime")
        rows, _, _ = self.rows_of(export(entry("reboot: HARDWARE PROTECTION shutdown (Temperature too high)",
                                               source=RECEIVED - 5)), clock="source_realtime")
        self.assertEqual(rows[0]["record_time_us"], str(RECEIVED - 5))
        self.assertIn("EARLIEST_TRUSTED", rows[0]["clock_field_basis"])


    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_every_producer_reaches_common_observations(self):
        text = export(
            entry("thermal thermal_zone0: x86_pkg_temp: critical temperature reached"),
            entry("reboot: HARDWARE PROTECTION shutdown (Temperature too high)", received=RECEIVED + 40),
            entry("hwmon hwmon1: Undervoltage detected!", received=RECEIVED + 80, priority="2"),
            ups("UPS rack-ups-1 on battery", received=RECEIVED + 120),
        )
        _, _, output = self.rows_of(text, ups_producer=DECLARED_UPS)
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(PROFILE), str(source),
                 "--allow-equal-time"], capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], 4)
        self.assertEqual(common["platform_domain"], "Unknown", "no platform is inferred from a log")
        self.assertTrue(all(o["clock_basis"] == "Unknown" for o in common["observations"]))
        trip, shutdown, power, supply = (o["fields"] for o in common["observations"])
        self.assertEqual(trip["reported_condition"], "CRITICAL_TEMPERATURE_TRIP_REACHED")
        self.assertEqual(shutdown["reported_condition"], "HARDWARE_PROTECTION_SHUTDOWN_INITIATED")
        self.assertEqual(power["condition_domain"], "POWER")
        units = common["profile_units"]
        self.assertIn("never_inferred_from_a_value", units["condition_domain"])
        self.assertIn("no_threshold_comparison", units["report_class_basis"])
        self.assertIn("absence_is_not_the_absence_of_an_interruption", units["interruption_assertion"])
        self.assertIn("never_an_event", units["journal_trusted_boot_id"])
        self.assertEqual(supply["reported_condition"], "UPS_REPORTED_ON_BATTERY")
        self.assertEqual(supply["declared_producer"], DECLARED_UPS)
        self.assertIn("not_a_mains_failure", units["interruption_assertion"])
        self.assertIn("administrator", units["declared_producer"])

    def test_the_cli_writes_its_accounting_and_never_overwrites(self):
        text = export(entry("hwmon hwmon1: Undervoltage detected!"))
        with tempfile.TemporaryDirectory() as work:
            base = Path(work)
            (base / "saved.json").write_text(text)
            command = [os.sys.executable, str(SCRIPT), str(base / "saved.json"), str(base / "out"),
                       "--clock", "journal_reception_realtime",
                       "--source-format", "journalctl-json-v257"]
            subprocess.run(command, check=True, capture_output=True)
            report = json.loads((base / "out/report.json").read_text())
            self.assertEqual(report["selected_entries"], 1)
            self.assertEqual(report["raspberry_pi_pin"], "raspberrypi-linux-70df9a55")
            self.assertEqual((base / "out/source.json").read_text(), text)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
