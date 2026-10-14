use crate::data::{Episode, FeatureVector, MODEL_SCENARIOS};

const RAW_FEATURE_COUNT: usize = 8;
const ENCODED_FEATURE_COUNT: usize = RAW_FEATURE_COUNT * 2;

#[derive(Debug, Clone)]
pub struct Prediction {
    pub label: Option<String>,
    pub abstention_reason: Option<&'static str>,
    pub score_margin: Option<f64>,
}

impl Prediction {
    pub const fn abstain(reason: &'static str) -> Self {
        Self {
            label: None,
            abstention_reason: Some(reason),
            score_margin: None,
        }
    }
}

#[derive(Debug, Clone)]
pub enum TrainedModel {
    Deterministic,
    Logistic(LogisticModel),
    BoostedStumps(BoostedStumpModel),
}

impl TrainedModel {
    pub const fn model_id(&self) -> &'static str {
        match self {
            Self::Deterministic => "deterministic_baseline",
            Self::Logistic(_) => "ovr_logistic_regression",
            Self::BoostedStumps(_) => "gradient_boosted_decision_stumps",
        }
    }

    pub const fn model_version() -> &'static str {
        "synthetic_eval_v1"
    }

    pub fn predict(&self, features: &FeatureVector) -> Prediction {
        match self {
            Self::Deterministic => deterministic_prediction(features),
            Self::Logistic(model) => model.predict(features),
            Self::BoostedStumps(model) => model.predict(features),
        }
    }
}

pub fn train_models(episodes: &[Episode]) -> Vec<TrainedModel> {
    let encoder = FeatureEncoder::fit(episodes);
    vec![
        TrainedModel::Deterministic,
        TrainedModel::Logistic(LogisticModel::train(episodes, encoder.clone())),
        TrainedModel::BoostedStumps(BoostedStumpModel::train(episodes, encoder)),
    ]
}

fn classes() -> Vec<String> {
    MODEL_SCENARIOS
        .into_iter()
        .map(|scenario| scenario.as_str().to_owned())
        .collect()
}

fn deterministic_prediction(features: &FeatureVector) -> Prediction {
    let Some(progress) = features.value(0) else {
        return Prediction::abstain("missing_progress_evidence");
    };
    let Some(effort) = features.value(1) else {
        return Prediction::abstain("missing_effort_evidence");
    };
    let Some(continuity_fault) = features.value(6) else {
        return Prediction::abstain("missing_continuity_evidence");
    };
    if continuity_fault >= 0.5 {
        return decided("tether_break");
    }
    let (Some(payout), Some(tension), Some(angle)) =
        (features.value(2), features.value(3), features.value(5))
    else {
        if progress < 0.3 && effort < 10.0 {
            return decided("normal_drag");
        }
        return Prediction::abstain("insufficient_direct_tether_observations");
    };
    if tension < 0.5 && progress > 0.85 && effort > 100.0 {
        decided("mobility_stall")
    } else if angle > 45.0 && tension > 2.0 && progress > 0.85 {
        decided("complete_restraint_or_loop")
    } else if angle > 15.0 && tension > 1.0 && progress > 0.55 {
        decided("partial_snag")
    } else if angle < 10.0 && payout > 0.3 && tension > 1.5 && progress < 0.8 {
        decided("reel_jam")
    } else if angle < 10.0 && tension > 2.2 && progress > 0.8 && effort > 80.0 {
        decided("length_limit")
    } else if progress < 0.35 && tension < 0.5 {
        decided("normal_drag")
    } else {
        Prediction::abstain("deterministic_evidence_not_separable")
    }
}

fn decided(label: &str) -> Prediction {
    Prediction {
        label: Some(label.to_owned()),
        abstention_reason: None,
        // Rule branches have no ranking scale comparable to either learned model.
        score_margin: None,
    }
}

#[derive(Debug, Clone)]
pub struct FeatureEncoder {
    means: [f64; RAW_FEATURE_COUNT],
    scales: [f64; RAW_FEATURE_COUNT],
}

impl FeatureEncoder {
    fn fit(episodes: &[Episode]) -> Self {
        let means = std::array::from_fn(|index| {
            let values: Vec<_> = episodes
                .iter()
                .filter_map(|episode| episode.features.values[index])
                .collect();
            if values.is_empty() {
                0.0
            } else {
                values.iter().sum::<f64>() / values.len() as f64
            }
        });
        let scales = std::array::from_fn(|index| {
            let values: Vec<_> = episodes
                .iter()
                .filter_map(|episode| episode.features.values[index])
                .collect();
            if values.len() < 2 {
                return 1.0;
            }
            let variance = values
                .iter()
                .map(|value| (value - means[index]).powi(2))
                .sum::<f64>()
                / values.len() as f64;
            variance.sqrt().max(1e-9)
        });
        Self { means, scales }
    }

    fn encode(&self, features: &FeatureVector) -> [f64; ENCODED_FEATURE_COUNT] {
        let mut encoded = [0.0; ENCODED_FEATURE_COUNT];
        for index in 0..RAW_FEATURE_COUNT {
            if let Some(value) = features.values[index] {
                encoded[index] = (value - self.means[index]) / self.scales[index];
            } else {
                encoded[index + RAW_FEATURE_COUNT] = 1.0;
            }
        }
        encoded
    }
}

#[derive(Debug, Clone)]
pub struct LogisticModel {
    encoder: FeatureEncoder,
    classes: Vec<String>,
    weights: Vec<Vec<f64>>,
}

impl LogisticModel {
    fn train(episodes: &[Episode], encoder: FeatureEncoder) -> Self {
        let classes = classes();
        let inputs: Vec<_> = episodes
            .iter()
            .map(|episode| encoder.encode(&episode.features))
            .collect();
        let mut weights = Vec::with_capacity(classes.len());
        for class in &classes {
            let mut class_weights = vec![0.0; ENCODED_FEATURE_COUNT + 1];
            for iteration in 0..320 {
                let learning_rate = 0.12 / f64::from(iteration).mul_add(0.003, 1.0);
                let mut gradient = vec![0.0; class_weights.len()];
                for (episode, input) in episodes.iter().zip(&inputs) {
                    let target = f64::from(episode.oracle_label == *class);
                    let score = dot_with_intercept(&class_weights, input);
                    let error = sigmoid(score) - target;
                    gradient[0] += error;
                    for index in 0..ENCODED_FEATURE_COUNT {
                        gradient[index + 1] += error * input[index];
                    }
                }
                let count = episodes.len() as f64;
                class_weights[0] -= learning_rate * gradient[0] / count;
                for index in 1..class_weights.len() {
                    let regularized =
                        0.001_f64.mul_add(class_weights[index], gradient[index] / count);
                    class_weights[index] -= learning_rate * regularized;
                }
            }
            weights.push(class_weights);
        }
        Self {
            encoder,
            classes,
            weights,
        }
    }

    fn predict(&self, features: &FeatureVector) -> Prediction {
        let input = self.encoder.encode(features);
        let scores: Vec<_> = self
            .weights
            .iter()
            .map(|weights| sigmoid(dot_with_intercept(weights, &input)))
            .collect();
        ranked_prediction(&self.classes, &scores, 0.45, 0.08)
    }
}

#[derive(Debug, Clone)]
struct Stump {
    feature_index: usize,
    threshold: f64,
    left_value: f64,
    right_value: f64,
}

#[derive(Debug, Clone)]
struct BinaryStumpEnsemble {
    bias: f64,
    stumps: Vec<Stump>,
}

#[derive(Debug, Clone)]
pub struct BoostedStumpModel {
    encoder: FeatureEncoder,
    classes: Vec<String>,
    ensembles: Vec<BinaryStumpEnsemble>,
}

impl BoostedStumpModel {
    fn train(episodes: &[Episode], encoder: FeatureEncoder) -> Self {
        let classes = classes();
        let inputs: Vec<_> = episodes
            .iter()
            .map(|episode| encoder.encode(&episode.features))
            .collect();
        let ensembles = classes
            .iter()
            .map(|class| train_binary_stumps(episodes, &inputs, class))
            .collect();
        Self {
            encoder,
            classes,
            ensembles,
        }
    }

    fn predict(&self, features: &FeatureVector) -> Prediction {
        let input = self.encoder.encode(features);
        let scores: Vec<_> = self
            .ensembles
            .iter()
            .map(|ensemble| sigmoid(stump_score(ensemble, &input)))
            .collect();
        ranked_prediction(&self.classes, &scores, 0.40, 0.06)
    }
}

fn train_binary_stumps(
    episodes: &[Episode],
    inputs: &[[f64; ENCODED_FEATURE_COUNT]],
    class: &str,
) -> BinaryStumpEnsemble {
    let positive_count = episodes
        .iter()
        .filter(|episode| episode.oracle_label == class)
        .count();
    let prior = (positive_count as f64 / episodes.len() as f64).clamp(1e-6, 1.0 - 1e-6);
    let bias = (prior / (1.0 - prior)).ln();
    let targets: Vec<_> = episodes
        .iter()
        .map(|episode| f64::from(episode.oracle_label == class))
        .collect();
    let mut scores = vec![bias; episodes.len()];
    let mut stumps = Vec::new();

    for _ in 0..24 {
        let residuals: Vec<_> = targets
            .iter()
            .zip(&scores)
            .map(|(target, score)| target - sigmoid(*score))
            .collect();
        let Some(mut stump) = best_stump(inputs, &residuals) else {
            break;
        };
        stump.left_value *= 0.35;
        stump.right_value *= 0.35;
        for (score, input) in scores.iter_mut().zip(inputs) {
            *score += stump_value(&stump, input);
        }
        stumps.push(stump);
    }
    BinaryStumpEnsemble { bias, stumps }
}

fn best_stump(inputs: &[[f64; ENCODED_FEATURE_COUNT]], residuals: &[f64]) -> Option<Stump> {
    let mut best: Option<(f64, Stump)> = None;
    for feature_index in 0..ENCODED_FEATURE_COUNT {
        let mut values: Vec<_> = inputs.iter().map(|input| input[feature_index]).collect();
        values.sort_by(f64::total_cmp);
        values.dedup_by(|left, right| left.total_cmp(right).is_eq());
        if values.len() < 2 {
            continue;
        }
        let thresholds: Vec<_> = (1..10)
            .filter_map(|quantile| {
                let index = quantile * (values.len() - 1) / 10;
                let next = (index + 1).min(values.len() - 1);
                (values[index] < values[next]).then_some(f64::midpoint(values[index], values[next]))
            })
            .collect();
        for threshold in thresholds {
            let mut left_sum = 0.0;
            let mut right_sum = 0.0;
            let mut left_count = 0_usize;
            let mut right_count = 0_usize;
            for (input, residual) in inputs.iter().zip(residuals) {
                if input[feature_index] <= threshold {
                    left_sum += residual;
                    left_count += 1;
                } else {
                    right_sum += residual;
                    right_count += 1;
                }
            }
            if left_count == 0 || right_count == 0 {
                continue;
            }
            let gain =
                left_sum.powi(2) / left_count as f64 + right_sum.powi(2) / right_count as f64;
            if best
                .as_ref()
                .is_none_or(|(best_gain, _)| gain > *best_gain + 1e-15)
            {
                best = Some((
                    gain,
                    Stump {
                        feature_index,
                        threshold,
                        left_value: left_sum / left_count as f64,
                        right_value: right_sum / right_count as f64,
                    },
                ));
            }
        }
    }
    best.map(|(_, stump)| stump)
}

fn stump_score(ensemble: &BinaryStumpEnsemble, input: &[f64; ENCODED_FEATURE_COUNT]) -> f64 {
    ensemble.bias
        + ensemble
            .stumps
            .iter()
            .map(|stump| stump_value(stump, input))
            .sum::<f64>()
}

fn stump_value(stump: &Stump, input: &[f64; ENCODED_FEATURE_COUNT]) -> f64 {
    if input[stump.feature_index] <= stump.threshold {
        stump.left_value
    } else {
        stump.right_value
    }
}

fn ranked_prediction(
    classes: &[String],
    scores: &[f64],
    minimum_top_score: f64,
    minimum_margin: f64,
) -> Prediction {
    let mut ranking: Vec<_> = (0..classes.len()).collect();
    ranking.sort_by(|left, right| {
        scores[*right]
            .total_cmp(&scores[*left])
            .then_with(|| classes[*left].cmp(&classes[*right]))
    });
    let top = ranking[0];
    let runner_up = ranking[1];
    let margin = scores[top] - scores[runner_up];
    if scores[top] < minimum_top_score || margin < minimum_margin {
        Prediction {
            label: None,
            abstention_reason: Some("low_model_separation"),
            score_margin: Some(margin),
        }
    } else {
        Prediction {
            label: Some(classes[top].clone()),
            abstention_reason: None,
            score_margin: Some(margin),
        }
    }
}

fn dot_with_intercept(weights: &[f64], input: &[f64; ENCODED_FEATURE_COUNT]) -> f64 {
    weights[0]
        + weights[1..]
            .iter()
            .zip(input)
            .map(|(weight, value)| weight * value)
            .sum::<f64>()
}

fn sigmoid(value: f64) -> f64 {
    if value >= 0.0 {
        1.0 / (1.0 + (-value).exp())
    } else {
        let exponential = value.exp();
        exponential / (1.0 + exponential)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use musubi_cable_sim::SensorTier;

    fn episode(label: &str, value: f64) -> Episode {
        Episode {
            case_id: format!("case_{label}_{value}"),
            oracle_label: label.to_owned(),
            oracle_source: "unit_fixture",
            sensor_tier: SensorTier::T2,
            features: FeatureVector {
                values: [
                    Some(value),
                    Some(value * 10.0),
                    Some(value),
                    Some(value),
                    Some(value),
                    Some(value),
                    Some(0.0),
                    Some(1.0),
                ],
            },
        }
    }

    #[test]
    fn sigmoid_is_finite_monotonic_and_centered() {
        let low = sigmoid(-100.0);
        let center = sigmoid(0.0);
        let high = sigmoid(100.0);
        assert!(low.is_finite() && center.is_finite() && high.is_finite());
        assert!(low < center && center < high);
        assert!((center - 0.5).abs() < f64::EPSILON);
    }

    #[test]
    fn ranked_prediction_uses_both_score_and_margin_abstention() {
        let labels = vec!["a".to_owned(), "b".to_owned(), "c".to_owned()];
        let decided = ranked_prediction(&labels, &[0.9, 0.6, 0.1], 0.4, 0.1);
        assert_eq!(decided.label.as_deref(), Some("a"));
        assert!(decided.abstention_reason.is_none());

        let low_score = ranked_prediction(&labels, &[0.3, 0.1, 0.0], 0.4, 0.1);
        assert_eq!(low_score.abstention_reason, Some("low_model_separation"));
        let low_margin = ranked_prediction(&labels, &[0.9, 0.85, 0.1], 0.4, 0.1);
        assert_eq!(low_margin.abstention_reason, Some("low_model_separation"));
    }

    #[test]
    fn stump_search_finds_a_repeatable_separating_split() {
        let mut low = [0.0; ENCODED_FEATURE_COUNT];
        let mut high = [0.0; ENCODED_FEATURE_COUNT];
        low[0] = -1.0;
        high[0] = 1.0;
        let stump = best_stump(&[low, high], &[-1.0, 1.0]).expect("separating stump");
        assert_eq!(stump.feature_index, 0);
        assert!(stump.left_value < 0.0);
        assert!(stump.right_value > 0.0);
        assert!(stump_value(&stump, &low) < stump_value(&stump, &high));
    }

    #[test]
    fn learned_methods_train_deterministically_and_emit_finite_margins() {
        let episodes: Vec<_> = MODEL_SCENARIOS
            .iter()
            .enumerate()
            .flat_map(|(index, scenario)| {
                let base = index as f64 + 1.0;
                [
                    episode(scenario.as_str(), base),
                    episode(scenario.as_str(), base + 0.05),
                ]
            })
            .collect();
        let models = train_models(&episodes);
        assert_eq!(models.len(), 3);
        for model in models.iter().skip(1) {
            let first = model.predict(&episodes[0].features);
            let second = model.predict(&episodes[0].features);
            assert_eq!(first.label, second.label);
            assert_eq!(first.abstention_reason, second.abstention_reason);
            assert_eq!(first.score_margin, second.score_margin);
            assert!(first.score_margin.is_some_and(f64::is_finite));
        }
    }
}
