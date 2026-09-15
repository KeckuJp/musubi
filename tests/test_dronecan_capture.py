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


class DroneCanCaptureTests(unittest.TestCase):

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


    def test_motion_report_invalid_absolute_or_unknown_float_reject(self):
        esc = dronecan.uavcan.equipment.esc.Status()
        for field, value in [("temperature", -1), ("voltage", -1)]:
            setattr(esc, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                convert(text(capture([esc])))
            setattr(esc, field, 0)
        # The external typed encoder saturates float16 Inf to 65504 before wire
        # generation; test the changed meaning boundary directly, not that fixture.
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
        self.assertEqual(rows[1]["node_reported_health"], "WARNING")
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



if __name__ == "__main__":
    unittest.main()
