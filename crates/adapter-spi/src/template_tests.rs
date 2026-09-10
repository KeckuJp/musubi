//! Acceptance oracle for the worked adapter. These tests are the credential.
//!
//! They are deliberately non-tautological: the zero-mismap golden asserts the normalized COM
//! field by field against independently known numbers, so a wrong mapping -- swapped latitude
//! and longitude, a wrong scale, a dropped field -- must fail. When you copy the template,
//! these are the tests you keep and re-point at your own golden frame.
//!
//! What they cover:
//! - zero-mismap golden: a fixed wire maps field by field to an expected PlatformState.
//! - read-only: the manifest declares zero egress and polling never opens a path.
//! - no silent drop: malformed input yields an INVALID MARK, never a silent `None`.
//! - the reserved MARK values are never fired by this adapter.
//! - quarantine runs before parsing, and the bound itself is exclusive at the edge.
//! - sealing round-trips, and a one-bit change to the sealed envelope is detected.

use crate::spi::{QueuedTap, ReadOnlyTap};
use crate::template::{ADAPTER_SLUG, MAX_WIRE_LINE_BYTES, MyDeviceNormalizer, parse_my_device};
use musubi_core::quarantine::QUARANTINE_REASON_CODE;
use musubi_core::{Normalizer, RawObservation, seal_digest, verify_digest};
use musubi_types::{ComObject, MarkStatus};

/// The reception time the ingest layer stamps. Non-zero on purpose: a zero here is what a
/// normalizer that hard-codes its own reception time would produce, and these tests must be
/// able to tell the two apart.
const RECEIVED_AT_MS: i64 = 1_726_371_737_123;

/// The golden wire. Independently known values, written out here so that the assertions below
/// compare against something other than the code under test.
const GOLDEN_WIRE: &str = "35.4215 139.6423 12.0 127.5 1726371737";
const GOLDEN_LAT: f64 = 35.4215;
const GOLDEN_LON: f64 = 139.6423;
const GOLDEN_ALT_M: f64 = 12.0;
const GOLDEN_HEADING_DEG: f64 = 127.5;
const GOLDEN_TS_EPOCH_S: f64 = 1_726_371_737.0;
const TOL: f64 = 1e-6;

fn raw(source_id: &str, wire: &str) -> RawObservation {
    RawObservation {
        source_id: source_id.to_string(),
        payload: wire.as_bytes().to_vec(),
        received_at: RECEIVED_AT_MS,
    }
}

fn platform_state(env: &musubi_types::EvidenceEnvelope) -> &musubi_types::PlatformState {
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected a platform state")
    };
    ps
}

#[test]
fn parse_golden_wire_field_by_field() {
    let obs = parse_my_device(GOLDEN_WIRE).expect("golden wire parses");
    assert!((obs.lat - GOLDEN_LAT).abs() < TOL, "lat {}", obs.lat);
    assert!((obs.lon - GOLDEN_LON).abs() < TOL, "lon {}", obs.lon);
    assert!((obs.alt_m.expect("alt") - GOLDEN_ALT_M).abs() < TOL);
    assert!((obs.heading_deg.expect("heading") - GOLDEN_HEADING_DEG).abs() < TOL);
    assert!((obs.ts_epoch_s.expect("ts") - GOLDEN_TS_EPOCH_S).abs() < TOL);
}

#[test]
fn golden_wire_maps_to_platformstate_within_tolerance() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", GOLDEN_WIRE))
        .expect("golden normalizes");
    let ps = platform_state(&env);
    let pos = ps.position.as_ref().expect("position present");

    assert!(
        (pos.lat_deg - GOLDEN_LAT).abs() < TOL,
        "lat {}",
        pos.lat_deg
    );
    assert!(
        (pos.lon_deg - GOLDEN_LON).abs() < TOL,
        "lon {}",
        pos.lon_deg
    );
    assert!((pos.alt_m.expect("alt") - GOLDEN_ALT_M).abs() < TOL);
    assert_eq!(ps.platform_id, "dev-01");
    assert_eq!(
        ps.timestamps.observed_at,
        Some(1_726_371_737_000_000),
        "epoch seconds become epoch microseconds"
    );
    assert_eq!(
        ps.timestamps.received_at, RECEIVED_AT_MS,
        "the ingest reception time is transcribed, not replaced with a constant"
    );
    assert_eq!(
        ps.platform_domain,
        musubi_types::PlatformDomain::Unknown,
        "a domain-agnostic template declares no domain rather than guessing one"
    );
}

#[test]
fn swapped_lat_lon_is_caught_by_the_golden_oracle() {
    // Non-vacuity for the golden above, in the two ways a column swap can show up.
    //
    // Swapping this particular wire puts 139.6423 in the latitude column, which is outside
    // the WGS-84 range, so the swap is refused outright rather than mapped.
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", "139.6423 35.4215 12.0 127.5 1726371737"))
        .expect_err("a swap that leaves the latitude range is refused");
    assert_eq!(err.mark.reason_code, "position-out-of-range");

    // A swap that stays inside both ranges is not caught by range validation, so it has to be
    // caught by the golden assertion itself. Here it is.
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", "12.3456 35.4215 12.0 127.5 1726371737"))
        .expect("in-range values normalize");
    let pos = platform_state(&env).position.as_ref().expect("position");
    assert!(
        (pos.lat_deg - GOLDEN_LAT).abs() > TOL,
        "an in-range wrong mapping must not satisfy the golden latitude"
    );
}

#[test]
fn comma_separated_wire_parses_identically() {
    let a = parse_my_device(GOLDEN_WIRE).expect("whitespace");
    let b = parse_my_device("35.4215,139.6423,12.0,127.5,1726371737").expect("commas");
    assert_eq!(a, b);
}

#[test]
fn absent_alt_yields_none_and_field_missing_alt() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", "35.4215 139.6423 - 127.5 1726371737"))
        .expect("normalizes");
    let ps = platform_state(&env);
    assert_eq!(ps.position.as_ref().expect("position").alt_m, None);
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("field-missing:alt"),
        "{}",
        env.claim.mark.reason_code
    );
}

#[test]
fn absent_timestamp_degrades_time_suspect() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", "35.4215 139.6423 12.0 127.5 -"))
        .expect("normalizes");
    assert_eq!(platform_state(&env).timestamps.observed_at, None);
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("time-suspect"),
        "{}",
        env.claim.mark.reason_code
    );
}

#[test]
fn minimal_two_field_wire_is_degraded_not_dropped() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", "35.4215 139.6423"))
        .expect("the two required columns are enough to normalize");
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(!env.claim.mark.reason_code.is_empty());
}

#[test]
fn malformed_wire_is_invalid_with_a_mark_never_a_silent_drop() {
    for wire in ["", "35.4215", "not-a-number 139.6423", "35.4215 139.6423 x"] {
        let err = MyDeviceNormalizer::new()
            .normalize(&raw("dev-01", wire))
            .expect_err(&format!("{wire:?} must be refused"));
        assert_eq!(err.mark.status, MarkStatus::Invalid, "{wire:?}");
        assert_eq!(err.source_id, "dev-01", "{wire:?}");
        assert!(!err.mark.reason_code.is_empty(), "{wire:?}");
        assert!(
            err.mark.provenance.iter().any(|p| p == "source:dev-01"),
            "{wire:?} loses its source"
        );
    }
}

#[test]
fn out_of_range_position_is_invalid_not_carried() {
    for wire in ["95.0 139.6423", "35.4215 200.0"] {
        let err = MyDeviceNormalizer::new()
            .normalize(&raw("dev-01", wire))
            .expect_err("out of range is refused");
        assert_eq!(err.mark.reason_code, "position-out-of-range", "{wire:?}");
    }
}

#[test]
fn every_input_carries_a_non_empty_reason_code_and_provenance() {
    let inputs = [GOLDEN_WIRE, "35.4215 139.6423", "", "nonsense"];
    for wire in inputs {
        let (reason, provenance) = match MyDeviceNormalizer::new().normalize(&raw("dev-01", wire)) {
            Ok(env) => (
                env.claim.mark.reason_code.clone(),
                env.claim.mark.provenance.clone(),
            ),
            Err(e) => (e.mark.reason_code.clone(), e.mark.provenance.clone()),
        };
        assert!(!reason.is_empty(), "{wire:?} produced an empty reason code");
        assert!(!provenance.is_empty(), "{wire:?} produced empty provenance");
        assert!(
            !reason.contains(';'),
            "{wire:?} reason code carries the field separator"
        );
        for token in &provenance {
            assert!(!token.contains(';'), "{wire:?} provenance token {token:?}");
        }
    }
}

#[test]
fn never_fires_reserved_mark_values() {
    // A positive allow-list rather than a denylist by name: adding a new reserved status to
    // MarkStatus cannot quietly widen what this adapter is permitted to emit.
    let inputs = [
        GOLDEN_WIRE,
        "35.4215 139.6423 - - -",
        "",
        "nonsense",
        "95.0 1.0",
    ];
    for wire in inputs {
        let status = match MyDeviceNormalizer::new().normalize(&raw("dev-01", wire)) {
            Ok(env) => env.claim.mark.status,
            Err(e) => e.mark.status,
        };
        assert!(
            matches!(
                status,
                MarkStatus::Ok | MarkStatus::Degraded | MarkStatus::Invalid
            ),
            "{wire:?} fired {status:?}, which is reserved for a path outside any adapter"
        );
    }
}

#[test]
fn confidence_is_adapter_assigned_and_says_so() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", GOLDEN_WIRE))
        .expect("normalizes");
    assert_eq!(
        env.claim.confidence_basis,
        Some(musubi_types::ConfidenceBasis::AdapterAssigned),
        "this wire has no confidence field, and the envelope says so rather than implying a \
         measurement"
    );
    assert!(
        env.claim
            .mark
            .provenance
            .iter()
            .any(|p| p == "confidence:adapter-assigned"),
        "{:?}",
        env.claim.mark.provenance
    );
}

#[test]
fn confidence_is_a_constant_not_derived_from_the_wire() {
    // Non-vacuity for the claim above: two materially different wires must produce the same
    // confidence, which is what makes it a constant rather than a measurement.
    let a = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", GOLDEN_WIRE))
        .expect("normalizes");
    let b = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", "35.4215 139.6423"))
        .expect("normalizes");
    assert!((a.claim.confidence - b.claim.confidence).abs() < f32::EPSILON);
}

#[test]
fn sealing_round_trips_and_a_one_bit_change_is_detected() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", GOLDEN_WIRE))
        .expect("normalizes");
    assert!(
        env.content_digest.is_some(),
        "normalize returns a sealed envelope"
    );
    assert_eq!(verify_digest(&env).status, MarkStatus::Ok);

    let mut tampered = env.clone();
    let ComObject::PlatformState(ps) = &mut tampered.observation else {
        panic!("expected a platform state")
    };
    let pos = ps.position.as_mut().expect("position");
    pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);
    let mark = verify_digest(&tampered);
    assert_eq!(mark.status, MarkStatus::Invalid);
    assert_eq!(mark.reason_code, "meta-tamper-detected");

    // Re-sealing the tampered envelope yields a different digest, so the change is visible
    // even to a reader that only compares digests.
    let resealed = seal_digest(tampered);
    assert_ne!(resealed.content_digest, env.content_digest);
}

#[test]
fn oversize_wire_line_is_quarantined_before_any_parsing() {
    // The payload is both oversize and unparseable. It must be refused for being oversize,
    // which is only possible if quarantine runs first.
    let payload = vec![b'x'; MAX_WIRE_LINE_BYTES + 1];
    let err = MyDeviceNormalizer::new()
        .normalize(&RawObservation {
            source_id: "dev-01".to_string(),
            payload,
            received_at: RECEIVED_AT_MS,
        })
        .expect_err("oversize is refused");
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn a_line_exactly_at_the_bound_is_not_quarantined() {
    // Non-vacuity for the bound: it is an upper bound, not a rejection of everything large.
    let mut payload = GOLDEN_WIRE.as_bytes().to_vec();
    payload.resize(MAX_WIRE_LINE_BYTES, b' ');
    let env = MyDeviceNormalizer::new()
        .normalize(&RawObservation {
            source_id: "dev-01".to_string(),
            payload,
            received_at: RECEIVED_AT_MS,
        })
        .expect("a line at the bound still normalizes");
    assert_ne!(env.claim.mark.status, MarkStatus::Invalid);
}

#[test]
fn the_adapter_slug_reaches_provenance() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("dev-01", GOLDEN_WIRE))
        .expect("normalizes");
    assert!(
        env.claim
            .mark
            .provenance
            .iter()
            .any(|p| p == &format!("adapter:{ADAPTER_SLUG}")),
        "{:?}",
        env.claim.mark.provenance
    );
}

#[test]
fn two_taps_of_this_adapter_type_get_distinct_platform_ids() {
    // This wire declares no identity of its own, so the tap-level source id is the only
    // identity available. Two taps with distinct source ids must not collapse into one track.
    let n = MyDeviceNormalizer::new();
    let a = n
        .normalize(&raw("dev-01", GOLDEN_WIRE))
        .expect("normalizes");
    let b = n
        .normalize(&raw("dev-02", GOLDEN_WIRE))
        .expect("normalizes");
    assert_ne!(
        platform_state(&a).platform_id,
        platform_state(&b).platform_id
    );
    assert_ne!(
        a.content_digest, b.content_digest,
        "distinct tracks must not share a content digest"
    );
}

#[test]
fn polling_a_tap_and_normalizing_it_never_opens_an_egress_path() {
    let mut tap = QueuedTap::new(
        "dev-01",
        vec![GOLDEN_WIRE.as_bytes().to_vec(), b"nonsense".to_vec()],
        RECEIVED_AT_MS,
    );
    let n = MyDeviceNormalizer::new();
    let mut seen = 0;
    while let Some(obs) = tap.poll() {
        // Both outcomes are accounted for: nothing is dropped on the way through.
        let _ = n.normalize(&obs);
        seen += 1;
        assert!(
            tap.manifest().is_read_only(),
            "egress opened after {seen} frames"
        );
    }
    assert_eq!(seen, 2, "every queued frame is accounted for");
}
