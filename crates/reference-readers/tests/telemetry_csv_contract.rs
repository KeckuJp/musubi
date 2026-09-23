use musubi_reference_readers::{
    ClockBasis, FieldValue, ProfileReader, SourceRole, telemetry_csv::TelemetryCsvReader,
};

fn profile() -> musubi_reference_readers::FamilyProfile {
    musubi_reference_readers::profile::parse_profile(
        include_str!("fixtures/unknown-adapter--turtlebot3-trajectory--profile.toml"),
        "public",
    )
    .unwrap()
}

#[test]
fn single_parse_report_preserves_columns_rows_policies_and_observations() {
    let selected = profile();
    let bytes = b"record_time (us),x_true,future\n1001,2.75,NaN\n1001,3.25,next\n";
    let options = musubi_decoded_csv::ParseOptions {
        allow_equal_time: true,
        preserve_nonfinite_as_text: true,
    };
    let report = TelemetryCsvReader
        .read_report_with_options(&selected, bytes, options)
        .unwrap();
    let old = TelemetryCsvReader
        .read_with_options(&selected, bytes, options)
        .unwrap();
    let table =
        musubi_decoded_csv::parse_with_options(bytes, &selected.fields.time, options).unwrap();
    assert_eq!(report.source_columns, table.columns);
    assert_eq!(report.source_rows, table.rows.len());
    assert_eq!(report.observations.len(), old.len());
    for (observed, previous) in report.observations.iter().zip(old.iter()) {
        assert_eq!(observed.fields, previous.fields);
        assert_eq!(observed.digest, previous.digest);
        assert_eq!(observed.clock_basis, ClockBasis::Unknown);
        assert_eq!(observed.t_boot_us, None);
    }
    assert!(
        TelemetryCsvReader
            .read_report_with_options(&selected, bytes, Default::default())
            .is_err()
    );
    let mut wrong = selected.clone();
    wrong.format = "px4_ulog".into();
    assert!(
        TelemetryCsvReader
            .read_report_with_options(&wrong, bytes, options)
            .is_err()
    );
    wrong = selected.clone();
    wrong.default_clock_basis = ClockBasis::GpsLocked;
    assert!(
        TelemetryCsvReader
            .read_report_with_options(&wrong, bytes, options)
            .is_err()
    );
}

#[test]
fn recorded_export_retains_unknown_fields_without_inventing_clock() {
    let result = TelemetryCsvReader.read(&profile(),
        b"record_time (us),time,x_true,future,optional\n1001,0.001001,2.75,new,\n2001,0.002001,3.25,next,0\n").unwrap();
    assert_eq!(result.len(), 2);
    assert_eq!(result[0].t_ms, 1);
    assert_eq!(result[1].t_ms, 2);
    assert_eq!(
        musubi_reference_readers::field(&result[0], "record_time"),
        Some(&FieldValue::I64(1001))
    );
    assert_eq!(
        musubi_reference_readers::field(&result[0], "time"),
        Some(&FieldValue::F64(0.001001))
    );
    for observation in &result {
        assert_eq!(observation.source_role, SourceRole::RecordedExport);
        assert_eq!(observation.clock_basis, ClockBasis::Unknown);
        assert_eq!(observation.time_confidence, 0.0);
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
        musubi_reference_readers::field(&result[0], "future"),
        Some(&FieldValue::Text("new".into()))
    );
    assert_eq!(
        musubi_reference_readers::field(&result[0], "optional"),
        Some(&FieldValue::Blank)
    );
    assert_eq!(
        musubi_reference_readers::field(&result[1], "optional"),
        Some(&FieldValue::I64(0))
    );
    assert_eq!(
        musubi_reference_readers::field(&result[0], "x_true"),
        Some(&FieldValue::F64(2.75))
    );
}

#[test]
fn explicit_boot_relative_profile_preserves_microseconds_without_wall_anchor() {
    let mut selected = profile();
    selected.default_clock_basis = ClockBasis::BootRelative;
    let result = TelemetryCsvReader
        .read(&selected, b"record_time (us),x_true\n1001,2.75\n")
        .unwrap();
    assert_eq!(result[0].clock_basis, ClockBasis::BootRelative);
    assert_eq!(result[0].t_boot_us, Some(1001));
    assert_eq!(result[0].t_ms, 1);
    assert_eq!(result[0].wall_ms, None);
    assert_eq!(result[0].anchor_unix_us, None);
}

#[test]
fn non_csv_and_ragged_input_are_not_silent_empty_success() {
    for bytes in [
        b"{\"record_time\":1001}".as_slice(),
        b"record_time (us),x\n1001\n",
        b"record_time (us),x\n1001,2,3\n",
        b"record_time (us),x\nnot-a-time,2\n",
    ] {
        assert!(TelemetryCsvReader.read(&profile(), bytes).is_err());
    }
}

#[test]
fn wrong_format_missing_time_and_forbidden_clock_are_rejected() {
    let input = b"record_time (us),x\n1001,2\n";
    let mut wrong = profile();
    wrong.format = "blackbox_decoded_csv".into();
    assert!(TelemetryCsvReader.read(&wrong, input).is_err());
    wrong = profile();
    wrong.fields.time = "other_time".into();
    assert!(TelemetryCsvReader.read(&wrong, input).is_err());
    for clock in [
        ClockBasis::GpsLocked,
        ClockBasis::HostReceived,
        ClockBasis::RtcSetOnce,
        ClockBasis::GpsSuspect,
        ClockBasis::BootRelativeOffsetEstimated,
    ] {
        wrong = profile();
        wrong.default_clock_basis = clock;
        assert!(TelemetryCsvReader.read(&wrong, input).is_err());
    }
}
