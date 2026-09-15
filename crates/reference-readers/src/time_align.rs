use musubi_reference_types::{
    ClockBasis, OffsetEstimate, OffsetSource, OrderRelation, TimeAlignment, order_with_bounds,
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
