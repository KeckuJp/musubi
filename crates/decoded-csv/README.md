# Decoded telemetry CSV

`musubi-decoded-csv` reads an unquoted decoded telemetry CSV into rows with a
boot-relative microsecond timestamp and all source columns. Unknown column names,
unit suffixes, text, empty values and column order are retained. It has no dependencies.
Cell text is trimmed and finite numeric cells are typed; retention is semantic, not
byte-for-byte preservation of whitespace or number spelling.

```rust
let table = musubi_decoded_csv::parse(
    b"time (us),voltage (mV),vendor_status\n1001,12000,ready\n",
    "time",
)?;
assert_eq!(table.rows[0].boot_us, 1001);
# Ok::<(), musubi_decoded_csv::ParseError>(())
```

Pass the declared time column name for another exporter. A bare time column means
microseconds; explicit `ms` or `s` is rejected. Timestamps must increase strictly
and fit a nonnegative signed 64-bit microsecond counter. The parser does not infer
an absolute time, platform domain, or measurement conversion. Map these explicitly
in the consuming reader or adapter and preserve fields not yet understood.

Raw BBL/BFL, quoted CSV, ragged rows, duplicate normalized headers, empty datasets
and non-finite numbers are rejected. A malformed later row fails the entire parse.
This is a decoded-file component, not a firmware decoder, live connection or device
compatibility guarantee. The tests use authored synthetic inputs; no third-party
log or source is bundled.

Run `cargo test -p musubi-decoded-csv`.
