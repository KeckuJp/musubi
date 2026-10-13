"""Supplied MQTT3.1.1 PUBLISH packets to existing saved payload semantics; no network."""
import argparse
import hashlib
import json
import mmap
from pathlib import Path
import sys

try:
    from . import convert_mqtt_recorded as existing
except ImportError:
    import convert_mqtt_recorded as existing

MAX_PACKETS = 32768
BASIS = 'RECONSTRUCTED_SAVED_MQTT311_PUBLISH_NOT_MOSQUITTO_CAPTURE'


def convert(data, *, selected_packets, topic, payload_format, capture_timestamp, protocol, _window=False,
            vda_actions=False, vda_errors=False):
    if type(_window) is not bool:
        raise ValueError('window selector must be bool')
    if protocol != '3.1.1':
        raise ValueError('explicit MQTT3.1.1 protocol declaration required')
    if (not isinstance(data, (bytes, mmap.mmap) if _window else bytes)
            or not data or len(data) > (2**31 if _window else existing.LIMIT)):
        raise ValueError('empty or oversized supplied packet sequence')
    if (not isinstance(selected_packets, list) or not selected_packets
            or any(type(x) is not int or x < 0 for x in selected_packets)
            or len(set(selected_packets)) != len(selected_packets)):
        raise ValueError('nonempty unique nonnegative packet indices required')
    if _window and len(selected_packets) > MAX_PACKETS:
        raise ValueError('selected packet count bound exceeded')
    report = dict(schema='mqtt311-saved-publish/v1', source_basis=BASIS, packets=[],
        source_packets=0, selected_packets=0, unselected_packets=0,
        source_sha256=hashlib.sha256(data).hexdigest(), source_bytes=len(data),
        source_retention='KEEP_ORIGINAL_PACKET_BYTES_WITH_REPORT',
        clock_basis='CALLER_REPORTED_CAPTURE_UNKNOWN_NOT_PACKET_TIME',
        delivery='NOT_ESTABLISHED_BY_QOS_DUP_RETAIN_OR_PACKET_IDENTIFIER')
    selected, records, offset = set(selected_packets), [], 0
    selected_bytes = 0
    while offset < len(data):
        if report['source_packets'] >= (5_000_000 if _window else MAX_PACKETS):
            raise ValueError('packet count bound exceeded; supply an aligned subset')
        start, header = offset, data[offset]
        offset += 1
        qos, dup, retain = (header >> 1) & 3, (header >> 3) & 1, header & 1
        if header >> 4 != 3 or qos == 3 or (qos == 0 and dup):
            raise ValueError('only valid MQTT3.1.1 PUBLISH flags accepted')
        length = 0
        for index in range(4):
            if offset >= len(data):
                raise ValueError('truncated remaining length')
            byte = data[offset]
            offset += 1
            length += (byte & 127) << (index * 7)
            if not byte & 128:
                if index and length < 128**index:
                    raise ValueError('noncanonical remaining length')
                break
        else:
            raise ValueError('remaining length exceeds four bytes')
        end = offset + length
        if end > len(data) or length < 2:
            raise ValueError('truncated packet or topic length')
        topic_length = int.from_bytes(data[offset:offset + 2], 'big')
        offset += 2
        if not topic_length or offset + topic_length > end:
            raise ValueError('invalid topic length')
        try:
            packet_topic = data[offset:offset + topic_length].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('invalid topic UTF8') from exc
        offset += topic_length
        if any(c in packet_topic for c in '#+\x00'):
            raise ValueError('wildcard or NUL topic refused')
        mid = None
        if qos:
            if offset + 2 > end:
                raise ValueError('truncated packet identifier')
            mid = int.from_bytes(data[offset:offset + 2], 'big')
            offset += 2
            if not mid:
                raise ValueError('zero packet identifier')
        packet_index = report['source_packets']
        chosen = packet_index in selected
        if not _window or chosen:
            raw_sha = hashlib.sha256(memoryview(data)[start:end]).hexdigest()
            report['packets'].append(dict(index=packet_index, byte_offset=start, byte_end=end,
                sha256=raw_sha, topic_hex=packet_topic.encode().hex(), qos=qos, dup=dup,
                retain=retain, packet_identifier=mid,
                disposition='SELECTED_PAYLOAD' if chosen else 'PAYLOAD_NOT_DECODED'))
        report['source_packets'] += 1
        report['selected_packets' if chosen else 'unselected_packets'] += 1
        if chosen:
            if end-offset > existing.LIMIT:
                raise ValueError('selected payload byte bound exceeded')
            payload = data[offset:end]
            try:
                text = payload.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ValueError('selected payload requires UTF8') from exc
            record = dict(tst=capture_timestamp, topic=packet_topic, qos=qos, retain=retain,
                payloadlen=len(payload), payload=text, source_basis=BASIS, dup=dup,
                raw_packet_index=packet_index, raw_byte_offset=start, raw_packet_sha256=raw_sha)
            if _window:
                record['saved_source_sha256'] = report['source_sha256']
            if mid is not None:
                record['mid'] = mid
            encoded = json.dumps(record)
            selected_bytes += len(encoded.encode()) + bool(records)
            if selected_bytes > existing.LIMIT:
                raise ValueError('selected envelope input bound exceeded')
            records.append(encoded)
        offset = end
    if any(index >= report['source_packets'] for index in selected):
        raise ValueError('selected packet index absent')
    envelope_text = '\n'.join(records)
    output = existing.convert(envelope_text, topic, payload_format,
                              vda_actions=vda_actions, vda_errors=vda_errors)
    report['generated_envelopes_sha256'] = hashlib.sha256(envelope_text.encode()).hexdigest()
    report['output_sha256'] = hashlib.sha256(output.encode()).hexdigest()
    if _window:
        report.update(selection_scope='EXPLICIT_PACKET_INDICES',
            packet_details_scope='SELECTED_ONLY',
            unselected_semantics='PAYLOAD_NOT_DECODED_STRUCTURAL_PACKET_TOPIC_ONLY')
    if len(json.dumps(report).encode()) > existing.LIMIT:
        raise ValueError('packet report bound exceeded')
    return output, report


def convert_path(path, **options):
    try:
        from .saved_binary_snapshot import convert_snapshot
    except ImportError:
        from saved_binary_snapshot import convert_snapshot
    return convert_snapshot(path, convert, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--packet-index', required=True, action='append', type=int)
    parser.add_argument('--topic', required=True)
    parser.add_argument('--payload-format', required=True, choices=existing.FORMATS)
    parser.add_argument('--capture-timestamp', required=True)
    parser.add_argument('--protocol', required=True, choices=['3.1.1'])
    parser.add_argument('--saved-window', action='store_true')
    parser.add_argument('--vda-actions', action='store_true')
    parser.add_argument('--vda-errors', action='store_true')
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError('output paths must be distinct and new')
        if args.saved_window:
            data = args.input
        else:
            with args.input.open('rb') as source:
                data = source.read(existing.LIMIT + 1)
        output, report = (convert_path if args.saved_window else convert)(data, selected_packets=args.packet_index, topic=args.topic,
            payload_format=args.payload_format, capture_timestamp=args.capture_timestamp, protocol=args.protocol,
            vda_actions=args.vda_actions, vda_errors=args.vda_errors)
        with args.output.open('x', encoding='utf-8', newline='') as target:
            target.write(output)
        with args.report.open('x', encoding='utf-8') as target:
            json.dump(report, target, indent=2)
        return 0
    except (ValueError, OSError) as exc:
        print(f'saved MQTT PUBLISH conversion rejected: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
