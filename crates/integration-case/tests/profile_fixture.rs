#![allow(clippy::expect_used)]

use musubi_integration_case::{
    VerificationDispositionV1, load_profile_candidate, sha256_digest, strict_manifest_from_json,
    verified_profile_report_from_json, verify_profile_case,
};

const MANIFEST: &[u8] =
    include_bytes!("../../../examples/offline-verification/profile/manifest.json");
const SAMPLE: &[u8] =
    include_bytes!("../../../examples/offline-verification/profile/input/sample.csv");
const ORACLE: &[u8] =
    include_bytes!("../../../examples/offline-verification/profile/oracle/profile-oracle.json");
const RECEIPT: &[u8] =
    include_bytes!("../../../examples/offline-verification/profile/verification-receipt-v1.json");
const REQUIREMENTS_COMMIT: &str = "1111111111111111111111111111111111111111";
const IMPLEMENTATION_COMMIT: &str = "2222222222222222222222222222222222222222";

#[test]
fn committed_profile_case_reproduces_the_frozen_receipt() {
    let manifest = strict_manifest_from_json(MANIFEST).expect("frozen manifest");
    let candidate_root = std::path::Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../examples/offline-verification/profile/candidate"
    ));
    let candidate =
        load_profile_candidate(candidate_root, &manifest).expect("bounded candidate root");
    assert_eq!(
        manifest
            .manifest_digest()
            .expect("manifest digest")
            .as_str(),
        "sha256:3ab10aa5070168875e683793bb2b2ff6d0c5b86e924b534d4b5efd24c32b52c1"
    );
    assert_eq!(manifest.requirements_commit, REQUIREMENTS_COMMIT);
    assert_eq!(manifest.implementation_commit, IMPLEMENTATION_COMMIT);
    assert_eq!(
        sha256_digest(SAMPLE).as_str(),
        "sha256:8227a9371621874feacf709f255ca35a3813586e8b12b800f29ac047c392135d"
    );
    assert_eq!(manifest.input_artifacts[0].digest, sha256_digest(SAMPLE));
    assert_eq!(
        sha256_digest(ORACLE).as_str(),
        "sha256:68f630a42642358ffc42caf90a54d7c722ec66c554ca98a1cb808dccf543f310"
    );
    assert_eq!(manifest.oracle.oracle_digest, sha256_digest(ORACLE));
    let first = verify_profile_case(
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        &candidate,
        SAMPLE,
        ORACLE,
    )
    .expect("first verification");
    let second = verify_profile_case(
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        &candidate,
        SAMPLE,
        ORACLE,
    )
    .expect("second verification");
    assert_eq!(first, second);
    let committed = verified_profile_report_from_json(
        RECEIPT,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        &candidate,
        SAMPLE,
        ORACLE,
    )
    .expect("committed receipt must equal a full replay of every frozen input");
    assert_eq!(committed.report(), first.verification());
    assert_eq!(
        first.verification().disposition,
        VerificationDispositionV1::AwaitingHumanSeal
    );
    assert_eq!(
        first.verification().candidate_digest.as_str(),
        "sha256:019cb552fd8f1fb89dd918f9e48f26af6cf852e02ff5c96bd138b3c0cd6c3622"
    );
    assert_eq!(
        first.bundle_digest().as_str(),
        "sha256:eb73206e2eadbe94379a05ed9938ff7b2210f362e76a6df48392ea1c90889fee"
    );
    let score = first.profile_score().expect("score");
    assert_eq!(
        (score.matched, score.missed, score.mis_mapped, score.phantom),
        (3, 0, 0, 0)
    );
}

#[test]
fn profile_artifact_gate_rejects_a_raw_report_that_bypasses_the_score_bundle() {
    let manifest = strict_manifest_from_json(MANIFEST).expect("frozen manifest");
    let candidate_root = std::path::Path::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../examples/offline-verification/profile/candidate"
    ));
    let candidate =
        load_profile_candidate(candidate_root, &manifest).expect("bounded candidate root");
    let replayed = verify_profile_case(
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        &candidate,
        SAMPLE,
        ORACLE,
    )
    .expect("fixture replay");
    let raw_report = serde_json::to_vec(replayed.verification()).expect("raw report JSON");

    let error = verified_profile_report_from_json(
        &raw_report,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        &candidate,
        SAMPLE,
        ORACLE,
    )
    .expect_err("a Profile report detached from its score bundle must be rejected");
    assert!(error.contains("profile report JSON"));

    let verified = verified_profile_report_from_json(
        RECEIPT,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        &candidate,
        SAMPLE,
        ORACLE,
    )
    .expect("the complete replay-equal score-bound bundle remains accepted");
    assert_eq!(verified.artifact_kind(), "profile_verification_bundle");
    assert_eq!(verified.report(), replayed.verification());
}
