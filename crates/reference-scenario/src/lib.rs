#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::doc_markdown,
    clippy::too_many_lines,
    clippy::missing_const_for_fn,
    clippy::module_name_repetitions,
    clippy::struct_excessive_bools,
    clippy::cognitive_complexity
)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

pub use musubi_reference_types::{ChannelId, FailureKind, Family, SourceRole};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FcLogFormat {
    ArduPilotBin,
    Px4Ulog,
    BlackboxCsv,
}

impl FcLogFormat {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ArduPilotBin => "ardupilot_bin",
            Self::Px4Ulog => "px4_ulog",
            Self::BlackboxCsv => "blackbox_csv",
        }
    }
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "ardupilot_bin" | "ardupilot" | "bin" => Some(Self::ArduPilotBin),
            "px4_ulog" | "px4" | "ulg" => Some(Self::Px4Ulog),
            "blackbox_csv" | "blackbox" => Some(Self::BlackboxCsv),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LinkProfile {
    AnalogRf,
    Fiber,
    FiberPlusRadio,
}

impl LinkProfile {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::AnalogRf => "analog_rf",
            Self::Fiber => "fiber",
            Self::FiberPlusRadio => "fiber_plus_radio",
        }
    }
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "analog_rf" | "rf" => Some(Self::AnalogRf),
            "fiber" => Some(Self::Fiber),
            "fiber_plus_radio" => Some(Self::FiberPlusRadio),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Asset {
    pub asset_id: String,
    pub family: Family,
    pub sources: Vec<SourceRole>,
    pub origin_lat_deg: f64,
    pub origin_lon_deg: f64,
    pub fc_log: FcLogFormat,
    pub link_profile: LinkProfile,
    pub boot_lead_us: i64,
    pub drift_ppm: i32,
    pub suppressed_channels: Vec<ChannelId>,
    pub handset_tmr10ms: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PreludeOrder {
    NoiseLimited,
    InterferenceLimited,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LogEndMode {
    Truncated,
    Reboot { after_ms: u64 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum HandsetLossMode {
    ZeroWithGpsBlank,
    Frozen,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RadioStatusVariant {
    Stops,
    RemoteAlive,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InjectionParams {
    pub prelude_ms: u64,
    pub prelude_order: PreludeOrder,
    pub log_end_mode: LogEndMode,
    pub handset_loss_mode: HandsetLossMode,
    pub radio_status: RadioStatusVariant,
}

impl InjectionParams {
    #[must_use]
    pub const fn defaults_for(kind: FailureKind) -> Self {
        let base = Self {
            prelude_ms: PRELUDE_MS,
            prelude_order: PreludeOrder::NoiseLimited,
            log_end_mode: LogEndMode::Truncated,
            handset_loss_mode: HandsetLossMode::ZeroWithGpsBlank,
            radio_status: RadioStatusVariant::Stops,
        };
        match kind {
            FailureKind::RcLinkLoss
            | FailureKind::TelemetryLinkLoss
            | FailureKind::GnssDegradation => base,
            FailureKind::FcFailure => Self {
                prelude_ms: 0,
                radio_status: RadioStatusVariant::RemoteAlive,
                ..base
            },
            FailureKind::FiberBreak | FailureKind::CameraStop | FailureKind::GnssInconsistency => {
                Self {
                    prelude_ms: 0,
                    ..base
                }
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Injection {
    pub kind: FailureKind,
    pub t_start_ms: u64,
    pub duration_ms: u64,
    pub family: Family,
    pub params: InjectionParams,
}

impl Injection {
    #[must_use]
    pub const fn new(kind: FailureKind, t_start_ms: u64, duration_ms: u64, family: Family) -> Self {
        Self {
            kind,
            t_start_ms,
            duration_ms,
            family,
            params: InjectionParams::defaults_for(kind),
        }
    }
    const fn t_end_ms(self) -> u64 {
        self.t_start_ms.saturating_add(self.duration_ms)
    }
    const fn active_at(self, t_ms: u64) -> bool {
        t_ms >= self.t_start_ms && t_ms < self.t_end_ms()
    }
    const fn in_prelude(self, t_ms: u64) -> bool {
        self.params.prelude_ms > 0
            && t_ms + self.params.prelude_ms >= self.t_start_ms
            && t_ms < self.t_start_ms
    }
    fn prelude_progress(self, t_ms: u64) -> f64 {
        if self.in_prelude(t_ms) {
            1.0 - ((self.t_start_ms - t_ms) as f64 / self.params.prelude_ms as f64)
        } else {
            0.0
        }
    }
}

pub const PRELUDE_MS: u64 = 10_000;

#[derive(Debug, Clone, PartialEq)]
pub struct Scenario {
    pub seed: u64,
    pub t0_unix_us: i64,
    pub duration_ms: u64,
    pub assets: Vec<Asset>,
    pub injections: Vec<Injection>,
}

impl Scenario {
    pub fn inject(&mut self, kind: FailureKind, t_start_ms: u64, duration_ms: u64, family: Family) {
        self.injections
            .push(Injection::new(kind, t_start_ms, duration_ms, family));
    }
    pub fn inject_with(&mut self, injection: Injection) {
        self.injections.push(injection);
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum EventKind {
    Heartbeat {
        custom_mode: u32,
        system_status: u8,
    },
    SysStatus {
        voltage_mv: u16,
        battery_remaining: i8,
        drop_rate_comm: u16,
    },
    GpsRawInt {
        fix_type: u8,
        satellites: u8,
        eph_cm: u16,
        lat_e7: i32,
        lon_e7: i32,
        alt_mm: i32,
        jam_ind: u8,
        time_valid: bool,
    },
    GlobalPositionInt {
        lat_e7: i32,
        lon_e7: i32,
        alt_mm: i32,
        hdg_cdeg: u16,
    },
    RadioStatus {
        rssi: u8,
        remrssi: u8,
        noise: u8,
        rxerrors: u16,
    },
    StatusText {
        severity: u8,
        text: String,
    },
    RcIn {
        chans: [u16; 4],
        rssi_pct: u8,
        lq_pct: u8,
        valid: bool,
    },
    EkfStatus {
        sqrtvar_v_x100: i16,
        sqrtvar_p_x100: i16,
        sqrtvar_h_x100: i16,
        sqrtvar_m_x100: i16,
        solution_status: u32,
        timeouts: u8,
        offset_north_m: f32,
    },
    ModeChange {
        mode: u8,
        reason: u8,
    },
    Err {
        subsys: u8,
        ecode: u8,
    },
    Reboot {
        epoch: u8,
    },
    HandsetRow {
        rss_dbm: i16,
        rqly_pct: u8,
        rsnr_db: i8,
        tpwr_mw: u16,
        rxbt_v: f32,
        sats: u8,
        gps_deg: Option<(f64, f64)>,
        link_alive: bool,
    },
    BlackboxRow {
        loop_iteration: u32,
        rc_command: [i16; 4],
        vbat_v: f32,
        amperage_a: f32,
        rssi: Option<u16>,
        gps_num_sat: u8,
        gps_deg: Option<(f64, f64)>,
        failsafe_phase: u8,
        rx_signal_received: bool,
        rx_flight_channels_valid: bool,
    },
    VideoPresence {
        present: bool,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub t_ms: u64,
    pub t_boot_us: u64,
    pub boot_epoch: u8,
    pub asset_id: String,
    pub family: Family,
    pub source: SourceRole,
    pub channel: ChannelId,
    pub kind: EventKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroundTruth {
    pub kind: FailureKind,
    pub family: Family,
    pub asset_id: String,
    pub t_start_ms: u64,
    pub t_end_ms: u64,
    pub affected_channels: Vec<ChannelId>,
    pub params: InjectionParams,
    pub missing_channels: Vec<ChannelId>,
    pub link_profile: LinkProfile,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Timeline {
    pub t0_unix_us: i64,
    pub duration_ms: u64,
    pub assets: Vec<Asset>,
    pub events: Vec<Event>,
    pub ground_truth: Vec<GroundTruth>,
}

impl Timeline {
    #[must_use]
    pub fn asset(&self, asset_id: &str) -> Option<&Asset> {
        self.assets.iter().find(|a| a.asset_id == asset_id)
    }
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n.max(1)
    }
}

const HEARTBEAT_MS: u64 = 1_000;
const SYS_STATUS_MS: u64 = 1_000;
const GPS_MS: u64 = 500;
const RADIO_MS: u64 = 1_000;
const RCIN_MS: u64 = 500;
const EKF_MS: u64 = 1_000;
const HANDSET_MS: u64 = 1_000;
const BLACKBOX_MS: u64 = 100;
const VIDEO_MS: u64 = 1_000;
const SPEED_DEG_PER_S: f64 = 0.000_009;
const ELRS_SENSITIVITY_DBM: i16 = -108;
const REBOOT_LOG_START_US: u64 = 2_000_000;
const FIBER_BLACKBOX_TRUNCATE_MS: u64 = 3_000;

pub mod mode_reason {
    pub const RC_COMMAND: u8 = 1;
    pub const RADIO_FAILSAFE: u8 = 3;
    pub const GCS_FAILSAFE: u8 = 5;
    pub const EKF_FAILSAFE: u8 = 6;
    pub const GPS_GLITCH: u8 = 7;
    pub const FAILSAFE: u8 = 25;
    pub const RADIO_FAILSAFE_RECOVERY: u8 = 48;
}

pub mod err_subsys {
    pub const GPS: u8 = 11;
    pub const EKFCHECK: u8 = 16;
    pub const FAILSAFE_EKFINAV: u8 = 17;
}

pub mod modes {
    pub const AUTO: u8 = 10;
    pub const RTL: u8 = 11;
    pub const ROVER_HOLD: u8 = 4;
    pub const PLANE_CIRCLE: u8 = 1;
}

fn affected_channels(kind: FailureKind, asset: &Asset) -> Vec<ChannelId> {
    use ChannelId as C;
    let v: Vec<ChannelId> = match (kind, asset.family) {
        (FailureKind::TelemetryLinkLoss, _) => {
            vec![
                C::Tlog,
                C::Heartbeat,
                C::LinkStats,
                C::GpsEkf,
                C::Event,
                C::Rc,
            ]
        }
        (FailureKind::FcFailure, Family::Fpv) => vec![C::Onboard, C::Rc, C::LinkStats, C::GpsEkf],
        (FailureKind::FcFailure, _) => vec![
            C::Onboard,
            C::Tlog,
            C::Heartbeat,
            C::GpsEkf,
            C::Event,
            C::Rc,
            C::LinkStats,
        ],
        (FailureKind::RcLinkLoss, Family::Fpv) => vec![C::LinkStats, C::Rc, C::Event],
        (FailureKind::RcLinkLoss, _) => vec![C::Rc, C::Event, C::LinkStats],
        (FailureKind::FiberBreak, _) => match asset.link_profile {
            LinkProfile::FiberPlusRadio => vec![C::Rc, C::Event, C::Video, C::Onboard],
            _ => vec![C::LinkStats, C::Rc, C::Event, C::Video, C::Onboard],
        },
        (FailureKind::CameraStop, _) => vec![C::Video],
        (FailureKind::GnssDegradation | FailureKind::GnssInconsistency, _) => {
            vec![C::GpsEkf, C::Event]
        }
    };
    v.into_iter()
        .filter(|c| !asset.suppressed_channels.contains(c))
        .collect()
}

#[must_use]
pub fn gps_week_ms(unix_us: i64) -> (u16, u32) {
    const GPS_EPOCH_UNIX_S: i64 = 315_964_800;
    const LEAP_S: i64 = 18;
    let gps_ms = (unix_us / 1000) - GPS_EPOCH_UNIX_S * 1000 + LEAP_S * 1000;
    let week = gps_ms.div_euclid(7 * 86_400 * 1000);
    let ms = gps_ms.rem_euclid(7 * 86_400 * 1000);
    (week as u16, ms as u32)
}

#[must_use]
pub fn generate(s: &Scenario) -> Timeline {
    let mut rng = Rng((s.seed ^ 0x9E37_79B9_7F4A_7C15).max(1));
    let mut events = Vec::new();
    let ticks = s.duration_ms / 100; // 100 ms 解像度で走査
    for asset in &s.assets {
        let inj: Vec<Injection> = s
            .injections
            .iter()
            .copied()
            .filter(|i| i.family == asset.family)
            .collect();
        let find =
            |kind: FailureKind, t: u64| inj.iter().find(|i| i.kind == kind && i.active_at(t));
        let active = |kind: FailureKind, t: u64| find(kind, t).is_some();
        let prelude = |kind: FailureKind, t: u64| {
            inj.iter()
                .find(|i| i.kind == kind && i.in_prelude(t))
                .map_or(0.0, |i| i.prelude_progress(t))
        };
        let onset_plus = |kind: FailureKind, t: u64, d: u64| {
            inj.iter().any(|i| i.kind == kind && i.t_start_ms + d == t)
        };
        let end_at =
            |kind: FailureKind, t: u64| inj.iter().any(|i| i.kind == kind && i.t_end_ms() == t);
        let onset_age = |kind: FailureKind, t: u64| find(kind, t).map(|i| t - i.t_start_ms);
        let suppressed = |c: ChannelId| asset.suppressed_channels.contains(&c);
        let is_rover = asset.family == Family::Ugv;
        let has_gcs = asset.sources.contains(&SourceRole::Gcs);
        let has_fc = asset.sources.contains(&SourceRole::Fc);
        let is_mavlink = asset.family != Family::Fpv;
        let fc_inj = inj
            .iter()
            .find(|i| i.kind == FailureKind::FcFailure)
            .copied();
        let reboot_at_ms: Option<u64> = fc_inj.and_then(|i| match i.params.log_end_mode {
            LogEndMode::Reboot { after_ms } => Some(i.t_start_ms + after_ms),
            LogEndMode::Truncated => None,
        });
        let mut cur_mode: u8 = modes::AUTO;
        let mut last_rc: [u16; 4] = [1500, 1500, 1000, 1500];
        let mut last_handset: Option<EventKind> = None;
        let mut loop_iter: u32 = 0;
        for k in 0..=ticks {
            let t = k * 100;
            let secs = t as f64 / 1000.0;
            let lat = asset.origin_lat_deg + SPEED_DEG_PER_S * secs;
            let lon = asset.origin_lon_deg;
            let (t_boot_us, boot_epoch): (u64, u8) = match reboot_at_ms {
                Some(rb) if t >= rb => ((t - rb) * 1000 + REBOOT_LOG_START_US, 1),
                _ => {
                    let raw = (t as i64) * 1000 + asset.boot_lead_us;
                    let drifted = (raw as f64 * (1.0 + f64::from(asset.drift_ppm) * 1e-6)).round();
                    (drifted.max(0.0) as u64, 0)
                }
            };
            let push = |events: &mut Vec<Event>, source, channel, kind| {
                if suppressed(channel) {
                    return;
                }
                events.push(Event {
                    t_ms: t,
                    t_boot_us,
                    boot_epoch,
                    asset_id: asset.asset_id.clone(),
                    family: asset.family,
                    source,
                    channel,
                    kind,
                });
            };
            let fc_dead =
                fc_inj.is_some_and(|i| t >= i.t_start_ms && reboot_at_ms.is_none_or(|rb| t < rb));
            if is_mavlink && (has_gcs || has_fc) {
                let tel_loss = find(FailureKind::TelemetryLinkLoss, t);
                let gcs_silent = fc_dead || tel_loss.is_some();
                let radio_alive = match (tel_loss, fc_dead) {
                    (Some(i), _) => i.params.radio_status == RadioStatusVariant::RemoteAlive,
                    (None, true) => fc_inj
                        .is_some_and(|i| i.params.radio_status == RadioStatusVariant::RemoteAlive),
                    (None, false) => true,
                };
                let (fix_type, sats, eph, jam) = onset_age(FailureKind::GnssDegradation, t)
                    .map_or_else(
                        || {
                            (
                                3u8,
                                14u8 + (rng.below(3) as u8),
                                90u16 + rng.below(20) as u16,
                                20u8 + rng.below(5) as u8,
                            )
                        },
                        |age| {
                            let f = (age.min(10_000) as f64) / 10_000.0;
                            let sats = (14.0 - 11.0 * f).round() as u8;
                            let eph = (90.0 + 1410.0 * f) as u16;
                            let jam = (20.0 + 100.0 * f) as u8;
                            (if f >= 1.0 { 1 } else { 3 }, sats, eph, jam)
                        },
                    );
                let time_valid = fix_type >= 3;
                let inconsistent = active(FailureKind::GnssInconsistency, t);
                let jump = if inconsistent { 0.000_45 } else { 0.0 };
                let lat_e7 = ((lat + jump) * 1e7) as i32;
                let lon_e7 = (lon * 1e7) as i32;
                let alt_mm = 100_000 + (rng.below(200) as i32) - 100;
                let rc_prelude = prelude(FailureKind::RcLinkLoss, t);
                let rc_lost = active(FailureKind::RcLinkLoss, t);
                let sources: [(SourceRole, bool); 2] = [
                    (SourceRole::Fc, has_fc && !fc_dead),
                    (SourceRole::Gcs, has_gcs && !gcs_silent),
                ];
                let mut mode_change: Option<(u8, u8, Vec<String>)> = None;
                if !fc_dead {
                    let rc_fs_delay = if is_rover { 1_000 } else { 500 };
                    if onset_plus(FailureKind::RcLinkLoss, t, rc_fs_delay) {
                        mode_change = Some(if is_rover {
                            (
                                modes::ROVER_HOLD,
                                mode_reason::FAILSAFE,
                                vec!["Radio Failsafe".to_string()],
                            )
                        } else {
                            (
                                modes::PLANE_CIRCLE,
                                mode_reason::RADIO_FAILSAFE,
                                vec![
                                    "RC Short Failsafe On".to_string(),
                                    "RC Short Failsafe: switched to CIRCLE".to_string(),
                                ],
                            )
                        });
                    } else if !is_rover && onset_plus(FailureKind::RcLinkLoss, t, 5_000) {
                        mode_change = Some((
                            modes::RTL,
                            mode_reason::RADIO_FAILSAFE,
                            vec!["RC Long Failsafe On: switched to RTL".to_string()],
                        ));
                    } else if end_at(FailureKind::RcLinkLoss, t) {
                        mode_change = Some((
                            cur_mode,
                            mode_reason::RADIO_FAILSAFE_RECOVERY,
                            vec![if is_rover {
                                "Radio Failsafe Cleared".to_string()
                            } else {
                                "RC Long Failsafe Cleared".to_string()
                            }],
                        ));
                    } else if onset_plus(FailureKind::TelemetryLinkLoss, t, 5_000) {
                        mode_change = Some(if is_rover {
                            (
                                modes::ROVER_HOLD,
                                mode_reason::FAILSAFE,
                                vec!["GCS Failsafe".to_string()],
                            )
                        } else {
                            (
                                modes::RTL,
                                mode_reason::GCS_FAILSAFE,
                                vec!["GCS Failsafe On: switched to RTL".to_string()],
                            )
                        });
                    } else if onset_plus(FailureKind::GnssDegradation, t, 6_000) {
                        mode_change = Some(if is_rover {
                            (
                                modes::ROVER_HOLD,
                                mode_reason::EKF_FAILSAFE,
                                vec!["EKF variance".to_string(), "EKF failsafe".to_string()],
                            )
                        } else {
                            (
                                modes::RTL,
                                mode_reason::EKF_FAILSAFE,
                                vec!["EKF variance".to_string()],
                            )
                        });
                    }
                }
                if let Some((m, _, _)) = &mode_change {
                    cur_mode = *m;
                }
                for (src, alive) in sources {
                    if !alive {
                        continue;
                    }
                    let onboard = src == SourceRole::Fc;
                    if t % HEARTBEAT_MS == 0 {
                        push(
                            &mut events,
                            src,
                            ChannelId::Heartbeat,
                            EventKind::Heartbeat {
                                custom_mode: u32::from(cur_mode),
                                system_status: 4, // MAV_STATE_ACTIVE
                            },
                        );
                    }
                    if t % SYS_STATUS_MS == 0 {
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::SysStatus {
                                voltage_mv: 12_400_u16.saturating_sub((t / 1000) as u16),
                                battery_remaining: (95 - (t / 10_000) as i64).max(0) as i8,
                                drop_rate_comm: 0,
                            },
                        );
                    }
                    if t % GPS_MS == 0 {
                        push(
                            &mut events,
                            src,
                            ChannelId::GpsEkf,
                            EventKind::GpsRawInt {
                                fix_type,
                                satellites: sats,
                                eph_cm: eph,
                                lat_e7,
                                lon_e7,
                                alt_mm,
                                jam_ind: jam,
                                time_valid,
                            },
                        );
                        push(
                            &mut events,
                            src,
                            ChannelId::GpsEkf,
                            EventKind::GlobalPositionInt {
                                lat_e7,
                                lon_e7,
                                alt_mm,
                                hdg_cdeg: 9_000,
                            },
                        );
                    }
                    if t % EKF_MS == 0 {
                        let deg = onset_age(FailureKind::GnssDegradation, t)
                            .map_or(0.0, |age| (age.min(10_000) as f64) / 10_000.0);
                        let sp = if inconsistent {
                            150
                        } else {
                            (20.0 + 130.0 * deg) as i16
                        };
                        let solution = if deg >= 1.0 {
                            0x0000_4000 | 0x0000_0001 // gps_glitching, attitude
                        } else {
                            0x0000_2001 // horiz_pos_abs, attitude
                        };
                        push(
                            &mut events,
                            src,
                            ChannelId::GpsEkf,
                            EventKind::EkfStatus {
                                sqrtvar_v_x100: (20.0 + 100.0 * deg) as i16,
                                sqrtvar_p_x100: sp,
                                sqrtvar_h_x100: 15,
                                sqrtvar_m_x100: 10,
                                solution_status: solution,
                                timeouts: if deg >= 1.0 { 0x03 } else { 0 },
                                offset_north_m: if inconsistent { 50.0 } else { 0.0 },
                            },
                        );
                    }
                    if t % RCIN_MS == 0 {
                        if !rc_lost {
                            last_rc = [
                                1500 + (rng.below(20) as u16),
                                1500 + (rng.below(20) as u16),
                                1300 + ((t / 1000) % 200) as u16,
                                1500,
                            ];
                        }
                        let rssi_pct = (100.0 - 80.0 * rc_prelude) as u8;
                        let lq_pct = if rc_prelude > 0.8 {
                            (100.0 - 400.0 * (rc_prelude - 0.8)) as u8
                        } else {
                            100
                        };
                        push(
                            &mut events,
                            src,
                            ChannelId::Rc,
                            EventKind::RcIn {
                                chans: last_rc,
                                rssi_pct: if rc_lost { 0 } else { rssi_pct },
                                lq_pct: if rc_lost { 0 } else { lq_pct },
                                valid: !rc_lost,
                            },
                        );
                    }
                    if let Some((mode, reason, texts)) = &mode_change {
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::ModeChange {
                                mode: *mode,
                                reason: *reason,
                            },
                        );
                        for text in texts {
                            push(
                                &mut events,
                                src,
                                ChannelId::Event,
                                EventKind::StatusText {
                                    severity: 2,
                                    text: text.clone(),
                                },
                            );
                        }
                    }
                    if onset_plus(FailureKind::GnssDegradation, t, 5_000)
                        || onset_plus(FailureKind::GnssInconsistency, t, 2_000)
                    {
                        if onboard {
                            push(
                                &mut events,
                                src,
                                ChannelId::Event,
                                EventKind::Err {
                                    subsys: err_subsys::GPS,
                                    ecode: 2, // GPS_GLITCH
                                },
                            );
                        }
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::StatusText {
                                severity: 2,
                                text: "GPS Glitch or Compass error".to_string(),
                            },
                        );
                    }
                    if onboard && onset_plus(FailureKind::GnssDegradation, t, 6_000) {
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::Err {
                                subsys: err_subsys::EKFCHECK,
                                ecode: 2, // BAD_VARIANCE
                            },
                        );
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::Err {
                                subsys: err_subsys::FAILSAFE_EKFINAV,
                                ecode: 1,
                            },
                        );
                    }
                    if reboot_at_ms == Some(t) {
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::Reboot { epoch: boot_epoch },
                        );
                        push(
                            &mut events,
                            src,
                            ChannelId::Event,
                            EventKind::StatusText {
                                severity: 1,
                                text: if onboard {
                                    "Watchdog reset: log restarted".to_string()
                                } else {
                                    "WDG: reset".to_string()
                                },
                            },
                        );
                    }
                }
                if t % RADIO_MS == 0 && radio_alive {
                    let f = prelude(FailureKind::TelemetryLinkLoss, t);
                    let (rssi, noise, rxerrors) = if f > 0.0 {
                        (
                            (180.0 - 150.0 * f) as u8,
                            (40.0 + 120.0 * f) as u8,
                            (f * 200.0) as u16,
                        )
                    } else {
                        (180 + rng.below(10) as u8, 40 + rng.below(5) as u8, 0)
                    };
                    let ev = EventKind::RadioStatus {
                        rssi,
                        remrssi: rssi.saturating_sub(5),
                        noise,
                        rxerrors,
                    };
                    if has_gcs {
                        push(
                            &mut events,
                            SourceRole::Gcs,
                            ChannelId::LinkStats,
                            ev.clone(),
                        );
                    }
                    if has_fc && !fc_dead {
                        push(&mut events, SourceRole::Fc, ChannelId::LinkStats, ev);
                    }
                }
            }
            if asset.family == Family::Fpv {
                let fiber = find(FailureKind::FiberBreak, t);
                let inj_rc = find(FailureKind::RcLinkLoss, t);
                let rf_lost = inj_rc.is_some();
                let rc_stop = rf_lost || fiber.is_some();
                let rc_stop_age = inj_rc.or(fiber).map(|i| t - i.t_start_ms);
                let f = prelude(FailureKind::RcLinkLoss, t);
                let order = inj
                    .iter()
                    .find(|i| i.kind == FailureKind::RcLinkLoss)
                    .map_or(PreludeOrder::NoiseLimited, |i| i.params.prelude_order);
                let (rss, rqly, rsnr, tpwr): (i16, u8, i8, u16) = match order {
                    PreludeOrder::NoiseLimited => {
                        let rss = (-70.0 + (f64::from(ELRS_SENSITIVITY_DBM) + 70.0) * f) as i16;
                        let rqly = if f > 0.85 {
                            (100.0 - 300.0 * (f - 0.85)) as u8
                        } else {
                            100
                        };
                        (
                            rss,
                            rqly,
                            (12.0 - 20.0 * f) as i8,
                            if f > 0.5 { 250 } else { 25 },
                        )
                    }
                    PreludeOrder::InterferenceLimited => {
                        let rss = -70 - (5.0 * f) as i16;
                        let rqly = (100.0 - 70.0 * f) as u8;
                        (
                            rss,
                            rqly,
                            (12.0 - 30.0 * f) as i8,
                            if f > 0.2 { 250 } else { 25 },
                        )
                    }
                };
                let bb_truncated = fiber
                    .is_some_and(|i| t >= i.t_start_ms + FIBER_BLACKBOX_TRUNCATE_MS)
                    || fc_dead;
                if has_fc && !bb_truncated && t % BLACKBOX_MS == 0 {
                    loop_iter = loop_iter.wrapping_add(128);
                    let age = rc_stop_age.unwrap_or(u64::MAX);
                    let rx_sig = !(rc_stop && age >= 150);
                    let recovered_recently = inj.iter().any(|i| {
                        matches!(i.kind, FailureKind::RcLinkLoss | FailureKind::FiberBreak)
                            && t >= i.t_end_ms()
                            && t < i.t_end_ms() + 1_000
                    });
                    let failsafe_phase = if rc_stop {
                        if age >= 1_500 {
                            2
                        } else if age >= 150 {
                            1
                        } else {
                            0
                        }
                    } else if recovered_recently {
                        5 // RX_LOSS_RECOVERED
                    } else {
                        0
                    };
                    let rssi_1023 = if asset.link_profile == LinkProfile::Fiber {
                        None
                    } else if rc_stop && age >= 250 {
                        Some(0)
                    } else {
                        Some(((f64::from(rss) + 130.0) / 60.0 * 1023.0).clamp(0.0, 1023.0) as u16)
                    };
                    push(
                        &mut events,
                        SourceRole::Fc,
                        ChannelId::Onboard,
                        EventKind::BlackboxRow {
                            loop_iteration: loop_iter,
                            rc_command: [
                                (rng.below(21) as i16) - 10,
                                (rng.below(21) as i16) - 10,
                                1_200 + ((t / 1000) % 300) as i16,
                                0,
                            ],
                            vbat_v: 16.2 - secs as f32 * 0.005,
                            amperage_a: 12.0 + (rng.below(30) as f32) / 10.0,
                            rssi: rssi_1023,
                            gps_num_sat: 12,
                            gps_deg: Some((lat, lon)),
                            failsafe_phase,
                            rx_signal_received: rx_sig,
                            rx_flight_channels_valid: rx_sig,
                        },
                    );
                }
                let handset_exists = asset.sources.contains(&SourceRole::Handset)
                    && asset.link_profile != LinkProfile::Fiber;
                if handset_exists && t % HANDSET_MS == 0 {
                    let handset_dead =
                        rf_lost || (fc_dead && asset.link_profile == LinkProfile::AnalogRf);
                    let loss_mode = inj_rc.map_or(HandsetLossMode::ZeroWithGpsBlank, |i| {
                        i.params.handset_loss_mode
                    });
                    let kind = if handset_dead {
                        match (loss_mode, &last_handset) {
                            (HandsetLossMode::Frozen, Some(prev)) => prev.clone(),
                            _ => EventKind::HandsetRow {
                                rss_dbm: 0,
                                rqly_pct: 0,
                                rsnr_db: 0,
                                tpwr_mw: 0,
                                rxbt_v: 0.0,
                                sats: 0,
                                gps_deg: None,
                                link_alive: false,
                            },
                        }
                    } else {
                        let row = EventKind::HandsetRow {
                            rss_dbm: rss - rng.below(3) as i16,
                            rqly_pct: rqly,
                            rsnr_db: rsnr,
                            tpwr_mw: tpwr,
                            rxbt_v: 16.2 - secs as f32 * 0.005,
                            sats: 12,
                            gps_deg: Some((lat, lon)),
                            link_alive: true,
                        };
                        last_handset = Some(row.clone());
                        row
                    };
                    push(&mut events, SourceRole::Handset, ChannelId::LinkStats, kind);
                }
                if asset.sources.contains(&SourceRole::Video) && t % VIDEO_MS == 0 {
                    let present = !(active(FailureKind::CameraStop, t) || fiber.is_some());
                    push(
                        &mut events,
                        SourceRole::Video,
                        ChannelId::Video,
                        EventKind::VideoPresence { present },
                    );
                }
            }
        }
    }
    let ground_truth = s
        .assets
        .iter()
        .flat_map(|a| {
            s.injections
                .iter()
                .filter(move |i| i.family == a.family)
                .map(move |i| GroundTruth {
                    kind: i.kind,
                    family: a.family,
                    asset_id: a.asset_id.clone(),
                    t_start_ms: i.t_start_ms,
                    t_end_ms: i.t_end_ms(),
                    affected_channels: affected_channels(i.kind, a),
                    params: i.params,
                    missing_channels: a.suppressed_channels.clone(),
                    link_profile: a.link_profile,
                })
        })
        .collect();
    Timeline {
        t0_unix_us: s.t0_unix_us,
        duration_ms: s.duration_ms,
        assets: s.assets.clone(),
        events,
        ground_truth,
    }
}

#[must_use]
pub fn pre_demo_assets() -> Vec<Asset> {
    vec![
        Asset {
            asset_id: "ugv-01".into(),
            family: Family::Ugv,
            sources: vec![SourceRole::Fc, SourceRole::Gcs],
            origin_lat_deg: 35.6762,
            origin_lon_deg: 139.6503,
            fc_log: FcLogFormat::ArduPilotBin,
            link_profile: LinkProfile::AnalogRf,
            boot_lead_us: 37_000_000,
            drift_ppm: 20,
            suppressed_channels: Vec::new(),
            handset_tmr10ms: false,
        },
        Asset {
            asset_id: "plane-01".into(),
            family: Family::FixedWing,
            sources: vec![SourceRole::Fc, SourceRole::Gcs],
            origin_lat_deg: 35.70,
            origin_lon_deg: 139.70,
            fc_log: FcLogFormat::Px4Ulog,
            link_profile: LinkProfile::AnalogRf,
            boot_lead_us: 123_000_000,
            drift_ppm: 0,
            suppressed_channels: Vec::new(),
            handset_tmr10ms: false,
        },
        Asset {
            asset_id: "fpv-01".into(),
            family: Family::Fpv,
            sources: vec![SourceRole::Fc, SourceRole::Handset, SourceRole::Video],
            origin_lat_deg: 35.65,
            origin_lon_deg: 139.60,
            fc_log: FcLogFormat::BlackboxCsv,
            link_profile: LinkProfile::AnalogRf,
            boot_lead_us: 8_000_000,
            drift_ppm: 0,
            suppressed_channels: Vec::new(),
            handset_tmr10ms: false,
        },
    ]
}

#[must_use]
pub fn pre_demo_default(seed: u64) -> Scenario {
    let mut s = Scenario {
        seed,
        t0_unix_us: 1_788_166_800_000_000,
        duration_ms: 180_000,
        assets: pre_demo_assets(),
        injections: Vec::new(),
    };
    s.inject(FailureKind::TelemetryLinkLoss, 60_000, 30_000, Family::Ugv);
    s.inject(FailureKind::GnssDegradation, 120_000, 40_000, Family::Ugv);
    s.inject(FailureKind::FcFailure, 90_000, 90_000, Family::FixedWing);
    s.inject(FailureKind::RcLinkLoss, 70_000, 20_000, Family::Fpv);
    s.inject(FailureKind::CameraStop, 120_000, 30_000, Family::Fpv);
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    fn only(kind: FailureKind, t: u64, d: u64, family: Family) -> Timeline {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        s.inject(kind, t, d, family);
        generate(&s)
    }

    #[test]
    fn same_seed_yields_identical_timeline() {
        let a = generate(&pre_demo_default(42));
        let b = generate(&pre_demo_default(42));
        assert_eq!(a, b);
        assert_ne!(generate(&pre_demo_default(43)).events, a.events);
    }

    #[test]
    fn telemetry_link_loss_silences_gcs_but_not_fc() {
        let tl = only(FailureKind::TelemetryLinkLoss, 60_000, 30_000, Family::Ugv);
        let ugv_gcs_in_window = tl.events.iter().filter(|e| {
            e.asset_id == "ugv-01"
                && e.source == SourceRole::Gcs
                && (60_000..90_000).contains(&e.t_ms)
        });
        assert_eq!(
            ugv_gcs_in_window.count(),
            0,
            "GCS stream must be silent in the window"
        );
        let ugv_fc_in_window = tl.events.iter().filter(|e| {
            e.asset_id == "ugv-01"
                && e.source == SourceRole::Fc
                && (60_000..90_000).contains(&e.t_ms)
        });
        assert!(
            ugv_fc_in_window.count() > 0,
            "onboard must continue (R-02 §0-1)"
        );
        assert!(tl.events.iter().any(|e| e.source == SourceRole::Fc
            && e.t_ms == 65_000
            && matches!(&e.kind, EventKind::ModeChange { reason, .. } if *reason == mode_reason::FAILSAFE)));
        assert!(tl.events.iter().any(|e| e.source == SourceRole::Fc
            && matches!(&e.kind, EventKind::StatusText { text, .. } if text == "GCS Failsafe")));
        assert!(
            !tl.events
                .iter()
                .any(|e| matches!(e.kind, EventKind::Err { subsys: 8, .. }))
        );
        assert_eq!(tl.ground_truth.len(), 1);
        assert_eq!(tl.ground_truth[0].kind, FailureKind::TelemetryLinkLoss);
    }

    #[test]
    fn fc_failure_keeps_radio_status_alive_as_discriminator() {
        let tl = only(FailureKind::FcFailure, 90_000, 30_000, Family::FixedWing);
        let radio = tl.events.iter().filter(|e| {
            e.asset_id == "plane-01"
                && matches!(e.kind, EventKind::RadioStatus { .. })
                && (90_000..120_000).contains(&e.t_ms)
        });
        assert!(
            radio.count() > 0,
            "RADIO_STATUS continues when the radio is alive (D1 variant B)"
        );
        let hb = tl.events.iter().filter(|e| {
            e.asset_id == "plane-01"
                && matches!(e.kind, EventKind::Heartbeat { .. })
                && (90_000..120_000).contains(&e.t_ms)
        });
        assert_eq!(hb.count(), 0);
        assert!(!tl.events.iter().any(|e| e.asset_id == "plane-01"
            && e.source == SourceRole::Fc
            && (90_000..120_000).contains(&e.t_ms)));
    }

    #[test]
    fn fc_failure_reboot_variant_resumes_with_new_boot_epoch_and_watchdog_marker() {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        let mut i = Injection::new(FailureKind::FcFailure, 90_000, 60_000, Family::FixedWing);
        i.params.log_end_mode = LogEndMode::Reboot { after_ms: 20_000 };
        s.inject_with(i);
        let tl = generate(&s);
        let after: Vec<&Event> = tl
            .events
            .iter()
            .filter(|e| e.asset_id == "plane-01" && e.t_ms >= 110_000)
            .collect();
        assert!(after.iter().all(|e| e.boot_epoch == 1));
        assert!(
            after
                .iter()
                .any(|e| matches!(e.kind, EventKind::Reboot { epoch: 1 }))
        );
        let first = after.first().expect("events after reboot");
        assert!(
            first.t_boot_us < 10_000_000,
            "time_boot rewinds: {}",
            first.t_boot_us
        );
        assert!(after.iter().any(|e| e.source == SourceRole::Gcs
            && matches!(&e.kind, EventKind::StatusText { text, .. } if text.starts_with("WDG"))));
    }

    #[test]
    fn affected_channels_match_what_generate_silences() {
        for (kind, family, asset, src) in [
            (
                FailureKind::TelemetryLinkLoss,
                Family::Ugv,
                "ugv-01",
                SourceRole::Gcs,
            ),
            (
                FailureKind::FcFailure,
                Family::FixedWing,
                "plane-01",
                SourceRole::Gcs,
            ),
            (
                FailureKind::FcFailure,
                Family::FixedWing,
                "plane-01",
                SourceRole::Fc,
            ),
            (
                FailureKind::FcFailure,
                Family::Fpv,
                "fpv-01",
                SourceRole::Fc,
            ),
            (
                FailureKind::RcLinkLoss,
                Family::Fpv,
                "fpv-01",
                SourceRole::Fc,
            ),
            (
                FailureKind::CameraStop,
                Family::Fpv,
                "fpv-01",
                SourceRole::Video,
            ),
        ] {
            let tl = only(kind, 60_000, 30_000, family);
            let gt = &tl.ground_truth[0];
            let chans = |lo: u64, hi: u64| -> std::collections::BTreeSet<ChannelId> {
                tl.events
                    .iter()
                    .filter(|e| {
                        e.asset_id == asset && e.source == src && (lo..hi).contains(&e.t_ms)
                    })
                    .map(|e| e.channel)
                    .collect()
            };
            let before = chans(0, 60_000);
            let during = chans(60_000, 90_000);
            for c in before.difference(&during) {
                assert!(
                    gt.affected_channels.contains(c),
                    "{kind:?}/{src:?}: silenced {c:?} missing from ground truth"
                );
            }
        }
        for (lp, expect_handset_rows) in [
            (LinkProfile::Fiber, false),
            (LinkProfile::FiberPlusRadio, true),
        ] {
            let mut s = pre_demo_default(1);
            s.injections.clear();
            s.assets[2].link_profile = lp;
            s.inject(FailureKind::FiberBreak, 60_000, 30_000, Family::Fpv);
            let tl = generate(&s);
            let rows: Vec<&Event> = tl
                .events
                .iter()
                .filter(|e| e.source == SourceRole::Handset && (60_000..90_000).contains(&e.t_ms))
                .collect();
            assert_eq!(!rows.is_empty(), expect_handset_rows, "{lp:?}");
            if expect_handset_rows {
                assert!(rows.iter().all(|e| matches!(
                    e.kind,
                    EventKind::HandsetRow {
                        link_alive: true,
                        ..
                    }
                )));
            }
            let gt = &tl.ground_truth[0];
            assert_eq!(gt.link_profile, lp);
            assert_eq!(
                gt.affected_channels.contains(&ChannelId::LinkStats),
                lp == LinkProfile::Fiber
            );
            assert!(gt.affected_channels.contains(&ChannelId::Video));
            let video_at = |t: u64| {
                tl.events
                    .iter()
                    .find(|e| e.source == SourceRole::Video && e.t_ms == t)
                    .map(|e| e.kind.clone())
            };
            assert_eq!(
                video_at(60_000),
                Some(EventKind::VideoPresence { present: false })
            );
            assert_eq!(
                video_at(59_000),
                Some(EventKind::VideoPresence { present: true })
            );
            let last_bb = tl
                .events
                .iter()
                .filter(|e| e.asset_id == "fpv-01" && e.source == SourceRole::Fc && e.t_ms < 90_000)
                .map(|e| e.t_ms)
                .max()
                .expect("bb rows");
            assert_eq!(last_bb, 62_900);
        }
    }

    #[test]
    fn gnss_degradation_ramps_sats_down_and_drops_fix_type() {
        let tl = only(FailureKind::GnssDegradation, 120_000, 40_000, Family::Ugv);
        let gps = |t: u64| {
            tl.events
                .iter()
                .find(|e| {
                    e.asset_id == "ugv-01"
                        && e.source == SourceRole::Gcs
                        && e.t_ms == t
                        && matches!(e.kind, EventKind::GpsRawInt { .. })
                })
                .map(|e| match &e.kind {
                    EventKind::GpsRawInt {
                        fix_type,
                        satellites,
                        eph_cm,
                        ..
                    } => (*fix_type, *satellites, *eph_cm),
                    _ => unreachable!(),
                })
                .expect("gps event")
        };
        let (f0, s0, e0) = gps(119_000);
        let (f1, s1, e1) = gps(135_000);
        assert_eq!(f0, 3);
        assert!(s0 >= 14 && s1 == 3, "sats 14→3: {s0}→{s1}");
        assert!(e1 > e0 * 10, "eph rises: {e0}→{e1}");
        assert_eq!(f1, 1, "fix_type 3→1 after the 10 s ramp");
        assert!(tl.events.iter().any(|e| e.t_ms == 125_000
            && matches!(&e.kind, EventKind::StatusText { text, .. } if text.contains("GPS Glitch"))));
        let err_at = |t: u64, subsys: u8, ecode: u8| {
            tl.events.iter().any(|e| {
                e.source == SourceRole::Fc
                    && e.t_ms == t
                    && e.kind == EventKind::Err { subsys, ecode }
            })
        };
        assert!(err_at(125_000, 11, 2));
        assert!(err_at(126_000, 16, 2));
        assert!(err_at(126_000, 17, 1));
    }

    #[test]
    fn rover_and_plane_radio_failsafe_differ_in_mode_reason_and_text_without_err() {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        s.assets[1].fc_log = FcLogFormat::ArduPilotBin;
        s.inject(FailureKind::RcLinkLoss, 60_000, 20_000, Family::Ugv);
        s.inject(FailureKind::RcLinkLoss, 60_000, 20_000, Family::FixedWing);
        let tl = generate(&s);
        let find_mode = |asset: &str, t: u64| {
            tl.events
                .iter()
                .find(|e| {
                    e.asset_id == asset
                        && e.source == SourceRole::Fc
                        && e.t_ms == t
                        && matches!(e.kind, EventKind::ModeChange { .. })
                })
                .map(|e| e.kind.clone())
        };
        assert_eq!(
            find_mode("ugv-01", 61_000),
            Some(EventKind::ModeChange {
                mode: modes::ROVER_HOLD,
                reason: mode_reason::FAILSAFE
            })
        );
        assert_eq!(
            find_mode("plane-01", 60_500),
            Some(EventKind::ModeChange {
                mode: modes::PLANE_CIRCLE,
                reason: mode_reason::RADIO_FAILSAFE
            })
        );
        assert_eq!(
            find_mode("plane-01", 65_000),
            Some(EventKind::ModeChange {
                mode: modes::RTL,
                reason: mode_reason::RADIO_FAILSAFE
            })
        );
        let has_text = |asset: &str, s: &str| {
            tl.events.iter().any(|e| {
                e.asset_id == asset
                    && matches!(&e.kind, EventKind::StatusText { text, .. } if text == s)
            })
        };
        assert!(has_text("ugv-01", "Radio Failsafe"));
        assert!(has_text("plane-01", "RC Short Failsafe On"));
        assert!(
            !tl.events
                .iter()
                .any(|e| matches!(e.kind, EventKind::Err { subsys: 5, .. }))
        );
        let rc: Vec<&Event> = tl
            .events
            .iter()
            .filter(|e| {
                e.asset_id == "ugv-01"
                    && e.source == SourceRole::Fc
                    && (60_000..80_000).contains(&e.t_ms)
                    && matches!(e.kind, EventKind::RcIn { .. })
            })
            .collect();
        assert!(!rc.is_empty());
        let chans: std::collections::BTreeSet<[u16; 4]> = rc
            .iter()
            .map(|e| match e.kind {
                EventKind::RcIn { chans, .. } => chans,
                _ => unreachable!(),
            })
            .collect();
        assert_eq!(chans.len(), 1, "frozen RCIN");
        assert!(rc.iter().all(|e| matches!(
            e.kind,
            EventKind::RcIn {
                valid: false,
                lq_pct: 0,
                ..
            }
        )));
    }

    #[test]
    fn fpv_rc_link_loss_writes_zero_rows_with_gps_blank() {
        let tl = only(FailureKind::RcLinkLoss, 70_000, 20_000, Family::Fpv);
        let rows: Vec<&Event> = tl
            .events
            .iter()
            .filter(|e| e.source == SourceRole::Handset && (70_000..90_000).contains(&e.t_ms))
            .collect();
        assert!(!rows.is_empty());
        for r in rows {
            match &r.kind {
                EventKind::HandsetRow {
                    rqly_pct,
                    gps_deg,
                    link_alive,
                    ..
                } => {
                    assert_eq!(*rqly_pct, 0);
                    assert!(gps_deg.is_none());
                    assert!(!link_alive);
                }
                other => panic!("unexpected {other:?}"),
            }
        }
        let pre = tl
            .events
            .iter()
            .find(|e| e.source == SourceRole::Handset && e.t_ms == 69_000)
            .expect("row at 69 s");
        assert!(matches!(pre.kind, EventKind::HandsetRow { rqly_pct, .. } if rqly_pct < 100));
        let bb = |t: u64| {
            tl.events
                .iter()
                .find(|e| {
                    e.source == SourceRole::Fc
                        && e.t_ms == t
                        && matches!(e.kind, EventKind::BlackboxRow { .. })
                })
                .map(|e| e.kind.clone())
                .expect("bb row")
        };
        assert!(matches!(
            bb(70_000),
            EventKind::BlackboxRow {
                rx_signal_received: true,
                failsafe_phase: 0,
                ..
            }
        ));
        assert!(matches!(
            bb(70_200),
            EventKind::BlackboxRow { rx_signal_received: false, failsafe_phase: 1, rssi: Some(r), .. } if r > 0
        ));
        assert!(matches!(
            bb(70_300),
            EventKind::BlackboxRow { rssi: Some(0), .. }
        ));
        assert!(matches!(
            bb(71_500),
            EventKind::BlackboxRow {
                failsafe_phase: 2,
                ..
            }
        ));
    }

    #[test]
    fn handset_frozen_variant_and_interference_limited_prelude() {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        let mut i = Injection::new(FailureKind::RcLinkLoss, 70_000, 20_000, Family::Fpv);
        i.params.handset_loss_mode = HandsetLossMode::Frozen;
        i.params.prelude_order = PreludeOrder::InterferenceLimited;
        s.inject_with(i);
        let tl = generate(&s);
        let row = |t: u64| {
            tl.events
                .iter()
                .find(|e| e.source == SourceRole::Handset && e.t_ms == t)
                .map(|e| e.kind.clone())
                .expect("row")
        };
        assert_eq!(row(75_000), row(69_000));
        assert_eq!(row(85_000), row(69_000));
        assert!(matches!(
            row(65_000),
            EventKind::HandsetRow { rss_dbm, rqly_pct, .. } if rss_dbm > -80 && rqly_pct < 90
        ));
    }

    #[test]
    fn boot_clock_has_lead_drift_and_gps_anchor_only_when_fixed() {
        let tl = only(FailureKind::GnssDegradation, 120_000, 40_000, Family::Ugv);
        let e0 = tl
            .events
            .iter()
            .find(|e| e.asset_id == "ugv-01" && e.t_ms == 0)
            .expect("t0 event");
        assert_eq!(e0.t_boot_us, 37_000_740);
        let e100 = tl
            .events
            .iter()
            .find(|e| e.asset_id == "ugv-01" && e.t_ms == 100_000)
            .expect("event");
        assert_eq!(e100.t_boot_us, 137_002_740);
        let tv = |t: u64| {
            tl.events
                .iter()
                .find(|e| {
                    e.asset_id == "ugv-01"
                        && e.source == SourceRole::Fc
                        && e.t_ms == t
                        && matches!(e.kind, EventKind::GpsRawInt { .. })
                })
                .map(|e| match e.kind {
                    EventKind::GpsRawInt { time_valid, .. } => time_valid,
                    _ => unreachable!(),
                })
                .expect("gps")
        };
        assert!(tv(100_000));
        assert!(!tv(135_000), "fix lost → GPS time anchor stops");
        let (wk, ms) = gps_week_ms(1_788_166_800_000_000);
        assert_eq!(wk, 2434);
        assert_eq!(ms, (86_400 + 9 * 3600 + 18) * 1000);
    }

    #[test]
    fn suppressed_channel_is_never_emitted_and_recorded_as_missing() {
        let mut s = pre_demo_default(1);
        s.injections.clear();
        s.assets[0].suppressed_channels = vec![ChannelId::LinkStats];
        s.inject(FailureKind::FcFailure, 60_000, 30_000, Family::Ugv);
        let tl = generate(&s);
        assert!(
            !tl.events
                .iter()
                .any(|e| e.asset_id == "ugv-01" && e.channel == ChannelId::LinkStats)
        );
        assert_eq!(
            tl.ground_truth[0].missing_channels,
            vec![ChannelId::LinkStats]
        );
        assert!(
            !tl.ground_truth[0]
                .affected_channels
                .contains(&ChannelId::LinkStats)
        );
    }
}
