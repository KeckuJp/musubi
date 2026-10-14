"""Authored saved attributes, never local hardware, through the real common reader."""
import io
import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.convert_saved_psu import PIN, convert
from scripts.recorded_units import normalize


class RecordedUnitUnderflowTests(unittest.TestCase):
    def test_nonzero_scale_underflow_is_not_a_reported_zero(self):
        for unit in ("mV", "mm", "uW", "deg"):
            for value in (5e-324, -5e-324):
                with self.subTest(unit=unit, value=value):
                    with self.assertRaisesRegex(ValueError, "underflow"):
                        normalize(value, unit)

    def test_identity_zero_offset_and_representable_values_are_unchanged(self):
        self.assertEqual(normalize(5e-324, "V"), ("voltage_v", 5e-324))
        self.assertEqual(normalize(0, "mV"), ("voltage_v", 0.0))
        self.assertEqual(normalize(-0.0, "mm"), ("distance_m", 0.0))
        self.assertEqual(normalize(-273.15, "degC"), ("temperature_k", 0.0))
        self.assertEqual(normalize(5e-324, "degC"), ("temperature_k", 273.15))
        self.assertEqual(normalize(1250, "mV"), ("voltage_v", 1.25))
        self.assertNotEqual(normalize(1e-300, "mV")[1], 0)

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/declared/saved-psu/profile.toml"


def snapshot(**changes):
    value = dict(schema="saved-corsair-psu/v1", source_pin=PIN, driver="corsair-psu",
                 native_units="linux-hwmon-abi", source_id="authored-psu-a",
                 association_basis="authored fixture, not authenticated device association",
                 capture_time_us=1000000,
                 attributes={"in0_input": "230000\n", "in1_input": "12120\n",
                             "curr2_input": "1500\n", "power2_input": "18180000\n"})
    value.update(changes)
    value["attributes"] = {"name": "corsairpsu\n", **value["attributes"]}
    return value


def export(*records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


class SavedPsuTests(unittest.TestCase):
    def test_saved_pmbus_pages_reach_common_without_presence_inference(self):
        reader = os.environ.get('MUSUBI_TELEMETRY_READER')
        self.assertTrue(reader)
        for driver, page, width in (('ibm_cffps1', 0, 16), ('ibm_cffps2', 1, 8)):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as folder:
                prefix = 'status'+str(page)
                record = dict(schema='saved-ibm-pmbus-status/v1', source_pin=PIN,
                    driver=driver, native_units='linux-pmbus-debugfs', source_id='authored-'+driver,
                    association_basis='saved page capture declaration only', capture_time_us=9,
                    page=page, status_width=width, attributes={prefix:'0x0040\n',
                    prefix+'_input':'0x08\n', prefix+'_vout':'0x01\n', prefix+'_iout':'0x00\n',
                    prefix+'_temp':None, prefix+'_mfr':'0x10\n', prefix+'_cml':'0x84\n',
                    prefix+'_other':'0x02\n', 'future':'uninterpreted'})
                output = io.StringIO(); self.assertEqual(convert(io.StringIO(export(record)), output, pmbus_status=True), 1)
                path = Path(folder)/'out.csv'; path.write_text(output.getvalue())
                if page == 0:
                    saved = Path(folder)/'saved.jsonl'; saved.write_text(export(record))
                    cli = Path(folder)/'cli.csv'
                    subprocess.run([os.sys.executable, str(ROOT/'scripts/convert_saved_psu.py'),
                        str(saved), str(cli), '--pmbus-status'], check=True, capture_output=True)
                    self.assertEqual(cli.read_text(), output.getvalue())
                profile = PROFILE.with_name('status-profile.toml')
                common = json.loads(subprocess.run([reader, str(profile), str(path)], check=True, capture_output=True).stdout)
                observation = common['observations'][0]; fields = observation['fields']
                self.assertEqual(observation['clock_basis'], 'Unknown')
                self.assertEqual(fields['reported_status_off'], 'true')
                self.assertEqual(fields['reported_status_input_vin_off'], 'true')
                self.assertEqual(fields['reported_status_iout_oc_fault'], 'false')
                self.assertEqual(fields['reported_status_vout_unknown_bits'], 1)
                self.assertIsNone(fields['reported_status_temp_ot_fault'])
                self.assertEqual(fields['reported_status_temp_availability'], 'CAPTURE_UNAVAILABLE')
                self.assertEqual(fields['reported_status_mfr_ps_kill'], 'true')
                self.assertEqual(fields['reported_status_cml_invalid_command'], 'true')
                self.assertEqual(fields['reported_status_cml_unknown_bits'], 4)
                self.assertEqual(fields['reported_status_other_unknown_bits'], 2)
                self.assertEqual(fields['physical_ac_presence'], 'UNKNOWN_NOT_A_CABLE_PRESENCE_TEST')
                self.assertEqual(fields['primary_status_coverage'], 'ALL_PRIMARY_REGISTERS_REPORTED')
                self.assertEqual(fields['unknown_attribute_count'], 1)
                self.assertEqual(fields['page'], page)
                self.assertEqual(fields['reported_status_power_good_n'], 'false' if width==16 else None)
                self.assertEqual(fields['source_record_hex'], 'hex:'+export(record).rstrip('\n').encode().hex())
                if page==1:
                    self.assertEqual(fields['reported_status_fan12_availability'], 'NOT_EXPOSED_ON_PAGE')

    def test_saved_pmbus_refuses_wrong_page_width_producer_and_format(self):
        base = dict(schema='saved-ibm-pmbus-status/v1', source_pin=PIN, driver='ibm_cffps1',
            native_units='linux-pmbus-debugfs', source_id='a', association_basis='authored',
            capture_time_us=1, page=0, status_width=16, attributes={'status0':'0x0000\n'})
        for changes in ({'driver':'corsair-psu'}, {'page':1}, {'page':True}, {'status_width':32},
                        {'attributes':{'status1':'0x0000'}}, {'attributes':{'status0':'0xfffff'}},
                        {'attributes':{'status0_input':'8'}}, {'attributes':{'status0_input':False}},
                        {'status_width':8,'attributes':{'status0':'0x0100'}},
                        {'driver':'ibm_cffps2','page':1,'attributes':{'status1_fan12':'0x00'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                convert(io.StringIO(export(dict(base, **changes))), io.StringIO(), pmbus_status=True)
        output = io.StringIO()
        convert(io.StringIO(export(base)), output, pmbus_status=True)
        row = next(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(row['primary_status_coverage'], 'INCOMPLETE_PRIMARY_REGISTERS')
        self.assertEqual(row['reported_status_input_vin_off'], '')

    def test_cli_validates_late_errors_before_creating_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "saved.jsonl", Path(tmp) / "out.csv"
            args = ["python3", str(ROOT / "scripts/convert_saved_psu.py"), str(source), str(target)]
            source.write_text(export(snapshot()) + "{bad}\n")
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
            self.assertFalse(target.exists())
            source.write_text(export(snapshot()))
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)
            original = target.read_bytes()
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
            self.assertEqual(target.read_bytes(), original)

    def test_microwatts_reuse_finite_unit_normalizer(self):
        self.assertEqual(normalize(400000000, "uW"), ("power_w", 400))
        self.assertEqual(normalize(-1000000, "uW"), ("power_w", -1))
        for value in (True, float("nan"), float("inf"), 2**53 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize(value, "uW")

    def test_schema_units_binding_and_native_integer_refusals(self):
        for changes in ({"schema": "other"}, {"source_pin": "other"},
                        {"driver": "other"}, {"native_units": "V"},
                        {"source_id": ""}, {"association_basis": ""},
                        {"capture_time_us": True}, {"extra": "no"},
                        {"attributes": {"in0_input": "230 V"}},
                        {"attributes": {"in1_input": "12.1"}},
                        {"attributes": {"name": "another_chip"}},
                        {"attributes": {"in0_input": 230000}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                convert(io.StringIO(export(snapshot(**changes))), io.StringIO())

    def test_large_record_rejected_but_ordinary_long_capture_not_capped(self):
        out = io.StringIO()
        count = convert(io.StringIO(export(snapshot()) * 10001), out)
        self.assertEqual(count, 10001)
        self.assertEqual(out.getvalue().count("\n"), 10002)
        with self.assertRaises(ValueError):
            convert(io.StringIO(export(snapshot(attributes={"future": "x" * 70000}))), io.StringIO())

    def test_duplicate_keys_and_decreasing_capture_refuse(self):
        no_name = snapshot()
        del no_name["attributes"]["name"]
        with self.assertRaises(ValueError):
            convert(io.StringIO(export(no_name)), io.StringIO())
        with self.assertRaises(ValueError):
            convert(io.StringIO(export(snapshot()).replace('"driver":', '"schema":"duplicate","driver":')), io.StringIO())
        with self.assertRaises(ValueError):
            convert(io.StringIO(export(snapshot(), snapshot(capture_time_us=999999))), io.StringIO())


class SavedPsuReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        if not cls.reader:
            raise unittest.SkipTest("real common reader not configured")
        if not Path(cls.reader).is_file():
            raise AssertionError("configured reader absent")

    def read(self, records):
        output = io.StringIO()
        count = convert(io.StringIO(export(*records)), output)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved.csv"
            path.write_text(output.getvalue())
            result = json.loads(subprocess.run([self.reader, str(PROFILE), str(path),
                                                "--allow-equal-time"], check=True,
                                               capture_output=True).stdout)
        self.assertEqual(result["main_rows"], count)
        for observation in result["observations"]:
            self.assertEqual(observation["clock_basis"], "Unknown")
            self.assertIsNone(observation["wall_ms"])
            self.assertIsNone(observation["t_boot_us"])
        return result

    def test_two_distinct_snapshots_normalize_native_units_and_keep_source(self):
        a = snapshot()
        b = snapshot(source_id="authored-psu-b", capture_time_us=2000000,
                     attributes={"in0_input": "100000", "in2_input": "5000",
                                 "curr1_input": "2500", "power1_input": "400000000"})
        result = self.read([a, b])
        first, second = [o["fields"] for o in result["observations"]]
        self.assertAlmostEqual(first["in1_input_voltage_v"], 12.12)
        self.assertAlmostEqual(first["curr2_input_current_a"], 1.5)
        self.assertAlmostEqual(first["power2_input_power_w"], 18.18)
        self.assertEqual(second["curr1_input_current_a"], 2.5)
        self.assertEqual(second["power1_input_power_w"], 400)
        self.assertNotEqual(first["source_id_hex"], second["source_id_hex"])
        self.assertEqual(json.loads(bytes.fromhex(first["source_record_hex"][4:])), a)
        self.assertEqual(first["alarm_status"], "NOT_EXPOSED_BY_SELECTED_DRIVER")

    def test_missing_zero_null_and_unknown_are_distinct_and_accounted(self):
        record = snapshot(attributes={"in1_input": "0", "curr2_input": None,
                                      "in1_alarm": "1", "future_native": "kept"})
        fields = self.read([record])["observations"][0]["fields"]
        self.assertEqual(fields["in1_input_voltage_v"], 0)
        self.assertEqual(fields["in1_input_availability"], "REPORTED")
        self.assertIsNone(fields["curr2_input_current_a"])
        self.assertEqual(fields["curr2_input_availability"], "CAPTURE_UNAVAILABLE")
        self.assertEqual(fields["in0_input_availability"], "NOT_CAPTURED")
        self.assertEqual(fields["unknown_attribute_count"], 2)
        native = json.loads(bytes.fromhex(fields["source_record_hex"][4:]))["attributes"]
        self.assertEqual(native, record["attributes"])
        self.assertEqual(fields["alarm_status"], "NOT_EXPOSED_BY_SELECTED_DRIVER")

    def test_10001_saved_snapshots_reach_common_without_drop(self):
        records = [snapshot(capture_time_us=1000000 + i) for i in range(10001)]
        result = self.read(records)
        self.assertEqual(len(result["observations"]), 10001)
        self.assertEqual(result["observations"][-1]["fields"]["source_record_index"], 10000)


if __name__ == "__main__":
    unittest.main()
