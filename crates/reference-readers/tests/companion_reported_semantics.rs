//! Authored fixed saveSession shapes, not Companion execution or radio recordings.
use musubi_reference_readers::{
    FieldValue, Observation, ProfileReader,
    edgetx::{EdgeTxCsvReader, HandsetCompoundKind},
    profile::parse_profile,
};

fn field<'a>(row: &'a Observation, key: &str) -> &'a FieldValue {
    &row.fields.iter().find(|(k, _)| k == key).unwrap().1
}

#[test]
fn companion_two_save_shapes_reuse_typed_meanings_and_stream_accounting() {
    let profile = parse_profile(
        include_str!("../../../profiles/declared/edgetx-handset-clock/rtc-date-time.profile.toml"),
        "test",
    )
    .unwrap();
    for epoch in [false, true] {
        let (extra_header, extra_value) = if epoch {
            (",MSecsSinceEpoch", ",0")
        } else {
            ("", "")
        };
        let input = format!(
            "Date,Time{extra_header},RQly(%),RxBt(V),Current(mA),Power(W),Speed(kmh),Distance(ft),GPS,Future(furlong)\n2026-10-09,10:20:30.000{extra_value},90,12.5,500,6.25,36,10,-0.123456 139.123456,7\n2026-10-09,10:20:30.000{extra_value},0,,0,0,0,0,,8\n"
        );
        let before = EdgeTxCsvReader.read(&profile, input.as_bytes()).unwrap();
        let roles = [("GPS", HandsetCompoundKind::GpsPair)];
        let rows = EdgeTxCsvReader
            .read_with_reported_fields(&profile, input.as_bytes(), &roles)
            .unwrap();
        for (key, number, unit) in [
            ("RxBt(V)", 12.5, "V"),
            ("Current(mA)", 0.5, "A"),
            ("Power(W)", 6.25, "W"),
            ("Speed(kmh)", 10.0, "m/s"),
            ("Distance(ft)", 3.048, "m"),
            ("RQly(%)", 0.9, "ratio"),
        ] {
            assert_eq!(
                field(&rows[0], &format!("handset_si.{key}.value")),
                &FieldValue::F64(number)
            );
            assert_eq!(
                field(&rows[0], &format!("handset_si.{key}.unit")),
                &FieldValue::Text(unit.into())
            );
        }
        assert_eq!(
            field(&rows[0], "handset_reported.GPS.latitude_deg"),
            &FieldValue::F64(-0.123456)
        );
        assert_eq!(
            field(&rows[0], "handset_reported.GPS.longitude_deg"),
            &FieldValue::F64(139.123456)
        );
        assert_eq!(
            field(&rows[1], "handset_si.RxBt(V).disposition"),
            &FieldValue::Text("BLANK".into())
        );
        assert!(
            !rows[1]
                .fields
                .iter()
                .any(|(k, _)| k.ends_with("latitude_deg"))
        );
        for (row, original) in rows.iter().zip(&before) {
            assert!(
                !row.fields
                    .iter()
                    .any(|(k, _)| k.starts_with("handset_si.Future"))
            );
            let mut restored = row.clone();
            restored.fields.truncate(original.fields.len());
            assert_eq!(&restored, original); // All source fields, Unknown clock, freshness and ordering.
        }
        let mut stream = Vec::new();
        let counts = EdgeTxCsvReader
            .read_stream_with_reported_fields(&profile, &mut input.as_bytes(), &roles, |row| {
                stream.push(row);
                Ok(())
            })
            .unwrap();
        assert_eq!(stream, rows);
        assert_eq!(
            (
                counts.records_read,
                counts.emitted_records,
                counts.bytes_read
            ),
            (2, 2, input.len() as u64)
        );
        let broken = input + "2026-10-09,broken\n";
        let error = EdgeTxCsvReader
            .read_stream_with_reported_fields(&profile, &mut broken.as_bytes(), &roles, |_| Ok(()))
            .unwrap_err();
        assert_eq!(error.counts.emitted_records, 2);
        assert_eq!(error.counts.records_read, 3);
    }
}
