use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use musubi_field_contracts::{
    CandidateKindV1, CheckStatusV1, ClaimCeilingV1, DigestV1, IntegrationCaseManifestV1,
    SCHEMA_VERSION_V1, Validate, VerificationCheckIdV1, VerificationDispositionV1,
    VerificationReportV1, canonical_sha256,
};
use musubi_reference_readers::{profile::parse_profile, reader_for};
use musubi_reference_types::FamilyProfile;
use serde::{Deserialize, Serialize};

use crate::report::check;
use crate::{sha256_digest, valid_relative_path};

/// Frozen semantic oracle for one synthetic Profile candidate.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileOracleV1 {
    pub schema_version: u32,
    pub bindings: Vec<RoleBindingOracleV1>,
    pub field_units: BTreeMap<String, String>,
    pub expectations: Vec<ExpectationOracleV1>,
}

/// Exact cadence semantics approved by the frozen semantic owner.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExpectationOracleV1 {
    pub channel: String,
    pub cadence_ms: u64,
    pub grace_k: u32,
    pub frozen_ms: Option<u64>,
}

/// Acceptable source fields for one role. Unreachable fields do not enter the denominator.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RoleBindingOracleV1 {
    pub role: String,
    pub acceptable_fields: Vec<String>,
    #[serde(default)]
    pub unreachable_fields: Vec<String>,
    #[serde(default = "default_required")]
    pub required: bool,
}

const fn default_required() -> bool {
    true
}

/// Classification of one candidate binding against the sample and frozen oracle.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BindingClass {
    Match,
    Miss,
    MisMap,
    Phantom,
    Unreachable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BindingResultV1 {
    pub role: String,
    pub field: String,
    pub class: BindingClass,
    pub consumed: bool,
}

/// Deterministic Profile score. Percentages are deliberately omitted.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileScoreV1 {
    pub reader_format: String,
    pub reader_observation_count: u32,
    pub unit_matched: u32,
    pub unit_mismatched: u32,
    pub cadence_matched: u32,
    pub cadence_mismatched: u32,
    pub matched: u32,
    pub missed: u32,
    pub mis_mapped: u32,
    pub phantom: u32,
    pub unreachable: u32,
    pub consumed: u32,
    pub inert: u32,
    pub bindings: Vec<BindingResultV1>,
}

#[derive(Serialize)]
struct CandidateArtifactDigestV1<'a> {
    relative_path: &'a str,
    digest: &'a DigestV1,
}

/// A single-file Profile candidate loaded from an isolated artifact root.
///
/// Construction requires the complete regular-file set under that root to equal the exact
/// manifest allowlist. Symlinks and extra files are rejected before candidate bytes are parsed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProfileCandidateArtifactV1 {
    relative_path: String,
    bytes: Vec<u8>,
    candidate_digest: DigestV1,
    artifact_set_digest: DigestV1,
}

impl ProfileCandidateArtifactV1 {
    /// Construct the same bounded artifact representation from in-memory fixture bytes.
    ///
    /// # Errors
    /// Returns an error unless the manifest declares exactly this one relative candidate file.
    pub(crate) fn from_single(
        manifest: &IntegrationCaseManifestV1,
        relative_path: &str,
        bytes: &[u8],
    ) -> Result<Self, String> {
        if manifest.candidate_kind != CandidateKindV1::Profile {
            return Err("profile candidate requires candidate_kind=profile".into());
        }
        if !valid_relative_path(relative_path)
            || manifest.candidate_write_allowlist != [relative_path.to_owned()]
        {
            return Err(
                "Profile v1 requires one exact relative file in candidate_write_allowlist".into(),
            );
        }
        let candidate_digest = sha256_digest(bytes);
        let artifact_set_digest = canonical_sha256(&[CandidateArtifactDigestV1 {
            relative_path,
            digest: &candidate_digest,
        }])
        .map_err(|error| error.to_string())?;
        Ok(Self {
            relative_path: relative_path.to_owned(),
            bytes: bytes.to_vec(),
            candidate_digest,
            artifact_set_digest,
        })
    }
}

/// Load the complete Profile candidate artifact root without following symlinks.
///
/// # Errors
/// Returns an error for unreadable paths, symlinks, non-UTF-8 relative paths, extra files, or a
/// file set that does not exactly equal the manifest allowlist.
pub fn load_profile_candidate(
    root: &Path,
    manifest: &IntegrationCaseManifestV1,
) -> Result<ProfileCandidateArtifactV1, String> {
    let mut files = Vec::new();
    collect_regular_files(root, root, &mut files)?;
    files.sort();
    if files.len() != 1 {
        return Err("Profile v1 candidate root must contain exactly one regular file".into());
    }
    let path = &files[0];
    let relative = path
        .strip_prefix(root)
        .map_err(|_| "candidate path is outside candidate root")?
        .to_str()
        .ok_or("candidate relative path is not UTF-8")?;
    let bytes = std::fs::read(path).map_err(|error| format!("{}: {error}", path.display()))?;
    ProfileCandidateArtifactV1::from_single(manifest, relative, &bytes)
}

fn collect_regular_files(
    root: &Path,
    current: &Path,
    output: &mut Vec<PathBuf>,
) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(current)
        .map_err(|error| format!("{}: {error}", current.display()))?;
    if metadata.file_type().is_symlink() {
        return Err(format!(
            "candidate symlink is forbidden: {}",
            current.display()
        ));
    }
    if metadata.is_file() {
        if current == root {
            return Err("candidate root must be a directory".into());
        }
        output.push(current.to_path_buf());
        return Ok(());
    }
    if !metadata.is_dir() {
        return Err(format!(
            "unsupported candidate artifact: {}",
            current.display()
        ));
    }
    let mut entries = std::fs::read_dir(current)
        .map_err(|error| format!("{}: {error}", current.display()))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("{}: {error}", current.display()))?;
    entries.sort_by_key(std::fs::DirEntry::file_name);
    for entry in entries {
        collect_regular_files(root, &entry.path(), output)?;
    }
    Ok(())
}

/// Canonical machine report plus the score artifact bound by its `PROFILE_SCORE` evidence digest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProfileVerificationBundleV1 {
    schema_version: String,
    verification: VerificationReportV1,
    profile_score: Option<ProfileScoreV1>,
    bundle_digest: DigestV1,
}

/// Private deserialization surface for an untrusted stored bundle. Public callers can only obtain
/// a bundle from the deterministic verifier and inspect it through read-only accessors.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileVerificationBundleWireV1 {
    schema_version: String,
    verification: VerificationReportV1,
    profile_score: Option<ProfileScoreV1>,
    bundle_digest: DigestV1,
}

#[derive(Serialize)]
struct BundlePayloadRef<'a> {
    schema_version: &'a str,
    verification: &'a VerificationReportV1,
    profile_score: &'a Option<ProfileScoreV1>,
}

impl ProfileVerificationBundleV1 {
    fn new(
        verification: VerificationReportV1,
        profile_score: Option<ProfileScoreV1>,
    ) -> Result<Self, String> {
        let bundle_digest = canonical_sha256(&BundlePayloadRef {
            schema_version: SCHEMA_VERSION_V1,
            verification: &verification,
            profile_score: &profile_score,
        })
        .map_err(|error| error.to_string())?;
        Ok(Self {
            schema_version: SCHEMA_VERSION_V1.to_owned(),
            verification,
            profile_score,
            bundle_digest,
        })
    }

    /// Schema version carried by this bundle.
    #[must_use]
    pub fn schema_version(&self) -> &str {
        &self.schema_version
    }

    /// Read-only access to the canonical machine report.
    #[must_use]
    pub const fn verification(&self) -> &VerificationReportV1 {
        &self.verification
    }

    /// Read-only access to the deterministic score, when parsing reached the scoring stage.
    #[must_use]
    pub const fn profile_score(&self) -> Option<&ProfileScoreV1> {
        self.profile_score.as_ref()
    }

    /// Digest over the complete bundle payload.
    #[must_use]
    pub const fn bundle_digest(&self) -> &DigestV1 {
        &self.bundle_digest
    }

    /// Parse and verify bundle integrity plus the canonical machine report.
    ///
    /// This is deliberately crate-private: internal consistency is not proof that the report was
    /// produced from the fixed candidate, sample, oracle, and source pins. The public acceptance
    /// path must replay [`verify_profile_case`] and compare the entire bundle.
    ///
    /// # Errors
    /// Returns an error for malformed JSON, invalid report, or digest mismatch.
    pub(crate) fn from_json(bytes: &[u8]) -> Result<Self, String> {
        let wire: ProfileVerificationBundleWireV1 = serde_json::from_slice(bytes)
            .map_err(|error| format!("profile report JSON: {error}"))?;
        let value = Self {
            schema_version: wire.schema_version,
            verification: wire.verification,
            profile_score: wire.profile_score,
            bundle_digest: wire.bundle_digest,
        };
        value.verify()?;
        Ok(value)
    }

    /// Verify the report and score-bundle digest.
    ///
    /// # Errors
    /// Returns an error if any machine invariant or digest differs.
    pub fn verify(&self) -> Result<(), String> {
        if self.schema_version != SCHEMA_VERSION_V1 {
            return Err("profile verification bundle schema_version must be 1".into());
        }
        self.verification
            .validate()
            .map_err(|error| error.to_string())?;
        let expected_score_digest = match &self.profile_score {
            Some(score) => canonical_sha256(score).map_err(|error| error.to_string())?,
            None => self.verification.candidate_digest.clone(),
        };
        for check_id in [
            VerificationCheckIdV1::ProfileAgainstSample,
            VerificationCheckIdV1::ProfileScore,
        ] {
            let check = self
                .verification
                .checks
                .iter()
                .find(|check| check.check_id == check_id)
                .ok_or("profile bundle is missing a score-bound check")?;
            if check.evidence.digest != expected_score_digest {
                return Err("profile score evidence digest mismatch".into());
            }
        }
        if self.verification.disposition == VerificationDispositionV1::AwaitingHumanSeal
            && self.profile_score.is_none()
        {
            return Err("all-pass profile bundle must include its bound score".into());
        }
        let expected = canonical_sha256(&BundlePayloadRef {
            schema_version: &self.schema_version,
            verification: &self.verification,
            profile_score: &self.profile_score,
        })
        .map_err(|error| error.to_string())?;
        if expected != self.bundle_digest {
            return Err("profile verification bundle digest mismatch".into());
        }
        Ok(())
    }

    /// Verify bundle integrity and bind the report to the complete frozen manifest.
    ///
    /// # Errors
    /// Returns an error for bundle drift, manifest drift, actor drift, or incomplete checks.
    pub(crate) fn verify_against_manifest(
        &self,
        manifest: &IntegrationCaseManifestV1,
    ) -> Result<(), String> {
        self.verify()?;
        self.verification
            .validate_against_manifest(manifest)
            .map_err(|error| error.to_string())
    }
}

/// Verify a Profile TOML against a synthetic sample header and frozen JSON oracle.
///
/// The candidate path, base pins, sample bytes, and oracle bytes are checked against the canonical
/// manifest before semantic scoring. All-pass output still waits for a separate human seal.
///
/// # Errors
/// Returns a string error for an invalid manifest, oracle, sample, or report construction.
#[allow(
    clippy::too_many_arguments,
    clippy::too_many_lines,
    reason = "the fixed verification vocabulary stays in one exhaustive auditable mapping"
)]
pub fn verify_profile_case(
    manifest: &IntegrationCaseManifestV1,
    requirements_commit: &str,
    implementation_commit: &str,
    candidate: &ProfileCandidateArtifactV1,
    sample_csv: &[u8],
    oracle_json: &[u8],
) -> Result<ProfileVerificationBundleV1, String> {
    manifest.validate().map_err(|error| error.to_string())?;
    if manifest.candidate_kind != CandidateKindV1::Profile {
        return Err("profile verifier requires candidate_kind=profile".into());
    }

    let manifest_digest = manifest
        .manifest_digest()
        .map_err(|error| error.to_string())?;
    let sample_digest = sha256_digest(sample_csv);
    let oracle_digest = sha256_digest(oracle_json);
    let candidate_digest = candidate.candidate_digest.clone();
    let input_match =
        manifest.input_artifacts.len() == 1 && manifest.input_artifacts[0].digest == sample_digest;
    let oracle_match = manifest.oracle.oracle_digest == oracle_digest;
    let base_match = manifest.requirements_commit == requirements_commit
        && manifest.implementation_commit == implementation_commit;
    let candidate_allowed = manifest.candidate_write_allowlist == [candidate.relative_path.clone()];

    let candidate_text = std::str::from_utf8(&candidate.bytes).ok();
    let parsed_profile = candidate_text.map(|text| parse_profile(text, "candidate"));
    let parse_pass = parsed_profile.as_ref().is_some_and(Result::is_ok);
    let oracle: ProfileOracleV1 = serde_json::from_slice(oracle_json)
        .map_err(|error| format!("profile oracle JSON: {error}"))?;
    validate_oracle(&oracle)?;
    let headers = parse_csv_header(sample_csv)?;
    let reader_run = parsed_profile
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .and_then(|profile| {
            reader_for(&profile.format).map(|reader| reader.read(profile, sample_csv))
        });
    let reader_observation_count = reader_run
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .map_or(0, Vec::len);
    let reader_pass = reader_observation_count > 0;
    let mut consumed_fields: BTreeSet<String> = reader_run
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .into_iter()
        .flatten()
        .flat_map(|observation| observation.fields.iter().map(|(field, _)| field.clone()))
        .collect();
    if reader_pass
        && let Some(profile) = parsed_profile
            .as_ref()
            .and_then(|result| result.as_ref().ok())
        && profile.format == "edgetx_csv"
    {
        consumed_fields.insert(profile.fields.time.clone());
    }
    let score = parsed_profile
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .map(|profile| {
            score_profile(
                profile,
                &headers,
                &oracle,
                u32::try_from(reader_observation_count).unwrap_or(u32::MAX),
                &consumed_fields,
            )
        });
    let against_pass = reader_pass && score.as_ref().is_some_and(|value| value.phantom == 0);
    let score_pass = score.as_ref().is_some_and(|value| {
        reader_pass
            && value.missed == 0
            && value.mis_mapped == 0
            && value.phantom == 0
            && value.inert == 0
            && value.unit_mismatched == 0
            && value.cadence_mismatched == 0
    });
    let score_digest = match &score {
        Some(value) => canonical_sha256(value).map_err(|error| error.to_string())?,
        None => candidate_digest.clone(),
    };

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
            VerificationCheckIdV1::DeclaredBasePinMatch => boolean_check(
                check_id,
                base_match,
                "DECLARED_BASE_PINS_MATCH",
                "DECLARED_BASE_PINS_MISMATCH",
                manifest_digest.clone(),
            ),
            VerificationCheckIdV1::InputDigestMatch => boolean_check(
                check_id,
                input_match,
                "INPUT_MATCH",
                "INPUT_MISMATCH",
                sample_digest.clone(),
            ),
            VerificationCheckIdV1::OracleDigestMatch => boolean_check(
                check_id,
                oracle_match,
                "ORACLE_MATCH",
                "ORACLE_MISMATCH",
                oracle_digest.clone(),
            ),
            VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist => boolean_check(
                check_id,
                candidate_allowed,
                "CANDIDATE_ARTIFACT_SET_ALLOWED",
                "CANDIDATE_ARTIFACT_SET_OUTSIDE_ALLOWLIST",
                candidate.artifact_set_digest.clone(),
            ),
            VerificationCheckIdV1::ProfileParse => boolean_check(
                check_id,
                parse_pass,
                "PROFILE_PARSED",
                "PROFILE_PARSE_FAILED",
                candidate_digest.clone(),
            ),
            VerificationCheckIdV1::ProfileAgainstSample => boolean_check(
                check_id,
                against_pass,
                "READER_SAMPLE_AND_BINDINGS_MATCH",
                "READER_SAMPLE_OR_BINDING_FAILURE",
                score_digest.clone(),
            ),
            VerificationCheckIdV1::ProfileScore => boolean_check(
                check_id,
                score_pass,
                "PROFILE_MATCHES_ORACLE",
                "PROFILE_SCORE_MISMATCH",
                score_digest.clone(),
            ),
            VerificationCheckIdV1::GeneratorVerifierDistinct => check(
                check_id,
                CheckStatusV1::Pass,
                "ACTORS_DISTINCT",
                manifest_digest.clone(),
            ),
            VerificationCheckIdV1::AdapterConformance
            | VerificationCheckIdV1::ArchitectureFitness
            | VerificationCheckIdV1::RuntimeEgressZero => check(
                check_id,
                CheckStatusV1::Fail,
                "WRONG_CHECK_FOR_PROFILE_CASE",
                manifest_digest.clone(),
            ),
        })
        .collect::<Vec<_>>();
    let disposition = if checks.iter().any(|item| item.status == CheckStatusV1::Fail) {
        VerificationDispositionV1::Fail
    } else if checks.iter().any(|item| item.status == CheckStatusV1::Hold) {
        VerificationDispositionV1::Hold
    } else {
        VerificationDispositionV1::AwaitingHumanSeal
    };
    let report = VerificationReportV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        case_id: manifest.case_id.clone(),
        candidate_kind: CandidateKindV1::Profile,
        generator_actor_id: manifest.generator_actor_id.clone(),
        verifier_actor_id: manifest.verifier_actor_id.clone(),
        input_manifest_digest: manifest_digest,
        candidate_digest,
        checks,
        abstention_reasons: Vec::new(),
        disposition,
        hot_reload_applied: false,
        runtime_load_authorized: false,
        claim_ceiling: ClaimCeilingV1::SimSelfAttest,
    };
    report
        .validate_against_manifest(manifest)
        .map_err(|error| error.to_string())?;
    ProfileVerificationBundleV1::new(report, score)
}

fn boolean_check(
    check_id: VerificationCheckIdV1,
    pass: bool,
    pass_code: &str,
    fail_code: &str,
    digest: DigestV1,
) -> musubi_field_contracts::VerificationCheckV1 {
    check(
        check_id,
        if pass {
            CheckStatusV1::Pass
        } else {
            CheckStatusV1::Fail
        },
        if pass { pass_code } else { fail_code },
        digest,
    )
}

fn validate_oracle(oracle: &ProfileOracleV1) -> Result<(), String> {
    if oracle.schema_version != 1
        || oracle.bindings.is_empty()
        || oracle.field_units.is_empty()
        || oracle.expectations.is_empty()
    {
        return Err(
            "profile oracle requires schema_version=1, bindings, field_units, and expectations"
                .into(),
        );
    }
    let mut roles = BTreeSet::new();
    for binding in &oracle.bindings {
        if !known_role(&binding.role) || !roles.insert(binding.role.as_str()) {
            return Err(format!(
                "unknown or duplicate oracle role: {}",
                binding.role
            ));
        }
        if binding.acceptable_fields.is_empty() && binding.unreachable_fields.is_empty() {
            return Err(format!("oracle role has no fields: {}", binding.role));
        }
    }
    let expected_fields: BTreeSet<_> = oracle
        .bindings
        .iter()
        .flat_map(|binding| binding.acceptable_fields.iter().map(String::as_str))
        .collect();
    let unit_fields: BTreeSet<_> = oracle.field_units.keys().map(String::as_str).collect();
    if expected_fields != unit_fields
        || oracle
            .field_units
            .values()
            .any(|unit| unit.trim().is_empty())
    {
        return Err("profile oracle field_units must exactly cover acceptable fields".into());
    }
    let mut expectation_channels = BTreeSet::new();
    for expectation in &oracle.expectations {
        if musubi_reference_types::ChannelId::parse(&expectation.channel).is_none()
            || expectation.cadence_ms == 0
            || expectation.grace_k == 0
            || !expectation_channels.insert(expectation.channel.as_str())
        {
            return Err("profile oracle has an invalid or duplicate expectation".into());
        }
    }
    Ok(())
}

fn known_role(role: &str) -> bool {
    matches!(
        role,
        "time" | "status" | "link" | "battery" | "gps" | "health"
    )
}

#[allow(clippy::too_many_lines)]
fn score_profile(
    profile: &FamilyProfile,
    headers: &BTreeSet<String>,
    oracle: &ProfileOracleV1,
    reader_observation_count: u32,
    consumed_fields: &BTreeSet<String>,
) -> ProfileScoreV1 {
    let candidate = candidate_bindings(profile);
    let expected_by_field: BTreeMap<&str, &str> = oracle
        .bindings
        .iter()
        .flat_map(|binding| {
            binding
                .acceptable_fields
                .iter()
                .map(move |field| (field.as_str(), binding.role.as_str()))
        })
        .collect();
    let unreachable: BTreeSet<&str> = oracle
        .bindings
        .iter()
        .flat_map(|binding| binding.unreachable_fields.iter().map(String::as_str))
        .collect();

    let mut results = Vec::new();
    let mut matched_expected = BTreeSet::new();
    for (role, fields) in &candidate {
        for field in fields {
            let class = if unreachable.contains(field.as_str()) {
                BindingClass::Unreachable
            } else if !headers.contains(field) {
                BindingClass::Phantom
            } else if expected_by_field
                .get(field.as_str())
                .is_some_and(|gold| *gold == role)
            {
                matched_expected.insert((role.clone(), field.clone()));
                BindingClass::Match
            } else if expected_by_field.contains_key(field.as_str()) {
                BindingClass::MisMap
            } else {
                BindingClass::Phantom
            };
            results.push(BindingResultV1 {
                role: role.clone(),
                field: field.clone(),
                class,
                consumed: consumed_fields.contains(field),
            });
        }
    }

    for binding in &oracle.bindings {
        if !binding.required {
            continue;
        }
        let has_match = binding
            .acceptable_fields
            .iter()
            .any(|field| matched_expected.contains(&(binding.role.clone(), field.clone())));
        if !has_match {
            results.push(BindingResultV1 {
                role: binding.role.clone(),
                field: "<required-binding>".into(),
                class: BindingClass::Miss,
                consumed: false,
            });
        }
    }
    results.sort_by(|left, right| (&left.role, &left.field).cmp(&(&right.role, &right.field)));

    let count = |class| {
        u32::try_from(
            results
                .iter()
                .filter(|result| result.class == class)
                .count(),
        )
        .unwrap_or(u32::MAX)
    };
    let unit_matched = oracle
        .field_units
        .iter()
        .filter(|(field, unit)| profile.field_units.get(*field) == Some(*unit))
        .count();
    let unit_keys: BTreeSet<_> = oracle
        .field_units
        .keys()
        .chain(profile.field_units.keys())
        .collect();
    let expected_cadence: BTreeSet<_> = oracle
        .expectations
        .iter()
        .map(|expectation| {
            (
                expectation.channel.as_str(),
                expectation.cadence_ms,
                expectation.grace_k,
                expectation.frozen_ms,
            )
        })
        .collect();
    let actual_cadence: BTreeSet<_> = profile
        .expectations
        .iter()
        .map(|expectation| {
            (
                expectation.channel.as_str(),
                expectation.cadence_ms,
                expectation.grace_k,
                expectation.frozen_ms,
            )
        })
        .collect();
    let cadence_matched = expected_cadence.intersection(&actual_cadence).count();
    let cadence_total = expected_cadence.union(&actual_cadence).count();
    ProfileScoreV1 {
        reader_format: profile.format.clone(),
        reader_observation_count,
        unit_matched: u32::try_from(unit_matched).unwrap_or(u32::MAX),
        unit_mismatched: u32::try_from(unit_keys.len().saturating_sub(unit_matched))
            .unwrap_or(u32::MAX),
        cadence_matched: u32::try_from(cadence_matched).unwrap_or(u32::MAX),
        cadence_mismatched: u32::try_from(cadence_total.saturating_sub(cadence_matched))
            .unwrap_or(u32::MAX),
        matched: count(BindingClass::Match),
        missed: count(BindingClass::Miss),
        mis_mapped: count(BindingClass::MisMap),
        phantom: count(BindingClass::Phantom),
        unreachable: count(BindingClass::Unreachable),
        consumed: u32::try_from(results.iter().filter(|result| result.consumed).count())
            .unwrap_or(u32::MAX),
        inert: u32::try_from(
            results
                .iter()
                .filter(|result| result.class != BindingClass::Miss && !result.consumed)
                .count(),
        )
        .unwrap_or(u32::MAX),
        bindings: results,
    }
}

fn candidate_bindings(profile: &FamilyProfile) -> BTreeMap<String, Vec<String>> {
    BTreeMap::from([
        ("time".into(), vec![profile.fields.time.clone()]),
        ("status".into(), profile.fields.status.clone()),
        ("link".into(), profile.fields.link.clone()),
        ("battery".into(), profile.fields.battery.clone()),
        ("gps".into(), profile.fields.gps.clone()),
        ("health".into(), profile.fields.health.clone()),
    ])
}

fn parse_csv_header(bytes: &[u8]) -> Result<BTreeSet<String>, String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "sample CSV is not UTF-8")?;
    let line = text.lines().next().ok_or("sample CSV has no header")?;
    let mut fields = Vec::new();
    let mut current = String::new();
    let mut chars = line.chars().peekable();
    let mut quoted = false;
    while let Some(ch) = chars.next() {
        match ch {
            '"' if quoted && chars.peek() == Some(&'"') => {
                current.push('"');
                let _ = chars.next();
            }
            '"' => quoted = !quoted,
            ',' if !quoted => {
                fields.push(current.trim().to_string());
                current.clear();
            }
            _ => current.push(ch),
        }
    }
    if quoted {
        return Err("sample CSV header has an unterminated quote".into());
    }
    fields.push(current.trim().to_string());
    if fields.iter().any(String::is_empty) {
        return Err("sample CSV header contains an empty field".into());
    }
    Ok(fields.into_iter().collect())
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};

    use musubi_field_contracts::{
        ArtifactReferenceV1, ArtifactResidencyV1, ArtifactRoleV1, CandidateLoadPolicyV1,
        DataBoundaryV1, DataClassificationV1, ExecutionEnvironmentV1, ExportPolicyV1,
        ExternalInferencePolicyV1, NetworkPolicyV1, OracleReferenceV1, RetentionPolicyV1,
    };

    use super::*;

    fn scratch_dir(label: &str) -> PathBuf {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        loop {
            let candidate = std::env::temp_dir().join(format!(
                "musubi_profile_case_{}_{}_{}",
                std::process::id(),
                label,
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            if std::fs::create_dir(&candidate).is_ok() {
                return candidate;
            }
        }
    }

    const PROFILE: &str = r#"
profile_id = "synthetic_profile"
version = "1"
family = "fpv"
source_role = "handset"
format = "edgetx_csv"
extensions = [".csv"]
default_clock_basis = "host_received"
channels = ["link_stats"]
[fields]
time = "tmr10ms"
status = ["Mode"]
link = ["RQly(%)"]

[units]
tmr10ms = "10_ms_tick"
Mode = "enum"
"RQly(%)" = "percent"

[[expectations]]
id = "synthetic_link_100ms"
channel = "link_stats"
cadence_ms = 100
grace_k = 3
"#;

    fn artifact(bytes: &[u8]) -> ArtifactReferenceV1 {
        ArtifactReferenceV1 {
            digest: sha256_digest(bytes),
            media_type: "text/csv".into(),
            role: ArtifactRoleV1::Fixture,
        }
    }

    fn fixture() -> (IntegrationCaseManifestV1, Vec<u8>, Vec<u8>) {
        let sample = b"tmr10ms,Mode,RQly(%)\n0,AUTO,90\n10,GUIDED,88\n".to_vec();
        let oracle = br#"{
          "schema_version": 1,
          "bindings": [
            {"role":"time","acceptable_fields":["tmr10ms"]},
            {"role":"status","acceptable_fields":["Mode"]},
            {"role":"link","acceptable_fields":["RQly(%)"]}
          ],
          "field_units": {
            "tmr10ms": "10_ms_tick",
            "Mode": "enum",
            "RQly(%)": "percent"
          },
          "expectations": [
            {"channel":"link_stats","cadence_ms":100,"grace_k":3,"frozen_ms":null}
          ]
        }"#
        .to_vec();
        let manifest = IntegrationCaseManifestV1 {
            schema_version: SCHEMA_VERSION_V1.into(),
            case_id: "profile_synth_v1".into(),
            case_version: "1.0.0".into(),
            candidate_kind: CandidateKindV1::Profile,
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
                oracle_id: "profile_oracle".into(),
                semantic_owner_id: "semantic_owner".into(),
                oracle_digest: sha256_digest(&oracle),
            },
            acceptance_owner_id: "acceptance_owner".into(),
            input_artifacts: vec![artifact(&sample)],
            candidate_write_allowlist: vec!["profile.toml".into()],
            allowed_tools: vec!["profile_verify".into()],
            generator_actor_id: "generator".into(),
            verifier_actor_id: "independent_verifier".into(),
            required_checks: vec![
                VerificationCheckIdV1::ManifestValid,
                VerificationCheckIdV1::DeclaredBasePinMatch,
                VerificationCheckIdV1::InputDigestMatch,
                VerificationCheckIdV1::OracleDigestMatch,
                VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist,
                VerificationCheckIdV1::ProfileParse,
                VerificationCheckIdV1::ProfileAgainstSample,
                VerificationCheckIdV1::ProfileScore,
                VerificationCheckIdV1::GeneratorVerifierDistinct,
            ],
            network_policy: NetworkPolicyV1::DenyAll,
            load_policy: CandidateLoadPolicyV1::NextStartAfterHumanSeal,
            claim_ceiling: ClaimCeilingV1::SimSelfAttest,
        };
        (manifest, sample, oracle)
    }

    fn verify(
        manifest: &IntegrationCaseManifestV1,
        profile: &[u8],
        sample: &[u8],
        oracle: &[u8],
    ) -> ProfileVerificationBundleV1 {
        let candidate = ProfileCandidateArtifactV1::from_single(manifest, "profile.toml", profile)
            .expect("bounded candidate");
        verify_profile_case(
            manifest,
            "1111111111111111111111111111111111111111",
            "2222222222222222222222222222222222222222",
            &candidate,
            sample,
            oracle,
        )
        .expect("verification")
    }

    #[test]
    fn exact_profile_waits_for_human_seal() {
        let (manifest, sample, oracle) = fixture();
        let bundle = verify(&manifest, PROFILE.as_bytes(), &sample, &oracle);
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::AwaitingHumanSeal
        );
        assert_eq!(bundle.profile_score.as_ref().unwrap().matched, 3);
        assert_eq!(bundle.profile_score.as_ref().unwrap().consumed, 3);
        assert_eq!(bundle.profile_score.as_ref().unwrap().inert, 0);
        assert_eq!(bundle.profile_score.as_ref().unwrap().unit_matched, 3);
        assert_eq!(bundle.profile_score.as_ref().unwrap().unit_mismatched, 0);
        assert_eq!(bundle.profile_score.as_ref().unwrap().cadence_matched, 1);
        assert_eq!(bundle.profile_score.as_ref().unwrap().cadence_mismatched, 0);
        assert_eq!(
            bundle
                .profile_score
                .as_ref()
                .unwrap()
                .reader_observation_count,
            2
        );
        bundle.verify().expect("bundle digest");
        bundle
            .verification
            .validate_against_manifest(&manifest)
            .expect("manifest binding");
    }

    #[test]
    fn phantom_field_fails_loud() {
        let (manifest, sample, oracle) = fixture();
        let bad = PROFILE.replace("RQly(%)", "missing_rssi");
        let bundle = verify(&manifest, bad.as_bytes(), &sample, &oracle);
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::Fail
        );
        assert_eq!(bundle.profile_score.as_ref().unwrap().phantom, 1);
    }

    #[test]
    fn role_swap_is_mis_map() {
        let (manifest, sample, oracle) = fixture();
        let bad = PROFILE.replace("status = [\"Mode\"]", "status = [\"RQly(%)\"]");
        let bundle = verify(&manifest, bad.as_bytes(), &sample, &oracle);
        assert_eq!(bundle.profile_score.as_ref().unwrap().mis_mapped, 1);
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::Fail
        );
    }

    #[test]
    fn wrong_base_pin_fails() {
        let (manifest, sample, oracle) = fixture();
        let candidate =
            ProfileCandidateArtifactV1::from_single(&manifest, "profile.toml", PROFILE.as_bytes())
                .expect("candidate");
        let bundle = verify_profile_case(
            &manifest,
            "deadbee",
            "2222222222222222222222222222222222222222",
            &candidate,
            &sample,
            &oracle,
        )
        .expect("report");
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::Fail
        );
    }

    #[test]
    fn profile_verifier_rejects_unverified_extra_input() {
        let (mut manifest, sample, oracle) = fixture();
        manifest.input_artifacts.push(artifact(b"unverified input"));
        let bundle = verify(&manifest, PROFILE.as_bytes(), &sample, &oracle);
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::Fail
        );
        assert_eq!(
            bundle
                .verification
                .checks
                .iter()
                .find(|check| check.check_id == VerificationCheckIdV1::InputDigestMatch)
                .expect("input check")
                .status,
            CheckStatusV1::Fail
        );
    }

    #[test]
    fn candidate_root_rejects_extra_files() {
        let (manifest, _, _) = fixture();
        let root = scratch_dir("extra");
        std::fs::write(root.join("profile.toml"), PROFILE).expect("profile fixture");
        std::fs::write(root.join("undeclared.txt"), "unexpected").expect("extra fixture");
        let result = load_profile_candidate(&root, &manifest);
        std::fs::remove_dir_all(&root).expect("clean scratch directory");
        assert!(result.is_err());
    }

    #[cfg(unix)]
    #[test]
    fn candidate_root_rejects_symlinks() {
        use std::os::unix::fs::symlink;

        let (manifest, _, _) = fixture();

        let symlink_root = scratch_dir("symlink");
        std::fs::write(symlink_root.join("profile.toml"), PROFILE).expect("profile fixture");
        symlink("profile.toml", symlink_root.join("alias.toml")).expect("symlink fixture");
        let symlink_result = load_profile_candidate(&symlink_root, &manifest);
        std::fs::remove_dir_all(&symlink_root).expect("clean symlink fixture");
        assert!(symlink_result.is_err());
    }

    #[test]
    fn candidate_root_must_be_a_directory() {
        let (manifest, _, _) = fixture();
        let directory = scratch_dir("file_root");
        let file_root = directory.join("profile.toml");
        std::fs::write(&file_root, PROFILE).expect("profile fixture");
        let result = load_profile_candidate(&file_root, &manifest);
        std::fs::remove_dir_all(&directory).expect("clean file-root fixture");
        assert!(result.is_err());
    }

    #[test]
    fn header_match_without_a_successful_real_reader_run_fails() {
        let (manifest, sample, oracle) = fixture();
        let incompatible = PROFILE.replace("edgetx_csv", "mavlink_tlog");
        let bundle = verify(&manifest, incompatible.as_bytes(), &sample, &oracle);
        assert_eq!(bundle.profile_score.as_ref().unwrap().phantom, 0);
        assert_eq!(
            bundle
                .profile_score
                .as_ref()
                .unwrap()
                .reader_observation_count,
            0
        );
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::Fail
        );
    }

    #[test]
    fn unit_or_cadence_drift_fails_the_frozen_oracle() {
        let (manifest, sample, oracle) = fixture();
        for changed in [
            PROFILE.replace("percent", "ratio"),
            PROFILE.replace("cadence_ms = 100", "cadence_ms = 999"),
        ] {
            let bundle = verify(&manifest, changed.as_bytes(), &sample, &oracle);
            assert_eq!(
                bundle.verification.disposition,
                VerificationDispositionV1::Fail
            );
        }
    }

    #[test]
    fn unknown_profile_key_is_not_silently_ignored() {
        let (manifest, sample, oracle) = fixture();
        let changed = PROFILE.replace("[fields]", "recommended_action = \"continue\"\n[fields]");
        let bundle = verify(&manifest, changed.as_bytes(), &sample, &oracle);
        assert_eq!(
            bundle.verification.disposition,
            VerificationDispositionV1::Fail
        );
        assert!(bundle.profile_score.is_none());
    }

    #[test]
    fn bundle_rejects_a_rehashed_score_not_bound_by_report_evidence() {
        let (manifest, sample, oracle) = fixture();
        let mut bundle = verify(&manifest, PROFILE.as_bytes(), &sample, &oracle);
        bundle.profile_score.as_mut().unwrap().matched = 99;
        bundle.bundle_digest = canonical_sha256(&BundlePayloadRef {
            schema_version: &bundle.schema_version,
            verification: &bundle.verification,
            profile_score: &bundle.profile_score,
        })
        .expect("rehashed outer bundle");
        assert!(bundle.verify().is_err());
    }

    #[test]
    fn profile_report_requires_full_replay_before_becoming_verified() {
        let (manifest, sample, oracle) = fixture();
        let candidate =
            ProfileCandidateArtifactV1::from_single(&manifest, "profile.toml", PROFILE.as_bytes())
                .expect("bounded candidate");
        let bundle = verify(&manifest, PROFILE.as_bytes(), &sample, &oracle);
        let bytes = serde_json::to_vec(&bundle).expect("stored bundle");

        assert!(
            crate::report::verified_adapter_hold_from_json(
                &bytes,
                &manifest,
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                b"not-an-adapter",
            )
            .is_err()
        );
        let verified = crate::report::verified_profile_report_from_json(
            &bytes,
            &manifest,
            &manifest.requirements_commit,
            &manifest.implementation_commit,
            &candidate,
            &sample,
            &oracle,
        )
        .expect("exact deterministic replay");
        assert_eq!(verified.report(), bundle.verification());
    }

    #[test]
    fn profile_replay_rejects_each_changed_frozen_input() {
        let (manifest, sample, oracle) = fixture();
        let candidate =
            ProfileCandidateArtifactV1::from_single(&manifest, "profile.toml", PROFILE.as_bytes())
                .expect("bounded candidate");
        let bundle = verify(&manifest, PROFILE.as_bytes(), &sample, &oracle);
        let bytes = serde_json::to_vec(&bundle).expect("stored bundle");
        let replay = |requirements_commit: &str,
                      implementation_commit: &str,
                      candidate: &ProfileCandidateArtifactV1,
                      sample: &[u8],
                      oracle: &[u8]| {
            crate::report::verified_profile_report_from_json(
                &bytes,
                &manifest,
                requirements_commit,
                implementation_commit,
                candidate,
                sample,
                oracle,
            )
        };

        assert!(
            replay(
                &"a".repeat(40),
                &manifest.implementation_commit,
                &candidate,
                &sample,
                &oracle,
            )
            .is_err()
        );
        assert!(
            replay(
                &manifest.requirements_commit,
                &"a".repeat(40),
                &candidate,
                &sample,
                &oracle,
            )
            .is_err()
        );
        let changed_profile = PROFILE.replace("percent", "ratio");
        let changed_candidate = ProfileCandidateArtifactV1::from_single(
            &manifest,
            "profile.toml",
            changed_profile.as_bytes(),
        )
        .expect("changed bounded candidate");
        assert!(
            replay(
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                &changed_candidate,
                &sample,
                &oracle,
            )
            .is_err()
        );
        let changed_sample = b"tmr10ms,Mode,RQly(%)\n0,AUTO,91\n10,GUIDED,89\n".to_vec();
        assert!(
            replay(
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                &candidate,
                &changed_sample,
                &oracle,
            )
            .is_err()
        );
        let changed_oracle = String::from_utf8(oracle)
            .expect("oracle UTF-8")
            .replace("\"percent\"", "\"ratio\"")
            .into_bytes();
        assert!(
            replay(
                &manifest.requirements_commit,
                &manifest.implementation_commit,
                &candidate,
                &sample,
                &changed_oracle,
            )
            .is_err()
        );
    }

    #[test]
    fn fully_rehashed_forged_profile_bundle_fails_replay_gate() {
        let (manifest, sample, oracle) = fixture();
        let candidate =
            ProfileCandidateArtifactV1::from_single(&manifest, "profile.toml", PROFILE.as_bytes())
                .expect("bounded candidate");
        let mut forged = verify(&manifest, PROFILE.as_bytes(), &sample, &oracle);
        forged.profile_score.as_mut().expect("score").matched = 99;
        let forged_score_digest =
            canonical_sha256(forged.profile_score.as_ref().expect("score")).expect("score digest");
        let mut rebound_checks = 0;
        for check in &mut forged.verification.checks {
            if matches!(
                check.check_id,
                VerificationCheckIdV1::ProfileAgainstSample | VerificationCheckIdV1::ProfileScore
            ) {
                check.evidence.digest = forged_score_digest.clone();
                rebound_checks += 1;
            }
        }
        assert_eq!(rebound_checks, 2);
        forged.bundle_digest = canonical_sha256(&BundlePayloadRef {
            schema_version: &forged.schema_version,
            verification: &forged.verification,
            profile_score: &forged.profile_score,
        })
        .expect("rehashed outer bundle");
        forged
            .verify()
            .expect("forged bundle is internally consistent");
        let bytes = serde_json::to_vec(&forged).expect("forged stored bundle");
        ProfileVerificationBundleV1::from_json(&bytes)
            .expect("integrity parsing alone cannot distinguish a fully rehashed forgery");

        let error = crate::report::verified_profile_report_from_json(
            &bytes,
            &manifest,
            &manifest.requirements_commit,
            &manifest.implementation_commit,
            &candidate,
            &sample,
            &oracle,
        )
        .expect_err("replay must reject attacker-rehashed semantics");
        assert!(error.contains("differs from deterministic replay"));
    }

    #[test]
    fn quoted_csv_header_is_supported() {
        let fields = parse_csv_header(br#""Date,Time",RSSI,"Mode""#).expect("header");
        assert!(fields.contains("Date,Time"));
        assert!(fields.contains("RSSI"));
    }
}
