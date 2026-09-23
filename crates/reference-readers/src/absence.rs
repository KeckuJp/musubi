use musubi_reference_types::{
    ClockBasis, DigestRef, FamilyProfile, Mark, MarkStatus, NegativeObservation, Subject,
    join_reason_tokens,
};

use crate::{Observation, default_time_confidence, value_digest};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GapEnd {
    NextObservation,
    NextObservationClockUnresolved,
    OpenEnd,
}

impl GapEnd {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::NextObservation => "next_observation",
            Self::NextObservationClockUnresolved => "next_observation_clock_unresolved",
            Self::OpenEnd => "open_end",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GapBounds {
    pub end: GapEnd,
    pub next_good: Option<DigestRef>,
    pub next_good_ms: Option<i64>,
    pub next_good_clock_basis: Option<ClockBasis>,
}

#[must_use]
pub fn apply_frozen_rule(
    obs: &[Observation],
    channel: musubi_reference_types::ChannelId,
    frozen_ms: u64,
) -> Vec<Observation> {
    apply_frozen_fields_rule(obs, channel, frozen_ms, None)
}

fn selected_values(o: &Observation, names: &[String]) -> Option<Vec<(String, crate::FieldValue)>> {
    names
        .iter()
        .map(|name| {
            let mut matches = o.fields.iter().filter(|(key, _)| key == name);
            let value = matches.next()?.1.clone();
            if matches.next().is_some()
                || matches!(value, crate::FieldValue::Blank)
                || matches!(value, crate::FieldValue::F64(n) if !n.is_finite())
            {
                return None;
            }
            Some((name.clone(), value))
        })
        .collect()
}

#[must_use]
pub fn apply_frozen_fields_rule(
    obs: &[Observation],
    channel: musubi_reference_types::ChannelId,
    frozen_ms: u64,
    names: Option<&[String]>,
) -> Vec<Observation> {
    let mut out: Vec<Observation> = obs.to_vec();
    let mut idx: Vec<usize> = (0..out.len())
        .filter(|&i| out[i].channel == channel)
        .collect();
    idx.sort_by_key(|&i| out[i].t_ms);
    let mut run_start_t: Option<i64> = None;
    let mut run_digest = None;
    for i in idx {
        let d = if let Some(names) = names {
            let Some(values) = selected_values(&out[i], names).filter(|v| !v.is_empty()) else {
                run_start_t = None;
                run_digest = None;
                continue;
            };
            crate::observation_digest(0, channel, &values)
        } else {
            value_digest(&out[i].fields)
        };
        if run_digest == Some(d) {
            if let Some(start) = run_start_t
                && out[i].t_ms.saturating_sub(start) >= i64::try_from(frozen_ms).unwrap_or(i64::MAX)
            {
                out[i].stale = true;
            }
        } else {
            run_digest = Some(d);
            run_start_t = Some(out[i].t_ms);
        }
    }
    out
}

#[must_use]
pub fn detect_absences(
    profile: &FamilyProfile,
    asset_id: &str,
    obs: &[Observation],
    window_start_ms: i64,
    window_end_ms: i64,
) -> Vec<NegativeObservation> {
    detect_absences_with_bounds(profile, asset_id, obs, window_start_ms, window_end_ms)
        .into_iter()
        .map(|(neg, _)| neg)
        .collect()
}

#[must_use]
pub fn detect_absences_with_bounds(
    profile: &FamilyProfile,
    asset_id: &str,
    obs: &[Observation],
    window_start_ms: i64,
    window_end_ms: i64,
) -> Vec<(NegativeObservation, GapBounds)> {
    detect_absences_in_declared_window(
        profile,
        asset_id,
        obs,
        (window_start_ms, window_end_ms),
        (window_start_ms, window_end_ms),
    )
}

#[must_use]
pub fn detect_absences_in_declared_window(
    profile: &FamilyProfile,
    asset_id: &str,
    obs: &[Observation],
    scan: (i64, i64),
    declared: (i64, i64),
) -> Vec<(NegativeObservation, GapBounds)> {
    let mut out = Vec::new();
    for exp in &profile.expectations {
        let (window_start_ms, window_end_ms) = if exp.declared_window_only {
            declared
        } else {
            scan
        };
        let windowed: Vec<Observation>;
        let obs: &[Observation] = if exp.declared_window_only {
            windowed = obs
                .iter()
                .filter(|o| o.t_ms >= window_start_ms && o.t_ms <= window_end_ms)
                .cloned()
                .collect();
            &windowed
        } else {
            obs
        };
        let threshold = i64::try_from(exp.cadence_ms.saturating_mul(u64::from(exp.grace_k)))
            .unwrap_or(i64::MAX);
        let frozen_applied;
        let src: &[Observation] = match exp.frozen_ms {
            Some(f) => {
                frozen_applied =
                    apply_frozen_fields_rule(obs, exp.channel, f, exp.frozen_fields.as_deref());
                &frozen_applied
            }
            None => obs,
        };
        let admitting: Option<&[String]> = exp
            .required_fields
            .as_deref()
            .or(exp.frozen_fields.as_deref());
        let mut good: Vec<&Observation> = src
            .iter()
            .filter(|o| o.channel == exp.channel && !o.stale)
            .filter(|o| admitting.is_none_or(|names| selected_values(o, names).is_some()))
            .collect();
        good.sort_by_key(|o| o.t_ms);
        let basis = good
            .first()
            .map_or(profile.default_clock_basis, |o| o.clock_basis);
        let tc_min = good.iter().map(|o| o.time_confidence).fold(
            default_time_confidence(profile.default_clock_basis),
            f32::min,
        );
        let content = |since: i64, until: i64| {
            exp.required_fields
                .as_deref()
                .map(|names| gap_content(src, exp.channel, names, since, until))
        };
        let make = |since: i64, last: Option<&Observation>, content: Option<GapContent>| {
            NegativeObservation {
                subject: Subject {
                    family: profile.family,
                    asset_id: asset_id.to_string(),
                    channel: exp.channel,
                },
                expectation_id: format!("{}@{}", exp.expectation_id, profile.version),
                absent_since_ms: since,
                expected_by_ms: since.saturating_add(threshold),
                last_good: last.map(|o| o.digest),
                clock_basis: basis,
                time_confidence_min: tc_min,
                mark: absence_mark(basis, profile, content),
            }
        };
        let bounds = |next: Option<&Observation>| {
            let Some(o) = next else {
                return GapBounds {
                    end: GapEnd::OpenEnd,
                    next_good: None,
                    next_good_ms: None,
                    next_good_clock_basis: None,
                };
            };
            let comparable = o.clock_basis == basis && o.clock_basis != ClockBasis::Unknown;
            GapBounds {
                end: if comparable {
                    GapEnd::NextObservation
                } else {
                    GapEnd::NextObservationClockUnresolved
                },
                next_good: Some(o.digest),
                next_good_ms: comparable.then_some(o.t_ms),
                next_good_clock_basis: Some(o.clock_basis),
            }
        };
        let mut prev: Option<&Observation> = None;
        for o in &good {
            let since = prev.map_or(window_start_ms, |p| p.t_ms);
            if o.t_ms - since > threshold {
                out.push((make(since, prev, content(since, o.t_ms)), bounds(Some(o))));
            }
            prev = Some(o);
        }
        let since = prev.map_or(window_start_ms, |p| p.t_ms);
        if window_end_ms - since > threshold {
            out.push((
                make(since, prev, content(since, window_end_ms)),
                bounds(None),
            ));
        }
    }
    out
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GapContent {
    NoRecordInGap,
    RequiredFieldAbsent,
    RecordMarkedStale,
}

impl GapContent {
    #[must_use]
    pub const fn as_label(self) -> &'static str {
        match self {
            Self::NoRecordInGap => "gap-content:no-record-in-gap",
            Self::RequiredFieldAbsent => "gap-content:required-field-absent",
            Self::RecordMarkedStale => "gap-content:record-marked-stale",
        }
    }
}

fn gap_content(
    src: &[Observation],
    channel: musubi_reference_types::ChannelId,
    names: &[String],
    since: i64,
    until: i64,
) -> GapContent {
    let in_gap = || {
        src.iter()
            .filter(move |o| o.channel == channel && o.t_ms >= since && o.t_ms < until)
    };
    if in_gap().any(|o| !o.stale && selected_values(o, names).is_none()) {
        GapContent::RequiredFieldAbsent
    } else if in_gap().any(|o| o.stale) {
        GapContent::RecordMarkedStale
    } else {
        GapContent::NoRecordInGap
    }
}

fn absence_mark(basis: ClockBasis, profile: &FamilyProfile, content: Option<GapContent>) -> Mark {
    let mut tokens = vec!["absence-expected-cadence".to_string(), basis.reason_token()];
    if let Some(c) = content {
        tokens.push(c.as_label().to_string());
    }
    let borrowed: Vec<&str> = tokens.iter().map(String::as_str).collect();
    Mark {
        status: MarkStatus::Degraded,
        reason_code: join_reason_tokens(&borrowed),
        provenance: vec![format!(
            "musubi-reference-readers/{}@{}",
            profile.profile_id, profile.version
        )],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{FieldValue, observation_digest};
    use musubi_reference_types::{ChannelId, ExpectationModel, Family, FieldMapping, SourceRole};

    fn profile() -> FamilyProfile {
        FamilyProfile {
            profile_id: "t".into(),
            version: "1".into(),
            family: Family::Ugv,
            source_role: SourceRole::Gcs,
            format: "mavlink_tlog".into(),
            extensions: vec!["tlog".into()],
            fields: FieldMapping::default(),
            field_units: std::collections::BTreeMap::new(),
            channels: vec![ChannelId::Heartbeat],
            expectations: vec![ExpectationModel {
                expectation_id: "hb".into(),
                channel: ChannelId::Heartbeat,
                cadence_ms: 1000,
                grace_k: 3,
                frozen_ms: None,
                frozen_fields: None,
                required_fields: None,
                declared_window_only: false,
            }],
            default_clock_basis: ClockBasis::HostReceived,
            declared_platform_domain: None,
            origin: "public".into(),
            declared_clock_rate: None,
            declared_sender: None,
        }
    }

    fn ob(t_ms: i64, stale: bool) -> Observation {
        ob_v(t_ms, stale, t_ms)
    }

    fn ob_on(t_ms: i64, channel: ChannelId, clock_basis: ClockBasis) -> Observation {
        let fields = vec![("x".to_string(), FieldValue::I64(t_ms))];
        Observation {
            clock_basis,
            channel,
            digest: observation_digest(t_ms, channel, &fields),
            fields,
            ..ob(t_ms, false)
        }
    }

    fn ob_v(t_ms: i64, stale: bool, value: i64) -> Observation {
        let fields = vec![("x".to_string(), FieldValue::I64(value))];
        Observation {
            t_ms,
            clock_basis: ClockBasis::HostReceived,
            time_confidence: 0.2,
            source_role: SourceRole::Gcs,
            channel: ChannelId::Heartbeat,
            digest: observation_digest(t_ms, ChannelId::Heartbeat, &fields),
            fields,
            stale,
            t_boot_us: None,
            anchor_unix_us: None,
            wall_ms: Some(t_ms),
        }
    }

    #[test]
    fn gap_longer_than_cadence_times_k_is_an_absence() {
        let obs: Vec<Observation> = [0, 1000, 2000, 9000, 10_000]
            .iter()
            .map(|&t| ob(t, false))
            .collect();
        let neg = detect_absences(&profile(), "a", &obs, 0, 10_000);
        assert_eq!(neg.len(), 1);
        assert_eq!(neg[0].absent_since_ms, 2000);
        assert_eq!(neg[0].expected_by_ms, 5000);
        assert_eq!(neg[0].last_good, Some(obs[2].digest));
        assert_eq!(
            neg[0].mark.reason_code,
            "absence-expected-cadence|clock-basis:host_received"
        );
        assert_eq!(neg[0].expectation_id, "hb@1");
    }

    #[test]
    fn stale_observations_do_not_count_as_good_and_tail_gap_is_reported() {
        let obs: Vec<Observation> = (0..=10)
            .map(|i| ob(i * 1000, (3..=10).contains(&i)))
            .collect();
        let neg = detect_absences(&profile(), "a", &obs, 0, 10_000);
        assert_eq!(neg.len(), 1);
        assert_eq!(neg[0].absent_since_ms, 2000);
    }

    #[test]
    fn never_observed_channel_is_absence_without_last_good() {
        let neg = detect_absences(&profile(), "a", &[], 0, 10_000);
        assert_eq!(neg.len(), 1);
        assert!(neg[0].last_good.is_none());
        assert_eq!(neg[0].absent_since_ms, 0);
        let (_, bounds) = &detect_absences_with_bounds(&profile(), "a", &[], 0, 10_000)[0];
        assert_eq!(bounds.end, GapEnd::OpenEnd);
        assert!(bounds.next_good.is_none() && bounds.next_good_clock_basis.is_none());
    }

    #[test]
    fn two_gaps_carry_the_next_admitted_observation_and_the_trailing_gap_is_open() {
        let obs: Vec<Observation> = [0, 1000, 2000, 9000, 10_000]
            .iter()
            .map(|&t| ob(t, false))
            .collect();
        let pairs = detect_absences_with_bounds(&profile(), "a", &obs, 0, 20_000);
        assert_eq!(pairs.len(), 2, "{pairs:?}");
        let (first, first_bounds) = &pairs[0];
        assert_eq!(
            (first.absent_since_ms, first.expected_by_ms, first.last_good),
            (2000, 5000, Some(obs[2].digest))
        );
        assert_eq!(first_bounds.end, GapEnd::NextObservation);
        assert_eq!(first_bounds.next_good, Some(obs[3].digest));
        assert_eq!(first_bounds.next_good_ms, Some(9000));
        assert_eq!(
            first_bounds.next_good_clock_basis,
            Some(ClockBasis::HostReceived)
        );
        let (tail, tail_bounds) = &pairs[1];
        assert_eq!(tail.absent_since_ms, 10_000);
        assert_eq!(tail.last_good, Some(obs[4].digest));
        assert_eq!(tail_bounds.end, GapEnd::OpenEnd);
        assert!(tail_bounds.next_good.is_none() && tail_bounds.next_good_ms.is_none());
        assert_eq!(
            detect_absences(&profile(), "a", &obs, 0, 20_000),
            pairs.iter().map(|(n, _)| n.clone()).collect::<Vec<_>>()
        );
    }

    #[test]
    fn another_channel_or_a_stale_row_at_the_same_time_does_not_close_a_gap() {
        let mut obs: Vec<Observation> = [0, 1000, 2000, 9000]
            .iter()
            .map(|&t| ob(t, false))
            .collect();
        obs.push(ob_on(5000, ChannelId::LinkStats, ClockBasis::HostReceived));
        obs.push(ob(6000, true)); // 同 channel だが stale＝admitted ではない
        let pairs = detect_absences_with_bounds(&profile(), "a", &obs, 0, 9000);
        assert_eq!(pairs.len(), 1, "{pairs:?}");
        let (neg, bounds) = &pairs[0];
        assert_eq!(neg.absent_since_ms, 2000);
        assert_eq!(bounds.next_good, Some(obs[3].digest));
        assert_eq!(bounds.next_good_ms, Some(9000)); // 5000 でも 6000 でもない
        assert_ne!(bounds.next_good, Some(obs[4].digest));
        assert_ne!(bounds.next_good, Some(obs[5].digest));
        let other = detect_absences_with_bounds(&profile(), "b", &obs, 0, 9000);
        assert_eq!(other[0].0.subject.asset_id, "b");
        assert_ne!(other[0].0.subject.asset_id, neg.subject.asset_id);
        assert_eq!(other[0].1, *bounds);
    }

    #[test]
    fn unknown_or_foreign_clock_leaves_the_end_unresolved_without_a_time() {
        let mut obs: Vec<Observation> = [0, 1000, 2000].iter().map(|&t| ob(t, false)).collect();
        obs.push(ob_on(9000, ChannelId::Heartbeat, ClockBasis::BootRelative));
        let pairs = detect_absences_with_bounds(&profile(), "a", &obs, 0, 9000);
        let (neg, bounds) = &pairs[0];
        assert_eq!(neg.absent_since_ms, 2000);
        assert_eq!(bounds.end, GapEnd::NextObservationClockUnresolved);
        assert_eq!(bounds.next_good, Some(obs[3].digest));
        assert!(bounds.next_good_ms.is_none());
        assert_eq!(bounds.next_good_clock_basis, Some(ClockBasis::BootRelative));
        let unknown: Vec<Observation> = [0, 1000, 2000, 9000]
            .iter()
            .map(|&t| ob_on(t, ChannelId::Heartbeat, ClockBasis::Unknown))
            .collect();
        let (_, u) = &detect_absences_with_bounds(&profile(), "a", &unknown, 0, 9000)[0];
        assert_eq!(u.end, GapEnd::NextObservationClockUnresolved);
        assert!(u.next_good.is_some() && u.next_good_ms.is_none());
    }

    #[test]
    fn within_cadence_yields_nothing() {
        let obs: Vec<Observation> = (0..=10).map(|i| ob(i * 1000, false)).collect();
        assert!(detect_absences(&profile(), "a", &obs, 0, 10_000).is_empty());
    }

    #[test]
    fn a_separate_authored_sequence_reads_the_same_way() {
        let mut p = profile();
        p.expectations[0].cadence_ms = 500;
        p.expectations[0].grace_k = 4; // threshold 2000
        let mut obs: Vec<Observation> = [0, 500, 1000, 5000, 5500]
            .iter()
            .map(|&t| ob(t, false))
            .collect();
        obs.push(ob(3000, true)); // stale
        obs.push(ob_on(3500, ChannelId::LinkStats, ClockBasis::HostReceived)); // 別 channel
        let pairs = detect_absences_with_bounds(&p, "a", &obs, 0, 12_000);
        assert_eq!(pairs.len(), 2, "{pairs:?}");
        assert_eq!(pairs[0].0.absent_since_ms, 1000);
        assert_eq!(pairs[0].0.expected_by_ms, 3000);
        assert_eq!(pairs[0].1.end, GapEnd::NextObservation);
        assert_eq!(pairs[0].1.next_good, Some(obs[3].digest));
        assert_eq!(pairs[0].1.next_good_ms, Some(5000));
        assert_eq!(pairs[1].0.absent_since_ms, 5500);
        assert_eq!(pairs[1].1.end, GapEnd::OpenEnd);
        assert!(pairs[1].1.next_good.is_none());
    }

    #[test]
    fn frozen_values_become_stale_after_frozen_ms_and_yield_absence() {
        let mut p = profile();
        p.expectations[0].frozen_ms = Some(3_000);
        let obs: Vec<Observation> = (0..=20)
            .map(|i| ob_v(i * 1000, false, if i < 5 { i } else { 77 }))
            .collect();
        let frozen = apply_frozen_rule(&obs, ChannelId::Heartbeat, 3_000);
        assert!(!frozen[7].stale && frozen[8].stale && frozen[20].stale);
        let neg = detect_absences(&p, "a", &obs, 0, 20_000);
        assert_eq!(neg.len(), 1, "{neg:?}");
        assert_eq!(neg[0].absent_since_ms, 7_000);
        assert!(detect_absences(&profile(), "a", &obs, 0, 20_000).is_empty());
    }
}
