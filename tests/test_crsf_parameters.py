"""Authored frames only, built byte by byte from the pinned payload definitions.

No real capture, no device, no TBS Agent file and no transmission is involved anywhere in this module.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_crsf_parameters.py"
PROFILE = ROOT / "profiles/declared/crsf-reported-parameters/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER",
                        str(ROOT / "target/debug/examples/read_telemetry_csv"))

spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TX = 0xEE        # R/C Transmitter Module, from the pinned Device Addresses list
HANDSET = 0xEA   # Remote Control


def frame(kind, payload, *, destination=HANDSET, origin=TX, length=None, crc=None):
    """An extended-header CRSF frame: sync, length, type, destination, origin, payload, CRC8."""
    body = bytes([kind, destination, origin]) + payload
    computed = module.crc8(body)
    return bytes([0xC8, len(body) + 1 if length is None else length]) + body + bytes(
        [computed if crc is None else crc])


def capture(*frames):
    rows = ["time_us,frame_hex"]
    for index, item in enumerate(frames):
        rows.append("%d,%s" % (1000 + index * 10, item.hex()))
    return "\n".join(rows) + "\n"


def device_information(name=b"AUTHORED-TX", serial=0x11223344, hardware=0x55667788,
                       firmware=0x99AABBCC, total=7, version=2):
    payload = name + b"\x00" + serial.to_bytes(4, "big") + hardware.to_bytes(4, "big") \
        + firmware.to_bytes(4, "big") + bytes([total])
    if version is not None:
        payload += bytes([version])
    return frame(module.DEVICE_INFORMATION, payload)


def float_entry(number=3, parent=0, value=250, low=0, high=1000, default=100, decimal=1, step=10,
                name=b"Max Power", unit=b"mW", hidden=False):
    payload = bytes([parent, module.FLOAT_TYPE | (module.HIDDEN_BIT if hidden else 0)]) + name + b"\x00"
    payload += b"".join(number.to_bytes(4, "big", signed=True)
                        for number in (value, low, high, default))
    payload += bytes([decimal]) + step.to_bytes(4, "big", signed=True)
    if unit is not None:
        payload += unit + b"\x00"
    return payload


def selection_entry(options=b"25mW;100mW;500mW", value=1, low=0, high=2, default=0, parent=1,
                    name=b"Power", unit=b""):
    payload = bytes([parent, module.SELECTION_TYPE]) + name + b"\x00" + options + b"\x00"
    payload += bytes([value, low, high, default]) + unit + b"\x00"
    return payload


def entry_frame(payload, *, number=3, remaining=0, origin=TX, destination=HANDSET):
    return frame(module.PARAMETER_ENTRY, bytes([number, remaining]) + payload, origin=origin,
                 destination=destination)


def chunked(payload, *, number=3, size=8, origin=TX, destination=HANDSET):
    """The same entry split as the pinned Chunks section describes: remaining counts down to 0."""
    pieces = [payload[at:at + size] for at in range(0, len(payload), size)]
    return [frame(module.PARAMETER_ENTRY, bytes([number, len(pieces) - 1 - index]) + piece,
                  origin=origin, destination=destination) for index, piece in enumerate(pieces)]


def read_request(number=3, chunk=0, *, device=TX, host=HANDSET):
    """The host's own 0x2C read, as the capture holds it. It is read here and never sent."""
    return frame(module.PARAMETER_READ, bytes([number, chunk]), destination=device, origin=host)


class ReportedParameters(unittest.TestCase):
    def rows(self, text, declared=False):
        output, report = module.convert(text, declared)
        rows = list(csv.DictReader(io.StringIO(output)))
        for row in rows:
            self.assertEqual(list(row), module.FIELDS)
            for value in row.values():
                self.assertNotIn(",", value)
                self.assertNotIn('"', value)
        return rows, report, output

    def test_a_reported_setting_reaches_common_with_its_device_identity(self):
        rows, report, _ = self.rows(capture(device_information(), read_request(number=3),
                                            entry_frame(float_entry(), number=3)))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["crsf_value_disposition"], "DEVICE_REPORTED_STORED_SETTING")
        self.assertEqual(row["crsf_configured_value_reported"], "250")
        self.assertEqual((row["crsf_value_min_reported"], row["crsf_value_max_reported"],
                          row["crsf_value_default_reported"]), ("0", "1000", "100"))
        self.assertEqual(row["crsf_value_decimal_point_reported"], "1")
        self.assertEqual(row["crsf_parameter_name_hex"], "hex:" + b"Max Power".hex())
        self.assertEqual(row["crsf_value_unit_hex"], "hex:" + b"mW".hex())
        self.assertEqual(row["crsf_parameter_type_name"], "FLOAT")
        self.assertEqual(row["crsf_parameter_hidden_reported"], "0")
        self.assertEqual(row["crsf_origin_role"], "RC_TRANSMITTER_MODULE")
        self.assertEqual(row["crsf_destination_address"], str(HANDSET))
        self.assertEqual(row["crsf_chain_disposition"],
                         "SINGLE_OBSERVED_FRAME_REPORTING_NO_CHUNKS_REMAINING_WHICH_ALONE_DOES_NOT_"
                         "SHOW_IT_IS_THE_WHOLE_ENTRY")
        self.assertEqual(row["crsf_entry_start_basis"],
                         "CORRELATED_0x2C_CHUNK_ZERO_READ_REQUEST_EVIDENCE_NOT_PROOF_THAT_NO_FRAME_"
                         "WAS_LOST_BETWEEN_IT_AND_THE_REPLY")
        self.assertEqual(row["crsf_chunk_count"], "1")
        self.assertEqual(row["crsf_device_name_hex"], "hex:" + b"AUTHORED-TX".hex())
        self.assertEqual(row["crsf_device_serial_reported"], str(0x11223344))
        self.assertEqual(row["crsf_device_firmware_id_reported"], str(0x99AABBCC))
        self.assertEqual(row["crsf_device_parameters_total_reported"], "7")
        self.assertEqual(row["crsf_device_identity_basis"],
                         "DEVICE_REPORTED_ITS_OWN_IDENTITY_IN_AN_0x29_FRAME_FROM_THIS_ORIGIN_NOT_"
                         "AUTHENTICATED")
        self.assertIn("never an observed hardware state", row["crsf_configuration_basis"])
        self.assertIn("never a requested write", row["crsf_configuration_basis"])
        self.assertEqual(report["parameter_entries"], 1)
        self.assertEqual(report["device_information_records"], 1)

    def test_a_selection_label_comes_only_from_that_frames_own_option_list(self):
        rows, _, _ = self.rows(capture(read_request(number=4), entry_frame(selection_entry(), number=4)))
        row = rows[0]
        self.assertEqual(row["crsf_parameter_type_name"], "TEXT_SELECTION")
        self.assertEqual(row["crsf_configured_value_reported"], "1")
        self.assertEqual(row["crsf_configured_value_label_hex"], "hex:" + b"100mW".hex())
        self.assertEqual(row["crsf_value_unit_hex"], "hex:")
        beyond, _, _ = self.rows(capture(read_request(number=4),
                                    entry_frame(selection_entry(value=9, high=9), number=4)))
        self.assertEqual(beyond[0]["crsf_configured_value_label_hex"],
                         "OPTION_INDEX_BEYOND_THE_REPORTED_LIST")
        self.assertEqual(row["crsf_device_name_hex"], "")
        self.assertEqual(row["crsf_device_identity_basis"],
                         "DEVICE_INFORMATION_NOT_REPORTED_BY_THIS_ORIGIN_IN_THIS_CAPTURE")

    def test_a_requested_write_is_never_a_reported_configuration(self):
        write = frame(module.PARAMETER_WRITE, bytes([3]) + (777).to_bytes(4, "big"))
        read = read_request(number=3)
        link_statistics = bytes([0xC8, 11, 0x14]) + bytes(9)
        link_statistics += bytes([module.crc8(link_statistics[2:])])
        rows, report, _ = self.rows(capture(write, read, link_statistics,
                                            entry_frame(float_entry(), number=3)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crsf_configured_value_reported"], "250")
        self.assertEqual([item["disposition"] for item in report["requested_frames"]],
                         ["REQUESTED_WRITE_NOT_A_REPORTED_CONFIGURATION",
                          "REQUESTED_READ_NOT_A_REPORTED_CONFIGURATION"])
        self.assertEqual([item["frame_type"] for item in report["unsupported_records"]], [0x14])
        self.assertEqual(report["source_records"], 4)

    def test_a_completed_run_carries_a_value_only_when_its_start_is_shown(self):
        payload = float_entry(name=b"A Long Parameter Name For Chunking", unit=b"dBm")
        pieces = chunked(payload, number=5)
        self.assertGreater(len(pieces), 1)
        rows, report, _ = self.rows(capture(read_request(number=5), *pieces))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crsf_configured_value_reported"], "250")
        self.assertEqual(rows[0]["crsf_value_disposition"], "DEVICE_REPORTED_STORED_SETTING")
        self.assertEqual(rows[0]["crsf_entry_start_basis"],
                         "CORRELATED_0x2C_CHUNK_ZERO_READ_REQUEST_EVIDENCE_NOT_PROOF_THAT_NO_FRAME_"
                         "WAS_LOST_BETWEEN_IT_AND_THE_REPLY")
        self.assertEqual(rows[0]["crsf_chain_disposition"],
                         "MULTI_FRAME_RUN_JOINED_WHILE_SELF_CONSISTENT_DOWN_TO_NO_CHUNKS_REMAINING")
        self.assertEqual(rows[0]["crsf_parameter_number"], "5")
        self.assertEqual((report["parameter_entries"], report["unqualified_entries"]), (1, 0))
        self.assertEqual(report["observed_chunk_zero_reads"], 1)
        bare, unqualified, _ = self.rows(capture(*pieces))
        self.assertEqual(bare[0]["crsf_value_disposition"],
                         "UNQUALIFIED_RUN_START_NOT_EVIDENCED_SO_NO_VALUE_IS_READ")
        self.assertEqual(bare[0]["crsf_entry_start_basis"],
                         "NO_CORRELATED_CHUNK_ZERO_READ_REQUEST_AND_NO_CALLER_DECLARATION")
        self.assertEqual(bare[0]["crsf_configured_value_reported"], "")
        self.assertEqual(bare[0]["crsf_parameter_type_name"], "")
        self.assertEqual(bare[0]["crsf_payload_consumed_bytes"], "0")
        self.assertEqual(bare[0]["crsf_entry_sha256"], rows[0]["crsf_entry_sha256"])
        self.assertEqual((unqualified["parameter_entries"], unqualified["unqualified_entries"]), (0, 1))
        declared, _, _ = self.rows(capture(*pieces), declared=True)
        self.assertEqual(declared[0]["crsf_configured_value_reported"], "250")
        self.assertEqual(declared[0]["crsf_entry_start_basis"],
                         "CALLER_DECLARED_PRECONDITION_THAT_EVERY_RUN_IN_THIS_FILE_STARTS_AT_CHUNK_"
                         "ZERO_NOT_OBSERVED_IN_BAND")
        with self.assertRaises(ValueError):
            module.convert(capture(*pieces), "yes")

    def test_a_lost_first_chunk_never_parses_its_own_tail_as_a_setting(self):
        payload = float_entry(name=b"A Long Parameter Name For Chunking", unit=b"dBm")
        pieces = chunked(payload, number=5)
        rows, report, _ = self.rows(capture(*pieces[1:]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crsf_value_disposition"],
                         "UNQUALIFIED_RUN_START_NOT_EVIDENCED_SO_NO_VALUE_IS_READ")
        self.assertEqual(rows[0]["crsf_configured_value_reported"], "")
        self.assertEqual(report["parameter_entries"], 0)
        for read in (read_request(number=4), read_request(number=5, chunk=1),
                     read_request(number=5, host=0xC8), read_request(number=5, device=0xEC)):
            other, _, _ = self.rows(capture(read, *pieces))
            self.assertEqual(other[0]["crsf_entry_start_basis"],
                             "NO_CORRELATED_CHUNK_ZERO_READ_REQUEST_AND_NO_CALLER_DECLARATION")
        twice, _, _ = self.rows(capture(read_request(number=5), *pieces, *pieces))
        self.assertEqual(twice[0]["crsf_entry_start_basis"][:10], "CORRELATED")
        self.assertEqual(twice[1]["crsf_value_disposition"],
                         "UNQUALIFIED_RUN_START_NOT_EVIDENCED_SO_NO_VALUE_IS_READ")

    def test_no_chunks_remaining_on_one_frame_is_not_a_whole_entry(self):
        coincidence = float_entry(value=999, name=b"Looks Whole", unit=b"mW")
        tail = frame(module.PARAMETER_ENTRY, bytes([5, 0]) + coincidence)
        rows, report, _ = self.rows(capture(tail))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crsf_chunk_count"], "1")
        self.assertEqual(rows[0]["crsf_chain_disposition"],
                         "SINGLE_OBSERVED_FRAME_REPORTING_NO_CHUNKS_REMAINING_WHICH_ALONE_DOES_NOT_"
                         "SHOW_IT_IS_THE_WHOLE_ENTRY")
        self.assertEqual(rows[0]["crsf_entry_start_basis"],
                         "NO_CORRELATED_CHUNK_ZERO_READ_REQUEST_AND_NO_CALLER_DECLARATION")
        self.assertEqual(rows[0]["crsf_value_disposition"],
                         "UNQUALIFIED_RUN_START_NOT_EVIDENCED_SO_NO_VALUE_IS_READ")
        self.assertEqual(rows[0]["crsf_configured_value_reported"], "")
        self.assertEqual(rows[0]["crsf_parameter_type_name"], "")
        self.assertEqual(rows[0]["crsf_payload_consumed_bytes"], "0")
        self.assertEqual((report["parameter_entries"], report["unqualified_entries"]), (0, 1))
        for text, declared in ((capture(read_request(number=5), tail), False), (capture(tail), True)):
            good, _, _ = self.rows(text, declared)
            self.assertEqual(good[0]["crsf_configured_value_reported"], "999")
            self.assertEqual(good[0]["crsf_value_disposition"], "DEVICE_REPORTED_STORED_SETTING")
            self.assertEqual(good[0]["crsf_parameter_type_name"], "FLOAT")

    def test_a_later_chunk_read_withdraws_the_chunk_zero_authorization(self):
        payload = float_entry()
        tail = entry_frame(payload, number=3)
        rows, report, _ = self.rows(capture(read_request(number=3, chunk=0),
                                            read_request(number=3, chunk=2), tail))
        self.assertEqual(rows[0]["crsf_entry_start_basis"],
                         "NO_CORRELATED_CHUNK_ZERO_READ_REQUEST_AND_NO_CALLER_DECLARATION")
        self.assertEqual(rows[0]["crsf_value_disposition"],
                         "UNQUALIFIED_RUN_START_NOT_EVIDENCED_SO_NO_VALUE_IS_READ")
        self.assertEqual(report["invalidated_chunk_zero_reads"], 1)
        self.assertEqual(report["observed_chunk_zero_reads"], 1)
        other, kept, _ = self.rows(capture(read_request(number=3, chunk=0),
                                           read_request(number=3, chunk=2, device=0xEC), tail))
        self.assertEqual(other[0]["crsf_configured_value_reported"], "250")
        self.assertEqual(kept["invalidated_chunk_zero_reads"], 0)

    def test_a_run_is_joined_only_while_it_stays_self_consistent(self):
        payload = float_entry(name=b"A Long Parameter Name For Chunking", unit=b"dBm")
        pieces = chunked(payload, number=5)
        _, stopped, output = self.rows(capture(*pieces[:-1]))
        self.assertEqual(stopped["incomplete_chunk_runs"], 1)
        self.assertEqual((stopped["parameter_entries"], stopped["unqualified_entries"]), (0, 0))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(output)))), 0)
        crossed = chunked(payload, number=5, destination=0xC8)
        with self.assertRaises(ValueError):
            module.convert(capture(pieces[0], crossed[1]))
        whole, _, _ = self.rows(capture(read_request(number=5, host=0xC8),
                                        *chunked(payload, number=5, destination=0xC8)))
        self.assertEqual(whole[0]["crsf_destination_address"], "200")
        self.assertEqual(whole[0]["crsf_configured_value_reported"], "250")

    def test_structural_and_semantic_failures_are_refused_not_guessed(self):
        good = float_entry()
        pieces = chunked(good, number=5)
        for text in (
                capture(pieces[0], pieces[2]),
                capture(pieces[0], pieces[0]),
                capture(pieces[0], *chunked(good, number=6)),
                capture(frame(module.PARAMETER_ENTRY, bytes([3, 0]) + good, crc=0x00)),
                capture(frame(module.PARAMETER_ENTRY, bytes([3, 0]) + good, length=9)),
                capture(read_request(), entry_frame(bytes([0, module.FLOAT_TYPE]) + b"N\x00"
                                                     + b"\x00\x01")),
                capture(read_request(), entry_frame(bytes([0, module.STRING_TYPE])
                                                    + b"unterminated")),
                "time_us,frame_hex\n5,notahexframe\n",
                "time_us,frame_hex\n20,%s\n10,%s\n" % (pieces[-1].hex(), pieces[-1].hex()),
                "frame_hex,time_us\n1,2\n"):
            with self.assertRaises(ValueError):
                module.convert(text)

    def test_unknown_and_non_setting_types_are_retained_never_mapped(self):
        cases = ((bytes([0, 0x7F]), "OUT_OF_RANGE",
                  "DEVICE_REPORTED_PARAMETER_NUMBER_OUT_OF_RANGE_SO_THERE_IS_NO_SETTING_HERE"),
                 (bytes([0, 0x0B]) + b"ROOT\x00" + bytes([1, 2, 0xFF]), "FOLDER",
                  "NOT_A_CONFIGURABLE_SETTING_BY_THE_PINNED_TYPE_DEFINITION"),
                 (bytes([0, 0x0D]) + b"Bind\x00", "COMMAND",
                  "NOT_A_CONFIGURABLE_SETTING_BY_THE_PINNED_TYPE_DEFINITION"),
                 (bytes([0, 0x0C]) + b"Status\x00" + b"OK\x00", "INFO",
                  "NOT_A_CONFIGURABLE_SETTING_BY_THE_PINNED_TYPE_DEFINITION", "hex:" + b"OK".hex()),
                 (bytes([0, 0x00]) + b"Old\x00" + b"\x01", "UINT8_DEPRECATED",
                  "TYPE_MARKED_DEPRECATED_BY_THE_PINNED_SPECIFICATION_SO_NO_VALUE_IS_READ"),
                 (bytes([0, 0x2A]) + b"Future\x00", "UNKNOWN_TYPE_RETAINED_AS_REPORTED",
                  "TYPE_NOT_DEFINED_BY_THE_PINNED_SPECIFICATION_SO_NO_VALUE_IS_READ"))
        for payload, name, disposition, *reported in cases:
            rows, _, _ = self.rows(capture(read_request(), entry_frame(payload)))
            self.assertEqual(rows[0]["crsf_parameter_type_name"], name)
            self.assertEqual(rows[0]["crsf_value_disposition"], disposition)
            self.assertEqual(rows[0]["crsf_configured_value_reported"],
                             reported[0] if reported else "")
            self.assertEqual(rows[0]["crsf_parameter_type_code"], str(payload[1] & ~module.HIDDEN_BIT))
        hidden, _, _ = self.rows(capture(read_request(), entry_frame(float_entry(hidden=True))))
        self.assertEqual(hidden[0]["crsf_parameter_hidden_reported"], "1")
        self.assertEqual(hidden[0]["crsf_parameter_type_name"], "FLOAT")
        self.assertEqual(hidden[0]["crsf_value_disposition"], "DEVICE_REPORTED_STORED_SETTING")
        shortened, _, _ = self.rows(capture(read_request(), entry_frame(float_entry(unit=None))))
        self.assertEqual(shortened[0]["crsf_value_unit_hex"], "ABSENT")
        without, _, _ = self.rows(capture(device_information(version=None), read_request(),
                                          entry_frame(float_entry())))
        self.assertEqual(without[0]["crsf_device_parameter_version_reported"], "")
        self.assertEqual(without[0]["crsf_device_parameters_total_reported"], "7")

    def test_every_payload_byte_is_accounted_for(self):
        payload = float_entry()
        rows, _, _ = self.rows(capture(read_request(), entry_frame(payload)))
        self.assertEqual(rows[0]["crsf_payload_consumed_bytes"], str(len(payload)))
        self.assertEqual(rows[0]["crsf_payload_remainder_bytes"], "0")
        self.assertEqual(rows[0]["crsf_payload_remainder_hex"], "NONE")
        self.assertEqual(rows[0]["crsf_value_step_reported"], "10")
        self.assertEqual(rows[0]["crsf_string_max_length_reported"], "")
        trailing, _, _ = self.rows(capture(read_request(), entry_frame(payload + b"\x07\x08\x09")))
        self.assertEqual(trailing[0]["crsf_payload_consumed_bytes"], str(len(payload)))
        self.assertEqual(trailing[0]["crsf_payload_remainder_bytes"], "3")
        self.assertEqual(trailing[0]["crsf_payload_remainder_hex"], "hex:070809")
        self.assertEqual(trailing[0]["crsf_configured_value_reported"], "250")
        unknown, _, _ = self.rows(capture(read_request(), entry_frame(bytes([0, 0x2A]) + b"Future\x00")))
        self.assertEqual(unknown[0]["crsf_payload_consumed_bytes"], "2")
        self.assertEqual(unknown[0]["crsf_payload_remainder_hex"], "hex:" + (b"Future\x00").hex())
        text = bytes([0, module.STRING_TYPE]) + b"Name\x00" + b"value\x00" + bytes([32])
        string_row, _, _ = self.rows(capture(read_request(), entry_frame(text)))
        self.assertEqual(string_row[0]["crsf_string_max_length_reported"], "32")
        self.assertEqual(string_row[0]["crsf_value_max_reported"], "")
        self.assertEqual(string_row[0]["crsf_payload_remainder_bytes"], "0")
        shortened, _, _ = self.rows(capture(read_request(), entry_frame(text[:-1])))
        self.assertEqual(shortened[0]["crsf_string_max_length_reported"], "ABSENT")
        with self.assertRaises(ValueError):
            module.convert(capture(read_request(), entry_frame(payload[:len(payload) - 8])))

    def test_identity_is_never_carried_across_origins(self):
        other = 0xEC  # RC Receiver: a different device on the same capture
        rows, _, _ = self.rows(capture(device_information(), read_request(device=other),
                                       entry_frame(float_entry(), origin=other)))
        self.assertEqual(rows[0]["crsf_origin_role"], "RC_RECEIVER")
        self.assertEqual(rows[0]["crsf_device_name_hex"], "")
        self.assertEqual(rows[0]["crsf_device_identity_basis"],
                         "DEVICE_INFORMATION_NOT_REPORTED_BY_THIS_ORIGIN_IN_THIS_CAPTURE")
        unlisted, _, _ = self.rows(capture(read_request(device=0x55), entry_frame(float_entry(), origin=0x55)))
        self.assertEqual(unlisted[0]["crsf_origin_address"], "85")
        self.assertEqual(unlisted[0]["crsf_origin_role"],
                         "ADDRESS_NOT_LISTED_BY_THE_PINNED_SPECIFICATION")

    def test_the_command_line_writes_declared_rows_for_the_common_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output, report = base / "capture.csv", base / "out.csv", base / "report.json"
            source.write_text(capture(device_information(), read_request(number=3),
                                      entry_frame(float_entry(), number=3), read_request(number=4),
                                      entry_frame(selection_entry(), number=4)), encoding="utf-8")
            subprocess.run([sys.executable, "-m", "scripts.convert_crsf_parameters", str(source),
                            str(output), str(report)], cwd=ROOT, check=True, capture_output=True)
            saved = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(saved["parameter_entries"], 2)
            self.assertEqual(saved["specification_pin"], module.PIN)
            self.assertIn("not coverage of any configuration application", saved["scope"])
            self.assertIn("no application file export is claimed to exist", saved["scope"])
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([READER, str(PROFILE), str(output)],
                                                   capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                fields = common["observations"][0]["fields"]
                self.assertEqual(str(fields["crsf_configured_value_reported"]), "250")
                self.assertEqual(fields["crsf_value_disposition"], "DEVICE_REPORTED_STORED_SETTING")
                self.assertEqual(common["observations"][1]["fields"]["crsf_parameter_type_name"],
                                 "TEXT_SELECTION")


if __name__ == "__main__":
    unittest.main()
