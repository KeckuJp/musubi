//! Oracles for the SPI contract itself, independent of any particular wire.
//!
//! The read-only declaration is the load-bearing claim in this crate, so it is asserted
//! rather than promised: the manifest declares zero egress, the tap hands out what it was
//! given and nothing else, and polling an empty tap terminates instead of inventing an
//! observation.

use crate::spi::{QueuedTap, ReadOnlyTap, SourceManifest, invalid, quarantine_error};
use musubi_core::quarantine::{QUARANTINE_REASON_CODE, QuarantineReject};
use musubi_types::MarkStatus;

const RECEIVED_AT_MS: i64 = 1_726_371_737_123;

#[test]
fn read_only_manifest_declares_every_egress_path_closed() {
    let m = SourceManifest::read_only();
    assert_eq!(m.egress_bytes, 0, "a read-only tap sends nothing");
    assert!(!m.emits_heartbeat, "a read-only tap emits no heartbeat");
    assert!(!m.emits_command, "a read-only tap emits no command");
    assert!(m.is_read_only());
    assert_eq!(
        SourceManifest::default(),
        m,
        "the default is the read-only declaration"
    );
}

#[test]
fn is_read_only_is_false_when_any_single_egress_path_opens() {
    // Non-vacuity: the predicate would be worthless if it answered true for everything.
    for opened in [
        SourceManifest {
            egress_bytes: 1,
            ..SourceManifest::read_only()
        },
        SourceManifest {
            emits_heartbeat: true,
            ..SourceManifest::read_only()
        },
        SourceManifest {
            emits_command: true,
            ..SourceManifest::read_only()
        },
    ] {
        assert!(!opened.is_read_only(), "{opened:?} is not read-only");
    }
}

#[test]
fn tap_hands_out_exactly_what_it_was_given_and_then_stops() {
    let mut tap = QueuedTap::new(
        "dev-01",
        vec![b"a".to_vec(), b"bb".to_vec()],
        RECEIVED_AT_MS,
    );
    assert_eq!(tap.source_id(), "dev-01");
    assert!(tap.manifest().is_read_only());

    let first = tap.poll().expect("first frame");
    assert_eq!(first.source_id, "dev-01");
    assert_eq!(first.payload, b"a");
    assert_eq!(
        first.received_at, RECEIVED_AT_MS,
        "the tap stamps the ingest time it was given; a normalizer transcribes it"
    );

    let second = tap.poll().expect("second frame");
    assert_eq!(second.payload, b"bb");

    assert!(
        tap.poll().is_none(),
        "an exhausted tap yields nothing, it does not fabricate"
    );
    assert!(
        tap.manifest().is_read_only(),
        "polling never opens an egress path"
    );
}

#[test]
fn invalid_carries_the_source_the_slug_and_the_specific_detail() {
    let e = invalid("dev-01", "acme/v1", "wire-malformed", "not-utf8:offset 3");
    assert_eq!(e.source_id, "dev-01");
    assert_eq!(e.mark.status, MarkStatus::Invalid);
    assert_eq!(e.mark.reason_code, "wire-malformed");
    assert_eq!(
        e.mark.provenance,
        vec![
            "source:dev-01".to_string(),
            "adapter:acme/v1".to_string(),
            "not-utf8:offset 3".to_string(),
        ],
        "which source, which adapter and what specifically went wrong all survive"
    );
}

#[test]
fn quarantine_error_uses_the_one_shared_reason_code() {
    let e = quarantine_error(
        "dev-01",
        "acme/v1",
        QuarantineReject::PayloadOversize {
            max_bytes: 8,
            actual_bytes: 9,
        },
    );
    assert_eq!(e.mark.status, MarkStatus::Invalid);
    assert_eq!(
        e.mark.reason_code, QUARANTINE_REASON_CODE,
        "a quarantine rejection is reported with the shared token, not a per-adapter one"
    );
    assert!(
        e.mark
            .provenance
            .iter()
            .any(|p| p.starts_with("quarantine:")),
        "the specific rejection survives in provenance: {:?}",
        e.mark.provenance
    );
}

#[test]
fn no_error_this_module_builds_carries_the_downstream_field_separator() {
    // ';' is the top-level separator on the reading side. A reason code or provenance token
    // containing one collides with that boundary and cannot be split apart again.
    let errors = [
        invalid("dev-01", "acme/v1", "wire-malformed", "detail"),
        quarantine_error(
            "dev-01",
            "acme/v1",
            QuarantineReject::PayloadOversize {
                max_bytes: 1,
                actual_bytes: 2,
            },
        ),
    ];
    for e in &errors {
        assert!(
            !e.mark.reason_code.contains(';'),
            "{:?}",
            e.mark.reason_code
        );
        for token in &e.mark.provenance {
            assert!(!token.contains(';'), "{token:?}");
        }
    }
}
