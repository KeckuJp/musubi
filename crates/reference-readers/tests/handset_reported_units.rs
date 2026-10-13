use musubi_reference_readers::{
    FieldValue, Observation, ProfileReader,
    edgetx::{EdgeTxCsvReader, HandsetUnitTable},
    profile::parse_profile,
};

fn value<'a>(o: &'a Observation, key: &str) -> &'a FieldValue {
    &o.fields.iter().find(|(k, _)| k == key).unwrap().1
}

#[test]
fn fixed_scalar_units_share_batch_stream_and_preserve_clock() {
    let cases = [
        ("kts", 10., "m/s", 18520. / 3600.),
        ("m/s", 2., "m/s", 2.),
        ("f/s", 10., "m/s", 3.048),
        ("kmh", 36., "m/s", 10.),
        ("mph", 10., "m/s", 4.4704),
        ("m", 3., "m", 3.),
        ("ft", 10., "m", 3.048),
        ("km", 2., "m", 2000.),
        ("°C", 0., "K", 273.15),
        ("°F", 32., "K", 273.15),
        ("@C", 20., "K", 293.15),
        ("@F", 212., "K", 373.15),
        ("%", 50., "ratio", 0.5),
        ("mAh", 1000., "C", 3600.),
        ("°", 180., "rad", std::f64::consts::PI),
        ("@", 180., "rad", std::f64::consts::PI),
        ("rad", 1., "rad", 1.),
        ("ml", 1000., "m3", 0.001),
        ("mlm", 60., "m3/s", 0.000001),
        ("Hz", 2., "Hz", 2.),
        ("ms", 2., "s", 0.002),
        ("mS", 2., "s", 0.002),
        ("us", 2., "s", 0.000002),
        ("uS", 2., "s", 0.000002),
        ("dB", -3., "dB", -3.),
        ("dBm", -10., "dBm", -10.),
        ("dbm", -20., "dBm", -20.),
        ("g", 2., "g_acceleration_reported", 2.),
        ("fOz", 3., "fOz_reported_volume", 3.),
    ];
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
        let p = parse_profile(profile, "test").unwrap();
        let columns = cases
            .iter()
            .enumerate()
            .map(|(i, (u, _, _, _))| format!("S{i}({u})"))
            .collect::<Vec<_>>()
            .join(",");
        let cells = cases
            .iter()
            .map(|(_, v, _, _)| v.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let (header, time) = if rtc {
            ("Date,Time", "2026-09-01,10:11:12.340")
        } else {
            ("tmr10ms", "3000")
        };
        let input = format!("{header},RQly(%),{columns}\n{time},99,{cells}\n");
        let native = EdgeTxCsvReader.read(&p, input.as_bytes()).unwrap();
        let batch = EdgeTxCsvReader
            .read_with_reported_units(&p, input.as_bytes())
            .unwrap();
        for (i, (u, _, unit, expected)) in cases.iter().enumerate() {
            let prefix = format!("handset_si.S{i}({u})");
            assert_eq!(
                value(&batch[0], &format!("{prefix}.unit")),
                &FieldValue::Text((*unit).into())
            );
            let FieldValue::F64(actual) = value(&batch[0], &format!("{prefix}.value")) else {
                panic!("missing scalar")
            };
            assert!((actual - expected).abs() < 1e-9, "{u}: {actual}");
        }
        let mut restored = batch[0].clone();
        restored.fields.truncate(native[0].fields.len());
        assert_eq!(restored, native[0]);
        let mut streamed = Vec::new();
        EdgeTxCsvReader
            .read_stream_with_reported_units(&p, &mut input.as_bytes(), |o| {
                streamed.push(o);
                Ok(())
            })
            .unwrap();
        assert_eq!(batch, streamed);
    }
}

#[test]
fn affine_temperature_invalidity_and_unknown_unit_are_explicit() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let input = "tmr10ms,RQly(%),Zero(°F),Cold(°C),Missing(°C),Future(K),Bad(°F)\n1,99,-459.67,-274,,123,bad\n";
    let row = EdgeTxCsvReader
        .read_with_reported_units(&p, input.as_bytes())
        .unwrap()
        .remove(0);
    assert_eq!(
        value(&row, "handset_si.Zero(°F).value"),
        &FieldValue::F64(0.)
    );
    assert_eq!(
        value(&row, "handset_si.Cold(°C).disposition"),
        &FieldValue::Text("BELOW_ABSOLUTE_ZERO".into())
    );
    assert_eq!(
        value(&row, "handset_si.Missing(°C).value"),
        &FieldValue::Blank
    );
    assert_eq!(value(&row, "handset_si.Bad(°F).value"), &FieldValue::Blank);
    assert!(
        !row.fields
            .iter()
            .any(|(k, _)| k.starts_with("handset_si.Future"))
    );
    let nonfinite = input.replace(",bad\n", ",NaN\n");
    assert!(
        EdgeTxCsvReader
            .read_with_reported_units(&p, nonfinite.as_bytes())
            .is_err()
    );
}

#[test]
fn reported_electrical_units_preserve_native_values_and_stream_contract() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let input = b"tmr10ms,RQly(%),RxBt(V),Curr(mA),TPWR(mW),Unknown\n3000,100,12.34,-250,1000,opaque\n3100,0,0,,0,other\n";
    let native = EdgeTxCsvReader.read(&p, input).unwrap();
    let batch = EdgeTxCsvReader.read_with_reported_units(&p, input).unwrap();
    assert_eq!(
        value(&batch[0], "handset_si.RxBt(V).value"),
        &FieldValue::F64(12.34)
    );
    assert_eq!(
        value(&batch[0], "handset_si.Curr(mA).value"),
        &FieldValue::F64(-0.25)
    );
    assert_eq!(
        value(&batch[0], "handset_si.TPWR(mW).value"),
        &FieldValue::F64(1.0)
    );
    assert_eq!(
        value(&batch[1], "handset_si.Curr(mA).value"),
        &FieldValue::Blank
    );
    assert_eq!(
        value(&batch[1], "handset_si.TPWR(mW).value"),
        &FieldValue::F64(0.0)
    );
    for (before, after) in native.iter().zip(&batch) {
        let mut restored = after.clone();
        restored.fields.truncate(before.fields.len());
        assert_eq!(&restored, before);
        assert_eq!(
            value(after, "handset_si.RxBt(V).basis"),
            &FieldValue::Text("REPORTED_HEADER_UNIT_VALIDITY_UNQUALIFIED".into())
        );
    }
    let mut streamed = Vec::new();
    let counts = EdgeTxCsvReader
        .read_stream_with_reported_units(&p, &mut input.as_slice(), |o| {
            streamed.push(o);
            Ok(())
        })
        .unwrap();
    assert_eq!(batch, streamed);
    assert_eq!(counts.records_read, 2);
    assert_eq!(counts.emitted_records, 2);
}

#[test]
fn independent_rtc_labels_unknown_units_and_precision_are_not_guessed() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/rtc-date-time.profile.toml"),
        "test",
    )
    .unwrap();
    let input = b"Date,Time,RQly(%),Load(A),Load(W),Other(kW),Huge(V),Text(V),Tiny(mA)\n2026-09-01,10:11:12.340,99,-1.2,15.25,2,9007199254740993,not_numeric,5e-324\n";
    let o = EdgeTxCsvReader
        .read_with_reported_units(&p, input)
        .unwrap()
        .remove(0);
    assert_eq!(
        value(&o, "handset_si.Load(A).value"),
        &FieldValue::F64(-1.2)
    );
    assert_eq!(
        value(&o, "handset_si.Load(W).unit"),
        &FieldValue::Text("W".into())
    );
    assert_eq!(value(&o, "Other(kW)"), &FieldValue::I64(2));
    assert!(
        !o.fields
            .iter()
            .any(|(k, _)| k.starts_with("handset_si.Other"))
    );
    assert_eq!(value(&o, "handset_si.Huge(V).value"), &FieldValue::Blank);
    assert_eq!(value(&o, "handset_si.Text(V).value"), &FieldValue::Blank);
    assert_eq!(value(&o, "handset_si.Tiny(mA).value"), &FieldValue::Blank);
    assert_eq!(
        value(&o, "handset_si.Tiny(mA).disposition"),
        &FieldValue::Text("DERIVED_NONFINITE_OR_UNDERFLOW".into())
    );
    assert_eq!(o.clock_basis, musubi_reference_readers::ClockBasis::Unknown);
    assert!(o.wall_ms.is_none());
}

#[test]
fn reserved_names_refuse_without_changing_default_reader() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let input = b"tmr10ms,RQly(%),handset_si.injected\n3000,100,1\n";
    assert!(EdgeTxCsvReader.read(&p, input).is_ok());
    assert!(EdgeTxCsvReader.read_with_reported_units(&p, input).is_err());
    let mut acknowledged = 0;
    let error = EdgeTxCsvReader
        .read_stream_with_reported_units(&p, &mut input.as_slice(), |_| {
            acknowledged += 1;
            Ok(())
        })
        .unwrap_err();
    assert_eq!(acknowledged, 0);
    assert_eq!(error.counts.emitted_records, 0);
}

/// Header unit text is only interpreted where every pinned writer translation gives it one
/// dimension: EdgeTX 65bb97db `i18n/tw.h` writes UNIT_G as "rpm", French "km/h" truncates to
/// "km/", and Ukrainian writes "mm" for ml/min. None of these may be converted.
#[test]
fn writer_unit_text_shared_by_two_dimensions_or_unlisted_stays_raw() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let input =
        "tmr10ms,RQly(%),Rot(rpm),Spd(km/),Flow(mm),Acc(g)\n5,99,-1.25,36,60,1.5\n5,98,,1,2,3\n";
    let native = EdgeTxCsvReader.read(&p, input.as_bytes()).unwrap();
    let rows = EdgeTxCsvReader
        .read_with_reported_units(&p, input.as_bytes())
        .unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].t_ms, rows[1].t_ms);
    assert_eq!(value(&rows[0], "Rot(rpm)"), &FieldValue::F64(-1.25));
    assert_eq!(
        value(&rows[0], "handset_si.Rot(rpm).value"),
        &FieldValue::Blank
    );
    assert_eq!(
        value(&rows[0], "handset_si.Rot(rpm).unit"),
        &FieldValue::Text("UNQUALIFIED".into())
    );
    for row in &rows {
        assert_eq!(
            value(row, "handset_si.Rot(rpm).disposition"),
            &FieldValue::Text("WRITER_UNIT_TEXT_SHARED_BY_RPM_AND_G".into())
        );
    }
    assert_eq!(value(&rows[1], "Rot(rpm)"), &FieldValue::Blank);
    assert_eq!(
        value(&rows[0], "handset_si.Acc(g).unit"),
        &FieldValue::Text("g_acceleration_reported".into())
    );
    for row in &rows {
        assert!(
            !row.fields
                .iter()
                .any(|(k, _)| k.starts_with("handset_si.Spd") || k.starts_with("handset_si.Flow"))
        );
    }
    for (before, after) in native.iter().zip(&rows) {
        let mut restored = after.clone();
        restored.fields.truncate(before.fields.len());
        assert_eq!(&restored, before);
    }
    // Ukrainian "вузл." truncated to three bytes is not UTF-8: refused, never relabelled.
    let mut broken = b"tmr10ms,RQly(%),Spd(".to_vec();
    broken.extend_from_slice(&"вузл.".as_bytes()[..3]);
    broken.extend_from_slice(b")\n1,99,5\n");
    assert!(
        EdgeTxCsvReader
            .read_with_reported_units(&p, &broken)
            .is_err()
    );
}

fn declared(
    table: HandsetUnitTable,
    input: &str,
    columns: &[&str],
) -> Result<Vec<Observation>, musubi_reference_readers::ReadError> {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    EdgeTxCsvReader.read_with_declared_units(&p, input.as_bytes(), table, columns)
}

/// A declared fixed writer/build-language table resolves `rpm`; the header alone still does not.
#[test]
fn declared_english_tables_resolve_rpm_and_keep_raw_and_unselected_columns() {
    for (table, celsius) in [
        (HandsetUnitTable::EdgeTx65bb97dbEnglish, "°C"),
        (HandsetUnitTable::OpenTxFb8622ddEnglish, "@C"),
    ] {
        let temp = format!("T({celsius})");
        let input = format!(
            "tmr10ms,RQly(%),Rot(rpm),Acc(g),{temp},RxBt(V)\n7,99,120,1.5,20,12.3\n7,98,-60,,-300,0\n"
        );
        let columns = ["Rot(rpm)", "Acc(g)", temp.as_str()];
        let rows = declared(table, &input, &columns).unwrap();
        assert_eq!(rows[0].t_ms, rows[1].t_ms);
        assert_eq!(
            value(&rows[0], "handset_si.Rot(rpm).value"),
            &FieldValue::F64(2.0)
        );
        assert_eq!(
            value(&rows[1], "handset_si.Rot(rpm).value"),
            &FieldValue::F64(-1.0)
        );
        assert_eq!(
            value(&rows[0], "handset_si.Rot(rpm).unit"),
            &FieldValue::Text("Hz".into())
        );
        assert_eq!(value(&rows[1], "Rot(rpm)"), &FieldValue::I64(-60));
        assert_eq!(
            value(&rows[1], "handset_si.Acc(g).disposition"),
            &FieldValue::Text("BLANK".into())
        );
        let key = format!("handset_si.{temp}.disposition");
        assert_eq!(
            value(&rows[1], &key),
            &FieldValue::Text("BELOW_ABSOLUTE_ZERO".into())
        );
        let FieldValue::Text(basis) = value(&rows[0], "handset_si.Rot(rpm).basis") else {
            panic!("basis")
        };
        assert!(basis.starts_with("CALLER_DECLARED_WRITER_TABLE_"));
        for row in &rows {
            assert!(
                !row.fields
                    .iter()
                    .any(|(k, _)| k.starts_with("handset_si.RxBt"))
            );
        }
        let mut streamed = Vec::new();
        let p = parse_profile(
            include_str!(
                "../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"
            ),
            "test",
        )
        .unwrap();
        let counts = EdgeTxCsvReader
            .read_stream_with_declared_units(&p, &mut input.as_bytes(), table, &columns, |o| {
                streamed.push(o);
                Ok(())
            })
            .unwrap();
        assert_eq!(streamed, rows);
        assert_eq!(counts.emitted_records, 2);
        let native = EdgeTxCsvReader.read(&p, input.as_bytes()).unwrap();
        for (before, after) in native.iter().zip(&rows) {
            let mut restored = after.clone();
            restored.fields.truncate(before.fields.len());
            assert_eq!(&restored, before);
        }
    }
}

#[test]
fn declared_tables_refuse_contradictions_and_keep_tw_rpm_unresolved() {
    let tw = HandsetUnitTable::EdgeTx65bb97dbTw;
    let rows = declared(
        tw,
        "tmr10ms,RQly(%),Rot(rpm),Dt(mS)\n1,99,120,5\n",
        &["Rot(rpm)", "Dt(mS)"],
    )
    .unwrap();
    // tw writes both UNIT_RPMS and UNIT_G as "rpm": still two meanings, so nothing is converted.
    assert_eq!(
        value(&rows[0], "handset_si.Rot(rpm).value"),
        &FieldValue::Blank
    );
    assert_eq!(
        value(&rows[0], "handset_si.Rot(rpm).disposition"),
        &FieldValue::Text("DECLARED_TABLE_TEXT_HAS_TWO_MEANINGS".into())
    );
    assert_eq!(
        value(&rows[0], "handset_si.Dt(mS).value"),
        &FieldValue::F64(0.005)
    );
    // Texts the declared table never writes contradict the declaration.
    for (table, column) in [
        (tw, "Acc(g)"),
        (tw, "Dt(ms)"),
        (HandsetUnitTable::EdgeTx65bb97dbEnglish, "T(@C)"),
        (HandsetUnitTable::OpenTxFb8622ddEnglish, "T(°C)"),
        (HandsetUnitTable::OpenTxFb8622ddEnglish, "S(km/)"),
    ] {
        let input = format!("tmr10ms,RQly(%),{column}\n1,99,5\n");
        assert!(declared(table, &input, &[column]).is_err(), "{column}");
    }
    let en = HandsetUnitTable::EdgeTx65bb97dbEnglish;
    let input = "tmr10ms,RQly(%),Rot(rpm),Name\n1,99,5,x\n";
    for bad in [
        &[][..],
        &["Absent(V)"],
        &["Name"],
        &["Rot(rpm)", "Rot(rpm)"],
    ] {
        assert!(declared(en, input, bad).is_err(), "{bad:?}");
    }
}

/// A label carrying its own parenthesis cannot be told apart from the writer suffix by the
/// header alone; the default path withholds it, a declaration must name the column.
#[test]
fn label_parenthesis_does_not_fabricate_a_header_unit() {
    let p = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/boot-counter.profile.toml"),
        "test",
    )
    .unwrap();
    let input = b"tmr10ms,RQly(%),(a)(V),x)(A),Plain(V)\n1,99,5,6,7\n";
    let row = EdgeTxCsvReader
        .read_with_reported_units(&p, input)
        .unwrap()
        .remove(0);
    for key in ["(a)(V)", "x)(A)"] {
        assert_eq!(
            value(&row, &format!("handset_si.{key}.value")),
            &FieldValue::Blank
        );
        assert_eq!(
            value(&row, &format!("handset_si.{key}.disposition")),
            &FieldValue::Text("LABEL_PARENTHESIS_UNIT_UNATTRIBUTABLE".into())
        );
    }
    assert_eq!(
        value(&row, "handset_si.Plain(V).value"),
        &FieldValue::F64(7.0)
    );
}
