use std::collections::{BTreeMap, BTreeSet};

use musubi_cable_sim::{ScenarioKind, SensorTier, SimulationConfig, generate_ndjson};
use musubi_field_contracts::{
    AnnotationCertaintyV1, AnnotationKindV1, EpisodeAnnotationV1, ObservationExtensionV1,
    ObservationValueV1,
};

use crate::EvaluationError;

pub const FEATURE_IDS: [&str; 8] = [
    "feature.progress_deficit",
    "feature.effort_to_progress",
    "feature.payout_motion_residual",
    "feature.tension_model_residual",
    "feature.force_transmission_ratio",
    "feature.tether_angle_misalignment",
    "feature.continuity_fault",
    "feature.observer_health",
];

pub const MODEL_SCENARIOS: [ScenarioKind; 7] = [
    ScenarioKind::NormalDrag,
    ScenarioKind::PartialSnag,
    ScenarioKind::CompleteRestraintOrLoop,
    ScenarioKind::ReelJam,
    ScenarioKind::LengthLimit,
    ScenarioKind::TetherBreak,
    ScenarioKind::MobilityStall,
];

#[derive(Debug, Clone)]
pub struct FeatureVector {
    pub values: [Option<f64>; 8],
}

impl FeatureVector {
    pub const fn value(&self, index: usize) -> Option<f64> {
        self.values[index]
    }

    pub fn observer_available(&self) -> bool {
        self.values[7].is_some_and(|value| value >= 0.5)
    }
}

#[derive(Debug, Clone)]
pub struct Episode {
    pub case_id: String,
    pub oracle_label: String,
    pub oracle_source: &'static str,
    pub sensor_tier: SensorTier,
    pub features: FeatureVector,
}

pub fn load_episodes(
    seeds: &[u64],
    tier: SensorTier,
    include_observer_fault: bool,
) -> Result<Vec<Episode>, EvaluationError> {
    let scenarios: Vec<_> = if include_observer_fault {
        ScenarioKind::ALL.to_vec()
    } else {
        MODEL_SCENARIOS.to_vec()
    };
    let mut episodes = Vec::with_capacity(seeds.len() * scenarios.len());
    for &seed in seeds {
        for &scenario in &scenarios {
            episodes.push(load_episode(seed, tier, scenario)?);
        }
    }
    Ok(episodes)
}

fn load_episode(
    seed: u64,
    tier: SensorTier,
    scenario: ScenarioKind,
) -> Result<Episode, EvaluationError> {
    let artifact = generate_ndjson(&SimulationConfig::new(scenario, tier, seed))?;
    let mut values_by_feature: [Vec<Option<f64>>; 8] = std::array::from_fn(|_| Vec::new());
    let mut oracle_label = None;

    for line in std::str::from_utf8(&artifact.bytes)
        .map_err(|error| EvaluationError::InvalidData(error.to_string()))?
        .lines()
    {
        let record: serde_json::Value = serde_json::from_str(line)?;
        match record["record_type"].as_str() {
            Some("observation_extension") => {
                let observation: ObservationExtensionV1 =
                    serde_json::from_value(record["record"].clone())?;
                let semantic_id = observation.semantic_id.as_str();
                let Some(index) = FEATURE_IDS
                    .iter()
                    .position(|candidate| *candidate == semantic_id)
                else {
                    continue;
                };
                let value = match observation.value {
                    Some(ObservationValueV1::F64(value)) => Some(value),
                    Some(ObservationValueV1::Bool(value)) => Some(if value { 1.0 } else { 0.0 }),
                    _ => None,
                };
                values_by_feature[index].push(value);
            }
            Some("episode_annotation") => {
                let annotation: EpisodeAnnotationV1 =
                    serde_json::from_value(record["record"].clone())?;
                if annotation.annotation_kind == AnnotationKindV1::GroundTruth
                    && annotation.certainty == AnnotationCertaintyV1::Confirmed
                {
                    oracle_label =
                        Some(annotation.label.trim_start_matches("confirmed_").to_owned());
                }
            }
            _ => {}
        }
    }

    let oracle_label = oracle_label.ok_or_else(|| {
        EvaluationError::InvalidData(format!(
            "fixture has no injected oracle for {}",
            scenario.as_str()
        ))
    })?;
    if oracle_label != scenario.as_str() {
        return Err(EvaluationError::InvalidData(format!(
            "oracle mismatch: fixture={oracle_label}, requested={}",
            scenario.as_str()
        )));
    }
    let values = std::array::from_fn(|index| {
        let tail: Vec<_> = values_by_feature[index]
            .iter()
            .rev()
            .take(4)
            .filter_map(|value| *value)
            .collect();
        if tail.is_empty() {
            None
        } else {
            Some(tail.iter().sum::<f64>() / tail.len() as f64)
        }
    });

    Ok(Episode {
        case_id: format!("{}:{}:{seed:016x}", tier.as_str(), scenario.as_str()),
        oracle_label,
        oracle_source: "deterministic_injection",
        sensor_tier: tier,
        features: FeatureVector { values },
    })
}

pub fn ambiguous_signatures(episodes: &[Episode]) -> BTreeSet<String> {
    let mut labels_by_signature: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for episode in episodes {
        if let Some(signature) = coarse_evidence_signature(&episode.features) {
            labels_by_signature
                .entry(signature)
                .or_default()
                .insert(episode.oracle_label.clone());
        }
    }
    labels_by_signature
        .into_iter()
        .filter_map(|(signature, labels)| (labels.len() > 1).then_some(signature))
        .collect()
}

pub fn coarse_evidence_signature(features: &FeatureVector) -> Option<String> {
    if !features.observer_available() {
        return None;
    }
    let progress = features.value(0)?;
    let effort = features.value(1)?;
    let continuity = features.value(6)? >= 0.5;
    let progress_bin = if progress <= 0.25 {
        "low"
    } else if progress <= 0.75 {
        "mid"
    } else {
        "high"
    };
    let effort_bin = if effort <= 10.0 {
        "low"
    } else if effort <= 80.0 {
        "mid"
    } else {
        "high"
    };
    let direct_tether_missing = features.values[2..6].iter().all(Option::is_none);
    Some(format!(
        "progress={progress_bin}|effort={effort_bin}|continuity={continuity}|direct_missing={direct_tether_missing}"
    ))
}
