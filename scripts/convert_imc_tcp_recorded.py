"""Finite saved one-direction SYN-to-FIN TCP stream, not a general TCP stack.

Library-only convert_path uses caller IPv4 four-tuple and explicit capture interval.
Keep original PCAP, returned CSV and mandatory report together. The report binds
original/global packets to stream byte spans, native IMC frames and output ordinals.
Original<=2GiB/1M packets and selected<=16MiB use the existing capture scanner;
stream<=16MiB, selected TCP<=65536, CSV<=128MiB and report<=32MiB remain bounded.
Only exact payload-range retransmissions are accepted; no partial overlap/reorder,
SYN/FIN retransmission, new epoch, TCP options interpretation or live acquisition.
SYN/FIN delimit only this saved direction, not peer delivery/session completeness.
Existing IMC schema/unit/CRC/time semantics apply, with capture times kept in the
report and never substituted for dispatcher time. Unknown messages stay accounted.
No writer qualification, TLS, compressed application stream or new IMC decoder.
Optional estimated_state=True forwards fixed current/explicit Neptus350 to the
same adopted resolver; it does not change transport, clock or platform identity.

Example (supply your own confirmed direction/interval; use a new output folder):
    csv_text, report = convert_path(path, flow, start_us=start, stop_us=stop)
    out.mkdir(exist_ok=False)
    (out / "observations.csv").write_text(csv_text, encoding="utf-8", newline="")
    (out / "report.json").write_text(json.dumps(report, indent=2)+"\\n", encoding="utf-8")
Retain the original path content under report["source_sha256"]. Failure during
these writes leaves an incomplete output set, never a successful saved artifact.
"""
import csv
import hashlib
import io
import ipaddress
import json
import struct

from scripts import convert_imc_sensors as imc

MASK = 2**32 - 1


def convert_path(path, flow, *, start_us, stop_us, single_vlan=False, schema=None,
                 water_velocity=False, estimated_state=False):
    """Return common CSV plus mandatory original/packet/stream/frame mapping report."""
    import dpkt
    if dpkt.__version__ != "1.9.8":
        raise ValueError("qualified dpkt1.9.8 required")
    if type(flow) is not dict or set(flow) != {"source_address", "source_port", "destination_address", "destination_port"}:
        raise ValueError("explicit direction and IPv4 four-tuple required")
    for key in ("source_address", "destination_address"):
        if type(flow[key]) is not str or str(ipaddress.IPv4Address(flow[key])) != flow[key]:
            raise ValueError("canonical IPv4 required")
    for key in ("source_port", "destination_port"):
        if type(flow[key]) is not int or not 1 <= flow[key] <= 65535:
            raise ValueError("explicit valid port required")
    if type(single_vlan) is not bool or type(water_velocity) is not bool:
        raise ValueError("boolean options required")
    imc.estimated_selection(estimated_state, schema)
    data, indexes, window = imc.read_pcap_window(path, start_us, stop_us)
    link, times = imc.read_pcap_records(data)
    if link != 1:
        raise ValueError("saved Ethernet TCP only")
    stream, packets, spans, seen = bytearray(), [], [], {}
    started = finished = False
    expected = None
    ignored = 0
    for local, (_, packet) in enumerate(dpkt.pcap.Reader(io.BytesIO(data))):
        network = imc.packet_ipv4(packet, link, single_vlan=single_vlan)
        entry = dict(packet_index=indexes[local], capture_time_us=times[local],
                     packet_sha256=hashlib.sha256(packet).hexdigest())
        if network is None:
            ignored += 1
            continue
        try:
            ip = dpkt.ip.IP(network)
        except dpkt.UnpackError as exc:
            raise ValueError("malformed IPv4") from exc
        if ip.v != 4 or ip.hl < 5 or not ip.hl * 4 <= ip.len <= len(network):
            raise ValueError("invalid IPv4 length")
        addresses = (str(ipaddress.IPv4Address(ip.src)), str(ipaddress.IPv4Address(ip.dst)))
        if ip.p != 6 or addresses != (flow["source_address"], flow["destination_address"]):
            ignored += 1
            continue
        if ip.offset or ip.mf:
            raise ValueError("fragmented candidate TCP is unsupported")
        raw = network[ip.hl * 4:ip.len]
        try:
            tcp = dpkt.tcp.TCP(raw)
        except dpkt.UnpackError as exc:
            raise ValueError("malformed TCP") from exc
        if (tcp.sport, tcp.dport) != (flow["source_port"], flow["destination_port"]):
            ignored += 1
            continue
        if tcp.off < 5 or tcp.off * 4 > len(raw) or raw[12] & 15 or tcp.flags & ~(1 | 2 | 8 | 16):
            raise ValueError("unsupported TCP header/flags")
        if dpkt.in_cksum(network[:ip.hl * 4]) or dpkt.in_cksum(ip.src + ip.dst + struct.pack(">BBH", 0, 6, len(raw)) + raw):
            raise ValueError("invalid selected IPv4/TCP checksum")
        payload = raw[tcp.off * 4:]
        syn, fin = bool(tcp.flags & 2), bool(tcp.flags & 1)
        if len(packets) >= 65536:
            raise ValueError("selected TCP packet limit")
        if not started:
            if not syn or fin or payload or tcp.flags & 8:
                raise ValueError("selected stream must begin with payload-free SYN")
            started, expected = True, (tcp.seq + 1) & MASK
            entry["disposition"] = "SYN_START_NOT_AUTHENTICATED"
        else:
            if finished or syn:
                raise ValueError("new epoch or packet after FIN")
            key = (tcp.seq, len(payload))
            if payload and not fin and key in seen:
                old_start, old_stop = seen[key]
                if payload != stream[old_start:old_stop]:
                    raise ValueError("conflicting retransmission")
                entry.update(disposition="IDENTICAL_PAYLOAD_RETRANSMISSION", stream_start=old_start, stream_stop=old_stop)
            else:
                if tcp.seq != expected:
                    raise ValueError("gap/reorder/partial overlap in selected stream")
                begin = len(stream)
                stream.extend(payload)
                if len(stream) > imc.LIMIT:
                    raise ValueError("reassembled stream bound")
                expected = (expected + len(payload) + int(fin)) & MASK
                if payload:
                    seen[key] = (begin, len(stream))
                    spans.append(dict(stream_start=begin, stream_stop=len(stream), packet_index=indexes[local], capture_time_us=times[local]))
                finished = fin
                entry.update(disposition="FIN_END" if fin else "PAYLOAD" if payload else "ACK_ONLY",
                             stream_start=begin, stream_stop=len(stream))
        packets.append(entry)
    if not started or not finished or not stream:
        raise ValueError("complete nonempty SYN-to-FIN stream required")
    output, semantics = imc.convert_input(bytes(stream), schema=schema, water_velocity=water_velocity,
                                           estimated_state=estimated_state)
    rows = list(csv.DictReader(io.StringIO(output)))
    frames, offset, row_index, span_index = [], 0, 0, 0
    while offset < len(stream):
        _, message, size, _, _, _ = imc._imc_header(stream[offset:offset + 20])
        stop = offset + 22 + size
        frame_hex = "hex:" + stream[offset:stop].hex()
        while spans[span_index]["stream_stop"] <= offset:
            span_index += 1
        linked, j = [], span_index
        while j < len(spans) and spans[j]["stream_start"] < stop:
            linked.append(dict(packet_index=spans[j]["packet_index"],
                               stream_start=max(offset, spans[j]["stream_start"]),
                               stream_stop=min(stop, spans[j]["stream_stop"])))
            j += 1
        chosen = row_index < len(rows) and rows[row_index]["source_frame_hex"] == frame_hex
        frames.append(dict(frame_index=len(frames), message_id=message, stream_start=offset,
                           stream_stop=stop, packet_spans=linked,
                           output_index=row_index if chosen else None))
        if chosen:
            rows[row_index].update(pcap_packet_index=linked[-1]["packet_index"],
                pcap_source_sha256=window["source_sha256"],
                pcap_clock_basis="CAPTURE_REPORTED_SEPARATE_IN_REPORT_NOT_MESSAGE_CLOCK")
            row_index += 1
        offset = stop
    if row_index != len(rows) or len(frames) != semantics["source_frames"]:
        raise ValueError("IMC output/frame accounting mismatch")
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, rows[0].keys(), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    result = target.getvalue()
    report = dict(source_sha256=window["source_sha256"], flow=dict(flow), capture_window=window,
                  selected_tcp_packets=len(packets), unselected_packets=ignored, packets=packets,
                  stream_sha256=hashlib.sha256(stream).hexdigest(), stream_bytes=len(stream),
                  frames=frames, imc_report=semantics, output_rows=len(rows),
                  output_sha256=hashlib.sha256(result.encode()).hexdigest(),
                  basis="SAVED_ONE_DIRECTION_CONTIGUOUS_SYN_FIN_NOT_PEER_DELIVERY_OR_CAPTURE_COMPLETENESS",
                  source_retention="original PCAP and this report required; hash is not authentication")
    if len(result.encode()) > imc.OUTPUT_LIMIT or len(json.dumps(report, indent=2).encode()) + 1 > 32 * 1024**2:
        raise ValueError("output/report bound")
    return result, report
