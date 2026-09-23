"""Authored CRSF records: conditional code/spec checks, not captured radio proof."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from scripts.convert_crsf_link import convert, crc8

ROOT = Path(__file__).resolve().parents[1]


def frame(payload=bytes([70, 80, 99, 251, 1, 250, 250, 90, 50, 3]), kind=0x14):
    body = bytes([kind]) + payload
    return bytes([0xC8, len(body) + 1]) + body + bytes([crc8(body)])


def capture(frames):
    return "time_us,frame_hex,unknown_hint\n" + "".join(
        f"{time},{data.hex()},keep-me\n" for time, data in frames)


class CrsfLinkTests(unittest.TestCase):
    def test_displayport_reuses_msp_with_glyph_semantics_and_capacity(self):
        def reply(payload, code=182):
            body = bytes([len(payload), code]) + payload
            checksum = 0
            for value in body: checksum ^= value
            return b'$M>' + body + bytes([checksum])
        for glyphs, bank in ((b'AB', 1), (bytes([128, 255]), 3)):
            frames = [(1, reply(bytes([op]))) for op in (0, 2)]
            frames += [(1, reply(bytes([3, 12, 29, bank | 64]) + glyphs)),
                       (1, reply(bytes([4]))), (2, reply(bytes([1]))),
                       (2, reply(bytes([6, 1, 2, 3])))]
            output, report = convert(capture(frames), 'msp-displayport-bf452')
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report['source_records'], report['decoded_records']), (6, 5))
            self.assertEqual(len(report['unsupported_records']), 1)
            self.assertEqual(rows[2]['display_glyph_bytes_hex'], 'hex:' + glyphs.hex())
            self.assertEqual(rows[2]['display_font_bank'], str(bank))
            self.assertEqual(rows[2]['display_blink_reported'], 'True')
            self.assertEqual([r['display_operation_reported'] for r in rows],
                ['HEARTBEAT_REPORTED', 'CLEAR_REPORTED', 'WRITE_GLYPHS', 'DRAW_REPORTED', 'RELEASE_REPORTED'])
            self.assertEqual(rows[0]['display_glyph_count'], '')
            self.assertEqual(json.loads(bytes.fromhex(rows[2]['source_record_hex'][4:]))['unknown_hint'], 'keep-me')
        empty, _ = convert(capture([(1, reply(bytes([3, 0, 0, 0])))]), 'msp-displayport-bf452')
        self.assertEqual(next(csv.DictReader(io.StringIO(empty)))['display_glyph_count'], '0')
        valid = reply(bytes([3, 1, 2, 0]) + b'x' * 30)
        for invalid in (reply(b''), reply(b'\x03'), reply(b'\x02\x01'),
                        reply(bytes([3, 1, 2, 128]) + b'x'), reply(bytes([3, 1, 2, 0, 0])),
                        reply(bytes([3, 1, 2, 0]) + b'x' * 31), valid[:-1],
                        valid[:-1] + bytes([valid[-1] ^ 1])):
            with self.assertRaises(ValueError): convert(capture([(1, invalid)]), 'msp-displayport-bf452')
        with self.assertRaises(ValueError): convert(capture([(1, valid)]), 'msp-attitude-bf452')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / 'input.csv'
            source.write_text(capture([(i, valid) for i in range(10000)]))
            subprocess.run([os.sys.executable, str(ROOT / 'scripts/convert_crsf_link.py'), str(source),
                str(base / 'out'), '--kind', 'msp-displayport-bf452'], check=True, capture_output=True)
            with (base / 'out/observations.csv').open() as stream:
                self.assertEqual(sum(1 for _ in csv.DictReader(stream)), 10000)
            if os.environ.get('MUSUBI_TELEMETRY_READER'):
                common = json.loads(subprocess.run([os.environ['MUSUBI_TELEMETRY_READER'],
                    str(ROOT / 'profiles/declared/crsf-link/profile.toml'),
                    str(base / 'out/observations.csv')], check=True, capture_output=True).stdout)
                self.assertEqual(common['main_rows'], 10000)
                self.assertEqual(common['observations'][-1]['fields']['display_glyph_count'], 30)
                self.assertEqual(common['observations'][-1]['clock_basis'], 'Unknown')

    def test_sport_electrical_reuses_capture_and_common_output(self):
        def packet(application, value, physical=0x1b, primitive=0x10):
            data = bytes([physical, primitive]) + struct.pack("<Hi", application, value)
            checksum = sum(data[1:])
            while checksum > 255:
                checksum = (checksum & 255) + (checksum >> 8)
            return data + bytes([255 - checksum])
        for volts, amps in ((2456, -123), (0, 456)):
            frames = [(1, packet(0x0210, volts)), (2, packet(0x020f, amps)), (3, packet(0x0400, 55))]
            output, report = convert(capture(frames), "sport-electrical-fb8622dd")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_records"], report["decoded_records"]), (3, 2))
            self.assertEqual(len(report["unsupported_records"]), 1)
            self.assertEqual(float(rows[0]["battery_voltage_v"]), volts / 100)
            self.assertEqual(float(rows[1]["battery_current_a"]), amps / 10)
            self.assertEqual(rows[0]["battery_current_a"], "")
            self.assertEqual(rows[1]["battery_voltage_v"], "")
            self.assertEqual(rows[0]["destination_address"], "")
            self.assertEqual(rows[0]["sport_physical_id_reported"], "27")
            self.assertEqual(bytes.fromhex(rows[0]["source_frame_hex"][4:]), frames[0][1])
            with tempfile.TemporaryDirectory() as tmp:
                source, destination = Path(tmp) / "input.csv", Path(tmp) / "converted"
                source.write_text(capture(frames))
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_crsf_link.py"),
                    str(source), str(destination), "--kind", "sport-electrical-fb8622dd"], check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/crsf-link/profile.toml"),
                        str(destination / "observations.csv")], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][1]["fields"]["battery_current_a"], amps / 10)
            with self.assertRaises(ValueError): convert(capture(frames), "link")
        good = packet(0x021f, 100)
        for bad in (good[:-1], good + b"\x00", good[:-1] + bytes([good[-1] ^ 1]),
                    packet(0x0210, -1), packet(0x0200, 1, primitive=0x32)):
            with self.assertRaises(ValueError): convert(capture([(1, bad)]), "sport-electrical-fb8622dd")

    def test_msp_reply_attitude_reuses_capture_and_common_output(self):
        import math
        def reply(payload, code=108, direction=b">"):
            body = bytes([len(payload), code]) + payload
            checksum = 0
            for byte in body:
                checksum ^= byte
            return b"$M" + direction + body + bytes([checksum])
        for roll, pitch, yaw in ((-123, 456, 270), (1800, -900, 360)):
            selected = reply(struct.pack("<hhH", roll, pitch, yaw))
            output, report = convert(capture([(1, reply(b"x", 1)), (1, selected)]), "msp-attitude-bf452")
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_records"], report["decoded_records"]), (2, 1))
            self.assertEqual(len(report["unsupported_records"]), 1)
            self.assertEqual(row["destination_address"], "")
            self.assertAlmostEqual(float(row["reported_roll_rad"]), math.radians(roll / 10))
            self.assertAlmostEqual(float(row["reported_pitch_rad"]), math.radians(pitch / 10))
            self.assertAlmostEqual(float(row["reported_yaw_rad"]), math.radians(yaw))
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "out.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path)], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 1)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                    self.assertAlmostEqual(common["observations"][0]["fields"]["reported_roll_rad"], math.radians(roll / 10))
            for invalid in (selected[:-1], selected[:-1] + bytes([selected[-1] ^ 1]),
                            reply(bytes(6), direction=b"<"), reply(bytes(6), direction=b"!"),
                            reply(bytes(5)), reply(struct.pack("<hhH", 1801, 0, 0)),
                            reply(struct.pack("<hhH", 0, 901, 0)), reply(struct.pack("<hhH", 0, 0, 361)),
                            frame()):
                with self.assertRaises(ValueError): convert(capture([(1, invalid)]), "msp-attitude-bf452")
            with self.assertRaises(ValueError): convert(capture([(1, selected)]), "attitude")

    def test_fixed_version_aliases_reuse_battery_values_without_false_version_inference(self):
        payload = struct.pack(">HH", 120, 23) + (456).to_bytes(3, "big") + bytes([60])
        source = capture([(1, frame(payload, 8))])
        for selection in ("pack", "cell"):
            reference, reference_report = convert(source, "battery-bf452-" + selection)
            expected = next(csv.DictReader(io.StringIO(reference)))
            for version in ("bf443", "bf450", "bf451"):
                output, report = convert(source, f"battery-{version}-{selection}")
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(row.pop("battery_wire_basis"), version.upper() + "_NONNEGATIVE_CURRENT_NO_CAPACITY_WRAP")
                self.assertEqual(row, {key: value for key, value in expected.items() if key != "battery_wire_basis"})
                self.assertEqual(report, reference_report)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "battery.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(path)], check=True, capture_output=True).stdout)
                        fields = common["observations"][0]["fields"]
                        self.assertEqual(fields["battery_voltage_v" if selection == "pack" else "battery_average_cell_voltage_v"], 12.)
                        self.assertEqual(fields["battery_current_a"], 2.3)
        with self.assertRaises(ValueError):
            convert(source, "battery-bf453-pack")

    def test_fixed_battery_writer_profiles_distinguish_pack_and_average_cell(self):
        for kind, field in (("battery-bf452-pack", "battery_voltage_v"),
                            ("battery-bf452-cell", "battery_average_cell_voltage_v")):
            for voltage, current, capacity, remaining in ((168, 123, 500, 75), (42, 0, 0x123456, 0)):
                payload = struct.pack(">HH", voltage, current) + capacity.to_bytes(3, "big") + bytes([remaining])
                output, report = convert(capture([(1, frame(payload, 8)), (1, frame())]), kind)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(float(row[field]), voltage / 10)
                self.assertEqual(float(row["battery_current_a"]), current / 10)
                self.assertEqual(float(row["battery_consumed_charge_c"]), capacity * 3.6)
                self.assertEqual(float(row["battery_remaining_reported_ratio"]), remaining / 100)
                other = "battery_voltage_v" if kind.endswith("cell") else "battery_average_cell_voltage_v"
                self.assertEqual(row[other], "")
                self.assertEqual((report["source_records"], report["decoded_records"]), (2, 1))
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "battery.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(path)], check=True, capture_output=True).stdout)
                        self.assertEqual(common["main_rows"], 1)
                        self.assertEqual(common["observations"][0]["fields"][field], voltage / 10)
            for bad in (bytes(7), bytes(9), bytes(7) + bytes([101]), b"\xff\xff" + bytes(6)):
                with self.assertRaises(ValueError):
                    convert(capture([(1, frame(bad, 8))]), kind)
        with self.assertRaises(ValueError):
            convert(capture([(1, frame(bytes(8), 8))]), "battery")

    def test_directional_reports_share_parser_without_power_code_guess(self):
        for kind, message in (("link-rx", 0x1C), ("link-tx", 0x1D)):
            for rssi, percent, quality, snr, power, fps in ((70, 80, 99, -5, 20, 15),
                                                        (0, 0, 0, -128, 0, 0)):
                payload = struct.pack("BBBbB", rssi, percent, quality, snr, power)
                if kind == "link-tx": payload += bytes([fps])
                selected = frame(payload, message)
                output, report = convert(capture([(1, selected), (1, frame())]), kind)
                row = next(csv.DictReader(io.StringIO(output)))
                self.assertEqual(float(row["reported_rssi_dbm"]), -rssi)
                self.assertEqual(float(row["reported_rssi_fraction"]), percent / 100)
                self.assertEqual(float(row["reported_lq_fraction"]), quality / 100)
                self.assertEqual(float(row["reported_snr_db"]), snr)
                self.assertEqual(float(row["reported_rf_power_dbm"]), power)
                self.assertEqual(row["reported_rf_frames_per_second"], str(fps * 10) if kind == "link-tx" else "")
                self.assertEqual((report["source_records"], report["decoded_records"]), (2, 1))
                self.assertEqual(len(report["unsupported_records"]), 1)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "output.csv"; path.write_text(output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                            str(path)], capture_output=True, check=True).stdout)
                        self.assertEqual(common["main_rows"], 1)
                        self.assertEqual(common["platform_domain"], "Unknown")
                        self.assertEqual(common["observations"][0]["fields"]["reported_lq_fraction"], quality / 100)
            for bad in (payload[:-1], payload + b"\0", bytes([70, 101]) + payload[2:],
                        payload[:2] + bytes([101]) + payload[3:]):
                with self.assertRaises(ValueError):
                    convert(capture([(1, frame(bad, message))]), kind)

    def test_attitude_selection_reuses_capture_without_sign_or_axis_loss(self):
        for values in [(10000, -20000, 0), (-31415, 31415, 1)]:
            attitude = frame(struct.pack(">hhh", *values), 0x1E)
            output, report = convert(capture([(1, attitude), (1, frame())]), "attitude")
            row = next(csv.DictReader(io.StringIO(output)))
            for name, value in zip(("pitch", "roll", "yaw"), values):
                self.assertEqual(float(row[f"reported_{name}_rad"]), value / 10000)
            self.assertEqual((report["source_records"], report["decoded_records"]), (2, 1))
            self.assertEqual(len(report["unsupported_records"]), 1)
            self.assertEqual(row["source_frame_hex"], "hex:" + attitude.hex())
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "output.csv"; path.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["reported_roll_rad"], values[1] / 10000)
                    self.assertEqual(common["platform_domain"], "Unknown")
        extended = frame(struct.pack(">hhh", 1, 2, 3) + b"future", 0x1E)
        row = next(csv.DictReader(io.StringIO(convert(capture([(1, extended)]), "attitude")[0])))
        self.assertEqual(row["source_frame_hex"], "hex:" + extended.hex())
        for payload in (b"", b"\0" * 5, struct.pack(">hhh", 32767, 0, 0)):
            with self.assertRaises(ValueError):
                convert(capture([(1, frame(payload, 0x1E))]), "attitude")
        with self.assertRaises(ValueError):
            convert(capture([(1, frame())]), "unknown")

    def test_meanings_unknown_fields_and_same_time_accounting(self):
        self.assertEqual(crc8(b"123456789"), 0xBC)
        zero = frame(bytes([0, 255, 0, 128, 8, 251, 252, 0, 0, 127]))
        output, report = convert(capture([(1, frame()), (1, zero), (2, frame(b"raw", 0x16))]))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual((len(rows), report["source_records"], report["decoded_records"]), (2, 3, 2))
        row = rows[0]
        for field, expected in {"uplink_rssi_1_dbm": -70, "uplink_rssi_2_dbm": -80,
                "uplink_lq_fraction": .99, "uplink_snr_db": -5,
                "downlink_rssi_dbm": -90, "downlink_lq_fraction": .5, "downlink_snr_db": 3}.items():
            self.assertEqual(float(row[field]), expected)
        self.assertEqual(row["rf_mode_code"], "250")
        self.assertNotIn("rf_rate_hz", row)
        self.assertEqual(row["source_frame_hex"], "hex:" + frame().hex())
        raw = json.loads(bytes.fromhex(row["source_record_hex"][4:]))
        self.assertEqual(raw["unknown_hint"], "keep-me")
        self.assertEqual(rows[1]["uplink_lq_fraction"], "0.0")
        self.assertEqual(rows[1]["active_antenna"], "UNKNOWN_8")
        self.assertEqual(report["unsupported_records"][0]["frame_hex"], frame(b"raw", 0x16).hex())

    def test_critical_failures_are_not_silent_skips(self):
        wrong_crc = bytearray(frame()); wrong_crc[-1] ^= 1
        invalid_percent = frame(bytes([70, 80, 101, 0, 0, 0, 0, 90, 100, 0]))
        for value in ["", "frame_hex,time_us\n00,1\n", "time_us,time_us,frame_hex\n1,1,00\n",
                capture([(1, frame()[:-1])]), capture([(1, bytes(wrong_crc))]),
                capture([(1, invalid_percent)]), capture([(1, frame(b"\0" * 11))]),
                capture([(2, frame()), (1, frame())]), capture([(-1, frame())]),
                capture([(2**63, frame())]), capture([(1, frame(b"raw", 0x16))]),
                capture([(1, frame())]).replace("c80c", "c8 0c")]:
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                convert(value)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_common_output_has_measurements_without_air_or_trusted_time(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observations.csv"
            source.write_text(convert(capture([(1, frame()), (1, frame())]))[0])
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/crsf-link/profile.toml"),
                str(source), "--allow-equal-time"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            output = json.loads(result.stdout)
            self.assertEqual((output["main_rows"], output["platform_domain"]), (2, "Unknown"))
            self.assertEqual([o["channel"] for o in output["observations"]],
                             ["onboard", "link_stats", "onboard", "link_stats"])
            for channel in (output["observations"][0], output["observations"][1]):
                fields = channel["fields"]
                self.assertEqual(fields["uplink_rssi_1_dbm"], -70)
                self.assertEqual(fields["uplink_rssi_2_dbm"], -80)
                self.assertEqual(fields["downlink_rssi_dbm"], -90)
                self.assertEqual(fields["uplink_lq_fraction"], .99)
                self.assertEqual(fields["downlink_lq_fraction"], .5)
                self.assertEqual(fields["uplink_snr_db"], -5)
                self.assertEqual(fields["downlink_snr_db"], 3)
                self.assertNotEqual(fields["uplink_rssi_1_dbm"], fields["downlink_rssi_dbm"])
                self.assertEqual(fields["active_antenna"], "ANTENNA_2")
                self.assertEqual((fields["rf_mode_code"], fields["tx_power_code"]), (250, 250))
            units = output["profile_units"]
            for name in ("uplink_rssi_1_dbm", "uplink_rssi_2_dbm", "uplink_lq_fraction",
                         "uplink_snr_db", "downlink_rssi_dbm", "downlink_lq_fraction",
                         "downlink_snr_db", "active_antenna", "rf_mode_code", "tx_power_code"):
                self.assertTrue(units.get(name), name)
            for refused in ("never_an_antenna_quality", "never_evidence_that_another_antenna_failed",
                            "never_the_cause_of_either_reported_uplink_RSSI",
                            "never_an_interference_obstruction_or_range_finding"):
                self.assertIn(refused, units["active_antenna"])
            onboard = output["observations"][0]["fields"]
            self.assertEqual(onboard["record_time_us"], 1)
            self.assertEqual(onboard["source_frame_hex"], "hex:" + frame().hex())
            self.assertEqual(json.loads(bytes.fromhex(onboard["source_record_hex"][4:]))
                             ["unknown_hint"], "keep-me")
            self.assertEqual(onboard["destination_address"], 200)
            for observation in output["observations"]:
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["anchor_unix_us"])
                self.assertEqual(observation["t_ms"], 0)
                self.assertIsNone(observation["t_boot_us"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_common_output_keeps_a_reported_zero_distinct_from_a_missing_value(self):
        zero = frame(bytes([0, 255, 0, 128, 8, 251, 252, 0, 0, 127]))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observations.csv"
            source.write_text(convert(capture([(1, frame()), (2, zero)]))[0])
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/crsf-link/profile.toml"),
                str(source)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            fields = json.loads(result.stdout)["observations"][3]["fields"]
            for name in ("uplink_rssi_1_dbm", "downlink_rssi_dbm", "uplink_lq_fraction",
                         "downlink_lq_fraction"):
                self.assertIn(name, fields, name)
                self.assertIsNotNone(fields[name], name)
                self.assertEqual(fields[name], 0, name)
            self.assertEqual(fields["uplink_rssi_2_dbm"], -255)
            self.assertEqual((fields["uplink_snr_db"], fields["downlink_snr_db"]), (-128, 127))
            self.assertEqual(fields["active_antenna"], "UNKNOWN_8")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_equal_time_records_are_refused_unless_the_caller_allows_them(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observations.csv"
            source.write_text(convert(capture([(1, frame()), (1, frame())]))[0])
            command = [os.environ["MUSUBI_TELEMETRY_READER"],
                       str(ROOT / "profiles/declared/crsf-link/profile.toml"),
                       str(source)]
            refused = subprocess.run(command, capture_output=True)
            self.assertNotEqual(refused.returncode, 0, "equal capture times must not pass silently")
            self.assertIn("strictly increasing", refused.stderr.decode())
            self.assertEqual(refused.stdout, b"")
            self.assertEqual(json.loads(subprocess.run(command + ["--allow-equal-time"],
                             capture_output=True, check=True).stdout)["main_rows"], 2)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "CI supplies shared reader")
    def test_directional_reports_reach_common_output_with_both_sides_distinct(self):
        seen = {}
        for kind, message in (("link-rx", 0x1C), ("link-tx", 0x1D)):
            payload = struct.pack("BBBbB", 70, 80, 99, -5, 20)
            if kind == "link-tx":
                payload += bytes([15])
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "observations.csv"
                source.write_text(convert(capture([(1, frame(payload, message))]), kind)[0])
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"], str(ROOT /
                    "profiles/declared/electrical-schema-reuse/json-profile.toml"),
                    str(source)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                common = json.loads(result.stdout)
                fields = common["observations"][0]["fields"]
            units = common["profile_units"]
            self.assertEqual(units["reported_rssi_dbm"], "dBm_reported_possible_stale_if_no_link")
            self.assertEqual(units["reported_lq_fraction"], "reported_packet_success_fraction")
            self.assertTrue(units["reported_snr_db"].startswith("dB_reported_signed"))
            self.assertTrue(units["reported_rssi_dbm"].startswith("dBm"))
            self.assertIn("percent_as_fraction", units["reported_rssi_fraction"])
            self.assertIn("not_dBm", units["reported_rssi_fraction"])
            self.assertNotEqual(units["reported_rssi_fraction"], units["reported_rssi_dbm"])
            self.assertIn("not_authenticated_publisher_identity", units["reported_link_side"])
            for name in ("reported_rssi_dbm", "reported_rssi_fraction", "reported_lq_fraction",
                         "reported_snr_db", "reported_link_side"):
                self.assertNotIn("measured", units[name], name)
                self.assertNotIn("actual", units[name], name)
            self.assertEqual(fields["reported_rssi_dbm"], -70)
            self.assertEqual(fields["reported_lq_fraction"], .99)
            self.assertEqual(fields["reported_snr_db"], -5)
            self.assertEqual(fields["reported_rssi_fraction"], .8)
            self.assertNotEqual(fields["reported_rssi_fraction"], fields["reported_rssi_dbm"])
            if kind == "link-rx":
                self.assertIsNone(fields["reported_rf_frames_per_second"])
            self.assertEqual(fields["record_time_us"], 1)
            seen[kind] = fields["reported_link_side"]
        self.assertEqual(seen, {"link-rx": "RX_REPORTED", "link-tx": "TX_REPORTED"})


if __name__ == "__main__":
    unittest.main()
