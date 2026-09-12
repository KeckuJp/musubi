# Recorded JSONL adapter example

Reuse `musubi-jsonl-log` for reading, then the adapter SPI helpers and core `Normalizer`,
`derive_mark` and `seal_digest` for evidence. The existing text template remains available.
This example reads files offline and has no device write path.

## Input and mapping

Input is decoded JSONL: each line is an object with `meta.type` (nonempty string) and
`data` (object). The reader preserves every parsed value, including unknown fields and
untimed metadata. This is parsed-value retention, not arbitrary-precision numeric or
byte-for-byte retention; see the [reader's numeric limits](../jsonl-log/README.md).
`data.TimeUS` is a nonnegative integer boot counter in microseconds;
the reader also supports `TimeMS`, rejecting conflicting counters. These are not UTC.

Declare the selected message and fields in a JSON object, for example:

```json
{
  "message_type": "LOCATION",
  "latitude_field": "Latitude",
  "longitude_field": "Longitude",
  "altitude_field": "Height",
  "position_units": "deg-m-msl",
  "domain": "unknown",
  "fix_field": "Fix",
  "min_fix": 3
}
```

The first five keys are required. `domain` defaults to `unknown`; other accepted values
are `air`, `surface`, `ground`. `fix_field` and unsigned integer `min_fix` must appear
together or both be absent. A selected record with a missing, noninteger or insufficient
fix is rejected. Latitude, longitude and altitude must all be finite JSON numbers.
Latitude must be within -90..90 and longitude within -180..180.

Only `deg-m-msl` is accepted: WGS-84 decimal degrees and altitude in metres above mean
sea level. This is the caller's declaration, not independently verified sensor metadata.
There is no scaling, coordinate transformation, frame inference or local-pose conversion.
Unknown profile keys, duplicate keys and unsupported unit declarations are errors.
Change field names in a second profile to reuse the same adapter with another input;
do not add record-specific exceptions. Field names refer directly to members of `data`.
Input extensions are retained even though profile extensions are rejected.

For observations from multiple recorded channels of the **same platform**, optionally
add `"source_channel_path":["data","source_record","I"]`. This is a path of 1..8
literal object keys starting at the record root, not dotted syntax, wildcard matching
or array indexing. Each key obeys the identifier limits below. The selected value must
be a string of 1..64 ASCII letters, digits, `_`, `-` or `.`. Labels such as `01` remain
distinct from `1`; numeric JSON values are not coerced. Missing or invalid labels in
selected timed records produce indexed rejections, never a fallback channel.

The mapping adds `recorded-channel:<label>` and `channel-identity:source-asserted`
to common claim provenance, covered by the existing envelope digest. The platform ID
remains the caller's vehicle label. A channel label is not authenticated identity, a
physical receiver count, a valid-fix assertion or a fusion instruction. Domain, time
and confidence policies are unchanged. Without this option, output and digest behavior
remain unchanged. The rest of the source sidecar is still unsealed.

## Run

```sh
cargo run -p musubi-adapter-spi --example ingest_recorded_jsonl -- input.jsonl profile.json source-a 1789084800000
```

Replace the last argument with the actual ingestion time in Unix epoch milliseconds.
The API is `recorded_jsonl::Mapping::parse(profile_bytes)`, followed by
`recorded_jsonl::ingest(input_bytes, &mapping, source_id, received_at)`.
`RecordedJsonlNormalizer::new(mapping)` also implements the existing `Normalizer` for a
single timed, selected JSONL record supplied as a `RawObservation`.

The CLI emits `recorded-jsonl-report/v1` JSON with `records` (all original parsed values),
`mapped`, `rejected`, `untimed_indices`, and `unmapped_indices`. Indices are zero-based
source record indices. Untimed records are classified first; other message types among
timed records are unmapped; selected timed records either map or have an indexed INVALID
error. These four outcomes are disjoint and account for every parsed record. Malformed
JSONL is refused atomically. Having no selected timed records is an error. Partial
rejections still emit the complete report and produce a nonzero exit status.

Mapped items include COM position, platform ID, declared domain, timestamps, confidence,
MARK, reason, provenance and content digest. `observed_at` is always null, time confidence
is zero, and the core derives the resulting MARK. Claim confidence is zero with
`AdapterAssigned` basis. `boot_us` remains a counter in the report. `received_at` is copied
from the caller, never fabricated from boot time. A source ID must identify one source.

The source values, counter and report indices are an **unsealed sidecar**. The envelope
digest covers mapped COM and claim content only. It does not bind the raw records,
profile or this diagnostic JSON serialization. Source whitespace is not preserved.
Signatures and source authentication are outside this example.

Input is capped at 16 MiB, each source record at 64 KiB, the profile at 4096 bytes and
JSON nesting at 32 before parsing. Identifiers are bounded to 256 bytes and reject
control/bidi characters and semicolon separators. The profile is parsed using the
reader's duplicate-key rejection with an internal synthetic envelope; its sentinel time
is never observation data. Ordinary tests use generated synthetic values only.
