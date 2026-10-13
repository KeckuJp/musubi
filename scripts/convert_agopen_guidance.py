#!/usr/bin/env python3
"""Conditionally bind saved lightbar deviation to a declared saved track; no device I/O."""
import argparse
import csv
import hashlib
import io
import ipaddress
import json
from pathlib import Path
try:
    from scripts import convert_agopen_steering as wire
    from scripts import convert_gpx_recorded as geometry
except ImportError:
    import convert_agopen_steering as wire
    import convert_gpx_recorded as geometry

PIN = '21be26aa58d09b9b5abacc715d0abcf4a021f1db'
BASIS = 'CALLER_DECLARED_NORMAL_GUIDANCE_AND_TRACK_ASSOCIATION_NOT_VERIFIED_ACTIVE_TRACK'
FIELDS = ['record_time_us','source_packet_index','source_frame_hex','capture_sha256','track_sha256',
          'track_index','binding_basis','reported_source_address','reported_source_port',
          'lightbar_code','quantized_deviation_m','deviation_status','quantization_step_m',
          'sign_basis','producer_pin']


def digest(raw): return 'sha256:' + hashlib.sha256(raw).hexdigest()


def convert(capture, track, declaration):
    keys = {'producer_pin','capture_sha256','track_sha256','track_index','track_capture_us',
            'normal_guidance_not_free_drive','source_address','source_port','destination_port'}
    if not isinstance(declaration, dict) or set(declaration) != keys:
        raise ValueError('closed explicit binding declaration required')
    if declaration['producer_pin'] != PIN or declaration['normal_guidance_not_free_drive'] is not True:
        raise ValueError('fixed producer and normal-guidance declaration required')
    if not isinstance(capture, bytes) or not 0 < len(capture) <= wire.MAX_INPUT:
        raise ValueError('bounded saved capture required')
    capture_sha, track_sha = digest(capture), digest(track)
    if declaration['capture_sha256'] != capture_sha or declaration['track_sha256'] != track_sha:
        raise ValueError('source binding digest mismatch')
    for name in ('source_port','destination_port'):
        if type(declaration[name]) is not int or not 1 <= declaration[name] <= 65535:
            raise ValueError('explicit UDP ports required')
    if str(ipaddress.IPv4Address(declaration['source_address'])) != declaration['source_address']:
        raise ValueError('canonical reported IPv4 address required')
    track_csv, track_report = geometry.convert_track(track, declaration['track_capture_us'])
    ordinal = declaration['track_index']
    if type(ordinal) is not int or not 0 <= ordinal < track_report['tracks']:
        raise ValueError('existing zero-based declared track required')
    if any(item['track_index'] == ordinal for item in track_report['unrecognised_modes']):
        raise ValueError('selected track geometry role unqualified')
    output = io.StringIO(newline=''); writer=csv.DictWriter(output,FIELDS,lineterminator='\n')
    writer.writeheader()
    counts={'source_datagrams':0,'selected_frames':0,'unselected_datagrams':0,'other_frames':0}
    for packet in wire.iter_udp_datagrams(capture, declaration['destination_port']):
        counts['source_datagrams'] += 1
        if packet.payload is None or packet.source_address != declaration['source_address'] or packet.source_port != declaration['source_port']:
            counts['unselected_datagrams'] += 1; continue
        frame=bytes(packet.payload)
        if len(frame)<4 or frame[:4] != b'\x80\x81\x7f\xfe':
            counts['other_frames'] += 1; continue
        if len(frame)!=wire.FRAME_BYTES or frame[4]!=8 or wire.checksum(frame)!=frame[-1]:
            raise ValueError('selected frame malformed')
        code=frame[10]
        value='' if code==255 else (code-127)*.02
        state=('UNAVAILABLE' if code==255 else 'SATURATED_LOWER_BOUND' if code==254
               else 'SATURATED_UPPER_BOUND' if code==0 else 'QUANTIZED_REPORTED_DEVIATION')
        writer.writerow(dict(zip(FIELDS,[packet.capture_time_us,packet.index,'hex:'+frame.hex(),
            capture_sha,track_sha,ordinal,BASIS,packet.source_address,packet.source_port,
            code,value,state,.02,'PRODUCER_SIGN_NO_LEFT_RIGHT_CONVENTION',PIN])))
        counts['selected_frames'] += 1
        if output.tell()>wire.MAX_OUTPUT: raise ValueError('output exceeds bound')
    if not counts['selected_frames']: raise ValueError('no qualified source frames')
    report={'schema':'musubi.agopen-declared-guidance/v1','declaration':declaration,'accounting':counts,
            'track_report':track_report,'clock':'PCAP capture / caller track capture; Unknown, not synchronized',
            'basis':BASIS,'retention':'exact capture and TrackLines retained; other frames not interpreted',
            'nonclaim':'not measured exact cross-track error, authenticated identity or active-track proof'}
    return {'deviation.csv':output.getvalue().encode(),'track.csv':track_csv.encode(),
            'source.pcap':capture,'TrackLines.txt':track,
            'report.json':(json.dumps(report,indent=2,allow_nan=False)+'\n').encode()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture',type=Path); parser.add_argument('track',type=Path)
    parser.add_argument('declaration',type=Path); parser.add_argument('output',type=Path)
    args=parser.parse_args()
    try:
        if args.output.exists(): raise ValueError('new output required')
        def read(path,limit):
            with path.open('rb') as f: raw=f.read(limit+1)
            if len(raw)>limit: raise ValueError('input exceeds bound')
            return raw
        files=convert(read(args.capture,wire.MAX_INPUT),read(args.track,geometry.INPUT_LIMIT),
                      json.loads(read(args.declaration,16384)))
        args.output.mkdir()
        for name,raw in files.items():
            with (args.output/name).open('xb') as f: f.write(raw)
    except (ValueError,OSError,UnicodeError,TypeError,ImportError):
        parser.exit(1,'saved guidance conversion refused; verify explicit binding and inputs\n')


if __name__=='__main__': main()
