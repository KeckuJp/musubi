use musubi_reference_readers::{
    FieldValue, ProfileReader,
    edgetx::{EdgeTxCsvReader, HandsetControlKind as K, HandsetWriter as W},
    profile::parse_profile,
};

#[test]
fn independent_saved_layouts_reach_common_and_stream_without_commands() {
    for (writer, rtc) in [
        (W::EdgeTx65bb97db, false),
        (W::OpenTxFb8622ddTaranisHorus, true),
    ] {
        let p = parse_profile(
            if rtc {
                include_str!("fixtures/edgetx-handset-clock--rtc-date-time.profile.toml")
            } else {
                include_str!("fixtures/edgetx-handset-clock--boot-counter.profile.toml")
            },
            "test",
        )
        .unwrap();
        let (h, t) = if rtc {
            ("Date,Time", "2026-10-12,12:00:00.000")
        } else {
            ("tmr10ms", "1000")
        };
        let input = format!(
            "{h},RQly(%),Rud,SA,LSW,CH1(us),Other\n{t},99,-123,1,0x8000000000000001,1500,unknown\n"
        );
        let mut roles = vec![
            ("Rud", K::CalibratedAnalog),
            ("SA", K::Switch),
            ("LSW", K::LogicalWord),
        ];
        if !rtc {
            roles.push(("CH1(us)", K::LoggedPulse));
        }
        let old = EdgeTxCsvReader.read(&p, input.as_bytes()).unwrap();
        let rows = EdgeTxCsvReader
            .read_with_reported_controls(&p, input.as_bytes(), writer, &roles)
            .unwrap();
        let get = |name: &str| &rows[0].fields.iter().find(|(k, _)| k == name).unwrap().1;
        assert_eq!(
            get("reported_control.Rud.calibrated_integer"),
            &FieldValue::I64(-123)
        );
        assert_eq!(get("reported_control.SA.switch_state"), &FieldValue::I64(1));
        for bit in [0, 63] {
            assert_eq!(
                get(&format!("reported_control.LSW.bit_{bit}")),
                &FieldValue::I64(1)
            );
        }
        assert_eq!(get("reported_control.LSW.bit_1"), &FieldValue::I64(0));
        assert_eq!(
            get("reported_control.LSW.original_word"),
            &FieldValue::Text("0x8000000000000001".into())
        );
        if !rtc {
            assert_eq!(
                get("reported_control.CH1(us).pulse_duration_s"),
                &FieldValue::F64(0.0015)
            );
        }
        let mut restored = rows[0].clone();
        restored.fields.truncate(old[0].fields.len());
        assert_eq!(restored, old[0]);
        let mut stream = vec![];
        let count = EdgeTxCsvReader
            .read_stream_with_reported_controls(&p, &mut input.as_bytes(), writer, &roles, |r| {
                stream.push(r);
                Ok(())
            })
            .unwrap();
        assert_eq!(stream, rows);
        assert_eq!(count.emitted_records, 1);
    }
}

#[test]
fn invalid_roles_words_ranges_and_clock_refuse() {
    let p = parse_profile(
        include_str!("fixtures/edgetx-handset-clock--boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let roles = [("SA", K::Switch), ("LSW", K::LogicalWord)];
    let good = "tmr10ms,RQly(%),SA,LSW\n1,99,-1,0xFFFFFFFFFFFFFFFF\n";
    assert!(
        EdgeTxCsvReader
            .read_with_reported_controls(&p, good.as_bytes(), W::EdgeTx65bb97db, &roles)
            .is_ok()
    );
    for bad in [
        good.replace(",-1,", ",2,"),
        good.replace("0xFFFFFFFFFFFFFFFF", "18446744073709551615"),
        good.replace("0xFFFFFFFFFFFFFFFF", "0x10000000000000000"),
        good.replace("0xFFFFFFFFFFFFFFFF", ""),
        format!("{good}0,99,0,0x0000000000000000\n"),
    ] {
        assert!(
            EdgeTxCsvReader
                .read_with_reported_controls(&p, bad.as_bytes(), W::EdgeTx65bb97db, &roles)
                .is_err()
        );
    }
    for selected in [
        vec![("absent", K::Switch)],
        vec![roles[0], roles[0]],
        vec![("SA", K::LoggedPulse)],
    ] {
        assert!(
            EdgeTxCsvReader
                .read_with_reported_controls(
                    &p,
                    good.as_bytes(),
                    W::OpenTxFb8622ddTaranisHorus,
                    &selected
                )
                .is_err()
        );
    }
}
