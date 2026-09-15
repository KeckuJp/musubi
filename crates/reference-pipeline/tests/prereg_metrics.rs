#![allow(clippy::doc_markdown, clippy::expect_used, clippy::unwrap_used)]

use std::collections::BTreeMap;

use musubi_reference_pipeline::prereg::{g_eff_table, report_text};
use musubi_reference_pipeline::{Knowledge, metrics, run_pipeline};
use musubi_reference_readers::eval::{AttributionWindow, CauseStamp, EvalConfig};
use musubi_reference_scenario::{FailureKind, Family, SourceRole, generate, pre_demo_default};
use musubi_reference_types::ChannelId;

#[test]
fn seed_42_corpus_yields_the_primary_metric_and_the_corpus_table() {
    let tl = generate(&pre_demo_default(42));
    let out = run_pipeline(&tl);
    let m = metrics(&tl, &out);
    println!("P-23 seed=42: {}", m.summary());
    println!("{}", m.detail_block());
    assert_eq!(m.incidents, 5);
    assert!(
        m.cause_at_1.p90_ms.is_some() || m.cause_at_1.censored == m.incidents,
        "{:?}",
        m.cause_at_1
    );
    assert_eq!(
        m.cause_at_1.samples_ms.len() + m.cause_at_1.censored,
        m.incidents,
        "全 incident が「出た」か censored のどちらかに入る"
    );
    assert!(m.cause_at_3.censored <= m.cause_at_1.censored);
    if let (Some(a), Some(b)) = (m.cause_at_1.p90_ms, m.cause_at_3.p90_ms) {
        assert!(b <= a, "@3 p90 {b} must not exceed @1 p90 {a}");
    }
    assert!(m.precision_at_3.is_some());
    assert_eq!(m.false_certainty, 0);
    assert_eq!(m.ambiguous_class_honest, m.ambiguous_class_total);
    let c = &m.corpus;
    assert_eq!(c.c1_incidents, 5);
    assert_eq!(c.c6_fpv_incidents, 2, "rc_link_loss ＋ camera_stop");
    assert_eq!(c.c5_gnss_value_degradation, 1);
    assert_eq!(c.c5_video_presence_only, 1);
    assert_eq!(c.c2_s_layer + c.c3_indistinguishable, c.c1_incidents);
    assert!(c.c7_clean_asset_hours > 0.0 && c.c7_clean_asset_hours < m.asset_hours);
}

#[test]
fn the_report_prints_every_sheet_row_and_no_acceptance_level() {
    let text = report_text(42, &EvalConfig::default(), &BTreeMap::new());
    for row in [
        "LI-ACA-1", "LI-ACA-2", "LI-ACA-3", "LI-ACA-4", "LI-ACA-5", "LI-ACA-6", "GR-ACA-1",
        "GR-ACA-2", "GR-ACA-3", "C-1", "C-2", "C-3", "C-4", "C-5", "C-6", "C-7", "C-8", "C-10",
        "B1", "B2", "B3", "B4", "B5", "B6",
    ] {
        assert!(text.contains(row), "report has no row {row}");
    }
    for banned in [
        "PASS",
        "FAIL",
        "pass/fail",
        "acceptable",
        "受入水準",
        "許容件数",
    ] {
        assert!(
            !text.contains(banned),
            "the report must not state an acceptance level: {banned}"
        );
    }
    assert!(text.contains("B8"), "B8 は空欄として残す旨を出す");
    assert!(text.contains("left blank for the evaluator"));
    assert!(text.contains("synthetic"));
    for k in ["A9", "A13", "A14", "cause_stamp"] {
        assert!(
            text.contains(k),
            "the report must name the open definition {k}"
        );
    }
}

#[test]
fn the_report_reads_the_alternative_definitions_too() {
    let alt = EvalConfig {
        attribution_window: AttributionWindow::InjectStartPlusTMax,
        cause_stamp: CauseStamp::WindowStart,
        ..EvalConfig::default()
    };
    let text = report_text(42, &alt, &BTreeMap::new());
    assert!(text.contains("inject_start_plus_t_max"));
    assert!(text.contains("window_start"));
}

#[test]
fn g_eff_overrides_move_the_shipped_profile_value_for_detection_and_scoring_together() {
    let base = Knowledge::repo_public();
    let shipped: Vec<u64> = g_eff_table(&base.profiles)
        .iter()
        .filter(|r| r.channel == ChannelId::Heartbeat)
        .map(|r| r.g_eff_ms)
        .collect();
    assert!(
        shipped.iter().all(|g| *g == 3_000),
        "shipped heartbeat G_eff = 3 s: {shipped:?}"
    );
    let mut o = BTreeMap::new();
    o.insert(ChannelId::Heartbeat, 5_000_u64);
    let moved = base.with_g_eff_overrides(&o);
    for r in g_eff_table(&moved.profiles) {
        if r.channel == ChannelId::Heartbeat {
            assert_eq!(r.g_eff_ms, 5_000, "{}", r.expectation_id);
        }
    }
    let tl = generate(&pre_demo_default(42));
    let out = musubi_reference_pipeline::run_pipeline_with(&tl, &moved);
    assert!(!out.claims.is_empty());
}

#[test]
fn the_guardrail_counters_move_when_the_corpus_makes_the_screen_abstain() {
    let mut sc = pre_demo_default(7);
    sc.injections.clear();
    sc.assets[0].suppressed_channels = vec![ChannelId::LinkStats];
    sc.assets[0].sources = vec![SourceRole::Gcs];
    sc.assets[2].sources = vec![SourceRole::Video];
    sc.inject(FailureKind::FcFailure, 60_000, 60_000, Family::Ugv);
    sc.inject(FailureKind::CameraStop, 60_000, 30_000, Family::Fpv);
    let tl = generate(&sc);
    let out = run_pipeline(&tl);
    let m = metrics(&tl, &out);
    println!("P-23 abstention variant: {}", m.summary());
    println!("{}", m.detail_block());
    assert_eq!(m.false_certainty, 0);
    assert!(m.ambiguous_class_total > 0);
    assert_eq!(m.ambiguity_honesty_rate, Some(1.0));
    assert!(m.missing_channel_incidents > 0);
    assert!(
        m.corpus.c4_missing_channel > 0 || m.unknown_by_reason.contains_key("channel-not-observed")
    );
    assert!(m.cause_at_1.censored > 0, "{:?}", m.cause_at_1);
    assert_eq!(
        m.cause_at_1.samples_ms.len() + m.cause_at_1.censored,
        m.incidents
    );
}
