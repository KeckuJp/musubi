"""Authored saved wire frames, never a live capture or physical protocol fault."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'profiles/declared/tlog-state/selected-sender-profile.toml'


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def frame(version, *, sender=42, seq=0, time=1000000, unknown=False):
    payload = b'future' if unknown else struct.pack('<IBBBBB', 7, 2, 3, 0, 4, 3)
    message = 200 if unknown else 0
    header = (bytes([0xfe, len(payload), seq, sender, 3, message]) if version == 1 else
              bytes([0xfd, len(payload), 0, 0, seq, sender, 3, message, 0, 0]))
    crc = 0xffff
    for byte in header[1:] + payload + bytes([0 if unknown else 50]):
        tmp = byte ^ (crc & 255)
        tmp ^= (tmp << 4) & 255
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 65535
    return struct.pack('>Q', time) + header + payload + struct.pack('<H', crc)


class ProtocolExpectationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        if not cls.reader:
            raise unittest.SkipTest('actual common reader not configured')
        if not Path(cls.reader).is_file():
            raise AssertionError('configured actual reader missing')

    def invoke(self, raw, *, version=1, modify=None, profile=None, extra=(), context_raw=None):
        profile = PROFILE.read_bytes() if profile is None else profile
        expectation = dict(input_sha256=sha(raw), profile_sha256=sha(profile),
                           expected_wire_version=version, session_id='declared-saved-session-A',
                           citation='caller intended wire version, not authentication')
        if modify:
            expectation.update(modify)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'source.tlog').write_bytes(raw)
            (directory / 'profile.toml').write_bytes(profile)
            (directory / 'expected.json').write_text(context_raw or json.dumps(expectation))
            return subprocess.run([self.reader, str(directory / 'profile.toml'),
                                   str(directory / 'source.tlog'), '--recorded-protocol-expectation',
                                   str(directory / 'expected.json'), *extra], capture_output=True)

    def common(self, *args, **kwargs):
        run = self.invoke(*args, **kwargs)
        self.assertEqual(run.returncode, 0, run.stderr.decode())
        return json.loads(run.stdout)

    def test_valid_other_wire_match_unknown_and_sender_accounting(self):
        raw = (frame(1) + frame(2, seq=1) + frame(2, sender=99) +
               frame(2, seq=2, unknown=True))
        report = self.common(raw)
        self.assertEqual(report['input_sha256'], sha(raw))
        self.assertEqual(report['profile_identity']['source_sha256'], sha(PROFILE.read_bytes()))
        self.assertEqual(report['platform_domain'], 'Unknown')
        self.assertEqual(report['domain_source'], 'profile_declared_family')
        self.assertNotIn('UGV', report['domain_basis'])
        self.assertIn('Unknown', report['domain_basis'])
        self.assertEqual(report['main_rows'], 3)
        self.assertEqual(report['selected_sender_records'], 3)
        self.assertEqual(report['unselected_sender_records'], 1)
        rows = report['observations']
        self.assertEqual([o['fields']['MAVLINK.wire_version'] for o in rows], [1, 2, 2])
        self.assertEqual({o['clock_basis'] for o in rows}, {'Unknown'})
        self.assertTrue(all(o['wall_ms'] is None for o in rows))
        self.assertEqual(len({o['fields']['MAVLINK.host_recorded_us'] for o in rows}), 1)
        cues = report['recorded_failure_candidates']
        self.assertEqual([c['protocol_comparison']['disposition'] for c in cues],
                         ['MATCH', 'MISMATCH', 'INSUFFICIENT_UNVERIFIED_CRC'])
        self.assertEqual([c['outcome'] for c in cues], ['unknown', 'ambiguous', 'unknown'])
        self.assertEqual(len(cues[1]['candidates']), 2)
        self.assertTrue(cues[1]['indistinguishable_by'])
        for cue in cues:
            self.assertIsNone(cue['time_window'])
            self.assertEqual(cue['protocol_comparison']['expected_wire_version'], 1)
        for candidate in cues[1]['candidates']:
            self.assertEqual(candidate['logic_confidence'], 'leading_indicator')
            self.assertTrue(candidate['supporting'])
        self.assertIn(b'future', bytes.fromhex(rows[-1]['fields']['MAVLINK.raw_frame_hex']))
        # A different authored sequence/sender and opposite expectation reuse the same path.
        profile = PROFILE.read_bytes().replace(b'system_id = 42', b'system_id = 17').replace(
            b'family = "unknown"', b'family = "ugv"')
        other = self.common(frame(1, sender=17, time=7654321), version=2, profile=profile)
        self.assertEqual(other['recorded_failure_candidates'][0]['outcome'], 'ambiguous')
        self.assertEqual(other['observations'][0]['fields']['MAVLINK.system_id'], 17)
        self.assertEqual(other['platform_domain'], 'Ground')
        self.assertEqual(other['domain_source'], 'profile_declared_family')
        self.assertIn('not an in-band identity', other['domain_basis'])

    def test_broken_input_refuses_not_a_mismatch_candidate(self):
        good = frame(2)
        for broken in (good[:-1], good[:-1] + bytes([good[-1] ^ 1]),
                       good[:10] + bytes([128]) + good[11:], b''):
            with self.subTest(size=len(broken)):
                run = self.invoke(broken)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(run.stdout, b'')
        # Valid records from another sender are not a successful zero-row comparison.
        self.assertNotEqual(self.invoke(frame(2, sender=99)).returncode, 0)

    def test_expectation_binding_types_options_and_profile_refuse(self):
        raw = frame(2)
        for change in ({'input_sha256': 'sha256:' + '0' * 64},
                       {'profile_sha256': 'sha256:' + '0' * 64},
                       {'expected_wire_version': True}, {'expected_wire_version': 3},
                       {'expected_wire_version': '1'}, {'session_id': ''},
                       {'session_id': 'x' * 257}, {'citation': ''}, {'unexpected': 1}):
            with self.subTest(change=change):
                self.assertNotEqual(self.invoke(raw, modify=change).returncode, 0)
        for extra in (('--allow-equal-time',), ('--preserve-nonfinite-as-text',),
                      ('--recorded-failure-candidates',)):
            self.assertNotEqual(self.invoke(raw, extra=extra).returncode, 0)
        duplicate = json.dumps(dict(input_sha256=sha(raw), profile_sha256=sha(PROFILE.read_bytes()),
                                    expected_wire_version=1, session_id='a', citation='declared'))
        duplicate = duplicate[:-1] + ',"session_id":"b"}'
        rejected = self.invoke(raw, context_raw=duplicate)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b'duplicate field', rejected.stderr)
        for profile in (PROFILE.read_bytes().replace(b'"unknown"', b'"host_received"'),
                        PROFILE.read_bytes().replace(b'system_id = 42', b'system_id = 0')):
            self.assertNotEqual(self.invoke(raw, profile=profile).returncode, 0)


if __name__ == '__main__':
    unittest.main()
