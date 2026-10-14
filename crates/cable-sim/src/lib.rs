//! Deterministic cable-interaction pilot data.
//!
//! The generator is a read-only synthetic fixture. It embeds the committed public cable profile
//! and extension registry, emits only registry-admitted contract records, and carries exact public
//! artifact digests in every session. It opens no network socket and exposes no command surface.
//! Every result is `SIMULATED`; it is not evidence of field performance.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]
#![allow(
    clippy::cast_precision_loss,
    clippy::doc_markdown,
    clippy::module_name_repetitions,
    clippy::similar_names,
    // Keep the arithmetic order explicit because the committed receipt pins exact float bytes.
    clippy::suboptimal_flops,
    clippy::too_many_arguments,
    clippy::too_many_lines
)]

mod model;

use std::fmt;
use std::fs::OpenOptions;
use std::io::Write as _;
use std::path::Path;

use musubi_field_contracts::{
    AnnotationCertaintyV1, AnnotationKindV1, AnnotationRegistryOutcomeV1, AnnotationSourceV1,
    ArtifactReferenceV1, ArtifactRoleV1, CableInteractionProfileV1, ClockBasisV1, ContractError,
    DigestV1, EpisodeAnnotationV1, MissingValueV1, NumericRangeV1, ObservationExtensionV1,
    ObservationKindV1, ObservationRegistryOutcomeV1, ObservationValueV1, PrimitiveValueKindV1,
    QualityFlagV1, QualityStatusV1, QualityV1, RecordV1, RegistryV1, SCHEMA_VERSION_V1,
    SIM_SELF_ATTEST, SourceReferenceV1, TransformerReferenceV1, Validate, canonical_sha256,
    encode_ndjson,
};
use serde::Serialize;

use model::{
    CALIBRATION_MISSING_TICK, CALIBRATION_UNKNOWN_TICK, CLOCK_UNTRUSTED_TICK, EVENT_START_TICK,
    Sample, TICK_INTERVAL_NS, derive_features, generate_samples,
};
pub use model::{ScenarioKind, SensorTier, SimulationConfig};

pub const WATERMARK: &str = "SIMULATED";
pub const FEATURE_TRANSFORMER_ID: &str = "cable_interaction_features";
pub const FEATURE_TRANSFORMER_VERSION: &str = "1.0.0";

const OBSERVATION_NAMESPACE: &str = "kecku.musubi.cable";
const OBSERVATION_SEMANTIC_VERSION: &str = "1.0.0";
const TAXONOMY_NAMESPACE: &str = "kecku.musubi.cable_interaction";
const TAXONOMY_VERSION: &str = "1.0.0";
const PUBLIC_PROFILE_JSON: &[u8] =
    include_bytes!("../../../profiles/public/cable/cable-interaction-profile-v1.json");
const PUBLIC_REGISTRY_JSON: &[u8] =
    include_bytes!("../../../profiles/public/extensions/cable-field-registry-v1.json");

#[derive(Debug)]
pub enum SimulationError {
    InvalidConfiguration(String),
    Contract(ContractError),
    Json(serde_json::Error),
    Io(std::io::Error),
}

impl fmt::Display for SimulationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfiguration(reason) => {
                write!(formatter, "invalid configuration: {reason}")
            }
            Self::Contract(error) => write!(formatter, "contract validation failed: {error}"),
            Self::Json(error) => write!(formatter, "JSON encoding failed: {error}"),
            Self::Io(error) => write!(formatter, "I/O failed: {error}"),
        }
    }
}

impl std::error::Error for SimulationError {}

impl From<ContractError> for SimulationError {
    fn from(value: ContractError) -> Self {
        Self::Contract(value)
    }
}

impl From<serde_json::Error> for SimulationError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}

impl From<std::io::Error> for SimulationError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

#[derive(Debug, Clone)]
pub struct PublicCableContracts {
    pub profile: CableInteractionProfileV1,
    pub registry: RegistryV1,
    pub profile_artifact_digest: DigestV1,
    pub registry_artifact_digest: DigestV1,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SimulationArtifact {
    pub bytes: Vec<u8>,
    /// SHA-256 over every emitted byte before the receipt line, including line terminators.
    pub pre_receipt_sha256: String,
    pub pre_receipt_record_count: usize,
    /// Canonical SHA-256 of the seed/scenario/tier/tick fixture manifest (`SimulationConfig`).
    pub fixture_manifest_sha256: String,
    pub public_profile_sha256: String,
    pub public_registry_sha256: String,
}

#[derive(Serialize)]
struct SessionRecord<'a> {
    record_type: &'static str,
    schema_version: &'static str,
    watermark: &'static str,
    claim_ceiling: &'static str,
    session_id: String,
    scenario: &'static str,
    sensor_tier: &'static str,
    seed: u64,
    tick_interval_ns: i64,
    tick_count: u32,
    fixture_manifest_sha256: &'a str,
    profile_id: &'a str,
    profile_semantic_version: &'a str,
    profile_artifact_sha256: &'a str,
    registry_schema_version: &'a str,
    registry_artifact_sha256: &'a str,
    transformer_id: &'a str,
    transformer_version: &'a str,
    transformer_configuration_sha256: &'a str,
    clock_untrusted_tick: u32,
    calibration_unknown_tick: u32,
    calibration_missing_tick: u32,
}

#[derive(Serialize)]
struct ReceiptRecord<'a> {
    record_type: &'static str,
    schema_version: &'static str,
    watermark: &'static str,
    session_id: &'a str,
    pre_receipt_sha256: &'a str,
    pre_receipt_byte_count: usize,
    pre_receipt_record_count: usize,
    fixture_manifest_sha256: &'a str,
    public_profile_sha256: &'a str,
    public_registry_sha256: &'a str,
    claim_ceiling: &'static str,
}

#[derive(Clone, Copy)]
enum SensorGroup {
    Vehicle,
    MotorTorque,
    DirectTether,
    ScalarTension,
    VehicleVectorTension,
    ReelVectorTension,
    Geometry,
    Observer,
}

struct RuntimeBindings<'a> {
    profile: &'a CableInteractionProfileV1,
    normal_tension_n: f64,
}

/// Load and validate the exact public artifacts compiled into the generator.
///
/// # Errors
///
/// Returns an error if either committed JSON artifact is malformed, violates its v1 contract, or
/// names a transformer that this deterministic implementation does not implement.
pub fn load_public_contracts() -> Result<PublicCableContracts, SimulationError> {
    let profile: CableInteractionProfileV1 = serde_json::from_slice(PUBLIC_PROFILE_JSON)?;
    let registry: RegistryV1 = serde_json::from_slice(PUBLIC_REGISTRY_JSON)?;
    profile.validate()?;
    registry.validate()?;
    if profile.feature_transformer.transformer_id != FEATURE_TRANSFORMER_ID
        || profile.feature_transformer.transformer_version != FEATURE_TRANSFORMER_VERSION
    {
        return Err(SimulationError::InvalidConfiguration(
            "public profile names an unsupported deterministic transformer".to_owned(),
        ));
    }
    if !profile.thresholds.is_empty() {
        return Err(SimulationError::InvalidConfiguration(
            "public synthetic profile must remain threshold-free".to_owned(),
        ));
    }
    Ok(PublicCableContracts {
        profile,
        registry,
        profile_artifact_digest: DigestV1::sha256(PUBLIC_PROFILE_JSON),
        registry_artifact_digest: DigestV1::sha256(PUBLIC_REGISTRY_JSON),
    })
}

impl<'a> RuntimeBindings<'a> {
    fn new(contracts: &'a PublicCableContracts) -> Result<Self, SimulationError> {
        let normal_tension_n = contracts
            .profile
            .baseline
            .iter()
            .find(|baseline| baseline.semantic_id == "tether.tension_magnitude_n")
            .map(|baseline| baseline.mean)
            .ok_or_else(|| {
                SimulationError::InvalidConfiguration(
                    "public profile is missing tether.tension_magnitude_n baseline".to_owned(),
                )
            })?;
        Ok(Self {
            profile: &contracts.profile,
            normal_tension_n,
        })
    }

    fn has_calibration(&self, semantic_id: &str) -> bool {
        self.profile
            .calibrations
            .iter()
            .any(|calibration| calibration.semantic_id == semantic_id)
    }
}

/// Generate a deterministic NDJSON artifact.
///
/// Observation and annotation lines use the [`RecordV1`] codec. Session and receipt lines are
/// intentionally small generator-specific records.
///
/// # Errors
///
/// Returns an error when configuration is incomplete, a public artifact is invalid, a generated
/// semantic is not known by the embedded registry, or deterministic encoding fails.
pub fn generate_ndjson(config: &SimulationConfig) -> Result<SimulationArtifact, SimulationError> {
    validate_config(config)?;
    let contracts = load_public_contracts()?;
    let bindings = RuntimeBindings::new(&contracts)?;
    // The canonical SimulationConfig is the bounded cable-fixture manifest. Public profile,
    // registry, and transformer digests are bound beside it rather than caller-overridable.
    let fixture_manifest_digest = canonical_sha256(config)?;
    let fixture_artifact = fixture_artifact(&fixture_manifest_digest);
    let session_id = format!(
        "sim_{}_{}_{:016x}",
        config.scenario.as_str(),
        config.sensor_tier.as_str(),
        config.seed
    );
    let session = SessionRecord {
        record_type: "session",
        schema_version: SCHEMA_VERSION_V1,
        watermark: WATERMARK,
        claim_ceiling: SIM_SELF_ATTEST,
        session_id: session_id.clone(),
        scenario: config.scenario.as_str(),
        sensor_tier: config.sensor_tier.as_str(),
        seed: config.seed,
        tick_interval_ns: TICK_INTERVAL_NS,
        tick_count: config.tick_count,
        fixture_manifest_sha256: fixture_manifest_digest.as_str(),
        profile_id: &contracts.profile.profile_id,
        profile_semantic_version: &contracts.profile.semantic_version,
        profile_artifact_sha256: contracts.profile_artifact_digest.as_str(),
        registry_schema_version: &contracts.registry.schema_version,
        registry_artifact_sha256: contracts.registry_artifact_digest.as_str(),
        transformer_id: &contracts.profile.feature_transformer.transformer_id,
        transformer_version: &contracts.profile.feature_transformer.transformer_version,
        transformer_configuration_sha256: contracts
            .profile
            .feature_transformer
            .configuration_digest
            .as_str(),
        clock_untrusted_tick: CLOCK_UNTRUSTED_TICK,
        calibration_unknown_tick: CALIBRATION_UNKNOWN_TICK,
        calibration_missing_tick: CALIBRATION_MISSING_TICK,
    };

    let mut bytes = Vec::new();
    let mut record_count = 0;
    push_line(&mut bytes, &session)?;
    record_count += 1;

    for sample in generate_samples(config, contracts.profile.nominal_length_m) {
        for observation in
            raw_observations(config.sensor_tier, sample, &fixture_artifact, &bindings)
        {
            admit_observation(contracts.registry.validate_observation(&observation)?)?;
            push_contract_record(&mut bytes, RecordV1::ObservationExtension(observation))?;
            record_count += 1;
        }

        for observation in derived_observations(
            config.sensor_tier,
            sample,
            &fixture_artifact,
            &contracts.profile.feature_transformer,
            bindings.normal_tension_n,
        ) {
            admit_observation(contracts.registry.validate_observation(&observation)?)?;
            push_contract_record(&mut bytes, RecordV1::ObservationExtension(observation))?;
            record_count += 1;
        }
    }

    let annotation = fixture_annotation(config, &session_id, fixture_artifact);
    validate_public_annotation_with_registry(&contracts.registry, &annotation)?;
    push_contract_record(&mut bytes, RecordV1::EpisodeAnnotation(annotation))?;
    record_count += 1;

    let pre_receipt_byte_count = bytes.len();
    let pre_receipt_sha256 = DigestV1::sha256(&bytes).as_str().to_owned();
    let receipt = ReceiptRecord {
        record_type: "receipt",
        schema_version: SCHEMA_VERSION_V1,
        watermark: WATERMARK,
        session_id: &session_id,
        pre_receipt_sha256: &pre_receipt_sha256,
        pre_receipt_byte_count,
        pre_receipt_record_count: record_count,
        fixture_manifest_sha256: fixture_manifest_digest.as_str(),
        public_profile_sha256: contracts.profile_artifact_digest.as_str(),
        public_registry_sha256: contracts.registry_artifact_digest.as_str(),
        claim_ceiling: SIM_SELF_ATTEST,
    };
    push_line(&mut bytes, &receipt)?;

    Ok(SimulationArtifact {
        bytes,
        pre_receipt_sha256,
        pre_receipt_record_count: record_count,
        fixture_manifest_sha256: fixture_manifest_digest.as_str().to_owned(),
        public_profile_sha256: contracts.profile_artifact_digest.as_str().to_owned(),
        public_registry_sha256: contracts.registry_artifact_digest.as_str().to_owned(),
    })
}

/// Write an artifact without replacing an existing path.
///
/// # Errors
///
/// Returns an I/O error if the path already exists or the new file cannot be fully persisted.
pub fn write_create_new(path: &Path, artifact: &SimulationArtifact) -> Result<(), SimulationError> {
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(&artifact.bytes)?;
    file.sync_all()?;
    Ok(())
}

fn admit_observation(outcome: ObservationRegistryOutcomeV1) -> Result<(), SimulationError> {
    match outcome {
        ObservationRegistryOutcomeV1::KnownValidated { .. } => Ok(()),
        ObservationRegistryOutcomeV1::UnknownPreserved { key } => {
            Err(SimulationError::InvalidConfiguration(format!(
                "generator emitted an unregistered observation: {key}"
            )))
        }
    }
}

fn admit_annotation(outcome: AnnotationRegistryOutcomeV1) -> Result<(), SimulationError> {
    match outcome {
        AnnotationRegistryOutcomeV1::KnownValidated { .. } => Ok(()),
        AnnotationRegistryOutcomeV1::UnknownPreserved { key } => {
            Err(SimulationError::InvalidConfiguration(format!(
                "generator emitted an unregistered annotation: {key}"
            )))
        }
    }
}

/// Validate a cable annotation against the embedded public registry and the `confirmed_*`
/// epistemic boundary.
///
/// # Errors
///
/// Returns an error when a confirmed label is used for a candidate/unknown statement, when its
/// evidence source is not an independent non-model fixture/evidence source, or when the label is
/// not known by the embedded public registry.
pub fn validate_public_annotation(annotation: &EpisodeAnnotationV1) -> Result<(), SimulationError> {
    let contracts = load_public_contracts()?;
    validate_public_annotation_with_registry(&contracts.registry, annotation)
}

fn validate_public_annotation_with_registry(
    registry: &RegistryV1,
    annotation: &EpisodeAnnotationV1,
) -> Result<(), SimulationError> {
    annotation.validate()?;
    if annotation.label.starts_with("confirmed_") {
        let independent_source = matches!(
            annotation.label_source,
            AnnotationSourceV1::ControlledFixture
                | AnnotationSourceV1::PostRunInspection
                | AnnotationSourceV1::Video
                | AnnotationSourceV1::MotionCapture
                | AnnotationSourceV1::ContinuityTest
                | AnnotationSourceV1::DeterministicInjection
                | AnnotationSourceV1::OtherIndependentEvidence { .. }
        );
        if annotation.annotation_kind != AnnotationKindV1::GroundTruth
            || annotation.certainty != AnnotationCertaintyV1::Confirmed
            || !independent_source
            || annotation.evidence.is_empty()
        {
            return Err(SimulationError::InvalidConfiguration(
                "confirmed_* labels require confirmed ground truth and independent non-model evidence"
                    .to_owned(),
            ));
        }
    }
    admit_annotation(registry.validate_annotation(annotation)?)
}

fn validate_config(config: &SimulationConfig) -> Result<(), SimulationError> {
    if config.tick_count <= EVENT_START_TICK {
        return Err(SimulationError::InvalidConfiguration(format!(
            "tick_count must be greater than {EVENT_START_TICK}"
        )));
    }
    Ok(())
}

fn fixture_artifact(digest: &DigestV1) -> ArtifactReferenceV1 {
    ArtifactReferenceV1 {
        digest: digest.clone(),
        media_type: "application/vnd.kecku.synthetic-cable-fixture+json".to_owned(),
        role: ArtifactRoleV1::Fixture,
    }
}

fn fixture_annotation(
    config: &SimulationConfig,
    session_id: &str,
    evidence: ArtifactReferenceV1,
) -> EpisodeAnnotationV1 {
    let window_start_ns = model::START_TIME_NS + i64::from(EVENT_START_TICK) * TICK_INTERVAL_NS;
    let window_end_ns =
        model::START_TIME_NS + i64::from(config.tick_count.saturating_sub(1)) * TICK_INTERVAL_NS;
    EpisodeAnnotationV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        episode_id: session_id.to_owned(),
        window_start_ns,
        window_end_ns,
        taxonomy_namespace: TAXONOMY_NAMESPACE.to_owned(),
        taxonomy_version: TAXONOMY_VERSION.to_owned(),
        label: config.scenario.annotation_label().to_owned(),
        annotation_kind: AnnotationKindV1::GroundTruth,
        certainty: AnnotationCertaintyV1::Confirmed,
        label_source: AnnotationSourceV1::DeterministicInjection,
        annotator_id: "deterministic_fixture_v1".to_owned(),
        evidence: vec![evidence],
    }
}

fn raw_observations(
    tier: SensorTier,
    sample: Sample,
    artifact: &ArtifactReferenceV1,
    bindings: &RuntimeBindings<'_>,
) -> Vec<ObservationExtensionV1> {
    let missing = |group, semantic_id| raw_missing(tier, group, semantic_id, sample, bindings);
    vec![
        raw_f64(
            "vehicle.position_x_m",
            sample,
            sample.position_x_m,
            missing(SensorGroup::Vehicle, "vehicle.position_x_m"),
            "m",
            0.001,
            -100_000.0,
            100_000.0,
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_f64(
            "vehicle.input_speed_mps",
            sample,
            sample.motion_reference_mps,
            missing(SensorGroup::Vehicle, "vehicle.input_speed_mps"),
            "m/s",
            0.001,
            -100.0,
            100.0,
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_f64(
            "vehicle.ground_speed_mps",
            sample,
            sample.ground_speed_mps,
            missing(SensorGroup::Vehicle, "vehicle.ground_speed_mps"),
            "m/s",
            0.001,
            -100.0,
            100.0,
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_f64(
            "vehicle.motor_current_a",
            sample,
            sample.motor_current_a,
            missing(SensorGroup::Vehicle, "vehicle.motor_current_a"),
            "A",
            0.01,
            0.0,
            500.0,
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_f64(
            "vehicle.motor_torque_nm",
            sample,
            sample.motor_torque_nm,
            missing(SensorGroup::MotorTorque, "vehicle.motor_torque_nm"),
            "N*m",
            0.001,
            0.0,
            1_000.0,
            SensorGroup::MotorTorque,
            artifact,
        ),
        raw_f64(
            "vehicle.motor_rpm",
            sample,
            sample.motor_rpm,
            missing(SensorGroup::Vehicle, "vehicle.motor_rpm"),
            "rpm",
            1.0,
            0.0,
            100_000.0,
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_f64(
            "vehicle.imu_longitudinal_accel_mps2",
            sample,
            sample.imu_longitudinal_accel_mps2,
            missing(SensorGroup::Vehicle, "vehicle.imu_longitudinal_accel_mps2"),
            "m/s^2",
            0.001,
            -200.0,
            200.0,
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_bool(
            "vehicle.link_present",
            sample,
            sample.link_present,
            missing(SensorGroup::Vehicle, "vehicle.link_present"),
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_bool(
            "tether.continuity_present",
            sample,
            sample.continuity_present,
            missing(SensorGroup::Vehicle, "tether.continuity_present"),
            SensorGroup::Vehicle,
            artifact,
        ),
        raw_f64(
            "tether.payout_length_m",
            sample,
            sample.payout_length_m,
            missing(SensorGroup::DirectTether, "tether.payout_length_m"),
            "m",
            0.001,
            0.0,
            bindings.profile.nominal_length_m,
            SensorGroup::DirectTether,
            artifact,
        ),
        raw_f64(
            "tether.payout_rate_mps",
            sample,
            sample.payout_rate_mps,
            missing(SensorGroup::DirectTether, "tether.payout_rate_mps"),
            "m/s",
            0.001,
            -10.0,
            10.0,
            SensorGroup::DirectTether,
            artifact,
        ),
        raw_f64(
            "tether.reel_load_a",
            sample,
            sample.reel_load_a,
            missing(SensorGroup::DirectTether, "tether.reel_load_a"),
            "A",
            0.01,
            0.0,
            500.0,
            SensorGroup::DirectTether,
            artifact,
        ),
        raw_f64(
            "tether.tension_scalar_n",
            sample,
            sample.tension_scalar_n,
            missing(SensorGroup::ScalarTension, "tether.tension_scalar_n"),
            "N",
            0.01,
            0.0,
            1_000.0,
            SensorGroup::ScalarTension,
            artifact,
        ),
        raw_f64(
            "tether.vehicle_tension_x_n",
            sample,
            sample.vehicle_tension_x_n,
            missing(
                SensorGroup::VehicleVectorTension,
                "tether.vehicle_tension_x_n",
            ),
            "N",
            0.01,
            -1_000.0,
            1_000.0,
            SensorGroup::VehicleVectorTension,
            artifact,
        ),
        raw_f64(
            "tether.vehicle_tension_y_n",
            sample,
            sample.vehicle_tension_y_n,
            missing(
                SensorGroup::VehicleVectorTension,
                "tether.vehicle_tension_y_n",
            ),
            "N",
            0.01,
            -1_000.0,
            1_000.0,
            SensorGroup::VehicleVectorTension,
            artifact,
        ),
        raw_f64(
            "tether.vehicle_tension_z_n",
            sample,
            sample.vehicle_tension_z_n,
            missing(
                SensorGroup::VehicleVectorTension,
                "tether.vehicle_tension_z_n",
            ),
            "N",
            0.01,
            -1_000.0,
            1_000.0,
            SensorGroup::VehicleVectorTension,
            artifact,
        ),
        raw_f64(
            "tether.reel_tension_x_n",
            sample,
            sample.reel_tension_x_n,
            missing(SensorGroup::ReelVectorTension, "tether.reel_tension_x_n"),
            "N",
            0.01,
            -1_000.0,
            1_000.0,
            SensorGroup::ReelVectorTension,
            artifact,
        ),
        raw_f64(
            "tether.reel_tension_y_n",
            sample,
            sample.reel_tension_y_n,
            missing(SensorGroup::ReelVectorTension, "tether.reel_tension_y_n"),
            "N",
            0.01,
            -1_000.0,
            1_000.0,
            SensorGroup::ReelVectorTension,
            artifact,
        ),
        raw_f64(
            "tether.reel_tension_z_n",
            sample,
            sample.reel_tension_z_n,
            missing(SensorGroup::ReelVectorTension, "tether.reel_tension_z_n"),
            "N",
            0.01,
            -1_000.0,
            1_000.0,
            SensorGroup::ReelVectorTension,
            artifact,
        ),
        raw_f64(
            "tether.exit_pitch_deg",
            sample,
            sample.exit_pitch_deg,
            missing(SensorGroup::Geometry, "tether.exit_pitch_deg"),
            "deg",
            0.01,
            -180.0,
            180.0,
            SensorGroup::Geometry,
            artifact,
        ),
        raw_f64(
            "tether.exit_yaw_deg",
            sample,
            sample.exit_yaw_deg,
            missing(SensorGroup::Geometry, "tether.exit_yaw_deg"),
            "deg",
            0.01,
            -180.0,
            360.0,
            SensorGroup::Geometry,
            artifact,
        ),
        raw_f64(
            "tether.shape_proxy_lateral_offset_m",
            sample,
            sample.shape_proxy_lateral_offset_m,
            missing(SensorGroup::Geometry, "tether.shape_proxy_lateral_offset_m"),
            "m",
            0.001,
            -100.0,
            100.0,
            SensorGroup::Geometry,
            artifact,
        ),
        raw_f64(
            "vehicle.heading_deg",
            sample,
            sample.vehicle_heading_deg,
            missing(SensorGroup::Geometry, "vehicle.heading_deg"),
            "deg",
            0.01,
            -180.0,
            360.0,
            SensorGroup::Geometry,
            artifact,
        ),
        raw_bool(
            "observer.liveness",
            sample,
            sample.observer_liveness,
            clock_missing(sample),
            SensorGroup::Observer,
            artifact,
        ),
    ]
}

fn derived_observations(
    tier: SensorTier,
    sample: Sample,
    artifact: &ArtifactReferenceV1,
    transformer: &TransformerReferenceV1,
    normal_tension_n: f64,
) -> Vec<ObservationExtensionV1> {
    let features = derive_features(sample, normal_tension_n);
    let upstream = upstream_missing(sample);
    let scalar_missing = upstream.clone().or_else(|| {
        (!tier.has_scalar_tether_sensors()).then_some(MissingValueV1::SensorUnavailable)
    });
    let vector_missing = upstream.clone().or_else(|| {
        (!tier.has_vector_and_geometry_sensors()).then_some(MissingValueV1::SensorUnavailable)
    });
    let torque_missing = upstream.clone().or_else(|| {
        (sample.tick == CALIBRATION_UNKNOWN_TICK).then_some(MissingValueV1::Unknown {
            detail: "upstream_calibration_unknown".to_owned(),
        })
    });
    let scalar_calibration_missing = scalar_missing.clone().or_else(|| {
        (sample.tick == CALIBRATION_MISSING_TICK).then_some(MissingValueV1::Unknown {
            detail: "upstream_calibration_metadata_missing".to_owned(),
        })
    });
    let vector_calibration_missing = vector_missing.clone().or_else(|| {
        (sample.tick == CALIBRATION_MISSING_TICK).then_some(MissingValueV1::Unknown {
            detail: "upstream_calibration_metadata_missing".to_owned(),
        })
    });

    vec![
        derived_f64(
            "feature.progress_deficit",
            sample,
            features.progress_deficit,
            upstream.clone(),
            "1",
            artifact,
            transformer,
        ),
        derived_f64(
            "feature.effort_to_progress",
            sample,
            features.effort_to_progress,
            torque_missing,
            "N*s",
            artifact,
            transformer,
        ),
        derived_f64(
            "feature.payout_motion_residual",
            sample,
            features.payout_motion_residual,
            scalar_missing,
            "1",
            artifact,
            transformer,
        ),
        derived_f64(
            "feature.tension_model_residual",
            sample,
            features.tension_model_residual,
            scalar_calibration_missing,
            "1",
            artifact,
            transformer,
        ),
        derived_f64(
            "feature.force_transmission_ratio",
            sample,
            features.force_transmission_ratio,
            vector_calibration_missing,
            "1",
            artifact,
            transformer,
        ),
        derived_f64(
            "feature.tether_angle_misalignment",
            sample,
            features.tether_angle_misalignment,
            vector_missing,
            "deg",
            artifact,
            transformer,
        ),
        derived_bool(
            "feature.continuity_fault",
            sample,
            features.continuity_fault,
            upstream,
            artifact,
            transformer,
        ),
        derived_f64(
            "feature.observer_health",
            sample,
            features.observer_health,
            clock_missing(sample),
            "1",
            artifact,
            transformer,
        ),
    ]
}

fn raw_missing(
    tier: SensorTier,
    group: SensorGroup,
    semantic_id: &str,
    sample: Sample,
    bindings: &RuntimeBindings<'_>,
) -> Option<MissingValueV1> {
    if sample.tick == CLOCK_UNTRUSTED_TICK {
        return Some(MissingValueV1::ClockUntrusted);
    }
    if !group_available(tier, group) {
        return Some(MissingValueV1::SensorUnavailable);
    }
    if !sample.observer_liveness && !matches!(group, SensorGroup::Observer) {
        return Some(MissingValueV1::Unknown {
            detail: "observer_fault_injected".to_owned(),
        });
    }
    if calibration_required(semantic_id) && !bindings.has_calibration(semantic_id) {
        return Some(MissingValueV1::Unknown {
            detail: "profile_calibration_missing".to_owned(),
        });
    }
    if semantic_id == "vehicle.motor_torque_nm" && sample.tick == CALIBRATION_UNKNOWN_TICK {
        return Some(MissingValueV1::Unknown {
            detail: "calibration_unknown_injected".to_owned(),
        });
    }
    if is_tension_semantic(semantic_id) && sample.tick == CALIBRATION_MISSING_TICK {
        return Some(MissingValueV1::Unknown {
            detail: "calibration_metadata_missing_injected".to_owned(),
        });
    }
    None
}

const fn group_available(tier: SensorTier, group: SensorGroup) -> bool {
    match group {
        SensorGroup::Vehicle | SensorGroup::MotorTorque | SensorGroup::Observer => true,
        SensorGroup::DirectTether | SensorGroup::ScalarTension => tier.has_scalar_tether_sensors(),
        SensorGroup::VehicleVectorTension
        | SensorGroup::ReelVectorTension
        | SensorGroup::Geometry => tier.has_vector_and_geometry_sensors(),
    }
}

fn calibration_required(semantic_id: &str) -> bool {
    semantic_id == "vehicle.motor_torque_nm"
        || semantic_id.starts_with("tether.payout_")
        || semantic_id == "tether.reel_load_a"
        || is_tension_semantic(semantic_id)
        || semantic_id.starts_with("tether.exit_")
        || semantic_id == "tether.shape_proxy_lateral_offset_m"
}

fn is_tension_semantic(semantic_id: &str) -> bool {
    semantic_id == "tether.tension_scalar_n"
        || semantic_id.starts_with("tether.vehicle_tension_")
        || semantic_id.starts_with("tether.reel_tension_")
}

fn upstream_missing(sample: Sample) -> Option<MissingValueV1> {
    if sample.tick == CLOCK_UNTRUSTED_TICK {
        Some(MissingValueV1::ClockUntrusted)
    } else if !sample.observer_liveness {
        Some(MissingValueV1::Unknown {
            detail: "upstream_observation_missing".to_owned(),
        })
    } else {
        None
    }
}

fn clock_missing(sample: Sample) -> Option<MissingValueV1> {
    (sample.tick == CLOCK_UNTRUSTED_TICK).then_some(MissingValueV1::ClockUntrusted)
}

#[allow(clippy::too_many_arguments)]
fn raw_f64(
    semantic_id: &str,
    sample: Sample,
    value: f64,
    missing: Option<MissingValueV1>,
    unit: &str,
    resolution: f64,
    minimum: f64,
    maximum: f64,
    group: SensorGroup,
    artifact: &ArtifactReferenceV1,
) -> ObservationExtensionV1 {
    let value = quantize_f64(value, resolution);
    observation(
        semantic_id,
        sample,
        PrimitiveValueKindV1::F64,
        missing
            .as_ref()
            .map_or(Some(ObservationValueV1::F64(value)), |_| None),
        missing,
        Some(unit),
        Some(resolution),
        Some(NumericRangeV1 { minimum, maximum }),
        group,
        ObservationKindV1::Raw,
        artifact,
        None,
    )
}

fn raw_bool(
    semantic_id: &str,
    sample: Sample,
    value: bool,
    missing: Option<MissingValueV1>,
    group: SensorGroup,
    artifact: &ArtifactReferenceV1,
) -> ObservationExtensionV1 {
    observation(
        semantic_id,
        sample,
        PrimitiveValueKindV1::Bool,
        missing
            .as_ref()
            .map_or(Some(ObservationValueV1::Bool(value)), |_| None),
        missing,
        None,
        None,
        None,
        group,
        ObservationKindV1::Raw,
        artifact,
        None,
    )
}

fn derived_f64(
    semantic_id: &str,
    sample: Sample,
    value: f64,
    missing: Option<MissingValueV1>,
    unit: &str,
    artifact: &ArtifactReferenceV1,
    transformer: &TransformerReferenceV1,
) -> ObservationExtensionV1 {
    let resolution = 0.000_001;
    let value = quantize_f64(value, resolution);
    observation(
        semantic_id,
        sample,
        PrimitiveValueKindV1::F64,
        missing
            .as_ref()
            .map_or(Some(ObservationValueV1::F64(value)), |_| None),
        missing,
        Some(unit),
        Some(resolution),
        None,
        SensorGroup::Observer,
        ObservationKindV1::Derived,
        artifact,
        Some(transformer.clone()),
    )
}

/// Quantize serialized measurements to their declared resolution.
///
/// Even pinned portable math produces values finer than the sensor contract. Sub-resolution
/// differences must never change the public NDJSON bytes or their regeneration digest.
fn quantize_f64(value: f64, resolution: f64) -> f64 {
    let quantized = (value / resolution).round() * resolution;
    if quantized == 0.0 { 0.0 } else { quantized }
}

fn derived_bool(
    semantic_id: &str,
    sample: Sample,
    value: bool,
    missing: Option<MissingValueV1>,
    artifact: &ArtifactReferenceV1,
    transformer: &TransformerReferenceV1,
) -> ObservationExtensionV1 {
    observation(
        semantic_id,
        sample,
        PrimitiveValueKindV1::Bool,
        missing
            .as_ref()
            .map_or(Some(ObservationValueV1::Bool(value)), |_| None),
        missing,
        None,
        None,
        None,
        SensorGroup::Observer,
        ObservationKindV1::Derived,
        artifact,
        Some(transformer.clone()),
    )
}

#[allow(clippy::too_many_arguments)]
fn observation(
    semantic_id: &str,
    sample: Sample,
    value_kind: PrimitiveValueKindV1,
    value: Option<ObservationValueV1>,
    missing: Option<MissingValueV1>,
    unit: Option<&str>,
    resolution: Option<f64>,
    valid_range: Option<NumericRangeV1>,
    group: SensorGroup,
    observation_kind: ObservationKindV1,
    artifact: &ArtifactReferenceV1,
    transformer: Option<TransformerReferenceV1>,
) -> ObservationExtensionV1 {
    let source = source_for(group);
    let quality = quality_for(missing.as_ref());
    let clock_basis = if matches!(missing, Some(MissingValueV1::ClockUntrusted)) {
        ClockBasisV1::Unknown
    } else {
        ClockBasisV1::HostReceived
    };

    ObservationExtensionV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        namespace: OBSERVATION_NAMESPACE.to_owned(),
        semantic_id: semantic_id.to_owned(),
        semantic_version: OBSERVATION_SEMANTIC_VERSION.to_owned(),
        event_time_ns: sample.event_time_ns,
        clock_basis,
        source,
        observation_kind,
        value_kind,
        value,
        missing,
        unit: unit.map(str::to_owned),
        resolution,
        valid_range,
        quality,
        raw_artifacts: vec![artifact.clone()],
        transformer,
    }
}

fn source_for(group: SensorGroup) -> SourceReferenceV1 {
    let (source_id, sensor_location) = match group {
        SensorGroup::Vehicle => ("synthetic_vehicle_telemetry", "vehicle"),
        SensorGroup::MotorTorque => ("synthetic_motor_torque", "vehicle_drive"),
        SensorGroup::DirectTether => ("synthetic_tether_reel", "reel"),
        SensorGroup::ScalarTension => ("synthetic_scalar_tension", "vehicle_end"),
        SensorGroup::VehicleVectorTension => ("synthetic_vector_tension", "vehicle_end"),
        SensorGroup::ReelVectorTension => ("synthetic_vector_tension", "reel_end"),
        SensorGroup::Geometry => ("synthetic_tether_geometry", "tether_shape_proxy"),
        SensorGroup::Observer => ("synthetic_observer", "sidecar"),
    };
    SourceReferenceV1 {
        source_id: source_id.to_owned(),
        sensor_location: sensor_location.to_owned(),
    }
}

fn quality_for(missing: Option<&MissingValueV1>) -> QualityV1 {
    let Some(missing) = missing else {
        return QualityV1 {
            status: QualityStatusV1::Good,
            score: Some(1.0),
            flags: Vec::new(),
        };
    };
    let flags = match missing {
        MissingValueV1::ClockUntrusted => vec![QualityFlagV1::ClockSkew],
        MissingValueV1::Unknown { detail } if detail.contains("calibration") => {
            vec![QualityFlagV1::CalibrationUnknown]
        }
        MissingValueV1::Unknown { detail }
            if detail == "observer_fault_injected" || detail == "upstream_observation_missing" =>
        {
            vec![QualityFlagV1::ObserverDegraded]
        }
        _ => Vec::new(),
    };
    QualityV1 {
        status: QualityStatusV1::Unknown,
        score: None,
        flags,
    }
}

fn push_contract_record(bytes: &mut Vec<u8>, record: RecordV1) -> Result<(), SimulationError> {
    bytes.extend_from_slice(&encode_ndjson(&[record])?);
    Ok(())
}

fn push_line<T: Serialize>(bytes: &mut Vec<u8>, record: &T) -> Result<(), SimulationError> {
    serde_json::to_writer(&mut *bytes, record)?;
    bytes.push(b'\n');
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_field_contracts::decode_ndjson;

    fn json_lines(artifact: &SimulationArtifact) -> Vec<serde_json::Value> {
        std::str::from_utf8(&artifact.bytes)
            .expect("generated NDJSON must be UTF-8")
            .lines()
            .map(|line| serde_json::from_str(line).expect("generated line must be JSON"))
            .collect()
    }

    fn annotation_label(artifact: &SimulationArtifact) -> String {
        json_lines(artifact)
            .into_iter()
            .find(|line| line["record_type"] == "episode_annotation")
            .expect("annotation record")["record"]["label"]
            .as_str()
            .expect("label string")
            .to_owned()
    }

    fn observations(artifact: &SimulationArtifact, semantic_id: &str) -> Vec<serde_json::Value> {
        json_lines(artifact)
            .into_iter()
            .filter(|line| {
                line["record_type"] == "observation_extension"
                    && line["record"]["semantic_id"] == semantic_id
            })
            .map(|line| line["record"].clone())
            .collect()
    }

    fn last_observation(artifact: &SimulationArtifact, semantic_id: &str) -> serde_json::Value {
        observations(artifact, semantic_id)
            .pop()
            .expect("matching observation")
    }

    fn observation_at(
        artifact: &SimulationArtifact,
        semantic_id: &str,
        tick: u32,
    ) -> serde_json::Value {
        let expected_time = model::START_TIME_NS + i64::from(tick) * TICK_INTERVAL_NS;
        observations(artifact, semantic_id)
            .into_iter()
            .find(|observation| observation["event_time_ns"] == expected_time)
            .expect("observation at tick")
    }

    fn assert_keys_are_observational(value: &serde_json::Value) {
        match value {
            serde_json::Value::Object(map) => {
                for (key, nested) in map {
                    assert!(!matches!(
                        key.as_str(),
                        "command" | "action" | "desired_state" | "recommended_action"
                    ));
                    assert_keys_are_observational(nested);
                }
            }
            serde_json::Value::Array(values) => {
                for nested in values {
                    assert_keys_are_observational(nested);
                }
            }
            _ => {}
        }
    }

    #[test]
    fn serialized_numeric_values_respect_their_declared_resolution() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::PartialSnag,
            SensorTier::T2,
            42,
        ))
        .expect("fixture");
        for line in json_lines(&artifact) {
            if line["record_type"] != "observation_extension" {
                continue;
            }
            let record = &line["record"];
            let (Some(value), Some(resolution)) = (
                record["value"]["value"].as_f64(),
                record["resolution"].as_f64(),
            ) else {
                continue;
            };
            let scaled = value / resolution;
            assert!(
                (scaled - scaled.round()).abs() < 1.0e-8,
                "{} emitted {value} outside resolution {resolution}",
                record["semantic_id"]
            );
        }
    }

    #[test]
    fn all_eight_fixture_states_emit_injected_ground_truth() {
        for scenario in ScenarioKind::ALL {
            let artifact = generate_ndjson(&SimulationConfig::new(scenario, SensorTier::T2, 42))
                .expect("scenario should generate");
            assert_eq!(annotation_label(&artifact), scenario.annotation_label());
            assert!(
                artifact
                    .bytes
                    .windows(WATERMARK.len())
                    .any(|window| window == WATERMARK.as_bytes())
            );
        }
    }

    #[test]
    fn observations_and_annotations_round_trip_through_record_v1_codec() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::PartialSnag,
            SensorTier::T2,
            42,
        ))
        .expect("fixture");
        let mut contract_lines = Vec::new();
        for line in std::str::from_utf8(&artifact.bytes).expect("UTF-8").lines() {
            let value: serde_json::Value = serde_json::from_str(line).expect("JSON");
            if matches!(
                value["record_type"].as_str(),
                Some("observation_extension" | "episode_annotation")
            ) {
                contract_lines.extend_from_slice(line.as_bytes());
                contract_lines.push(b'\n');
            }
        }
        let decoded = decode_ndjson(&contract_lines).expect("RecordV1 stream");
        assert_eq!(decoded.len(), artifact.pre_receipt_record_count - 1);
        assert!(decoded.iter().all(|record| matches!(
            record,
            RecordV1::ObservationExtension(_) | RecordV1::EpisodeAnnotation(_)
        )));
    }

    #[test]
    fn public_artifact_digests_and_runtime_bindings_are_in_session() {
        let contracts = load_public_contracts().expect("public contracts");
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::NormalDrag,
            SensorTier::T0,
            1,
        ))
        .expect("fixture");
        let session = json_lines(&artifact).remove(0);
        assert_eq!(
            session["profile_artifact_sha256"],
            contracts.profile_artifact_digest.as_str()
        );
        assert_eq!(
            session["registry_artifact_sha256"],
            contracts.registry_artifact_digest.as_str()
        );
        assert_eq!(session["profile_id"], contracts.profile.profile_id);
        assert_eq!(
            session["transformer_configuration_sha256"],
            contracts
                .profile
                .feature_transformer
                .configuration_digest
                .as_str()
        );
    }

    #[test]
    fn tier_capabilities_are_distinct_and_missing_fields_are_retained() {
        let t0 = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::NormalDrag,
            SensorTier::T0,
            11,
        ))
        .expect("T0");
        assert!(last_observation(&t0, "vehicle.motor_torque_nm")["value"].is_object());
        assert_eq!(
            last_observation(&t0, "tether.tension_scalar_n")["missing"]["reason"],
            "sensor_unavailable"
        );

        let t1 = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::NormalDrag,
            SensorTier::T1,
            11,
        ))
        .expect("T1");
        assert!(last_observation(&t1, "tether.tension_scalar_n")["value"].is_object());
        assert_eq!(
            last_observation(&t1, "tether.vehicle_tension_x_n")["missing"]["reason"],
            "sensor_unavailable"
        );
        assert_eq!(
            last_observation(&t1, "tether.shape_proxy_lateral_offset_m")["missing"]["reason"],
            "sensor_unavailable"
        );

        let t2 = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::NormalDrag,
            SensorTier::T2,
            11,
        ))
        .expect("T2");
        assert!(last_observation(&t2, "tether.vehicle_tension_x_n")["value"].is_object());
        assert!(last_observation(&t2, "tether.reel_tension_x_n")["value"].is_object());
        assert!(last_observation(&t2, "tether.shape_proxy_lateral_offset_m")["value"].is_object());
    }

    #[test]
    fn clock_and_calibration_faults_force_deterministic_abstention() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::NormalDrag,
            SensorTier::T2,
            17,
        ))
        .expect("fixture");

        let clock = observation_at(&artifact, "vehicle.ground_speed_mps", CLOCK_UNTRUSTED_TICK);
        assert!(clock["value"].is_null());
        assert_eq!(clock["missing"]["reason"], "clock_untrusted");
        assert_eq!(clock["clock_basis"], "unknown");
        assert_eq!(clock["quality"]["flags"][0], "clock_skew");
        assert!(
            observation_at(&artifact, "feature.progress_deficit", CLOCK_UNTRUSTED_TICK,)["value"]
                .is_null()
        );

        let calibration_unknown = observation_at(
            &artifact,
            "vehicle.motor_torque_nm",
            CALIBRATION_UNKNOWN_TICK,
        );
        assert!(calibration_unknown["value"].is_null());
        assert_eq!(
            calibration_unknown["missing"]["detail"],
            "calibration_unknown_injected"
        );
        assert_eq!(
            calibration_unknown["quality"]["flags"][0],
            "calibration_unknown"
        );
        assert!(
            observation_at(
                &artifact,
                "feature.effort_to_progress",
                CALIBRATION_UNKNOWN_TICK,
            )["value"]
                .is_null()
        );

        let calibration_missing = observation_at(
            &artifact,
            "tether.tension_scalar_n",
            CALIBRATION_MISSING_TICK,
        );
        assert!(calibration_missing["value"].is_null());
        assert_eq!(
            calibration_missing["missing"]["detail"],
            "calibration_metadata_missing_injected"
        );
        assert!(
            observation_at(
                &artifact,
                "feature.tension_model_residual",
                CALIBRATION_MISSING_TICK,
            )["value"]
                .is_null()
        );
    }

    #[test]
    fn profile_calibration_removal_fails_closed_as_missing() {
        let mut contracts = load_public_contracts().expect("public contracts");
        contracts
            .profile
            .calibrations
            .retain(|entry| entry.semantic_id != "vehicle.motor_torque_nm");
        let bindings = RuntimeBindings::new(&contracts).expect("bindings");
        let sample = generate_samples(
            &SimulationConfig::new(ScenarioKind::NormalDrag, SensorTier::T0, 99),
            contracts.profile.nominal_length_m,
        )[0];
        let fixture = fixture_artifact(&DigestV1::sha256(b"fixture"));
        let torque = raw_observations(SensorTier::T0, sample, &fixture, &bindings)
            .into_iter()
            .find(|observation| observation.semantic_id == "vehicle.motor_torque_nm")
            .expect("torque observation");
        assert_eq!(
            torque.missing,
            Some(MissingValueV1::Unknown {
                detail: "profile_calibration_missing".to_owned()
            })
        );
        assert_eq!(
            torque.quality.flags,
            vec![QualityFlagV1::CalibrationUnknown]
        );
    }

    #[test]
    fn partial_snag_break_and_mobility_stall_remain_distinguishable() {
        let generate = |scenario| {
            generate_ndjson(&SimulationConfig::new(scenario, SensorTier::T2, 9))
                .expect("scenario should generate")
        };
        let snag = generate(ScenarioKind::PartialSnag);
        let broken = generate(ScenarioKind::TetherBreak);
        let stalled = generate(ScenarioKind::MobilityStall);

        assert_eq!(
            last_observation(&snag, "feature.continuity_fault")["value"]["value"],
            false
        );
        assert_eq!(
            last_observation(&broken, "feature.continuity_fault")["value"]["value"],
            true
        );
        let snag_tension =
            last_observation(&snag, "feature.tension_model_residual")["value"]["value"]
                .as_f64()
                .expect("snag tension feature");
        let stalled_tension = last_observation(&stalled, "feature.tension_model_residual")["value"]
            ["value"]
            .as_f64()
            .expect("stall tension feature");
        assert!(snag_tension > 1.0);
        assert!(stalled_tension < 0.1);
    }

    #[test]
    fn observer_fault_is_explicit_and_propagates_missingness() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::ObserverFault,
            SensorTier::T2,
            10,
        ))
        .expect("scenario should generate");
        let speed = last_observation(&artifact, "vehicle.ground_speed_mps");
        assert!(speed["value"].is_null());
        assert_eq!(speed["missing"]["detail"], "observer_fault_injected");
        let feature = last_observation(&artifact, "feature.progress_deficit");
        assert!(feature["value"].is_null());
        assert_eq!(feature["missing"]["detail"], "upstream_observation_missing");
        assert_eq!(
            last_observation(&artifact, "feature.observer_health")["value"]["value"],
            0.0
        );
    }

    #[test]
    fn identical_inputs_are_byte_identical_and_seed_changes_receipt() {
        let config = SimulationConfig::new(ScenarioKind::PartialSnag, SensorTier::T2, 55);
        let first = generate_ndjson(&config).expect("first");
        let second = generate_ndjson(&config).expect("second");
        assert_eq!(first, second);

        let changed = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::PartialSnag,
            SensorTier::T2,
            56,
        ))
        .expect("changed seed");
        assert_ne!(first.pre_receipt_sha256, changed.pre_receipt_sha256);
        assert_ne!(first.bytes, changed.bytes);
    }

    #[test]
    fn receipt_hashes_every_preceding_byte() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::ReelJam,
            SensorTier::T1,
            77,
        ))
        .expect("scenario should generate");
        let receipt_start = artifact
            .bytes
            .windows(b"{\"record_type\":\"receipt\"".len())
            .rposition(|window| window == b"{\"record_type\":\"receipt\"")
            .expect("receipt line");
        assert_eq!(
            DigestV1::sha256(&artifact.bytes[..receipt_start]).as_str(),
            artifact.pre_receipt_sha256
        );
        let receipt = json_lines(&artifact).pop().expect("last record");
        assert_eq!(receipt["record_type"], "receipt");
        assert_eq!(receipt["pre_receipt_sha256"], artifact.pre_receipt_sha256);
        assert_eq!(
            receipt["pre_receipt_record_count"],
            artifact.pre_receipt_record_count
        );
    }

    #[test]
    fn emitted_surface_has_no_instruction_shaped_field_names() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::CompleteRestraintOrLoop,
            SensorTier::T2,
            12,
        ))
        .expect("scenario should generate");
        for line in json_lines(&artifact) {
            assert_keys_are_observational(&line);
            if let Some(semantic_id) = line["record"]["semantic_id"].as_str() {
                let segments: Vec<_> = semantic_id.split(['.', '_']).collect();
                assert!(!segments.contains(&"command"));
                assert!(!segments.contains(&"action"));
                assert!(!semantic_id.contains("desired_state"));
                assert!(!semantic_id.contains("recommended_action"));
            }
        }
    }

    #[test]
    fn create_new_refuses_to_replace_existing_artifact() {
        let artifact = generate_ndjson(&SimulationConfig::new(
            ScenarioKind::LengthLimit,
            SensorTier::T1,
            13,
        ))
        .expect("scenario should generate");
        let path = std::env::temp_dir().join(format!(
            "musubi-cable-sim-create-new-{}-{}.ndjson",
            std::process::id(),
            artifact.pre_receipt_sha256.trim_start_matches("sha256:")
        ));
        if path.exists() {
            std::fs::remove_file(&path).expect("remove stale test file");
        }
        write_create_new(&path, &artifact).expect("first create");
        let second = write_create_new(&path, &artifact).expect_err("replacement must fail");
        assert!(matches!(second, SimulationError::Io(_)));
        std::fs::remove_file(path).expect("cleanup");
    }
}
