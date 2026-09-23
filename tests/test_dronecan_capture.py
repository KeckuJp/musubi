"""Authored frames with the pinned external decoder. No physical recording claim."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import dronecan
from dronecan.transport import Transfer
from scripts.convert_dronecan_capture import convert

ROOT = Path(__file__).resolve().parents[1]


def messages():
    battery = dronecan.uavcan.equipment.power.BatteryInfo()
    battery.voltage, battery.current, battery.temperature = 24, 2, 300
    battery.remaining_capacity_wh = 2
    battery.state_of_charge_pct, battery.state_of_health_pct = 75, 127
    battery.battery_id = 3
    node = dronecan.uavcan.protocol.NodeStatus()
    node.uptime_sec, node.health, node.mode = 100, 1, 5
    node.sub_mode, node.vendor_specific_status_code = 3, 41215
    air = dronecan.uavcan.equipment.air_data.RawAirData()
    air.static_pressure, air.differential_pressure, air.static_air_temperature = 100000, -2, 280
    magnet = dronecan.uavcan.equipment.ahrs.MagneticFieldStrength2()
    magnet.sensor_id, magnet.magnetic_field_ga = 2, [1, -0.5, 0]
    return [battery, node, air, magnet]


def capture(payloads):
    rows = []
    for transfer_id, payload in enumerate(payloads):
        for frame in Transfer(transfer_id=transfer_id, source_node_id=42, payload=payload).to_frames():
            rows.append([str(1000 + len(rows)), f"{frame.message_id:x}", frame.bytes.hex()])
    return rows


def text(rows):
    return "time_us,extended_id,data_hex\n" + "".join(",".join(row) + "\n" for row in rows)


def node_info(*, hardware, software, flags, unique, name, coa=(), commit=0, crc=0, uptime=1234):
    """One authored GetNodeInfo response payload; never a request, never transmitted."""
    payload = dronecan.uavcan.protocol.GetNodeInfo.Response()
    payload.status.uptime_sec, payload.status.health, payload.status.mode = uptime, 1, 0
    payload.status.sub_mode, payload.status.vendor_specific_status_code = 2, 777
    payload.hardware_version.major, payload.hardware_version.minor = hardware
    payload.hardware_version.unique_id = list(unique)
    payload.hardware_version.certificate_of_authenticity = list(coa)
    payload.software_version.major, payload.software_version.minor = software
    payload.software_version.optional_field_flags = flags
    payload.software_version.vcs_commit, payload.software_version.image_crc = commit, crc
    payload.name = name
    return payload


def esc_status(index, rpm):
    status = dronecan.uavcan.equipment.esc.Status()
    status.esc_index, status.rpm, status.voltage, status.current = index, rpm, 24.5, 3.25
    status.temperature, status.error_count, status.power_rating_pct = 300, 7, 50
    return status


def node_status(uptime, health, mode, sub_mode=2, vendor=777):
    status = dronecan.uavcan.protocol.NodeStatus()
    status.uptime_sec, status.health, status.mode = uptime, health, mode
    status.sub_mode, status.vendor_specific_status_code = sub_mode, vendor
    return status


def service_frames(payload, *, source, destination, transfer_id):
    return Transfer(transfer_id=transfer_id, source_node_id=source, dest_node_id=destination,
                    payload=payload, service_not_message=True,
                    request_not_response=False).to_frames()


def message_frames(payload, *, source, transfer_id):
    return Transfer(transfer_id=transfer_id, source_node_id=source, payload=payload).to_frames()


def numbered(frames):
    """Frames in capture order with an increasing capture clock."""
    return [[str(1000 + index), f"{identifier:x}", data]
            for index, (identifier, data) in enumerate(frames)]


NODE_CASES = [
    dict(source=42, destination=7, hardware=(2, 9), software=(4, 7), flags=3,
         unique=range(16), name="com.example.esc", coa=(1, 2, 3), commit=0xDEADBEEF,
         crc=0x0123456789ABCDEF, status=(1234, 1, 0), esc_index=0, rpm=-1500,
         expect=dict(vcs="hex:deadbeef", image="hex:0123456789abcdef",
                     uid="hex:000102030405060708090a0b0c0d0e0f", coa="hex:010203",
                     uid_disposition="REPORTED_BY_NODE_NOT_VERIFIED",
                     software_disposition="REPORTED_BY_NODE_NOT_VERIFIED")),
    dict(source=11, destination=126, hardware=(0, 1), software=(0, 0), flags=0,
         unique=[0] * 16, name="com.other.controller", coa=(), commit=0xAAAA, crc=0xBBBB,
         status=(55, 0, 1), esc_index=3, rpm=900,
         expect=dict(vcs=None, image=None, uid="hex:" + "0" * 32, coa="hex:",
                     uid_disposition="ALL_ZERO_UNDEFINED_PER_DEFINITION",
                     software_disposition="VERSION_REPORTED_UNKNOWN_BOTH_ZERO")),
]


class DroneCanCaptureTests(unittest.TestCase):
    def test_actuator_layout_is_explicit_and_linear_angular_units_stay_distinct(self):
        first = dronecan.uavcan.equipment.actuator.Status()
        first.actuator_id, first.position, first.force, first.speed, first.power_rating_pct = 2, 1, 2, 3, 127
        second = dronecan.uavcan.equipment.actuator.Status()
        second.actuator_id, second.position, second.force, second.speed, second.power_rating_pct = 3, -1, -2, -3, 50
        output, report = convert(text(capture([first, second])), actuator_layout={2: "linear", 3: "angular"})
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["decoded_transfers"], 2)
        self.assertEqual(rows[0]["actuator_unit_basis"], "OPERATOR_DECLARED_LINEAR")
        self.assertEqual(float(rows[0]["actuator_position_m"]), 1)
        self.assertEqual(rows[0]["actuator_position_rad"], "")
        self.assertEqual(rows[0]["actuator_demand_fraction"], "")
        self.assertEqual(float(rows[1]["actuator_torque_n_m"]), -2)
        self.assertEqual(float(rows[1]["actuator_angular_speed_rad_s"]), -3)
        self.assertEqual(float(rows[1]["actuator_demand_fraction"]), .5)
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "out.csv"; source.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                    str(source), "--allow-equal-time"], capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][1]["fields"]["actuator_position_rad"], -1)
                linear, angular = (observation["fields"] for observation in common["observations"])
                self.assertEqual(linear["actuator_unit_basis"], "OPERATOR_DECLARED_LINEAR")
                self.assertEqual(angular["actuator_unit_basis"], "OPERATOR_DECLARED_ANGULAR")
                self.assertEqual((linear["actuator_position_m"], linear["actuator_force_n"]), (1, 2))
                self.assertEqual(angular["actuator_torque_n_m"], -2)
                self.assertEqual(angular["actuator_angular_speed_rad_s"], -3)
                self.assertEqual((linear["actuator_id"], angular["actuator_id"]), (2, 3))
                self.assertIsNone(linear["actuator_demand_fraction"])
                self.assertEqual(angular["actuator_demand_fraction"], .5)
                units = common["profile_units"]
                self.assertIn("operator_declared", units["actuator_unit_basis"])
                self.assertIn("not_independently_proved_hardware_configuration",
                              units["actuator_unit_basis"])
                self.assertTrue(units["actuator_position_m"].startswith("m_reported"))
                self.assertTrue(units["actuator_position_rad"].startswith("rad_reported"))
                self.assertTrue(units["actuator_torque_n_m"].startswith("N_m_reported"))
                self.assertIn("no_zero_reference_axis_or_calibration", units["actuator_position_rad"])
                self.assertIn("127_unknown_value", units["actuator_demand_fraction"])

    def test_actuator_missing_or_wrong_layout_and_invalid_percent_fail(self):
        from scripts.convert_dronecan_capture import parse_layout
        self.assertEqual(parse_layout(["2:linear", "3:angular"]), {2: "linear", 3: "angular"})
        for entries in [["2:linear", "2:angular"], ["2:guess"], ["256:linear"], ["2"], ["-1:angular"]]:
            with self.assertRaises(ValueError):
                parse_layout(entries)
        actuator = dronecan.uavcan.equipment.actuator.Status()
        actuator.actuator_id = 2
        capture_text = text(capture([actuator]))
        for layout in [None, {}, {3: "linear"}, {2: "guess"}, {True: "linear"}, {256: "angular"}]:
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                convert(capture_text, actuator_layout=layout)
        actuator.power_rating_pct = 101
        with self.assertRaises(ValueError):
            convert(text(capture([actuator])), actuator_layout={2: "linear"})

    def test_esc_and_rpm_reuse_capture_path_and_reported_meanings(self):
        esc = dronecan.uavcan.equipment.esc.Status()
        esc.error_count, esc.voltage, esc.current, esc.temperature = 3, 24, -2, float("nan")
        esc.rpm, esc.power_rating_pct, esc.esc_index = -600, 127, 2
        rpm = dronecan.dronecan.sensors.rpm.RPM()
        rpm.sensor_id, rpm.flags, rpm.rpm = 4, 3, 1200
        output, report = convert(text(capture([esc, rpm])))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(report["decoded_transfers"], 2)
        self.assertEqual(float(rows[0]["esc_current_a"]), -2)
        self.assertEqual(rows[0]["esc_temperature_k"], "")
        self.assertEqual(float(rows[0]["reported_rpm"]), -600)
        self.assertEqual(float(rows[0]["esc_demand_fraction"]), 1.27)
        self.assertEqual(rows[0]["esc_reported_error_count"], "3")
        self.assertEqual(rows[1]["rpm_reported_health"], "UNHEALTHY")
        self.assertEqual(rows[1]["rpm_unknown_flags"], "2")
        if os.environ.get("MUSUBI_TELEMETRY_READER"):
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "out.csv"; source.write_text(output)
                common = json.loads(subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                    str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                    str(source), "--allow-equal-time"], capture_output=True, check=True).stdout)
                self.assertEqual(common["main_rows"], 2)
                self.assertEqual(common["observations"][0]["fields"]["esc_demand_fraction"], 1.27)
                self.assertEqual(common["observations"][1]["fields"]["reported_rpm"], 1200)
                esc_fields = common["observations"][0]["fields"]
                self.assertEqual(esc_fields["reported_rpm"], -600)  # sign kept, no direction claim
                self.assertEqual(esc_fields["esc_voltage_v"], 24)
                self.assertEqual(esc_fields["esc_current_a"], -2)  # regenerative report stays signed
                self.assertIsNone(esc_fields["esc_temperature_k"])  # reported unknown, not zero
                self.assertEqual(esc_fields["esc_index"], 2)
                self.assertEqual(esc_fields["esc_reported_error_count"], 3)
                self.assertEqual(esc_fields["source_node_id"], 42)  # the authored sender
                self.assertEqual(esc_fields["data_type"], "ESCStatus")
                units = common["profile_units"]
                self.assertTrue(units["reported_rpm"].startswith("revolutions_per_minute"))
                self.assertIn("no_mechanical_rotation_direction", units["reported_rpm"])
                self.assertNotIn("command", units["reported_rpm"])
                self.assertIn("data_type_says_which_message", units["reported_rpm"])
                self.assertEqual(common["observations"][1]["fields"]["data_type"], "RPM")
                self.assertTrue(units["esc_voltage_v"].startswith("V_reported"))
                self.assertTrue(units["esc_temperature_k"].startswith("K_reported"))
                self.assertIn("regenerative_braking_report", units["esc_current_a"])
                self.assertIn("resets_when_the_motor_restarts", units["esc_reported_error_count"])
                self.assertIn("never_a_physical_fault", units["esc_reported_error_count"])
                self.assertIn("not_the_actuator_unknown_sentinel", units["esc_demand_fraction"])
                self.assertIn("not_an_independently_diagnosed", units["rpm_reported_health"])

    def test_motion_report_invalid_absolute_or_unknown_float_reject(self):
        esc = dronecan.uavcan.equipment.esc.Status()
        for field, value in [("temperature", -1), ("voltage", -1)]:
            setattr(esc, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                convert(text(capture([esc])))
            setattr(esc, field, 0)
        from types import SimpleNamespace
        from scripts.convert_dronecan_capture import meanings
        with self.assertRaises(ValueError):
            meanings(1034, SimpleNamespace(voltage=24., current=float("inf"), temperature=300.,
                esc_index=0, error_count=0, rpm=0, power_rating_pct=0))
        rpm = dronecan.dronecan.sensors.rpm.RPM()
        rpm.rpm = float("nan")
        with self.assertRaises(ValueError):
            convert(text(capture([rpm])))

    def test_four_typed_paths_units_enums_and_frame_accounting(self):
        frames = capture(messages())
        frames.append([str(2000), f"{(31 << 24) | (999 << 8) | 42:x}", "00c0"])
        output, report = convert(text(frames))
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual(len(rows), 4)
        self.assertEqual(report["decoded_frames"] + len(report["unsupported_frames"]), len(frames))
        self.assertEqual(report["unsupported_frames"][0]["source"], frames[-1])
        self.assertEqual(float(rows[0]["battery_power_w"]), 48)
        self.assertEqual(float(rows[0]["battery_remaining_energy_j"]), 7200)
        self.assertEqual(rows[0]["battery_consumed_j"], "")
        self.assertEqual(rows[0]["battery_health_fraction"], "")
        self.assertEqual(rows[1]["node_reported_mode"], "UNKNOWN_5")
        self.assertEqual(rows[1]["node_mode_code"], "5")
        self.assertEqual(rows[1]["node_reported_health"], "WARNING")
        self.assertEqual(rows[1]["node_health_code"], "1")
        self.assertEqual(rows[1]["node_sub_mode_reported"], "3")
        self.assertEqual(rows[1]["node_vendor_status_code"], "41215")
        self.assertEqual(rows[1]["node_uptime_s"], "100")
        self.assertNotEqual(rows[1]["node_uptime_s"], rows[1]["record_time_us"])
        self.assertTrue(all(row["node_uptime_s"] == "" and row["node_health_code"] == ""
                            for row in rows if row is not rows[1]))
        self.assertEqual(float(rows[2]["air_static_pressure_pa"]), 100000)
        self.assertAlmostEqual(float(rows[3]["magnetic_body_x_t"]), .0001)
        restored = [frame for row in rows for frame in json.loads(bytes.fromhex(row["source_frames_hex"][4:]))]
        self.assertEqual(restored, frames[:-1])

    def test_nan_and_percent_unknown_remain_unavailable(self):
        battery = messages()[0]
        battery.current, battery.state_of_charge_pct = float("nan"), 127
        row = next(csv.DictReader(io.StringIO(convert(text(capture([battery])))[0])))
        self.assertEqual([row[key] for key in ("battery_current_a", "battery_power_w", "battery_remaining_fraction")], ["", "", ""])

    def test_corrupt_truncated_orphan_and_wrong_capture_reject(self):
        frames = capture(messages()[:1])
        broken = [row[:] for row in frames]
        broken[0][2] = (bytes([int(broken[0][2][:2], 16) ^ 1]) + bytes.fromhex(broken[0][2][2:])).hex()
        for rows in [frames[:-1], frames[1:], broken, [frames[0], frames[0]],
                     [["0", "20000000", "c0"]], [["0", "1", ""]]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                convert(text(rows))
        with self.assertRaises(ValueError):
            convert("time,can_id,payload\n1,0,00\n")

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"), "set built reader path for connection test; required CI does")
    def test_shared_observation_reader_connection(self):
        output, _ = convert(text(capture(messages())))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "converted.csv"
            source.write_text(output)
            result = subprocess.run([os.environ["MUSUBI_TELEMETRY_READER"],
                str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                str(source), "--allow-equal-time"], capture_output=True, check=True)
            data = json.loads(result.stdout)
            self.assertEqual((data["main_rows"], data["platform_domain"]), (4, "Unknown"))
            self.assertEqual(data["observations"][0]["fields"]["battery_power_w"], 48)
            self.assertEqual(data["observations"][3]["fields"]["magnetic_body_x_t"], .0001)
            status = data["observations"][1]["fields"]
            self.assertEqual((status["node_reported_health"], status["node_health_code"]),
                             ("WARNING", 1))
            self.assertEqual((status["node_reported_mode"], status["node_mode_code"]),
                             ("UNKNOWN_5", 5))
            self.assertEqual((status["node_sub_mode_reported"], status["node_vendor_status_code"]),
                             (3, 41215))
            self.assertEqual(status["node_uptime_s"], 100)
            self.assertEqual(status["source_node_id"], 42)
            units = data["profile_units"]
            for column in ("node_uptime_s", "node_reported_health", "node_health_code",
                           "node_reported_mode", "node_mode_code", "node_sub_mode_reported",
                           "node_vendor_status_code"):
                self.assertTrue(units.get(column), f"{column} must declare its meaning to the reader")
            self.assertIn("not_a_physical_health_verdict", units["node_reported_health"])
            self.assertIn("since_that_nodes_own_boot", units["node_uptime_s"])
            self.assertIn("never_the_capture_clock", units["node_uptime_s"])
            self.assertIn("not_a_link_status", units["node_reported_mode"])
            self.assertIn("never_decoded_into_a_condition", units["node_vendor_status_code"])
            for record in data["observations"]:
                self.assertEqual(record["clock_basis"], "Unknown")
                self.assertIsNone(record["t_boot_us"])
                self.assertIsNone(record["wall_ms"])


    def test_saved_node_info_response_binds_declared_versions_to_that_nodes_other_rows(self):
        """A saved GetNodeInfo response, beside the same node's NodeStatus and ESC reports.

        Nothing is requested, encoded or sent: the response transfer is read from the capture like
        any other saved transfer, and its version fields stay declarations.
        """
        response, esc = node_info, esc_status
        for case in NODE_CASES:
            with self.subTest(source=case["source"]):
                info = service_frames(response(hardware=case["hardware"], software=case["software"],
                    flags=case["flags"], unique=case["unique"], name=case["name"], coa=case["coa"],
                    commit=case["commit"], crc=case["crc"]),
                    source=case["source"], destination=case["destination"], transfer_id=5)
                status = message_frames(node_status(*case["status"]), source=case["source"],
                                        transfer_id=1)
                motor = message_frames(esc(case["esc_index"], case["rpm"]), source=case["source"],
                                       transfer_id=2)
                other = 2 << 16 | case["destination"] << 8 | 0x80 | case["source"]
                ordered = []
                for index, frame in enumerate(info):
                    ordered.append((frame.message_id, frame.bytes.hex()))
                    if index == 1:
                        ordered.extend((f.message_id, f.bytes.hex()) for f in status)
                    if index == 2:
                        ordered.append((other, "c0"))
                    if index == 3:
                        ordered.extend((f.message_id, f.bytes.hex()) for f in motor)
                rows = [[str(1000 + i), f"{identifier:x}", data]
                        for i, (identifier, data) in enumerate(ordered)]
                output, report = convert(text(rows))
                self.assertEqual(report["decoded_transfers"], 3)
                self.assertEqual(len(report["unsupported_frames"]), 1, "the unselected service is"
                                 " accounted, never decoded")
                decoded = list(csv.DictReader(io.StringIO(output)))
                self.assertEqual([row["data_type"] for row in decoded],
                                 ["NodeStatus", "ESCStatus", "GetNodeInfo.Response"])
                self.assertEqual([row["transfer_kind"] for row in decoded],
                                 ["MESSAGE", "MESSAGE", "SERVICE_RESPONSE"])
                self.assertEqual(decoded[2]["destination_node_id"], str(case["destination"]))
                self.assertEqual([row["destination_node_id"] for row in decoded[:2]], ["", ""])
    def common_observations(self, output):
        """Run the actual shared reader. A configured path that does not exist is a failure."""
        declared = os.environ["MUSUBI_TELEMETRY_READER"]
        if not Path(declared).exists():
            self.fail(f"MUSUBI_TELEMETRY_READER is set to {declared!r}, which does not exist")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "out.csv"
            source.write_text(output)
            return json.loads(subprocess.run([declared,
                str(ROOT / "profiles/declared/dronecan-recorded/profile.toml"),
                str(source), "--allow-equal-time"], capture_output=True, check=True).stdout)

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-reader proof needs the built shared reader; required CI supplies it")
    def test_declared_versions_reach_common_output_for_each_authored_node(self):
        for case in NODE_CASES:
            with self.subTest(source=case["source"]):
                info = service_frames(node_info(hardware=case["hardware"], software=case["software"],
                    flags=case["flags"], unique=case["unique"], name=case["name"], coa=case["coa"],
                    commit=case["commit"], crc=case["crc"]),
                    source=case["source"], destination=case["destination"], transfer_id=5)
                status = message_frames(node_status(*case["status"]), source=case["source"],
                                        transfer_id=1)
                motor = message_frames(esc_status(case["esc_index"], case["rpm"]),
                                       source=case["source"], transfer_id=2)
                rows = numbered([(f.message_id, f.bytes.hex())
                                 for f in (*status, *motor, *info)])
                common = self.common_observations(convert(text(rows))[0])
                self.assertEqual(common["main_rows"], 3)
                status_row, motor_row, version = (o["fields"] for o in common["observations"])
                self.assertEqual({status_row["source_node_id"], motor_row["source_node_id"],
                                  version["source_node_id"]}, {case["source"]})
                self.assertEqual(version["destination_node_id"], case["destination"])
                self.assertNotEqual(version["destination_node_id"], version["source_node_id"])
                self.assertEqual((version["node_hardware_major_reported"],
                                  version["node_hardware_minor_reported"]), case["hardware"])
                self.assertEqual((version["node_software_major_reported"],
                                  version["node_software_minor_reported"]), case["software"])
                self.assertEqual(version["node_hardware_unique_id_hex"], case["expect"]["uid"])
                self.assertEqual(version["node_unique_id_disposition"],
                                 case["expect"]["uid_disposition"])
                self.assertEqual(version["node_software_version_disposition"],
                                 case["expect"]["software_disposition"])
                self.assertEqual(version["node_software_optional_flags_reported"], case["flags"])
                self.assertEqual(version.get("node_software_vcs_commit_hex"), case["expect"]["vcs"])
                self.assertEqual(version.get("node_software_image_crc_hex"), case["expect"]["image"])
                self.assertEqual(version["node_certificate_of_authenticity_hex"],
                                 case["expect"]["coa"])
                self.assertEqual(bytes.fromhex(version["node_name_reported_hex"][4:]).decode(),
                                 case["name"])
                self.assertIn("NOT_AUTHENTICATED", version["node_version_identity_basis"])
                self.assertEqual((version["node_uptime_s"], version["node_reported_health"]),
                                 (1234, "WARNING"))
                self.assertEqual(motor_row["reported_rpm"], case["rpm"])
                self.assertEqual(motor_row["esc_index"], case["esc_index"])
                self.assertEqual(status_row["node_uptime_s"], case["status"][0])
                for row in (status_row, motor_row):
                    self.assertIsNone(row.get("node_hardware_unique_id_hex"))
                    self.assertIsNone(row.get("node_version_identity_basis"))
                for column in ("node_hardware_unique_id_hex", "node_unique_id_disposition",
                               "node_software_vcs_commit_hex", "node_version_identity_basis",
                               "transfer_kind", "destination_node_id"):
                    self.assertTrue(common["profile_units"].get(column), column)
                units = common["profile_units"]
                self.assertIn("never_proof_of_device_identity", units["node_hardware_unique_id_hex"])
                self.assertIn("shared_reported_key", units["node_version_identity_basis"])
                self.assertIn("not_detected_segmented_or_qualified_here",
                              units["node_version_identity_basis"])

    @unittest.skipUnless(os.environ.get("MUSUBI_TELEMETRY_READER"),
                         "common-reader proof needs the built shared reader; required CI supplies it")
    def test_two_nodes_in_one_capture_keep_their_own_version_uid_and_state(self):
        """One capture, two responding nodes interleaved with their own status and ESC reports."""
        frames = []
        for case in NODE_CASES:
            frames.append([(f.message_id, f.bytes.hex()) for f in service_frames(
                node_info(hardware=case["hardware"], software=case["software"], flags=case["flags"],
                          unique=case["unique"], name=case["name"], coa=case["coa"],
                          commit=case["commit"], crc=case["crc"], uptime=case["status"][0]),
                source=case["source"], destination=case["destination"], transfer_id=5)])
            frames.append([(f.message_id, f.bytes.hex()) for f in message_frames(
                node_status(*case["status"]), source=case["source"], transfer_id=1)])
            frames.append([(f.message_id, f.bytes.hex()) for f in message_frames(
                esc_status(case["esc_index"], case["rpm"]), source=case["source"], transfer_id=2)])
        interleaved = []
        for slot in range(max(len(group) for group in frames)):
            for group in frames:
                if slot < len(group):
                    interleaved.append(group[slot])
        output, report = convert(text(numbered(interleaved)))
        self.assertEqual(report["decoded_transfers"], 6)
        self.assertEqual(report["unsupported_frames"], [])
        common = self.common_observations(output)
        self.assertEqual(common["main_rows"], 6)
        by_node = {}
        for observation in common["observations"]:
            by_node.setdefault(observation["fields"]["source_node_id"], []).append(
                observation["fields"])
        self.assertEqual(sorted(by_node), sorted(case["source"] for case in NODE_CASES))
        for case in NODE_CASES:
            with self.subTest(source=case["source"]):
                rows = by_node[case["source"]]
                self.assertEqual(len(rows), 3)
                version = next(row for row in rows if row["transfer_kind"] == "SERVICE_RESPONSE")
                motor_row = next(row for row in rows if row["esc_index"] is not None)
                status_row = next(row for row in rows if row is not version and row is not motor_row)
                self.assertEqual(version["node_hardware_unique_id_hex"], case["expect"]["uid"])
                self.assertEqual((version["node_hardware_major_reported"],
                                  version["node_hardware_minor_reported"]), case["hardware"])
                self.assertEqual((version["node_software_major_reported"],
                                  version["node_software_minor_reported"]), case["software"])
                self.assertEqual(version.get("node_software_vcs_commit_hex"), case["expect"]["vcs"])
                self.assertEqual(version["node_unique_id_disposition"],
                                 case["expect"]["uid_disposition"])
                self.assertEqual(bytes.fromhex(version["node_name_reported_hex"][4:]).decode(),
                                 case["name"])
                self.assertEqual(version["destination_node_id"], case["destination"])
                self.assertEqual(status_row["node_uptime_s"], case["status"][0])
                self.assertEqual(motor_row["reported_rpm"], case["rpm"])
                self.assertEqual(motor_row["esc_index"], case["esc_index"])
                self.assertTrue(all(row["source_node_id"] == case["source"] for row in rows))
        other = {case["source"]: case for case in NODE_CASES}
        for source, rows in by_node.items():
            for row in rows:
                if row["transfer_kind"] == "MESSAGE":
                    self.assertIsNone(row.get("node_hardware_unique_id_hex"))
                else:
                    self.assertNotEqual(row["node_hardware_unique_id_hex"],
                                        next(case["expect"]["uid"] for case in NODE_CASES
                                             if case["source"] != source))
        self.assertNotEqual(other[NODE_CASES[0]["source"]]["expect"]["uid"],
                            other[NODE_CASES[1]["source"]]["expect"]["uid"])

    def test_node_info_direction_selection_and_damaged_response_fail(self):
        payload = dronecan.uavcan.protocol.GetNodeInfo.Response()
        payload.hardware_version.unique_id = [1] * 16
        payload.software_version.major, payload.software_version.minor = 1, 2
        payload.name = "com.example.node"
        frames = Transfer(transfer_id=3, source_node_id=42, dest_node_id=7, payload=payload,
                          service_not_message=True, request_not_response=False).to_frames()
        rows = [[str(1000 + i), f"{f.message_id:x}", f.bytes.hex()] for i, f in enumerate(frames)]
        self.assertEqual(convert(text(rows))[1]["decoded_transfers"], 1)
        request = dronecan.uavcan.protocol.GetNodeInfo.Request()
        request_frames = Transfer(transfer_id=3, source_node_id=7, dest_node_id=42, payload=request,
                                  service_not_message=True, request_not_response=True).to_frames()
        request_rows = [[str(1000 + i), f"{f.message_id:x}", f.bytes.hex()]
                        for i, f in enumerate(request_frames)]
        with self.assertRaises(ValueError):
            convert(text(request_rows))
        self.assertTrue(all(int(row[1], 16) & 0x8000 for row in request_rows))
        status = dronecan.uavcan.protocol.NodeStatus()
        status.uptime_sec, status.health, status.mode = 9, 0, 0
        beside = [[str(2000 + i), f"{f.message_id:x}", f.bytes.hex()]
                  for i, f in enumerate(Transfer(transfer_id=4, source_node_id=42,
                                                 payload=status).to_frames())]
        output, report = convert(text(request_rows + beside))
        self.assertEqual(report["decoded_transfers"], 1)
        self.assertEqual(len(report["unsupported_frames"]), len(request_rows))
        decoded = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual([row["data_type"] for row in decoded], ["NodeStatus"])
        self.assertEqual(decoded[0]["node_hardware_unique_id_hex"], "")
        damaged = {
            "truncated_no_end_of_transfer": rows[:-1],
            "orphan_continuation_without_start": rows[1:],
            "reordered_inside_one_transfer": [rows[0], rows[2], rows[1], *rows[3:]],
            "corrupted_payload_byte": [row if index != 2 else [row[0], row[1], "ff" + row[2][2:]]
                                       for index, row in enumerate(rows)],
            "one_frame_from_another_source": [row if index != 2
                                              else [row[0], f"{int(row[1], 16) ^ 1:x}", row[2]]
                                              for index, row in enumerate(rows)],
            "response_relabelled_as_request": [[row[0], f"{int(row[1], 16) | 0x8000:x}", row[2]]
                                               for row in rows],
        }
        for case, rowset in damaged.items():
            with self.subTest(case=case), self.assertRaises(ValueError):
                convert(text(rowset))
        unselected = [[row[0], f"{(int(row[1], 16) & ~(0xff << 16)) | (2 << 16):x}", row[2]]
                      for row in rows]
        with self.assertRaises(ValueError):
            convert(text(unselected))

    def test_a_service_id_is_never_read_through_the_message_table(self):
        """Service and message ids are separate number spaces, and they do collide in practice."""
        identifier = 1 << 16 | 85 << 8 | 0x80 | 42
        self.assertEqual((identifier >> 8) & 0xffff, 341)
        from scripts.convert_dronecan_capture import SERVICES, TYPES
        self.assertEqual(TYPES[341], "NodeStatus")
        self.assertEqual(SERVICES[1], "GetNodeInfo.Response")
        self.assertNotIn(1, TYPES)
        payload = dronecan.uavcan.protocol.GetNodeInfo.Response()
        payload.hardware_version.unique_id = [2] * 16
        payload.software_version.major = 3
        payload.name = "com.example.controller"
        frames = Transfer(transfer_id=1, source_node_id=42, dest_node_id=85, payload=payload,
                          service_not_message=True, request_not_response=False).to_frames()
        self.assertEqual(frames[0].message_id & 0xffff, identifier & 0xffff)
        rows = [[str(1000 + i), f"{f.message_id:x}", f.bytes.hex()] for i, f in enumerate(frames)]
        decoded = list(csv.DictReader(io.StringIO(convert(text(rows))[0])))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0]["data_type"], "GetNodeInfo.Response")
        self.assertEqual(decoded[0]["transfer_kind"], "SERVICE_RESPONSE")
        self.assertEqual(decoded[0]["destination_node_id"], "85")
        self.assertEqual(decoded[0]["node_hardware_unique_id_hex"], "hex:" + "02" * 16)


if __name__ == "__main__":
    unittest.main()
