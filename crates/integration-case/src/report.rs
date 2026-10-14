use musubi_field_contracts::{
    ArtifactReferenceV1, ArtifactRoleV1, CandidateKindV1, CheckStatusV1, ClaimCeilingV1,
    ContractError, IntegrationCaseManifestV1, SCHEMA_VERSION_V1, Validate, VerificationCheckIdV1,
    VerificationCheckV1, VerificationDispositionV1, VerificationReportV1, canonical_json_bytes,
};

use crate::{
    DigestV1, ProfileCandidateArtifactV1, ProfileVerificationBundleV1, sha256_digest,
    verify_profile_case,
};

/// A verification artifact that has passed the candidate-kind-specific integrity gate.
///
/// The inner report is intentionally private: callers that create a human seal must carry this
/// wrapper, so a Profile report cannot be detached from its score-bound bundle first.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedReportArtifactV1 {
    report: VerificationReportV1,
    artifact_kind: &'static str,
}

impl VerifiedReportArtifactV1 {
    /// Borrow the canonical machine report after artifact verification.
    #[must_use]
    pub const fn report(&self) -> &VerificationReportV1 {
        &self.report
    }

    /// Stable machine-readable name of the accepted outer artifact.
    #[must_use]
    pub const fn artifact_kind(&self) -> &'static str {
        self.artifact_kind
    }
}

/// Parse the canonical public manifest and enforce all cross-field invariants.
///
/// # Errors
/// Returns a JSON or contract-validation error. The canonical type rejects unknown fixed fields.
pub fn strict_manifest_from_json(bytes: &[u8]) -> Result<IntegrationCaseManifestV1, String> {
    let manifest: IntegrationCaseManifestV1 =
        serde_json::from_slice(bytes).map_err(|error| format!("manifest JSON: {error}"))?;
    manifest.validate().map_err(|error| error.to_string())?;
    Ok(manifest)
}

/// Replay and parse the exact Adapter HOLD artifact without executing an Adapter candidate.
///
/// This kind-specific entry point rejects Profile manifests. Adapter cases currently accept only
/// their exact raw HOLD report because no Adapter bundle exists until the frozen runner is
/// implemented. The candidate bytes are hashed but never loaded or executed. The received report
/// must equal a freshly generated report from the candidate bytes and both declared source pins.
/// Profile callers must use [`verified_profile_report_from_json`] with all replay inputs.
///
/// # Errors
/// Returns an error for malformed JSON, a kind-inappropriate artifact, report drift, source-pin
/// drift, candidate drift, or manifest binding drift.
#[allow(
    clippy::too_many_arguments,
    reason = "the Adapter replay gate keeps every frozen report input explicit"
)]
pub fn verified_adapter_hold_from_json(
    bytes: &[u8],
    manifest: &IntegrationCaseManifestV1,
    requirements_commit: &str,
    implementation_commit: &str,
    candidate_bytes: &[u8],
) -> Result<VerifiedReportArtifactV1, String> {
    if manifest.candidate_kind != CandidateKindV1::Adapter {
        return Err("adapter HOLD verification requires candidate_kind=adapter".into());
    }
    let json: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|error| format!("report JSON: {error}"))?;
    if json.get("verification").is_some() {
        return Err("adapter case requires a VerificationReportV1 HOLD artifact".into());
    }
    let report: VerificationReportV1 = serde_json::from_value(json)
        .map_err(|error| format!("verification report JSON: {error}"))?;
    report
        .validate_against_manifest(manifest)
        .map_err(|error| error.to_string())?;
    validate_adapter_frozen_hold_artifact(&report)?;
    let replayed = adapter_hold_report(
        manifest,
        requirements_commit,
        implementation_commit,
        candidate_bytes,
    )
    .map_err(|error| error.to_string())?;
    validate_adapter_frozen_hold_artifact(&replayed)?;
    let received_canonical = canonical_json_bytes(&report).map_err(|error| error.to_string())?;
    let replayed_canonical = canonical_json_bytes(&replayed).map_err(|error| error.to_string())?;
    if received_canonical != replayed_canonical {
        return Err(
            "adapter HOLD report differs from deterministic replay of candidate bytes and source pins"
                .into(),
        );
    }
    Ok(VerifiedReportArtifactV1 {
        report: replayed,
        artifact_kind: "verification_report",
    })
}

/// Replay a stored Profile bundle from all frozen inputs before exposing it as verified.
///
/// Bundle digests provide corruption detection only. This gate reruns [`verify_profile_case`] from
/// the candidate bytes, sample bytes, oracle bytes, and declared source pins, then requires exact
/// canonical equality with the entire received bundle. Outer JSON whitespace and object key order
/// are ignored; every deserialized field must be identical. Rehashing attacker-controlled score
/// and report fields therefore cannot create a verified artifact.
///
/// # Errors
/// Returns an error for a non-Profile manifest, malformed or internally inconsistent bundle,
/// manifest drift, replay failure, or any byte-semantic difference from the replayed bundle.
#[allow(
    clippy::too_many_arguments,
    reason = "the replay gate keeps every frozen verifier input explicit at the trust boundary"
)]
pub fn verified_profile_report_from_json(
    bytes: &[u8],
    manifest: &IntegrationCaseManifestV1,
    requirements_commit: &str,
    implementation_commit: &str,
    candidate: &ProfileCandidateArtifactV1,
    sample_csv: &[u8],
    oracle_json: &[u8],
) -> Result<VerifiedReportArtifactV1, String> {
    if manifest.candidate_kind != CandidateKindV1::Profile {
        return Err("profile replay requires candidate_kind=profile".into());
    }
    let received = ProfileVerificationBundleV1::from_json(bytes)?;
    received.verify_against_manifest(manifest)?;
    let received_canonical = canonical_json_bytes(&received).map_err(|error| error.to_string())?;
    let replayed = verify_profile_case(
        manifest,
        requirements_commit,
        implementation_commit,
        candidate,
        sample_csv,
        oracle_json,
    )?;
    let replayed_canonical = canonical_json_bytes(&replayed).map_err(|error| error.to_string())?;
    if received_canonical != replayed_canonical {
        return Err(
            "profile verification bundle differs from deterministic replay of frozen inputs".into(),
        );
    }
    Ok(VerifiedReportArtifactV1 {
        report: replayed.verification().clone(),
        artifact_kind: "profile_verification_bundle",
    })
}

fn validate_adapter_frozen_hold_artifact(report: &VerificationReportV1) -> Result<(), String> {
    const EXPECTED_CHECKS: &[(VerificationCheckIdV1, CheckStatusV1, &str)] = &[
        (
            VerificationCheckIdV1::ManifestValid,
            CheckStatusV1::Pass,
            "MANIFEST_VALIDATED",
        ),
        (
            VerificationCheckIdV1::DeclaredBasePinMatch,
            CheckStatusV1::Pass,
            "DECLARED_BASE_PINS_MATCH",
        ),
        (
            VerificationCheckIdV1::InputDigestMatch,
            CheckStatusV1::Hold,
            "HOLD_FROZEN_INPUT_RUNNER",
        ),
        (
            VerificationCheckIdV1::OracleDigestMatch,
            CheckStatusV1::Hold,
            "HOLD_FROZEN_ORACLE_RUNNER",
        ),
        (
            VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist,
            CheckStatusV1::Hold,
            "HOLD_EXTERNAL_ARTIFACT_SET_AUDIT",
        ),
        (
            VerificationCheckIdV1::AdapterConformance,
            CheckStatusV1::Hold,
            "HOLD_EXTERNAL_FROZEN_RUNNER",
        ),
        (
            VerificationCheckIdV1::ArchitectureFitness,
            CheckStatusV1::Hold,
            "HOLD_EXTERNAL_FITNESS_RUNNER",
        ),
        (
            VerificationCheckIdV1::RuntimeEgressZero,
            CheckStatusV1::Hold,
            "HOLD_LINUX_EGRESS_COUNTER",
        ),
        (
            VerificationCheckIdV1::GeneratorVerifierDistinct,
            CheckStatusV1::Pass,
            "ACTORS_DISTINCT",
        ),
    ];
    if report.disposition != VerificationDispositionV1::Hold
        || report.abstention_reasons != ["HOLD_ADAPTER_EXECUTION_REQUIRES_FROZEN_EXTERNAL_RUNNER"]
    {
        return Err(
            "adapter case accepts only the frozen VerificationReportV1 HOLD artifact until the runner and bundle exist"
                .into(),
        );
    }
    for (check_id, expected_status, expected_code) in EXPECTED_CHECKS {
        let check = report
            .checks
            .iter()
            .find(|check| check.check_id == *check_id)
            .ok_or_else(|| format!("adapter HOLD artifact is missing {check_id:?}"))?;
        if check.status != *expected_status || check.detail_code != *expected_code {
            return Err(format!(
                "adapter HOLD artifact check {check_id:?} must remain {expected_status:?}/{expected_code}"
            ));
        }
    }
    Ok(())
}

/// Emit a truthful Adapter HOLD report without executing candidate code or manifest commands.
///
/// The frozen Adapter runner, architecture fitness runner, filesystem diff audit, and Linux egress
/// counter are intentionally external gates. This function cannot turn their absence into PASS.
///
/// # Errors
/// Returns a contract error if the manifest is invalid or is not an Adapter case.
#[allow(
    clippy::too_many_lines,
    reason = "the fixed Adapter HOLD vocabulary stays in one exhaustive auditable mapping"
)]
pub fn adapter_hold_report(
    manifest: &IntegrationCaseManifestV1,
    requirements_commit: &str,
    implementation_commit: &str,
    candidate_bytes: &[u8],
) -> Result<VerificationReportV1, ContractError> {
    manifest.validate()?;
    if manifest.candidate_kind != CandidateKindV1::Adapter {
        return Err(ContractError::InvalidField {
            field: "integration_case.candidate_kind",
            reason: "adapter report requires candidate_kind=adapter".to_owned(),
        });
    }
    let manifest_digest = manifest.manifest_digest()?;
    let candidate_digest = sha256_digest(candidate_bytes);
    let base_match = manifest.requirements_commit == requirements_commit
        && manifest.implementation_commit == implementation_commit;
    let checks = manifest
        .required_checks
        .iter()
        .copied()
        .map(|check_id| match check_id {
            VerificationCheckIdV1::ManifestValid => check(
                check_id,
                CheckStatusV1::Pass,
                "MANIFEST_VALIDATED",
                manifest_digest.clone(),
            ),
            VerificationCheckIdV1::DeclaredBasePinMatch => check(
                check_id,
                if base_match {
                    CheckStatusV1::Pass
                } else {
                    CheckStatusV1::Fail
                },
                if base_match {
                    "DECLARED_BASE_PINS_MATCH"
                } else {
                    "DECLARED_BASE_PINS_MISMATCH"
                },
                manifest_digest.clone(),
            ),
            VerificationCheckIdV1::GeneratorVerifierDistinct => check(
                check_id,
                CheckStatusV1::Pass,
                "ACTORS_DISTINCT",
                manifest_digest.clone(),
            ),
            VerificationCheckIdV1::InputDigestMatch => check(
                check_id,
                CheckStatusV1::Hold,
                "HOLD_FROZEN_INPUT_RUNNER",
                manifest_digest.clone(),
            ),
            VerificationCheckIdV1::OracleDigestMatch => check(
                check_id,
                CheckStatusV1::Hold,
                "HOLD_FROZEN_ORACLE_RUNNER",
                manifest.oracle.oracle_digest.clone(),
            ),
            VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist => check(
                check_id,
                CheckStatusV1::Hold,
                "HOLD_EXTERNAL_ARTIFACT_SET_AUDIT",
                candidate_digest.clone(),
            ),
            VerificationCheckIdV1::AdapterConformance => check(
                check_id,
                CheckStatusV1::Hold,
                "HOLD_EXTERNAL_FROZEN_RUNNER",
                candidate_digest.clone(),
            ),
            VerificationCheckIdV1::ArchitectureFitness => check(
                check_id,
                CheckStatusV1::Hold,
                "HOLD_EXTERNAL_FITNESS_RUNNER",
                candidate_digest.clone(),
            ),
            VerificationCheckIdV1::RuntimeEgressZero => check(
                check_id,
                CheckStatusV1::Hold,
                "HOLD_LINUX_EGRESS_COUNTER",
                candidate_digest.clone(),
            ),
            VerificationCheckIdV1::ProfileParse
            | VerificationCheckIdV1::ProfileAgainstSample
            | VerificationCheckIdV1::ProfileScore => check(
                check_id,
                CheckStatusV1::Fail,
                "WRONG_CHECK_FOR_ADAPTER_CASE",
                manifest_digest.clone(),
            ),
        })
        .collect();
    let report = VerificationReportV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        case_id: manifest.case_id.clone(),
        candidate_kind: CandidateKindV1::Adapter,
        generator_actor_id: manifest.generator_actor_id.clone(),
        verifier_actor_id: manifest.verifier_actor_id.clone(),
        input_manifest_digest: manifest_digest,
        candidate_digest,
        checks,
        abstention_reasons: vec![
            "HOLD_ADAPTER_EXECUTION_REQUIRES_FROZEN_EXTERNAL_RUNNER".to_owned(),
        ],
        disposition: VerificationDispositionV1::Hold,
        hot_reload_applied: false,
        runtime_load_authorized: false,
        claim_ceiling: ClaimCeilingV1::SimSelfAttest,
    };
    report.validate_against_manifest(manifest)?;
    Ok(report)
}

pub fn check(
    check_id: VerificationCheckIdV1,
    status: CheckStatusV1,
    detail_code: &str,
    digest: DigestV1,
) -> VerificationCheckV1 {
    VerificationCheckV1 {
        check_id,
        status,
        detail_code: detail_code.to_owned(),
        evidence: ArtifactReferenceV1 {
            digest,
            media_type: "application/json".to_owned(),
            role: ArtifactRoleV1::Receipt,
        },
    }
}

#[cfg(test)]
mod tests {
    use musubi_field_contracts::{
        ArtifactResidencyV1, ArtifactRoleV1, CandidateLoadPolicyV1, DataBoundaryV1,
        DataClassificationV1, ExecutionEnvironmentV1, ExportPolicyV1, ExternalInferencePolicyV1,
        NetworkPolicyV1, OracleReferenceV1, RetentionPolicyV1,
    };

    use super::*;

    fn artifact(value: &[u8]) -> ArtifactReferenceV1 {
        ArtifactReferenceV1 {
            digest: sha256_digest(value),
            media_type: "application/octet-stream".into(),
            role: ArtifactRoleV1::Fixture,
        }
    }

    fn adapter_manifest() -> IntegrationCaseManifestV1 {
        IntegrationCaseManifestV1 {
            schema_version: SCHEMA_VERSION_V1.into(),
            case_id: "adapter_synth_v1".into(),
            case_version: "1.0.0".into(),
            candidate_kind: CandidateKindV1::Adapter,
            requirements_commit: "1111111111111111111111111111111111111111".into(),
            implementation_commit: "2222222222222222222222222222222222222222".into(),
            data_boundary: DataBoundaryV1 {
                classification: DataClassificationV1::Synthetic,
                tenant_id: None,
                rights_digest: None,
                execution_environment: ExecutionEnvironmentV1::IsolatedSynthetic,
                external_inference: ExternalInferencePolicyV1::Denied,
                export_policy: ExportPolicyV1::Denied,
                raw_logs_residency: ArtifactResidencyV1::NotApplicable,
                labels_residency: ArtifactResidencyV1::NotApplicable,
                model_weights_residency: ArtifactResidencyV1::NotApplicable,
                retention: RetentionPolicyV1 {
                    maximum_days: Some(30),
                    deletion_authority_id: "fixture_owner".into(),
                },
            },
            oracle: OracleReferenceV1 {
                oracle_id: "adapter_oracle".into(),
                semantic_owner_id: "semantic_owner".into(),
                oracle_digest: sha256_digest(b"oracle"),
            },
            acceptance_owner_id: "acceptance_owner".into(),
            input_artifacts: vec![artifact(b"input")],
            candidate_write_allowlist: vec!["candidate/src".into()],
            allowed_tools: vec!["cargo_test".into()],
            generator_actor_id: "generator".into(),
            verifier_actor_id: "verifier".into(),
            required_checks: vec![
                VerificationCheckIdV1::ManifestValid,
                VerificationCheckIdV1::DeclaredBasePinMatch,
                VerificationCheckIdV1::InputDigestMatch,
                VerificationCheckIdV1::OracleDigestMatch,
                VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist,
                VerificationCheckIdV1::AdapterConformance,
                VerificationCheckIdV1::ArchitectureFitness,
                VerificationCheckIdV1::RuntimeEgressZero,
                VerificationCheckIdV1::GeneratorVerifierDistinct,
            ],
            network_policy: NetworkPolicyV1::DenyAll,
            load_policy: CandidateLoadPolicyV1::NextStartAfterHumanSeal,
            claim_ceiling: ClaimCeilingV1::SimSelfAttest,
        }
    }

    #[test]
    fn adapter_is_explicit_hold_without_external_runners() {
        let manifest = adapter_manifest();
        let mut report = adapter_hold_report(
            &manifest,
            "1111111111111111111111111111111111111111",
            "2222222222222222222222222222222222222222",
            b"candidate",
        )
        .expect("report");
        assert_eq!(report.disposition, VerificationDispositionV1::Hold);
        assert!(report.checks.iter().any(|check| {
            check.check_id == VerificationCheckIdV1::RuntimeEgressZero
                && check.status == CheckStatusV1::Hold
        }));
        report
            .validate_against_manifest(&manifest)
            .expect("binding");

        let bytes = serde_json::to_vec(&report).expect("stored Adapter HOLD");
        let verified = verified_adapter_hold_from_json(
            &bytes,
            &manifest,
            &manifest.requirements_commit,
            &manifest.implementation_commit,
            b"candidate",
        )
        .expect("exact Adapter HOLD remains accepted");
        assert_eq!(verified.report(), &report);

        let input_check = report
            .checks
            .iter_mut()
            .find(|check| check.check_id == VerificationCheckIdV1::InputDigestMatch)
            .expect("input check");
        input_check.status = CheckStatusV1::Pass;
        input_check.detail_code = "INPUT_MATCH".into();
        let changed = serde_json::to_vec(&report).expect("changed Adapter report");
        assert!(
            verified_adapter_hold_from_json(
                &changed,
                &manifest,
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                b"candidate",
            )
            .is_err()
        );

        let original = adapter_hold_report(
            &manifest,
            &manifest.requirements_commit,
            &manifest.implementation_commit,
            b"candidate",
        )
        .expect("report");
        let original_bytes = serde_json::to_vec(&original).expect("stored Adapter HOLD");
        assert!(
            verified_adapter_hold_from_json(
                &original_bytes,
                &manifest,
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                b"changed candidate",
            )
            .is_err()
        );
        assert!(
            verified_adapter_hold_from_json(
                &original_bytes,
                &manifest,
                "0000000000000000000000000000000000000000",
                &manifest.implementation_commit,
                b"candidate",
            )
            .is_err()
        );

        let mut forged = original;
        forged.candidate_digest = sha256_digest(b"forged candidate");
        for check in &mut forged.checks {
            if matches!(
                check.check_id,
                VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist
                    | VerificationCheckIdV1::AdapterConformance
                    | VerificationCheckIdV1::ArchitectureFitness
                    | VerificationCheckIdV1::RuntimeEgressZero
            ) {
                check.evidence.digest = forged.candidate_digest.clone();
            }
        }
        let forged_bytes = serde_json::to_vec(&forged).expect("fully rebound forgery");
        assert!(
            verified_adapter_hold_from_json(
                &forged_bytes,
                &manifest,
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                b"candidate",
            )
            .is_err()
        );
    }
}
