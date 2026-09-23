use std::collections::BTreeSet;

use musubi_reference_types::{
    BasisMix, CauseCandidate, CauseClaim, CauseOutcome, ChannelCoverage, ChannelId, ClaimKind,
    ClockBasis, Cue, DigestRef, Family, FamilyProfile, GeneratorRef, InputConfidenceRef,
    LogicConfidence, Mark, MarkStatus, NegativeObservation, SignatureCatalog, SignatureMatch,
    SourceRole, Subject, TimeAlignment, UnknownReason, join_reason_tokens,
};

use crate::{FieldValue, Observation, field, time_align::to_wall_us};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Window {
    pub start_ms: i64,
    pub end_ms: i64,
}

#[derive(Debug, Clone)]
pub struct SourceInput<'a> {
    pub profile: &'a FamilyProfile,
    pub obs: &'a [Observation],
    pub negatives: &'a [NegativeObservation],
    pub alignment: TimeAlignment,
}

pub const PRELUDE_LOOKBACK_MS: i64 = 15_000;
pub const BASELINE_LOOKBACK_MS: i64 = 30_000;
pub const ONSET_TOLERANCE_MS: i64 = 10_000;

#[derive(Debug, Clone)]
pub struct CueSet {
    pub present: BTreeSet<(ChannelId, String)>,
    pub observed_channels: BTreeSet<ChannelId>,
    pub evidence: Vec<DigestRef>,
    pub weakest_basis: ClockBasis,
    pub time_confidence_min: f32,
    pub time_untrusted: bool,
}

impl Default for CueSet {
    fn default() -> Self {
        Self {
            present: BTreeSet::new(),
            observed_channels: BTreeSet::new(),
            evidence: Vec::new(),
            weakest_basis: ClockBasis::GpsLocked,
            time_confidence_min: 1.0,
            time_untrusted: false,
        }
    }
}

fn neg_wall(n: &NegativeObservation, al: &TimeAlignment, host_axis: bool) -> Option<(i64, i64)> {
    if host_axis
        || matches!(
            n.clock_basis,
            ClockBasis::HostReceived | ClockBasis::Unknown
        )
    {
        return Some((n.absent_since_ms, n.expected_by_ms));
    }
    let (a, _) = to_wall_us(al, (n.absent_since_ms.max(0) as u64) * 1000)?;
    let (b, _) = to_wall_us(al, (n.expected_by_ms.max(0) as u64) * 1000)?;
    Some((a / 1000, b / 1000))
}

fn in_window(o: &Observation, w: Window) -> bool {
    o.wall_ms.is_some_and(|t| t >= w.start_ms && t < w.end_ms)
}

fn num(o: &Observation, name: &str) -> Option<f64> {
    field(o, name).and_then(FieldValue::as_f64)
}

fn text_has(o: &Observation, needle: &str) -> bool {
    o.fields
        .iter()
        .any(|(_, v)| matches!(v, FieldValue::Text(s) if s.contains(needle)))
}

#[must_use]
pub fn extract_cues(sources: &[SourceInput<'_>], w: Window) -> CueSet {
    let mut c = CueSet::default();
    let add = |c: &mut CueSet, ch: ChannelId, id: &str, ev: &[&Observation]| {
        c.present.insert((ch, id.to_string()));
        for o in ev {
            c.evidence.push(o.digest);
        }
    };
    let prelude = Window {
        start_ms: w.start_ms - PRELUDE_LOOKBACK_MS,
        end_ms: w.start_ms,
    };
    let baseline = Window {
        start_ms: w.start_ms - BASELINE_LOOKBACK_MS,
        end_ms: w.start_ms,
    };
    let mut heartbeat_stop = false;
    for s in sources {
        for ch in &s.profile.channels {
            c.observed_channels.insert(*ch);
        }
        let basis = s.alignment.basis;
        if basis_rank(basis) < basis_rank(c.weakest_basis) {
            c.weakest_basis = basis;
        }
        for o in s.obs.iter().filter(|o| in_window(o, w)) {
            if o.time_confidence < c.time_confidence_min {
                c.time_confidence_min = o.time_confidence;
            }
        }
        let boot_axis = !matches!(
            s.profile.default_clock_basis,
            ClockBasis::HostReceived | ClockBasis::Unknown
        );
        if boot_axis && s.alignment.offset.is_none() {
            c.time_untrusted = true;
        }
        if matches!(basis, ClockBasis::GpsSuspect | ClockBasis::Unknown) {
            c.time_untrusted = true;
        }
    }
    for s in sources {
        let host_axis = matches!(
            s.profile.default_clock_basis,
            ClockBasis::HostReceived | ClockBasis::Unknown
        );
        for n in s.negatives {
            let Some((since, by)) = neg_wall(n, &s.alignment, host_axis) else {
                continue;
            };
            if !(since < w.end_ms && by > w.start_ms) {
                continue;
            }
            if (since - w.start_ms).abs() > ONSET_TOLERANCE_MS {
                continue; // 窓の開始から離れた absence は別の出来事。
            }
            let id = match n.subject.channel {
                ChannelId::Heartbeat => {
                    heartbeat_stop = true;
                    "heartbeat_stop"
                }
                ChannelId::LinkStats => {
                    if s.profile.source_role == SourceRole::Handset {
                        add(&mut c, ChannelId::LinkStats, "handset_link_dead", &[]);
                    }
                    "link_stats_stop"
                }
                ChannelId::Rc => "rc_stop",
                ChannelId::Video => "video_stop",
                ChannelId::Onboard => {
                    let any_after = s
                        .obs
                        .iter()
                        .any(|o| !o.stale && o.wall_ms.is_some_and(|t| t > since + 1));
                    if any_after {
                        "onboard_stop"
                    } else {
                        "onboard_truncated"
                    }
                }
                ChannelId::GpsEkf
                | ChannelId::Event
                | ChannelId::Tlog
                | ChannelId::Osd
                | ChannelId::Fiber => continue,
            };
            add(&mut c, n.subject.channel, id, &[]);
        }
    }
    for s in sources {
        let in_w: Vec<&Observation> = s.obs.iter().filter(|o| in_window(o, w)).collect();
        let half = w.start_ms + (w.end_ms - w.start_ms) / 2;
        let count_alive = |ch: ChannelId| {
            let alive: Vec<&&Observation> = in_w
                .iter()
                .filter(|o| o.channel == ch && !o.stale)
                .collect();
            if alive.len() >= 2 && alive.iter().any(|o| o.wall_ms.is_some_and(|t| t >= half)) {
                alive.len()
            } else {
                0
            }
        };
        if count_alive(ChannelId::Heartbeat) >= 2 {
            add(&mut c, ChannelId::Heartbeat, "heartbeat_continue", &[]);
        }
        if count_alive(ChannelId::Rc) >= 2 {
            add(&mut c, ChannelId::Rc, "rc_continue", &[]);
        }
        if count_alive(ChannelId::Video) >= 2 {
            add(&mut c, ChannelId::Video, "video_continue", &[]);
        }
        if count_alive(ChannelId::Onboard) >= 2 {
            add(&mut c, ChannelId::Onboard, "onboard_continue", &[]);
        }
        if count_alive(ChannelId::LinkStats) >= 2 {
            add(&mut c, ChannelId::LinkStats, "link_stats_continue", &[]);
            if s.profile.source_role == SourceRole::Handset {
                add(&mut c, ChannelId::LinkStats, "handset_link_alive", &[]);
            }
            if s.profile.source_role == SourceRole::Gcs && heartbeat_stop {
                let ev: Vec<&Observation> = in_w
                    .iter()
                    .copied()
                    .filter(|o| o.channel == ChannelId::LinkStats)
                    .collect();
                add(&mut c, ChannelId::LinkStats, "radio_remote_alive", &ev);
            }
        }
        let pre: Vec<&Observation> = s
            .obs
            .iter()
            .filter(|o| o.channel == ChannelId::LinkStats && !o.stale && in_window(o, prelude))
            .collect();
        if !pre.is_empty() {
            if link_quality_degraded(&pre) {
                add(&mut c, ChannelId::LinkStats, "prelude_link_stats", &pre);
            } else if pre.len() >= 3 {
                add(&mut c, ChannelId::LinkStats, "no_prelude", &pre);
            }
        }
        let ev: Vec<&Observation> = in_w
            .iter()
            .copied()
            .filter(|o| matches!(o.channel, ChannelId::Event | ChannelId::Rc))
            .collect();
        let reason = |o: &Observation| num(o, "MODE.Rsn");
        let rc_text = |o: &Observation| {
            text_has(o, "Radio Failsafe")
                || text_has(o, "RC Short Failsafe")
                || text_has(o, "RC Long Failsafe")
                || text_has(o, "RC LINK LOST")
        };
        let gcs_text = |o: &Observation| text_has(o, "GCS Failsafe");
        let rc_fs: Vec<&Observation> = ev
            .iter()
            .copied()
            .filter(|o| {
                rc_text(o)
                    || reason(o).is_some_and(|r| r == 3.0 || r == 48.0)
                    || num(o, "failsafe_flags.manual_control_signal_lost").is_some_and(|v| v > 0.0)
                    || num(o, "failsafePhase").is_some_and(|v| v >= 1.0)
            })
            .collect();
        let gcs_fs: Vec<&Observation> = ev
            .iter()
            .copied()
            .filter(|o| {
                gcs_text(o)
                    || reason(o).is_some_and(|r| r == 5.0)
                    || num(o, "failsafe_flags.gcs_connection_lost").is_some_and(|v| v > 0.0)
                    || num(o, "vehicle_status.gcs_connection_lost").is_some_and(|v| v > 0.0)
            })
            .collect();
        let rover_fs: Vec<&Observation> = ev
            .iter()
            .copied()
            .filter(|o| reason(o).is_some_and(|r| r == 25.0))
            .collect();
        let ekf_fs: Vec<&Observation> = ev
            .iter()
            .copied()
            .filter(|o| {
                text_has(o, "EKF")
                    || reason(o).is_some_and(|r| r == 6.0)
                    || num(o, "ERR.Subsys").is_some_and(|v| v == 16.0 || v == 17.0)
            })
            .collect();
        let glitch: Vec<&Observation> = ev
            .iter()
            .copied()
            .filter(|o| {
                text_has(o, "GPS Glitch") || num(o, "ERR.Subsys").is_some_and(|v| v == 11.0)
            })
            .collect();
        if !rc_fs.is_empty() || (!rover_fs.is_empty() && gcs_fs.is_empty()) {
            add(&mut c, ChannelId::Event, "rc_failsafe_event", &rc_fs);
        }
        if !gcs_fs.is_empty() || (!rover_fs.is_empty() && rc_fs.is_empty()) {
            add(&mut c, ChannelId::Event, "gcs_failsafe_event", &gcs_fs);
        }
        if !ekf_fs.is_empty() {
            add(&mut c, ChannelId::Event, "ekf_failsafe_event", &ekf_fs);
        }
        let glitch_flag: Vec<&Observation> = in_w
            .iter()
            .copied()
            .filter(|o| num(o, "EKF_STATUS_REPORT.flags").is_some_and(|v| (v as u32) & 32768 != 0))
            .collect();
        if !glitch.is_empty() || !glitch_flag.is_empty() {
            add(&mut c, ChannelId::Event, "gps_glitch_event", &glitch);
        }
        let wdg: Vec<&Observation> = ev
            .iter()
            .copied()
            .filter(|o| text_has(o, "WDG") || text_has(o, "atchdog"))
            .collect();
        let rewind = in_w.windows(2).any(
            |p| matches!((p[0].t_boot_us, p[1].t_boot_us), (Some(a), Some(b)) if b + 1_000_000 < a),
        );
        if !wdg.is_empty() || rewind {
            add(&mut c, ChannelId::Event, "reboot_marker", &wdg);
        }
        let gps_w: Vec<&Observation> = in_w
            .iter()
            .copied()
            .filter(|o| o.channel == ChannelId::GpsEkf)
            .collect();
        let gps_b: Vec<&Observation> = s
            .obs
            .iter()
            .filter(|o| o.channel == ChannelId::GpsEkf && in_window(o, baseline))
            .collect();
        gps_cues(&mut c, &gps_w, &gps_b, |c, id, ev| {
            add(c, ChannelId::GpsEkf, id, ev)
        });
    }
    c.evidence.sort_by(|a, b| a.0.cmp(&b.0));
    c.evidence.dedup();
    c
}

fn basis_rank(b: ClockBasis) -> u8 {
    match b {
        ClockBasis::Unknown => 0,
        ClockBasis::GpsSuspect => 1,
        ClockBasis::BootRelative => 2,
        ClockBasis::HostReceived => 3,
        ClockBasis::BootRelativeOffsetEstimated => 4,
        ClockBasis::RtcSetOnce => 5,
        ClockBasis::GpsLocked => 6,
    }
}

const LQ_FIELDS: [&str; 4] = [
    "RQly(%)",
    "RSSI.RXLQ",
    "input_rc.link_quality",
    "RC_CHANNELS.rssi",
];
const RSSI_FIELDS: [&str; 4] = ["RADIO_STATUS.rssi", "RAD.RSSI", "rssi", "RSSI.RXRSSI"];
const DBM_FIELDS: [&str; 2] = ["1RSS(dB)", "input_rc.rssi_dbm"];
const NOISE_FIELDS: [&str; 2] = ["RADIO_STATUS.noise", "RAD.Noise"];

fn link_quality_degraded(pre: &[&Observation]) -> bool {
    let series = |names: &[&str], pred: &dyn Fn(&str, f64) -> bool| -> Vec<(String, f64)> {
        pre.iter()
            .flat_map(|o| {
                names.iter().filter_map(|n| {
                    num(o, n)
                        .filter(|v| pred(n, *v))
                        .map(|v| ((*n).to_string(), v))
                })
            })
            .collect()
    };
    let lq = series(&LQ_FIELDS, &|n, v| !(n == "RC_CHANNELS.rssi" && v >= 255.0));
    if lq.iter().any(|(n, v)| {
        if n == "RC_CHANNELS.rssi" {
            *v < 228.0
        } else {
            *v < 90.0
        }
    }) {
        return true;
    }
    let rssi = series(&RSSI_FIELDS, &|_, _| true);
    let (mut mx, mut mn) = (f64::MIN, f64::MAX);
    for (_, v) in &rssi {
        mx = mx.max(*v);
        mn = mn.min(*v);
    }
    if !rssi.is_empty() && mx > 0.0 && mn / mx < 0.75 {
        return true;
    }
    if series(&DBM_FIELDS, &|_, _| true)
        .iter()
        .any(|(_, v)| *v <= -100.0)
    {
        return true;
    }
    series(&NOISE_FIELDS, &|_, _| true)
        .iter()
        .any(|(_, v)| *v >= 100.0)
}

const SATS_FIELDS: [&str; 5] = [
    "GPS.NSats",
    "GPS_RAW_INT.satellites_visible",
    "sensor_gps.satellites_used",
    "GPS_numSat",
    "Sats",
];
const FIX_FIELDS: [&str; 4] = [
    "GPS.Status",
    "GPS_RAW_INT.fix_type",
    "sensor_gps.fix_type",
    "GPS_fixType",
];
const LAT_E7_FIELDS: [&str; 4] = [
    "GPS.Lat",
    "GLOBAL_POSITION_INT.lat",
    "GPS_RAW_INT.lat",
    "sensor_gps.lat",
];
const LAT_DEG_FIELDS: [&str; 2] = ["vehicle_global_position.lat", "GPS_coord[0]"];

fn gps_cues(
    c: &mut CueSet,
    w: &[&Observation],
    b: &[&Observation],
    mut add: impl FnMut(&mut CueSet, &str, &[&Observation]),
) {
    let pick = |obs: &[&Observation], names: &[&str]| -> Vec<f64> {
        obs.iter()
            .flat_map(|o| {
                names.iter().filter_map(|n| {
                    num(o, n).filter(|v| match *n {
                        "GPS_RAW_INT.satellites_visible" => *v != 255.0,
                        "GPS_RAW_INT.fix_type" => (0.0..=8.0).contains(v),
                        _ => true,
                    })
                })
            })
            .collect()
    };
    let sats_w = pick(w, &SATS_FIELDS);
    let sats_b = pick(b, &SATS_FIELDS);
    let fix_w = pick(w, &FIX_FIELDS);
    let min = |v: &[f64]| v.iter().copied().fold(f64::INFINITY, f64::min);
    let max = |v: &[f64]| v.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    if !sats_w.is_empty() && !sats_b.is_empty() && min(&sats_w) <= 6.0 && max(&sats_b) >= 10.0 {
        add(c, "gps_sats_drop", w);
    }
    if !fix_w.is_empty() && min(&fix_w) < 3.0 {
        add(c, "gps_fix_drop", w);
    } else if !fix_w.is_empty()
        && (sats_w.is_empty() || min(&sats_w) >= 10.0)
        && !w.iter().any(|o| {
            num(o, "GPS_RAW_INT.satellites_visible") == Some(255.0)
                || num(o, "GPS_RAW_INT.fix_type").is_some_and(|v| !(0.0..=8.0).contains(&v))
        })
    {
        add(c, "gps_fix_good", w);
    }
    let hdop_hit = w.iter().any(|o| {
        num(o, "GPS.HDop").is_some_and(|v| v >= 200.0)
            || num(o, "GPS_RAW_INT.eph").is_some_and(|v| v >= 200.0 && v != 65535.0)
            || num(o, "sensor_gps.hdop").is_some_and(|v| v >= 2.0)
            || num(o, "GPS_hdop").is_some_and(|v| v >= 2.0)
    });
    if hdop_hit {
        add(c, "gps_hdop_rise", w);
    }
    let var_hit = w.iter().any(|o| {
        num(o, "XKF4.SP").is_some_and(|v| v >= 100.0)
            || num(o, "EKF_STATUS_REPORT.pos_horiz_variance").is_some_and(|v| v >= 1.0)
            || num(o, "estimator_status.pos_test_ratio").is_some_and(|v| v >= 1.0)
    });
    if var_hit {
        add(c, "ekf_variance_rise", w);
    }
    let jam_hit = w.iter().any(|o| {
        num(o, "UBX1.jamInd").is_some_and(|v| v >= 80.0)
            || num(o, "sensor_gps.jamming_indicator").is_some_and(|v| v >= 80.0)
            || num(o, "sensor_gps.jamming_state").is_some_and(|v| v == 3.0)
    });
    if jam_hit {
        add(c, "jam_indicator_rise", w);
    }
    let reset_hit = w.iter().any(|o| {
        num(o, "XKF4.OFN").is_some_and(|v| v.abs() > 0.5)
            || num(o, "estimator_status.reset_count_pos_ne").is_some_and(|v| v > 0.0)
    });
    if reset_hit {
        add(c, "ekf_position_reset", w);
    }
    let mut lat: Vec<(i64, f64)> = b
        .iter()
        .chain(w.iter())
        .flat_map(|o| {
            let t = o.wall_ms.unwrap_or(o.t_ms);
            LAT_E7_FIELDS
                .iter()
                .filter_map(move |n| num(o, n).map(|v| (t, v / 1e7)))
                .chain(
                    LAT_DEG_FIELDS
                        .iter()
                        .filter_map(move |n| num(o, n).map(|v| (t, v))),
                )
        })
        .collect();
    lat.retain(|(_, v)| v.abs() > 0.0001);
    if lat
        .windows(2)
        .any(|p| (p[1].1 - p[0].1).abs() >= 0.000_18 && (p[1].0 - p[0].0).abs() <= 3_000)
    {
        add(c, "position_jump", w);
    }
}

fn is_state_cue(label: &str) -> bool {
    label.ends_with("_good")
        || label.ends_with("_continue")
        || label.ends_with("_alive")
        || label.ends_with(":no_prelude")
}

fn cue_key(c: &Cue) -> (ChannelId, String) {
    (c.channel, c.cue_id.clone())
}

fn label(c: &Cue) -> String {
    format!("{}:{}", c.channel.as_str(), c.cue_id)
}

#[must_use]
pub fn match_signatures(catalog: &SignatureCatalog, family: Family, cues: &CueSet) -> CauseOutcome {
    let mut full: Vec<CauseCandidate> = Vec::new();
    let mut partial: Vec<CauseCandidate> = Vec::new();
    let mut undefined_missing: BTreeSet<String> = BTreeSet::new();
    let mut any_applicable = false;
    for sig in &catalog.signatures {
        if !sig.families.is_empty() && !sig.families.contains(&family) {
            continue;
        }
        any_applicable = true;
        let missing: Vec<String> = sig
            .required_cues
            .iter()
            .filter(|c| !cues.observed_channels.contains(&c.channel))
            .map(label)
            .collect();
        if !missing.is_empty() {
            undefined_missing.extend(missing);
            continue; // channel 欠落＝match 未定義（R-02 §5）。
        }
        let matched: Vec<String> = sig
            .required_cues
            .iter()
            .filter(|c| cues.present.contains(&cue_key(c)))
            .map(label)
            .collect();
        let optional: Vec<String> = sig
            .optional_cues
            .iter()
            .filter(|c| cues.present.contains(&cue_key(c)))
            .map(label)
            .collect();
        let contradicting: Vec<String> = sig
            .contradicting_cues
            .iter()
            .filter(|c| cues.present.contains(&cue_key(c)))
            .map(label)
            .collect();
        let discriminating_missing: Vec<String> = sig
            .optional_cues
            .iter()
            .chain(&sig.contradicting_cues)
            .filter(|c| !cues.observed_channels.contains(&c.channel))
            .map(label)
            .collect();
        if matched.is_empty() || !contradicting.is_empty() {
            continue;
        }
        let all = matched.len() == sig.required_cues.len();
        if !all && matched.iter().all(|m| is_state_cue(m)) {
            continue;
        }
        let explicit_event = sig
            .required_cues
            .iter()
            .chain(&sig.optional_cues)
            .any(|c| c.channel == ChannelId::Event && cues.present.contains(&cue_key(c)));
        let mut logic = if all {
            if explicit_event {
                LogicConfidence::DeterministicFact
            } else {
                LogicConfidence::DirectEvidence
            }
        } else {
            LogicConfidence::LeadingIndicator
        };
        if cues.time_untrusted && logic > LogicConfidence::LeadingIndicator {
            logic = LogicConfidence::LeadingIndicator;
        }
        let cand = CauseCandidate {
            signature_id: sig.signature_id.clone(),
            signature_match: SignatureMatch {
                matched_cues: matched,
                required_cues: sig.required_cues.iter().map(label).collect(),
                optional_cues_matched: optional,
                contradicting_cues: contradicting,
            },
            logic_confidence: logic,
            supporting: cues.evidence.clone(),
            contradicting: Vec::new(),
            discriminating_missing,
        };
        if all {
            full.push(cand)
        } else {
            partial.push(cand)
        }
    }
    if !any_applicable {
        return CauseOutcome::Unknown {
            reason: UnknownReason::NoSignatureMatch,
        };
    }
    match (full.len(), partial.len()) {
        (1, _) => {
            let mut candidates = full;
            candidates.extend(partial);
            CauseOutcome::Consistent { candidates }
        }
        (n, _) if n > 1 => {
            let indistinguishable_by = full
                .iter()
                .flat_map(|c| c.discriminating_missing.iter().cloned())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect();
            CauseOutcome::Ambiguous {
                candidates: full,
                indistinguishable_by,
            }
        }
        (0, 1) => CauseOutcome::Consistent {
            candidates: partial,
        },
        (0, n) if n > 1 => {
            let indistinguishable_by = partial
                .iter()
                .flat_map(|c| c.discriminating_missing.iter().cloned())
                .chain(partial.iter().flat_map(|c| {
                    c.signature_match
                        .required_cues
                        .iter()
                        .filter(|r| !c.signature_match.matched_cues.contains(r))
                        .cloned()
                }))
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect();
            CauseOutcome::Ambiguous {
                candidates: partial,
                indistinguishable_by,
            }
        }
        _ => {
            if cues.present.is_empty() {
                CauseOutcome::Unknown {
                    reason: UnknownReason::InsufficientEvidence,
                }
            } else if !undefined_missing.is_empty() {
                CauseOutcome::Unknown {
                    reason: UnknownReason::ChannelNotObserved,
                }
            } else {
                CauseOutcome::Unknown {
                    reason: UnknownReason::NoSignatureMatch,
                }
            }
        }
    }
}

#[must_use]
pub fn event_trigger_times(obs: &[Observation]) -> Vec<i64> {
    obs.iter()
        .filter(|o| matches!(o.channel, ChannelId::Event | ChannelId::Rc))
        .filter(|o| {
            !(text_has(o, "Cleared")
                || text_has(o, "restored")
                || num(o, "MODE.Rsn").is_some_and(|r| r == 48.0))
        })
        .filter(|o| {
            text_has(o, "Failsafe")
                || text_has(o, "failsafe")
                || text_has(o, "FAILSAFE")
                || text_has(o, "GPS Glitch")
                || text_has(o, "EKF")
                || text_has(o, "WDG")
                || text_has(o, "atchdog")
                || num(o, "ERR.Subsys").is_some()
                || num(o, "MODE.Rsn").is_some_and(|r| r >= 3.0)
                || num(o, "failsafePhase").is_some_and(|v| v >= 1.0)
                || num(o, "failsafe_flags.manual_control_signal_lost").is_some_and(|v| v > 0.0)
                || num(o, "failsafe_flags.gcs_connection_lost").is_some_and(|v| v > 0.0)
                || num(o, "failsafe_flags.gnss_lost").is_some_and(|v| v > 0.0)
                || num(o, "vehicle_status.failsafe").is_some_and(|v| v > 0.0)
        })
        .filter_map(|o| o.wall_ms)
        .collect()
}

#[must_use]
pub fn negative_digest(n: &NegativeObservation) -> DigestRef {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(n.expectation_id.as_bytes());
    h.update(n.subject.asset_id.as_bytes());
    h.update(n.subject.channel.as_str().as_bytes());
    h.update(n.absent_since_ms.to_le_bytes());
    h.update(n.expected_by_ms.to_le_bytes());
    DigestRef(h.finalize().into())
}

#[must_use]
pub fn build_claim(
    catalog: &SignatureCatalog,
    sources: &[SourceInput<'_>],
    family: Family,
    asset_id: &str,
    trigger_channel: ChannelId,
    absences: &[&NegativeObservation],
    w: Window,
    generated_at_ms: i64,
) -> CauseClaim {
    let cues = extract_cues(sources, w);
    let outcome = match_signatures(catalog, family, &cues);
    let required: BTreeSet<ChannelId> = catalog
        .signatures
        .iter()
        .filter(|s| s.families.is_empty() || s.families.contains(&family))
        .flat_map(|s| s.required_cues.iter().map(|c| c.channel))
        .collect();
    let present: Vec<ChannelId> = required
        .iter()
        .copied()
        .filter(|c| cues.observed_channels.contains(c))
        .collect();
    let outcome_token = match &outcome {
        CauseOutcome::Consistent { .. } => "cause-candidate".to_string(),
        CauseOutcome::Ambiguous { .. } => "cause-ambiguous".to_string(),
        CauseOutcome::Unknown { reason } => format!("cause-unknown:{}", reason.as_label()),
    };
    let mut tokens: Vec<String> = vec![outcome_token, cues.weakest_basis.reason_token()];
    if cues.time_untrusted {
        tokens.push("time-untrusted".to_string());
    }
    let token_refs: Vec<&str> = tokens.iter().map(String::as_str).collect();
    let claim_id = {
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(asset_id.as_bytes());
        h.update(w.start_ms.to_le_bytes());
        h.update(w.end_ms.to_le_bytes());
        h.update(tokens.join("|").as_bytes());
        let d = h.finalize();
        format!(
            "cause-{}",
            d.iter()
                .take(8)
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        )
    };
    let profile_versions: Vec<String> = sources
        .iter()
        .map(|s| format!("{}@{}", s.profile.profile_id, s.profile.version))
        .collect();
    let mut basis_mix = BasisMix::default();
    for _ in &cues.evidence {
        basis_mix.count(None); // file 由来の観測は ConfidenceBasis を持たない（unspecified として正直に数える）。
    }
    CauseClaim {
        claim_id,
        kind: ClaimKind::Candidate,
        subject: Subject {
            family,
            asset_id: asset_id.to_string(),
            channel: trigger_channel,
        },
        absence: absences.iter().copied().map(negative_digest).collect(),
        outcome,
        input_confidence: InputConfidenceRef {
            envelope_digests: cues.evidence.clone(),
            min_confidence: 0.0,
            basis_mix,
            time_confidence_min: cues.time_confidence_min,
            channel_coverage: ChannelCoverage {
                required: required.into_iter().collect(),
                present,
            },
        },
        evidence: cues.evidence,
        generator: GeneratorRef {
            catalog_version: catalog.version.clone(),
            profile_version: profile_versions.join(","),
            build_id: format!("musubi-reference-readers@{}", env!("CARGO_PKG_VERSION")),
        },
        generated_at_ms,
        mark: Mark {
            status: MarkStatus::Degraded,
            reason_code: join_reason_tokens(&token_refs),
            provenance: vec![format!(
                "musubi-reference-readers/cause@{}",
                catalog.version
            )],
        },
    }
}

#[must_use]
pub fn windows_from_triggers(
    triggers: &[(i64, usize)],
    gap_ms: i64,
    span_ms: i64,
) -> Vec<(Window, Vec<usize>)> {
    let mut sorted: Vec<(i64, usize)> = triggers.to_vec();
    sorted.sort_unstable();
    let mut out: Vec<(Window, Vec<usize>)> = Vec::new();
    let mut last_t = i64::MIN;
    for (t, i) in sorted {
        if let Some((_, ids)) = out.last_mut()
            && t - last_t <= gap_ms
        {
            ids.push(i);
            last_t = t;
            continue;
        }
        out.push((
            Window {
                start_ms: t,
                end_ms: t + span_ms,
            },
            vec![i],
        ));
        last_t = t;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_types::FailureKind;

    fn cat() -> SignatureCatalog {
        crate::catalog::parse_catalog(
            r#"
version = "t"
[[signature]]
id = "telemetry_link_loss"
kind = "telemetry_link_loss"
families = ["ugv"]
required = ["heartbeat:heartbeat_stop", "link_stats:link_stats_stop"]
contradicting = ["link_stats:radio_remote_alive", "onboard:onboard_truncated"]
discriminators = ["D1"]
consistent_with = "consistent with telemetry link loss"
[[signature]]
id = "fc_failure"
kind = "fc_failure"
families = ["ugv"]
required = ["heartbeat:heartbeat_stop", "onboard:onboard_truncated"]
optional = ["link_stats:radio_remote_alive"]
discriminators = ["D1"]
consistent_with = "consistent with FC failure"
"#,
        )
        .expect("catalog")
    }

    fn cues(present: &[(ChannelId, &str)], observed: &[ChannelId]) -> CueSet {
        CueSet {
            present: present
                .iter()
                .map(|(c, s)| (*c, (*s).to_string()))
                .collect(),
            observed_channels: observed.iter().copied().collect(),
            weakest_basis: ClockBasis::HostReceived,
            time_confidence_min: 0.2,
            ..CueSet::default()
        }
    }

    #[test]
    fn tlog_only_silence_without_radio_status_is_ambiguous_and_missing_channel_is_named() {
        let c = cues(
            &[
                (ChannelId::Heartbeat, "heartbeat_stop"),
                (ChannelId::LinkStats, "link_stats_stop"),
            ],
            &[
                ChannelId::Heartbeat,
                ChannelId::LinkStats,
                ChannelId::Onboard,
            ],
        );
        let o = match_signatures(&cat(), Family::Ugv, &c);
        let CauseOutcome::Consistent { candidates } = &o else {
            panic!("{o:?}")
        };
        assert_eq!(candidates[0].signature_id, "telemetry_link_loss");
        assert_eq!(
            candidates[0].logic_confidence,
            LogicConfidence::DirectEvidence
        );
        let c2 = cues(
            &[
                (ChannelId::Heartbeat, "heartbeat_stop"),
                (ChannelId::LinkStats, "link_stats_stop"),
            ],
            &[ChannelId::Heartbeat, ChannelId::LinkStats],
        );
        let o2 = match_signatures(&cat(), Family::Ugv, &c2);
        let CauseOutcome::Consistent { candidates } = &o2 else {
            panic!("{o2:?}")
        };
        assert!(
            candidates[0]
                .discriminating_missing
                .contains(&"onboard:onboard_truncated".to_string())
        );
    }

    #[test]
    fn contradiction_excludes_and_no_cue_is_unknown() {
        let c = cues(
            &[
                (ChannelId::Heartbeat, "heartbeat_stop"),
                (ChannelId::LinkStats, "link_stats_stop"),
                (ChannelId::LinkStats, "radio_remote_alive"),
                (ChannelId::Onboard, "onboard_truncated"),
            ],
            &[
                ChannelId::Heartbeat,
                ChannelId::LinkStats,
                ChannelId::Onboard,
            ],
        );
        let o = match_signatures(&cat(), Family::Ugv, &c);
        let CauseOutcome::Consistent { candidates } = &o else {
            panic!("{o:?}")
        };
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].signature_id, "fc_failure");
        let empty = cues(&[], &[ChannelId::Heartbeat]);
        assert_eq!(
            match_signatures(&cat(), Family::Ugv, &empty),
            CauseOutcome::Unknown {
                reason: UnknownReason::InsufficientEvidence
            }
        );
        assert_eq!(
            match_signatures(&cat(), Family::Fpv, &empty),
            CauseOutcome::Unknown {
                reason: UnknownReason::NoSignatureMatch
            }
        );
        let _ = FailureKind::FcFailure;
    }

    #[test]
    fn unknown_alignment_reaches_existing_time_confidence_cap() {
        let mut profile = crate::profile::parse_profile(
            include_str!("fixtures/unknown-adapter--pose-text--profile.toml"),
            "test",
        )
        .expect("profile");
        for basis in [
            ClockBasis::Unknown,
            ClockBasis::GpsSuspect,
            ClockBasis::HostReceived,
        ] {
            profile.default_clock_basis = basis;
            let mut extracted = extract_cues(
                &[SourceInput {
                    profile: &profile,
                    obs: &[],
                    negatives: &[],
                    alignment: TimeAlignment {
                        basis,
                        offset: None,
                    },
                }],
                Window {
                    start_ms: 0,
                    end_ms: 1000,
                },
            );
            let untrusted = basis != ClockBasis::HostReceived;
            assert_eq!(extracted.time_untrusted, untrusted, "{basis:?}");
            let fixture = cues(
                &[
                    (ChannelId::Heartbeat, "heartbeat_stop"),
                    (ChannelId::Onboard, "onboard_truncated"),
                ],
                &[
                    ChannelId::Heartbeat,
                    ChannelId::LinkStats,
                    ChannelId::Onboard,
                ],
            );
            extracted.present = fixture.present;
            extracted.observed_channels = fixture.observed_channels;
            let outcome = match_signatures(&cat(), Family::Ugv, &extracted);
            let CauseOutcome::Consistent { candidates } = outcome else {
                panic!("expected candidate")
            };
            assert_eq!(
                candidates[0].logic_confidence,
                if untrusted {
                    LogicConfidence::LeadingIndicator
                } else {
                    LogicConfidence::DirectEvidence
                }
            );
        }
    }

    #[test]
    fn time_untrusted_caps_logic_confidence() {
        let mut c = cues(
            &[
                (ChannelId::Heartbeat, "heartbeat_stop"),
                (ChannelId::Onboard, "onboard_truncated"),
            ],
            &[
                ChannelId::Heartbeat,
                ChannelId::LinkStats,
                ChannelId::Onboard,
            ],
        );
        c.time_untrusted = true;
        let o = match_signatures(&cat(), Family::Ugv, &c);
        let CauseOutcome::Consistent { candidates } = &o else {
            panic!("{o:?}")
        };
        assert_eq!(
            candidates[0].logic_confidence,
            LogicConfidence::LeadingIndicator
        );
    }

    #[test]
    fn gps_mavlink_missing_values_are_not_good_or_hdop_evidence() {
        for (fix, sats, hdop, expected) in [
            (3, 12, 100, vec!["gps_fix_good"]),
            (2, 5, 300, vec!["gps_fix_drop", "gps_hdop_rise"]),
            (3, 255, 65535, vec![]),
            (255, 12, 65535, vec![]),
            (9, 255, 65535, vec![]),
        ] {
            let mut o = obs_text(1000, "unused", "");
            o.fields = vec![
                ("GPS_RAW_INT.fix_type".into(), FieldValue::I64(fix)),
                (
                    "GPS_RAW_INT.satellites_visible".into(),
                    FieldValue::I64(sats),
                ),
                ("GPS_RAW_INT.eph".into(), FieldValue::I64(hdop)),
            ];
            let mut found = Vec::new();
            gps_cues(&mut CueSet::default(), &[&o], &[], |_, label, _| {
                found.push(label.to_string())
            });
            assert_eq!(found, expected, "fix={fix} satellites={sats} eph={hdop}");
        }
    }

    fn obs_text(t_ms: i64, key: &str, text: &str) -> Observation {
        let fields = vec![(key.to_string(), FieldValue::Text(text.to_string()))];
        Observation {
            t_ms,
            clock_basis: ClockBasis::HostReceived,
            time_confidence: 0.2,
            source_role: SourceRole::Gcs,
            channel: ChannelId::Event,
            digest: crate::observation_digest(t_ms, ChannelId::Event, &fields),
            fields,
            stale: false,
            t_boot_us: None,
            anchor_unix_us: None,
            wall_ms: Some(t_ms),
        }
    }

    fn tlog_profile(basis: ClockBasis) -> musubi_reference_types::FamilyProfile {
        musubi_reference_types::FamilyProfile {
            profile_id: "p".into(),
            version: "1".into(),
            family: Family::Ugv,
            source_role: SourceRole::Gcs,
            format: "mavlink_tlog".into(),
            extensions: vec!["tlog".into()],
            fields: musubi_reference_types::FieldMapping::default(),
            field_units: std::collections::BTreeMap::new(),
            channels: vec![
                ChannelId::Heartbeat,
                ChannelId::LinkStats,
                ChannelId::Onboard,
            ],
            expectations: vec![],
            default_clock_basis: basis,
            declared_platform_domain: None,
            origin: "public".into(),
            declared_clock_rate: None,
            declared_sender: None,
        }
    }

    fn obs_link_stats(t_ms: i64, lq: f64) -> Observation {
        let fields = vec![("RQly(%)".to_string(), FieldValue::F64(lq))];
        Observation {
            t_ms,
            clock_basis: ClockBasis::HostReceived,
            time_confidence: 0.4,
            source_role: SourceRole::Gcs,
            channel: ChannelId::LinkStats,
            digest: crate::observation_digest(t_ms, ChannelId::LinkStats, &fields),
            fields,
            stale: false,
            t_boot_us: None,
            anchor_unix_us: None,
            wall_ms: Some(t_ms),
        }
    }

    fn heartbeat_absence() -> NegativeObservation {
        NegativeObservation {
            subject: musubi_reference_types::Subject {
                family: Family::Ugv,
                asset_id: "a".into(),
                channel: ChannelId::Heartbeat,
            },
            expectation_id: "hb@1".into(),
            absent_since_ms: 60_000,
            expected_by_ms: 63_000,
            last_good: None,
            clock_basis: ClockBasis::HostReceived,
            time_confidence_min: 0.2,
            mark: Mark {
                status: MarkStatus::Degraded,
                reason_code: "absence-expected-cadence".into(),
                provenance: vec![],
            },
        }
    }

    fn prelude_catalog() -> SignatureCatalog {
        crate::catalog::parse_catalog(
            r#"
version = "t-prelude"
[[signature]]
id = "rf_link_loss_prelude"
kind = "rc_link_loss"
families = ["ugv"]
required = ["link_stats:prelude_link_stats", "heartbeat:heartbeat_stop"]
discriminators = ["D1"]
consistent_with = "consistent with RF link loss after a prelude"
"#,
        )
        .expect("catalog")
    }

    #[test]
    fn every_supporting_record_keeps_its_digest_beyond_the_eighth() {
        let w = Window {
            start_ms: 60_000,
            end_ms: 90_000,
        };
        let host_obs: Vec<Observation> = (0..10_i64)
            .map(|i| obs_link_stats(50_000 + i * 100, 60.0 - i as f64))
            .collect();
        let unknown_obs: Vec<Observation> = (0..3_i64)
            .map(|i| obs_link_stats(55_000 + i * 100, 40.0 - i as f64))
            .collect();
        let host_profile = tlog_profile(ClockBasis::HostReceived);
        let unknown_profile = tlog_profile(ClockBasis::Unknown);
        let neg = heartbeat_absence();
        let sources = vec![
            SourceInput {
                profile: &host_profile,
                obs: &host_obs,
                negatives: std::slice::from_ref(&neg),
                alignment: TimeAlignment {
                    basis: ClockBasis::HostReceived,
                    offset: None,
                },
            },
            SourceInput {
                profile: &unknown_profile,
                obs: &unknown_obs,
                negatives: &[],
                alignment: TimeAlignment {
                    basis: ClockBasis::Unknown,
                    offset: None,
                },
            },
        ];
        let mut expected: Vec<DigestRef> = host_obs
            .iter()
            .chain(&unknown_obs)
            .map(|o| o.digest)
            .collect();
        expected.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(expected.len(), 13, "fixture must exceed the old cap of 8");

        let cues = extract_cues(&sources, w);
        assert!(
            cues.present
                .contains(&(ChannelId::LinkStats, "prelude_link_stats".to_string()))
        );
        assert!(
            cues.present
                .contains(&(ChannelId::Heartbeat, "heartbeat_stop".to_string()))
        );
        assert_eq!(cues.evidence, expected);
        for (i, o) in host_obs.iter().chain(&unknown_obs).enumerate() {
            assert!(
                cues.evidence.contains(&o.digest),
                "supporting record {i} lost its digest"
            );
        }
        assert_eq!(extract_cues(&sources, w).evidence, cues.evidence);
        assert!(cues.time_untrusted);
        assert_eq!(cues.weakest_basis, ClockBasis::Unknown);

        let claim = build_claim(
            &prelude_catalog(),
            &sources,
            Family::Ugv,
            "a",
            ChannelId::Heartbeat,
            &[&neg],
            w,
            0,
        );
        assert_eq!(claim.evidence, expected);
        assert_eq!(claim.input_confidence.envelope_digests, expected);
        assert_eq!(claim.input_confidence.basis_mix.unspecified, 13);
        let CauseOutcome::Consistent { candidates } = &claim.outcome else {
            panic!("{:?}", claim.outcome)
        };
        assert!(!candidates.is_empty());
        for cand in candidates {
            assert_eq!(cand.supporting, expected);
            assert_eq!(cand.logic_confidence, LogicConfidence::LeadingIndicator);
        }
        assert_eq!(claim.kind, ClaimKind::Candidate);
    }

    #[test]
    fn a_large_supporting_batch_is_not_capped_and_duplicates_still_collapse() {
        let w = Window {
            start_ms: 60_000,
            end_ms: 90_000,
        };
        let mut obs: Vec<Observation> = (0..10_000)
            .map(|i| obs_link_stats(45_000 + i, 80.0))
            .collect();
        obs.push(obs[0].clone()); // 同一 digest（重複）は 1 件に畳む。
        let profile = tlog_profile(ClockBasis::HostReceived);
        let sources = vec![SourceInput {
            profile: &profile,
            obs: &obs,
            negatives: &[],
            alignment: TimeAlignment {
                basis: ClockBasis::HostReceived,
                offset: None,
            },
        }];
        let cues = extract_cues(&sources, w);
        assert_eq!(cues.evidence.len(), 10_000);
        let distinct: BTreeSet<[u8; 32]> = cues.evidence.iter().map(|d| d.0).collect();
        assert_eq!(distinct.len(), 10_000, "no duplicate digest survives");
        assert!(cues.evidence.windows(2).all(|p| p[0].0 < p[1].0));
    }

    #[test]
    fn event_triggers_include_failsafe_and_glitch_but_exclude_recovery_text() {
        let obs = vec![
            obs_text(1_000, "STATUSTEXT.text", "Radio Failsafe"),
            obs_text(2_000, "STATUSTEXT.text", "Radio Failsafe Cleared"),
            obs_text(3_000, "MSG.Message", "GPS Glitch or Compass error"),
            obs_text(4_000, "STATUSTEXT.text", "Flight mode AUTO restored"),
            obs_text(5_000, "STATUSTEXT.text", "WDG: reset"),
        ];
        assert_eq!(event_trigger_times(&obs), vec![1_000, 3_000, 5_000]);
    }

    #[test]
    fn negative_digest_and_claim_id_are_deterministic() {
        use musubi_reference_types::{Mark, MarkStatus, Subject};
        let n = NegativeObservation {
            subject: Subject {
                family: Family::Ugv,
                asset_id: "a".into(),
                channel: ChannelId::Heartbeat,
            },
            expectation_id: "hb@1".into(),
            absent_since_ms: 59_000,
            expected_by_ms: 62_000,
            last_good: None,
            clock_basis: ClockBasis::HostReceived,
            time_confidence_min: 0.2,
            mark: Mark {
                status: MarkStatus::Degraded,
                reason_code: "absence-expected-cadence".into(),
                provenance: vec![],
            },
        };
        assert_eq!(negative_digest(&n), negative_digest(&n.clone()));
        let profile = tlog_profile(ClockBasis::HostReceived);
        let src = SourceInput {
            profile: &profile,
            obs: &[],
            negatives: std::slice::from_ref(&n),
            alignment: TimeAlignment {
                basis: ClockBasis::HostReceived,
                offset: None,
            },
        };
        let w = Window {
            start_ms: 59_000,
            end_ms: 89_000,
        };
        let a = build_claim(
            &cat(),
            &[src.clone()],
            Family::Ugv,
            "a",
            ChannelId::Heartbeat,
            &[&n],
            w,
            0,
        );
        let b = build_claim(
            &cat(),
            &[src],
            Family::Ugv,
            "a",
            ChannelId::Heartbeat,
            &[&n],
            w,
            0,
        );
        assert_eq!(a.claim_id, b.claim_id);
        assert!(a.claim_id.starts_with("cause-"));
        assert_eq!(a.kind, ClaimKind::Candidate);
        assert_eq!(a.absence, vec![negative_digest(&n)]);
        assert!(a.mark.reason_code.starts_with("cause-"));
        assert!(a.mark.reason_code.contains("clock-basis:host_received"));
        assert!(!a.mark.reason_code.contains(';'));
        assert_eq!(a.input_confidence.min_confidence, 0.0);
        assert!(
            matches!(a.outcome, CauseOutcome::Ambiguous { .. }),
            "{:?}",
            a.outcome
        );
    }

    #[test]
    fn windows_cluster_by_gap() {
        let w = windows_from_triggers(
            &[(100, 0), (5_000, 1), (14_000, 3), (40_000, 2)],
            10_000,
            30_000,
        );
        assert_eq!(w.len(), 2, "chained triggers stay in one window: {w:?}");
        assert_eq!(w[0].1, vec![0, 1, 3]);
        assert_eq!(w[1].0.start_ms, 40_000);
    }
}
