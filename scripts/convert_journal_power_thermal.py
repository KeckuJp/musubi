#!/usr/bin/env python3
"""Saved `journalctl -o json` entries carrying a kernel power/thermal assertion -> CSV for the reader.

Passive import of an export an engineer already saved. No journal is opened, no device is inspected,
`journalctl` is never run, and nothing here diagnoses hardware.

This converter emits **only an event the producer itself asserted**. It never emits a measurement, a
configured limit, a threshold comparison or a physical causal diagnosis, and it never treats a gap in
observations as a condition. The producers below print **no temperature and no voltage**, so there is
no quantity column at all: there is nothing to scale and nothing to compare against a limit.

Selection always needs a pinned producer text **and** a trusted binding for that producer; the
sentence alone never carries a row. There is no `MESSAGE_ID` on any of these lines, so the text is
the only carrier, and every row records its match as a **text match** - weaker provenance than a
structured identifier - with the original report retained verbatim.

* **Kernel producers** need the **trusted** `_TRANSPORT` field to be `kernel`.
  `man/systemd.journal-fields.xml` at systemd `v257`
  (`70bae7648f2c18010187c9cf20093155eaa26029`) lists `_TRANSPORT` among the Trusted Journal Fields -
  journald sets it and client code cannot alter it - and documents `kernel` as "for those read from
  the kernel". That is what keeps a user process from writing one of these sentences itself.
* The **UPS daemon producer** needs a caller **declaration** plus the trusted `_COMM` being `upsmon`,
  because unlike the kernel strings its text is an administrator-replaceable default. Without the
  declaration no UPS line is read at all.

Producers, read at the pins this repository already declares where one existed:

* Linux `adc218676eef25575469234709c2d87185ca223a`, `drivers/thermal/thermal_core.c`
  `thermal_zone_device_halt()`: `dev_emerg(&tz->device, "%s: critical temperature reached\\n",
  tz->type)`, then `hw_protection_shutdown("Temperature too high", ...)` for the shutdown case or
  `hw_protection_reboot(...)` for the reboot case. `handle_critical_trips()` routes
  `THERMAL_TRIP_CRITICAL` to `tz->ops.critical` and everything else to `tz->ops.hot`, so a HOT trip is
  a different trip and is not this message.
* Linux, same pin, `kernel/reboot.c` `__hw_protection_shutdown()`:
  `pr_emerg("HARDWARE PROTECTION shutdown (%s)\\n", reason)`, plus "Hardware protection timed-out.
  Trying forced poweroff" and "Hardware protection shutdown failed. Trying emergency restart". The
  reason is the **caller's** string, so this message is not thermal by itself: only the exact reason
  `Temperature too high` binds it to the thermal domain at this pin, and any other reason is retained
  verbatim with the domain left unknown.
* Raspberry Pi Linux `70df9a55434b2c05f70163524007f1b4382489a3`,
  `drivers/hwmon/raspberrypi-hwmon.c` `rpi_firmware_get_throttled()`:
  `dev_crit(..., "Undervoltage detected!\\n")` and `dev_info(..., "Voltage normalised\\n")`, emitted
  only when `UNDERVOLTAGE_STICKY_BIT BIT(16)` **changes** between polls `2 * HZ` apart.

What is NOT asserted, and is therefore not claimed anywhere in the output: **no producer read here
says a power supply was interrupted.** `Undervoltage detected!` asserts a firmware sticky-bit
transition that the driver labels; it prints no voltage and no threshold. Because it is edge-driven
on a two-second poll, its **absence is not the absence of an interruption** - a hard cut may reset the
board before the poll runs. A new `_BOOT_ID` with no preceding clean-shutdown record would be an
inference by the reader, not a source assertion, so nothing of the sort is derived here.

The UPS daemon does not close that gap either, and is not renamed to pretend it does: NUT defines
**no mains-failure event**. `ONBATT` says "UPS %s on battery" - the UPS's own report about which
source it is drawing from. It is not a mains failure, not a downstream computer power loss and not a
physical failed-power cause, and each row says so. (apcupsd's `powerout` / "Power failure." was
considered and not used: it is fired from the same on-battery status-bit edge, with no separate
line-fail bit, so its stronger wording is the producer's own inference rather than a distinct
observation.)

The documented export encodings, the two journal clocks and the bounds are reused by **calling** the
adopted `convert_journal_storage_errors` module rather than restating them, so those rules keep one
definition. That module is not modified.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

# This module calls the adopted journal converter rather than restating its documented encodings, so
# the repository root has to be importable when the file is run directly as a script as well as when
# it is imported as `scripts.convert_journal_power_thermal` by the tests.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.convert_journal_storage_errors import (CLOCKS, IDENTITY_BASIS, LIMIT, MAX_ENTRIES,
                                                    MAX_LINE, OUTPUT_LIMIT, SOURCE_FORMAT,
                                                    SOURCE_PIN, SOURCE_TIME_ABSENT,
                                                    SOURCE_TIME_PRESENT, _identity, _microseconds,
                                                    _text)

LINUX_PIN = "linux-v6.12-adc21867"
RPI_PIN = "raspberrypi-linux-70df9a55"
KERNEL_TRANSPORT = "kernel"
# `drivers/base/core.c` `__dev_printk()` at the Linux pin emits a `dev_*` line for a device as
# `"%s %s: %pV"` of `dev_driver_string(dev)`, `dev_name(dev)` and the message. `dev_driver_string()`
# can return an empty string, so the documented shape is `<driver> <name>: <message>` with exactly
# one space between the two fields. Anything that merely *ends* with a pinned sentence is not this
# producer's line: an arbitrary prefix is not accepted.
DEV_PREFIX = r"(?P<label>\S* \S+)"
# `kernel/reboot.c` begins with `#define pr_fmt(fmt) "reboot: " fmt`, so every `pr_*` line in that
# file carries that literal prefix. The bare sentence is NOT what the producer writes, and these
# lines have no device prefix, so they are matched as a whole message and never searched for inside
# a longer one.
REBOOT_PREFIX = "reboot: "
PATTERN_ASSERTIONS = (
    (re.compile(DEV_PREFIX + r": (?P<zone>.+): critical temperature reached"),
     "THERMAL", "CRITICAL_TEMPERATURE_TRIP_REACHED", "ENTERED", LINUX_PIN),
    (re.compile(DEV_PREFIX + r": Undervoltage detected!"),
     "POWER", "UNDERVOLTAGE_CONDITION_ENTERED", "ENTERED", RPI_PIN),
    (re.compile(DEV_PREFIX + r": Voltage normalised"),
     "POWER", "UNDERVOLTAGE_CONDITION_CLEARED", "CLEARED", RPI_PIN),
)
EXACT_ASSERTIONS = {
    REBOOT_PREFIX + "Hardware protection timed-out. Trying forced poweroff":
        ("UNKNOWN", "HARDWARE_PROTECTION_FORCED_POWEROFF", LINUX_PIN),
    REBOOT_PREFIX + "Hardware protection shutdown failed. Trying emergency restart":
        ("UNKNOWN", "HARDWARE_PROTECTION_EMERGENCY_RESTART", LINUX_PIN),
}
PROTECTION_SHUTDOWN = re.compile(
    re.escape(REBOOT_PREFIX) + r"HARDWARE PROTECTION shutdown \((?P<reason>.*)\)")
# Network UPS Tools `upsmon`, read at NUT `v2.8.2` -> `440ca2348e665abf3787c30bdcd9373b479f4efc`.
# `clients/upsmon.h` `notifylist[]` carries the stock texts `"UPS %s on line power"` and
# `"UPS %s on battery"`, where `%s` is `ups->sys` - the identifier the administrator wrote in
# upsmon.conf, not an authenticated device. `clients/upsmon.c` `ups_on_batt()` fires ONBATT only on
# the OL->OB change ("must have changed from OL to OB, so notify") and `ups_on_line()` fires ONLINE
# on the reverse, ignoring the first OL at startup.
#
# Two things make this producer different in kind from the kernel lines above, and both are declared
# rather than assumed. `do_notify()` formats
# `notifylist[i].msg ? notifylist[i].msg : notifylist[i].stockmsg`, and `NOTIFYMSG <type> <message>`
# in upsmon.conf replaces that text outright - the manual's own example is
# `NOTIFYMSG ONBATT "Someone pulled the plug on %s"`. Whether anything is written at all is
# `NOTIFYFLAG`: `notify()` reaches syslog only when `NOTIFY_SYSLOG` is set and does nothing under
# `NOTIFY_IGNORE`. So the text is administrator-supplied configuration, and a caller must DECLARE
# that the export came from the pinned release with the stock messages before these lines are read.
UPS_PRODUCERS = {"upsmon-2.8.2-stock-notifymsg": "nut-v2.8.2-440ca234"}
UPS_COMM = "upsmon"
UPS_ASSERTIONS = (
    (re.compile(r"UPS (?P<label>.+) on battery"),
     "UPS_REPORTED_ON_BATTERY", "ENTERED"),
    (re.compile(r"UPS (?P<label>.+) on line power"),
     "UPS_REPORTED_ON_LINE_POWER", "CLEARED"),
)
THERMAL_REASON = "Temperature too high"
REPORT_CLASS = "ASSERTED_EVENT_THE_PRODUCER_ITSELF_REPORTED"
REPORT_CLASS_BASIS = (
    "THIS_CONVERTER_EMITS_ONLY_AN_EVENT_THE_PRODUCER_ASSERTED_IT_NEVER_EMITS_A_MEASUREMENT_A_"
    "CONFIGURED_LIMIT_A_THRESHOLD_COMPARISON_OR_A_PHYSICAL_CAUSAL_DIAGNOSIS_AND_A_GAP_IN_"
    "OBSERVATIONS_IS_NEVER_READ_AS_A_CONDITION")
MATCH_BASIS = (
    "MATCHED_AGAINST_A_PINNED_PRODUCER_FORMAT_STRING_ON_AN_ENTRY_WHOSE_TRUSTED_TRANSPORT_IS_KERNEL_"
    "THESE_LINES_CARRY_NO_MESSAGE_ID_SO_TEXT_IS_THE_ONLY_CARRIER_WHICH_IS_WEAKER_PROVENANCE_THAN_A_"
    "STRUCTURED_IDENTIFIER_AND_IS_NOT_PROOF_OF_THE_PRODUCERS_VERSION")
NO_VALUE_PRINTED = (
    "NO_TEMPERATURE_AND_NO_VOLTAGE_IS_PRINTED_BY_THESE_PINNED_FORMAT_STRINGS_SO_NONE_IS_EMITTED_HERE_"
    "AND_NO_LIMIT_IS_READ_OR_COMPARED")
INTERRUPTION_NOT_ASSERTED = (
    "NO_PRODUCER_READ_HERE_ASSERTS_THAT_A_POWER_SUPPLY_WAS_INTERRUPTED_THE_UNDERVOLTAGE_LINE_ASSERTS_"
    "A_FIRMWARE_STICKY_BIT_TRANSITION_THE_DRIVER_LABELS_AND_BECAUSE_IT_IS_EDGE_DRIVEN_ON_A_TWO_SECOND_"
    "POLL_ITS_ABSENCE_IS_NOT_THE_ABSENCE_OF_AN_INTERRUPTION")
SHUTDOWN_REASON_BASIS = (
    "THE_REASON_IS_THE_CALLERS_OWN_STRING_SO_ONLY_THE_EXACT_PINNED_THERMAL_REASON_BINDS_THIS_"
    "SHUTDOWN_TO_THE_THERMAL_DOMAIN_ANY_OTHER_REASON_IS_RETAINED_VERBATIM_WITH_THE_DOMAIN_UNKNOWN_"
    "AND_AN_INITIATED_SHUTDOWN_IS_NOT_A_COMPLETED_ONE")
UPS_MATCH_BASIS = (
    "MATCHED_AGAINST_THE_STOCK_NOTIFY_TEXT_OF_A_DECLARED_UPSMON_RELEASE_ON_AN_ENTRY_WHOSE_TRUSTED_"
    "COMM_IS_UPSMON_THAT_TEXT_IS_A_DEFAULT_THE_ADMINISTRATOR_CAN_REPLACE_WITH_NOTIFYMSG_AND_WHOSE_"
    "LOGGING_AT_ALL_IS_CHOSEN_BY_NOTIFYFLAG_SO_THE_DECLARATION_IS_A_PREREQUISITE_AND_NOT_A_FIXED_"
    "PRODUCER_STRING_AND_A_PLAIN_USER_MESSAGE_WITH_THE_SAME_WORDS_IS_NOT_THIS_REPORT")
UPS_SUPPLY_BASIS = (
    "THE_UPS_OWN_REPORT_ABOUT_WHICH_SOURCE_IT_IS_DRAWING_FROM_THE_PINNED_PRODUCER_DEFINES_NO_MAINS_"
    "FAILURE_EVENT_AT_ALL_SO_ON_BATTERY_IS_NOT_RENAMED_HERE_IT_IS_NOT_A_MAINS_FAILURE_NOT_A_"
    "DOWNSTREAM_COMPUTER_POWER_LOSS_AND_NOT_A_PHYSICAL_FAILED_POWER_CAUSE")
UPS_LABEL_BASIS = (
    "THE_UPS_IDENTIFIER_THE_ADMINISTRATOR_WROTE_IN_UPSMON_CONF_CARRIED_AS_REPORTED_TEXT_AND_NEVER_AN_"
    "AUTHENTICATED_DEVICE_IDENTITY")
# Per origin: how the line was matched, and what the row must keep saying about interruption.
ORIGIN_BASES = {"KERNEL": None, "UPS": (UPS_MATCH_BASIS, UPS_SUPPLY_BASIS)}
TRANSITION_NOT_APPLICABLE = "NOT_A_TRANSITION_REPORT_THIS_PRODUCER_REPORTS_AN_ACTION_NOT_AN_EDGE"
DEVICE_LABEL_BASIS = (
    "THE_KERNELS_OWN_DEVICE_PREFIX_AND_ZONE_LABEL_EXACTLY_AS_WRITTEN_RETAINED_UNPARSED_BECAUSE_"
    "SPLITTING_IT_INTO_AN_IDENTITY_WOULD_INVENT_A_STRUCTURE_THE_LINE_DOES_NOT_DECLARE")
TRUSTED = (("_TRANSPORT", "journal_trusted_transport"), ("_BOOT_ID", "journal_trusted_boot_id"),
           ("_COMM", "journal_trusted_comm"), ("_KERNEL_SUBSYSTEM", "journal_trusted_kernel_subsystem"))
CLIENT = (("PRIORITY", "client_reported_priority"),)
COLUMNS = (["record_time_us", "record_time_basis", "clock_field_basis",
            "journal_reception_realtime_us", "journal_source_realtime_us", "source_time_status",
            "condition_domain", "reported_condition", "condition_transition", "report_class",
            "report_class_basis", "match_basis", "measured_value_status",
            "interruption_assertion", "declared_producer", "reported_device_label_hex",
            "device_label_basis",
            "reported_shutdown_reason_hex", "shutdown_reason_basis", "original_report_hex",
            "original_report_sha256", "original_report_bytes"]
           + [column for _, column in TRUSTED] + [column for _, column in CLIENT]
           + ["identity_encoding_notes", "identity_basis", "producer_pin", "source_pin",
              "record_sha256"])


def _hex(value):
    return "hex:" + value.encode("utf-8").hex()


def classify(message, *, ups_producer=None):
    """One pinned assertion, or None when this line is not one of them.

    Returns (domain, condition, transition, producer pin, device label, shutdown reason, origin).
    The UPS producer is read only when the caller has declared it, because its text is an
    administrator-replaceable default rather than a fixed producer string.
    """
    if ups_producer is not None:
        for pattern, condition, transition in UPS_ASSERTIONS:
            found = pattern.fullmatch(message)
            if found is not None:
                return ("POWER", condition, transition, UPS_PRODUCERS[ups_producer],
                        found.group("label"), "", "UPS")
    if message in EXACT_ASSERTIONS:
        domain, condition, pin = EXACT_ASSERTIONS[message]
        return domain, condition, TRANSITION_NOT_APPLICABLE, pin, "", "", "KERNEL"
    # `fullmatch`, not `search`: this producer writes no device prefix, so the pinned sentence must
    # BE the message. Searching would classify any longer line that merely contains it.
    shutdown = PROTECTION_SHUTDOWN.fullmatch(message)
    if shutdown is not None:
        reason = shutdown.group("reason")
        # The reason belongs to whichever subsystem called for the shutdown. Only the exact pinned
        # thermal reason binds it to the thermal domain; anything else keeps the reason and no domain.
        domain = "THERMAL" if reason == THERMAL_REASON else "UNKNOWN"
        return (domain, "HARDWARE_PROTECTION_SHUTDOWN_INITIATED", TRANSITION_NOT_APPLICABLE,
                LINUX_PIN, "", reason, "KERNEL")
    for pattern, domain, condition, transition, pin in PATTERN_ASSERTIONS:
        found = pattern.fullmatch(message)
        if found is not None:
            # The kernel's own `<driver> <name>` prefix, plus the zone label where the producer
            # writes one. Kept exactly as written and never split further into an identity.
            zone = found.groupdict().get("zone")
            label = found.group("label") + (f": {zone}" if zone else "")
            return domain, condition, transition, pin, label, "", "KERNEL"
    return None


def _readable_message(entry):
    """The MESSAGE as text plus why it is unusable, keeping absence apart from unreadability.

    An entry with **no** MESSAGE field is not the same as one whose MESSAGE the exporter withheld
    over its 4096-byte limit (`null`) or wrote as a byte-number array because it was not UTF-8. All
    three carry no readable sentence and so cannot be classified, but they are counted separately
    rather than collapsed, and none of them is guessed at or refused as malformed.
    """
    if "MESSAGE" not in entry:
        return None, "absent"
    value = entry["MESSAGE"]
    return (value, None) if isinstance(value, str) else (None, "unreadable")


def convert(text, *, clock, source_format=SOURCE_FORMAT, ups_producer=None):
    if source_format != SOURCE_FORMAT:
        raise ValueError("explicit supported export format required")
    if clock not in CLOCKS:
        raise ValueError("explicit supported journal clock required")
    if ups_producer is not None and ups_producer not in UPS_PRODUCERS:
        # The UPS text is an administrator-replaceable default, so the release and the fact that the
        # stock messages are in use are a declared prerequisite, never a guess from the words.
        raise ValueError("explicit supported UPS producer declaration required")
    field, basis, clock_basis = CLOCKS[clock]
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized export")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    report = {"source_pin": SOURCE_PIN, "linux_pin": LINUX_PIN, "raspberry_pi_pin": RPI_PIN,
              "declared_clock": clock, "clock_field": field, "clock_field_basis": clock_basis,
              "source_entries": 0, "selected_entries": 0, "entries_not_kernel_transport": 0,
              "entries_without_a_pinned_assertion": 0, "entries_without_a_message_field": 0,
              "entries_with_an_unreadable_message": 0,
              "entries_not_from_the_declared_ups_daemon": 0,
              "declared_ups_producer": ups_producer or "NOT_DECLARED_SO_NO_UPS_REPORT_IS_READ",
              "thermal_entries": 0, "power_entries": 0, "ups_entries": 0,
              "source_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
              "interruption_assertion": INTERRUPTION_NOT_ASSERTED,
              "measured_value_status": NO_VALUE_PRINTED}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode()) > MAX_LINE:
            raise ValueError(f"entry {number}: exceeds the line bound")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"entry {number}: not one JSON object per line") from error
        if not isinstance(entry, dict):
            raise ValueError(f"entry {number}: the export writes one JSON object per line")
        report["source_entries"] += 1
        # The bound is on entries READ, not on rows written. Checking it only where a row is emitted
        # would let an export of unselected entries run past it unbounded, because every `continue`
        # below skips the check.
        if report["source_entries"] > MAX_ENTRIES:
            raise ValueError("saved export exceeds the entry bound")
        message, unusable = _readable_message(entry)
        if message is None:
            report["entries_without_a_message_field" if unusable == "absent"
                   else "entries_with_an_unreadable_message"] += 1
            continue
        found = classify(message, ups_producer=ups_producer)
        if found is None:
            report["entries_without_a_pinned_assertion"] += 1
            continue
        domain, condition, transition, producer_pin, label, reason, origin = found
        # Each origin has its own trusted binding, and the sentence alone never carries a row.
        if origin == "KERNEL" and _text(entry, "_TRANSPORT", number) != KERNEL_TRANSPORT:
            # Without the trusted kernel transport the sentence is not a kernel assertion, whatever
            # it says. Counted, never selected.
            report["entries_not_kernel_transport"] += 1
            continue
        if origin == "UPS" and _text(entry, "_COMM", number) != UPS_COMM:
            # The same words from any other process are not this daemon's report.
            report["entries_not_from_the_declared_ups_daemon"] += 1
            continue
        origin_bases = ORIGIN_BASES[origin]
        match_basis, interruption = origin_bases or (MATCH_BASIS, INTERRUPTION_NOT_ASSERTED)
        label_basis = UPS_LABEL_BASIS if origin == "UPS" else DEVICE_LABEL_BASIS
        received = _microseconds(entry, "__REALTIME_TIMESTAMP", number,
                                 required=field == "__REALTIME_TIMESTAMP")
        source_time = _microseconds(entry, "_SOURCE_REALTIME_TIMESTAMP", number,
                                    required=field == "_SOURCE_REALTIME_TIMESTAMP")
        chosen = received if field == "__REALTIME_TIMESTAMP" else source_time
        raw = message.encode()
        row = {"record_time_us": chosen, "record_time_basis": basis,
               "clock_field_basis": clock_basis,
               "journal_reception_realtime_us": "" if received is None else received,
               "journal_source_realtime_us": "" if source_time is None else source_time,
               "source_time_status": SOURCE_TIME_PRESENT if source_time is not None
                                     else SOURCE_TIME_ABSENT,
               "condition_domain": domain, "reported_condition": condition,
               "condition_transition": transition, "report_class": REPORT_CLASS,
               "report_class_basis": REPORT_CLASS_BASIS,
               "match_basis": match_basis, "measured_value_status": NO_VALUE_PRINTED,
               "interruption_assertion": interruption,
               "declared_producer": ups_producer if origin == "UPS" else "",
               "reported_device_label_hex": _hex(label) if label else "",
               "device_label_basis": (label_basis if label else ""),
               "reported_shutdown_reason_hex": _hex(reason) if reason else "",
               "shutdown_reason_basis": SHUTDOWN_REASON_BASIS if reason else "",
               # The report itself is the evidence here, so it is retained whole rather than reduced
               # to a digest, with the digest and length beside it.
               "original_report_hex": _hex(message),
               "original_report_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
               "original_report_bytes": len(raw),
               "identity_basis": IDENTITY_BASIS, "producer_pin": producer_pin,
               "source_pin": SOURCE_PIN,
               "record_sha256": "sha256:" + hashlib.sha256(line.encode()).hexdigest()}
        notes = []
        for name, column in TRUSTED + CLIENT:
            row[column], note = _identity(entry, name, number)
            if note is not None:
                notes.append(f"{name}:{note}")
        row["identity_encoding_notes"] = " ".join(notes)
        writer.writerow(row)
        report["selected_entries"] += 1
        report["thermal_entries"] += domain == "THERMAL"
        report["power_entries"] += domain == "POWER"
        report["ups_entries"] += origin == "UPS"
        if output.tell() > OUTPUT_LIMIT:
            raise ValueError("converted export exceeds bound")
    if not report["selected_entries"]:
        raise ValueError("no entry carries a pinned kernel power or thermal assertion")
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="saved `journalctl -o json` export")
    parser.add_argument("output_directory", type=Path, help="new directory only")
    parser.add_argument("--clock", required=True, choices=tuple(CLOCKS),
                        help="which documented journal timestamp this conversion uses")
    parser.add_argument("--source-format", required=True, choices=(SOURCE_FORMAT,))
    parser.add_argument("--ups-reports", choices=tuple(UPS_PRODUCERS), default=None,
                        help="declare that the export carries a UPS daemon's STOCK notify texts at "
                             "that release; without this declaration no UPS line is read, because "
                             "the text is an administrator-replaceable default")
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        saved = args.input.read_bytes()
        if len(saved) > LIMIT:
            raise ValueError("input exceeds bound")
        output, report = convert(saved.decode("utf-8"), clock=args.clock,
                                 source_format=args.source_format,
                                 ups_producer=args.ups_reports)
        if report["source_sha256"] != "sha256:" + hashlib.sha256(saved).hexdigest():
            raise ValueError("reported digest does not bind the input bytes")
        args.output_directory.mkdir()
        (args.output_directory / "observations.csv").write_text(output, encoding="utf-8")
        (args.output_directory / "source.json").write_bytes(saved)
        (args.output_directory / "report.json").write_text(json.dumps(report, indent=2),
                                                           encoding="utf-8")
    except (ValueError, OSError, UnicodeDecodeError) as failure:
        print(f"journal power/thermal conversion failed; no successful conversion claim: {failure}",
              file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
