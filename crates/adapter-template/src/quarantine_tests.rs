//! contract: quarantine harness ground point for the TEMPLATE adapter (Guardrail #2 anchor:
//! the quarantine contract). This is the copy-paste source for future adapters (the public adapter documentation), so demonstrating the quarantine call here matters beyond this one
//! crate — it is what future adapter authors will see and keep.

use super::{MAX_WIRE_LINE_BYTES, MyDeviceNormalizer};
use musubi_core::quarantine::QUARANTINE_REASON_CODE;
use musubi_core::{Normalizer, RawObservation};
use musubi_types::MarkStatus;

const GOLDEN_WIRE: &str = "35.4215 139.6423 12.0 127.5 1726371737";

fn raw(source_id: &str, payload: Vec<u8>) -> RawObservation {
    RawObservation {
        source_id: source_id.to_string(),
        payload,
        received_at: 0,
    }
}

#[test]
fn golden_line_still_normalizes_after_quarantine_wiring() {
    // Regression: quarantine wiring must not disturb the existing golden line.
    let env = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", GOLDEN_WIRE.as_bytes().to_vec()))
        .expect("golden wire still normalizes");
    assert_ne!(env.claim.mark.status, MarkStatus::Invalid);
}

#[test]
fn oversize_wire_line_is_quarantined_before_any_parsing() {
    // A wire "line" far larger than any real 5-column text line is rejected before UTF-8
    // decode / `parse_my_device` is even attempted.
    let oversize = vec![b'9'; MAX_WIRE_LINE_BYTES + 1];
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", oversize))
        .expect_err("oversize wire line must be rejected");
    assert_eq!(err.mark.status, MarkStatus::Invalid);
    assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    assert!(
        err.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:payload-oversize:")),
        "{:?}",
        err.mark.provenance
    );
}

#[test]
fn wire_line_exactly_at_the_bound_is_not_quarantined() {
    // Boundary check: exactly at the cap must NOT be quarantine-rejected (though it will still
    // fail `parse_my_device` for being nonsense — that is a different, pre-existing reason_code).
    let at_bound = vec![b'9'; MAX_WIRE_LINE_BYTES];
    let err = MyDeviceNormalizer::new()
        .normalize(&raw("my-device-01", at_bound))
        .expect_err("a wall of digits is not a valid 5-column wire line");
    assert_ne!(
        err.mark.reason_code, QUARANTINE_REASON_CODE,
        "at-bound payload must pass quarantine (reject only strictly over the cap)"
    );
}
