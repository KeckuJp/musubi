use musubi_reference_types::{
    ClockBasis, ClockRateClass, ClockRateConfig, ClockRateGap, ClockRateOutcome, ClockRateReport,
    OffsetEstimate, OffsetSource, OrderRelation, TimeAlignment, order_with_bounds,
};

use crate::{Observation, default_time_confidence};

pub const DRIFT_PPM_BOUND: f64 = 100.0;
pub const DISCONTINUITY_US: i64 = 1_000_000;
pub const ANCHOR_TAIL_GAP_MS: i64 = 5_000;

#[must_use]
pub fn estimate_alignment(obs: &[Observation], default_basis: ClockBasis) -> TimeAlignment {
    let mut anchors: Vec<(u64, i64)> = obs
        .iter()
        .filter_map(|o| Some((o.t_boot_us?, o.anchor_unix_us?)))
        .collect();
    anchors.sort_unstable();
    anchors.dedup();
    if matches!(
        default_basis,
        ClockBasis::HostReceived | ClockBasis::Unknown
    ) && anchors.is_empty()
    {
        return TimeAlignment {
            basis: default_basis,
            offset: None,
        };
    }
    if anchors.is_empty() {
        return TimeAlignment {
            basis: ClockBasis::BootRelative,
            offset: None,
        };
    }
    let mut offsets: Vec<i64> = anchors.iter().map(|&(b, a)| a - b as i64).collect();
    let discontinuity = offsets
        .windows(2)
        .any(|w| (w[1] - w[0]).abs() >= DISCONTINUITY_US);
    offsets.sort_unstable();
    let median = offsets[offsets.len() / 2];
    let spread = offsets
        .iter()
        .map(|o| (o - median).abs())
        .max()
        .unwrap_or(0);
    let (first_boot, last_boot) = (anchors[0].0, anchors[anchors.len() - 1].0);
    let log_last_boot = obs
        .iter()
        .filter_map(|o| o.t_boot_us)
        .max()
        .unwrap_or(last_boot);
    let extrapolated_us = log_last_boot.saturating_sub(last_boot) as f64
        + if anchors.len() == 1 {
            (log_last_boot - first_boot) as f64
        } else {
            0.0
        };
    let bound = spread + (extrapolated_us * DRIFT_PPM_BOUND * 1e-6).ceil() as i64;
    let tail_gap_ms = (log_last_boot.saturating_sub(last_boot) / 1000) as i64;
    let max_inner_gap_ms = anchors
        .windows(2)
        .map(|p| ((p[1].0 - p[0].0) / 1000) as i64)
        .max()
        .unwrap_or(0);
    let basis = if matches!(default_basis, ClockBasis::HostReceived) {
        ClockBasis::HostReceived // host 軸は据え置き（機体 offset は付加情報・不連続は `discontinuity` flag に残る）。
    } else if discontinuity {
        ClockBasis::GpsSuspect
    } else if tail_gap_ms <= ANCHOR_TAIL_GAP_MS && max_inner_gap_ms <= ANCHOR_TAIL_GAP_MS {
        ClockBasis::GpsLocked
    } else {
        ClockBasis::BootRelativeOffsetEstimated
    };
    TimeAlignment {
        basis,
        offset: Some(OffsetEstimate {
            offset_us: median,
            bound_us: bound.max(1),
            source: OffsetSource::InLogAnchor,
            anchors: anchors.len() as u32,
            discontinuity,
        }),
    }
}

#[must_use]
pub fn estimate_relative_rate(
    obs: &[Observation],
    al: &TimeAlignment,
    cfg: &ClockRateConfig,
) -> ClockRateReport {
    let gap = |g: ClockRateGap| ClockRateReport {
        outcome: ClockRateOutcome::InsufficientBasis(g),
        anchors: 0,
        span_us: 0,
        config: *cfg,
    };
    if matches!(al.basis, ClockBasis::HostReceived | ClockBasis::Unknown) {
        return gap(ClockRateGap::ClockBasisNotDeviceClock);
    }
    match al.offset {
        None => return gap(ClockRateGap::FewerThanTwoAnchors),
        Some(o) if o.source != OffsetSource::InLogAnchor => {
            return gap(ClockRateGap::OffsetSourceNotInLogAnchor);
        }
        Some(o) if o.discontinuity => return gap(ClockRateGap::OffsetDiscontinuity),
        Some(_) => {}
    }
    let mut sorted: Vec<(u64, i64)> = obs
        .iter()
        .filter_map(|o| Some((o.t_boot_us?, o.anchor_unix_us?)))
        .collect();
    if sorted.windows(2).any(|w| w[1].0 < w[0].0) {
        return gap(ClockRateGap::DeviceClockNotMonotonic);
    }
    sorted.sort_unstable();
    sorted.dedup();
    let (Some(&(b0, a0)), Some(&(bn, an))) = (sorted.first(), sorted.last()) else {
        return gap(ClockRateGap::FewerThanTwoAnchors);
    };
    if sorted.len() < 2 || bn == b0 {
        return gap(ClockRateGap::FewerThanTwoAnchors);
    }
    let span = i128::from(bn) - i128::from(b0);
    if span < i128::from(cfg.min_span_us) {
        return gap(ClockRateGap::SpanShorterThanDeclaredMinimum);
    }
    let two_u = 2 * i128::from(cfg.anchor_uncertainty_us);
    if span - two_u <= 0 {
        return gap(ClockRateGap::UncertaintyNotSmallerThanSpan);
    }
    let d_offset = (i128::from(an) - i128::from(bn)) - (i128::from(a0) - i128::from(b0));
    let mut low = i128::MAX;
    let mut high = i128::MIN;
    for n in [d_offset - two_u, d_offset + two_u] {
        for d in [span - two_u, span + two_u] {
            let scaled = n * 1_000_000;
            low = low.min(scaled.div_euclid(d));
            high = high.max(-((-scaled).div_euclid(d)));
        }
    }
    let t = i128::from(cfg.max_abs_rate_ppm);
    let class = if low >= -t && high <= t {
        ClockRateClass::WithinDeclaredLimit
    } else if low > t || high < -t {
        ClockRateClass::OutsideDeclaredLimit
    } else {
        ClockRateClass::Indeterminate
    };
    let (Ok(span_us), Ok(low_ppm), Ok(high_ppm)) =
        (i64::try_from(span), i64::try_from(low), i64::try_from(high))
    else {
        return gap(ClockRateGap::OutOfRepresentableRange);
    };
    ClockRateReport {
        outcome: ClockRateOutcome::Observed {
            low_ppm,
            high_ppm,
            class,
        },
        anchors: sorted.len() as u32,
        span_us,
        config: *cfg,
    }
}

#[must_use]
pub fn align_by_start(
    obs: &[Observation],
    reference_start_wall_ms: i64,
    bound_us: i64,
) -> Option<TimeAlignment> {
    let first_boot = obs.iter().filter_map(|o| o.t_boot_us).min()?;
    Some(TimeAlignment {
        basis: ClockBasis::BootRelativeOffsetEstimated,
        offset: Some(OffsetEstimate {
            offset_us: reference_start_wall_ms * 1000 - first_boot as i64,
            bound_us,
            source: OffsetSource::CrossLogEvent,
            anchors: 1,
            discontinuity: false,
        }),
    })
}

#[must_use]
pub fn to_wall_us(al: &TimeAlignment, t_boot_us: u64) -> Option<(i64, i64)> {
    let o = al.offset?;
    Some((t_boot_us as i64 + o.offset_us, o.bound_us))
}

#[must_use]
pub fn promote(obs: &[Observation], al: &TimeAlignment) -> Vec<Observation> {
    let mut out = obs.to_vec();
    for o in &mut out {
        if matches!(
            o.clock_basis,
            ClockBasis::HostReceived | ClockBasis::Unknown
        ) {
            continue;
        }
        o.clock_basis = al.basis;
        o.time_confidence = default_time_confidence(al.basis);
        o.wall_ms = o
            .t_boot_us
            .and_then(|b| to_wall_us(al, b))
            .map(|(w, _)| w / 1000);
    }
    out
}

#[must_use]
pub const fn order(a_us: i64, a_bound: i64, b_us: i64, b_bound: i64) -> OrderRelation {
    order_with_bounds(a_us, a_bound, b_us, b_bound)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ChannelId, FieldValue, SourceRole, observation_digest};

    fn ob(t_boot_us: u64, anchor: Option<i64>, basis: ClockBasis) -> Observation {
        let fields = vec![("x".to_string(), FieldValue::I64(t_boot_us as i64))];
        Observation {
            t_ms: (t_boot_us / 1000) as i64,
            clock_basis: basis,
            time_confidence: 0.2,
            source_role: SourceRole::Fc,
            channel: ChannelId::GpsEkf,
            digest: observation_digest(0, ChannelId::GpsEkf, &fields),
            fields,
            stale: false,
            t_boot_us: Some(t_boot_us),
            anchor_unix_us: anchor,
            wall_ms: None,
        }
    }

    fn paired(n: u64, step_us: u64, b0: u64, a0: i64, ppm: i64) -> Vec<Observation> {
        (0..n)
            .map(|i| {
                let elapsed = (i * step_us) as i64;
                ob(
                    b0 + i * step_us,
                    Some(a0 + elapsed + elapsed * ppm / 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect()
    }

    fn cfg(uncertainty_us: i64, min_span_us: i64, max_abs_rate_ppm: i64) -> ClockRateConfig {
        ClockRateConfig {
            anchor_uncertainty_us: uncertainty_us,
            min_span_us,
            max_abs_rate_ppm,
        }
    }

    fn rate_of(obs: &[Observation], c: &ClockRateConfig) -> ClockRateReport {
        let al = estimate_alignment(obs, ClockBasis::BootRelative);
        estimate_relative_rate(obs, &al, c)
    }

    fn observed(obs: &[Observation], c: &ClockRateConfig) -> (i64, i64, ClockRateClass) {
        match rate_of(obs, c).outcome {
            ClockRateOutcome::Observed {
                low_ppm,
                high_ppm,
                class,
            } => (low_ppm, high_ppm, class),
            other => panic!("expected an observed interval, got {other:?}"),
        }
    }

    #[test]
    fn observed_relative_rate_is_a_signed_outward_rounded_interval_classified_against_the_declared_limit()
     {
        let faster_reference = paired(101, 1_000_000, 37_000_000, 1_000_000_000_000_000, 50);
        let (low, high, _) = observed(&faster_reference, &cfg(100, 60_000_000, 1_000));
        assert_eq!((low, high), (47, 53));
        let slower_reference = paired(101, 1_000_000, 37_000_000, 1_000_000_000_000_000, -50);
        assert_eq!(
            observed(&slower_reference, &cfg(100, 60_000_000, 1_000)).0
                ..=observed(&slower_reference, &cfg(100, 60_000_000, 1_000)).1,
            -53..=-47
        );

        let class = |obs: &[Observation], t: i64| observed(obs, &cfg(100, 60_000_000, t)).2;
        assert_eq!(
            class(&faster_reference, 53),
            ClockRateClass::WithinDeclaredLimit
        );
        assert_eq!(class(&faster_reference, 52), ClockRateClass::Indeterminate);
        assert_eq!(
            class(&slower_reference, 53),
            ClockRateClass::WithinDeclaredLimit
        );
        assert_eq!(class(&slower_reference, 52), ClockRateClass::Indeterminate);
        assert_eq!(class(&faster_reference, 47), ClockRateClass::Indeterminate);
        assert_eq!(
            class(&faster_reference, 46),
            ClockRateClass::OutsideDeclaredLimit
        );
        assert_eq!(
            class(&slower_reference, 46),
            ClockRateClass::OutsideDeclaredLimit
        );
        let r = rate_of(&faster_reference, &cfg(100, 60_000_000, 53));
        assert_eq!(r.anchors, 101);
        assert_eq!(r.span_us, 100_000_000);
        assert_eq!(r.config.max_abs_rate_ppm, 53);
    }

    #[test]
    fn large_epoch_and_ten_thousand_pairs_give_the_same_interval_as_a_small_epoch() {
        let c = cfg(100, 60_000_000, 1_000);
        let near_zero = observed(&paired(101, 1_000_000, 0, 1_000_000, 50), &c);
        let year_2026 = observed(
            &paired(101, 1_000_000, 37_000_000, 1_788_166_800_000_000, 50),
            &c,
        );
        assert_eq!(near_zero, year_2026);

        let many = paired(10_000, 10_000, 37_000_000, 1_788_166_800_000_000, 50);
        let r = rate_of(&many, &c);
        assert_eq!(r.anchors, 10_000);
        assert_eq!(r.span_us, 99_990_000);
        assert_eq!(
            r.outcome,
            ClockRateOutcome::Observed {
                low_ppm: 47,
                high_ppm: 52,
                class: ClockRateClass::WithinDeclaredLimit,
            }
        );
    }

    #[test]
    fn insufficient_basis_is_named_and_never_substituted_by_a_rate() {
        let gap = |obs: &[Observation], c: &ClockRateConfig| match rate_of(obs, c).outcome {
            ClockRateOutcome::InsufficientBasis(g) => g,
            other => panic!("expected a named gap, got {other:?}"),
        };
        let c = cfg(100, 60_000_000, 50);

        let none: Vec<Observation> = (0..10)
            .map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        assert_eq!(gap(&none, &c), ClockRateGap::FewerThanTwoAnchors);
        assert_eq!(
            gap(&paired(1, 1_000_000, 0, 1_000_000_000_000_000, 50), &c),
            ClockRateGap::FewerThanTwoAnchors
        );
        assert_eq!(
            gap(&paired(3, 1_000_000, 0, 1_000_000_000_000_000, 50), &c),
            ClockRateGap::SpanShorterThanDeclaredMinimum
        );
        assert_eq!(
            gap(
                &paired(2, 1_000, 0, 1_000_000_000_000_000, 0),
                &cfg(500, 1_000, 50)
            ),
            ClockRateGap::UncertaintyNotSmallerThanSpan
        );
        let clean = TimeAlignment {
            basis: ClockBasis::BootRelative,
            offset: Some(OffsetEstimate {
                offset_us: 0,
                bound_us: 1,
                source: OffsetSource::InLogAnchor,
                anchors: 2,
                discontinuity: false,
            }),
        };
        let direct = |obs: &[Observation], c: &ClockRateConfig| {
            estimate_relative_rate(obs, &clean, c).outcome
        };
        let huge_span = [
            ob(0, Some(0), ClockBasis::BootRelative),
            ob(u64::MAX, Some(0), ClockBasis::BootRelative),
        ];
        assert_eq!(
            direct(&huge_span, &cfg(1_000, 1, 50)),
            ClockRateOutcome::InsufficientBasis(ClockRateGap::OutOfRepresentableRange)
        );
        let huge_endpoint = [
            ob(0, Some(0), ClockBasis::BootRelative),
            ob(3, Some(30_000_000_000_003), ClockBasis::BootRelative),
        ];
        assert_eq!(
            direct(&huge_endpoint, &cfg(1, 1, 50)),
            ClockRateOutcome::InsufficientBasis(ClockRateGap::OutOfRepresentableRange)
        );
        let representable = [
            ob(0, Some(0), ClockBasis::BootRelative),
            ob(3, Some(3_000_000_003), ClockBasis::BootRelative),
        ];
        assert!(matches!(
            direct(&representable, &cfg(1, 1, 50)),
            ClockRateOutcome::Observed { .. }
        ));
        let full_span = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 50);
        assert_eq!(
            gap(&full_span, &cfg(i64::MAX, 1, 50)),
            ClockRateGap::UncertaintyNotSmallerThanSpan
        );
        assert_eq!(
            gap(&full_span, &cfg(100, i64::MAX, 50)),
            ClockRateGap::SpanShorterThanDeclaredMinimum
        );
        assert_eq!(
            observed(&full_span, &cfg(100, 60_000_000, i64::MAX)),
            (47, 53, ClockRateClass::WithinDeclaredLimit)
        );
        let mut jump = paired(11, 1_000_000, 0, 1_000_000_000_000_000, 0);
        jump.extend((11..22).map(|i| {
            ob(
                i * 1_000_000,
                Some(1_000_000_000_000_000 + i as i64 * 1_000_000 + 7_000_000),
                ClockBasis::BootRelative,
            )
        }));
        assert_eq!(gap(&jump, &c), ClockRateGap::OffsetDiscontinuity);
        let mut reset = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 0);
        reset.push(ob(
            50_500_000,
            Some(1_000_000_000_000_000 + 50_500_000),
            ClockBasis::BootRelative,
        ));
        let al = estimate_alignment(&reset, ClockBasis::BootRelative);
        assert!(
            !al.offset.expect("offset").discontinuity,
            "the existing flag does not see this reset"
        );
        assert_eq!(
            estimate_relative_rate(&reset, &al, &c).outcome,
            ClockRateOutcome::InsufficientBasis(ClockRateGap::DeviceClockNotMonotonic)
        );
        let cross = align_by_start(&none, 1_788_166_800_000, 10_000_000).expect("aligned");
        assert_eq!(
            estimate_relative_rate(&none, &cross, &c).outcome,
            ClockRateOutcome::InsufficientBasis(ClockRateGap::OffsetSourceNotInLogAnchor)
        );
        let host = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 50);
        let host_al = estimate_alignment(&host, ClockBasis::HostReceived);
        assert_eq!(
            estimate_relative_rate(&host, &host_al, &c).outcome,
            ClockRateOutcome::InsufficientBasis(ClockRateGap::ClockBasisNotDeviceClock)
        );
    }

    #[test]
    fn the_assumed_drift_bound_keeps_its_meaning_and_the_observed_interval_does_not_touch_it() {
        assert!((DRIFT_PPM_BOUND - 100.0).abs() < f64::EPSILON);
        let obs = paired(101, 1_000_000, 0, 1_000_000_000_000_000, 50);
        let before = estimate_alignment(&obs, ClockBasis::BootRelative);
        let _ = estimate_relative_rate(&obs, &before, &cfg(100, 60_000_000, 1_000));
        let after = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(before, after);
        assert_eq!(after.basis, ClockBasis::GpsLocked);
        assert_eq!(after.offset.expect("offset").bound_us, 2_500);
    }

    #[test]
    fn anchors_to_the_end_give_gps_locked_and_offset() {
        let obs: Vec<Observation> = (0..100)
            .map(|i| {
                ob(
                    37_000_000 + i * 1_000_000,
                    Some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.basis, ClockBasis::GpsLocked);
        let o = al.offset.expect("offset");
        assert_eq!(o.offset_us, 1_000_000_000_000_000 - 37_000_000);
        assert!(!o.discontinuity);
        let p = promote(&obs, &al);
        assert_eq!(p[0].wall_ms, Some(1_000_000_000_000));
        assert_eq!(p[0].clock_basis, ClockBasis::GpsLocked);
    }

    #[test]
    fn anchors_stopping_midway_give_offset_estimated_with_wider_bound() {
        let mut obs: Vec<Observation> = (0..50)
            .map(|i| {
                ob(
                    i * 1_000_000,
                    Some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        obs.extend((50..100).map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative)));
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated);
        assert_eq!(al.offset.expect("offset").bound_us, 5_000);
    }

    #[test]
    fn align_by_start_gives_cross_log_offset_with_wide_bound_and_none_without_boot_time() {
        let obs: Vec<Observation> = (0..10)
            .map(|i| ob(8_000_000 + i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        let al = align_by_start(&obs, 1_788_166_800_000, 10_000_000).expect("aligned");
        assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated);
        let o = al.offset.expect("offset");
        assert_eq!(o.source, OffsetSource::CrossLogEvent);
        assert_eq!(o.offset_us, 1_788_166_800_000_000 - 8_000_000);
        assert_eq!(o.bound_us, 10_000_000);
        assert_eq!(
            to_wall_us(&al, 8_000_000),
            Some((1_788_166_800_000_000, 10_000_000))
        );
        let mut no_boot = ob(0, None, ClockBasis::BootRelative);
        no_boot.t_boot_us = None;
        assert!(align_by_start(&[no_boot], 0, 1).is_none());
    }

    #[test]
    fn single_anchor_bound_covers_whole_log_and_inner_gap_demotes_to_estimated() {
        let mut obs: Vec<Observation> = (0..100)
            .map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        obs[0].anchor_unix_us = Some(1_000_000_000_000_000);
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.offset.expect("o").bound_us, 19_800);
        assert_eq!(al.basis, ClockBasis::BootRelativeOffsetEstimated);
        let obs: Vec<Observation> = (0..100)
            .map(|i| {
                let anchored = i < 30 || i >= 60;
                ob(
                    i * 1_000_000,
                    anchored.then_some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        assert_eq!(
            estimate_alignment(&obs, ClockBasis::BootRelative).basis,
            ClockBasis::BootRelativeOffsetEstimated
        );
    }

    #[test]
    fn no_anchor_is_boot_relative_and_jump_is_gps_suspect_and_order_unknown_when_bands_overlap() {
        let obs: Vec<Observation> = (0..10)
            .map(|i| ob(i * 1_000_000, None, ClockBasis::BootRelative))
            .collect();
        assert_eq!(
            estimate_alignment(&obs, ClockBasis::BootRelative).basis,
            ClockBasis::BootRelative
        );
        let mut obs: Vec<Observation> = (0..10)
            .map(|i| {
                ob(
                    i * 1_000_000,
                    Some(1_000_000_000_000_000 + i as i64 * 1_000_000),
                    ClockBasis::BootRelative,
                )
            })
            .collect();
        obs.extend((10..20).map(|i| {
            ob(
                i * 1_000_000,
                Some(1_000_000_000_000_000 + i as i64 * 1_000_000 + 7_000_000),
                ClockBasis::BootRelative,
            )
        }));
        let al = estimate_alignment(&obs, ClockBasis::BootRelative);
        assert_eq!(al.basis, ClockBasis::GpsSuspect);
        assert!(al.offset.expect("o").discontinuity);
        assert!(al.offset.expect("o").bound_us >= 7_000_000);
        assert_eq!(
            order(0, 10_000_000, 5_000_000, 10_000_000),
            OrderRelation::OrderUnknown
        );
        assert_eq!(order(0, 1_000, 5_000_000, 1_000), OrderRelation::Before);
    }
}
