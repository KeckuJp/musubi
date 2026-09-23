#!/usr/bin/env python3
"""Saved `journalctl -o json` entries reporting a storage/write error -> CSV for TelemetryCsvReader.

Passive import of an export an engineer already saved. No journal is opened, no device or filesystem is
inspected, `journalctl` is never run, and nothing here diagnoses hardware.

Export shape, read at systemd tag `v257` (`70bae7648f2c18010187c9cf20093155eaa26029`):
`docs/JOURNAL_EXPORT_FORMATS.md` and `man/journalctl.xml` state that `-o json` writes one JSON object per
line, that values are strings except a field over 4096 bytes (`null`), a non-unique field (JSON array) and
a field with non-printable/non-UTF8 bytes (array of unsigned byte numbers), and that field order is
undefined. `man/systemd.journal-fields.xml` states that `ERRNO=` "contains the numeric value of errno(3)
formatted as a decimal string", that `__REALTIME_TIMESTAMP` is the CLOCK_REALTIME instant "at the point in
time the entry was received by the journal" in microseconds since the epoch UTC, that
`_SOURCE_REALTIME_TIMESTAMP` is "the earliest trusted timestamp of the message" on the same clock, and that
`_`-prefixed fields are trusted because they "cannot be altered by client code" - `ERRNO` is not one of
them.

Both timestamps are therefore journal-generated, not client claims: `__REALTIME_TIMESTAMP` is an **address
field** of the entry and `_SOURCE_REALTIME_TIMESTAMP` is documented inside "Trusted Journal Fields".
"Trusted" there means journald added it and client code cannot alter it; it is **not** authentication, and
CLOCK_REALTIME is a settable wall clock, so neither value is a verified absolute instant. The adopted
telemetry-CSV reader admits only `unknown`/`boot_relative` for this format, so no absolute-clock basis is
declared to it either way.

Error numbers come from the declared kernel ABI (Linux tag `v6.12`,
`adc218676eef25575469234709c2d87185ca223a`, `include/uapi/asm-generic/errno-base.h`): `EIO 5`,
`ENOSPC 28`, `EROFS 30`. Any other number keeps its value and is classed unrecognised.

What this does NOT establish: that a write was attempted or that it failed. No field in that schema names
the operation or its target, so the assertion stays unestablished unless the entry carries the one catalog
identity that documents a limited store (`SD_MESSAGE_TRUNCATED_CORE`). `MESSAGE` prose is never parsed.

Nothing an entry carried is dropped: fields this converter does not interpret are counted and named in the
row and preserved in a paired local sidecar (`--record-sidecar`, required), each record bound to its row by
`record_sha256`. `MESSAGE` is the one deliberate exception - withheld by design and represented only by
status, digest and byte length - and a selected identity field whose encoding is not a single string keeps an
explicit note instead of silently becoming an empty cell.

Two digests with two different scopes, stated rather than conflated: `source_sha256` is over the **whole file
as saved**, so it retains every byte including CRLF line terminators, while `record_sha256` is over **one
record's own bytes excluding its line terminator** (the line as split, so neither LF nor CRLF is hashed). The
sidecar's `preserved_fields` is a **re-serialised** JSON view of that record: every field value is preserved,
but key order and whitespace are this converter's, so it is not a copy of the record's raw bytes - the raw
bytes are identified by `record_sha256`, not reproduced.
"""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

try:
    from scripts.convert_position_csv import unique_object, invalid_constant
except ModuleNotFoundError:
    from convert_position_csv import unique_object, invalid_constant

LIMIT = 16 * 1024 * 1024
OUTPUT_LIMIT = 128 * 1024 * 1024
MAX_ENTRIES = 200_000
MAX_LINE = 64 * 1024
SOURCE_FORMAT = "journalctl-json-v257"
SOURCE_PIN = "systemd-v257-70bae764"
ERRNO_ABI = "linux-6.12-asm-generic"
ERRNO_ABI_PIN = "linux-v6.12-adc21867"
# `include/uapi/asm-generic/errno-base.h` at the declared ABI. The set is deliberately finite: adding one
# means adding its constant from the same header, not widening a pattern.
ERRNO_NAMES = {5: "EIO", 28: "ENOSPC", 30: "EROFS"}
ERRNO_CLASSES = {5: "REPORTED_IO_ERROR", 28: "REPORTED_NO_SPACE_LEFT_ON_DEVICE",
                 30: "REPORTED_READ_ONLY_FILE_SYSTEM"}
UNRECOGNISED = "REPORTED_ERRNO_UNRECOGNISED"
# errno values are small positive integers in this ABI; a decimal string outside that is not an errno.
MAX_ERRNO = 4095
# The only catalog identity in `sd-messages.h` + `catalog/systemd.catalog.in` whose documented meaning is
# about storing bytes: "Only the first @SIZE_LIMIT@ bytes were saved" for a core file. That is a
# CONFIGURED LIMIT, not a disk fault and not an errno failure.
TRUNCATED_CORE_ID = "5aadd8e954dc4b1a8c954d63fd9e1137"
TRUNCATED_CORE_CLASS = "REPORTED_TRUNCATED_STORE_BY_CONFIGURED_SIZE_LIMIT"
WRITE_UNESTABLISHED = "NOT_ESTABLISHED_NO_JOURNAL_FIELD_NAMES_THE_OPERATION_OR_TARGET"
WRITE_TRUNCATED_STORE = "REPORTED_TRUNCATED_STORE_DOCUMENTED_BY_CATALOG_MESSAGE_ID"
# Both are journal-generated: the first is an address field of the entry, the second a documented trusted
# field. Neither is authenticated and CLOCK_REALTIME is settable, so neither is a verified instant.
CLOCKS = {"journal_reception_realtime":
          ("__REALTIME_TIMESTAMP", "HOST_RECEIVED",
           "JOURNALD_RECEPTION_CLOCK_REALTIME_US_ADDRESS_FIELD_NOT_THE_EARLIEST_TRUSTED_SOURCE_TIME"),
          "source_realtime":
          ("_SOURCE_REALTIME_TIMESTAMP", "UNKNOWN",
           "EARLIEST_TRUSTED_CLOCK_REALTIME_US_JOURNALD_ADDED_TRUSTED_FIELD_NOT_AUTHENTICATED_"
           "AND_NOT_A_VERIFIED_ABSOLUTE_INSTANT")}
SOURCE_TIME_PRESENT = "REPORTED_EARLIEST_TRUSTED_SOURCE_TIME"
SOURCE_TIME_ABSENT = "ABSENT_FROM_THIS_ENTRY_NO_TRUSTED_SOURCE_TIME_DIFFERENT_FROM_RECEPTION"
IDENTITY_BASIS = "JOURNALD_TRUSTED_FIELDS_REPORTED_NOT_CRYPTOGRAPHIC_OR_RUNTIME_IDENTITY"
# A provided identity value is never silently erased: when the documented encoding is not a single string
# the cell stays empty and the reason is named here, with the value itself preserved in the sidecar record.
WITHHELD_NULL = "WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL"
NON_UTF8_BYTES = "NON_UTF8_BYTE_ARRAY_VALUE_IN_SIDECAR"
MULTIPLE_VALUES = "MULTIPLE_VALUES_IN_SIDECAR"
SIDECAR_FORMAT = "journal-storage-errors-record-sidecar-v1"
# Applications may define new fields freely, so an unrecognised field is preserved, never interpreted.
MAX_FIELDS = 256
MAX_NAMES_IN_CELL = 32
ERRNO_BASIS = "CLIENT_SUPPLIED_ERRNO_FIELD_AT_DECLARED_KERNEL_ABI_NOT_A_TRUSTED_FIELD"
CATALOG_BASIS = "DOCUMENTED_CATALOG_MESSAGE_ID_AT_THE_PINNED_RELEASE"
DECIMAL = re.compile(r"[0-9]{1,19}\Z")
MESSAGE_ID = re.compile(r"[0-9a-f]{32}\Z")
# The common reader parses the time cell as i64 before widening it.
MAX_TIME_US = 2 ** 63 - 1
# Trusted (`_`-prefixed) fields carried as reported context, plus the two client-supplied labels that are
# marked as such in the column names. `_KERNEL_DEVICE` exists on kernel entries only.
TRUSTED = (("_COMM", "journal_trusted_comm"), ("_SYSTEMD_UNIT", "journal_trusted_unit"),
           ("_PID", "journal_trusted_pid"), ("_BOOT_ID", "journal_trusted_boot_id"),
           ("_KERNEL_DEVICE", "journal_trusted_kernel_device"))
CLIENT = (("PRIORITY", "client_reported_priority"),
          ("SYSLOG_IDENTIFIER", "client_reported_syslog_identifier"))
COLUMNS = ["record_time_us", "record_time_basis", "clock_field_basis",
           "journal_reception_realtime_us", "journal_source_realtime_us", "source_time_status",
           "storage_event_class", "storage_event_basis", "reported_errno", "reported_errno_name",
           "write_operation_assertion", "reported_message_id", "message_text_status",
           "message_text_sha256", "message_text_bytes"]
COLUMNS += [column for _, column in TRUSTED] + [column for _, column in CLIENT]
COLUMNS += ["identity_encoding_notes", "journal_cursor", "record_sha256", "record_bytes",
            "unknown_field_count", "unknown_field_names", "identity_basis", "source_pin", "errno_abi_pin"]
# Everything this converter interprets. Anything else in an entry is an unknown field: counted, named and
# preserved verbatim in the paired sidecar, never dropped and never given a meaning here.
INTERPRETED = ({"__CURSOR", "__REALTIME_TIMESTAMP", "_SOURCE_REALTIME_TIMESTAMP", "ERRNO", "MESSAGE_ID",
                "MESSAGE"} | {name for name, _ in TRUSTED} | {name for name, _ in CLIENT})


def _text(entry, name, number):
    """One documented field as text, or None when absent.

    An array means the entry carried the field more than once and a `null` means the exporter withheld a
    field over its 4096-byte limit; neither can be read as a single value, so both are refused here rather
    than collapsed into a guess. Callers that tolerate withholding check the raw value themselves.
    """
    if name not in entry:
        return None
    value = entry[name]
    if isinstance(value, str):
        return value
    if value is None:
        raise ValueError(f"entry {number}: {name} was withheld by the exporter's size limit")
    if isinstance(value, list):
        raise ValueError(f"entry {number}: {name} carries more than one value or non-UTF8 bytes")
    raise ValueError(f"entry {number}: {name} is not the exporter's string encoding")


def _microseconds(entry, name, number, *, required):
    token = _text(entry, name, number)
    if token is None:
        if required:
            raise ValueError(f"entry {number}: the declared clock needs {name}, which this entry lacks")
        return None
    if not DECIMAL.fullmatch(token) or int(token) > MAX_TIME_US:
        raise ValueError(f"entry {number}: {name} is not the documented decimal microsecond string")
    return int(token)


def _identity(entry, name, number):
    """One selected identity field as a cell plus a note when the encoding is not a single string.

    Absence is distinguishable from withholding and from ambiguity: an absent field gets no note, while a
    `null` (the exporter's size limit), a byte-number array (non-UTF8) and a multi-valued field each get
    their own note and keep their real value in the paired sidecar record. An encoding the export does not
    document for a field value is refused rather than flattened.
    """
    if name not in entry:
        return "", None
    value = entry[name]
    if isinstance(value, str):
        return value, None
    if value is None:
        return "", WITHHELD_NULL
    if isinstance(value, list):
        if value and all(type(item) is int and 0 <= item <= 255 for item in value):
            return "", NON_UTF8_BYTES
        if all(isinstance(item, (str, list)) or item is None for item in value):
            return "", MULTIPLE_VALUES
        raise ValueError(f"entry {number}: {name} is not a documented field encoding")
    raise ValueError(f"entry {number}: {name} is not a documented field encoding")


def _message_status(entry, number):
    """Account for the MESSAGE field without reading it as prose.

    The documented encodings are all kept apart: a string, a `null` the exporter substituted for a field
    over 4096 bytes, and an array of byte numbers for non-UTF8 content. Only a digest and a length leave
    here, so the accounting never carries log text.
    """
    if "MESSAGE" not in entry:
        return "ABSENT", "", ""
    value = entry["MESSAGE"]
    if value is None:
        return "WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL", "", ""
    if isinstance(value, str):
        raw = value.encode()
        return "PRESENT_UTF8_TEXT_NOT_PARSED", "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)
    if isinstance(value, list):
        if not all(type(item) is int and 0 <= item <= 255 for item in value):
            raise ValueError(f"entry {number}: MESSAGE is not the documented byte-number array")
        raw = bytes(value)
        return "NON_UTF8_BYTE_ARRAY_NOT_PARSED", "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)
    raise ValueError(f"entry {number}: MESSAGE is not a documented encoding")


def convert(text, *, clock, source_format=SOURCE_FORMAT, errno_abi=ERRNO_ABI):
    if source_format != SOURCE_FORMAT:
        raise ValueError("explicit supported source format required")
    if errno_abi != ERRNO_ABI:
        # The numbers only mean anything at a stated ABI; a different one is refused, never assumed equal.
        raise ValueError("explicit supported errno ABI required")
    if clock not in CLOCKS:
        raise ValueError("explicit supported clock selection required")
    clock_field, clock_basis, clock_field_basis = CLOCKS[clock]
    if not text or len(text.encode()) > LIMIT:
        raise ValueError("empty or oversized export")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, COLUMNS, lineterminator="\n")
    writer.writeheader()
    sidecar = io.StringIO(newline="")
    report = {"source_pin": SOURCE_PIN, "errno_abi_pin": ERRNO_ABI_PIN, "clock": clock_basis,
              "clock_basis": clock_field_basis, "entries": 0, "selected_entries": 0,
              "entries_without_selector": 0, "unrecognised_errno_entries": 0,
              "withheld_message_fields": 0, "absent_source_time_entries": 0,
              "ambiguous_identity_fields": 0, "unknown_fields_total": 0, "sidecar_records": 0,
              "sidecar_format": SIDECAR_FORMAT,
              "source_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
              "errno_basis": ERRNO_BASIS, "identity_basis": IDENTITY_BASIS,
              "write_assertion_default": WRITE_UNESTABLISHED}
    sidecar.write(json.dumps(
        {"sidecar_format": SIDECAR_FORMAT, "source_pin": SOURCE_PIN, "errno_abi_pin": ERRNO_ABI_PIN,
         "source_sha256": report["source_sha256"], "record_time_basis": clock_basis,
         "clock_field_basis": clock_field_basis,
         "message_withheld_by_design": "MESSAGE text is never copied here; only its status, digest and "
                                       "byte length. Every other field value is preserved.",
         "record_sha256_scope": "sha256 of one record's own bytes EXCLUDING its line terminator; "
                                "source_sha256 is the whole saved file INCLUDING CRLF",
         "preserved_fields_scope": "re-serialised JSON: every field value preserved, key order and "
                                   "whitespace are this converter's, so not the record's raw bytes"},
        sort_keys=True) + "\n")
    cursors = set()
    rows = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode()) > MAX_LINE:
            raise ValueError(f"entry {number}: exceeds the line bound")
        try:
            entry = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except json.JSONDecodeError as error:
            raise ValueError(f"entry {number}: not one complete JSON object per line ({error.msg})")
        if not isinstance(entry, dict):
            raise ValueError(f"entry {number}: the export writes one JSON object per line")
        report["entries"] += 1
        if report["entries"] > MAX_ENTRIES:
            raise ValueError("export exceeds the entry bound")
        cursor = _text(entry, "__CURSOR", number)
        if cursor is not None:
            if cursor in cursors:
                # One cursor addresses exactly one entry, so a repeat means overlapping exports were
                # concatenated; that is a corrupt input, not two observations.
                raise ValueError(f"entry {number}: __CURSOR repeats an earlier entry")
            cursors.add(cursor)
        message_id = _text(entry, "MESSAGE_ID", number)
        if message_id is not None and not MESSAGE_ID.fullmatch(message_id):
            raise ValueError(f"entry {number}: MESSAGE_ID is not the documented 128-bit lower-case hex")
        errno_token = _text(entry, "ERRNO", number)
        truncated_store = message_id == TRUNCATED_CORE_ID
        if errno_token is None and not truncated_store:
            # Not a reported error entry: counted, never decoded and never described as inspected.
            report["entries_without_selector"] += 1
            continue
        reported_errno = ""
        errno_name = ""
        if errno_token is not None:
            if not DECIMAL.fullmatch(errno_token) or not 1 <= int(errno_token) <= MAX_ERRNO:
                raise ValueError(f"entry {number}: ERRNO is not the documented decimal errno string")
            value = int(errno_token)
            reported_errno = value
            errno_name = ERRNO_NAMES.get(value, "")
            event_class = ERRNO_CLASSES.get(value, UNRECOGNISED)
            event_basis = ERRNO_BASIS
            if value not in ERRNO_CLASSES:
                report["unrecognised_errno_entries"] += 1
        else:
            event_class = TRUNCATED_CORE_CLASS
            event_basis = CATALOG_BASIS
        reception = _microseconds(entry, "__REALTIME_TIMESTAMP", number,
                                 required=clock_field == "__REALTIME_TIMESTAMP")
        source_time = _microseconds(entry, "_SOURCE_REALTIME_TIMESTAMP", number,
                                   required=clock_field == "_SOURCE_REALTIME_TIMESTAMP")
        if source_time is None:
            report["absent_source_time_entries"] += 1
        status, digest, length = _message_status(entry, number)
        if status == "WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL":
            report["withheld_message_fields"] += 1
        row = {"record_time_us": reception if clock_field == "__REALTIME_TIMESTAMP" else source_time,
               "record_time_basis": clock_basis, "clock_field_basis": clock_field_basis,
               "journal_reception_realtime_us": "" if reception is None else reception,
               "journal_source_realtime_us": "" if source_time is None else source_time,
               "source_time_status": (SOURCE_TIME_PRESENT if source_time is not None
                                      else SOURCE_TIME_ABSENT),
               "storage_event_class": event_class, "storage_event_basis": event_basis,
               "reported_errno": reported_errno, "reported_errno_name": errno_name,
               # The schema names no operation and no target, so a write is not asserted from an errno.
               "write_operation_assertion": (WRITE_TRUNCATED_STORE if truncated_store
                                             else WRITE_UNESTABLISHED),
               "reported_message_id": message_id or "", "message_text_status": status,
               "message_text_sha256": digest, "message_text_bytes": length,
               "journal_cursor": cursor or "", "identity_basis": IDENTITY_BASIS,
               "source_pin": SOURCE_PIN, "errno_abi_pin": ERRNO_ABI_PIN}
        notes = []
        for name, column in TRUSTED + CLIENT:
            cell, note = _identity(entry, name, number)
            row[column] = cell
            if note is not None:
                notes.append(f"{column}={note}")
                report["ambiguous_identity_fields"] += 1
        row["identity_encoding_notes"] = ";".join(notes)
        if len(entry) > MAX_FIELDS:
            raise ValueError(f"entry {number}: exceeds the field-count bound")
        unknown = sorted(name for name in entry if name not in INTERPRETED)
        report["unknown_fields_total"] += len(unknown)
        shown = unknown[:MAX_NAMES_IN_CELL]
        row["unknown_field_count"] = len(unknown)
        # Names only: every unknown VALUE stays in the sidecar, so no field content lands in a cell.
        row["unknown_field_names"] = ";".join(shown) + (
            f";+{len(unknown) - len(shown)}_more" if len(unknown) > len(shown) else "")
        # `splitlines` already removed the terminator, so this digest is of the record's own bytes without
        # LF or CRLF; the whole-file digest in the report is the one that retains them.
        raw = line.encode()
        row["record_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        row["record_bytes"] = len(raw)
        writer.writerow(row)
        # The sidecar is the preserving half of the pair: every field value except MESSAGE, which is
        # withheld by design and represented only by its digest, status and length. It is re-serialised
        # JSON, not the record's raw bytes; `record_sha256` is what identifies those bytes.
        sidecar.write(json.dumps(
            {"row": rows + 1, "entry_number": number, "record_sha256": row["record_sha256"],
             "record_bytes": len(raw), "journal_cursor": cursor,
             "preserved_fields": {name: value for name, value in entry.items() if name != "MESSAGE"},
             "message_withheld_by_design": {"status": status, "sha256": digest or None,
                                            "bytes": length if length != "" else None},
             "identity_encoding_notes": notes, "unknown_field_names": unknown},
            sort_keys=True) + "\n")
        report["selected_entries"] += 1
        report["sidecar_records"] += 1
        rows += 1
        if output.tell() > OUTPUT_LIMIT or sidecar.tell() > OUTPUT_LIMIT:
            raise ValueError("output exceeds the bound")
    if not rows:
        raise ValueError("no entry reported an error code or a documented limited store")
    return output.getvalue(), report, sidecar.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-format", required=True, choices=(SOURCE_FORMAT,))
    parser.add_argument("--errno-abi", required=True, choices=(ERRNO_ABI,),
                        help="the kernel ABI whose constants the numbers are read against")
    parser.add_argument("--clock", required=True, choices=tuple(CLOCKS),
                        help="which documented journal timestamp becomes the record time")
    parser.add_argument("--report", type=Path, help="write the accounting as JSON to this new file")
    # Required, not optional: the rows say MULTIPLE_VALUES_IN_SIDECAR / NON_UTF8_BYTE_ARRAY_VALUE_IN_SIDECAR
    # and the accounting reports sidecar_records, so a run that wrote no sidecar would promise preserved
    # values that no artifact holds. The pair is the output.
    parser.add_argument("--record-sidecar", required=True, type=Path,
                        help="REQUIRED: write the paired per-record sidecar (JSONL) to this new file - every "
                             "field preserved except MESSAGE, which stays a digest, and each record bound "
                             "to its row by record_sha256")
    args = parser.parse_args()
    written = []
    try:
        if args.input.stat().st_size > LIMIT:
            raise ValueError("input exceeds bound")
        with args.input.open("rb") as stream:
            data = stream.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("input exceeds bound")
        # Bytes, then a strict decode: the digest must bind the saved export, not a re-encoded copy.
        text = data.decode("utf-8")
        output, report, sidecar = convert(text, clock=args.clock, source_format=args.source_format,
                                         errno_abi=args.errno_abi)
        if report["source_sha256"] != "sha256:" + hashlib.sha256(data).hexdigest():
            raise ValueError("reported digest does not bind the input bytes")
        summary = json.dumps(report, sort_keys=True)
        # Check every target before publishing any of them, so a sidecar or report that cannot be written
        # never leaves a CSV behind claiming preserved values that no file holds. Existing files are never
        # touched, and two targets may not be the same path.
        targets = [("converted CSV", args.output), ("record sidecar", args.record_sidecar)]
        if args.report is not None:
            targets.append(("accounting report", args.report))
        seen = {}
        for label, target in targets:
            if target.exists():
                raise ValueError(f"the {label} target already exists; this importer never overwrites")
            key = os.path.realpath(target)
            if key in seen:
                raise ValueError(f"the {label} and the {seen[key]} target are the same path")
            seen[key] = label
        # The CSV is written last: if the sidecar or the report fails, no CSV is published at all, and any
        # file this run did create is removed again. Nothing pre-existing is opened or deleted.
        for label, target, body in (("record sidecar", args.record_sidecar, sidecar),
                                    ("accounting report", args.report,
                                     None if args.report is None else summary + "\n"),
                                    ("converted CSV", args.output, output)):
            if body is None:
                continue
            with target.open("x", encoding="utf-8") as stream:
                written.append(target)
                stream.write(body)
        # Counts, pins, bases and the digest only - never an entry, a cursor or log text.
        print(summary, file=sys.stderr)
    except (OSError, ValueError, csv.Error) as error:
        for target in written:
            try:
                target.unlink()
            except OSError:
                pass
        parser.exit(2, f"journal storage error import failed; no successful conversion claim: {error}\n")


if __name__ == "__main__":
    main()
