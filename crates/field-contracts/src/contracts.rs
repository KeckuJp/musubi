use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};

use crate::error::{require_finite, require_non_empty, require_schema_v1};
use crate::{ContractError, ContractResult, Validate, canonical_validated_sha256, lint_identifier};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct DigestV1(String);

impl DigestV1 {
    pub fn new(value: impl Into<String>) -> ContractResult<Self> {
        Self::try_from(value.into())
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    #[must_use]
    pub fn sha256(bytes: &[u8]) -> Self {
        let hash = Sha256::digest(bytes);
        let mut encoded = String::with_capacity(71);
        encoded.push_str("sha256:");
        for byte in hash {
            use std::fmt::Write as _;
            let _ = write!(encoded, "{byte:02x}");
        }
        Self(encoded)
    }
}

impl TryFrom<String> for DigestV1 {
    type Error = ContractError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        let Some(hex) = value.strip_prefix("sha256:") else {
            return Err(ContractError::InvalidField {
                field: "digest",
                reason: "must start with sha256:".to_owned(),
            });
        };
        if hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(ContractError::InvalidField {
                field: "digest",
                reason: "must contain exactly 64 lowercase hexadecimal characters".to_owned(),
            });
        }
        Ok(Self(value))
    }
}

impl From<DigestV1> for String {
    fn from(value: DigestV1) -> Self {
        value.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactRoleV1 {
    RawInput,
    Evidence,
    Fixture,
    Candidate,
    Receipt,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactReferenceV1 {
    pub digest: DigestV1,
    pub media_type: String,
    pub role: ArtifactRoleV1,
}

impl Validate for ArtifactReferenceV1 {
    fn validate(&self) -> ContractResult<()> {
        require_non_empty(&self.media_type, "artifact.media_type")?;
        if !self.media_type.contains('/') {
            return Err(ContractError::InvalidField {
                field: "artifact.media_type",
                reason: "must be an IANA-style type/subtype".to_owned(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PrimitiveValueKindV1 {
    Bool,
    I64,
    U64,
    F64,
    Text,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", content = "value", rename_all = "snake_case")]
pub enum ObservationValueV1 {
    Bool(bool),
    I64(i64),
    U64(u64),
    F64(f64),
    Text(String),
}

impl ObservationValueV1 {
    #[must_use]
    pub const fn kind(&self) -> PrimitiveValueKindV1 {
        match self {
            Self::Bool(_) => PrimitiveValueKindV1::Bool,
            Self::I64(_) => PrimitiveValueKindV1::I64,
            Self::U64(_) => PrimitiveValueKindV1::U64,
            Self::F64(_) => PrimitiveValueKindV1::F64,
            Self::Text(_) => PrimitiveValueKindV1::Text,
        }
    }
}

impl Validate for ObservationValueV1 {
    fn validate(&self) -> ContractResult<()> {
        match self {
            Self::F64(value) => require_finite(*value, "observation.value"),
            Self::Text(value) => require_non_empty(value, "observation.value"),
            Self::Bool(_) | Self::I64(_) | Self::U64(_) => Ok(()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum MissingValueV1 {
    NotObserved,
    SensorUnavailable,
    ClockUntrusted,
    Redacted,
    Sentinel { encoded_value: String },
    Unknown { detail: String },
}

impl Validate for MissingValueV1 {
    fn validate(&self) -> ContractResult<()> {
        match self {
            Self::Sentinel { encoded_value } => {
                require_non_empty(encoded_value, "observation.missing.encoded_value")
            }
            Self::Unknown { detail } => require_non_empty(detail, "observation.missing.detail"),
            Self::NotObserved | Self::SensorUnavailable | Self::ClockUntrusted | Self::Redacted => {
                Ok(())
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QualityStatusV1 {
    Good,
    Suspect,
    Bad,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QualityFlagV1 {
    ClockSkew,
    Saturated,
    CalibrationUnknown,
    ObserverDegraded,
    Interpolated,
    OutOfDeclaredRange,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QualityV1 {
    pub status: QualityStatusV1,
    pub score: Option<f64>,
    pub flags: Vec<QualityFlagV1>,
}

impl Validate for QualityV1 {
    fn validate(&self) -> ContractResult<()> {
        if let Some(score) = self.score {
            require_finite(score, "quality.score")?;
            if !(0.0..=1.0).contains(&score) {
                return Err(ContractError::InvalidField {
                    field: "quality.score",
                    reason: "must be in [0, 1]".to_owned(),
                });
            }
        }
        let mut flags = self.flags.clone();
        flags.sort_unstable();
        flags.dedup();
        if flags.len() != self.flags.len() {
            return Err(ContractError::InvalidField {
                field: "quality.flags",
                reason: "duplicate quality flag".to_owned(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NumericRangeV1 {
    pub minimum: f64,
    pub maximum: f64,
}

impl Validate for NumericRangeV1 {
    fn validate(&self) -> ContractResult<()> {
        require_finite(self.minimum, "valid_range.minimum")?;
        require_finite(self.maximum, "valid_range.maximum")?;
        if self.minimum > self.maximum {
            return Err(ContractError::InvalidField {
                field: "valid_range",
                reason: "minimum must not exceed maximum".to_owned(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceReferenceV1 {
    pub source_id: String,
    pub sensor_location: String,
}

impl Validate for SourceReferenceV1 {
    fn validate(&self) -> ContractResult<()> {
        require_non_empty(&self.source_id, "source.source_id")?;
        require_non_empty(&self.sensor_location, "source.sensor_location")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClockBasisV1 {
    GpsLocked,
    GpsSuspect,
    RtcSetOnce,
    BootRelativeOffsetEstimated,
    HostReceived,
    BootRelative,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransformerReferenceV1 {
    pub transformer_id: String,
    pub transformer_version: String,
    pub configuration_digest: DigestV1,
}

impl Validate for TransformerReferenceV1 {
    fn validate(&self) -> ContractResult<()> {
        lint_identifier(&self.transformer_id)?;
        require_non_empty(&self.transformer_version, "transformer.transformer_version")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationKindV1 {
    Raw,
    Derived,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationExtensionV1 {
    pub schema_version: String,
    pub namespace: String,
    pub semantic_id: String,
    pub semantic_version: String,
    pub event_time_ns: i64,
    pub clock_basis: ClockBasisV1,
    pub source: SourceReferenceV1,
    pub observation_kind: ObservationKindV1,
    pub value_kind: PrimitiveValueKindV1,
    pub value: Option<ObservationValueV1>,
    pub missing: Option<MissingValueV1>,
    pub unit: Option<String>,
    pub resolution: Option<f64>,
    pub valid_range: Option<NumericRangeV1>,
    pub quality: QualityV1,
    pub raw_artifacts: Vec<ArtifactReferenceV1>,
    pub transformer: Option<TransformerReferenceV1>,
}

impl Validate for ObservationExtensionV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        lint_identifier(&self.namespace)?;
        lint_identifier(&self.semantic_id)?;
        require_non_empty(&self.semantic_version, "semantic_version")?;
        self.source.validate()?;
        match (&self.value, &self.missing) {
            (Some(value), None) => {
                value.validate()?;
                if value.kind() != self.value_kind {
                    return Err(ContractError::InvalidField {
                        field: "observation.value_kind",
                        reason: "must match the encoded primitive value".to_owned(),
                    });
                }
            }
            (None, Some(missing)) => missing.validate()?,
            _ => {
                return Err(ContractError::InvalidField {
                    field: "observation.value/missing",
                    reason: "exactly one of value and missing must be present".to_owned(),
                });
            }
        }
        if let Some(unit) = &self.unit {
            require_non_empty(unit, "observation.unit")?;
        }
        if let Some(resolution) = self.resolution {
            require_finite(resolution, "observation.resolution")?;
            if resolution <= 0.0 {
                return Err(ContractError::InvalidField {
                    field: "observation.resolution",
                    reason: "must be greater than zero".to_owned(),
                });
            }
        }
        if let Some(range) = &self.valid_range {
            range.validate()?;
        }
        self.quality.validate()?;
        if self.raw_artifacts.is_empty() {
            return Err(ContractError::MissingField("observation.raw_artifacts"));
        }
        for artifact in &self.raw_artifacts {
            artifact.validate()?;
        }
        match (self.observation_kind, &self.transformer) {
            (ObservationKindV1::Raw, None) => {}
            (ObservationKindV1::Derived, Some(transformer)) => transformer.validate()?,
            (ObservationKindV1::Raw, Some(_)) => {
                return Err(ContractError::InvalidField {
                    field: "observation.transformer",
                    reason: "raw observations must not name a transformer".to_owned(),
                });
            }
            (ObservationKindV1::Derived, None) => {
                return Err(ContractError::MissingField("observation.transformer"));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AnnotationKindV1 {
    Observation,
    CauseCandidate,
    GroundTruth,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AnnotationCertaintyV1 {
    Suspected,
    Ambiguous,
    Unknown,
    Confirmed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AnnotationSourceV1 {
    ControlledFixture,
    OperatorAnnotation,
    PostRunInspection,
    Video,
    MotionCapture,
    ContinuityTest,
    DeterministicInjection,
    ModelOutput {
        model_id: String,
        model_version: String,
    },
    OtherIndependentEvidence {
        description: String,
    },
}

impl Validate for AnnotationSourceV1 {
    fn validate(&self) -> ContractResult<()> {
        match self {
            Self::ModelOutput {
                model_id,
                model_version,
            } => {
                lint_identifier(model_id)?;
                require_non_empty(model_version, "annotation.label_source.model_version")
            }
            Self::OtherIndependentEvidence { description } => {
                require_non_empty(description, "annotation.label_source.description")
            }
            Self::ControlledFixture
            | Self::OperatorAnnotation
            | Self::PostRunInspection
            | Self::Video
            | Self::MotionCapture
            | Self::ContinuityTest
            | Self::DeterministicInjection => Ok(()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EpisodeAnnotationV1 {
    pub schema_version: String,
    pub episode_id: String,
    pub window_start_ns: i64,
    pub window_end_ns: i64,
    pub taxonomy_namespace: String,
    pub taxonomy_version: String,
    pub label: String,
    pub annotation_kind: AnnotationKindV1,
    pub certainty: AnnotationCertaintyV1,
    pub label_source: AnnotationSourceV1,
    pub annotator_id: String,
    pub evidence: Vec<ArtifactReferenceV1>,
}

impl Validate for EpisodeAnnotationV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        lint_identifier(&self.episode_id)?;
        lint_identifier(&self.taxonomy_namespace)?;
        require_non_empty(&self.taxonomy_version, "annotation.taxonomy_version")?;
        lint_identifier(&self.label)?;
        require_non_empty(&self.annotator_id, "annotation.annotator_id")?;
        if self.window_start_ns > self.window_end_ns {
            return Err(ContractError::InvalidField {
                field: "annotation.window",
                reason: "window_start_ns must not exceed window_end_ns".to_owned(),
            });
        }
        self.label_source.validate()?;
        for artifact in &self.evidence {
            artifact.validate()?;
        }
        if self.label.starts_with("confirmed_")
            && self.certainty != AnnotationCertaintyV1::Confirmed
        {
            return Err(ContractError::InvalidField {
                field: "annotation.label",
                reason: "labels in the reserved confirmed_* namespace require confirmed certainty"
                    .to_owned(),
            });
        }
        if self.certainty == AnnotationCertaintyV1::Confirmed {
            if self.annotation_kind != AnnotationKindV1::GroundTruth {
                return Err(ContractError::InvalidField {
                    field: "annotation.annotation_kind",
                    reason: "confirmed annotations must be ground_truth".to_owned(),
                });
            }
            if self.evidence.is_empty() {
                return Err(ContractError::MissingField("annotation.evidence"));
            }
            if !matches!(
                self.label_source,
                AnnotationSourceV1::ControlledFixture
                    | AnnotationSourceV1::PostRunInspection
                    | AnnotationSourceV1::Video
                    | AnnotationSourceV1::MotionCapture
                    | AnnotationSourceV1::ContinuityTest
                    | AnnotationSourceV1::DeterministicInjection
                    | AnnotationSourceV1::OtherIndependentEvidence { .. }
            ) {
                return Err(ContractError::InvalidField {
                    field: "annotation.label_source",
                    reason: "confirmed ground truth requires an independent non-operator, non-model evidence source"
                        .to_owned(),
                });
            }
            if !self.evidence.iter().any(|artifact| {
                matches!(
                    artifact.role,
                    ArtifactRoleV1::Evidence | ArtifactRoleV1::Fixture
                )
            }) {
                return Err(ContractError::InvalidField {
                    field: "annotation.evidence",
                    reason: "confirmed ground truth requires at least one evidence or fixture artifact role"
                        .to_owned(),
                });
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MeasurementEndV1 {
    Vehicle,
    Reel,
    Both,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReelDirectionV1 {
    PositivePaysOut,
    PositiveRecoils,
    NotInstrumented,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SensorCalibrationV1 {
    pub semantic_id: String,
    pub scale: f64,
    pub offset: f64,
    pub unit: String,
    pub calibration_artifact: ArtifactReferenceV1,
}

impl Validate for SensorCalibrationV1 {
    fn validate(&self) -> ContractResult<()> {
        lint_identifier(&self.semantic_id)?;
        require_finite(self.scale, "calibration.scale")?;
        require_finite(self.offset, "calibration.offset")?;
        if self.scale == 0.0 {
            return Err(ContractError::InvalidField {
                field: "calibration.scale",
                reason: "must be non-zero".to_owned(),
            });
        }
        require_non_empty(&self.unit, "calibration.unit")?;
        self.calibration_artifact.validate()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BaselineStatisticV1 {
    pub semantic_id: String,
    pub mean: f64,
    pub standard_deviation: f64,
    pub sample_count: u64,
}

impl Validate for BaselineStatisticV1 {
    fn validate(&self) -> ContractResult<()> {
        lint_identifier(&self.semantic_id)?;
        require_finite(self.mean, "baseline.mean")?;
        require_finite(self.standard_deviation, "baseline.standard_deviation")?;
        if self.standard_deviation < 0.0 {
            return Err(ContractError::InvalidField {
                field: "baseline.standard_deviation",
                reason: "must be non-negative".to_owned(),
            });
        }
        if self.sample_count == 0 {
            return Err(ContractError::InvalidField {
                field: "baseline.sample_count",
                reason: "must be greater than zero".to_owned(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ThresholdComparisonV1 {
    GreaterThan,
    GreaterThanOrEqual,
    LessThan,
    LessThanOrEqual,
    OutsideInclusiveRange,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeatureThresholdV1 {
    pub feature_semantic_id: String,
    pub comparison: ThresholdComparisonV1,
    pub primary_value: f64,
    pub secondary_value: Option<f64>,
    pub minimum_duration_ms: u64,
    pub applicability: String,
    pub provenance: ArtifactReferenceV1,
}

impl Validate for FeatureThresholdV1 {
    fn validate(&self) -> ContractResult<()> {
        lint_identifier(&self.feature_semantic_id)?;
        require_finite(self.primary_value, "threshold.primary_value")?;
        if let Some(value) = self.secondary_value {
            require_finite(value, "threshold.secondary_value")?;
        }
        match (self.comparison, self.secondary_value) {
            (ThresholdComparisonV1::OutsideInclusiveRange, Some(upper)) => {
                if self.primary_value > upper {
                    return Err(ContractError::InvalidField {
                        field: "threshold",
                        reason: "range lower bound must not exceed upper bound".to_owned(),
                    });
                }
            }
            (ThresholdComparisonV1::OutsideInclusiveRange, None) => {
                return Err(ContractError::MissingField("threshold.secondary_value"));
            }
            (_, Some(_)) => {
                return Err(ContractError::InvalidField {
                    field: "threshold.secondary_value",
                    reason: "only outside_inclusive_range accepts a second value".to_owned(),
                });
            }
            (_, None) => {}
        }
        require_non_empty(&self.applicability, "threshold.applicability")?;
        self.provenance.validate()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CableInteractionProfileV1 {
    pub schema_version: String,
    pub profile_id: String,
    pub semantic_version: String,
    pub tether_type: String,
    pub nominal_length_m: f64,
    pub measurement_end: MeasurementEndV1,
    pub reel_direction: ReelDirectionV1,
    pub calibrations: Vec<SensorCalibrationV1>,
    pub baseline: Vec<BaselineStatisticV1>,
    pub feature_transformer: TransformerReferenceV1,
    pub thresholds: Vec<FeatureThresholdV1>,
    pub applicability_dimensions: BTreeMap<String, String>,
}

impl Validate for CableInteractionProfileV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        lint_identifier(&self.profile_id)?;
        require_non_empty(&self.semantic_version, "profile.semantic_version")?;
        require_non_empty(&self.tether_type, "profile.tether_type")?;
        require_finite(self.nominal_length_m, "profile.nominal_length_m")?;
        if self.nominal_length_m <= 0.0 {
            return Err(ContractError::InvalidField {
                field: "profile.nominal_length_m",
                reason: "must be greater than zero".to_owned(),
            });
        }
        if self.calibrations.is_empty() {
            return Err(ContractError::MissingField("profile.calibrations"));
        }
        for calibration in &self.calibrations {
            calibration.validate()?;
        }
        if self.baseline.is_empty() {
            return Err(ContractError::MissingField("profile.baseline"));
        }
        for statistic in &self.baseline {
            statistic.validate()?;
        }
        self.feature_transformer.validate()?;
        for threshold in &self.thresholds {
            threshold.validate()?;
        }
        for (key, value) in &self.applicability_dimensions {
            lint_identifier(key)?;
            require_non_empty(value, "profile.applicability_dimensions.value")?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateKindV1 {
    Profile,
    Adapter,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum VerificationCheckIdV1 {
    ManifestValid,
    DeclaredBasePinMatch,
    InputDigestMatch,
    OracleDigestMatch,
    CandidateArtifactSetWithinAllowlist,
    ProfileParse,
    ProfileAgainstSample,
    ProfileScore,
    AdapterConformance,
    ArchitectureFitness,
    RuntimeEgressZero,
    GeneratorVerifierDistinct,
}

const fn common_required_checks() -> [VerificationCheckIdV1; 6] {
    [
        VerificationCheckIdV1::ManifestValid,
        VerificationCheckIdV1::DeclaredBasePinMatch,
        VerificationCheckIdV1::InputDigestMatch,
        VerificationCheckIdV1::OracleDigestMatch,
        VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist,
        VerificationCheckIdV1::GeneratorVerifierDistinct,
    ]
}

const fn kind_required_checks(kind: CandidateKindV1) -> [VerificationCheckIdV1; 3] {
    match kind {
        CandidateKindV1::Profile => [
            VerificationCheckIdV1::ProfileParse,
            VerificationCheckIdV1::ProfileAgainstSample,
            VerificationCheckIdV1::ProfileScore,
        ],
        CandidateKindV1::Adapter => [
            VerificationCheckIdV1::AdapterConformance,
            VerificationCheckIdV1::ArchitectureFitness,
            VerificationCheckIdV1::RuntimeEgressZero,
        ],
    }
}

fn require_commit_pin(value: &str, field: &'static str) -> ContractResult<()> {
    if (7..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err(ContractError::InvalidField {
            field,
            reason: "must be 7-64 lowercase hexadecimal characters".to_owned(),
        })
    }
}

fn require_relative_path(value: &str, field: &'static str) -> ContractResult<()> {
    if value.is_empty()
        || value.starts_with('/')
        || value.starts_with('\\')
        || value.contains('\\')
        || value.contains('\0')
        || value
            .split('/')
            .any(|component| component.is_empty() || matches!(component, "." | ".."))
        || value.as_bytes().get(1) == Some(&b':')
    {
        Err(ContractError::InvalidField {
            field,
            reason: "must be a normalized relative path without '.', '..', or empty components"
                .to_owned(),
        })
    } else {
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DataClassificationV1 {
    Public,
    Synthetic,
    Anonymized,
    ClientConfidential,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactResidencyV1 {
    NotApplicable,
    ClientLocalOnly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionEnvironmentV1 {
    IsolatedSynthetic,
    GovernedIsolated,
    ClientLocal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
pub enum ExternalInferencePolicyV1 {
    Denied,
    ContractAuthorized { authorization_digest: DigestV1 },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
pub enum ExportPolicyV1 {
    Denied,
    GenericSchemaAggregateAndReceiptOnly,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyPolicyFieldsV1 {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthorizedPolicyFieldsV1 {
    authorization_digest: DigestV1,
}

#[derive(Deserialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
enum ExternalInferencePolicyWireV1 {
    Denied(EmptyPolicyFieldsV1),
    ContractAuthorized(AuthorizedPolicyFieldsV1),
}

impl<'de> Deserialize<'de> for ExternalInferencePolicyV1 {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Ok(
            match ExternalInferencePolicyWireV1::deserialize(deserializer)? {
                ExternalInferencePolicyWireV1::Denied(_) => Self::Denied,
                ExternalInferencePolicyWireV1::ContractAuthorized(fields) => {
                    Self::ContractAuthorized {
                        authorization_digest: fields.authorization_digest,
                    }
                }
            },
        )
    }
}

#[derive(Deserialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
enum ExportPolicyWireV1 {
    Denied(EmptyPolicyFieldsV1),
    GenericSchemaAggregateAndReceiptOnly(EmptyPolicyFieldsV1),
}

impl<'de> Deserialize<'de> for ExportPolicyV1 {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Ok(match ExportPolicyWireV1::deserialize(deserializer)? {
            ExportPolicyWireV1::Denied(_) => Self::Denied,
            ExportPolicyWireV1::GenericSchemaAggregateAndReceiptOnly(_) => {
                Self::GenericSchemaAggregateAndReceiptOnly
            }
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetentionPolicyV1 {
    pub maximum_days: Option<u32>,
    pub deletion_authority_id: String,
}

impl Validate for RetentionPolicyV1 {
    fn validate(&self) -> ContractResult<()> {
        require_non_empty(
            &self.deletion_authority_id,
            "data_boundary.retention.deletion_authority_id",
        )?;
        if self.maximum_days == Some(0) {
            return Err(ContractError::InvalidField {
                field: "data_boundary.retention.maximum_days",
                reason: "must be greater than zero when present".to_owned(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataBoundaryV1 {
    pub classification: DataClassificationV1,
    pub tenant_id: Option<String>,
    pub rights_digest: Option<DigestV1>,
    pub execution_environment: ExecutionEnvironmentV1,
    pub external_inference: ExternalInferencePolicyV1,
    pub export_policy: ExportPolicyV1,
    pub raw_logs_residency: ArtifactResidencyV1,
    pub labels_residency: ArtifactResidencyV1,
    pub model_weights_residency: ArtifactResidencyV1,
    pub retention: RetentionPolicyV1,
}

impl Validate for DataBoundaryV1 {
    fn validate(&self) -> ContractResult<()> {
        self.retention.validate()?;
        match self.classification {
            DataClassificationV1::ClientConfidential => {
                let tenant = self
                    .tenant_id
                    .as_deref()
                    .ok_or(ContractError::MissingField("data_boundary.tenant_id"))?;
                require_non_empty(tenant, "data_boundary.tenant_id")?;
                if self.rights_digest.is_none() {
                    return Err(ContractError::MissingField("data_boundary.rights_digest"));
                }
                if self.execution_environment != ExecutionEnvironmentV1::ClientLocal {
                    return Err(ContractError::InvalidField {
                        field: "data_boundary.execution_environment",
                        reason: "client-confidential input must execute client-locally".to_owned(),
                    });
                }
                for (field, residency) in [
                    ("data_boundary.raw_logs_residency", self.raw_logs_residency),
                    ("data_boundary.labels_residency", self.labels_residency),
                    (
                        "data_boundary.model_weights_residency",
                        self.model_weights_residency,
                    ),
                ] {
                    if residency != ArtifactResidencyV1::ClientLocalOnly {
                        return Err(ContractError::InvalidField {
                            field,
                            reason: "client-confidential artifacts must remain client-local"
                                .to_owned(),
                        });
                    }
                }
            }
            DataClassificationV1::Anonymized => {
                let tenant = self
                    .tenant_id
                    .as_deref()
                    .ok_or(ContractError::MissingField("data_boundary.tenant_id"))?;
                require_non_empty(tenant, "data_boundary.tenant_id")?;
                if self.rights_digest.is_none() {
                    return Err(ContractError::MissingField("data_boundary.rights_digest"));
                }
                match self.execution_environment {
                    ExecutionEnvironmentV1::ClientLocal => {
                        for (field, residency) in [
                            ("data_boundary.raw_logs_residency", self.raw_logs_residency),
                            ("data_boundary.labels_residency", self.labels_residency),
                            (
                                "data_boundary.model_weights_residency",
                                self.model_weights_residency,
                            ),
                        ] {
                            if residency != ArtifactResidencyV1::ClientLocalOnly {
                                return Err(ContractError::InvalidField {
                                    field,
                                    reason:
                                        "client-local anonymized artifacts must remain client-local"
                                            .to_owned(),
                                });
                            }
                        }
                    }
                    ExecutionEnvironmentV1::GovernedIsolated => {
                        if !matches!(
                            self.external_inference,
                            ExternalInferencePolicyV1::ContractAuthorized { .. }
                        ) || self.export_policy
                            != ExportPolicyV1::GenericSchemaAggregateAndReceiptOnly
                        {
                            return Err(ContractError::InvalidField {
                                field: "data_boundary.anonymized_external_use",
                                reason: "governed isolated use of anonymized client-derived data requires an explicit inference authorization digest and bounded generic-schema/aggregate/receipt-only export"
                                    .to_owned(),
                            });
                        }
                    }
                    ExecutionEnvironmentV1::IsolatedSynthetic => {
                        return Err(ContractError::InvalidField {
                            field: "data_boundary.execution_environment",
                            reason: "anonymized client-derived data is not synthetic".to_owned(),
                        });
                    }
                }
            }
            DataClassificationV1::Public | DataClassificationV1::Synthetic => {
                if self.tenant_id.is_some() || self.rights_digest.is_some() {
                    return Err(ContractError::InvalidField {
                        field: "data_boundary",
                        reason: "non-client input must not imply a tenant or client rights grant"
                            .to_owned(),
                    });
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
pub enum NetworkPolicyV1 {
    DenyAll,
    Allowlisted {
        hosts: Vec<String>,
        authorization_digest: DigestV1,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AllowlistedNetworkPolicyFieldsV1 {
    hosts: Vec<String>,
    authorization_digest: DigestV1,
}

#[derive(Deserialize)]
#[serde(tag = "policy", rename_all = "snake_case")]
enum NetworkPolicyWireV1 {
    DenyAll(EmptyPolicyFieldsV1),
    Allowlisted(AllowlistedNetworkPolicyFieldsV1),
}

impl<'de> Deserialize<'de> for NetworkPolicyV1 {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Ok(match NetworkPolicyWireV1::deserialize(deserializer)? {
            NetworkPolicyWireV1::DenyAll(_) => Self::DenyAll,
            NetworkPolicyWireV1::Allowlisted(fields) => Self::Allowlisted {
                hosts: fields.hosts,
                authorization_digest: fields.authorization_digest,
            },
        })
    }
}

impl Validate for NetworkPolicyV1 {
    fn validate(&self) -> ContractResult<()> {
        if let Self::Allowlisted { hosts, .. } = self {
            if hosts.is_empty() {
                return Err(ContractError::MissingField("network_policy.hosts"));
            }
            for host in hosts {
                require_non_empty(host, "network_policy.host")?;
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateLoadPolicyV1 {
    NextStartAfterHumanSeal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClaimCeilingV1 {
    #[serde(rename = "SIM_SELF_ATTEST")]
    SimSelfAttest,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OracleReferenceV1 {
    pub oracle_id: String,
    pub semantic_owner_id: String,
    pub oracle_digest: DigestV1,
}

impl Validate for OracleReferenceV1 {
    fn validate(&self) -> ContractResult<()> {
        lint_identifier(&self.oracle_id)?;
        require_non_empty(&self.semantic_owner_id, "oracle.semantic_owner_id")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IntegrationCaseManifestV1 {
    pub schema_version: String,
    pub case_id: String,
    pub case_version: String,
    pub candidate_kind: CandidateKindV1,
    pub requirements_commit: String,
    pub implementation_commit: String,
    pub data_boundary: DataBoundaryV1,
    pub oracle: OracleReferenceV1,
    pub acceptance_owner_id: String,
    pub input_artifacts: Vec<ArtifactReferenceV1>,
    pub candidate_write_allowlist: Vec<String>,
    pub allowed_tools: Vec<String>,
    pub generator_actor_id: String,
    pub verifier_actor_id: String,
    pub required_checks: Vec<VerificationCheckIdV1>,
    pub network_policy: NetworkPolicyV1,
    pub load_policy: CandidateLoadPolicyV1,
    pub claim_ceiling: ClaimCeilingV1,
}

impl IntegrationCaseManifestV1 {
    pub fn manifest_digest(&self) -> ContractResult<DigestV1> {
        canonical_validated_sha256(self)
    }
}

impl Validate for IntegrationCaseManifestV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        lint_identifier(&self.case_id)?;
        require_non_empty(&self.case_version, "integration_case.case_version")?;
        require_commit_pin(
            &self.requirements_commit,
            "integration_case.requirements_commit",
        )?;
        require_commit_pin(
            &self.implementation_commit,
            "integration_case.implementation_commit",
        )?;
        self.data_boundary.validate()?;
        self.oracle.validate()?;
        require_non_empty(
            &self.acceptance_owner_id,
            "integration_case.acceptance_owner_id",
        )?;
        if self.input_artifacts.is_empty() {
            return Err(ContractError::MissingField(
                "integration_case.input_artifacts",
            ));
        }
        for artifact in &self.input_artifacts {
            artifact.validate()?;
        }
        if self.candidate_write_allowlist.is_empty() {
            return Err(ContractError::MissingField(
                "integration_case.candidate_write_allowlist",
            ));
        }
        let mut candidate_paths = BTreeSet::new();
        for path in &self.candidate_write_allowlist {
            require_relative_path(path, "integration_case.candidate_write_allowlist")?;
            if !candidate_paths.insert(path) {
                return Err(ContractError::InvalidField {
                    field: "integration_case.candidate_write_allowlist",
                    reason: "duplicate path".to_owned(),
                });
            }
        }
        if self.allowed_tools.is_empty() {
            return Err(ContractError::MissingField(
                "integration_case.allowed_tools",
            ));
        }
        let mut tools = BTreeSet::new();
        for tool in &self.allowed_tools {
            lint_identifier(tool)?;
            if !tools.insert(tool) {
                return Err(ContractError::InvalidField {
                    field: "integration_case.allowed_tools",
                    reason: "duplicate tool identifier".to_owned(),
                });
            }
        }
        require_non_empty(
            &self.generator_actor_id,
            "integration_case.generator_actor_id",
        )?;
        require_non_empty(
            &self.verifier_actor_id,
            "integration_case.verifier_actor_id",
        )?;
        if self.generator_actor_id == self.verifier_actor_id {
            return Err(ContractError::InvalidField {
                field: "integration_case.verifier_actor_id",
                reason: "generator and verifier must be distinct".to_owned(),
            });
        }
        let check_set: BTreeSet<_> = self.required_checks.iter().copied().collect();
        if check_set.len() != self.required_checks.len() {
            return Err(ContractError::InvalidField {
                field: "integration_case.required_checks",
                reason: "duplicate fixed check id".to_owned(),
            });
        }
        let expected_checks: BTreeSet<_> = common_required_checks()
            .into_iter()
            .chain(kind_required_checks(self.candidate_kind))
            .collect();
        if check_set != expected_checks {
            return Err(ContractError::InvalidField {
                field: "integration_case.required_checks",
                reason: "must equal the fixed check set for candidate_kind".to_owned(),
            });
        }
        self.network_policy.validate()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckStatusV1 {
    Pass,
    Fail,
    Hold,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerificationCheckV1 {
    pub check_id: VerificationCheckIdV1,
    pub status: CheckStatusV1,
    pub detail_code: String,
    pub evidence: ArtifactReferenceV1,
}

impl Validate for VerificationCheckV1 {
    fn validate(&self) -> ContractResult<()> {
        require_non_empty(&self.detail_code, "verification.check.detail_code")?;
        if !self
            .detail_code
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
        {
            return Err(ContractError::InvalidField {
                field: "verification.check.detail_code",
                reason: "must contain only uppercase ASCII, digits, and underscore".to_owned(),
            });
        }
        self.evidence.validate()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerificationDispositionV1 {
    Fail,
    Hold,
    AwaitingHumanSeal,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VerificationReportV1 {
    pub schema_version: String,
    pub case_id: String,
    pub candidate_kind: CandidateKindV1,
    pub generator_actor_id: String,
    pub verifier_actor_id: String,
    pub input_manifest_digest: DigestV1,
    pub candidate_digest: DigestV1,
    pub checks: Vec<VerificationCheckV1>,
    pub abstention_reasons: Vec<String>,
    pub disposition: VerificationDispositionV1,
    pub hot_reload_applied: bool,
    pub runtime_load_authorized: bool,
    pub claim_ceiling: ClaimCeilingV1,
}

impl VerificationReportV1 {
    pub fn receipt_digest(&self) -> ContractResult<DigestV1> {
        canonical_validated_sha256(self)
    }

    pub fn validate_against_manifest(
        &self,
        manifest: &IntegrationCaseManifestV1,
    ) -> ContractResult<()> {
        self.validate()?;
        manifest.validate()?;
        if self.case_id != manifest.case_id
            || self.candidate_kind != manifest.candidate_kind
            || self.generator_actor_id != manifest.generator_actor_id
            || self.verifier_actor_id != manifest.verifier_actor_id
            || self.input_manifest_digest != manifest.manifest_digest()?
        {
            return Err(ContractError::InvalidField {
                field: "verification.manifest_binding",
                reason: "case, actors, or manifest digest do not match".to_owned(),
            });
        }
        let report_checks: BTreeSet<_> = self.checks.iter().map(|check| check.check_id).collect();
        let manifest_checks: BTreeSet<_> = manifest.required_checks.iter().copied().collect();
        if report_checks != manifest_checks {
            return Err(ContractError::InvalidField {
                field: "verification.checks",
                reason: "report check set must exactly equal the frozen manifest check set"
                    .to_owned(),
            });
        }
        Ok(())
    }
}

impl Validate for VerificationReportV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        lint_identifier(&self.case_id)?;
        require_non_empty(&self.generator_actor_id, "verification.generator_actor_id")?;
        require_non_empty(&self.verifier_actor_id, "verification.verifier_actor_id")?;
        if self.generator_actor_id == self.verifier_actor_id {
            return Err(ContractError::InvalidField {
                field: "verification.verifier_actor_id",
                reason: "generator and verifier must be distinct".to_owned(),
            });
        }
        if self.checks.is_empty() {
            return Err(ContractError::MissingField("verification.checks"));
        }
        let mut check_ids = BTreeSet::new();
        for check in &self.checks {
            check.validate()?;
            if !check_ids.insert(check.check_id) {
                return Err(ContractError::InvalidField {
                    field: "verification.checks",
                    reason: "duplicate fixed check id".to_owned(),
                });
            }
        }
        let expected_checks: BTreeSet<_> = common_required_checks()
            .into_iter()
            .chain(kind_required_checks(self.candidate_kind))
            .collect();
        if check_ids != expected_checks {
            return Err(ContractError::InvalidField {
                field: "verification.checks",
                reason: "must equal the fixed check set for candidate_kind".to_owned(),
            });
        }
        for reason in &self.abstention_reasons {
            require_non_empty(reason, "verification.abstention_reasons")?;
        }
        if self.hot_reload_applied {
            return Err(ContractError::InvalidField {
                field: "verification.hot_reload_applied",
                reason: "runtime hot reload is forbidden".to_owned(),
            });
        }
        if self.runtime_load_authorized {
            return Err(ContractError::InvalidField {
                field: "verification.runtime_load_authorized",
                reason: "machine verification never authorizes runtime loading".to_owned(),
            });
        }
        let expected_disposition = if self
            .checks
            .iter()
            .any(|check| check.status == CheckStatusV1::Fail)
        {
            VerificationDispositionV1::Fail
        } else if self
            .checks
            .iter()
            .any(|check| check.status == CheckStatusV1::Hold)
            || !self.abstention_reasons.is_empty()
        {
            VerificationDispositionV1::Hold
        } else {
            VerificationDispositionV1::AwaitingHumanSeal
        };
        if self.disposition != expected_disposition {
            return Err(ContractError::InvalidField {
                field: "verification.disposition",
                reason: "must match check failures, holds, and abstention state".to_owned(),
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IdentityBasisV1 {
    SelfAttested,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HumanSealDecisionV1 {
    ApproveForNextStart,
    Reject,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HumanSealV1 {
    pub schema_version: String,
    pub report_digest: DigestV1,
    pub candidate_digest: DigestV1,
    pub approver_id: String,
    pub identity_basis: IdentityBasisV1,
    pub decision: HumanSealDecisionV1,
    pub sealed_at_ns: i64,
    pub runtime_load_authorized: bool,
}

impl HumanSealV1 {
    pub fn validate_against_report(&self, report: &VerificationReportV1) -> ContractResult<()> {
        self.validate()?;
        report.validate()?;
        if self.report_digest != report.receipt_digest()?
            || self.candidate_digest != report.candidate_digest
        {
            return Err(ContractError::InvalidField {
                field: "human_seal.report_binding",
                reason: "seal does not cover the exact report and candidate digests".to_owned(),
            });
        }
        if self.decision == HumanSealDecisionV1::ApproveForNextStart
            && report.disposition != VerificationDispositionV1::AwaitingHumanSeal
        {
            return Err(ContractError::InvalidField {
                field: "human_seal.decision",
                reason: "approve_for_next_start requires an all-pass report awaiting human seal"
                    .to_owned(),
            });
        }
        Ok(())
    }
}

impl Validate for HumanSealV1 {
    fn validate(&self) -> ContractResult<()> {
        require_schema_v1(&self.schema_version)?;
        require_non_empty(&self.approver_id, "human_seal.approver_id")?;
        if self.runtime_load_authorized {
            return Err(ContractError::InvalidField {
                field: "human_seal.runtime_load_authorized",
                reason: "a detached self-attested seal never authorizes runtime loading".to_owned(),
            });
        }
        Ok(())
    }
}
