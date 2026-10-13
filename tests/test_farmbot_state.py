"""Conditional producer-code checks for saved FarmBot state; no device access."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.convert_ardupilot_battery_csv import convert
from scripts.convert_software_identity_binding import convert as bind_identity

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/electrical-schema-reuse/farmbot-profile.toml"
IDENTITIES = ("controller_uuid", "controller_version", "controller_commit",
              "firmware_version", "firmware_commit")


def record(settings, time=1234567):
    return {"capture_time_us": time, "payload": {"informational_settings": settings,
        "location_data": {"position": {"x": 100, "y": None}}, "unknown": ["retain"]}}


class FarmbotStateTests(unittest.TestCase):
    def common(self, output, profile=PROFILE):
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not reader:
            self.skipTest("required CI supplies the built shared reader")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"; path.write_text(output)
            return json.loads(subprocess.run([reader, str(profile), str(path),
                "--allow-equal-time"], check=True, capture_output=True).stdout)

    def test_identity_meanings_are_declared(self):
        declared = PROFILE.read_text().split("[units]")[1]
        for field in IDENTITIES:
            self.assertIn("\n" + field + "_hex = ", declared)

    def test_reported_version_commit_pairs_bind_back_to_exact_observations(self):
        settings = {"controller_uuid": "reported-controller", "controller_version": "v1",
                    "controller_commit": "reported-a", "firmware_version": "f1",
                    "firmware_commit": "reported-fw-a", "busy": False, "soc_temp": 30}
        inputs = [record(settings), record(dict(settings, controller_commit="reported-b",
                                               firmware_commit="reported-fw-b")),
                  record({"controller_uuid": "other", "controller_version": None,
                          "firmware_version": None, "busy": True}),
                  record({key: "" for key in IDENTITIES}), record(settings)]
        output = convert("\n".join(json.dumps(r) for r in inputs), "farmbot-state")
        rows = list(csv.DictReader(io.StringIO(output)))
        common = self.common(output)
        self.assertEqual((common["main_rows"], common["platform_domain"]), (5, "Unknown"))
        for field in IDENTITIES:
            self.assertIn("reported", common["profile_units"][field + "_hex"])
        for original, row, observation in zip(inputs, rows, common["observations"]):
            self.assertEqual(json.loads(bytes.fromhex(row["source_record_hex"][4:])), original)
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["anchor_unix_us"])
            for field in IDENTITIES:
                value = original["payload"]["informational_settings"].get(field)
                if value is not None:
                    self.assertEqual(observation["fields"][field + "_hex"], "hex:" + value.encode().hex())
        self.assertEqual(common["observations"][0]["fields"]["soc_temperature_k"], 303.15)
        self.assertEqual(common["observations"][0]["fields"]["busy_reported"], 0)
        for scope in ("controller", "firmware"):
            columns = (scope + "_version_hex", scope + "_commit_hex")
            bound, report = bind_identity(output.encode(), paired_identities=(
                "SOURCE_SOFTWARE=VERSION:" + columns[0] + ",PIN:" + columns[1],),
                namespace="caller-declared-farmbot-" + scope,
                source_citation="FarmBotOS e711c03 informational_settings saved declaration")
            bindings = list(csv.DictReader(io.StringIO(bound)))
            common_binding = self.common(bound,
                ROOT / "profiles/declared/software-identity-binding/profile.toml")
            self.assertEqual(common_binding["main_rows"], 4)
            self.assertEqual(report["pairs_with_a_missing_component"], 1)
            self.assertEqual(sorted(int(r["identity_rows_carrying_value"]) for r in bindings), [1, 1, 1, 2])
            values = []
            for row in bindings:
                self.assertEqual(row["identity_kind"], "VERSION+PIN")
                self.assertEqual(row["identity_instance_hex"], "")
                self.assertEqual(row["bound_observations_sha256"], "sha256:" + hashlib.sha256(output.encode()).hexdigest())
                selection = json.loads(bytes.fromhex(row["identity_join_hex"][4:]))
                self.assertEqual(set(selection), set(columns))
                matched = [r for r in rows if all(r[k] == v for k, v in selection.items())]
                self.assertEqual(len(matched), int(row["identity_rows_carrying_value"]))
                self.assertTrue(row["identity_collision"])
                values.append(json.loads(bytes.fromhex(row["identity_value_hex"][4:])))
            self.assertIn({"VERSION": None, "PIN": None}, values)
            self.assertIn({"VERSION": "hex:", "PIN": "hex:"}, values)

    def test_every_reported_identity_rejects_nonstring_values(self):
        for key in IDENTITIES:
            for value in (False, 23, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    convert(json.dumps(record({key: value})), "farmbot-state")

    def test_meanings_nulls_false_and_source_retention(self):
        source = [record({"uptime": 12, "soc_temp": 47, "busy": False, "idle": True,
            "locked": False, "firmware_version": "declared", "controller_uuid": "reported"}),
            record({"uptime": None, "soc_temp": None, "busy": True})]
        output = convert("\n".join(json.dumps(r) for r in source), "farmbot-state")
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[0]["soc_temperature_k"]), 320.15)
        self.assertEqual(rows[0]["runtime_uptime_s"], "12")
        self.assertEqual((rows[0]["busy_reported"], rows[0]["idle_reported"], rows[0]["locked_reported"]), ("0", "1", "0"))
        self.assertEqual(rows[1]["runtime_uptime_s"], "")
        self.assertEqual(rows[1]["soc_temperature_k"], "")
        self.assertEqual(float(rows[0]["farm_position_x_m"]), 0.1)
        self.assertEqual(rows[0]["farm_position_y_m"], "")
        self.assertEqual(json.loads(bytes.fromhex(rows[0]["source_record_hex"][4:])), source[0])
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.csv"; path.write_text(output)
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/electrical-schema-reuse/farmbot-profile.toml"),
                    str(path), "--allow-equal-time"], capture_output=True, check=True)
                common = json.loads(result.stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["observations"][0]["fields"]["soc_temperature_k"], 320.15)
                self.assertIsNone(common["observations"][0]["anchor_unix_us"])

    def test_no_semantic_guess_from_malformed_empty_or_untimed_state(self):
        for settings in [{}, {"busy": "false"}, {"uptime": -1}, {"soc_temp": -274},
                         {"uptime": 1.2}, {"soc_temp": "47"}, {"controller_uuid": 12}]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                value = record(settings); value["payload"].pop("location_data")
                convert(json.dumps(value), "farmbot-state")
        raw = record({"busy": False}); raw.pop("capture_time_us")
        with self.assertRaises(ValueError): convert(json.dumps(raw), "farmbot-state")
        with self.assertRaises(ValueError): convert('{"payload":{},"payload":{}}', "farmbot-state")
        with self.assertRaises(ValueError):
            convert(json.dumps(record({"busy": True}, -1)), "farmbot-state")


    def test_location_reuses_common_path_without_frame_or_load_guess(self):
        for position in ({"x": 1000, "y": -250.5, "z": None},
                         {"x": 0, "y": 2500, "z": -10}):
            source = record({})
            source["payload"]["location_data"] = {"position": position,
                "scaled_encoders": {"x": 1250, "y": None, "z": -1},
                "raw_encoders": {"x": 2048, "y": -1, "z": 0}, "load": {"x": 999},
                "unmapped": "retained"}
            output = convert(json.dumps(source), "farmbot-state")
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row["farm_position_y_m"]), position["y"] / 1000)
            self.assertEqual(float(row["farm_scaled_encoder_x_m"]), 1.25)
            self.assertEqual(float(row["farm_raw_encoder_x_edges"]), 2048)
            self.assertEqual(json.loads(bytes.fromhex(row["source_record_hex"][4:])), source)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "location.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/farmbot-profile.toml"),
                        str(path)], capture_output=True, check=True).stdout)
                    observation = common["observations"][0]
                    self.assertEqual(common["main_rows"], 1)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(observation["fields"]["farm_position_y_m"], position["y"] / 1000)
                    self.assertIsNone(observation["anchor_unix_us"])
        for bad in (True, "10", float("inf"), [], {}):
            source = record({})
            source["payload"]["location_data"]["position"]["x"] = bad
            with self.assertRaises(ValueError):
                convert(json.dumps(source), "farmbot-state")
        for count in (-(2**63), 2**63 - 1):
            source = record({})
            source["payload"]["location_data"] = {"raw_encoders": {"x": count}}
            row = next(csv.DictReader(io.StringIO(convert(json.dumps(source), "farmbot-state"))))
            self.assertEqual(row["farm_raw_encoder_x_edges"], str(count))
        source["payload"]["location_data"]["raw_encoders"]["x"] = 2**63
        with self.assertRaises(ValueError):
            convert(json.dumps(source), "farmbot-state")


if __name__ == "__main__": unittest.main()
