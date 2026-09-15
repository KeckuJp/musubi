#![allow(clippy::expect_used, clippy::missing_const_for_fn)]

use std::collections::BTreeMap;

use musubi_field_contracts::*;

fn digest(byte: char) -> DigestV1 {
    DigestV1::new(format!("sha256:{}", byte.to_string().repeat(64))).expect("valid digest")
}

fn artifact(byte: char, role: ArtifactRoleV1) -> ArtifactReferenceV1 {
    ArtifactReferenceV1 {
        digest: digest(byte),
        media_type: "application/octet-stream".to_owned(),
        role,
    }
}

fn quality() -> QualityV1 {
    QualityV1 {
        status: QualityStatusV1::Good,
        score: Some(1.0),
        flags: Vec::new(),
    }
}

fn raw_observation(semantic_id: &str) -> ObservationExtensionV1 {
    ObservationExtensionV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        namespace: "kecku.cable".to_owned(),
        semantic_id: semantic_id.to_owned(),
        semantic_version: "1.0.0".to_owned(),
        event_time_ns: 10,
        clock_basis: ClockBasisV1::BootRelative,
        source: SourceReferenceV1 {
            source_id: "synthetic_sensor_1".to_owned(),
            sensor_location: "reel".to_owned(),
        },
        observation_kind: ObservationKindV1::Raw,
        value_kind: PrimitiveValueKindV1::F64,
        value: Some(ObservationValueV1::F64(12.5)),
        missing: None,
        unit: Some("N".to_owned()),
        resolution: Some(0.1),
        valid_range: Some(NumericRangeV1 {
            minimum: 0.0,
            maximum: 100.0,
        }),
        quality: quality(),
        raw_artifacts: vec![artifact('a', ArtifactRoleV1::RawInput)],
        transformer: None,
    }
}

#[test]
fn digest_requires_full_lowercase_sha256() {
    assert!(DigestV1::new(format!("sha256:{}", "a".repeat(64))).is_ok());
    assert!(DigestV1::new(format!("sha256:{}", "A".repeat(64))).is_err());
    assert!(DigestV1::new(format!("sha256:{}", "a".repeat(63))).is_err());
    assert!(DigestV1::new("sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa").is_err());
}

#[test]
fn observation_enforces_exactly_one_value_and_derivation_rules() {
    let mut observation = raw_observation("tether.tension_n");
    observation.missing = Some(MissingValueV1::NotObserved);
    assert!(observation.validate().is_err());

    observation.value = None;
    assert!(observation.validate().is_ok());

    observation.observation_kind = ObservationKindV1::Derived;
    assert!(observation.validate().is_err());
    observation.transformer = Some(TransformerReferenceV1 {
        transformer_id: "tension_residual".to_owned(),
        transformer_version: "1.0.0".to_owned(),
        configuration_digest: digest('b'),
    });
    assert!(observation.validate().is_ok());

    observation.value = Some(ObservationValueV1::F64(f64::NAN));
    observation.missing = None;
    assert!(observation.validate().is_err());
}

#[test]
fn confirmed_annotation_requires_ground_truth_non_model_evidence() {
    let mut annotation = EpisodeAnnotationV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        episode_id: "episode_1".to_owned(),
        window_start_ns: 10,
        window_end_ns: 20,
        taxonomy_namespace: "kecku.cable.labels".to_owned(),
        taxonomy_version: "1.0.0".to_owned(),
        label: "confirmed_partial_snag".to_owned(),
        annotation_kind: AnnotationKindV1::GroundTruth,
        certainty: AnnotationCertaintyV1::Confirmed,
        label_source: AnnotationSourceV1::PostRunInspection,
        annotator_id: "fixture_oracle".to_owned(),
        evidence: Vec::new(),
    };
    assert!(annotation.validate().is_err());
    annotation.evidence = vec![artifact('c', ArtifactRoleV1::Evidence)];
    assert!(annotation.validate().is_ok());

    annotation.label_source = AnnotationSourceV1::ModelOutput {
        model_id: "baseline_classifier".to_owned(),
        model_version: "1".to_owned(),
    };
    assert!(annotation.validate().is_err());

    annotation.label_source = AnnotationSourceV1::OperatorAnnotation;
    annotation.evidence = vec![artifact('c', ArtifactRoleV1::RawInput)];
    assert!(annotation.validate().is_err());

    annotation.label_source = AnnotationSourceV1::ControlledFixture;
    annotation.evidence = vec![artifact('c', ArtifactRoleV1::Fixture)];
    assert!(annotation.validate().is_ok());
}

#[test]
fn reserved_confirmed_label_cannot_be_used_as_an_unconfirmed_candidate() {
    let annotation = EpisodeAnnotationV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        episode_id: "episode_1".to_owned(),
        window_start_ns: 10,
        window_end_ns: 20,
        taxonomy_namespace: "kecku.cable.labels".to_owned(),
        taxonomy_version: "1.0.0".to_owned(),
        label: "confirmed_partial_snag".to_owned(),
        annotation_kind: AnnotationKindV1::CauseCandidate,
        certainty: AnnotationCertaintyV1::Unknown,
        label_source: AnnotationSourceV1::OperatorAnnotation,
        annotator_id: "operator_1".to_owned(),
        evidence: Vec::new(),
    };
    assert!(annotation.validate().is_err());
}

fn registry_entry(description: &str) -> RegistryEntryV1 {
    RegistryEntryV1 {
        key: RegistryKeyV1 {
            kind: RegistryKindV1::Observation,
            namespace: "kecku.cable".to_owned(),
            semantic_id: "tether.tension_n".to_owned(),
            semantic_version: "1.0.0".to_owned(),
        },
        value_kind: PrimitiveValueKindV1::F64,
        unit: Some("N".to_owned()),
        description: description.to_owned(),
        provenance: artifact('d', ArtifactRoleV1::Fixture),
        deprecated: false,
    }
}

#[test]
fn client_overlay_extends_but_cannot_silently_redefine_public_meaning() {
    let public = RegistryV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        scope: RegistryScopeV1::Public,
        entries: vec![registry_entry("measured tether tension")],
    };
    let mut client_entry = registry_entry("silently changed definition");
    let client = RegistryV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        scope: RegistryScopeV1::ClientOverlay {
            tenant_id: "tenant_1".to_owned(),
            rights_digest: digest('e'),
        },
        entries: vec![client_entry.clone()],
    };
    assert!(matches!(
        RegistryV1::overlay(&public, &client),
        Err(ContractError::SilentRedefinition(_))
    ));

    client_entry.key.semantic_id = "tether.encoder_rate_mps".to_owned();
    let client = RegistryV1 {
        entries: vec![client_entry],
        ..client
    };
    let merged = RegistryV1::overlay(&public, &client).expect("non-colliding overlay");
    assert_eq!(merged.entries.len(), 2);
}

#[test]
fn registry_validates_known_type_and_unit_but_preserves_unknown_semantics() {
    let registry = RegistryV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        scope: RegistryScopeV1::Public,
        entries: vec![registry_entry("measured tether tension")],
    };
    let known = raw_observation("tether.tension_n");
    assert!(matches!(
        registry
            .validate_observation(&known)
            .expect("known observation"),
        ObservationRegistryOutcomeV1::KnownValidated { .. }
    ));

    let unknown = raw_observation("future_sensor.new_field");
    assert!(matches!(
        registry
            .validate_observation(&unknown)
            .expect("unknown is preserved"),
        ObservationRegistryOutcomeV1::UnknownPreserved { .. }
    ));

    let mut wrong_unit = known;
    wrong_unit.unit = Some("m".to_owned());
    assert!(registry.validate_observation(&wrong_unit).is_err());
}

#[test]
fn registry_validates_known_label_but_preserves_unknown_label() {
    let mut entry = registry_entry("confirmed fixture label");
    entry.key.kind = RegistryKindV1::AnnotationLabel;
    entry.key.namespace = "kecku.cable.labels".to_owned();
    entry.key.semantic_id = "confirmed_partial_snag".to_owned();
    entry.value_kind = PrimitiveValueKindV1::Text;
    entry.unit = None;
    let registry = RegistryV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        scope: RegistryScopeV1::Public,
        entries: vec![entry],
    };
    let mut annotation = EpisodeAnnotationV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        episode_id: "episode_1".to_owned(),
        window_start_ns: 10,
        window_end_ns: 20,
        taxonomy_namespace: "kecku.cable.labels".to_owned(),
        taxonomy_version: "1.0.0".to_owned(),
        label: "confirmed_partial_snag".to_owned(),
        annotation_kind: AnnotationKindV1::GroundTruth,
        certainty: AnnotationCertaintyV1::Confirmed,
        label_source: AnnotationSourceV1::ControlledFixture,
        annotator_id: "fixture_oracle".to_owned(),
        evidence: vec![artifact('c', ArtifactRoleV1::Evidence)],
    };
    assert!(matches!(
        registry
            .validate_annotation(&annotation)
            .expect("known label"),
        AnnotationRegistryOutcomeV1::KnownValidated { .. }
    ));

    annotation.label = "future_client_label".to_owned();
    assert!(matches!(
        registry
            .validate_annotation(&annotation)
            .expect("unknown label is preserved"),
        AnnotationRegistryOutcomeV1::UnknownPreserved { .. }
    ));

    let records = vec![RecordV1::EpisodeAnnotation(annotation)];
    assert_eq!(
        decode_ndjson(&encode_ndjson(&records).expect("encode")).expect("decode"),
        records
    );
}

#[test]
fn command_shaped_semantics_are_rejected_without_banning_observed_modes() {
    assert!(lint_identifier("recommended_action").is_err());
    assert!(lint_identifier("vehicle.command.target").is_err());
    assert!(lint_identifier("flight_mode_observed").is_ok());
    assert!(lint_identifier("motor_effort").is_ok());
}

#[test]
fn ndjson_preserves_unknown_semantic_id_without_registry_lookup() {
    let observation = raw_observation("future_sensor.unrecognized_measurement");
    let records = vec![RecordV1::ObservationExtension(observation.clone())];
    let encoded = encode_ndjson(&records).expect("encode");
    let decoded = decode_ndjson(&encoded).expect("decode");
    assert_eq!(decoded, records);
    let RecordV1::ObservationExtension(decoded_observation) = &decoded[0] else {
        panic!("wrong record type");
    };
    assert_eq!(decoded_observation.semantic_id, observation.semantic_id);
}

fn client_boundary() -> DataBoundaryV1 {
    DataBoundaryV1 {
        classification: DataClassificationV1::ClientConfidential,
        tenant_id: Some("tenant_1".to_owned()),
        rights_digest: Some(digest('f')),
        execution_environment: ExecutionEnvironmentV1::ClientLocal,
        external_inference: ExternalInferencePolicyV1::Denied,
        export_policy: ExportPolicyV1::GenericSchemaAggregateAndReceiptOnly,
        raw_logs_residency: ArtifactResidencyV1::ClientLocalOnly,
        labels_residency: ArtifactResidencyV1::ClientLocalOnly,
        model_weights_residency: ArtifactResidencyV1::ClientLocalOnly,
        retention: RetentionPolicyV1 {
            maximum_days: Some(30),
            deletion_authority_id: "client_data_owner".to_owned(),
        },
    }
}

fn required_profile_checks() -> Vec<VerificationCheckIdV1> {
    vec![
        VerificationCheckIdV1::ManifestValid,
        VerificationCheckIdV1::DeclaredBasePinMatch,
        VerificationCheckIdV1::InputDigestMatch,
        VerificationCheckIdV1::OracleDigestMatch,
        VerificationCheckIdV1::CandidateArtifactSetWithinAllowlist,
        VerificationCheckIdV1::GeneratorVerifierDistinct,
        VerificationCheckIdV1::ProfileParse,
        VerificationCheckIdV1::ProfileAgainstSample,
        VerificationCheckIdV1::ProfileScore,
    ]
}

fn profile_manifest() -> IntegrationCaseManifestV1 {
    IntegrationCaseManifestV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        case_id: "case_1".to_owned(),
        case_version: "1.0.0".to_owned(),
        candidate_kind: CandidateKindV1::Profile,
        requirements_commit: "a".repeat(40),
        implementation_commit: "b".repeat(40),
        data_boundary: client_boundary(),
        oracle: OracleReferenceV1 {
            oracle_id: "frozen_profile_oracle".to_owned(),
            semantic_owner_id: "client_semantic_owner".to_owned(),
            oracle_digest: digest('4'),
        },
        acceptance_owner_id: "client_acceptance_owner".to_owned(),
        input_artifacts: vec![artifact('5', ArtifactRoleV1::RawInput)],
        candidate_write_allowlist: vec!["candidate/profile".to_owned()],
        allowed_tools: vec!["profile_generator".to_owned()],
        generator_actor_id: "claude_candidate_generator".to_owned(),
        verifier_actor_id: "codex_independent_verifier".to_owned(),
        required_checks: required_profile_checks(),
        network_policy: NetworkPolicyV1::DenyAll,
        load_policy: CandidateLoadPolicyV1::NextStartAfterHumanSeal,
        claim_ceiling: ClaimCeilingV1::SimSelfAttest,
    }
}

#[test]
fn confidential_case_requires_client_local_rights_and_residency() {
    let mut boundary = client_boundary();
    assert!(boundary.validate().is_ok());
    boundary.rights_digest = None;
    assert!(boundary.validate().is_err());
    boundary = client_boundary();
    boundary.model_weights_residency = ArtifactResidencyV1::NotApplicable;
    assert!(boundary.validate().is_err());
}

#[test]
fn anonymized_client_data_keeps_rights_and_explicit_external_authority() {
    let mut boundary = client_boundary();
    boundary.classification = DataClassificationV1::Anonymized;
    assert!(boundary.validate().is_ok());

    boundary.execution_environment = ExecutionEnvironmentV1::GovernedIsolated;
    boundary.raw_logs_residency = ArtifactResidencyV1::NotApplicable;
    boundary.labels_residency = ArtifactResidencyV1::NotApplicable;
    boundary.model_weights_residency = ArtifactResidencyV1::NotApplicable;
    assert!(boundary.validate().is_err());
    boundary.external_inference = ExternalInferencePolicyV1::ContractAuthorized {
        authorization_digest: digest('7'),
    };
    boundary.export_policy = ExportPolicyV1::GenericSchemaAggregateAndReceiptOnly;
    assert!(boundary.validate().is_ok());
    boundary.rights_digest = None;
    assert!(boundary.validate().is_err());
}

#[test]
fn integration_manifest_pins_sources_paths_actors_and_case_specific_checks() {
    let mut manifest = profile_manifest();
    assert!(manifest.validate().is_ok());

    manifest
        .required_checks
        .retain(|check| *check != VerificationCheckIdV1::ProfileScore);
    assert!(manifest.validate().is_err());
    manifest = profile_manifest();
    manifest.candidate_write_allowlist = vec!["candidate/../oracle".to_owned()];
    assert!(manifest.validate().is_err());
    manifest = profile_manifest();
    manifest.candidate_write_allowlist = vec!["/absolute/path".to_owned()];
    assert!(manifest.validate().is_err());
    manifest = profile_manifest();
    manifest.verifier_actor_id = manifest.generator_actor_id.clone();
    assert!(manifest.validate().is_err());
    manifest = profile_manifest();
    manifest
        .required_checks
        .push(VerificationCheckIdV1::AdapterConformance);
    assert!(manifest.validate().is_err());
    manifest = profile_manifest();
    manifest.allowed_tools.push("profile_generator".to_owned());
    assert!(manifest.validate().is_err());
}

#[test]
fn fixed_contract_json_rejects_unknown_struct_fields() {
    let mut json = serde_json::to_value(profile_manifest()).expect("manifest JSON");
    json.as_object_mut()
        .expect("object")
        .insert("agent_supplied_check".to_owned(), serde_json::json!(true));
    assert!(serde_json::from_value::<IntegrationCaseManifestV1>(json).is_err());
}

#[test]
fn manifest_rejects_unknown_fields_inside_tagged_policies() {
    for pointer in [
        "/data_boundary/external_inference",
        "/data_boundary/export_policy",
        "/network_policy",
    ] {
        let mut json = serde_json::to_value(profile_manifest()).expect("manifest JSON");
        json.pointer_mut(pointer)
            .and_then(serde_json::Value::as_object_mut)
            .expect("tagged policy object")
            .insert("agent_extension".to_owned(), serde_json::json!(true));
        assert!(
            serde_json::from_value::<IntegrationCaseManifestV1>(json).is_err(),
            "unknown nested field was accepted at {pointer}"
        );
    }
}

#[test]
fn data_bearing_tagged_policies_reject_unknown_fields() {
    let mut manifest = profile_manifest();
    manifest.data_boundary.external_inference = ExternalInferencePolicyV1::ContractAuthorized {
        authorization_digest: digest('7'),
    };
    manifest.network_policy = NetworkPolicyV1::Allowlisted {
        hosts: vec!["inference.example".to_owned()],
        authorization_digest: digest('8'),
    };
    let clean = serde_json::to_value(&manifest).expect("manifest JSON");
    serde_json::from_value::<IntegrationCaseManifestV1>(clean)
        .expect("known data-bearing policies round trip");

    for pointer in ["/data_boundary/external_inference", "/network_policy"] {
        let mut json = serde_json::to_value(&manifest).expect("manifest JSON");
        json.pointer_mut(pointer)
            .and_then(serde_json::Value::as_object_mut)
            .expect("tagged policy object")
            .insert("unreviewed_scope".to_owned(), serde_json::json!("wider"));
        assert!(
            serde_json::from_value::<IntegrationCaseManifestV1>(json).is_err(),
            "unknown nested field was accepted at {pointer}"
        );
    }
}

#[test]
fn machine_report_binds_to_frozen_manifest_and_covers_required_checks() {
    let manifest = profile_manifest();
    let mut value = report();
    value.input_manifest_digest = manifest.manifest_digest().expect("manifest digest");
    value.checks = manifest
        .required_checks
        .iter()
        .copied()
        .map(|check_id| VerificationCheckV1 {
            check_id,
            status: CheckStatusV1::Pass,
            detail_code: "PASS".to_owned(),
            evidence: artifact('6', ArtifactRoleV1::Receipt),
        })
        .collect();
    assert!(value.validate_against_manifest(&manifest).is_ok());
    value.checks.pop();
    assert!(value.validate_against_manifest(&manifest).is_err());
}

fn report() -> VerificationReportV1 {
    let checks = required_profile_checks()
        .into_iter()
        .map(|check_id| VerificationCheckV1 {
            check_id,
            status: CheckStatusV1::Pass,
            detail_code: "DETERMINISTIC_FIXTURE_PASSED".to_owned(),
            evidence: artifact('3', ArtifactRoleV1::Receipt),
        })
        .collect();
    VerificationReportV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        case_id: "case_1".to_owned(),
        candidate_kind: CandidateKindV1::Profile,
        generator_actor_id: "claude_candidate_generator".to_owned(),
        verifier_actor_id: "codex_independent_verifier".to_owned(),
        input_manifest_digest: digest('1'),
        candidate_digest: digest('2'),
        checks,
        abstention_reasons: Vec::new(),
        disposition: VerificationDispositionV1::AwaitingHumanSeal,
        hot_reload_applied: false,
        runtime_load_authorized: false,
        claim_ceiling: ClaimCeilingV1::SimSelfAttest,
    }
}

#[test]
fn verification_separates_generator_and_verifier_and_forbids_hot_reload() {
    let mut value = report();
    assert!(value.validate().is_ok());

    value.verifier_actor_id = value.generator_actor_id.clone();
    assert!(value.validate().is_err());
    value = report();
    value.hot_reload_applied = true;
    assert!(value.validate().is_err());
    value = report();
    value.runtime_load_authorized = true;
    assert!(value.validate().is_err());

    let invalid_claim = serde_json::to_string(&report())
        .expect("serialize")
        .replace("SIM_SELF_ATTEST", "PARTNER_ACCEPTED");
    assert!(serde_json::from_str::<VerificationReportV1>(&invalid_claim).is_err());
}

#[test]
fn verification_disposition_must_match_fail_hold_or_all_pass() {
    let mut value = report();
    value.checks[0].status = CheckStatusV1::Hold;
    assert!(value.validate().is_err());
    value.disposition = VerificationDispositionV1::Hold;
    assert!(value.validate().is_ok());
    value.checks[0].status = CheckStatusV1::Fail;
    assert!(value.validate().is_err());
    value.disposition = VerificationDispositionV1::Fail;
    assert!(value.validate().is_ok());
}

#[test]
fn detached_self_attested_human_seal_binds_exact_report_and_candidate() {
    let value = report();
    let mut seal = HumanSealV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        report_digest: value.receipt_digest().expect("report digest"),
        candidate_digest: value.candidate_digest.clone(),
        approver_id: "arai".to_owned(),
        identity_basis: IdentityBasisV1::SelfAttested,
        decision: HumanSealDecisionV1::ApproveForNextStart,
        sealed_at_ns: 123,
        runtime_load_authorized: false,
    };
    assert!(seal.validate_against_report(&value).is_ok());
    seal.candidate_digest = digest('9');
    assert!(seal.validate_against_report(&value).is_err());
    seal.candidate_digest = value.candidate_digest.clone();
    seal.runtime_load_authorized = true;
    assert!(seal.validate_against_report(&value).is_err());

    let mut held = value;
    held.checks[0].status = CheckStatusV1::Hold;
    held.disposition = VerificationDispositionV1::Hold;
    seal.runtime_load_authorized = false;
    seal.report_digest = held.receipt_digest().expect("held report digest");
    seal.candidate_digest = held.candidate_digest.clone();
    assert!(seal.validate_against_report(&held).is_err());
    seal.decision = HumanSealDecisionV1::Reject;
    assert!(seal.validate_against_report(&held).is_ok());
}

#[test]
fn canonical_receipt_digest_is_stable_and_full() {
    let first = report();
    let second = report();
    let first_digest = first.receipt_digest().expect("digest");
    assert_eq!(first_digest, second.receipt_digest().expect("digest"));
    assert_eq!(first_digest.as_str().len(), 71);

    let mut left = BTreeMap::new();
    left.insert("z", 1);
    left.insert("a", 2);
    let mut right = BTreeMap::new();
    right.insert("a", 2);
    right.insert("z", 1);
    assert_eq!(
        canonical_sha256(&left).expect("left digest"),
        canonical_sha256(&right).expect("right digest")
    );
}
