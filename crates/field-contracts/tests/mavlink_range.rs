#![allow(clippy::expect_used, clippy::missing_const_for_fn)]

use musubi_field_contracts::*;

fn checksum(bytes: &[u8]) -> u16 {
    let mut crc = 0xffff_u16;
    for byte in bytes {
        crc ^= u16::from(*byte);
        for _ in 0..8 {
            crc = (crc >> 1) ^ if crc & 1 != 0 { 0x8408 } else { 0 };
        }
    }
    crc
}

fn frame(payload: &[u8]) -> Vec<u8> {
    let mut bytes = vec![
        0xfd,
        u8::try_from(payload.len()).expect("fixture length"),
        0,
        0,
        9,
        1,
        42,
        132,
        0,
        0,
    ];
    bytes.extend_from_slice(payload);
    let mut crc_input = bytes[1..].to_vec();
    crc_input.push(85);
    bytes.extend_from_slice(&checksum(&crc_input).to_le_bytes());
    bytes
}

fn payload(signal: Option<u8>) -> Vec<u8> {
    let mut bytes = vec![0; if signal.is_some() { 39 } else { 14 }];
    bytes[0..4].copy_from_slice(&123_456_u32.to_le_bytes());
    bytes[4..6].copy_from_slice(&2_u16.to_le_bytes());
    bytes[6..8].copy_from_slice(&400_u16.to_le_bytes());
    bytes[8..10].copy_from_slice(&37_u16.to_le_bytes());
    bytes[10] = 1;
    bytes[11] = 7;
    bytes[13] = 255;
    if let Some(signal) = signal {
        bytes[38] = signal;
    }
    bytes
}

fn decode(bytes: &[u8]) -> ObservationExtensionV1 {
    decode_mavlink_distance(bytes, "synthetic-rover")
        .expect("valid frame")
        .expect("DISTANCE_SENSOR observation")
}

#[test]
fn hardcoded_crc_oracle_and_base_payload_preserve_raw_distance() {
    assert_eq!(checksum(b"123456789"), 0x6f91);
    let wire = [
        0xfd, 0x0e, 0, 0, 9, 1, 42, 132, 0, 0, 0x40, 0xe2, 1, 0, 2, 0, 0x90, 1, 37, 0, 1, 7, 0,
        255, 0xbc, 0x2f,
    ];
    assert_eq!(frame(&payload(None)), wire);
    let value = decode(&wire);
    value.validate().expect("valid detached contract");
    assert_eq!(value.schema_version, "1");
    assert_eq!(value.namespace, "mavlink");
    assert_eq!(value.semantic_id, "distance_sensor.current_distance");
    assert_eq!(value.semantic_version, "1");
    assert_eq!(value.value_kind, PrimitiveValueKindV1::U64);
    assert_eq!(value.value, Some(ObservationValueV1::U64(37)));
    assert_eq!(value.missing, None);
    assert_eq!(value.unit.as_deref(), Some("cm"));
    assert_eq!(value.resolution, Some(1.0));
    assert_eq!(
        value.valid_range,
        Some(NumericRangeV1 {
            minimum: 2.0,
            maximum: 400.0
        })
    );
    assert_eq!(value.event_time_ns, 123_456_000_000);
    assert_eq!(value.clock_basis, ClockBasisV1::BootRelative);
    assert_eq!(value.observation_kind, ObservationKindV1::Raw);
    assert_eq!(value.transformer, None);
    assert_eq!(value.source.source_id, "synthetic-rover/sys-1/comp-42");
    assert_eq!(
        value.source.sensor_location,
        "rangefinder-7/orientation-0/type-1"
    );
    assert_eq!(value.quality.status, QualityStatusV1::Unknown);
    assert_eq!(value.quality.flags, [QualityFlagV1::CalibrationUnknown]);
    assert_eq!(value.raw_artifacts.len(), 1);
    assert_eq!(value.raw_artifacts[0].digest, DigestV1::sha256(&wire));
    assert_eq!(value.raw_artifacts[0].role, ArtifactRoleV1::RawInput);
}

#[test]
fn extended_quality_never_claims_calibrated_good() {
    for signal in [2, 50, 100] {
        let observation = decode(&frame(&payload(Some(signal))));
        observation.validate().expect("valid contract");
        assert_eq!(observation.quality.status, QualityStatusV1::Suspect);
        assert_eq!(
            observation.quality.flags,
            [QualityFlagV1::CalibrationUnknown]
        );
        assert_eq!(observation.value, Some(ObservationValueV1::U64(37)));
    }
    assert_eq!(
        decode(&frame(&payload(Some(0)))).quality.status,
        QualityStatusV1::Unknown
    );
}

#[test]
fn signal_one_is_absence_not_a_magic_numeric_measurement() {
    let observation = decode(&frame(&payload(Some(1))));
    assert_eq!(observation.value, None);
    assert_eq!(observation.missing, Some(MissingValueV1::SensorUnavailable));
    assert_eq!(observation.quality.status, QualityStatusV1::Bad);
    assert!(
        observation
            .quality
            .flags
            .contains(&QualityFlagV1::CalibrationUnknown)
    );
    observation
        .validate()
        .expect("valid missing-value contract");
}

#[test]
fn mavlink_trailing_zero_truncation_is_padded_without_altering_raw_digest() {
    let mut full = payload(Some(0));
    full[11] = 0;
    full[13] = 0;
    let full_wire = frame(&full);
    while full.last() == Some(&0) && full.len() > 1 {
        full.pop();
    }
    assert_eq!(full.len(), 11);
    let trimmed_wire = frame(&full);
    let mut full_observation = decode(&full_wire);
    let trimmed_observation = decode(&trimmed_wire);
    assert_ne!(
        full_observation.raw_artifacts,
        trimmed_observation.raw_artifacts
    );
    full_observation
        .raw_artifacts
        .clone_from(&trimmed_observation.raw_artifacts);
    assert_eq!(full_observation, trimmed_observation);
}

#[test]
fn out_of_declared_range_is_retained_flagged_and_not_clipped() {
    for distance in [0_u16, 1, 401, u16::MAX] {
        let mut bytes = payload(Some(0));
        bytes[8..10].copy_from_slice(&distance.to_le_bytes());
        let observation = decode(&frame(&bytes));
        assert_eq!(
            observation.value,
            Some(ObservationValueV1::U64(u64::from(distance)))
        );
        assert_eq!(observation.quality.status, QualityStatusV1::Suspect);
        assert_eq!(
            observation.quality.flags,
            [
                QualityFlagV1::CalibrationUnknown,
                QualityFlagV1::OutOfDeclaredRange
            ]
        );
        observation.validate().expect("valid flagged observation");
    }
}

#[test]
fn corrupt_crc_header_or_frame_boundaries_fail_closed() {
    let valid = frame(&payload(None));
    for index in [4, 5, 6, 10, 18, valid.len() - 2, valid.len() - 1] {
        let mut corrupt = valid.clone();
        corrupt[index] ^= 1;
        assert!(
            decode_mavlink_distance(&corrupt, "source").is_err(),
            "CRC-bound byte {index}"
        );
    }
    for length in 0..valid.len() {
        assert!(
            decode_mavlink_distance(&valid[..length], "source").is_err(),
            "truncation {length}"
        );
    }
    for extra in [vec![0], valid.clone()] {
        let mut trailing = valid.clone();
        trailing.extend(extra);
        assert!(decode_mavlink_distance(&trailing, "source").is_err());
    }
    let mut wrong_magic = valid.clone();
    wrong_magic[0] = 0xfe;
    assert!(decode_mavlink_distance(&wrong_magic, "source").is_err());
    for incompat in [1, 2, 128, 255] {
        let mut flagged = valid.clone();
        flagged[2] = incompat;
        assert!(decode_mavlink_distance(&flagged, "source").is_err());
    }
    let mut signed = valid.clone();
    signed[2] = 1;
    signed.extend_from_slice(&[0; 13]);
    assert!(decode_mavlink_distance(&signed, "source").is_err());
    assert!(decode_mavlink_distance(&frame(&[]), "source").is_err());
    assert!(decode_mavlink_distance(&frame(&[0; 40]), "source").is_err());
    assert!(decode_mavlink_distance(&valid, "").is_err());
    assert!(decode_mavlink_distance(&valid, " \t").is_err());
}

#[test]
fn invalid_bounds_and_signal_quality_fail_closed() {
    for (minimum, maximum) in [(0_u16, 0_u16), (401, 400)] {
        let mut bytes = payload(None);
        bytes[4..6].copy_from_slice(&minimum.to_le_bytes());
        bytes[6..8].copy_from_slice(&maximum.to_le_bytes());
        assert!(decode_mavlink_distance(&frame(&bytes), "source").is_err());
    }
    for signal in [101, 255] {
        assert!(decode_mavlink_distance(&frame(&payload(Some(signal))), "source").is_err());
    }
    assert!(decode_mavlink_distance(&frame(&[1]), "source").is_err());
}

#[test]
fn other_messages_are_unmapped_without_claiming_crc_validation() {
    let mut unknown = frame(&payload(None));
    unknown[7] = 33;
    assert_eq!(
        decode_mavlink_distance(&unknown, "source").expect("unmapped"),
        None
    );
    unknown.pop();
    assert!(decode_mavlink_distance(&unknown, "source").is_err());
}

#[test]
fn source_identity_and_exact_artifact_bytes_are_digest_bound() {
    let wire = frame(&payload(None));
    let first = decode(&wire);
    let second = decode_mavlink_distance(&wire, "other-source")
        .expect("decode")
        .expect("observation");
    assert_ne!(first.source, second.source);
    assert_eq!(first.raw_artifacts, second.raw_artifacts);
    assert_ne!(
        canonical_validated_sha256(&first).expect("digest"),
        canonical_validated_sha256(&second).expect("digest")
    );
    let mut payload = payload(None);
    payload[12] = 25;
    let rotated = decode(&frame(&payload));
    assert_eq!(
        rotated.source.sensor_location,
        "rangefinder-7/orientation-25/type-1"
    );
    assert_ne!(first.raw_artifacts, rotated.raw_artifacts);
    let mut max_time = payload;
    max_time[..4].copy_from_slice(&u32::MAX.to_le_bytes());
    assert_eq!(
        decode(&frame(&max_time)).event_time_ns,
        i64::from(u32::MAX) * 1_000_000
    );
}

#[test]
fn detached_ndjson_roundtrip_preserves_unknown_semantics_without_geographic_claims() {
    let observation = decode(&frame(&payload(None)));
    let registry = RegistryV1 {
        schema_version: "1".to_owned(),
        scope: RegistryScopeV1::Public,
        entries: vec![],
    };
    assert!(matches!(
        registry
            .validate_observation(&observation)
            .expect("registry"),
        ObservationRegistryOutcomeV1::UnknownPreserved { .. }
    ));
    let records = vec![RecordV1::ObservationExtension(observation)];
    let bytes = encode_ndjson(&records).expect("NDJSON");
    assert_eq!(decode_ndjson(&bytes).expect("roundtrip"), records);
    let json: serde_json::Value = serde_json::from_slice(&bytes).expect("JSON");
    assert_eq!(json["record_type"], "observation_extension");
    let record = json["record"].as_object().expect("record object");
    for invented_field in [
        "latitude",
        "longitude",
        "position",
        "velocity",
        "platform_state",
        "altitude",
    ] {
        assert!(!record.contains_key(invented_field));
    }
}
