#![allow(clippy::expect_used, clippy::missing_const_for_fn)]

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use musubi_field_contracts::{
    ArtifactReferenceV1, ArtifactRoleV1, ClockBasisV1, DigestV1, MissingValueV1,
    ObservationExtensionV1, ObservationKindV1, ObservationValueV1, PrimitiveValueKindV1,
    QualityFlagV1, QualityStatusV1, QualityV1, RecordV1, RegistryScopeV1, RegistryV1,
    SCHEMA_VERSION_V1, SourceReferenceV1, encode_ndjson,
};

use crate::detached::{load, time_label, value_label};

static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);
const MAX_BYTES: usize = 16 * 1024 * 1024;

struct TestDirectory(PathBuf);

impl TestDirectory {
    fn new() -> Self {
        let root = std::env::temp_dir();
        loop {
            let serial = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let path = root.join(format!(
                "musubi-detached-reader-test-{}-{serial}",
                std::process::id()
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("cannot create synthetic test directory: {error}"),
            }
        }
    }

    fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, bytes).expect("write synthetic input");
        path
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn observation(semantic_id: &str) -> ObservationExtensionV1 {
    ObservationExtensionV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        namespace: "synthetic.future_observations".to_owned(),
        semantic_id: semantic_id.to_owned(),
        semantic_version: "8.2.1".to_owned(),
        event_time_ns: 900_000_123,
        clock_basis: ClockBasisV1::BootRelative,
        source: SourceReferenceV1 {
            source_id: "synthetic_source".to_owned(),
            sensor_location: "synthetic_bench".to_owned(),
        },
        observation_kind: ObservationKindV1::Raw,
        value_kind: PrimitiveValueKindV1::F64,
        value: Some(ObservationValueV1::F64(23.75)),
        missing: None,
        unit: Some("synthetic_unit".to_owned()),
        resolution: None,
        valid_range: None,
        quality: QualityV1 {
            status: QualityStatusV1::Unknown,
            score: None,
            flags: vec![QualityFlagV1::CalibrationUnknown],
        },
        raw_artifacts: vec![ArtifactReferenceV1 {
            digest: DigestV1::sha256(b"synthetic immutable raw input, never device data"),
            media_type: "application/octet-stream".to_owned(),
            role: ArtifactRoleV1::RawInput,
        }],
        transformer: None,
    }
}

fn encoded(observations: &[ObservationExtensionV1]) -> Vec<u8> {
    let records: Vec<_> = observations
        .iter()
        .cloned()
        .map(RecordV1::ObservationExtension)
        .collect();
    encode_ndjson(&records).expect("valid synthetic observation contracts")
}

fn rejected(path: &Path) {
    match load(path) {
        Ok(_) => panic!("invalid input was accepted: {}", path.display()),
        Err(reason) => assert!(!reason.trim().is_empty(), "error must explain rejection"),
    }
}

#[test]
fn detached_unknown_semantic_keys_types_quality_and_artifacts_survive_in_file_order() {
    let directory = TestDirectory::new();
    let first = observation("future_channel.unregistered_scalar");
    let mut second = observation("future_channel.unregistered_text");
    second.event_time_ns = -27;
    second.value_kind = PrimitiveValueKindV1::Text;
    second.value = Some(ObservationValueV1::Text("synthetic sample".to_owned()));
    let expected = vec![first, second];
    let path = directory.write("synthetic.ndjson", &encoded(&expected));

    let result = load(&path).expect("valid unknown semantic keys remain admissible");

    assert_eq!(result.records, expected);
    assert_eq!(result.input, path);
    assert_eq!(result.records[0].quality.status, QualityStatusV1::Unknown);
    assert_eq!(result.records[0].quality.score, None);
    assert_eq!(
        result.records[0].quality.flags,
        vec![QualityFlagV1::CalibrationUnknown]
    );
    assert_eq!(result.records[0].raw_artifacts, expected[0].raw_artifacts);
    assert_eq!(result.records[1].event_time_ns, -27);
}

#[test]
fn detached_digest_covers_original_bytes_including_whitespace_and_line_endings() {
    let directory = TestDirectory::new();
    let canonical = encoded(&[observation("future_channel.exact_bytes")]);
    let mut alternate = b" \t".to_vec();
    alternate.extend_from_slice(&canonical[..canonical.len() - 1]);
    alternate.extend_from_slice(b"  \r\n");
    let first = directory.write("canonical.ndjson", &canonical);
    let second = directory.write("alternate.ndjson", &alternate);

    let canonical_result = load(&first).expect("canonical record");
    let alternate_result = load(&second).expect("equivalent JSON with original whitespace");

    assert_eq!(canonical_result.records, alternate_result.records);
    assert_eq!(
        canonical_result.digest,
        DigestV1::sha256(&canonical).as_str()
    );
    assert_eq!(
        alternate_result.digest,
        DigestV1::sha256(&alternate).as_str()
    );
    assert_ne!(canonical_result.digest, alternate_result.digest);
    assert_eq!(fs::read(second).expect("input remains readable"), alternate);
}

#[test]
fn detached_missing_values_remain_absent_with_the_original_reason() {
    let directory = TestDirectory::new();
    for reason in [
        MissingValueV1::NotObserved,
        MissingValueV1::SensorUnavailable,
        MissingValueV1::ClockUntrusted,
        MissingValueV1::Redacted,
        MissingValueV1::Sentinel {
            encoded_value: "synthetic_sentinel".to_owned(),
        },
        MissingValueV1::Unknown {
            detail: "synthetic unknown reason".to_owned(),
        },
    ] {
        let mut record = observation("future_channel.absent_reading");
        record.value = None;
        record.missing = Some(reason.clone());
        let path = directory.write("missing.ndjson", &encoded(&[record]));
        let loaded = load(&path).expect("explicit missing observation");
        let actual = &loaded.records[0];
        assert_eq!(actual.value, None);
        assert_eq!(actual.missing, Some(reason.clone()));
        let label = value_label(actual);
        assert!(label.contains("ABSENT"), "{label}");
        assert!(label.contains(&format!("{reason:?}")), "{label}");
        assert_ne!(label, "0 synthetic_unit");
    }
}

#[test]
fn detached_values_keep_their_declared_primitive_and_unit() {
    let directory = TestDirectory::new();
    for (value, expected_text) in [
        (ObservationValueV1::Bool(false), "false"),
        (ObservationValueV1::I64(-17), "-17"),
        (ObservationValueV1::U64(23), "23"),
        (ObservationValueV1::F64(23.75), "23.75"),
        (
            ObservationValueV1::Text("synthetic sample".to_owned()),
            "synthetic sample",
        ),
    ] {
        let mut record = observation("future_channel.typed_reading");
        record.value_kind = value.kind();
        record.value = Some(value);
        let path = directory.write("typed.ndjson", &encoded(&[record.clone()]));
        let loaded = load(&path).expect("typed synthetic observation");
        assert_eq!(loaded.records, vec![record]);
        let label = value_label(&loaded.records[0]);
        assert!(label.contains(expected_text), "{label}");
        assert!(label.contains("synthetic_unit"), "{label}");
        assert!(!label.contains("ABSENT"), "{label}");
    }
}

#[test]
fn detached_boot_relative_times_remain_signed_nanoseconds_without_inferred_utc() {
    for event_time_ns in [-987_654_321, 0, 7_123_456_789] {
        let mut record = observation("future_channel.relative_time");
        record.event_time_ns = event_time_ns;
        let label = time_label(&record);
        assert!(label.contains("boot_relative"), "{label}");
        assert!(label.contains(&event_time_ns.to_string()), "{label}");
        assert!(!label.to_ascii_uppercase().contains("UTC"), "{label}");
        assert!(!label.contains("1970-"), "{label}");
    }
}

#[test]
fn detached_requires_an_existing_exact_ndjson_file() {
    let directory = TestDirectory::new();
    let bytes = encoded(&[observation("future_channel.file_selection")]);
    directory.write("valid.ndjson", &bytes);
    rejected(&directory.0);
    rejected(&directory.0.join("absent.ndjson"));
    rejected(&directory.write("wrong.json", &bytes));
    rejected(&directory.write("wrong.ndjson.bak", &bytes));
}

#[test]
fn detached_rejects_empty_malformed_invalid_utf8_and_invalid_contracts() {
    let directory = TestDirectory::new();
    let valid = encoded(&[observation("future_channel.validation")]);
    let mut interior_empty_line = valid.clone();
    interior_empty_line.push(b'\n');
    interior_empty_line.extend_from_slice(&valid);
    let kind_mismatch = String::from_utf8(valid.clone())
        .expect("encoded JSON is UTF-8")
        .replace("\"value_kind\":\"f64\"", "\"value_kind\":\"bool\"");
    let value_and_missing = String::from_utf8(valid)
        .expect("encoded JSON is UTF-8")
        .replace(
            "\"missing\":null",
            "\"missing\":{\"reason\":\"not_observed\"}",
        );

    for bytes in [
        Vec::new(),
        b" \n".to_vec(),
        b"{broken json}\n".to_vec(),
        b"{}\n".to_vec(),
        vec![0xff, 0xfe, b'\n'],
        interior_empty_line,
        kind_mismatch.into_bytes(),
        value_and_missing.into_bytes(),
    ] {
        rejected(&directory.write("invalid.ndjson", &bytes));
    }
}

#[test]
fn detached_rejects_other_contract_record_types_even_in_a_mixed_stream() {
    let directory = TestDirectory::new();
    let registry = RecordV1::Registry(RegistryV1 {
        schema_version: SCHEMA_VERSION_V1.to_owned(),
        scope: RegistryScopeV1::Public,
        entries: Vec::new(),
    });
    let standalone =
        encode_ndjson(std::slice::from_ref(&registry)).expect("valid synthetic registry contract");
    let mixed = encode_ndjson(&[
        RecordV1::ObservationExtension(observation("future_channel.before_other_record")),
        registry,
    ])
    .expect("valid mixed NDJSON contracts");
    rejected(&directory.write("registry.ndjson", &standalone));
    rejected(&directory.write("mixed.ndjson", &mixed));
}

#[test]
fn detached_accepts_exactly_sixteen_mib_and_rejects_one_more_byte() {
    let directory = TestDirectory::new();
    let record = observation("future_channel.bounded_input");
    let mut exact = encoded(&[record.clone()]);
    exact.pop();
    exact.resize(MAX_BYTES, b' ');
    let exact_path = directory.write("exact-limit.ndjson", &exact);
    let accepted = load(&exact_path).expect("the documented inclusive byte limit");
    assert_eq!(accepted.records, vec![record]);
    assert_eq!(accepted.digest, DigestV1::sha256(&exact).as_str());

    exact.push(b' ');
    rejected(&directory.write("over-limit.ndjson", &exact));
}
