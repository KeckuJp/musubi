"""Independently authored ExpressLRS /config documents; no device, UI or real export."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_elrs_settings import (MODEL_COUNT, RATE_BASIS, RATE_UNRESOLVED,
                                           REDACTION_REASON, REFERENCE_ONLY, SOURCE_FORMAT,
                                           convert_elrs_settings)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/convert_elrs_settings.py"
PROFILE = ROOT / "profiles/declared/elrs-settings/profile.toml"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
SSID, PASSWORD, DISCRIMINATOR = "ZZSSID7391", "ZZPASSWORD7391", "ZZDISCRIM7391"


def model(rate=0, tlm=4, switch=1, match=1, antenna=0, power=None, extra=None):
    entry = {"packet-rate": rate, "telemetry-ratio": tlm, "switch-mode": switch,
             "model-match": match, "tx-antenna": antenna,
             "power": power if power is not None else
             {"max-power": 3, "dynamic-power": 1, "boost-channel": 2}}
    if extra:
        entry.update(extra)
    return entry


def export_document(models=None, device=True, secrets=False):
    """Shape of GetConfiguration with the export argument at the pinned release."""
    config = {"uid": [1, 2, 3, 4, 5, 6], "model": models if models is not None else {"0": model()}}
    if device:
        config.update({"fan-mode": 2, "power-fan-threshold": 3, "motion-mode": 1,
                       "vtx-admin": {"band": 1, "channel": 5, "pitmode": 0, "power": 2},
                       "backpack": {"dvr-start-delay": 4, "dvr-stop-delay": 6, "dvr-aux-channel": 7}})
    document = {"config": config}
    if secrets:
        config["ssid"] = SSID
        document["options"] = {"wifi-ssid": SSID, "wifi-password": PASSWORD,
                               "flash-discriminator": DISCRIMINATOR, "uid": [9, 9, 9]}
    return json.dumps(document)


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def path_of(row):
    return bytes.fromhex(row["elrs_setting_path_hex"][4:]).decode()


class ElrsSettingsTests(unittest.TestCase):
    def common(self, text, directory, name):
        path = Path(directory) / f"{name}.csv"
        path.write_text(text, encoding="utf-8", newline="")
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            return None
        return json.loads(subprocess.run([READER, str(PROFILE), str(path), "--allow-equal-time"],
                                         check=True, capture_output=True, text=True).stdout)

    def test_normal_transmitter_export_reaches_common_observation(self):
        text, report = convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT, capture_time_us=1700,
                                             role="tx", radio="sx128x")
        rows = rows_of(text)
        self.assertEqual(report["declared_role"], "tx")
        self.assertEqual(report["declared_radio"], "sx128x")
        self.assertEqual(report["models_present"], 1)
        self.assertEqual(report["model_capacity"], MODEL_COUNT)
        self.assertEqual(report["clock"], "Unknown")
        self.assertEqual(report["packet_rate_basis"], RATE_BASIS)
        by_path = {path_of(row): row for row in rows}
        self.assertEqual(by_path["config.model.0.packet-rate"]["elrs_value_label"],
                         "RATE_FLRC_1000HZ")
        self.assertEqual(by_path["config.model.0.packet-rate"]["packet_rate_configured_hz"], "1000")
        self.assertIn("NOT_OBSERVED_THROUGHPUT",
                      by_path["config.model.0.packet-rate"]["packet_rate_basis"])
        self.assertEqual(by_path["config.model.0.telemetry-ratio"]["elrs_value_label"],
                         "TLM_RATIO_1_32")
        self.assertEqual(by_path["config.model.0.telemetry-ratio"]["telemetry_ratio_denominator"], "32")
        self.assertEqual(by_path["config.model.0.switch-mode"]["elrs_value_label"], "smHybridOr16ch")
        self.assertEqual(by_path["config.model.0.model-match"]["elrs_value_reported"], "1")
        self.assertEqual(by_path["config.model.0.power.max-power"]["elrs_value_reported"], "3")
        self.assertEqual(by_path["config.backpack.dvr-aux-channel"]["elrs_value_reported"], "7")
        self.assertEqual(by_path["config.vtx-admin.channel"]["elrs_scope"], "DEVICE")
        self.assertEqual(by_path["config.model.0.packet-rate"]["elrs_model_index"], "0")
        self.assertIn("config.uid", [item["path"] for item in report["redacted_fields"]])
        self.assertNotIn("config.uid", [path_of(row) for row in rows])
        with tempfile.TemporaryDirectory() as directory:
            common = self.common(text, directory, "normal")
            if common:
                self.assertEqual(common["main_rows"], len(rows))
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["elrs_scope"], "DEVICE")
                for record in common["observations"]:
                    self.assertEqual(record["clock_basis"], "Unknown")
                    self.assertIsNone(record["anchor_unix_us"])
                rate = [record for record in common["observations"]
                        if record["fields"]["elrs_value_label"] == "RATE_FLRC_1000HZ"]
                self.assertEqual(len(rate), 1)
                self.assertEqual(rate[0]["fields"]["packet_rate_configured_hz"], 1000)

    def test_distinct_layout_radio_and_unknown_enum(self):
        """A different radio, a sparse model layout, an unknown enum and an absent field."""
        models = {"3": model(rate=5, tlm=9, switch=3, match=0, antenna=1,
                             extra={"future-field": 9}),
                  "12": {"packet-rate": 2, "telemetry-ratio": 0}}
        text, report = convert_elrs_settings(export_document(models, device=False),
            source_format=SOURCE_FORMAT, capture_time_us=42, role="tx", radio="sx127x")
        rows = rows_of(text)
        by_path = {path_of(row): row for row in rows}
        self.assertEqual(report["models_present"], 2)
        self.assertEqual(by_path["config.model.3.packet-rate"]["elrs_value_label"], "RATE_DVDA_50HZ")
        self.assertEqual(by_path["config.model.3.packet-rate"]["packet_rate_configured_hz"], "50")
        self.assertEqual(by_path["config.model.3.telemetry-ratio"]["elrs_value_label"],
                         "TLM_RATIO_DISARMED")
        self.assertEqual(by_path["config.model.3.telemetry-ratio"]["telemetry_ratio_denominator"], "")
        self.assertEqual(by_path["config.model.12.telemetry-ratio"]["elrs_value_label"],
                         "TLM_RATIO_STD")
        self.assertEqual(by_path["config.model.12.telemetry-ratio"]["telemetry_ratio_denominator"], "")
        self.assertEqual(by_path["config.model.3.switch-mode"]["elrs_value_status"],
                         "UNKNOWN_ENUM_RETAINED")
        self.assertEqual(by_path["config.model.3.switch-mode"]["elrs_value_label"], "")
        self.assertEqual(by_path["config.model.3.switch-mode"]["elrs_value_reported"], "3")
        self.assertNotIn("config.model.12.switch-mode", by_path)
        self.assertNotIn("config.model.12.model-match", by_path)
        self.assertEqual([item["path"] for item in report["unknown_retained_fields"]],
                         ["config.model.3.future-field"])
        self.assertEqual(by_path["config.model.12.packet-rate"]["elrs_value_label"],
                         "RATE_LORA_100HZ")
        other, _ = convert_elrs_settings(export_document(models, device=False),
            source_format=SOURCE_FORMAT, capture_time_us=42, role="tx", radio="sx128x")
        self.assertEqual({path_of(row): row["elrs_value_label"] for row in rows_of(other)}
                         ["config.model.12.packet-rate"], "RATE_DVDA_500HZ")

    def test_radio_not_declared_leaves_the_rate_index_unresolved(self):
        text, report = convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT, capture_time_us=1, role="tx")
        row = {path_of(item): item for item in rows_of(text)}["config.model.0.packet-rate"]
        self.assertEqual(report["declared_radio"], "NOT_DECLARED")
        self.assertEqual(report["packet_rate_basis"], RATE_UNRESOLVED)
        self.assertEqual(row["elrs_value_status"], RATE_UNRESOLVED)
        self.assertEqual(row["packet_rate_configured_hz"], "")
        self.assertEqual(row["elrs_value_label"], "")
        self.assertEqual(row["elrs_value_reported"], "0")

    def test_secret_looking_fields_are_accounted_but_never_emitted(self):
        text, report = convert_elrs_settings(export_document(secrets=True), source_format=SOURCE_FORMAT, capture_time_us=5,
                                             role="tx", radio="lr1121")
        blob = json.dumps(report) + text
        for secret in (SSID, PASSWORD, DISCRIMINATOR):
            self.assertNotIn(secret, blob)
        redacted = {item["path"]: item for item in report["redacted_fields"]}
        self.assertEqual(sorted(redacted), ["config.ssid", "config.uid", "options"])
        self.assertEqual(redacted["options"]["member_count"], 4)
        for item in report["redacted_fields"]:
            self.assertEqual(item["reason"], REDACTION_REASON)
            self.assertEqual(sorted(item), sorted({"path", "reason"} | ({"member_count"}
                                                   if "member_count" in item else set())))
        self.assertNotIn("value_sha256", blob)
        self.assertNotIn("member_keys", blob)
        for name in ("wifi-password", "wifi-ssid", "flash-discriminator"):
            self.assertNotIn(name, blob)
        self.assertIn("secret_policy", report)
        self.assertNotIn("options", [path_of(row) for row in rows_of(text)])

    def test_full_model_capacity_reaches_common_observation(self):
        models = {str(index): model(rate=index % 6, tlm=(index % 7) + 2, switch=index % 3,
                                    match=index % 2, antenna=index % 4)
                  for index in range(MODEL_COUNT)}
        text, report = convert_elrs_settings(export_document(models), source_format=SOURCE_FORMAT, capture_time_us=9,
                                             role="tx", radio="sx127x")
        rows = rows_of(text)
        self.assertEqual(report["models_present"], MODEL_COUNT)
        device_settings = 3 + 4 + 3
        self.assertEqual(len(rows), MODEL_COUNT * 8 + device_settings)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                         "3b89ed08820b7d4e1f5f9335554d660904134d3d01637ff315867de8c6fa2178")
        self.assertEqual(report["unknown_retained_fields"], [])
        self.assertEqual(sum(1 for row in rows if row["elrs_scope"] == "DEVICE"), device_settings)
        self.assertEqual(report["output_records"], len(rows))
        indexes = sorted({int(row["elrs_model_index"]) for row in rows if row["elrs_model_index"]})
        self.assertEqual(indexes, list(range(MODEL_COUNT)))
        with tempfile.TemporaryDirectory() as directory:
            common = self.common(text, directory, "full")
            if common:
                self.assertEqual(common["main_rows"], len(rows))
                labels = {record["fields"]["elrs_value_label"] for record in common["observations"]
                          if record["fields"]["elrs_value_label"]}
                self.assertIn("RATE_DVDA_50HZ", labels)
                self.assertIn("TLM_RATIO_1_2", labels)

    def test_wrong_role_format_and_out_of_range_values_reject(self):
        cases = [
            (export_document(), dict(role="rx", radio="sx128x"), "rx role with model block"),
            ("[]", dict(role="tx"), "not an object"),
            ('{"options": {}}', dict(role="tx"), "no config block"),
            (json.dumps({"config": {"model": []}}), dict(role="tx"), "model not an object"),
            (json.dumps({"config": {"model": {"64": model()}}}), dict(role="tx"), "model out of range"),
            (json.dumps({"config": {"model": {"x": model()}}}), dict(role="tx"), "non numeric model"),
            (json.dumps({"config": {"model": {"0": model(rate=16)}}}), dict(role="tx"), "rate over 4 bits"),
            (json.dumps({"config": {"model": {"0": model(tlm=16)}}}), dict(role="tx"), "tlm over 4 bits"),
            (json.dumps({"config": {"model": {"0": model(switch=4)}}}), dict(role="tx"), "switch over 2 bits"),
            (json.dumps({"config": {"model": {"0": model(antenna=-1)}}}), dict(role="tx"), "negative"),
            (json.dumps({"config": {"model": {"0": model(rate="0")}}}), dict(role="tx"), "string value"),
            (json.dumps({"config": {"model": {"0": model(power={"max-power": 8})}}}),
             dict(role="tx"), "power over 3 bits"),
            (json.dumps({"config": {"model": {"0": model(power=5)}}}), dict(role="tx"), "power not object"),
            (json.dumps({"config": {"vtx-admin": 5}}), dict(role="tx"), "vtx not object"),
            (json.dumps({"config": {}}), dict(role="rx"), "no selected settings")]
        for text, options, reason in cases:
            with self.assertRaises(ValueError, msg=reason):
                convert_elrs_settings(text, source_format=SOURCE_FORMAT, capture_time_us=1, **options)
        for capture, reason in ((-1, "negative capture"), (1.5, "float capture"), (None, "absent")):
            with self.assertRaises(ValueError, msg=reason):
                convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT, capture_time_us=capture, role="tx")
        with self.assertRaises(ValueError):
            convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT, capture_time_us=1, role="both")
        with self.assertRaises(ValueError):
            convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT, capture_time_us=1, role="tx", radio="sx1276")

    def test_declared_source_format_is_required_and_never_inferred(self):
        """The export carries no version, so this is a caller precondition."""
        text, report = convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT,
                                             capture_time_us=1, role="tx")
        self.assertEqual(report["declared_source_format"], SOURCE_FORMAT)
        self.assertIn("NOT_IN_BAND_FIRMWARE_AUTHENTICATION", report["source_format_basis"])
        self.assertIn("transmitter export shape only", report["scope"])
        for declared in ("elrs-config-export-4.1.0", "elrs-config-export-3.6.3", "3.6.4",
                         "", None, "ELRS-CONFIG-EXPORT-3.6.4"):
            with self.assertRaises(ValueError, msg=repr(declared)):
                convert_elrs_settings(export_document(), source_format=declared,
                                      capture_time_us=1, role="tx")
        failed = subprocess.run([os.sys.executable, str(SCRIPT), "/dev/null", "/dev/null",
                                 "--capture-time-us", "1", "--role", "tx"], capture_output=True)
        self.assertNotEqual(failed.returncode, 0)

    def test_receiver_declaration_is_refused_with_the_source_reason(self):
        """Every selected field is TARGET_TX only, so an rx export has none of them."""
        with self.assertRaises(ValueError) as caught:
            convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT,
                                  capture_time_us=1, role="rx")
        self.assertIn("contradicts transmitter export fields", str(caught.exception))
        for field in ('{"config": {"fan-mode": 1}}', '{"config": {"vtx-admin": {"band": 1}}}',
                      '{"config": {"backpack": {"dvr-start-delay": 1}}}'):
            with self.assertRaises(ValueError) as caught:
                convert_elrs_settings(field, source_format=SOURCE_FORMAT,
                                      capture_time_us=1, role="rx")
            self.assertIn("contradicts transmitter export fields", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            convert_elrs_settings('{"config": {"uid": [1, 2]}}', source_format=SOURCE_FORMAT,
                                  capture_time_us=1, role="rx")
        self.assertIn("no selected settings", str(caught.exception))

    def test_duplicate_keys_are_refused_without_echoing_a_value(self):
        duplicates = (
            '{"config": {"fan-mode": 1}, "config": {"fan-mode": 2}}',
            '{"config": {"fan-mode": 1, "fan-mode": 2}}',
            '{"config": {"model": {"0": {"packet-rate": 1}, "0": {"packet-rate": 2}}}}',
            '{"config": {"model": {"0": {"packet-rate": 1, "packet-rate": 2}}}}',
            '{"config": {"fan-mode": 1}, "options": {"wifi-password": "' + PASSWORD +
            '", "wifi-password": "' + PASSWORD + '"}}')
        for text in duplicates:
            with self.assertRaises(ValueError) as caught:
                convert_elrs_settings(text, source_format=SOURCE_FORMAT,
                                      capture_time_us=1, role="tx")
            message = str(caught.exception)
            self.assertIn("duplicate", message)
            for secret in (PASSWORD, "wifi-password"):
                self.assertNotIn(secret, message)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "duplicate.json"
            source.write_text(duplicates[-1], encoding="utf-8")
            failed = subprocess.run([os.sys.executable, str(SCRIPT), str(source),
                str(Path(directory) / "out"), "--source-format", SOURCE_FORMAT,
                "--capture-time-us", "1", "--role", "tx"], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn(PASSWORD, failed.stdout + failed.stderr)
            self.assertFalse((Path(directory) / "out").exists())

    def test_no_unknown_value_is_inlined_whatever_its_json_type(self):
        """A number can be a PIN or a one-time code, so type says nothing about secrecy."""
        models = {"0": model(extra={"future_otp": 654321, "future-note": "ZZUNKNOWNSTR7391",
                                    "future-block": {"a": 1}, "future-flag": True,
                                    "future-null": None, "future-real": 1.5,
                                    "future-list": [7, 8]})}
        text, report = convert_elrs_settings(export_document(models, device=False),
                                             source_format=SOURCE_FORMAT, capture_time_us=1,
                                             role="tx", radio="sx128x")
        unknown = {item["path"]: item for item in report["unknown_retained_fields"]}
        self.assertEqual(sorted(unknown), sorted(
            f"config.model.0.{name}" for name in
            ("future_otp", "future-note", "future-block", "future-flag", "future-null",
             "future-real", "future-list")))
        for path, item in unknown.items():
            self.assertEqual(item, {"path": path, "status": REFERENCE_ONLY,
                                    "source_sha256": report["source_sha256"]}, msg=path)
        blob = json.dumps(report) + text
        for value in ("654321", "ZZUNKNOWNSTR7391", "1.5", "INLINE_VALUE", "value_sha256"):
            self.assertNotIn(value, blob)
        by_path = {path_of(row): row for row in rows_of(text)}
        self.assertEqual(by_path["config.model.0.packet-rate"]["elrs_value_label"],
                         "RATE_FLRC_1000HZ")

    def test_command_line_writes_a_new_directory_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.json"
            source.write_text(export_document(secrets=True), encoding="utf-8")
            target = Path(directory) / "out"
            result = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                                     "--source-format", SOURCE_FORMAT, "--capture-time-us", "1700", "--role", "tx",
                                     "--radio", "sx128x"], check=True, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["declared_radio"], "sx128x")
            saved = (target / "observations.csv").read_text()
            self.assertEqual(saved, convert_elrs_settings(export_document(secrets=True),
                source_format=SOURCE_FORMAT, capture_time_us=1700, role="tx", radio="sx128x")[0])
            written = (target / "report.json").read_text()
            for secret in (SSID, PASSWORD, DISCRIMINATOR):
                self.assertNotIn(secret, written + saved + result.stdout)
            failed = subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(target),
                                     "--source-format", SOURCE_FORMAT, "--capture-time-us", "1700", "--role", "tx"],
                                    capture_output=True)
            self.assertNotEqual(failed.returncode, 0)


class VtxSavedSettingsTests(unittest.TestCase):
    """The saved VTX administration settings, read as settings and never as radiated output.

    Every meaning asserted here comes from the pinned source, not from a key's name:
    `config.h:85-88` gives band "0=Off, else band number", channel "0=Ch1 -> 7=Ch8", power
    "0=Do not set, else power number" and pitmode "Off/On/AUX1^/AUX1v/etc"; `devVTX.cpp` uses
    pitmode <= 1 directly and otherwise derives an aux channel and inversion from it, and builds
    `(band-1)*8 + channel`; `devBackpack.cpp` indexes a {0,5,15,30,45,60,120} seconds table for the
    DVR delays and derives an aux channel and inversion from `dvrAux`.

    So pitmode and dvrAux are ENCODED SELECTIONS, not flags or plain channel numbers. No index is
    resolved here and no frequency or milliwatt value is produced. Authored documents only -- real
    records 0.
    """

    def vtx_rows(self):
        text = convert_elrs_settings(export_document(), source_format=SOURCE_FORMAT,
                                     capture_time_us=4242, role="tx")[0]
        return text, {path_of(row): row for row in rows_of(text)}

    def test_vtx_settings_carry_the_pinned_stored_meaning_and_no_rf_unit(self):
        _, by_path = self.vtx_rows()
        for name, value in (("band", "1"), ("channel", "5"), ("power", "2")):
            row = by_path[f"config.vtx-admin.{name}"]
            with self.subTest(name=name):
                self.assertEqual(row["elrs_value_reported"], value)
                self.assertEqual(row["elrs_value_label"], "")
                self.assertEqual(row["elrs_scope"], "DEVICE")
        band = by_path["config.vtx-admin.band"]["elrs_value_status"]
        self.assertIn("BAND_NUMBER_WHERE_ZERO_MEANS_OFF", band)
        self.assertIn("NO_FREQUENCY_IN_MHZ_FOLLOWS_FROM_IT", band)
        channel = by_path["config.vtx-admin.channel"]["elrs_value_status"]
        self.assertIn("ZERO_IS_CHANNEL_ONE_AND_SEVEN_IS_CHANNEL_EIGHT", channel)
        self.assertIn("STILL_NOT_A_FREQUENCY", channel)
        power = by_path["config.vtx-admin.power"]["elrs_value_status"]
        self.assertIn("ZERO_MEANS_DO_NOT_SET", power)
        self.assertIn("NO_POWER_IN_MILLIWATTS_FOLLOWS_FROM_IT", power)
        pitmode = by_path["config.vtx-admin.pitmode"]
        self.assertEqual(pitmode["elrs_value_reported"], "0")
        self.assertIn("NOT_A_BINARY_FLAG", pitmode["elrs_value_status"])
        self.assertIn("ENCODES_AN_AUX_CHANNEL_AND_INVERSION", pitmode["elrs_value_status"])
        self.assertIn("NEVER_A_MEASUREMENT_OF_RADIATED_OUTPUT", pitmode["elrs_value_status"])
        document = json.loads(export_document())
        document["config"]["vtx-admin"]["pitmode"] = 7
        aux = {path_of(row): row for row in rows_of(convert_elrs_settings(
            json.dumps(document), source_format=SOURCE_FORMAT, capture_time_us=1, role="tx")[0])}
        self.assertEqual(aux["config.vtx-admin.pitmode"]["elrs_value_reported"], "7")
        self.assertIn("NOT_A_BINARY_FLAG", aux["config.vtx-admin.pitmode"]["elrs_value_status"])

    def test_the_backpack_dvr_settings_declare_their_own_kind(self):
        _, by_path = self.vtx_rows()
        for name in ("dvr-start-delay", "dvr-stop-delay"):
            row = by_path[f"config.backpack.{name}"]
            with self.subTest(name=name):
                self.assertIn("DVR_DELAY_TABLE_INDEX", row["elrs_value_status"])
                self.assertIn("TABLE_0_5_15_30_45_60_120", row["elrs_value_status"])
                self.assertIn("NOT_RESOLVED_TO_SECONDS_HERE", row["elrs_value_status"])
                self.assertIn("AN_INDEX_AND_NOT_A_DURATION", row["elrs_value_status"])
        channel = by_path["config.backpack.dvr-aux-channel"]
        self.assertIn("NOT_A_PLAIN_CHANNEL_NUMBER", channel["elrs_value_status"])
        self.assertIn("ENCODES_AN_AUX_CHANNEL_AND_INVERSION", channel["elrs_value_status"])
        self.assertIn("NEVER_A_COMMAND", channel["elrs_value_status"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_the_vtx_settings_reach_the_actual_common_reader(self):
        text, by_path = self.vtx_rows()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vtx.csv"
            path.write_text(text, encoding="utf-8")
            common = json.loads(subprocess.run([READER, str(PROFILE), str(path), "--allow-equal-time"],
                                               check=True, capture_output=True, text=True).stdout)
        self.assertEqual(common["main_rows"], len(by_path))
        self.assertEqual(common["platform_domain"], "Unknown")
        statuses = {o["fields"]["elrs_value_status"] for o in common["observations"]}
        self.assertTrue(any("BAND_NUMBER_WHERE_ZERO_MEANS_OFF" in s for s in statuses))
        self.assertTrue(any("NOT_A_BINARY_FLAG" in s for s in statuses))
        self.assertTrue(any("DVR_DELAY_TABLE_INDEX" in s for s in statuses))
        units = common["profile_units"]
        self.assertIn("ENCODED_SELECTION_and_not_a_binary_flag", units["elrs_value_status"])
        self.assertIn("power_in_milliwatts_here", units["elrs_value_status"])
        self.assertIn("width_this_converter_declares_for_that_particular_field",
                      units["elrs_value_reported"])
        self.assertIn("checked_at_eight_bits", units["elrs_value_reported"])
        self.assertIn("not_a_width_guarantee", units["elrs_value_reported"])

    def test_missing_unknown_and_wrong_version_stay_refused_or_accounted(self):
        text = convert_elrs_settings(export_document(device=False), source_format=SOURCE_FORMAT,
                                     capture_time_us=1, role="tx")[0]
        self.assertEqual([p for p in map(path_of, rows_of(text)) if "vtx-admin" in p], [])
        with self.assertRaises(ValueError):
            convert_elrs_settings(export_document(), source_format="elrs-config-export-9.9.9",
                                  capture_time_us=1, role="tx")
        document = json.loads(export_document())
        document["config"]["vtx-admin"]["undocumented"] = 3
        report = convert_elrs_settings(json.dumps(document), source_format=SOURCE_FORMAT,
                                       capture_time_us=1, role="tx")[1]
        self.assertIn("config.vtx-admin.undocumented",
                      [item["path"] for item in report["unknown_retained_fields"]])
        self.assertNotIn("config.vtx-admin.undocumented",
                         [path_of(row) for row in rows_of(convert_elrs_settings(
                             json.dumps(document), source_format=SOURCE_FORMAT,
                             capture_time_us=1, role="tx")[0])])


if __name__ == "__main__":
    unittest.main()
