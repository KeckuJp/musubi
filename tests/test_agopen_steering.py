"""Authored AgOpen steering captures; code/spec checks, not a real machine or autosteer run.

Every byte is constructed here from the two pinned sources. No device, no live UDP, no
steering command and no real capture body is involved.
"""
import csv
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

from scripts.convert_agopen_steering import (
    FRAME_BYTES, HEADING_UNAVAILABLE, IMU_BNO, IMU_NONE, IMU_UNQUALIFIED, ROLL_UNAVAILABLE,
    checksum, convert)

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "profiles/declared/agopen-steering-reuse"
SCRIPT = ROOT / "scripts/convert_agopen_steering.py"
READER = os.environ.get("MUSUBI_TELEMETRY_READER", str(ROOT / "target/debug/examples/read_telemetry_csv"))
PORT = 8888


def steering_frame(angle_hundredths=-1234, heading_tenths=1234, roll_tenths=-56,
                   switches=0b011, pwm=0, *, source=126, pgn=253, length=8, header=b"\x80\x81",
                   bad_checksum=False):
    """PGN 253 exactly as Autosteer_UDP_v5.ino assembles it, low byte first."""
    frame = bytearray(header + bytes([source, pgn, length])
                      + struct.pack("<hhh", angle_hundredths, heading_tenths, roll_tenths)
                      + bytes([switches, pwm, 0]))
    frame[FRAME_BYTES - 1] = (checksum(frame) + (1 if bad_checksum else 0)) & 0xFF
    return bytes(frame)


def ones_complement(data):
    total = sum(struct.unpack(">%dH" % (len(data) // 2), data[:len(data) // 2 * 2]))
    if len(data) % 2:
        total += data[-1] << 8
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def datagram(payload, port=PORT):
    addresses = bytes([192, 0, 2, 1, 192, 0, 2, 2])
    udp = struct.pack(">HHHH", 6001, port, 8 + len(payload), 0) + payload
    check = ones_complement(addresses + struct.pack(">BBH", 0, 17, len(udp)) + udp) or 65535
    udp = udp[:6] + struct.pack(">H", check) + udp[8:]
    ip = struct.pack(">BBHHHBBH", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0) + addresses
    ip = ip[:10] + struct.pack(">H", ones_complement(ip)) + ip[12:]
    return b"\0" * 12 + b"\x08\x00" + ip + udp


def pcap(packets, first_second=100, seconds=None):
    header = struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    stamps = seconds if seconds is not None else [first_second + i for i in range(len(packets))]
    return header + b"".join(
        struct.pack("<IIII", stamp, 123456, len(packet), len(packet)) + packet
        for stamp, packet in zip(stamps, packets))


class AgOpenSteeringTests(unittest.TestCase):
    def rows(self, packets, port=PORT, imu_source=IMU_BNO):
        output, report = convert(pcap(packets), port, imu_source)
        return list(csv.DictReader(io.StringIO(output))), report

    def test_saved_capture_reaches_common_output_with_measured_angle(self):
        observed, report = self.rows([datagram(steering_frame())])
        self.assertEqual((report["source_datagrams"], report["selected_datagrams"],
                          report["decoded_frames"]), (1, 1, 1))
        row = observed[0]
        self.assertEqual(float(row["agopen_steer_angle_deg"]), -12.34)
        self.assertIn("NOT_A_COMMAND", row["agopen_steer_angle_basis"])
        self.assertIn("NOT_A_GLOBAL_CONVENTION", row["agopen_steer_angle_sign_basis"])
        self.assertEqual(float(row["agopen_heading_deg"]), 123.4)
        self.assertEqual(float(row["agopen_roll_deg"]), -5.6)
        self.assertEqual(row["agopen_heading_status"], "REPORTED")
        self.assertEqual((row["agopen_work_bit"], row["agopen_steer_bit"], row["agopen_remote_bit"]),
                         ("1", "1", "0"))
        self.assertIn("ACTIVE_SENSE_NOT_DECLARED", row["agopen_switch_sense_basis"])
        self.assertEqual(row["agopen_pwm_display_code"], "0")
        self.assertIn("NOT_A_MEASURED_STATE", row["agopen_pwm_display_basis"])
        self.assertEqual(row["record_time_us"], "100123456")
        self.assertEqual(row["source_packet_index"], "0")
        self.assertEqual(bytes.fromhex(row["source_frame_hex"][4:]), steering_frame())
        self.assertEqual(row["agopen_reported_source_address"], "192.0.2.1")

        if not Path(READER).exists():
            print("agopen steering common-reader integration NOT_RUN: reader not built")
            return
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.pcap"
            source.write_bytes(pcap([datagram(steering_frame())]))
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(out),
                            "--udp-destination-port", str(PORT),
                            "--imu-source", IMU_BNO], check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 1)
            self.assertEqual(common["platform_domain"], "Unknown")
            self.assertEqual(common["observations"][0]["clock_basis"], "Unknown")
            self.assertEqual(common["observations"][0]["fields"]["agopen_steer_angle_deg"], -12.34)
            self.assertEqual(common["observations"][0]["fields"]["agopen_heading_deg"], 123.4)
            self.assertEqual(json.loads((out / "report.json").read_text())["decoded_frames"], 1)

    def test_second_authored_layout_keeps_sentinels_signs_and_extremes(self):
        """A different value set: no IMU, opposite sign, every switch bit, full-scale angle."""
        frames = [steering_frame(angle_hundredths=3276, heading_tenths=HEADING_UNAVAILABLE,
                                 roll_tenths=ROLL_UNAVAILABLE, switches=0b111, pwm=255),
                  steering_frame(angle_hundredths=-32768, heading_tenths=-1, roll_tenths=0,
                                 switches=0, pwm=1)]
        observed, report = self.rows([datagram(frame) for frame in frames])
        self.assertEqual(report["decoded_frames"], 2)
        absent, extreme = observed
        self.assertEqual(absent["agopen_heading_status"], "UNAVAILABLE_NO_IMU_SENTINEL_9999")
        self.assertEqual(absent["agopen_roll_status"], "UNAVAILABLE_NO_IMU_SENTINEL_8888")
        self.assertEqual(absent["agopen_heading_deg"], "")
        self.assertEqual(absent["agopen_roll_deg"], "")
        self.assertEqual(float(absent["agopen_steer_angle_deg"]), 32.76)
        self.assertEqual((absent["agopen_work_bit"], absent["agopen_steer_bit"],
                          absent["agopen_remote_bit"]), ("1", "1", "1"))
        self.assertEqual(absent["agopen_pwm_display_code"], "255")
        self.assertEqual(float(extreme["agopen_steer_angle_deg"]), -327.68)
        self.assertEqual(float(extreme["agopen_heading_deg"]), -0.1)
        self.assertEqual(float(extreme["agopen_roll_deg"]), 0.0)
        self.assertEqual(extreme["agopen_roll_status"], "REPORTED")
        self.assertEqual(extreme["record_time_us"], "101123456")

    def test_selected_frame_failures_are_refused_and_others_are_retained(self):
        for payload in (steering_frame(bad_checksum=True), steering_frame()[:-1],
                        steering_frame() + b"\x00"):
            with self.assertRaises(ValueError):
                self.rows([datagram(payload)])
        for wrong in (steering_frame(header=b"\x80\x80"), steering_frame(source=100),
                      steering_frame(pgn=250)):
            with self.assertRaises(ValueError):
                self.rows([datagram(wrong)])
        observed, report = self.rows([datagram(steering_frame(pgn=250)),
                                      datagram(steering_frame()),
                                      datagram(steering_frame(), port=PORT + 1)])
        self.assertEqual(report["decoded_frames"], 1)
        self.assertEqual(report["selected_datagrams"], 2)
        self.assertEqual(report["unselected_datagrams"], 1)
        self.assertEqual(len(report["unsupported_frames"]), 1)
        retained = report["unsupported_frames"][0]
        self.assertEqual(retained["packet_index"], 0)
        self.assertEqual(retained["disposition"], "NOT_THE_SELECTED_PGN_253_FRAME_RETAINED")
        self.assertEqual(bytes.fromhex(retained["frame_prefix_hex"]), steering_frame(pgn=250))
        self.assertEqual(observed[0]["source_packet_index"], "1")
        with self.assertRaises(ValueError):
            self.rows([datagram(steering_frame())], port=PORT + 2)
        packets = [datagram(steering_frame())] * 2
        with self.assertRaises(ValueError):
            convert(pcap(packets, seconds=[100, 90]), PORT, IMU_BNO)
        _, forward = convert(pcap(packets, seconds=[100, 110]), PORT, IMU_BNO)
        self.assertEqual(forward["decoded_frames"], 2)

    def test_imu_degrees_need_a_declaration_the_wire_cannot_give(self):
        """An unqualified branch must not be able to look qualified."""
        packets = [datagram(steering_frame())]
        for missing in (None, "", "cmps14", "bno08x", True):
            with self.assertRaises(ValueError, msg=repr(missing)):
                convert(pcap(packets), PORT, missing)
        observed, report = self.rows(packets, imu_source=IMU_UNQUALIFIED)
        row = observed[0]
        self.assertEqual(row["agopen_imu_source_declared"], IMU_UNQUALIFIED)
        self.assertEqual(row["agopen_heading_status"], "UNQUALIFIED_IMU_SOURCE_NO_DEGREES")
        self.assertEqual(row["agopen_roll_status"], "UNQUALIFIED_IMU_SOURCE_NO_DEGREES")
        self.assertEqual((row["agopen_heading_deg"], row["agopen_roll_deg"]), ("", ""))
        self.assertEqual((row["agopen_heading_raw_int16"], row["agopen_roll_raw_int16"]),
                         ("1234", "-56"))
        self.assertIn("NO_DEGREE_MEANING_ESTABLISHED", row["agopen_imu_scale_basis"])
        self.assertEqual(report["declared_imu_source"], IMU_UNQUALIFIED)
        self.assertIn("does not identify", report["imu_source_basis"])
        self.assertEqual(float(row["agopen_steer_angle_deg"]), -12.34)
        self.assertEqual(row["agopen_steer_bit"], "1")
        with self.assertRaises(ValueError):
            self.rows(packets, imu_source=IMU_NONE)
        sentinels = [datagram(steering_frame(heading_tenths=HEADING_UNAVAILABLE,
                                             roll_tenths=ROLL_UNAVAILABLE))]
        observed, _ = self.rows(sentinels, imu_source=IMU_NONE)
        self.assertEqual(observed[0]["agopen_heading_status"], "UNAVAILABLE_NO_IMU_SENTINEL_9999")
        self.assertEqual(observed[0]["agopen_roll_status"], "UNAVAILABLE_NO_IMU_SENTINEL_8888")
        self.assertEqual(observed[0]["agopen_heading_raw_int16"], "9999")
        self.assertEqual(observed[0]["agopen_heading_deg"], "")
        under = {source: self.rows(sentinels, imu_source=source)[0][0]["agopen_heading_status"]
                 for source in (IMU_BNO, IMU_NONE, IMU_UNQUALIFIED)}
        self.assertEqual(set(under.values()), {"UNAVAILABLE_NO_IMU_SENTINEL_9999"})
        qualified = self.rows(packets, imu_source=IMU_BNO)[0][0]
        self.assertEqual(float(qualified["agopen_heading_deg"]), 123.4)

    def test_declared_pgn_with_bad_length_refuses_even_in_a_mixed_capture(self):
        """Recognition is header/source/PGN; length and checksum are validation, not identity."""
        good = datagram(steering_frame())
        other = datagram(steering_frame(pgn=250))
        for corrupt in (steering_frame()[:-1], steering_frame() + b"\x00",
                        steering_frame(length=7), steering_frame(length=9),
                        steering_frame(bad_checksum=True)):
            with self.assertRaises(ValueError, msg=corrupt.hex()):
                self.rows([good, datagram(corrupt)])
            with self.assertRaises(ValueError, msg=corrupt.hex()):
                self.rows([datagram(corrupt), good])
        observed, report = self.rows([other, good])
        self.assertEqual((report["decoded_frames"], report["unsupported_frames_total"]), (1, 1))
        self.assertEqual(observed[0]["source_packet_index"], "1")

    def test_mixed_capture_bounds_the_sample_without_dropping_the_count(self):
        others = 300
        packets = [datagram(steering_frame(pgn=250, length=index % 8)) for index in range(others)]
        packets += [datagram(steering_frame(angle_hundredths=100 * index)) for index in range(3)]
        observed, report = self.rows(packets)
        self.assertEqual(report["unsupported_frames_total"], others)
        self.assertEqual(report["decoded_frames"], 3)
        self.assertEqual(len(observed), 3)
        self.assertEqual([float(row["agopen_steer_angle_deg"]) for row in observed],
                         [0.0, 1.0, 2.0])
        self.assertLessEqual(report["unsupported_frames_retained"], others)
        self.assertEqual(report["unsupported_sample_truncated"],
                         report["unsupported_frames_retained"] != others)
        entry = report["unsupported_frames"][0]
        self.assertEqual(entry["bytes"], FRAME_BYTES)
        self.assertEqual(entry["prefix_bytes"], FRAME_BYTES)
        self.assertTrue(entry["frame_sha256"].startswith("sha256:"))
        self.assertIn("frame_prefix_hex", entry)
        self.assertNotIn("frame_hex", entry)
        if not Path(READER).exists():
            print("agopen mixed-capture common-reader integration NOT_RUN: reader not built")
            return
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mixed.pcap"
            source.write_bytes(pcap(packets))
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(out),
                            "--udp-destination-port", str(PORT),
                            "--imu-source", IMU_BNO], check=True, capture_output=True)
            common = json.loads(subprocess.run(
                [READER, str(CASE / "profile.toml"), str(out / "observations.csv"),
                 "--allow-equal-time"], check=True, capture_output=True).stdout)
            self.assertEqual(common["main_rows"], 3)
            self.assertEqual(common["observations"][2]["fields"]["agopen_steer_angle_deg"], 2.0)
            saved = json.loads((out / "report.json").read_text())
            self.assertEqual(saved["unsupported_frames_total"], others)
            self.assertEqual(saved["declared_imu_source"], IMU_BNO)

    def test_practical_capture_size_and_cli_refusals(self):
        count = 2000
        packets = [datagram(steering_frame(angle_hundredths=index - 1000)) for index in range(count)]
        observed, report = self.rows(packets)
        self.assertEqual(report["decoded_frames"], count)
        self.assertEqual(len(observed), count)
        self.assertEqual(float(observed[0]["agopen_steer_angle_deg"]), -10.0)
        self.assertEqual(float(observed[-1]["agopen_steer_angle_deg"]), 9.99)
        self.assertEqual(observed[-1]["record_time_us"], str((100 + count - 1) * 1000000 + 123456))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "capture.pcap"
            source.write_bytes(pcap([datagram(steering_frame())]))
            out = Path(directory) / "out"
            subprocess.run([os.sys.executable, str(SCRIPT), str(source), str(out),
                            "--udp-destination-port", str(PORT),
                            "--imu-source", IMU_BNO], check=True, capture_output=True)
            self.assertEqual(subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(out),
                 "--udp-destination-port", str(PORT),
                 "--imu-source", IMU_BNO], capture_output=True).returncode, 1)
            self.assertEqual(subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(Path(directory) / "other"),
                 "--udp-destination-port", "1",
                 "--imu-source", IMU_BNO], capture_output=True).returncode, 1)
            self.assertNotEqual(subprocess.run(
                [os.sys.executable, str(SCRIPT), str(source), str(Path(directory) / "third")],
                capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
