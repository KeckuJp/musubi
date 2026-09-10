use musubi_decoded_csv::{Value, parse};

#[test]
fn preserves_unknown_columns_units_values_and_microseconds() {
    let table = parse(b"time (us),vendor_counter,custom (mV),state\n1001,42,3.5,ready\n1999,90071992547409930,,idle\n", "time").unwrap();
    assert_eq!(table.columns[2].unit.as_deref(), Some("mV"));
    assert_eq!(table.rows[0].boot_us, 1001);
    assert_eq!(table.rows[1].boot_us, 1999);
    assert_eq!(table.rows[1].source_line, 3);
    assert_eq!(table.rows[1].values[1], Value::Integer(90071992547409930));
    assert_eq!(table.rows[1].values[2], Value::Blank);
    assert_eq!(table.rows[0].values[3], Value::Text("ready".into()));
}

#[test]
fn malformed_tables_never_return_partial_success() {
    for input in [
        "",
        "time,x",
        "time,,x\n1,2,3\n",
        "time,x,x (mV)\n1,2,3\n",
        "time,x\n1\n",
        "time,x\n1,2,3\n",
        "time,x\n1,2\n\n2,3\n",
        "time,x\n1,2\n2\n",
        "time\n1\n",
    ] {
        assert!(parse(input.as_bytes(), "time").is_err(), "{input:?}");
    }
    assert!(parse(&[0xff], "time").is_err());
}

#[test]
fn raw_and_quoted_input_are_rejected() {
    for input in [
        "H Product:Blackbox flight data recorder\nI\0",
        "other,x\n1,2\n",
        "time,x\n1,\"2\"\n",
    ] {
        assert!(parse(input.as_bytes(), "time").is_err());
    }
}

#[test]
fn time_is_not_silently_coerced_or_reordered() {
    for time in ["-1", "1.5", "NaN", "inf", "9223372036854775808"] {
        assert!(parse(format!("time,x\n{time},1\n").as_bytes(), "time").is_err());
    }
    for second in ["1", "0"] {
        assert!(parse(format!("time,x\n1,2\n{second},3\n").as_bytes(), "time").is_err());
    }
    assert!(parse(b"time,x\n1,NaN\n", "time").is_err());
}

#[test]
fn unsupported_time_units_are_not_stripped() {
    for unit in ["ms", "s", "", "us)extra"] {
        assert!(parse(format!("time ({unit}),x\n1,2\n").as_bytes(), "time").is_err());
    }
    assert!(parse(b"tick (us),x\n1,2\n", "tick").is_ok());
    assert!(parse(b"time,x\n1,2\n", "time").is_ok());
}
