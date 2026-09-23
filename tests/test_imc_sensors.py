"""Authored IMC packets; finite spec checks, not real marine recordings."""
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from scripts.convert_imc_sensors import (LIMIT, NODE_REPORT_BASIS, NODE_REPORT_FORMAT, SENSORS,
                                         convert, convert_input, convert_alog, convert_node_report,
                                         convert_slog, crc16, node_report_values)

ROOT = Path(__file__).resolve().parents[1]


def frame(message, value, order="<", timestamp=1700000000.25):
    payload = struct.pack(order + SENSORS[message][0], value) if message in SENSORS else bytes(value)
    raw = struct.pack(order + "HHHdHBHB", 0xFE54, message, len(payload), timestamp, 42, 3, 65535, 255) + payload
    return raw + struct.pack(order + "H", crc16(raw))


def stream(order="<"):
    values = {263: 20, 264: 1013.25, 265: 7.5, 267: 1500, 268: 1025, 270: 35}
    return b"".join(frame(key, value, order) for key, value in values.items()) + frame(65000, b"unmapped", order)


class ImcSensorTests(unittest.TestCase):
    def test_neptus_schema_reuses_five_sensors_without_guessing_salinity(self):
        schema = "neptus-2019.10.1-sensors"
        for order, temperature in (("<", 12.), (">", 31.)):
            raw = frame(263, temperature, order) + stream(order)
            saved = gzip.compress(raw, mtime=0)
            output, report = convert_input(saved, schema=schema)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_frames"], report["decoded_frames"]), (8, 6))
            self.assertEqual([x["message_id"] for x in report["unsupported_frames"]], [270, 65000])
            self.assertTrue(all(not row["salinity_psu"] for row in rows))
            self.assertAlmostEqual(float(rows[0]["temperature_k"]), temperature + 273.15)
            self.assertEqual(report["selected_schema"], schema)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "Data.lsf.gz"
                source.write_bytes(saved)
                destination = Path(directory) / "converted"
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_imc_sensors.py"),
                    str(source), str(destination), "--source-format", schema], check=True)
                self.assertEqual((destination / "observations.csv").read_text(), output)
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                    str(destination / "observations.csv"), "--allow-equal-time"],
                    capture_output=True, check=True)
                common = json.loads(result.stdout)
                self.assertEqual(common["main_rows"], 6)
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertAlmostEqual(common["observations"][0]["fields"]["temperature_k"], temperature + 273.15)
        with self.assertRaises(ValueError):
            convert_input(raw, schema="neptus-unqualified")
        with self.assertRaises(ValueError):
            convert_input(raw[:-1], schema=schema)
        with self.assertRaises(ValueError):
            convert_input(frame(270, 35), schema=schema)

    def test_saved_icmp_echo_round_trip_reaches_common_output_without_claiming_a_network(self):
        import importlib.util
        if importlib.util.find_spec("dpkt") is None:
            self.skipTest("optional pinned PCAP decoder is exercised in the external-decoder CI environment")
        import dpkt
        from scripts.convert_imc_sensors import convert_icmp_echo

        def echo(kind, ident, seq, payload):
            body = struct.pack(">BBHHH", kind, 0, 0, ident, seq) + payload
            return body[:2] + struct.pack(">H", dpkt.in_cksum(body)) + body[4:]

        def packet(src, dst, message, protocol=1):
            total = 20 + len(message)
            header = struct.pack(">BBHHHBBH", 0x45, 0, total, 1, 0, 64, protocol, 0)
            header += bytes(int(p) for p in src.split(".")) + bytes(int(p) for p in dst.split("."))
            header = header[:10] + struct.pack(">H", dpkt.in_cksum(header)) + header[12:]
            return b"\xff" * 6 + b"\x11" * 6 + b"\x08\x00" + header + message

        def capture(frames):
            out = struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
            for micros, raw in frames:
                out += struct.pack("<IIII", micros // 1000000, micros % 1000000,
                                   len(raw), len(raw)) + raw
            return out

        for src, dst, ident, seq, payload, sent, back in (
                ("10.0.0.1", "10.0.0.2", 0x1234, 1, b"first-payload", 1_000_000, 1_002_500),
                ("192.168.5.9", "192.168.5.1", 0x0000, 65535, b"second-*-payload", 7, 9)):
            data = capture([(sent, packet(src, dst, echo(8, ident, seq, payload))),
                            (back, packet(dst, src, echo(0, ident, seq, payload)))])
            output, report = convert_icmp_echo(data)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(row["icmp_pair_status"], "PAIRED")
            self.assertEqual(int(row["icmp_rtt_us"]), back - sent)
            self.assertAlmostEqual(float(row["icmp_rtt_s"]), (back - sent) / 1000000)
            self.assertEqual((row["icmp_source_ip"], row["icmp_destination_ip"]), (src, dst))
            self.assertEqual((int(row["icmp_identifier"]), int(row["icmp_sequence"])), (ident, seq))
            self.assertEqual((row["icmp_request_packet_index"], row["icmp_reply_packet_index"]),
                             ("0", "1"))
            self.assertEqual((int(row["icmp_request_capture_us"]), int(row["icmp_reply_capture_us"])),
                             (sent, back))
            self.assertEqual(int(row["icmp_payload_bytes"]), len(payload))
            self.assertEqual(report["paired_exchanges"], 1)
            self.assertEqual(report["capture_resolution_us"], 1)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / "icmp.csv"
                    target.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                        str(target), "--allow-equal-time"], check=True,
                        capture_output=True).stdout)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["icmp_rtt_us"], back - sent)
                self.assertEqual(fields["icmp_pair_status"], "PAIRED")
                self.assertEqual(fields["icmp_source_ip"], src)
                self.assertEqual(fields["icmp_identifier"], ident)
                units = common["profile_units"]
                self.assertIn("including_whatever_the_remote_host_took_to_answer",
                              units["icmp_rtt_us"])
                self.assertIn("not_one_way_delay", units["icmp_rtt_us"])
                self.assertIn("not_proven_packet_loss", units["icmp_pair_status"])
                self.assertIn("never_the_whole_pair_key", units["icmp_identifier"])

        base = b"payload"
        request = packet("10.0.0.1", "10.0.0.2", echo(8, 7, 3, base))
        reply = packet("10.0.0.2", "10.0.0.1", echo(0, 7, 3, base))
        poisoned_capture = capture([(1, request), (2, request), (3, reply),
                                    (4, request), (5, reply)])
        text, ambiguous = convert_icmp_echo(poisoned_capture)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(ambiguous["paired_exchanges"], 0)
        self.assertEqual(ambiguous["ambiguous_exchanges"], 5)
        self.assertTrue(all(r["icmp_pair_status"] == "AMBIGUOUS_DUPLICATE_KEY" for r in rows))
        self.assertTrue(all(r["icmp_rtt_us"] == "" for r in rows))
        referenced = set()
        for row in rows:
            for column in ("icmp_request_packet_index", "icmp_reply_packet_index"):
                if row[column] != "":
                    referenced.add(int(row[column]))
        self.assertEqual(referenced, set(range(5)))
        self.assertEqual(ambiguous["accounted_selected_packets"], ambiguous["selected_packets"])
        with self.assertRaises(ValueError):
            convert_icmp_echo(capture([(1, request), (2, reply), (3, request), (4, reply)]))
        alone, missing = convert_icmp_echo(capture([(1, request)]))
        self.assertEqual(missing["unmatched_requests"], 1)
        lonely = next(csv.DictReader(io.StringIO(alone)))
        self.assertEqual(lonely["icmp_pair_status"], "NO_REPLY_IN_CAPTURE")
        self.assertEqual((lonely["icmp_rtt_us"], lonely["icmp_reply_packet_index"]), ("", ""))
        _, mismatch = convert_icmp_echo(capture([
            (1, request), (2, packet("10.0.0.2", "10.0.0.1", echo(0, 7, 3, b"altered")))]))
        self.assertEqual((mismatch["returned_data_mismatches"], mismatch["paired_exchanges"]), (1, 0))
        _, orphan = convert_icmp_echo(capture([(1, reply)]))
        self.assertEqual(orphan["replies_without_request"], 1)
        _, wrong = convert_icmp_echo(capture([
            (1, request), (2, packet("10.0.0.3", "10.0.0.1", echo(0, 7, 3, base)))]))
        self.assertEqual((wrong["paired_exchanges"], wrong["unmatched_requests"]), (0, 1))
        self.assertEqual(wrong["replies_without_request"], 1)
        _, mixed = convert_icmp_echo(capture([
            (1, request), (2, reply),
            (3, packet("10.0.0.1", "10.0.0.2", b"\x00" * 12, protocol=17)),
            (4, packet("10.0.0.1", "10.0.0.2", echo(3, 0, 0, b"unreachable")))]))
        self.assertEqual((mixed["paired_exchanges"], mixed["unselected_packets"]), (1, 2))
        corrupt = bytearray(request)
        corrupt[36] ^= 0xFF  # inside the ICMP message
        for bad in (capture([(1, bytes(corrupt)), (2, reply)]),
                    capture([(1, request[:-3]), (2, reply)]),
                    capture([(2, request), (1, reply)])):
            with self.assertRaises(ValueError):
                convert_icmp_echo(bad)
        batch, micros = [], 1000
        for index in range(10000):
            body = b"batch-%06d" % index
            batch.append((micros, packet("10.0.0.1", "10.0.0.2", echo(8, 9, index % 65536, body))))
            batch.append((micros + 40, packet("10.0.0.2", "10.0.0.1", echo(0, 9, index % 65536, body))))
            micros += 100
        large, volume = convert_icmp_echo(capture(batch))
        self.assertEqual(volume["paired_exchanges"], 10000)
        self.assertEqual(sum(1 for _ in csv.DictReader(io.StringIO(large))), 10000)
        self.assertTrue(all(int(r["icmp_rtt_us"]) == 40
                            for r in csv.DictReader(io.StringIO(large))))

    def test_pcap_udp_reuses_imc_with_separate_capture_clock(self):
        import importlib.util
        if importlib.util.find_spec("dpkt") is None:
            self.skipTest("optional pinned PCAP decoder is exercised in the external-decoder CI environment")
        from scripts.convert_imc_sensors import convert_pcap
        def checksum(data):
            data += b"\0" * (len(data) % 2)
            total = sum(struct.unpack(">" + "H" * (len(data) // 2), data))
            while total >> 16:
                total = (total & 65535) + (total >> 16)
            return 65535 - total
        def datagram(payload, port=6002, fragment=0):
            addresses = bytes([192, 0, 2, 1, 192, 0, 2, 2])
            udp = struct.pack(">HHHH", 6001, port, 8 + len(payload), 0) + payload
            check = checksum(addresses + struct.pack(">BBH", 0, 17, len(udp)) + udp) or 65535
            udp = udp[:6] + struct.pack(">H", check) + udp[8:]
            ip = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 1, fragment, 64, 17, 0) + addresses
            ip = ip[:10] + struct.pack(">H", checksum(ip)) + ip[12:]
            return b"\0" * 12 + b"\x08\x00" + ip + udp
        def pcap(packets, order="<", nano=False, linktype=1):
            header = struct.pack(order + "IHHIIII", 0xa1b23c4d if nano else 0xa1b2c3d4, 2, 4, 0, 0, 65535, linktype)
            return header + b"".join(struct.pack(order + "IIII", 100 + i, 123456789 if nano else 123456,
                len(packet), len(packet)) + packet for i, packet in enumerate(packets))
        def wifi(ethernet, ds=0, flags=0, fragment=0):
            header = struct.pack("<HH", 8 | ds << 8 | flags, 0) + bytes(range(18)) + struct.pack("<H", 16 | fragment)
            if ds == 3:
                header += bytes(range(6))
            return header + b"\xaa\xaa\x03\0\0\0" + ethernet[12:]
        for order, nano, value in (("<", False, 20), (">", True, 30)):
            packets = [datagram(frame(263, value, order)), datagram(frame(65000, b"future")),
                       datagram(frame(263, 99), port=6003)]
            data = pcap(packets, order, nano)
            output, report = convert_pcap(data, 6002)
            row = next(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(row["temperature_k"]), value + 273.15)
            self.assertEqual(row["record_time_us"], "100123456")
            self.assertEqual(row["imc_payload_time_us"], "1700000000250000")
            self.assertEqual((report["packets"], report["selected_packets"], report["unselected_packets"], report["decoded_records"]), (3, 2, 1, 1))
            self.assertEqual(report["selected_sources"][1]["imc"]["unsupported_frames"][0]["message_id"], 65000)
            for ds in range(4):
                wireless = [wifi(packet, ds, flags=0x800) for packet in packets]
                wifi_output, wifi_report = convert_pcap(pcap(wireless, order, nano, 105), 6002)
                wifi_rows = list(csv.DictReader(io.StringIO(wifi_output)))
                self.assertEqual(float(wifi_rows[0]["temperature_k"]), value + 273.15)
                self.assertEqual(wifi_rows[0]["record_time_us"], "100123456")
                self.assertEqual((wifi_report["selected_packets"], wifi_report["unselected_packets"]), (2, 1))
                self.assertEqual(wifi_report["selected_sources"][0]["packet_hex"], wireless[0].hex())
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    with tempfile.TemporaryDirectory() as tmp:
                        target = Path(tmp) / "wifi.csv"
                        target.write_text(wifi_output)
                        common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                            str(ROOT / "profiles/declared/imc-sensors/profile.toml"), str(target)],
                            check=True, capture_output=True).stdout)
                        self.assertEqual(common["observations"][0]["fields"]["temperature_k"], value + 273.15)
                        self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for bad_wireless in ([wifi(packets[0], flags=flag) for flag in (1, 0x80, 0x400, 0x4000, 0x8000)] +
                                 [wifi(packets[0], fragment=1), wifi(packets[0])[:25],
                                  wifi(packets[0]) + b"\0" * 4,
                                  wifi(packets[0])[:24] + b"\0" + wifi(packets[0])[25:]]):
                with self.assertRaises(ValueError):
                    convert_pcap(pcap([bad_wireless], linktype=105), 6002)
            _, mixed_report = convert_pcap(pcap([b"\x80\0", wifi(packets[0])], linktype=105), 6002)
            self.assertEqual(mixed_report["unselected_packets"], 1)
            self.assertEqual(mixed_report["unselected_status"], "NOT_DECODED_OR_VALIDATED")
            unchecked = packets[0][:40] + b"\0\0" + packets[0][42:]
            self.assertEqual(convert_pcap(pcap([unchecked]), 6002)[1]["selected_sources"][0]["udp_checksum"], "NOT_PROVIDED")
            with tempfile.TemporaryDirectory() as tmp:
                source, destination = Path(tmp) / "input.pcap", Path(tmp) / "output"
                source.write_bytes(data)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_imc_sensors.py"), str(source),
                    str(destination), "--source-format", "pcap-udp-imc", "--udp-destination-port", "6002"], check=True, capture_output=True)
                self.assertEqual((destination / "source.pcap").read_bytes(), data)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                        str(destination / "observations.csv")], check=True, capture_output=True).stdout)
                    self.assertEqual(common["observations"][0]["fields"]["temperature_k"], value + 273.15)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for bad in (data[:-1], data + b"x", pcap([datagram(frame(263, 1), fragment=0x2000)]),
                        pcap([packets[0][:24] + bytes([packets[0][24] ^ 1]) + packets[0][25:]]),
                        pcap([packets[0][:-1] + bytes([packets[0][-1] ^ 1])])):
                with self.assertRaises(ValueError): convert_pcap(bad, 6002)
            with self.assertRaises(ValueError): convert_pcap(data, 1)

        def sentence(body):
            value = 0
            for byte in body.encode("ascii"): value ^= byte
            return f"${body}*{value:02X}\r\n".encode("ascii")
        for angle, unit, speed, expected in ((90, "N", 10, 10 * 1852 / 3600), (270, "K", 36, 10)):
            good = sentence(f"WIMWV,{angle},R,{speed},{unit},A")
            invalid = sentence("WIMWV,0,T,2,M,V")
            unknown = sentence("GPXYZ,future")
            data = pcap([datagram(good + invalid), datagram(unknown)])
            output, report = convert_pcap(data, 6002, payload_format="nmea-wind")
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(float(rows[0]["wind_speed_m_s"]), expected)
            self.assertEqual(rows[0]["record_time_us"], "100123456")
            self.assertEqual(rows[0]["nmea_unlocated_time_us"], "0")
            self.assertEqual(rows[1]["wind_speed_m_s"], "")
            self.assertEqual(report["valid_wind_measurements"], 1)
            self.assertEqual(report["selected_sources"][1]["nmea"]["unsupported_records"], 1)
            wireless_output, wireless_report = convert_pcap(
                pcap([wifi(datagram(good + invalid), 1), wifi(datagram(unknown), 2)], linktype=105),
                6002, payload_format="nmea-wind")
            self.assertAlmostEqual(float(next(csv.DictReader(io.StringIO(wireless_output)))["wind_speed_m_s"]), expected)
            self.assertEqual(wireless_report["valid_wind_measurements"], 1)
            with tempfile.TemporaryDirectory() as tmp:
                source, target = Path(tmp) / "input.pcap", Path(tmp) / "out"
                source.write_bytes(data)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_imc_sensors.py"), str(source),
                    str(target), "--source-format", "pcap-udp-nmea-wind", "--udp-destination-port", "6002"], check=True, capture_output=True)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                        str(target / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertAlmostEqual(common["observations"][0]["fields"]["wind_speed_m_s"], expected)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                self.assertEqual((target / "source.pcap").read_bytes(), data)
            with self.assertRaises(ValueError): convert_pcap(pcap([datagram(unknown)]), 6002, payload_format="nmea-wind")
            with self.assertRaises(ValueError): convert_pcap(pcap([datagram(good.replace(b"MWV", b"MWX"))]), 6002, payload_format="nmea-wind")
            _, withheld = convert_pcap(pcap([datagram(invalid)]), 6002, payload_format="nmea-wind")
            self.assertEqual(withheld["conversion_status"], "ONLY_WITHHELD_VALUES")

    def test_fixed_slog_reuses_units_and_keeps_ambiguous_nan(self):
        for names, values, selected, expected in ((["PRESSURE", "OTHER"], "10 3", "PRESSURE", 1000),
                                                  (["OTHER", "PRESSURE"], "4 20", "PRESSURE", 2000)):
            header = "%%   (1) TIME\n" + "".join(f"%%   ({i + 2}) {name}\n" for i, name in enumerate(names))
            header += "%% TIME " + " ".join(names) + "\n%% TIME [ 0.00] [ 0.00]\n"
            data = (header + "0.1234567 " + values + "\n1 NaN NaN\n").encode()
            output, report = convert_slog(data, selected, "hPa", 123)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(float(rows[0]["pressure_pa"]), expected)
            self.assertEqual(rows[0]["logger_relative_time_us"], "123456")
            self.assertEqual(rows[0]["logger_relative_time_text"], "0.1234567")
            self.assertEqual(rows[1]["pressure_pa"], "")
            self.assertEqual(rows[1]["value_disposition"], "WITHHELD_NOT_FRESH_OR_NONNUMERIC_OR_MISSING")
            self.assertEqual((report["selected_records"], report["withheld_records"]), (1, 1))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "source.slog"; path.write_bytes(data)
                target = Path(directory) / "out"
                subprocess.run(["python3", "-m", "scripts.convert_imc_sensors", str(path), str(target),
                    "--source-format", "moos-fixed-slog", "--variable", selected, "--unit", "hPa",
                    "--capture-time-us", "123"], cwd=ROOT, check=True, capture_output=True)
                self.assertEqual((target / "source.slog").read_bytes(), data)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/alog-profile.toml"),
                        str(target / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"]["pressure_pa"], expected)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            for bad in (data.replace(b"%% TIME " + " ".join(names).encode(), b"%% TIME OTHER CHANGED"),
                        data.replace(b"1 NaN NaN", b"0 NaN NaN"), data.replace(b"1 NaN NaN", b"1 NaN"),
                        data.replace(values.encode(), b"inf inf"), data.replace(b"%%   (1) TIME", b"%% (2) TIME")):
                with self.assertRaises(ValueError): convert_slog(bad, selected, "hPa", 123)
            with self.assertRaises(ValueError): convert_slog(data, selected, "unknown", 123)
            with self.assertRaises(ValueError): convert_slog(data, "TIME", "hPa", 123)
        output, report = convert_slog((header + "0 NaN NaN\n").encode(), selected, "hPa", 123)
        self.assertEqual(report["conversion_status"], "ONLY_WITHHELD_VALUES")

    def test_saved_node_report_composite_reaches_common_output(self):
        """A03-07-U004/U005. The string composite pMarineViewer consumes, which the D:-only path
        refuses by design. Authored fixtures; no MOOS process and no viewer session."""
        header = b"%% LOGSTART 1700000000.25\n%% DATATYPE MARKING ON\n"
        composite = ("NAME=alpha,TYPE=KAYAK,UTC_TIME=1267294386.51,X=29.66,Y=-23.49, "
                     "LAT=43.825089, LON=-70.330030, SPD=2.00, HDG=119.06,DEPTH=0.00,"
                     "LENGTH=4.0,MODE=DRIVE,GROUP=A,VSOURCE=ais")
        data = header + (f"0.12345 NODE_REPORT_LOCAL pNodeReporter S:{composite}\n"
                         "0.20000 NAV_X pNav D:12.5\n").encode()
        output, report = convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(bytes.fromhex(row["node_name_hex"][4:]).decode(), "alpha")
        self.assertEqual(bytes.fromhex(row["node_mode_hex"][4:]).decode(), "DRIVE")
        self.assertEqual((row["node_reported_x"], row["node_reported_y"]), ("29.66", "-23.49"))
        self.assertEqual((row["node_reported_spd"], row["node_reported_hdg"]), ("2.00", "119.06"))
        self.assertEqual(row["node_reported_dep"], "0.00")          # DEPTH folded to DEP
        self.assertEqual(row["node_reported_time_text"], "1267294386.51")  # UTC_TIME folded to TIME
        for column in rows[0]:
            for unit in ("_m", "_deg", "_rad", "_mps", "_kn", "_s"):
                self.assertFalse(column.startswith("node_reported") and column.endswith(unit))
        self.assertEqual(row["node_reported_alt"], "")
        self.assertEqual(row["node_absent_fields"], "ALT")
        self.assertEqual(row["node_unselected_key_count"], "2")     # LENGTH and VSOURCE
        self.assertEqual(bytes.fromhex(row["node_report_hex"][4:]).decode(), composite)
        self.assertEqual(row["node_declaration_basis"], NODE_REPORT_BASIS)
        for stated in ("PRODUCERS_PUBLISHED_DOCUMENTATION_STATES", "NEVER_A_DEFAULT",
                       "STAY_UNQUALIFIED", "FRAME_STAYS_UNRESOLVED", "YAW_EXCLUDED"):
            self.assertIn(stated, row["node_declaration_basis"])
        self.assertEqual((report["source_records"], report["selected_records"],
                          report["unselected_records"]), (2, 1, 1))
        self.assertEqual(report["declared_logstart_text"], "1700000000.25")

        with self.assertRaises(ValueError):
            convert_alog(data, "NODE_REPORT_LOCAL", "pNodeReporter", "mV", 555, timestamp_decimals=5)
        with self.assertRaises(ValueError):
            convert_node_report(data, "NODE_REPORT", "pNodeReporter", 555)   # selects nothing
        numeric = header + b"0.12345 NODE_REPORT pNodeReporter D:12.5\n"
        with self.assertRaises(ValueError):
            convert_node_report(numeric, "NODE_REPORT", "pNodeReporter", 555)

        for bad in ("{\"NAME\":\"alpha\"}",          # braced JSON form is not this selector's
                    "NAME=alpha,X=abc",              # pinned parser would silently ignore this
                    "NAME=alpha,X=1,X=2",            # pinned parser would take the last
                    "NAME=alpha,X=nan", "NAME=alpha,X=",
                    "TYPE=KAYAK", "NAME=,X=1", "NAME=alpha,justtext", ""):
            with self.subTest(composite=bad), self.assertRaises(ValueError):
                node_report_values(bad)
        kept, unselected = node_report_values("NAME=a,TRAJECTORY={1,2,3},X=4")
        self.assertEqual((kept["NAME"], kept["X"], unselected), ("a", "4", 1))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, out = base / "input.alog", base / "out"
            source.write_bytes(data)
            subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_imc_sensors.py"),
                str(source), str(out), "--source-format", NODE_REPORT_FORMAT,
                "--variable", "NODE_REPORT_LOCAL", "--publisher", "pNodeReporter",
                "--capture-time-us", "555"], check=True, capture_output=True)
            self.assertEqual((out / "source.alog").read_bytes(), data)   # source retained whole
            self.assertNotEqual(0, subprocess.run([os.sys.executable,
                str(ROOT / "scripts/convert_imc_sensors.py"), str(source), str(base / "u"),
                "--source-format", NODE_REPORT_FORMAT, "--variable", "NODE_REPORT_LOCAL",
                "--publisher", "pNodeReporter", "--capture-time-us", "555",
                "--unit", "mV"], capture_output=True).returncode)
            if os.environ.get("MUSUBI_TELEMETRY_READER"):
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/moos-node-report/profile.toml"),
                    str(out / "observations.csv"), "--allow-equal-time"],
                    check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 1)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["node_reported_x"], 29.66)
                self.assertEqual(fields["node_reported_alt"], None)   # absent stays absent
                self.assertEqual(fields["node_report_form"], "CSP")
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")

    def test_declared_node_units_become_canonical_meaning_and_misdeclaration_refuses(self):
        """A03-07-U004/U005 completion. The record declares no unit, so a deployment declares one
        with its stated basis and the shared engineering-unit table converts it. Nothing is
        defaulted: without a declaration the canonical columns stay blank."""
        header = b"%% LOGSTART 1700000000.250000\n%% DATATYPE MARKING ON\n"
        composite = "NAME=alpha,X=29.66,Y=-23.49,SPD=2.00,HDG=180.00,DEPTH=1.50"
        data = header + f"0.12345 NODE_REPORT_LOCAL pNodeReporter S:{composite}\n".encode()
        declared = {"SPD": "m/s", "HDG": "deg", "X": "m", "Y": "m", "DEP": "m"}

        output, report = convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555)
        bare = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(bare["node_reported_spd"], "2.00")
        for column in ("node_speed_m_s", "node_heading_rad", "node_x_m", "node_y_m",
                       "node_depth_m"):
            self.assertEqual(bare[column], "")
        self.assertEqual(report["unit_declaration"], "NONE_DECLARED")

        output, report = convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
            declared_units=declared, unit_basis="mission-configuration-declared",
            record_clock="unix-epoch-logstart-relative")
        row = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(float(row["node_speed_m_s"]), 2.0)
        self.assertEqual(float(row["node_heading_rad"]), math.pi)
        self.assertEqual((float(row["node_x_m"]), float(row["node_y_m"])), (29.66, -23.49))
        self.assertEqual(float(row["node_depth_m"]), 1.5)
        self.assertIn("basis=mission-configuration-declared", row["node_unit_declaration"])
        self.assertEqual(row["node_reported_hdg"], "180.00")   # the reported value still stands
        self.assertEqual(row["record_time_us"], "1700000000373450")
        self.assertIn("DECLARED_UNIX_EPOCH_LOGSTART", row["record_clock_basis"])
        other, _ = convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
            declared_units={"HDG": "rad"}, unit_basis="operator-declared")
        self.assertEqual(float(list(csv.DictReader(io.StringIO(other)))[0]["node_heading_rad"]),
                         180.0)

        for bad, basis in (({"SPD": "deg"}, "operator-declared"),      # wrong quantity for SPD
                           ({"HDG": "m"}, "operator-declared"),
                           ({"X": "kn"}, "operator-declared"),         # not in the shared table
                           ({"DEP": "m/s"}, "operator-declared"),
                           ({"NAME": "m"}, "operator-declared"),       # not a declarable field
                           ({"SPD": "m/s"}, None),                     # declaration without basis
                           ({"SPD": "m/s"}, "because-i-say-so")):
            with self.subTest(declared=bad), self.assertRaises(ValueError):
                convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
                    declared_units=bad, unit_basis=basis)
        with self.assertRaises(ValueError):    # a basis with nothing declared
            convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
                unit_basis="operator-declared")
        with self.assertRaises(ValueError):    # a log start the caller cannot state exactly
            convert_node_report(header.replace(b".250000", b".2500005")
                + f"0.12345 NODE_REPORT_LOCAL pNodeReporter S:{composite}\n".encode(),
                "NODE_REPORT_LOCAL", "pNodeReporter", 555,
                record_clock="unix-epoch-logstart-relative")

        split = f"0.12345 NODE_REPORT_LOCAL pNodeReporter {composite}\n".encode()
        output, report = convert_node_report(split, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
            declared_start="1700000000.250000", declared_units={"SPD": "km/h"},
            unit_basis="operator-declared", record_clock="unix-epoch-logstart-relative")
        row = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(float(row["node_speed_m_s"]), 2.0 / 3.6)   # declared km/h, canonical m/s
        self.assertEqual(row["record_time_us"], "1700000000373450")
        self.assertIn("WITHOUT_THE_WRITERS_TYPE_HEADER", report["header_basis"])
        self.assertEqual(bytes.fromhex(row["node_report_hex"][4:]).decode(), composite)
        for wrong in (header + split,                       # a % line contradicts the declaration
                      f"0.12345 NODE_REPORT_LOCAL pNodeReporter S:{composite}\n".encode()):
            with self.subTest(input=wrong[:20]), self.assertRaises(ValueError):
                convert_node_report(wrong, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
                                    declared_start="1700000000.250000")
        with self.assertRaises(ValueError):    # unmarked records without any declared start
            convert_node_report(split, "NODE_REPORT_LOCAL", "pNodeReporter", 555)

        output, _ = convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
                                        unit_basis="producer-documentation-stated")
        cited = list(csv.DictReader(io.StringIO(output)))[0]
        self.assertEqual(float(cited["node_speed_m_s"]), 2.0)
        self.assertEqual(float(cited["node_heading_rad"]), math.pi)
        self.assertEqual(float(cited["node_depth_m"]), 1.5)
        self.assertIn("basis=producer-documentation-stated", cited["node_unit_declaration"])
        for miscited in ({"SPD": "km/h"}, {"HDG": "rad"}, {"X": "cm"},
                         {"ALT": "m"}, {"LAT": "deg"}):   # not in the documented sentence
            with self.subTest(miscited=miscited), self.assertRaises(ValueError):
                convert_node_report(data, "NODE_REPORT_LOCAL", "pNodeReporter", 555,
                    declared_units=miscited, unit_basis="producer-documentation-stated")

        numeric = b"%% LOGSTART 1700000000.25\n%% DATATYPE MARKING ON\n0.500 POWER sensor D:12000\n"
        rows = list(csv.DictReader(io.StringIO(
            convert_alog(numeric, "POWER", "sensor", "mV", 555)[0])))
        self.assertEqual(float(rows[0]["voltage_v"]), 12)
        self.assertEqual(rows[0]["record_time_us"], "555")

    def test_node_report_cli_applies_or_refuses_and_the_meaning_reaches_common_output(self):
        """The declared meaning has to arrive in common output, not just in the CSV, and an option
        the chosen format cannot consume has to be refused instead of silently ignored."""
        reader = os.environ.get("MUSUBI_TELEMETRY_READER")
        script = str(ROOT / "scripts/convert_imc_sensors.py")
        profile = str(ROOT / "profiles/declared/moos-node-report/profile.toml")
        header = b"%% LOGSTART 1700000000.250000\n%% DATATYPE MARKING ON\n"
        composite = "NAME=alpha,X=29.66,Y=-23.49,SPD=2.00,HDG=180.00,DEPTH=1.50"
        typed = header + f"0.12345 NODE_REPORT_LOCAL pNodeReporter S:{composite}\n".encode()
        split = f"0.12345 NODE_REPORT_LOCAL pNodeReporter {composite}\n".encode()
        selection = ["--variable", "NODE_REPORT_LOCAL", "--publisher", "pNodeReporter",
                     "--capture-time-us", "555"]

        def run(source, name, *options, expect=0):
            out = source.parent / name
            done = subprocess.run([os.sys.executable, script, str(source), str(out),
                                   *selection, *options], capture_output=True)
            self.assertEqual(done.returncode, expect, done.stderr)
            if expect:
                self.assertFalse(out.exists())   # a refusal leaves no partial conversion behind
                return None
            return out

        def common_output(out):
            if not reader:
                print("moos node report common-reader integration NOT_RUN: reader not configured")
                return None
            return json.loads(subprocess.run([reader, profile, str(out / "observations.csv"),
                "--allow-equal-time"], check=True, capture_output=True).stdout)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            alog = base / "in.alog"; alog.write_bytes(typed)
            klog = base / "in.klog"; klog.write_bytes(split)

            out = run(alog, "cited", "--source-format", "moos-ivp-node-report-f56c",
                      "--unit-basis", "producer-documentation-stated",
                      "--record-clock", "unix-epoch-logstart-relative")
            self.assertEqual((out / "source.alog").read_bytes(), typed)   # source kept whole
            common = common_output(out)
            if common:
                self.assertEqual(common["main_rows"], 1)
                fields = common["observations"][0]["fields"]
                self.assertEqual(fields["node_speed_m_s"], 2.0)
                self.assertEqual(fields["node_heading_rad"], math.pi)
                self.assertEqual(fields["node_x_m"], 29.66)
                self.assertIn("producer-documentation-stated", fields["node_unit_declaration"])
                self.assertEqual(common["observations"][0]["t_ms"], 1700000000373)
                self.assertIn("DECLARED_UNIX_EPOCH_LOGSTART", fields["record_clock_basis"])
                self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                self.assertEqual(common["platform_domain"], "Unknown")
                self.assertEqual(common["domain_source"], "profile_declared_family")
                self.assertIn("moos_ivp_node_report_f56c", json.dumps(common["profile_identity"]))
                self.assertIn("not authenticity", common["identity_basis"])

            out = run(klog, "split", "--source-format", "moos-ivp-split-klog-f56c",
                      "--declared-logstart", "1700000000.250000",
                      "--node-unit", "SPD=km/h", "--unit-basis", "operator-declared",
                      "--record-clock", "unix-epoch-logstart-relative")
            self.assertEqual((out / "source.klog").read_bytes(), split)   # original kept whole
            self.assertFalse((out / "source.alog").exists())
            common = common_output(out)
            if common:
                fields = common["observations"][0]["fields"]
                self.assertAlmostEqual(fields["node_speed_m_s"], 2.0 / 3.6)
                self.assertIn("operator-declared", fields["node_unit_declaration"])
                self.assertEqual(bytes.fromhex(fields["node_report_hex"][4:]).decode(), composite)
                self.assertEqual(common["platform_domain"], "Unknown")

            numeric = base / "numeric.alog"
            numeric.write_bytes(header + b"0.50000 POWER sensor D:12000\n")
            numeric_selection = ("--source-format", "moos-ivp-typed-alog-f56c",
                                 "--variable", "POWER", "--publisher", "sensor", "--unit", "mV")
            out = run(numeric, "numeric", *numeric_selection)
            self.assertIn("voltage_v", (out / "observations.csv").read_text())
            for ignored in (["--node-unit", "SPD=m/s"], ["--unit-basis", "operator-declared"],
                            ["--declared-logstart", "1700000000.250000"],
                            ["--record-clock", "unix-epoch-logstart-relative"]):
                with self.subTest(option=ignored[0]):
                    run(numeric, "rejected", *numeric_selection, *ignored, expect=2)
            run(alog, "rejected", "--source-format", "moos-ivp-node-report-f56c",
                "--declared-logstart", "1700000000.250000", expect=2)
            run(klog, "rejected", "--source-format", "moos-ivp-split-klog-f56c", expect=2)
            run(alog, "rejected", "--source-format", "moos-ivp-split-klog-f56c",
                "--declared-logstart", "1700000000.250000", expect=2)


    def test_moos_ivp_five_decimal_clock_reuses_typed_units(self):
        header = b"%% LOGSTART 1700000000.123456\n%% DATATYPE MARKING ON\n"
        for variable, unit, value, field, expected in (("VOLT", "mV", 12345, "voltage_v", 12.345),
                                                       ("PRESS", "hPa", 1000, "pressure_pa", 100000)):
            data = header + f"-0.12345 {variable} sensor D:{value}\n0.00001 OTHER other S:unknown\n0.00123 {variable} sensor D:{value}\n".encode()
            with self.assertRaises(ValueError): convert_alog(data, variable, "sensor", unit, 555)
            output, report = convert_alog(data, variable, "sensor", unit, 555, timestamp_decimals=5)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual([r["logger_relative_time_us"] for r in rows], ["-123450", "1230"])
            self.assertEqual(float(rows[0][field]), expected)
            self.assertEqual((report["source_records"], report["selected_records"], report["unselected_records"]), (3, 2, 1))
            self.assertEqual(report["timestamp_decimals"], 5)
            self.assertEqual(report["declared_logstart_text"], "1700000000.123456")
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory); source = base / "input.alog"; out = base / "out"
                source.write_bytes(data)
                subprocess.run([os.sys.executable, str(ROOT / "scripts/convert_imc_sensors.py"),
                    str(source), str(out), "--source-format", "moos-ivp-typed-alog-f56c",
                    "--variable", variable, "--publisher", "sensor", "--unit", unit,
                    "--capture-time-us", "555"], check=True, capture_output=True)
                self.assertEqual((out / "source.alog").read_bytes(), data)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/alog-profile.toml"),
                        str(out / "observations.csv"), "--allow-equal-time"], check=True, capture_output=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"][field], expected)
            for precision in (True, 4, 6):
                with self.assertRaises(ValueError): convert_alog(data, variable, "sensor", unit, 555, timestamp_decimals=precision)
            with self.assertRaises(ValueError):
                convert_alog(data.replace(b"-0.12345", b"-0.123"), variable, "sensor", unit, 555, timestamp_decimals=5)

    def test_marked_alog_reuses_units_and_preserves_clocks_and_unselected_records(self):
        header = b"%% LOGSTART 1700000000.25\n%% DATATYPE MARKING ON\n"
        for name, unit, source_value, field, expected in (
                ("POWER", "mV", "12000", "voltage_v", 12),
                ("TEMP", "degC", "25", "temperature_k", 298.15)):
            data = header + f"-0.125 {name} sensor D:{source_value}\n0.500 OTHER other S:a string with spaces\n0.500 {name} sensor D:{source_value}\n".encode()
            output, report = convert_alog(data, name, "sensor", unit, 555)
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual((report["source_records"], report["selected_records"], report["unselected_records"]), (3, 2, 1))
            self.assertEqual(rows[0]["logger_relative_time_us"], "-125000")
            self.assertEqual(float(rows[0][field]), expected)
            self.assertEqual(report["declared_logstart_text"], "1700000000.25")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.csv"; path.write_text(output)
                if os.environ.get("MUSUBI_TELEMETRY_READER"):
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/alog-profile.toml"),
                        str(path), "--allow-equal-time"], capture_output=True, check=True).stdout)
                    self.assertEqual(common["main_rows"], 2)
                    self.assertEqual(common["observations"][0]["fields"][field], expected)
                    self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
                source = Path(directory) / "in.alog"; source.write_bytes(data)
                command = [os.sys.executable, str(ROOT / "scripts/convert_imc_sensors.py"),
                    str(source), str(Path(directory) / "out"), "--source-format", "moos-typed-alog",
                    "--variable", name, "--publisher", "sensor", "--unit", unit, "--capture-time-us", "555"]
                subprocess.run(command, capture_output=True, check=True)
                self.assertEqual((Path(directory) / "out/source.alog").read_bytes(), data)
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
        valid = header + b"0.000 POWER sensor D:12\n"
        for bad in (valid.replace(b"D:12", b"S:12"), valid.replace(b"D:12", b"D:NaN"),
                    valid.replace(b"D:12", b"D:1e-999"), valid.replace(b"0.000", b"0.0001"),
                    valid.replace(b"%% DATATYPE MARKING ON\n", b""), header + valid,
                    header + b"0.000 incomplete\n", header):
            with self.assertRaises(ValueError): convert_alog(bad, "POWER", "sensor", "V", 1)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_practical_record_capacity_preserves_complete_common_output(self):
        count = 180000
        data = b"".join(frame(251, 12.5 if index % 2 else 24., timestamp=index / 10.)
                        for index in range(count))
        output, report = convert_input(data)
        self.assertLess(len(data), LIMIT)
        self.assertGreater(len(output.encode()), LIMIT)
        self.assertEqual((report["source_frames"], report["decoded_frames"]), (count, count))
        rows = csv.DictReader(io.StringIO(output))
        recovered = hashlib.sha256()
        for index, row in enumerate(rows):
            self.assertEqual(float(row["electrical_voltage_v"]), 12.5 if index % 2 else 24.)
            recovered.update(bytes.fromhex(row["source_frame_hex"][4:]))
        self.assertEqual(index + 1, count)
        self.assertEqual(recovered.hexdigest(), hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "out.csv"; source.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/imc-sensors/profile.toml"), str(source)],
                check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], count)
            self.assertEqual(len(common["observations"]), count)
            self.assertEqual(common["observations"][-1]["fields"]["electrical_voltage_v"], 12.5)
            self.assertEqual(common["observations"][-1]["clock_basis"], "Unknown")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_gzip_single_and_concatenated_members_reuse_common_output(self):
        for order, voltage in [("<", 12.5), (">", 24.)]:
            raw = frame(251, voltage, order) + stream(order)
            inputs = [gzip.compress(raw, mtime=0),
                      gzip.compress(raw[:7], mtime=1) + gzip.compress(raw[7:], mtime=2)]
            for source_bytes in inputs:
                output, report = convert_input(source_bytes)
                self.assertEqual(output, convert(raw)[0])
                self.assertEqual(report["source_sha256"], hashlib.sha256(source_bytes).hexdigest())
                self.assertEqual(report["decoded_input_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(report["compression"], "gzip")
                self.assertEqual(report["source_frames"], 8)
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "observations.csv"
                    source.write_text(output)
                    result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/profile.toml"), str(source),
                        "--allow-equal-time"], capture_output=True, check=True)
                    observations = json.loads(result.stdout)["observations"]
                    self.assertEqual(len(observations), 7)
                    self.assertEqual(observations[0]["fields"]["electrical_voltage_v"], voltage)
                    self.assertEqual(observations[0]["clock_basis"], "Unknown")
            self.assertEqual(convert_input(raw)[0], convert(raw)[0])

    def test_gzip_crc_size_truncation_trailing_and_expansion_fail_closed(self):
        packed = gzip.compress(stream(), mtime=0)
        crc_bad = bytearray(packed); crc_bad[-8] ^= 1
        size_bad = bytearray(packed); size_bad[-4] ^= 1
        for data in (packed[:-1], packed[:12], packed + b"garbage", bytes(crc_bad), bytes(size_bad),
                     gzip.compress(b"\0" * (LIMIT + 1), mtime=0),
                     gzip.compress(b"\0" * (LIMIT // 2 + 1), mtime=0) * 2,
                     gzip.compress(b"", mtime=0) * 1025):
            with self.subTest(prefix=data[:10]), self.assertRaises(ValueError):
                convert_input(data)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_inertial_vectors_reuse_packet_path_and_keep_device_clock_separate(self):
        for order, device_time, axes in [("<", 12.5, (1., -2., 3.)), (">", -0.5, (-4., 5., -6.))]:
            data = b"".join(frame(message, struct.pack(order + "dddd", device_time, *axes), order)
                            for message in (256, 257))
            output, report = convert(data)
            self.assertEqual((report["source_frames"], report["decoded_frames"]), (2, 2))
            rows = list(csv.DictReader(io.StringIO(output)))
            self.assertEqual(b"".join(bytes.fromhex(row["source_frame_hex"][4:]) for row in rows), data)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "inertial.csv"
                source.write_text(output)
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/imc-sensors/profile.toml"), str(source),
                    "--allow-equal-time"], capture_output=True, check=True)
                common = json.loads(result.stdout)
                observations = common["observations"]
                self.assertEqual(len(observations), 2)
                declared = common["profile_units"]
                for axis in "xyz":
                    self.assertTrue(declared["reported_angular_velocity_" + axis + "_rad_s"]
                                    .startswith("rad/s_reported_"))
                    self.assertTrue(declared["reported_acceleration_" + axis + "_m_s2"]
                                    .startswith("m/s^2_reported_"))
                    for key in ("reported_angular_velocity_" + axis + "_rad_s",
                                "reported_acceleration_" + axis + "_m_s2"):
                        self.assertTrue("no_reference_frame" in declared[key]
                                        or "unspecified_axis_frame" in declared[key], key)
                self.assertIn("The_device_time", declared["device_time_s"])
                self.assertIn("not_UTC", declared["device_time_s"])
                self.assertTrue(declared["device_clock_basis"].startswith("UNSPECIFIED_because"))
                self.assertTrue(declared["axis_frame_basis"].startswith("UNSPECIFIED_BY_MESSAGE_SCHEMA"))
                self.assertIn("ONLY_WITHHELD_COMPONENTS", declared["measurement_status"])
                self.assertIn("UNAVAILABLE_SOURCE_SENTINEL", declared["measurement_status"])
                for observation, prefix, unit in zip(observations,
                        ("reported_angular_velocity_", "reported_acceleration_"), ("_rad_s", "_m_s2")):
                    fields = observation["fields"]
                    self.assertEqual([fields[prefix + axis + unit] for axis in "xyz"], list(axes))
                    self.assertEqual(fields["device_time_s"], device_time)
                    self.assertEqual(fields["axis_frame_basis"], "UNSPECIFIED_BY_MESSAGE_SCHEMA")
                    self.assertEqual(fields["device_clock_basis"], "UNSPECIFIED")
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertIsNone(observation["anchor_unix_us"])
            for message in (256, 257):
                for payload in (b"\0" * 24, b"\0" * 40,
                                *(struct.pack(order + "dddd", *(float("nan") if i == bad else 1.
                                  for i in range(4))) for bad in range(4))):
                    with self.assertRaises(ValueError):
                        convert(frame(message, payload, order))

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_ground_velocity_components_carry_their_reported_validity_to_common_output(self):
        """259: validity first, three fp64 m/s, and an invalid component is withheld, never zero."""
        def velocity(mask, axes, order="<", message=259, timestamp=1700000000.25):
            return frame(message, struct.pack(order + "Bddd", mask, *axes), order, timestamp)

        sets = [("<", 0x07, (0., -1.5, 2.25), ["REPORTED_VALID"] * 3, [0., -1.5, 2.25]),
                (">", 0x05, (3.5, 9.75, -0.25), ["REPORTED_VALID",
                                                 "WITHHELD_COMPONENT_NOT_REPORTED_VALID",
                                                 "REPORTED_VALID"], [3.5, None, -0.25])]
        for order, mask, axes, dispositions, expected in sets:
            with self.subTest(order=order, mask=mask):
                data = (velocity(mask, axes, order)
                        + velocity(0x00, (7., 7., 7.), order, timestamp=1700000000.5)
                        + velocity(0x08 | 0x01, (4., 5., 6.), order, timestamp=1700000000.75)
                        + velocity(0x07, (1., 2., 3.), order, 260, timestamp=1700000001.))
                output, report = convert(data)
                self.assertEqual((report["source_frames"], report["decoded_frames"]), (4, 3))
                self.assertEqual([x["message_id"] for x in report["unsupported_frames"]], [260])
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual(b"".join(bytes.fromhex(row["source_frame_hex"][4:]) for row in rows)
                                 + bytes.fromhex(report["unsupported_frames"][0]["frame_hex"]), data)
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "velocity.csv"
                    source.write_text(output)
                    common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                        str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                        str(source), "--allow-equal-time"], check=True, capture_output=True).stdout)
                self.assertEqual(common["main_rows"], 3)
                self.assertEqual(common["platform_domain"], "Unknown")
                reported, withheld, unknown_bit = (o["fields"] for o in common["observations"])
                self.assertEqual([reported.get("reported_ground_velocity_" + axis + "_m_s")
                                  for axis in "xyz"], expected)
                self.assertEqual([reported["ground_velocity_" + axis + "_disposition"]
                                  for axis in "xyz"], dispositions)
                self.assertEqual(reported["velocity_validity_mask_reported"], mask)
                self.assertEqual(reported["velocity_unknown_validity_bits"], 0)
                self.assertEqual(reported["velocity_reference_basis"],
                                 "REPORTED_RELATIVE_TO_GROUND_AXIS_FRAME_UNSPECIFIED_BY_MESSAGE_SCHEMA")
                self.assertEqual(reported["measurement_status"], "REPORTED")
                self.assertEqual(reported["source_id"], 42)
                self.assertTrue(all(withheld.get("reported_ground_velocity_" + axis + "_m_s") is None
                                    for axis in "xyz"))
                self.assertTrue(all(withheld["ground_velocity_" + axis + "_disposition"]
                                    == "WITHHELD_COMPONENT_NOT_REPORTED_VALID" for axis in "xyz"))
                self.assertEqual(withheld["measurement_status"], "ONLY_WITHHELD_COMPONENTS")
                self.assertEqual(withheld["velocity_validity_mask_reported"], 0)
                self.assertEqual(unknown_bit["velocity_unknown_validity_bits"], 0x08)
                self.assertEqual(unknown_bit["velocity_validity_mask_reported"], 0x09)
                self.assertEqual(unknown_bit["reported_ground_velocity_x_m_s"], 4.)
                self.assertIsNone(unknown_bit.get("reported_ground_velocity_y_m_s"))
                self.assertIsNone(unknown_bit.get("reported_ground_velocity_z_m_s"))
                for observation in common["observations"]:
                    self.assertEqual(observation["clock_basis"], "Unknown")
                    self.assertIsNone(observation["anchor_unix_us"])
                    self.assertIsNone(observation["fields"].get("device_time_s"))
        count = 5000
        bulk = b"".join(velocity(index % 8, (index / 8., -index / 4., 0.), timestamp=index / 10.)
                        for index in range(count))
        output, report = convert(bulk)
        self.assertEqual((report["source_frames"], report["decoded_frames"]), (count, count))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bulk.csv"
            source.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/imc-sensors/profile.toml"),
                str(source)], check=True, capture_output=True).stdout)
        self.assertEqual((common["main_rows"], len(common["observations"])), (count, count))
        self.assertEqual(common["observations"][-1]["fields"]["velocity_validity_mask_reported"],
                         (count - 1) % 8)

    def test_ground_velocity_size_validity_and_legacy_schema_boundaries(self):
        def velocity(mask, axes, order="<", message=259):
            return frame(message, struct.pack(order + "Bddd", mask, *axes), order)

        for order in ("<", ">"):
            for bit, index in ((0x01, 0), (0x02, 1), (0x04, 2)):
                axes = [1., 2., 3.]
                axes[index] = float("nan")
                with self.subTest(order=order, bit=bit), self.assertRaises(ValueError):
                    convert(velocity(0x07, tuple(axes), order))
                row = next(csv.DictReader(io.StringIO(convert(
                    velocity(0x07 & ~bit, tuple(axes), order))[0])))
                self.assertEqual(row["ground_velocity_" + "xyz"[index] + "_disposition"],
                                 "WITHHELD_COMPONENT_NOT_REPORTED_VALID")
                self.assertEqual(row["reported_ground_velocity_" + "xyz"[index] + "_m_s"], "")
        for payload in (b"\0" * 24, b"\0" * 26, struct.pack("<dddd", 1., 2., 3., 4.), b""):
            with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                convert(frame(259, payload))
        corrupt = bytearray(velocity(0x07, (1., 2., 3.)))
        corrupt[25] ^= 1
        with self.assertRaises(ValueError):
            convert(bytes(corrupt))
        schema = "neptus-2019.10.1-sensors"
        output, report = convert(velocity(0x07, (1., 2., 3.)) + velocity(0x07, (4., 5., 6.), message=260),
                                 schema=schema)
        self.assertEqual((report["source_frames"], report["decoded_frames"]), (2, 1))
        self.assertEqual([x["message_id"] for x in report["unsupported_frames"]], [260])
        self.assertEqual(float(next(csv.DictReader(io.StringIO(output)))["reported_ground_velocity_x_m_s"]), 1.)
        with self.assertRaises(ValueError):
            convert(velocity(0x07, (1., 2., 3.)), schema="imc-5.4.11")
        with self.assertRaises(ValueError):
            convert(velocity(0x07, (1., 2., 3.), message=260), schema=schema)

    def test_both_endians_six_meanings_and_full_frame_accounting(self):
        self.assertEqual(crc16(b"123456789"), 0xBB3D)
        for order in ("<", ">"):
            with self.subTest(order=order):
                data = stream(order)
                output, report = convert(data)
                rows = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual((len(rows), report["source_frames"], report["decoded_frames"]), (6, 7, 6))
                for row, (field, value) in zip(rows, [("temperature_k", 293.15), ("external_pressure_pa", 101325),
                    ("depth_m", 7.5), ("sound_speed_m_s", 1500), ("water_density_kg_m3", 1025), ("salinity_psu", 35)]):
                    self.assertAlmostEqual(float(row[field]), value)
                    self.assertEqual(row["record_time_us"], "1700000000250000")
                    self.assertEqual(row["source_id"], "42")
                restored = b"".join(bytes.fromhex(row["source_frame_hex"][4:]) for row in rows)
                restored += bytes.fromhex(report["unsupported_frames"][0]["frame_hex"])
                self.assertEqual(restored, data)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_electrical_reports_reuse_common_output_without_inferring_direction(self):
        for order, values in [("<", (12.5, -2.5, -31.25)), (">", (24.0, 3.0, 72.0))]:
            raw = b"".join(frame(message, value, order) for message, value in zip((251, 252, 364), values))
            output, report = convert(raw)
            self.assertEqual((report["source_frames"], report["decoded_frames"]), (3, 3))
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "electrical.csv"
                source.write_text(output)
                result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/imc-sensors/profile.toml"), str(source),
                    "--allow-equal-time"], capture_output=True, check=True)
                observations = json.loads(result.stdout)["observations"]
                self.assertEqual(len(observations), 3)
                for observation, field, value in zip(observations,
                        ("electrical_voltage_v", "electrical_current_a", "electrical_power_w"), values):
                    self.assertEqual(observation["fields"][field], value)
                    self.assertNotIn("battery_current_a", observation["fields"])
            for message in (251, 252, 364):
                with self.assertRaises(ValueError):
                    convert(frame(message, float("nan"), order))
                payload = struct.pack(order + "HHHdHBHB", 0xFE54, message, 2, 1., 1, 1, 1, 1) + b"\0\0"
                with self.assertRaises(ValueError):
                    convert(payload + struct.pack(order + "H", crc16(payload)))

    def test_unavailable_sound_and_important_failures(self):
        row = next(csv.DictReader(io.StringIO(convert(frame(267, -1))[0])))
        self.assertEqual(row["sound_speed_m_s"], "")
        self.assertEqual(row["measurement_status"], "UNAVAILABLE_SOURCE_SENTINEL")
        raw = frame(263, 20)
        bad_crc = bytearray(raw); bad_crc[21] ^= 1
        header = struct.pack("<HHHdHBHB", 0xFE54, 263, 8, 1., 1, 1, 1, 1) + b"\0" * 8
        wrong_size = header + struct.pack("<H", crc16(header))
        for data in [b"", raw[:-1], bytes(bad_crc), b"\x55\xfe" + raw[2:], wrong_size,
                     frame(263, float("nan")), frame(263, -274), frame(263, 20, timestamp=-1),
                     raw + frame(263, 20, timestamp=1), frame(65000, b"only unsupported")]:
            with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                convert(data)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "required CI sets built shared reader")
    def test_common_observations_do_not_turn_depth_into_altitude_or_trust_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "observations.csv"
            source.write_text(convert(stream())[0])
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/imc-sensors/profile.toml"), str(source),
                "--allow-equal-time"], capture_output=True, check=True)
            data = json.loads(result.stdout)
            self.assertEqual((data["main_rows"], data["platform_domain"]), (6, "Unknown"))
            self.assertEqual(data["observations"][2]["fields"]["depth_m"], 7.5)
            self.assertNotIn("altitude", data["observations"][2]["fields"])
            for observation in data["observations"]:
                self.assertEqual(observation["clock_basis"], "Unknown")
                self.assertIsNone(observation["anchor_unix_us"])


class MarinePayloadIdentityTests(unittest.TestCase):
    """A marine payload measurement must reach common output with its unit, time AND identity.

    The identity half is what this adds: `source_id` is the reporting system and `source_entity`
    names which component of it produced the value. Neither is an authenticated device, and this
    path reads no entity list, so an entity number is never resolved to a name. Authored packets
    only -- no real marine recording is claimed here.
    """

    SCHEMA = "neptus-2019.10.1-sensors"
    PROFILE = ROOT / "profiles/declared/imc-sensors/profile.toml"

    def payload_rows(self):
        output, report = convert_input(gzip.compress(stream(), mtime=0), schema=self.SCHEMA)
        return output, report, list(csv.DictReader(io.StringIO(output)))

    def test_measurement_time_and_reporting_identity_travel_together(self):
        _, _, rows = self.payload_rows()
        depth = [row for row in rows if row["depth_m"]]
        self.assertTrue(depth, "the authored stream carries a depth measurement")
        row = depth[0]
        self.assertAlmostEqual(float(row["depth_m"]), 7.5)
        self.assertEqual(row["source_timestamp_s"], "1700000000.25")
        self.assertEqual((row["source_id"], row["source_entity"]), ("42", "3"))
        self.assertNotEqual(row["record_time_us"], row["source_timestamp_s"])
        sound = [r for r in rows if r["sound_speed_m_s"]]
        self.assertAlmostEqual(float(sound[0]["sound_speed_m_s"]), 1500)
        self.assertEqual(sound[0]["source_entity"], "3")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "required CI sets built shared reader")
    def test_the_payload_identity_reaches_common_output_as_unauthenticated(self):
        output, _, rows = self.payload_rows()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text(output)
            common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(self.PROFILE), str(path), "--allow-equal-time"],
                capture_output=True, check=True).stdout)
        self.assertEqual(common["main_rows"], len(rows))
        self.assertEqual(common["platform_domain"], "Unknown")
        fields = [o["fields"] for o in common["observations"]]
        self.assertTrue(any(f["depth_m"] == 7.5 for f in fields))
        self.assertTrue(all(f["source_entity"] == 3 for f in fields))
        units = common["profile_units"]
        self.assertIn("not_authenticated", units["source_id"])
        self.assertIn("which_entity_of_the_reporting_system_produced_the_measurement",
                      units["source_entity"])
        self.assertIn("not_an_authenticated_device_identity", units["source_entity"])
        self.assertIn("reads_no_entity_list", units["source_entity"])
        self.assertIn("m_source_depth_reference_not_geodetic_altitude", units["depth_m"])


if __name__ == "__main__":
    unittest.main()
