"""Declared GPX1.1 point fixtures, not new field recordings."""
import csv
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from xml.etree.ElementTree import ParseError
from scripts.convert_gpx_recorded import convert, convert_boundary, deere_points_to_gpx, DEERE_WGS84

ROOT = Path(__file__).resolve().parents[1]


def document(body):
    return ('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:x="urn:fixture" version="1.1" creator="authored fixture">' + body + '</gpx>').encode()


class GpxRecordedTests(unittest.TestCase):







    def test_important_wrong_layout_quantity_time_and_xml_rejections(self):
        for body in ('', '<rte/>', '<wpt lat="90.000000000000000001" lon="0"/>',
                     '<wpt lat="0" lon="180"/>', '<wpt lat="NaN" lon="0"/>',
                     '<wpt lat="0"/>', '<wpt lat="0" lon="0"><ele>Inf</ele></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-02-30T00:00:00Z</time></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-01-01T00:00:00</time></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-01-01T00:00:00.1234567Z</time></wpt>',
                     '<wpt lat="0" lon="0"><time>2026-01-01T00:00:00+14:01</time></wpt>',
                     '<wpt lat="0" lon="0"><ele>1</ele><ele>2</ele></wpt>',
                     '<trk><trkpt lat="0" lon="0"/></trk>'):
            with self.assertRaises(ValueError): convert(document(body), 1)
        good = document('<wpt lat="-90" lon="-180"/>')
        for source in (good.replace(b'version="1.1"', b'version="1.0"'),
                       b'<!DOCTYPE gpx [<!ENTITY x "bad">]>' + good, good.replace(b'/GPX/1/1', b'/GPX/1/0')):
            with self.assertRaises(ValueError): convert(source, 1)
        for capture in (None, True, -1, 2**63):
            with self.assertRaises(ValueError): convert(good, capture)
        with self.assertRaises(ParseError): convert(good[:-1], 1)


if __name__ == "__main__":
    unittest.main()
