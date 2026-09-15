use crate::mavlink;
use crate::{EventKind, FamilyWriter, SourceRole, Timeline, WriteError, events_for};

#[derive(Debug, Clone, Copy, Default)]
pub struct TlogWriter;

#[must_use]
pub const fn mav_type_for(family: crate::Family) -> u8 {
    match family {
        crate::Family::Unknown => 0, // MAV_TYPE_GENERIC; render rejects unknown family.
        crate::Family::Ugv => 10,
        crate::Family::FixedWing => 1,
        crate::Family::Fpv => 2,
    }
}

impl FamilyWriter for TlogWriter {
    fn format_id(&self) -> &'static str {
        "mavlink_tlog"
    }
    fn extension(&self) -> &'static str {
        "tlog"
    }
    fn source_role(&self) -> SourceRole {
        SourceRole::Gcs
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError> {
        let events = events_for(timeline, asset_id, SourceRole::Gcs);
        if events.is_empty() {
            return Err(WriteError::NoEvents {
                asset_id: asset_id.to_string(),
                source: SourceRole::Gcs,
            });
        }
        if events
            .iter()
            .any(|event| event.family == crate::Family::Unknown)
        {
            return Err(WriteError::FormatMismatch {
                asset_id: asset_id.to_string(),
                format: "known-family synthetic mavlink_tlog",
            });
        }
        let mav_type = mav_type_for(events[0].family);
        let mut out = Vec::new();
        let mut seq: u8 = 0;
        fn emit(out: &mut Vec<u8>, seq: &mut u8, host_us: i64, frame: &[u8]) {
            out.extend_from_slice(&(host_us as u64).to_be_bytes());
            out.extend_from_slice(frame);
            *seq = seq.wrapping_add(1);
        }
        for e in events {
            let host_us = timeline.t0_unix_us + (e.t_ms as i64) * 1000;
            let boot_ms = (e.t_boot_us / 1000) as u32;
            let frame = match &e.kind {
                EventKind::Heartbeat {
                    custom_mode,
                    system_status,
                } => {
                    let f = mavlink::heartbeat(seq, *custom_mode, mav_type, *system_status);
                    emit(&mut out, &mut seq, host_us, &f);
                    mavlink::system_time(seq, host_us as u64, boot_ms)
                }
                EventKind::SysStatus {
                    voltage_mv,
                    battery_remaining,
                    drop_rate_comm,
                } => mavlink::sys_status(seq, *voltage_mv, *battery_remaining, *drop_rate_comm),
                EventKind::GpsRawInt {
                    fix_type,
                    satellites,
                    eph_cm,
                    lat_e7,
                    lon_e7,
                    alt_mm,
                    ..
                } => mavlink::gps_raw_int(
                    seq,
                    e.t_boot_us, // ArduPilot は last_fix_time_ms×1000＝boot 時刻を入れる（R-03 出力 1）。
                    *lat_e7,
                    *lon_e7,
                    *alt_mm,
                    *eph_cm,
                    *fix_type,
                    *satellites,
                ),
                EventKind::GlobalPositionInt {
                    lat_e7,
                    lon_e7,
                    alt_mm,
                    hdg_cdeg,
                } => {
                    mavlink::global_position_int(seq, boot_ms, *lat_e7, *lon_e7, *alt_mm, *hdg_cdeg)
                }
                EventKind::RadioStatus {
                    rssi,
                    remrssi,
                    noise,
                    rxerrors,
                } => mavlink::radio_status(seq, *rssi, *remrssi, *noise, *rxerrors),
                EventKind::StatusText { severity, text } => {
                    mavlink::statustext(seq, *severity, text)
                }
                EventKind::RcIn {
                    chans,
                    rssi_pct,
                    valid,
                    ..
                } => mavlink::rc_channels(
                    seq,
                    boot_ms,
                    *chans,
                    if *valid { 4 } else { 0 },
                    if *valid {
                        (u16::from(*rssi_pct) * 254 / 100) as u8
                    } else {
                        255 // UINT8_MAX = invalid/unknown
                    },
                ),
                EventKind::EkfStatus {
                    sqrtvar_v_x100,
                    sqrtvar_p_x100,
                    sqrtvar_m_x100,
                    solution_status,
                    ..
                } => {
                    let mut flags: u16 = 0;
                    if solution_status & 0x2000 != 0 {
                        flags |= 16; // EKF_POS_HORIZ_ABS
                    }
                    if solution_status & 0x4000 != 0 {
                        flags |= 32768; // EKF_GPS_GLITCHING
                    }
                    mavlink::ekf_status_report(
                        seq,
                        f32::from(*sqrtvar_v_x100) / 100.0,
                        f32::from(*sqrtvar_p_x100) / 100.0,
                        f32::from(*sqrtvar_m_x100) / 100.0,
                        flags,
                    )
                }
                EventKind::ModeChange { .. } | EventKind::Err { .. } | EventKind::Reboot { .. } => {
                    continue;
                }
                EventKind::HandsetRow { .. }
                | EventKind::BlackboxRow { .. }
                | EventKind::VideoPresence { .. } => continue,
            };
            emit(&mut out, &mut seq, host_us, &frame);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_scenario::{generate, pre_demo_default};

    #[test]
    fn tlog_records_are_8_byte_be_timestamp_then_mavlink2_frame() {
        let tl = generate(&pre_demo_default(7));
        let bytes = TlogWriter
            .render(&tl, "ugv-01")
            .expect("ugv has gcs events");
        let ts = u64::from_be_bytes(bytes[..8].try_into().expect("8 bytes"));
        assert_eq!(ts, tl.t0_unix_us as u64);
        assert_eq!(bytes[8], mavlink::MAVLINK2_MAGIC);
        let len = usize::from(bytes[9]);
        let next = 8 + 10 + len + 2;
        assert_eq!(bytes[next + 8], mavlink::MAVLINK2_MAGIC);
    }

    #[test]
    fn tlog_skips_assets_without_gcs_source() {
        let tl = generate(&pre_demo_default(7));
        let r = TlogWriter.render(&tl, "fpv-01");
        assert!(matches!(r, Err(WriteError::NoEvents { .. })));
    }
}
