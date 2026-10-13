"""Qualified single-interface saved PCAPNG to existing passive UDP meanings."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import struct

import dpkt
from dpkt import pcapng as ng

try:
    from scripts.convert_imc_sensors import convert_pcap, OUTPUT_LIMIT
except ModuleNotFoundError:
    from convert_imc_sensors import convert_pcap, OUTPUT_LIMIT

LIMIT = 16 * 1024 * 1024
REPORT_LIMIT = 32 * 1024 * 1024
SOURCE_LIMIT = 2 * 1024**3
PACKET_LIMIT = 1000000


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def options(block, raw, offset, allowed, endian):
    """Validate option framing not enforced by dpkt; decode remains in dpkt."""
    end, seen = len(raw) - 4, set()
    while offset < end:
        if offset + 4 > end:
            raise ValueError("truncated option header")
        code, length = struct.unpack_from(endian + "HH", raw, offset)
        offset += 4
        if code == 0:
            if length or offset != end:
                raise ValueError("invalid option terminator")
            break
        if code not in allowed or code in seen:
            raise ValueError("unsupported or duplicate option")
        seen.add(code)
        padded = (length + 3) & ~3
        if offset + padded > end or any(raw[offset + length:offset + padded]):
            raise ValueError("truncated option or nonzero padding")
        offset += padded
    return {opt.code: opt.data for opt in block.opts if opt.code}


def qualify(data):
    if not data or len(data) > LIMIT:
        raise ValueError("bounded PCAPNG section required")
    return qualify_stream(io.BytesIO(data))


def qualify_stream(source, start_us=None, stop_us=None):
    window = start_us is not None or stop_us is not None
    if window and (type(start_us) is not int or type(stop_us) is not int or not 0 <= start_us < stop_us):
        raise ValueError("explicit increasing nonnegative microsecond interval required")
    digest, total = hashlib.sha256(), 0

    def read(size):
        nonlocal total
        chunk = source.read(size)
        total += len(chunk)
        if total > SOURCE_LIMIT:
            raise ValueError("original capture exceeds bound")
        digest.update(chunk)
        return chunk

    first = read(12)
    if len(first) != 12 or first[:4] != b"\x0a\x0d\x0d\x0a":
        raise ValueError("PCAPNG section required")
    endian = {b"\x4d\x3c\x2b\x1a": "<", b"\x1a\x2b\x3c\x4d": ">"}.get(first[8:12])
    if endian is None:
        raise ValueError("invalid section byte order")
    classes = ((ng.SectionHeaderBlockLE, ng.InterfaceDescriptionBlockLE, ng.EnhancedPacketBlockLE)
               if endian == "<" else (ng.SectionHeaderBlock, ng.InterfaceDescriptionBlock, ng.EnhancedPacketBlock))
    offset, ordinal, divisor, time_offset = 0, 0, 1000000, 0
    packets, derived, snaplen = [], bytearray(), None
    original_packets, previous, selected_bytes = 0, None, 0
    prefix = first
    while prefix:
        if len(prefix) != 12:
            raise ValueError("truncated block")
        kind, length = struct.unpack_from(endian + "II", prefix)
        if length < 12 or length > LIMIT or length % 4:
            raise ValueError("invalid block length")
        raw = prefix + read(length - 12)
        if len(raw) != length:
            raise ValueError("truncated block")
        expected = ng.PCAPNG_BT_SHB if ordinal == 0 else ng.PCAPNG_BT_IDB if ordinal == 1 else ng.PCAPNG_BT_EPB
        if kind != expected:
            raise ValueError("only one section/interface and enhanced packets are qualified")
        block = classes[min(ordinal, 2)](raw)
        if ordinal == 0:
            if (block.v_major, block.v_minor, block.sec_len) != (1, 0, -1):
                raise ValueError("section 1.0 with unspecified length required")
            options(block, raw, 24, {1}, endian)
        elif ordinal == 1:
            opts = options(block, raw, 16, {1, 2, 3, 9, 14}, endian)
            if block.linktype != 1 or block._reserved or not 0 <= block.snaplen <= 0xffffffff:
                raise ValueError("Ethernet interface required")
            snaplen = block.snaplen
            if 9 in opts:
                if len(opts[9]) != 1:
                    raise ValueError("invalid timestamp resolution")
                res = opts[9][0]
                divisor = (2 if res & 128 else 10) ** (res & 127)
            if 14 in opts:
                if len(opts[14]) != 8:
                    raise ValueError("invalid timestamp offset")
                time_offset = struct.unpack(endian + "q", opts[14])[0]
            derived.extend(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, snaplen or 0xffffffff, 1))
        else:
            if (block.iface_id != 0 or block.caplen != block.pkt_len or
                    block.caplen != len(block.pkt_data) or (snaplen and block.caplen > snaplen)):
                raise ValueError("wrong interface or incomplete packet")
            payload_end = 28 + ((block.caplen + 3) & ~3)
            if payload_end > length - 4 or any(raw[28 + block.caplen:payload_end]):
                raise ValueError("invalid packet extent or padding")
            options(block, raw, payload_end, {1}, endian)
            ticks = (block.ts_high << 32) | block.ts_low
            micros, remainder = divmod(ticks * 1000000, divisor)
            micros += time_offset * 1000000
            if remainder or not 0 <= micros < (1 << 32) * 1000000:
                raise ValueError("timestamp not exactly representable in classic microseconds")
            if window and previous is not None and micros < previous:
                raise ValueError("original capture time decreases")
            previous = micros
            if original_packets >= PACKET_LIMIT:
                raise ValueError("original packet count exceeds bound")
            selected = not window or start_us <= micros < stop_us
            if selected:
                selected_bytes += length
                if selected_bytes > LIMIT or len(derived) + 16 + block.caplen > LIMIT:
                    raise ValueError("selected capture exceeds bound")
                sec, usec = divmod(micros, 1000000)
                derived.extend(struct.pack("<IIII", sec, usec, block.caplen, block.pkt_len))
                derived.extend(block.pkt_data)
                packets.append(dict(packet_index=original_packets, block_ordinal=ordinal,
                    block_byte_offset=offset, timestamp_ticks=ticks, capture_time_us=micros,
                    captured_length=block.caplen, original_length=block.pkt_len, interface_id=0))
            original_packets += 1
        offset += length
        ordinal += 1
        prefix = read(12)
    if not packets or len(derived) > LIMIT:
        raise ValueError("no packets or derived capture exceeds bound")
    provenance = dict(section_version="1.0", byte_order=endian, snaplen=snaplen,
        timestamp_units_per_second=divisor, timestamp_offset_seconds=time_offset, packets=packets)
    if window:
        provenance["window"] = dict(start_us=start_us, stop_us=stop_us,
            original_packets=original_packets, selected_packets=len(packets),
            excluded_packets=original_packets-len(packets), original_bytes=total,
            original_sha256="sha256:" + digest.hexdigest(),
            excluded_payload_status="NOT_DECODED_OR_CHECKSUM_VALIDATED")
    return bytes(derived), provenance


def convert(data, port, *, payload_format="imc"):
    try:
        derived, provenance = qualify(data)
    except (dpkt.UnpackError, struct.error, IndexError, UnicodeError) as exc:
        raise ValueError("invalid PCAPNG structure") from exc
    original_sha = "sha256:" + hashlib.sha256(data).hexdigest()
    return compose(derived, provenance, original_sha, port, payload_format)


def convert_path(path, port, *, start_us, stop_us, payload_format="imc"):
    try:
        with Path(path).open("rb") as source:
            derived, provenance = qualify_stream(source, start_us, stop_us)
    except (dpkt.UnpackError, struct.error, IndexError, UnicodeError) as exc:
        raise ValueError("invalid PCAPNG structure") from exc
    return compose(derived, provenance, provenance["window"]["original_sha256"], port, payload_format)


def compose(derived, provenance, original_sha, port, payload_format):
    text, report = convert_pcap(derived, port, payload_format=payload_format)
    derived_sha = report["source_sha256"]
    report.update(source_sha256=original_sha, source_format="qualified PCAPNG 1.0",
        derived_capture_sha256=derived_sha, pcapng=provenance,
        original_required="Exact PCAPNG retained; hashes are not producer authentication")
    if "window" in provenance:
        report["packet_count_scope"] = "SELECTED_INTERVAL_ONLY"
        for saved in report["selected_sources"]:
            local = saved["packet_index"]
            saved.update(packet_index=provenance["packets"][local]["packet_index"], window_packet_index=local)
    out = io.StringIO(newline="")
    reader = csv.DictReader(io.StringIO(text))
    writer = csv.DictWriter(out, reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in reader:
        row["pcap_source_sha256"] = original_sha
        row["pcap_packet_index"] = provenance["packets"][int(row["pcap_packet_index"])]["packet_index"]
        writer.writerow(row)
    if len(out.getvalue().encode()) > OUTPUT_LIMIT or len(encoded(report)) > REPORT_LIMIT:
        raise ValueError("output bound exceeded")
    return out.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--udp-destination-port", required=True, type=int)
    parser.add_argument("--payload-format", choices=("imc", "nmea-wind"), default="imc")
    parser.add_argument("--start-us", type=int)
    parser.add_argument("--stop-us", type=int)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output already exists")
        if args.start_us is not None or args.stop_us is not None:
            data = None
            text, report = convert_path(args.input, args.udp_destination_port,
                start_us=args.start_us, stop_us=args.stop_us, payload_format=args.payload_format)
        else:
            with args.input.open("rb") as source:
                data = source.read(LIMIT + 1)
            text, report = convert(data, args.udp_destination_port, payload_format=args.payload_format)
        args.output.mkdir(parents=True, exist_ok=False)
        if data is not None:
            (args.output / "source.pcapng").write_bytes(data)
        (args.output / "observations.csv").write_text(text)
        (args.output / "report.json").write_bytes(encoded(report) + b"\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Saved PCAPNG refused or output incomplete: {exc}\n")


if __name__ == "__main__":
    main()
