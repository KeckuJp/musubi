"""Small independent regressions for declared CSV and downstream token contracts.

Authored inputs only. Point MUSUBI_POSITION_CSV_ROOT at the converter checkout.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(os.environ.get("MUSUBI_POSITION_CSV_ROOT", Path(__file__).resolve().parents[1]))
SCRIPT = ROOT / "scripts" / "convert_position_csv.py"
PROFILE = {
    "message_type": "RECORDED_POSITION",
    "counter_field": "clock",
    "counter_unit": "us",
    "latitude_field": "north",
    "longitude_field": "east",
    "angle_unit": "deg",
    "altitude_field": "height",
    "altitude_unit": "m",
    "altitude_reference": "msl",
}
HEADER = b"clock,north,east,height,note\n"
SIMPLE = HEADER + b"1,12.5,-45.25,123.4,authored\n"


class PositionBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("position_review_target", SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def replay(self, output, message_type="RECORDED_POSITION"):
        public_root = os.environ.get("MUSUBI_PUBLIC_REPLAY_ROOT")
        if not public_root:
            self.skipTest("public SPI replay requires MUSUBI_PUBLIC_REPLAY_ROOT")
        binary = Path(public_root) / "target" / "debug" / "examples" / "ingest_recorded_jsonl"
        self.assertTrue(binary.is_file(), "build existing public replay example first")
        mapping = {
            "message_type": message_type,
            "latitude_field": "lat_deg",
            "longitude_field": "lon_deg",
            "altitude_field": "alt_m",
            "position_units": "deg-m-msl",
            "domain": "unknown",
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as work:
            work = Path(work)
            source, profile = work / "authored.jsonl", work / "mapping.json"
            source.write_bytes(output)
            profile.write_text(json.dumps(mapping), encoding="utf-8")
            return subprocess.run(
                [str(binary), str(source), str(profile), "authored-source", "1789084800000"],
                capture_output=True, timeout=20,
            )

    def test_unquoted_stray_quotes_are_invalid_csv(self):
        inputs = (
            HEADER + b'1,12.5,-45.25,123.4,unquoted"tail\n',
            b'clock,north,east,height,no"te\n1,12.5,-45.25,123.4,authored\n',
        )
        for raw in inputs:
            with self.subTest(csv=raw), self.assertRaises(ValueError):
                self.module.convert(raw, PROFILE)

    def test_standard_csv_quoted_content_is_preserved(self):
        raw = HEADER + b'1,12.5,-45.25,123.4,"quoted ""word"", then\nnext line"\n'
        record = json.loads(self.module.convert(raw, PROFILE))
        self.assertEqual(record["data"]["source_record"]["note"],
                         'quoted "word", then\nnext line')

    def test_whitespace_only_is_not_a_message_identifier(self):
        with self.assertRaises(ValueError):
            self.module.convert(SIMPLE, {**PROFILE, "message_type": "   "})

    def test_accepted_message_types_remain_usable_in_public_spi(self):
        for message_type in ("RECORDED_POSITION", "é" * 129, "GPS\u200b"):
            with self.subTest(message_type=message_type):
                try:
                    output = self.module.convert(SIMPLE, {**PROFILE, "message_type": message_type})
                except ValueError:
                    self.assertNotEqual(message_type, "RECORDED_POSITION")
                    continue
                result = self.replay(output, message_type)
                self.assertEqual(result.returncode, 0,
                                 "converter accepted a message token refused by the public SPI: "
                                 + result.stderr.decode())

    def test_maximum_generated_record_remains_usable_after_spi_reserialization(self):
        # Python emits 1e-05; serde_json emits 0.00001. Check the public adapter's
        # reserialized payload boundary too, not only the converter's byte count.
        row = b"1,0.00001,0.00001,0.00001,"
        baseline = self.module.convert(HEADER + row + b"\n", PROFILE)
        self.assertEqual(self.replay(baseline).returncode, 0)
        padding = 64 * 1024 - (len(baseline) - 1)
        raw = HEADER + row + b"a" * padding + b"\n"
        self.assertLess(len(raw.splitlines()[1]), 64 * 1024)
        try:
            output = self.module.convert(raw, PROFILE)
        except ValueError:
            # The converter may conservatively refuse a line that cannot survive
            # the downstream representation; it must not emit unusable success.
            return
        self.assertEqual(len(output) - 1, 64 * 1024)
        result = self.replay(output)
        report = json.loads(result.stdout) if result.stdout else {}
        self.assertEqual(result.returncode, 0,
                         "accepted converter output exceeded the public adapter record bound: "
                         + json.dumps(report.get("rejected", [])))


if __name__ == "__main__":
    unittest.main()
