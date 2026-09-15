use std::collections::BTreeMap;
use std::fmt::Write as _;

use musubi_reference_readers::eval::{
    AttributionWindow, CauseTiming, EvalConfig, Incident, Metrics, NoticedAbsence, evaluate_with,
};
use musubi_reference_scenario::{generate, pre_demo_default};
use musubi_reference_types::{ChannelId, Family, FamilyProfile};

use crate::export::NOTE_CEILING;
use crate::{GroundTruthRow, Knowledge, incidents_from, run_pipeline_with};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GEffRow {
    pub sheet_row: &'static str,
    pub profile_id: String,
    pub expectation_id: String,
    pub channel: ChannelId,
    pub cadence_ms: u64,
    pub grace_k: u32,
    pub g_eff_ms: u64,
    pub frozen_ms: Option<u64>,
}

fn sheet_row_of(profile_id: &str, channel: ChannelId, cadence_ms: u64) -> &'static str {
    if cadence_ms <= 200 {
        "B3" // 高速 channel（100 ms 行＝blackbox の記録／RC）
    } else if channel == ChannelId::LinkStats {
        if profile_id.contains("handset") {
            "B4" // 送信機側のテレメトリ（EdgeTX SD Logs 行）
        } else {
            "B2" // 地上局との通信状態（RADIO_STATUS／telemetry_status）
        }
    } else if channel == ChannelId::Heartbeat {
        "B1"
    } else {
        "B5" // 機体内部の記録／操縦入力・測位の停止／映像 file の生存
    }
}

#[must_use]
pub fn g_eff_table(profiles: &[FamilyProfile]) -> Vec<GEffRow> {
    let mut rows: Vec<GEffRow> = profiles
        .iter()
        .flat_map(|p| {
            p.expectations.iter().map(move |e| GEffRow {
                sheet_row: sheet_row_of(&p.profile_id, e.channel, e.cadence_ms),
                profile_id: p.profile_id.clone(),
                expectation_id: e.expectation_id.clone(),
                channel: e.channel,
                cadence_ms: e.cadence_ms,
                grace_k: e.grace_k,
                g_eff_ms: e.cadence_ms * u64::from(e.grace_k),
                frozen_ms: e.frozen_ms,
            })
        })
        .collect();
    rows.sort_by(|a, b| {
        (a.sheet_row, &a.profile_id, &a.expectation_id).cmp(&(
            b.sheet_row,
            &b.profile_id,
            &b.expectation_id,
        ))
    });
    rows
}

impl Knowledge {
    #[must_use]
    pub fn with_g_eff_overrides(mut self, overrides: &BTreeMap<ChannelId, u64>) -> Self {
        for p in &mut self.profiles {
            for e in &mut p.expectations {
                if let Some(g) = overrides.get(&e.channel) {
                    let c = e.cadence_ms.max(1);
                    e.grace_k = u32::try_from(g.div_ceil(c).max(1)).unwrap_or(u32::MAX);
                }
            }
        }
        self
    }
}

fn opt_i(v: Option<i64>) -> String {
    v.map_or_else(|| "-".to_string(), |x| x.to_string())
}

fn opt_f(v: Option<f64>) -> String {
    v.map_or_else(|| "-".to_string(), |x| format!("{x:.3}"))
}

fn timing_cell(t: &CauseTiming) -> String {
    format!(
        "{} / {} (scored={} censored={} early_true={} p90_with_censored_at_bound={})",
        opt_i(t.median_ms),
        opt_i(t.p90_ms),
        t.samples_ms.len(),
        t.censored,
        t.early_true,
        opt_i(t.p90_with_censored_at_bound_ms)
    )
}

fn subset(
    incidents: &[Incident],
    keep: impl Fn(&Incident) -> bool,
    noticed: &[NoticedAbsence],
    claims: &[crate::Claim],
    kinds: &BTreeMap<String, musubi_reference_types::FailureKind>,
    asset_count: usize,
    duration_ms: i64,
    cfg: &EvalConfig,
) -> Metrics {
    let inc: Vec<Incident> = incidents.iter().filter(|i| keep(i)).cloned().collect();
    evaluate_with(&inc, noticed, claims, kinds, asset_count, duration_ms, cfg)
}

#[must_use]
pub fn report_text(
    seed: u64,
    cfg: &EvalConfig,
    g_eff_overrides: &BTreeMap<ChannelId, u64>,
) -> String {
    let knowledge = Knowledge::repo_public().with_g_eff_overrides(g_eff_overrides);
    let tl = generate(&pre_demo_default(seed));
    let out = run_pipeline_with(&tl, &knowledge);
    let rows: Vec<GroundTruthRow> = tl.ground_truth.iter().map(GroundTruthRow::from).collect();
    let t0_ms = tl.t0_unix_us / 1000;
    let incidents = incidents_from(&rows, t0_ms, &knowledge.profiles);
    let noticed: Vec<NoticedAbsence> = out
        .noticed
        .iter()
        .map(|n| NoticedAbsence::from_negative(&n.negative, n.since_wall_ms, n.by_wall_ms))
        .collect();
    let kinds = knowledge.catalog_kinds();
    let assets = tl.assets.len();
    let dur = tl.duration_ms as i64;
    let m = evaluate_with(&incidents, &noticed, &out.claims, &kinds, assets, dur, cfg);
    let s = &mut String::new();

    let _ = writeln!(
        s,
        "musubi pre-demo detection metrics -- input for the pre-registration sheet (P-23 / R-16 sec.6 / R-05 sec.4.2)"
    );
    let _ = writeln!(
        s,
        "seed={seed}  assets={assets}  duration_ms={dur}  profiles=profiles/public  catalog={}",
        knowledge.catalog.version
    );
    let _ = writeln!(s, "claim ceiling: {NOTE_CEILING}");
    let _ = writeln!(
        s,
        "note: measured values and denominators only. no acceptance level and no verdict is stated here."
    );
    let _ = writeln!(
        s,
        "note: B8 (accepted level for the primary metric) and B10-B12 (counts) are left blank for the evaluator on 9/3 (R-16 sec.4)."
    );

    let _ = writeln!(
        s,
        "\n[1] front side, Block B-I -- when a gap starts counting (G_eff = cadence x grace_k, shipped values)"
    );
    let _ = writeln!(
        s,
        "sheet_row  channel      expectation_id            cadence_ms  grace_k  g_eff_ms  frozen_ms  profile_id"
    );
    for r in g_eff_table(&knowledge.profiles) {
        let _ = writeln!(
            s,
            "{:<10} {:<12} {:<25} {:<11} {:<8} {:<9} {:<10} {}",
            r.sheet_row,
            r.channel.as_str(),
            r.expectation_id,
            r.cadence_ms,
            r.grace_k,
            r.g_eff_ms,
            r.frozen_ms
                .map_or_else(|| "-".to_string(), |v| v.to_string()),
            r.profile_id
        );
    }
    let _ = writeln!(
        s,
        "B4'        (frozen)     value unchanged for frozen_ms is a separate rule from grace (R-16 B4')"
    );
    let _ = writeln!(
        s,
        "B6         (window)     observation window t_max_ms = {} (EvalConfig; shipped default)",
        cfg.t_max_ms
    );

    let _ = writeln!(
        s,
        "\n[2] front side -- the six metrics and the three guardrails on this synthetic corpus"
    );
    let _ = writeln!(s, "id         metric                                 value");
    let _ = writeln!(
        s,
        "LI-ACA-1   time_to_notice_absence  p50/p90 ms     {} / {} (n={} early_true={})",
        opt_i(m.time_to_notice_median_ms),
        opt_i(m.time_to_notice_p90_ms),
        m.time_to_notice_ms.len(),
        m.early_true
    );
    let _ = writeln!(
        s,
        "LI-ACA-2   time_to_plausible_cause@1 p50/p90 ms    {}   <-- B7 default primary (P1, top-1)",
        timing_cell(&m.cause_at_1)
    );
    let _ = writeln!(
        s,
        "LI-ACA-2   time_to_plausible_cause@3 p50/p90 ms    {}",
        timing_cell(&m.cause_at_3)
    );
    let _ = writeln!(
        s,
        "LI-ACA-3   false_alarm  count / per clean asset-hour  {} / {:.2}  (clean_asset_hours={:.4}, all_asset_hours={:.4})",
        m.false_alarms, m.false_alarm_rate_per_clean_asset_hour, m.clean_asset_hours, m.asset_hours
    );
    let _ = writeln!(
        s,
        "LI-ACA-4   missed_absence  count / rate           {} / {}  (of {} incidents)",
        m.missed,
        opt_f(m.missed_rate),
        m.incidents
    );
    let _ = writeln!(
        s,
        "LI-ACA-5   cause_candidate_precision@1 / @3       {} ({}/{}) / {} ({}/{})",
        opt_f(m.precision_at_1),
        m.precision_at_1_hits,
        m.precision_at_1_total,
        opt_f(m.precision_at_3),
        m.precision_at_3_hits,
        m.precision_at_1_total
    );
    let _ = writeln!(
        s,
        "LI-ACA-6   abstention_rate_S / overall            {} ({}/{}) / {} ({}/{})",
        opt_f(m.abstention_rate_s),
        m.abstained_s,
        m.claimed_s,
        opt_f(m.abstention_rate),
        m.abstained,
        m.precision_at_1_total + m.abstained
    );
    let _ = writeln!(
        s,
        "           unknown_by_reason                      {:?}",
        m.unknown_by_reason
    );
    let _ = writeln!(
        s,
        "GR-ACA-1   false_certainty (falsely certain)      {} (defined as 0 by R-05 sec.4.2; this line reports the count)",
        m.false_certainty
    );
    let _ = writeln!(
        s,
        "GR-ACA-2   ambiguity_honesty                      {}/{} (rate {})",
        m.ambiguous_class_honest,
        m.ambiguous_class_total,
        opt_f(m.ambiguity_honesty_rate)
    );
    let _ = writeln!(
        s,
        "GR-ACA-3   missing-channel disposition            {} claim(s) reported as unknown{{channel-not-observed}}, counted separately",
        m.missing_channel_incidents
    );

    let m_non_fpv = subset(
        &incidents,
        |i| i.family != Family::Fpv,
        &noticed,
        &out.claims,
        &kinds,
        assets,
        dur,
        cfg,
    );
    let m_fpv = subset(
        &incidents,
        |i| i.family == Family::Fpv,
        &noticed,
        &out.claims,
        &kinds,
        assets,
        dur,
        cfg,
    );
    let _ = writeln!(
        s,
        "\n[3] B9 -- FPV in the primary denominator or listed separately (sheet default: listed separately)"
    );
    let _ = writeln!(
        s,
        "group      incidents  ttpc@1 p50/p90 ms                       precision@1  precision@3  abstention_rate_S"
    );
    for (name, x) in [("all", &m), ("non-FPV", &m_non_fpv), ("FPV-only", &m_fpv)] {
        let _ = writeln!(
            s,
            "{:<10} {:<10} {:<39} {:<12} {:<12} {}",
            name,
            x.incidents,
            format!(
                "{} / {} (censored={})",
                opt_i(x.cause_at_1.median_ms),
                opt_i(x.cause_at_1.p90_ms),
                x.cause_at_1.censored
            ),
            opt_f(x.precision_at_1),
            opt_f(x.precision_at_3),
            opt_f(x.abstention_rate_s)
        );
    }
    let _ = writeln!(
        s,
        "note: false_alarm and the clean asset-hour denominator are read from the 'all' row only (a subset run re-attributes absences)."
    );

    let c = &m.corpus;
    let _ = writeln!(
        s,
        "\n[4] back side -- corpus composition (printed before the sheet is handed over; the evaluator sees totals only)"
    );
    let _ = writeln!(
        s,
        "C-1   total incidents (family x cause class x seed)          {}",
        c.c1_incidents
    );
    let _ = writeln!(
        s,
        "C-2   distinguishable AND channel-complete (= layer S)      {}",
        c.c2_s_layer
    );
    let _ = writeln!(
        s,
        "C-3   indistinguishable class (R-02 D1-D14)                 {}",
        c.c3_indistinguishable
    );
    let _ = writeln!(
        s,
        "C-4   missing-channel (unknown{{channel-not-observed}})       {}",
        c.c4_missing_channel
    );
    let _ = writeln!(
        s,
        "C-5   GNSS value-degradation / video-presence-only           {} / {}",
        c.c5_gnss_value_degradation, c.c5_video_presence_only
    );
    let _ = writeln!(
        s,
        "C-6   FPV family incidents                                   {}",
        c.c6_fpv_incidents
    );
    let _ = writeln!(
        s,
        "C-7   clean asset-hours (non-injected)                       {:.4}",
        c.c7_clean_asset_hours
    );
    let _ = writeln!(
        s,
        "C-8   candidate list length 1/2/3/4+                         {}/{}/{}/{}",
        c.c8_candidate_list_len.get(&1).copied().unwrap_or(0),
        c.c8_candidate_list_len.get(&2).copied().unwrap_or(0),
        c.c8_candidate_list_len.get(&3).copied().unwrap_or(0),
        c.c8_candidate_list_len.get(&4).copied().unwrap_or(0)
    );
    let _ = writeln!(
        s,
        "C-8   clock-fault injections / self-liveness losses          0 / 0 (not injected in this corpus; no such injection kind exists yet)"
    );
    let _ = writeln!(
        s,
        "C-10  lower bound of the overall abstention rate             {} = C-3 / (claimed + abstained) [shipped denominator]",
        opt_f(c.c10_abstention_lower_bound_claimed)
    );
    let _ = writeln!(
        s,
        "C-10  same, with the R-05 sec.4.2 wording denominator        {} = C-3 / noticed",
        opt_f(c.c10_abstention_lower_bound_noticed)
    );

    let alt_cfg = EvalConfig {
        attribution_window: match cfg.attribution_window {
            AttributionWindow::InjectEndPlusTMax => AttributionWindow::InjectStartPlusTMax,
            AttributionWindow::InjectStartPlusTMax => AttributionWindow::InjectEndPlusTMax,
        },
        ..*cfg
    };
    let alt = evaluate_with(
        &incidents,
        &noticed,
        &out.claims,
        &kinds,
        assets,
        dur,
        &alt_cfg,
    );
    let _ = writeln!(
        s,
        "\n[5] open definitions -- one reading has to be fixed at D-16 (R-16 sec.2.2); both readings are computed here"
    );
    let _ = writeln!(
        s,
        "A9   attribution window        used={}  |  alternative={}: ttpc@1 p90={} censored={} missed={}",
        cfg.attribution_window.as_label(),
        alt_cfg.attribution_window.as_label(),
        opt_i(alt.cause_at_1.p90_ms),
        alt.cause_at_1.censored,
        alt.missed
    );
    let _ = writeln!(
        s,
        "A13  abstention denominator    shipped (channel-not-observed counted)={} ({}/{})  |  excluded={} ({}/{})",
        opt_f(m.abstention_rate),
        m.abstained,
        m.precision_at_1_total + m.abstained,
        opt_f(m.abstention_rate_excl_channel_not_observed),
        m.abstained_excl_channel_not_observed,
        m.precision_at_1_total + m.abstained_excl_channel_not_observed
    );
    let _ = writeln!(
        s,
        "A-1  abstention_rate_S numerator  unknown within S / claims within S = {}/{} (reading (ii) = (iii))",
        m.abstained_s, m.claimed_s
    );
    let _ = writeln!(
        s,
        "A14  censored in p90           count-only p90={}  |  imputed at the window bound p90={} (censored={})",
        opt_i(m.cause_at_1.p90_ms),
        opt_i(m.cause_at_1.p90_with_censored_at_bound_ms),
        m.cause_at_1.censored
    );
    let _ = writeln!(
        s,
        "cause_stamp  when a cause candidate counts as out   used={} (default: the analysis window closes; the alternative is the trigger time)",
        cfg.cause_stamp.as_label()
    );
    let _ = writeln!(
        s,
        "\n[6] raw metrics line (same text as metrics.txt)\n{}\n{}",
        m.summary(),
        m.detail_block()
    );
    s.clone()
}
