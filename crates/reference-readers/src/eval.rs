use std::collections::BTreeMap;

use musubi_reference_types::{
    CauseCandidate, CauseClaim, CauseOutcome, ChannelId, FailureKind, Family, LogicConfidence,
    NegativeObservation, UnknownReason,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AttributionWindow {
    #[default]
    InjectEndPlusTMax,
    InjectStartPlusTMax,
}

impl AttributionWindow {
    #[must_use]
    pub const fn end_ms(self, inc: &Incident, t_max_ms: i64) -> i64 {
        match self {
            Self::InjectEndPlusTMax => inc.t_end_ms + t_max_ms,
            Self::InjectStartPlusTMax => inc.t_inject_ms + t_max_ms,
        }
    }

    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::InjectEndPlusTMax => "inject_end_plus_t_max",
            Self::InjectStartPlusTMax => "inject_start_plus_t_max",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CauseStamp {
    #[default]
    WindowEnd,
    WindowStart,
}

impl CauseStamp {
    #[must_use]
    pub const fn stamp_of(self, c: &ClaimRecord) -> i64 {
        match self {
            Self::WindowEnd => c.claim.generated_at_ms,
            Self::WindowStart => c.window_start_ms,
        }
    }

    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::WindowEnd => "window_end",
            Self::WindowStart => "window_start",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EvalConfig {
    pub t_max_ms: i64,
    pub attribution_window: AttributionWindow,
    pub cause_stamp: CauseStamp,
}

impl Default for EvalConfig {
    fn default() -> Self {
        Self {
            t_max_ms: 30_000,
            attribution_window: AttributionWindow::InjectEndPlusTMax,
            cause_stamp: CauseStamp::WindowEnd,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct CauseTiming {
    pub k: usize,
    pub samples_ms: Vec<i64>,
    pub median_ms: Option<i64>,
    pub p90_ms: Option<i64>,
    pub p90_with_censored_at_bound_ms: Option<i64>,
    pub censored: usize,
    pub early_true: usize,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct CorpusCounts {
    pub c1_incidents: usize,
    pub c2_s_layer: usize,
    pub c3_indistinguishable: usize,
    pub c4_missing_channel: usize,
    pub c5_gnss_value_degradation: usize,
    pub c5_video_presence_only: usize,
    pub c6_fpv_incidents: usize,
    pub c7_clean_asset_hours: f64,
    pub c8_candidate_list_len: BTreeMap<usize, usize>,
    pub c10_abstention_lower_bound_claimed: Option<f64>,
    pub c10_abstention_lower_bound_noticed: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Incident {
    pub asset_id: String,
    pub family: Family,
    pub kind: FailureKind,
    pub t_inject_ms: i64,
    pub t_end_ms: i64,
    pub t_detectable_ms: i64,
    pub affected_channels: Vec<ChannelId>,
    pub indistinguishable: bool,
    pub channel_complete: bool,
}

#[derive(Debug, Clone)]
pub struct NoticedAbsence {
    pub asset_id: String,
    pub channel: ChannelId,
    pub absent_since_wall_ms: i64,
    pub expected_by_wall_ms: i64,
}

impl NoticedAbsence {
    #[must_use]
    pub fn from_negative(n: &NegativeObservation, since_wall_ms: i64, by_wall_ms: i64) -> Self {
        Self {
            asset_id: n.subject.asset_id.clone(),
            channel: n.subject.channel,
            absent_since_wall_ms: since_wall_ms,
            expected_by_wall_ms: by_wall_ms,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ClaimRecord {
    pub asset_id: String,
    pub window_start_ms: i64,
    pub window_end_ms: i64,
    pub claim: CauseClaim,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Metrics {
    pub incidents: usize,
    pub noticed: usize,
    pub missed: usize,
    pub time_to_notice_ms: Vec<i64>,
    pub time_to_notice_median_ms: Option<i64>,
    pub time_to_notice_p90_ms: Option<i64>,
    pub early_true: usize,
    pub false_alarms: usize,
    pub asset_hours: f64,
    pub false_alarm_rate_per_asset_hour: f64,
    pub claims: usize,
    pub abstained: usize,
    pub abstention_rate: Option<f64>,
    pub unknown_by_reason: BTreeMap<String, usize>,
    pub precision_at_1: Option<f64>,
    pub precision_at_1_hits: usize,
    pub precision_at_1_total: usize,
    pub false_certainty: usize,
    pub ambiguous_class_total: usize,
    pub ambiguous_class_honest: usize,
    pub missing_channel_incidents: usize,
    pub cause_at_1: CauseTiming,
    pub cause_at_3: CauseTiming,
    pub precision_at_3: Option<f64>,
    pub precision_at_3_hits: usize,
    pub missed_rate: Option<f64>,
    pub ambiguity_honesty_rate: Option<f64>,
    pub abstained_excl_channel_not_observed: usize,
    pub abstention_rate_excl_channel_not_observed: Option<f64>,
    pub abstained_s: usize,
    pub claimed_s: usize,
    pub abstention_rate_s: Option<f64>,
    pub clean_asset_hours: f64,
    pub false_alarm_rate_per_clean_asset_hour: f64,
    pub corpus: CorpusCounts,
    pub config: EvalConfig,
}

fn kind_of(
    signature_id: &str,
    catalog_kinds: &BTreeMap<String, FailureKind>,
) -> Option<FailureKind> {
    catalog_kinds.get(signature_id).copied()
}

#[must_use]
pub fn top_k_candidates(outcome: &CauseOutcome, k: usize) -> Vec<(String, LogicConfidence, bool)> {
    let (cands, ambiguous) = match outcome {
        CauseOutcome::Consistent { candidates } => (candidates, false),
        CauseOutcome::Ambiguous { candidates, .. } => (candidates, true),
        CauseOutcome::Unknown { .. } => return Vec::new(),
    };
    let ratio = |c: &CauseCandidate| {
        c.signature_match.matched_cues.len() as f64
            / c.signature_match.required_cues.len().max(1) as f64
    };
    let mut ranked: Vec<&CauseCandidate> = cands.iter().collect();
    ranked.sort_by(|a, b| {
        (b.logic_confidence, ratio(b))
            .partial_cmp(&(a.logic_confidence, ratio(a)))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    ranked
        .into_iter()
        .take(k)
        .map(|c| (c.signature_id.clone(), c.logic_confidence, ambiguous))
        .collect()
}

#[must_use]
pub fn top_candidate(outcome: &CauseOutcome) -> Option<(String, LogicConfidence, bool)> {
    top_k_candidates(outcome, 1).into_iter().next()
}

fn candidate_len(outcome: &CauseOutcome) -> usize {
    match outcome {
        CauseOutcome::Consistent { candidates } | CauseOutcome::Ambiguous { candidates, .. } => {
            candidates.len()
        }
        CauseOutcome::Unknown { .. } => 0,
    }
}

fn median_of(sorted: &[i64]) -> Option<i64> {
    (!sorted.is_empty()).then(|| sorted[sorted.len() / 2])
}

fn quantile(sorted: &[i64], q: f64) -> Option<i64> {
    if sorted.is_empty() {
        return None;
    }
    let i = ((sorted.len() as f64 * q).ceil() as usize)
        .saturating_sub(1)
        .min(sorted.len() - 1);
    Some(sorted[i])
}

fn cause_timing(
    k: usize,
    incidents: &[Incident],
    claims: &[ClaimRecord],
    catalog_kinds: &BTreeMap<String, FailureKind>,
    cfg: &EvalConfig,
) -> CauseTiming {
    let mut samples: Vec<i64> = Vec::new();
    let mut censored_bounds: Vec<i64> = Vec::new();
    let mut early_true = 0usize;
    for inc in incidents {
        let w_end = cfg.attribution_window.end_ms(inc, cfg.t_max_ms);
        let mut first: Option<i64> = None;
        for c in claims.iter().filter(|c| claim_is_relevant(c, inc, w_end)) {
            let stamp = cfg.cause_stamp.stamp_of(c);
            if stamp < inc.t_inject_ms || stamp > w_end {
                continue;
            }
            let named = top_k_candidates(&c.claim.outcome, k)
                .iter()
                .any(|(sig, _, _)| kind_of(sig, catalog_kinds) == Some(inc.kind));
            if named {
                first = Some(first.map_or(stamp, |f: i64| f.min(stamp)));
            }
        }
        match first {
            Some(t) => {
                let d = t - inc.t_detectable_ms;
                if d < 0 {
                    early_true += 1;
                }
                samples.push(d);
            }
            None => censored_bounds.push(w_end - inc.t_detectable_ms),
        }
    }
    let mut sorted = samples.clone();
    sorted.sort_unstable();
    let mut with_censored = sorted.clone();
    with_censored.extend(censored_bounds.iter().copied());
    with_censored.sort_unstable();
    CauseTiming {
        k,
        median_ms: median_of(&sorted),
        p90_ms: quantile(&sorted, 0.9),
        p90_with_censored_at_bound_ms: quantile(&with_censored, 0.9),
        censored: censored_bounds.len(),
        early_true,
        samples_ms: samples,
    }
}

fn injected_ms(incidents: &[Incident], duration_ms: i64, cfg: &EvalConfig) -> i64 {
    let mut by_asset: BTreeMap<&str, Vec<(i64, i64)>> = BTreeMap::new();
    for inc in incidents {
        by_asset.entry(inc.asset_id.as_str()).or_default().push((
            inc.t_inject_ms,
            cfg.attribution_window.end_ms(inc, cfg.t_max_ms),
        ));
    }
    let mut total = 0i64;
    for mut spans in by_asset.into_values() {
        spans.sort_unstable();
        let mut per_asset = 0i64;
        let mut cur: Option<(i64, i64)> = None;
        for (a, b) in spans {
            cur = match cur {
                Some((s, e)) if a <= e => Some((s, e.max(b))),
                Some((s, e)) => {
                    per_asset += e - s;
                    Some((a, b))
                }
                None => Some((a, b)),
            };
        }
        if let Some((s, e)) = cur {
            per_asset += e - s;
        }
        total += per_asset.min(duration_ms);
    }
    total
}

fn claim_is_relevant(c: &ClaimRecord, inc: &Incident, w_end: i64) -> bool {
    c.asset_id == inc.asset_id && c.window_start_ms < w_end && c.window_end_ms > inc.t_inject_ms
}

#[must_use]
pub fn evaluate(
    incidents: &[Incident],
    noticed: &[NoticedAbsence],
    claims: &[ClaimRecord],
    catalog_kinds: &BTreeMap<String, FailureKind>,
    asset_count: usize,
    duration_ms: i64,
    t_max_ms: i64,
) -> Metrics {
    evaluate_with(
        incidents,
        noticed,
        claims,
        catalog_kinds,
        asset_count,
        duration_ms,
        &EvalConfig {
            t_max_ms,
            ..EvalConfig::default()
        },
    )
}

#[must_use]
pub fn evaluate_with(
    incidents: &[Incident],
    noticed: &[NoticedAbsence],
    claims: &[ClaimRecord],
    catalog_kinds: &BTreeMap<String, FailureKind>,
    asset_count: usize,
    duration_ms: i64,
    cfg: &EvalConfig,
) -> Metrics {
    let mut ttn: Vec<i64> = Vec::new();
    let mut missed = 0usize;
    let mut early_true = 0usize;
    let mut matched_neg: Vec<bool> = vec![false; noticed.len()];
    for inc in incidents {
        let w_end = cfg.attribution_window.end_ms(inc, cfg.t_max_ms);
        let mut first: Option<i64> = None;
        for (i, n) in noticed.iter().enumerate() {
            if n.asset_id != inc.asset_id || !inc.affected_channels.contains(&n.channel) {
                continue;
            }
            if n.expected_by_wall_ms >= inc.t_inject_ms && n.absent_since_wall_ms <= w_end {
                matched_neg[i] = true;
                first = Some(
                    first.map_or(n.expected_by_wall_ms, |f: i64| f.min(n.expected_by_wall_ms)),
                );
            }
        }
        if first.is_none() {
            first = claims
                .iter()
                .filter(|c| {
                    c.asset_id == inc.asset_id
                        && c.window_start_ms >= inc.t_inject_ms
                        && c.window_start_ms <= w_end
                })
                .map(|c| c.window_start_ms)
                .min();
        }
        match first {
            Some(f) if f <= w_end => {
                let d = f - inc.t_detectable_ms;
                if d < 0 && f >= inc.t_inject_ms {
                    early_true += 1;
                }
                ttn.push(d);
            }
            _ => missed += 1,
        }
    }
    let false_alarms = matched_neg.iter().filter(|m| !**m).count();
    let asset_hours = asset_count as f64 * duration_ms as f64 / 3_600_000.0;
    let clean_ms =
        (asset_count as i64 * duration_ms - injected_ms(incidents, duration_ms, cfg)).max(0);
    let clean_asset_hours = clean_ms as f64 / 3_600_000.0;
    let mut sorted = ttn.clone();
    sorted.sort_unstable();
    let median = median_of(&sorted);
    let p90 = quantile(&sorted, 0.9);
    let mut abstained = 0usize;
    let mut abstained_excl_cno = 0usize;
    let mut abstained_s = 0usize;
    let mut claimed_s = 0usize;
    let mut unknown_by_reason: BTreeMap<String, usize> = BTreeMap::new();
    let mut hits = 0usize;
    let mut hits3 = 0usize;
    let mut total = 0usize;
    let mut candidate_list_len: BTreeMap<usize, usize> = BTreeMap::new();
    let mut false_certainty = 0usize;
    let mut amb_total = 0usize;
    let mut amb_honest = 0usize;
    let mut missing_channel_incidents = 0usize;
    for inc in incidents {
        let w_end = cfg.attribution_window.end_ms(inc, cfg.t_max_ms);
        let rel: Vec<&ClaimRecord> = claims
            .iter()
            .filter(|c| claim_is_relevant(c, inc, w_end))
            .collect();
        if rel.is_empty() {
            continue;
        }
        let tally_unknown = |rel: &[&ClaimRecord],
                             unknown_by_reason: &mut BTreeMap<String, usize>,
                             missing_channel_incidents: &mut usize| {
            for c in rel {
                if let CauseOutcome::Unknown { reason } = &c.claim.outcome {
                    *unknown_by_reason
                        .entry(reason.as_label().to_string())
                        .or_default() += 1;
                    if *reason == UnknownReason::ChannelNotObserved {
                        *missing_channel_incidents += 1;
                    }
                }
            }
        };
        let only_channel_not_observed = rel.iter().all(|c| {
            matches!(
                &c.claim.outcome,
                CauseOutcome::Unknown {
                    reason: UnknownReason::ChannelNotObserved
                }
            )
        });
        let top = rel
            .iter()
            .filter_map(|c| top_candidate(&c.claim.outcome).map(|t| (c, t)))
            .next();
        let all_unknown = rel
            .iter()
            .all(|c| matches!(c.claim.outcome, CauseOutcome::Unknown { .. }));
        if inc.indistinguishable {
            amb_total += 1;
            let honest = rel.iter().all(|c| match &c.claim.outcome {
                CauseOutcome::Unknown { .. } | CauseOutcome::Ambiguous { .. } => true,
                CauseOutcome::Consistent { candidates } => candidates
                    .iter()
                    .all(|k| k.logic_confidence <= LogicConfidence::LeadingIndicator),
            });
            if honest {
                amb_honest += 1;
            }
            if all_unknown {
                abstained += 1;
                if !only_channel_not_observed {
                    abstained_excl_cno += 1;
                }
            }
            tally_unknown(&rel, &mut unknown_by_reason, &mut missing_channel_incidents);
            continue;
        }
        let in_s = inc.channel_complete;
        if in_s {
            claimed_s += 1;
        }
        match top {
            None => {
                abstained += 1;
                if !only_channel_not_observed {
                    abstained_excl_cno += 1;
                }
                if in_s {
                    abstained_s += 1;
                }
                tally_unknown(&rel, &mut unknown_by_reason, &mut missing_channel_incidents);
            }
            Some((c, (sig, logic, ambiguous))) => {
                total += 1;
                *candidate_list_len
                    .entry(candidate_len(&c.claim.outcome).min(4))
                    .or_default() += 1;
                let k = kind_of(&sig, catalog_kinds);
                if k == Some(inc.kind) {
                    hits += 1;
                } else if !ambiguous && logic >= LogicConfidence::DirectEvidence {
                    false_certainty += 1;
                }
                if top_k_candidates(&c.claim.outcome, 3)
                    .iter()
                    .any(|(s, _, _)| kind_of(s, catalog_kinds) == Some(inc.kind))
                {
                    hits3 += 1;
                }
            }
        }
    }
    let claims_n = claims.len();
    let noticed_n = incidents.len() - missed;
    let c3 = incidents.iter().filter(|i| i.indistinguishable).count();
    let corpus = CorpusCounts {
        c1_incidents: incidents.len(),
        c2_s_layer: incidents
            .iter()
            .filter(|i| !i.indistinguishable && i.channel_complete)
            .count(),
        c3_indistinguishable: c3,
        c4_missing_channel: incidents.iter().filter(|i| !i.channel_complete).count(),
        c5_gnss_value_degradation: incidents
            .iter()
            .filter(|i| {
                matches!(
                    i.kind,
                    FailureKind::GnssDegradation | FailureKind::GnssInconsistency
                )
            })
            .count(),
        c5_video_presence_only: incidents
            .iter()
            .filter(|i| {
                !i.affected_channels.is_empty()
                    && i.affected_channels
                        .iter()
                        .all(|c| matches!(c, ChannelId::Video | ChannelId::Osd))
            })
            .count(),
        c6_fpv_incidents: incidents.iter().filter(|i| i.family == Family::Fpv).count(),
        c7_clean_asset_hours: clean_asset_hours,
        c8_candidate_list_len: candidate_list_len,
        c10_abstention_lower_bound_claimed: (total + abstained > 0)
            .then(|| c3 as f64 / (total + abstained) as f64),
        c10_abstention_lower_bound_noticed: (noticed_n > 0).then(|| c3 as f64 / noticed_n as f64),
    };
    Metrics {
        incidents: incidents.len(),
        noticed: noticed_n,
        missed,
        time_to_notice_ms: ttn,
        time_to_notice_median_ms: median,
        time_to_notice_p90_ms: p90,
        early_true,
        false_alarms,
        asset_hours,
        false_alarm_rate_per_asset_hour: if asset_hours > 0.0 {
            false_alarms as f64 / asset_hours
        } else {
            0.0
        },
        claims: claims_n,
        abstained,
        abstention_rate: (total + abstained > 0)
            .then(|| abstained as f64 / (total + abstained) as f64),
        unknown_by_reason,
        precision_at_1: (total > 0).then(|| hits as f64 / total as f64),
        precision_at_1_hits: hits,
        precision_at_1_total: total,
        false_certainty,
        ambiguous_class_total: amb_total,
        ambiguous_class_honest: amb_honest,
        missing_channel_incidents,
        cause_at_1: cause_timing(1, incidents, claims, catalog_kinds, cfg),
        cause_at_3: cause_timing(3, incidents, claims, catalog_kinds, cfg),
        precision_at_3: (total > 0).then(|| hits3 as f64 / total as f64),
        precision_at_3_hits: hits3,
        missed_rate: (!incidents.is_empty()).then(|| missed as f64 / incidents.len() as f64),
        ambiguity_honesty_rate: (amb_total > 0).then(|| amb_honest as f64 / amb_total as f64),
        abstained_excl_channel_not_observed: abstained_excl_cno,
        abstention_rate_excl_channel_not_observed: (total + abstained_excl_cno > 0)
            .then(|| abstained_excl_cno as f64 / (total + abstained_excl_cno) as f64),
        abstained_s,
        claimed_s,
        abstention_rate_s: (claimed_s > 0).then(|| abstained_s as f64 / claimed_s as f64),
        clean_asset_hours,
        false_alarm_rate_per_clean_asset_hour: if clean_asset_hours > 0.0 {
            false_alarms as f64 / clean_asset_hours
        } else {
            0.0
        },
        corpus,
        config: *cfg,
    }
}

impl Metrics {
    #[must_use]
    pub fn detail_block(&self) -> String {
        let f = |v: Option<f64>| v.map_or_else(|| "none".to_string(), |x| format!("{x:.4}"));
        let i = |v: Option<i64>| v.map_or_else(|| "none".to_string(), |x| x.to_string());
        let mut s = String::new();
        s.push_str(&format!(
            "config_t_max_ms={}\nconfig_attribution_window={}\nconfig_cause_stamp={}\n",
            self.config.t_max_ms,
            self.config.attribution_window.as_label(),
            self.config.cause_stamp.as_label()
        ));
        for t in [&self.cause_at_1, &self.cause_at_3] {
            let k = t.k;
            s.push_str(&format!(
                "time_to_plausible_cause_at_{k}_scored={}\ntime_to_plausible_cause_at_{k}_median_ms={}\ntime_to_plausible_cause_at_{k}_p90_ms={}\ntime_to_plausible_cause_at_{k}_p90_with_censored_at_bound_ms={}\ntime_to_plausible_cause_at_{k}_censored={}\ntime_to_plausible_cause_at_{k}_early_true={}\n",
                t.samples_ms.len(),
                i(t.median_ms),
                i(t.p90_ms),
                i(t.p90_with_censored_at_bound_ms),
                t.censored,
                t.early_true
            ));
        }
        s.push_str(&format!(
            "precision_at_3={} ({}/{})\nmissed_rate={}\nambiguity_honesty_rate={}\n",
            f(self.precision_at_3),
            self.precision_at_3_hits,
            self.precision_at_1_total,
            f(self.missed_rate),
            f(self.ambiguity_honesty_rate)
        ));
        s.push_str(&format!(
            "abstention_rate_s={} ({}/{})\nabstention_rate_excl_channel_not_observed={} ({}/{})\n",
            f(self.abstention_rate_s),
            self.abstained_s,
            self.claimed_s,
            f(self.abstention_rate_excl_channel_not_observed),
            self.abstained_excl_channel_not_observed,
            self.precision_at_1_total + self.abstained_excl_channel_not_observed
        ));
        s.push_str(&format!(
            "clean_asset_hours={:.4}\nfalse_alarm_rate_per_clean_asset_hour={:.2}\n",
            self.clean_asset_hours, self.false_alarm_rate_per_clean_asset_hour
        ));
        let c = &self.corpus;
        s.push_str(&format!(
            "corpus_c1_incidents={}\ncorpus_c2_s_layer={}\ncorpus_c3_indistinguishable={}\ncorpus_c4_missing_channel={}\ncorpus_c5_gnss_value_degradation={}\ncorpus_c5_video_presence_only={}\ncorpus_c6_fpv_incidents={}\ncorpus_c7_clean_asset_hours={:.4}\ncorpus_c8_candidate_list_len={:?}\ncorpus_c10_abstention_lower_bound_claimed={}\ncorpus_c10_abstention_lower_bound_noticed={}\n",
            c.c1_incidents,
            c.c2_s_layer,
            c.c3_indistinguishable,
            c.c4_missing_channel,
            c.c5_gnss_value_degradation,
            c.c5_video_presence_only,
            c.c6_fpv_incidents,
            c.c7_clean_asset_hours,
            c.c8_candidate_list_len,
            f(c.c10_abstention_lower_bound_claimed),
            f(c.c10_abstention_lower_bound_noticed)
        ));
        s
    }

    #[must_use]
    pub fn summary(&self) -> String {
        format!(
            "incidents={} noticed={} missed={} time_to_notice_ms(median/p90)={:?}/{:?} early_true={} false_alarms={} ({:.2}/asset-hour) claims={} abstained={} abstention_rate={:?} precision@1={:?} ({}/{}) false_certainty={} ambiguity_honesty={}/{} missing_channel_incidents={} unknown_by_reason={:?}",
            self.incidents,
            self.noticed,
            self.missed,
            self.time_to_notice_median_ms,
            self.time_to_notice_p90_ms,
            self.early_true,
            self.false_alarms,
            self.false_alarm_rate_per_asset_hour,
            self.claims,
            self.abstained,
            self.abstention_rate,
            self.precision_at_1,
            self.precision_at_1_hits,
            self.precision_at_1_total,
            self.false_certainty,
            self.ambiguous_class_honest,
            self.ambiguous_class_total,
            self.missing_channel_incidents,
            self.unknown_by_reason
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_reference_types::{
        BasisMix, CauseCandidate, ChannelCoverage, ClaimKind, Family, GeneratorRef,
        InputConfidenceRef, Mark, MarkStatus, SignatureMatch, Subject, UnknownReason,
    };

    fn inc(asset: &str, kind: FailureKind, t: i64, indistinguishable: bool) -> Incident {
        Incident {
            asset_id: asset.into(),
            family: Family::Ugv,
            kind,
            t_inject_ms: t,
            t_end_ms: t + 30_000,
            t_detectable_ms: t + 3_000,
            affected_channels: vec![ChannelId::Heartbeat],
            indistinguishable,
            channel_complete: !indistinguishable,
        }
    }

    fn noticed(asset: &str, since: i64, by: i64) -> NoticedAbsence {
        NoticedAbsence {
            asset_id: asset.into(),
            channel: ChannelId::Heartbeat,
            absent_since_wall_ms: since,
            expected_by_wall_ms: by,
        }
    }

    fn claim(asset: &str, start: i64, outcome: CauseOutcome) -> ClaimRecord {
        ClaimRecord {
            asset_id: asset.into(),
            window_start_ms: start,
            window_end_ms: start + 30_000,
            claim: CauseClaim {
                claim_id: "c".into(),
                kind: ClaimKind::Candidate,
                subject: Subject {
                    family: Family::Ugv,
                    asset_id: asset.into(),
                    channel: ChannelId::Heartbeat,
                },
                absence: vec![],
                outcome,
                input_confidence: InputConfidenceRef {
                    envelope_digests: vec![],
                    min_confidence: 0.0,
                    basis_mix: BasisMix::default(),
                    time_confidence_min: 0.2,
                    channel_coverage: ChannelCoverage::default(),
                },
                evidence: vec![],
                generator: GeneratorRef {
                    catalog_version: "t".into(),
                    profile_version: "t".into(),
                    build_id: "t".into(),
                },
                generated_at_ms: start + 30_000,
                mark: Mark {
                    status: MarkStatus::Degraded,
                    reason_code: "cause-candidate".into(),
                    provenance: vec![],
                },
            },
        }
    }

    fn cand(id: &str, logic: LogicConfidence) -> CauseCandidate {
        CauseCandidate {
            signature_id: id.into(),
            signature_match: SignatureMatch {
                matched_cues: vec!["a".into(), "b".into()],
                required_cues: vec!["a".into(), "b".into()],
                optional_cues_matched: vec![],
                contradicting_cues: vec![],
            },
            logic_confidence: logic,
            supporting: vec![],
            contradicting: vec![],
            discriminating_missing: vec![],
        }
    }

    fn kinds() -> BTreeMap<String, FailureKind> {
        [
            ("fc_failure".to_string(), FailureKind::FcFailure),
            (
                "telemetry_link_loss".to_string(),
                FailureKind::TelemetryLinkLoss,
            ),
        ]
        .into_iter()
        .collect()
    }

    #[test]
    fn wrong_kind_with_direct_evidence_counts_as_false_certainty_but_leading_indicator_does_not() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, false)];
        let n = vec![noticed("a", 59_000, 62_000)];
        let wrong = CauseOutcome::Consistent {
            candidates: vec![cand("telemetry_link_loss", LogicConfidence::DirectEvidence)],
        };
        let m = evaluate(
            &incs,
            &n,
            &[claim("a", 59_000, wrong)],
            &kinds(),
            1,
            180_000,
            30_000,
        );
        assert_eq!(m.false_certainty, 1);
        assert_eq!(m.precision_at_1, Some(0.0));
        assert_eq!(m.time_to_notice_ms, vec![-1_000]);
        assert_eq!(
            m.early_true, 1,
            "expected_by before t_detectable but after t_inject"
        );
        let weak = CauseOutcome::Consistent {
            candidates: vec![cand(
                "telemetry_link_loss",
                LogicConfidence::LeadingIndicator,
            )],
        };
        let m = evaluate(
            &incs,
            &n,
            &[claim("a", 59_000, weak)],
            &kinds(),
            1,
            180_000,
            30_000,
        );
        assert_eq!(m.false_certainty, 0);
        assert_eq!(m.precision_at_1_hits, 0);
    }

    #[test]
    fn missed_false_alarm_and_abstention_are_counted() {
        let incs = vec![
            inc("a", FailureKind::FcFailure, 60_000, false),
            inc("b", FailureKind::FcFailure, 60_000, false),
        ];
        let n = vec![noticed("b", 59_000, 62_000), noticed("c", 10_000, 13_000)]; // c: no incident → false alarm
        let unk = CauseOutcome::Unknown {
            reason: UnknownReason::InsufficientEvidence,
        };
        let m = evaluate(
            &incs,
            &n,
            &[claim("b", 59_000, unk)],
            &kinds(),
            3,
            180_000,
            30_000,
        );
        assert_eq!(m.missed, 1);
        assert_eq!(m.noticed, 1);
        assert_eq!(m.false_alarms, 1);
        assert!((m.false_alarm_rate_per_asset_hour - 1.0 / (3.0 * 0.05)).abs() < 1e-9);
        assert_eq!(m.abstained, 1);
        assert_eq!(m.abstention_rate, Some(1.0));
        assert_eq!(m.unknown_by_reason.get("insufficient-evidence"), Some(&1));
    }

    #[test]
    fn indistinguishable_incident_is_honest_only_when_not_asserted_with_direct_evidence() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, true)];
        let n = vec![noticed("a", 59_000, 62_000)];
        let amb = CauseOutcome::Ambiguous {
            candidates: vec![
                cand("fc_failure", LogicConfidence::DirectEvidence),
                cand("telemetry_link_loss", LogicConfidence::DirectEvidence),
            ],
            indistinguishable_by: vec!["onboard".into()],
        };
        let m = evaluate(
            &incs,
            &n,
            &[claim("a", 59_000, amb)],
            &kinds(),
            1,
            180_000,
            30_000,
        );
        assert_eq!((m.ambiguous_class_total, m.ambiguous_class_honest), (1, 1));
        assert_eq!(
            m.precision_at_1_total, 0,
            "ambiguous class excluded from precision"
        );
        let sure = CauseOutcome::Consistent {
            candidates: vec![cand("fc_failure", LogicConfidence::DirectEvidence)],
        };
        let m = evaluate(
            &incs,
            &n,
            &[claim("a", 59_000, sure)],
            &kinds(),
            1,
            180_000,
            30_000,
        );
        assert_eq!((m.ambiguous_class_total, m.ambiguous_class_honest), (1, 0));
    }

    #[test]
    fn median_and_p90_follow_sorted_order_and_top_candidate_derives_by_logic_then_ratio() {
        let incs: Vec<Incident> = (0..10)
            .map(|i| inc(&format!("a{i}"), FailureKind::FcFailure, 60_000, false))
            .collect();
        let n: Vec<NoticedAbsence> = (0..10)
            .map(|i| noticed(&format!("a{i}"), 59_000, 63_000 + i * 1_000))
            .collect();
        let m = evaluate(&incs, &n, &[], &kinds(), 10, 180_000, 30_000);
        assert_eq!(m.time_to_notice_median_ms, Some(5_000));
        assert_eq!(m.time_to_notice_p90_ms, Some(8_000));
        let o = CauseOutcome::Consistent {
            candidates: vec![
                cand("x", LogicConfidence::LeadingIndicator),
                cand("y", LogicConfidence::DirectEvidence),
            ],
        };
        assert_eq!(top_candidate(&o).map(|t| t.0), Some("y".to_string()));
        assert!(
            top_candidate(&CauseOutcome::Unknown {
                reason: UnknownReason::NoSignatureMatch
            })
            .is_none()
        );
    }

    fn inc_ch(
        asset: &str,
        family: Family,
        kind: FailureKind,
        t: i64,
        channels: Vec<ChannelId>,
    ) -> Incident {
        Incident {
            asset_id: asset.into(),
            family,
            kind,
            t_inject_ms: t,
            t_end_ms: t + 30_000,
            t_detectable_ms: t + 3_000,
            affected_channels: channels,
            indistinguishable: false,
            channel_complete: true,
        }
    }

    fn consistent(ids: &[(&str, LogicConfidence)]) -> CauseOutcome {
        CauseOutcome::Consistent {
            candidates: ids.iter().map(|(i, l)| cand(i, *l)).collect(),
        }
    }

    #[test]
    fn time_to_plausible_cause_shares_the_origin_with_time_to_notice_and_stamps_the_window_end() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, false)];
        let n = vec![noticed("a", 59_000, 64_000)];
        let right = consistent(&[("fc_failure", LogicConfidence::DirectEvidence)]);
        let claims = vec![claim("a", 64_000, right.clone())]; // 窓 [64_000, 94_000] → stamp 94_000
        let m = evaluate_with(
            &incs,
            &n,
            &claims,
            &kinds(),
            1,
            180_000,
            &EvalConfig::default(),
        );
        assert_eq!(m.cause_at_1.samples_ms, vec![31_000]);
        assert_eq!(m.cause_at_1.median_ms, Some(31_000));
        assert_eq!(m.cause_at_1.p90_ms, Some(31_000));
        assert_eq!(m.cause_at_1.censored, 0);
        assert_eq!(m.cause_at_1.k, 1);
        let cfg = EvalConfig {
            cause_stamp: CauseStamp::WindowStart,
            ..EvalConfig::default()
        };
        let m2 = evaluate_with(&incs, &n, &claims, &kinds(), 1, 180_000, &cfg);
        assert_eq!(m2.cause_at_1.samples_ms, vec![1_000]);
    }

    #[test]
    fn top_k_moves_the_stop_time_so_at_3_can_be_earlier_than_at_1() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, false)];
        let n = vec![noticed("a", 59_000, 64_000)];
        let claims = vec![
            claim(
                "a",
                64_000,
                consistent(&[
                    ("telemetry_link_loss", LogicConfidence::DirectEvidence),
                    ("fc_failure", LogicConfidence::LeadingIndicator),
                ]),
            ),
            claim(
                "a",
                85_000,
                consistent(&[("fc_failure", LogicConfidence::DirectEvidence)]),
            ),
        ];
        let m = evaluate_with(
            &incs,
            &n,
            &claims,
            &kinds(),
            1,
            180_000,
            &EvalConfig::default(),
        );
        assert_eq!(m.cause_at_3.samples_ms, vec![31_000], "@3 は早い窓で止まる");
        assert_eq!(m.cause_at_1.samples_ms, vec![52_000], "@1 は遅い窓まで待つ");
        assert_eq!(m.precision_at_1, Some(0.0));
        assert_eq!(m.precision_at_3, Some(1.0));
        assert_eq!((m.precision_at_3_hits, m.precision_at_1_total), (1, 1));
        assert_eq!(m.corpus.c8_candidate_list_len.get(&2), Some(&1));
    }

    #[test]
    fn censored_cause_is_counted_and_only_the_bounded_p90_imputes_it() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, false)];
        let n = vec![noticed("a", 59_000, 64_000)];
        let wrong = consistent(&[("telemetry_link_loss", LogicConfidence::LeadingIndicator)]);
        let m = evaluate_with(
            &incs,
            &n,
            &[claim("a", 64_000, wrong)],
            &kinds(),
            1,
            180_000,
            &EvalConfig::default(),
        );
        assert_eq!(m.cause_at_1.censored, 1);
        assert!(m.cause_at_1.samples_ms.is_empty());
        assert_eq!(
            m.cause_at_1.p90_ms, None,
            "件数併記のみ＝除外して算出しない"
        );
        assert_eq!(m.cause_at_1.p90_with_censored_at_bound_ms, Some(57_000));
    }

    #[test]
    fn attribution_window_shape_switches_between_the_shipped_and_the_wording_reading() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, false)];
        let n = vec![noticed("a", 59_000, 64_000)];
        let claims = vec![claim(
            "a",
            85_000,
            consistent(&[("fc_failure", LogicConfidence::DirectEvidence)]),
        )]; // stamp 115_000
        let shipped = evaluate_with(
            &incs,
            &n,
            &claims,
            &kinds(),
            1,
            180_000,
            &EvalConfig::default(),
        );
        assert_eq!(shipped.cause_at_1.samples_ms, vec![52_000]);
        let wording = EvalConfig {
            attribution_window: AttributionWindow::InjectStartPlusTMax,
            ..EvalConfig::default()
        };
        let m = evaluate_with(&incs, &n, &claims, &kinds(), 1, 180_000, &wording);
        assert_eq!(
            m.cause_at_1.censored, 1,
            "窓 [60_000, 90_000] の外なので censored"
        );
    }

    #[test]
    fn abstention_is_reported_for_the_s_layer_and_for_both_readings_of_the_denominator() {
        let incs = vec![
            inc("s1", FailureKind::FcFailure, 60_000, false),
            inc("s2", FailureKind::FcFailure, 60_000, false),
            inc("x1", FailureKind::FcFailure, 60_000, true), // 区別不能クラス＝channel-incomplete
        ];
        let n = vec![
            noticed("s1", 59_000, 64_000),
            noticed("s2", 59_000, 64_000),
            noticed("x1", 59_000, 64_000),
        ];
        let claims = vec![
            claim(
                "s1",
                64_000,
                CauseOutcome::Unknown {
                    reason: UnknownReason::InsufficientEvidence,
                },
            ),
            claim(
                "s2",
                64_000,
                consistent(&[("fc_failure", LogicConfidence::DirectEvidence)]),
            ),
            claim(
                "x1",
                64_000,
                CauseOutcome::Unknown {
                    reason: UnknownReason::ChannelNotObserved,
                },
            ),
        ];
        let m = evaluate_with(
            &incs,
            &n,
            &claims,
            &kinds(),
            3,
            180_000,
            &EvalConfig::default(),
        );
        assert_eq!((m.abstained_s, m.claimed_s), (1, 2));
        assert_eq!(m.abstention_rate_s, Some(0.5));
        assert_eq!(m.abstained, 2);
        assert_eq!(m.abstention_rate, Some(2.0 / 3.0));
        assert_eq!(m.abstained_excl_channel_not_observed, 1);
        assert_eq!(m.abstention_rate_excl_channel_not_observed, Some(0.5));
        assert_eq!(m.missing_channel_incidents, 1);
    }

    #[test]
    fn clean_asset_hours_exclude_the_attribution_windows_so_the_false_alarm_denominator_matches() {
        let incs = vec![inc("a", FailureKind::FcFailure, 60_000, false)];
        let n = vec![noticed("a", 59_000, 64_000), noticed("a", 10_000, 13_000)];
        let m = evaluate_with(&incs, &n, &[], &kinds(), 1, 180_000, &EvalConfig::default());
        assert_eq!(m.false_alarms, 1);
        assert!((m.asset_hours - 0.05).abs() < 1e-9);
        assert!((m.clean_asset_hours - 120_000.0 / 3_600_000.0).abs() < 1e-9);
        assert!((m.false_alarm_rate_per_clean_asset_hour - 30.0).abs() < 1e-9);
        assert_eq!(m.missed_rate, Some(0.0));
    }

    #[test]
    fn corpus_counts_fill_the_back_of_the_pre_registration_sheet() {
        let incs = vec![
            inc("ugv-01", FailureKind::FcFailure, 60_000, false),
            inc_ch(
                "ugv-01",
                Family::Ugv,
                FailureKind::GnssDegradation,
                120_000,
                vec![ChannelId::GpsEkf],
            ),
            inc_ch(
                "fpv-01",
                Family::Fpv,
                FailureKind::CameraStop,
                120_000,
                vec![ChannelId::Video],
            ),
            inc_ch(
                "fpv-01",
                Family::Fpv,
                FailureKind::RcLinkLoss,
                70_000,
                vec![ChannelId::Rc],
            ),
            inc("ugv-02", FailureKind::FcFailure, 60_000, true),
        ];
        let m = evaluate_with(
            &incs,
            &[],
            &[],
            &kinds(),
            3,
            180_000,
            &EvalConfig::default(),
        );
        let c = &m.corpus;
        assert_eq!(c.c1_incidents, 5);
        assert_eq!(c.c2_s_layer, 4);
        assert_eq!(c.c3_indistinguishable, 1);
        assert_eq!(c.c4_missing_channel, 1);
        assert_eq!(c.c5_gnss_value_degradation, 1);
        assert_eq!(c.c5_video_presence_only, 1);
        assert_eq!(c.c6_fpv_incidents, 2);
        assert!(c.c7_clean_asset_hours > 0.0);
        assert_eq!(
            c.c10_abstention_lower_bound_noticed, None,
            "noticed 0 件では下限を出さない"
        );
    }

    #[test]
    fn top_k_candidates_derive_by_the_same_rule_as_the_single_top_candidate() {
        let o = consistent(&[
            ("x", LogicConfidence::LeadingIndicator),
            ("y", LogicConfidence::DirectEvidence),
            ("z", LogicConfidence::LeadingIndicator),
        ]);
        let ids: Vec<String> = top_k_candidates(&o, 3).into_iter().map(|c| c.0).collect();
        assert_eq!(ids, vec!["y".to_string(), "x".to_string(), "z".to_string()]);
        assert_eq!(top_k_candidates(&o, 1).len(), 1);
        assert_eq!(
            top_k_candidates(&o, 1)[0].0,
            top_candidate(&o).expect("top").0
        );
        assert!(
            top_k_candidates(
                &CauseOutcome::Unknown {
                    reason: UnknownReason::NoSignatureMatch
                },
                3
            )
            .is_empty()
        );
    }
}
