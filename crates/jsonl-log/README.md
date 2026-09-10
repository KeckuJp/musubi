# Decoded JSONL log reader

An offline library for exports containing one JSON object per line with a `meta.type` string and
a `data` object. This includes the shape produced by `pymavlink`'s JSON log exporter. The external
decoder is not bundled or invoked. Raw binary logs and live connections are not supported.

```rust
let input = br#"{"meta":{"type":"INFO"},"data":{"note":"synthetic"}}
{"meta":{"type":"SAMPLE"},"data":{"TimeUS":1200,"value":3.5,"new_field":true}}"#;
let report = musubi_jsonl_log::parse(input)?;
assert_eq!(report.records.len(), 2);
assert_eq!(report.untimed_records, 1);
let timed = &report.timed_records[0];
assert_eq!(timed.boot_us, 1200);
assert_eq!(report.records[timed.record_index]["data"]["new_field"], true);
# Ok::<(), musubi_jsonl_log::ParseError>(())
```

Run `cargo run -p musubi-jsonl-log --example inspect -- EXPORT.jsonl` to inspect a file.
The report retains every parsed record, including untimed metadata and unknown nested fields;
timed entries point into that same collection. `records.len()` equals `timed_records.len()` plus
`untimed_records`. An error returns no partial success.

## Semantics and limits

- `TimeUS` is a nonnegative integer counter; `TimeMS` is multiplied by 1,000 with overflow checks.
  If both occur they must agree. Exporter `meta.timestamp` never becomes an authoritative clock.
- Record order is preserved, not sorted. Counter regressions or equal counters remain visible;
  this library does not infer clock resets, synchronization, causality or an absolute time origin.
- Numeric values are already decoded and are not wire-scaled again. Signed/unsigned 64-bit integers
  and finite floating-point values use the JSON library's representation. Floating decimals round
  to f64. Integers outside the signed/unsigned 64-bit range may also round to f64; exact retention
  of such extensions is not supported. Exact arbitrary-precision decimal spelling, whitespace and
  object-key order are not retained.
- Units, platform domain, firmware identity and downstream observation-channel mapping are the
  caller's responsibility. Retaining a field does not establish its meaning or a device's identity.
- Malformed UTF-8/JSON, duplicate keys at any depth, missing type/data, invalid counters and inputs
  with no timed record are rejected. A final newline is allowed; blank records are not.
- The complete export is held in memory. Apply input-size limits at your ingestion boundary.
- Synthetic tests cover this API, not compatibility with all exporters, firmware versions or devices.

Dependencies are `serde` and `serde_json` (`float_roundtrip` enabled). No network or decoder dependency
is added to the core. Source preview only; this component produces a retained-record report, not
a platform-state object or a domain/clock assurance decision.
