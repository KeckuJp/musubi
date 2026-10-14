//! Deterministic, offline contract for Profile/Adapter candidate generation and verification.
//!
//! This crate intentionally does not invoke an agent, execute manifest-provided commands, load a
//! candidate into a runtime, or create a human approval. It verifies bounded artifacts and emits a
//! machine report whose maximum claim is [`ClaimCeilingV1::SimSelfAttest`].

#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]

mod profile;
mod report;

pub use musubi_field_contracts::{
    ArtifactReferenceV1, ArtifactResidencyV1, ArtifactRoleV1, CandidateKindV1,
    CandidateLoadPolicyV1, CheckStatusV1, ClaimCeilingV1, DataBoundaryV1, DataClassificationV1,
    DigestV1, ExecutionEnvironmentV1, ExportPolicyV1, ExternalInferencePolicyV1,
    HumanSealDecisionV1, HumanSealV1, IdentityBasisV1, IntegrationCaseManifestV1, NetworkPolicyV1,
    OracleReferenceV1, RetentionPolicyV1, SCHEMA_VERSION_V1, Validate, VerificationCheckIdV1,
    VerificationCheckV1, VerificationDispositionV1, VerificationReportV1, canonical_sha256,
};
pub use profile::{
    BindingClass, BindingResultV1, ExpectationOracleV1, ProfileCandidateArtifactV1,
    ProfileOracleV1, ProfileScoreV1, ProfileVerificationBundleV1, RoleBindingOracleV1,
    load_profile_candidate, verify_profile_case,
};
pub use report::{
    VerifiedReportArtifactV1, adapter_hold_report, strict_manifest_from_json,
    verified_adapter_hold_from_json, verified_profile_report_from_json,
};

/// SHA-256 over bytes, encoded as `sha256:<64 lowercase hex>`.
#[must_use]
pub fn sha256_digest(bytes: &[u8]) -> DigestV1 {
    DigestV1::sha256(bytes)
}

pub(crate) fn valid_relative_path(value: &str) -> bool {
    if value.is_empty() || value.starts_with('/') || value.starts_with('\\') {
        return false;
    }
    let mut saw_component = false;
    for part in value.split(['/', '\\']) {
        if part.is_empty() || part == "." || part == ".." {
            return false;
        }
        saw_component = true;
    }
    saw_component
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_shape_is_fixed() {
        assert_eq!(
            sha256_digest(b"abc").as_str(),
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn path_guard_rejects_absolute_and_parent_paths() {
        assert!(valid_relative_path("candidate/profile.toml"));
        assert!(!valid_relative_path("/tmp/profile.toml"));
        assert!(!valid_relative_path("candidate/../oracle.toml"));
        assert!(!valid_relative_path("candidate//profile.toml"));
    }
}
