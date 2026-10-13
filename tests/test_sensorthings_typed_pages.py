"""Authored caller-saved pages; no HTTP/server or real sensor claims."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts import convert_opcua_recorded as module

ROOT = Path(__file__).resolve().parents[1]
PROPERTY = "urn:example:selected"
TIME = "2026-01-01T00:00:00Z"


def observation(kind, value, identity=1):
    name = {"count": "OM_CountObservation", "truth": "OM_TruthObservation", "celsius": "OM_Measurement"}[kind]
    unit = dict.fromkeys(("name", "symbol", "definition")) if kind != "celsius" else {
        "name": "degree Celsius", "symbol": "°C", "definition": "http://unitsofmeasure.org/ucum.html#para-30"}
    return {"@iot.id": identity, "phenomenonTime": TIME, "resultTime": None, "result": value,
            "Datastream": {"@iot.id": 7, "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/" + name,
                "unitOfMeasurement": unit, "ObservedProperty": {"definition": PROPERTY}}, "future": "retained"}


def manifest(observations):
    return "\n".join(json.dumps({"declared_page_url": "urn:page:" + str(i),
        "page_body": json.dumps({"value": values, **({"@iot.nextLink": "urn:page:" + str(i + 1)}
                                                   if i < len(observations)-1 else {})})})
        for i, values in enumerate(observations))


class TypedPagesTests(unittest.TestCase):
    def test_explicit_cli_selection_and_wrong_source_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source, output, report = [folder / x for x in ("pages.jsonl", "out.csv", "report.json")]
            source.write_text(manifest([[observation("count", 3)]]))
            args = [os.sys.executable, str(ROOT / "scripts/convert_opcua_recorded.py"), str(source),
                    str(output), str(report), "--source-format", module.SENSORTHINGS_PAGES_FORMAT,
                    "--observed-property", PROPERTY, "--capture-time-us", "1", "--result-kind", "count"]
            subprocess.run(args, check=True, capture_output=True)
            self.assertEqual(self.common(output.read_text())["main_rows"], 1)
            self.assertEqual(json.loads(report.read_text())["declared_result_kind"], "count")
            args[args.index(module.SENSORTHINGS_PAGES_FORMAT)] = "opcua"
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)

    def common(self, text):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        self.assertTrue(reader and Path(reader).is_file(), "postbuild reader required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.csv"
            path.write_text(text)
            result = subprocess.run([reader, str(ROOT / "profiles/declared/sensorthings-dataarray/profile.toml"),
                                     str(path), "--allow-empty-rows", "--allow-equal-time"], check=True, capture_output=True)
        return json.loads(result.stdout)

    def test_count_truth_pages_reuse_semantics_null_empty_and_common(self):
        for kind, first, second in (("count", -5, 2**53 - 1), ("truth", False, True)):
            source = manifest([[observation(kind, first)], [], [observation(kind, second, 2), observation(kind, None, 3)]])
            text, report = module.sensorthings_saved_pages(source, PROPERTY, 10, result_kind=kind)
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual([r["saved_page_index"] for r in rows], ["0", "2", "2"])
            self.assertEqual(report["empty_pages"], 1)
            self.assertEqual(report["withheld_values"], 1)
            common = self.common(text)
            self.assertEqual(common["main_rows"], 3)
            fields = common["observations"][0]["fields"]
            self.assertEqual(fields[kind + "_reported"], first if kind == "count" else "false")
            self.assertIn("future", bytes.fromhex(fields["source_record_hex"][4:]).decode())
            empty, report = module.sensorthings_saved_pages(manifest([[]]), PROPERTY, 1, result_kind=kind, empty_header=True)
            self.assertEqual(self.common(empty)["main_rows"], 0)
            self.assertIn("NOT_ON_EMPTY", report["scalar_metadata_basis"])

    def test_default_parity_and_invalid_metadata_values_or_chain(self):
        temperature = manifest([[observation("celsius", 20)]])
        self.assertEqual(module.sensorthings_saved_pages(temperature, PROPERTY, 1),
                         module.sensorthings_saved_pages(temperature, PROPERTY, 1, result_kind="celsius"))
        self.assertEqual(module.sensorthings_saved_pages(manifest([[]]), PROPERTY, 1)[0], "")
        for kind, value in (("count", True), ("count", 2**53), ("truth", 1)):
            with self.assertRaises(ValueError):
                module.sensorthings_saved_pages(manifest([[observation(kind, value)]]), PROPERTY, 1, result_kind=kind)
        wrong = observation("count", 1)
        wrong["Datastream"]["unitOfMeasurement"]["symbol"] = "m"
        with self.assertRaises(ValueError):
            module.sensorthings_saved_pages(manifest([[wrong]]), PROPERTY, 1, result_kind="count")
        with self.assertRaises(ValueError):
            module.sensorthings_saved_pages(manifest([[]]), PROPERTY, 1, result_kind="unknown")
