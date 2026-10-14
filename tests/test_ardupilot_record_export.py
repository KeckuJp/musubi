"""Authored binary DataFlash through the pinned decoder to common Observation.

Every input is constructed here. No real, vendor or downloaded log is used, and no
device or download is involved: the exporter only reads a file already on disk.
"""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts/export_ardupilot_records.py"
HEAD = b"\xa3\x95"
SIZES = {"Q": 8, "B": 1, "H": 2, "I": 4, "i": 4, "f": 4, "d": 8, "b": 1, "h": 2,
         "c": 2, "L": 4, "e": 4, "N": 16, "Z": 64, "M": 1, "n": 4}
# Exact writer declarations read at Rover-4.5.7 52bed8d5 and Sub-4.7.1 dbe79216.
WRITERS = {
    "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
    "ARM": ("QBIBB", "TimeUS,ArmState,ArmChecks,Forced,Method"),
    "BAT": ("QBfffffcfBBB", "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH"),
    "GPS": ("QBBIHBcLLeffffB", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,Yaw,U"),
    "VER": ("QBHBBBBIZHB", "TimeUS,BT,BST,Maj,Min,Pat,FWT,GH,FWS,APJ,BU"),
    "XKF1": ("QBff", "TimeUS,C,Roll,Pitch"),  # an unselected type, to prove accounting
}


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body_length(fmt):
    return 3 + sum(SIZES[c] for c in fmt)


def pack(fmt, values):
    out = b""
    for char, value in zip(fmt, values):
        if char in "QBHIibd":
            out += struct.pack("<" + {"Q": "Q", "B": "B", "H": "H", "I": "I",
                                      "i": "i", "b": "b", "d": "d"}[char], value)
        elif char == "f":
            out += struct.pack("<f", value)
        elif char in "cM":
            out += struct.pack("<h" if char == "c" else "<B", value)
        elif char in "Le":
            out += struct.pack("<i", value)
        elif char == "N":
            out += struct.pack("<16s", value)
        elif char == "Z":
            out += struct.pack("<64s", value)
        else:
            raise AssertionError(char)
    return out


IDS = {name: index for index, name in enumerate(WRITERS, start=140)}
# One self-describing FMT plus one FMT per writer, each 89 bytes.
PREAMBLE = 89 * (1 + len(WRITERS))


def fmt_record(type_id, length, name, fmt, labels):
    return HEAD + bytes([128]) + struct.pack(
        "<BB4s16s64s", type_id, length, name.encode(), fmt.encode(), labels.encode())


def dataflash_binary(records):
    """A minimal but real DataFlash container: FMT declarations then data messages.

    A record named `FMT` carries an explicit re-declaration, so a test can change one
    type's format mid-file without disturbing the framing.
    """
    data = fmt_record(128, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
    for name, (fmt, labels) in WRITERS.items():
        data += fmt_record(IDS[name], body_length(fmt), name, fmt, labels)
    for name, values in records:
        if name == "FMT":
            data += fmt_record(*values)
            continue
        data += HEAD + bytes([IDS[name]]) + pack(WRITERS[name][0], values)
    return data


def spans(records):
    """The byte span each record occupies, computed from the same declarations."""
    offset, out = PREAMBLE, []
    for name, values in records:
        length = 89 if name == "FMT" else body_length(WRITERS[name][0])
        out.append((name, offset, length))
        offset += length
    return out


def blueboat_records(samples=40):
    """BlueBoat layout: ArduRover firmware, surface GPS fixes present."""
    records = [("VER", (500, 9, 21, 4, 5, 7, 3, 0xABCD, b"ArduRover V4.5.7", 1054, 1)),
               ("ARM", (700, 1, 0, 0, 3))]
    for index in range(samples):
        stamp = 1_000_000 + index * 100_000
        records.append(("MODE", (stamp, 10, 10, 1)))
        records.append(("BAT", (stamp, 0, 15.8 - index * 0.01, 16.0, 3.25, 120.5 + index,
                                1.75, 2150, 0.012, 87 - index // 8, 1, 100)))
        records.append(("GPS", (stamp, 0, 3, 1000 + index, 2300, 12, 150,
                                -357000000 + index * 1000, 1491000000 + index * 1000,
                                1234 + index, 3.2, 90.0, 0.1, 0.0, 1)))
        records.append(("XKF1", (stamp, 0, 0.1, 0.2)))
    # One no-fix row, as ArduPilot writes it: Status 0 with zeroed coordinates. It is a
    # sentinel, not a position at Null Island, and nothing here may filter it silently.
    records.append(("GPS", (1_000_000 + samples * 100_000, 0, 0, 0, 0, 0, 0,
                            0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0)))
    return records


def blueboat_log(samples=40):
    return dataflash_binary(blueboat_records(samples))


def bluerov2_records(samples=24):
    """BlueROV2 layout: ArduSub firmware, deliberately no GPS records at all."""
    # FWS carries the build annotation the write site appends when GIT_VERSION is defined.
    records = [("VER", (400, 9, 21, 4, 7, 1, 3, 0x1234, b"ArduSub V4.7.1 (deadbeef)", 1054, 12)),
               ("ARM", (600, 1, 0, 0, 3))]
    for index in range(samples):
        stamp = 2_000_000 + index * 250_000
        records.append(("MODE", (stamp, 19 if index % 2 else 21, 19 if index % 2 else 21, 2)))
        records.append(("BAT", (stamp, 0, 15.2 - index * 0.02, 15.4, 8.5, 300.0 + index,
                                3.5, 1900, 0.02, 70 - index // 4, 1, 100)))
    return records


def bluerov2_log(samples=24):
    return dataflash_binary(bluerov2_records(samples))


class RecordExportTests(unittest.TestCase):
    def test_selected_jsonl_reaches_existing_common_with_untimed_and_reuse(self):
        binary = os.environ.get("MUSUBI_MAVLOG_READER")
        self.assertTrue(binary, "existing JSONL common reader is required")
        gps = blueboat_records(1)[4][1]
        self.assertEqual(blueboat_records(1)[4][0], "GPS")
        # Deliberately untimed VER plus source-qualified GPS FMT, not a claimed flight.
        blob = fmt_record(128, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")
        blob += fmt_record(140, 67, "VER", "Z", "FWS")
        fmt, labels = WRITERS["GPS"]
        blob += fmt_record(141, body_length(fmt), "GPS", fmt, labels)
        blob += HEAD + bytes([140]) + pack("Z", [b"ArduPlane V4.3.1 authored"])
        blob += HEAD + bytes([141]) + pack(fmt, gps)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.bin'
            path.write_bytes(blob)
            exports, report = self.exporter.export_records(path, ['VER', 'GPS'], output_format='jsonl')
            self.assertEqual((report['selected_timed_records'], report['selected_untimed_records']), (1, 1))
            selected = [json.loads(line) for line in exports['selected'].splitlines()]
            self.assertEqual([r['meta']['type'] for r in selected], ['VER', 'GPS'])
            for record in selected:
                offset, length = record['source_offset'], record['source_length']
                self.assertEqual(bytes.fromhex(record['source_record_hex']), blob[offset:offset + length])
            target = Path(tmp) / 'selected.jsonl'
            target.write_text(exports['selected'])
            common = json.loads(subprocess.run([binary,
                str(ROOT / 'profiles/declared/arduplane-jsonl/profile.toml'), str(target)],
                check=True, capture_output=True).stdout)
            self.assertEqual((common['decoded_observations'], common['untimed_records']), (1, 1))
            self.assertEqual(len(common['source_records']), 2)
            self.assertEqual(common['platform_domain'], 'Unknown')
            fields = common['observations'][0]['fields']
            self.assertAlmostEqual(fields['GPS.Lat'], -35.7)
            self.assertAlmostEqual(fields['GPS.Alt'], 12.34)
            self.assertEqual(fields['GPS.Yaw'], 0.0)
            self.assertEqual(common['observations'][0]['t_boot_us'], gps[0])
            self.assertEqual(common['profile_units']['GPS.Alt'], 'm')
            self.assertEqual(report['byte_coverage']['undecoded_bytes'], 0)
            self.assertEqual(report['unselected_counts']['FMT'], 3)
            destination = Path(tmp) / 'jsonl-export'
            subprocess.run([os.sys.executable, str(EXPORT), str(path), str(destination),
                '--types', 'VER,GPS', '--output-format', 'jsonl'], check=True, capture_output=True)
            self.assertEqual((destination / 'selected.jsonl').read_text(), exports['selected'])
            self.assertFalse((destination / 'GPS.csv').exists())
            nonfinite = list(gps)
            nonfinite[10] = float('nan')  # Spd: preserve missing scalar as labelled text, not zero.
            path.write_bytes(blob[:-body_length(fmt)] + HEAD + bytes([141]) + pack(fmt, nonfinite))
            missing, _ = self.exporter.export_records(path, ['GPS'], output_format='jsonl')
            missing_record = json.loads(missing['selected'])
            self.assertEqual(missing_record['data']['Spd'], 'NaN')
            self.assertEqual(missing_record['meta']['nonfinite_fields'], ['Spd'])
            # Different existing authored layout, all five supported types in source order.
            path.write_bytes(bluerov2_log(2))
            plain, plain_report = self.exporter.export_records(path, self.exporter.SUPPORTED)
            other, other_report = self.exporter.export_records(path, self.exporter.SUPPORTED, output_format='jsonl')
            target.write_text(other['selected'])
            reused = json.loads(subprocess.run([binary,
                str(ROOT / 'profiles/declared/arduplane-jsonl/profile.toml'), str(target)],
                check=True, capture_output=True).stdout)
            self.assertEqual(reused['decoded_observations'], sum(other_report['exported_counts'].values()))
            self.assertEqual(reused['platform_domain'], 'Unknown')
            self.assertEqual(other_report['byte_coverage'], plain_report['byte_coverage'])
            self.assertEqual(self.exporter.export_records(path, self.exporter.SUPPORTED), (plain, plain_report))
            bad_fmt = fmt_record(141, body_length(fmt), 'GPS', fmt.replace('L', 'i'), labels)
            with self.assertRaises(ValueError):
                self.export(path.read_bytes()[:-2], tmp, ['GPS'])
            # A valid frame but wrong GPS scaling declaration must not be silently labelled degrees.
            path.write_bytes(blob.replace(fmt_record(141, body_length(fmt), 'GPS', fmt, labels), bad_fmt))
            with self.assertRaisesRegex(ValueError, 'qualified'):
                self.exporter.export_records(path, ['GPS'], output_format='jsonl')
            duplicate = fmt_record(128, 89, 'FMT', 'BBnNZ', 'Type,Length,Name,Format,Columns')
            duplicate += fmt_record(140, 131, 'VER', 'ZZ', 'FWS,FWS')
            duplicate += HEAD + bytes([140]) + pack('ZZ', [b'first', b'second'])
            path.write_bytes(duplicate)
            with self.assertRaisesRegex(ValueError, 'unique'):
                self.exporter.export_records(path, ['VER'], output_format='jsonl')

    @classmethod
    def setUpClass(cls):
        cls.exporter = load(EXPORT)
        cls.mode = load(ROOT / "scripts/convert_ardupilot_mode_csv.py")
        cls.battery = load(ROOT / "scripts/convert_ardupilot_battery_csv.py")

    def export(self, blob, directory, types):
        path = Path(directory) / "log.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return self.exporter.export_records(path, types)

    def reader(self, text, directory, name, profile):
        path = Path(directory) / f"{name}.csv"
        self.exporter.write(path, text)
        if not os.environ.get("MUSUBI_TELEMETRY_READER"):
            return None
        return json.loads(subprocess.run(
            [os.environ["MUSUBI_TELEMETRY_READER"], str(profile), str(path), "--allow-equal-time"],
            check=True, capture_output=True).stdout)

    def ingest(self, csv_text, directory, mapping):
        """Navigation reaches common through the adopted recorded-CSV adapter SPI.

        That SPI carries the flat source columns into `data`, which is where its existing
        `fix_field`/`min_fix` policy looks. The JSONL route is deliberately not used for
        GPS: `convert_position_csv.py` puts `Status` inside the unsealed `source_record`
        sidecar, where the same policy cannot see it, so a no-fix row would be published
        as an ordinary position.
        """
        path = Path(directory) / "gps-ingest.csv"
        self.exporter.write(path, csv_text)
        if not os.environ.get("MUSUBI_RECORDED_CSV_INGEST"):
            return None
        # A rejected record makes this exit non-zero by design, so do not check=True.
        result = subprocess.run(
            [os.environ["MUSUBI_RECORDED_CSV_INGEST"], str(path), str(mapping),
             "marine-authored-blueboat", "1757900000000", "TimeUS", "--allow-equal-time"],
            capture_output=True)
        return json.loads(result.stdout)

    def test_blueboat_binary_reaches_mode_electrical_and_navigation(self):
        marine = ROOT / "profiles/declared/marine-ardu-reuse/profile.toml"
        with tempfile.TemporaryDirectory() as directory:
            exports, report = self.export(blueboat_log(), directory,
                                          ["MODE", "ARM", "VER", "BAT", "GPS"])
            self.assertEqual(report["decoder"], "pymavlink 2.4.49 DFReader_binary")
            self.assertEqual(report["absent_selected_types"], [])
            self.assertEqual(report["exported_counts"],
                             {"ARM": 1, "BAT": 40, "GPS": 41, "MODE": 40, "VER": 1})
            # Every message is accounted for, including the type we did not select.
            self.assertEqual(report["unselected_counts"], {"FMT": 7, "XKF1": 40})
            self.assertEqual(report["total_messages"], sum(report["message_counts"].values()))

            # Scaling is the decoder's, applied once: L gives 1e-7 and e gives 1e-2.
            gps = list(csv.DictReader(io.StringIO(exports["GPS"])))
            self.assertEqual(gps[0]["Lat"], "-35.7")
            self.assertEqual(gps[0]["Lng"], "149.1")
            self.assertEqual(gps[0]["Alt"], "12.34")
            self.assertEqual(gps[0]["HDop"], "1.5")
            self.assertEqual(gps[0]["TimeUS"], "1000000")  # Q stays native microseconds

            # Reported mode through the adopted converter and the common reader.
            modes = self.mode.convert(exports["MODE"], "rover-4.5.7", "mode")
            rows = list(csv.DictReader(io.StringIO(modes)))
            self.assertEqual(len(rows), 40)
            self.assertTrue(all(row["reported_mode_name"] == "AUTO" for row in rows))
            common = self.reader(modes, directory, "mode", marine)
            if common:
                self.assertEqual(common["main_rows"], 40)
                self.assertEqual(common["platform_domain"], "Unknown")

            # Reported identity, cross-checked against the declaration.
            identity = self.mode.convert(exports["VER"], "rover-4.5.7", "ver")
            record = next(csv.DictReader(io.StringIO(identity)))
            self.assertEqual(record["reported_firmware_family"], "ArduRover")
            self.assertEqual(record["declared_vehicle_agreement"], "AGREES_WITH_DECLARED_RELEASE")
            self.assertEqual(record["FWS"], "ArduRover V4.5.7")

            # Electrical through the instance selector and the common reader.
            electrical = self.battery.convert(exports["BAT"], "ardupilot-bat-inst")
            battery_rows = list(csv.DictReader(io.StringIO(electrical)))
            self.assertEqual(len(battery_rows), 40)
            # BAT.Volt is a float32 at the write site, so the exported decimal is the exact
            # value of that float32 and not a prettier 15.8. Passing it through unrounded is
            # the point: 3.25 is exact in float32 and stays short on its own.
            self.assertEqual(battery_rows[0]["battery_voltage_v"],
                             repr(struct.unpack("<f", struct.pack("<f", 15.8))[0]))
            self.assertEqual(battery_rows[0]["battery_current_a"], "3.25")
            common = self.reader(electrical, directory, "bat",
                                 ROOT / "profiles/declared/ardupilot-battery/profile.toml")
            if common:
                self.assertEqual(common["main_rows"], 40)
                self.assertAlmostEqual(
                    common["observations"][0]["fields"]["battery_voltage_v"], 15.8, places=5)

            # Navigation straight into the adopted recorded-CSV SPI, under the mapping's
            # own fix policy. `Status` is a flat column there, so the policy can read it.
            spi = self.ingest(exports["GPS"], directory, ROOT / "profiles/declared"
                              "/marine-ardu-reuse/gps-fix-mapping.json")
            if spi:
                # Every row is accounted for as exactly one of mapped or rejected.
                self.assertEqual(spi["source_record_count"], 41)
                self.assertEqual(len(spi["mapped"]) + len(spi["rejected"]), 41)
                self.assertEqual(len(spi["mapped"]), 40)
                first = spi["mapped"][0]
                self.assertEqual(first["position"],
                                 {"lat_deg": -35.7, "lon_deg": 149.1, "alt_m": 12.34})
                # Domain: no marine member exists, so it stays Unknown rather than Ground.
                self.assertEqual(first["domain"], "Unknown")
                # Time: a boot counter is not an epoch, so the record is time-suspect and
                # observed_at stays null rather than being invented.
                self.assertEqual(first["boot_us"], 1_000_000)
                self.assertIsNone(first["observed_at"])
                self.assertEqual(first["reason_code"], "time-suspect")
                self.assertEqual(first["mark"], "Degraded")
                self.assertIn("timestamp:boot-counter-not-epoch", first["provenance"])

                # The no-fix row is the negative, asserted in the common output itself: it
                # is the last record, it is INVALID for want of a fix, and it publishes no
                # position at all. It is not a Degraded ordinary position, and its absence
                # from `mapped` is what stops a zeroed sentinel becoming a coordinate.
                self.assertEqual(len(spi["rejected"]), 1)
                nofix = spi["rejected"][0]
                self.assertEqual(nofix["record_index"], 40)
                self.assertEqual(nofix["mark"], "Invalid")
                self.assertEqual(nofix["reason_code"], "insufficient-fix")
                self.assertNotIn("position", nofix)
                self.assertNotIn(40, [record["record_index"] for record in spi["mapped"]])
                self.assertTrue(all(record["mark"] == "Degraded" for record in spi["mapped"]))
                # Retained, not deleted: the whole source CSV including that row is kept
                # beside the observations, and it is not sealed.
                self.assertIn("5000000,0,0,0,0,0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0",
                              spi["source_csv"])
                self.assertIs(spi["source_records_sealed"], False)

    def test_bluerov2_binary_has_no_gps_and_absence_stays_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            exports, report = self.export(bluerov2_log(), directory,
                                          ["MODE", "ARM", "VER", "BAT", "GPS"])
            # A submersible log with no GPS: absent, never a zero row and never a depth.
            self.assertEqual(report["absent_selected_types"], ["GPS"])
            self.assertNotIn("GPS", exports)
            self.assertNotIn("GPS", report["exported_counts"])
            self.assertEqual(report["exported_counts"], {"ARM": 1, "BAT": 24, "MODE": 24, "VER": 1})

            modes = self.mode.convert(exports["MODE"], "sub-4.7.1", "mode")
            rows = list(csv.DictReader(io.StringIO(modes)))
            self.assertEqual({row["reported_mode_name"] for row in rows}, {"MANUAL", "SURFTRAK"})
            identity = next(csv.DictReader(io.StringIO(
                self.mode.convert(exports["VER"], "sub-4.7.1", "ver"))))
            self.assertEqual(identity["reported_firmware_family"], "ArduSub")
            self.assertEqual(identity["declared_vehicle_agreement"],
                             "AGREES_WITH_DECLARED_RELEASE_BUILD_COMMIT_NOT_CHECKED")
            self.assertEqual(identity["reported_firmware_build_annotation"], "deadbeef")
            # BU carries fwver.vehicle_type at the write site: an APM_BUILD_DIRECTORY code,
            # preserved lexically and deliberately not decoded into a vehicle or a product.
            self.assertEqual(identity["BU"], "12")
            electrical = self.battery.convert(exports["BAT"], "ardupilot-bat-inst")
            self.assertEqual(len(list(csv.DictReader(io.StringIO(electrical)))), 24)
            # The rover table would give a different meaning to the same numbers.
            self.assertEqual(self.mode.MODE_SCHEMAS["rover-4.5.7"].get(19), None)

    def test_provenance_is_file_association_and_refusals_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            exports, report = self.export(blueboat_log(4), directory, ["MODE", "VER"])
            self.assertIn("not hardware authentication", report["association_basis"])
            # Every export from one file shares that file's digest; a separately supplied
            # CSV is not covered by it.
            _, second = self.export(bluerov2_log(4), Path(directory) / "b", ["MODE"])
            self.assertNotEqual(report["source_sha256"], second["source_sha256"])
            # A declared vehicle contradicting the logged firmware is still refused.
            with self.assertRaises(ValueError):
                self.mode.convert(exports["VER"], "sub-4.7.1", "ver")
            with self.assertRaises(ValueError):
                self.exporter.export_records(Path(directory) / "log.bin", ["MODE", "GPSX"])
            with self.assertRaises(ValueError):
                self.exporter.export_records(Path(directory) / "log.bin", [])

    def test_unselected_records_are_retained_by_offset_with_their_schema(self):
        """Counting an unselected record is not keeping it; keep where and under what."""
        records = blueboat_records(3)
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.export(dataflash_binary(records), directory, ["GPS"])
            retained = report["retained_unselected"]["XKF1"]
            expected = [offset for name, offset, _ in spans(records) if name == "XKF1"]
            self.assertEqual(retained["offsets"], expected)
            self.assertEqual(retained["count"], 3)
            self.assertEqual(retained["offsets_retained"], "ALL")
            self.assertEqual(report["unretained_unselected_records"], 0)
            # The schema it was parsed under, not just its labels: format and multipliers.
            schema = report["schemas"][retained["schema_ref"]]
            self.assertEqual(schema["format"], "QBff")
            self.assertEqual(schema["columns"], ["TimeUS", "C", "Roll", "Pitch"])
            # An unscaled format character carries no multiplier at all, and that is
            # recorded as such rather than as a 1 we invented.
            self.assertEqual(schema["multipliers"],
                             {"TimeUS": None, "C": None, "Roll": None, "Pitch": None})
            # GPS is selected, and its scaling basis is recorded as the decoder applied it.
            self.assertEqual(report["schemas"]["GPS"]["multipliers"]["Lat"], 1.0e-7)
            self.assertEqual(report["schemas"]["GPS"]["multipliers"]["Alt"], 1.0e-2)
            self.assertIsNone(report["schemas"]["GPS"]["multipliers"]["TimeUS"])
            self.assertIn("VERIFIED_EQUAL_BEFORE_DECODING", report["digest_basis"])

    def test_undecoded_and_truncated_spans_refuse_a_clean_export(self):
        """recv_msg() resyncs and stops; neither may be reported as a complete export."""
        records = blueboat_records(4)
        blob = dataflash_binary(records)
        name, offset, length = next(item for item in spans(records) if item[0] == "MODE")
        with tempfile.TemporaryDirectory() as directory:
            # Damage one record's header. The decoder steps over it byte by byte to the
            # next signature, so exactly that record's span is undecoded.
            damaged = blob[:offset] + b"\x00" * 3 + blob[offset + 3:]
            with self.assertRaises(self.exporter.UndecodedSpans) as caught:
                self.export(damaged, directory, ["MODE", "GPS"])
            report = caught.exception.report
            self.assertEqual(report["framing"], self.exporter.INCOMPLETE)
            self.assertEqual(report["byte_coverage"]["undecoded_spans"],
                             [{"offset": offset, "bytes": length,
                               "disposition": "UNDECODED_SPAN_SKIPPED_BY_THE_DECODER"}])
            self.assertEqual(report["byte_coverage"]["undecoded_bytes"], length)
            self.assertEqual(report["byte_coverage"]["decoded_bytes"], len(blob) - length)
            # The message balance still "balances" -- which is exactly why it is not proof.
            self.assertEqual(report["exported_counts"]["MODE"], 3)
            self.assertEqual(sum(report["exported_counts"].values())
                             + sum(report["unselected_counts"].values()),
                             report["total_messages"])

            # A tail cut mid-record leaves bytes the decoder never frames.
            cut = 7
            with self.assertRaises(self.exporter.UndecodedSpans) as caught:
                self.export(blob[:-cut], Path(directory) / "t", ["MODE", "GPS"])
            trailing = caught.exception.report["byte_coverage"]["undecoded_spans"]
            last = spans(records)[-1]
            self.assertEqual(trailing, [{"offset": last[1], "bytes": last[2] - cut,
                                         "disposition": "TRAILING_BYTES_THE_DECODER_DID_NOT_FRAME"}])
            self.assertEqual(caught.exception.report["exported_counts"]["GPS"], 4)

            # An intact file is the control: every byte is a declared record.
            _, clean = self.export(blob, Path(directory) / "c", ["MODE", "GPS"])
            self.assertEqual(clean["framing"], self.exporter.COMPLETE)
            self.assertEqual(clean["byte_coverage"]["undecoded_spans"], [])
            self.assertEqual(clean["byte_coverage"]["decoded_bytes"], len(blob))

    def test_same_labels_under_a_new_format_are_refused_not_mixed(self):
        """A mid-file FMT change keeping the labels would silently mix two meanings."""
        records = blueboat_records(3)
        # BAT.Temp is `c` (int16 x 0.01). Re-declare it as `h` (int16 x 1): same labels,
        # same length, same framing -- a different meaning for the same column.
        drifted = WRITERS["BAT"][0].replace("c", "h")
        self.assertEqual(body_length(drifted), body_length(WRITERS["BAT"][0]))
        records.insert(6, ("FMT", (IDS["BAT"], body_length(drifted), "BAT",
                                   drifted, WRITERS["BAT"][1])))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as caught:
                self.export(dataflash_binary(records), directory, ["BAT"])
            self.assertIn("FMT declaration", str(caught.exception))
            # The unchanged file is accepted, so the refusal is the drift and nothing else.
            _, report = self.export(dataflash_binary(blueboat_records(3)),
                                    Path(directory) / "ok", ["BAT"])
            self.assertEqual(report["schemas"]["BAT"]["format"], WRITERS["BAT"][0])

    def test_selecting_types_the_file_does_not_contain_exports_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            exports, report = self.export(bluerov2_log(3), directory, ["GPS"])
            self.assertEqual(exports, {})
            self.assertEqual(report["absent_selected_types"], ["GPS"])
            self.assertEqual(report["exported_counts"], {})
            # Nothing exported is still fully accounted: every byte, every message.
            self.assertEqual(sum(report["unselected_counts"].values()),
                             report["total_messages"])
            self.assertEqual(report["framing"], self.exporter.COMPLETE)
            target = Path(directory) / "out"
            result = subprocess.run(
                [os.sys.executable, str(EXPORT), str(Path(directory) / "log.bin"),
                 str(target), "--types", "GPS"], check=True, capture_output=True)
            self.assertEqual([p.name for p in target.iterdir()], ["report.json"])
            self.assertEqual(json.loads(result.stdout)["absent_selected_types"], ["GPS"])
            # An empty selection is a caller error, not an empty success.
            for argument in ("", ",", "MODE,,BAT"):
                self.assertNotEqual(subprocess.run(
                    [os.sys.executable, str(EXPORT), str(Path(directory) / "log.bin"),
                     str(Path(directory) / "x"), "--types", argument],
                    capture_output=True).returncode, 0)

    def test_command_line_reports_undecoded_spans_and_writes_nothing(self):
        records = blueboat_records(3)
        blob = dataflash_binary(records)
        _, offset, length = next(item for item in spans(records) if item[0] == "MODE")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "log.bin"
            source.write_bytes(blob[:offset] + b"\x00" * 3 + blob[offset + 3:])
            target = Path(directory) / "out"
            result = subprocess.run(
                [os.sys.executable, str(EXPORT), str(source), str(target), "--types", "MODE"],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, "")
            self.assertFalse(target.exists())
            reported = json.loads(result.stderr[:result.stderr.rindex("}") + 1])
            self.assertEqual(reported["byte_coverage"]["undecoded_bytes"], length)
            self.assertEqual(reported["framing"], self.exporter.INCOMPLETE)

    def test_command_line_writes_one_csv_per_type(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "log.bin"
            source.write_bytes(bluerov2_log(6))
            target = Path(directory) / "out"
            result = subprocess.run(
                [os.sys.executable, str(EXPORT), str(source), str(target), "--types", "MODE,BAT,GPS"],
                check=True, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["absent_selected_types"], ["GPS"])
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             ["BAT.csv", "MODE.csv", "report.json"])
            failed = subprocess.run(
                [os.sys.executable, str(EXPORT), str(source), str(target), "--types", "MODE"],
                capture_output=True)
            self.assertNotEqual(failed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
