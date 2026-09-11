use musubi_decoded_csv::{Value, parse, parse_non_decreasing};

#[test]
fn equal_times_preserve_every_field_and_source_order() {
    let input = b"time (us),x (m),state,extra\n9007199254740993,1.25,first,\n9007199254740993,-2.5,second,kept\n9007199254740994,0,third,last\n";
    let table = parse_non_decreasing(input, "time").unwrap();
    assert_eq!(table.columns.len(), 4);
    assert_eq!(table.columns[1].unit.as_deref(), Some("m"));
    assert_eq!(table.columns[3].name, "extra");
    assert_eq!(table.rows.len(), 3);
    assert_eq!(
        table.rows.iter().map(|row| row.boot_us).collect::<Vec<_>>(),
        [9007199254740993, 9007199254740993, 9007199254740994]
    );
    for (index, row) in table.rows.iter().enumerate() {
        assert_eq!(row.source_line, index + 2);
        assert_eq!(row.values.len(), 4);
    }
    assert_eq!(
        table.rows[0].values,
        vec![
            Value::Integer(9007199254740993),
            Value::Number(1.25),
            Value::Text("first".into()),
            Value::Blank
        ]
    );
    assert_eq!(
        table.rows[1].values,
        vec![
            Value::Integer(9007199254740993),
            Value::Number(-2.5),
            Value::Text("second".into()),
            Value::Text("kept".into())
        ]
    );
    assert_eq!(
        table.rows[2].values,
        vec![
            Value::Integer(9007199254740994),
            Value::Integer(0),
            Value::Text("third".into()),
            Value::Text("last".into())
        ]
    );
    assert_eq!(parse(input, "time").unwrap_err().line, 3);
}

#[test]
fn allowing_equal_time_still_rejects_decreasing_time() {
    let input = b"time,x\n1001,1\n1001,2\n1000,3\n";
    assert_eq!(parse_non_decreasing(input, "time").unwrap_err().line, 4);
}

#[test]
fn allowing_equal_time_does_not_relax_other_validation() {
    for input in [
        "time (ms),x\n1,2\n",
        "time,x\n1,2\n1,NaN\n",
        "time,x\n1,2\n1\n",
        "time,x\n1,2\n1.5,3\n",
    ] {
        assert!(parse_non_decreasing(input.as_bytes(), "time").is_err());
    }
}
