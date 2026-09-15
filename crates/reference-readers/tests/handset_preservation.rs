use musubi_reference_readers::{
    ClockBasis, FieldValue, ProfileReader, edgetx::EdgeTxCsvReader, profile::parse_profile,
};

fn profile(time: &str) -> musubi_reference_types::FamilyProfile {
    parse_profile(
        &format!(
            r#"profile_id="handset"
version="1"
family="fpv"
source_role="handset"
format="edgetx_csv"
extensions=["csv"]
default_clock_basis="host_received"
[fields]
time="{time}"
link=["RQly(%)"]
"#
        ),
        "test",
    )
    .unwrap()
}

#[test]
fn unknown_columns_and_equal_time_records_survive_without_changing_stale_selection() {
    let p = profile("Date,Time");
    let input = b"Date,Time,RQly(%),unknown\n2024-02-29,12:34:56.100,100, keep \n2024-02-29,12:34:56.100,0,keep2\n";
    let observations = EdgeTxCsvReader.read(&p, input).unwrap();
    assert_eq!(observations.len(), 2);
    assert_eq!(observations[0].fields.len(), 4);
    assert!(
        observations[0]
            .fields
            .contains(&("unknown".into(), FieldValue::Text(" keep ".into())))
    );
    assert!(!observations[0].stale);
    assert!(observations[1].stale);
    assert!(observations.iter().all(|o| o.anchor_unix_us.is_none()));
    assert!(
        observations
            .iter()
            .all(|o| o.clock_basis == ClockBasis::Unknown && o.wall_ms.is_none())
    );
}

#[test]
fn official_time_tick_header_requires_explicit_mapping_and_keeps_all_rows() {
    for mapping in ["Time", "Date,Time|Time|tmr10ms"] {
        let p = profile(mapping);
        let input = b"Time,RQly(%),future\n100,100,a\n100,90,b\n101,0,c\n";
        let observations = EdgeTxCsvReader.read(&p, input).unwrap();
        assert_eq!(
            observations.iter().map(|o| o.t_ms).collect::<Vec<_>>(),
            [1000, 1000, 1010]
        );
        assert!(
            observations
                .iter()
                .all(|o| o.clock_basis == ClockBasis::BootRelative && o.wall_ms.is_none())
        );
        assert_eq!(observations[2].fields[2].1, FieldValue::Text("c".into()));
        for invalid in [
            b"Time,RQly(%)\n1.5,100\n".as_slice(),
            b"Time,RQly(%)\n-1,100\n",
            b"Time,RQly(%)\n2,100\n1,100\n",
        ] {
            assert!(EdgeTxCsvReader.read(&p, invalid).is_err());
        }
    }
    let p = profile("Date,Time|Time|tmr10ms");
    assert!(
        EdgeTxCsvReader
            .read(&p, b"Time,tmr10ms,RQly(%)\n10,10,100\n")
            .is_err()
    );
}

#[test]
fn invalid_calendar_width_duplicates_and_nonfinite_values_fail_closed() {
    let p = profile("Date,Time");
    for value in [
        "Date,Time,RQly(%)\n2023-02-29,12:00:00,100\n",
        "Date,Time,RQly(%)\n2024-13-01,12:00:00,100\n",
        "Date,Time,RQly(%)\n2024-02-29,24:00:00,100\n",
        "Date,Time,RQly(%)\n2024-02-29,12:00:00:extra,100\n",
        "Date,Time,RQly(%)\n2024-02-29,12:00:00,NaN\n",
        "Date,Time,RQly(%)\n2024-02-29,12:00:00\n",
        "Date,Time,RQly(%)\n2024-02-29,12:00:00,100,extra\n",
        "Date,Time,RQly(%),RQly(%)\n2024-02-29,12:00:00,100,0\n",
        "Date,Time,RQly(%)\n\n",
        "Date,Time,RQly(%)\n2024-02-29,12:00:00,\"100\"\n",
    ] {
        assert!(
            EdgeTxCsvReader.read(&p, value.as_bytes()).is_err(),
            "{value}"
        );
    }
}

#[test]
fn integer_fields_outside_i64_are_preserved_without_float_rounding() {
    let p = profile("Time");
    for token in [
        "9223372036854775808",
        "18446744073709551615",
        "-9223372036854775809",
        " +18446744073709551615 ",
    ] {
        let input = format!(
            "Time,RQly(%),identifier,normal,decimal\n1,100,{token},9223372036854775807,1.25\n"
        );
        let rows = EdgeTxCsvReader.read(&p, input.as_bytes()).unwrap();
        assert_eq!(rows[0].fields[2].1, FieldValue::Text(token.into()));
        assert_eq!(rows[0].fields[3].1, FieldValue::I64(i64::MAX));
        assert_eq!(rows[0].fields[4].1, FieldValue::F64(1.25));
    }
}

#[test]
fn ticks_are_bounded_ordered_and_require_some_declared_sensor() {
    let p = profile("tmr10ms");
    for input in [
        b"tmr10ms,RQly(%)\n-1,100\n".as_slice(),
        b"tmr10ms,RQly(%)\n9223372036854775807,100\n",
        b"tmr10ms,RQly(%)\n2,100\n1,100\n",
        b"tmr10ms,unknown\n1,100\n",
    ] {
        assert!(EdgeTxCsvReader.read(&p, input).is_err());
    }
    let mut wrong = p.clone();
    wrong.format = "video_presence".into();
    assert!(
        EdgeTxCsvReader
            .read(&wrong, b"tmr10ms,RQly(%)\n1,100\n")
            .is_err()
    );
}
