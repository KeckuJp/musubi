use musubi_reference_readers::{
    ClockBasis, FieldValue, ProfileReader, telemetry_csv::TelemetryCsvReader,
};

fn profile() -> musubi_reference_readers::FamilyProfile {
    musubi_reference_readers::profile::parse_profile(
        include_str!("fixtures/turtlebot3-trajectory--profile.toml"),
        "public",
    )
    .unwrap()
}

#[test]
fn explicit_non_decreasing_path_preserves_ties_and_source_order() {
    let input =
        b"record_time (us),tx,qw,extra\n1001,-1,2,first\n1001,-2,3,second\n2001,-3,4,third\n";
    assert!(musubi_decoded_csv::parse(input, "record_time").is_err());
    assert!(TelemetryCsvReader.read(&profile(), input).is_err());
    assert_eq!(
        musubi_decoded_csv::parse_non_decreasing(input, "record_time")
            .unwrap()
            .rows
            .len(),
        3
    );
    let rows = TelemetryCsvReader
        .read_non_decreasing(&profile(), input)
        .unwrap();
    assert_eq!(rows.len(), 3);
    for (index, row) in rows.iter().enumerate() {
        assert_eq!(row.clock_basis, ClockBasis::Unknown);
        assert_eq!(row.time_confidence, 0.0);
        assert_eq!(
            (row.t_boot_us, row.wall_ms, row.anchor_unix_us),
            (None, None, None)
        );
        assert_eq!(row.t_ms, if index < 2 { 1 } else { 2 });
        for (key, expected) in [
            ("record_time", if index < 2 { 1001 } else { 2001 }),
            ("tx", -(index as i64) - 1),
            ("qw", index as i64 + 2),
        ] {
            assert_eq!(
                musubi_reference_readers::field(row, key),
                Some(&FieldValue::I64(expected))
            );
        }
        assert_eq!(
            musubi_reference_readers::field(row, "extra"),
            Some(&FieldValue::Text(
                ["first", "second", "third"][index].into()
            ))
        );
    }
}

#[test]
fn decreases_and_wrong_profile_or_clock_still_fail() {
    let decreasing = b"record_time (us),tx\n2001,1\n1001,2\n";
    assert!(musubi_decoded_csv::parse_non_decreasing(decreasing, "record_time").is_err());
    assert!(
        TelemetryCsvReader
            .read_non_decreasing(&profile(), decreasing)
            .is_err()
    );
    let input = b"record_time (us),tx\n1001,1\n1001,2\n";
    let mut wrong = profile();
    wrong.format = "blackbox_decoded_csv".into();
    assert!(
        TelemetryCsvReader
            .read_non_decreasing(&wrong, input)
            .is_err()
    );
    wrong = profile();
    wrong.fields.time = "missing".into();
    assert!(
        TelemetryCsvReader
            .read_non_decreasing(&wrong, input)
            .is_err()
    );
    for clock in [
        ClockBasis::GpsLocked,
        ClockBasis::HostReceived,
        ClockBasis::RtcSetOnce,
        ClockBasis::GpsSuspect,
        ClockBasis::BootRelativeOffsetEstimated,
    ] {
        wrong = profile();
        wrong.default_clock_basis = clock;
        assert!(
            TelemetryCsvReader
                .read_non_decreasing(&wrong, input)
                .is_err()
        );
    }
}
