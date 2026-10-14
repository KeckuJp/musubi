//! Acceptance oracle for the adapter TEMPLATE — these tests are the *credential*.
//!
//! They are deliberately NON-tautological: the 誤マップ=0 (zero-mismap) golden asserts the
//! parsed/normalized COM field-by-field against independently-known numbers, so a WRONG
//! mapping (swapped lat/lon, wrong scale, dropped field) MUST fail. When an adopter copies
//! this template, these are the tests they keep and re-point at their own golden frame.
//!
//! Coverage (matches the template's stated contract):
//! - 誤マップ=0 golden: a fixed wire maps field-by-field to expected PlatformState (tol 1e-6).
//! - §5 read-only: manifest declares egress_bytes==0; tap exposes no send surface; poll never
//!   egresses regardless of input.
//! - no-silent-drop: malformed wire ⇒ INVALID Mark (Some), never a silent None/drop.
//! - never fires reserved MARK values
//!   (ORDER_UNKNOWN/WITHHELD/STALE_REVOCATION/SELF_ASSERTED_TIME).
//! - own-device → CoT via the SAME CotPublisher (reuse, not a bespoke serializer).

use super::{
    MyDeviceManifest, MyDeviceNormalizer, QueuedMyDeviceTap, RawObs, ReadOnlyMyDeviceTap,
    parse_my_device, publish_my_device_cot,
};
use musubi_core::{Normalizer, RawObservation, verify_digest};
use musubi_types::{ComObject, MarkStatus};

/// The golden `my-device` wire line (synthetic) with INDEPENDENTLY KNOWN field values:
/// lat=35.4215, lon=139.6423, alt=12.0 m, heading=127.5°, ts=1726371737 (epoch s).
const GOLDEN_WIRE: &str = "35.4215 139.6423 12.0 127.5 1726371737";

fn raw(source_id: &str, wire: &str) -> RawObservation {
    RawObservation {
        source_id: source_id.to_string(),
        payload: wire.as_bytes().to_vec(),
        received_at: 0,
    }
}

// =====================================================================================
// 誤マップ=0 golden  —  the adopter's OWN correctness oracle (field-by-field, tol 1e-6)
// =====================================================================================

#[test]
fn parse_golden_wire_field_by_field() {
    // The parser alone (THE function an adopter rewrites) must decode each column exactly.
    // A swapped column or wrong split would change one of these and fail.
    let obs = parse_my_device(GOLDEN_WIRE).expect("golden wire parses");
    assert!((obs.lat - 35.4215).abs() < 1e-6, "lat={}", obs.lat);
    assert!((obs.lon - 139.6423).abs() < 1e-6, "lon={}", obs.lon);
    assert!(
        (obs.alt_m.expect("alt present") - 12.0).abs() < 1e-6,
        "alt={:?}",
        obs.alt_m
    );
    assert!(
        (obs.heading_deg.expect("heading present") - 127.5).abs() < 1e-6,
        "heading={:?}",
        obs.heading_deg
    );
    assert!(
        (obs.ts_epoch_s.expect("ts present") - 1_726_371_737.0).abs() < 1e-6,
        "ts={:?}",
        obs.ts_epoch_s
    );
}

#[test]
fn golden_wire_maps_to_platformstate_within_tolerance() {
    // contract-style: the normalized COM PlatformState matches expected field-by-field (tol 1e-6).
    // This is the zero-mismap oracle: wrong scale or swapped lat/lon fails here.
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("golden wire normalizes");

    // Converges onto the SAME COM PlatformState type a MAVLink/USV adapter produces.
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    let pos = ps.position.as_ref().expect("position present");

    assert!((pos.lat_deg - 35.4215).abs() < 1e-6, "lat={}", pos.lat_deg);
    assert!((pos.lon_deg - 139.6423).abs() < 1e-6, "lon={}", pos.lon_deg);
    assert!(
        (pos.alt_m.expect("alt present") - 12.0).abs() < 1e-6,
        "alt={:?}",
        pos.alt_m
    );
    assert_eq!(ps.platform_id, "my-device-01");

    // ts=1726371737 epoch s ⇒ observed_at = 1726371737 * 1_000_000 us (independent calc).
    assert_eq!(
        ps.timestamps.observed_at,
        Some(1_726_371_737_i64 * 1_000_000)
    );
    // Absolute epoch ⇒ confidence above the time-suspect threshold (0.5).
    assert!(ps.timestamps.time_confidence > 0.5);

    // Sealed (content_digest filled); signature stays None (keys are TOE-外, impl-deferred).
    assert!(env.content_digest.is_some(), "envelope must be sealed");
    assert!(env.signature.is_none());
}

#[test]
fn golden_full_fields_is_ok_with_provenance_preserved() {
    // All fields present ⇒ zero degradation ⇒ OK. Provenance keeps source/adapter + notes.
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("normalizes");
    assert_eq!(env.claim.mark.status, MarkStatus::Ok);
    assert_eq!(env.claim.mark.reason_code, "normalized");

    let prov = &env.claim.mark.provenance;
    assert_eq!(prov[0], "source:my-device-01");
    assert_eq!(prov[1], "adapter:my-device-text/v1");
    assert!(prov.iter().any(|p| p == "alt-ref:MSL-assumed"), "{prov:?}");
    assert!(prov.iter().any(|p| p == "heading_deg:127.5"), "{prov:?}");
    assert_eq!(env.claim.claim_id, "my-device-01:my-device-text");
}

// =====================================================================================
// platform_id cross-instance collision (regression review)
// =====================================================================================

#[test]
fn two_instances_of_this_adapter_type_get_distinct_platform_ids() {
    // Positive test (contract A-2): same adapter TYPE (my-device-text), two DIFFERENT instances
    // (distinct `source_id` — the tap-level identifier the caller assigns per physical
    // connection/device) must not collapse onto the same COM `platform_id`. This synthetic wire
    // has no device-declared identity field of its own (see [`RawObs`]) so `source_id` IS the
    // full identity here; this regression-guards that property (see the honest-scope comment at
    // `platform_id: source_id.clone()` in `lib.rs` for why this differs from
    // `adapter-mavlink`/`adapter-usv-json`, which compose `source_id` with a wire-declared field).
    let env_a = MyDeviceNormalizer::new()
        .normalize(&raw("device-a", GOLDEN_WIRE))
        .expect("device-a normalizes");
    let env_b = MyDeviceNormalizer::new()
        .normalize(&raw("device-b", GOLDEN_WIRE)) // identical payload, different source_id
        .expect("device-b normalizes");

    let ComObject::PlatformState(ps_a) = &env_a.observation else {
        panic!("expected PlatformState")
    };
    let ComObject::PlatformState(ps_b) = &env_b.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(ps_a.platform_id, "device-a");
    assert_eq!(ps_b.platform_id, "device-b");
    assert_ne!(
        ps_a.platform_id, ps_b.platform_id,
        "two distinct adapter instances must not collapse to one platform_id"
    );
    assert_ne!(env_a.claim.claim_id, env_b.claim.claim_id);
}

#[test]
fn swapped_lat_lon_is_caught_by_the_golden_oracle() {
    // Proves the golden oracle is NOT tautological: a wire whose lat/lon columns are swapped
    // produces a different COM, so an adopter who mis-orders columns fails the field-by-field
    // assertion above.
    // (1) Out-of-range swap: lon=139.6423 placed in the lat column is out of WGS-84 range ⇒ INVALID.
    let swapped = "139.6423 35.4215 12.0 127.5 1726371737";
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", swapped))
        .expect_err("lat=139.6423 is out of WGS-84 latitude range");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "position-out-of-range");

    // (2) IN-RANGE swap (both values are valid lat AND lon, so no range check fires): the parse is
    // positional, so transposed columns yield a DIFFERENT RawObs. This is what makes the
    // field-by-field golden oracle non-tautological even when a mis-map stays in range — an adopter
    // who transposes columns fails `golden_wire_maps_to_platformstate_within_tolerance`, not just
    // the range guard above.
    let ordered = parse_my_device("10.0 20.0 12.0 127.5 1726371737").expect("in range");
    let transposed = parse_my_device("20.0 10.0 12.0 127.5 1726371737").expect("in range");
    assert_ne!(
        ordered, transposed,
        "column order must be significant (positional parse) — the golden oracle catches in-range mis-maps"
    );
}

#[test]
fn comma_separated_wire_parses_identically() {
    // The wire doc promises commas are accepted like whitespace; pin that so the parser's
    // separator handling can't silently regress.
    let csv = "35.4215,139.6423,12.0,127.5,1726371737";
    assert_eq!(
        parse_my_device(csv).expect("csv parses"),
        RawObs {
            lat: 35.4215,
            lon: 139.6423,
            alt_m: Some(12.0),
            heading_deg: Some(127.5),
            ts_epoch_s: Some(1_726_371_737.0),
        }
    );
}

// =====================================================================================
// DEGRADED semantics  —  absent optional fields contribute, core decides (not the adapter)
// =====================================================================================

#[test]
fn absent_alt_yields_none_and_field_missing_alt() {
    // alt column = `-` (explicit absent) ⇒ COM alt_m=None + DEGRADED(field-missing:alt).
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-02", "35.41 139.651 - 90.0 1726371800"))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(
        ps.position.as_ref().expect("pos").alt_m,
        None,
        "absent alt must be None in COM"
    );
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("field-missing:alt"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn absent_heading_degrades_with_field_missing_heading() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-02", "35.41 139.651 5.0 - 1726371800"))
        .expect("normalizes");
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.contains("field-missing:heading"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn absent_timestamp_degrades_time_suspect() {
    // ts column = `-` ⇒ observed_at=None, low confidence, DEGRADED(time-suspect first).
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-02", "35.41 139.651 5.0 90.0 -"))
        .expect("normalizes");
    let ComObject::PlatformState(ps) = &env.observation else {
        panic!("expected PlatformState")
    };
    assert_eq!(ps.timestamps.observed_at, None);
    assert!((ps.timestamps.time_confidence - 0.2).abs() < 1e-6);
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
    assert!(
        env.claim.mark.reason_code.starts_with("time-suspect"),
        "reason={}",
        env.claim.mark.reason_code
    );
}

#[test]
fn minimal_two_field_wire_is_degraded_not_dropped() {
    // Only the 2 required columns ⇒ all optionals absent ⇒ DEGRADED (never an error/drop).
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-03", "35.0 139.0"))
        .expect("two-field wire still normalizes");
    assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
}

// =====================================================================================
// no-silent-drop  —  malformed wire ⇒ INVALID Mark (Some), never a silent None
// =====================================================================================

#[test]
fn malformed_wire_is_invalid_with_mark() {
    // A single garbage token (< 2 required columns) ⇒ INVALID(wire-malformed), MARK carried.
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-x", "garbage"))
        .expect_err("malformed wire must not normalize");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "wire-malformed");
    assert!(!err.mark.reason_code.is_empty());
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p == "source:my-device-x")
    );
}

#[test]
fn unparseable_field_is_invalid() {
    // lon column is not a number ⇒ INVALID(field-unparseable) with the offending token kept.
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-x", "35.0 NORTH 12.0 90.0 1726371737"))
        .expect_err("non-numeric lon must not normalize");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "field-unparseable");
    assert!(
        err.mark.provenance.iter().any(|p| p.contains("parse:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn unparseable_optional_field_is_invalid_not_silently_none() {
    // An optional column that is present but garbage must NOT become a silent None — it is an
    // explicit INVALID (only the `-` token means absent).
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-x", "35.0 139.0 high 90.0 1726371737"))
        .expect_err("garbage alt must be an error, not a silent None");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "field-unparseable");
}

#[test]
fn lat_out_of_range_is_invalid_not_dropped() {
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-x", "91.0 139.0 12.0 90.0 1726371737"))
        .expect_err("lat 91 is out of WGS-84 range");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, "position-out-of-range");
}

#[test]
fn every_input_carries_a_mark_reason_code() {
    // Sweep OK / DEGRADED / each INVALID: every outcome carries a non-empty MARK reason_code
    // (nothing is silently dropped).
    let inputs: [&str; 6] = [
        GOLDEN_WIRE,        // OK
        "35.0 139.0",       // DEGRADED (all optionals absent)
        "35.0 139.0 - - -", // DEGRADED (explicit absents)
        "garbage",          // INVALID wire-malformed
        "35.0 NORTH",       // INVALID field-unparseable
        "91.0 139.0",       // INVALID position-out-of-range
    ];
    for wire in inputs {
        let result = MyDeviceNormalizer::new().normalize(&raw("u", wire));
        let reason_nonempty = result.as_ref().map_or_else(
            |e| !e.mark.reason_code.is_empty(),
            |ok| !ok.claim.mark.reason_code.is_empty(),
        );
        assert!(
            reason_nonempty,
            "every outcome must carry a MARK reason_code: {wire:?}"
        );
    }
}

// =====================================================================================
// Never fires reserved MARK values
// (ORDER_UNKNOWN / WITHHELD / STALE_REVOCATION / SELF_ASSERTED_TIME / BUFFER_SATURATED)
// =====================================================================================

#[test]
fn never_fires_reserved_mark_values() {
    // The adapter may only emit OK / DEGRADED / INVALID. The reserved 5 belong to the DDIL
    // path, northbound fan-out, revocation, self-asserted-time degrade (contract §3.3), and
    // store-and-forward bounded-buffer saturation (contract) respectively — never to an adapter.
    let inputs: [&str; 5] = [
        GOLDEN_WIRE,
        "35.0 139.0",            // DEGRADED
        "35.0 139.0 5.0 90.0 -", // DEGRADED time-suspect
        "garbage",               // INVALID
        "91.0 139.0",            // INVALID out-of-range
    ];
    for wire in inputs {
        let status = match MyDeviceNormalizer::new().normalize(&raw("u", wire)) {
            Ok(env) => env.claim.mark.status,
            Err(e) => e.mark.status,
        };
        assert!(
            matches!(
                status,
                MarkStatus::Ok | MarkStatus::Degraded | MarkStatus::Invalid
            ),
            "template must not emit reserved MarkStatus, got {status:?} for {wire:?}"
        );
    }
}

// =====================================================================================
// seal → verify round-trip  (contract base reuse — own device gets tamper-evidence for free)
// =====================================================================================

#[test]
fn ok_golden_seals_and_verifies_roundtrip() {
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("normalizes");
    let mark = verify_digest(&env); // core reuse
    assert_eq!(mark.status, MarkStatus::Ok);
    assert_eq!(mark.reason_code, "digest-verified");
}

#[test]
fn one_bit_meta_tamper_is_detected() {
    // Tamper-evidence works on the template's output too (format-independent): flip 1 bit of
    // lat after sealing ⇒ verify_digest reports INVALID(meta-tamper-detected).
    let mut env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("normalizes");
    assert_eq!(verify_digest(&env).status, MarkStatus::Ok);
    let ComObject::PlatformState(ps) = &mut env.observation else {
        panic!("expected PlatformState")
    };
    let pos = ps.position.as_mut().expect("position present");
    pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);
    let mark = verify_digest(&env);
    assert_eq!(mark.status, MarkStatus::Invalid);
    assert_eq!(mark.reason_code, "meta-tamper-detected");
}

// =====================================================================================
// own-device → CoT via the SAME CotPublisher (reuse, not a bespoke serializer)
// =====================================================================================

#[test]
fn envelope_publishes_via_same_cot_publisher() {
    // The template's helper hands the sealed envelope to the shared CotPublisher and gets
    // schema-valid CoT — proving connect-once → C2 without writing a serializer.
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("normalizes");
    let xml = String::from_utf8(publish_my_device_cot(&env).expect("publishes CoT")).expect("utf8");

    // Same uid/point structure as every other adapter (device-independent north-bound).
    assert!(xml.contains("uid=\"my-device-01\""), "{xml}");
    assert!(xml.contains("<event "), "{xml}");
    assert!(xml.contains("<point "), "{xml}");
    assert!(xml.contains("lat=\"35.4215\""), "{xml}");
    assert!(xml.contains("lon=\"139.6423\""), "{xml}");
    // The 7 required CoT event attrs are present (structural validity floor).
    assert!(xml.contains("version=\"2.0\""), "{xml}");
    assert!(xml.contains("how=\"m-g\""), "{xml}");
    assert!(xml.contains("time=\""), "{xml}");
    assert!(xml.contains("stale=\""), "{xml}");
}

#[test]
fn degraded_mark_is_visible_in_cot_remarks() {
    // no-silent-drop end-to-end: a DEGRADED state surfaces in CoT <remarks> (the operator can
    // read it in a TAK client's Remarks field). Absent timestamp ⇒ time-suspect DEGRADED.
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-02", "35.0 139.0 5.0 90.0 -"))
        .expect("normalizes");
    let xml = String::from_utf8(publish_my_device_cot(&env).expect("publishes")).expect("utf8");
    assert!(xml.contains("<remarks>"), "{xml}");
    assert!(xml.contains("MARK:DEGRADED:time-suspect"), "{xml}");
}

// =====================================================================================
// contract: adapter-assigned confidence is never silently fabricated as measured
// =====================================================================================

#[test]
fn confidence_provenance_marks_adapter_assigned_origin() {
    // Claim.confidence has no upstream wire counterpart in this format. Musubi must never
    // let that constant pass for a propagated/measured value — the "confidence:adapter-assigned"
    // note keeps the origin visible in `mark.provenance` (audit contract).
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("normalizes");
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
    // Identity / fabrication=0 property: since the wire carries no confidence signal, varying
    // every OTHER field must never change `Claim.confidence` — if it did, that would mean the
    // adapter is quietly deriving a "confidence" out of thin air instead of asserting one fixed,
    // clearly-marked default (the only honest option when there is no upstream signal at all).
    let full = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE))
        .expect("normalizes");
    let minimal = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-02", "35.0 139.0")) // all optionals absent ⇒ DEGRADED
        .expect("two-field wire still normalizes");
    assert_eq!(full.claim.confidence, minimal.claim.confidence);
    assert!((full.claim.confidence - 0.6).abs() < 1e-6);
}

// =====================================================================================
// §5 read-only invariant  (egress_bytes==0; tap has no send surface; poll never egresses)
// =====================================================================================

#[test]
fn read_only_manifest_declares_zero_egress() {
    let m = MyDeviceManifest::read_only();
    assert_eq!(m.egress_bytes, 0, "read-only tap must not egress");
    assert!(!m.emits_heartbeat, "must not emit heartbeat");
    assert!(!m.emits_command, "must not emit command (X-NOWRITE / §5)");
}

#[test]
fn tap_poll_does_not_egress_regardless_of_input() {
    // poll consumes the receive queue (read-only) ⇒ egress_bytes stays 0 across OK/garbage/
    // INVALID frames, and normalize never opens a send path either.
    let frames = vec![
        GOLDEN_WIRE.as_bytes().to_vec(),
        b"garbage".to_vec(),
        b"91.0 139.0 12.0 90.0 1726371737".to_vec(), // out-of-range ⇒ INVALID
    ];
    let mut tap = QueuedMyDeviceTap::new("my-device-01", frames);
    assert_eq!(tap.source_id(), "my-device-01");

    let mut polled = 0;
    while let Some(obs) = tap.poll() {
        polled += 1;
        assert_eq!(tap.manifest().egress_bytes, 0);
        let _ = MyDeviceNormalizer::new().normalize(&obs); // success or failure, no send path
        assert_eq!(tap.manifest().egress_bytes, 0);
    }
    assert_eq!(polled, 3, "all queued frames consumed read-only");
}
