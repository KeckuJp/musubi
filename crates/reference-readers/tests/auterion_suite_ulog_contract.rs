use musubi_reference_readers::{
    ChannelId, ClockBasis, FamilyProfile, FieldValue, profile::parse_profile, ulog::Px4UlgReader,
};

const MAGIC: [u8; 7] = [0x55, 0x4C, 0x6F, 0x67, 0x01, 0x12, 0x35];

const VEHICLE_LOCAL_POSITION: &[u8] = b"vehicle_local_position:uint64_t timestamp;uint64_t timestamp_sample;bool xy_valid;bool z_valid;bool v_xy_valid;bool v_z_valid;float x;float y;float z;float[2] delta_xy;uint8_t xy_reset_counter;float delta_z;uint8_t z_reset_counter;float vx;float vy;float vz;float z_deriv;float[2] delta_vxy;uint8_t vxy_reset_counter;float delta_vz;uint8_t vz_reset_counter;float ax;float ay;float az;float heading;float heading_var;float unaided_heading;float delta_heading;uint8_t heading_reset_counter;bool heading_good_for_control;float tilt_var;bool xy_global;bool z_global;uint64_t ref_timestamp;double ref_lat;double ref_lon;float ref_alt;bool dist_bottom_valid;float dist_bottom;float dist_bottom_var;float delta_dist_bottom;uint8_t dist_bottom_reset_counter;uint8_t dist_bottom_sensor_bitfield;float eph;float epv;float evh;float evv;bool dead_reckoning;float vxy_max;float vz_max;float hagl_min;float hagl_max_z;float hagl_max_xy;";
const SENSOR_GPS: &[u8] = b"sensor_gps:uint64_t timestamp;uint64_t timestamp_sample;uint32_t device_id;double latitude_deg;double longitude_deg;double altitude_msl_m;double altitude_ellipsoid_m;float s_variance_m_s;float c_variance_rad;uint8_t fix_type;float eph;float epv;float hdop;float vdop;int32_t noise_per_ms;uint16_t automatic_gain_control;uint8_t jamming_state;int32_t jamming_indicator;uint8_t spoofing_state;uint8_t authentication_state;float vel_m_s;float vel_n_m_s;float vel_e_m_s;float vel_d_m_s;float cog_rad;bool vel_ned_valid;int32_t timestamp_time_relative;uint64_t time_utc_usec;uint8_t satellites_used;uint32_t system_error;float heading;float heading_offset;float heading_accuracy;float rtcm_injection_rate;uint8_t selected_rtcm_instance;bool rtcm_crc_failed;uint8_t rtcm_msg_used;";
const VEHICLE_STATUS: &[u8] = b"vehicle_status:uint64_t timestamp;uint64_t armed_time;uint64_t takeoff_time;uint8_t arming_state;uint8_t latest_arming_reason;uint8_t latest_disarming_reason;uint64_t nav_state_timestamp;uint8_t nav_state_user_intention;uint8_t nav_state;uint8_t executor_in_charge;uint32_t valid_nav_states_mask;uint32_t can_set_nav_states_mask;uint16_t failure_detector_status;uint8_t hil_state;uint8_t vehicle_type;bool failsafe;bool failsafe_and_user_took_over;uint8_t failsafe_defer_state;bool gcs_connection_lost;uint8_t gcs_connection_lost_counter;bool high_latency_data_link_lost;bool is_vtol;bool is_vtol_tailsitter;bool in_transition_mode;bool in_transition_to_fw;uint8_t system_type;uint8_t system_id;uint8_t component_id;bool safety_button_available;bool safety_off;bool power_input_valid;bool usb_connected;bool open_drone_id_system_present;bool open_drone_id_system_healthy;bool parachute_system_present;bool parachute_system_healthy;bool rc_calibration_in_progress;bool calibration_enabled;bool pre_flight_checks_pass;";

const LOCAL_POSITION_LEN: usize = 192;
const SENSOR_GPS_LEN: usize = 147;
const VEHICLE_STATUS_LEN: usize = 74;

fn profile() -> FamilyProfile {
    parse_profile(
        include_str!("fixtures/unknown-adapter--auterion-suite-export--profile.toml"),
        "development",
    )
    .expect("private Auterion Suite ULog case profile parses")
}

fn message(bytes: &mut Vec<u8>, kind: u8, payload: &[u8]) {
    bytes
        .extend_from_slice(&(u16::try_from(payload.len()).expect("bounded payload")).to_le_bytes());
    bytes.push(kind);
    bytes.extend_from_slice(payload);
}

fn header(file_version: u8) -> Vec<u8> {
    let mut bytes = MAGIC.to_vec();
    bytes.push(file_version);
    bytes.extend_from_slice(&0_u64.to_le_bytes());
    message(&mut bytes, b'B', &[0; 40]);
    bytes
}

fn body(msg_id: u16, len: usize) -> Vec<u8> {
    let mut out = msg_id.to_le_bytes().to_vec();
    out.extend(std::iter::repeat_n(0_u8, len));
    out
}

fn put_f32(body: &mut [u8], offset: usize, value: f32) {
    body[2 + offset..2 + offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn put_f64(body: &mut [u8], offset: usize, value: f64) {
    body[2 + offset..2 + offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn put_u64(body: &mut [u8], offset: usize, value: u64) {
    body[2 + offset..2 + offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn put_u8(body: &mut [u8], offset: usize, value: u8) {
    body[2 + offset] = value;
}

fn local_position_record(timestamp: u64) -> Vec<u8> {
    let mut out = body(0, LOCAL_POSITION_LEN);
    put_u64(&mut out, 0, timestamp); // timestamp
    put_u64(&mut out, 8, timestamp - 500); // timestamp_sample
    put_u8(&mut out, 16, 1); // xy_valid
    put_u8(&mut out, 17, 1); // z_valid
    put_u8(&mut out, 18, 1); // v_xy_valid
    put_u8(&mut out, 19, 0); // v_z_valid: declared invalid, still decoded and still counted
    put_f32(&mut out, 20, 12.5); // x, metres north
    put_f32(&mut out, 24, -4.25); // y, metres east
    put_f32(&mut out, 28, -18.0); // z, metres down (negative altitude)
    put_f32(&mut out, 46, 1.5); // vx, metres/sec north
    put_f32(&mut out, 50, -0.25); // vy, metres/sec east
    put_f32(&mut out, 54, 0.125); // vz, metres/sec down
    put_f32(&mut out, 88, 1.5); // heading, radians
    put_u8(&mut out, 110, 1); // xy_global
    put_u8(&mut out, 111, 1); // z_global
    put_f64(&mut out, 120, 47.25); // ref_lat, degrees
    put_f64(&mut out, 128, 8.5); // ref_lon, degrees
    put_f32(&mut out, 136, 412.0); // ref_alt, metres AMSL
    put_f32(&mut out, 155, 0.75); // eph, metres
    put_f32(&mut out, 159, 1.25); // epv, metres
    put_f32(&mut out, 163, 0.5); // evh, metres/sec
    put_f32(&mut out, 167, 0.25); // evv, metres/sec
    put_u8(&mut out, 171, 0); // dead_reckoning
    out
}

fn sensor_gps_record(timestamp: u64, utc: u64) -> Vec<u8> {
    let mut out = body(1, SENSOR_GPS_LEN);
    put_u64(&mut out, 0, timestamp);
    put_f64(&mut out, 20, 47.2501); // latitude_deg, WGS84
    put_f64(&mut out, 28, 8.5002); // longitude_deg, WGS84
    put_f64(&mut out, 36, 415.5); // altitude_msl_m
    put_f64(&mut out, 44, 463.25); // altitude_ellipsoid_m
    put_u8(&mut out, 60, 3); // fix_type = FIX_TYPE_3D
    put_f32(&mut out, 61, 0.8); // eph, metres
    put_f32(&mut out, 65, 1.4); // epv, metres
    put_u64(&mut out, 115, utc); // time_utc_usec
    put_u8(&mut out, 123, 14); // satellites_used
    out
}

fn vehicle_status_record(timestamp: u64) -> Vec<u8> {
    let mut out = body(2, VEHICLE_STATUS_LEN);
    put_u64(&mut out, 0, timestamp);
    put_u8(&mut out, 24, 2); // arming_state = ARMING_STATE_ARMED
    put_u8(&mut out, 35, 0); // nav_state_user_intention = NAVIGATION_STATE_MANUAL
    put_u8(&mut out, 36, 3); // nav_state = NAVIGATION_STATE_AUTO_MISSION
    put_u8(&mut out, 50, 0); // failsafe
    put_u8(&mut out, 51, 0); // failsafe_and_user_took_over
    put_u8(&mut out, 73, 1); // pre_flight_checks_pass
    out
}

fn declarations(file_version: u8) -> Vec<u8> {
    let mut bytes = header(file_version);
    message(&mut bytes, b'F', VEHICLE_LOCAL_POSITION);
    message(&mut bytes, b'F', SENSOR_GPS);
    message(&mut bytes, b'F', VEHICLE_STATUS);
    message(&mut bytes, b'A', b"\0\0\0vehicle_local_position");
    message(&mut bytes, b'A', b"\0\x01\0sensor_gps");
    message(&mut bytes, b'A', b"\0\x02\0vehicle_status");
    bytes
}

fn authored_log(utc: u64) -> Vec<u8> {
    let mut bytes = declarations(1);
    message(&mut bytes, b'D', &local_position_record(2_000_000));
    message(&mut bytes, b'D', &sensor_gps_record(2_000_250, utc));
    message(&mut bytes, b'D', &vehicle_status_record(2_000_500));
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

const UTC_US: u64 = 1_773_144_000_000_000;

#[test]
fn selected_flight_state_reaches_common_output_with_the_pinned_px4_meanings() {
    let profile = profile();
    for (field, expected) in [
        ("vehicle_local_position.x", "m_north_position"),
        ("vehicle_local_position.vz", "m_per_s_down_velocity"),
        (
            "vehicle_local_position.ref_lat",
            "deg_latitude_of_the_local",
        ),
        ("sensor_gps.latitude_deg", "deg_latitude_in_the_WGS84"),
        ("sensor_gps.time_utc_usec", "us_UTC_which_the_source_states"),
        (
            "vehicle_status.nav_state",
            "declared_enum_of_the_currently_active_mode",
        ),
    ] {
        let declared = profile
            .field_units
            .get(field)
            .unwrap_or_else(|| panic!("{field} is declared in the case profile"));
        assert!(
            declared.starts_with(expected),
            "{field} declares {declared}"
        );
    }

    let report = Px4UlgReader
        .read_report(&profile, &authored_log(UTC_US))
        .expect("authored Suite-shaped ULog decodes");
    assert_eq!(
        (
            report.data_records,
            report.decoded_data_records,
            report.unsupported_records.len(),
            report.observations.len()
        ),
        (3, 3, 0, 3)
    );

    let position = &report.observations[0];
    assert_eq!(position.channel, ChannelId::Onboard);
    assert_eq!(position.clock_basis, ClockBasis::BootRelative);
    assert_eq!(position.t_boot_us, Some(2_000_000));
    for (field, expected) in [
        ("vehicle_local_position.x", 12.5_f64),
        ("vehicle_local_position.y", -4.25),
        ("vehicle_local_position.z", -18.0),
        ("vehicle_local_position.vx", 1.5),
        ("vehicle_local_position.vy", -0.25),
        ("vehicle_local_position.vz", 0.125),
        ("vehicle_local_position.heading", 1.5),
        ("vehicle_local_position.eph", 0.75),
        ("vehicle_local_position.evh", 0.5),
    ] {
        assert_eq!(
            value(&position.fields, field),
            FieldValue::F64(expected),
            "{field}"
        );
    }
    assert_eq!(
        value(&position.fields, "vehicle_local_position.ref_lat"),
        FieldValue::F64(47.25)
    );
    assert_eq!(
        value(&position.fields, "vehicle_local_position.ref_lon"),
        FieldValue::F64(8.5)
    );
    assert_eq!(
        value(&position.fields, "vehicle_local_position.ref_alt"),
        FieldValue::F64(412.0)
    );
    assert_eq!(
        value(&position.fields, "vehicle_local_position.xy_valid"),
        FieldValue::I64(1)
    );
    assert_eq!(
        value(&position.fields, "vehicle_local_position.v_z_valid"),
        FieldValue::I64(0)
    );
    assert_eq!(
        value(&position.fields, "vehicle_local_position.xy_global"),
        FieldValue::I64(1)
    );

    let gps = &report.observations[1];
    assert_eq!(gps.channel, ChannelId::GpsEkf);
    for (field, expected) in [
        ("sensor_gps.latitude_deg", 47.2501_f64),
        ("sensor_gps.longitude_deg", 8.5002),
        ("sensor_gps.altitude_msl_m", 415.5),
        ("sensor_gps.altitude_ellipsoid_m", 463.25),
    ] {
        assert_eq!(
            value(&gps.fields, field),
            FieldValue::F64(expected),
            "{field}"
        );
    }
    assert_eq!(
        value(&gps.fields, "sensor_gps.fix_type"),
        FieldValue::I64(3)
    );
    assert_eq!(
        value(&gps.fields, "sensor_gps.satellites_used"),
        FieldValue::I64(14)
    );
    assert_eq!(gps.anchor_unix_us, Some(i64::try_from(UTC_US).unwrap()));
    assert_eq!(position.anchor_unix_us, None);

    let status = &report.observations[2];
    assert_eq!(status.channel, ChannelId::Event);
    assert_eq!(
        value(&status.fields, "vehicle_status.arming_state"),
        FieldValue::I64(2)
    );
    assert_eq!(
        value(&status.fields, "vehicle_status.nav_state"),
        FieldValue::I64(3)
    );
    assert_eq!(
        value(&status.fields, "vehicle_status.nav_state_user_intention"),
        FieldValue::I64(0)
    );
    assert_eq!(
        value(&status.fields, "vehicle_status.failsafe"),
        FieldValue::I64(0)
    );
    assert_eq!(
        value(&status.fields, "vehicle_status.pre_flight_checks_pass"),
        FieldValue::I64(1)
    );
}

#[test]
fn an_unavailable_utc_stamp_leaves_the_record_boot_relative_and_still_counted() {
    let report = Px4UlgReader
        .read_report(&profile(), &authored_log(0))
        .expect("a log without UTC still decodes");
    assert_eq!(
        (report.data_records, report.decoded_data_records),
        (3, 3),
        "a missing UTC is not a dropped record"
    );
    let gps = &report.observations[1];
    assert_eq!(
        value(&gps.fields, "sensor_gps.time_utc_usec"),
        FieldValue::I64(0)
    );
    assert_eq!(gps.anchor_unix_us, None);
    assert!(
        report
            .observations
            .iter()
            .all(|o| o.clock_basis == ClockBasis::BootRelative)
    );
}

#[test]
fn a_container_this_path_does_not_accept_is_refused_rather_than_partly_read() {
    let mut bytes = declarations(2);
    message(&mut bytes, b'D', &local_position_record(2_000_000));
    assert!(Px4UlgReader.read_report(&profile(), &bytes).is_err());

    let mut truncated = declarations(1);
    let mut short = local_position_record(2_000_000);
    short.truncate(short.len() - 8);
    message(&mut truncated, b'D', &short);
    assert!(Px4UlgReader.read_report(&profile(), &truncated).is_err());
}
