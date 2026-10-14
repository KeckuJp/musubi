use serde::Serialize;

use crate::models::Prediction;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OracleReference {
    pub label: String,
    pub source: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModelOutput {
    pub candidate_label: Option<String>,
    pub abstention_reason: Option<String>,
    /// Ranking separation only. It is not a calibrated probability or authority score.
    pub score_margin: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CaseOutcome {
    Correct,
    Incorrect,
    Abstained,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CaseEvaluation {
    pub case_id: String,
    pub oracle: OracleReference,
    pub model_output: ModelOutput,
    pub outcome: CaseOutcome,
}

impl CaseEvaluation {
    pub fn new(
        case_id: String,
        oracle_label: String,
        oracle_source: &str,
        prediction: Prediction,
    ) -> Self {
        let outcome = match &prediction.label {
            Some(label) if *label == oracle_label => CaseOutcome::Correct,
            Some(_) => CaseOutcome::Incorrect,
            None => CaseOutcome::Abstained,
        };
        Self {
            case_id,
            oracle: OracleReference {
                label: oracle_label,
                source: oracle_source.to_owned(),
            },
            model_output: ModelOutput {
                candidate_label: prediction.label,
                abstention_reason: prediction.abstention_reason.map(str::to_owned),
                score_margin: prediction.score_margin,
            },
            outcome,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MetricSummary {
    pub total: usize,
    pub correct: usize,
    pub incorrect: usize,
    pub abstained: usize,
    pub coverage: f64,
    pub accuracy_on_decided: Option<f64>,
}

impl MetricSummary {
    #[must_use]
    pub fn from_cases(cases: &[CaseEvaluation]) -> Self {
        let correct = cases
            .iter()
            .filter(|case| case.outcome == CaseOutcome::Correct)
            .count();
        let incorrect = cases
            .iter()
            .filter(|case| case.outcome == CaseOutcome::Incorrect)
            .count();
        let abstained = cases.len() - correct - incorrect;
        let decided = correct + incorrect;
        Self {
            total: cases.len(),
            correct,
            incorrect,
            abstained,
            coverage: ratio(decided, cases.len()),
            accuracy_on_decided: (decided > 0).then(|| ratio(correct, decided)),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModelEvaluation {
    pub model_id: String,
    pub model_version: String,
    pub primary_metrics: MetricSummary,
    pub ambiguity_probe_metrics: MetricSummary,
    /// Incorrect, non-abstained primary decisions.
    pub false_certainty: usize,
    pub false_certainty_rate: f64,
    /// Fraction of pre-declared indistinguishable low-fidelity cases that were abstained.
    pub ambiguity_honesty: f64,
    pub primary_cases: Vec<CaseEvaluation>,
    pub ambiguity_probe_cases: Vec<CaseEvaluation>,
}

pub fn ratio(numerator: usize, denominator: usize) -> f64 {
    if denominator == 0 {
        0.0
    } else {
        numerator as f64 / denominator as f64
    }
}
