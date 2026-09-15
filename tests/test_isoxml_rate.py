"""Authored layout/meaning checks; no physical application claim."""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from scripts.convert_isoxml_rate import convert

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = b'<TIM A="" D="4"><PTN A="" B="" D=""/><DLV A="0002" B="" C="DET-1"/><DLV A="0001" B="" C="DET-1"/><DLV A="0002" B="" C="DET-2"/></TIM>'


def row(changes, *, position=True, ms=1000):
    data = struct.pack("<IH", ms, 10000)
    if position:
        data += struct.pack("<iiB", 0, 0, 0)  # Zero GNSS does not remove application records.
    data += bytes([len(changes)])
    return data + b"".join(struct.pack("<Bi", index, value) for index, value in changes)


class IsoxmlRateTests(unittest.TestCase):





    def test_layout_reuse_updates_not_forward_fill_and_setpoint_is_not_actual(self):
        for position in (True, False):
            template = TEMPLATE if position else TEMPLATE.replace(b'<PTN A="" B="" D=""/>', b"")
            data = row([(0, 10000), (1, 90000), (2, 20000), (0, 30000)], position=position)
            data += row([], position=position, ms=2000)
            output, report = convert(template, data)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([float(r["actual_volume_rate_l_ha"]) for r in rows], [1., 2., 3.])
            self.assertEqual([r["source_column"] for r in rows], ["0", "2", "0"])
            self.assertEqual([r["source_update"] for r in rows], ["0", "2", "3"])
            self.assertEqual(report["source_records"], 2)
            self.assertEqual(report["source_updates"], 4)
            self.assertEqual(report["decoded_updates"] + report["unsupported_updates"], 4)
            self.assertEqual(report["records_without_supported_update"], 1)
            self.assertEqual(b"".join(bytes.fromhex(r) for r in report["source_records_hex"]), data)
            self.assertEqual(bytes.fromhex(report["template_hex"]), template)

    def test_important_failures(self):
        data = row([(0, 10000)])
        for template, binary in [(TEMPLATE, data[:-1]), (TEMPLATE, row([(3, 1)])),
            (TEMPLATE, row([(0, -1)])), (TEMPLATE, row([(1, 1)])),
            (TEMPLATE, row([(0, 1)], ms=86400000)), (TEMPLATE, data + row([(0, 1)], ms=0)),
            (TEMPLATE.replace(b'D="4"', b'D="1"'), data),
            (TEMPLATE.replace(b'B="" C="DET-1"', b'B="3" C="DET-1"'), data),
            (b'<!DOCTYPE x [<!ENTITY e "x">]>' + TEMPLATE, data)]:
            with self.subTest(template=template[:20]), self.assertRaises(ValueError):
                convert(template, binary)



if __name__ == "__main__":
    unittest.main()
