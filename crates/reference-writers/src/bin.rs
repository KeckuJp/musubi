use musubi_reference_scenario::{FcLogFormat, gps_week_ms};

use crate::{EventKind, FamilyWriter, SourceRole, Timeline, WriteError, fc_events_for};

pub const HEAD: [u8; 2] = [0xA3, 0x95];
pub const FMT_MSGID: u8 = 128;

#[derive(Debug, Clone, Copy)]
pub struct MsgDef {
    pub msgid: u8,
    pub name: &'static str,
    pub format: &'static str,
    pub labels: &'static str,
}

#[must_use]
pub const fn format_char_len(c: u8) -> usize {
    match c {
        b'a' => 64,
        b'b' | b'B' | b'M' => 1,
        b'h' | b'H' | b'c' | b'C' => 2,
        b'i' | b'I' | b'f' | b'e' | b'E' | b'L' => 4,
        b'd' | b'q' | b'Q' => 8,
        b'n' => 4,
        b'N' => 16,
        b'Z' => 64,
        _ => 0,
    }
}

#[must_use]
pub fn record_len(format: &str) -> usize {
    3 + format.bytes().map(format_char_len).sum::<usize>()
}

pub const DEF_FMT: MsgDef = MsgDef {
    msgid: FMT_MSGID,
    name: "FMT",
    format: "BBnNZ",
    labels: "Type,Length,Name,Format,Columns",
};
pub const DEF_PARM: MsgDef = MsgDef {
    msgid: 32,
    name: "PARM",
    format: "QNff",
    labels: "TimeUS,Name,Value,Default",
};
pub const DEF_GPS: MsgDef = MsgDef {
    msgid: 129,
    name: "GPS",
    format: "QBBIHBcLLeffffB",
    labels: "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,Yaw,U",
};
pub const DEF_GPA: MsgDef = MsgDef {
    msgid: 130,
    name: "GPA",
    format: "QBCCCCfBIHeHH",
    labels: "TimeUS,I,VDop,HAcc,VAcc,SAcc,YAcc,VV,SMS,Delta,AEl,RTCMFU,RTCMFD",
};
pub const DEF_UBX1: MsgDef = MsgDef {
    msgid: 131,
    name: "UBX1",
    format: "QBHBBHI",
    labels: "TimeUS,Instance,noisePerMS,jamInd,aPower,agcCnt,config",
};
pub const DEF_XKF4: MsgDef = MsgDef {
    msgid: 132,
    name: "XKF4",
    format: "QBcccccfffHBIHb",
    labels: "TimeUS,C,SV,SP,SH,SM,SVT,errRP,OFN,OFE,FS,TS,SS,GPS,PI",
};
pub const DEF_BAT: MsgDef = MsgDef {
    msgid: 133,
    name: "BAT",
    format: "QBfffffcfBBB",
    labels: "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH",
};
pub const DEF_RCIN: MsgDef = MsgDef {
    msgid: 134,
    name: "RCIN",
    format: "QHHHHHHHHHHHHHH",
    labels: "TimeUS,C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14",
};
pub const DEF_RSSI: MsgDef = MsgDef {
    msgid: 135,
    name: "RSSI",
    format: "Qff",
    labels: "TimeUS,RXRSSI,RXLQ",
};
pub const DEF_RAD: MsgDef = MsgDef {
    msgid: 136,
    name: "RAD",
    format: "QBBBBBHH",
    labels: "TimeUS,RSSI,RemRSSI,TxBuf,Noise,RemNoise,RxErrors,Fixed",
};
pub const DEF_MODE: MsgDef = MsgDef {
    msgid: 137,
    name: "MODE",
    format: "QMBB",
    labels: "TimeUS,Mode,ModeNum,Rsn",
};
pub const DEF_ATT: MsgDef = MsgDef {
    msgid: 138,
    name: "ATT",
    format: "QccccCCCCB",
    labels: "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,ErrRP,ErrYaw,AEKF",
};
pub const DEF_ERR: MsgDef = MsgDef {
    msgid: 139,
    name: "ERR",
    format: "QBB",
    labels: "TimeUS,Subsys,ECode",
};
pub const DEF_EV: MsgDef = MsgDef {
    msgid: 140,
    name: "EV",
    format: "QB",
    labels: "TimeUS,Id",
};
pub const DEF_MSG: MsgDef = MsgDef {
    msgid: 141,
    name: "MSG",
    format: "QBBZ",
    labels: "TimeUS,ID,Seq,Message",
};
pub const DEF_ARM: MsgDef = MsgDef {
    msgid: 142,
    name: "ARM",
    format: "QBIBB",
    labels: "TimeUS,ArmState,ArmChecks,Forced,Method",
};
pub const DEF_POWR: MsgDef = MsgDef {
    msgid: 143,
    name: "POWR",
    format: "QffHHB",
    labels: "TimeUS,Vcc,VServo,Flags,AccFlags,Safety",
};

pub const DEFS: [MsgDef; 17] = [
    DEF_FMT, DEF_PARM, DEF_MSG, DEF_MODE, DEF_ERR, DEF_EV, DEF_ARM, DEF_GPS, DEF_GPA, DEF_UBX1,
    DEF_XKF4, DEF_BAT, DEF_POWR, DEF_RCIN, DEF_RSSI, DEF_RAD, DEF_ATT,
];

struct Rec {
    buf: Vec<u8>,
    def: MsgDef,
}

impl Rec {
    fn new(def: MsgDef) -> Self {
        let mut buf = Vec::with_capacity(record_len(def.format));
        buf.extend_from_slice(&HEAD);
        buf.push(def.msgid);
        Self { buf, def }
    }
    fn u8(mut self, v: u8) -> Self {
        self.buf.push(v);
        self
    }
    fn i8(mut self, v: i8) -> Self {
        self.buf.push(v as u8);
        self
    }
    fn u16(mut self, v: u16) -> Self {
        self.buf.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn i16(mut self, v: i16) -> Self {
        self.buf.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn u32(mut self, v: u32) -> Self {
        self.buf.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn i32(mut self, v: i32) -> Self {
        self.buf.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn u64(mut self, v: u64) -> Self {
        self.buf.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn f32(mut self, v: f32) -> Self {
        self.buf.extend_from_slice(&v.to_le_bytes());
        self
    }
    fn str_n(mut self, s: &str, n: usize) -> Self {
        let mut b = vec![0u8; n];
        for (d, c) in b.iter_mut().zip(s.bytes()) {
            *d = c;
        }
        self.buf.extend_from_slice(&b);
        self
    }
    fn finish(self) -> Vec<u8> {
        assert_eq!(
            self.buf.len(),
            record_len(self.def.format),
            "{}: payload length must match format",
            self.def.name
        );
        self.buf
    }
}

#[must_use]
pub fn fmt_record(def: MsgDef) -> Vec<u8> {
    Rec::new(DEF_FMT)
        .u8(def.msgid)
        .u8(record_len(def.format) as u8)
        .str_n(def.name, 4)
        .str_n(def.format, 16)
        .str_n(def.labels, 64)
        .finish()
}

fn header_block(t_us: u64, vehicle: &str) -> Vec<u8> {
    let mut out = Vec::new();
    for d in DEFS {
        out.extend_from_slice(&fmt_record(d));
    }
    for (name, value) in [
        ("SYSID_THISMAV", 1.0f32),
        ("LOG_BITMASK", 65535.0),
        ("LOG_DISARMED", 0.0),
    ] {
        out.extend_from_slice(
            &Rec::new(DEF_PARM)
                .u64(t_us)
                .str_n(name, 16)
                .f32(value)
                .f32(value)
                .finish(),
        );
    }
    out.extend_from_slice(&msg_record(
        t_us,
        0,
        &format!("{vehicle} V4.5.0 (synthetic)"),
    ));
    out.extend_from_slice(&msg_record(
        t_us,
        1,
        "Musubi pre-demo synthetic dataflash (public format spec)",
    ));
    out.extend_from_slice(
        &Rec::new(DEF_ARM)
            .u64(t_us)
            .u8(1)
            .u32(0)
            .u8(0)
            .u8(1)
            .finish(),
    );
    out
}

fn msg_record(t_us: u64, seq: u8, text: &str) -> Vec<u8> {
    Rec::new(DEF_MSG)
        .u64(t_us)
        .u8(0)
        .u8(seq)
        .str_n(text, 64)
        .finish()
}

#[derive(Debug, Clone, Copy, Default)]
pub struct ArduPilotBinWriter;

impl FamilyWriter for ArduPilotBinWriter {
    fn format_id(&self) -> &'static str {
        "ardupilot_dataflash_bin"
    }
    fn extension(&self) -> &'static str {
        "bin"
    }
    fn source_role(&self) -> SourceRole {
        SourceRole::Fc
    }
    fn render(&self, timeline: &Timeline, asset_id: &str) -> Result<Vec<u8>, WriteError> {
        let events = fc_events_for(
            timeline,
            asset_id,
            FcLogFormat::ArduPilotBin,
            "ardupilot_dataflash_bin",
        )?;
        if events
            .iter()
            .any(|event| event.family == crate::Family::Unknown)
        {
            return Err(WriteError::FormatMismatch {
                asset_id: asset_id.to_string(),
                format: "known-family synthetic ardupilot_dataflash_bin",
            });
        }
        let vehicle = match events[0].family {
            crate::Family::Unknown => {
                return Err(WriteError::FormatMismatch {
                    asset_id: asset_id.to_string(),
                    format: "known-family synthetic ardupilot_dataflash_bin",
                });
            }
            crate::Family::Ugv => "ArduRover",
            crate::Family::FixedWing => "ArduPlane",
            crate::Family::Fpv => "ArduCopter",
        };
        let mut out = header_block(events[0].t_boot_us, vehicle);
        let mut msg_seq: u8 = 2;
        let mut last_hdg_cdeg: u16 = 0;
        for e in events {
            if e.boot_epoch != 0 {
                break;
            }
            let t = e.t_boot_us;
            match &e.kind {
                EventKind::Heartbeat { .. } => {
                    let yaw_cd = (last_hdg_cdeg / 100) as u16 * 100;
                    out.extend_from_slice(
                        &Rec::new(DEF_ATT)
                            .u64(t)
                            .i16(0)
                            .i16(12)
                            .i16(0)
                            .i16(-20)
                            .u16(yaw_cd)
                            .u16(yaw_cd)
                            .u16(5)
                            .u16(3)
                            .u8(1)
                            .finish(),
                    );
                }
                EventKind::SysStatus {
                    voltage_mv,
                    battery_remaining,
                    ..
                } => {
                    let v = f32::from(*voltage_mv) / 1000.0;
                    out.extend_from_slice(
                        &Rec::new(DEF_BAT)
                            .u64(t)
                            .u8(0)
                            .f32(v)
                            .f32(v + 0.2)
                            .f32(8.5)
                            .f32(0.0)
                            .f32(0.0)
                            .i16(2500)
                            .f32(0.012)
                            .u8((*battery_remaining).max(0) as u8)
                            .u8(1)
                            .u8(100)
                            .finish(),
                    );
                    out.extend_from_slice(
                        &Rec::new(DEF_POWR)
                            .u64(t)
                            .f32(5.02)
                            .f32(0.0)
                            .u16(0)
                            .u16(0)
                            .u8(1)
                            .finish(),
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
                    let (gwk, gms) = if *time_valid {
                        gps_week_ms(wall_us)
                    } else {
                        (0, 0)
                    };
                    out.extend_from_slice(
                        &Rec::new(DEF_GPS)
                            .u64(t)
                            .u8(0)
                            .u8(*fix_type)
                            .u32(gms)
                            .u16(gwk)
                            .u8(*satellites)
                            .i16(*eph_cm as i16) // HDop ×100
                            .i32(*lat_e7)
                            .i32(*lon_e7)
                            .i32(alt_mm / 10) // Alt 'e' = cm
                            .f32(1.0)
                            .f32(90.0)
                            .f32(0.0)
                            .f32(0.0)
                            .u8(1)
                            .finish(),
                    );
                    out.extend_from_slice(
                        &Rec::new(DEF_GPA)
                            .u64(t)
                            .u8(0)
                            .u16(eph_cm.saturating_mul(2))
                            .u16(*eph_cm) // HAcc ×100 (m)
                            .u16(eph_cm.saturating_mul(2))
                            .u16(50)
                            .f32(0.0)
                            .u8(1)
                            .u32((t / 1000) as u32)
                            .u16(200)
                            .i32(alt_mm / 10)
                            .u16(0)
                            .u16(0)
                            .finish(),
                    );
                    out.extend_from_slice(
                        &Rec::new(DEF_UBX1)
                            .u64(t)
                            .u8(0)
                            .u16(80 + u16::from(*jam_ind))
                            .u8(*jam_ind)
                            .u8(0)
                            .u16(5000)
                            .u32(0)
                            .finish(),
                    );
                }
                EventKind::GlobalPositionInt { hdg_cdeg, .. } => {
                    last_hdg_cdeg = *hdg_cdeg;
                }
                EventKind::RadioStatus {
                    rssi,
                    remrssi,
                    noise,
                    rxerrors,
                } => {
                    out.extend_from_slice(
                        &Rec::new(DEF_RAD)
                            .u64(t)
                            .u8(*rssi)
                            .u8(*remrssi)
                            .u8(100)
                            .u8(*noise)
                            .u8(noise.saturating_add(2))
                            .u16(*rxerrors)
                            .u16(0)
                            .finish(),
                    );
                }
                EventKind::StatusText { text, .. } => {
                    out.extend_from_slice(&msg_record(t, msg_seq, text));
                    msg_seq = msg_seq.wrapping_add(1);
                }
                EventKind::RcIn {
                    chans,
                    rssi_pct,
                    lq_pct,
                    ..
                } => {
                    let mut r = Rec::new(DEF_RCIN).u64(t);
                    for c in chans {
                        r = r.u16(*c);
                    }
                    for _ in 4..14 {
                        r = r.u16(0);
                    }
                    out.extend_from_slice(&r.finish());
                    out.extend_from_slice(
                        &Rec::new(DEF_RSSI)
                            .u64(t)
                            .f32(f32::from(*rssi_pct) / 100.0)
                            .f32(f32::from(*lq_pct))
                            .finish(),
                    );
                }
                EventKind::EkfStatus {
                    sqrtvar_v_x100,
                    sqrtvar_p_x100,
                    sqrtvar_h_x100,
                    sqrtvar_m_x100,
                    solution_status,
                    timeouts,
                    offset_north_m,
                } => {
                    out.extend_from_slice(
                        &Rec::new(DEF_XKF4)
                            .u64(t)
                            .u8(0)
                            .i16(*sqrtvar_v_x100)
                            .i16(*sqrtvar_p_x100)
                            .i16(*sqrtvar_h_x100)
                            .i16(*sqrtvar_m_x100)
                            .i16(0)
                            .f32(0.0)
                            .f32(*offset_north_m)
                            .f32(0.0)
                            .u16(0)
                            .u8(*timeouts)
                            .u32(*solution_status)
                            .u16(if *timeouts != 0 { 0x07 } else { 0 })
                            .i8(0)
                            .finish(),
                    );
                }
                EventKind::ModeChange { mode, reason } => {
                    out.extend_from_slice(
                        &Rec::new(DEF_MODE)
                            .u64(t)
                            .u8(*mode)
                            .u8(*mode)
                            .u8(*reason)
                            .finish(),
                    );
                }
                EventKind::Err { subsys, ecode } => {
                    out.extend_from_slice(
                        &Rec::new(DEF_ERR).u64(t).u8(*subsys).u8(*ecode).finish(),
                    );
                }
                EventKind::Reboot { .. }
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
    fn ardupilot_bin_writer_renders_fmt_and_records() {
        let tl = generate(&pre_demo_default(1));
        let bytes = ArduPilotBinWriter.render(&tl, "ugv-01").expect("P-03");
        assert_eq!(&bytes[..2], &[0xA3, 0x95], "dataflash record head marker");
        assert_eq!(bytes[2], FMT_MSGID);
        assert_eq!(bytes[3], 128);
        assert_eq!(bytes[4], 89);
        assert_eq!(&bytes[5..8], b"FMT");
    }

    #[test]
    fn every_record_length_matches_its_fmt_declaration() {
        for d in DEFS {
            let fmt = fmt_record(d);
            assert_eq!(fmt.len(), 89);
            assert_eq!(usize::from(fmt[4]), record_len(d.format), "{}", d.name);
        }
        assert_eq!(record_len("QBBIHBcLLeffffB"), 51);
        assert_eq!(record_len("QBcccccfffHBIHb"), 44);
    }

    #[test]
    fn bin_writer_rejects_px4_asset_and_writes_plane_variant() {
        let mut s = pre_demo_default(1);
        let tl = generate(&s);
        assert!(matches!(
            ArduPilotBinWriter.render(&tl, "plane-01"),
            Err(WriteError::FormatMismatch { .. })
        ));
        s.assets[1].fc_log = FcLogFormat::ArduPilotBin;
        let tl = generate(&s);
        let bytes = ArduPilotBinWriter
            .render(&tl, "plane-01")
            .expect("plane bin");
        let text = String::from_utf8_lossy(&bytes);
        assert!(text.contains("ArduPlane V4.5.0 (synthetic)"));
    }
}
