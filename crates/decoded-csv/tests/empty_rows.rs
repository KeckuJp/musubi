use musubi_decoded_csv::{ParseOptions, parse_with_options};

#[test]
fn explicit_empty_still_validates_header_and_every_row() {
    let options = ParseOptions {
        allow_empty_rows: true,
        ..Default::default()
    };
    assert!(parse_with_options(b"time,x\n", "time", Default::default()).is_err());
    let table = parse_with_options(b"time,x\n", "time", options).unwrap();
    assert!(table.rows.is_empty());
    assert_eq!(table.columns.len(), 2);
    for bytes in [
        b"".as_slice(),
        b"time\n",
        b"x,y\n",
        b"time,time\n",
        b"time,x\n\n",
        b"time,x\n1\n",
        b"time (s),x\n",
    ] {
        assert!(parse_with_options(bytes, "time", options).is_err());
    }
    assert_eq!(
        parse_with_options(b"time,x\n1,2\n", "time", options).unwrap(),
        parse_with_options(b"time,x\n1,2\n", "time", Default::default()).unwrap()
    );
}
