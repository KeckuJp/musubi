"""Authored `journalctl -o json` exports, shaped exactly as the documented format produces them.

Shape and field meaning come from systemd v257 `70bae7648f2c18010187c9cf20093155eaa26029`
(`docs/JOURNAL_EXPORT_FORMATS.md`, `man/journalctl.xml`, `man/systemd.journal-fields.xml`,
`src/systemd/sd-messages.h`, `catalog/systemd.catalog.in`) and the error numbers from Linux v6.12
`adc218676eef25575469234709c2d87185ca223a` `include/uapi/asm-generic/errno-base.h`.

No journal is read, `journalctl` is never run and no real or vendor log body is used: every entry here is
authored, and the message texts are invented for this test.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_journal_storage_errors import (ERRNO_ABI, SOURCE_FORMAT, TRUNCATED_CORE_ID, convert)

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/storage-reported-errors"
RECEPTION_PROFILE = CASE / "reception-profile.toml"
SOURCE_TIME_PROFILE = CASE / "source-time-profile.toml"
SCRIPT = ROOT / "scripts/convert_journal_storage_errors.py"
BOOT = "3f1c5d9e2a7b41c8bd0e6f4a9c3d2e10"


def entry(index, **fields):
    """One exported entry: the addressing fields journalctl always prints, plus the given fields."""
    base = {"__CURSOR": f"s=1a2b;i={index:x};b={BOOT};m=4fc72436e;t=4c508a72423d9;p=system.journal",
            "__REALTIME_TIMESTAMP": str(1_789_000_000_000_000 + index * 1_000_000),
            "__MONOTONIC_TIMESTAMP": str(21_415_215_982 + index * 1_000_000),
            "_BOOT_ID": BOOT, "_TRANSPORT": "journal"}
    base.update(fields)
    return json.dumps(base)


def errno_entry(index, errno, **fields):
    fields.setdefault("MESSAGE", f"authored synthetic entry {index}")
    fields.setdefault("_COMM", "authored-writer")
    fields.setdefault("PRIORITY", "3")
    return entry(index, ERRNO=str(errno), **fields)


class JournalStorageErrorTests(unittest.TestCase):
    def rows_of(self, text, **kwargs):
        kwargs.setdefault("clock", "journal_reception_realtime")
        output, report, sidecar = convert(text, **kwargs)
        self.sidecar = [json.loads(line) for line in sidecar.splitlines()]
        return list(csv.DictReader(io.StringIO(output))), report, output

    def observations(self, output, profile):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "observations.csv"
            source.write_text(output)
            data = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(source), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
        self.assertEqual(data["platform_domain"], "Unknown", "no platform is inferred from a log export")
        return data


    def test_each_reported_error_number_reaches_its_own_class(self):
        text = "\n".join([
            errno_entry(1, 28, _SYSTEMD_UNIT="authored-recorder.service", _PID="4242"),
            errno_entry(2, 30, _COMM="authored-rotator"),
            errno_entry(3, 5, SYSLOG_IDENTIFIER="authored-identifier"),
            errno_entry(4, 122),  # EDQUOT at this ABI: a real number this importer does not map
            entry(5, MESSAGE="authored entry without an error code", _COMM="authored-writer"),
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertEqual((report["entries"], report["selected_entries"],
                          report["entries_without_selector"]), (5, 4, 1))
        self.assertEqual([r["storage_event_class"] for r in rows],
                         ["REPORTED_NO_SPACE_LEFT_ON_DEVICE", "REPORTED_READ_ONLY_FILE_SYSTEM",
                          "REPORTED_IO_ERROR", "REPORTED_ERRNO_UNRECOGNISED"])
        self.assertEqual([r["reported_errno"] for r in rows], ["28", "30", "5", "122"])
        self.assertEqual([r["reported_errno_name"] for r in rows], ["ENOSPC", "EROFS", "EIO", ""])
        self.assertEqual(report["unrecognised_errno_entries"], 1, "the unmapped number keeps its value")
        self.assertEqual({r["write_operation_assertion"] for r in rows},
                         {"NOT_ESTABLISHED_NO_JOURNAL_FIELD_NAMES_THE_OPERATION_OR_TARGET"})
        self.assertEqual([r["record_time_us"] for r in rows],
                         ["1789000001000000", "1789000002000000", "1789000003000000",
                          "1789000004000000"])
        self.assertEqual({r["record_time_basis"] for r in rows}, {"HOST_RECEIVED"})
        self.assertEqual(rows[0]["journal_trusted_unit"], "authored-recorder.service")
        self.assertEqual(rows[0]["journal_trusted_pid"], "4242")
        self.assertEqual(rows[1]["journal_trusted_comm"], "authored-rotator")
        self.assertEqual(rows[2]["client_reported_syslog_identifier"], "authored-identifier")
        self.assertEqual(rows[0]["client_reported_priority"], "3")
        self.assertEqual(rows[0]["source_pin"], "systemd-v257-70bae764")
        self.assertEqual(rows[0]["errno_abi_pin"], "linux-v6.12-adc21867")
        self.assertIn("NOT_A_TRUSTED_FIELD", rows[0]["storage_event_basis"])
        self.assertIn("NOT_CRYPTOGRAPHIC", rows[0]["identity_basis"])
        self.assertEqual(rows[0]["message_text_status"], "PRESENT_UTF8_TEXT_NOT_PARSED")
        self.assertEqual(rows[0]["message_text_bytes"], "26")
        self.assertNotIn("authored synthetic entry", rows[0]["message_text_sha256"])


    def test_the_documented_limited_store_and_sender_clock_variation(self):
        text = "\n".join([
            entry(11, MESSAGE_ID=TRUNCATED_CORE_ID, MESSAGE="authored truncated store notice",
                  _COMM="authored-coredump", _SOURCE_REALTIME_TIMESTAMP="1789500000000000"),
            errno_entry(12, 5, _KERNEL_DEVICE="b8:0", _COMM="authored-kernel",
                        _SOURCE_REALTIME_TIMESTAMP="1789500001000000", MESSAGE=None),
            errno_entry(13, 28, _SOURCE_REALTIME_TIMESTAMP="1789500002000000",
                        MESSAGE=[104, 105, 7, 200, 201]),
        ]) + "\n"
        rows, report, _ = self.rows_of(text, clock="source_realtime")
        self.assertEqual((report["entries"], report["selected_entries"]), (3, 3))
        self.assertEqual(rows[0]["storage_event_class"],
                         "REPORTED_TRUNCATED_STORE_BY_CONFIGURED_SIZE_LIMIT")
        self.assertEqual(rows[0]["write_operation_assertion"],
                         "REPORTED_TRUNCATED_STORE_DOCUMENTED_BY_CATALOG_MESSAGE_ID")
        self.assertEqual(rows[0]["storage_event_basis"], "DOCUMENTED_CATALOG_MESSAGE_ID_AT_THE_PINNED_RELEASE")
        self.assertEqual(rows[0]["reported_errno"], "", "a documented limited store is not an errno failure")
        self.assertEqual(rows[0]["reported_message_id"], TRUNCATED_CORE_ID)
        self.assertEqual(rows[1]["journal_trusted_kernel_device"], "b8:0")
        self.assertEqual(rows[1]["message_text_status"], "WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL")
        self.assertEqual(rows[1]["message_text_sha256"], "")
        self.assertEqual(report["withheld_message_fields"], 1)
        self.assertEqual(rows[2]["message_text_status"], "NON_UTF8_BYTE_ARRAY_NOT_PARSED")
        self.assertEqual(rows[2]["message_text_bytes"], "5")
        self.assertEqual([r["record_time_us"] for r in rows],
                         ["1789500000000000", "1789500001000000", "1789500002000000"])
        self.assertEqual({r["record_time_basis"] for r in rows}, {"UNKNOWN"})
        self.assertEqual({r["source_time_status"] for r in rows},
                         {"REPORTED_EARLIEST_TRUSTED_SOURCE_TIME"})
        self.assertEqual(rows[0]["journal_reception_realtime_us"], "1789000011000000")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets the built reader")
    def test_both_authored_inputs_reach_common_observations(self):
        _, _, reception = self.rows_of(
            "\n".join([errno_entry(1, 28, _SYSTEMD_UNIT="authored-recorder.service"),
                       errno_entry(2, 30)]) + "\n")
        common = self.observations(reception, RECEPTION_PROFILE)
        self.assertEqual(common["main_rows"], 2)
        first = common["observations"][0]
        self.assertEqual(first["clock_basis"], "Unknown")
        self.assertEqual(first["fields"]["record_time_basis"], "HOST_RECEIVED")
        self.assertEqual(first["t_ms"], 1_789_000_001_000)
        self.assertEqual(first["fields"]["reported_errno"], 28)
        self.assertEqual(first["fields"]["storage_event_class"], "REPORTED_NO_SPACE_LEFT_ON_DEVICE")
        self.assertEqual(first["fields"]["write_operation_assertion"],
                         "NOT_ESTABLISHED_NO_JOURNAL_FIELD_NAMES_THE_OPERATION_OR_TARGET")
        self.assertIn("never_a_physical_disk_diagnosis", common["profile_units"]["storage_event_class"])
        self.assertIn("journald_reception", common["profile_units"]["record_time_us"])
        self.assertIn("not_a_byte_count", common["profile_units"]["reported_errno"])

        _, _, sender = self.rows_of(
            entry(11, MESSAGE_ID=TRUNCATED_CORE_ID, _SOURCE_REALTIME_TIMESTAMP="1789500000000000",
                  MESSAGE="authored truncated store notice") + "\n", clock="source_realtime")
        variation = self.observations(sender, SOURCE_TIME_PROFILE)
        self.assertEqual(variation["main_rows"], 1)
        entry_out = variation["observations"][0]
        self.assertEqual(entry_out["clock_basis"], "Unknown")
        self.assertEqual(entry_out["fields"]["storage_event_class"],
                         "REPORTED_TRUNCATED_STORE_BY_CONFIGURED_SIZE_LIMIT")
        self.assertIn("earliest_trusted_source_time", variation["profile_units"]["record_time_us"])


    def test_the_declared_settings_are_required_and_checked(self):
        text = errno_entry(1, 28) + "\n"
        self.rows_of(text)  # the declared triple converts
        for kwargs in ({"source_format": "journalctl-json-v256"}, {"source_format": ""},
                       {"errno_abi": "linux-6.1-asm-generic"}, {"errno_abi": ""},
                       {"clock": "wall"}, {"clock": "__MONOTONIC_TIMESTAMP"}, {"clock": ""}):
            with self.assertRaises(ValueError):
                self.rows_of(text, **kwargs)

    def test_ambiguous_withheld_or_malformed_fields_are_refused(self):
        cases = {
            "errno repeated": entry(1, ERRNO=["28", "30"]),
            "errno withheld": entry(1, ERRNO=None),
            "errno not decimal": entry(1, ERRNO="ENOSPC"),
            "errno zero": entry(1, ERRNO="0"),
            "errno beyond the abi": entry(1, ERRNO="99999"),
            "errno signed": entry(1, ERRNO="-28"),
            "message id not hex": entry(1, ERRNO="28", MESSAGE_ID="NOT-A-CATALOG-ID"),
            "message id upper case": entry(1, ERRNO="28", MESSAGE_ID=TRUNCATED_CORE_ID.upper()),
            "message not a documented encoding": entry(1, ERRNO="28", MESSAGE={"text": "no"}),
            "message bytes out of range": entry(1, ERRNO="28", MESSAGE=[104, 999]),
            "reception time missing": json.dumps({"__CURSOR": "s=1a2b;i=1", "ERRNO": "28"}),
            "reception time not decimal": entry(1, ERRNO="28", __REALTIME_TIMESTAMP="2026-09-20"),
            "not an object": "[{\"ERRNO\": \"28\"}]",
            "truncated json": entry(1, ERRNO="28")[:-3],
            "duplicate json key": '{"ERRNO": "28", "ERRNO": "30"}',
            "no reported error at all": entry(1, MESSAGE="authored entry with no code"),
            "empty export": "",
        }
        for name, line in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.rows_of(line if name == "empty export" else line + "\n")
        with self.assertRaises(ValueError):
            self.rows_of(errno_entry(1, 28) + "\n", clock="source_realtime")

    def test_a_repeated_cursor_is_refused_as_overlapping_exports(self):
        line = errno_entry(1, 28)
        self.rows_of(line + "\n")
        with self.assertRaises(ValueError):
            self.rows_of(line + "\n" + line + "\n")
        rows, report, _ = self.rows_of(line + "\n" + errno_entry(2, 28) + "\n")
        self.assertEqual((len(rows), report["selected_entries"]), (2, 2))

    def test_an_absent_sender_timestamp_is_named_not_silently_blank(self):
        rows, report, _ = self.rows_of("\n".join([errno_entry(1, 28),
                                                  errno_entry(2, 28, _SOURCE_REALTIME_TIMESTAMP=
                                                              "1789500002000000")]) + "\n")
        self.assertEqual(report["absent_source_time_entries"], 1)
        self.assertEqual(rows[0]["source_time_status"],
                         "ABSENT_FROM_THIS_ENTRY_NO_TRUSTED_SOURCE_TIME_DIFFERENT_FROM_RECEPTION")
        self.assertEqual(rows[0]["journal_source_realtime_us"], "")
        self.assertEqual(rows[1]["source_time_status"], "REPORTED_EARLIEST_TRUSTED_SOURCE_TIME")
        self.assertEqual(rows[1]["journal_source_realtime_us"], "1789500002000000")


    def test_the_cli_binds_the_bytes_and_hands_over_the_accounting(self):
        text = "\n".join([errno_entry(1, 28), errno_entry(2, 5),
                          entry(3, MESSAGE="authored entry with no code")]) + "\n"
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "journal-storage.json"
            source.write_bytes(text.encode())
            target = Path(work) / "observations.csv"
            report_path = Path(work) / "report.json"
            command = [os.sys.executable, str(SCRIPT), str(source), str(target),
                       "--source-format", SOURCE_FORMAT, "--errno-abi", ERRNO_ABI,
                       "--clock", "journal_reception_realtime",
                       "--record-sidecar", str(Path(work) / "records.jsonl")]
            done = subprocess.run(command + ["--report", str(report_path)], check=True,
                                  capture_output=True)
            for text_out in (done.stderr.decode(), report_path.read_text()):
                report = json.loads(text_out.strip())
                self.assertEqual((report["entries"], report["selected_entries"],
                                  report["entries_without_selector"]), (3, 2, 1))
                self.assertEqual(report["source_pin"], "systemd-v257-70bae764")
                self.assertEqual(report["clock"], "HOST_RECEIVED")
                self.assertNotIn("authored", text_out)
                self.assertNotIn("__CURSOR", text_out)
                self.assertNotIn(BOOT, text_out)
            self.assertEqual(done.stdout, b"")
            self.assertEqual(json.loads(done.stderr.decode())["source_sha256"],
                             "sha256:" + __import__("hashlib").sha256(text.encode()).hexdigest())
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            refused = subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(Path(work) / "other.csv"),
                 "--source-format", SOURCE_FORMAT, "--errno-abi", ERRNO_ABI,
                 "--clock", "source_realtime",
                 "--record-sidecar", str(Path(work) / "other.jsonl")], capture_output=True)
            self.assertEqual(refused.returncode, 2)
            self.assertIn(b"no successful conversion claim", refused.stderr)


    def test_a_provided_identity_value_is_never_silently_erased(self):
        """A blank identity cell must be distinguishable from absence, withholding and ambiguity."""
        text = "\n".join([
            errno_entry(21, 28, _COMM=["authored-first", "authored-second"]),   # multi-valued
            errno_entry(22, 28, _PID=None),                                    # withheld by size limit
            errno_entry(23, 28, _SYSTEMD_UNIT=[104, 105, 7]),                  # non-UTF8 bytes
            errno_entry(24, 28, _COMM="authored-plain"),                       # a plain single value
            errno_entry(25, 28, _SYSTEMD_UNIT=None, PRIORITY=None),            # two notes in one row
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertEqual(rows[0]["identity_encoding_notes"],
                         "journal_trusted_comm=MULTIPLE_VALUES_IN_SIDECAR")
        self.assertEqual(rows[0]["journal_trusted_comm"], "", "no single value may be invented")
        self.assertEqual(rows[1]["identity_encoding_notes"],
                         "journal_trusted_pid=WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL")
        self.assertEqual(rows[2]["identity_encoding_notes"],
                         "journal_trusted_unit=NON_UTF8_BYTE_ARRAY_VALUE_IN_SIDECAR")
        self.assertEqual((rows[3]["journal_trusted_comm"], rows[3]["identity_encoding_notes"]),
                         ("authored-plain", ""))
        self.assertEqual((rows[3]["journal_trusted_unit"], rows[3]["identity_encoding_notes"]), ("", ""))
        self.assertEqual(rows[4]["identity_encoding_notes"],
                         "journal_trusted_unit=WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL;"
                         "client_reported_priority=WITHHELD_BY_EXPORTER_SIZE_LIMIT_NULL")
        self.assertEqual(report["ambiguous_identity_fields"], 5)
        preserved = self.sidecar[1]["preserved_fields"]
        self.assertEqual(preserved["_COMM"], ["authored-first", "authored-second"])
        self.assertIsNone(self.sidecar[2]["preserved_fields"]["_PID"])
        self.assertEqual(self.sidecar[3]["preserved_fields"]["_SYSTEMD_UNIT"], [104, 105, 7])
        for bad in ({"_COMM": 17}, {"_PID": {"nested": "object"}}, {"PRIORITY": True},
                    {"_COMM": [{"nested": 1}]}):
            with self.subTest(str(bad)), self.assertRaises(ValueError):
                self.rows_of(entry(26, ERRNO="28", **bad) + "\n")

    def test_unknown_fields_are_counted_named_and_preserved_verbatim(self):
        text = "\n".join([
            errno_entry(31, 28, CODE_FILE="authored.c", CODE_LINE="42",
                        AUTHORED_ARRAY=["one", "two"], AUTHORED_NULL=None,
                        AUTHORED_NESTED={"authored": {"depth": [1, 2]}}, AUTHORED_BYTES=[7, 200]),
            errno_entry(32, 30),
        ]) + "\n"
        rows, report, _ = self.rows_of(text)
        self.assertEqual(rows[0]["unknown_field_count"], "8")
        self.assertEqual(rows[0]["unknown_field_names"],
                         "AUTHORED_ARRAY;AUTHORED_BYTES;AUTHORED_NESTED;AUTHORED_NULL;CODE_FILE;"
                         "CODE_LINE;_TRANSPORT;__MONOTONIC_TIMESTAMP")
        self.assertEqual(rows[1]["unknown_field_count"], "2")
        self.assertEqual(report["unknown_fields_total"], 10)
        preserved = self.sidecar[1]["preserved_fields"]
        self.assertEqual(preserved["AUTHORED_NESTED"], {"authored": {"depth": [1, 2]}})
        self.assertEqual(preserved["AUTHORED_ARRAY"], ["one", "two"])
        self.assertIsNone(preserved["AUTHORED_NULL"])
        self.assertEqual(preserved["AUTHORED_BYTES"], [7, 200])
        self.assertEqual(preserved["CODE_FILE"], "authored.c")
        self.assertNotIn("authored.c", ",".join(rows[0].values()))

    def test_the_sidecar_pairs_each_row_and_withholds_only_the_message(self):
        message = "authored synthetic entry 41"
        text = errno_entry(41, 28, MESSAGE=message) + "\n"
        rows, report, _ = self.rows_of(text)
        record = text.splitlines()[0].encode()
        expected = "sha256:" + hashlib.sha256(record).hexdigest()
        self.assertEqual(rows[0]["record_sha256"], expected, "the row binds its own record bytes")
        self.assertEqual(rows[0]["record_bytes"], str(len(record)))
        header, entry_out = self.sidecar
        self.assertEqual(header["sidecar_format"], "journal-storage-errors-record-sidecar-v1")
        self.assertEqual(header["source_sha256"], report["source_sha256"])
        self.assertEqual(entry_out["record_sha256"], expected)
        self.assertEqual(entry_out["row"], 1)
        self.assertNotIn("MESSAGE", entry_out["preserved_fields"])
        self.assertEqual(entry_out["message_withheld_by_design"],
                         {"status": "PRESENT_UTF8_TEXT_NOT_PARSED",
                          "sha256": "sha256:" + hashlib.sha256(message.encode()).hexdigest(),
                          "bytes": len(message)})
        for text_out in (json.dumps(self.sidecar), ",".join(rows[0].values()),
                         json.dumps(report)):
            self.assertNotIn(message, text_out)
        self.assertEqual(report["sidecar_records"], 1)

    def test_the_cli_writes_the_paired_sidecar_to_a_new_file(self):
        text = "\n".join([errno_entry(51, 28, AUTHORED_EXTRA="authored-value"),
                          errno_entry(52, 5)]) + "\n"
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "journal-storage.json"
            source.write_bytes(text.encode())
            target = Path(work) / "observations.csv"
            sidecar_path = Path(work) / "records.jsonl"
            command = [os.sys.executable, str(SCRIPT), str(source), str(target),
                       "--source-format", SOURCE_FORMAT, "--errno-abi", ERRNO_ABI,
                       "--clock", "journal_reception_realtime", "--record-sidecar", str(sidecar_path)]
            subprocess.run(command, check=True, capture_output=True)
            lines = [json.loads(line) for line in sidecar_path.read_text().splitlines()]
            self.assertEqual(len(lines), 3, "one header plus one record per selected row")
            rows = list(csv.DictReader(io.StringIO(target.read_text())))
            self.assertEqual([r["record_sha256"] for r in rows],
                             [line["record_sha256"] for line in lines[1:]])
            self.assertEqual(lines[1]["preserved_fields"]["AUTHORED_EXTRA"], "authored-value")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_the_pair_is_mandatory_and_never_published_half_written(self):
        """The rows promise preserved values, so a run must not succeed without the sidecar beside them."""
        text = "\n".join([errno_entry(61, 28, _COMM=["authored-first", "authored-second"]),
                          errno_entry(62, 30, AUTHORED_EXTRA={"kept": [1, None]})]) + "\n"
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = work / "journal-storage.json"
            source.write_bytes(text.encode())
            settings = ["--source-format", SOURCE_FORMAT, "--errno-abi", ERRNO_ABI,
                        "--clock", "journal_reception_realtime"]

            omitted = subprocess.run([os.sys.executable, str(SCRIPT), str(source),
                                      str(work / "no-pair.csv")] + settings, capture_output=True)
            self.assertEqual(omitted.returncode, 2)
            self.assertIn(b"--record-sidecar", omitted.stderr)
            self.assertNotIn(b"Traceback", omitted.stderr)
            self.assertFalse((work / "no-pair.csv").exists(), "no CSV without its sidecar")

            (work / "taken.jsonl").write_text("authored pre-existing content\n")
            for name, extra in (("existing sidecar", ["--record-sidecar", str(work / "taken.jsonl")]),
                                ("sidecar == csv", ["--record-sidecar", str(work / "collide.csv")]),
                                ("report == sidecar", ["--record-sidecar", str(work / "same.jsonl"),
                                                       "--report", str(work / "same.jsonl")])):
                csv_target = work / ("collide.csv" if name == "sidecar == csv" else f"{name}.csv")
                refused = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(csv_target)]
                                         + settings + extra, capture_output=True)
                with self.subTest(name):
                    self.assertEqual(refused.returncode, 2)
                    self.assertIn(b"no successful conversion claim", refused.stderr)
                    self.assertFalse(csv_target.exists(), "a refused run publishes no CSV")
            self.assertEqual((work / "taken.jsonl").read_text(), "authored pre-existing content\n")
            self.assertFalse((work / "same.jsonl").exists())

            target, sidecar_path, report_path = (work / "obs.csv", work / "rec.jsonl", work / "rep.json")
            done = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target)] + settings
                                  + ["--record-sidecar", str(sidecar_path), "--report", str(report_path)],
                                  check=True, capture_output=True)
            rows = list(csv.DictReader(io.StringIO(target.read_text())))
            records = [json.loads(line) for line in sidecar_path.read_text().splitlines()]
            report = json.loads(done.stderr.decode())
            self.assertEqual(report["sidecar_records"], len(rows))
            self.assertEqual(len(records), len(rows) + 1, "one header plus one record per row")
            self.assertEqual([r["record_sha256"] for r in rows],
                             [record["record_sha256"] for record in records[1:]])
            self.assertEqual(records[1]["preserved_fields"]["_COMM"],
                             ["authored-first", "authored-second"])
            self.assertEqual(records[2]["preserved_fields"]["AUTHORED_EXTRA"], {"kept": [1, None]})
            self.assertIn("MULTIPLE_VALUES_IN_SIDECAR", rows[0]["identity_encoding_notes"])

    def test_the_two_digests_have_the_scopes_the_output_states(self):
        """The record digest excludes the line terminator; the file digest keeps every byte, CRLF included."""
        first, second = errno_entry(71, 28), errno_entry(72, 30)
        crlf = (first + "\r\n" + second + "\r\n").encode()
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = work / "journal-storage.json"
            source.write_bytes(crlf)
            done = subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(work / "obs.csv"),
                 "--source-format", SOURCE_FORMAT, "--errno-abi", ERRNO_ABI,
                 "--clock", "journal_reception_realtime",
                 "--record-sidecar", str(work / "rec.jsonl")], check=True, capture_output=True)
            rows = list(csv.DictReader(io.StringIO((work / "obs.csv").read_text())))
            report = json.loads(done.stderr.decode())
            self.assertEqual(report["source_sha256"], "sha256:" + hashlib.sha256(crlf).hexdigest())
            self.assertNotEqual(report["source_sha256"],
                                "sha256:" + hashlib.sha256(crlf.replace(b"\r\n", b"\n")).hexdigest())
            self.assertEqual([r["record_sha256"] for r in rows],
                             ["sha256:" + hashlib.sha256(line.encode()).hexdigest()
                              for line in (first, second)])
            self.assertEqual([r["record_bytes"] for r in rows],
                             [str(len(first.encode())), str(len(second.encode()))])
            header = json.loads((work / "rec.jsonl").read_text().splitlines()[0])
            self.assertIn("EXCLUDING its line terminator", header["record_sha256_scope"])
            self.assertIn("not the record's raw bytes", header["preserved_fields_scope"])

    def test_a_practical_export_size_stays_inside_the_bounds(self):
        text = "".join(errno_entry(index, 28) + "\n" for index in range(5000))
        rows, report, _ = self.rows_of(text)
        self.assertEqual((len(rows), report["selected_entries"]), (5000, 5000))
        self.assertEqual(rows[-1]["record_time_us"], str(1_789_000_000_000_000 + 4999 * 1_000_000))


if __name__ == "__main__":
    unittest.main()
