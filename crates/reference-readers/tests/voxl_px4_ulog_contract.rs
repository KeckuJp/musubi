use musubi_reference_readers::{
    ClockBasis, FamilyProfile, FieldValue, ProfileReader, profile::parse_profile,
    ulog::Px4UlgReader,
};

const MAGIC_AND_VERSION: [u8; 8] = [0x55, 0x4C, 0x6F, 0x67, 0x01, 0x12, 0x35, 0x01];

const SENSOR_COMBINED: &[u8] = b"sensor_combined:uint64_t timestamp;float[3] gyro_rad;uint32_t gyro_integral_dt;int32_t accelerometer_timestamp_relative;float[3] accelerometer_m_s2;uint32_t accelerometer_integral_dt;uint8_t accelerometer_clipping;uint8_t gyro_clipping;uint8_t accel_calibration_count;uint8_t gyro_calibration_count;";
const VEHICLE_ATTITUDE: &[u8] = b"vehicle_attitude:uint64_t timestamp;uint64_t timestamp_sample;float[4] q;float[4] delta_q_reset;uint8_t quat_reset_counter;uint8_t[7] _padding0;";

const RELATIVE_TIMESTAMP_INVALID: i32 = 2_147_483_647;

fn profile() -> FamilyProfile {
    parse_profile(
        include_str!("fixtures/unknown-adapter--voxl-px4-ulog--profile.toml"),
        "development",
    )
    .expect("private VOXL case profile parses")
}

fn message(bytes: &mut Vec<u8>, kind: u8, payload: &[u8]) {
    bytes
        .extend_from_slice(&(u16::try_from(payload.len()).expect("bounded payload")).to_le_bytes());
    bytes.push(kind);
    bytes.extend_from_slice(payload);
}

fn header() -> Vec<u8> {
    let mut bytes = MAGIC_AND_VERSION.to_vec();
    bytes.extend_from_slice(&0_u64.to_le_bytes());
    let mut out = bytes;
    message(&mut out, b'B', &[0; 40]);
    out
}

fn sensor_combined_record(
    timestamp: u64,
    gyro: [f32; 3],
    accel: [f32; 3],
    accel_relative: i32,
    accel_clipping: u8,
) -> Vec<u8> {
    let mut body = vec![0, 0]; // msg_id 0
    body.extend_from_slice(&timestamp.to_le_bytes());
    for axis in gyro {
        body.extend_from_slice(&axis.to_le_bytes());
    }
    body.extend_from_slice(&1000_u32.to_le_bytes()); // gyro_integral_dt
    body.extend_from_slice(&accel_relative.to_le_bytes());
    for axis in accel {
        body.extend_from_slice(&axis.to_le_bytes());
    }
    body.extend_from_slice(&2000_u32.to_le_bytes()); // accelerometer_integral_dt
    body.push(accel_clipping);
    body.push(0); // gyro_clipping
    body.push(7); // accel_calibration_count
    body.push(9); // gyro_calibration_count
    body
}

fn vehicle_attitude_record(timestamp: u64, sample: u64, q: [f32; 4]) -> Vec<u8> {
    let mut body = vec![1, 0]; // msg_id 1
    body.extend_from_slice(&timestamp.to_le_bytes());
    body.extend_from_slice(&sample.to_le_bytes());
    for component in q {
        body.extend_from_slice(&component.to_le_bytes());
    }
    for _ in 0..4 {
        body.extend_from_slice(&0.0_f32.to_le_bytes()); // delta_q_reset
    }
    body.push(3); // quat_reset_counter
    body.extend_from_slice(&[0; 7]); // _padding0
    body
}

fn both_topics() -> Vec<u8> {
    let mut bytes = header();
    message(&mut bytes, b'F', SENSOR_COMBINED);
    message(&mut bytes, b'F', VEHICLE_ATTITUDE);
    message(&mut bytes, b'A', b"\0\0\0sensor_combined");
    message(&mut bytes, b'A', b"\0\x01\0vehicle_attitude");
    bytes
}

fn value(fields: &[(String, FieldValue)], key: &str) -> FieldValue {
    fields
        .iter()
        .find(|(name, _)| name == key)
        .unwrap_or_else(|| panic!("{key} reached the common observation"))
        .1
        .clone()
}

#[test]
fn selected_voxl_quantities_reach_common_output_with_the_declared_source_semantics() {
    let mut bytes = both_topics();
    let gyro = [0.25_f32, -0.5, 1.25];
    let accel = [0.125_f32, -9.8125, 2.0];
    message(
        &mut bytes,
        b'D',
        &sensor_combined_record(1_000_000, gyro, accel, 250, 0),
    );
    message(
        &mut bytes,
        b'D',
        &vehicle_attitude_record(1_000_500, 1_000_400, [1.0, 0.0, -0.5, 0.25]),
    );

    let profile = profile();
    let report = Px4UlgReader
        .read_report(&profile, &bytes)
        .expect("authored VOXL ULog decodes");

    assert_eq!(
        (
            report.data_records,
            report.decoded_data_records,
            report.unsupported_records.len()
        ),
        (2, 2, 0)
    );
    assert_eq!(report.observations.len(), 2);

    let imu = &report.observations[0].fields;
    for (index, expected) in gyro.iter().enumerate() {
        assert_eq!(
            value(imu, &format!("sensor_combined.gyro_rad[{index}]")),
            FieldValue::F64(f64::from(*expected))
        );
    }
    for (index, expected) in accel.iter().enumerate() {
        assert_eq!(
            value(imu, &format!("sensor_combined.accelerometer_m_s2[{index}]")),
            FieldValue::F64(f64::from(*expected))
        );
    }
    assert_eq!(
        value(imu, "sensor_combined.gyro_integral_dt"),
        FieldValue::I64(1000)
    );
    assert_eq!(
        value(imu, "sensor_combined.accelerometer_integral_dt"),
        FieldValue::I64(2000)
    );
    assert_eq!(
        value(imu, "sensor_combined.gyro_clipping"),
        FieldValue::I64(0)
    );
    assert_eq!(
        value(imu, "sensor_combined.accel_calibration_count"),
        FieldValue::I64(7)
    );

    let attitude = &report.observations[1].fields;
    for (index, expected) in [1.0_f32, 0.0, -0.5, 0.25].iter().enumerate() {
        assert_eq!(
            value(attitude, &format!("vehicle_attitude.q[{index}]")),
            FieldValue::F64(f64::from(*expected))
        );
    }
    assert_eq!(
        value(attitude, "vehicle_attitude.timestamp_sample"),
        FieldValue::I64(1_000_400)
    );
    assert_eq!(
        value(attitude, "vehicle_attitude.quat_reset_counter"),
        FieldValue::I64(3)
    );

    for observation in &report.observations {
        assert_eq!(observation.clock_basis, ClockBasis::BootRelative);
        assert_eq!(observation.anchor_unix_us, None);
    }
    assert_eq!(report.observations[0].t_ms, 1000);

    assert!(
        profile.field_units["sensor_combined.gyro_rad[0]"].starts_with("rad_per_s"),
        "the profile declares what the source states"
    );
    assert!(profile.field_units["vehicle_attitude.q[0]"].contains("neither_the_component_order"));

    assert_eq!(
        Px4UlgReader
            .read(&profile, &bytes)
            .expect("strict read")
            .len(),
        2
    );
}

#[test]
fn declared_invalid_sentinel_is_kept_visible_rather_than_turned_into_a_time() {
    let mut bytes = both_topics();
    message(
        &mut bytes,
        b'D',
        &sensor_combined_record(
            2_000_000,
            [0.0; 3],
            [0.0; 3],
            RELATIVE_TIMESTAMP_INVALID,
            1 | 4,
        ),
    );
    let profile = profile();
    let report = Px4UlgReader
        .read_report(&profile, &bytes)
        .expect("a record carrying the source's invalid sentinel still decodes");

    assert_eq!((report.data_records, report.decoded_data_records), (1, 1));
    let fields = &report.observations[0].fields;
    assert_eq!(
        value(fields, "sensor_combined.accelerometer_timestamp_relative"),
        FieldValue::I64(i64::from(RELATIVE_TIMESTAMP_INVALID))
    );
    assert_eq!(
        value(fields, "sensor_combined.accelerometer_clipping"),
        FieldValue::I64(5)
    );
    assert_eq!(
        value(fields, "sensor_combined.gyro_rad[1]"),
        FieldValue::F64(0.0)
    );
    assert!(
        profile.field_units["sensor_combined.accelerometer_timestamp_relative"]
            .contains("states_no_unit_on_that_line")
    );
}

#[test]
fn container_schema_and_order_failures_are_refused_rather_than_guessed() {
    let profile = profile();
    let good = {
        let mut bytes = both_topics();
        message(
            &mut bytes,
            b'D',
            &sensor_combined_record(3_000_000, [0.0; 3], [0.0; 3], 0, 0),
        );
        bytes
    };
    assert!(Px4UlgReader.read(&profile, &good).is_ok());

    let mut wrong_version = good.clone();
    wrong_version[7] = 2;
    assert!(Px4UlgReader.read(&profile, &wrong_version).is_err());

    let mut decreasing = good.clone();
    message(
        &mut decreasing,
        b'D',
        &sensor_combined_record(2_999_999, [0.0; 3], [0.0; 3], 0, 0),
    );
    assert!(Px4UlgReader.read(&profile, &decreasing).is_err());

    let mut truncated = both_topics();
    let short = sensor_combined_record(3_000_000, [0.0; 3], [0.0; 3], 0, 0);
    message(&mut truncated, b'D', &short[..short.len() - 4]);
    assert!(Px4UlgReader.read(&profile, &truncated).is_err());

    let mut unsupported = good.clone();
    message(&mut unsupported, b'R', &[0, 0]);
    message(
        &mut unsupported,
        b'D',
        &sensor_combined_record(4_000_000, [0.0; 3], [0.0; 3], 0, 0),
    );
    let report = Px4UlgReader
        .read_report(&profile, &unsupported)
        .expect("report still accounts for the file");
    assert_eq!(report.unsupported_records.len(), 1);
    assert_eq!(
        (report.data_records, report.decoded_data_records),
        (2, 1),
        "the skipped record is counted, not hidden"
    );
    assert!(Px4UlgReader.read(&profile, &unsupported).is_err());
}
