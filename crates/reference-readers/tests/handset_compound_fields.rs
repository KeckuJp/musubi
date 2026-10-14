use musubi_reference_readers::{
    FieldValue, ProfileReader,
    edgetx::{EdgeTxCsvReader, HandsetCompoundKind},
    profile::parse_profile,
};

#[test]
fn two_fixed_writer_clock_inputs_reuse_common_meaning_and_stream() {
    for rtc in [false, true] {
        let profile = if rtc {
            include_str!(
                "../../../profiles/declared/edgetx-handset-clock/rtc-date-time.profile.toml"
            )
        } else {
            include_str!(
                "../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"
            )
        };
        let profile = parse_profile(profile, "test").unwrap();
        let (header, time) = if rtc {
            ("Date,Time", "2026-10-09,10:20:30.000")
        } else {
            ("tmr10ms", "1000")
        };
        let input = format!(
            "{header},RQly(%),Location,Calendar,Label,RxBt(V)\n{time},99,-0.123456 139.123456,2024-02-29 01:02:03,\"ready, sensor\",12.5\n"
        );
        let roles = [
            ("Location", HandsetCompoundKind::GpsPair),
            ("Calendar", HandsetCompoundKind::SensorCalendar),
        ];
        let before = EdgeTxCsvReader.read(&profile, input.as_bytes()).unwrap();
        let rows = EdgeTxCsvReader
            .read_with_reported_fields(&profile, input.as_bytes(), &roles)
            .unwrap();
        let value = |key: &str| &rows[0].fields.iter().find(|(k, _)| k == key).unwrap().1;
        assert_eq!(
            value("handset_reported.Location.latitude_deg"),
            &FieldValue::F64(-0.123456)
        );
        assert_eq!(
            value("handset_reported.Location.longitude_deg"),
            &FieldValue::F64(139.123456)
        );
        assert_eq!(value("handset_reported.Calendar.day"), &FieldValue::I64(29));
        assert_eq!(value("handset_si.RxBt(V).value"), &FieldValue::F64(12.5));
        let mut restored = rows[0].clone();
        restored.fields.truncate(before[0].fields.len());
        assert_eq!(restored, before[0]);
        let mut streamed = Vec::new();
        let counts = EdgeTxCsvReader
            .read_stream_with_reported_fields(&profile, &mut input.as_bytes(), &roles, |r| {
                streamed.push(r);
                Ok(())
            })
            .unwrap();
        assert_eq!(streamed, rows);
        assert_eq!(counts.emitted_records, 1);
    }
}

#[test]
fn missing_zero_invalid_and_role_errors_do_not_become_positions_or_clocks() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let roles = [
        ("GPS", HandsetCompoundKind::GpsPair),
        ("SensorTime", HandsetCompoundKind::SensorCalendar),
    ];
    let input=b"tmr10ms,RQly(%),GPS,SensorTime\n1,0,,0000-00-00 00:00:00\n2,99,0.000000 12.000000,2023-02-29 01:02:03\n3,99,91.000000 12.000000,2026-10-09 01:02:03\n";
    let rows = EdgeTxCsvReader
        .read_with_reported_fields(&p, input, &roles)
        .unwrap();
    for row in &rows {
        assert!(!row.fields.iter().any(|(k, _)| k.ends_with("latitude_deg")));
    }
    assert!(
        rows[0]
            .fields
            .iter()
            .any(|(_, v)| v == &FieldValue::Text("BLANK_REPORTED".into()))
    );
    assert!(
        rows[1]
            .fields
            .iter()
            .any(|(_, v)| v == &FieldValue::Text("ZERO_PAIR_NOT_EMITTED_BY_FIXED_WRITER".into()))
    );
    assert!(
        rows[2]
            .fields
            .iter()
            .any(|(_, v)| v == &FieldValue::Text("INVALID_COORDINATE_PAIR".into()))
    );
    assert!(
        EdgeTxCsvReader
            .read_with_reported_fields(&p, input, &[("absent", HandsetCompoundKind::GpsPair)])
            .is_err()
    );
    assert!(
        EdgeTxCsvReader
            .read_with_reported_fields(&p, input, &[roles[0], roles[0]])
            .is_err()
    );
    let collision = b"tmr10ms,RQly(%),handset_reported.x\n1,99,raw\n";
    assert!(EdgeTxCsvReader.read(&p, collision).is_ok());
    assert!(
        EdgeTxCsvReader
            .read_with_reported_fields(&p, collision, &[])
            .is_err()
    );
}
