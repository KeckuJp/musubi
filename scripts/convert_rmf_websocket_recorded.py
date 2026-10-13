"""Finite saved RFC6455 text frames to the adopted RMF Socket.IO meaning path."""
import argparse
import hashlib
import json
import mmap
from pathlib import Path
import sys

try:
    from . import convert_ardupilot_battery_csv as existing
except ImportError:
    import convert_ardupilot_battery_csv as existing

MAX_INPUT = 16 * 1024 * 1024
MAX_MESSAGE = 512 * 1024
MAX_FRAMES = 32768


def envelope(packet, capture_time_us, location_unit, yaw_unit):
    result = dict(schema_commit='a736ddebeee9eaf3970756d7dd452065273da918',
        protocol_version=5, transport='websocket-complete-text', direction='received',
        capture_time_us=capture_time_us, packet=packet)
    if location_unit is not None or yaw_unit is not None:
        result.update(location_unit=location_unit, yaw_unit=yaw_unit)
    return result


def convert(data, *, selected_messages, capture_time_us, direction, extensions,
            location_unit=None, yaw_unit=None, _window=False):
    if type(_window) is not bool:
        raise ValueError('window selector must be bool')
    if (not isinstance(data, (bytes, mmap.mmap) if _window else bytes)
            or not data or len(data) > (2**31 if _window else MAX_INPUT)):
        raise ValueError('empty or oversized saved frame sequence')
    if direction != 'server-to-client' or extensions != 'none':
        raise ValueError('explicit server-to-client direction and no extensions required')
    if (not isinstance(selected_messages, list) or not selected_messages
            or any(type(x) is not int or x < 0 for x in selected_messages)
            or len(set(selected_messages)) != len(selected_messages)):
        raise ValueError('nonempty unique nonnegative text-message indices required')
    if _window and len(selected_messages) > MAX_FRAMES:
        raise ValueError('selected message count bound exceeded')
    if type(capture_time_us) is not int or not 0 <= capture_time_us < 2**64:
        raise ValueError('capture timestamp must be an unsigned integer in microseconds')
    if (location_unit is not None or yaw_unit is not None) and (location_unit, yaw_unit) != ('m', 'rad'):
        raise ValueError('location requires explicit metres and radians')
    report = dict(schema='rmf-saved-websocket-frames/v1', source_frames=0, text_messages=0,
        control_frames=0, selected_text_messages=0, unselected_text_messages=0, frames=[], messages=[],
        source_sha256=hashlib.sha256(data).hexdigest(), source_bytes=len(data),
        source_retention='KEEP_ORIGINAL_BINARY_WITH_THIS_REPORT',
        clock_basis='UNKNOWN_CALLER_CAPTURE_NOT_SERVER_EVENT',
        capture_completeness='NOT_ESTABLISHED_FRAME_ALIGNED_SUPPLIED_SEQUENCE_ONLY')
    offset, pending, indices, records = 0, None, [], []
    frame_offsets = []
    selected_bytes = 0
    selected = set(selected_messages)
    while offset < len(data):
        if report['source_frames'] >= (5_000_000 if _window else MAX_FRAMES):
            raise ValueError('saved frame count bound exceeded; supply a smaller aligned sequence')
        start = offset
        if len(data) - offset < 2:
            raise ValueError('truncated frame header')
        first, second = data[offset:offset + 2]
        offset += 2
        final, opcode, length = bool(first & 128), first & 15, second & 127
        if first & 112 or second & 128:
            raise ValueError('RSV/extensions or masked server frame unsupported')
        if opcode not in (0, 1, 9, 10):
            raise ValueError('only text, continuation, ping and pong accepted')
        if length in (126, 127):
            width = 2 if length == 126 else 8
            if len(data) - offset < width:
                raise ValueError('truncated extended frame length')
            length = int.from_bytes(data[offset:offset + width], 'big')
            offset += width
            if length < (126 if width == 2 else 65536) or length >= 2**63:
                raise ValueError('noncanonical frame length')
        if length > MAX_MESSAGE or len(data) - offset < length:
            raise ValueError('oversized or truncated frame payload')
        payload = data[offset:offset + length]
        offset += length
        frame_index = report['source_frames']
        retain_frame = not _window or report['text_messages'] in selected
        if retain_frame:
            if len(report['frames']) >= MAX_FRAMES:
                raise ValueError('selected frame detail bound exceeded')
            report['frames'].append(dict(index=frame_index, byte_offset=start, byte_end=offset,
                opcode=opcode, final=final, sha256=hashlib.sha256(data[start:offset]).hexdigest()))
        report['source_frames'] += 1
        if opcode in (9, 10):
            if not final or length > 125:
                raise ValueError('invalid control frame')
            report['control_frames'] += 1
            continue
        if opcode == 1:
            if pending is not None:
                raise ValueError('new text message inside incomplete fragment sequence')
            pending, indices = bytearray(), []
            frame_offsets = []
        elif pending is None:
            raise ValueError('continuation without initial text frame')
        pending.extend(payload)
        indices.append(frame_index)
        frame_offsets.append(start)
        if len(pending) > MAX_MESSAGE or len(indices) > MAX_FRAMES:
            raise ValueError('reassembled message bound exceeded')
        if final:
            try:
                packet = pending.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ValueError('invalid complete text UTF8') from exc
            index = report['text_messages']
            accepted = index in selected
            if not _window or accepted:
                report['messages'].append(dict(index=index, frame_indices=indices,
                    sha256=hashlib.sha256(pending).hexdigest(),
                    disposition='SELECTED_RMF_EVENT' if accepted else 'NOT_SELECTED'))
            report['text_messages'] += 1
            report['selected_text_messages' if accepted else 'unselected_text_messages'] += 1
            if accepted:
                record = envelope(packet, capture_time_us, location_unit, yaw_unit)
                if _window:
                    record.update(saved_source_sha256=report['source_sha256'],
                        saved_message_index=index, saved_frame_indices=indices,
                        saved_frame_offsets=frame_offsets)
                encoded = json.dumps(record)
                selected_bytes += len(encoded.encode()) + bool(records)
                if selected_bytes > MAX_INPUT:
                    raise ValueError('selected envelope input bound exceeded')
                records.append(encoded)
            pending = None
    if pending is not None:
        raise ValueError('incomplete final fragmented message')
    if any(index >= report['text_messages'] for index in selected):
        raise ValueError('selected text-message index absent from supplied sequence')
    text = '\n'.join(records)
    if len(text.encode()) > MAX_INPUT:
        raise ValueError('selected envelope input bound exceeded')
    output = existing.convert(text, existing.RMF_SOCKET_FORMAT)
    report['output_sha256'] = hashlib.sha256(output.encode()).hexdigest()
    if _window:
        report.update(selection_scope='EXPLICIT_COMPLETE_MESSAGE_INDICES',
            frame_details_scope='SELECTED_MESSAGES_AND_INTERLEAVED_OR_PRECEDING_CONTROLS',
            omitted_frame_details=report['source_frames']-len(report['frames']),
            unselected_semantics='NOT_DECODED_STRUCTURAL_UTF8_ONLY')
    if len(json.dumps(report).encode()) > MAX_INPUT:
        raise ValueError('frame report bound exceeded')
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
    parser.add_argument('--message-index', required=True, type=int, action='append')
    parser.add_argument('--capture-time-us', required=True, type=int)
    parser.add_argument('--direction', required=True, choices=['server-to-client'])
    parser.add_argument('--extensions', required=True, choices=['none'])
    parser.add_argument('--location-unit', choices=['m'])
    parser.add_argument('--yaw-unit', choices=['rad'])
    parser.add_argument('--saved-window', action='store_true')
    args = parser.parse_args()
    try:
        if args.output.exists() or args.report.exists() or args.output.resolve() == args.report.resolve():
            raise ValueError('output paths must be distinct and new')
        if args.saved_window:
            data = args.input
        else:
            with args.input.open('rb') as source:
                data = source.read(MAX_INPUT + 1)
        output, report = (convert_path if args.saved_window else convert)(data, selected_messages=args.message_index,
            capture_time_us=args.capture_time_us, direction=args.direction, extensions=args.extensions,
            location_unit=args.location_unit, yaw_unit=args.yaw_unit)
        with args.output.open('x', encoding='utf-8', newline='') as target:
            target.write(output)
        with args.report.open('x', encoding='utf-8') as target:
            json.dump(report, target, indent=2)
        return 0
    except (ValueError, OSError) as exc:
        print(f'saved WebSocket conversion rejected: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
