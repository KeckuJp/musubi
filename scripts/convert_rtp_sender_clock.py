"""Caller-bound saved RTP -> sender's reported clock; never UTC or latency."""
import argparse
import csv
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path

from scripts import convert_rtp_records as rtp
from scripts import convert_rtcp_sender_reports as rtcp

BASIS = 'CALLER_DECLARED_SAME_SENDER_SESSION_NOT_AUTHENTICATED_NO_UTC_ERA_OR_LATENCY'
KEYS = {'rtp_sha256', 'rtcp_sha256', 'rtp_tuple', 'ssrc', 'payload_type',
        'clock_rate_hz', 'rtcp_port', 'anchor_capture_index', 'anchor_packet_index',
        'max_abs_delta_ticks', 'relation'}


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def convert(raw, control, declaration):
    if not isinstance(declaration, dict) or set(declaration) != KEYS:
        raise ValueError('exact closed sender-clock declaration required')
    if declaration['relation'] != BASIS:
        raise ValueError('explicit sender/session declaration required')
    if declaration['rtp_sha256'] != digest(raw) or declaration['rtcp_sha256'] != digest(control):
        raise ValueError('declared saved capture digest differs')
    limits = {'ssrc': (0, 2**32-1), 'payload_type': (0, 127),
              'clock_rate_hz': (1, 1_000_000_000), 'rtcp_port': (1, 65535),
              'anchor_capture_index': (0, 2**31-1), 'anchor_packet_index': (0, 63),
              'max_abs_delta_ticks': (1, 2**31-1)}
    for key, (low, high) in limits.items():
        value = declaration[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError('invalid finite sender-clock declaration')
    peer = declaration['rtp_tuple']
    if (not isinstance(peer, list) or len(peer) != 4
            or any(type(peer[i]) is not int or not 1 <= peer[i] <= 65535 for i in (1, 3))
            or any(not isinstance(peer[i], str) or len(peer[i]) > 15 for i in (0, 2))):
        raise ValueError('explicit IPv4 source/destination tuple required')
    anchor_csv, control_report = rtcp.convert(control, declaration['rtcp_port'])
    anchors = [row for row in csv.DictReader(io.StringIO(anchor_csv))
               if row['rtcp_record_kind'] == 'SENDER_REPORT'
               and int(row['pcap_packet_index']) == declaration['anchor_capture_index']
               and int(row['rtcp_packet_index_in_datagram']) == declaration['anchor_packet_index']]
    if len(anchors) != 1:
        raise ValueError('exact unique sender report required')
    anchor = anchors[0]
    if int(anchor['rtcp_ssrc']) != declaration['ssrc']:
        raise ValueError('declared RTP source differs from sender report')
    seconds, fraction = int(anchor['sr_ntp_seconds_reported']), int(anchor['sr_ntp_fraction_reported'])
    if seconds == fraction == 0:
        raise ValueError('sender did not report a clock anchor')
    media = int(anchor['sr_rtp_timestamp_reported'])
    source_csv, source_report = rtp.convert_pcap_rtp(
        raw, peer[3], clock_rate=declaration['clock_rate_hz'],
        payload_type=declaration['payload_type'], ssrc=declaration['ssrc'])
    output = io.StringIO(newline='')
    writer, selected, excluded = None, 0, 0
    for row in csv.DictReader(io.StringIO(source_csv)):
        actual = [row['rtp_source_address'], int(row['rtp_source_port']),
                  row['rtp_destination_address'], int(row['rtp_destination_port'])]
        if actual != peer:
            excluded += 1
            continue
        if (row['rtp_sequence_epoch'] != '0' or row['rtp_timestamp_state'] == 'discontinuity_unknown'
                or row['rtp_order_state'] in ('discontinuity_unknown', 'restart')):
            raise ValueError('source discontinuity cannot reuse one clock anchor')
        unsigned = (int(row['rtp_timestamp']) - media) % 2**32
        delta = unsigned if unsigned < 2**31 else unsigned - 2**32
        if unsigned == 2**31 or abs(delta) > declaration['max_abs_delta_ticks']:
            raise ValueError('anchor distance exceeds explicitly declared unambiguous interval')
        mapped = Fraction(seconds * 2**32 + fraction, 2**32) + Fraction(delta, declaration['clock_rate_hz'])
        if not 0 <= mapped < 2**32:
            raise ValueError('sender seconds cross unresolved NTP era boundary')
        row.update(sender_clock_basis=BASIS, sender_anchor_capture_sha256=digest(control),
                   sender_anchor_packet_sha256=anchor['rtcp_packet_sha256'],
                   sender_anchor_capture_index=declaration['anchor_capture_index'],
                   sender_anchor_packet_index=declaration['anchor_packet_index'],
                   sender_anchor_ntp_seconds=seconds, sender_anchor_ntp_fraction=fraction,
                   sender_anchor_rtp_timestamp=media, sender_delta_ticks=delta,
                   sender_seconds_exact='ratio:' + str(mapped.numerator) + '/' + str(mapped.denominator),
                   sender_microseconds_floor=(mapped.numerator * 1_000_000) // mapped.denominator,
                   sender_precision_basis='EXACT_RATIONAL_AND_FLOOR_US_NOT_MEASUREMENT_ACCURACY')
        if writer is None:
            writer = csv.DictWriter(output, list(row), lineterminator='\n')
            writer.writeheader()
        writer.writerow(row)
        selected += 1
        if output.tell() > rtp.OUTPUT_LIMIT:
            raise ValueError('sender-clock CSV exceeds existing bound')
    if not selected:
        raise ValueError('declared source tuple absent')
    return output.getvalue(), dict(selected_packets=selected, other_tuple_packets=excluded,
        rtp=source_report, rtcp=control_report, declaration=declaration,
        time_basis=BASIS, precision='RATIONAL_EXACT_FLOOR_US',
        nonclaims=['UTC', 'NTP era', 'clock authentication', 'latency', 'physical synchronization'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rtp_capture', type=Path)
    parser.add_argument('rtcp_capture', type=Path)
    parser.add_argument('declaration', type=Path)
    parser.add_argument('new_output_directory', type=Path)
    args = parser.parse_args()
    try:
        if (args.rtp_capture.stat().st_size > rtcp.LIMIT or args.rtcp_capture.stat().st_size > rtcp.LIMIT
                or args.declaration.stat().st_size > 8192):
            raise ValueError('input bound exceeded')
        raw, control = args.rtp_capture.read_bytes(), args.rtcp_capture.read_bytes()
        declared = args.declaration.read_bytes()
        converted, report = convert(raw, control, json.loads(declared))
        args.new_output_directory.mkdir()
        for name, data in [('rtp.pcap', raw), ('rtcp.pcap', control), ('declaration.json', declared)]:
            (args.new_output_directory / name).write_bytes(data)
        (args.new_output_directory / 'observations.csv').write_text(converted, encoding='utf-8')
        (args.new_output_directory / 'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    except (ValueError, OSError, ImportError):
        parser.exit(2, 'saved sender-clock conversion failed; check captures and declaration\n')


if __name__ == '__main__':
    main()
