use musubi_decoded_csv::{ParseOptions, Value, parse, parse_with_options};

#[test]
fn default_stays_strict_and_explicit_option_preserves_nonfinite_spelling() {
    let input = b"time,finite,nan_text,positive,negative\n1001,2.5,NaN,inf,-inf\n";
    assert!(parse(input, "time").is_err());
    assert!(parse_with_options(input, "time", ParseOptions::default()).is_err());
    let table = parse_with_options(
        input,
        "time",
        ParseOptions {
            allow_equal_time: false,
            preserve_nonfinite_as_text: true,
        },
    )
    .unwrap();
    assert_eq!(
        table.rows[0].values,
        vec![
            Value::Integer(1001),
            Value::Number(2.5),
            Value::Text("NaN".into()),
            Value::Text("inf".into()),
            Value::Text("-inf".into())
        ]
    );
}

#[test]
fn preservation_cannot_promote_invalid_time_or_bypass_time_order() {
    let preserve = ParseOptions {
        allow_equal_time: false,
        preserve_nonfinite_as_text: true,
    };
    for time in ["nan", "inf", "-inf", "-1", "9223372036854775808"] {
        let input = format!("time,unknown\n{time},nan\n");
        assert!(
            parse_with_options(input.as_bytes(), "time", preserve).is_err(),
            "{time}"
        );
    }
    let equal = b"time,unknown\n1,nan\n1,inf\n";
    assert!(parse_with_options(equal, "time", preserve).is_err());
    let both = ParseOptions {
        allow_equal_time: true,
        preserve_nonfinite_as_text: true,
    };
    assert_eq!(
        parse_with_options(equal, "time", both).unwrap().rows.len(),
        2
    );
    assert!(parse_with_options(b"time,unknown\n2,nan\n1,inf\n", "time", both).is_err());
    assert!(parse_with_options(b"time,unknown\n1\n", "time", both).is_err());
}
