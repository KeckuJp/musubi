#![allow(clippy::expect_used)]

use musubi_integration_case::{
    CheckStatusV1, VerificationCheckIdV1, VerificationDispositionV1, adapter_hold_report,
    sha256_digest, strict_manifest_from_json, verified_adapter_hold_from_json,
};

const MANIFEST: &[u8] =
    include_bytes!("../../../examples/offline-verification/adapter/manifest.json");
const CANDIDATE: &[u8] =
    include_bytes!("../../../examples/offline-verification/adapter/candidate/src/lib.rs");
const INPUT: &[u8] =
    include_bytes!("../../../examples/offline-verification/adapter/input/sample.msyn");
const ORACLE: &[u8] =
    include_bytes!("../../../examples/offline-verification/adapter/oracle/normalized.json");
const REQUIREMENTS_COMMIT: &str = "1111111111111111111111111111111111111111";
const IMPLEMENTATION_COMMIT: &str = "2222222222222222222222222222222222222222";

#[test]
fn committed_adapter_case_reproduces_an_explicit_hold() {
    let manifest = strict_manifest_from_json(MANIFEST).expect("frozen manifest");
    assert_eq!(
        manifest
            .manifest_digest()
            .expect("manifest digest")
            .as_str(),
        "sha256:a6bc4e5dbcc4897462c92e2d4df654d931455d6f1b5bea0566be120abbfb3e9b"
    );
    assert_eq!(
        sha256_digest(INPUT).as_str(),
        "sha256:eb07992c970e4bcee34a65c3253ea346d1e0783b9300bee3b9652b0a5ab521cf"
    );
    assert_eq!(manifest.input_artifacts[0].digest, sha256_digest(INPUT));
    assert_eq!(
        sha256_digest(ORACLE).as_str(),
        "sha256:fb44fc30dbe31bbdd49167bbe8cb94d738dedc6aa8cbbeb530f6ef88209738e2"
    );
    assert_eq!(manifest.oracle.oracle_digest, sha256_digest(ORACLE));
    let first = adapter_hold_report(
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect("first HOLD report");
    let second = adapter_hold_report(
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect("second HOLD report");
    assert_eq!(first, second);
    assert_eq!(
        first.candidate_digest.as_str(),
        "sha256:38b181ab41a474bdbcb611418bea0fbb9233a5f67a686e209b260b7a51d748f9"
    );
    assert_eq!(first.disposition, VerificationDispositionV1::Hold);
    for (check_id, detail_code) in [
        (
            VerificationCheckIdV1::InputDigestMatch,
            "HOLD_FROZEN_INPUT_RUNNER",
        ),
        (
            VerificationCheckIdV1::OracleDigestMatch,
            "HOLD_FROZEN_ORACLE_RUNNER",
        ),
        (
            VerificationCheckIdV1::AdapterConformance,
            "HOLD_EXTERNAL_FROZEN_RUNNER",
        ),
    ] {
        assert!(first.checks.iter().any(|check| {
            check.check_id == check_id
                && check.status == CheckStatusV1::Hold
                && check.detail_code == detail_code
        }));
    }
    assert!(!first.runtime_load_authorized);
    let report_json = serde_json::to_vec(&first).expect("Adapter HOLD JSON");
    let verified = verified_adapter_hold_from_json(
        &report_json,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect("Adapter HOLD artifact gate");
    assert_eq!(verified.artifact_kind(), "verification_report");
    assert_eq!(verified.report(), &first);
}

#[test]
fn adapter_artifact_gate_preserves_every_external_hold() {
    let manifest = strict_manifest_from_json(MANIFEST).expect("frozen manifest");
    let first = adapter_hold_report(
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect("frozen HOLD report");

    let mut partial_unearned_pass = first.clone();
    let conformance = partial_unearned_pass
        .checks
        .iter_mut()
        .find(|check| check.check_id == VerificationCheckIdV1::AdapterConformance)
        .expect("Adapter conformance check");
    conformance.status = CheckStatusV1::Pass;
    conformance.detail_code = "UNEARNED_PASS_WITH_OTHER_HOLDS".into();
    let partial_json =
        serde_json::to_vec(&partial_unearned_pass).expect("partial Adapter PASS JSON");
    let error = verified_adapter_hold_from_json(
        &partial_json,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect_err("one external check cannot become PASS while the overall report stays HOLD");
    assert!(error.contains("must remain Hold"));

    let mut forged_hold_reason = first.clone();
    forged_hold_reason
        .checks
        .iter_mut()
        .find(|check| check.check_id == VerificationCheckIdV1::RuntimeEgressZero)
        .expect("runtime egress check")
        .detail_code = "HOLD_WITHOUT_THE_REQUIRED_LINUX_COUNTER".into();
    let forged_json =
        serde_json::to_vec(&forged_hold_reason).expect("forged Adapter HOLD reason JSON");
    let error = verified_adapter_hold_from_json(
        &forged_json,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect_err("a generic HOLD cannot replace the frozen external-gate reason");
    assert!(error.contains("HOLD_LINUX_EGRESS_COUNTER"));

    let mut unearned_pass = first;
    for check in &mut unearned_pass.checks {
        if check.status == CheckStatusV1::Hold {
            check.status = CheckStatusV1::Pass;
        }
    }
    unearned_pass.abstention_reasons.clear();
    unearned_pass.disposition = VerificationDispositionV1::AwaitingHumanSeal;
    let unearned_json = serde_json::to_vec(&unearned_pass).expect("unearned Adapter PASS JSON");
    let error = verified_adapter_hold_from_json(
        &unearned_json,
        &manifest,
        REQUIREMENTS_COMMIT,
        IMPLEMENTATION_COMMIT,
        CANDIDATE,
    )
    .expect_err("Adapter cannot advance before its frozen runner and bundle exist");
    assert!(error.contains("HOLD artifact"));
}
