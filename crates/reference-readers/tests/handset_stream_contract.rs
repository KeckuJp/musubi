use musubi_reference_readers::{
    ClockBasis, FieldValue, ProfileReader, edgetx::EdgeTxCsvReader, field, profile::parse_profile,
};
use std::io::Cursor;

const PROFILE: &str = include_str!("fixtures/edgetx-handset-clock--boot-counter.profile.toml");

#[test]
fn stream_crosses_old_capacity_without_collecting_observations() {
    let profile = parse_profile(PROFILE, "authored").unwrap();
    let mut input = String::from("tmr10ms,RQly(%),uninterpreted\n");
    let text = "x".repeat(32768);
    for i in 0..600 {
        input.push_str(&format!("{},90,{text}\n", i / 2));
    }
    assert!(input.len() > 16 * 1024 * 1024);
    assert!(EdgeTxCsvReader.read(&profile, input.as_bytes()).is_err());
    let mut emitted = 0u64;
    let counts = EdgeTxCsvReader
        .read_stream(&profile, &mut Cursor::new(input.as_bytes()), |row| {
            assert_eq!(row.clock_basis, ClockBasis::BootRelative);
            assert_eq!(row.t_ms, (emitted / 2 * 10) as i64);
            assert!(row.anchor_unix_us.is_none());
            assert_eq!(
                field(&row, "uninterpreted"),
                Some(&FieldValue::Text(text.clone()))
            );
            emitted += 1;
            Ok(())
        })
        .unwrap();
    assert_eq!(
        (counts.records_read, counts.emitted_records, emitted),
        (600, 600, 600)
    );
    assert_eq!(counts.bytes_read, input.len() as u64);
}

#[test]
fn small_stream_matches_batch_with_equal_time_and_last_row_without_newline() {
    for (source, text) in [
        (
            PROFILE,
            "tmr10ms,RQly(%),label\n1,0,\n1,80,\"text,雪\"\n2,90,last",
        ),
        (
            include_str!("fixtures/edgetx-handset-clock--rtc-date-time.profile.toml"),
            "Date,Time,RQly(%)\n2026-01-02,03:04:05.000,90\n2026-01-02,03:04:05.000,80",
        ),
    ] {
        let profile = parse_profile(source, "authored").unwrap();
        let batch = EdgeTxCsvReader.read(&profile, text.as_bytes()).unwrap();
        let mut streamed = Vec::new();
        EdgeTxCsvReader
            .read_stream(&profile, &mut Cursor::new(text.as_bytes()), |row| {
                streamed.push(row);
                Ok(())
            })
            .unwrap();
        assert_eq!(format!("{batch:?}"), format!("{streamed:?}"));
    }
}

#[test]
fn decode_reset_and_output_failures_report_the_committed_prefix() {
    let profile = parse_profile(PROFILE, "authored").unwrap();
    for bad in ["0,80", "2", "2,NaN"] {
        let input = format!("tmr10ms,RQly(%)\n1,90\n{bad}\n");
        let mut emitted = 0;
        let failure = EdgeTxCsvReader
            .read_stream(&profile, &mut Cursor::new(input.as_bytes()), |_| {
                emitted += 1;
                Ok(())
            })
            .unwrap_err();
        assert_eq!(emitted, 1);
        assert_eq!(failure.counts.emitted_records, 1);
        assert_eq!(failure.counts.records_read, 2);
        assert_eq!(failure.line_number, 3);
    }
    let failure = EdgeTxCsvReader
        .read_stream(
            &profile,
            &mut Cursor::new(b"tmr10ms,RQly(%)\n1,90\n2,80\n"),
            |_| Err("destination refused row".into()),
        )
        .unwrap_err();
    assert_eq!(failure.counts.emitted_records, 0);
    assert_eq!(failure.counts.records_read, 1);
}

#[test]
fn per_line_bound_does_not_allow_an_oversized_tail_to_be_emitted() {
    let profile = parse_profile(PROFILE, "authored").unwrap();
    let input = format!(
        "tmr10ms,RQly(%),extra\n1,90,ok\n2,80,{}\n",
        "x".repeat(65536)
    );
    let mut rows = 0;
    let failure = EdgeTxCsvReader
        .read_stream(&profile, &mut Cursor::new(input.as_bytes()), |_| {
            rows += 1;
            Ok(())
        })
        .unwrap_err();
    assert_eq!(rows, 1);
    assert_eq!(failure.counts.emitted_records, 1);
    assert_eq!(failure.line_number, 3);
}
