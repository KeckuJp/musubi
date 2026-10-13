//! Bounded saved-report mapping into the existing cause outcome/catalog vocabulary.
//! No temporal window, clock promotion, prose parsing, operation target or physical diagnosis.
use musubi_reference_types::{
    CauseOutcome, ChannelId, Family, LogicConfidence, SignatureCatalog, UnknownReason,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{
    FieldValue, Observation, ReadError,
    catalog::parse_catalog,
    cause::{CueSet, match_signatures},
    field,
};

/// Caller assertions tied to one exact saved journal record, not authenticated configuration.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConfigurationComparison {
    pub record_sha256: String,
    pub expected_core_limit_bytes: u64,
    pub observed_core_limit_bytes: u64,
}

/// This catalog is intentionally separate from the existing window-based default catalog.
pub fn catalog() -> Result<SignatureCatalog, ReadError> {
    parse_catalog(include_str!(
        "../../../profiles/declared/storage-reported-errors/cause-signatures.toml"
    ))
}

/// Separate finite declaration-comparison catalog; storage/default catalogs remain unchanged.
pub fn comparison_catalog() -> Result<SignatureCatalog, ReadError> {
    parse_catalog(include_str!(
        "../../../profiles/declared/schema-unit-checks/cause-signatures.toml"
    ))
}

/// Dedicated saved wire framing comparison; never a default timed catalog.
pub fn protocol_catalog() -> Result<SignatureCatalog, ReadError> {
    parse_catalog(include_str!(
        "../../../profiles/declared/protocol-wire-expectation/cause-signatures.toml"
    ))
}

/// Development-reader connection after exact file/profile/expectation binding.
/// The original Observation is unchanged; CRC validation is not authentication.
#[must_use]
pub fn protocol_as_json(o: &Observation, catalog: &SignatureCatalog, expected: u64) -> Value {
    let actual = match field(o, "MAVLINK.wire_version") {
        Some(FieldValue::I64(n @ (1 | 2))) => Some(*n as u64),
        _ => None,
    };
    let qualified = catalog.version == "recorded-protocol-wire-v1"
        && matches!(expected, 1 | 2)
        && o.clock_basis == musubi_reference_types::ClockBasis::Unknown
        && matches!(field(o, "MAVLINK.crc_verified"), Some(FieldValue::I64(1)))
        && actual.is_some();
    let disposition = if !qualified {
        "INSUFFICIENT_UNVERIFIED_CRC"
    } else if actual == Some(expected) {
        "MATCH"
    } else {
        "MISMATCH"
    };
    let report = if disposition == "MISMATCH" {
        let mut cues = CueSet {
            weakest_basis: o.clock_basis,
            time_confidence_min: o.time_confidence,
            evidence: vec![o.digest],
            ..CueSet::default()
        };
        cues.observed_channels.insert(ChannelId::Event);
        cues.present.insert((
            ChannelId::Event,
            "saved_wire_expectation_discrepancy".into(),
        ));
        match_report(
            catalog,
            &cues,
            "validated_saved_framing_differs_from_caller_expectation_neither_side_established_wrong",
        )
    } else {
        unknown(if qualified {
            "matching_wire_no_mismatch_evidence"
        } else {
            "unqualified_crc_or_wire_basis"
        })
    };
    let mut output = report_json(o, catalog, None, report);
    output["context_basis"] =
        json!("exact_input_profile_bound_caller_session_and_expected_wire_not_authenticated");
    output["protocol_comparison"] = json!({
        "expected_wire_version":expected, "actual_wire_version":actual,
        "disposition":disposition, "crc_verified":finite_number(o,"MAVLINK.crc_verified"),
    });
    output
}

/// Separate reported producer-version expectation catalog, never the default catalog.
pub fn producer_version_catalog() -> Result<SignatureCatalog, ReadError> {
    parse_catalog(include_str!(
        "../../../profiles/declared/producer-software-version/cause-signatures.toml"
    ))
}

const PRODUCER_VERSION: &str = "recorded-producer-version-v1";
const PRODUCER_BASIS: &str =
    "REPORTED_PRODUCER_VERSION_VS_CALLER_EXPECTED_SET_NOT_COMPATIBILITY_OR_PHYSICAL_CAUSE";

/// Explicit source-local key uniqueness only; neither identity authentication nor chronology.
pub fn identifier_uniqueness_catalog() -> Result<SignatureCatalog, ReadError> {
    parse_catalog(include_str!(
        "../../../profiles/declared/identifier-uniqueness/cause-signatures.toml"
    ))
}

const UNIQUENESS_VERSION: &str = "recorded-identifier-uniqueness-v1";
const UNIQUENESS_BASIS: &str = "CALLER_SINGLE_NAME_PER_REPORTED_SOURCE_LOCAL_PIN_WITHIN_DECLARED_FILE_WINDOW_NOT_AUTHENTICATION_OR_PHYSICAL_COLLISION";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IdentityWitness {
    source_observation_index: usize,
    bound_observations_sha256: String,
    columns: [String; 2],
    pin: Option<String>,
    name: Option<String>,
    rows: u64,
    bound_rows: u64,
}

fn identifier_uniqueness(
    o: &Observation,
    catalog: &SignatureCatalog,
    context: Option<&ConfigurationComparison>,
) -> RecordedFailure {
    let count = |name| match field(o, name) {
        Some(FieldValue::I64(n)) => usize::try_from(*n).ok(),
        _ => None,
    };
    if context.is_some()
        || o.clock_basis != musubi_reference_types::ClockBasis::Unknown
        || text(o, "check_kind") != Some("IDENTIFIER_UNIQUENESS")
        || text(o, "source_role") != Some("SOURCE_SOFTWARE")
        || text(o, "subject_kind") != Some("PIN")
        || text(o, "attribute_kind") != Some("NAME")
        || text(o, "uniqueness_basis") != Some(UNIQUENESS_BASIS)
        || text(o, "disposition") != Some("CONTRADICTION")
        || count("max_distinct_values") != Some(1)
        || count("missing_pairs") != Some(0)
        || !matches!(field(o, "insufficient_reason"), Some(FieldValue::Blank))
        || [
            "scope_id_hex",
            "namespace_hex",
            "instance_hex",
            "window_id_hex",
            "citation_hex",
        ]
        .iter()
        .any(|name| declared_hex(o, name, 200).is_none())
        || [
            "declaration_sha256",
            "read_report_sha256",
            "artefact_input_sha256",
            "profile_source_sha256",
        ]
        .iter()
        .any(|name| !text(o, name).is_some_and(valid_digest))
    {
        return unknown("missing_or_unqualified_identifier_uniqueness_scope");
    }
    let (Some(subject), Some(sources), Some(names), Some(witnesses)) = (
        declared_hex(o, "subject_key_hex", 256),
        declared_hex(o, "window_sources_hex", 8192)
            .and_then(|s| serde_json::from_str::<Vec<String>>(&s).ok()),
        declared_hex(o, "observed_names_hex", 65536)
            .and_then(|s| serde_json::from_str::<Vec<String>>(&s).ok()),
        declared_hex(o, "witnesses_hex", 65536)
            .filter(|s| s.len() <= 65536)
            .and_then(|s| serde_json::from_str::<Vec<IdentityWitness>>(&s).ok()),
    ) else {
        return unknown("missing_identifier_witnesses_or_declared_subject");
    };
    let source_set: std::collections::BTreeSet<_> = sources.iter().cloned().collect();
    let name_set: std::collections::BTreeSet<_> = names.iter().cloned().collect();
    if sources.is_empty()
        || sources.len() > 64
        || source_set.len() != sources.len()
        || sources.iter().any(|s| !valid_digest(s))
        || count("window_sources_reported") != Some(sources.len())
        || witnesses.len() < 2
        || witnesses.len() > 64
        || count("selected_pairs") != Some(witnesses.len())
        || names.len() < 2
        || names.len() > 64
        || name_set.len() != names.len()
        || count("distinct_values") != Some(names.len())
        || names
            .iter()
            .any(|s| s.is_empty() || s.chars().count() > 256)
    {
        return unknown("inconsistent_identifier_scope_or_witness_counts");
    }
    let mut indices = std::collections::BTreeSet::new();
    let mut tuples = std::collections::BTreeSet::new();
    let mut actual_names = std::collections::BTreeSet::new();
    let mut per_source = std::collections::BTreeMap::new();
    for w in witnesses {
        let (Some(pin), Some(name)) = (w.pin, w.name) else {
            return unknown("identifier_witness_has_missing_component");
        };
        if pin != subject
            || name.is_empty()
            || name.chars().count() > 256
            || w.source_observation_index >= 4096
            || !indices.insert(w.source_observation_index)
            || !source_set.contains(&w.bound_observations_sha256)
            || w.columns[0].is_empty()
            || w.columns[1].is_empty()
            || w.columns[0] == w.columns[1]
            || w.columns.iter().any(|s| s.chars().count() > 83)
            || w.rows == 0
            || w.rows > w.bound_rows
            || w.bound_rows > 200_000
            || !tuples.insert((w.bound_observations_sha256.clone(), name.clone()))
        {
            return unknown("contradictory_or_duplicate_identifier_witness");
        }
        let basis = per_source.entry(w.bound_observations_sha256).or_insert((
            w.columns.clone(),
            w.bound_rows,
            0_u64,
        ));
        // Each accepted count <=200000 and there are <=64 witnesses: sum is bounded.
        basis.2 += w.rows;
        if basis.0 != w.columns || basis.1 != w.bound_rows || basis.2 > basis.1 {
            return unknown("identifier_source_shape_or_row_accounting_conflict");
        }
        actual_names.insert(name);
    }
    if actual_names != name_set {
        return unknown("reported_identifier_name_set_disagrees_with_witnesses");
    }
    let mut cues = CueSet {
        weakest_basis: o.clock_basis,
        time_confidence_min: o.time_confidence,
        evidence: vec![o.digest],
        ..CueSet::default()
    };
    cues.observed_channels.insert(ChannelId::Event);
    cues.present.insert((
        ChannelId::Event,
        "reported_identifier_uniqueness_discrepancy".into(),
    ));
    match_report(
        catalog,
        &cues,
        "reported_key_attribute_conflict_or_history_or_scope_error_not_established_collision",
    )
}

fn producer_version(
    o: &Observation,
    catalog: &SignatureCatalog,
    context: Option<&ConfigurationComparison>,
) -> RecordedFailure {
    if context.is_some()
        || o.clock_basis != musubi_reference_types::ClockBasis::Unknown
        || text(o, "check_kind") != Some("PRODUCER_SOFTWARE_VERSION")
        || text(o, "software_role") != Some("SOURCE_SOFTWARE")
        || text(o, "reported_version_basis")
            != Some("SOURCE_SOFTWARE_NAME_VERSION_PAIRED_ON_ORIGINAL_SAVED_ROWS")
        || text(o, "comparison_basis") != Some(PRODUCER_BASIS)
        || text(o, "identity_evidence") != Some("READ_FROM_THE_OBSERVATION_ROWS")
        || text(o, "disposition") != Some("MISMATCH")
        || !matches!(field(o, "insufficient_reason"), Some(FieldValue::Blank))
        || declared_hex(o, "citation_hex", 200).is_none()
        || [
            "expectation_sha256",
            "read_report_sha256",
            "artefact_input_sha256",
            "profile_source_sha256",
            "bound_observations_sha256",
        ]
        .iter()
        .any(|name| !text(o, name).is_some_and(valid_digest))
        || !matches!(field(o, "source_observation_index"), Some(FieldValue::I64(n)) if (0..4096).contains(n))
        || !matches!(field(o, "identity_rows_carrying_value"), Some(FieldValue::I64(n)) if (1..=200_000).contains(n))
    {
        return unknown("missing_or_unqualified_reported_producer_version");
    }
    let (Some(name), Some(actual), Some(expected_json), Some(join_json), Some(columns)) = (
        declared_hex(o, "software_name_hex", 256),
        declared_hex(o, "actual_declared_hex", 256),
        declared_hex(o, "expected_set_hex", 65536),
        declared_hex(o, "identity_join_hex", 8192),
        declared_hex(o, "identity_source_hex", 256),
    ) else {
        return unknown("missing_producer_version_sides_or_join");
    };
    let expected = serde_json::from_str::<Vec<String>>(&expected_json).ok();
    let join = serde_json::from_str::<std::collections::BTreeMap<String, String>>(&join_json).ok();
    let columns: Vec<_> = columns.split(',').collect();
    let (Some(expected), Some(join)) = (expected, join) else {
        return unknown("invalid_producer_expected_set_or_join");
    };
    let unique: std::collections::BTreeSet<_> = expected.iter().collect();
    if expected.is_empty()
        || expected.len() > 64
        || unique.len() != expected.len()
        || expected
            .iter()
            .any(|s| s.is_empty() || s.chars().count() > 256)
        || expected.contains(&actual)
        || actual.is_empty()
        || name.is_empty()
        || columns.len() != 2
        || columns[0].is_empty()
        || columns[0] == columns[1]
        || join.len() != 2
        || join.get(columns[0]) != Some(&name)
        || join.get(columns[1]) != Some(&actual)
    {
        return unknown("contradictory_producer_version_membership_or_subject");
    }
    // The adopted paired writer emits this exact compact NAME-then-VERSION JSON shape.
    // Reject duplicate keys/alternative escapes rather than accepting serde's last-value winner.
    let canonical_join = format!(
        "{{{}:{},{}:{}}}",
        json!(columns[0]),
        json!(name),
        json!(columns[1]),
        json!(actual)
    );
    if canonical_join != join_json {
        return unknown("noncanonical_producer_version_source_filter");
    }
    let mut cues = CueSet {
        weakest_basis: o.clock_basis,
        time_confidence_min: o.time_confidence,
        evidence: vec![o.digest],
        ..CueSet::default()
    };
    cues.observed_channels.insert(ChannelId::Event);
    cues.present.insert((
        ChannelId::Event,
        "reported_producer_version_discrepancy".into(),
    ));
    match_report(
        catalog,
        &cues,
        "reported_version_disagrees_with_declared_set_neither_side_established_wrong",
    )
}

const COMPARISON_VERSION: &str = "recorded-declared-comparison-v1";
const COMPARISON_BASIS: &str = concat!(
    "A_DISAGREEMENT_BETWEEN_TWO_DECLARATIONS_READ_AS_WRITTEN_IT_IS_NOT_A_STATEMENT_THAT_EITHER_SIDE_",
    "IS_WRONG_NOT_AN_INTEGRATION_CAUSE_NOT_A_PHYSICAL_FINDING_AND_NEVER_A_UNIT_INFERRED_FROM_A_",
    "COLUMN_NAME_OR_FROM_A_VALUE"
);
const INTERPRETED_BASIS: &str = concat!(
    "BOTH_DECLARED_SYMBOLS_ARE_IN_THE_REPOSITORYS_FINITE_SOURCE_QUALIFIED_UNIT_TABLE_SO_THE_",
    "DIFFERENCE_IS_NAMED_AS_A_SCALE_OFFSET_OR_QUANTITY_KIND_DIFFERENCE_FROM_THAT_TABLE_ALONE"
);
const TEXT_ONLY_BASIS: &str = concat!(
    "AT_LEAST_ONE_DECLARED_STRING_IS_NOT_A_SYMBOL_IN_THAT_FINITE_TABLE_SO_THE_TWO_DECLARATIONS_ARE_",
    "COMPARED_AS_TEXT_AND_NEITHER_IS_INTERPRETED"
);

fn declared_hex(o: &Observation, name: &str, limit: usize) -> Option<String> {
    let h = text(o, name)?.strip_prefix("hex:")?;
    if h.is_empty()
        || h.len() > limit * 8
        || h.len() % 2 != 0
        || !h
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return None;
    }
    let bytes: Option<Vec<u8>> = (0..h.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&h[i..i + 2], 16).ok())
        .collect();
    let value = String::from_utf8(bytes?).ok()?;
    (value.chars().count() <= limit).then_some(value)
}

fn finite_number(o: &Observation, name: &str) -> Option<f64> {
    let n = match field(o, name)? {
        FieldValue::I64(n) => *n as f64,
        FieldValue::F64(n) => *n,
        _ => return None,
    };
    n.is_finite().then_some(n)
}

fn comparison(
    o: &Observation,
    catalog: &SignatureCatalog,
    context: Option<&ConfigurationComparison>,
) -> RecordedFailure {
    if context.is_some() {
        return unknown("storage_context_does_not_bind_declaration_comparison");
    }
    if text(o, "comparison_basis") != Some(COMPARISON_BASIS)
        || text(o, "record_time_basis")
            != Some("CALLER_DECLARED_CAPTURE_MICROSECONDS_A_COMPARISON_HAS_NO_CLOCK_OF_ITS_OWN")
        || text(o, "expected_source") != Some("THE_CALLERS_DECLARED_EXPECTATION_DOCUMENT")
        || text(o, "evidence_state") != Some("BOTH_SIDES_DECLARED")
        || declared_hex(o, "check_subject_hex", 256).is_none()
        || [
            "expectation_sha256",
            "read_report_sha256",
            "artefact_input_sha256",
            "profile_source_sha256",
        ]
        .iter()
        .any(|name| !text(o, name).is_some_and(valid_digest))
    {
        return unknown("missing_or_unqualified_comparison_basis_subject_or_document_digest");
    }
    let expected = declared_hex(o, "expected_declared_hex", 1024);
    let actual = declared_hex(o, "actual_declared_hex", 1024);
    let disposition = text(o, "disposition");
    let cue = if text(o, "check_kind") == Some("SCHEMA_COLUMN")
        && text(o, "actual_source")
            == Some("THE_SAVED_READ_REPORTS_SOURCE_COLUMNS_THE_ARTEFACTS_ACTUAL_COLUMNS")
    {
        let valid = match disposition {
            Some("SCHEMA_COLUMN_ABSENT_FROM_ARTEFACT") => {
                expected.as_deref() == Some("DECLARED_EXPECTED_COLUMN")
                    && matches!(field(o, "actual_declared_hex"), Some(FieldValue::Blank))
            }
            Some("SCHEMA_COLUMN_NOT_EXPECTED") => {
                actual.as_deref() == Some("PRESENT_IN_THE_ARTEFACT")
                    && matches!(field(o, "expected_declared_hex"), Some(FieldValue::Blank))
            }
            _ => false,
        };
        if !valid {
            return unknown("no_consistent_declared_schema_discrepancy");
        }
        "declared_schema_discrepancy"
    } else if text(o, "check_kind") == Some("COLUMN_UNIT")
        && text(o, "actual_source")
            == Some("THE_SAVED_READ_REPORTS_PROFILE_UNITS_THE_PROFILES_ACTUAL_DECLARED_UNIT")
    {
        if expected.is_none() || actual.is_none() || expected == actual {
            return unknown("missing_or_equal_declared_unit_sides");
        }
        let valid = if disposition == Some("UNIT_DECLARATION_DIFFERENT") {
            text(o, "unit_interpretation_basis") == Some(TEXT_ONLY_BASIS)
        } else {
            let numbers: Option<Vec<f64>> = [
                "expected_scale",
                "actual_scale",
                "expected_offset",
                "actual_offset",
            ]
            .iter()
            .map(|name| finite_number(o, name))
            .collect();
            let kinds = (
                text(o, "expected_quantity_kind"),
                text(o, "actual_quantity_kind"),
            );
            match (numbers, kinds) {
                (Some(n), (Some(a), Some(b)))
                    if !a.is_empty()
                        && !b.is_empty()
                        && a.len() <= 256
                        && b.len() <= 256
                        && n[0] > 0.0
                        && n[1] > 0.0
                        && text(o, "unit_interpretation_basis") == Some(INTERPRETED_BASIS) =>
                {
                    match disposition {
                        Some("UNIT_SCALE_DIFFERENT") => a == b && (n[0] != n[1] || n[2] != n[3]),
                        Some("UNIT_QUANTITY_KIND_DIFFERENT") => a != b,
                        _ => false,
                    }
                }
                _ => false,
            }
        };
        if !valid {
            return unknown("no_consistent_declared_unit_discrepancy");
        }
        "declared_unit_discrepancy"
    } else {
        return unknown("comparison_kind_or_actual_origin_not_qualified");
    };
    let mut cues = CueSet {
        weakest_basis: o.clock_basis,
        time_confidence_min: o.time_confidence,
        time_untrusted: false,
        evidence: vec![o.digest],
        ..CueSet::default()
    };
    cues.observed_channels.insert(ChannelId::Event);
    cues.present.insert((ChannelId::Event, cue.into()));
    match_report(
        catalog,
        &cues,
        "declared_disagreement_not_which_side_is_wrong_or_a_physical_cause",
    )
}

fn text<'a>(o: &'a Observation, name: &str) -> Option<&'a str> {
    match field(o, name) {
        Some(FieldValue::Text(s)) => Some(s),
        _ => None,
    }
}

fn errno(o: &Observation) -> Option<i64> {
    match field(o, "reported_errno") {
        Some(FieldValue::I64(n)) => Some(*n),
        _ => None,
    }
}

fn valid_digest(s: &str) -> bool {
    s.strip_prefix("sha256:").is_some_and(|h| {
        h.len() == 64
            && h.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

/// Outcome plus eligibility reason. The observation and caller context remain separate evidence.
#[derive(Debug, Clone)]
pub struct RecordedFailure {
    pub outcome: CauseOutcome,
    pub basis: &'static str,
}

fn unknown(basis: &'static str) -> RecordedFailure {
    RecordedFailure {
        outcome: CauseOutcome::Unknown {
            reason: UnknownReason::InsufficientEvidence,
        },
        basis,
    }
}

/// Qualify one actual common Observation, then reuse catalog matching. No clock is consulted.
#[must_use]
pub fn evaluate(
    o: &Observation,
    catalog: &SignatureCatalog,
    context: Option<&ConfigurationComparison>,
) -> RecordedFailure {
    if catalog.version == UNIQUENESS_VERSION {
        return identifier_uniqueness(o, catalog, context);
    }
    if catalog.version == PRODUCER_VERSION {
        return producer_version(o, catalog, context);
    }
    if catalog.version == COMPARISON_VERSION {
        return comparison(o, catalog, context);
    }
    if text(o, "source_pin") != Some("systemd-v257-70bae764")
        || text(o, "errno_abi_pin") != Some("linux-v6.12-adc21867")
        || !text(o, "record_sha256").is_some_and(valid_digest)
    {
        return unknown("unqualified_source_abi_or_record_digest");
    }
    if let Some(ctx) = context
        && text(o, "record_sha256") != Some(ctx.record_sha256.as_str())
    {
        return unknown("caller_context_does_not_bind_this_exact_record");
    }
    if !matches!(
        field(o, "reported_errno"),
        Some(FieldValue::I64(_) | FieldValue::Blank)
    ) {
        return unknown("errno_column_is_missing_or_not_an_exact_integer");
    }
    let mut cues = CueSet {
        weakest_basis: o.clock_basis,
        time_confidence_min: o.time_confidence,
        // This mapping never asserts simultaneity; cap report confidence explicitly below.
        time_untrusted: false,
        evidence: vec![o.digest],
        ..CueSet::default()
    };
    cues.observed_channels.insert(ChannelId::Event);
    let class = text(o, "storage_event_class");
    let basis;
    if let Some(code) = errno(o) {
        if text(o, "storage_event_basis")
            != Some("CLIENT_SUPPLIED_ERRNO_FIELD_AT_DECLARED_KERNEL_ABI_NOT_A_TRUSTED_FIELD")
        {
            return unknown("errno_report_basis_not_qualified");
        }
        let (expected_class, expected_name, cue) = match code {
            28 => (
                "REPORTED_NO_SPACE_LEFT_ON_DEVICE",
                "ENOSPC",
                "reported_no_space",
            ),
            30 => (
                "REPORTED_READ_ONLY_FILE_SYSTEM",
                "EROFS",
                "reported_readonly",
            ),
            5 => {
                return unknown(
                    "io_error_without_operation_or_target_not_database_export_or_corruption_evidence",
                );
            }
            _ => return unknown("unrecognised_errno_retained_without_failure_mapping"),
        };
        if class != Some(expected_class) || text(o, "reported_errno_name") != Some(expected_name) {
            return unknown("contradictory_errno_class_or_name");
        }
        cues.present.insert((ChannelId::Event, cue.into()));
        basis = "client_reported_errno_not_a_verified_operation_or_physical_fault";
    } else if class == Some("REPORTED_TRUNCATED_STORE_BY_CONFIGURED_SIZE_LIMIT") {
        if text(o, "reported_message_id") != Some("5aadd8e954dc4b1a8c954d63fd9e1137")
            || text(o, "storage_event_basis")
                != Some("DOCUMENTED_CATALOG_MESSAGE_ID_AT_THE_PINNED_RELEASE")
        {
            return unknown("configured_limit_catalog_identity_not_qualified");
        }
        let Some(ctx) = context else {
            return unknown("configured_limit_alone_is_not_misconfiguration");
        };
        if ctx.expected_core_limit_bytes == ctx.observed_core_limit_bytes {
            return unknown("caller_context_contradicts_configuration_discrepancy");
        }
        cues.present
            .insert((ChannelId::Event, "reported_configured_limit".into()));
        cues.present
            .insert((ChannelId::Event, "caller_asserted_limit_discrepancy".into()));
        basis = "exact_record_bound_caller_asserted_configuration_discrepancy_not_authenticated";
    } else {
        return unknown("no_qualified_recorded_failure_statement");
    }
    match_report(catalog, &cues, basis)
}

fn match_report(catalog: &SignatureCatalog, cues: &CueSet, basis: &'static str) -> RecordedFailure {
    let mut outcome = match_signatures(catalog, Family::Unknown, cues);
    // Carry the finite catalog's actual discriminators through existing outcome fields.
    if let CauseOutcome::Consistent { candidates } | CauseOutcome::Ambiguous { candidates, .. } =
        &mut outcome
    {
        for c in candidates {
            c.logic_confidence = LogicConfidence::LeadingIndicator;
            if let Some(sig) = catalog
                .signatures
                .iter()
                .find(|s| s.signature_id == c.signature_id)
            {
                c.discriminating_missing
                    .extend(sig.discriminators.iter().cloned());
            }
        }
    }
    if let CauseOutcome::Ambiguous {
        candidates,
        indistinguishable_by,
    } = &mut outcome
    {
        *indistinguishable_by = candidates
            .iter()
            .flat_map(|c| c.discriminating_missing.iter().cloned())
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
    }
    RecordedFailure { outcome, basis }
}

/// Existing development reader output: explicit single-record scope, no invented window or clock.
#[must_use]
pub fn as_json(
    o: &Observation,
    catalog: &SignatureCatalog,
    context: Option<&ConfigurationComparison>,
) -> Value {
    let report = evaluate(o, catalog, context);
    report_json(o, catalog, context, report)
}

// Shared rendering only: a failed read must not construct a pretend Observation.
fn outcome_json(report: &RecordedFailure, catalog: Option<&SignatureCatalog>) -> Value {
    let (label, candidates, missing, unknown_reason) = match &report.outcome {
        CauseOutcome::Consistent { candidates } => {
            ("consistent", candidates.as_slice(), Vec::new(), None)
        }
        CauseOutcome::Ambiguous {
            candidates,
            indistinguishable_by,
        } => (
            "ambiguous",
            candidates.as_slice(),
            indistinguishable_by.clone(),
            None,
        ),
        CauseOutcome::Unknown { reason } => {
            ("unknown", &[][..], Vec::new(), Some(reason.as_label()))
        }
    };
    let digest = |d: &musubi_reference_types::DigestRef| {
        format!(
            "sha256:{}",
            d.0.iter().map(|b| format!("{b:02x}")).collect::<String>()
        )
    };
    json!({
        "outcome":label, "unknown_reason":unknown_reason,
        "basis":report.basis, "indistinguishable_by":missing,
        "candidates":candidates.iter().map(|c| json!({
            "signature_id":c.signature_id, "logic_confidence":c.logic_confidence.as_label(),
            "consistent_with":catalog.and_then(|cat| cat.signatures.iter().find(|s|s.signature_id==c.signature_id)).map(|s|s.consistent_with.as_str()),
            "supporting":c.supporting.iter().map(digest).collect::<Vec<_>>(),
            "contradicting":c.contradicting.iter().map(digest).collect::<Vec<_>>(),
            "matched_cues":c.signature_match.matched_cues, "discriminating_missing":c.discriminating_missing,
        })).collect::<Vec<_>>()
    })
}

/// Finite development telemetry CSV refusal, distinct from a successfully read observation.
#[derive(Debug)]
pub enum SavedReadRefusal {
    Reader(ReadError),
    Io(std::io::Error),
    BoundExceeded { limit: usize },
    ProfileEncoding,
    NotRegular,
    ExpectedBytesMismatch,
}

/// Typed refusal evidence and common abstention; never infers a physical cause from prose.
#[must_use]
pub fn telemetry_refusal_as_json(error: &SavedReadRefusal) -> Value {
    let (failure, explanation) = match error {
        SavedReadRefusal::ExpectedBytesMismatch => (
            json!({"kind":"ExpectedBytesMismatch"}),
            "loaded bytes differ from caller expectation; neither source nor expectation is authenticated".into(),
        ),
        SavedReadRefusal::Reader(ReadError::Malformed { offset, what }) => (
            json!({"kind":"Malformed", "locator":{"value":offset,
                "unit":"source_line_or_zero_unavailable"}}),
            what.clone(),
        ),
        SavedReadRefusal::Reader(ReadError::Profile(message)) => {
            (json!({"kind":"Profile"}), message.clone())
        }
        SavedReadRefusal::Reader(ReadError::UnsupportedFormat { format }) => {
            (json!({"kind":"UnsupportedFormat"}), format.clone())
        }
        SavedReadRefusal::Reader(ReadError::NotImplemented { format, ledger }) => (
            json!({"kind":"NotImplemented"}),
            format!("{format}: {ledger}"),
        ),
        SavedReadRefusal::Io(error) => (
            json!({"kind":"Io", "io_kind":format!("{:?}",error.kind()),
            "raw_os_error":error.raw_os_error()}),
            error.to_string(),
        ),
        SavedReadRefusal::BoundExceeded { limit } => (
            json!({"kind":"BoundExceeded", "limit_bytes":limit}),
            "opt-in saved byte bound exceeded".into(),
        ),
        SavedReadRefusal::ProfileEncoding => (
            json!({"kind":"ProfileEncoding"}),
            "profile is not UTF-8".into(),
        ),
        SavedReadRefusal::NotRegular => (
            json!({"kind":"NotRegular"}),
            "saved regular file required".into(),
        ),
    };
    let mut end = explanation.len().min(4096);
    while !explanation.is_char_boundary(end) {
        end -= 1;
    }
    json!({
        "schema":"musubi.saved-read-refusal.v1", "status":"READ_REFUSED",
        "scope":"one_local_reader_attempt_not_an_observation_or_physical_incident",
        "accepted_observations":0, "input_record_count":Value::Null, "parsed_prefix_records":Value::Null,
        "clock_basis":"Unknown", "time_window":Value::Null, "failure":failure,
        "diagnostic":{"quoted_text_prefix":&explanation[..end],"truncated":end<explanation.len(),
            "original_bytes":explanation.len(),"sha256":format!("sha256:{:x}",Sha256::digest(explanation.as_bytes()))},
        "failure_outcome":outcome_json(&unknown("typed_reader_refusal_not_physical_corruption_or_a_causal_verdict"),None),
        "unresolved_alternatives":["incompatible_or_wrong_target_profile_or_format",
            "malformed_or_incomplete_saved_bytes_if_the_selected_profile_applies",
            "local_access_or_resource_refusal_distinct_from_source_corruption"],
        "alternatives_basis":"possibilities_not_matched_cause_candidates_or_established_failures",
    })
}

fn report_json(
    o: &Observation,
    catalog: &SignatureCatalog,
    context: Option<&ConfigurationComparison>,
    report: RecordedFailure,
) -> Value {
    let mut output = outcome_json(&report, Some(catalog));
    output.as_object_mut().expect("outcome object").extend(json!({
        "scope":"single_saved_record_not_a_timed_incident", "time_window":Value::Null,
        "clock_basis":format!("{:?}",o.clock_basis),
        "record_sha256":text(o,"record_sha256"), "reported_errno":errno(o),
        "evidence_digests":[format!("sha256:{}",o.digest.0.iter().map(|b|format!("{b:02x}")).collect::<String>())],
        "catalog_version":catalog.version,
        "context_basis":if context.is_some() { "caller_asserted_not_authenticated" } else { "not_supplied" },
        "caller_context":context,
    }).as_object().expect("record object").clone());
    if catalog.version == COMPARISON_VERSION {
        output["context_basis"] = json!("expectation_document_declared_not_authenticated");
        output["comparison"] = json!({
            "check_kind":text(o,"check_kind"), "check_subject_hex":text(o,"check_subject_hex"),
            "expected_declared_hex":text(o,"expected_declared_hex"),
            "actual_declared_hex":text(o,"actual_declared_hex"), "disposition":text(o,"disposition"),
            "expectation_sha256":text(o,"expectation_sha256"), "read_report_sha256":text(o,"read_report_sha256"),
            "artefact_input_sha256":text(o,"artefact_input_sha256"), "profile_source_sha256":text(o,"profile_source_sha256")
        });
    }
    if catalog.version == PRODUCER_VERSION {
        output["context_basis"] = json!("producer_expectation_declared_not_authenticated");
        output["comparison"] = json!({
            "software_role":text(o,"software_role"), "software_name_hex":text(o,"software_name_hex"),
            "expected_set_hex":text(o,"expected_set_hex"), "actual_declared_hex":text(o,"actual_declared_hex"),
            "disposition":text(o,"disposition"), "reported_version_basis":text(o,"reported_version_basis"),
            "expectation_sha256":text(o,"expectation_sha256"), "read_report_sha256":text(o,"read_report_sha256"),
            "artefact_input_sha256":text(o,"artefact_input_sha256"), "profile_source_sha256":text(o,"profile_source_sha256"),
            "bound_observations_sha256":text(o,"bound_observations_sha256"),
            "identity_join_hex":text(o,"identity_join_hex"), "identity_source_hex":text(o,"identity_source_hex"),
            "source_observation_index":finite_number(o,"source_observation_index"),
        });
    }
    if catalog.version == UNIQUENESS_VERSION {
        output["context_basis"] =
            json!("uniqueness_scope_and_file_window_caller_declared_not_authenticated");
        output["comparison"] = json!({
            "scope_id_hex":text(o,"scope_id_hex"), "namespace_hex":text(o,"namespace_hex"),
            "instance_hex":text(o,"instance_hex"), "window_id_hex":text(o,"window_id_hex"),
            "subject_key_hex":text(o,"subject_key_hex"), "source_role":text(o,"source_role"),
            "subject_kind":text(o,"subject_kind"), "attribute_kind":text(o,"attribute_kind"),
            "window_sources_hex":text(o,"window_sources_hex"), "witnesses_hex":text(o,"witnesses_hex"),
            "observed_names_hex":text(o,"observed_names_hex"), "disposition":text(o,"disposition"),
            "declaration_sha256":text(o,"declaration_sha256"), "read_report_sha256":text(o,"read_report_sha256"),
            "artefact_input_sha256":text(o,"artefact_input_sha256"), "profile_source_sha256":text(o,"profile_source_sha256")
        });
    }
    output
}
