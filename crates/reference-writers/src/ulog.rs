use musubi_reference_scenario::FcLogFormat;

use crate::{EventKind, FamilyWriter, SourceRole, Timeline, WriteError, fc_events_for};

pub const MAGIC: [u8; 8] = [0x55, 0x4C, 0x6F, 0x67, 0x01, 0x12, 0x35, 0x01];

pub const TOPICS: [(&str, &str); 8] = [
    (
        "vehicle_status",
        "uint64_t timestamp;uint32_t nav_state;uint8_t arming_state;uint8_t failsafe;uint8_t gcs_connection_lost;uint8_t nav_state_user_intention;",
    ),
    (
        "battery_status",
        "uint64_t timestamp;float voltage_v;float current_a;float remaining;uint8_t warning;",
    ),
    (
        "sensor_gps",
        "uint64_t timestamp;uint64_t time_utc_usec;int32_t lat;int32_t lon;int32_t alt;float hdop;float vdop;float eph;float epv;uint8_t fix_type;uint8_t satellites_used;uint8_t jamming_state;uint8_t jamming_indicator;",
    ),
    (
        "input_rc",
        "uint64_t timestamp;uint64_t timestamp_last_signal;float rssi_dbm;int32_t rssi;uint16_t[4] values;int8_t link_quality;bool rc_lost;bool rc_failsafe;",
    ),
    (
        "estimator_status",
        "uint64_t timestamp;float pos_horiz_accuracy;float pos_test_ratio;float vel_test_ratio;float hgt_test_ratio;float mag_test_ratio;uint16_t gps_check_fail_flags;uint16_t innovation_check_flags;uint8_t reset_count_pos_ne;",
    ),
    (
        "failsafe_flags",
        "uint64_t timestamp;bool manual_control_signal_lost;bool gcs_connection_lost;bool gnss_lost;bool global_position_invalid;",
    ),
    (
        "telemetry_status",
        "uint64_t timestamp;float rx_message_lost_rate;bool heartbeat_type_gcs;",
    ),
    (
        "vehicle_global_position",
        "uint64_t timestamp;double lat;double lon;float alt;",
    ),
];

const VEHICLE_STATUS: u16 = 0;
const BATTERY_STATUS: u16 = 1;
const SENSOR_GPS: u16 = 2;
const INPUT_RC: u16 = 3;
const ESTIMATOR_STATUS: u16 = 4;
const FAILSAFE_FLAGS: u16 = 5;
const TELEMETRY_STATUS: u16 = 6;
const VEHICLE_GLOBAL_POSITION: u16 = 7;

fn msg(out: &mut Vec<u8>, ty: u8, payload: &[u8]) {
    out.extend_from_slice(&(payload.len() as u16).to_le_bytes());
    out.push(ty);
    out.extend_from_slice(payload);
}

fn info(out: &mut Vec<u8>, key: &str, value: &[u8]) {
    let mut p = vec![key.len() as u8];
    p.extend_from_slice(key.as_bytes());
    p.extend_from_slice(value);
    msg(out, b'I', &p);
}

fn param_f32(out: &mut Vec<u8>, name: &str, v: f32) {
    let key = format!("float {name}");
    let mut p = vec![key.len() as u8];
    p.extend_from_slice(key.as_bytes());
    p.extend_from_slice(&v.to_le_bytes());
    msg(out, b'P', &p);
}

fn param_i32(out: &mut Vec<u8>, name: &str, v: i32) {
    let key = format!("int32_t {name}");
    let mut p = vec![key.len() as u8];
    p.extend_from_slice(key.as_bytes());
    p.extend_from_slice(&v.to_le_bytes());
    msg(out, b'P', &p);
}

fn data(out: &mut Vec<u8>, msg_id: u16, body: &[u8]) {
    let mut p = msg_id.to_le_bytes().to_vec();
    p.extend_from_slice(body);
    msg(out, b'D', &p);
}

fn logged(out: &mut Vec<u8>, level: u8, t_us: u64, text: &str) {
    let mut p = vec![level];
    p.extend_from_slice(&t_us.to_le_bytes());
    p.extend_from_slice(text.as_bytes());
    msg(out, b'L', &p);
}

struct Body(Vec<u8>);
impl Body {
    fn new(t_us: u64) -> Self {
        Self(t_us.to_le_bytes().to_vec())
    }
    fn u8(mut self, v: u8) -> Self {
        self.0.push(v);
        self
    }
    fn i8(mut self, v: i8) -> Self {
        self.0.push(v as u8);
        self
    }
    fn bool(self, v: bool) -> Self {
        self.u8(u8::from(v))
    }
    fn u16(mut self, v: u16) -> Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn u32(mut self, v: u32) -> Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn i32(mut self, v: i32) -> Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn u64(mut self, v: u64) -> Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn f32(mut self, v: f32) -> Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn f64(mut self, v: f64) -> Self {
        self.0.extend_from_slice(&v.to_le_bytes());
        self
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct Px4UlgWriter;

impl FamilyWriter for Px4UlgWriter {
    fn format_id(&self) -> &'static str {
        "px4_ulog"
    }
    fn extension(&self) -> &'static str {
        "ulg"
    }
    fn source_role(&self) -> SourceRole {
        SourceRole::Fc
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError> {
        let events = fc_events_for(timeline, asset_id, FcLogFormat::Px4Ulog, "px4_ulog")?;
        let t_start = events[0].t_boot_us;
        let mut out = Vec::new();
        out.extend_from_slice(&MAGIC);
        out.extend_from_slice(&t_start.to_le_bytes());
        msg(&mut out, b'B', &[0u8; 40]);
        for (name, fields) in TOPICS {
            msg(&mut out, b'F', format!("{name}:{fields}").as_bytes());
        }
        info(&mut out, "char[3] sys_name", b"PX4");
        info(&mut out, "char[16] ver_sw", b"v1.14.0-synth   ");
        info(&mut out, "char[12] ver_hw", b"MUSUBI_SYNTH");
        info(&mut out, "int32_t time_ref_utc", &0_i32.to_le_bytes());
        param_f32(&mut out, "EKF2_REQ_EPH", 3.0);
        param_f32(&mut out, "EKF2_REQ_EPV", 5.0);
        param_i32(&mut out, "EKF2_REQ_NSATS", 6);
        param_i32(&mut out, "SDLOG_MODE", 0);
        param_f32(&mut out, "COM_RC_LOSS_T", 0.5);
        param_f32(&mut out, "COM_DL_LOSS_T", 10.0);
        for (i, (name, _)) in TOPICS.iter().enumerate() {
            let mut p = vec![0u8];
            p.extend_from_slice(&(i as u16).to_le_bytes());
            p.extend_from_slice(name.as_bytes());
            msg(&mut out, b'A', &p);
        }
        let mut nav_state: u32 = 4; // AUTO_MISSION
        let mut failsafe = false;
        let mut gcs_lost = false;
        let mut rc_lost = false;
        let mut last_rc_t = t_start;
        for e in events {
            if e.boot_epoch != 0 {
                break; // 再起動後は別 log file（本 writer は初回 boot だけ）。
            }
            let t = e.t_boot_us;
            match &e.kind {
                EventKind::Heartbeat { .. } => {
                    data(
                        &mut out,
                        VEHICLE_STATUS,
                        &Body::new(t)
                            .u32(nav_state)
                            .u8(2) // ARMING_STATE_ARMED
                            .bool(failsafe)
                            .bool(gcs_lost)
                            .u8(nav_state as u8)
                            .0,
                    );
                    data(
                        &mut out,
                        FAILSAFE_FLAGS,
                        &Body::new(t)
                            .bool(rc_lost)
                            .bool(gcs_lost)
                            .bool(false)
                            .bool(false)
                            .0,
                    );
                    data(
                        &mut out,
                        TELEMETRY_STATUS,
                        &Body::new(t).f32(0.0).bool(!gcs_lost).0,
                    );
                }
                EventKind::SysStatus {
                    voltage_mv,
                    battery_remaining,
                    ..
                } => {
                    data(
                        &mut out,
                        BATTERY_STATUS,
                        &Body::new(t)
                            .f32(f32::from(*voltage_mv) / 1000.0)
                            .f32(8.5)
                            .f32(f32::from(*battery_remaining).max(0.0) / 100.0)
                            .u8(0)
                            .0,
                    );
                }
                EventKind::GpsRawInt {
                    fix_type,
                    satellites,
                    eph_cm,
                    lat_e7,
                    lon_e7,
                    alt_mm,
                    jam_ind,
                    time_valid,
                } => {
                    let wall_us = timeline.t0_unix_us + (e.t_ms as i64) * 1000;
                    let utc = if *time_valid { wall_us as u64 } else { 0 };
                    let eph_m = f32::from(*eph_cm) / 100.0;
                    data(
                        &mut out,
                        SENSOR_GPS,
                        &Body::new(t)
                            .u64(utc)
                            .i32(*lat_e7)
                            .i32(*lon_e7)
                            .i32(*alt_mm)
                            .f32(eph_m)
                            .f32(eph_m * 2.0)
                            .f32(eph_m)
                            .f32(eph_m * 2.0)
                            .u8(*fix_type)
                            .u8(*satellites)
                            .u8(if *jam_ind > 80 { 3 } else { 1 }) // DETECTED / OK
                            .u8(*jam_ind)
                            .0,
                    );
                }
                EventKind::GlobalPositionInt {
                    lat_e7,
                    lon_e7,
                    alt_mm,
                    ..
                } => {
                    data(
                        &mut out,
                        VEHICLE_GLOBAL_POSITION,
                        &Body::new(t)
                            .f64(f64::from(*lat_e7) / 1e7)
                            .f64(f64::from(*lon_e7) / 1e7)
                            .f32(*alt_mm as f32 / 1000.0)
                            .0,
                    );
                }
                EventKind::RcIn {
                    chans,
                    rssi_pct,
                    lq_pct,
                    valid,
                } => {
                    if *valid {
                        last_rc_t = t;
                    }
                    rc_lost = !*valid;
                    let mut b = Body::new(t)
                        .u64(last_rc_t)
                        .f32(-70.0 - (100.0 - f32::from(*rssi_pct)) * 0.4)
                        .i32(if *valid { i32::from(*rssi_pct) } else { 0 });
                    for c in chans {
                        b = b.u16(*c);
                    }
                    data(
                        &mut out,
                        INPUT_RC,
                        &b.i8(if *valid { *lq_pct as i8 } else { 0 })
                            .bool(!*valid)
                            .bool(!*valid)
                            .0,
                    );
                }
                EventKind::EkfStatus {
                    sqrtvar_v_x100,
                    sqrtvar_p_x100,
                    sqrtvar_h_x100,
                    sqrtvar_m_x100,
                    solution_status,
                    offset_north_m,
                    ..
                } => {
                    let glitch = solution_status & 0x4000 != 0;
                    data(
                        &mut out,
                        ESTIMATOR_STATUS,
                        &Body::new(t)
                            .f32(if glitch { 20.0 } else { 1.0 })
                            .f32(f32::from(*sqrtvar_p_x100) / 100.0)
                            .f32(f32::from(*sqrtvar_v_x100) / 100.0)
                            .f32(f32::from(*sqrtvar_h_x100) / 100.0)
                            .f32(f32::from(*sqrtvar_m_x100) / 100.0)
                            .u16(if glitch { 0x0007 } else { 0 })
                            .u16(if *sqrtvar_p_x100 > 100 { 0x0002 } else { 0 })
                            .u8(u8::from(*offset_north_m > 0.0))
                            .0,
                    );
                }
                EventKind::ModeChange { mode, reason } => {
                    nav_state = if *reason >= 3 { 5 } else { u32::from(*mode) };
                    failsafe = *reason >= 3 && *reason != 48;
                    gcs_lost = *reason == 5;
                }
                EventKind::StatusText { severity, text } => {
                    logged(&mut out, b'0' + (*severity).min(7), t, text);
                }
                EventKind::RadioStatus { .. }
                | EventKind::Err { .. }
                | EventKind::Reboot { .. }
                | EventKind::HandsetRow { .. }
                | EventKind::BlackboxRow { .. }
                | EventKind::VideoPresence { .. } => {}
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_scenario::{generate, pre_demo_default};

    #[test]
    fn px4_ulg_writer_renders_ulog_header() {
        let tl = generate(&pre_demo_default(1));
        let bytes = Px4UlgWriter.render(&tl, "plane-01").expect("P-03");
        assert_eq!(&bytes[..7], b"ULog\x01\x12\x35");
        assert_eq!(bytes[7], 1, "version");
        assert_eq!(u16::from_le_bytes([bytes[16], bytes[17]]), 40);
        assert_eq!(bytes[18], b'B');
    }

    #[test]
    fn ulg_writer_rejects_ardupilot_asset() {
        let tl = generate(&pre_demo_default(1));
        assert!(matches!(
            Px4UlgWriter.render(&tl, "ugv-01"),
            Err(WriteError::FormatMismatch { .. })
        ));
    }
}
