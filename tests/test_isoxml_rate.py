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


GEOMETRY_TEMPLATE = TEMPLATE.replace(b'A="0001"', b'A="0043"')
GEOMETRY_DESCRIPTION = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2"><DVC A="DVC-1">'
    '<DET A="DET-1" B="1" C="4" D="Left" E="1" F="0">'
    '<DOR A="10"/><DOR A="11"/><DOR A="12"/><DOR A="13"/><DOR A="14"/><DOR A="15"/>'
    '<DOR A="16"/><DOR A="17"/><DOR A="99"/></DET>'
    '<DET A="DET-2" B="2" C="4" D="Right" E="2" F="0"/>'
    '<DPT A="10" B="0043" C="3000"/><DPT A="11" B="0044" C="3200"/><DPT A="12" B="0046" C="3500"/>'
    '<DPT A="13" B="0086" C="-500"/><DPT A="14" B="0087" C="1234"/>'
    '<DPT A="15" B="0088" C="1200" E="30"/><DPT A="16" B="00A0" C="7"/>'
    '<DPD A="17" B="0002" C="1" D="1"/><DVP A="30" B="0" C="0.001" D="3" E="m"/>'
    '</DVC></ISO11783_TaskData>')


def row(changes, *, position=True, ms=1000):
    data = struct.pack("<IH", ms, 10000)
    if position:
        data += struct.pack("<iiB", 0, 0, 0)  # Zero GNSS does not remove application records.
    data += bytes([len(changes)])
    return data + b"".join(struct.pack("<Bi", index, value) for index, value in changes)


class IsoxmlRateTests(unittest.TestCase):
    def test_device_reference_links_local_ids_without_changing_rate(self):
        for designator in ('Left', 'Right'):
            description = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2"><!-- writer -->'
                '<DVC A="DVC-1"><DET A="DET-1" B="1" C="4" D="' + designator + '" E="1" F="0"/></DVC>'
                '<DVC A="DVC-2"><DET A="DET-2" B="1" C="99" E="2" F="0"/></DVC>'
                '</ISO11783_TaskData>').encode()
            data = row([(0, 10000), (2, 20000)])
            baseline, _ = convert(TEMPLATE, data)
            output, report = convert(TEMPLATE, data, taskdata=description)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r['actual_volume_rate_l_ha'] for r in rows],
                             [r['actual_volume_rate_l_ha'] for r in csv.DictReader(io.StringIO(baseline))])
            self.assertEqual([r['declared_device_id_hex'] for r in rows], ['hex:4456432d31', 'hex:4456432d32'])
            self.assertEqual([r['declared_element_object_id'] for r in rows], ['1', '1'])
            self.assertEqual(rows[1]['declared_element_type'], 'UNKNOWN_RETAINED')
            self.assertEqual(rows[0]['declared_element_designator_hex'], 'hex:' + designator.encode().hex())
            self.assertEqual(bytes.fromhex(report['device_description_hex']), description)
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / 'tim.xml').write_bytes(TEMPLATE); (base / 'tim.bin').write_bytes(data)
                (base / 'task.xml').write_bytes(description)
                subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_isoxml_rate.py'),
                    str(base / 'tim.xml'), str(base / 'tim.bin'), str(base / 'out'),
                    '--taskdata', str(base / 'task.xml')], check=True, capture_output=True)
                self.assertEqual((base / 'out/observations.csv').read_text(), output)
                if os.environ.get('MUSUBI_TELEMETRY_READER'):
                    common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(ROOT / 'profiles/declared/isoxml-rate/profile.toml'),
                        str(base / 'out/observations.csv'), '--allow-equal-time'], check=True, capture_output=True).stdout)
                    self.assertEqual(common['main_rows'], 2)
                    self.assertEqual(common['observations'][1]['fields']['declared_element_type'], 'UNKNOWN_RETAINED')
                    self.assertEqual(common['platform_domain'], 'Unknown')

    def test_device_reference_rejects_ambiguous_graph_and_handles_long_chain(self):
        from scripts.convert_isoxml_rate import device_references
        def document(elements):
            return ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2"><DVC A="DVC-1">'
                    + elements + '</DVC></ISO11783_TaskData>').encode()
        first = '<DET A="DET-1" B="1" C="1" E="0" F="0"/>'
        second = '<DET A="DET-2" B="2" C="4" E="1" F="1"/>'
        for broken in (first + first, first + second.replace('B="2"', 'B="1"'),
                       first + second.replace('F="1"', 'F="3"'),
                       first.replace('F="0"', 'F="2"') + second,
                       first + '<DPD A="1"/>'):
            with self.assertRaises(ValueError): device_references(document(broken))
        valid = document(first + second)
        for broken in (valid.replace(b'VersionMinor="2"', b'VersionMinor="1"'),
                       b'<!DOCTYPE x>' + valid):
            with self.assertRaises(ValueError): device_references(broken)
        with self.assertRaises(ValueError): convert(TEMPLATE, row([(0, 1)]), taskdata=document(first))
        chain = ''.join(f'<DET A="DET-{i}" B="{i}" C="4" E="{i}" F="{i-1}"/>'
                        for i in range(1, 1001))
        references, _ = device_references(document(chain))
        self.assertEqual(len(references), 1000)
        self.assertEqual(references['DET-1000'][2], 999)

    def test_work_state_rows_carry_the_declared_device_element_they_belong_to(self):
        """The required link: a reported state attributable to its declared element."""
        template = (b'<TIM A="" D="4"><DLV A="008D" B="" C="DET-W"/>'
                    b'<DLV A="00A1" B="" C="DET-P"/></TIM>')
        description = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2">'
            '<DVC A="DVC-9"><DET A="DET-W" B="3" C="4" D="Boom" E="7" F="0"/>'
            '<DET A="DET-P" B="4" C="6" D="Section block" E="8" F="3"/></DVC>'
            '</ISO11783_TaskData>').encode()
        data = row([(0, 1), (1, 0x00000006)], position=False)
        output, report = convert(template, data, work_state=True, taskdata=description)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(rows[0]['reported_work_state'], 'ENABLED')
        self.assertEqual(rows[0]['declared_element_designator_hex'], 'hex:' + b'Boom'.hex())
        self.assertEqual(rows[0]['declared_element_number'], '7')
        self.assertEqual(rows[1]['first_child_ordinal'], '1')
        self.assertEqual((rows[1]['child_state_1'], rows[1]['child_state_2']), ('ERROR', 'ENABLED'))
        self.assertEqual(rows[1]['declared_element_object_id'], '4')
        self.assertEqual(rows[1]['declared_parent_object_id'], '3')
        self.assertEqual(rows[1]['declared_element_designator_hex'], 'hex:' + b'Section block'.hex())
        self.assertNotEqual(rows[0]['device_element_hex'], rows[1]['device_element_hex'])
        self.assertEqual(bytes.fromhex(report['device_description_hex']), description)
        if os.environ.get('MUSUBI_TELEMETRY_READER'):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'state.csv'
                target.write_text(output)
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/isoxml-rate/profile.toml'),
                    str(target), '--allow-equal-time'], check=True, capture_output=True).stdout)
            element, section = (o['fields'] for o in common['observations'])
            self.assertEqual(element['reported_work_state'], 'ENABLED')
            self.assertEqual(element['declared_element_number'], 7)
            self.assertEqual(section['child_state_1'], 'ERROR')
            self.assertEqual(section['declared_parent_object_id'], 3)
            self.assertEqual(section['declared_element_designator_hex'],
                             'hex:' + b'Section block'.hex())
            self.assertEqual(section['device_description_sha256'],
                             element['device_description_sha256'])
            units = common['profile_units']
            self.assertIn('neither_disabled_nor_a_proven_absence_of_hardware', units['child_state_1'])
            self.assertTrue(units['declared_element_designator_hex'])

    def test_work_state_reuses_tim_without_setpoint_or_missing_as_off(self):
        template = b'<TIM A="" D="4"><DLV A="008D" B="" C="DET-W"/><DLV A="00A1" B="" C="DET-P"/><DLV A="00A2" B="" C="DET-P"/><DLV A="00A3" B="" C="DET-P"/><DLV A="0122" B="" C="DET-P"/></TIM>'
        names = ("DISABLED", "ENABLED", "ERROR", "UNDEFINED_OR_NOT_INSTALLED")
        for pattern in (0xFFFFFFE4, 0x01234567):
            signed = pattern if pattern < 2**31 else pattern - 2**32
            data = row([(0, i) for i in range(4)] + [(1, signed), (2, signed), (3, -1), (4, 0), (1, signed)], position=False)
            data += row([], position=False, ms=2000)
            output, report = convert(template, data, work_state=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report['source_updates'], report['decoded_updates'], report['unsupported_updates']), (9, 8, 1))
            self.assertEqual(report['records_without_supported_update'], 1)
            self.assertEqual([r['reported_work_state'] for r in rows[:4]], list(names))
            self.assertEqual([r['first_child_ordinal'] for r in rows[4:]], ['1', '17', '33', '1'])
            for i in range(16):
                self.assertEqual(rows[4][f'child_state_{i+1}'], names[(pattern >> (i * 2)) & 3])
                self.assertEqual(rows[6][f'child_state_{i+1}'], names[3])
            self.assertTrue(all(r['actual_volume_rate_l_ha'] == '' for r in rows))
            self.assertEqual(rows[4]['source_raw_value'], str(signed))
            self.assertEqual(b''.join(bytes.fromhex(x) for x in report['source_records_hex']), data)
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); xml = base / 'tim.xml'; binary = base / 'tim.bin'; out = base / 'out'
                xml.write_bytes(template); binary.write_bytes(data)
                subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_isoxml_rate.py'),
                    str(xml), str(binary), str(out), '--work-state'], check=True, capture_output=True)
                self.assertEqual((out / 'observations.csv').read_text(), output)
                if os.environ.get('MUSUBI_TELEMETRY_READER'):
                    common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                        str(ROOT / 'profiles/declared/isoxml-rate/profile.toml'),
                        str(out / 'observations.csv'), '--allow-equal-time'], check=True, capture_output=True).stdout)
                    self.assertEqual(common['main_rows'], 8)
                    self.assertEqual(common['observations'][4]['fields']['child_state_1'], names[pattern & 3])
                    self.assertEqual(common['observations'][6]['fields']['first_child_ordinal'], 33)
                    element = common['observations'][0]['fields']
                    self.assertEqual(element['reported_work_state'], names[0])
                    self.assertEqual(element['source_ddi'], 141)
                    self.assertTrue(element['device_element_hex'].startswith('hex:'))
                    self.assertEqual(common['observations'][4]['fields']['source_ddi'], 161)
                    units = common['profile_units']
                    self.assertIn('not_measured_material_flow', units['reported_work_state'])
                    self.assertIn('not_physical_geometry', units['first_child_ordinal'])
                    for slot in range(1, 17):
                        declared = units[f'child_state_{slot}']
                        self.assertIn(f'first_child_ordinal_plus_{slot - 1}', declared)
                        self.assertIn('neither_disabled_nor_a_proven_absence_of_hardware', declared)
                        self.assertIn('not_physical_geometry', declared)
                    self.assertEqual(common['platform_domain'], 'Unknown')
                    self.assertTrue(all(o['clock_basis'] == 'Unknown' for o in common['observations']))
        for data in (row([(0, -1)], position=False), row([(0, 4)], position=False),
                     row([(4, 0)], position=False), data[:-1]):
            with self.assertRaises(ValueError): convert(template, data, work_state=True)
        with self.assertRaises(ValueError): convert(template, row([(0, 1)], position=False))

    def test_same_record_position_reuses_rates_without_fix_or_hold_inference(self):
        for extra in (False, True):
            template = (b'<TIM A="" D="4"><PTN A="" B=""' + (b' C="" D=""' if extra else b'') +
                        b'/><DLV A="0002" B="" C="DET-1"/><DLV A="0054" B="" C="DET-2"/></TIM>')
            def located_row(north, east, ms, changes):
                return (struct.pack("<IHii", ms, 10000, north, east) +
                        (struct.pack("<iB", 123456, 15) if extra else b'') + bytes([len(changes)]) +
                        b''.join(struct.pack("<Bi", i, value) for i, value in changes))
            latitude, longitude = (-35, 120) if extra else (35, -120)
            data = located_row(latitude * 10000000, longitude * 10000000, 1, [(0, 10000), (1, 2500000)])
            data += located_row(-100000000, 800000000, 2, [])
            data += located_row(2**31 - 1, 100000000, 3, [(0, 20000)])
            output, report = convert(template, data, harvest=True, located=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_records"], report["decoded_updates"], report["records_without_supported_update"]), (3, 3, 1))
            self.assertEqual([float(r["reported_latitude_deg"]) for r in rows[:2]], [latitude, latitude])
            self.assertEqual(float(rows[1]["reported_longitude_deg"]), longitude)
            self.assertEqual(float(rows[1]["reported_yield_mass_kg_m2"]), 2.5)
            self.assertEqual(rows[0]["position_status_raw"], "15" if extra else "")
            self.assertEqual(rows[0]["position_disposition"], "REPORTED_NOT_FIX_QUALIFIED")
            self.assertEqual(rows[2]["reported_latitude_deg"], "")
            self.assertEqual(rows[2]["reported_longitude_deg"], "")
            self.assertEqual(rows[2]["position_disposition"], "NOT_PROVIDED")
            with tempfile.TemporaryDirectory() as directory:
                xml = Path(directory) / "header.xml"; xml.write_bytes(template)
                binary = Path(directory) / "data.bin"; binary.write_bytes(data)
                out = Path(directory) / "out"
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_isoxml_rate.py"), str(xml),
                    str(binary), str(out), "--harvest", "--position"], check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/isoxml-rate/profile.toml"),
                        str(out / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 3)
                    self.assertEqual(common["observations"][0]["fields"]["reported_longitude_deg"], longitude)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for north, east in ((900000001, 0), (0, -1800000001)):
                with self.assertRaises(ValueError):
                    convert(template, located_row(north, east, 1, [(0, 1)]), located=True)
        with self.assertRaises(ValueError): convert(b'<TIM A="" D="4"><DLV A="0002" B="" C="D"/></TIM>', row([(0, 1)], position=False), located=True)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "shared reader required")
    def test_harvest_reuses_decoder_with_distinct_dimensions_and_average(self):
        template = b'<TIM A="" D="4"><DLV A="0054" B="" C="DET-Y"/><DLV A="0057" B="" C="DET-Y"/><DLV A="005A" B="" C="DET-Y"/><DLV A="0106" B="" C="DET-M"/><DLV A="00B7" B="" C="DET-Y"/></TIM>'
        for mass, flow, total, moisture in ((2500000, 12000000, 12345, 150000),
                                             (0, 10, 2147483647, 1000000)):
            data = row([(0, mass), (1, flow), (2, total), (3, moisture), (4, 123), (3, moisture)], position=False)
            data += row([], position=False, ms=1000)
            output, report = convert(template, data, harvest=True)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_updates"], report["decoded_updates"], report["unsupported_updates"]), (6, 5, 1))
            self.assertEqual(report["records_without_supported_update"], 1)
            self.assertEqual([int(r["source_ddi"]) for r in rows], [84, 87, 90, 262, 262])
            self.assertAlmostEqual(float(rows[0]["reported_yield_mass_kg_m2"]), mass / 1000000)
            self.assertAlmostEqual(float(rows[1]["reported_yield_mass_kg_s"]), flow / 1000000)
            self.assertEqual(int(rows[2]["reported_yield_total_mass_kg"]), total)
            self.assertEqual(float(rows[3]["reported_average_crop_moisture_fraction"]), moisture / 1000000)
            self.assertTrue(all(r["actual_mass_rate_kg_m2"] == "" for r in rows))
            self.assertEqual(b"".join(bytes.fromhex(r) for r in report["source_records_hex"]), data)
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); xml = base / "tim.xml"; binary = base / "tim.bin"; out = base / "out"
                xml.write_bytes(template); binary.write_bytes(data)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_isoxml_rate.py"),
                                str(xml), str(binary), str(out), "--harvest"], check=True, capture_output=True)
                self.assertEqual((out / "observations.csv").read_text(), output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/isoxml-rate/profile.toml"),
                    str(out / "observations.csv"), "--allow-equal-time"], capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 5)
                self.assertEqual(common["observations"][2]["fields"]["reported_yield_total_mass_kg"], total)
                self.assertEqual(common["observations"][3]["fields"]["reported_average_crop_moisture_fraction"], moisture / 1000000)
                self.assertTrue(all(o["clock_basis"] == "Unknown" and o["anchor_unix_us"] is None for o in common["observations"]))
        for value in (1000001, 2147483647):
            rows = list(csv.DictReader(io.StringIO(convert(template, row([(3, value)], position=False), harvest=True)[0])))
            self.assertEqual(rows[0]["reported_average_crop_moisture_fraction"], "")
            self.assertEqual(rows[0]["reported_average_crop_moisture_ppm"], str(value))
            self.assertEqual(rows[0]["reported_moisture_status"], "OUTSIDE_FRACTION_RANGE")
        for binary in (row([(3, -1)], position=False), data[:-1]):
            with self.assertRaises(ValueError):
                convert(template, binary, harvest=True)
        with self.assertRaises(ValueError):
            convert(template, data)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "shared reader required")
    def test_harvest_dimensions_identity_and_withheld_moisture_reach_common_output(self):
        """Every supported yield/moisture dimension, its device identity and its reason, in one run."""
        template = (b'<TIM A="" D="4"><DLV A="0054" B="" C="DET-H"/><DLV A="0057" B="" C="DET-H"/>'
                    b'<DLV A="005A" B="" C="DET-H"/><DLV A="0106" B="" C="DET-H"/></TIM>')
        description = (b'<ISO11783_TaskData VersionMajor="4" VersionMinor="2">'
                       b'<DVC A="DVC-H"><DET A="DET-H" B="7" C="6" D="Combine header" E="3" F="0"/></DVC>'
                       b'</ISO11783_TaskData>')
        mass, flow, total, moisture, withheld = 1234567, 98765, 4242, 123456, 1500000
        data = row([(0, mass), (1, flow), (2, total), (3, moisture)], position=False)
        data += row([(3, withheld)], position=False, ms=2000)
        output, report = convert(template, data, harvest=True, taskdata=description)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([int(r["source_ddi"]) for r in rows], [84, 87, 90, 262, 262])
        self.assertEqual(report["device_description_sha256"][:7], "sha256:")
        self.assertTrue(all(r["actual_volume_rate_l_ha"] == "" and r["actual_mass_rate_kg_m2"] == ""
                            and r["actual_count_per_m2"] == "" for r in rows))
        self.assertEqual(rows[4]["reported_yield_mass_kg_m2"], "")
        self.assertEqual(rows[4]["reported_yield_total_mass_kg"], "")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "tim.xml").write_bytes(template); (base / "tim.bin").write_bytes(data)
            (base / "task.xml").write_bytes(description)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_isoxml_rate.py"),
                            str(base / "tim.xml"), str(base / "tim.bin"), str(base / "out"),
                            "--harvest", "--taskdata", str(base / "task.xml")],
                           check=True, capture_output=True)
            self.assertEqual((base / "out/observations.csv").read_text(), output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/isoxml-rate/profile.toml"),
                str(base / "out/observations.csv"), "--allow-equal-time"],
                check=True, capture_output=True).stdout)
        self.assertEqual(common["main_rows"], 5)
        self.assertEqual(common["platform_domain"], "Unknown")
        fields = [observation["fields"] for observation in common["observations"]]
        self.assertAlmostEqual(fields[0]["reported_yield_mass_kg_m2"], mass / 1000000)
        self.assertAlmostEqual(fields[1]["reported_yield_mass_kg_s"], flow / 1000000)
        self.assertEqual(fields[2]["reported_yield_total_mass_kg"], total)
        self.assertEqual(fields[3]["reported_average_crop_moisture_ppm"], moisture)
        self.assertAlmostEqual(fields[3]["reported_average_crop_moisture_fraction"], moisture / 1000000)
        self.assertEqual(fields[3]["reported_moisture_status"], "REPORTED")
        self.assertEqual([field["source_raw_value"] for field in fields],
                         [mass, flow, total, moisture, withheld])
        self.assertEqual(fields[4]["reported_average_crop_moisture_ppm"], withheld)
        self.assertIsNone(fields[4]["reported_average_crop_moisture_fraction"])
        self.assertEqual(fields[4]["reported_moisture_status"], "OUTSIDE_FRACTION_RANGE")
        for field in fields:
            self.assertEqual(field["declared_device_id_hex"], "hex:" + b"DVC-H".hex())
            self.assertEqual(field["declared_element_object_id"], 7)
            self.assertEqual((field["declared_element_type"], field["declared_element_type_raw"],
                              field["declared_element_number"]), ("CONNECTOR", 6, 3))
            self.assertEqual(field["declared_element_designator_hex"],
                             "hex:" + "Combine header".encode().hex())
            self.assertEqual(field["device_description_sha256"], report["device_description_sha256"])
        self.assertTrue(all(observation["clock_basis"] == "Unknown"
                            and observation["anchor_unix_us"] is None
                            for observation in common["observations"]))
        self.assertLess(fields[0]["source_row"], fields[4]["source_row"])
        units = common["profile_units"]
        for column in ("reported_yield_mass_kg_m2", "reported_yield_mass_kg_s",
                       "reported_yield_total_mass_kg", "reported_average_crop_moisture_ppm",
                       "reported_average_crop_moisture_fraction", "reported_moisture_status",
                       "source_column", "declared_device_id_hex", "declared_element_object_id",
                       "declared_parent_object_id", "declared_element_type",
                       "declared_element_designator_hex", "declared_element_type_raw",
                       "declared_element_number", "device_description_sha256"):
            self.assertTrue(units.get(column), f"{column} must declare its meaning to the reader")
        self.assertIn("not_moisture_corrected", units["reported_yield_mass_kg_m2"])
        self.assertIn("task_average_not_instantaneous", units["reported_average_crop_moisture_ppm"])
        self.assertIn("not_an_authenticated_device", units["declared_device_id_hex"])
        self.assertIn("not_a_measured_implement_identity", units["declared_element_designator_hex"])
        with self.assertRaises(ValueError):
            convert(template, data, harvest=True,
                    taskdata=description.replace(b'A="DET-H"', b'A="DET-OTHER"'))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "shared reader required")
    def test_continuous_three_rate_output_reuses_practical_capacity(self):
        count = 80000
        template = b'<TIM A="" D="4"><DLV A="0002" B="" C="DET-V"/><DLV A="0007" B="" C="DET-M"/><DLV A="000C" B="" C="DET-C"/></TIM>'
        data = b"".join(row([(0, 10000), (1, 2000000), (2, 3000)], position=False, ms=i * 1000)
                        for i in range(count))
        output, report = convert(template, data)
        self.assertGreater(len(output.encode()), 16 * 1024 * 1024)
        self.assertEqual((report["source_records"], report["decoded_updates"]), (count, count * 3))
        self.assertEqual(b"".join(bytes.fromhex(raw) for raw in report["source_records_hex"]), data)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rates.csv"; source.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/isoxml-rate/profile.toml"),
                str(source), "--allow-equal-time"], capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], count * 3)
            for index, field, value in ((0, "actual_volume_rate_l_ha", 1.),
                                        (1, "actual_mass_rate_kg_m2", 2.),
                                        (2, "actual_count_per_m2", 3.)):
                self.assertEqual(common["observations"][index]["fields"][field], value)
                self.assertEqual(common["observations"][-3 + index]["fields"][field], value)

    def test_mass_and_count_reuse_without_setpoint_or_forward_fill(self):
        template = b'<TIM A="" D="4"><DLV A="0002" B="" C="DET-V"/><DLV A="0007" B="" C="DET-M"/><DLV A="000C" B="" C="DET-C"/><DLV A="0006" B="" C="DET-M"/><DLV A="000B" B="" C="DET-C"/></TIM>'
        for volume, mass, count in ((10000, 2000000, 3000), (0, 10, 2147483647)):
            data = row([(0, volume), (1, mass), (2, count), (3, 999), (4, 999), (1, mass)], position=False)
            output, report = convert(template, data)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([int(r["source_ddi"]) for r in rows], [2, 7, 12, 7])
            self.assertEqual(float(rows[0]["actual_volume_rate_l_ha"]), volume * .0001)
            self.assertEqual(float(rows[1]["actual_mass_rate_kg_m2"]), mass * .000001)
            self.assertEqual(float(rows[2]["actual_count_per_m2"]), count * .001)
            self.assertEqual(rows[2]["actual_mass_rate_kg_m2"], "")
            self.assertEqual((report["source_updates"], report["decoded_updates"], report["unsupported_updates"]), (6, 4, 2))
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "rates.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/isoxml-rate/profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(common["main_rows"], 4)
                    self.assertEqual(common["platform_domain"], "Unknown")
                    self.assertEqual(common["observations"][1]["fields"]["actual_mass_rate_kg_m2"], mass * .000001)
                    self.assertEqual(common["observations"][2]["fields"]["actual_count_per_m2"], count * .001)
        for column in (1, 2):
            with self.assertRaises(ValueError):
                convert(template, row([(column, -1)], position=False))
        with self.assertRaises(ValueError):
            convert(template, row([(3, 1), (4, 1)], position=False))

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

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets shared reader")
    def test_common_observation_keeps_device_occurrences_and_unknown_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "output.csv"
            source.write_text(convert(TEMPLATE, row([(0, 10000), (2, 20000)]))[0])
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/isoxml-rate/profile.toml"), str(source),
                "--allow-equal-time"], capture_output=True, check=True)
            output = json.loads(result.stdout)
            self.assertEqual((output["main_rows"], output["platform_domain"]), (2, "Unknown"))
            self.assertEqual(output["observations"][1]["fields"]["actual_volume_rate_l_ha"], 2.)
            for observation in output["observations"]:
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["anchor_unix_us"])
                self.assertNotIn("setpoint", observation["fields"])

    def test_declared_transfer_provenance_travels_without_authenticating_anything(self):
        """The root says which side wrote the file; that is a declaration, not a credential."""
        def description(attributes=""):
            return ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2"' + attributes + '>'
                    '<DVC A="DVC-1"><DET A="DET-1" B="1" C="4" D="Left" E="1" F="0"/></DVC>'
                    '<DVC A="DVC-2"><DET A="DET-2" B="1" C="4" D="Right" E="2" F="0"/></DVC>'
                    '</ISO11783_TaskData>').encode()
        data = row([(0, 10000)])
        written = (' DataTransferOrigin="1" ManagementSoftwareManufacturer="AgGateway"'
                   ' ManagementSoftwareVersion="1.0" TaskControllerManufacturer="Acme TC"'
                   ' TaskControllerVersion="3.4"')
        first = list(csv.DictReader(io.StringIO(
            convert(TEMPLATE, data, taskdata=description(written))[0])))[0]
        self.assertEqual((first["declared_transfer_origin_code"], first["declared_transfer_origin"]),
                         ("1", "FMIS"))
        self.assertEqual(bytes.fromhex(first["declared_management_software_hex"][4:]).decode(), "AgGateway")
        self.assertEqual(bytes.fromhex(first["declared_management_software_version_hex"][4:]).decode(), "1.0")
        self.assertEqual(bytes.fromhex(first["declared_task_controller_hex"][4:]).decode(), "Acme TC")
        self.assertEqual(bytes.fromhex(first["declared_task_controller_version_hex"][4:]).decode(), "3.4")
        self.assertIn("NOT_AUTHENTICATED", first["declared_transfer_provenance_basis"])
        baseline = list(csv.DictReader(io.StringIO(convert(TEMPLATE, data)[0])))[0]
        self.assertEqual(first["actual_volume_rate_l_ha"], baseline["actual_volume_rate_l_ha"])
        self.assertNotIn("declared_transfer_origin", baseline)
        absent = list(csv.DictReader(io.StringIO(convert(TEMPLATE, data, taskdata=description())[0])))[0]
        self.assertEqual([absent[key] for key in ("declared_transfer_origin_code",
                                                  "declared_transfer_origin",
                                                  "declared_management_software_hex")], ["", "", ""])
        self.assertIn("NOT_AUTHENTICATED", absent["declared_transfer_provenance_basis"])
        unknown = list(csv.DictReader(io.StringIO(convert(
            TEMPLATE, data, taskdata=description(' DataTransferOrigin="9"'))[0])))[0]
        self.assertEqual((unknown["declared_transfer_origin_code"], unknown["declared_transfer_origin"]),
                         ("9", "UNKNOWN_CODE_RETAINED"))
        with self.assertRaises(ValueError):
            convert(TEMPLATE, data, taskdata=description(' DataTransferOrigin="FMIS"'))

    def test_declared_transfer_provenance_reaches_common_output(self):
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output proof is the "
                          "point of this test, so a missing reader is an explicit skip and not a pass")
        description = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2" DataTransferOrigin="2"'
                       ' ManagementSoftwareManufacturer="Field Desk" TaskControllerVersion="7.1">'
                       '<DVC A="DVC-1"><DET A="DET-1" B="1" C="4" D="Left" E="1" F="0"/></DVC>'
                       '<DVC A="DVC-2"><DET A="DET-2" B="1" C="4" D="Right" E="2" F="0"/></DVC>'
                       '</ISO11783_TaskData>').encode()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "output.csv"
            source.write_text(convert(TEMPLATE, row([(0, 10000)]), taskdata=description)[0])
            output = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/isoxml-rate/profile.toml"), str(source),
                "--allow-equal-time"], capture_output=True, check=True).stdout)
        fields = output["observations"][0]["fields"]
        self.assertEqual(fields["declared_transfer_origin"], "MICS")
        self.assertEqual(bytes.fromhex(fields["declared_management_software_hex"][4:]).decode(), "Field Desk")
        self.assertIsNone(fields["declared_task_controller_hex"])  # absent stays absent
        self.assertIn("authenticates_nothing", output["profile_units"]["declared_transfer_origin"])

    def test_declared_geometry_resolves_through_object_references_without_a_position(self):
        """DET -> DOR -> DPT, as ISOv4Plugin 5bcf596c resolves it; a declaration, not a position."""
        description = GEOMETRY_DESCRIPTION.encode()
        data = row([(0, 10000), (1, 43000), (2, 20000)])
        plain, _ = convert(GEOMETRY_TEMPLATE, data, taskdata=description)
        output, report = convert(GEOMETRY_TEMPLATE, data, taskdata=description, section_geometry=True)
        self.assertNotIn("declared_element_offset_x_m", plain.splitlines()[0])
        rows = list(csv.DictReader(io.StringIO(output)))
        left, right = rows[0], rows[1]
        self.assertEqual(left["declared_actual_working_width_m"], "3.000")
        self.assertEqual(left["declared_default_working_width_m"], "3.200")
        self.assertEqual(left["declared_maximum_working_width_m"], "3.500")
        self.assertEqual([left["declared_element_offset_x_m"], left["declared_element_offset_y_m"],
                          left["declared_element_offset_z_m"]], ["-0.500", "1.234", "1.200"])
        self.assertEqual(left["declared_geometry_raw_mm"],
                         "0043=3000;0044=3200;0046=3500;0086=-500;0087=1234;0088=1200")
        self.assertEqual(left["declared_geometry_property_ids"],
                         "0043=10;0044=11;0046=12;0086=13;0087=14;0088=15")
        self.assertIn("NO_AXIS_SIGN_CONVENTION", left["declared_geometry_reference_basis"])
        self.assertEqual(left["declared_geometry_value_presentation"], "0088=30")
        self.assertIn("RETAINED_NOT_APPLIED", left["declared_geometry_value_presentation_disposition"])
        self.assertEqual(left["declared_referenced_property_count"], "7")
        self.assertEqual(left["declared_unselected_property_ddis"], "00A0")
        self.assertEqual(left["declared_unresolved_reference_ids"], "99")
        self.assertEqual(left["declared_referenced_process_data_count"], "1")
        self.assertEqual([right[name] for name in ("declared_actual_working_width_m",
                                                   "declared_element_offset_x_m",
                                                   "declared_geometry_raw_mm",
                                                   "declared_geometry_reference_basis")], [""] * 4)
        self.assertEqual(right["declared_geometry_value_presentation_disposition"], "NOT_DECLARED")
        self.assertEqual([r["actual_volume_rate_l_ha"] for r in rows],
                         [r["actual_volume_rate_l_ha"] for r in csv.DictReader(io.StringIO(plain))])
        self.assertEqual(report["reported_geometry_updates"], 1)
        self.assertEqual(report["unsupported_updates"], 1)
        self.assertEqual([report["declared_geometry_elements"], report["declared_geometry_properties"],
                          report["declared_unselected_properties"],
                          report["declared_unresolved_references"],
                          report["declared_geometry_value_presentations"]], [1, 6, 1, 1, 1])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "tim.xml").write_bytes(GEOMETRY_TEMPLATE); (base / "tim.bin").write_bytes(data)
            (base / "task.xml").write_bytes(description)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_isoxml_rate.py"),
                str(base / "tim.xml"), str(base / "tim.bin"), str(base / "out"),
                "--taskdata", str(base / "task.xml"), "--section-geometry"],
                check=True, capture_output=True)
            self.assertEqual((base / "out/observations.csv").read_text(), output)

    def test_declared_geometry_refuses_what_it_cannot_choose_or_resolve(self):
        data = row([(0, 10000), (2, 20000)])
        for find, replace, why in (
                ('B="0044"', 'B="0043"', "the same DDI declared twice on one element"),
                ('C="3000"', 'C="-1"', "a width below the published DDI range"),
                ('B="0046"', 'B="004G"', "a DDI that is not four hex digits"),
                ('C="1234"', 'C="3.5"', "a non-integer declared value"),
                ('<DPT A="16"', '<DPT F="1" A="16"', "an attribute the source does not declare"),
                ('<DPT A="16"', '<DPT D="' + "x" * 33 + '" A="16"', "an oversized designator"),
                ('<DOR A="10"/>', '<DOR A="10"/><DOR A="10"/>', "a repeated reference"),
                ('<DOR A="10"/>', '<DOR A="10" B="1"/>', "an attribute DOR does not have"),
                ('<DOR A="10"/>', '<DOR A="0"/>', "an object id outside the declared range"),
                ('<DOR A="10"/>', '<DPT A="21" B="0043" C="1"/>', "a child DET cannot have")):
            broken = GEOMETRY_DESCRIPTION.replace(find, replace).encode()
            self.assertNotEqual(broken, GEOMETRY_DESCRIPTION.encode(), why)
            with self.assertRaises(ValueError, msg=why):
                convert(TEMPLATE, data, taskdata=broken, section_geometry=True)
        self.assertTrue(convert(TEMPLATE, data, taskdata=GEOMETRY_DESCRIPTION.replace(
            '<DOR A="10"/>', '<DOR A="10" B="1"/>').encode())[0])
        with self.assertRaises(ValueError):  # Geometry has nothing to resolve against on its own.
            convert(TEMPLATE, data, section_geometry=True)

    def test_declared_geometry_reaches_common_output(self):
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            self.skipTest("MUSUBI_TELEMETRY_READER not configured; the common-output proof is the "
                          "point of this test, so a missing reader is an explicit skip and not a pass")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "output.csv"
            source.write_text(convert(TEMPLATE, row([(0, 10000), (2, 20000)]),
                                      taskdata=GEOMETRY_DESCRIPTION.encode(),
                                      section_geometry=True)[0])
            output = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/isoxml-rate/profile.toml"), str(source),
                "--allow-equal-time"], capture_output=True, check=True).stdout)
        self.assertEqual(output["main_rows"], 2)
        fields = output["observations"][0]["fields"]
        self.assertEqual(fields["declared_maximum_working_width_m"], 3.5)
        self.assertEqual(fields["declared_element_offset_x_m"], -0.5)
        self.assertEqual(fields["declared_unselected_property_ddis"], "00A0")
        self.assertIsNone(output["observations"][1]["fields"]["declared_element_offset_x_m"])
        units = output["profile_units"]
        self.assertIn("not_a_measured_or_verified_implement_dimension",
                      units["declared_actual_working_width_m"])
        self.assertIn("no_axis_sign_convention", units["declared_element_offset_x_m"])
        self.assertIn("never_invisible", units["declared_unselected_property_ddis"])


    def test_the_converter_names_itself_on_every_row_without_moving_anything_else(self):
        """The applied converter identity travels with the observations it produced."""
        from scripts.convert_isoxml_rate import (APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION,
                                                 prescriptions)
        data = row([(0, 10000), (2, 20000)])
        output, report = convert(TEMPLATE, data)
        header = output.splitlines()[0].split(",")
        self.assertEqual(header[-2:], ["applied_converter_id", "applied_converter_revision"])
        self.assertEqual(header[:6], ["record_time_us", "source_row", "source_update",
                                      "source_column", "device_element_hex", "source_raw_value"])
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["applied_converter_id"] == APPLIED_CONVERTER_ID for r in rows))
        self.assertTrue(all(r["applied_converter_revision"] == APPLIED_CONVERTER_REVISION
                            for r in rows))
        self.assertEqual([(r["record_time_us"], r["actual_volume_rate_l_ha"]) for r in rows],
                         [("864000001000000", "1.0"), ("864000001000000", "2.0")])
        self.assertEqual((report["source_records"], report["decoded_updates"]), (1, 2))
        self.assertEqual(report["applied_converter_id"], APPLIED_CONVERTER_ID)
        for absent in ("AUTHENTICAT", "ATTEST", "VERIFIED", "PROOF"):
            self.assertNotIn(absent, report["applied_converter_basis"].replace(
                "NOT_A_SOURCE_BYTE_OR_INSTALLED_PACKAGE_ATTESTATION", ""))
        self.assertIn("NOT_A_SIGNATURE_OR_RECEIPT", report["applied_converter_basis"])
        description = ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2"><TSK A="TSK-1" B="Plan">'
                       '<TZN A="1"><PDV A="0001" B="5000"/></TZN>'
                       '<GRD A="52.0" B="4.0" C="0.001" D="0.001" E="1" F="1" G="GRID1" I="1"/>'
                       '</TSK></ISO11783_TaskData>').encode()
        grid, report = prescriptions(description, bytes([1]), capture_time_us=7,
                                     byte_order="little-endian-pinned-decoder", grid_name="grid1")
        cell = next(csv.DictReader(io.StringIO(grid)))
        self.assertEqual((cell["applied_converter_id"], cell["applied_converter_revision"]),
                         (APPLIED_CONVERTER_ID, APPLIED_CONVERTER_REVISION))
        self.assertEqual(cell["setpoint_volume_rate_l_ha"], "0.5")  # unchanged measurement
        self.assertEqual(report["applied_converter_revision"], APPLIED_CONVERTER_REVISION)




TWO_PRODUCTS = (
    '<ISO11783_TaskData VersionMajor="4" VersionMinor="2">'
    '<PDT A="PDT1" B="Irrigation water" C="PGP1" E="0007" F="1"/>'
    '<PDT A="PDT2" B="Urea 46" F="2"/>'
    '<DVC A="DVC-1"><DET A="DET-1" B="1" C="4" D="Left" E="1" F="0"/>'
    '<DET A="DET-2" B="2" C="4" D="Right" E="2" F="0"/></DVC>'
    '<TSK A="TSK1">{allocations}</TSK></ISO11783_TaskData>')
BOUND_PAIR = '<PAN A="PDT1" E="DET-1" D="1"/><PAN A="PDT2" E="DET-2" D="1"/>'


class IsoxmlProductAllocationTests(unittest.TestCase):
    def rows(self, allocations=BOUND_PAIR, **kwargs):
        document = TWO_PRODUCTS.format(allocations=allocations).encode()
        output, report = convert(TEMPLATE, row([(0, 1), (2, 5)]), taskdata=document,
                                 product_allocation=True, **kwargs)
        return list(csv.DictReader(io.StringIO(output))), report, output

    @staticmethod
    def unhex(value):
        return "" if value == "" else bytes.fromhex(value[4:]).decode()

    def test_two_products_bind_to_their_own_elements_and_never_cross(self):
        rows, _, _ = self.rows()
        by_element = {self.unhex(r["device_element_hex"]): r for r in rows}
        self.assertEqual(set(by_element), {"DET-1", "DET-2"})
        first, second = by_element["DET-1"], by_element["DET-2"]
        self.assertEqual(self.unhex(first["declared_product_designator_hex"]), "Irrigation water")
        self.assertEqual(self.unhex(second["declared_product_designator_hex"]), "Urea 46")
        self.assertNotEqual(first["declared_product_id_hex"], second["declared_product_id_hex"])
        self.assertEqual(self.unhex(first["declared_allocation_element_ref_hex"]), "DET-1")
        self.assertEqual(self.unhex(second["declared_allocation_element_ref_hex"]), "DET-2")
        self.assertEqual((first["declared_product_type_code"],
                          second["declared_product_type_code"]), ("1", "2"))
        self.assertEqual(first["declared_product_quantity_ddi"], "0007")
        self.assertEqual(json.loads(self.unhex(first["declared_allocation_source_hex"])),
                         [["task", "TSK1"], ["allocation_index", 0], ["product_ref", "PDT1"]])
        for reported in (first, second):
            self.assertIn("NEVER_CHEMICAL_AUTHENTICATION_NOR_PROOF_OF_PHYSICAL_APPLICATION",
                          reported["product_allocation_basis"])

    def test_the_measured_quantity_is_untouched_by_the_attribution(self):
        with_product, _, _ = self.rows()
        document = TWO_PRODUCTS.format(allocations=BOUND_PAIR).encode()
        without, _ = convert(TEMPLATE, row([(0, 1), (2, 5)]), taskdata=document)
        plain = list(csv.DictReader(io.StringIO(without)))
        shared = [c for c in plain[0] if c in with_product[0]]
        self.assertIn("actual_volume_rate_l_ha", shared)
        for before, after in zip(plain, with_product):
            self.assertEqual([before[c] for c in shared], [after[c] for c in shared])

    def test_every_unsupported_allocation_scope_is_named_and_binds_nothing(self):
        cases = {
            '<PAN A="PDT9" E="DET-1"/><PAN A="PDT2" E="DET-2"/>': "NAMES_A_PRODUCT_REFERENCE_THAT_NO_PDT",
            '<PAN A="PDT1" E="DET-1"/><PAN A="PDT2" E="DET-1"/><PAN A="PDT2" E="DET-2"/>':
                "MORE_THAN_ONE_PRODUCT_ALLOCATION_NAMES_THIS_DEVICE_ELEMENT",
            '<PAN A="PDT1" E="DET-1"><ASP A="s" B="e"/></PAN><PAN A="PDT2" E="DET-2"/>':
                "CARRIES_AN_ALLOCATION_STAMP",
            '<PAN A="PDT1"/>': "DECLARES_NO_DEVICE_ELEMENT_REFERENCE",
            '': "NO_PRODUCT_ALLOCATION_IN_THE_DECLARED_TASK_NAMES_THIS_DEVICE_ELEMENT",
        }
        for allocations, expected in cases.items():
            rows, _, _ = self.rows(allocations)
            first = next(r for r in rows if self.unhex(r["device_element_hex"]) == "DET-1")
            self.assertIn(expected, first["product_allocation_basis"], expected)
            self.assertEqual(first["declared_product_designator_hex"], "", expected)
            if "NO_PDT" in expected:  # the dangling reference is still carried as reported
                self.assertEqual(self.unhex(first["declared_product_id_hex"]), "PDT9")
            else:
                self.assertEqual(first["declared_product_id_hex"], "", expected)

    def test_more_than_one_task_is_the_callers_to_name(self):
        two = TWO_PRODUCTS.replace('<TSK A="TSK1">{allocations}</TSK>',
                                   '<TSK A="TSK1">{allocations}</TSK><TSK A="TSK2"/>')
        document = two.format(allocations=BOUND_PAIR).encode()
        output, _ = convert(TEMPLATE, row([(0, 1)]), taskdata=document, product_allocation=True)
        first = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertIn("MORE_THAN_ONE_TASK", first["product_allocation_basis"])
        self.assertEqual(first["declared_product_id_hex"], "")
        named, _ = convert(TEMPLATE, row([(0, 1)]), taskdata=document,
                           product_allocation=True, declared_task="TSK1")
        self.assertEqual(self.unhex(list(csv.DictReader(io.StringIO(named)))[0]
                                    ["declared_product_designator_hex"]), "Irrigation water")
        with self.assertRaises(ValueError):
            convert(TEMPLATE, row([(0, 1)]), taskdata=document,
                    product_allocation=True, declared_task="TSK9")

    def test_the_attribution_is_read_from_the_saved_taskdata_or_not_at_all(self):
        with self.assertRaises(ValueError):
            convert(TEMPLATE, row([(0, 1)]), product_allocation=True)
        for malformed in ('<PAN E="DET-1"/>',):  # the pinned definition requires ProductIdRef
            with self.assertRaises(ValueError):
                self.rows(malformed)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-output proof needs the built shared reader")
    def test_the_attributed_rows_reach_common_output(self):
        _, _, output = self.rows()
        profile = ROOT / "profiles/declared/isoxml-rate/profile.toml"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output, encoding="utf-8")
            common = json.loads(subprocess.run(
                [os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(path),
                 "--allow-equal-time"], capture_output=True, check=True).stdout)
            self.assertEqual(common["main_rows"], 2)
            designators = {bytes.fromhex(o["fields"]["declared_product_designator_hex"][4:]).decode()
                           for o in common["observations"]}
            self.assertEqual(designators, {"Irrigation water", "Urea 46"})

    def document(self, products, allocations, task='TSK1'):
        return ('<ISO11783_TaskData VersionMajor="4" VersionMinor="2">' + products +
                '<DVC A="DVC-1"><DET A="DET-1" B="1" C="4" D="Left" E="1" F="0"/>'
                '<DET A="DET-2" B="2" C="4" D="Right" E="2" F="0"/></DVC>'
                '<TSK A="%s">%s</TSK></ISO11783_TaskData>' % (task, allocations)).encode()

    def test_an_empty_reference_is_never_matched_against_another_empty_one(self):
        """Two blanks must not resolve into a product the document never declared."""
        for products, allocations, task, why in (
                ('<PDT A="" B="Ghost"/>', '<PAN A="" E="DET-1"/>', 'TSK1', "empty PDT.A and PAN.A"),
                ('<PDT A="PDT1" B="Water"/>', '<PAN A="PDT1" E=""/>', 'TSK1', "empty PAN.E"),
                ('<PDT A="PDT1" B="Water"/>', '<PAN A="PDT1" E="DET-1"/>', '', "empty TSK.A"),
                ('<PDT A="PDT1" B="Water" C=""/>', '<PAN A="PDT1" E="DET-1"/>', 'TSK1',
                 "empty PDT.C group reference")):
            with self.assertRaises(ValueError, msg=why):
                convert(TEMPLATE, row([(0, 1)]),
                        taskdata=self.document(products, allocations, task),
                        product_allocation=True)

    def test_a_raw_code_cell_can_never_carry_a_delimiter_into_the_common_output(self):
        """The common reader refuses quoted input, so one stray comma would break the file."""
        for products, allocations, why in (
                ('<PDT A="PDT1" F="1,2"/>', '<PAN A="PDT1" E="DET-1"/>', "comma in PDT.F"),
                ('<PDT A="PDT1" F="seed"/>', '<PAN A="PDT1" E="DET-1"/>', "non-integer PDT.F"),
                ('<PDT A="PDT1" E="00,7"/>', '<PAN A="PDT1" E="DET-1"/>', "comma in PDT.E"),
                ('<PDT A="PDT1" E="7"/>', '<PAN A="PDT1" E="DET-1"/>', "PDT.E not four hex digits"),
                ('<PDT A="PDT1"/>', '<PAN A="PDT1" E="DET-1" D="1&#10;2"/>', "newline in PAN.D"),
                ('<PDT A="PDT1"/>', '<PAN A="PDT1" E="DET-1" D="x"/>', "non-integer PAN.D")):
            with self.assertRaises(ValueError, msg=why):
                convert(TEMPLATE, row([(0, 1)]), taskdata=self.document(products, allocations),
                        product_allocation=True)
        output, _ = convert(TEMPLATE, row([(0, 1)]), product_allocation=True,
                            taskdata=self.document('<PDT A="PDT1" B="Urea, 46%"/>',
                                                   '<PAN A="PDT1" E="DET-1"/>'))
        self.assertNotIn('"', output)
        first = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(self.unhex(first["declared_product_designator_hex"]), "Urea, 46%")

    def test_an_unscoped_allocation_coexisting_with_an_element_one_is_not_resolved(self):
        """Nothing in the pinned definitions ranks a task-scoped against an element-scoped one."""
        output, _ = convert(TEMPLATE, row([(0, 1), (2, 5)]), product_allocation=True,
                            taskdata=self.document(
                                '<PDT A="PDT1" B="Irrigation water"/><PDT A="PDT2" B="Urea 46"/>',
                                '<PAN A="PDT1" E="DET-1"/><PAN A="PDT2" E="DET-2"/>'
                                '<PAN A="PDT1"/>'))
        rows = list(csv.DictReader(io.StringIO(output)))
        for reported in rows:
            self.assertEqual(reported["declared_product_id_hex"], "")
            self.assertEqual(reported["declared_product_designator_hex"], "")
            self.assertIn("STATE_NO_PRECEDENCE_BETWEEN", reported["product_allocation_basis"])
            self.assertIn("UNRESOLVED_AND_NOTHING_IS_BOUND", reported["product_allocation_basis"])
            self.assertNotEqual(reported["declared_allocation_element_ref_hex"], "")
        self.assertEqual(rows[0]["source_raw_value"], "1")


if __name__ == "__main__":
    unittest.main()
