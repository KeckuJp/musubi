#![allow(clippy::expect_used)]

use std::collections::BTreeSet;

use musubi_cable_sim::{
    ScenarioKind, SensorTier, SimulationConfig, generate_ndjson, validate_public_annotation,
};
use musubi_field_contracts::{
    AnnotationCertaintyV1, AnnotationKindV1, AnnotationRegistryOutcomeV1, AnnotationSourceV1,
    CableInteractionProfileV1, DigestV1, EpisodeAnnotationV1, ObservationExtensionV1,
    ObservationRegistryOutcomeV1, RegistryV1, Validate,
};

const REGISTRY_JSON: &[u8] =
    include_bytes!("../../../profiles/public/extensions/cable-field-registry-v1.json");
const REGISTRY_README: &[u8] = include_bytes!("../../../profiles/public/extensions/README.md");
const PROFILE_JSON: &[u8] =
    include_bytes!("../../../profiles/public/cable/cable-interaction-profile-v1.json");
const PROFILE_README: &[u8] = include_bytes!("../../../profiles/public/cable/README.md");

#[test]
fn public_registry_admits_every_emitted_observation_and_fixture_label() {
    let registry: RegistryV1 = serde_json::from_slice(REGISTRY_JSON).expect("public registry JSON");
    registry.validate().expect("public registry contract");
    let expected_provenance = DigestV1::sha256(REGISTRY_README);
    assert!(
        registry
            .entries
            .iter()
            .all(|entry| entry.provenance.digest == expected_provenance)
    );

    let mut emitted_semantics = BTreeSet::new();
    let mut emitted_labels = BTreeSet::new();
    let mut first_observation = None;
    for (index, scenario) in ScenarioKind::ALL.into_iter().enumerate() {
        let config = SimulationConfig::new(scenario, SensorTier::T2, index as u64);
        let artifact = generate_ndjson(&config).expect("synthetic fixture");
        let text = std::str::from_utf8(&artifact.bytes).expect("UTF-8 NDJSON");
        for line in text.lines() {
            let value: serde_json::Value = serde_json::from_str(line).expect("NDJSON record");
            match value["record_type"].as_str() {
                Some("observation_extension") => {
                    let observation: ObservationExtensionV1 =
                        serde_json::from_value(value["record"].clone())
                            .expect("observation contract");
                    assert!(matches!(
                        registry
                            .validate_observation(&observation)
                            .expect("known type and unit"),
                        ObservationRegistryOutcomeV1::KnownValidated { .. }
                    ));
                    emitted_semantics.insert(observation.semantic_id.clone());
                    first_observation.get_or_insert(observation);
                }
                Some("episode_annotation") => {
                    let annotation: EpisodeAnnotationV1 =
                        serde_json::from_value(value["record"].clone())
                            .expect("annotation contract");
                    let label = annotation.label.clone();
                    assert!(matches!(
                        registry
                            .validate_annotation(&annotation)
                            .expect("known label"),
                        AnnotationRegistryOutcomeV1::KnownValidated { .. }
                    ));
                    emitted_labels.insert(label);
                }
                Some("session" | "receipt") => {}
                other => panic!("unexpected record type: {other:?}"),
            }
        }
    }
    assert_eq!(emitted_semantics.len(), 32);
    assert_eq!(emitted_labels.len(), 8);

    let mut unknown = first_observation.expect("at least one observation");
    unknown.semantic_id = "future_sensor.new_primitive".to_owned();
    assert!(matches!(
        registry
            .validate_observation(&unknown)
            .expect("unknown must be preserved"),
        ObservationRegistryOutcomeV1::UnknownPreserved { .. }
    ));

    let mut unknown_label = EpisodeAnnotationV1 {
        schema_version: "1".to_owned(),
        episode_id: "future_label_episode".to_owned(),
        window_start_ns: 0,
        window_end_ns: 0,
        taxonomy_namespace: "kecku.musubi.cable_interaction".to_owned(),
        taxonomy_version: "1.0.0".to_owned(),
        label: "future_client_label".to_owned(),
        annotation_kind: AnnotationKindV1::CauseCandidate,
        certainty: AnnotationCertaintyV1::Unknown,
        label_source: AnnotationSourceV1::OperatorAnnotation,
        annotator_id: "client_annotator".to_owned(),
        evidence: Vec::new(),
    };
    assert!(matches!(
        registry
            .validate_annotation(&unknown_label)
            .expect("unknown label must be preserved"),
        AnnotationRegistryOutcomeV1::UnknownPreserved { .. }
    ));
    unknown_label.label = "suspected_partial_snag".to_owned();
    unknown_label.certainty = AnnotationCertaintyV1::Suspected;
    assert!(matches!(
        registry
            .validate_annotation(&unknown_label)
            .expect("known unconfirmed label"),
        AnnotationRegistryOutcomeV1::KnownValidated { .. }
    ));
    validate_public_annotation(&unknown_label).expect("unconfirmed cause candidate");

    let invalid_confirmed_use = EpisodeAnnotationV1 {
        label: "confirmed_partial_snag".to_owned(),
        ..unknown_label
    };
    let registry_error = registry
        .validate_annotation(&invalid_confirmed_use)
        .expect_err("reserved confirmed label must fail before registry admission");
    assert!(registry_error.to_string().contains("confirmed_* namespace"));
    let error = validate_public_annotation(&invalid_confirmed_use)
        .expect_err("confirmed label cannot be a suspected operator cause candidate");
    assert!(error.to_string().contains("confirmed_* namespace"));
}

#[test]
fn public_cable_profile_is_threshold_free_and_bound_to_its_provenance() {
    let profile: CableInteractionProfileV1 =
        serde_json::from_slice(PROFILE_JSON).expect("public profile JSON");
    profile.validate().expect("public profile contract");
    assert!(profile.thresholds.is_empty());
    let expected_provenance = DigestV1::sha256(PROFILE_README);
    assert!(
        profile
            .calibrations
            .iter()
            .all(|calibration| calibration.calibration_artifact.digest == expected_provenance)
    );
    assert_eq!(
        profile.feature_transformer.configuration_digest,
        expected_provenance
    );
}
