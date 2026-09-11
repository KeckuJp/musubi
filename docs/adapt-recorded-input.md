# Adapt a recorded input with an existing reader

This worked path connects a decoded JSONL reader to the public common model through
the adapter SPI. It is an offline development example, not device support or live
integration. Python is optional; Rust is pinned in `rust-toolchain.toml`.

## 1. Investigate before choosing a change

Keep the original file. Establish the format, numeric units, coordinate frame, time
basis, source identity and rights from the producer's documentation. A filename or
plausible value is not evidence of those meanings. Inventory unknown fields and
untimed records; do not remove them to make a parser succeed.

Choose the smallest layer that actually resolves the mismatch:

| Mismatch | Change |
|---|---|
| Existing decoded shape, different field names | Mapping profile |
| Same meaning, different delimiters or time units | Small conversion before the reader |
| Unsupported binary encoding or structural version | Decoder or reader, with independent input tests |
| New meaning fits an existing public type | Adapter mapping, preserving unmodeled fields separately |
| Meaning cannot be represented truthfully | Stop that mapping and propose a model change; never coerce it |

For example, local-map pose is not WGS-84 latitude/longitude. This example accepts
only caller-declared WGS-84 degrees and altitude metres above mean sea level.
See [the adapter contract](../crates/adapter-spi/README.md) for exact fields and limits.

## 2. First input: connect the reader to the common model

From the repository root, create a fresh `example-run/` directory with `mkdir example-run`.
If that name already exists, choose another unused name and replace it consistently
below; do not overwrite an earlier run. Keep it outside any real recording archive.
Save this authored synthetic input as `example-run/input.jsonl` (one JSON object per line):

```jsonl
{"meta":{"type":"META"},"data":{"label":"synthetic-only"}}
{"meta":{"type":"LOCATION"},"data":{"TimeUS":1001,"Latitude":12.25,"Longitude":34.5,"Height":8.0,"Fix":3,"extra":{"quality_label":"unverified"}}}
{"meta":{"type":"OTHER"},"data":{"TimeUS":1001,"retained":true}}
{"meta":{"type":"LOCATION"},"data":{"TimeUS":1001,"Latitude":12.5,"Longitude":34.75,"Height":9.0,"Fix":3}}
```

Save `example-run/profile.json`:

```json
{"message_type":"LOCATION","latitude_field":"Latitude","longitude_field":"Longitude","altitude_field":"Height","position_units":"deg-m-msl","fix_field":"Fix","min_fix":3}
```

From the repository root, provide the paths and actual ingestion time in Unix
milliseconds. For this deterministic synthetic replay only, use the fixed replay
ingestion timestamp below. Do not present it as a measured historical receipt time.

```sh
cargo run --quiet --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- example-run/input.jsonl example-run/profile.json synthetic-a 1789084800000 > example-run/report.json
cargo test --locked -p musubi-adapter-spi --test recorded_jsonl
```

Expect four retained records, two mapped envelopes, one untimed index, one unmapped
index and no rejection. The two same-time positions stay separate. Each mapped
envelope contains a public `PlatformState`, a core-derived MARK and content digest.
Boot counters stay counters: `observed_at` is null, `time_confidence` and claim
confidence are zero, and domain is `Unknown`. No trustworthy UTC or physical domain
is inferred from the JSON. Reception time is epoch milliseconds, not microseconds.

## 3. Reuse on a different input: change the profile, not the core

Save `example-run/second.jsonl` and `example-run/second-profile.json`:

```jsonl
{"meta":{"type":"FIX"},"data":{"TimeUS":2020,"north_deg":-10.5,"east_deg":20.25,"msl_m":5,"vendor_extension":[1,2]}}
{"meta":{"type":"FIX"},"data":{"TimeUS":2010,"north_deg":-10.25,"east_deg":20.5,"msl_m":6}}
```

```json
{"message_type":"FIX","latitude_field":"north_deg","longitude_field":"east_deg","altitude_field":"msl_m","position_units":"deg-m-msl"}
```

```sh
cargo run --quiet --locked -p musubi-adapter-spi --example ingest_recorded_jsonl -- example-run/second.jsonl example-run/second-profile.json synthetic-b 1789084800000 > example-run/second-report.json
```

Expect two mapped records with source indices 0 and 1, counters 2020 and 2010, and
the original extension retained. This reader preserves source order; it does not
sort counters or claim chronology. The previous profile must fail on this input
because it selects no timed records. Unsupported units, duplicate profile keys,
out-of-range coordinates, malformed records and insufficient fix also fail.
The tests demonstrate these rejection cases and envelope tamper detection.

## 4. What the output does and does not preserve

`records` retains all parsed source values, not original whitespace or arbitrary-precision
number spelling. The [JSONL reader's numeric limits](../crates/jsonl-log/README.md)
apply (64-bit integers and finite f64 values); keep the original bytes separately.
The four outcome
collections are disjoint: untimed, unmapped timed, mapped timed, rejected timed.
Keep the complete report, not just `mapped`. Partial rejection emits a report and
exits nonzero; malformed input or profile emits an error instead. Always inspect
exit status. Shell redirection can create or overwrite an output even on failure;
use new output paths.

The source records, mapping profile, boot counters and report indices are an
**unsealed sidecar**. Envelope digests cover mapped common-model and claim content,
not the entire report or source evidence. Digests are tamper-evident, not signatures,
authentication or proof of sensor accuracy. This example does not seal every unknown
field or provide an archival evidence format. Keep originals and profiles separately.

## 5. Use Codex or Claude Code as an engineering assistant

Both tools can use the same instructions below. They are optional development tools,
not runtime dependencies, and they grant no merge/publication/device authority.
Use a separate working copy with public source and authorized synthetic inputs only.
Do not include recordings, credentials, private source or internal project history in
an AI session unless that exact material is separately authorized for that provider.

> Read this recipe and the recorded JSONL adapter README. Inventory both synthetic
> inputs and their declared meanings. Explain which layer must change and why.
> Create the input and profile files in a fresh directory. Run both conversions and
> the relevant tests; inspect every mapped position, timestamp and retained extension.
> Prove the first profile rejects the second input, then change only the profile to
> adapt it. Do not modify core/types, drop fields, infer UTC or promote domain trust.
> Save commands, actual outcomes, changed files, unresolved limits and the next step.
> Do not push, merge, publish, install packages or contact a device.

Another session should be able to use the saved files and commands without the
original conversation. A proposed profile is not success until it is executed and
checked on both inputs. Record provider success/failure separately from adapter
results. These synthetic examples demonstrate the procedure, not a real firmware
version change, clean-machine installation or independent human maintenance.
