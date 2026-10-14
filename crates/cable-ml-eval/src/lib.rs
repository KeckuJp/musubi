//! Synthetic-only offline model comparison for cable-interaction observations.
//!
//! This crate measures three bounded methods on one deterministic split. It does not select a
//! production model, claim field accuracy, or admit customer data.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]
#![allow(
    clippy::cast_precision_loss,
    clippy::doc_markdown,
    clippy::module_name_repetitions,
    clippy::too_many_lines
)]

mod data;
mod models;
mod report;

use std::collections::BTreeSet;
use std::fmt;

use data::{
    FEATURE_IDS, MODEL_SCENARIOS, ambiguous_signatures, coarse_evidence_signature, load_episodes,
};
use models::{Prediction, TrainedModel, train_models};
pub use report::{
    CaseEvaluation, CaseOutcome, MetricSummary, ModelEvaluation, ModelOutput, OracleReference,
};
use serde::Serialize;
use sha2::{Digest as _, Sha256};

pub const WATERMARK: &str = "SIMULATED";

const REQUIRED_HOLDS: [&str; 6] = [
    "HOLD_CUSTOMER_DATA",
    "HOLD_UNSLOTH",
    "HOLD_SEQUENCE_MODEL",
    "HOLD_FIELD_ACCURACY",
    "HOLD_ML_SUPERIORITY",
    "HOLD_MODEL_ADOPTION",
];

#[derive(Debug)]
pub enum EvaluationError {
    InvalidConfiguration(String),
    InvalidData(String),
    Simulation(musubi_cable_sim::SimulationError),
    Json(serde_json::Error),
}

impl fmt::Display for EvaluationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfiguration(reason) => {
                write!(formatter, "invalid configuration: {reason}")
            }
            Self::InvalidData(reason) => write!(formatter, "invalid synthetic fixture: {reason}"),
            Self::Simulation(error) => {
                write!(formatter, "cable fixture generation failed: {error}")
            }
            Self::Json(error) => write!(formatter, "JSON processing failed: {error}"),
        }
    }
}

impl std::error::Error for EvaluationError {}

impl From<musubi_cable_sim::SimulationError> for EvaluationError {
    fn from(value: musubi_cable_sim::SimulationError) -> Self {
        Self::Simulation(value)
    }
}

impl From<serde_json::Error> for EvaluationError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EvaluationConfig {
    pub split_seed: u64,
    pub train_seed_count: usize,
    pub test_seed_count: usize,
    pub sensor_tier: musubi_cable_sim::SensorTier,
}

impl Default for EvaluationConfig {
    fn default() -> Self {
        Self {
            split_seed: 42,
            train_seed_count: 12,
            test_seed_count: 6,
            sensor_tier: musubi_cable_sim::SensorTier::T2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SeedSplit {
    pub split_seed: u64,
    pub train_seeds: Vec<u64>,
    pub test_seeds: Vec<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EvaluationPayload {
    pub schema_version: String,
    pub watermark: String,
    pub claim_ceiling: String,
    pub fixture_source: String,
    pub configuration: EvaluationConfig,
    pub seed_split: SeedSplit,
    pub feature_ids: Vec<String>,
    pub model_classes: Vec<String>,
    pub oracle_model_separation: String,
    pub abstention_policy: String,
    pub model_evaluations: Vec<ModelEvaluation>,
    pub guardrail_passed: bool,
    pub guardrail_failures: Vec<String>,
    pub adoption_disposition: String,
    pub ml_superiority_claimed: bool,
    pub field_accuracy_claimed: bool,
    pub interpretation: String,
    pub holds: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct EvaluationReport {
    pub report_digest: String,
    pub digest_basis: String,
    pub payload: EvaluationPayload,
}

impl EvaluationReport {
    /// Validate digest integrity, honesty guardrails, and the unconditional adoption HOLD.
    ///
    /// # Errors
    /// Returns an error when metrics and guardrail status diverge or a comparison report attempts
    /// to authorize model adoption.
    pub fn validate(&self) -> Result<(), EvaluationError> {
        if self.payload.schema_version != "1"
            || self.payload.watermark != WATERMARK
            || self.payload.claim_ceiling != "SIM_SELF_ATTEST"
            || self.digest_basis != "sha256_of_compact_payload_json_v1"
        {
            return Err(EvaluationError::InvalidData(
                "evaluation schema, SIMULATED watermark, claim ceiling, or digest basis drifted"
                    .to_owned(),
            ));
        }
        let expected_digest = sha256_bytes(&serde_json::to_vec(&self.payload)?);
        if self.report_digest != expected_digest {
            return Err(EvaluationError::InvalidData(
                "evaluation report digest mismatch".to_owned(),
            ));
        }
        let expected_models = BTreeSet::from([
            "deterministic_baseline",
            "ovr_logistic_regression",
            "gradient_boosted_decision_stumps",
        ]);
        let actual_models: BTreeSet<_> = self
            .payload
            .model_evaluations
            .iter()
            .map(|evaluation| evaluation.model_id.as_str())
            .collect();
        if self.payload.model_evaluations.len() != expected_models.len()
            || actual_models != expected_models
        {
            return Err(EvaluationError::InvalidData(
                "evaluation must contain each frozen comparison method exactly once".to_owned(),
            ));
        }
        for evaluation in &self.payload.model_evaluations {
            validate_evaluation_metrics(evaluation)?;
        }
        let expected_failures = guardrail_failures(&self.payload.model_evaluations);
        if self.payload.guardrail_failures != expected_failures
            || self.payload.guardrail_passed != expected_failures.is_empty()
        {
            return Err(EvaluationError::InvalidData(
                "guardrail status does not match false-certainty/ambiguity metrics".to_owned(),
            ));
        }
        if !self.payload.guardrail_passed || !self.payload.guardrail_failures.is_empty() {
            return Err(EvaluationError::InvalidData(
                "synthetic acceptance requires false_certainty=0 and ambiguity_honesty=1 for every compared method"
                    .to_owned(),
            ));
        }
        if self.payload.adoption_disposition != "HOLD"
            || !self
                .payload
                .holds
                .iter()
                .map(String::as_str)
                .eq(REQUIRED_HOLDS)
            || self.payload.ml_superiority_claimed
            || self.payload.field_accuracy_claimed
        {
            return Err(EvaluationError::InvalidData(
                "synthetic comparison must retain the complete ordered HOLD vocabulary".to_owned(),
            ));
        }
        Ok(())
    }
}

fn validate_evaluation_metrics(evaluation: &ModelEvaluation) -> Result<(), EvaluationError> {
    for case in evaluation
        .primary_cases
        .iter()
        .chain(&evaluation.ambiguity_probe_cases)
    {
        let expected_outcome = match &case.model_output.candidate_label {
            Some(label) if label == &case.oracle.label => CaseOutcome::Correct,
            Some(_) => CaseOutcome::Incorrect,
            None => CaseOutcome::Abstained,
        };
        if case.outcome != expected_outcome
            || (case.model_output.candidate_label.is_some()
                == case.model_output.abstention_reason.is_some())
            || case
                .model_output
                .score_margin
                .is_some_and(|margin| !margin.is_finite() || margin < 0.0)
        {
            return Err(EvaluationError::InvalidData(format!(
                "case outcome/model-output mismatch for {}:{}",
                evaluation.model_id, case.case_id
            )));
        }
    }

    let primary = MetricSummary::from_cases(&evaluation.primary_cases);
    let ambiguity = MetricSummary::from_cases(&evaluation.ambiguity_probe_cases);
    let false_certainty = primary.incorrect;
    let false_certainty_rate = report::ratio(false_certainty, primary.total);
    let ambiguity_honesty = report::ratio(ambiguity.abstained, ambiguity.total);
    if evaluation.primary_metrics != primary
        || evaluation.ambiguity_probe_metrics != ambiguity
        || evaluation.false_certainty != false_certainty
        || (evaluation.false_certainty_rate - false_certainty_rate).abs() > f64::EPSILON
        || (evaluation.ambiguity_honesty - ambiguity_honesty).abs() > f64::EPSILON
    {
        return Err(EvaluationError::InvalidData(format!(
            "published metrics do not match sealed cases for {}",
            evaluation.model_id
        )));
    }
    Ok(())
}

/// Run all three methods on one deterministic seed split.
///
/// # Errors
///
/// Returns an error when the split is empty, a synthetic fixture violates its public contract, or
/// JSON records cannot be decoded.
pub fn evaluate(config: &EvaluationConfig) -> Result<EvaluationReport, EvaluationError> {
    if config.train_seed_count == 0 || config.test_seed_count == 0 {
        return Err(EvaluationError::InvalidConfiguration(
            "train_seed_count and test_seed_count must be positive".to_owned(),
        ));
    }
    let seed_split = derive_seed_split(config);
    let train = load_episodes(&seed_split.train_seeds, config.sensor_tier, false)?;
    let primary_test = load_episodes(&seed_split.test_seeds, config.sensor_tier, true)?;

    // The ambiguity vocabulary is frozen from training seeds at T0. Test oracle labels are not
    // consulted to decide whether an evidence signature is ambiguous.
    let ambiguity_reference = load_episodes(
        &seed_split.train_seeds,
        musubi_cable_sim::SensorTier::T0,
        false,
    )?;
    let ambiguous = ambiguous_signatures(&ambiguity_reference);
    let ambiguity_test_all = load_episodes(
        &seed_split.test_seeds,
        musubi_cable_sim::SensorTier::T0,
        false,
    )?;
    let ambiguity_test: Vec<_> = ambiguity_test_all
        .into_iter()
        .filter(|episode| {
            coarse_evidence_signature(&episode.features)
                .is_some_and(|signature| ambiguous.contains(&signature))
        })
        .collect();
    if ambiguity_test.is_empty() {
        return Err(EvaluationError::InvalidData(
            "ambiguity probe produced no indistinguishable signatures".to_owned(),
        ));
    }

    let models = train_models(&train);
    let model_evaluations: Vec<ModelEvaluation> = models
        .iter()
        .map(|model| evaluate_model(model, &primary_test, &ambiguity_test, &ambiguous))
        .collect();
    let guardrail_failures = guardrail_failures(&model_evaluations);
    let guardrail_passed = guardrail_failures.is_empty();
    let payload = EvaluationPayload {
        schema_version: "1".to_owned(),
        watermark: WATERMARK.to_owned(),
        claim_ceiling: "SIM_SELF_ATTEST".to_owned(),
        fixture_source: "musubi-cable-sim synthetic NDJSON".to_owned(),
        configuration: config.clone(),
        seed_split,
        feature_ids: FEATURE_IDS.iter().map(|value| (*value).to_owned()).collect(),
        model_classes: MODEL_SCENARIOS
            .into_iter()
            .map(|scenario| scenario.as_str().to_owned())
            .collect(),
        oracle_model_separation:
            "oracle is deterministic_injection; model_output is evaluated afterward and cannot rewrite it"
                .to_owned(),
        abstention_policy:
            "observer unavailable, training-declared T0 signature collision, or low model separation"
                .to_owned(),
        model_evaluations,
        guardrail_passed,
        guardrail_failures,
        adoption_disposition: "HOLD".to_owned(),
        ml_superiority_claimed: false,
        field_accuracy_claimed: false,
        interpretation:
            "synthetic method comparison only; measurements do not select ML or establish field performance"
                .to_owned(),
        holds: REQUIRED_HOLDS
            .into_iter()
            .map(str::to_owned)
            .collect(),
    };
    let compact_payload = serde_json::to_vec(&payload)?;
    let report = EvaluationReport {
        report_digest: sha256_bytes(&compact_payload),
        digest_basis: "sha256_of_compact_payload_json_v1".to_owned(),
        payload,
    };
    report.validate()?;
    Ok(report)
}

fn guardrail_failures(evaluations: &[ModelEvaluation]) -> Vec<String> {
    let mut failures = Vec::new();
    for evaluation in evaluations {
        if evaluation.false_certainty != 0 {
            failures.push(format!("FALSE_CERTAINTY_NONZERO:{}", evaluation.model_id));
        }
        if (evaluation.ambiguity_honesty - 1.0).abs() > f64::EPSILON {
            failures.push(format!(
                "AMBIGUITY_ABSTENTION_INCOMPLETE:{}",
                evaluation.model_id
            ));
        }
    }
    failures
}

/// Encode one compact JSON report suitable for standard output.
///
/// # Errors
///
/// Returns an error if a report cannot be generated or serialized.
pub fn evaluate_json(config: &EvaluationConfig) -> Result<String, EvaluationError> {
    Ok(serde_json::to_string(&evaluate(config)?)?)
}

fn evaluate_model(
    model: &TrainedModel,
    primary_test: &[data::Episode],
    ambiguity_test: &[data::Episode],
    ambiguous: &BTreeSet<String>,
) -> ModelEvaluation {
    let primary_cases: Vec<_> = primary_test
        .iter()
        .map(|episode| evaluate_case(model, episode, ambiguous))
        .collect();
    let ambiguity_probe_cases: Vec<_> = ambiguity_test
        .iter()
        .map(|episode| evaluate_case(model, episode, ambiguous))
        .collect();
    let primary_metrics = MetricSummary::from_cases(&primary_cases);
    let ambiguity_probe_metrics = MetricSummary::from_cases(&ambiguity_probe_cases);
    let false_certainty = primary_metrics.incorrect;
    let ambiguity_abstained = ambiguity_probe_metrics.abstained;
    ModelEvaluation {
        model_id: model.model_id().to_owned(),
        model_version: TrainedModel::model_version().to_owned(),
        false_certainty,
        false_certainty_rate: report::ratio(false_certainty, primary_metrics.total),
        ambiguity_honesty: report::ratio(ambiguity_abstained, ambiguity_probe_metrics.total),
        primary_metrics,
        ambiguity_probe_metrics,
        primary_cases,
        ambiguity_probe_cases,
    }
}

fn evaluate_case(
    model: &TrainedModel,
    episode: &data::Episode,
    ambiguous: &BTreeSet<String>,
) -> CaseEvaluation {
    let prediction = if !episode.features.observer_available() {
        Prediction::abstain("observer_unavailable")
    } else if episode.sensor_tier == musubi_cable_sim::SensorTier::T0
        && coarse_evidence_signature(&episode.features)
            .is_some_and(|signature| ambiguous.contains(&signature))
    {
        Prediction::abstain("indistinguishable_low_fidelity_signature")
    } else {
        model.predict(&episode.features)
    };
    CaseEvaluation::new(
        episode.case_id.clone(),
        episode.oracle_label.clone(),
        episode.oracle_source,
        prediction,
    )
}

fn derive_seed_split(config: &EvaluationConfig) -> SeedSplit {
    let mut state = config.split_seed;
    let mut used = BTreeSet::new();
    let mut next_unique = || loop {
        state = splitmix64(state);
        if used.insert(state) {
            break state;
        }
    };
    let train_seeds = (0..config.train_seed_count)
        .map(|_| next_unique())
        .collect();
    let test_seeds = (0..config.test_seed_count).map(|_| next_unique()).collect();
    SeedSplit {
        split_seed: config.split_seed,
        train_seeds,
        test_seeds,
    }
}

const fn splitmix64(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

fn sha256_bytes(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut encoded = String::with_capacity(71);
    encoded.push_str("sha256:");
    for byte in digest {
        use fmt::Write as _;
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;

    fn small_config() -> EvaluationConfig {
        EvaluationConfig {
            split_seed: 17,
            train_seed_count: 4,
            test_seed_count: 2,
            sensor_tier: musubi_cable_sim::SensorTier::T2,
        }
    }

    #[test]
    fn same_configuration_is_byte_and_digest_deterministic() {
        let config = small_config();
        let first = evaluate_json(&config).expect("first report");
        let second = evaluate_json(&config).expect("second report");
        assert_eq!(first, second);
        let changed = evaluate(&EvaluationConfig {
            split_seed: 18,
            ..config
        })
        .expect("changed split");
        let original = evaluate(&small_config()).expect("original split");
        assert_ne!(original.report_digest, changed.report_digest);
    }

    #[test]
    fn all_methods_share_split_and_report_required_honesty_metrics() {
        let report = evaluate(&small_config()).expect("report");
        assert_eq!(report.payload.model_evaluations.len(), 3);
        assert_eq!(report.payload.seed_split.train_seeds.len(), 4);
        assert_eq!(report.payload.seed_split.test_seeds.len(), 2);
        for evaluation in &report.payload.model_evaluations {
            assert_eq!(evaluation.false_certainty, 0);
            assert!(evaluation.false_certainty_rate.abs() < f64::EPSILON);
            assert!((evaluation.ambiguity_honesty - 1.0).abs() < f64::EPSILON);
            assert!(evaluation.ambiguity_probe_metrics.total > 0);
        }
        assert!(report.payload.guardrail_passed);
        assert!(report.payload.guardrail_failures.is_empty());
        assert_eq!(report.payload.adoption_disposition, "HOLD");
    }

    #[test]
    fn nonzero_false_certainty_cannot_be_sealed_as_a_valid_comparison() {
        let mut report = evaluate(&small_config()).expect("report");
        report.payload.model_evaluations[0].false_certainty = 1;
        report.payload.guardrail_failures = guardrail_failures(&report.payload.model_evaluations);
        report.payload.guardrail_passed = false;
        report.report_digest = sha256_bytes(
            &serde_json::to_vec(&report.payload).expect("tampered payload serialization"),
        );
        assert!(report.validate().is_err());
    }

    #[test]
    fn public_guardrail_fields_cannot_hide_an_incorrect_sealed_case() {
        let mut report = evaluate(&small_config()).expect("report");
        let evaluation = &mut report.payload.model_evaluations[0];
        let case = evaluation
            .primary_cases
            .iter_mut()
            .find(|case| case.outcome == CaseOutcome::Correct)
            .expect("decided fixture case");
        case.model_output.candidate_label = Some("deliberately_wrong_label".to_owned());
        case.outcome = CaseOutcome::Incorrect;
        report.report_digest = sha256_bytes(
            &serde_json::to_vec(&report.payload).expect("tampered payload serialization"),
        );
        assert!(report.validate().is_err());
    }

    #[test]
    fn public_honesty_field_cannot_hide_a_decided_ambiguity_case() {
        let mut report = evaluate(&small_config()).expect("report");
        let evaluation = &mut report.payload.model_evaluations[0];
        let case = evaluation
            .ambiguity_probe_cases
            .first_mut()
            .expect("ambiguity fixture case");
        case.model_output.candidate_label = Some(case.oracle.label.clone());
        case.model_output.abstention_reason = None;
        case.outcome = CaseOutcome::Correct;
        report.report_digest = sha256_bytes(
            &serde_json::to_vec(&report.payload).expect("tampered payload serialization"),
        );
        assert!(report.validate().is_err());
    }

    #[test]
    fn simulated_watermark_and_claim_ceiling_are_validation_invariants() {
        for mutate in ["watermark", "claim_ceiling"] {
            let mut report = evaluate(&small_config()).expect("report");
            if mutate == "watermark" {
                report.payload.watermark = "FIELD_VALIDATED".to_owned();
            } else {
                report.payload.claim_ceiling = "DEPLOYABLE".to_owned();
            }
            report.report_digest = sha256_bytes(
                &serde_json::to_vec(&report.payload).expect("tampered payload serialization"),
            );
            assert!(report.validate().is_err());
        }
    }

    #[test]
    fn observer_fault_and_indistinguishable_cases_abstain() {
        let report = evaluate(&small_config()).expect("report");
        for evaluation in &report.payload.model_evaluations {
            let observer_cases: Vec<_> = evaluation
                .primary_cases
                .iter()
                .filter(|case| case.oracle.label == "observer_fault")
                .collect();
            assert_eq!(observer_cases.len(), 2);
            assert!(observer_cases.iter().all(|case| {
                case.outcome == CaseOutcome::Abstained
                    && case.model_output.abstention_reason.as_deref()
                        == Some("observer_unavailable")
            }));
            assert!(evaluation.ambiguity_probe_cases.iter().all(|case| {
                case.outcome == CaseOutcome::Abstained
                    && case.model_output.abstention_reason.as_deref()
                        == Some("indistinguishable_low_fidelity_signature")
            }));
        }
    }

    #[test]
    fn serialized_cases_keep_oracle_and_model_output_separate() {
        let report = evaluate_json(&small_config()).expect("report");
        let value: serde_json::Value = serde_json::from_str(&report).expect("JSON");
        let case = &value["payload"]["model_evaluations"][0]["primary_cases"][0];
        assert!(case.get("oracle").is_some());
        assert!(case.get("model_output").is_some());
        assert!(case["oracle"].get("candidate_label").is_none());
        assert!(case["model_output"].get("source").is_none());
    }

    #[test]
    fn report_refuses_ml_and_field_claims_and_keeps_holds() {
        let report = evaluate(&small_config()).expect("report");
        assert!(!report.payload.ml_superiority_claimed);
        assert!(!report.payload.field_accuracy_claimed);
        assert!(
            report
                .payload
                .holds
                .iter()
                .map(String::as_str)
                .eq(REQUIRED_HOLDS)
        );
        report.validate().expect("honest report");
    }

    #[test]
    fn redigested_report_cannot_omit_any_required_hold() {
        let mut report = evaluate(&small_config()).expect("report");
        report.payload.holds.remove(0);
        report.report_digest = sha256_bytes(
            &serde_json::to_vec(&report.payload).expect("tampered payload serialization"),
        );
        assert!(report.validate().is_err());
    }
}
