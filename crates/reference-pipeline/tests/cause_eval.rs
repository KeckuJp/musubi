use musubi_reference_pipeline::{claims_for, metrics, run_pipeline};
use musubi_reference_scenario::{
    ChannelId, FailureKind, Family, Injection, LinkProfile, LogEndMode, generate, pre_demo_default,
};
use musubi_reference_types::{CauseOutcome, ClaimKind, LogicConfidence, UnknownReason};

fn top_ids(o: &CauseOutcome) -> Vec<String> {
    match o {
        CauseOutcome::Consistent { candidates } | CauseOutcome::Ambiguous { candidates, .. } => {
            candidates.iter().map(|c| c.signature_id.clone()).collect()
        }
        CauseOutcome::Unknown { .. } => vec![],
    }
}

#[test]
fn default_scenario_yields_consistent_candidates_matching_ground_truth_and_metrics() {
    let tl = generate(&pre_demo_default(42));
    let out = run_pipeline(&tl);
    assert_eq!(
        out.files.len(),
        7,
        "{:?}",
        out.files
            .iter()
            .map(|f| f.file_name.clone())
            .collect::<Vec<_>>()
    );
    for c in &out.claims {
        assert_eq!(c.claim.kind, ClaimKind::Candidate);
        assert!(
            c.claim.mark.reason_code.starts_with("cause-"),
            "{}",
            c.claim.mark.reason_code
        );
        assert!(!c.claim.mark.reason_code.contains(';'));
    }
    let t0 = tl.t0_unix_us / 1000;
    let find = |asset: &str, at_ms: i64| {
        out.claims
            .iter()
            .find(|c| {
                c.asset_id == asset
                    && c.window_start_ms <= t0 + at_ms
                    && c.window_end_ms > t0 + at_ms
            })
            .unwrap_or_else(|| {
                panic!(
                    "claim for {asset} around +{at_ms} ms: {:?}",
                    out.claims
                        .iter()
                        .map(|c| (
                            c.asset_id.clone(),
                            c.window_start_ms - t0,
                            top_ids(&c.claim.outcome)
                        ))
                        .collect::<Vec<_>>()
                )
            })
    };
    let c = find("ugv-01", 62_000);
    let CauseOutcome::Consistent { candidates } = &c.claim.outcome else {
        panic!("{:?}", c.claim.outcome)
    };
    assert_eq!(candidates[0].signature_id, "telemetry_link_loss");
    assert!(
        candidates[0].logic_confidence >= LogicConfidence::DirectEvidence,
        "{:?}",
        candidates[0]
    );
    let c = find("ugv-01", 135_000);
    let CauseOutcome::Consistent { candidates } = &c.claim.outcome else {
        panic!("{:?}", c.claim.outcome)
    };
    assert_eq!(
        candidates[0].signature_id,
        "gnss_degradation",
        "{:?}",
        top_ids(&c.claim.outcome)
    );
    let c = find("plane-01", 92_000);
    let CauseOutcome::Consistent { candidates } = &c.claim.outcome else {
        panic!("{:?}", c.claim.outcome)
    };
    assert_eq!(
        candidates[0].signature_id,
        "fc_failure",
        "{:?}",
        top_ids(&c.claim.outcome)
    );
    assert!(
        candidates[0]
            .signature_match
            .optional_cues_matched
            .contains(&"link_stats:radio_remote_alive".to_string())
    );
    let c = find("fpv-01", 72_000);
    let CauseOutcome::Consistent { candidates } = &c.claim.outcome else {
        panic!("{:?}", c.claim.outcome)
    };
    assert_eq!(
        candidates[0].signature_id,
        "rf_link_loss",
        "{:?}",
        top_ids(&c.claim.outcome)
    );
    let c = find("fpv-01", 122_000);
    let CauseOutcome::Consistent { candidates } = &c.claim.outcome else {
        panic!("{:?}", c.claim.outcome)
    };
    assert_eq!(
        candidates[0].signature_id,
        "camera_stop",
        "{:?}",
        top_ids(&c.claim.outcome)
    );
    let m = metrics(&tl, &out);
    println!("METRICS default seed=42: {}", m.summary());
    for c in &out.claims {
        println!(
            "CLAIM {} +{}..+{} ms subject={:?} -> {:?} [{}]",
            c.asset_id,
            c.window_start_ms - t0,
            c.window_end_ms - t0,
            c.claim.subject.channel,
            top_ids(&c.claim.outcome),
            c.claim.mark.reason_code
        );
    }
    assert_eq!(m.incidents, 5);
    assert_eq!(m.missed, 0);
    assert_eq!(
        m.false_certainty, 0,
        "guardrail: no falsely certain attribution"
    );
    assert_eq!(m.precision_at_1_hits, 5);
}

#[test]
fn no_injection_scenario_has_no_false_alarms() {
    let mut s = pre_demo_default(5);
    s.injections.clear();
    let tl = generate(&s);
    let out = run_pipeline(&tl);
    let m = metrics(&tl, &out);
    println!("METRICS no-injection seed=5: {}", m.summary());
    assert_eq!(m.incidents, 0);
    assert_eq!(
        m.false_alarms,
        0,
        "{:?}",
        out.noticed
            .iter()
            .map(|r| (
                r.asset_id.clone(),
                r.negative.subject.channel,
                r.since_wall_ms - tl.t0_unix_us / 1000
            ))
            .collect::<Vec<_>>()
    );
    assert!(out.claims.is_empty());
}

#[test]
fn tlog_only_silence_is_ambiguous_when_radio_status_is_dropped_and_onboard_absent() {
    let mut s = pre_demo_default(7);
    s.injections.clear();
    s.assets[0].suppressed_channels = vec![ChannelId::LinkStats];
    s.assets[0].sources = vec![musubi_reference_scenario::SourceRole::Gcs];
    s.inject(FailureKind::FcFailure, 60_000, 60_000, Family::Ugv);
    let tl = generate(&s);
    let out = run_pipeline(&tl);
    let claims = claims_for(&out, "ugv-01");
    assert!(!claims.is_empty());
    for c in &claims {
        match &c.outcome {
            CauseOutcome::Ambiguous {
                candidates,
                indistinguishable_by,
            } => {
                let ids = candidates
                    .iter()
                    .map(|c| c.signature_id.as_str())
                    .collect::<Vec<_>>();
                assert!(
                    ids.contains(&"telemetry_link_loss") || ids.contains(&"fc_failure"),
                    "{ids:?}"
                );
                assert!(
                    !indistinguishable_by.is_empty(),
                    "what would disambiguate must be named"
                );
            }
            CauseOutcome::Unknown { reason } => {
                assert!(
                    matches!(
                        reason,
                        UnknownReason::ChannelNotObserved | UnknownReason::InsufficientEvidence
                    ),
                    "{reason:?}"
                );
            }
            CauseOutcome::Consistent { candidates } => {
                assert!(
                    candidates
                        .iter()
                        .all(|k| k.logic_confidence <= LogicConfidence::LeadingIndicator),
                    "{candidates:?}"
                );
            }
        }
    }
    let m = metrics(&tl, &out);
    println!("METRICS D1 tlog-only: {}", m.summary());
    assert_eq!(m.false_certainty, 0);
    assert_eq!(m.ambiguous_class_total, 1);
    assert_eq!(m.ambiguous_class_honest, 1, "ambiguity_honesty 100%");
}

#[test]
fn fiber_break_vs_rf_loss_are_separated_by_prelude_and_video() {
    for (lp, expect) in [
        (LinkProfile::Fiber, "fiber_break"),
        (LinkProfile::FiberPlusRadio, "fiber_break"),
        (LinkProfile::AnalogRf, "rf_link_loss"),
    ] {
        let mut s = pre_demo_default(9);
        s.injections.clear();
        s.assets[2].link_profile = lp;
        if lp == LinkProfile::AnalogRf {
            s.inject(FailureKind::RcLinkLoss, 60_000, 30_000, Family::Fpv);
        } else {
            s.inject(FailureKind::FiberBreak, 60_000, 30_000, Family::Fpv);
        }
        let tl = generate(&s);
        let out = run_pipeline(&tl);
        let t0 = tl.t0_unix_us / 1000;
        let c = out
            .claims
            .iter()
            .find(|c| {
                c.asset_id == "fpv-01"
                    && c.window_start_ms <= t0 + 63_000
                    && c.window_end_ms > t0 + 63_000
            })
            .unwrap_or_else(|| {
                panic!(
                    "{lp:?}: claims {:?}",
                    out.claims
                        .iter()
                        .map(|c| (c.window_start_ms - t0, top_ids(&c.claim.outcome)))
                        .collect::<Vec<_>>()
                )
            });
        let ids = top_ids(&c.claim.outcome);
        assert_eq!(
            ids.first().map(String::as_str),
            Some(expect),
            "{lp:?}: {:?}",
            c.claim.outcome
        );
        let m = metrics(&tl, &out);
        println!("METRICS {lp:?}: {}", m.summary());
        assert_eq!(m.false_certainty, 0);
        assert_eq!(m.missed, 0);
    }
}

#[test]
fn video_only_source_abstains_with_channel_not_observed() {
    let mut s = pre_demo_default(3);
    s.injections.clear();
    s.assets[2].sources = vec![musubi_reference_scenario::SourceRole::Video];
    s.inject(FailureKind::CameraStop, 60_000, 30_000, Family::Fpv);
    let tl = generate(&s);
    let out = run_pipeline(&tl);
    let claims = claims_for(&out, "fpv-01");
    assert_eq!(claims.len(), 1, "{claims:?}");
    assert_eq!(
        claims[0].outcome,
        CauseOutcome::Unknown {
            reason: UnknownReason::ChannelNotObserved
        }
    );
    assert!(
        claims[0]
            .mark
            .reason_code
            .contains("cause-unknown:channel-not-observed")
    );
    assert!(
        claims[0]
            .input_confidence
            .channel_coverage
            .required
            .contains(&ChannelId::LinkStats)
    );
    assert!(
        !claims[0]
            .input_confidence
            .channel_coverage
            .present
            .contains(&ChannelId::LinkStats)
    );
}

#[test]
fn reboot_variant_keeps_fc_failure_candidate_with_reboot_marker() {
    let mut s = pre_demo_default(13);
    s.injections.clear();
    let mut i = Injection::new(FailureKind::FcFailure, 90_000, 60_000, Family::FixedWing);
    i.params.log_end_mode = LogEndMode::Reboot { after_ms: 20_000 };
    s.inject_with(i);
    let tl = generate(&s);
    let out = run_pipeline(&tl);
    let t0 = tl.t0_unix_us / 1000;
    let c = out
        .claims
        .iter()
        .find(|c| {
            c.asset_id == "plane-01"
                && c.window_start_ms <= t0 + 92_000
                && c.window_end_ms > t0 + 92_000
        })
        .expect("claim");
    let CauseOutcome::Consistent { candidates } = &c.claim.outcome else {
        panic!("{:?}", c.claim.outcome)
    };
    assert_eq!(candidates[0].signature_id, "fc_failure");
    assert!(
        candidates[0]
            .signature_match
            .optional_cues_matched
            .contains(&"event:reboot_marker".to_string()),
        "{:?}",
        candidates[0]
    );
}
