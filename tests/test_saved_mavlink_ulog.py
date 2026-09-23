"""Authored saved-fragment reassembly tests; no real flight log, device or streaming tool is used."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/assemble_saved_mavlink_ulog.py"
HEARTBEAT_FIELDS = (("uint32_t", "custom_mode", 0), ("uint8_t", "type", 0), ("uint8_t", "autopilot", 0),
                    ("uint8_t", "base_mode", 0), ("uint8_t", "system_status", 0),
                    ("uint8_t", "mavlink_version", 0))
SYSTEM_TIME_FIELDS = (("uint64_t", "time_unix_usec", 0), ("uint32_t", "time_boot_ms", 0))


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assembler = load(SCRIPT)



def ulog_message(kind, payload):
    return struct.pack("<HB", len(payload), ord(kind)) + payload


def ulog_info(key, value):
    return ulog_message("I", bytes([len(key)]) + key.encode("ascii") + value)


def ulog_add_logged(msg_id, name):
    return ulog_message("A", bytes([0]) + struct.pack("<H", msg_id) + name.encode("ascii"))


def ulog_data(msg_id, body):
    return ulog_message("D", struct.pack("<H", msg_id) + body)


def authored_ulog(variant):
    """Two distinguishable valid ULogs, built like crates/predemo-writers/src/ulog.rs (synthetic values)."""
    if variant == "gps":
        topics = [("sensor_gps", "uint64_t timestamp;uint64_t time_utc_usec;int32_t lat;int32_t lon;"
                                 "float hdop;uint8_t fix_type;uint8_t satellites_used;"),
                  ("input_rc", "uint64_t timestamp;float rssi_dbm;int8_t link_quality;bool rc_lost;")]
        start_us = 3_000_000
    elif variant == "status":
        topics = [("vehicle_status", "uint64_t timestamp;uint32_t nav_state;uint8_t arming_state;"
                                     "uint8_t failsafe;"),
                  ("battery_status", "uint64_t timestamp;float voltage_v;float current_a;uint8_t warning;"),
                  ("telemetry_status", "uint64_t timestamp;float rx_message_lost_rate;bool heartbeat_type_gcs;")]
        start_us = 11_000_000
    else:
        raise AssertionError("unknown authored variant")
    out = bytearray(assembler.ULOG_MAGIC + struct.pack("<Q", start_us))
    out += ulog_message("B", bytes(40))
    for name, fields in topics:
        out += ulog_message("F", f"{name}:{fields}".encode("ascii"))
    out += ulog_info("char[3] sys_name", b"PX4")
    out += ulog_info("int32_t time_ref_utc", struct.pack("<i", 0))
    for index, (name, _) in enumerate(topics):
        out += ulog_add_logged(index, name)
    for step in range(36):
        t = start_us + step * 500_000
        if variant == "gps":
            out += ulog_data(0, struct.pack("<QQiifBB", t, 1_700_000_000_000_000 + t, 356_000_000,
                                            1_396_000_000, 0.9 + step / 100, 3, 11 + (step % 3)))
            out += ulog_data(1, struct.pack("<QfbB", t, -85.0 - step, 90 - step, 0))
        else:
            out += ulog_data(0, struct.pack("<QIBB", t, 4, 2, 0))
            out += ulog_data(1, struct.pack("<QffB", t, 15.8 - step / 20, 8.5, 0))
            out += ulog_data(2, struct.pack("<Qf?", t, step / 100, True))
    out += ulog_message("L", bytes([6]) + struct.pack("<Q", start_us + 6_000_000)
                        + f"authored {variant} marker".encode("ascii"))
    return bytes(out)


def ulog_boundaries(data):
    """Offsets where a ULog message starts: the header, then every message header."""
    bounds = [0, assembler.ULOG_HEADER_LEN]
    offset = assembler.ULOG_HEADER_LEN
    while offset < len(data):
        size = struct.unpack_from("<H", data, offset)[0]
        offset += 3 + size
        bounds.append(offset)
    return bounds



def mavlink2_frame(msgid, payload, extra, seq=0, sysid=1, compid=1, truncate=True, incompat=0):
    wire = bytes(payload)
    if truncate:
        wire = wire.rstrip(b"\x00") or b"\x00"
    header = struct.pack("<BBBBBBB", 0xFD, len(wire), incompat, 0, seq, sysid, compid) \
        + msgid.to_bytes(3, "little")
    crc = assembler.crc_x25(bytes([extra]), assembler.crc_x25(header[1:] + wire))
    signature = bytes(13) if incompat & 0x01 else b""
    return header + wire + struct.pack("<H", crc) + signature


def mavlink1_frame(msgid, payload, extra, seq=0, sysid=1, compid=1):
    header = struct.pack("<BBBBBB", 0xFE, len(payload), seq, sysid, compid, msgid)
    crc = assembler.crc_x25(bytes([extra]), assembler.crc_x25(header[1:] + bytes(payload)))
    return header + bytes(payload) + struct.pack("<H", crc)


def tlog_record(frame, host_us):
    return struct.pack(">Q", host_us) + frame


def logging_payload(sequence, data, first_message_offset, target=(255, 0)):
    assert len(data) <= assembler.LOGGING_DATA_LEN
    return struct.pack("<HBBBB", sequence, target[0], target[1], len(data), first_message_offset) \
        + bytes(data).ljust(assembler.LOGGING_DATA_LEN, b"\x00")


def fragment_ulog(ulog, sizes, start_sequence=0, acked_header=True):
    """Split an authored ULog the way PX4 streams it: header section acked, first_message_offset exact."""
    bounds = ulog_boundaries(ulog)
    fragments = []
    offset = 0
    index = 0
    next_bound = 0  # bounds are ascending, so one linear pass finds each fragment's first start
    while offset < len(ulog):
        size = min(sizes[index % len(sizes)], len(ulog) - offset)
        chunk = ulog[offset:offset + size]
        while next_bound < len(bounds) and bounds[next_bound] < offset:
            next_bound += 1
        start = bounds[next_bound] if next_bound < len(bounds) else None
        first = (start - offset) if start is not None and start < offset + size \
            else assembler.NO_MESSAGE_START
        sequence = (start_sequence + index) % assembler.SEQUENCE_MODULO
        acked = acked_header and offset < assembler.ULOG_HEADER_LEN
        msgid = assembler.LOGGING_DATA_ACKED if acked else assembler.LOGGING_DATA
        fragments.append({"sequence": sequence, "msgid": msgid, "data": chunk,
                          "first_message_offset": first})
        offset += size
        index += 1
    return fragments


def saved_tlog(fragments, host_start_us=1_700_000_000_000_000, sysid=1, compid=1, unrelated=0):
    """Assemble authored fragments (plus optional unrelated traffic) into one saved TLOG."""
    out = bytearray()
    host = host_start_us
    for _ in range(unrelated):
        beat = bytes([0, 0, 0, 0, 1, 3, 0, 4, 3])
        extra = assembler.crc_extra("HEARTBEAT", HEARTBEAT_FIELDS)
        out += tlog_record(mavlink2_frame(0, beat, extra, sysid=sysid, compid=compid), host)
        host += 1000
    for index, fragment in enumerate(fragments):
        payload = logging_payload(fragment["sequence"], fragment["data"], fragment["first_message_offset"])
        extra = assembler.LOGGING_CRC_EXTRA[fragment["msgid"]]
        frame = mavlink2_frame(fragment["msgid"], payload, extra, seq=index % 256, sysid=sysid,
                               compid=compid)
        out += tlog_record(frame, host)
        host += 25_000
    return bytes(out)


def host_axis_tlog(host_start_us=1_700_000_000_000_000, beats=30):
    """A minimal saved GCS TLOG so the existing folder path has a host clock to work from."""
    out = bytearray()
    heartbeat_extra = assembler.crc_extra("HEARTBEAT", HEARTBEAT_FIELDS)
    system_time_extra = assembler.crc_extra("SYSTEM_TIME", SYSTEM_TIME_FIELDS)
    for step in range(beats):
        host = host_start_us + step * 500_000
        beat = struct.pack("<IBBBBB", 0, 1, 3, 81, 4, 3)
        out += tlog_record(mavlink2_frame(0, beat, heartbeat_extra, seq=step % 256), host)
        out += tlog_record(mavlink2_frame(2, struct.pack("<QI", host, 3_000_000 + step * 500_000),
                                          system_time_extra, seq=step % 256), host + 1000)
    return bytes(out)


class DerivationTests(unittest.TestCase):
    def test_crc_extra_derivation_matches_published_and_in_repo_values(self):
        self.assertEqual(assembler.LOGGING_CRC_EXTRA[assembler.LOGGING_DATA], 193)
        self.assertEqual(assembler.LOGGING_CRC_EXTRA[assembler.LOGGING_DATA_ACKED], 35)
        self.assertEqual(assembler.crc_extra("HEARTBEAT", HEARTBEAT_FIELDS), 50)
        self.assertEqual(assembler.crc_extra("SYSTEM_TIME", SYSTEM_TIME_FIELDS), 137)


class ReassemblyTests(unittest.TestCase):
    def test_two_distinguishable_ulogs_reassemble_byte_exactly_with_full_references(self):
        cases = [("gps", [assembler.LOGGING_DATA_LEN]), ("status", [37, 249, 128, 5])]
        digests = set()
        for variant, sizes in cases:
            original = authored_ulog(variant)
            fragments = fragment_ulog(original, sizes)
            self.assertGreater(len(fragments), 5, "authored input must need several fragments")
            ulog, refs = assembler.assemble(saved_tlog(fragments, unrelated=3))
            self.assertEqual(ulog, original)
            digests.add(hashlib.sha256(ulog).hexdigest())
            accounting = refs["accounting"]
            self.assertEqual(accounting["appended_fragments"], len(fragments))
            self.assertEqual(accounting["collapsed_duplicates"], 0)
            self.assertEqual(accounting["ulog_bytes"], len(original))
            self.assertEqual(accounting["ulog_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(accounting["unrelated_frames"], 3)
            self.assertEqual(refs["unrelated_messages"]["0"]["frames"], 3)
            self.assertEqual(len(refs["unrelated_messages"]["0"]["tlog_offsets"]), 3)
            self.assertEqual(accounting["ulog_message_counts"],
                             assembler.ulog_message_counts(original))
            for kind in ("B", "F", "I", "A", "D", "L"):
                self.assertGreater(accounting["ulog_message_counts"][kind], 0, kind)
            self.assertEqual(len(refs["fragments"]), len(fragments))
            covered = 0
            for entry, authored in zip(refs["fragments"], fragments):
                self.assertEqual(entry["logging_sequence"], authored["sequence"])
                self.assertEqual(entry["length"], len(authored["data"]))
                self.assertEqual(entry["first_message_offset"], authored["first_message_offset"])
                self.assertEqual(entry["data_sha256"], hashlib.sha256(authored["data"]).hexdigest())
                self.assertEqual(entry["output_start"], covered)
                self.assertEqual(entry["output_end"], covered + len(authored["data"]))
                self.assertEqual(ulog[entry["output_start"]:entry["output_end"]], authored["data"])
                self.assertIn("target_system", entry)
                self.assertIn("recorded_timestamp_word", entry)
                self.assertGreater(entry["tlog_offset"], 0)
                covered = entry["output_end"]
            self.assertEqual(covered, len(original))
            self.assertEqual(accounting["acked_fragments"], 1)
            self.assertEqual(accounting["unacked_fragments"], len(fragments) - 1)
            json.dumps(refs)  # references stay serialisable evidence
        self.assertEqual(len(digests), 2, "the two authored inputs must be distinguishable")

    def test_mixed_mavlink1_traffic_is_unrelated_accounting_not_a_fragment(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [201, 64])
        beat = bytes([0, 0, 0, 0, 1, 3, 0, 4, 3])
        extra = assembler.crc_extra("HEARTBEAT", HEARTBEAT_FIELDS)
        saved = tlog_record(mavlink1_frame(0, beat, extra), 1_699_999_000_000_000) + saved_tlog(fragments)
        ulog, refs = assembler.assemble(saved)
        self.assertEqual(ulog, original)
        self.assertEqual(refs["accounting"]["unrelated_frames"], 1)
        self.assertEqual(refs["unrelated_messages"]["0"]["tlog_offsets"], [0])

    def test_sequence_wrap_is_normal_and_counted(self):
        original = authored_ulog("status")
        fragments = fragment_ulog(original, [96], start_sequence=assembler.SEQUENCE_MODULO - 2)
        ulog, refs = assembler.assemble(saved_tlog(fragments))
        self.assertEqual(ulog, original)
        self.assertEqual(refs["accounting"]["sequence_wraps"], 1)
        self.assertEqual([f["logging_sequence"] for f in refs["fragments"][:3]], [65534, 65535, 0])

    def test_identical_retransmission_collapses_and_a_differing_repeat_is_refused(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [120])
        with_duplicate = fragments[:2] + [dict(fragments[1])] + fragments[2:]
        ulog, refs = assembler.assemble(saved_tlog(with_duplicate))
        self.assertEqual(ulog, original)
        self.assertEqual(refs["accounting"]["collapsed_duplicates"], 1)
        self.assertEqual(refs["accounting"]["appended_fragments"], len(fragments))
        self.assertEqual(len(refs["fragments"]), len(fragments) + 1)
        collapsed = [f for f in refs["fragments"] if f["collapsed_duplicate_of"] is not None]
        self.assertEqual(len(collapsed), 1)
        self.assertIsNone(collapsed[0]["output_start"])
        conflicting = dict(fragments[1], data=bytes(len(fragments[1]["data"])))
        self.assertEqual(self.refusal(saved_tlog(fragments[:2] + [conflicting] + fragments[2:])),
                         "DUPLICATE_CONFLICT")

    def test_a_large_authored_capture_passes_inside_the_declared_bounds(self):
        original = authored_ulog("gps")
        tail = b"".join(ulog_data(1, struct.pack("<QfbB", 30_000_000 + step * 1000, -70.0, 80, 0))
                        for step in range(120_000))
        big = original + tail
        self.assertGreater(len(big), 2 * 1024 * 1024)
        self.assertLess(len(big), assembler.MAX_ULOG_BYTES)
        fragments = fragment_ulog(big, [assembler.LOGGING_DATA_LEN])
        self.assertLess(len(fragments), assembler.MAX_FRAGMENTS)
        ulog, refs = assembler.assemble(saved_tlog(fragments))
        self.assertEqual(ulog, big)
        self.assertEqual(refs["accounting"]["appended_fragments"], len(fragments))
        self.assertEqual(refs["accounting"]["sequence_wraps"], len(fragments) // assembler.SEQUENCE_MODULO)
        self.assertEqual(len(refs["fragments"]), len(fragments))
        self.assertEqual(assembler.ulog_message_counts(ulog)["D"],
                         assembler.ulog_message_counts(original)["D"] + 120_000)

    def test_an_oversized_capture_is_refused_by_a_bounded_read_not_by_reading_it(self):
        requested = []

        class CountingPath:
            def __init__(self, data):
                self.data = data

            def open(self, mode="rb"):
                class Handle(io.BytesIO):
                    def read(self, size=-1):
                        requested.append(size)
                        return super().read(size)

                return Handle(self.data)

        keep = assembler.MAX_INPUT_BYTES
        try:
            assembler.MAX_INPUT_BYTES = 1_000
            with self.assertRaises(assembler.Refused) as caught:
                assembler.read_bounded(CountingPath(b"\x00" * 50_000))
            self.assertEqual(caught.exception.code, "INPUT_TOO_LARGE")
            self.assertEqual(requested, [1_001], "exactly one read of the bound plus one byte")
            requested.clear()
            self.assertEqual(len(assembler.read_bounded(CountingPath(b"\x00" * 1_000))), 1_000)
            self.assertEqual(requested, [1_001])
        finally:
            assembler.MAX_INPUT_BYTES = keep

    def test_cli_refuses_an_oversized_file_without_materialising_it(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "sparse.tlog"
            with source.open("wb") as handle:  # sparse: logical size only, no disk and no content
                handle.truncate(assembler.MAX_INPUT_BYTES + 1)
            self.assertEqual(source.stat().st_size, assembler.MAX_INPUT_BYTES + 1)
            target = Path(work) / "out"
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2)
            self.assertIn("INPUT_TOO_LARGE", done.stderr)
            self.assertFalse(target.exists())

    def test_reading_records_never_copies_the_remaining_capture(self):
        original = authored_ulog("status")
        fragments = fragment_ulog(original, [249])
        saved = saved_tlog(fragments, unrelated=4)
        records = len(fragments) + 4
        sizes = []
        real_memoryview = memoryview

        class Watched:
            def __init__(self, view):
                self.view = view

            def __len__(self):
                return len(self.view)

            def __getitem__(self, key):
                if isinstance(key, slice):
                    start, stop, _ = key.indices(len(self.view))
                    sizes.append(max(stop - start, 0))
                return self.view[key]

        assembler.memoryview = lambda data: Watched(real_memoryview(data))
        try:
            ulog, refs = assembler.assemble(saved)
        finally:
            del assembler.memoryview
        self.assertEqual(ulog, original)
        self.assertEqual(refs["input"]["records"], records)
        self.assertTrue(sizes, "the instrumented view must actually have been used")
        self.assertLessEqual(max(sizes), assembler.MAX_FRAME_BYTES,
                             "a slice larger than one record means the tail was copied")
        self.assertLessEqual(sum(sizes), 3 * len(saved),
                             "sliced volume must stay proportional to the capture, not quadratic")
        self.assertLessEqual(len(sizes), 6 * records)

    def test_each_declared_bound_refuses_loudly_instead_of_truncating(self):
        original = authored_ulog("status")
        saved = saved_tlog(fragment_ulog(original, [64]))
        for name, value, code in [("MAX_INPUT_BYTES", 32, "INPUT_TOO_LARGE"),
                                  ("MAX_RECORDS", 2, "TOO_MANY_RECORDS"),
                                  ("MAX_FRAGMENTS", 3, "TOO_MANY_FRAGMENTS"),
                                  ("MAX_ULOG_BYTES", 200, "ULOG_TOO_LARGE")]:
            keep = getattr(assembler, name)
            try:
                setattr(assembler, name, value)
                self.assertEqual(self.refusal(saved), code, name)
            finally:
                setattr(assembler, name, keep)
        ulog, _ = assembler.assemble(saved)  # the same input passes at the shipped bounds
        self.assertEqual(ulog, original)

    def test_the_retention_bound_is_enforced_while_collecting_not_after_parsing_everything(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [110])
        self.assertGreater(len(fragments), 4)
        corrupt = bytearray(saved_tlog(fragments))
        corrupt[-1] ^= 0xFF  # the last frame's CRC
        keep = assembler.MAX_FRAGMENTS
        try:
            assembler.MAX_FRAGMENTS = 3
            self.assertEqual(self.refusal(bytes(corrupt)), "TOO_MANY_FRAGMENTS")
        finally:
            assembler.MAX_FRAGMENTS = keep
        self.assertEqual(self.refusal(bytes(corrupt)), "CRC_MISMATCH")

    def refusal(self, data, source=None):
        with self.assertRaises(assembler.Refused) as caught:
            assembler.assemble(data, source=source)
        return caught.exception.code

    def test_missing_out_of_order_and_mid_stream_captures_are_refused(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [80])
        self.assertEqual(self.refusal(saved_tlog(fragments[:3] + fragments[4:])), "SEQUENCE_GAP")
        swapped = fragments[:3] + [fragments[4], fragments[3]] + fragments[5:]
        self.assertEqual(self.refusal(saved_tlog(swapped)), "SEQUENCE_GAP")
        backward = fragments[:4] + [fragments[2]] + fragments[4:]
        self.assertEqual(self.refusal(saved_tlog(backward)), "SEQUENCE_OUT_OF_ORDER")
        self.assertEqual(self.refusal(saved_tlog(fragments[1:])), "CAPTURE_STARTS_MID_MESSAGE")
        self.assertEqual(self.refusal(saved_tlog(fragments[:-1])), "TRUNCATED_ULOG_MESSAGE")
        headless = fragment_ulog(original, [80])[4:]
        headless[0] = dict(headless[0], first_message_offset=0)
        for index, fragment in enumerate(headless):
            fragment["sequence"] = index
        self.assertEqual(self.refusal(saved_tlog(headless)), "NO_ULOG_HEADER")

    def test_malformed_frames_and_fields_are_refused(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [110])
        good = saved_tlog(fragments)
        corrupt = bytearray(good)
        corrupt[-1] ^= 0xFF  # last frame's CRC
        self.assertEqual(self.refusal(bytes(corrupt)), "CRC_MISMATCH")
        self.assertEqual(self.refusal(good[:-4]), "FRAME_EXCEEDS_INPUT")
        self.assertEqual(self.refusal(good + b"\x00" * 9), "TRUNCATED_RECORD")
        bad_stx = bytearray(good)
        bad_stx[8] = 0xAA
        self.assertEqual(self.refusal(bytes(bad_stx)), "UNEXPECTED_FRAME_START")
        over_length = dict(fragments[1])
        payload = bytearray(logging_payload(over_length["sequence"], over_length["data"],
                                            over_length["first_message_offset"]))
        payload[4] = assembler.LOGGING_DATA_LEN + 1
        self.assertEqual(self.refusal(self.tlog_with_payload(fragments, 1, bytes(payload))),
                         "INVALID_LENGTH")
        payload[4] = len(fragments[1]["data"])
        payload[5] = len(fragments[1]["data"]) + 1
        self.assertEqual(self.refusal(self.tlog_with_payload(fragments, 1, bytes(payload))),
                         "INVALID_FIRST_MESSAGE_OFFSET")
        signed = self.tlog_with_payload(fragments, 1, None, incompat=0x01)
        ulog, refs = assembler.assemble(signed)  # signature skipped, never verified
        self.assertEqual(ulog, original)
        self.assertEqual(sum(1 for f in refs["fragments"] if f["signed_frame"]), 1)
        self.assertEqual(self.refusal(self.tlog_with_payload(fragments, 1, None, incompat=0x02)),
                         "UNSUPPORTED_INCOMPAT_FLAG")
        trimmed = self.tlog_with_payload(fragments, 1, None)
        ulog_trimmed, _ = assembler.assemble(trimmed)
        self.assertEqual(ulog_trimmed, original)

    def tlog_with_payload(self, fragments, index, payload, incompat=0, mavlink1=False):
        """Rebuild the saved TLOG with one fragment's payload or framing replaced."""
        out = bytearray()
        host = 1_700_000_000_000_000
        for position, fragment in enumerate(fragments):
            body = payload if (position == index and payload is not None) else logging_payload(
                fragment["sequence"], fragment["data"], fragment["first_message_offset"])
            extra = assembler.LOGGING_CRC_EXTRA[fragment["msgid"]]
            if position == index and mavlink1:
                frame = mavlink1_frame(fragment["msgid"], body, extra, seq=position % 256)
            else:
                frame = mavlink2_frame(fragment["msgid"], body, extra, seq=position % 256,
                                       truncate=position != index or incompat == 0,
                                       incompat=incompat if position == index else 0)
            out += tlog_record(frame, host)
            host += 25_000
        return bytes(out)

    def test_zero_selected_unrelated_only_and_several_senders_are_accounted_not_ingested(self):
        self.assertEqual(self.refusal(saved_tlog([], unrelated=5)), "NO_LOGGING_FRAGMENTS")
        original = authored_ulog("status")
        fragments = fragment_ulog(original, [130])
        mine = saved_tlog(fragments, sysid=1, compid=1)
        theirs = saved_tlog(fragment_ulog(authored_ulog("gps"), [130]), sysid=9, compid=2)
        both = mine + theirs
        self.assertEqual(self.refusal(both), "AMBIGUOUS_LOGGING_SOURCES")
        self.assertEqual(self.refusal(both, source=(3, 4)), "SOURCE_NOT_PRESENT")
        ulog, refs = assembler.assemble(both, source=(1, 1))
        self.assertEqual(ulog, original)
        self.assertEqual(refs["selected_source"], {"sysid": 1, "compid": 1})
        self.assertEqual(refs["other_logging_sources"],
                         [{"sysid": 9, "compid": 2, "fragments": len(fragment_ulog(
                             authored_ulog("gps"), [130]))}])
        self.assertEqual(refs["accounting"]["selected_fragments"], len(fragments))
        self.assertGreater(refs["accounting"]["logging_fragments"],
                           refs["accounting"]["selected_fragments"])


class CommandLineTests(unittest.TestCase):
    def run_cli(self, saved, out_dir, *extra):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "saved.tlog"
            source.write_bytes(saved)
            target = Path(work) / out_dir
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target), *extra],
                                  capture_output=True, text=True, check=False)
            files = sorted(p.name for p in target.iterdir()) if target.is_dir() else []
            payload = (target / "reconstructed.ulg").read_bytes() if "reconstructed.ulg" in files else b""
            return done, files, payload

    def test_cli_writes_reconstruction_and_references_then_refuses_with_a_reason_code(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [160])
        done, files, payload = self.run_cli(saved_tlog(fragments), "new")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(files, ["reconstructed.ulg", "references.json"])
        self.assertEqual(payload, original)
        self.assertIn("reconstructed", done.stdout)
        gapped, _, _ = self.run_cli(saved_tlog(fragments[:2] + fragments[3:]), "new")
        self.assertEqual(gapped.returncode, 2)
        self.assertIn("SEQUENCE_GAP", gapped.stderr)
        self.assertEqual(gapped.stdout, "")

    def test_cli_path_goes_through_the_bounded_read(self):
        original = authored_ulog("gps")
        saved = saved_tlog(fragment_ulog(original, [160]))
        calls = []
        real_read = assembler.read_bounded

        def watched(path, limit=None):
            calls.append(path)
            return real_read(path, limit)

        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "saved.tlog"
            source.write_bytes(saved)
            target = Path(work) / "out"
            argv = sys.argv
            assembler.read_bounded = watched
            try:
                sys.argv = [str(SCRIPT), str(source), str(target)]
                with contextlib.redirect_stdout(io.StringIO()):
                    assembler.main()
            finally:
                sys.argv = argv
                assembler.read_bounded = real_read
            self.assertEqual([str(p) for p in calls], [str(source)],
                             "the command line must read through the bounded read, not the whole file")
            self.assertEqual((target / "reconstructed.ulg").read_bytes(), original)

    def test_cli_never_overwrites_an_existing_output_directory(self):
        original = authored_ulog("status")
        saved = saved_tlog(fragment_ulog(original, [90]))
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "saved.tlog"
            source.write_bytes(saved)
            target = Path(work) / "existing"
            target.mkdir()
            (target / "evidence.txt").write_text("keep", encoding="utf-8")
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2)
            self.assertEqual((target / "evidence.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["evidence.txt"])


@unittest.skipUnless(os.environ.get("MUSUBI_REFERENCE_VIEWER_BIN"),
                     "set MUSUBI_REFERENCE_VIEWER_BIN to the existing headless folder reader to check "
                     "PX4/common semantics on the reconstruction")
class Px4SemanticsTests(unittest.TestCase):
    """The reconstruction must be read by the adopted PX4 reader, not merely start with the magic."""

    def export(self, work, ulog_bytes):
        """Run the existing headless folder reader over the reconstruction plus a host-clock capture."""
        folder = Path(work) / "folder"
        folder.mkdir()
        (folder / "plane-01_gcs.tlog").write_bytes(host_axis_tlog())
        (folder / "plane-01_fc.ulg").write_bytes(ulog_bytes)
        export = Path(work) / "export"
        done = subprocess.run([os.environ["MUSUBI_REFERENCE_VIEWER_BIN"], "--in", str(folder),
                               "--export", str(export), "--profiles", str(ROOT / "profiles"),
                               "--asset-family", "plane-01=fixed_wing"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        log = done.stdout + done.stderr
        self.assertIn("loaded 2 files", log)
        self.assertNotIn("skipped", log.lower())
        rows = (export / "absences.csv").read_text(encoding="utf-8").splitlines()
        header = rows[0].split(",")
        answer = (export / "answer.txt").read_text(encoding="utf-8")
        return [dict(zip(header, row.split(","))) for row in rows[1:]], answer

    def test_reconstructed_ulog_reaches_px4_channel_semantics(self):
        expectations = {"gps": ({"onboard"}, {"gps_ekf", "rc"}),
                        "status": ({"onboard", "gps_ekf", "rc"}, {"link_stats"})}
        for variant, sizes in [("gps", [assembler.LOGGING_DATA_LEN]), ("status", [64, 249, 7])]:
            absent_expected, observed_expected = expectations[variant]
            original = authored_ulog(variant)
            ulog, _ = assembler.assemble(saved_tlog(fragment_ulog(original, sizes)))
            self.assertEqual(ulog, original)
            with tempfile.TemporaryDirectory() as work:
                rows, answer = self.export(work, ulog)
                from_ulog = [r for r in rows if r["profile_id"] == "px4_ulg"]
                self.assertTrue(from_ulog, f"{variant}: the PX4 reader produced no channel rows")
                for row in from_ulog:
                    self.assertEqual(row["file"], "plane-01_fc.ulg")
                    self.assertEqual(row["source_role"], "fc")
                    self.assertEqual(row["family"], "fixed_wing")
                    self.assertEqual(row["clock_basis"], "boot_relative_offset_estimated")
                    self.assertTrue(row["last_good_digest"].startswith(("sha256:", "never-observed")))
                absent = {row["channel"] for row in from_ulog}
                self.assertEqual(absent, absent_expected, f"{variant}: absent px4 channels")
                self.assertFalse(observed_expected & absent,
                                 f"{variant}: declared topics must read as observed, not absent")
                self.assertIn("px4_ulg", answer)

    def test_a_refused_capture_never_produces_a_file_to_read(self):
        original = authored_ulog("gps")
        fragments = fragment_ulog(original, [100])
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "saved.tlog"
            source.write_bytes(saved_tlog(fragments[:3] + fragments[4:]))
            target = Path(work) / "out"
            done = subprocess.run([sys.executable, str(SCRIPT), str(source), str(target)],
                                  capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 2)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
