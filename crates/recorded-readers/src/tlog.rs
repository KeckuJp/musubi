use crate::config::{ChannelId, ReadProfile};
use crate::mavlink_header::parse_mavlink2_header;

use crate::{FieldValue, Observation, ProfileReader, ReadError, observation};

#[derive(Debug, Clone, Copy, Default)]
pub struct TlogReader;

const MAVLINK1_MAGIC: u8 = 0xFE;
const MAVLINK2_MAGIC: u8 = 0xFD;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DefiningDialect {
    Minimal,
    Standard,
    Common,
    ArduPilotMega,
}

impl DefiningDialect {
    #[must_use]
    pub const fn file(self) -> &'static str {
        match self {
            Self::Minimal => "minimal.xml",
            Self::Standard => "standard.xml",
            Self::Common => "common.xml",
            Self::ArduPilotMega => "ardupilotmega.xml",
        }
    }

    #[must_use]
    pub const fn inherited_from_common_chain(self) -> bool {
        matches!(self, Self::Minimal | Self::Standard | Self::Common)
    }

    #[must_use]
    pub const fn scope(self) -> &'static str {
        if self.inherited_from_common_chain() {
            "COMMON_INCLUDE_CHAIN_DEFINITION_INHERITED_UNCHANGED_BY_EVERY_DIALECT_THAT_INCLUDES_IT"
        } else {
            "VENDOR_DIALECT_DEFINITION_NOT_PRESENT_IN_THE_COMMON_INCLUDE_CHAIN"
        }
    }
}

pub const DIALECT_DEFINITION_PIN: &str = "mavlink/mavlink@3203f89c510337c0088244735c6a5056c52b5a28";

pub const DIALECT_UNQUALIFIED: &str =
    "UNQUALIFIED_MSGID_NOT_IN_THE_PINNED_TABLE_NO_DIALECT_CLAIMED";

#[must_use]
pub const fn defining_dialect(msgid: u32) -> Option<DefiningDialect> {
    match msgid {
        0 => Some(DefiningDialect::Minimal),
        33 | 148 => Some(DefiningDialect::Standard),
        1 | 2 | 24 | 26 | 29 | 30 | 32 | 42 | 46 | 65 | 109 | 116 | 129 | 132 | 137 | 143 | 147
        | 253 | 262 | 270 => Some(DefiningDialect::Common),
        193 => Some(DefiningDialect::ArduPilotMega),
        _ => None,
    }
}

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
                f("GPS_RAW_INT.vel", u16_at(p, 24)),
                f("GPS_RAW_INT.fix_type", u8_at(p, 28)),
                f("GPS_RAW_INT.satellites_visible", u8_at(p, 29)),
                (
                    "gps_fix_type_reported".into(),
                    FieldValue::Text(
                        match u8_at(p, 28) {
                            0 => "NO_GPS",
                            1 => "NO_FIX",
                            2 => "2D_FIX",
                            3 => "3D_FIX",
                            4 => "DGPS",
                            5 => "RTK_FLOAT",
                            6 => "RTK_FIXED",
                            7 => "STATIC",
                            8 => "PPP",
                            _ => "UNKNOWN_RETAINED",
                        }
                        .into(),
                    ),
                ),
                (
                    "gps_satellites_visible_reported".into(),
                    if u8_at(p, 29) == 255 {
                        FieldValue::Blank
                    } else {
                        FieldValue::I64(u8_at(p, 29))
                    },
                ),
                (
                    "gps_ground_speed_m_s_reported".into(),
                    if u16_at(p, 24) == 65535 {
                        FieldValue::Blank
                    } else {
                        FieldValue::F64(u16_at(p, 24) as f64 / 100.0)
                    },
                ),
                (
                    "gps_hdop_reported".into(),
                    if u16_at(p, 20) == 65535 {
                        FieldValue::Blank
                    } else {
                        FieldValue::F64(u16_at(p, 20) as f64 / 100.0)
                    },
                ),
            ];
            for (name, value) in [
                (
                    "gps_rtk_status_basis",
                    "REPORTED_FIX_TYPE_ONLY_CORRECTION_LINK_BASE_STATION_AND_AUTHENTICATION_ABSENT_FROM_THIS_MESSAGE_WHILE_UNCERTAINTY_EXTENSION_FIELDS_EXIST_BUT_ARE_NOT_DECODED_IN_THIS_PATH",
                ),
                (
                    "gps_channel_basis",
                    "SHARED_CHANNEL_NAME_ONLY_NOT_A_FUSED_OR_EKF_SOLUTION",
                ),
            ] {
                d.fields.push((name.into(), FieldValue::Text(value.into())));
            }
            let latitude = i32_at(p, 8) as f64 / 1e7;
            let longitude = i32_at(p, 12) as f64 / 1e7;
            let latitude_in_domain = (-90.0..=90.0).contains(&latitude);
            let longitude_in_domain = (-180.0..=180.0).contains(&longitude);
            for (name, value, in_domain) in [
                ("gps_latitude_deg_reported", latitude, latitude_in_domain),
                ("gps_longitude_deg_reported", longitude, longitude_in_domain),
            ] {
                d.fields.push((
                    name.into(),
                    if in_domain {
                        FieldValue::F64(value)
                    } else {
                        FieldValue::Blank
                    },
                ));
            }
            d.fields.push((
                "gps_coordinate_domain_disposition".into(),
                FieldValue::Text(
                    match (latitude_in_domain, longitude_in_domain) {
                        (true, true) => "WITHIN_DECLARED_DEGREE_DOMAIN",
                        (false, true) => "LATITUDE_OUTSIDE_DEGREE_DOMAIN_DERIVED_VALUE_WITHHELD",
                        (true, false) => "LONGITUDE_OUTSIDE_DEGREE_DOMAIN_DERIVED_VALUE_WITHHELD",
                        (false, false) => "BOTH_OUTSIDE_DEGREE_DOMAIN_DERIVED_VALUES_WITHHELD",
                    }
                    .into(),
                ),
            ));
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
                f("RADIO_STATUS.fixed", u16_at(p, 2)),
                f("RADIO_STATUS.rssi", u8_at(p, 4)),
                f("RADIO_STATUS.remrssi", u8_at(p, 5)),
                f("RADIO_STATUS.txbuf", u8_at(p, 6)),
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
        let selected = profile.declared_sender;
        let mut unselected_records = 0usize;
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
                    "MAVLINK.defining_dialect".into(),
                    FieldValue::Text(match defining_dialect(msgid) {
                        Some(dialect) => dialect.file().into(),
                        None => DIALECT_UNQUALIFIED.into(),
                    }),
                ),
                (
                    "MAVLINK.defining_dialect_pin".into(),
                    FieldValue::Text(match defining_dialect(msgid) {
                        Some(_) => DIALECT_DEFINITION_PIN.into(),
                        None => DIALECT_UNQUALIFIED.into(),
                    }),
                ),
                (
                    "MAVLINK.defining_dialect_scope".into(),
                    FieldValue::Text(match defining_dialect(msgid) {
                        Some(dialect) => dialect.scope().into(),
                        None => DIALECT_UNQUALIFIED.into(),
                    }),
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
            if let Some(want) = selected {
                if source_key.0 == 0 || source_key.1 == 0 {
                    return Err(ReadError::Malformed {
                        offset: off + 8,
                        what: "record reports no addressable sender while one sender is selected"
                            .into(),
                    });
                }
                if (source_key.0, source_key.1) != (want.system_id, want.component_id) {
                    unselected_records += 1;
                    off += 8 + total;
                    continue;
                }
                d.fields.push((
                    "MAVLINK.declared_sender_selection".into(),
                    FieldValue::Text(format!(
                        "SELECTED_DECLARED_SENDER_{}_{}_REPORTED_NOT_AUTHENTICATED_THIS_READ_IS_A_SUBSET_OF_THE_RECORDING",
                        want.system_id, want.component_id
                    )),
                ));
            }
            let t_ms = i64::try_from(host_us / 1000).unwrap_or(i64::MAX);
            let mut o = observation(profile, t_ms, d.channel, d.fields, d.stale);
            o.t_boot_us = d.t_boot_us;
            o.anchor_unix_us = if recorded { None } else { d.anchor_unix_us };
            out.push(o);
            off += 8 + total;
        }
        let emitted = out.len();
        if let Some(want) = selected {
            if let Some(first) = out.first_mut() {
                first.fields.extend([
                    (
                        "MAVLINK.selected_sender_records".into(),
                        FieldValue::I64(i64::try_from(emitted).unwrap_or(i64::MAX)),
                    ),
                    (
                        "MAVLINK.unselected_sender_records".into(),
                        FieldValue::I64(i64::try_from(unselected_records).unwrap_or(i64::MAX)),
                    ),
                    (
                        "MAVLINK.selected_sender_record_scope".into(),
                        FieldValue::Text(
                            "WHOLE_RECORDING_COUNTS_ON_THE_FIRST_SELECTED_ROW_EVERY_VALIDATED_RECORD_INCLUDING_UNKNOWN_MESSAGE_TYPES_COUNTED_ONCE_THIS_READ_IS_A_SUBSET_OF_THE_RECORDING"
                                .into(),
                        ),
                    ),
                ]);
                first.digest = crate::observation_digest(first.t_ms, first.channel, &first.fields);
            }
            if out.is_empty() {
                return Err(ReadError::Profile(format!(
                    "selected sender {}/{} reports no record in this recording ({unselected_records} \
                     records report other senders)",
                    want.system_id, want.component_id
                )));
            }
        }
        Ok(out)
    }
}
