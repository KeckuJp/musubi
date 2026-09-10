//! Oracles for the pack.
//!
//! Two things have to be true for a reference pack to be worth anything. It has to be
//! reproducible, or it cannot be a golden. And it has to contain a mixed population --
//! usable observations, degraded ones and refused ones -- or a consumer running against it
//! proves only that the happy path works.

use crate::consumers::{
    HIGH_MOTOR_TEMP_C, LOW_BATTERY_PCT, MarkLedger, disaster_view, farm_view, industrial_view,
    inspection_view, logistics_view, maintenance_view,
};
use crate::pack::ReferencePack;
use crate::sources::SourceKind;
use crate::synth::{FIELD_LAT, FIELD_LON, INGEST_RECEIVED_AT_MS};
use musubi_core::verify_digest;
use musubi_types::{ComObject, MarkStatus};

fn pack() -> ReferencePack {
    ReferencePack::default()
}

#[test]
fn the_pack_is_reproducible_from_its_seed() {
    assert_eq!(pack(), pack(), "the default pack is a golden");
    assert_eq!(pack().wire_lines(), pack().wire_lines());
}

#[test]
fn a_different_seed_gives_a_different_pack() {
    // Non-vacuity for the test above.
    let a = ReferencePack::new(1, 4);
    let b = ReferencePack::new(2, 4);
    assert_ne!(a.wire_lines(), b.wire_lines());
}

#[test]
fn every_source_kind_is_present_and_carries_its_share() {
    let p = pack();
    for kind in SourceKind::ALL {
        assert_eq!(
            p.from_source(kind).len(),
            p.per_source,
            "{kind} is missing observations"
        );
    }
    assert_eq!(p.observations.len(), SourceKind::ALL.len() * p.per_source);
}

#[test]
fn every_observation_normalizes_into_sealed_evidence() {
    for e in pack().evidence() {
        assert!(e.content_digest.is_some(), "normalize seals");
        assert_eq!(
            verify_digest(&e).status,
            MarkStatus::Ok,
            "a sealed envelope verifies against its own content"
        );
    }
}

#[test]
fn no_observation_fires_a_status_an_adapter_may_not_emit() {
    // A positive allow-list, so a new reserved status cannot quietly widen what the pack
    // is permitted to produce.
    for e in pack().evidence() {
        assert!(
            matches!(e.claim.mark.status, MarkStatus::Ok | MarkStatus::Degraded),
            "{:?} is not something a normalizing source may emit",
            e.claim.mark.status
        );
    }
}

#[test]
fn the_population_is_mixed_not_uniformly_clean() {
    // The point of the pack. If every observation were OK, a consumer test running against it
    // would demonstrate nothing about degradation handling.
    let ledger = MarkLedger::of(&pack().evidence());
    assert!(ledger.ok > 0, "no usable observations: {ledger:?}");
    assert!(ledger.degraded > 0, "no degraded observations: {ledger:?}");
    assert_eq!(
        ledger.reserved, 0,
        "no reserved status may appear: {ledger:?}"
    );
}

#[test]
fn every_reason_code_and_provenance_token_is_splittable() {
    // ';' is the top-level separator on the reading side.
    for e in pack().evidence() {
        assert!(!e.claim.mark.reason_code.contains(';'));
        for token in &e.claim.mark.provenance {
            assert!(!token.contains(';'), "{token:?}");
        }
    }
}

#[test]
fn every_position_is_inside_the_stated_rectangle() {
    // The provenance claim in the crate documentation, checked: positions come from the
    // rectangle written down in synth, and from nowhere else.
    for e in pack().evidence() {
        let ComObject::PlatformState(ps) = &e.observation else {
            panic!("expected a platform state")
        };
        let pos = ps
            .position
            .as_ref()
            .expect("every source reports a position");
        assert!(
            (FIELD_LAT.0..=FIELD_LAT.1).contains(&pos.lat_deg),
            "{pos:?}"
        );
        assert!(
            (FIELD_LON.0..=FIELD_LON.1).contains(&pos.lon_deg),
            "{pos:?}"
        );
    }
}

#[test]
fn the_ingest_time_is_transcribed_not_replaced() {
    for e in pack().evidence() {
        let ComObject::PlatformState(ps) = &e.observation else {
            panic!("expected a platform state")
        };
        assert_eq!(ps.timestamps.received_at, INGEST_RECEIVED_AT_MS);
        assert_ne!(ps.timestamps.received_at, 0);
    }
}

#[test]
fn stationary_sources_hold_one_position_and_moving_ones_do_not() {
    let p = pack();
    for kind in SourceKind::ALL {
        let obs = p.from_source(kind);
        let distinct: std::collections::BTreeSet<String> = obs
            .iter()
            .map(|o| format!("{:.6},{:.6}", o.lat, o.lon))
            .collect();
        if kind.is_stationary() {
            assert_eq!(distinct.len(), 1, "{kind} moved");
        } else {
            assert!(distinct.len() > 1, "{kind} never moved");
        }
    }
}

#[test]
fn some_observations_carry_no_absolute_time_and_say_so() {
    let view = disaster_view(&pack().evidence());
    assert!(
        view.without_absolute_time > 0,
        "the pack never exercises the time-suspect path"
    );
    assert_eq!(view.sealed, view.ledger.total(), "everything is sealed");
    assert_eq!(view.unsealed, 0);
}

#[test]
fn the_adversarial_fixtures_are_refused_with_a_reason_and_a_source() {
    let p = pack();
    assert!(!p.rejections.is_empty());
    for r in &p.rejections {
        let err = r.to_error();
        assert_eq!(err.mark.status, MarkStatus::Invalid, "{:?}", r.wire);
        assert_eq!(err.source_id, r.source_id);
        assert!(!err.mark.reason_code.is_empty());
        assert!(
            err.mark.provenance.iter().any(|t| t.starts_with("source:")),
            "a refusal that loses its source cannot be acted on: {:?}",
            err.mark.provenance
        );
    }
}

#[test]
fn every_consumer_accounts_for_every_envelope_it_was_given() {
    // The one property all six share: they summarise without dropping.
    let evidence = pack().evidence();
    let n = evidence.len();
    assert_eq!(farm_view(&evidence).ledger.total(), n);
    assert_eq!(maintenance_view(&evidence).ledger.total(), n);
    assert_eq!(inspection_view(&evidence).ledger.total(), n);
    assert_eq!(logistics_view(&evidence).ledger.total(), n);
    assert_eq!(disaster_view(&evidence).ledger.total(), n);
    assert_eq!(industrial_view(&evidence).ledger.total(), n);
}

#[test]
fn the_farm_view_reports_one_last_position_per_platform_inside_its_own_bounds() {
    let evidence = pack().evidence();
    let view = farm_view(&evidence);
    assert_eq!(
        view.last_position_by_platform.len(),
        SourceKind::ALL.len(),
        "one platform per source kind"
    );
    let (min_lat, min_lon, max_lat, max_lon) = view.bounds.expect("positions were seen");
    for (lat, lon) in view.last_position_by_platform.values() {
        assert!((min_lat..=max_lat).contains(lat));
        assert!((min_lon..=max_lon).contains(lon));
    }
}

#[test]
fn the_inspection_view_surfaces_exactly_what_is_not_plainly_usable() {
    let evidence = pack().evidence();
    let view = inspection_view(&evidence);
    assert_eq!(
        view.follow_up.len(),
        view.ledger.degraded + view.ledger.invalid + view.ledger.reserved,
        "everything not OK is surfaced, and nothing else is"
    );
    for (_, status, reason) in &view.follow_up {
        assert_ne!(*status, MarkStatus::Ok);
        assert!(
            !reason.is_empty(),
            "a follow-up with no reason is not actionable"
        );
    }
}

#[test]
fn the_logistics_view_keeps_an_undeclared_domain_undeclared() {
    let evidence = pack().evidence();
    let view = logistics_view(&evidence);
    assert_eq!(view.platforms.len(), SourceKind::ALL.len());
    assert_eq!(
        view.platforms_by_domain.values().sum::<usize>(),
        view.platforms.len(),
        "every platform lands in exactly one domain bucket"
    );
    assert!(
        view.platforms_by_domain.contains_key("Unknown"),
        "the health source declares no domain, and the view must not invent one: {:?}",
        view.platforms_by_domain
    );
}

#[test]
fn the_maintenance_view_only_flags_readings_past_its_stated_thresholds() {
    let evidence = pack().evidence();
    let view = maintenance_view(&evidence);
    for pct in view.low_battery.values() {
        assert!(*pct <= LOW_BATTERY_PCT, "{pct}");
    }
    for c in view.high_motor_temp.values() {
        assert!(*c >= HIGH_MOTOR_TEMP_C, "{c}");
    }
    assert!(
        !view.reasons.is_empty(),
        "the pack carries degradations, so the reason tally cannot be empty"
    );
}

#[test]
fn the_industrial_view_separates_no_data_from_bad_data() {
    let evidence = pack().evidence();
    let view = industrial_view(&evidence);
    assert!(
        view.usable_share("no-such-platform").is_none(),
        "a platform never seen is not a platform with a zero score"
    );
    for platform in view.per_platform.keys() {
        let share = view.usable_share(platform).expect("seen");
        assert!((0.0..=1.0).contains(&share), "{platform}: {share}");
    }
}

#[test]
fn a_pack_with_one_observation_per_source_still_holds_together() {
    // A boundary the default pack cannot exercise: the smallest pack that is still a pack.
    let p = ReferencePack::new(99, 1);
    let evidence = p.evidence();
    assert_eq!(evidence.len(), SourceKind::ALL.len());
    assert_eq!(farm_view(&evidence).ledger.total(), evidence.len());
}
