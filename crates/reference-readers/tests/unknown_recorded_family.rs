use musubi_reference_readers::{
    ClockBasis, FieldValue, field, profile::parse_profile, telemetry_csv::TelemetryCsvReader,
};

const PROFILE: &str = r#"
profile_id = "synthetic_unlocated_wind"
version = "1"
family = "unknown"
source_role = "recorded_export"
format = "telemetry_csv_us"
extensions = ["csv"]
default_clock_basis = "unknown"
channels = ["onboard"]
[fields]
time = "unlocated_time_us"
"#;

#[test]
fn explicit_unknown_family_is_preserved_without_vehicle_or_clock_promotion() {
    let profile = parse_profile(PROFILE, "synthetic").expect("explicit unknown family must parse");
    assert_eq!(profile.family.as_str(), "unknown");
    let observations = TelemetryCsvReader
        .read_non_decreasing(
            &profile,
            b"unlocated_time_us,source_line,wind_speed_m_s\n0,1,2.5\n0,3,\n",
        )
        .unwrap();
    assert_eq!(observations.len(), 2);
    for observation in &observations {
        assert_eq!(observation.clock_basis, ClockBasis::Unknown);
        assert_eq!(observation.time_confidence, 0.0);
        assert_eq!(observation.t_ms, 0);
        assert_eq!(
            (
                observation.t_boot_us,
                observation.wall_ms,
                observation.anchor_unix_us
            ),
            (None, None, None)
        );
    }
    assert_eq!(
        field(&observations[0], "wind_speed_m_s"),
        Some(&FieldValue::F64(2.5))
    );
    assert_eq!(
        field(&observations[1], "wind_speed_m_s"),
        Some(&FieldValue::Blank)
    );
    assert_eq!(
        field(&observations[1], "source_line"),
        Some(&FieldValue::I64(3))
    );
}

#[test]
fn arbitrary_or_misspelled_family_does_not_silently_become_unknown() {
    for family in ["marine", "Unknown", "arbitrary", ""] {
        let text = PROFILE.replace("family = \"unknown\"", &format!("family = \"{family}\""));
        assert!(parse_profile(&text, "synthetic").is_err(), "{family}");
    }
}
