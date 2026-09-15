"""Independent synthetic acceptance checks for the recorded observation CLI.

Build the public ingest_recorded_observations example and provide its executable
through MUSUBI_RECORDED_OBSERVATIONS_BIN. No private fixtures are required.
"""

import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


def encoded(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def profile(units=None, clock="boot_relative"):
    return {
        "profile_id": "synthetic-recording",
        "version": "1",
        "family": "unknown",
        "source_role": "recorded_export",
        "format": "telemetry_csv_us",
        "extensions": ["csv"],
        "default_clock_basis": clock,
        "channels": ["onboard"],
        "fields": {"time": "TimeUS"},
        "units": units or {},
    }


def meanings(subject, fields):
    return {"schema": "recorded-meanings/v1", "subject": subject, "fields": fields}


def dataflash_format(message_type, record_length, name, format_codes, labels):
    """Author a synthetic FMT record using the public packed DataFlash layout."""
    return (b"\xa3\x95\x80" + bytes([message_type, record_length])
            + name.ljust(4, b"\0") + format_codes.ljust(16, b"\0")
            + labels.ljust(64, b"\0"))


def dataflash_record(message_type, payload):
    return b"\xa3\x95" + bytes([message_type]) + payload


def ulog_message(message_type, payload):
    """Author the public ULog message header: payload length and type byte."""
    return struct.pack("<HB", len(payload), ord(message_type)) + payload


BATTERY_SUBJECT = {"kind": "battery", "frame": "not_applicable", "convention": "declared"}
BATTERY_UNITS = {"battery_voltage_v": "V", "battery_current_a": "A", "battery_power_w": "W"}
BATTERY_MEANINGS = meanings(BATTERY_SUBJECT, {
    "battery_voltage_v": {"meaning": "battery_voltage", "unit": "V", "basis": "reported"},
    "battery_current_a": {"meaning": "battery_current", "unit": "A", "basis": "reported"},
    "battery_power_w": {"meaning": "battery_power", "unit": "W", "basis": "calculated"},
})


class RecordedObservationConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("MUSUBI_RECORDED_OBSERVATIONS_BIN")
        if not binary:
            raise AssertionError("MUSUBI_RECORDED_OBSERVATIONS_BIN is required; build the public example first")
        cls.binary = Path(binary).resolve()
        if not cls.binary.is_file() or not os.access(cls.binary, os.X_OK):
            raise AssertionError("MUSUBI_RECORDED_OBSERVATIONS_BIN must identify an executable")

    def invoke(self, source, reader_profile=None, annotations=None, options=()):
        return self.invoke_raw(
            source,
            encoded(reader_profile if reader_profile is not None else profile()),
            encoded(annotations if annotations is not None else BATTERY_MEANINGS),
            options,
        )

    def invoke_raw(self, source, profile_bytes, meanings_bytes, options=()):
        self.source = source
        self.profile_bytes = profile_bytes
        self.meanings_bytes = meanings_bytes
        with tempfile.TemporaryDirectory(prefix="recorded-observation-") as directory:
            root = Path(directory)
            paths = [root / "synthetic recording.csv", root / "profile.json", root / "meanings.json"]
            for path, content in zip(paths, [source, self.profile_bytes, self.meanings_bytes]):
                path.write_bytes(content)
            return subprocess.run(
                [str(self.binary), *(str(path) for path in paths), "synthetic-source", "1700000000123", *options],
                cwd=root, capture_output=True, timeout=20,
            )

    def success(self, result, channel="onboard"):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "recorded-observation-report/v1")
        self.assertIs(report["com_sealed"], False)
        self.assertEqual(report["source_id"], "synthetic-source")
        self.assertEqual(report["received_at_ms"], 1700000000123)
        self.assertEqual(report["source_bytes"], len(self.source))
        self.assertEqual(report["source_hex"], self.source.hex())
        self.assertEqual(result.stdout.count(self.source.hex().encode()), 1)
        for key, content in [("input_sha256", self.source), ("profile_sha256", self.profile_bytes),
                             ("meanings_sha256", self.meanings_bytes)]:
            self.assertEqual(report[key], hashlib.sha256(content).hexdigest())
        for index, observation in enumerate(report["observations"]):
            self.assertEqual(observation["observation_index"], index)
            self.assertEqual(observation["source"], "synthetic-source")
            self.assertEqual(observation["channel"], channel)
        return report

    def fields(self, observation):
        names = [field["name"] for field in observation["fields"]]
        self.assertEqual(len(names), len(set(names)), "duplicate source field loses accounting")
        return {field["name"]: field for field in observation["fields"]}

    def test_two_battery_recordings_preserve_reported_values_and_calculated_power(self):
        # Values are already normalized by a caller-reviewed conversion contract.
        # The connection must preserve these values, not recompute power itself.
        outputs = []
        for row, expected in [(b"1000,12.5,2,25\n", (12.5, 2, 25)),
                              (b"1000,11.75,-1.2,-14.1\n", (11.75, -1.2, -14.1))]:
            with self.subTest(values=expected):
                source = b"TimeUS,battery_voltage_v,battery_current_a,battery_power_w\n" + row
                report = self.success(self.invoke(source, profile(BATTERY_UNITS)))
                observation, = report["observations"]
                self.assertEqual(observation["subject"], BATTERY_SUBJECT)
                self.assertEqual(observation["t_ms"], 1)
                self.assertEqual(observation["t_boot_us"], 1000)
                self.assertEqual(observation["clock_basis"], "boot_relative")
                self.assertIsNone(observation["wall_ms"])
                self.assertIsNone(observation["anchor_unix_us"])
                fields = self.fields(observation)
                for name, value in zip(BATTERY_UNITS, expected):
                    self.assertAlmostEqual(fields[name]["value"], value)
                    self.assertEqual(fields[name]["source_unit"], BATTERY_UNITS[name])
                    for key in ["meaning", "unit", "basis"]:
                        self.assertEqual(fields[name][key], BATTERY_MEANINGS["fields"][name][key])
                    self.assertEqual(fields[name]["disposition"], "present")
                outputs.append(observation["fields"])
        self.assertNotEqual(outputs[0], outputs[1])

    def test_camera_pose_keeps_local_frame_quaternion_and_unknown_clock(self):
        subject = {"kind": "camera_pose", "frame": "local_first_keyframe", "convention": "camera_to_world_xyzw"}
        names = ["x", "y", "z", "qx", "qy", "qz", "qw"]
        values = [1.25, -2.5, 3.75, 0, 0, 0.6, 0.8]
        units = dict(zip(names, ["m", "m", "m", "1", "1", "1", "1"]))
        annotations = meanings(subject, {
            name: {"meaning": "camera_pose_" + name, "unit": units[name], "basis": "declared"}
            for name in names
        })
        report = self.success(self.invoke(
            b"TimeUS,x,y,z,qx,qy,qz,qw\n1000,1.25,-2.5,3.75,0,0,0.6,0.8\n",
            profile(units, clock="unknown"), annotations,
        ))
        observation, = report["observations"]
        self.assertEqual(observation["subject"], subject)
        self.assertEqual(observation["t_ms"], 1)
        self.assertEqual(observation["clock_basis"], "unknown")
        self.assertEqual(observation["time_confidence"], 0)
        for key in ["t_boot_us", "wall_ms", "anchor_unix_us"]:
            self.assertIsNone(observation[key])
        fields = self.fields(observation)
        for name, value in zip(names, values):
            self.assertAlmostEqual(fields[name]["value"], value)
            self.assertEqual(fields[name]["meaning"], "camera_pose_" + name)
            self.assertEqual(fields[name]["source_unit"], units[name])
        for inferred in ["position", "vehicle", "health", "authenticity"]:
            self.assertNotIn(inferred, observation)

    def test_undeclared_fields_blank_and_optional_meanings_are_retained(self):
        source = b"TimeUS,battery_voltage_v,Opaque,Missing\r\n1000,12.5,retained-token,\r\n"
        report = self.success(self.invoke(source, profile(BATTERY_UNITS)))
        fields = self.fields(report["observations"][0])
        self.assertEqual(set(fields), {"TimeUS", "battery_voltage_v", "Opaque", "Missing"})
        self.assertEqual(fields["Opaque"]["value"], "retained-token")
        self.assertEqual(fields["Opaque"]["disposition"], "present")
        self.assertIsNone(fields["Missing"]["value"])
        self.assertEqual(fields["Missing"]["disposition"], "blank")
        for name in ["Opaque", "Missing"]:
            for key in ["source_unit", "meaning", "unit", "basis"]:
                self.assertIsNone(fields[name][key])
        self.assertNotIn("battery_power_w", fields)

    def test_nonfinite_text_requires_explicit_option_and_preserves_spelling(self):
        source = b"TimeUS,NaNValue,PositiveInfinity,NegativeInfinity\n1000,NaN,inf,-Infinity\n"
        rejected = self.invoke(source)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, b"")
        report = self.success(self.invoke(source, options=("--preserve-nonfinite-as-text",)))
        fields = self.fields(report["observations"][0])
        for name, value in [("NaNValue", "NaN"), ("PositiveInfinity", "inf"), ("NegativeInfinity", "-Infinity")]:
            self.assertEqual(fields[name]["value"], value)
            self.assertEqual(fields[name]["disposition"], "nonfinite-as-text")
            self.assertIsNone(fields[name]["meaning"])

    def test_equal_time_requires_option_and_does_not_merge_records(self):
        source = b"TimeUS,value\n1000,17\n1000,29\n"
        rejected = self.invoke(source)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, b"")
        report = self.success(self.invoke(source, options=("--allow-equal-time",)))
        self.assertEqual(len(report["observations"]), 2)
        self.assertEqual([self.fields(item)["value"]["value"] for item in report["observations"]], [17, 29])

    def test_decreasing_time_is_rejected_even_with_equal_time_enabled(self):
        source = b"TimeUS,value\n2000,17\n1000,29\n"
        for options in [(), ("--allow-equal-time",), ("--allow-equal-time", "--preserve-nonfinite-as-text")]:
            with self.subTest(options=options):
                rejected = self.invoke(source, options=options)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(rejected.stdout, b"")

    def test_hashes_bind_independent_input_profile_and_meaning_changes(self):
        source = b"TimeUS,battery_voltage_v\n1000,12.5\n"
        original = self.success(self.invoke(source, profile(BATTERY_UNITS)))
        variants = []
        variants.append(("input_sha256", self.success(self.invoke(source.replace(b"12.5", b"13.5"), profile(BATTERY_UNITS)))))
        changed_profile = profile(BATTERY_UNITS)
        changed_profile["version"] = "2"
        variants.append(("profile_sha256", self.success(self.invoke(source, changed_profile))))
        changed_meanings = copy.deepcopy(BATTERY_MEANINGS)
        changed_meanings["fields"]["battery_voltage_v"]["basis"] = "declared"
        variants.append(("meanings_sha256", self.success(self.invoke(source, profile(BATTERY_UNITS), changed_meanings))))
        for changed, report in variants:
            for key in ["input_sha256", "profile_sha256", "meanings_sha256"]:
                if key == changed:
                    self.assertNotEqual(original[key], report[key])
                else:
                    self.assertEqual(original[key], report[key])

    def test_malformed_input_errors_are_fixed_without_source_or_path_echo(self):
        errors = []
        for token in [b"confidential-synthetic-alpha", b"confidential-synthetic-beta"]:
            result = self.invoke(b"TimeUS,value\n1000,7\n" + token + b"\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(token, result.stderr)
            self.assertNotIn(b"recorded-observation-", result.stderr)
            message = json.loads(result.stderr)
            self.assertEqual(set(message), {"error"})
            self.assertIsInstance(message["error"], str)
            self.assertLess(len(result.stderr), 128)
            errors.append(result.stderr)
        self.assertEqual(errors[0], errors[1])

    def test_duplicate_profile_keys_are_rejected_without_echoing_either_value(self):
        source = b"TimeUS,battery_voltage_v\n1000,12.5\n"
        original = encoded(profile(BATTERY_UNITS))
        variants = [
            original.replace(b'"time":"TimeUS"', b'"time":"hidden-time-sentinel","time":"TimeUS"'),
            original.replace(b'"default_clock_basis":"boot_relative"',
                             b'"default_clock_basis":"unknown","default_clock_basis":"boot_relative"'),
        ]
        errors = []
        for duplicate in variants:
            with self.subTest(duplicate=variants.index(duplicate)):
                result = self.invoke_raw(source, duplicate, encoded(BATTERY_MEANINGS))
                self.assertNotEqual(result.returncode, 0, "duplicate profile key was accepted")
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b'{"error":"recorded observation ingestion failed"}\n')
                self.assertNotIn(b"hidden-time-sentinel", result.stderr)
                self.assertNotIn(b"recorded-observation-", result.stderr)
                self.assertLess(len(result.stderr), 128)
                errors.append(result.stderr)
        if len(errors) == 2:
            self.assertEqual(errors[0], errors[1])

    def test_duplicate_meaning_keys_are_rejected_without_last_value_precedence(self):
        source = b"TimeUS,battery_voltage_v\n1000,12.5\n"
        original = encoded(BATTERY_MEANINGS)
        variants = [
            original.replace(b'"unit":"V"', b'"unit":"hidden-unit-sentinel","unit":"V"'),
            original.replace(b'"meaning":"battery_voltage"',
                             b'"meaning":"hidden-meaning-sentinel","meaning":"battery_voltage"'),
        ]
        errors = []
        for duplicate in variants:
            with self.subTest(duplicate=variants.index(duplicate)):
                result = self.invoke_raw(source, encoded(profile(BATTERY_UNITS)), duplicate)
                self.assertNotEqual(result.returncode, 0, "duplicate meaning key was accepted")
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b'{"error":"recorded observation ingestion failed"}\n')
                self.assertNotIn(b"hidden-unit-sentinel", result.stderr)
                self.assertNotIn(b"hidden-meaning-sentinel", result.stderr)
                self.assertNotIn(b"recorded-observation-", result.stderr)
                self.assertLess(len(result.stderr), 128)
                errors.append(result.stderr)
        if len(errors) == 2:
            self.assertEqual(errors[0], errors[1])

    def test_dataflash_untimed_record_coexists_with_normal_observations(self):
        definitions = (dataflash_format(42, 15, b"BAT", b"Qf", b"TimeUS,Voltage")
                       + dataflash_format(43, 7, b"AUX", b"f", b"Value"))
        first = dataflash_record(42, struct.pack("<Qf", 1000, 12.5))
        untimed = dataflash_record(43, struct.pack("<f", 7.25))
        second = dataflash_record(42, struct.pack("<Qf", 2000, 11.5))
        reader_profile = profile()
        reader_profile["format"] = "ardupilot_dataflash_bin"
        reader_profile["extensions"] = ["bin"]
        source = definitions + first + untimed + second
        report = self.success(self.invoke(source, reader_profile), channel="event")
        self.assertEqual(len(report["observations"]), 2)
        self.assertEqual([self.fields(item)["BAT.Voltage"]["value"]
                          for item in report["observations"]], [12.5, 11.5])
        self.assertEqual([item["t_boot_us"] for item in report["observations"]], [1000, 2000])
        retained, = report["accounting"]["untimed_records"]
        self.assertEqual(retained["offset"], len(definitions) + len(first))
        self.assertEqual(retained["byte_length"], len(untimed))
        start = retained["offset"]
        end = start + retained["byte_length"]
        self.assertEqual(bytes.fromhex(report["source_hex"])[start:end], untimed)
        retained_fields = {field["name"]: field["value"] for field in retained["fields"]}
        self.assertEqual(retained_fields, {"AUX.Value": 7.25})

    def test_ulog_unselected_instance_is_accounted_beside_selected_observations(self):
        header = b"ULog\x01\x12\x35\x01" + struct.pack("<Q", 0)
        definitions = (header + ulog_message("B", bytes(40))
                       + ulog_message("F", b"battery:uint64_t timestamp;float voltage;")
                       + ulog_message("A", struct.pack("<BH", 0, 0) + b"battery")
                       + ulog_message("A", struct.pack("<BH", 1, 1) + b"battery"))
        first = ulog_message("D", struct.pack("<HQf", 0, 1000, 12.5))
        unselected = ulog_message("D", struct.pack("<HQf", 1, 1500, 99.0))
        second = ulog_message("D", struct.pack("<HQf", 0, 2000, 11.5))
        reader_profile = profile()
        reader_profile["format"] = "px4_ulog"
        reader_profile["extensions"] = ["ulg"]
        reader_profile["fields"]["time"] = "timestamp"
        source = definitions + first + unselected + second
        report = self.success(self.invoke(source, reader_profile))
        self.assertEqual(len(report["observations"]), 2)
        self.assertEqual([self.fields(item)["battery.voltage"]["value"]
                          for item in report["observations"]], [12.5, 11.5])
        accounting = report["accounting"]
        self.assertEqual(accounting["data_records"], 3)
        self.assertEqual(accounting["decoded_data_records"], 2)
        self.assertEqual(accounting["selected_multi_id"], 0)
        retained, = accounting["unsupported_records"]
        self.assertEqual(retained["offset"], len(definitions) + len(first))
        self.assertEqual(retained["byte_length"], len(unselected))
        self.assertIsInstance(retained["reason"], str)
        self.assertTrue(retained["reason"].strip())
        start = retained["offset"]
        end = start + retained["byte_length"]
        self.assertEqual(bytes.fromhex(report["source_hex"])[start:end], unselected)

    def test_decoded_battery_report_can_feed_public_normalization_without_rescaling(self):
        records = [
            {"meta": {"type": "BAT"}, "data": {"TimeUS": 1000, "Instance": 0,
                "Volt": 12.5, "Curr": 2, "CurrTot": 100, "EnrgTot": 1, "RemPct": 80}},
            {"meta": {"type": "BAT"}, "data": {"TimeUS": 2000, "Instance": 0,
                "Volt": 11.5, "Curr": -1, "CurrTot": 101, "EnrgTot": 2, "RemPct": 79}},
        ]
        source = b"".join(encoded(record) for record in records)
        reader_profile = profile()
        reader_profile["format"] = "pymavlink_dataflash_jsonl"
        reader_profile["extensions"] = ["jsonl"]
        result = self.invoke(source, reader_profile)
        report = self.success(result, channel="event")
        self.assertEqual(report["value_basis"], "pymavlink_decoded_no_second_wire_scaling")
        self.assertEqual(report["source_records"], records)
        self.assertEqual(len(report["observations"]), 2)
        # Import solely from the public checkout containing this test. The
        # isolated interpreter excludes environment and user-site module paths.
        public_root = Path(__file__).resolve().parents[1]
        converter = (
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "from scripts.normalize_ardupilot_report import convert; "
            "text,accounting=convert(sys.stdin.buffer.read(),battery_format='ardupilot-bat-4.3'); "
            "print(json.dumps({'csv':text,'accounting':accounting}))"
        )
        normalized = subprocess.run(
            [sys.executable, "-I", "-c", converter, str(public_root)],
            cwd=public_root, input=result.stdout, capture_output=True, timeout=20,
        )
        self.assertEqual(normalized.returncode, 0, normalized.stderr)
        self.assertEqual(normalized.stderr, b"")
        output = json.loads(normalized.stdout)
        rows = list(csv.DictReader(io.StringIO(output["csv"])))
        self.assertEqual(len(rows), 2)
        for row, voltage, current, power, index in zip(rows, [12.5, 11.5], [2, -1], [25, -11.5], [0, 1]):
            self.assertEqual(float(row["battery_voltage_v"]), voltage)
            self.assertEqual(float(row["battery_current_a"]), current)
            self.assertEqual(float(row["battery_power_w"]), power)
            self.assertEqual(int(row["source_report_record_index"]), index)
            self.assertEqual(row["source_report_sha256"], "sha256:" + hashlib.sha256(result.stdout).hexdigest())
        self.assertEqual(output["accounting"]["selected_records"], 2)
        self.assertEqual(output["accounting"]["not_selected_records"], 0)


if __name__ == "__main__":
    unittest.main()
