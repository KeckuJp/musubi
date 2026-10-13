"""Declared geometry closure; authored archives, not physical trajectories."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from scripts.convert_gpx_recorded import convert_kml, convert_kmz

PROFILE = Path(__file__).resolve().parents[1] / 'profiles/declared/gpx-points/kmz-profile.toml'
PREFIX = '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">'

def polygon():
    return '<Polygon id="area"><altitudeMode>absolute</altitudeMode><outerBoundaryIs><LinearRing><coordinates>0,0,5 2,0,5 2,2,5 0,0,5</coordinates></LinearRing></outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>.2,.2,5 .4,.2,5 .4,.4,5 .2,.2,5</coordinates></LinearRing></innerBoundaryIs></Polygon>'

class KmlFeatureGeometryTests(unittest.TestCase):
    def common(self, output):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader, 'required postbuild reader')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'data.csv'; path.write_text(output)
            run = subprocess.run([reader, str(PROFILE), str(path), '--allow-equal-time'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)['observations']

    def test_polygon_feature_interval_and_separate_multitrack(self):
        first = PREFIX+'<Document><TimeSpan><begin>2026-01</begin></TimeSpan><Placemark id="plot"><name>区画,一</name>'+polygon()+'</Placemark></Document></kml>'
        second = PREFIX+'<Placemark id="path"><TimeSpan><begin>2026-01-01T00:00:00Z</begin><end>2026-01-02T00:00:00Z</end></TimeSpan><gx:MultiTrack id="trip"><altitudeMode>absolute</altitudeMode><gx:interpolate>1</gx:interpolate><gx:Track><when>2026-01-01T00:00:01Z</when><gx:coord>1 2 3</gx:coord></gx:Track><gx:Track><when>2026-01-01T00:00:02Z</when><gx:coord></gx:coord></gx:Track></gx:MultiTrack></Placemark></kml>'
        for raw in (first.encode(), second.encode()):
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as z: z.writestr('doc.kml', raw)
            for converter, data in ((convert_kml,raw), (convert_kmz,archive.getvalue())):
                output, report = converter(data, 50, feature_geometry=True)
                rows = list(csv.DictReader(io.StringIO(output)))
                observations = self.common(output)
                self.assertEqual(len(rows), len(observations))
                self.assertTrue(all(o['clock_basis']=='Unknown' for o in observations))
                if raw == first.encode():
                    self.assertEqual(len(rows),8)
                    self.assertEqual([r['ring_role'] for r in rows], ['OUTER']*4+['INNER']*4)
                    self.assertEqual(rows[0]['feature_begin_precision'],'MONTH')
                    self.assertEqual(rows[0]['feature_begin_unix_us'],'')
                    self.assertEqual(rows[0]['feature_end_precision'],'UNBOUNDED')
                    self.assertEqual(bytes.fromhex(rows[0]['feature_name_hex'][4:]).decode(),'区画,一')
                    self.assertEqual(observations[-1]['fields']['declared_altitude_m'],5)
                else:
                    self.assertEqual(len(rows),2)
                    self.assertEqual([r['multitrack_part_index'] for r in rows],['0','1'])
                    self.assertEqual(rows[0]['multitrack_interpolate_reported'],'true')
                    self.assertEqual(rows[1]['position_status'],'MISSING_COORDINATE_NOT_INTERPOLATED')
                    self.assertNotEqual(rows[0]['feature_begin_unix_us'],'')
                    self.assertEqual(observations[0]['fields']['declared_altitude_m'],3)

    def test_partial_timestamp_cli_and_missing_fields(self):
        raw=(PREFIX+'<Document><TimeStamp><when>2026</when></TimeStamp><Placemark><Point><coordinates>3,4</coordinates></Point></Placemark></Document></kml>').encode()
        output, report=convert_kml(raw,50,feature_geometry=True)
        row=next(csv.DictReader(io.StringIO(output)))
        self.assertEqual((row['feature_time_precision'],row['feature_time_year'],row['feature_time_month'],row['feature_time_unix_us']),('YEAR','2026','',''))
        self.assertEqual(self.common(output)[0]['fields']['feature_time_year'],2026)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); source=base/'saved.kml'; source.write_bytes(raw)
            result=subprocess.run([sys.executable,'-m','scripts.convert_gpx_recorded',str(source),str(base/'out'),
                '--capture-time-us','50','--source-format','kml22','--kml-feature-geometry'],
                cwd=PROFILE.parents[3],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual((base/'out'/'observations.csv').read_text(),output)

    def test_refuse_broken_selected_geometry(self):
        good = PREFIX+'<Placemark>'+polygon()+'</Placemark></kml>'
        bads = [good.replace('0,0,5</coordinates>','1,1,5</coordinates>',1),
                good.replace('<outerBoundaryIs>','<innerBoundaryIs>').replace('</outerBoundaryIs>','</innerBoundaryIs>'),
                good.replace('<Placemark>','<Placemark><TimeSpan><begin>2026-02-30</begin></TimeSpan>'),
                good.replace('<Placemark>','<Placemark><TimeSpan><begin>2026-02</begin><end>2026-01</end></TimeSpan>')]
        multi=PREFIX+'<Placemark><gx:MultiTrack><altitudeMode>absolute</altitudeMode><gx:Track><altitudeMode>relativeToGround</altitudeMode><when>2026-01-01T00:00:00Z</when><gx:coord>1 2 3</gx:coord></gx:Track></gx:MultiTrack></Placemark></kml>'
        bads.extend([multi, multi.replace('<altitudeMode>relativeToGround</altitudeMode>','<gx:angles>1 2</gx:angles>'),
                     good.replace('<Placemark>','<Placemark><TimeSpan><begin/></TimeSpan>')])
        for source in bads:
            with self.subTest(source=source), self.assertRaises(ValueError): convert_kml(source.encode(),50,feature_geometry=True)

if __name__=='__main__': unittest.main()
