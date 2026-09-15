//! Offline tlog decoding and record preservation. See the recorded-input guide.
use crate::config::{ChannelId, ReadProfile};
use crate::mavlink_header::parse_mavlink2_header;

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug, Clone, Copy, Default)]
pub struct TlogReader;

const MAVLINK1_MAGIC: u8 = 0xFE;
const MAVLINK2_MAGIC: u8 = 0xFD;

#[must_use]
pub const fn crc_extra(msgid: u32) -> Option<u8> {
    match msgid {
        0 => Some(50),
        1 => Some(124),
        2 => Some(137),
        24 => Some(24),
        26 => Some(170),
        29 => Some(115),
        30 => Some(39),
        32 => Some(185),
        33 => Some(104),
        42 => Some(28),
        46 => Some(11),
        65 => Some(118),
        109 => Some(185),
        116 => Some(76),
        129 => Some(46),
        132 => Some(85),
        137 => Some(195),
        143 => Some(131),
        147 => Some(154),
        148 => Some(178),
        193 => Some(71),
        253 => Some(83),
        262 => Some(12),
        270 => Some(59),
        _ => None,
    }
}

fn mavlink1_length(msgid: u32) -> Option<usize> {
    Some(match msgid {
        0 => 9,
        1 => 31,
        2 => 12,
        24 => 30,
        26 | 116 | 129 => 22,
        29 | 137 | 143 => 14,
        30 | 32 => 28,
        33 => 28,
        42 | 46 => 2,
        65 => 42,
        109 => 9,
        132 => 14,
        147 => 36,
        148 => 60,
        193 => 22,
        253 => 51,
        _ => return None,
    })
}

fn crc_x25(bytes: &[u8], init: u16) -> u16 {
    let mut crc = init;
    for &b in bytes {
        let mut tmp = b ^ (crc & 0xFF) as u8;
        tmp ^= tmp << 4;
        crc = (crc >> 8) ^ (u16::from(tmp) << 8) ^ (u16::from(tmp) << 3) ^ (u16::from(tmp) >> 4);
    }
    crc
}

fn bytes_at<const N: usize>(payload: &[u8], off: usize) -> [u8; N] {
    let mut b = [0u8; N];
    for (i, slot) in b.iter_mut().enumerate() {
        *slot = payload.get(off + i).copied().unwrap_or(0);
    }
    b
}
fn u8_at(p: &[u8], off: usize) -> i64 {
    i64::from(bytes_at::<1>(p, off)[0])
}
fn i8_at(p: &[u8], off: usize) -> i64 {
    i64::from(bytes_at::<1>(p, off)[0] as i8)
}
fn u16_at(p: &[u8], off: usize) -> i64 {
    i64::from(u16::from_le_bytes(bytes_at::<2>(p, off)))
}
fn i16_at(p: &[u8], off: usize) -> i64 {
    i64::from(i16::from_le_bytes(bytes_at::<2>(p, off)))
}
fn u32_at(p: &[u8], off: usize) -> i64 {
    i64::from(u32::from_le_bytes(bytes_at::<4>(p, off)))
}
fn i32_at(p: &[u8], off: usize) -> i64 {
    i64::from(i32::from_le_bytes(bytes_at::<4>(p, off)))
}
fn u64_at(p: &[u8], off: usize) -> u64 {
    u64::from_le_bytes(bytes_at::<8>(p, off))
}
fn f32_at(p: &[u8], off: usize) -> f64 {
    f64::from(f32::from_le_bytes(bytes_at::<4>(p, off)))
}

struct Decoded {
    channel: ChannelId,
    fields: Vec<(String, FieldValue)>,
    stale: bool,
    t_boot_us: Option<u64>,
    anchor_unix_us: Option<i64>,
}

fn reported_sensor_status(p: &[u8], wire_v2: bool) -> Vec<(String, FieldValue)> {
    const BASE: &[&str] = &[
        "gyro3d",
        "accel3d",
        "mag3d",
        "absolute_pressure",
        "differential_pressure",
        "gps",
        "optical_flow",
        "vision_position",
        "laser_position",
        "external_ground_truth",
        "angular_rate_control",
        "attitude_stabilization",
        "yaw_position",
        "altitude_control",
        "xy_position_control",
        "motor_outputs",
        "rc_receiver",
        "gyro3d_2",
        "accel3d_2",
        "mag3d_2",
        "geofence",
        "ahrs",
        "terrain",
        "reverse_motor",
        "logging",
        "battery",
        "proximity",
        "satcom",
        "prearm_check",
        "obstacle_avoidance",
        "propulsion",
    ];
    const EXTENDED: &[&str] = &[
        "recovery_system",
        "leak",
        "gyro3d_3",
        "accel3d_3",
        "gyro3d_4",
        "accel3d_4",
        "mag3d_3",
        "mag3d_4",
    ];
    let declared = u32_at(p, 0) & (1_i64 << 31) != 0;
    let available = declared && wire_v2;
    let mut output = vec![
        (
            "SYS_STATUS.extended_declared_reported".into(),
            FieldValue::I64(i64::from(declared)),
        ),
        (
            "SYS_STATUS.extended_interpretation".into(),
            FieldValue::Text(
                if available {
                    "DECLARED_MAVLINK2"
                } else {
                    "NOT_DECLARED_OR_MAVLINK1"
                }
                .into(),
            ),
        ),
        (
            "SYS_STATUS.mainloop_load_fraction_reported".into(),
            FieldValue::F64(u16_at(p, 12) as f64 / 1000.),
        ),
    ];
    for (index, state) in ["present", "enabled", "healthy"].iter().enumerate() {
        let mask = u32_at(p, index * 4);
        for (bit, name) in BASE.iter().enumerate() {
            output.push((
                format!("SYS_STATUS.{name}.{state}_reported"),
                FieldValue::I64(i64::from(mask & (1_i64 << bit) != 0)),
            ));
        }
        let extended = u32_at(p, 31 + index * 4);
        output.push((
            format!("SYS_STATUS.{state}_extended_raw"),
            if wire_v2 {
                FieldValue::I64(extended)
            } else {
                FieldValue::Blank
            },
        ));
        output.push((
            format!("SYS_STATUS.{state}_extended_unknown_bits"),
            if available {
                FieldValue::I64(extended & !255)
            } else {
                FieldValue::Blank
            },
        ));
        for (bit, name) in EXTENDED.iter().enumerate() {
            output.push((
                format!("SYS_STATUS.{name}.{state}_reported"),
                if available {
                    FieldValue::I64(i64::from(extended & (1_i64 << bit) != 0))
                } else {
                    FieldValue::Blank
                },
            ));
        }
    }
    output
}

fn decode(msgid: u32, p: &[u8]) -> Decoded {
    let f = |k: &str, v: i64| (k.to_string(), FieldValue::I64(v));
    let ff = |k: &str, v: f64| (k.to_string(), FieldValue::F64(v));
    let value = |name: &str, raw: i64, missing: i64, scale: f64| {
        (
            name.to_owned(),
            if raw == missing {
                FieldValue::Blank
            } else {
                FieldValue::F64(raw as f64 * scale)
            },
        )
    };
    let mut d = Decoded {
        channel: ChannelId::Tlog,
        fields: Vec::new(),
        stale: false,
        t_boot_us: None,
        anchor_unix_us: None,
    };
    match msgid {
        270 => {
            d.channel = ChannelId::Event;
            let flags = u16_at(p, 8);
            let stream = u8_at(p, 18);
            let camera = u8_at(p, 19);
            d.fields = vec![
                ff("video_stream_framerate_hz_reported", f32_at(p, 0)),
                f("video_stream_bitrate_bits_s_reported", u32_at(p, 4)),
                f("video_stream_flags_raw", flags),
                f("video_stream_unknown_flag_bits", flags & !7),
                f("video_stream_running_reported", i64::from(flags & 1 != 0)),
                f("video_stream_thermal_reported", i64::from(flags & 2 != 0)),
                f(
                    "video_stream_thermal_range_capable_reported",
                    i64::from(flags & 4 != 0),
                ),
                f("video_stream_width_pixels_reported", u16_at(p, 10)),
                f("video_stream_height_pixels_reported", u16_at(p, 12)),
                f(
                    "video_stream_rotation_clockwise_deg_reported",
                    u16_at(p, 14),
                ),
                f("video_stream_hfov_deg_reported", u16_at(p, 16)),
                f("video_stream_id_raw", stream),
                f("video_stream_camera_id_raw", camera),
                (
                    "video_stream_id_basis".into(),
                    FieldValue::Text(
                        if stream == 0 {
                            "UNQUALIFIED_ZERO"
                        } else {
                            "REPORTED_STREAM_INDEX"
                        }
                        .into(),
                    ),
                ),
                (
                    "video_stream_camera_id_basis".into(),
                    FieldValue::Text(
                        match camera {
                            0 => "SENDER_OR_UNAVAILABLE_EXTENSION",
                            1..=6 => "ATTACHED_CAMERA_REPORTED",
                            _ => "UNKNOWN_RETAINED",
                        }
                        .into(),
                    ),
                ),
            ];
        }
        262 => {
            d.channel = ChannelId::Event;
            d.t_boot_us = Some(u32_at(p, 0) as u64 * 1000);
            let image = u8_at(p, 16);
            let video = u8_at(p, 17);
            let count = i32_at(p, 18);
            let device = u8_at(p, 22);
            d.fields = vec![
                f("camera_image_status_raw", image),
                f("camera_video_status_raw", video),
                f("camera_image_count_raw", count),
                f("camera_device_id_raw", device),
                ff("camera_image_interval_s_reported", f32_at(p, 4)),
                ff("camera_available_capacity_mib_raw", f32_at(p, 12)),
            ];
            for (name, value) in [
                (
                    "camera_image_status_reported",
                    match image {
                        0 => "IDLE",
                        1 => "CAPTURING",
                        2 => "INTERVAL_IDLE",
                        3 => "INTERVAL_CAPTURING",
                        _ => "UNKNOWN",
                    },
                ),
                (
                    "camera_video_status_reported",
                    match video {
                        0 => "IDLE",
                        1 => "CAPTURING",
                        _ => "UNKNOWN",
                    },
                ),
                (
                    "camera_id_basis",
                    match device {
                        0 => "SENDER_COMPONENT_OR_EXTENSION_UNAVAILABLE",
                        1..=6 => "ATTACHED_CAMERA_REPORTED",
                        _ => "UNKNOWN",
                    },
                ),
                (
                    "camera_image_count_status",
                    match count {
                        1.. => "REPORTED",
                        0 => "ZERO_OR_EXTENSION_UNAVAILABLE",
                        _ => "UNQUALIFIED_NEGATIVE",
                    },
                ),
                (
                    "camera_recording_time_status",
                    if u32_at(p, 8) == 0 {
                        "NOT_PROVIDED"
                    } else {
                        "REPORTED"
                    },
                ),
                (
                    "camera_capacity_status",
                    if f32_at(p, 12) < 0.0 {
                        "UNQUALIFIED_NEGATIVE"
                    } else {
                        "REPORTED"
                    },
                ),
            ] {
                d.fields.push((name.into(), FieldValue::Text(value.into())));
            }
            if count > 0 {
                d.fields.push(f("camera_image_count_reported", count));
            }
            if u32_at(p, 8) > 0 {
                d.fields.push(ff(
                    "camera_recording_time_s_reported",
                    u32_at(p, 8) as f64 / 1000.,
                ));
            }
            if f32_at(p, 12) >= 0.0 {
                d.fields.push(ff(
                    "camera_available_capacity_bytes_reported",
                    f32_at(p, 12) * 1_048_576.,
                ));
            }
        }
        26 | 116 | 129 => {
            d.channel = ChannelId::Onboard;
            d.t_boot_us = Some(u32_at(p, 0) as u64 * 1000);
            d.fields.push(f(
                "imu_sensor_index_reported",
                match msgid {
                    26 => 1,
                    116 => 2,
                    _ => 3,
                },
            ));
            for (offset, quantity, scale) in [
                (4, "acceleration_m_s2", 9.80665 / 1000.0),
                (10, "angular_velocity_rad_s", 0.001),
                (16, "magnetic_field_t", 1e-7),
            ] {
                for (axis, step) in [("x", 0), ("y", 2), ("z", 4)] {
                    d.fields.push(ff(
                        &format!("scaled_imu_{quantity}_{axis}"),
                        i16_at(p, offset + step) as f64 * scale,
                    ));
                }
            }
            let temperature = i16_at(p, 22);
            d.fields.push(f("imu_temperature_cdeg_raw", temperature));
            d.fields.push((
                "imu_temperature_k".into(),
                if temperature == 0 || temperature == 1 {
                    FieldValue::Blank
                } else {
                    FieldValue::F64(temperature as f64 / 100.0 + 273.15)
                },
            ));
            d.fields.push((
                "imu_temperature_status".into(),
                FieldValue::Text(
                    match temperature {
                        0 => "NOT_AVAILABLE",
                        1 => "ZERO_OR_ONE_CENTIDEGREE",
                        _ => "REPORTED",
                    }
                    .into(),
                ),
            ));
        }
        29 | 137 | 143 => {
            d.channel = ChannelId::Onboard;
            d.t_boot_us = Some(u32_at(p, 0) as u64 * 1000);
            d.fields.push(f(
                "pressure_sensor_index_reported",
                match msgid {
                    29 => 1,
                    137 => 2,
                    _ => 3,
                },
            ));
            d.fields
                .push(ff("pressure_absolute_pa", f32_at(p, 4) * 100.0));
            d.fields
                .push(ff("pressure_differential_pa", f32_at(p, 8) * 100.0));
            d.fields.push(ff(
                "pressure_temperature_k",
                i16_at(p, 12) as f64 / 100.0 + 273.15,
            ));
            let temperature = i16_at(p, 14);
            d.fields
                .push(f("differential_temperature_cdeg_raw", temperature));
            d.fields.push((
                "differential_temperature_k".into(),
                if temperature == 0 || temperature == 1 {
                    FieldValue::Blank
                } else {
                    FieldValue::F64(temperature as f64 / 100.0 + 273.15)
                },
            ));
            d.fields.push((
                "differential_temperature_status".into(),
                FieldValue::Text(
                    match temperature {
                        0 => "NOT_AVAILABLE",
                        1 => "ZERO_OR_ONE_CENTIDEGREE",
                        _ => "REPORTED",
                    }
                    .into(),
                ),
            ));
        }
        132 => {
            d.channel = ChannelId::Onboard;
            let time = u32_at(p, 0);
            d.t_boot_us = Some(time as u64 * 1000);
            d.fields.push(f("DISTANCE_SENSOR.time_boot_ms", time));
            for (name, offset) in [
                ("min_distance_m", 4),
                ("max_distance_m", 6),
                ("reported_distance_m", 8),
            ] {
                d.fields.push(ff(
                    &format!("DISTANCE_SENSOR.{name}"),
                    u16_at(p, offset) as f64 / 100.0,
                ));
            }
            for (name, offset) in [
                ("type_code", 10),
                ("sensor_id", 11),
                ("orientation_code", 12),
                ("variance_cm2_raw", 13),
                ("signal_quality_percent_raw", 38),
            ] {
                d.fields
                    .push(f(&format!("DISTANCE_SENSOR.{name}"), u8_at(p, offset)));
            }
            let covariance = u8_at(p, 13);
            d.fields.push((
                "DISTANCE_SENSOR.variance_m2".into(),
                if covariance == 255 {
                    FieldValue::Blank
                } else {
                    FieldValue::F64(covariance as f64 / 10000.0)
                },
            ));
            for (name, offset) in [("horizontal_fov_rad", 14), ("vertical_fov_rad", 18)] {
                let value = f32_at(p, offset);
                d.fields.push((
                    format!("DISTANCE_SENSOR.{name}"),
                    if value == 0.0 {
                        FieldValue::Blank
                    } else {
                        FieldValue::F64(value)
                    },
                ));
            }
            for (index, axis) in ["w", "x", "y", "z"].iter().enumerate() {
                d.fields.push(ff(
                    &format!("DISTANCE_SENSOR.orientation_quaternion_{axis}_reported"),
                    f32_at(p, 22 + index * 4),
                ));
            }
            let quality = u8_at(p, 38);
            let state = if quality == 1 {
                "INVALID_SIGNAL"
            } else if u16_at(p, 8) < u16_at(p, 4) || u16_at(p, 8) > u16_at(p, 6) {
                "OUTSIDE_DECLARED_RANGE"
            } else {
                "WITHIN_DECLARED_RANGE_NOT_PHYSICAL_VALIDATION"
            };
            d.fields.push((
                "DISTANCE_SENSOR.range_state".into(),
                FieldValue::Text(state.into()),
            ));
            d.fields.push((
                "DISTANCE_SENSOR.signal_quality_state".into(),
                FieldValue::Text(
                    if quality == 0 {
                        "NOT_PROVIDED"
                    } else if quality == 1 {
                        "INVALID_SIGNAL"
                    } else {
                        "REPORTED_PERCENT"
                    }
                    .into(),
                ),
            ));
        }
        147 => {
            d.channel = ChannelId::Event;
            d.fields.extend([
                f("battery_id_reported", u8_at(p, 32)),
                f("battery_function_code_reported", u8_at(p, 33)),
                f("battery_type_code_reported", u8_at(p, 34)),
                f("battery_charge_state_code_reported", u8_at(p, 40)),
                f("battery_mode_code_reported", u8_at(p, 49)),
                f("battery_fault_mask_reported", u32_at(p, 50)),
                value("battery_current_a", i16_at(p, 30), -1, 0.01),
                value("battery_consumed_ah", i32_at(p, 0), -1, 0.001),
                value("battery_consumed_j", i32_at(p, 4), -1, 100.0),
                value("battery_remaining_fraction", i8_at(p, 35), -1, 0.01),
                value("battery_time_remaining_s", i32_at(p, 36), 0, 1.0),
            ]);
            let temperature = i16_at(p, 8);
            d.fields.push((
                "battery_temperature_k".into(),
                if temperature == 32767 {
                    FieldValue::Blank
                } else {
                    FieldValue::F64(temperature as f64 * 0.01 + 273.15)
                },
            ));
            d.fields.push((
                "battery_current_sign_basis".into(),
                FieldValue::Text("SOURCE_REPORTED_SIGN_NOT_INFERRED".into()),
            ));
            d.fields.push((
                "battery_fault_mask_applicability".into(),
                FieldValue::Text(
                    if matches!(u8_at(p, 40), 5 | 6) {
                        "FAILED_OR_UNHEALTHY_REPORTED"
                    } else {
                        "NOT_INTERPRETED_FOR_THIS_CHARGE_STATE"
                    }
                    .into(),
                ),
            ));
            for index in 0..14 {
                let raw = u16_at(
                    p,
                    if index < 10 {
                        10 + index * 2
                    } else {
                        41 + (index - 10) * 2
                    },
                );
                let (missing, status) = if index < 10 {
                    (
                        raw == 65535,
                        if raw == 65535 {
                            "ABSENT_SLOT"
                        } else {
                            "CELL_OR_AGGREGATE_SEGMENT"
                        },
                    )
                } else {
                    (
                        raw <= 1,
                        match raw {
                            0 => "NOT_SUPPORTED_OR_ABSENT",
                            1 => "ZERO_OR_ONE_MV_AMBIGUOUS",
                            _ => "REPORTED_EXTENSION_SLOT",
                        },
                    )
                };
                d.fields.push((
                    format!("battery_voltage_slot_v_{}", index + 1),
                    if missing {
                        FieldValue::Blank
                    } else {
                        FieldValue::F64(raw as f64 * 0.001)
                    },
                ));
                d.fields.push((
                    format!("battery_voltage_slot_status_{}", index + 1),
                    FieldValue::Text(status.into()),
                ));
            }
        }
        148 => {
            let version = u32_at(p, 16);
            let release = version & 255;
            let channel = match release {
                0..=63 => "DEV",
                64..=127 => "ALPHA",
                128..=191 => "BETA",
                192..=254 => "RC",
                _ => "OFFICIAL",
            };
            for (name, value) in [
                ("flight_sw_version", version),
                ("major", version >> 24),
                ("minor", (version >> 16) & 255),
                ("patch", (version >> 8) & 255),
                ("release_type_code", release),
                ("middleware_sw_version", u32_at(p, 20)),
                ("os_sw_version", u32_at(p, 24)),
                ("board_version", u32_at(p, 28)),
                ("vendor_id", u16_at(p, 32)),
                ("product_id", u16_at(p, 34)),
            ] {
                d.fields
                    .push(f(&format!("AUTOPILOT_VERSION.{name}"), value));
            }
            let uid2 = bytes_at::<18>(p, 60);
            let uid_kind = if uid2.iter().any(|&b| b != 0) {
                "UID2"
            } else if u64_at(p, 8) != 0 {
                "UID"
            } else {
                "NOT_PROVIDED"
            };
            for (name, value) in [
                ("release_channel_reported", channel.to_string()),
                ("uid_preference_reported", uid_kind.to_string()),
                ("identity_basis", "SELF_REPORTED_NOT_AUTHENTICATED".into()),
                ("capabilities_hex", format!("{:016x}", u64_at(p, 0))),
                ("uid_hex", format!("{:016x}", u64_at(p, 8))),
                (
                    "uid2_hex",
                    uid2.iter().map(|b| format!("{b:02x}")).collect(),
                ),
            ] {
                d.fields
                    .push((format!("AUTOPILOT_VERSION.{name}"), FieldValue::Text(value)));
            }
            for (name, offset) in [("flight", 36), ("middleware", 44), ("os", 52)] {
                d.fields.push((
                    format!("AUTOPILOT_VERSION.{name}_custom_bytes_hex"),
                    FieldValue::Text(
                        bytes_at::<8>(p, offset)
                            .iter()
                            .map(|b| format!("{b:02x}"))
                            .collect(),
                    ),
                ));
            }
        }
        30 | 32 => {
            let (prefix, names, frame) = if msgid == 30 {
                (
                    "ATTITUDE",
                    [
                        "roll_rad",
                        "pitch_rad",
                        "yaw_rad",
                        "rollspeed_rad_s",
                        "pitchspeed_rad_s",
                        "yawspeed_rad_s",
                    ],
                    "AERONAUTICAL_Z_DOWN_Y_RIGHT_X_FRONT_INTRINSIC_ZYX",
                )
            } else {
                (
                    "LOCAL_POSITION_NED",
                    [
                        "north_m",
                        "east_m",
                        "down_m",
                        "north_velocity_m_s",
                        "east_velocity_m_s",
                        "down_velocity_m_s",
                    ],
                    "LOCAL_NED_ORIGIN_UNSPECIFIED",
                )
            };
            d.t_boot_us = Some(u32_at(p, 0) as u64 * 1000);
            d.fields
                .push(f(&format!("{prefix}.time_boot_ms"), u32_at(p, 0)));
            for (index, name) in names.iter().enumerate() {
                d.fields
                    .push(ff(&format!("{prefix}.{name}"), f32_at(p, 4 + index * 4)));
            }
            d.fields
                .push((format!("{prefix}.frame"), FieldValue::Text(frame.into())));
        }
        0 => {
            d.channel = ChannelId::Heartbeat;
            d.fields = vec![
                f("HEARTBEAT.custom_mode", u32_at(p, 0)),
                f("HEARTBEAT.type", u8_at(p, 4)),
                f("HEARTBEAT.autopilot", u8_at(p, 5)),
                f("HEARTBEAT.base_mode", u8_at(p, 6)),
                f(
                    "HEARTBEAT.reported_armed",
                    i64::from(u8_at(p, 6) & 128 != 0),
                ),
                f("HEARTBEAT.system_status", u8_at(p, 7)),
                f("HEARTBEAT.mavlink_version", u8_at(p, 8)),
            ];
            let state = u8_at(p, 7) as usize;
            let name = [
                "UNINIT",
                "BOOT",
                "CALIBRATING",
                "STANDBY",
                "ACTIVE",
                "CRITICAL",
                "EMERGENCY",
                "POWEROFF",
                "FLIGHT_TERMINATION",
            ]
            .get(state)
            .map_or_else(|| format!("UNKNOWN_{state}"), |name| (*name).to_owned());
            d.fields
                .push(("system_state_reported".into(), FieldValue::Text(name)));
        }
        1 => {
            d.channel = ChannelId::Event;
            d.fields = vec![
                f("SYS_STATUS.onboard_control_sensors_present", u32_at(p, 0)),
                f("SYS_STATUS.onboard_control_sensors_enabled", u32_at(p, 4)),
                f("SYS_STATUS.onboard_control_sensors_health", u32_at(p, 8)),
                f("SYS_STATUS.load", u16_at(p, 12)),
                f("SYS_STATUS.voltage_battery", u16_at(p, 14)),
                f("SYS_STATUS.current_battery", i16_at(p, 16)),
                f("SYS_STATUS.drop_rate_comm", u16_at(p, 18)),
                f("SYS_STATUS.errors_comm", u16_at(p, 20)),
                f("SYS_STATUS.battery_remaining", i8_at(p, 30)),
            ];
            for index in 0..4 {
                d.fields.push(f(
                    &format!("SYS_STATUS.errors_count{}", index + 1),
                    u16_at(p, 22 + index * 2),
                ));
            }
            d.fields.extend([
                value("battery_voltage_v", u16_at(p, 14), 65535, 0.001),
                value("battery_current_a", i16_at(p, 16), -1, 0.01),
                value("battery_remaining_fraction", i8_at(p, 30), -1, 0.01),
                ff("communication_drop_fraction", u16_at(p, 18) as f64 / 10000.),
            ]);
        }
        42 => {
            d.channel = ChannelId::Event;
            d.fields = vec![
                f("MISSION_CURRENT.seq", u16_at(p, 0)),
                f("MISSION_CURRENT.total", u16_at(p, 2)),
                f("MISSION_CURRENT.mission_state", u8_at(p, 4)),
                f("MISSION_CURRENT.mission_mode", u8_at(p, 5)),
                f("mission_current_sequence", u16_at(p, 0)),
            ];
            let state = u8_at(p, 4) as usize;
            let name = [
                "UNKNOWN",
                "NO_MISSION",
                "NOT_STARTED",
                "ACTIVE",
                "PAUSED",
                "COMPLETE",
            ]
            .get(state)
            .map_or_else(|| format!("UNKNOWN_{state}"), |name| (*name).to_owned());
            d.fields
                .push(("mission_state_reported".into(), FieldValue::Text(name)));
            let total = u16_at(p, 2);
            d.fields.push((
                "mission_total_items".into(),
                if total == 0 || total == 65535 {
                    FieldValue::Blank
                } else {
                    FieldValue::I64(total)
                },
            ));
            d.fields.push((
                "mission_total_disposition".into(),
                FieldValue::Text(
                    match total {
                        0 => "NOT_SUPPORTED",
                        65535 => "NO_MISSION_REPORTED",
                        _ => "REPORTED_EXCLUDING_HOME",
                    }
                    .into(),
                ),
            ));
            let mode = u8_at(p, 5);
            d.fields.push((
                "mission_mode_reported".into(),
                FieldValue::Text(match mode {
                    0 => "UNKNOWN".into(),
                    1 => "MISSION_MODE_REPORTED".into(),
                    2 => "SUSPENDED_REPORTED".into(),
                    _ => format!("UNKNOWN_{mode}"),
                }),
            ));
            for (name, offset) in [
                ("mission_plan_id_reported", 6),
                ("fence_plan_id_reported", 10),
                ("rally_plan_id_reported", 14),
            ] {
                let id = u32_at(p, offset);
                d.fields.push((
                    name.into(),
                    if id == 0 {
                        FieldValue::Blank
                    } else {
                        FieldValue::I64(id)
                    },
                ));
            }
        }
        46 => {
            d.channel = ChannelId::Event;
            d.fields = vec![
                f("MISSION_ITEM_REACHED.seq", u16_at(p, 0)),
                f("mission_item_reached_sequence_reported", u16_at(p, 0)),
                (
                    "mission_reach_disposition".into(),
                    FieldValue::Text("REPORTED_NOT_PHYSICAL_VERIFICATION".into()),
                ),
            ];
        }
        2 => {
            let unix = u64_at(p, 0);
            let boot_ms = u32_at(p, 8);
            d.channel = ChannelId::Tlog;
            d.fields = vec![
                f("SYSTEM_TIME.time_unix_usec", unix as i64),
                f("SYSTEM_TIME.time_boot_ms", boot_ms),
            ];
            d.t_boot_us = Some((boot_ms as u64) * 1000);
            d.anchor_unix_us = (unix > 946_684_800_000_000).then_some(unix as i64);
        }
        24 => {
            d.channel = ChannelId::GpsEkf;
            let t = u64_at(p, 0);
            d.fields = vec![
                f("GPS_RAW_INT.time_usec", t as i64),
                f("GPS_RAW_INT.lat", i32_at(p, 8)),
                f("GPS_RAW_INT.lon", i32_at(p, 12)),
                f("GPS_RAW_INT.eph", u16_at(p, 20)),
                f("GPS_RAW_INT.fix_type", u8_at(p, 28)),
                f("GPS_RAW_INT.satellites_visible", u8_at(p, 29)),
            ];
            if t < 1_000_000_000_000_000 {
                d.t_boot_us = Some(t);
            }
        }
        33 => {
            d.channel = ChannelId::GpsEkf;
            d.fields = vec![
                f("GLOBAL_POSITION_INT.time_boot_ms", u32_at(p, 0)),
                f("GLOBAL_POSITION_INT.lat", i32_at(p, 4)),
                f("GLOBAL_POSITION_INT.lon", i32_at(p, 8)),
                f("GLOBAL_POSITION_INT.alt", i32_at(p, 12)),
            ];
            d.t_boot_us = Some((u32_at(p, 0) as u64) * 1000);
        }
        65 => {
            d.channel = ChannelId::Rc;
            let chancount = u8_at(p, 40);
            let rssi = u8_at(p, 41);
            d.fields = vec![
                f("RC_CHANNELS.time_boot_ms", u32_at(p, 0)),
                f("RC_CHANNELS.chan1_raw", u16_at(p, 4)),
                f("RC_CHANNELS.chan3_raw", u16_at(p, 8)),
                f("RC_CHANNELS.chancount", chancount),
                f("RC_CHANNELS.rssi", rssi),
            ];
            d.t_boot_us = Some((u32_at(p, 0) as u64) * 1000);
            d.stale = chancount == 0;
        }
        109 => {
            d.channel = ChannelId::LinkStats;
            d.fields = vec![
                f("RADIO_STATUS.rxerrors", u16_at(p, 0)),
                f("RADIO_STATUS.rssi", u8_at(p, 4)),
                f("RADIO_STATUS.remrssi", u8_at(p, 5)),
                f("RADIO_STATUS.noise", u8_at(p, 7)),
                f("RADIO_STATUS.remnoise", u8_at(p, 8)),
            ];
        }
        193 => {
            d.channel = ChannelId::GpsEkf;
            d.fields = vec![
                ff("EKF_STATUS_REPORT.velocity_variance", f32_at(p, 0)),
                ff("EKF_STATUS_REPORT.pos_horiz_variance", f32_at(p, 4)),
                ff("EKF_STATUS_REPORT.compass_variance", f32_at(p, 12)),
                f("EKF_STATUS_REPORT.flags", u16_at(p, 20)),
            ];
        }
        253 => {
            let text: Vec<u8> = (1..51)
                .map(|i| bytes_at::<1>(p, i)[0])
                .take_while(|&b| b != 0)
                .collect();
            d.channel = ChannelId::Event;
            d.fields = vec![
                f("STATUSTEXT.severity", u8_at(p, 0)),
                (
                    "STATUSTEXT.text".to_string(),
                    FieldValue::Text(String::from_utf8_lossy(&text).into_owned()),
                ),
            ];
        }
        other => {
            d.fields = vec![f("msgid", i64::from(other))];
        }
    }
    d
}

impl ProfileReader for TlogReader {
    fn format_id(&self) -> &'static str {
        "mavlink_tlog"
    }
    fn read(&self, profile: &ReadProfile, bytes: &[u8]) -> Result<Vec<Observation>, ReadError> {
        let mavproxy = profile.fields.time == "mavproxy_recorded_us";
        let recorded = profile.fields.time == "host_recorded_us" || mavproxy;
        if profile.format != self.format_id()
            || profile.default_clock_basis
                != if recorded {
                    crate::ClockBasis::Unknown
                } else {
                    crate::ClockBasis::HostReceived
                }
        {
            return Err(ReadError::Profile(
                "TLOG requires host_received, or explicit recorded clock with unknown basis".into(),
            ));
        }
        let mut out = Vec::new();
        let mut off = 0usize;
        let mut sequences = std::collections::BTreeMap::<(u8, u8, u8), (u8, usize)>::new();
        while off < bytes.len() {
            let rest = &bytes[off..];
            if rest.len() < 8 + 6 {
                return Err(ReadError::Malformed {
                    offset: off,
                    what: "truncated tlog record".into(),
                });
            }
            let recorded_word = u64::from_be_bytes(bytes_at::<8>(rest, 0));
            let host_us = if mavproxy {
                recorded_word & !3
            } else {
                recorded_word
            };
            let fr = &rest[8..];
            let (hdr_len, payload_len, msgid, signed) = match fr[0] {
                MAVLINK2_MAGIC => {
                    let h = parse_mavlink2_header(fr).map_err(|e| ReadError::Malformed {
                        offset: off + 8,
                        what: format!("{e:?}"),
                    })?;
                    if h.incompat_flags & !1 != 0 {
                        return Err(ReadError::Malformed {
                            offset: off + 8,
                            what: "unsupported MAVLink incompatibility flag".into(),
                        });
                    }
                    (
                        10usize,
                        usize::from(h.len),
                        h.msgid,
                        h.incompat_flags & 0x01 != 0,
                    )
                }
                MAVLINK1_MAGIC => (6usize, usize::from(fr[1]), u32::from(fr[5]), false),
                other => {
                    return Err(ReadError::Malformed {
                        offset: off + 8,
                        what: format!("unexpected STX 0x{other:02X}"),
                    });
                }
            };
            let total = hdr_len + payload_len + 2 + if signed { 13 } else { 0 };
            if fr.len() < total {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "frame exceeds file".into(),
                });
            }
            if fr[0] == MAVLINK1_MAGIC
                && mavlink1_length(msgid).is_some_and(|length| length != payload_len)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "MAVLink1 payload has wrong fixed length".into(),
                });
            }
            if let Some(extra) = crc_extra(msgid) {
                let crc = crc_x25(&fr[1..hdr_len + payload_len], 0xFFFF);
                let crc = crc_x25(&[extra], crc);
                let got =
                    u16::from_le_bytes([fr[hdr_len + payload_len], fr[hdr_len + payload_len + 1]]);
                if crc != got {
                    return Err(ReadError::Malformed {
                        offset: off + 8,
                        what: format!(
                            "CRC mismatch on msgid {msgid}: expected {crc:#06x}, got {got:#06x}"
                        ),
                    });
                }
            }
            let payload = &fr[hdr_len..hdr_len + payload_len];
            if msgid == 270
                && (payload_len > 20 || !f32_at(payload, 0).is_finite() || f32_at(payload, 0) < 0.0)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid video stream status length or framerate".into(),
                });
            }
            if msgid == 262
                && (payload_len > 23
                    || !f32_at(payload, 4).is_finite()
                    || !f32_at(payload, 12).is_finite()
                    || f32_at(payload, 4) < 0.0)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid camera report length or quantity".into(),
                });
            }
            if matches!(msgid, 26 | 116 | 129) && (payload_len > 24 || i16_at(payload, 22) < -27315)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid scaled IMU temperature or length".into(),
                });
            }
            if matches!(msgid, 29 | 137 | 143)
                && (payload_len > 16
                    || !f32_at(payload, 4).is_finite()
                    || !f32_at(payload, 8).is_finite()
                    || f32_at(payload, 4) < 0.0
                    || i16_at(payload, 12) < -27315
                    || i16_at(payload, 14) < -27315)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid pressure report quantity or length".into(),
                });
            }
            if (msgid == 42 && payload_len > 18) || (msgid == 46 && payload_len > 2) {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "overlong mission report payload".into(),
                });
            }
            if msgid == 132
                && (payload_len > 39
                    || u16_at(payload, 4) > u16_at(payload, 6)
                    || u8_at(payload, 38) > 100
                    || (0..6).any(|index| !f32_at(payload, 14 + index * 4).is_finite())
                    || f32_at(payload, 14) < 0.0
                    || f32_at(payload, 18) < 0.0)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid DISTANCE_SENSOR length/range/quality/quantity".into(),
                });
            }
            if msgid == 148 && payload_len > 78 {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "overlong AUTOPILOT_VERSION payload".into(),
                });
            }
            if msgid == 147
                && (payload_len > 54
                    || i32_at(payload, 0) < -1
                    || i32_at(payload, 4) < -1
                    || i32_at(payload, 36) < 0
                    || i16_at(payload, 8) < -27315
                    || !(-1..=100).contains(&i8_at(payload, 35)))
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid BATTERY_STATUS range or length".into(),
                });
            }
            if matches!(msgid, 30 | 32)
                && (payload_len > 28
                    || (0..6).any(|index| !f32_at(payload, 4 + index * 4).is_finite()))
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid motion payload length or nonfinite quantity".into(),
                });
            }
            if msgid == 1
                && (payload_len > 43
                    || u16_at(payload, 12) > 1000
                    || !(-1..=100).contains(&i8_at(payload, 30))
                    || u16_at(payload, 18) > 10000)
            {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "invalid SYS_STATUS percent range".into(),
                });
            }
            if matches!(msgid, 2 | 24) && u64_at(payload, 0) > i64::MAX as u64 {
                return Err(ReadError::Malformed {
                    offset: off + 8,
                    what: "source microseconds exceed common signed clock range".into(),
                });
            }
            let mut d = decode(msgid, payload);
            if msgid == 1 {
                d.fields
                    .extend(reported_sensor_status(payload, fr[0] != MAVLINK1_MAGIC));
            }
            let identity_offset = if hdr_len == 10 { 4 } else { 2 };
            let sequence = fr[identity_offset];
            let source_key = (
                fr[identity_offset + 1],
                fr[identity_offset + 2],
                if mavproxy {
                    (recorded_word & 3) as u8
                } else {
                    0
                },
            );
            let relation = if crc_extra(msgid).is_none() || source_key.0 == 0 || source_key.1 == 0 {
                sequences.remove(&source_key);
                "UNQUALIFIED_HEADER"
            } else if let Some((previous, previous_offset)) =
                sequences.insert(source_key, (sequence, off))
            {
                let step = sequence.wrapping_sub(previous);
                d.fields.extend([
                    (
                        "MAVLINK.previous_packet_sequence".into(),
                        FieldValue::I64(i64::from(previous)),
                    ),
                    (
                        "MAVLINK.recorded_sequence_step_mod256".into(),
                        FieldValue::I64(i64::from(step)),
                    ),
                    (
                        "MAVLINK.previous_record_offset_bytes".into(),
                        FieldValue::Text(previous_offset.to_string()),
                    ),
                ]);
                match step {
                    0 => "SAME_COUNTER",
                    1 => "NEXT_MOD256",
                    _ => "DISCONTINUITY_AMBIGUOUS",
                }
            } else {
                "FIRST"
            };
            d.fields.push((
                "MAVLINK.recorded_sequence_relation".into(),
                FieldValue::Text(relation.into()),
            ));
            d.fields.extend([
                (
                    "MAVLINK.wire_version".into(),
                    FieldValue::I64(if hdr_len == 10 { 2 } else { 1 }),
                ),
                (
                    "MAVLINK.packet_sequence".into(),
                    FieldValue::I64(i64::from(fr[identity_offset])),
                ),
                (
                    "MAVLINK.system_id".into(),
                    FieldValue::I64(i64::from(fr[identity_offset + 1])),
                ),
                (
                    "MAVLINK.component_id".into(),
                    FieldValue::I64(i64::from(fr[identity_offset + 2])),
                ),
                (
                    "MAVLINK.message_id".into(),
                    FieldValue::I64(i64::from(msgid)),
                ),
                (
                    if recorded {
                        "MAVLINK.host_recorded_us"
                    } else {
                        "MAVLINK.host_received_us"
                    }
                    .into(),
                    FieldValue::Text(host_us.to_string()),
                ),
                (
                    "MAVLINK.crc_verified".into(),
                    FieldValue::I64(i64::from(crc_extra(msgid).is_some())),
                ),
                (
                    "MAVLINK.signature".into(),
                    FieldValue::Text(
                        if signed {
                            "present_unverified"
                        } else {
                            "absent"
                        }
                        .into(),
                    ),
                ),
                (
                    "MAVLINK.raw_frame_hex".into(),
                    FieldValue::Text(fr[..total].iter().map(|b| format!("{b:02x}")).collect()),
                ),
            ]);
            if mavproxy {
                d.fields.extend([
                    (
                        "MAVLINK.mavproxy_recorded_word".into(),
                        FieldValue::Text(recorded_word.to_string()),
                    ),
                    (
                        "MAVLINK.mavproxy_link_tag".into(),
                        FieldValue::I64((recorded_word & 3) as i64),
                    ),
                    (
                        "MAVLINK.recorded_clock_quantum_us".into(),
                        FieldValue::I64(4),
                    ),
                ]);
            }
            if recorded {
                d.fields.push((
                    "MAVLINK.direction".into(),
                    FieldValue::Text(
                        if mavproxy {
                            "NOT_INFERRED_FROM_AMBIGUOUS_LINK_TAG"
                        } else {
                            "UNKNOWN_NOT_ENCODED_IN_RECORD"
                        }
                        .into(),
                    ),
                ));
            }
            let t_ms = i64::try_from(host_us / 1000).unwrap_or(i64::MAX);
            let mut o = observation(profile, t_ms, d.channel, d.fields, d.stale);
            o.t_boot_us = d.t_boot_us;
            o.anchor_unix_us = if recorded { None } else { d.anchor_unix_us };
            out.push(o);
            off += 8 + total;
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::profile::parse_profile;

    const PROFILE: &str = r#"{"profile_id": "p", "version": "1", "family": "ugv", "source_role": "gcs", "format": "mavlink_tlog", "extensions": ["tlog"], "default_clock_basis": "host_received", "fields": {"time": "host_us"}}"#;

    #[test]
    fn truncated_record_is_malformed_not_silently_dropped() {
        let p = parse_profile(PROFILE).expect("profile");
        let mut bytes = vec![0u8; 8];
        bytes.extend_from_slice(&[0xFD, 0x09, 0, 0, 0, 1, 1, 0, 0, 0]); // header says 9 B payload, none follows
        assert!(matches!(
            TlogReader.read(&p, &bytes),
            Err(ReadError::Malformed { .. })
        ));
    }

    #[test]
    fn mission_reports_distinguish_reached_current_totals_and_plan_ids() {
        let profile = parse_profile(PROFILE).unwrap();
        for (sequence, total, id) in [(7_u16, 12_u16, 42_u32), (3, 65535, 4294967295)] {
            let mut payload = sequence.to_le_bytes().to_vec();
            payload.extend_from_slice(&total.to_le_bytes());
            payload.extend_from_slice(&[3, 2]);
            payload.extend_from_slice(&id.to_le_bytes());
            payload.extend_from_slice(&0_u32.to_le_bytes());
            payload.extend_from_slice(&9_u32.to_le_bytes());
            let current = TlogReader
                .read(&profile, &framed(42, &payload, true))
                .unwrap();
            let fields = &current[0].fields;
            assert!(fields.contains(&(
                "mission_plan_id_reported".into(),
                FieldValue::I64(i64::from(id))
            )));
            assert!(fields.contains(&("fence_plan_id_reported".into(), FieldValue::Blank)));
            assert!(fields.contains(&(
                "mission_total_items".into(),
                if total == 65535 {
                    FieldValue::Blank
                } else {
                    FieldValue::I64(i64::from(total))
                }
            )));
            assert!(fields.contains(&(
                "mission_mode_reported".into(),
                FieldValue::Text("SUSPENDED_REPORTED".into())
            )));
            let reached = TlogReader
                .read(&profile, &framed(46, &sequence.to_le_bytes(), false))
                .unwrap();
            assert!(reached[0].fields.contains(&(
                "mission_item_reached_sequence_reported".into(),
                FieldValue::I64(i64::from(sequence))
            )));
            assert!(
                !reached[0]
                    .fields
                    .iter()
                    .any(|(key, _)| key == "mission_current_sequence")
            );
            assert!(reached[0].t_boot_us.is_none());
            assert_eq!(reached[0].clock_basis, crate::ClockBasis::HostReceived);
        }
        for (payload, v2) in [(&[1, 0][..], false), (&[1][..], true)] {
            let current = TlogReader.read(&profile, &framed(42, payload, v2)).unwrap();
            assert!(
                current[0]
                    .fields
                    .contains(&("mission_plan_id_reported".into(), FieldValue::Blank))
            );
            assert!(current[0].fields.contains(&(
                "mission_total_disposition".into(),
                FieldValue::Text("NOT_SUPPORTED".into())
            )));
        }
        let unknown = TlogReader
            .read(&profile, &framed(42, &[1, 0, 3, 0, 254, 255], true))
            .unwrap();
        assert!(unknown[0].fields.contains(&(
            "mission_mode_reported".into(),
            FieldValue::Text("UNKNOWN_255".into())
        )));
        for (id, payload) in [(42, vec![0; 19]), (46, vec![0; 3])] {
            assert!(
                TlogReader
                    .read(&profile, &framed(id, &payload, true))
                    .is_err()
            );
        }
        assert!(TlogReader.read(&profile, &framed(46, &[1], false)).is_err());
        let mut bad = framed(46, &[1, 0], true);
        *bad.last_mut().unwrap() ^= 1;
        assert!(TlogReader.read(&profile, &bad).is_err());
    }

    #[test]
    fn mavlink1_full_payload_is_read_but_truncation_is_not_zero_filled() {
        let p = parse_profile(PROFILE).expect("profile");
        let mut bytes = 1_700_000_000_000_000_u64.to_be_bytes().to_vec();
        let mut frame = vec![0xFE, 9, 0, 1, 1, 0];
        frame.extend_from_slice(&[0; 9]);
        let crc = crc_x25(&frame[1..], 0xFFFF);
        let crc = crc_x25(&[50], crc);
        frame.extend_from_slice(&crc.to_le_bytes());
        bytes.extend_from_slice(&frame);
        let obs = TlogReader.read(&p, &bytes).expect("reads");
        assert_eq!(obs.len(), 1);
        assert_eq!(obs[0].channel, ChannelId::Heartbeat);
        assert_eq!(obs[0].t_ms, 1_700_000_000_000);
        assert_eq!(obs[0].clock_basis, crate::ClockBasis::HostReceived);
        let short = framed(0, &[], false);
        assert!(TlogReader.read(&p, &short).is_err());
    }

    #[test]
    fn crc_mismatch_on_known_msgid_is_malformed() {
        let p = parse_profile(PROFILE).expect("profile");
        let mut bytes = 1_700_000_000_000_000_u64.to_be_bytes().to_vec();
        bytes.extend_from_slice(&[0xFE, 9, 0, 1, 1, 0]);
        bytes.extend_from_slice(&[0; 9]);
        bytes.extend_from_slice(&[0xAB, 0xCD]);
        assert!(matches!(
            TlogReader.read(&p, &bytes),
            Err(ReadError::Malformed { what, .. }) if what.contains("CRC mismatch")
        ));
    }

    fn framed(id: u32, payload: &[u8], v2: bool) -> Vec<u8> {
        let mut record = 1_700_000_000_123_456_u64.to_be_bytes().to_vec();
        let mut frame = if v2 {
            vec![
                0xFD,
                payload.len() as u8,
                0,
                0,
                7,
                42,
                3,
                id as u8,
                (id >> 8) as u8,
                (id >> 16) as u8,
            ]
        } else {
            vec![0xFE, payload.len() as u8, 7, 42, 3, id as u8]
        };
        frame.extend_from_slice(payload);
        let crc = crc_x25(&[crc_extra(id).unwrap_or(0)], crc_x25(&frame[1..], 0xffff));
        frame.extend_from_slice(&crc.to_le_bytes());
        record.extend_from_slice(&frame);
        record
    }

    #[test]
    fn recorded_sequence_relations_preserve_wrap_sources_and_ambiguity() {
        fn packet(v2: bool, seq: u8, sys: u8, comp: u8, tag: u8, unknown: bool) -> Vec<u8> {
            let id = if unknown {
                if v2 { 999_999 } else { 200 }
            } else {
                0
            };
            if unknown {
                assert!(crc_extra(id).is_none());
            }
            let mut bytes = framed(id, &[0; 9], v2);
            bytes[7] = (bytes[7] & !3) | tag;
            let pos = 8 + if v2 { 4 } else { 2 };
            bytes[pos..pos + 3].copy_from_slice(&[seq, sys, comp]);
            let end = bytes.len() - 2;
            let crc = crc_x25(
                &[crc_extra(id).unwrap_or(0)],
                crc_x25(&bytes[9..end], 0xffff),
            );
            bytes[end..].copy_from_slice(&crc.to_le_bytes());
            bytes
        }
        let relation =
            |o: &Observation| crate::field(o, "MAVLINK.recorded_sequence_relation").cloned();
        for v2 in [false, true] {
            let profile = parse_profile(PROFILE).unwrap();
            let specs = [
                (254, 1, 1, false),
                (20, 2, 1, false),
                (255, 1, 1, false),
                (0, 1, 1, false),
                (0, 1, 1, false),
                (3, 1, 1, false),
                (1, 1, 1, false),
                (21, 2, 1, false),
                (8, 1, 2, false),
                (2, 1, 1, true),
                (3, 1, 1, false),
                (4, 0, 1, false),
            ];
            let bytes: Vec<u8> = specs
                .iter()
                .flat_map(|&(s, y, c, u)| packet(v2, s, y, c, 0, u))
                .collect();
            let obs = TlogReader.read(&profile, &bytes).unwrap();
            assert_eq!(obs.len(), specs.len());
            for (o, expected) in obs.iter().zip([
                "FIRST",
                "FIRST",
                "NEXT_MOD256",
                "NEXT_MOD256",
                "SAME_COUNTER",
                "DISCONTINUITY_AMBIGUOUS",
                "DISCONTINUITY_AMBIGUOUS",
                "NEXT_MOD256",
                "FIRST",
                "UNQUALIFIED_HEADER",
                "FIRST",
                "UNQUALIFIED_HEADER",
            ]) {
                assert_eq!(relation(o), Some(FieldValue::Text(expected.into())));
                assert!(crate::field(o, "MAVLINK.raw_frame_hex").is_some());
            }
            assert_eq!(
                crate::field(&obs[5], "MAVLINK.recorded_sequence_step_mod256"),
                Some(&FieldValue::I64(3))
            );
            assert_eq!(
                crate::field(&obs[6], "MAVLINK.recorded_sequence_step_mod256"),
                Some(&FieldValue::I64(254))
            );
            assert_eq!(
                crate::field(&obs[2], "MAVLINK.previous_record_offset_bytes"),
                Some(&FieldValue::Text("0".into()))
            );
            assert!(crate::field(&obs[10], "MAVLINK.previous_packet_sequence").is_none());
            let mut bad = bytes.clone();
            bad[9] ^= 1; // corrupt known frame/header, no successful partial output
            assert!(TlogReader.read(&profile, &bad).is_err());
        }
        let mut profile = parse_profile(PROFILE).unwrap();
        profile.fields.time = "mavproxy_recorded_us".into();
        profile.default_clock_basis = crate::ClockBasis::Unknown;
        let bytes: Vec<u8> = [(4, 0), (200, 1), (5, 0), (201, 1)]
            .iter()
            .flat_map(|&(seq, tag)| packet(true, seq, 1, 1, tag, false))
            .collect();
        let obs = TlogReader.read(&profile, &bytes).unwrap();
        for (o, expected) in obs
            .iter()
            .zip(["FIRST", "FIRST", "NEXT_MOD256", "NEXT_MOD256"])
        {
            assert_eq!(relation(o), Some(FieldValue::Text(expected.into())));
            assert_eq!(o.clock_basis, crate::ClockBasis::Unknown);
        }
    }

    #[test]
    fn video_stream_reports_preserve_status_without_inventing_delivery() {
        let mut profile = parse_profile(PROFILE).unwrap();
        profile.fields.time = "host_recorded_us".into();
        profile.default_clock_basis = crate::ClockBasis::Unknown;
        for (rate, bitrate, flags, stream) in [
            (29.97_f32, 4_000_000_u32, 1_u16, 1_u8),
            (60.0, 12_000_000, 0x8006, 2),
        ] {
            let mut payload = [0u8; 20];
            payload[..4].copy_from_slice(&rate.to_le_bytes());
            payload[4..8].copy_from_slice(&bitrate.to_le_bytes());
            payload[8..10].copy_from_slice(&flags.to_le_bytes());
            payload[10..12].copy_from_slice(&1920u16.to_le_bytes());
            payload[12..14].copy_from_slice(&1080u16.to_le_bytes());
            payload[14..16].copy_from_slice(&90u16.to_le_bytes());
            payload[16..18].copy_from_slice(&75u16.to_le_bytes());
            payload[18] = stream;
            payload[19] = 2;
            let bytes = framed(270, &payload, true);
            let rows = TlogReader.read(&profile, &bytes).unwrap();
            assert_eq!(rows.len(), 1);
            let row = &rows[0];
            assert_eq!(row.channel, ChannelId::Event);
            assert_eq!(row.clock_basis, crate::ClockBasis::Unknown);
            assert!(row.t_boot_us.is_none());
            assert!(row.anchor_unix_us.is_none());
            assert_eq!(
                crate::field(row, "video_stream_framerate_hz_reported"),
                Some(&FieldValue::F64(f64::from(rate)))
            );
            assert_eq!(
                crate::field(row, "video_stream_bitrate_bits_s_reported"),
                Some(&FieldValue::I64(i64::from(bitrate)))
            );
            assert_eq!(
                crate::field(row, "video_stream_running_reported"),
                Some(&FieldValue::I64(i64::from(flags & 1 != 0)))
            );
            assert_eq!(
                crate::field(row, "video_stream_unknown_flag_bits"),
                Some(&FieldValue::I64(i64::from(flags & !7)))
            );
            assert_eq!(
                crate::field(row, "video_stream_id_raw"),
                Some(&FieldValue::I64(i64::from(stream)))
            );
            let mut corrupt = bytes.clone();
            corrupt[20] ^= 1;
            assert!(TlogReader.read(&profile, &corrupt).is_err());
            for invalid in [f32::NAN, f32::INFINITY, -1.0] {
                payload[..4].copy_from_slice(&invalid.to_le_bytes());
                assert!(
                    TlogReader
                        .read(&profile, &framed(270, &payload, true))
                        .is_err()
                );
            }
        }
        let zero = TlogReader.read(&profile, &framed(270, &[0], true)).unwrap();
        assert_eq!(
            crate::field(&zero[0], "video_stream_id_basis"),
            Some(&FieldValue::Text("UNQUALIFIED_ZERO".into()))
        );
        assert_eq!(
            crate::field(&zero[0], "video_stream_camera_id_basis"),
            Some(&FieldValue::Text("SENDER_OR_UNAVAILABLE_EXTENSION".into()))
        );
        assert!(
            TlogReader
                .read(&profile, &framed(270, &[0; 21], true))
                .is_err()
        );
    }

    #[test]
    fn camera_reports_reuse_frame_clock_without_claiming_recording_success() {
        let mut profile = parse_profile(PROFILE).unwrap();
        profile.fields.time = "host_recorded_us".into();
        profile.default_clock_basis = crate::ClockBasis::Unknown;
        assert_eq!(crc_extra(262), Some(12));
        for (interval, capacity, count) in [(1.5_f32, 1024.25_f32, 12_i32), (0.0, 0.5, 314)] {
            let mut payload = [0u8; 23];
            payload[..4].copy_from_slice(&1234u32.to_le_bytes());
            payload[4..8].copy_from_slice(&interval.to_le_bytes());
            payload[8..12].copy_from_slice(&2345u32.to_le_bytes());
            payload[12..16].copy_from_slice(&capacity.to_le_bytes());
            payload[16] = 3;
            payload[17] = 1;
            payload[18..22].copy_from_slice(&count.to_le_bytes());
            payload[22] = 2;
            let bytes = framed(262, &payload, true);
            let rows = TlogReader.read(&profile, &bytes).unwrap();
            let row = &rows[0];
            assert_eq!(rows.len(), 1);
            assert_eq!(row.channel, ChannelId::Event);
            assert_eq!(row.t_boot_us, Some(1_234_000));
            assert_eq!(row.clock_basis, crate::ClockBasis::Unknown);
            assert!(row.anchor_unix_us.is_none());
            for (field, value) in [
                ("camera_image_interval_s_reported", f64::from(interval)),
                (
                    "camera_available_capacity_bytes_reported",
                    f64::from(capacity) * 1_048_576.,
                ),
                ("camera_recording_time_s_reported", 2.345),
            ] {
                assert_eq!(crate::field(row, field), Some(&FieldValue::F64(value)));
            }
            assert_eq!(
                crate::field(row, "camera_image_count_reported"),
                Some(&FieldValue::I64(i64::from(count)))
            );
            assert_eq!(
                crate::field(row, "camera_video_status_reported"),
                Some(&FieldValue::Text("CAPTURING".into()))
            );
            assert!(crate::field(row, "MAVLINK.raw_frame_hex").is_some());
            assert!(
                TlogReader
                    .read(&profile, &bytes[..bytes.len() - 1])
                    .is_err()
            );
            let mut corrupt = bytes;
            *corrupt.last_mut().unwrap() ^= 1;
            assert!(TlogReader.read(&profile, &corrupt).is_err());
            payload[16] = 255;
            payload[17] = 99;
            payload[22] = 255;
            payload[18..22].copy_from_slice(&(-1i32).to_le_bytes());
            payload[12..16].copy_from_slice(&(-1f32).to_le_bytes());
            let row = TlogReader
                .read(&profile, &framed(262, &payload, true))
                .unwrap()
                .remove(0);
            assert_eq!(
                crate::field(&row, "camera_video_status_reported"),
                Some(&FieldValue::Text("UNKNOWN".into()))
            );
            assert!(crate::field(&row, "camera_image_count_reported").is_none());
            assert!(crate::field(&row, "camera_available_capacity_bytes_reported").is_none());
            for (offset, value) in [(4, -0.1f32), (4, f32::NAN), (12, f32::INFINITY)] {
                let mut invalid = payload;
                invalid[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
                assert!(
                    TlogReader
                        .read(&profile, &framed(262, &invalid, true))
                        .is_err()
                );
            }
        }
        let row = TlogReader
            .read(&profile, &framed(262, &[0], true))
            .unwrap()
            .remove(0);
        assert_eq!(
            crate::field(&row, "camera_image_count_status"),
            Some(&FieldValue::Text("ZERO_OR_EXTENSION_UNAVAILABLE".into()))
        );
        assert!(crate::field(&row, "camera_recording_time_s_reported").is_none());
        assert!(crate::field(&row, "camera_image_count_reported").is_none());
        assert!(
            TlogReader
                .read(&profile, &framed(262, &[0; 24], true))
                .is_err()
        );
    }

    #[test]
    fn mavproxy_tagged_clock_reuses_frames_without_treating_tag_as_time() {
        let p = parse_profile(include_str!(
            "../../../profiles/recorded/tlog-state/mavproxy-recorded-profile.json"
        ))
        .unwrap();
        for (v2, voltage) in [(false, 12300_u16), (true, 24600)] {
            let mut payload = [0_u8; 31];
            payload[14..16].copy_from_slice(&voltage.to_le_bytes());
            payload[16..18].copy_from_slice(&(-120_i16).to_le_bytes());
            payload[30] = 50;
            let mut bytes = Vec::new();
            for tag in [3_u64, 0, 2, 1] {
                let mut frame = framed(1, &payload, v2);
                frame[..8].copy_from_slice(&(1_700_000_000_123_456_u64 | tag).to_be_bytes());
                bytes.extend(frame);
            }
            let rows = TlogReader.read(&p, &bytes).unwrap();
            assert_eq!(rows.len(), 4);
            for (row, tag) in rows.iter().zip([3_u64, 0, 2, 1]) {
                assert_eq!(row.t_ms, 1_700_000_000_123);
                assert_eq!(row.clock_basis, crate::ClockBasis::Unknown);
                assert!(row.anchor_unix_us.is_none());
                assert_eq!(
                    crate::field(row, "MAVLINK.host_recorded_us"),
                    Some(&FieldValue::Text("1700000000123456".into()))
                );
                assert_eq!(
                    crate::field(row, "MAVLINK.mavproxy_link_tag"),
                    Some(&FieldValue::I64(tag as i64))
                );
                assert_eq!(
                    crate::field(row, "MAVLINK.mavproxy_recorded_word"),
                    Some(&FieldValue::Text(
                        (1_700_000_000_123_456_u64 | tag).to_string()
                    ))
                );
                assert_eq!(
                    crate::field(row, "battery_voltage_v"),
                    Some(&FieldValue::F64(f64::from(voltage) / 1000.))
                );
                assert!(crate::field(row, "MAVLINK.raw_frame_hex").is_some());
            }
            assert!(TlogReader.read(&p, &bytes[..bytes.len() - 1]).is_err());
        }
        let mut wrong = p;
        wrong.default_clock_basis = crate::ClockBasis::HostReceived;
        assert!(TlogReader.read(&wrong, &[]).is_err());
    }

    #[test]
    fn explicit_gcs_recorded_clock_preserves_units_without_receipt_or_anchor_claim() {
        let mut p = parse_profile(PROFILE).unwrap();
        p.fields.time = "host_recorded_us".into();
        p.default_clock_basis = crate::ClockBasis::Unknown;
        for (v2, millivolts) in [(false, 12_300_u16), (true, 24_600)] {
            let mut payload = [0_u8; 31];
            payload[14..16].copy_from_slice(&millivolts.to_le_bytes());
            payload[16..18].copy_from_slice(&120_i16.to_le_bytes());
            payload[30] = 50;
            let mut bytes = framed(1, &payload, v2);
            let mut time = 1_700_000_000_000_000_u64.to_le_bytes().to_vec();
            time.extend_from_slice(&42_u32.to_le_bytes());
            bytes.extend(framed(2, &time, v2));
            let rows = TlogReader.read(&p, &bytes).unwrap();
            assert_eq!(rows.len(), 2);
            assert_eq!(
                crate::field(&rows[0], "battery_voltage_v"),
                Some(&FieldValue::F64(f64::from(millivolts) / 1000.0))
            );
            for row in &rows {
                assert_eq!(row.clock_basis, crate::ClockBasis::Unknown);
                assert_eq!(row.t_ms, 1_700_000_000_123);
                assert!(row.anchor_unix_us.is_none());
                assert!(crate::field(row, "MAVLINK.host_received_us").is_none());
                assert_eq!(
                    crate::field(row, "MAVLINK.host_recorded_us"),
                    Some(&FieldValue::Text("1700000000123456".into()))
                );
                assert_eq!(
                    crate::field(row, "MAVLINK.direction"),
                    Some(&FieldValue::Text("UNKNOWN_NOT_ENCODED_IN_RECORD".into()))
                );
            }
            assert!(crate::field(&rows[1], "SYSTEM_TIME.time_unix_usec").is_some());
            assert!(TlogReader.read(&p, &bytes[..bytes.len() - 1]).is_err());
        }
        p.default_clock_basis = crate::ClockBasis::HostReceived;
        assert!(TlogReader.read(&p, &[]).is_err());
        p.default_clock_basis = crate::ClockBasis::Unknown;
        p.fields.time = "host_us".into();
        assert!(TlogReader.read(&p, &[]).is_err());
    }

    #[test]
    fn scaled_imu_instances_reuse_units_and_keep_temperature_uncertainty() {
        let profile = parse_profile(PROFILE).unwrap();
        for (id, index, crc) in [(26, 1, 170), (116, 2, 76), (129, 3, 46)] {
            assert_eq!(crc_extra(id), Some(crc));
            for sample in [1000i16, -2000] {
                let mut payload = [0u8; 24];
                payload[..4].copy_from_slice(&1234u32.to_le_bytes());
                for offset in (4..22).step_by(2) {
                    payload[offset..offset + 2].copy_from_slice(&sample.to_le_bytes());
                }
                for v2 in [false, true] {
                    for temperature in [0i16, 1, -500, 2000] {
                        payload[22..].copy_from_slice(&temperature.to_le_bytes());
                        let selected = if v2 { &payload[..] } else { &payload[..22] };
                        let rows = TlogReader
                            .read(&profile, &framed(id, selected, v2))
                            .unwrap();
                        assert_eq!(rows.len(), 1);
                        assert_eq!(rows[0].t_boot_us, Some(1_234_000));
                        assert_eq!(
                            crate::field(&rows[0], "imu_sensor_index_reported"),
                            Some(&FieldValue::I64(index))
                        );
                        for (quantity, scale) in [
                            ("acceleration_m_s2", 9.80665 / 1000.0),
                            ("angular_velocity_rad_s", 0.001),
                            ("magnetic_field_t", 1e-7),
                        ] {
                            for axis in ["x", "y", "z"] {
                                assert_eq!(
                                    crate::field(
                                        &rows[0],
                                        &format!("scaled_imu_{quantity}_{axis}")
                                    ),
                                    Some(&FieldValue::F64(sample as f64 * scale))
                                );
                            }
                        }
                        let expected = if !v2 || temperature == 0 || temperature == 1 {
                            FieldValue::Blank
                        } else {
                            FieldValue::F64(temperature as f64 / 100.0 + 273.15)
                        };
                        assert_eq!(crate::field(&rows[0], "imu_temperature_k"), Some(&expected));
                        assert!(rows[0].anchor_unix_us.is_none());
                    }
                }
                payload[22..].copy_from_slice(&(-27316i16).to_le_bytes());
                assert!(
                    TlogReader
                        .read(&profile, &framed(id, &payload, true))
                        .is_err()
                );
                assert!(
                    TlogReader
                        .read(&profile, &framed(id, &[0; 25], true))
                        .is_err()
                );
                assert!(
                    TlogReader
                        .read(&profile, &framed(id, &[0; 21], false))
                        .is_err()
                );
            }
        }
    }

    #[test]
    fn pressure_instances_share_units_without_inventing_altitude_or_missing_temperature() {
        let profile = parse_profile(PROFILE).unwrap();
        for (id, index, crc) in [(29, 1, 115), (137, 2, 195), (143, 3, 131)] {
            assert_eq!(crc_extra(id), Some(crc));
            for (absolute, differential, temperature) in
                [(1013.25f32, -2.5f32, 2000i16), (900.0, 0.0, -500)]
            {
                let mut payload = [0u8; 16];
                payload[..4].copy_from_slice(&1234u32.to_le_bytes());
                payload[4..8].copy_from_slice(&absolute.to_le_bytes());
                payload[8..12].copy_from_slice(&differential.to_le_bytes());
                payload[12..14].copy_from_slice(&temperature.to_le_bytes());
                for v2 in [false, true] {
                    for extra in [0i16, 1, -100, 2500] {
                        payload[14..].copy_from_slice(&extra.to_le_bytes());
                        let selected = if v2 { &payload[..] } else { &payload[..14] };
                        let rows = TlogReader
                            .read(&profile, &framed(id, selected, v2))
                            .unwrap();
                        assert_eq!(rows.len(), 1);
                        assert_eq!(rows[0].t_boot_us, Some(1_234_000));
                        assert_eq!(
                            crate::field(&rows[0], "pressure_sensor_index_reported"),
                            Some(&FieldValue::I64(index))
                        );
                        assert_eq!(
                            crate::field(&rows[0], "pressure_absolute_pa"),
                            Some(&FieldValue::F64(absolute as f64 * 100.0))
                        );
                        assert_eq!(
                            crate::field(&rows[0], "pressure_differential_pa"),
                            Some(&FieldValue::F64(differential as f64 * 100.0))
                        );
                        assert_eq!(
                            crate::field(&rows[0], "pressure_temperature_k"),
                            Some(&FieldValue::F64(temperature as f64 / 100.0 + 273.15))
                        );
                        let expected = if !v2 || extra == 0 || extra == 1 {
                            FieldValue::Blank
                        } else {
                            FieldValue::F64(extra as f64 / 100.0 + 273.15)
                        };
                        assert_eq!(
                            crate::field(&rows[0], "differential_temperature_k"),
                            Some(&expected)
                        );
                        assert!(rows[0].anchor_unix_us.is_none());
                    }
                }
                for offset in [4, 8] {
                    let mut bad = payload;
                    bad[offset..offset + 4].copy_from_slice(&f32::NAN.to_le_bytes());
                    assert!(TlogReader.read(&profile, &framed(id, &bad, true)).is_err());
                }
                for offset in [12, 14] {
                    let mut bad = payload;
                    bad[offset..offset + 2].copy_from_slice(&(-27316i16).to_le_bytes());
                    assert!(TlogReader.read(&profile, &framed(id, &bad, true)).is_err());
                }
                assert!(
                    TlogReader
                        .read(&profile, &framed(id, &[0; 17], true))
                        .is_err()
                );
                assert!(
                    TlogReader
                        .read(&profile, &framed(id, &[0; 13], false))
                        .is_err()
                );
            }
        }
    }

    #[test]
    fn recorded_distance_preserves_units_quality_and_source_without_world_transform() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut payload = [0u8; 39];
        payload[..4].copy_from_slice(&1234u32.to_le_bytes());
        payload[4..6].copy_from_slice(&20u16.to_le_bytes());
        payload[6..8].copy_from_slice(&1000u16.to_le_bytes());
        payload[8..10].copy_from_slice(&125u16.to_le_bytes());
        payload[11] = 7;
        payload[12] = 250; // Unknown direction code is preserved, not rotated.
        payload[13] = 25;
        payload[14..18].copy_from_slice(&0.5f32.to_le_bytes());
        payload[22..26].copy_from_slice(&1.0f32.to_le_bytes());
        payload[38] = 75;
        for v2 in [false, true] {
            let input = if v2 { &payload[..] } else { &payload[..14] };
            let rows = TlogReader.read(&profile, &framed(132, input, v2)).unwrap();
            assert_eq!(rows[0].t_boot_us, Some(1_234_000));
            for (name, value) in [
                ("reported_distance_m", 1.25),
                ("min_distance_m", 0.2),
                ("variance_m2", 0.0025),
            ] {
                assert_eq!(
                    crate::field(&rows[0], &format!("DISTANCE_SENSOR.{name}")),
                    Some(&FieldValue::F64(value))
                );
            }
            assert_eq!(
                crate::field(&rows[0], "DISTANCE_SENSOR.sensor_id"),
                Some(&FieldValue::I64(7))
            );
            assert_eq!(
                crate::field(&rows[0], "DISTANCE_SENSOR.orientation_code"),
                Some(&FieldValue::I64(250))
            );
            assert_eq!(
                crate::field(&rows[0], "DISTANCE_SENSOR.horizontal_fov_rad"),
                Some(&if v2 {
                    FieldValue::F64(0.5)
                } else {
                    FieldValue::Blank
                })
            );
        }
        payload[13] = 255;
        payload[38] = 1;
        let rows = TlogReader
            .read(&profile, &framed(132, &payload, true))
            .unwrap();
        assert_eq!(
            crate::field(&rows[0], "DISTANCE_SENSOR.variance_m2"),
            Some(&FieldValue::Blank)
        );
        assert_eq!(
            crate::field(&rows[0], "DISTANCE_SENSOR.range_state"),
            Some(&FieldValue::Text("INVALID_SIGNAL".into()))
        );
        payload[38] = 0;
        payload[8..10].copy_from_slice(&1100u16.to_le_bytes());
        let rows = TlogReader
            .read(&profile, &framed(132, &payload, true))
            .unwrap();
        assert_eq!(
            crate::field(&rows[0], "DISTANCE_SENSOR.range_state"),
            Some(&FieldValue::Text("OUTSIDE_DECLARED_RANGE".into()))
        );
        for (offset, value) in [(38, 101), (14, 255)] {
            let mut invalid = payload;
            invalid[offset] = value;
            if offset == 14 {
                invalid[14..18].copy_from_slice(&f32::NAN.to_le_bytes());
            }
            assert!(
                TlogReader
                    .read(&profile, &framed(132, &invalid, true))
                    .is_err()
            );
        }
        assert!(
            TlogReader
                .read(&profile, &framed(132, &[0; 40], true))
                .is_err()
        );
        assert!(TlogReader.read(&profile, &framed(132, &[1], true)).is_ok());
        assert!(
            TlogReader
                .read(&profile, &framed(132, &[1], false))
                .is_err()
        );
        let mut corrupt = framed(132, &payload, true);
        *corrupt.last_mut().unwrap() ^= 1;
        assert!(TlogReader.read(&profile, &corrupt).is_err());
    }

    #[test]
    fn motion_messages_preserve_units_frames_and_boot_time() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut payload = 1234u32.to_le_bytes().to_vec();
        for value in [0.25f32, -0.5, 1.0, 2.0, -3.0, 4.0] {
            payload.extend_from_slice(&value.to_le_bytes());
        }
        for id in [30, 32] {
            for v2 in [false, true] {
                let rows = TlogReader
                    .read(&profile, &framed(id, &payload, v2))
                    .unwrap();
                assert_eq!(rows[0].t_boot_us, Some(1_234_000));
                assert_eq!(rows[0].clock_basis, crate::ClockBasis::HostReceived);
                let key = if id == 30 {
                    "ATTITUDE.roll_rad"
                } else {
                    "LOCAL_POSITION_NED.north_m"
                };
                assert_eq!(crate::field(&rows[0], key), Some(&FieldValue::F64(0.25)));
            }
            assert!(TlogReader.read(&profile, &framed(id, &[1], true)).is_ok());
            assert!(TlogReader.read(&profile, &framed(id, &[1], false)).is_err());
            assert!(
                TlogReader
                    .read(&profile, &framed(id, &[0; 29], true))
                    .is_err()
            );
            let mut invalid = payload.clone();
            invalid[4..8].copy_from_slice(&f32::NAN.to_le_bytes());
            assert!(
                TlogReader
                    .read(&profile, &framed(id, &invalid, true))
                    .is_err()
            );
            let mut corrupt = framed(id, &payload, true);
            *corrupt.last_mut().unwrap() ^= 1;
            assert!(TlogReader.read(&profile, &corrupt).is_err());
        }
    }

    #[test]
    fn battery_status_preserves_units_missing_slots_and_multiple_ids() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut payload = [0u8; 54];
        payload[..4].copy_from_slice(&1250i32.to_le_bytes());
        payload[4..8].copy_from_slice(&36i32.to_le_bytes());
        payload[8..10].copy_from_slice(&2500i16.to_le_bytes());
        for index in 0..10 {
            payload[10 + index * 2..12 + index * 2].copy_from_slice(&65535u16.to_le_bytes());
        }
        payload[10..12].copy_from_slice(&12000u16.to_le_bytes());
        payload[30..32].copy_from_slice(&250i16.to_le_bytes());
        payload[35] = 75;
        payload[36..40].copy_from_slice(&60i32.to_le_bytes());
        payload[40] = 5;
        payload[41..43].copy_from_slice(&1u16.to_le_bytes());
        for (id, v2) in [(1, false), (2, true)] {
            payload[32] = id;
            let bytes = if v2 { &payload[..] } else { &payload[..36] };
            let rows = TlogReader.read(&profile, &framed(147, bytes, v2)).unwrap();
            assert_eq!(
                crate::field(&rows[0], "battery_id_reported"),
                Some(&FieldValue::I64(id.into()))
            );
            for (name, expected) in [
                ("battery_consumed_ah", 1.25),
                ("battery_consumed_j", 3600.0),
                ("battery_current_a", 2.5),
                ("battery_temperature_k", 298.15),
                ("battery_remaining_fraction", 0.75),
                ("battery_voltage_slot_v_1", 12.0),
            ] {
                assert_eq!(
                    crate::field(&rows[0], name),
                    Some(&FieldValue::F64(expected))
                );
            }
            assert_eq!(
                crate::field(&rows[0], "battery_voltage_slot_v_2"),
                Some(&FieldValue::Blank)
            );
            assert_eq!(
                crate::field(&rows[0], "battery_voltage_slot_v_11"),
                Some(&FieldValue::Blank)
            );
            assert_eq!(rows[0].clock_basis, crate::ClockBasis::HostReceived);
        }
        payload[..8].fill(255);
        payload[8..10].copy_from_slice(&32767i16.to_le_bytes());
        payload[30..32].copy_from_slice(&(-1i16).to_le_bytes());
        payload[35] = 255;
        let rows = TlogReader
            .read(&profile, &framed(147, &payload, true))
            .unwrap();
        for name in [
            "battery_current_a",
            "battery_consumed_ah",
            "battery_consumed_j",
            "battery_temperature_k",
            "battery_remaining_fraction",
        ] {
            assert_eq!(crate::field(&rows[0], name), Some(&FieldValue::Blank));
        }
        let mut bad = payload;
        bad[35] = 101;
        assert!(TlogReader.read(&profile, &framed(147, &bad, true)).is_err());
        assert!(
            TlogReader
                .read(&profile, &framed(147, &[0; 55], true))
                .is_err()
        );
        assert!(
            TlogReader
                .read(&profile, &framed(147, &[0; 35], false))
                .is_err()
        );
        let mut corrupt = framed(147, &payload, true);
        *corrupt.last_mut().unwrap() ^= 1;
        assert!(TlogReader.read(&profile, &corrupt).is_err());
    }

    #[test]
    fn wire_version_and_clock_range_do_not_wrap_into_false_time() {
        let profile = parse_profile(PROFILE).unwrap();
        for id in [2, 24] {
            let mut payload = vec![0; if id == 2 { 12 } else { 30 }];
            for v2 in [false, true] {
                for timestamp in [0, 1_700_000_000_000_000, i64::MAX as u64] {
                    payload[..8].copy_from_slice(&timestamp.to_le_bytes());
                    let rows = TlogReader
                        .read(&profile, &framed(id, &payload, v2))
                        .unwrap();
                    assert_eq!(
                        crate::field(&rows[0], "MAVLINK.wire_version"),
                        Some(&FieldValue::I64(if v2 { 2 } else { 1 }))
                    );
                    let key = if id == 2 {
                        "SYSTEM_TIME.time_unix_usec"
                    } else {
                        "GPS_RAW_INT.time_usec"
                    };
                    assert_eq!(
                        crate::field(&rows[0], key),
                        Some(&FieldValue::I64(timestamp as i64))
                    );
                    if id == 2 {
                        assert_eq!(
                            rows[0].anchor_unix_us,
                            if timestamp == 0 {
                                None
                            } else {
                                Some(timestamp as i64)
                            }
                        );
                    }
                }
                for timestamp in [i64::MAX as u64 + 1, u64::MAX] {
                    payload[..8].copy_from_slice(&timestamp.to_le_bytes());
                    assert!(
                        TlogReader
                            .read(&profile, &framed(id, &payload, v2))
                            .is_err()
                    );
                }
            }
        }
    }

    #[test]
    fn reported_firmware_and_uid_do_not_become_authenticated_identity() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut payload = [0u8; 78];
        payload[0..8].copy_from_slice(&u64::MAX.to_le_bytes());
        payload[8..16].copy_from_slice(&u64::MAX.to_le_bytes());
        payload[16..20].copy_from_slice(&0x04050341u32.to_le_bytes());
        payload[32..34].copy_from_slice(&123u16.to_le_bytes());
        payload[36..44].copy_from_slice(&[1, 2, 3, 4, 5, 6, 7, 8]);
        for v2 in [false, true] {
            let data = if v2 { &payload[..] } else { &payload[..60] };
            let rows = TlogReader.read(&profile, &framed(148, data, v2)).unwrap();
            for (key, value) in [
                ("major", 4),
                ("minor", 5),
                ("patch", 3),
                ("release_type_code", 65),
                ("vendor_id", 123),
            ] {
                assert_eq!(
                    crate::field(&rows[0], &format!("AUTOPILOT_VERSION.{key}")),
                    Some(&FieldValue::I64(value))
                );
            }
            for (key, value) in [
                ("release_channel_reported", "ALPHA"),
                ("uid_preference_reported", "UID"),
                ("uid_hex", "ffffffffffffffff"),
                ("flight_custom_bytes_hex", "0102030405060708"),
                ("identity_basis", "SELF_REPORTED_NOT_AUTHENTICATED"),
            ] {
                assert_eq!(
                    crate::field(&rows[0], &format!("AUTOPILOT_VERSION.{key}")),
                    Some(&FieldValue::Text(value.into()))
                );
            }
            assert_eq!(rows[0].t_boot_us, None);
        }
        payload[60] = 1;
        let rows = TlogReader
            .read(&profile, &framed(148, &payload[..61], true))
            .unwrap();
        assert_eq!(
            crate::field(&rows[0], "AUTOPILOT_VERSION.uid_preference_reported"),
            Some(&FieldValue::Text("UID2".into()))
        );
        let rows = TlogReader.read(&profile, &framed(148, &[0], true)).unwrap();
        assert_eq!(
            crate::field(&rows[0], "AUTOPILOT_VERSION.uid_preference_reported"),
            Some(&FieldValue::Text("NOT_PROVIDED".into()))
        );
        assert!(
            TlogReader
                .read(&profile, &framed(148, &[0; 59], false))
                .is_err()
        );
        assert!(
            TlogReader
                .read(&profile, &framed(148, &[0; 79], true))
                .is_err()
        );
        let mut corrupt = framed(148, &payload, true);
        *corrupt.last_mut().unwrap() ^= 1;
        assert!(TlogReader.read(&profile, &corrupt).is_err());
    }

    #[test]
    fn state_units_unknown_frames_and_sender_survive_the_same_reader() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut heartbeat = [0; 9];
        heartbeat[6] = 128;
        heartbeat[7] = 4;
        heartbeat[8] = 3;
        let mut sys = [0; 31];
        sys[14..16].copy_from_slice(&24000u16.to_le_bytes());
        sys[16..18].copy_from_slice(&200i16.to_le_bytes());
        sys[18..20].copy_from_slice(&250u16.to_le_bytes());
        sys[30] = 75;
        let unknown = framed(20000, &[0x12, 0x34, 0x56], true);
        let bytes = [
            framed(0, &heartbeat, false),
            framed(1, &sys, false),
            framed(42, &[2, 0, 10, 0, 3, 1], true),
            unknown.clone(),
        ]
        .concat();
        let obs = TlogReader.read(&profile, &bytes).unwrap();
        assert_eq!(obs.len(), 4);
        assert_eq!(
            crate::field(&obs[0], "HEARTBEAT.reported_armed"),
            Some(&FieldValue::I64(1))
        );
        assert_eq!(
            crate::field(&obs[1], "battery_voltage_v").unwrap().as_f64(),
            Some(24.)
        );
        assert_eq!(
            crate::field(&obs[1], "battery_current_a").unwrap().as_f64(),
            Some(2.)
        );
        assert_eq!(
            crate::field(&obs[1], "communication_drop_fraction")
                .unwrap()
                .as_f64(),
            Some(0.025)
        );
        assert_eq!(
            crate::field(&obs[2], "mission_state_reported"),
            Some(&FieldValue::Text("ACTIVE".into()))
        );
        assert_eq!(
            crate::field(&obs[3], "MAVLINK.crc_verified"),
            Some(&FieldValue::I64(0))
        );
        assert_eq!(
            crate::field(&obs[3], "MAVLINK.raw_frame_hex"),
            Some(&FieldValue::Text(
                unknown[8..].iter().map(|b| format!("{b:02x}")).collect()
            ))
        );
        for o in obs {
            assert_eq!(
                crate::field(&o, "MAVLINK.system_id"),
                Some(&FieldValue::I64(42))
            );
            assert_eq!(
                crate::field(&o, "MAVLINK.component_id"),
                Some(&FieldValue::I64(3))
            );
            assert_eq!(o.clock_basis, crate::ClockBasis::HostReceived);
        }
    }

    #[test]
    fn sensor_report_bits_preserve_declared_extensions_without_safety_inference() {
        let profile = parse_profile(PROFILE).unwrap();
        for (present, enabled, healthy) in [
            (0x8000_0001_u32, 2_u32, 4_u32),
            (0xc001_0000, 0x1000_0000, 0x1000_0000),
        ] {
            let mut payload = [0_u8; 43];
            payload[..4].copy_from_slice(&present.to_le_bytes());
            payload[4..8].copy_from_slice(&enabled.to_le_bytes());
            payload[8..12].copy_from_slice(&healthy.to_le_bytes());
            payload[12..14].copy_from_slice(&500_u16.to_le_bytes());
            payload[31..35].copy_from_slice(&0x8000_0081_u32.to_le_bytes());
            payload[35..39].copy_from_slice(&2_u32.to_le_bytes());
            payload[39..43].copy_from_slice(&4_u32.to_le_bytes());
            let rows = TlogReader
                .read(&profile, &framed(1, &payload, true))
                .unwrap();
            for (name, value) in [
                ("gyro3d.present_reported", i64::from(present & 1 != 0)),
                ("accel3d.enabled_reported", i64::from(enabled & 2 != 0)),
                ("mag3d.healthy_reported", i64::from(healthy & 4 != 0)),
                (
                    "prearm_check.healthy_reported",
                    i64::from(healthy & 0x1000_0000 != 0),
                ),
                ("recovery_system.present_reported", 1),
                ("mag3d_4.present_reported", 1),
                ("leak.enabled_reported", 1),
                ("gyro3d_3.healthy_reported", 1),
                ("present_extended_unknown_bits", 0x8000_0000),
            ] {
                assert_eq!(
                    crate::field(&rows[0], &format!("SYS_STATUS.{name}")),
                    Some(&FieldValue::I64(value))
                );
            }
            assert_eq!(
                crate::field(&rows[0], "SYS_STATUS.mainloop_load_fraction_reported"),
                Some(&FieldValue::F64(0.5))
            );
            assert_eq!(rows[0].t_boot_us, None);
            let v1 = TlogReader
                .read(&profile, &framed(1, &payload[..31], false))
                .unwrap();
            assert_eq!(
                crate::field(&v1[0], "SYS_STATUS.leak.present_reported"),
                Some(&FieldValue::Blank)
            );
            payload[3] &= 0x7f;
            let undeclared = TlogReader
                .read(&profile, &framed(1, &payload, true))
                .unwrap();
            assert_eq!(
                crate::field(&undeclared[0], "SYS_STATUS.present_extended_raw"),
                Some(&FieldValue::I64(0x8000_0081))
            );
            assert_eq!(
                crate::field(&undeclared[0], "SYS_STATUS.leak.present_reported"),
                Some(&FieldValue::Blank)
            );
            payload[12..14].copy_from_slice(&1001_u16.to_le_bytes());
            assert!(
                TlogReader
                    .read(&profile, &framed(1, &payload, true))
                    .is_err()
            );
        }
        assert!(
            TlogReader
                .read(&profile, &framed(1, &[0; 44], true))
                .is_err()
        );
        let short = TlogReader.read(&profile, &framed(1, &[0], true)).unwrap();
        assert_eq!(
            crate::field(&short[0], "SYS_STATUS.extended_declared_reported"),
            Some(&FieldValue::I64(0))
        );
    }

    #[test]
    fn unavailable_v2_extension_and_wrong_profile_do_not_become_claims() {
        let mut profile = parse_profile(PROFILE).unwrap();
        let obs = TlogReader.read(&profile, &framed(42, &[2], true)).unwrap();
        assert_eq!(
            crate::field(&obs[0], "mission_state_reported"),
            Some(&FieldValue::Text("UNKNOWN".into()))
        );
        profile.default_clock_basis = crate::ClockBasis::BootRelative;
        assert!(TlogReader.read(&profile, &framed(42, &[2], true)).is_err());
    }

    #[test]
    fn sys_status_missing_and_invalid_percent_are_distinct() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut payload = [0; 31];
        payload[14..18].fill(255);
        payload[30] = 255;
        let obs = TlogReader
            .read(&profile, &framed(1, &payload, true))
            .unwrap();
        for name in [
            "battery_voltage_v",
            "battery_current_a",
            "battery_remaining_fraction",
        ] {
            assert_eq!(crate::field(&obs[0], name), Some(&FieldValue::Blank));
        }
        payload[30] = 101;
        assert!(
            TlogReader
                .read(&profile, &framed(1, &payload, true))
                .is_err()
        );
    }

    #[test]
    fn signature_is_retained_but_never_authenticated_and_unknown_flags_reject() {
        let profile = parse_profile(PROFILE).unwrap();
        let mut bytes = framed(42, &[2], true);
        bytes[10] = 1; // incompatibility flag, after eight-byte receipt timestamp
        let crc = crc_x25(&[28], crc_x25(&bytes[9..19], 0xffff));
        bytes[19..21].copy_from_slice(&crc.to_le_bytes());
        bytes.extend_from_slice(&[7; 13]);
        let obs = TlogReader.read(&profile, &bytes).unwrap();
        assert_eq!(
            crate::field(&obs[0], "MAVLINK.signature"),
            Some(&FieldValue::Text("present_unverified".into()))
        );
        bytes[10] = 2;
        assert!(TlogReader.read(&profile, &bytes).is_err());
    }
}
