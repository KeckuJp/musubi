//! Authored saved ULog, not a device run. Only the declared pinned AttitudeEstimatorQ writer
//! supplies component meaning; the shared reader neither identifies that writer nor repairs q.
use musubi_reference_readers::{
    ClockBasis, FamilyProfile, FieldValue, Observation, ProfileReader, profile::parse_profile,
    ulog::Px4UlgReader,
};

fn profile() -> FamilyProfile {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../profiles/declared/voxl-px4-ulog/attitude-estimator-q-profile.toml");
    parse_profile(
        &std::fs::read_to_string(path).expect("qualified profile"),
        "development",
    )
    .expect("profile parses")
}

fn message(bytes: &mut Vec<u8>, kind: u8, body: &[u8]) {
    bytes.extend_from_slice(&u16::try_from(body.len()).unwrap().to_le_bytes());
    bytes.push(kind);
    bytes.extend_from_slice(body);
}

fn header() -> Vec<u8> {
    let mut bytes = b"ULog\x01\x12\x35\x01".to_vec();
    bytes.extend_from_slice(&0_u64.to_le_bytes());
    message(&mut bytes, b'B', &[0; 40]);
    message(&mut bytes, b'F', b"vehicle_attitude:uint64_t timestamp;uint64_t timestamp_sample;float[4] q;float[4] delta_q_reset;uint8_t quat_reset_counter;uint8_t[7] _padding0;");
    message(&mut bytes, b'A', b"\0\0\0vehicle_attitude");
    bytes
}

fn body(q: [f32; 4]) -> Vec<u8> {
    let mut bytes = vec![0, 0];
    bytes.extend_from_slice(&1_000_500_u64.to_le_bytes());
    bytes.extend_from_slice(&999_900_u64.to_le_bytes());
    for value in q {
        bytes.extend_from_slice(&value.to_le_bytes());
    }
    bytes.extend_from_slice(&[0; 16]); // reset delta reported as zero, not inferred identity
    bytes.push(0);
    bytes.extend_from_slice(&[0; 7]);
    bytes
}

fn field<'a>(row: &'a Observation, key: &str) -> &'a FieldValue {
    &row.fields.iter().find(|(name, _)| name == key).unwrap().1
}

#[test]
fn declared_hamilton_components_reach_common_without_reordering_or_world_conversion() {
    let profile = profile();
    let mut bytes = header();
    let values = [[0.5_f32, -0.5, 0.5, -0.5], [0.0, 1.0, 0.0, 0.0]];
    for q in values {
        message(&mut bytes, b'D', &body(q));
    }
    let report = Px4UlgReader.read_report(&profile, &bytes).unwrap();
    assert_eq!(
        (
            report.data_records,
            report.decoded_data_records,
            report.unsupported_records.len()
        ),
        (2, 2, 0)
    );
    for (row, q) in report.observations.iter().zip(values) {
        assert_eq!(row.clock_basis, ClockBasis::BootRelative);
        assert_eq!(row.anchor_unix_us, None);
        assert_eq!(row.t_ms, 1000);
        assert_eq!(
            field(row, "vehicle_attitude.timestamp_sample"),
            &FieldValue::I64(999_900)
        );
        assert_eq!(
            field(row, "vehicle_attitude.quat_reset_counter"),
            &FieldValue::I64(0)
        );
        for (index, (value, component)) in q.into_iter().zip(["w", "x", "y", "z"]).enumerate() {
            let key = format!("vehicle_attitude.q[{index}]");
            assert_eq!(field(row, &key), &FieldValue::F64(f64::from(value)));
            let meaning = &profile.field_units[&key];
            assert!(meaning.contains(&format!("Hamilton_{component}_")));
            assert!(meaning.contains("FRD_body_to_NED_earth"));
            assert!(meaning.contains("caller_declared_writer_not_authenticated"));
            assert!(meaning.contains("not_validated_or_normalized_by_reader"));
        }
        assert!(
            !row.fields
                .iter()
                .any(|(key, _)| key.contains("euler") || key.contains("heading"))
        );
    }
    assert_eq!(Px4UlgReader.read(&profile, &bytes).unwrap().len(), 2);
}

#[test]
fn source_values_are_not_repaired_or_silently_defaulted_and_default_stays_unqualified() {
    let profile = profile();
    let mut bytes = header();
    for q in [
        [2.0_f32, 0.0, 0.0, 0.0],
        [0.0; 4],
        [f32::from_bits(0x7fc0_1234), 0.0, 0.0, 0.0],
    ] {
        message(&mut bytes, b'D', &body(q));
    }
    let rows = Px4UlgReader.read(&profile, &bytes).unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(
        field(&rows[0], "vehicle_attitude.q[0]"),
        &FieldValue::F64(2.0)
    );
    for index in 0..4 {
        assert_eq!(
            field(&rows[1], &format!("vehicle_attitude.q[{index}]")),
            &FieldValue::F64(0.0)
        );
    }
    assert_eq!(
        field(&rows[2], "vehicle_attitude.q[0]"),
        &FieldValue::Text("nonfinite_float32:0x7fc01234".into())
    );

    let original = parse_profile(
        include_str!("../../../profiles/declared/voxl-px4-ulog/profile.toml"),
        "development",
    )
    .unwrap();
    assert!(original.field_units["vehicle_attitude.q[0]"].contains("neither_the_component_order"));
    let old_rows = Px4UlgReader.read(&original, &bytes).unwrap();
    for (new, old) in rows.iter().zip(old_rows) {
        assert_eq!(new.fields, old.fields);
        assert_eq!(new.clock_basis, old.clock_basis);
    }
    let mut truncated = header();
    message(&mut truncated, b'D', &body([1.0, 0.0, 0.0, 0.0])[..20]);
    assert!(Px4UlgReader.read(&profile, &truncated).is_err());
    // Unresolved data subscription, not an unknown non-data message kind (which the
    // existing reader ignores). This checks the existing no-silent-drop D accounting.
    message(&mut bytes, b'D', &[99, 0, 1, 2]);
    let report = Px4UlgReader.read_report(&profile, &bytes).unwrap();
    assert_eq!(
        (
            report.data_records,
            report.decoded_data_records,
            report.unsupported_records.len()
        ),
        (4, 3, 1)
    );
    assert!(Px4UlgReader.read(&profile, &bytes).is_err());
}
