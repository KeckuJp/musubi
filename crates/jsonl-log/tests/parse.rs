use musubi_jsonl_log::parse;
use serde_json::json;

#[test]
fn preserves_all_records_values_and_source_order() {
    let input = br#"{"meta":{"type":"SCHEMA","extra":true},"data":{"nested":{"items":[null,"future",7]}}}
{"meta":{"type":"POSITION","timestamp":1509241629.8634121},"data":{"TimeUS":2001,"huge":18446744073709551615,"value":35.123456789012345},"extension":{"keep":true}}
{"meta":{"type":"FUTURE"},"data":{"TimeMS":1,"unknown":"retained"}}
{"meta":{"type":"POSITION"},"data":{"TimeUS":1000,"TimeMS":1}}"#;
    let report = parse(input).expect("synthetic records");
    assert_eq!(report.records.len(), 4);
    assert_eq!(report.untimed_records, 1);
    assert_eq!(
        report.records[0]["data"]["nested"],
        json!({"items":[null,"future",7]})
    );
    assert_eq!(report.records[0]["meta"]["extra"], true);
    assert_eq!(report.records[1]["extension"], json!({"keep":true}));
    assert_eq!(report.records[1]["data"]["huge"].as_u64(), Some(u64::MAX));
    assert_eq!(
        report.records[1]["meta"]["timestamp"]
            .as_f64()
            .unwrap()
            .to_bits(),
        "1509241629.8634121".parse::<f64>().unwrap().to_bits()
    );
    assert_eq!(
        report.records[1]["data"]["value"]
            .as_f64()
            .unwrap()
            .to_bits(),
        "35.123456789012345".parse::<f64>().unwrap().to_bits()
    );
    assert_eq!(report.records[2]["data"]["unknown"], "retained");
    assert_eq!(
        report
            .timed_records
            .iter()
            .map(|r| (r.record_index, r.boot_us))
            .collect::<Vec<_>>(),
        vec![(1, 2001), (2, 1000), (3, 1000)]
    );
}

#[test]
fn accepts_zero_and_largest_unsigned_microsecond_timestamp() {
    let input = br#"{"meta":{"type":"SAMPLE"},"data":{"TimeUS":0}}
{"meta":{"type":"SAMPLE"},"data":{"TimeUS":18446744073709551615}}"#;
    let report = parse(input).unwrap();
    assert_eq!(report.timed_records[0].boot_us, 0);
    assert_eq!(report.timed_records[1].boot_us, u64::MAX);
}

#[test]
fn rejects_malformed_shape_empty_and_metadata_only() {
    for input in [
        "",
        "\n",
        "not json",
        "[]",
        "null",
        "{}",
        r#"{"meta":[],"data":{"TimeUS":1}}"#,
        r#"{"meta":{"type":"X"},"data":[]}"#,
        r#"{"meta":{"type":null},"data":{"TimeUS":1}}"#,
        r#"{"meta":{"type":""},"data":{"TimeUS":1}}"#,
        r#"{"meta":{},"data":{"TimeUS":1}}"#,
        r#"{"meta":{"type":"META"},"data":{"description":"untimed"}}"#,
    ] {
        assert!(parse(input.as_bytes()).is_err(), "accepted {input:?}");
    }
    assert!(parse(b"\xff").is_err());
    let valid = "{\"meta\":{\"type\":\"X\"},\"data\":{\"TimeUS\":1}}";
    for suffix in ["\n\n", "\n \n", "\n{}", "\nnot json"] {
        assert!(parse(format!("{valid}{suffix}").as_bytes()).is_err());
    }
}

#[test]
fn rejects_invalid_or_conflicting_timestamps() {
    for data in [
        r#"{"TimeUS":-1}"#,
        r#"{"TimeUS":1.5}"#,
        r#"{"TimeUS":"1"}"#,
        r#"{"TimeUS":null}"#,
        r#"{"TimeUS":true}"#,
        r#"{"TimeUS":18446744073709551616}"#,
        r#"{"TimeMS":18446744073709552}"#,
        r#"{"TimeMS":-1}"#,
        r#"{"TimeMS":0.5}"#,
        r#"{"TimeMS":1,"TimeUS":1001}"#,
    ] {
        let input = format!("{{\"meta\":{{\"type\":\"X\"}},\"data\":{data}}}");
        assert!(parse(input.as_bytes()).is_err(), "accepted {data}");
    }
}

#[test]
fn duplicate_keys_are_rejected_at_every_depth() {
    for input in [
        r#"{"meta":{"type":"X"},"data":{"TimeUS":1},"data":{"TimeUS":2}}"#,
        r#"{"meta":{"type":"X","type":"Y"},"data":{"TimeUS":1}}"#,
        r#"{"meta":{"type":"X"},"data":{"TimeUS":1,"TimeUS":1}}"#,
        r#"{"meta":{"type":"X"},"data":{"TimeUS":1,"nested":{"a":1,"a":2}}}"#,
        r#"{"meta":{"type":"X"},"data":{"TimeUS":1,"array":[{"a":1,"\u0061":2}]}}"#,
    ] {
        assert!(
            parse(input.as_bytes()).is_err(),
            "accepted duplicate keys: {input}"
        );
    }
}
