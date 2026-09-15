use musubi_reference_readers::{
    FieldValue, ProfileReader, field, profile::parse_profile, telemetry_csv::TelemetryCsvReader,
};
use musubi_reference_types::{ChannelId, ClockBasis, Family};

const PROFILE: &str = include_str!("fixtures/boreas-enu--profile.toml");
const HEADER: &str = "GPSTime,easting,northing,altitude,vel_east,vel_north,vel_up,roll,pitch,heading,angvel_z,angvel_y,angvel_x,vendor_future,vendor_blank";
const VALUES: &str = "1.25,-2.5,0.125,0.75,-1.5,0.25,0.01,-0.02,0.3,0.1,-0.2,0.4,not-identified,";

fn row(time: &str) -> String {
    format!("{time},{VALUES}\n")
}

#[test]
fn profile_only_route_retains_local_source_fields_without_clock_or_frame_inference() {
    let p = parse_profile(PROFILE, "synthetic").unwrap();
    assert_eq!(p.family, Family::Ugv);
    assert_eq!(p.fields.time, "GPSTime");
    assert_eq!(p.channels, vec![ChannelId::Onboard]);
    assert_eq!(p.default_clock_basis, ClockBasis::Unknown);
    let input = format!("{HEADER}\n{}{}", row("1000001"), row("2000002"));
    let observations = TelemetryCsvReader.read(&p, input.as_bytes()).unwrap();
    assert_eq!(observations.len(), 2);
    for (index, observation) in observations.iter().enumerate() {
        assert_eq!(observation.channel, ChannelId::Onboard);
        assert_eq!(observation.clock_basis, ClockBasis::Unknown);
        assert_eq!(observation.time_confidence, 0.0);
        assert_eq!(observation.t_boot_us, None);
        assert_eq!(observation.wall_ms, None);
        assert_eq!(observation.anchor_unix_us, None);
        assert_eq!(observation.fields.len(), 15);
        assert_eq!(observation.t_ms, [1000, 2000][index]);
        assert_eq!(
            field(observation, "GPSTime"),
            Some(&FieldValue::I64([1000001, 2000002][index]))
        );
        assert_eq!(field(observation, "easting"), Some(&FieldValue::F64(1.25)));
        assert_eq!(field(observation, "northing"), Some(&FieldValue::F64(-2.5)));
        assert_eq!(
            field(observation, "altitude"),
            Some(&FieldValue::F64(0.125))
        );
        assert_eq!(field(observation, "vel_east"), Some(&FieldValue::F64(0.75)));
        assert_eq!(
            field(observation, "vel_north"),
            Some(&FieldValue::F64(-1.5))
        );
        assert_eq!(field(observation, "vel_up"), Some(&FieldValue::F64(0.25)));
        assert_eq!(field(observation, "roll"), Some(&FieldValue::F64(0.01)));
        assert_eq!(field(observation, "pitch"), Some(&FieldValue::F64(-0.02)));
        assert_eq!(field(observation, "heading"), Some(&FieldValue::F64(0.3)));
        assert_eq!(field(observation, "angvel_z"), Some(&FieldValue::F64(0.1)));
        assert_eq!(field(observation, "angvel_y"), Some(&FieldValue::F64(-0.2)));
        assert_eq!(field(observation, "angvel_x"), Some(&FieldValue::F64(0.4)));
        assert_eq!(
            field(observation, "vendor_future"),
            Some(&FieldValue::Text("not-identified".into()))
        );
        assert_eq!(field(observation, "vendor_blank"), Some(&FieldValue::Blank));
        assert_eq!(field(observation, "latitude"), None);
        assert_eq!(field(observation, "longitude"), None);
    }
    for name in HEADER.split(',').take(13) {
        assert!(
            p.field_units.get(name).is_some_and(|unit| !unit.is_empty()),
            "missing source unit for {name}"
        );
    }
}

#[test]
fn old_trajectory_profile_does_not_silently_reinterpret_microseconds() {
    let p = parse_profile(
        include_str!("fixtures/turtlebot3-trajectory--profile.toml"),
        "synthetic",
    )
    .unwrap();
    let input = format!("{HEADER}\n{}", row("1000001"));
    assert!(TelemetryCsvReader.read(&p, input.as_bytes()).is_err());
}

#[test]
fn structurally_wrong_inputs_reject_without_partial_observation_output() {
    let p = parse_profile(PROFILE, "synthetic").unwrap();
    let valid = format!("{HEADER}\n{}", row("1000001"));
    let inputs = [
        String::new(),
        format!("{HEADER}\n"),
        valid.replace("GPSTime", "OtherTime"),
        valid.replace("GPSTime", "GPSTime (s)"),
        valid.replace("easting,northing", "easting,easting"),
        format!("{HEADER}\n1000001,1.25\n"),
        format!("{valid}\n"),
        valid.replace("1.25", "NaN"),
        valid.replace("1.25", "inf"),
        valid.replace("not-identified", "\"quoted\""),
        valid.replace("not-identified", "\0"),
        format!("{valid}{}", row("1000000")),
    ];
    for input in inputs {
        assert!(TelemetryCsvReader.read(&p, input.as_bytes()).is_err());
    }
    for time in ["-1", "1.25", "oops", "NaN", "9223372036854775808"] {
        let input = format!("{HEADER}\n{}", row(time));
        assert!(TelemetryCsvReader.read(&p, input.as_bytes()).is_err());
    }
}

#[test]
fn equal_times_require_existing_opt_in_and_keep_both_records() {
    let p = parse_profile(PROFILE, "synthetic").unwrap();
    let input = format!("{HEADER}\n{}{}", row("1000001"), row("1000001"));
    assert!(TelemetryCsvReader.read(&p, input.as_bytes()).is_err());
    let observations = TelemetryCsvReader
        .read_non_decreasing(&p, input.as_bytes())
        .unwrap();
    assert_eq!(observations.len(), 2);
    assert_eq!(observations[0].t_ms, observations[1].t_ms);
    assert_eq!(
        field(&observations[0], "GPSTime"),
        field(&observations[1], "GPSTime")
    );
    assert_eq!(observations[0].clock_basis, ClockBasis::Unknown);
    let decreasing = format!("{HEADER}\n{}{}", row("1000001"), row("1000000"));
    assert!(
        TelemetryCsvReader
            .read_non_decreasing(&p, decreasing.as_bytes())
            .is_err()
    );
}

#[test]
fn exact_i64_microsecond_boundary_is_preserved_as_a_source_field() {
    let p = parse_profile(PROFILE, "synthetic").unwrap();
    let input = format!("{HEADER}\n{}", row("9223372036854775807"));
    let observations = TelemetryCsvReader.read(&p, input.as_bytes()).unwrap();
    assert_eq!(
        field(&observations[0], "GPSTime"),
        Some(&FieldValue::I64(i64::MAX))
    );
    assert_eq!(observations[0].t_ms, i64::MAX / 1000);
    assert_eq!(observations[0].t_boot_us, None);
}
