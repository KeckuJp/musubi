"""Saved LAS chunks reach existing observation reports without dropping points.

Authored LAS 1.4 bytes only, no survey input. No optional/skipped E2E path.
"""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


def authored_las(count):
    # LAS 1.4 header offsets from its public binary layout, PDRF 0 (20 bytes).
    # Opaque WKT remains producer text; this test asserts no interpreted CRS.
    wkt = b'LOCAL_CS["authored"]\0'
    vlr = struct.pack('<H16sHH32s', 0, b'LASF_Projection', 2112, len(wkt), b'authored') + wkt
    header = bytearray(375)
    header[:4] = b'LASF'
    struct.pack_into('<H', header, 6, 16)
    header[24:26] = bytes([1, 4])
    struct.pack_into('<HII', header, 94, 375, 375 + len(vlr), 1)
    struct.pack_into('<BHI', header, 104, 0, 20, count)
    struct.pack_into('<3d', header, 131, .01, .01, .01)
    struct.pack_into('<Q', header, 247, count)
    points = b''.join(struct.pack('<iiiHBBbBH', 100 + i, -200 - i, 300 + i, 12, 9, 2, 0, 0, 1)
                      for i in range(count))
    return bytes(header) + vlr + points


class PublicPointChunks(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ.get('MUSUBI_PUBLIC_STEPS_ROOT', Path(__file__).resolve().parents[1]))
        self.driver = self.root / 'scripts/ingest_point_cloud_chunks.py'
        self.assertTrue(self.driver.is_file(), 'public chunk ingestion driver missing')
        self.ingest = os.environ.get('MUSUBI_RECORDED_OBSERVATIONS_BIN')
        self.viewer = os.environ.get('MUSUBI_REFERENCE_VIEWER_BIN')
        self.assertTrue(self.ingest and self.viewer, 'real ingest and viewer binaries required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.run_count = 0

    def export(self, count=3):
        source = self.work / 'authored.las'
        source.write_bytes(authored_las(count))
        output = self.work / 'chunks'
        result = subprocess.run([sys.executable, str(self.root / 'scripts/export_point_cloud_chunks.py'),
                                 str(source), str(output), '--capture-time-us', '1700000000000000',
                                 '--points-per-chunk', '2'], cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return output

    @staticmethod
    def load(directory):
        return json.loads((directory / 'manifest.json').read_text())

    @staticmethod
    def save(directory, manifest):
        (directory / 'manifest.json').write_text(json.dumps(manifest))

    def run_driver(self, directory, *, accepted=True, ingest=None, viewer=None):
        self.run_count += 1
        output = self.work / ('observations-%d' % self.run_count)
        run = subprocess.run([sys.executable, str(self.driver), str(directory),
            str(self.root / 'profiles/recorded/saved-point-cloud/profile.json'),
            str(self.root / 'profiles/meanings/saved-point-cloud/profile.json'), str(output),
            '--ingest-bin', ingest or self.ingest, '--viewer-bin', viewer or self.viewer],
            cwd=self.root, capture_output=True)
        if accepted:
            self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
            return output, self.load(output)
        self.assertNotEqual(run.returncode, 0, 'invalid chunks or failed subprocess reported success')
        self.assertFalse((output / 'manifest.json').exists(), 'false completion after refusal')

    def test_every_point_reaches_verified_report_once_in_order(self):
        chunks = self.export()
        output, complete = self.run_driver(chunks)
        self.assertEqual(complete['schema'], 'point-cloud-observation-chunks/v1')
        self.assertEqual(complete['points_carried'], 3)
        self.assertEqual(complete['chunk_count'], 2)
        self.assertEqual([entry['point_count'] for entry in complete['chunks']], [2, 1])
        indexes = []
        for entry in complete['chunks']:
            name = entry['report']
            self.assertEqual(Path(name).name, name)
            raw = (output / name).read_bytes()
            self.assertEqual(entry['sha256'].removeprefix('sha256:'), hashlib.sha256(raw).hexdigest())
            report = json.loads(raw)
            self.assertEqual(report['schema'], 'recorded-observation-report/v1')
            for observation in report['observations']:
                fields = {f['name']: f['value'] for f in observation['fields']}
                if fields['las_record_kind'] == 'POINT_RECORD':
                    indexes.append(fields['point_index'])
                self.assertEqual(observation['clock_basis'], 'unknown')
                self.assertIsNone(observation['wall_ms'])
        self.assertEqual(indexes, [0, 1, 2])

    def test_zero_points_keeps_one_file_declaration_report(self):
        output, complete = self.run_driver(self.export(0))
        self.assertEqual((complete['points_carried'], complete['chunk_count']), (0, 1))
        self.assertEqual(complete['chunks'][0]['point_count'], 0)
        report = json.loads((output / complete['chunks'][0]['report']).read_text())
        self.assertEqual(len(report['observations']), 1)
        fields = {f['name']: f['value'] for f in report['observations'][0]['fields']}
        self.assertEqual(fields['las_record_kind'], 'FILE_DECLARATION')

    def test_tampered_and_missing_csv_never_complete(self):
        chunks = self.export()
        csv = chunks / 'chunk-000001/observations.csv'
        original = csv.read_bytes()
        csv.write_bytes(original.replace(b'POINT_RECORD', b'FALSE_RECORD'))
        self.run_driver(chunks, accepted=False)
        csv.unlink()
        self.run_driver(chunks, accepted=False)

    def test_manifest_discontinuous_ranges_and_totals_refused(self):
        chunks = self.export()
        original = self.load(chunks)
        changes = [('points_carried', 2), ('points_to_carry', 4), ('chunk_count', 4097)]
        for key, value in changes:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(original))
                changed[key] = value
                self.save(chunks, changed)
                self.run_driver(chunks, accepted=False)
        changed = json.loads(json.dumps(original))
        changed['chunks'][1]['first_point_index'] = 3
        self.save(chunks, changed)
        self.run_driver(chunks, accepted=False)

    def test_path_traversal_and_symlink_refused(self):
        chunks = self.export()
        original = self.load(chunks)
        changed = json.loads(json.dumps(original))
        changed['chunks'][0]['chunk'] = '../chunks/chunk-000000'
        self.save(chunks, changed)
        self.run_driver(chunks, accepted=False)
        self.save(chunks, original)
        csv = chunks / 'chunk-000000/observations.csv'
        escaped = self.work / 'outside.csv'
        csv.rename(escaped)
        csv.symlink_to(escaped)
        self.run_driver(chunks, accepted=False)

    def test_manifest_and_csv_size_bounds_refused(self):
        chunks = self.export()
        original = self.load(chunks)
        (chunks / 'manifest.json').write_bytes(b' ' * (4 * 1024 * 1024 + 1))
        self.run_driver(chunks, accepted=False)
        self.save(chunks, original)
        csv = chunks / 'chunk-000000/observations.csv'
        with csv.open('wb') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        original['chunks'][0]['csv_bytes'] = csv.stat().st_size
        original['chunks'][0]['csv_sha256'] = 'sha256:' + hashlib.sha256(csv.read_bytes()).hexdigest()
        original['csv_bytes'] = sum(entry['csv_bytes'] for entry in original['chunks'])
        self.save(chunks, original)
        self.run_driver(chunks, accepted=False)

    def test_ingest_or_viewer_failure_never_writes_completion(self):
        chunks = self.export()
        self.run_driver(chunks, accepted=False, ingest='/usr/bin/false')
        self.run_driver(chunks, accepted=False, viewer='/usr/bin/false')


if __name__ == '__main__':
    unittest.main()
