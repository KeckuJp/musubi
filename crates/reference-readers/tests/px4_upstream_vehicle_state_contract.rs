//! Authored ULog fixtures for the saved upstream PX4 vehicle-state profile at
//! `4842c542b80e7dcc433424984aed98907e054052`: `msg/versioned/VehicleStatus.msg` (MESSAGE_VERSION 1)
//! and `msg/versioned/VehicleAttitude.msg` (MESSAGE_VERSION 0), reproduced field-for-field.
//!
//! Code and specification evidence only: no real flight log, simulator or device. Fixed-wing,
//! multicopter and VTOL-in-transition records differ only in what the producer reports in
//! `vehicle_status`; nothing is inferred from a file name, the profile or the protocol.
use musubi_reference_readers::{
    ChannelId, ClockBasis, FamilyProfile, FieldValue,
    profile::parse_profile,
    ulog::{Px4UlgReader, REPORTED_AIR_BASIS, qualify_reported_air_domain},
};

const MAGIC: [u8; 7] = [0x55, 0x4C, 0x6F, 0x67, 0x01, 0x12, 0x35];
const VEHICLE_STATUS: &[u8] = b"vehicle_status:uint64_t timestamp;uint64_t armed_time;uint64_t takeoff_time;uint8_t arming_state;uint8_t latest_arming_reason;uint8_t latest_disarming_reason;uint64_t nav_state_timestamp;uint8_t nav_state_user_intention;uint8_t nav_state;uint8_t executor_in_charge;uint32_t valid_nav_states_mask;uint32_t can_set_nav_states_mask;uint16_t failure_detector_status;uint8_t hil_state;uint8_t vehicle_type;bool failsafe;bool failsafe_and_user_took_over;uint8_t failsafe_defer_state;bool gcs_connection_lost;uint8_t gcs_connection_lost_counter;bool high_latency_data_link_lost;bool is_vtol;bool is_vtol_tailsitter;bool in_transition_mode;bool in_transition_to_fw;uint8_t system_type;uint8_t system_id;uint8_t component_id;bool safety_button_available;bool safety_off;bool power_input_valid;bool usb_connected;bool open_drone_id_system_present;bool open_drone_id_system_healthy;bool parachute_system_present;bool parachute_system_healthy;bool rc_calibration_in_progress;bool calibration_enabled;bool pre_flight_checks_pass;";
const VEHICLE_ATTITUDE: &[u8] = b"vehicle_attitude:uint64_t timestamp;uint64_t timestamp_sample;float[4] q;float[4] delta_q_reset;uint8_t quat_reset_counter;";
const VEHICLE_STATUS_LEN: usize = 74;
const VEHICLE_ATTITUDE_LEN: usize = 49;

fn profile() -> FamilyProfile {
    parse_profile(
        include_str!("../../../profiles/declared/px4-upstream-vehicle-state/profile.toml"),
        "development",
    )
    .expect("upstream PX4 vehicle-state profile parses")
}

fn message(bytes: &mut Vec<u8>, kind: u8, payload: &[u8]) {
    bytes.extend_from_slice(&(u16::try_from(payload.len()).expect("bounded")).to_le_bytes());
    bytes.push(kind);
    bytes.extend_from_slice(payload);
}

fn body(msg_id: u16, len: usize) -> Vec<u8> {
    let mut out = msg_id.to_le_bytes().to_vec();
    out.extend(std::iter::repeat_n(0_u8, len));
    out
}

fn put(body: &mut [u8], offset: usize, value: &[u8]) {
    body[2 + offset..2 + offset + value.len()].copy_from_slice(value);
}

/// Reported vehicle type and VTOL flags at their pinned offsets.
fn status(timestamp: u64, vehicle_type: u8, vtol: [u8; 4]) -> Vec<u8> {
    let mut out = body(0, VEHICLE_STATUS_LEN);
    put(&mut out, 0, &timestamp.to_le_bytes());
    put(&mut out, 24, &[2]); // arming_state = ARMED
    put(&mut out, 36, &[3]); // nav_state = AUTO_MISSION
    put(&mut out, 49, &[vehicle_type]);
    put(&mut out, 56, &vtol); // is_vtol, is_vtol_tailsitter, in_transition_mode, in_transition_to_fw
    put(&mut out, 60, &[22]); // system_type: raw MAV_TYPE code, no name asserted
    out
}

fn attitude(timestamp: u64, q: [f32; 4]) -> Vec<u8> {
    let mut out = body(1, VEHICLE_ATTITUDE_LEN);
    put(&mut out, 0, &timestamp.to_le_bytes());
    put(&mut out, 8, &(timestamp - 200).to_le_bytes());
    for (i, v) in q.iter().enumerate() {
        put(&mut out, 16 + 4 * i, &v.to_le_bytes());
    }
    put(&mut out, 48, &[3]); // quat_reset_counter
    out
}

fn log(records: &[Vec<u8>]) -> Vec<u8> {
    let mut bytes = MAGIC.to_vec();
    bytes.push(1);
    bytes.extend_from_slice(&0_u64.to_le_bytes());
    message(&mut bytes, b'B', &[0; 40]);
    message(&mut bytes, b'F', VEHICLE_STATUS);
    message(&mut bytes, b'F', VEHICLE_ATTITUDE);
    message(&mut bytes, b'A', b"\0\0\0vehicle_status");
    message(&mut bytes, b'A', b"\0\x01\0vehicle_attitude");
    for record in records {
        message(&mut bytes, b'D', record);
    }
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
fn shared_declarations_stay_identical_to_the_adopted_upstream_profile() {
    let adopted = parse_profile(
        include_str!("../../../profiles/declared/auterion-suite-export/profile.toml"),
        "development",
    )
    .expect("adopted profile parses");
    let ours = profile();
    for (field, unit) in &adopted.field_units {
        assert_eq!(ours.field_units.get(field), Some(unit), "{field} drifted");
    }
    assert_eq!(
        ours.family, adopted.family,
        "no airframe family is declared"
    );
}

#[test]
fn fixed_wing_multicopter_and_vtol_reports_reach_common_output_as_reported() {
    let profile = profile();
    for (field, start) in [
        (
            "vehicle_status.vehicle_type",
            "declared_enum_of_the_current_locomotion_method",
        ),
        (
            "vehicle_status.is_vtol",
            "declared_flag_the_source_states_is_true_if_the_system_is_VTOL",
        ),
        (
            "vehicle_attitude.q[0]",
            "dimensionless_quaternion_w_the_source_states_Hamilton",
        ),
    ] {
        assert!(profile.field_units[field].starts_with(start), "{field}");
    }
    // (vehicle_type, [is_vtol, tailsitter, in_transition, to_fw]) as each endpoint's producer reports it.
    let cases = [
        (2_u8, [0_u8, 0, 0, 0]),
        (1, [0, 0, 0, 0]),
        (1, [1, 0, 1, 1]),
    ];
    for (vehicle_type, vtol) in cases {
        let q = [0.5_f32, -0.5, 0.5, 0.5];
        let report = Px4UlgReader
            .read_report(
                &profile,
                &log(&[
                    status(3_000_000, vehicle_type, vtol),
                    attitude(3_000_100, q),
                ]),
            )
            .expect("authored upstream vehicle state decodes");
        assert_eq!(
            (
                report.data_records,
                report.decoded_data_records,
                report.unsupported_records.len()
            ),
            (2, 2, 0)
        );
        let status_row = &report.observations[0];
        assert_eq!(status_row.channel, ChannelId::Event);
        assert_eq!(status_row.clock_basis, ClockBasis::BootRelative);
        assert_eq!(
            value(&status_row.fields, "vehicle_status.vehicle_type"),
            FieldValue::I64(i64::from(vehicle_type))
        );
        for (index, name) in [
            "is_vtol",
            "is_vtol_tailsitter",
            "in_transition_mode",
            "in_transition_to_fw",
        ]
        .iter()
        .enumerate()
        {
            assert_eq!(
                value(&status_row.fields, &format!("vehicle_status.{name}")),
                FieldValue::I64(i64::from(vtol[index])),
                "{name}"
            );
        }
        assert_eq!(
            value(&status_row.fields, "vehicle_status.system_type"),
            FieldValue::I64(22)
        );
        let attitude_row = &report.observations[1];
        assert_eq!(attitude_row.t_boot_us, Some(3_000_100));
        for (i, expected) in q.iter().enumerate() {
            assert_eq!(
                value(&attitude_row.fields, &format!("vehicle_attitude.q[{i}]")),
                FieldValue::F64(f64::from(*expected))
            );
        }
        assert_eq!(
            value(&attitude_row.fields, "vehicle_attitude.timestamp_sample"),
            FieldValue::I64(2_999_900)
        );
        assert!(
            attitude_row.anchor_unix_us.is_none(),
            "no UTC is declared for these topics"
        );
    }
}

#[test]
fn unknown_vehicle_type_stays_raw_and_truncated_attitude_is_refused() {
    let profile = profile();
    let report = Px4UlgReader
        .read_report(&profile, &log(&[status(1_000, 9, [0; 4])]))
        .expect("an undefined vehicle_type code still decodes");
    assert_eq!(
        value(
            &report.observations[0].fields,
            "vehicle_status.vehicle_type"
        ),
        FieldValue::I64(9)
    );
    let mut short = attitude(2_000, [1.0, 0.0, 0.0, 0.0]);
    short.truncate(short.len() - 4);
    assert!(Px4UlgReader.read_report(&profile, &log(&[short])).is_err());
}

fn domain(
    profile: &FamilyProfile,
    records: &[Vec<u8>],
) -> (musubi_types::PlatformDomain, &'static str) {
    let report = Px4UlgReader
        .read_report(profile, &log(records))
        .expect("authored log decodes");
    let d = qualify_reported_air_domain(profile, &report);
    (d.platform_domain, d.basis)
}

#[test]
fn reported_rotary_fixed_and_vtol_locomotion_qualify_air_only_from_the_session_reports() {
    use musubi_types::PlatformDomain::{Air, Unknown};
    let p = profile();
    // Fixed-wing, multicopter, and a VTOL that reports rotary then fixed wing across a transition.
    for records in [
        vec![status(10, 2, [0; 4])],
        vec![status(10, 1, [0; 4])],
        vec![status(10, 1, [1, 0, 1, 1]), status(20, 2, [1, 0, 0, 0])],
    ] {
        assert_eq!(domain(&p, &records), (Air, REPORTED_AIR_BASIS));
    }
    let report = Px4UlgReader
        .read_report(
            &p,
            &log(&[
                status(10, 0, [0; 4]),
                status(20, 1, [1, 0, 1, 1]),
                status(30, 2, [1, 0, 0, 0]),
            ]),
        )
        .unwrap();
    let d = qualify_reported_air_domain(&p, &report);
    assert_eq!(d.platform_domain, Air);
    assert_eq!(d.status_records, 3);
    assert_eq!(
        d.vehicle_type_counts.into_iter().collect::<Vec<_>>(),
        vec![(0, 1), (1, 1), (2, 1)]
    );
    assert_eq!(
        (d.first_status_t_boot_us, d.last_status_t_boot_us),
        (Some(10), Some(30))
    );
    // VTOL capability and transition remain their own reported fields, not folded into the domain.
    assert_eq!(
        value(
            &report.observations[1].fields,
            "vehicle_status.in_transition_mode"
        ),
        FieldValue::I64(1)
    );

    // Refusals: never Air without a qualifying, consistent report.
    for (records, basis) in [
        (
            vec![attitude(1_000, [1.0, 0.0, 0.0, 0.0])],
            "no_vehicle_status_report_in_this_session",
        ),
        (
            vec![status(10, 0, [0; 4])],
            "only_unspecified_vehicle_type_reported",
        ),
        (
            vec![status(10, 3, [0; 4])],
            "reported_rover_locomotion_is_not_air",
        ),
        (
            vec![status(10, 1, [0; 4]), status(20, 3, [0; 4])],
            "conflicting_rover_and_air_locomotion_reports_in_one_session",
        ),
        (
            vec![status(10, 2, [0; 4]), status(20, 9, [0; 4])],
            "undefined_vehicle_type_code_reported",
        ),
    ] {
        assert_eq!(domain(&p, &records), (Unknown, basis));
    }
    // The same air reports through a profile not qualified for this pin's enum give no Air:
    // the profile name or family alone never qualifies, and the adopted Suite candidate stays as it was.
    let suite = parse_profile(
        include_str!("../../../profiles/declared/auterion-suite-export/profile.toml"),
        "development",
    )
    .unwrap();
    let report = Px4UlgReader
        .read_report(&suite, &log(&[status(10, 2, [0; 4])]))
        .unwrap();
    let d = qualify_reported_air_domain(&suite, &report);
    assert_eq!(d.platform_domain, Unknown);
    assert_eq!(
        d.status_records, 0,
        "nothing is examined for an unqualified profile"
    );
    // The profile's own configured domain is untouched by the reports.
    assert_eq!(p.configured_platform_domain().0, Unknown);
}
