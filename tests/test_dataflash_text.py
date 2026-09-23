"""Constructed text captures; code qualification, not new flight evidence."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_dataflash_text import convert

ROOT = Path(__file__).resolve().parents[1]


def sample(value=7, default=False):
    return ("FMT, 128, 89, FMT, BBnNZ, Type,Length,Name,Format,Columns\n"
        "FMT, 129, 75, MSG, QZ, TimeUS,Message\n"
        + ("FMT, 32, 35, PARM, QNff, TimeUS,Name,Value,Default\n" if default else
           "FMT, 32, 31, PARM, QNf, TimeUS,Name,Value\n")
        + "MSG, 100, ArduPlane synthetic\n"
        + f"PARM, 100, LOG_BITMASK, {value}" + (", 1" if default else "") + "\n").encode()


class TextCapture(unittest.TestCase):
    def test_fmt_unambiguous_trailing_text_reuses_common_reader(self):
        for code, width, value in (("n", 4, "a, b"), ("N", 16, "alpha, beta"), ("Z", 64, "温度, 42")):
            for delimiter in (", ", ","):
                prefix = sample().decode().replace(", ", delimiter)
                source = (prefix + delimiter.join(("FMT", "150", str(11 + width), "TEXT", "Q" + code,
                                                   "TimeUS,Text")) + "\n"
                          + delimiter.join(("TEXT", "200", value)) + "\n").encode()
                records = [json.loads(line) for line in convert(source).splitlines()]
                self.assertEqual(records[-1]["data"]["Text"], value)
                self.assertEqual(records[-1]["meta"]["reassembled_trailing_text"], "Text")
                self.assertEqual(bytes.fromhex(records[-1]["source_line_hex"][4:]), source.splitlines()[-1])
                if os.environ.get("MUSUBI_MAVLOG_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "out.jsonl"
                        path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
                        common = json.loads(subprocess.run([os.environ["MUSUBI_MAVLOG_READER"],
                            str(ROOT / "profiles/declared/arduplane-jsonl/profile.toml"),
                            str(path)], check=True, capture_output=True).stdout)
                        self.assertEqual(common["decoded_observations"], 3)
                        self.assertEqual(common["untimed_records"], 4)
                        self.assertEqual(common["observations"][-1]["fields"]["TEXT.Text"], value)
                        self.assertEqual(common["observations"][-1]["t_boot_us"], 200)
        for source in (sample().replace(b"LOG_BITMASK", b"LOG, BITMASK"),
                       sample().replace(b"ArduPlane synthetic", b"x" * 64 + b", x")):
            with self.assertRaises(ValueError):
                convert(source)

    def test_nonfinite_retention_and_practical_record_count(self):
        from unittest.mock import patch
        for count, token in ((100000, "NaN"), (2, "-Infinity")):
            source = sample().splitlines(keepends=True)[:4]
            source += [f"PARM, {index + 100}, LOG_BITMASK, {token if index == 0 else index}\n".encode()
                       for index in range(count)]
            raw = b"".join(source)
            if count == 100000:
                self.assertGreater(len(raw), 2 * 1024 * 1024)
            output = convert(raw, preserve_nonfinite=True)
            records = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(len(records), count + 4)
            self.assertEqual(records[4]["data"]["Value"], token)
            self.assertEqual(records[4]["meta"]["nonfinite_text_fields"], {"Value": "f"})
            self.assertEqual(bytes.fromhex(records[4]["source_line_hex"][4:]), raw.splitlines()[4])
            self.assertEqual(records[-1]["data"]["Value"], count - 1)
            with patch("scripts.convert_dataflash_text.MAX_INPUT", len(raw) - 1), self.assertRaises(ValueError):
                convert(raw, preserve_nonfinite=True)
            if count == 2:
                with patch("scripts.convert_dataflash_text.MAX_OUTPUT", 100), self.assertRaises(ValueError):
                    convert(raw, preserve_nonfinite=True)
            if os.environ.get("MUSUBI_MAVLOG_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.jsonl"; path.write_text(output)
                    with (Path(directory) / "common.json").open("wb") as destination:
                        subprocess.run([os.environ["MUSUBI_MAVLOG_READER"],
                            str(ROOT / "profiles/declared/arduplane-jsonl/profile.toml"),
                            str(path)], check=True, stdout=destination)
                    with (Path(directory) / "common.json").open() as stream:
                        common = json.load(stream)
                    self.assertEqual(common["decoded_observations"], count + 1)
                    self.assertEqual(common["untimed_records"], 3)
                    self.assertEqual(common["observations"][1]["fields"]["PARM.Value"], token)
                    self.assertEqual(common["observations"][-1]["fields"]["PARM.Value"], count - 1)
                    del common

    def test_two_schemas_to_existing_common_reader(self):
        for value, default in [(7, False), (3, True)]:
            source = sample(value, default)
            output = convert(source)
            records = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(len(records), 5)
            self.assertEqual(records[-1]["data"]["Value"], value)
            self.assertEqual("Default" in records[-1]["data"], default)
            self.assertEqual(bytes.fromhex(records[-1]["source_line_hex"][4:]), source.splitlines()[-1])
            if os.environ.get("MUSUBI_MAVLOG_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.jsonl"; path.write_text(output)
                    report = json.loads(subprocess.run([os.environ["MUSUBI_MAVLOG_READER"],
                        str(ROOT / "profiles/declared/arduplane-jsonl/profile.toml"),
                        str(path)], check=True, capture_output=True).stdout)
                    self.assertEqual(report["decoded_observations"], 2)
                    self.assertEqual(report["untimed_records"], 3)
                    self.assertEqual(report["observations"][-1]["fields"]["PARM.Value"], value)
                    self.assertEqual(report["observations"][-1]["t_boot_us"], 100)

    def test_no_ignored_prefix_unknown_short_duplicate_or_missing_newline(self):
        for data in [b"garbage\n" + sample(), sample()[:-1], sample() + b"UNKNOWN, 1\n",
                     sample().replace(b"PARM, 100, LOG_BITMASK, 7", b"PARM, 100"),
                     sample() + sample(), sample() + b"\n",
                     sample().replace(b"LOG_BITMASK, 7", b"LOG_BITMASK, NaN"),
                     sample().replace(b"TimeUS,Name,Value", b"TimeUS,Name,mavpackettype"),
                     sample().replace(b"32, 31, PARM", b"32, 30, PARM")]:
            with self.assertRaises((ValueError, TypeError)):
                convert(data)
