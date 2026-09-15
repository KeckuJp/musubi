use crate::frame::{GpiFields, build_global_position_int_frame};

pub const MAVLINK2_MAGIC: u8 = 0xFD;
const SYSID: u8 = 1;
const COMPID: u8 = 1;

pub const HEARTBEAT: (u32, u8) = (0, 50);
pub const SYS_STATUS: (u32, u8) = (1, 124);
pub const GPS_RAW_INT: (u32, u8) = (24, 24);
pub const GLOBAL_POSITION_INT: (u32, u8) = (33, 104);
pub const RADIO_STATUS: (u32, u8) = (109, 185);
pub const STATUSTEXT: (u32, u8) = (253, 83);
pub const SYSTEM_TIME: (u32, u8) = (2, 137);
pub const RC_CHANNELS: (u32, u8) = (65, 118);
pub const EKF_STATUS_REPORT: (u32, u8) = (193, 71);

#[must_use]
pub fn crc_x25(bytes: &[u8], init: u16) -> u16 {
    let mut crc = init;
    for &b in bytes {
        let mut tmp = b ^ (crc & 0xFF) as u8;
        tmp ^= tmp << 4;
        crc = (crc >> 8) ^ (u16::from(tmp) << 8) ^ (u16::from(tmp) << 3) ^ (u16::from(tmp) >> 4);
    }
    crc
}

pub fn seal_crc(frame_without_crc: &mut Vec<u8>, crc_extra: u8) {
    let crc = crc_x25(&frame_without_crc[1..], 0xFFFF);
    let crc = crc_x25(&[crc_extra], crc);
    frame_without_crc.extend_from_slice(&crc.to_le_bytes());
}

#[must_use]
pub fn frame(seq: u8, (msgid, crc_extra): (u32, u8), payload: &[u8]) -> Vec<u8> {
    let mut f = Vec::with_capacity(12 + payload.len());
    f.push(MAVLINK2_MAGIC);
    f.push(payload.len() as u8);
    f.push(0x00); // incompat_flags（署名なし）
    f.push(0x00); // compat_flags
    f.push(seq);
    f.push(SYSID);
    f.push(COMPID);
    f.push((msgid & 0xFF) as u8);
    f.push(((msgid >> 8) & 0xFF) as u8);
    f.push(((msgid >> 16) & 0xFF) as u8);
    f.extend_from_slice(payload);
    seal_crc(&mut f, crc_extra);
    f
}

#[must_use]
pub fn heartbeat(seq: u8, custom_mode: u32, mav_type: u8, system_status: u8) -> Vec<u8> {
    let mut p = Vec::with_capacity(9);
    p.extend_from_slice(&custom_mode.to_le_bytes());
    p.push(mav_type);
    p.push(3); // MAV_AUTOPILOT_ARDUPILOTMEGA
    p.push(0x81); // base_mode: CUSTOM_MODE_ENABLED|SAFETY_ARMED（合成値）
    p.push(system_status);
    p.push(3); // mavlink_version
    frame(seq, HEARTBEAT, &p)
}

#[must_use]
pub fn sys_status(seq: u8, voltage_mv: u16, battery_remaining: i8, drop_rate_comm: u16) -> Vec<u8> {
    let mut p = Vec::with_capacity(31);
    for _ in 0..3 {
        p.extend_from_slice(&0x0000_0000_u32.to_le_bytes()); // sensors present/enabled/health（合成: 未宣言）
    }
    p.extend_from_slice(&500_u16.to_le_bytes()); // load 50%
    p.extend_from_slice(&voltage_mv.to_le_bytes());
    p.extend_from_slice(&(-1_i16).to_le_bytes()); // current_battery: unknown
    p.extend_from_slice(&drop_rate_comm.to_le_bytes());
    for _ in 0..5 {
        p.extend_from_slice(&0_u16.to_le_bytes()); // errors_comm, errors_count1..4
    }
    p.push(battery_remaining as u8);
    frame(seq, SYS_STATUS, &p)
}

#[must_use]
pub fn gps_raw_int(
    seq: u8,
    time_usec: u64,
    lat_e7: i32,
    lon_e7: i32,
    alt_mm: i32,
    eph_cm: u16,
    fix_type: u8,
    satellites: u8,
) -> Vec<u8> {
    let mut p = Vec::with_capacity(30);
    p.extend_from_slice(&time_usec.to_le_bytes());
    p.extend_from_slice(&lat_e7.to_le_bytes());
    p.extend_from_slice(&lon_e7.to_le_bytes());
    p.extend_from_slice(&alt_mm.to_le_bytes());
    p.extend_from_slice(&eph_cm.to_le_bytes());
    p.extend_from_slice(&(eph_cm.saturating_mul(2)).to_le_bytes()); // epv
    p.extend_from_slice(&100_u16.to_le_bytes()); // vel 1 m/s
    p.extend_from_slice(&9_000_u16.to_le_bytes()); // cog
    p.push(fix_type);
    p.push(satellites);
    frame(seq, GPS_RAW_INT, &p)
}

#[must_use]
pub fn global_position_int(
    seq: u8,
    time_boot_ms: u32,
    lat_e7: i32,
    lon_e7: i32,
    alt_mm: i32,
    hdg_cdeg: u16,
) -> Vec<u8> {
    let mut f = build_global_position_int_frame(
        seq,
        GpiFields {
            time_boot_ms,
            lat: lat_e7,
            lon: lon_e7,
            alt: alt_mm,
            relative_alt: alt_mm,
            hdg: hdg_cdeg,
            ..GpiFields::default()
        },
    );
    f.truncate(f.len() - 2); // placeholder CRC を落とす
    seal_crc(&mut f, GLOBAL_POSITION_INT.1);
    f
}

#[must_use]
pub fn radio_status(seq: u8, rssi: u8, remrssi: u8, noise: u8, rxerrors: u16) -> Vec<u8> {
    let mut p = Vec::with_capacity(9);
    p.extend_from_slice(&rxerrors.to_le_bytes());
    p.extend_from_slice(&0_u16.to_le_bytes()); // fixed
    p.push(rssi);
    p.push(remrssi);
    p.push(100); // txbuf
    p.push(noise);
    p.push(noise.saturating_add(2)); // remnoise
    frame(seq, RADIO_STATUS, &p)
}

#[must_use]
pub fn statustext(seq: u8, severity: u8, text: &str) -> Vec<u8> {
    let mut p = vec![severity];
    let mut t = [0u8; 50];
    for (d, s) in t.iter_mut().zip(text.bytes()) {
        *d = s;
    }
    p.extend_from_slice(&t);
    frame(seq, STATUSTEXT, &p)
}

#[must_use]
pub fn system_time(seq: u8, time_unix_usec: u64, time_boot_ms: u32) -> Vec<u8> {
    let mut p = Vec::with_capacity(12);
    p.extend_from_slice(&time_unix_usec.to_le_bytes());
    p.extend_from_slice(&time_boot_ms.to_le_bytes());
    frame(seq, SYSTEM_TIME, &p)
}

#[must_use]
pub fn rc_channels(
    seq: u8,
    time_boot_ms: u32,
    chans: [u16; 4],
    chancount: u8,
    rssi: u8,
) -> Vec<u8> {
    let mut p = Vec::with_capacity(42);
    p.extend_from_slice(&time_boot_ms.to_le_bytes());
    for c in chans {
        p.extend_from_slice(&c.to_le_bytes());
    }
    for _ in 4..18 {
        p.extend_from_slice(&0_u16.to_le_bytes());
    }
    p.push(chancount);
    p.push(rssi);
    frame(seq, RC_CHANNELS, &p)
}

#[must_use]
pub fn ekf_status_report(
    seq: u8,
    velocity_variance: f32,
    pos_horiz_variance: f32,
    compass_variance: f32,
    flags: u16,
) -> Vec<u8> {
    let mut p = Vec::with_capacity(26);
    p.extend_from_slice(&velocity_variance.to_le_bytes());
    p.extend_from_slice(&pos_horiz_variance.to_le_bytes());
    p.extend_from_slice(&0.1_f32.to_le_bytes()); // pos_vert_variance
    p.extend_from_slice(&compass_variance.to_le_bytes());
    p.extend_from_slice(&0.0_f32.to_le_bytes()); // terrain_alt_variance
    p.extend_from_slice(&flags.to_le_bytes());
    p.extend_from_slice(&0.0_f32.to_le_bytes()); // airspeed_variance
    frame(seq, EKF_STATUS_REPORT, &p)
}

#[cfg(test)]
mod tests {
    use super::*;

    const GOLDEN_FRAME: [u8; 40] = [
        0xFD, 0x1C, 0x00, 0x00, 0x00, 0x01, 0x01, 0x21, 0x00, 0x00, 0x39, 0x30, 0x00, 0x00, 0x4C,
        0x52, 0x40, 0x1C, 0x44, 0xF4, 0x17, 0x05, 0x40, 0x72, 0x07, 0x00, 0x50, 0xC3, 0x00, 0x00,
        0x64, 0x00, 0x00, 0x00, 0xF6, 0xFF, 0x78, 0x69, 0x21, 0xA3,
    ];

    #[test]
    fn crc_reproduces_golden_global_position_int_trailer() {
        let mut f = GOLDEN_FRAME[..38].to_vec();
        seal_crc(&mut f, GLOBAL_POSITION_INT.1);
        assert_eq!(f, GOLDEN_FRAME.to_vec());
    }

    #[test]
    fn global_position_int_reuses_scenario_runner_builder_with_real_crc() {
        let f = global_position_int(7, 12_345, 473_977_420, 85_455_940, 488_000, 27_000);
        assert_eq!(f.len(), 40);
        assert_eq!(f[4], 7, "seq");
        assert_eq!(u32::from_le_bytes([f[10], f[11], f[12], f[13]]), 12_345);
        assert_eq!(
            i32::from_le_bytes([f[14], f[15], f[16], f[17]]),
            473_977_420
        );
        assert_ne!(&f[38..], &[0, 0]);
    }

    #[test]
    fn payload_lengths_match_dialect_definitions() {
        assert_eq!(heartbeat(0, 0, 10, 4).len(), 10 + 9 + 2);
        assert_eq!(sys_status(0, 12_000, 90, 0).len(), 10 + 31 + 2);
        assert_eq!(gps_raw_int(0, 0, 0, 0, 0, 0, 3, 12).len(), 10 + 30 + 2);
        assert_eq!(radio_status(0, 180, 175, 40, 0).len(), 10 + 9 + 2);
        assert_eq!(
            statustext(0, 2, "GPS Glitch or Compass error").len(),
            10 + 51 + 2
        );
        assert_eq!(system_time(0, 0, 0).len(), 10 + 12 + 2);
        assert_eq!(rc_channels(0, 0, [1500; 4], 4, 200).len(), 10 + 42 + 2);
        assert_eq!(ekf_status_report(0, 0.2, 0.2, 0.1, 16).len(), 10 + 26 + 2);
    }
}
