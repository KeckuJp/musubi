# Recorded miniSEED2 reader

`musubi-miniseed2` decodes a deliberately limited subset of recorded miniSEED 2.x files.
It has no dependencies and performs no networking. The library takes bytes and returns
records; the optional example reads one local file and writes decoded samples to stdout.

```sh
cargo test -p musubi-miniseed2
cargo run --quiet -p musubi-miniseed2 --example inspect -- recording.mseed
```

The example outputs CSV columns `record_index,sample_index,time_unix_us,value_count`.
Record identity is available through `Record::source_id()` in the library, not guessed from
the file name. Parse errors exit nonzero without sample output. No recording is bundled.

```rust
let records = musubi_miniseed2::parse(&bytes)?;
for record in &records {
    for (index, value) in record.samples.iter().enumerate() {
        let declared_time_us = record.sample_time_unix_us(index);
        // Keep record.source_id(), declared_time_us and the instrument count together.
    }
}
```

## Accepted subset and retained information

- miniSEED2 big-endian fixed headers and big-endian Steim-1/Steim-2 compressed data only.
- Record length and encoding come from blockette 1000. Blockette 1001 timing metadata is
  retained. Other blockettes are retained as raw bytes, except blockette 100, whose sample-rate
  override is explicitly rejected instead of ignored.
- Source timestamps apply the declared header correction only when its activity flag says
  it is not already applied, plus blockette 1001's signed microsecond offset. Raw time and
  correction fields remain separately available.
- Values are instrument counts, not velocity or acceleration. No instrument response is
  applied. Timing-quality and locked-clock flags are data, not verified clock confidence.
- Unknown blockettes, record flags, gaps and zero-sample records are not silently discarded.
  Parsing errors reject the whole input; no partial record list is returned.

## Limits

This is a structural parsing component, not an equipment-specific adapter or common-model
mapping. Callers must retain identity and interpret units/time before integration.
It does not establish device, firmware or all-version compatibility.

miniSEED3, full SEED volumes, other sample encodings, little-endian records, nonintegral
microsecond sample periods, unsupported calendar values and inconsistent/truncated records
are rejected. Records of the same stream may share a start time but cannot move backwards.
No filtering, response removal, live acquisition or device commands are provided.
Parsing loads the input and decoded samples in memory; callers should bound file sizes.
Only call `sample_time_unix_us` with indices from the decoded samples, and do not mutate a
record's public time fields before using its arithmetic helper methods.

Implementation reference: [SEED Reference Manual 2.4](https://www.fdsn.org/pdf/SEEDManual_V2.4.pdf).
No third-party decoder or third-party test recording is included. Tests construct synthetic
headers and packed differences; passing them is not field validation.

License: Apache-2.0, as provided by the workspace LICENSE and NOTICE.
