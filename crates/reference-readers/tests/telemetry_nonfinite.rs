use musubi_decoded_csv::ParseOptions;
use musubi_reference_readers::{
    ClockBasis, FieldValue, ProfileReader, field, profile::parse_profile,
    telemetry_csv::TelemetryCsvReader,
};

fn profile() -> musubi_reference_readers::FamilyProfile {
    parse_profile(
        r#"
profile_id = "synthetic_recorded_nonfinite"
version = "1"
family = "unknown"
source_role = "recorded_export"
format = "telemetry_csv_us"
extensions = ["csv"]
default_clock_basis = "boot_relative"
channels = ["onboard"]
[fields]
time = "timestamp"
"#,
        "synthetic",
    )
    .unwrap()
}

fn preserve() -> ParseOptions {
    ParseOptions {
        allow_equal_time: true,
        preserve_nonfinite_as_text: true,
    }
}

#[test]
fn battery_only_profile_does_not_assign_an_air_vehicle_family() {
    let profile = parse_profile(
        include_str!("fixtures/unknown-adapter--px4-battery--profile.toml"),
        "synthetic",
    )
    .unwrap();
    assert_eq!(profile.family.as_str(), "unknown");
    assert_eq!(profile.default_clock_basis, ClockBasis::BootRelative);
}

#[test]
fn existing_route_rejects_and_explicit_route_retains_text_and_clock() {
    let input = b"timestamp,current_a,unselected\n1001,2,nan\n1001,3,inf\n";
    assert!(TelemetryCsvReader.read(&profile(), input).is_err());
    assert!(
        TelemetryCsvReader
            .read_non_decreasing(&profile(), input)
            .is_err()
    );
    let records = TelemetryCsvReader
        .read_with_options(&profile(), input, preserve())
        .unwrap();
    assert_eq!(records.len(), 2);
    for row in &records {
        assert_eq!(row.clock_basis, ClockBasis::BootRelative);
        assert_eq!(row.t_boot_us, Some(1001));
        assert_eq!(row.t_ms, 1);
        assert_eq!((row.wall_ms, row.anchor_unix_us), (None, None));
    }
    assert_eq!(
        field(&records[0], "unselected"),
        Some(&FieldValue::Text("nan".into()))
    );
    assert_eq!(
        field(&records[1], "unselected"),
        Some(&FieldValue::Text("inf".into()))
    );
}

#[test]
fn options_preserve_profile_clock_and_invalid_time_guards() {
    let input = b"timestamp,unselected\n1001,nan\n";
    let mut unknown = profile();
    unknown.default_clock_basis = ClockBasis::Unknown;
    let records = TelemetryCsvReader
        .read_with_options(&unknown, input, preserve())
        .unwrap();
    assert_eq!(records[0].clock_basis, ClockBasis::Unknown);
    assert_eq!(records[0].time_confidence, 0.0);
    assert_eq!(
        (
            records[0].t_boot_us,
            records[0].wall_ms,
            records[0].anchor_unix_us
        ),
        (None, None, None)
    );
    let mut bad = profile();
    bad.format = "wrong_format".into();
    assert!(
        TelemetryCsvReader
            .read_with_options(&bad, input, preserve())
            .is_err()
    );
    bad = profile();
    bad.default_clock_basis = ClockBasis::GpsLocked;
    assert!(
        TelemetryCsvReader
            .read_with_options(&bad, input, preserve())
            .is_err()
    );
    assert!(
        TelemetryCsvReader
            .read_with_options(&profile(), b"timestamp,x\nnan,2\n", preserve())
            .is_err()
    );
}
